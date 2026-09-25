"""Unit tests for the stdlib-only observability collector.

Covers the pure helpers (outbox/backup/RESP parsing), the Prometheus formatter,
a live HTTP service probe, and the "no secret leakage" guarantee.
"""

import io
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from backend.services.observability import collector
from backend.services.observability.collector import (
    VALKEY_CLUSTER_LEASES_KEY,
    _ValkeyClient,
    _count_outbox_backlog,
    _fmt_value,
    _newest_backup_age,
    _parse_valkey_url,
    _probe_http,
    _probe_tcp,
    collect_samples,
    format_prometheus,
)


# ---------------------------------------------------------------------------
# Pure filesystem helpers
# ---------------------------------------------------------------------------


def test_count_outbox_backlog_empty(tmp_path):
    outbox = tmp_path / "outbox.jsonl"
    outbox.write_text("", encoding="utf-8")
    count, age = _count_outbox_backlog(outbox)
    assert count == 0
    assert age is None


def test_count_outbox_backlog_parses_oldest_age(tmp_path):
    outbox = tmp_path / "outbox.jsonl"
    now = time.time()
    old_ts = now - 7200  # two hours ago
    recent_ts = now - 60
    lines = [
        json.dumps({"event_id": "a", "timestamp": _iso(old_ts)}),
        json.dumps({"event_id": "b", "timestamp": _iso(recent_ts)}),
        "not valid json\n",
        "",
    ]
    outbox.write_text("\n".join(lines) + "\n", encoding="utf-8")
    count, age = _count_outbox_backlog(outbox)
    assert count == 3  # two JSON events + the malformed line; blank lines skipped
    assert age is not None
    assert 7100 <= age <= 7300


