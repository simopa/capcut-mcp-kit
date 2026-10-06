# Added in capcut-mcp-kit (2026): regression tests for the third external review — a reader cannot
# bring back an older draft, a create/open leaves nothing behind on failure, saves journal their whole
# plan and a crash at any step ends in the old or the new version (never in something else, never
# over what someone else changed since), media identified by content. See NOTICE at the repository root.
import json
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import draft_store
import existing_project
import save_draft_impl as sd
from test_commit_safety import call, client, env, png, restart, texts_in  # noqa: F401 (fixtures)
from test_existing_project import COPIES, HOST, capcut_content, write_project

CRASHED = 73


def hidden(folder):
    return sorted(p.name for p in Path(folder).iterdir() if p.name.startswith(".capcut-mcp-"))


def text_values(path):
    return sorted(json.loads(t["content"])["text"] for t in json.loads(Path(path).read_text())["materials"]["texts"])


# Crash in a child process after the n-th step of a save. The steps are every journal write, the
# draft commit and every rename or file replacement: os._exit leaves things exactly as they are.
CHILD = r'''
import os, sys, json
import draft_store, save_draft_impl as sd, existing_project as ep
sd.find_capcut_projects_dir = lambda: {projects!r}
sd.capcut_is_running = lambda: False
crash_after, count = {crash_after}, [0]
def step(fn):
    def wrapped(*a, **kw):
        out = fn(*a, **kw)
        count[0] += 1
        if count[0] == crash_after:
            os._exit({crashed})
        return out
    return wrapped
for module in (sd, ep):
    module.journal_begin = step(module.journal_begin)
    module.journal_update = step(module.journal_update)
draft_store._commit_change = step(draft_store._commit_change)
sd.rename_noreplace = step(sd.rename_noreplace)
os.rename = step(os.rename)
real_replace = os.replace
def replace(*a, **kw):  # a step before (temporary file written, not yet in place) and after
    count[0] += 1
    if count[0] == crash_after:
        os._exit({crashed})
    return step(real_replace)(*a, **kw)
os.replace = replace
if {exdev!r}:  # the backups are on another volume
    import errno
    real_rename = sd.rename_noreplace
    def cross(src, dst, *a, **kw):
        if os.path.basename(str(src)).startswith(".capcut-mcp-old-") and not os.path.basename(str(dst)).startswith("."):
            raise OSError(errno.EXDEV, "Cross-device link")
        return real_rename(src, dst, *a, **kw)
    sd.rename_noreplace = cross
    real_clone = sd.clone_tree
    def clone(src, dst):  # a step in the middle of the copy
        os.makedirs(dst)
        with open(os.path.join(src, "draft_info.json"), "rb") as source:
            with open(os.path.join(dst, "draft_info.json"), "wb") as partial:
                partial.write(source.read(32))
        count[0] += 1
        if count[0] == crash_after:
            os._exit({crashed})
        shutil.rmtree(dst)
        real_clone(src, dst)
    import shutil
    sd.clone_tree = clone
import capcut_server
r = capcut_server.app.test_client().post('/save_draft', json={payload!r}, headers={host!r})
print(json.dumps(r.get_json()))
'''


def crash_save(env, crash_after, exdev=False, **payload):
    """Run a save in a child process that dies after step `crash_after`. Returns True if it died,
    False if the save finished first."""
    src = CHILD.format(projects=str(env.projects), crash_after=crash_after, crashed=CRASHED,
                       payload=payload, host=HOST, exdev=exdev)
    done = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, timeout=60,
                          cwd=os.path.dirname(sd.__file__), env=os.environ.copy())
    if done.returncode == CRASHED:
        return True
    assert done.returncode == 0, done.stderr[-3000:]
    assert json.loads(done.stdout.strip().splitlines()[-1])["success"], done.stdout
    return False


def settle_after_restart():
    restart()
    return sd.recover_saves()


