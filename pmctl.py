#!/usr/bin/env python3
"""
pmctl — Project Manager Control Tool
=====================================
Beheer, monitor en bestuur al je projecten vanuit één plek.

CLI-gebruik:
  pmctl list              # overzicht alle projecten
  pmctl status [naam]     # gedetailleerde status
  pmctl start <naam>      # project opstarten
  pmctl stop <naam>       # project stoppen
  pmctl restart <naam>    # herstart
  pmctl logs <naam>       # logs bekijken
  pmctl disk              # schijfruimte overzicht
  pmctl deps <naam>       # dependencies tonen
  pmctl web [--port 7777] # web dashboard
  pmctl add <naam> <pad>  # project toevoegen
  pmctl remove <naam>     # project verwijderen
"""

import json
import os
import re
import subprocess
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional, List, Dict, Any

# ── Optionele imports ─────────────────────────────────────────────────────────
try:
    import psutil
except ImportError:
    psutil = None

try:
    import typer
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
    from rich import box
    from rich.columns import Columns
    from rich.padding import Padding
    console = Console()
    app = typer.Typer(
        help="[bold green]pmctl[/] — Project Manager Control Tool",
        rich_markup_mode="rich",
        no_args_is_help=True,
    )
except ImportError:
    print("Ontbrekende dependencies. Voer uit:\n  pip install -r requirements.txt")
    sys.exit(1)

try:
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.middleware.cors import CORSMiddleware
    import uvicorn as _uvicorn
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

# ── Config ────────────────────────────────────────────────────────────────────
PMCTL_DIR = Path(__file__).parent.resolve()
PROJECTS_FILE = PMCTL_DIR / "projects.json"


def load_projects() -> Dict[str, Any]:
    if not PROJECTS_FILE.exists():
        return {}
    with open(PROJECTS_FILE) as f:
        return json.load(f).get("projects", {})


def save_projects(projects: Dict[str, Any]):
    with open(PROJECTS_FILE, "w") as f:
        json.dump({"projects": projects}, f, indent=2)


def get_project(name: str) -> Dict[str, Any]:
    projects = load_projects()
    if name not in projects:
        console.print(f"[red]✗  Project '[bold]{name}[/]' niet gevonden.[/]")
        console.print("   Gebruik [bold cyan]pmctl list[/] voor een overzicht.")
        raise typer.Exit(1)
    return projects[name]


# ── Procesdetectie ────────────────────────────────────────────────────────────
def find_processes(project: Dict) -> List:
    """Vind alle processen die bij dit project horen."""
    if not psutil:
        return []

    found: Dict[int, Any] = {}
    path = project.get("path", "")
    ports = project.get("ports", [])
    patterns = project.get("process_patterns", [])

    # Via poort (meest betrouwbaar)
    if ports:
        try:
            for conn in psutil.net_connections(kind="inet"):
                if conn.status == "LISTEN" and conn.laddr.port in ports and conn.pid:
                    try:
                        proc = psutil.Process(conn.pid)
                        found[proc.pid] = proc
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
        except (psutil.AccessDenied, PermissionError):
            pass

    # Via process_patterns + werkdirectory (alleen als er patronen zijn)
    if path and patterns:
        # Exacte padgrens: /project of /project/...  maar NIET /project-other
        path_norm = path.rstrip("/")
        try:
            for proc in psutil.process_iter(["pid", "cwd", "cmdline", "name"]):
                try:
                    cwd = proc.info.get("cwd") or ""
                    cmdline = " ".join(proc.info.get("cmdline") or [])
                    # Cwd moet exact in de projectmap zijn
                    cwd_match = cwd == path_norm or cwd.startswith(path_norm + "/")
                    # Cmdline moet een van de geconfigureerde patronen bevatten
                    pattern_match = any(p.lower() in cmdline.lower() for p in patterns)
                    if (cwd_match or pattern_match) and "pmctl" not in cmdline:
                        found[proc.pid] = proc
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass
        except (psutil.AccessDenied, PermissionError):
            pass

    return list(found.values())


def is_running(project: Dict) -> bool:
    return len(find_processes(project)) > 0


def get_memory_mb(project: Dict) -> float:
    total = 0.0
    for p in find_processes(project):
        try:
            total += p.memory_info().rss / 1024 / 1024
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return total


def get_open_ports(project: Dict) -> List[int]:
    if not psutil:
        return []
    ports = project.get("ports", [])
    result = []
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.status == "LISTEN" and conn.laddr.port in ports:
                result.append(conn.laddr.port)
    except (psutil.AccessDenied, PermissionError):
        pass
    return sorted(set(result))


