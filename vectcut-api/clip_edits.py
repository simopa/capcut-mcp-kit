# Added in capcut-mcp-kit (2026): the clips of a project opened with capcut_open_project, which of
# them can be edited, and the log of edits applied to them at save time. See NOTICE at the repository root.
"""
Clips of an existing project.

The project's own timeline (base_project["raw"], the exact bytes read at opening) stays the source
of truth. An edit is recorded in the draft (base_project["edits"]) as the segment it changes and,
for each field of that segment, the value it expects to find and the value it writes. At save time
the edits are applied to the original content in order (apply_edits), each one checking that its
fields still hold the expected values, before the additions are appended; verify_untouched then
puts back every declared value, removes the additions and must find the original exactly. So an
edit can change nothing but the fields it declares, on the segment it names.

Each kind of clip has an adapter: what it reads from a segment and how it writes it back. A clip
is editable only if (clip_verdict):
- its id is unique in the project,
- it is of a kind an adapter handles (today: video, photo and audio clips, for timing),
- everything it refers to is a material the kit knows,
- its adapter's round trip (read, write back unchanged) gives back the segment exactly.
Anything else is listed with the reason it is locked.
"""

import copy
import json

# Materials a timed media clip may refer to (extra_material_refs), as seen in CapCut 9.1 projects
KNOWN_REFS = {"speeds", "placeholder_infos", "canvases", "sound_channel_mappings", "material_colors",
              "vocal_separations", "transitions", "material_animations", "loudnesses", "audio_fades"}
TIMED_KINDS = {"video", "photo", "audio"}


def _errors():
    import save_draft_impl  # imported lazily: it imports existing_project, which imports this module
    return save_draft_impl


def _materials(content: dict) -> dict:
    """{material id: (list name, material)} for every material of the project."""
    out = {}
    for key, items in content.get("materials", {}).items():
        if isinstance(items, list):
            for m in items:
                if isinstance(m, dict) and "id" in m:
                    out.setdefault(m["id"], (key, m))
    return out


def _segments(content: dict):
    """([(track index, track, segment)] in timeline order of tracks, ids used more than once)."""
    found, seen, dups = [], set(), set()
    for ti, track in enumerate(content.get("tracks", [])):
        for seg in track.get("segments") or []:
            sid = seg.get("id")
            if sid in seen:
                dups.add(sid)
            seen.add(sid)
            found.append((ti, track, seg))
    return found, dups


def _kind(seg: dict, mats: dict) -> str:
    key, material = mats.get(seg.get("material_id"), (None, {}))
    if key == "videos":
        return "photo" if material.get("type") == "photo" else "video"
    return {"audios": "audio", "texts": "text", "stickers": "sticker", "video_effects": "effect"}.get(key, key or "unknown")


# --- Timing adapter -------------------------------------------------------------------------------

def read_timing(seg: dict) -> dict:
    """What the timing adapter edits: where the clip is on the timeline, which part of its file it
    plays, and the offsets of its keyframes (relative to the clip's start)."""
    t, s = seg["target_timerange"], seg["source_timerange"]
    return {"target": [t["start"], t["duration"]], "source": [s["start"], s["duration"]],
            "keyframes": [[k["time_offset"] for k in kl.get("keyframe_list", [])]
                          for kl in seg.get("common_keyframes") or []]}


def write_timing(seg: dict, timing: dict) -> dict:
    """A copy of seg with the timing written back; nothing else changes."""
    out = copy.deepcopy(seg)
    out["target_timerange"]["start"], out["target_timerange"]["duration"] = timing["target"]
    out["source_timerange"]["start"], out["source_timerange"]["duration"] = timing["source"]
    for kl, offsets in zip(out.get("common_keyframes") or [], timing["keyframes"]):
        for k, offset in zip(kl.get("keyframe_list", []), offsets):
            k["time_offset"] = offset
    return out


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def clip_verdict(seg: dict, kind: str, mats: dict, dups: set):
    """None if the clip can be edited, else why it is locked."""
    if seg.get("id") in dups or not seg.get("id"):
        return "its id is not unique in the project"
    if kind not in TIMED_KINDS:
        return f"{kind} clips cannot be edited yet"
    t, s = seg.get("target_timerange"), seg.get("source_timerange")
    if not (isinstance(t, dict) and isinstance(s, dict) and all(_is_int(r.get(k)) for r in (t, s)
                                                                for k in ("start", "duration"))):
        return "its timing is not in the expected form"
    speeds = []
    for ref in seg.get("extra_material_refs") or []:
        key, material = mats.get(ref, (None, None))
        if key not in KNOWN_REFS:
            return f"it refers to a material the kit does not know ({key or 'missing'})"
        if key == "speeds":
            if material.get("curve_speed"):
                return "its speed is not a constant 1×"
            speeds.append(material.get("speed"))
    speed = seg["speed"] if "speed" in seg else next((v for v in speeds if v is not None), None)
    if speed is None:
        return "its speed is not recorded"
    if speed not in (1, 1.0) or any(v not in (None, 1, 1.0) for v in speeds):
        return "its speed is not a constant 1×"
    if abs(t["duration"] - s["duration"]) > 1:  # 1 µs: rounding of a cut, kept as it is by edits
        return "its timeline and source durations disagree"
    rt = seg.get("render_timerange")
    if rt not in (None, {}, {"start": 0, "duration": 0}):
        return "it has a render range the kit does not handle"
    for kl in seg.get("common_keyframes") or []:
        for k in kl.get("keyframe_list") or []:
            if k.get("curveType") != "Line" or not _is_int(k.get("time_offset")):
                return "it has keyframes with curves the kit does not handle"
    if write_timing(seg, read_timing(seg)) != seg:
        return "the kit cannot rewrite it unchanged"
    return None


