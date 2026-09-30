"""Admin-only HTTP surface for the local model lifecycle.

All mutations require the `admin` role, which is derived from credentials (the
master key), never from a client header. Downloads and vLLM starts run in
background threads; the caller polls `GET /api/v1/models/{name}`.
"""
import os
import shutil
import threading
import time
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request

from services.agent_tools.audit import log_audit_event
from services.auth_gateway.server import authenticate_request, role_for_user

from . import downloader, llamacpp_server, litellm_sync, promotion as promotion_module, registry as registry_module, vllm_server
from services.control_store import open_fleet_registry
from services.logging_setup import get_logger, log_event

router = APIRouter()
model_registry = registry_module.model_registry
_LIFECYCLE_LOG = get_logger("model_manager")

_START_JOBS: Dict[str, Dict[str, Any]] = {}
_START_LOCK = threading.Lock()
_ENGINE_START_LOCK = threading.Lock()

# Loading parameters an admin may select per engine. PATCH accepts only these
# keys (everything else is immutable through the API); registration reads the
# same sets so the registry stays bounded and predictable.
VLLM_LOAD_FIELDS = frozenset({
    "quantization", "max_model_len", "tensor_parallel_size", "gpu_memory_utilization",
    "dtype", "kv_cache_dtype", "enforce_eager", "max_num_seqs", "enable_prefix_caching",
})
LLAMACPP_LOAD_FIELDS = frozenset({
    "ctx_size", "n_gpu_layers", "flash_attn", "threads", "batch_size", "mmap", "mlock",
})


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


def _parse_vllm_fields(body: Dict[str, Any]) -> Dict[str, Any]:
    """Validated vLLM loading parameters. An explicit `null` clears a field."""
    fields: Dict[str, Any] = {}
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


def _parse_llamacpp_fields(body: Dict[str, Any], include_defaults: bool = False) -> Dict[str, Any]:
    """Validated llama.cpp loading parameters. An explicit `null` clears a field.

    With `include_defaults` (registration), the three legacy fields are always
    materialized with their defaults when the caller did not choose a value.
    """
    fields: Dict[str, Any] = {}
    defaults = {"ctx_size": 2048, "n_gpu_layers": "all", "flash_attn": True}
    for field in ("ctx_size", "n_gpu_layers", "flash_attn"):
        if field in body:
            value = body.get(field)
        elif include_defaults:
            value = defaults[field]
        else:
            continue
        if value is None:
            fields[field] = None
            continue
        if field == "ctx_size":
            if type(value) is not int or not 512 <= value <= 131072:
                raise ValueError("ctx_size must be an integer between 512 and 131072")
        elif field == "n_gpu_layers":
            if value != "all" and (type(value) is not int or value < 0):
                raise ValueError("n_gpu_layers must be 'all' or a non-negative integer")
        elif type(value) is not bool:
            raise ValueError("flash_attn must be a boolean")
        fields[field] = value
    for field in ("threads", "batch_size"):
        if field in body:
            fields[field] = _positive_int(body.get(field), field)
    for field in ("mmap", "mlock"):
        if field in body:
            fields[field] = _flag(body.get(field), field)
    return fields


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


def _audit_failure(reviewer: str, action: str, parameters: Dict[str, Any], extra: Optional[dict] = None) -> None:
    """Best-effort audit of a rejected/failed lifecycle call.

    A failed audit write must never change the rejection outcome, so every
    exception is swallowed here (matches the census best-effort convention).
    """
    log_event(_LIFECYCLE_LOG, "model_lifecycle_rejected", f"{action} rejected",
              fields={"action": action, "name": (parameters or {}).get("name"),
                      "reason": (extra or {}).get("reason"), "error": (extra or {}).get("error")},
              level=logging.WARNING)
    try:
        _audit(reviewer or "anonymous", action, parameters, exit_code=1, extra=extra)
    except Exception:
        pass


