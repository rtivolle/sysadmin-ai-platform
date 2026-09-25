"""
Direct subprocess tests for the least-privilege target executor
(backend/services/target_executor/main.py).

The executor is exercised as a standalone process — exactly as the privileged
`sudo -n /usr/local/libexec/sysadmin-target-exec <verb> <args>` boundary will —
against an allowlist in ``tmp_path`` and a fake ``systemctl`` supplied through
``TARGET_EXEC_SYSTEMCTL`` (honoured only because the test allowlist sets
``test_hooks: true``; a production allowlist must never set that flag).
"""
import hashlib
import json
import os
import subprocess
import sys
import textwrap

import pytest

EXECUTOR_MODULE = "backend.services.target_executor.main"


def _run_executor(env, verb, *args):
    return subprocess.run(
        [sys.executable, "-m", EXECUTOR_MODULE, verb, *args],
        capture_output=True, text=True, env=env, timeout=60,
    )


def _result(proc):
    assert proc.stdout, f"expected JSON on stdout; stderr={proc.stderr!r}"
    return json.loads(proc.stdout)


def _make_fake_systemctl(tmp_path):
    log = tmp_path / "systemctl.log"
    script = tmp_path / "fake-systemctl"
    script.write_text(textwrap.dedent(
        f"""\
        #!/bin/sh
        echo "$@" >> {log}
        case "${{1}}" in
          is-active) echo "active"; exit 0 ;;
          restart|reload) echo "ok"; exit 0 ;;
          *) echo "unknown verb $1"; exit 1 ;;
        esac
        """
    ))
    os.chmod(script, 0o755)
    return script, log


def _make_allowlist(tmp_path, *, test_hooks=True, services=None, destinations=None,
                    staging=None, systemctl=None, file_policy=None):
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir(exist_ok=True)
    data = {
        "schema_version": 1,
        "test_hooks": test_hooks,
        "staging_dir": str(staging_dir),
        "systemctl": systemctl or "/usr/bin/systemctl",
        "timeout_seconds": 30,
        "services": services or ["nginx", "traefik", "valkey"],
        "destinations": destinations or [{"path": str(tmp_path / "etc"), "type": "dir"}],
        "staging": staging if staging is not None else {"mode": "0600", "owner": "nonroot"},
        "file_policy": file_policy or {},
    }
    path = tmp_path / "allowlist.json"
    path.write_text(json.dumps(data))
    os.chmod(path, 0o644)
    return path


def _env(tmp_path, allowlist_path, systemctl_override=None):
    env = os.environ.copy()
    env["TARGET_EXEC_ALLOWLIST_PATH"] = str(allowlist_path)
    if systemctl_override is not None:
        env["TARGET_EXEC_SYSTEMCTL"] = str(systemctl_override)
    return env


