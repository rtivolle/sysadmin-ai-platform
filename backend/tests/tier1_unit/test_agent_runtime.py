"""
Tier 1 Unit Test: Server-Side ReAct Agent Runtime & Valkey Session Store.
Validates parser self-healing, multi-user session isolation, ReAct execution loop,
and HITL approval gate interception.
"""
import os
import sys
import time
import pytest
import httpx
import importlib

from backend.services.agent_runtime.models import (
    AgentChatRequest,
    AgentChatResponse,
    SessionState
)
from backend.services.agent_runtime.parser import parse_model_output, clean_and_repair_json
from backend.services.agent_runtime.session_store import SessionStore
from backend.services.agent_runtime.react_loop import run_react_agent
from backend.services.agent_tools.server import app
from backend.services.inference_engine.server import simulate_chat_completion

AVAILABLE_TOOLS = [
    "search_log_stream",
    "config_lint_and_diff",
    "doc_runbook_reader",
    "sandboxed_bash"
]


@pytest.fixture
def mock_inference(monkeypatch):
    """Exercise the agent loop without a running inference gateway."""
    async def completion(messages, model, user_id):
        return simulate_chat_completion(messages, model)

    monkeypatch.setattr("backend.services.agent_runtime.react_loop.call_llm", completion)
    monkeypatch.setattr("services.agent_runtime.react_loop.call_llm", completion)

def test_parser_standard_react():
    """Verify parsing standard Thought / Action / Action Input text."""
    raw = (
        "Thought: I should search the nginx error log for connection failures.\n"
        "Action: search_log_stream\n"
        "Action Input: {\"target\": \"./backend/data/logs/nginx_error.log\", \"pattern\": \"error\", \"max_matches\": 10}"
    )
    parsed = parse_model_output(raw, AVAILABLE_TOOLS)
    assert parsed.type == "action"
    assert parsed.tool_name == "search_log_stream"
    assert parsed.tool_args["pattern"] == "error"
    assert parsed.tool_args["max_matches"] == 10
    assert "search the nginx error log" in parsed.thought

def test_parser_markdown_code_fences():
    """Verify stripping markdown code fences (```json ... ```) in Action Input."""
    raw = (
        "Action: doc_runbook_reader\n"
        "Action Input: ```json\n"
        "{\n  \"runbook_path\": \"./backend/data/runbooks/nginx_recovery.md\",\n  \"section_title\": \"Diagnostic Rapide\"\n}\n"
        "```"
    )
    parsed = parse_model_output(raw, AVAILABLE_TOOLS)
    assert parsed.type == "action"
    assert parsed.tool_name == "doc_runbook_reader"
    assert parsed.tool_args["section_title"] == "Diagnostic Rapide"

def test_parser_malformed_json_recovery():
    """Verify repair of unescaped regex backslashes and trailing commas."""
    # Unescaped regex backslash + trailing comma
    raw = (
        "Action: search_log_stream\n"
        "Action Input: {\"target\": \"nginx.log\", \"pattern\": \"connect\\(\\)\", \"max_matches\": 10, }"
    )
    parsed = parse_model_output(raw, AVAILABLE_TOOLS)
    assert parsed.type == "action"
    assert parsed.tool_args["max_matches"] == 10
    assert "connect" in parsed.tool_args["pattern"]

def test_parser_final_answer():
    """Verify clean extraction of Final Answer response."""
    raw = (
        "Thought: I have sufficient information to answer the request.\n"
        "Final Answer: Nginx is returning 502 Bad Gateway because upstream PHP-FPM is stopped."
    )
    parsed = parse_model_output(raw, AVAILABLE_TOOLS)
    assert parsed.type == "final_answer"
    assert "502 Bad Gateway" in parsed.final_answer
    assert "PHP-FPM is stopped" in parsed.final_answer

@pytest.mark.asyncio
async def test_session_store_create_and_get():
    """Verify session creation and retrieval with turn counts."""
    store = SessionStore()
    sess = await store.create_or_get_session(user_id="sysadmin-01", session_id="test-sess-unit-01")
    assert sess.session_id == "test-sess-unit-01"
    assert sess.user_id == "sysadmin-01"

    retrieved = await store.get_session(user_id="sysadmin-01", session_id="test-sess-unit-01")
    assert retrieved is not None
    assert retrieved.session_id == "test-sess-unit-01"

@pytest.mark.asyncio
async def test_cross_user_session_isolation():
    """Verify User B cannot access User A's session store (returns None)."""
    store = SessionStore()
    sess = await store.create_or_get_session(user_id="sysadmin-01", session_id="private-sess-01")
    assert sess is not None

    # sysadmin-02 attempts to retrieve sysadmin-01's session
    cross_access = await store.get_session(user_id="sysadmin-02", session_id="private-sess-01")
    assert cross_access is None

@pytest.mark.asyncio
async def test_react_loop_diagnose_log(mock_inference):
    """Verify ReAct reasoning loop invokes search_log_stream and synthesizes answer."""
    store = SessionStore()
    session = await store.create_or_get_session(user_id="sysadmin-01", session_id="test-loop-01")
    req = AgentChatRequest(prompt="Investigate 502 errors in nginx log")

    resp = await run_react_agent(req, "sysadmin-01", session, store)
    assert resp.session_id == "test-loop-01"
    assert resp.approval_required is False
    assert len(resp.tools_executed) >= 1
    assert resp.tools_executed[0].tool == "search_log_stream"
    assert "502" in resp.response or "Connection refused" in resp.response or "FastCGI" in resp.response

@pytest.mark.asyncio
async def test_react_loop_mutating_hitl_approval(mock_inference):
    """Verify mutating command halts loop and returns approval_required: true."""
    store = SessionStore()
    session = await store.create_or_get_session(user_id="sysadmin-01", session_id="test-loop-02")
    req = AgentChatRequest(prompt="Restart the nginx service")

    resp = await run_react_agent(req, "sysadmin-01", session, store)
    assert resp.approval_required is True
    assert resp.approval_id is not None
    assert "systemctl restart nginx" in resp.command
    assert "Human-in-the-Loop authorization" in resp.response

@pytest.mark.asyncio
async def test_agent_chat_api_endpoint(mock_inference, monkeypatch):
    """Verify POST /api/v1/agent/chat endpoint via Starlette/FastAPI ASGI transport."""
    monkeypatch.setattr(importlib.import_module("services.agent_runtime.router"), "authenticate_request", lambda request: ("sysadmin-01", "test"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "session_id": "api-sess-01",
            "prompt": "Investigate 502 errors in nginx log"
        }
        headers = {"X-Forwarded-User": "sysadmin-01"}
        resp = await client.post("/api/v1/agent/chat", json=payload, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "api-sess-01"
        assert "response" in data
        assert isinstance(data["tools_executed"], list)
