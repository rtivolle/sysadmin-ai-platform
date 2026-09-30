"""Machine roles for the GPU-fleet split: `platform` and `inference` (Phase B).

Pins the operator-facing contract for the fleet topology
(docs/multi-host.md §9):

  - each role starts only its own services (`role_services`);
  - `export_peer_urls`: platform serves LiteLLM on loopback (no pair), the
    GPU node (inference) has no LITELLM_URL pair at all, and both roles
    publish SYSADMIN_ROLE, PLATFORM_URL, NODE_NAME and PEER_INFERENCE_HOSTS;
  - the GPU node is secretless: it needs no master.key and no
    valkey-password.key, so `load_secrets` succeeds with empty key stores;
  - the installer resolves, validates and records both roles
    (`--platform-url` required on inference, bootstrap hosts recorded on
    platform), and the role matrix stays green;
  - an invalid role is still rejected with exit 2.

Everything runs offline, following the scratch-tree pattern of
test_multihost_roles.py: no binary is downloaded, no venv is built and no
service is started.
"""
import os
import pathlib
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
REPO_CONFIG = REPO_ROOT / "backend" / "config"

PLATFORM_PEERS = {
    "LAN_BIND_IP": "10.0.0.20",
    "PEER_INFERENCE_HOSTS": "10.0.0.21,10.0.0.22",
}

INFERENCE_PEERS = {
    "LAN_BIND_IP": "10.0.0.21",
    "PLATFORM_URL": "https://10.0.0.20:3080",
    "NODE_NAME": "gpu-01",
    "PEER_DATA_HOST": "10.0.0.20",
}


def _stage_platform(tmp_path, role, peers=None):
    """A scratch backend tree containing platform.sh and a recorded role."""
    backend = tmp_path / "backend"
    (backend / "config" / "roles").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "backend" / "platform.sh", backend / "platform.sh")

    env = {"ROLE": role}
    env.update(peers or {})
    (backend / "config" / "roles" / "deployment.env").write_text(
        "# test deployment\n" + "".join(f"{key}={value}\n" for key, value in env.items()),
        encoding="utf-8",
    )
    return backend


def _run_platform(backend, script):
    """Source platform.sh (as the `status` command) and run `script` after it."""
    return subprocess.run(
        ["bash", "-c", f'source "{backend}/platform.sh" status >/dev/null 2>&1\n{script}'],
        capture_output=True,
        text=True,
        cwd=str(backend.parent),
    )


def _role_services(backend, role):
    result = _run_platform(backend, f'ROLE={role} role_services')
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


# --------------------------------------------------------------------------- #
# role_services per role
# --------------------------------------------------------------------------- #

def test_platform_role_services(tmp_path):
    backend = _stage_platform(tmp_path, "platform", PLATFORM_PEERS)
    services = _role_services(backend, "platform")
    assert services == (
        "valkey victorialogs audit_outbox seaweedfs auth_gateway agent_tools "
        "litellm litellm_sync traefik harness_gateway"
    )


def test_inference_role_services(tmp_path):
    backend = _stage_platform(tmp_path, "inference", INFERENCE_PEERS)
    services = _role_services(backend, "inference")
    assert services == "inference audit_outbox node_agent"


def test_all_role_includes_node_agent_and_litellm_sync(tmp_path):
    backend = _stage_platform(tmp_path, "all")
    services = _role_services(backend, "all")
    for name in ("node_agent", "litellm_sync"):
        assert name in services.split()


def test_service_port_covers_the_new_services(tmp_path):
    backend = _stage_platform(tmp_path, "all")
    result = _run_platform(
        backend,
        'echo "node_agent_port=$(service_port node_agent)"\n'
        'echo "litellm_sync_port=$(service_port litellm_sync)"',
    )
    assert result.returncode == 0, result.stderr
    assert "node_agent_port=8001" in result.stdout
    # litellm_sync is a portless daemon; the "-" placeholder keeps `status`
    # and `stop_all` table-driven.
    assert "litellm_sync_port=-" in result.stdout


