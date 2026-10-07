# Added in capcut-mcp-kit (2026): edits of an opened project's clips at their limits — values that
# are not numbers, the draft's own additions under a ripple, existing animations a move would bend,
# uniform scale, the project duration, long projects. See NOTICE at the repository root. The
# projects are synthetic (see test_existing_project.capcut_content).
import copy
import json

import pytest

import draft_store
import existing_project
from test_clip_edits import edit_clip, keyframed_project, opened, segment, timing, with_title_and_music
from test_commit_safety import call, client, env, png  # noqa: F401 (fixtures)


def saved(root):
    return json.loads((root / "draft_info.json").read_text())


# --- Keyframe values -----------------------------------------------------------------------------

@pytest.mark.parametrize("prop, value, track", [
    ("rotation", "inf", 0), ("rotation", "1e309", 0), ("rotation", "nan deg", 0), ("scale_x", "nan", 0),
    ("volume", "inf%", 1), ("alpha", "200%", 0), ("alpha", "-5%", 0), ("volume", "-10%", 1), ("saturation", "+3", 0),
])
def test_keyframe_values_must_be_finite_and_in_range(env, client, prop, value, track):
    c = keyframed_project()
    for seg in c["tracks"][0]["segments"]:
        seg["uniform_scale"]["on"] = False  # scale_x is then a value of its own
    root, d = opened(env, client, c)
    out = call(client, "/add_video_keyframe", draft_id=d, property_types=[prop], times=[1], values=[value],
               project_track=track)
    assert not out["success"], out
    assert call(client, "/list_clips", draft_id=d)["output"]["edits"] == 0


@pytest.mark.parametrize("prop, value", [("rotation", "inf"), ("alpha", "nan%"), ("alpha", "150%"), ("scale_x", "-1")])
def test_a_new_draft_refuses_the_same_values(env, client, tmp_path, prop, value):
    d = call(client, "/create_draft", width=1080, height=1920)["output"]["draft_id"]
    assert call(client, "/add_image", draft_id=d, image_url=png(tmp_path / "a.png", (0, 0, 255)), start=0, end=3,
                track_name="video_main")["success"]
    out = call(client, "/add_video_keyframe", draft_id=d, property_type=prop, time=1, value=value)
    assert not out["success"] and "video_main" not in out["error"], out
    assert call(client, "/add_video_keyframe", draft_id=d, property_type=prop, time=1, value="1")["success"]


def test_no_timeline_is_written_with_values_that_are_not_numbers():
    for dump in existing_project.SERIALIZERS.values():
        with pytest.raises(ValueError):
            dump({"x": float("inf")})


# --- ripple_all and the draft's own additions ----------------------------------------------------

def added_text(root, text):
    content = saved(root)
    mats = {m["id"]: m for m in content["materials"]["texts"]}
    return next(s["target_timerange"] for t in content["tracks"] for s in t["segments"]
                if s["material_id"] in mats and json.loads(mats[s["material_id"]]["content"])["text"] == text)


def test_ripple_all_moves_what_this_draft_added(env, client):
    root, d = opened(env, client, with_title_and_music())  # SEG-1 0-4, SEG-2 4-8 on the main track
    assert call(client, "/add_text", draft_id=d, text="Nuovo", start=5, end=6)["success"]
    out = edit_clip(client, d, "SEG-1", trim_end=1, ripple_all=True)
    assert out["success"], out
    assert any("1 clip(s) added by this draft moved too" in n for n in out["output"]["notes"]), out
    assert call(client, "/save_draft", draft_id=d)["success"]
    assert added_text(root, "Nuovo") == {"start": 4_000_000, "duration": 1_000_000}


def test_ripple_all_moves_additions_already_saved(env, client):
    root, d = opened(env, client, with_title_and_music())
    assert call(client, "/add_text", draft_id=d, text="Nuovo", start=5, end=6)["success"]
    assert call(client, "/save_draft", draft_id=d)["success"]
    draft_store.forget_cache()  # as after a restart: the draft comes back from SQLite
    assert edit_clip(client, d, "SEG-1", trim_end=1, ripple_all=True)["success"]
    assert call(client, "/save_draft", draft_id=d)["success"]
    assert added_text(root, "Nuovo") == {"start": 4_000_000, "duration": 1_000_000}
    assert timing(client, d, "SEG-2")[:2] == (3, 7)


def test_without_ripple_all_additions_count_among_what_did_not_follow(env, client):
    root, d = opened(env, client, with_title_and_music())
    assert call(client, "/add_text", draft_id=d, text="Nuovo", start=5, end=6)["success"]
    out = edit_clip(client, d, "SEG-1", trim_start=1)
    assert "3 clip(s) on other tracks did not move" in out["output"]["notes"][0], out