def get_process_list(project: Dict) -> List[Dict]:
    procs = find_processes(project)
    result = []
    for p in procs:
        try:
            info = {
                "pid": p.pid,
                "name": p.name(),
                "cmdline": " ".join(p.cmdline() or [])[:80],
                "memory_mb": round(p.memory_info().rss / 1024 / 1024, 1),
                "cpu_percent": p.cpu_percent(interval=0.1),
            }
            result.append(info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return result


# ── Schijfruimte ──────────────────────────────────────────────────────────────
def get_disk_usage(project: Dict) -> str:
    path = project.get("path", "")
    if not path or not Path(path).exists():
        return "?"
    try:
        r = subprocess.run(
            ["du", "-sh", path], capture_output=True, text=True, timeout=15
        )
        return r.stdout.split()[0] if r.stdout.strip() else "?"
    except Exception:
        return "?"


# ── Port Registry integratie ──────────────────────────────────────────────────
REGISTRY_URL = "http://localhost:4444"
_registry_cache: Dict = {}
_registry_cache_time: float = 0
CACHE_TTL = 5  # seconden


def fetch_registry() -> Dict:
    """Haal alle services op uit het centraal register (gecached)."""
    global _registry_cache, _registry_cache_time
    now = time.time()
    if now - _registry_cache_time < CACHE_TTL and _registry_cache:
        return _registry_cache
    try:
        import urllib.request as _ur
        with _ur.urlopen(f"{REGISTRY_URL}/ports", timeout=2) as r:
            _registry_cache = json.loads(r.read())
            _registry_cache_time = now
            return _registry_cache
    except Exception:
        return _registry_cache  # verouderde cache of leeg


def resolve_project_ports(project: Dict) -> List[int]:
    """
    Geeft de poorten van een project — live uit het register als mogelijk,
    anders de hardcoded 'ports' lijst als fallback.
    """
    services = project.get("services", [])
    if not services:
        return project.get("ports", [])
    registry = fetch_registry()
    if not registry:
        return project.get("ports", [])
    ports = []
    for svc in services:
        if svc in registry:
            ports.append(registry[svc]["port"])
    return ports


def get_port_conflicts(projects: Dict) -> Dict[int, List[str]]:
    """
    Detecteer poortconflicten via het register (als actief),
    anders via hardcoded ports in projects.json.
    """
    registry = fetch_registry()
    port_map: Dict[int, List[str]] = {}

    for name, project in projects.items():
        ports = resolve_project_ports(project)
        for port in ports:
            port_map.setdefault(port, []).append(name)

    # Controleer ook register op conflicten (meerdere services, zelfde project kan OK zijn)
    if registry:
        svc_by_port: Dict[int, List[str]] = {}
        for svc, info in registry.items():
            p = info["port"]
            proj = info.get("project", "?")
            svc_by_port.setdefault(p, []).append(proj)
        for port, projs in svc_by_port.items():
            if len(set(projs)) > 1:  # twee VERSCHILLENDE projecten, zelfde poort
                for name, project in projects.items():
                    if port in resolve_project_ports(project):
                        port_map.setdefault(port, []).append(name)

    return {port: list(set(names)) for port, names in port_map.items() if len(set(names)) > 1}


# ── Token Usage uit logs ──────────────────────────────────────────────────────
TOKEN_PATTERNS = [
    r'"total_tokens"\s*:\s*(\d+)',
    r'"completion_tokens"\s*:\s*(\d+)',
    r'"prompt_tokens"\s*:\s*(\d+)',
    r'[Tt]otal[_ ]?[Tt]okens?\s*[:=]\s*(\d+)',
    r'[Tt]okens?\s+used\s*[:=]\s*(\d+)',
    r'input_tokens.*?(\d+)',
    r'output_tokens.*?(\d+)',
]


def parse_token_usage(project: Dict) -> int:
    path = project.get("path", "")
    if not path:
        return 0

    total = 0
    log_files = project.get("log_files", [])
    search_paths = [Path(path) / lf for lf in log_files]
    if not search_paths:
        search_paths = list(Path(path).glob("*.log"))

    for log_path in search_paths:
        if not log_path.exists():
            continue
        try:
            with open(log_path, "r", errors="ignore") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 150 * 1024))
                content = f.read()
            for pattern in TOKEN_PATTERNS:
                matches = re.findall(pattern, content)
                if matches:
                    total += sum(int(m) for m in matches[-100:])
                    break
        except Exception:
            pass

    return total


# ── Dependencies ──────────────────────────────────────────────────────────────
def get_dependencies(project: Dict) -> Dict[str, List[str]]:
    path = project.get("path", "")
    if not path:
        return {}

    result = {}
    base = Path(path)

    req = base / "requirements.txt"
    if req.exists():
        lines = [
            l.strip()
            for l in req.read_text(errors="ignore").splitlines()
            if l.strip() and not l.startswith("#")
        ]
        if lines:
            result["python"] = lines

    pyproj = base / "pyproject.toml"
    if pyproj.exists() and "python" not in result:
        content = pyproj.read_text(errors="ignore")
        deps = re.findall(r'"([a-zA-Z][^"]+[>=<][^"]*)"', content)
        if deps:
            result["python"] = deps

    pkg = base / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text())
            deps = list(data.get("dependencies", {}).keys())
            dev = list(data.get("devDependencies", {}).keys())
            if deps:
                result["node"] = deps
            if dev:
                result["node_dev"] = dev
        except Exception:
            pass

    return result


