"""Server-owned, per-user workspace directories for sandbox execution."""

import os
import re
import stat
from pathlib import Path

WORKSPACES_DIR = Path(__file__).resolve().parents[2] / "data" / "workspaces"
_USER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


def ensure_workspace(user_id: str) -> str:
    """Create and return a private workspace; never interpret a user ID as a path."""
    if not isinstance(user_id, str) or not _USER_ID.fullmatch(user_id):
        raise ValueError("Invalid user ID for workspace")

    root = WORKSPACES_DIR
    if root.is_symlink():
        raise ValueError("Workspace root must not be a symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not root.is_dir():
        raise ValueError("Workspace root is not a directory")

    path = root / user_id
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    # O_NOFOLLOW rejects an attacker-controlled symlink at the final component.
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise ValueError("Workspace is not a directory")
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)
    return str(path.absolute())
