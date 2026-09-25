#!/usr/bin/env python3
"""Render role-specific config files for the multi-host split (PR-H1).

This is the single source of truth for the differences between the single-host
``all`` role and the ``web`` / ``inference`` / ``data`` roles. For ``all`` the
renderer reproduces the checked-in files byte-for-byte, so running it is a
no-op on a single host.

Reads ``backend/config/roles/deployment.env`` (absent => ``all``). Writes, in
``backend/config``:

  - ``valkey/valkey.conf``      — adds the LAN bind address for ``data``
  - ``traefik/dynamic.yml``     — LiteLLM / SeaweedFS / VictoriaLogs upstream
                                  URLs point at peers for ``web``

Nothing else is role-dependent:

  - LiteLLM's model ``api_base`` stays loopback: the inference engine always
    runs on the same machine as LiteLLM (the ``inference`` host).
  - Traefik keeps binding every interface; ``backend/config/firewall/web.nft``
    restricts who may reach it.
  - ForwardAuth / agent platform / sandbox stay loopback on ``web``.
"""
import os
import sys

DEFAULT_ROLE = "all"

DEFAULT_PEERS = {
    "PEER_INFERENCE_HOST": "127.0.0.1",
    "PEER_INFERENCE_PORT": "4000",
    "PEER_DATA_HOST": "127.0.0.1",
    "PEER_DATA_VALKEY_PORT": "6379",
    "PEER_DATA_LOGS_PORT": "9428",
    "PEER_DATA_SEAWEEDFS_PORT": "8333",
}

# The three upstream URLs in traefik/dynamic.yml that move off loopback on web.
# Keys are the loopback literal as written in the checked-in file.
_DYNAMIC_UPSTREAMS = {
    "http://127.0.0.1:4000": ("PEER_INFERENCE_HOST", "PEER_INFERENCE_PORT"),
    "http://127.0.0.1:8333": ("PEER_DATA_HOST", "PEER_DATA_SEAWEEDFS_PORT"),
    "http://127.0.0.1:9428": ("PEER_DATA_HOST", "PEER_DATA_LOGS_PORT"),
}


def parse_env_file(path):
    """Parse ``KEY=VALUE`` lines, ignoring comments and blanks."""
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


def resolve_config_dir():
    here = os.path.dirname(os.path.abspath(__file__))  # .../config/roles
    return os.path.abspath(os.path.join(here, ".."))


def render_valkey_conf(role, lan_bind_ip, existing):
    """Return the valkey.conf content for ``role``.

    ``data`` adds the LAN address to ``bind`` while keeping loopback for
    D-local access (resilience BGSAVE etc.). Every other role leaves the
    file unchanged.
    """
    if role != "data":
        return existing
    lan_bind_ip = (lan_bind_ip or "").strip()
    if not lan_bind_ip or lan_bind_ip == "127.0.0.1":
        return existing
    lines = []
    for line in existing.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("bind ") and lan_bind_ip not in line.split():
            line = line.rstrip() + f" {lan_bind_ip}"
        lines.append(line)
    return "\n".join(lines) + ("\n" if existing.endswith("\n") else "")


def render_traefik_dynamic(role, env, existing):
    """Return the dynamic.yml content for ``role``.

    ``web`` points the LiteLLM / SeaweedFS / VictoriaLogs backends at the
    peer addresses. ``auth-service`` and ``agent-service`` stay loopback
    (they live on ``web``). Every other role leaves the file unchanged.
    """
    if role != "web":
        return existing
    rendered = existing
    for loopback, (host_key, port_key) in _DYNAMIC_UPSTREAMS.items():
        host = env.get(host_key, DEFAULT_PEERS[host_key])
        port = env.get(port_key, DEFAULT_PEERS[port_key])
        replacement = f"http://{host}:{port}"
        if replacement != loopback:
            rendered = rendered.replace(loopback, replacement)
    return rendered


def render_all(role, env, config_dir):
    """Render every role-dependent file under ``config_dir``. Returns a list of
    changed file paths (empty when the tree is already correct for ``role``).
    """
    changed = []

    valkey_path = os.path.join(config_dir, "valkey", "valkey.conf")
    if os.path.isfile(valkey_path):
        with open(valkey_path, "r", encoding="utf-8") as handle:
            valkey = handle.read()
        new_valkey = render_valkey_conf(role, env.get("LAN_BIND_IP", ""), valkey)
        if new_valkey != valkey:
            with open(valkey_path, "w", encoding="utf-8") as handle:
                handle.write(new_valkey)
            changed.append(valkey_path)

    dynamic_path = os.path.join(config_dir, "traefik", "dynamic.yml")
    if os.path.isfile(dynamic_path):
        with open(dynamic_path, "r", encoding="utf-8") as handle:
            dynamic = handle.read()
        new_dynamic = render_traefik_dynamic(role, env, dynamic)
        if new_dynamic != dynamic:
            with open(dynamic_path, "w", encoding="utf-8") as handle:
                handle.write(new_dynamic)
            changed.append(dynamic_path)

    return changed


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    config_dir = resolve_config_dir()
    env_path = None

    args = iter(argv)
    for arg in args:
        if arg in ("--config-dir",):
            config_dir = next(args)
        elif arg in ("--env",):
            env_path = next(args)
        else:
            # First positional argument is the deployment env path.
            env_path = arg

    if env_path is None:
        env_path = os.path.join(config_dir, "roles", "deployment.env")

    env = parse_env_file(env_path)
    role = env.get("ROLE", DEFAULT_ROLE) or DEFAULT_ROLE

    changed = render_all(role, env, config_dir)
    if changed:
        for path in changed:
            print(f"rendered: {path} (role={role})")
    else:
        print(f"config up to date (role={role})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
