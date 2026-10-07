# Added in capcut-mcp-kit (2026): the clips of an opened project — which can be edited — and the edit
# log applied at save time, checked to change nothing but what it declares. See NOTICE at the
# repository root. The projects are synthetic (see test_existing_project.capcut_content).
import copy
import json

import pytest

import clip_edits
import draft_store
from test_commit_safety import call, client, env  # noqa: F401 (fixtures)
from test_existing_project import COPIES, capcut_content, write_project


def opened(env, client, content, name="Clip"):
    root = write_project(env.projects, name, content)
    d = call(client, "/open_project", project_name=name)["output"]["draft_id"]
    return root, d


def edit(d, segment_id, **fields):
    """Record an edit in the draft as a change of its own (the edit tools will do this)."""
    with draft_store.draft_lock(d):
        rev, script = draft_store._current(d)
        working = draft_store._copy(script)
        clip_edits.record_edit(working.base_project, segment_id, fields)
        draft_store._commit_change(d, rev, working, [])


def rich_content():
    """The fixture with one clip of each kind the inventory must judge."""
    c = capcut_content()
    v = c["tracks"][0]["segments"][0]
    m = c["materials"]
    m["videos"].append({"id": "MAT-P1", "path": "/somewhere/logo.png", "type": "photo", "duration": 10_800_000_000})
    m["audios"] = [{"id": "MAT-A1", "path": "/somewhere/voice.wav", "type": "extract_music", "duration": 9_000_000}]
    m["texts"] = [{"id": "MAT-T1", "content": json.dumps({"text": "Titolo"})}]
    m["speeds"].append({"id": "SPD-2", "speed": 2.0, "type": "speed"})
    m["transitions"] = [{"id": "TR-1", "duration": 500_000}]

    def seg(sid, mid, start, dur, refs, src=0, **extra):
        return {"id": sid, "material_id": mid, "target_timerange": {"start": start, "duration": dur},
                "source_timerange": {"start": src, "duration": dur}, "extra_material_refs": refs,
                "speed": 1.0, **extra}
    c["tracks"].append({"id": "TRK-2", "type": "video", "name": "", "segments": [
        seg("SEG-P", "MAT-P1", 0, 1_000_000, ["SPD-1"]),
        seg("SEG-FAST", "MAT-V1", 1_000_000, 1_000_000, ["SPD-2"]),
        seg("SEG-UNK", "MAT-V1", 2_000_000, 1_000_000, ["SPD-1", "NOWHERE"]),
        seg("SEG-CURVE", "MAT-V1", 3_000_000, 1_000_000, ["SPD-1", "TR-1"], common_keyframes=[
            {"property_type": "KFTypeScaleX", "keyframe_list": [{"id": "K1", "time_offset": 0, "curveType": "Bezier",
                                                                  "values": [1.0]}]}]),
    ]})
    c["tracks"].append({"id": "TRK-3", "type": "audio", "name": "voce", "segments": [
        seg("SEG-A", "MAT-A1", 0, 4_000_000, ["SPD-1"], src=1_000_000)]})
    c["tracks"][1]["segments"].append(seg("SEG-NOSPEED", "MAT-V1", 4_000_000, 1_000_000, ["SPD-1"], speed=None))
    c["tracks"][1]["segments"].append(seg("SEG-ROUND", "MAT-V1", 5_000_000, 1_866_666, ["SPD-1"]))
    c["tracks"][1]["segments"][-1]["source_timerange"]["duration"] = 1_866_667  # 1 µs from a cut
    c["tracks"].append({"id": "TRK-4", "type": "text", "name": "", "segments": [
        {**seg("SEG-T", "MAT-T1", 0, 2_000_000, ["NO-SUCH-SPEED"]), "source_timerange": None}]})
    v["common_keyframes"] = [{"property_type": "KFTypePositionX", "keyframe_list": [
        {"id": "K0", "time_offset": 1_000_000, "curveType": "Line", "values": [0.1], "graphID": "",
         "left_control": {"x": 0.0, "y": 0.0}, "right_control": {"x": 0.0, "y": 0.0}}]}]
    return c


# --- Inventory -----------------------------------------------------------------------------------

