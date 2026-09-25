"""
Stdlib-only local observability collector.

Scrapes platform health from *outside* the running services (no service code
changes): each service's loopback health endpoint, the VictoriaLogs audit
outbox, the newest backup, host disk/RAM/load/GPU, and Valkey reachability plus
stale lease counts (key names are read from ``quota_manager.py`` as a
reference; see the comments next to the constants).

Output is Prometheus text exposition; a compact JSON summary is also sent,
best-effort, to VictoriaLogs under the stream ``service:observability``.

Usage:
    python -m backend.services.observability.collector [--loop N]
        [--textfile PATH] [--serve 127.0.0.1:9464]

Guardrails respected here:
  * No secrets are read from ``backend/config/keys/*`` (only the ``VALKEY_URL``
    environment variable is parsed for host/port/password, and the password is
    never logged or emitted into any metric or summary).
  * The collector is read-only: it opens no files for writing except the
    optional ``--textfile`` destination, and never mutates platform state.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib import error as urlerror
from urllib import request as urlrequest

# ---------------------------------------------------------------------------
# Paths and defaults
# ---------------------------------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BACKEND_DIR / "data"
CONFIG_DIR = BACKEND_DIR / "config"
RUN_DIR = BACKEND_DIR / "run"

OUTBOX_PATH = DATA_DIR / "victorialogs" / "outbox.jsonl"
BACKUP_DIR = DATA_DIR / "backups"

VICTORIALOGS_URL = os.getenv("VICTORIALOGS_URL", "http://127.0.0.1:9428")
VALKEY_URL = os.getenv("VALKEY_URL", "redis://127.0.0.1:6379/0")

# Key names below mirror the constants embedded in
# backend/services/auth_gateway/quota_manager.py (LUA_ACQUIRE_LEASE /
# acquire_concurrency_slot). They are duplicated here so the collector stays
# stdlib-only and does not import the service module. Keep in sync if the
# quota manager key scheme changes.
VALKEY_CLUSTER_LEASES_KEY = "quota:leases:cluster"
VALKEY_USER_LEASES_PREFIX = "quota:leases:user:"

DEFAULT_AGENT_PORT = int(os.getenv("SYSADMIN_AGENT_PORT", "3080"))

# Per-service catalogue. ``probe`` selects the liveness check:
#   http    -> GET the loopback health endpoint (any 2xx/3xx = up)
#   tcp     -> TCP connect (SeaweedFS, Traefik)
#   redis   -> TCP connect + RESP PING (Valkey)
#   pidfile -> process liveness via backend/run/<name>.pid (audit_outbox worker)
SERVICES: List[Dict[str, Any]] = [
    {"name": "valkey", "probe": "redis", "host": "127.0.0.1", "port": 6379},
    {"name": "victorialogs", "probe": "http", "url": "http://127.0.0.1:9428/health"},
    {"name": "seaweedfs", "probe": "tcp", "host": "127.0.0.1", "port": 8333},
    {"name": "inference", "probe": "http", "url": "http://127.0.0.1:8000/health"},
    {"name": "auth_gateway", "probe": "http", "url": "http://127.0.0.1:3081/health"},
    {"name": "agent_tools", "probe": "http", "url": f"http://127.0.0.1:{DEFAULT_AGENT_PORT}/health"},
    {"name": "litellm", "probe": "http", "url": "http://127.0.0.1:4000/health/readiness"},
    {"name": "traefik", "probe": "tcp", "host": "127.0.0.1", "port": 8080},
    {"name": "harness_gateway", "probe": "http", "url": "http://127.0.0.1:3085/api/gateway/health"},
    {"name": "audit_outbox", "probe": "pidfile"},
]

# ---------------------------------------------------------------------------
# Minimal RESP (Valkey) client — stdlib socket only
# ---------------------------------------------------------------------------


class ValkeyError(Exception):
    pass


class _ValkeyClient:
    """Tiny RESP2 client sufficient for PING / SCAN / ZRANGEBYSCORE."""

    def __init__(self, host: str, port: int, password: Optional[str] = None, timeout: float = 2.0):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._fp: Any = None

    def connect(self) -> None:
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._fp = self._sock.makefile("rb")
        if self.password:
            try:
                self._command("AUTH", self.password)
            except ValkeyError:
                # Wrong/placeholder password: reachability still stands; the
                # lease scan below will simply be skipped.
                pass

    def close(self) -> None:
        try:
            if self._sock is not None:
                self._sock.close()
        finally:
            self._sock = None
            self._fp = None

    def _send(self, *args: Any) -> None:
        out = [f"*{len(args)}\r\n".encode()]
        for a in args:
            b = a if isinstance(a, bytes) else str(a).encode("utf-8")
            out.append(f"${len(b)}\r\n".encode())
            out.append(b)
            out.append(b"\r\n")
        self._sock.sendall(b"".join(out))

    def _read_line(self) -> bytes:
        line = self._fp.readline()
        if not line:
            raise ValkeyError("connection closed")
        return line

    def _read_reply(self) -> Any:
        line = self._read_line()
        prefix = line[:1]
        body = line[1:-2]  # strip trailing \r\n
        if prefix == b"+":
            return body.decode("utf-8")
        if prefix == b"-":
            raise ValkeyError(body.decode("utf-8", "replace"))
        if prefix == b":":
            return int(body)
        if prefix == b"$":
            length = int(body)
            if length == -1:
                return None
            data = self._fp.read(length)
            self._fp.read(2)  # trailing \r\n
            return data.decode("utf-8", "replace")
        if prefix == b"*":
            n = int(body)
            if n == -1:
                return None
            return [self._read_reply() for _ in range(n)]
        raise ValkeyError(f"unknown reply prefix {prefix!r}")

    def _command(self, *args: Any) -> Any:
        self._send(*args)
        return self._read_reply()

    def ping(self) -> str:
        return self._command("PING")

    def scan_iter(self, match: Optional[str] = None, count: int = 1000) -> List[str]:
        cursor = 0
        found: List[str] = []
        while True:
            args: List[str] = ["SCAN", str(cursor)]
            if match:
                args += ["MATCH", match]
            args += ["COUNT", str(count)]
            reply = self._command(*args)
            cursor = int(reply[0])
            found.extend(reply[1])
            if cursor == 0:
                break
        return found

    def zrangebyscore(self, key: str, min_score: str, max_score: str) -> List[str]:
        return self._command("ZRANGEBYSCORE", key, min_score, max_score)


def _parse_valkey_url(url: str) -> Tuple[str, int, Optional[str]]:
    """Return (host, port, password) from a ``redis://[user]:pass@host:port/db`` URL."""
    host, port, password = "127.0.0.1", 6379, None
    try:
        rest = url
        if "://" in rest:
            rest = rest.split("://", 1)[1]
        if "/" in rest:
            rest = rest.split("/", 1)[0]
        if "@" in rest:
            creds, rest = rest.rsplit("@", 1)
            if ":" in creds:
                password = creds.split(":", 1)[1]
        if ":" in rest:
            host, port_s = rest.rsplit(":", 1)
            port = int(port_s)
        else:
            host = rest
    except Exception:
        host, port, password = "127.0.0.1", 6379, None
    return host, port, (password or None)


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def _probe_http(url: str, timeout: float) -> bool:
    try:
        with urlrequest.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except Exception:
        return False


