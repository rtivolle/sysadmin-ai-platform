"""Off-host backup copy, verification and retention (PR-D2).

After ``BackupManager.create_backup`` produces a local archive, the archive and
its manifest can be pushed to independently controlled storage so a compromise
of the platform host cannot destroy both the primary data and its backup.

Supported destinations:

* ``OFFHOST_BACKUP_DEST=/mnt/worm-backups`` — a local mount path (required for
  production; it must be a separately mounted, ideally WORM/append-only
  filesystem). Files are copied with ``shutil.copyfileobj`` and re-read from the
  destination to verify the SHA-256 matches the sidecar.
* ``OFFHOST_BACKUP_DEST=ssh://user@host:/absolute/path`` — optional; copied via
  ``rsync -a`` and verified by pulling the remote archive back over the same
  transport and re-hashing it.

Every archive carries a ``<name>.sha256`` sidecar (the ``sha256sum`` format)
written *at the destination*; nothing is reported successful until the copy is
re-read and its hash recomputed and compared. Any failure raises
``OffhostBackupError`` — there is no silent, best-effort path.

Retention pruning removes archives older than ``OFFHOST_RETENTION_DAYS``
(default 90). The data owner must approve this retention policy; until then it
is a configurable placeholder, not a legal retention decision.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .backup_manager import compute_sha256

logger = logging.getLogger("resilience.offhost_backup")

OFFHOST_BACKUP_DEST = os.getenv("OFFHOST_BACKUP_DEST", "")
DEFAULT_RETENTION_DAYS = 90
SIDECAR_SUFFIX = ".sha256"


class OffhostBackupError(RuntimeError):
    """A copy, verification or prune step on off-host storage failed."""


@dataclass(frozen=True)
class OffhostTarget:
    """Parsed OFFHOST_BACKUP_DEST."""

    scheme: str  # "local" or "ssh"
    path: str
    host: Optional[str] = None  # "user@host" for ssh, None for local


def parse_offhost_dest(dest: str) -> OffhostTarget:
    """Parse ``OFFHOST_BACKUP_DEST`` into a local path or an rsync ssh target."""
    if not dest or not dest.strip():
        raise OffhostBackupError("off-host destination is empty")
    dest = dest.strip()
    if dest.startswith("ssh://"):
        # ssh://[user@]host:/absolute/path
        remainder = dest[len("ssh://"):]
        if "/" not in remainder:
            raise OffhostBackupError(f"ssh destination missing a path: {dest}")
        host, _, path = remainder.partition("/")
        path = "/" + path
        if host.endswith(":"):
            host = host[:-1]
        if not host or not path.startswith("/"):
            raise OffhostBackupError(f"ssh destination must be ssh://[user@]host:/absolute/path: {dest}")
        return OffhostTarget(scheme="ssh", path=path, host=host)
    if "://" in dest:
        raise OffhostBackupError(f"unsupported off-host scheme (only local paths and ssh:// are supported): {dest}")
    if not dest.startswith("/"):
        raise OffhostBackupError(
            f"off-host destination must be an absolute local mount path: {dest}"
        )
    return OffhostTarget(scheme="local", path=dest)


def _sidecar_name(archive_filename: str) -> str:
    return archive_filename + SIDECAR_SUFFIX


def _sidecar_content(archive_sha256: str, archive_filename: str) -> bytes:
    return f"{archive_sha256}  {archive_filename}\n".encode("utf-8")


def _verify_local_archive(archive_path: Path, sidecar_path: Path) -> None:
    """Re-read a local archive and compare its SHA-256 to the sidecar."""
    if not sidecar_path.exists():
        raise OffhostBackupError(f"sidecar missing at {sidecar_path}")
    try:
        expected = sidecar_path.read_text(encoding="utf-8").split()[0]
    except (OSError, IndexError) as exc:
        raise OffhostBackupError(f"unreadable sidecar {sidecar_path}: {exc}") from exc
    if len(expected) != 64:
        raise OffhostBackupError(f"sidecar {sidecar_path} does not contain a 64-char SHA-256")
    if not archive_path.exists():
        raise OffhostBackupError(f"off-host archive missing at {archive_path}")
    actual = compute_sha256(archive_path)
    if actual != expected:
        raise OffhostBackupError(
            f"off-host archive checksum mismatch for {archive_path}: "
            f"expected {expected[:12]}..., got {actual[:12]}..."
        )


def _run_rsync(args: List[str]) -> None:
    """Run rsync, raising OffhostBackupError on any non-zero exit."""
    try:
        result = subprocess.run(
            ["rsync", "-a"] + args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise OffhostBackupError("rsync is required for ssh:// off-host destinations") from exc
    if result.returncode != 0:
        raise OffhostBackupError(
            f"rsync failed (exit {result.returncode}): {result.stderr.strip()[:400]}"
        )


def copy_archive_to_offhost(
    archive_path: Path,
    manifest_path: Path,
    archive_sha256: str,
    dest: str,
) -> Dict[str, Any]:
    """Copy an archive + manifest + SHA-256 sidecar to off-host storage and verify.

    Returns a metadata dict on success and raises ``OffhostBackupError`` on any
    copy or checksum failure.
    """
    archive_path = Path(archive_path)
    manifest_path = Path(manifest_path)
    archive_filename = archive_path.name
    sidecar_filename = _sidecar_name(archive_filename)
    target = parse_offhost_dest(dest)
    sidecar_bytes = _sidecar_content(archive_sha256, archive_filename)

    if target.scheme == "local":
        dst_dir = Path(target.path)
        dst_archive = dst_dir / archive_filename
        dst_manifest = dst_dir / manifest_path.name
        dst_sidecar = dst_dir / sidecar_filename
        try:
            dst_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(archive_path, dst_archive)
            shutil.copyfile(manifest_path, dst_manifest)
            dst_sidecar.write_bytes(sidecar_bytes)
        except OSError as exc:
            raise OffhostBackupError(f"off-host copy to {dst_dir} failed: {exc}") from exc
        _verify_local_archive(dst_archive, dst_sidecar)
        return {
            "dest": target.path,
            "scheme": "local",
            "archive": str(dst_archive),
            "manifest": str(dst_manifest),
            "sidecar": str(dst_sidecar),
            "archive_sha256": archive_sha256,
            "verified": True,
        }

    # ssh:// via rsync. Write the sidecar locally into a temp staging, rsync all
    # three up, then rsync the archive back to verify it round-trips intact.
    import tempfile

    remote_path = target.path.rstrip("/")
    remote_archive = f"{remote_path}/{archive_filename}"
    remote_manifest = f"{remote_path}/{manifest_path.name}"
    remote_sidecar = f"{remote_path}/{sidecar_filename}"
    staging = Path(tempfile.mkdtemp(prefix="offhost_sidecar_"))
    local_sidecar = staging / sidecar_filename
    local_sidecar.write_bytes(sidecar_bytes)
    try:
        remote = f"{target.host}:{remote_path}/"
        _run_rsync([str(archive_path), str(manifest_path), str(local_sidecar), remote])
        # Round-trip the archive back to verify integrity over the transport.
        verify_dir = Path(tempfile.mkdtemp(prefix="offhost_verify_"))
        try:
            _run_rsync([f"{target.host}:{remote_archive}", str(verify_dir)])
            fetched = verify_dir / archive_filename
            _verify_local_archive(fetched, local_sidecar)
        finally:
            shutil.rmtree(verify_dir, ignore_errors=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {
        "dest": target.path,
        "scheme": "ssh",
        "host": target.host,
        "archive": remote_archive,
        "manifest": remote_manifest,
        "sidecar": remote_sidecar,
        "archive_sha256": archive_sha256,
        "verified": True,
    }


def prune_offhost(
    dest: str,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Remove off-host archives older than ``retention_days`` (local dest only).

    Matches ``*.tar.gz`` by mtime; each archive's ``.manifest.json`` and
    ``.sha256`` siblings are removed alongside it. Retention is a placeholder
    default pending the data owner's decision.
    """
    if retention_days < 0:
        raise OffhostBackupError("retention_days must be non-negative")
    target = parse_offhost_dest(dest)
    if target.scheme != "local":
        # Pruning over ssh is deliberately left to the operator rather than
        # guessed at; a silent "no-op" would be misleading.
        raise OffhostBackupError("off-host retention pruning is only supported for local mount paths")
    now_epoch = time.time() if now is None else float(now)
    cutoff = now_epoch - retention_days * 86400
    dst_dir = Path(target.path)
    if not dst_dir.exists():
        return {"dest": target.path, "pruned": [], "cutoff_epoch": cutoff}
    removed: List[str] = []
    for archive in sorted(dst_dir.glob("*.tar.gz")):
        if not archive.is_file():
            continue
        try:
            if archive.stat().st_mtime >= cutoff:
                continue
        except OSError as exc:
            raise OffhostBackupError(f"cannot stat {archive}: {exc}") from exc
        for sibling in (
            archive,
            dst_dir / archive.name.replace(".tar.gz", ".manifest.json"),
            dst_dir / _sidecar_name(archive.name),
        ):
            try:
                if sibling.exists() and sibling.is_file():
                    sibling.unlink()
                    removed.append(sibling.name)
            except OSError as exc:
                raise OffhostBackupError(f"cannot prune {sibling}: {exc}") from exc
    return {"dest": target.path, "pruned": sorted(set(removed)), "cutoff_epoch": cutoff}


