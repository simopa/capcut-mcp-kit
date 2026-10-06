# Modified in capcut-mcp-kit (2026) from VectCutAPI @ cfa4779: Windows asset paths built off Windows no longer double the drive separator; material names of local files depend on the file's content.
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
    For a local file the hash also covers a digest of its content (capcut-mcp-kit): a file
    replaced or edited at the same path becomes a different material, so clips added before keep
    the version they were made with.
    """
    return hashlib.sha256(_source_key(url).encode('utf-8')).hexdigest()[:length]


_digests = {}  # (dev, inode, size, mtime_ns, ctime_ns) -> sha256 of the content


def content_digest(path):
    """sha256 of a file's content. The stat fields only spare reading again, within this process,
    a file nothing has touched since (ctime changes on every write and cannot be set back by a
    program); they never stand for the content. The key is the file, not its path: a file in a
    folder that was renamed (staging folder put in place, previous version moved aside) is not
    read again."""
    path = os.path.realpath(os.path.expanduser(str(path)))
    st = os.stat(path)
    key = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    if key not in _digests:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        _digests[key] = h.hexdigest()
    return _digests[key]


def _source_key(url, digest=None):
    local = os.path.expanduser(str(url))
    if "://" not in str(url) and os.path.isfile(local):
        return f"{url}\0sha256:{digest or content_digest(local)}"
    return str(url)


def _versioned(material_name):
    m = re.match(r"^(?:video|image|audio)_([0-9a-f]{16})\.", str(material_name or ""))
    return m.group(1) if m else None


def source_changed(material_name, url, copy=None):
    """True when a material named by url_to_hash is not the version of the file at url it was
    added with: judged on `copy` (the file actually going into the project) when given, else on
    the file at url. Names of kit 0.4.0 (size and modification time) are judged by those, names
    of older versions (path only) and other names are not judged."""
    name_hash = _versioned(material_name)
    if not name_hash or not url:
        return False
    local = os.path.expanduser(str(url))
    if "://" in str(url) or not os.path.isfile(copy or local):
        return False
    plain = hashlib.sha256(str(url).encode('utf-8')).hexdigest()[:16]
    if name_hash == plain:
        return False
    current = hashlib.sha256(_source_key(url, content_digest(copy or local)).encode('utf-8')).hexdigest()[:16]
    if name_hash == current:
        return False
    if os.path.isfile(local):  # kit 0.4.0
        st = os.stat(local)
        legacy = f"{url}\0{st.st_size}\0{st.st_mtime_ns}"
        return name_hash != hashlib.sha256(legacy.encode('utf-8')).hexdigest()[:16]
    return True


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
