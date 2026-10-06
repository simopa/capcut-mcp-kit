# Added in capcut-mcp-kit (2026): unknown names, out-of-range keyframes and fps are errors or applied, never silently ignored.
# See NOTICE at the repository root.
import shutil
import subprocess

import pytest

from draft_cache import DRAFT_CACHE

HOST = {"Host": "127.0.0.1:9001"}


@pytest.fixture
def client():
    import capcut_server
    return capcut_server.app.test_client()


def call(client, route, **payload):
    return client.post(route, json=payload, headers=HOST).get_json()


def new_draft(client, **kw):
    body = call(client, "/create_draft", width=1080, height=1920, **kw)
    assert body["success"], body
    return body["output"]["draft_id"]


@pytest.fixture
def clip(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    path = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=2",
                    "-f", "lavfi", "-i", "sine=duration=2", "-shortest", str(path)], check=True)
    return str(path)


def test_requested_fps_is_applied_and_returned(client):
    body = call(client, "/create_draft", width=1920, height=1080, fps=60)
    assert body["success"]
    assert body["output"]["fps"] == 60
    assert (body["output"]["width"], body["output"]["height"]) == (1920, 1080)
    assert DRAFT_CACHE[body["output"]["draft_id"]].fps == 60


def test_unsupported_fps_is_refused(client):
    body = call(client, "/create_draft", width=1080, height=1920, fps=45)
    assert body["success"] is False
    assert "fps" in body["error"]


def test_unknown_text_animation_is_an_error(client):
    draft_id = new_draft(client)
    body = call(client, "/add_text", draft_id=draft_id, text="Ciao", start=0, end=2,
                intro_animation="Not_a_real_animation")
    assert body["success"] is False
    assert "text_intro" in body["error"]
    assert not any(t.segments for t in DRAFT_CACHE[draft_id].tracks.values())


def test_unknown_audio_effect_is_an_error(client, clip):
    draft_id = new_draft(client)
    body = call(client, "/add_audio", draft_id=draft_id, audio_url=clip, start=0, end=2,
                effect_type="Not_a_real_effect")
    assert body["success"] is False
    assert "audio effect" in body["error"]


def test_keyframe_outside_any_clip_is_an_error(client, clip):
    draft_id = new_draft(client)
    assert call(client, "/add_video", draft_id=draft_id, video_url=clip, start=0, end=2)["success"]

    body = call(client, "/add_video_keyframe", draft_id=draft_id, track_name="video_main",
                property_type="alpha", time=100, value="0.5")

    assert body["success"] is False
    assert "No clip" in body["error"]
    assert DRAFT_CACHE[draft_id].tracks["video_main"].pending_keyframes == []


def test_one_bad_keyframe_in_a_batch_queues_none(client, clip):
    draft_id = new_draft(client)
    assert call(client, "/add_video", draft_id=draft_id, video_url=clip, start=0, end=2)["success"]

    body = call(client, "/add_video_keyframe", draft_id=draft_id, track_name="video_main",
                property_types=["alpha", "alpha"], times=[0.5, 50], values=["1.0", "0.2"])

    assert body["success"] is False
    assert DRAFT_CACHE[draft_id].tracks["video_main"].pending_keyframes == []


def test_valid_keyframes_are_queued_and_applied(client, clip):
    draft_id = new_draft(client)
    assert call(client, "/add_video", draft_id=draft_id, video_url=clip, start=0, end=2)["success"]
    body = call(client, "/add_video_keyframe", draft_id=draft_id, track_name="video_main",
                property_types=["alpha", "alpha"], times=[0, 1.5], values=["1.0", "0.2"])
    assert body["success"], body
    track = DRAFT_CACHE[draft_id].tracks["video_main"]
    track.process_pending_keyframes()
    assert track.pending_keyframes == []
    assert track.segments[0].common_keyframes


def test_pending_keyframe_that_cannot_be_applied_raises(client, clip):
    draft_id = new_draft(client)
    assert call(client, "/add_video", draft_id=draft_id, video_url=clip, start=0, end=2)["success"]
    track = DRAFT_CACHE[draft_id].tracks["video_main"]
    track.add_pending_keyframe("alpha", 99, "0.5")
    with pytest.raises(ValueError, match="99"):
        track.process_pending_keyframes()
