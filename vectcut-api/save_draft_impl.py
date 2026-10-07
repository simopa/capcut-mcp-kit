# Modified in capcut-mcp-kit (2026) from VectCutAPI @ cfa4779: save straight into the local CapCut drafts folder (macOS/Windows), rename the folder to project_name, fix draft_meta_info.json; media under ~/Movies referenced in place; each file copied once even when used by many clips; validated folder names, staging + swap, existing folders moved to backups instead of deleted, failures reported; drafts opened from an existing project are saved by existing_project; one lock per project folder held for the whole save, checks repeated at commit, rename without replacing, the old folder kept on the same volume until the new one is in, every path and identity journalled before the first change and unfinished saves settled from that plan (never touching what something else changed since); a folder may be replaced only if the draft's own state says it saved it; CapCut must be known to be closed.
# See NOTICE at the repository root.
import contextlib
import ctypes
import errno
import os
import sys
import pyJianYingDraft as draft
import shutil
from util import zip_draft, build_draft_asset_path, source_changed, content_digest
from oss import upload_to_oss
from typing import Dict, Literal
from draft_store import (get_draft, DraftNotFound, project_lock, until_commit, journal_begin, journal_update,
                         journal_pending, journal_get, committed_to_disk, NOTICES)
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
    """Return the local CapCut/Jianying desktop drafts directory, or None if not found.
    CAPCUT_PROJECTS_DIR, if set, is used instead (another folder of projects, or tests)."""
    configured = os.environ.get("CAPCUT_PROJECTS_DIR")
    if configured:
        configured = os.path.expanduser(configured)
        return configured if os.path.isdir(configured) else None
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
    if os.path.islink(root):
        raise SaveDraftError(f"'{os.path.basename(root)}' is a symbolic link: the kit only reads and writes inside "
                             f"project folders. Nothing was written.")
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

def backup_dest(name, taken=()):
    """A new, unused path in the backups folder for a copy of the project `name` (and none of
    `taken`, paths already chosen for other copies)."""
    root = backup_root()
    os.makedirs(root, exist_ok=True)
    base = f"{name} {time.strftime('%Y%m%d-%H%M%S')}"
    dest, n = os.path.join(root, base), 1
    while os.path.lexists(dest) or os.path.lexists(dest + ".partial") or dest in taken:
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

def move_to_backup(path, name=None, dest=None):
    """Move a folder to the backups (to dest, or a new path there) instead of deleting it. Returns
    where it went. On another volume it is copied in full (to a .partial folder renamed once
    complete) before the original is removed; if the removal fails the backup is complete and the
    error says what is left."""
    dest = dest or backup_dest(name or os.path.basename(path))
    if os.path.lexists(dest):
        raise SaveDraftError(f"{dest} already exists")
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

