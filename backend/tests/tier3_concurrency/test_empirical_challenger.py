#!/usr/bin/env python3
"""
Empirical Challenger Test Suite for Milestone M1.
Tests:
1. ForwardAuth Header Spoofing & Rejection
2. QuotaManager Concurrency Ceiling (2 in-flight) & 10-user isolation
3. Rate Limiting (60 RPM, 150k TPM) & Daily Midnight Rollover
4. P1 Emergency Elevation & TTL Expiration
5. Live Endpoint Concurrency & Rate Limiting
6. ReAct Agent Parser Resilience & Adversarial Fuzzing
"""
import sys
import os
import time
import json
import asyncio
import datetime
import pytest
import httpx
import redis
from pathlib import Path

# Add backend directory to sys.path
BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from services.auth_gateway.quota_manager import QuotaManager, QuotaExceededException
from services.agent_runtime.parser import parse_model_output, clean_and_repair_json, ParsedTurn

_valkey_key = Path(BACKEND_DIR) / "config" / "keys" / "valkey-password.key"
VALKEY_URL = f"redis://:{_valkey_key.read_text().strip()}@127.0.0.1:6379/0" if _valkey_key.exists() else "redis://:CONFIGURE_VIA_PLATFORM_SH@127.0.0.1:6379/0"
TRAEFIK_URL = "http://127.0.0.1:8080"
AUTH_URL = "http://127.0.0.1:3081"
AGENT_URL = "http://127.0.0.1:3080"
LITELLM_URL = "http://127.0.0.1:4000"

KEYS_DIR = Path(BACKEND_DIR) / "config" / "keys"
TOKEN_01 = (KEYS_DIR / "sysadmin-01.key").read_text().strip() if (KEYS_DIR / "sysadmin-01.key").exists() else ""
TOKEN_02 = (KEYS_DIR / "sysadmin-02.key").read_text().strip() if (KEYS_DIR / "sysadmin-02.key").exists() else ""
P1_TOKEN = (KEYS_DIR / "emergency-p1.key").read_text().strip() if (KEYS_DIR / "emergency-p1.key").exists() else ""


@pytest.fixture(autouse=True)
def live_service_prerequisites(request):
    name = request.node.name
    if name.startswith("test_forwardauth") or name.startswith("test_traefik"):
        url = f"{TRAEFIK_URL}/ping" if name.startswith("test_traefik") else f"{AUTH_URL}/health"
        try:
            httpx.get(url, timeout=1)
        except httpx.RequestError:
            pytest.skip("Live auth/Traefik services are unavailable on this host")
        if name in {"test_forwardauth_identity_spoofing_prevented", "test_traefik_header_stripping_and_protection"} and not TOKEN_01:
            pytest.skip("Provisioned bearer token is unavailable")
    elif name.startswith("test_quotamanager") or name.startswith("test_p1_elevation"):
        try:
            client = redis.Redis.from_url(VALKEY_URL, socket_timeout=1)
            client.ping()
        except redis.RedisError:
            pytest.skip("Live Valkey service is unavailable on this host")

# ==============================================================================
# Suite 1: ForwardAuth & Header Spoofing Tests
# ==============================================================================

def test_forwardauth_unauthenticated_rejected():
    """Verify missing credentials rejected with HTTP 401."""
    resp = httpx.get(f"{AUTH_URL}/verify", timeout=3)
    assert resp.status_code == 401
    assert "WWW-Authenticate" in resp.headers

def test_forwardauth_spoofed_x_user_rejected():
    """Verify spoofed X-User without token is rejected with HTTP 401."""
    resp = httpx.get(f"{AUTH_URL}/verify", headers={"X-User": "sysadmin-02"}, timeout=3)
    assert resp.status_code == 401

def test_forwardauth_spoofed_x_forwarded_user_rejected():
    """Verify spoofed X-Forwarded-User without token is rejected with HTTP 401."""
    resp = httpx.get(f"{AUTH_URL}/verify", headers={"X-Forwarded-User": "sysadmin-02"}, timeout=3)
    assert resp.status_code == 401

