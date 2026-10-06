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
- CapCut is open, or it cannot be told whether it is (it would write its copy back over the saved
  project when it closes).

What "the project's timeline" is comes from a manifest taken at opening (timeline_manifest): the
selector Timelines/project.json, the main timeline it names, and every copy of it with its hash.
Opening refuses a selector naming no timeline, copies that disagree, and any path through a
symbolic link; saving refuses unless the manifest is still exactly the same, under the project's
lock, checked again just before the timeline is written.

Before writing, the whole project folder is copied to the backups folder and the write is
journalled: if it fails it is undone (timeline files from the backup, added media removed); if
the backend stops halfway, the next save of the draft (or the next start) settles it.
Media files are never overwritten: a different file with a name already in the project gets a
name of its own, and media an earlier save put there keeps the version its clips were made with.
"""

import copy
import filecmp
import hashlib
import json
import os
import shutil
import tempfile
import time
import uuid

import pyJianYingDraft as draft
from draft_store import committed_to_disk, journal_begin, journal_update, project_lock, store_new

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


def timeline_manifest(project_dir: str) -> dict:
    """Which files hold the timeline CapCut reads, and their content: {"selector": sha256 of
    Timelines/project.json or None, "main_timeline_id", "copies": {relative path: sha256}}.
    Refused if a path goes through a symbolic link, the selector names no timeline, or the copies
    disagree with each other. Saving compares the whole manifest with the one taken at opening."""
    sd = _errors()
    if os.path.islink(project_dir) or not os.path.isdir(project_dir):
        raise sd.SaveDraftError(f"'{os.path.basename(project_dir)}' is not a project folder")
    name = os.path.basename(project_dir)
    rels, selector, main_id = [CONTENT_FILE, MIRROR_FILE], None, None
    selector_path = sd.project_path(project_dir, "Timelines", "project.json")
    if os.path.lexists(selector_path):
        raw_selector = _read(selector_path)
        try:
            main_id = json.loads(raw_selector).get("main_timeline_id")
        except (ValueError, AttributeError):
            raise sd.SaveDraftError(f"Timelines/project.json of '{name}' is not valid: it will not be edited")
        if not isinstance(main_id, str) or not main_id:
            raise sd.SaveDraftError(f"Timelines/project.json of '{name}' names no main timeline: it will not be edited")
        sd.validate_folder_name(main_id, "main_timeline_id")
        if not os.path.isfile(sd.project_path(project_dir, "Timelines", main_id, CONTENT_FILE)):
            raise sd.SaveDraftError(f"The main timeline of '{name}' ({main_id}) does not exist: it will not be edited")
        rels = [f"Timelines/{main_id}/{CONTENT_FILE}", f"Timelines/{main_id}/{MIRROR_FILE}"] + rels
        selector = _sha(raw_selector)
    copies = {}
    for rel in rels:
        path = sd.project_path(project_dir, *rel.split("/"))
        if os.path.lexists(path):
            if not os.path.isfile(path):
                raise sd.SaveDraftError(f"{rel} in '{name}' is not a file: it will not be edited")
            copies[rel] = _sha(_read(path))
    if not copies:
        raise sd.SaveDraftError(f"'{name}' is not a CapCut project (no {CONTENT_FILE})")
    if len(set(copies.values())) != 1:
        raise sd.SaveDraftError(f"The timeline files of '{name}' disagree with each other: open the project in "
                                f"CapCut, close it, and try again")
    return {"selector": selector, "main_timeline_id": main_id, "copies": copies}


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
    with project_lock(project_dir):
        manifest = timeline_manifest(project_dir)
        raw = _read(sd.project_path(project_dir, *next(iter(manifest["copies"])).split("/")))
        if _sha(raw) != next(iter(manifest["copies"].values())):
            raise sd.SaveDraftError(f"'{name}' changed while it was being opened: try again")
    content, fmt, exact = round_trip(raw)

    canvas = content["canvas_config"]
    fps = content.get("fps") or 30
    script = draft.Script_file(int(canvas.get("width") or 1920), int(canvas.get("height") or 1080),
                               int(round(float(fps))))
    # assets: {material id: path} of the media files earlier saves of this draft put in the project
    script.base_project = {"dir": project_dir, "name": name, "raw": raw, "format": fmt,
                           "manifest": manifest, "assets": {}}
    draft_id = f"dfd_cat_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    store_new(draft_id, script)
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


def _check_unchanged(project_dir: str, base: dict):
    if timeline_manifest(project_dir) != base["manifest"]:
        raise _errors().SaveDraftError(f"'{base['name']}' changed since it was opened (edited in CapCut?): open it "
                                       f"again with capcut_open_project. Nothing was written.")


def _file_sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _place_assets(script, project_dir: str, stage: str, kept: set) -> list:
    """Decide where each media file copied into stage goes in the project. A file already there
    with the same content is reused; a different file with the same name (a source replaced at
    the same path, or an unrelated file) is never overwritten: the new one gets its own name and
    the materials using it are pointed there. Returns the staged files to move, relative to stage."""
    sd = _errors()
    to_add = []
    for dirpath, _, files in os.walk(stage):
        for fname in files:
            staged = os.path.join(dirpath, fname)
            rel = os.path.relpath(staged, stage)
            dest = sd.project_path(project_dir, *rel.split(os.sep))
            if not os.path.lexists(dest):
                to_add.append(rel)
                continue
            if os.path.isfile(dest) and filecmp.cmp(staged, dest, shallow=False):
                os.remove(staged)
                continue
            stem, ext = os.path.splitext(fname)
            new_rel = os.path.join(os.path.dirname(rel), f"{stem}-{_file_sha(staged)[:12]}{ext}")
            new_dest = sd.project_path(project_dir, *new_rel.split(os.sep))
            if os.path.isfile(new_dest) and filecmp.cmp(staged, new_dest, shallow=False):
                os.remove(staged)
            elif os.path.lexists(new_dest):
                raise sd.SaveDraftError(f"{new_rel} already exists in the project with other content: nothing was written")
            else:
                os.rename(staged, os.path.join(stage, new_rel))
                to_add.append(new_rel)
            for material, _ in sd._media_materials(script):
                if material.material_id not in kept and material.replace_path == dest:
                    material.replace_path = new_dest
    return to_add


def save_into_existing(draft_id: str, script, task_id: str) -> dict:
    sd = _errors()
    base = script.base_project
    project_dir, name = base["dir"], base["name"]
    if "manifest" not in base:
        raise sd.SaveDraftError(f"This draft was opened by an older version of the kit: open '{name}' again with "
                                f"capcut_open_project. Nothing was written.")
    with project_lock(project_dir):
        sd.ensure_capcut_closed(f"saving into '{name}'")
        _check_unchanged(project_dir, base)
        original = json.loads(base["raw"])
        sd.update_media_metadata(script, task_id)  # also applies pending keyframes

        projects_dir = os.path.dirname(project_dir)
        stage = sd.child_dir(projects_dir, f".capcut-mcp-assets-{uuid.uuid4().hex[:8]}")
        os.makedirs(stage)
        try:
            # Media an earlier save put in the project stays as it is (the version those clips were made with)
            assets = base.setdefault("assets", {})
            kept = {mid for mid, path in assets.items() if os.path.isfile(path)}
            sd.copy_assets(script, task_id, projects_dir, name, stage, skip=kept)
            to_add = _place_assets(script, project_dir, stage, kept)
            sd.verify_references(script, project_dir, stage, existing_ok=True)
            from draft_profiles import get_draft_profile
            additions = json.loads(script.dumps(get_draft_profile()))
            merged, added_ids, added_tracks = merge(original, additions)
            verify_untouched(original, merged, added_ids, added_tracks)
            data = SERIALIZERS[base["format"]](merged).encode("utf-8")
            meta_path = sd.project_path(project_dir, "draft_meta_info.json")
            meta = _update_meta(project_dir, merged["duration"]) if os.path.isfile(meta_path) else None
            copies = list(base["manifest"]["copies"])

            # Back up the whole project, journal the write, then put the new files in place
            backup = sd.backup_dest(name)
            sd.clone_tree(project_dir, backup)
            details = {"dir": project_dir, "name": name, "backup": backup, "copies": copies,
                       "old_sha": next(iter(base["manifest"]["copies"].values())), "new_sha": _sha(data),
                       "meta": meta is not None, "added": [r.replace(os.sep, "/") for r in to_add],
                       "locks": [os.path.realpath(project_dir)]}
            op_id = journal_begin(draft_id, "existing", details)
            moved, written = [], []
            try:
                for rel in to_add:
                    dest = sd.project_path(project_dir, *rel.split(os.sep))
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    sd.rename_noreplace(os.path.join(stage, rel), dest)
                    moved.append(dest)
                _check_unchanged(project_dir, base)  # again, just before the timeline is written
                for rel in copies:
                    written.append(rel)
                    _write_atomic(sd.project_path(project_dir, *rel.split("/")), data)
                if meta is not None:
                    written.append("draft_meta_info.json")
                    _write_atomic(meta_path, meta)
            except BaseException as e:
                try:
                    _restore(project_dir, backup, written, moved)
                    journal_update(op_id, "rolled_back")
                except Exception as undo_error:
                    raise sd.SaveDraftError(f"{e}. The project could not be put back as it was ({undo_error}): "
                                            f"restore it from the backup {backup}")
                raise
        finally:
            shutil.rmtree(stage, ignore_errors=True)

        base["manifest"] = timeline_manifest(project_dir)
        for material, _ in sd._media_materials(script):
            if material.material_id not in kept and material.replace_path and \
                    sd._inside(material.replace_path, project_dir):
                assets[material.material_id] = material.replace_path
        committed_to_disk(op_id, f"'{name}' saved, previous version in {backup}")
    sd.update_task_fields(task_id, status="completed", progress=100, message="Saved into existing project")
    return {"draft_url": project_dir, "backups": [backup], "added_tracks": added_tracks}


def _restore(project_dir: str, backup: str, written: list, moved: list):
    """Undo a save into an existing project: the files it wrote come back from the backup, the
    media files it added are removed."""
    sd = _errors()
    for rel in written:
        src = os.path.join(backup, *rel.split("/"))
        if os.path.isfile(src):
            _write_atomic(sd.project_path(project_dir, *rel.split("/")), _read(src))
    for path in moved:
        if os.path.isfile(path):
            os.remove(path)


def recover(op_id: str, details: dict, script):
    """Settle a save into an existing project that did not finish (crash, or the draft could not
    be recorded): if the timeline copies are all the new version it happened (recorded in the
    draft, when at hand); if they are all the old one it did not (the media it added are removed);
    if they are mixed, the old version comes back from the backup."""
    project_dir = details["dir"]
    meta = ["draft_meta_info.json"] if details["meta"] else []
    added = [os.path.join(project_dir, *rel.split("/")) for rel in details["added"]]

    def copies_now():
        return {_sha(_read(p)) if os.path.isfile(p) else None
                for p in (os.path.join(project_dir, *rel.split("/")) for rel in details["copies"])}

    values = copies_now()
    if values == {details["new_sha"]}:
        if script is None or getattr(script, "base_project", None) is None:
            return  # recorded when the draft is next saved
        script.base_project["manifest"] = timeline_manifest(project_dir)
        committed_to_disk(op_id, f"an earlier save into '{details['name']}', reconciled")
        return
    if values == {details["old_sha"]}:  # stopped before the timeline was written
        _restore(project_dir, details["backup"], [], added)
        journal_update(op_id, "rolled_back")
        return
    if details["new_sha"] in values:  # stopped while writing the timeline copies
        _restore(project_dir, details["backup"], details["copies"] + meta, added)
        if copies_now() == {details["old_sha"]}:
            journal_update(op_id, "rolled_back")
        return
    journal_update(op_id, "abandoned")  # changed since (edited in CapCut): nothing to settle
