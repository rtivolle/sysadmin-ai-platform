"""
Tier 4 Recovery Test: VictoriaLogs Outbox Outage Buffering & Replay Worker.
Verifies that during collector downtime, audit events are atomically spooled to durable outbox.jsonl,
and upon collector restoration, the outbox drain worker flushes all buffered events with zero loss.
"""
import os
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread

from backend.services.agent_tools import audit
from backend.services.agent_tools.audit import log_audit_event

def test_outbox_fallback_on_collector_outage(monkeypatch, tmp_path):
    """
    Verify that when VictoriaLogs is unreachable, events are persisted
    to durable outbox.jsonl with destination == 'outbox'.
    """
    test_outbox = str(tmp_path / "outbox.jsonl")
    def offline(_event):
        raise OSError("collector offline")

    monkeypatch.setattr(audit, "_post_event", offline)
    monkeypatch.setattr("backend.services.agent_tools.audit.OUTBOX_PATH", test_outbox)

    # Ingest 5 audit events during simulated collector downtime
    for i in range(5):
        res = log_audit_event(
            user_id="sysadmin-01",
            session_id="sess-outbox-test",
            tool_name="search_log_stream",
            command=f"search_{i}",
            human_approved=False,
            exit_code=0,
            duration_ms=25 + i,
            tokens_prompt=100,
            tokens_completion=50
        )
        assert res["logged"] is True
        assert res["destination"] == "outbox"

    # Assert outbox file exists and contains exactly 5 valid JSON lines
    assert os.path.exists(test_outbox)
    with open(test_outbox, "r", encoding="utf-8") as f:
        lines = [json.loads(line) for line in f if line.strip()]

    assert len(lines) == 5
    for i, rec in enumerate(lines):
        assert rec["service"] == "dsh-agent"
        assert rec["user_id"] == "sysadmin-01"
        assert rec["command"] == f"search_{i}"
        assert "timestamp" in rec
        assert rec["tokens_prompt"] == 100

def test_outbox_drain_and_replay_worker(monkeypatch, tmp_path):
    """The production replay function checkpoints only accepted records."""
    test_outbox = str(tmp_path / "outbox_drain.jsonl")
    
    # Pre-populate outbox with 3 buffered events
    buffered_events = [
        {"timestamp": "2026-09-23T22:00:00Z", "service": "dsh-agent", "user_id": "sysadmin-01", "action": f"test_{i}"}
        for i in range(3)
    ]
    with open(test_outbox, "w", encoding="utf-8") as f:
        for ev in buffered_events:
            f.write(json.dumps(ev) + "\n")

    monkeypatch.setattr(audit, "OUTBOX_PATH", test_outbox)
    received_by_collector = []
    monkeypatch.setattr(audit, "_post_event", lambda event: received_by_collector.append(event) or True)
    assert audit.flush_outbox() == {"sent": 3, "pending": 0}
    assert received_by_collector == buffered_events
    assert os.path.getsize(test_outbox) == 0


def test_partial_replay_retains_failed_record_and_tail(monkeypatch, tmp_path):
    path = tmp_path / "outbox.jsonl"
    records = [{"event_id": str(i)} for i in range(3)]
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    monkeypatch.setattr(audit, "OUTBOX_PATH", str(path))
    received = []

    def send(record):
        if record["event_id"] == "1":
            return False
        received.append(record)
        return True

    monkeypatch.setattr(audit, "_post_event", send)
    assert audit.flush_outbox() == {"sent": 1, "pending": 2, "error": "collector rejected audit record"}
    assert [json.loads(line) for line in path.read_text().splitlines()] == records[1:]
    assert received == records[:1]

    monkeypatch.setattr(audit, "_post_event", lambda record: received.append(record) or True)
    assert audit.flush_outbox() == {"sent": 2, "pending": 0}
    assert received == records


def test_concurrent_fallback_writes_preserve_every_record(monkeypatch, tmp_path):
    monkeypatch.setattr(audit, "OUTBOX_PATH", str(tmp_path / "outbox.jsonl"))
    monkeypatch.setattr(audit, "_post_event", lambda _event: False)

    def write(index):
        return log_audit_event("operator", "session", "bash", command=str(index))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(write, range(40)))
    assert all(result == {"logged": True, "destination": "outbox"} for result in results)
    records = [json.loads(line) for line in (tmp_path / "outbox.jsonl").read_text().splitlines()]
    assert {record["command"] for record in records} == {str(i) for i in range(40)}
    assert len({record["event_id"] for record in records}) == 40


def test_append_during_replay_is_not_lost(monkeypatch, tmp_path):
    path = tmp_path / "outbox.jsonl"
    path.write_text(json.dumps({"event_id": "old"}) + "\n")
    monkeypatch.setattr(audit, "OUTBOX_PATH", str(path))
    replay_started = Event()
    release_replay = Event()

    def send(event):
        if event["event_id"] == "old":
            replay_started.set()
            assert release_replay.wait(timeout=5)
            return True
        return False

    monkeypatch.setattr(audit, "_post_event", send)
    replay = Thread(target=audit.flush_outbox)
    replay.start()
    assert replay_started.wait(timeout=5)
    with ThreadPoolExecutor(max_workers=1) as pool:
        appended = pool.submit(log_audit_event, "operator", "session", "bash", command="new")
        release_replay.set()
        assert appended.result(timeout=5)["destination"] == "outbox"
    replay.join(timeout=5)
    assert not replay.is_alive()
    assert [record["command"] for record in map(json.loads, path.read_text().splitlines())] == ["new"]
