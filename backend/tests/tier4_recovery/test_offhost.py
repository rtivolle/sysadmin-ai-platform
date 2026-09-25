"""
Tier 4 Recovery Tests: off-host backup copy, verification and retention (PR-D2).

Covers: off-host copy with SHA-256 sidecar verification, checksum mismatch
detection, restore from the off-host copy, retention pruning, destination
parsing, and the fail-loud contract (failure raises, never returns success).
"""
import os
import time

import pytest

from backend.services.resilience.backup_manager import BackupManager, compute_sha256
from backend.services.resilience.offhost_backup import (
    OffhostBackupError,
    copy_archive_to_offhost,
    fetch_offhost_archive,
    parse_offhost_dest,
    prune_offhost,
)
from backend.services.resilience.restore_manager import RestoreManager


def make_archive(tmp_path, name="backup_test.tar.gz"):
    archive = tmp_path / name
    archive.write_bytes(b"hello world backup data " * 64)
    manifest = tmp_path / name.replace(".tar.gz", ".manifest.json")
    manifest.write_text('{"backup_id": "backup_test"}')
    sha = compute_sha256(archive)
    return archive, manifest, sha


# ---------------------------------------------------------------------------
# Destination parsing
# ---------------------------------------------------------------------------

def test_parse_local_dest():
    target = parse_offhost_dest("/mnt/worm-backups")
    assert target.scheme == "local"
    assert target.path == "/mnt/worm-backups"
    assert target.host is None


def test_parse_ssh_dest():
    target = parse_offhost_dest("ssh://backup@nas:/srv/backups")
    assert target.scheme == "ssh"
    assert target.host == "backup@nas"
    assert target.path == "/srv/backups"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "relative/path",
        "https://example.com/backups",
        "ssh://host-without-path",
    ],
)
def test_parse_rejects_bad_dest(bad):
    with pytest.raises(OffhostBackupError):
        parse_offhost_dest(bad)


# ---------------------------------------------------------------------------
# Copy and verification
# ---------------------------------------------------------------------------

def test_offhost_copy_writes_sidecar_and_verifies(tmp_path):
    archive, manifest, sha = make_archive(tmp_path)
    dest = tmp_path / "offhost"
    result = copy_archive_to_offhost(archive, manifest, sha, str(dest))

    assert result["verified"] is True
    assert (dest / "backup_test.tar.gz").exists()
    assert (dest / "backup_test.manifest.json").exists()
    sidecar = dest / "backup_test.tar.gz.sha256"
    assert sidecar.exists()
    assert sidecar.read_text().split()[0] == sha


def test_offhost_copy_checksum_mismatch_detected(tmp_path):
    archive, manifest, sha = make_archive(tmp_path)
    dest = tmp_path / "offhost"
    copy_archive_to_offhost(archive, manifest, sha, str(dest))

    # Corrupt the off-host archive.
    (dest / "backup_test.tar.gz").write_bytes(b"tampered")

    with pytest.raises(OffhostBackupError) as exc_info:
        fetch_offhost_archive(str(dest), "backup_test", tmp_path / "scratch")
    assert "checksum mismatch" in str(exc_info.value)


def test_offhost_copy_failure_raises_not_silent(tmp_path):
    archive, manifest, sha = make_archive(tmp_path)
    # Point the destination inside an existing *file* so the copy fails.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    with pytest.raises(OffhostBackupError):
        copy_archive_to_offhost(archive, manifest, sha, str(blocker / "offhost"))


# ---------------------------------------------------------------------------
# Retention pruning
# ---------------------------------------------------------------------------

def test_prune_offhost_retention(tmp_path):
    archive, manifest, sha = make_archive(tmp_path)
    dest = tmp_path / "offhost"
    copy_archive_to_offhost(archive, manifest, sha, str(dest))

    # No pruning within the retention window.
    result = prune_offhost(str(dest), retention_days=90, now=time.time())
    assert result["pruned"] == []

    # Force the archive to look old by rewriting the retention to 0.
    result = prune_offhost(str(dest), retention_days=0, now=time.time() + 1)
    assert "backup_test.tar.gz" in result["pruned"]
    assert not (dest / "backup_test.tar.gz").exists()
    assert not (dest / "backup_test.tar.gz.sha256").exists()
    assert not (dest / "backup_test.manifest.json").exists()


def test_prune_offhost_keeps_recent_archive(tmp_path):
    archive, manifest, sha = make_archive(tmp_path)
    dest = tmp_path / "offhost"
    copy_archive_to_offhost(archive, manifest, sha, str(dest))
    # A far-future "now" keeps everything with a 90-day retention.
    result = prune_offhost(str(dest), retention_days=90, now=time.time())
    assert result["pruned"] == []
    assert (dest / "backup_test.tar.gz").exists()


# ---------------------------------------------------------------------------
# Restore from off-host
# ---------------------------------------------------------------------------

def test_restore_from_offhost_copy(tmp_path):
    backup_mgr = BackupManager(backup_dir=tmp_path / "backups")
    offhost = tmp_path / "offhost"
    manifest = backup_mgr.create_backup(
        backup_id="offhost_restore_001", offhost_dest=str(offhost)
    )

    assert manifest["offhost"]["verified"] is True
    assert (offhost / "offhost_restore_001.tar.gz").exists()

    restore_mgr = RestoreManager(data_dir=tmp_path / "target_data")
    result = restore_mgr.restore_from_offhost(
        offhost_dest=str(offhost),
        backup_id="offhost_restore_001",
        target_staging_base=tmp_path / "target_staging",
    )
    assert result["success"] is True
    assert result["backup_id"] == "offhost_restore_001"


def test_restore_from_offhost_missing_archive_raises(tmp_path):
    restore_mgr = RestoreManager(data_dir=tmp_path / "target_data")
    with pytest.raises(OffhostBackupError):
        restore_mgr.restore_from_offhost(
            offhost_dest=str(tmp_path / "empty_offhost"),
            backup_id="does_not_exist",
        )
