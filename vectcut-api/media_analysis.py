# Added in capcut-mcp-kit: speech transcription, pause detection, pause-free assembly and
# automatic subtitles that follow the edit. See NOTICE at the repository root.
"""
Media analysis for content-aware editing.

- Transcription runs locally with Whisper: mlx-whisper on Apple Silicon (GPU), faster-whisper
  elsewhere. The result is saved next to the video (<name>.transcript.json with word timings, and a
  readable <name>.transcript.txt), so a video is transcribed once and its files stay together.
- Pauses come from the transcript's word timings when available (robust to background noise),
  otherwise from ffmpeg's silencedetect.
- Every video placed on the timeline is recorded in a per-draft time map, so subtitles generated
  from the source transcript land at the right timeline positions even after cuts.
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
from util import generate_draft_url, url_to_hash

# Used only when the video's own folder is not writable
FALLBACK_DIR = os.path.join(os.path.expanduser("~"), ".cache", "capcut-mcp-kit", "transcripts")

MODELS = {
    # name: (mlx-whisper repo, faster-whisper model)
    "turbo": ("mlx-community/whisper-large-v3-turbo", "large-v3-turbo"),
    "large": ("mlx-community/whisper-large-v3-mlx", "large-v3"),
    "small": ("mlx-community/whisper-small-mlx", "small"),
}

# draft_id -> list of {"src", "src_start", "src_end", "tl_start", "speed"}
DRAFT_TIMEMAPS: Dict[str, List[dict]] = {}

_jobs: Dict[str, dict] = {}
_jobs_lock = threading.Lock()


# ---------------------------------------------------------------- helpers

def _local_path(path: str) -> str:
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"File not found: {path}")
    return os.path.abspath(path)


def _transcript_paths(path: str) -> List[str]:
    """Candidate locations of a file's transcript: next to it first, then the fallback folder."""
    stem = os.path.splitext(os.path.basename(path))[0]
    tag = hashlib.sha1(path.encode()).hexdigest()[:10]
    return [os.path.join(os.path.dirname(path), f"{stem}.transcript.json"),
            os.path.join(FALLBACK_DIR, f"{stem}-{tag}.transcript.json")]


def _fingerprint(path: str) -> dict:
    st = os.stat(path)
    return {"source_size": st.st_size, "source_mtime": int(st.st_mtime)}


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


def _save_transcript(path: str, result: dict):
    result.update(_fingerprint(path))
    for target in _transcript_paths(path):
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False)
            with open(target[:-len(".json")] + ".txt", "w", encoding="utf-8") as f:
                f.write(_blocks_text(result))
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
    duration = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        check=True, capture_output=True, text=True).stdout.strip() or 0)
    return {"source": path, "model": model, "engine": engine, "language": detected,
            "duration": duration, "segments": segments}


def _job_worker(key: str, path: str, model: str, language: Optional[str]):
    try:
        _save_transcript(path, _run_transcription(path, model, language))
        with _jobs_lock:
            _jobs[key]["status"] = "done"
    except Exception as e:  # surfaced to the caller on the next status check
        with _jobs_lock:
            _jobs[key]["status"] = "failed"
            _jobs[key]["error"] = str(e)


def transcribe(path: str, model: str = "turbo", language: Optional[str] = None, wait: float = 45) -> dict:
    """Start (or reuse) a transcription and wait up to `wait` seconds.

    Returns {"status": "done", "transcript": {...}} or {"status": "running", "elapsed": s}.
    Long files keep transcribing in the background; call again to collect the result.
    """
    if model not in MODELS:
        raise ValueError(f"Unknown model {model}; use one of {', '.join(MODELS)}")
    path = _local_path(path)
    key = f"{path}|{model}"
    cached = load_cached(path, model)
    if cached is not None:
        return {"status": "done", "transcript": cached}

    with _jobs_lock:
        job = _jobs.get(key)
        if job is None or job["status"] == "failed":
            if job is not None and job["status"] == "failed":
                error = job["error"]
                del _jobs[key]
                raise RuntimeError(f"Transcription failed: {error}")
            job = {"status": "running", "started": time.time()}
            _jobs[key] = job
            threading.Thread(target=_job_worker, args=(key, path, model, language), daemon=True).start()

    deadline = time.time() + max(0.0, wait)
    while time.time() < deadline and job["status"] == "running":
        time.sleep(0.5)
    if job["status"] == "done":
        return {"status": "done", "transcript": load_cached(path, model)}
    if job["status"] == "failed":
        with _jobs_lock:
            del _jobs[key]
        raise RuntimeError(f"Transcription failed: {job.get('error')}")
    return {"status": "running", "elapsed": round(time.time() - job["started"])}