def test_the_inventory_says_which_clips_can_be_edited_and_why_not(env, client):
    root, d = opened(env, client, rich_content())
    out = call(client, "/list_clips", draft_id=d)
    assert out["success"], out
    clips = {c["id"]: c for c in out["output"]["clips"]}
    assert out["output"]["total"] == 9 and out["output"]["project_name"] == "Clip"
    assert {i for i, c in clips.items() if c["editable"]} == {"SEG-1", "SEG-P", "SEG-A", "SEG-ROUND", "SEG-T"}
    assert clips["SEG-NOSPEED"]["locked_reason"] == "its speed is not recorded"
    assert "speed" in clips["SEG-FAST"]["locked_reason"]
    assert "does not know (missing)" in clips["SEG-UNK"]["locked_reason"]
    assert "curves" in clips["SEG-CURVE"]["locked_reason"] and clips["SEG-CURVE"]["transition"]
    assert clips["SEG-T"]["kind"] == "text" and clips["SEG-T"]["name"] == "Titolo"  # its missing ref points nowhere
    v = clips["SEG-1"]
    assert (v["kind"], v["name"], v["start"], v["end"], v["source_start"], v["source_end"]) == \
        ("video", "clip.mov", 0, 4, 0, 4)
    assert v["keyframes"] == ["KFTypePositionX"] and clips["SEG-A"]["source_start"] == 1
    # Filters and pages
    only = call(client, "/list_clips", draft_id=d, editable_only=True, kind="audio")["output"]
    assert [c["id"] for c in only["clips"]] == ["SEG-A"]
    page = call(client, "/list_clips", draft_id=d, limit=3)["output"]
    assert len(page["clips"]) == 3 and page["next_offset"] == 3


def test_a_duplicated_clip_id_is_locked(env, client):
    c = capcut_content()
    c["tracks"].append(copy.deepcopy(c["tracks"][0]))
    root, d = opened(env, client, c)
    clips = call(client, "/list_clips", draft_id=d)["output"]["clips"]
    assert [c["locked_reason"] for c in clips] == ["its id is not unique in the project"] * 2


def test_a_new_draft_has_no_clips_of_its_own(env, client):
    d = call(client, "/create_draft", width=1080, height=1920)["output"]["draft_id"]
    out = call(client, "/list_clips", draft_id=d)
    assert not out["success"] and "capcut_open_project" in out["error"]


def test_every_editable_clip_round_trips_unchanged():
    c = rich_content()
    mats = clip_edits._materials(c)
    found, dups = clip_edits._segments(c)
    for _, _, seg in found:
        if clip_edits.clip_verdict(seg, clip_edits._kind(seg, mats), mats, dups) is None:
            assert clip_edits.write_timing(seg, clip_edits.read_timing(seg)) == seg


# --- Edit log ------------------------------------------------------------------------------------

def test_an_edit_changes_only_what_it_declares(env, client):
    original = rich_content()
    root, d = opened(env, client, original)
    edit(d, "SEG-1", target_timerange={"start": 0, "duration": 2_000_000},
         source_timerange={"start": 0, "duration": 2_000_000})
    call(client, "/add_text", draft_id=d, text="Sopra", start=0, end=1)
    out = call(client, "/save_draft", draft_id=d)
    assert out["success"], out

    saved = json.loads((root / "draft_info.json").read_bytes())
    assert {(root / rel).read_bytes() for rel in COPIES} == {(root / "draft_info.json").read_bytes()}
    seg = next(s for t in saved["tracks"] for s in t["segments"] if s["id"] == "SEG-1")
    assert seg["target_timerange"]["duration"] == 2_000_000 and seg["source_timerange"]["duration"] == 2_000_000
    assert saved["duration"] == 6_866_666  # where the last clip ends (SEG-ROUND)
    # Everything else is the original: put the edit back and drop the added track and its materials
    clip_edits.revert_edits(saved, draft_store.get_draft(d).base_project["edits"])
    saved["tracks"] = saved["tracks"][:len(original["tracks"])]
    saved["materials"] = {k: v[:len(original["materials"].get(k, []))] for k, v in saved["materials"].items()
                          if k in original["materials"]}
    saved["duration"] = original["duration"]  # follows the clips (the fixture's own value is arbitrary)
    assert saved == original
    # The inventory and the timeline show the edited clip
    clip = next(c for c in call(client, "/list_clips", draft_id=d)["output"]["clips"] if c["id"] == "SEG-1")
    assert clip["end"] == 2 and call(client, "/list_clips", draft_id=d)["output"]["edits"] == 1