def _entry_or_404(name: str, action: Optional[str] = None, reviewer: Optional[str] = None) -> Dict[str, Any]:
    entry = model_registry.get(name)
    if entry is None:
        if action:
            _audit_failure(reviewer, action, {"name": name}, extra={"reason": "unknown_model"})
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
                # Keep pid/port so an explicit stop can retry reaping. Marking
                # error also prevents every list/get request from blocking on
                # the same failed shutdown attempt.
                server={**entry.get("server", {}), "status": "error"},
                status=registry_module.STATUS_ERROR,
                last_error="Model server identity was stale and shutdown could not be confirmed",
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
        _audit_failure(reviewer, "model_register", {}, extra={"reason": "invalid_body"})
        raise HTTPException(status_code=400, detail="a JSON object is required")
    try:
        repo, revision = downloader.parse_hf_reference(body.get("hf_repo") or body.get("reference") or "")
        name = body.get("name") or repo.split("/")[-1]
        name = registry_module.validate_name(name)
        revision = registry_module.validate_revision(body.get("revision") or revision)
        engine = registry_module.validate_engine(body.get("engine"))
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
            for field, value in _parse_llamacpp_fields(body, include_defaults=True).items():
                if value is not None:
                    entry_fields[field] = value
        elif body.get("gguf_file") is not None:
            raise ValueError("gguf_file is only valid for the llamacpp engine")
        for field, value in _parse_vllm_fields(body).items():
            if value is not None:
                entry_fields[field] = value
    except HTTPException:
        raise
    except (ValueError, TypeError) as exc:
        _audit_failure(reviewer, "model_register",
                       {"name": str(body.get("name") or "")[:64], "hf_repo": str(body.get("hf_repo") or "")[:128]},
                       extra={"reason": "validation", "error": str(exc)[:300]})
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with _START_LOCK:
        existing = model_registry.get(name)
        if existing:
            existing = _public(existing)
            job = _START_JOBS.get(name)
            download_job = downloader.job_status(name)
            if ((job and job.get("status") == "starting")
                    or download_job.get("status") == "downloading"
                    or existing.get("status") in (registry_module.STATUS_STARTING, registry_module.STATUS_DOWNLOADING)
                    or _is_engine_running(existing)):
                _audit_failure(reviewer, "model_register", {"name": name, "hf_repo": repo[:128]},
                               extra={"reason": "active_operation"})
                raise HTTPException(status_code=409, detail=f"model '{name}' is active; stop it before changing registration")
        entry = model_registry.upsert(name, entry_fields)
    _audit(reviewer, "model_register", {"name": name, "hf_repo": repo[:128], "revision": revision})
    log_event(_LIFECYCLE_LOG, "model_registered", f"model '{name}' registered",
              fields={"name": name, "engine": engine, "hf_repo": repo[:128]})
    return {"model": entry}


@router.patch("/api/v1/models/{name}")
async def patch_model(name: str, request: Request):
    """Update the loading parameters of an existing, inactive model.

    Only the engine's loading parameters may be changed; name, source, engine
    and files are immutable here. A field set to `null` is cleared so the
    engine's own default (or the operator config file) applies at next start.
    """
    reviewer = require_admin(request)
    try:
        registry_module.validate_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    body = await request.json()
    if not isinstance(body, dict):
        _audit_failure(reviewer, "model_update", {"name": name}, extra={"reason": "invalid_body"})
        raise HTTPException(status_code=400, detail="a JSON object is required")
    with _START_LOCK:
        entry = _entry_or_404(name, "model_update", reviewer)
        job = _START_JOBS.get(name)
        download_job = downloader.job_status(name)
        if ((job and job.get("status") == "starting")
                or download_job.get("status") == "downloading"
                or entry.get("status") in (registry_module.STATUS_STARTING, registry_module.STATUS_DOWNLOADING)
                or _is_engine_running(entry)):
            _audit_failure(reviewer, "model_update", {"name": name}, extra={"reason": "active_operation"})
            raise HTTPException(status_code=409,
                                detail=f"model '{name}' is active; stop it before changing loading parameters")
        engine = registry_module.validate_engine(entry.get("engine"))
        allowed = LLAMACPP_LOAD_FIELDS if engine == registry_module.ENGINE_LLAMACPP else VLLM_LOAD_FIELDS
        unknown = sorted(set(body) - set(allowed))
        if unknown or not body:
            error = (f"unknown or immutable field(s): {', '.join(unknown)}" if unknown
                     else "no loading parameters provided")
            _audit_failure(reviewer, "model_update", {"name": name},
                           extra={"reason": "validation", "error": error})
            raise HTTPException(status_code=400, detail=error)
        try:
            fields = (_parse_llamacpp_fields(body)
                      if engine == registry_module.ENGINE_LLAMACPP
                      else _parse_vllm_fields(body))
        except (ValueError, TypeError) as exc:
            _audit_failure(reviewer, "model_update", {"name": name},
                           extra={"reason": "validation", "error": str(exc)[:300]})
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        updated = model_registry.update(name, **fields)
    _audit(reviewer, "model_update", {"name": name, "fields": fields})
    log_event(_LIFECYCLE_LOG, "model_parameters_updated", f"loading parameters updated for '{name}'",
              fields={"name": name, "fields": fields})
    return {"model": updated}


