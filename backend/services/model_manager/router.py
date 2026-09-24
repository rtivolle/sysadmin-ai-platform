"""Admin-only HTTP surface for the local model lifecycle.

All mutations require the `admin` role, which is derived from credentials (the
master key), never from a client header. Downloads and vLLM starts run in
background threads; the caller polls `GET /api/v1/models/{name}`.
"""
import os
import shutil
import threading
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request

from services.agent_tools.audit import log_audit_event
from services.auth_gateway.server import authenticate_request, role_for_user

from . import downloader, llamacpp_server, litellm_sync, registry as registry_module, vllm_server

router = APIRouter()
model_registry = registry_module.model_registry

_START_JOBS: Dict[str, Dict[str, Any]] = {}
_START_LOCK = threading.Lock()
_ENGINE_START_LOCK = threading.Lock()


def require_admin(request: Request) -> str:
    user_id, _ = authenticate_request(request)
    if role_for_user(user_id) != "admin":
        raise HTTPException(status_code=403, detail="Administrator required")
    return user_id


def _audit(reviewer: str, action: str, parameters: Dict[str, Any], exit_code: int = 0, extra: Optional[dict] = None) -> None:
    log_audit_event(
        user_id=reviewer, session_id="", tool_name=action, action=action,
        parameters=parameters, exit_code=exit_code, duration_ms=0, extra=extra,
    )


def _entry_or_404(name: str) -> Dict[str, Any]:
    entry = model_registry.get(name)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Unknown model '{name}'")
    return entry


def _server_for(entry: Dict[str, Any]):
    engine = registry_module.validate_engine(entry.get("engine", registry_module.ENGINE_VLLM))
    return llamacpp_server if engine == registry_module.ENGINE_LLAMACPP else vllm_server


def _is_engine_running(entry: Dict[str, Any]) -> bool:
    """Use process identity for exclusion; never signal on an HTTP health miss."""
    server = _server_for(entry)
    if server is llamacpp_server:
        return server.process_matches(entry, model_registry)
    return server.is_running(entry)


