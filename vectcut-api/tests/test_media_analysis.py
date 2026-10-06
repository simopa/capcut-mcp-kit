# Added in capcut-mcp-kit (2026): subtitles, pause detection and transcript cache (Whisper is never run).
# See NOTICE at the repository root.
import json
import os
import re
import shutil
import subprocess
import threading
import time

import pytest

import media_analysis as ma

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def srt_times(srt):
    def secs(t):
        h, m, rest = t.split(":")
        s, ms = rest.split(",")
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000
    return [(secs(a), secs(b)) for a, b in re.findall(r"(\S+) --> (\S+)", srt)]


def assert_valid_cues(times):
    for s, e in times:
        assert e > s
    for (_, e1), (s2, _) in zip(times, times[1:]):
        assert s2 >= e1


# ---------------------------------------------------------------- SRT

def test_short_punctuated_words_do_not_overlap():
    words = [{"word": "Sì.", "start": 0.0, "end": 0.05}, {"word": "No.", "start": 0.1, "end": 0.15},
             {"word": "Forse", "start": 0.3, "end": 0.6}]
    srt, count = ma.build_srt(words)
    times = srt_times(srt)
    assert count == len(times)
    assert_valid_cues(times)
    assert "Sì." in srt and "No." in srt and "Forse" in srt


def test_cue_with_no_room_is_merged_into_the_next():
    words = [{"word": "A.", "start": 1.0, "end": 1.0}, {"word": "B", "start": 1.0, "end": 1.4}]
    srt, count = ma.build_srt(words)
    assert count == 1
    assert "A. B" in srt
    assert_valid_cues(srt_times(srt))


def test_last_cue_keeps_minimum_duration():
    srt, _ = ma.build_srt([{"word": "Ciao", "start": 2.0, "end": 2.01}])
    assert srt_times(srt) == [(2.0, 2.2)]


def test_many_random_words_never_overlap():
    import random
    rnd = random.Random(7)
    t, words = 0.0, []
    for i in range(300):
        t += rnd.choice([0, 0.01, 0.05, 0.2, 0.7])
        words.append({"word": rnd.choice(["a", "b.", "ciao", "ok!", "va?"]), "start": t, "end": t + rnd.random() * 0.3})
    srt, count = ma.build_srt(words)
    times = srt_times(srt)
    assert count == len(times)
    assert_valid_cues(times)


# ---------------------------------------------------------------- timeline words

def test_repeated_source_range_yields_words_twice(tmp_path, monkeypatch):
    video = tmp_path / "talk.mp4"
    video.write_bytes(b"x")
    transcript = {"segments": [{"words": [{"word": "ciao", "start": 1.0, "end": 1.5}]}]}
    monkeypatch.setattr(ma, "load_cached", lambda path, *a: transcript)
    monkeypatch.setitem(ma.DRAFT_TIMEMAPS, "d-repeat", [])
    ma.record_placement("d-repeat", str(video), 0, 2, 0)
    ma.record_placement("d-repeat", str(video), 0, 2, 5)

    words = ma.timeline_words("d-repeat", str(video))

    assert [w["start"] for w in words] == [1.0, 6.0]


# ---------------------------------------------------------------- pauses

@needs_ffmpeg
def test_unreadable_file_is_an_error_not_all_speech(tmp_path):
    fake = tmp_path / "broken.mp4"
    fake.write_bytes(b"not a video at all")
    with pytest.raises(RuntimeError):
        ma.speech_ranges(str(fake), start=0, end=2, method="audio")


def test_invalid_range_is_refused(tmp_path):
    f = tmp_path / "a.wav"
    f.write_bytes(b"x")
    with pytest.raises(ValueError):
        ma.speech_ranges(str(f), start=3, end=1, method="audio")


@needs_ffmpeg
def test_silence_is_detected_in_a_real_file(tmp_path):
    wav = tmp_path / "tone.wav"
    # 1 s tone, 1.5 s silence, 1 s tone
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                    "-filter_complex", "[1]atrim=duration=1.5[s];[0][s][0]concat=n=3:v=0:a=1",
                    str(wav)], check=True)
    plan = ma.speech_ranges(str(wav), method="audio", padding=0)
    assert len(plan["ranges"]) == 2
    assert plan["removed_duration"] == pytest.approx(1.5, abs=0.1)


# ---------------------------------------------------------------- transcript cache

