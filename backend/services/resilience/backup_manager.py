"""
BackupManager: Live snapshotting and packaging of platform state stores.
Snapshots:
1. Valkey (BGSAVE or dump.rdb / appendonlydir)
2. VictoriaLogs (/snapshot/create or partitions / outbox.jsonl)
3. SeaweedFS (m9333, filerldb2, vol_dir.uuid, volume data)
4. Config Keys (backend/config/keys/ with permissions preserved)
Packages with cryptographic SHA-256 manifest into tar.gz archive.
"""
import hashlib
import json
import logging
import os
import shutil
import tarfile
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import redis

logger = logging.getLogger("resilience.backup_manager")

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BACKEND_DIR / "data"
CONFIG_DIR = BACKEND_DIR / "config"
DEFAULT_BACKUP_DIR = DATA_DIR / "backups"


def compute_sha256(file_path: Path) -> str:
    """Computes SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def compute_dir_sha256(dir_path: Path) -> str:
    """Computes aggregate SHA-256 of all files within a directory sorted deterministically by path."""
    h = hashlib.sha256()
    if not dir_path.exists():
        return h.hexdigest()
    all_files = []
    for root, _, files in os.walk(dir_path):
        for fname in files:
            fpath = Path(root) / fname
            all_files.append((str(fpath.relative_to(dir_path)), fpath))
    all_files.sort(key=lambda x: x[0])
    for rel_path_str, fpath in all_files:
        h.update(rel_path_str.encode("utf-8"))
        if fpath.is_file():
            h.update(compute_sha256(fpath).encode("utf-8"))
    return h.hexdigest()


class BackupManager:
    def __init__(
        self,
        backup_dir: Optional[Path] = None,
        valkey_url: Optional[str] = None,
        victorialogs_url: Optional[str] = None,
    ):
        self.backup_dir = Path(backup_dir or DEFAULT_BACKUP_DIR)
        self.valkey_url = valkey_url or os.getenv("VALKEY_URL", "redis://127.0.0.1:6379/0")
        self.victorialogs_url = victorialogs_url or os.getenv("VICTORIALOGS_URL", "http://127.0.0.1:9428")
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def snapshot_valkey(self, staging_dir: Path) -> Dict[str, Any]:
        """Snapshots Valkey state via BGSAVE if live, or copies dump.rdb / appendonlydir."""
        valkey_dest = staging_dir / "valkey"
        valkey_dest.mkdir(parents=True, exist_ok=True)
        valkey_src = DATA_DIR / "valkey"

        live_saved = False
        try:
            r = redis.Redis.from_url(self.valkey_url, decode_responses=True, socket_timeout=2.0)
            r.ping()
            # Trigger BGSAVE
            last_save = r.lastsave()
            try:
                r.bgsave()
            except redis.ResponseError as re:
                if "already in progress" not in str(re).lower():
                    raise
            # Wait briefly for BGSAVE to finish (up to 5s)
            for _ in range(25):
                time.sleep(0.2)
                cur_save = r.lastsave()
                if cur_save > last_save or r.info("persistence").get("rdb_bgsave_in_progress") == 0:
                    live_saved = True
                    break
        except Exception as e:
            logger.debug("Valkey live snapshot bypassed (%s), copying local data", e)

        files_info: List[Dict[str, Any]] = []
        if valkey_src.exists():
            for item in valkey_src.iterdir():
                dest_item = valkey_dest / item.name
                if item.is_file():
                    shutil.copy2(item, dest_item)
                    files_info.append({"name": item.name, "sha256": compute_sha256(dest_item)})
                elif item.is_dir():
                    shutil.copytree(item, dest_item, dirs_exist_ok=True)
                    files_info.append({"name": item.name, "is_dir": True, "sha256": compute_dir_sha256(dest_item)})

        return {
            "mode": "live_bgsave" if live_saved else "filesystem_copy",
            "files": files_info,
            "aggregate_sha256": compute_dir_sha256(valkey_dest),
        }

    def snapshot_victorialogs(self, staging_dir: Path) -> Dict[str, Any]:
        """Snapshots VictoriaLogs via /snapshot/create API or partitions/outbox.jsonl."""
        vl_dest = staging_dir / "victorialogs"
        vl_dest.mkdir(parents=True, exist_ok=True)
        vl_src = DATA_DIR / "victorialogs"

        snapshot_created = False
        snapshot_id = None
        try:
            with httpx.Client(timeout=3.0) as client:
                res = client.get(f"{self.victorialogs_url.rstrip('/')}/snapshot/create")
                if res.status_code == 200:
                    data = res.json()
                    snapshot_id = data.get("snapshot")
                    snapshot_created = True
        except Exception as e:
            logger.debug("VictoriaLogs /snapshot/create skipped (%s), copying data directly", e)

        files_info: List[Dict[str, Any]] = []
        if snapshot_created and snapshot_id:
            # Copy from snapshot directory
            snap_dir = vl_src / "snapshots" / snapshot_id
            if snap_dir.exists():
                shutil.copytree(snap_dir, vl_dest / "partitions", dirs_exist_ok=True)
            # Delete snapshot
            try:
                with httpx.Client(timeout=3.0) as client:
                    client.get(f"{self.victorialogs_url.rstrip('/')}/snapshot/delete?snapshot={snapshot_id}")
            except Exception:
                pass
        else:
            # Direct copy of partitions and outbox
            if (vl_src / "partitions").exists():
                shutil.copytree(vl_src / "partitions", vl_dest / "partitions", dirs_exist_ok=True)

        # Copy outbox.jsonl if present
        if (vl_src / "outbox.jsonl").exists():
            shutil.copy2(vl_src / "outbox.jsonl", vl_dest / "outbox.jsonl")

        for item in vl_dest.iterdir():
            if item.is_file():
                files_info.append({"name": item.name, "sha256": compute_sha256(item)})
            elif item.is_dir():
                files_info.append({"name": item.name, "is_dir": True, "sha256": compute_dir_sha256(item)})

        return {
            "mode": "api_snapshot" if snapshot_created else "filesystem_copy",
            "files": files_info,
            "aggregate_sha256": compute_dir_sha256(vl_dest),
        }

    def snapshot_seaweedfs(self, staging_dir: Path) -> Dict[str, Any]:
        """Snapshots SeaweedFS metadata (m9333, filerldb2) and volume data."""
        sw_dest = staging_dir / "seaweedfs"
        sw_dest.mkdir(parents=True, exist_ok=True)
        sw_src = DATA_DIR / "seaweedfs"

        files_info: List[Dict[str, Any]] = []
        if sw_src.exists():
            for item in sw_src.iterdir():
                dest_item = sw_dest / item.name
                if item.is_file():
                    shutil.copy2(item, dest_item)
                    files_info.append({"name": item.name, "sha256": compute_sha256(dest_item)})
                elif item.is_dir():
                    shutil.copytree(item, dest_item, dirs_exist_ok=True)
                    files_info.append({"name": item.name, "is_dir": True, "sha256": compute_dir_sha256(dest_item)})

        return {
            "mode": "filesystem_copy",
            "files": files_info,
            "aggregate_sha256": compute_dir_sha256(sw_dest),
        }

    def snapshot_config_keys(self, staging_dir: Path) -> Dict[str, Any]:
        """Snapshots platform secrets and encryption keys preserving 0600 permissions."""
        keys_dest = staging_dir / "config_keys"
        keys_dest.mkdir(parents=True, exist_ok=True)
        os.chmod(keys_dest, 0o700)
        keys_src = CONFIG_DIR / "keys"

        files_info: List[Dict[str, Any]] = []
        if keys_src.exists():
            for item in keys_src.iterdir():
                dest_item = keys_dest / item.name
                if item.is_file():
                    shutil.copy2(item, dest_item)
                    os.chmod(dest_item, 0o600)
                    files_info.append({"name": item.name, "sha256": compute_sha256(dest_item)})
                elif item.is_dir():
                    shutil.copytree(item, dest_item, dirs_exist_ok=True)
                    os.chmod(dest_item, 0o700)
                    files_info.append({"name": item.name, "is_dir": True, "sha256": compute_dir_sha256(dest_item)})

        return {
            "mode": "filesystem_copy",
            "files": files_info,
            "aggregate_sha256": compute_dir_sha256(keys_dest),
        }

    def snapshot_postgres_pitr(self) -> Dict[str, Any]:
        """Records Postgres PITR metadata in the platform backup manifest.

        The base backups and WAL segments themselves live in the PITR
        destination (``SYSADMIN_PG_PITR_DEST``, often off-host) and are NOT
        copied into the platform archive; this records which base backup and
        how many WAL segments exist so a point-in-time restore can be planned
        from the manifest alone. Returns ``{"configured": False}`` when no
        PITR destination has been initialised yet, in which case the caller
        omits the component entirely.
        """
        from .pg_pitr_backup import DEFAULT_PITR_DEST, latest_base_label

        dest = DEFAULT_PITR_DEST
        label = latest_base_label(dest)
        if label is None:
            return {"configured": False}
        wal_dir = dest / "wal"
        wal_count = len(list(wal_dir.iterdir())) if wal_dir.is_dir() else 0
        manifest_path = dest / "base" / label / "pitr-manifest.json"
        manifest_sha = compute_sha256(manifest_path) if manifest_path.is_file() else None
        return {
            "configured": True,
            "mode": "metadata_reference",
            "dest": str(dest),
            "latest_base_label": label,
            "pitr_manifest_sha256": manifest_sha,
            "wal_segments_archived": wal_count,
            "note": "base backups + WAL segments stay in the PITR destination; "
                    "restore with: python -m backend.services.resilience.pg_pitr_backup "
                    "restore --target-time '<ISO>'",
        }

    def create_backup(
        self,
        backup_id: Optional[str] = None,
        offhost_dest: Optional[str] = None,
        offhost_retention_days: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Creates a full platform backup archive and manifest.json.

        When ``offhost_dest`` is set (a local mount path or ``ssh://`` target),
        the archive + manifest + SHA-256 sidecar are copied to that location and
        verified after the copy; a failure raises ``OffhostBackupError`` rather
        than returning a "successful" backup. ``offhost_retention_days`` triggers
        pruning of older off-host archives (default 90).
        Returns:
            Dict containing manifest metadata and archive_path.
        """
        now = time.time()
        now_dt = time.strftime("%Y%m%d_%H%M%S", time.gmtime(now))
        bid = backup_id or f"backup_{now_dt}_{uuid.uuid4().hex[:8]}"

        staging_dir = Path(tempfile.mkdtemp(prefix="platform_backup_staging_"))
        try:
            # 1. Take snapshots
            valkey_meta = self.snapshot_valkey(staging_dir)
            vl_meta = self.snapshot_victorialogs(staging_dir)
            sw_meta = self.snapshot_seaweedfs(staging_dir)
            keys_meta = self.snapshot_config_keys(staging_dir)
            pitr_meta = self.snapshot_postgres_pitr()

            # 2. Build Manifest
            components = {
                "valkey": valkey_meta,
                "victorialogs": vl_meta,
                "seaweedfs": sw_meta,
                "config_keys": keys_meta,
            }
            # Only present once a PITR destination has been initialised, so
            # existing manifests (and their tests) are unchanged otherwise.
            if pitr_meta.get("configured"):
                components["postgres_pitr"] = pitr_meta
            manifest = {
                "backup_id": bid,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                "created_at_epoch": now,
                "platform_version": "2.0.0",
                "components": components,
            }

            manifest_file = staging_dir / "manifest.json"
            with open(manifest_file, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2)

            # 3. Create compressed tarball
            archive_filename = f"{bid}.tar.gz"
            archive_path = self.backup_dir / archive_filename
            with tarfile.open(archive_path, "w:gz") as tar:
                tar.add(staging_dir, arcname=bid)

            # 4. Record archive SHA-256 in final manifest
            archive_sha256 = compute_sha256(archive_path)
            manifest["archive_sha256"] = archive_sha256
            manifest["archive_file"] = archive_filename
            manifest["total_bytes"] = os.path.getsize(archive_path)

            final_manifest_path = self.backup_dir / f"{bid}.manifest.json"
            with open(final_manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2)

            manifest["archive_path"] = str(archive_path)
            manifest["manifest_path"] = str(final_manifest_path)

            # 5. Optional off-host copy with post-copy verification.
            if offhost_dest:
                from .offhost_backup import copy_archive_to_offhost, prune_offhost

                offhost_result = copy_archive_to_offhost(
                    archive_path,
                    final_manifest_path,
                    archive_sha256,
                    offhost_dest,
                )
                manifest["offhost"] = offhost_result
                with open(final_manifest_path, "w", encoding="utf-8") as f:
                    json.dump(manifest, f, indent=2)
                if offhost_retention_days is not None:
                    offhost_result["prune"] = prune_offhost(offhost_dest, offhost_retention_days)

            return manifest

        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)


_GLOBAL_BACKUP_MGR: Optional[BackupManager] = None


def get_backup_manager() -> BackupManager:
    global _GLOBAL_BACKUP_MGR
    if _GLOBAL_BACKUP_MGR is None:
        _GLOBAL_BACKUP_MGR = BackupManager()
    return _GLOBAL_BACKUP_MGR
