"""Unit tests for the stdlib-only alert evaluator.

Demonstrates pending -> firing -> resolved lifecycle honouring ``for_seconds``,
and that the service-down and backup-age alerts FIRE with the shipped rules.
"""

import json
from pathlib import Path

import pytest

from backend.services.observability import alerts
from backend.services.observability.alerts import (
    _instance_key,
    _matches,
    _render_summary,
    evaluate,
    load_rules,
    save_state,
)


SERVICE_DOWN_RULE = {
    "name": "service_down",
    "metric": "observability_service_up",
    "labels": {"service": "*"},
    "op": "lt",
    "threshold": 1,
    "for_seconds": 60,
    "severity": "critical",
    "owner": "owner-pending",
    "runbook": "service_down.md",
    "summary": "Service {{service}} is down",
}

BACKUP_AGE_RULE = {
    "name": "backup_age",
    "metric": "observability_backup_newest_age_seconds",
    "op": "gt",
    "threshold": 93600,
    "for_seconds": 3600,
    "severity": "critical",
    "owner": "owner-pending",
    "runbook": "backup_age.md",
    "summary": "Newest backup is {{value}}s old (limit 26h)",
}


def _rules(*rules):
    return {"rules": list(rules)}


# ---------------------------------------------------------------------------
# Instance identity + matching
# ---------------------------------------------------------------------------


def test_instance_key_and_matches():
    rule = SERVICE_DOWN_RULE
    sample = {"name": "observability_service_up", "labels": {"service": "agent_tools"}, "value": 0.0}
    assert _matches(rule, sample) is True
    key = _instance_key(rule, sample["labels"])
    assert key == "service_down{service=agent_tools}"

    # Non-matching metric / label value
    assert _matches(rule, {"name": "other", "labels": {"service": "x"}, "value": 0.0}) is False
    assert _matches(rule, {"name": "observability_service_up", "labels": {}, "value": 0.0}) is False


def test_render_summary():
    assert _render_summary("Service {{service}} is down", {"service": "valkey"}, None) == "Service valkey is down"
    assert _render_summary("Backlog {{value}} events", {}, 42.0) == "Backlog 42 events"
    assert _render_summary("GPU {{gpu}} at {{value}}%", {"gpu": "0"}, 91.5) == "GPU 0 at 91.5%"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_service_down_fires_after_for_seconds():
    rules = _rules(SERVICE_DOWN_RULE)
    samples = [{"name": "observability_service_up", "labels": {"service": "agent_tools"}, "value": 0.0}]
    state = {}

    events, state = evaluate(samples, rules, state, now=1000.0)
    assert events == []
    assert state["service_down{service=agent_tools}"]["state"] == "pending"

    # Still within for_seconds: no fire yet.
    events, state = evaluate(samples, rules, state, now=1030.0)
    assert events == []

    # Past for_seconds: fires.
    events, state = evaluate(samples, rules, state, now=1061.0)
    assert len(events) == 1
    ev = events[0]
    assert ev["type"] == "firing"
    assert ev["name"] == "service_down"
    assert ev["severity"] == "critical"
    assert ev["summary"] == "Service agent_tools is down"
    assert state["service_down{service=agent_tools}"]["state"] == "firing"


def test_backup_age_fires():
    rules = _rules(BACKUP_AGE_RULE)
    samples = [{"name": "observability_backup_newest_age_seconds", "labels": {}, "value": 100000.0}]
    state = {}

    events, state = evaluate(samples, rules, state, now=1000.0)
    assert events == []
    events, state = evaluate(samples, rules, state, now=5000.0)  # > 3600s
    assert len(events) == 1
    assert events[0]["type"] == "firing"
    assert events[0]["name"] == "backup_age"


def test_resolve_honours_condition_clear():
    rules = _rules(SERVICE_DOWN_RULE)
    key = "service_down{service=agent_tools}"
    samples_down = [{"name": "observability_service_up", "labels": {"service": "agent_tools"}, "value": 0.0}]
    state = {}

    evaluate(samples_down, rules, state, now=1000.0)
    evaluate(samples_down, rules, state, now=2000.0)
    assert state[key]["state"] == "firing"

    # Service recovers.
    samples_up = [{"name": "observability_service_up", "labels": {"service": "agent_tools"}, "value": 1.0}]
    events, state = evaluate(samples_up, rules, state, now=3000.0)
    assert len(events) == 1
    assert events[0]["type"] == "resolved"
    assert state[key]["state"] == "resolved"


def test_firing_not_repeated():
    rules = _rules(SERVICE_DOWN_RULE)
    samples = [{"name": "observability_service_up", "labels": {"service": "valkey"}, "value": 0.0}]
    state = {}
    evaluate(samples, rules, state, now=1000.0)
    events, state = evaluate(samples, rules, state, now=2000.0)
    assert len(events) == 1
    events, state = evaluate(samples, rules, state, now=3000.0)
    assert events == []  # already firing; no duplicate


def test_disabled_rule_is_skipped():
    rules = _rules({**BACKUP_AGE_RULE, "enabled": False})
    samples = [{"name": "observability_backup_newest_age_seconds", "labels": {}, "value": 100000.0}]
    state = {}
    events, state = evaluate(samples, rules, state, now=999999.0)
    assert events == []


# ---------------------------------------------------------------------------
# Shipped rules file
# ---------------------------------------------------------------------------


def test_shipped_rules_load_and_cover_required_alerts(tmp_path):
    repo = Path(__file__).resolve().parents[3]
    rules = load_rules(repo / "backend" / "config" / "observability" / "alerts.json")
    names = {r["name"] for r in rules["rules"]}
    required = {
        "service_down",
        "outbox_backlog",
        "outbox_age",
        "backup_age",
        "disk_usage",
        "ram_usage",
        "stale_leases",
        "gpu_memory",
        "quota_reject_spike",
    }
    assert required <= names
    for rule in rules["rules"]:
        assert rule.get("owner") == "owner-pending"
        assert rule.get("runbook")
        assert rule.get("severity") in ("warning", "critical")


def test_state_persistence_roundtrip(tmp_path):
    state = {"k": {"state": "firing", "since": 1.0, "value": 2.0}}
    save_state(state, tmp_path / "state.json")
    loaded = alerts.load_state(tmp_path / "state.json")
    assert loaded == state
