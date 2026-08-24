"""
Betting lines via The Odds API (the-odds-api.com).

GAME LINES ARE NO LONGER DISPLAYED FROM HERE -- see clients/fantasylabs.py.
services/mlb_slate.py sources every game's actual score/total/spread/
moneyline/implied-runs from FantasyLabs' free Vegas dashboard instead
(no credit cost, and it also has the OPENING line, which this API never
exposed). `get_game_lines()` below is still called once per slate build,
but ONLY to read each game's Odds-API `event_id` off the response --
that id is what `get_player_props()` needs to fetch props for a specific
game, and FantasyLabs has no props data at all. The h2h/spreads/totals
values in `get_game_lines()`'s own return shape are computed and cached
same as always, just no longer read by anything.

CREDIT COST -- read this before turning props on
------------------------------------------------
The Odds API charges "credits". Roughly:

  * One /odds call for a whole sport costs
        (number of markets) x (number of regions)
    ...so pulling h2h + spreads + totals for all of MLB costs 3 credits
    -- paid purely for the event_id lookup now, see above.

  * Player props are priced PER GAME (there's no bulk multi-game props
    call, only per-event). Pulling this app's 3 prop markets
    (batter_home_runs, batter_hits, pitcher_strikeouts) across a
    15-game slate costs ~45 credits per full refresh.

Both `get_game_lines()` and `get_player_props()` cache by CALENDAR DAY
(the cache key embeds `day`, with a multi-day TTL) rather than a short
rolling TTL -- the first request for a given day fetches, everything
after that for the rest of the day is served from cache, regardless of
how many times the slate gets rebuilt. Pass `force=True` (the existing
"Refresh matchups" button already does) to explicitly bypass this and
pull a genuinely fresh line/prop within the same day.

At that once-a-day cadence, a real 500-credit/month free-tier budget
covers game lines (3 credits/day = ~90/month, now spent solely to
unlock props' event_id) plus 3-market props on a realistic ~10-game
slate (30 credits/day = ~900/month for props ALONE if pulled every
single day) -- still tight for a full-season daily habit, but the
$30/month/20,000-credit plan comfortably covers both at this cadence
with a lot of room to spare.

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

# Batter/pitcher prop markets worth pulling for MLB DFS. batter_total_bases
# was deliberately dropped (kept the 3 markets with the clearest, most
# directly DFS-relevant read) to cut the per-game cost from 4 credits to 3.
MLB_PROP_MARKETS = [
    "batter_home_runs",
    "batter_hits",
    "pitcher_strikeouts",
]

_USAGE_KEY = "odds:usage"

# Cache TTL for both game lines and player props -- day-keyed cache
# entries (see get_game_lines/get_player_props below) only ever need to
# outlive the day they're for, but a multi-day TTL matches this app's
# existing convention for every other day-keyed cache (projections.py,
# salaries.py, dk_entries.py all use the same value) and costs nothing
# extra, since a stale prior day's key is simply never looked up again.
_DAILY_TTL = 60 * 60 * 24 * 7


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

async def get_game_lines(
    sport: str = "mlb", *, day: str, force: bool = False
) -> list[dict[str, Any]]:
    """
    Moneyline, run line and total for every upcoming game.

    The total is the single most useful number on the whole dashboard:
    it is the market's estimate of combined runs, and it correlates with
    DFS scoring better than almost anything you can compute yourself.

    Cached once per `day` (see the module docstring's credit-cost math)
    -- pass `force=True` to explicitly bypass and pull a fresh line
    within the same day (the existing "Refresh matchups" button does).
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
            f"odds:{sport}:lines:{day}", _DAILY_TTL, _load, force=force
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
    *,
    day: str,
    force: bool = False,
) -> dict[str, Any]:
    """
    Prop lines for a single game. Only called when ODDS_FETCH_PROPS=true.

    Props are gold for DFS -- a batter priced at +320 to hit a home run
    is the market telling you his HR probability is about 24%, which you
    can compare against his salary.

    Cached once per `day` per event, same "once a day, force to bypass"
    policy as get_game_lines() -- see the module docstring's credit-cost
    math for why this matters so much more here (props are billed per
    GAME, not once for the whole slate).
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
            f"odds:props:{event_id}:{day}", _DAILY_TTL, _load, force=force
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