# --------------------------------------------------------------------------- #
# export_peer_urls + SYSADMIN_ROLE
# --------------------------------------------------------------------------- #

def test_platform_serves_litellm_on_loopback(tmp_path):
    backend = _stage_platform(tmp_path, "platform", PLATFORM_PEERS)
    result = _run_platform(
        backend,
        'echo "ROLE=$SYSADMIN_ROLE"\n'
        'echo "LITELLM=$LITELLM_URL"\n'
        'echo "NODE=$NODE_NAME"\n'
        'echo "HOSTS=$PEER_INFERENCE_HOSTS"\n',
    )
    assert result.returncode == 0, result.stderr
    assert "ROLE=platform" in result.stdout
    assert "LITELLM=http://127.0.0.1:4000/v1" in result.stdout
    # NODE_NAME defaults to the machine hostname when unset.
    assert "NODE=" in result.stdout and "NODE=\n" not in result.stdout
    assert "HOSTS=10.0.0.21,10.0.0.22" in result.stdout


def test_inference_has_no_litellm_pair(tmp_path):
    backend = _stage_platform(tmp_path, "inference", INFERENCE_PEERS)
    result = _run_platform(
        backend,
        'echo "ROLE=$SYSADMIN_ROLE"\n'
        'echo "LITELLM=${LITELLM_URL-unset}"\n'
        'echo "SYSLITELLM=${SYSADMIN_LITELLM_URL-unset}"\n'
        'echo "PLATFORM_URL=$PLATFORM_URL"\n'
        'echo "NODE=$NODE_NAME"\n',
    )
    assert result.returncode == 0, result.stderr
    assert "ROLE=inference" in result.stdout
    # The GPU node has no inference peer: no LITELLM_URL pair is exported.
    assert "LITELLM=unset" in result.stdout
    assert "SYSLITELLM=unset" in result.stdout
    assert "PLATFORM_URL=https://10.0.0.20:3080" in result.stdout
    assert "NODE=gpu-01" in result.stdout


