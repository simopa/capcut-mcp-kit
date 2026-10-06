# Added in capcut-mcp-kit (2026): regression tests for the second external review — saves that
# race, symbolic links, timeline manifests, SQLite and filesystem kept consistent, retries, media
# versions, backups across volumes, CapCut state unknown. See NOTICE at the repository root.
import errno
import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import draft_store
import save_draft_impl
from test_existing_project import HOST, capcut_content, snapshot, write_project

REAL_CAPCUT_CHECK = save_draft_impl.capcut_is_running  # before the autouse fixture replaces it


@pytest.fixture
def env(monkeypatch, tmp_path):
    import save_draft_impl
    from types import SimpleNamespace
    projects = tmp_path / "projects"
    projects.mkdir()
    state = SimpleNamespace(projects=projects, running=False, backups=tmp_path / "backups", tmp=tmp_path)
    monkeypatch.setattr(save_draft_impl, "find_capcut_projects_dir", lambda: str(projects))
    monkeypatch.setattr(save_draft_impl, "capcut_is_running", lambda: state.running)
    return state


@pytest.fixture
def client():
    import capcut_server
    return capcut_server.app.test_client()


def call(client, route, **payload):
    return client.post(route, json=payload, headers=HOST).get_json()


def restart():
    draft_store.forget_cache()


def texts_in(path):
    content = json.loads(Path(path).read_text())
    return sorted(t["content"] for t in content["materials"].get("texts", []))


def text_count(draft_id):
    script = draft_store.get_draft(draft_id)
    return sum(len(t.segments) for n, t in script.tracks.items() if n.startswith("text"))


def png(path, rgb):
    import numpy as np
    import imageio.v2 as imageio
    imageio.imwrite(path, np.full((8, 8, 3), rgb, dtype=np.uint8))
    return str(path)


# 1. Two drafts of one project, saves interleaved -----------------------------------------------

def test_two_drafts_of_one_project_cannot_both_win(env, client, monkeypatch):
    import save_draft_impl
    root = write_project(env.projects, "Condiviso", capcut_content())
    a = call(client, "/open_project", project_name="Condiviso")["output"]["draft_id"]
    b = call(client, "/open_project", project_name="Condiviso")["output"]["draft_id"]
    call(client, "/add_text", draft_id=a, text="A", start=0, end=1)
    call(client, "/add_text", draft_id=b, text="B", start=1, end=2)

    real_copy, results = save_draft_impl.copy_assets, {}

    def copy_then_let_b_try(*args, **kwargs):
        # A is past its first check and holds the project: B starts now and must wait
        real_copy(*args, **kwargs)
        if "b" not in results:
            results["b"] = None
            t = threading.Thread(target=lambda: results.update(b=call(client, "/save_draft", draft_id=b)))
            t.start()
            t.join(0.5)
            assert t.is_alive(), "B saved while A held the project"
            results["thread"] = t
    monkeypatch.setattr(save_draft_impl, "copy_assets", copy_then_let_b_try)

    first = call(client, "/save_draft", draft_id=a)
    results["thread"].join(10)

    assert first["success"], first
    assert results["b"]["success"] is False and "changed since it was opened" in results["b"]["error"]
    assert len(texts_in(root / "draft_info.json")) == 1


def test_a_folder_that_appears_during_a_save_is_not_replaced(env, client, monkeypatch):
    import save_draft_impl
    d = call(client, "/create_draft", width=1080, height=1920)["output"]["draft_id"]
    call(client, "/add_text", draft_id=d, text="Mio", start=0, end=1)
    real = save_draft_impl.update_media_metadata

    def someone_creates_it(*args, **kwargs):
        (env.projects / "Nuovo").mkdir()
        (env.projects / "Nuovo" / "draft_info.json").write_text('{"made":"elsewhere"}')
        return real(*args, **kwargs)
    monkeypatch.setattr(save_draft_impl, "update_media_metadata", someone_creates_it)

    body = call(client, "/save_draft", draft_id=d, project_name="Nuovo")

    assert body["success"] is False
    assert json.loads((env.projects / "Nuovo" / "draft_info.json").read_text()) == {"made": "elsewhere"}
    assert not [p for p in env.projects.iterdir() if p.name.startswith(".capcut-mcp")]


