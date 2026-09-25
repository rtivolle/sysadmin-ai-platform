#!/usr/bin/env python3
"""Resolve the durable control store DSN and its mode, without leaking secrets.

Mode is explicit, never inferred from a failed connection:

* ``SYSADMIN_CONTROL_STORE=postgres`` (or an explicit ``SYSADMIN_DATABASE_URL``)
  makes the PostgreSQL key/ledger store authoritative. When it is selected and
  the store cannot be reached, callers must fail closed.
* anything else keeps the prototype's file-based behaviour byte for byte.

The password lives in ``backend/config/keys/postgres-password.key`` (mode
``0600``, Git-ignored), written by ``backend/config/postgres/postgres.sh
provision``. Secrets stay in files; they are never echoed, logged or placed in
an audit event.
"""
import os
from pathlib import Path
from typing import Mapping, Optional
from urllib.parse import quote, urlsplit

from services.control_store.errors import ControlStoreError, ControlStoreUnavailable

MODE_ENV = "SYSADMIN_CONTROL_STORE"
DSN_ENV = "SYSADMIN_DATABASE_URL"
MODE_FILE = "file"
MODE_POSTGRES = "postgres"

DEFAULT_HOST = "127.0.0.1"
# 5433 by default: a host PostgreSQL on 5432 stays untouched, and the platform
# cluster is always the one we provisioned ourselves.
DEFAULT_PORT = 5433
DEFAULT_DB = "sysadmin_control"
DEFAULT_USER = "sysadmin_control"
PASSWORD_FILENAME = "postgres-password.key"

BACKEND_DIR = Path(__file__).resolve().parents[2]
KEYS_DIR = BACKEND_DIR / "config" / "keys"
_ACCEPTED_SCHEMES = {"postgres", "postgresql"}


def store_mode(env: Optional[Mapping[str, str]] = None) -> str:
    """Return ``postgres`` or ``file``; reject an unknown value loudly."""
    env = os.environ if env is None else env
    raw = (env.get(MODE_ENV) or "").strip().lower()
    if raw in {"", MODE_FILE}:
        return MODE_POSTGRES if (env.get(DSN_ENV) or "").strip() else MODE_FILE
    if raw in {MODE_POSTGRES, "postgresql"}:
        return MODE_POSTGRES
    raise ControlStoreError(
        f"Unknown {MODE_ENV} value {raw!r} (expected 'postgres' or 'file')"
    )


def password_path(env: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    override = (env.get("SYSADMIN_POSTGRES_PASSWORD_FILE") or "").strip()
    return Path(override) if override else KEYS_DIR / PASSWORD_FILENAME


def read_password(path: Optional[Path] = None) -> str:
    """Read the cluster password; raise ``ControlStoreUnavailable`` when absent.

    The value is returned to the caller only so it can be placed in a DSN. It
    is never logged, audited or included in an error message.
    """
    target = path or password_path()
    try:
        password = target.read_text().strip()
    except OSError as exc:
        raise ControlStoreUnavailable(
            f"PostgreSQL password file {target} is missing or unreadable; "
            "run backend/config/postgres/postgres.sh provision"
        ) from exc
    if not password:
        raise ControlStoreUnavailable(
            f"PostgreSQL password file {target} is empty; "
            "run backend/config/postgres/postgres.sh provision"
        )
    return password


def resolve_dsn(
    env: Optional[Mapping[str, str]] = None,
    password_file: Optional[Path] = None,
) -> Optional[str]:
    """Return a libpq DSN for the durable store, or None when it is not selected."""
    env = os.environ if env is None else env
    explicit = (env.get(DSN_ENV) or "").strip()
    if explicit:
        scheme = urlsplit(explicit).scheme.lower()
        if scheme not in _ACCEPTED_SCHEMES:
            raise ControlStoreError(
                f"{DSN_ENV} must use a PostgreSQL scheme, got {scheme or 'none'!r}"
            )
        return explicit
    if store_mode(env) != MODE_POSTGRES:
        return None

    host = (env.get("SYSADMIN_POSTGRES_HOST") or DEFAULT_HOST).strip()
    port = (env.get("SYSADMIN_POSTGRES_PORT") or str(DEFAULT_PORT)).strip()
    database = (env.get("SYSADMIN_POSTGRES_DB") or DEFAULT_DB).strip()
    user = (env.get("SYSADMIN_POSTGRES_USER") or DEFAULT_USER).strip()
    if not port.isdigit():
        raise ControlStoreError(f"SYSADMIN_POSTGRES_PORT must be numeric, got {port!r}")
    password = read_password(password_file)
    return (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{quote(database, safe='')}"
    )


def redact(dsn: Optional[str]) -> str:
    """Return a log-safe rendering of a DSN with the password removed."""
    if not dsn:
        return "<unconfigured>"
    parts = urlsplit(dsn)
    if parts.password is None:
        return dsn
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    user = quote(parts.username or "", safe="")
    return parts._replace(netloc=f"{user}:***@{host}{port}").geturl()
