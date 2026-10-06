# Modified in capcut-mcp-kit (2026) from VectCutAPI @ cfa4779: autouse fixture that hides the real CapCut drafts folder, backups and draft store from every test.
# See NOTICE at the repository root.
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


import pytest


def cache_only(draft_id, script):
    """Put a draft in the cache only (revision 0, nothing in SQLite): for tests whose stand-in
    drafts cannot be pickled."""
    import draft_store
    with draft_store._cache_lock:
        draft_store._cache[draft_id] = (0, script)


@pytest.fixture(autouse=True)
def isolate_from_capcut(tmp_path):
    """Added in capcut-mcp-kit: no test may touch the real CapCut drafts folder, backups or draft store.
    Its own MonkeyPatch, so a test calling monkeypatch.undo() cannot lift the isolation."""
    import draft_store
    import save_draft_impl
    draft_store.forget_cache()  # each test has its own draft store
    mp = pytest.MonkeyPatch()
    mp.setattr(save_draft_impl, "find_capcut_projects_dir", lambda: None)
    mp.setattr(save_draft_impl, "capcut_is_running", lambda: False)
    mp.setenv("CAPCUT_MCP_BACKUP_DIR", str(tmp_path / "backups"))
    mp.setenv("CAPCUT_MCP_STATE_DIR", str(tmp_path / "state"))
    mp.setenv("CAPCUT_MCP_TOKEN", "test-token")
    yield
    mp.undo()