def _sha(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _read(log):
    return log.read_text() if log.exists() else ""


# --------------------------------------------------------------------------- #
# Service verbs
# --------------------------------------------------------------------------- #

def test_service_verbs_via_fake_systemctl(tmp_path):
    fake, log = _make_fake_systemctl(tmp_path)
    allowlist = _make_allowlist(tmp_path, services=["nginx", "valkey"])
    env = _env(tmp_path, allowlist, systemctl_override=fake)

    proc = _run_executor(env, "service-restart", "nginx")
    res = _result(proc)
    assert proc.returncode == 0
    assert res["ok"] is True and res["exit_code"] == 0
    assert res["unit"] == "nginx"

    proc = _run_executor(env, "service-status", "valkey")
    res = _result(proc)
    assert proc.returncode == 0 and res["ok"] is True
    assert "active" in res.get("stdout", "")

    calls = log.read_text().splitlines()
    assert "restart nginx.service" in calls
    assert "is-active valkey.service" in calls


def test_service_unit_with_dot_service_suffix_normalized(tmp_path):
    fake, log = _make_fake_systemctl(tmp_path)
    allowlist = _make_allowlist(tmp_path, services=["nginx"])
    env = _env(tmp_path, allowlist, systemctl_override=fake)
    proc = _run_executor(env, "service-status", "nginx.service")
    assert proc.returncode == 0
    assert "is-active nginx.service" in log.read_text().splitlines()


def test_rejects_non_allowlisted_unit(tmp_path):
    fake, log = _make_fake_systemctl(tmp_path)
    allowlist = _make_allowlist(tmp_path, services=["nginx"])
    env = _env(tmp_path, allowlist, systemctl_override=fake)
    proc = _run_executor(env, "service-restart", "docker")
    res = _result(proc)
    assert proc.returncode == 3 and res["ok"] is False
    assert res["exit_code"] == 3
    # No side effect: fake systemctl was never invoked.
    assert _read(log).strip() == ""


def test_systemctl_override_ignored_without_test_hooks(tmp_path):
    fake, log = _make_fake_systemctl(tmp_path)
    missing = tmp_path / "no-such-systemctl"
    allowlist = _make_allowlist(
        tmp_path, test_hooks=False, services=["nginx"], systemctl=str(missing),
    )
    env = _env(tmp_path, allowlist, systemctl_override=fake)
    proc = _run_executor(env, "service-status", "nginx")
    res = _result(proc)
    # The test_hooks=false allowlist must ignore the env override and try the
    # (non-existent) allowlisted systemctl path, failing without side effects.
    assert proc.returncode != 0 and res["ok"] is False
    assert "failed to invoke systemctl" in res["message"]
    assert _read(log).strip() == ""


# --------------------------------------------------------------------------- #
# config-install / config-rollback
# --------------------------------------------------------------------------- #

def test_config_install_over_existing_file_with_backup(tmp_path):
    dest_dir = tmp_path / "etc"
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / "app.json"
    dest.write_text('{"version": 1}')
    allowlist = _make_allowlist(tmp_path)

    new_content = '{"version": 2}'
    staged = tmp_path / "staging" / "s1"
    staged.write_text(new_content)
    os.chmod(staged, 0o600)

    env = _env(tmp_path, allowlist)
    proc = _run_executor(env, "config-install", str(staged), str(dest), _sha(new_content))
    res = _result(proc)
    assert proc.returncode == 0 and res["ok"] is True
    assert res["sha256"] == _sha(new_content)
    assert dest.read_text() == new_content

    backup = res["backup"]
    assert backup is not None and os.path.exists(backup)
    assert open(backup).read() == '{"version": 1}'


def test_config_install_new_file(tmp_path):
    dest_dir = tmp_path / "etc"
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / "new.json"
    allowlist = _make_allowlist(tmp_path, file_policy={"default_mode": "0600"})

    content = '{"brand": "new"}'
    staged = tmp_path / "staging" / "s2"
    staged.write_text(content)
    os.chmod(staged, 0o600)

    env = _env(tmp_path, allowlist)
    proc = _run_executor(env, "config-install", str(staged), str(dest), _sha(content))
    res = _result(proc)
    assert proc.returncode == 0 and res["ok"] is True
    assert dest.read_text() == content
    assert res["backup"] is None
    import stat
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600


def test_config_rollback_restores_previous_content(tmp_path):
    dest_dir = tmp_path / "etc"
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / "app.json"
    dest.write_text('{"version": 1}')
    allowlist = _make_allowlist(tmp_path)
    env = _env(tmp_path, allowlist)

    new_content = '{"version": 2}'
    staged = tmp_path / "staging" / "s3"
    staged.write_text(new_content)
    os.chmod(staged, 0o600)

    install = _result(_run_executor(env, "config-install", str(staged), str(dest), _sha(new_content)))
    assert install["ok"] is True
    backup = install["backup"]

    rollback = _result(_run_executor(env, "config-rollback", backup, str(dest)))
    assert rollback["ok"] is True
    assert dest.read_text() == '{"version": 1}'


# --------------------------------------------------------------------------- #
# Negative cases — each must take no side effect on disk
# --------------------------------------------------------------------------- #

def test_rejects_non_allowlisted_dest(tmp_path):
    allowlist = _make_allowlist(tmp_path)
    dest = tmp_path / "outside" / "app.json"
    staged = tmp_path / "staging" / "s4"
    staged.write_text("{}")
    os.chmod(staged, 0o600)
    env = _env(tmp_path, allowlist)

    proc = _run_executor(env, "config-install", str(staged), str(dest), _sha("{}"))
    res = _result(proc)
    assert proc.returncode == 3 and res["ok"] is False
    assert "not allow-listed" in res["message"]
    assert not dest.exists()


def test_rejects_symlinked_dest_parent(tmp_path):
    dest_dir = tmp_path / "etc"
    dest_dir.mkdir(exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (dest_dir / "link").symlink_to(outside, target_is_directory=True)
    allowlist = _make_allowlist(tmp_path)

    staged = tmp_path / "staging" / "s5"
    staged.write_text("{}")
    os.chmod(staged, 0o600)
    dest = dest_dir / "link" / "app.json"

    proc = _run_executor(_env(tmp_path, allowlist), "config-install", str(staged), str(dest), _sha("{}"))
    res = _result(proc)
    assert proc.returncode == 3 and res["ok"] is False
    assert not dest.exists() and not (outside / "app.json").exists()


def test_rejects_symlinked_staged_file(tmp_path):
    dest_dir = tmp_path / "etc"
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / "app.json"
    allowlist = _make_allowlist(tmp_path)

    real = tmp_path / "staging" / "real"
    real.write_text("{}")
    os.chmod(real, 0o600)
    link = tmp_path / "staging" / "s-link"
    link.symlink_to(real)

    proc = _run_executor(_env(tmp_path, allowlist), "config-install", str(link), str(dest), _sha("{}"))
    res = _result(proc)
    assert proc.returncode == 3 and res["ok"] is False
    assert "symlink" in res["message"]
    assert not dest.exists()


def test_rejects_traversal(tmp_path):
    allowlist = _make_allowlist(tmp_path)
    staged = tmp_path / "staging" / "s6"
    staged.write_text("{}")
    os.chmod(staged, 0o600)
    dest = f"{tmp_path}/etc/../../etc/shadow"

    proc = _run_executor(_env(tmp_path, allowlist), "config-install", str(staged), dest, _sha("{}"))
    res = _result(proc)
    assert proc.returncode == 3 and res["ok"] is False
    assert "traversal" in res["message"]


def test_rejects_hash_mismatch(tmp_path):
    dest_dir = tmp_path / "etc"
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / "app.json"
    dest.write_text('{"version": 1}')
    allowlist = _make_allowlist(tmp_path)

    staged = tmp_path / "staging" / "s7"
    staged.write_text('{"version": 2}')
    os.chmod(staged, 0o600)

    proc = _run_executor(_env(tmp_path, allowlist), "config-install", str(staged), str(dest), "0" * 64)
    res = _result(proc)
    assert proc.returncode == 3 and res["ok"] is False
    assert "mismatch" in res["message"]
    assert dest.read_text() == '{"version": 1}'
    assert not list(dest_dir.glob(".app.json.*"))


def test_rejects_staged_mode_mismatch(tmp_path):
    dest_dir = tmp_path / "etc"
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / "app.json"
    allowlist = _make_allowlist(tmp_path, staging={"mode": "0640", "owner": "nonroot"})

    staged = tmp_path / "staging" / "s8"
    staged.write_text("{}")
    os.chmod(staged, 0o644)  # world-readable but not group/world-writable

    proc = _run_executor(_env(tmp_path, allowlist), "config-install", str(staged), str(dest), _sha("{}"))
    res = _result(proc)
    assert proc.returncode == 3 and res["ok"] is False
    assert "mode mismatch" in res["message"]
    assert not dest.exists()


def test_rejects_staged_owner_mismatch(tmp_path):
    dest_dir = tmp_path / "etc"
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / "app.json"
    allowlist = _make_allowlist(tmp_path, staging={"mode": "0600", "owner": 999999})

    staged = tmp_path / "staging" / "s9"
    staged.write_text("{}")
    os.chmod(staged, 0o600)

    proc = _run_executor(_env(tmp_path, allowlist), "config-install", str(staged), str(dest), _sha("{}"))
    res = _result(proc)
    assert proc.returncode == 3 and res["ok"] is False
    assert "owner mismatch" in res["message"]
    assert not dest.exists()


def test_rejects_unknown_verb(tmp_path):
    allowlist = _make_allowlist(tmp_path)
    proc = _run_executor(_env(tmp_path, allowlist), "frobnicate")
    res = _result(proc)
    assert proc.returncode == 2 and res["ok"] is False
    assert res["exit_code"] == 2


def test_rejects_extra_args(tmp_path):
    fake, log = _make_fake_systemctl(tmp_path)
    allowlist = _make_allowlist(tmp_path, services=["nginx"])
    proc = _run_executor(_env(tmp_path, allowlist, systemctl_override=fake), "service-restart", "nginx", "extra")
    res = _result(proc)
    assert proc.returncode == 2 and res["ok"] is False
    assert res["exit_code"] == 2
    assert _read(log).strip() == ""
