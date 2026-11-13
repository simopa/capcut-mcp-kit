# Added in capcut-mcp-kit: a library of professional camera movements (drifts, dollies, rushes, bounces,
# step zooms, slides, spins, shakes...) built from keyframes and CapCut effects. See NOTICE at the
# repository root.
"""
Camera move presets: each one is a builder of relative keyframe curves (as in camera_moves.BUILDERS)
plus, where the look needs it, CapCut effects timed inside the move's range.

Builders get (duration d, intensity k, ease) and return {property: [(seconds, value)]} with scale as a
multiplier, x / y as image offsets in half-canvas units (+x right, +y up), rotation in degrees
(clockwise) and alpha as a multiplier. Two optional keys guide the caller:
  "_steps": start times of the zoom steps of a multi-target move (one focus point per step)
Timings (in, hold, out) are in seconds; intensity 1 gives each move's own amplitude, and every
amplitude scales with it.

Moves flagged "fill" in camera_moves.json shrink the image below the canvas on purpose (tunnel,
recoil, full spin, whip): they are not covered by extra zoom, and on a new draft's main track the
clip gets a blurred background behind it. All the others are covered: the scale rises as much as
rotation and offsets need, so no edge of the canvas is ever empty; "uniform" covers keep the move's
shape by zooming the whole move by one factor (slides, steady moves, a zoom out that rolls).
Zoom-outs start from a closer framing and pull back to the clip's own: start them on a cut.
"""

import math
import random
from typing import Callable, Dict, List, Tuple

Curve = List[Tuple[float, float]]
FRAME = 1 / 24


# ---------------------------------------------------------------- shapes

def iho(d: float, peak: float, rin: float, rout: float, e: Callable, base: float = 1.0) -> Curve:
    """In, hold, out: ease from base to peak in rin seconds, hold, ease back in rout seconds."""
    rin, rout = min(rin, d / 3), min(rout, d / 3)
    up = e([(0, base), (rin, peak)])
    down = e([(d - rout, peak), (d, base)])
    return up + [p for p in down if p[0] > rin]


def line(d: float, v0: float, v1: float) -> Curve:
    return [(0, v0), (d, v1)]


def wave(d: float, amp: float, hz: float, phase: float = 0.0, t0: float = 0.0, t1: float = None,
         fade: float = 0.3) -> Curve:
    """A sine around 0 between t0 and t1, faded in and out, sampled 12 times a second (CapCut
    interpolates linearly between keyframes)."""
    t1 = d if t1 is None else min(t1, d)
    t0 = max(0.0, t0)
    if t1 - t0 < 2 * FRAME:
        return [(0, 0.0), (d, 0.0)]
    n = max(2, int((t1 - t0) * 12))
    out = [(0, 0.0)] if t0 > 0 else []
    for i in range(n + 1):
        t = t0 + (t1 - t0) * i / n
        env = min(1.0, (t - t0) / fade, (t1 - t) / fade) if fade else 1.0
        out.append((t, amp * env * math.sin(2 * math.pi * hz * (t - t0) + phase)))
    if t1 < d:
        out.append((d, 0.0))
    return out


def jitter(d: float, amp: float, rate: float, seed: int, t0: float = 0.0, t1: float = None) -> Curve:
    """Irregular shake: random values in [-amp, amp] rate times a second, 0 at both ends."""
    t1 = d if t1 is None else min(t1, d)
    t0 = max(0.0, t0)
    if t1 - t0 < 2 * FRAME:
        return [(0, 0.0), (d, 0.0)]
    rnd = random.Random(seed)
    n = max(2, int((t1 - t0) * rate))
    pts = [(t0 + (t1 - t0) * i / n, 0.0 if i in (0, n) else rnd.uniform(-amp, amp)) for i in range(n + 1)]
    return ([(0, 0.0)] if t0 > 0 else []) + pts + ([(d, 0.0)] if t1 < d else [])


def add(*curves: Curve) -> Curve:
    """Sum of curves (each linear between its points), sampled at every point of any of them."""
    times = sorted({t for c in curves for t, _ in c})
    return [(t, sum(at(c, t) for c in curves)) for t in times]


def at(c: Curve, t: float) -> float:
    if t <= c[0][0]:
        return c[0][1]
    for (t0, v0), (t1, v1) in zip(c, c[1:]):
        if t <= t1:
            return v0 if t1 == t0 else v0 + (v1 - v0) * (t - t0) / (t1 - t0)
    return c[-1][1]