def test_rename_never_replaces(tmp_path):
    from save_draft_impl import rename_noreplace
    (tmp_path / "src").mkdir()
    (tmp_path / "empty").mkdir()
    with pytest.raises(OSError):
        rename_noreplace(tmp_path / "src", tmp_path / "empty")
    rename_noreplace(tmp_path / "src", tmp_path / "dst")
    assert (tmp_path / "dst").is_dir() and not (tmp_path / "src").exists()


# 2. Symbolic links inside a project -----------------------------------------------------------

def test_timelines_linked_outside_the_project_are_refused(env, client):
    root = write_project(env.projects, "Collegato", capcut_content())
    outside = env.tmp / "outside"
    (root / "Timelines").rename(outside)
    (root / "Timelines").symlink_to(outside)
    before = {p: p.read_bytes() for p in outside.rglob("*") if p.is_file()}

    body = call(client, "/open_project", project_name="Collegato")

    assert body["success"] is False and "symbolic link" in body["error"]
    assert {p: p.read_bytes() for p in outside.rglob("*") if p.is_file()} == before


def test_assets_folder_linked_outside_is_refused_at_save(env, client):
    root = write_project(env.projects, "Asset", capcut_content())
    d = call(client, "/open_project", project_name="Asset")["output"]["draft_id"]
    image = png(env.tmp / "red.png", (255, 0, 0))
    assert call(client, "/add_image", draft_id=d, image_url=image, start=0, end=1)["success"]
    outside = env.tmp / "elsewhere"
    outside.mkdir()
    (root / "assets").symlink_to(outside)
    before = snapshot(root)

    body = call(client, "/save_draft", draft_id=d)

    assert body["success"] is False and "symbolic link" in body["error"]
    assert list(outside.iterdir()) == [] and snapshot(root) == before


# 3. The draft and the reply to its request_id are one commit -----------------------------------

def test_reply_log_failure_leaves_the_draft_unchanged_on_disk(env, client, monkeypatch):
    d = call(client, "/create_draft", width=1080, height=1920)["output"]["draft_id"]

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")
    with monkeypatch.context() as m:
        m.setattr(draft_store, "_remember", broken)
        body = call(client, "/add_text", draft_id=d, text="Una", start=0, end=1, request_id="r-1")
    assert body["success"] is False and "left unchanged" in body["error"]
    assert text_count(d) == 0
    restart()
    assert text_count(d) == 0

    call(client, "/add_text", draft_id=d, text="Una", start=0, end=1, request_id="r-1")
    call(client, "/add_text", draft_id=d, text="Una", start=0, end=1, request_id="r-1")
    restart()
    assert text_count(d) == 1


# 4. A save that wrote the project but could not be recorded ------------------------------------

def test_project_written_but_not_recorded_is_reconciled(env, client, monkeypatch):
    root = write_project(env.projects, "Registro", capcut_content())
    d = call(client, "/open_project", project_name="Registro")["output"]["draft_id"]
    call(client, "/add_text", draft_id=d, text="Uno", start=0, end=1)

    real_commit = draft_store._commit

    def broken(conn, draft_id, *args):
        if draft_id == d:
            raise sqlite3.OperationalError("disk full")
        return real_commit(conn, draft_id, *args)
    with monkeypatch.context() as m:
        m.setattr(draft_store, "_commit", broken)
        body = call(client, "/save_draft", draft_id=d)

    assert body["success"] is False and "WAS written" in body["error"]
    assert len(texts_in(root / "draft_info.json")) == 1
    restart()  # also across a restart
    call(client, "/add_text", draft_id=d, text="Due", start=1, end=2, track_name="t2")
    again = call(client, "/save_draft", draft_id=d)
    assert again["success"], again
    assert len(texts_in(root / "draft_info.json")) == 2
    assert draft_store.journal_pending() == []


def test_failed_write_into_existing_project_removes_added_media(env, client, monkeypatch):
    import existing_project
    root = write_project(env.projects, "Rotto", capcut_content())
    d = call(client, "/open_project", project_name="Rotto")["output"]["draft_id"]
    assert call(client, "/add_image", draft_id=d, image_url=png(env.tmp / "a.png", (0, 255, 0)), start=0, end=1)["success"]
    before = snapshot(root)
    real, calls = existing_project._write_atomic, []

    def fail_second(path, data):
        calls.append(path)
        if len(calls) == 2:
            raise OSError(errno.EIO, "I/O error")
        return real(path, data)
    monkeypatch.setattr(existing_project, "_write_atomic", fail_second)

    body = call(client, "/save_draft", draft_id=d)

    assert body["success"] is False
    assert snapshot(root) == before
    assert not (root / "assets").exists() or not any((root / "assets").rglob("*.png"))
    assert draft_store.journal_pending() == []