@router.post("/api/v1/models/{name}/download", status_code=202)
def download_model(name: str, request: Request):
    reviewer = require_admin(request)
    with _START_LOCK:
        entry = _entry_or_404(name, "model_download", reviewer)
        download_job = downloader.job_status(name)
        if (entry.get("status") in (registry_module.STATUS_STARTING, registry_module.STATUS_DOWNLOADING)
                or download_job.get("status") == "downloading"):
            _audit_failure(reviewer, "model_download", {"name": name}, extra={"reason": "active_operation"})
            raise HTTPException(status_code=409, detail=f"model '{name}' already has an active operation")
        try:
            downloader.start_download(name, model_registry)
        except RuntimeError as exc:
            _audit_failure(reviewer, "model_download", {"name": name},
                           extra={"reason": "start_failed", "error": str(exc)[:300]})
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    _audit(reviewer, "model_download", {"name": name})
    log_event(_LIFECYCLE_LOG, "model_download_started", f"download started for '{name}'",
              fields={"name": name})
    return {"status": "downloading", "download": downloader.job_status(name)}


def _start_job(name: str) -> None:
    with _START_LOCK:
        job = _START_JOBS.get(name) or {}
        started_at = job.get("started_at", time.time())
        _START_JOBS[name] = {"status": "starting", "started_at": started_at,
                             "error": None, "reviewer": job.get("reviewer")}
    try:
        current = model_registry.get(name) or {}
        server = _server_for(current)
        # Only one model can claim a host GPU/port window during load at a time.
        with _ENGINE_START_LOCK:
            entry = server.start(name, model_registry)
        sync_result = litellm_sync.sync(model_registry)
        restart = sync_result.get("restart") or {}
        if restart and (restart.get("exit_code") != 0 or restart.get("error")):
            # Bound the captured LiteLLM output before it enters the exception
            # message, the registry last_error, the audit event and the log.
            output = str(restart.get("output", "unknown error"))[:300]
            raise RuntimeError(f"LiteLLM restart failed: {output}")
        with _START_LOCK:
            _START_JOBS[name] = {
                "status": "running", "started_at": _START_JOBS[name]["started_at"],
                "error": None, "port": (entry.get("server") or {}).get("port"),
                "litellm": sync_result,
            }
        log_event(_LIFECYCLE_LOG, "model_start_finished", f"model '{name}' started",
                  fields={"name": name, "port": (entry.get("server") or {}).get("port"),
                          "litellm_restart": bool(restart),
                          "duration_seconds": round(time.time() - _START_JOBS[name]["started_at"], 2)})
    except Exception as exc:
        current = model_registry.get(name) or {}
        cleanup_error = None
        if (current.get("server") or {}).get("status") == "running":
            try:
                _server_for(current).stop(name, model_registry)
            except Exception as cleanup_exc:
                cleanup_error = cleanup_exc
            else:
                try:
                    litellm_sync.sync(model_registry, restart=False)
                except Exception as sync_cleanup_exc:
                    cleanup_error = sync_cleanup_exc
        failure = f"{exc}; cleanup not confirmed: {cleanup_error}" if cleanup_error else str(exc)
        _audit_failure((_START_JOBS.get(name) or {}).get("reviewer"), "model_start", {"name": name},
                       extra={"reason": "start_failed", "error": str(exc)[:300],
                              "cleanup_not_confirmed": bool(cleanup_error)})
        update = {"status": registry_module.STATUS_ERROR, "last_error": failure}
        if cleanup_error and (current.get("server") or {}).get("pid"):
            update["server"] = {**current["server"], "status": "error"}
        model_registry.update(name, **update)
        with _START_LOCK:
            _START_JOBS[name] = {"status": "error", "started_at": time.time(), "error": failure}