def _identity(path):
    """[device, inode] of what is at path (a link itself, not what it points to), or None. A
    rename keeps it: it tells the folder this save created or checked from anything put at the
    same path by someone else."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return [st.st_dev, st.st_ino]

def _hidden(parent, kind):
    """A new hidden path next to the project folders, for this save only."""
    return child_dir(parent, f".capcut-mcp-{kind}-{uuid.uuid4().hex[:16]}")

def check_replace(target, saved, overwrite, in_capcut, content_file="draft_info.json"):
    """Refuse to replace a folder this draft did not save, or that changed since this draft saved
    it (unless overwrite), and any CapCut project while CapCut is open (or might be). Returns what
    is at target as checked, for the swap to verify again: {"exists": False}, or {"exists": True,
    "id": its identity, "sha": sha256 of its timeline or None, "inventory": every entry's content}."""
    if not os.path.lexists(target):
        return {"exists": False}
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
    return {"exists": True, "id": _identity(target), "sha": current, "inventory": _inventory(target)}

def swap_in(pair, content_file):
    """Put the staging folder at the target, along the paths journalled beforehand in pair. A
    target that was there when checked is renamed to pair["aside"] (same folder: atomic) and
    verified to be the folder checked, with the same timeline; a target absent when checked must
    still be absent. The stage is then renamed in without replacing anything that appeared
    meanwhile. Raises SaveDraftError; if a folder took the name between the two renames, the
    previous version stays at the aside path for the caller to settle (see _settle_replace)."""
    target, stage, aside, old = pair["target"], pair["stage"], pair["aside"], pair["old"]
    name = os.path.basename(target)
    if old["exists"]:
        try:
            rename_noreplace(target, aside)
        except OSError:
            raise SaveDraftError(f"'{name}' changed while it was being saved: nothing was replaced, try again")
        if _identity(aside) != old["id"] or not _matches_inventory(aside, old.get("inventory")):
            rename_noreplace(aside, target)
            raise SaveDraftError(f"'{name}' changed while it was being saved: nothing was replaced, try again")
    try:
        rename_noreplace(stage, target)
    except OSError:
        if old["exists"]:
            try:
                rename_noreplace(aside, target)
            except OSError:
                raise SaveDraftError(f"A folder named '{name}' appeared while saving and was left as it is")
        raise SaveDraftError(f"A folder named '{name}' appeared while saving: nothing was replaced, try again")

def _inventory(folder):
    """Every entry of a folder this save wrote, with the content of each file: what makes it this
    save's own folder, unchanged (the identity alone does not: files can be edited or added in it)."""
    out = {}
    def unreadable(error):
        raise error
    if os.path.islink(folder) or not os.path.isdir(folder):
        raise SaveDraftError(f"{folder} is not a regular project folder")
    for dirpath, dirnames, files in os.walk(folder, onerror=unreadable):
        for name in dirnames + files:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, folder).replace(os.sep, "/")
            if os.path.islink(path):
                out[rel] = "link:" + os.readlink(path)
            elif os.path.isdir(path):
                out[rel] = "dir"
            else:
                out[rel] = content_digest(path)
    return out

def _matches_inventory(folder, expected):
    """Missing plans and unreadable folders never count as an unchanged version."""
    try:
        return expected is not None and _inventory(folder) == expected
    except (OSError, SaveDraftError):
        return False

def _pair_state(pair):
    """Where a journalled swap stands: "new" (the staged folder is at target, exactly as written),
    "changed" (it is, but something in it was edited or added since), "old" (the target is as it
    was before the save), "aside" (stopped between the two renames: the previous version is at the
    aside path, nothing at target) or "foreign" (something else is at target)."""
    t, old, new = _identity(pair["target"]), pair["old"], pair["new"]
    if old["exists"] and os.path.lexists(pair["aside"]) and \
            (_identity(pair["aside"]) != old["id"] or not _matches_inventory(pair["aside"], old.get("inventory"))):
        # A verified completed backup can coexist with an aside partially removed by a crash.
        # Recovery may finish that removal, but must never restore the partial aside.
        if not (t == new["id"] and pair.get("backup") and
                _identity(pair["backup"]) in (old["id"], pair.get("backup_id")) and
                _matches_inventory(pair["backup"], old.get("inventory"))):
            return "foreign"
    if t is not None and t == new["id"]:
        return "new" if new.get("inventory") is not None and _inventory(pair["target"]) == new["inventory"] \
            else "changed"
    if not old["exists"]:
        return "old" if t is None else "foreign"
    if t == old["id"]:
        return "old" if _matches_inventory(pair["target"], old.get("inventory")) else "foreign"
    if t is None and _identity(pair["aside"]) == old["id"]:
        return "aside" if _matches_inventory(pair["aside"], old.get("inventory")) else "foreign"
    return "foreign"

