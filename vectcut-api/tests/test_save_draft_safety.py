# Added in capcut-mcp-kit (2026): saving never deletes or escapes the drafts folder, and failures are reported.
# See NOTICE at the repository root.
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A fake CapCut drafts folder and backup folder under tmp_path; CapCut reported closed."""
    import save_draft_impl
    from draft_profiles import get_draft_profile

    root = tmp_path / "User Data"
    projects = root / "Projects" / "com.lveditor.draft"
    projects.mkdir(parents=True)
    (root / "Projects" / "sentinel.txt").write_text("keep")
    backups = tmp_path / "backups"
    state = SimpleNamespace(projects=projects, backups=backups, running=False, root=root)

    monkeypatch.setattr(save_draft_impl, "find_capcut_projects_dir", lambda: str(projects))
    monkeypatch.setattr(save_draft_impl, "capcut_is_running", lambda: state.running)
    monkeypatch.setattr(save_draft_impl, "get_draft_profile", lambda: get_draft_profile("capcut_legacy"))
    monkeypatch.setattr(save_draft_impl, "update_media_metadata", lambda script, task_id=None: None)
    monkeypatch.setattr(save_draft_impl, "IS_UPLOAD_DRAFT", False)
    monkeypatch.setenv("CAPCUT_MCP_BACKUP_DIR", str(backups))
    return state


def make_draft(draft_id, videos=(), marker="v1"):
    from draft_cache import DRAFT_CACHE

    payload = {"tracks": [], "materials": {}, "duration": 0, "marker": marker}
    script = SimpleNamespace(
        materials=SimpleNamespace(audios=[], videos=list(videos)),
        duration=10_000_000,
        dumps=lambda profile=None: json.dumps(payload),
    )
    DRAFT_CACHE[draft_id] = script
    return payload


def save(draft_id, **kw):
    import save_draft_impl
    return save_draft_impl.save_draft_impl(draft_id, None, **kw)


def no_staging_left(folder):
    return not [p for p in Path(folder).iterdir() if p.name.startswith(".capcut-mcp-saving-")]


@pytest.mark.parametrize("name", ["..", ".", "../x", "a/b", "a\\b", "a:b", ".hidden", "   ", "a\nb"])
def test_unsafe_project_names_are_refused(env, name):
    make_draft("safety-names")
    before = sorted(p.name for p in env.projects.parent.iterdir())

    result = save("safety-names", project_name=name)

    assert result["success"] is False
    assert sorted(p.name for p in env.projects.parent.iterdir()) == before
    assert (env.projects.parent / "sentinel.txt").read_text() == "keep"
    assert list(env.projects.iterdir()) == []


def test_new_project_is_saved_with_marker_and_meta(env):
    make_draft("safety-new")

    result = save("safety-new", project_name="Mio video")

    target = env.projects / "Mio video"
    assert result == {"success": True, "draft_url": str(target), "backups": []}
    assert json.loads((target / ".capcut_mcp_kit.json").read_text()) == {"draft_id": "safety-new"}
    meta = json.loads((target / "draft_meta_info.json").read_text())
    assert meta["draft_name"] == "Mio video"
    assert meta["draft_fold_path"] == str(target)
    assert no_staging_left(env.projects)


def test_resaving_moves_previous_version_to_backup_and_keeps_identity(env):
    payload = make_draft("safety-resave")
    save("safety-resave", project_name="Video")
    first_meta = json.loads((env.projects / "Video" / "draft_meta_info.json").read_text())

    payload["marker"] = "v2"
    result = save("safety-resave", project_name="Video")

    assert result["success"] is True
    assert len(result["backups"]) == 1
    backup = Path(result["backups"][0])
    assert backup.parent == env.backups
    assert json.loads((backup / "draft_info.json").read_text())["marker"] == "v1"
    assert json.loads((env.projects / "Video" / "draft_info.json").read_text())["marker"] == "v2"
    meta = json.loads((env.projects / "Video" / "draft_meta_info.json").read_text())
    assert meta["draft_id"] == first_meta["draft_id"]
    assert meta["tm_draft_create"] == first_meta["tm_draft_create"]


def test_foreign_project_needs_overwrite_and_is_backed_up(env):
    existing = env.projects / "Altro"
    existing.mkdir()
    (existing / "draft_info.json").write_text("user work")
    make_draft("safety-foreign")

    refused = save("safety-foreign", project_name="Altro")
    assert refused["success"] is False
    assert "overwrite" in refused["error"]
    assert (existing / "draft_info.json").read_text() == "user work"
    assert no_staging_left(env.projects)

    replaced = save("safety-foreign", project_name="Altro", overwrite=True)
    assert replaced["success"] is True
    assert (Path(replaced["backups"][0]) / "draft_info.json").read_text() == "user work"


def test_another_draft_cannot_replace_a_kit_project_without_overwrite(env):
    make_draft("safety-owner")
    save("safety-owner", project_name="Condiviso")
    make_draft("safety-intruder", marker="intruder")

    result = save("safety-intruder", project_name="Condiviso")

    assert result["success"] is False
    assert json.loads((env.projects / "Condiviso" / "draft_info.json").read_text())["marker"] == "v1"


def test_replacing_is_refused_while_capcut_is_open(env):
    make_draft("safety-open")
    save("safety-open", project_name="Aperto")
    env.running = True

    refused = save("safety-open", project_name="Aperto")
    assert refused["success"] is False
    assert "CapCut is open" in refused["error"]
    assert not env.backups.exists()

    # A new project is harmless: CapCut only lists it after a restart
    assert save("safety-open", project_name="Nuovo")["success"] is True


def test_failed_copy_is_a_failure_and_replaces_nothing(env, monkeypatch, tmp_path):
    import save_draft_impl

    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    video = SimpleNamespace(remote_url=str(source), material_name="clip.mp4",
                            material_type="video", replace_path=None)
    payload = make_draft("safety-copy", videos=[video])
    save("safety-copy", project_name="Copia")

    def broken_copy(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(save_draft_impl, "download_file", broken_copy)
    payload["marker"] = "v2"
    result = save("safety-copy", project_name="Copia")

    assert result["success"] is False
    assert "clip.mp4" in result["error"]
    assert json.loads((env.projects / "Copia" / "draft_info.json").read_text())["marker"] == "v1"
    assert (env.projects / "Copia" / "assets" / "video" / "clip.mp4").exists()
    assert no_staging_left(env.projects)
    assert not env.backups.exists()


def test_copied_asset_paths_point_at_the_final_folder(env, tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    video = SimpleNamespace(remote_url=str(source), material_name="clip.mp4",
                            material_type="video", replace_path=None)
    make_draft("safety-paths", videos=[video])

    assert save("safety-paths", project_name="Percorsi")["success"] is True
    assert video.replace_path == str(env.projects / "Percorsi" / "assets" / "video" / "clip.mp4")
    assert Path(video.replace_path).read_bytes() == b"video"


def test_unknown_draft_is_a_failure(env):
    result = save("safety-does-not-exist", project_name="X")
    assert result["success"] is False
    assert "not found" in result["error"]
    assert list(env.projects.iterdir()) == []


@pytest.fixture
def client():
    import capcut_server
    return capcut_server.app.test_client()


def test_foreign_host_and_origin_are_refused(client):
    assert client.get("/camera_moves", headers={"Host": "evil.example:9001"}).status_code == 403
    assert client.get("/camera_moves", headers={"Host": "127.0.0.1:9001",
                                                "Origin": "https://evil.example"}).status_code == 403
    assert client.get("/camera_moves", headers={"Host": "localhost:9001"}).status_code == 200
    assert client.get("/camera_moves", headers={"Host": "[::1]:9001"}).status_code == 200


def test_preview_routes_are_off_by_default(client):
    response = client.get("/preview/media", query_string={"path": __file__},
                          headers={"Host": "127.0.0.1:9001"})
    assert response.status_code == 404


def test_save_route_reports_failure(env, client):
    response = client.post("/save_draft", json={"draft_id": "route-missing", "project_name": ".."},
                           headers={"Host": "127.0.0.1:9001"})
    body = response.get_json()
    assert body["success"] is False
    assert body["error"]