def test_role_matrix_lists_platform_and_inference(tmp_path):
    backend = _stage_platform(tmp_path, "platform", PLATFORM_PEERS)
    result = subprocess.run(
        ["bash", str(backend / "platform.sh"), "roles"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    # The staged role is the marked one.
    assert "* platform   valkey victorialogs audit_outbox seaweedfs auth_gateway agent_tools litellm litellm_sync traefik harness_gateway" in result.stdout
    assert "  inference  inference audit_outbox node_agent" in result.stdout


# --------------------------------------------------------------------------- #
# Secretless GPU node
# --------------------------------------------------------------------------- #

def test_inference_role_needs_no_key_files(tmp_path):
    """The GPU node is secretless: load_secrets succeeds with empty key stores."""
    backend = _stage_platform(tmp_path, "inference", INFERENCE_PEERS)
    result = _run_platform(
        backend,
        "if load_secrets; then echo SECRETS_OK; else echo SECRETS_FAILED; fi\n"
        "role_needs_master_key && echo NEEDS_MASTER || echo NO_MASTER\n"
        "role_needs_valkey_password && echo NEEDS_VALKEY || echo NO_VALKEY",
    )
    assert "SECRETS_OK" in result.stdout, result.stdout + result.stderr
    assert "NO_MASTER" in result.stdout
    assert "NO_VALKEY" in result.stdout


def test_platform_role_still_requires_both_key_files(tmp_path):
    backend = _stage_platform(tmp_path, "platform", PLATFORM_PEERS)
    result = _run_platform(
        backend,
        "if load_secrets; then echo SECRETS_OK; else echo SECRETS_FAILED; fi\n"
        "role_needs_master_key && echo NEEDS_MASTER || echo NO_MASTER\n"
        "role_needs_valkey_password && echo NEEDS_VALKEY || echo NO_VALKEY",
    )
    assert "SECRETS_FAILED" in result.stdout
    assert "NEEDS_MASTER" in result.stdout
    assert "NEEDS_VALKEY" in result.stdout


# --------------------------------------------------------------------------- #
# install.sh: role resolution, validation, recording
# --------------------------------------------------------------------------- #

def _stage_installer(tmp_path):
    """Copy install.sh and the config the decision half needs into a scratch tree.

    The script is truncated at the first install step: these tests exercise role
    resolution, validation, the pre-apply check and the write/render path, never
    a download, a venv build or a service start.
    """
    backend = tmp_path / "backend"
    (backend / "config" / "roles").mkdir(parents=True)
    (backend / "config" / "valkey").mkdir(parents=True)
    (backend / "config" / "traefik").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "install.sh", tmp_path / "install.sh")
    shutil.copy2(REPO_ROOT / "backend" / "platform.sh", backend / "platform.sh")
    for name in ("render_config.py", "connectivity_check.py"):
        shutil.copy2(REPO_CONFIG / "roles" / name, backend / "config" / "roles" / name)
    shutil.copy2(REPO_CONFIG / "valkey" / "valkey.conf", backend / "config" / "valkey" / "valkey.conf")
    shutil.copy2(REPO_CONFIG / "traefik" / "dynamic.yml", backend / "config" / "traefik" / "dynamic.yml")

    script = (tmp_path / "install.sh").read_text(encoding="utf-8")
    marker = "\n# 1. Directory Structure\n"
    assert marker in script, "install.sh layout changed: update this test's truncation marker"
    (tmp_path / "install.sh").write_text(script.split(marker)[0] + "\nexit 0\n", encoding="utf-8")
    os.chmod(tmp_path / "install.sh", 0o755)
    return tmp_path


def _run_installer(root, *args):
    return subprocess.run(
        [str(root / "install.sh"), *args], capture_output=True, text=True, cwd=str(root)
    )


def test_installer_dry_run_platform(tmp_path):
    root = _stage_installer(tmp_path)
    result = _run_installer(
        root, "--dry-run", "--role", "platform", "--lan-bind-ip", "10.0.0.20",
        "--peer-inference-hosts", "10.0.0.21,10.0.0.22",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "role:        platform" in result.stdout
    assert "node-name:" in result.stdout
    assert "inference-hosts (bootstrap): 10.0.0.21,10.0.0.22" in result.stdout
    # Dry run records nothing.
    assert not (root / "backend" / "config" / "roles" / "deployment.env").exists()


def test_installer_dry_run_inference(tmp_path):
    root = _stage_installer(tmp_path)
    result = _run_installer(
        root, "--dry-run", "--role", "inference", "--lan-bind-ip", "10.0.0.21",
        "--platform-url", "https://10.0.0.20:3080", "--node-name", "gpu-01",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "role:        inference" in result.stdout
    assert "platform-url: https://10.0.0.20:3080" in result.stdout
    assert "node-name:    gpu-01" in result.stdout


def test_installer_inference_requires_platform_url(tmp_path):
    root = _stage_installer(tmp_path)
    result = _run_installer(root, "--role", "inference", "--lan-bind-ip", "10.0.0.21")
    assert result.returncode == 2
    assert "needs the platform URL" in result.stderr
    assert not (root / "backend" / "config" / "roles" / "deployment.env").exists()


def test_installer_records_platform_and_inference(tmp_path):
    root = _stage_installer(tmp_path)
    result = _run_installer(
        root, "--role", "platform", "--lan-bind-ip", "10.0.0.20",
        "--peer-inference-hosts", "10.0.0.21,10.0.0.22",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    recorded = (root / "backend" / "config" / "roles" / "deployment.env").read_text(encoding="utf-8")
    assert "ROLE=platform" in recorded
    assert "PEER_INFERENCE_HOSTS=10.0.0.21,10.0.0.22" in recorded

    # The GPU node has no platform to dial in a unit test: stub the pre-apply
    # check open (the check itself is covered in test_multihost_render.py).
    root = _stage_installer(tmp_path / "second")
    (root / "backend" / "config" / "roles" / "connectivity_check.py").write_text(
        "import sys\nsys.exit(0)\n", encoding="utf-8"
    )
    result = _run_installer(
        root, "--role", "inference", "--lan-bind-ip", "10.0.0.21",
        "--platform-url", "https://10.0.0.20:3080", "--node-name", "gpu-01",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    recorded = (root / "backend" / "config" / "roles" / "deployment.env").read_text(encoding="utf-8")
    assert "ROLE=inference" in recorded
    assert "PLATFORM_URL=https://10.0.0.20:3080" in recorded
    assert "NODE_NAME=gpu-01" in recorded
    # --peer-data omitted: derived from the platform URL host.
    assert "PEER_DATA_HOST=10.0.0.20" in recorded


def test_installer_rejects_an_invalid_role(tmp_path):
    root = _stage_installer(tmp_path)
    result = _run_installer(root, "--role", "bogus")
    assert result.returncode == 2
    assert "Invalid role" in result.stderr


# --------------------------------------------------------------------------- #
# installer_tui: validation and recording through the wizard helpers
# --------------------------------------------------------------------------- #

def test_tui_validation_platform_and_inference():
    from backend.installer_tui import validate_role_config

    assert validate_role_config({"role": "platform", "lan_bind_ip": "10.0.0.20"}) is None
    assert validate_role_config({"role": "platform", "lan_bind_ip": "127.0.0.1"}) is not None
    assert (
        validate_role_config(
            {
                "role": "inference",
                "lan_bind_ip": "10.0.0.21",
                "platform_url": "https://10.0.0.20:3080",
                "peer_data_host": "",
            }
        )
        is None
    )
    assert "platform URL" in validate_role_config(
        {"role": "inference", "lan_bind_ip": "10.0.0.21", "platform_url": ""}
    )


def test_tui_inference_data_host_derivation():
    from backend.installer_tui import _inference_data_host

    # --peer-data wins when it is not loopback.
    assert _inference_data_host({"peer_data_host": "10.0.0.30", "platform_url": "https://10.0.0.20:3080"}) == "10.0.0.30"
    # Otherwise the host comes from the platform URL.
    assert _inference_data_host({"peer_data_host": "", "platform_url": "https://10.0.0.20:3080"}) == "10.0.0.20"
    assert _inference_data_host({"peer_data_host": "127.0.0.1", "platform_url": "https://10.0.0.20"}) == "10.0.0.20"
    # Nothing usable anywhere: stays loopback, and validation fails closed.
    assert _inference_data_host({"peer_data_host": "", "platform_url": ""}) == ""


def test_tui_peer_env_carries_fleet_keys(tmp_path):
    from backend.installer_tui import _peer_env, write_deployment_env

    env = _peer_env(
        {
            "role": "inference",
            "lan_bind_ip": "10.0.0.21",
            "platform_url": "https://10.0.0.20:3080",
            "node_name": "gpu-01",
            "peer_data_host": "",
        }
    )
    assert env["PLATFORM_URL"] == "https://10.0.0.20:3080"
    assert env["NODE_NAME"] == "gpu-01"
    # install.sh semantics: derived from the platform URL host.
    assert env["PEER_DATA_HOST"] == "10.0.0.20"

    recorded = pathlib.Path(
        write_deployment_env(
            {
                "role": "platform",
                "lan_bind_ip": "10.0.0.20",
                "peer_inference_hosts": "10.0.0.21,10.0.0.22",
            },
            str(tmp_path),
        )
    ).read_text(encoding="utf-8")
    assert "ROLE=platform" in recorded
    assert "PEER_INFERENCE_HOSTS=10.0.0.21,10.0.0.22" in recorded
