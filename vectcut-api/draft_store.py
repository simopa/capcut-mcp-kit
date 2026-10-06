# Added in capcut-mcp-kit (2026): drafts persisted in SQLite so they survive a backend restart,
# one lock per draft (shared between processes), all-or-nothing changes, safe retries and a journal
# of the writes to project folders. See NOTICE at the repository root.
"""
Draft state.

SQLite (in the state folder, private to the user) is the owner of every draft: a pickled
Script_file (pending keyframes included) with a revision number. The cache only keeps copies of
what SQLite holds: each entry is one (revision, draft) pair, read from SQLite in one query and
replaced as a whole, never by an older revision, and used only while it is still the current
revision (a draft changed by another backend process is loaded again). Readers get the revision
and the draft from the same entry.

A change runs under the draft's lock (threads and processes) on a private working copy. If the
route reports success, the new draft, its revision (compare-and-swap on the revision the copy was
made from), the reply for the request_id and the end of any journalled write to a project folder
are committed in one SQLite transaction; only then does the copy replace the cached one. On failure
the working copy is dropped. A save of the draft that did not finish (see save_draft_impl) is
settled, and its result recorded in the draft, before any other change to it.

A draft that is created (create, open) is stored in the same transaction as the reply to its
request_id: a failure leaves neither.

A call may carry:
- request_id: the same id again with the same call (a retry) returns the first reply without
  applying it twice. An id reused for a different call is refused. Replies are kept for
  REPLY_DAYS days; after that the id is still known and a retry is refused rather than applied.
- expected_revision: the change is refused if the draft has moved on (another client changed it).
"""

import contextlib
import contextvars
import hashlib
import json
import os
import pickle
import sqlite3
import threading
import time
from functools import wraps

from collections import OrderedDict

# Bump when Script_file objects pickled by an older version can no longer be used
FORMAT = 1
REPLY_DAYS = 7


class DraftNotFound(LookupError):
    pass


class RequestConflict(ValueError):
    pass


def state_dir() -> str:
    configured = os.environ.get("CAPCUT_MCP_STATE_DIR")
    if configured:
        return os.path.expanduser(configured)
    if os.name == "nt":
        return os.path.expandvars(r"%LOCALAPPDATA%\capcut-mcp-kit")
    if os.uname().sysname == "Darwin":
        return os.path.expanduser("~/Library/Application Support/capcut-mcp-kit")
    return os.path.expanduser("~/.local/state/capcut-mcp-kit")


def private_dir(path: str) -> str:
    """Create a folder only the user can read (the drafts and the token are in it)."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    if os.name != "nt":
        os.chmod(path, 0o700)
    return path


def _connect() -> sqlite3.Connection:
    folder = private_dir(state_dir())
    path = os.path.join(folder, "drafts.sqlite3")
    if not os.path.exists(path):
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS drafts (
        draft_id TEXT PRIMARY KEY, format INTEGER NOT NULL, revision INTEGER NOT NULL,
        updated_at REAL NOT NULL, data BLOB NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS request_log (
        request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, draft_id TEXT, reply TEXT,
        created_at REAL NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS operations (
        op_id TEXT PRIMARY KEY, draft_id TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL,
        details TEXT NOT NULL, created_at REAL NOT NULL)""")
    conn.execute("DROP TABLE IF EXISTS requests")  # per-draft replies of kit 0.3.0
    if os.name != "nt":
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.chmod(path + suffix, 0o600)
    return conn


@contextlib.contextmanager
def _db():
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


# --- Locks shared by the threads of this process and by other processes -------------------------

class _NamedLock:
    """A re-entrant lock: a thread lock inside the process, flock on a file in the state folder
    between processes (the same name always maps to the same file)."""

    def __init__(self, name: str):
        self.name = name
        self.thread_lock = threading.RLock()
        self.depth = 0
        self.fd = None

    def __enter__(self):
        self.thread_lock.acquire()
        if self.depth == 0 and os.name != "nt":
            import fcntl
            folder = private_dir(os.path.join(state_dir(), "locks"))
            path = os.path.join(folder, hashlib.sha256(self.name.encode()).hexdigest()[:32] + ".lock")
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except BaseException:
                os.close(fd)
                self.thread_lock.release()
                raise
            self.fd = fd
        self.depth += 1
        return self

    def __exit__(self, *exc):
        self.depth -= 1
        if self.depth == 0 and self.fd is not None:
            import fcntl
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None
        self.thread_lock.release()


