#!/usr/bin/env python3
"""
Sysadmin AI Platform - End-to-End Backend Verification Test Suite
"""
import os
import sys
import time
import json
import socket
import functools
import httpx
import redis
from pathlib import Path

try:
    import pytest
except ImportError:  # the file is also run directly by ./platform.sh test
    pytest = None

PASS = "\033[32m[PASS]\033[0m"
FAIL = "\033[31m[FAIL]\033[0m"
WARN = "\033[33m[WARN]\033[0m"

errors = 0

def test_valkey():
    global errors
    print("\n--- Testing Valkey State & Quota Store (Port 6379) ---")
    try:
        password = (Path(__file__).resolve().parents[1] / "config/keys/valkey-password.key").read_text().strip()
        r = redis.Redis(host="127.0.0.1", port=6379, password=password, socket_timeout=2)
        r.ping()
        r.set("test_key", "test_val", ex=10)
        val = r.get("test_key").decode()
        if val == "test_val":
            print(f"{PASS} Valkey connection, authentication, and atomic SET/GET succeeded.")
        else:
            print(f"{FAIL} Valkey GET mismatch: {val}")
            errors += 1
    except Exception as e:
        print(f"{FAIL} Valkey error: {e}")
        errors += 1

def test_victorialogs():
    global errors
    print("\n--- Testing VictoriaLogs Audit Engine (Port 9428) ---")
    try:
        # Ingest an audit event
        url = "http://127.0.0.1:9428/insert/jsonline?_stream_fields=service,user_id&_time_field=timestamp"
        record = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "service": "dsh-agent",
            "user_id": "sysadmin-01",
            "session_id": "test-session",
            "tool_name": "test_verification",
            "exit_code": 0
        }
        resp = httpx.post(url, content=json.dumps(record) + "\n", headers={"Content-Type": "application/stream+json"}, timeout=3)
        if resp.status_code in [200, 204]:
            print(f"{PASS} VictoriaLogs audit log streaming ingestion succeeded (HTTP {resp.status_code}).")
        else:
            print(f"{FAIL} VictoriaLogs ingestion failed with status {resp.status_code}: {resp.text}")
            errors += 1
    except Exception as e:
        print(f"{FAIL} VictoriaLogs error: {e}")
        errors += 1

def test_seaweedfs():
    global errors
    print("\n--- Testing SeaweedFS S3 Storage & Master (Port 8333/9333) ---")
    try:
        resp = httpx.get("http://127.0.0.1:9333/cluster/status", timeout=3)
        if resp.status_code == 200:
            print(f"{PASS} SeaweedFS Master API responding healthy (HTTP 200).")
        else:
            print(f"{FAIL} SeaweedFS Master status code: {resp.status_code}")
            errors += 1

        # S3 gateway may take 3-4 seconds after master election
        s3_ok = False
        for _ in range(10):
            try:
                s3_resp = httpx.get("http://127.0.0.1:8333/", timeout=2)
                if s3_resp.status_code in [200, 403, 400]:
                    s3_ok = True
                    break
            except Exception:
                time.sleep(1)
        if s3_ok:
            print(f"{PASS} SeaweedFS S3 Gateway active on port 8333.")
        else:
            print(f"{FAIL} SeaweedFS S3 Gateway not responding on port 8333.")
            errors += 1
    except Exception as e:
        print(f"{FAIL} SeaweedFS error: {e}")
        errors += 1

def test_inference_engine():
    global errors
    print("\n--- Testing Local Inference Engine (Port 8000) ---")
    try:
        models_resp = httpx.get("http://127.0.0.1:8000/v1/models", timeout=3)
        if models_resp.status_code == 200:
            models = [m["id"] for m in models_resp.json().get("data", [])]
            if "fast-model" in models and "heavy-model" in models:
                print(f"{PASS} Inference Engine registered models: {models}")
            else:
                print(f"{FAIL} Expected models missing: {models}")
                errors += 1
        else:
            print(f"{FAIL} Inference Engine /v1/models failed: {models_resp.status_code}")
            errors += 1

        # Test Streaming Chat Completion
        stream_payload = {
            "model": "fast-model",
            "messages": [{"role": "user", "content": "Diagnose nginx logs"}],
            "stream": True
        }
        with httpx.stream("POST", "http://127.0.0.1:8000/v1/chat/completions", json=stream_payload, timeout=5) as r:
            chunks = list(r.iter_lines())
            if any("chat.completion.chunk" in c for c in chunks):
                print(f"{PASS} Inference Engine SSE streaming verified ({len(chunks)} chunks received).")
            else:
                print(f"{FAIL} Inference Engine SSE streaming returned unexpected data.")
                errors += 1
    except Exception as e:
        print(f"{FAIL} Inference Engine error: {e}")
        errors += 1

