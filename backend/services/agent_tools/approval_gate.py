"""Human approval for sandbox commands.

Only a small set of simple read-only commands runs without approval. An approval
is bound to one user, session, workspace and exact command, and is consumed
atomically before execution. Backed by transactional Valkey store with in-memory fallback.
"""
import os
import re
import shlex
import threading
import time
import uuid
from typing import Any, Dict

from backend.services.approval_gate.filter import (
    DANGEROUS_COMMANDS,
    HARDENED_DANGEROUS_PATTERNS,
    SHELL_SYNTAX,
    READ_ONLY_COMMANDS,
    normalize_command,
    evaluate_command_safety,
)
from backend.services.approval_gate.models import ApprovalRecord
from backend.services.approval_gate.store import (
    ValkeyApprovalStore,
    get_approval_store,
    ApprovalStoreMappingProxy,
)

_store = get_approval_store()
PENDING_APPROVALS: Dict[str, Dict[str, Any]] = ApprovalStoreMappingProxy(_store)
_APPROVAL_LOCK = _store._lock


def create_approval_request(user_id: str, session_id: str, command: str, reason: str, workspace: str = "") -> str:
    approval_id = f"appr-{uuid.uuid4().hex}"
    now = time.time()
    canonical_cmd, tokens = normalize_command(command)
    norm_workspace = os.path.realpath(workspace) if workspace else ""

    record = ApprovalRecord(
        approval_id=approval_id,
        user_id=user_id,
        session_id=session_id,
        command=command,
        normalized_command=canonical_cmd,
        normalized_args=tokens,
        workspace=norm_workspace,
        reason=reason,
        status="pending",
        created_at=now,
        expires_at=now + 300.0,
    )
    _store.create_approval(record)
    return approval_id


def decide_approval(approval_id: str, approved: bool, reviewer: str, reviewer_role: str = "") -> Dict[str, Any]:
    """Only an authenticated administrator other than the requester may decide."""
    return _store.decide_approval(
        approval_id=approval_id,
        approved=approved,
        reviewer=reviewer,
        reviewer_role=reviewer_role,
    )


def consume_approval(approval_id: str, user_id: str, session_id: str, command: str, workspace: str) -> bool:
    """Atomically consume a matching, unexpired approval before dispatch."""
    if not approval_id:
        return False
    norm_workspace = os.path.realpath(workspace) if workspace else ""
    claim_res = _store.claim_for_execution(
        approval_id=approval_id,
        user_id=user_id,
        session_id=session_id,
        workspace=norm_workspace,
        command=command,
    )
    if claim_res.get("success"):
        # For legacy compatibility, mark as consumed
        _store.update_raw(approval_id, {"status": "consumed", "consumed_at": time.time()})
        return True
    return False
