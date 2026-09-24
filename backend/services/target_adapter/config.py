"""
Configuration constants, path boundaries, and strict whitelists for Scoped Target Adapter.
"""
import os
import re
from pathlib import Path
from typing import Optional, Set

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
PROJECT_ROOT = BACKEND_DIR.parent

APPROVAL_TTL_SECONDS = 300

# 1. Allowed Actions
ALLOWED_ACTIONS = frozenset({
    "service_restart",
    "service_reload",
    "service_status",
    "config_deploy",
})

# 2. Canonical 9 Whitelisted Services
CANONICAL_SERVICES = frozenset({
    "nginx",
    "traefik",
    "valkey",
    "victorialogs",
    "seaweedfs",
    "postgresql",
    "dsh-agent",
    "dsh-sysadmin",
    "litellm",
})

SERVICE_ALIASES = {
    "valkey-server": "valkey",
    "victoria-logs": "victorialogs",
    "weed": "seaweedfs",
}

WHITELISTED_SERVICES = CANONICAL_SERVICES | frozenset(SERVICE_ALIASES.keys())

# 3. Whitelisted Target Configuration Roots
WHITELISTED_CONFIG_ROOTS = [
    "/etc/nginx",
    "/etc/traefik",
    "/etc/systemd/system",
    str(BACKEND_DIR / "config"),
    str(BACKEND_DIR / "tests/fixtures/config"),
    str(BACKEND_DIR / "data/test_fixtures/config"),
]

# 4. Sensitive / Forbidden Target Paths
FORBIDDEN_TARGET_PATHS = frozenset({
    "/etc/shadow",
    "/etc/gshadow",
    "/etc/sudoers",
    "/etc/sudoers.d",
    "/etc/passwd",
    "/etc/master.passwd",
    "/proc",
    "/sys",
    "/dev",
    "/boot",
    "/root",
    "/bin",
    "/sbin",
    "/usr/bin",
    "/usr/sbin",
})

_SERVICE_REGEX = re.compile(r"^[a-z0-9_-]+$")


def normalize_service_name(service: str) -> str:
    """Normalizes service name by stripping .service, lowercasing, and resolving aliases."""
    if not service or not isinstance(service, str):
        raise ValueError("Service name cannot be empty")
    s = service.strip().lower()
    if s.endswith(".service"):
        s = s[:-8]
    if not _SERVICE_REGEX.match(s):
        raise ValueError(f"Invalid service name syntax: {service}")
    canonical = SERVICE_ALIASES.get(s, s)
    return canonical


def validate_target_service(service: str) -> str:
    """Validates service against the 9 whitelisted services. Raises PermissionError if not allowed."""
    canonical = normalize_service_name(service)
    if canonical not in CANONICAL_SERVICES:
        raise PermissionError(
            f"Target service '{service}' is not in allowable whitelist: {sorted(list(CANONICAL_SERVICES))}"
        )
    return canonical


def validate_target_config_path(target_path: str, allow_tmp: bool = False) -> str:
    """
    Validates configuration destination path:
    1. Resolves canonical realpath.
    2. Enforces forbidden path rejections.
    3. Confirms containment within whitelisted configuration roots.
    """
    if not target_path or not isinstance(target_path, str):
        raise ValueError("Target configuration path cannot be empty")

    # Resolve path
    norm_path = os.path.realpath(os.path.abspath(os.path.normpath(target_path)))

    # Check forbidden paths
    for forbidden in FORBIDDEN_TARGET_PATHS:
        if norm_path == forbidden or norm_path.startswith(forbidden + os.sep):
            raise PermissionError(f"Target path '{target_path}' is in forbidden system path: {forbidden}")

    allow_tmp_env = allow_tmp or (os.getenv("TARGET_CONFIG_ALLOW_TMP") == "1")

    # Check whitelisted roots
    matched = False
    for root in WHITELISTED_CONFIG_ROOTS:
        norm_root = os.path.realpath(os.path.abspath(root))
        if norm_path == norm_root or norm_path.startswith(norm_root + os.sep):
            matched = True
            break

    if not matched and allow_tmp_env:
        # Check if in temp directory
        temp_dir = os.path.abspath(os.getenv("TMPDIR", "/tmp"))
        if norm_path.startswith(temp_dir + os.sep) or norm_path.startswith("/tmp/"):
            matched = True

    if not matched:
        raise PermissionError(
            f"Target path '{target_path}' is outside whitelisted configuration roots: {WHITELISTED_CONFIG_ROOTS}"
        )

    return norm_path
