"""
Adapter-in-sudo-mode tests: ServiceManager / ConfigDeployer delegate privileged
work to the least-privilege executor through a fake ``sudo`` wrapper.

The fake sudo wrapper emulates the privileged context: it records the
invocation and runs the real executor ``run()`` in-process with a test allowlist
in ``tmp_path`` and a fake ``systemctl`` (enabled only because the test allowlist
sets ``test_hooks: true``). This exercises the full adapter → executor chain
without ever requiring real root or a real systemctl.
"""
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from backend.services.target_adapter.config_deployer import ConfigDeployer
from backend.services.target_adapter.executor import TargetExecutorClient
from backend.services.target_adapter.service_manager import ServiceManager

REPO_ROOT = str(Path(__file__).resolve().parents[3])


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _make_sudo_env(tmp_path):
    """Build a coordinated staging dir, allowlist, fake systemctl and fake sudo."""
    staging = tmp_path / "staging"
    staging.mkdir()
    allowed = tmp_path / "etc"
    allowed.mkdir()

    systemctl_log = tmp_path / "systemctl.log"
    fake_systemctl = tmp_path / "fake-systemctl"
    fake_systemctl.write_text(
        f"#!/bin/sh\n"
        f"echo \"$@\" >> {systemctl_log}\n"
        f"case \"$1\" in\n"
        f"  is-active) echo active; exit 0 ;;\n"
        f"  restart|reload) echo ok; exit 0 ;;\n"
        f"  *) echo \"unknown $1\"; exit 1 ;;\n"
        f"esac\n"
    )
    os.chmod(fake_systemctl, 0o755)

    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text(json.dumps({
        "schema_version": 1,
        "test_hooks": True,
        "staging_dir": str(staging),
        "systemctl": "/usr/bin/systemctl",
        "timeout_seconds": 30,
        "services": ["nginx", "valkey"],
        "destinations": [{"path": str(allowed), "type": "dir"}],
        "staging": {"mode": "0600", "owner": "nonroot"},
        "file_policy": {},
    }))
    os.chmod(allowlist, 0o644)

    sudo_log = tmp_path / "sudo.log"
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(
        "import json, os, sys\n"
        f"REPO_ROOT = {REPO_ROOT!r}\n"
        f"ALLOWLIST = {str(allowlist)!r}\n"
        f"SYSTEMCTL = {str(fake_systemctl)!r}\n"
        f"LOG = {str(sudo_log)!r}\n"
        "with open(LOG, 'a') as f:\n"
        "    f.write(json.dumps(sys.argv) + '\\n')\n"
        "sys.path.insert(0, REPO_ROOT)\n"
        "from backend.services.target_executor.main import run\n"
        "verb_args = sys.argv[3:]  # drop [wrapper, '-n', executor_path]\n"
        "env = dict(os.environ)\n"
        "env['TARGET_EXEC_ALLOWLIST_PATH'] = ALLOWLIST\n"
        "env['TARGET_EXEC_SYSTEMCTL'] = SYSTEMCTL\n"
        "result = run(verb_args, env)\n"
        "sys.stdout.write(json.dumps(result) + '\\n')\n"
        "sys.exit(int(result.get('exit_code', 4)))\n"
    )

    fake_sudo = tmp_path / "fake-sudo"
    fake_sudo.write_text(
        f"#!/bin/sh\n"
        f"exec {sys.executable!r} {str(wrapper)!r} \"$@\"\n"
    )
    os.chmod(fake_sudo, 0o755)

    return {
        "sudo": str(fake_sudo),
        "staging": str(staging),
        "allowed": str(allowed),
        "systemctl_log": systemctl_log,
        "sudo_log": sudo_log,
    }


def _client(env):
    return TargetExecutorClient(
        sudo_bin=env["sudo"],
        staging_dir=env["staging"],
        executor_path="/usr/local/libexec/sysadmin-target-exec",
    )


def test_service_manager_sudo_mode_routes_to_executor(tmp_path):
    env = _make_sudo_env(tmp_path)
    mgr = ServiceManager(mode="sudo", executor=_client(env))

    code, stdout, stderr = mgr.execute_action("service_restart", "nginx")
    assert code == 0
    assert "restart nginx.service" in env["systemctl_log"].read_text().splitlines()

    # The adapter went through the sudo wrapper, which saw the fixed verb.
    calls = [json.loads(line) for line in env["sudo_log"].read_text().splitlines()]
    assert calls[0][3] == "service-restart"
    assert calls[0][4] == "nginx"


def test_config_deployer_sudo_mode_stages_and_installs(tmp_path, monkeypatch):
    monkeypatch.setenv("TARGET_CONFIG_ALLOW_TMP", "1")
    env = _make_sudo_env(tmp_path)
    dest = Path(env["allowed"]) / "app.json"
    dest.write_text('{"version": 1}')

    deployer = ConfigDeployer(allow_tmp=True, mode="sudo", executor=_client(env))
    content = '{"version": 2}'
    res = deployer.deploy(
        approval_id="appr-sudo-1",
        user_id="sysadmin-01",
        target_path=str(dest),
        staged_content=content,
        proposed_hash=_sha(content),
        base_hash=_sha('{"version": 1}'),
    )

    assert res["success"] is True
    assert res["status"] == "succeeded"
    assert dest.read_text() == content
    assert res["backup_path"] is not None and os.path.exists(res["backup_path"])
    assert open(res["backup_path"]).read() == '{"version": 1}'
    # The sudo wrapper recorded a config-install verb, not a direct write.
    calls = [json.loads(line) for line in env["sudo_log"].read_text().splitlines()]
    assert calls[0][3] == "config-install"


def test_config_deployer_sudo_mode_rejects_non_allowlisted_dest(tmp_path, monkeypatch):
    monkeypatch.setenv("TARGET_CONFIG_ALLOW_TMP", "1")
    env = _make_sudo_env(tmp_path)
    # Dest passes the adapter's in-process /tmp validation but is NOT in the
    # executor's allowlist (which only contains <tmp>/etc).
    dest = tmp_path / "other" / "app.json"
    content = '{"version": 2}'

    deployer = ConfigDeployer(allow_tmp=True, mode="sudo", executor=_client(env))
    res = deployer.deploy(
        approval_id="appr-sudo-2",
        user_id="sysadmin-01",
        target_path=str(dest),
        staged_content=content,
        proposed_hash=_sha(content),
        expected_target_exists=False,
    )

    assert res["success"] is False
    assert res["status"] == "failed"
    assert "not allow-listed" in res["message"]
    assert not dest.exists()


def test_config_deployer_simulation_mode_does_not_write(tmp_path, monkeypatch):
    monkeypatch.setenv("TARGET_CONFIG_ALLOW_TMP", "1")
    dest = tmp_path / "app.json"
    dest.write_text('{"version": 1}')

    deployer = ConfigDeployer(allow_tmp=True, mode="simulation")
    content = '{"version": 2}'
    res = deployer.deploy(
        approval_id="appr-sim-1",
        user_id="sysadmin-01",
        target_path=str(dest),
        staged_content=content,
        proposed_hash=_sha(content),
    )

    assert res["success"] is True
    assert res["status"] == "succeeded"
    assert dest.read_text() == '{"version": 1}'  # untouched
    assert res["backup_path"] is None
