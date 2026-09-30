"""Local model lifecycle admin surface for the GPU node.

A slimmed, secretless port of the model lifecycle: register / download /
start / stop / params, backed by the same `registry`, `downloader`,
`vllm_server` and `llamacpp_server` modules the mono-host API uses.

Deliberately NOT imported (GPU nodes are secretless):
- `services.agent_tools` (audit, bearer auth) — the node holds no user keys;
- `services.auth_gateway` — no user identity exists on this host.

The gate is node identity (`node_agent.auth.require_node_identity`): every
call carries `node_name` (query param for GETs, JSON body otherwise) and the
mTLS handshake proves fleet membership. Structural validation below mirrors
`services/model_manager/router.py`, which cannot be imported here because it
pulls in `services.agent_tools`/`services.auth_gateway`.
"""
import logging
import threading
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from services.model_manager import downloader, llamacpp_server, registry as registry_module, vllm_server
from services.logging_setup import get_logger, log_event
from .auth import require_node_identity

router = APIRouter()
model_registry = registry_module.model_registry
_LOG = get_logger("node_agent.models_admin")

_JOBS: Dict[str, Dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()

# Bounded, admin-selectable loading parameters per engine. Same sets as the
# mono-host API (`services/model_manager/router.py`); re-declared here because
# importing that module would drag in agent_tools/auth_gateway.
VLLM_LOAD_FIELDS = frozenset({
    "quantization", "max_model_len", "tensor_parallel_size", "gpu_memory_utilization",
    "dtype", "kv_cache_dtype", "max_num_seqs", "enforce_eager", "enable_prefix_caching",
})
LLAMACPP_LOAD_FIELDS = frozenset({
    "ctx_size", "n_gpu_layers", "flash_attn", "threads", "batch_size", "mmap", "mlock",
})


async def node_identity(request: Request, node_name: Optional[str] = Query(default=None)) -> str:
    """Resolve `node_name` from the query string (GETs) or the JSON body."""
    if node_name is None:
        try:
            body = await request.json()
        except Exception:
            body = None
        node_name = body.get("node_name") if isinstance(body, dict) else None
    return require_node_identity(request, node_name)


def _positive_int(value: Any, field: str) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be an integer") from None
    if result < 1:
        raise ValueError(f"{field} must be positive")
    return result


def _flag(value: Any, field: str) -> Optional[bool]:
    if value is None:
        return None
    if type(value) is not bool:
        raise ValueError(f"{field} must be a boolean")
    return value


def _parse_fields(body: Dict[str, Any], engine: str) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    if engine == registry_module.ENGINE_LLAMACPP:
        for field in ("ctx_size",):
            if field in body:
                value = _positive_int(body.get(field), field)
                if value is not None and not 512 <= value <= 131072:
                    raise ValueError("ctx_size must be between 512 and 131072")
                fields[field] = value
        if "n_gpu_layers" in body:
            value = body.get("n_gpu_layers")
            if value is None:
                fields["n_gpu_layers"] = None
            elif value != "all" and (type(value) is not int or value < 0):
                raise ValueError("n_gpu_layers must be 'all' or a non-negative integer")
            else:
                fields["n_gpu_layers"] = value
        for field in ("threads", "batch_size"):
            if field in body:
                fields[field] = _positive_int(body.get(field), field)
        for field in ("flash_attn", "mmap", "mlock"):
            if field in body:
                fields[field] = _flag(body.get(field), field)
    else:
        if "quantization" in body:
            value = body.get("quantization")
            fields["quantization"] = str(value) if value else None
        for field in ("max_model_len", "tensor_parallel_size", "max_num_seqs"):
            if field in body:
                fields[field] = _positive_int(body.get(field), field)
        if "gpu_memory_utilization" in body:
            value = body.get("gpu_memory_utilization")
            if value is not None:
                value = float(value)
                if not 0 < value <= 1:
                    raise ValueError("gpu_memory_utilization must be in (0, 1]")
            fields["gpu_memory_utilization"] = value
        if "dtype" in body:
            fields["dtype"] = registry_module.validate_dtype(body.get("dtype"))
        if "kv_cache_dtype" in body:
            fields["kv_cache_dtype"] = registry_module.validate_kv_cache_dtype(body.get("kv_cache_dtype"))
        for field in ("enforce_eager", "enable_prefix_caching"):
            if field in body:
                fields[field] = _flag(body.get(field), field)
    return fields


def _entry_or_404(name: str) -> Dict[str, Any]:
    entry = model_registry.get(name)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Unknown model '{name}'")
    return entry


def _server_for(entry: Dict[str, Any]):
    engine = registry_module.validate_engine(entry.get("engine", registry_module.ENGINE_VLLM))
    return llamacpp_server if engine == registry_module.ENGINE_LLAMACPP else vllm_server


@router.get("/api/v1/models")
async def list_models(request: Request, node_name: str = Depends(node_identity)):
    return {"node": node_name, "models": model_registry.all()}


@router.get("/api/v1/models/{name}")
async def get_model(name: str, request: Request, node_name: str = Depends(node_identity)):
    try:
        registry_module.validate_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"node": node_name, "model": _entry_or_404(name),
            "download": downloader.job_status(name)}


