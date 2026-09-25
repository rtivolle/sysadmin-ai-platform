#!/usr/bin/env python3
"""Durable control store: the platform's PostgreSQL leg.

Two things live here, both selected explicitly by ``SYSADMIN_CONTROL_STORE=postgres``
(or an explicit ``SYSADMIN_DATABASE_URL``):

* :class:`KeyStore` - the authoritative, audited bearer-key lifecycle that
  ADR-0001's corrective work asked for (issue / rotate / revoke), storing only
  token hashes;
* :class:`DurableTokenLedger` - the durable daily token record that reconciles
  a restarted process instead of handing back a fresh budget.

When the mode is ``file`` (the default, and every existing deployment) nothing
here is consulted and the prototype's behaviour is unchanged. That is a mode
switch, not a fallback: with the mode set to `postgres`, an unreachable store
raises :class:`ControlStoreUnavailable` and callers fail closed with HTTP 503.
"""
import os
from typing import Any, Dict, Mapping, Optional

from services.control_store.connection import check_ready
from services.control_store.dsn import (
    MODE_ENV,
    MODE_FILE,
    MODE_POSTGRES,
    password_path,
    redact,
    resolve_dsn,
    store_mode,
)
from services.control_store.errors import (
    ControlStoreError,
    ControlStoreIntegrityError,
    ControlStoreUnavailable,
)
from services.control_store.executor import Executor, SingleConnectionExecutor, open_executor
from services.control_store.key_store import KeyStore, generate_token, hash_token
from services.control_store.ledger import DurableTokenLedger
from services.control_store.schema import SCHEMA_VERSION, apply_schema

__all__ = [
    "MODE_ENV",
    "MODE_FILE",
    "MODE_POSTGRES",
    "SCHEMA_VERSION",
    "ControlStoreError",
    "ControlStoreIntegrityError",
    "ControlStoreUnavailable",
    "DurableTokenLedger",
    "Executor",
    "KeyStore",
    "SingleConnectionExecutor",
    "apply_schema",
    "check_ready",
    "generate_token",
    "hash_token",
    "health",
    "open_executor",
    "open_key_store",
    "open_token_ledger",
    "password_path",
    "redact",
    "resolve_dsn",
    "store_mode",
]


def _executor_for(
    env: Optional[Mapping[str, str]], executor: Optional[Executor]
) -> Optional[Executor]:
    if executor is not None:
        return executor
    dsn = resolve_dsn(env)
    if not dsn:
        return None
    return open_executor(dsn)


def open_key_store(
    env: Optional[Mapping[str, str]] = None,
    executor: Optional[Executor] = None,
) -> Optional[KeyStore]:
    """Return the durable key store, or None when file mode is configured."""
    handle = _executor_for(env, executor)
    return KeyStore(handle) if handle is not None else None


def open_token_ledger(
    env: Optional[Mapping[str, str]] = None,
    executor: Optional[Executor] = None,
) -> Optional[DurableTokenLedger]:
    """Return the durable token ledger, or None when file mode is configured."""
    handle = _executor_for(env, executor)
    return DurableTokenLedger(handle) if handle is not None else None


def health(env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Report store mode and reachability for status surfaces; never a secret."""
    env = os.environ if env is None else env
    try:
        mode = store_mode(env)
    except ControlStoreError as exc:
        return {"mode": "invalid", "reachable": False, "detail": str(exc)}
    if mode == MODE_FILE:
        return {"mode": MODE_FILE, "reachable": None, "dsn": "<unconfigured>"}
    try:
        dsn = resolve_dsn(env)
    except (ControlStoreError, ControlStoreUnavailable) as exc:
        return {"mode": mode, "reachable": False, "detail": str(exc)}
    return {
        "mode": mode,
        "reachable": check_ready(dsn) if dsn else False,
        "dsn": redact(dsn),
    }