# --- 1. A reader cannot put an older draft back in the cache --------------------------------------

def test_a_slow_reader_cannot_bring_back_an_older_draft(env, client, monkeypatch):
    d = call(client, "/create_draft", width=1080, height=1920)["output"]["draft_id"]
    assert call(client, "/add_text", draft_id=d, text="A", start=0, end=1)["success"]
    before = draft_store.revision(d)
    draft_store.forget_cache()  # the reader loads from SQLite
    loaded, go_on = threading.Event(), threading.Event()
    real = draft_store._install

    def slow_reader(draft_id, rev, script):
        if threading.current_thread().name == "reader":
            loaded.set()
            assert go_on.wait(10)
        return real(draft_id, rev, script)
    monkeypatch.setattr(draft_store, "_install", slow_reader)
    replies = []
    reader = threading.Thread(target=lambda: replies.append(call(client, "/timeline", draft_id=d)), name="reader")
    reader.start()
    assert loaded.wait(5)
    assert call(client, "/add_text", draft_id=d, text="B", start=1, end=2, expected_revision=before)["success"]
    go_on.set()
    reader.join(10)

    assert replies[0]["success"] and replies[0]["output"]["revision"] == before  # what it read, consistently
    assert call(client, "/add_text", draft_id=d, text="C", start=2, end=3, expected_revision=before + 1)["success"]
    restart()
    texts = sorted(json.loads(m["content"])["text"]
                   for m in json.loads(draft_store.get_draft(d).dumps())["materials"]["texts"])
    assert texts == ["A", "B", "C"]


# --- 9. Create/open: the draft and the reply in one transaction ----------------------------------

def test_a_create_that_cannot_record_its_reply_leaves_no_draft(env, client, monkeypatch):
    def drafts():
        with draft_store._db() as conn:
            return conn.execute("select count(*) from drafts").fetchone()[0]
    with monkeypatch.context() as m:
        def fail(*a, **k):
            raise sqlite3.OperationalError("injected failure")
        m.setattr(draft_store, "_remember", fail)
        out = call(client, "/create_draft", width=1080, height=1920, request_id="once")
    assert not out["success"] and drafts() == 0
    assert call(client, "/create_draft", width=1080, height=1920, request_id="once")["success"]
    assert call(client, "/create_draft", width=1080, height=1920, request_id="once")["success"]
    assert drafts() == 1


def test_an_open_that_cannot_record_its_reply_leaves_no_draft(env, client, monkeypatch):
    write_project(env.projects, "Aperto", capcut_content())
    with monkeypatch.context() as m:
        m.setattr(draft_store, "_remember", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("x")))
        out = call(client, "/open_project", project_name="Aperto", request_id="o1")
    assert not out["success"]
    with draft_store._db() as conn:
        assert conn.execute("select count(*) from drafts").fetchone()[0] == 0


# --- 7. A folder that appears after the check is never replaced ----------------------------------

def test_a_folder_appearing_after_the_check_is_left_alone(env, client, monkeypatch):
    d = call(client, "/create_draft", width=1080, height=1920)["output"]["draft_id"]
    call(client, "/add_text", draft_id=d, text="Mio", start=0, end=1)
    real = sd.swap_in

    def appears(pair, content_file):
        Path(pair["target"]).mkdir()
        (Path(pair["target"]) / "foreign.txt").write_text("mine")
        return real(pair, content_file)
    monkeypatch.setattr(sd, "swap_in", appears)

    out = call(client, "/save_draft", draft_id=d, project_name="Tardi")
    assert not out["success"] and "appeared" in out["error"]
    assert sorted(p.name for p in (env.projects / "Tardi").iterdir()) == ["foreign.txt"]
    assert not env.backups.exists() or not any(env.backups.iterdir())
    assert hidden(env.projects) == [] and draft_store.journal_pending() == []


# --- 8. Media identified by content --------------------------------------------------------------

