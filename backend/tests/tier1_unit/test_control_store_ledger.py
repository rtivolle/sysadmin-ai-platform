"""Durable daily token ledger: exactly-once settlement and conservative totals."""
import datetime

import pytest

from control_store_fakes import LedgerTableExecutor
from services.control_store.ledger import DurableTokenLedger

DAY = "2026-09-24"
# A clock inside the fixture day, so retention/expiry comparisons are
# deterministic instead of depending on when the suite runs.
NOW = datetime.datetime(2026, 9, 24, 12, tzinfo=datetime.timezone.utc).timestamp()


@pytest.fixture
def ledger():
    return DurableTokenLedger(LedgerTableExecutor(), clock=lambda: NOW)


def test_reservation_is_persisted_once(ledger):
    assert ledger.record_reservation("sysadmin-01", DAY, "res-1", 500, ttl_seconds=60) is True
    assert ledger.record_reservation("sysadmin-01", DAY, "res-1", 500, ttl_seconds=60) is False
    assert ledger.pending_reservations("sysadmin-01", DAY) == 1


def test_negative_estimate_is_rejected(ledger):
    with pytest.raises(ValueError):
        ledger.record_reservation("sysadmin-01", DAY, "res-1", -1, ttl_seconds=60)


def test_unsettled_reservation_is_charged_at_its_estimate(ledger):
    ledger.record_reservation("sysadmin-01", DAY, "res-1", 700, ttl_seconds=60)
    assert ledger.durable_total("sysadmin-01", DAY) == 700


def test_settlement_replaces_the_estimate_exactly_once(ledger):
    ledger.record_reservation("sysadmin-01", DAY, "res-1", 700, ttl_seconds=60)
    assert ledger.settle("sysadmin-01", DAY, "res-1", 120) is True
    assert ledger.settle("sysadmin-01", DAY, "res-1", 9999) is False
    assert ledger.durable_total("sysadmin-01", DAY) == 120


def test_completion_without_a_reservation_is_recorded_as_settled(ledger):
    assert ledger.settle("sysadmin-02", DAY, "unknown-res", 42) is True
    assert ledger.durable_total("sysadmin-02", DAY) == 42
    assert ledger.settle("sysadmin-02", DAY, "unknown-res", 42) is False
    assert ledger.durable_total("sysadmin-02", DAY) == 42


def test_direct_usage_is_durable_and_zero_is_not_recorded(ledger):
    assert ledger.record_direct_usage("sysadmin-03", DAY, 300) is True
    assert ledger.record_direct_usage("sysadmin-03", DAY, 0) is False
    assert ledger.durable_total("sysadmin-03", DAY) == 300


def test_totals_are_scoped_to_user_and_day(ledger):
    ledger.record_reservation("sysadmin-04", DAY, "res-a", 100, ttl_seconds=60)
    ledger.record_reservation("sysadmin-04", "2026-09-25", "res-b", 900, ttl_seconds=60)
    ledger.record_reservation("sysadmin-05", DAY, "res-c", 5000, ttl_seconds=60)
    assert ledger.durable_total("sysadmin-04", DAY) == 100
    assert ledger.durable_total("sysadmin-04", "2026-09-25") == 900
    assert ledger.durable_total("sysadmin-05", DAY) == 5000


def test_expired_reservations_are_made_permanent_at_their_estimate(ledger):
    ledger.record_reservation("sysadmin-06", DAY, "res-old", 250, ttl_seconds=1)
    executor = ledger._executor
    executor.rows["res-old"]["expires_at"] = datetime.datetime.fromtimestamp(NOW - 1000, tz=datetime.timezone.utc)
    assert ledger.settle_expired("sysadmin-06", DAY) == 1
    assert ledger.pending_reservations("sysadmin-06", DAY) == 0
    assert ledger.durable_total("sysadmin-06", DAY) == 250


def test_unexpired_reservations_are_not_force_settled(ledger):
    ledger.record_reservation("sysadmin-07", DAY, "res-live", 250, ttl_seconds=3600)
    assert ledger.settle_expired("sysadmin-07", DAY) == 0
    assert ledger.pending_reservations("sysadmin-07", DAY) == 1


def test_prune_drops_days_past_the_retention_window(ledger):
    ledger.record_reservation("sysadmin-08", "2020-01-01", "res-ancient", 10, ttl_seconds=60)
    ledger.record_reservation("sysadmin-08", DAY, "res-recent", 10, ttl_seconds=60)
    assert ledger.prune(retention_days=30) == 1
    assert ledger.durable_total("sysadmin-08", DAY) == 10


def test_iso_day_strings_and_date_objects_are_equivalent(ledger):
    ledger.record_reservation("sysadmin-09", datetime.date(2026, 9, 24), "res-1", 60, ttl_seconds=60)
    assert ledger.durable_total("sysadmin-09", DAY) == 60