def test_ripple_all_refuses_when_an_addition_would_overlap(env, client):
    root, d = opened(env, client, with_title_and_music())
    assert call(client, "/add_text", draft_id=d, text="Prima", start=2, end=3.5, track_name="titoli")["success"]
    assert call(client, "/add_text", draft_id=d, text="Dopo", start=4, end=5, track_name="titoli")["success"]
    before = call(client, "/timeline", draft_id=d)["output"]
    out = edit_clip(client, d, "SEG-1", trim_end=1, ripple_all=True)
    assert not out["success"] and "added by this draft" in out["error"] and "overlap" in out["error"], out
    assert call(client, "/timeline", draft_id=d)["output"] == before
    assert call(client, "/list_clips", draft_id=d)["output"]["edits"] == 0


def test_ripple_all_names_additions_that_span_the_cut(env, client):
    root, d = opened(env, client, with_title_and_music())
    assert call(client, "/add_text", draft_id=d, text="Lungo", start=3, end=6)["success"]
    out = edit_clip(client, d, "SEG-1", trim_end=1, ripple_all=True)
    assert out["success"], out
    assert any("added by this draft" in n and "span the cut" in n for n in out["output"]["notes"]), out


def test_ripple_all_keeps_pending_keyframes_on_the_moved_addition(env, client, tmp_path):
    root, d = opened(env, client, with_title_and_music())
    assert call(client, "/add_image", draft_id=d, image_url=png(tmp_path / "a.png", (0, 0, 255)), start=5, end=6,
                track_name="sopra")["success"]
    assert call(client, "/add_video_keyframe", draft_id=d, track_name="sopra", property_type="alpha", time=5.5,
                value="50%")["success"]
    assert edit_clip(client, d, "SEG-1", trim_end=1, ripple_all=True)["success"]
    assert call(client, "/save_draft", draft_id=d)["success"]
    image = next(s for t in saved(root)["tracks"] if t.get("name") == "sopra" for s in t["segments"])
    assert image["target_timerange"]["start"] == 4_000_000
    alpha = next(kl for kl in image["common_keyframes"] if kl["property_type"] == "KFTypeAlpha")
    assert [k["time_offset"] for k in alpha["keyframe_list"]] == [500_000]  # the same moment of the image


# --- mode="refuse" and an animation that crosses the range ---------------------------------------

def panning_project(end_value=1.0):
    """SEG-1 (0-4 s) pans: position x goes linearly from 0 at 0 s to end_value at 4 s."""
    c = keyframed_project()
    c["tracks"][0]["segments"][0]["common_keyframes"] = [{"id": "KL-X", "property_type": "KFTypePositionX",
                                                          "keyframe_list": [
        {"id": "KX0", "time_offset": 0, "curveType": "Line", "values": [0.0]},
        {"id": "KX1", "time_offset": 4_000_000, "curveType": "Line", "values": [end_value]}]}]
    return c


def test_refuse_mode_refuses_a_range_an_animation_crosses(env, client):
    root, d = opened(env, client, panning_project())
    out = call(client, "/add_camera_move", draft_id=d, move="pan_right", start=1, end=2, mode="refuse")
    assert not out["success"] and "mode='refuse'" in out["error"], out
    assert call(client, "/list_clips", draft_id=d)["output"]["edits"] == 0


def test_refuse_mode_accepts_a_range_where_the_clip_holds_still(env, client):
    root, d = opened(env, client, panning_project(end_value=0.0))  # keyframes, but no movement
    out = call(client, "/add_camera_move", draft_id=d, move="pan_right", start=1, end=2, mode="refuse")
    assert out["success"], out


def test_refuse_mode_on_a_new_draft_refuses_a_crossing_animation(env, client, tmp_path):
    d = call(client, "/create_draft", width=1080, height=1920)["output"]["draft_id"]
    assert call(client, "/add_image", draft_id=d, image_url=png(tmp_path / "a.png", (0, 0, 255)), start=0, end=4,
                track_name="video_main")["success"]
    assert call(client, "/add_video_keyframe", draft_id=d, property_types=["position_x", "position_x"], times=[0, 4],
                values=["0", "1"])["success"]
    out = call(client, "/add_camera_move", draft_id=d, move="pan_right", start=1, end=2, mode="refuse")
    assert not out["success"] and "mode='refuse'" in out["error"], out


# --- Uniform scale -------------------------------------------------------------------------------

@pytest.mark.parametrize("prop", ["scale_x", "scale_y"])
def test_one_axis_of_a_uniform_scale_is_refused(env, client, prop):
    root, d = opened(env, client, keyframed_project())  # uniform_scale on
    out = call(client, "/add_video_keyframe", draft_id=d, property_types=[prop], times=[1], values=["2"])
    assert not out["success"] and "uniform_scale" in out["error"], out
    assert call(client, "/list_clips", draft_id=d)["output"]["edits"] == 0
    assert call(client, "/add_video_keyframe", draft_id=d, property_types=["uniform_scale"], times=[1],
                values=["2"])["success"]


