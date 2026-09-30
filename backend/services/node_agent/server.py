#!/usr/bin/env python3
"""Node agent HTTP service (port 8001, LAN-facing).

Endpoints:
- POST /api/v1/fleet/register — the node enrolls itself at boot (auto-
  registration). Called locally by the node's own startup with its hardware
  inventory; the node appears as `pending` until a human approves it on the
  platform side.
- POST /api/v1/fleet/nodes/{name}/heartbeat — state exchange with the
  platform. The platform polls this endpoint (mTLS); the request carries the
  desired-state delta, the response carries the actual state plus the applied
  desired-state version. The node converges in a background thread.
- GET /healthz — local liveness: engines alive, nvidia-smi responsive.
- POST /api/v1/fleet/nodes/{name}/drain — execute the local drain: refuse new
  models, stop running ones, confirm `drained`.
- GET /api/v1/node/info — local inventory.
- /api/v1/models/* — local model lifecycle (see models_admin.py).

Design note: the heartbeat direction here (platform polls node) is inverted
relative to ARCHITECTURE.md §5 (node pushes to platform), because this task
places register/heartbeat on the node-agent's :8001 surface. The state-
exchange semantics are identical: desired state in, actual state out, and the
platform remains the placement authority. The node never decides placement.

Auth: `auth.require_node_identity` everywhere. In `mtls` mode (default) the
TLS handshake with `CERT_REQUIRED` + the fleet CA is the membership check;
the asserted `node_name` is bound to the request and again at human approval.
"""
import os
import ssl
import sys
import threading
import time
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
import uvicorn

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from services.logging_setup import RequestLoggingMiddleware, configure, get_logger, log_event
from services.model_manager import registry as registry_module

from . import auth, converge as converge_module, hwinfo
from .models_admin import router as models_admin_router, node_identity

app = FastAPI(title="Sysadmin Node Agent", version="1.0.0")
app.add_middleware(RequestLoggingMiddleware, service="node_agent")
_LOG = get_logger("node_agent.server")

app.include_router(models_admin_router)

# --- local node state (secretless, ephemeral; the platform is authoritative) --
_STATE: Dict[str, Any] = {
    "registered": False,
    "node_name": None,
    "inventory": {},
    "address": None,
    "registered_at": None,
    "pending_desired": None,      # {"version": int, "desired": {...}}
    "applied_version": 0,
    "last_converge": None,        # per-model result map of the last run
    "draining": False,
    "drain_status": "idle",       # idle | draining | drained | error
    "drained_at": None,
}


def _reset_state() -> None:
    """Test hook: restore the pristine boot state."""
    _STATE.update({
        "registered": False, "node_name": None, "inventory": {},
        "address": None, "registered_at": None, "pending_desired": None,
        "applied_version": 0, "last_converge": None, "draining": False,
        "drain_status": "idle", "drained_at": None,
    })


def _is_running(entry: Dict[str, Any]) -> bool:
    return converge_module._is_running(entry, model_registry)


model_registry = registry_module.model_registry


def build_ssl_context() -> Optional[ssl.SSLContext]:
    """mTLS server context from operator-provisioned files.

    All three of NODE_AGENT_TLS_CERT / NODE_AGENT_TLS_KEY / NODE_AGENT_TLS_CA
    must be set; the CA is the fleet trust anchor and the client certificate
    is *required* at the handshake (membership is cryptographic). Returns None
    when TLS is not configured — `auth` then fails closed for non-HTTPS
    requests in `mtls` mode, so a missing cert is loud, not silent.
    """
    cert = os.getenv("NODE_AGENT_TLS_CERT")
    key = os.getenv("NODE_AGENT_TLS_KEY")
    ca = os.getenv("NODE_AGENT_TLS_CA")
    if not (cert and key and ca):
        return None
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(certfile=cert, keyfile=key)
    context.load_verify_locations(cafile=ca)
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def _actual_state() -> Dict[str, Any]:
    models = []
    for entry in model_registry.all():
        server = entry.get("server") or {}
        models.append({
            "name": entry.get("name"),
            "engine": entry.get("engine"),
            "status": entry.get("status"),
            "running": _is_running(entry),
            "port": server.get("port"),
        })
    return {"models": models, "draining": _STATE["draining"],
            "drain_status": _STATE["drain_status"]}