def _remove_leftovers(pair):
    """Remove what this save created and is no longer in use. Its staging folder (a hidden path of
    this save only) by identity; the new folder moved out by an undo only if it is still exactly
    what the save wrote, otherwise it goes to the backups. Returns notes."""
    new = pair.get("new") or {}
    if new.get("id") is not None and _identity(pair["stage"]) == new["id"]:
        shutil.rmtree(pair["stage"])
    if new.get("id") is not None and _identity(pair["failed"]) == new["id"]:
        if new.get("inventory") is not None and _inventory(pair["failed"]) == new["inventory"]:
            shutil.rmtree(pair["failed"])
        else:
            where = move_to_backup(pair["failed"], os.path.basename(pair["target"]))
            return [f"the version of '{pair['target']}' an interrupted save wrote had changed since; it is at {where}"]
    return []

def undo_swap(pair):
    """Put back what swap_in replaced, for a pair whose state is "new" or "aside": the new folder
    is moved out (and removed if unchanged), the previous version renamed back. Returns notes."""
    target = pair["target"]
    if _identity(target) == pair["new"]["id"]:
        rename_noreplace(target, pair["failed"])
    if pair["old"]["exists"] and not os.path.lexists(target) and _identity(pair["aside"]) == pair["old"]["id"]:
        rename_noreplace(pair["aside"], target)
    return _remove_leftovers(pair)

def _partial_copy_matches(folder, reference, expected):
    """Whether an interrupted copy contains only entries/prefixes still held in reference.
    A partial with anything else is kept: its directory identity does not own later edits.
    """
    def unreadable(error):
        raise error
    try:
        if os.path.islink(folder) or not os.path.isdir(folder):
            return False
        for parent, dirs, files in os.walk(folder, onerror=unreadable):
            for name in dirs + files:
                path = os.path.join(parent, name)
                rel = os.path.relpath(path, folder).replace(os.sep, "/")
                if rel not in expected:
                    return False
                if os.path.islink(path):
                    if expected[rel] != "link:" + os.readlink(path):
                        return False
                elif os.path.isdir(path):
                    if expected[rel] != "dir":
                        return False
                else:
                    src = project_path(reference, *rel.split("/"))
                    if not os.path.isfile(path) or not os.path.isfile(src):
                        return False
                    with open(path, "rb") as partial_file, open(src, "rb") as complete_file:
                        for chunk in iter(lambda: partial_file.read(1 << 20), b""):
                            if chunk != complete_file.read(len(chunk)):
                                return False
        return True
    except (OSError, SaveDraftError):
        return False

