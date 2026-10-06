# Modified in capcut-mcp-kit (2026) from VectCutAPI @ cfa4779: Windows asset paths built off Windows no longer double the drive separator; material names of local files depend on the file's version.
# See NOTICE at the repository root.
import shutil
import subprocess
import json
import re
import os
import hashlib
import functools
import time
from settings.local import DRAFT_DOMAIN, PREVIEW_ROUTER, IS_CAPCUT_ENV

def hex_to_rgb(hex_color: str) -> tuple:
    """Convert hexadecimal color code to RGB tuple (range 0.0-1.0)"""
    hex_color = hex_color.lstrip('#')
    if len(hex_color) == 3:
        hex_color = ''.join([c*2 for c in hex_color])  # Handle shorthand form (e.g. #fff)
    try:
        r = int(hex_color[0:2], 16) / 255.0
        g = int(hex_color[2:4], 16) / 255.0
        b = int(hex_color[4:6], 16) / 255.0
        return (r, g, b)
    except ValueError:
        raise ValueError(f"Invalid hexadecimal color code: {hex_color}")


def is_windows_path(path):
    """Detect if the path is Windows style"""
    # Check if it starts with a drive letter (e.g. C:\) or contains Windows style separators
    return re.match(r'^[a-zA-Z]:\\|\\\\', path) is not None

def build_draft_asset_path(draft_folder: str, draft_id: str, asset_type: str, material_name: str) -> str:
    """Build the path Jianying/CapCut should use for a material inside a draft."""
    if is_windows_path(draft_folder):
        if os.name == 'nt':
            return os.path.join(draft_folder, draft_id, "assets", asset_type, material_name)

        windows_drive, windows_path = re.match(r'([a-zA-Z]:)(.*)', draft_folder).groups()
        parts = [p for p in windows_path.split('\\') if p]
        return "\\".join([windows_drive, *parts, draft_id, "assets", asset_type, material_name])

    return os.path.join(draft_folder, draft_id, "assets", asset_type, material_name)


def zip_draft(draft_id):
    current_dir = os.path.dirname(os.path.abspath(__file__))
    # Compress folder
    zip_dir = os.path.join(current_dir, "tmp/zip")
    os.makedirs(zip_dir, exist_ok=True)
    zip_path = os.path.join(zip_dir, f"{draft_id}.zip")
    shutil.make_archive(os.path.join(zip_dir, draft_id), 'zip', os.path.join(current_dir, draft_id))
    return zip_path

def url_to_hash(url, length=16):
    """
    Convert URL to a fixed-length hash string (without extension), used to name media materials.
    For a local file the hash also covers its size and modification time (capcut-mcp-kit): a file
    replaced at the same path becomes a different material, so clips added before keep the version
    they were made with.
    """
    return hashlib.sha256(_source_key(url).encode('utf-8')).hexdigest()[:length]


def _source_key(url):
    local = os.path.expanduser(str(url))
    if "://" not in str(url) and os.path.isfile(local):
        st = os.stat(local)
        return f"{url}\0{st.st_size}\0{st.st_mtime_ns}"
    return str(url)


def source_changed(material_name, url):
    """True when a material named by url_to_hash no longer matches the file at url (replaced or
    edited after it was added). Names from older versions (path only) and other names are not judged."""
    m = re.match(r"^(?:video|image|audio)_([0-9a-f]{16})\.", str(material_name or ""))
    if not m or not url:
        return False
    plain = hashlib.sha256(str(url).encode('utf-8')).hexdigest()[:16]
    return m.group(1) not in (url_to_hash(url), plain)


def timing_decorator(func_name):
    """Decorator: Used to monitor function execution time"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            start_time = time.time()
            print(f"[{func_name}] Starting execution...")
            try:
                result = func(*args, **kwargs)
                end_time = time.time()
                duration = end_time - start_time
                print(f"[{func_name}] Execution completed, time taken: {duration:.3f} seconds")
                return result
            except Exception as e:
                end_time = time.time()
                duration = end_time - start_time
                print(f"[{func_name}] Execution failed, time taken: {duration:.3f} seconds, error: {e}")
                raise
        return wrapper
    return decorator

def generate_draft_url(draft_id):
    return f"{DRAFT_DOMAIN}{PREVIEW_ROUTER}?draft_id={draft_id}&is_capcut={1 if IS_CAPCUT_ENV else 0}"
