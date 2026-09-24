# Tool reference

The agent can call four bounded tools. Tools are dispatched by
`backend/services/agent_runtime/tool_registry.py`; the HTTP surface is
`POST /api/tools/execute` on the agent platform.

## Catalogue

| Tool | Purpose | Key parameters |
|---|---|---|
| `search_log_stream` | Bounded regex search in files or journalctl. | `target`, `pattern`, `max_matches`, `context_lines` |
| `config_lint_and_diff` | Validate JSON/YAML/systemd/conf and produce a unified diff with hashes. | `target_file`, `proposed_content` |
| `doc_runbook_reader` | Extract one Markdown section from a runbook. | `runbook_path`, `section_title` |
| `sandboxed_bash` | Execute a command under the approval gate and Bubblewrap sandbox. | `command`, `approval_id` (optional) |

---

## `search_log_stream`

Streams a log file (or queries `journalctl`) without buffering the whole file.

- `max_matches` is clamped to 1–50; `context_lines` to 0–10.
- A `.service` target (or `is_journalctl`) runs
  `journalctl -u <unit> --no-pager -n 1000 --grep <pattern>` (10 s timeout); the
  unit name must match `^[A-Za-z0-9_@.\-]+$`.
- File targets are resolved and confined (see
  [security.md](security.md#path-confinement-backend-tools)). If `/usr/bin/rg` exists it is
  used with a 512 KiB output cap and early termination; otherwise a pure-Python
  line-by-line engine runs (context via a ring buffer, RSS well under 100 MB).
- Result: `matched`, `target`, `pattern`, `match_count`, `line_numbers`,
  `source_id`, `max_matches`, `context_lines`, `truncated`, `output`,
  `rss_kb`, and `error` on failure. "No matches" and "execution failure" are
  distinguished.

## `config_lint_and_diff`

Validates proposed content and returns a real unified diff.

- Format by extension: `.json` (strict JSON), `.yaml`/`.yml` (multi-document
  safe load), `.service`/`.unit`/`.socket`/`.timer`/`.mount` (systemd unit
  validator), `.conf` (systemd headers if present, otherwise brace balance).
- The systemd validator checks section headers, `key=value` directives, valid
  `Type` and `Restart` values, quote balance, and that a `.service` has an
  `ExecStart`.
- Returns `valid`, `error`, `target_file`, sizes, `original_hash`,
  `proposed_hash` and `diff` (or `(No changes detected)`).

## `doc_runbook_reader`

Extracts the section whose heading matches `section_title` (case-insensitive,
leading numbering like `1.` stripped) and keeps its subsections.

- Returns `found`, `matched_header`, `header_level`, `content`,
  `start_line`, `end_line`, `source_id` and `available_sections`.
- On a miss it returns the available section titles to help the agent retry.
- Path-confined to runbook roots and the caller's workspace.

## `sandboxed_bash`

1. Recomputes the caller's workspace with `ensure_workspace` and rejects a
   session workspace that does not match.
2. Classifies the command (see below).
3. On `APPROVAL_REQUIRED`, requires a valid `approval_id`; otherwise it creates
   one and returns `approval_required`.
4. Executes via `bwrap-runner.sh <workspace> /bin/sh -c <command>`.

The runtime's `tool_registry` records a `ToolExecutionRecord` for every outcome
and writes an audit event.

---

## Command policy

`evaluate_command_safety` (Python: `backend/services/approval_gate/filter.py`;
identical JS port in the harness plugin) returns one of:

### `BLOCKED`

- `rm` with both recursive and forced flags (combined `-rf` or split `-r -f`).
- `mkfs[.*]`.
- `dd` reading/writing `/dev/sd*|nvme*|vd*|hd*|mapper*`.
- Redirection into a raw block device.
- `iptables ... -F`, `nft flush`, `ufw reset|disable`.
- `reboot`, `shutdown`, `poweroff`, `init 0|6`, `telinit 0|6`.
- Fork bombs (several whitespace/subshell variants).
- An empty command.

### `ALLOW`

A command with **no** shell metacharacters
(`; | & < > ` $ ( ) { } \` newline`) whose first token is one of:
`cat`, `date`, `df`, `echo`, `free`, `head`, `id`, `ls`, `pwd`, `tail`,
`uname`, `wc`, `whoami`.

### `APPROVAL_REQUIRED`

Everything else — any shell expression or non-whitelisted command.

> Regex classification is a first filter, not the security boundary. Run-time
> confinement (Bubblewrap/cgroups), approval binding and audit are the controls.

---

## Workspace and path rules

- Workspaces are server-owned: `backend/data/workspaces/<user_id>`, created
  `0700`, rejected if the path is a symlink, and never selected by the client.
- Read tools must resolve inside a category root and outside the forbidden list;
  workspace paths must belong to the authenticated user.
- The target adapter requires staged files to be regular files directly inside
  the workspace, opened `O_NOFOLLOW` and ≤ 1 MiB.
