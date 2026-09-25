"""Chained, batched integrity anchor for the platform audit stream (PR-D2).

``agent_tools/audit.py`` delivers audit events to VictoriaLogs at least once;
a host compromise or a privileged attacker could edit or delete stored
history. This module seals fixed time windows of the ``service:dsh-agent``
stream into a hash-chained JSONL ledger that is meant to live on independently
controlled storage (an off-host mount with a WORM/append-only policy and
separate credentials):

    batch_hash = sha256(prev_hash || window || count || sha256(canonical events))

Each ledger record carries ``index``, the sealed ``window`` bounds, the event
``count``, ``events_sha256``, ``prev_hash`` and the resulting ``hash``.
``verify`` re-queries every sealed window, recomputes the chain and names the
exact failing batch and reason (event added / removed / modified, chain
broken, ledger truncated or edited).

Honest property: the ledger is tamper-EVIDENT, not immutable. Any interior
edit breaks the chain, but an attacker who can rewrite the *whole* ledger and
the store can recompute a self-consistent one. Immutability depends entirely
on the anchor storage policy: put ``AUDIT_ANCHOR_DIR`` on a WORM/append-only
filesystem with separate credentials (``chattr +a`` plus a dedicated host
account is the cheap local approximation). Tail truncation of the ledger is
only detectable against an externally recorded tip hash — record the tip
printed by ``verify`` in independent monitoring, or pass ``--expect-tip``.
"""
import argparse
import fcntl
import hashlib
import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import httpx

logger = logging.getLogger("resilience.audit_anchor")

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
REPO_ROOT = BACKEND_DIR.parent

VICTORIALOGS_URL = os.getenv("VICTORIALOGS_URL", "http://127.0.0.1:9428")
DEFAULT_WINDOW_SECONDS = 3600
DEFAULT_GRACE_SECONDS = 900
QUERY_LIMIT = 1000000
LEDGER_FILENAME = "audit_anchor_ledger.jsonl"
LOCK_FILENAME = "audit_anchor.lock"
GENESIS_PREV_HASH = "0" * 64
# LogsQL exact-match filter. The value MUST be quoted: the bare ``dsh-agent``
# token would otherwise be parsed as ``dsh - agent`` (a subtraction of two
# stream-field selectors) and match nothing.
QUERY = 'service:="dsh-agent"'

# Fields injected by VictoriaLogs on query results. The platform audit schema
# (agent_tools/audit.py) never emits underscore-prefixed keys, so dropping this
# fixed set is lossless and keeps the canonical form stable across re-queries.
VL_INTERNAL_FIELDS = frozenset({"_msg", "_stream", "_stream_id", "_time"})


class AnchorError(RuntimeError):
    """Operational anchor failure (bad configuration, missing ledger, ...)."""


class LedgerCorruption(AnchorError):
    """The ledger file is truncated mid-record or otherwise unparsable."""


# ---------------------------------------------------------------------------
# Hashing and canonicalization
# ---------------------------------------------------------------------------