def test_a_replaced_source_with_same_size_and_time_is_a_new_material(env, client):
    root = write_project(env.projects, "Stessa", capcut_content())
    d = call(client, "/open_project", project_name="Stessa")["output"]["draft_id"]
    source = env.tmp / "logo.bmp"
    png(source, (255, 0, 0))
    red, stat = source.read_bytes(), source.stat()
    assert call(client, "/add_image", draft_id=d, image_url=str(source), start=0, end=1)["success"]
    assert call(client, "/save_draft", draft_id=d)["success"]
    png(source, (0, 0, 255))
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert source.stat().st_size == stat.st_size and source.read_bytes() != red
    assert call(client, "/add_image", draft_id=d, image_url=str(source), start=1, end=2, track_name="altro")["success"]

    out = call(client, "/save_draft", draft_id=d)
    assert out["success"], out
    photos = [m for m in json.loads((root / "draft_info.json").read_text())["materials"]["videos"] if m.get("type") == "photo"]
    assert sorted(Path(p["path"]).read_bytes() == red for p in photos) == [False, True]


def test_a_source_edited_between_adding_and_saving_is_caught_on_the_copy(env, client):
    d = call(client, "/create_draft", width=1080, height=1920)["output"]["draft_id"]
    source = env.tmp / "logo.bmp"
    png(source, (255, 0, 0))
    stat = source.stat()
    assert call(client, "/add_image", draft_id=d, image_url=str(source), start=0, end=1)["success"]
    png(source, (0, 0, 255))
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    out = call(client, "/save_draft", draft_id=d, project_name="Cambiata")
    assert not out["success"] and "add it again" in out["error"]
    assert not (env.projects / "Cambiata").exists() and hidden(env.projects) == []


# --- 5. A save written but not recorded keeps its media mapping ----------------------------------

def test_reconciling_restores_which_media_the_project_holds(env, client, monkeypatch):
    root = write_project(env.projects, "Congelata", capcut_content())
    d = call(client, "/open_project", project_name="Congelata")["output"]["draft_id"]
    source = env.tmp / "changing.png"
    png(source, (255, 0, 0))
    assert call(client, "/add_image", draft_id=d, image_url=str(source), start=0, end=1)["success"]
    with monkeypatch.context() as m:
        def fail(*a, **k):
            raise sqlite3.OperationalError("injected failure")
        m.setattr(draft_store, "_commit", fail)
        out = call(client, "/save_draft", draft_id=d)
    assert not out["success"] and "WAS written" in out["error"]
    png(source, (0, 0, 255))
    os.utime(source, ns=(1_800_000_000_000_000_000, 1_800_000_000_000_000_000))
    restart()

    call(client, "/add_text", draft_id=d, text="Dopo", start=1, end=2)
    out = call(client, "/save_draft", draft_id=d)
    assert out["success"], out
    assert draft_store.journal_pending() == []
    photos = [m for m in json.loads((root / "draft_info.json").read_text())["materials"]["videos"] if m.get("type") == "photo"]
    assert len(photos) == 1 and Path(photos[0]["path"]).read_bytes() != source.read_bytes()


# --- 2-6. A crash at any step ends in the old or the new version ---------------------------------