def test_auth_gateway():
    global errors
    print("\n--- Testing ForwardAuth Identity Gateway (Port 3081) ---")
    try:
        # 1. Negative test: Unauthenticated request must return 401
        resp_unauth = httpx.get("http://127.0.0.1:3081/verify", timeout=3)
        if resp_unauth.status_code == 401:
            print(f"{PASS} ForwardAuth fail-closed: Unauthenticated request rejected with HTTP 401.")
        else:
            print(f"{FAIL} ForwardAuth fail-closed failed: Expected 401, got {resp_unauth.status_code}")
            errors += 1

        # 2. Negative test: Forged X-User header without Bearer token must return 401
        resp_forged = httpx.get("http://127.0.0.1:3081/verify", headers={"X-User": "sysadmin-03"}, timeout=3)
        if resp_forged.status_code == 401:
            print(f"{PASS} ForwardAuth security: Forged X-User header rejected with HTTP 401.")
        else:
            print(f"{FAIL} ForwardAuth failed to reject forged X-User: {resp_forged.status_code}")
            errors += 1

        # 3. Negative test: Substring / invalid token must return 401
        resp_bad_tok = httpx.get("http://127.0.0.1:3081/verify", headers={"Authorization": "Bearer fake-sysadmin-03"}, timeout=3)
        if resp_bad_tok.status_code == 401:
            print(f"{PASS} ForwardAuth security: Invalid Bearer token rejected with HTTP 401.")
        else:
            print(f"{FAIL} ForwardAuth accepted invalid Bearer token: {resp_bad_tok.status_code}")
            errors += 1

        # 4. Positive test: Valid Bearer token for sysadmin-03
        keys_dir = Path(__file__).resolve().parents[1] / "config/keys"
        token_03 = (keys_dir / "sysadmin-03.key").read_text().strip()
        resp = httpx.get("http://127.0.0.1:3081/verify", headers={"Authorization": f"Bearer {token_03}"}, timeout=3)
        if resp.status_code == 200 and resp.headers.get("X-Forwarded-User") == "sysadmin-03":
            print(f"{PASS} ForwardAuth verified identity: user={resp.headers.get('X-Forwarded-User')}, role={resp.headers.get('X-Forwarded-Role')}")
        else:
            print(f"{FAIL} ForwardAuth failed with Bearer token: {resp.status_code} headers={dict(resp.headers)}")
            errors += 1
    except Exception as e:
        print(f"{FAIL} Auth Gateway error: {e}")
        errors += 1

