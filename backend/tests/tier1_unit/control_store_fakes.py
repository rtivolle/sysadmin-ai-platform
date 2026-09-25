"""Executor doubles for the durable control store.

The PostgreSQL leg must be testable on a host with no database: these doubles
implement the narrow ``Executor`` protocol (query / execute / transaction) and
emulate the two platform tables in memory, so the tests assert both the emitted
SQL and the resulting behaviour.
"""
import contextlib


class KeyTableExecutor:
    """In-memory ``sysadmin_api_keys``."""

    def __init__(self):
        self.rows = {}  # token_sha256 -> dict
        self.statements = []
        self.fail_with = None

    # -- Executor protocol -------------------------------------------------
    def query(self, sql, params=()):
        self._maybe_fail()
        self.statements.append((sql, tuple(params)))
        if "revoked_at IS NULL AND (%s IS NULL OR user_id = %s)" in sql:
            wanted = params[0]
            return [
                (row["user_id"], row["label"], row["created_at"], row["created_by"])
                for row in self.rows.values()
                if row["revoked_at"] is None and (wanted is None or row["user_id"] == wanted)
            ]
        if "WHERE user_id = %s ORDER BY created_at" in sql:
            return [
                (row["user_id"], row["label"], row["created_at"], row["created_by"], row["revoked_at"])
                for row in self.rows.values()
                if row["user_id"] == params[0]
            ]
        if "revoked_at IS NULL LIMIT 1" in sql:
            row = self.rows.get(params[0])
            if row and row["revoked_at"] is None:
                return [(row["user_id"],)]
            return []
        raise AssertionError(f"unexpected query: {sql}")

    def execute(self, sql, params=()):
        self._maybe_fail()
        self.statements.append((sql, tuple(params)))
        if "INSERT INTO sysadmin_api_keys" in sql:
            token_sha256, user_id, label, created_by = params
            if token_sha256 in self.rows:
                return 0
            self.rows[token_sha256] = {
                "user_id": user_id,
                "label": label,
                "created_by": created_by,
                "created_at": "2026-09-24T00:00:00Z",
                "revoked_at": None,
                "rotated_to": None,
            }
            return 1
        if "token_sha256 <> %s" in sql:  # rotate: revoke every live key but the new one
            actor, rotated_to, user_id, exclude = params
            return self._revoke(actor, user_id, rotated_to=rotated_to, exclude=exclude)
        if "token_sha256 = %s AND revoked_at IS NULL" in sql:  # revoke exactly one
            actor, user_id, token_sha256 = params
            return self._revoke(actor, user_id, only=token_sha256)
        if "rotated_to = NULL WHERE user_id = %s" in sql:  # revoke every live key
            actor, user_id = params
            return self._revoke(actor, user_id)
        raise AssertionError(f"unexpected statement: {sql}")

    def _revoke(self, actor, user_id, rotated_to=None, exclude=None, only=None):
        affected = 0
        for token_sha256, row in self.rows.items():
            if row["user_id"] != user_id or row["revoked_at"] is not None:
                continue
            if exclude is not None and token_sha256 == exclude:
                continue
            if only is not None and token_sha256 != only:
                continue
            row["revoked_at"] = "2026-09-24T01:00:00Z"
            row["revoked_by"] = actor
            row["rotated_to"] = rotated_to
            affected += 1
        return affected

    @contextlib.contextmanager
    def transaction(self):
        yield self

    # -- helpers -----------------------------------------------------------
    def live_tokens_for(self, user_id):
        return [h for h, row in self.rows.items() if row["user_id"] == user_id and row["revoked_at"] is None]

    def _maybe_fail(self):
        if self.fail_with is not None:
            raise self.fail_with


class LedgerTableExecutor:
    """In-memory ``sysadmin_token_ledger``."""

    def __init__(self):
        self.rows = {}  # reservation_id -> dict
        self.statements = []
        self.fail_with = None

    def query(self, sql, params=()):
        self._maybe_fail()
        self.statements.append((sql, tuple(params)))
        if "SUM(COALESCE(settled_tokens, estimated_tokens)" in sql:
            user_id, day = params
            return [(sum(
                row["settled_tokens"] if row["settled_tokens"] is not None else row["estimated_tokens"]
                for row in self.rows.values()
                if row["user_id"] == user_id and str(row["admission_day"]) == str(day)
            ),)]
        if "SELECT COUNT(*) FROM sysadmin_token_ledger" in sql:
            user_id, day = params
            return [(len([
                row for row in self.rows.values()
                if row["user_id"] == user_id and str(row["admission_day"]) == str(day)
                and row["settled_tokens"] is None
            ]),)]
        if "SELECT 1 FROM sysadmin_token_ledger" in sql:
            return [(1,)] if params[0] in self.rows else []
        raise AssertionError(f"unexpected query: {sql}")

    def execute(self, sql, params=()):
        self._maybe_fail()
        self.statements.append((sql, tuple(params)))
        if "INSERT INTO sysadmin_token_ledger" in sql and "settled_tokens" not in sql:
            reservation_id, user_id, day, estimated, expires_at = params
            if reservation_id in self.rows:
                return 0
            self.rows[reservation_id] = {
                "user_id": user_id,
                "admission_day": str(day),
                "estimated_tokens": estimated,
                "settled_tokens": None,
                "expires_at": expires_at,
            }
            return 1
        if "INSERT INTO sysadmin_token_ledger" in sql and "settled_tokens" in sql:
            reservation_id, user_id, day, estimated, settled = params
            if reservation_id in self.rows:
                return 0
            self.rows[reservation_id] = {
                "user_id": user_id,
                "admission_day": str(day),
                "estimated_tokens": estimated,
                "settled_tokens": settled,
                "expires_at": "already-settled",
            }
            return 1
        if sql.startswith("UPDATE sysadmin_token_ledger SET settled_tokens = %s"):
            actual, reservation_id, user_id, day = params
            row = self.rows.get(reservation_id)
            if (row and row["user_id"] == user_id and str(row["admission_day"]) == str(day)
                    and row["settled_tokens"] is None):
                row["settled_tokens"] = actual
                return 1
            return 0
        if sql.startswith("UPDATE sysadmin_token_ledger SET settled_tokens = estimated_tokens"):
            user_id, day, cutoff = params
            affected = 0
            for row in self.rows.values():
                if (row["user_id"] == user_id and str(row["admission_day"]) == str(day)
                        and row["settled_tokens"] is None and row["expires_at"] <= cutoff):
                    row["settled_tokens"] = row["estimated_tokens"]
                    affected += 1
            return affected
        if sql.startswith("DELETE FROM sysadmin_token_ledger"):
            (cutoff,) = params
            doomed = [rid for rid, row in self.rows.items() if str(row["admission_day"]) < str(cutoff)]
            for rid in doomed:
                del self.rows[rid]
            return len(doomed)
        if sql.startswith("CREATE"):
            return 0
        raise AssertionError(f"unexpected statement: {sql}")

    @contextlib.contextmanager
    def transaction(self):
        yield self

    def _maybe_fail(self):
        if self.fail_with is not None:
            raise self.fail_with