@pytest.mark.parametrize("exdev", [False, True], ids=["same-volume", "backups-on-another-volume"])
def test_a_crash_at_any_step_of_replacing_a_project_ends_old_or_new(env, client, exdev):
    outcomes = set()
    for n in range(1, 40):
        d = call(client, "/create_draft", width=1080, height=1920)["output"]["draft_id"]
        call(client, "/add_text", draft_id=d, text="Old", start=0, end=1)
        name = f"P{n}"
        assert call(client, "/save_draft", draft_id=d, project_name=name)["success"]
        call(client, "/add_text", draft_id=d, text="New", start=1, end=2, track_name="text_2")
        if not crash_save(env, n, exdev=exdev, draft_id=d, project_name=name):
            break
        notes = settle_after_restart()

        assert notes == [], (n, notes)
        assert draft_store.journal_pending() == [], n
        assert call(client, "/timeline", draft_id=d)["output"]["unsettled_saves"] == [], n
        assert hidden(env.projects) == [], (n, hidden(env.projects))
        assert not list(env.backups.glob("*.partial")), n
        texts = text_values(env.projects / name / "draft_info.json")
        assert texts in (["Old"], ["New", "Old"]), (n, texts)
        outcomes.add(len(texts))
        # The draft is consistent afterwards: saving again needs no overwrite
        out = call(client, "/save_draft", draft_id=d, project_name=name)
        assert out["success"], (n, out)
        assert text_values(env.projects / name / "draft_info.json") == ["New", "Old"]
    else:
        pytest.fail("the save never finished")
    assert outcomes == {1, 2}


def test_a_crash_with_a_draft_folder_leaves_both_copies_at_one_version(env, client):
    elsewhere = env.tmp / "mine"
    elsewhere.mkdir()
    outcomes = set()
    for n in range(1, 60):
        d = call(client, "/create_draft", width=1080, height=1920)["output"]["draft_id"]
        call(client, "/add_text", draft_id=d, text="Old", start=0, end=1)
        name = f"C{n}"
        assert call(client, "/save_draft", draft_id=d, project_name=name, draft_folder=str(elsewhere))["success"]
        call(client, "/add_text", draft_id=d, text="New", start=1, end=2, track_name="text_2")
        if not crash_save(env, n, draft_id=d, project_name=name, draft_folder=str(elsewhere)):
            break
        assert settle_after_restart() == [], n
        assert draft_store.journal_pending() == [] and hidden(env.projects) == [] and hidden(elsewhere) == [], n
        both = [text_values(elsewhere / d / "draft_info.json"), text_values(env.projects / name / "draft_info.json")]
        assert both[0] == both[1] and both[0] in (["Old"], ["New", "Old"]), (n, both)
        outcomes.add(len(both[0]))
    else:
        pytest.fail("the save never finished")
    assert outcomes == {1, 2}


def test_a_crash_at_any_step_of_saving_into_a_project_ends_old_or_new(env, client):
    outcomes = set()
    image = png(env.tmp / "verde.png", (0, 255, 0))
    for n in range(1, 40):
        name = f"E{n}"
        root = write_project(env.projects, name, capcut_content())
        before = (root / COPIES[0]).read_bytes()
        d = call(client, "/open_project", project_name=name)["output"]["draft_id"]
        assert call(client, "/add_image", draft_id=d, image_url=image, start=0, end=1)["success"]
        assert call(client, "/add_text", draft_id=d, text="Lungo", start=0, end=10)["success"]
        if not crash_save(env, n, draft_id=d):
            break
        notes = settle_after_restart()

        assert notes == [], (n, notes)
        assert draft_store.journal_pending() == [], n
        assert hidden(env.projects) == [], (n, hidden(env.projects))
        assert not list(root.rglob(".capcut-mcp-*")), (n, list(root.rglob(".capcut-mcp-*")))
        copies = {(root / rel).read_bytes() for rel in COPIES}
        assert len(copies) == 1, n
        meta = json.loads((root / "draft_meta_info.json").read_text())["tm_duration"]
        added = list((root / "assets").rglob("*.png")) if (root / "assets").exists() else []
        if copies == {before}:
            assert meta == 4_000_000 and added == [], n
            outcomes.add("old")
        else:
            assert json.loads(copies.pop())["duration"] == 10_000_000 and meta == 10_000_000 and len(added) == 1, n
            outcomes.add("new")
        out = call(client, "/save_draft", draft_id=d)
        assert out["success"], (n, out)
    else:
        pytest.fail("the save never finished")
    assert outcomes == {"old", "new"}


