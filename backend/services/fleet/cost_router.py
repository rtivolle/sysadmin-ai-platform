"""Fleet cost HTTP API (platform side).

- GET /api/v1/fleet/costs/summary?scope_type=&scope_id=&day= — attribute the
  day's GPU cost to one model, team or node (chargeback).

Auth copies the `fleet/router.py` pattern: the `admin` role derived from
credentials (never from a client header). The tracker is fail-closed: with no
store configured, the endpoint answers 503.

The module-level `cost_tracker` is overridden by tests with a fake-backed
tracker; production wiring (not done here — integration mounts this router in
`agent_tools/server.py`) sets it via `configure()`.
"""
import datetime
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request

from services.auth_gateway.server import authenticate_request, role_for_user
from services.fleet.cost_tracker import CostTracker
from services.logging_setup import get_logger

router = APIRouter()
_COST_LOG = get_logger("fleet.cost_router")

# Overridden by tests with a fake-backed tracker; set by integration wiring.
cost_tracker: Optional[CostTracker] = None


def configure(tracker: CostTracker) -> None:
    """Attach the production tracker. Called once by the integration wiring."""
    global cost_tracker
    cost_tracker = tracker


def require_admin(request: Request) -> str:
    user_id, _ = authenticate_request(request)
    if role_for_user(user_id) != "admin":
        raise HTTPException(status_code=403, detail="Administrator required")
    return user_id


def _tracker_or_503() -> CostTracker:
    if cost_tracker is None:
        raise HTTPException(
            status_code=503,
            detail="Cost tracker unavailable (no durable store configured)",
        )
    return cost_tracker


def _parse_day(value: Optional[str]) -> datetime.date:
    if not value:
        return datetime.date.today()
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail="day must be YYYY-MM-DD")


@router.get("/api/v1/fleet/costs/summary")
def cost_summary(
    request: Request,
    scope_type: str,
    scope_id: str,
    day: Optional[str] = None,
) -> Dict[str, Any]:
    require_admin(request)
    tracker = _tracker_or_503()
    try:
        return tracker.cost_summary(scope_type, scope_id, _parse_day(day))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
