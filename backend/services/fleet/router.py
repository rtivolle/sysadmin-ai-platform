"""Admin Fleet API (platform side, mounted in `agent_tools`).

- POST /api/v1/fleet/nodes/{name}/approve — human approval gate: a `pending`
  node joins the fleet only when an administrator says so.
- POST /api/v1/fleet/nodes/{name}/drain — graceful removal: call the
  node-agent over mTLS, then mark the node `drained` in the registry. The
  `drained` registry status means "no new placements" — the node stops
  accepting new models the moment it accepts the drain, then finishes
  stopping running models and confirms via subsequent heartbeats.
- POST /api/v1/fleet/nodes/{name}/decommission — retire a node permanently.
- GET /api/v1/fleet/nodes — list the fleet.
- GET /api/v1/fleet/health — summary: counts by status, VRAM totals.

Auth copies the `model_manager` router pattern: the `admin` role derived from
credentials (never from a client header). The registry is fail-closed: with
no durable store configured, every endpoint answers 503.
"""
import logging
import os
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
import httpx

from services.agent_tools.audit import log_audit_event
from services.auth_gateway.server import authenticate_request, role_for_user
from services.control_store import open_fleet_registry
from services.control_store.fleet_registry import FleetRegistry, validate_node_name
from services.logging_setup import get_logger, log_event

router = APIRouter()
_FLEET_LOG = get_logger("fleet.router")

# Overridden by tests with a fake-backed registry.
fleet_registry: Optional[FleetRegistry] = open_fleet_registry()

NODE_AGENT_PORT = int(os.getenv("FLEET_NODE_PORT", "8001"))


def _node_http_client() -> httpx.Client:
    """mTLS client for node-agent calls. All three files are required."""
    ca = os.getenv("FLEET_NODE_CA")
    cert = os.getenv("FLEET_PLATFORM_CERT")
    key = os.getenv("FLEET_PLATFORM_KEY")
    missing = [name for name, value in
               (("FLEET_NODE_CA", ca), ("FLEET_PLATFORM_CERT", cert), ("FLEET_PLATFORM_KEY", key))
               if not value]
    if missing:
        raise RuntimeError(f"fleet mTLS not configured (missing: {', '.join(missing)})")
    return httpx.Client(verify=ca, cert=(cert, key), timeout=30.0)


# Replaceable for tests.
node_http_client: Callable[[], httpx.Client] = _node_http_client


def require_admin(request: Request) -> str:
    user_id, _ = authenticate_request(request)
    if role_for_user(user_id) != "admin":
        raise HTTPException(status_code=403, detail="Administrator required")
    return user_id


def _registry_or_503() -> FleetRegistry:
    if fleet_registry is None:
        raise HTTPException(status_code=503, detail="Fleet registry unavailable (no durable store configured)")
    return fleet_registry


def _audit(reviewer: str, action: str, parameters: Dict[str, Any], exit_code: int = 0,
           extra: Optional[dict] = None) -> None:
    try:
        log_audit_event(
            user_id=reviewer, session_id="", tool_name=action, action=action,
            parameters=parameters, exit_code=exit_code, duration_ms=0, extra=extra,
        )
    except Exception:
        pass  # audit is best-effort; it must never change the fleet outcome


def _node_or_404(name: str) -> Dict[str, Any]:
    try:
        validate_node_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    node = _registry_or_503().get_node(name)
    if node is None:
        raise HTTPException(status_code=404, detail=f"Unknown node '{name}'")
    return node


@router.get("/api/v1/fleet/nodes")
def list_fleet_nodes(request: Request):
    require_admin(request)
    return {"nodes": _registry_or_503().list_nodes()}


@router.get("/api/v1/fleet/health")
def fleet_health(request: Request):
    require_admin(request)
    registry = _registry_or_503()
    nodes = registry.list_nodes()
    by_status: Dict[str, int] = {}
    vram_total = 0.0
    for node in nodes:
        by_status[node["status"]] = by_status.get(node["status"], 0) + 1
        vram_total += float(node.get("vram_total_gb") or 0)
    healthy = registry.healthy_nodes()
    return {
        "nodes": len(nodes),
        "by_status": by_status,
        "vram_total_gb": round(vram_total, 2),
        "healthy_nodes": len(healthy),
        "vram_healthy_gb": round(sum(float(node.get("vram_total_gb") or 0) for node in healthy), 2),
        # Per-node *free* VRAM is reported by the node-agents in their
        # heartbeat payloads (see the daemon); the registry tracks totals.
    }


