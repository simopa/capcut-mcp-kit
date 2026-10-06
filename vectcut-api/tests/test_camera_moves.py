# Added in capcut-mcp-kit (2026): camera moves compose with existing keyframes, fit short ranges,
# never duplicate timestamps, and effect parameters are honoured. See NOTICE at the repository root.
import pytest

import pyJianYingDraft as draft
from pyJianYingDraft import trange
from pyJianYingDraft.keyframe import Keyframe_property as KP

import camera_moves as cm


def make_draft(clips=((0, 4),), scale=1.0):
    """A draft with one video track; clips are (timeline start, duration) in seconds."""
    from create_draft import create_draft
    script, draft_id = create_draft()
    script.add_track(draft.Track_type.video, track_name="main")
    material = draft.Video_material(material_type="video", remote_url="/tmp/x.mp4", material_name="x.mp4",
                                    duration=100, width=0, height=0)
    for at, dur in clips:
        seg = draft.Video_segment(material, target_timerange=trange(f"{at}s", f"{dur}s"),
                                  source_timerange=trange(f"{at}s", f"{dur}s"),
                                  clip_settings=draft.Clip_settings(scale_x=scale, scale_y=scale))
        script.add_segment(seg, track_name="main")
    return draft_id, script


def kf(seg, prop):
    lst = next((k for k in seg.common_keyframes if k.keyframe_property == prop), None)
    return [(k.time_offset, round(k.values[0], 6)) for k in lst.keyframes] if lst else []


def at(seg, prop, offset):
    """The value CapCut shows at offset (linear between keyframes)."""
    lst = next(k for k in seg.common_keyframes if k.keyframe_property == prop)
    return cm._value_at(lst, offset, None)


def times_unique(seg):
    for lst in seg.common_keyframes:
        offsets = [k.time_offset for k in lst.keyframes]
        assert offsets == sorted(set(offsets)), lst.keyframe_property


def move(draft_id, name, start, end, **kw):
    return cm.add_camera_move(draft_id, name, start, end, track_name="main", **kw)


def test_replace_redoes_a_move_without_duplicates():
    draft_id, script = make_draft()
    move(draft_id, "punch_in", 1, 2)
    once = kf(script.tracks["main"].segments[0], KP.scale_x)
    move(draft_id, "punch_in", 1, 2, mode="replace")
    seg = script.tracks["main"].segments[0]
    times_unique(seg)
    assert kf(seg, KP.scale_x) == once


def test_compose_stacks_on_the_previous_move():
    draft_id, script = make_draft()
    move(draft_id, "punch_in", 1, 3)  # holds 1.18 inside
    move(draft_id, "punch_in", 1.5, 2.5)
    seg = script.tracks["main"].segments[0]
    times_unique(seg)
    assert at(seg, KP.scale_x, 2_000_000) == pytest.approx(1.18 * 1.18)
    # outside the second move the first one is intact
    assert at(seg, KP.scale_x, 1_300_000) == pytest.approx(1.18)
    assert at(seg, KP.scale_x, 2_800_000) == pytest.approx(1.18)


def test_refuse_errors_and_changes_nothing():
    draft_id, script = make_draft()
    move(draft_id, "punch_in", 1, 2)
    before = kf(script.tracks["main"].segments[0], KP.scale_x)
    with pytest.raises(ValueError, match="refuse"):
        move(draft_id, "zoom_in_out", 1.5, 3, mode="refuse")
    assert kf(script.tracks["main"].segments[0], KP.scale_x) == before


def test_keyframes_outside_the_range_are_untouched():
    draft_id, script = make_draft()
    seg = script.tracks["main"].segments[0]
    seg.add_keyframe(KP.position_x, 3_500_000, 0.4)
    move(draft_id, "pan_left", 0.5, 2)
    assert (3_500_000, 0.4) in kf(seg, KP.position_x)


def test_short_punch_is_squeezed_and_ends_neutral():
    draft_id, script = make_draft(scale=1.12)
    move(draft_id, "punch", 1, 1.1)
    seg = script.tracks["main"].segments[0]
    times_unique(seg)
    curve = kf(seg, KP.scale_x)
    assert curve[0] == (1_000_000, pytest.approx(1.12))
    assert curve[-1] == (1_100_000, pytest.approx(1.12))
    assert max(v for _, v in curve) > 1.12 * 1.2  # the kick is still there