_locks = {}
_locks_guard = threading.Lock()


def named_lock(name: str) -> _NamedLock:
    with _locks_guard:
        return _locks.setdefault(name, _NamedLock(name))


def draft_lock(draft_id: str) -> _NamedLock:
    return named_lock("draft:" + draft_id)


def project_lock(path: str) -> _NamedLock:
    """One lock per project folder, whichever draft or route writes it."""
    return named_lock("project:" + os.path.realpath(path))


@contextlib.contextmanager
def until_commit(lock):
    """Take a lock for the rest of the current change: it is released only once the change is
    committed (or dropped), so whoever takes it next finds the change's journal entry closed.
    Outside a change, held for the block."""
    held = HELD.get()
    if held is None:
        with lock:
            yield
    else:
        held.enter_context(lock)
        yield


# --- Drafts -------------------------------------------------------------------------------------

WORKING = contextvars.ContextVar("working_drafts", default=None)  # {draft_id: (base revision, script)} in a change
FS_OPS = contextvars.ContextVar("fs_ops", default=None)  # journal entries a change committed to disk
NEW_DRAFTS = contextvars.ContextVar("new_drafts", default=None)  # drafts created by a create/open route
NOTICES = contextvars.ContextVar("notices", default=None)  # what settling unfinished saves reported
HELD = contextvars.ContextVar("held_locks", default=None)  # locks released once the change is committed

MAX_CACHED = 1000
_cache = OrderedDict()  # draft_id -> (revision, script), each entry replaced as a whole
_cache_lock = threading.Lock()


def _cached(draft_id: str, rev: int):
    with _cache_lock:
        entry = _cache.get(draft_id)
        if entry is None or entry[0] != rev:
            return None
        _cache.move_to_end(draft_id)
        return entry[1]


def _install(draft_id: str, rev: int, script):
    """Cache (rev, script) unless the cache already holds this revision or a newer one."""
    with _cache_lock:
        entry = _cache.get(draft_id)
        if entry is not None and entry[0] >= rev:
            return
        _cache[draft_id] = (rev, script)
        _cache.move_to_end(draft_id)
        while len(_cache) > MAX_CACHED:
            _cache.popitem(last=False)


def forget_cache():
    """Drop every cached draft (they are loaded again from SQLite)."""
    with _cache_lock:
        _cache.clear()


def revision(draft_id: str) -> int:
    with _db() as conn:
        row = conn.execute("SELECT revision FROM drafts WHERE draft_id = ?", (draft_id,)).fetchone()
    return row[0] if row else 0


def _load(draft_id: str):
    """(revision, script) from SQLite, read together, or None."""
    with _db() as conn:
        row = conn.execute("SELECT format, revision, data FROM drafts WHERE draft_id = ?", (draft_id,)).fetchone()
    if row is None:
        return None
    if row[0] != FORMAT:
        raise DraftNotFound(f"Draft {draft_id} was saved by an incompatible version of the kit: create a new draft")
    return row[1], pickle.loads(row[2])


def _current(draft_id: str):
    """(revision, script) for the draft as SQLite holds it now, from the cache when still current.
    The script is shared: callers must not change it."""
    if not draft_id:
        raise DraftNotFound("draft_id is required: create a draft with capcut_create_draft first")
    rev = revision(draft_id)
    script = _cached(draft_id, rev)
    if script is not None:
        return rev, script
    loaded = _load(draft_id)
    if loaded is None:
        with _cache_lock:
            _cache.pop(draft_id, None)
        raise DraftNotFound(f"Draft {draft_id} not found: create a draft with capcut_create_draft")
    _install(draft_id, *loaded)
    return loaded


def current(draft_id: str):
    """(revision, draft) read together: inside a change, the working copy with the revision it
    was made from; otherwise the current one. Never creates one."""
    working = WORKING.get()
    if working and draft_id in working:
        return working[draft_id]
    return _current(draft_id)


def get_draft(draft_id):
    """The draft with this ID: inside a change, its working copy; otherwise the current one.
    Never creates one."""
    return current(draft_id)[1]


def _insert_new(conn, draft_id: str, script):
    conn.execute("INSERT INTO drafts (draft_id, format, revision, updated_at, data) VALUES (?, ?, 1, ?, ?)",
                 (draft_id, FORMAT, time.time(), pickle.dumps(script, protocol=pickle.HIGHEST_PROTOCOL)))


