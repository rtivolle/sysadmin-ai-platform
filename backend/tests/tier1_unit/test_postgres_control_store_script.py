"""Contract of backend/config/postgres/postgres.sh (native cluster lifecycle).

The tests drive the script with stub binaries on PATH, so they exercise the real
control flow - argument handling, exit codes, password handling and the pg_ctl
invocations - without a PostgreSQL installation.
"""
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "config" / "postgres" / "postgres.sh"
PASSWORD_SENTINEL = "pw-sentinel-do-not-print"

FAKE_TOOLS = {
    "initdb": 'echo "initdb $*" >> "$RECORD"; data=""; while [ "$#" -gt 0 ]; do if [ "$1" = "-D" ]; then data="$2"; fi; shift; done; mkdir -p "$data"; echo "17" > "$data/PG_VERSION"; exit 0',
    "pg_ctl": 'echo "pg_ctl $*" >> "$RECORD"; case " $* " in *" status "*) exit "${PG_CTL_STATUS_EXIT:-1}";; esac; exit 0',
    "pg_isready": 'echo "pg_isready $*" >> "$RECORD"; exit "${PG_ISREADY_EXIT:-0}"',
    "psql": 'echo "psql $*" >> "$RECORD"; printf "%s" "${PSQL_OUT:-}"',
    "pg_dump": 'echo "pg_dump $*" >> "$RECORD"; out=""; for a in "$@"; do case "$a" in --file=*) out="${a#--file=}";; esac; done; printf "dump" > "$out"; exit 0',
    "pg_restore": 'echo "pg_restore $*" >> "$RECORD"; exit 0',
}
BASE_PATH = "/usr/bin:/bin"


class Harness:
    """Runs the real script against stub binaries and an isolated layout."""

    def __init__(self, root, env, record_file):
        self.root = root
        self.env = env
        self.record_file = record_file
        self.path = env["SYSADMIN_POSTGRES_PASSWORD_FILE"]
        self.data_dir = env["SYSADMIN_POSTGRES_DATA_DIR"]

    def run(self, *args, extra_env=None):
        merged = dict(self.env)
        merged.update(extra_env or {})
        return subprocess.run(
            [str(SCRIPT), *args], env=merged, capture_output=True, text=True, timeout=60
        )

    def record(self):
        return self.record_file.read_text()

    def provision(self, *args, extra_env=None):
        return self.run("provision", *args, extra_env=extra_env)


