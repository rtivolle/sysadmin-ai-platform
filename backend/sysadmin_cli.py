#!/usr/bin/env python3
"""
Sysadmin AI Assistant — Terminal Client (interactive REPL + one-shot subcommands).

Connects to the Traefik gateway and agent platform with provisioned Bearer keys,
demonstrating server-side ReAct reasoning, bounded log searches, lint/diff,
runbook lookups, sandboxed execution and Human-in-the-Loop approvals.

Features over the original prototype:
  * streaming ReAct output (Server-Sent Events) instead of a blocking spinner
  * session management (list / switch / new / delete)
  * user switching across provisioned identities (admin approvals included)
  * Ctrl+C cancels the in-flight request without killing the CLI
  * readline history + line editing, citations and structured tool tables
  * non-interactive subcommands for scripting (`chat`, `tools`, `sessions`, ...)

Environment:
  GATEWAY_URL        gateway base URL            (default http://127.0.0.1:8080)
  SYSADMIN_USER      default identity            (default sysadmin-01)
  SYSADMIN_CLI_HISTORY  history file location    (default ~/.sysadmin_cli_history)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

console = Console()


class GatewayUnreachable(Exception):
    """Raised when the gateway cannot be reached (connection refused/timeout)."""


GATEWAY_URL = os.getenv("GATEWAY_URL", "http://127.0.0.1:8080")
DEFAULT_USER = os.getenv("SYSADMIN_USER", "sysadmin-01")

ROOT_DIR = Path(__file__).resolve().parent
if (ROOT_DIR / "backend").is_dir():
    KEYS_DIR = ROOT_DIR / "backend" / "config" / "keys"
else:
    KEYS_DIR = ROOT_DIR / "config" / "keys"

HISTORY_FILE = Path(
    os.path.expanduser(os.getenv("SYSADMIN_CLI_HISTORY", "~/.sysadmin_cli_history"))
)

# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------

def list_provisioned_users() -> Dict[str, str]:
    """Map user_id -> Bearer token for every provisioned identity.

    Secrets are loaded into memory only; they are never printed or logged.
    """
    users: Dict[str, str] = {}
    if not KEYS_DIR.is_dir():
        return users
    master = KEYS_DIR / "master.key"
    if master.is_file():
        token = master.read_text().strip()
        if token:
            users["sysadmin-admin"] = token
    for key_file in sorted(KEYS_DIR.glob("*.key")):
        if key_file.name in {"master.key", "valkey-password.key"}:
            continue
        user_id = "emergency-p1-oncall" if key_file.stem == "emergency-p1" else key_file.stem
        try:
            token = key_file.read_text().strip()
        except OSError:
            continue
        if token:
            users[user_id] = token
    return users


def get_user_token(user_id: str) -> Optional[str]:
    return list_provisioned_users().get(user_id)


def role_for_user(user_id: str) -> str:
    return "admin" if user_id == "sysadmin-admin" else "sysadmin"


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------

class Client:
    """Thin authenticated HTTP client over the Traefik gateway."""

    def __init__(self, user_id: str, gateway_url: str = GATEWAY_URL):
        self.user_id = user_id
        self.gateway_url = gateway_url.rstrip("/")
        self.session_id: Optional[str] = None
        self.last_request_id: Optional[str] = None

    # -- auth -------------------------------------------------------------
    def headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        token = get_user_token(self.user_id)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def switch_user(self, user_id: str) -> bool:
        if user_id not in list_provisioned_users():
            return False
        self.user_id = user_id
        self.session_id = None
        return True

    def _client(self, read_timeout: Optional[float] = 15.0) -> httpx.Client:
        return httpx.Client(
            timeout=httpx.Timeout(connect=5.0, read=read_timeout, write=15.0, pool=10.0)
        )

    def get(self, path: str, timeout: float = 15.0) -> httpx.Response:
        try:
            with self._client(timeout) as client:
                return client.get(f"{self.gateway_url}{path}", headers=self.headers())
        except httpx.RequestError as exc:
            raise GatewayUnreachable(f"{self.gateway_url}{path}") from exc

    def post(self, path: str, json_body=None, timeout: float = 15.0) -> httpx.Response:
        try:
            with self._client(timeout) as client:
                return client.post(
                    f"{self.gateway_url}{path}", json=json_body, headers=self.headers()
                )
        except httpx.RequestError as exc:
            raise GatewayUnreachable(f"{self.gateway_url}{path}") from exc

    # -- session ----------------------------------------------------------
    def ensure_session_id(self) -> str:
        if not self.session_id:
            self.session_id = f"sess-{uuid.uuid4().hex[:12]}"
        return self.session_id

    def new_session(self) -> str:
        self.session_id = f"sess-{uuid.uuid4().hex[:12]}"
        return self.session_id

    def cancel(self, request_id: Optional[str] = None, session_id: Optional[str] = None):
        try:
            self.post(
                "/api/v1/agent/cancel",
                {"request_id": request_id, "session_id": session_id},
                timeout=5.0,
            )
        except Exception:
            pass


# --------------------------------------------------------------------------
# Chat
# --------------------------------------------------------------------------

def _render_http_error(client: Client, response: httpx.Response) -> None:
    if response.status_code == 429:
        console.print(
            "[bold red]Rate limit or quota ceiling exceeded (HTTP 429). "
            "Wait before retrying.[/bold red]"
        )
    elif response.status_code == 401:
        console.print(
            f"[bold red]Authentication failed (HTTP 401). "
            f"Check the Bearer key for '{client.user_id}'.[/bold red]"
        )
    elif response.status_code == 503:
        console.print(f"[bold red]Service unavailable (HTTP 503): {response.text}[/bold red]")
    else:
        console.print(f"[red]Agent error (HTTP {response.status_code}): {response.text}[/red]")


def _looks_like_action(chunk: str) -> bool:
    return chunk.lstrip().startswith("Thinking:") or "\nAction:" in chunk


def _render_action(chunk: str) -> None:
    for line in chunk.strip().splitlines():
        if line.startswith("Action:"):
            console.print(f"[dim]  ⚡ [bold cyan]{line}[/bold cyan][/dim]")
        elif line.startswith("Thinking:"):
            console.print(f"[dim]  {line}[/dim]")
        else:
            console.print(f"[dim]    {line}[/dim]")


def render_citations(citations: List[dict]) -> None:
    if not citations:
        return
    table = Table(show_header=False, box=None, padding=(0, 0, 0, 2))
    table.add_column(style="dim")
    for c in citations:
        loc = str(c.get("source", ""))
        if c.get("section_or_query"):
            loc += f"  ·  {c['section_or_query']}"
        if c.get("start_line") is not None:
            loc += f"  ·  L{c['start_line']}-{c.get('end_line', '')}"
        table.add_row(f"📄 {loc}")
    console.print(table)


def render_tools_executed(tools: List[dict]) -> None:
    if not tools:
        return
    table = Table(title="[bold]Agent Tools Executed[/bold]", box=None)
    table.add_column("Tool", style="bold cyan")
    table.add_column("Status")
    table.add_column("Duration")
    table.add_column("Detail", overflow="fold")
    for t in tools:
        status = t.get("status", "success")
        color = "green" if status == "success" else (
            "yellow" if status == "approval_required" else "red"
        )
        detail = t.get("command") or t.get("error") or ""
        table.add_row(
            t.get("tool", ""),
            f"[{color}]{status}[/{color}]",
            f"{t.get('duration_ms', 0)}ms",
            str(detail)[:120],
        )
    console.print(table)


def render_approval_required(approval_id: Optional[str], command: Optional[str]) -> None:
    console.print(
        Panel(
            f"[bold red]⚠ HUMAN-IN-THE-LOOP APPROVAL REQUIRED[/bold red]\n\n"
            f"[bold]Command:[/bold] `{command}`\n"
            f"[bold]Approval ID:[/bold] `{approval_id}`\n"
            f"[dim]Expires in 300 seconds.[/dim]",
            title="[bold yellow]Security Interceptor Alert[/bold yellow]",
            border_style="yellow",
        )
    )
    console.print(
        f"[yellow]Ask an administrator to review it, then run /resume {approval_id}.[/yellow]"
    )


def run_chat(
    client: Client,
    prompt: str,
    model: str,
    stream: bool,
    max_steps: Optional[int] = None,
    render: bool = True,
):
    """Send one prompt. Returns the raw dict, or None. `render=False` emits only
    the raw response (used by the `--json` flag)."""
    request_id = f"req-{uuid.uuid4().hex[:12]}"
    session_id = client.ensure_session_id()
    client.last_request_id = request_id

    payload = {
        "session_id": session_id,
        "prompt": prompt,
        "model": model,
        "request_id": request_id,
    }
    if max_steps:
        payload["max_steps"] = max_steps

    if stream:
        return _chat_streaming(client, payload, request_id, session_id)
    return _chat_json(client, payload, request_id, session_id, render=render)


def _chat_json(client: Client, payload: dict, request_id: str, session_id: str, render: bool = True):
    try:
        if render:
            with console.status(
                "[bold cyan]Agent reasoning in ReAct loop...[/bold cyan]", spinner="dots"
            ):
                response = client.post("/api/v1/agent/chat", payload, timeout=90.0)
        else:
            response = client.post("/api/v1/agent/chat", payload, timeout=90.0)
    except KeyboardInterrupt:
        client.cancel(request_id, session_id)
        console.print("[yellow]Request cancelled.[/yellow]")
        return None
    except Exception as exc:
        console.print(f"[red]Error contacting agent gateway: {exc}[/red]")
        return None

    if response.status_code != 200:
        if render:
            _render_http_error(client, response)
        else:
            console.print(f"[red]Agent error (HTTP {response.status_code}).[/red]")
        return None

    data = response.json()
    client.session_id = data.get("session_id", client.session_id)

    if render:
        render_tools_executed(data.get("tools_executed", []))
        if data.get("response"):
            console.print(Panel(Markdown(data["response"]), title="[bold cyan]Sysadmin Assistant[/bold cyan]"))
        render_citations(data.get("citations", []))
        if data.get("approval_required"):
            render_approval_required(data.get("approval_id"), data.get("command"))
    return data


def _chat_streaming(client: Client, payload: dict, request_id: str, session_id: str):
    url = f"{client.gateway_url}/api/v1/agent/chat"
    answer_parts: List[str] = []
    citations: List[dict] = []
    result = {
        "session_id": session_id,
        "response": "",
        "citations": [],
        "approval_required": False,
        "approval_id": None,
        "command": None,
    }
    try:
        with httpx.stream(
            "POST",
            url,
            json=payload,
            headers=client.headers(),
            timeout=httpx.Timeout(connect=5.0, read=None, write=15.0, pool=10.0),
        ) as response:
            if response.status_code != 200:
                _render_http_error(client, response)
                return None
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                chunk = obj.get("chunk", "")
                cits = obj.get("citations") or []
                if cits:
                    citations = cits
                if obj.get("approval_required"):
                    result["approval_required"] = True
                    result["approval_id"] = obj.get("approval_id")
                    result["command"] = obj.get("command")
                    render_approval_required(result["approval_id"], result["command"])
                    continue
                if not chunk:
                    continue
                if _looks_like_action(chunk):
                    _render_action(chunk)
                else:
                    answer_parts.append(chunk)
    except KeyboardInterrupt:
        client.cancel(request_id, session_id)
        console.print("\n[yellow]Request cancelled.[/yellow]")
        return None
    except Exception as exc:
        console.print(f"[red]Streaming error: {exc}[/red]")
        return None

    answer = "".join(answer_parts).strip()
    if answer:
        console.print(Panel(Markdown(answer), title="[bold cyan]Sysadmin Assistant[/bold cyan]"))
    render_citations(citations)
    result["response"] = answer
    result["citations"] = citations
    return result


# --------------------------------------------------------------------------
# Slash commands (shared by the REPL)
# --------------------------------------------------------------------------

def cmd_tools(client: Client) -> None:
    response = client.get("/api/tools/list", timeout=5.0)
    if response.status_code != 200:
        _render_http_error(client, response)
        return
    table = Table(title="[bold green]Registered Sysadmin Agent Tools[/bold green]")
    table.add_column("Tool Name", style="bold cyan")
    table.add_column("Description", overflow="fold")
    for tool in response.json().get("tools", []):
        table.add_row(tool["name"], tool["description"])
    console.print(table)


def cmd_users(client: Client) -> None:
    users = list_provisioned_users()
    if not users:
        console.print("[yellow]No provisioned keys found in config/keys/.[/yellow]")
        return
    table = Table(title="[bold green]Provisioned Identities[/bold green]")
    table.add_column("User", style="bold cyan")
    table.add_column("Role")
    table.add_column("Active")
    for user_id in sorted(users):
        role = role_for_user(user_id)
        marker = "[bold green]◀ current[/bold green]" if user_id == client.user_id else ""
        table.add_row(user_id, role, marker)
    console.print(table)


def cmd_sessions(client: Client) -> None:
    response = client.get("/api/v1/agent/sessions", timeout=5.0)
    if response.status_code != 200:
        _render_http_error(client, response)
        return
    sessions = response.json().get("sessions", [])
    if not sessions:
        console.print("[green]No sessions for this user yet.[/green]")
        return
    table = Table(title=f"[bold green]Sessions for {client.user_id}[/bold green]")
    table.add_column("Session ID", style="bold cyan")
    table.add_column("Active")
    for sid in sessions:
        marker = "[bold green]◀ current[/bold green]" if sid == client.session_id else ""
        table.add_row(sid, marker)
    console.print(table)


def cmd_delete_session(client: Client, session_id: str) -> None:
    response = client.get(f"/api/v1/agent/sessions/{session_id}", timeout=5.0)
    if response.status_code != 200:
        console.print("[yellow]Session not found.[/yellow]")
        return
    resp = httpx.delete(
        f"{client.gateway_url}/api/v1/agent/sessions/{session_id}",
        headers=client.headers(),
        timeout=10.0,
    )
    if resp.status_code == 200:
        if client.session_id == session_id:
            client.new_session()
        console.print(f"[green]Deleted session {session_id}.[/green]")
    else:
        console.print(f"[red]Failed to delete session (HTTP {resp.status_code}).[/red]")


def cmd_approvals(client: Client) -> None:
    response = client.get("/api/approvals/pending", timeout=5.0)
    if response.status_code == 403:
        console.print("[red]Listing approvals requires an administrator identity.[/red]")
        return
    if response.status_code != 200:
        _render_http_error(client, response)
        return
    approvals = response.json().get("pending_approvals", [])
    if not approvals:
        console.print("[green]No pending approvals.[/green]")
        return
    table = Table(title="[bold red]Pending Human-in-the-Loop Approvals[/bold red]")
    table.add_column("ID", style="bold yellow")
    table.add_column("User", style="bold cyan")
    table.add_column("Command", style="bold red", overflow="fold")
    table.add_column("Reason", overflow="fold")
    for a in approvals:
        table.add_row(
            a.get("approval_id", ""),
            a.get("user_id", ""),
            a.get("command", ""),
            a.get("reason", ""),
        )
    console.print(table)


def cmd_decide(client: Client, approval_id: str, approved: bool) -> None:
    response = client.post(
        "/api/approvals/decide",
        {"approval_id": approval_id, "approved": approved},
        timeout=10.0,
    )
    if response.status_code == 200:
        action = "approved" if approved else "rejected"
        console.print(f"[green]Approval {approval_id} {action}.[/green]")
    else:
        console.print(f"[red]{response.text}[/red]")


def cmd_resume(client: Client, approval_id: str, pending: Dict[str, Tuple[str, str]]) -> None:
    operation = pending.get(approval_id)
    if not operation:
        console.print(
            "[red]Unknown approval in this CLI session. "
            "Re-run the prompt that requested it, then resume here.[/red]"
        )
        return
    command, session_id = operation
    response = client.post(
        "/api/tools/execute",
        {
            "name": "sandboxed_bash",
            "session_id": session_id,
            "parameters": {"command": command, "approval_id": approval_id},
        },
        timeout=30.0,
    )
    if response.status_code == 200:
        pending.pop(approval_id, None)
        result = response.json().get("result", {})
        if result.get("stdout"):
            console.print(result["stdout"])
        if result.get("stderr"):
            console.print(f"[yellow]{result['stderr']}[/yellow]")
        console.print(f"[green]Exit code: {result.get('exit_code', 0)}[/green]")
    else:
        console.print(f"[red]{response.text}[/red]")


def cmd_status(client: Client) -> None:
    subprocess.run(["./platform.sh", "status"], cwd=str(ROOT_DIR))


def cmd_logs(client: Client, service: Optional[str] = None) -> None:
    args = ["./platform.sh", "logs"]
    if service:
        args.append(service)
    subprocess.run(args, cwd=str(ROOT_DIR))


def cmd_health(client: Client) -> None:
    checks = [
        ("traefik", f"{client.gateway_url}/api/tools/list"),
        ("agent platform", "http://127.0.0.1:3080/health"),
        ("auth gateway", "http://127.0.0.1:3081/health"),
        ("inference", "http://127.0.0.1:8000/health"),
    ]
    table = Table(title="[bold green]Health Checks[/bold green]")
    table.add_column("Service", style="bold")
    table.add_column("Status")
    for name, url in checks:
        try:
            r = httpx.get(url, timeout=3.0)
            status = "[bold green]UP[/bold green]" if r.status_code < 500 else "[bold red]ERROR[/bold red]"
        except Exception:
            status = "[bold red]DOWN[/bold red]"
        table.add_row(name, status)
    console.print(table)


def cmd_p1(client: Client, action: str, incident_id: Optional[str] = None) -> None:
    if action == "status":
        response = client.get("/api/v1/auth/p1/status", timeout=5.0)
        console.print(response.json() if response.status_code == 200 else f"[red]{response.text}[/red]")
    elif action == "elevate" and incident_id:
        response = client.post(
            "/api/v1/auth/p1/elevate",
            {"incident_id": incident_id},
            timeout=5.0,
        )
        if response.status_code == 200:
            console.print(f"[green]P1 elevation granted for incident {incident_id}.[/green]")
        else:
            console.print(f"[red]{response.text}[/red]")
    elif action == "revoke":
        response = client.post("/api/v1/auth/p1/revoke", {}, timeout=5.0)
        console.print(response.json() if response.status_code == 200 else f"[red]{response.text}[/red]")


# --------------------------------------------------------------------------
# Interactive REPL
# --------------------------------------------------------------------------

HELP_TEXT = """[bold]Commands[/bold]
  [bold cyan]/help[/bold cyan]                       This help
  [bold cyan]/users[/bold cyan]                      List provisioned identities
  [bold cyan]/user <id>[/bold cyan]                  Switch identity (e.g. /user sysadmin-admin)
  [bold cyan]/whoami[/bold cyan]                     Show current identity and role
  [bold cyan]/tools[/bold cyan]                      List registered agent tools
  [bold cyan]/sessions[/bold cyan]                   List this user's sessions
  [bold cyan]/session <id>[/bold cyan]               Continue an existing session
  [bold cyan]/session rm <id>[/bold cyan]            Delete a session
  [bold cyan]/new[/bold cyan]                        Start a fresh session
  [bold cyan]/status[/bold cyan]                     Show platform service status
  [bold cyan]/logs [service][/bold cyan]             Tail service logs
  [bold cyan]/health[/bold cyan]                     Check gateway and service health
  [bold cyan]/approvals[/bold cyan]                  List pending approvals (admin)
  [bold cyan]/approve <id>[/bold cyan]               Approve a request (admin)
  [bold cyan]/reject <id>[/bold cyan]                Reject a request (admin)
  [bold cyan]/resume <id>[/bold cyan]                Execute an approved command once
  [bold cyan]/p1 status|elevate <incident>|revoke[/bold cyan]   P1 elevation controls
  [bold cyan]/stream on|off[/bold cyan]              Toggle streaming output
  [bold cyan]/exit[/bold cyan], [bold cyan]/quit[/bold cyan]   Exit