# --- Edit log -------------------------------------------------------------------------------------

def apply_edits(content: dict, edits: list) -> dict:
    """A copy of content with the edits applied in order. Each edit must find every field it
    changes at the value it expects; the project duration follows the clips."""
    sd = _errors()
    out = copy.deepcopy(content)
    if not edits:
        return out
    found, dups = _segments(out)
    by_id = {seg.get("id"): seg for _, _, seg in found}
    for edit in edits:
        sid = edit["segment_id"]
        if sid in dups or sid not in by_id:
            raise sd.SaveDraftError(f"Clip {sid} is no longer in the project (or not only once): nothing was written")
        seg = by_id[sid]
        for field, change in edit["fields"].items():
            if field not in seg or seg[field] != change["old"]:
                raise sd.SaveDraftError(f"Clip {sid} changed since it was edited ({field}): nothing was written")
            seg[field] = copy.deepcopy(change["new"])
    out["duration"] = max((seg["target_timerange"]["start"] + seg["target_timerange"]["duration"]
                           for _, _, seg in found if isinstance(seg.get("target_timerange"), dict)), default=0)
    return out


def revert_edits(content: dict, edits: list):
    """Put back, in place, the values the edits replaced, checking each field holds what the edit
    wrote. Raises if anything does not match (used by the check before writing)."""
    sd = _errors()
    found, dups = _segments(content)
    by_id = {seg.get("id"): seg for _, _, seg in found}
    for edit in reversed(edits):
        seg = by_id.get(edit["segment_id"])
        if seg is None or edit["segment_id"] in dups:
            raise sd.SaveDraftError("Internal check failed: an edited clip is missing; nothing was written")
        for field, change in edit["fields"].items():
            if seg.get(field) != change["new"]:
                raise sd.SaveDraftError("Internal check failed: an edited clip does not hold its edit; nothing was written")
            seg[field] = copy.deepcopy(change["old"])


def current_content(base: dict) -> dict:
    """The project's timeline as the draft will write it, before the additions."""
    return apply_edits(json.loads(base["raw"]), base.get("edits") or [])


def record_edit(base: dict, segment_id: str, fields: dict) -> dict:
    """Add an edit to the draft's log: the new values of some fields of one editable clip. The
    values it expects are those the clip has now (with the earlier edits). Returns the edit."""
    sd = _errors()
    content = current_content(base)
    mats = _materials(content)
    found, dups = _segments(content)
    seg = next((s for _, _, s in found if s.get("id") == segment_id), None)
    if seg is None:
        raise sd.SaveDraftError(f"No clip {segment_id} in '{base['name']}': list the clips with capcut_list_clips")
    reason = clip_verdict(seg, _kind(seg, mats), mats, dups)
    if reason:
        raise sd.SaveDraftError(f"Clip {segment_id} cannot be edited: {reason}")
    edit = {"segment_id": segment_id,
            "fields": {f: {"old": copy.deepcopy(seg[f]), "new": copy.deepcopy(v)} for f, v in fields.items()
                       if f in seg and seg[f] != v}}
    if not edit["fields"]:
        raise sd.SaveDraftError(f"Clip {segment_id} would not change")
    apply_edits(content, [edit])  # the edit applies to what the draft holds now
    base.setdefault("edits", []).append(edit)
    return edit


# --- Inventory ------------------------------------------------------------------------------------

def _label(seg: dict, mats: dict) -> str:
    key, material = mats.get(seg.get("material_id"), (None, {}))
    if key == "texts":
        try:
            text = json.loads(material.get("content") or "{}").get("text") or ""
        except (ValueError, AttributeError):
            text = ""
        return text if len(text) <= 60 else text[:57] + "..."
    path = material.get("path") or material.get("material_name") or material.get("name") or ""
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def inventory(base: dict, kind: str = None, editable_only: bool = False, offset: int = 0, limit: int = 100) -> dict:
    """The project's own clips (with the draft's edits applied), in track order, each with whether
    it can be edited and, if not, why."""
    content = current_content(base)
    mats = _materials(content)
    found, dups = _segments(content)
    clips = []
    for ti, track, seg in found:
        k = _kind(seg, mats)
        if kind and k != kind:
            continue
        reason = clip_verdict(seg, k, mats, dups)
        if editable_only and reason:
            continue
        t, s = seg.get("target_timerange") or {}, seg.get("source_timerange") or {}
        refs = [mats.get(r, (None,))[0] for r in seg.get("extra_material_refs") or []]
        clip = {"id": seg.get("id"), "track": ti, "track_type": track.get("type"), "track_name": track.get("name") or "",
                "kind": k, "name": _label(seg, mats),
                "start": round(t.get("start", 0) / 1e6, 3), "end": round((t.get("start", 0) + t.get("duration", 0)) / 1e6, 3),
                "keyframes": sorted({kl.get("property_type") for kl in seg.get("common_keyframes") or []}),
                "transition": "transitions" in refs, "animation": "material_animations" in refs,
                "editable": reason is None, "locked_reason": reason}
        if s:
            clip["source_start"] = round(s.get("start", 0) / 1e6, 3)
            clip["source_end"] = round((s.get("start", 0) + s.get("duration", 0)) / 1e6, 3)
        clips.append(clip)
    offset, limit = max(0, int(offset)), max(1, min(int(limit), 500))
    page = clips[offset:offset + limit]
    return {"project_name": base["name"], "total": len(clips), "offset": offset, "clips": page,
            "next_offset": offset + limit if offset + limit < len(clips) else None,
            "edits": len(base.get("edits") or [])}
