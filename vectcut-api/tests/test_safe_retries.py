# Added in capcut-mcp-kit (2026): a retried call is not applied twice, a stale revision is refused,
# and an old draft cannot silently replace a project edited in CapCut. See NOTICE at the repository root.
import json
import draft_store
from pathlib import Path

import pytest

HOST = {"Host": "127.0.0.1:9001", "X-CapCut-Kit-Token": "test-token"}


@pytest.fixture
def client():
    import capcut_server
    return capcut_server.app.test_client()


def call(client, route, **payload):
    return client.post(route, json=payload, headers=HOST).get_json()


def new_draft(client):
    return call(client, "/create_draft", width=1080, height=1920)["output"]["draft_id"]


def texts(draft_id):
    return sum(len(t.segments) for n, t in draft_store.get_draft(draft_id).tracks.items() if n.startswith("text"))


def test_same_request_id_is_applied_once(client):
    d = new_draft(client)
    first = call(client, "/add_text", draft_id=d, text="A", start=0, end=1, request_id="r-1")
    again = call(client, "/add_text", draft_id=d, text="A", start=0, end=1, request_id="r-1")
    assert first == again
    assert texts(d) == 1
    call(client, "/add_text", draft_id=d, text="B", start=2, end=3, request_id="r-2")
    assert texts(d) == 2


def test_a_failed_call_can_be_retried_with_its_id(client):
    d = new_draft(client)
    bad = call(client, "/add_text", draft_id=d, text="A", start=0, end=1, intro_animation="Nope", request_id="r-3")
    assert bad["success"] is False
    ok = call(client, "/add_text", draft_id=d, text="A", start=0, end=1, request_id="r-3")
    assert ok["success"] is True and texts(d) == 1


def test_replies_carry_the_revision_and_stale_revisions_are_refused(client):
    d = new_draft(client)
    r1 = call(client, "/add_text", draft_id=d, text="A", start=0, end=1)["output"]["revision"]
    r2 = call(client, "/add_text", draft_id=d, text="B", start=2, end=3, expected_revision=r1)["output"]["revision"]
    assert r2 == r1 + 1
    stale = call(client, "/add_text", draft_id=d, text="C", start=4, end=5, expected_revision=r1)
    assert stale["success"] is False and f"revision {r2}" in stale["error"]
    assert texts(d) == 2
    assert call(client, "/timeline", draft_id=d)["output"]["revision"] == r2


def test_old_draft_cannot_replace_a_project_edited_in_capcut(client, tmp_path, monkeypatch):
    import save_draft_impl
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(save_draft_impl, "find_capcut_projects_dir", lambda: str(projects))
    d = new_draft(client)
    call(client, "/add_text", draft_id=d, text="A", start=0, end=1)
    assert call(client, "/save_draft", draft_id=d, project_name="Mio")["success"]
    # untouched: saving again replaces it as before
    assert call(client, "/save_draft", draft_id=d, project_name="Mio")["success"]

    content = projects / "Mio" / "draft_info.json"
    content.write_text(json.dumps({"edited": "in CapCut"}))  # what CapCut does when you edit and close
    refused = call(client, "/save_draft", draft_id=d, project_name="Mio")
    assert refused["success"] is False and "changed in CapCut" in refused["error"]
    assert json.loads(content.read_text()) == {"edited": "in CapCut"}

    forced = call(client, "/save_draft", draft_id=d, project_name="Mio", overwrite=True)
    assert forced["success"]
    backup = Path(forced["output"]["backups"][0])
    assert json.loads((backup / "draft_info.json").read_text()) == {"edited": "in CapCut"}
