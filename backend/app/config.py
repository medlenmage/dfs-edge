"""
Central place for every setting the app needs.

Everything is read from the .env file at the project root. If a value
is missing we fall back to a sensible default so the app still boots --
features that genuinely need a key (Claude, betting lines) just switch
themselves off instead of crashing.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# backend/app/config.py -> backend/app -> backend -> project root
BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent

# Load .env from the project root, then fall back to backend/.env
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(BACKEND_DIR / ".env")


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def _list(name: str, default: str) -> list[str]:
    raw = os.getenv(name, "").strip() or default
    return [item.strip() for item in raw.split(",") if item.strip()]


class Settings:
    """Plain object rather than BaseSettings -- easier to read and debug."""

    def __init__(self) -> None:
        # --- Anthropic ---
        self.anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "").strip()
        self.anthropic_model: str = (
            os.getenv("ANTHROPIC_MODEL", "").strip() or "claude-sonnet-5"
        )

        # --- The Odds API ---
        self.odds_api_key: str = os.getenv("ODDS_API_KEY", "").strip()
        self.odds_bookmakers: list[str] = _list(
            "ODDS_BOOKMAKERS", "draftkings,fanduel"
        )
        # Defaults on now that odds.py/mlb_slate.py cache game lines and
        # props once per calendar day (not per TTL window) -- see
        # odds.py's own module docstring for the real credit-cost math
        # that made this affordable to turn on by default.
        self.odds_fetch_props: bool = _bool("ODDS_FETCH_PROPS", True)

        # --- Cache TTLs (seconds) ---
        # CACHE_TTL_ODDS was removed -- game lines and player props now
        # cache by calendar day instead of a rolling TTL window (see
        # clients/odds.py's own credit-cost docstring), so there's no
        # longer a TTL setting for odds to override.
        self.ttl_schedule: int = _int("CACHE_TTL_SCHEDULE", 900)
        self.ttl_stats: int = _int("CACHE_TTL_STATS", 21_600)
        self.ttl_weather: int = _int("CACHE_TTL_WEATHER", 1_800)
        self.ttl_injuries: int = _int("CACHE_TTL_INJURIES", 3_600)
        # A played game's box score doesn't change -- this could be
        # cached far longer than ttl_stats, but matching its cadence
        # keeps one predictable daily-refresh rhythm across every stat
        # fetch in this app rather than a separate "basically forever"
        # special case (which would be wrong anyway for a season still
        # in progress, where new games keep appending).
        self.ttl_game_logs: int = _int("CACHE_TTL_GAME_LOGS", 21_600)

        # --- Background lineup watcher ---
        # How often to re-check confirmed lineups for scratches, in
        # seconds. Matches mlb.get_lineups()'s own 300s cache TTL so a
        # poll is never wasted work re-fetching something still fresh.
        self.lineup_poll_interval_sec: int = _int("LINEUP_POLL_INTERVAL_SEC", 300)

        # --- Storage ---
        db_path = os.getenv("DB_PATH", "data/dfsedge.db").strip()
        self.db_path: Path = (
            Path(db_path) if Path(db_path).is_absolute() else BACKEND_DIR / db_path
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # --- Historical data store (Supabase Postgres) ---
        # Durable archive for data meant to outlive cache.py's TTL'd
        # SQLite rows (real RotoWire uploads, game results). Optional --
        # history_db.py no-ops everywhere if this is unset, same
        # "feature switches itself off instead of crashing" philosophy
        # as the Claude/Odds keys above.
        self.supabase_db_url: str = os.getenv("SUPABASE_DB_URL", "").strip()

        # --- Server ---
        self.api_host: str = os.getenv("API_HOST", "127.0.0.1").strip()
        self.api_port: int = _int("API_PORT", 8000)
        self.cors_origins: list[str] = _list(
            "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
        )

    # --- Convenience flags used by the UI to grey out panels ---
    @property
    def has_claude(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def has_odds(self) -> bool:
        return bool(self.odds_api_key)

    @property
    def has_history_db(self) -> bool:
        return bool(self.supabase_db_url)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
