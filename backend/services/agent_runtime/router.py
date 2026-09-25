import sys
import uuid
import asyncio
import logging
import time
from typing import Optional, Union, Any
from fastapi import APIRouter, Request, HTTPException, status
from fastapi.responses import StreamingResponse
from .models import (
    AgentChatRequest,
    AgentChatResponse,
    AgentCancelRequest,
    AgentCancelResponse
)
from .session_store import SessionStore
from .react_loop import run_react_agent, run_react_agent_stream
from .cancellation import registry
from services.auth_gateway.quota_manager import quota_mgr, QuotaExceededException
from services.agent_tools.audit import log_audit_event

logger = logging.getLogger("agent_runtime.router")

router = APIRouter()
session_store = SessionStore()


async def _audit_agent_cancel(user_id, session_id, request_id, cause):
    """Best-effort audit of an in-flight request cancellation.

    ``cause`` is one of ``user`` (via /agent/cancel), ``disconnect`` (SSE
    client disconnect) or ``lease_lost`` (quota lease ownership lost). Emission
    never changes the cancellation outcome.
    """
    try:
        await asyncio.to_thread(
            log_audit_event,
            user_id=user_id,
            session_id=session_id or "",
            tool_name="agent_cancel",
            action="agent_cancel",
            exit_code=0,
            extra={"cause": cause, "request_id": request_id or ""},
        )
    except Exception as exc:
        print(f"[!] Audit emission failed for agent_cancel: {exc}", file=sys.stderr)


async def _audit_disconnect_if_needed(record, request, user_id, session_id, request_id) -> bool:
    """Report an SSE client disconnect and return True when the client is gone.

    A user-initiated cancel is already reported by /agent/cancel (cause=user),
    so a set ``cancel_event`` must not be double-reported as a disconnect.
    """
    if record.cancel_event.is_set():
        return True
    if request is not None and await request.is_disconnected():
        await _audit_agent_cancel(user_id, session_id, request_id, "disconnect")
        return True
    return False

def _default_authenticate_request(request: Request):
    """Delegate to auth_gateway server unless monkeypatched."""
    import services.auth_gateway.server as srv
    return srv.authenticate_request(request)

authenticate_request = _default_authenticate_request

async def _maintain_quota_lease(lease_id: str, stop_event: asyncio.Event, active_task: asyncio.Task,
                                user_id: Optional[str] = None, session_id: Optional[str] = None,
                                request_id: Optional[str] = None, renew_interval: float = 30.0) -> None:
    """Renew a live request lease and stop work if ownership is lost."""
    last_success = time.monotonic()
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=renew_interval)
            return
        except asyncio.TimeoutError:
            pass

        try:
            renewed = await asyncio.to_thread(quota_mgr.renew_concurrency_slot, lease_id, 120)
        except ConnectionError:
            logger.warning("Quota lease renewal unavailable for %s", lease_id)
            if time.monotonic() - last_success >= 110:
                await _audit_agent_cancel(user_id or "", session_id, request_id, "lease_lost")
                active_task.cancel()
                return
            continue

        if not renewed:
            logger.error("Quota lease ownership lost for %s; cancelling request", lease_id)
            await _audit_agent_cancel(user_id or "", session_id, request_id, "lease_lost")
            active_task.cancel()
            return
        last_success = time.monotonic()

def get_current_user(request: Request) -> str:
    """Resolve user from credentials even when accessed without Traefik."""
    for mod_name in ("services.agent_runtime.router", "backend.services.agent_runtime.router", __name__):
        mod = sys.modules.get(mod_name)
        if mod and hasattr(mod, "authenticate_request"):
            func = getattr(mod, "authenticate_request")
            if getattr(func, "__name__", "") != "_default_authenticate_request":
                user, _ = func(request)
                return user

    import services.auth_gateway.server as srv
    user, _ = srv.authenticate_request(request)
    return user

