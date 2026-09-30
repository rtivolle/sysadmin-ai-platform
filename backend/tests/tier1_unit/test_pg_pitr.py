"""Tier 1 unit tests: Postgres PITR (pg_pitr_backup).

Pure-function tests only: command construction, archive_command snippet
generation, target-time parsing, and the restore confirmation contract
(dry-run by default, explicit confirmation required without --yes).
No postgres server is needed.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.services.resilience import pg_pitr_backup as pitr
from backend.services.resilience.pg_pitr_backup import (
    PitrError,
    archive_command_snippet,
    build_base_backup_cmd,
    build_restore_plan,
    parse_target_time,
    restore,
)


def test_base_backup_command_construction(tmp_path: Path):
    dest = tmp_path / "base" / "base_20260930_120000"
    cmd = build_base_backup_cmd(dest, "base_20260930_120000")
    assert cmd[0] == "pg_basebackup"
    assert "-D" in cmd and str(dest) in cmd
    assert "-l" in cmd and "base_20260930_120000" in cmd
    assert "-X" in cmd and "stream" in cmd  # consistent backup
    assert "-Ft" in cmd  # tar output
    # The password must never appear on the command line.
    joined = " ".join(cmd)
    assert "PGPASSWORD" not in joined


def test_archive_command_snippet(tmp_path: Path):
    dest = tmp_path / "pitr"
    snippet = archive_command_snippet(dest)
    assert "archive_mode = on" in snippet
    assert "archive_command" in snippet
    assert str(dest / "wal") in snippet
    assert "%f" in snippet and "%p" in snippet


def test_parse_target_time_formats():
    now = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)
    assert parse_target_time("now", now=now) == now
    assert parse_target_time("2026-09-30T10:30:00+00:00") == datetime(
        2026, 9, 30, 10, 30, tzinfo=timezone.utc
    )
    assert parse_target_time("2026-09-30 10:30:00") == datetime(
        2026, 9, 30, 10, 30, tzinfo=timezone.utc
    )
    assert parse_target_time("30m ago", now=now) == now - timedelta(minutes=30)
    assert parse_target_time("2h ago", now=now) == now - timedelta(hours=2)
    assert parse_target_time("1d ago", now=now) == now - timedelta(days=1)
    with pytest.raises(PitrError, match="invalid target time"):
        parse_target_time("yesterdayish")


def _seed_base_backup(dest: Path, label: str = "base_20260930_120000") -> Path:
    import tarfile

    base_dir = dest / "base" / label
    base_dir.mkdir(parents=True)
    # Minimal fake base.tar.gz containing a backup_label file.
    with tarfile.open(base_dir / "base.tar.gz", "w:gz") as tar:
        info_path = base_dir / "backup_label"
        info_path.write_text("START WAL LOCATION: 0/1\n", encoding="utf-8")
        tar.add(info_path, arcname="backup_label")
    (base_dir / "pitr-manifest.json").write_text("{}", encoding="utf-8")
    return dest


def test_restore_dry_run_returns_plan_without_changes(tmp_path: Path):
    dest = _seed_base_backup(tmp_path / "pitr")
    res = restore("2026-09-30 10:00:00", dest=dest, dry_run=True)
    assert res["dry_run"] is True
    assert res["backup_label"] == "base_20260930_120000"
    assert len(res["plan"]) >= 7
    assert any("recovery_target_time" in step or "recovery target" in step for step in res["plan"])
    # Nothing was staged.
    assert not (tmp_path / "postgres_restored").exists()
    assert res["target_rto_seconds"] == 2 * 3600


def test_restore_requires_confirmation_without_yes(tmp_path: Path):
    dest = _seed_base_backup(tmp_path / "pitr")
    with pytest.raises(PitrError, match="explicit confirmation"):
        restore("2026-09-30 10:00:00", dest=dest, dry_run=False, yes=False)


def test_restore_fails_closed_without_base_backup(tmp_path: Path):
    with pytest.raises(PitrError, match="no base backup"):
        restore("now", dest=tmp_path / "empty", dry_run=True)


def test_restore_plan_mentions_rollback():
    plan = build_restore_plan(
        datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc),
        "base_20260930_120000",
        Path("/pitr"),
        Path("/restore"),
    )
    joined = "\n".join(plan).lower()
    assert "rollback" in joined
    assert "recovery.signal" in joined


def test_rpo_rto_targets_documented():
    assert pitr.TARGET_RPO_SECONDS == 15 * 60
    assert pitr.TARGET_RTO_SECONDS == 2 * 3600
    help_text = pitr.build_parser().format_help()
    assert "RPO" in help_text and "RTO" in help_text