@pytest.fixture
def harness(tmp_path):
    """Stub binaries plus an isolated cluster/run/log/password layout."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in FAKE_TOOLS.items():
        path = bin_dir / name
        path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body + "\n")
        path.chmod(0o755)
    record = tmp_path / "record.txt"
    record.write_text("")
    env = {
        "PATH": f"{bin_dir}:{BASE_PATH}",
        "HOME": str(tmp_path),
        "RECORD": str(record),
        "SYSADMIN_POSTGRES_DATA_DIR": str(tmp_path / "data"),
        "SYSADMIN_POSTGRES_RUN_DIR": str(tmp_path / "run"),
        "SYSADMIN_POSTGRES_LOG_FILE": str(tmp_path / "logs" / "postgres.log"),
        "SYSADMIN_POSTGRES_PASSWORD_FILE": str(tmp_path / "keys" / "postgres-password.key"),
        "SYSADMIN_POSTGRES_PORT": "5599",
        "SYSADMIN_POSTGRES_BIND": "127.0.0.1",
    }
    return Harness(tmp_path, env, record)


def test_missing_binaries_fail_closed_with_the_install_command(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    env = {"PATH": f"{empty}:{BASE_PATH}", "HOME": str(tmp_path)}
    result = subprocess.run([str(SCRIPT), "check"], env=env, capture_output=True, text=True)
    assert result.returncode == 3
    assert "MISSING" in result.stdout
    assert "apt-get install -y postgresql" in result.stderr
    assert "never installs packages" in result.stderr


def test_check_lists_every_tool_when_installed(harness):
    result = harness.run("check")
    assert result.returncode == 0
    for tool in ("initdb", "pg_ctl", "pg_isready", "psql", "pg_dump", "pg_restore"):
        assert tool in result.stdout


def test_dsn_output_never_contains_the_password(harness):
    Path(harness.path).parent.mkdir(parents=True, exist_ok=True)
    Path(harness.path).write_text(PASSWORD_SENTINEL + "\n")
    result = harness.run("dsn")
    assert result.returncode == 0
    assert PASSWORD_SENTINEL not in result.stdout
    assert result.stdout.strip() == "postgresql://sysadmin_control:***@127.0.0.1:5599/sysadmin_control"


def test_provision_initialises_the_cluster_and_creates_the_database(harness):
    result = harness.provision()
    assert result.returncode == 0, result.stderr
    assert (Path(harness.data_dir) / "PG_VERSION").read_text().strip() == "17"
    password_file = Path(harness.path)
    assert password_file.exists()
    assert stat.S_IMODE(password_file.stat().st_mode) == 0o600
    record = harness.record()
    assert "initdb" in record and "--pwfile=" in record
    assert "pg_ctl" in record and " start" in record
    assert "-p 5599" in record and "listen_addresses='127.0.0.1'" in record
    assert "CREATE DATABASE" in record
    assert PASSWORD_SENTINEL not in result.stdout + result.stderr


def test_provision_is_idempotent_without_force(harness):
    harness.provision()
    before = harness.record().count("initdb")
    second = harness.provision()
    assert second.returncode == 0
    assert "already initialised" in second.stdout
    assert harness.record().count("initdb") == before


def test_provision_keeps_an_existing_password_file(harness):
    password_file = Path(harness.path)
    password_file.parent.mkdir(parents=True, exist_ok=True)
    password_file.write_text("existing-password\n")
    password_file.chmod(0o600)
    harness.provision()
    assert password_file.read_text().strip() == "existing-password"


def test_start_without_a_cluster_fails_closed(harness):
    result = harness.run("start")
    assert result.returncode == 2
    assert "provision" in result.stderr


def test_health_fails_closed_when_the_server_is_not_ready(harness):
    harness.provision()
    result = harness.run("health", extra_env={"PG_ISREADY_EXIT": "1"})
    assert result.returncode == 3
    assert "unavailable" in result.stderr


def test_status_reports_running_then_stopped(harness):
    harness.provision()
    running = harness.run("status", extra_env={"PG_CTL_STATUS_EXIT": "0"})
    assert running.returncode == 0
    assert "RUNNING" in running.stdout
    stopped = harness.run("status", extra_env={"PG_CTL_STATUS_EXIT": "1"})
    assert stopped.returncode == 1
    assert "STOPPED" in stopped.stdout


def test_stop_is_a_noop_when_not_running(harness):
    harness.provision()
    result = harness.run("stop", extra_env={"PG_CTL_STATUS_EXIT": "1"})
    assert result.returncode == 0
    assert "not running" in result.stdout


def test_backup_refuses_to_dump_a_stopped_cluster(harness):
    harness.provision()
    target = Path(harness.root) / "backup.dump"
    result = harness.run("backup", "--out", str(target), extra_env={"PG_CTL_STATUS_EXIT": "1"})
    assert result.returncode == 3
    assert not target.exists()


def test_backup_writes_a_private_dump(harness):
    harness.provision()
    target = Path(harness.root) / "backup.dump"
    result = harness.run("backup", "--out", str(target), extra_env={"PG_CTL_STATUS_EXIT": "0"})
    assert result.returncode == 0, result.stderr
    assert target.read_text() == "dump"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert "pg_dump" in harness.record()


def test_destroy_requires_an_explicit_yes(harness):
    harness.provision()
    result = harness.run("destroy")
    assert result.returncode == 64
    assert Path(harness.data_dir).exists()
    forced = harness.run("destroy", "--yes", extra_env={"PG_CTL_STATUS_EXIT": "1"})
    assert forced.returncode == 0
    assert not Path(harness.data_dir).exists()


def test_unknown_command_uses_the_usage_exit_code(harness):
    result = harness.run("frobnicate")
    assert result.returncode == 64
    assert "Unknown command" in result.stderr


def test_restore_requires_an_existing_backup(harness):
    harness.provision()
    result = harness.run("restore", "--from", str(Path(harness.root) / "absent.dump"))
    assert result.returncode == 2


def test_help_lists_the_operator_commands(harness):
    result = harness.run("--help")
    assert result.returncode == 0
    assert "provision" in result.stdout
    assert "backup" in result.stdout