def _retire(op_id, details, pair):
    """Move the previous version of a target (at its aside path) to the backups, at a destination
    journalled before the move, so a stop halfway (a copy to another volume) is finished or cleaned
    up from the journal. Returns (where it is, warning or None): if it cannot be moved it stays
    next to the target, hidden, and the warning says where."""
    name = os.path.basename(pair["target"])
    aside = pair["aside"]
    try:
        expected = pair["old"].get("inventory")
        if expected is None:
            raise SaveDraftError("the earlier save did not record the previous folder's inventory; keep it for manual recovery")
        if not pair.get("backup"):
            pair["backup"] = backup_dest(name, taken={p.get("backup") for p in details["pairs"]})
            journal_update(op_id, details=details)
        dest = pair["backup"]
        partial = dest + ".partial"

        def clean_partial(reference):
            if not os.path.lexists(partial):
                return
            if not os.path.islink(partial) and os.path.isdir(partial) and not os.listdir(partial):
                # Empty: it holds no data, whoever made it (a stop right after creating it, before
                # it was journalled). rmdir fails if anything appears in it meanwhile.
                os.rmdir(partial)
                return
            if os.path.islink(partial) or _identity(partial) != pair.get("backup_partial_id"):
                raise SaveDraftError(f"{partial} is not a staging folder owned by this save; it was left as it is")
            entries = os.listdir(partial)
            if entries and (entries != ["project"] or not _partial_copy_matches(
                    os.path.join(partial, "project"), reference, expected)):
                raise SaveDraftError(f"{partial} contains data not proven to be in the original; it was kept")
            shutil.rmtree(partial)

        def finish_copy():
            # Identity proves ownership; content proves completeness. Neither alone suffices.
            if _identity(dest) not in (pair["old"]["id"], pair.get("backup_id")) or \
                    not _matches_inventory(dest, expected):
                raise SaveDraftError(f"{dest} is not a verified complete backup; the previous version was kept")
            clean_partial(dest)
            if os.path.lexists(aside):
                if _identity(aside) != pair["old"]["id"]:
                    raise SaveDraftError(f"{aside} was replaced; it was kept")
                # A crash during rmtree can leave only a subset. Every remaining entry must
                # still be in the verified backup, with identical content, before removal.
                remaining = _inventory(aside)
                if any(expected.get(rel) != value for rel, value in remaining.items()):
                    raise SaveDraftError(f"{aside} changed after the backup; it was kept")
                shutil.rmtree(aside)
            return dest, None

        if os.path.lexists(dest):
            return finish_copy()
        if not _matches_inventory(aside, expected):
            raise SaveDraftError(f"{aside} changed since the save was planned; it was kept")
        clean_partial(aside)
        try:
            rename_noreplace(aside, dest)
            return dest, None
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise
        # An exclusively created container lets a restart distinguish our incomplete copy
        # from a foreign .partial. Copy inside it, then publish the verified child unchanged.
        os.mkdir(partial)
        pair["backup_partial_id"] = _identity(partial)
        journal_update(op_id, details=details)
        copied = os.path.join(partial, "project")
        clone_tree(aside, copied)
        if not _matches_inventory(copied, expected) or not _matches_inventory(aside, expected):
            raise SaveDraftError("the previous version changed while its backup was being copied; both were kept")
        pair["backup_id"] = _identity(copied)
        journal_update(op_id, details=details)
        rename_noreplace(copied, dest)
        return finish_copy()
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
    Every copy is checked to be the version of the file the material was added with (its content).
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
        name, (url, copy) = task['material'].material_name, task['args']
        if not os.path.isfile(copy):
            if not any(m.startswith(name) for m in missing):
                missing.append(f"{name} (not copied)")
        elif source_changed(name, url, copy):
            missing.append(f"{name} ({url} was replaced or edited after it was added: add it again)")
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

# --- Saves that did not finish -------------------------------------------------------------------
#
# A save journals its plan before it creates anything: every path it will use (staging folder,
# where the previous version goes, ...) while preparing, then, before the first change to a
# project folder, the identity and content of what is there and of what replaces it. A save that
# stops halfway (crash, or the draft could not be recorded) is settled from that plan and from
# what is on disk now, under the folders' locks: either all of it happened (it is recorded in the
# draft) or none of it (what it created is removed, what it moved is put back, then checked). If
# something else changed a folder since, nothing there is touched: the entry is 'abandoned' and a
# note says where the previous version is. Undoing anything inside CapCut's projects folder waits
# until CapCut is known to be closed.

