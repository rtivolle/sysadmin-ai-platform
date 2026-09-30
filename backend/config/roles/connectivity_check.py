#!/usr/bin/env python3
"""Pre-apply TCP connectivity check for multi-host peers (PR-H1).

Fails closed: if any required peer port is unreachable the installer aborts
before applying any configuration. Runs with the system Python (no venv needed)
because install.sh invokes it before the venv exists.

Usage:
    python3 backend/config/roles/connectivity_check.py [deployment.env]
    ROLE=web PEER_INFERENCE_HOST=10.0.0.11 PEER_DATA_HOST=10.0.0.12 \
        python3 backend/config/roles/connectivity_check.py

Values taken from the process environment win over the file, so the installer
can validate a role before it writes (or overwrites) deployment.env: a failed
check leaves the machine exactly as it was.
"""
import os
import socket
import sys
from urllib.parse import urlparse

# role -> list of (label, host_env_key, port_env_key) the role must reach.
# The Phase-B `inference` (GPU node) role reaches the platform instead of a
# `data` peer: its fleet API endpoint comes from PLATFORM_URL (parsed below),
# and its audit outbox replays to the platform's VictoriaLogs.
_REQUIRED = {
    "web": [
        ("LiteLLM (inference peer)", "PEER_INFERENCE_HOST", "PEER_INFERENCE_PORT"),
        ("Valkey (data peer)", "PEER_DATA_HOST", "PEER_DATA_VALKEY_PORT"),
        ("SeaweedFS (data peer)", "PEER_DATA_HOST", "PEER_DATA_SEAWEEDFS_PORT"),
        ("VictoriaLogs (data peer)", "PEER_DATA_HOST", "PEER_DATA_LOGS_PORT"),
    ],
    "inference": [
        ("VictoriaLogs (platform)", "PEER_DATA_HOST", "PEER_DATA_LOGS_PORT"),
    ],
    "platform": [],
    "data": [],
    "all": [],
}

_DEFAULTS = {
    "PEER_INFERENCE_HOST": "127.0.0.1",
    "PEER_INFERENCE_PORT": "4000",
    "PEER_DATA_HOST": "127.0.0.1",
    "PEER_DATA_VALKEY_PORT": "6379",
    "PEER_DATA_LOGS_PORT": "9428",
    "PEER_DATA_SEAWEEDFS_PORT": "8333",
}

# Keys the environment may supply or override.
_ENV_KEYS = ("ROLE", "LAN_BIND_IP", "PLATFORM_URL", "NODE_NAME",
             "PEER_INFERENCE_HOSTS") + tuple(_DEFAULTS)


def platform_api_target(env):
    """(label, host, port) for the fleet API a GPU node registers to.

    Parsed from PLATFORM_URL (e.g. ``https://<platform-lan-ip>:3080``); an
    absent port defaults to 3080, the agent platform's fleet API port. An empty
    URL yields an empty host, which the TCP probe reports as unreachable — the
    installer already fails closed earlier with a clearer message.
    """
    parsed = urlparse((env.get("PLATFORM_URL") or "").strip())
    host = parsed.hostname or ""
    port = parsed.port or 3080
    return ("Platform fleet API (register/heartbeat)", host, str(port))


def parse_env_file(path):
    env = {}
    if not path or not os.path.isfile(path):
        return env
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def resolve_env(env_path=None, environ=None):
    """Deployment values, with non-empty process environment values winning."""
    env = parse_env_file(env_path)
    source = os.environ if environ is None else environ
    for key in _ENV_KEYS:
        value = source.get(key)
        if value:
            env[key] = value
    return env


def check_tcp(host, port, timeout=3.0):
    """Return True if a TCP connection to ``host:port`` succeeds."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def required_targets(role, env):
    targets = []
    for label, host_key, port_key in _REQUIRED.get(role, []):
        host = env.get(host_key, _DEFAULTS[host_key])
        port = env.get(port_key, _DEFAULTS[port_key])
        targets.append((label, host, port))
    if role == "inference":
        targets.append(platform_api_target(env))
    return targets


def check_role(role, env, timeout=3.0):
    """Return a list of unreachable ``(label, host, port)`` tuples."""
    failures = []
    for label, host, port in required_targets(role, env):
        if not check_tcp(host, port, timeout):
            failures.append((label, host, port))
    return failures


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    env_path = argv[0] if argv else None

    env = resolve_env(env_path)
    role = env.get("ROLE", "all") or "all"

    failures = check_role(role, env)
    if failures:
        print(f"Connectivity check FAILED for role '{role}':", file=sys.stderr)
        for label, host, port in failures:
            print(f"  - cannot reach {label} at {host}:{port}", file=sys.stderr)
        staged = {
            "platform": "platform first, then the GPU nodes",
            "inference": "the platform host first, then this GPU node",
        }.get(role, "staged order D -> I -> W")
        print(
            "Aborting: no configuration was applied. Bring the peer services "
            f"up ({staged}) and verify the firewall rules, then "
            "re-run the installer.",
            file=sys.stderr,
        )
        return 1

    if role in ("all", "data"):
        print(f"Connectivity check passed for role '{role}' (no peers to reach).")
    else:
        print(f"Connectivity check passed for role '{role}'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