def crash_when(env, d, reached, **payload):
    """Crash the save of d at the first step after which reached() is true (undoing the crashes
    before it). Returns the journal details of the unfinished save."""
    for n in range(1, 20):
        assert crash_save(env, n, draft_id=d, **payload)
        pending = draft_store.journal_pending(d)
        if pending and reached(pending[0][3]):
            return pending[0][3]
        sd.recover_saves()
    pytest.fail("never reached")


def test_after_a_crash_an_edit_made_since_is_never_overwritten(env, client, monkeypatch):
    root = write_project(env.projects, "Modificato", capcut_content())
    d = call(client, "/open_project", project_name="Modificato")["output"]["draft_id"]
    call(client, "/add_text", draft_id=d, text="Aggiunta", start=0, end=1)
    # Stopped after the first timeline copy was written
    crash_when(env, d, lambda details: (root / COPIES[0]).read_bytes() != (root / COPIES[1]).read_bytes())
    edited = json.loads((root / COPIES[1]).read_text())
    edited["user_edit_after_crash"] = True
    (root / COPIES[1]).write_text(json.dumps(edited))
    after_edit = {rel: (root / rel).read_bytes() for rel in COPIES}

    monkeypatch.setattr(sd, "capcut_is_running", lambda: True)
    notes = settle_after_restart()
    assert draft_store.journal_pending(d) and "CapCut" in notes[0]
    assert {rel: (root / rel).read_bytes() for rel in COPIES} == after_edit
    shown = call(client, "/timeline", draft_id=d)["output"]["unsettled_saves"]
    assert [(u["state"], u["kind"]) for u in shown] == [("pending", "existing")]
    assert shown[0]["folders"] == [os.path.realpath(root)] and "cannot be saved" in shown[0]["note"]

    monkeypatch.setattr(sd, "capcut_is_running", lambda: False)
    notes = settle_after_restart()
    assert draft_store.journal_pending(d) == [] and "left as it is" in notes[0]
    shown = call(client, "/timeline", draft_id=d)["output"]["unsettled_saves"]
    assert [u["state"] for u in shown] == ["abandoned"] and shown[0]["note"] == notes[0]
    assert {rel: (root / rel).read_bytes() for rel in COPIES} == after_edit
    assert hidden(env.projects) == []
    out = call(client, "/save_draft", draft_id=d)  # and saving again does not either
    assert not out["success"] and "disagree" in out["error"]
    assert {rel: (root / rel).read_bytes() for rel in COPIES} == after_edit


def test_recovery_removes_only_the_media_this_save_moved_in(env, client):
    root = write_project(env.projects, "Estraneo", capcut_content())
    d = call(client, "/open_project", project_name="Estraneo")["output"]["draft_id"]
    assert call(client, "/add_image", draft_id=d, image_url=png(env.tmp / "s.png", (255, 0, 0)), start=0, end=1)["success"]
    # Right after the plan is complete, before anything moves in
    details = crash_when(env, d, lambda details: details["phase"] == "ready")
    assert not (root / details["added"][0]["rel"]).exists()
    foreign = root / details["added"][0]["rel"]
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_bytes(b"CREATED BY ANOTHER WRITER AFTER THE CRASH")

    assert settle_after_restart() == []
    assert foreign.read_bytes() == b"CREATED BY ANOTHER WRITER AFTER THE CRASH"
    assert draft_store.journal_pending() == [] and hidden(env.projects) == []


