# Added in capcut-mcp-kit (2026): every backend reply the MCP server reads matches the contract it
# expects (tests/contracts.json, exported from capcut-mcp-server/src/contracts.ts by `npm run
# contracts`). A renamed or retyped field fails here instead of in a user's session.
# See NOTICE at the repository root.
import json
import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from test_existing_project import capcut_content, write_project

with open(os.path.join(os.path.dirname(__file__), "contracts.json"), encoding="utf-8") as f:
    CONTRACTS = json.load(f)

HOST = {"Host": "127.0.0.1:9001", "X-CapCut-Kit-Token": "test-token"}
LIST_ENDPOINTS = ["/get_transition_types", "/get_intro_animation_types", "/get_outro_animation_types",
                  "/get_combo_animation_types", "/get_text_intro_types", "/get_text_outro_types",
                  "/get_video_scene_effect_types", "/get_video_character_effect_types", "/get_mask_types",
                  "/get_audio_effect_types", "/get_font_types"]


def _is(value, kind):
    return {
        "string": isinstance(value, str),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
        "array": isinstance(value, list),
        "object": isinstance(value, dict),
    }[kind]


def validate(schema, value, path="$"):
    """The JSON Schema subset zod-to-json-schema emits for these contracts."""
    if "anyOf" in schema:
        return [] if any(not validate(s, value, path) for s in schema["anyOf"]) else [f"{path}: matches no variant"]
    kinds = schema.get("type")
    if kinds is not None and not any(_is(value, k) for k in (kinds if isinstance(kinds, list) else [kinds])):
        return [f"{path}: expected {kinds}, got {type(value).__name__} {value!r:.60}"]
    errors = []
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in {schema['enum']}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: missing")
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                errors += validate(sub, value[key], f"{path}.{key}")
        if schema.get("additionalProperties") is False:
            errors += [f"{path}.{k}: unexpected" for k in value if k not in schema.get("properties", {})]
    if isinstance(value, list) and "items" in schema:
        items = schema["items"]
        if isinstance(items, list):
            if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", len(value)):
                errors.append(f"{path}: {len(value)} items")
            for i, (sub, v) in enumerate(zip(items, value)):
                errors += validate(sub, v, f"{path}[{i}]")
        else:
            for i, v in enumerate(value):
                errors += validate(items, v, f"{path}[{i}]")
    return errors


def test_validator_catches_drift():
    schema = CONTRACTS["CreateDraftResult"]
    assert validate(schema, {"draft_id": "d", "draft_url": "u", "width": 1, "height": 2, "fps": 30}) == []
    assert validate(schema, {"draft_id": "d", "draft_url": "u", "width": 1, "height": 2}) == ["$.fps: missing"]
    assert validate(schema, {"draft_id": "d", "draft_url": "u", "width": "1", "height": 2, "fps": 30})


@pytest.fixture
def env(monkeypatch, tmp_path):
    import capcut_server
    import save_draft_impl
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(save_draft_impl, "find_capcut_projects_dir", lambda: str(projects))
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    clip, png = tmp_path / "clip.mp4", tmp_path / "logo.png"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=4",
                    "-f", "lavfi", "-i", "sine=duration=4", "-shortest", str(clip)], check=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x64",
                    "-frames:v", "1", str(png)], check=True)
    return SimpleNamespace(projects=projects, clip=str(clip), png=str(png),
                           client=capcut_server.app.test_client())


def reply(env, route, contract, method="POST", **payload):
    if method == "GET":
        body = env.client.get(route, headers=HOST).get_json()
    else:
        body = env.client.post(route, json=payload, headers=HOST).get_json()
    assert body["success"], (route, body)
    errors = validate(CONTRACTS[contract], body["output"])
    assert errors == [], (route, errors)
    return body["output"]