@router.post("/api/v1/fleet/nodes/{name}/approve")
async def approve_node(name: str, request: Request):
    reviewer = require_admin(request)
    node = _node_or_404(name)
    if node["status"] != "pending":
        _audit(reviewer, "fleet_approve", {"node": name},
               exit_code=1, extra={"reason": f"not_pending:{node['status']}"})
        raise HTTPException(status_code=409,
                            detail=f"node '{name}' is not pending (status={node['status']})")
    if not _registry_or_503().approve_node(name, reviewer):
        raise HTTPException(status_code=409, detail=f"node '{name}' could not be approved")
    _audit(reviewer, "fleet_approve", {"node": name})
    log_event(_FLEET_LOG, "node_approved", f"node '{name}' approved by '{reviewer}'",
              fields={"node": name, "approved_by": reviewer})
    return {"node": name, "status": "approved"}


@router.post("/api/v1/fleet/nodes/{name}/drain")
async def drain_node(name: str, request: Request):
    reviewer = require_admin(request)
    node = _node_or_404(name)
    if node["status"] not in ("approved", "active", "stale"):
        _audit(reviewer, "fleet_drain", {"node": name},
               exit_code=1, extra={"reason": f"bad_status:{node['status']}"})
        raise HTTPException(status_code=409,
                            detail=f"node '{name}' cannot be drained (status={node['status']})")
    address = node.get("address")
    if not address:
        raise HTTPException(status_code=409, detail=f"node '{name}' has no address recorded")
    url = f"https://{address}:{NODE_AGENT_PORT}/api/v1/fleet/nodes/{name}/drain"
    try:
        client = node_http_client()
    except RuntimeError as exc:
        _audit(reviewer, "fleet_drain", {"node": name}, exit_code=1,
               extra={"reason": "mtls_not_configured"})
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        with client:
            response = client.post(url, json={"node_name": name})
    except httpx.HTTPError as exc:
        _audit(reviewer, "fleet_drain", {"node": name}, exit_code=1,
               extra={"reason": "node_unreachable", "error": str(exc)[:200]})
        log_event(_FLEET_LOG, "drain_call_failed", f"node-agent drain call failed for '{name}'",
                  fields={"node": name, "error": str(exc)[:200]}, level=logging.WARNING)
        raise HTTPException(status_code=502, detail=f"node-agent unreachable: {exc}") from exc
    if response.status_code not in (200, 202):
        _audit(reviewer, "fleet_drain", {"node": name}, exit_code=1,
               extra={"reason": "node_rejected", "status": response.status_code})
        raise HTTPException(status_code=502,
                            detail=f"node-agent refused the drain: {response.status_code}")
    try:
        agent_state = response.json()
    except Exception:
        agent_state = {}
    # Acceptance of the drain is the point of no return for new traffic: the
    # node stops converging new models immediately, then stops the running
    # ones. The registry flips to `drained` now; the node confirms completion
    # in subsequent heartbeats.
    _registry_or_503().drain_node(name)
    _audit(reviewer, "fleet_drain", {"node": name},
           extra={"node_agent_status": agent_state.get("status")})
    log_event(_FLEET_LOG, "node_drained", f"node '{name}' drain accepted",
              fields={"node": name, "node_agent_status": agent_state.get("status")})
    return {"node": name, "status": "drained", "node_agent": agent_state}


@router.post("/api/v1/fleet/nodes/{name}/decommission")
async def decommission_node(name: str, request: Request):
    reviewer = require_admin(request)
    node = _node_or_404(name)
    if node["status"] == "retired":
        raise HTTPException(status_code=409, detail=f"node '{name}' is already retired")
    if not _registry_or_503().decommission_node(name):
        raise HTTPException(status_code=409, detail=f"node '{name}' could not be decommissioned")
    _audit(reviewer, "fleet_decommission", {"node": name})
    log_event(_FLEET_LOG, "node_decommissioned", f"node '{name}' decommissioned",
              fields={"node": name, "decommissioned_by": reviewer})
    return {"node": name, "status": "retired"}