def test_a_change_the_edit_did_not_declare_is_refused(env, client, monkeypatch):
    root, d = opened(env, client, capcut_content())
    edit(d, "SEG-1", target_timerange={"start": 0, "duration": 3_000_000},
         source_timerange={"start": 0, "duration": 3_000_000})
    before = {rel: (root / rel).read_bytes() for rel in COPIES}
    real = clip_edits.apply_edits

    def also_volume(content, edits):
        out = real(content, edits)
        next(s for t in out["tracks"] for s in t["segments"] if s["id"] == "SEG-1")["volume"] = 0.0
        return out
    monkeypatch.setattr(clip_edits, "apply_edits", also_volume)

    out = call(client, "/save_draft", draft_id=d)
    assert not out["success"] and "Internal check failed" in out["error"]
    assert {rel: (root / rel).read_bytes() for rel in COPIES} == before


def test_an_edit_that_no_longer_finds_its_values_is_refused(env, client):
    root, d = opened(env, client, capcut_content())
    edit(d, "SEG-1", target_timerange={"start": 0, "duration": 3_000_000},
         source_timerange={"start": 0, "duration": 3_000_000})
    with draft_store.draft_lock(d):  # as if the log had been written against another version
        rev, script = draft_store._current(d)
        working = draft_store._copy(script)
        working.base_project["edits"][0]["fields"]["target_timerange"]["old"]["duration"] = 1
        draft_store._commit_change(d, rev, working, [])
    out = call(client, "/save_draft", draft_id=d)
    assert not out["success"] and "changed since it was edited" in out["error"]


def test_edits_stack_and_only_editable_clips_take_them(env, client):
    root, d = opened(env, client, rich_content())
    edit(d, "SEG-A", target_timerange={"start": 0, "duration": 3_000_000},
         source_timerange={"start": 1_000_000, "duration": 3_000_000})
    edit(d, "SEG-A", target_timerange={"start": 500_000, "duration": 3_000_000})  # expects the first edit's value
    clip = next(c for c in call(client, "/list_clips", draft_id=d)["output"]["clips"] if c["id"] == "SEG-A")
    assert (clip["start"], clip["end"]) == (0.5, 3.5)
    with pytest.raises(Exception, match="cannot be edited: its speed"):
        edit(d, "SEG-FAST", target_timerange={"start": 0, "duration": 1})
    with pytest.raises(Exception, match="No clip"):
        edit(d, "NOPE", target_timerange={"start": 0, "duration": 1})
    with pytest.raises(Exception, match="would not change"):
        edit(d, "SEG-A", target_timerange={"start": 500_000, "duration": 3_000_000})


# --- Timing edits (trim, move) -------------------------------------------------------------------

def main_track(transition=False, animation=False, fades=False):
    """Two clips back to back on the main track (from one 20 s file), plus an audio clip."""
    c = capcut_content()
    m = c["materials"]
    m["videos"][0]["duration"] = 20_000_000
    first = c["tracks"][0]["segments"][0]
    first["speed"] = 1.0
    second = copy.deepcopy(first)
    second.update(id="SEG-2", target_timerange={"start": 4_000_000, "duration": 4_000_000},
                  source_timerange={"start": 10_000_000, "duration": 4_000_000})
    c["tracks"][0]["segments"].append(second)
    if transition:
        m["transitions"] = [{"id": "TR-1", "duration": 1_000_000, "name": "Dissolve"}]
        first["extra_material_refs"].append("TR-1")
    if animation:
        m["material_animations"] = [{"id": "AN-1", "animations": [{"type": "in", "start": 0, "duration": 500_000}]}]
        second["extra_material_refs"].append("AN-1")
    m["audios"] = [{"id": "MAT-A1", "path": "/somewhere/voice.wav", "duration": 9_000_000}]
    audio = {"id": "SEG-A", "material_id": "MAT-A1", "speed": 1.0, "extra_material_refs": [],
             "target_timerange": {"start": 0, "duration": 4_000_000},
             "source_timerange": {"start": 0, "duration": 4_000_000}}
    if fades:
        m["audio_fades"] = [{"id": "FD-1", "fade_in_duration": 1_000_000, "fade_out_duration": 1_500_000}]
        audio["extra_material_refs"].append("FD-1")
    c["tracks"].append({"id": "TRK-A", "type": "audio", "name": "", "segments": [audio]})
    return c


