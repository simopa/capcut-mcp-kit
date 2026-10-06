# Added in capcut-mcp-kit: speech transcription, pause detection, pause-free assembly and
# automatic subtitles that follow the edit. See NOTICE at the repository root.
"""
Media analysis for content-aware editing.

- Transcription runs locally with Whisper: mlx-whisper on Apple Silicon (GPU), faster-whisper
  elsewhere, one file at a time. The result is saved next to the video (<file>.transcript.json with
  word timings, and a readable <file>.transcript.txt, e.g. talk.mp4.transcript.json), so a video is
  transcribed once and its files stay together.
- Pauses come from the transcript's word timings when available (robust to background noise),
  otherwise from ffmpeg's silencedetect.
- Subtitles generated from the source transcript are placed by reading the draft's own clips
  (source range, timeline position, speed), so they land right even after cuts and repeats.
"""

import hashlib
import json
import os
import platform
import re
import subprocess
import tempfile
import threading
import time
from typing import Dict, List, Optional, Tuple

import pyJianYingDraft as draft
from pyJianYingDraft import trange, Clip_settings
from create_draft import get_or_create_draft
from draft_store import get_draft
from util import generate_draft_url, url_to_hash

# Used only when the video's own folder is not writable
FALLBACK_DIR = os.path.join(os.path.expanduser("~"), ".cache", "capcut-mcp-kit", "transcripts")

MODELS = {
    # name: (mlx-whisper repo, faster-whisper model)
    "turbo": ("mlx-community/whisper-large-v3-turbo", "large-v3-turbo"),
    "large": ("mlx-community/whisper-large-v3-mlx", "large-v3"),
    "small": ("mlx-community/whisper-small-mlx", "small"),
}

_jobs: Dict[str, dict] = {}
_jobs_lock = threading.Lock()
# Whisper saturates the GPU/CPU: run one transcription at a time, the others wait their turn
_worker_slots = threading.Semaphore(1)
_save_lock = threading.Lock()

TRANSCRIPT_FORMAT = 2


# ---------------------------------------------------------------- helpers

def _local_path(path: str) -> str:
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"File not found: {path}")
    return os.path.abspath(path)


def _transcript_paths(path: str) -> List[str]:
    """Where a file's transcript is written: next to it (the name keeps the extension, so
    talk.mp4 and talk.wav do not share one), else the fallback folder."""
    name = os.path.basename(path)
    tag = hashlib.sha1(path.encode()).hexdigest()[:10]
    return [os.path.join(os.path.dirname(path), f"{name}.transcript.json"),
            os.path.join(FALLBACK_DIR, f"{name}-{tag}.transcript.json")]


def _legacy_transcript_paths(path: str) -> List[str]:
    """Names used before the extension was kept (<stem>.transcript.json); still read."""
    stem = os.path.splitext(os.path.basename(path))[0]
    tag = hashlib.sha1(path.encode()).hexdigest()[:10]
    return [os.path.join(os.path.dirname(path), f"{stem}.transcript.json"),
            os.path.join(FALLBACK_DIR, f"{stem}-{tag}.transcript.json")]


def _fingerprint(path: str) -> dict:
    st = os.stat(path)
    return {"source_size": st.st_size, "source_mtime": int(st.st_mtime), "source_mtime_ns": st.st_mtime_ns}


def _matches(data: dict, path: str, model: Optional[str], language: Optional[str]) -> bool:
    """A saved transcript is valid only for the same file (name with extension, size, mtime),
    and for the model/language asked for, when given."""
    fp = _fingerprint(path)
    if os.path.basename(str(data.get("source", ""))) != os.path.basename(path):
        return False
    if data.get("source_size") != fp["source_size"]:
        return False
    if "source_mtime_ns" in data:
        if data["source_mtime_ns"] != fp["source_mtime_ns"]:
            return False
    elif data.get("source_mtime") != fp["source_mtime"]:
        return False
    if model is not None and data.get("model") != model:
        return False
    if language is not None and data.get("language") != language:
        return False
    return isinstance(data.get("segments"), list)


