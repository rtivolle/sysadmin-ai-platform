"""
Milestone M4 Empirical Challenger Test Suite.
Adversarially challenges:
1. Bubblewrap sandbox confinement, filesystem barriers, host network isolation, and capability dropping.
2. Destructive command evasion (split flags, subshells, variable interpolation, disk wipes, fork bombs).
3. Audit outbox durability, flock concurrency, partial transport failure recovery, and poison pill quarantine.
"""
import os
import json
import socket
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
import httpx

from backend.services.agent_tools import audit
from backend.services.agent_tools.audit import log_audit_event, flush_outbox
from backend.services.agent_tools.tools import execute_sandboxed_command
from backend.services.approval_gate.filter import (
    evaluate_command_safety,
    normalize_command,
    DANGEROUS_COMMANDS,
    HARDENED_DANGEROUS_PATTERNS,
)
from backend.services.agent_tools import server
from services.auth_gateway import server as auth_gateway


@pytest.fixture
def sandbox_workspace():
    """Provides a fresh isolated 0700 workspace directory."""
    with tempfile.TemporaryDirectory(prefix="challenger_ws_") as tmpdir:
        os.chmod(tmpdir, 0o700)
        yield tmpdir


# ==============================================================================
# Suite 1: Bubblewrap Sandbox Confinement & Escape Challenges
# ==============================================================================

def test_sandbox_readonly_usr_barrier_fails_closed(sandbox_workspace):
    """Writing to /usr inside the sandbox must fail closed with Read-only file system."""
    code, stdout, stderr = execute_sandboxed_command(sandbox_workspace, "touch /usr/malicious_payload")
    assert code != 0
    assert "Read-only file system" in stderr


def test_sandbox_remount_usr_rw_fails_closed(sandbox_workspace):
    """Attempting to remount /usr as read-write must fail closed with permission error."""
    code, stdout, stderr = execute_sandboxed_command(sandbox_workspace, "mount -o remount,rw /usr")
    assert code != 0
    assert "must be superuser" in stderr or "Operation not permitted" in stderr or "Permission denied" in stderr


def test_sandbox_network_isolation_external_unreachable(sandbox_workspace):
    """Outbound socket creation to public IPs (e.g. 8.8.8.8) must fail closed with Network unreachable."""
    py_probe = 'python3 -c "import socket; s = socket.socket(); s.settimeout(1); s.connect((\'8.8.8.8\', 53))"'
    code, stdout, stderr = execute_sandboxed_command(sandbox_workspace, py_probe)
    assert code != 0
    assert "Network is unreachable" in stderr or "timed out" in stderr


def test_sandbox_host_loopback_unreachable(sandbox_workspace):
    """Connecting to host Valkey (6379) or VictoriaLogs (9428) loopback must fail closed in unshared net namespace."""
    py_probe = 'python3 -c "import socket; s = socket.socket(); s.settimeout(1); s.connect((\'127.0.0.1\', 6379))"'
    code, stdout, stderr = execute_sandboxed_command(sandbox_workspace, py_probe)
    assert code != 0
    assert "Connection refused" in stderr or "Network is unreachable" in stderr


def test_sandbox_dropped_capabilities_all_zero(sandbox_workspace):
    """Verify that all Linux capabilities in /proc/self/status are dropped to zero."""
    code, stdout, stderr = execute_sandboxed_command(sandbox_workspace, "grep -E '^Cap(Inh|Prm|Eff|Bnd|Amb):' /proc/self/status")
    assert code == 0
    caps = {}
    for line in stdout.strip().splitlines():
        k, v = line.split(":")
        caps[k.strip()] = v.strip()
    assert caps["CapInh"] == "0000000000000000"
    assert caps["CapPrm"] == "0000000000000000"
    assert caps["CapEff"] == "0000000000000000"
    assert caps["CapBnd"] == "0000000000000000"
    assert caps["CapAmb"] == "0000000000000000"


def test_sandbox_root_privilege_escalation_fails_closed(sandbox_workspace):
    """Attempting su or sudo inside sandbox must fail closed without root escalation."""
    code_su, _, stderr_su = execute_sandboxed_command(sandbox_workspace, "su -c id")
    assert code_su != 0
    assert "user root does not exist" in stderr_su or "Authentication failure" in stderr_su or "Permission denied" in stderr_su

    code_sudo, _, stderr_sudo = execute_sandboxed_command(sandbox_workspace, "sudo -n true")
    assert code_sudo != 0
    assert "sudo: not found" in stderr_sudo or "sudo: a password is required" in stderr_sudo or "Operation not permitted" in stderr_sudo