def store_new(draft_id: str, script):
    """Record a draft that was just created (revision 1). Inside a create/open route it is stored
    with the route's reply, when the route succeeds."""
    pending = NEW_DRAFTS.get()
    if pending is not None:
        pending.append((draft_id, script))
        return
    with _db() as conn, conn:
        _insert_new(conn, draft_id, script)
    _install(draft_id, 1, script)


def _commit(conn, draft_id: str, base_revision: int, script):
    data = pickle.dumps(script, protocol=pickle.HIGHEST_PROTOCOL)
    changed = conn.execute("UPDATE drafts SET format = ?, revision = ?, updated_at = ?, data = ? "
                           "WHERE draft_id = ? AND revision = ?",
                           (FORMAT, base_revision + 1, time.time(), data, draft_id, base_revision)).rowcount
    if changed != 1:
        raise RuntimeError(f"Draft {draft_id} was changed by another process meanwhile")


# --- Requests ----------------------------------------------------------------------------------

def fingerprint(endpoint: str, payload: dict) -> str:
    """What a request_id stands for: the route and everything sent with it."""
    body = {k: v for k, v in payload.items() if k != "request_id"}
    return hashlib.sha256(json.dumps([endpoint, body], sort_keys=True, default=str).encode()).hexdigest()


def remembered_reply(request_id: str, fp: str):
    """The reply to an earlier call with this request_id, None if the id is new.
    Raises RequestConflict if the id was used for a different call or its reply has expired."""
    with _db() as conn:
        row = conn.execute("SELECT fingerprint, reply FROM request_log WHERE request_id = ?", (request_id,)).fetchone()
    if row is None:
        return None
    if row[0] != fp:
        raise RequestConflict(f"request_id {request_id!r} was already used for a different call: use a new one")
    if row[1] is None:
        raise RequestConflict(f"request_id {request_id!r} was used more than {REPLY_DAYS} days ago and its reply is "
                              f"no longer kept: check the draft (capcut_get_timeline) and use a new request_id")
    return row[1]


def _remember(conn, request_id: str, fp: str, draft_id, reply: str):
    conn.execute("INSERT INTO request_log (request_id, fingerprint, draft_id, reply, created_at) "
                 "VALUES (?, ?, ?, ?, ?)", (request_id, fp, draft_id, reply, time.time()))
    # Old replies are dropped, their ids kept: a late retry is refused, never applied again
    conn.execute("UPDATE request_log SET reply = NULL WHERE reply IS NOT NULL AND created_at < ?",
                 (time.time() - REPLY_DAYS * 86400,))


def remember_reply(request_id: str, fp: str, draft_id, reply: str):
    with _db() as conn, conn:
        _remember(conn, request_id, fp, draft_id, reply)


# --- Journal of writes to project folders --------------------------------------------------------

def journal_begin(draft_id: str, kind: str, details: dict) -> str:
    """Record a write to a project folder before anything is created for it. It stays 'pending'
    until the draft change that made it is committed ('done'), it is undone and checked to be
    ('rolled_back'), or it is left for the user because something else changed the folder
    since ('abandoned', with a note of what is where)."""
    import uuid
    op_id = uuid.uuid4().hex
    with _db() as conn, conn:
        conn.execute("INSERT INTO operations (op_id, draft_id, kind, state, details, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (op_id, draft_id, kind, "pending", json.dumps(details), time.time()))
    return op_id


def journal_update(op_id: str, state: str = None, details: dict = None):
    with _db() as conn, conn:
        if state:
            conn.execute("UPDATE operations SET state = ? WHERE op_id = ?", (state, op_id))
        if details is not None:
            conn.execute("UPDATE operations SET details = ? WHERE op_id = ?", (json.dumps(details), op_id))


def journal_get(op_id: str):
    """(state, details) of one entry as recorded now, or None."""
    with _db() as conn:
        row = conn.execute("SELECT state, details FROM operations WHERE op_id = ?", (op_id,)).fetchone()
    return (row[0], json.loads(row[1])) if row else None


def journal_pending(draft_id: str = None) -> list:
    """[(op_id, draft_id, kind, details)] of the writes not settled yet, oldest first."""
    with _db() as conn:
        sql = "SELECT op_id, draft_id, kind, details FROM operations WHERE state = 'pending'"
        rows = conn.execute(sql + (" AND draft_id = ?" if draft_id else "") + " ORDER BY created_at",
                            (draft_id,) if draft_id else ()).fetchall()
    return [(r[0], r[1], r[2], json.loads(r[3])) for r in rows]


def journal_unsettled(draft_id: str) -> list:
    """The saves of a draft that need the user's attention, oldest first: those still pending and
    those abandoned in the last REPLY_DAYS days. Read under the draft's lock when any is pending,
    so a save in progress is not reported as unfinished."""
    def rows():
        with _db() as conn:
            return conn.execute("SELECT state, kind, details, created_at FROM operations WHERE draft_id = ? AND "
                                "(state = 'pending' OR (state = 'abandoned' AND created_at >= ?)) ORDER BY created_at",
                                (draft_id, time.time() - REPLY_DAYS * 86400)).fetchall()
    found = rows()
    if any(r[0] == "pending" for r in found):
        with draft_lock(draft_id):
            found = rows()
    out = []
    for state, kind, details, created_at in found:
        details = json.loads(details)
        if state == "pending":
            note = ("A save of this draft stopped halfway and is not settled yet: it is settled at the next change of "
                    "this draft or the next start of the backend (once CapCut is closed, if it has to undo "
                    "something in CapCut's projects folder). The draft cannot be saved until then.")
        else:
            note = " ".join(details.get("notes") or ["A save of this draft stopped halfway and was left as it is."])
        out.append({"state": state, "kind": kind, "folders": details.get("locks") or [],
                    "started": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(created_at)), "note": note})
    return out


