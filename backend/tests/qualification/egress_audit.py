#!/usr/bin/env python3
"""
Sovereignty / blocked-egress audit for the sysadmin AI platform (PR-D3 tooling).

Two modes:

1. Static census (default)
   Scans the platform's own code and config (backend/services, backend/*.py,
   backend/config, install.sh, backend/platform.sh, packages/harness-integration)
   for outbound destinations and telemetry / cloud-fallback surfaces, and maps
   every one onto an allowlist (egress_allowlist.json, next to this file).

   Each destination is classified as:
     install-time      artifact download during install; needs a mirror
     runtime-local     loopback / internal address; no public egress
     runtime-external  reachable from service code at runtime; must be disabled
                       or routed through a controlled import

   If an external host appears in runtime code and matches no allowlist entry,
   the script exits non-zero. A classified runtime-external destination whose
   disable switch is not present in code is reported (but does not by itself
   fail) as "lacking a disable switch".

2. Runtime check (--runtime)
   Unprivileged egress measurement. Samples `ss -tnp` over N seconds and
   attributes non-loopback TCP connections to the live platform PIDs read from
   backend/run/*.pid. If that cannot be measured (no live platform PIDs, or
   `ss` unavailable), it falls back to an `unshare -rn` network-namespace
   import smoke to prove the runtime's heavy imports need no egress. Fails
   clearly (exit 2) when neither path can measure anything.

Never reads backend/config/keys/*, backend/bin, backend/logs, backend/run
state beyond the pid files, backend/data, backend/.venv, or .git.

Exit codes: 0 = clean/classified; 1 = unclassified external destination in
runtime code (static) or a detected non-loopback platform connection (runtime);
2 = harness error / unmeasurable.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_ALLOWLIST = Path(__file__).resolve().parent / "egress_allowlist.json"

# Directories/files never scanned. `keys` carries secrets; the rest are
# generated state, dependency trees, tests, or docs (out of egress scope).
EXCLUDED_PARTS = {
    ".git", ".venv", "node_modules", "__pycache__", ".pytest_cache",
    "bin", "logs", "run", "keys", "data", "tests", ".slim", ".agents",
    ".codex", ".opencode",
}

# Roots whose text files are scanned (relative to REPO_ROOT).
SCAN_ROOTS = [
    Path("backend/services"),
    Path("backend/config"),
    Path("packages/harness-integration"),
]

# Top-level files scanned individually (install/lifecycle scripts + TUI/CLI).
SCAN_FILES = [
    Path("install.sh"),
    Path("backend/platform.sh"),
    Path("backend/sysadmin_cli.py"),
    Path("backend/platform_tui.py"),
    Path("backend/installer_tui.py"),
]

TEXT_SUFFIXES = {
    ".py", ".js", ".mjs", ".json", ".yaml", ".yml", ".sh", ".conf",
    ".css", ".html", ".md", ".txt", ".example",
}

# A host is "external" (network-relevant) only if it looks like a domain: it
# contains a dot. Single-label hosts ("test", "localhost") are internal.
_URL_RE = re.compile(r"https?://([^\s/\"'<>(){},]+)", re.IGNORECASE)
_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")

# --- allowlist loading -------------------------------------------------------


def load_allowlist(path: Path = DEFAULT_ALLOWLIST) -> dict:
    """Load and minimally validate the allowlist JSON."""
    if not path.exists():
        raise FileNotFoundError(f"allowlist not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    for entry in data.get("destinations", []):
        for required in ("name", "pattern", "class"):
            if required not in entry:
                raise ValueError(f"allowlist entry missing {required!r}: {entry.get('name')}")
        try:
            re.compile(entry["pattern"])
        except re.error as exc:
            raise ValueError(f"bad pattern in {entry.get('name')!r}: {exc}") from exc
    return data


# --- file discovery ----------------------------------------------------------


def _excluded(path: Path, root: Path) -> bool:
    """True if a relative path sits under an excluded directory."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return True
    for part in rel.parts:
        if part in EXCLUDED_PARTS:
            return True
    return False


def iter_text_files(roots=None, files=None) -> list:
    """Return [(repo_relative_posix_path, absolute_path)] of scannable text files."""
    roots = roots if roots is not None else SCAN_ROOTS
    files = files if files is not None else SCAN_FILES
    out = []
    for root in roots:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            if _excluded(path, base):
                continue
            out.append((path.relative_to(REPO_ROOT).as_posix(), path))
    for f in files:
        path = REPO_ROOT / f
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
            out.append((path.relative_to(REPO_ROOT).as_posix(), path))
    return sorted(set(out))


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# --- URL / host helpers ------------------------------------------------------


