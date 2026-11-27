# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Stevieag and PriceWatch contributors
"""One-command start / stop / status for the whole local stack:
Docker Desktop -> SearXNG container, Ollama, and the PriceWatch server (in the background).

    python3 -m pricewatch start [--no-browser]
    python3 -m pricewatch status
    python3 -m pricewatch stop [--all]      # --all also quits Ollama and Docker Desktop

Only touches what PriceWatch uses: the `searxng` service from this project's
docker-compose.yml (never other containers), and Ollama/Docker apps only with --all.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from .config import Settings

ROOT = Path(__file__).resolve().parent.parent          # the project folder
COMPOSE = ROOT / "docker-compose.yml"
IS_MAC = sys.platform == "darwin"
LOG_MAX_BYTES = 5 * 1024 * 1024


def _say(ok: bool | None, what: str, detail: str = "") -> None:
    mark = {True: "  ✓", False: "  ✗", None: "  …"}[ok]
    print(f"{mark} {what}{'  - ' + detail if detail else ''}", flush=True)


def _http_ok(url: str, timeout: float = 3) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _wait(check, seconds: float, step: float = 1.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if check():
            return True
        time.sleep(step)
    return check()


def _run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


# ------------------------------------------------------------------ PriceWatch server

def pid_file(s: Settings) -> Path:
    return s.data_dir / "pricewatch.pid"


def log_file(s: Settings) -> Path:
    return s.data_dir / "pricewatch.log"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def server_pid(s: Settings) -> int | None:
    try:
        pid = int(pid_file(s).read_text().strip())
    except (OSError, ValueError):
        return None
    if _alive(pid):
        return pid
    pid_file(s).unlink(missing_ok=True)            # stale (e.g. after a reboot)
    return None


def server_url(s: Settings) -> str:
    host = "127.0.0.1" if s.host in ("0.0.0.0", "::", "") else s.host
    return f"http://{host}:{s.port}"


def start_server(s: Settings) -> bool:
    pid = server_pid(s)
    if pid:
        _say(True, "PriceWatch already running", f"{server_url(s)} (pid {pid})")
        return True
    if _http_ok(server_url(s) + "/healthz"):
        _say(True, "PriceWatch already running", f"{server_url(s)} (started in another window)")
        return True
    s.data_dir.mkdir(parents=True, exist_ok=True)
    log = log_file(s)
    if log.exists() and log.stat().st_size > LOG_MAX_BYTES:
        log.replace(log.with_suffix(".log.1"))
    with open(log, "a", encoding="utf-8") as out:
        os.chmod(log, 0o600)
        proc = subprocess.Popen([sys.executable, "-m", "pricewatch", "serve"], cwd=str(ROOT),
                                stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                                start_new_session=True)
    pid_file(s).write_text(str(proc.pid))
    if _wait(lambda: _http_ok(server_url(s) + "/healthz") or proc.poll() is not None, 20, 0.3) \
            and proc.poll() is None:
        _say(True, "PriceWatch started", f"{server_url(s)} (log: {log})")
        return True
    pid_file(s).unlink(missing_ok=True)
    _say(False, "PriceWatch didn't start", f"see the end of {log}")
    return False


def stop_server(s: Settings) -> None:
    pid = server_pid(s)
    if not pid:
        msg = "not running" if not _http_ok(server_url(s) + "/healthz") else \
            "running in a Terminal window: press Ctrl+C there"
        _say(None, "PriceWatch", msg)
        return
    os.kill(pid, signal.SIGTERM)
    if not _wait(lambda: not _alive(pid), 10, 0.2):
        os.kill(pid, signal.SIGKILL)
    pid_file(s).unlink(missing_ok=True)
    _say(True, "PriceWatch stopped")


# ------------------------------------------------------------------ Docker + SearXNG

def docker_ready() -> bool:
    return bool(shutil.which("docker")) and _run(["docker", "info"], 20).returncode == 0


def start_docker() -> bool:
    if docker_ready():
        _say(True, "Docker running")
        return True
    if not shutil.which("docker") and not IS_MAC:
        _say(False, "Docker not installed", "install Docker, then try again")
        return False
    if IS_MAC:
        _say(None, "Starting Docker Desktop", "this can take up to a minute")
        _run(["open", "-a", "Docker"], 20)
    if _wait(docker_ready, 120, 3):
        _say(True, "Docker running")
        return True
    _say(False, "Docker didn't start", "open Docker Desktop yourself, then run start again")
    return False


def _compose(*args: str) -> subprocess.CompletedProcess:
    return _run(["docker", "compose", "-f", str(COMPOSE), "--profile", "search", *args], 300)


def start_searxng(s: Settings) -> bool:
    if _http_ok(s.searxng_url + "/healthz") or _http_ok(s.searxng_url):
        _say(True, "SearXNG running", s.searxng_url)
        return True
    if not COMPOSE.exists():
        _say(False, "SearXNG", f"docker-compose.yml not found in {ROOT}")
        return False
    if not start_docker():
        return False
    r = _compose("up", "-d", "searxng")
    if r.returncode != 0:
        _say(False, "SearXNG didn't start", (r.stderr or r.stdout).strip().splitlines()[-1:][0]
             if (r.stderr or r.stdout).strip() else "docker compose failed")
        return False
    if _wait(lambda: _http_ok(s.searxng_url), 60, 2):
        _say(True, "SearXNG running", s.searxng_url)
        return True
    _say(False, "SearXNG started but isn't answering yet", "try: python3 -m pricewatch doctor")
    return False


def stop_searxng() -> None:
    if not docker_ready():
        _say(None, "SearXNG", "Docker isn't running, so nothing to stop")
        return
    r = _compose("stop", "searxng")
    _say(r.returncode == 0, "SearXNG stopped" if r.returncode == 0 else "SearXNG stop failed")


# ------------------------------------------------------------------ Ollama

def ollama_ready(s: Settings) -> bool:
    return _http_ok(s.llm_base_url + "/api/tags")


def start_ollama(s: Settings) -> bool:
    if ollama_ready(s):
        _say(True, "Ollama running", s.llm_base_url)
        return True
    if IS_MAC and _run(["open", "-a", "Ollama"], 20).returncode == 0:
        pass
    elif shutil.which("ollama"):
        subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    else:
        _say(False, "Ollama not installed", "see https://ollama.com/download")
        return False
    if _wait(lambda: ollama_ready(s), 40, 1):
        _say(True, "Ollama running", s.llm_base_url)
        return True
    _say(False, "Ollama didn't start", "open the Ollama app yourself")
    return False


def _quit_mac_app(name: str) -> None:
    r = _run(["osascript", "-e", f'quit app "{name}"'], 30)
    _say(r.returncode == 0, f"{name} quit" if r.returncode == 0 else f"Couldn't quit {name}")


# ------------------------------------------------------------------ commands

def start(s: Settings, open_browser: bool = True) -> int:
    print("Starting PriceWatch\n")
    # PriceWatch first: price checks don't need Docker or Ollama (only the shop search does),
    # so a slow or broken Docker Desktop never keeps your prices away
    server_ok = start_server(s)
    if server_ok and open_browser:
        webbrowser.open(server_url(s))
    ok = True
    if s.search_provider == "searxng":
        ok &= start_searxng(s)
    if s.llm_provider == "ollama":
        ok &= start_ollama(s)
    if not ok and server_ok:
        print("  … PriceWatch is running; only the search for other shops needs the parts above.")
    print(f"\n{'Ready' if ok and server_ok else 'Started with problems (see above)'} - "
          f"{server_url(s)}\nStop everything with: python3 -m pricewatch stop")
    return 0 if ok and server_ok else 1


def stop(s: Settings, everything: bool = False) -> int:
    print("Stopping PriceWatch\n")
    stop_server(s)
    if s.search_provider == "searxng":
        stop_searxng()
    if everything and IS_MAC:
        if s.llm_provider == "ollama":
            _quit_mac_app("Ollama")
        _quit_mac_app("Docker")
    elif everything:
        _say(None, "Ollama / Docker", "stop them the usual way on this system")
    print("\nStopped.")
    return 0


def status(s: Settings) -> int:
    print("PriceWatch status\n")
    pid = server_pid(s)
    up = _http_ok(server_url(s) + "/healthz")
    _say(up, "PriceWatch", f"{server_url(s)}" + (f" (pid {pid}, log {log_file(s)})" if pid else "")
         if up else "not running - start with: python3 -m pricewatch start")
    if s.search_provider == "searxng":
        _say(_http_ok(s.searxng_url), "SearXNG", s.searxng_url)
    if s.llm_provider == "ollama":
        _say(ollama_ready(s), "Ollama", s.llm_base_url)
    return 0 if up else 1


def as_json(s: Settings) -> str:  # used by tests
    return json.dumps({"pid": server_pid(s), "url": server_url(s)})


# ------------------------------------------------------------------ start at login (macOS)

LABEL = "io.github.stevieag.pricewatch"


def launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def launch_agent_plist(s: Settings) -> bytes:
    """A LaunchAgent that runs `pricewatch start --no-browser` once at login. The started
    processes outlive it (AbandonProcessGroup), so PriceWatch stays up for the bookmarklet."""
    import plistlib
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-m", "pricewatch", "start", "--no-browser"],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "AbandonProcessGroup": True,
        "StandardOutPath": str(s.data_dir / "autostart.log"),
        "StandardErrorPath": str(s.data_dir / "autostart.log"),
        # launchd's PATH is minimal: add where Homebrew, Docker and Ollama put their commands
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:"
                                         "/Applications/Docker.app/Contents/Resources/bin"},
    })


def autostart(s: Settings, action: str) -> int:
    path = launch_agent_path()
    if not IS_MAC:
        print("Start-at-login is set up for macOS. On Linux, add this to your session's startup\n"
              f"applications or a systemd user service:\n  cd {ROOT} && {sys.executable} -m pricewatch start --no-browser")
        return 1
    domain = f"gui/{os.getuid()}"
    if action == "status":
        on = path.exists()
        _say(on, "Start at login", f"on ({path})" if on else "off - turn on with: python3 -m pricewatch autostart on")
        return 0
    if action == "on":
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            _run(["launchctl", "bootout", domain, str(path)], 30)
        path.write_bytes(launch_agent_plist(s))
        os.chmod(path, 0o644)
        r = _run(["launchctl", "bootstrap", domain, str(path)], 30)
        if r.returncode != 0:  # older macOS
            r = _run(["launchctl", "load", "-w", str(path)], 30)
        _say(r.returncode == 0, "Start at login turned on" if r.returncode == 0 else "Couldn't register with launchd",
             f"PriceWatch, SearXNG and Ollama will start when you log in (from {ROOT})"
             if r.returncode == 0 else (r.stderr or r.stdout).strip()[:200])
        if r.returncode == 0:
            print("    If you move the project folder, run this command again from its new place.")
        return 0 if r.returncode == 0 else 1
    if action == "off":
        if path.exists():
            _run(["launchctl", "bootout", domain, str(path)], 30)
            path.unlink()
        _say(True, "Start at login turned off", "PriceWatch keeps running until you stop it")
        return 0
    print("Use: autostart on | off | status")
    return 2