def test_agent_tools_and_security():
    global errors
    print("\n--- Testing Agent Platform Tools & Bubblewrap Confinement (Port 3080) ---")
    base_url = "http://127.0.0.1:3080"
    keys_dir = Path(__file__).resolve().parents[1] / "config/keys"
    token_01 = (keys_dir / "sysadmin-01.key").read_text().strip()
    headers = {"Authorization": f"Bearer {token_01}"}
    try:
        # 1. Bounded Log Search
        search_req = {
            "name": "search_log_stream",
            "parameters": {
                "target": "./backend/data/logs/nginx_error.log",
                "pattern": "connect\\(\\)",
                "max_matches": 10
            }
        }
        resp = httpx.post(f"{base_url}/api/tools/execute", json=search_req, headers=headers, timeout=5)
        if resp.status_code == 200 and resp.json()["result"]["matched"]:
            print(f"{PASS} Tool 'search_log_stream' matched log pattern within bounds.")
        else:
            print(f"{FAIL} Tool 'search_log_stream' failed: {resp.text}")
            errors += 1

        # 2. Syntax Lint and Unified Diff
        lint_req = {
            "name": "config_lint_and_diff",
            "parameters": {
                "target_file": "./backend/data/runbooks/test_unit.json",
                "proposed_content": "{\n  \"status\": \"active\",\n  \"port\": 8080\n}\n"
            }
        }
        resp = httpx.post(f"{base_url}/api/tools/execute", json=lint_req, headers=headers, timeout=5)
        if resp.status_code == 200 and resp.json()["result"]["valid"]:
            print(f"{PASS} Tool 'config_lint_and_diff' validated syntax and generated diff.")
        else:
            print(f"{FAIL} Tool 'config_lint_and_diff' failed: {resp.text}")
            errors += 1

        # 3. Markdown Runbook Reader
        rb_req = {
            "name": "doc_runbook_reader",
            "parameters": {
                "runbook_path": "./backend/data/runbooks/nginx_recovery.md",
                "section_title": "Diagnostic Rapide"
            }
        }
        resp = httpx.post(f"{base_url}/api/tools/execute", json=rb_req, headers=headers, timeout=5)
        if resp.status_code == 200 and resp.json()["result"]["found"]:
            print(f"{PASS} Tool 'doc_runbook_reader' extracted specified section.")
        else:
            print(f"{FAIL} Tool 'doc_runbook_reader' failed: {resp.text}")
            errors += 1

        # 4. Bubblewrap Sandboxed Execution (Safe Command)
        bash_req = {
            "name": "sandboxed_bash",
            "parameters": {"command": "echo 'Sandbox Active'"}
        }
        resp = httpx.post(f"{base_url}/api/tools/execute", json=bash_req, headers=headers, timeout=10)
        if resp.status_code == 200 and "Sandbox Active" in resp.json()["result"]["stdout"]:
            print(f"{PASS} Tool 'sandboxed_bash' executed command inside isolated Bubblewrap sandbox.")
        else:
            print(f"{FAIL} Tool 'sandboxed_bash' failed: {resp.text}")
            errors += 1

        # 5. Security: Dangerous Command Interception (Must Block rm -rf)
        danger_req = {
            "name": "sandboxed_bash",
            "parameters": {"command": "rm -rf /"}
        }
        resp = httpx.post(f"{base_url}/api/tools/execute", json=danger_req, headers=headers, timeout=5)
        if resp.status_code == 403:
            print(f"{PASS} Security Interceptor: Blocked dangerous command 'rm -rf /' (HTTP 403).")
        else:
            print(f"{FAIL} Security Interceptor failed to block dangerous command (status {resp.status_code}).")
            errors += 1

        # 6. Security: Mutating Command Interception (Requires Human Approval)
        mutate_req = {
            "name": "sandboxed_bash",
            "parameters": {"command": "systemctl restart nginx"}
        }
        resp = httpx.post(f"{base_url}/api/tools/execute", json=mutate_req, headers=headers, timeout=5)
        if resp.status_code == 202 and resp.json().get("status") == "approval_required":
            appr_id = resp.json().get("approval_id")
            print(f"{PASS} Approval Gate: Held mutating command for Human-in-the-Loop review (Approval ID: {appr_id}).")

            # Test approving the request using admin credentials (master.key)
            admin_token = (keys_dir / "master.key").read_text().strip()
            admin_headers = {"Authorization": f"Bearer {admin_token}"}
            decide_req = {"approval_id": appr_id, "approved": True}
            dec_resp = httpx.post(f"{base_url}/api/approvals/decide", json=decide_req, headers=admin_headers, timeout=5)
            if dec_resp.status_code == 200 and dec_resp.json()["approval"]["status"] == "approved":
                print(f"{PASS} Approval Gate: Human review approved state change successfully.")
            else:
                print(f"{FAIL} Approval Gate decision failed: {dec_resp.text}")
                errors += 1
        else:
            print(f"{FAIL} Approval Gate failed to intercept mutating command: {resp.text}")
            errors += 1

    except Exception as e:
        print(f"{FAIL} Agent Platform error: {e}")
        errors += 1