def fetch_offhost_archive(
    dest: str,
    backup_id: str,
    local_dir: Path,
) -> Tuple[Path, Path]:
    """Pull an archive + manifest from off-host storage, verify SHA-256, return paths.

    Returns ``(local_archive_path, local_manifest_path)``. Raises
    ``OffhostBackupError`` if the archive is missing or its checksum fails.
    """
    local_dir = Path(local_dir)
    local_dir.mkdir(parents=True, exist_ok=True)
    archive_filename = f"{backup_id}.tar.gz"
    manifest_filename = f"{backup_id}.manifest.json"
    sidecar_filename = _sidecar_name(archive_filename)
    target = parse_offhost_dest(dest)

    if target.scheme == "local":
        src_dir = Path(target.path)
        src_archive = src_dir / archive_filename
        src_manifest = src_dir / manifest_filename
        src_sidecar = src_dir / sidecar_filename
        if not src_archive.exists():
            raise OffhostBackupError(f"off-host archive not found: {src_archive}")
        dst_archive = local_dir / archive_filename
        dst_manifest = local_dir / manifest_filename
        dst_sidecar = local_dir / sidecar_filename
        try:
            shutil.copyfile(src_archive, dst_archive)
            shutil.copyfile(src_manifest, dst_manifest)
            shutil.copyfile(src_sidecar, dst_sidecar)
        except OSError as exc:
            raise OffhostBackupError(f"fetching off-host archive {backup_id} failed: {exc}") from exc
        _verify_local_archive(dst_archive, dst_sidecar)
        return dst_archive, dst_manifest

    remote = f"{target.host}:{target.path.rstrip('/')}/{archive_filename}"
    dst_archive = local_dir / archive_filename
    dst_manifest = local_dir / manifest_filename
    _run_rsync(
        [
            remote,
            f"{target.host}:{target.path.rstrip('/')}/{manifest_filename}",
            f"{target.host}:{target.path.rstrip('/')}/{sidecar_filename}",
            str(local_dir),
        ]
    )
    _verify_local_archive(dst_archive, local_dir / sidecar_filename)
    return dst_archive, dst_manifest