def edit_clip(client, d, clip_id, **kw):
    return call(client, "/edit_clip", draft_id=d, clip_id=clip_id, **kw)


def timing(client, d, clip_id):
    c = next(c for c in call(client, "/list_clips", draft_id=d)["output"]["clips"] if c["id"] == clip_id)
    return c["start"], c["end"], c["source_start"], c["source_end"]


def test_trimming_the_end_shortens_the_clip_and_leaves_a_gap(env, client):
    root, d = opened(env, client, main_track())
    out = edit_clip(client, d, "SEG-1", trim_end=1)
    assert out["success"], out
    assert out["output"]["clip"]["end"] == 3 and out["output"]["revision"] == 2
    assert "gap at 3.000s (1.000s long)" in out["output"]["notes"][0]
    assert timing(client, d, "SEG-2") == (4, 8, 10, 14)
    assert call(client, "/save_draft", draft_id=d)["success"]
    seg = json.loads((root / "draft_info.json").read_text())["tracks"][0]["segments"][0]
    assert seg["target_timerange"] == {"start": 0, "duration": 3_000_000}
    assert seg["source_timerange"] == {"start": 0, "duration": 3_000_000}


def test_trimming_the_start_moves_the_clip_start_and_its_keyframes(env, client):
    c = main_track()
    c["tracks"][1]["segments"][0]["common_keyframes"] = []  # the audio clip, as CapCut writes it
    c["tracks"][0]["segments"][1]["common_keyframes"] = [{"property_type": "KFTypeScaleX", "keyframe_list": [
        {"id": "K", "time_offset": 2_000_000, "curveType": "Line", "values": [1.2]}]}]
    root, d = opened(env, client, c)
    assert edit_clip(client, d, "SEG-2", trim_start=1.5)["success"]
    assert timing(client, d, "SEG-2") == (5.5, 8, 11.5, 14)
    assert call(client, "/save_draft", draft_id=d)["success"]
    seg = json.loads((root / "draft_info.json").read_text())["tracks"][0]["segments"][1]
    assert seg["common_keyframes"][0]["keyframe_list"][0]["time_offset"] == 500_000  # same moment of the file


def test_ripple_moves_the_clips_after_it(env, client):
    root, d = opened(env, client, main_track())
    out = edit_clip(client, d, "SEG-1", trim_end=1, ripple=True)
    assert out["success"] and out["output"]["shifted"] == ["SEG-2"]
    assert out["output"]["notes"] == ["Ripple: the clips after it moved by -1.000s"]
    assert timing(client, d, "SEG-2") == (3, 7, 10, 14)
    assert timing(client, d, "SEG-A") == (0, 4, 0, 4)  # other tracks do not move


def test_moving_a_clip(env, client):
    root, d = opened(env, client, main_track())
    assert edit_clip(client, d, "SEG-2", move_to=9)["success"]
    assert timing(client, d, "SEG-2") == (9, 13, 10, 14)
    out = edit_clip(client, d, "SEG-2", move_to=2)
    assert not out["success"] and "overlap clip SEG-1" in out["error"]


@pytest.mark.parametrize("kw, reason", [
    (dict(trim_end=-7), "past the end of its file"),
    (dict(trim_start=-1), "overlap clip SEG-1"),
    (dict(trim_end=3.99), "less than a frame"),
    (dict(trim_start=1, move_to=0, ripple=False, clip="SEG-1"), "before the beginning of its file"),
    (dict(move_to=-1), "before the beginning of the timeline"),
    (dict(), "Nothing to do"),
])
def test_impossible_edits_are_refused_and_change_nothing(env, client, kw, reason):
    root, d = opened(env, client, main_track())
    clip = kw.pop("clip", "SEG-2")
    if clip == "SEG-1":
        kw = dict(trim_start=-0.5)
    before = call(client, "/list_clips", draft_id=d)["output"]
    out = edit_clip(client, d, clip, **kw)
    assert not out["success"] and reason in out["error"], out
    assert call(client, "/list_clips", draft_id=d)["output"] == before