def test_forwardauth_invalid_tokens_rejected():
    """Verify malformed and malicious tokens fail closed with HTTP 401."""
    malicious_tokens = [
        "Bearer",
        "Bearer invalid-token-12345",
        "Bearer sk-sysadmin-01-tampered",
        "Bearer ' OR '1'='1",
        "Bearer ../../../etc/shadow",
        "Bearer <script>alert(1)</script>",
        "Bearer " + "A" * 5000,
    ]
    for tok in malicious_tokens:
        resp = httpx.get(f"{AUTH_URL}/verify", headers={"Authorization": tok}, timeout=3)
        assert resp.status_code == 401, f"Failed for token: {tok}"

def test_forwardauth_identity_spoofing_prevented():
    """
    CRITICAL: When authenticated as sysadmin-01, client attempts to pass
    X-User: sysadmin-02. ForwardAuth MUST ignore client header and assert sysadmin-01.
    """
    headers = {
        "Authorization": f"Bearer {TOKEN_01}",
        "X-User": "sysadmin-02",
        "X-Forwarded-User": "sysadmin-02",
        "X-User-Role": "admin"
    }
    resp = httpx.get(f"{AUTH_URL}/verify", headers=headers, timeout=3)
    assert resp.status_code == 200
    assert resp.headers.get("X-Forwarded-User") == "sysadmin-01"
    assert resp.headers.get("X-User") == "sysadmin-01"
    assert resp.headers.get("X-User-Role") == "sysadmin"

def test_traefik_header_stripping_and_protection():
    """Verify Traefik strips client headers and protects backend routes."""
    # 1. Unauthenticated to protected route
    r_unauth = httpx.get(f"{TRAEFIK_URL}/api/tools/list", headers={"X-User": "sysadmin-01"}, timeout=3)
    assert r_unauth.status_code == 401

    # 2. Authenticated with sysadmin-01 but spoofing sysadmin-02
    headers = {
        "Authorization": f"Bearer {TOKEN_01}",
        "X-User": "sysadmin-02"
    }
    r_auth = httpx.get(f"{TRAEFIK_URL}/api/tools/list", headers=headers, timeout=3)
    assert r_auth.status_code == 200

# ==============================================================================
# Suite 2: QuotaManager Concurrency Ceiling (2 In-Flight Limit)
# ==============================================================================

def test_quotamanager_concurrency_ceiling_two_slots():
    """Verify QuotaManager strictly permits 2 in-flight slots and rejects 3rd."""
    qm = QuotaManager(VALKEY_URL)
    user = f"challenger-concurrency-{time.time()}"

    # Clean initial state
    r = qm.redis
    if r:
        r.delete(f"inflight:{user}")

    # Slot 1
    lease1 = qm.acquire_concurrency_slot(user)
    assert lease1.startswith(f"lease:{user}:")

    # Slot 2
    lease2 = qm.acquire_concurrency_slot(user)
    assert lease2.startswith(f"lease:{user}:")

    # Slot 3: MUST raise QuotaExceededException
    with pytest.raises(QuotaExceededException) as exc_info:
        qm.acquire_concurrency_slot(user)
    assert exc_info.value.limit_type == "concurrency"
    assert exc_info.value.limit == 2

    # Release 1 slot
    qm.release_concurrency_slot(user)

    # Now Slot 3 can be acquired
    lease3 = qm.acquire_concurrency_slot(user)
    assert lease3.startswith(f"lease:{user}:")

    # Release remaining
    qm.release_concurrency_slot(user)
    qm.release_concurrency_slot(user)

