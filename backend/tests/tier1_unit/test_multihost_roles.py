"""Machine-role selection: one host or a three-machine split (PR-H1).

These tests pin the operator-facing contract that
docs/plans/MULTI_HOST_DEPLOYMENT.md describes and docs/multi-host.md documents:

  - the choice is reversible: `--role all` restores the checked-in single-host
    configuration byte-for-byte, so "one machine" stays a real option after a
    split was installed;
  - a split host cannot be configured to talk to itself on loopback (fail
    closed), and a failed pre-apply check leaves nothing behind;
  - the recorded role is reused when no `--role` is passed, which is what makes
    `./update.sh` (it calls install.sh with no flags) safe on a split host;
  - each role starts only its own services and reaches its peers by their
    recorded addresses (ForwardAuth needs VALKEY_HOST/VALKEY_PORT, not only
    VALKEY_URL);
  - the state tier starts with only the key file it actually holds.

Everything runs offline: the shell entrypoints are exercised in scratch trees
with the install half truncated at the first install step, so no binary is
downloaded, no venv is built and no service is started.
"""
import os
import pathlib
import shutil
import subprocess

import pytest

from backend.config.roles import connectivity_check, render_config

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
REPO_CONFIG = REPO_ROOT / "backend" / "config"

DEFAULT_PEERS = {
    "PEER_INFERENCE_HOST": "127.0.0.1",
    "PEER_INFERENCE_PORT": "4000",
    "PEER_DATA_HOST": "127.0.0.1",
    "PEER_DATA_VALKEY_PORT": "6379",
    "PEER_DATA_LOGS_PORT": "9428",
    "PEER_DATA_SEAWEEDFS_PORT": "8333",
}

WEB_PEERS = {
    "LAN_BIND_IP": "10.0.0.10",
    "PEER_INFERENCE_HOST": "10.0.0.11",
    "PEER_INFERENCE_PORT": "4000",
    "PEER_DATA_HOST": "10.0.0.12",
    "PEER_DATA_VALKEY_PORT": "6379",
    "PEER_DATA_LOGS_PORT": "9428",
    "PEER_DATA_SEAWEEDFS_PORT": "8333",
}


def _read(rel_path):
    return (REPO_CONFIG / rel_path).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Rendering round-trips: the role choice must be reversible
# --------------------------------------------------------------------------- #

def test_web_then_all_restores_the_checked_in_dynamic_config(tmp_path):
    (tmp_path / "valkey").mkdir()
    (tmp_path / "traefik").mkdir()
    (tmp_path / "valkey" / "valkey.conf").write_text(_read("valkey/valkey.conf"))
    (tmp_path / "traefik" / "dynamic.yml").write_text(_read("traefik/dynamic.yml"))

    render_config.render_all("web", {"ROLE": "web", **WEB_PEERS}, str(tmp_path))
    rendered = (tmp_path / "traefik" / "dynamic.yml").read_text()
    assert "http://10.0.0.11:4000" in rendered
    assert "http://10.0.0.12:9428" in rendered

    render_config.render_all("all", {}, str(tmp_path))
    assert (tmp_path / "traefik" / "dynamic.yml").read_text() == _read("traefik/dynamic.yml")


def test_data_then_all_restores_the_checked_in_valkey_config(tmp_path):
    (tmp_path / "valkey").mkdir()
    (tmp_path / "valkey" / "valkey.conf").write_text(_read("valkey/valkey.conf"))

    render_config.render_all("data", {"ROLE": "data", "LAN_BIND_IP": "10.0.0.12"}, str(tmp_path))
    assert "bind 127.0.0.1 10.0.0.12" in (tmp_path / "valkey" / "valkey.conf").read_text()

    render_config.render_all("all", {}, str(tmp_path))
    assert (tmp_path / "valkey" / "valkey.conf").read_text() == _read("valkey/valkey.conf")


def test_changing_the_lan_address_replaces_the_previous_bind(tmp_path):
    (tmp_path / "valkey").mkdir()
    (tmp_path / "valkey" / "valkey.conf").write_text(_read("valkey/valkey.conf"))

    render_config.render_all("data", {"ROLE": "data", "LAN_BIND_IP": "10.0.0.12"}, str(tmp_path))
    render_config.render_all("data", {"ROLE": "data", "LAN_BIND_IP": "10.0.0.99"}, str(tmp_path))
    rendered = (tmp_path / "valkey" / "valkey.conf").read_text()
    assert "bind 127.0.0.1 10.0.0.99" in rendered
    assert "10.0.0.12" not in rendered


