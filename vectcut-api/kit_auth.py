# Added in capcut-mcp-kit (2026): shared token between the MCP server and the backend, and the
# kit's identity for the health check. See NOTICE at the repository root.
"""
Every request to the backend (except GET /health) must carry the header X-CapCut-Kit-Token with
the token stored in <state dir>/token (created on first use, readable only by the user). The MCP
server reads the same file. CAPCUT_MCP_TOKEN overrides the file (tests, custom setups).
"""

import hmac
import os
import secrets

from draft_store import private_dir, state_dir

KIT_SERVICE = "capcut-mcp-kit"
KIT_VERSION = "0.4.0"
KIT_API = 1
TOKEN_HEADER = "X-CapCut-Kit-Token"


def token_path() -> str:
    return os.path.join(state_dir(), "token")


def token() -> str:
    env = os.environ.get("CAPCUT_MCP_TOKEN")
    if env:
        return env
    path = token_path()
    if not os.path.exists(path):
        private_dir(os.path.dirname(path))
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_urlsafe(32))
        except FileExistsError:
            pass  # another process created it first
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


def authorized(header_value) -> bool:
    return bool(header_value) and hmac.compare_digest(str(header_value), token())