[bold]Sample queries (ReAct engine)[/bold]
  • "Search for connect errors in nginx logs"
  • "Read the Nginx recovery runbook"
  • "Show diff for updated configuration"
  • "Restart nginx service"   (triggers Human-in-the-Loop approval)"""


class Repl:
    def __init__(self, user_id: str, gateway_url: str, model: str, stream: bool):
        self.client = Client(user_id, gateway_url)
        self.model = model
        self.stream = stream
        # approval_id -> (command, session_id) for /resume within this process
        self.pending: Dict[str, Tuple[str, str]] = {}
        self._readline: Optional[object] = None
        self._setup_readline()

    # -- readline history --------------------------------------------------
    def _setup_readline(self):
        try:
            import readline  # noqa: F401  (Unix)

            self._readline = readline
            try:
                readline.read_history_file(str(HISTORY_FILE))
            except FileNotFoundError:
                pass
            except OSError:
                pass
        except Exception:
            self._readline = None

    def _save_history(self):
        if self._readline is None:
            return
        try:
            self._readline.write_history_file(str(HISTORY_FILE))
        except OSError:
            pass

    def _remember(self, line: str):
        if self._readline is not None and line.strip():
            try:
                self._readline.add_history(line.strip())
            except Exception:
                pass

    # -- UI ---------------------------------------------------------------
    def _banner(self):
        console.print(
            "\n[bold cyan]╔══════════════════════════════════════════════════════════════════╗[/bold cyan]"
        )
        console.print(
            "[bold cyan]║          Sysadmin AI Assistant — Interactive Terminal            ║[/bold cyan]"
        )
        console.print(
            "[bold cyan]╚══════════════════════════════════════════════════════════════════╝[/bold cyan]"
        )
        token = "Loaded" if get_user_token(self.client.user_id) else "None"
        stream = "on" if self.stream else "off"
        console.print(
            f"[dim]Gateway: {self.client.gateway_url} | User: [bold white]{self.client.user_id}[/bold white] "
            f"(Token: {token}) | Streaming: {stream}[/dim]"
        )
        console.print("[dim]Type a sysadmin query, or /help for commands.[/dim]\n")

    def _prompt(self) -> str:
        user = self.client.user_id
        session = self.client.session_id or "new"
        try:
            line = Prompt.ask(
                f"[bold green]{user}[/bold green] [dim]({session[:12]})[/dim] [bold cyan]>[/bold cyan] "
            )
        except (KeyboardInterrupt, EOFError):
            raise
        self._remember(line)
        return line

    # -- dispatch ----------------------------------------------------------
    def run(self):
        self._banner()
        while True:
            try:
                raw = self._prompt()
            except KeyboardInterrupt:
                console.print("\n[dim]Session terminated.[/dim]")
                break
            except EOFError:
                console.print("\n[dim]Session terminated.[/dim]")
                break

            line = raw.strip()
            if not line:
                continue
            try:
                if not self._dispatch(line):
                    break
            except GatewayUnreachable as exc:
                console.print(
                    f"[red]Gateway unreachable: {exc}. "
                    f"Is the platform running? ([bold]./platform.sh start[/bold])[/red]"
                )
            self._save_history()
            console.print("")

    def _dispatch(self, line: str) -> bool:
        """Handle a line; return False to exit the REPL."""
        if line in ("/exit", "/quit", "exit", "quit"):
            console.print("[dim]Exiting Sysadmin Assistant session.[/dim]")
            return False

        if line == "/help":
            console.print(Panel(HELP_TEXT, title="[bold blue]Assistant Help[/bold blue]"))
            return True

        if line == "/users":
            cmd_users(self.client)
            return True

        if line.startswith("/user "):
            target = line.split(" ", 1)[1].strip()
            if self.client.switch_user(target):
                console.print(f"[green]Switched to {target}.[/green]")
            else:
                console.print(f"[red]No provisioned key for '{target}'. See /users.[/red]")
            return True

        if line == "/whoami":
            role = role_for_user(self.client.user_id)
            console.print(f"[bold]{self.client.user_id}[/bold] (role: [bold cyan]{role}[/bold cyan])")
            return True

        if line == "/tools":
            cmd_tools(self.client)
            return True

        if line == "/sessions":
            cmd_sessions(self.client)
            return True

        if line.startswith("/session rm "):
            cmd_delete_session(self.client, line.split(" ", 2)[2].strip())
            return True

        if line.startswith("/session "):
            target = line.split(" ", 1)[1].strip()
            response = self.client.get(f"/api/v1/agent/sessions/{target}", timeout=5.0)
            if response.status_code == 200:
                self.client.session_id = target
                console.print(f"[green]Continuing session {target}.[/green]")
            else:
                console.print("[yellow]Session not found for this user.[/yellow]")
            return True

        if line == "/new":
            sid = self.client.new_session()
            console.print(f"[green]Started new session {sid}.[/green]")
            return True

        if line == "/status":
            cmd_status(self.client)
            return True

        if line.startswith("/logs"):
            parts = line.split(" ", 1)
            cmd_logs(self.client, parts[1].strip() if len(parts) > 1 else None)
            return True

        if line == "/health":
            cmd_health(self.client)
            return True

        if line == "/approvals":
            cmd_approvals(self.client)
            return True

        if line.startswith("/approve ") or line.startswith("/reject "):
            action, approval_id = line.split(" ", 1)
            cmd_decide(self.client, approval_id.strip(), action == "/approve")
            return True

        if line.startswith("/resume "):
            cmd_resume(self.client, line.split(" ", 1)[1].strip(), self.pending)
            return True

        if line.startswith("/p1"):
            parts = line.split()
            if len(parts) == 2 and parts[1] == "status":
                cmd_p1(self.client, "status")
            elif len(parts) == 2 and parts[1] == "revoke":
                cmd_p1(self.client, "revoke")
            elif len(parts) >= 3 and parts[1] == "elevate":
                cmd_p1(self.client, "elevate", parts[2])
            else:
                console.print("[yellow]Usage: /p1 status|elevate <incident>|revoke[/yellow]")
            return True

        if line.startswith("/stream "):
            value = line.split(" ", 1)[1].strip().lower()
            if value in ("on", "off"):
                self.stream = value == "on"
                console.print(f"[green]Streaming {'enabled' if self.stream else 'disabled'}.[/green]")
            else:
                console.print("[yellow]Usage: /stream on|off[/yellow]")
            return True

        if line.startswith("/"):
            console.print(f"[yellow]Unknown command '{line.split()[0]}'. Try /help.[/yellow]")
            return True

        # Otherwise: natural-language query to the ReAct agent.
        self._handle_chat(line)
        return True

    def _handle_chat(self, prompt: str):
        result = run_chat(self.client, prompt, self.model, self.stream)
        if result and result.get("approval_required"):
            appr_id = result.get("approval_id")
            command = result.get("command")
            if appr_id:
                self.pending[appr_id] = (command or "", self.client.session_id or "")


# --------------------------------------------------------------------------
# One-shot subcommands
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sysadmin-cli",
        description="Sysadmin AI Assistant terminal client (REPL by default).",
    )
    parser.add_argument("--user", default=DEFAULT_USER, help="Identity to act as (default: %(default)s)")
    parser.add_argument("--gateway", default=GATEWAY_URL, help="Gateway base URL (default: %(default)s)")
    parser.add_argument("--model", default="fast-model", help="Model alias (fast-model|heavy-model)")
    parser.add_argument("--stream", dest="stream", action="store_true", default=None,
                        help="Stream output (SSE). Default in interactive mode.")
    parser.add_argument("--no-stream", dest="stream", action="store_false",
                        help="Disable streaming output.")

    sub = parser.add_subparsers(dest="command")

    chat = sub.add_parser("chat", help="Send a one-shot prompt to the agent")
    chat.add_argument("prompt", nargs="*", help="Prompt text")
    chat.add_argument("--session", help="Continue a specific session ID")
    chat.add_argument("--json", action="store_true", help="Emit raw JSON response (no streaming)")

    sub.add_parser("tools", help="List registered agent tools")
    sub.add_parser("users", help="List provisioned identities")
    sub.add_parser("approvals", help="List pending approvals (admin)")

    sessions = sub.add_parser("sessions", help="List sessions")
    sessions.add_argument("--delete", metavar="ID", help="Delete a session")
    sessions.add_argument("--show", metavar="ID", help="Show a session's details")

    decide = sub.add_parser("decide", help="Approve/reject an approval (admin)")
    decide.add_argument("approval_id")
    group = decide.add_mutually_exclusive_group(required=True)
    group.add_argument("--approve", action="store_true")
    group.add_argument("--reject", action="store_true")

    sub.add_parser("status", help="Show platform service status")
    sub.add_parser("health", help="Check service health")

    p1 = sub.add_parser("p1", help="P1 elevation controls")
    p1.add_argument("action", choices=["status", "elevate", "revoke"])
    p1.add_argument("incident", nargs="?", help="Incident ID (for elevate)")

    return parser


def dispatch_subcommand(args) -> int:
    client = Client(args.user, args.gateway)
    stream = args.stream if args.stream is not None else True

    if args.command == "chat":
        prompt = " ".join(args.prompt).strip()
        if not prompt:
            console.print("[yellow]No prompt provided. Reading from stdin...[/yellow]")
            prompt = sys.stdin.read().strip()
        if not prompt:
            console.print("[red]Empty prompt.[/red]")
            return 1
        if args.session:
            client.session_id = args.session
        if args.json:
            result = run_chat(client, prompt, args.model, stream=False, render=False)
            if result:
                console.print_json(data=result)
            return 0 if result else 1
        result = run_chat(client, prompt, args.model, stream=stream)
        return 0 if result is not None else 1

    if args.command == "tools":
        cmd_tools(client)
        return 0

    if args.command == "users":
        cmd_users(client)
        return 0

    if args.command == "sessions":
        if args.delete:
            cmd_delete_session(client, args.delete)
        elif args.show:
            response = client.get(f"/api/v1/agent/sessions/{args.show}", timeout=5.0)
            if response.status_code == 200:
                console.print_json(data=response.json())
            else:
                console.print("[yellow]Session not found.[/yellow]")
                return 1
        else:
            cmd_sessions(client)
        return 0

    if args.command == "approvals":
        cmd_approvals(client)
        return 0

    if args.command == "decide":
        cmd_decide(client, args.approval_id, args.approve)
        return 0

    if args.command == "status":
        cmd_status(client)
        return 0

    if args.command == "health":
        cmd_health(client)
        return 0

    if args.command == "p1":
        cmd_p1(client, args.action, args.incident)
        return 0

    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        stream = args.stream if args.stream is not None else True
        repl = Repl(args.user, args.gateway, args.model, stream)
        repl.run()
        return 0

    try:
        return dispatch_subcommand(args)
    except GatewayUnreachable as exc:
        console.print(
            f"[red]Gateway unreachable: {exc}. "
            f"Is the platform running? ([bold]./platform.sh start[/bold])[/red]"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
