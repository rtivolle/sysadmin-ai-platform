"""
Tier 4 Recovery & Adversarial Stress Tests: Emergency P1 Elevation & DR Resilience.

Adversarial Stress Test Matrix:
1. P1 Elevation Validation:
   - Missing, empty, whitespace, and injection-laden incident IDs rejected (ValueError & HTTP 400).
   - Unauthenticated callers rejected with HTTP 401; user_id spoofing in JSON payload ignored.
   - Single-token and multi-token revocation invalidate all issued tokens.
   - 60-minute TTL expiry: valid at issued time, fails closed once expired.
2. Destructive Command Boundary during P1:
   - Destructive command suite (rm -rf /, mkfs, fork-bombs, dd, raw block writes, poweroff, etc.)
     unconditionally blocked with HTTP 403 Forbidden even with X-Priority: P1-CRITICAL.
   - Split-flag edge case (rm -r -f /var vs rm -rf /var) behavior analysis.
   - Target adapter blocks destructive actions, dangerous targets, and forbidden system paths under P1.
3. Corrupted Backup Recovery:
   - Corrupted manifest SHA-256 fails closed before touching target staging directory.
   - Tampered file content in component fails closed before touching target directory.
   - Truncated tarball fails closed before touching target directory.
   - Missing manifest.json fails closed before touching target directory.
4. Restore Sequence Violation:
   - DR drill fails with sequence mismatch when mandatory stages are omitted, skipped, or reordered.
   - RestoreManager behavior when component directory is missing from archive.
5. RTO/RPO Boundary Stress:
   - Exact boundary stress at RPO = 86,400s (24h) and RTO = 14,400s (4h).
   - Near-boundary stress (86,399s vs 86,401s; 14,399s vs 14,401s).
   - Clock skew / future timestamp evaluation.
"""
import asyncio
import io
import json
import os
import shutil
import tarfile
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from backend.services.auth_gateway.p1_elevation import P1EmergencyGate
from backend.services.auth_gateway.server import app as auth_app
from backend.services.agent_tools.server import app as tools_app
from backend.services.approval_gate.filter import evaluate_command_safety
from backend.services.resilience.backup_manager import BackupManager
from backend.services.resilience.restore_manager import (
    RestoreManager,
    MANDATORY_RESTORE_SEQUENCE,
)
from backend.services.resilience.dr_drill import (
    DisasterRecoveryDrill,
    RTO_MAX_SECONDS,
    RPO_MAX_SECONDS,
)

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
KEYS_DIR = ROOT_DIR / "config" / "keys"


def _read_sysadmin_key(user: str = "sysadmin-01") -> str:
    key_file = KEYS_DIR / f"{user}.key"
    if key_file.exists():
        return key_file.read_text().strip()
    return "mock-key-sysadmin-01"


# ============================================================================
# Section 1: P1 Elevation Validation Stress Tests
# ============================================================================

def test_p1_elevation_invalid_incident_ids_rejected():
    """Verify all forms of invalid or malicious incident IDs are rejected."""
    gate = P1EmergencyGate()

    invalid_ids = [
        ("", "empty string"),
        ("   ", "whitespace only"),
        ("IN", "too short (<3 chars)"),
        ("A" * 33, "too long (>32 chars)"),
        ("INC 123", "contains spaces"),
        ("INC;rm -rf", "command injection syntax"),
        ("INC<script>", "HTML/XSS syntax"),
        ("INC/../../etc", "path traversal syntax"),
        ("INC\n123", "newline injection"),
    ]

    for inc_id, desc in invalid_ids:
        with pytest.raises(ValueError, match=r"(?i)incident") as exc_info:
            gate.issue_p1_token("sysadmin-01", inc_id)
        assert "mandatory" in str(exc_info.value).lower() or "invalid incident id format" in str(exc_info.value).lower(), (
            f"Expected rejection for {desc} ('{inc_id}'), got {exc_info.value}"
        )


