# Modified in capcut-mcp-kit (2026) from VectCutAPI @ cfa4779: save straight into the local CapCut drafts folder (macOS/Windows), rename the folder to project_name, fix draft_meta_info.json; media under ~/Movies referenced in place; each file copied once even when used by many clips; validated folder names, staging + swap, existing folders moved to backups instead of deleted, failures reported; drafts opened from an existing project are saved by existing_project; one lock per project folder, checks repeated at commit, rename without replacing, the old folder kept on the same volume until the new one is in, journalled saves settled after a crash; a folder may be replaced only if the draft's own state says it saved it; CapCut must be known to be closed.
# See NOTICE at the repository root.
import contextlib
import ctypes
import errno
import os
import sys
import pyJianYingDraft as draft
import shutil
from util import zip_draft, build_draft_asset_path, source_changed
from oss import upload_to_oss
from typing import Dict, Literal
from draft_store import (get_draft, DraftNotFound, project_lock, journal_begin, journal_update, journal_pending,
                         committed_to_disk)
from save_task_cache import DRAFT_TASKS, get_task_status, update_tasks_cache, update_task_field, increment_task_field, update_task_fields, create_task
from downloader import download_audio, download_file, download_image, download_video
from concurrent.futures import ThreadPoolExecutor, as_completed
import imageio.v2 as imageio
import subprocess
import json
from get_duration_impl import get_video_duration
import uuid
import threading
from collections import OrderedDict
import time
import requests # Import requests for making HTTP calls
import logging
# Import configuration
from settings import IS_UPLOAD_DRAFT
from draft_profiles import get_draft_profile, write_profile_content

# --- Get your Logger instance ---
# The name here must match the logger name you configured in app.py
logger = logging.getLogger('flask_video_generator') 

# Define task status enumeration type
TaskStatus = Literal["initialized", "processing", "completed", "failed", "not_found"]

def build_asset_path(draft_folder: str, draft_id: str, asset_type: str, material_name: str) -> str:
    """
    Build asset file path
    :param draft_folder: Draft folder path
    :param draft_id: Draft ID
    :param asset_type: Asset type (audio, image, video)
    :param material_name: Material name
    :return: Built path
    """
    return build_draft_asset_path(draft_folder, draft_id, asset_type, material_name)

def find_capcut_projects_dir():
    """Return the local CapCut/Jianying desktop drafts directory, or None if not found."""
    if os.name == 'nt':
        candidates = [os.path.expandvars(r"%LOCALAPPDATA%\CapCut\User Data\Projects\com.lveditor.draft")]
    else:
        candidates = [
            os.path.expanduser('~/Movies/CapCut/User Data/Projects/com.lveditor.draft'),  # CapCut (international)
            os.path.expanduser('~/Library/Containers/com.lemon.lvpro/Data/Documents/JianyingPro/User Data/Projects/com.lveditor.draft'),
        ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None

def fix_draft_meta(meta_dir, draft_dir, display_name, duration, previous=None):
    """The template's draft_meta_info.json carries stale paths/ids; point it at draft_dir so CapCut
    lists it. meta_dir is where the file is now (a staging folder until commit). When replacing a
    project this draft saved before, `previous` (its old meta) keeps the CapCut id and creation date."""
    meta_path = os.path.join(meta_dir, "draft_meta_info.json")
    if not os.path.exists(meta_path):
        return
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    previous = previous or {}
    now = int(time.time() * 1_000_000)
    meta.update(
        draft_fold_path=draft_dir,
        draft_root_path=os.path.dirname(draft_dir),
        draft_name=display_name,
        draft_id=previous.get("draft_id") or str(uuid.uuid4()).upper(),
        draft_cover="",
        cloud_draft_cover=False,
        cloud_draft_sync=False,
        tm_draft_create=previous.get("tm_draft_create") or now,
        tm_draft_modified=now,
        tm_duration=duration,
    )
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)

def _inside(path, folder) -> bool:
    """True if path is folder or anything in it (symbolic links resolved)."""
    path, folder = os.path.realpath(path), os.path.realpath(folder)
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)

def use_in_place(material, source, avoid=()) -> bool:
    """Reference local media where it is instead of copying it into the project. CapCut is
    sandboxed and can only open files under ~/Movies on its own, so this applies there; other
    local files are cloned into the project (copy-on-write, see downloader.download_file).
    Files inside `avoid` (the folders this save replaces) are always copied: the folder they are
    in goes to the backups. Returns True if handled."""
    local = os.path.realpath(os.path.expanduser(str(source)))
    movies = os.path.realpath(os.path.expanduser("~/Movies")) + os.sep
    if IS_UPLOAD_DRAFT or not os.path.isfile(local) or not local.startswith(movies):
        return False
    if any(_inside(local, folder) for folder in avoid):
        return False
    material.replace_path = local
    return True

# Written into every folder this kit saves, for whoever looks at it. It authorizes nothing: which
# folders a draft may replace is in the draft's own state (kit_saves).
KIT_MARKER = ".capcut_mcp_kit.json"

class SaveDraftError(Exception):
    """A save that was refused or failed; nothing was replaced."""

def validate_folder_name(name, what="project_name"):
    """A name used as one folder inside the drafts directory: no separators, '.' or '..'."""
    if not isinstance(name, str) or not name.strip():
        raise SaveDraftError(f"{what} must be a non-empty string")
    name = name.strip()
    if name.startswith(".") or any(c in name for c in '/\\:') or any(ord(c) < 32 for c in name):
        raise SaveDraftError(f"Invalid {what} {name!r}: it cannot start with '.' or contain / \\ : or control characters")
    if len(name.encode("utf-8")) > 200:
        raise SaveDraftError(f"{what} is too long")
    return name

