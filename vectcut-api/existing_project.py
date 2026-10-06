# Added in capcut-mcp-kit (2026): open a project made in CapCut and add to it without touching what
# is already there. See NOTICE at the repository root.
"""
Existing CapCut projects.

capcut_open_project reads a project and starts a draft for it. The draft holds only what is
added (new tracks with their materials); the project's own timeline is kept as the exact bytes
read. Saving rebuilds the timeline as: the original content, unchanged, plus the additions
appended (new tracks on top, new materials at the end of their lists). Nothing already in the
project is modified, and saving refuses to run if:

- the project files were not a faithful round trip (they could not be rewritten byte for byte, or
  at least to identical content),
- the timeline copies CapCut keeps (draft_info.json, template-2.tmp, Timelines/<id>/...) disagree,
- the project changed on disk since it was opened or last saved (edited in CapCut meanwhile),
- CapCut is open (it would write its copy back over the saved project when it closes).

Before writing, the whole project folder is copied to the backups folder.
"""

import copy
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid

import pyJianYingDraft as draft
from draft_cache import update_cache
from draft_store import persist

CONTENT_FILE = "draft_info.json"
MIRROR_FILE = "template-2.tmp"

SERIALIZERS = {
    # CapCut's own format first
    "compact": lambda d: json.dumps(d, ensure_ascii=False, separators=(",", ":")),
    "indent4": lambda d: json.dumps(d, ensure_ascii=False, indent=4),
    "indent2": lambda d: json.dumps(d, ensure_ascii=False, indent=2),
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _errors():
    # Imported lazily: save_draft_impl imports this module
    import save_draft_impl
    return save_draft_impl


def content_files(project_dir: str):
    """The timeline file CapCut reads and every identical copy of it.
    Returns (raw bytes, [paths relative to project_dir])."""
    sd = _errors()
    candidates = []
    try:
        with open(os.path.join(project_dir, "Timelines", "project.json"), encoding="utf-8") as f:
            main_id = json.load(f).get("main_timeline_id")
    except (OSError, ValueError, AttributeError):
        main_id = None
    if main_id:
        timeline_dir = sd.child_dir(os.path.join(project_dir, "Timelines"), str(main_id))
        candidates += [os.path.join(timeline_dir, CONTENT_FILE), os.path.join(timeline_dir, MIRROR_FILE)]
    root_content = os.path.join(project_dir, CONTENT_FILE)
    candidates += [root_content, os.path.join(project_dir, MIRROR_FILE)]
    present = [p for p in candidates if os.path.isfile(p) and not os.path.islink(p)]
    if not present:
        raise sd.SaveDraftError(f"'{os.path.basename(project_dir)}' is not a CapCut project (no {CONTENT_FILE})")
    raw = _read(present[0])  # the main timeline's file when there is one
    copies = [p for p in present if _read(p) == raw]
    if os.path.isfile(root_content) and root_content not in copies:
        raise sd.SaveDraftError(
            f"The timeline files of '{os.path.basename(project_dir)}' disagree with each other: open the "
            f"project in CapCut, close it, and try again")
    return raw, [os.path.relpath(p, project_dir) for p in copies]


def round_trip(raw: bytes):
    """Parse the timeline and find how to write it back. Returns (content, serializer name, exact)."""
    sd = _errors()
    try:
        content = json.loads(raw)
    except ValueError as e:
        raise sd.SaveDraftError(f"The project timeline is not valid JSON: {e}")
    if not (isinstance(content, dict) and isinstance(content.get("tracks"), list)
            and isinstance(content.get("materials"), dict) and isinstance(content.get("canvas_config"), dict)):
        raise sd.SaveDraftError("The project timeline has an unexpected structure: it will not be edited")
    for name, dump in SERIALIZERS.items():
        if dump(content).encode("utf-8") == raw:
            return content, name, True
    if json.loads(SERIALIZERS["compact"](content)) != content:
        raise sd.SaveDraftError("The project timeline cannot be rewritten without changing it: it will not be edited")
    return content, "compact", False


def _summary(content: dict) -> list:
    tracks = []
    for t in content["tracks"]:
        segs = t.get("segments") or []
        end = max((s.get("target_timerange", {}).get("start", 0) + s.get("target_timerange", {}).get("duration", 0)
                   for s in segs), default=0)
        tracks.append({"type": t.get("type"), "name": t.get("name") or "", "clips": len(segs),
                       "end": round(end / 1e6, 3)})
    return tracks


def open_project(project_name: str) -> dict:
    sd = _errors()
    projects_dir = sd.find_capcut_projects_dir()
    if not projects_dir:
        raise sd.SaveDraftError("CapCut's projects folder was not found")
    name = sd.validate_folder_name(project_name)
    project_dir = sd.child_dir(projects_dir, name)
    if not os.path.isdir(project_dir) or os.path.islink(project_dir):
        raise sd.SaveDraftError(f"No CapCut project named '{name}'")
    raw, copies = content_files(project_dir)
    content, fmt, exact = round_trip(raw)

    canvas = content["canvas_config"]
    fps = content.get("fps") or 30
    script = draft.Script_file(int(canvas.get("width") or 1920), int(canvas.get("height") or 1080),
                               int(round(float(fps))))
    script.base_project = {"dir": project_dir, "name": name, "raw": raw, "copies": copies,
                           "format": fmt, "written_sha": _sha(raw)}
    draft_id = f"dfd_cat_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    update_cache(draft_id, script)
    persist(draft_id, script)
    return {"draft_id": draft_id, "project_name": name, "path": project_dir,
            "width": script.width, "height": script.height, "fps": content.get("fps"),
            "duration": round((content.get("duration") or 0) / 1e6, 3),
            "tracks": _summary(content), "exact_round_trip": exact,
            "note": "Additions go on new tracks above the existing ones; existing clips are not changed. "
                    "Quit CapCut before saving."}


def merge(original: dict, additions: dict):
    """The original content with the additions appended. Returns (merged, added material ids,
    number of added tracks)."""
    sd = _errors()
    merged = copy.deepcopy(original)
    used_ids = {m.get("id") for items in original["materials"].values() if isinstance(items, list)
                for m in items if isinstance(m, dict)}
    added_ids = set()
    for key, items in additions.get("materials", {}).items():
        if not isinstance(items, list) or not items:
            continue
        target = merged["materials"].setdefault(key, [])
        if not isinstance(target, list):
            raise sd.SaveDraftError(f"Unexpected materials.{key} in the project: nothing was written")
        for item in items:
            if item.get("id") in used_ids or item.get("id") in added_ids:
                raise sd.SaveDraftError(f"Material id {item.get('id')} already exists in the project: nothing was written")
            target.append(item)
            added_ids.add(item.get("id"))
    new_tracks = [t for t in additions.get("tracks", []) if t.get("segments")]
    merged["tracks"].extend(new_tracks)
    merged["duration"] = max(original.get("duration") or 0, additions.get("duration") or 0)
    return merged, added_ids, len(new_tracks)


def verify_untouched(original: dict, merged: dict, added_ids: set, added_tracks: int):
    """Removing the additions from merged must give back the original exactly."""
    sd = _errors()
    check = copy.deepcopy(merged)
    check["tracks"] = check["tracks"][:len(check["tracks"]) - added_tracks]
    for key in list(check["materials"]):
        items = check["materials"][key]
        if isinstance(items, list):
            check["materials"][key] = [m for m in items if not (isinstance(m, dict) and m.get("id") in added_ids)]
            if key not in original["materials"] and not check["materials"][key]:
                del check["materials"][key]
    if "duration" in original:
        check["duration"] = original["duration"]
    else:
        check.pop("duration", None)
    if check != original:
        raise sd.SaveDraftError("Internal check failed: the save would have changed existing content; nothing was written")


def _clone_tree(src: str, dst: str):
    """Copy a folder, as copy-on-write clones where the filesystem supports it (APFS)."""
    if hasattr(os, "uname") and os.uname().sysname == "Darwin":
        if subprocess.run(["cp", "-c", "-R", src, dst], capture_output=True).returncode == 0:
            return
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst, symlinks=True)