@router.post("/api/v1/models", status_code=201)
async def register_model(request: Request, node_name: str = Depends(node_identity)):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="a JSON object is required")
    try:
        repo, revision = downloader.parse_hf_reference(body.get("hf_repo") or body.get("reference") or "")
        name = body.get("name") or repo.split("/")[-1]
        name = registry_module.validate_name(name)
        revision = registry_module.validate_revision(body.get("revision") or revision)
        engine = registry_module.validate_engine(body.get("engine"))
        entry_fields: Dict[str, Any] = {
            "hf_repo": repo, "revision": revision, "engine": engine,
            "status": registry_module.STATUS_REGISTERED,
            "path": model_registry.path_for(name),
            "last_error": None,
            "server": {"pid": None, "port": None, "status": "stopped"},
        }
        if engine == registry_module.ENGINE_LLAMACPP:
            entry_fields["gguf_file"] = registry_module.validate_gguf_file(body.get("gguf_file"))
        elif body.get("gguf_file") is not None:
            raise ValueError("gguf_file is only valid for the llamacpp engine")
        allowed = LLAMACPP_LOAD_FIELDS if engine == registry_module.ENGINE_LLAMACPP else VLLM_LOAD_FIELDS
        unknown = sorted(set(body) - {"node_name", "name", "hf_repo", "reference", "revision",
                                     "engine", "gguf_file"} - set(allowed))
        if unknown:
            raise ValueError(f"unknown field(s): {', '.join(unknown)}")
        for field, value in _parse_fields(body, engine).items():
            if value is not None:
                entry_fields[field] = value
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if model_registry.get(name):
        raise HTTPException(status_code=409, detail=f"model '{name}' is already registered")
    entry = model_registry.upsert(name, entry_fields)
    log_event(_LOG, "model_registered", f"model '{name}' registered by node '{node_name}'",
              fields={"node": node_name, "name": name, "engine": engine})
    return {"node": node_name, "model": entry}


@router.patch("/api/v1/models/{name}")
async def patch_model(name: str, request: Request, node_name: str = Depends(node_identity)):
    try:
        registry_module.validate_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="a JSON object is required")
    entry = _entry_or_404(name)
    engine = registry_module.validate_engine(entry.get("engine"))
    allowed = LLAMACPP_LOAD_FIELDS if engine == registry_module.ENGINE_LLAMACPP else VLLM_LOAD_FIELDS
    unknown = sorted(set(body) - set(allowed) - {"node_name"})
    if unknown or not {k for k in body if k != "node_name"}:
        raise HTTPException(
            status_code=400,
            detail=(f"unknown or immutable field(s): {', '.join(unknown)}" if unknown
                    else "no loading parameters provided"),
        )
    try:
        fields = _parse_fields(body, engine)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    updated = model_registry.update(name, **fields)
    log_event(_LOG, "model_parameters_updated", f"loading parameters updated for '{name}'",
              fields={"node": node_name, "name": name, "fields": list(fields)})
    return {"node": node_name, "model": updated}


