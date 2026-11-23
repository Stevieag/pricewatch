"""Configuration loading.

Design goals:
  * Zero secrets in the repository. Config lives OUTSIDE the project directory by
    default (~/.config/pricewatch/pricewatch.env) so AI coding agents working in
    the repo never see it.
  * Secret values are wrapped in ``Secret`` so they cannot leak through repr(),
    str(), f-strings, logs, tracebacks or LLM prompts by accident.
  * Values from the env file are NOT exported into os.environ, so child
    processes (and anything that dumps the environment) never receive them.
"""
from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "pricewatch"

# Search order for the env file. First one that exists wins.
def _candidate_env_files() -> list[Path]:
    paths: list[Path] = []
    if os.environ.get("PW_ENV_FILE"):
        paths.append(Path(os.environ["PW_ENV_FILE"]).expanduser())
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    paths.append(xdg / APP_NAME / f"{APP_NAME}.env")
    paths.append(Path.cwd() / ".env")  # supported, but discouraged (see SECURITY.md)
    return paths


def _default_data_dir() -> Path:
    xdg = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return xdg / APP_NAME


class Secret:
    """Holds a sensitive string. Never renders its value implicitly."""

    __slots__ = ("_value",)

    def __init__(self, value: str | None):
        self._value = value or ""

    def reveal(self) -> str:
        """The ONLY way to read the value. Grep for `.reveal()` to audit usage."""
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __repr__(self) -> str:
        return "Secret('***')" if self._value else "Secret('')"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return repr(self)

    def __reduce__(self):  # refuse pickling / serialisation
        raise TypeError("Secret values cannot be serialised")


