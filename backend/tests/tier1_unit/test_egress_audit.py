"""
Tier 1 Unit Test: egress / sovereignty static census (PR-D3 tooling).

Pins the behaviour of backend/tests/qualification/egress_audit.py:

  * the allowlist JSON is structurally valid and every pattern compiles;
  * URL/host extraction normalises ports and trailing dots;
  * loopback vs external classification is correct;
  * an unclassified external host in runtime code is flagged, while known
    hosts, loopback addresses and install-time scripts are not;
  * the real-repo census is clean (no unclassified external destination) and
    correctly reports vLLM usage stats as a runtime-external destination
    lacking a disable switch in code;
  * the `ss -tnp` socket sampler attributes non-loopback peers to live
    platform PIDs (and only those), and the runtime check fails clearly when
    it cannot measure anything.

All fixtures are synthetic files written under tmp_path — no live services and
no network are touched.
"""
import importlib.util
import json
import os
import re
from pathlib import Path

import pytest

# Load the qualification module by path (backend/tests/qualification has no
# __init__.py, and this keeps the import independent of package layout).
_QA_PATH = Path(__file__).resolve().parents[1] / "qualification" / "egress_audit.py"
_spec = importlib.util.spec_from_file_location("egress_audit", _QA_PATH)
egress_audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(egress_audit)

_ALLOWLIST = _QA_PATH.parent / "egress_allowlist.json"


# ---------------------------------------------------------------------------
# Allowlist integrity
# ---------------------------------------------------------------------------

def test_allowlist_json_is_valid_and_patterns_compile():
    data = json.loads(_ALLOWLIST.read_text(encoding="utf-8"))
    assert data["schema_version"] == 2
    destinations = data["destinations"]
    assert destinations, "allowlist must enumerate destinations"
    for entry in destinations:
        for required in ("name", "pattern", "class"):
            assert required in entry, f"{entry.get('name')}: missing {required}"
        assert entry["class"] in data["classes"], entry
        re.compile(entry["pattern"])  # raises on bad regex
    classes = {e["class"] for e in destinations}
    assert "runtime-external" in classes
    assert "install-time" in classes


def test_allowlist_loader_rejects_bad_pattern(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"destinations": [
        {"name": "x", "pattern": "([", "class": "runtime-external"},
    ]}))
    with pytest.raises(ValueError):
        egress_audit.load_allowlist(bad)


def test_allowlist_loader_rejects_missing_fields(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"destinations": [{"name": "x"}]}))
    with pytest.raises(ValueError):
        egress_audit.load_allowlist(bad)


def test_allowlist_loader_raises_on_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        egress_audit.load_allowlist(tmp_path / "nope.json")


# ---------------------------------------------------------------------------
# URL / host extraction and classification
# ---------------------------------------------------------------------------

def test_extract_urls_normalizes_ports_and_case():
    text = "https://HuggingFace.co:443/org/model https://mila.quebec/ http://127.0.0.1:4000/v1"
    assert egress_audit.extract_urls(text) == ["127.0.0.1", "huggingface.co", "mila.quebec"]


def test_extract_urls_strips_bracket_ipv6_port():
    assert "::1" in egress_audit.extract_urls("http://[::1]:9428/insert")


def test_loopback_detection():
    assert egress_audit.is_loopback("127.0.0.1")
    assert egress_audit.is_loopback("localhost")
    assert egress_audit.is_loopback("::1")
    assert not egress_audit.is_loopback("8.8.8.8")
    assert not egress_audit.is_loopback("huggingface.co")


def test_external_requires_dot():
    assert egress_audit.is_external("huggingface.co")
    assert not egress_audit.is_external("127.0.0.1")
    # Single-label hosts (internal hostnames) are not treated as external egress.
    assert not egress_audit.is_external("test")


def test_match_destination_classifies_known_hosts():
    allowlist = egress_audit.load_allowlist(_ALLOWLIST)
    assert egress_audit.match_destination("huggingface.co", allowlist) is not None
    assert egress_audit.match_destination("github.com/traefik/traefik/releases/download/x", allowlist) is not None
    assert egress_audit.match_destination("8.8.8.8", allowlist) is None


