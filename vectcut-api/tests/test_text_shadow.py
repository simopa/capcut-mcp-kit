# Added in capcut-mcp-kit (2026): a text shadow with styled ranges (issue #1, fixed in #2) must not fail
# the export, and every style entry carries the shadow. See NOTICE at the repository root.
import json

import pytest

import draft_store
from test_commit_safety import call, client, env  # noqa: F401 (fixtures)


def styles_of(d):
    content = json.loads(draft_store.get_draft(d).dumps())
    texts = content["materials"]["texts"]
    assert len(texts) == 1
    return json.loads(texts[0]["content"])["styles"]


@pytest.mark.parametrize("ranges, entries", [
    ([], 1),
    ([(6, 12)], 3),               # head, range, tail
    ([(2, 5), (8, 12)], 5),       # head, range, gap, range, tail
    ([(0, 18)], 1),
])
def test_a_shadow_covers_every_style_entry(env, client, ranges, entries):
    d = call(client, "/create_draft", width=1080, height=1920)["output"]["draft_id"]
    out = call(client, "/add_text", draft_id=d, text="Hello shadow world", start=0, end=3,
               shadow_enabled=True, shadow_color="#000000",
               text_styles=[{"start": a, "end": b, "style": {"color": "#FF0000", "bold": True}} for a, b in ranges])
    assert out["success"], out
    styles = styles_of(d)
    assert len(styles) == entries
    assert all(s.get("shadows") for s in styles)
    assert all(s["shadows"][0]["content"]["solid"]["color"] == [0, 0, 0] for s in styles)


def test_without_shadow_no_entry_has_one(env, client):
    d = call(client, "/create_draft", width=1080, height=1920)["output"]["draft_id"]
    out = call(client, "/add_text", draft_id=d, text="Hello shadow world", start=0, end=3,
               text_styles=[{"start": 2, "end": 5, "style": {"color": "#FF0000"}}, {"start": 8, "end": 12, "style": {}}])
    assert out["success"], out
    assert not any("shadows" in s for s in styles_of(d))
