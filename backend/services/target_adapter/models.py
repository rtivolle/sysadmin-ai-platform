"""
Pydantic v2 schemas for Target Adapter requests, state transitions, and execution results.
"""
from typing import Optional, Dict, Any, Literal, List
from pydantic import BaseModel, Field

from backend.services.approval_gate.models import ApprovalStatus


class ProposalRequest(BaseModel):
    user_id: str
    session_id: str = ""
    action: Literal["service_restart", "service_reload", "service_status", "config_deploy"]
    target: str
    staged_path: Optional[str] = None
    reason: str = "Target mutation request"
    command: Optional[str] = None
    base_hash: Optional[str] = None
    proposed_hash: Optional[str] = None
    workspace: Optional[str] = ""


class ProposalResponse(BaseModel):
    approval_id: str
    status: ApprovalStatus
    action: str
    target: str
    content_hash: str
    base_hash: Optional[str] = None
    created_at: float
    expires_at: float
    message: str


class DecisionRequest(BaseModel):
    approval_id: str
    approved: bool
    reviewer: str
    reviewer_role: str = "admin"
    reason: Optional[str] = ""


class DecisionResponse(BaseModel):
    approval_id: str
    status: ApprovalStatus
    decided_by: str
    decided_at: float
    message: str


class ExecutionRequest(BaseModel):
    approval_id: str
    user_id: str
    session_id: Optional[str] = ""
    action: Optional[str] = None
    target: Optional[str] = None
    command: Optional[str] = None
    content_hash: Optional[str] = None
    staged_path: Optional[str] = None


class ExecutionResponse(BaseModel):
    approval_id: str
    status: ApprovalStatus
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    message: str
    duration_ms: int = 0
    backup_path: Optional[str] = None
    rollback_performed: bool = False
    details: Dict[str, Any] = Field(default_factory=dict)


class AdapterStatusResponse(BaseModel):
    approval_id: str
    status: ApprovalStatus
    record: Dict[str, Any]