def _converge_pending() -> None:
    pending = _STATE.get("pending_desired")
    if not pending:
        return
    version = pending["version"]
    desired = pending["desired"]
    log_event(_LOG, "converge_started", f"converging desired state v{version}",
              fields={"version": version, "models": sorted(desired)})
    results = converge_module.converge(desired, model_registry)
    _STATE["last_converge"] = {"version": version, "results": results,
                               "finished_at": time.time()}
    if _STATE.get("pending_desired") is pending:
        _STATE["pending_desired"] = None
        _STATE["applied_version"] = version
    log_event(_LOG, "converge_finished", f"desired state v{version} applied",
              fields={"version": version,
                      "failed": sum(1 for r in results.values() if r.get("status") == "error")})


@app.post("/api/v1/fleet/register", status_code=201)
async def register_node(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="a JSON object is required")
    node_name = auth.require_node_identity(request, body.get("node_name"))
    inventory = body.get("inventory")
    if inventory is None:
        inventory = hwinfo.gpu_inventory()
    elif not isinstance(inventory, dict):
        raise HTTPException(status_code=400, detail="inventory must be an object")
    address = body.get("address")
    if address is not None and not isinstance(address, str):
        raise HTTPException(status_code=400, detail="address must be a string")
    _STATE.update({
        "registered": True, "node_name": node_name, "inventory": inventory,
        "address": address, "registered_at": time.time(),
    })
    log_event(_LOG, "node_registered", f"node '{node_name}' registered (pending approval)",
              fields={"node": node_name, "gpu_count": inventory.get("gpu_count"),
                      "vram_total_gb": inventory.get("vram_total_gb")})
    return {"node": node_name, "status": "pending", "registered_at": _STATE["registered_at"],
            "inventory": inventory}


@app.post("/api/v1/fleet/nodes/{name}/heartbeat")
async def heartbeat(name: str, request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="a JSON object is required")
    node_name = auth.require_self(request, name, body.get("node_name"))
    if not _STATE["registered"]:
        raise HTTPException(status_code=409, detail=f"node '{node_name}' is not registered")
    desired = body.get("desired_state") or {}
    version = body.get("desired_version") or 0
    if not isinstance(desired, dict):
        raise HTTPException(status_code=400, detail="desired_state must be an object")
    response: Dict[str, Any] = {
        "node": node_name,
        "status": "draining" if _STATE["draining"] else "ok",
        "registered": True,
        "applied_desired_version": _STATE["applied_version"],
        "actual": _actual_state(),
    }
    if _STATE["draining"] or _STATE["drain_status"] == "drained":
        # A draining node accepts no new work; convergence is limited to stops.
        stops = {model: spec for model, spec in desired.items()
                 if isinstance(spec, dict) and spec.get("action") == "stop"}
        if version > _STATE["applied_version"] and stops:
            _STATE["pending_desired"] = {"version": version, "desired": stops}
            threading.Thread(target=_converge_pending, daemon=True).start()
        response["note"] = "node is draining: only stop actions converge"
        return response
    if version > _STATE["applied_version"]:
        _STATE["pending_desired"] = {"version": version, "desired": desired}
        threading.Thread(target=_converge_pending, daemon=True).start()
        response["converging"] = version
    return response


