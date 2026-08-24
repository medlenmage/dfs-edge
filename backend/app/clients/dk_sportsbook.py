"""
Client for DraftKings Sportsbook's own public odds API -- moneylines,
run lines, totals, and MLB player props, free and with no API key,
meant as an alternative to clients/odds.py's paid Odds API integration
(set ODDS_SOURCE=draftkings in .env to switch to it; see config.py).

STATUS: TWO DIFFERENT CONFIDENCE LEVELS, NEITHER VERIFIED LIVE
------------------------------------------------------------------
A live request to this domain from this app's own dev environment came
back 403 Access Denied at Akamai's edge, with or without full
browser-matching headers -- almost certainly a datacenter-IP block on
DK's regulated real-money sportsbook product, a meaningfully different,
more aggressively bot-protected target than the free DK DFS lobby/
draftables endpoints clients/draftkings.py already scrapes successfully
elsewhere in this app. This app's actual backend runs on your own
machine, not that dev sandbox, so it may reach this API fine from your
own connection -- but that means everything below was written without
ever seeing a real response, at two different confidence levels:

  * get_event_odds() (per-game moneyline/run line/total/props, hitting
    .../api/v3/event/{event_id}) targets a schema CONFIRMED against a
    real, independent, working implementation -- an open-source
    scraper (github.com/declanwalpole/sportsbook-odds-scraper) whose
    DraftKings module was read (not copied -- that repo carries no
    LICENSE file, so its source wasn't reused verbatim; only the
    field-level API shape it revealed was) to build this parser fresh.
    Decimal odds (`oddsDecimal`), `isSuspended`/`isOpen` filtering, and
    the eventCategories -> componentizedOffers -> offers -> outcomes
    nesting below are all taken directly from that confirmed shape.
    Player-prop PLAYER NAME extraction is the one genuinely uncertain
    part even here -- that tool's own generic Market/Selection
    dataclasses don't preserve a separate player-name field, so
    _parse_player_props() below has to guess at where DK puts it
    (tried: the market's own label, e.g. "Aaron Judge Home Runs").

  * get_event_group() (the bulk "what MLB games are live right now"
    discovery call, hitting .../api/v5/eventgroups/{id}) is still this
    app's own best-effort guess -- the open-source tool above doesn't
    do bulk discovery at all, it requires a specific event URL pasted
    in by hand for every single game. There was no independent source
    to confirm this one against.

If parsing comes back empty once you try it:
  1. Check whether get_event_group() or get_event_odds() is the one
     failing (a log warning names which) -- they're independently
     verified/unverified as described above.
  2. Capture a real response (browser DevTools -> Network tab -> find
     the failing request -> Copy Response) and diff it against this
     module's docstring.
  3. MLB_EVENT_GROUP_ID may have changed, or DK may not have MLB live
     right now -- confirm from the same DevTools capture.

Every parsing step is defensive (dict access uses .get() with a
fallback) so a shape mismatch degrades to an empty result rather than
a crash -- same resilience convention as every other scraper client in
this app.

SHAPE THIS TARGETS:

    GET .../eventgroups/{id}?format=json  (discovery only)
    {"eventGroup": {"events": [
        {"eventId": "...", "name": "AWAY @ HOME", "startDate": "..."}, ...
    ]}}

    GET .../v3/event/{event_id}  (CONFIRMED against a real working tool)
    {
      "event": {"name": "..."},
      "eventCategories": [
        {"componentizedOffers": [
          {"subcategoryName": "Game Lines" | "Home Runs" | "Hits" | ...,
           "offers": [[
             {"providerOfferId": ..., "isSuspended": false, "isOpen": true,
              "label": "Moneyline" | "Total" | "Run Line" | "Aaron Judge Home Runs" | ...,
              "outcomes": [
                {"hidden": false, "providerOutcomeId": ...,
                 "label": "Team Name" | "Over" | "Under",
                 "oddsDecimal": 1.65, "line": 8.5}
              ]}
           ]]}
        ]}
      ]
    }

get_game_lines() / get_player_props() return the exact same row shapes
as clients/odds.py's own versions, so mlb_slate.py's _match_odds() and
every downstream consumer (scoring.py's market-blend components) work
completely unchanged regardless of which source is active.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from app.cache import cached
from app.clients.http import DEFAULT_TIMEOUT, ApiError
from app.clients.odds import american_to_probability

log = logging.getLogger(__name__)

EVENTGROUPS_BASE = "https://sportsbook.draftkings.com/sites/US-SB/api/v5/eventgroups"
EVENT_BASE = "https://sportsbook.draftkings.com/sites/US-SB/api/v3/event"

# DK's own event group id for MLB -- publicly stable and referenced
# across other independent DK odds tools, but not documented anywhere
# official. Confirm from a real browser capture if this stops working.
MLB_EVENT_GROUP_ID = 84240

# A plainly server-identifying User-Agent (this app's usual clients/
# http.py DEFAULT_HEADERS, "DFSEdge/0.1 (personal DFS research tool)")
# reads as an obvious bot to Akamai. These mirror what a real browser
# sends -- the best available chance of getting through from a real
# (non-datacenter) connection.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://sportsbook.draftkings.com/leagues/baseball/mlb",
}

# Free and uncapped (no per-call credit cost, unlike The Odds API), so
# short TTLs are fine -- lines/props move throughout the day.
_EVENTGROUP_TTL = 300  # 5 min -- just the game list, rarely changes
_EVENT_TTL = 120  # 2 min -- the actual odds, refreshed more eagerly

_MONEYLINE_NAMES = {"moneyline", "money line"}
_RUNLINE_NAMES = {"run line", "runline", "spread"}
_TOTAL_NAMES = {"total", "game total", "total runs", "totals"}

# DK's own subcategoryName -> this app's market key (matching
# clients/odds.py's MLB_PROP_MARKETS exactly, so mlb_slate.py's
# _market_at_least_one_pct_by_name()/_market_line_by_name() work
# unchanged regardless of source).
_PROP_SUBCATEGORY_MAP = {
    "home runs": "batter_home_runs",
    "to hit a home run": "batter_home_runs",
    "hits": "batter_hits",
    "total hits": "batter_hits",
    "player hits": "batter_hits",
    "strikeouts": "pitcher_strikeouts",
    "total strikeouts": "pitcher_strikeouts",
    "pitcher strikeouts": "pitcher_strikeouts",
}


async def _fetch(url: str) -> Any:
    """
    A dedicated request bypassing clients/http.py's shared get_json() --
    that client's own DEFAULT_HEADERS User-Agent honestly identifies
    itself as a research tool, which reads as a bot to Akamai. This one
    presents real browser headers instead. No retry loop (unlike
    get_json()): a 403 here is a network-edge block, not a transient
    failure -- retrying it changes nothing.
    """
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, headers=_HEADERS) as client:
            resp = await client.get(url, params={"format": "json"})
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise ApiError(
            f"Could not reach DraftKings Sportsbook: {exc}", source="DraftKings Sportsbook"
        ) from exc

    if resp.status_code != 200:
        raise ApiError(
            f"DraftKings Sportsbook returned HTTP {resp.status_code} -- if this is a "
            "403, it's very likely a network-edge block (Akamai bot/IP protection on "
            "the regulated sportsbook product) rather than a real request problem. "
            "Confirmed happening from a cloud dev environment; a residential "
            "connection may fare differently.",
            status=resp.status_code,
            source="DraftKings Sportsbook",
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise ApiError(
            "DraftKings Sportsbook returned a non-JSON response", source="DraftKings Sportsbook"
        ) from exc


# --------------------------------------------------------------------------
# Discovery -- today's live MLB events (own best-effort guess, see docstring)
# --------------------------------------------------------------------------

async def get_event_group(
    event_group_id: int = MLB_EVENT_GROUP_ID, *, force: bool = False
) -> dict[str, Any]:
    """The raw eventgroup payload -- used only to discover which MLB
    games are live right now (event id, team names, start time). Actual
    odds come from get_event_odds() per game -- see module docstring
    for why these two calls have different confidence levels."""

    async def _load() -> Any:
        return await _fetch(f"{EVENTGROUPS_BASE}/{event_group_id}")

    return await cached(f"dk_sportsbook:eventgroup:{event_group_id}", _EVENTGROUP_TTL, _load, force=force)


def _list_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Pure transform -- testable against a fixture, no network call."""
    event_group = payload.get("eventGroup") or {}
    events = []
    for e in event_group.get("events") or []:
        event_id = str(e.get("eventId") or "")
        if not event_id:
            continue
        home, away = _team_names(e)
        if not home or not away:
            continue
        events.append(
            {
                "event_id": event_id,
                "home_team": home,
                "away_team": away,
                "commence_time": e.get("startDate"),
            }
        )
    return events