def _probe_duration(path: str) -> float:
    proc = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                          capture_output=True, text=True, timeout=60)
    try:
        duration = float(proc.stdout.strip())
    except ValueError:
        duration = 0.0
    if proc.returncode != 0 or duration <= 0:
        detail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "no duration"
        raise RuntimeError(f"Not a readable audio/video file: {path} ({detail})")
    return duration


def _write_atomic(target: str, text: str):
    """Write next to target and rename over it, so readers never see half a file."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".tmp-", suffix=".transcript")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _blocks_text(result: dict, max_seconds: float = 20) -> str:
    """Readable transcript: sentence blocks of up to ~20 s, one time range each."""
    def clock(t):
        m, sec = divmod(t, 60)
        return f"{int(m):02d}:{sec:04.1f}"
    lines, cur = [], None
    for seg in result["segments"]:
        if cur and (seg["end"] - cur[0] > max_seconds or seg["start"] - cur[1] > 2):
            lines.append(cur)
            cur = None
        cur = [cur[0], seg["end"], cur[2] + " " + seg["text"]] if cur else [seg["start"], seg["end"], seg["text"]]
        if re.search(r"[.!?]$", seg["text"]) and cur[1] - cur[0] > max_seconds / 2:
            lines.append(cur)
            cur = None
    if cur:
        lines.append(cur)
    return "".join(f"[{clock(a)} - {clock(b)}] {t}\n" for a, b, t in lines)


def _save_transcript(path: str, result: dict, fingerprint: dict):
    """fingerprint is taken before transcribing: a file changed meanwhile will not match it."""
    result.update(fingerprint, format=TRANSCRIPT_FORMAT)
    with _save_lock:
        for target in _transcript_paths(path):
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                _write_atomic(target, json.dumps(result, ensure_ascii=False))
                _write_atomic(target[:-len(".json")] + ".txt", _blocks_text(result))
                return
            except OSError:
                continue  # folder not writable: try the fallback
    raise OSError("Could not save the transcript next to the video nor in " + FALLBACK_DIR)


def _use_mlx() -> bool:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        return False
    try:
        import mlx_whisper  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------- transcription

def _run_transcription(path: str, model: str, language: Optional[str]) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        wav = os.path.join(tmp, "audio.wav")
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", path, "-vn", "-ac", "1", "-ar", "16000", wav],
                       check=True, capture_output=True)
        mlx_repo, fw_model = MODELS[model]
        if _use_mlx():
            import mlx_whisper
            raw = mlx_whisper.transcribe(wav, path_or_hf_repo=mlx_repo, language=language,
                                         word_timestamps=True, condition_on_previous_text=False)
            engine = "mlx-whisper"
            segments = [{
                "start": s["start"], "end": s["end"], "text": s["text"].strip(),
                "words": [{"start": w["start"], "end": w["end"], "word": w["word"].strip()} for w in s.get("words", [])]
            } for s in raw["segments"]]
            detected = raw.get("language", language)
        else:
            from faster_whisper import WhisperModel
            fw = WhisperModel(fw_model, device="auto", compute_type="int8")
            segs, info = fw.transcribe(wav, language=language, word_timestamps=True, vad_filter=True,
                                       condition_on_previous_text=False)
            engine = "faster-whisper"
            segments = [{
                "start": s.start, "end": s.end, "text": s.text.strip(),
                "words": [{"start": w.start, "end": w.end, "word": w.word.strip()} for w in (s.words or [])]
            } for s in segs]
            detected = info.language
    return {"source": path, "model": model, "engine": engine, "language": detected,
            "requested_language": language, "duration": _probe_duration(path), "segments": segments}


def _job_worker(job: dict, path: str, model: str, language: Optional[str]):
    try:
        with _worker_slots:
            fingerprint = _fingerprint(path)
            _save_transcript(path, _run_transcription(path, model, language), fingerprint)
        with _jobs_lock:
            job["status"] = "done"
    except Exception as e:  # surfaced to the caller on the next status check
        with _jobs_lock:
            job["status"] = "failed"
            job["error"] = str(e)


def transcribe(path: str, model: str = "turbo", language: Optional[str] = None, wait: float = 45) -> dict:
    """Start (or reuse) a transcription and wait up to `wait` seconds.

    Returns {"status": "done", "transcript": {...}} or {"status": "running", "elapsed": s}.
    Long files keep transcribing in the background; call again to collect the result.
    """
    if model not in MODELS:
        raise ValueError(f"Unknown model {model}; use one of {', '.join(MODELS)}")
    path = _local_path(path)
    key = f"{path}|{model}|{language or ''}"
    cached = load_cached(path, model, language)
    if cached is not None:
        return {"status": "done", "transcript": cached}

    with _jobs_lock:
        job = _jobs.get(key)
        if job is not None and job["status"] == "failed":
            # Reported once; the next call starts over
            del _jobs[key]
            raise RuntimeError(f"Transcription failed: {job['error']}")
        if job is not None and job["status"] == "done":
            cached = load_cached(path, model, language)
            if cached is not None:  # finished since the check above
                return {"status": "done", "transcript": cached}
            job = None  # the file changed or its transcript was deleted: transcribe again
        if job is None:
            job = {"status": "running", "started": time.time()}
            _jobs[key] = job
            threading.Thread(target=_job_worker, args=(job, path, model, language), daemon=True).start()

    deadline = time.time() + max(0.0, wait)
    while time.time() < deadline and job["status"] == "running":
        time.sleep(0.5)
    if job["status"] == "done":
        cached = load_cached(path, model, language)
        if cached is None:
            raise RuntimeError("Transcription finished but its result no longer matches the file "
                               "(was it modified meanwhile?): call again to transcribe it anew")
        return {"status": "done", "transcript": cached}
    if job["status"] == "failed":
        with _jobs_lock:
            if _jobs.get(key) is job:
                del _jobs[key]
        raise RuntimeError(f"Transcription failed: {job.get('error')}")
    return {"status": "running", "elapsed": round(time.time() - job["started"])}


def load_cached(path: str, model: Optional[str] = None, language: Optional[str] = None) -> Optional[dict]:
    """The file's saved transcript if it still matches the file (and the model/language, when
    given). A missing, unreadable or foreign transcript is simply a cache miss."""
    path = _local_path(path)
    for target in _transcript_paths(path) + _legacy_transcript_paths(path):
        try:
            with open(target, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and _matches(data, path, model, language):
            return data
    return None


# ---------------------------------------------------------------- pauses

def _audio_silences(path: str, start: float, end: Optional[float], noise_db: float, min_pause: float) -> List[Tuple[float, float]]:
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-ss", str(start)]
    if end is not None:
        cmd += ["-to", str(end)]
    cmd += ["-i", path, "-vn", "-af", f"silencedetect=noise={noise_db}dB:d={min_pause}", "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    out = proc.stderr
    if proc.returncode != 0:
        detail = out.strip().splitlines()[-1] if out.strip() else f"exit code {proc.returncode}"
        raise RuntimeError(f"ffmpeg could not analyse the audio of {path}: {detail}")
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", out)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", out)]
    silences = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else (end - start if end is not None else None)
        if e is None:
            continue
        silences.append((max(0.0, s) + start, e + start))
    return silences


def speech_ranges(path: str, start: float = 0, end: Optional[float] = None, min_pause: float = 0.7,
                  padding: float = 0.15, method: str = "auto", noise_db: float = -35,
                  min_clip: float = 0.4) -> dict:
    """Source ranges to keep (speech), dropping pauses longer than min_pause. Pieces shorter than
    min_clip after padding are dropped too (very short utterances included)."""
    path = _local_path(path)
    if start < 0 or (end is not None and end <= start):
        raise ValueError(f"Invalid range: start={start}, end={end} (need 0 <= start < end)")
    transcript = load_cached(path) if method in ("auto", "transcript") else None
    if method == "transcript" and transcript is None:
        raise RuntimeError("No transcript for this file yet: run capcut_transcribe first, or use method='audio'")

    if transcript is not None:
        duration = transcript["duration"]
        end = duration if end is None else min(end, duration)
        if start >= end:
            raise ValueError(f"start={start} is past the end of the file ({duration:.2f} s)")
        words = [w for s in transcript["segments"] for w in s["words"] if w["end"] > start and w["start"] < end]
        ranges: List[List[float]] = []
        for w in words:
            ws, we = max(w["start"], start), min(w["end"], end)
            if ranges and ws - ranges[-1][1] < min_pause:
                ranges[-1][1] = max(ranges[-1][1], we)
            else:
                ranges.append([ws, we])
        used = "transcript"
    else:
        duration = _probe_duration(path)
        end = duration if end is None else min(end, duration)
        if start >= end:
            raise ValueError(f"start={start} is past the end of the file ({duration:.2f} s)")
        silences = _audio_silences(path, start, end, noise_db, min_pause)
        ranges, cursor = [], start
        for s, e in silences:
            if s > cursor:
                ranges.append([cursor, s])
            cursor = max(cursor, e)
        if cursor < end:
            ranges.append([cursor, end])
        used = "audio"

    # Pad around speech, merge what touches, drop slivers
    padded: List[List[float]] = []
    for s, e in ranges:
        s, e = max(start, s - padding), min(end, e + padding)
        if padded and s <= padded[-1][1]:
            padded[-1][1] = max(padded[-1][1], e)
        else:
            padded.append([s, e])
    kept = [(round(s, 3), round(e, 3)) for s, e in padded if e - s >= min_clip]
    kept_total = sum(e - s for s, e in kept)
    return {"method": used, "start": start, "end": end, "ranges": kept,
            "original_duration": round(end - start, 2), "kept_duration": round(kept_total, 2),
            "removed_duration": round(end - start - kept_total, 2), "cuts": max(0, len(kept) - 1)}


# ---------------------------------------------------------------- timeline

def placements(script, path: str) -> List[dict]:
    """Where `path` sits on the draft's timeline: one entry per clip (video or audio) that uses it."""
    target = os.path.realpath(path)
    materials = {m.material_id: m for m in list(script.materials.videos) + list(script.materials.audios)}
    out = []
    for track in script.tracks.values():
        for seg in track.segments:
            material = materials.get(getattr(seg, "material_id", None))
            if material is None or not getattr(material, "remote_url", None):
                continue
            if os.path.realpath(os.path.expanduser(str(material.remote_url))) != target:
                continue
            speed = getattr(getattr(seg, "speed", None), "speed", 1.0) or 1.0
            src_start = seg.source_timerange.start / 1e6
            out.append({"track": track.name, "segment_id": seg.segment_id,
                        "src_start": src_start, "src_end": src_start + seg.source_timerange.duration / 1e6,
                        "tl_start": seg.target_timerange.start / 1e6, "speed": speed})
    return out


