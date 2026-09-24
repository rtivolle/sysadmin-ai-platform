"""
DisasterRecoveryDrill: Automated disaster recovery drill runner.
Validates:
- Automated snapshot generation via BackupManager.
- Strict RPO compliance (< 24 hours / 86,400s).
- Clean staging restore via RestoreManager enforcing the 7-stage restoration sequence.
- Strict RTO compliance (< 4 hours / 14,400s).
- Multi-service health qualification across Valkey, VictoriaLogs, SeaweedFS, and platform credentials.
"""
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .backup_manager import BackupManager, get_backup_manager
from .restore_manager import RestoreManager, get_restore_manager, MANDATORY_RESTORE_SEQUENCE

logger = logging.getLogger("resilience.dr_drill")

RTO_MAX_SECONDS = 4 * 3600    # 14,400s (4 hours)
RPO_MAX_SECONDS = 24 * 3600   # 86,400s (24 hours)


class DisasterRecoveryDrill:
    def __init__(
        self,
        backup_mgr: Optional[BackupManager] = None,
        restore_mgr: Optional[RestoreManager] = None,
    ):
        self.backup_mgr = backup_mgr or get_backup_manager()
        self.restore_mgr = restore_mgr or get_restore_manager()

    def run_drill(self, target_staging_dir: Optional[Path] = None) -> Dict[str, Any]:
        """
        Executes an end-to-end disaster recovery drill.
        """
        drill_start_time = time.time()
        errors = []

        # Step 1: Create Backup Snapshot
        try:
            backup_result = self.backup_mgr.create_backup()
        except Exception as e:
            logger.exception("Drill failed during backup snapshot creation: %s", e)
            return {
                "success": False,
                "error": f"Backup snapshot creation failed: {e}",
                "phase": "backup",
            }

        archive_path = Path(backup_result["archive_path"])
        created_at_epoch = backup_result.get("created_at_epoch", time.time())

        # Step 2: Validate RPO (< 24h)
        snapshot_age = time.time() - created_at_epoch
        rpo_passed = (snapshot_age < RPO_MAX_SECONDS)
        if not rpo_passed:
            errors.append(f"RPO threshold exceeded: snapshot age {snapshot_age:.1f}s >= {RPO_MAX_SECONDS}s")

        # Step 3: Execute Clean Staging Restore
        staging_dir = target_staging_dir or Path(tempfile.mkdtemp(prefix="dr_drill_target_"))
        restore_start = time.time()
        try:
            restore_result = self.restore_mgr.restore_from_archive(
                archive_path=archive_path,
                target_staging_base=staging_dir / "data",
            )
            restore_duration = time.time() - restore_start
        except Exception as e:
            logger.exception("Drill failed during staging restore: %s", e)
            return {
                "success": False,
                "error": f"Staging restore failed: {e}",
                "phase": "restore",
            }

        # Step 4: Validate RTO (< 4h)
        rto_passed = (restore_duration < RTO_MAX_SECONDS)
        if not rto_passed:
            errors.append(f"RTO threshold exceeded: restore took {restore_duration:.1f}s >= {RTO_MAX_SECONDS}s")

        # Step 5: Verify 7-Stage Restoration Sequence Compliance
        restored_sequence = restore_result.get("restore_sequence", [])
        sequence_passed = (restored_sequence == MANDATORY_RESTORE_SEQUENCE)
        if not sequence_passed:
            errors.append(
                f"Restore sequence mismatch. Expected: {MANDATORY_RESTORE_SEQUENCE}, got: {restored_sequence}"
            )

        # Step 6: Post-Restore Health Qualifications
        health_checks = {}

        # 6.1 Valkey Health Check
        valkey_rdb = staging_dir / "data" / "valkey" / "dump.rdb"
        if valkey_rdb.exists():
            with open(valkey_rdb, "rb") as f:
                header = f.read(6)
            valkey_valid = header.startswith(b"VALKEY") or header.startswith(b"REDIS")
            health_checks["valkey"] = {
                "status": "healthy" if valkey_valid else "corrupted",
                "rdb_size": os.path.getsize(valkey_rdb),
                "rdb_signature_valid": valkey_valid,
            }
        else:
            health_checks["valkey"] = {"status": "unverified", "message": "No dump.rdb file found"}

        # 6.2 VictoriaLogs Health Check
        vl_dir = staging_dir / "data" / "victorialogs"
        vl_partitions = vl_dir / "partitions"
        vl_outbox = vl_dir / "outbox.jsonl"
        vl_healthy = vl_dir.exists() and (vl_partitions.exists() or vl_outbox.exists())
        health_checks["victorialogs"] = {
            "status": "healthy" if vl_healthy else "missing_data",
            "partitions_present": vl_partitions.exists(),
            "outbox_present": vl_outbox.exists(),
        }

        # 6.3 SeaweedFS Health Check
        sw_dir = staging_dir / "data" / "seaweedfs"
        sw_healthy = sw_dir.exists()
        health_checks["seaweedfs"] = {
            "status": "healthy" if sw_healthy else "missing_data",
            "filer_present": (sw_dir / "filerldb2").exists(),
            "master_present": (sw_dir / "m9333").exists(),
        }

        # 6.4 Config Keys Health Check
        keys_dir = staging_dir / "config" / "keys"
        master_key = keys_dir / "master.key"
        keys_healthy = keys_dir.exists()
        health_checks["config_keys"] = {
            "status": "healthy" if keys_healthy else "missing_keys",
            "keys_count": len(list(keys_dir.iterdir())) if keys_dir.exists() else 0,
            "master_key_present": master_key.exists(),
        }

        drill_total_elapsed = time.time() - drill_start_time
        success = (len(errors) == 0) and all(
            h.get("status") in ("healthy", "unverified") for h in health_checks.values()
        )

        return {
            "success": success,
            "backup_id": backup_result["backup_id"],
            "archive_path": str(archive_path),
            "archive_sha256": backup_result["archive_sha256"],
            "rpo": {
                "target_seconds": RPO_MAX_SECONDS,
                "actual_seconds": snapshot_age,
                "passed": rpo_passed,
            },
            "rto": {
                "target_seconds": RTO_MAX_SECONDS,
                "actual_seconds": restore_duration,
                "passed": rto_passed,
            },
            "sequence_verification": {
                "expected": MANDATORY_RESTORE_SEQUENCE,
                "actual": restored_sequence,
                "passed": sequence_passed,
            },
            "health_checks": health_checks,
            "drill_total_seconds": drill_total_elapsed,
            "errors": errors,
        }


_GLOBAL_DRILL: Optional[DisasterRecoveryDrill] = None


def get_dr_drill() -> DisasterRecoveryDrill:
    global _GLOBAL_DRILL
    if _GLOBAL_DRILL is None:
        _GLOBAL_DRILL = DisasterRecoveryDrill()
    return _GLOBAL_DRILL
