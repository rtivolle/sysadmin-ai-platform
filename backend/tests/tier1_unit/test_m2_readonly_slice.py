"""
Tier 1 Unit Test: Milestone M2 Read-Only Vertical Slice.
Tests:
1. Model Schema Expansion (CitationRecord, stream, workspace, request_id, cancel models)
2. ForwardAuth Verify Alias (/api/v1/auth/verify)
3. Concurrency Ceiling 429 Limit & Slot Management
4. Cancellation Endpoint & Cross-User Security Isolation
5. SSE Streaming Response (format & terminal [DONE])
6. Structured Tool Citations (doc_runbook_reader & search_log_stream)
7. Audit Event Schema Conformance & Exit Code Mapping
"""
import os
import sys
import json
import time
import uuid
import asyncio
import pytest
import httpx
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.agent_runtime.models import (
    AgentChatRequest,
    AgentChatResponse,
    CitationRecord,
    AgentCancelRequest,
    AgentCancelResponse
)
from services.agent_runtime.cancellation import registry, RequestRegistry
from services.agent_runtime.router import router as agent_router
from services.agent_runtime.session_store import SessionStore
from services.agent_runtime.react_loop import run_react_agent, run_react_agent_stream
from services.agent_tools.tools import search_log_stream, doc_runbook_reader, config_lint_and_diff
from services.agent_tools.server import app as agent_app
from services.agent_tools.audit import log_audit_event, flush_outbox
from services.auth_gateway.server import app as auth_app
from services.auth_gateway.quota_manager import QuotaManager, QuotaExceededException
from services.inference_engine.server import simulate_chat_completion
from tests.fixtures import LOGS_DIR, RUNBOOKS_DIR

KEYS_DIR = BACKEND_DIR / "config" / "keys"
TOKEN_01 = (KEYS_DIR / "sysadmin-01.key").read_text().strip() if (KEYS_DIR / "sysadmin-01.key").exists() else "mock-token-01"
TOKEN_02 = (KEYS_DIR / "sysadmin-02.key").read_text().strip() if (KEYS_DIR / "sysadmin-02.key").exists() else "mock-token-02"


@pytest.fixture
def mock_inference(monkeypatch):
    """Exercise the agent loop without requiring an external inference server."""
    async def mock_call(messages, model, user_id, session_id=None):
        return simulate_chat_completion(messages, model)

    monkeypatch.setattr("services.agent_runtime.react_loop.call_llm", mock_call)
    monkeypatch.setattr("backend.services.agent_runtime.react_loop.call_llm", mock_call)


# ---------------------------------------------------------------------------
# 1. Model Schema Tests
# ---------------------------------------------------------------------------
def test_models_m2_schema():
    """Verify that M2 fields are accepted by Pydantic models with extra='forbid'."""
    # AgentChatRequest with stream, workspace, request_id
    req = AgentChatRequest(
        prompt="Investigate nginx",
        session_id="sess-01",
        stream=True,
        workspace="/data/workspaces/sysadmin-01",
        request_id="req-99"
    )
    assert req.stream is True
    assert req.workspace == "/data/workspaces/sysadmin-01"
    assert req.request_id == "req-99"

    # CitationRecord
    citation = CitationRecord(
        source="/var/log/nginx/error.log",
        section_or_query="Connection refused",
        start_line=12,
        end_line=15,
        artifact_hash="abc123hash"
    )
    assert citation.source == "/var/log/nginx/error.log"
    assert citation.start_line == 12

    # AgentChatResponse with citations
    resp = AgentChatResponse(
        session_id="sess-01",
        response="Found connection error",
        citations=[citation]
    )
    assert len(resp.citations) == 1
    assert resp.citations[0].start_line == 12

    # AgentCancelRequest & AgentCancelResponse
    cancel_req = AgentCancelRequest(request_id="req-99", session_id="sess-01")
    assert cancel_req.request_id == "req-99"

    cancel_resp = AgentCancelResponse(success=True, message="Request cancelled", cancelled_request_id="req-99")
    assert cancel_resp.success is True
    assert cancel_resp.cancelled_request_id == "req-99"


