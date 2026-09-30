"""Multi-host role rendering, firewall matrix, connectivity and shell checks (PR-H1).

These tests pin the contract from docs/plans/MULTI_HOST_DEPLOYMENT.md:

  - role `all` renders the checked-in configs byte-for-byte (no-op);
  - `web` points Traefik upstreams at peers; `data` widens the Valkey bind;
  - the per-role nftables rulesets implement the §4 matrix;
  - the pre-apply connectivity check fails closed against a dead port and
    passes against a live listener;
  - the shell entrypoints remain syntactically valid.
"""
import os
import shutil
import socket
import subprocess
import threading

import pytest

from backend.config.roles import connectivity_check, render_config

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
REPO_CONFIG = os.path.join(REPO_ROOT, "backend", "config")
FIREWALL_DIR = os.path.join(REPO_CONFIG, "firewall")


def _read(rel_path):
    with open(os.path.join(REPO_CONFIG, rel_path), encoding="utf-8") as handle:
        return handle.read()


def _firewall(name):
    return _read(os.path.join("firewall", name))


def _chain_body(text, chain):
    """Extract the body of an nftables ``chain <name> { ... }`` block."""
    start = text.index(f"chain {chain} {{")
    rest = text[start:]
    end = rest.index("\n  }")
    return rest[:end]


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def test_all_role_renders_identical_to_checked_in_configs():
    valkey = _read("valkey/valkey.conf")
    dynamic = _read("traefik/dynamic.yml")
    assert render_config.render_valkey_conf("all", "127.0.0.1", valkey) == valkey
    assert render_config.render_traefik_dynamic("all", {}, dynamic) == dynamic


def test_non_relevant_roles_leave_configs_unchanged():
    valkey = _read("valkey/valkey.conf")
    dynamic = _read("traefik/dynamic.yml")
    # inference does not serve Traefik or Valkey.
    assert render_config.render_valkey_conf("inference", "10.0.0.11", valkey) == valkey
    assert render_config.render_traefik_dynamic("inference", {}, dynamic) == dynamic
    # web does not serve Valkey.
    assert render_config.render_valkey_conf("web", "10.0.0.10", valkey) == valkey


def test_web_dynamic_points_upstreams_at_peers():
    dynamic = _read("traefik/dynamic.yml")
    env = {
        "ROLE": "web",
        "PEER_INFERENCE_HOST": "10.0.0.11",
        "PEER_INFERENCE_PORT": "4000",
        "PEER_DATA_HOST": "10.0.0.12",
        "PEER_DATA_LOGS_PORT": "9428",
        "PEER_DATA_SEAWEEDFS_PORT": "8333",
    }
    out = render_config.render_traefik_dynamic("web", env, dynamic)
    assert "http://10.0.0.11:4000" in out
    assert "http://10.0.0.12:8333" in out
    assert "http://10.0.0.12:9428" in out
    # ForwardAuth and the agent platform stay loopback on web.
    assert "http://127.0.0.1:3081" in out
    assert "http://127.0.0.1:3080" in out
    # No leftover loopback for the backends that moved off-box.
    assert "http://127.0.0.1:4000" not in out
    assert "http://127.0.0.1:8333" not in out
    assert "http://127.0.0.1:9428" not in out


def test_data_valkey_adds_lan_bind_and_is_idempotent():
    valkey = _read("valkey/valkey.conf")
    out = render_config.render_valkey_conf("data", "10.0.0.12", valkey)
    assert "bind 127.0.0.1 10.0.0.12" in out
    # Loopback is retained for D-local access (resilience BGSAVE etc.).
    assert "bind 127.0.0.1 10.0.0.12" in out.splitlines()[1]
    # Re-rendering the same input must not duplicate the address.
    assert render_config.render_valkey_conf("data", "10.0.0.12", out) == out


def test_data_valkey_loopback_bind_is_unchanged():
    valkey = _read("valkey/valkey.conf")
    assert render_config.render_valkey_conf("data", "127.0.0.1", valkey) == valkey
    assert render_config.render_valkey_conf("data", "", valkey) == valkey


def test_platform_valkey_adds_lan_bind_like_data():
    valkey = _read("valkey/valkey.conf")
    out = render_config.render_valkey_conf("platform", "10.0.0.20", valkey)
    assert "bind 127.0.0.1 10.0.0.20" in out
    # inference still does not touch Valkey.
    assert render_config.render_valkey_conf("inference", "10.0.0.21", valkey) == valkey


