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
journalled with its whole plan (see save_draft_impl, "Saves that did not finish"): if it fails it
is undone (the files it wrote come back from the backup, the media it added are removed); if the
backend stops halfway, the next call on the draft (or the next start) completes it or undoes it,
once CapCut is known to be closed, and leaves the project alone if anything else changed it.
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
from draft_store import committed_to_disk, journal_begin, journal_update, project_lock, store_new, until_commit

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


def _update_meta(raw: bytes, duration: int) -> bytes:
    """New bytes for draft_meta_info.json with only the modification time and duration changed."""
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


def _sha_or_none(path: str):
    return _file_sha(path) if os.path.isfile(path) and not os.path.islink(path) else None


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
    """Add the draft to its project. Under the project's lock: the staging and backup paths are
    journalled before anything is created; once the new files are ready, the plan is completed
    with every file to write (old and new hash, the new metadata), the identity of every media
    file to move in and what the draft records once saved; then the project is written."""
    sd = _errors()
    base = script.base_project
    project_dir, name = base["dir"], base["name"]
    if "manifest" not in base:
        raise sd.SaveDraftError(f"This draft was opened by an older version of the kit: open '{name}' again with "
                                f"capcut_open_project. Nothing was written.")
    projects_dir = os.path.dirname(project_dir)
    lock = os.path.realpath(project_dir)
    with until_commit(project_lock(project_dir)):
        sd.ensure_settled(draft_id, [lock])
        sd.ensure_capcut_closed(f"saving into '{name}'")
        _check_unchanged(project_dir, base)
        original = json.loads(base["raw"])
        sd.update_media_metadata(script, task_id)  # also applies pending keyframes

        stage = sd._hidden(projects_dir, "assets")
        backup = sd.backup_dest(name)
        details = {"phase": "preparing", "dir": project_dir, "name": name, "stage": stage, "backup": backup,
                   "locks": [lock]}
        op_id = journal_begin(draft_id, "existing", details)
        try:
            os.makedirs(stage)
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
            meta_old = _read(meta_path) if os.path.isfile(meta_path) else None
            meta_new = _update_meta(meta_old, merged["duration"]) if meta_old is not None else None
            old_sha = next(iter(base["manifest"]["copies"].values()))

            # The whole project is backed up (named as the backup only once complete)
            sd.clone_tree(project_dir, backup + ".partial")
            os.rename(backup + ".partial", backup)

            details.update(
                phase="ready",
                copies={rel: {"old": old_sha, "new": _sha(data)} for rel in base["manifest"]["copies"]},
                meta={"old": _sha(meta_old), "new": _sha(meta_new), "data": meta_new.decode("utf-8")}
                if meta_old is not None else None,
                added=[{"rel": rel.replace(os.sep, "/"), "sha": _file_sha(os.path.join(stage, rel)),
                        "id": sd._identity(os.path.join(stage, rel))} for rel in to_add],
                assets_after={m.material_id: m.replace_path for m, _ in sd._media_materials(script)
                              if m.material_id not in kept and m.replace_path and sd._inside(m.replace_path, project_dir)},
                manifest_after={**base["manifest"], "copies": {rel: _sha(data) for rel in base["manifest"]["copies"]}})
            journal_update(op_id, details=details)
        except BaseException:
            try:
                shutil.rmtree(stage, ignore_errors=True)
                shutil.rmtree(backup + ".partial", ignore_errors=True)
                journal_update(op_id, "rolled_back")
            except Exception as e:
                sd.logger.error(f"Could not clean up save {op_id}: {e}")
            raise

        try:
            for item in details["added"]:
                dest = sd.project_path(project_dir, *item["rel"].split("/"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                sd.rename_noreplace(os.path.join(stage, *item["rel"].split("/")), dest)
            # Again, just before the timeline is written
            _check_unchanged(project_dir, base)
            if meta_old is not None and _sha_or_none(meta_path) != details["meta"]["old"]:
                raise sd.SaveDraftError(f"'{name}' changed while it was being saved (draft_meta_info.json): "
                                        f"nothing was written")
            for rel in details["copies"]:
                _write_atomic(sd.project_path(project_dir, *rel.split("/")), data)
            if meta_new is not None:
                _write_atomic(meta_path, meta_new)
        except BaseException as e:
            try:
                undone = _roll_back(details)
            except Exception as undo_error:
                undone, e = False, f"{e} ({undo_error})"
            if not undone:
                raise sd.SaveDraftError(f"{e}. The project could not be put back as it was: the version before this "
                                        f"save is in the backup {backup}")
            journal_update(op_id, "rolled_back")
            raise
        finally:
            shutil.rmtree(stage, ignore_errors=True)

        base["manifest"] = timeline_manifest(project_dir)
        assets.update(details["assets_after"])
        committed_to_disk(op_id, f"'{name}' saved, previous version in {backup}")
    sd.update_task_fields(task_id, status="completed", progress=100, message="Saved into existing project")
    return {"draft_url": project_dir, "backups": [backup], "added_tracks": added_tracks}


def _planned_files(details: dict) -> dict:
    """{relative path: {"old": sha, "new": sha}} of every file the save writes."""
    files = dict(details["copies"])
    if details.get("meta"):
        files["draft_meta_info.json"] = details["meta"]
    return files


def _roll_back(details: dict) -> bool:
    """Put the project back as it was before the save: each planned file that holds the new
    version gets the old one back from the backup (checked to be that version first); the media
    files the save moved in are removed, only those that are still the very files it moved; the
    staging folder too. Returns True if no planned file holds the new version any more."""
    sd = _errors()
    project_dir, backup = details["dir"], details["backup"]
    files = _planned_files(details)
    for rel, f in files.items():
        path = sd.project_path(project_dir, *rel.split("/"))
        if _sha_or_none(path) == f["new"]:
            src = os.path.join(backup, *rel.split("/"))
            if _sha_or_none(src) != f["old"]:
                raise sd.SaveDraftError(f"the backup {backup} does not hold the previous version of {rel}")
            _write_atomic(path, _read(src))
    for item in details["added"]:
        path = sd.project_path(project_dir, *item["rel"].split("/"))
        if sd._identity(path) == item["id"] and _sha_or_none(path) == item["sha"]:
            os.remove(path)
    if os.path.isdir(details["stage"]) and not os.path.islink(details["stage"]):
        shutil.rmtree(details["stage"])
    return all(_sha_or_none(sd.project_path(project_dir, *rel.split("/"))) != f["new"] for rel, f in files.items())


def settle(op_id: str, details: dict, script) -> list:
    """Settle a save into an existing project that did not finish (crash, or the draft could not
    be recorded), from its journalled plan. Only with CapCut known to be closed, and only if every
    planned file is the old or the new version: if all timeline copies are new the save is
    completed (metadata included) and recorded in the draft, with the media it placed; otherwise
    the project is put back as it was. Anything else means the project changed since: it is left
    as it is. Returns notes for the user."""
    sd = _errors()
    project_dir, name = details["dir"], details["name"]
    if details["phase"] != "ready":
        # Stopped while preparing: the project was not touched; the staging folder and an
        # unfinished backup are this save's own paths
        for path in (details["stage"], details["backup"] + ".partial"):
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
        journal_update(op_id, "rolled_back")
        return []
    if sd.capcut_is_running() is not False:
        return [f"A save of this draft into '{name}' stopped halfway; it is settled once CapCut is known to be "
                f"closed: quit CapCut and call again. Nothing was written meanwhile."]
    files = _planned_files(details)
    now = {rel: _sha_or_none(sd.project_path(project_dir, *rel.split("/"))) for rel in files}
    foreign = [rel for rel, f in files.items() if now[rel] not in (f["old"], f["new"])]
    if not foreign and all(now[rel] == f["new"] for rel, f in details["copies"].items()):
        meta = details.get("meta")
        if meta and now["draft_meta_info.json"] == meta["old"]:
            path = sd.project_path(project_dir, "draft_meta_info.json")
            _write_atomic(path, meta["data"].encode("utf-8"))
            if _sha_or_none(path) != meta["new"]:
                raise sd.SaveDraftError(f"draft_meta_info.json of '{name}' could not be completed")
        manifest = timeline_manifest(project_dir)
        if manifest != details["manifest_after"]:
            foreign = ["Timelines/project.json"]
        else:
            if os.path.isdir(details["stage"]) and not os.path.islink(details["stage"]):
                shutil.rmtree(details["stage"])
            base = getattr(script, "base_project", None) if script is not None else None
            if base is None:
                journal_update(op_id, "done")  # the draft is gone: nothing to record it in
                return []
            base["manifest"] = manifest
            base.setdefault("assets", {}).update(details["assets_after"])
            for material, _ in sd._media_materials(script):
                if material.material_id in details["assets_after"]:
                    material.replace_path = details["assets_after"][material.material_id]
            committed_to_disk(op_id, f"an earlier save into '{name}', reconciled")
            return []
    if foreign:
        if os.path.isdir(details["stage"]) and not os.path.islink(details["stage"]):
            shutil.rmtree(details["stage"])
        note = (f"'{name}' was changed by something else after a save of this draft stopped halfway "
                f"({', '.join(foreign)}): it was left as it is; the version before that save is in {details['backup']}")
        journal_update(op_id, "abandoned", details={**details, "notes": [note]})
        return [note]
    if _roll_back(details):
        journal_update(op_id, "rolled_back")
        return []
    return [f"A save of this draft into '{name}' stopped halfway and could not be fully undone: the version "
            f"before it is in {details['backup']}"]