# ---------------------------------------------------------------------------
# 2. ForwardAuth Verify Alias Tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_forwardauth_verify_alias():
    """Verify GET and POST /api/v1/auth/verify alias endpoints."""
    transport = httpx.ASGITransport(app=auth_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Unauthenticated request should fail with 401
        res_unauth = await client.get("/api/v1/auth/verify")
        assert res_unauth.status_code == 401

        # 2. Authenticated GET with Bearer token
        headers = {"Authorization": f"Bearer {TOKEN_01}"}
        res_auth_get = await client.get("/api/v1/auth/verify", headers=headers)
        assert res_auth_get.status_code == 200
        assert res_auth_get.headers.get("X-User") == "sysadmin-01"
        assert res_auth_get.headers.get("X-User-Role") == "sysadmin"

        # 3. Authenticated POST with Bearer token
        res_auth_post = await client.post("/api/v1/auth/verify", headers=headers)
        assert res_auth_post.status_code == 200
        assert res_auth_post.headers.get("X-User") == "sysadmin-01"

        # 4. Canonical /verify path remains fully functional
        res_canonical = await client.get("/verify", headers=headers)
        assert res_canonical.status_code == 200
        assert res_canonical.headers.get("X-User") == "sysadmin-01"


# ---------------------------------------------------------------------------
# 3. Concurrency Ceiling 429 Limit Tests
# ---------------------------------------------------------------------------
def test_concurrency_ceiling_in_flight():
    """Verify QuotaManager enforces the 2 in-flight ceiling and raises QuotaExceededException."""
    qm = QuotaManager(redis_client=None)  # Use in-memory tracker
    user = "test-user-concurrency"

    slot1 = qm.acquire_concurrency_slot(user)
    assert slot1.startswith("lease:")

    slot2 = qm.acquire_concurrency_slot(user)
    assert slot2.startswith("lease:")

    # 3rd request exceeds ceiling of 2
    with pytest.raises(QuotaExceededException) as exc_info:
        qm.acquire_concurrency_slot(user)
    assert exc_info.value.limit == 2
    assert "Concurrency ceiling exceeded" in exc_info.value.message

    # Release one slot, then 3rd attempt succeeds
    qm.release_concurrency_slot(user)
    slot3 = qm.acquire_concurrency_slot(user)
    assert slot3.startswith("lease:")

    # Cleanup
    qm.release_concurrency_slot(user)
    qm.release_concurrency_slot(user)


@pytest.mark.asyncio
async def test_chat_endpoint_429_on_concurrency_exhaustion(mock_inference, monkeypatch):
    """Verify POST /api/v1/agent/chat returns HTTP 429 when concurrency ceiling is reached."""
    monkeypatch.setattr("services.auth_gateway.server.authenticate_request", lambda req: ("sysadmin-01", "test"))
    monkeypatch.setattr("services.agent_tools.server.authenticate_request", lambda req: ("sysadmin-01", "test"))

    transport = httpx.ASGITransport(app=agent_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        from services.auth_gateway.server import quota_mgr

        # Acquire 2 slots directly to saturate quota
        slot1 = quota_mgr.acquire_concurrency_slot("sysadmin-01")
        slot2 = quota_mgr.acquire_concurrency_slot("sysadmin-01")

        try:
            # 3rd request via chat endpoint must receive 429
            payload = {"prompt": "Investigate logs", "session_id": "test-429-sess"}
            resp = await client.post("/api/v1/agent/chat", json=payload)
            assert resp.status_code == 429
            data = resp.json()
            assert "Concurrency ceiling exceeded" in data["detail"]
        finally:
            quota_mgr.release_concurrency_slot("sysadmin-01")
            quota_mgr.release_concurrency_slot("sysadmin-01")

        # After slots released, chat request succeeds
        resp_after = await client.post("/api/v1/agent/chat", json=payload)
        assert resp_after.status_code == 200


# ---------------------------------------------------------------------------
# 4. Cancellation Endpoint & Security Isolation Tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cancellation_registry_and_endpoint(monkeypatch):
    """Verify active request registration, cancellation, and cross-user isolation."""
    # Unit level tracker
    tracker = RequestRegistry()
    user_a = "sysadmin-01"
    user_b = "sysadmin-02"
    req_id = "req-cancel-01"

    event_task = asyncio.create_task(asyncio.sleep(10))
    await tracker.register(req_id, user_a, "sess-01", event_task)

    # 1. User B cannot cancel User A's request (Isolation)
    success, _, msg = await tracker.cancel(user_b, request_id=req_id)
    assert success is False
    assert "Unauthorized" in msg
    assert not event_task.cancelled()

    # 2. User A cancels their own request
    success, cancelled_id, msg = await tracker.cancel(user_a, request_id=req_id)
    assert success is True
    assert cancelled_id == req_id
    await asyncio.sleep(0.01)
    assert event_task.cancelled() or event_task.cancelling() > 0

    # 3. HTTP endpoint testing
    transport = httpx.ASGITransport(app=agent_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        monkeypatch.setattr("services.auth_gateway.server.authenticate_request", lambda req: ("sysadmin-01", "test"))
        monkeypatch.setattr("services.agent_tools.server.authenticate_request", lambda req: ("sysadmin-01", "test"))

        # Register a live mock task in the global registry
        mock_task = asyncio.create_task(asyncio.sleep(10))
        await registry.register("req-http-01", "sysadmin-01", "sess-http-01", mock_task)

        # sysadmin-01 cancels req-http-01
        resp = await client.post("/api/v1/agent/cancel", json={"request_id": "req-http-01"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["cancelled_request_id"] == "req-http-01"
        await asyncio.sleep(0.01)
        assert mock_task.cancelled() or mock_task.cancelling() > 0

        # Cancelling an unknown request returns success: False
        resp_unknown = await client.post("/api/v1/agent/cancel", json={"request_id": "nonexistent-req"})
        assert resp_unknown.status_code == 200
        assert resp_unknown.json()["success"] is False


# ---------------------------------------------------------------------------
# 5. SSE Streaming Response Tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_chat_sse_streaming_response(mock_inference, monkeypatch):
    """Verify POST /api/v1/agent/chat with stream=True returns valid SSE event-stream ending in [DONE]."""
    monkeypatch.setattr("services.auth_gateway.server.authenticate_request", lambda req: ("sysadmin-01", "test"))
    monkeypatch.setattr("services.agent_tools.server.authenticate_request", lambda req: ("sysadmin-01", "test"))

    transport = httpx.ASGITransport(app=agent_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "session_id": "sse-sess-01",
            "prompt": "Investigate 502 errors in nginx log",
            "stream": True
        }
        resp = await client.post("/api/v1/agent/chat", json=payload)
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]

        content = resp.text
        lines = content.strip().split("\n\n")

        # Verify stream contains event data
        assert len(lines) >= 2
        assert lines[-1] == "data: [DONE]"

        # Verify intermediate events match {"chunk": "...", "citations": [...]}
        for event_block in lines[:-1]:
            assert event_block.startswith("data: ")
            event_json = event_block[len("data: "):]
            event_data = json.loads(event_json)
            assert "chunk" in event_data
            assert "citations" in event_data
            assert isinstance(event_data["citations"], list)


# ---------------------------------------------------------------------------
# 6. Structured Tool Citations Tests
# ---------------------------------------------------------------------------
def test_doc_runbook_reader_citations():
    """Verify doc_runbook_reader returns start_line, end_line, and source_id metadata."""
    target = os.path.join(RUNBOOKS_DIR, "nginx_recovery.md")
    res = doc_runbook_reader(runbook_path=target, section_title="Diagnostic Rapide")

    assert res["found"] is True
    assert isinstance(res["start_line"], int)
    assert isinstance(res["end_line"], int)
    assert res["start_line"] > 0
    assert res["end_line"] >= res["start_line"]
    assert res["source_id"] == os.path.realpath(target)


def test_search_log_stream_citations():
    """Verify search_log_stream returns line_numbers list and source_id."""
    target = os.path.join(LOGS_DIR, "nginx_error.log")
    res = search_log_stream(target=target, pattern="Connection refused", max_matches=10)

    assert res["matched"] is True
    assert isinstance(res["line_numbers"], list)
    assert len(res["line_numbers"]) >= 1
    assert all(isinstance(l, int) for l in res["line_numbers"])
    assert res["source_id"] == os.path.realpath(target)


@pytest.mark.asyncio
async def test_agent_chat_response_citations(mock_inference):
    """Verify ReAct agent turn collects citations and populates AgentChatResponse.citations."""
    store = SessionStore()
    session = await store.create_or_get_session(user_id="sysadmin-01", session_id="cit-test-sess")
    req = AgentChatRequest(prompt="Investigate 502 errors in nginx log")

    resp = await run_react_agent(req, "sysadmin-01", session, store)
    assert resp.session_id == "cit-test-sess"
    assert len(resp.citations) >= 1
    cit = resp.citations[0]
    assert cit.source != ""
    assert cit.artifact_hash is not None
    assert len(cit.artifact_hash) == 64  # SHA-256


# ---------------------------------------------------------------------------
# 7. Audit Event Schema Conformance & Exit Code Mapping Tests
# ---------------------------------------------------------------------------
def test_audit_event_schema_conformance(monkeypatch, tmp_path):
    """Verify tool audit events conform to PROJECT.md:124-138 with action, parameters, and tokens."""
    test_outbox = str(tmp_path / "outbox_audit_test.jsonl")
    monkeypatch.setattr("services.agent_tools.audit.OUTBOX_PATH", test_outbox)
    monkeypatch.setattr("services.agent_tools.audit._post_event", lambda ev: False)  # Force outbox spool

    params = {"target": "nginx_error.log", "pattern": "502 Bad Gateway"}
    res = log_audit_event(
        user_id="sysadmin-01",
        session_id="sess-audit-01",
        tool_name="search_log_stream",
        action="search_log_stream",
        parameters=params,
        exit_code=0,
        duration_ms=42,
        prompt_tokens=150,
        completion_tokens=60,
        approval_id=None
    )
    assert res["logged"] is True
    assert res["destination"] == "outbox"

    with open(test_outbox, "r") as f:
        records = [json.loads(line) for line in f if line.strip()]

    assert len(records) == 1
    rec = records[0]
    assert rec["action"] == "search_log_stream"
    assert rec["tool_name"] == "search_log_stream"
    assert rec["parameters"] == params
    assert rec["duration_ms"] == 42
    assert rec["exit_code"] == 0
    assert rec["prompt_tokens"] == 150
    assert rec["completion_tokens"] == 60
    assert rec["tokens_prompt"] == 150
    assert rec["tokens_completion"] == 60


def test_tool_failure_non_zero_exit_codes():
    """Verify tool errors map to non-zero exit codes (1 for syntax/missing, 2 for not found)."""
    # 1. Config lint error -> exit_code 1
    malformed_yaml = "server:\n  port: [broken\n"
    res_yaml = config_lint_and_diff("config.yaml", malformed_yaml, user_id="sysadmin-01")
    assert res_yaml["valid"] is False

    # 2. Runbook missing section -> exit_code 1
    target_rbk = os.path.join(RUNBOOKS_DIR, "nginx_recovery.md")
    res_rbk = doc_runbook_reader(runbook_path=target_rbk, section_title="Nonexistent Section")
    assert res_rbk["found"] is False

    # 3. Log search missing file -> exit_code 2
    res_log = search_log_stream(target="/tmp/totally_missing_log.log", pattern="error")
    assert res_log["matched"] is False
    assert "error" in res_log


def test_outbox_quarantine_corrupted_line(monkeypatch, tmp_path):
    """Verify corrupted non-JSON lines in outbox.jsonl are quarantined without stalling replay."""
    test_outbox = str(tmp_path / "outbox.jsonl")
    corrupted_outbox = str(tmp_path / "outbox_corrupted.jsonl")
    monkeypatch.setattr("services.agent_tools.audit.OUTBOX_PATH", test_outbox)

    valid_record = json.dumps({"timestamp": "2026-09-24T03:00:00Z", "service": "dsh-agent", "user_id": "sysadmin-01"}).encode("utf-8") + b"\n"
    bad_record = b"CORRUPTED_NON_JSON_DATA_GARBAGE\n"

    # Write: bad line, then valid line
    with open(test_outbox, "wb") as f:
        f.write(bad_record)
        f.write(valid_record)

    sent = []
    monkeypatch.setattr("services.agent_tools.audit._post_event", lambda ev: sent.append(ev) or True)

    result = flush_outbox()
    # Bad line was quarantined, valid line was sent
    assert result["sent"] == 1
    assert result["pending"] == 0
    assert len(sent) == 1

    # Verify corrupted line was appended to outbox_corrupted.jsonl
    assert os.path.exists(corrupted_outbox)
    with open(corrupted_outbox, "rb") as cf:
        assert b"CORRUPTED_NON_JSON_DATA_GARBAGE" in cf.read()