def test_keyframes_that_would_fall_outside_are_refused(env, client):
    c = main_track()
    c["tracks"][1]["segments"][0]["common_keyframes"] = []  # the audio clip, as CapCut writes it
    c["tracks"][0]["segments"][1]["common_keyframes"] = [{"property_type": "KFTypeScaleX", "keyframe_list": [
        {"id": "K", "time_offset": 500_000, "curveType": "Line", "values": [1.2]}]}]
    root, d = opened(env, client, c)
    out = edit_clip(client, d, "SEG-2", trim_start=1)
    assert not out["success"] and "keyframe would fall outside" in out["error"]


def test_a_transition_must_still_join_its_clips(env, client):
    root, d = opened(env, client, main_track(transition=True))
    out = edit_clip(client, d, "SEG-1", trim_end=1)
    assert not out["success"] and "no longer join them" in out["error"]
    out = edit_clip(client, d, "SEG-1", trim_end=3.5, ripple=True)
    assert not out["success"] and "no longer fit" in out["error"]
    assert edit_clip(client, d, "SEG-1", trim_end=1, ripple=True)["success"]  # the cut moves, both follow


def test_animations_and_fades_limit_what_can_change(env, client):
    root, d = opened(env, client, main_track(animation=True, fades=True))
    out = edit_clip(client, d, "SEG-2", trim_end=1)
    assert not out["success"] and "animations" in out["error"]
    assert edit_clip(client, d, "SEG-2", move_to=10)["success"]  # moving keeps its length
    out = edit_clip(client, d, "SEG-A", trim_end=2)
    assert not out["success"] and "fades would no longer fit" in out["error"]
    assert edit_clip(client, d, "SEG-A", trim_end=1)["success"]


def test_a_locked_clip_or_a_new_draft_cannot_be_edited(env, client):
    root, d = opened(env, client, rich_content())
    out = edit_clip(client, d, "SEG-FAST", trim_end=0.5)
    assert not out["success"] and "speed is not a constant" in out["error"]
    new = call(client, "/create_draft", width=1080, height=1920)["output"]["draft_id"]
    out = edit_clip(client, new, "SEG-1", trim_end=1)
    assert not out["success"] and "capcut_open_project" in out["error"]


def test_an_edit_is_a_draft_change_like_any_other(env, client):
    root, d = opened(env, client, main_track())
    first = edit_clip(client, d, "SEG-1", trim_end=1, request_id="r1")
    assert first["success"]
    assert edit_clip(client, d, "SEG-1", trim_end=1, request_id="r1") == first  # a retry is not applied twice
    assert timing(client, d, "SEG-1") == (0, 3, 0, 3)
    out = edit_clip(client, d, "SEG-1", trim_end=1, expected_revision=1)
    assert not out["success"] and "revision" in out["error"]


def magnet_project():
    c = main_track()
    c["config"] = {"maintrack_adsorb": True}
    c["materials"]["texts"] = [{"id": "MAT-T1", "content": json.dumps({"text": "Titolo"})}]
    c["tracks"].append({"id": "TRK-T", "type": "text", "name": "", "segments": [
        {"id": "SEG-T", "material_id": "MAT-T1", "target_timerange": {"start": 5_000_000, "duration": 1_000_000}}]})
    return c


def test_on_the_main_track_with_the_magnet_a_trim_closes_up_like_capcut(env, client):
    root, d = opened(env, client, magnet_project())
    out = edit_clip(client, d, "SEG-1", trim_start=1)
    assert out["success"], out
    assert timing(client, d, "SEG-1") == (0, 3, 1, 4)  # keeps its start, plays from 1 s of its file
    assert timing(client, d, "SEG-2") == (3, 7, 10, 14)  # closed up
    assert out["output"]["shifted"] == ["SEG-2"] and "moved by -1.000s" in out["output"]["notes"][0]
    assert "1 clip(s) on other tracks did not move" in out["output"]["notes"][0]
    assert edit_clip(client, d, "SEG-2", trim_end=1)["success"]  # the last one: nothing follows
    assert timing(client, d, "SEG-2") == (3, 6, 10, 13)


