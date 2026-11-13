# Added in capcut-mcp-kit (2026): the camera move presets write clean keyframes inside their range,
# never show the canvas behind a covered move, aim zooms at the focus point(s), add their CapCut
# effects at the right times, and fill behind the moves that shrink the image. See NOTICE at the
# repository root.
import json
import math
import os

import pytest

import pyJianYingDraft as draft
from pyJianYingDraft import trange, CapCut_Video_scene_effect_type
from pyJianYingDraft.keyframe import Keyframe_property as KP

import camera_moves as cm
import camera_presets as cp

ASPECT = 1080 / 1920


def make_draft(clips=((0, 6),), track="video_main"):
    from create_draft import create_draft
    script, draft_id = create_draft(width=1080, height=1920)
    script.add_track(draft.Track_type.video, track_name=track)
    material = draft.Video_material(material_type="video", remote_url="/tmp/x.mp4", material_name="x.mp4",
                                    duration=100, width=0, height=0)
    for at, dur in clips:
        script.add_segment(draft.Video_segment(material, target_timerange=trange(f"{at}s", f"{dur}s"),
                                               source_timerange=trange(f"{at}s", f"{dur}s")), track_name=track)
    return draft_id, script


def lists(seg):
    return {kl.keyframe_property: [(k.time_offset, k.values[0]) for k in kl.keyframes] for kl in seg.common_keyframes}


def value(kfs, off):
    if off <= kfs[0][0]:
        return kfs[0][1]
    for (t0, v0), (t1, v1) in zip(kfs, kfs[1:]):
        if off <= t1:
            return v0 if t1 == t0 else v0 + (v1 - v0) * (off - t0) / (t1 - t0)
    return kfs[-1][1]


def test_catalog_and_builders_agree():
    with open(os.path.join(os.path.dirname(cm.__file__), "camera_moves.json"), encoding="utf-8") as f:
        catalog = {m["name"]: m for m in json.load(f)["keyframes"]}
    assert set(cp.PRESETS) <= set(catalog) and len(cp.PRESETS) == 50
    assert set(cp.EFFECTS) <= set(cp.PRESETS)
    for name in cp.PRESETS:
        assert catalog[name].get("cover") or catalog[name].get("fill"), name
    # every effect a preset uses exists in CapCut, with the parameters it sets
    for name, layers in cp.EFFECTS.items():
        for effect, a, b, values in layers(5.0, [0.0, 1.5, 3.0]):
            meta = CapCut_Video_scene_effect_type[effect].value
            assert set(values) <= {p.name for p in meta.params}, (name, effect)
            assert 0 <= a < b <= 5.0, (name, effect, a, b)


@pytest.mark.parametrize("move", sorted(cp.PRESETS))
@pytest.mark.parametrize("length", [0.6, 5.0])
def test_every_preset_writes_clean_keyframes_inside_its_range(move, length):
    draft_id, script = make_draft()
    cm.add_camera_move(draft_id, move, 0.5, 0.5 + length, fill="none")
    seg = script.tracks["video_main"].segments[0]
    a, b = 500_000, int((0.5 + length) * 1e6)
    for prop, kfs in lists(seg).items():
        offsets = [t for t, _ in kfs]
        assert offsets == sorted(set(offsets)), (move, prop)
        assert all(a - 1000 <= t <= b for t in offsets), (move, prop)  # a cut-in sits 1 ms before
        assert all(math.isfinite(v) for _, v in kfs), (move, prop)
        static = {KP.scale_x: 1.0, KP.position_x: 0.0, KP.position_y: 0.0, KP.rotation: 0.0, KP.alpha: 1.0}[prop]
        # outside the range the clip keeps its own framing
        assert value(kfs, a - 1000) == pytest.approx(static, abs=1e-6), (move, prop)
        assert value(kfs, b) == pytest.approx(static, abs=1e-6), (move, prop)


