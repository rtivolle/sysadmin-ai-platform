"""
Tier 4 Recovery Tests: audit anchor (PR-D2) — hash-chained, tamper-evident
sealing of the VictoriaLogs ``service:dsh-agent`` stream.

Covers: seal/verify round-trip, idempotent seal, fail-closed seal, and verify
naming the exact failing batch for added / removed / modified events, a broken
chain and a truncated / edited ledger.
"""
import json

import pytest

from backend.services.resilience.audit_anchor import (
    AuditAnchor,
    AnchorError,
    LedgerCorruption,
    check_ledger,
    main,
    read_ledger,
)

WINDOW = 3600
GRACE = 0


def make_event(event_id, action="run", user_id="u1", **extra):
    event = {
        "event_id": event_id,
        "service": "dsh-agent",
        "user_id": user_id,
        "action": action,
    }
    event.update(extra)
    return event


class FakeQuerier:
    """Injectable querier keyed by window start epoch."""

    def __init__(self, events=None):
        self.events = events or {}

    def __call__(self, start, end):
        return list(self.events.get(int(start), []))

    def set(self, start, events):
        self.events[int(start)] = list(events)


def make_anchor(tmp_path, querier, window=WINDOW, grace=GRACE):
    return AuditAnchor(
        anchor_dir=tmp_path / "anchor",
        querier=querier,
        window_seconds=window,
        grace_seconds=grace,
        allow_local=True,
    )


def ledger_path(tmp_path):
    return tmp_path / "anchor" / "audit_anchor_ledger.jsonl"


# ---------------------------------------------------------------------------
# Sealing and verification
# ---------------------------------------------------------------------------

def test_seal_and_verify_roundtrip(tmp_path):
    querier = FakeQuerier({3600: [make_event("a"), make_event("b")]})
    anchor = make_anchor(tmp_path, querier)
    result = anchor.seal(now=7200)
    assert result["sealed"] == 1
    assert result["records"][0]["count"] == 2

    report = anchor.verify()
    assert report["ok"] is True
    assert report["mode"] == "ledger+store"
    assert report["batches"] == 1
    assert report["checked_batches"] == 1
    assert report["problems"] == []
    assert len(report["tip_hash"]) == 64


def test_seal_is_idempotent(tmp_path):
    querier = FakeQuerier({3600: [make_event("a")]})
    anchor = make_anchor(tmp_path, querier)
    first = anchor.seal(now=7200)
    assert first["sealed"] == 1

    second = anchor.seal(now=7200)
    assert second["sealed"] == 0
    assert second["records"] == []
    # Ledger still has exactly one record.
    assert len(read_ledger(anchor.anchor_dir)) == 1


def test_seal_fail_closed_when_store_unreachable(tmp_path):
    class DownQuerier:
        def __call__(self, start, end):
            raise ConnectionError("VictoriaLogs unreachable")

    anchor = make_anchor(tmp_path, DownQuerier())
    with pytest.raises(ConnectionError):
        anchor.seal(now=7200)
    # Nothing was written to the ledger: fail closed.
    assert not ledger_path(tmp_path).exists()


def test_verify_names_batch_for_added_event(tmp_path):
    querier = FakeQuerier({3600: [make_event("a"), make_event("b")]})
    anchor = make_anchor(tmp_path, querier)
    anchor.seal(now=7200)

    querier.set(3600, [make_event("a"), make_event("b"), make_event("c")])
    report = anchor.verify()
    assert report["ok"] is False
    assert len(report["problems"]) == 1
    assert "batch 0" in report["problems"][0]
    assert "added" in report["problems"][0]
    assert "c" in report["problems"][0]


def test_verify_names_batch_for_removed_event(tmp_path):
    querier = FakeQuerier({3600: [make_event("a"), make_event("b")]})
    anchor = make_anchor(tmp_path, querier)
    anchor.seal(now=7200)

    querier.set(3600, [make_event("a")])
    report = anchor.verify()
    assert report["ok"] is False
    assert "batch 0" in report["problems"][0]
    assert "removed" in report["problems"][0]
    assert "b" in report["problems"][0]


def test_verify_names_batch_for_modified_event(tmp_path):
    querier = FakeQuerier({3600: [make_event("a", action="run")]})
    anchor = make_anchor(tmp_path, querier)
    anchor.seal(now=7200)

    querier.set(3600, [make_event("a", action="delete")])
    report = anchor.verify()
    assert report["ok"] is False
    assert "batch 0" in report["problems"][0]
    assert "modified" in report["problems"][0]
    assert "a" in report["problems"][0]


