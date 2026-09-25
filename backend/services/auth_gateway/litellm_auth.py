#!/usr/bin/env python3
"""
LiteLLM Custom Authentication & Logging Integration.
Enforces per-user concurrency ceiling of 2 in-flight requests, 60 RPM, 150,000 TPM,
and daily budget of 2,000,000 tokens with midnight rollover and P1 emergency elevation.
"""
import os
import sys
import json
import logging
import asyncio
import uuid
from typing import Optional
from pathlib import Path

from fastapi import Request, HTTPException, status
from litellm.proxy._types import UserAPIKeyAuth
from litellm.integrations.custom_logger import CustomLogger

# Ensure imports resolve cleanly
CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from server import resolve_identity
from quota_manager import QuotaManager, QuotaExceededException
from backend.services.agent_tools.audit import log_audit_event

logger = logging.getLogger("auth_gateway.litellm_auth")
quota_mgr = QuotaManager()

async def sysadmin_custom_auth(request: Request, api_key: str) -> UserAPIKeyAuth:
    """
    Invoked by LiteLLM Proxy for incoming chat completions calls.
    Returns UserAPIKeyAuth object configuring user limits and activating
    LiteLLM's internal rate-limiting and concurrency semaphores.
    """
    # 1. Bearer token from the api_key argument, else from the header
    clean_key = (api_key or "").strip()
    if clean_key.startswith("Bearer "):
        clean_key = clean_key.split(" ", 1)[1].strip()
    if not clean_key:
        auth_header = request.headers.get("authorization", "").strip()
        if auth_header.startswith("Bearer "):
            clean_key = auth_header.split(" ", 1)[1].strip()

    # 2. Identity resolution is shared with ForwardAuth: file-provisioned keys in
    #    file mode, the durable PostgreSQL key store when
    #    SYSADMIN_CONTROL_STORE=postgres (so the two layers cannot disagree).
    try:
        user_id = resolve_identity(clean_key)
    except ConnectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Shared identity store unavailable",
        ) from exc

    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Valid sysadmin token or ForwardAuth identity required."
        )

    is_p1 = quota_mgr.is_p1_elevated(user_id)
    try:
        limits = quota_mgr.get_limits(user_id, is_p1)
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail="Shared quota state unavailable") from exc

    # LiteLLM has already parsed the request body before invoking custom auth.
    # Reserve a conservative daily token estimate atomically before model work.
    route = request.url.path.rstrip("/").lower()
    is_generation = route.endswith(("/chat/completions", "/completions", "/responses"))
    if is_generation:
        parsed = request.scope.get("parsed_body")
        body = parsed[1] if isinstance(parsed, tuple) and len(parsed) == 2 else None
        if not isinstance(body, dict):
            try:
                body = await request.json()
            except Exception as exc:
                raise HTTPException(status_code=400, detail="Invalid inference request body") from exc
        output_field = "max_output_tokens" if route.endswith("/responses") else "max_tokens"
        requested_output = body.get(
            output_field,
            body.get("max_completion_tokens", body.get("max_tokens", 512)),
        )
        if isinstance(requested_output, bool) or not isinstance(requested_output, int) or requested_output < 1:
            raise HTTPException(status_code=400, detail="max_tokens must be a positive integer")
        bounded_output = min(requested_output, 2048)
        body[output_field] = bounded_output
        if output_field != "max_output_tokens" and "max_completion_tokens" in body:
            body["max_completion_tokens"] = bounded_output

        input_payload = body.get("messages", body.get("input", body.get("prompt", "")))
        try:
            input_bytes = len(json.dumps(input_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Inference input must be JSON serializable") from exc
        if input_bytes + bounded_output > 32768:
            raise HTTPException(status_code=413, detail="Inference input exceeds the 32K-context request limit")
        estimated_tokens = input_bytes + bounded_output
        metadata = body.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            body["metadata"] = metadata
        reservation_id = uuid.uuid4().hex
        try:
            admission_day, _, _ = quota_mgr.reserve_daily_token_budget(
                user_id=user_id,
                reservation_id=reservation_id,
                estimated_tokens=estimated_tokens,
            )
        except QuotaExceededException as qe:
            raise HTTPException(status_code=429, detail=str(qe)) from qe
        except ConnectionError as exc:
            raise HTTPException(status_code=503, detail="Shared quota state unavailable") from exc
        metadata["quota_reservation_id"] = reservation_id
        metadata["quota_admission_day"] = admission_day
        # Keep LiteLLM's parsed representation and downstream body parser in sync.
        request.scope["parsed_body"] = (tuple(body.keys()), body)
        request._json = body
        request._body = json.dumps(body, ensure_ascii=True, separators=(",", ":")).encode("utf-8")

    # 5. Return UserAPIKeyAuth with enforced per-user limits
    return UserAPIKeyAuth(
        api_key=clean_key or f"auth-{user_id}",
        user_id=user_id,
        max_parallel_requests=limits["concurrency"],
        rpm_limit=limits["rpm"],
        tpm_limit=limits["tpm"],
        user_role="proxy_admin" if user_id == "sysadmin-admin" else "internal_user"
    )

class QuotaLoggingHandler(CustomLogger):
    """Logs token usage to Valkey daily ledger upon request completion."""

    @staticmethod
    def _resolve_user_id(kwargs, response_obj):
        user_id = kwargs.get("user") or kwargs.get("litellm_params", {}).get("metadata", {}).get("user_id")
        if not user_id and "user_api_key_dict" in kwargs:
            user_id = getattr(kwargs["user_api_key_dict"], "user_id", None)
        if not user_id and hasattr(response_obj, "user"):
            user_id = response_obj.user
        return user_id

    @staticmethod
    def _classify_failure(response_obj):
        """Map a LiteLLM failure object to a stable, non-sensitive error_type string."""
        try:
            status_code = getattr(response_obj, "status_code", None)
            if status_code is not None:
                return f"http_{status_code}"
        except Exception:
            pass
        cls = getattr(response_obj, "__class__", None)
        if cls is not None:
            return getattr(cls, "__name__", "unknown_error")
        return "unknown_error"

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            user_id = self._resolve_user_id(kwargs, response_obj)
            
            if user_id and hasattr(response_obj, "usage") and response_obj.usage:
                prompt_tokens = getattr(response_obj.usage, "prompt_tokens", 0) or 0
                completion_tokens = getattr(response_obj.usage, "completion_tokens", 0) or 0
                metadata = kwargs.get("litellm_params", {}).get("metadata") or {}
                reservation_id = metadata.get("quota_reservation_id")
                admission_day = metadata.get("quota_admission_day")
                if reservation_id and admission_day:
                    quota_mgr.settle_daily_token_reservation(
                        user_id,
                        reservation_id,
                        admission_day,
                        prompt_tokens,
                        completion_tokens,
                    )
                else:
                    # Compatibility for a completion admitted before reservations
                    # were deployed or for a non-standard LiteLLM callback.
                    quota_mgr.record_token_consumption(user_id, prompt_tokens, completion_tokens)
                audit_result = await asyncio.to_thread(
                    log_audit_event,
                    user_id=user_id,
                    session_id=str(metadata.get("session_id") or ""),
                    tool_name="litellm_completion",
                    tokens_prompt=prompt_tokens,
                    tokens_completion=completion_tokens,
                    extra={"response_id": str(getattr(response_obj, "id", ""))},
                )
                if not audit_result["logged"]:
                    logger.error("Token usage audit could not be persisted: %s", audit_result.get("error"))
        except Exception as e:
            logger.warning(f"Failed to record token consumption: {e}")

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        """Audit a failed/rejected completion and settle its reservation once.

        Mirrors the success path: an admitted completion that then fails still
        holds a daily-token reservation, so that reservation is settled (with
        whatever tokens actually surfaced, normally zero) exactly once rather
        than leaking capacity. The concurrency slot is owned by the agent
        runtime and is reclaimed there; this handler never touches it.
        """
        try:
            user_id = self._resolve_user_id(kwargs, response_obj)
            metadata = kwargs.get("litellm_params", {}).get("metadata") or {}
            reservation_id = metadata.get("quota_reservation_id")
            admission_day = metadata.get("quota_admission_day")

            if user_id:
                # Settle any outstanding reservation so a failed request does
                # not leak its reserved daily capacity. settle_* is exactly-once.
                if reservation_id and admission_day:
                    usage = getattr(response_obj, "usage", None)
                    prompt_tokens = (getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
                    completion_tokens = (getattr(usage, "completion_tokens", 0) or 0) if usage else 0
                    quota_mgr.settle_daily_token_reservation(
                        user_id,
                        reservation_id,
                        admission_day,
                        prompt_tokens,
                        completion_tokens,
                    )

                error_type = self._classify_failure(response_obj)
                audit_result = await asyncio.to_thread(
                    log_audit_event,
                    user_id=user_id,
                    session_id=str(metadata.get("session_id") or ""),
                    tool_name="litellm_completion_failure",
                    action="litellm_completion_failure",
                    exit_code=1,
                    extra={
                        "error_type": error_type,
                        "response_id": str(getattr(response_obj, "id", "")),
                    },
                )
                if not audit_result["logged"]:
                    logger.error("Completion failure audit could not be persisted: %s", audit_result.get("error"))
        except Exception as e:
            logger.warning(f"Failed to record completion failure: {e}")


proxy_handler_instance = QuotaLoggingHandler()