# ── Gecombineerde projectinfo ─────────────────────────────────────────────────
def get_project_info(name: str, project: Dict, include_disk: bool = True) -> Dict[str, Any]:
    # Poorten live uit register (of fallback hardcoded)
    ports = resolve_project_ports(project)
    # Zet ook terug in project zodat find_processes ze gebruikt
    project = {**project, "ports": ports}

    # Eén keer processen ophalen en hergebruiken
    procs = find_processes(project)
    running = len(procs) > 0

    # Geheugen uit al opgehaalde processen
    mem_mb = 0.0
    proc_list = []
    for p in procs:
        try:
            mem = p.memory_info().rss / 1024 / 1024
            mem_mb += mem
            proc_list.append({
                "pid": p.pid,
                "name": p.name(),
                "cmdline": " ".join(p.cmdline() or [])[:80],
                "memory_mb": round(mem, 1),
                "cpu_percent": 0,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # Open poorten
    open_ports = []
    if psutil and ports:
        try:
            for conn in psutil.net_connections(kind="inet"):
                if conn.status == "LISTEN" and conn.laddr.port in ports:
                    open_ports.append(conn.laddr.port)
        except (psutil.AccessDenied, PermissionError):
            pass

    # Poortconflicten
    all_projects = load_projects()
    conflicts = get_port_conflicts(all_projects)
    conflicting_ports = {p: conflicts[p] for p in ports if p in conflicts}

    return {
        "name": name,
        "description": project.get("description", ""),
        "tech": project.get("tech", ""),
        "path": project.get("path", ""),
        "status": "running" if running else "stopped",
        "ports": ports,
        "open_ports": sorted(set(open_ports)),
        "memory_mb": round(mem_mb, 1),
        "disk_usage": get_disk_usage(project) if include_disk else "...",
        "token_usage": parse_token_usage(project),
        "relations": project.get("relations", []),
        "dependencies": get_dependencies(project),
        "start_script": project.get("start_script"),
        "notes": project.get("notes", ""),
        "log_files": project.get("log_files", []),
        "processes": proc_list,
        "pid_count": len(procs),
        "port_conflicts": conflicting_ports,
        "pm2_name": project.get("pm2_name"),
    }


# ── Start / Stop ──────────────────────────────────────────────────────────────
def pm2_action(pm2_name: str, action: str) -> bool:
    """Voer een PM2-actie uit (start/stop/restart/status)."""
    try:
        r = subprocess.run(
            ["pm2", action, pm2_name],
            capture_output=True, text=True, timeout=15
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def do_start(name: str, project: Dict) -> bool:
    # PM2-beheerd project → via PM2
    pm2_name = project.get("pm2_name")
    if pm2_name:
        console.print(f"[cyan]▶  PM2 start: [bold]{pm2_name}[/]...")
        if pm2_action(pm2_name, "start"):
            console.print(f"[green]✓  {name} gestart via PM2.[/]")
            return True
        else:
            console.print(f"[yellow]⚠  PM2 start mislukt, probeer script...[/]")

    # Waarschuw bij poortconflicten vóór het starten
    all_projects = load_projects()
    conflicts = get_port_conflicts(all_projects)
    for port in resolve_project_ports(project):
        if port in conflicts:
            others = [p for p in conflicts[port] if p != name]
            console.print(
                f"[yellow]⚠  Poortconflict: :{port} wordt ook gebruikt door "
                f"[bold]{', '.join(others)}[/]. "
                f"Start één project tegelijk op deze poort.[/]"
            )

    path = project.get("path", "")
    script = project.get("start_script")

    if not script:
        console.print(f"\n[yellow]⚠  Geen start_script voor '[bold]{name}[/]'.[/]")
        notes = project.get("notes", "")
        if notes:
            console.print(Panel(notes, title="Opstartinstructies", border_style="yellow"))
        return False

    script_path = Path(path) / script
    if not script_path.exists():
        console.print(f"[red]✗  Script niet gevonden: {script_path}[/]")
        return False

    console.print(f"[cyan]▶  Starten: [bold]{name}[/] via [dim]{script}[/]...")

    try:
        proc = subprocess.Popen(
            ["/bin/bash", str(script_path)],
            cwd=path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        console.print(f"   [dim]Wachten op processen...[/]")
        for i in range(12):
            time.sleep(1)
            if is_running(project):
                mem = round(get_memory_mb(project), 1)
                open_ports = get_open_ports(project)
                console.print(f"[green]✓  {name} is online![/]  "
                               f"[dim]geheugen: {mem} MB  "
                               f"poorten: {open_ports or 'geen'}[/]")
                notes = project.get("notes", "")
                if notes:
                    console.print(f"   [dim]{notes}[/]")
                return True

        if proc.poll() is None:
            console.print(f"[yellow]⚠  Script draait (PID {proc.pid}), poorten nog niet open.[/]")
            return True
        else:
            console.print(f"[red]✗  Script gestopt (exit: {proc.returncode}). Controleer logs.[/]")
            return False
    except Exception as e:
        console.print(f"[red]✗  Fout bij starten: {e}[/]")
        return False


def do_stop(name: str, project: Dict) -> bool:
    # PM2-beheerd project → via PM2
    pm2_name = project.get("pm2_name")
    if pm2_name:
        console.print(f"[cyan]■  PM2 stop: [bold]{pm2_name}[/]...")
        if pm2_action(pm2_name, "stop"):
            console.print(f"[green]✓  {name} gestopt via PM2.[/]")
            return True
        console.print(f"[yellow]⚠  PM2 stop mislukt, probeer proces-kill...[/]")

    procs = find_processes(project)

    if not procs:
        console.print(f"[yellow]⚠  '{name}' draait niet.[/]")
        return True

    console.print(f"[cyan]■  Stoppen: [bold]{name}[/] ({len(procs)} proces(sen))...")

    for proc in procs:
        try:
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    time.sleep(2)

    still = find_processes(project)
    for proc in still:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    time.sleep(1)

    if not is_running(project):
        console.print(f"[green]✓  {name} gestopt.[/]")
        return True
    else:
        console.print(f"[red]✗  Kon {name} niet volledig stoppen.[/]")
        return False


# ── Logs lezen ────────────────────────────────────────────────────────────────
def read_logs(project: Dict, lines: int = 50) -> str:
    path = project.get("path", "")
    log_files = project.get("log_files", [])
    if not log_files:
        log_files = [str(p.relative_to(path)) for p in Path(path).glob("*.log")]
    if not log_files:
        return "(geen log-bestanden geconfigureerd)"

    output = []
    for lf in log_files:
        log_path = Path(path) / lf
        if not log_path.exists():
            continue
        try:
            result = subprocess.run(
                ["tail", f"-{lines}", str(log_path)],
                capture_output=True, text=True, timeout=5
            )
            if result.stdout.strip():
                output.append(f"── {lf} ──")
                output.append(result.stdout)
        except Exception:
            pass

    return "\n".join(output) if output else "(logs zijn leeg)"


# ═══════════════════════════════════════════════════════════════════════════════
# CLI COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

@app.command("list", help="Overzicht van alle projecten met status")
def cmd_list():
    projects = load_projects()
    if not projects:
        console.print("[yellow]Geen projecten geconfigureerd.[/]")
        return

    table = Table(
        box=box.ROUNDED,
        border_style="bright_black",
        header_style="bold cyan",
        show_lines=False,
        title="[bold green]pmctl[/] — Projectoverzicht",
        title_style="bold",
    )
    table.add_column("Project", style="bold white", min_width=18)
    table.add_column("Status", min_width=10)
    table.add_column("Geheugen", justify="right", min_width=9)
    table.add_column("Schijf", justify="right", min_width=7)
    table.add_column("Tokens", justify="right", min_width=8)
    table.add_column("Poorten", min_width=12)
    table.add_column("Relaties", style="dim magenta")
    table.add_column("Tech", style="dim")

    running_count = 0
    for name, project in projects.items():
        running = is_running(project)
        if running:
            running_count += 1
            status = "[bold green]● draait[/]"
            mem = f"{get_memory_mb(project):.0f} MB"
        else:
            status = "[red]○ gestopt[/]"
            mem = "—"

        disk = get_disk_usage(project)
        tokens = parse_token_usage(project)
        token_str = f"{tokens:,}" if tokens else "—"

        ports = project.get("ports", [])
        open_p = get_open_ports(project)
        port_str = " ".join(
            f"[green]:{p}[/]" if p in open_p else f"[dim]:{p}[/]"
            for p in ports
        ) if ports else "—"

        relations = ", ".join(project.get("relations", [])) or "—"
        tech = project.get("tech", "—")

        table.add_row(name, status, mem, disk, token_str, port_str, relations, tech)

    console.print()
    console.print(table)
    console.print(
        f"\n  [dim]{running_count}/{len(projects)} projecten actief   •   "
        f"[cyan]pmctl status <naam>[/] voor details   •   "
        f"[cyan]pmctl web[/] voor dashboard[/]\n"
    )


@app.command("ls", help="Alias voor list", hidden=True)
def cmd_ls():
    cmd_list()


@app.command("status", help="Gedetailleerde status van één of alle projecten")
def cmd_status(
    name: Optional[str] = typer.Argument(None, help="Projectnaam (leeg = alle)")
):
    projects = load_projects()
    targets = {name: get_project(name)} if name else projects

    for pname, project in targets.items():
        info = get_project_info(pname, project)
        running = info["status"] == "running"

        status_str = "[bold green]● DRAAIT[/]" if running else "[red]○ GESTOPT[/]"
        title = f"[bold cyan]{pname}[/]  {status_str}"

        lines = [
            f"[dim]Beschrijving:[/]  {info['description']}",
            f"[dim]Tech:[/]         {info['tech']}",
            f"[dim]Pad:[/]          {info['path']}",
            "",
            f"[dim]Geheugen:[/]     [bold]{info['memory_mb']} MB[/]",
            f"[dim]Schijf:[/]       [bold]{info['disk_usage']}[/]",
            f"[dim]Tokens:[/]       [bold]{info['token_usage']:,}[/]",
            "",
            f"[dim]Geconfigureerde poorten:[/]  {info['ports'] or '—'}",
            f"[dim]Open poorten:[/]             {info['open_ports'] or '—'}",
        ]

        if info["relations"]:
            lines.append(f"[dim]Relaties:[/]     [magenta]{', '.join(info['relations'])}[/]")

        if info["start_script"]:
            lines.append(f"[dim]Start-script:[/] [cyan]{info['start_script']}[/]")

        if info["processes"]:
            lines.append("")
            lines.append("[dim]Processen:[/]")
            for p in info["processes"][:5]:
                lines.append(
                    f"  [dim]PID {p['pid']}[/]  {p['name']}  "
                    f"[dim]{p['memory_mb']} MB  {p['cmdline'][:60]}[/]"
                )

        if info["notes"]:
            lines.append("")
            lines.append(f"[yellow]Notities:[/] {info['notes']}")

        console.print(Panel("\n".join(lines), title=title, border_style="cyan"))


@app.command("start", help="Project opstarten")
def cmd_start(
    name: str = typer.Argument(..., help="Naam van het project")
):
    project = get_project(name)
    if is_running(project):
        console.print(f"[yellow]⚠  '{name}' draait al.[/]")
        return
    do_start(name, project)


@app.command("stop", help="Project stoppen")
def cmd_stop(
    name: str = typer.Argument(..., help="Naam van het project")
):
    project = get_project(name)
    do_stop(name, project)


@app.command("restart", help="Project herstarten")
def cmd_restart(
    name: str = typer.Argument(..., help="Naam van het project")
):
    project = get_project(name)
    console.print(f"[cyan]↺  Herstarten: [bold]{name}[/]...[/]")
    do_stop(name, project)
    time.sleep(1)
    do_start(name, project)


@app.command("logs", help="Recente logs bekijken")
def cmd_logs(
    name: str = typer.Argument(..., help="Naam van het project"),
    lines: int = typer.Option(50, "--lines", "-n", help="Aantal regels"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Blijf volgen (tail -f)"),
):
    project = get_project(name)
    path = project.get("path", "")
    log_files = project.get("log_files", [])

    if not log_files:
        log_files = [str(p.relative_to(path)) for p in Path(path).glob("*.log")]

    if not log_files:
        console.print(f"[yellow]Geen log-bestanden gevonden voor '{name}'.[/]")
        return

    if follow and len(log_files) == 1:
        log_path = Path(path) / log_files[0]
        console.print(f"[dim]Volgen: {log_path}  (Ctrl+C om te stoppen)[/]\n")
        try:
            subprocess.run(["tail", "-f", str(log_path)])
        except KeyboardInterrupt:
            pass
        return

    content = read_logs(project, lines)
    console.print(Panel(content, title=f"Logs — [bold]{name}[/]", border_style="dim"))


@app.command("disk", help="Schijfruimteoverzicht van alle projecten")
def cmd_disk():
    projects = load_projects()
    if not projects:
        console.print("[yellow]Geen projecten.[/]")
        return

    table = Table(
        box=box.SIMPLE,
        header_style="bold cyan",
        title="[bold]Schijfruimte per project[/]",
    )
    table.add_column("Project", style="bold white")
    table.add_column("Pad")
    table.add_column("Grootte", justify="right", style="bold yellow")

    for name, project in projects.items():
        size = get_disk_usage(project)
        table.add_row(name, project.get("path", "?"), size)

    console.print()
    console.print(table)


@app.command("deps", help="Dependencies van een project tonen")
def cmd_deps(
    name: str = typer.Argument(..., help="Naam van het project")
):
    project = get_project(name)
    deps = get_dependencies(project)

    if not deps:
        console.print(f"[yellow]Geen dependencies gevonden voor '{name}'.[/]")
        return

    content_lines = []
    for kind, items in deps.items():
        labels = {
            "python": "[bold yellow]Python[/] (requirements.txt)",
            "node": "[bold green]Node.js[/] (dependencies)",
            "node_dev": "[bold blue]Node.js[/] (devDependencies)",
        }
        content_lines.append(labels.get(kind, kind))
        for item in items:
            content_lines.append(f"  [dim]·[/] {item}")
        content_lines.append("")

    console.print(Panel(
        "\n".join(content_lines).rstrip(),
        title=f"Dependencies — [bold]{name}[/]",
        border_style="cyan",
    ))


@app.command("add", help="Project toevoegen aan de lijst")
def cmd_add(
    name: str = typer.Argument(..., help="Naam voor het project"),
    path: str = typer.Argument(..., help="Absoluut pad naar de projectmap"),
):
    p = Path(path).resolve()
    if not p.exists():
        console.print(f"[red]✗  Pad bestaat niet: {p}[/]")
        raise typer.Exit(1)

    projects = load_projects()
    if name in projects:
        console.print(f"[yellow]⚠  '{name}' bestaat al. Gebruik een andere naam of verwijder het eerst.[/]")
        raise typer.Exit(1)

    # Auto-detect
    start_script = None
    for candidate in ["start.sh", "start-*.sh", "run.sh", "run_*.sh"]:
        matches = list(p.glob(candidate))
        if matches:
            start_script = matches[0].name
            break

    tech_parts = []
    if (p / "requirements.txt").exists() or (p / "pyproject.toml").exists():
        tech_parts.append("Python")
    if (p / "package.json").exists():
        tech_parts.append("Node.js")
    if not tech_parts:
        sh_scripts = list(p.glob("*.sh"))
        if sh_scripts:
            tech_parts.append("Bash")

    entry = {
        "path": str(p),
        "description": "",
        "tech": " + ".join(tech_parts) or "Onbekend",
        "start_script": start_script,
        "ports": [],
        "process_patterns": [],
        "relations": [],
        "log_files": [],
        "notes": "",
    }
    projects[name] = entry
    save_projects(projects)
    console.print(f"[green]✓  '{name}' toegevoegd.[/]")
    console.print(f"   [dim]Bewerk {PROJECTS_FILE} om poorten, relaties en notities in te stellen.[/]")


@app.command("remove", help="Project verwijderen uit de lijst")
def cmd_remove(
    name: str = typer.Argument(..., help="Naam van het project"),
    force: bool = typer.Option(False, "--force", "-f", help="Niet vragen om bevestiging"),
):
    get_project(name)  # check exists
    if not force:
        bevestig = typer.confirm(f"'{name}' verwijderen uit pmctl?", default=False)
        if not bevestig:
            console.print("[dim]Geannuleerd.[/]")
            return
    projects = load_projects()
    del projects[name]
    save_projects(projects)
    console.print(f"[green]✓  '{name}' verwijderd.[/]")


def _resolve_port(service: str, preferred: int) -> int:
    """Vraag poort op bij centraal register; fallback naar preferred."""
    try:
        import urllib.request, json as _json
        body = _json.dumps({
            "service": service, "project": "pmctl",
            "description": "pmctl web dashboard",
            "preferred_port": preferred,
        }).encode()
        req = urllib.request.Request(
            "http://localhost:4444/ports/request",
            data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=2) as r:
            return _json.loads(r.read())["port"]
    except Exception:
        return preferred  # Register niet actief → gebruik default


@app.command("web", help="Web dashboard starten (standaard poort 7777)")
def cmd_web(
    port: int = typer.Option(7777, "--port", "-p", help="Poort voor het dashboard"),
    host: str = typer.Option("0.0.0.0", "--host", help="Bind-adres"),
):
    if not HAS_FASTAPI:
        console.print("[red]✗  FastAPI niet geïnstalleerd. Voer uit: pip install fastapi uvicorn[/]")
        raise typer.Exit(1)

    resolved = _resolve_port("pmctl", port)
    if resolved != port:
        console.print(f"[dim]Port Registry: poort gewijzigd van :{port} naar :{resolved}[/]")
        port = resolved

    console.print(f"\n[bold green]pmctl Web Dashboard[/]")
    console.print(f"  [cyan]http://localhost:{port}[/]\n")
    console.print(f"  [dim]Ctrl+C om te stoppen[/]\n")

    web_app = build_fastapi_app()
    _uvicorn.run(web_app, host=host, port=port, log_level="warning")


# ═══════════════════════════════════════════════════════════════════════════════
# WEB SERVER (FastAPI)
# ═══════════════════════════════════════════════════════════════════════════════

def build_fastapi_app():
    web = FastAPI(title="pmctl", docs_url=None, redoc_url=None)
    web.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @web.get("/", response_class=HTMLResponse)
    def index():
        return HTML_TEMPLATE

    @web.get("/api/projects")
    def api_projects():
        projects = load_projects()

        def load_one(item):
            name, project = item
            return name, get_project_info(name, project)

        with ThreadPoolExecutor(max_workers=len(projects) or 1) as ex:
            result = dict(ex.map(load_one, projects.items()))
        return JSONResponse(result)

    @web.get("/api/projects/{name}")
    def api_project(name: str):
        projects = load_projects()
        if name not in projects:
            return JSONResponse({"error": "niet gevonden"}, status_code=404)
        return JSONResponse(get_project_info(name, projects[name]))

    @web.post("/api/projects/{name}/start")
    def api_start(name: str):
        projects = load_projects()
        if name not in projects:
            return JSONResponse({"success": False, "message": "niet gevonden"}, status_code=404)
        project = projects[name]
        if is_running(project):
            return JSONResponse({"success": False, "message": "draait al"})

        def _start():
            do_start(name, project)

        t = threading.Thread(target=_start, daemon=True)
        t.start()
        return JSONResponse({"success": True, "message": "gestart"})

    @web.post("/api/projects/{name}/stop")
    def api_stop(name: str):
        projects = load_projects()
        if name not in projects:
            return JSONResponse({"success": False, "message": "niet gevonden"}, status_code=404)
        ok = do_stop(name, projects[name])
        return JSONResponse({"success": ok})

    @web.post("/api/projects/{name}/restart")
    def api_restart(name: str):
        projects = load_projects()
        if name not in projects:
            return JSONResponse({"success": False, "message": "niet gevonden"}, status_code=404)
        project = projects[name]

        def _restart():
            do_stop(name, project)
            time.sleep(2)
            do_start(name, project)

        t = threading.Thread(target=_restart, daemon=True)
        t.start()
        return JSONResponse({"success": True, "message": "herstarten..."})

    @web.get("/api/projects/{name}/logs")
    def api_logs(name: str, lines: int = 100):
        projects = load_projects()
        if name not in projects:
            return JSONResponse({"error": "niet gevonden"}, status_code=404)
        content = read_logs(projects[name], lines)
        return JSONResponse({"content": content})

    return web


# ═══════════════════════════════════════════════════════════════════════════════
# HTML TEMPLATE
# ═══════════════════════════════════════════════════════════════════════════════

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="nl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>pmctl — Project Manager</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css" rel="stylesheet">
  <style>
    :root {
      --bg: #0d1117;
      --card: #161b22;
      --card-hover: #1c2333;
      --border: #30363d;
      --text: #c9d1d9;
      --muted: #8b949e;
      --green: #3fb950;
      --red: #f85149;
      --yellow: #e3b341;
      --blue: #58a6ff;
      --purple: #bc8cff;
      --cyan: #39d353;
    }
    * { box-sizing: border-box; }
    body { background: var(--bg); color: var(--text); font-family: -apple-system, 'Segoe UI', sans-serif; min-height: 100vh; }
    a { color: var(--blue); }

    /* Navbar */
    .navbar { background: var(--card) !important; border-bottom: 1px solid var(--border); }
    .brand { font-weight: 800; font-size: 1.1rem; color: var(--green) !important; letter-spacing: -0.5px; }
    .brand span { color: var(--text); font-weight: 400; }

    /* Cards */
    .proj-card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      transition: border-color 0.2s, transform 0.15s;
      height: 100%;
    }
    .proj-card:hover { border-color: var(--blue); transform: translateY(-2px); }
    .proj-card.running { border-left: 3px solid var(--green); }
    .proj-card.stopped { border-left: 3px solid #30363d; }

    .card-header-line {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 14px 16px 10px;
      border-bottom: 1px solid var(--border);
    }
    .proj-name { font-weight: 700; font-size: 1rem; color: var(--blue); }
    .proj-desc { font-size: 0.78rem; color: var(--muted); margin-bottom: 14px; line-height: 1.4; }
    .card-body-inner { padding: 14px 16px; }

    /* Status badge */
    .status-badge {
      display: inline-flex; align-items: center; gap: 5px;
      font-size: 0.72rem; font-weight: 600; padding: 3px 9px;
      border-radius: 20px; letter-spacing: 0.3px;
    }
    .status-running { background: rgba(63,185,80,0.15); color: var(--green); border: 1px solid rgba(63,185,80,0.3); }
    .status-stopped { background: rgba(248,81,73,0.12); color: var(--red); border: 1px solid rgba(248,81,73,0.2); }
    .dot-pulse { width: 6px; height: 6px; border-radius: 50%; background: currentColor; animation: pulse 2s infinite; }
    .dot-static { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
    @keyframes pulse { 0%,100% { opacity:1; transform:scale(1); } 50% { opacity:0.5; transform:scale(0.85); } }

    /* Stats */
    .stats-row { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; margin-bottom: 14px; }
    .stat-box {
      background: rgba(0,0,0,0.25); border-radius: 7px; padding: 8px 6px;
      text-align: center; border: 1px solid var(--border);
    }
    .stat-val { font-size: 0.95rem; font-weight: 700; color: var(--blue); }
    .stat-lbl { font-size: 0.6rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.8px; margin-top: 1px; }

    /* Tags */
    .tag { display: inline-block; font-size: 0.7rem; padding: 2px 7px; border-radius: 4px; font-weight: 600; margin: 1px; }
    .tag-port-open { background: rgba(88,166,255,0.15); color: var(--blue); border: 1px solid rgba(88,166,255,0.3); }
    .tag-port-closed { background: rgba(48,54,61,0.5); color: var(--muted); border: 1px solid var(--border); }
    .tag-relation { background: rgba(188,140,255,0.15); color: var(--purple); border: 1px solid rgba(188,140,255,0.3); }
    .tag-tech { background: rgba(227,179,65,0.1); color: var(--yellow); border: 1px solid rgba(227,179,65,0.2); }
    .tag-port-conflict { background: rgba(227,179,65,0.15); color: var(--yellow); border: 1px solid rgba(227,179,65,0.4); cursor: help; }

    /* Meta row */
    .meta-row { margin-bottom: 8px; }
    .meta-lbl { font-size: 0.65rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.7px; margin-bottom: 3px; }

    /* Action buttons */
    .action-row { display: flex; gap: 6px; margin-top: 14px; flex-wrap: wrap; }
    .btn-act {
      font-size: 0.75rem; font-weight: 600; padding: 5px 12px;
      border-radius: 6px; border: none; cursor: pointer; display: inline-flex;
      align-items: center; gap: 4px; transition: opacity 0.15s, transform 0.1s;
    }
    .btn-act:hover { opacity: 0.85; transform: scale(0.98); }
    .btn-act:disabled { opacity: 0.4; cursor: not-allowed; }
    .btn-start { background: var(--green); color: #000; }
    .btn-stop  { background: var(--red); color: #fff; }
    .btn-restart { background: var(--yellow); color: #000; }
    .btn-logs  { background: var(--border); color: var(--text); }

    /* Notes */
    .notes-box {
      background: rgba(227,179,65,0.06); border: 1px solid rgba(227,179,65,0.2);
      border-radius: 6px; padding: 8px 10px; font-size: 0.75rem;
      color: var(--yellow); margin-top: 10px; line-height: 1.5;
    }

    /* Log modal */
    .modal-content { background: var(--card); border: 1px solid var(--border); }
    .modal-header { border-bottom: 1px solid var(--border); }
    .log-box {
      background: #010409; border: 1px solid var(--border); border-radius: 6px;
      font-family: 'Cascadia Code', 'Fira Code', monospace; font-size: 0.78rem;
      max-height: 420px; overflow-y: auto; color: #7ee787; padding: 14px;
      white-space: pre-wrap; word-break: break-all; line-height: 1.55;
    }

    /* Topbar */
    .topbar-stat {
      display: inline-flex; align-items: center; gap: 6px;
      font-size: 0.78rem; color: var(--muted);
      background: rgba(0,0,0,0.3); border: 1px solid var(--border);
      border-radius: 20px; padding: 3px 12px;
    }
    .topbar-stat b { color: var(--text); }

    /* Grid */
    .proj-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; }

    /* Scrollbar */
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

    .refresh-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--green); display: inline-block; margin-right: 4px; }
    .refreshing { animation: spin 1s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .tab-btn { background: transparent; border: 1px solid var(--border); color: var(--muted); border-radius: 6px; padding: 4px 12px; font-size: 0.78rem; cursor: pointer; transition: all 0.15s; }
    .tab-btn:hover { border-color: var(--blue); color: var(--text); }
    .tab-btn.active { background: rgba(88,166,255,0.1); border-color: var(--blue); color: var(--blue); font-weight: 600; }
    .reg-table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
    .reg-table th { color: var(--muted); font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.8px; padding: 8px 12px; border-bottom: 1px solid var(--border); text-align: left; }
    .reg-table td { padding: 10px 12px; border-bottom: 1px solid rgba(48,54,61,0.5); vertical-align: middle; }
    .reg-table tr:hover td { background: rgba(255,255,255,0.02); }
    .reg-card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
    .reg-offline { color: var(--red); font-size: 0.78rem; padding: 40px; text-align: center; }
  </style>
</head>
<body>

<nav class="navbar navbar-dark sticky-top">
  <div class="container-fluid px-4">
    <a class="navbar-brand brand" href="#" onclick="showTab('projects')" style="cursor:pointer"><i class="bi bi-grid-3x3-gap-fill me-2"></i>pmctl <span>dashboard</span></a>
    <div class="d-flex align-items-center gap-3">
      <div class="d-flex gap-1">
        <button onclick="showTab('projects')" id="tab-projects" class="tab-btn active"><i class="bi bi-grid me-1"></i>Projecten</button>
        <button onclick="showTab('registry')" id="tab-registry" class="tab-btn"><i class="bi bi-diagram-3 me-1"></i>Port Register</button>
      </div>
      <span id="running-badge" class="topbar-stat"><b id="running-num">—</b> actief</span>
      <span id="last-update" class="topbar-stat"><i class="bi bi-arrow-repeat me-1" id="refresh-icon"></i><span id="update-time">laden...</span></span>
    </div>
  </div>
</nav>

<div class="container-fluid py-4 px-4">
  <div id="view-projects">
    <div id="proj-grid" class="proj-grid"><!-- kaarten komen hier --></div>
  </div>
  <div id="view-registry" style="display:none">
    <h5 style="color:var(--muted);margin-bottom:16px"><i class="bi bi-diagram-3 me-2"></i>Centraal Poortenregister <span style="font-size:0.75rem;color:var(--green)">● live via :4444</span></h5>
    <div id="registry-content">laden...</div>
  </div>
</div>

<!-- Log Modal -->
<div class="modal fade" id="logModal" tabindex="-1">
  <div class="modal-dialog modal-xl modal-dialog-scrollable">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title text-white" id="logTitle"><i class="bi bi-terminal me-2"></i>Logs</h5>
        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body">
        <div id="log-content" class="log-box">laden...</div>
      </div>
      <div class="modal-footer" style="border-top:1px solid var(--border);">
        <button class="btn btn-sm btn-secondary" onclick="refreshLogs()"><i class="bi bi-arrow-clockwise me-1"></i>Vernieuwen</button>
        <button class="btn btn-sm btn-secondary" data-bs-dismiss="modal">Sluiten</button>
      </div>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
<script>
let allProjects = {};
let activeLogProject = null;

function statusBadge(status) {
  if (status === 'running') {
    return '<span class="status-badge status-running"><span class="dot-pulse"></span>draait</span>';
  }
  return '<span class="status-badge status-stopped"><span class="dot-static"></span>gestopt</span>';
}

function portTags(ports, openPorts, conflicts) {
  if (!ports.length) return '<span class="text-muted" style="font-size:0.75rem">—</span>';
  return ports.map(p => {
    const open = openPorts.includes(p);
    const conflict = conflicts && conflicts[p];
    const icon = `<i class="bi bi-hdd-network" style="font-size:0.65rem"></i>`;
    if (conflict) {
      const others = conflict.join(', ');
      return `<span class="tag tag-port-conflict" title="⚠ Conflict met: ${others}">${icon} :${p} <i class="bi bi-exclamation-triangle-fill" style="font-size:0.6rem"></i></span>`;
    }
    if (open) {
      return `<a href="http://localhost:${p}" target="_blank" class="tag tag-port-open" style="text-decoration:none" title="Open in browser">${icon} :${p} <i class="bi bi-box-arrow-up-right" style="font-size:0.55rem;opacity:0.7"></i></a>`;
    }
    return `<span class="tag tag-port-closed" title="Poort niet open">${icon} :${p}</span>`;
  }).join('');
}

function relationTags(rels) {
  if (!rels.length) return '<span class="text-muted" style="font-size:0.75rem">—</span>';
  return rels.map(r => `<span class="tag tag-relation"><i class="bi bi-link-45deg" style="font-size:0.65rem"></i> ${r}</span>`).join('');
}

function depCount(deps) {
  let total = 0;
  for (const k of Object.keys(deps)) { total += deps[k].length; }
  return total;
}

function renderCard(name, info) {
  const running = info.status === 'running';
  const mem = running ? `${info.memory_mb} MB` : '—';
  const tokens = info.token_usage > 0 ? info.token_usage.toLocaleString('nl-NL') : '—';
  const disk = info.disk_usage || '—';

  const startBtn = !running
    ? `<button class="btn-act btn-start" onclick="doAction('${name}','start')"><i class="bi bi-play-fill"></i>Start</button>`
    : '';
  const stopBtn = running
    ? `<button class="btn-act btn-stop" onclick="doAction('${name}','stop')"><i class="bi bi-stop-fill"></i>Stop</button>`
    : '';
  const restartBtn = running
    ? `<button class="btn-act btn-restart" onclick="doAction('${name}','restart')"><i class="bi bi-arrow-counterclockwise"></i></button>`
    : '';
  const logsBtn = info.log_files && info.log_files.length
    ? `<button class="btn-act btn-logs" onclick="showLogs('${name}')"><i class="bi bi-file-text"></i>Logs</button>`
    : '';

  const notes = info.notes
    ? `<div class="notes-box"><i class="bi bi-info-circle me-1"></i>${info.notes}</div>`
    : '';

  const deps = depCount(info.dependencies || {});
  const depsStr = deps > 0 ? `${deps} packages` : '—';

  return `
    <div class="proj-card ${running ? 'running' : 'stopped'}" id="card-${name}">
      <div class="card-header-line">
        <div>
          <div class="proj-name"><i class="bi bi-folder2-open me-1" style="font-size:0.85rem"></i>${name}</div>
          <span class="tag tag-tech" style="font-size:0.65rem; margin-top:3px; display:inline-block">${info.tech || '?'}</span>
        </div>
        ${statusBadge(info.status)}
      </div>
      <div class="card-body-inner">
        <p class="proj-desc">${info.description || '(geen beschrijving)'}</p>

        <div class="stats-row">
          <div class="stat-box">
            <div class="stat-val">${mem}</div>
            <div class="stat-lbl">Geheugen</div>
          </div>
          <div class="stat-box">
            <div class="stat-val">${disk}</div>
            <div class="stat-lbl">Schijf</div>
          </div>
          <div class="stat-box">
            <div class="stat-val">${tokens}</div>
            <div class="stat-lbl">Tokens</div>
          </div>
        </div>

        <div class="meta-row">
          <div class="meta-lbl"><i class="bi bi-hdd-network me-1"></i>Poorten</div>
          ${portTags(info.ports, info.open_ports, info.port_conflicts)}
        </div>
        ${Object.keys(info.port_conflicts || {}).length ? `
        <div style="background:rgba(227,179,65,0.08);border:1px solid rgba(227,179,65,0.3);border-radius:6px;padding:7px 10px;font-size:0.72rem;color:var(--yellow);margin-bottom:8px">
          <i class="bi bi-exclamation-triangle-fill me-1"></i>
          <b>Poortconflict:</b> ${Object.entries(info.port_conflicts).map(([p,names])=>`<b>:${p}</b> ook bij <b>${names.join(', ')}</b>`).join(' · ')}
        </div>` : ''}

        <div class="meta-row">
          <div class="meta-lbl"><i class="bi bi-link-45deg me-1"></i>Relaties</div>
          ${relationTags(info.relations)}
        </div>

        <div class="meta-row">
          <div class="meta-lbl"><i class="bi bi-box me-1"></i>Dependencies</div>
          <span style="font-size:0.75rem; color:var(--muted)">${depsStr}</span>
        </div>

        ${notes}

        <div class="action-row">
          ${startBtn}${stopBtn}${restartBtn}${logsBtn}
        </div>
      </div>
    </div>`;
}

async function loadProjects() {
  const icon = document.getElementById('refresh-icon');
  icon.classList.add('refreshing');

  try {
    const r = await fetch('/api/projects');
    allProjects = await r.json();

    const grid = document.getElementById('proj-grid');
    grid.innerHTML = Object.entries(allProjects).map(([n, i]) => renderCard(n, i)).join('');

    const running = Object.values(allProjects).filter(p => p.status === 'running').length;
    const total = Object.keys(allProjects).length;
    document.getElementById('running-num').textContent = `${running}/${total}`;
    document.getElementById('update-time').textContent = new Date().toLocaleTimeString('nl-NL');
  } catch(e) {
    document.getElementById('update-time').textContent = 'fout bij laden';
  } finally {
    icon.classList.remove('refreshing');
  }
}

async function doAction(name, action) {
  const card = document.getElementById(`card-${name}`);
  const buttons = card ? card.querySelectorAll('.btn-act') : [];
  buttons.forEach(b => b.disabled = true);

  const labels = { start: 'Starten...', stop: 'Stoppen...', restart: 'Herstarten...' };
  if (buttons.length) buttons[0].textContent = labels[action] || '...';

  try {
    const r = await fetch(`/api/projects/${name}/${action}`, { method: 'POST' });
    const data = await r.json();
    if (!data.success && data.message !== 'gestart' && data.message !== 'herstarten...') {
      console.warn(`${name} ${action}: ${data.message}`);
    }
  } catch(e) {
    alert('Fout: ' + e);
  }

  // Ververs na actie
  const delay = action === 'start' ? 4000 : action === 'restart' ? 6000 : 2000;
  setTimeout(loadProjects, delay);
}

async function showLogs(name) {
  activeLogProject = name;
  document.getElementById('logTitle').innerHTML = `<i class="bi bi-terminal me-2"></i>Logs — <b>${name}</b>`;
  document.getElementById('log-content').textContent = 'laden...';
  new bootstrap.Modal(document.getElementById('logModal')).show();
  await refreshLogs();
}

async function refreshLogs() {
  if (!activeLogProject) return;
  try {
    const r = await fetch(`/api/projects/${activeLogProject}/logs?lines=100`);
    const data = await r.json();
    const box = document.getElementById('log-content');
    box.textContent = data.content || '(geen logs)';
    box.scrollTop = box.scrollHeight;
  } catch(e) {
    document.getElementById('log-content').textContent = 'Fout bij laden logs';
  }
}

// ── Tab switching ─────────────────────────────────────────────────────────────
let currentTab = 'projects';
function showTab(tab) {
  currentTab = tab;
  document.getElementById('view-projects').style.display = tab === 'projects' ? '' : 'none';
  document.getElementById('view-registry').style.display = tab === 'registry' ? '' : 'none';
  document.getElementById('tab-projects').classList.toggle('active', tab === 'projects');
  document.getElementById('tab-registry').classList.toggle('active', tab === 'registry');
  if (tab === 'registry') loadRegistry();
}

// ── Port Registry tab ─────────────────────────────────────────────────────────
async function loadRegistry() {
  const el = document.getElementById('registry-content');
  try {
    const r = await fetch('http://localhost:4444/ports');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    const rows = Object.entries(data).map(([svc, info]) => {
      const inUse = info.in_use;
      const dot = inUse
        ? '<span class="dot-pulse" style="background:var(--green);width:7px;height:7px;border-radius:50%;display:inline-block"></span>'
        : '<span style="background:var(--border);width:7px;height:7px;border-radius:50%;display:inline-block"></span>';
      const portLink = inUse
        ? `<a href="http://localhost:${info.port}" target="_blank" class="tag tag-port-open" style="text-decoration:none">:${info.port} <i class="bi bi-box-arrow-up-right" style="font-size:0.55rem"></i></a>`
        : `<span class="tag tag-port-closed">:${info.port}</span>`;
      return `<tr>
        <td>${dot} <strong style="color:var(--blue)">${svc}</strong></td>
        <td>${portLink}</td>
        <td style="color:var(--muted)">${info.project || '—'}</td>
        <td style="color:var(--text)">${info.description || '—'}</td>
        <td style="color:var(--muted);font-size:0.72rem">${inUse ? '<span style="color:var(--green)">actief</span>' : 'gestopt'}</td>
      </tr>`;
    }).join('');
    el.innerHTML = `<div class="reg-card">
      <table class="reg-table">
        <thead><tr><th>Service</th><th>Poort</th><th>Project</th><th>Beschrijving</th><th>Status</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <p style="font-size:0.72rem;color:var(--muted);margin-top:10px">
      <i class="bi bi-info-circle me-1"></i>
      Bron: <a href="http://localhost:4444/ports" target="_blank">http://localhost:4444/ports</a> &nbsp;·&nbsp;
      Docs: <a href="http://localhost:4444/docs" target="_blank">http://localhost:4444/docs</a>
    </p>`;
  } catch(e) {
    el.innerHTML = `<div class="reg-offline">
      <i class="bi bi-plug-fill" style="font-size:2rem;color:var(--red)"></i><br><br>
      <strong>Port Registry niet bereikbaar</strong><br>
      <span style="color:var(--muted)">Start het register: <code>port-registry</code> of <code>python ~/port-registry/server.py</code></span>
    </div>`;
  }
}

// ── Laad meteen, daarna iedere 5 seconden ─────────────────────────────────────
loadProjects();
setInterval(() => {
  if (currentTab === 'projects') loadProjects();
  else loadRegistry();
}, 5000);
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app()
