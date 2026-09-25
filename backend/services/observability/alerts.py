"""
Stdlib-only alert evaluator.

Reads alert rules from ``backend/config/observability/alerts.json``, evaluates
the current metric samples (produced by ``collector.collect_samples``), and
maintains per-alert state under ``backend/data/observability/`` with the
pending -> firing -> resolved lifecycle honouring each rule's ``for_seconds``.

Sinks, in order:
  1. A JSON-lines log file (``backend/data/observability/alerts.log``).
  2. A best-effort event to VictoriaLogs under stream ``service:observability``.
  3. An optional per-rule command hook (run without a shell; environment
     variables ALERT_NAME / ALERT_STATE / ALERT_VALUE / ALERT_SEVERITY /
     ALERT_LABELS are populated).

Usage:
    python -m backend.services.observability.alerts
        [--textfile PATH] [--once] [--no-victorialogs]

By default the evaluator collects fresh samples via the collector, evaluates
them, persists state, runs sinks and exits. With ``--loop N`` it repeats.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib import request as urlrequest

from . import collector as _collector

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BACKEND_DIR / "data"
CONFIG_DIR = BACKEND_DIR / "config"

DEFAULT_RULES_PATH = CONFIG_DIR / "observability" / "alerts.json"
STATE_DIR = DATA_DIR / "observability"
STATE_PATH = STATE_DIR / "state.json"
LOG_PATH = STATE_DIR / "alerts.log"

VICTORIALOGS_URL = os.getenv("VICTORIALOGS_URL", "http://127.0.0.1:9428")

_OPS = {
    "lt": lambda a, b: a < b,
    "le": lambda a, b: a <= b,
    "gt": lambda a, b: a > b,
    "ge": lambda a, b: a >= b,
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
}

_STATES = ("inactive", "pending", "firing", "resolved")


def load_rules(path: Optional[Path] = None) -> Dict[str, Any]:
    path = Path(path) if path is not None else DEFAULT_RULES_PATH
    if not path.exists():
        raise FileNotFoundError(f"alert rules file not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_state(path: Optional[Path] = None) -> Dict[str, Any]:
    path = Path(path) if path is not None else STATE_PATH
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def save_state(state: Dict[str, Any], path: Optional[Path] = None) -> None:
    path = Path(path) if path is not None else STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _render_summary(template: str, labels: Dict[str, str], value: Optional[float]) -> str:
    out = template
    for k, v in labels.items():
        out = out.replace("{{" + k + "}}", str(v))
    if value is not None:
        rendered = str(int(value)) if float(value).is_integer() else f"{value:.6g}"
        out = out.replace("{{value}}", rendered)
    return out


def _instance_key(rule: Dict[str, Any], labels: Dict[str, str]) -> str:
    wildcard = [
        k for k, v in (rule.get("labels") or {}).items()
        if v == "*" and k in labels
    ]
    parts = [f"{k}={labels[k]}" for k in sorted(wildcard)]
    if parts:
        return f"{rule['name']}{{{','.join(parts)}}}"
    return rule["name"]


def _matches(rule: Dict[str, Any], sample: Dict[str, Any]) -> bool:
    if sample.get("name") != rule.get("metric"):
        return False
    if sample.get("value") is None:
        return False
    for k, v in (rule.get("labels") or {}).items():
        if v == "*":
            if k not in sample.get("labels", {}):
                return False
        elif sample.get("labels", {}).get(k) != v:
            return False
    return True


def evaluate(
    samples: List[Dict[str, Any]],
    rules: Dict[str, Any],
    state: Dict[str, Any],
    now: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Return (events, new_state). ``state`` is mutated in place and returned."""
    now = now if now is not None else time.time()
    events: List[Dict[str, Any]] = []
    active_keys: set = set()

    for rule in rules.get("rules", []):
        if rule.get("enabled") is False:
            continue
        op = _OPS.get(rule.get("op", ""))
        if op is None:
            continue
        threshold = float(rule.get("threshold", 0))
        for_seconds = float(rule.get("for_seconds", 0))
        instances: Dict[str, Dict[str, str]] = {}
        values: Dict[str, float] = {}
        for sample in samples:
            if not _matches(rule, sample):
                continue
            labels = dict(sample.get("labels", {}))
            key = _instance_key(rule, labels)
            # A label combination can only produce one sample per metric; keep
            # the first value seen.
            if key not in values:
                instances[key] = labels
                values[key] = float(sample["value"])

        for key, labels in instances.items():
            value = values[key]
            active_keys.add(key)
            firing_condition = op(value, threshold)
            record = state.get(key, {"state": "inactive", "since": now, "value": value})

            if firing_condition:
                if record.get("state") in ("inactive", "resolved"):
                    record = {"state": "pending", "since": now, "value": value,
                              "rule": rule["name"], "labels": labels}
                    state[key] = record
                elif record.get("state") == "pending":
                    since = float(record.get("since", now))
                    if now - since >= for_seconds:
                        record["state"] = "firing"
                        record["value"] = value
                        state[key] = record
                        events.append(_make_event("firing", rule, labels, value, now))
                elif record.get("state") == "firing":
                    record["value"] = value
                    state[key] = record
            else:
                if record.get("state") in ("pending", "firing"):
                    record = {"state": "resolved", "since": now, "value": value,
                              "rule": rule["name"], "labels": labels}
                    state[key] = record
                    events.append(_make_event("resolved", rule, labels, value, now))
                elif record.get("state") == "resolved":
                    state[key] = record

    # Prune state for rules/labels no longer present so stale instances do not
    # linger forever after a metric disappears.
    for key in list(state.keys()):
        if key not in active_keys and state.get(key, {}).get("state") in ("firing", "pending"):
            state[key]["state"] = "resolved"
            state[key]["since"] = now

    return events, state