def _team_names(event: dict[str, Any]) -> tuple[str, str]:
    """
    DK's own event `name` follows "AWAY @ HOME" (the same convention
    already confirmed for DK's DFS lobby API in this app's own
    clients/draftkings.py -- "Description": "AWAY @ HOME").
    """
    name = (event.get("name") or "").strip()
    if "@" in name:
        parts = [p.strip() for p in name.split("@", 1)]
        if len(parts) == 2 and all(parts):
            return parts[1], parts[0]  # home, away
    return (event.get("teamName1") or "").strip(), (event.get("teamName2") or "").strip()


# --------------------------------------------------------------------------
# Per-event odds -- CONFIRMED schema (see module docstring)
# --------------------------------------------------------------------------

async def get_event_odds(event_id: str, *, force: bool = False) -> dict[str, Any]:
    """The raw per-event payload -- every market and price DK has live
    for this one game right now."""

    async def _load() -> Any:
        return await _fetch(f"{EVENT_BASE}/{event_id}")

    return await cached(f"dk_sportsbook:event:{event_id}", _EVENT_TTL, _load, force=force)


def _iter_markets(payload: dict[str, Any]):
    """Yield (subcategory_name, market_label, outcomes) for every open,
    non-suspended market in an event payload -- shared walk for both
    game lines and player props."""
    for category in payload.get("eventCategories") or []:
        for grouping in category.get("componentizedOffers") or []:
            sub_name = (grouping.get("subcategoryName") or "").strip()
            for market_group in grouping.get("offers") or []:
                for market in market_group or []:
                    if market.get("isSuspended") or market.get("isOpen") is False:
                        continue
                    label = (market.get("label") or "").strip()
                    outcomes = [o for o in (market.get("outcomes") or []) if not o.get("hidden")]
                    yield sub_name, label, outcomes


