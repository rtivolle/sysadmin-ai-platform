#!/usr/bin/env python3
"""
PR-B3: Multi-worker admission atomicity qualification (LIVE disposable Valkey).

Proves that the quota_manager admission primitives are atomic under genuine
concurrent worker PROCESSES (multiprocessing, never threads for the admission
actors), against a disposable Valkey started from backend/bin/valkey-server on a
free port with `--save "" --appendonly no` and a per-run random requirepass.

Coverage:
  1. Concurrency leases  -- N workers x M attempts for one user never exceed the
     per-user active ceiling at any sampled instant (store sampled directly) and
     no slots leak after completion. A worker SIGKILLed mid-lease leaves a stale
     lease that is recovered by the existing TTL/expiry mechanism.
  2. RPM                 -- admitted == limit exactly within the rolling window.
  3. Daily reservation   -- concurrent reservations never overspend; settlement
     is exactly-once (duplicate IDs rejected / ignored).
  4. Fail-closed         -- killing Valkey mid-run yields ConnectionError and
     never an admit.

The runner writes a JSON report (counts, max observed concurrency, violations,
timings, valkey version) to --out (default /tmp/opencode/multiworker_admission.json).

Usage:
  backend/.venv/bin/python3 backend/tests/qualification/multiworker_admission.py [--out PATH]

Exit: 0 = all checks passed, 1 = violations, 3 = skipped (valkey-server missing).
"""
import argparse
import json
import multiprocessing
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import redis  # noqa: E402
from services.auth_gateway.quota_manager import (  # noqa: E402
    QuotaManager,
    QuotaExceededException,
)

VALKEY_BIN = BACKEND_DIR / "bin" / "valkey-server"


# ---------------------------------------------------------------------------
# Disposable Valkey fixture
# ---------------------------------------------------------------------------
def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class DisposableValkey:
    def __init__(self):
        self.port = _free_port()
        self.password = secrets.token_hex(24)
        self.dir = tempfile.mkdtemp(prefix="multiworker-valkey-")
        self.proc = None
        self.url = f"redis://:{self.password}@127.0.0.1:{self.port}/0"

    def version(self) -> str:
        try:
            return subprocess.run(
                [str(VALKEY_BIN), "--version"], capture_output=True, text=True
            ).stdout.strip()
        except Exception:
            return "unknown"

    def start(self):
        args = [
            str(VALKEY_BIN),
            "--port", str(self.port),
            "--bind", "127.0.0.1",
            "--save", "",
            "--appendonly", "no",
            "--requirepass", self.password,
            "--dir", self.dir,
            "--protected-mode", "no",
            "--pidfile", os.path.join(self.dir, "valkey.pid"),
            "--logfile", os.path.join(self.dir, "valkey.log"),
        ]
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 15
        while time.time() < deadline:
            if self.proc.poll() is not None:
                log = os.path.join(self.dir, "valkey.log")
                tail = ""
                try:
                    tail = Path(log).read_text()[-2000:]
                except Exception:
                    pass
                raise RuntimeError(f"valkey-server exited early rc={self.proc.returncode}\n{tail}")
            try:
                r = redis.Redis.from_url(self.url, decode_responses=True, socket_timeout=1.0)
                if r.ping():
                    return
            except Exception:
                pass
            time.sleep(0.05)
        raise TimeoutError("valkey-server did not become ready")

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        shutil.rmtree(self.dir, ignore_errors=True)


def _client(url: str) -> redis.Redis:
    return redis.Redis.from_url(url, decode_responses=True, socket_timeout=2.0)


# ---------------------------------------------------------------------------
# Worker functions (run in separate PROCESSES via multiprocessing)
# ---------------------------------------------------------------------------
def _worker_concurrency(valkey_url, user_id, attempts, hold_ms, outq):
    qm = QuotaManager(valkey_url=valkey_url)
    admitted = rejected = 0
    errors = []
    for _ in range(attempts):
        try:
            lease = qm.acquire_concurrency_slot(user_id, timeout_seconds=30)
            admitted += 1
            time.sleep(hold_ms / 1000.0)
            qm.release_concurrency_slot(lease)
        except QuotaExceededException:
            rejected += 1
            time.sleep(0.001 + 0.004 * (uuid.uuid4().int % 1000) / 1000.0)
        except Exception as exc:  # fail-closed: a ConnectionError is a violation
            errors.append(f"{type(exc).__name__}: {exc}")
    outq.put({"admitted": admitted, "rejected": rejected, "errors": errors})