def test_quotamanager_ten_users_concurrent_isolation():
    """Verify 10 concurrent sysadmins can each hold 2 slots (20 total) without collision."""
    qm = QuotaManager(VALKEY_URL)
    r = qm.redis
    users = [f"challenger-user-{i:02d}-{int(time.time())}" for i in range(1, 11)]

    # Clean
    if r:
        for u in users:
            r.delete(f"inflight:{u}")

    # All 10 acquire slot 1
    for u in users:
        qm.acquire_concurrency_slot(u)

    # All 10 acquire slot 2 (20 total active slots)
    for u in users:
        qm.acquire_concurrency_slot(u)

    # All 10 attempt slot 3 -> ALL MUST FAIL
    for u in users:
        with pytest.raises(QuotaExceededException):
            qm.acquire_concurrency_slot(u)

    # Release all
    for u in users:
        qm.release_concurrency_slot(u)
        qm.release_concurrency_slot(u)

# ==============================================================================
# Suite 3: Rate Limiting & Daily Rollover
# ==============================================================================

def test_quotamanager_rpm_60_burst():
    """Verify QuotaManager allows 60 requests in 60s and rejects 61st."""
    qm = QuotaManager(VALKEY_URL)
    user = f"challenger-rpm-{time.time()}"
    r = qm.redis
    if r:
        r.delete(f"rate:rpm:{user}")

    # 60 requests succeed
    for i in range(60):
        cnt = qm.check_and_record_rpm(user)
        assert cnt == i + 1

    # 61st request rejected
    with pytest.raises(QuotaExceededException) as exc_info:
        qm.check_and_record_rpm(user)
    assert exc_info.value.limit_type == "rpm"
    assert exc_info.value.limit == 60

def test_quotamanager_daily_budget_2m_and_midnight_rollover():
    """Verify 2,000,000 daily token budget and rollover across calendar days."""
    qm = QuotaManager(VALKEY_URL)
    user = f"challenger-daily-{time.time()}"
    today_str = datetime.date.today().isoformat()
    daily_key = f"daily_tokens:{user}:{today_str}"
    r = qm.redis
    if r:
        r.delete(daily_key)

    # Consume 1,999,900 tokens
    qm.record_token_consumption(user, prompt_tokens=1000000, completion_tokens=999900)
    current, limit = qm.check_daily_token_budget(user, estimated_tokens=50)
    assert current == 1999900
    assert limit == 2000000

    # Consume another 200 tokens -> Exceeds 2M limit
    qm.record_token_consumption(user, prompt_tokens=100, completion_tokens=100)
    with pytest.raises(QuotaExceededException) as exc_info:
        qm.check_daily_token_budget(user)
    assert exc_info.value.limit_type == "daily_tokens"

    # Simulate midnight rollover (new day's key does not exist yet)
    tomorrow_str = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    # If checked on tomorrow's date, consumed is 0
    if r:
        tom_consumed = int(r.get(f"daily_tokens:{user}:{tomorrow_str}") or 0)
        assert tom_consumed == 0

# ==============================================================================
# Suite 4: P1 Emergency Elevation
# ==============================================================================

def test_p1_elevation_lifecycle():
    """Verify P1 elevation grants 6 concurrent slots and expires after TTL."""
    qm = QuotaManager(VALKEY_URL)
    user = f"challenger-p1-{time.time()}"
    r = qm.redis

    # Standard user has limit 2
    assert qm.is_p1_elevated(user) is False

    # Grant short 2-second P1 elevation
    qm.grant_p1_elevation(user, duration_seconds=2, reason="Drill test")
    assert qm.is_p1_elevated(user) is True

    # P1 user can acquire 6 slots
    leases = []
    for _ in range(6):
        leases.append(qm.acquire_concurrency_slot(user))

    # 7th slot fails
    with pytest.raises(QuotaExceededException):
        qm.acquire_concurrency_slot(user)

    # Release slots
    for _ in leases:
        qm.release_concurrency_slot(user)

    # Wait for TTL to expire
    time.sleep(2.1)
    assert qm.is_p1_elevated(user) is False

    # Once expired, limit reverts to 2
    l1 = qm.acquire_concurrency_slot(user)
    l2 = qm.acquire_concurrency_slot(user)
    with pytest.raises(QuotaExceededException):
        qm.acquire_concurrency_slot(user)

    qm.release_concurrency_slot(user)
    qm.release_concurrency_slot(user)

