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
import asyncio
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
import httpx
import uvicorn

app = FastAPI(title="Sysadmin AI Platform Inference Engine", version="1.0.0")

UPSTREAM_VLLM_URL = os.getenv("UPSTREAM_VLLM_URL", "")

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
    return {"object": "list", "data": MODELS}

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

    # 1. Forward to upstream vLLM if configured
    if UPSTREAM_VLLM_URL:
        async with httpx.AsyncClient(timeout=120.0) as client:
            headers = {k: v for k, v in request.headers.items() if k.lower() not in ["host", "content-length"]}
            try:
                upstream_resp = await client.post(
                    f"{UPSTREAM_VLLM_URL.rstrip('/')}/v1/chat/completions",
                    json=body,
                    headers=headers
                )
                if stream:
                    return StreamingResponse(upstream_resp.aiter_raw(), media_type="text/event-stream")
                return JSONResponse(upstream_resp.json(), status_code=upstream_resp.status_code)
            except Exception as e:
                raise HTTPException(status_code=502, detail=f"Error connecting to upstream vLLM: {str(e)}")

    # 2. Local Simulated Inference (Zero GPU overhead for dev/test)
    req_id = f"chatcmpl-{int(time.time()*1000)}"
    reply_text = simulate_chat_completion(messages, model)

    prompt_tokens = sum(len(m.get("content", "").split()) for m in messages) + 10
    completion_tokens = len(reply_text.split()) + 5

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

        return StreamingResponse(event_generator(), media_type="text/event-stream")

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
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
