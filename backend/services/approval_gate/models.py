"""
Pydantic and typing models for Approval Gate state machine.
"""
from typing import Optional, List, Dict, Any, Literal
from pydantic import BaseModel, Field

ApprovalStatus = Literal[
    "proposed", "pending", "approved", "rejected", "expired",
    "executing", "succeeded", "failed", "consumed"
]


class ApprovalRecord(BaseModel):
    approval_id: str
    status: ApprovalStatus = "pending"
    user_id: str
    session_id: str = ""
    workspace: str = ""
    target: str = ""
    action: str = ""
    command: str = ""
    normalized_command: str = ""
    normalized_args: List[str] = Field(default_factory=list)
    content_hash: str = ""
    base_hash: Optional[str] = None
    reason: str = ""
    created_at: float = 0.0
    expires_at: float = 0.0
    decided_by: Optional[str] = None
    decided_at: Optional[float] = None
    decision_reason: Optional[str] = None
    executed_by: Optional[str] = None
    executed_at: Optional[float] = None
    completed_at: Optional[float] = None
    exit_code: Optional[int] = None
    result_summary: Optional[str] = None
    extra: Dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = self.model_dump()
        return data