def test_web_rewrites_an_upstream_chosen_on_a_custom_port(tmp_path):
    """The upstreams are found by service name, not by matching a loopback literal."""
    (tmp_path / "traefik").mkdir()
    custom = _read("traefik/dynamic.yml").replace('"http://127.0.0.1:4000"', '"http://127.0.0.1:4001"')
    (tmp_path / "traefik" / "dynamic.yml").write_text(custom)

    render_config.render_all("web", {"ROLE": "web", **WEB_PEERS}, str(tmp_path))
    rendered = (tmp_path / "traefik" / "dynamic.yml").read_text()
    assert "http://10.0.0.11:4000" in rendered
    assert "4001" not in rendered


def test_all_leaves_a_loopback_upstream_alone(tmp_path):
    """`all` only reverts peer URLs; a single host keeps its chosen local ports."""
    (tmp_path / "traefik").mkdir()
    custom = _read("traefik/dynamic.yml").replace('"http://127.0.0.1:4000"', '"http://127.0.0.1:4001"')
    (tmp_path / "traefik" / "dynamic.yml").write_text(custom)

    assert render_config.render_all("all", {}, str(tmp_path)) == []
    assert (tmp_path / "traefik" / "dynamic.yml").read_text() == custom


# --------------------------------------------------------------------------- #
# Connectivity check: environment wins over the file
# --------------------------------------------------------------------------- #

def test_resolve_env_lets_the_environment_override_the_file(tmp_path):
    env_file = tmp_path / "deployment.env"
    env_file.write_text("ROLE=web\nPEER_DATA_HOST=10.0.0.12\n", encoding="utf-8")

    resolved = connectivity_check.resolve_env(str(env_file), environ={"ROLE": "data"})
    assert resolved["ROLE"] == "data"
    # Values the environment does not set still come from the file.
    assert resolved["PEER_DATA_HOST"] == "10.0.0.12"


def test_resolve_env_without_a_file_uses_the_environment():
    resolved = connectivity_check.resolve_env(None, environ={"ROLE": "inference", "PEER_DATA_HOST": "10.0.0.12"})
    assert resolved["ROLE"] == "inference"
    assert resolved["PEER_DATA_HOST"] == "10.0.0.12"


# --------------------------------------------------------------------------- #
# platform.sh: role-filtered services, peer addresses, role-scoped secrets
# --------------------------------------------------------------------------- #

def _stage_platform(tmp_path, role, peers=None, keys=()):
    """A scratch backend tree containing platform.sh and a recorded role."""
    backend = tmp_path / "backend"
    (backend / "config" / "roles").mkdir(parents=True)
    (backend / "config" / "valkey").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "backend" / "platform.sh", backend / "platform.sh")
    shutil.copy2(REPO_CONFIG / "valkey" / "valkey.conf", backend / "config" / "valkey" / "valkey.conf")

    env = {"ROLE": role, **DEFAULT_PEERS}
    env.update(peers or {})
    (backend / "config" / "roles" / "deployment.env").write_text(
        "# test deployment\n" + "".join(f"{key}={value}\n" for key, value in env.items()),
        encoding="utf-8",
    )

    keys_dir = backend / "config" / "keys"
    keys_dir.mkdir(parents=True)
    for name in keys:
        (keys_dir / f"{name}.key").write_text("unit-test-secret\n", encoding="utf-8")
    return backend


def _run_platform(backend, script):
    """Source platform.sh (as the `status` command) and run `script` after it."""
    return subprocess.run(
        ["bash", "-c", f'source "{backend}/platform.sh" status >/dev/null 2>&1\n{script}'],
        capture_output=True,
        text=True,
        cwd=str(backend.parent),
    )


