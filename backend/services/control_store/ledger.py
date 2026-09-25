#!/usr/bin/env python3
"""The durable daily token ledger.

Valkey keeps the atomic counters and leases (it is good at that); PostgreSQL
keeps the record that must survive a restart, a lost AOF or a flushed
keyspace. Two contracts from DEVELOPMENT_PLAN §4 are implemented here:

* **never silently count zero** - a reservation that was never settled is
  charged at its estimate, and :meth:`settle_expired` makes that charge
  permanent instead of leaking capacity;
* **reconcile after restart** - :meth:`durable_total` is the floor a restarted
  process raises its in-memory/Valkey counter to, so a lost counter cannot hand
  a user a second full budget.
"""
import datetime
import time
import uuid
from typing import Any, Optional, Union

_INSERT_RESERVATION = (
    "INSERT INTO sysadmin_token_ledger "
    "(reservation_id, user_id, admission_day, estimated_tokens, expires_at) "
    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (reservation_id) DO NOTHING"
)
_SETTLE = (
    "UPDATE sysadmin_token_ledger SET settled_tokens = %s, settled_at = now() "
    "WHERE reservation_id = %s AND user_id = %s AND admission_day = %s "
    "AND settled_tokens IS NULL"
)
_EXISTS = "SELECT 1 FROM sysadmin_token_ledger WHERE reservation_id = %s LIMIT 1"
_INSERT_SETTLED = (
    "INSERT INTO sysadmin_token_ledger "
    "(reservation_id, user_id, admission_day, estimated_tokens, settled_tokens, "
    " settled_at, expires_at) VALUES (%s, %s, %s, %s, %s, now(), now()) "
    "ON CONFLICT (reservation_id) DO NOTHING"
)
# Conservative total: a settled row counts what it actually used; an unsettled
# row still counts its estimate, because the platform must not assume a
# dispatched-but-unreported request was free.
_DURABLE_TOTAL = (
    "SELECT COALESCE(SUM(COALESCE(settled_tokens, estimated_tokens)), 0) "
    "FROM sysadmin_token_ledger WHERE user_id = %s AND admission_day = %s"
)
_PENDING = (
    "SELECT COUNT(*) FROM sysadmin_token_ledger "
    "WHERE user_id = %s AND admission_day = %s AND settled_tokens IS NULL"
)
_SETTLE_EXPIRED = (
    "UPDATE sysadmin_token_ledger SET settled_tokens = estimated_tokens, settled_at = now() "
    "WHERE user_id = %s AND admission_day = %s AND settled_tokens IS NULL AND expires_at <= %s"
)
_PRUNE = "DELETE FROM sysadmin_token_ledger WHERE admission_day < %s"


def _as_date(day: Union[str, datetime.date]) -> datetime.date:
    if isinstance(day, datetime.date):
        return day
    return datetime.date.fromisoformat(str(day))


class DurableTokenLedger:
    """Append-only reservation/settlement ledger with conservative totals."""

    def __init__(self, executor, clock=None):
        self._executor = executor
        self._clock = clock or time.time

    def record_reservation(
        self,
        user_id: str,
        admission_day: Union[str, datetime.date],
        reservation_id: str,
        estimated_tokens: int,
        ttl_seconds: int,
    ) -> bool:
        """Persist an admitted reservation; idempotent on reservation_id."""
        if estimated_tokens < 0:
            raise ValueError("estimated_tokens must be non-negative")
        expires_at = datetime.datetime.fromtimestamp(
            self._clock() + max(1, int(ttl_seconds)), tz=datetime.timezone.utc
        )
        affected = self._executor.execute(
            _INSERT_RESERVATION,
            (
                reservation_id,
                user_id,
                _as_date(admission_day),
                int(estimated_tokens),
                expires_at,
            ),
        )
        return int(affected) > 0

    def settle(
        self,
        user_id: str,
        admission_day: Union[str, datetime.date],
        reservation_id: str,
        actual_tokens: int,
    ) -> bool:
        """Replace an estimate with actual usage exactly once.

        Returns True when this call performed the settlement, False when the
        reservation was already settled (a duplicate completion event must not
        double-charge) - or when it was unknown and had to be recorded as an
        already-settled row, which is the case for a completion admitted before
        the ledger existed.
        """
        actual = max(0, int(actual_tokens))
        day = _as_date(admission_day)
        affected = self._executor.execute(
            _SETTLE, (actual, reservation_id, user_id, day)
        )
        if int(affected) > 0:
            return True
        known = self._executor.query(_EXISTS, (reservation_id,))
        if known:
            return False
        inserted = self._executor.execute(
            _INSERT_SETTLED,
            (reservation_id, user_id, day, actual, actual),
        )
        return int(inserted) > 0

    def record_direct_usage(
        self,
        user_id: str,
        admission_day: Union[str, datetime.date],
        total_tokens: int,
        reference: Optional[str] = None,
    ) -> bool:
        """Record usage reported without a reservation (compatibility path).

        Stored as an already-settled row, so it contributes to the durable total
        and is never charged twice: a completion admitted before the ledger
        existed, or reported by a non-standard LiteLLM callback, must still leave
        a durable trace instead of vanishing on restart.
        """
        total = max(0, int(total_tokens))
        if total == 0:
            return False
        reservation_id = reference or f"direct:{user_id}:{_as_date(admission_day).isoformat()}:{uuid.uuid4().hex}"
        inserted = self._executor.execute(
            _INSERT_SETTLED,
            (reservation_id, user_id, _as_date(admission_day), total, total),
        )
        return int(inserted) > 0

    def durable_total(
        self, user_id: str, admission_day: Union[str, datetime.date]
    ) -> int:
        """The conservative day total the runtime counter must be raised to."""
        rows = self._executor.query(_DURABLE_TOTAL, (user_id, _as_date(admission_day)))
        if not rows:
            return 0
        return max(0, int(rows[0][0] or 0))

    def pending_reservations(
        self, user_id: str, admission_day: Union[str, datetime.date]
    ) -> int:
        """How many admitted requests for the day are still unsettled."""
        rows = self._executor.query(_PENDING, (user_id, _as_date(admission_day)))
        if not rows:
            return 0
        return max(0, int(rows[0][0] or 0))

    def settle_expired(
        self, user_id: str, admission_day: Union[str, datetime.date]
    ) -> int:
        """Charge expired, unreported reservations at their estimate."""
        cutoff = datetime.datetime.fromtimestamp(
            self._clock(), tz=datetime.timezone.utc
        )
        affected = self._executor.execute(
            _SETTLE_EXPIRED, (user_id, _as_date(admission_day), cutoff)
        )
        return max(0, int(affected))

    def prune(self, retention_days: int = 30) -> int:
        """Drop ledger rows older than the retention window."""
        cutoff = (
            datetime.datetime.fromtimestamp(self._clock(), tz=datetime.timezone.utc).date()
            - datetime.timedelta(days=max(1, int(retention_days)))
        )
        affected = self._executor.execute(_PRUNE, (cutoff,))
        return max(0, int(affected))