def _public(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Freshen the liveness of the server record for reads."""
    server = _server_for(entry)
    running = _is_engine_running(entry)
    if entry.get("server", {}).get("status") == "running" and not running:
        try:
            entry = server.stop(entry["name"], model_registry)
        except Exception:
            entry = model_registry.update(
                entry["name"],
                server={**entry.get("server", {}), "status": "error", "pid": None, "port": None},
                status=registry_module.STATUS_ERROR,
                last_error="Model server health check failed and shutdown could not be confirmed",
            )
    return entry


@router.get("/api/v1/models")
def list_models(request: Request):
    require_admin(request)
    return {"models": [_public(entry) for entry in model_registry.all()]}


@router.get("/api/v1/models/{name}")
def get_model(name: str, request: Request):
    require_admin(request)
    try:
        registry_module.validate_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"model": _public(_entry_or_404(name)), "download": downloader.job_status(name)}


@router.post("/api/v1/models", status_code=201)
async def register_model(request: Request):
    reviewer = require_admin(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="a JSON object is required")
    try:
        repo, revision = downloader.parse_hf_reference(body.get("hf_repo") or body.get("reference") or "")
        name = body.get("name") or repo.split("/")[-1]
        name = registry_module.validate_name(name)
        revision = registry_module.validate_revision(body.get("revision") or revision)
        engine = registry_module.validate_engine(body.get("engine"))
        existing = model_registry.get(name)
        if existing:
            existing = _public(existing)
            if existing.get("status") in (registry_module.STATUS_STARTING, registry_module.STATUS_DOWNLOADING) \
                    or _is_engine_running(existing):
                raise HTTPException(status_code=409, detail=f"model '{name}' is active; stop it before changing registration")
        entry_fields: Dict[str, Any] = {
            "hf_repo": repo,
            "revision": revision,
            "engine": engine,
            "status": registry_module.STATUS_REGISTERED,
            "path": model_registry.path_for(name),
            "last_error": None,
            "server": {"pid": None, "port": None, "status": "stopped"},
        }
        if engine == registry_module.ENGINE_LLAMACPP:
            entry_fields["gguf_file"] = registry_module.validate_gguf_file(body.get("gguf_file"))
            ctx_size = body.get("ctx_size", 2048)
            if type(ctx_size) is not int or not 512 <= ctx_size <= 131072:
                raise ValueError("ctx_size must be an integer between 512 and 131072")
            entry_fields["ctx_size"] = ctx_size
            n_gpu_layers = body.get("n_gpu_layers", "all")
            if n_gpu_layers != "all" and (type(n_gpu_layers) is not int or n_gpu_layers < 0):
                raise ValueError("n_gpu_layers must be 'all' or a non-negative integer")
            entry_fields["n_gpu_layers"] = n_gpu_layers
            flash_attn = body.get("flash_attn", True)
            if type(flash_attn) is not bool:
                raise ValueError("flash_attn must be a boolean")
            entry_fields["flash_attn"] = flash_attn
        elif body.get("gguf_file") is not None:
            raise ValueError("gguf_file is only valid for the llamacpp engine")
        for field in ("quantization",):
            if body.get(field):
                entry_fields[field] = str(body[field])
        for field in ("max_model_len", "tensor_parallel_size"):
            if body.get(field) is not None:
                value = int(body[field])
                if value < 1:
                    raise ValueError(f"{field} must be positive")
                entry_fields[field] = value
        if body.get("gpu_memory_utilization") is not None:
            value = float(body["gpu_memory_utilization"])
            if not 0 < value <= 1:
                raise ValueError("gpu_memory_utilization must be in (0, 1]")
            entry_fields["gpu_memory_utilization"] = value
    except HTTPException:
        raise
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    entry = model_registry.upsert(name, entry_fields)
    _audit(reviewer, "model_register", {"name": name, "hf_repo": repo, "revision": revision})
    return {"model": entry}


@router.post("/api/v1/models/{name}/download", status_code=202)
def download_model(name: str, request: Request):
    reviewer = require_admin(request)
    _entry_or_404(name)
    try:
        downloader.start_download(name, model_registry)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _audit(reviewer, "model_download", {"name": name})
    return {"status": "downloading", "download": downloader.job_status(name)}


def _start_job(name: str) -> None:
    with _START_LOCK:
        started_at = (_START_JOBS.get(name) or {}).get("started_at", time.time())
        _START_JOBS[name] = {"status": "starting", "started_at": started_at, "error": None}
    try:
        current = model_registry.get(name) or {}
        server = _server_for(current)
        # Only one model can claim a host GPU/port window during load at a time.
        with _ENGINE_START_LOCK:
            entry = server.start(name, model_registry)
        sync_result = litellm_sync.sync(model_registry)
        restart = sync_result.get("restart") or {}
        if restart and (restart.get("exit_code") != 0 or restart.get("error")):
            raise RuntimeError(f"LiteLLM restart failed: {restart.get('output', 'unknown error')}")
        with _START_LOCK:
            _START_JOBS[name] = {
                "status": "running", "started_at": _START_JOBS[name]["started_at"],
                "error": None, "port": (entry.get("server") or {}).get("port"),
                "litellm": sync_result,
            }
    except Exception as exc:
        current = model_registry.get(name) or {}
        if (current.get("server") or {}).get("status") == "running":
            try:
                _server_for(current).stop(name, model_registry)
                litellm_sync.sync(model_registry, restart=False)
            except Exception:
                pass
        model_registry.update(name, status=registry_module.STATUS_ERROR, last_error=str(exc))
        with _START_LOCK:
            _START_JOBS[name] = {"status": "error", "started_at": time.time(), "error": str(exc)}


@router.post("/api/v1/models/{name}/start", status_code=202)
def start_model(name: str, request: Request):
    reviewer = require_admin(request)
    entry = _public(_entry_or_404(name))
    if _is_engine_running(entry):
        raise HTTPException(status_code=409, detail=f"model '{name}' is already running")
    if entry.get("status") not in (registry_module.STATUS_DOWNLOADED, registry_module.STATUS_STOPPED,
                                   registry_module.STATUS_ERROR):
        raise HTTPException(status_code=409, detail=f"model '{name}' is not downloaded (status={entry.get('status')})")
    with _START_LOCK:
        job = _START_JOBS.get(name)
        current = model_registry.get(name) or {}
        if (job and job.get("status") == "starting") or current.get("status") == registry_module.STATUS_STARTING:
            raise HTTPException(status_code=409, detail=f"model '{name}' is already starting")
        _START_JOBS[name] = {"status": "starting", "started_at": time.time(), "error": None}
    _audit(reviewer, "model_start", {"name": name})
    try:
        threading.Thread(target=_start_job, args=(name,), daemon=True).start()
    except Exception:
        with _START_LOCK:
            _START_JOBS[name] = {"status": "error", "started_at": time.time(), "error": "Could not start model job"}
        raise
    return {"status": "starting", "name": name}


@router.post("/api/v1/models/{name}/stop")
def stop_model(name: str, request: Request):
    reviewer = require_admin(request)
    entry = _entry_or_404(name)
    with _START_LOCK:
        job = _START_JOBS.get(name)
        if (job and job.get("status") == "starting") or entry.get("status") == registry_module.STATUS_STARTING:
            raise HTTPException(status_code=409, detail=f"model '{name}' is starting")
    entry = _server_for(_entry_or_404(name)).stop(name, model_registry)
    sync_result = litellm_sync.sync(model_registry)
    _audit(reviewer, "model_stop", {"name": name})
    return {"model": entry, "litellm": sync_result}


@router.post("/api/v1/models/{name}/restart", status_code=202)
def restart_model(name: str, request: Request):
    require_admin(request)
    entry = _entry_or_404(name)
    with _START_LOCK:
        job = _START_JOBS.get(name)
        if (job and job.get("status") == "starting") or entry.get("status") == registry_module.STATUS_STARTING:
            raise HTTPException(status_code=409, detail=f"model '{name}' is starting")
    _server_for(_entry_or_404(name)).stop(name, model_registry)
    return start_model(name, request)


@router.delete("/api/v1/models/{name}")
def delete_model(name: str, request: Request, delete_files: int = 0):
    reviewer = require_admin(request)
    entry = _entry_or_404(name)
    if entry.get("status") in (registry_module.STATUS_DOWNLOADING, registry_module.STATUS_STARTING):
        raise HTTPException(status_code=409, detail="wait for the active model operation before deleting it")
    if _is_engine_running(entry):
        raise HTTPException(status_code=409, detail="stop the model before deleting it")
    path = entry.get("path") or model_registry.path_for(name)
    if delete_files == 1 and os.path.isdir(path):
        expected = os.path.abspath(model_registry.models_dir)
        if os.path.commonpath([os.path.abspath(path), expected]) != expected:
            raise HTTPException(status_code=400, detail="model path escapes the models directory")
        shutil.rmtree(path)
    model_registry.delete(name)
    litellm_sync.sync(model_registry)
    _audit(reviewer, "model_delete", {"name": name, "delete_files": bool(delete_files)})
    return {"status": "deleted", "name": name}


@router.get("/api/v1/models/{name}/logs")
def model_logs(name: str, request: Request, tail: int = 32768):
    require_admin(request)
    _entry_or_404(name)
    tail = max(1024, min(int(tail), 262144))
    return {"name": name, "output": _server_for(_entry_or_404(name)).tail_log(name, tail)}
