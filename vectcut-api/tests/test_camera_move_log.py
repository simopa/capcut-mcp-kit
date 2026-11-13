# Added in capcut-mcp-kit (2026): camera moves are listed, taken off (the clip's keyframes, effects and
# background go back to what they were) and moved elsewhere, on new drafts and opened projects.
# See NOTICE at the repository root.
import json

import pytest

import pyJianYingDraft as draft
from pyJianYingDraft import trange

import camera_moves as cm
import draft_store


def make_draft(length=20):
    from create_draft import create_draft
    script, draft_id = create_draft(width=1080, height=1920)
    script.add_track(draft.Track_type.video, track_name="video_main")
    material = draft.Video_material(material_type="video", remote_url="/tmp/x.mp4", material_name="x.mp4",
                                    duration=100, width=0, height=0)
    script.add_segment(draft.Video_segment(material, target_timerange=trange("0s", f"{length}s"),
                                           source_timerange=trange("0s", f"{length}s")), track_name="video_main")
    return draft_id, script


def state(script):
    """Everything a move can change: the clip's keyframes, effect segments and materials, backgrounds."""
    seg = script.tracks["video_main"].segments[0]
    kfs = sorted((kl.keyframe_property.name, tuple((k.time_offset, round(k.values[0], 9)) for k in kl.keyframes))
                 for kl in seg.common_keyframes)
    fx = sorted((name, s.target_timerange.start, s.target_timerange.duration)
                for name, t in script.tracks.items() if t.track_type == draft.Track_type.effect for s in t.segments)
    return kfs, fx, len(script.materials.video_effects), seg.background_filling is not None, \
        len(script.materials.canvases), list(seg.extra_material_refs)


def test_a_move_is_listed_and_taken_off_completely():
    draft_id, script = make_draft()
    before = state(script)
    out = cm.add_camera_move(draft_id, "flash_punches", 2, 8, focus=[[0.3, 0.3], [0.7, 0.3], [0.5, 0.6]],
                             flash=True)
    assert out["move_id"] == "m1" and (out["start"], out["end"]) == (2, 8)
    listed = cm.list_camera_moves(draft_id)["moves"]
    assert [(m["id"], m["move"], m["start"], m["end"], m["effects"]) for m in listed] == \
        [("m1", "flash_punches", 2, 8, 4)]
    assert state(script) != before
    removed = cm.remove_camera_move(draft_id, "m1")
    assert [m["id"] for m in removed["removed"]] == ["m1"] and removed["moves"] == []
    assert state(script) == before


def test_a_move_that_cuts_in_inside_a_clip_comes_off_completely():
    draft_id, script = make_draft()
    cm.add_camera_move(draft_id, "drift_in", 0, 10)
    before = state(script)
    cm.add_camera_move(draft_id, "drift_out", 12, 15)  # starts closer: cuts in 1 ms before 12 s
    cm.add_camera_move(draft_id, "glide_out", 4, 7)  # over the drift, same
    cm.remove_camera_move(draft_id, "m3")
    cm.remove_camera_move(draft_id, "m2")
    assert state(script) == before


def test_taking_off_restores_what_was_there_before():
    draft_id, script = make_draft()
    cm.add_camera_move(draft_id, "drift_in", 0, 10)
    after_first = state(script)
    cm.add_camera_move(draft_id, "quiver", 3, 6)  # composes on the drift
    with pytest.raises(ValueError, match="take off m2 first"):
        cm.remove_camera_move(draft_id, "m1")
    cm.remove_camera_move(draft_id, "m2")
    assert state(script) == after_first


def test_a_range_takes_off_every_move_inside_it():
    draft_id, script = make_draft()
    before = state(script)
    cm.add_camera_move(draft_id, "drift_in", 1, 9)
    cm.add_camera_move(draft_id, "snap_in", 3, 5)
    cm.add_camera_move(draft_id, "jolt", 12, 12.5)
    out = cm.remove_camera_move(draft_id, start=0, end=10)
    assert sorted(m["id"] for m in out["removed"]) == ["m1", "m2"] and [m["id"] for m in out["moves"]] == ["m3"]
    cm.remove_camera_move(draft_id, "m3")
    assert state(script) == before
    with pytest.raises(ValueError, match="No camera move"):
        cm.remove_camera_move(draft_id, start=0, end=10)


def test_a_move_moves_with_its_effects_and_settings():
    draft_id, script = make_draft()
    cm.add_camera_move(draft_id, "blur_push", 2, 6, intensity=0.5, focus=[0.6, 0.4])
    elsewhere = make_draft()
    cm.add_camera_move(elsewhere[0], "blur_push", 10, 14, intensity=0.5, focus=[0.6, 0.4])
    out = cm.move_camera_move(draft_id, "m1", 10)
    assert out["move_id"] == "m2" and out["replaced"] == "m1" and (out["start"], out["end"]) == (10, 14)
    assert state(script)[:3] == state(elsewhere[1])[:3]
    out = cm.move_camera_move(draft_id, "m2", 1, 2, intensity=1.0)
    assert [(m["id"], m["start"], m["end"], m["intensity"]) for m in cm.list_camera_moves(draft_id)["moves"]] == \
        [("m3", 1, 2, 1.0)]


