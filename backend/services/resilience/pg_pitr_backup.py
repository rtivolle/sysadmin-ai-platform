"""
pg_pitr_backup: PostgreSQL point-in-time recovery for the platform node.

Enterprise tier-1 hardening (spec ARCHITECTURE.md §8.2):
- **Targets**: RPO <= 15 min (continuous WAL archiving), RTO <= 2 h
  (documented, restore drill-tested).
- Weekly full base backup via ``pg_basebackup`` to a configured destination
  (local disk, NAS mount, or off-host path) + continuous WAL archiving via an
  ``archive_command`` snippet generated for ``backend/config/postgres/``.

Conventions (mirrors backend/services/resilience/backup_manager.py and
backend/config/postgres/postgres.sh):
- dry-run by default wherever destructive; restore demands explicit
  confirmation unless ``--yes`` is passed.
- the postgres password is read from the provisioned key file and passed to
  libpq tools through the ``PGPASSWORD`` environment variable only — it is
  never printed, logged, or placed on a command line.
- fail closed with a clear message when ``pg_basebackup``/``psql`` are absent
  or the cluster is unreachable.

Layout of the PITR destination directory::

    <dest>/
        base/                          # pg_basebackup outputs, one dir per label
            base_20260930_120000/
                base.tar.gz
                pg_wal.tar.gz          # (-X stream bundles WAL)
                pitr-manifest.json
        wal/                           # archived WAL segments (0000000100000000...)
        wal-archive.conf               # (generated) archive_command snippet

Usage:
    python -m backend.services.resilience.pg_pitr_backup backup-base [--dest DIR] [--label L]
    python -m backend.services.resilience.pg_pitr_backup wal-snippet [--dest DIR] [--write]
    python -m backend.services.resilience.pg_pitr_backup restore --target-time "2026-09-30 10:00" [--backup-label L] [--dest DIR] [--yes]
    python -m backend.services.resilience.pg_pitr_backup verify [--dest DIR] [--backup-label L]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("resilience.pg_pitr")

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = BACKEND_DIR / "config"
POSTGRES_CONFIG_DIR = CONFIG_DIR / "postgres"

# --- connection / path defaults mirror backend/config/postgres/postgres.sh ---
PG_DATA_DIR = Path(os.getenv("SYSADMIN_POSTGRES_DATA_DIR", BACKEND_DIR / "data" / "postgres"))
PG_RUN_DIR = Path(os.getenv("SYSADMIN_POSTGRES_RUN_DIR", BACKEND_DIR / "run" / "postgres"))
PG_PORT = os.getenv("SYSADMIN_POSTGRES_PORT", "5433")
PG_USER = os.getenv("SYSADMIN_POSTGRES_USER", "sysadmin_control")
PG_DB = os.getenv("SYSADMIN_POSTGRES_DB", "sysadmin_control")
PG_PASSWORD_FILE = Path(
    os.getenv("SYSADMIN_POSTGRES_PASSWORD_FILE", CONFIG_DIR / "keys" / "postgres-password.key")
)

# PITR destination: where base backups and archived WAL segments live.
DEFAULT_PITR_DEST = Path(
    os.getenv("SYSADMIN_PG_PITR_DEST", BACKEND_DIR / "data" / "pg_pitr")
)

# --- enterprise tier-1 targets (spec §8.2) ---
TARGET_RPO_SECONDS = 15 * 60      # RPO <= 15 min via continuous WAL archiving
TARGET_RTO_SECONDS = 2 * 60 * 60  # RTO <= 2 h via documented, drill-tested restore


class PitrError(RuntimeError):
    """Fail-closed error: the PITR operation could not be performed safely."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _which_or_raise(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise PitrError(
            f"'{tool}' is not installed or not on PATH; cannot perform this "
            f"PITR operation. Install the PostgreSQL client/server tools first."
        )
    return path


def _read_password() -> str:
    try:
        pw = PG_PASSWORD_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        pw = ""
    if not pw:
        raise PitrError(
            f"postgres password file {PG_PASSWORD_FILE} is missing or empty; "
            "run ./install.sh (provisioning) before using PITR backups."
        )
    return pw


def _pg_env() -> Dict[str, str]:
    env = dict(os.environ)
    env["PGPASSWORD"] = _read_password()  # never on the command line, never logged
    return env