def test_file_is_runtime():
    assert egress_audit.file_is_runtime("backend/services/model_manager/downloader.py")
    assert egress_audit.file_is_runtime("backend/config/litellm/config.yaml")
    assert not egress_audit.file_is_runtime("install.sh")
    assert not egress_audit.file_is_runtime("backend/platform.sh")
    assert not egress_audit.file_is_runtime("packages/harness-integration/package.json")
    assert not egress_audit.file_is_runtime("packages/harness-integration/scripts/verify-harness.mjs")


# ---------------------------------------------------------------------------
# Unclassified-external detection with synthetic fixture files
# ---------------------------------------------------------------------------

def _files(tmp_path, mapping):
    """Write {relpath: content} fixtures and return the (rel, path) list."""
    out = []
    for rel, content in mapping.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        out.append((rel, path))
    return out


def test_unclassified_external_flagged_in_runtime_code(tmp_path):
    files = _files(tmp_path, {
        "backend/services/evil.py": 'x = "https://evil.example.com/callback"',
    })
    allowlist = egress_audit.load_allowlist(_ALLOWLIST)
    findings = egress_audit._unclassified_external(files, allowlist)
    assert findings == [{"file": "backend/services/evil.py", "host": "evil.example.com"}]


def test_known_and_loopback_hosts_not_flagged(tmp_path):
    files = _files(tmp_path, {
        "backend/services/ok.py": (
            'a = "https://huggingface.co/org/model"\n'
            'b = "http://127.0.0.1:4000/v1"\n'
            'c = "https://mila.quebec/"\n'
        ),
    })
    allowlist = egress_audit.load_allowlist(_ALLOWLIST)
    assert egress_audit._unclassified_external(files, allowlist) == []


def test_install_time_script_not_flagged_for_external_host(tmp_path):
    files = _files(tmp_path, {
        "install.sh": 'curl -sSL "https://mirror.example.com/traefik.tar.gz"',
    })
    allowlist = egress_audit.load_allowlist(_ALLOWLIST)
    assert egress_audit._unclassified_external(files, allowlist) == []


def test_deduplicates_repeated_host(tmp_path):
    files = _files(tmp_path, {
        "backend/services/a.py": 'x = "https://evil.example.com/1" "https://evil.example.com/2"',
    })
    allowlist = egress_audit.load_allowlist(_ALLOWLIST)
    assert egress_audit._unclassified_external(files, allowlist) == [
        {"file": "backend/services/a.py", "host": "evil.example.com"}
    ]


# ---------------------------------------------------------------------------
# Real-repo census conclusions
# ---------------------------------------------------------------------------

def test_real_repo_census_is_clean_and_flags_vllm_usage_stats():
    report = egress_audit.run_static_census(_ALLOWLIST)
    assert report["files_scanned"] > 50, "census should scan the real tree"
    assert report["clean"] is True, report["unclassified_external"]
    assert report["unclassified_external"] == []

    by_name = {d["name"]: d for d in report["destinations"]}
    # Runtime-external destinations whose disable switch is already in code:
    assert by_name["LiteLLM telemetry"]["applied_in_code"] is True
    assert by_name["DeepSeek Harness telemetry"]["applied_in_code"] is True
    assert by_name["DeepSeek Harness LLM endpoint"]["applied_in_code"] is True
    # vLLM usage stats is the key gap: the switch is not set in code.
    assert by_name["vLLM usage stats"]["applied_in_code"] is False

    lacking = [d["name"] for d in report["runtime_external_lacking_switch"]]
    assert "vLLM usage stats" in lacking
    assert "HuggingFace Hub telemetry" in lacking
    # LiteLLM telemetry is disabled in config, so it must NOT be reported lacking.
    assert "LiteLLM telemetry" not in lacking


# ---------------------------------------------------------------------------
# ss -tnp parsing and socket sampling (runtime check helpers)
# ---------------------------------------------------------------------------

def test_parse_ss_ip():
    assert egress_audit.parse_ss_ip("127.0.0.1:4000") == ("127.0.0.1", "4000")
    assert egress_audit.parse_ss_ip("[::1]:9428") == ("::1", "9428")
    assert egress_audit.parse_ss_ip("*") == (None, None)


