"""
ConfigDeployer: 10-phase staged atomic configuration deployment engine.
Enforces workspace staging, SHA-256 validation, base hash conflict detection,
backup creation, same-filesystem atomic swap (os.replace), syntax verification, and automated rollback.
"""
import hashlib
import json
import logging
import os
import shutil
import stat
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml

from .config import validate_target_config_path

logger = logging.getLogger("target_adapter.config_deployer")


def validate_syntax(content: str, filename: str) -> Tuple[bool, Optional[str]]:
    """Validates configuration content syntax based on extension."""
    lower = filename.lower()
    if lower.endswith(".json"):
        try:
            json.loads(content)
            return True, None
        except Exception as e:
            return False, f"JSON syntax error: {e}"

    if lower.endswith((".yaml", ".yml")):
        try:
            list(yaml.safe_load_all(content))
            return True, None
        except Exception as e:
            return False, f"YAML syntax error: {e}"

    if lower.endswith((".service", ".unit", ".socket", ".timer")):
        # Validate systemd unit structure
        lines = content.splitlines()
        has_section = any(line.strip().startswith("[") and line.strip().endswith("]") for line in lines)
        if not has_section and content.strip():
            return False, "Systemd unit missing section header (e.g. [Unit] or [Service])"
        return True, None

    if lower.endswith(".conf"):
        # Basic balanced braces check for nginx/c-style conf
        open_braces = content.count("{")
        close_braces = content.count("}")
        if open_braces != close_braces:
            return False, f"Unbalanced braces in config file: {open_braces} open vs {close_braces} close"
        return True, None

    return True, None