def _run(cmd: List[str], env: Optional[Dict[str, str]] = None, timeout: float = 600.0) -> subprocess.CompletedProcess:
    # Redact PGPASSWORD from any accidental logging: it is env-only.
    logger.debug("running: %s", " ".join(cmd))
    try:
        return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)
    except OSError as e:
        raise PitrError(f"failed to execute {' '.join(cmd)}: {e}")


# ---------------------------------------------------------------------------
# Target-time parsing
# ---------------------------------------------------------------------------

_RELATIVE_RE = re.compile(r"^(\d+)\s*([smhd])\s*ago$", re.IGNORECASE)


def parse_target_time(value: str, now: Optional[datetime] = None) -> datetime:
    """Parse a restore target time. Accepts:

    - ``now``
    - ISO 8601 (``2026-09-30T10:00:00``, with or without offset)
    - ``YYYY-MM-DD HH:MM:SS`` (assumed UTC)
    - relative ``<N>s|m|h|d ago`` (e.g. ``30m ago``, ``2h ago``)

    Returns a timezone-aware UTC datetime. Raises PitrError on invalid input.
    """
    now = now or datetime.now(timezone.utc)
    v = value.strip()
    if v.lower() == "now":
        return now
    m = _RELATIVE_RE.match(v)
    if m:
        amount = int(m.group(1))
        unit = m.group(2).lower()
        delta = {"s": timedelta(seconds=amount), "m": timedelta(minutes=amount),
                 "h": timedelta(hours=amount), "d": timedelta(days=amount)}[unit]
        return now - delta
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(v, fmt).replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass
    raise PitrError(
        f"invalid target time {value!r}: expected 'now', ISO 8601, "
        "'YYYY-MM-DD HH:MM:SS', or '<N>s|m|h|d ago'."
    )


# ---------------------------------------------------------------------------
# backup-base
# ---------------------------------------------------------------------------


def build_base_backup_cmd(dest_dir: Path, label: str) -> List[str]:
    """Build the pg_basebackup command (pure: no side effects)."""
    return [
        "pg_basebackup",
        "-h", str(PG_RUN_DIR),          # unix socket dir, like postgres.sh
        "-p", PG_PORT,
        "-U", PG_USER,
        "-D", str(dest_dir),
        "-Ft",                          # tar output
        "-z",                           # gzip
        "-X", "stream",                 # stream WAL needed for a consistent backup
        "-c", "fast",                   # fast checkpoint
        "-l", label,                    # backup label
        "-v",
    ]


def _latest_base_label(dest: Path) -> Optional[str]:
    base_dir = dest / "base"
    if not base_dir.is_dir():
        return None
    labels = sorted(p.name for p in base_dir.iterdir() if p.is_dir())
    return labels[-1] if labels else None


def latest_base_label(dest: Optional[Path] = None) -> Optional[str]:
    """Newest base-backup label in a PITR destination, or None if none exists."""
    return _latest_base_label(Path(dest) if dest else DEFAULT_PITR_DEST)


