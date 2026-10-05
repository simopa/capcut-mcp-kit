# Added in capcut-mcp-kit: virtual camera moves (punch-ins, pushes, pans, shakes...) built from
# keyframes and CapCut's own effects. See NOTICE at the repository root.
"""
Camera moves for talking-head and B-roll edits.

Keyframe moves animate scale / position / rotation of the clips on a video track. A move can span
several clips (e.g. after pause removal): it is sampled on every clip it overlaps, so the motion
stays continuous across cuts. Values are relative to each clip's own framing (scale multiplies the
clip's scale, offsets add to its position), so moves stack on top of a reframed clip.

Effect moves add one of CapCut's built-in effects (shake, lens zoom, flash...) on an effect track.
"""

import math
from typing import Callable, Dict, List, Tuple

import pyJianYingDraft as draft
from pyJianYingDraft import trange
from create_draft import get_or_create_draft
from util import generate_draft_url

Points = Dict[str, List[Tuple[float, float]]]  # property -> [(seconds from move start, value)]


def _ease(points: List[Tuple[float, float]], steps: int = 4) -> List[Tuple[float, float]]:
    """Insert ease-in-out samples between points (CapCut keyframes interpolate linearly)."""
    out = [points[0]]
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        for i in range(1, steps):
            f = i / steps
            f = 0.5 - 0.5 * math.cos(math.pi * f)
            out.append((t0 + (t1 - t0) * i / steps, v0 + (v1 - v0) * f))
        out.append((t1, v1))
    return out


def _hold(d: float, ramp: float, peak: float) -> List[Tuple[float, float]]:
    ramp = min(ramp, d / 3)
    return [(0, 1.0), (ramp, peak), (d - ramp, peak), (d, 1.0)]