def test_a_crash_between_the_two_renames_puts_the_project_back(env, client):
    d = call(client, "/create_draft", width=1080, height=1920)["output"]["draft_id"]
    call(client, "/add_text", draft_id=d, text="Old", start=0, end=1)
    assert call(client, "/save_draft", draft_id=d, project_name="Tra")["success"]
    call(client, "/add_text", draft_id=d, text="New", start=1, end=2, track_name="text_2")
    for n in range(1, 20):
        assert crash_save(env, n, draft_id=d, project_name="Tra")
        if not (env.projects / "Tra").exists():
            break
        sd.recover_saves()
    else:
        pytest.fail("no crash between the renames")
    assert [p for p in hidden(env.projects) if p.startswith(".capcut-mcp-old-")]
    assert settle_after_restart() == []
    assert text_values(env.projects / "Tra" / "draft_info.json") == ["Old"]
    assert hidden(env.projects) == [] and draft_store.journal_pending() == []


# --- Review 4: recovery acts only on states it can prove are its own ------------------------------

def existing_with_media(env, client, name):
    root = write_project(env.projects, name, capcut_content())
    d = call(client, "/open_project", project_name=name)["output"]["draft_id"]
    assert call(client, "/add_text", draft_id=d, text="Nuovo", start=0, end=10)["success"]
    assert call(client, "/add_image", draft_id=d, image_url=png(env.tmp / f"{name}.png", (255, 0, 0)), start=0, end=1)["success"]
    return d, root


def mixed_copies(root):
    return lambda details: details["phase"] == "ready" and len({(root / rel).read_bytes() for rel in COPIES}) > 1


def test_a_new_folder_edited_after_a_crash_is_kept(env, client):
    elsewhere = env.tmp / "export"
    elsewhere.mkdir()
    d = call(client, "/create_draft", width=1080, height=1920)["output"]["draft_id"]
    call(client, "/add_text", draft_id=d, text="Old", start=0, end=1)
    opts = dict(project_name="Misto", draft_folder=str(elsewhere))
    assert call(client, "/save_draft", draft_id=d, **opts)["success"]
    call(client, "/add_text", draft_id=d, text="New", start=1, end=2, track_name="text_2")
    details = crash_when(env, d, lambda x: x["phase"] == "ready" and
                         [sd._pair_state(p) for p in x["pairs"]] == ["new", "old"], **opts)
    first = Path(details["pairs"][0]["target"])
    (first / "user-file.txt").write_text("irreplaceable")

    notes = settle_after_restart()
    assert (first / "user-file.txt").read_text() == "irreplaceable"
    assert text_values(first / "draft_info.json") == ["New", "Old"]
    assert any("changed since" in n for n in notes), notes
    assert draft_store.journal_pending() == [] and hidden(elsewhere) == []
    # The version before is in the backups, and the note says where
    backup = next(Path(n.rsplit(" at ", 1)[1]) for n in notes if "version before" in n)
    assert text_values(backup / "draft_info.json") == ["Old"]


def test_a_switched_main_timeline_stops_recovery_before_anything_is_written(env, client):
    d, root = existing_with_media(env, client, "Selettore")
    details = crash_when(env, d, mixed_copies(root))
    other = root / "Timelines" / "USER"
    other.mkdir()
    new = next((root / rel).read_bytes() for rel, f in details["copies"].items()
               if existing_project._sha((root / rel).read_bytes()) == f["new"])
    (other / "draft_info.json").write_bytes(new)
    (root / "Timelines" / "project.json").write_text(json.dumps({"main_timeline_id": "USER"}))
    media = root / details["added"][0]["rel"]
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}

    notes = settle_after_restart()
    assert "Timelines/project.json" in notes[0] and "left as it is" in notes[0]
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before and media.exists()


def test_a_changed_selector_leaves_the_metadata_alone_too(env, client):
    d, root = existing_with_media(env, client, "Meta")
    crash_when(env, d, lambda x: x["phase"] == "ready" and all(
        existing_project._sha((root / rel).read_bytes()) == f["new"] for rel, f in x["copies"].items())
        and existing_project._sha((root / "draft_meta_info.json").read_bytes()) == x["meta"]["old"])
    selector = root / "Timelines" / "project.json"
    selector.write_text(json.dumps({**json.loads(selector.read_text()), "user_edit": True}))
    meta = (root / "draft_meta_info.json").read_bytes()

    notes = settle_after_restart()
    assert "left as it is" in notes[0]
    assert (root / "draft_meta_info.json").read_bytes() == meta


