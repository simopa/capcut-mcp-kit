# Added in capcut-mcp-kit: virtual camera moves (punch-ins, pushes, pans, shakes...) built from
# keyframes and CapCut's own effects. See NOTICE at the repository root.
"""
Camera moves for talking-head and B-roll edits.

Keyframe moves animate scale / position / rotation of the clips on a video track. A move can span
several clips (e.g. after pause removal): it is sampled on every clip it overlaps, so the motion
stays continuous across cuts. Values are relative to the clip's framing at each moment: scale
multiplies it, offsets add to it. With mode="compose" (default) that framing includes keyframes
already on the clip (an earlier move, a manual keyframe), so moves stack; mode="replace" starts from
the clip's static framing and replaces keyframes inside the range, keeping the earlier animation's
values at the range's edges so it plays unchanged outside; mode="refuse" errors if the range
already has keyframes. Keyframes outside the range are never touched.

Very short ranges compress a move's shape to fit, and every move ends on the starting framing.

Effect moves add one of CapCut's built-in effects (shake, lens zoom, flash...) on an effect track;
intensity scales the effect's strength setting.
"""

import json
import math
import os
from typing import Callable, Dict, List, Tuple

import pyJianYingDraft as draft
from pyJianYingDraft import trange, CapCut_Video_scene_effect_type
from pyJianYingDraft.keyframe import Keyframe, Keyframe_list, Keyframe_property
from create_draft import get_or_create_draft
from util import generate_draft_url

Points = Dict[str, List[Tuple[float, float]]]  # property -> [(seconds from move start, value)]


EASINGS: Dict[str, Callable[[float], float]] = {
    "smooth": lambda f: 0.5 - 0.5 * math.cos(math.pi * f),  # ease in and out
    "linear": lambda f: f,
    "snappy": lambda f: 1 - (1 - f) ** 3,  # fast start, soft landing
    "dramatic": lambda f: 4 * f ** 3 if f < 0.5 else 1 - (-2 * f + 2) ** 3 / 2,  # slow, fast, slow
}

MIN_DURATION = 0.1  # seconds


def _easer(easing: str) -> Callable:
    curve = EASINGS[easing]

    def ease(points: List[Tuple[float, float]], steps: int = 4) -> List[Tuple[float, float]]:
        """Insert eased samples between points (CapCut keyframes interpolate linearly)."""
        out = [points[0]]
        for (t0, v0), (t1, v1) in zip(points, points[1:]):
            for i in range(1, steps):
                out.append((t0 + (t1 - t0) * i / steps, v0 + (v1 - v0) * curve(i / steps)))
            out.append((t1, v1))
        return out
    return ease


def _hold(d: float, ramp: float, peak: float) -> List[Tuple[float, float]]:
    ramp = min(ramp, d / 3)
    return [(0, 1.0), (ramp, peak), (d - ramp, peak), (d, 1.0)]