@pytest.mark.parametrize("move", sorted(cm.COVER_MOVES))
def test_covered_presets_never_show_the_canvas(move):
    curves = cm.move_curves(move, 5.0, 1.0, "smooth", [0.8, 0.2], ASPECT)
    times = sorted({t for pts in curves.values() for t, _ in pts})
    for t in times:
        v = {p: cm._interp(curves[p], t) if p in curves else cm.NEUTRAL[p] for p in ("scale", "x", "y", "rotation")}
        assert v["scale"] >= cm._cover(v["scale"], v["x"], v["y"], v["rotation"], ASPECT) - 1e-6, (move, t)


def test_uniform_cover_keeps_the_shape_of_a_slide():
    raw = cp.PRESETS["track_diagonal"](4.0, 1.0, cm._easer("smooth"))
    curves = cm.move_curves("track_diagonal", 4.0, 1.0, "smooth", None, ASPECT)
    inside = [v for t, v in curves["scale"] if 0.01 < t < 3.99]
    assert max(inside) == pytest.approx(min(inside)) and min(inside) > 1.3  # one zoom for the whole slide
    assert cm._interp(curves["x"], 2.0) == pytest.approx(cm._interp(raw["x"], 2.0))


def test_a_zoom_keeps_the_focus_point_still():
    draft_id, script = make_draft()
    cm.add_camera_move(draft_id, "snap_in", 0, 4, focus=[0.8, 0.3])
    kf = lists(script.tracks["video_main"].segments[0])
    s, x, y = (value(kf[p], 2_000_000) for p in (KP.scale_x, KP.position_x, KP.position_y))
    fx, fy = 2 * 0.8 - 1, 1 - 2 * 0.3
    assert s == pytest.approx(1.25) and x == pytest.approx(fx * (1 - s)) and y == pytest.approx(fy * (1 - s))


def test_each_step_of_a_multi_zoom_aims_at_its_own_point():
    points = [[0.3, 0.3], [0.7, 0.3], [0.5, 0.7]]
    curves = cm.move_curves("triple_punch", 6.0, 1.0, "smooth", points, ASPECT)
    span = (6.0 - 0.3) / 3
    for i, (px, py) in enumerate(points):
        t = i * span + span * 0.8  # well inside step i, after its zoom
        s, x, y = (cm._interp(curves[p], t) for p in ("scale", "x", "y"))
        assert x == pytest.approx((2 * px - 1) * (1 - s), abs=1e-6) and y == pytest.approx((1 - 2 * py) * (1 - s), abs=1e-6)


def test_focus_is_checked_before_anything_changes():
    draft_id, script = make_draft()
    for bad in ([1.2, 0.5], [0.5], [[0.5, 0.5], [0.5]], "middle"):
        with pytest.raises(ValueError, match="focus"):
            cm.add_camera_move(draft_id, "snap_in", 0, 2, focus=bad)
    assert not script.tracks["video_main"].segments[0].common_keyframes


def effect_spans(script, track):
    t = script.tracks.get(track)
    return [] if t is None else [(round(s.target_timerange.start / 1e6, 2), round(s.target_timerange.end / 1e6, 2))
                                 for s in t.segments]


def test_presets_add_their_effects_at_the_right_times():
    draft_id, script = make_draft(((0, 20),))
    out = cm.add_camera_move(draft_id, "flash_punches", 2, 8)
    assert out["effects"] == 3
    span = (6 - 0.3) / 3
    assert effect_spans(script, "camera_fx_white_flash") == [(round(2 + i * span, 2), round(2 + i * span + 0.25, 2))
                                                             for i in range(3)]
    cm.add_camera_move(draft_id, "blur_push", 10, 15)
    # the blur comes in after a beat, peaks during the zoom and is gone by 1.1 s
    assert effect_spans(script, "camera_fx_blur") == [(10.35, 11.1), (13.9, 14.5)]


