# Added in capcut-mcp-kit (2026): adding to a project made in CapCut never changes what is already there.
# See NOTICE at the repository root. The fixture is synthetic: it mimics CapCut 9.1's layout and
# carries fields this kit does not know, which must survive untouched.
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from draft_cache import DRAFT_CACHE

HOST = {"Host": "127.0.0.1:9001", "X-CapCut-Kit-Token": "test-token"}
COPIES = ["draft_info.json", "template-2.tmp", "Timelines/TL-1/draft_info.json", "Timelines/TL-1/template-2.tmp"]
UNTOUCHED = ["draft_info.json.bak", "Timelines/TL-1/template.tmp", "Timelines/project.json", "key_value.json"]


def capcut_content():
    return {
        "id": "TL-1", "fps": 30.0, "duration": 4_000_000, "version": 360000, "new_version": "179.0.0",
        "canvas_config": {"width": 1080, "height": 1920, "ratio": "original", "background": None},
        "future_top_level": {"nested": [1, 2.5, "è", None]},
        "keyframe_graph_list": [], "relationships": [{"type": "unknown", "id_list": ["a"]}],
        "materials": {
            "videos": [{"id": "MAT-V1", "path": "/somewhere/clip.mov", "type": "video", "duration": 4_000_000,
                        "future_video_field": {"crop": [0, 0, 1, 1]}}],
            "speeds": [{"id": "SPD-1", "speed": 1.0, "type": "speed"}],
            "texts": [],
            "a_list_this_kit_does_not_know": [{"id": "X-1", "blob": "ok"}],
        },
        "tracks": [{
            "id": "TRK-1", "type": "video", "name": "", "flag": 0, "attribute": 0, "is_default_name": True,
            "segments": [{"id": "SEG-1", "material_id": "MAT-V1", "render_index": 0,
                          "target_timerange": {"start": 0, "duration": 4_000_000},
                          "source_timerange": {"start": 0, "duration": 4_000_000},
                          "extra_material_refs": ["SPD-1"], "future_segment_field": True}],
        }],
    }


def write_project(projects, name, content, fmt="compact"):
    from existing_project import SERIALIZERS
    root = projects / name
    (root / "Timelines" / "TL-1").mkdir(parents=True)
    data = SERIALIZERS[fmt](content).encode()
    for rel in COPIES:
        (root / rel).write_bytes(data)
    (root / "draft_info.json.bak").write_text('{"older":"version"}')
    (root / "Timelines" / "TL-1" / "template.tmp").write_text("different format")
    (root / "Timelines" / "project.json").write_text(json.dumps({"main_timeline_id": "TL-1", "timelines": [{"id": "TL-1"}]}))
    (root / "key_value.json").write_text('{"SEG-1":{"user":"data"}}')
    (root / "draft_meta_info.json").write_text(json.dumps({"draft_name": name, "draft_id": "META-ID", "tm_duration": 4_000_000,
                                                           "tm_draft_modified": 1, "draft_cover": "cover.jpg"},
                                                          separators=(",", ":")))
    return root


@pytest.fixture
def env(monkeypatch, tmp_path):
    import save_draft_impl
    from types import SimpleNamespace
    projects = tmp_path / "projects"
    projects.mkdir()
    state = SimpleNamespace(projects=projects, running=False, backups=tmp_path / "backups")
    monkeypatch.setattr(save_draft_impl, "find_capcut_projects_dir", lambda: str(projects))
    monkeypatch.setattr(save_draft_impl, "capcut_is_running", lambda: state.running)
    monkeypatch.setenv("CAPCUT_MCP_BACKUP_DIR", str(state.backups))
    return state


@pytest.fixture
def client():
    import capcut_server
    return capcut_server.app.test_client()


def call(client, route, **payload):
    return client.post(route, json=payload, headers=HOST).get_json()


def snapshot(root):
    return {rel: (root / rel).read_bytes() for rel in COPIES + UNTOUCHED}


def strip_additions(merged, original):
    """merged without the tracks/materials that are not in original."""
    out = json.loads(json.dumps(merged))
    out["tracks"] = out["tracks"][:len(original["tracks"])]
    for key, items in list(out["materials"].items()):
        if key not in original["materials"]:
            del out["materials"][key]
        else:
            out["materials"][key] = items[:len(original["materials"][key])]
    out["duration"] = original["duration"]
    return out


def test_open_reports_tracks_and_exact_round_trip(env, client):
    write_project(env.projects, "Mio progetto", capcut_content())
    body = call(client, "/open_project", project_name="Mio progetto")
    assert body["success"], body
    out = body["output"]
    assert out["exact_round_trip"] is True
    assert (out["width"], out["height"], out["duration"]) == (1080, 1920, 4.0)
    assert out["tracks"] == [{"type": "video", "name": "", "clips": 1, "end": 4.0}]


def test_adding_text_keeps_every_existing_byte_of_content(env, client):
    root = write_project(env.projects, "Mio progetto", capcut_content())
    before = snapshot(root)
    draft_id = call(client, "/open_project", project_name="Mio progetto")["output"]["draft_id"]
    assert call(client, "/add_text", draft_id=draft_id, text="Titolo", start=1, end=3)["success"]

    body = call(client, "/save_draft", draft_id=draft_id)

    assert body["success"], body
    after = snapshot(root)
    assert len({after[rel] for rel in COPIES}) == 1  # all timeline copies written identically
    for rel in UNTOUCHED:
        assert after[rel] == before[rel]
    original, merged = capcut_content(), json.loads(after["draft_info.json"])
    assert strip_additions(merged, original) == original
    assert [t["type"] for t in merged["tracks"]] == ["video", "text"]
    assert merged["tracks"][1]["segments"][0]["target_timerange"] == {"start": 1_000_000, "duration": 2_000_000}
    # written in CapCut's compact format
    assert after["draft_info.json"].startswith(b'{"id":"TL-1",')
    meta = json.loads((root / "draft_meta_info.json").read_text())
    assert meta["draft_id"] == "META-ID" and meta["draft_cover"] == "cover.jpg" and meta["tm_draft_modified"] > 1
    backup = Path(body["output"]["backups"][0])
    assert (backup / "draft_info.json").read_bytes() == before["draft_info.json"]