# Each builder gets (duration, intensity, ease) and returns relative keyframe curves:
#   scale: multiplier of the clip's scale; x / y: image offset in half-canvas units (+x right,
#   +y up; the camera moves the opposite way, so a pan left shifts the image right);
#   rotation: degrees added (clockwise).
BUILDERS: Dict[str, Callable[[float, float, Callable], Points]] = {
    "punch_in": lambda d, k, e: {"scale": _hold(d, 0.03, 1 + 0.18 * k)},
    "zoom_in_out": lambda d, k, e: {"scale": e(_hold(d, 0.6, 1 + 0.15 * k))},
    "push_in": lambda d, k, e: {"scale": e([(0, 1.0), (d, 1 + 0.12 * k)], 6)},
    "pull_out": lambda d, k, e: {"scale": e([(0, 1 + 0.12 * k), (d, 1.0)], 6)},
    "punch": lambda d, k, e: {"scale": [(0, 1.0), (0.06, 1 + 0.25 * k), (0.2, 1 + 0.08 * k), (0.35, 1 + 0.02 * k), (0.5, 1.0)]},
    "zoom_bounce": lambda d, k, e: {"scale": [(0, 1.0), (0.12, 1 + 0.2 * k), (0.28, 1 + 0.12 * k), (0.4, 1 + 0.16 * k), (0.55, 1 + 0.15 * k)]
                                  + ([(d, 1 + 0.15 * k)] if d > 0.55 else [])},
    "pan_left": lambda d, k, e: {"scale": [(0, 1 + 0.2 * k), (d, 1 + 0.2 * k)], "x": e([(0, -0.12 * k), (d, 0.12 * k)], 6)},
    "pan_right": lambda d, k, e: {"scale": [(0, 1 + 0.2 * k), (d, 1 + 0.2 * k)], "x": e([(0, 0.12 * k), (d, -0.12 * k)], 6)},
    "tilt_up": lambda d, k, e: {"scale": [(0, 1 + 0.2 * k), (d, 1 + 0.2 * k)], "y": e([(0, 0.12 * k), (d, -0.12 * k)], 6)},
    "tilt_down": lambda d, k, e: {"scale": [(0, 1 + 0.2 * k), (d, 1 + 0.2 * k)], "y": e([(0, -0.12 * k), (d, 0.12 * k)], 6)},
    "rotate_in": lambda d, k, e: {"rotation": e([(0, -6 * k), (min(0.6, d), 0.0)]),
                                "scale": e([(0, 1 + 0.15 * k), (min(0.6, d), 1.0)])},
    "dutch_tilt": lambda d, k, e: {"rotation": [(t, (v - 1) / 0.12 * 5) for t, v in _hold(d, 0.25, 1 + 0.12 * k)],
                                 "scale": _hold(d, 0.25, 1 + 0.12 * k)},
    "handheld": lambda d, k, e: {"scale": [(0, 1 + 0.06 * k), (d, 1 + 0.06 * k)],
                               "x": [(t, 0.012 * k * math.sin(t * 1.3)) for t in _steps(d, 0.5)],
                               "y": [(t, 0.01 * k * math.sin(t * 0.9 + 1)) for t in _steps(d, 0.5)]},
    "whip_left": lambda d, k, e: {"x": [(0, 0.0), (max(0, d - 0.18), 0.0), (d, 0.8 * k)]},
    "whip_right": lambda d, k, e: {"x": [(0, 0.0), (max(0, d - 0.18), 0.0), (d, -0.8 * k)]},
}


def _steps(d: float, step: float) -> List[float]:
    n = max(1, int(d / step))
    return [d * i / n for i in range(n + 1)]


# Names and descriptions live in camera_moves.json, shared with the MCP server
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "camera_moves.json"), encoding="utf-8") as _f:
    _CATALOG = json.load(_f)
KEYFRAME_MOVES: Dict[str, Tuple[str, Callable[[float, float, Callable], Points]]] = {
    m["name"]: (m["description"], BUILDERS[m["name"]]) for m in _CATALOG["keyframes"]}
if set(KEYFRAME_MOVES) != set(BUILDERS):
    raise RuntimeError(f"camera_moves.json and BUILDERS disagree: {sorted(set(BUILDERS) ^ set(KEYFRAME_MOVES))}")
# Moves made with CapCut's own effects (scene effects, on an effect track): name -> (effect, description)
EFFECT_MOVES: Dict[str, Tuple[str, str]] = {m["name"]: (m["capcut_effect"], m["description"]) for m in _CATALOG["effects"]}


def list_moves() -> List[dict]:
    return ([{"name": n, "kind": "keyframes", "description": desc} for n, (desc, _) in KEYFRAME_MOVES.items()] +
            [{"name": n, "kind": "effect", "description": f"{desc} (CapCut effect {eff})"} for n, (eff, desc) in EFFECT_MOVES.items()])


NEUTRAL = {"scale": 1.0, "x": 0.0, "y": 0.0, "rotation": 0.0}
EDGE = 0.001  # seconds


def _fit(points: List[Tuple[float, float]], d: float) -> List[Tuple[float, float]]:
    """Squeeze a fixed-length shape (punch, bounce, rotate-in...) into a shorter range, and keep
    one value per time (the last), so no two keyframes share a timestamp."""
    longest = max(t for t, _ in points)
    factor = d / longest if longest > d else 1.0
    out: List[Tuple[float, float]] = []
    for t, v in sorted(((max(0.0, t) * factor, v) for t, v in points), key=lambda p: p[0]):
        if out and abs(out[-1][0] - t) < 1e-9:
            out[-1] = (t, v)
        else:
            out.append((t, v))
    return out