def backup_base(dest: Optional[Path] = None, label: Optional[str] = None,
                dry_run: bool = False) -> Dict[str, Any]:
    """Take a pg_basebackup full backup. Returns a result dict."""
    _which_or_raise("pg_basebackup")
    dest = Path(dest) if dest else DEFAULT_PITR_DEST
    label = label or ("base_" + time.strftime("%Y%m%d_%H%M%S", time.gmtime()))
    out_dir = dest / "base" / label
    cmd = build_base_backup_cmd(out_dir, label)

    if dry_run:
        return {"dry_run": True, "command": cmd, "dest": str(out_dir), "label": label}

    out_dir.mkdir(parents=True, exist_ok=True)
    r = _run(cmd, env=_pg_env(), timeout=3600.0)
    if r.returncode != 0:
        raise PitrError(f"pg_basebackup failed: {(r.stderr or r.stdout).strip()[:2000]}")

    manifest = {
        "label": label,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "command": cmd,
        "files": sorted(p.name for p in out_dir.iterdir()),
        "target_rpo_seconds": TARGET_RPO_SECONDS,
        "target_rto_seconds": TARGET_RTO_SECONDS,
    }
    (out_dir / "pitr-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    logger.info("base backup %s written to %s", label, out_dir)
    return {"label": label, "dest": str(out_dir), "manifest": manifest}


# ---------------------------------------------------------------------------
# wal-snippet (archive_command generation)
# ---------------------------------------------------------------------------


def archive_command_snippet(dest: Path) -> str:
    """Return the postgresql.conf snippet enabling continuous WAL archiving."""
    wal_dir = dest / "wal"
    # `test ! -f` keeps archive_command idempotent across retries; the snippet
    # is written for postgresql.conf `include` / `include_dir`.
    return (
        "# Generated by pg_pitr_backup wal-snippet. Include from postgresql.conf:\n"
        "#   include = 'wal-archive.conf'\n"
        "# Continuous WAL archiving -> RPO <= 15 min (spec §8.2).\n"
        f"archive_mode = on\n"
        f"archive_command = 'test ! -f {wal_dir}/%f && cp %p {wal_dir}/%f'\n"
        f"archive_timeout = 900\n"
    )


def write_wal_snippet(dest: Optional[Path] = None,
                      config_dir: Optional[Path] = None) -> Path:
    """Write the archive_command snippet under backend/config/postgres/."""
    dest = Path(dest) if dest else DEFAULT_PITR_DEST
    config_dir = Path(config_dir) if config_dir else POSTGRES_CONFIG_DIR
    config_dir.mkdir(parents=True, exist_ok=True)
    snippet_path = config_dir / "wal-archive.conf"
    snippet_path.write_text(archive_command_snippet(dest), encoding="utf-8")
    os.chmod(snippet_path, 0o644)
    (dest / "wal").mkdir(parents=True, exist_ok=True)
    return snippet_path


# ---------------------------------------------------------------------------
# restore --target-time
# ---------------------------------------------------------------------------


def build_restore_plan(target_time: datetime, backup_label: str,
                       dest: Path, restore_data_dir: Optional[Path] = None) -> List[str]:
    """Build the ordered, human-readable restore procedure (pure)."""
    base_dir = dest / "base" / backup_label
    wal_dir = dest / "wal"
    data_dir = restore_data_dir or (PG_DATA_DIR.parent / "postgres_restored")
    target_str = target_time.strftime("%Y-%m-%d %H:%M:%S %Z")
    return [
        f"1. STOP the live postgres cluster (./backend/config/postgres/postgres.sh stop) "
        f"so the restore cannot race the running server.",
        f"2. EXTRACT base backup '{backup_label}' from {base_dir} into {data_dir} "
        f"(base.tar.gz, then pg_wal.tar.gz if present).",
        f"3. WRITE recovery signal: create {data_dir}/recovery.signal and set "
        f"restore_command = 'cp {wal_dir}/%f %p' in postgresql.auto.conf.",
        f"4. SET recovery target: recovery_target_time = '{target_str}' "
        f"(PostgreSQL replays WAL up to this point — point-in-time recovery).",
        f"5. FIX ownership/permissions: data dir 0700, owned by the postgres user.",
        f"6. START postgres on {data_dir} and watch logs until 'recovery complete'.",
        f"7. VERIFY: connect, check row counts / application smoke test, then "
        f"promote (touch {data_dir}/promote) or keep as standby.",
        f"8. ROLLBACK PLAN: the original cluster data is untouched until you "
        f"swap it; keep the pre-restore data dir until the application is green.",
    ]


def restore(target_time_str: str, backup_label: Optional[str] = None,
            dest: Optional[Path] = None, restore_data_dir: Optional[Path] = None,
            yes: bool = False, dry_run: bool = True) -> Dict[str, Any]:
    """Point-in-time restore.

    - ``dry_run=True`` (default): returns the plan, changes nothing.
    - ``dry_run=False`` without ``--yes``: raises PitrError demanding explicit
      confirmation (fail closed — a restore destroys the target data dir).
    - ``dry_run=False`` with ``yes=True``: executes the mechanical steps
      (extract + recovery config); the operator still stops/starts postgres
      via postgres.sh as the plan describes.
    """
    dest = Path(dest) if dest else DEFAULT_PITR_DEST
    target_time = parse_target_time(target_time_str)
    label = backup_label or _latest_base_label(dest)
    if not label:
        raise PitrError(f"no base backup found under {dest}/base; run backup-base first.")
    base_dir = dest / "base" / label
    if not (base_dir / "base.tar.gz").is_file():
        raise PitrError(f"base backup {label} is incomplete ({base_dir}/base.tar.gz missing).")

    data_dir = Path(restore_data_dir) if restore_data_dir else (PG_DATA_DIR.parent / "postgres_restored")
    plan = build_restore_plan(target_time, label, dest, data_dir)
    result: Dict[str, Any] = {
        "target_time": target_time.isoformat(),
        "backup_label": label,
        "restore_data_dir": str(data_dir),
        "plan": plan,
        "target_rto_seconds": TARGET_RTO_SECONDS,
    }

    if dry_run:
        result["dry_run"] = True
        return result
    if not yes:
        raise PitrError(
            "restore without --yes requires explicit confirmation: re-run with "
            "--yes to execute the mechanical restore steps. Dry-run plan returned "
            "unchanged."
        )

    # --- mechanical execution (operator stops/starts postgres per the plan) ---
    if data_dir.exists():
        raise PitrError(
            f"refusing to restore over existing {data_dir}; move or remove it first."
        )
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)
    with tarfile.open(base_dir / "base.tar.gz", "r:gz") as tar:
        tar.extractall(path=data_dir)
    wal_tar = base_dir / "pg_wal.tar.gz"
    if wal_tar.is_file():
        with tarfile.open(wal_tar, "r:gz") as tar:
            tar.extractall(path=data_dir / "pg_wal")
    (data_dir / "recovery.signal").write_text("", encoding="utf-8")
    auto_conf = data_dir / "postgresql.auto.conf"
    with open(auto_conf, "a", encoding="utf-8") as fh:
        fh.write(f"\n# written by pg_pitr_backup restore (PITR to {target_time.isoformat()})\n")
        fh.write(f"restore_command = 'cp {dest / 'wal'}/%f %p'\n")
        fh.write(f"recovery_target_time = '{target_time.strftime('%Y-%m-%d %H:%M:%S %z')}'\n")
    result["executed"] = True
    result["executed_steps"] = ["extracted base backup", "wrote recovery.signal",
                                "wrote restore_command + recovery_target_time"]
    logger.info("PITR restore staged in %s for target %s", data_dir, target_time.isoformat())
    return result


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def verify(dest: Optional[Path] = None,
           backup_label: Optional[str] = None) -> Dict[str, Any]:
    """Verify a base backup's integrity.

    Always checks files (manifest + base.tar.gz readable + WAL dir present).
    If the postgres server binaries are available, additionally restores into
    an ephemeral instance and runs a smoke check; otherwise reports
    ``ephemeral_restore: skipped`` explicitly instead of claiming success.
    """
    dest = Path(dest) if dest else DEFAULT_PITR_DEST
    label = backup_label or _latest_base_label(dest)
    if not label:
        raise PitrError(f"no base backup found under {dest}/base; nothing to verify.")
    base_dir = dest / "base" / label
    checks: Dict[str, Any] = {"label": label}

    manifest_path = base_dir / "pitr-manifest.json"
    checks["manifest_present"] = manifest_path.is_file()
    base_tar = base_dir / "base.tar.gz"
    checks["base_tar_present"] = base_tar.is_file()
    try:
        with tarfile.open(base_tar, "r:gz") as tar:
            members = tar.getnames()
        checks["base_tar_readable"] = True
        checks["base_tar_members"] = len(members)
        checks["has_backup_label_file"] = any(m.endswith("backup_label") for m in members)
    except Exception as e:
        checks["base_tar_readable"] = False
        checks["base_tar_error"] = str(e)[:300]
    wal_dir = dest / "wal"
    wal_files = sorted(p.name for p in wal_dir.iterdir()) if wal_dir.is_dir() else []
    checks["wal_segments_archived"] = len(wal_files)
    checks["wal_dir_present"] = wal_dir.is_dir()

    # Ephemeral restore test when binaries exist.
    if shutil.which("initdb") and shutil.which("pg_ctl") and shutil.which("pg_isready"):
        checks["ephemeral_restore"] = _verify_ephemeral(base_dir, wal_dir)
    else:
        checks["ephemeral_restore"] = {
            "status": "skipped",
            "reason": "postgres server binaries (initdb/pg_ctl) not on PATH",
        }

    checks["ok"] = bool(
        checks["manifest_present"] and checks["base_tar_readable"]
        and checks.get("has_backup_label_file")
    )
    return checks