def test_platform_dynamic_reverts_offbox_upstreams_to_loopback():
    dynamic = _read("traefik/dynamic.yml")
    env = {
        "ROLE": "web",
        "PEER_INFERENCE_HOST": "10.0.0.11",
        "PEER_INFERENCE_PORT": "4000",
        "PEER_DATA_HOST": "10.0.0.12",
        "PEER_DATA_LOGS_PORT": "9428",
        "PEER_DATA_SEAWEEDFS_PORT": "8333",
    }
    offbox = render_config.render_traefik_dynamic("web", env, dynamic)
    assert "http://10.0.0.11:4000" in offbox
    # The platform tier serves LiteLLM itself on loopback, so it behaves like
    # `all`: peer-pointing upstreams are reverted.
    back = render_config.render_traefik_dynamic("platform", {}, offbox)
    assert "http://127.0.0.1:4000" in back
    assert "http://10.0.0.11:4000" not in back


def test_render_all_role_all_is_noop(tmp_path):
    (tmp_path / "valkey").mkdir()
    (tmp_path / "traefik").mkdir()
    valkey_src = _read("valkey/valkey.conf")
    dynamic_src = _read("traefik/dynamic.yml")
    (tmp_path / "valkey" / "valkey.conf").write_text(valkey_src)
    (tmp_path / "traefik" / "dynamic.yml").write_text(dynamic_src)

    changed = render_config.render_all("all", {}, str(tmp_path))
    assert changed == []
    assert (tmp_path / "valkey" / "valkey.conf").read_text() == valkey_src
    assert (tmp_path / "traefik" / "dynamic.yml").read_text() == dynamic_src


def test_render_all_web_rewrites_only_dynamic(tmp_path):
    (tmp_path / "valkey").mkdir()
    (tmp_path / "traefik").mkdir()
    valkey_src = _read("valkey/valkey.conf")
    (tmp_path / "valkey" / "valkey.conf").write_text(valkey_src)
    (tmp_path / "traefik" / "dynamic.yml").write_text(_read("traefik/dynamic.yml"))

    env = {
        "ROLE": "web",
        "PEER_INFERENCE_HOST": "10.0.0.11",
        "PEER_DATA_HOST": "10.0.0.12",
    }
    changed = render_config.render_all("web", env, str(tmp_path))
    assert len(changed) == 1
    assert changed[0].endswith("dynamic.yml")
    assert "http://10.0.0.11:4000" in (tmp_path / "traefik" / "dynamic.yml").read_text()
    # valkey.conf untouched on web.
    assert (tmp_path / "valkey" / "valkey.conf").read_text() == valkey_src


# --------------------------------------------------------------------------- #
# Firewall matrix (§4)
# --------------------------------------------------------------------------- #

def test_firewall_user_networks_reach_only_web_and_platform():
    # The user-facing LAN accept (iifname) exists only on the front-door hosts.
    assert "iifname $LAN_IFACE" in _firewall("web.nft")
    assert "iifname $LAN_IFACE" in _firewall("platform.nft")
    assert "iifname $LAN_IFACE" not in _firewall("inference.nft")
    assert "iifname $LAN_IFACE" not in _firewall("data.nft")


def test_firewall_web_front_door_only():
    inp = _chain_body(_firewall("web.nft"), "input")
    out = _chain_body(_firewall("web.nft"), "output")
    # User networks reach only 8443 (TLS) + 8080 (redirect).
    assert "tcp dport 8443 accept" in inp
    assert "tcp dport 8080 accept" in inp
    # No inbound accept of the peer ports.
    for port in ("4000", "6379", "8333", "9428"):
        assert f"dport {port}" not in inp
    # Outbound goes only to the two peers.
    assert "daddr $PEER_INFERENCE_IP tcp dport 4000 accept" in out
    assert "daddr $PEER_DATA_IP tcp dport { 6379, 8333, 9428 } accept" in out


def test_firewall_inference_accepts_engine_from_platform_only():
    inp = _chain_body(_firewall("inference.nft"), "input")
    out = _chain_body(_firewall("inference.nft"), "output")
    # Only the platform host may call the engine (:8000) and the node-agent
    # (:8001).
    assert "saddr $PEER_PLATFORM_IP tcp dport { 8000, 8001 } accept" in inp
    assert "saddr $PEER_WEB_IP" not in inp
    assert "saddr $PEER_DATA_IP" not in inp
    # The node reaches out to the platform only: fleet API (:3080) and the
    # audit outbox replay (:9428).
    assert "daddr $PEER_PLATFORM_IP tcp dport { 3080, 9428 } accept" in out