def test_draft_folder_and_capcut_copy_are_saved_together(env, client, monkeypatch, tmp_path):
    import save_draft_impl
    d = call(client, "/create_draft", width=1080, height=1920)["output"]["draft_id"]
    call(client, "/add_text", draft_id=d, text="X", start=0, end=1)
    elsewhere = tmp_path / "mine"
    elsewhere.mkdir()
    real, calls = save_draft_impl.swap_in, []

    def deploy_fails(pair, content_file):
        calls.append(pair["target"])
        if len(calls) == 2:
            raise OSError(errno.ENOSPC, "No space left")
        return real(pair, content_file)
    monkeypatch.setattr(save_draft_impl, "swap_in", deploy_fails)

    body = call(client, "/save_draft", draft_id=d, draft_folder=str(elsewhere), project_name="Coppia")

    assert body["success"] is False
    assert list(elsewhere.iterdir()) == [] and not (env.projects / "Coppia").exists()


# 5. Two processes, one draft ------------------------------------------------------------------

def test_another_process_change_is_not_lost(env, client):
    d = call(client, "/create_draft", width=1080, height=1920)["output"]["draft_id"]
    assert call(client, "/add_text", draft_id=d, text="A", start=0, end=1)["success"]
    script = f"""
import sys; sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
import capcut_server
c = capcut_server.app.test_client()
r = c.post("/add_text", json={{"draft_id": {d!r}, "text": "B", "start": 2, "end": 3, "track_name": "text_2"}},
           headers={HOST!r}).get_json()
assert r["success"], r
"""
    subprocess.run([sys.executable, "-c", script], check=True, env=os.environ.copy(), capture_output=True)
    rev = draft_store.revision(d)
    body = call(client, "/add_text", draft_id=d, text="C", start=4, end=5, track_name="text_3", expected_revision=rev)
    assert body["success"], body
    restart()
    assert text_count(d) == 3


# 6. Timeline manifest ------------------------------------------------------------------------

def test_a_disagreeing_mirror_is_refused(env, client):
    root = write_project(env.projects, "Specchio", capcut_content())
    (root / "Timelines" / "TL-1" / "template-2.tmp").write_text("{}")
    body = call(client, "/open_project", project_name="Specchio")
    assert body["success"] is False and "disagree" in body["error"]


def test_a_selector_naming_no_timeline_is_refused(env, client):
    root = write_project(env.projects, "Selettore", capcut_content())
    (root / "Timelines" / "project.json").write_text(json.dumps({"main_timeline_id": "TL-404"}))
    body = call(client, "/open_project", project_name="Selettore")
    assert body["success"] is False and "does not exist" in body["error"]


def test_switching_the_main_timeline_after_opening_is_refused(env, client):
    root = write_project(env.projects, "Cambio", capcut_content())
    d = call(client, "/open_project", project_name="Cambio")["output"]["draft_id"]
    call(client, "/add_text", draft_id=d, text="X", start=0, end=1)
    (root / "Timelines" / "TL-2").mkdir()
    for name in ("draft_info.json", "template-2.tmp"):
        (root / "Timelines" / "TL-2" / name).write_bytes((root / "draft_info.json").read_bytes())
    (root / "Timelines" / "project.json").write_text(json.dumps({"main_timeline_id": "TL-2"}))
    before = snapshot(root)
    body = call(client, "/save_draft", draft_id=d)
    assert body["success"] is False and "changed since it was opened" in body["error"]
    assert snapshot(root) == before


# 7. Media inside the project being replaced -------------------------------------------------------

def test_media_inside_a_replaced_project_is_copied(env, client, monkeypatch, tmp_path):
    home = tmp_path / "home"
    projects = home / "Movies" / "CapCut" / "drafts"
    projects.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    import save_draft_impl
    monkeypatch.setattr(save_draft_impl, "find_capcut_projects_dir", lambda: str(projects))
    old = projects / "Vecchio"
    (old / "assets").mkdir(parents=True)
    (old / "draft_info.json").write_text("{}")
    image = png(old / "assets" / "logo.png", (0, 0, 255))
    d = call(client, "/create_draft", width=1080, height=1920)["output"]["draft_id"]
    assert call(client, "/add_image", draft_id=d, image_url=image, start=0, end=1)["success"]

    body = call(client, "/save_draft", draft_id=d, project_name="Vecchio", overwrite=True)

    assert body["success"], body
    content = json.loads((projects / "Vecchio" / "draft_info.json").read_text())
    paths = [m["path"] for m in content["materials"]["videos"]]
    assert paths and all(Path(p).is_file() for p in paths)
    assert all(str(projects / "Vecchio") in p for p in paths)


