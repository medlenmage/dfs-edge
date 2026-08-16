"""
NFL schedule and betting-line data via nflverse's public CSV releases
(https://github.com/nflverse/nflverse-data) -- free, no key, no rate
limit worth worrying about. Same "unofficial CSV export" pattern as
clients/savant.py: this is a community data project, not an official
NFL feed, so if the shape changes without notice, callers just get an
empty/neutral result instead of a broken page.

`nfl_data_py` (the README's original suggestion) turned out to be a
dead end here -- it pins pandas<2.0, which has no build for this
project's Python version. Fetching nflverse's own CSV releases directly
sidesteps that entirely and matches how the rest of this app already
handles CSV-export data sources (no pandas anywhere in this codebase).

Unlike MLB, a DK NFL slate is weekly, not daily -- every function here
takes (season, week) rather than a date. "season" means the year the
season *starts* in (the 2026 season runs Sept 2026 - Feb 2027 and is
season=2026 for the whole span, including its January games).
"""

from __future__ import annotations

import csv
import io
from datetime import date as date_cls
from typing import Any

from app.cache import cached
from app.clients.http import get_text
from app.config import get_settings

GAMES_URL = "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv"


def _f(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _i(value: Any) -> int | None:
    f = _f(value)
    return int(f) if f is not None else None


async def _load_games_csv() -> str:
    settings = get_settings()

    async def _load() -> str:
        return await get_text(GAMES_URL, source="nflverse schedule")

    # The season's full schedule barely changes week to week (mostly
    # just which games have a final score) -- the schedule TTL is
    # generous, but this is a much bigger, slower-changing file than
    # MLB's daily schedule pull, so caching it is worth more here.
    return await cached("nfl:games:raw", settings.ttl_schedule, _load)


async def get_schedule(
    season: int, week: int | None = None, game_type: str = "REG"
) -> list[dict[str, Any]]:
    """
    Games for a season (optionally filtered to one week). `game_type`
    defaults to "REG" (regular season) -- nflverse also uses "PRE" for
    preseason (not present in this release as of writing) and
    "WC"/"DIV"/"CON"/"SB" for the playoff rounds.
    """
    text = await _load_games_csv()
    games: list[dict[str, Any]] = []
    for row in csv.DictReader(io.StringIO(text)):
        if _i(row.get("season")) != season or row.get("game_type") != game_type:
            continue
        row_week = _i(row.get("week"))
        if week is not None and row_week != week:
            continue
        games.append(
            {
                "game_id": row.get("game_id"),
                "season": season,
                "week": row_week,
                "game_type": row.get("game_type"),
                "gameday": row.get("gameday"),
                "gametime": row.get("gametime"),
                "weekday": row.get("weekday"),
                "away_team": row.get("away_team"),
                "home_team": row.get("home_team"),
                "away_moneyline": _f(row.get("away_moneyline")),
                "home_moneyline": _f(row.get("home_moneyline")),
                "spread_line": _f(row.get("spread_line")),
                "total_line": _f(row.get("total_line")),
                "div_game": row.get("div_game") == "1",
                "roof": row.get("roof") or None,
                "surface": row.get("surface") or None,
                "stadium": row.get("stadium") or None,
            }
        )
    games.sort(key=lambda g: (g.get("gameday") or "", g.get("gametime") or ""))
    return games


def season_for_date(day: date_cls) -> int:
    """
    Which NFL season a calendar date falls in. The season is named for
    the year it starts -- a January/February date is still part of the
    season that kicked off the previous September.
    """
    return day.year if day.month >= 3 else day.year - 1


def current_week(season_games: list[dict[str, Any]], today_iso: str) -> int:
    """
    The week to show by default: the first one whose games aren't all
    in the past yet. Falls back to week 1 if the season hasn't started
    (every week is still upcoming) or the last week if it's fully over.
    """
    if not season_games:
        return 1
    weeks = sorted({g["week"] for g in season_games if g.get("week") is not None})
    for w in weeks:
        week_days = [g["gameday"] for g in season_games if g["week"] == w and g.get("gameday")]
        if week_days and max(week_days) >= today_iso:
            return w
    return weeks[-1] if weeks else 1


def implied_team_totals(game: dict[str, Any]) -> dict[str, float | None]:
    """
    Each side's implied point total from the closing total + spread --
    the same "how many is Vegas expecting from this team" signal MLB's
    implied runs already gives hitters, just for a different sport.

    Deliberately doesn't rely on knowing which direction nflverse's own
    `spread_line` sign convention points (documented inconsistently
    across sources, and getting it backwards would silently produce a
    wrong number for every game). Instead: the moneyline sign
    unambiguously says who's favored, and the standard total+spread
    split (half the total, plus or minus half the spread) is applied
    from there.
    """
    total = game.get("total_line")
    spread = game.get("spread_line")
    home_ml = game.get("home_moneyline")
    if total is None or spread is None or home_ml is None:
        return {"home": None, "away": None}

    half_total = total / 2
    half_spread = abs(spread) / 2
    if home_ml < 0:  # home team favored
        return {"home": round(half_total + half_spread, 1), "away": round(half_total - half_spread, 1)}
    return {"home": round(half_total - half_spread, 1), "away": round(half_total + half_spread, 1)}