def parse_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser (supports comments, quotes, `export ` prefix)."""
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        elif " #" in val:
            val = val.split(" #", 1)[0].rstrip()
        out[key] = val
    return out


def _warn_if_world_readable(path: Path) -> None:
    if os.name != "posix":
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        print(
            f"[pricewatch] WARNING: {path} is readable by other users. "
            f"Run: chmod 600 {path}",
            file=sys.stderr,
        )


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _int(v: str | None, default: int) -> int:
    try:
        return int(v) if v not in (None, "") else default
    except ValueError:
        return default


def _float(v: str | None, default: float) -> float:
    try:
        return float(v) if v not in (None, "") else default
    except ValueError:
        return default


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8765
    data_dir: Path = field(default_factory=_default_data_dir)
    allowed_hosts: list[str] = field(default_factory=list)
    access_token: Secret = field(default_factory=lambda: Secret(""))
    min_interval_minutes: int = 5
    # Many shops (Argos, Currys, Amazon...) reject unknown bots, so default to a normal browser.
    user_agent: str = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
    allow_private_hosts: bool = False  # SSRF guard; keep False
    fetch_timeout: int = 20
    browser_fetch: str = "auto"    # auto (fallback when blocked / no price) | always | never
    browser_path: str = ""         # optional explicit path to Chrome/Edge/Brave/Chromium/Firefox
    container_loopback_only: bool = False  # Docker: port published on 127.0.0.1 only
    scheduler_tick_seconds: int = 20

    # v2 - AI agent
    llm_provider: str = "none"  # none | ollama | openai_compatible | anthropic
    llm_base_url: str = "http://127.0.0.1:11434"
    llm_model: str = "llama3.1"
    llm_api_key: Secret = field(default_factory=lambda: Secret(""))
    search_provider: str = "none"  # none | searxng
    searxng_url: str = "http://127.0.0.1:8888"
    agent_max_candidates: int = 8
    agent_interval_hours: int = 0  # 0 = only on demand

    # v4 - fair price
    fair_proxies: Secret = field(default_factory=lambda: Secret(""))  # comma-separated proxy URLs
    fair_threshold_pct: float = 2.0     # % difference that counts as "different price"
    surge_threshold_pct: float = 15.0   # % above 30-day median that counts as a surge
    fair_interval_hours: int = 24       # automatic persona check per item; 0 = on demand only

    env_file: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "pricewatch.db"

    def is_loopback_bind(self) -> bool:
        return self.host in {"127.0.0.1", "localhost", "::1"}

    def public_summary(self) -> dict:
        """Safe-to-display config (no secrets, only whether they are set)."""
        return {
            "host": self.host,
            "port": self.port,
            "data_dir": str(self.data_dir),
            "env_file": str(self.env_file) if self.env_file else None,
            "access_token_set": bool(self.access_token),
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "llm_api_key_set": bool(self.llm_api_key),
            "search_provider": self.search_provider,
            "fair_proxies_set": bool(self.fair_proxies),
            "browser_fetch": self.browser_fetch,
            "fair_interval_hours": self.fair_interval_hours,
        }


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """Build Settings from (in priority order) real env vars, then the env file."""
    file_vals: dict[str, str] = {}
    # v4 - fair price
    fair_proxies: Secret = field(default_factory=lambda: Secret(""))  # comma-separated proxy URLs
    fair_threshold_pct: float = 2.0     # % difference that counts as "different price"
    surge_threshold_pct: float = 15.0   # % above 30-day median that counts as a surge
    fair_interval_hours: int = 24       # automatic persona check per item; 0 = on demand only

    env_file: Path | None = None
    if env is None:
        for p in _candidate_env_files():
            if p.is_file():
                env_file = p
                _warn_if_world_readable(p)
                file_vals = parse_env_file(p)
                break
        merged = {**file_vals, **{k: v for k, v in os.environ.items() if k.startswith("PW_")}}
    else:
        merged = dict(env)

    g = merged.get
    s = Settings(
        host=g("PW_HOST", "127.0.0.1"),
        port=_int(g("PW_PORT"), 8765),
        data_dir=Path(g("PW_DATA_DIR") or _default_data_dir()).expanduser(),
        allowed_hosts=[h.strip() for h in (g("PW_ALLOWED_HOSTS") or "").split(",") if h.strip()],
        access_token=Secret(g("PW_ACCESS_TOKEN")),
        min_interval_minutes=max(1, _int(g("PW_MIN_INTERVAL_MINUTES"), 5)),
        user_agent=g("PW_USER_AGENT") or Settings.user_agent,
        allow_private_hosts=_bool(g("PW_ALLOW_PRIVATE_HOSTS")),
        fetch_timeout=_int(g("PW_FETCH_TIMEOUT"), 20),
        container_loopback_only=_bool(g("PW_CONTAINER_LOOPBACK_ONLY")),
        browser_fetch=(g("PW_BROWSER_FETCH") or "auto").lower(),
        browser_path=g("PW_BROWSER_PATH") or "",
        scheduler_tick_seconds=max(1, _int(g("PW_SCHEDULER_TICK_SECONDS"), 20)),
        llm_provider=(g("PW_LLM_PROVIDER") or "none").lower(),
        llm_base_url=(g("PW_LLM_BASE_URL") or "http://127.0.0.1:11434").rstrip("/"),
        llm_model=g("PW_LLM_MODEL") or "llama3.1",
        llm_api_key=Secret(g("PW_LLM_API_KEY")),
        search_provider=(g("PW_SEARCH_PROVIDER") or "none").lower(),
        searxng_url=(g("PW_SEARXNG_URL") or "http://127.0.0.1:8888").rstrip("/"),
        agent_max_candidates=_int(g("PW_AGENT_MAX_CANDIDATES"), 8),
        agent_interval_hours=_int(g("PW_AGENT_INTERVAL_HOURS"), 0),
        fair_proxies=Secret(g("PW_FAIR_PROXIES")),
        fair_threshold_pct=_float(g("PW_FAIR_THRESHOLD_PCT"), 2.0),
        surge_threshold_pct=_float(g("PW_SURGE_THRESHOLD_PCT"), 15.0),
        fair_interval_hours=_int(g("PW_FAIR_INTERVAL_HOURS"), 24),
        env_file=env_file,
    )
    return s


def validate_settings(s: Settings) -> list[str]:
    """Return fatal problems. The server refuses to start if any exist."""
    problems = []
    if not s.is_loopback_bind() and not s.access_token and not s.container_loopback_only:
        problems.append(
            "PW_HOST is not loopback but PW_ACCESS_TOKEN is empty. Refusing to expose "
            "an unauthenticated server to the network. Generate a token with: "
            "python -m pricewatch gen-token"
        )
    if s.access_token and len(s.access_token.reveal()) < 24:
        problems.append("PW_ACCESS_TOKEN must be at least 24 characters.")
    if s.llm_provider not in {"none", "ollama", "openai_compatible", "anthropic"}:
        problems.append(f"Unknown PW_LLM_PROVIDER: {s.llm_provider}")
    if s.browser_fetch not in {"auto", "always", "never"}:
        problems.append(f"PW_BROWSER_FETCH must be auto, always or never (got {s.browser_fetch})")
    if s.search_provider not in {"none", "searxng"}:
        problems.append(f"Unknown PW_SEARCH_PROVIDER: {s.search_provider}")
    return problems