def load_cached(path: str, model: Optional[str] = None) -> Optional[dict]:
    """The file's saved transcript if it still matches the file (and the model, when given)."""
    path = _local_path(path)
    fp = _fingerprint(path)
    for target in _transcript_paths(path):
        if not os.path.exists(target):
            continue
        with open(target, encoding="utf-8") as fh:
            data = json.load(fh)
        if all(data.get(k) == v for k, v in fp.items()) and (model is None or data.get("model") == model):
            return data
    return None


# ---------------------------------------------------------------- pauses

def _audio_silences(path: str, start: float, end: Optional[float], noise_db: float, min_pause: float) -> List[Tuple[float, float]]:
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-ss", str(start)]
    if end is not None:
        cmd += ["-to", str(end)]
    cmd += ["-i", path, "-vn", "-af", f"silencedetect=noise={noise_db}dB:d={min_pause}", "-f", "null", "-"]
    out = subprocess.run(cmd, capture_output=True, text=True).stderr
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
    """Source ranges to keep (speech), dropping pauses longer than min_pause."""
    path = _local_path(path)
    transcript = load_cached(path) if method in ("auto", "transcript") else None
    if method == "transcript" and transcript is None:
        raise RuntimeError("No transcript for this file yet: run capcut_transcribe first, or use method='audio'")

    if transcript is not None:
        duration = transcript["duration"]
        end = duration if end is None else min(end, duration)
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
        if end is None:
            end = float(subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                check=True, capture_output=True, text=True).stdout.strip())
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

def record_placement(draft_id: str, src: str, src_start: float, src_end: float, tl_start: float, speed: float = 1.0):
    """Remember where a source range sits on the timeline (used to place auto subtitles)."""
    try:
        src = os.path.abspath(os.path.expanduser(src))
    except Exception:
        pass
    DRAFT_TIMEMAPS.setdefault(draft_id, []).append(
        {"src": src, "src_start": src_start, "src_end": src_end, "tl_start": tl_start, "speed": speed or 1.0})


def add_video_without_pauses(draft_id: Optional[str], video_url: str, start: float = 0, end: Optional[float] = None,
                             target_start: float = 0, min_pause: float = 0.7, padding: float = 0.15,
                             method: str = "auto", noise_db: float = -35, volume: float = 1.0,
                             track_name: str = "video_main", width: int = 1080, height: int = 1920) -> dict:
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
    for s, e in plan["ranges"]:
        seg = draft.Video_segment(material,
                                  target_timerange=trange(f"{cursor}s", f"{e - s}s"),
                                  source_timerange=trange(f"{s}s", f"{e - s}s"),
                                  clip_settings=Clip_settings(), volume=volume)
        script.add_segment(seg, track_name=track_name)
        record_placement(draft_id, path, s, e, cursor)
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
    """Transcript words of `path` mapped onto the draft timeline, dropping words that were cut."""
    path = _local_path(path)
    transcript = load_cached(path)
    if transcript is None:
        raise RuntimeError("No transcript for this file yet: run capcut_transcribe first")
    placements = [p for p in DRAFT_TIMEMAPS.get(draft_id, []) if p["src"] == path]
    if not placements:
        raise RuntimeError("This video is not on the draft's timeline (add it with capcut_add_video or "
                           "capcut_add_video_without_pauses in this backend session first)")
    out = []
    for seg in transcript["segments"]:
        for w in seg["words"]:
            mid = (w["start"] + w["end"]) / 2
            for p in placements:
                if p["src_start"] <= mid < p["src_end"]:
                    ws = max(w["start"], p["src_start"])
                    we = min(w["end"], p["src_end"])
                    out.append({"word": w["word"],
                                "start": p["tl_start"] + (ws - p["src_start"]) / p["speed"],
                                "end": p["tl_start"] + (we - p["src_start"]) / p["speed"]})
                    break
    out.sort(key=lambda w: w["start"])
    return out


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
    lines = []
    for i, c in enumerate(cues, 1):
        end = c[-1]["end"]
        if i < len(cues):
            end = min(end, cues[i][0]["start"] - 0.01)  # no overlap with the next cue
        lines.append(f"{i}\n{_fmt_srt(c[0]['start'])} --> {_fmt_srt(max(end, c[0]['start'] + 0.2))}\n"
                     + " ".join(x["word"] for x in c) + "\n")
    return "\n".join(lines), len(cues)