def test_separate_axes_take_their_own_keyframes(env, client):
    c = keyframed_project()
    c["tracks"][0]["segments"][0]["uniform_scale"]["on"] = False
    root, d = opened(env, client, c)
    assert call(client, "/add_video_keyframe", draft_id=d, property_types=["scale_y"], times=[1], values=["2"])["success"]
    assert call(client, "/save_draft", draft_id=d)["success"]
    seg = segment(root, "SEG-1")
    assert seg["uniform_scale"]["on"] is False
    assert [kl["property_type"] for kl in seg["common_keyframes"]] == ["KFTypeScaleY"]


# --- The project duration at save ----------------------------------------------------------------

def broken_merge(monkeypatch, duration):
    real = existing_project.merge

    def merge(original, additions):
        merged, ids, tracks = real(original, additions)
        merged["duration"] = duration
        return merged, ids, tracks
    monkeypatch.setattr(existing_project, "merge", merge)


@pytest.mark.parametrize("trim", [True, False])
def test_a_wrong_project_duration_is_caught_before_writing(env, client, monkeypatch, trim):
    root, d = opened(env, client, keyframed_project())
    if trim:
        assert edit_clip(client, d, "SEG-1", trim_end=1)["success"]
    else:
        assert call(client, "/add_video_keyframe", draft_id=d, property_types=["alpha"], times=[1],
                    values=["50%"])["success"]
    before = (root / "draft_info.json").read_bytes()
    broken_merge(monkeypatch, 99_000_000)
    out = call(client, "/save_draft", draft_id=d)
    assert not out["success"] and "Internal check failed" in out["error"], out
    assert (root / "draft_info.json").read_bytes() == before


def test_keyframes_alone_leave_the_project_duration_as_it_was(env, client):
    c = keyframed_project()
    c["duration"] = 8_000_000  # where the clips end
    root, d = opened(env, client, c)
    assert call(client, "/add_video_keyframe", draft_id=d, property_types=["alpha"], times=[1], values=["50%"])["success"]
    assert call(client, "/save_draft", draft_id=d)["success"]
    assert saved(root)["duration"] == 8_000_000
    assert json.loads((root / "draft_meta_info.json").read_text())["tm_duration"] == 8_000_000


def test_a_trim_sets_the_duration_to_where_the_clips_end(env, client):
    c = keyframed_project()
    c["duration"] = 8_000_000
    c["tracks"][0]["segments"][1]["common_keyframes"] = []  # its keyframe would fall outside
    root, d = opened(env, client, c)
    assert edit_clip(client, d, "SEG-2", trim_end=1)["success"]
    assert call(client, "/save_draft", draft_id=d)["success"]
    assert saved(root)["duration"] == 7_000_000
    assert json.loads((root / "draft_meta_info.json").read_text())["tm_duration"] == 7_000_000


# --- Long projects -------------------------------------------------------------------------------

def test_a_clip_past_the_first_page_can_be_edited(env, client):
    c = keyframed_project()
    c["materials"]["videos"][0]["duration"] = 600_000_000
    first = c["tracks"][0]["segments"][0]
    c["tracks"][0]["segments"] = []
    for i in range(501):
        seg = copy.deepcopy(first)
        seg.update(id=f"SEG-{i:03d}", target_timerange={"start": i * 1_000_000, "duration": 1_000_000},
                   source_timerange={"start": i * 1_000_000, "duration": 1_000_000})
        c["tracks"][0]["segments"].append(seg)
    root, d = opened(env, client, c)
    page = call(client, "/list_clips", draft_id=d, offset=500)["output"]
    assert page["clips"][0]["id"] == "SEG-500" and page["clips"][0]["editable"]
    out = edit_clip(client, d, "SEG-500", trim_end=0.5)
    assert out["success"], out
    assert (out["output"]["clip"]["start"], out["output"]["clip"]["end"]) == (500, 500.5)


# --- Keyframe lists a move does not touch --------------------------------------------------------

def test_a_keyframe_list_the_move_does_not_touch_stays_as_it_was(env, client):
    c = keyframed_project()
    rotation = {"id": "KL-R", "material_id": "", "property_type": "KFTypeRotation"}  # no keyframe_list at all
    c["tracks"][0]["segments"][0]["common_keyframes"] = [rotation]
    root, d = opened(env, client, c)
    assert call(client, "/add_camera_move", draft_id=d, move="push_in", start=1, end=2)["success"]
    assert call(client, "/save_draft", draft_id=d)["success"]
    lists = segment(root, "SEG-1")["common_keyframes"]
    assert lists[0] == rotation and [kl["property_type"] for kl in lists] == ["KFTypeRotation", "KFTypeScaleX"]
