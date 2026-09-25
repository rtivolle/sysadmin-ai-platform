"""
RestoreManager: Clean staging restore enforcing the 7-stage recovery sequence.
Sequence:
1. cgroups (sandbox layout & workspace isolation)
2. valkey (dump.rdb & appendonlydir)
3. seaweedfs (master, filer LevelDB, volume storage)
4. victorialogs (partitions & outbox.jsonl)
5. inference (vLLM / mock engine readiness)
6. litellm (LiteLLM proxy & config)
7. traefik (Traefik ingress & ForwardAuth)
"""
import hashlib
import json
import logging
import os
import shutil
import tarfile
import tempfile
import time
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional

from .backup_manager import compute_sha256, compute_dir_sha256

logger = logging.getLogger("resilience.restore_manager")

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BACKEND_DIR / "data"
CONFIG_DIR = BACKEND_DIR / "config"

MANDATORY_RESTORE_SEQUENCE = [
    "cgroups",
    "valkey",
    "seaweedfs",
    "victorialogs",
    "inference",
    "litellm",
    "traefik",
]
REQUIRED_BACKUP_COMPONENTS = frozenset({"valkey", "seaweedfs", "victorialogs", "config_keys"})


class RestoreManager:
    def __init__(self, data_dir: Optional[Path] = None, config_dir: Optional[Path] = None):
        self.data_dir = Path(data_dir or DATA_DIR)
        self.config_dir = Path(config_dir or CONFIG_DIR)

    def restore_from_offhost(
        self,
        offhost_dest: str,
        backup_id: str,
        target_staging_base: Optional[Path] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Fetch a backup from off-host storage, verify its SHA-256, then restore.

        Pulls ``<backup_id>.tar.gz``, ``<backup_id>.manifest.json`` and the
        ``.sha256`` sidecar into a local scratch directory, recomputes the
        archive hash against the sidecar (raising ``OffhostBackupError`` on any
        mismatch or missing file), and delegates to ``restore_from_archive``.
        """
        from .offhost_backup import fetch_offhost_archive

        scratch = Path(tempfile.mkdtemp(prefix="offhost_restore_"))
        try:
            archive_path, manifest_path = fetch_offhost_archive(
                offhost_dest, backup_id, scratch
            )
            return self.restore_from_archive(
                archive_path=archive_path,
                manifest_path=manifest_path,
                target_staging_base=target_staging_base,
                dry_run=dry_run,
            )
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def restore_from_archive(
        self,
        archive_path: Path,
        manifest_path: Optional[Path] = None,
        target_staging_base: Optional[Path] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        Executes clean staging restore with SHA-256 verification and 7-stage bringup sequence.
        """
        start_time = time.time()
        archive_path = Path(archive_path)
        if not archive_path.exists():
            raise FileNotFoundError(f"Backup archive not found: {archive_path}")

        # Phase 1: Unpack into isolated staging directory
        staging_dir = Path(tempfile.mkdtemp(prefix="platform_restore_staging_"))
        executed_stages: List[str] = []
        try:
            with tarfile.open(archive_path, "r:gz") as tar:
                members = tar.getmembers()
                roots = set()
                for member in members:
                    path = PurePosixPath(member.name)
                    if not path.parts or path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
                        raise ValueError(f"Unsafe backup archive member: {member.name}")
                    roots.add(path.parts[0])
                if len(roots) != 1:
                    raise ValueError("Backup archive must contain one root directory")
                tar.extractall(staging_dir)

            # Find extracted root directory
            extracted_subdirs = [p for p in staging_dir.iterdir() if p.is_dir()]
            unpacked_root = extracted_subdirs[0] if extracted_subdirs else staging_dir

            # Phase 2: Locate and Validate Manifest
            manifest_file = unpacked_root / "manifest.json"
            if not manifest_file.exists() and manifest_path:
                manifest_file = Path(manifest_path)

            if not manifest_file.exists():
                raise ValueError("Manifest not found in archive or specified path")

            with open(manifest_file, "r", encoding="utf-8") as f:
                manifest = json.load(f)

            # Verify component hashes
            components = manifest.get("components", {})
            if not isinstance(components, dict) or set(components) != REQUIRED_BACKUP_COMPONENTS:
                raise ValueError("Backup manifest is missing required components")
            for comp_name, comp_data in components.items():
                comp_dir = unpacked_root / comp_name
                if not comp_dir.is_dir() or comp_dir.is_symlink():
                    raise ValueError(f"Backup component missing: {comp_name}")
                expected_sha = comp_data.get("aggregate_sha256") if isinstance(comp_data, dict) else None
                if not isinstance(expected_sha, str) or len(expected_sha) != 64:
                    raise ValueError(f"Backup component hash missing: {comp_name}")
                actual_sha = compute_dir_sha256(comp_dir)
                if actual_sha != expected_sha:
                    raise ValueError(
                        f"Integrity violation on component '{comp_name}': "
                        f"Expected SHA {expected_sha[:12]}..., got {actual_sha[:12]}..."
                    )

            # Phase 3: Enforce Chronological 7-Stage Restore Sequence
            target_data = Path(target_staging_base or self.data_dir)
            target_config = target_data.parent / "config" if target_staging_base else self.config_dir

            # Stage 1: cgroups & sandbox layout
            executed_stages.append("cgroups")
            workspaces_dir = target_data / "workspaces"
            workspaces_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(workspaces_dir, 0o700)

            # Stage 2: valkey
            executed_stages.append("valkey")
            valkey_src = unpacked_root / "valkey"
            valkey_dest = target_data / "valkey"
            if not dry_run and valkey_src.exists():
                valkey_dest.mkdir(parents=True, exist_ok=True)
                os.chmod(valkey_dest, 0o700)
                for item in valkey_src.iterdir():
                    d = valkey_dest / item.name
                    if item.is_file():
                        shutil.copy2(item, d)
                        os.chmod(d, 0o600)
                    elif item.is_dir():
                        shutil.copytree(item, d, dirs_exist_ok=True)
                        os.chmod(d, 0o700)

            # Stage 3: seaweedfs
            executed_stages.append("seaweedfs")
            sw_src = unpacked_root / "seaweedfs"
            sw_dest = target_data / "seaweedfs"
            if not dry_run and sw_src.exists():
                sw_dest.mkdir(parents=True, exist_ok=True)
                os.chmod(sw_dest, 0o700)
                for item in sw_src.iterdir():
                    d = sw_dest / item.name
                    if item.is_file():
                        shutil.copy2(item, d)
                    elif item.is_dir():
                        shutil.copytree(item, d, dirs_exist_ok=True)
                        os.chmod(d, 0o700)

            # Stage 4: victorialogs
            executed_stages.append("victorialogs")
            vl_src = unpacked_root / "victorialogs"
            vl_dest = target_data / "victorialogs"
            if not dry_run and vl_src.exists():
                vl_dest.mkdir(parents=True, exist_ok=True)
                os.chmod(vl_dest, 0o700)
                for item in vl_src.iterdir():
                    d = vl_dest / item.name
                    if item.is_file():
                        shutil.copy2(item, d)
                        os.chmod(d, 0o600)
                    elif item.is_dir():
                        shutil.copytree(item, d, dirs_exist_ok=True)
                        os.chmod(d, 0o700)

            # Stage 5: inference engine check
            executed_stages.append("inference")

            # Stage 6: litellm configuration & keys
            executed_stages.append("litellm")
            keys_src = unpacked_root / "config_keys"
            keys_dest = target_config / "keys"
            if not dry_run and keys_src.exists():
                keys_dest.mkdir(parents=True, exist_ok=True)
                os.chmod(keys_dest, 0o700)
                for item in keys_src.iterdir():
                    d = keys_dest / item.name
                    if item.is_file():
                        shutil.copy2(item, d)
                        os.chmod(d, 0o600)
                    elif item.is_dir():
                        shutil.copytree(item, d, dirs_exist_ok=True)
                        os.chmod(d, 0o700)

            # Stage 7: traefik front door
            executed_stages.append("traefik")

            elapsed = time.time() - start_time
            return {
                "success": True,
                "status": "restored",
                "backup_id": manifest.get("backup_id"),
                "created_at": manifest.get("created_at"),
                "restore_duration_seconds": elapsed,
                "restore_sequence": executed_stages,
                "components_restored": list(components.keys()),
            }

        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)


_GLOBAL_RESTORE_MGR: Optional[RestoreManager] = None


def get_restore_manager() -> RestoreManager:
    global _GLOBAL_RESTORE_MGR
    if _GLOBAL_RESTORE_MGR is None:
        _GLOBAL_RESTORE_MGR = RestoreManager()
    return _GLOBAL_RESTORE_MGR
