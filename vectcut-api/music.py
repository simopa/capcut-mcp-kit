# Added in capcut-mcp-kit (2026): background music that fills the edit, with fades and ducking under
# the voice, and a timeline summary. See NOTICE at the repository root.
"""
Background music.

The music track is laid from `start` to `end` (default: the end of the timeline), looping the file
back to back if it is shorter. The first piece fades in, the last fades out. With `duck_under` (a
video or audio on the timeline that has a transcript), the music dips to `volume * duck_level`
while someone speaks, with short ramps, using volume keyframes that stay editable in CapCut.
"""

import os
from typing import List, Optional, Tuple

import pyJianYingDraft as draft
from pyJianYingDraft import trange
from create_draft import get_or_create_draft
from util import generate_draft_url, url_to_hash

RAMP_DOWN = 0.25  # seconds before speech the music starts dipping
RAMP_UP = 0.5     # seconds after speech it is back up
MERGE_GAP = RAMP_DOWN + RAMP_UP + 0.3  # shorter silences stay ducked


def speech_spans(draft_id: str, voice_path: str) -> List[Tuple[float, float]]:
    """Timeline ranges where the transcript of voice_path has words, short gaps merged."""
    import media_analysis
    spans: List[List[float]] = []
    for w in media_analysis.timeline_words(draft_id, voice_path):
        if spans and w["start"] - spans[-1][1] < MERGE_GAP:
            spans[-1][1] = max(spans[-1][1], w["end"])
        else:
            spans.append([w["start"], w["end"]])
    return [(s, e) for s, e in spans]


def duck_curve(spans, volume: float, level: float, start: float, end: float) -> List[Tuple[float, float]]:
    """Volume points (time, value) over [start, end]: the whole ducking curve sampled on the range,
    so music that starts or ends during speech stays ducked there."""
    low = volume * level
    merged: List[List[float]] = []
    for s, e in sorted(spans):  # dips whose ramps would touch become one dip
        if merged and s - RAMP_DOWN <= merged[-1][1] + RAMP_UP:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    full = []
    for s, e in merged:
        full += [(s - RAMP_DOWN, volume), (s, low), (max(s, e), low), (max(s, e) + RAMP_UP, volume)]
    out = []
    for t in sorted({start, end} | {t for t, _ in full if start < t < end}):
        v = _value(full, t) if full else volume
        if out and abs(out[-1][0] - t) < 1e-6:  # one value per time, the last wins
            out[-1] = (t, v)
        else:
            out.append((t, v))
    return out


def _value(points, t: float) -> float:
    if t <= points[0][0]:
        return points[0][1]
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if t <= t1:
            return v0 if t1 == t0 else v0 + (v1 - v0) * (t - t0) / (t1 - t0)
    return points[-1][1]


def add_background_music(draft_id: str, audio_url: str, volume: float = 0.25, fade_in: float = 1.0,
                         fade_out: float = 2.0, start: float = 0.0, end: Optional[float] = None,
                         duck_under: Optional[str] = None, duck_level: float = 0.35,
                         track_name: str = "music") -> dict:
    import media_analysis
    path = media_analysis._local_path(audio_url)
    if not 0 < volume <= 2:
        raise ValueError("volume must be between 0 and 2 (1 = original)")
    if not 0 <= duck_level <= 1:
        raise ValueError("duck_level must be between 0 and 1")
    draft_id, script = get_or_create_draft(draft_id=draft_id)
    if end is None:
        end = script.duration / 1e6
        base = getattr(script, "base_project", None)
        if base:  # an opened project: fill it to its own end too
            import json
            end = max(end, (json.loads(base["raw"]).get("duration") or 0) / 1e6)
        if end <= start:
            raise ValueError("The timeline is empty: add the video first, or give an end time")
    if end - start < 0.5:
        raise ValueError("The music range must be at least 0.5 s")
    length = media_analysis._probe_duration(path)
    spans = speech_spans(draft_id, os.path.expanduser(duck_under)) if duck_under else []
    curve = duck_curve(spans, volume, duck_level, start, end) if duck_under else None

    if track_name not in script.tracks:
        script.add_track(draft.Track_type.audio, track_name=track_name)
    material = draft.Audio_material(remote_url=path, material_name=f"audio_{url_to_hash(path)}{os.path.splitext(path)[1]}",
                                    duration=length)
    pieces, cursor = [], start
    while cursor < end - 1e-3:
        dur = min(length, end - cursor)
        pieces.append((cursor, dur))
        cursor += dur
    for i, (at, dur) in enumerate(pieces):
        seg = draft.Audio_segment(material, target_timerange=trange(f"{at}s", f"{dur}s"),
                                  source_timerange=trange("0s", f"{dur}s"), volume=volume)
        fi = fade_in if i == 0 else 0
        fo = fade_out if i == len(pieces) - 1 else 0
        if fi or fo:
            seg.add_fade(f"{min(fi, dur / 2)}s", f"{min(fo, dur / 2)}s")
        if curve:
            times = sorted({at, at + dur} | {t for t, _ in curve if at < t < at + dur})
            for t in times:
                seg.add_keyframe(int(round((t - at) * 1e6)), _value(curve, t))
        script.add_segment(seg, track_name=track_name)

    return {"draft_id": draft_id, "draft_url": generate_draft_url(draft_id), "track": track_name,
            "pieces": len(pieces), "start": start, "end": round(end, 3),
            "ducked_spans": len(spans)}


def timeline(draft_id: str) -> dict:
    """The draft's tracks with where each one ends, to place new items after or over them."""
    import draft_store
    rev, script = draft_store.current(draft_id)  # read together: the revision is the one shown
    tracks = []
    for name, t in script.tracks.items():
        if not t.segments:
            continue
        tracks.append({"name": name, "type": t.track_type.name, "clips": len(t.segments),
                       "end": round(max(s.target_timerange.end for s in t.segments) / 1e6, 3)})
    out = {"draft_id": draft_id, "revision": rev,
           "duration": round(script.duration / 1e6, 3), "tracks": tracks}
    base = getattr(script, "base_project", None)
    if base:
        import json
        import existing_project
        content = json.loads(base["raw"])
        out["existing_project"] = {"name": base["name"], "duration": round((content.get("duration") or 0) / 1e6, 3),
                                   "tracks": existing_project._summary(content)}
    return out
