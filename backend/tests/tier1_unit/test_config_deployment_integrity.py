"""Regression checks for approved destination state and private config files."""
import hashlib
import os
import stat

import pytest

from backend.services.approval_gate.gate import ApprovalGate
from backend.services.approval_gate.store import ValkeyApprovalStore
from backend.services.target_adapter.adapter import TargetAdapter
from backend.services.target_adapter.config_deployer import ConfigDeployer
from backend.services.target_adapter.models import ExecutionRequest, ProposalRequest


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setenv("TARGET_CONFIG_ALLOW_TMP", "1")
    monkeypatch.setattr(ValkeyApprovalStore, "redis", property(lambda self: None))
    monkeypatch.setattr("backend.services.target_adapter.adapter.log_audit_event", lambda **kwargs: None)
    return TargetAdapter(approval_gate=ApprovalGate(store=ValkeyApprovalStore()),
                         config_deployer=ConfigDeployer(allow_tmp=True))


@pytest.mark.parametrize("change", ["created", "deleted", "line_endings"])
def test_destination_change_after_approval_is_rejected(adapter, tmp_path, change):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.json").write_text('{"version": 2}')
    target = tmp_path / "app.json"
    if change != "created":
        target.write_bytes(b'{"version": 1}\r\n')
    proposal = adapter.propose(ProposalRequest(
        user_id="sysadmin-01", action="config_deploy", target=str(target),
        workspace=str(workspace), staged_path="app.json",
    ))
    assert adapter.gate.decide(proposal.approval_id, True, "reviewer", "admin")["success"]
    if change == "deleted":
        target.unlink()
    else:
        target.write_bytes(b'{"version": 1}\n')
    before = target.read_bytes() if target.exists() else None
    result = adapter.execute(ExecutionRequest(
        approval_id=proposal.approval_id, user_id="sysadmin-01", staged_path="app.json",
    ))
    assert result.status == "failed"
    assert "Conflict detected" in result.message
    assert (target.read_bytes() if target.exists() else None) == before
    assert not list(tmp_path.glob(".app.json.*"))
    assert adapter.gate.get_status(proposal.approval_id)["status"] == "failed"


@pytest.mark.parametrize("mode", [0o600, 0o640])
def test_replacement_preserves_config_permissions_and_owner(tmp_path, mode):
    target = tmp_path / "private.json"
    target.write_text('{"secret": "old"}')
    target.chmod(mode)
    original = target.stat()
    result = ConfigDeployer(allow_tmp=True).deploy(
        "appr-private", "sysadmin-01", str(target), '{"secret": "new"}',
        base_hash=hashlib.sha256(target.read_bytes()).hexdigest(),
    )
    assert result["success"]
    assert stat.S_IMODE(target.stat().st_mode) == mode
    assert (target.stat().st_uid, target.stat().st_gid) == (original.st_uid, original.st_gid)
    assert stat.S_IMODE(os.stat(result["backup_path"]).st_mode) == mode


def test_rejected_content_does_not_create_destination_directory(tmp_path):
    target = tmp_path / "not-created" / "app.json"
    result = ConfigDeployer(allow_tmp=True).deploy(
        "appr-tampered", "sysadmin-01", str(target), '{}', proposed_hash="0" * 64,
    )
    assert not result["success"]
    assert not target.parent.exists()


def test_new_configuration_is_private(tmp_path):
    target = tmp_path / "new" / "app.json"
    result = ConfigDeployer(allow_tmp=True).deploy(
        "appr-new", "sysadmin-01", str(target), '{}', expected_target_exists=False,
    )
    assert result["success"]
    assert target.read_bytes() == b'{}'
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_byte_hashes_accept_unchanged_crlf_and_keep_exact_backup(tmp_path):
    target = tmp_path / "app.json"
    original = b'{"version": 1}\r\n'
    target.write_bytes(original)
    content = '{"version": 2}\r\n'
    result = ConfigDeployer(allow_tmp=True).deploy(
        "appr-crlf", "sysadmin-01", str(target), content,
        base_hash=hashlib.sha256(original).hexdigest(),
    )
    assert result["success"]
    assert target.read_bytes() == content.encode()
    with open(result["backup_path"], "rb") as backup:
        assert backup.read() == original


def test_metadata_failure_aborts_before_replacement(tmp_path, monkeypatch):
    target = tmp_path / "private.json"
    target.write_bytes(b'{"secret": "old"}')
    target.chmod(0o600)

    def denied(*args):
        raise PermissionError("Cannot preserve target permissions")

    monkeypatch.setattr(os, "fchmod", denied)
    result = ConfigDeployer(allow_tmp=True).deploy(
        "appr-denied", "sysadmin-01", str(target), '{"secret": "new"}',
        base_hash=hashlib.sha256(target.read_bytes()).hexdigest(),
    )
    assert not result["success"]
    assert target.read_bytes() == b'{"secret": "old"}'
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".private.json.tmp.*"))