def test_is_loopback_ip():
    assert egress_audit.is_loopback_ip("127.0.0.1")
    assert egress_audit.is_loopback_ip("::1")
    assert egress_audit.is_loopback_ip(None)
    assert not egress_audit.is_loopback_ip("8.8.8.8")
    assert not egress_audit.is_loopback_ip("192.168.14.159")


def test_parse_ss_line_attributes_pid_and_peer():
    line = 'ESTAB 0 0 127.0.0.1:4000 8.8.8.8:443 users:(("python3",pid=12345,fd=3))'
    assert egress_audit._parse_ss_line(line) == (12345, "8.8.8.8")

    loop = 'ESTAB 0 0 127.0.0.1:4000 127.0.0.1:50000 users:(("python3",pid=9,fd=3))'
    assert egress_audit._parse_ss_line(loop) == (9, "127.0.0.1")

    assert egress_audit._parse_ss_line("State Recv-Q Send-Q Local Peer Process") == (None, None)


def test_read_platform_pids(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "litellm.pid").write_text("1234\n")
    (run_dir / "valkey.pid").write_text("junk\n")
    pids = egress_audit.read_platform_pids(run_dir)
    assert pids == {"litellm": 1234}
    assert egress_audit.read_platform_pids(tmp_path / "missing") == {}


def test_sample_ss_attributes_nonloopback_to_live_pid(tmp_path):
    pid = os.getpid()  # our own, guaranteed alive
    script = tmp_path / "fake-ss"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "echo 'State Recv-Q Send-Q Local Address:Port Peer Address:Port Process'\n"
        f"echo 'ESTAB 0 0 127.0.0.1:4000 8.8.8.8:443 users:((\"python3\",pid={pid},fd=3))'\n"
        "echo 'ESTAB 0 0 127.0.0.1:4000 127.0.0.1:50000 users:((\"python3\",pid=1,fd=3))'\n"
    )
    script.chmod(0o755)
    result = egress_audit.sample_ss(1, {"test": pid}, ss_bin=str(script))
    assert result["measurable"] is True
    assert result["non_loopback"] == [{"pid": pid, "peer": "8.8.8.8"}]


def test_sample_ss_clean_when_only_loopback(tmp_path):
    pid = os.getpid()
    script = tmp_path / "fake-ss-clean"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "echo 'State Recv-Q Send-Q Local Address:Port Peer Address:Port Process'\n"
        f"echo 'ESTAB 0 0 127.0.0.1:4000 127.0.0.1:50000 users:((\"python3\",pid={pid},fd=3))'\n"
    )
    script.chmod(0o755)
    result = egress_audit.sample_ss(1, {"test": pid}, ss_bin=str(script))
    assert result["measurable"] is True
    assert result["non_loopback"] == []


def test_sample_ss_unmeasurable_with_no_live_pids():
    result = egress_audit.sample_ss(1, {"dead": 2**30})
    assert result["measurable"] is False
    assert result["reason"] == "no live platform PIDs"


def test_runtime_check_fails_clearly_when_unmeasurable(monkeypatch):
    monkeypatch.setattr(egress_audit, "read_platform_pids", lambda *a: {})
    monkeypatch.setattr(egress_audit, "sample_ss", lambda *a, **k: {
        "measurable": False, "reason": "no live platform PIDs", "non_loopback": [], "samples": 0})
    monkeypatch.setattr(egress_audit, "unshare_netns_smoke", lambda *a: {
        "measurable": False, "reason": "unprivileged user namespaces unavailable"})
    result = egress_audit.run_runtime_check(seconds=1)
    assert result["measurable"] is False
    assert result["clean"] is False


def test_render_report_lists_lacking_switch_and_unclassified(tmp_path):
    files = _files(tmp_path, {
        "backend/services/evil.py": 'x = "https://unlisted.example.net/x"',
    })
    allowlist = egress_audit.load_allowlist(_ALLOWLIST)
    report = {
        "allowlist": str(_ALLOWLIST),
        "files_scanned": 1,
        "destinations": egress_audit._destination_rows(allowlist, files),
        "unclassified_external": egress_audit._unclassified_external(files, allowlist),
        "runtime_external_lacking_switch": [],
        "clean": False,
    }
    text = egress_audit.render_report(report)
    assert "UNCLASSIFIED EXTERNAL DESTINATIONS IN RUNTIME CODE" in text
    assert "unlisted.example.net" in text
