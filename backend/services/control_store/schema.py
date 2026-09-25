#!/usr/bin/env python3
"""Idempotent DDL for the platform-owned control-store tables.

LiteLLM owns and migrates its own tables (spend logs, virtual keys) inside the
same database; the platform never touches them. The tables below are the ones
the platform itself needs:

* ``sysadmin_api_keys`` - durable bearer-key lifecycle. Only a SHA-256 of the
  token is ever stored, so a database dump or backup cannot be replayed as a
  credential.
* ``sysadmin_token_ledger`` - the durable daily token ledger. An unsettled row
  is charged at its reservation estimate, which is the conservative direction
  required by DEVELOPMENT_PLAN §4 ("never silently count zero").
"""
from typing import Sequence

from services.control_store.connection import Connection

SCHEMA_VERSION = "1"

STATEMENTS: Sequence[str] = (
    """
    CREATE TABLE IF NOT EXISTS sysadmin_api_keys (
        token_sha256   TEXT PRIMARY KEY,
        user_id        TEXT NOT NULL,
        label          TEXT NOT NULL DEFAULT '',
        created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
        created_by     TEXT NOT NULL DEFAULT '',
        revoked_at     TIMESTAMPTZ,
        revoked_by     TEXT NOT NULL DEFAULT '',
        rotated_to     TEXT,
        CHECK (length(token_sha256) = 64)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS sysadmin_api_keys_active_user_idx
        ON sysadmin_api_keys (user_id)
        WHERE revoked_at IS NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS sysadmin_token_ledger (
        reservation_id   TEXT PRIMARY KEY,
        user_id          TEXT NOT NULL,
        admission_day    DATE NOT NULL,
        estimated_tokens BIGINT NOT NULL CHECK (estimated_tokens >= 0),
        settled_tokens   BIGINT CHECK (settled_tokens >= 0),
        created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        settled_at       TIMESTAMPTZ,
        expires_at       TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS sysadmin_token_ledger_day_idx
        ON sysadmin_token_ledger (user_id, admission_day)
    """,
    """
    CREATE TABLE IF NOT EXISTS sysadmin_schema_meta (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
)

VERSION_STATEMENT = (
    """
    INSERT INTO sysadmin_schema_meta (key, value, updated_at)
    VALUES ('schema_version', %s, now())
    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
    """,
    (SCHEMA_VERSION,),
)


def apply_schema(conn: Connection) -> None:
    """Create every platform table; safe to run on every start."""
    with conn.transaction():
        conn.execute_many(STATEMENTS)
        conn.execute(*VERSION_STATEMENT)
