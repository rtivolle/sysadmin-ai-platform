#!/usr/bin/env python3
"""
Interactive TUI Installer & Configurator for Sysadmin AI Platform
Features:
- Device & Hardware Surveying (GPUs, CUDA, Topology, RAM, cgroups v2, bwrap)
- Interactive TUI step-by-step wizard (rich)
- Configuration generation (Traefik, LiteLLM, Valkey, SeaweedFS, VictoriaLogs, bwrap)
- Non-interactive / unattended CLI mode support
"""
import os
import sys
import time
import json
import shutil
import argparse
import subprocess
import urllib.parse
import yaml
from typing import Dict, Any

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt, Confirm, IntPrompt
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.layout import Layout
from rich.text import Text

# Import our hardware survey engine
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from backend.services.hardware_survey import run_hardware_survey, export_survey_reports
from backend.config.roles import connectivity_check, render_config

console = Console()

BANNER = """[bold cyan]
███████╗██╗   ██╗███████╗ █████╗ ██████╗ ███╗   ███╗██╗███╗   ██╗     █████╗ ██╗
██╔════╝╚██╗ ██╔╝██╔════╝██╔══██╗██╔══██╗████╗ ████║██║████╗  ██║    ██╔══██╗██║
███████╗ ╚████╔╝ ███████╗███████║██║  ██║██╔████╔██║██║██╔██╗ ██║    ███████║██║
╚════██║  ╚██╔╝  ╚════██║██╔══██║██║  ██║██║╚██╔╝██║██║██║╚██╗██║    ██╔══██║██║
███████║   ██║   ███████║██║  ██║██████╔╝██║ ╚═╝ ██║██║██║ ╚████║    ██║  ██║██║
╚══════╝   ╚═╝   ╚══════╝╚═╝  ╚═╝╚═════╝ ╚═╝     ╚═╝╚═╝╚═╝  ╚═══╝    ╚═╝  ╚═╝╚═╝
[bold white]   On-Premises Sysadmin AI Platform — TUI Installer & Configurator[/bold white]
[/bold cyan]"""

def display_hardware_survey(survey: Dict[str, Any]):
    console.print(Panel(BANNER, border_style="cyan"))

    # GPU Table
    if survey["gpus"]:
        table = Table(title="[bold green]Hardware Survey — Detected NVIDIA GPUs[/bold green]", show_header=True, header_style="bold magenta")
        table.add_column("Index", width=6)
        table.add_column("Model", style="bold")
        table.add_column("Architecture")
        table.add_column("VRAM Total")
        table.add_column("VRAM Free")
        table.add_column("Temp")
        table.add_column("Driver")

        for g in survey["gpus"]:
            table.add_row(
                str(g["index"]),
                g["name"],
                f"{g['architecture']} (v{g['compute_capability']})",
                f"{g['vram_total_mb']} MB",
                f"[green]{g['vram_free_mb']} MB[/green]",
                f"{g['temperature_c']}°C",
                g["driver_version"]
            )
        console.print(table)
    else:
        console.print("[yellow][!] No NVIDIA GPUs detected via nvidia-smi. Emulation or Remote Mode available.[/yellow]\n")

    # Host & Isolation Status
    cpu = survey["cpu"]
    ram = survey["ram"]
    sb = survey["sandbox_confinement"]
    cg = survey["cgroups_v2"]

    host_status = (
        f"[bold]Operating System:[/bold] {survey['os']['distro']} ({survey['os']['release']} {survey['os']['machine']})\n"
        f"[bold]Processor:[/bold] {cpu['model']} ({cpu['cores_logical']} logical cores)\n"
        f"[bold]System Memory:[/bold] {ram['total_mb']} MB Total ({ram['available_mb']} MB Available)\n"
        f"[bold]Bubblewrap Sandbox:[/bold] [{'green' if sb['functional'] else 'red'}]{'Ready (Hermetic Linux Namespaces)' if sb['functional'] else 'Not Functional'}[/]\n"
        f"[bold]cgroups v2:[/bold] [{'green' if cg.get('supported') else 'yellow'}]{', '.join(cg.get('controllers', [])) if cg.get('supported') else 'Inactive'}[/]"
    )
    console.print(Panel(host_status, title="[bold blue]Host Environment & Security Sandbox[/bold blue]"))

    rec = survey["inference_recommendation"]
    rec_box = (
        f"[bold]Suggested Inference Mode:[/bold] [green]{rec['recommended_mode']}[/green]\n"
        f"[bold]Suggested Fast Model:[/bold] {rec['fast_model']} (TP={rec['tp_fast']})\n"
        f"[bold]Suggested Heavy Model:[/bold] {rec['heavy_model']} (TP={rec['tp_heavy']})\n"
        f"[bold]Recommended Context Limit:[/bold] {rec['max_context']} tokens\n"
        f"[bold]Sizing Advice:[/bold] {' '.join(rec['notes'])}"
    )
    console.print(Panel(rec_box, title="[bold green]Hardware Recommendations[/bold green]"))