def _anchored(points: List[Tuple[float, float]], d: float, neutral: float) -> List[Tuple[float, float]]:
    """Make a curve start and end at the clip's own framing, so the move stays inside its range
    (CapCut interpolates between consecutive keyframes, i.e. into the next move otherwise).
    A move that begins/ends away from neutral (pans, push_in...) cuts in/out at its edges."""
    pts = _fit(points, d)
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


def _find(seg, prop: Keyframe_property):
    return next((kl for kl in seg.common_keyframes if kl.keyframe_property == prop), None)


def _value_at(kf_list, offset: int, default: float) -> float:
    """The value a keyframe list gives at offset (CapCut interpolates linearly), or default."""
    if kf_list is None or not kf_list.keyframes:
        return default
    kfs = kf_list.keyframes
    if offset <= kfs[0].time_offset:
        return kfs[0].values[0]
    for k0, k1 in zip(kfs, kfs[1:]):
        if offset <= k1.time_offset:
            if k1.time_offset == k0.time_offset:
                return k1.values[0]
            return k0.values[0] + (k1.values[0] - k0.values[0]) * (offset - k0.time_offset) / (k1.time_offset - k0.time_offset)
    return kfs[-1].values[0]


def _write(seg, prop: Keyframe_property, samples: List[Tuple[int, float]], a: int, b: int):
    """Replace the keyframes of prop between offsets a and b (inclusive) with samples."""
    kf_list = _find(seg, prop)
    if kf_list is None:
        kf_list = Keyframe_list(prop)
        seg.common_keyframes.append(kf_list)
    kf_list.keyframes = [k for k in kf_list.keyframes if not (a <= k.time_offset <= b)]
    kf_list.keyframes.extend(Keyframe(off, v) for off, v in samples)
    kf_list.keyframes.sort(key=lambda k: k.time_offset)


def _targets(seg, prop: str):
    """(keyframe property, value without keyframes, combine) for each list a move property drives.
    Uniform scale is stored in the scale_x list; a clip with separate x/y scale gets both."""
    cs = seg.clip_settings
    mul, add = (lambda base, v: base * v), (lambda base, v: base + v)
    if prop == "scale":
        if seg.uniform_scale:
            return [(Keyframe_property.scale_x, cs.scale_x, mul)]
        return [(Keyframe_property.scale_x, cs.scale_x, mul), (Keyframe_property.scale_y, cs.scale_y, mul)]
    if prop == "x":
        return [(Keyframe_property.position_x, cs.transform_x, add)]
    if prop == "y":
        return [(Keyframe_property.position_y, cs.transform_y, add)]
    return [(Keyframe_property.rotation, cs.rotation, add)]


def _effect_params(move: str, intensity: float):
    """CapCut effect parameters (0-100 each, None = default) with the strength setting scaled."""
    meta = CapCut_Video_scene_effect_type[EFFECT_MOVES[move][0]].value
    idx = next((i for i, p in enumerate(meta.params) if not p.name.endswith("_speed")), None)
    if idx is None:
        if abs(intensity - 1.0) > 1e-9:
            raise ValueError(f"{move} has no strength setting: leave intensity at 1")
        return None
    p = meta.params[idx]
    real = min(p.max_value, max(p.min_value, p.default_value * intensity))
    params = [None] * len(meta.params)
    params[idx] = (real - p.min_value) / (p.max_value - p.min_value) * 100
    return params


