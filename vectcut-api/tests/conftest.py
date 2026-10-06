# Modified in capcut-mcp-kit (2026) from VectCutAPI @ cfa4779: autouse fixture that hides the real CapCut drafts folder, backups and draft store from every test.
# See NOTICE at the repository root.
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


import pytest


@pytest.fixture(autouse=True)
def isolate_from_capcut(monkeypatch, tmp_path):
    """Added in capcut-mcp-kit: no test may touch the real CapCut drafts folder, backups or draft store."""
    import save_draft_impl
    monkeypatch.setattr(save_draft_impl, "find_capcut_projects_dir", lambda: None)
    monkeypatch.setattr(save_draft_impl, "capcut_is_running", lambda: False)
    monkeypatch.setenv("CAPCUT_MCP_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("CAPCUT_MCP_STATE_DIR", str(tmp_path / "state"))
