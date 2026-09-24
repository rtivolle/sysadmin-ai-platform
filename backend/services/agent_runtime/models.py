import os
import time
from typing import List, Dict, Any, Optional, Literal
from pydantic import BaseModel, Field, ConfigDict, field_validator

class CitationRecord(BaseModel):
    source: str = Field(..., description="Source path or log target")
    section_or_query: Optional[str] = Field(None, description="Section heading or search query")
    start_line: Optional[int] = Field(None, description="Starting line number")
    end_line: Optional[int] = Field(None, description="Ending line number")
    artifact_hash: Optional[str] = Field(None, description="Hash of the referenced file or diff")

class AgentChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(..., description="User query or sysadmin instruction")
    session_id: Optional[str] = Field(None, description="Conversational session ID")
    model: Optional[str] = Field("fast-model", description="Model alias (fast-model or heavy-model)")
    max_steps: Optional[int] = Field(5, description="Maximum ReAct reasoning steps")
    stream: bool = Field(False, description="Whether to stream response via SSE")
    workspace: Optional[str] = Field(None, description="User workspace path")
    request_id: Optional[str] = Field(None, description="Client-provided request correlation ID")

    @field_validator("workspace", mode="before")
    @classmethod
    def validate_workspace_path(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            norm = os.path.normpath(str(v))
            if not ("workspaces" in norm or norm.startswith("/workspace")):
                raise ValueError(f"Client cannot supply arbitrary workspace path outside workspaces directory: {v}")
        return v

class ToolExecutionRecord(BaseModel):
    tool: str
    status: Literal["success", "error", "approval_required", "blocked"]
    parameters: Optional[Dict[str, Any]] = None
    duration_ms: Optional[int] = 0
    approval_id: Optional[str] = None
    command: Optional[str] = None
    exit_code: Optional[int] = None
    error: Optional[str] = None

class AgentChatResponse(BaseModel):
    session_id: str
    response: str
    tools_executed: List[ToolExecutionRecord] = Field(default_factory=list)
    approval_required: bool = False
    approval_id: Optional[str] = None
    command: Optional[str] = None
    user_id: Optional[str] = None
    turn_count: int = 1
    citations: Optional[List[CitationRecord]] = Field(default_factory=list)

class AgentCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: Optional[str] = Field(None, description="Specific in-flight request ID to cancel")
    session_id: Optional[str] = Field(None, description="Session ID whose active execution to cancel")

class AgentCancelResponse(BaseModel):
    success: bool
    message: str
    cancelled_request_id: Optional[str] = None

class SessionMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str
    thought_steps: Optional[List[Dict[str, Any]]] = None
    tools_executed: Optional[List[Dict[str, Any]]] = None
    timestamp: float = Field(default_factory=time.time)

class SessionState(BaseModel):
    session_id: str
    user_id: str
    workspace: str
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    turn_count: int = 0
    messages: List[SessionMessage] = Field(default_factory=list)
    context_metadata: Dict[str, Any] = Field(default_factory=dict)
