"""
Tier 1 Unit Test: update.sh (platform self-update / "modules update").

Drives the real ``update.sh`` against throwaway git checkouts in tmp dirs and
asserts the fail-closed contract:

- ``--check`` exit codes: 0 up to date, 1 updates available, 2 refusal;
- fast-forward application invokes install.sh and the harness installer;
- secrets under ``backend/config/keys/`` are preserved byte-for-byte;
- refusal on a dirty working tree, a diverged history and missing keys;
- the ``--source`` overlay copies code but never keys, data, logs, run or bin;
- every outcome is recorded as JSON lines in ``backend/logs/update.log``.

No network is used: the "remote" is a local bare repository.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
UPDATE_SH = REPO_ROOT / "update.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="git and bash are required to exercise update.sh",
)


def run(args, cwd, input=None):
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, input=input)


def git(cwd, *args):
    res = run(["git", "-C", str(cwd), *args], cwd)
    if res.returncode != 0:
        raise AssertionError(f"git {args} failed: {res.stderr}")
    return res


def write_base_files(root: Path):
    """A minimal but convincing platform checkout skeleton."""
    for sub in (
        "backend/config/keys",
        "backend/bin",
        "backend/data/valkey",
        "backend/run",
        "backend/logs",
        "backend/services",
        "packages/harness-integration",
    ):
        (root / sub).mkdir(parents=True, exist_ok=True)

    install = root / "install.sh"
    install.write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        'DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
        'mkdir -p "${DIR}/backend/logs"\n'
        'touch "${DIR}/backend/logs/installed.flag"\n'
        "exit 0\n"
    )
    install.chmod(0o755)

    platform = root / "backend" / "platform.sh"
    platform.write_text("#!/usr/bin/env bash\nexit 0\n")
    platform.chmod(0o755)

    harness = root / "packages" / "harness-integration" / "install-harness.sh"
    harness.write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        'DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"\n'
        'mkdir -p "${DIR}/backend/logs"\n'
        'touch "${DIR}/backend/logs/harness.flag"\n'
        "exit 0\n"
    )
    harness.chmod(0o755)

    (root / "backend" / "services" / "version.py").write_text('VERSION = "v1"\n')
    (root / ".gitignore").write_text(
        "backend/config/keys/\nbackend/logs/\nbackend/run/\nbackend/bin/\n"
        "backend/data/\nbackend/.venv/\nbackend/.vllm-venv/\n.pytest_cache/\n"
    )


def provision_keys(root: Path):
    keys = root / "backend" / "config" / "keys"
    keys.mkdir(parents=True, exist_ok=True)
    (keys / "master.key").write_text("sk-master-test-secret\n")
    (keys / "valkey-password.key").write_text("pw-test-secret\n")


def install_update_script(root: Path):
    shutil.copy(UPDATE_SH, root / "update.sh")


def build_repo(tmp_path: Path) -> Path:
    """A clone at v1 whose bare origin also contains a newer v2 commit."""
    bare = tmp_path / "upstream.git"
    git(tmp_path, "init", "--bare", str(bare))

    work = tmp_path / "work"
    work.mkdir()
    git(work, "init")
    git(work, "config", "user.email", "update@test.local")
    git(work, "config", "user.name", "update test")
    write_base_files(work)
    # The update script is tracked in the real repository; commit it into the
    # skeleton so the working tree starts clean.
    install_update_script(work)
    git(work, "add", "-A")
    git(work, "commit", "-m", "v1 base")
    git(work, "remote", "add", "origin", str(bare))
    branch = git(work, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    git(work, "push", "-u", "origin", f"HEAD:{branch}")

    other = tmp_path / "other"
    git(tmp_path, "clone", str(bare), str(other))
    git(other, "config", "user.email", "update@test.local")
    git(other, "config", "user.name", "update test")
    (other / "backend" / "services" / "version.py").write_text('VERSION = "v2"\n')
    (other / "backend" / "services" / "marker_v2.txt").write_text("new module\n")
    git(other, "add", "-A")
    git(other, "commit", "-m", "v2 modules update")
    git(other, "push", "origin", "HEAD")
    return work


def updated_checkout(tmp_path: Path):
    work = build_repo(tmp_path)
    provision_keys(work)
    install_update_script(work)
    return work


def test_check_reports_updates_available(tmp_path):
    work = updated_checkout(tmp_path)
    res = run(["bash", "update.sh", "--check"], work)
    assert res.returncode == 1, res.stderr
    assert "available" in res.stdout
    assert "VERSION = \"v1\"" in (work / "backend" / "services" / "version.py").read_text()


def test_apply_fast_forwards_preserves_secrets_and_records_log(tmp_path):
    work = updated_checkout(tmp_path)
    res = run(["bash", "update.sh", "--yes", "--skip-restart"], work)
    assert res.returncode == 0, res.stderr

    # New modules are in place.
    assert 'VERSION = "v2"' in (work / "backend" / "services" / "version.py").read_text()
    assert (work / "backend" / "services" / "marker_v2.txt").exists()
    # Dependencies and harness modules were refreshed.
    assert (work / "backend" / "logs" / "installed.flag").exists()
    assert (work / "backend" / "logs" / "harness.flag").exists()
    # Secrets survived byte-for-byte.
    assert (work / "backend" / "config" / "keys" / "master.key").read_text() == "sk-master-test-secret\n"
    assert (work / "backend" / "config" / "keys" / "valkey-password.key").read_text() == "pw-test-secret\n"

    # The audit log records the update with both revisions.
    lines = (work / "backend" / "logs" / "update.log").read_text().strip().splitlines()
    events = [json.loads(line) for line in lines]
    assert events[-1]["result"] == "updated"
    assert events[-1]["to"] == git(work, "rev-parse", "HEAD").stdout.strip()
    assert events[-1]["restarted"] == []

    # And the platform now reports itself up to date.
    res2 = run(["bash", "update.sh", "--check"], work)
    assert res2.returncode == 0, res2.stderr


def test_check_refuses_dirty_working_tree(tmp_path):
    work = updated_checkout(tmp_path)
    (work / "backend" / "services" / "version.py").write_text('VERSION = "local edit"\n')

    res = run(["bash", "update.sh", "--check"], work)
    assert res.returncode == 2
    assert "uncommitted" in res.stderr

    res2 = run(["bash", "update.sh", "--yes"], work)
    assert res2.returncode == 2
    # The local edit was not overwritten.
    assert 'VERSION = "local edit"' in (work / "backend" / "services" / "version.py").read_text()


def test_force_proceeds_with_unrelated_dirty_file(tmp_path):
    work = updated_checkout(tmp_path)
    (work / "backend" / "notes.txt").write_text("operator notes\n")  # untracked => dirty

    res = run(["bash", "update.sh", "--force", "--yes", "--skip-restart"], work)
    assert res.returncode == 0, res.stderr
    assert 'VERSION = "v2"' in (work / "backend" / "services" / "version.py").read_text()
    assert (work / "backend" / "notes.txt").read_text() == "operator notes\n"


def test_refuses_diverged_history(tmp_path):
    work = updated_checkout(tmp_path)
    git(work, "config", "user.email", "update@test.local")
    git(work, "config", "user.name", "update test")
    (work / "local.txt").write_text("local commit\n")
    git(work, "add", "-A")
    git(work, "commit", "-m", "local commit")

    res = run(["bash", "update.sh", "--check"], work)
    assert res.returncode == 2
    assert "diverged" in res.stderr


def test_refuses_missing_secrets(tmp_path):
    work = build_repo(tmp_path)
    install_update_script(work)

    res = run(["bash", "update.sh", "--check"], work)
    assert res.returncode == 2
    assert "master.key" in res.stderr


def test_confirmation_fails_closed_without_terminal(tmp_path):
    work = updated_checkout(tmp_path)
    res = run(["bash", "update.sh"], work, input="")
    assert res.returncode == 2
    assert "no interactive confirmation" in res.stderr
    # Nothing was applied.
    assert 'VERSION = "v1"' in (work / "backend" / "services" / "version.py").read_text()


def test_confirmation_decline_aborts_cleanly(tmp_path):
    work = updated_checkout(tmp_path)
    res = run(["bash", "update.sh"], work, input="n\n")
    assert res.returncode == 0
    assert "Aborted" in res.stdout
    assert 'VERSION = "v1"' in (work / "backend" / "services" / "version.py").read_text()


def test_overlay_source_excludes_secrets_and_generated_dirs(tmp_path):
    target = build_repo(tmp_path)
    shutil.rmtree(target / ".git")  # simulate a non-git deployment
    provision_keys(target)
    (target / "backend" / "bin" / "traefik").write_text("real binary")
    (target / "backend" / "data" / "valkey" / "db").write_text("real state")
    (target / "backend" / "logs" / "service.log").write_text("real logs")
    (target / "backend" / "run" / "valkey.pid").write_text("12345")
    install_update_script(target)

    source = tmp_path / "source"
    source.mkdir()
    write_base_files(source)
    (source / "backend" / "services" / "version.py").write_text('VERSION = "v2"\n')
    # A malicious or stale source tree must not leak into the host.
    (source / "backend" / "config" / "keys" / "master.key").write_text("EVIL")
    (source / "backend" / "bin" / "traefik").write_text("poison binary")
    (source / "backend" / "data" / "valkey" / "db").write_text("poison state")
    (source / "backend" / "logs" / "service.log").write_text("poison logs")

    res = run(["bash", "update.sh", "--source", str(source), "--yes", "--skip-restart"], target)
    assert res.returncode == 0, res.stderr

    assert 'VERSION = "v2"' in (target / "backend" / "services" / "version.py").read_text()
    assert (target / "backend" / "config" / "keys" / "master.key").read_text() == "sk-master-test-secret\n"
    assert (target / "backend" / "bin" / "traefik").read_text() == "real binary"
    assert (target / "backend" / "data" / "valkey" / "db").read_text() == "real state"
    assert (target / "backend" / "logs" / "service.log").read_text() == "real logs"
    # Dependencies were refreshed on the overlay path too.
    assert (target / "backend" / "logs" / "installed.flag").exists()

    lines = (target / "backend" / "logs" / "update.log").read_text().strip().splitlines()
    assert json.loads(lines[-1])["result"] == "updated"


def test_check_with_source_is_rejected(tmp_path):
    target = build_repo(tmp_path)
    shutil.rmtree(target / ".git")
    provision_keys(target)
    install_update_script(target)

    res = run(["bash", "update.sh", "--check", "--source", str(tmp_path / "source")], target)
    assert res.returncode == 2
    assert "--source" in res.stderr
