"""QuotaManager team/project scope admission (additive extension tests).

These cover ONLY the new scoped methods. The existing per-user suite
(test_quota_fail_closed.py, test_daily_token_reservation.py) must keep
passing unchanged — no existing method was modified.
"""
import pytest

from services.auth_gateway.quota_manager import (
    QuotaExceededException,
    QuotaManager,
)
from services.control_store.quota_scopes import QuotaScopes
from test_quota_scopes import QuotaScopesTableExecutor


def _manager():
    # No Valkey contact at construction; the scoped paths never touch Valkey.
    return QuotaManager(valkey_url="redis://127.0.0.1:9/0", durable_ledger=None)


def _attached():
    manager = _manager()
    fake = QuotaScopesTableExecutor()
    scopes = QuotaScopes(fake)
    scopes.set_limits("team", "alpha", {"daily_tokens": 1000})
    manager.attach_quota_scopes(scopes)
    return manager, scopes


def test_no_attached_store_is_noop():
    manager = _manager()
    assert manager.check_scoped_token_budget("team", "alpha", 10 ** 9) is None
    assert manager.record_scoped_usage("team", "alpha", 10) is False


def test_scoped_admission_under_budget():
    manager, _ = _attached()
    info = manager.check_scoped_token_budget("team", "alpha", 100)
    assert info["limit"] == 1000
    assert info["used"] == 0


def test_scoped_admission_exhausted_raises_429_exception():
    manager, scopes = _attached()
    scopes.record_usage("team", "alpha", 1000)
    with pytest.raises(QuotaExceededException) as excinfo:
        manager.check_scoped_token_budget("team", "alpha", 1)
    assert excinfo.value.limit_type == "team_tokens"
    assert excinfo.value.current == 1000
    assert excinfo.value.limit == 1000


def test_scoped_usage_recorded():
    manager, scopes = _attached()
    assert manager.record_scoped_usage("team", "alpha", 250, model="llama-8b") is True
    assert scopes.usage_summary("team", "alpha")["tokens"] == 250


def test_scoped_store_outage_fails_closed():
    manager = _manager()
    fake = QuotaScopesTableExecutor()
    fake.fail_with = OSError("postgres down")
    manager.attach_quota_scopes(QuotaScopes(fake))
    with pytest.raises(ConnectionError):
        manager.check_scoped_token_budget("team", "alpha", 1)
    with pytest.raises(ConnectionError):
        manager.record_scoped_usage("team", "alpha", 1)


def test_explicit_scopes_argument_overrides_attached():
    manager = _manager()  # nothing attached
    fake = QuotaScopesTableExecutor()
    scopes = QuotaScopes(fake)
    scopes.set_limits("project", "beta", {"daily_tokens": 10})
    info = manager.check_scoped_token_budget(
        "project", "beta", 5, quota_scopes=scopes)
    assert info["limit"] == 10
    with pytest.raises(QuotaExceededException):
        manager.check_scoped_token_budget(
            "project", "beta", 11, quota_scopes=scopes)
