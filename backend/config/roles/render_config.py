#!/usr/bin/env python3
"""Render role-specific config files for the multi-host split (PR-H1).

This is the single source of truth for the differences between the single-host
``all`` role and the ``web`` / ``inference`` / ``data`` roles. Rendering is
**reversible**: switching a machine back to ``all`` (or changing its LAN
address) restores the single-host files, so the installer can offer "one
machine" and "three machines" as a choice instead of a one-way door.

Reads ``backend/config/roles/deployment.env`` (absent => ``all``). Writes, in
``backend/config``:

  - ``valkey/valkey.conf``      — adds the LAN bind address for ``data`` and
                                  removes it again for ``all``
  - ``traefik/dynamic.yml``     — LiteLLM / SeaweedFS / VictoriaLogs upstream
                                  URLs point at peers for ``web``, and revert to
                                  loopback for ``all``

Ownership rules that make the round-trip safe:

  - The Valkey renderer owns every **non-loopback** address on the ``bind`` line.
    Loopback entries are preserved exactly as written, so D-local access
    (resilience BGSAVE and friends) keeps working.
  - The Traefik renderer owns the ``url:`` of the three managed upstreams by
    service name, not by matching a loopback literal, so it still works when an
    operator picked non-default ports. On ``all`` it only rewrites a URL that is
    currently pointing off-box: a loopback URL (default or custom port) is left
    untouched.

Nothing else is role-dependent:

  - LiteLLM's model ``api_base`` stays loopback: the inference engine always
    runs on the same machine as LiteLLM (the ``inference`` host).
  - Traefik keeps binding every interface; ``backend/config/firewall/web.nft``
    restricts who may reach it.
  - ForwardAuth / agent platform / sandbox stay loopback on ``web``.
"""
import os
import sys
from urllib.parse import urlparse

DEFAULT_ROLE = "all"

DEFAULT_PEERS = {
    "PEER_INFERENCE_HOST": "127.0.0.1",
    "PEER_INFERENCE_PORT": "4000",
    "PEER_DATA_HOST": "127.0.0.1",
    "PEER_DATA_VALKEY_PORT": "6379",
    "PEER_DATA_LOGS_PORT": "9428",
    "PEER_DATA_SEAWEEDFS_PORT": "8333",
}

# The three upstreams in traefik/dynamic.yml that move off loopback on ``web``.
# service key -> (peer host env key, peer port env key, single-host loopback port)
_MANAGED_SERVICES = {
    "litellm-service": ("PEER_INFERENCE_HOST", "PEER_INFERENCE_PORT", "4000"),
    "s3-service": ("PEER_DATA_HOST", "PEER_DATA_SEAWEEDFS_PORT", "8333"),
    "audit-service": ("PEER_DATA_HOST", "PEER_DATA_LOGS_PORT", "9428"),
}

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}

# Addresses the Valkey renderer leaves alone on the ``bind`` line.
_LOOPBACK_BINDS = {"127.0.0.1", "::1", "localhost", "-::1"}


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
    D-local access (resilience BGSAVE etc.). ``all`` removes a previously added
    LAN address, so a host switched back to single-host stops binding a
    peer-facing interface. Any other role leaves the file unchanged.
    """
    if role not in ("all", "data"):
        return existing

    lan_bind_ip = (lan_bind_ip or "").strip()
    if role != "data" or lan_bind_ip in _LOOPBACK_BINDS:
        lan_bind_ip = ""

    lines = []
    for line in existing.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("bind "):
            kept = [addr for addr in stripped.split()[1:] if addr in _LOOPBACK_BINDS]
            if lan_bind_ip:
                kept.append(lan_bind_ip)
            line = f"{line[: len(line) - len(stripped)]}bind {' '.join(kept)}"
        lines.append(line)
    return "\n".join(lines) + ("\n" if existing.endswith("\n") else "")


def _service_block(lines, service):
    """Locate a ``services:`` entry by name, at any indentation.

    Returns ``(header_index, header_indent)``, or ``(None, None)`` when the
    service is absent. The indentation is measured rather than assumed so a
    reformatted file still resolves.
    """
    needle = f"{service}:"
    for index, line in enumerate(lines):
        if line.strip() == needle:
            return index, len(line) - len(line.lstrip())
    return None, None


def _service_url(text, service):
    """Return the first ``url:`` value inside a ``services:`` entry, or None."""
    lines = text.splitlines()
    start, indent = _service_block(lines, service)
    if start is None:
        return None
    for line in lines[start + 1 :]:
        stripped = line.strip()
        # The block ends at the next key indented at or above the header.
        if stripped and (len(line) - len(line.lstrip())) <= indent:
            break
        for prefix in ("- url:", "url:"):
            if stripped.startswith(prefix):
                return stripped[len(prefix) :].strip().strip('"').strip("'")
    return None


def _rewrite_service_url(text, service, url):
    """Replace one service's ``url:`` value, leaving every other byte alone.

    Line-oriented on purpose: re-serialising the YAML with a parser would
    reformat unrelated lines, and the ``all`` role must reproduce the
    checked-in file byte-for-byte.
    """
    lines = text.splitlines(keepends=True)
    start, indent = _service_block(lines, service)
    if start is None:
        return text
    for index in range(start + 1, len(lines)):
        line = lines[index]
        stripped = line.strip()
        if stripped and (len(line) - len(line.lstrip())) <= indent:
            break
        prefix = "- url:" if stripped.startswith("- url:") else ("url:" if stripped.startswith("url:") else None)
        if prefix is None:
            continue
        leading = line[: len(line) - len(line.lstrip())]
        ending = "\n" if line.endswith("\n") else ""
        lines[index] = f'{leading}{prefix} "{url}"{ending}'
        return "".join(lines)
    return text


def _is_loopback_url(url):
    return (urlparse(url).hostname or "") in _LOOPBACK_HOSTS


def render_traefik_dynamic(role, env, existing):
    """Return the dynamic.yml content for ``role``.

    ``web`` points the LiteLLM / SeaweedFS / VictoriaLogs backends at the peer
    addresses. ``all`` reverts those three backends to loopback when (and only
    when) they currently point off-box, so a host switched back to single-host
    stops proxying to machines that are no longer part of the deployment.
    ``auth-service`` and ``agent-service`` always stay loopback on ``web``, and
    every other role leaves the file unchanged.
    """
    if role not in ("all", "web"):
        return existing

    rendered = existing
    for service, (host_key, port_key, local_port) in _MANAGED_SERVICES.items():
        current = _service_url(rendered, service)
        if role == "all":
            if current is None or _is_loopback_url(current):
                continue
            target = f"http://127.0.0.1:{local_port}"
        else:
            host = (env.get(host_key) or DEFAULT_PEERS[host_key]).strip()
            port = (env.get(port_key) or DEFAULT_PEERS[port_key]).strip()
            target = f"http://{host}:{port}"
        if current != target:
            rendered = _rewrite_service_url(rendered, service, target)
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
