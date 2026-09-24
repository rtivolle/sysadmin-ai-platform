#!/usr/bin/env python3
"""
Tier 3 Concurrency & Cancellation Empirical Stress Suite (Milestone M2).
Adversarially probes:
1. High Concurrency Burst:
   - >2 rapid parallel chat requests per user returning HTTP 429 consistently without in-flight corruption.
   - Multi-user burst isolation across 5 users (25 concurrent requests).
   - QuotaManager in-flight lease saturation stress.
2. Slot Recovery on Cancellation and Failures:
   - Immediate slot reclamation after upstream inference failure (502 / network error).
   - Immediate slot reclamation after active task cancellation via POST /api/v1/agent/cancel.
   - Prevention of cross-user cancellation attacks (HTTP 403) with no slot theft.
   - Repeated cancel/failure soak cycles verifying zero slot drift.
3. Client Disconnect During SSE Generation:
   - Abrupt client TCP disconnect mid-stream terminating generator and freeing slot.
   - Client disconnect before first chunk aborting upstream inference.
   - 10-cycle disconnect soak verifying zero slot leaks.
   - Race condition between client disconnect and cancel endpoint.
"""
import os
import sys
import json
import time
import uuid
import socket
import asyncio
import pytest
import httpx
import uvicorn
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.agent_runtime.models import (
    AgentChatRequest,
    AgentChatResponse,
    AgentCancelRequest,
    AgentCancelResponse
)
from services.agent_runtime.cancellation import registry, RequestRegistry, ActiveRequestRecord
from services.agent_runtime.router import router as agent_router
from services.agent_runtime.session_store import SessionStore
from services.agent_runtime.react_loop import run_react_agent, run_react_agent_stream
from services.agent_tools.server import app as agent_app
from services.auth_gateway.quota_manager import QuotaManager, QuotaExceededException
from services.auth_gateway.server import quota_mgr
from services.inference_engine.server import simulate_chat_completion