@pytest.mark.asyncio
async def test_p1_elevation_api_validation_and_unauthenticated():
    """Verify HTTP endpoint rejects invalid incident IDs and unauthenticated calls."""
    transport = httpx.ASGITransport(app=auth_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Unauthenticated request
        res = await client.post("/api/v1/auth/p1/elevate", json={"incident_id": "INC-VALID-01"})
        assert res.status_code == 401

        # Invalid bearer token
        res_fake = await client.post(
            "/api/v1/auth/p1/elevate",
            headers={"Authorization": "Bearer fake-token-12345"},
            json={"incident_id": "INC-VALID-01"},
        )
        assert res_fake.status_code == 401

        # Authenticated with empty incident_id
        valid_key = _read_sysadmin_key("sysadmin-01")
        res_empty = await client.post(
            "/api/v1/auth/p1/elevate",
            headers={"Authorization": f"Bearer {valid_key}"},
            json={"incident_id": "   "},
        )
        assert res_empty.status_code == 400

        # Authenticated with payload trying to elevate another user
        res_spoof = await client.post(
            "/api/v1/auth/p1/elevate",
            headers={"Authorization": f"Bearer {valid_key}"},
            json={"user_id": "attacker-user", "incident_id": "INC-VALID-02"},
        )
        assert res_spoof.status_code == 200
        # Must elevate caller sysadmin-01, NOT attacker-user
        assert res_spoof.json()["user_id"] == "sysadmin-01"


def test_p1_elevation_token_revocation_lifecycle():
    """Verify single-token revocation invalidates token and drops elevation status."""
    gate = P1EmergencyGate()
    token = gate.issue_p1_token("sysadmin-05", "INC-REV-01", ttl_seconds=3600)

    val = gate.validate_p1_request(token)
    assert val["valid"] is True
    assert gate.get_p1_status("sysadmin-05")["elevated"] is True

    # Revoke
    rev_ok = gate.revoke_p1_elevation("sysadmin-05")
    assert rev_ok is True

    # Token must now be invalid
    val_revoked = gate.validate_p1_request(token)
    assert val_revoked["valid"] is False
    assert "invalid" in val_revoked["error"].lower()

    # User status must be back to standard
    status = gate.get_p1_status("sysadmin-05")
    assert status["elevated"] is False
    assert status["priority"] == "standard"
    assert status["max_in_flight"] == 2


def test_p1_elevation_multi_token_revocation():
    """
    Adversarial Challenge: When multiple tokens are issued for the same user,
    Revoking a user invalidates every token issued for that user.
    """
    gate = P1EmergencyGate()
    t1 = gate.issue_p1_token("sysadmin-06", "INC-MULTI-1", ttl_seconds=3600)
    t2 = gate.issue_p1_token("sysadmin-06", "INC-MULTI-2", ttl_seconds=3600)

    assert gate.validate_p1_request(t1)["valid"] is True
    assert gate.validate_p1_request(t2)["valid"] is True

    # Revoke user elevation
    gate.revoke_p1_elevation("sysadmin-06")

    # Latest token is properly revoked
    assert gate.validate_p1_request(t2)["valid"] is False

    t1_validation = gate.validate_p1_request(t1)
    assert t1_validation["valid"] is False


def test_p1_elevation_ttl_expiration_boundaries():
    """Verify P1 elevation expires strictly at and after 60-minute TTL."""
    gate = P1EmergencyGate()
    now = time.time()
    token = gate.issue_p1_token("sysadmin-07", "INC-TTL-BOUND", ttl_seconds=3600)

    # Active inside 60-minute window
    val_now = gate.validate_p1_request(token)
    assert val_now["valid"] is True
    assert val_now["ttl_remaining"] > 3500

    # Simulate exactly at expiration threshold
    meta = gate.active_p1_tokens[token]
    meta["expires_at"] = now - 0.1
    if "sysadmin-07" in gate._local_user_elevations:
        gate._local_user_elevations["sysadmin-07"]["expires_at"] = now - 0.1

    val_expired = gate.validate_p1_request(token)
    assert val_expired["valid"] is False
    assert "expired" in val_expired["error"].lower()

    status = gate.get_p1_status("sysadmin-07")
    assert status["elevated"] is False
    assert status["priority"] == "standard"


# ============================================================================
# Section 2: Destructive Command Boundary during P1
# ============================================================================

@pytest.mark.asyncio
async def test_destructive_commands_unconditionally_blocked_under_p1():
    """Verify destructive commands remain blocked (403 Forbidden) even with P1-CRITICAL headers."""
    valid_key = _read_sysadmin_key("sysadmin-01")
    transport = httpx.ASGITransport(app=tools_app)

    p1_headers = {
        "Authorization": f"Bearer {valid_key}",
        "X-User": "sysadmin-01",
        "X-User-Role": "p1-operator",
        "X-Priority": "P1-CRITICAL",
        "X-Incident-ID": "INC-P1-DISASTER",
    }

    destructive_commands = [
        "rm -rf /",
        "rm -rf /etc",
        "rm -fr /home",
        "rm --recursive --force /opt",
        "mkfs.ext4 /dev/sda1",
        "mkfs /dev/nvme0n1",
        ":(){ :|:& };:",
        ":(){ : | : & };:",
        "bomb(){ bomb | bomb & }; bomb",
        "dd if=/dev/zero of=/dev/sda",
        "> /dev/sda",
        "iptables -F",
        "reboot",
        "shutdown -h now",
        "poweroff",
        "init 0",
        "nft flush",
        "ufw reset",
    ]

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for cmd in destructive_commands:
            payload = {
                "name": "sandboxed_bash",
                "parameters": {"command": cmd},
                "session_id": "sess-p1-adversarial",
            }
            res = await client.post("/api/tools/execute", headers=p1_headers, json=payload)
            assert res.status_code == 403, f"Destructive command '{cmd}' was not blocked with 403 (got {res.status_code})"


def test_destructive_command_split_flag_edge_case():
    """
    Adversarial Challenge: Test split-flag variants of rm -rf.
    Split recursive and force flags must be blocked like combined flags.
    """
    # Canonical rm -rf is BLOCKED
    assert evaluate_command_safety("rm -rf /tmp/data")["action"] == "BLOCKED"
    assert evaluate_command_safety("rm -fr /tmp/data")["action"] == "BLOCKED"
    assert evaluate_command_safety("rm --recursive --force /tmp/data")["action"] == "BLOCKED"

    split_r_f = evaluate_command_safety("rm -r -f /tmp/data")
    split_f_r = evaluate_command_safety("rm -f -r /tmp/data")
    assert split_r_f["action"] == "BLOCKED"
    assert split_f_r["action"] == "BLOCKED"


@pytest.mark.asyncio
async def test_target_adapter_blocks_destructive_proposals_under_p1():
    """Verify target adapter rejects destructive commands and forbidden paths even with P1 headers."""
    valid_key = _read_sysadmin_key("sysadmin-01")
    transport = httpx.ASGITransport(app=tools_app)

    p1_headers = {
        "Authorization": f"Bearer {valid_key}",
        "X-User": "sysadmin-01",
        "X-Priority": "P1-CRITICAL",
        "X-Incident-ID": "INC-P1-ADAPTER",
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Unapproved action (rejected by schema validation)
        res_action = await client.post(
            "/api/v1/approval/propose",
            headers=p1_headers,
            json={
                "user_id": "sysadmin-01",
                "action": "execute_shell",
                "target": "rm -rf /",
            },
        )
        assert res_action.status_code in (403, 422)

        # 2. Unwhitelisted service (e.g. docker)
        res_unwhitelisted = await client.post(
            "/api/v1/approval/propose",
            headers=p1_headers,
            json={
                "user_id": "sysadmin-01",
                "action": "service_restart",
                "target": "docker",
            },
        )
        assert res_unwhitelisted.status_code == 403

        # 3. Command injection via target service name
        res_inject = await client.post(
            "/api/v1/approval/propose",
            headers=p1_headers,
            json={
                "user_id": "sysadmin-01",
                "action": "service_restart",
                "target": "nginx; rm -rf /",
            },
        )
        assert res_inject.status_code == 400

        # 4. Forbidden system destination path
        res_forbidden = await client.post(
            "/api/v1/approval/propose",
            headers=p1_headers,
            json={
                "user_id": "sysadmin-01",
                "action": "config_deploy",
                "target": "/etc/shadow",
            },
        )
        assert res_forbidden.status_code == 403


# ============================================================================
# Section 3: Corrupted Backup Recovery Stress Tests
# ============================================================================

def test_restore_fails_closed_on_corrupted_manifest_sha256(tmp_path):
    """Verify restore fails closed and target directory remains untouched on manifest SHA mismatch."""
    backup_mgr = BackupManager(backup_dir=tmp_path / "backups")
    manifest = backup_mgr.create_backup(backup_id="adv_corrupt_manifest")
    archive_path = Path(manifest["archive_path"])

    # Unpack, corrupt expected sha256 in manifest.json, repack
    unpack_dir = tmp_path / "unpack_corrupt_manifest"
    with tarfile.open(archive_path, "r:gz") as tar:
        tar.extractall(unpack_dir, filter="data") if hasattr(tarfile, "data_filter") else tar.extractall(unpack_dir)
    root = list(unpack_dir.iterdir())[0]

    with open(root / "manifest.json", "r") as f:
        data = json.load(f)
    data["components"]["valkey"]["aggregate_sha256"] = "deadbeef" * 8
    with open(root / "manifest.json", "w") as f:
        json.dump(data, f)

    tampered_archive = tmp_path / "tampered_manifest.tar.gz"
    with tarfile.open(tampered_archive, "w:gz") as tar:
        tar.add(root, arcname="adv_corrupt_manifest")

    target_staging = tmp_path / "target_staging_31"
    restore_mgr = RestoreManager()

    with pytest.raises(ValueError, match="Integrity violation on component 'valkey'"):
        restore_mgr.restore_from_archive(tampered_archive, target_staging_base=target_staging)

    # Fail-closed guarantee: target staging was never touched
    assert not target_staging.exists()


def test_restore_fails_closed_on_tampered_component_file(tmp_path):
    """Verify restore fails closed when file content within a component is tampered."""
    backup_mgr = BackupManager(backup_dir=tmp_path / "backups")
    manifest = backup_mgr.create_backup(backup_id="adv_tamper_file")
    archive_path = Path(manifest["archive_path"])

    unpack_dir = tmp_path / "unpack_tamper_file"
    with tarfile.open(archive_path, "r:gz") as tar:
        tar.extractall(unpack_dir, filter="data") if hasattr(tarfile, "data_filter") else tar.extractall(unpack_dir)
    root = list(unpack_dir.iterdir())[0]

    # Tamper with file in seaweedfs or victorialogs
    sw_dir = root / "seaweedfs"
    sw_dir.mkdir(parents=True, exist_ok=True)
    (sw_dir / "injected_evil.bin").write_bytes(b"\x00" * 1024)

    tampered_archive = tmp_path / "tampered_file.tar.gz"
    with tarfile.open(tampered_archive, "w:gz") as tar:
        tar.add(root, arcname="adv_tamper_file")

    target_staging = tmp_path / "target_staging_32"
    restore_mgr = RestoreManager()

    with pytest.raises(ValueError, match="Integrity violation"):
        restore_mgr.restore_from_archive(tampered_archive, target_staging_base=target_staging)

    assert not target_staging.exists()


def test_restore_fails_closed_on_truncated_tarball(tmp_path):
    """Verify restore fails closed when backup archive is truncated."""
    backup_mgr = BackupManager(backup_dir=tmp_path / "backups")
    manifest = backup_mgr.create_backup(backup_id="adv_truncated")
    archive_path = Path(manifest["archive_path"])

    raw = archive_path.read_bytes()
    truncated_path = tmp_path / "truncated.tar.gz"
    truncated_path.write_bytes(raw[: len(raw) // 2])

    target_staging = tmp_path / "target_staging_33"
    restore_mgr = RestoreManager()

    with pytest.raises((EOFError, tarfile.ReadError, Exception)):
        restore_mgr.restore_from_archive(truncated_path, target_staging_base=target_staging)

    assert not target_staging.exists()


def test_restore_fails_closed_on_missing_manifest(tmp_path):
    """Verify restore fails closed when manifest.json is absent."""
    backup_mgr = BackupManager(backup_dir=tmp_path / "backups")
    manifest = backup_mgr.create_backup(backup_id="adv_no_manifest")
    archive_path = Path(manifest["archive_path"])

    unpack_dir = tmp_path / "unpack_no_manifest"
    with tarfile.open(archive_path, "r:gz") as tar:
        tar.extractall(unpack_dir, filter="data") if hasattr(tarfile, "data_filter") else tar.extractall(unpack_dir)
    root = list(unpack_dir.iterdir())[0]

    (root / "manifest.json").unlink()

    no_manifest_archive = tmp_path / "no_manifest.tar.gz"
    with tarfile.open(no_manifest_archive, "w:gz") as tar:
        tar.add(root, arcname="adv_no_manifest")

    target_staging = tmp_path / "target_staging_34"
    restore_mgr = RestoreManager()

    with pytest.raises(ValueError, match="Manifest not found"):
        restore_mgr.restore_from_archive(no_manifest_archive, target_staging_base=target_staging)

    assert not target_staging.exists()


def test_restore_rejects_archive_path_escape_before_target_write(tmp_path):
    outside = tmp_path / "escaped.txt"
    archive = tmp_path / "path_escape.tar.gz"
    payload = b"unexpected write"
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo(str(outside))
        member.size = len(payload)
        tar.addfile(member, io.BytesIO(payload))

    target = tmp_path / "restore_target"
    with pytest.raises(ValueError, match="Unsafe backup archive member"):
        RestoreManager().restore_from_archive(archive, target_staging_base=target)
    assert not outside.exists()
    assert not target.exists()


# ============================================================================
# Section 4: Restore Sequence Violation Stress Tests
# ============================================================================

def test_dr_drill_fails_on_sequence_omissions_and_reordering(tmp_path):
    """Verify DR drill marks failure when restore sequence violates 7-stage specification."""
    backup_mgr = MagicMock()
    restore_mgr = MagicMock()
    drill = DisasterRecoveryDrill(backup_mgr=backup_mgr, restore_mgr=restore_mgr)

    now = time.time()
    backup_mgr.create_backup.return_value = {
        "backup_id": "mock_b",
        "archive_path": str(tmp_path / "mock.tar.gz"),
        "archive_sha256": "mock_sha",
        "created_at_epoch": now - 100.0,
    }

    # Case 1: Stage 1 (cgroups) omitted
    restore_mgr.restore_from_archive.return_value = {
        "restore_sequence": ["valkey", "seaweedfs", "victorialogs", "inference", "litellm", "traefik"],
    }
    r1 = drill.run_drill(target_staging_dir=tmp_path / "drill_1")
    assert r1["success"] is False
    assert r1["sequence_verification"]["passed"] is False
    assert any("Restore sequence mismatch" in err for err in r1["errors"])

    # Case 2: Out of order (valkey before cgroups)
    restore_mgr.restore_from_archive.return_value = {
        "restore_sequence": ["valkey", "cgroups", "seaweedfs", "victorialogs", "inference", "litellm", "traefik"],
    }
    r2 = drill.run_drill(target_staging_dir=tmp_path / "drill_2")
    assert r2["success"] is False
    assert r2["sequence_verification"]["passed"] is False

    # Case 3: Stage 7 (traefik) omitted
    restore_mgr.restore_from_archive.return_value = {
        "restore_sequence": ["cgroups", "valkey", "seaweedfs", "victorialogs", "inference", "litellm"],
    }
    r3 = drill.run_drill(target_staging_dir=tmp_path / "drill_3")
    assert r3["success"] is False
    assert r3["sequence_verification"]["passed"] is False


def test_restore_manager_rejects_missing_component(tmp_path):
    """
    Adversarial Challenge: When a mandatory component directory is deleted from an archive,
    RestoreManager rejects the archive before touching the target staging directory.
    """
    backup_mgr = BackupManager(backup_dir=tmp_path / "backups")
    manifest = backup_mgr.create_backup(backup_id="adv_missing_valkey")
    archive_path = Path(manifest["archive_path"])

    unpack_dir = tmp_path / "unpack_missing_valkey"
    with tarfile.open(archive_path, "r:gz") as tar:
        tar.extractall(unpack_dir, filter="data") if hasattr(tarfile, "data_filter") else tar.extractall(unpack_dir)
    root = list(unpack_dir.iterdir())[0]

    # Completely remove valkey from unpacked directory & manifest
    shutil.rmtree(root / "valkey")
    with open(root / "manifest.json", "r") as f:
        data = json.load(f)
    del data["components"]["valkey"]
    with open(root / "manifest.json", "w") as f:
        json.dump(data, f)

    no_valkey_archive = tmp_path / "no_valkey.tar.gz"
    with tarfile.open(no_valkey_archive, "w:gz") as tar:
        tar.add(root, arcname="adv_missing_valkey")

    target_staging = tmp_path / "target_staging_42"
    restore_mgr = RestoreManager()
    with pytest.raises(ValueError, match="missing required components"):
        restore_mgr.restore_from_archive(no_valkey_archive, target_staging_base=target_staging)
    assert not target_staging.exists()


# ============================================================================
# Section 5: RTO/RPO Boundary Stress Tests
# ============================================================================

def test_dr_drill_rpo_and_rto_boundary_limits(tmp_path):
    """Verify strict mathematical limit enforcement for RPO (< 24h) and RTO (< 4h)."""
    backup_mgr = MagicMock()
    restore_mgr = MagicMock()
    drill = DisasterRecoveryDrill(backup_mgr=backup_mgr, restore_mgr=restore_mgr)

    class MockClock:
        def __init__(self, t0):
            self.cur = t0
        def time(self):
            return self.cur
        def advance(self, dt):
            self.cur += dt

    def execute_drill(snapshot_age_sec, restore_duration_sec):
        clock = MockClock(100000.0)
        backup_mgr.create_backup.return_value = {
            "backup_id": "bound_test",
            "archive_path": str(tmp_path / "arch.tar.gz"),
            "archive_sha256": "mock_sha",
            "created_at_epoch": 100000.0 - snapshot_age_sec,
        }
        def mock_restore(*args, **kwargs):
            clock.advance(restore_duration_sec)
            return {
                "restore_sequence": MANDATORY_RESTORE_SEQUENCE,
            }
        restore_mgr.restore_from_archive.side_effect = mock_restore

        import backend.services.resilience.dr_drill as dd
        orig_time = dd.time.time
        dd.time.time = clock.time
        try:
            return drill.run_drill(target_staging_dir=tmp_path / f"stg_{snapshot_age_sec}_{restore_duration_sec}")
        finally:
            dd.time.time = orig_time

    # RPO: 86399s (< 24h) passes
    r_rpo_pass = execute_drill(snapshot_age_sec=86399.0, restore_duration_sec=10.0)
    assert r_rpo_pass["rpo"]["passed"] is True

    # RPO: 86400s (>= 24h) FAILS
    r_rpo_boundary = execute_drill(snapshot_age_sec=86400.0, restore_duration_sec=10.0)
    assert r_rpo_boundary["rpo"]["passed"] is False
    assert any("RPO threshold exceeded" in e for e in r_rpo_boundary["errors"])

    # RPO: 86401s (> 24h) FAILS
    r_rpo_fail = execute_drill(snapshot_age_sec=86401.0, restore_duration_sec=10.0)
    assert r_rpo_fail["rpo"]["passed"] is False

    # RTO: 14399s (< 4h) passes
    r_rto_pass = execute_drill(snapshot_age_sec=100.0, restore_duration_sec=14399.0)
    assert r_rto_pass["rto"]["passed"] is True

    # RTO: 14400s (>= 4h) FAILS
    r_rto_boundary = execute_drill(snapshot_age_sec=100.0, restore_duration_sec=14400.0)
    assert r_rto_boundary["rto"]["passed"] is False
    assert any("RTO threshold exceeded" in e for e in r_rto_boundary["errors"])

    # RTO: 14401s (> 4h) FAILS
    r_rto_fail = execute_drill(snapshot_age_sec=100.0, restore_duration_sec=14401.0)
    assert r_rto_fail["rto"]["passed"] is False