def _is_loopback(host: Any) -> bool:
    value = str(host or "").strip()
    return value == "" or value.startswith("127.") or value in ("localhost", "::1")


def _platform_url_host(platform_url: Any) -> str:
    """Host part of a PLATFORM_URL (``https://<platform-ip>:3080`` -> ``<platform-ip>``)."""
    try:
        return urllib.parse.urlparse(str(platform_url or "").strip()).hostname or ""
    except ValueError:
        return ""


def _inference_data_host(cfg: Dict[str, Any]) -> str:
    """Effective data host for the inference role.

    Mirrors install.sh: an explicit --peer-data wins; otherwise the host is
    derived from the platform URL (same machine in the reference topology).
    """
    peer = str(cfg.get("peer_data_host") or "").strip()
    if not _is_loopback(peer):
        return peer
    return _platform_url_host(cfg.get("platform_url"))


def validate_role_config(cfg: Dict[str, Any]) -> str:
    """Return a fail-closed error for an unusable split, or None.

    Mirrors install.sh: a split host bound to loopback, or one whose peers are
    loopback, is a deployment that cannot work, so it must not be applied.
    """
    role = cfg.get("role", "all")
    if role == "all":
        return None
    if _is_loopback(cfg.get("lan_bind_ip")):
        return f"Role '{role}' needs this machine's LAN address (--lan-bind-ip <ip>)."
    if role == "web" and _is_loopback(cfg.get("peer_inference_host")):
        return "Role 'web' needs the inference host (--peer-inference <ip>)."
    if role == "web" and _is_loopback(cfg.get("peer_data_host")):
        return "Role 'web' needs the data host (--peer-data <ip>)."
    if role == "inference":
        if not str(cfg.get("platform_url") or "").strip():
            return "Role 'inference' needs the platform URL (--platform-url <url>)."
        if _is_loopback(_inference_data_host(cfg)):
            return "Role 'inference' needs the platform (data) host (--peer-data <ip>)."
    return None


def run_role_prompt(defaults_only: bool = False, cli: Dict[str, Any] = None) -> Dict[str, Any]:
    """Ask which machine role this host plays and its peer addresses (PR-H1, Phase B).

    Runs first in the wizard (docs/plans/MULTI_HOST_DEPLOYMENT.md §6): role,
    then peer addresses, then a connectivity check before any config is applied.
    Values given on the command line (--role/--peer-*/--lan-bind-ip) are never
    re-asked, so a split install can run unattended through the TUI.
    """
    cli = cli or {}
    cfg = {
        "role": "all",
        "lan_bind_ip": "127.0.0.1",
        "peer_inference_host": "127.0.0.1",
        "peer_inference_port": 4000,
        "peer_data_host": "127.0.0.1",
        "peer_data_valkey_port": 6379,
        "peer_data_logs_port": 9428,
        "peer_data_seaweedfs_port": 8333,
        "peer_inference_hosts": "",
        "platform_url": "",
        "node_name": "",
    }
    for key in cfg:
        if cli.get(key) not in (None, ""):
            cfg[key] = cli[key]

    console.print("\n[bold cyan]═══════════ Machine Role (multi-host) ═══════════[/bold cyan]")

    if cli.get("role"):
        console.print(f"[dim]Role from the command line: {cfg['role']}.[/dim]")
        error = validate_role_config(cfg)
        if error:
            console.print(f"[red]{error}[/red]")
            raise SystemExit(2)
        return cfg

    if defaults_only:
        console.print("[dim]Unattended: role = all (single host).[/dim]")
        return cfg

    console.print("  [bold cyan]1.[/bold cyan] [bold]all[/bold]        single host — everything (today's default)")
    console.print("  [bold cyan]2.[/bold cyan] [bold]web[/bold]        application tier (Traefik, agent platform, sandbox)")
    console.print("  [bold cyan]3.[/bold cyan] [bold]inference[/bold]  GPU node (inference engine + node-agent, secretless)")
    console.print("  [bold cyan]4.[/bold cyan] [bold]data[/bold]       state tier (Valkey, VictoriaLogs, SeaweedFS)")
    console.print("  [bold cyan]5.[/bold cyan] [bold]platform[/bold]   data+admin tier (state + agent + LiteLLM + fleet control)")
    choice = Prompt.ask("Machine role", choices=["1", "2", "3", "4", "5"], default="1")
    role = {"1": "all", "2": "web", "3": "inference", "4": "data", "5": "platform"}[choice]
    cfg["role"] = role

    if role != "all":
        cfg["lan_bind_ip"] = Prompt.ask("LAN bind address for this machine", default="10.0.0.10")

    if role == "web":
        cfg["peer_inference_host"] = Prompt.ask("Inference peer (I) LAN address", default="10.0.0.11")
        cfg["peer_data_host"] = Prompt.ask("Data peer (D) LAN address", default="10.0.0.12")
    elif role == "inference":
        # No default: the platform URL is required and must not silently point
        # at an example address.
        cfg["platform_url"] = Prompt.ask("Platform URL (fleet API, e.g. https://<platform-ip>:3080)")
        cfg["node_name"] = Prompt.ask("Node name", default="gpu-01")
        cfg["peer_data_host"] = Prompt.ask(
            "Data peer (D) LAN address (empty = same host as the platform URL)",
            default="",
        )
    elif role == "platform":
        cfg["peer_inference_hosts"] = Prompt.ask(
            "GPU node bootstrap list, comma-separated (empty ok)",
            default="",
        )
    # data serves only; it has no peers to reach.

    error = validate_role_config(cfg)
    if error:
        console.print(f"[red]{error}[/red]")
        raise SystemExit(2)
    return cfg


