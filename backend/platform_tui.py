#!/usr/bin/env python3
"""
Sysadmin AI Platform — Real-Time TUI Operations Monitor
Displays live status of all 8 services, host CPU/RAM, GPU VRAM & thermals,
pending Human-in-the-Loop approvals, and VictoriaLogs audit event stream.
"""
import os
import sys
import time
import json
import httpx
import shutil
import subprocess
from datetime import datetime
from typing import Dict, Any, List

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.live import Live
from rich.text import Text

console = Console()

SERVICES = [
    {"name": "traefik", "port": 8080, "desc": "Reverse Proxy & TLS"},
    {"name": "litellm", "port": 4000, "desc": "Quota & Token Gateway"},
    {"name": "agent_tools", "port": 3080, "desc": "Agent Platform & Tools"},
    {"name": "auth_gateway", "port": 3081, "desc": "ForwardAuth Identity"},
    {"name": "inference", "port": 8000, "desc": "Inference Engine / vLLM"},
    {"name": "seaweedfs", "port": 8333, "desc": "S3 Storage & Workspaces"},
    {"name": "victorialogs", "port": 9428, "desc": "Forensic Audit DB"},
    {"name": "valkey", "port": 6379, "desc": "Atomic State & Counters"}
]

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RUN_DIR = os.path.join(ROOT_DIR, "backend/run")

def get_service_status() -> List[Dict[str, Any]]:
    results = []
    for s in SERVICES:
        pid_file = os.path.join(RUN_DIR, f"{s['name']}.pid")
        is_running = False
        pid = None
        mem_rss = "N/A"
        if os.path.exists(pid_file):
            try:
                with open(pid_file, "r") as f:
                    pid_str = f.read().strip()
                if pid_str:
                    pid = int(pid_str)
                    # Check if process is alive
                    os.kill(pid, 0)
                    is_running = True
                    # Get RSS memory
                    res = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], stdout=subprocess.PIPE, text=True)
                    if res.returncode == 0 and res.stdout.strip():
                        mem_kb = int(res.stdout.strip())
                        mem_rss = f"{mem_kb / 1024:.1f} MB"
            except Exception:
                is_running = False

        results.append({
            "name": s["name"],
            "desc": s["desc"],
            "port": s["port"],
            "running": is_running,
            "pid": str(pid) if is_running else "-",
            "memory": mem_rss
        })
    return results

def get_gpu_status() -> List[Dict[str, Any]]:
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return []
    try:
        cmd = [nvidia_smi, "--query-gpu=name,memory.total,memory.used,temperature.gpu,utilization.gpu", "--format=csv,noheader,nounits"]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=2)
        if res.returncode != 0:
            return []
        gpus = []
        for line in res.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                gpus.append({
                    "name": parts[0],
                    "total_mb": int(parts[1]),
                    "used_mb": int(parts[2]),
                    "temp": float(parts[3]),
                    "util": int(parts[4])
                })
        return gpus
    except Exception:
        return []

def get_pending_approvals() -> List[Dict[str, Any]]:
    try:
        r = httpx.get("http://127.0.0.1:3080/api/approvals/pending", timeout=1.0)
        if r.status_code == 200:
            return r.json().get("pending_approvals", [])
    except Exception:
        pass
    return []

def get_recent_audit_events() -> List[Dict[str, Any]]:
    outbox = os.path.join(ROOT_DIR, "backend/data/victorialogs/outbox.jsonl")
    events = []
    if os.path.exists(outbox):
        try:
            with open(outbox, "r") as f:
                lines = f.readlines()[-6:]
            for l in lines:
                if l.strip():
                    events.append(json.loads(l.strip()))
        except Exception:
            pass
    return events

def make_layout() -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="main", ratio=1),
        Layout(name="footer", size=3)
    )
    layout["main"].split_row(
        Layout(name="left", ratio=2),
        Layout(name="right", ratio=3)
    )
    layout["right"].split_column(
        Layout(name="hardware", size=8),
        Layout(name="approvals", size=6),
        Layout(name="audit", ratio=1)
    )
    return layout

