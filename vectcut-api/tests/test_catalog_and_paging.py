# Added in capcut-mcp-kit (2026): one camera-move catalog for backend and MCP server; transcripts are
# paged in the backend without word timings. See NOTICE at the repository root.
import json
import os

import media_analysis as ma


def test_camera_catalog_matches_the_implementation():
    import camera_moves as cm
    from pyJianYingDraft import CapCut_Video_scene_effect_type
    with open(os.path.join(os.path.dirname(cm.__file__), "camera_moves.json"), encoding="utf-8") as f:
        catalog = json.load(f)
    assert [m["name"] for m in catalog["keyframes"]] == list(cm.BUILDERS)
    for m in catalog["effects"]:
        assert CapCut_Video_scene_effect_type[m["capcut_effect"]]
    assert all(m["description"] for m in catalog["keyframes"] + catalog["effects"])


def transcript(n=40):
    segs = [{"start": i * 3.0, "end": i * 3.0 + 2.5, "text": f"Frase numero {i}.",
             "words": [{"word": "x", "start": i * 3.0, "end": i * 3.0 + 0.2}]} for i in range(n)]
    return {"source": "/v.mp4", "language": "it", "duration": n * 3.0, "engine": "test", "model": "turbo",
            "segments": segs}


def test_page_has_blocks_and_no_word_timings():
    page = ma.transcript_page(transcript(), 0, None, max_chars=200)
    assert page["blocks"] and "words" not in json.dumps(page)
    assert page["next_from"] == page["blocks"][-1]["end"]
    assert sum(len(b["text"]) + 30 for b in page["blocks"]) <= 200 + 200  # at least one block always fits


def test_pages_cover_everything_once():
    t, seen, cursor = transcript(), [], 0.0
    while cursor is not None:
        page = ma.transcript_page(t, cursor, None, max_chars=150)
        seen += [b["text"] for b in page["blocks"]]
        cursor = page["next_from"]
    joined = " ".join(seen)
    assert all(f"Frase numero {i}." in joined for i in range(40))
    assert len(seen) == len(set(seen))


def test_range_limits_the_page():
    page = ma.transcript_page(transcript(), 30, 45, max_chars=100000)
    assert page["blocks"][0]["end"] > 30 and page["blocks"][-1]["start"] < 45
    assert page["next_from"] is None


def test_transcribe_route_returns_a_page(monkeypatch):
    import capcut_server
    monkeypatch.setattr(ma, "transcribe", lambda *a, **k: {"status": "done", "transcript": transcript()})
    body = capcut_server.app.test_client().post(
        "/transcribe", json={"path": "/v.mp4", "page": {"from_time": 0, "max_chars": 300}},
        headers={"Host": "127.0.0.1:9001", "X-CapCut-Kit-Token": "test-token"}).get_json()
    assert body["success"] and body["output"]["page"]["blocks"]
    assert "transcript" not in body["output"]
