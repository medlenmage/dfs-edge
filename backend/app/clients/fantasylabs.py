"""
Client for FantasyLabs' MLB Vegas dashboard (fantasylabs.com/mlb/vegas).

Free, no API key, no login wall -- the same JSON endpoint the dashboard
itself calls in your browser (confirmed live via the network tab, not
documented anywhere: `GET /api/sportevents/{sportId}/{date}/vegas`,
sportId=3 for MLB). robots.txt has no disallow rules. Like
clients/rotowire.py and clients/draftkings.py, this reads public data an
anonymous browser session already gets, but is still automated access to
a third party's undocumented endpoint -- worth stating plainly. This
client identifies itself honestly (the same DEFAULT_HEADERS User-Agent
every other client in this app uses) and everything goes through the
cache layer so a bad response never takes the app down.

The whole reason this exists alongside clients/odds.py (The Odds API):
that client only ever surfaces the CURRENT line, never what it opened
at, and costs real credits for every pull. FantasyLabs' own dashboard
tracks both side by side for spread, moneyline, and total, plus each
team's own implied run total (their "Vegas Runs" number) -- open AND
current for that too, for free. services/mlb_slate.py now sources every
game's score/total/spread/moneyline/implied-runs from here instead of
odds.py -- odds.py's `get_game_lines()` is still called, but only to get
each game's Odds-API event_id, needed to fetch player props (the one
thing FantasyLabs' dashboard doesn't have, and still costs real
credits).
"""

from __future__ import annotations

import logging
from typing import Any

from app.cache import cached
from app.clients.http import get_json

log = logging.getLogger(__name__)

BASE = "https://www.fantasylabs.com/api/sportevents"
MLB_SPORT_ID = 3

_TTL = 900  # 15 min -- lines move throughout the day, this is a free endpoint


async def get_vegas_odds(day: str, *, force: bool = False) -> list[dict[str, Any]]:
    """
    Open and current spread/moneyline/total/implied-runs for every MLB
    game on `day` (YYYY-MM-DD).

    Cached for 15 minutes -- pass `force=True` to bypass and pull a
    genuinely fresh read within that window (there's no credit cost to
    worry about here, unlike odds.py).
    """
    async def _load() -> Any:
        return await get_json(
            f"{BASE}/{MLB_SPORT_ID}/{day}/vegas", source="FantasyLabs"
        )

    try:
        events = await cached(f"fantasylabs:vegas:{day}", _TTL, _load, force=force)
    except Exception as exc:
        log.warning("FantasyLabs vegas fetch failed: %s", exc)
        return []

    return [row for row in (_parse_event(e) for e in events or []) if row is not None]


def _parse_event(event: dict[str, Any]) -> dict[str, Any] | None:
    props = ((event or {}).get("EventDetails") or {}).get("Properties")
    if not props:
        return None

    return {
        "event_id": event.get("EventId"),
        "home_team": props.get("HomeTeam"),
        "away_team": props.get("VisitorTeam"),
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
