"""PR-B3: multi-worker admission atomicity against a LIVE disposable Valkey.

The admission actors are genuine worker PROCESSES (multiprocessing, not
threads) exercising the real quota_manager public API. This pytest wraps the
standalone qualification runner
(backend/tests/qualification/multiworker_admission.py), which starts a
disposable Valkey on a free port with `--save "" --appendonly no`, a per-run
random requirepass, and tears it down in a finally block.

Skips when the valkey-server binary is unavailable (and when the runner itself
reports a skip via exit code 3).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
RUNNER = BACKEND_DIR / "tests" / "qualification" / "multiworker_admission.py"
VALKEY_BIN = BACKEND_DIR / "bin" / "valkey-server"


def _binary_available() -> bool:
    try:
        return VALKEY_BIN.exists() and os.access(VALKEY_BIN, os.X_OK)
    except OSError:
        return False


@pytest.mark.skipif(not _binary_available(), reason="valkey-server binary unavailable")
def test_multiworker_admission_atomicity(tmp_path):
    out = tmp_path / "report.json"
    proc = subprocess.run(
        [sys.executable, str(RUNNER), "--out", str(out)],
        capture_output=True, text=True, timeout=180,
    )
    if proc.returncode == 3:
        pytest.skip("runner skipped (valkey-server unavailable)")
    assert proc.returncode == 0, f"runner failed:\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"

    report = json.loads(out.read_text())
    assert report["passed"] is True, f"violations: {report['violations']}"

    # (1) Concurrency: never exceed the per-user ceiling at any sampled instant,
    #     and no leaked slots once all workers complete.
    conc = report["concurrency"]
    assert conc["max_observed_concurrency"] <= conc["limit"], \
        f"max observed {conc['max_observed_concurrency']} > limit {conc['limit']}"
    assert conc["admitted"] > 0
    assert conc["leaked_user_slots"] == 0
    assert conc["leaked_cluster_slots"] == 0

    # (1b) SIGKILL mid-lease -> stale lease recovered via TTL/expiry, reacquired.
    stale = report["stale_lease_recovery"]
    assert stale["active_at_kill"] == 1
    assert stale["recovered"] is True
    assert stale["reacquired_after_recovery"] is True
    assert stale["leaked_slots"] == 0

    # (2) RPM: admitted == limit exactly within the rolling window.
    rpm = report["rpm"]
    assert rpm["admitted"] == rpm["limit"]
    assert rpm["admitted"] + rpm["rejected"] == rpm["attempts"]

    # (3) Daily reservation: no overspend, settlement exactly-once, dup ID rejected.
    dr = report["daily_reservation"]
    assert dr["reserved_total"] == dr["reservations"] * dr["estimate"]
    assert dr["reserved_total"] <= dr["limit"]
    do = report["daily_overspend"]
    assert do["reserved_total"] <= do["limit"]
    assert do["admitted"] == do["limit"] // do["estimate"]
    ds = report["daily_settlement"]
    assert ds["consumed"] == ds["expected"]
    assert ds["settled_entries"] == ds["reservations"]
    assert ds["active_left"] == 0
    assert report["duplicate_reservation_id"] == {"successes": 1, "rejections": 1}

    # (4) Fail-closed: killing Valkey mid-run yields ConnectionError, never admit.
    fc = report["fail_closed"]
    assert fc["killed"] is True
    assert fc["outcomes"]["acquire"] == "ConnectionError"
    assert fc["outcomes"]["rpm"] == "ConnectionError"
    assert fc["outcomes"]["reserve"] == "ConnectionError"