KEYS_DIR = BACKEND_DIR / "config" / "keys"
TOKEN_01 = (KEYS_DIR / "sysadmin-01.key").read_text().strip() if (KEYS_DIR / "sysadmin-01.key").exists() else "mock-token-01"
TOKEN_02 = (KEYS_DIR / "sysadmin-02.key").read_text().strip() if (KEYS_DIR / "sysadmin-02.key").exists() else "mock-token-02"


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ==============================================================================
# Suite 1: High Concurrency Burst Stress Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_rapid_parallel_chat_burst_single_user(monkeypatch):
    """
    Burst 10 rapid concurrent chat requests from a single user.
    Verifies:
    - Exactly 2 requests succeed (ceiling = 2).
    - Exactly 8 requests receive HTTP 429 with 'Concurrency ceiling exceeded'.
    - In-flight slots are completely reclaimed (count = 0) once processing finishes.
    - Subsequent requests are immediately admitted without quota corruption.
    """
    user_id = f"burst-user-{uuid.uuid4().hex[:6]}"
    monkeypatch.setattr("services.auth_gateway.server.authenticate_request", lambda req: (user_id, "sysadmin"))
    monkeypatch.setattr("services.agent_tools.server.authenticate_request", lambda req: (user_id, "sysadmin"))

    # Simulate an inference delay so requests overlap in-flight
    async def delayed_llm(messages, model, uid, session_id=None):
        await asyncio.sleep(0.15)
        return simulate_chat_completion(messages, model)

    monkeypatch.setattr("services.agent_runtime.react_loop.call_llm", delayed_llm)
    monkeypatch.setattr("backend.services.agent_runtime.react_loop.call_llm", delayed_llm)

    transport = httpx.ASGITransport(app=agent_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Launch 10 simultaneous requests
        tasks = [
            client.post("/api/v1/agent/chat", json={"prompt": f"Burst request {i}", "session_id": f"sess-{i}"})
            for i in range(10)
        ]
        responses = await asyncio.gather(*tasks)

        status_codes = [r.status_code for r in responses]
        success_count = status_codes.count(200)
        rate_limited_count = status_codes.count(429)

        assert success_count == 2, f"Expected exactly 2 admitted requests, got {success_count}. Codes: {status_codes}"
        assert rate_limited_count == 8, f"Expected 8 HTTP 429 rejections, got {rate_limited_count}. Codes: {status_codes}"

        # Verify all 429 responses contain appropriate detail
        for r in responses:
            if r.status_code == 429:
                detail = r.json().get("detail", "")
                assert "Concurrency ceiling exceeded" in detail
                assert "2/2 in-flight calls active" in detail or "1/2 in-flight calls active" in detail or "in-flight" in detail

        # Verify in-flight count returns strictly to 0
        local_count = getattr(quota_mgr, "_local_inflight", {}).get(user_id, 0)
        assert local_count == 0, f"In-flight slot leak detected: user {user_id} has {local_count} slots remaining!"

        # Verify new requests immediately succeed
        resp_after = await client.post("/api/v1/agent/chat", json={"prompt": "Post-burst test", "session_id": "sess-new"})
        assert resp_after.status_code == 200


@pytest.mark.asyncio
async def test_rapid_parallel_chat_burst_multi_user_isolation(monkeypatch):
    """
    Burst 25 concurrent requests across 5 independent users (5 requests each).
    Verifies:
    - Every user gets exactly 2 admitted (HTTP 200) and 3 rejected (HTTP 429).
    - Platform total: exactly 10 HTTP 200 and 15 HTTP 429.
    - Zero cross-user quota pollution or starvation.
    - All 5 users return to 0 in-flight slots after completion.
    """
    users = [f"multi-user-{i:02d}-{uuid.uuid4().hex[:4]}" for i in range(5)]

    # Mock auth resolving user from custom header for test multi-user simulation
    def dynamic_auth(request):
        u = request.headers.get("X-Test-User", users[0])
        return (u, "sysadmin")

    monkeypatch.setattr("services.auth_gateway.server.authenticate_request", dynamic_auth)
    monkeypatch.setattr("services.agent_tools.server.authenticate_request", dynamic_auth)

    async def delayed_llm(messages, model, uid, session_id=None):
        await asyncio.sleep(0.12)
        return simulate_chat_completion(messages, model)

    monkeypatch.setattr("services.agent_runtime.react_loop.call_llm", delayed_llm)
    monkeypatch.setattr("backend.services.agent_runtime.react_loop.call_llm", delayed_llm)

    transport = httpx.ASGITransport(app=agent_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 5 requests per user = 25 total
        all_tasks = []
        user_map = {}
        for u in users:
            for req_idx in range(5):
                t = client.post(
                    "/api/v1/agent/chat",
                    headers={"X-Test-User": u},
                    json={"prompt": f"User {u} req {req_idx}", "session_id": f"sess-{u}-{req_idx}"}
                )
                all_tasks.append(t)
                user_map[len(all_tasks) - 1] = u

        responses = await asyncio.gather(*all_tasks)

        # Tabulate results per user
        user_results = {u: {"200": 0, "429": 0, "other": 0} for u in users}
        for idx, resp in enumerate(responses):
            u = user_map[idx]
            if resp.status_code == 200:
                user_results[u]["200"] += 1
            elif resp.status_code == 429:
                user_results[u]["429"] += 1
            else:
                user_results[u]["other"] += 1

        for u in users:
            assert user_results[u]["200"] == 2, f"User {u} expected 2 successes, got: {user_results[u]}"
            assert user_results[u]["429"] == 3, f"User {u} expected 3 rejections (429), got: {user_results[u]}"
            assert user_results[u]["other"] == 0, f"User {u} got unexpected status codes: {user_results[u]}"

        # Verify all users have 0 in-flight slots
        for u in users:
            cnt = getattr(quota_mgr, "_local_inflight", {}).get(u, 0)
            assert cnt == 0, f"Slot leak detected for user {u}: count is {cnt}"


@pytest.mark.asyncio
async def test_quotamanager_high_concurrency_saturation_stress():
    """
    Low-level stress on QuotaManager: 50 concurrent tasks attempting slot acquisition.
    Verifies:
    - Never exceeds limit = 2.
    - Releases do not underflow into negative numbers.
    - Re-acquisitions succeed immediately.
    """
    qm = QuotaManager(redis_client=None)
    user = f"stress-qm-{uuid.uuid4().hex[:8]}"

    acquired_leases = []
    rejected_count = 0
    lock = asyncio.Lock()

    async def worker():
        nonlocal rejected_count
        try:
            lease = qm.acquire_concurrency_slot(user)
            async with lock:
                acquired_leases.append(lease)
            await asyncio.sleep(0.02)
        except QuotaExceededException:
            async with lock:
                rejected_count += 1

    tasks = [worker() for _ in range(50)]
    await asyncio.gather(*tasks)

    assert len(acquired_leases) == 2
    assert rejected_count == 48

    # Release both leases
    qm.release_concurrency_slot(user)
    qm.release_concurrency_slot(user)

    # Count must be exactly 0
    assert getattr(qm, "_local_inflight", {}).get(user, 0) == 0

    # Over-release must not corrupt count into negative
    qm.release_concurrency_slot(user)
    assert getattr(qm, "_local_inflight", {}).get(user, 0) == 0


@pytest.mark.asyncio
async def test_valkey_redis_concurrency_burst_and_slot_recovery():
    """
    Live Valkey Redis stress test:
    Launches an ephemeral Valkey server instance to verify real Redis atomic
    lease-set admission, 100 concurrent requests across 10 users, key
    expiration, and zero slot leaks.
    """
    valkey_bin = BACKEND_DIR / "bin" / "valkey-server"
    if not valkey_bin.is_file() or not os.access(valkey_bin, os.X_OK):
        pytest.skip("valkey-server binary not found")

    vport = get_free_port()
    proc = await asyncio.create_subprocess_exec(
        str(valkey_bin),
        "--port", str(vport),
        "--save", "",
        "--appendonly", "no",
        "--daemonize", "no",
        "--loglevel", "warning",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL
    )

    try:
        # Wait for Valkey readiness
        qm = None
        for _ in range(20):
            try:
                import redis
                client = redis.Redis(host="127.0.0.1", port=vport, socket_timeout=1)
                client.ping()
                qm = QuotaManager(redis_client=client)
                break
            except Exception:
                await asyncio.sleep(0.05)

        assert qm is not None, "Failed to connect to ephemeral Valkey instance"

        # 1. Single User Burst: 30 parallel requests to Valkey
        test_user = f"valkey-burst-{uuid.uuid4().hex[:6]}"
        valkey_leases = []
        valkey_rejections = 0
        lock = asyncio.Lock()

        async def valkey_worker():
            nonlocal valkey_rejections
            try:
                lease = qm.acquire_concurrency_slot(test_user)
                async with lock:
                    valkey_leases.append(lease)
                await asyncio.sleep(0.05)
            except QuotaExceededException:
                async with lock:
                    valkey_rejections += 1

        await asyncio.gather(*[valkey_worker() for _ in range(30)])

        assert len(valkey_leases) == 2, f"Expected 2 admitted leases in Valkey, got {len(valkey_leases)}"
        assert valkey_rejections == 28, f"Expected 28 rejections in Valkey, got {valkey_rejections}"

        # Check the raw Redis lease set: one sorted-set member per live lease
        user_key = f"quota:leases:user:{test_user}"
        raw_val = int(qm.redis.zcard(user_key))
        assert raw_val == 2, f"Valkey lease set should hold exactly 2 in-flight leases, got {raw_val}"
        assert int(qm.redis.zcard("quota:leases:cluster")) >= 2

        # Release both slots
        qm.release_concurrency_slot(test_user)
        qm.release_concurrency_slot(test_user)

        # In-flight lease set should be empty again
        assert int(qm.redis.zcard(user_key)) == 0

        # 2. Multi-User Valkey Burst: 10 users * 10 requests = 100 concurrent operations
        cluster_before = int(qm.redis.zcard("quota:leases:cluster"))
        vusers = [f"vuser-{i:02d}-{uuid.uuid4().hex[:4]}" for i in range(10)]
        user_acquired = {u: 0 for u in vusers}
        user_rejected = {u: 0 for u in vusers}

        async def multi_vworker(u):
            try:
                qm.acquire_concurrency_slot(u)
                async with lock:
                    user_acquired[u] += 1
                await asyncio.sleep(0.05)
            except QuotaExceededException:
                async with lock:
                    user_rejected[u] += 1

        all_vtasks = []
        for u in vusers:
            for _ in range(10):
                all_vtasks.append(multi_vworker(u))

        await asyncio.gather(*all_vtasks)

        for u in vusers:
            assert user_acquired[u] == 2, f"User {u} acquired {user_acquired[u]} (expected 2)"
            assert user_rejected[u] == 8, f"User {u} rejected {user_rejected[u]} (expected 8)"
            # Release both slots
            qm.release_concurrency_slot(u)
            qm.release_concurrency_slot(u)
            assert int(qm.redis.zcard(f"quota:leases:user:{u}")) == 0

        # No lease may survive the burst in the shared cluster index either.
        assert int(qm.redis.zcard("quota:leases:cluster")) == cluster_before

    finally:
        try:
            proc.terminate()
            await proc.wait()
        except Exception:
            pass


# ==============================================================================
# Suite 2: Slot Recovery on Cancellation and Failures
# ==============================================================================

@pytest.mark.asyncio
async def test_slot_recovery_on_upstream_inference_failure(monkeypatch):
    """
    Verify that when upstream inference raises an exception or returns 502,
    the concurrency slot is immediately reclaimed and future requests succeed.
    """
    user_id = f"fail-user-{uuid.uuid4().hex[:6]}"
    monkeypatch.setattr("services.auth_gateway.server.authenticate_request", lambda req: (user_id, "sysadmin"))
    monkeypatch.setattr("services.agent_tools.server.authenticate_request", lambda req: (user_id, "sysadmin"))

    # Force call_llm to raise connection error
    async def broken_llm(messages, model, uid, session_id=None):
        raise httpx.ConnectError("Connection refused to mock inference gateway")

    monkeypatch.setattr("services.agent_runtime.react_loop.call_llm", broken_llm)
    monkeypatch.setattr("backend.services.agent_runtime.react_loop.call_llm", broken_llm)

    transport = httpx.ASGITransport(app=agent_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Request should fail with 502
        resp = await client.post("/api/v1/agent/chat", json={"prompt": "Failing query", "session_id": "sess-fail"})
        assert resp.status_code == 502

        # Verify slot was immediately released
        in_flight = getattr(quota_mgr, "_local_inflight", {}).get(user_id, 0)
        assert in_flight == 0, f"Slot was leaked after 502 failure! in_flight={in_flight}"

        # Now restore healthy inference
        async def healthy_llm(msgs, m, uid, session_id=None):
            return simulate_chat_completion(msgs, m)

        monkeypatch.setattr("services.agent_runtime.react_loop.call_llm", healthy_llm)
        monkeypatch.setattr("backend.services.agent_runtime.react_loop.call_llm", healthy_llm)

        # 2 new requests can be admitted without 429
        r1 = await client.post("/api/v1/agent/chat", json={"prompt": "Recovered 1", "session_id": "sess-r1"})
        assert r1.status_code == 200

        r2 = await client.post("/api/v1/agent/chat", json={"prompt": "Recovered 2", "session_id": "sess-r2"})
        assert r2.status_code == 200


@pytest.mark.asyncio
async def test_slot_recovery_on_active_task_cancellation(monkeypatch):
    """
    Verify that cancelling an active in-flight request via POST /api/v1/agent/cancel:
    1. Returns HTTP 200 with cancelled_request_id.
    2. Aborts the active asyncio task.
    3. Immediately frees the concurrency slot in QuotaManager.
    4. Unregisters the request from RequestRegistry.
    5. Permits an immediate subsequent request without 429.
    """
    user_id = f"cancel-user-{uuid.uuid4().hex[:6]}"
    req_id = f"req-cancel-test-{uuid.uuid4().hex[:8]}"

    monkeypatch.setattr("services.auth_gateway.server.authenticate_request", lambda req: (user_id, "sysadmin"))
    monkeypatch.setattr("services.agent_tools.server.authenticate_request", lambda req: (user_id, "sysadmin"))

    inference_started = asyncio.Event()
    inference_cancelled = asyncio.Event()

    async def cancellable_slow_llm(messages, model, uid, session_id=None):
        inference_started.set()
        try:
            await asyncio.sleep(5.0)  # Wait for cancellation
            return simulate_chat_completion(messages, model)
        except asyncio.CancelledError:
            inference_cancelled.set()
            raise

    monkeypatch.setattr("services.agent_runtime.react_loop.call_llm", cancellable_slow_llm)
    monkeypatch.setattr("backend.services.agent_runtime.react_loop.call_llm", cancellable_slow_llm)

    transport = httpx.ASGITransport(app=agent_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Launch long-running chat request as background task
        chat_task = asyncio.create_task(
            client.post(
                "/api/v1/agent/chat",
                json={"prompt": "Slow query", "session_id": "sess-cancel", "request_id": req_id}
            )
        )

        # Wait until inference actually starts
        await asyncio.wait_for(inference_started.wait(), timeout=2.0)

        # Verify 1 slot is currently acquired
        assert getattr(quota_mgr, "_local_inflight", {}).get(user_id, 0) == 1

        # Issue cancel request
        cancel_resp = await client.post("/api/v1/agent/cancel", json={"request_id": req_id})
        assert cancel_resp.status_code == 200
        data = cancel_resp.json()
        assert data["success"] is True
        assert data["cancelled_request_id"] == req_id

        # Verify chat task terminates with CancelledError
        with pytest.raises((asyncio.CancelledError, Exception)):
            await asyncio.wait_for(chat_task, timeout=1.0)

        # Verify upstream inference received CancelledError
        assert inference_cancelled.is_set(), "Upstream inference was NOT aborted on task cancellation!"

        # Verify slot is immediately freed
        assert getattr(quota_mgr, "_local_inflight", {}).get(user_id, 0) == 0

        # Verify active registry unregisters request
        assert await registry.get_active_count(user_id) == 0

        # Verify new request immediately succeeds
        async def healthy_llm2(msgs, m, uid, session_id=None):
            return simulate_chat_completion(msgs, m)

        monkeypatch.setattr("services.agent_runtime.react_loop.call_llm", healthy_llm2)
        monkeypatch.setattr("backend.services.agent_runtime.react_loop.call_llm", healthy_llm2)

        resp_new = await client.post("/api/v1/agent/chat", json={"prompt": "Next request", "session_id": "sess-new"})
        assert resp_new.status_code == 200


@pytest.mark.asyncio
async def test_cross_user_cancellation_attack_prevented(monkeypatch):
    """
    Adversarial attack: User B attempts to cancel User A's active request.
    Verifies:
    - User B is rejected with HTTP 403 Forbidden.
    - User A's task is NOT cancelled.
    - User A's slot is NOT leaked or stolen.
    - When User A completes, its slot is cleanly released.
    """
    user_a = f"victim-user-{uuid.uuid4().hex[:6]}"
    user_b = f"attacker-user-{uuid.uuid4().hex[:6]}"
    req_a = f"req-victim-{uuid.uuid4().hex[:8]}"

    current_auth_user = user_a

    def auth_dispatch(req):
        return (current_auth_user, "sysadmin")

    monkeypatch.setattr("services.auth_gateway.server.authenticate_request", auth_dispatch)
    monkeypatch.setattr("services.agent_tools.server.authenticate_request", auth_dispatch)

    step_completed = asyncio.Event()

    async def controlled_llm(messages, model, uid, session_id=None):
        await step_completed.wait()
        return simulate_chat_completion(messages, model)

    monkeypatch.setattr("services.agent_runtime.react_loop.call_llm", controlled_llm)
    monkeypatch.setattr("backend.services.agent_runtime.react_loop.call_llm", controlled_llm)

    transport = httpx.ASGITransport(app=agent_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. User A starts request
        current_auth_user = user_a
        task_a = asyncio.create_task(
            client.post("/api/v1/agent/chat", json={"prompt": "User A query", "request_id": req_a})
        )
        await asyncio.sleep(0.05)

        # 2. User B tries to cancel User A's request
        current_auth_user = user_b
        attack_resp = await client.post("/api/v1/agent/cancel", json={"request_id": req_a})
        assert attack_resp.status_code == 403, f"Expected 403 Forbidden, got {attack_resp.status_code}"
        assert "Unauthorized" in attack_resp.json().get("detail", "")

        # 3. Verify User A's task is still active and slot is still held
        assert getattr(quota_mgr, "_local_inflight", {}).get(user_a, 0) == 1
        assert not task_a.done()

        # 4. Release User A and let it finish
        current_auth_user = user_a
        step_completed.set()
        res_a = await asyncio.wait_for(task_a, timeout=2.0)
        assert res_a.status_code == 200

        # User A's slot is now cleanly 0
        assert getattr(quota_mgr, "_local_inflight", {}).get(user_a, 0) == 0


@pytest.mark.asyncio
async def test_repeated_cancel_soak_zero_slot_drift(monkeypatch):
    """
    Stress soak: Perform 15 consecutive acquire -> cancel cycles.
    Verifies that slots are reclaimed 100% of the time with zero drift.
    """
    user_id = f"soak-user-{uuid.uuid4().hex[:6]}"
    monkeypatch.setattr("services.auth_gateway.server.authenticate_request", lambda req: (user_id, "sysadmin"))
    monkeypatch.setattr("services.agent_tools.server.authenticate_request", lambda req: (user_id, "sysadmin"))

    transport = httpx.ASGITransport(app=agent_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for i in range(15):
            req_id = f"soak-req-{i}-{uuid.uuid4().hex[:6]}"

            hold_event = asyncio.Event()

            async def holding_llm(msgs, m, uid, session_id=None):
                await hold_event.wait()
                return simulate_chat_completion(msgs, m)

            monkeypatch.setattr("services.agent_runtime.react_loop.call_llm", holding_llm)
            monkeypatch.setattr("backend.services.agent_runtime.react_loop.call_llm", holding_llm)

            # Start chat
            t = asyncio.create_task(
                client.post("/api/v1/agent/chat", json={"prompt": f"Soak {i}", "request_id": req_id})
            )
            await asyncio.sleep(0.02)

            # In-flight count must be 1
            assert getattr(quota_mgr, "_local_inflight", {}).get(user_id, 0) == 1

            # Cancel it
            c_res = await client.post("/api/v1/agent/cancel", json={"request_id": req_id})
            assert c_res.status_code == 200
            assert c_res.json()["success"] is True

            # Wait for task exit
            try:
                await asyncio.wait_for(t, timeout=0.5)
            except (asyncio.CancelledError, Exception):
                pass

            # Count must be exactly 0 after each iteration
            current_count = getattr(quota_mgr, "_local_inflight", {}).get(user_id, 0)
            assert current_count == 0, f"Drift detected at iteration {i}: in_flight={current_count}"


# ==============================================================================
# Suite 3: Client Disconnect During SSE Generation Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_client_tcp_disconnect_mid_stream_terminates_and_frees_slot(monkeypatch):
    """
    Empirical live TCP stress test:
    1. Runs the agent API under a live in-process Uvicorn server on a loopback port.
    2. Opens a raw TCP socket connection and initiates an SSE chat request (stream=True).
    3. Reads HTTP 200 and the first SSE data chunk.
    4. Abruptly closes the TCP connection (simulating network drop / browser tab close).
    5. Verifies:
       - Generator aborts / task is cancelled via ASGI disconnect.
       - In-flight slot is immediately returned to 0 in QuotaManager.
       - Active request registry is cleared.
       - A new request is immediately admitted.
    """
    user_id = f"disconnect-user-{uuid.uuid4().hex[:6]}"
    monkeypatch.setattr("services.auth_gateway.server.authenticate_request", lambda req: (user_id, "sysadmin"))
    monkeypatch.setattr("services.agent_tools.server.authenticate_request", lambda req: (user_id, "sysadmin"))

    # Multi-step LLM response to provide plenty of stream chunks
    async def streaming_llm_mock(messages, model, uid, session_id=None):
        return simulate_chat_completion(messages, model)

    monkeypatch.setattr("services.agent_runtime.react_loop.call_llm", streaming_llm_mock)
    monkeypatch.setattr("backend.services.agent_runtime.react_loop.call_llm", streaming_llm_mock)

    port = get_free_port()
    config = uvicorn.Config(agent_app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    try:
        # Wait for server readiness
        for _ in range(20):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.05)

        # 1. Connect and send SSE request
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        body = json.dumps({"prompt": "Investigate 502 errors in nginx log", "stream": True}).encode("utf-8")
        req_lines = [
            b"POST /api/v1/agent/chat HTTP/1.1",
            b"Host: 127.0.0.1",
            b"Content-Type: application/json",
            f"Content-Length: {len(body)}".encode("utf-8"),
            b"Connection: close",
            b"",
            body
        ]
        writer.write(b"\r\n".join(req_lines))
        await writer.drain()

        # 2. Read headers and initial SSE chunks
        raw_header = await reader.readuntil(b"\r\n\r\n")
        assert b"200 OK" in raw_header
        assert b"text/event-stream" in raw_header

        # Read first SSE chunk
        first_chunk = await reader.read(256)
        assert len(first_chunk) > 0
        assert b"data: " in first_chunk

        # 3. Abruptly sever the TCP connection
        writer.close()
        await writer.wait_closed()

        # 4. Wait brief interval for ASGI server to process disconnection
        await asyncio.sleep(0.4)

        # 5. Verify concurrency slot is released
        count = getattr(quota_mgr, "_local_inflight", {}).get(user_id, 0)
        assert count == 0, f"Slot was leaked after TCP client disconnect! in_flight={count}"

        # 6. Verify active registry is empty
        active_cnt = await registry.get_active_count(user_id)
        assert active_cnt == 0, f"Request still registered in active registry after disconnect: {active_cnt}"

        # 7. Connect again with a normal request and verify admission succeeds
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http_client:
            res_after = await http_client.post(
                "/api/v1/agent/chat",
                json={"prompt": "Check after disconnect"}
            )
            assert res_after.status_code == 200

    finally:
        server.should_exit = True
        await server_task


@pytest.mark.asyncio
async def test_client_disconnect_before_first_chunk_aborts_upstream_inference(monkeypatch):
    """
    Verify that when a client disconnects *before* the first token is generated:
    1. Upstream inference is aborted via task cancellation.
    2. Concurrency slot is reclaimed.
    """
    user_id = f"early-disc-user-{uuid.uuid4().hex[:6]}"
    monkeypatch.setattr("services.auth_gateway.server.authenticate_request", lambda req: (user_id, "sysadmin"))
    monkeypatch.setattr("services.agent_tools.server.authenticate_request", lambda req: (user_id, "sysadmin"))

    inference_began = asyncio.Event()
    inference_aborted = asyncio.Event()

    async def slow_upstream(msgs, model, uid, session_id=None):
        inference_began.set()
        try:
            await asyncio.sleep(5.0)
            return simulate_chat_completion(msgs, model)
        except asyncio.CancelledError:
            inference_aborted.set()
            raise

    monkeypatch.setattr("services.agent_runtime.react_loop.call_llm", slow_upstream)
    monkeypatch.setattr("backend.services.agent_runtime.react_loop.call_llm", slow_upstream)

    port = get_free_port()
    config = uvicorn.Config(agent_app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    try:
        for _ in range(20):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.05)

        # Connect and initiate streaming request
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        body = json.dumps({"prompt": "Early disconnect test", "stream": True}).encode("utf-8")
        req_lines = [
            b"POST /api/v1/agent/chat HTTP/1.1",
            b"Host: 127.0.0.1",
            b"Content-Type: application/json",
            f"Content-Length: {len(body)}".encode("utf-8"),
            b"",
            body
        ]
        writer.write(b"\r\n".join(req_lines))
        await writer.drain()

        # Wait until upstream inference actually starts
        await asyncio.wait_for(inference_began.wait(), timeout=2.0)

        # Slot is held
        assert getattr(quota_mgr, "_local_inflight", {}).get(user_id, 0) == 1

        # Client closes connection before any data is sent
        writer.close()
        await writer.wait_closed()

        # Wait for cancellation propagation
        await asyncio.sleep(0.4)

        # Upstream inference must have been cancelled
        assert inference_aborted.is_set(), "Upstream inference was not aborted when client disconnected early!"

        # Slot must be released
        assert getattr(quota_mgr, "_local_inflight", {}).get(user_id, 0) == 0

    finally:
        server.should_exit = True
        await server_task


@pytest.mark.asyncio
async def test_consecutive_sse_disconnects_slot_leak_prevention(monkeypatch):
    """
    Stress harness: 10 consecutive SSE requests where client reads 1 chunk and disconnects.
    If slots are leaked, request #3 would fail with HTTP 429.
    Verifies that all 10 requests connect successfully and release their slots.
    """
    user_id = f"leak-test-user-{uuid.uuid4().hex[:6]}"
    monkeypatch.setattr("services.auth_gateway.server.authenticate_request", lambda req: (user_id, "sysadmin"))
    monkeypatch.setattr("services.agent_tools.server.authenticate_request", lambda req: (user_id, "sysadmin"))

    async def soak_stream_llm(msgs, m, uid, session_id=None):
        return simulate_chat_completion(msgs, m)

    monkeypatch.setattr("services.agent_runtime.react_loop.call_llm", soak_stream_llm)
    monkeypatch.setattr("backend.services.agent_runtime.react_loop.call_llm", soak_stream_llm)

    port = get_free_port()
    config = uvicorn.Config(agent_app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    try:
        for _ in range(20):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.05)

        for cycle in range(10):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            body = json.dumps({"prompt": f"Cycle {cycle}", "stream": True}).encode("utf-8")
            req_lines = [
                b"POST /api/v1/agent/chat HTTP/1.1",
                b"Host: 127.0.0.1",
                b"Content-Type: application/json",
                f"Content-Length: {len(body)}".encode("utf-8"),
                b"Connection: close",
                b"",
                body
            ]
            writer.write(b"\r\n".join(req_lines))
            await writer.drain()

            raw_header = await reader.readuntil(b"\r\n\r\n")
            # If slots leaked, this would be 429
            assert b"200 OK" in raw_header, f"Failed on cycle {cycle}: headers={raw_header.decode()}"

            # Read a small amount of stream
            await reader.read(128)

            # Abruptly close
            writer.close()
            await writer.wait_closed()

            await asyncio.sleep(0.1)
            # Slot must return to 0
            cur = getattr(quota_mgr, "_local_inflight", {}).get(user_id, 0)
            assert cur == 0, f"Slot leaked on cycle {cycle}! in_flight={cur}"

    finally:
        server.should_exit = True
        await server_task


@pytest.mark.asyncio
async def test_simultaneous_cancellation_and_disconnect_race(monkeypatch):
    """
    Race condition probe: Client disconnects TCP stream at the exact same instant
    it triggers POST /api/v1/agent/cancel via another channel.
    Verifies no double-decrement, no race exceptions, and slot cleanly reaches 0.
    """
    user_id = f"race-user-{uuid.uuid4().hex[:6]}"
    req_id = f"race-req-{uuid.uuid4().hex[:8]}"

    monkeypatch.setattr("services.auth_gateway.server.authenticate_request", lambda req: (user_id, "sysadmin"))
    monkeypatch.setattr("services.agent_tools.server.authenticate_request", lambda req: (user_id, "sysadmin"))

    async def slow_mock(msgs, m, uid, session_id=None):
        await asyncio.sleep(0.5)
        return simulate_chat_completion(msgs, m)

    monkeypatch.setattr("services.agent_runtime.react_loop.call_llm", slow_mock)
    monkeypatch.setattr("backend.services.agent_runtime.react_loop.call_llm", slow_mock)

    port = get_free_port()
    config = uvicorn.Config(agent_app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    try:
        for _ in range(20):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.05)

        # 1. Connect TCP stream
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        body = json.dumps({"prompt": "Race test", "stream": True, "request_id": req_id}).encode("utf-8")
        req_lines = [
            b"POST /api/v1/agent/chat HTTP/1.1",
            b"Host: 127.0.0.1",
            b"Content-Type: application/json",
            f"Content-Length: {len(body)}".encode("utf-8"),
            b"Connection: close",
            b"",
            body
        ]
        writer.write(b"\r\n".join(req_lines))
        await writer.drain()

        # Read response headers
        header = await reader.readuntil(b"\r\n\r\n")
        assert b"200 OK" in header

        # 2. Race: close TCP socket and call /cancel simultaneously
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            async def do_close():
                writer.close()
                await writer.wait_closed()

            async def do_cancel():
                return await client.post("/api/v1/agent/cancel", json={"request_id": req_id})

            results = await asyncio.gather(do_close(), do_cancel(), return_exceptions=True)

        cancel_resp = results[1]
        assert isinstance(cancel_resp, httpx.Response)
        assert cancel_resp.status_code == 200

        await asyncio.sleep(0.3)

        # Verify in-flight count is exactly 0 (no negative or stuck slots)
        cur = getattr(quota_mgr, "_local_inflight", {}).get(user_id, 0)
        assert cur == 0, f"Slot corruption detected in race test! in_flight={cur}"

    finally:
        server.should_exit = True
        await server_task