def child_dir(parent, name):
    """parent/name, refused unless it resolves to a direct child of parent."""
    path = os.path.join(parent, name)
    if os.path.dirname(os.path.realpath(path)) != os.path.realpath(parent):
        raise SaveDraftError(f"{path} resolves outside {parent}")
    return path

def project_path(root, *parts):
    """root/parts..., refused if it would leave root or pass through a symbolic link (checked for
    every part that exists: a link inside a project could point anywhere)."""
    path = root
    for part in parts:
        if part in ("", ".", "..") or "/" in part or "\\" in part:
            raise SaveDraftError(f"Invalid path {'/'.join(parts)!r} in project {os.path.basename(root)!r}")
        path = os.path.join(path, part)
        if os.path.islink(path):
            raise SaveDraftError(f"'{os.path.relpath(path, root)}' in project '{os.path.basename(root)}' is a symbolic "
                                 f"link: the kit only reads and writes inside the project folder. Nothing was written.")
    if not _inside(path, root):
        raise SaveDraftError(f"{path} resolves outside {root}")
    return path

def capcut_is_running():
    """True/False, or None when it cannot be determined."""
    try:
        if os.name == 'nt':
            done = subprocess.run(["tasklist", "/FI", "IMAGENAME eq CapCut.exe", "/NH"],
                                  capture_output=True, text=True, timeout=5)
            return "CapCut.exe" in done.stdout if done.returncode == 0 else None
        code = subprocess.run(["pgrep", "-x", "CapCut"], capture_output=True, timeout=5).returncode
        return {0: True, 1: False}.get(code)  # pgrep: 1 = no such process, anything else = error
    except Exception:
        return None

def ensure_capcut_closed(what, hint=""):
    """Refuse `what` unless CapCut is known to be closed: it keeps the project in memory and writes
    it back over the saved one when it closes."""
    running = capcut_is_running()
    if running is False:
        return
    if running:
        raise SaveDraftError(f"CapCut is open: quit CapCut before {what}, otherwise it writes its own copy back "
                             f"over the project when it closes. Nothing was written.{hint}")
    raise SaveDraftError(f"Could not tell whether CapCut is open, so {what} was not attempted: quit CapCut "
                         f"and try again. Nothing was written.{hint}")

def backup_root():
    configured = os.environ.get("CAPCUT_MCP_BACKUP_DIR")
    if configured:
        return os.path.expanduser(configured)
    if os.name == 'nt':
        return os.path.expandvars(r"%LOCALAPPDATA%\CapCut MCP Backups")
    return os.path.expanduser("~/Movies/CapCut MCP Backups")

def backup_dest(name):
    """A new, unused path in the backups folder for a copy of the project `name`."""
    root = backup_root()
    os.makedirs(root, exist_ok=True)
    base = f"{name} {time.strftime('%Y%m%d-%H%M%S')}"
    dest, n = os.path.join(root, base), 1
    while os.path.lexists(dest) or os.path.lexists(dest + ".partial"):
        n += 1
        dest = os.path.join(root, f"{base}-{n}")
    return dest

def clone_tree(src, dst):
    """Copy a folder, as copy-on-write clones where the filesystem supports it (APFS)."""
    if hasattr(os, "uname") and os.uname().sysname == "Darwin":
        if subprocess.run(["cp", "-c", "-R", src, dst], capture_output=True).returncode == 0:
            return
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst, symlinks=True)

def move_to_backup(path, name=None):
    """Move a folder to the backups instead of deleting it. Returns where it went. On another
    volume it is copied in full (to a .partial folder renamed once complete) before the original
    is removed; if the removal fails the backup is complete and the error says what is left."""
    dest = backup_dest(name or os.path.basename(path))
    try:
        os.rename(path, dest)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        partial = dest + ".partial"
        try:
            clone_tree(path, partial)
            os.rename(partial, dest)
        except BaseException:
            shutil.rmtree(partial, ignore_errors=True)
            raise
        try:
            shutil.rmtree(path)
        except OSError as e2:
            raise SaveDraftError(f"Backed up to {dest}, but the old copy at {path} could not be removed: {e2}")
    logger.info(f"Moved {path} to backup {dest}")
    return dest

_renamex_np = None

def rename_noreplace(src, dst):
    """Rename src to dst, failing (FileExistsError or OSError) if anything exists at dst, without a
    window in which something created meanwhile could be replaced."""
    global _renamex_np
    if sys.platform == "darwin":
        if _renamex_np is None:
            libc = ctypes.CDLL(None, use_errno=True)
            _renamex_np = libc.renamex_np
            _renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        if _renamex_np(os.fsencode(src), os.fsencode(dst), 0x4) != 0:  # RENAME_EXCL
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err), dst)
        return
    if os.path.lexists(dst):
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), dst)
    os.rename(src, dst)

