#!/usr/bin/env python3
"""Daily token reservation & settlement lifecycle (R1/R4 quota accounting).

Admission reserves capacity atomically before inference is dispatched, and the
completion replaces that estimate with actual usage exactly once. These tests
cover the process-local ledger and, when a live Valkey is reachable, the shared
ledger that the deployed services use.
"""
import sys
import uuid
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.auth_gateway.quota_manager import QuotaManager, QuotaExceededException

STANDARD_LIMIT = 2_000_000
KEYS_DIR = BACKEND_DIR / "config" / "keys"


def _local_manager(monkeypatch) -> QuotaManager:
    """A manager with no shared store configured (process-local ledger)."""
    monkeypatch.delenv("VALKEY_URL", raising=False)
    manager = QuotaManager()
    assert manager.require_shared is False
    return manager


def _live_manager() -> QuotaManager:
    password_file = KEYS_DIR / "valkey-password.key"
    if not password_file.exists():
        pytest.skip("Valkey credentials are not provisioned on this host")
    manager = QuotaManager(valkey_url=f"redis://:{password_file.read_text().strip()}@127.0.0.1:6379/0")
    if manager.redis is None:
        pytest.skip("Live Valkey is unavailable on this host")
    return manager


def test_local_reservation_admits_until_budget_exhausted(monkeypatch):
    qm = _local_manager(monkeypatch)
    user = f"reserve-limit-{uuid.uuid4().hex[:8]}"

    day, reserved, limit = qm.reserve_daily_token_budget(user, "res-full", STANDARD_LIMIT)
    assert (reserved, limit) == (STANDARD_LIMIT, STANDARD_LIMIT)

    with pytest.raises(QuotaExceededException) as exc:
        qm.reserve_daily_token_budget(user, "res-over", 1)
    assert exc.value.limit_type == "daily_tokens"
    assert exc.value.current == STANDARD_LIMIT
    assert day == qm._quota_day()


def test_local_duplicate_reservation_id_rejected(monkeypatch):
    qm = _local_manager(monkeypatch)
    user = f"reserve-dup-{uuid.uuid4().hex[:8]}"
    qm.reserve_daily_token_budget(user, "res-1", 100)
    with pytest.raises(ValueError):
        qm.reserve_daily_token_budget(user, "res-1", 100)


def test_local_settlement_replaces_estimate_and_is_idempotent(monkeypatch):
    qm = _local_manager(monkeypatch)
    user = f"reserve-settle-{uuid.uuid4().hex[:8]}"
    day, _, _ = qm.reserve_daily_token_budget(user, "res-2", 2_000_000)
    assert getattr(qm, "_local_daily_actual", {}).get((user, day), 0) == 0

    assert qm.settle_daily_token_reservation(user, "res-2", day, 300, 200) == 500
    # The estimate is gone: actual usage is what remains on the ledger.
    assert qm._local_daily_actual[(user, day)] == 500
    assert (user, day, "res-2") not in qm._local_daily_reservations

    # A repeated callback for the same completion must not double count.
    assert qm.settle_daily_token_reservation(user, "res-2", day, 300, 200) == 500
    assert qm._local_daily_actual[(user, day)] == 500


def test_local_settlement_frees_reserved_capacity(monkeypatch):
    qm = _local_manager(monkeypatch)
    user = f"reserve-release-{uuid.uuid4().hex[:8]}"
    day, _, _ = qm.reserve_daily_token_budget(user, "res-3", STANDARD_LIMIT)
    with pytest.raises(QuotaExceededException):
        qm.reserve_daily_token_budget(user, "res-4", 1)

    qm.settle_daily_token_reservation(user, "res-3", day, 100, 0)
    # Only the 100 actually consumed tokens still occupy the budget.
    _, reserved, _ = qm.reserve_daily_token_budget(user, "res-5", STANDARD_LIMIT - 100)
    assert reserved == STANDARD_LIMIT

    with pytest.raises(QuotaExceededException):
        qm.reserve_daily_token_budget(user, "res-6", 1)


def test_local_settlement_without_reservation_records_actual(monkeypatch):
    qm = _local_manager(monkeypatch)
    user = f"reserve-missing-{uuid.uuid4().hex[:8]}"
    day = qm._quota_day()

    # A completion admitted before reservations were deployed (or a lost
    # reservation) must still be billed rather than silently dropped.
    assert qm.settle_daily_token_reservation(user, "res-unknown", day, 120, 30) == 150
    assert qm._local_daily_actual[(user, day)] == 150


def test_required_shared_store_outage_fails_closed(monkeypatch):
    monkeypatch.delenv("VALKEY_URL", raising=False)
    qm = QuotaManager(valkey_url="redis://:unused@127.0.0.1:9/0")
    assert qm.require_shared is True
    assert qm.redis is None

    with pytest.raises(ConnectionError):
        qm.reserve_daily_token_budget("sysadmin-01", "res-outage", 100)
    with pytest.raises(ConnectionError):
        qm.settle_daily_token_reservation("sysadmin-01", "res-outage", qm._quota_day(), 10, 10)


def test_live_reservation_settlement_roundtrip():
    qm = _live_manager()
    user = f"live-reserve-{uuid.uuid4().hex[:8]}"
    day, _, limit = qm.reserve_daily_token_budget(user, "live-res-1", 1000)
    assert limit == STANDARD_LIMIT

    daily_key = f"daily_tokens:{user}:{day}"
    active_key = f"daily_reservations:active:{user}:{day}"
    settled_key = f"daily_reservations:settled:{user}:{day}"
    try:
        assert qm.redis.hget(active_key, "live-res-1") == "1000"
        assert int(qm.redis.get(daily_key) or 0) == 0

        assert qm.settle_daily_token_reservation(user, "live-res-1", day, 120, 80) == 200
        assert int(qm.redis.get(daily_key) or 0) == 200
        assert qm.redis.hget(active_key, "live-res-1") is None
        assert qm.redis.hget(settled_key, "live-res-1") == "200"

        # Repeated settlement callbacks stay exactly-once.
        assert qm.settle_daily_token_reservation(user, "live-res-1", day, 120, 80) == 200
        assert int(qm.redis.get(daily_key) or 0) == 200
    finally:
        qm.redis.delete(daily_key, active_key, settled_key,
                        f"daily_reservations:index:{user}:{day}", f"rate:tpm:{user}")


def test_live_reservation_is_atomic_across_managers():
    qm_a = _live_manager()
    qm_b = _live_manager()
    user = f"live-atomic-{uuid.uuid4().hex[:8]}"
    day = qm_a._quota_day()
    daily_key = f"daily_tokens:{user}:{day}"
    active_key = f"daily_reservations:active:{user}:{day}"
    settled_key = f"daily_reservations:settled:{user}:{day}"
    try:
        qm_a.reserve_daily_token_budget(user, "atomic-1", STANDARD_LIMIT)
        # A second worker sharing the store cannot oversubscribe the day.
        with pytest.raises(QuotaExceededException):
            qm_b.reserve_daily_token_budget(user, "atomic-2", 1)
        # A duplicate reservation ID from another worker is rejected too.
        with pytest.raises(ValueError):
            qm_b.reserve_daily_token_budget(user, "atomic-1", 1)
    finally:
        qm_a.redis.delete(daily_key, active_key, settled_key,
                          f"daily_reservations:index:{user}:{day}", f"rate:tpm:{user}")
