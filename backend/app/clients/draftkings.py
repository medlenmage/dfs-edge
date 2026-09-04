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
from collections.abc import Iterable
from typing import Any

from app.cache import cached
from app.clients.http import get_json
from app.services.player_match import normalize_name

log = logging.getLogger(__name__)

LOBBY_URL = "https://www.draftkings.com/lobby/getcontests"
DRAFTABLES_URL = "https://api.draftkings.com/draftgroups/v1/draftgroups/{}/draftables"

SPORT = "MLB"  # kept for callers that predate the sport argument

# Which DK GameTypeIds are the Classic salary-cap pools this app is
# built around. Everything else (Snake, Tiers, Showdown, Madden Stream,
# Sit & Go, ...) uses roster rules this app doesn't support.
#
# THE IDS ARE NOT SHARED BETWEEN SPORTS. MLB Classic is 2 (114 for a
# Classic restricted to one game); NFL Classic is 1. Verified against
# the live lobby rather than assumed -- pulling draftables for each
# candidate id showed only NFL's 1 carries both salaries and a DST,
# while 189 and 145 (Sit & Go) return neither and are draft formats:
#
#     type 1    1,486 draftables   QB/RB/WR/TE/DST   salaries 2000-8000
#     type 189  1,658 draftables   no DST            no salaries
#     type 145  4,501 draftables   no DST            no salaries
#
# Salaries-and-a-DST is the signature to re-check against if DK ever
# renumbers these.
CLASSIC_GAME_TYPE_IDS = {2, 114}          # MLB, kept as the old name
SPORT_CLASSIC_GAME_TYPE_IDS: dict[str, set[int]] = {
    "MLB": {2, 114},
    "NFL": {1},
}

# Slates and their labels rarely change once posted; salaries/players
# can, especially close to lock (late scratches, swaps) -- so get_slates
# is cached longer than get_draftables, which the UI's own "Refresh"
# button (force=True) is meant to bypass on demand.
_SLATES_TTL = 900  # 15 min
_DRAFTABLES_TTL = 600  # 10 min

# DK's own "Fantasy Points Per Game" stat attribute id -- the same
# number a CSV export's AvgPointsPerGame column carries.
_FPPG_ATTRIBUTE_ID = 408


async def get_slates(
    day: str,
    *,
    sport: str = "MLB",
    days: Iterable[str] | None = None,
    force: bool = False,
) -> list[dict[str, Any]]:
    """
    Every live Classic slate whose games start on `day` (YYYY-MM-DD,
    matched against DK's own Eastern-local StartDateEst) -- id, a human
    label ("Early", "Main", "Night", ...; DK leaves the biggest slate of
    the day unlabeled, shown here as "Main"), how many games it covers,
    and the games themselves, so the frontend can show a real picker
    instead of guessing from an uploaded file.

    `days` takes a whole set of dates instead, which is what NFL needs:
    a football week is not a day. A single Sunday-Monday slate and the
    Thursday-through-Monday one that includes it start on different
    dates, and both belong to the same week -- so the caller passes
    every date the week covers and gets all of them back.
    """
    wanted = {day} if days is None else {d for d in days if d}

    async def _load() -> Any:
        return await get_json(
            LOBBY_URL, params={"sport": sport}, source="DraftKings lobby"
        )

    payload = await cached(f"dk:slates:{sport}", _SLATES_TTL, _load, force=force)
    return _parse_slates(
        payload, wanted, SPORT_CLASSIC_GAME_TYPE_IDS.get(sport, CLASSIC_GAME_TYPE_IDS)
    )


def _parse_slates(
    payload: dict[str, Any],
    days: str | Iterable[str],
    classic_game_type_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    """The pure transformation half of get_slates() -- split out so it's
    directly testable against a fixture payload, with no network call."""
    wanted = {days} if isinstance(days, str) else set(days)
    allowed = classic_game_type_ids or CLASSIC_GAME_TYPE_IDS
    game_sets = {gs.get("GameSetKey"): gs for gs in (payload.get("GameSets") or [])}

    slates: list[dict[str, Any]] = []
    for dg in payload.get("DraftGroups") or []:
        if dg.get("GameTypeId") not in allowed:
            continue
        start_est = dg.get("StartDateEst") or ""
        if not any(start_est.startswith(d) for d in wanted):
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
    # DK returns ONE ROW PER ROSTER SLOT, not per player, so anyone
    # eligible in more than one slot appears more than once. It is not a
    # rounding error: on a 12-game NFL slate 628 of 744 players are
    # duplicated, because every skill player is listed at his own
    # position and again at FLEX. Left in, they become duplicate players
    # on the slate and a lineup could roster the same man twice.
    #
    # Deduplicating on DK's own player id is safe for both sports: the
    # rows for one player carry identical salary and position, and MLB's
    # multi-eligibility travels inside the position string itself
    # ("1B/OF" on BOTH of Schwarber's rows), so nothing is lost by
    # keeping the first. Verified against live payloads for each sport
    # rather than assumed -- 0 of 25 duplicated MLB players had rows
    # whose positions disagreed.
    seen: set[Any] = set()
    rows: list[dict[str, Any]] = []
    for p in payload.get("draftables") or []:
        if p.get("isDisabled"):
            continue
        key = p.get("playerDkId") or (p.get("displayName"), p.get("teamAbbreviation"))
        if key in seen:
            continue
        seen.add(key)
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