def _settle_replace(op_id, details, script, forward=True):
    """Settle a save that replaced project folders and did not finish (crash, the draft could not
    be recorded, or forward=False: the save itself failed and is undone). Every pair is judged
    before anything is done (_pair_state). If something else changed any of them, nothing is
    undone: each previous version still hidden is put back or moved to the backups, and the note
    names where every version is. Otherwise, if all are new (and forward) the save is recorded in
    the draft; if not, all go back as they were. Returns notes for the user."""
    pairs = details["pairs"]
    if details.get("phase") != "ready":
        # Stopped while preparing: no project folder was touched. The staging folders were
        # created after this entry, under the lock, at paths of this save only.
        for pair in pairs:
            if os.path.isdir(pair["stage"]) and not os.path.islink(pair["stage"]):
                shutil.rmtree(pair["stage"])
        journal_update(op_id, "rolled_back")
        return []
    states = [_pair_state(pair) for pair in pairs]
    if not (forward and all(state == "new" for state in states)):
        # A retired old version is safe in its backup, but cannot be restored by undo_swap.
        # If the destinations are now mixed, preserve them rather than remove a target whose
        # aside is absent or only partly left after an interrupted retirement.
        for i, (pair, state) in enumerate(zip(pairs, states)):
            old = pair["old"]
            if state == "new" and old["exists"] and (_identity(pair["aside"]) != old["id"] or
                    not _matches_inventory(pair["aside"], old.get("inventory"))):
                states[i] = "foreign"
    if all(state == "old" or (state == "foreign" and _identity(pair["target"]) != pair["new"]["id"]
                             and _identity(pair["aside"]) != pair["old"].get("id"))
           for pair, state in zip(pairs, states)):
        # Nothing of this save is in place and no previous version was moved: only leftovers to remove
        notes = [note for pair in pairs for note in _remove_leftovers(pair)]
        journal_update(op_id, "rolled_back", details={**details, "notes": notes} if notes else None)
        return notes
    if any(state in ("foreign", "changed") for state in states):
        notes = []
        for pair, state in zip(pairs, states):
            target = pair["target"]
            where = None
            if state == "aside" and not os.path.lexists(target):
                rename_noreplace(pair["aside"], target)
            elif pair["old"]["exists"] and (_identity(pair["aside"]) == pair["old"]["id"] or pair.get("backup")):
                where, warning = _retire(op_id, details, pair)
                if warning:
                    notes.append(warning)
            notes += _remove_leftovers(pair)
            before = f"; the version before that save is at {where}" if where else ""
            if state == "foreign":
                notes.append(f"'{target}' or its previous version no longer matches the interrupted save's "
                             f"plan: it was left as it is{before}")
            elif state == "changed":
                notes.append(f"'{target}' holds the version that save wrote, changed since: it was left as it is and "
                             f"is not recorded in the draft{before}")
            elif state == "new":
                notes.append(f"'{target}' holds the version that save wrote, not recorded in the draft{before}")
            elif state == "aside":
                notes.append(f"'{target}' was put back as it was before that save")
        journal_update(op_id, "abandoned", details={**details, "notes": notes})
        return notes
    if forward and all(state == "new" for state in states):
        notes = []
        for pair in pairs:
            if pair["old"]["exists"] and (_identity(pair["aside"]) == pair["old"]["id"] or pair.get("backup")):
                _, warning = _retire(op_id, details, pair)
                if warning:
                    notes.append(warning)
            notes += _remove_leftovers(pair)
        if notes:
            journal_update(op_id, "abandoned", details={**details, "notes": notes})
        if script is None:
            if not notes:
                journal_update(op_id, "done")  # the draft is gone: nothing to record it in
            return notes
        for pair in pairs:
            kit_saves(script)[os.path.realpath(pair["target"])] = pair["new"]["sha"]
        committed_to_disk(op_id, "an earlier save, reconciled")
        return notes
    # Not all of it happened: everything goes back as it was
    if any(state == "new" and pair["in_capcut"] for pair, state in zip(pairs, states)) and \
            capcut_is_running() is not False:
        return [f"A save of this draft stopped halfway; it is undone once CapCut is known to be closed: quit "
                f"CapCut, then make any change to this draft (or restart the backend). Nothing was written meanwhile."]
    notes = []
    for pair, state in zip(pairs, states):
        if state in ("new", "aside"):
            notes += undo_swap(pair)
        else:
            notes += _remove_leftovers(pair)
    if all(_pair_state(pair) == "old" for pair in pairs):
        journal_update(op_id, "rolled_back", details={**details, "notes": notes} if notes else None)
        return notes
    return notes + [f"A save of this draft stopped halfway and could not be fully undone: check "
                    f"{', '.join(p['target'] for p in pairs)}"]