# 9. Backups on another volume ------------------------------------------------------------------

def test_backup_on_another_volume_keeps_the_new_project_whole(env, client, monkeypatch):
    import save_draft_impl
    d = call(client, "/create_draft", width=1080, height=1920)["output"]["draft_id"]
    call(client, "/add_text", draft_id=d, text="Uno", start=0, end=1)
    assert call(client, "/save_draft", draft_id=d, project_name="Volume")["success"]
    call(client, "/add_text", draft_id=d, text="Due", start=1, end=2, track_name="t2")

    real_rename, real_rmtree = os.rename, save_draft_impl.shutil.rmtree

    def rename(src, dst, *a, **k):
        if str(dst).startswith(str(env.backups)) and not str(dst).endswith(".partial"):
            if not str(src).endswith(".partial"):
                raise OSError(errno.EXDEV, "Cross-device link")
        return real_rename(src, dst, *a, **k)

    def rmtree(path, *a, **k):
        if ".capcut-mcp-old-" in str(path) and not k.get("ignore_errors"):
            raise OSError(errno.EACCES, "Permission denied")
        return real_rmtree(path, *a, **k)
    with monkeypatch.context() as m:
        m.setattr(save_draft_impl.os, "rename", rename)
        m.setattr(save_draft_impl.shutil, "rmtree", rmtree)
        body = call(client, "/save_draft", draft_id=d, project_name="Volume")

    assert body["success"], body
    assert len(texts_in(env.projects / "Volume" / "draft_info.json")) == 2
    backup = Path(body["output"]["backups"][0])
    assert len(texts_in(backup / "draft_info.json")) == 1
    assert "could not be moved" in body["output"]["warnings"][0] or "could not be removed" in body["output"]["warnings"][0]


# 10. Retries ------------------------------------------------------------------------------

def test_create_and_open_with_the_same_request_id_start_one_draft(env, client):
    first = call(client, "/create_draft", width=1080, height=1920, request_id="c-1")
    again = call(client, "/create_draft", width=1080, height=1920, request_id="c-1")
    assert first["output"]["draft_id"] == again["output"]["draft_id"]
    write_project(env.projects, "Riprova", capcut_content())
    o1 = call(client, "/open_project", project_name="Riprova", request_id="o-1")
    o2 = call(client, "/open_project", project_name="Riprova", request_id="o-1")
    assert o1["output"]["draft_id"] == o2["output"]["draft_id"]


def test_a_request_id_reused_for_another_call_is_refused(env, client):
    d = call(client, "/create_draft", width=1080, height=1920)["output"]["draft_id"]
    assert call(client, "/add_text", draft_id=d, text="A", start=0, end=1, request_id="x-1")["success"]
    body = call(client, "/save_draft", draft_id=d, project_name="Mai", request_id="x-1")
    assert body["success"] is False and "different call" in body["error"]
    assert not (env.projects / "Mai").exists()


def test_an_expired_request_id_is_refused_not_applied_again(env, client):
    d = call(client, "/create_draft", width=1080, height=1920)["output"]["draft_id"]
    assert call(client, "/add_text", draft_id=d, text="A", start=0, end=1, request_id="old")["success"]
    with draft_store._db() as conn, conn:
        conn.execute("UPDATE request_log SET created_at = ?", (time.time() - 30 * 86400,))
    call(client, "/add_text", draft_id=d, text="B", start=2, end=3, request_id="new")  # prunes old replies
    body = call(client, "/add_text", draft_id=d, text="A", start=0, end=1, request_id="old")
    assert body["success"] is False and "no longer kept" in body["error"]
    assert text_count(d) == 2


# 11. A media file replaced at the same path ------------------------------------------------------