def add_camera_move(draft_id: str, move: str, start: float, end: float, intensity: float = 1.0,
                    track_name: str = "video_main", flash: bool = False, mode: str = "compose",
                    easing: str = "smooth") -> dict:
    if start < 0 or end <= start:
        raise ValueError("Need 0 <= start < end")
    if end - start < MIN_DURATION:
        raise ValueError(f"A camera move needs at least {MIN_DURATION}s")
    if mode not in ("compose", "replace", "refuse"):
        raise ValueError("mode must be compose, replace or refuse")
    if easing not in EASINGS:
        raise ValueError(f"easing must be one of {', '.join(EASINGS)}")
    if move not in KEYFRAME_MOVES and move not in EFFECT_MOVES:
        raise ValueError(f"Unknown move {move}: see the list in capcut_add_camera_move")
    draft_id, script = get_or_create_draft(draft_id=draft_id)
    from add_effect_impl import add_effect_impl

    if move in EFFECT_MOVES:
        add_effect_impl(effect_type=EFFECT_MOVES[move][0], effect_category="scene", start=start, end=end,
                        draft_id=draft_id, track_name="camera_fx", params=_effect_params(move, intensity))
        if flash:
            add_effect_impl(effect_type="White_Flash", effect_category="scene", start=start,
                            end=min(end, start + 0.25), draft_id=draft_id, track_name="camera_fx_flash")
        return {"draft_id": draft_id, "draft_url": generate_draft_url(draft_id), "move": move,
                "kind": "effect", "clips": 0}

    track = script.tracks.get(track_name)
    if track is None or track.track_type != draft.Track_type.video:
        raise ValueError(f"Video track {track_name} not found (video tracks: "
                         f"{[n for n, t in script.tracks.items() if t.track_type == draft.Track_type.video]})")
    # Keyframes queued with capcut_add_keyframe become real first, so the move composes with them
    track.process_pending_keyframes()

    d = end - start
    curves = {prop: _anchored(pts, d, NEUTRAL[prop])
              for prop, pts in KEYFRAME_MOVES[move][1](d, intensity, _easer(easing)).items()}
    s_us, e_us = int(start * 1e6), int(end * 1e6)
    overlaps = []
    for seg in track.segments:
        a = max(seg.target_timerange.start, s_us)
        b = min(seg.target_timerange.end, e_us)
        if b > a:
            overlaps.append((seg, a - seg.target_timerange.start, b - seg.target_timerange.start))
    if not overlaps:
        raise ValueError(f"No clip on track {track_name} between {start}s and {end}s")

    if mode == "refuse":
        for seg, a, b in overlaps:
            for prop in curves:
                for kf_prop, _, _ in _targets(seg, prop):
                    kf_list = _find(seg, kf_prop)
                    if kf_list and any(a <= k.time_offset <= b for k in kf_list.keyframes):
                        raise ValueError(f"The range already has {kf_prop.name} keyframes (mode='refuse'); "
                                         f"use mode='compose' to stack or 'replace' to redo")

    edge_us = int(EDGE * 1e6)
    for seg, a, b in overlaps:
        seg_start = seg.target_timerange.start
        for prop, pts in curves.items():
            for kf_prop, static, combine in _targets(seg, prop):
                existing = _find(seg, kf_prop)

                def value(off):
                    base = _value_at(existing, off, static) if mode == "compose" else static
                    return combine(base, _interp(pts, (seg_start + off - s_us) / 1e6))

                # sample at the overlap's edges, the move's own points, and (when composing) the
                # existing keyframes inside the range so their shape is kept
                times = {a, b} | {s_us + int(t * 1e6) - seg_start for t, _ in pts}
                if mode == "compose" and existing:
                    times |= {k.time_offset for k in existing.keyframes}
                # replace: where the range ends inside the clip, keep the old curve's value on the
                # edge and cut to the move just inside, so the animation outside is unchanged
                keep = {}
                if mode == "replace" and existing and existing.keyframes:
                    for edge, inner, inside in ((a, a + edge_us, a > 0),
                                                (b, b - edge_us, b < seg.target_timerange.duration)):
                        old_v = _value_at(existing, edge, static)
                        if inside and abs(old_v - value(edge)) > 1e-9:
                            keep[edge] = old_v
                            if a < inner < b:
                                times.add(inner)
                times = sorted(t for t in times if a <= t <= b)
                samples = [(off, keep[off] if off in keep else value(off)) for off in times]
                _write(seg, kf_prop, samples, a, b)

    if flash:
        add_effect_impl(effect_type="White_Flash", effect_category="scene", start=start,
                        end=min(end, start + 0.25), draft_id=draft_id, track_name="camera_fx_flash")

    return {"draft_id": draft_id, "draft_url": generate_draft_url(draft_id), "move": move,
            "kind": "keyframes", "clips": len(overlaps), "mode": mode}