def test_an_empty_first_video_track_is_the_main_track(env, client):
    # Seen in CapCut: with an empty first video track (projects of kit 0.6 and earlier) the cover is
    # on that track and a gap on the next one stays
    c = magnet_project()
    c["tracks"].insert(0, {"id": "TRK-EMPTY", "type": "video", "name": "video", "segments": []})
    root, d = opened(env, client, c)
    out = edit_clip(client, d, "SEG-1", trim_end=1)
    assert out["success"] and out["output"]["shifted"] == [] and out["output"]["notes"] == [], out
    assert timing(client, d, "SEG-2") == (4, 8, 10, 14)  # a gap, as in CapCut
    assert edit_clip(client, d, "SEG-2", move_to=10)["success"]  # not the main track: moves are allowed


def test_on_the_main_track_with_the_magnet_a_move_is_refused(env, client):
    root, d = opened(env, client, magnet_project())
    out = edit_clip(client, d, "SEG-2", move_to=10)
    assert not out["success"] and "main track magnet" in out["error"]
    assert edit_clip(client, d, "SEG-A", move_to=1)["success"]  # other tracks move freely


def test_a_project_saved_by_the_kit_can_be_opened_and_edited(env, client, tmp_path):
    from test_commit_safety import png
    d = call(client, "/create_draft", width=1920, height=1080, fps=25)["output"]["draft_id"]
    assert call(client, "/add_image", draft_id=d, image_url=png(tmp_path / "a.png", (0, 0, 255)), start=0, end=3)["success"]
    assert call(client, "/save_draft", draft_id=d, project_name="Fatto dal kit")["success"]
    root = env.projects / "Fatto dal kit"
    assert (root / "template-2.tmp").read_bytes() == (root / "draft_info.json").read_bytes()
    opened = call(client, "/open_project", project_name="Fatto dal kit")
    assert opened["success"], opened
    clip = call(client, "/list_clips", draft_id=opened["output"]["draft_id"])["output"]["clips"][0]
    assert clip["kind"] == "photo" and clip["editable"], clip["locked_reason"]



# --- Texts and ripple across tracks --------------------------------------------------------------

def with_title_and_music():
    """The magnet project with a title over the second clip and music from 6 s."""
    c = magnet_project()  # SEG-1 0-4, SEG-2 4-8 (main track), SEG-T 5-6 (text), SEG-A 0-4 (audio)
    c["tracks"][-1]["segments"][0]["source_timerange"] = None
    c["tracks"][1]["segments"].append({"id": "SEG-M", "material_id": "MAT-A1", "speed": 1.0, "extra_material_refs": [],
                                       "target_timerange": {"start": 6_000_000, "duration": 2_000_000},
                                       "source_timerange": {"start": 0, "duration": 2_000_000}})
    return c


def test_a_title_can_be_trimmed_and_moved(env, client):
    root, d = opened(env, client, with_title_and_music())
    assert edit_clip(client, d, "SEG-T", move_to=4.5)["success"]
    assert edit_clip(client, d, "SEG-T", trim_end=0.5)["success"]
    clip = next(c for c in call(client, "/list_clips", draft_id=d)["output"]["clips"] if c["id"] == "SEG-T")
    assert (clip["start"], clip["end"]) == (4.5, 5) and "source_start" not in clip
    assert call(client, "/save_draft", draft_id=d)["success"]
    seg = next(s for t in json.loads((root / "draft_info.json").read_text())["tracks"] for s in t["segments"]
               if s["id"] == "SEG-T")
    assert seg["target_timerange"] == {"start": 4_500_000, "duration": 500_000} and seg["source_timerange"] is None


def test_ripple_all_keeps_titles_and_music_in_step(env, client):
    c = with_title_and_music()
    voice = c["tracks"][1]["segments"][0]  # SEG-A, 0-5: spans the cut at 4 s
    voice["target_timerange"]["duration"] = voice["source_timerange"]["duration"] = 5_000_000
    root, d = opened(env, client, c)
    out = edit_clip(client, d, "SEG-1", trim_start=1, ripple_all=True)
    assert out["success"], out
    assert sorted(out["output"]["shifted"]) == ["SEG-2", "SEG-M", "SEG-T"]
    assert "Not moved, because they span the cut: SEG-A" in out["output"]["notes"]
    assert timing(client, d, "SEG-2")[:2] == (3, 7)
    title = next(c for c in call(client, "/list_clips", draft_id=d)["output"]["clips"] if c["id"] == "SEG-T")
    assert (title["start"], title["end"]) == (4, 5)
    assert timing(client, d, "SEG-M")[:2] == (5, 7) and timing(client, d, "SEG-A")[:2] == (0, 5)


