"""Approval IDs authorize one exact operation in one assigned workspace."""
import time

from backend.services.agent_tools.approval_gate import (
    PENDING_APPROVALS,
    consume_approval,
    create_approval_request,
    decide_approval,
)
from services.agent_runtime import tool_registry
from services.agent_runtime.workspace import ensure_workspace


def test_approval_binding_and_single_use(tmp_path):
    workspace = str(tmp_path / "workspace")
    command = "touch /tmp/example"
    approval_id = create_approval_request("user-a", "session-a", command, "write", workspace)
    assert decide_approval(approval_id, True, "admin", "admin")["success"]
    assert not consume_approval(approval_id, "user-b", "session-a", command, workspace)
    assert not consume_approval(approval_id, "user-a", "session-b", command, workspace)
    assert not consume_approval(approval_id, "user-a", "session-a", "touch /tmp/other", workspace)
    assert not consume_approval(approval_id, "user-a", "session-a", command, str(tmp_path / "other"))
    assert consume_approval(approval_id, "user-a", "session-a", command, workspace)
    assert PENDING_APPROVALS[approval_id]["status"] == "consumed"
    assert not consume_approval(approval_id, "user-a", "session-a", command, workspace)


def test_approval_expiry_and_reviewer_role(tmp_path):
    approval_id = create_approval_request("user-a", "session-a", "touch file", "write", str(tmp_path))
    assert not decide_approval(approval_id, True, "user-a", "admin")["success"]
    assert not decide_approval(approval_id, True, "user-b", "sysadmin")["success"]
    assert decide_approval(approval_id, True, "admin", "admin")["success"]
    PENDING_APPROVALS[approval_id]["expires_at"] = time.time() - 1
    assert not consume_approval(approval_id, "user-a", "session-a", "touch file", str(tmp_path))
    assert PENDING_APPROVALS[approval_id]["status"] == "expired"


def test_runtime_dispatch_checks_workspace_and_consumes(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(tool_registry, "execute_sandboxed_command", lambda workspace, command: (calls.append((workspace, command)) or (0, "", "")))
    monkeypatch.setattr(tool_registry, "log_audit_event", lambda *args, **kwargs: None)
    assigned = ensure_workspace("sysadmin-01")
    args = {"command": "touch /tmp/runtime-test"}
    mismatched = tool_registry.execute_tool_call("sandboxed_bash", args, "sysadmin-01", "sess-runtime", str(tmp_path))
    assert mismatched["status"] == "error"
    requested = tool_registry.execute_tool_call("sandboxed_bash", args, "sysadmin-01", "sess-runtime", assigned)
    assert requested["status"] == "approval_required"
    approval_id = requested["approval_id"]
    from services.agent_tools.approval_gate import decide_approval as decide_runtime_approval
    assert decide_runtime_approval(approval_id, True, "sysadmin-admin", "admin")["success"]
    approved_args = {**args, "approval_id": approval_id}
    assert tool_registry.execute_tool_call("sandboxed_bash", approved_args, "sysadmin-01", "sess-runtime", assigned)["status"] == "success"
    assert tool_registry.execute_tool_call("sandboxed_bash", approved_args, "sysadmin-01", "sess-runtime", assigned)["status"] == "blocked"
    assert calls == [(assigned, args["command"])]