# Each builder gets (duration, intensity) and returns relative keyframe curves:
#   scale: multiplier of the clip's scale; x / y: image offset in half-canvas units (+x right,
#   +y up; the camera moves the opposite way, so a pan left shifts the image right);
#   rotation: degrees added (clockwise).
KEYFRAME_MOVES: Dict[str, Tuple[str, Callable[[float, float], Points]]] = {
    "punch_in": ("Cut in closer for the range, then cut back (hard zoom, no animation)",
                 lambda d, k: {"scale": _hold(d, 0.03, 1 + 0.18 * k)}),
    "zoom_in_out": ("Smoothly move in closer, hold, then ease back out",
                    lambda d, k: {"scale": _ease(_hold(d, 0.6, 1 + 0.15 * k))}),
    "push_in": ("Slow continuous push in (Ken Burns) over the range",
                lambda d, k: {"scale": _ease([(0, 1.0), (d, 1 + 0.12 * k)], 6)}),
    "pull_out": ("Start close and slowly pull back to the normal framing",
                 lambda d, k: {"scale": _ease([(0, 1 + 0.12 * k), (d, 1.0)], 6)}),
    "punch": ("Impact hit: fast zoom kick that settles back (fits a beat or a strong word)",
              lambda d, k: {"scale": [(0, 1.0), (0.06, 1 + 0.25 * k), (0.2, 1 + 0.08 * k), (0.35, 1 + 0.02 * k), (0.5, 1.0)]}),
    "zoom_bounce": ("Quick zoom in with a small elastic overshoot",
                    lambda d, k: {"scale": [(0, 1.0), (0.12, 1 + 0.2 * k), (0.28, 1 + 0.12 * k), (0.4, 1 + 0.16 * k), (0.55, 1 + 0.15 * k)]
                                  + ([(d, 1 + 0.15 * k)] if d > 0.55 else [])}),
    "pan_left": ("Camera pans left across a slightly enlarged frame",
                 lambda d, k: {"scale": [(0, 1 + 0.2 * k), (d, 1 + 0.2 * k)], "x": _ease([(0, -0.12 * k), (d, 0.12 * k)], 6)}),
    "pan_right": ("Camera pans right across a slightly enlarged frame",
                  lambda d, k: {"scale": [(0, 1 + 0.2 * k), (d, 1 + 0.2 * k)], "x": _ease([(0, 0.12 * k), (d, -0.12 * k)], 6)}),
    "tilt_up": ("Camera tilts up across a slightly enlarged frame",
                lambda d, k: {"scale": [(0, 1 + 0.2 * k), (d, 1 + 0.2 * k)], "y": _ease([(0, 0.12 * k), (d, -0.12 * k)], 6)}),
    "tilt_down": ("Camera tilts down across a slightly enlarged frame",
                  lambda d, k: {"scale": [(0, 1 + 0.2 * k), (d, 1 + 0.2 * k)], "y": _ease([(0, -0.12 * k), (d, 0.12 * k)], 6)}),
    "rotate_in": ("Enter slightly rotated and zoomed, straighten out",
                  lambda d, k: {"rotation": _ease([(0, -6 * k), (min(0.6, d), 0.0)]),
                                "scale": _ease([(0, 1 + 0.15 * k), (min(0.6, d), 1.0)])}),
    "dutch_tilt": ("Hold a tilted (dutch angle) framing for the range",
                   lambda d, k: {"rotation": [(t, (v - 1) / 0.12 * 5) for t, v in _hold(d, 0.25, 1 + 0.12 * k)],
                                 "scale": _hold(d, 0.25, 1 + 0.12 * k)}),
    "handheld": ("Subtle floating handheld drift",
                 lambda d, k: {"scale": [(0, 1 + 0.06 * k), (d, 1 + 0.06 * k)],
                               "x": [(t, 0.012 * k * math.sin(t * 1.3)) for t in _steps(d, 0.5)],
                               "y": [(t, 0.01 * k * math.sin(t * 0.9 + 1)) for t in _steps(d, 0.5)]}),
    "whip_left": ("Fast whip pan out to the left at the end of the range (pair with a cut)",
                  lambda d, k: {"x": [(0, 0.0), (max(0, d - 0.18), 0.0), (d, 0.8 * k)]}),
    "whip_right": ("Fast whip pan out to the right at the end of the range (pair with a cut)",
                   lambda d, k: {"x": [(0, 0.0), (max(0, d - 0.18), 0.0), (d, -0.8 * k)]}),
}


def _steps(d: float, step: float) -> List[float]:
    n = max(1, int(d / step))
    return [d * i / n for i in range(n + 1)]


# Moves made with CapCut's own effects (scene effects, on an effect track)
EFFECT_MOVES: Dict[str, Tuple[str, str]] = {
    "shake": ("Camera_Shake", "Camera shake / quake"),
    "shake_strong": ("Shake_3", "Stronger, rougher shake"),
    "lens_zoom": ("Zoom_Lens", "Lens zoom pulse"),
    "mini_zoom": ("Mini_Zoom", "Small rhythmic zoom"),
    "chroma_zoom": ("Chromozoom", "Zoom with chromatic aberration"),
    "fisheye": ("Fisheye", "Fisheye lens distortion"),
    "focus_pull": ("Camera_Focus", "Rack focus (blur to sharp)"),
    "motion_blur": ("Motion_Blur", "Motion blur (use with whips)"),
    "swing": ("Rebound_Swing", "Swinging camera"),
    "flash": ("White_Flash", "White flash (punch / transition accent)"),
    "flash_black": ("Black_Flash", "Black flash"),
    "glitch": ("Glitch", "Digital glitch"),
}


def list_moves() -> List[dict]:
    return ([{"name": n, "kind": "keyframes", "description": desc} for n, (desc, _) in KEYFRAME_MOVES.items()] +
            [{"name": n, "kind": "effect", "description": f"{desc} (CapCut effect {eff})"} for n, (eff, desc) in EFFECT_MOVES.items()])


NEUTRAL = {"scale": 1.0, "x": 0.0, "y": 0.0, "rotation": 0.0}
EDGE = 0.001  # seconds