def test_firewall_platform_front_door_and_fleet_only():
    inp = _chain_body(_firewall("platform.nft"), "input")
    out = _chain_body(_firewall("platform.nft"), "output")
    # User networks reach only 8443 (TLS) + 8080 (redirect).
    assert "tcp dport 8443 accept" in inp
    assert "tcp dport 8080 accept" in inp
    # No inbound accept of the peer ports or of LiteLLM/Valkey.
    for port in ("8000", "8001", "4000", "6379", "8333"):
        assert f"dport {port}" not in inp
    # GPU nodes reach the fleet API (:3080) and VictoriaLogs (:9428).
    assert "saddr $FLEET_NET tcp dport 3080 accept" in inp
    assert "saddr $FLEET_NET tcp dport 9428 accept" in inp
    # Outbound goes only to the GPU fleet: inference traffic + fleet control.
    assert "daddr $FLEET_NET tcp dport { 8000, 8001 } accept" in out


def test_firewall_data_accepts_from_peers_only():
    inp = _chain_body(_firewall("data.nft"), "input")
    # Valkey from W and I.
    assert "saddr $PEER_WEB_IP tcp dport 6379 accept" in inp
    assert "saddr $PEER_INFERENCE_IP tcp dport 6379 accept" in inp
    # VictoriaLogs + SeaweedFS from W only.
    assert "saddr $PEER_WEB_IP tcp dport { 8333, 8888, 9333, 9428 } accept" in inp
    # No other inbound source is permitted.
    assert "saddr $PEER_INFERENCE_IP tcp dport 8333" not in inp


# --------------------------------------------------------------------------- #
# Connectivity check
# --------------------------------------------------------------------------- #

@pytest.fixture
def fake_listener():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    port = server.getsockname()[1]
    stop = threading.Event()

    def serve():
        server.settimeout(0.1)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
                conn.close()
            except socket.timeout:
                continue
            except OSError:
                break

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield "127.0.0.1", port
    stop.set()
    server.close()
    thread.join(timeout=1.0)


def _web_env(host, port):
    return {
        "ROLE": "web",
        "PEER_INFERENCE_HOST": host,
        "PEER_INFERENCE_PORT": str(port),
        "PEER_DATA_HOST": host,
        "PEER_DATA_VALKEY_PORT": str(port),
        "PEER_DATA_SEAWEEDFS_PORT": str(port),
        "PEER_DATA_LOGS_PORT": str(port),
    }


def test_connectivity_web_all_peers_reachable(fake_listener):
    host, port = fake_listener
    assert connectivity_check.check_role("web", _web_env(host, port)) == []


def test_connectivity_web_reports_unreachable_peer():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()
    env = _web_env("127.0.0.1", dead_port)
    failures = connectivity_check.check_role("web", env)
    assert failures, "expected the dead port to be reported"
    # The LiteLLM (inference) peer is one of the required web targets.
    assert any(label.startswith("LiteLLM") for label, _, _ in failures)


def test_connectivity_all_and_data_have_no_required_peers():
    assert connectivity_check.check_role("all", {}) == []
    assert connectivity_check.check_role("data", {}) == []
    assert connectivity_check.check_role("platform", {}) == []


def test_connectivity_inference_reaches_platform_api_and_logs(fake_listener):
    host, port = fake_listener
    env = {
        "ROLE": "inference",
        "PLATFORM_URL": f"http://{host}:{port}",
        "PEER_DATA_HOST": host,
        "PEER_DATA_LOGS_PORT": str(port),
    }
    assert connectivity_check.check_role("inference", env) == []


def test_connectivity_inference_reports_platform_api_target():
    env = {
        "ROLE": "inference",
        "PLATFORM_URL": "https://10.0.0.20:3080",
        "PEER_DATA_HOST": "10.0.0.20",
        "PEER_DATA_LOGS_PORT": "9428",
    }
    targets = connectivity_check.required_targets("inference", env)
    assert ("Platform fleet API (register/heartbeat)", "10.0.0.20", "3080") in targets
    assert ("VictoriaLogs (platform)", "10.0.0.20", "9428") in targets


def test_connectivity_inference_platform_url_without_port_defaults_to_3080():
    env = {"ROLE": "inference", "PLATFORM_URL": "https://10.0.0.20"}
    label, host, port = connectivity_check.platform_api_target(env)
    assert host == "10.0.0.20"
    assert port == "3080"


# --------------------------------------------------------------------------- #
# Shell entrypoints
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("script", ["install.sh", "backend/platform.sh"])
def test_shell_scripts_are_syntactically_valid(script):
    result = subprocess.run(
        ["bash", "-n", os.path.join(REPO_ROOT, script)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"{script}: {result.stderr}"


@pytest.mark.parametrize("script", ["install.sh", "backend/platform.sh"])
def test_shellcheck_when_available(script):
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed")
    result = subprocess.run(
        [
            "shellcheck",
            "--severity=error",
            "--exclude=SC1090,SC1091,SC2034,SC2155",
            os.path.join(REPO_ROOT, script),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"{script}: {result.stderr}"
