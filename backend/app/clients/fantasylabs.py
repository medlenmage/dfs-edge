"""
Client for FantasyLabs' Vegas dashboards (fantasylabs.com/mlb/vegas,
fantasylabs.com/nfl/vegas -- same underlying endpoint, different sport
id, confirmed live for both).

Free, no API key, no login wall -- the same JSON endpoint the dashboard
itself calls in your browser (confirmed live via the network tab, not
documented anywhere: `GET /api/sportevents/{sportId}/{date}/vegas`,
sportId=3 for MLB, sportId=1 for NFL). robots.txt has no disallow
rules. Like clients/rotowire.py and clients/draftkings.py, this reads
public data an anonymous browser session already gets, but is still
automated access to a third party's undocumented endpoint -- worth
stating plainly. This client identifies itself honestly (the same
DEFAULT_HEADERS User-Agent every other client in this app uses) and
everything goes through the cache layer so a bad response never takes
the app down.

The whole reason this exists alongside clients/odds.py (The Odds API):
that client only ever surfaces the CURRENT line, never what it opened
at, and costs real credits for every pull. FantasyLabs' own dashboard
tracks both side by side for spread, moneyline, and total, for free.
Both services/mlb_slate.py and services/nfl_slate.py source every
game's score/total/spread/moneyline from here instead of odds.py --
odds.py's `get_game_lines()` is still called for each sport, but only
to get each game's Odds-API event_id, needed to fetch player props (the
one thing FantasyLabs' dashboard doesn't have, and still costs real
credits).

Confirmed live: querying ANY single date inside an NFL week returns
that WHOLE week's real games (not just that exact date) -- unlike MLB,
where a "day" genuinely means one calendar day of games, NFL's own week
spans Thu/Sun/Mon (sometimes an early international game too), so one
call with any of that week's real game dates is enough to cover it.
Also confirmed live: `HomeTeamShort`/`VisitorTeamShort` already match
this app's own nflverse team abbreviations exactly (including the
easy-to-get-wrong ones -- LA/LAC/LV, WAS) for both sports, so matching
a FantasyLabs row to a real game never needs MLB's own fuzzy full-name
matching (`mlb_slate._match_odds()`) for NFL -- a direct abbreviation
lookup is exact and simpler.
"""

from __future__ import annotations

import logging
from typing import Any

from app.cache import cached
from app.clients.http import get_json

log = logging.getLogger(__name__)

BASE = "https://www.fantasylabs.com/api/sportevents"
MLB_SPORT_ID = 3
NFL_SPORT_ID = 1

_TTL = 900  # 15 min -- lines move throughout the day, this is a free endpoint


async def _fetch_vegas_odds(sport_id: int, day: str, *, force: bool = False) -> list[dict[str, Any]]:
    async def _load() -> Any:
        return await get_json(
            f"{BASE}/{sport_id}/{day}/vegas", source="FantasyLabs"
        )

    try:
        events = await cached(f"fantasylabs:vegas:{sport_id}:{day}", _TTL, _load, force=force)
    except Exception as exc:
        log.warning("FantasyLabs vegas fetch failed (sport %s): %s", sport_id, exc)
        return []

    return [row for row in (_parse_event(e) for e in events or []) if row is not None]


async def get_vegas_odds(day: str, *, force: bool = False) -> list[dict[str, Any]]:
    """
    Open and current spread/moneyline/total/implied-runs for every MLB
    game on `day` (YYYY-MM-DD).

    Cached for 15 minutes -- pass `force=True` to bypass and pull a
    genuinely fresh read within that window (there's no credit cost to
    worry about here, unlike odds.py).
    """
    return await _fetch_vegas_odds(MLB_SPORT_ID, day, force=force)


async def get_nfl_vegas_odds(day: str, *, force: bool = False) -> list[dict[str, Any]]:
    """
    Open and current spread/moneyline/total for every NFL game in the
    real week containing `day` (YYYY-MM-DD) -- any real game date from
    that week works, see module docstring for why. Same 15-minute cache
    as the MLB version.
    """
    return await _fetch_vegas_odds(NFL_SPORT_ID, day, force=force)


def _parse_event(event: dict[str, Any]) -> dict[str, Any] | None:
    props = ((event or {}).get("EventDetails") or {}).get("Properties")
    if not props:
        return None

    return {
        "event_id": event.get("EventId"),
        "home_team": props.get("HomeTeam"),
        "away_team": props.get("VisitorTeam"),
        # Abbreviations -- confirmed live to already match this app's
        # own nflverse team codes exactly, so NFL matching can use a
        # direct lookup instead of MLB's fuzzy full-name matching.
        "home_short": props.get("HomeTeamShort"),
        "away_short": props.get("VisitorTeamShort"),
        "game_time_utc": props.get("EventDateTime"),
        "home_spread_open": props.get("HomeGameSpreadOpen"),
        "home_spread_current": props.get("HomeGameSpreadCurrent"),
        "away_spread_open": props.get("VisitorGameSpreadOpen"),
        "away_spread_current": props.get("VisitorGameSpreadCurrent"),
        "home_moneyline_open": props.get("HomeGameMoneylineOpen"),
        "home_moneyline_current": props.get("HomeGameMoneylineCurrent"),
        "away_moneyline_open": props.get("VisitorGameMoneylineOpen"),
        "away_moneyline_current": props.get("VisitorGameMoneylineCurrent"),
        # The total (over/under) is one game-level number -- Home/Visitor
        # copies of it are identical on FantasyLabs' own payload, so only
        # one pair of open/current values is kept.
        "total_open": props.get("HomeGameOUOpen"),
        "total_current": props.get("HomeGameOUCurrent"),
        "home_implied_runs_open": props.get("HomeVegasRunsOpen"),
        "home_implied_runs_current": props.get("HomeVegasRuns"),
        "away_implied_runs_open": props.get("VisitorVegasRunsOpen"),
        "away_implied_runs_current": props.get("VisitorVegasRuns"),
    }