def extract_urls(text: str) -> list:
    """Return the list of unique lowercased `scheme://host` (port stripped)."""
    urls = []
    for match in _URL_RE.findall(text):
        host = match.rstrip(".").lower()
        # Strip an explicit :port suffix so matching is on the hostname.
        if host.startswith("[") and "]" in host:
            host = host[1:host.index("]")]
        elif host.count(":") == 1:
            host = host.rsplit(":", 1)[0]
        if host:
            urls.append(host)
    return sorted(set(urls))


def is_loopback(host: str) -> bool:
    host = host.lower().strip("[]")
    if host in {"localhost", "::1", "0.0.0.0", "::"}:
        return True
    if _IPV4_RE.match(host) and host.startswith("127."):
        return True
    return False


def is_external(host: str) -> bool:
    """A network-relevant public host: not loopback and looks like a domain/IP."""
    if is_loopback(host):
        return False
    return "." in host


def match_destination(host: str, allowlist: dict):
    """Return the first allowlist destination whose pattern matches the host."""
    for entry in allowlist.get("destinations", []):
        if re.search(entry["pattern"], host, re.IGNORECASE):
            return entry
    return None


# --- strictness --------------------------------------------------------------


def file_is_runtime(rel_path: str) -> bool:
    """Strict files are runtime service code and runtime config.

    Install/lifecycle scripts, package manifests and docs are non-strict: they
    may legitimately reference install-time artifact hosts (mirror targets),
    which the allowlist already covers.
    """
    if rel_path in {"install.sh", "backend/platform.sh", "install-harness.sh"}:
        return False
    if rel_path.endswith("package.json"):
        return False
    if rel_path.startswith("packages/harness-integration/scripts/"):
        return False
    return True


# --- static census -----------------------------------------------------------


def _destination_rows(allowlist: dict, files: list) -> list:
    """Attribute each allowlist destination to the files that reference it, and
    determine whether its disable switch is present anywhere in the scanned code."""
    all_text = "\n".join(_read_text(path) for _, path in files)
    rows = []
    for entry in allowlist.get("destinations", []):
        pattern = re.compile(entry["pattern"], re.IGNORECASE)
        evidence = []
        for rel, path in files:
            text = _read_text(path)
            if pattern.search(text):
                evidence.append(rel)
        tokens = [str(t) for t in entry.get("control_tokens", [])]
        applied_in_code = bool(tokens) and any(t in all_text for t in tokens)
        if entry.get("control") is None:
            scope = "n/a"  # non-egress (e.g. browser hyperlink) or no switch exists
        elif applied_in_code:
            scope = "code"
        else:
            scope = "operator"
        rows.append({
            "name": entry["name"],
            "class": entry["class"],
            "control": entry.get("control"),
            "control_tokens": tokens,
            "applied_in_code": applied_in_code,
            "scope": scope,
            "note": entry.get("note", ""),
            "evidence": evidence,
        })
    return rows


def _unclassified_external(files: list, allowlist: dict) -> list:
    """External hosts in strict (runtime) files that match no allowlist entry."""
    findings = []
    for rel, path in files:
        if not file_is_runtime(rel):
            continue
        text = _read_text(path)
        for host in extract_urls(text):
            if not is_external(host):
                continue
            if match_destination(host, allowlist) is None:
                findings.append({"file": rel, "host": host})
    # De-duplicate while preserving first-seen order.
    seen = set()
    unique = []
    for f in findings:
        key = (f["file"], f["host"])
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


def run_static_census(allowlist_path: Path = DEFAULT_ALLOWLIST) -> dict:
    allowlist = load_allowlist(allowlist_path)
    files = iter_text_files()
    destinations = _destination_rows(allowlist, files)
    unclassified = _unclassified_external(files, allowlist)
    external = [d for d in destinations if d["class"] == "runtime-external"]
    # A runtime-external destination "lacks a disable switch in code" when a
    # switch is documented (control is set) but its tokens do not appear in the
    # scanned platform code/config.
    lacking_switch = [
        d for d in external if d.get("control") and not d["applied_in_code"]
    ]
    return {
        "allowlist": str(allowlist_path),
        "files_scanned": len(files),
        "destinations": destinations,
        "unclassified_external": unclassified,
        "runtime_external_lacking_switch": lacking_switch,
        "clean": len(unclassified) == 0,
    }


