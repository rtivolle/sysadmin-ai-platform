"""
Command normalization and destructive command safety filter.
"""
import re
import shlex
from typing import Any, Dict, List, Tuple

DANGEROUS_COMMANDS = [
    re.compile(r"rm\s+-rf", re.IGNORECASE),
    re.compile(r"mkfs", re.IGNORECASE),
    re.compile(r"dd\s+if=", re.IGNORECASE),
    re.compile(r">\s*/dev/(?:sd|nvme|vd|hd)", re.IGNORECASE),
    re.compile(r"iptables\s+-F", re.IGNORECASE),
    re.compile(r"\breboot\b|\bshutdown\b", re.IGNORECASE),
    re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;", re.IGNORECASE),
]

HARDENED_DANGEROUS_PATTERNS = [
    # rm -rf and recursive forced deletions
    re.compile(r"\brm\s+.*-(?:[a-zA-Z]*r[a-zA-Z]*f|[a-zA-Z]*f[a-zA-Z]*r)", re.IGNORECASE),
    re.compile(r"\brm\s+.*--recursive.*--force", re.IGNORECASE),
    re.compile(r"\brm\s+.*--force.*--recursive", re.IGNORECASE),
    
    # Filesystem formatting
    re.compile(r"\bmkfs(?:\.[a-z0-9]+)?\b", re.IGNORECASE),
    
    # Raw block device writes via dd
    re.compile(r"\bdd\s+.*(?:of|if)=/dev/(?:sd|nvme|vd|hd|mapper)", re.IGNORECASE),
    
    # Direct shell redirection to raw block devices
    re.compile(r">\s*/dev/(?:sd|nvme|vd|hd|mapper)", re.IGNORECASE),
    
    # Firewall flushing
    re.compile(r"\biptables\s+.*-F", re.IGNORECASE),
    re.compile(r"\bnft\s+flush", re.IGNORECASE),
    re.compile(r"\bufw\s+(?:reset|disable)", re.IGNORECASE),
    
    # System power state changes
    re.compile(r"\b(?:reboot|shutdown|poweroff|init\s+[06]|telinit\s+[06])\b", re.IGNORECASE),
    
    # Fork-bombs (including whitespace and subshell variants)
    re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;", re.IGNORECASE),
    re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;", re.IGNORECASE),
    re.compile(r"\w+\(\)\s*\{\s*\w+\s*\|\s*\w+\s*&\s*\}\s*;\s*\w+", re.IGNORECASE),
]

SHELL_SYNTAX = re.compile(r"[;|&<>`$(){}\\\r\n]")
READ_ONLY_COMMANDS = frozenset({
    "cat", "date", "df", "echo", "free", "head", "id", "ls", "pwd",
    "tail", "uname", "wc", "whoami",
})


def normalize_command(command: str) -> Tuple[str, List[str]]:
    """
    Normalizes a command string into canonical representation:
    1. Strips whitespace.
    2. Parses tokens via shlex.split.
    3. Rebuilds canonical single-space separated command string.
    """
    if not command or not command.strip():
        return "", []
    try:
        tokens = shlex.split(command.strip())
    except ValueError:
        tokens = command.strip().split()
    canonical_command = " ".join(tokens)
    return canonical_command, tokens


def evaluate_command_safety(command: str) -> Dict[str, Any]:
    """
    Evaluates command safety against blocked patterns and read-only whitelist.
    Returns:
      - BLOCKED: matches destructive pattern or empty
      - ALLOW: simple read-only command without shell syntax
      - APPROVAL_REQUIRED: mutating operation or shell expression
    """
    if not isinstance(command, str) or not command.strip():
        return {"action": "BLOCKED", "reason": "Security violation: empty command"}

    # Combined and split rm flags have the same destructive effect. Inspect
    # tokens in each rm invocation before the broader text patterns below.
    for invocation in re.finditer(r"\brm\b[^;&|]*", command, re.IGNORECASE):
        try:
            tokens = shlex.split(invocation.group())
        except ValueError:
            tokens = invocation.group().split()
        flags = {letter.lower() for token in tokens[1:] if token.startswith("-") and not token.startswith("--") for letter in token[1:]}
        long_flags = {token for token in tokens[1:] if token.startswith("--")}
        if ("r" in flags or "--recursive" in long_flags) and ("f" in flags or "--force" in long_flags):
            return {"action": "BLOCKED", "reason": "Security violation: recursive forced deletion"}

    # Check baseline dangerous commands first (for backwards test exact match)
    for pattern in DANGEROUS_COMMANDS:
        if pattern.search(command):
            return {
                "action": "BLOCKED",
                "reason": f"Security violation: Command matched blocked pattern '{pattern.pattern}'"
            }

    # Check hardened patterns
    for pattern in HARDENED_DANGEROUS_PATTERNS:
        if pattern.search(command):
            return {
                "action": "BLOCKED",
                "reason": f"Security violation: Command matched blocked pattern '{pattern.pattern}'"
            }

    # Check if simple read-only command without shell syntax
    if not SHELL_SYNTAX.search(command):
        try:
            argv = shlex.split(command)
        except ValueError:
            argv = []
        if argv and argv[0] in READ_ONLY_COMMANDS:
            return {"action": "ALLOW", "reason": "Simple read-only command"}

    return {
        "action": "APPROVAL_REQUIRED",
        "reason": "Mutating operation detected or shell expression; human approval required"
    }