def fake_transcript(path, model="turbo", language="it"):
    return {"source": str(path), "model": model, "engine": "test", "language": language,
            "duration": 1.0, "segments": [{"start": 0, "end": 1, "text": "ciao", "words": []}]}


def test_same_stem_different_extension_do_not_share_a_transcript(tmp_path):
    mp4, wav = tmp_path / "Review.mp4", tmp_path / "Review.wav"
    mp4.write_bytes(b"same")
    wav.write_bytes(b"same")
    os.utime(wav, ns=(os.stat(mp4).st_atime_ns, os.stat(mp4).st_mtime_ns))
    ma._save_transcript(str(mp4), fake_transcript(mp4), ma._fingerprint(str(mp4)))

    assert ma.load_cached(str(mp4)) is not None
    assert ma.load_cached(str(wav)) is None
    assert (tmp_path / "Review.mp4.transcript.json").exists()


def test_legacy_sidecar_is_still_read(tmp_path):
    video = tmp_path / "talk.mp4"
    video.write_bytes(b"v")
    data = fake_transcript(video)
    st = os.stat(video)
    data.update(source_size=st.st_size, source_mtime=int(st.st_mtime))  # old format: no mtime_ns
    (tmp_path / "talk.transcript.json").write_text(json.dumps(data))

    assert ma.load_cached(str(video))["engine"] == "test"


def test_corrupt_cache_is_a_miss(tmp_path):
    video = tmp_path / "talk.mp4"
    video.write_bytes(b"v")
    (tmp_path / "talk.mp4.transcript.json").write_text("{ not json")
    assert ma.load_cached(str(video)) is None


def test_language_and_model_are_part_of_the_cache_key(tmp_path):
    video = tmp_path / "talk.mp4"
    video.write_bytes(b"v")
    ma._save_transcript(str(video), fake_transcript(video, "turbo", "it"), ma._fingerprint(str(video)))
    assert ma.load_cached(str(video), "turbo", "it") is not None
    assert ma.load_cached(str(video), "turbo", "en") is None
    assert ma.load_cached(str(video), "small") is None


def test_modified_file_invalidates_cache(tmp_path):
    video = tmp_path / "talk.mp4"
    video.write_bytes(b"v")
    ma._save_transcript(str(video), fake_transcript(video), ma._fingerprint(str(video)))
    st = os.stat(video)
    os.utime(video, ns=(st.st_atime_ns, st.st_mtime_ns + 1000))
    assert ma.load_cached(str(video)) is None


def test_finished_job_restarts_when_its_transcript_is_gone(tmp_path, monkeypatch):
    video = tmp_path / "talk.mp4"
    video.write_bytes(b"v")
    runs = []

    def fake_run(path, model, language):
        runs.append(path)
        return fake_transcript(path, model, language)
    monkeypatch.setattr(ma, "_run_transcription", fake_run)
    monkeypatch.setattr(ma, "_jobs", {})

    first = ma.transcribe(str(video), language="it", wait=5)
    assert first["status"] == "done" and first["transcript"]
    os.remove(tmp_path / "talk.mp4.transcript.json")

    second = ma.transcribe(str(video), language="it", wait=5)

    assert second["status"] == "done" and second["transcript"] is not None
    assert len(runs) == 2


def test_failed_job_is_reported_then_retried(tmp_path, monkeypatch):
    video = tmp_path / "talk.mp4"
    video.write_bytes(b"v")
    calls = {"n": 0}

    def flaky(path, model, language):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("model download failed")
        return fake_transcript(path, model, language)
    monkeypatch.setattr(ma, "_run_transcription", flaky)
    monkeypatch.setattr(ma, "_jobs", {})

    with pytest.raises(RuntimeError, match="model download failed"):
        ma.transcribe(str(video), wait=5)
    assert ma.transcribe(str(video), wait=5)["status"] == "done"


def test_transcriptions_run_one_at_a_time(tmp_path, monkeypatch):
    active, peak = [0], [0]
    lock = threading.Lock()

    def slow(path, model, language):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.2)
        with lock:
            active[0] -= 1
        return fake_transcript(path, model, language)
    monkeypatch.setattr(ma, "_run_transcription", slow)
    monkeypatch.setattr(ma, "_jobs", {})
    files = []
    for i in range(3):
        f = tmp_path / f"v{i}.mp4"
        f.write_bytes(b"v")
        files.append(f)

    for f in files:
        ma.transcribe(str(f), wait=0)
    for f in files:
        assert ma.transcribe(str(f), wait=5)["status"] == "done"
    assert peak[0] == 1