def test_web_platform_reaches_peers_not_loopback(tmp_path):
    backend = _stage_platform(tmp_path, "web", peers=WEB_PEERS)
    result = _run_platform(
        backend,
        "echo \"VALKEY=$VALKEY_HOST:$VALKEY_PORT\"\n"
        "echo \"VLOGS=$VICTORIALOGS_URL\"\n"
        "echo \"LITELLM=$LITELLM_URL\"\n"
        "echo \"SYSLITELLM=$SYSADMIN_LITELLM_URL\"\n"
        "echo \"SYSVALKEY=$SYSADMIN_VALKEY_HOST:$SYSADMIN_VALKEY_PORT\"\n"
        "echo \"SYSSEA=$SYSADMIN_SEAWEEDFS_HOST:$SYSADMIN_SEAWEEDFS_MASTER_PORT\"\n"
        "echo \"INFERLOCAL=$SYSADMIN_INFERENCE_LOCAL\"",
    )
    assert result.returncode == 0, result.stderr
    # ForwardAuth builds its P1 store from VALKEY_HOST/PORT: loopback here would
    # fail closed with 503 on a web host, where no Valkey runs.
    assert "VALKEY=10.0.0.12:6379" in result.stdout
    assert "VLOGS=http://10.0.0.12:9428" in result.stdout
    assert "LITELLM=http://10.0.0.11:4000/v1" in result.stdout
    assert "SYSLITELLM=http://10.0.0.11:4000/v1" in result.stdout
    assert "SYSVALKEY=10.0.0.12:6379" in result.stdout
    assert "SYSSEA=10.0.0.12:9333" in result.stdout
    # The inference engine binds loopback on its own host, so it is not local here.
    assert "INFERLOCAL=0" in result.stdout


def test_all_platform_keeps_every_address_loopback(tmp_path):
    backend = _stage_platform(tmp_path, "all")
    result = _run_platform(
        backend,
        "echo \"VALKEY=$VALKEY_HOST:$VALKEY_PORT\"\n"
        "echo \"VLOGS=$VICTORIALOGS_URL\"\n"
        "echo \"INFERLOCAL=$SYSADMIN_INFERENCE_LOCAL\"",
    )
    assert result.returncode == 0, result.stderr
    assert "VALKEY=127.0.0.1:6379" in result.stdout
    assert "VLOGS=http://127.0.0.1:9428" in result.stdout
    assert "INFERLOCAL=1" in result.stdout


def test_data_role_loads_secrets_without_the_litellm_master_key(tmp_path):
    """D holds only valkey-password.key; demanding master.key made it unstartable."""
    backend = _stage_platform(tmp_path, "data", peers={"LAN_BIND_IP": "10.0.0.12"}, keys=("valkey-password",))
    result = _run_platform(
        backend,
        "if load_secrets; then echo SECRETS_OK; else echo SECRETS_FAILED; fi\n"
        "cat run/valkey.conf",
    )
    assert "SECRETS_OK" in result.stdout, result.stdout + result.stderr
    runtime_conf = (backend / "run" / "valkey.conf").read_text(encoding="utf-8")
    assert "unit-test-secret" in runtime_conf
    assert "CONFIGURE_VIA_PLATFORM_SH" not in runtime_conf


def test_web_role_still_requires_the_master_key(tmp_path):
    backend = _stage_platform(tmp_path, "web", peers=WEB_PEERS, keys=("valkey-password",))
    result = _run_platform(backend, "if load_secrets; then echo SECRETS_OK; else echo SECRETS_FAILED; fi")
    assert "SECRETS_FAILED" in result.stdout
    # Web never serves Valkey, so it must not render a runtime Valkey config.
    assert not (backend / "run" / "valkey.conf").exists()