@router.post("/api/v1/models/{name}/start", status_code=202)
def start_model(name: str, request: Request):
    reviewer = require_admin(request)
    entry = _public(_entry_or_404(name, "model_start", reviewer))
    if _is_engine_running(entry):
        _audit_failure(reviewer, "model_start", {"name": name}, extra={"reason": "already_running"})
        raise HTTPException(status_code=409, detail=f"model '{name}' is already running")
    if entry.get("status") not in (registry_module.STATUS_DOWNLOADED, registry_module.STATUS_STOPPED,
                                   registry_module.STATUS_ERROR):
        _audit_failure(reviewer, "model_start", {"name": name}, extra={"reason": "not_downloaded"})
        raise HTTPException(status_code=409, detail=f"model '{name}' is not downloaded (status={entry.get('status')})")
    with _START_LOCK:
        job = _START_JOBS.get(name)
        current = model_registry.get(name) or {}
        download_job = downloader.job_status(name)
        if ((job and job.get("status") == "starting")
                or download_job.get("status") == "downloading"
                or current.get("status") in (registry_module.STATUS_DOWNLOADING, registry_module.STATUS_STARTING)):
            _audit_failure(reviewer, "model_start", {"name": name}, extra={"reason": "active_operation"})
            raise HTTPException(status_code=409, detail=f"model '{name}' already has an active lifecycle operation")
        if current.get("status") not in (registry_module.STATUS_DOWNLOADED, registry_module.STATUS_STOPPED,
                                           registry_module.STATUS_ERROR):
            _audit_failure(reviewer, "model_start", {"name": name}, extra={"reason": "not_downloaded"})
            raise HTTPException(status_code=409, detail=f"model '{name}' is not downloaded (status={current.get('status')})")
        if _is_engine_running(current):
            _audit_failure(reviewer, "model_start", {"name": name}, extra={"reason": "already_running"})
            raise HTTPException(status_code=409, detail=f"model '{name}' is already running")
        _START_JOBS[name] = {"status": "starting", "started_at": time.time(), "error": None, "reviewer": reviewer}
    _audit(reviewer, "model_start", {"name": name})
    log_event(_LIFECYCLE_LOG, "model_start_requested", f"start requested for '{name}'",
              fields={"name": name})
    try:
        threading.Thread(target=_start_job, args=(name,), daemon=True).start()
    except Exception:
        _audit_failure(reviewer, "model_start", {"name": name}, extra={"reason": "spawn_failed"})
        with _START_LOCK:
            _START_JOBS[name] = {"status": "error", "started_at": time.time(), "error": "Could not start model job"}
        raise
    return {"status": "starting", "name": name}


@router.post("/api/v1/models/{name}/stop")
def stop_model(name: str, request: Request):
    reviewer = require_admin(request)
    entry = _entry_or_404(name, "model_stop", reviewer)
    with _START_LOCK:
        job = _START_JOBS.get(name)
        if (job and job.get("status") == "starting") or entry.get("status") == registry_module.STATUS_STARTING:
            _audit_failure(reviewer, "model_stop", {"name": name}, extra={"reason": "starting"})
            raise HTTPException(status_code=409, detail=f"model '{name}' is starting")
    try:
        entry = _server_for(_entry_or_404(name)).stop(name, model_registry)
        sync_result = litellm_sync.sync(model_registry)
    except Exception as exc:
        _audit_failure(reviewer, "model_stop", {"name": name},
                       extra={"reason": "stop_failed", "error": str(exc)[:300]})
        raise
    _audit(reviewer, "model_stop", {"name": name})
    log_event(_LIFECYCLE_LOG, "model_stopped", f"model '{name}' stopped", fields={"name": name})
    return {"model": entry, "litellm": sync_result}


@router.post("/api/v1/models/{name}/restart", status_code=202)
def restart_model(name: str, request: Request):
    reviewer = require_admin(request)
    entry = _entry_or_404(name, "model_restart", reviewer)
    with _START_LOCK:
        job = _START_JOBS.get(name)
        if (job and job.get("status") == "starting") or entry.get("status") == registry_module.STATUS_STARTING:
            _audit_failure(reviewer, "model_restart", {"name": name}, extra={"reason": "starting"})
            raise HTTPException(status_code=409, detail=f"model '{name}' is starting")
    try:
        _server_for(_entry_or_404(name)).stop(name, model_registry)
    except Exception as exc:
        _audit_failure(reviewer, "model_restart", {"name": name},
                       extra={"reason": "stop_failed", "error": str(exc)[:300]})
        raise
    return start_model(name, request)