def stairs(d: float, levels: List[float], rise: float, e: Callable, out: float = 0.25,
           base: float = 1.0) -> Tuple[Curve, List[float]]:
    """One level per equal part of the range, each reached in `rise` seconds from the step's start
    and held, then back to base in `out` seconds. Returns (curve, start time of each step)."""
    out = min(out, d / 4)
    span = (d - out) / len(levels)
    rise = min(rise, span / 2)
    pts, prev, starts = [(0.0, base)], base, []
    for i, lv in enumerate(levels):
        t = i * span
        starts.append(t)
        if pts[-1][0] < t:
            pts.append((t, prev))
        pts += e([(t, prev), (t + rise, lv)])[1:]
        prev = lv
    if pts[-1][0] < d - out:
        pts.append((d - out, prev))
    pts += e([(d - out, prev), (d, base)])[1:]
    return pts, starts


def hard_stairs(d: float, levels: List[float], gap: float = 2 * FRAME) -> Tuple[Curve, Curve, List[float]]:
    """Jump cuts between framings separated by black frames: (scale, alpha, step starts)."""
    span = d / len(levels)
    s, a, starts = [], [(0, 1.0)], []
    for i, lv in enumerate(levels):
        t = i * span
        starts.append(t)
        s += [(t, lv), (t + span - 1e-3, lv)] if i < len(levels) - 1 else [(t, lv), (d, lv)]
        if i:
            a += [(t - gap - 1e-3, 1.0), (t - gap, 0.0), (t, 0.0), (t + 1e-3, 1.0)]
    a.append((d, 1.0))
    return s, a, starts


# ---------------------------------------------------------------- the moves

def _whip_punch(d, k, e):
    w = min(0.2, d / 4)
    peak = 1 + 0.5 * k
    return {"scale": [(0, 1.0), (w, 1.0), (w + 1e-3, peak), (d - w, peak), (d - w + 1e-3, 1.0), (d, 1.0)],
            "x": [(0, 0.0), (w, -0.9 * k), (w + 1e-3, 0.9 * k), (2 * w, 0.0), (d - 2 * w, 0.0),
                  (d - w, -0.9 * k), (d - w + 1e-3, 0.9 * k), (d, 0.0)]}


def _multi(levels, rise, rot=None):
    """Successive zooms, one framing per step (each step can aim at its own focus point)."""
    def build(d, k, e):
        curve, starts = stairs(d, [1 + (lv - 1) * k for lv in levels], rise, e, out=0.3)
        out = {"scale": curve, "_steps": starts}
        if rot:
            out["rotation"] = stairs(d, [r * k for r in rot], rise, e, out=0.3, base=0.0)[0]
        return out
    return build


def _three_speed(slow1, fast, slow2, delay=0.0):
    """Slow, fast, slow (or the reverse) zoom: levels reached at fractions of the range."""
    def build(d, k, e):
        out = min(0.3, d / 5)
        pts = [(0, 1.0)] + ([(delay * d, 1.0)] if delay else [])
        for frac, lv in (slow1, fast, slow2):
            pts.append((frac * (d - out), 1 + (lv - 1) * k))
        pts += e([(d - out, pts[-1][1]), (d, 1.0)])[1:]
        return {"scale": pts}
    return build


def _slide(points):
    """A slide through (fraction of the range, x, y) points: half the travel measured on the
    reference, so the zoom that keeps the canvas covered stays moderate."""
    def build(d, k, e):
        xs = [(f * d, x * k) for f, x, _ in points]
        ys = [(f * d, y * k) for f, _, y in points]
        return {"x": xs, "y": ys}
    return build


def _hold_drift(peak, rin, rout, x=0.0, y=0.0, rot=0.0):
    """In, hold with a drift that goes out and back (sine half period), out."""
    def build(d, k, e):
        r_in, r_out = min(rin, d / 3), min(rout, d / 3)
        out = {"scale": iho(d, 1 + (peak - 1) * k, r_in, r_out, e)}
        hold = d - r_in - r_out
        for prop, amp in (("x", x), ("y", y), ("rotation", rot)):
            if amp:
                out[prop] = wave(d, amp * k, 0.5 / hold, 0.0, r_in, d - r_out, fade=0)
        return out
    return build


