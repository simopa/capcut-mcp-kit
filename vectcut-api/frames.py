# Added in capcut-mcp-kit: frames of a file or of a draft's timeline, so the assistant can see the
# framing before placing camera moves. See NOTICE at the repository root.
"""
Still frames for looking at the footage.

A frame is taken with ffmpeg from the source file, at a time of the file or of the timeline (the
clip on a video track at that moment, its source time worked out from the clip's range and speed).
It shows the footage as filmed: CapCut's keyframes, effects, filters and titles are not rendered.
Frames come back as small JPEGs, base64 encoded, each with its time.
"""

import base64
import os
import re
import subprocess
from typing import List, Optional

MAX_FRAMES = 24
WIDTH_RANGE = (160, 960)
PHOTO_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".bmp", ".gif", ".tif", ".tiff")
# A time at the very end of a file or clip is read this much earlier: ffmpeg returns the first frame
# at or after the time asked, which at a cut would be footage the edit left out
END_MARGIN = 0.05
# Cloud projects keep their media inside the project folder under this prefix
PLACEHOLDER = re.compile(r"^##_draftpath_placeholder_[^#]*_##")


def frame_times(times: Optional[List[float]] = None, every: Optional[float] = None, start: float = 0,
                end: Optional[float] = None) -> List[float]:
    """The times asked for: a list, or one every `every` seconds from start up to end."""
    if (times is None) == (every is None):
        raise ValueError("Give either times or every")
    if times is not None:
        out = [round(float(t), 3) for t in times]
        if not out:
            raise ValueError("times is empty")
        if any(t < 0 for t in out):
            raise ValueError("Times must be 0 or more")
    else:
        every = float(every)
        if every <= 0:
            raise ValueError("every must be more than 0")
        if end is None or end <= start:
            raise ValueError("Need start < end")
        count = int((end - start) / every + 1e-9) + 1
        if count > MAX_FRAMES:
            raise ValueError(f"{count} frames between {start}s and {end}s every {every}s: at most {MAX_FRAMES} per "
                             f"call, so use every={round((end - start) / (MAX_FRAMES - 1) + 0.005, 2)} or a shorter range")
        out = [round(start + i * every, 3) for i in range(count)]
    if len(out) > MAX_FRAMES:
        raise ValueError(f"{len(out)} times: at most {MAX_FRAMES} per call")
    return out


def grab(path: str, at: float, width: int, photo: bool = False) -> Optional[bytes]:
    """One JPEG frame of `path` at `at` seconds, scaled to `width` (None if there is no frame there)."""
    cmd = ["ffmpeg", "-loglevel", "error", "-nostdin"]
    if not photo:
        cmd += ["-ss", f"{at:.3f}"]
    cmd += ["-i", path, "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "5",
            "-f", "image2", "-c:v", "mjpeg", "pipe:1"]
    proc = subprocess.run(cmd, capture_output=True, timeout=60)
    if proc.returncode != 0 or not proc.stdout:
        return None
    return proc.stdout


def _is_photo(path: str) -> bool:
    return path.lower().endswith(PHOTO_EXTENSIONS)


def _resolve(path: str, project_dir: Optional[str]) -> str:
    path = path.replace("\\", "/")
    if project_dir and PLACEHOLDER.match(path):
        path = PLACEHOLDER.sub(project_dir.replace("\\", "/"), path, count=1)
    return os.path.expanduser(path)


# ---------------------------------------------------------------- what plays on a track

def _draft_clips(script, track_name: str) -> List[dict]:
    """The clips of a draft's own video track: file, timeline range, source start, speed."""
    import pyJianYingDraft as draft
    track = script.tracks.get(track_name)
    if track is None or track.track_type != draft.Track_type.video:
        raise ValueError(f"Video track {track_name} not found (video tracks: "
                         f"{[n for n, t in script.tracks.items() if t.track_type == draft.Track_type.video]})")
    materials = {m.material_id: m for m in script.materials.videos}
    out = []
    for seg in track.segments:
        material = materials.get(getattr(seg, "material_id", None))
        if material is None:
            continue
        path = str(getattr(material, "remote_url", None) or getattr(material, "path", None) or "")
        speed = getattr(getattr(seg, "speed", None), "speed", 1.0) or 1.0
        out.append({"path": os.path.expanduser(path), "photo": getattr(material, "material_type", "") == "photo",
                    "start": seg.target_timerange.start / 1e6,
                    "end": (seg.target_timerange.start + seg.target_timerange.duration) / 1e6,
                    "source_start": seg.source_timerange.start / 1e6 if seg.source_timerange else 0.0,
                    "speed": speed})
    return out


