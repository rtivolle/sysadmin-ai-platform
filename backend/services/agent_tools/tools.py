"""
Core Sysadmin Bounded Operational Tools:
1. search_log_stream: Line-by-line streaming log search capped at 50 matches (<100 MB RSS).
2. config_lint_and_diff: Real syntax validation (JSON, YAML, systemd unit) + unified diff + SHA256 hashes.
3. doc_runbook_reader: Section-specific Markdown runbook retrieval with subsection preservation.
4. execute_sandboxed_command: Bubblewrap-confined command execution.
"""
import os
import sys
import re
import json
import yaml
import difflib
import hashlib
import subprocess
from collections import deque
from typing import Dict, Any, Tuple, List, Optional

# Base directories
BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
PROJECT_ROOT = os.path.dirname(BACKEND_DIR)
LOGS_DIR = os.path.join(BACKEND_DIR, "data/logs")
RUNBOOKS_DIR = os.path.join(BACKEND_DIR, "data/runbooks")
WORKSPACES_DIR = os.path.join(BACKEND_DIR, "data/workspaces")
CONFIG_DIR = os.path.join(BACKEND_DIR, "config")
FIXTURES_DIR = os.path.join(BACKEND_DIR, "tests/fixtures")

# Blocked sensitive paths
FORBIDDEN_PATHS = [
    "/etc/shadow", "/etc/gshadow", "/etc/sudoers", "/etc/sudoers.d",
    "/proc/kcore", "/dev/mem", "/dev/kmem"
]

def get_current_rss_kb() -> int:
    """Reads current process RSS memory in kilobytes from /proc/self/status."""
    try:
        with open("/proc/self/status", "r") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    return int(parts[1])
    except Exception:
        pass
    return 0

def validate_path_confinement(
    raw_path: str,
    allowed_categories: List[str],
    user_id: Optional[str] = None,
    allow_nonexistent: bool = False
) -> Tuple[bool, str, Optional[str]]:
    """
    Validates path confinement and rejects path traversal.
    Allowed categories: 'logs', 'runbooks', 'configs', 'workspaces'
    """
    if not raw_path or not isinstance(raw_path, str):
        return False, "", "Path must be a non-empty string"

    clean_path = raw_path.strip()

    # Resolve relative to category root or project root if not absolute
    if not os.path.isabs(clean_path):
        if os.path.dirname(clean_path) == "":
            if "configs" in allowed_categories:
                target_path = os.path.join(CONFIG_DIR, clean_path)
            elif "runbooks" in allowed_categories:
                target_path = os.path.join(RUNBOOKS_DIR, clean_path)
            elif "logs" in allowed_categories:
                target_path = os.path.join(LOGS_DIR, clean_path)
            else:
                target_path = os.path.abspath(os.path.join(PROJECT_ROOT, clean_path))
        else:
            target_path = os.path.abspath(os.path.join(PROJECT_ROOT, clean_path))
    else:
        target_path = os.path.abspath(clean_path)

    canonical = os.path.realpath(target_path)

    # Check forbidden paths
    for forbidden in FORBIDDEN_PATHS:
        if canonical == forbidden or canonical.startswith(forbidden + "/"):
            return False, canonical, f"Access denied: Path '{raw_path}' is protected by platform security"

    if ".ssh" in canonical or "backend/config/keys" in canonical:
        return False, canonical, "Access denied: Access to credentials or keys is prohibited"

    # Block access to other sensitive system paths like /etc/passwd
    if canonical in ["/etc/passwd", "/etc/master.passwd"]:
        return False, canonical, f"Access denied: Path '{raw_path}' is protected by platform security"

    # Build allowed directories whitelist
    allowed_roots = ["/tmp"]
    if "logs" in allowed_categories:
        allowed_roots.extend([LOGS_DIR, "/var/log", FIXTURES_DIR, os.path.join(FIXTURES_DIR, "logs")])
    if "runbooks" in allowed_categories:
        allowed_roots.extend([RUNBOOKS_DIR, os.path.join(PROJECT_ROOT, "docs/runbooks"), FIXTURES_DIR, os.path.join(FIXTURES_DIR, "runbooks")])
    if "configs" in allowed_categories:
        allowed_roots.extend([
            CONFIG_DIR, RUNBOOKS_DIR, FIXTURES_DIR,
            os.path.join(FIXTURES_DIR, "config"),
            "/etc/nginx", "/etc/systemd/system"
        ])
    if "workspaces" in allowed_categories:
        if user_id:
            user_ws = os.path.realpath(os.path.join(WORKSPACES_DIR, user_id))
            allowed_roots.append(user_ws)
        else:
            allowed_roots.append(os.path.realpath(WORKSPACES_DIR))

    # Canonicalize all allowed roots
    allowed_roots = [os.path.realpath(r) for r in allowed_roots if os.path.exists(r) or r.startswith("/")]

    # Cross-user workspace check: if path is in workspaces, must belong to user_id
    real_workspaces = os.path.realpath(WORKSPACES_DIR)
    if canonical.startswith(real_workspaces):
        if user_id:
            expected_user_ws = os.path.realpath(os.path.join(WORKSPACES_DIR, user_id))
            if not (canonical == expected_user_ws or canonical.startswith(expected_user_ws + os.sep)):
                return False, canonical, f"Access denied: Cross-workspace access forbidden for user '{user_id}'"

    # Check if canonical path starts with any allowed root
    is_allowed = any(
        canonical == root or canonical.startswith(root + os.sep)
        for root in allowed_roots
    )

    if not is_allowed:
        return False, canonical, f"Access denied: Path '{raw_path}' traverses outside allowed directories"

    if not allow_nonexistent and not os.path.exists(canonical):
        return False, canonical, f"Target file not found: {raw_path}"

    return True, canonical, None