def _read_marker(folder) -> dict:
    try:
        with open(os.path.join(folder, KIT_MARKER), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def read_kit_marker(folder):
    return _read_marker(folder).get("draft_id")


def _file_sha(path):
    import hashlib
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None

def read_meta(folder):
    try:
        with open(os.path.join(folder, "draft_meta_info.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None

def kit_saves(script) -> dict:
    """The folders this draft saved and the timeline it wrote in each: {real path: sha256}. Kept in
    the draft (not in the folder) so a file in the folder cannot authorize replacing it."""
    if not isinstance(getattr(script, "kit_saves", None), dict):
        script.kit_saves = {}
    return script.kit_saves

def check_replace(target, saved, overwrite, in_capcut, content_file="draft_info.json"):
    """Refuse to replace a folder this draft did not save, or that changed since this draft saved
    it (unless overwrite), and any CapCut project while CapCut is open (or might be). Returns the
    sha256 of the target's timeline as checked (None if it has none), to verify it again when the
    folder is moved."""
    if not os.path.lexists(target):
        return None
    name = os.path.basename(target)
    if os.path.islink(target) or not os.path.isdir(target):
        raise SaveDraftError(f"'{name}' exists and is not a project folder")
    recorded = saved.get(os.path.realpath(target))
    current = _file_sha(os.path.join(target, content_file))
    if not overwrite and recorded is None:
        raise SaveDraftError(
            f"A project named '{name}' already exists and was not saved from this draft. "
            f"Choose another project_name, or pass overwrite=true to replace it "
            f"(the old folder is moved to {backup_root()}, not deleted).")
    if not overwrite and current != recorded:
        raise SaveDraftError(
            f"'{name}' was changed in CapCut after this draft saved it: saving the draft again would replace "
            f"those changes. Open it with capcut_open_project to add to it, or pass overwrite=true to replace "
            f"it anyway (the edited version is moved to {backup_root()}, not deleted).")
    if in_capcut:
        ensure_capcut_closed(f"replacing '{name}'", " Saving under a new project_name works while CapCut is open.")
    return current

def swap_in(stage_dir, target, checked_sha, content_file):
    """Put a fully written staging folder at target. An existing target is first renamed aside in
    the same folder (atomic, same volume) and verified to be what was checked; the new folder is
    then renamed in without replacing anything that appeared meanwhile. Returns the aside path (or
    None): the old project, still next to the new one until finish_swap moves it to the backups."""
    name = os.path.basename(target)
    aside = None
    if os.path.lexists(target):
        aside = child_dir(os.path.dirname(target), f".capcut-mcp-old-{uuid.uuid4().hex[:8]}")
        os.rename(target, aside)
        if _file_sha(os.path.join(aside, content_file)) != checked_sha:
            os.rename(aside, target)
            raise SaveDraftError(f"'{name}' changed while it was being saved: nothing was replaced, try again")
    try:
        rename_noreplace(stage_dir, target)
    except OSError:
        if aside:
            rename_noreplace(aside, target)
        raise SaveDraftError(f"A folder named '{name}' appeared while saving: nothing was replaced, try again")
    return aside

def undo_swap(target, aside):
    """Put back what swap_in replaced; the new folder (written by this save) is removed."""
    failed = child_dir(os.path.dirname(target), f".capcut-mcp-failed-{uuid.uuid4().hex[:8]}")
    os.rename(target, failed)
    if aside:
        rename_noreplace(aside, target)
    shutil.rmtree(failed, ignore_errors=True)

def finish_swap(aside, name):
    """Move the replaced project to the backups. Returns (where it is, warning or None): if it
    cannot be moved it stays next to the new one, hidden, and the warning says where."""
    try:
        return move_to_backup(aside, name), None
    except Exception as e:
        where = aside if os.path.lexists(aside) else None
        logger.error(f"Could not move {aside} to the backups: {e}", exc_info=True)
        return where, f"the replaced version of '{name}' could not be moved to the backups ({e})" + \
            (f"; it is at {aside}" if where else "")

def write_kit_marker(folder, draft_id, content_file="draft_info.json"):
    """Record which draft wrote this folder and the timeline it wrote (informational)."""
    with open(os.path.join(folder, KIT_MARKER), "w", encoding="utf-8") as f:
        json.dump({"draft_id": draft_id, "content_sha": _file_sha(os.path.join(folder, content_file))}, f)

def _media_materials(script):
    materials = [(audio, "audio") for audio in (script.materials.audios or [])]
    for video in (script.materials.videos or []):
        if video.material_type == 'photo':
            materials.append((video, "image"))
        elif video.material_type == 'video':
            materials.append((video, "video"))
    return materials

def copy_assets(script, task_id, path_base, path_name, dest_root, avoid=(), skip=()):
    """Point every media material at <path_base>/<path_name>/assets/<type>/<name> (where it will be
    once dest_root is in place) and copy the files into <dest_root>/assets/<type>/<name>. Media
    under ~/Movies is referenced where it is, unless it is inside `avoid` (folders this save
    replaces). Materials whose id is in `skip` are left as they are (already in the project).
    Raises SaveDraftError if any file is missing or changed since it was added."""
    download_tasks = []
    missing = []
    for material, asset_type in _media_materials(script):
        if skip and getattr(material, "material_id", None) in skip:
            continue
        remote_url = material.remote_url
        material_name = material.material_name
        material.replace_path = build_asset_path(path_base, path_name, asset_type, material_name)
        if not remote_url:
            missing.append(f"{material_name} (no source)")
            continue
        if use_in_place(material, remote_url, avoid):
            continue
        if source_changed(material_name, remote_url):
            missing.append(f"{material_name} ({remote_url} was replaced or edited after it was added: add it again)")
            continue
        download_tasks.append({
            'type': asset_type,
            'func': download_file,
            'args': (remote_url, os.path.join(dest_root, "assets", asset_type, material_name)),
            'material': material
        })

    update_task_fields(task_id, message=f"Collected {len(download_tasks)} download tasks in total", progress=10)

    # Several clips of the same file share one destination: copy/download it only once
    unique_tasks = {}
    for task in download_tasks:
        unique_tasks.setdefault(task['args'][1], task)
    download_tasks = list(unique_tasks.values())

    completed_files = 0
    if download_tasks:
        logger.info(f"Starting concurrent download of {len(download_tasks)} files...")
        with ThreadPoolExecutor(max_workers=16) as executor:
            future_to_task = {
                executor.submit(task['func'], *task['args']): task
                for task in download_tasks
            }
            for future in as_completed(future_to_task):
                task = future_to_task[future]
                try:
                    future.result()
                except Exception as e:
                    logger.error(f"Task {task_id}: Download {task['type']} file failed: {str(e)}", exc_info=True)
                    missing.append(f"{task['material'].material_name} ({e})")
                    continue
                completed_files += 1
                total = len(download_tasks)
                update_task_fields(task_id, completed_files=completed_files, total_files=total,
                                   progress=10 + int((completed_files / total) * 60),
                                   message=f"Downloaded {completed_files}/{total} files")
    for task in download_tasks:
        if not os.path.isfile(task['args'][1]):
            name = task['material'].material_name
            if not any(m.startswith(name) for m in missing):
                missing.append(f"{name} (not copied)")
    if missing:
        raise SaveDraftError("Draft not saved, some media could not be copied: " + "; ".join(missing))

def verify_references(script, final_dir, stage_dir, avoid=(), existing_ok=False):
    """Every media material must point at a file that will exist once stage_dir is at final_dir,
    and never into a folder this save replaces."""
    broken = []
    for material, _ in _media_materials(script):
        path = material.replace_path
        if not path:
            broken.append(material.material_name)
        elif _inside(path, final_dir):
            rel = os.path.relpath(os.path.abspath(path), os.path.abspath(final_dir))
            if not os.path.isfile(os.path.join(stage_dir, rel)) and not (existing_ok and os.path.isfile(path)):
                broken.append(material.material_name)
        elif not os.path.isfile(path) or any(_inside(path, folder) for folder in avoid):
            broken.append(material.material_name)
    if broken:
        raise SaveDraftError("Draft not saved, some media would point at files that will not exist: " + ", ".join(broken))

def _recover_replace(op_id, details, script):
    """Settle a save that replaced project folders and did not finish (crash, or the draft could
    not be recorded). Each folder ends up either the new version or the old one; with the draft at
    hand, the new versions are recorded in it."""
    content_file = details["content_file"]
    settled, adopted = True, []
    for pair in details["pairs"]:
        target, aside, stage = pair["target"], pair.get("aside"), pair.get("stage")
        written = os.path.isdir(target) and not os.path.islink(target) and \
            _file_sha(os.path.join(target, content_file)) == pair["content_sha"]
        if written:
            adopted.append(pair)
            if aside and os.path.lexists(aside):
                finish_swap(aside, os.path.basename(target))
        elif not os.path.lexists(target) and aside and os.path.lexists(aside):
            rename_noreplace(aside, target)  # interrupted between the two renames
        elif aside and os.path.lexists(aside):
            settled = False  # something else is at target: leave the old version where it is
            logger.error(f"Save {op_id}: '{target}' is not the saved version; the previous one is at {aside}")
        if stage and os.path.lexists(stage):
            shutil.rmtree(stage, ignore_errors=True)
    if not settled:
        return
    if adopted and script is None:
        return  # the draft records them when it is next saved
    for pair in adopted:
        kit_saves(script)[os.path.realpath(pair["target"])] = pair["content_sha"]
    if adopted:
        committed_to_disk(op_id, "an earlier save, reconciled")
    else:
        journal_update(op_id, "rolled_back")

def recover_saves(draft_id=None, script=None):
    """Settle the journalled saves that did not finish: those of draft_id (recording what they
    wrote in script, its working copy), or all of them at startup."""
    import existing_project
    for op_id, op_draft, kind, details in journal_pending(draft_id):
        folders = details.get("locks") or []
        with contextlib.ExitStack() as stack:
            for folder in sorted(folders):
                stack.enter_context(project_lock(folder))
            if not any(op == op_id for op, *_ in journal_pending(op_draft)):
                continue  # settled meanwhile
            try:
                if kind == "replace":
                    _recover_replace(op_id, details, script if op_draft == draft_id else None)
                elif kind == "existing":
                    existing_project.recover(op_id, details, script if op_draft == draft_id else None)
            except Exception as e:
                logger.error(f"Could not settle save {op_id}: {e}", exc_info=True)

def save_draft_background(draft_id, draft_folder, task_id, project_name=None, auto_deploy=True, overwrite=False):
    """Save a draft: write it into a staging folder next to its destination, check every asset
    arrived, then swap it in. An existing folder is moved to backups, never deleted.
    Returns {"draft_url": path, "backups": [...]}; raises on failure with nothing replaced."""
    stages = []
    try:
        validate_folder_name(draft_id, "draft_id")
        if project_name:
            project_name = validate_folder_name(project_name)
        try:
            script = get_draft(draft_id)
        except DraftNotFound as e:
            raise SaveDraftError(str(e))
        recover_saves(draft_id, script)
        if getattr(script, "base_project", None):
            # Opened with capcut_open_project: add to that project, never replace it
            if project_name and project_name.strip() != script.base_project["name"]:
                raise SaveDraftError(f"This draft adds to the existing project '{script.base_project['name']}': "
                                     f"leave project_name out (or use that name)")
            import existing_project
            return existing_project.save_into_existing(draft_id, script, task_id)

        update_task_fields(task_id, status="processing", message="Preparing draft files", progress=0)

        current_dir = os.path.dirname(os.path.abspath(__file__))
        capcut_projects_dir = find_capcut_projects_dir()
        # Write straight into CapCut's drafts directory so asset paths stay valid after deploy
        if not draft_folder and auto_deploy and not IS_UPLOAD_DRAFT:
            draft_folder = capcut_projects_dir
        output_base_dir = draft_folder or current_dir
        in_place = (not IS_UPLOAD_DRAFT and bool(capcut_projects_dir)
                    and os.path.realpath(output_base_dir) == os.path.realpath(capcut_projects_dir))
        # CapCut lists projects by folder name, so in place the folder is named after the project
        final_name = (project_name or draft_id) if in_place else draft_id
        draft_dir = child_dir(output_base_dir, final_name)
        deploy_dir = None
        if not in_place and capcut_projects_dir and (auto_deploy or project_name):
            deploy_dir = child_dir(capcut_projects_dir, project_name or draft_id)
        targets = [(draft_dir, in_place)] + ([(deploy_dir, True)] if deploy_dir else [])
        avoid = [t for t, _ in targets]
        saved = kit_saves(script)

        # Refuse before writing anything
        draft_profile = get_draft_profile()
        content_file = draft_profile.content_file
        for target, in_capcut in targets:
            check_replace(target, saved, overwrite, in_capcut, content_file)

        template_source_dir = os.path.join(current_dir, draft_profile.template_dir)
        if not os.path.exists(template_source_dir):
            raise FileNotFoundError(f"Template draft {draft_profile.template_dir} does not exist")
        stage_dir = child_dir(output_base_dir, f".capcut-mcp-saving-{uuid.uuid4().hex[:8]}")
        stages.append(stage_dir)
        shutil.copytree(template_source_dir, stage_dir)

        update_task_fields(task_id, message="Updating media file metadata", progress=5)
        update_media_metadata(script, task_id)

        # Assets are copied into the staging folder; replace_path is where they will be once it
        # is renamed to draft_dir.
        copy_assets(script, task_id, output_base_dir, final_name, stage_dir, avoid=avoid)
        verify_references(script, draft_dir, stage_dir, avoid)

        update_task_fields(task_id, message="Saving draft information", progress=70)
        written_files = write_profile_content(draft_profile, stage_dir, script.dumps(draft_profile))
        logger.info(f"Draft information has been saved to {[str(path) for path in written_files]}.")
        write_kit_marker(stage_dir, draft_id, draft_profile.content_file)
        content_sha = _file_sha(os.path.join(stage_dir, content_file))
        if in_place:
            previous = read_meta(draft_dir) if os.path.realpath(draft_dir) in saved else None
            fix_draft_meta(stage_dir, draft_dir, project_name or draft_id, script.duration, previous)
        pairs = [{"target": draft_dir, "stage": stage_dir}]

        if deploy_dir:
            # Saved elsewhere: also place a copy in CapCut's drafts directory (committed together)
            deploy_stage = child_dir(capcut_projects_dir, f".capcut-mcp-saving-{uuid.uuid4().hex[:8]}")
            stages.append(deploy_stage)
            shutil.copytree(stage_dir, deploy_stage)
            lock_f = os.path.join(deploy_stage, ".locked")
            if os.path.exists(lock_f):
                os.remove(lock_f)
            previous = read_meta(deploy_dir) if os.path.realpath(deploy_dir) in saved else None
            fix_draft_meta(deploy_stage, deploy_dir, project_name or draft_id, script.duration, previous)
            pairs.append({"target": deploy_dir, "stage": deploy_stage})
        for pair in pairs:
            pair["content_sha"] = content_sha

        # Commit under every target's lock, checking again what was checked before staging
        backups, warnings = [], []
        with contextlib.ExitStack() as stack:
            for target in sorted(os.path.realpath(t) for t, _ in targets):
                stack.enter_context(project_lock(target))
            checked = {target: check_replace(target, saved, overwrite, in_capcut, content_file)
                       for target, in_capcut in targets}
            details = {"content_file": content_file, "pairs": pairs,
                       "locks": [os.path.realpath(t) for t, _ in targets]}
            op_id = journal_begin(draft_id, "replace", details)
            swapped = []
            try:
                for pair in pairs:
                    pair["aside"] = None
                    aside = swap_in(pair["stage"], pair["target"], checked[pair["target"]], content_file)
                    pair["aside"] = aside
                    stages.remove(pair["stage"])
                    swapped.append(pair)
                    journal_update(op_id, details=details)
            except BaseException:
                try:
                    for pair in reversed(swapped):
                        undo_swap(pair["target"], pair["aside"])
                    journal_update(op_id, "rolled_back")
                except Exception as e:
                    logger.error(f"Could not undo save {op_id}: {e}", exc_info=True)
                raise
            for pair in pairs:
                saved[os.path.realpath(pair["target"])] = content_sha
                if pair["aside"]:
                    where, warning = finish_swap(pair["aside"], os.path.basename(pair["target"]))
                    if where:
                        backups.append(where)
                    if warning:
                        warnings.append(warning)
            committed_to_disk(op_id, "saved " + " and ".join(p["target"] for p in pairs) +
                              (f", previous versions in {', '.join(backups)}" if backups else ""))
        for pair in pairs:
            logger.info(f"Saved draft {draft_id} to {pair['target']}")

        draft_url = ""
        # Only upload draft information when IS_UPLOAD_DRAFT is True
        if IS_UPLOAD_DRAFT:
            update_task_fields(task_id, message="Compressing draft files", progress=80)
            zip_path = zip_draft(draft_id)
            update_task_fields(task_id, message="Uploading to cloud storage", progress=90)
            draft_url = upload_to_oss(zip_path)
            logger.info(f"Draft archive has been uploaded to OSS, URL: {draft_url}")
            update_task_field(task_id, "draft_url", draft_url)
            # Clean up temporary files
            if os.path.exists(os.path.join(current_dir, draft_id)):
                shutil.rmtree(os.path.join(current_dir, draft_id))

        update_task_fields(task_id, status="completed", progress=100, message="Draft creation completed")
        logger.info(f"Task {task_id} completed: {deploy_dir or draft_dir}")
        result = {"draft_url": draft_url if IS_UPLOAD_DRAFT else (deploy_dir or draft_dir), "backups": backups}
        if warnings:
            result["warnings"] = warnings
        return result

    except Exception as e:
        for stage in stages:
            # Only ever staging folders this call created
            shutil.rmtree(stage, ignore_errors=True)
        update_task_fields(task_id, status="failed", message=f"Failed to save draft: {str(e)}")
        logger.error(f"Saving draft {draft_id} task {task_id} failed: {str(e)}", exc_info=True)
        raise

def query_task_status(task_id: str):
    return get_task_status(task_id)

def save_draft_impl(draft_id: str, draft_folder: str = None, project_name: str = None, auto_deploy: bool = True, overwrite: bool = False) -> Dict:
    """Save the draft synchronously. Returns {"success": True, "draft_url", "backups"} or {"success": False, "error"}."""
    logger.info(f"Received save draft request: draft_id={draft_id}, draft_folder={draft_folder}, project_name={project_name}, auto_deploy={auto_deploy}, overwrite={overwrite}")
    try:
        task_id = draft_id
        create_task(task_id)
        result = save_draft_background(draft_id, draft_folder, task_id, project_name=project_name,
                                       auto_deploy=auto_deploy, overwrite=overwrite)
        return {"success": True, **result}
    except SaveDraftError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        return {"success": False, "error": f"Failed to save draft: {e}"}

def update_media_metadata(script, task_id=None):
    """
    Update metadata for all media files in the script (duration, width/height, etc.)
    
    :param script: Draft script object
    :param task_id: Optional task ID for updating task status
    :return: None
    """
    # Process audio file metadata
    audios = script.materials.audios
    if not audios:
        logger.info("No audio files found in the draft.")
    else:
        for audio in audios:
            remote_url = audio.remote_url
            material_name = audio.material_name
            if not remote_url:
                logger.warning(f"Warning: Audio file {material_name} has no remote_url, skipped.")
                continue
            
            try:
                video_command = [
                    'ffprobe',
                    '-v', 'error',
                    '-select_streams', 'v:0',
                    '-show_entries', 'stream=codec_type',
                    '-of', 'json',
                    remote_url
                ]
                video_result = subprocess.check_output(video_command, stderr=subprocess.STDOUT)
                video_result_str = video_result.decode('utf-8')
                # Find JSON start position (first '{')
                video_json_start = video_result_str.find('{')
                if video_json_start != -1:
                    video_json_str = video_result_str[video_json_start:]
                    video_info = json.loads(video_json_str)
                    if 'streams' in video_info and len(video_info['streams']) > 0:
                        logger.warning(f"Warning: Audio file {material_name} contains video tracks, skipped its metadata update.")
                        continue
            except Exception as e:
                logger.error(f"Error occurred while checking if audio {material_name} contains video streams: {str(e)}", exc_info=True)

            # Get audio duration and set it
            try:
                duration_result = get_video_duration(remote_url)
                if duration_result["success"]:
                    if task_id:
                        update_task_field(task_id, "message", f"Processing audio metadata: {material_name}")
                    # Convert seconds to microseconds
                    audio.duration = int(duration_result["output"] * 1000000)
                    logger.info(f"Successfully obtained audio {material_name} duration: {duration_result['output']:.2f} seconds ({audio.duration} microseconds).")
                    
                    # Update timerange for all segments using this audio material
                    for track_name, track in script.tracks.items():
                        if track.track_type == draft.Track_type.audio:
                            for segment in track.segments:
                                if isinstance(segment, draft.Audio_segment) and segment.material_id == audio.material_id:
                                    # Get current settings
                                    current_target = segment.target_timerange
                                    current_source = segment.source_timerange
                                    speed = segment.speed.speed
                                    
                                    # If the end time of source_timerange exceeds the new audio duration, adjust it
                                    if current_source.end > audio.duration or current_source.end <= 0:
                                        # Adjust source_timerange to fit the new audio duration
                                        new_source_duration = audio.duration - current_source.start
                                        if new_source_duration <= 0:
                                            logger.warning(f"Warning: Audio segment {segment.segment_id} start time {current_source.start} exceeds audio duration {audio.duration}, will skip this segment.")
                                            continue
                                            
                                        # Update source_timerange
                                        segment.source_timerange = draft.Timerange(current_source.start, new_source_duration)
                                        
                                        # Update target_timerange based on new source_timerange and speed
                                        new_target_duration = int(new_source_duration / speed)
                                        segment.target_timerange = draft.Timerange(current_target.start, new_target_duration)
                                        
                                        logger.info(f"Adjusted audio segment {segment.segment_id} timerange to fit the new audio duration.")
                else:
                    logger.warning(f"Warning: Unable to get audio {material_name} duration: {duration_result['error']}.")
            except Exception as e:
                logger.error(f"Error occurred while getting audio {material_name} duration: {str(e)}", exc_info=True)
    
    # Process video and image file metadata
    videos = script.materials.videos
    if not videos:
        logger.info("No video or image files found in the draft.")
    else:
        for video in videos:
            remote_url = video.remote_url
            material_name = video.material_name
            if not remote_url:
                logger.warning(f"Warning: Media file {material_name} has no remote_url, skipped.")
                continue
                
            if video.material_type == 'photo':
                # Use imageio to get image width/height and set it
                try:
                    if task_id:
                        update_task_field(task_id, "message", f"Processing image metadata: {material_name}")
                    img = imageio.imread(remote_url)
                    video.height, video.width = img.shape[:2]
                    logger.info(f"Successfully set image {material_name} dimensions: {video.width}x{video.height}.")
                except Exception as e:
                    logger.error(f"Failed to set image {material_name} dimensions: {str(e)}, using default values 1920x1080.", exc_info=True)
                    video.width = 1920
                    video.height = 1080
            
            elif video.material_type == 'video':
                # Get video duration and width/height information
                try:
                    if task_id:
                        update_task_field(task_id, "message", f"Processing video metadata: {material_name}")
                    # Use ffprobe to get video information
                    command = [
                        'ffprobe',
                        '-v', 'error',
                        '-select_streams', 'v:0',  # Select the first video stream
                        '-show_entries', 'stream=width,height,duration',
                        '-show_entries', 'format=duration',
                        '-of', 'json',
                        remote_url
                    ]
                    result = subprocess.check_output(command, stderr=subprocess.STDOUT)
                    result_str = result.decode('utf-8')
                    # Find JSON start position (first '{')
                    json_start = result_str.find('{')
                    if json_start != -1:
                        json_str = result_str[json_start:]
                        info = json.loads(json_str)
                        
                        if 'streams' in info and len(info['streams']) > 0:
                            stream = info['streams'][0]
                            # Set width and height
                            video.width = int(stream.get('width', 0))
                            video.height = int(stream.get('height', 0))
                            logger.info(f"Successfully set video {material_name} dimensions: {video.width}x{video.height}.")
                            
                            # Set duration
                            # Prefer stream duration, if not available use format duration
                            duration = stream.get('duration') or info['format'].get('duration', '0')
                            video.duration = int(float(duration) * 1000000)  # Convert to microseconds
                            logger.info(f"Successfully obtained video {material_name} duration: {float(duration):.2f} seconds ({video.duration} microseconds).")
                            
                            # Update timerange for all segments using this video material
                            for track_name, track in script.tracks.items():
                                if track.track_type == draft.Track_type.video:
                                    for segment in track.segments:
                                        if isinstance(segment, draft.Video_segment) and segment.material_id == video.material_id:
                                            # Get current settings
                                            current_target = segment.target_timerange
                                            current_source = segment.source_timerange
                                            speed = segment.speed.speed

                                            # If the end time of source_timerange exceeds the new video duration, adjust it
                                            if current_source.end > video.duration or current_source.end <= 0:
                                                # Adjust source_timerange to fit the new video duration
                                                new_source_duration = video.duration - current_source.start
                                                if new_source_duration <= 0:
                                                    logger.warning(f"Warning: Video segment {segment.segment_id} start time {current_source.start} exceeds video duration {video.duration}, will skip this segment.")
                                                    continue
                                                    
                                                # Update source_timerange
                                                segment.source_timerange = draft.Timerange(current_source.start, new_source_duration)
                                                
                                                # Update target_timerange based on new source_timerange and speed
                                                new_target_duration = int(new_source_duration / speed)
                                                segment.target_timerange = draft.Timerange(current_target.start, new_target_duration)
                                                
                                                logger.info(f"Adjusted video segment {segment.segment_id} timerange to fit the new video duration.")
                        else:
                            logger.warning(f"Warning: Unable to get video {material_name} stream information.")
                            # Set default values
                            video.width = 1920
                            video.height = 1080
                    else:
                        logger.warning(f"Warning: Could not find JSON data in ffprobe output.")
                        # Set default values
                        video.width = 1920
                        video.height = 1080
                except Exception as e:
                    logger.error(f"Error occurred while getting video {material_name} information: {str(e)}, using default values 1920x1080.", exc_info=True)
                    # Set default values
                    video.width = 1920
                    video.height = 1080
                    
                    # Try to get duration separately
                    try:
                        duration_result = get_video_duration(remote_url)
                        if duration_result["success"]:
                            # Convert seconds to microseconds
                            video.duration = int(duration_result["output"] * 1000000)
                            logger.info(f"Successfully obtained video {material_name} duration: {duration_result['output']:.2f} seconds ({video.duration} microseconds).")
                        else:
                            logger.warning(f"Warning: Unable to get video {material_name} duration: {duration_result['error']}.")
                    except Exception as e2:
                        logger.error(f"Error occurred while getting video {material_name} duration: {str(e2)}.", exc_info=True)

    # After updating all segments' timerange, check if there are time range conflicts in each track, and delete the later segment in case of conflict
    logger.info("Checking track segment time range conflicts...")
    for track_name, track in script.tracks.items():
        # Use a set to record segment indices that need to be deleted
        to_remove = set()
        
        # Check for conflicts between all segments
        for i in range(len(track.segments)):
            # Skip if current segment is already marked for deletion
            if i in to_remove:
                continue
                
            for j in range(len(track.segments)):
                # Skip self-comparison and segments already marked for deletion
                if i == j or j in to_remove:
                    continue
                    
                # Check if there is a conflict
                if track.segments[i].overlaps(track.segments[j]):
                    # Always keep the segment with the smaller index (added first)
                    later_index = max(i, j)
                    logger.warning(f"Time range conflict between segments {track.segments[min(i, j)].segment_id} and {track.segments[later_index].segment_id} in track {track_name}, deleting the later segment")
                    to_remove.add(later_index)
        
        # Delete marked segments from back to front to avoid index change issues
        for index in sorted(to_remove, reverse=True):
            track.segments.pop(index)

    # After updating all segments' timerange, recalculate the total duration of the script
    max_duration = 0
    for track_name, track in script.tracks.items():
        for segment in track.segments:
            max_duration = max(max_duration, segment.end)
    script.duration = max_duration
    logger.info(f"Updated script total duration to: {script.duration} microseconds.")
    
    # Process all pending keyframes in tracks
    logger.info("Processing pending keyframes...")
    for track_name, track in script.tracks.items():
        if hasattr(track, 'pending_keyframes') and track.pending_keyframes:
            logger.info(f"Processing {len(track.pending_keyframes)} pending keyframes in track {track_name}...")
            track.process_pending_keyframes()
            logger.info(f"Pending keyframes in track {track_name} have been processed.")

def query_script_impl(draft_id: str, force_update: bool = True):
    """
    Query draft script object, with option to force refresh media metadata
    
    :param draft_id: Draft ID
    :param force_update: Whether to force refresh media metadata, default is True
    :return: Script object
    """
    try:
        script = get_draft(draft_id)
    except DraftNotFound:
        logger.warning(f"Draft {draft_id} not found.")
        return None
    
    # If force_update is True, force refresh media metadata
    if force_update:
        logger.info(f"Force refreshing media metadata for draft {draft_id}.")
        update_media_metadata(script)
    
    # Return script object
    return script

def download_script(draft_id: str, draft_folder: str = None, script_data: Dict = None) -> Dict[str, str]:
    """Downloads the draft script and its associated media assets.

    This function fetches the script object from a remote API,
    then iterates through its materials (audios, videos, images)
    to download them to the specified draft folder. It also updates
    task status and progress throughout the process.

    :param draft_id: The ID of the draft to download.
    :param draft_folder: The base folder where the draft's assets will be stored.
                         If None, assets will be stored directly under a folder named
                         after the draft_id in the current working directory.
    :return: A dictionary indicating success and, if successful, the URL where the draft
             would eventually be saved (though this function primarily focuses on download).
             If failed, it returns an error message.
    """

    logger.info(f"Starting to download draft: {draft_id} to folder: {draft_folder}")
    # Copy template to target directory
    draft_profile = get_draft_profile()
    template_path = os.path.join("./", draft_profile.template_dir)
    new_draft_path = os.path.join(draft_folder, draft_id)
    if os.path.exists(new_draft_path):
        logger.warning(f"Deleting existing draft target folder: {new_draft_path}")
        shutil.rmtree(new_draft_path)

    # Copy draft folder
    shutil.copytree(template_path, new_draft_path)
    
    try:
        # 1. Fetch the script from the remote endpoint
        if script_data is None:
            query_url = "https://cut-jianying-vdvswivepm.cn-hongkong.fcapp.run/query_script"
            headers = {"Content-Type": "application/json"}
            payload = {"draft_id": draft_id}

            logger.info(f"Attempting to get script for draft ID: {draft_id} from {query_url}.")
            response = requests.post(query_url, headers=headers, json=payload)
            response.raise_for_status()  # Raise an exception for HTTP errors (4xx or 5xx)
            
            script_data = json.loads(response.json().get('output'))
            logger.info(f"Successfully retrieved script data for draft {draft_id}.")
        else:
            logger.info(f"Using provided script_data, skipping remote retrieval.")

        # Collect download tasks
        download_tasks = []
        
        # Collect audio download tasks
        audios = script_data.get('materials',{}).get('audios',[])
        if audios:
            for audio in audios:
                remote_url = audio['remote_url']
                material_name = audio['name']
                # Use helper function to build path
                if draft_folder:
                    audio['path']=build_asset_path(draft_folder, draft_id, "audio", material_name)
                    logger.debug(f"Local path for audio {material_name}: {audio['path']}")
                if not remote_url:
                    logger.warning(f"Audio file {material_name} has no remote_url, skipping download.")
                    continue
                
                # Add audio download task
                download_tasks.append({
                    'type': 'audio',
                    'func': download_file,
                    'args': (remote_url, audio['path']),
                    'material': audio
                })
        
        # Collect video and image download tasks
        videos = script_data['materials']['videos']
        if videos:
            for video in videos:
                remote_url = video['remote_url']
                material_name = video['material_name']
                
                if video['type'] == 'photo':
                    # Use helper function to build path
                    if draft_folder:
                        video['path'] = build_asset_path(draft_folder, draft_id, "image", material_name)
                    if not remote_url:
                        logger.warning(f"Image file {material_name} has no remote_url, skipping download.")
                        continue
                    
                    # Add image download task
                    download_tasks.append({
                        'type': 'image',
                        'func': download_file,
                        'args': (remote_url, video['path']),
                        'material': video
                    })
                
                elif video['type'] == 'video':
                    # Use helper function to build path
                    if draft_folder:
                        video['path'] = build_asset_path(draft_folder, draft_id, "video", material_name)
                    if not remote_url:
                        logger.warning(f"Video file {material_name} has no remote_url, skipping download.")
                        continue
                    
                    # Add video download task
                    download_tasks.append({
                        'type': 'video',
                        'func': download_file,
                        'args': (remote_url, video['path']),
                        'material': video
                    })

        # Several clips of the same file share one destination: copy/download it only once
        unique_tasks = {}
        for task in download_tasks:
            unique_tasks.setdefault(task['args'][1], task)
        download_tasks = list(unique_tasks.values())

        # Execute all download tasks concurrently
        downloaded_paths = []
        completed_files = 0
        if download_tasks:
            logger.info(f"Starting concurrent download of {len(download_tasks)} files...")
            
            # Use thread pool for concurrent downloads, maximum concurrency of 16
            with ThreadPoolExecutor(max_workers=16) as executor:
                # Submit all download tasks
                future_to_task = {
                    executor.submit(task['func'], *task['args']): task 
                    for task in download_tasks
                }
                
                # Wait for all tasks to complete
                for future in as_completed(future_to_task):
                    task = future_to_task[future]
                    try:
                        local_path = future.result()
                        downloaded_paths.append(local_path)
                        
                        # Update task status - only update completed files count
                        completed_files += 1
                        logger.info(f"Downloaded {completed_files}/{len(download_tasks)} files.")
                    except Exception as e:
                        logger.error(f"Failed to download {task['type']} file {task['args'][0]}: {str(e)}", exc_info=True)
                        logger.error("Download failed.")
                        # Continue processing other files, don't interrupt the entire process
            
            logger.info(f"Concurrent download completed, downloaded {len(downloaded_paths)} files in total.")
        
        """Write draft file content to file"""
        write_profile_content(draft_profile, os.path.join(draft_folder, draft_id), json.dumps(script_data, ensure_ascii=False))
        logger.info(f"Draft has been saved.")

        # No draft_url for download, but return success
        return {"success": True, "message": f"Draft {draft_id} and its assets downloaded successfully"}

    except requests.exceptions.RequestException as e:
        logger.error(f"API request failed: {e}", exc_info=True)
        return {"success": False, "error": f"Failed to fetch script from API: {str(e)}"}
    except Exception as e:
        logger.error(f"Unexpected error during download: {e}", exc_info=True)
        return {"success": False, "error": f"An unexpected error occurred: {str(e)}"}

if __name__ == "__main__":
    print('hello')
    download_script("dfd_cat_1751012163_a7e8c315",'/Users/sunguannan/Movies/JianyingPro/User Data/Projects/com.lveditor.draft')