def test_without_ripple_all_the_reply_says_what_did_not_follow(env, client):
    root, d = opened(env, client, with_title_and_music())
    out = edit_clip(client, d, "SEG-1", trim_start=1)
    assert "2 clip(s) on other tracks did not move" in out["output"]["notes"][0]
    assert "ripple_all moves them" in out["output"]["notes"][0]


def test_ripple_all_stops_at_a_clip_it_cannot_move(env, client):
    c = with_title_and_music()
    c["materials"]["speeds"].append({"id": "SPD-2", "speed": 2.0})
    c["tracks"][1]["segments"][1]["extra_material_refs"] = ["SPD-2"]  # SEG-M: locked
    root, d = opened(env, client, c)
    out = edit_clip(client, d, "SEG-1", trim_end=1, ripple_all=True)
    assert not out["success"] and "SEG-M after it would have to move but cannot be edited" in out["error"]
    assert timing(client, d, "SEG-2")[:2] == (4, 8)


def test_new_titles_refer_to_nothing_missing(env, client):
    import clip_edits
    d = call(client, "/create_draft", width=1080, height=1920)["output"]["draft_id"]
    assert call(client, "/add_text", draft_id=d, text="Ciao", start=0, end=1)["success"]
    content = json.loads(draft_store.get_draft(d).dumps())
    mats = clip_edits._materials(content)
    refs = [r for t in content["tracks"] for s in t["segments"] for r in s["extra_material_refs"]]
    assert all(r in mats for r in refs)


# --- Camera moves on existing clips --------------------------------------------------------------

def keyframed_project():
    """Two clips on the main track; the second already has a scale keyframe with a field the kit
    does not know, which must survive untouched outside the move's range."""
    c = main_track()
    for seg in c["tracks"][0]["segments"]:
        seg.update(common_keyframes=[], uniform_scale={"on": True, "value": 1.0},
                   clip={"scale": {"x": 1.0, "y": 1.0}, "rotation": 0.0, "transform": {"x": 0.0, "y": 0.0},
                         "alpha": 1.0, "flip": {"horizontal": False, "vertical": False}})
    c["tracks"][1]["segments"][0]["common_keyframes"] = []  # the audio clip, as CapCut writes it
    c["tracks"][0]["segments"][1]["common_keyframes"] = [{
        "id": "KL-1", "material_id": "", "property_type": "KFTypeScaleX", "future_list_field": 7,
        "keyframe_list": [{"id": "K-OLD", "time_offset": 3_500_000, "curveType": "Line", "values": [1.5],
                           "graphID": "", "left_control": {"x": 0.0, "y": 0.0}, "right_control": {"x": 0.0, "y": 0.0},
                           "future_keyframe_field": "keep me"}]}]
    return c


def segment(root, sid):
    return next(s for t in json.loads((root / "draft_info.json").read_text())["tracks"] for s in t["segments"]
                if s["id"] == sid)


def test_a_camera_move_animates_existing_clips_and_keeps_other_keyframes(env, client):
    root, d = opened(env, client, keyframed_project())
    out = call(client, "/add_camera_move", draft_id=d, move="punch_in", start=1, end=2)
    assert out["success"] and out["output"]["clips"] == 1 and out["output"]["project_track"] == 0, out
    out = call(client, "/add_camera_move", draft_id=d, move="push_in", start=4.5, end=6)
    assert out["success"] and out["output"]["clips"] == 1, out
    assert call(client, "/save_draft", draft_id=d)["success"]

    first = segment(root, "SEG-1")
    scale = next(kl for kl in first["common_keyframes"] if kl["property_type"] == "KFTypeScaleX")
    offsets = [k["time_offset"] for k in scale["keyframe_list"]]
    assert offsets[0] >= 1_000_000 and offsets[-1] <= 2_000_000 and len(offsets) >= 3
    second = segment(root, "SEG-2")
    kl = next(kl for kl in second["common_keyframes"] if kl["property_type"] == "KFTypeScaleX")
    assert kl["future_list_field"] == 7 and kl["id"] == "KL-1"
    old = [k for k in kl["keyframe_list"] if k["id"] == "K-OLD"]
    assert old == [{"id": "K-OLD", "time_offset": 3_500_000, "curveType": "Line", "values": [1.5], "graphID": "",
                    "left_control": {"x": 0.0, "y": 0.0}, "right_control": {"x": 0.0, "y": 0.0},
                    "future_keyframe_field": "keep me"}]  # outside the move (0.5-2 s of the clip): as it was
    clip = next(c for c in call(client, "/list_clips", draft_id=d)["output"]["clips"] if c["id"] == "SEG-1")
    assert clip["keyframes"] == ["KFTypeScaleX"]