def _make_event(kind: str, rule: Dict[str, Any], labels: Dict[str, str], value: float, now: float) -> Dict[str, Any]:
    return {
        "type": kind,
        "timestamp": _collector._iso_now(now),
        "name": rule["name"],
        "severity": rule.get("severity", "warning"),
        "owner": rule.get("owner", "owner-pending"),
        "runbook": rule.get("runbook", ""),
        "labels": labels,
        "value": value,
        "summary": _render_summary(rule.get("summary", rule["name"]), labels, value),
    }


# ---------------------------------------------------------------------------
# Sinks
# ---------------------------------------------------------------------------


def _log_sink(event: Dict[str, Any], log_path: Optional[Path] = None) -> None:
    path = Path(log_path) if log_path is not None else LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def _victorialogs_sink(event: Dict[str, Any], url: str, timeout: float = 2.0) -> bool:
    try:
        target = f"{url.rstrip('/')}/insert/jsonline?_stream_fields=service&_time_field=timestamp"
        payload = dict(event)
        payload["service"] = "observability"
        req = urlrequest.Request(
            target,
            data=(json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"),
            headers={"Content-Type": "application/stream+json"},
            method="POST",
        )
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except Exception:
        return False


def _hook_sink(event: Dict[str, Any], rule: Dict[str, Any]) -> None:
    command = rule.get("command")
    if not command:
        return
    env = dict(os.environ)
    env["ALERT_NAME"] = event.get("name", "")
    env["ALERT_STATE"] = event.get("type", "")
    env["ALERT_VALUE"] = str(event.get("value", ""))
    env["ALERT_SEVERITY"] = event.get("severity", "")
    env["ALERT_LABELS"] = json.dumps(event.get("labels", {}), sort_keys=True)
    try:
        subprocess.run(shlex.split(command), env=env, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        # Hooks are best-effort; a broken hook must not break the evaluator.
        pass


def run_once(
    rules: Optional[Dict[str, Any]] = None,
    samples: Optional[List[Dict[str, Any]]] = None,
    state_path: Optional[Path] = None,
    log_path: Optional[Path] = None,
    victorialogs: bool = True,
    now: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    rules = rules if rules is not None else load_rules()
    samples = samples if samples is not None else _collector.collect_samples()[0]
    state = load_state(state_path)
    events, state = evaluate(samples, rules, state, now=now)
    save_state(state, state_path)
    for event in events:
        _log_sink(event, log_path)
        if victorialogs:
            _victorialogs_sink(event, VICTORIALOGS_URL)
        rule = next((r for r in rules.get("rules", []) if r.get("name") == event["name"]), {})
        _hook_sink(event, rule)
    return events, state


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Stdlib-only alert evaluator")
    parser.add_argument("--loop", type=int, default=0, help="repeat N times (default 0 = once)")
    parser.add_argument("--interval", type=float, default=30.0, help="seconds between loop iterations")
    parser.add_argument("--no-victorialogs", action="store_true", help="skip VictoriaLogs event sink")
    args = parser.parse_args(argv)

    rules = load_rules()
    iterations = max(0, args.loop) if args.loop > 0 else 1
    for _ in range(iterations):
        events, _state = run_once(rules=rules, victorialogs=not args.no_victorialogs)
        for ev in events:
            print(json.dumps(ev, ensure_ascii=False, sort_keys=True))
        if iterations > 1:
            time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