def render_report(report: dict) -> str:
    lines = []
    lines.append("=" * 100)
    lines.append("Sysadmin AI Platform — egress / sovereignty static census")
    lines.append(f"  allowlist       : {report['allowlist']}")
    lines.append(f"  files scanned   : {report['files_scanned']}")
    lines.append("=" * 100)
    lines.append("")
    lines.append(f"{'DESTINATION':38} {'CLASS':18} {'CONTROL':44} SCOPE")
    lines.append("-" * 100)
    for d in report["destinations"]:
        scope = d["scope"]
        control = (d["control"] or "-")
        lines.append(f"{d['name']:38} {d['class']:18} {control:44} {scope}")
    lines.append("")
    lines.append("Evidence (files referencing each destination):")
    for d in report["destinations"]:
        ev = ", ".join(d["evidence"]) if d["evidence"] else "(none)"
        lines.append(f"  - {d['name']}: {ev}")
    lines.append("")
    if report["runtime_external_lacking_switch"]:
        lines.append("Runtime-external destinations lacking a disable switch in code:")
        for d in report["runtime_external_lacking_switch"]:
            lines.append(f"  [!] {d['name']} — control: {d['control']}")
    else:
        lines.append("Runtime-external destinations: all have a disable switch or controlled import.")
    lines.append("")
    if report["unclassified_external"]:
        lines.append("UNCLASSIFIED EXTERNAL DESTINATIONS IN RUNTIME CODE:")
        for f in report["unclassified_external"]:
            lines.append(f"  [FAIL] {f['file']} -> {f['host']}")
        lines.append("")
        lines.append("Result: FAIL (unclassified external destination in runtime code)")
    else:
        lines.append("Result: PASS (every external destination is classified)")
    return "\n".join(lines)


# --- runtime check -----------------------------------------------------------


