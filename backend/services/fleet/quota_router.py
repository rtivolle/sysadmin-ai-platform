"""Admin quota-scope API (platform side).

- ``PUT /api/v1/quotas/{scope_type}/{scope_id}`` — create/replace a team or
  project budget (also ``user``, for symmetry with the per-user Valkey quotas).
- ``GET /api/v1/quotas/{scope_type}/{scope_id}`` — budget plus today's usage.
- ``DELETE /api/v1/quotas/{scope_type}/{scope_id}`` — remove a budget.
- ``GET /api/v1/quotas/{scope_type}/{scope_id}/usage?day=`` — usage summary.
- ``GET /api/v1/quotas/chargeback?day=&scope_type=`` — admin chargeback view.
- ``POST /api/v1/quotas/check`` — scoped admission check: 200 when admitted,
  **429 with a ``Retry-After`` header** when the budget is exhausted, never a
  500. A store outage answers 503 (fail-closed).

Auth copies the ``fleet`` router pattern: the ``admin`` role derived from
credentials (never from a client header). The scope store is opened lazily on
first request — unlike the fleet router's import-time open — so importing
this module never performs store I/O; every endpoint still fails closed with
503 when no durable store is configured.

NOTE: this router is not mounted yet. The mount in
``services/agent_tools/server.py`` happens at integration time (out of scope
for this change); see ``docs/quota-aware-distribution.md``.
"""
from typing import Any, Dict, Optional
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from services.auth_gateway.server import authenticate_request, role_for_user
from services.control_store.quota_scopes import (
    QuotaScopes,
    open_quota_scopes,
    validate_scope_id,
    validate_scope_type,
)
from services.agent_tools.audit import log_audit_event
from services.logging_setup import get_logger

router = APIRouter()
_QUOTA_LOG = get_logger("fleet.quota_router")

# Set by _scopes_or_503() on first request; overridden by tests with a
# fake-backed store. Never opened at import time.
quota_scopes: Optional[QuotaScopes] = None


def require_admin(request: Request) -> str:
    user_id, _ = authenticate_request(request)
    if role_for_user(user_id) != "admin":
        raise HTTPException(status_code=403, detail="Administrator required")
    return user_id


def _scopes_or_503() -> QuotaScopes:
    global quota_scopes
    if quota_scopes is None:
        try:
            quota_scopes = open_quota_scopes()
        except Exception as exc:
            # Misconfigured or unreachable store: fail closed, never 500.
            raise HTTPException(
                status_code=503, detail="Quota scope store unavailable"
            ) from exc
    if quota_scopes is None:
        raise HTTPException(
            status_code=503,
            detail="Quota scope store unavailable (no durable store configured)",
        )
    return quota_scopes


def _audit(reviewer: str, action: str, parameters: Dict[str, Any],
           exit_code: int = 0) -> None:
    try:
        log_audit_event(
            user_id=reviewer, session_id="", tool_name=action, action=action,
            parameters=parameters, exit_code=exit_code, duration_ms=0,
        )
    except Exception:
        pass  # audit is best-effort; it must never change the quota outcome


def _checked_scope(scope_type: str, scope_id: str):
    try:
        validate_scope_type(scope_type)
        validate_scope_id(scope_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return scope_type, scope_id


def _store_or_503(scopes: QuotaScopes, fn, *args):
    """Run a QuotaScopes call; store outages become 503, bad input 400."""
    try:
        return fn(*args)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ConnectionError as exc:
        raise HTTPException(
            status_code=503, detail="Quota scope store unavailable"
        ) from exc


class SetLimitsBody(BaseModel):
    limits: Dict[str, int] = Field(..., min_length=1)


class CheckBody(BaseModel):
    scope_type: str
    scope_id: str
    tokens: int = Field(default=0, ge=0)


@router.put("/api/v1/quotas/{scope_type}/{scope_id}")
def set_scope_limits(scope_type: str, scope_id: str, body: SetLimitsBody,
                     request: Request):
    reviewer = require_admin(request)
    scope_type, scope_id = _checked_scope(scope_type, scope_id)
    scopes = _scopes_or_503()
    stored = _store_or_503(scopes, scopes.set_limits, scope_type, scope_id,
                           body.limits)
    _audit(reviewer, "quota_scope_set_limits",
           {"scope_type": scope_type, "scope_id": scope_id, "limits": stored})
    return {"scope_type": scope_type, "scope_id": scope_id, "limits": stored}


@router.get("/api/v1/quotas/{scope_type}/{scope_id}")
def get_scope(scope_type: str, scope_id: str, request: Request):
    require_admin(request)
    scope_type, scope_id = _checked_scope(scope_type, scope_id)
    scopes = _scopes_or_503()
    limits = _store_or_503(scopes, scopes.get_limits, scope_type, scope_id)
    if limits is None:
        raise HTTPException(status_code=404, detail="Unknown quota scope")
    usage = _store_or_503(scopes, scopes.usage_summary, scope_type, scope_id)
    return {
        "scope_type": scope_type, "scope_id": scope_id,
        "limits": limits, "usage": usage,
    }


@router.delete("/api/v1/quotas/{scope_type}/{scope_id}")
def delete_scope_limits(scope_type: str, scope_id: str, request: Request):
    reviewer = require_admin(request)
    scope_type, scope_id = _checked_scope(scope_type, scope_id)
    scopes = _scopes_or_503()
    deleted = _store_or_503(scopes, scopes.delete_limits, scope_type, scope_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Unknown quota scope")
    _audit(reviewer, "quota_scope_delete_limits",
           {"scope_type": scope_type, "scope_id": scope_id})
    return {"scope_type": scope_type, "scope_id": scope_id, "deleted": True}


@router.get("/api/v1/quotas/{scope_type}/{scope_id}/usage")
def get_scope_usage(scope_type: str, scope_id: str, request: Request,
                    day: Optional[str] = None):
    require_admin(request)
    scope_type, scope_id = _checked_scope(scope_type, scope_id)
    scopes = _scopes_or_503()
    try:
        return _store_or_503(scopes, scopes.usage_summary, scope_type, scope_id,
                             day)
    except HTTPException:
        raise
    except ValueError as exc:  # bad day format from usage_summary
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/v1/quotas/chargeback")
def get_chargeback(request: Request, day: Optional[str] = None):
    require_admin(request)
    scopes = _scopes_or_503()
    try:
        return _store_or_503(scopes, scopes.chargeback, day)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/v1/quotas/check")
def check_scope_budget(body: CheckBody, request: Request):
    require_admin(request)
    try:
        validate_scope_type(body.scope_type)
        validate_scope_id(body.scope_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    scopes = _scopes_or_503()
    try:
        admitted, info = scopes.check_budget(
            body.scope_type, body.scope_id, body.tokens)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ConnectionError as exc:
        raise HTTPException(
            status_code=503, detail="Quota scope store unavailable"
        ) from exc
    payload = {"admitted": admitted, **info}
    if admitted:
        return payload
    # Exhausted: 429 with Retry-After, never a 500. The caller (or the
    # end user) retries after the budget window resets.
    retry_after = info.get("reset_in_seconds") or 60
    return JSONResponse(
        status_code=429,
        content=payload,
        headers={"Retry-After": str(int(retry_after))},
    )
