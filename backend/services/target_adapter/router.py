"""
FastAPI router for Scoped Target Adapter and Approval Gate endpoints.
Exposes:
- POST /api/v1/approval/propose
- POST /api/v1/approvals/decide
- POST /api/v1/adapter/execute
- GET /api/v1/adapter/status/{approval_id}
"""
import logging
from fastapi import APIRouter, HTTPException, Request, status
from typing import Dict, Any

from services.agent_runtime.workspace import ensure_workspace
from services.agent_tools.audit import log_audit_event
from services.auth_gateway.server import authenticate_request, role_for_user

from .adapter import TargetAdapter, get_target_adapter
from .models import (
    ProposalRequest,
    ProposalResponse,
    DecisionRequest,
    DecisionResponse,
    ExecutionRequest,
    ExecutionResponse,
    AdapterStatusResponse,
)

logger = logging.getLogger("target_adapter.router")

router = APIRouter(tags=["Target Adapter & Approval Gate"])


@router.post(
    "/api/v1/approval/propose",
    response_model=ProposalResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Propose a mutating host operation or configuration deployment"
)
async def propose_approval(request: ProposalRequest, http_request: Request):
    user_id, _ = authenticate_request(http_request)
    if request.user_id != user_id:
        raise HTTPException(status_code=403, detail="Requester identity mismatch")
    try:
        workspace = ensure_workspace(user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    request = request.model_copy(update={"workspace": workspace})
    adapter = get_target_adapter()
    try:
        res = adapter.propose(request)
        return res
    except PermissionError as pe:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(pe))
    except (ValueError, FileNotFoundError) as ve:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail="Shared approval store unavailable") from exc
    except Exception as e:
        logger.exception("Unexpected error in propose_approval: %s", e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Proposal failed") from e


@router.post(
    "/api/v1/approvals/decide",
    response_model=DecisionResponse,
    summary="Review and decide an approval request (admin role required)"
)
async def decide_approval(request: DecisionRequest, http_request: Request):
    reviewer, _ = authenticate_request(http_request)
    reviewer_role = role_for_user(reviewer)
    if reviewer_role != "admin":
        raise HTTPException(status_code=403, detail="Authenticated administrator required")
    adapter = get_target_adapter()
    res = adapter.gate.decide(
        approval_id=request.approval_id,
        approved=request.approved,
        reviewer=reviewer,
        reviewer_role=reviewer_role,
        reason=request.reason or "",
    )
    if not res.get("success"):
        err = res.get("error", "Decision failed")
        if "role required" in err or "cannot decide own" in err:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=err)
        if "not found" in err:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=err)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=err)

    appr = res.get("approval", {})
    log_audit_event(
        user_id=reviewer,
        session_id=appr.get("session_id", ""),
        tool_name="approval_decision",
        command=appr.get("command", ""),
        human_approved=request.approved,
        exit_code=0,
        duration_ms=0,
        extra={
            "approval_decision": res.get("status", "approved" if request.approved else "rejected"),
            "approval_id": request.approval_id,
            "requester": appr.get("user_id"),
        },
    )
    return DecisionResponse(
        approval_id=request.approval_id,
        status=res.get("status", "approved" if request.approved else "rejected"),
        decided_by=appr.get("decided_by", reviewer),
        decided_at=appr.get("decided_at", 0.0),
        message=f"Request {request.approval_id} {res.get('status')}",
    )


@router.post(
    "/api/v1/adapter/execute",
    response_model=ExecutionResponse,
    summary="Execute an approved mutation via Scoped Target Adapter"
)
async def execute_adapter(request: ExecutionRequest, http_request: Request):
    user_id, _ = authenticate_request(http_request)
    if request.user_id != user_id:
        raise HTTPException(status_code=403, detail="Executor identity mismatch")
    adapter = get_target_adapter()
    res = adapter.execute(request)
    if res.status == "failed" and "Claim failed" in res.message:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=res.message)
    return res


@router.get(
    "/api/v1/adapter/status/{approval_id}",
    response_model=AdapterStatusResponse,
    summary="Query status of an approval token and execution record"
)
async def get_adapter_status(approval_id: str, http_request: Request):
    user_id, _ = authenticate_request(http_request)
    adapter = get_target_adapter()
    status_res = adapter.get_status(approval_id)
    if not status_res.record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Approval ID {approval_id} not found")
    if role_for_user(user_id) != "admin" and status_res.record.get("user_id") != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Approval ID {approval_id} not found")
    return status_res