def committed_to_disk(op_id: str, summary: str):
    """Called once a change has written a project folder: its journal entry is closed together
    with the draft, and a failure from here on must not claim that nothing happened. Outside a
    change (no draft to commit with) it is closed now."""
    ops = FS_OPS.get()
    if ops is None:
        journal_update(op_id, "done")
    else:
        ops.append((op_id, summary))


# --- Routes ------------------------------------------------------------------------------------

def _succeeded(response) -> bool:
    resp = response[0] if isinstance(response, tuple) else response
    try:
        body = resp.get_json(silent=True)
    except AttributeError:
        return False
    return isinstance(body, dict) and body.get("success") is True


def _amend_reply(response, moved, new_revision) -> str:
    """Add the draft's new revision (and the items moved to a free track) to a successful reply.
    Returns the reply body, which is what a retry with the same request_id gets back."""
    resp = response[0] if isinstance(response, tuple) else response
    body = resp.get_json(silent=True)
    if isinstance(body, dict) and isinstance(body.get("output"), dict):
        if new_revision is not None:
            body["output"]["revision"] = new_revision
        if moved:
            body["output"]["moved_to_free_track"] = moved
        resp.set_data(json.dumps(body))
    return resp.get_data(as_text=True)


def _request_id(data):
    request_id = data.get("request_id")
    return str(request_id) if request_id not in (None, "") else None


def _failure(error: str):
    from flask import jsonify
    return jsonify({"success": False, "output": "", "error": error})


def _not_recorded(error, ops) -> str:
    if not ops:
        return f"{error} (the draft was left unchanged)"
    written = "; ".join(summary for _, summary in ops)
    return (f"{error}. The project folder WAS written ({written}) but the kit could not record it; "
            f"it is reconciled at the next call on this draft or the next start of the backend")


def _commit_change(draft_id, base_revision, working, ops, request_id=None, fp=None, reply=None):
    """The new draft, its reply and the end of its journalled writes, in one transaction; then
    the cache."""
    with _db() as conn, conn:
        _commit(conn, draft_id, base_revision, working)
        if request_id:
            _remember(conn, request_id, fp, draft_id, reply)
        for op_id, _ in ops:
            conn.execute("UPDATE operations SET state = 'done' WHERE op_id = ? AND state = 'pending'", (op_id,))
    _install(draft_id, base_revision + 1, working)


def _copy(script):
    return pickle.loads(pickle.dumps(script, protocol=pickle.HIGHEST_PROTOCOL))


def reconcile(draft_id: str) -> list:
    """Settle the unfinished saves of a draft (crash, or the draft could not be recorded) and
    record what they wrote in the draft, as a change of its own. Call with the draft's lock held
    (it is re-entrant). Returns notes for the user (saves left pending or abandoned)."""
    if not journal_pending(draft_id):
        return []
    import save_draft_impl  # imported here: it imports this module
    with draft_lock(draft_id):
        try:
            base_revision, current_script = _current(draft_id)
        except DraftNotFound:
            return save_draft_impl.settle_saves(draft_id, None)
        working = _copy(current_script)
        held = contextlib.ExitStack()
        tokens = [(WORKING, WORKING.set({draft_id: (base_revision, working)})), (FS_OPS, FS_OPS.set([])),
                  (HELD, HELD.set(held))]
        try:
            notes = save_draft_impl.settle_saves(draft_id, working)
            ops = FS_OPS.get()
            if ops:
                _commit_change(draft_id, base_revision, working, ops)
            return notes
        finally:
            for var, token in reversed(tokens):
                var.reset(token)
            held.close()


