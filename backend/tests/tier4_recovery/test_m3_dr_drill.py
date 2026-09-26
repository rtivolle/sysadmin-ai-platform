"""
Tier 4 Recovery Test: Automated Disaster Recovery Drill & 7-Stage Restore Sequence Verification.
Verifies:
- Automated backup archive and SHA-256 manifest creation across Valkey, VictoriaLogs, SeaweedFS, and config keys.
- Clean staging restore enforcing the mandatory 7-stage sequence:
  cgroups -> valkey -> seaweedfs -> victorialogs -> inference -> litellm -> traefik
- Automated recovery drill verifying RTO < 4h and RPO < 24h compliance.
- Post-restore multi-service health qualifications.
"""
import os
import tarfile
import tempfile
import time
from pathlib import Path
import pytest

from backend.services.resilience.backup_manager import BackupManager, compute_sha256
from backend.services.resilience.restore_manager import (
    RestoreManager,
    MANDATORY_RESTORE_SEQUENCE,
)
from backend.services.resilience.dr_drill import DisasterRecoveryDrill, RTO_MAX_SECONDS, RPO_MAX_SECONDS


def test_backup_manager_creates_valid_archive_and_manifest(tmp_path):
    """Verify live snapshot packaging and SHA-256 manifest generation."""
    backup_mgr = BackupManager(backup_dir=tmp_path / "backups")
    manifest = backup_mgr.create_backup(backup_id="test_backup_001")

    assert manifest["backup_id"] == "test_backup_001"
    assert "archive_path" in manifest
    assert os.path.exists(manifest["archive_path"])
    assert "archive_sha256" in manifest

    # Check components in manifest
    components = manifest["components"]
    for comp in ["valkey", "victorialogs", "seaweedfs", "config_keys"]:
        assert comp in components
        assert "aggregate_sha256" in components[comp]

    # Verify archive integrity
    with tarfile.open(manifest["archive_path"], "r:gz") as tar:
        names = tar.getnames()
        assert any("manifest.json" in n for n in names)


def test_restore_manager_sequence_and_staging(tmp_path):
    """Verify clean staging restore enforces the 7-stage bringup sequence."""
    backup_mgr = BackupManager(backup_dir=tmp_path / "backups")
    manifest = backup_mgr.create_backup(backup_id="test_restore_002")

    restore_mgr = RestoreManager(data_dir=tmp_path / "target_data")
    result = restore_mgr.restore_from_archive(
        archive_path=Path(manifest["archive_path"]),
        target_staging_base=tmp_path / "target_staging",
    )

    assert result["success"] is True
    assert result["restore_sequence"] == MANDATORY_RESTORE_SEQUENCE
    assert result["restore_duration_seconds"] < RTO_MAX_SECONDS


def test_restore_manager_tamper_detection_aborts(tmp_path):
    """Verify restore manager aborts if unpacked component hash does not match manifest."""
    backup_mgr = BackupManager(backup_dir=tmp_path / "backups")
    manifest = backup_mgr.create_backup(backup_id="test_tamper_003")

    # Corrupt the archive by rewriting with invalid manifest
    archive_path = Path(manifest["archive_path"])
    corrupt_staging = tmp_path / "corrupt_staging"
    corrupt_staging.mkdir()
    with tarfile.open(archive_path, "r:gz") as tar:
        tar.extractall(corrupt_staging, filter="data") if hasattr(tarfile, "data_filter") else tar.extractall(corrupt_staging)

    root_dir = list(corrupt_staging.iterdir())[0]
    manifest_file = root_dir / "manifest.json"
    import json
    with open(manifest_file, "r") as f:
        data = json.load(f)
    # Tamper the expected hash
    data["components"]["valkey"]["aggregate_sha256"] = "deadbeef" * 8
    with open(manifest_file, "w") as f:
        json.dump(data, f)

    corrupt_archive = tmp_path / "corrupted.tar.gz"
    with tarfile.open(corrupt_archive, "w:gz") as tar:
        tar.add(root_dir, arcname="test_tamper_003")

    restore_mgr = RestoreManager()
    with pytest.raises(ValueError) as exc_info:
        restore_mgr.restore_from_archive(corrupt_archive)
    assert "Integrity violation" in str(exc_info.value)


def test_dr_drill_end_to_end_qualification(tmp_path):
    """Verify automated recovery drill satisfies RTO (< 4h) and RPO (< 24h) and health checks."""
    backup_mgr = BackupManager(backup_dir=tmp_path / "backups")
    restore_mgr = RestoreManager(data_dir=tmp_path / "data")
    drill = DisasterRecoveryDrill(backup_mgr=backup_mgr, restore_mgr=restore_mgr)

    drill_report = drill.run_drill(target_staging_dir=tmp_path / "drill_staging")

    assert drill_report["success"] is True
    assert drill_report["rpo"]["passed"] is True
    assert drill_report["rto"]["passed"] is True
    assert drill_report["sequence_verification"]["passed"] is True
    assert drill_report["health_checks"]["valkey"]["status"] in ("healthy", "unverified")
    assert drill_report["health_checks"]["victorialogs"]["status"] == "healthy"
    assert drill_report["health_checks"]["seaweedfs"]["status"] == "healthy"
    assert drill_report["health_checks"]["config_keys"]["status"] == "healthy"