def settle_saves(draft_id, script):
    """Settle the journalled saves of draft_id that did not finish, recording in script (the
    draft's working copy, None if the draft is gone) what they wrote. Each entry is read again
    under its folders' locks, so one settled meanwhile is not settled twice. Returns notes for
    the user: saves left pending (CapCut open) or abandoned (something else changed the folder)."""
    import existing_project
    notes = []
    for op_id, _, kind, _ in journal_pending(draft_id):
        with contextlib.ExitStack() as stack:
            recorded = journal_get(op_id)
            for folder in sorted((recorded[1] if recorded else {}).get("locks") or []):
                stack.enter_context(until_commit(project_lock(folder)))
            recorded = journal_get(op_id)
            if recorded is None or recorded[0] != "pending":
                continue
            details = recorded[1]
            try:
                if "phase" not in details:
                    places = [details.get("dir"), details.get("backup")] + \
                        [p.get(k) for p in details.get("pairs", []) for k in ("target", "aside", "stage")]
                    places = [p for p in places if p and os.path.lexists(p)]
                    note = (f"A save of this draft by an older version of the kit did not finish; nothing was changed. "
                            f"Check these folders by hand (the previous version is in the backups or in a hidden "
                            f".capcut-mcp-old-* folder): {', '.join(places) or 'none of them exists any more'}")
                    journal_update(op_id, "abandoned", details={**details, "notes": [note]})
                    notes.append(note)
                elif kind == "replace":
                    notes += _settle_replace(op_id, details, script)
                elif kind == "existing":
                    notes += existing_project.settle(op_id, details, script)
            except Exception as e:
                logger.error(f"Could not settle save {op_id}: {e}", exc_info=True)
                notes.append(f"A save of this draft did not finish and could not be settled ({e}); nothing else "
                             f"was changed")
    return notes

def recover_saves():
    """At startup: settle every journalled save a previous run left unfinished, each recorded in
    its own draft. Returns the notes (also logged)."""
    import draft_store
    notes = []
    for draft_id in dict.fromkeys(op[1] for op in journal_pending()):
        try:
            notes += draft_store.reconcile(draft_id)
        except Exception as e:
            logger.error(f"Could not settle the saves of draft {draft_id}: {e}", exc_info=True)
    for note in notes:
        logger.warning(note)
    return notes

def ensure_settled(draft_id, folders):
    """Refuse to save while a save of this draft, or one into these folders, is unfinished."""
    folders = set(folders)
    for op_id, op_draft, kind, details in journal_pending():
        if op_draft == draft_id:
            notes = NOTICES.get() or []
            raise SaveDraftError(notes[-1] if notes else "An earlier save of this draft did not finish and could not "
                                 "be settled: this save was not attempted. Nothing was written.")
        if folders & set(details.get("locks") or []):
            raise SaveDraftError(f"A save of another draft ({op_draft}) into the same folder did not finish: it is "
                                 f"settled by any change to that draft (a capcut_add_* call or capcut_save_draft on "
                                 f"it) or by restarting the backend. Nothing was written.")