def _verify_ephemeral(base_dir: Path, wal_dir: Path) -> Dict[str, Any]:
    """Restore into a temp instance, start it read-only-ish, smoke check, tear down."""
    import tempfile as _tempfile

    tmp = Path(_tempfile.mkdtemp(prefix="pg_pitr_verify_"))
    data_dir = tmp / "data"
    port = "55433"
    result: Dict[str, Any] = {"status": "unknown"}
    try:
        with tarfile.open(base_dir / "base.tar.gz", "r:gz") as tar:
            tar.extractall(path=data_dir)
        os.chmod(data_dir, 0o700)
        (data_dir / "recovery.signal").write_text("", encoding="utf-8")
        with open(data_dir / "postgresql.auto.conf", "a", encoding="utf-8") as fh:
            fh.write(f"\nrestore_command = 'cp {wal_dir}/%f %p'\n")
            fh.write("recovery_target_timeline = 'latest'\n")
        # Bind to loopback on an unlikely port; never exposed.
        with open(data_dir / "postgresql.auto.conf", "a", encoding="utf-8") as fh:
            fh.write(f"port = {port}\nlisten_addresses = '127.0.0.1'\n")
        start = _run(["pg_ctl", "-D", str(data_dir), "-l", str(tmp / "log"),
                      "-o", f"-p {port}", "start"], timeout=120.0)
        if start.returncode != 0:
            result = {"status": "failed", "stage": "pg_ctl start",
                      "detail": (start.stderr or start.stdout).strip()[:500]}
            return result
        ready = _run(["pg_isready", "-h", "127.0.0.1", "-p", port], timeout=60.0)
        result = {"status": "ok" if ready.returncode == 0 else "failed",
                  "stage": "pg_isready", "rc": ready.returncode}
    except PitrError as e:
        result = {"status": "failed", "stage": "exception", "detail": str(e)[:500]}
    finally:
        try:
            _run(["pg_ctl", "-D", str(data_dir), "-m", "fast", "stop"], timeout=60.0)
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pg-pitr-backup",
        description=(
            "PostgreSQL point-in-time recovery for the platform node. "
            f"Enterprise tier-1 targets: RPO <= {TARGET_RPO_SECONDS // 60} min "
            f"(continuous WAL archiving), RTO <= {TARGET_RTO_SECONDS // 3600} h "
            "(documented, drill-tested restore; see docs/runbooks/gpu-fleet.md)."
        ),
    )
    p.add_argument("--dest", default=None,
                   help=f"PITR destination dir (default: {DEFAULT_PITR_DEST})")
    sub = p.add_subparsers(dest="command", required=True)

    bb = sub.add_parser("backup-base", help="take a pg_basebackup full backup")
    bb.add_argument("--label", default=None, help="backup label (default: base_YYYYMMDD_HHMMSS)")
    bb.add_argument("--dry-run", action="store_true", help="print the command without running it")

    ws = sub.add_parser("wal-snippet", help="generate the archive_command snippet for backend/config/postgres/")
    ws.add_argument("--write", action="store_true",
                    help="write backend/config/postgres/wal-archive.conf (default: print only)")

    rs = sub.add_parser("restore", help="point-in-time restore (dry-run by default)")
    rs.add_argument("--target-time", required=True,
                    help="'now', ISO 8601, 'YYYY-MM-DD HH:MM:SS', or '<N>s|m|h|d ago'")
    rs.add_argument("--backup-label", default=None, help="base backup label (default: latest)")
    rs.add_argument("--restore-dir", default=None, help="target data dir (default: <data>/postgres_restored)")
    rs.add_argument("--yes", action="store_true",
                    help="execute the mechanical restore steps (default: dry-run plan only)")

    vf = sub.add_parser("verify", help="verify a base backup (file checks + ephemeral restore when possible)")
    vf.add_argument("--backup-label", default=None, help="base backup label (default: latest)")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    dest = Path(args.dest) if args.dest else None
    try:
        if args.command == "backup-base":
            res = backup_base(dest=dest, label=args.label, dry_run=args.dry_run)
            print(json.dumps(res, indent=2, default=str))
        elif args.command == "wal-snippet":
            d = dest or DEFAULT_PITR_DEST
            if args.write:
                path = write_wal_snippet(dest=d)
                print(f"wrote {path} (include it from postgresql.conf)")
            else:
                print(archive_command_snippet(d))
        elif args.command == "restore":
            res = restore(
                args.target_time,
                backup_label=args.backup_label,
                dest=dest,
                restore_data_dir=Path(args.restore_dir) if args.restore_dir else None,
                yes=args.yes,
                dry_run=not args.yes,
            )
            print(json.dumps(res, indent=2, default=str))
        elif args.command == "verify":
            res = verify(dest=dest, backup_label=args.backup_label)
            print(json.dumps(res, indent=2, default=str))
            return 0 if res.get("ok") else 1
        return 0
    except PitrError as e:
        print(f"pg-pitr-backup: error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