def test_refuse_mode_and_locked_clips_stop_a_move_on_existing_clips(env, client):
    c = keyframed_project()
    root, d = opened(env, client, c)
    out = call(client, "/add_camera_move", draft_id=d, move="push_in", start=7, end=8, mode="refuse")
    assert not out["success"] and "mode='refuse'" in out["error"]
    out = call(client, "/add_camera_move", draft_id=d, move="push_in", start=0, end=1, project_track=1)
    assert not out["success"] and "index of a video track" in out["error"]
    out = call(client, "/add_camera_move", draft_id=d, move="push_in", start=20, end=21)
    assert not out["success"] and "No clip on track 0" in out["error"]
    assert call(client, "/list_clips", draft_id=d)["output"]["edits"] == 0


def test_an_effect_move_on_an_opened_project_is_an_added_track(env, client):
    root, d = opened(env, client, keyframed_project())
    out = call(client, "/add_camera_move", draft_id=d, move="shake", start=1, end=2)
    assert out["success"] and out["output"]["kind"] == "effect", out
    out = call(client, "/save_draft", draft_id=d)
    assert out["success"] and out["output"]["added_tracks"] == 1


def test_keyframes_on_existing_clips(env, client):
    root, d = opened(env, client, keyframed_project())
    out = call(client, "/add_video_keyframe", draft_id=d, property_types=["alpha", "alpha", "rotation"],
               times=[0, 1, 5], values=["0%", "100%", "45deg"])
    assert out["success"] and out["output"]["clips"] == 2 and out["output"]["added_keyframes_count"] == 3, out
    out = call(client, "/add_video_keyframe", draft_id=d, property_types=["alpha"], times=[1], values=["50%"])
    assert out["success"]  # replaces the keyframe at the same moment
    assert call(client, "/save_draft", draft_id=d)["success"]
    alpha = next(kl for kl in segment(root, "SEG-1")["common_keyframes"] if kl["property_type"] == "KFTypeAlpha")
    assert [(k["time_offset"], k["values"]) for k in alpha["keyframe_list"]] == [(0, [0.0]), (1_000_000, [0.5])]
    rotation = next(kl for kl in segment(root, "SEG-2")["common_keyframes"] if kl["property_type"] == "KFTypeRotation")
    assert [(k["time_offset"], k["values"]) for k in rotation["keyframe_list"]] == [(1_000_000, [45.0])]
    old = next(kl for kl in segment(root, "SEG-2")["common_keyframes"] if kl["property_type"] == "KFTypeScaleX")
    assert old["keyframe_list"][0]["future_keyframe_field"] == "keep me"


def test_keyframes_on_existing_clips_are_all_or_nothing(env, client):
    root, d = opened(env, client, keyframed_project())
    out = call(client, "/add_video_keyframe", draft_id=d, property_types=["alpha", "alpha"], times=[1, 30],
               values=["50%", "50%"])
    assert not out["success"] and "No clip on track 0" in out["error"]
    out = call(client, "/add_video_keyframe", draft_id=d, property_types=["position_x"], times=[1], values=["99"])
    assert not out["success"] and "between -10 and 10" in out["error"]
    out = call(client, "/add_video_keyframe", draft_id=d, property_types=["rotation"], times=[1], values=["9deg"],
               project_track=1)
    assert not out["success"] and "only volume" in out["error"]
    assert call(client, "/add_video_keyframe", draft_id=d, property_types=["volume"], times=[1], values=["50%"],
                project_track=1)["success"]
    assert call(client, "/list_clips", draft_id=d)["output"]["edits"] == 1
