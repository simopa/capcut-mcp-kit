# Modified in capcut-mcp-kit (2026) from VectCutAPI @ cfa4779: save straight into the local CapCut drafts folder (macOS/Windows), rename the folder to project_name, fix draft_meta_info.json; media under ~/Movies referenced in place; each file copied once even when used by many clips; validated folder names, staging + swap, existing folders moved to backups instead of deleted, failures reported.
# See NOTICE at the repository root.
import os
import pyJianYingDraft as draft
import shutil
from util import zip_draft, build_draft_asset_path
from oss import upload_to_oss
from typing import Dict, Literal
from draft_cache import DRAFT_CACHE
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

def use_in_place(material, source) -> bool:
    """Reference local media where it is instead of copying it into the project. CapCut is
    sandboxed and can only open files under ~/Movies on its own, so this applies there; other
    local files are cloned into the project (copy-on-write, see downloader.download_file).
    Returns True if handled."""
    local = os.path.realpath(os.path.expanduser(str(source)))
    movies = os.path.realpath(os.path.expanduser("~/Movies")) + os.sep
    if IS_UPLOAD_DRAFT or not os.path.isfile(local) or not local.startswith(movies):
        return False
    material.replace_path = local
    return True

# Written into every folder this kit saves, so a later save of the same draft can tell its own
# project apart from one it must not replace.
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

def capcut_is_running():
    """True/False, or None when it cannot be determined."""
    try:
        if os.name == 'nt':
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq CapCut.exe", "/NH"],
                                 capture_output=True, text=True, timeout=5).stdout
            return "CapCut.exe" in out
        return subprocess.run(["pgrep", "-x", "CapCut"], capture_output=True, timeout=5).returncode == 0
    except Exception:
        return None

def backup_root():
    configured = os.environ.get("CAPCUT_MCP_BACKUP_DIR")
    if configured:
        return os.path.expanduser(configured)
    if os.name == 'nt':
        return os.path.expandvars(r"%LOCALAPPDATA%\CapCut MCP Backups")
    return os.path.expanduser("~/Movies/CapCut MCP Backups")

def move_to_backup(path):
    """Move a folder out of the way instead of deleting it. Returns where it went."""
    root = backup_root()
    os.makedirs(root, exist_ok=True)
    base = f"{os.path.basename(path)} {time.strftime('%Y%m%d-%H%M%S')}"
    dest, n = os.path.join(root, base), 1
    while os.path.lexists(dest):
        n += 1
        dest = os.path.join(root, f"{base}-{n}")
    shutil.move(path, dest)
    logger.info(f"Moved {path} to backup {dest}")
    return dest

def read_kit_marker(folder):
    try:
        with open(os.path.join(folder, KIT_MARKER), "r", encoding="utf-8") as f:
            return json.load(f).get("draft_id")
    except (OSError, ValueError, AttributeError):
        return None

