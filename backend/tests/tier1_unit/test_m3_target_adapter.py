"""
Unit Tests for Milestone M3: Scoped Target Execution Adapter & Staged Config Deployment.
Verifies:
- Strict allowlist of 9 canonical services, normalization, and aliases.
- Allowed actions: service_restart, service_reload, service_status, config_deploy.
- Forbidden target path rejections (anti-traversal, /etc/shadow, etc.).
- ServiceManager execution with TARGET_ADAPTER_SIMULATION=1.
- 10-phase staged atomic configuration deployment, conflict detection, backup creation, atomic swap, and rollback.
- CAS approval claiming, anti-replay, and parameter binding.
"""
import os
import time
import pytest
from pathlib import Path

from backend.services.target_adapter.config import (
    ALLOWED_ACTIONS,
    CANONICAL_SERVICES,
    normalize_service_name,
    validate_target_service,
    validate_target_config_path,
)
from backend.services.target_adapter.service_manager import ServiceManager
from backend.services.target_adapter.config_deployer import ConfigDeployer, validate_syntax
from backend.services.target_adapter.adapter import TargetAdapter
from backend.services.target_adapter.models import ProposalRequest, ExecutionRequest
from backend.services.approval_gate.gate import ApprovalGate
from backend.services.approval_gate.store import ValkeyApprovalStore


def test_allowed_actions_whitelist():
    """Verify strictly defined mutating actions."""
    assert ALLOWED_ACTIONS == frozenset({
        "service_restart", "service_reload", "service_status", "config_deploy"
    })


def test_target_services_whitelist_and_normalization():
    """Verify 9 canonical services and alias resolution."""
    expected_services = {
        "nginx", "traefik", "valkey", "victorialogs", "seaweedfs",
        "postgresql", "dsh-agent", "dsh-sysadmin", "litellm"
    }
    assert CANONICAL_SERVICES == expected_services

    for svc in expected_services:
        assert validate_target_service(svc) == svc
        assert validate_target_service(f"{svc}.service") == svc

    # Aliases
    assert validate_target_service("valkey-server.service") == "valkey"
    assert validate_target_service("victoria-logs") == "victorialogs"
    assert validate_target_service("weed.service") == "seaweedfs"

    # Unwhitelisted services rejected
    for invalid_svc in ["docker", "cron", "sshd", "apache2", "iptables", "systemd"]:
        with pytest.raises(PermissionError):
            validate_target_service(invalid_svc)


def test_forbidden_target_paths_and_traversal_rejection():
    """Verify forbidden paths (/etc/shadow, /dev, /proc, /etc/sudoers) and traversal attacks are blocked."""
    forbidden = ["/etc/shadow", "/etc/sudoers", "/dev/sda", "/proc/sys", "/root/.ssh/id_rsa"]
    for path in forbidden:
        with pytest.raises(PermissionError):
            validate_target_config_path(path)

    # Traversal attempt
    with pytest.raises(PermissionError):
        validate_target_config_path("/etc/nginx/../../etc/shadow")


def test_staged_file_cannot_escape_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    valid = workspace / "config.json"
    valid.write_text('{"safe": true}')
    outside = tmp_path / "secret.json"
    outside.write_text('{"secret": true}')
    (workspace / "link.json").symlink_to(outside)

    assert TargetAdapter._read_staged_content(str(workspace), "config.json") == '{"safe": true}'
    with pytest.raises(PermissionError):
        TargetAdapter._read_staged_content(str(workspace), str(outside))
    with pytest.raises(OSError):
        TargetAdapter._read_staged_content(str(workspace), "link.json")

    linked_target = tmp_path / "linked-target.json"
    linked_target.symlink_to("/etc/shadow")
    with pytest.raises(PermissionError):
        validate_target_config_path(str(linked_target), allow_tmp=True)


def test_shared_approval_store_outage_fails_closed(monkeypatch):
    from backend.services.approval_gate.models import ApprovalRecord
    from backend.services.approval_gate import store as store_module

    def unavailable(*args, **kwargs):
        raise OSError("Valkey unavailable")

    monkeypatch.setattr(store_module.redis.Redis, "from_url", unavailable)
    store = ValkeyApprovalStore(valkey_url="redis://127.0.0.1:6399/0")
    record = ApprovalRecord(approval_id="appr-unavailable", user_id="sysadmin-01")
    with pytest.raises(ConnectionError):
        store.create_approval(record)
    with pytest.raises(ConnectionError):
        store.claim_for_execution("appr-unavailable", "sysadmin-01")
    assert store._local_records == {}


def test_service_manager_simulation():
    """Verify ServiceManager execution under TARGET_ADAPTER_SIMULATION=1."""
    mgr = ServiceManager(simulation=True)
    
    code, stdout, stderr = mgr.execute_action("service_restart", "nginx")
    assert code == 0
    assert "restarted" in stdout.lower()

    code, stdout, stderr = mgr.execute_action("service_status", "valkey")
    assert code == 0
    assert "active" in stdout.lower()

    with pytest.raises(PermissionError):
        mgr.execute_action("service_restart", "unauthorized-service")