def transactional(view):
    """Wrap a Flask route that changes the draft named by its JSON "draft_id": run it under the
    draft's lock on a working copy; commit it only if the route reports success."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        from flask import Response, request
        import placement
        data = request.get_json(silent=True) or {}
        draft_id = data.get("draft_id")
        if not draft_id or not isinstance(draft_id, str):
            return _failure("draft_id is required: create a draft with capcut_create_draft first")
        request_id = _request_id(data)
        fp = fingerprint(request.path, data)
        expected = data.get("expected_revision")
        with draft_lock(draft_id):
            if request_id:
                try:
                    earlier = remembered_reply(request_id, fp)
                except RequestConflict as e:
                    return _failure(str(e))
                if earlier is not None:  # a retry: this change was already applied
                    return Response(earlier, mimetype="application/json")
            try:
                base_revision = _current(draft_id)[0]
            except DraftNotFound as e:
                return _failure(str(e))
            if expected is not None and (not isinstance(expected, int) or isinstance(expected, bool)
                                         or expected != base_revision):
                return _failure(f"Draft {draft_id} is at revision {base_revision}, not {expected!r}: another call "
                                f"changed it meanwhile. Check it (capcut_get_timeline) and try again.")
            try:
                notes = reconcile(draft_id)  # an unfinished save is recorded before anything else
                base_revision, current_script = _current(draft_id)
            except Exception as e:
                return _failure(f"An unfinished save of this draft could not be settled: {e}")
            working = _copy(current_script)
            held = contextlib.ExitStack()
            tokens = [(WORKING, WORKING.set({draft_id: (base_revision, working)})), (FS_OPS, FS_OPS.set([])),
                      (NOTICES, NOTICES.set(notes)), (HELD, HELD.set(held)),
                      (placement.AUTO_TRACK, placement.AUTO_TRACK.set(data.get("auto_track", True) is not False)),
                      (placement.MOVED, placement.MOVED.set([]))]
            try:
                try:
                    response = view(*args, **kwargs)
                except Exception as e:
                    return _failure(_not_recorded(e, FS_OPS.get()))
                ops = FS_OPS.get()
                if not _succeeded(response):
                    if ops:
                        resp = response[0] if isinstance(response, tuple) else response
                        body = resp.get_json(silent=True) or {}
                        return _failure(_not_recorded(body.get("error") or "Failed", ops))
                    return response
                try:
                    reply = _amend_reply(response, placement.MOVED.get(), base_revision + 1)
                    _commit_change(draft_id, base_revision, working, ops, request_id, fp, reply)
                except Exception as e:
                    return _failure(_not_recorded(e, ops))
                return response
            finally:
                for var, token in reversed(tokens):
                    var.reset(token)
                held.close()
    return wrapper


def idempotent(view):
    """Wrap a Flask route that starts a draft (create, open): the drafts it creates are stored
    only if it succeeds, together with its reply, in one transaction. With a request_id, a retry
    returns the first reply instead of starting a second draft."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        from flask import Response, request
        data = request.get_json(silent=True) or {}
        request_id = _request_id(data)
        fp = fingerprint(request.path, data)
        with contextlib.ExitStack() as stack:
            if request_id:
                stack.enter_context(named_lock("request:" + request_id))
                try:
                    earlier = remembered_reply(request_id, fp)
                except RequestConflict as e:
                    return _failure(str(e))
                if earlier is not None:
                    return Response(earlier, mimetype="application/json")
            token = NEW_DRAFTS.set([])
            try:
                response = view(*args, **kwargs)
                created = NEW_DRAFTS.get()
            finally:
                NEW_DRAFTS.reset(token)
            if not _succeeded(response):
                return response
            try:
                reply = _amend_reply(response, None, None)
                with _db() as conn, conn:
                    for draft_id, script in created:
                        _insert_new(conn, draft_id, script)
                    if request_id:
                        _remember(conn, request_id, fp, None, reply)
            except Exception as e:
                return _failure(f"{e} (nothing was created)")
            for draft_id, script in created:
                _install(draft_id, 1, script)
            return response
    return wrapper