def _iso(epoch):
    import datetime
    return datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_newest_backup_age_prefers_manifest(tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    now = time.time()
    manifest = backups / "backup_old.manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    old_mtime = now - 100000  # > 26h
    os.utime(manifest, (old_mtime, old_mtime))
    tar = backups / "backup_recent.tar.gz"
    tar.write_bytes(b"x")
    os.utime(tar, (now, now))

    present, age = _newest_backup_age(backups, now)
    assert present is True
    # manifest is preferred, even though the tarball is newer
    assert 99900 <= age <= 100100


def test_newest_backup_age_missing(tmp_path):
    present, age = _newest_backup_age(tmp_path / "nope", time.time())
    assert present is False
    assert age is None


# ---------------------------------------------------------------------------
# Valkey URL + RESP client
# ---------------------------------------------------------------------------


def test_parse_valkey_url_password_not_logged():
    host, port, pw = _parse_valkey_url("redis://:s3cr3t-pass@10.0.0.5:6380/2")
    assert (host, port) == ("10.0.0.5", 6380)
    assert pw == "s3cr3t-pass"


def test_parse_valkey_url_plain():
    host, port, pw = _parse_valkey_url("redis://127.0.0.1:6379/0")
    assert (host, port) == ("127.0.0.1", 6379)
    assert pw is None


def test_resp_reply_parsing():
    client = _ValkeyClient("127.0.0.1", 6379)
    # +PONG\r\n
    client._fp = io.BytesIO(b"+PONG\r\n")
    assert client._read_reply() == "PONG"

    # *3\r\n$1\r\na\r\n$1\r\nb\r\n$1\r\nc\r\n
    client._fp = io.BytesIO(b"*3\r\n$1\r\na\r\n$1\r\nb\r\n$1\r\nc\r\n")
    assert client._read_reply() == ["a", "b", "c"]

    # -ERR wrong\r\n
    client._fp = io.BytesIO(b"-ERR wrong\r\n")
    with pytest.raises(Exception):
        client._read_reply()

    # :42\r\n
    client._fp = io.BytesIO(b":42\r\n")
    assert client._read_reply() == 42


# ---------------------------------------------------------------------------
# Live HTTP probe + tcp probe
# ---------------------------------------------------------------------------


class _HealthHandler(BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):  # noqa: N802
        body = b"ok"
        self.send_response(self.status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: A002
        pass


def _serve(handler_cls):
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def test_probe_http_up_and_down():
    server, port = _serve(_HealthHandler)
    try:
        assert _probe_http(f"http://127.0.0.1:{port}/health", 2.0) is True
    finally:
        server.shutdown()

    # Nothing listens on a random ephemeral port we just closed.
    assert _probe_http(f"http://127.0.0.1:{port}/health", 0.5) is False


def test_probe_tcp_closed_port():
    sock = __import__("socket").socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    assert _probe_tcp("127.0.0.1", port, 0.5) is False


def test_probe_pidfile(tmp_path):
    from backend.services.observability.collector import _probe_pidfile

    # No pid file.
    assert _probe_pidfile("audit_outbox", tmp_path) is False
    # A pid that is not running.
    (tmp_path / "audit_outbox.pid").write_text("99999999", encoding="utf-8")
    assert _probe_pidfile("audit_outbox", tmp_path) is False
    # Our own live pid.
    (tmp_path / "audit_outbox.pid").write_text(str(os.getpid()), encoding="utf-8")
    assert _probe_pidfile("audit_outbox", tmp_path) is True


# ---------------------------------------------------------------------------
# collect_samples with fake endpoints / tmp files
# ---------------------------------------------------------------------------


def test_collect_samples_service_up(tmp_path):
    server, port = _serve(_HealthHandler)
    try:
        services = [{"name": "fake_http", "probe": "http", "url": f"http://127.0.0.1:{port}/health"}]
        samples, summary = collect_samples(
            services=services,
            data_dir=tmp_path,
            outbox_path=tmp_path / "missing.jsonl",
            backup_dir=tmp_path / "missing_backups",
            valkey_url="redis://127.0.0.1:1/0",  # unreachable -> valkey_up 0
            timeout=1.0,
        )
    finally:
        server.shutdown()

    by_name = {s["name"]: s for s in samples}
    assert by_name["observability_service_up"]["value"] == 1.0
    assert by_name["observability_service_up"]["labels"] == {"service": "fake_http"}
    assert by_name["observability_valkey_up"]["value"] == 0.0
    assert summary["service_fake_http_up"] is True


def test_no_secret_leakage(tmp_path):
    """A secret placed on the filesystem must never appear in collector output."""
    secret = "SUPER-SECRET-9f3a1b2c"
    outbox = tmp_path / "outbox.jsonl"
    outbox.write_text(json.dumps({"event_id": "x", "timestamp": _iso(time.time()), "secret": secret}) + "\n", encoding="utf-8")
    # Also put the secret in a fake key file the collector never reads.
    (tmp_path / "master.key").write_text(secret, encoding="utf-8")

    samples, summary = collect_samples(
        services=[],
        data_dir=tmp_path,
        outbox_path=outbox,
        backup_dir=tmp_path / "missing",
        valkey_url=f"redis://:{secret}@127.0.0.1:1/0",
        timeout=0.5,
    )
    text = format_prometheus(samples)
    assert secret not in text
    assert secret not in json.dumps(summary)
    # The metric set must still be present (outbox backlog counted).
    assert "observability_outbox_backlog" in text


def test_format_prometheus_shape():
    samples = [
        {"name": "observability_service_up", "labels": {"service": "valkey"}, "value": 1.0},
        {"name": "observability_service_up", "labels": {"service": "traefik"}, "value": 0.0},
        {"name": "observability_load1", "labels": {}, "value": 0.55},
    ]
    text = format_prometheus(samples)
    assert 'observability_service_up{service="valkey"} 1' in text
    assert 'observability_service_up{service="traefik"} 0' in text
    assert "observability_load1 0.550000" in text
    assert "# HELP observability_service_up" in text
    assert "# TYPE observability_service_up gauge" in text


def test_fmt_value_integer():
    assert _fmt_value(1.0) == "1"
    assert _fmt_value(1.5) == "1.500000"