@router.delete("/api/v1/models/{name}")
def delete_model(name: str, request: Request, delete_files: int = 0):
    reviewer = require_admin(request)
    with _START_LOCK:
        job = _START_JOBS.get(name)
        entry = _entry_or_404(name, "model_delete", reviewer)
        download_job = downloader.job_status(name)
        if ((job and job.get("status") == "starting")
                or download_job.get("status") == "downloading"
                or entry.get("status") in (registry_module.STATUS_DOWNLOADING, registry_module.STATUS_STARTING)):
            _audit_failure(reviewer, "model_delete", {"name": name}, extra={"reason": "active_operation"})
            raise HTTPException(status_code=409, detail="wait for the active model operation before deleting it")
        if _is_engine_running(entry):
            _audit_failure(reviewer, "model_delete", {"name": name}, extra={"reason": "running"})
            raise HTTPException(status_code=409, detail="stop the model before deleting it")
        path = entry.get("path") or model_registry.path_for(name)
        if delete_files == 1 and os.path.isdir(path):
            expected = os.path.abspath(model_registry.models_dir)
            if os.path.commonpath([os.path.abspath(path), expected]) != expected:
                _audit_failure(reviewer, "model_delete", {"name": name}, extra={"reason": "path_escape"})
                raise HTTPException(status_code=400, detail="model path escapes the models directory")
            shutil.rmtree(path)
        model_registry.delete(name)
        litellm_sync.sync(model_registry)
    _audit(reviewer, "model_delete", {"name": name, "delete_files": bool(delete_files)})
    log_event(_LIFECYCLE_LOG, "model_deleted", f"model '{name}' deleted",
              fields={"name": name, "delete_files": bool(delete_files)})
    return {"status": "deleted", "name": name}


@router.get("/api/v1/models/{name}/logs")
def model_logs(name: str, request: Request, tail: int = 32768):
    require_admin(request)
    _entry_or_404(name)
    tail = max(1024, min(int(tail), 262144))
    return {"name": name, "output": _server_for(_entry_or_404(name)).tail_log(name, tail)}


# --- versioned rollout (staging -> canary -> prod) --------------------------

def _fleet_or_503():
    """Fail closed: promotion needs the durable control store, never a stub."""
    fleet = open_fleet_registry()
    if fleet is None:
        raise HTTPException(status_code=503, detail="fleet control store unavailable")
    return fleet


def _promotion_error(reviewer, action, name, exc):
    if isinstance(exc, ValueError) and "unknown" in str(exc):
        _audit_failure(reviewer, action, {"name": name}, extra={"reason": "unknown", "error": str(exc)[:300]})
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _audit_failure(reviewer, action, {"name": name}, extra={"reason": "promotion_failed", "error": str(exc)[:300]})
    raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/v1/models/{name}/versions", status_code=201)
async def register_model_version(name: str, request: Request):
    """Record a new build of a model in the versioned rollout lifecycle."""
    reviewer = require_admin(request)
    try:
        registry_module.validate_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    body = await request.json()
    if not isinstance(body, dict):
        _audit_failure(reviewer, "model_version_register", {"name": name}, extra={"reason": "invalid_body"})
        raise HTTPException(status_code=400, detail="a JSON object is required")
    try:
        version = body.get("version")
        hf_repo = body.get("hf_repo")
        if not version or not hf_repo:
            raise ValueError("version and hf_repo are required")
        record = model_registry.register_version(
            name,
            version,
            hf_repo,
            revision=body.get("revision"),
            engine=body.get("engine"),
            stage=body.get("stage") or registry_module.STAGE_STAGING,
        )
    except (ValueError, TypeError) as exc:
        _audit_failure(reviewer, "model_version_register", {"name": name},
                       extra={"reason": "validation", "error": str(exc)[:300]})
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(reviewer, "model_version_register",
           {"name": name, "version": record["version"], "stage": record["stage"]})
    log_event(_LIFECYCLE_LOG, "model_version_registered",
              f"version '{record['version']}' registered for '{name}'",
              fields={"name": name, "version": record["version"], "stage": record["stage"]})
    return {"model": name, "version": record}


@router.get("/api/v1/models/{name}/versions")
def list_model_versions(name: str, request: Request):
    require_admin(request)
    _entry_or_404(name)
    return {"model": name, "versions": model_registry.list_versions(name),
            "history": model_registry.promotion_history(name)}