def test_saving_twice_adds_each_clip_once(env, client):
    write_project(env.projects, "Due volte", capcut_content())
    draft_id = call(client, "/open_project", project_name="Due volte")["output"]["draft_id"]
    assert call(client, "/add_text", draft_id=draft_id, text="Uno", start=0, end=1)["success"]
    assert call(client, "/save_draft", draft_id=draft_id)["success"]
    assert call(client, "/add_text", draft_id=draft_id, text="Due", start=1, end=2, track_name="t2")["success"]
    assert call(client, "/save_draft", draft_id=draft_id)["success"]

    merged = json.loads((env.projects / "Due volte" / "draft_info.json").read_text())
    assert [t["type"] for t in merged["tracks"]] == ["video", "text", "text"]
    assert len(merged["materials"]["texts"]) == 2
    assert strip_additions(merged, capcut_content()) == capcut_content()


def test_save_refused_while_capcut_is_open(env, client):
    root = write_project(env.projects, "Aperto", capcut_content())
    before = snapshot(root)
    draft_id = call(client, "/open_project", project_name="Aperto")["output"]["draft_id"]
    call(client, "/add_text", draft_id=draft_id, text="X", start=0, end=1)
    env.running = True

    body = call(client, "/save_draft", draft_id=draft_id)

    assert body["success"] is False and "CapCut is open" in body["error"]
    assert snapshot(root) == before
    assert not env.backups.exists()


def test_save_refused_if_project_changed_since_open(env, client):
    root = write_project(env.projects, "Cambiato", capcut_content())
    draft_id = call(client, "/open_project", project_name="Cambiato")["output"]["draft_id"]
    call(client, "/add_text", draft_id=draft_id, text="X", start=0, end=1)
    edited = capcut_content()
    edited["duration"] = 5_000_000
    from existing_project import SERIALIZERS
    for rel in COPIES:
        (root / rel).write_bytes(SERIALIZERS["compact"](edited).encode())
    before = snapshot(root)

    body = call(client, "/save_draft", draft_id=draft_id)

    assert body["success"] is False and "changed since it was opened" in body["error"]
    assert snapshot(root) == before


def test_disagreeing_timeline_copies_are_refused(env, client):
    root = write_project(env.projects, "Disallineato", capcut_content())
    (root / "draft_info.json").write_text('{"something":"else","tracks":[],"materials":{},"canvas_config":{}}')
    body = call(client, "/open_project", project_name="Disallineato")
    assert body["success"] is False and "disagree" in body["error"]


def test_indented_project_keeps_its_format(env, client):
    root = write_project(env.projects, "Indentato", capcut_content(), fmt="indent2")
    out = call(client, "/open_project", project_name="Indentato")["output"]
    assert out["exact_round_trip"] is True
    call(client, "/add_text", draft_id=out["draft_id"], text="X", start=0, end=1)
    assert call(client, "/save_draft", draft_id=out["draft_id"])["success"]
    assert (root / "draft_info.json").read_text().startswith('{\n  "id": "TL-1"')


def test_project_name_must_match_when_saving(env, client):
    write_project(env.projects, "Nome", capcut_content())
    draft_id = call(client, "/open_project", project_name="Nome")["output"]["draft_id"]
    body = call(client, "/save_draft", draft_id=draft_id, project_name="Altro")
    assert body["success"] is False and "existing project 'Nome'" in body["error"]
    assert not (env.projects / "Altro").exists()


@pytest.mark.parametrize("name", ["..", "Non esiste", "../projects"])
def test_bad_or_missing_project_names(env, client, name):
    body = call(client, "/open_project", project_name=name)
    assert body["success"] is False


def test_open_project_survives_a_restart(env, client):
    write_project(env.projects, "Riavvio", capcut_content())
    draft_id = call(client, "/open_project", project_name="Riavvio")["output"]["draft_id"]
    call(client, "/add_text", draft_id=draft_id, text="X", start=0, end=1)
    DRAFT_CACHE.clear()
    assert call(client, "/save_draft", draft_id=draft_id)["success"]


def test_added_video_is_copied_into_the_project(env, client, tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    clip = tmp_path / "broll.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=320x240:d=2",
                    str(clip)], check=True)
    root = write_project(env.projects, "Con video", capcut_content())
    draft_id = call(client, "/open_project", project_name="Con video")["output"]["draft_id"]
    assert call(client, "/add_video", draft_id=draft_id, video_url=str(clip), start=0, end=2,
                target_start=1, track_name="broll")["success"]

    assert call(client, "/save_draft", draft_id=draft_id)["success"]

    merged = json.loads((root / "draft_info.json").read_text())
    new_video = merged["materials"]["videos"][-1]
    assert new_video["path"].startswith(str(root / "assets" / "video"))
    assert Path(new_video["path"]).read_bytes() == clip.read_bytes()
    assert not [p for p in env.projects.iterdir() if p.name.startswith(".capcut-mcp-")]