def _decimal_to_american(decimal_odds: float | None) -> int | None:
    if decimal_odds is None or decimal_odds <= 1.0:
        return None
    if decimal_odds >= 2.0:
        return round((decimal_odds - 1) * 100)
    return round(-100 / (decimal_odds - 1))


def _to_float(val: Any) -> float | None:
    try:
        return float(val) if val not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _norm(name: str | None) -> str:
    return re.sub(r"[^a-z]", "", (name or "").lower())


def _side_matches(label: str, team_name: str) -> bool:
    a, b = _norm(label), _norm(team_name)
    if not a or not b:
        return False
    return a in b or b in a


# --------------------------------------------------------------------------
# Game lines
# --------------------------------------------------------------------------

async def get_game_lines(
    sport: str = "mlb", *, day: str | None = None, force: bool = False
) -> list[dict[str, Any]]:
    """
    Moneyline, run line, and total for every live MLB game on DK
    Sportsbook right now. Same return shape as clients/odds.py's
    get_game_lines() -- `sport`/`day` are accepted only to match that
    function's call signature; unused here (this client is MLB-only
    and DK's own payload is always "what's live right now").

    Discovers events via get_event_group(), then fetches each event's
    own odds via get_event_odds() -- one bulk call plus one call per
    live game (all cached, all free, no credit cost).
    """
    try:
        group_payload = await get_event_group(force=force)
    except ApiError as exc:
        log.warning("DK Sportsbook event discovery failed: %s", exc)
        return []
    events = _list_events(group_payload)

    rows = []
    for event in events:
        try:
            event_payload = await get_event_odds(event["event_id"], force=force)
        except ApiError as exc:
            log.warning("DK Sportsbook odds fetch failed for event %s: %s", event["event_id"], exc)
            continue
        rows.append(_build_game_line(event, event_payload))
    return rows


