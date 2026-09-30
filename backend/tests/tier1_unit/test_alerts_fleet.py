"""Tier 1 unit tests: fleet alert rules (backend/config/observability/alerts-fleet.yml).

Validates that the YAML parses, every rule carries a threshold + severity +
description (summary), and that the rules are schema-compatible with the
existing stdlib alert evaluator (alerts.py / alerts.json format).
"""
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

from backend.services.observability import alerts  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
FLEET_ALERTS = REPO_ROOT / "backend" / "config" / "observability" / "alerts-fleet.yml"

REQUIRED_RULES = {
    "fleet_node_down",
    "fleet_node_stale",
    "fleet_model_unhealthy",
    "fleet_audit_outbox_lag",
    "fleet_vram_high",
    "fleet_cert_expiring",
}

ALLOWED_OPS = {"lt", "le", "gt", "ge", "eq", "ne"}
ALLOWED_SEVERITIES = {"critical", "warning"}


@pytest.fixture(scope="module")
def rules_doc():
    with open(FLEET_ALERTS, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    assert isinstance(doc, dict) and isinstance(doc.get("rules"), list)
    return doc


@pytest.fixture(scope="module")
def rules(rules_doc):
    return {r["name"]: r for r in rules_doc["rules"]}


def test_all_required_rules_present(rules):
    assert REQUIRED_RULES <= set(rules), f"missing: {REQUIRED_RULES - set(rules)}"


def test_rule_names_unique(rules_doc):
    names = [r["name"] for r in rules_doc["rules"]]
    assert len(names) == len(set(names))


def test_every_rule_has_threshold_severity_description(rules):
    for name, rule in rules.items():
        assert "threshold" in rule, f"{name}: missing threshold"
        assert isinstance(rule["threshold"], (int, float)), f"{name}: threshold not numeric"
        assert rule.get("severity") in ALLOWED_SEVERITIES, f"{name}: bad severity"
        assert rule.get("summary", "").strip(), f"{name}: missing description (summary)"
        assert rule.get("metric", "").strip(), f"{name}: missing metric"
        assert rule.get("op") in ALLOWED_OPS, f"{name}: bad op"


def test_slo_thresholds_match_spec(rules):
    # Spec §8.2 / §9 P1.11: outbox lag > 60 s, VRAM > 90 %, cert < 30 days.
    assert rules["fleet_audit_outbox_lag"]["threshold"] == 60
    assert rules["fleet_audit_outbox_lag"]["op"] == "gt"
    assert rules["fleet_vram_high"]["threshold"] == 90
    assert rules["fleet_vram_high"]["op"] == "gt"
    assert rules["fleet_cert_expiring"]["threshold"] == 30
    assert rules["fleet_cert_expiring"]["op"] == "lt"


def test_rules_evaluate_with_existing_evaluator(rules_doc):
    """The YAML rules must be consumable by alerts.evaluate (same schema as alerts.json)."""
    now = 1_700_000_000.0
    # A down node sample: fleet_node_down should go pending -> firing.
    samples = [{"name": "observability_fleet_node_up",
                "labels": {"node": "gpu-01"}, "value": 0.0}]
    events, state = alerts.evaluate(samples, rules_doc, {}, now=now)
    assert not events  # pending first
    later = now + 31  # for_seconds=30 elapsed
    events, _state = alerts.evaluate(samples, rules_doc, state, now=later)
    firing = [e for e in events if e["type"] == "firing" and e["name"] == "fleet_node_down"]
    assert len(firing) == 1
    assert firing[0]["labels"] == {"node": "gpu-01"}
    assert firing[0]["severity"] == "critical"