# ---------------------------------------------------------------------------
# 1. search_log_stream
# ---------------------------------------------------------------------------

def search_log_stream(
    target: str,
    pattern: str,
    max_matches: int = 50,
    context_lines: int = 2,
    is_journalctl: bool = False,
    user_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Bounded streaming log search without full-file memory buffering (<100 MB RSS).
    Capped at max 50 matches.
    """
    bounded_max_matches = min(max(1, int(max_matches)), 50)
    bounded_context = min(max(0, int(context_lines)), 10)

    # 1. Journalctl mode
    if is_journalctl or (isinstance(target, str) and target.endswith(".service")):
        if not re.match(r"^[A-Za-z0-9_@\.\-]+$", target):
            return {"matched": False, "target": target, "pattern": pattern, "match_count": 0, "output": "No occurrences found", "error": f"Invalid systemd unit name: '{target}'"}

        cmd = ["/usr/bin/journalctl", "-u", target, "--no-pager", "-n", "1000", "--grep", pattern]
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
            output = proc.stdout
            lines = [l for l in output.splitlines() if l.strip()][:bounded_max_matches]
            matched = len(lines) > 0
            final_rss = get_current_rss_kb()
            return {
                "matched": matched,
                "target": target,
                "pattern": pattern,
                "match_count": len(lines),
                "max_matches": bounded_max_matches,
                "context_lines": bounded_context,
                "truncated": len(output.splitlines()) > bounded_max_matches,
                "output": "\n".join(lines) if matched else "No occurrences found",
                "rss_kb": final_rss
            }
        except Exception as e:
            return {"matched": False, "target": target, "pattern": pattern, "match_count": 0, "output": "No occurrences found", "error": f"journalctl search failed: {str(e)}"}

    # 2. Check existence first if file doesn't exist
    resolved_candidate = target
    if not os.path.isabs(target) and not target.startswith("."):
        direct_candidate = os.path.join(LOGS_DIR, target)
        if os.path.exists(direct_candidate):
            resolved_candidate = direct_candidate

    if not os.path.exists(resolved_candidate):
        return {
            "matched": False,
            "target": target,
            "pattern": pattern,
            "match_count": 0,
            "output": "No occurrences found",
            "error": f"Log file not found: {target}"
        }

    # Path confinement check
    valid, canonical_path, err = validate_path_confinement(
        resolved_candidate,
        allowed_categories=["logs", "workspaces"],
        user_id=user_id,
        allow_nonexistent=False
    )
    if not valid:
        return {
            "matched": False,
            "target": target,
            "pattern": pattern,
            "match_count": 0,
            "line_numbers": [],
            "source_id": target,
            "output": "No occurrences found",
            "error": err
        }

    # Validate regex pattern
    try:
        pattern_re = re.compile(pattern)
    except re.error as e:
        return {
            "matched": False,
            "target": target,
            "pattern": pattern,
            "match_count": 0,
            "line_numbers": [],
            "source_id": canonical_path,
            "output": "No occurrences found",
            "error": f"Invalid regular expression: {str(e)}"
        }

    # Check for ripgrep binary
    rg_bin = "/usr/bin/rg"
    if os.path.exists(rg_bin):
        cmd = [
            rg_bin, "--line-number", "--color", "never",
            "-a",
            "--max-count", str(bounded_max_matches),
            "-C", str(bounded_context),
            pattern, canonical_path
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1
            )
            output_lines = []
            line_numbers = []
            match_count = 0
            total_bytes = 0
            MAX_BYTES = 512 * 1024  # 512 KB cap

            for line in proc.stdout:
                total_bytes += len(line)
                if total_bytes > MAX_BYTES:
                    output_lines.append("\n[OUTPUT TRUNCATED: Max byte limit reached]")
                    break
                output_lines.append(line)
                # Count matching lines (rg uses <lineno>: for matches)
                m_line = re.match(r"^(\d+):", line)
                if m_line:
                    match_count += 1
                    line_numbers.append(int(m_line.group(1)))
                if match_count >= bounded_max_matches and bounded_context == 0:
                    break

            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()

            final_rss = get_current_rss_kb()
            output_str = "".join(output_lines)
            matched = match_count > 0 or len(output_str.strip()) > 0
            return {
                "matched": matched,
                "target": target,
                "pattern": pattern,
                "match_count": match_count,
                "line_numbers": line_numbers,
                "source_id": canonical_path,
                "max_matches": bounded_max_matches,
                "context_lines": bounded_context,
                "truncated": match_count >= bounded_max_matches,
                "output": output_str if matched else "No occurrences found",
                "rss_kb": final_rss
            }
        except Exception:
            pass  # Fallback to pure Python streaming

    # Pure Python Line-by-Line Streaming Engine (RSS < 100 MB guaranteed)
    try:
        output_blocks = []
        line_numbers = []
        match_count = 0
        before_buf = deque(maxlen=bounded_context)
        post_lines_remaining = 0
        last_match_line = -999

        with open(canonical_path, "r", encoding="utf-8", errors="replace") as f:
            for lineno, raw_line in enumerate(f, 1):
                line = raw_line.rstrip("\r\n")

                if pattern_re.search(line):
                    match_count += 1
                    line_numbers.append(lineno)
                    if output_blocks and (lineno - last_match_line > bounded_context * 2 + 1):
                        output_blocks.append("--")

                    for b_lineno, b_line in before_buf:
                        if b_lineno > last_match_line:
                            output_blocks.append(f"{b_lineno}-{b_line}")

                    output_blocks.append(f"{lineno}:{line}")
                    last_match_line = lineno
                    post_lines_remaining = bounded_context

                    if match_count >= bounded_max_matches:
                        if bounded_context == 0:
                            break
                elif post_lines_remaining > 0:
                    output_blocks.append(f"{lineno}-{line}")
                    last_match_line = lineno
                    post_lines_remaining -= 1
                    if match_count >= bounded_max_matches and post_lines_remaining == 0:
                        break

                before_buf.append((lineno, line))

        final_rss = get_current_rss_kb()
        matched = match_count > 0
        output_str = "\n".join(output_blocks)
        if output_str:
            output_str += "\n"

        return {
            "matched": matched,
            "target": target,
            "pattern": pattern,
            "match_count": match_count,
            "line_numbers": line_numbers,
            "source_id": canonical_path,
            "max_matches": bounded_max_matches,
            "context_lines": bounded_context,
            "truncated": match_count >= bounded_max_matches,
            "output": output_str if matched else "No occurrences found",
            "rss_kb": final_rss
        }
    except Exception as e:
        return {
            "matched": False,
            "target": target,
            "pattern": pattern,
            "match_count": 0,
            "line_numbers": [],
            "source_id": canonical_path if 'canonical_path' in locals() else target,
            "output": "No occurrences found",
            "error": str(e)
        }


# ---------------------------------------------------------------------------
# 2. config_lint_and_diff
# ---------------------------------------------------------------------------

def validate_systemd_unit_syntax(content: str, filename: str = "") -> Tuple[bool, Optional[str]]:
    """
    Validates systemd unit syntax conforming to standard systemd specifications.
    """
    lines = content.splitlines()
    if not lines or not content.strip():
        return False, "Systemd unit syntax error: File content is empty"

    VALID_SECTIONS = {
        "Unit", "Service", "Install", "Socket", "Timer", "Path",
        "Mount", "Automount", "Swap", "Scope", "Slice"
    }
    VALID_SERVICE_TYPES = {"simple", "exec", "forking", "oneshot", "dbus", "notify", "idle"}
    VALID_RESTARTS = {"no", "always", "on-success", "on-failure", "on-abnormal", "on-watchdog", "on-abort"}

    current_section = None
    sections_found = set()
    directives_by_section = {}
    has_directives = False

    for lineno, raw_line in enumerate(lines, 1):
        line = raw_line.strip()
        # Comments and blank lines
        if not line or line.startswith("#") or line.startswith(";"):
            continue

        # Section header
        if line.startswith("["):
            if not line.endswith("]"):
                return False, f"Systemd unit syntax error at line {lineno}: Malformed section header (missing closing ']'): '{line}'"
            sec_name = line[1:-1].strip()
            if not sec_name:
                return False, f"Systemd unit syntax error at line {lineno}: Empty section header '[]'"
            if not re.match(r"^[A-Za-z0-9_]+$", sec_name):
                return False, f"Systemd unit syntax error at line {lineno}: Invalid characters in section header '{sec_name}'"
            current_section = sec_name
            sections_found.add(sec_name)
            continue

        # Directive before any section header
        if current_section is None:
            return False, f"Systemd unit syntax error at line {lineno}: Directive found before any section header: '{line}'"

        # Must have key=value assignment
        if "=" not in line:
            return False, f"Systemd unit syntax error at line {lineno}: Directive missing '=' delimiter: '{line}'"

        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()

        if not key:
            return False, f"Systemd unit syntax error at line {lineno}: Empty directive key"
        if not re.match(r"^[A-Za-z0-9_]+$", key):
            return False, f"Systemd unit syntax error at line {lineno}: Invalid directive key name '{key}'"

        has_directives = True
        directives_by_section.setdefault(current_section, {})[key] = val

        # Semantic validation
        if current_section == "Service":
            if key == "Type" and val.lower() not in VALID_SERVICE_TYPES:
                return False, f"Systemd unit syntax error at line {lineno}: Invalid Service Type '{val}'. Expected one of: {sorted(VALID_SERVICE_TYPES)}"
            if key == "Restart" and val.lower() not in VALID_RESTARTS:
                return False, f"Systemd unit syntax error at line {lineno}: Invalid Restart policy '{val}'. Expected one of: {sorted(VALID_RESTARTS)}"

        # Double quotes matching check
        if val.count('"') % 2 != 0:
            return False, f"Systemd unit syntax error at line {lineno}: Unmatched double quote in value: '{val}'"

    if not sections_found:
        return False, "Systemd unit syntax error: No section header found (e.g. [Unit], [Service], [Install])"

    if not has_directives:
        return False, "Systemd unit syntax error: Section defined but no directives present"

    if filename.endswith(".service"):
        if "Service" not in sections_found:
            return False, "Systemd unit syntax error: .service unit requires a [Service] section"
        service_dirs = directives_by_section.get("Service", {})
        if "ExecStart" not in service_dirs or not service_dirs["ExecStart"]:
            return False, "Systemd unit syntax error: .service unit requires an 'ExecStart' directive in [Service] section"

    return True, None


def config_lint_and_diff(
    target_file: str,
    proposed_content: str,
    user_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Validates syntax (JSON, YAML, systemd unit) and generates a unified diff with SHA-256 hashes.
    """
    # Path confinement check
    valid_path, canonical_target, err = validate_path_confinement(
        target_file,
        allowed_categories=["configs", "workspaces", "runbooks"],
        user_id=user_id,
        allow_nonexistent=True
    )
    if not valid_path:
        return {
            "valid": False,
            "error": err,
            "target_file": target_file,
            "original_size": 0,
            "proposed_size": len(proposed_content),
            "original_hash": None,
            "proposed_hash": hashlib.sha256(proposed_content.encode("utf-8")).hexdigest(),
            "diff": ""
        }

    original_content = ""
    if os.path.exists(canonical_target):
        try:
            with open(canonical_target, "r", encoding="utf-8") as f:
                original_content = f.read()
        except Exception:
            original_content = ""

    ext = os.path.splitext(target_file)[1].lower()
    syntax_valid = True
    syntax_error = None

    # Format-specific validation
    if ext == ".json":
        try:
            json.loads(proposed_content)
        except json.JSONDecodeError as e:
            syntax_valid = False
            syntax_error = f"JSON syntax error at line {e.lineno}, column {e.colno}: {e.msg}"
        except Exception as e:
            syntax_valid = False
            syntax_error = f"JSON syntax error: {str(e)}"

    elif ext in [".yaml", ".yml"]:
        try:
            list(yaml.safe_load_all(proposed_content))
        except yaml.MarkedYAMLError as e:
            syntax_valid = False
            line = e.problem_mark.line + 1 if e.problem_mark else "?"
            col = e.problem_mark.column + 1 if e.problem_mark else "?"
            syntax_error = f"YAML syntax error at line {line}, column {col}: {e.problem}"
        except Exception as e:
            syntax_valid = False
            syntax_error = f"YAML syntax error: {str(e)}"

    elif ext in [".service", ".unit", ".socket", ".timer", ".mount"]:
        is_valid, sys_err = validate_systemd_unit_syntax(proposed_content, filename=target_file)
        if not is_valid:
            syntax_valid = False
            syntax_error = sys_err

    elif ext == ".conf":
        has_systemd_headers = any(
            re.match(r"^\s*\[(Unit|Service|Install|Socket|Timer|Path|Mount|Scope|Slice)\]", line)
            for line in proposed_content.splitlines()
        )
        if has_systemd_headers:
            is_valid, sys_err = validate_systemd_unit_syntax(proposed_content, filename=target_file)
            if not is_valid:
                syntax_valid = False
                syntax_error = sys_err
        else:
            open_b = proposed_content.count("{")
            close_b = proposed_content.count("}")
            if open_b != close_b:
                syntax_valid = False
                syntax_error = f"Configuration syntax error: Unbalanced braces ({{: {open_b}, }}: {close_b})"

    # Compute cryptographic hashes for R2 Human-in-the-Loop binding
    original_hash = hashlib.sha256(original_content.encode("utf-8")).hexdigest() if original_content else None
    proposed_hash = hashlib.sha256(proposed_content.encode("utf-8")).hexdigest()

    # Generate standard unified diff matching patch format
    orig_lines = original_content.splitlines(keepends=True)
    prop_lines = proposed_content.splitlines(keepends=True)
    fromfile = f"a/{target_file}" if original_content else "/dev/null"
    tofile = f"b/{target_file}"

    diff = list(difflib.unified_diff(
        orig_lines,
        prop_lines,
        fromfile=fromfile,
        tofile=tofile
    ))
    diff_text = "".join(diff)

    return {
        "valid": syntax_valid,
        "error": syntax_error,
        "target_file": target_file,
        "original_size": len(original_content),
        "proposed_size": len(proposed_content),
        "original_hash": original_hash,
        "proposed_hash": proposed_hash,
        "diff": diff_text if diff_text else "(No changes detected)"
    }


# ---------------------------------------------------------------------------
# 3. doc_runbook_reader
# ---------------------------------------------------------------------------

def doc_runbook_reader(
    runbook_path: str,
    section_title: str,
    user_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Extracts a specific Markdown section from a runbook without truncating subsections.
    """
    # Support resolving plain filenames inside backend/data/runbooks/
    resolved_candidate = runbook_path
    if not os.path.isabs(runbook_path) and not runbook_path.startswith("."):
        direct_rb = os.path.join(RUNBOOKS_DIR, runbook_path)
        if os.path.exists(direct_rb):
            resolved_candidate = direct_rb

    if not os.path.exists(resolved_candidate):
        return {
            "found": False,
            "runbook_path": runbook_path,
            "section_title": section_title,
            "content": "Section not found",
            "start_line": None,
            "end_line": None,
            "source_id": runbook_path,
            "error": f"Runbook file not found: {runbook_path}"
        }

    valid_path, canonical_path, err = validate_path_confinement(
        resolved_candidate,
        allowed_categories=["runbooks", "workspaces"],
        user_id=user_id,
        allow_nonexistent=False
    )
    if not valid_path:
        return {
            "found": False,
            "runbook_path": runbook_path,
            "section_title": section_title,
            "content": "Section not found",
            "start_line": None,
            "end_line": None,
            "source_id": canonical_path if 'canonical_path' in locals() else runbook_path,
            "error": err
        }

    try:
        with open(canonical_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        header_re = re.compile(r"^(#{1,6})\s+(.+)$")
        sections = []
        for idx, line in enumerate(lines):
            m = header_re.match(line.strip())
            if m:
                level = len(m.group(1))
                title = m.group(2).strip()
                sections.append((idx, level, title, line))

        target_norm = section_title.strip().lower()
        clean_target = re.sub(r"^\d+[\.\)]\s*", "", target_norm)

        matched_idx = None
        for idx, (lineno, level, title, raw_hdr) in enumerate(sections):
            title_norm = title.lower()
            clean_title = re.sub(r"^\d+[\.\)]\s*", "", title_norm)
            if clean_target == clean_title or clean_target in clean_title or target_norm in title_norm:
                matched_idx = idx
                break

        if matched_idx is None:
            available = [s[2] for s in sections]
            return {
                "found": False,
                "runbook_path": runbook_path,
                "section_title": section_title,
                "content": "Section not found",
                "start_line": None,
                "end_line": None,
                "source_id": canonical_path,
                "available_sections": available,
                "error": f"Section '{section_title}' not found in runbook. Available sections: {available}"
            }

        start_line, target_level, matched_title, matched_raw_header = sections[matched_idx]
        end_line = len(lines)
        for idx in range(matched_idx + 1, len(sections)):
            next_level = sections[idx][1]
            if next_level <= target_level:
                end_line = sections[idx][0]
                break

        extracted_lines = lines[start_line:end_line]
        return {
            "found": True,
            "runbook_path": runbook_path,
            "section_title": section_title,
            "matched_header": matched_title,
            "header_level": target_level,
            "content": "".join(extracted_lines),
            "start_line": start_line + 1,
            "end_line": end_line,
            "source_id": canonical_path,
            "available_sections": [s[2] for s in sections]
        }
    except Exception as e:
        return {
            "found": False,
            "runbook_path": runbook_path,
            "section_title": section_title,
            "content": "Section not found",
            "start_line": None,
            "end_line": None,
            "source_id": canonical_path if 'canonical_path' in locals() else runbook_path,
            "error": str(e)
        }


# ---------------------------------------------------------------------------
# 4. execute_sandboxed_command
# ---------------------------------------------------------------------------

def execute_sandboxed_command(workspace: str, command: str) -> Tuple[int, str, str]:
    """
    Executes a shell command inside the Bubblewrap sandbox runner.
    """
    bwrap_runner = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../config/sandbox/bwrap-runner.sh"))
    if not os.path.exists(bwrap_runner):
        return 127, "", f"Sandbox runner script not found: {bwrap_runner}"

    proc = subprocess.run(
        [bwrap_runner, workspace, "/bin/sh", "-c", command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    return proc.returncode, proc.stdout, proc.stderr