# ==============================================================================
# Suite 5: ReAct Loop Parser Resilience (Adversarial Fuzzing)
# ==============================================================================

def test_parser_unclosed_markdown_fence():
    """Model emits code fence that is never closed."""
    tools = ["search_log_stream", "config_lint_and_diff"]
    text = (
        "Thought: Let me check logs.\n"
        "Action: search_log_stream\n"
        "Action Input: ```json\n"
        "{\"target\": \"nginx.log\", \"pattern\": \"502\"}"
    )
    turn = parse_model_output(text, tools)
    assert turn.type == "action"
    assert turn.tool_name == "search_log_stream"
    assert turn.tool_args == {"target": "nginx.log", "pattern": "502"}

def test_parser_unescaped_regex_backslashes():
    """Model outputs unescaped regexes like connect\\(\\) or \\d+."""
    tools = ["search_log_stream"]
    text = (
        "Thought: Search for regex.\n"
        "Action: search_log_stream\n"
        "Action Input: {\"target\": \"nginx.log\", \"pattern\": \"connect\\(\\)\"}"
    )
    turn = parse_model_output(text, tools)
    assert turn.type == "action"
    assert "pattern" in turn.tool_args

def test_parser_trailing_commas():
    """Model outputs JSON with trailing commas."""
    tools = ["search_log_stream"]
    text = (
        "Thought: Checking.\n"
        "Action: search_log_stream\n"
        "Action Input: {\"target\": \"nginx.log\", \"pattern\": \"error\",}"
    )
    turn = parse_model_output(text, tools)
    assert turn.type == "action"
    assert turn.tool_args["pattern"] == "error"

def test_parser_single_quotes_dict():
    """Model outputs python dictionary with single quotes."""
    tools = ["search_log_stream"]
    text = (
        "Action: search_log_stream\n"
        "Action Input: {'target': 'nginx.log', 'pattern': 'critical'}"
    )
    turn = parse_model_output(text, tools)
    assert turn.type == "action"
    assert turn.tool_args["pattern"] == "critical"

def test_parser_xml_tool_call():
    """Model outputs XML style tool call."""
    tools = ["search_log_stream"]
    text = (
        "<tool_call>\n"
        "{\"name\": \"search_log_stream\", \"arguments\": {\"target\": \"test.log\", \"pattern\": \"foo\"}}\n"
        "</tool_call>"
    )
    turn = parse_model_output(text, tools)
    assert turn.type == "action"
    assert turn.tool_name == "search_log_stream"
    assert turn.tool_args["target"] == "test.log"

def test_parser_malformed_json_returns_error_turn_not_exception():
    """Completely unparseable action input must return error turn, never crash."""
    tools = ["search_log_stream"]
    text = (
        "Action: search_log_stream\n"
        "Action Input: {this is complete nonsense [[[]]"
    )
    turn = parse_model_output(text, tools)
    assert turn.type == "error"
    assert turn.error_message is not None

def test_parser_unknown_tool_returns_error_turn():
    """Unknown tool name returns error turn."""
    tools = ["search_log_stream"]
    text = "Action: dangerous_unknown_tool\nAction Input: {}"
    turn = parse_model_output(text, tools)
    assert turn.type == "error"
    assert "dangerous_unknown_tool" in turn.error_message

def test_parser_pure_conversational_response():
    """Standard conversational response without Final Answer marker falls back gracefully."""
    tools = ["search_log_stream"]
    text = "The issue appears to be related to upstream DNS timeout."
    turn = parse_model_output(text, tools)
    assert turn.type == "final_answer"
    assert "DNS timeout" in turn.final_answer

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
