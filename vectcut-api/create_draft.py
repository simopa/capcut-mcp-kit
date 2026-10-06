# Modified in capcut-mcp-kit (2026) from VectCutAPI @ cfa4779: the requested fps is applied (it was ignored); drafts are persisted; get_or_create_draft no longer creates.
# See NOTICE at the repository root.
import uuid
import pyJianYingDraft as draft
import time
from draft_cache import DRAFT_CACHE, update_cache
from draft_store import get_draft, store_new

# Frame rates CapCut offers for a project
SUPPORTED_FPS = (24, 25, 30, 50, 60)

def create_draft(width=1080, height=1920, fps=30):
    """
    Create new CapCut draft
    :param width: Video width, default 1080
    :param height: Video height, default 1920
    :return: (draft_name, draft_path, draft_id, draft_url)
    """
    # Generate timestamp and draft_id
    unix_time = int(time.time())
    unique_id = uuid.uuid4().hex[:8]  # Take the first 8 digits of UUID
    draft_id = f"dfd_cat_{unix_time}_{unique_id}"  # Use Unix timestamp and UUID combination
    
    if fps not in SUPPORTED_FPS:
        raise ValueError(f"Unsupported fps {fps}: use one of {', '.join(map(str, SUPPORTED_FPS))}")
    # Create CapCut draft with specified resolution
    script = draft.Script_file(width, height, fps)
    
    # Store on disk and in the cache
    store_new(draft_id, script)
    
    return script, draft_id

def get_or_create_draft(draft_id=None, width=1080, height=1920):
    """
    Get an existing CapCut draft (kept under this name for the upstream call sites).
    It no longer creates one: a missing or unknown draft_id raises draft_store.DraftNotFound, so a
    stale ID cannot silently start an empty project. width/height are ignored.
    :return: (draft_id, script)
    """
    return draft_id, get_draft(draft_id)
