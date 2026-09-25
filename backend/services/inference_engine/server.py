#!/usr/bin/env python3
"""
Lightweight Inference Engine (OpenAI-compatible)
Acts as a high-speed local mock for development / testing,
and seamlessly transparently proxies to production vLLM when UPSTREAM_VLLM_URL is set.
"""
import os
import sys
import time
import json
import logging
import asyncio
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, Response
from starlette.background import BackgroundTask
import httpx
import uvicorn

from services.logging_setup import RequestLoggingMiddleware, configure, get_logger, log_event

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

app = FastAPI(title="Sysadmin AI Platform Inference Engine", version="1.0.0")
app.add_middleware(RequestLoggingMiddleware, service="inference")
_LOG = get_logger("inference_engine.server")

UPSTREAM_VLLM_URL = os.getenv("UPSTREAM_VLLM_URL", "")


def _registry_running_models() -> List[Dict[str, Any]]:
    """Models whose registered server process identity is still live."""
    from services.model_manager import llamacpp_server, registry, vllm_server

    running = []
    for entry in registry.model_registry.all():
        if entry.get("status") != registry.STATUS_RUNNING:
            continue
        engine = registry.validate_engine(entry.get("engine", registry.ENGINE_VLLM))
        server = llamacpp_server if engine == registry.ENGINE_LLAMACPP else vllm_server
        if server is llamacpp_server:
            is_live = server.is_running(entry, registry.model_registry)
        else:
            is_live = server.is_running(entry)
        if is_live:
            running.append(entry)
        else:
            try:
                server.stop(entry["name"], registry.model_registry)
            except Exception:
                registry.model_registry.update(
                    entry["name"],
                    status=registry.STATUS_ERROR,
                    server={**(entry.get("server") or {}), "status": "error"},
                    last_error="Model server identity was stale and shutdown could not be confirmed",
                )
    return running


def _local_model_port(model: str) -> Optional[int]:
    for entry in _registry_running_models():
        if entry.get("name") == model:
            return (entry.get("server") or {}).get("port")
    return None


def _is_registry_model(model: str) -> bool:
    from services.model_manager.registry import model_registry
    return model_registry.get(model) is not None

MODELS = [
    {
        "id": "fast-model",
        "object": "model",
        "created": int(time.time()),
        "owned_by": "qwen",
        "description": "Qwen/Qwen2.5-Coder-14B-Instruct (Fast Triage & Syntax)"
    },
    {
        "id": "heavy-model",
        "object": "model",
        "created": int(time.time()),
        "owned_by": "qwen",
        "description": "Qwen/Qwen2.5-Coder-32B-Instruct (Deep Diagnosis & Architecture)"
    },
    {
        "id": "openai/Qwen/Qwen2.5-Coder-14B-Instruct",
        "object": "model",
        "created": int(time.time()),
        "owned_by": "qwen"
    },
    {
        "id": "openai/Qwen/Qwen2.5-Coder-32B-Instruct",
        "object": "model",
        "created": int(time.time()),
        "owned_by": "qwen"
    }
]

@app.get("/health")
async def health():
    return {"status": "healthy", "upstream_vllm": UPSTREAM_VLLM_URL or "local-simulated", "timestamp": time.time()}

@app.get("/v1/models")
async def list_models():
    try:
        entries = list(MODELS)
        known = {entry["id"] for entry in entries}
        for entry in _registry_running_models():
            name = entry.get("name")
            if name and name not in known:
                engine = entry.get("engine", "vllm")
                entries.append({
                    "id": name, "object": "model", "created": int(time.time()),
                    "owned_by": "local-llamacpp" if engine == "llamacpp" else "local-vllm",
                })
        return {"object": "list", "data": entries}
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Local model registry unavailable") from exc