def read_meta(folder):
    try:
        with open(os.path.join(folder, "draft_meta_info.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None

def check_replace(target, draft_id, overwrite, in_capcut):
    """Refuse to replace a folder this draft did not save (unless overwrite), and any CapCut
    project while CapCut is open: it keeps the project in memory and writes it back on close."""
    if not os.path.lexists(target):
        return
    name = os.path.basename(target)
    if os.path.islink(target) or not os.path.isdir(target):
        raise SaveDraftError(f"'{name}' exists and is not a project folder")
    if read_kit_marker(target) != draft_id and not overwrite:
        raise SaveDraftError(
            f"A project named '{name}' already exists and was not saved from this draft. "
            f"Choose another project_name, or pass overwrite=true to replace it "
            f"(the old folder is moved to {backup_root()}, not deleted).")
    if in_capcut and capcut_is_running():
        raise SaveDraftError(
            f"CapCut is open: quit CapCut before replacing '{name}', otherwise it writes its own "
            f"copy back over the saved project when it closes. Saving under a new project_name works while it is open.")

def commit_dir(stage_dir, target):
    """Swap a fully written staging folder into place, moving any existing target to backups."""
    backup = move_to_backup(target) if os.path.lexists(target) else None
    try:
        os.rename(stage_dir, target)
    except Exception:
        if backup:
            shutil.move(backup, target)
        raise
    return backup

def write_kit_marker(folder, draft_id):
    with open(os.path.join(folder, KIT_MARKER), "w", encoding="utf-8") as f:
        json.dump({"draft_id": draft_id}, f)

def save_draft_background(draft_id, draft_folder, task_id, project_name=None, auto_deploy=True, overwrite=False):
    """Save a draft: write it into a staging folder next to its destination, check every asset
    arrived, then swap it in. An existing folder is moved to backups, never deleted.
    Returns {"draft_url": path, "backups": [...]}; raises on failure with nothing replaced."""
    stages = []
    try:
        validate_folder_name(draft_id, "draft_id")
        if project_name:
            project_name = validate_folder_name(project_name)
        if draft_id not in DRAFT_CACHE:
            raise SaveDraftError(f"Draft {draft_id} not found (drafts live in memory and are lost when "
                                 f"the backend restarts): create a new draft.")
        script = DRAFT_CACHE[draft_id]

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

        # Refuse before writing anything
        check_replace(draft_dir, draft_id, overwrite, in_capcut=in_place)
        if deploy_dir:
            check_replace(deploy_dir, draft_id, overwrite, in_capcut=True)

        draft_profile = get_draft_profile()
        template_source_dir = os.path.join(current_dir, draft_profile.template_dir)
        if not os.path.exists(template_source_dir):
            raise FileNotFoundError(f"Template draft {draft_profile.template_dir} does not exist")
        stage_dir = child_dir(output_base_dir, f".capcut-mcp-saving-{uuid.uuid4().hex[:8]}")
        stages.append(stage_dir)
        shutil.copytree(template_source_dir, stage_dir)

        update_task_fields(task_id, message="Updating media file metadata", progress=5)
        update_media_metadata(script, task_id)

        # replace_path is where the asset will be once the staging folder is renamed to draft_dir;
        # the file itself is copied into the staging folder.
        download_tasks = []
        missing = []
        materials = [(audio, "audio") for audio in (script.materials.audios or [])]
        for video in (script.materials.videos or []):
            if video.material_type == 'photo':
                materials.append((video, "image"))
            elif video.material_type == 'video':
                materials.append((video, "video"))
        for material, asset_type in materials:
            remote_url = material.remote_url
            material_name = material.material_name
            material.replace_path = build_asset_path(output_base_dir, final_name, asset_type, material_name)
            if not remote_url:
                missing.append(f"{material_name} (no source)")
                continue
            if use_in_place(material, remote_url):
                continue
            download_tasks.append({
                'type': asset_type,
                'func': download_file,
                'args': (remote_url, os.path.join(stage_dir, "assets", asset_type, material_name)),
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

        update_task_fields(task_id, message="Saving draft information", progress=70)
        written_files = write_profile_content(draft_profile, stage_dir, script.dumps(draft_profile))
        logger.info(f"Draft information has been saved to {[str(path) for path in written_files]}.")
        write_kit_marker(stage_dir, draft_id)
        if in_place:
            previous = read_meta(draft_dir) if read_kit_marker(draft_dir) == draft_id else None
            fix_draft_meta(stage_dir, draft_dir, project_name or draft_id, script.duration, previous)

        backups = []
        backup = commit_dir(stage_dir, draft_dir)
        stages.remove(stage_dir)
        if backup:
            backups.append(backup)

        if deploy_dir:
            # Saved elsewhere: also place a copy in CapCut's drafts directory
            deploy_stage = child_dir(capcut_projects_dir, f".capcut-mcp-saving-{uuid.uuid4().hex[:8]}")
            stages.append(deploy_stage)
            shutil.copytree(draft_dir, deploy_stage)
            lock_f = os.path.join(deploy_stage, ".locked")
            if os.path.exists(lock_f):
                os.remove(lock_f)
            previous = read_meta(deploy_dir) if read_kit_marker(deploy_dir) == draft_id else None
            fix_draft_meta(deploy_stage, deploy_dir, project_name or draft_id, script.duration, previous)
            backup = commit_dir(deploy_stage, deploy_dir)
            stages.remove(deploy_stage)
            if backup:
                backups.append(backup)
            logger.info(f"Auto-deployed draft to CapCut directory: {deploy_dir}")

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
        return {"draft_url": draft_url if IS_UPLOAD_DRAFT else (deploy_dir or draft_dir), "backups": backups}

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
    # Get draft information from global cache
    if draft_id not in DRAFT_CACHE:
        logger.warning(f"Draft {draft_id} does not exist in cache.")
        return None
        
    script = DRAFT_CACHE[draft_id]
    logger.info(f"Retrieved draft {draft_id} from cache.")
    
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