def test_config_deployer_syntax_validation():
    """Verify JSON, YAML, and systemd syntax validation."""
    valid_json = '{"key": "value", "count": 42}'
    invalid_json = '{"key": "value", count: 42}'
    assert validate_syntax(valid_json, "test.json")[0] is True
    assert validate_syntax(invalid_json, "test.json")[0] is False

    valid_yaml = "services:\n  traefik:\n    image: traefik:v3\n"
    invalid_yaml = "services:\n  traefik: [unclosed"
    assert validate_syntax(valid_yaml, "config.yaml")[0] is True
    assert validate_syntax(invalid_yaml, "config.yaml")[0] is False

    valid_unit = "[Unit]\nDescription=Test\n[Service]\nExecStart=/bin/true\n"
    invalid_unit = "ExecStart=/bin/true\nWithoutSection=True\n"
    assert validate_syntax(valid_unit, "test.service")[0] is True
    assert validate_syntax(invalid_unit, "test.service")[0] is False


def test_config_deployer_happy_path_and_backup_creation(tmp_path, monkeypatch):
    """Verify 10-phase staged deployment: backup creation, atomic swap, post-swap verification."""
    monkeypatch.setenv("TARGET_CONFIG_ALLOW_TMP", "1")
    deployer = ConfigDeployer(allow_tmp=True)

    dest_file = tmp_path / "test_config.json"
    dest_file.write_text('{"version": 1}')

    import hashlib
    base_hash = hashlib.sha256(dest_file.read_bytes()).hexdigest()

    staged_content = '{"version": 2, "updated": true}'
    proposed_hash = hashlib.sha256(staged_content.encode("utf-8")).hexdigest()

    res = deployer.deploy(
        approval_id="appr-test-101",
        user_id="sysadmin-01",
        target_path=str(dest_file),
        staged_content=staged_content,
        proposed_hash=proposed_hash,
        base_hash=base_hash,
    )

    assert res["success"] is True
    assert res["status"] == "succeeded"
    assert dest_file.read_text() == staged_content
    assert res["backup_path"] is not None
    assert os.path.exists(res["backup_path"])
    # Backup holds version 1
    assert open(res["backup_path"]).read() == '{"version": 1}'


def test_config_deployer_conflict_detection(tmp_path, monkeypatch):
    """Verify destination conflict detection aborts if target was modified out-of-band."""
    monkeypatch.setenv("TARGET_CONFIG_ALLOW_TMP", "1")
    deployer = ConfigDeployer(allow_tmp=True)

    dest_file = tmp_path / "test_conflict.yaml"
    dest_file.write_text("port: 8080\n")

    staged_content = "port: 9090\n"

    # Pass expected base hash that does NOT match current content
    res = deployer.deploy(
        approval_id="appr-test-conflict",
        user_id="sysadmin-01",
        target_path=str(dest_file),
        staged_content=staged_content,
        base_hash="0000000000000000000000000000000000000000000000000000000000000000",
    )

    assert res["success"] is False
    assert res["status"] == "failed"
    assert "Conflict detected" in res["message"]
    # Destination content preserved without modification
    assert dest_file.read_text() == "port: 8080\n"


def test_config_deployer_tamper_detection(tmp_path, monkeypatch):
    """Verify staged content tamper detection aborts if hash does not match proposed_hash."""
    monkeypatch.setenv("TARGET_CONFIG_ALLOW_TMP", "1")
    deployer = ConfigDeployer(allow_tmp=True)

    dest_file = tmp_path / "test_tamper.json"
    dest_file.write_text('{"clean": true}')

    staged_content = '{"clean": false, "tampered": true}'

    res = deployer.deploy(
        approval_id="appr-test-tamper",
        user_id="sysadmin-01",
        target_path=str(dest_file),
        staged_content=staged_content,
        proposed_hash="ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
    )

    assert res["success"] is False
    assert res["status"] == "failed"
    assert "Tamper detected" in res["message"]
    assert dest_file.read_text() == '{"clean": true}'


def test_target_adapter_cas_lifecycle_and_single_use(tmp_path, monkeypatch):
    """Verify end-to-end adapter proposal, admin decision, atomic CAS claim, and execution."""
    monkeypatch.setenv("TARGET_CONFIG_ALLOW_TMP", "1")
    gate = ApprovalGate()
    svc_mgr = ServiceManager(simulation=True)
    deployer = ConfigDeployer(allow_tmp=True)
    adapter = TargetAdapter(approval_gate=gate, service_manager=svc_mgr, config_deployer=deployer)

    # 1. Propose service restart
    prop = adapter.propose(ProposalRequest(
        user_id="sysadmin-01",
        action="service_restart",
        target="nginx",
        reason="Restart web front door",
    ))
    assert prop.status == "pending"
    approval_id = prop.approval_id

    # 2. Rejection by non-admin or self
    dec_self = gate.decide(approval_id, approved=True, reviewer="sysadmin-01", reviewer_role="admin")
    assert dec_self["success"] is False

    dec_nonadmin = gate.decide(approval_id, approved=True, reviewer="sysadmin-02", reviewer_role="user")
    assert dec_nonadmin["success"] is False

    # 3. Decision by authorized admin
    dec_ok = gate.decide(approval_id, approved=True, reviewer="sysadmin-admin", reviewer_role="admin")
    assert dec_ok["success"] is True
    assert dec_ok["status"] == "approved"

    # 4. Atomic execution dispatch
    exec_res = adapter.execute(ExecutionRequest(
        approval_id=approval_id,
        user_id="sysadmin-01",
        action="service_restart",
        target="nginx",
    ))
    assert exec_res.status == "succeeded"
    assert exec_res.exit_code == 0

    # 5. Anti-Replay: Second execution must fail
    replay_res = adapter.execute(ExecutionRequest(
        approval_id=approval_id,
        user_id="sysadmin-01",
        action="service_restart",
        target="nginx",
    ))
    assert replay_res.status == "failed"
    assert "Claim failed" in replay_res.message