def test_moves_that_shrink_the_image_get_a_blurred_background():
    draft_id, script = make_draft()
    out = cm.add_camera_move(draft_id, "spin", 0, 4)
    assert out["fill"] == "blur" and script.tracks["video_main"].segments[0].background_filling is not None
    # again on the same clip: the background is already there
    assert cm.add_camera_move(draft_id, "far_pull", 4, 6)["fill"] == "blur"
    # not covered: the spin turns the image without extra zoom
    assert max(v for _, v in lists(script.tracks["video_main"].segments[0])[KP.scale_x]) <= 1.0 + 1e-9


def test_fill_needs_the_bottom_track():
    draft_id, script = make_draft()
    script.add_track(draft.Track_type.video, track_name="overlay")
    material = draft.Video_material(material_type="video", remote_url="/tmp/y.mp4", material_name="y.mp4",
                                    duration=100, width=0, height=0)
    script.add_segment(draft.Video_segment(material, target_timerange=trange("0s", "4s")), track_name="overlay")
    assert cm.add_camera_move(draft_id, "recoil", 0, 0.5, track_name="overlay")["fill"] == "none"
    with pytest.raises(ValueError, match="blurred background"):
        cm.add_camera_move(draft_id, "spin", 0, 4, track_name="overlay", fill="blur")
    assert cm.add_camera_move(draft_id, "spin", 0, 4, fill="none")["fill"] == "none"
    assert script.tracks["video_main"].segments[0].background_filling is None


def test_blackout_steps_cut_through_black_frames():
    draft_id, script = make_draft()
    cm.add_camera_move(draft_id, "dark_cuts_in", 0, 4)
    kf = lists(script.tracks["video_main"].segments[0])
    scale = [value(kf[KP.scale_x], int(t * 1e6)) for t in (0.5, 1.5, 2.5, 3.5)]
    assert scale == pytest.approx([1.0, 1.18, 1.36, 1.54])
    for cut in (1.0, 2.0, 3.0):
        assert value(kf[KP.alpha], int((cut - 0.04) * 1e6)) == pytest.approx(0.0)
        assert value(kf[KP.alpha], int((cut + 0.1) * 1e6)) == pytest.approx(1.0)


def test_presets_work_on_an_opened_project(tmp_path, monkeypatch):
    import save_draft_impl
    from test_existing_project import capcut_content, write_project
    import existing_project
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(save_draft_impl, "find_capcut_projects_dir", lambda: str(projects))
    write_project(projects, "Esistente", capcut_content())
    d = existing_project.open_project("Esistente")["draft_id"]
    out = cm.add_camera_move(d, "dark_cuts_in", 0, 3, focus=[0.5, 0.3])
    assert out["clips"] == 1 and out["fill"] == "none"
    from draft_store import get_draft
    edit = get_draft(d).base_project["edits"][-1]
    props = {kl["property_type"] for kl in edit["fields"]["common_keyframes"]["new"]}
    assert {"KFTypeScaleX", "KFTypeAlpha", "KFTypePositionY"} <= props


@pytest.mark.parametrize("at_start", [True, False])
def test_the_first_frame_of_a_move_shows_the_move(at_start):
    """A zoom out starts from a closer framing: its first frame is already close, both on a cut (the
    clip's start, nothing before it) and inside a clip (cut in 1 ms before the range)."""
    draft_id, script = make_draft()
    start = 0 if at_start else 2
    cm.add_camera_move(draft_id, "drift_out", start, start + 3)
    kfs = lists(script.tracks["video_main"].segments[0])[KP.scale_x]
    a = int(start * 1e6)
    assert value(kfs, a) == pytest.approx(1.25)
    if at_start:
        assert kfs[0] == (0, pytest.approx(1.25))
    else:
        assert value(kfs, a - 1000) == pytest.approx(1.0) and value(kfs, a - 40_000) == pytest.approx(1.0)