def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonicalize_event(event: Dict[str, Any]) -> str:
    """Stable canonical form: VictoriaLogs-internal fields dropped, keys sorted.

    ``json.dumps(sort_keys=True)`` sorts nested objects recursively, so the
    output is byte-identical across re-queries of the same logical event.
    """
    cleaned = {k: v for k, v in event.items() if k not in VL_INTERNAL_FIELDS}
    return json.dumps(cleaned, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def event_digest(canonical: str) -> str:
    return _sha256_hex(canonical.encode("utf-8"))


def compute_events_sha256(digests: Sequence[str]) -> str:
    """Aggregate hash over the ordered per-event digests (empty list -> sha256 of nothing)."""
    h = hashlib.sha256()
    for digest in digests:
        h.update(digest.encode("ascii"))
    return h.hexdigest()


def compute_batch_hash(
    prev_hash: str,
    window_start_epoch: int,
    window_end_epoch: int,
    count: int,
    events_sha256: str,
) -> str:
    """sha256(prev_hash || window || count || events_sha256); pipe-separated fixed-format fields."""
    material = f"{prev_hash}|{window_start_epoch}-{window_end_epoch}|{count}|{events_sha256}"
    return _sha256_hex(material.encode("utf-8"))


def prepare_events(raw_events: Sequence[Dict[str, Any]]) -> List[Tuple[str, str]]:
    """Canonicalize, dedupe by ``event_id`` (delivery is at-least-once) and sort.

    Returns a list of ``(event_id, digest)`` sorted by ``event_id``. Raises
    ``ValueError`` for malformed events or for two different payloads sharing
    one ``event_id`` — a window in that state is refused, never sealed.
    """
    by_id: Dict[str, str] = {}
    for raw in raw_events:
        if not isinstance(raw, dict):
            raise ValueError("audit query returned a non-object event")
        canonical = canonicalize_event(raw)
        event_id = json.loads(canonical).get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("audit event without an event_id in query result")
        digest = event_digest(canonical)
        if event_id in by_id and by_id[event_id] != digest:
            raise ValueError(f"conflicting contents for event_id {event_id}; refusing to seal")
        by_id[event_id] = digest
    return sorted(by_id.items())


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# VictoriaLogs querier (injectable for tests)
# ---------------------------------------------------------------------------

# Querier signature: (window_start_epoch, window_end_epoch) -> raw event dicts.
Querier = Callable[[float, float], List[Dict[str, Any]]]


class VictoriaLogsQuerier:
    """Queries ``/select/logsql/query`` for the ``service:dsh-agent`` stream."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: float = 10.0,
        limit: int = QUERY_LIMIT,
        client: Optional[httpx.Client] = None,
    ):
        self.base_url = (base_url or VICTORIALOGS_URL).rstrip("/")
        self.timeout = timeout
        self.limit = limit
        self._client = client

    def __call__(self, start_epoch: float, end_epoch: float) -> List[Dict[str, Any]]:
        params = {
            "query": QUERY,
            "start": _iso(start_epoch),
            "end": _iso(end_epoch),
            "limit": str(self.limit),
        }
        url = f"{self.base_url}/select/logsql/query"
        if self._client is not None:
            response = self._client.get(url, params=params)
        else:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.get(url, params=params)
        if response.status_code != 200:
            raise ConnectionError(
                f"VictoriaLogs query failed with HTTP {response.status_code}: {response.text[:200]}"
            )
        events: List[Dict[str, Any]] = []
        for line in response.text.splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
        if len(events) >= self.limit:
            raise AnchorError(
                f"window [{_iso(start_epoch)}..{_iso(end_epoch)}] returned {len(events)} events, "
                f"hitting the query limit; refusing to seal a possibly truncated batch"
            )
        return events


# ---------------------------------------------------------------------------
# Ledger I/O
# ---------------------------------------------------------------------------

def _ledger_path(anchor_dir: Path) -> Path:
    return anchor_dir / LEDGER_FILENAME


def _sidecar_path(anchor_dir: Path, index: int) -> Path:
    return anchor_dir / f"batch-{index:06d}.events"


def read_ledger(anchor_dir: Path) -> List[Dict[str, Any]]:
    """Read every ledger record; raises LedgerCorruption on a truncated/corrupt line."""
    path = _ledger_path(Path(anchor_dir))
    records: List[Dict[str, Any]] = []
    if not path.exists():
        return records
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LedgerCorruption(
                    f"ledger truncated or corrupted at line {lineno}: {exc}"
                ) from exc
            if not isinstance(record, dict):
                raise LedgerCorruption(f"ledger line {lineno} is not a JSON object")
            records.append(record)
    return records


def _write_bytes_append(path: Path, data: bytes) -> None:
    """0600 O_APPEND write with fsync, mirroring the audit outbox contract."""
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written == 0:
                raise OSError(f"write to {path} made no progress")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def check_ledger(records: Sequence[Dict[str, Any]]) -> List[str]:
    """Validate index contiguity, per-record hash and chain linkage.

    Returns a list of human-readable problems (empty means the ledger is
    internally consistent). Stops at the first structural break to avoid
    cascaded noise.
    """
    problems: List[str] = []
    prev_hash = GENESIS_PREV_HASH
    prev_window_end: Optional[int] = None
    for position, record in enumerate(records):
        index = record.get("index")
        if index != position:
            problems.append(
                f"ledger line {position + 1}: record index {index!r}, expected {position} "
                f"(record inserted, removed or reordered)"
            )
            break
        try:
            recomputed = compute_batch_hash(
                record["prev_hash"],
                int(record["window_start_epoch"]),
                int(record["window_end_epoch"]),
                int(record["count"]),
                record["events_sha256"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            problems.append(f"batch {index}: ledger record malformed or edited ({exc})")
            break
        if recomputed != record.get("hash"):
            problems.append(
                f"batch {index} (window {record.get('window_start')}..{record.get('window_end')}): "
                f"ledger record edited — stored hash does not match recomputed fields"
            )
            break
        if record.get("prev_hash") != prev_hash:
            problems.append(
                f"batch {index} (window {record.get('window_start')}..{record.get('window_end')}): "
                f"chain broken — prev_hash does not match the previous record's hash"
            )
            break
        if prev_window_end is not None and int(record["window_start_epoch"]) != prev_window_end:
            problems.append(
                f"batch {index}: window gap or overlap — window_start_epoch "
                f"{record['window_start_epoch']} != previous window_end_epoch {prev_window_end}"
            )
            break
        prev_hash = record["hash"]
        prev_window_end = int(record["window_end_epoch"])
    return problems


# ---------------------------------------------------------------------------
# AuditAnchor
# ---------------------------------------------------------------------------

class AuditAnchor:
    """Seals audit windows into the chained ledger and verifies them."""

    def __init__(
        self,
        anchor_dir: Optional[Path] = None,
        victorialogs_url: Optional[str] = None,
        querier: Optional[Querier] = None,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        grace_seconds: int = DEFAULT_GRACE_SECONDS,
        allow_local: bool = False,
        repo_root: Optional[Path] = None,
    ):
        raw_dir = anchor_dir or os.getenv("AUDIT_ANCHOR_DIR")
        if not raw_dir:
            raise AnchorError(
                "anchor directory required: pass anchor_dir or set AUDIT_ANCHOR_DIR "
                "(an independently controlled, ideally off-host, mount)"
            )
        self.anchor_dir = Path(raw_dir).resolve()
        self.repo_root = Path(repo_root).resolve() if repo_root else REPO_ROOT
        if not allow_local and self._is_within(self.anchor_dir, self.repo_root):
            raise AnchorError(
                f"anchor directory {self.anchor_dir} is inside the repository "
                f"({self.repo_root}); the anchor must live on independently controlled "
                f"storage. --allow-local exists only for tests."
            )
        self.window_seconds = int(window_seconds)
        self.grace_seconds = int(grace_seconds)
        if self.window_seconds <= 0 or self.grace_seconds < 0:
            raise AnchorError("window_seconds must be positive and grace_seconds non-negative")
        if querier is not None:
            self.querier = querier
        else:
            self.querier = VictoriaLogsQuerier(victorialogs_url)

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            return os.path.commonpath([str(path), str(root)]) == str(root)
        except ValueError:
            return False

    # -- window arithmetic -------------------------------------------------

    def _align_floor(self, epoch: float) -> int:
        return int(epoch // self.window_seconds) * self.window_seconds

    def _last_complete_window_start(self, now: float) -> Optional[int]:
        """Newest window whose end + grace delay is fully in the past."""
        latest_start = self._align_floor(now - self.grace_seconds - self.window_seconds)
        if latest_start < 0 or latest_start + self.window_seconds + self.grace_seconds > now:
            return None
        return latest_start

    # -- sealing -------------------------------------------------------------

    def seal(self, now: Optional[float] = None, since: Optional[float] = None) -> Dict[str, Any]:
        """Seal every complete, not-yet-sealed window. Idempotent.

        Fail closed: the store is queried before anything is written, so an
        unreachable VictoriaLogs leaves the ledger untouched and raises.
        """
        now = time.time() if now is None else float(now)
        self.anchor_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.anchor_dir / LOCK_FILENAME
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(lock_fd, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            records = read_ledger(self.anchor_dir)
            problems = check_ledger(records)
            if problems:
                raise AnchorError(
                    f"refusing to extend a ledger that fails verification: {problems[0]}"
                )
            tip = records[-1] if records else None
            last_complete = self._last_complete_window_start(now)
            if last_complete is None:
                return {"sealed": 0, "records": [], "reason": "no window fully in the past yet"}
            if tip is not None:
                start = int(tip["window_end_epoch"])
            elif since is not None:
                start = self._align_floor(float(since))
            else:
                start = last_complete
            sealed: List[Dict[str, Any]] = []
            prev_hash = tip["hash"] if tip else GENESIS_PREV_HASH
            index = int(tip["index"]) + 1 if tip else 0
            window = start
            while window <= last_complete:
                record = self._seal_window(window, index, prev_hash)
                sealed.append(record)
                prev_hash = record["hash"]
                index += 1
                window += self.window_seconds
            return {"sealed": len(sealed), "records": sealed}
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def _seal_window(self, window_start: int, index: int, prev_hash: str) -> Dict[str, Any]:
        window_end = window_start + self.window_seconds
        # Query first: any store failure aborts before a single byte is written.
        raw_events = self.querier(window_start, window_end)
        events = prepare_events(raw_events)
        digests = [digest for _, digest in events]
        events_sha256 = compute_events_sha256(digests)
        record = {
            "index": index,
            "window_start": _iso(window_start),
            "window_end": _iso(window_end),
            "window_start_epoch": window_start,
            "window_end_epoch": window_end,
            "count": len(events),
            "events_sha256": events_sha256,
            "prev_hash": prev_hash,
            "hash": compute_batch_hash(prev_hash, window_start, window_end, len(events), events_sha256),
        }
        # Diagnostic sidecar (one "digest  event_id" line per event, sorted by
        # event_id) lets verify name the exact added/removed/modified events.
        # Its integrity is anchored: events_sha256 is derived from these digests.
        sidecar = "".join(f"{digest}  {event_id}\n" for event_id, digest in events)
        _write_bytes_atomic(_sidecar_path(self.anchor_dir, index), sidecar.encode("utf-8"))
        _write_bytes_append(
            _ledger_path(self.anchor_dir),
            (json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"),
        )
        return record

    # -- verification --------------------------------------------------------

    def verify(
        self,
        ledger_only: bool = False,
        expect_tip: Optional[str] = None,
        expect_min_index: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Re-check the ledger (and, unless ledger_only, re-query every window).

        Returns a report with ``ok``, ``problems`` (each naming the exact batch
        index/window and reason) and the current ``tip_hash`` for external
        notarization.
        """
        records = read_ledger(self.anchor_dir)
        if not records:
            raise AnchorError(
                f"ledger {_ledger_path(self.anchor_dir)} missing or empty — "
                f"nothing has been sealed, or the ledger was deleted"
            )
        problems = check_ledger(records)
        if expect_tip is not None and records[-1].get("hash") != expect_tip:
            problems.append(
                f"ledger tip hash {records[-1].get('hash')} != expected {expect_tip} "
                f"(tail truncated or edited)"
            )
        if expect_min_index is not None and int(records[-1].get("index", -1)) < expect_min_index:
            problems.append(
                f"ledger ends at index {records[-1].get('index')}, expected at least "
                f"{expect_min_index} (ledger truncated)"
            )
        checked_batches = 0
        if not ledger_only and not problems:
            for record in records:
                problems.extend(self._verify_batch(record))
                checked_batches += 1
        return {
            "ok": not problems,
            "mode": "ledger" if ledger_only else "ledger+store",
            "batches": len(records),
            "checked_batches": checked_batches if not ledger_only else 0,
            "tip_hash": records[-1].get("hash"),
            "problems": problems,
        }

    def _verify_batch(self, record: Dict[str, Any]) -> List[str]:
        window_label = f"{record.get('window_start')}..{record.get('window_end')}"
        raw_events = self.querier(int(record["window_start_epoch"]), int(record["window_end_epoch"]))
        events = prepare_events(raw_events)
        digests = [digest for _, digest in events]
        events_sha256 = compute_events_sha256(digests)
        if events_sha256 == record["events_sha256"] and len(events) == int(record["count"]):
            return []
        reason = self._classify_diff(record, events)
        return [f"batch {record['index']} (window {window_label}): {reason}"]

    def _classify_diff(self, record: Dict[str, Any], current: List[Tuple[str, str]]) -> str:
        """Name the added/removed/modified event_ids using the sealed sidecar."""
        sidecar = _sidecar_path(self.anchor_dir, int(record["index"]))
        sealed: Dict[str, str] = {}
        sidecar_valid = False
        if sidecar.exists():
            try:
                for line in sidecar.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    digest, _, event_id = line.partition("  ")
                    sealed[event_id.strip()] = digest.strip()
                ordered = [sealed[k] for k in sorted(sealed)]
                sidecar_valid = compute_events_sha256(ordered) == record["events_sha256"]
            except OSError:
                sidecar_valid = False
        if not sidecar_valid:
            return (
                f"batch content mismatch (event-level diagnosis unavailable: sidecar "
                f"{sidecar.name} missing or tampered)"
            )
        now_by_id = dict(current)
        added = sorted(set(now_by_id) - set(sealed))
        removed = sorted(set(sealed) - set(now_by_id))
        modified = sorted(k for k in set(sealed) & set(now_by_id) if sealed[k] != now_by_id[k])
        parts = []
        if removed:
            parts.append(f"{len(removed)} event(s) removed: {removed}")
        if added:
            parts.append(f"{len(added)} event(s) added: {added}")
        if modified:
            parts.append(f"{len(modified)} event(s) modified: {modified}")
        detail = "; ".join(parts) if parts else "count/content mismatch"
        return f"batch content mismatch — {detail}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_since(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        pass
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--anchor-dir", help="ledger directory (default: AUDIT_ANCHOR_DIR)")
    parser.add_argument("--victorialogs-url", help=f"default: {VICTORIALOGS_URL}")
    parser.add_argument(
        "--allow-local",
        action="store_true",
        help="permit an anchor directory inside the repository (tests only)",
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="audit_anchor",
        description="Seal and verify hash-chained audit log integrity anchors (tamper-evident).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    seal_p = sub.add_parser("seal", help="seal complete audit windows into the ledger")
    _add_common_args(seal_p)
    seal_p.add_argument("--window-seconds", type=int, default=DEFAULT_WINDOW_SECONDS)
    seal_p.add_argument("--grace-seconds", type=int, default=DEFAULT_GRACE_SECONDS)
    seal_p.add_argument(
        "--since",
        help="backfill start (epoch seconds or ISO 8601); default: newest complete window "
        "or, once seeded, the ledger tip",
    )

    verify_p = sub.add_parser("verify", help="re-query sealed windows and check the chain")
    _add_common_args(verify_p)
    verify_p.add_argument("--window-seconds", type=int, default=DEFAULT_WINDOW_SECONDS)
    verify_p.add_argument("--ledger-only", action="store_true", help="skip re-querying the store")
    verify_p.add_argument("--expect-tip", help="expected tip hash (externally notarized)")
    verify_p.add_argument("--expect-min-index", type=int, help="lowest acceptable last index")

    args = parser.parse_args(argv)
    try:
        anchor = AuditAnchor(
            anchor_dir=args.anchor_dir,
            victorialogs_url=args.victorialogs_url,
            window_seconds=args.window_seconds,
            grace_seconds=getattr(args, "grace_seconds", DEFAULT_GRACE_SECONDS),
            allow_local=args.allow_local,
        )
        if args.command == "seal":
            result = anchor.seal(since=_parse_since(args.since) if args.since else None)
            for record in result["records"]:
                print(
                    f"sealed batch {record['index']}: window {record['window_start']}.."
                    f"{record['window_end']} count={record['count']} hash={record['hash']}"
                )
            if not result["records"]:
                print(f"nothing to seal ({result.get('reason', 'windows already sealed')})")
            return 0
        report = anchor.verify(
            ledger_only=args.ledger_only,
            expect_tip=args.expect_tip,
            expect_min_index=args.expect_min_index,
        )
        for problem in report["problems"]:
            print(f"FAIL: {problem}")
        if report["ok"]:
            print(
                f"OK: {report['batches']} batch(es) verified ({report['mode']}); "
                f"tip={report['tip_hash']}"
            )
            return 0
        print(f"verification FAILED with {len(report['problems'])} problem(s)", file=sys.stderr)
        return 1
    except (AnchorError, ConnectionError, httpx.HTTPError, OSError, ValueError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
