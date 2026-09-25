"""Control-store selection, DSN resolution and secret hygiene."""
from pathlib import Path

import pytest

from services.control_store import dsn as dsn_module
from services.control_store.errors import ControlStoreError, ControlStoreUnavailable


def test_default_mode_is_file_and_resolves_no_dsn(monkeypatch):
    monkeypatch.delenv("SYSADMIN_CONTROL_STORE", raising=False)
    monkeypatch.delenv("SYSADMIN_DATABASE_URL", raising=False)
    assert dsn_module.store_mode({}) == dsn_module.MODE_FILE
    assert dsn_module.resolve_dsn({}) is None


def test_explicit_database_url_selects_postgres_mode():
    env = {"SYSADMIN_DATABASE_URL": "postgresql://u:p@db.internal:5432/control"}
    assert dsn_module.store_mode(env) == dsn_module.MODE_POSTGRES
    assert dsn_module.resolve_dsn(env) == env["SYSADMIN_DATABASE_URL"]


def test_unknown_mode_is_rejected_instead_of_defaulting():
    with pytest.raises(ControlStoreError):
        dsn_module.store_mode({"SYSADMIN_CONTROL_STORE": "mysql"})


def test_non_postgres_dsn_scheme_is_rejected():
    with pytest.raises(ControlStoreError):
        dsn_module.resolve_dsn({"SYSADMIN_DATABASE_URL": "sqlite:///tmp/x.db"})


def test_local_cluster_dsn_uses_password_file_and_default_port(tmp_path):
    password_file = tmp_path / "postgres-password.key"
    password_file.write_text("s3cret-value\n")
    env = {"SYSADMIN_CONTROL_STORE": "postgres"}
    resolved = dsn_module.resolve_dsn(env, password_file=password_file)
    assert resolved == "postgresql://sysadmin_control:s3cret-value@127.0.0.1:5433/sysadmin_control"


def test_missing_password_file_fails_closed(tmp_path):
    env = {"SYSADMIN_CONTROL_STORE": "postgres"}
    with pytest.raises(ControlStoreUnavailable):
        dsn_module.resolve_dsn(env, password_file=tmp_path / "absent.key")


def test_blank_password_file_fails_closed(tmp_path):
    password_file = tmp_path / "postgres-password.key"
    password_file.write_text("   \n")
    with pytest.raises(ControlStoreUnavailable):
        dsn_module.resolve_dsn({"SYSADMIN_CONTROL_STORE": "postgres"}, password_file=password_file)


def test_non_numeric_port_is_rejected(tmp_path):
    password_file = tmp_path / "postgres-password.key"
    password_file.write_text("pw")
    env = {"SYSADMIN_CONTROL_STORE": "postgres", "SYSADMIN_POSTGRES_PORT": "port"}
    with pytest.raises(ControlStoreError):
        dsn_module.resolve_dsn(env, password_file=password_file)


def test_redaction_removes_the_password_and_keeps_the_host():
    rendered = dsn_module.redact("postgresql://sysadmin_control:hunter2@127.0.0.1:5433/sysadmin_control")
    assert "hunter2" not in rendered
    assert "127.0.0.1:5433/sysadmin_control" in rendered
    assert dsn_module.redact(None) == "<unconfigured>"


def test_health_reports_file_mode_without_touching_a_database():
    assert dsn_module.store_mode({}) == dsn_module.MODE_FILE
    from services.control_store import health

    report = health({})
    assert report["mode"] == "file"
    assert report["reachable"] is None
    assert "password" not in str(report).lower() or report["dsn"] == "<unconfigured>"


def test_health_reports_unreachable_when_the_password_is_absent(tmp_path):
    from services.control_store import health

    report = health({"SYSADMIN_CONTROL_STORE": "postgres", "SYSADMIN_POSTGRES_PASSWORD_FILE": str(tmp_path / "nope")})
    assert report["mode"] == "postgres"
    assert report["reachable"] is False
