"""
Betting lines via The Odds API (the-odds-api.com).

CREDIT COST -- read this before turning props on
------------------------------------------------
The Odds API charges "credits". Roughly:

  * One /odds call for a whole sport costs
        (number of markets) x (number of regions)
    ...so pulling h2h + spreads + totals for all of MLB costs 3 credits.
    Refreshing every 10 minutes for 12 hours = ~216 credits/day. That
    already exceeds the free tier's 500/month.

  * Player props are priced PER GAME. Pulling 4 prop markets across a
    15-game slate costs ~60 credits per refresh. Cache aggressively.

The $30/month plan gives 20,000 credits, which comfortably covers game
lines refreshed every 10 minutes plus a few prop pulls per slate.

Your remaining balance comes back in response headers on every call and
is surfaced at /api/health so you can keep an eye on it.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.cache import cached, get, put
from app.clients.http import ApiError, get_client
from app.config import get_settings

log = logging.getLogger(__name__)

BASE = "https://api.the-odds-api.com/v4"
SPORT_KEYS = {"mlb": "baseball_mlb", "nfl": "americanfootball_nfl", "nba": "basketball_nba"}

# Batter/pitcher prop markets worth pulling for MLB DFS.
MLB_PROP_MARKETS = [
    "batter_home_runs",
    "batter_hits",
    "batter_total_bases",
    "pitcher_strikeouts",
]

_USAGE_KEY = "odds:usage"


async def _get(path: str, params: dict[str, Any]) -> Any:
    """GET against The Odds API, recording remaining credits."""
    settings = get_settings()
    if not settings.odds_api_key:
        raise ApiError("No ODDS_API_KEY set", source="The Odds API")

    client = get_client()
    params = {**params, "apiKey": settings.odds_api_key}

    try:
        resp = await client.get(f"{BASE}{path}", params=params)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise ApiError(f"Could not reach The Odds API: {exc}", source="The Odds API") from exc

    remaining = resp.headers.get("x-requests-remaining")
    used = resp.headers.get("x-requests-used")
    last_cost = resp.headers.get("x-requests-last")
    if remaining is not None:
        put(
            _USAGE_KEY,
            {"remaining": remaining, "used": used, "last_call_cost": last_cost},
            ttl=86_400,
        )

    if resp.status_code == 401:
        raise ApiError(
            "The Odds API rejected your key (401). Check ODDS_API_KEY in .env.",
            status=401,
            source="The Odds API",
        )
    if resp.status_code == 422:
        raise ApiError(
            "The Odds API rejected the request (422) - usually an unsupported "
            "market for your plan. Player props need a paid plan.",
            status=422,
            source="The Odds API",
        )
    if resp.status_code != 200:
        raise ApiError(
            f"The Odds API returned HTTP {resp.status_code}: {resp.text[:200]}",
            status=resp.status_code,
            source="The Odds API",
        )

    return resp.json()


def get_usage() -> dict[str, Any] | None:
    """Last known credit balance, for the health endpoint."""
    return get(_USAGE_KEY)


# --------------------------------------------------------------------------
# Game lines
# --------------------------------------------------------------------------

async def get_game_lines(sport: str = "mlb", *, force: bool = False) -> list[dict[str, Any]]:
    """
    Moneyline, run line and total for every upcoming game.

    The total is the single most useful number on the whole dashboard:
    it is the market's estimate of combined runs, and it correlates with
    DFS scoring better than almost anything you can compute yourself.
    """
    settings = get_settings()
    if not settings.odds_api_key:
        return []

    sport_key = SPORT_KEYS.get(sport, sport)

    async def _load() -> Any:
        return await _get(
            f"/sports/{sport_key}/odds",
            {
                "regions": "us",
                "markets": "h2h,spreads,totals",
                "oddsFormat": "american",
                "bookmakers": ",".join(settings.odds_bookmakers),
            },
        )

    try:
        events = await cached(
            f"odds:{sport}:lines", settings.ttl_odds, _load, force=force
        )
    except ApiError as exc:
        log.warning("Odds fetch failed: %s", exc)
        return []

    return [_simplify_event(e) for e in events or []]


def _simplify_event(event: dict[str, Any]) -> dict[str, Any]:
    """Flatten the nested bookmaker structure into one clean row."""
    home = event.get("home_team")
    away = event.get("away_team")

    total, over_price, under_price = None, None, None
    home_ml, away_ml = None, None
    home_spread, away_spread = None, None
    book_used = None

    for book in event.get("bookmakers") or []:
        for market in book.get("markets") or []:
            key = market.get("key")
            outcomes = market.get("outcomes") or []

            if key == "totals" and total is None:
                for o in outcomes:
                    if o.get("name") == "Over":
                        total = o.get("point")
                        over_price = o.get("price")
                    elif o.get("name") == "Under":
                        under_price = o.get("price")
                book_used = book.get("title")

            elif key == "h2h" and home_ml is None:
                for o in outcomes:
                    if o.get("name") == home:
                        home_ml = o.get("price")
                    elif o.get("name") == away:
                        away_ml = o.get("price")

            elif key == "spreads" and home_spread is None:
                for o in outcomes:
                    if o.get("name") == home:
                        home_spread = o.get("point")
                    elif o.get("name") == away:
                        away_spread = o.get("point")

    return {
        "event_id": event.get("id"),
        "commence_time": event.get("commence_time"),
        "home_team": home,
        "away_team": away,
        "total": total,
        "over_price": over_price,
        "under_price": under_price,
        "home_moneyline": home_ml,
        "away_moneyline": away_ml,
        "home_spread": home_spread,
        "away_spread": away_spread,
        "home_implied_runs": _implied_team_runs(total, home_spread),
        "away_implied_runs": _implied_team_runs(total, away_spread),
        "book": book_used,
    }


def _implied_team_runs(total: float | None, spread: float | None) -> float | None:
    """
    Split the game total into each team's expected runs using the run line.

    team runs = (total / 2) - (spread / 2)

    A team favoured by -1.5 in a 9-run game is implied for 5.25 runs and
    their opponent 3.75. Implied team total is THE stacking metric --
    you want your bats in the highest implied-run offences on the slate.
    """
    if total is None:
        return None
    if spread is None:
        return round(total / 2, 2)
    return round((total / 2) - (spread / 2), 2)


# --------------------------------------------------------------------------
# Player props (optional -- costs credits per game)
# --------------------------------------------------------------------------

async def get_player_props(
    event_id: str,
    sport: str = "mlb",
    markets: list[str] | None = None,
) -> dict[str, Any]:
    """
    Prop lines for a single game. Only called when ODDS_FETCH_PROPS=true.

    Props are gold for DFS -- a batter priced at +320 to hit a home run
    is the market telling you his HR probability is about 24%, which you
    can compare against his salary.
    """
    settings = get_settings()
    if not settings.odds_api_key or not settings.odds_fetch_props:
        return {}

    sport_key = SPORT_KEYS.get(sport, sport)
    markets = markets or MLB_PROP_MARKETS

    async def _load() -> Any:
        return await _get(
            f"/sports/{sport_key}/events/{event_id}/odds",
            {
                "regions": "us",
                "markets": ",".join(markets),
                "oddsFormat": "american",
                "bookmakers": ",".join(settings.odds_bookmakers),
            },
        )

    try:
        payload = await cached(
            f"odds:props:{event_id}", settings.ttl_odds, _load
        )
    except ApiError as exc:
        log.warning("Prop fetch failed for %s: %s", event_id, exc)
        return {}

    props: dict[str, list[dict[str, Any]]] = {}
    for book in payload.get("bookmakers") or []:
        for market in book.get("markets") or []:
            key = market.get("key")
            bucket = props.setdefault(key, [])
            for o in market.get("outcomes") or []:
                bucket.append(
                    {
                        "player": o.get("description") or o.get("name"),
                        "side": o.get("name"),
                        "line": o.get("point"),
                        "price": o.get("price"),
                        "implied_pct": american_to_probability(o.get("price")),
                        "book": book.get("title"),
                    }
                )
    return props


def american_to_probability(price: Any) -> float | None:
    """
    Convert American odds to an implied probability percentage.

    Note this includes the book's vig, so the probabilities across a
    market will add up to slightly more than 100%.
    """
    try:
        p = float(price)
    except (TypeError, ValueError):
        return None
    if p > 0:
        return round(100 / (p + 100) * 100, 1)
    return round(-p / (-p + 100) * 100, 1)
