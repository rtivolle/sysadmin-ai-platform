"""Tests hermétiques des git hooks du dépôt (.githooks/).

Chaque test crée un dépôt git temporaire (tmp_path) et y exécute les vrais
scripts de hooks — aucune modification du dépôt réel, aucun réseau, aucun
service requis.
"""
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
HOOKS_DIR = REPO_ROOT / ".githooks"
PRE_COMMIT = HOOKS_DIR / "pre-commit"
PRE_PUSH = HOOKS_DIR / "pre-push"
INSTALL_HOOKS = REPO_ROOT / "scripts" / "install-hooks.sh"

# Clé AWS *factice* documentée par AWS pour les exemples — jamais un vrai secret.
FAKE_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
FAKE_GHP = "ghp_" + "x" * 36


def run(cmd, cwd, env=None, timeout=60):
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env
    )


def make_repo(tmp_path: Path) -> Path:
    """Dépôt git vierge avec identité locale (hermétique)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    run(["git", "init", "-q"], cwd=repo)
    run(["git", "config", "user.email", "test@example.com"], cwd=repo)
    run(["git", "config", "user.name", "Test Hooks"], cwd=repo)
    run(["git", "config", "commit.gpgsign", "false"], cwd=repo)
    return repo


def install_hooks_into(repo: Path) -> Path:
    dest = repo / ".githooks"
    shutil.copytree(HOOKS_DIR, dest)
    for hook in ("pre-commit", "pre-push"):
        p = dest / hook
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return dest / "pre-commit"


def git_add(repo: Path, *files: str):
    r = run(["git", "add", *files], cwd=repo)
    assert r.returncode == 0, r.stderr


# --- pre-commit : scan de secrets ------------------------------------------------

def test_precommit_blocks_staged_fake_secret(tmp_path):
    repo = make_repo(tmp_path)
    hook = install_hooks_into(repo)
    (repo / "config.py").write_text(f'AWS_KEY = "{FAKE_AWS_KEY}"\n')
    (repo / "deploy.sh").write_text(
        "#!/usr/bin/env bash\n"
        f"export GITHUB_TOKEN={FAKE_GHP}\n"
    )
    git_add(repo, "config.py", "deploy.sh")

    start = time.monotonic()
    r = run([str(hook)], cwd=repo)
    elapsed = time.monotonic() - start

    assert r.returncode != 0, f"le hook aurait dû bloquer : {r.stderr}"
    assert "config.py:1" in r.stderr
    assert "deploy.sh:2" in r.stderr
    assert "SECRET" in r.stderr
    assert "--no-verify" in r.stderr  # consigne de contournement
    assert elapsed < 10, f"hook trop lent : {elapsed:.1f}s"


def test_precommit_blocks_suspicious_assignment(tmp_path):
    repo = make_repo(tmp_path)
    hook = install_hooks_into(repo)
    (repo / "settings.py").write_text('db_password = "s3cr3t-hunter2-value"\n')
    git_add(repo, "settings.py")

    r = run([str(hook)], cwd=repo)
    assert r.returncode != 0
    assert "settings.py:1" in r.stderr


def test_precommit_ignores_placeholders_and_unstaged(tmp_path):
    repo = make_repo(tmp_path)
    hook = install_hooks_into(repo)
    # Placeholders / références d'env : ne doivent pas bloquer.
    (repo / "settings.py").write_text(
        'password = "changeme"\n'
        "api_key = os.environ[\"API_KEY\"]\n"
        "token = None\n"
    )
    git_add(repo, "settings.py")
    r = run(["git", "commit", "-q", "-m", "clean"], cwd=repo)
    assert r.returncode == 0

    # Secret ajouté mais NON stagé : le hook ne regarde que l'index.
    (repo / "settings.py").write_text(
        (repo / "settings.py").read_text() + f'token = "{FAKE_AWS_KEY}"\n'
    )
    r = run([str(hook)], cwd=repo)
    assert r.returncode == 0, f"faux positif sur lignes non stagées : {r.stderr}"

    # Le même secret stagé doit bloquer.
    git_add(repo, "settings.py")
    r = run([str(hook)], cwd=repo)
    assert r.returncode != 0


def test_precommit_passes_on_clean_files(tmp_path):
    repo = make_repo(tmp_path)
    hook = install_hooks_into(repo)
    (repo / "main.py").write_text("def f():\n    return 1\n")
    (repo / "run.sh").write_text("#!/usr/bin/env bash\necho ok\n")
    (repo / "notes.txt").write_text("rien de secret ici\n")
    git_add(repo, "main.py", "run.sh", "notes.txt")

    r = run([str(hook)], cwd=repo)
    assert r.returncode == 0, f"échec inattendu : {r.stderr}"
    assert "OK" in r.stderr


# --- pre-commit : syntaxe ----------------------------------------------------------

def test_precommit_blocks_bash_syntax_error(tmp_path):
    repo = make_repo(tmp_path)
    hook = install_hooks_into(repo)
    (repo / "broken.sh").write_text("#!/usr/bin/env bash\nif [ -n foo\necho oops\n")
    git_add(repo, "broken.sh")

    r = run([str(hook)], cwd=repo)
    assert r.returncode != 0
    assert "broken.sh" in r.stderr
    assert "bash -n" in r.stderr


def test_precommit_blocks_python_syntax_error(tmp_path):
    repo = make_repo(tmp_path)
    hook = install_hooks_into(repo)
    (repo / "broken.py").write_text("def broken(:\n    pass\n")
    git_add(repo, "broken.py")

    r = run([str(hook)], cwd=repo)
    assert r.returncode != 0
    assert "broken.py" in r.stderr


def test_precommit_without_shellcheck_and_ruff(tmp_path):
    """PATH restreint sans shellcheck/ruff : warn non bloquant, exit 0."""
    repo = make_repo(tmp_path)
    hook = install_hooks_into(repo)
    (repo / "main.py").write_text("def f():\n    return 1\n")
    (repo / "run.sh").write_text("#!/usr/bin/env bash\necho ok\n")
    git_add(repo, "main.py", "run.sh")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("git", "bash", "python3", "grep", "sed", "mktemp", "rm"):
        src = shutil.which(tool)
        assert src, f"{tool} introuvable sur le PATH hôte"
        (bindir / tool).symlink_to(src)
    env = dict(os.environ, PATH=str(bindir))

    r = run([str(hook)], cwd=repo, env=env)
    assert r.returncode == 0, f"bloqué alors que l'env est incomplet : {r.stderr}"
    assert "shellcheck" in r.stderr
    assert "ruff" in r.stderr


# --- scripts/install-hooks.sh ------------------------------------------------------

def test_install_hooks_configures_hooks_path(tmp_path):
    repo = make_repo(tmp_path)
    shutil.copytree(HOOKS_DIR, repo / ".githooks")

    r = run(["bash", str(INSTALL_HOOKS)], cwd=repo)
    assert r.returncode == 0, f"install-hooks.sh a échoué : {r.stderr}"

    r = run(["git", "config", "--get", "core.hooksPath"], cwd=repo)
    assert r.stdout.strip() == ".githooks"

    for hook in ("pre-commit", "pre-push"):
        assert os.access(repo / ".githooks" / hook, os.X_OK)

    # Idempotence : deuxième passage sans erreur ni changement.
    r = run(["bash", str(INSTALL_HOOKS)], cwd=repo)
    assert r.returncode == 0
    r = run(["git", "config", "--get", "core.hooksPath"], cwd=repo)
    assert r.stdout.strip() == ".githooks"


def test_install_hooks_fails_outside_git_repo(tmp_path):
    r = run(["bash", str(INSTALL_HOOKS)], cwd=tmp_path)
    assert r.returncode != 0


# --- pre-push -----------------------------------------------------------------------

def test_prepush_warns_without_venv_but_passes(tmp_path):
    """Sans backend/.venv : avertissement, jamais de blocage sur env incomplet."""
    repo = make_repo(tmp_path)
    hook = repo / ".githooks" / "pre-push"
    shutil.copytree(HOOKS_DIR, repo / ".githooks")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (repo / "backend" / "services").mkdir(parents=True)
    (repo / "backend" / "tests").mkdir(parents=True)
    (repo / "backend" / "services" / "ok.py").write_text("x = 1\n")

    r = run([str(hook)], cwd=repo, timeout=120)
    assert r.returncode == 0, f"pre-push bloqué sur env incomplet : {r.stderr}"
    assert "avertissement" in r.stderr