PRESETS: Dict[str, Callable] = {
    # Slow continuous moves over the whole range (they cut back at the end: end them on a cut)
    "drift_in": lambda d, k, e: {"scale": line(d, 1.0, 1 + 0.25 * k)},
    "drift_out": lambda d, k, e: {"scale": line(d, 1 + 0.25 * k, 1.0)},
    "drift_in_roll": lambda d, k, e: {"scale": line(d, 1.0, 1 + 0.25 * k), "rotation": line(d, 0.0, -8 * k)},
    "drift_out_roll": lambda d, k, e: {"scale": line(d, 1 + 0.25 * k, 1.0), "rotation": line(d, 0.0, -5 * k)},
    "glide_in": lambda d, k, e: {"scale": e(line(d, 1.0, 1 + 0.36 * k), 4)},
    "glide_out": lambda d, k, e: {"scale": e(line(d, 1 + 0.4 * k, 1.0), 4)},
    "stride_in": lambda d, k, e: {"scale": line(d, 1.0, 1 + 0.4 * k), "y": wave(d, 0.02 * k, 1.8),
                                "rotation": wave(d, 0.4 * k, 0.9, 1.0)},
    "stride_out": lambda d, k, e: {"scale": line(d, 1 + 0.39 * k, 1.0), "y": wave(d, 0.02 * k, 1.8),
                                 "rotation": wave(d, 0.4 * k, 0.9, 1.0)},
    "rock_in": lambda d, k, e: {"scale": line(d, 1.0, 1 + 0.49 * k), "rotation": wave(d, 2.0 * k, 0.7),
                                  "x": wave(d, 0.02 * k, 0.7, 0.5)},
    "jitter_in": lambda d, k, e: {"scale": line(d, 1.0, 1 + 0.5 * k), "x": jitter(d, 0.008 * k, 10, 7),
                                   "y": jitter(d, 0.008 * k, 10, 8)},
    "rumble_in": lambda d, k, e: {"scale": line(d, 1.0, 1 + 0.25 * k), "rotation": wave(d, 2.0 * k, 0.9),
                                   "x": wave(d, 0.04 * k, 0.9, 0.3), "y": wave(d, 0.06 * k, 0.9, math.pi)},
    "lens_push": lambda d, k, e: {"scale": line(d, 1.0, 1 + 0.2 * k)},
    "lens_pull": lambda d, k, e: {"scale": line(d, 1 + 0.18 * k, 1.0)},
    "fringe_push": lambda d, k, e: {"scale": line(d, 1.0, 1 + 0.04 * k)},
    "fringe_pull": lambda d, k, e: {"scale": line(d, 1 + 0.2 * k, 1.0)},
    "lag_push": lambda d, k, e: {"scale": line(d, 1.0, 1 + 0.2 * k)},
    "lag_pull": lambda d, k, e: {"scale": line(d, 1 + 0.22 * k, 1.0), "rotation": wave(d, 3 * k, 0.25)},
    "surge_snap": lambda d, k, e: {"scale": [(0, 1.0), (max(d - 0.25, d * 0.8), 1 + 0.49 * k), (d, 1.0)]},
    "beat_zoom": lambda d, k, e: {"scale": stairs(d, [1 + 0.09 * k * (i + 1) for i in range(max(1, int((d - 0.25) / 0.5)))],
                                                  0.08, e)[0]},

    # In, hold, out
    "snap_in": lambda d, k, e: {"scale": iho(d, 1 + 0.25 * k, 0.08, 0.1, e)},
    "handheld_soft": lambda d, k, e: {"scale": iho(d, 1 + 0.05 * k, 0.35, 0.35, e),
                                      "x": wave(d, 0.02 * k, 0.35), "y": wave(d, 0.015 * k, 0.5, 1.0),
                                      "rotation": wave(d, 0.3 * k, 0.4, 2.0)},
    "crane_lift": lambda d, k, e: {"scale": iho(d, 1 + 0.15 * k, 0.6, 0.6, e), "y": iho(d, -0.14 * k, 0.6, 0.6, e, 0.0)},
    "ease_zoom": lambda d, k, e: {"scale": iho(d, 1 + 0.25 * k, 0.75, 0.75, e)},
    "iris_in": lambda d, k, e: {"scale": [(0, 1.0), (min(0.25, d / 4), 1 + 0.11 * k), (min(0.6, d / 3), 1 + 0.17 * k)]
                                      + [p for p in e([(d - min(0.4, d / 3), 1 + 0.17 * k), (d, 1.0)]) if p[0] > min(0.6, d / 3)]},
    "blur_push": lambda d, k, e: {"scale": iho(d - min(0.5, d / 4), 1 + 0.25 * k, 1.0, 0.5, e) + [(d, 1.0)]},
    "blur_rumble": lambda d, k, e: {"scale": iho(d, 1 + 0.24 * k, 1.1, 0.9, e),
                                      "x": jitter(d, 0.01 * k, 8, 3), "y": jitter(d, 0.01 * k, 8, 4)},
    "scramble_in": lambda d, k, e: {"scale": add(iho(d, 1 + 0.25 * k, 0.35, 0.5, e), jitter(d, 0.04 * k, 12, 11, 0, 0.4),
                                                jitter(d, 0.05 * k, 12, 12, d - 0.5)),
                                   "x": add(jitter(d, 0.06 * k, 12, 13, 0, 0.4), jitter(d, 0.2 * k, 12, 14, d - 0.5)),
                                   "y": add(jitter(d, 0.1 * k, 12, 15, 0, 0.4), jitter(d, 0.1 * k, 12, 16, d - 0.5))},
    "float_up": _hold_drift(1.25, 0.45, 0.35, y=0.14, rot=-1.3),
    "float_side": _hold_drift(1.25, 0.45, 0.35, x=0.30),
    "breathe": lambda d, k, e: {"scale": [(t, 1 + 0.125 * k * (1 - math.cos(2 * math.pi * t / d))) for t in
                                          [d * i / 24 for i in range(25)]]},
    "quiver": lambda d, k, e: {"scale": iho(d, 1 + 0.1 * k, 0.4, 0.25, e),
                                "x": jitter(d, 0.06 * k, 6, 21, 0.4, d - 0.25), "rotation": jitter(d, 0.5 * k, 6, 22, 0.4, d - 0.25)},
    "far_pull": lambda d, k, e: {"scale": iho(d, max(0.2, 1 - 0.65 * k), 0.4, 0.4, e)},
    "hurtle_in": lambda d, k, e: {"scale": iho(d, 1 + 0.5 * k, 1.0, 0.6, e),
                                          "rotation": add(jitter(d, 2 * k, 10, 31, 0, 1.0), jitter(d, 3 * k, 10, 32, d - 0.6))},
    "woozy_drift": lambda d, k, e: {"x": [(0, 0.0), (min(0.25, d / 4), -0.2 * k), (d - min(0.25, d / 4), 0.24 * k), (d, 0.0)],
                                    "scale": [(0, 1.0), (d - min(0.25, d / 4), 1 + 0.24 * k), (d, 1.0)],
                                    "rotation": wave(d, 1.5 * k, 0.4)},
    "recoil": lambda d, k, e: {"scale": [(0, 1.0), (0.17, 1 - 0.13 * k), (0.25, 1 - 0.12 * k), (0.42, 1.0)]},
    "jolt": lambda d, k, e: {"y": [(0, 0.0), (0.08, 0.11 * k), (0.2, 0.07 * k), (0.33, 0.1 * k), (0.5, 0.0)],
                             "x": [(0, 0.0), (0.12, 0.03 * k), (0.25, -0.03 * k), (0.38, 0.01 * k), (0.5, 0.0)]},

    # Several framings, slides, spins
    "triple_punch": _multi([1.5, 1.62, 2.0], 0.3),
    "flash_punches": _multi([2.03, 1.39, 2.4], 0.25),
    "fringe_punches": _multi([1.4, 1.5], 0.35, rot=[4.5, -3.0]),
    "creep_leap_creep": _three_speed((0.5, 1.29), (0.54, 1.89), (1.0, 2.45)),
    "leap_creep_leap": _three_speed((0.2, 1.85), (0.52, 2.06), (0.62, 2.91), delay=0.08),
    "slide_leap": _slide([(0, 0, 0), (0.46, -0.11, 0.03), (0.5, -0.27, 0.07), (1.0, -0.4, 0.11)]),
    "slide_burst": _slide([(0, 0, 0), (0.1, 0.12, -0.18), (0.82, 0.16, -0.24), (0.94, 0.31, -0.46), (1.0, 0.31, -0.46)]),
    "track_up": lambda d, k, e: {"y": line(d, 0.0, 0.11 * k)},
    "track_side": lambda d, k, e: {"x": line(d, 0.0, 0.1 * k)},
    "track_diagonal": lambda d, k, e: {"x": line(d, 0.0, -0.3 * k), "y": line(d, 0.0, -0.39 * k)},
    "whip_punch": _whip_punch,
    "dark_cuts_in": lambda d, k, e: dict(zip(("scale", "alpha", "_steps"), hard_stairs(
        d, [1 + 0.18 * k * i for i in range(max(2, min(5, int(d))))]))),
    "dark_cuts_out": lambda d, k, e: dict(zip(("scale", "alpha", "_steps"), hard_stairs(
        d, [1 + 0.18 * k * i for i in reversed(range(max(2, min(5, int(d)))))]))),
    "spin": lambda d, k, e: {"rotation": [(t, -360 * k * (0.5 - 0.5 * math.cos(math.pi * t / d))) for t in
                                          [d * i / 36 for i in range(37)]]},
}