def add_video_without_pauses(draft_id: Optional[str], video_url: str, start: float = 0, end: Optional[float] = None,
                             target_start: float = 0, min_pause: float = 0.7, padding: float = 0.15,
                             method: str = "auto", noise_db: float = -35, volume: float = 1.0,
                             track_name: str = "video_main", width: int = 1080, height: int = 1920,
                             punch_in_zoom: float = 1.0) -> dict:
    """Place the speech parts of a source range back to back, with a single shared media material."""
    path = _local_path(video_url)
    plan = speech_ranges(path, start, end, min_pause, padding, method, noise_db)
    draft_id, script = get_or_create_draft(draft_id=draft_id, width=width, height=height)

    if track_name not in script.tracks:
        script.add_track(draft.Track_type.video, track_name=track_name)

    material = draft.Video_material(material_type="video", remote_url=path,
                                    material_name=f"video_{url_to_hash(path)}.mp4",
                                    duration=plan["end"], width=0, height=0)
    cursor = target_start
    for i, (s, e) in enumerate(plan["ranges"]):
        # Alternate a tighter framing on every other piece to hide the jump cuts
        zoom = punch_in_zoom if i % 2 == 1 else 1.0
        seg = draft.Video_segment(material,
                                  target_timerange=trange(f"{cursor}s", f"{e - s}s"),
                                  source_timerange=trange(f"{s}s", f"{e - s}s"),
                                  clip_settings=Clip_settings(scale_x=zoom, scale_y=zoom), volume=volume)
        script.add_segment(seg, track_name=track_name)
        cursor += e - s

    return {"draft_id": draft_id, "draft_url": generate_draft_url(draft_id),
            "method": plan["method"], "segments": len(plan["ranges"]), "cuts": plan["cuts"],
            "original_duration": plan["original_duration"], "new_duration": plan["kept_duration"],
            "removed_duration": plan["removed_duration"], "timeline_end": round(cursor, 2)}


