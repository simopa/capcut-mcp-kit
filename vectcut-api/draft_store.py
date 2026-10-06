# Added in capcut-mcp-kit (2026): drafts persisted in SQLite so they survive a backend restart,
# one lock per draft, and all-or-nothing changes. See NOTICE at the repository root.
"""
Draft state.

Every draft lives in memory (draft_cache.DRAFT_CACHE) and, after each successful change, in a
SQLite file as a pickled Script_file (pending keyframes included). After a restart a draft is
loaded back on first use. A change runs under the draft's lock on a snapshot: if it fails, the
draft goes back to exactly what it was.
"""

import os
import pickle
import sqlite3
import threading
import time
from functools import wraps

from draft_cache import DRAFT_CACHE, update_cache

# Bump when Script_file objects pickled by an older version can no longer be used
FORMAT = 1


class DraftNotFound(LookupError):
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


def _connect() -> sqlite3.Connection:
    folder = state_dir()
    os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(os.path.join(folder, "drafts.sqlite3"), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS drafts (
        draft_id TEXT PRIMARY KEY, format INTEGER NOT NULL, revision INTEGER NOT NULL,
        updated_at REAL NOT NULL, data BLOB NOT NULL)""")
    return conn


def persist(draft_id: str, script) -> int:
    """Store the draft; returns its new revision."""
    data = pickle.dumps(script, protocol=pickle.HIGHEST_PROTOCOL)
    conn = _connect()
    try:
        with conn:
            row = conn.execute("SELECT revision FROM drafts WHERE draft_id = ?", (draft_id,)).fetchone()
            revision = (row[0] if row else 0) + 1
            conn.execute("INSERT OR REPLACE INTO drafts (draft_id, format, revision, updated_at, data) "
                         "VALUES (?, ?, ?, ?, ?)", (draft_id, FORMAT, revision, time.time(), data))
        return revision
    finally:
        conn.close()


def _load(draft_id: str):
    conn = _connect()
    try:
        row = conn.execute("SELECT format, data FROM drafts WHERE draft_id = ?", (draft_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    if row[0] != FORMAT:
        raise DraftNotFound(f"Draft {draft_id} was saved by an incompatible version of the kit: create a new draft")
    return pickle.loads(row[1])


def get_draft(draft_id):
    """The draft with this ID, from memory or from disk. Never creates one."""
    if not draft_id:
        raise DraftNotFound("draft_id is required: create a draft with capcut_create_draft first")
    if draft_id in DRAFT_CACHE:
        script = DRAFT_CACHE[draft_id]
        update_cache(draft_id, script)
        return script
    script = _load(draft_id)
    if script is None:
        raise DraftNotFound(f"Draft {draft_id} not found: create a draft with capcut_create_draft")
    update_cache(draft_id, script)
    return script


_locks = {}
_locks_guard = threading.Lock()


def draft_lock(draft_id: str) -> threading.RLock:
    with _locks_guard:
        return _locks.setdefault(draft_id, threading.RLock())


def _succeeded(response) -> bool:
    resp = response[0] if isinstance(response, tuple) else response
    try:
        body = resp.get_json(silent=True)
    except AttributeError:
        return False
    return isinstance(body, dict) and body.get("success") is True


def transactional(view):
    """Wrap a Flask route that changes the draft named by its JSON "draft_id": run it under the
    draft's lock; keep and persist the change only if the route reports success, otherwise put
    the draft back as it was."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        from flask import jsonify, request
        data = request.get_json(silent=True) or {}
        draft_id = data.get("draft_id")
        if not draft_id or not isinstance(draft_id, str):
            return jsonify({"success": False, "output": "",
                            "error": "draft_id is required: create a draft with capcut_create_draft first"})
        with draft_lock(draft_id):
            try:
                snapshot = pickle.dumps(get_draft(draft_id), protocol=pickle.HIGHEST_PROTOCOL)
            except DraftNotFound as e:
                return jsonify({"success": False, "output": "", "error": str(e)})
            try:
                response = view(*args, **kwargs)
                if _succeeded(response):
                    persist(draft_id, DRAFT_CACHE[draft_id])
                    return response
            except Exception as e:
                update_cache(draft_id, pickle.loads(snapshot))
                return jsonify({"success": False, "output": "", "error": f"{e} (the draft was left unchanged)"})
            update_cache(draft_id, pickle.loads(snapshot))
            return response
    return wrapper