def _worker_rpm(valkey_url, user_id, attempts, outq):
    qm = QuotaManager(valkey_url=valkey_url)
    admitted = rejected = 0
    errors = []
    for _ in range(attempts):
        try:
            qm.check_and_record_rpm(user_id)
            admitted += 1
        except QuotaExceededException:
            rejected += 1
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
    outq.put({"admitted": admitted, "rejected": rejected, "errors": errors})


def _worker_reserve(valkey_url, user_id, reservations, estimate, outq):
    qm = QuotaManager(valkey_url=valkey_url)
    admitted = rejected = dups = 0
    errors = []
    for rid in reservations:
        try:
            qm.reserve_daily_token_budget(user_id, rid, estimate)
            admitted += 1
        except QuotaExceededException:
            rejected += 1
        except ValueError:
            dups += 1
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
    outq.put({"admitted": admitted, "rejected": rejected, "duplicates": dups, "errors": errors})


def _worker_settle(valkey_url, user_id, day, reservations, prompt, completion, outq):
    qm = QuotaManager(valkey_url=valkey_url)
    settled = 0
    errors = []
    for rid in reservations:
        try:
            qm.settle_daily_token_reservation(user_id, rid, day, prompt, completion)
            settled += 1
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
    outq.put({"settled": settled, "errors": errors})


def _worker_hold_lease(valkey_url, user_id, timeout_s, ready_evt):
    qm = QuotaManager(valkey_url=valkey_url)
    qm.acquire_concurrency_slot(user_id, timeout_seconds=timeout_s)
    ready_evt.set()
    time.sleep(60)  # held forever; the parent SIGKILLs this process mid-lease


def _sample_max_concurrency(url, user_id, stop_evt, max_val):
    r = _client(url)
    key = f"quota:leases:user:{user_id}"
    while not stop_evt.is_set():
        try:
            now = time.time()
            c = int(r.zcount(key, f"({now}", "+inf"))
            if c > max_val.value:
                max_val.value = c
        except Exception:
            pass
        time.sleep(0.001)


def _spawn(target, args):
    q = multiprocessing.Queue()
    procs = []
    for a in args:
        p = multiprocessing.Process(target=target, args=(*a, q))
        p.start()
        procs.append(p)
    return procs, q


def _collect(procs, q):
    results = []
    for _ in procs:
        results.append(q.get(timeout=60))
    for p in procs:
        p.join(timeout=60)
    return results