def test_a_failed_move_leaves_the_draft_as_it_was_through_the_route(monkeypatch):
    import capcut_server
    draft_id, script = make_draft(length=6)
    cm.add_camera_move(draft_id, "snap_in", 1, 2)
    client = capcut_server.app.test_client()
    headers = {"Host": "127.0.0.1:9001", "X-CapCut-Kit-Token": "test-token"}
    body = client.post("/move_camera_move", json={"draft_id": draft_id, "move_id": "m1", "start": 30},
                       headers=headers).get_json()
    assert not body["success"] and "No clip" in body["error"]
    assert [m["id"] for m in cm.list_camera_moves(draft_id)["moves"]] == ["m1"]


def test_the_log_survives_a_restart():
    import capcut_server
    draft_id, _ = make_draft()
    headers = {"Host": "127.0.0.1:9001", "X-CapCut-Kit-Token": "test-token"}
    body = capcut_server.app.test_client().post("/add_camera_move", headers=headers, json={
        "draft_id": draft_id, "move": "snap_in", "start": 1, "end": 2}).get_json()
    assert body["success"], body
    draft_store.forget_cache()  # the next read comes from the database, as after a restart
    assert [m["id"] for m in cm.list_camera_moves(draft_id)["moves"]] == ["m1"]


def test_a_blurred_background_is_among_the_exported_materials():
    draft_id, script = make_draft()
    cm.add_camera_move(draft_id, "spin", 0, 4)
    exported = json.loads(script.dumps())
    seg = exported["tracks"][0]["segments"][0]
    canvases = {c["id"] for c in exported["materials"]["canvases"]}
    assert any(ref in canvases for ref in seg["extra_material_refs"])
    assert any(c["type"] == "canvas_blur" for c in exported["materials"]["canvases"])


def test_moves_on_an_opened_project_come_off_too(tmp_path, monkeypatch):
    import clip_edits
    import existing_project
    import save_draft_impl
    from test_existing_project import capcut_content, write_project
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(save_draft_impl, "find_capcut_projects_dir", lambda: str(projects))
    write_project(projects, "Esistente", capcut_content())
    d = existing_project.open_project("Esistente")["draft_id"]
    base = draft_store.get_draft(d).base_project
    original = json.dumps(clip_edits.current_content(base)["tracks"], sort_keys=True)
    out = cm.add_camera_move(d, "dark_cuts_in", 0, 3, focus=[0.5, 0.3])
    assert cm.list_camera_moves(d)["moves"][0]["where"] == f"project track {out['project_track']}"
    cm.add_camera_move(d, "fringe_push", 0.5, 2.5)
    cm.remove_camera_move(d, start=0, end=3)
    assert json.dumps(clip_edits.current_content(base)["tracks"], sort_keys=True) == original
    assert all(not t.segments for t in draft_store.get_draft(d).tracks.values()
               if t.track_type == draft.Track_type.effect)


def test_a_move_is_nudged_by_seconds_or_frames():
    draft_id, script = make_draft()  # 30 fps
    cm.add_camera_move(draft_id, "snap_in", 4, 6)
    out = cm.move_camera_move(draft_id, "m1", shift_frames=-3)
    assert (out["start"], out["end"]) == (pytest.approx(3.9), pytest.approx(5.9))
    out = cm.move_camera_move(draft_id, out["move_id"], shift=0.6, extend=0.5)
    assert (out["start"], out["end"]) == (pytest.approx(4.5), pytest.approx(7.0))
    out = cm.move_camera_move(draft_id, out["move_id"], extend_frames=-15)
    assert (out["start"], out["end"]) == (pytest.approx(4.5), pytest.approx(6.5))
    out = cm.move_camera_move(draft_id, out["move_id"], end=8)  # where it is, longer
    assert (out["start"], out["end"]) == (pytest.approx(4.5), 8)
    # the result is the same as adding the move there in the first place
    elsewhere = make_draft()
    cm.add_camera_move(elsewhere[0], "snap_in", 4.5, 8)
    assert state(script)[:3] == state(elsewhere[1])[:3]


def test_a_nudge_that_cannot_be_done_changes_nothing():
    draft_id, script = make_draft()
    cm.add_camera_move(draft_id, "snap_in", 0.2, 1)
    before = state(script)
    for bad, match in (({"start": 2, "shift": 1}, "not more than one"),
                       ({"extend": 1, "end": 3}, "not more than one"),
                       ({"shift": -0.5}, "before the timeline's start"),
                       ({"extend": -0.75}, "at least")):
        with pytest.raises(ValueError, match=match):
            cm.move_camera_move(draft_id, "m1", **bad)
    assert state(script) == before and [m["id"] for m in cm.list_camera_moves(draft_id)["moves"]] == ["m1"]
