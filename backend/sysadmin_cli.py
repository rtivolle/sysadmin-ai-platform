#!/usr/bin/env python3
"""
Sysadmin AI Assistant — Interactive Terminal Client
Connects to Traefik Reverse Proxy & Agent Platform with trusted ForwardAuth Bearer tokens,
demonstrates server-side ReAct agent reasoning, bounded log searches, linting/diffs,
runbook lookups, and interactive Human-in-the-Loop approvals.
"""
import os
import sys
import time
import json
import subprocess
from typing import Optional, Dict, Tuple
from pathlib import Path

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.markdown import Markdown
from rich.prompt import Prompt

console = Console()

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://127.0.0.1:8080")
DEFAULT_USER = os.getenv("SYSADMIN_USER", "sysadmin-01")
CLI_SESSION_ID: Optional[str] = None
PENDING_OPERATIONS: Dict[str, Tuple[str, str]] = {}

ROOT_DIR = Path(__file__).resolve().parent
if (ROOT_DIR / "backend").is_dir():
    KEYS_DIR = ROOT_DIR / "backend" / "config" / "keys"
else:
    KEYS_DIR = ROOT_DIR / "config" / "keys"

def get_user_token(user_id: str) -> Optional[str]:
    """Retrieves provisioned API key for the user from config/keys/."""
    key_file = KEYS_DIR / ("master.key" if user_id == "sysadmin-admin" else f"{user_id}.key")
    if key_file.is_file():
        try:
            return key_file.read_text().strip()
        except Exception:
            pass
    return None

USER_TOKEN = get_user_token(DEFAULT_USER)

def get_auth_headers(user_id: str = DEFAULT_USER) -> dict:
    headers = {"Content-Type": "application/json"}
    token = get_user_token(user_id) or USER_TOKEN
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers

def check_gateway() -> bool:
    try:
        r = httpx.get(f"{GATEWAY_URL}/api/tools/list", headers=get_auth_headers(DEFAULT_USER), timeout=2.0)
        return r.status_code == 200
    except Exception:
        return False

def execute_chat(user_msg: str, user_id: str):
    """Sends user prompt to server-side ReAct agent runtime via Traefik Gateway."""
    global CLI_SESSION_ID
    url = f"{GATEWAY_URL}/api/v1/agent/chat"
    payload = {
        "session_id": CLI_SESSION_ID,
        "prompt": user_msg,
        "workspace": f"./backend/data/workspaces/{user_id}",
        "model": "fast-model"
    }

    with console.status("[bold cyan]Agent reasoning in ReAct loop...[/bold cyan]", spinner="dots"):
        try:
            r = httpx.post(url, json=payload, headers=get_auth_headers(user_id), timeout=35.0)
        except Exception as e:
            console.print(f"[red]Error contacting agent gateway: {e}[/red]")
            return

    if r.status_code == 200:
        data = r.json()
        CLI_SESSION_ID = data.get("session_id", CLI_SESSION_ID)

        # 1. Print Tool Executions
        tools = data.get("tools_executed", [])
        for t in tools:
            st = t.get("status")
            color = "green" if st == "success" else ("yellow" if st == "approval_required" else "red")
            dur = t.get("duration_ms", 0)
            console.print(f"[dim]⚡ [Agent Tool] Invoked [bold]{t.get('tool')}[/bold] (Status: [{color}]{st}[/{color}], Duration: {dur}ms)[/dim]")

        # 2. Display Assistant Markdown
        console.print(Panel(Markdown(data.get("response", "")), title="[bold cyan]Sysadmin Assistant[/bold cyan]"))

        # 3. Handle Approval Requirement
        if data.get("approval_required"):
            appr_id = data.get("approval_id")
            cmd = data.get("command")
            PENDING_OPERATIONS[appr_id] = (cmd, CLI_SESSION_ID)
            console.print(Panel(
                f"[bold red]⚠ HUMAN-IN-THE-LOOP APPROVAL REQUIRED[/bold red]\n\n"
                f"[bold]Command:[/bold] `{cmd}`\n"
                f"[bold]Approval ID:[/bold] `{appr_id}`\n"
                f"[dim]Expires in 300 seconds.[/dim]",
                title="[bold yellow]Security Interceptor Alert[/bold yellow]",
                border_style="yellow"
            ))
            console.print(f"[yellow]Ask an administrator to review this ID, then run /resume {appr_id}.[/yellow]")
    elif r.status_code == 429:
        console.print("[bold red]Rate limit or quota ceiling exceeded (HTTP 429). Please wait before retrying.[/bold red]")
    elif r.status_code == 401:
        console.print(f"[bold red]Authentication failed (HTTP 401). Check Bearer token in config/keys/{user_id}.key[/bold red]")
    else:
        console.print(f"[red]Agent error (HTTP {r.status_code}): {r.text}[/red]")