def _split(items, parts):
    """Split items into `parts` roughly-equal lists (keeps order)."""
    out = [[] for _ in range(parts)]
    for i, it in enumerate(items):
        out[i % parts].append(it)
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Multi-worker admission atomicity qualification")
    ap.add_argument("--out", default="/tmp/opencode/multiworker_admission.json",
                    help="JSON report path")
    args = ap.parse_args()

    if not VALKEY_BIN.exists() or not os.access(VALKEY_BIN, os.X_OK):
        print(f"SKIP: valkey-server binary missing or not executable at {VALKEY_BIN}")
        return 3

    run_id = uuid.uuid4().hex[:8]
    report = {"run_id": run_id, "valkey_version": None, "violations": []}
    timings = {}

    def check(ok, name, detail=""):
        if not ok:
            report["violations"].append({"name": name, "detail": detail})

    inst = DisposableValkey()
    report["valkey_version"] = inst.version()
    report["port"] = inst.port
    print(f"multiworker_admission: valkey {report['valkey_version']} on 127.0.0.1:{inst.port}")

    try:
        inst.start()
        r = _client(inst.url)

        # ==================================================================
        # (1) Concurrency leases across worker processes
        # ==================================================================
        t0 = time.monotonic()
        user_conc = f"prb3-conc-{run_id}"
        N, M, HOLD = 4, 40, 12
        stop_evt = multiprocessing.Event()
        max_val = multiprocessing.Value("i", 0)
        sampler = multiprocessing.Process(
            target=_sample_max_concurrency, args=(inst.url, user_conc, stop_evt, max_val))
        sampler.start()
        procs, q = _spawn(_worker_concurrency, [(inst.url, user_conc, M, HOLD) for _ in range(N)])
        res = _collect(procs, q)
        stop_evt.set()
        sampler.join(timeout=10)
        conc_admitted = sum(x["admitted"] for x in res)
        conc_rejected = sum(x["rejected"] for x in res)
        conc_errors = [e for x in res for e in x["errors"]]
        max_seen = int(max_val.value)
        leaked = int(r.zcard(f"quota:leases:user:{user_conc}"))
        cluster_leaked = int(r.zcard("quota:leases:cluster"))
        inflight = r.get(f"inflight:{user_conc}")
        timings["concurrency_s"] = round(time.monotonic() - t0, 3)

        report["concurrency"] = {
            "workers": N, "attempts_per_worker": M, "limit": 2,
            "admitted": conc_admitted, "rejected": conc_rejected,
            "max_observed_concurrency": max_seen,
            "leaked_user_slots": leaked, "leaked_cluster_slots": cluster_leaked,
            "inflight_mirror": inflight, "errors": conc_errors,
        }
        check(max_seen <= 2, "concurrency_ceiling_never_exceeded",
              f"max observed {max_seen} > ceiling 2")
        check(conc_admitted > 0, "concurrency_admission_observed", f"admitted {conc_admitted}")
        check(leaked == 0 and cluster_leaked == 0, "concurrency_no_leaked_slots",
              f"user {leaked} cluster {cluster_leaked}")
        check(inflight in (None, "0"), "concurrency_inflight_mirror_consistent", f"inflight={inflight}")
        check(not conc_errors, "concurrency_no_connection_errors", "; ".join(conc_errors[:3]))

        # ==================================================================
        # (1b) SIGKILL mid-lease -> stale lease recovery via TTL/expiry
        # ==================================================================
        t0 = time.monotonic()
        user_stale = f"prb3-stale-{run_id}"
        ready = multiprocessing.Event()
        holder = multiprocessing.Process(
            target=_worker_hold_lease, args=(inst.url, user_stale, 1, ready))
        holder.start()
        if not ready.wait(timeout=10):
            check(False, "stale_holder_acquired", "holder never signalled acquisition")
        else:
            active_now = int(r.zcount(f"quota:leases:user:{user_stale}", f"({time.time()}", "+inf"))
            os.kill(holder.pid, signal.SIGKILL)
            holder.join(timeout=10)
            # Stale member score == acquire_time + 1s; the exclusive-now count drops
            # to 0 once the lease expires (existing TTL mechanism), then a fresh
            # acquire succeeds after ZREMRANGEBYSCORE prunes the dead member.
            recovered_at = None
            deadline = time.time() + 5
            while time.time() < deadline:
                if int(r.zcount(f"quota:leases:user:{user_stale}", f"({time.time()}", "+inf")) == 0:
                    recovered_at = time.monotonic()
                    break
                time.sleep(0.02)
            qm = QuotaManager(valkey_url=inst.url)
            try:
                lease = qm.acquire_concurrency_slot(user_stale, timeout_seconds=10)
                reacquired = True
                qm.release_concurrency_slot(lease)
            except Exception as exc:
                reacquired = False
                conc_errors.append(f"stale-reacquire: {type(exc).__name__}: {exc}")
            stale_leaked = int(r.zcard(f"quota:leases:user:{user_stale}"))
            timings["stale_recovery_s"] = round(time.monotonic() - t0, 3)
            report["stale_lease_recovery"] = {
                "active_at_kill": active_now,
                "recovered": recovered_at is not None,
                "reacquired_after_recovery": reacquired,
                "leaked_slots": stale_leaked,
            }
            check(active_now == 1, "stale_active_at_kill", f"active={active_now}")
            check(recovered_at is not None, "stale_lease_expired_via_ttl", "no expiry observed within 5s")
            check(reacquired, "stale_slot_reacquired_after_recovery", "fresh acquire failed")
            check(stale_leaked == 0, "stale_no_leak_after_recovery", f"zcard={stale_leaked}")

        # ==================================================================
        # (2) RPM across workers: admitted == limit exactly
        # ==================================================================
        t0 = time.monotonic()
        user_rpm = f"prb3-rpm-{run_id}"
        RPM_LIMIT, RPM_ATTEMPTS, RPM_WORKERS = 50, 60, 4
        qm_set = QuotaManager(valkey_url=inst.url)
        qm_set.set_limits(user_rpm, {"rpm": RPM_LIMIT})
        per = RPM_ATTEMPTS // RPM_WORKERS
        procs, q = _spawn(_worker_rpm, [(inst.url, user_rpm, per) for _ in range(RPM_WORKERS)])
        res = _collect(procs, q)
        rpm_admitted = sum(x["admitted"] for x in res)
        rpm_rejected = sum(x["rejected"] for x in res)
        rpm_errors = [e for x in res for e in x["errors"]]
        timings["rpm_s"] = round(time.monotonic() - t0, 3)
        report["rpm"] = {"limit": RPM_LIMIT, "attempts": RPM_ATTEMPTS,
                         "admitted": rpm_admitted, "rejected": rpm_rejected,
                         "errors": rpm_errors}
        check(rpm_admitted == RPM_LIMIT, "rpm_admitted_exactly_limit",
              f"admitted {rpm_admitted} != limit {RPM_LIMIT}")
        check(rpm_admitted + rpm_rejected == RPM_ATTEMPTS, "rpm_no_silent_drops",
              f"{rpm_admitted}+{rpm_rejected} != {RPM_ATTEMPTS}")
        check(not rpm_errors, "rpm_no_connection_errors", "; ".join(rpm_errors[:3]))

        # ==================================================================
        # (3a) Daily reservation: concurrent reservations never overspend
        # ==================================================================
        t0 = time.monotonic()
        user_daily = f"prb3-daily-{run_id}"
        DAILY_LIMIT, EST, RESV = 100_000, 500, 100
        qm_set.set_limits(user_daily, {"daily_tokens": DAILY_LIMIT})
        ids = [f"resv-{run_id}-{i}" for i in range(RESV)]
        procs, q = _spawn(_worker_reserve, [(inst.url, user_daily, part, EST)
                                            for part in _split(ids, 4)])
        res = _collect(procs, q)
        daily_admitted = sum(x["admitted"] for x in res)
        daily_rejected = sum(x["rejected"] for x in res)
        daily_errors = [e for x in res for e in x["errors"]]
        day = QuotaManager._quota_day()
        active_key = f"daily_reservations:active:{user_daily}:{day}"
        reserved_total = sum(int(v) for v in r.hvals(active_key))
        timings["daily_reserve_s"] = round(time.monotonic() - t0, 3)
        report["daily_reservation"] = {
            "limit": DAILY_LIMIT, "reservations": RESV, "estimate": EST,
            "admitted": daily_admitted, "rejected": daily_rejected,
            "reserved_total": reserved_total, "errors": daily_errors,
        }
        check(daily_admitted == RESV and reserved_total == RESV * EST,
              "daily_no_overspend_all_admitted",
              f"admitted {daily_admitted}, reserved {reserved_total}, expected {RESV * EST}")
        check(reserved_total <= DAILY_LIMIT, "daily_reserved_within_budget",
              f"reserved {reserved_total} > limit {DAILY_LIMIT}")
        check(not daily_errors, "daily_no_connection_errors", "; ".join(daily_errors[:3]))

        # ==================================================================
        # (3b) Overspend rejection under concurrency (atomic no-overspend)
        # ==================================================================
        t0 = time.monotonic()
        user_over = f"prb3-over-{run_id}"
        OVER_LIMIT, OVER_EST, OVER_ATTEMPTS = 3_000, 1_000, 40
        qm_set.set_limits(user_over, {"daily_tokens": OVER_LIMIT})
        over_ids = [f"over-{run_id}-{i}" for i in range(OVER_ATTEMPTS)]
        procs, q = _spawn(_worker_reserve, [(inst.url, user_over, part, OVER_EST)
                                            for part in _split(over_ids, 4)])
        res = _collect(procs, q)
        over_admitted = sum(x["admitted"] for x in res)
        over_rejected = sum(x["rejected"] for x in res)
        over_day = QuotaManager._quota_day()
        over_key = f"daily_reservations:active:{user_over}:{over_day}"
        over_reserved = sum(int(v) for v in r.hvals(over_key))
        timings["daily_overspend_s"] = round(time.monotonic() - t0, 3)
        report["daily_overspend"] = {
            "limit": OVER_LIMIT, "estimate": OVER_EST, "attempts": OVER_ATTEMPTS,
            "admitted": over_admitted, "rejected": over_rejected,
            "reserved_total": over_reserved,
        }
        expected_admits = OVER_LIMIT // OVER_EST
        check(over_admitted == expected_admits, "daily_overspend_atomic",
              f"admitted {over_admitted} != floor(limit/est) {expected_admits}")
        check(over_reserved == expected_admits * OVER_EST and over_reserved <= OVER_LIMIT,
              "daily_overspend_within_budget",
              f"reserved {over_reserved} > limit {OVER_LIMIT}")

        # ==================================================================
        # (3c) Settlement exactly-once (duplicate settles ignored)
        # ==================================================================
        t0 = time.monotonic()
        PROMPT, COMPL = 300, 200
        ACTUAL = PROMPT + COMPL
        procs, q = _spawn(_worker_settle, [(inst.url, user_daily, day, ids, PROMPT, COMPL)
                                           for _ in range(4)])
        res = _collect(procs, q)
        settle_errors = [e for x in res for e in x["errors"]]
        daily_key = f"daily_tokens:{user_daily}:{day}"
        settled_key = f"daily_reservations:settled:{user_daily}:{day}"
        consumed = int(r.get(daily_key) or 0)
        settled_entries = int(r.hlen(settled_key))
        active_left = int(r.hlen(active_key))
        timings["daily_settle_s"] = round(time.monotonic() - t0, 3)
        report["daily_settlement"] = {
            "reservations": RESV, "actual_each": ACTUAL,
            "consumed": consumed, "expected": RESV * ACTUAL,
            "settled_entries": settled_entries, "active_left": active_left,
            "errors": settle_errors,
        }
        check(consumed == RESV * ACTUAL, "settlement_exactly_once",
              f"consumed {consumed} != expected {RESV * ACTUAL}")
        check(settled_entries == RESV and active_left == 0,
              "settlement_idempotent_ledger",
              f"settled {settled_entries} active-left {active_left}")
        check(not settle_errors, "settlement_no_errors", "; ".join(settle_errors[:3]))

        # ==================================================================
        # (3d) Duplicate reservation ID rejected across workers
        # ==================================================================
        t0 = time.monotonic()
        user_dup = f"prb3-dup-{run_id}"
        dup_rid = f"dup-rid-{run_id}"
        procs, q = _spawn(_worker_reserve, [(inst.url, user_dup, [dup_rid], 100) for _ in range(2)])
        res = _collect(procs, q)
        dup_success = sum(x["admitted"] for x in res)
        dup_reject = sum(x["duplicates"] for x in res)
        timings["daily_dup_s"] = round(time.monotonic() - t0, 3)
        report["duplicate_reservation_id"] = {"successes": dup_success, "rejections": dup_reject}
        check(dup_success == 1 and dup_reject == 1, "duplicate_reservation_id_rejected",
              f"successes {dup_success}, rejections {dup_reject}")

        # ==================================================================
        # (4) Fail-closed: kill Valkey mid-run -> ConnectionError, never admit
        # ==================================================================
        t0 = time.monotonic()
        user_fail = f"prb3-fail-{run_id}"
        qm_fail = QuotaManager(valkey_url=inst.url)
        lease = qm_fail.acquire_concurrency_slot(user_fail, timeout_seconds=30)
        qm_fail.release_concurrency_slot(lease)  # prove connectivity first

        inst.proc.kill()
        inst.proc.wait(timeout=10)

        outcomes = {}
        try:
            qm_fail.acquire_concurrency_slot(user_fail, timeout_seconds=30)
            outcomes["acquire"] = "ADMITTED"
        except ConnectionError:
            outcomes["acquire"] = "ConnectionError"
        except Exception as exc:
            outcomes["acquire"] = f"{type(exc).__name__}"
        try:
            qm_fail.check_and_record_rpm(user_fail)
            outcomes["rpm"] = "ADMITTED"
        except ConnectionError:
            outcomes["rpm"] = "ConnectionError"
        except Exception as exc:
            outcomes["rpm"] = f"{type(exc).__name__}"
        try:
            qm_fail.reserve_daily_token_budget(user_fail, f"fail-{run_id}", 100)
            outcomes["reserve"] = "ADMITTED"
        except ConnectionError:
            outcomes["reserve"] = "ConnectionError"
        except Exception as exc:
            outcomes["reserve"] = f"{type(exc).__name__}"
        timings["fail_closed_s"] = round(time.monotonic() - t0, 3)
        report["fail_closed"] = {"killed": True, "outcomes": outcomes}
        for op in ("acquire", "rpm", "reserve"):
            check(outcomes[op] == "ConnectionError", f"fail_closed_{op}",
                  f"{op} -> {outcomes[op]}")

    finally:
        inst.stop()

    report["timings"] = timings
    report["passed"] = not report["violations"]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, sort_keys=True))

    print("\n=== MULTIWORKER ADMISSION SUMMARY ===")
    print(json.dumps({k: v for k, v in report.items() if k != "violations"},
                     indent=2, default=str))
    if report["violations"]:
        print(f"FAIL: {len(report['violations'])} violations: "
              f"{[v['name'] for v in report['violations']]}")
        return 1
    print("PASS: all admission atomicity checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
