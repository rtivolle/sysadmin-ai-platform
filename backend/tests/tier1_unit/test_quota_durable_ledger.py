"""Durable ledger integration in the quota manager (no live database needed)."""
import pytest

from services.auth_gateway.quota_manager import (
    QuotaManager,
    QuotaExceededException,
    _UnavailableLedger,
)


class FakeLedger:
    def __init__(self, total=0, failure=None):
        self.total = total
        self.failure = failure
        self.reservations = []
        self.settlements = []
        self.direct = []
        self.settle_expired_calls = 0
        self.total_calls = 0

    def _maybe_fail(self):
        if self.failure is not None:
            raise self.failure

    def record_reservation(self, user_id, day, reservation_id, estimated_tokens, ttl_seconds):
        self._maybe_fail()
        self.reservations.append((user_id, day, reservation_id, estimated_tokens, ttl_seconds))
        return True

    def settle(self, user_id, day, reservation_id, actual_tokens):
        self._maybe_fail()
        self.settlements.append((user_id, day, reservation_id, actual_tokens))
        return True

    def record_direct_usage(self, user_id, day, total_tokens):
        self._maybe_fail()
        self.direct.append((user_id, day, total_tokens))
        return True

    def settle_expired(self, user_id, day):
        self._maybe_fail()
        self.settle_expired_calls += 1
        return 0

    def durable_total(self, user_id, day):
        self._maybe_fail()
        self.total_calls += 1
        return self.total


@pytest.fixture
def local_manager(monkeypatch):
    monkeypatch.delenv("VALKEY_URL", raising=False)
    monkeypatch.delenv("SYSADMIN_CONTROL_STORE", raising=False)
    monkeypatch.delenv("SYSADMIN_DATABASE_URL", raising=False)
    return QuotaManager()


def test_file_mode_disables_every_durable_hook(local_manager):
    assert local_manager.durable_ledger is None
    day, reserved, limit = local_manager.reserve_daily_token_budget("u", "res-1", 100)
    assert reserved == 100
    assert local_manager.reconcile_daily_usage("u", day) == 0


def test_reservations_are_mirrored_into_the_durable_ledger(local_manager):
    ledger = FakeLedger()
    local_manager.durable_ledger = ledger
    day, _, _ = local_manager.reserve_daily_token_budget("sysadmin-01", "res-1", 250)
    assert ledger.reservations == [
        ("sysadmin-01", day, "res-1", 250, QuotaManager.RESERVATION_TTL_SECONDS)
    ]


def test_settlement_is_mirrored_exactly_once(local_manager):
    ledger = FakeLedger()
    local_manager.durable_ledger = ledger
    day, _, _ = local_manager.reserve_daily_token_budget("sysadmin-01", "res-1", 250)
    local_manager.settle_daily_token_reservation("sysadmin-01", "res-1", day, 30, 12)
    local_manager.settle_daily_token_reservation("sysadmin-01", "res-1", day, 30, 12)
    assert ledger.settlements == [("sysadmin-01", day, "res-1", 42)]


def test_direct_consumption_is_recorded_durably(local_manager):
    ledger = FakeLedger()
    local_manager.durable_ledger = ledger
    local_manager.record_token_consumption("sysadmin-01", 10, 5)
    assert ledger.direct == [("sysadmin-01", local_manager._quota_day(), 15)]


def test_reconciliation_raises_the_local_counter_to_the_durable_floor(local_manager):
    ledger = FakeLedger(total=1_500_000)
    local_manager.durable_ledger = ledger
    day = local_manager._quota_day()
    assert local_manager.reconcile_daily_usage("sysadmin-01", day) == 1_500_000
    assert ledger.settle_expired_calls == 1
    # The floor applies to admission: a fresh budget of 2M is no longer available.
    with pytest.raises(QuotaExceededException) as exc:
        local_manager.reserve_daily_token_budget("sysadmin-01", "res-big", 600_000, admission_day=day)
    assert exc.value.current == 1_500_000


def test_reconciliation_runs_once_per_day_per_process(local_manager):
    ledger = FakeLedger(total=10)
    local_manager.durable_ledger = ledger
    local_manager.reserve_daily_token_budget("sysadmin-01", "res-1", 1)
    local_manager.reserve_daily_token_budget("sysadmin-01", "res-2", 1)
    assert ledger.total_calls == 1


def test_store_failure_fails_the_admission_closed(local_manager):
    local_manager.durable_ledger = FakeLedger(failure=ConnectionError("store down"))
    with pytest.raises(ConnectionError):
        local_manager.reserve_daily_token_budget("sysadmin-01", "res-1", 10)


def test_non_connection_store_errors_are_normalized_to_connection_error(local_manager):
    local_manager.durable_ledger = FakeLedger(failure=RuntimeError("driver exploded"))
    with pytest.raises(ConnectionError):
        local_manager.reserve_daily_token_budget("sysadmin-01", "res-1", 10)


def test_selected_but_unusable_store_fails_every_operation(monkeypatch):
    monkeypatch.delenv("VALKEY_URL", raising=False)
    monkeypatch.setenv("SYSADMIN_CONTROL_STORE", "postgres")
    monkeypatch.setenv("SYSADMIN_POSTGRES_PASSWORD_FILE", "/nonexistent/postgres-password.key")
    manager = QuotaManager()
    assert isinstance(manager.durable_ledger, _UnavailableLedger)
    with pytest.raises(ConnectionError):
        manager.reserve_daily_token_budget("sysadmin-01", "res-1", 10)