def test_traefik_gateway():
    global errors
    print("\n--- Testing Traefik Reverse Proxy & ForwardAuth Gateway (Port 8080) ---")
    try:
        keys_dir = Path(__file__).resolve().parents[1] / "config/keys"
        token_01 = (keys_dir / "sysadmin-01.key").read_text().strip()

        # 1. Unauthenticated request to /api/tools/list must return 401
        resp_unauth = httpx.get("http://127.0.0.1:8080/api/tools/list", timeout=5)
        if resp_unauth.status_code == 401:
            print(f"{PASS} Traefik enforced ForwardAuth on /api/tools/list: Returned HTTP 401.")
        else:
            print(f"{FAIL} Traefik unauthenticated access not blocked: status {resp_unauth.status_code}")
            errors += 1

        # 2. Authenticated route to agent tools via Traefik with ForwardAuth
        resp = httpx.get(
            "http://127.0.0.1:8080/api/tools/list",
            headers={"Authorization": f"Bearer {token_01}"},
            timeout=5
        )
        if resp.status_code == 200:
            tools = [t["name"] for t in resp.json().get("tools", [])]
            print(f"{PASS} Traefik routed request through ForwardAuth to Agent API. Tools: {tools}")
        else:
            print(f"{FAIL} Traefik routing failed with status {resp.status_code}: {resp.text}")
            errors += 1

        # 3. Route to agent runtime chat endpoint via Traefik (:8080/api/v1/agent/chat)
        chat_payload = {
            "prompt": "Investigate 502 errors in nginx log",
            "session_id": "e2e-traefik-chat-01"
        }
        chat_resp = httpx.post(
            "http://127.0.0.1:8080/api/v1/agent/chat",
            json=chat_payload,
            headers={"Authorization": f"Bearer {token_01}"},
            timeout=10
        )
        if chat_resp.status_code == 200 and "response" in chat_resp.json():
            print(f"{PASS} Traefik routed to server-side ReAct chat endpoint (/api/v1/agent/chat).")
        else:
            print(f"{FAIL} Traefik agent chat endpoint failed with status {chat_resp.status_code}: {chat_resp.text}")
            errors += 1

        # 4. LiteLLM endpoint protected by ForwardAuth
        llm_unauth = httpx.get("http://127.0.0.1:8080/v1/models", timeout=5)
        if llm_unauth.status_code == 401:
            print(f"{PASS} Traefik enforced ForwardAuth on LiteLLM (/v1/models): Returned HTTP 401.")
        else:
            print(f"{FAIL} LiteLLM route unprotected by ForwardAuth: status {llm_unauth.status_code}")
            errors += 1

        # 5. S3 endpoint protected by ForwardAuth
        s3_unauth = httpx.get("http://127.0.0.1:8080/s3", timeout=5)
        if s3_unauth.status_code in [401, 403]:
            print(f"{PASS} Traefik enforced ForwardAuth on S3 (/s3): Returned HTTP {s3_unauth.status_code}.")
        else:
            print(f"{FAIL} S3 route unprotected by ForwardAuth: status {s3_unauth.status_code}")
            errors += 1

    except Exception as e:
        print(f"{FAIL} Traefik error: {e}")
        errors += 1

# ---------------------------------------------------------------------------
# pytest integration
#
# This file is both the "./platform.sh test" script and a pytest module. As a
# script it reports through the PASS/FAIL lines and the exit code. Under pytest
# it must stay honest: skip when the live stack is not running, and fail when a
# section records a failure instead of passing because the failure was only
# printed.
# ---------------------------------------------------------------------------

REQUIRED_STACK_PORTS = {
    6379: "valkey",
    9428: "victorialogs",
    8333: "seaweedfs",
    8000: "inference",
    3081: "auth_gateway",
    3080: "agent_tools",
    4000: "litellm",
    8080: "traefik",
}


def missing_stack_ports() -> list:
    missing = []
    for port, _service in REQUIRED_STACK_PORTS.items():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                missing.append(port)
    return missing


def _fail_on_recorded_error(fn):
    """Turn a printed section failure into a pytest failure."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        before = errors
        fn(*args, **kwargs)
        assert errors == before, f"{fn.__name__}: one or more platform checks failed (see output above)"

    return wrapper


if pytest is not None and "pytest" in sys.modules:
    _missing_ports = missing_stack_ports()
    if _missing_ports:
        pytestmark = pytest.mark.skip(
            reason=(
                "live platform stack is not running (ports down: "
                + ", ".join(str(port) for port in _missing_ports)
                + "); run ./platform.sh test"
            )
        )
    else:
        for _name in (
            "test_valkey",
            "test_victorialogs",
            "test_seaweedfs",
            "test_inference_engine",
            "test_auth_gateway",
            "test_agent_tools_and_security",
            "test_traefik_gateway",
        ):
            globals()[_name] = _fail_on_recorded_error(globals()[_name])


def main():
    print("================================================================")
    print("      Sysadmin AI Platform - Backend Verification Test Suite    ")
    print("================================================================")
    test_valkey()
    test_victorialogs()
    test_seaweedfs()
    test_inference_engine()
    test_auth_gateway()
    test_agent_tools_and_security()
    test_traefik_gateway()
    print("\n================================================================")
    if errors == 0:
        print("\033[32m ALL BACKEND TESTS PASSED! System fully functional & verified.\033[0m")
        print("================================================================")
        sys.exit(0)
    else:
        print(f"\033[31m {errors} TEST(S) FAILED. Check service logs.\033[0m")
        print("================================================================")
        sys.exit(1)

if __name__ == "__main__":
    main()