def _anchored(points: List[Tuple[float, float]], d: float, neutral: float) -> List[Tuple[float, float]]:
    """Make a curve start and end at the clip's own framing, so the move stays inside its range
    (CapCut interpolates between consecutive keyframes, i.e. into the next move otherwise).
    A move that begins/ends away from neutral (pans, push_in...) cuts in/out at its edges."""
    pts = [(min(max(t, 0.0), d), v) for t, v in points]
    if abs(pts[0][1] - neutral) > 1e-9:
        pts = [(0.0, neutral), (EDGE, pts[0][1])] + [p for p in pts[1:] if p[0] > EDGE]
    if abs(pts[-1][1] - neutral) > 1e-9:
        pts = [p for p in pts[:-1] if p[0] < d - EDGE] + [(d - EDGE, pts[-1][1]), (d, neutral)]
    return pts


def _interp(points: List[Tuple[float, float]], t: float) -> float:
    if t <= points[0][0]:
        return points[0][1]
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if t <= t1:
            return v0 if t1 == t0 else v0 + (v1 - v0) * (t - t0) / (t1 - t0)
    return points[-1][1]


def add_camera_move(draft_id: str, move: str, start: float, end: float, intensity: float = 1.0,
                    track_name: str = "video_main", flash: bool = False) -> dict:
    if end <= start:
        raise ValueError("end must be after start")
    draft_id, script = get_or_create_draft(draft_id=draft_id)

    if move in EFFECT_MOVES:
        from add_effect_impl import add_effect_impl
        add_effect_impl(effect_type=EFFECT_MOVES[move][0], effect_category="scene", start=start, end=end,
                        draft_id=draft_id, track_name="camera_fx")
        return {"draft_id": draft_id, "draft_url": generate_draft_url(draft_id), "move": move,
                "kind": "effect", "clips": 0}

    if move not in KEYFRAME_MOVES:
        raise ValueError(f"Unknown move {move}; use capcut_list_camera_moves")
    if track_name not in script.tracks:
        raise ValueError(f"Track {track_name} not found (video tracks: "
                         f"{[n for n, t in script.tracks.items() if t.track_type == draft.Track_type.video]})")

    curves = {prop: _anchored(pts, end - start, NEUTRAL[prop])
              for prop, pts in KEYFRAME_MOVES[move][1](end - start, intensity).items()}
    s_us, e_us = int(start * 1e6), int(end * 1e6)
    touched = 0
    for seg in script.tracks[track_name].segments:
        a = max(seg.target_timerange.start, s_us)
        b = min(seg.target_timerange.end, e_us)
        if b <= a:
            continue
        touched += 1
        cs = seg.clip_settings
        for prop, pts in curves.items():
            # keyframe times: the overlap's edges plus the curve's own points inside it
            times = sorted({a, b} | {s_us + int(t * 1e6) for t, _ in pts if a < s_us + int(t * 1e6) < b})
            for t in times:
                v = _interp(pts, (t - s_us) / 1e6)
                offset = t - seg.target_timerange.start
                if prop == "scale":
                    seg.add_keyframe(draft.Keyframe_property.uniform_scale, offset, cs.scale_x * v)
                elif prop == "x":
                    seg.add_keyframe(draft.Keyframe_property.position_x, offset, cs.transform_x + v)
                elif prop == "y":
                    seg.add_keyframe(draft.Keyframe_property.position_y, offset, cs.transform_y + v)
                elif prop == "rotation":
                    seg.add_keyframe(draft.Keyframe_property.rotation, offset, cs.rotation + v)
    if touched == 0:
        raise ValueError(f"No clip on track {track_name} between {start}s and {end}s")

    if flash:
        from add_effect_impl import add_effect_impl
        add_effect_impl(effect_type="White_Flash", effect_category="scene", start=start,
                        end=min(end, start + 0.25), draft_id=draft_id, track_name="camera_fx")

    return {"draft_id": draft_id, "draft_url": generate_draft_url(draft_id), "move": move,
            "kind": "keyframes", "clips": touched}