@router.post("/api/v1/models/{name}/promote")
async def promote_model_version(name: str, request: Request):
    """Move a version along the rollout chain and sync the fleet policy.

    Body: {"version": "v2", "target_stage": "canary"|"prod"|..., "canary_percent": 10}.
    Promoting to canary writes `canary_version`/`canary_traffic_percent` into
    the model's fleet desired-state policy (read-modify-write); promoting to
    prod clears them and records the prod version.
    """
    reviewer = require_admin(request)
    try:
        registry_module.validate_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    body = await request.json()
    if not isinstance(body, dict):
        _audit_failure(reviewer, "model_promote", {"name": name}, extra={"reason": "invalid_body"})
        raise HTTPException(status_code=400, detail="a JSON object is required")
    version = body.get("version")
    target_stage = body.get("target_stage")
    if not version or not target_stage:
        _audit_failure(reviewer, "model_promote", {"name": name}, extra={"reason": "missing_fields"})
        raise HTTPException(status_code=400, detail="version and target_stage are required")
    canary_percent = body.get("canary_percent", 10)
    if target_stage == registry_module.STAGE_CANARY:
        try:
            registry_module.validate_canary_percent(canary_percent)
        except (ValueError, TypeError) as exc:
            _audit_failure(reviewer, "model_promote", {"name": name, "version": version},
                           extra={"reason": "validation", "error": str(exc)[:300]})
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        fleet = _fleet_or_503()
    except HTTPException:
        _audit_failure(reviewer, "model_promote", {"name": name, "version": version},
                       extra={"reason": "fleet_unavailable"})
        raise
    try:
        record = promotion_module.promote(
            name, version, target_stage,
            canary_percent=canary_percent,
            registry=model_registry, fleet=fleet,
        )
    except (ValueError, TypeError, RuntimeError) as exc:
        _promotion_error(reviewer, "model_promote", name, exc)
    _audit(reviewer, "model_promote",
           {"name": name, "version": version, "target_stage": record["stage"]})
    log_event(_LIFECYCLE_LOG, "model_promoted",
              f"version '{version}' of '{name}' -> {record['stage']}",
              fields={"name": name, "version": version, "target_stage": record["stage"]})
    return {"model": name, "version": record}


@router.post("/api/v1/models/{name}/rollback")
async def rollback_model(name: str, request: Request):
    """Restore the previous prod version (from the promotion history)."""
    reviewer = require_admin(request)
    try:
        registry_module.validate_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        fleet = _fleet_or_503()
    except HTTPException:
        _audit_failure(reviewer, "model_rollback", {"name": name}, extra={"reason": "fleet_unavailable"})
        raise
    try:
        record = promotion_module.rollback(name, registry=model_registry, fleet=fleet)
    except (ValueError, TypeError, RuntimeError) as exc:
        _promotion_error(reviewer, "model_rollback", name, exc)
    _audit(reviewer, "model_rollback", {"name": name, "version": record["version"]})
    log_event(_LIFECYCLE_LOG, "model_rollback",
              f"model '{name}' rolled back to '{record['version']}'",
              fields={"name": name, "version": record["version"]})
    return {"model": name, "version": record}


@router.post("/api/v1/models/{name}/canary")
async def set_canary_traffic(name: str, request: Request):
    """Adjust the traffic share of the canary version (percent 0-100).

    Body: {"version": "v2", "percent": 25}. The version must be in the canary
    stage; the fleet policy keeps every other field it already had.
    """
    reviewer = require_admin(request)
    try:
        registry_module.validate_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    body = await request.json()
    if not isinstance(body, dict):
        _audit_failure(reviewer, "model_canary", {"name": name}, extra={"reason": "invalid_body"})
        raise HTTPException(status_code=400, detail="a JSON object is required")
    version = body.get("version")
    percent = body.get("percent")
    if not version or percent is None:
        _audit_failure(reviewer, "model_canary", {"name": name}, extra={"reason": "missing_fields"})
        raise HTTPException(status_code=400, detail="version and percent are required")
    try:
        registry_module.validate_canary_percent(percent)
    except (ValueError, TypeError) as exc:
        _audit_failure(reviewer, "model_canary", {"name": name, "version": version},
                       extra={"reason": "validation", "error": str(exc)[:300]})
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        fleet = _fleet_or_503()
    except HTTPException:
        _audit_failure(reviewer, "model_canary", {"name": name, "version": version},
                       extra={"reason": "fleet_unavailable"})
        raise
    try:
        record = promotion_module.set_canary_traffic(
            name, version, percent, registry=model_registry, fleet=fleet)
    except (ValueError, TypeError, RuntimeError) as exc:
        _promotion_error(reviewer, "model_canary", name, exc)
    _audit(reviewer, "model_canary",
           {"name": name, "version": version, "percent": record["canary_percent"]})
    return {"model": name, "version": record}