def _background_download(name: str) -> None:
    try:
        downloader.download_sync(name, model_registry)
    except Exception as exc:
        log_event(_LOG, "model_download_failed", f"download failed for '{name}'",
                  fields={"name": name, "error": str(exc)[:200]}, level=logging.WARNING)


@router.post("/api/v1/models/{name}/download", status_code=202)
async def download_model(name: str, request: Request, node_name: str = Depends(node_identity)):
    try:
        registry_module.validate_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _entry_or_404(name)
    with _JOBS_LOCK:
        job = _JOBS.get(name)
        if job and job.get("status") == "downloading":
            raise HTTPException(status_code=409, detail=f"model '{name}' already has an active operation")
        _JOBS[name] = {"status": "downloading", "started_at": time.time()}
    threading.Thread(target=_background_download, args=(name,), daemon=True).start()
    log_event(_LOG, "model_download_started", f"download started for '{name}'",
              fields={"node": node_name, "name": name})
    return {"node": node_name, "status": "downloading", "name": name}


def _background_start(name: str) -> None:
    try:
        entry = model_registry.get(name) or {}
        _server_for(entry).start(name, model_registry)
        with _JOBS_LOCK:
            _JOBS[name] = {"status": "running", "started_at": time.time()}
    except Exception as exc:
        with _JOBS_LOCK:
            _JOBS[name] = {"status": "error", "started_at": time.time(), "error": str(exc)[:300]}
        log_event(_LOG, "model_start_failed", f"start failed for '{name}'",
                  fields={"name": name, "error": str(exc)[:200]}, level=logging.WARNING)


@router.post("/api/v1/models/{name}/start", status_code=202)
async def start_model(name: str, request: Request, node_name: str = Depends(node_identity)):
    try:
        registry_module.validate_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    entry = _entry_or_404(name)
    if entry.get("status") not in (registry_module.STATUS_DOWNLOADED, registry_module.STATUS_STOPPED,
                                   registry_module.STATUS_ERROR):
        raise HTTPException(status_code=409,
                            detail=f"model '{name}' is not downloaded (status={entry.get('status')})")
    with _JOBS_LOCK:
        job = _JOBS.get(name)
        if job and job.get("status") == "starting":
            raise HTTPException(status_code=409, detail=f"model '{name}' already has an active operation")
        _JOBS[name] = {"status": "starting", "started_at": time.time()}
    threading.Thread(target=_background_start, args=(name,), daemon=True).start()
    log_event(_LOG, "model_start_requested", f"start requested for '{name}'",
              fields={"node": node_name, "name": name})
    return {"node": node_name, "status": "starting", "name": name}


@router.post("/api/v1/models/{name}/stop")
async def stop_model(name: str, request: Request, node_name: str = Depends(node_identity)):
    try:
        registry_module.validate_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    entry = _entry_or_404(name)
    try:
        entry = _server_for(entry).stop(name, model_registry)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"stop failed: {exc}") from exc
    log_event(_LOG, "model_stopped", f"model '{name}' stopped",
              fields={"node": node_name, "name": name})
    return {"node": node_name, "model": entry}


@router.delete("/api/v1/models/{name}")
async def delete_model(name: str, request: Request, node_name: str = Depends(node_identity)):
    try:
        registry_module.validate_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _entry_or_404(name)
    with _JOBS_LOCK:
        job = _JOBS.get(name)
        if job and job.get("status") in ("downloading", "starting"):
            raise HTTPException(status_code=409, detail="wait for the active model operation before deleting it")
    model_registry.delete(name)
    log_event(_LOG, "model_deleted", f"model '{name}' deleted by node '{node_name}'",
              fields={"node": node_name, "name": name})
    return {"node": node_name, "status": "deleted", "name": name}