def _execute_drain() -> None:
    """Local drain sequence: refuse new models, stop running ones, confirm."""
    _STATE["draining"] = True
    _STATE["drain_status"] = "draining"
    log_event(_LOG, "drain_started", "drain started: no new models will be accepted",
              fields={"node": _STATE.get("node_name")})
    try:
        running = [entry["name"] for entry in model_registry.all() if _is_running(entry)]
        if running:
            desired = {name: {"action": "stop"} for name in running}
            results = converge_module.converge(desired, model_registry)
            failed = [name for name, r in results.items() if r.get("status") == "error"]
            if failed:
                raise RuntimeError(f"could not stop: {', '.join(failed)}")
        _STATE["drain_status"] = "drained"
        _STATE["drained_at"] = time.time()
        log_event(_LOG, "drain_confirmed", "drain confirmed: node is drained",
                  fields={"node": _STATE.get("node_name"), "stopped": running})
    except Exception as exc:
        _STATE["drain_status"] = "error"
        log_event(_LOG, "drain_failed", f"drain failed: {exc}",
                  fields={"node": _STATE.get("node_name")}, level=40)
    finally:
        _STATE["draining"] = False


@app.post("/api/v1/fleet/nodes/{name}/drain", status_code=202)
async def drain_node(name: str, request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="a JSON object is required")
    node_name = auth.require_self(request, name, body.get("node_name"))
    if not _STATE["registered"]:
        raise HTTPException(status_code=409, detail=f"node '{node_name}' is not registered")
    if _STATE["drain_status"] == "drained":
        return {"node": node_name, "status": "drained", "drained_at": _STATE["drained_at"]}
    if _STATE["draining"]:
        return {"node": node_name, "status": "draining"}
    threading.Thread(target=_execute_drain, daemon=True).start()
    return {"node": node_name, "status": "draining"}


@app.get("/healthz")
async def healthz():
    checks: Dict[str, Any] = {}
    inventory = hwinfo.gpu_inventory()
    checks["gpu"] = {"ok": bool(inventory.get("available")),
                     "detail": f"{inventory.get('gpu_count')}x {inventory.get('gpu_model')}"}
    unhealthy = []
    for entry in model_registry.all():
        server = entry.get("server") or {}
        if server.get("status") == "running" and not _is_running(entry):
            unhealthy.append(entry.get("name"))
    checks["engines"] = {"ok": not unhealthy, "unhealthy": unhealthy}
    ok = all(check["ok"] for check in checks.values())
    return JSONResponse(status_code=200 if ok else 503,
                        content={"status": "ok" if ok else "degraded", "checks": checks,
                                 "node": _STATE.get("node_name"), "drain_status": _STATE["drain_status"]})


@app.get("/api/v1/node/info")
async def node_info(request: Request, node_name: str = Depends(node_identity)):
    return {"node": node_name,
            "registered": _STATE["registered"],
            "registered_at": _STATE["registered_at"],
            "address": _STATE["address"],
            "inventory": hwinfo.gpu_inventory(),
            "applied_desired_version": _STATE["applied_version"],
            "drain_status": _STATE["drain_status"],
            "actual": _actual_state()}


@app.exception_handler(ConnectionError)
async def store_unavailable(_request: Request, _exc: ConnectionError):
    return JSONResponse(status_code=503, content={"detail": "Required store unavailable"})


def main() -> None:
    configure("node_agent")
    host = os.getenv("NODE_AGENT_HOST", "0.0.0.0")
    port = int(os.getenv("NODE_AGENT_PORT", "8001"))
    ssl_context = build_ssl_context()
    if ssl_context is None:
        log_event(_LOG, "tls_not_configured",
                  "NODE_AGENT_TLS_CERT/KEY/CA not all set: serving plain HTTP; "
                  "mtls mode will fail closed on every fleet call")
    else:
        log_event(_LOG, "tls_configured", "mTLS enabled: client certificates required at handshake")
    log_event(_LOG, "service_start", f"node agent listening on {host}:{port}",
              fields={"host": host, "port": port, "tls": ssl_context is not None})
    uvicorn.run(app, host=host, port=port, ssl=ssl_context,
                log_level="info", access_log=False)  # requests are logged as JSON by the middleware


if __name__ == "__main__":
    main()