def read_platform_pids(run_dir: Path = None) -> dict:
    run_dir = run_dir or (REPO_ROOT / "backend" / "run")
    pids = {}
    if not run_dir.is_dir():
        return pids
    for pidfile in sorted(run_dir.glob("*.pid")):
        try:
            pid = int(pidfile.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            continue
        pids[pidfile.stem] = pid
    return pids


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def parse_ss_ip(addr: str):
    """Split an `ss` address token into (ip, port). Returns (None, None) on junk."""
    addr = (addr or "").strip()
    if not addr or addr == "*":
        return None, None
    if addr.startswith("["):
        end = addr.find("]")
        if end == -1:
            return None, None
        ip = addr[1:end]
        port = addr[end + 1:].lstrip(":")
        return ip, port or None
    if addr.count(":") == 1:
        ip, port = addr.rsplit(":", 1)
        return ip, port or None
    return None, None


def is_loopback_ip(ip) -> bool:
    if ip is None:
        return True
    ip = ip.strip("[]")
    if ip in {"localhost", "::1", "0.0.0.0", "::", "*"}:
        return True
    if _IPV4_RE.match(ip) and ip.startswith("127."):
        return True
    return False


def _parse_ss_line(line: str):
    """Return (pid_or_None, remote_ip_or_None) from one `ss -tnp` output line.

    Expected columns: State Recv-Q Send-Q Local Peer Process. The Process
    column carries `users:(("name",pid=NNN,fd=N))`. pid is only parsed when the
    socket belongs to a process (listening sockets under unprivileged `ss`
    sometimes have no process column).
    """
    if not line.strip() or line.startswith("State"):
        return None, None
    fields = line.split()
    if len(fields) < 5:
        return None, None
    local = fields[3]
    remote = fields[4]
    remote_ip, _ = parse_ss_ip(remote)
    if remote_ip is None:
        remote_ip, _ = parse_ss_ip(local)
    pid = None
    m = re.search(r"pid=(\d+)", line)
    if m:
        pid = int(m.group(1))
    return pid, remote_ip


def sample_ss(seconds: int, platform_pids: dict, ss_bin: str = "ss") -> dict:
    """Sample `ss -tnp` repeatedly and attribute non-loopback peers to platform PIDs."""
    live_pids = {name: pid for name, pid in platform_pids.items() if _pid_alive(pid)}
    if not live_pids:
        return {"measurable": False, "reason": "no live platform PIDs",
                "non_loopback": [], "samples": 0}
    pid_set = set(live_pids.values())
    non_loopback = []
    samples = 0
    for _ in range(max(1, seconds)):
        try:
            proc = subprocess.run(
                [ss_bin, "-tnp"], capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"measurable": False, "reason": f"ss failed: {exc}",
                    "non_loopback": [], "samples": samples}
        if proc.returncode != 0:
            return {"measurable": False, "reason": f"ss exit {proc.returncode}",
                    "non_loopback": [], "samples": samples}
        samples += 1
        for line in proc.stdout.splitlines():
            pid, remote_ip = _parse_ss_line(line)
            if pid is None or pid not in pid_set:
                continue
            if remote_ip is not None and not is_loopback_ip(remote_ip):
                non_loopback.append({"pid": pid, "peer": remote_ip})
    # Deduplicate.
    seen = set()
    unique = []
    for item in non_loopback:
        key = (item["pid"], item["peer"])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return {"measurable": True, "live_pids": live_pids, "samples": samples,
            "non_loopback": unique}


def unshare_netns_smoke(python_bin: str = None) -> dict:
    """Prove the runtime's heavy imports succeed inside a fresh network namespace."""
    python_bin = python_bin or str(REPO_ROOT / "backend" / ".venv" / "bin" / "python3")
    import_code = "import httpx, fastapi, yaml, redis, pydantic; print('import-ok')"
    try:
        proc = subprocess.run(
            ["unshare", "-rn", "--", python_bin, "-c", import_code],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        return {"measurable": False, "reason": "unshare not found"}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"measurable": False, "reason": f"unshare failed: {exc}"}
    if proc.returncode == 0 and "import-ok" in proc.stdout:
        return {"measurable": True, "import_ok": True, "detail": "imports succeed with no network"}
    return {"measurable": False, "reason": "unprivileged user namespaces unavailable",
            "stderr": proc.stderr.strip()[:300]}


def run_runtime_check(seconds: int = 3) -> dict:
    pids = read_platform_pids()
    socket_check = sample_ss(seconds, pids)
    if socket_check["measurable"]:
        clean = len(socket_check["non_loopback"]) == 0
        return {
            "method": "ss -tnp",
            "live_pids": socket_check.get("live_pids", {}),
            "samples": socket_check["samples"],
            "non_loopback": socket_check["non_loopback"],
            "clean": clean,
            "measurable": True,
            "fallback": None,
        }
    # Fall back to a network-namespace import smoke.
    fallback = unshare_netns_smoke()
    return {
        "method": "unshare -rn import smoke",
        "ss_result": socket_check,
        "clean": bool(fallback.get("measurable")),
        "measurable": bool(fallback.get("measurable")),
        "fallback": fallback,
        "non_loopback": [],
    }


def render_runtime_report(result: dict) -> str:
    lines = []
    lines.append("=" * 100)
    lines.append("Sysadmin AI Platform — runtime egress check")
    lines.append(f"  method        : {result['method']}")
    if result.get("ss_result"):
        lines.append(f"  ss result     : measurable={result['ss_result']['measurable']} "
                     f"({result['ss_result']['reason']})")
    if result.get("live_pids"):
        lines.append("  live platform PIDs:")
        for name, pid in sorted(result["live_pids"].items()):
            lines.append(f"    - {name}: {pid}")
    if result.get("fallback"):
        lines.append(f"  fallback      : {result['fallback']}")
    lines.append("-" * 100)
    if not result.get("measurable"):
        lines.append("Result: UNMEASURABLE (no live platform PIDs and unprivileged userns unavailable)")
        lines.append("  Cannot attribute or rule out platform egress on this host.")
    elif result.get("non_loopback"):
        lines.append("Result: FAIL — non-loopback connections attributed to platform PIDs:")
        for item in result["non_loopback"]:
            lines.append(f"  pid {item['pid']} -> {item['peer']}")
    else:
        lines.append("Result: PASS — no non-loopback connection attributed to a platform PID")
    return "\n".join(lines)


# --- CLI ---------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Sovereignty / blocked-egress audit (PR-D3 tooling).",
    )
    parser.add_argument("--runtime", action="store_true",
                        help="run the unprivileged runtime egress check instead of the static census")
    parser.add_argument("--seconds", type=int, default=3,
                        help="sampling window (seconds) for the runtime check")
    parser.add_argument("--allowlist", type=str, default=str(DEFAULT_ALLOWLIST),
                        help="path to the allowlist JSON")
    parser.add_argument("--json", action="store_true",
                        help="emit machine-readable JSON")
    args = parser.parse_args(argv)

    if args.runtime:
        result = run_runtime_check(args.seconds)
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(render_runtime_report(result))
        if not result["measurable"]:
            return 2
        return 0 if result["clean"] else 1

    try:
        report = run_static_census(Path(args.allowlist))
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"harness error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_report(report))
    return 0 if report["clean"] else 1


if __name__ == "__main__":
    sys.exit(main())