def test_verify_detects_broken_chain(tmp_path):
    querier = FakeQuerier({0: [make_event("a")], 3600: [make_event("b")]})
    anchor = make_anchor(tmp_path, querier)
    anchor.seal(now=7200, since=0)
    records = read_ledger(anchor.anchor_dir)
    assert len(records) == 2

    # Rewrite batch 1 to point at a wrong (but self-consistent) parent: its own
    # hash stays valid for its own fields, only the linkage to batch 0 breaks.
    from backend.services.resilience.audit_anchor import compute_batch_hash

    records[1]["prev_hash"] = "f" * 64
    records[1]["hash"] = compute_batch_hash(
        records[1]["prev_hash"],
        int(records[1]["window_start_epoch"]),
        int(records[1]["window_end_epoch"]),
        int(records[1]["count"]),
        records[1]["events_sha256"],
    )
    with open(ledger_path(tmp_path), "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, sort_keys=True) + "\n")

    report = anchor.verify(ledger_only=True)
    assert report["ok"] is False
    assert any("chain broken" in p and "batch 1" in p for p in report["problems"])


def test_verify_detects_edited_record(tmp_path):
    querier = FakeQuerier({3600: [make_event("a")]})
    anchor = make_anchor(tmp_path, querier)
    anchor.seal(now=7200)
    records = read_ledger(anchor.anchor_dir)
    records[0]["count"] = 999
    with open(ledger_path(tmp_path), "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, sort_keys=True) + "\n")

    report = anchor.verify(ledger_only=True)
    assert report["ok"] is False
    assert any("edited" in p for p in report["problems"])


def test_truncated_ledger_raises(tmp_path):
    querier = FakeQuerier({3600: [make_event("a")]})
    anchor = make_anchor(tmp_path, querier)
    anchor.seal(now=7200)
    # Truncate the ledger mid-record.
    with open(ledger_path(tmp_path), "a", encoding="utf-8") as f:
        f.write('{"index": 1, "window_start": "20')

    with pytest.raises(LedgerCorruption):
        read_ledger(anchor.anchor_dir)


def test_verify_tail_truncation_with_expected_tip(tmp_path):
    querier = FakeQuerier({0: [make_event("a")], 3600: [make_event("b")]})
    anchor = make_anchor(tmp_path, querier)
    anchor.seal(now=7200, since=0)
    records = read_ledger(anchor.anchor_dir)
    full_tip = records[-1]["hash"]

    # Drop the last record (tail truncation).
    with open(ledger_path(tmp_path), "w", encoding="utf-8") as f:
        f.write(json.dumps(records[0], sort_keys=True) + "\n")

    report = anchor.verify(ledger_only=True, expect_tip=full_tip)
    assert report["ok"] is False
    assert any("truncated" in p for p in report["problems"])


def test_check_ledger_rejects_inserted_record(tmp_path):
    querier = FakeQuerier({0: [make_event("a")], 3600: [make_event("b")]})
    anchor = make_anchor(tmp_path, querier)
    anchor.seal(now=7200, since=0)
    records = read_ledger(anchor.anchor_dir)
    # Insert a duplicate index record to simulate an inserted batch.
    records.insert(0, records[0])
    problems = check_ledger(records)
    assert any("index" in p and "inserted" in p for p in problems)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_verify_ledger_only_exit_codes(tmp_path):
    querier = FakeQuerier({3600: [make_event("a")]})
    anchor = make_anchor(tmp_path, querier)
    anchor.seal(now=7200)

    code = main(
        ["verify", "--ledger-only", "--anchor-dir", str(anchor.anchor_dir), "--allow-local"]
    )
    assert code == 0

    # Tamper the ledger and verify the CLI exits non-zero.
    records = read_ledger(anchor.anchor_dir)
    records[0]["count"] = 1234
    with open(ledger_path(tmp_path), "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, sort_keys=True) + "\n")
    code = main(
        ["verify", "--ledger-only", "--anchor-dir", str(anchor.anchor_dir), "--allow-local"]
    )
    assert code == 1


def test_cli_rejects_anchor_dir_inside_repo(tmp_path, monkeypatch):
    # Without --allow-local, a directory inside the repository is refused.
    from pathlib import Path

    from backend.services.resilience.audit_anchor import REPO_ROOT

    inner = REPO_ROOT / "anchor-in-repo"
    with pytest.raises(AnchorError):
        AuditAnchor(anchor_dir=inner, querier=FakeQuerier(), allow_local=False)
