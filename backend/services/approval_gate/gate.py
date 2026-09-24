"""
Approval Gate high-level coordinator.
Provides approval proposal, safety evaluation, decision, claiming, and execution completion.
"""
from typing import Any, Dict, Optional, Tuple, List
import hashlib
import time
import uuid

from .filter import evaluate_command_safety, normalize_command
from .models import ApprovalRecord
from .store import ValkeyApprovalStore, get_approval_store


class ApprovalGate:
    def __init__(self, store: Optional[ValkeyApprovalStore] = None):
        self.store = store or get_approval_store()

    def evaluate_safety(self, command: str) -> Dict[str, Any]:
        return evaluate_command_safety(command)

    def propose(
        self,
        user_id: str,
        target: str,
        action: str,
        command: str = "",
        session_id: str = "",
        workspace: str = "",
        reason: str = "",
        base_hash: Optional[str] = None,
        proposed_content: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Proposes an operation requiring human approval.
        Computes SHA-256 content_hash and binds parameters.
        Returns response dict with approval_id, status, and content_hash.
        """
        # Safety check if command is supplied
        if command:
            safety = self.evaluate_safety(command)
            if safety["action"] == "BLOCKED":
                return {
                    "success": False,
                    "blocked": True,
                    "error": safety["reason"],
                    "code": "BLOCKED"
                }

        approval_id = f"appr-{uuid.uuid4().hex}"
        now = time.time()
        canonical_cmd, tokens = normalize_command(command) if command else ("", [])

        # Compute content hash
        if proposed_content is not None:
            content_hash = hashlib.sha256(proposed_content.encode("utf-8")).hexdigest()
        elif command:
            content_hash = hashlib.sha256(canonical_cmd.encode("utf-8")).hexdigest()
        else:
            content_hash = hashlib.sha256(f"{target}:{action}".encode("utf-8")).hexdigest()

        record = ApprovalRecord(
            approval_id=approval_id,
            status="pending",
            user_id=user_id,
            session_id=session_id,
            workspace=workspace,
            target=target,
            action=action,
            command=command,
            normalized_command=canonical_cmd,
            normalized_args=tokens,
            content_hash=content_hash,
            base_hash=base_hash,
            reason=reason,
            created_at=now,
            expires_at=now + 300.0,
        )

        self.store.create_approval(record)
        return {
            "success": True,
            "approval_id": approval_id,
            "status": "pending",
            "target": target,
            "action": action,
            "content_hash": content_hash,
            "base_hash": base_hash,
            "created_at": now,
            "expires_at": now + 300.0,
            "message": "Mutating operation requires human approval",
        }

    def decide(
        self,
        approval_id: str,
        approved: bool,
        reviewer: str,
        reviewer_role: str,
        reason: str = ""
    ) -> Dict[str, Any]:
        return self.store.decide_approval(
            approval_id=approval_id,
            approved=approved,
            reviewer=reviewer,
            reviewer_role=reviewer_role,
            reason=reason,
        )

    def claim_execution(
        self,
        approval_id: str,
        user_id: str,
        session_id: str = "",
        workspace: str = "",
        command: str = "",
        target: str = "",
        content_hash: str = ""
    ) -> Dict[str, Any]:
        return self.store.claim_for_execution(
            approval_id=approval_id,
            user_id=user_id,
            session_id=session_id,
            workspace=workspace,
            command=command,
            target=target,
            content_hash=content_hash,
        )

    def complete_execution(
        self,
        approval_id: str,
        is_success: bool,
        exit_code: int = 0,
        result_summary: str = ""
    ) -> Dict[str, Any]:
        return self.store.complete_execution(
            approval_id=approval_id,
            is_success=is_success,
            exit_code=exit_code,
            result_summary=result_summary,
        )

    def get_status(self, approval_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get_approval(approval_id)


_GLOBAL_GATE: Optional[ApprovalGate] = None


def get_approval_gate() -> ApprovalGate:
    global _GLOBAL_GATE
    if _GLOBAL_GATE is None:
        _GLOBAL_GATE = ApprovalGate()
    return _GLOBAL_GATE
