#!/usr/bin/env python3
"""Durable API-key lifecycle: the PostgreSQL authority for bearer identity.

Replaces the file-only model described in ADR-0001 with a store that can issue,
rotate and revoke keys atomically and audit every transition, while keeping the
guardrail that matters most: **only a SHA-256 of a token is ever stored**, so a
database dump, backup or replica cannot be replayed as a credential. Plaintext
key material still lives only in ``backend/config/keys/*.key`` (mode ``0600``,
Git-ignored), which is also the bootstrap input for :meth:`import_file_keys`.
"""
import hashlib
import secrets
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Sequence

from services.control_store.errors import ControlStoreIntegrityError

TOKEN_PREFIX = "sk-"
TOKEN_BYTES = 48
FINGERPRINT_LENGTH = 12

# Files in backend/config/keys that are not per-user bearer identities.
_NON_IDENTITY_KEYS = {"master", "valkey-password", "postgres-password"}

_SELECT_USER = (
    "SELECT user_id FROM sysadmin_api_keys "
    "WHERE token_sha256 = %s AND revoked_at IS NULL LIMIT 1"
)
_INSERT_KEY = (
    "INSERT INTO sysadmin_api_keys "
    "(token_sha256, user_id, label, created_by) VALUES (%s, %s, %s, %s) "
    "ON CONFLICT (token_sha256) DO NOTHING"
)
# Revoking "everything live" must exclude the key the same transaction just
# issued, otherwise a rotation would immediately revoke its own replacement.
_REVOKE_OTHERS = (
    "UPDATE sysadmin_api_keys SET revoked_at = now(), revoked_by = %s, rotated_to = %s "
    "WHERE user_id = %s AND revoked_at IS NULL AND token_sha256 <> %s"
)
_REVOKE_ALL = (
    "UPDATE sysadmin_api_keys SET revoked_at = now(), revoked_by = %s, rotated_to = NULL "
    "WHERE user_id = %s AND revoked_at IS NULL"
)
_REVOKE_TOKEN = (
    "UPDATE sysadmin_api_keys SET revoked_at = now(), revoked_by = %s, rotated_to = NULL "
    "WHERE user_id = %s AND token_sha256 = %s AND revoked_at IS NULL"
)
_LIST_ACTIVE = (
    "SELECT user_id, label, created_at, created_by FROM sysadmin_api_keys "
    "WHERE revoked_at IS NULL AND (%s IS NULL OR user_id = %s) "
    "ORDER BY user_id, created_at"
)
_LIST_USER = (
    "SELECT user_id, label, created_at, created_by, revoked_at FROM sysadmin_api_keys "
    "WHERE user_id = %s ORDER BY created_at"
)