def test_too_short_is_refused():
    draft_id, _ = make_draft()
    with pytest.raises(ValueError, match="at least"):
        move(draft_id, "punch", 1, 1.05)


def test_pending_keyframes_are_applied_and_composed():
    draft_id, script = make_draft()
    track = script.tracks["main"]
    track.add_pending_keyframe("position_x", 1.0, "0.3")
    move(draft_id, "pan_right", 0.5, 1.5, easing="linear")
    assert track.pending_keyframes == []
    values = dict(kf(track.segments[0], KP.position_x))
    # pan_right at its midpoint is offset 0 -> only the pending keyframe's 0.3 remains
    assert values[1_000_000] == pytest.approx(0.3, abs=1e-6)


def test_move_across_a_cut_is_continuous():
    draft_id, script = make_draft(clips=((0, 2), (2, 2)))
    move(draft_id, "push_in", 1, 3)
    first, second = script.tracks["main"].segments
    end_of_first = dict(kf(first, KP.scale_x))[2_000_000]
    start_of_second = dict(kf(second, KP.scale_x))[0]
    assert end_of_first == pytest.approx(start_of_second)


def test_non_uniform_scale_clip_gets_both_axes():
    draft_id, script = make_draft()
    seg = script.tracks["main"].segments[0]
    seg.add_keyframe(KP.scale_y, 0, 0.9)  # turns the clip non-uniform
    move(draft_id, "punch_in", 1, 2)
    assert kf(seg, KP.scale_x) and kf(seg, KP.scale_y)
    assert at(seg, KP.scale_y, 1_500_000) == pytest.approx(0.9 * 1.18)


def test_easing_changes_the_curve():
    d1, s1 = make_draft()
    d2, s2 = make_draft()
    move(d1, "push_in", 0, 4, easing="linear")
    move(d2, "push_in", 0, 4, easing="dramatic")
    assert kf(s1.tracks["main"].segments[0], KP.scale_x) != kf(s2.tracks["main"].segments[0], KP.scale_x)


def test_effect_intensity_scales_the_strength_setting():
    draft_id, script = make_draft()
    move(draft_id, "shake", 0, 2, intensity=2)
    effect = script.tracks["camera_fx"].segments[0]
    strength = next(p for p in effect.effect_inst.adjust_params if p.name == "effects_adjust_range")
    assert strength.value == pytest.approx(0.3)


def test_effect_without_strength_refuses_intensity():
    draft_id, _ = make_draft()
    with pytest.raises(ValueError, match="no strength"):
        move(draft_id, "flash", 0, 1, intensity=2)


def test_flash_works_with_effect_moves():
    draft_id, script = make_draft()
    move(draft_id, "shake", 0, 2, flash=True)
    assert len(script.tracks["camera_fx_flash"].segments) == 1


def test_replace_keeps_the_animation_outside_the_range():
    draft_id, script = make_draft(clips=((0, 10),))
    seg = script.tracks["main"].segments[0]
    seg.add_keyframe(KP.scale_x, 0, 1.0)
    seg.add_keyframe(KP.scale_x, 10_000_000, 2.0)  # 1 -> 2 over the clip
    move(draft_id, "punch_in", 4, 6, mode="replace")
    times_unique(seg)
    for t in (0, 1, 2, 3, 3.5, 4, 6, 6.5, 8, 10):
        assert at(seg, KP.scale_x, int(t * 1e6)) == pytest.approx(1 + t / 10), t
    assert at(seg, KP.scale_x, 5_000_000) == pytest.approx(1.18)  # static framing * punch, inside


def test_replace_keeps_offsets_outside_and_ignores_clip_edges():
    draft_id, script = make_draft(clips=((0, 4), (4, 4)))
    first, second = script.tracks["main"].segments
    for seg in (first, second):
        seg.add_keyframe(KP.position_x, 0, 0.0)
        seg.add_keyframe(KP.position_x, 4_000_000, 0.4)
    move(draft_id, "pan_left", 3, 5, mode="replace")
    for seg in (first, second):
        times_unique(seg)
    for t in (0, 1, 2.5, 3):
        assert at(first, KP.position_x, int(t * 1e6)) == pytest.approx(0.1 * t), t
    for t in (1, 2, 3, 4):
        assert at(second, KP.position_x, int(t * 1e6)) == pytest.approx(0.1 * t), t
    # at the cut the pan is continuous, not pinned to the old values
    assert at(first, KP.position_x, 4_000_000) == pytest.approx(at(second, KP.position_x, 0))