def test_editing_routes_match_their_contracts(env, monkeypatch):
    d = reply(env, "/create_draft", "CreateDraftResult", width=1080, height=1920, fps=30)["draft_id"]
    reply(env, "/add_video", "DraftRefResult", draft_id=d, video_url=env.clip, start=0, end=4)
    reply(env, "/add_audio", "DraftRefResult", draft_id=d, audio_url=env.clip, start=0, end=4)
    reply(env, "/add_text", "DraftRefResult", draft_id=d, text="Ciao", start=0, end=2)
    moved = reply(env, "/add_text", "DraftRefResult", draft_id=d, text="Sopra", start=1, end=2)
    assert moved["moved_to_free_track"]
    reply(env, "/add_image", "DraftRefResult", draft_id=d, image_url=env.png, start=0, end=2)
    reply(env, "/add_subtitle", "DraftRefResult", draft_id=d, srt="1\n00:00:00,000 --> 00:00:01,000\nCiao\n")
    reply(env, "/add_effect", "DraftRefResult", draft_id=d, effect_type="Camera_Shake", start=0, end=1)
    reply(env, "/add_video_keyframe", "DraftRefResult", draft_id=d, track_name="video_main",
          property_type="alpha", time=1, value="0.5")
    reply(env, "/add_camera_move", "CameraMoveResult", draft_id=d, move="punch_in", start=1, end=2)
    reply(env, "/add_camera_move", "CameraMoveResult", draft_id=d, move="shake", start=2, end=3)
    reply(env, "/add_background_music", "MusicResult", draft_id=d, audio_url=env.clip)
    reply(env, "/timeline", "TimelineResult", draft_id=d)
    reply(env, "/get_duration", "DurationResult", url=env.clip)
    reply(env, "/save_draft", "SaveDraftResult", draft_id=d, project_name="Contratto")


def test_media_routes_match_their_contracts(env, monkeypatch):
    import media_analysis
    reply(env, "/detect_pauses", "SpeechRangesResult", path=env.clip, method="audio")
    d = reply(env, "/create_draft", "CreateDraftResult", width=1080, height=1920)["draft_id"]
    reply(env, "/add_video_without_pauses", "WithoutPausesResult", draft_id=d, video_url=env.clip, method="audio")
    monkeypatch.setattr(media_analysis, "timeline_words", lambda *a: [{"word": "Ciao.", "start": 0.2, "end": 0.6}])
    reply(env, "/add_auto_subtitles", "AutoSubtitlesResult", draft_id=d, video_url=env.clip)

    monkeypatch.setattr(media_analysis, "transcribe", lambda *a, **k: {"status": "running", "elapsed": 3})
    reply(env, "/transcribe", "TranscribeResult", path=env.clip, page={"from_time": 0})
    transcript = {"source": env.clip, "language": "it", "duration": 4.0, "engine": "test", "model": "turbo",
                  "segments": [{"start": 0, "end": 1, "text": "Ciao.", "words": []}]}
    monkeypatch.setattr(media_analysis, "transcribe", lambda *a, **k: {"status": "done", "transcript": transcript})
    reply(env, "/transcribe", "TranscribeResult", path=env.clip, page={"from_time": 0})


def test_project_routes_match_their_contracts(env):
    write_project(env.projects, "Esistente", capcut_content())
    projects = reply(env, "/list_projects", "ListProjectsResult", method="GET")
    assert [p["name"] for p in projects["projects"]] == ["Esistente"]
    d = reply(env, "/open_project", "OpenProjectResult", project_name="Esistente")["draft_id"]
    reply(env, "/add_text", "DraftRefResult", draft_id=d, text="Titolo", start=0, end=1)
    reply(env, "/timeline", "TimelineResult", draft_id=d)
    clips = reply(env, "/list_clips", "ClipListResult", draft_id=d)
    assert [c["id"] for c in clips["clips"]] == ["SEG-1"] and clips["clips"][0]["editable"]
    reply(env, "/edit_clip", "EditClipResult", draft_id=d, clip_id="SEG-1", trim_end=1)
    saved = reply(env, "/save_draft", "SaveDraftResult", draft_id=d)
    assert saved["added_tracks"] == 1


@pytest.mark.parametrize("endpoint", LIST_ENDPOINTS)
def test_catalog_routes_match_their_contract(env, endpoint):
    names = reply(env, endpoint, "ListTypesResult", method="GET")
    assert names