def test_a_replaced_source_does_not_change_clips_already_saved(env, client):
    root = write_project(env.projects, "Colori", capcut_content())
    d = call(client, "/open_project", project_name="Colori")["output"]["draft_id"]
    source = env.tmp / "logo.png"
    png(source, (255, 0, 0))
    assert call(client, "/add_image", draft_id=d, image_url=str(source), start=0, end=1)["success"]
    assert call(client, "/save_draft", draft_id=d)["success"]
    red = source.read_bytes()
    time.sleep(0.01)
    png(source, (0, 0, 255))
    os.utime(source, ns=(time.time_ns(), time.time_ns() + 1_000_000))
    assert call(client, "/add_image", draft_id=d, image_url=str(source), start=1, end=2, track_name="i2")["success"]
    body = call(client, "/save_draft", draft_id=d)
    assert body["success"], body

    content = json.loads((root / "draft_info.json").read_text())
    added = [m["path"] for m in content["materials"]["videos"] if m.get("type") == "photo"]
    assert len(added) == 2 and len(set(added)) == 2
    assert sorted(Path(p).read_bytes() == red for p in added) == [False, True]


def test_a_source_changed_after_it_was_added_is_reported(env, client):
    d = call(client, "/create_draft", width=1080, height=1920)["output"]["draft_id"]
    source = env.tmp / "late.png"
    png(source, (255, 0, 0))
    assert call(client, "/add_image", draft_id=d, image_url=str(source), start=0, end=1)["success"]
    png(source, (0, 0, 255))
    os.utime(source, ns=(time.time_ns(), time.time_ns() + 1_000_000))
    body = call(client, "/save_draft", draft_id=d, project_name="Tardi")
    assert body["success"] is False and "add it again" in body["error"]


# 12. The marker in the folder authorizes nothing ------------------------------------------------

def test_marker_without_hash_does_not_allow_replacing_an_edit(env, client):
    d = call(client, "/create_draft", width=1080, height=1920)["output"]["draft_id"]
    call(client, "/add_text", draft_id=d, text="A", start=0, end=1)
    assert call(client, "/save_draft", draft_id=d, project_name="Segno")["success"]
    root = env.projects / "Segno"
    (root / ".capcut_mcp_kit.json").write_text(json.dumps({"draft_id": d}))
    (root / "draft_info.json").write_text('{"edited":true}')
    body = call(client, "/save_draft", draft_id=d, project_name="Segno")
    assert body["success"] is False
    assert json.loads((root / "draft_info.json").read_text()) == {"edited": True}


def test_a_forged_marker_does_not_allow_replacing(env, client):
    d = call(client, "/create_draft", width=1080, height=1920)["output"]["draft_id"]
    root = env.projects / "Altrui"
    root.mkdir()
    (root / "draft_info.json").write_text('{"theirs":true}')
    (root / ".capcut_mcp_kit.json").write_text(json.dumps({"draft_id": d, "content_sha": "x"}))
    body = call(client, "/save_draft", draft_id=d, project_name="Altrui")
    assert body["success"] is False and "not saved from this draft" in body["error"]


# 15. CapCut state unknown --------------------------------------------------------------------

def test_unknown_capcut_state_blocks_writing_projects(env, client, monkeypatch):
    import save_draft_impl
    monkeypatch.setattr(save_draft_impl, "capcut_is_running", lambda: None)
    root = write_project(env.projects, "Incerto", capcut_content())
    d = call(client, "/open_project", project_name="Incerto")["output"]["draft_id"]
    call(client, "/add_text", draft_id=d, text="X", start=0, end=1)
    before = snapshot(root)
    body = call(client, "/save_draft", draft_id=d)
    assert body["success"] is False and "Could not tell whether CapCut is open" in body["error"]
    assert snapshot(root) == before


def test_pgrep_errors_are_not_read_as_closed(monkeypatch):
    from types import SimpleNamespace
    import save_draft_impl
    for rc, expected in ((0, True), (1, False), (2, None), (3, None)):
        monkeypatch.setattr(save_draft_impl.subprocess, "run",
                            lambda *a, rc=rc, **k: SimpleNamespace(returncode=rc, stdout=""))
        assert REAL_CAPCUT_CHECK() is expected


# Store ---------------------------------------------------------------------------------------

@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_state_is_private(client):
    old = os.umask(0o022)
    try:
        call(client, "/create_draft", width=1080, height=1920)
    finally:
        os.umask(old)
    folder = Path(draft_store.state_dir())
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE((folder / "drafts.sqlite3").stat().st_mode) == 0o600
