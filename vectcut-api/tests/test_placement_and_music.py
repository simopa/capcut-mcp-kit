# Added in capcut-mcp-kit (2026): overlapping items go to a free track; background music fills the
# edit with fades and ducks under the voice. See NOTICE at the repository root.
import shutil
import subprocess

import pytest

from draft_cache import DRAFT_CACHE
import music

HOST = {"Host": "127.0.0.1:9001", "X-CapCut-Kit-Token": "test-token"}


@pytest.fixture
def client():
    import capcut_server
    return capcut_server.app.test_client()


def call(client, route, **payload):
    return client.post(route, json=payload, headers=HOST).get_json()


def new_draft(client):
    return call(client, "/create_draft", width=1080, height=1920)["output"]["draft_id"]


def media(tmp_path, name, seconds, video=True):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    path = tmp_path / name
    src = (["-f", "lavfi", "-i", f"color=c=blue:s=320x240:d={seconds}"] if video else []) + \
          ["-f", "lavfi", "-i", f"sine=duration={seconds}"]
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *src, "-shortest", str(path)], check=True)
    return str(path)


def test_overlapping_text_goes_to_a_free_track(client):
    draft_id = new_draft(client)
    assert call(client, "/add_text", draft_id=draft_id, text="A", start=0, end=4)["success"]
    second = call(client, "/add_text", draft_id=draft_id, text="B", start=1, end=2)
    third = call(client, "/add_text", draft_id=draft_id, text="C", start=1.5, end=3)
    fourth = call(client, "/add_text", draft_id=draft_id, text="D", start=5, end=6)

    assert second["output"]["moved_to_free_track"] == [{"requested_track": "text_main", "track": "text_main_2", "start": 1.0}]
    assert third["output"]["moved_to_free_track"][0]["track"] == "text_main_3"
    assert "moved_to_free_track" not in fourth["output"]
    tracks = DRAFT_CACHE[draft_id].tracks
    assert [len(tracks[n].segments) for n in ("text_main", "text_main_2", "text_main_3")] == [2, 1, 1]
    assert tracks["text_main_2"].render_index > tracks["text_main"].render_index


def test_auto_track_can_be_turned_off(client):
    draft_id = new_draft(client)
    call(client, "/add_text", draft_id=draft_id, text="A", start=0, end=4)
    body = call(client, "/add_text", draft_id=draft_id, text="B", start=1, end=2, auto_track=False)
    assert body["success"] is False
    assert "overlap" in body["error"].lower()


def test_overlapping_video_becomes_an_overlay(client, tmp_path):
    clip = media(tmp_path, "a.mp4", 4)
    draft_id = new_draft(client)
    assert call(client, "/add_video", draft_id=draft_id, video_url=clip, start=0, end=4)["success"]
    body = call(client, "/add_video", draft_id=draft_id, video_url=clip, start=0, end=2, target_start=1)
    assert body["output"]["moved_to_free_track"][0]["track"] == "video_main_2"


def test_duck_curve_dips_during_speech():
    pts = music.duck_curve([(2.0, 3.0)], volume=0.4, level=0.5, start=0, end=10)
    assert pts == [(0, 0.4), (1.75, 0.4), (2.0, 0.2), (3.0, 0.2), (3.5, 0.4), (10, 0.4)]


def test_duck_curve_merges_into_continuous_dips():
    pts = music.duck_curve([(1.0, 2.0), (2.1, 3.0)], volume=1, level=0.3, start=0, end=5)
    times = [t for t, _ in pts]
    assert times == sorted(times) and len(times) == len(set(times))


def test_music_loops_to_fill_the_edit_with_fades_and_ducking(client, tmp_path, monkeypatch):
    clip = media(tmp_path, "talk.mp4", 10)
    song = media(tmp_path, "song.wav", 4, video=False)
    draft_id = new_draft(client)
    assert call(client, "/add_video", draft_id=draft_id, video_url=clip, start=0, end=10)["success"]
    import media_analysis
    monkeypatch.setattr(media_analysis, "timeline_words",
                        lambda d, p: [{"word": "ciao", "start": 5.0, "end": 6.0}])

    body = call(client, "/add_background_music", draft_id=draft_id, audio_url=song, volume=0.5,
                duck_under=clip, duck_level=0.2)

    assert body["success"], body
    assert (body["output"]["pieces"], body["output"]["end"], body["output"]["ducked_spans"]) == (3, 10.0, 1)
    segs = DRAFT_CACHE[draft_id].tracks["music"].segments
    assert [(s.target_timerange.start, s.target_timerange.duration) for s in segs] == \
        [(0, 4_000_000), (4_000_000, 4_000_000), (8_000_000, 2_000_000)]
    assert segs[0].fade.in_duration == 1_000_000 and segs[-1].fade.out_duration == 1_000_000  # capped at half
    middle = {k.time_offset: k.values[0] for k in segs[1].common_keyframes[0].keyframes}
    assert middle[1_000_000] == pytest.approx(0.1)  # 5.0 s on the timeline: ducked to 0.5 * 0.2
    assert middle[0] == pytest.approx(0.5)


def test_music_needs_a_timeline(client, tmp_path):
    song = media(tmp_path, "song.wav", 2, video=False)
    body = call(client, "/add_background_music", draft_id=new_draft(client), audio_url=song)
    assert body["success"] is False and "empty" in body["error"]


def test_timeline_lists_tracks_with_their_end(client):
    draft_id = new_draft(client)
    call(client, "/add_text", draft_id=draft_id, text="A", start=0, end=4)
    call(client, "/add_text", draft_id=draft_id, text="B", start=2, end=7)
    body = call(client, "/timeline", draft_id=draft_id)
    assert body["output"]["tracks"] == [{"name": "text_main", "type": "text", "clips": 1, "end": 4.0},
                                        {"name": "text_main_2", "type": "text", "clips": 1, "end": 7.0}]
