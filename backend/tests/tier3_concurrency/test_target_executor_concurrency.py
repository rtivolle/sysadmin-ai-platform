"""
Concurrency behaviour of the least-privilege target executor.

The executor performs an atomic rename-based install; concurrent installs to
the same destination must never corrupt it (the final content is always one
complete staged content, never a mixture), and every install's reported sha256
must match exactly the content it staged. Service verbs are read-only with
respect to the executor's own state and must return well-formed JSON under
concurrency.
"""
import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

EXECUTOR_MODULE = "backend.services.target_executor.main"


def _sha(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _make_env(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    etc = tmp_path / "etc"
    etc.mkdir()

    fake_systemctl = tmp_path / "fake-systemctl"
    fake_systemctl.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  is-active) echo active; exit 0 ;;\n"
        "  restart|reload) echo ok; exit 0 ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n"
    )
    os.chmod(fake_systemctl, 0o755)

    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text(json.dumps({
        "schema_version": 1,
        "test_hooks": True,
        "staging_dir": str(staging),
        "systemctl": "/usr/bin/systemctl",
        "timeout_seconds": 30,
        "services": ["nginx"],
        "destinations": [{"path": str(etc), "type": "dir"}],
        "staging": {"mode": "0600", "owner": "nonroot"},
        "file_policy": {},
    }))
    os.chmod(allowlist, 0o644)

    env = os.environ.copy()
    env["TARGET_EXEC_ALLOWLIST_PATH"] = str(allowlist)
    env["TARGET_EXEC_SYSTEMCTL"] = str(fake_systemctl)
    return env


def _run(env, *args):
    return subprocess.run(
        [sys.executable, "-m", EXECUTOR_MODULE, *args],
        capture_output=True, text=True, env=env, timeout=60,
    )


def test_concurrent_config_install_is_atomic(tmp_path):
    env = _make_env(tmp_path)
    dest = tmp_path / "etc" / "app.json"
    dest.write_text('{"v": 0}')
    contents = [json.dumps({"v": i, "pad": "x" * 2000}) for i in range(1, 21)]

    staged = []
    for i, content in enumerate(contents):
        p = tmp_path / "staging" / f"s{i}"
        p.write_text(content)
        os.chmod(p, 0o600)
        staged.append((p, content))

    def do(pair):
        p, content = pair
        return json.loads(_run(env, "config-install", str(p), str(dest), _sha(content)).stdout)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(do, staged))

    # The destination is exactly one complete content, never a mixture.
    assert dest.read_text() in contents
    # Every successful install reported the exact sha it staged.
    for (_, content), res in zip(staged, results):
        if res["ok"]:
            assert res["sha256"] == _sha(content)
    # Atomicity does not silently fail: at least one install succeeded.
    assert any(res["ok"] for res in results)


def test_concurrent_service_status_is_well_formed(tmp_path):
    env = _make_env(tmp_path)

    def do(_):
        proc = _run(env, "service-status", "nginx")
        return proc.returncode, json.loads(proc.stdout)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(do, range(24)))

    for code, res in results:
        assert code == 0
        assert res["ok"] is True and res["exit_code"] == 0