def render_dashboard(layout: Layout):
    # Header
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    layout["header"].update(
        Panel(f"[bold cyan]Sysadmin AI Platform — Operations Control Center[/bold cyan] [dim]| Host: {os.uname().nodename} | {now}[/dim]", border_style="cyan")
    )

    # Left: Services Table
    services = get_service_status()
    table = Table(title="[bold blue]Backend Core Services[/bold blue]", expand=True)
    table.add_column("Service", style="bold")
    table.add_column("Port")
    table.add_column("Status")
    table.add_column("PID")
    table.add_column("RAM RSS")

    for s in services:
        status_text = "[bold green]ONLINE[/bold green]" if s["running"] else "[bold red]STOPPED[/bold red]"
        table.add_row(s["name"], str(s["port"]), status_text, s["pid"], s["memory"])

    layout["left"].update(Panel(table, border_style="blue"))

    # Hardware Panel
    gpus = get_gpu_status()
    hw_text = ""
    if gpus:
        for i, g in enumerate(gpus):
            pct = (g["used_mb"] / g["total_mb"]) * 100
            bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
            hw_text += f"[bold]GPU {i}: {g['name']}[/bold]\n"
            hw_text += f"  VRAM: [{bar}] {g['used_mb']}/{g['total_mb']} MB ({pct:.1f}%)\n"
            hw_text += f"  Temp: {g['temp']}°C | GPU Util: {g['util']}%\n"
    else:
        hw_text = "[yellow]No NVIDIA GPU detected. CPU Emulation Mode Active.[/yellow]\n"

    layout["hardware"].update(Panel(hw_text.strip(), title="[bold green]Hardware Accelerators & VRAM[/bold green]", border_style="green"))

    # Pending Approvals Panel
    approvals = get_pending_approvals()
    if approvals:
        appr_table = Table(expand=True, box=None)
        appr_table.add_column("ID", style="bold yellow")
        appr_table.add_column("User")
        appr_table.add_column("Command", style="bold red")
        for a in approvals[:3]:
            appr_table.add_row(a["approval_id"], a["user_id"], a["command"][:40])
        layout["approvals"].update(Panel(appr_table, title="[bold red]Pending Human-in-the-Loop Approvals[/bold red]", border_style="red"))
    else:
        layout["approvals"].update(Panel("[green]✔ No mutating commands waiting for approval.[/green]", title="[bold green]Approval Gate[/bold green]", border_style="green"))

    # Audit Stream
    events = get_recent_audit_events()
    audit_table = Table(expand=True, box=None)
    audit_table.add_column("Time", style="dim")
    audit_table.add_column("User")
    audit_table.add_column("Tool")
    audit_table.add_column("Exit")
    audit_table.add_column("Command / Detail")

    for ev in events:
        audit_table.add_row(
            ev.get("timestamp", "").split("T")[-1].replace("Z", ""),
            ev.get("user_id", ""),
            ev.get("tool_name", ""),
            str(ev.get("exit_code", 0)),
            ev.get("command", "")[:35]
        )

    layout["audit"].update(Panel(audit_table, title="[bold magenta]Recent Forensic Audit Events (VictoriaLogs)[/bold magenta]", border_style="magenta"))

    # Footer
    layout["footer"].update(
        Panel("[dim]Press [bold white]Ctrl+C[/bold white] to exit dashboard | Run [bold white]./platform.sh test[/bold white] for full verification suite[/dim]", border_style="dim")
    )

def main():
    if "--once" in sys.argv:
        layout = make_layout()
        render_dashboard(layout)
        console.print(layout)
        return

    layout = make_layout()
    with Live(layout, refresh_per_second=1, screen=True) as live:
        try:
            while True:
                render_dashboard(layout)
                time.sleep(1)
        except KeyboardInterrupt:
            pass

if __name__ == "__main__":
    main()