def main():
    console.print("\n[bold cyan]╔══════════════════════════════════════════════════════════════════╗[/bold cyan]")
    console.print("[bold cyan]║          Sysadmin AI Assistant — Interactive Terminal            ║[/bold cyan]")
    console.print("[bold cyan]╚══════════════════════════════════════════════════════════════════╝[/bold cyan]")
    console.print(f"[dim]Connected via Gateway: {GATEWAY_URL} | Active User: [bold white]{DEFAULT_USER}[/bold white] (Token: {'Loaded' if USER_TOKEN else 'None'})[/dim]")

    if not check_gateway():
        console.print("[yellow][!] Warning: Gateway at http://127.0.0.1:8080 is not responding.[/yellow]")
        console.print("[yellow]    Please ensure backend is started: [bold]./platform.sh start[/bold][/yellow]\n")

    console.print("[dim]Type your sysadmin query, or /help for commands.[/dim]\n")

    while True:
        try:
            prompt = Prompt.ask(f"[bold green]{DEFAULT_USER}[/bold green] [bold cyan]>[/bold cyan]")
            query = prompt.strip()
            if not query:
                continue

            if query in ["/exit", "/quit", "exit", "quit"]:
                console.print("[dim]Exiting Sysadmin Assistant session.[/dim]")
                break

            elif query == "/help":
                console.print(Panel(
                    "[bold]Available Commands & Actions:[/bold]\n"
                    "  [bold cyan]/tools[/bold cyan]            - List all registered agent tools\n"
                    "  [bold cyan]/status[/bold cyan]           - Check backend connectivity and service statuses\n"
                    "  [bold cyan]/approvals[/bold cyan]        - List pending Human-in-the-Loop approvals\n"
                    "  [bold cyan]/approve ID[/bold cyan]       - Administrator approves a request\n"
                    "  [bold cyan]/reject ID[/bold cyan]        - Administrator rejects a request\n"
                    "  [bold cyan]/resume ID[/bold cyan]        - Execute your approved request once\n"
                    "  [bold cyan]/exit[/bold cyan]             - Exit assistant\n\n"
                    "[bold]Sample Natural Language Queries (ReAct Engine):[/bold]\n"
                    "  • 'Search for connect errors in nginx logs'\n"
                    "  • 'Read the Nginx recovery runbook'\n"
                    "  • 'Show diff for updated configuration'\n"
                    "  • 'Restart nginx service'",
                    title="[bold blue]Assistant Help[/bold blue]"
                ))

            elif query == "/tools":
                res = httpx.get(f"{GATEWAY_URL}/api/tools/list", headers=get_auth_headers(DEFAULT_USER), timeout=2.0)
                if res.status_code == 200:
                    t_table = Table(title="[bold green]Registered Sysadmin Agent Tools[/bold green]")
                    t_table.add_column("Tool Name", style="bold cyan")
                    t_table.add_column("Description")
                    for t in res.json().get("tools", []):
                        t_table.add_row(t["name"], t["description"])
                    console.print(t_table)
                else:
                    console.print("[red]Unable to fetch tools list.[/red]")

            elif query == "/status":
                subprocess.run(["./platform.sh", "status"], cwd=str(ROOT_DIR))

            elif query == "/approvals":
                res = httpx.get(f"{GATEWAY_URL}/api/approvals/pending", headers=get_auth_headers(DEFAULT_USER), timeout=2.0)
                if res.status_code == 200:
                    apprs = res.json().get("pending_approvals", [])
                    if apprs:
                        for a in apprs:
                            console.print(f"• ID: [bold yellow]{a['approval_id']}[/bold yellow] | User: {a['user_id']} | Command: [red]{a['command']}[/red] | Reason: {a['reason']}")
                    else:
                        console.print("[green]No pending approvals.[/green]")
                else:
                    console.print("[red]Failed to query approvals.[/red]")

            elif query.startswith("/approve ") or query.startswith("/reject "):
                action, approval_id = query.split(" ", 1)
                response = httpx.post(
                    f"{GATEWAY_URL}/api/approvals/decide",
                    json={"approval_id": approval_id.strip(), "approved": action == "/approve"},
                    headers=get_auth_headers(DEFAULT_USER), timeout=5.0,
                )
                console.print(f"[green]{response.json()}[/green]" if response.status_code == 200 else f"[red]{response.text}[/red]")

            elif query.startswith("/resume "):
                approval_id = query.split(" ", 1)[1].strip()
                operation = PENDING_OPERATIONS.get(approval_id)
                if not operation:
                    console.print("[red]Unknown approval in this CLI session.[/red]")
                else:
                    command, session_id = operation
                    response = httpx.post(
                        f"{GATEWAY_URL}/api/tools/execute",
                        json={"name": "sandboxed_bash", "session_id": session_id,
                              "parameters": {"command": command, "approval_id": approval_id}},
                        headers=get_auth_headers(DEFAULT_USER), timeout=25.0,
                    )
                    if response.status_code == 200:
                        PENDING_OPERATIONS.pop(approval_id, None)
                        console.print(response.json())
                    else:
                        console.print(f"[red]{response.text}[/red]")

            else:
                execute_chat(query, DEFAULT_USER)

            console.print("")

        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Session terminated.[/dim]")
            break

if __name__ == "__main__":
    main()