def test_sandbox_pid_namespace_isolated(sandbox_workspace):
    """Process table inside sandbox must only contain bwrap and child processes, not host PIDs."""
    code, stdout, stderr = execute_sandboxed_command(sandbox_workspace, "ps aux")
    assert code == 0
    lines = stdout.strip().splitlines()
    assert len(lines) >= 2  # header + bwrap + ps
    # Host daemons like traefik, weed, victoria-logs must not appear in sandbox ps
    assert "traefik" not in stdout
    assert "victoria-logs" not in stdout
    assert "valkey-server" not in stdout


def test_sandbox_host_filesystem_escape_blocked(sandbox_workspace):
    """Navigating outside /workspace must not expose /home, /root, or parent workspaces."""
    code, stdout, stderr = execute_sandboxed_command(sandbox_workspace, "ls /home")
    assert code != 0
    assert "No such file or directory" in stderr

    code_parent, stdout_parent, _ = execute_sandboxed_command(sandbox_workspace, "ls -1 /workspace/..")
    assert code_parent == 0
    parent_dirs = set(stdout_parent.strip().splitlines())
    # Only container root pseudo-mounts should exist
    assert "home" not in parent_dirs
    assert "root" not in parent_dirs


def test_sandbox_etc_write_behavior_discrepancy(sandbox_workspace):
    """
    DISCREPANCY CHALLENGE:
    docs/status/BENCHMARK_REPORT.md claimed /etc is mounted read-only via
    '--ro-bind /etc /etc' (corrected in that file on 2026-09-24).
    Empirical test reveals that bwrap-runner.sh only binds /etc/resolv.conf and /etc/ssl,
    so touching a new file in container /etc succeeds (code 0) rather than failing closed.
    Note: While this does NOT affect host /etc, it is a divergence from the documented claim.
    """
    # 1. Read-only mounted file inside /etc DOES fail closed:
    code_ro, _, stderr_ro = execute_sandboxed_command(sandbox_workspace, "touch /etc/resolv.conf")
    assert code_ro != 0
    assert "Read-only file system" in stderr_ro

    # 2. Writing a new path inside sandbox /etc currently succeeds in the tmpfs directory:
    code_new, _, _ = execute_sandboxed_command(sandbox_workspace, "touch /etc/sandbox_test_file")
    assert code_new == 0, "Divergence: touching /etc/sandbox_test_file succeeds in container tmpfs"


# ==============================================================================
# Suite 2: Destructive Command Evasion Challenges
# ==============================================================================

@pytest.mark.parametrize("cmd", [
    # Split flags
    "rm -r -f /",
    "rm -f -r /",
    "rm -R -f /",
    "rm -f -R /",
    "rm -r -v -f /",
    "rm -v -r -f /",
    "rm --recursive --force /",
    "rm --force --recursive /",
    "rm -r --force /",
    "rm --recursive -f /",
    "rm -rf /var/log",
    "rm -fr /var/log",
    "rm -rf /*",
    "/bin/rm -rf /",
    "/usr/bin/rm -rf /",
    "\\rm -rf /",
    "\"rm\" -rf /",
    "'rm' -rf /",
    "rm\t-rf\t/",
    "rm\t-r\t-f\t/",
    "rm -r\t-f /",
    "rm -rf\t/",
    "rm  -rf   /",
    "rm -r  -f /",
    # Disk destruction & raw block device writes
    "mkfs.ext4 /dev/sda1",
    "mkfs.xfs -f /dev/nvme0n1",
    "mkfs.btrfs /dev/sda",
    "mkfs.vfat /dev/sdb",
    "mkfs /dev/sda",
    "dd if=/dev/zero of=/dev/sda",
    "dd of=/dev/sda if=/dev/zero",
    "dd if=/dev/zero of=/dev/nvme0n1",
    "dd if=/dev/zero of=/dev/mapper/root",
    "cat /dev/zero > /dev/sda",
    "echo evil > /dev/nvme0n1",
    "> /dev/sda",
    "> /dev/nvme0n1",
    # Network & system shutdown
    "iptables -F",
    "iptables --flush",
    "nft flush ruleset",
    "ufw disable",
    "reboot",
    "reboot -f",
    "shutdown -h now",
    "poweroff",
    "init 0",
    "init 6",
    "telinit 0",
    "telinit 6",
    # Fork-bombs
    ":(){ :|:& };:",
    ":(){ : | : & };:",
    "bomb(){ bomb | bomb & }; bomb",
    # Subshells, wrappers, and interpolation containing dangerous commands
    "$(rm -rf /)",
    "`rm -rf /`",
    "eval \"rm -rf /\"",
    "sh -c \"rm -rf /\"",
    "bash -c \"rm -rf /\"",
    "echo \"rm -rf /\" | sh",
    "cat <<EOF | bash\nrm -rf /\nEOF",
    "CMD=\"rm -rf /\"; $CMD",
    "DIR=\"/\"; rm -rf $DIR",
    "python3 -c \"import os; os.system('rm -rf /')\"",
])
def test_destructive_command_evasions_unconditionally_blocked(cmd):
    """Every destructive pattern and evasion variant must be unconditionally BLOCKED (HTTP 403)."""
    res = evaluate_command_safety(cmd)
    assert res["action"] == "BLOCKED", f"Command failed to block: {cmd}, got {res}"
    assert "Security violation" in res["reason"]