@router.post("/chat", response_model=None)
async def chat_endpoint(request_body: AgentChatRequest, request: Request):
    user_id = get_current_user(request)
    req_id = request_body.request_id or f"req-{uuid.uuid4().hex[:12]}"

    # Enforce in-flight concurrency limit (ceiling of 2 per user)
    try:
        lease_id = quota_mgr.acquire_concurrency_slot(user_id)
    except QuotaExceededException as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Concurrency ceiling exceeded ({exc.current}/{exc.limit} in-flight calls active)."
        ) from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail="Shared quota state unavailable") from exc

    # Setup can fail before the response stream starts, so release the lease
    # and registry entry for every setup exception.
    registered = False
    try:
        record = await registry.register(
            request_id=req_id,
            user_id=user_id,
            session_id=request_body.session_id
        )
        registered = True
        session = await session_store.create_or_get_session(
            user_id=user_id,
            session_id=request_body.session_id
        )
    except BaseException as exc:
        if registered:
            await registry.unregister(req_id)
        quota_mgr.release_concurrency_slot(lease_id)
        if isinstance(exc, ValueError) and "request_id" in str(exc):
            raise HTTPException(status_code=409, detail="An active request already uses this request_id") from exc
        raise

    # Branch on SSE streaming vs standard JSON
    if request_body.stream or request.headers.get("accept") == "text/event-stream":
        async def sse_stream_wrapper():
            current_task = asyncio.current_task()
            stop_renewal = asyncio.Event()
            lease_task = asyncio.create_task(
                _maintain_quota_lease(lease_id, stop_renewal, current_task,
                                      user_id=user_id, session_id=request_body.session_id, request_id=req_id)
            ) if current_task else None
            try:
                if current_task:
                    await registry.update_task(req_id, current_task)
                async for sse_chunk in run_react_agent_stream(
                    request=request_body,
                    user_id=user_id,
                    session=session,
                    session_store=session_store,
                    cancel_event=record.cancel_event,
                    http_request=request
                ):
                    if await _audit_disconnect_if_needed(record, request, user_id, request_body.session_id, req_id):
                        logger.info(f"Stream terminating early for req_id={req_id}")
                        break
                    yield sse_chunk
            except asyncio.CancelledError:
                logger.info(f"Streaming request {req_id} cancelled.")
                raise
            finally:
                stop_renewal.set()
                if lease_task:
                    lease_task.cancel()
                # Reclaim the lease and the registry entry before awaiting
                # anything. A client that disconnects mid-stream cancels this
                # generator, and the cancellation is re-delivered at the first
                # await inside cleanup; awaiting here would skip the release and
                # strand the user's in-flight slots until their TTL expires.
                quota_mgr.release_concurrency_slot(lease_id)
                registry.unregister_nowait(req_id)

        return StreamingResponse(sse_stream_wrapper(), media_type="text/event-stream")

    else:
        current_task = asyncio.current_task()
        stop_renewal = asyncio.Event()
        lease_task = asyncio.create_task(
            _maintain_quota_lease(lease_id, stop_renewal, current_task,
                                  user_id=user_id, session_id=request_body.session_id, request_id=req_id)
        ) if current_task else None
        try:
            if current_task:
                await registry.update_task(req_id, current_task)
            return await run_react_agent(request_body, user_id, session, session_store)
        except asyncio.CancelledError:
            logger.info(f"Request {req_id} cancelled.")
            raise
        finally:
            stop_renewal.set()
            if lease_task:
                lease_task.cancel()
            # Cancellation is re-delivered at the first await inside cleanup, so
            # reclaim the lease and the registry entry without awaiting.
            quota_mgr.release_concurrency_slot(lease_id)
            registry.unregister_nowait(req_id)

@router.post("/cancel", response_model=AgentCancelResponse)
async def cancel_endpoint(request_body: AgentCancelRequest, request: Request):
    user_id = get_current_user(request)
    success, cancelled_id, msg = await registry.cancel(
        user_id=user_id,
        request_id=request_body.request_id,
        session_id=request_body.session_id
    )
    if not success:
        if "Unauthorized" in msg:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=msg)
        return AgentCancelResponse(success=False, message=msg)
    await _audit_agent_cancel(user_id, request_body.session_id, cancelled_id, "user")
    return AgentCancelResponse(success=True, message=msg, cancelled_request_id=cancelled_id)

@router.get("/sessions")
async def list_sessions_endpoint(request: Request):
    user_id = get_current_user(request)
    sessions = await session_store.list_user_sessions(user_id)
    return {"user_id": user_id, "sessions": sessions}

@router.get("/sessions/{session_id}")
async def get_session_endpoint(session_id: str, request: Request):
    user_id = get_current_user(request)
    session = await session_store.get_session(user_id, session_id)
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    return session

@router.delete("/sessions/{session_id}")
async def delete_session_endpoint(session_id: str, request: Request):
    user_id = get_current_user(request)
    deleted = await session_store.delete_session(user_id, session_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    return {"success": True, "session_id": session_id}