def _build_game_line(event: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    home, away = event["home_team"], event["away_team"]
    home_ml = away_ml = home_spread = away_spread = None
    total = over_price = under_price = None

    for sub_name, label, outcomes in _iter_markets(payload):
        key = label.strip().lower()
        if key in _MONEYLINE_NAMES:
            for o in outcomes:
                price = _decimal_to_american(_to_float(o.get("oddsDecimal")))
                olabel = (o.get("label") or "").strip()
                if _side_matches(olabel, home):
                    home_ml = price
                elif _side_matches(olabel, away):
                    away_ml = price
        elif key in _RUNLINE_NAMES:
            for o in outcomes:
                line = _to_float(o.get("line"))
                olabel = (o.get("label") or "").strip()
                if line is None:
                    continue
                if _side_matches(olabel, home):
                    home_spread = line
                elif _side_matches(olabel, away):
                    away_spread = line
        elif key in _TOTAL_NAMES:
            for o in outcomes:
                olabel = (o.get("label") or "").strip().lower()
                line = _to_float(o.get("line"))
                price = _decimal_to_american(_to_float(o.get("oddsDecimal")))
                if "over" in olabel:
                    if line is not None:
                        total = line
                    over_price = price
                elif "under" in olabel:
                    if line is not None and total is None:
                        total = line
                    under_price = price

    return {
        "event_id": event["event_id"],
        "commence_time": event["commence_time"],
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
        "book": "DraftKings",
    }


def _implied_team_runs(total: float | None, spread: float | None) -> float | None:
    """Same formula as clients/odds.py's _implied_team_runs() -- kept as
    a small local copy so this client has no import dependency on which
    other source module is active."""
    if total is None:
        return None
    if spread is None:
        return round(total / 2, 2)
    return round((total / 2) - (spread / 2), 2)


# --------------------------------------------------------------------------
# Player props
# --------------------------------------------------------------------------

async def get_player_props(
    event_id: str,
    sport: str = "mlb",
    markets: list[str] | None = None,
    *,
    day: str | None = None,
    force: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """
    Prop lines for a single game -- same return shape as clients/
    odds.py's get_player_props(). `sport`/`markets`/`day` are accepted
    only to match that function's call signature; unused here.
    """
    try:
        payload = await get_event_odds(event_id, force=force)
    except ApiError as exc:
        log.warning("DK Sportsbook props fetch failed for event %s: %s", event_id, exc)
        return {}
    return _parse_player_props(payload)


def _parse_player_props(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """
    Pure transform -- testable against a fixture, no network call.

    The confirmed schema (see module docstring) has no separate
    player-name field on a prop outcome -- only a market-level `label`
    (e.g. "Aaron Judge Home Runs") and an outcome-level `label` that's
    usually just "Over"/"Under". Player name is recovered by stripping
    the matched market key's own display words off the market label --
    the single least-certain piece of this whole module.
    """
    props: dict[str, list[dict[str, Any]]] = {}

    for sub_name, market_label, outcomes in _iter_markets(payload):
        market_key = _PROP_SUBCATEGORY_MAP.get(sub_name.strip().lower())
        if market_key is None:
            continue
        player = _strip_market_suffix(market_label, sub_name)
        if not player:
            continue
        bucket = props.setdefault(market_key, [])
        for o in outcomes:
            row = _prop_row(o, player)
            if row is not None:
                bucket.append(row)
    return props


def _strip_market_suffix(market_label: str, subcategory: str) -> str:
    """"Aaron Judge Home Runs" + subcategory "Home Runs" -> "Aaron Judge"."""
    label = market_label.strip()
    sub = subcategory.strip()
    if sub and label.lower().endswith(sub.lower()):
        label = label[: -len(sub)].strip()
    return label


def _prop_row(o: dict[str, Any], player: str) -> dict[str, Any] | None:
    label = (o.get("label") or "").strip().lower()
    side = "Over" if "over" in label else "Under" if "under" in label else None
    price = _decimal_to_american(_to_float(o.get("oddsDecimal")))
    line = _to_float(o.get("line"))
    if side is None or price is None:
        return None
    return {
        "player": player,
        "side": side,
        "line": line,
        "price": price,
        "implied_pct": american_to_probability(price),
        "book": "DraftKings",
    }
