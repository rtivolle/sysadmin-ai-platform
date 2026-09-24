"""
TargetAdapter: Central coordinator for scoped host execution, approval claiming, and audit.
"""
import hashlib
import logging
import os
import stat
import time
from pathlib import Path
from typing import Any, Dict, Optional

from backend.services.approval_gate.gate import ApprovalGate, get_approval_gate
from backend.services.approval_gate.models import ApprovalRecord
from backend.services.agent_tools.audit import log_audit_event
from .config import (
    ALLOWED_ACTIONS,
    WHITELISTED_SERVICES,
    validate_target_service,
    validate_target_config_path,
    APPROVAL_TTL_SECONDS,
)
from .models import (
    ProposalRequest,
    ProposalResponse,
    ExecutionRequest,
    ExecutionResponse,
    AdapterStatusResponse,
)
from .service_manager import ServiceManager, get_service_manager
from .config_deployer import ConfigDeployer, get_config_deployer, validate_syntax

logger = logging.getLogger("target_adapter.adapter")


class TargetAdapter:
    def __init__(
        self,
        approval_gate: Optional[ApprovalGate] = None,
        service_manager: Optional[ServiceManager] = None,
        config_deployer: Optional[ConfigDeployer] = None,
    ):
        self.gate = approval_gate or get_approval_gate()
        self.svc_mgr = service_manager or get_service_manager()
        self.deployer = config_deployer or get_config_deployer()

    @staticmethod
    def _read_staged_content(workspace: str, staged_path: str) -> str:
        """Read one regular file directly inside the assigned workspace."""
        root = os.path.realpath(workspace)
        candidate = staged_path if os.path.isabs(staged_path) else os.path.join(root, staged_path)
        if not staged_path or os.path.dirname(os.path.abspath(candidate)) != root:
            raise PermissionError("Staged file must be directly inside the assigned workspace")
        fd = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise PermissionError("Staged file must be a regular file")
            if metadata.st_size > 1024 * 1024:
                raise ValueError("Staged configuration exceeds 1 MiB")
            with os.fdopen(fd, "r", encoding="utf-8") as file:
                fd = -1
                content = file.read(1024 * 1024 + 1)
                if len(content.encode("utf-8")) > 1024 * 1024:
                    raise ValueError("Staged configuration exceeds 1 MiB")
                return content
        finally:
            if fd >= 0:
                os.close(fd)

    def propose(self, req: ProposalRequest) -> ProposalResponse:
        """Validates parameters, calculates hashes, and registers approval token."""
        if req.action not in ALLOWED_ACTIONS:
            raise PermissionError(f"Action '{req.action}' is not in allowed actions: {sorted(list(ALLOWED_ACTIONS))}")

        now = time.time()
        base_hash = None
        content_hash = ""
        staged_content = None

        if req.action in ("service_restart", "service_reload", "service_status"):
            canonical_service = validate_target_service(req.target)
            cmd = f"systemctl {req.action.split('_')[1]} {canonical_service}.service"
            content_hash = hashlib.sha256(cmd.encode("utf-8")).hexdigest()
            prop_res = self.gate.propose(
                user_id=req.user_id,
                target=canonical_service,
                action=req.action,
                command=cmd,
                session_id=req.session_id,
                workspace=req.workspace or "",
                reason=req.reason,
            )
        elif req.action == "config_deploy":
            canonical_target = validate_target_config_path(req.target)
            
            # Resolve staged file in workspace
            workspace_dir = req.workspace or os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                "data", "workspaces", req.user_id
            )
            staged_file = req.staged_path or os.path.basename(canonical_target)
            staged_content = self._read_staged_content(workspace_dir, staged_file)

            # Pre-validate syntax
            valid_syntax, syn_err = validate_syntax(staged_content, os.path.basename(canonical_target))
            if not valid_syntax:
                raise ValueError(f"Pre-proposal syntax validation failed: {syn_err}")

            content_hash = hashlib.sha256(staged_content.encode("utf-8")).hexdigest()

            # Calculate base hash if destination exists
            if os.path.exists(canonical_target):
                with open(canonical_target, "r", encoding="utf-8", errors="replace") as f:
                    base_content = f.read()
                base_hash = hashlib.sha256(base_content.encode("utf-8")).hexdigest()

            prop_res = self.gate.propose(
                user_id=req.user_id,
                target=canonical_target,
                action=req.action,
                command=f"deploy {canonical_target}",
                session_id=req.session_id,
                workspace=workspace_dir,
                reason=req.reason,
                base_hash=base_hash,
                proposed_content=staged_content,
            )
        else:
            raise ValueError(f"Unsupported action: {req.action}")

        if not prop_res.get("success"):
            raise ValueError(f"Proposal failed: {prop_res.get('error')}")

        return ProposalResponse(
            approval_id=prop_res["approval_id"],
            status=prop_res["status"],
            action=req.action,
            target=prop_res["target"],
            content_hash=prop_res["content_hash"],
            base_hash=prop_res.get("base_hash"),
            created_at=prop_res["created_at"],
            expires_at=prop_res["expires_at"],
            message=prop_res["message"],
        )

    def execute(self, req: ExecutionRequest) -> ExecutionResponse:
        """
        Executes an approved mutation.
        Performs atomic CAS claiming, dispatches to ServiceManager or ConfigDeployer,
        and logs to VictoriaLogs audit stream.
        """
        start_time = time.time()
        record = self.gate.get_status(req.approval_id)
        if not record:
            return ExecutionResponse(
                approval_id=req.approval_id,
                status="failed",
                exit_code=1,
                message=f"Approval token '{req.approval_id}' not found",
            )

        # Atomic claim transition: approved -> executing
        claim = self.gate.claim_execution(
            approval_id=req.approval_id,
            user_id=req.user_id,
            session_id=req.session_id or "",
            command=req.command or record.get("command", ""),
            target=req.target or record.get("target", ""),
            content_hash=req.content_hash or record.get("content_hash", ""),
        )

        if not claim.get("success"):
            return ExecutionResponse(
                approval_id=req.approval_id,
                status="failed",
                exit_code=1,
                message=f"Claim failed: {claim.get('error', claim.get('code'))}",
            )

        action = record.get("action")
        target = record.get("target")

        # Execute Service Action
        if action in ("service_restart", "service_reload", "service_status"):
            exit_code, stdout, stderr = self.svc_mgr.execute_action(action, target)
            is_success = (exit_code == 0)
            self.gate.complete_execution(
                approval_id=req.approval_id,
                is_success=is_success,
                exit_code=exit_code,
                result_summary=stdout[:200] if is_success else stderr[:200],
            )
            duration_ms = int((time.time() - start_time) * 1000)

            # Audit event
            log_audit_event(
                user_id=req.user_id,
                session_id=req.session_id or "",
                tool_name=f"adapter_{action}",
                action=action,
                parameters={"target": target, "action": action},
                command=record.get("command", ""),
                human_approved=True,
                approval_id=req.approval_id,
                exit_code=exit_code,
                duration_ms=duration_ms,
            )

            return ExecutionResponse(
                approval_id=req.approval_id,
                status="succeeded" if is_success else "failed",
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                message=f"Service {action} completed with exit code {exit_code}",
                duration_ms=duration_ms,
            )

        # Execute Staged Configuration Deployment
        elif action == "config_deploy":
            workspace_dir = record.get("workspace") or os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                "data", "workspaces", req.user_id
            )
            staged_file = req.staged_path or os.path.basename(target)
            try:
                staged_content = self._read_staged_content(workspace_dir, staged_file)
            except (OSError, ValueError, PermissionError) as exc:
                self.gate.complete_execution(req.approval_id, is_success=False, exit_code=1, result_summary="Staged file unavailable")
                return ExecutionResponse(
                    approval_id=req.approval_id,
                    status="failed",
                    exit_code=1,
                    message=f"Staged file unavailable at execution time: {exc}",
                )

            deploy_res = self.deployer.deploy(
                approval_id=req.approval_id,
                user_id=req.user_id,
                target_path=target,
                staged_content=staged_content,
                proposed_hash=record.get("content_hash"),
                base_hash=record.get("base_hash"),
            )

            is_success = deploy_res.get("success", False)
            exit_code = 0 if is_success else 1
            duration_ms = int((time.time() - start_time) * 1000)

            self.gate.complete_execution(
                approval_id=req.approval_id,
                is_success=is_success,
                exit_code=exit_code,
                result_summary=deploy_res.get("message", "")[:200],
            )

            # Audit event
            log_audit_event(
                user_id=req.user_id,
                session_id=req.session_id or "",
                tool_name="adapter_config_deploy",
                action="config_deploy",
                parameters={
                    "target": target,
                    "backup_path": deploy_res.get("backup_path"),
                    "rollback_performed": deploy_res.get("rollback_performed"),
                },
                command=f"deploy {target}",
                human_approved=True,
                approval_id=req.approval_id,
                exit_code=exit_code,
                duration_ms=duration_ms,
            )

            return ExecutionResponse(
                approval_id=req.approval_id,
                status="succeeded" if is_success else "failed",
                exit_code=exit_code,
                message=deploy_res.get("message", ""),
                backup_path=deploy_res.get("backup_path"),
                rollback_performed=deploy_res.get("rollback_performed", False),
                duration_ms=duration_ms,
                details=deploy_res,
            )

        else:
            return ExecutionResponse(
                approval_id=req.approval_id,
                status="failed",
                exit_code=1,
                message=f"Unknown action: {action}",
            )

    def get_status(self, approval_id: str) -> AdapterStatusResponse:
        record = self.gate.get_status(approval_id)
        if not record:
            return AdapterStatusResponse(
                approval_id=approval_id,
                status="failed",
                record={},
            )
        return AdapterStatusResponse(
            approval_id=approval_id,
            status=record.get("status", "pending"),
            record=record,
        )


_GLOBAL_ADAPTER: Optional[TargetAdapter] = None


def get_target_adapter() -> TargetAdapter:
    global _GLOBAL_ADAPTER
    if _GLOBAL_ADAPTER is None:
        _GLOBAL_ADAPTER = TargetAdapter()
    return _GLOBAL_ADAPTER
