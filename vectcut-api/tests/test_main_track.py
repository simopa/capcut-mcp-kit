# Added in capcut-mcp-kit (2026): new projects put their clips on CapCut's main track (the first
# video track, bottom layer) and warn when that track has gaps CapCut's magnet would close.
# See NOTICE at the repository root.
import json

import draft_store
from test_commit_safety import call, client, env, png  # noqa: F401 (fixtures)


def saved_tracks(env, name):
    return json.loads((env.projects / name / "draft_info.json").read_text())["tracks"]


def test_clips_go_on_the_main_track_with_no_empty_track_before(env, client):
    d = call(client, "/create_draft", width=1920, height=1080)["output"]["draft_id"]
    assert call(client, "/add_image", draft_id=d, image_url=png(env.tmp / "a.png", (255, 0, 0)), start=0, end=2,
                track_name="video_main")["success"]
    out = call(client, "/save_draft", draft_id=d, project_name="Principale")
    assert out["success"] and "warnings" not in out["output"], out
    tracks = saved_tracks(env, "Principale")
    assert all(t["segments"] for t in tracks)
    assert tracks[0]["type"] == "video" and tracks[0]["name"] == "video_main"


def test_video_main_is_the_bottom_layer_even_if_an_overlay_came_first(env, client):
    d = call(client, "/create_draft", width=1920, height=1080)["output"]["draft_id"]
    assert call(client, "/add_image", draft_id=d, image_url=png(env.tmp / "logo.png", (0, 0, 255)), start=1, end=2,
                track_name="logo")["success"]
    assert call(client, "/add_image", draft_id=d, image_url=png(env.tmp / "a.png", (255, 0, 0)), start=0, end=3,
                track_name="video_main")["success"]
    assert call(client, "/save_draft", draft_id=d, project_name="Sovrapposto")["success"]
    names = [t["name"] for t in saved_tracks(env, "Sovrapposto") if t["type"] == "video"]
    assert names == ["video_main", "logo"]


def test_a_gap_on_the_main_track_is_reported_at_save(env, client):
    d = call(client, "/create_draft", width=1920, height=1080)["output"]["draft_id"]
    img = png(env.tmp / "a.png", (255, 0, 0))
    assert call(client, "/add_image", draft_id=d, image_url=img, start=2, end=4, track_name="video_main")["success"]
    out = call(client, "/save_draft", draft_id=d, project_name="Buco")
    assert out["success"]
    assert "main track" in out["output"]["warnings"][0] and "0.000-2.000s" in out["output"]["warnings"][0]


def test_an_older_draft_with_an_empty_default_track_saves_without_it(env, client):
    import pyJianYingDraft as draft
    d = call(client, "/create_draft", width=1920, height=1080)["output"]["draft_id"]
    with draft_store.draft_lock(d):  # what kit 0.6 and earlier created before the first clip
        rev, script = draft_store._current(d)
        working = draft_store._copy(script)
        working.add_track(draft.Track_type.video, relative_index=0)
        draft_store._commit_change(d, rev, working, [])
    assert call(client, "/add_image", draft_id=d, image_url=png(env.tmp / "a.png", (255, 0, 0)), start=0, end=2,
                track_name="video_main")["success"]
    assert call(client, "/save_draft", draft_id=d, project_name="Vecchio")["success"]
    assert [t["name"] for t in saved_tracks(env, "Vecchio")] == ["video_main"]