def test_recovery_does_not_follow_a_project_replaced_by_a_link(env, client):
    d, root = existing_with_media(env, client, "Collegato")
    crash_when(env, d, mixed_copies(root))
    moved = env.tmp / "outside"
    root.rename(moved)
    root.symlink_to(moved, target_is_directory=True)
    before = {p: p.read_bytes() for p in moved.rglob("*") if p.is_file()}

    notes = settle_after_restart()
    assert "project folder itself" in notes[0]
    assert {p: p.read_bytes() for p in moved.rglob("*") if p.is_file()} == before


def test_project_path_refuses_a_linked_root(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    with pytest.raises(sd.SaveDraftError):
        sd.project_path(str(tmp_path / "link"), "draft_info.json")


def test_a_failed_save_keeps_media_a_changed_timeline_uses(env, client, monkeypatch):
    d, root = existing_with_media(env, client, "Concorrente")
    real, count = existing_project._write_atomic, [0]
    first = root / "Timelines" / "TL-1" / "draft_info.json"  # the first copy written

    def edited_then_fail(path, data, *tmp):
        count[0] += 1
        if count[0] == 2:
            edited = json.loads(first.read_text())
            edited["user_edit"] = True
            first.write_text(json.dumps(edited))
            raise OSError("injected failure")
        return real(path, data, *tmp)
    with monkeypatch.context() as m:
        m.setattr(existing_project, "_write_atomic", edited_then_fail)
        out = call(client, "/save_draft", draft_id=d)

    assert not out["success"] and "left as it is" in out["error"]
    content = json.loads(first.read_text())
    assert content["user_edit"]
    photo = next(v for v in content["materials"]["videos"] if v.get("type") == "photo")
    assert Path(photo["path"]).exists()
    shown = call(client, "/timeline", draft_id=d)["output"]["unsettled_saves"]
    assert [u["state"] for u in shown] == ["abandoned"]


def test_undoing_keeps_media_another_timeline_mentions(env, client):
    d, root = existing_with_media(env, client, "Citato")
    details = crash_when(env, d, mixed_copies(root))
    media = root / details["added"][0]["rel"]
    other = root / "Timelines" / "OTHER"
    other.mkdir()
    (other / "draft_info.json").write_text(json.dumps({"materials": {"videos": [{"path": str(media)}]}}))

    notes = settle_after_restart()
    assert media.exists() and "was kept" in notes[0]
    assert {(root / rel).read_bytes() for rel in COPIES} == {(root / COPIES[1]).read_bytes()}
    assert draft_store.journal_pending() == []


def test_a_conflict_on_one_folder_still_accounts_for_the_other(env, client):
    elsewhere = env.tmp / "export"
    elsewhere.mkdir()
    d = call(client, "/create_draft", width=1080, height=1920)["output"]["draft_id"]
    call(client, "/add_text", draft_id=d, text="Old", start=0, end=1)
    opts = dict(project_name="Due", draft_folder=str(elsewhere))
    assert call(client, "/save_draft", draft_id=d, **opts)["success"]
    call(client, "/add_text", draft_id=d, text="New", start=1, end=2, track_name="text_2")
    details = crash_when(env, d, lambda x: x["phase"] == "ready" and
                         [sd._pair_state(p) for p in x["pairs"]] == ["new", "aside"], **opts)
    Path(details["pairs"][1]["target"]).mkdir()

    notes = settle_after_restart()
    assert hidden(elsewhere) == [] and hidden(env.projects) == []
    assert len(notes) == 2 and all("version before that save is at" in n for n in notes), notes
    for note in notes:
        assert text_values(Path(note.rsplit(" at ", 1)[1]) / "draft_info.json") == ["Old"]