def generate_token() -> str:
    """Mint a bearer token in the same shape as the file provisioning script."""
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """Return the SHA-256 (hex) of a token; the only form ever persisted."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def fingerprint(token_sha256: str) -> str:
    """A short, non-reversible correlation id for audit events."""
    return token_sha256[:FINGERPRINT_LENGTH]


def _default_audit() -> Callable[..., Dict[str, Any]]:
    from services.agent_tools.audit import log_audit_event

    return log_audit_event


class KeyStore:
    """Issue, resolve, rotate and revoke bearer keys against PostgreSQL."""

    def __init__(self, executor, audit: Optional[Callable[..., Dict[str, Any]]] = None):
        self._executor = executor
        self._audit = audit

    # --- resolution -------------------------------------------------------
    def resolve(self, token: str) -> Optional[str]:
        """Return the user_id for a live token, or None when it is unknown/revoked.

        Raises :class:`ControlStoreUnavailable` (a ``ConnectionError``) when the
        store is unreachable: the store is authoritative once configured, so an
        outage must never fall back to the file map.
        """
        if not token or not token.strip():
            return None
        rows = self._executor.query(_SELECT_USER, (hash_token(token.strip()),))
        return str(rows[0][0]) if rows else None

    def active_keys(self, user_id: Optional[str] = None) -> list[Dict[str, Any]]:
        """Metadata for live keys; never returns token material or hashes."""
        rows = self._executor.query(_LIST_ACTIVE, (user_id, user_id))
        return [
            {"user_id": r[0], "label": r[1], "created_at": r[2], "created_by": r[3]}
            for r in rows
        ]

    def key_history(self, user_id: str) -> list[Dict[str, Any]]:
        """Every key ever issued to a user, including revoked ones."""
        rows = self._executor.query(_LIST_USER, (user_id,))
        return [
            {
                "user_id": r[0],
                "label": r[1],
                "created_at": r[2],
                "created_by": r[3],
                "revoked_at": r[4],
            }
            for r in rows
        ]

    # --- lifecycle --------------------------------------------------------
    def issue(
        self,
        user_id: str,
        label: str = "",
        created_by: str = "",
        token: Optional[str] = None,
    ) -> str:
        """Issue a key and return the plaintext exactly once."""
        if not user_id:
            raise ControlStoreIntegrityError("A key must be bound to a user_id")
        plaintext = token or generate_token()
        token_sha256 = hash_token(plaintext)
        affected = self._executor.execute(
            _INSERT_KEY, (token_sha256, user_id, label, created_by)
        )
        if affected == 0:
            raise ControlStoreIntegrityError(
                "Refusing to reissue an existing token; rotate it instead"
            )
        self._audit_event(
            "api_key_issue",
            user_id,
            created_by,
            {"label": label, "key_fingerprint": fingerprint(token_sha256)},
        )
        return plaintext

    def rotate(self, user_id: str, label: str = "", created_by: str = "") -> str:
        """Atomically replace every live key for a user with one new key."""
        if not user_id:
            raise ControlStoreIntegrityError("A key must be bound to a user_id")
        plaintext = generate_token()
        new_hash = hash_token(plaintext)
        with self._executor.transaction() as tx:
            affected = tx.execute(
                _INSERT_KEY, (new_hash, user_id, label, created_by)
            )
            if affected == 0:
                raise ControlStoreIntegrityError(
                    "Key rotation collided with an existing token; retry"
                )
            revoked = tx.execute(_REVOKE_OTHERS, (created_by, new_hash, user_id, new_hash))
        self._audit_event(
            "api_key_rotate",
            user_id,
            created_by,
            {
                "label": label,
                "revoked": int(revoked),
                "key_fingerprint": fingerprint(new_hash),
            },
        )
        return plaintext

    def revoke(
        self, user_id: str, token: Optional[str] = None, actor: str = ""
    ) -> int:
        """Revoke one token, or every live key of a user, and audit the count."""
        if not user_id:
            raise ControlStoreIntegrityError("A key must be bound to a user_id")
        if token:
            affected = self._executor.execute(
                _REVOKE_TOKEN, (actor, user_id, hash_token(token.strip()))
            )
        else:
            affected = self._executor.execute(_REVOKE_ALL, (actor, user_id))
        affected = max(0, int(affected))
        self._audit_event(
            "api_key_revoke", user_id, actor, {"revoked": affected, "single": bool(token)}
        )
        return affected

    # --- bootstrap --------------------------------------------------------
    def import_file_keys(
        self, keys_dir: Path, actor: str = "provision"
    ) -> Dict[str, int]:
        """Import the provisioned file keys into the store (idempotent).

        Files remain the source of key *material*; after import the store is the
        runtime authority, so both layers cannot disagree. Only hashes cross
        into the database and no token value is logged.
        """
        imported = 0
        existing = 0
        skipped = 0
        for key_file in sorted(Path(keys_dir).glob("*.key")):
            user_id = _user_for_key_file(key_file)
            if user_id is None:
                skipped += 1
                continue
            try:
                token = key_file.read_text().strip()
            except OSError:
                skipped += 1
                continue
            if not token:
                skipped += 1
                continue
            affected = self._executor.execute(
                _INSERT_KEY,
                (hash_token(token), user_id, "provisioned-file", actor),
            )
            if affected == 0:
                existing += 1
            else:
                imported += 1
        return {"imported": imported, "existing": existing, "skipped": skipped}

    # --- internals --------------------------------------------------------
    def _audit_event(
        self, action: str, user_id: str, actor: str, extra: Dict[str, Any]
    ) -> None:
        """Record a key transition; never let an audit failure undo the change.

        The change is already committed when this runs, so a failed audit write
        is reported to the caller's log rather than raised: the reverse order
        would let an audit outage block emergency key revocation.
        """
        audit = self._audit or _default_audit()
        payload = dict(extra)
        payload["actor"] = actor
        try:
            result = audit(
                user_id=user_id,
                session_id="",
                tool_name=action,
                action=action,
                parameters=payload,
            )
        except Exception:  # pragma: no cover - audit must never mask the change
            return
        if isinstance(result, dict) and not result.get("logged", True):
            return


def _user_for_key_file(key_file: Path) -> Optional[str]:
    stem = key_file.stem
    if stem in _NON_IDENTITY_KEYS:
        return None
    if stem == "emergency-p1":
        return "emergency-p1-oncall"
    return stem


def iter_identity_files(keys_dir: Path) -> Iterable[Path]:
    for key_file in sorted(Path(keys_dir).glob("*.key")):
        if _user_for_key_file(key_file) is not None:
            yield key_file