def simulate_chat_completion(messages: list, model: str = "fast-model") -> str:
    """Generates simulated model completions for dev/test without physical GPUs."""
    last_msg = messages[-1].get("content", "") if messages else "No prompt"
    is_react = any("REACT REASONING PROTOCOL:" in m.get("content", "") for m in messages)
    has_observation = any("Observation:" in m.get("content", "") for m in messages if m.get("role") != "system")

    if is_react:
        user_prompt = ""
        for m in messages:
            if m.get("role") == "user" and not m.get("content", "").startswith("Observation:"):
                user_prompt = m.get("content", "")
        low_p = user_prompt.lower()

        if "restart" in low_p or "systemctl" in low_p:
            if not has_observation:
                return (
                    "Thought: The user requested a service restart. I will invoke sandboxed_bash to execute systemctl restart.\n"
                    "Action: sandboxed_bash\n"
                    "Action Input: {\"command\": \"systemctl restart nginx\"}"
                )
            else:
                return "Thought: Command executed.\nFinal Answer: The service has been successfully restarted."
        elif "nginx" in low_p or "502" in low_p or "log" in low_p:
            if not has_observation:
                return (
                    "Thought: I should search the Nginx error log for connection failures.\n"
                    "Action: search_log_stream\n"
                    "Action Input: {\"target\": \"./backend/data/logs/nginx_error.log\", \"pattern\": \"connect\\\\(\\\\)\", \"max_matches\": 10}"
                )
            else:
                return (
                    "Thought: The log output shows connection refused on 127.0.0.1:9000.\n"
                    "Final Answer: Nginx is returning 502 Bad Gateway because the upstream FastCGI application on 127.0.0.1:9000 is refusing connections (111: Connection refused). The PHP-FPM service is down."
                )
        elif "runbook" in low_p:
            if not has_observation:
                return (
                    "Thought: I should look up the recovery runbook.\n"
                    "Action: doc_runbook_reader\n"
                    "Action Input: {\"runbook_path\": \"./backend/data/runbooks/nginx_recovery.md\", \"section_title\": \"Diagnostic Rapide\"}"
                )
            else:
                return (
                    "Thought: I found the recovery section.\n"
                    "Final Answer: According to the operational runbook, verify Nginx syntax using 'nginx -t' before applying changes."
                )
        elif "diff" in low_p or "lint" in low_p or "config" in low_p:
            if not has_observation:
                return (
                    "Thought: I should check the proposed configuration diff.\n"
                    "Action: config_lint_and_diff\n"
                    "Action Input: {\"target_file\": \"./backend/data/workspaces/sysadmin-01/app.json\", \"proposed_content\": \"{\\\"service\\\": \\\"nginx\\\", \\\"status\\\": \\\"active\\\"}\"}"
                )
            else:
                return (
                    "Thought: Config diff validated.\n"
                    "Final Answer: Proposed configuration is syntactically valid."
                )
        else:
            return f"Thought: I have sufficient information to answer the request.\nFinal Answer: Sysadmin operations assistant ready. Received instruction: '{user_prompt}'."
    else:
        if "nginx" in last_msg.lower():
            return "Nginx service diagnostic: checked /var/log/nginx/error.log. Configuration valid. No syntax anomalies detected."
        elif "diff" in last_msg.lower():
            return "Generated unified diff for configuration update. Staged changes require sysadmin human-in-the-loop approval."
        return f"[Inference Engine - {model}]\nAnalysed request: '{last_msg[:80]}'.\nSysadmin operations healthy. Confinement active."

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    model = body.get("model", "fast-model")
    messages = body.get("messages", [])
    stream = body.get("stream", False)

    # 1. Route a locally-managed model to its own vLLM instance, else proxy upstream.
    try:
        local_port = _local_model_port(model)
        registered = _is_registry_model(model)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Local model registry unavailable") from exc
    if local_port is None and registered:
        raise HTTPException(status_code=503, detail=f"Model '{model}' is registered but not running")

    upstream_base = f"http://127.0.0.1:{local_port}/v1" if local_port else (
        f"{UPSTREAM_VLLM_URL.rstrip('/')}/v1" if UPSTREAM_VLLM_URL else ""
    )
    if upstream_base:
        # Keep both response and client alive until the downstream stream closes.
        started = time.monotonic()
        client = httpx.AsyncClient(timeout=120.0)
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in ["host", "content-length"]}
        try:
            upstream_req = client.build_request(
                "POST", f"{upstream_base}/chat/completions", json=body, headers=headers)
            upstream_resp = await client.send(upstream_req, stream=True)
        except Exception as exc:
            await client.aclose()
            log_event(_LOG, "completion", "upstream connect failed",
                      fields={"model": model, "stream": stream, "error_type": type(exc).__name__,
                              "duration_ms": round((time.monotonic() - started) * 1000, 2)},
                      level=logging.WARNING)
            raise HTTPException(status_code=502, detail=f"Error connecting to vLLM backend: {exc}") from exc

        async def close_upstream():
            # Both the relay `finally` (mid-stream upstream drop / downstream
            # disconnect) and the BackgroundTask (headers sent, iterator never
            # consumed) may call this. httpx `aclose` is idempotent, so the
            # overlap is a harmless no-op; keep both owners.
            await upstream_resp.aclose()
            await client.aclose()

        if stream and upstream_resp.is_success:
            async def relay():
                relayed = 0
                try:
                    async for chunk in upstream_resp.aiter_bytes():
                        relayed += len(chunk)
                        yield chunk
                    log_event(_LOG, "completion", "streamed completion relayed",
                              fields={"model": model, "stream": True,
                                      "upstream_status": upstream_resp.status_code,
                                      "bytes": relayed,
                                      "duration_ms": round((time.monotonic() - started) * 1000, 2)})
                finally:
                    await close_upstream()
            return StreamingResponse(relay(), status_code=upstream_resp.status_code,
                                     media_type="text/event-stream",
                                     background=BackgroundTask(close_upstream))
        try:
            await upstream_resp.aread()
            fields = {"model": model, "stream": stream,
                      "upstream_status": upstream_resp.status_code,
                      "duration_ms": round((time.monotonic() - started) * 1000, 2)}
            if upstream_resp.status_code == 200:
                try:
                    usage = upstream_resp.json().get("usage") or {}
                    for key in ("prompt_tokens", "completion_tokens"):
                        value = usage.get(key)
                        if value is not None:
                            fields[key] = value
                except Exception:
                    pass  # non-JSON upstream body: counts stay absent, never guessed
            else:
                fields["error"] = True
            log_event(_LOG, "completion", "completion served", fields=fields)
            # Preserve upstream validation errors instead of disguising them as SSE 200.
            return Response(upstream_resp.content, status_code=upstream_resp.status_code,
                            media_type=upstream_resp.headers.get("content-type", "application/json"))
        finally:
            await close_upstream()

    # 2. Local Simulated Inference (Zero GPU overhead for dev/test)
    simulated_started = time.monotonic()
    req_id = f"chatcmpl-{int(time.time()*1000)}"
    reply_text = simulate_chat_completion(messages, model)

    prompt_tokens = sum(len(m.get("content", "").split()) for m in messages) + 10
    completion_tokens = len(reply_text.split()) + 5
    simulated_fields = {"model": model, "stream": stream, "simulated": True,
                        "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}

    if stream:
        async def event_generator():
            words = reply_text.split(" ")
            for i, word in enumerate(words):
                chunk = {
                    "id": req_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": word + (" " if i < len(words) - 1 else "")},
                            "finish_reason": None
                        }
                    ]
                }
                yield f"data: {json.dumps(chunk)}\n\n"
                await asyncio.sleep(0.02)  # Simulate streaming latency
            
            # Final chunk
            end_chunk = {
                "id": req_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens
                }
            }
            yield f"data: {json.dumps(end_chunk)}\n\n"
            yield "data: [DONE]\n\n"
            log_event(_LOG, "completion", "simulated completion streamed",
                      fields={**simulated_fields,
                              "duration_ms": round((time.monotonic() - simulated_started) * 1000, 2)})

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    log_event(_LOG, "completion", "simulated completion served",
              fields={**simulated_fields,
                      "duration_ms": round((time.monotonic() - simulated_started) * 1000, 2)})
    return {
        "id": req_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": reply_text
                },
                "finish_reason": "stop"
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens
        }
    }

if __name__ == "__main__":
    configure("inference")
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    log_event(_LOG, "service_start", f"inference engine listening on 127.0.0.1:{port}",
              fields={"port": port, "host": "127.0.0.1",
                      "upstream_vllm": bool(UPSTREAM_VLLM_URL)})
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info", access_log=False)  # requests are logged as JSON by the middleware