def _peer_env(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Map the TUI cfg keys onto the connectivity/render env names.

    For `all` every address is forced to loopback: a peer address left over from
    an earlier split would otherwise make a single-host install talk to machines
    that are no longer part of the deployment.
    """
    role = cfg.get("role", "all")
    env = {
        "ROLE": role,
        "LAN_BIND_IP": cfg.get("lan_bind_ip", "127.0.0.1"),
        "PEER_INFERENCE_HOST": cfg.get("peer_inference_host", "127.0.0.1"),
        "PEER_INFERENCE_PORT": str(cfg.get("peer_inference_port", 4000)),
        "PEER_DATA_HOST": cfg.get("peer_data_host", "127.0.0.1"),
        "PEER_DATA_VALKEY_PORT": str(cfg.get("peer_data_valkey_port", 6379)),
        "PEER_DATA_LOGS_PORT": str(cfg.get("peer_data_logs_port", 9428)),
        "PEER_DATA_SEAWEEDFS_PORT": str(cfg.get("peer_data_seaweedfs_port", 8333)),
        "PLATFORM_URL": str(cfg.get("platform_url") or ""),
        "NODE_NAME": str(cfg.get("node_name") or ""),
        "PEER_INFERENCE_HOSTS": str(cfg.get("peer_inference_hosts") or ""),
    }
    if role == "all":
        env.update({
            "LAN_BIND_IP": "127.0.0.1",
            "PEER_INFERENCE_HOST": "127.0.0.1",
            "PEER_INFERENCE_PORT": "4000",
            "PEER_DATA_HOST": "127.0.0.1",
            "PEER_DATA_VALKEY_PORT": "6379",
            "PEER_DATA_LOGS_PORT": "9428",
            "PEER_DATA_SEAWEEDFS_PORT": "8333",
            "PLATFORM_URL": "",
            "NODE_NAME": "",
            "PEER_INFERENCE_HOSTS": "",
        })
    if role == "inference":
        # install.sh semantics: --peer-data wins, otherwise the data host is
        # derived from the platform URL (same machine in the reference topology).
        env["PEER_DATA_HOST"] = _inference_data_host(cfg) or "127.0.0.1"
    return env


def write_deployment_env(cfg: Dict[str, Any], config_dir: str) -> str:
    """Write backend/config/roles/deployment.env and return its path.

    Written for every role, including `all` (which means "one machine, all
    loopback"), so the choice is recorded, greppable and reversible.
    """
    roles_dir = os.path.join(config_dir, "roles")
    os.makedirs(roles_dir, exist_ok=True)
    env_path = os.path.join(roles_dir, "deployment.env")
    env = _peer_env(cfg)
    with open(env_path, "w", encoding="utf-8") as handle:
        handle.write(
            "# Generated by installer_tui.py (PR-H1 / Phase B). Machine-specific; git-ignored.\n"
            "# ROLE=all means one machine: every service, all binds loopback.\n"
            + "".join(f"{key}={value}\n" for key, value in env.items())
        )
    return env_path


def check_peer_connectivity(cfg: Dict[str, Any]) -> bool:
    """Fail-closed TCP check to required peers. Returns True when reachable."""
    role = cfg.get("role", "all")
    env = _peer_env(cfg)
    failures = connectivity_check.check_role(role, env)
    if not failures:
        return True
    console.print(f"[red]Connectivity check FAILED for role '{role}':[/red]")
    for label, host, port in failures:
        console.print(f"[red]  - cannot reach {label} at {host}:{port}[/red]")
    staged = {
        "platform": "platform first, then the GPU nodes",
        "inference": "the platform host first, then this GPU node",
    }.get(role, "D -> I -> W")
    console.print(f"[red]No configuration was applied. Bring peers up ({staged}) and retry.[/red]")
    return False


def run_interactive_wizard(survey: Dict[str, Any], defaults_only: bool = False, role: str = "all") -> Dict[str, Any]:
    """Asks only the questions this machine's role actually needs.

    Role-appropriate subsets (docs/plans/MULTI_HOST_DEPLOYMENT.md §6): the
    inference questions belong to `all`/`inference`, the workspace and quota
    questions to `all`/`web`/`platform`, and each port question only to the
    roles that serve it. Skipped questions keep their defaults, so the saved configuration
    snapshot stays complete for every role.
    """
    cfg = {}
    ask_inference = role in ("all", "inference")
    ask_workspaces = role in ("all", "web", "platform")

    console.print("\n[bold yellow]═══════════ Step 1: Inference Engine Configuration ═══════════[/bold yellow]")

    rec = survey["inference_recommendation"]
    default_mode = "remote_vllm" if not survey["gpus"] else ("local_gpu" if len(survey["gpus"]) >= 1 else "emulated")

    if not ask_inference:
        console.print(f"[dim]Role '{role}' does not run the inference engine; keeping the hardware recommendation.[/dim]")

    if defaults_only or not ask_inference:
        cfg["inference_mode"] = default_mode
        cfg["upstream_vllm_url"] = "http://127.0.0.1:8000" if default_mode != "remote_vllm" else "http://gpu-cluster:8000"
        cfg["fast_model"] = rec["fast_model"]
        cfg["heavy_model"] = rec["heavy_model"]
    else:
        console.print("[dim]Select how model inference will be executed:[/dim]")
        console.print("  [bold cyan]1.[/bold cyan] [bold]Local GPU Inference[/bold] (Uses local NVIDIA GPU via local vLLM/engine)")
        console.print("  [bold cyan]2.[/bold cyan] [bold]Remote vLLM Cluster[/bold] (Points to dedicated 8× RTX 8000 cluster)")
        console.print("  [bold cyan]3.[/bold cyan] [bold]Zero-GPU Emulated Mode[/bold] (Fast simulated streaming for dev/CI)")

        choice = Prompt.ask("Choose inference mode", choices=["1", "2", "3"], default="1" if survey["gpus"] else "2")
        mode_map = {"1": "local_gpu", "2": "remote_vllm", "3": "emulated"}
        cfg["inference_mode"] = mode_map[choice]

        if cfg["inference_mode"] == "remote_vllm":
            cfg["upstream_vllm_url"] = Prompt.ask("Remote vLLM Base URL", default="http://gpu-cluster:8000")
        elif cfg["inference_mode"] == "local_gpu":
            cfg["upstream_vllm_url"] = Prompt.ask("Local vLLM / Engine Port/URL", default="http://127.0.0.1:8000")
        else:
            cfg["upstream_vllm_url"] = ""

        cfg["fast_model"] = Prompt.ask("Fast Model (Triage/Syntax)", default=rec["fast_model"])
        cfg["heavy_model"] = Prompt.ask("Heavy Model (Complex reasoning)", default=rec["heavy_model"])

    console.print("\n[bold yellow]═══════════ Step 2: Network & Gateway Ports ═══════════[/bold yellow]")
    cfg["traefik_http_port"] = 8080
    cfg["traefik_https_port"] = 8443
    cfg["traefik_dash_port"] = 8081
    cfg["litellm_port"] = 4000
    cfg["valkey_port"] = 6379
    cfg["seaweedfs_s3_port"] = 8333
    cfg["victorialogs_port"] = 9428
    cfg["agent_port"] = 3080
    cfg["auth_port"] = 3081
    if not defaults_only:
        # Only the ports this role serves are worth asking about; the rest keep
        # their defaults so the snapshot stays complete.
        if role in ("all", "web", "platform"):
            cfg["traefik_http_port"] = IntPrompt.ask("Traefik HTTP Gateway Port", default=8080)
            cfg["traefik_https_port"] = IntPrompt.ask("Traefik HTTPS Gateway Port", default=8443)
            cfg["traefik_dash_port"] = IntPrompt.ask("Traefik Dashboard Port", default=8081)
            cfg["agent_port"] = IntPrompt.ask("Agent Platform API Port", default=3080)
            cfg["auth_port"] = IntPrompt.ask("ForwardAuth Port", default=3081)
        if role in ("all", "platform"):
            # LiteLLM moved to the platform tier in the GPU-fleet split (Phase B).
            cfg["litellm_port"] = IntPrompt.ask("LiteLLM Proxy Port", default=4000)
        if role in ("all", "data", "platform"):
            cfg["valkey_port"] = IntPrompt.ask("Valkey State & Cache Port", default=6379)
            cfg["seaweedfs_s3_port"] = IntPrompt.ask("SeaweedFS S3 Port", default=8333)
            cfg["victorialogs_port"] = IntPrompt.ask("VictoriaLogs Audit Port", default=9428)

    console.print("\n[bold yellow]═══════════ Step 3: Storage & Workspaces ═══════════[/bold yellow]")
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if not ask_workspaces:
        console.print(f"[dim]Workspaces live on the web host; keeping the default path.[/dim]")
    if defaults_only or not ask_workspaces:
        cfg["workspaces_dir"] = os.path.join(base_dir, "backend/data/workspaces")
        cfg["retention_days"] = 90
    else:
        cfg["workspaces_dir"] = Prompt.ask("Per-User Workspaces Directory", default=os.path.join(base_dir, "backend/data/workspaces"))
        cfg["retention_days"] = IntPrompt.ask("Audit Log Retention (Days)", default=90)

    console.print("\n[bold yellow]═══════════ Step 4: Sysadmin Users & Resource Quotas ═══════════[/bold yellow]")
    if not ask_workspaces:
        console.print(f"[dim]Quotas are enforced by LiteLLM/ForwardAuth (web/inference); keeping the defaults.[/dim]")
    if defaults_only or not ask_workspaces:
        cfg["num_users"] = 10
        cfg["max_parallel_per_user"] = 2
        cfg["rpm_limit"] = 60
        cfg["tpm_limit"] = 150000
        cfg["daily_token_budget"] = 2000000
        cfg["emergency_p1_user"] = "emergency-p1-oncall"
    else:
        cfg["num_users"] = IntPrompt.ask("Number of Sysadmin Accounts", default=10)
        cfg["max_parallel_per_user"] = IntPrompt.ask("Max Parallel Requests per User", default=2)
        cfg["rpm_limit"] = IntPrompt.ask("Rate Limit Requests Per Minute (RPM)", default=60)
        cfg["tpm_limit"] = IntPrompt.ask("Token Limit Per Minute (TPM)", default=150000)
        cfg["daily_token_budget"] = IntPrompt.ask("Daily Token Budget per User", default=2000000)
        cfg["emergency_p1_user"] = Prompt.ask("Emergency P1 On-Call Username", default="emergency-p1-oncall")

    return cfg

def apply_configuration(cfg: Dict[str, Any], root_dir: str):
    """Writes the configuration files this machine's role actually serves."""
    backend_dir = os.path.join(root_dir, "backend")
    config_dir = os.path.join(backend_dir, "config")
    data_dir = os.path.join(backend_dir, "data")
    keys_dir = os.path.join(config_dir, "keys")

    role = cfg.get("role", "all")
    serves_traefik = role in ("all", "web", "platform")
    # LiteLLM moved to the platform tier in the GPU-fleet split (Phase B).
    serves_litellm = role in ("all", "platform")
    serves_valkey = role in ("all", "data", "platform")

    def write_config(path: str, content: str, enabled: bool) -> None:
        """Write a generated config only on the roles that consume it."""
        if not enabled:
            console.print(f"[dim]Skipping {os.path.relpath(path, root_dir)} (role '{role}' does not serve it).[/dim]")
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        console=console
    ) as progress:
        task1 = progress.add_task("[cyan]Writing Traefik configurations...", total=100)

        # 1. Traefik Static Config
        traefik_static = f"""# Traefik Static Configuration (Generated by TUI Installer)
global:
  checkNewVersion: false
  sendAnonymousUsage: false

api:
  dashboard: false
  insecure: false

entryPoints:
  web:
    address: ":{cfg['traefik_http_port']}"
  websecure:
    address: ":{cfg['traefik_https_port']}"
  traefik:
    address: ":{cfg['traefik_dash_port']}"

providers:
  file:
    filename: "./backend/config/traefik/dynamic.yml"
    watch: true

log:
  level: INFO
  format: common
"""
        write_config(os.path.join(config_dir, "traefik/traefik.yml"), traefik_static, serves_traefik)
        progress.update(task1, advance=50)

        # 2. Traefik Dynamic Config
        # Preserve the checked-in security middleware and route definitions.
        dynamic_path = os.path.join(config_dir, "traefik/dynamic.yml")
        with open(dynamic_path, "r", encoding="utf-8") as f:
            traefik_dynamic = yaml.safe_load(f)
        http_config = traefik_dynamic["http"]
        http_config["middlewares"]["auth-forward"]["forwardAuth"]["address"] = (
            f"http://127.0.0.1:{cfg['auth_port']}/verify"
        )
        for service, port in {
            "auth-service": cfg["auth_port"],
            "agent-service": cfg["agent_port"],
            "litellm-service": cfg["litellm_port"],
            "s3-service": cfg["seaweedfs_s3_port"],
            "audit-service": cfg["victorialogs_port"],
        }.items():
            http_config["services"][service]["loadBalancer"]["servers"][0]["url"] = f"http://127.0.0.1:{port}"
        if serves_traefik:
            with open(dynamic_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(traefik_dynamic, f, sort_keys=False)
        else:
            console.print(f"[dim]Skipping traefik/dynamic.yml (role '{role}' does not serve it).[/dim]")
        progress.update(task1, advance=50)

        # 3. LiteLLM Config
        task2 = progress.add_task("[green]Configuring LiteLLM & Valkey Quota Policies...", total=100)
        litellm_path = os.path.join(config_dir, "litellm/config.yaml")
        with open(litellm_path, "r", encoding="utf-8") as f:
            litellm_config = yaml.safe_load(f)
        for model_entry, model_id in zip(litellm_config["model_list"], (cfg["fast_model"], cfg["heavy_model"])):
            model_entry["litellm_params"]["model"] = f"openai/{model_id}"
        litellm_config["general_settings"].pop("database_url", None)
        litellm_config["general_settings"].pop("master_key", None)
        litellm_config["general_settings"]["custom_auth"] = (
            "backend.services.auth_gateway.litellm_auth.sysadmin_custom_auth"
        )
        litellm_config["litellm_settings"]["max_parallel_requests_per_user"] = cfg["max_parallel_per_user"]
        litellm_config["litellm_settings"]["callbacks"] = [
            "backend.services.auth_gateway.litellm_auth.proxy_handler_instance"
        ]
        if serves_litellm:
            with open(litellm_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(litellm_config, f, sort_keys=False)
        else:
            console.print(f"[dim]Skipping litellm/config.yaml (role '{role}' does not serve it).[/dim]")
        progress.update(task2, advance=50)

        # 4. Valkey Config
        valkey_cfg = f"""# Valkey Configuration (Generated by TUI Installer)
bind 127.0.0.1
port {cfg['valkey_port']}
protected-mode yes
requirepass "CONFIGURE_VIA_PLATFORM_SH"
maxmemory 256mb
maxmemory-policy allkeys-lru
appendonly yes
appendfilename "appendonly.aof"
dir "./backend/data/valkey"
loglevel notice
logfile ""
daemonize no
"""
        write_config(os.path.join(config_dir, "valkey/valkey.conf"), valkey_cfg, serves_valkey)
        progress.update(task2, advance=50)

        # 5. Workspaces & Key Provisioning
        task3 = progress.add_task("[yellow]Provisioning Workspaces & Sysadmin Keys...", total=100)
        os.makedirs(keys_dir, exist_ok=True)

        if role in ("all", "web", "platform"):
            os.makedirs(cfg["workspaces_dir"], exist_ok=True)
            for i in range(1, cfg["num_users"] + 1):
                u_id = f"sysadmin-{i:02d}"
                u_workspace = os.path.join(cfg["workspaces_dir"], u_id)
                os.makedirs(u_workspace, exist_ok=True)
                os.chmod(u_workspace, 0o700)
                progress.update(task3, advance=int(80 / cfg["num_users"]))

            subprocess.run([os.path.join(keys_dir, "provision-keys.sh")], check=True)
            subprocess.run([sys.executable, os.path.join(keys_dir, "provision-logins.py")], check=True)
        else:
            progress.update(task3, advance=80)
            console.print("[dim]Workspaces and keys live on the web/platform host; copy keys from W (docs/multi-host.md).[/dim]")

        # Save platform configuration snapshot
        with open(os.path.join(config_dir, "platform_config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)

        progress.update(task3, advance=20)

    console.print("\n[bold green]✔ All configurations and security policies generated successfully![/bold green]")

def main():
    parser = argparse.ArgumentParser(description="Sysadmin AI Platform TUI Installer & Configurator")
    parser.add_argument("--unattended", "--default", action="store_true", help="Run unattended installation with automatic hardware-based defaults")
    parser.add_argument("--survey-only", action="store_true", help="Run hardware survey and exit")
    parser.add_argument("--start", action="store_true", help="Automatically launch all backend services after setup")
    parser.add_argument("--dry-run", action="store_true",
                        help="Resolve the role, show the plan and exit without writing anything")
    parser.add_argument("--role", choices=["all", "web", "inference", "data", "platform"],
                        help="Machine role: one host (all) or a split tier (web/inference/data/platform)")
    parser.add_argument("--lan-bind-ip", help="This machine's LAN address (required for every role except all)")
    parser.add_argument("--peer-inference", help="Inference host address (role web, compat PR-H1)")
    parser.add_argument("--peer-inference-port", type=int, help="LiteLLM port on the inference host (default 4000)")
    parser.add_argument("--peer-data", help="Data host address (roles web/inference, compat PR-H1)")
    parser.add_argument("--peer-data-valkey-port", type=int, help="Valkey port on the data host (default 6379)")
    parser.add_argument("--peer-data-logs-port", type=int, help="VictoriaLogs port on the data host (default 9428)")
    parser.add_argument("--peer-data-seaweedfs-port", type=int, help="SeaweedFS S3 port on the data host (default 8333)")
    parser.add_argument("--platform-url", help="Platform fleet API URL (role inference, Phase B)")
    parser.add_argument("--node-name", help="Fleet node name (role inference; default: hostname)")
    parser.add_argument("--peer-inference-hosts", help="GPU node bootstrap list, comma-separated (role platform)")
    args = parser.parse_args()
    role_cli = {
        "role": args.role,
        "lan_bind_ip": args.lan_bind_ip,
        "peer_inference_host": args.peer_inference,
        "peer_inference_port": args.peer_inference_port,
        "peer_data_host": args.peer_data,
        "peer_data_valkey_port": args.peer_data_valkey_port,
        "peer_data_logs_port": args.peer_data_logs_port,
        "peer_data_seaweedfs_port": args.peer_data_seaweedfs_port,
        "platform_url": args.platform_url,
        "node_name": args.node_name,
        "peer_inference_hosts": args.peer_inference_hosts,
    }

    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    data_dir = os.path.join(root_dir, "backend/data")

    # Step 1: Run Hardware Survey
    survey = run_hardware_survey()
    export_survey_reports(survey, data_dir)
    display_hardware_survey(survey)

    if args.survey_only:
        console.print("[green]Hardware survey completed. Reports saved in backend/data/.[/green]")
        return

    # Step 2: Wizard / Configuration Prompting
    role_cfg = run_role_prompt(defaults_only=args.unattended, cli=role_cli)
    cfg = run_interactive_wizard(survey, defaults_only=args.unattended, role=role_cfg["role"])
    cfg.update(role_cfg)

    # Driver changes remain a separate, explicit operation, including on hosts
    # whose NVIDIA cards are invisible to nvidia-smi until a driver is installed.
    if cfg["role"] in ("all", "inference") and not args.unattended:
        if Confirm.ask("Show NVIDIA driver/tool installation plan?", default=False):
            gpu_setup = [sys.executable, os.path.join(root_dir, "backend/scripts/nvidia_setup.py")]
            subprocess.run(gpu_setup, check=True)
            if Confirm.ask("Retrieve and install these system packages (sudo; reboot may be needed)?", default=False):
                subprocess.run(gpu_setup + ["--apply"], check=True)

    # Step 3: Confirmation Summary
    summary_table = Table(title="[bold cyan]Selected Platform Configuration Summary[/bold cyan]", show_header=True)
    summary_table.add_column("Parameter", style="bold")
    summary_table.add_column("Configured Value", style="green")

    summary_table.add_row("Machine Role", cfg["role"])
    if cfg["role"] != "all":
        summary_table.add_row("This Host's LAN Address", str(cfg["lan_bind_ip"]))
        if cfg["role"] in ("web",):
            summary_table.add_row("Inference Peer", f"{cfg['peer_inference_host']}:{cfg['peer_inference_port']}")
            summary_table.add_row("Data Peer", f"{cfg['peer_data_host']}:{cfg['peer_data_valkey_port']}")
        elif cfg["role"] == "inference":
            summary_table.add_row("Platform URL", str(cfg["platform_url"]))
            summary_table.add_row("Node Name", str(cfg["node_name"]) or "(hostname)")
            summary_table.add_row("Data Peer", f"{_inference_data_host(cfg)}:{cfg['peer_data_logs_port']}")
        elif cfg["role"] == "platform":
            hosts = str(cfg["peer_inference_hosts"] or "").strip() or "(fleet registry)"
            summary_table.add_row("GPU Bootstrap List", hosts)
    summary_table.add_row("Inference Mode", cfg["inference_mode"])
    summary_table.add_row("Upstream vLLM URL", cfg["upstream_vllm_url"] or "(Local simulated)")
    summary_table.add_row("Fast Model", cfg["fast_model"])
    summary_table.add_row("Heavy Model", cfg["heavy_model"])
    summary_table.add_row("Traefik HTTP / HTTPS", f":{cfg['traefik_http_port']} / :{cfg['traefik_https_port']}")
    summary_table.add_row("LiteLLM / Valkey Ports", f":{cfg['litellm_port']} / :{cfg['valkey_port']}")
    summary_table.add_row("SeaweedFS S3 / VictoriaLogs", f":{cfg['seaweedfs_s3_port']} / :{cfg['victorialogs_port']}")
    summary_table.add_row("Sysadmin Accounts", f"{cfg['num_users']} users ({cfg['rpm_limit']} RPM, {cfg['max_parallel_per_user']} in-flight)")
    summary_table.add_row("Workspaces Base Path", cfg["workspaces_dir"])
    console.print(summary_table)

    if args.dry_run:
        console.print("[yellow]Dry run: nothing was written, installed or started.[/yellow]")
        console.print(f"Re-run without --dry-run to apply the '{cfg['role']}' role.")
        return

    if not args.unattended:
        confirm = Confirm.ask("Apply configuration and finalize setup now?", default=True)
        if not confirm:
            console.print("[yellow]Installation canceled by user.[/yellow]")
            return

    config_dir = os.path.join(root_dir, "backend", "config")

    # Multi-host: fail closed on unreachable peers BEFORE the role is recorded
    # or any configuration is written (docs/plans/MULTI_HOST_DEPLOYMENT.md §6).
    if not check_peer_connectivity(cfg):
        return

    # Step 4: Apply Configuration
    write_deployment_env(cfg, config_dir)
    apply_configuration(cfg, root_dir)

    # Role-specific bind/upstream rendering. For `all` this reverts a previous
    # split's rendering, so switching back to one machine is a real choice.
    render_config.render_all(cfg["role"], _peer_env(cfg), config_dir)

    console.print("\n[bold white on blue] Platform Ready! [/bold white on blue]")
    if cfg["role"] == "all":
        console.print("Machine role: [bold]all[/bold] (one machine, everything local)")
    else:
        console.print(f"Machine role: [bold]{cfg['role']}[/bold] (switch back with ./install.sh --role all)")
    console.print("Commands:")
    console.print("  [bold green]./platform.sh start[/bold green]       # Start this role's services")
    console.print("  [bold green]./platform.sh dashboard[/bold green]   # Open live TUI Operations Monitor")
    console.print("  [bold green]./platform.sh chat[/bold green]        # Open interactive Sysadmin Agent CLI")
    console.print("  [bold green]./platform.sh test[/bold green]        # Run end-to-end verification test suite\n")

    if args.start or (not args.unattended and Confirm.ask("Would you like to start the platform now?", default=False)):
        console.print("[cyan]Launching platform services...[/cyan]")
        subprocess.run(["./platform.sh", "start"], cwd=root_dir)
        console.print("[green]All services started.[/green]")

if __name__ == "__main__":
    main()