def _write_atomic(path: str, data: bytes):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".capcut-mcp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _update_meta(project_dir: str, duration: int) -> bytes:
    """New bytes for draft_meta_info.json with only the modification time and duration changed."""
    path = os.path.join(project_dir, "draft_meta_info.json")
    raw = _read(path)
    meta = json.loads(raw)
    fmt = next((n for n, dump in SERIALIZERS.items() if dump(meta).encode("utf-8") == raw), "compact")
    meta["tm_draft_modified"] = int(time.time() * 1_000_000)
    meta["tm_duration"] = duration
    return SERIALIZERS[fmt](meta).encode("utf-8")


def save_into_existing(draft_id: str, script, task_id: str) -> dict:
    sd = _errors()
    base = script.base_project
    project_dir, name = base["dir"], base["name"]
    if sd.capcut_is_running():
        raise sd.SaveDraftError(f"CapCut is open: quit CapCut before saving into '{name}', otherwise it writes "
                                f"its own copy back over the project when it closes. Nothing was written.")
    for rel in base["copies"]:
        try:
            current = _read(os.path.join(project_dir, rel))
        except OSError:
            current = b""
        if _sha(current) != base["written_sha"]:
            raise sd.SaveDraftError(f"'{name}' changed since it was opened (edited in CapCut?): open it again with "
                                    f"capcut_open_project. Nothing was written.")

    original = json.loads(base["raw"])
    sd.update_media_metadata(script, task_id)  # also applies pending keyframes

    projects_dir = os.path.dirname(project_dir)
    stage = sd.child_dir(projects_dir, f".capcut-mcp-assets-{uuid.uuid4().hex[:8]}")
    os.makedirs(stage)
    try:
        sd.copy_assets(script, task_id, projects_dir, name, stage)
        from draft_profiles import get_draft_profile
        additions = json.loads(script.dumps(get_draft_profile()))
        merged, added_ids, added_tracks = merge(original, additions)
        verify_untouched(original, merged, added_ids, added_tracks)
        data = SERIALIZERS[base["format"]](merged).encode("utf-8")
        meta = _update_meta(project_dir, merged["duration"]) \
            if os.path.isfile(os.path.join(project_dir, "draft_meta_info.json")) else None

        # Back up the whole project, then put the new files in place
        os.makedirs(sd.backup_root(), exist_ok=True)
        backup = os.path.join(sd.backup_root(), f"{name} {time.strftime('%Y%m%d-%H%M%S')} {uuid.uuid4().hex[:4]}")
        _clone_tree(project_dir, backup)
        try:
            for dirpath, _, files in os.walk(stage):
                rel_dir = os.path.relpath(dirpath, stage)
                for fname in files:
                    dest = os.path.join(project_dir, rel_dir, fname)
                    if not os.path.exists(dest):  # same name = same source file
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        shutil.move(os.path.join(dirpath, fname), dest)
            for rel in base["copies"]:
                _write_atomic(os.path.join(project_dir, rel), data)
            if meta is not None:
                _write_atomic(os.path.join(project_dir, "draft_meta_info.json"), meta)
        except BaseException:
            for rel in base["copies"] + ["draft_meta_info.json"]:
                src = os.path.join(backup, rel)
                if os.path.isfile(src):
                    shutil.copy2(src, os.path.join(project_dir, rel))
            raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    base["written_sha"] = _sha(data)
    sd.update_task_fields(task_id, status="completed", progress=100, message="Saved into existing project")
    return {"draft_url": project_dir, "backups": [backup], "added_tracks": added_tracks}
