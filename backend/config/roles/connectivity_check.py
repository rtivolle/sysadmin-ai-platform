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

# role -> list of (label, host_env_key, port_env_key) the role must reach.
_REQUIRED = {
    "web": [
        ("LiteLLM (inference peer)", "PEER_INFERENCE_HOST", "PEER_INFERENCE_PORT"),
        ("Valkey (data peer)", "PEER_DATA_HOST", "PEER_DATA_VALKEY_PORT"),
        ("SeaweedFS (data peer)", "PEER_DATA_HOST", "PEER_DATA_SEAWEEDFS_PORT"),
        ("VictoriaLogs (data peer)", "PEER_DATA_HOST", "PEER_DATA_LOGS_PORT"),
    ],
    "inference": [
        ("Valkey (data peer)", "PEER_DATA_HOST", "PEER_DATA_VALKEY_PORT"),
        ("VictoriaLogs (data peer)", "PEER_DATA_HOST", "PEER_DATA_LOGS_PORT"),
    ],
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
_ENV_KEYS = ("ROLE", "LAN_BIND_IP") + tuple(_DEFAULTS)


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
        print(
            "Aborting: no configuration was applied. Bring the peer services "
            "up (staged order D -> I -> W) and verify the firewall rules, then "
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