@pytest.mark.parametrize("evasion_cmd", [
    'A="rm"; B="-rf"; $A $B /',
    'X="rm"; $X -rf /',
    'F="-rf"; rm $F /',
    'python3 -c "import shutil; shutil.rmtree(\'/\')"',
])
def test_indirect_variable_evasions_require_approval_and_block_unauthorized(evasion_cmd):
    """
    Commands that construct shell commands across variables without literal 'rm -rf'
    must NOT be classified as ALLOW; they require human approval and cannot execute unapproved.
    """
    res = evaluate_command_safety(evasion_cmd)
    assert res["action"] == "APPROVAL_REQUIRED", f"Expected APPROVAL_REQUIRED for: {evasion_cmd}, got {res}"


@pytest.mark.asyncio
async def test_destructive_command_http_returns_403(monkeypatch):
    """Verify HTTP endpoint unconditionally returns HTTP 403 Forbidden for destructive commands."""
    monkeypatch.setattr(server, "log_audit_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(auth_gateway, "load_valid_tokens", lambda: {"sysadmin-token": "sysadmin-01"})
    
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": "Bearer sysadmin-token"}
        destructive_commands = [
            "rm -r -f /",
            "mkfs.ext4 /dev/sda1",
            "dd if=/dev/zero of=/dev/sda",
            ":(){ :|:& };:",
            "$(rm -rf /)",
        ]
        for cmd in destructive_commands:
            payload = {
                "name": "sandboxed_bash",
                "session_id": "sess-adversarial-test",
                "parameters": {"command": cmd}
            }
            resp = await client.post("/api/tools/execute", json=payload, headers=headers)
            assert resp.status_code == 403, f"Expected 403 for {cmd}, got {resp.status_code}: {resp.text}"
            assert "Security violation" in resp.text


# ==============================================================================
# Suite 3: Audit Outbox Transport Outages & Resilience Challenges
# ==============================================================================

def test_outbox_complete_transport_outage_durable_buffering(monkeypatch, tmp_path):
    """
    During a 100% VictoriaLogs transport outage, 200 concurrent audit events
    from 10 sysadmins must be written durably to outbox.jsonl under flock with 0 loss.
    """
    test_outbox = str(tmp_path / "outbox.jsonl")
    monkeypatch.setattr(audit, "OUTBOX_PATH", test_outbox)
    monkeypatch.setattr(audit, "_post_event", lambda ev: False)

    num_events = 200
    def write_event(idx):
        user_id = f"sysadmin-{(idx % 10) + 1:02d}"
        return log_audit_event(
            user_id=user_id,
            session_id=f"sess-{idx // 10}",
            tool_name="sandboxed_bash",
            command=f"echo outbox_event_{idx}",
            exit_code=0,
            duration_ms=15 + idx,
            tokens_prompt=120,
            tokens_completion=60,
            parameters={"event_num": idx}
        )

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(write_event, range(num_events)))

    assert len(results) == num_events
    assert all(r["logged"] is True and r["destination"] == "outbox" for r in results)

    # Read back outbox file and verify integrity
    assert os.path.exists(test_outbox)
    with open(test_outbox, "r", encoding="utf-8") as f:
        lines = [json.loads(line) for line in f if line.strip()]

    assert len(lines) == num_events
    unique_event_ids = {line["event_id"] for line in lines}
    assert len(unique_event_ids) == num_events

    # Verify auto-drain upon reconnection
    received = []
    monkeypatch.setattr(audit, "_post_event", lambda ev: received.append(ev) or True)

    drain_res = flush_outbox(max_events=500)
    assert drain_res["sent"] == num_events
    assert drain_res["pending"] == 0
    assert len(received) == num_events
    assert {e["event_id"] for e in received} == unique_event_ids
    assert os.path.getsize(test_outbox) == 0