def save_draft_background(draft_id, draft_folder, task_id, project_name=None, auto_deploy=True, overwrite=False):
    """Save a draft. Under the destination folders' locks: the paths it will use are journalled
    before anything is created; the draft is written into a staging folder next to its
    destination and every asset checked; what is there is checked again and journalled with what
    replaces it; then it is swapped in. An existing folder is moved to backups, never deleted.
    Returns {"draft_url": path, "backups": [...], "warnings"?}; raises on failure with nothing
    replaced."""
    try:
        validate_folder_name(draft_id, "draft_id")
        if project_name:
            project_name = validate_folder_name(project_name)
        try:
            script = get_draft(draft_id)
        except DraftNotFound as e:
            raise SaveDraftError(str(e))
        notes = list(NOTICES.get() or [])
        if getattr(script, "base_project", None):
            # Opened with capcut_open_project: add to that project, never replace it
            if project_name and project_name.strip() != script.base_project["name"]:
                raise SaveDraftError(f"This draft adds to the existing project '{script.base_project['name']}': "
                                     f"leave project_name out (or use that name)")
            import existing_project
            result = existing_project.save_into_existing(draft_id, script, task_id)
            if notes:
                result["warnings"] = notes + result.get("warnings", [])
            return result

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
        draft_profile = get_draft_profile()
        content_file = draft_profile.content_file
        template_source_dir = os.path.join(current_dir, draft_profile.template_dir)
        if not os.path.exists(template_source_dir):
            raise FileNotFoundError(f"Template draft {draft_profile.template_dir} does not exist")
        locks = sorted({os.path.realpath(t) for t, _ in targets})

        backups, warnings = [], list(notes)
        with contextlib.ExitStack() as stack:
            for folder in locks:
                stack.enter_context(until_commit(project_lock(folder)))
            ensure_settled(draft_id, locks)
            for target, in_capcut in targets:  # refuse before writing anything
                check_replace(target, saved, overwrite, in_capcut, content_file)

            pairs = [{"target": target, "in_capcut": in_capcut,
                      "stage": _hidden(os.path.dirname(target), "saving"),
                      "aside": _hidden(os.path.dirname(target), "old"),
                      "failed": _hidden(os.path.dirname(target), "failed")} for target, in_capcut in targets]
            details = {"phase": "preparing", "content_file": content_file, "pairs": pairs, "locks": locks}
            op_id = journal_begin(draft_id, "replace", details)
            try:
                stage_dir = pairs[0]["stage"]
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
                if deploy_dir:
                    # Saved elsewhere: also place a copy in CapCut's drafts directory (committed together)
                    deploy_stage = pairs[1]["stage"]
                    shutil.copytree(stage_dir, deploy_stage)
                    lock_f = os.path.join(deploy_stage, ".locked")
                    if os.path.exists(lock_f):
                        os.remove(lock_f)
                    previous = read_meta(deploy_dir) if os.path.realpath(deploy_dir) in saved else None
                    fix_draft_meta(deploy_stage, deploy_dir, project_name or draft_id, script.duration, previous)

                # What is there now (checked again: CapCut does not take the kit's locks) and what
                # replaces it, journalled before the first rename
                for pair in pairs:
                    pair["new"] = {"sha": content_sha, "id": _identity(pair["stage"]),
                                   "inventory": _inventory(pair["stage"])}
                    pair["old"] = check_replace(pair["target"], saved, overwrite, pair["in_capcut"], content_file)
                details["phase"] = "ready"
                journal_update(op_id, details=details)
            except BaseException:
                try:
                    for pair in pairs:
                        if os.path.isdir(pair["stage"]) and not os.path.islink(pair["stage"]):
                            shutil.rmtree(pair["stage"])
                    journal_update(op_id, "rolled_back")
                except Exception as e:
                    logger.error(f"Could not clean up save {op_id}: {e}", exc_info=True)
                raise

            try:
                for pair in pairs:
                    swap_in(pair, content_file)
            except BaseException as e:
                # Undone with the same checks as a recovery after a crash
                try:
                    undo_notes = _settle_replace(op_id, details, None, forward=False)
                except Exception as undo_error:
                    logger.error(f"Could not undo save {op_id}: {undo_error}", exc_info=True)
                    undo_notes = [f"It could not be undone ({undo_error}): it is settled by the next change of this "
                                  f"draft or by restarting the backend"]
                if undo_notes:
                    raise SaveDraftError(f"{e}. " + " ".join(undo_notes)) from e
                raise
            for pair in pairs:
                saved[os.path.realpath(pair["target"])] = content_sha
                if pair["old"]["exists"]:
                    where, warning = _retire(op_id, details, pair)
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