class ConfigDeployer:
    """
    Staged configuration deployment engine.
    """
    def __init__(self, allow_tmp: bool = False):
        self.allow_tmp = allow_tmp

    def deploy(
        self,
        approval_id: str,
        user_id: str,
        target_path: str,
        staged_content: str,
        proposed_hash: Optional[str] = None,
        base_hash: Optional[str] = None,
        expected_target_exists: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Executes the staged configuration deployment pipeline.
        expected_target_exists binds an approved creation/replacement to the
        destination's existence at proposal time. A base_hash implies True;
        the adapter explicitly passes False for an approved creation.
        Returns:
            Dict containing status ("succeeded" / "failed"), message, backup_path, rollback_performed.
        """
        # Phase 1: Destination Validation
        canonical_target = validate_target_config_path(target_path, allow_tmp=self.allow_tmp)
        target_dir = os.path.dirname(canonical_target)
        target_name = os.path.basename(canonical_target)

        # Phase 2: Staged Content SHA-256 Verification (Tamper Check)
        actual_proposed_hash = hashlib.sha256(staged_content.encode("utf-8")).hexdigest()
        if proposed_hash and actual_proposed_hash != proposed_hash:
            return {
                "success": False,
                "status": "failed",
                "message": (
                    f"Tamper detected: Proposed content hash mismatch. "
                    f"Expected {proposed_hash[:12]}..., computed {actual_proposed_hash[:12]}..."
                ),
                "rollback_performed": False,
            }

        # Phase 3: Pre-swap Syntax Verification
        valid, err = validate_syntax(staged_content, target_name)
        if not valid:
            return {
                "success": False,
                "status": "failed",
                "message": f"Pre-deployment syntax validation failed: {err}",
                "rollback_performed": False,
            }

        # Phase 4: Destination Conflict Detection
        target_exists = os.path.exists(canonical_target)
        # A digest implies an existing target. The adapter also supplies False
        # for an approved creation, so an intervening creation is a conflict.
        if expected_target_exists is None and base_hash is not None:
            expected_target_exists = True
        if expected_target_exists is not None and target_exists != expected_target_exists:
            return {
                "success": False,
                "status": "failed",
                "message": "Conflict detected: Target destination existence changed since proposal.",
                "rollback_performed": False,
            }
        actual_base_hash = None
        target_metadata = None
        if target_exists:
            with open(canonical_target, "rb") as f:
                target_metadata = os.fstat(f.fileno())
                dest_content = f.read()
            actual_base_hash = hashlib.sha256(dest_content).hexdigest()

            if base_hash is not None and base_hash != actual_base_hash:
                return {
                    "success": False,
                    "status": "failed",
                    "message": (
                        f"Conflict detected: Target destination modified out-of-band. "
                        f"Expected base hash {base_hash[:12]}..., found {actual_base_hash[:12]}... "
                        f"Deployment aborted without modifications."
                    ),
                    "rollback_performed": False,
                }

        # Phase 5: Backup Creation (in exact same directory)
        backup_path = None
        if target_exists:
            timestamp = int(time.time())
            backup_path = os.path.join(target_dir, f".{target_name}.bak.{timestamp}_{approval_id}")
            try:
                shutil.copy2(canonical_target, backup_path)
                with open(backup_path, "a") as f:
                    os.fsync(f.fileno())
                # Verify backup
                with open(backup_path, "rb") as f:
                    backup_content = f.read()
                backup_hash = hashlib.sha256(backup_content).hexdigest()
                if backup_hash != actual_base_hash:
                    if os.path.exists(backup_path):
                        os.unlink(backup_path)
                    return {
                        "success": False,
                        "status": "failed",
                        "message": "Backup verification failed: hash mismatch on backup copy.",
                        "rollback_performed": False,
                    }
            except Exception as e:
                return {
                    "success": False,
                    "status": "failed",
                    "message": f"Failed to create pre-deployment backup: {e}",
                    "rollback_performed": False,
                }

        # Phase 6: Same-Filesystem Atomic Swap (os.replace)
        tmp_path = os.path.join(target_dir, f".{target_name}.tmp.{uuid.uuid4().hex}")
        try:
            os.makedirs(target_dir, exist_ok=True)
            fd = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with open(fd, "w", encoding="utf-8") as f:
                f.write(staged_content)
                f.flush()
                if target_metadata is not None:
                    replacement_metadata = os.fstat(f.fileno())
                    if (replacement_metadata.st_uid, replacement_metadata.st_gid) != (
                        target_metadata.st_uid, target_metadata.st_gid
                    ):
                        os.fchown(f.fileno(), target_metadata.st_uid, target_metadata.st_gid)
                    os.fchmod(f.fileno(), stat.S_IMODE(target_metadata.st_mode))
                os.fsync(f.fileno())
            os.replace(tmp_path, canonical_target)
        except Exception as e:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            return {
                "success": False,
                "status": "failed",
                "message": f"Atomic swap failed: {e}",
                "rollback_performed": False,
            }

        # Phase 7: Post-Deployment Verification
        try:
            with open(canonical_target, "rb") as f:
                deployed_bytes = f.read()
            deployed_hash = hashlib.sha256(deployed_bytes).hexdigest()
            deployed_content = deployed_bytes.decode("utf-8")

            if deployed_hash != actual_proposed_hash:
                raise ValueError("Post-deployment content hash mismatch")

            valid_post, err_post = validate_syntax(deployed_content, target_name)
            if not valid_post:
                raise ValueError(f"Post-deployment syntax validation failed: {err_post}")

        except Exception as verify_err:
            # Phase 8: Automated Rollback on Verification Failure
            logger.error("Post-deployment verification error: %s. Initiating automated rollback...", verify_err)
            rollback_ok = False
            try:
                if backup_path and os.path.exists(backup_path):
                    os.replace(backup_path, canonical_target)
                    rollback_ok = True
                elif not target_exists and os.path.exists(canonical_target):
                    os.unlink(canonical_target)
                    rollback_ok = True
            except Exception as rb_err:
                logger.critical("Rollback failure: %s", rb_err)

            return {
                "success": False,
                "status": "failed",
                "message": f"Post-deployment verification failed: {verify_err}. Rollback status: {rollback_ok}",
                "rollback_performed": rollback_ok,
                "backup_path": backup_path,
            }

        # Phase 9: Success & Audit Retention
        return {
            "success": True,
            "status": "succeeded",
            "message": f"Successfully deployed configuration to {canonical_target}",
            "backup_path": backup_path,
            "base_hash": actual_base_hash,
            "deployed_hash": deployed_hash,
            "rollback_performed": False,
        }


_GLOBAL_DEPLOYER: Optional[ConfigDeployer] = None


def get_config_deployer(allow_tmp: bool = False) -> ConfigDeployer:
    global _GLOBAL_DEPLOYER
    if _GLOBAL_DEPLOYER is None or allow_tmp:
        _GLOBAL_DEPLOYER = ConfigDeployer(allow_tmp=allow_tmp)
    return _GLOBAL_DEPLOYER