def test_outbox_partial_transport_failure_and_checkpointing(monkeypatch, tmp_path):
    """
    When VictoriaLogs fails midway through a drain, successfully delivered records
    must be removed, unacknowledged records must be preserved in order, and auto-drained on retry.
    """
    test_outbox = str(tmp_path / "outbox.jsonl")
    monkeypatch.setattr(audit, "OUTBOX_PATH", test_outbox)

    total_events = 80
    for i in range(total_events):
        audit._append_outbox({"event_id": f"evt-{i:03d}", "idx": i, "data": "payload"})

    received = []
    def flaky_post(ev):
        if len(received) >= 35:
            return False
        received.append(ev)
        return True

    monkeypatch.setattr(audit, "_post_event", flaky_post)
    res_flaky = flush_outbox(max_events=100)
    assert res_flaky["sent"] == 35
    assert res_flaky["pending"] == 45
    assert res_flaky["error"] == "collector rejected audit record"

    # Verify remaining unposted lines
    with open(test_outbox, "r", encoding="utf-8") as f:
        rem_lines = [json.loads(l) for l in f if l.strip()]
    assert len(rem_lines) == 45
    assert rem_lines[0]["idx"] == 35
    assert rem_lines[-1]["idx"] == 79

    # Reconnection: flush remaining
    monkeypatch.setattr(audit, "_post_event", lambda ev: received.append(ev) or True)
    res_recovery = flush_outbox(max_events=100)
    assert res_recovery["sent"] == 45
    assert res_recovery["pending"] == 0
    assert len(received) == total_events
    assert [e["idx"] for e in received] == list(range(total_events))


def test_outbox_concurrent_appends_during_active_drain(monkeypatch, tmp_path):
    """
    Appends occurring while flush_outbox is actively executing must not be clobbered
    or lost when _replace_outbox swaps the buffer.
    """
    test_outbox = str(tmp_path / "outbox.jsonl")
    monkeypatch.setattr(audit, "OUTBOX_PATH", test_outbox)

    # Populate 30 initial records
    for i in range(30):
        audit._append_outbox({"event_id": f"initial-{i:02d}", "stage": "initial"})

    received = []
    drain_started = threading.Event()
    release_drain = threading.Event()

    def controlled_post(ev):
        if not drain_started.is_set():
            drain_started.set()
            assert release_drain.wait(timeout=5)
        received.append(ev)
        return True

    monkeypatch.setattr(audit, "_post_event", controlled_post)

    drain_thread = threading.Thread(target=flush_outbox)
    drain_thread.start()
    assert drain_started.wait(timeout=5)

    def write_concurrent(idx):
        return audit._append_outbox({"event_id": f"concurrent-{idx:02d}", "stage": "concurrent"})

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(write_concurrent, i) for i in range(20)]
        time.sleep(0.05)  # Let threads queue on the outbox file lock
        release_drain.set()
        drain_thread.join(timeout=5)
        for f in futures:
            f.result()

    assert len(received) == 30

    # Remaining in outbox must be exactly the 20 concurrent records
    with open(test_outbox, "r", encoding="utf-8") as f:
        remaining = [json.loads(line) for line in f if line.strip()]
    assert len(remaining) == 20
    assert all(r["stage"] == "concurrent" for r in remaining)

    # Drain remaining 20
    monkeypatch.setattr(audit, "_post_event", lambda ev: received.append(ev) or True)
    res_final = flush_outbox()
    assert res_final["sent"] == 20
    assert res_final["pending"] == 0
    assert len(received) == 50


def test_outbox_poison_pill_quarantine_resilience(monkeypatch, tmp_path):
    """
    Corrupted non-JSON lines in outbox.jsonl must be safely quarantined to
    outbox_corrupted.jsonl without halting replay or dropping surrounding valid records.
    """
    test_outbox = str(tmp_path / "outbox.jsonl")
    test_corrupted = str(tmp_path / "outbox_corrupted.jsonl")
    monkeypatch.setattr(audit, "OUTBOX_PATH", test_outbox)

    # Write mixture of valid records, malformed JSON, and truncated text
    raw_content = (
        json.dumps({"event_id": "valid-head", "cmd": "head"}) + "\n"
        + "{MALFORMED JSON POISON PILL\n"
        + "GARBAGE LINE WITHOUT BRACKETS\n"
        + json.dumps({"event_id": "valid-tail", "cmd": "tail"}) + "\n"
    )
    with open(test_outbox, "w", encoding="utf-8") as f:
        f.write(raw_content)

    received = []
    monkeypatch.setattr(audit, "_post_event", lambda ev: received.append(ev) or True)

    res = flush_outbox()
    assert res["sent"] == 2
    assert res["pending"] == 0
    assert [r["event_id"] for r in received] == ["valid-head", "valid-tail"]

    # Verify corrupted lines were written to quarantine
    assert os.path.exists(test_corrupted)
    with open(test_corrupted, "r", encoding="utf-8") as cf:
        quarantined = cf.read()
    assert "MALFORMED JSON POISON PILL" in quarantined
    assert "GARBAGE LINE WITHOUT BRACKETS" in quarantined