def _probe_tcp(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _probe_pidfile(name: str, run_dir: Path) -> bool:
    """Mirror platform.sh ``is_running``: read <run_dir>/<name>.pid and check liveness."""
    pid_file = run_dir / f"{name}.pid"
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _probe_valkey(
    host: str, port: int, password: Optional[str], now: float, timeout: float
) -> Dict[str, Any]:
    result: Dict[str, Any] = {"up": False, "stale_total": None, "stale_cluster": None, "stale_user": None}
    client = _ValkeyClient(host, port, password, timeout)
    try:
        client.connect()
        result["up"] = True
        now_str = str(now)
        stale_cluster = 0
        stale_user = 0
        try:
            stale_cluster = len(client.zrangebyscore(VALKEY_CLUSTER_LEASES_KEY, "-inf", now_str))
        except ValkeyError:
            pass
        try:
            for key in client.scan_iter(match=f"{VALKEY_USER_LEASES_PREFIX}*"):
                stale_user += len(client.zrangebyscore(key, "-inf", now_str))
        except ValkeyError:
            pass
        result["stale_cluster"] = stale_cluster
        result["stale_user"] = stale_user
        result["stale_total"] = stale_cluster + stale_user
    except (OSError, ValkeyError, ValueError):
        result["up"] = False
    finally:
        client.close()
    return result


# ---------------------------------------------------------------------------
# Filesystem / host fact helpers (pure, testable)
# ---------------------------------------------------------------------------


def _count_outbox_backlog(path: Path) -> Tuple[int, Optional[float]]:
    """Return (number_of_lines, oldest_age_seconds)."""
    if not path.exists():
        return 0, None
    count = 0
    oldest_ts: Optional[float] = None
    now = time.time()
    with open(path, "rb") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            count += 1
            try:
                obj = json.loads(line)
            except (ValueError, json.JSONDecodeError):
                continue
            ts = _parse_iso(obj.get("timestamp"))
            if ts is not None:
                if oldest_ts is None or ts < oldest_ts:
                    oldest_ts = ts
    age = (now - oldest_ts) if oldest_ts is not None else None
    return count, age


def _parse_iso(value: Any) -> Optional[float]:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _newest_backup_age(path: Path, now: float) -> Tuple[bool, Optional[float]]:
    """Return (present, age_seconds). Prefers ``*.manifest.json``, falls back to ``*.tar.gz``."""
    if not path.exists():
        return False, None
    candidates: List[float] = []
    for pattern in ("*.manifest.json", "*.tar.gz"):
        for f in path.glob(pattern):
            try:
                candidates.append(f.stat().st_mtime)
            except OSError:
                continue
        if candidates:
            break
    if not candidates:
        return False, None
    newest = max(candidates)
    return True, max(0.0, now - newest)


def _read_meminfo() -> Dict[str, int]:
    values: Dict[str, int] = {"MemTotal": 0, "MemAvailable": 0}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if ":" not in line:
                    continue
                key, val = [p.strip() for p in line.split(":", 1)]
                if key in values:
                    values[key] = int(val.split()[0]) * 1024  # kB -> bytes
    except OSError:
        pass
    return values


def _query_gpu() -> Tuple[bool, List[Dict[str, Any]]]:
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return False, []
    try:
        proc = subprocess.run(
            [
                nvidia_smi,
                "--query-gpu=index,memory.total,memory.used",
                "--format=csv,noheader,nounits",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0:
            return False, []
    except (OSError, subprocess.SubprocessError):
        return False, []
    gpus: List[Dict[str, Any]] = []
    for line in proc.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            total = int(float(parts[1]))
            used = int(float(parts[2]))
        except ValueError:
            continue
        gpus.append({"gpu": parts[0], "total_mb": total, "used_mb": used})
    return True, gpus


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

# metric_name -> (TYPE, HELP)
_METRIC_DOCS: Dict[str, Tuple[str, str]] = {
    "observability_up": ("gauge", "Collector liveness (always 1 when scraped)."),
    "observability_scrape_duration_seconds": ("gauge", "Seconds the last scrape took."),
    "observability_service_up": ("gauge", "1 when the service health probe succeeds, else 0."),
    "observability_outbox_backlog": ("gauge", "Number of pending audit events in the outbox."),
    "observability_outbox_oldest_age_seconds": ("gauge", "Age in seconds of the oldest pending outbox event."),
    "observability_backup_present": ("gauge", "1 when at least one backup archive/manifest exists."),
    "observability_backup_newest_age_seconds": ("gauge", "Age in seconds of the newest backup."),
    "observability_disk_total_bytes": ("gauge", "Filesystem total bytes hosting backend/data."),
    "observability_disk_used_bytes": ("gauge", "Filesystem used bytes hosting backend/data."),
    "observability_disk_used_percent": ("gauge", "Filesystem usage percent hosting backend/data."),
    "observability_ram_total_bytes": ("gauge", "Host total RAM bytes."),
    "observability_ram_available_bytes": ("gauge", "Host available RAM bytes."),
    "observability_ram_used_percent": ("gauge", "Host RAM usage percent."),
    "observability_load1": ("gauge", "1-minute load average."),
    "observability_load5": ("gauge", "5-minute load average."),
    "observability_load15": ("gauge", "15-minute load average."),
    "observability_gpu_present": ("gauge", "1 when nvidia-smi reported GPUs, else 0."),
    "observability_gpu_memory_total_mb": ("gauge", "GPU VRAM total (MiB)."),
    "observability_gpu_memory_used_mb": ("gauge", "GPU VRAM used (MiB)."),
    "observability_gpu_memory_used_percent": ("gauge", "GPU VRAM usage percent."),
    "observability_valkey_up": ("gauge", "1 when the Valkey TCP/PING probe succeeds."),
    "observability_stale_leases_total": ("gauge", "Total expired quota lease entries still resident in Valkey."),
    "observability_stale_leases": ("gauge", "Expired quota lease entries per scope (cluster|user)."),
}


def _sample(name: str, value: Optional[float], labels: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    return {"name": name, "labels": labels or {}, "value": value}


def collect_samples(
    services: Optional[List[Dict[str, Any]]] = None,
    data_dir: Optional[Path] = None,
    outbox_path: Optional[Path] = None,
    backup_dir: Optional[Path] = None,
    run_dir: Optional[Path] = None,
    valkey_url: Optional[str] = None,
    timeout: float = 2.0,
    now: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Collect every metric sample plus a compact summary dict.

    All I/O is injected via parameters (defaulting to the real paths) so the
    function is unit-testable with fake endpoints and tmp files.
    """
    now = now if now is not None else time.time()
    data_dir = Path(data_dir) if data_dir is not None else DATA_DIR
    outbox_path = Path(outbox_path) if outbox_path is not None else OUTBOX_PATH
    backup_dir = Path(backup_dir) if backup_dir is not None else BACKUP_DIR
    run_dir = Path(run_dir) if run_dir is not None else RUN_DIR
    services = services if services is not None else SERVICES

    start = time.time()
    samples: List[Dict[str, Any]] = []
    summary: Dict[str, Any] = {"service": "observability", "timestamp": _iso_now(now)}

    # --- service health ---
    for svc in services:
        name = svc["name"]
        probe = svc.get("probe", "http")
        up = False
        if probe == "http":
            up = _probe_http(svc["url"], timeout)
        elif probe == "tcp":
            up = _probe_tcp(svc["host"], svc["port"], timeout)
        elif probe == "redis":
            up = _probe_tcp(svc["host"], svc["port"], timeout)
        elif probe == "pidfile":
            up = _probe_pidfile(name, run_dir)
        samples.append(_sample("observability_service_up", 1.0 if up else 0.0, {"service": name}))
        summary[f"service_{name}_up"] = bool(up)

    # --- audit outbox ---
    backlog, oldest_age = _count_outbox_backlog(outbox_path)
    samples.append(_sample("observability_outbox_backlog", float(backlog)))
    summary["outbox_backlog"] = backlog
    if oldest_age is not None:
        samples.append(_sample("observability_outbox_oldest_age_seconds", oldest_age))
        summary["outbox_oldest_age_seconds"] = oldest_age

    # --- backup freshness ---
    backup_present, backup_age = _newest_backup_age(backup_dir, now)
    samples.append(_sample("observability_backup_present", 1.0 if backup_present else 0.0))
    summary["backup_present"] = backup_present
    if backup_age is not None:
        samples.append(_sample("observability_backup_newest_age_seconds", backup_age))
        summary["backup_newest_age_seconds"] = backup_age

    # --- disk usage (filesystem hosting backend/data) ---
    try:
        usage = shutil.disk_usage(data_dir)
        samples.append(_sample("observability_disk_total_bytes", float(usage.total)))
        samples.append(_sample("observability_disk_used_bytes", float(usage.used)))
        percent = (usage.used / usage.total * 100.0) if usage.total else 0.0
        samples.append(_sample("observability_disk_used_percent", percent))
        summary["disk_used_percent"] = round(percent, 2)
    except OSError:
        pass

    # --- RAM ---
    mem = _read_meminfo()
    if mem["MemTotal"] > 0:
        total = mem["MemTotal"]
        available = mem["MemAvailable"]
        used_percent = (total - available) / total * 100.0
        samples.append(_sample("observability_ram_total_bytes", float(total)))
        samples.append(_sample("observability_ram_available_bytes", float(available)))
        samples.append(_sample("observability_ram_used_percent", used_percent))
        summary["ram_used_percent"] = round(used_percent, 2)

    # --- load ---
    try:
        load1, load5, load15 = os.getloadavg()
        samples.append(_sample("observability_load1", load1))
        samples.append(_sample("observability_load5", load5))
        samples.append(_sample("observability_load15", load15))
        summary["load1"] = load1
    except (OSError, AttributeError):
        pass

    # --- GPU ---
    gpu_present, gpus = _query_gpu()
    samples.append(_sample("observability_gpu_present", 1.0 if gpu_present else 0.0))
    summary["gpu_present"] = gpu_present
    for g in gpus:
        labels = {"gpu": g["gpu"]}
        samples.append(_sample("observability_gpu_memory_total_mb", float(g["total_mb"]), labels))
        samples.append(_sample("observability_gpu_memory_used_mb", float(g["used_mb"]), labels))
        pct = (g["used_mb"] / g["total_mb"] * 100.0) if g["total_mb"] else 0.0
        samples.append(_sample("observability_gpu_memory_used_percent", pct, labels))
        summary[f"gpu_{g['gpu']}_mem_used_percent"] = round(pct, 2)

    # --- Valkey reachability + stale leases ---
    host, port, password = _parse_valkey_url(valkey_url or VALKEY_URL)
    valkey = _probe_valkey(host, port, password, now, timeout)
    samples.append(_sample("observability_valkey_up", 1.0 if valkey["up"] else 0.0))
    summary["valkey_up"] = valkey["up"]
    if valkey["stale_total"] is not None:
        samples.append(_sample("observability_stale_leases_total", float(valkey["stale_total"])))
        summary["stale_leases_total"] = valkey["stale_total"]
        samples.append(_sample("observability_stale_leases", float(valkey["stale_cluster"]), {"scope": "cluster"}))
        samples.append(_sample("observability_stale_leases", float(valkey["stale_user"]), {"scope": "user"}))

    # --- collector liveness + duration ---
    duration = time.time() - start
    samples.append(_sample("observability_up", 1.0))
    samples.append(_sample("observability_scrape_duration_seconds", duration))
    summary["scrape_duration_seconds"] = round(duration, 4)

    return samples, summary


def _iso_now(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_prometheus(samples: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    seen_docs: set = set()
    for name in sorted({s["name"] for s in samples}):
        type_, help_ = _METRIC_DOCS.get(name, ("untyped", name))
        if name not in seen_docs:
            lines.append(f"# HELP {name} {help_}")
            lines.append(f"# TYPE {name} {type_}")
            seen_docs.add(name)
    for s in samples:
        if s["value"] is None:
            continue
        name = s["name"]
        labels = s["labels"]
        if labels:
            lbl = ",".join(f'{k}="{_escape_label(v)}"' for k, v in sorted(labels.items()))
            lines.append(f"{name}{{{lbl}}} {_fmt_value(s['value'])}")
        else:
            lines.append(f"{name} {_fmt_value(s['value'])}")
    return "\n".join(lines) + "\n"


def _escape_label(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt_value(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return f"{value:.6f}"


def send_summary_victorialogs(summary: Dict[str, Any], url: str, timeout: float = 2.0) -> bool:
    """Best-effort JSON summary to VictoriaLogs stream ``service:observability``."""
    try:
        target = f"{url.rstrip('/')}/insert/jsonline?_stream_fields=service&_time_field=timestamp"
        payload = json.dumps(summary, ensure_ascii=False) + "\n"
        req = urlrequest.Request(
            target,
            data=payload.encode("utf-8"),
            headers={"Content-Type": "application/stream+json"},
            method="POST",
        )
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except Exception:
        return False


# ---------------------------------------------------------------------------
# CLI / server
# ---------------------------------------------------------------------------


def _render(samples: List[Dict[str, Any]]) -> str:
    return format_prometheus(samples)


def _make_handler(refresh):
    class _MetricsHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path in ("/metrics", "/health", "/"):
                try:
                    body = _render(refresh()[0]).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; version=0.0.4")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as exc:  # pragma: no cover - defensive
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(f"collector error: {exc}".encode())
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):  # noqa: A002
            pass

    return _MetricsHandler


def _run_serve(bind: str, collect_kwargs: Dict[str, Any], timeout: float) -> None:
    host, _, port_s = bind.rpartition(":")
    port = int(port_s)

    def refresh():
        return collect_samples(timeout=timeout, **collect_kwargs)

    server = ThreadingHTTPServer((host, port), _make_handler(refresh))
    print(f"observability collector serving /metrics on http://{host}:{port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Stdlib-only observability collector")
    parser.add_argument("--loop", type=int, default=0, help="repeat N times (default 0 = once)")
    parser.add_argument("--interval", type=float, default=30.0, help="seconds between loop iterations")
    parser.add_argument("--textfile", type=str, default=None, help="also write Prometheus text to this path")
    parser.add_argument("--serve", type=str, default=None, help="host:port to serve /metrics (e.g. 127.0.0.1:9464)")
    parser.add_argument("--timeout", type=float, default=2.0, help="per-probe timeout in seconds")
    parser.add_argument("--no-victorialogs", action="store_true", help="skip the VictoriaLogs summary send")
    args = parser.parse_args(argv)

    if args.serve:
        _run_serve(args.serve, {}, args.timeout)
        return 0

    iterations = max(0, args.loop) if args.loop > 0 else 1
    for i in range(iterations):
        samples, summary = collect_samples(timeout=args.timeout)
        text = format_prometheus(samples)
        sys.stdout.write(text)
        sys.stdout.flush()
        if args.textfile:
            tmp = f"{args.textfile}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, args.textfile)
        if not args.no_victorialogs:
            send_summary_victorialogs(summary, VICTORIALOGS_URL, args.timeout)
        if i < iterations - 1:
            time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
