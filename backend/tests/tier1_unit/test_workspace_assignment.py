"""Workspace paths are chosen and secured by the server."""

import os
import stat
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from backend.services.agent_runtime import workspace as workspace_module
from backend.services.agent_runtime.models import AgentChatRequest, SessionState
from backend.services.agent_runtime.session_store import SessionStore


def test_request_cannot_supply_workspace():
    with pytest.raises(ValidationError):
        AgentChatRequest(prompt="hello", workspace="/etc")


def test_workspace_is_private_and_user_id_cannot_escape(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_module, "WORKSPACES_DIR", tmp_path / "workspaces")
    path = workspace_module.ensure_workspace("sysadmin-01")
    assert path == str(tmp_path / "workspaces" / "sysadmin-01")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o700

    os.chmod(path, 0o777)
    assert workspace_module.ensure_workspace("sysadmin-01") == path
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o700
    for user_id in ("../other", "/etc", "a/b", "a:b", ""):
        with pytest.raises(ValueError):
            workspace_module.ensure_workspace(user_id)


def test_workspace_symlink_is_rejected(tmp_path, monkeypatch):
    root = tmp_path / "workspaces"
    root.mkdir()
    (root / "sysadmin-01").symlink_to(tmp_path, target_is_directory=True)
    monkeypatch.setattr(workspace_module, "WORKSPACES_DIR", root)
    with pytest.raises(OSError):
        workspace_module.ensure_workspace("sysadmin-01")


@pytest.mark.asyncio
async def test_existing_client_selected_path_is_replaced(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_module, "WORKSPACES_DIR", tmp_path / "workspaces")
    store = SessionStore()
    old = SessionState(session_id="old", user_id="sysadmin-01", workspace="/etc")
    store.get_session = AsyncMock(return_value=old)
    store.save_session = AsyncMock()
    session = await store.create_or_get_session("sysadmin-01", "old")
    assert session.workspace == str(tmp_path / "workspaces" / "sysadmin-01")
    store.save_session.assert_awaited_once_with(session)