# ---------------------------------------------------------------- effects inside a move

# (CapCut scene effect, start, end, {parameter: value in its own range}); negative times count from
# the end of the range, None = the range's end
Layer = Tuple[str, float, float, Dict[str, float]]


def _steps_flash(effect: str, length: float, values=None):
    def layers(d, steps):
        return [(effect, t, min(d, t + length), values or {}) for t in steps if t > 0] + \
               [(effect, 0.0, min(d, length), values or {})]
    return layers


EFFECTS: Dict[str, Callable[[float, List[float]], List[Layer]]] = {
    "lens_push": lambda d, s: [("Fisheye", 0, d, {"effects_adjust_distortion": 0.5})],
    "lens_pull": lambda d, s: [("Fisheye", 0, d, {"effects_adjust_distortion": 0.5})],
    "fringe_push": lambda d, s: [("Chromatic", 0, d, {}), ("Fisheye", 0, d, {"effects_adjust_distortion": 0.3})],
    "fringe_pull": lambda d, s: [("Chromatic", 0, d, {}), ("Fisheye", 0, d, {"effects_adjust_distortion": 0.3})],
    "lag_push": lambda d, s: [("Stuck_Frame", 0, d, {})],
    "lag_pull": lambda d, s: [("Stuck_Frame", 0, d, {})],
    "surge_snap": lambda d, s: [("Motion_Blur", max(0, d - 0.35), d, {"effects_adjust_intensity": 0.6})],
    "iris_in": lambda d, s: [("Vignette", 0, d, {}), ("Fisheye", 0, d, {"effects_adjust_distortion": 0.25})],
    "blur_push": lambda d, s: [("Blur", min(0.35, d / 8), min(1.1, d / 3), {"effects_adjust_blur": 0.6}),
                                  ("Blur", max(0, d - 1.1), max(0, d - min(0.5, d / 4)), {"effects_adjust_blur": 0.6})],
    "blur_rumble": lambda d, s: [("Blur", 0, min(1.1, d / 3), {"effects_adjust_blur": 0.4}),
                                   ("Blur", max(0, d - 0.9), d, {"effects_adjust_blur": 0.4})],
    "scramble_in": lambda d, s: [("Motion_Blur", 0, min(0.4, d / 3), {}), ("Motion_Blur", max(0, d - 0.5), d, {})],
    "hurtle_in": lambda d, s: [("Motion_Blur", 0, min(1.0, d / 3), {"effects_adjust_intensity": 0.5}),
                                       ("Motion_Blur", max(0, d - 0.6), d, {"effects_adjust_intensity": 0.5})],
    "woozy_drift": lambda d, s: [("Dizzy", 0, d, {})],
    "whip_punch": lambda d, s: [("Motion_Blur", 0, min(0.4, d / 3), {}), ("Motion_Blur", max(0, d - 0.4), d, {})],
    "flash_punches": _steps_flash("White_Flash", 0.25),
    "fringe_punches": _steps_flash("Chromatic", 0.4),
    "spin": lambda d, s: [("Rotary_Focus", 0.15 * d, 0.85 * d, {})],
    "recoil": lambda d, s: [("White_Flash", 0, min(0.2, d), {})],
}