def test_role_matrix_lists_each_role_services(tmp_path):
    backend = _stage_platform(tmp_path, "web", peers=WEB_PEERS)
    result = subprocess.run(
        ["bash", str(backend / "platform.sh"), "roles"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "* web        auth_gateway agent_tools audit_outbox traefik harness_gateway" in result.stdout
    assert "  data       valkey victorialogs seaweedfs" in result.stdout
    assert "  inference  inference audit_outbox node_agent" in result.stdout


def test_status_lists_only_this_roles_services(tmp_path):
    backend = _stage_platform(tmp_path, "web", peers=WEB_PEERS)
    result = subprocess.run(
        ["bash", str(backend / "platform.sh"), "status"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "role: web" in result.stdout
    assert "agent_tools" in result.stdout
    # No Valkey *service* line (Valkey is the data peer's, not this host's).
    assert "Port: 6379" not in result.stdout
    # Peers are reported so an operator can tell "down" from "owned elsewhere".
    assert "valkey(data)" in result.stdout
    assert "peer 10.0.0.12:6379" in result.stdout
    assert "litellm(inference)" in result.stdout
    assert "peer 10.0.0.11:4000" in result.stdout


# --------------------------------------------------------------------------- #
# install.sh: recording the choice, failing closed, and switching back
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


def test_installer_records_the_data_role_and_renders_the_lan_bind(tmp_path):
    root = _stage_installer(tmp_path)
    result = _run_installer(root, "--role", "data", "--lan-bind-ip", "10.0.0.12")
    assert result.returncode == 0, result.stdout + result.stderr

    recorded = (root / "backend" / "config" / "roles" / "deployment.env").read_text(encoding="utf-8")
    assert "ROLE=data" in recorded
    assert "LAN_BIND_IP=10.0.0.12" in recorded
    assert "PEER_DATA_HOST=127.0.0.1" in recorded
    valkey = (root / "backend" / "config" / "valkey" / "valkey.conf").read_text(encoding="utf-8")
    assert "bind 127.0.0.1 10.0.0.12" in valkey


def test_installer_switches_a_split_host_back_to_one_machine(tmp_path):
    root = _stage_installer(tmp_path)
    checked_in_valkey = (REPO_CONFIG / "valkey" / "valkey.conf").read_text(encoding="utf-8")
    checked_in_dynamic = (REPO_CONFIG / "traefik" / "dynamic.yml").read_text(encoding="utf-8")

    assert _run_installer(root, "--role", "data", "--lan-bind-ip", "10.0.0.12").returncode == 0
    result = _run_installer(root, "--role", "all")
    assert result.returncode == 0, result.stdout + result.stderr

    recorded = (root / "backend" / "config" / "roles" / "deployment.env").read_text(encoding="utf-8")
    assert "ROLE=all" in recorded
    assert "PEER_DATA_HOST=127.0.0.1" in recorded
    assert "10.0.0.12" not in recorded
    # The rendered files return to the single-host form byte-for-byte.
    assert (root / "backend" / "config" / "valkey" / "valkey.conf").read_text(encoding="utf-8") == checked_in_valkey
    assert (root / "backend" / "config" / "traefik" / "dynamic.yml").read_text(encoding="utf-8") == checked_in_dynamic


def test_installer_reuses_the_recorded_role_when_no_role_flag_is_given(tmp_path):
    """./update.sh runs install.sh with no flags: it must not re-role the host."""
    root = _stage_installer(tmp_path)
    env_path = root / "backend" / "config" / "roles" / "deployment.env"
    recorded = "# recorded\nROLE=data\nLAN_BIND_IP=10.0.0.12\n" + "".join(
        f"{key}={value}\n" for key, value in DEFAULT_PEERS.items()
    )
    env_path.write_text(recorded, encoding="utf-8")

    result = _run_installer(root, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Reusing the recorded machine role 'data'" in result.stdout
    assert "role:        data" in result.stdout
    # A dry run changes nothing, including the recorded role itself.
    assert env_path.read_text(encoding="utf-8") == recorded


def test_installer_dry_run_previews_the_all_in_one_reset(tmp_path):
    root = _stage_installer(tmp_path)
    env_path = root / "backend" / "config" / "roles" / "deployment.env"
    env_path.write_text(
        "# recorded\nROLE=web\nLAN_BIND_IP=10.0.0.10\n"
        + "".join(f"{key}={value}\n" for key, value in WEB_PEERS.items() if key != "LAN_BIND_IP"),
        encoding="utf-8",
    )

    result = _run_installer(root, "--dry-run", "--role", "all")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "role:        all" in result.stdout
    assert "inference:   127.0.0.1:4000" in result.stdout
    assert "data:        127.0.0.1" in result.stdout
    assert env_path.read_text(encoding="utf-8").startswith("# recorded")


def test_installer_dry_run_lists_the_services_each_role_runs(tmp_path):
    root = _stage_installer(tmp_path)
    result = _run_installer(root, "--dry-run", "--role", "all")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "services per role:" in result.stdout
    assert "web        auth_gateway agent_tools audit_outbox traefik harness_gateway" in result.stdout


@pytest.mark.parametrize(
    "args,message",
    [
        (("--role", "web"), "needs this machine's LAN address"),
        (("--role", "web", "--lan-bind-ip", "10.0.0.10"), "needs the inference host"),
        (("--role", "web", "--lan-bind-ip", "10.0.0.10", "--peer-inference", "10.0.0.11"), "needs the data host"),
        (("--role", "inference", "--lan-bind-ip", "10.0.0.10"), "needs the platform URL"),
        (
            ("--role", "web", "--lan-bind-ip", "127.0.0.1", "--peer-inference", "10.0.0.11", "--peer-data", "10.0.0.12"),
            "needs this machine's LAN address",
        ),
    ],
)
def test_installer_fails_closed_on_an_incomplete_split(tmp_path, args, message):
    root = _stage_installer(tmp_path)
    result = _run_installer(root, *args)
    assert result.returncode == 2
    assert message in result.stderr
    # Nothing was recorded: the machine is left exactly as it was.
    assert not (root / "backend" / "config" / "roles" / "deployment.env").exists()


def test_installer_leaves_nothing_behind_when_a_peer_is_unreachable(tmp_path):
    root = _stage_installer(tmp_path)
    script = (root / "install.sh").read_text(encoding="utf-8")
    # Make the pre-apply check fail immediately instead of waiting for TCP.
    patched = script.replace("connectivity_check.py", "connectivity_check_failing.py")
    failing = root / "backend" / "config" / "roles" / "connectivity_check_failing.py"
    failing.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    (root / "install.sh").write_text(patched, encoding="utf-8")
    os.chmod(root / "install.sh", 0o755)

    result = _run_installer(
        root,
        "--role",
        "web",
        "--lan-bind-ip",
        "10.0.0.10",
        "--peer-inference",
        "10.0.0.11",
        "--peer-data",
        "10.0.0.12",
    )
    assert result.returncode == 1
    assert "nothing was applied" in result.stderr
    assert not (root / "backend" / "config" / "roles" / "deployment.env").exists()


# --------------------------------------------------------------------------- #
# installer_tui: the same decisions through the wizard
# --------------------------------------------------------------------------- #

def test_tui_role_validation_matches_the_installer_rules():
    from backend.installer_tui import validate_role_config

    assert validate_role_config({"role": "all"}) is None
    assert "LAN address" in validate_role_config({"role": "data", "lan_bind_ip": "127.0.0.1"})
    assert "inference host" in validate_role_config(
        {"role": "web", "lan_bind_ip": "10.0.0.10", "peer_inference_host": "127.0.0.1", "peer_data_host": "10.0.0.12"}
    )
    assert "platform URL" in validate_role_config(
        {"role": "inference", "lan_bind_ip": "10.0.0.11", "peer_data_host": "localhost"}
    )
    assert (
        validate_role_config(
            {
                "role": "web",
                "lan_bind_ip": "10.0.0.10",
                "peer_inference_host": "10.0.0.11",
                "peer_data_host": "10.0.0.12",
            }
        )
        is None
    )


def test_tui_peer_env_forces_loopback_on_a_single_host():
    from backend.installer_tui import _peer_env

    env = _peer_env(
        {
            "role": "all",
            "lan_bind_ip": "10.0.0.12",
            "peer_inference_host": "10.0.0.11",
            "peer_data_host": "10.0.0.12",
        }
    )
    assert env["ROLE"] == "all"
    assert env["LAN_BIND_IP"] == "127.0.0.1"
    assert env["PEER_INFERENCE_HOST"] == "127.0.0.1"
    assert env["PEER_DATA_HOST"] == "127.0.0.1"
    assert env["PEER_DATA_VALKEY_PORT"] == "6379"


def test_tui_records_the_all_role_with_loopback_peers(tmp_path):
    from backend.installer_tui import write_deployment_env

    cfg = {
        "role": "all",
        "lan_bind_ip": "10.0.0.12",
        "peer_inference_host": "10.0.0.11",
        "peer_data_host": "10.0.0.12",
        "peer_inference_port": 4000,
        "peer_data_valkey_port": 6379,
        "peer_data_logs_port": 9428,
        "peer_data_seaweedfs_port": 8333,
    }
    recorded = pathlib.Path(write_deployment_env(cfg, str(tmp_path))).read_text(encoding="utf-8")
    assert "ROLE=all" in recorded
    assert "LAN_BIND_IP=127.0.0.1" in recorded
    assert "PEER_DATA_HOST=127.0.0.1" in recorded
    assert "10.0.0.12" not in recorded


def test_tui_records_a_split_role_with_its_peers(tmp_path):
    from backend.installer_tui import write_deployment_env

    cfg = {
        "role": "inference",
        "lan_bind_ip": "10.0.0.11",
        "peer_inference_host": "127.0.0.1",
        "peer_data_host": "10.0.0.12",
        "peer_inference_port": 4000,
        "peer_data_valkey_port": 6379,
        "peer_data_logs_port": 9428,
        "peer_data_seaweedfs_port": 8333,
        "platform_url": "https://10.0.0.20:3080",
        "node_name": "gpu-01",
    }
    recorded = pathlib.Path(write_deployment_env(cfg, str(tmp_path))).read_text(encoding="utf-8")
    assert "ROLE=inference" in recorded
    assert "LAN_BIND_IP=10.0.0.11" in recorded
    assert "PLATFORM_URL=https://10.0.0.20:3080" in recorded
    assert "NODE_NAME=gpu-01" in recorded
    assert "PEER_DATA_HOST=10.0.0.12" in recorded