# ---------------------------------------------------------------- subtitles

def _fmt_srt(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def timeline_words(draft_id: str, path: str) -> List[dict]:
    """Transcript words of `path` mapped onto the draft timeline, dropping words that were cut.
    A source range placed twice yields its words twice."""
    path = _local_path(path)
    transcript = load_cached(path)
    if transcript is None:
        raise RuntimeError("No transcript for this file yet: run capcut_transcribe first")
    placed = placements(get_draft(draft_id), path)
    if not placed:
        raise RuntimeError("This video is not on the draft's timeline: add it with capcut_add_video or "
                           "capcut_add_video_without_pauses first")
    out = []
    for seg in transcript["segments"]:
        for w in seg["words"]:
            mid = (w["start"] + w["end"]) / 2
            for p in placed:
                if p["src_start"] <= mid < p["src_end"]:
                    ws = max(w["start"], p["src_start"])
                    we = min(w["end"], p["src_end"])
                    out.append({"word": w["word"],
                                "start": p["tl_start"] + (ws - p["src_start"]) / p["speed"],
                                "end": p["tl_start"] + (we - p["src_start"]) / p["speed"]})
    out.sort(key=lambda w: w["start"])
    return out


MIN_CUE_MS = 200


def build_srt(words: List[dict], max_chars: int = 32, max_duration: float = 3.0, max_gap: float = 0.6) -> Tuple[str, int]:
    cues, cur = [], []
    for w in words:
        if cur:
            text_len = len(" ".join(x["word"] for x in cur + [w]))
            if (text_len > max_chars or w["end"] - cur[0]["start"] > max_duration
                    or w["start"] - cur[-1]["end"] > max_gap or re.search(r"[.!?]$", cur[-1]["word"])):
                cues.append(cur)
                cur = []
        cur.append(w)
    if cur:
        cues.append(cur)
    # Times in whole milliseconds so rounding cannot create overlaps. Each cue lasts at least
    # MIN_CUE_MS unless the next one starts sooner; cues never overlap; a cue left with no time
    # before the next one is shown together with it.
    timed = [[int(round(c[0]["start"] * 1000)), int(round(c[-1]["end"] * 1000)), " ".join(x["word"] for x in c)]
             for c in cues]
    out: List[List] = []
    carry = None
    for i, (s, e, text) in enumerate(timed):
        if carry:
            s, text = carry[0], carry[1] + " " + text
            carry = None
        if out:
            s = max(s, out[-1][1])
        e = max(e, s + MIN_CUE_MS)
        if i + 1 < len(timed):
            e = min(e, timed[i + 1][0])
        if e <= s:
            carry = (s, text)
            continue
        out.append([s, e, text])
    lines = [f"{i}\n{_fmt_srt(s / 1000)} --> {_fmt_srt(e / 1000)}\n{text}\n" for i, (s, e, text) in enumerate(out, 1)]
    return "\n".join(lines), len(out)
