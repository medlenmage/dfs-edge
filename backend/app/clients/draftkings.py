"""
Client for DraftKings' own public lobby/draftables API.

Free, no API key -- this is the exact same data draftkings.com's own
lobby page loads in your browser when you're picking a contest, just
read directly instead of scraped from rendered HTML. Confirmed working
live against real endpoints (see the README roadmap entry for this
feature). Like clients/mlb.py's use of the unofficial MLB Stats API,
this is unsupported and could change without notice -- everything here
goes through the cache layer so a bad response never takes the app
down, and parsing is defensive rather than assuming the shape never
moves.

Two calls cover everything a manual DK salary CSV upload used to:

  1. get_slates(day) -- every real Classic MLB slate live for that date
     (Early/Main/Night/Featured/etc, each with its real game count and
     start time) so the user can pick exactly the one they're playing,
     the same way build_contest_field()'s "games filter" already lets
     them focus a slate -- just sourced live instead of inferred from
     an uploaded CSV's Game Info column.
  2. get_draftables(draft_group_id) -- every player, salary, position,
     team, and opponent for that specific slate, returned in the exact
     row shape services/salaries.py's CSV parser already produces, so
     it plugs straight into salaries.store() with no changes needed
     anywhere downstream (mlb_slate.py's in_slate detection, the
     optimizer, the contest generator all keep working unmodified).

Only DK's "Classic" roster format is surfaced (GameTypeId 2 for the
big multi-game slates, 114 for a single-game Classic pool) -- Snake,
Tiers, and Home Run Showdown are real DK contest types but use
different roster rules this app was never built to optimize for.
"""

from __future__ import annotations

import logging
from typing import Any

from app.cache import cached
from app.clients.http import get_json
from app.services.player_match import normalize_name

log = logging.getLogger(__name__)

LOBBY_URL = "https://www.draftkings.com/lobby/getcontests"
DRAFTABLES_URL = "https://api.draftkings.com/draftgroups/v1/draftgroups/{}/draftables"

SPORT = "MLB"
# GameTypeId 2 = Classic (the big Early/Main/Night/Featured multi-game
# pools this app is built around); 114 = Classic restricted to a
# single game. Everything else (Snake, Tiers, Home Run Showdown, ...)
# uses roster rules this app doesn't support.
CLASSIC_GAME_TYPE_IDS = {2, 114}

# Slates and their labels rarely change once posted; salaries/players
# can, especially close to lock (late scratches, swaps) -- so get_slates
# is cached longer than get_draftables, which the UI's own "Refresh"
# button (force=True) is meant to bypass on demand.
_SLATES_TTL = 900  # 15 min
_DRAFTABLES_TTL = 600  # 10 min

# DK's own "Fantasy Points Per Game" stat attribute id -- the same
# number a CSV export's AvgPointsPerGame column carries.
_FPPG_ATTRIBUTE_ID = 408


async def get_slates(day: str, *, force: bool = False) -> list[dict[str, Any]]:
    """
    Every live Classic MLB slate whose games start on `day` (YYYY-MM-DD,
    matched against DK's own Eastern-local StartDateEst) -- id, a
    human label ("Early", "Main", "Night", ...; DK leaves the biggest
    slate of the day unlabeled, shown here as "Main"), how many games
    it covers, and the games themselves (teams + start time), so the
    frontend can show a real picker instead of guessing from an
    uploaded file.
    """

    async def _load() -> Any:
        return await get_json(
            LOBBY_URL, params={"sport": SPORT}, source="DraftKings lobby"
        )

    payload = await cached(f"dk:slates:{SPORT}", _SLATES_TTL, _load, force=force)
    return _parse_slates(payload, day)


def _parse_slates(payload: dict[str, Any], day: str) -> list[dict[str, Any]]:
    """The pure transformation half of get_slates() -- split out so it's
    directly testable against a fixture payload, with no network call."""
    game_sets = {gs.get("GameSetKey"): gs for gs in (payload.get("GameSets") or [])}

    slates: list[dict[str, Any]] = []
    for dg in payload.get("DraftGroups") or []:
        if dg.get("GameTypeId") not in CLASSIC_GAME_TYPE_IDS:
            continue
        start_est = dg.get("StartDateEst") or ""
        if not start_est.startswith(day):
            continue

        games = []
        game_set = game_sets.get(dg.get("GameSetKey")) or {}
        for comp in game_set.get("Competitions") or []:
            # "Description" is already "AWAY @ HOME" in the same team
            # abbreviations used everywhere else in this app.
            parts = (comp.get("Description") or "").split("@")
            away, home = (p.strip() for p in parts) if len(parts) == 2 else ("", "")
            games.append(
                {
                    "game_id": comp.get("GameId"),
                    "away": away,
                    "home": home,
                    "start_time_utc": comp.get("StartDate"),
                }
            )

        label = (dg.get("ContestStartTimeSuffix") or "").strip(" ()") or "Main"
        slates.append(
            {
                "draft_group_id": dg.get("DraftGroupId"),
                "label": label,
                "game_type_id": dg.get("GameTypeId"),
                "game_count": dg.get("GameCount"),
                "start_time_utc": dg.get("StartDate"),
                "games": games,
            }
        )

    slates.sort(key=lambda s: s["start_time_utc"] or "")
    return slates


def _fppg(draft_stat_attributes: list[dict[str, Any]] | None) -> float | None:
    for attr in draft_stat_attributes or []:
        if attr.get("id") == _FPPG_ATTRIBUTE_ID:
            try:
                return float(attr["value"])
            except (TypeError, ValueError):
                return None
    return None


async def get_draftables(draft_group_id: int, *, force: bool = False) -> list[dict[str, Any]]:
    """
    Every player, salary, position, and matchup for one specific slate
    (from get_slates()' draft_group_id) -- returned in the exact row
    shape services/salaries.parse_dk_csv() produces from a manual CSV
    upload, so salaries.store(day, rows) accepts this directly with no
    downstream changes needed. Excludes anything DK itself has flagged
    isDisabled (truly undraftable, not just day-to-day/IL -- those
    still show up here the same way they would in a real CSV export).
    """

    async def _load() -> Any:
        return await get_json(
            DRAFTABLES_URL.format(draft_group_id), source="DraftKings draftables"
        )

    payload = await cached(f"dk:draftables:{draft_group_id}", _DRAFTABLES_TTL, _load, force=force)
    return _parse_draftables(payload)


def _parse_draftables(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The pure transformation half of get_draftables() -- split out so
    it's directly testable against a fixture payload, with no network
    call."""
    rows: list[dict[str, Any]] = []
    for p in payload.get("draftables") or []:
        if p.get("isDisabled"):
            continue
        name = (p.get("displayName") or "").strip()
        salary = p.get("salary")
        if not name or not salary:
            continue

        competition = p.get("competition") or {}
        matchup = (competition.get("name") or "").replace(" ", "")  # "SEA @ MIL" -> "SEA@MIL"

        rows.append(
            {
                "name": name,
                "normalized_name": normalize_name(name),
                "team": (p.get("teamAbbreviation") or "").strip().upper(),
                "position": (p.get("position") or "").strip(),
                "salary": int(salary),
                "avg_points": _fppg(p.get("draftStatAttributes")),
                "game_info": matchup,
                "dk_id": str(p.get("playerDkId") or ""),
            }
        )
    return rows