def _project_clips(base: dict, project_track: Optional[int]) -> List[dict]:
    """The clips of a video track of a project opened with capcut_open_project (default its main
    track, the first video track with clips, as for camera moves), with the draft's edits."""
    import clip_edits
    content = clip_edits.current_content(base)
    mats = clip_edits._materials(content)
    tracks = content.get("tracks", [])
    if project_track is None:
        ti = next((i for i, t in enumerate(tracks) if t.get("type") == "video" and t.get("segments")), None)
    else:
        ti = int(project_track)
    if ti is None or not 0 <= ti < len(tracks) or tracks[ti].get("type") != "video":
        raise ValueError("project_track must be the index of a video track of the project (the track field of "
                         "capcut_list_clips)")
    out = []
    for seg in tracks[ti].get("segments") or []:
        key, material = mats.get(seg.get("material_id"), (None, {}))
        if key != "videos":
            continue
        speed = 1.0
        for ref in seg.get("extra_material_refs") or []:
            k, m = mats.get(ref, (None, {}))
            if k == "speeds" and isinstance(m.get("speed"), (int, float)) and m["speed"] > 0:
                speed = float(m["speed"])
        t, s = seg.get("target_timerange") or {}, seg.get("source_timerange") or {}
        out.append({"path": _resolve(material.get("path") or "", base.get("dir")),
                    "photo": material.get("type") == "photo",
                    "start": t.get("start", 0) / 1e6, "end": (t.get("start", 0) + t.get("duration", 0)) / 1e6,
                    "source_start": s.get("start", 0) / 1e6, "speed": speed})
    return out


def timeline_clips(draft_id: str, track_name: Optional[str] = None, project_track: Optional[int] = None):
    """(clips of the chosen video track, a label for it)."""
    from draft_store import get_draft
    script = get_draft(draft_id)
    base = getattr(script, "base_project", None)
    name = track_name or "video_main"
    if base and (project_track is not None or name not in script.tracks):
        clips = _project_clips(base, project_track)
        return clips, f"project track {project_track if project_track is not None else 'main'}"
    return _draft_clips(script, name), f'track "{name}"'


# ---------------------------------------------------------------- frames

def view_frames(path: Optional[str] = None, draft_id: Optional[str] = None, times: Optional[List[float]] = None,
                every: Optional[float] = None, start: float = 0, end: Optional[float] = None, width: int = 360,
                track_name: Optional[str] = None, project_track: Optional[int] = None) -> dict:
    """Frames of a file (times of the file) or of a draft's video track (times of the timeline)."""
    if (path is None) == (draft_id is None):
        raise ValueError("Give either path (times of the file) or draft_id (times of the timeline)")
    width = int(width)
    if not WIDTH_RANGE[0] <= width <= WIDTH_RANGE[1]:
        raise ValueError(f"width must be between {WIDTH_RANGE[0]} and {WIDTH_RANGE[1]}")
    start = float(start)
    end = float(end) if end is not None else None

    if path is not None:
        from media_analysis import _local_path, _probe_duration
        path = _local_path(path)
        photo = _is_photo(path)
        duration = 0.0 if photo else _probe_duration(path)
        if end is None and every is not None:
            end = duration
        frames = []
        for t in frame_times(times, every, start, end):
            if not photo and t > duration:
                frames.append({"time": t, "note": f"past the end of the file ({round(duration, 3)}s)"})
                continue
            src = t if photo else round(max(0.0, min(t, duration - END_MARGIN)), 3)
            image = grab(path, src, width, photo)
            frames.append({"time": t, "source": os.path.basename(path), "source_time": src,
                           **({"image": base64.b64encode(image).decode()} if image
                              else {"note": "ffmpeg found no frame there"})})
        return {"of": os.path.basename(path), "timeline": False, "frames": frames}

    clips, label = timeline_clips(draft_id, track_name, project_track)
    if end is None and every is not None:
        end = max((c["end"] for c in clips), default=0.0)
    frames = []
    for t in frame_times(times, every, start, end):
        # A clip owns [start, end); at the very end of the track the last clip still shows
        clip = next((c for c in clips if c["start"] <= t < c["end"]), None) or \
            next((c for c in reversed(clips) if abs(t - c["end"]) < 1e-6), None)
        frame = {"time": t}
        if clip is None:
            frame["note"] = "no clip at this time on the track"
        elif not os.path.isfile(clip["path"]):
            frame.update(source=os.path.basename(clip["path"]), note="the clip's file is not on this Mac")
        else:
            last = clip["source_start"] + max(0.0, (clip["end"] - clip["start"]) * clip["speed"] - END_MARGIN)
            src = clip["source_start"] + (t - clip["start"]) * clip["speed"]
            src = round(max(clip["source_start"], min(src, last)), 3)
            image = grab(clip["path"], src, width, clip["photo"] or _is_photo(clip["path"]))
            frame.update(source=os.path.basename(clip["path"]), source_time=src,
                         **({"image": base64.b64encode(image).decode()} if image
                            else {"note": "ffmpeg found no frame there"}))
        frames.append(frame)
    return {"of": label, "timeline": True, "frames": frames}
