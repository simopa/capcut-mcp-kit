# Added in capcut-mcp-kit (2026): the backend started the way the MCP server starts it (python
# capcut_server.py, its own process), driven over HTTP. In-process tests import the module and never
# run its startup code: a name shadowed there broke every save. Projects, state and backups are
# temporary folders (CAPCUT_PROJECTS_DIR, CAPCUT_MCP_STATE_DIR, CAPCUT_MCP_BACKUP_DIR).
# See NOTICE at the repository root.
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from test_commit_safety import REAL_CAPCUT_CHECK
from test_existing_project import capcut_content, write_project


@pytest.fixture
def backend(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "CAPCUT_PORT": str(port), "CAPCUT_PROJECTS_DIR": str(projects),
           "CAPCUT_MCP_STATE_DIR": str(tmp_path / "state"), "CAPCUT_MCP_BACKUP_DIR": str(tmp_path / "backups"),
           "CAPCUT_MCP_TOKEN": "test-token"}
    proc = subprocess.Popen([sys.executable, "capcut_server.py"], cwd=Path(__file__).resolve().parents[1], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    url = f"http://127.0.0.1:{port}"

    def call(route, **payload):
        req = urllib.request.Request(url + route, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-CapCut-Kit-Token": "test-token"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    try:
        for _ in range(200):
            try:
                urllib.request.urlopen(url + "/health", timeout=1)
                break
            except OSError:
                if proc.poll() is not None:
                    pytest.fail(proc.stderr.read().decode()[-3000:])
                time.sleep(0.1)
        yield call, projects
    finally:
        proc.terminate()
        proc.wait(10)


def test_the_real_backend_saves_a_new_project(backend):
    call, projects = backend
    d = call("/create_draft", width=1080, height=1920)["output"]["draft_id"]
    assert call("/add_text", draft_id=d, text="Ciao", start=0, end=1)["success"]
    out = call("/save_draft", draft_id=d, project_name="Reale")
    assert out["success"], out
    assert (projects / "Reale" / "draft_info.json").is_file()


@pytest.mark.skipif(REAL_CAPCUT_CHECK() is not False, reason="CapCut is open on this machine: saving into a project is refused")
def test_the_real_backend_edits_and_saves_an_existing_project(backend):
    call, projects = backend
    root = write_project(projects, "Esistente", capcut_content())
    d = call("/open_project", project_name="Esistente")["output"]["draft_id"]
    clip = call("/list_clips", draft_id=d)["output"]["clips"][0]
    assert call("/edit_clip", draft_id=d, clip_id=clip["id"], trim_end=1)["success"]
    out = call("/save_draft", draft_id=d)
    assert out["success"], out
    seg = json.loads((root / "draft_info.json").read_text())["tracks"][0]["segments"][0]
    assert seg["target_timerange"]["duration"] == 3_000_000
