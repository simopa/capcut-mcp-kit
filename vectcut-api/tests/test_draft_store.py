# Added in capcut-mcp-kit (2026): drafts survive a restart, unknown IDs are errors, failed changes roll back.
# See NOTICE at the repository root.
import json
import shutil
import subprocess
import threading

import pytest

import draft_store
from draft_cache import DRAFT_CACHE

HOST = {"Host": "127.0.0.1:9001"}


@pytest.fixture
def client():
    import capcut_server
    return capcut_server.app.test_client()


def call(client, route, **payload):
    return client.post(route, json=payload, headers=HOST).get_json()


def new_draft(client):
    body = call(client, "/create_draft", width=1080, height=1920)
    assert body["success"], body
    return body["output"]["draft_id"]


def restart():
    """What a backend restart does to memory: every draft is gone from the cache."""
    DRAFT_CACHE.clear()


def segments(draft_id, track):
    return len(DRAFT_CACHE[draft_id].tracks[track].segments) if track in DRAFT_CACHE[draft_id].tracks else 0


@pytest.fixture
def clip(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    path = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=3",
                    "-f", "lavfi", "-i", "sine=duration=3", "-shortest", str(path)], check=True)
    return str(path)


def test_unknown_draft_id_is_an_error_and_creates_nothing(client):
    before = set(DRAFT_CACHE)
    body = call(client, "/add_text", draft_id="dfd_cat_0_missing", text="Ciao", start=0, end=1)
    assert body["success"] is False
    assert "not found" in body["error"]
    assert set(DRAFT_CACHE) == before


def test_missing_draft_id_is_an_error(client):
    body = call(client, "/add_text", text="Ciao", start=0, end=1)
    assert body["success"] is False
    assert "draft_id is required" in body["error"]


def test_draft_survives_a_restart_with_pending_keyframes(client, clip):
    draft_id = new_draft(client)
    assert call(client, "/add_video", draft_id=draft_id, video_url=clip, start=0, end=3)["success"]
    assert call(client, "/add_video_keyframe", draft_id=draft_id, track_name="video_main",
                property_type="alpha", time=1, value="0.5")["success"]

    restart()

    assert call(client, "/add_text", draft_id=draft_id, text="Dopo il riavvio", start=0, end=1)["success"]
    script = DRAFT_CACHE[draft_id]
    assert len(script.tracks["video_main"].segments) == 1
    assert script.tracks["video_main"].pending_keyframes == [{"property_type": "alpha", "time": 1, "value": "0.5"}]
    assert script.materials.videos[0].remote_url == clip


def test_saving_after_a_restart_works(client, clip, tmp_path, monkeypatch):
    import save_draft_impl
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(save_draft_impl, "find_capcut_projects_dir", lambda: str(projects))
    draft_id = new_draft(client)
    assert call(client, "/add_video", draft_id=draft_id, video_url=clip, start=0, end=3)["success"]

    restart()
    body = call(client, "/save_draft", draft_id=draft_id, project_name="Dopo riavvio")

    assert body["success"], body
    content = json.loads((projects / "Dopo riavvio" / "draft_info.json").read_text())
    assert any(t["segments"] for t in content["tracks"])


def test_failed_change_leaves_the_draft_unchanged(client, monkeypatch):
    import capcut_server
    draft_id = new_draft(client)
    assert call(client, "/add_text", draft_id=draft_id, text="Primo", start=0, end=1)["success"]

    real = capcut_server.add_text_impl

    def half_done(**kwargs):
        real(**kwargs)  # the text is added...
        raise RuntimeError("then something fails")
    monkeypatch.setattr(capcut_server, "add_text_impl", half_done)
    body = call(client, "/add_text", draft_id=draft_id, text="Secondo", start=1, end=2)

    assert body["success"] is False
    assert segments(draft_id, "text_main") == 1
    restart()
    draft_store.get_draft(draft_id)
    assert segments(draft_id, "text_main") == 1


def test_failed_save_keeps_pending_keyframes(client, clip, tmp_path, monkeypatch):
    import save_draft_impl
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "Occupato").mkdir()
    monkeypatch.setattr(save_draft_impl, "find_capcut_projects_dir", lambda: str(projects))
    draft_id = new_draft(client)
    assert call(client, "/add_video", draft_id=draft_id, video_url=clip, start=0, end=3)["success"]
    assert call(client, "/add_video_keyframe", draft_id=draft_id, track_name="video_main",
                property_type="alpha", time=1, value="0.5")["success"]

    assert call(client, "/save_draft", draft_id=draft_id, project_name="Occupato")["success"] is False

    assert DRAFT_CACHE[draft_id].tracks["video_main"].pending_keyframes


def test_each_success_bumps_the_stored_revision(client):
    draft_id = new_draft(client)
    conn = draft_store._connect()
    rev = lambda: conn.execute("SELECT revision FROM drafts WHERE draft_id = ?", (draft_id,)).fetchone()[0]
    assert rev() == 1
    call(client, "/add_text", draft_id=draft_id, text="A", start=0, end=1)
    call(client, "/add_text", draft_id=draft_id, text="B", start=0, end=1, intro_animation="Nope")
    assert rev() == 2
    conn.close()


def test_concurrent_changes_to_one_draft_are_all_kept(client):
    draft_id = new_draft(client)
    errors = []

    def writer(track):
        for i in range(15):
            body = call(client, "/add_text", draft_id=draft_id, text=f"{track}{i}", start=i, end=i + 1,
                        track_name=track)
            if not body["success"]:
                errors.append(body["error"])
    threads = [threading.Thread(target=writer, args=(f"t{n}",)) for n in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    restart()
    draft_store.get_draft(draft_id)
    assert [segments(draft_id, f"t{n}") for n in range(3)] == [15, 15, 15]
