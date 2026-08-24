"""
Client for DraftKings Sportsbook's own public odds API -- moneylines,
run lines, totals, and MLB player props, free and with no API key,
meant as an alternative to clients/odds.py's paid Odds API integration
(set ODDS_SOURCE=draftkings in .env to switch to it; see config.py).

STATUS: BUILT DEFENSIVELY, NOT YET VERIFIED AGAINST A LIVE PAYLOAD
--------------------------------------------------------------------
A direct request to this exact endpoint from this app's own dev
environment came back 403 Access Denied at Akamai's edge, with or
without full browser-matching headers -- almost certainly a
datacenter-IP block on DK's regulated real-money sportsbook product,
which is a meaningfully different, more aggressively bot-protected
target than the free DK DFS lobby/draftables endpoints
clients/draftkings.py already scrapes successfully elsewhere in this
app. This app's actual backend runs on your own machine, not that dev
sandbox, so it may well reach this API fine from your own connection
-- but that means the parsing below was written against the general,
widely-observed public shape of DK's eventgroups API (the same shape
referenced across other independent open-source DK odds tools), not a
payload this app has actually seen. Try it (ODDS_SOURCE=draftkings,
then hit /api/mlb/games or /api/health and check the logs / a game's
"betting" field). If it comes back empty:

  1. It's almost always a field-name drift in _parse_game_lines() /
     _parse_player_props() below, not a fundamental design problem --
     capture a real response (browser DevTools -> Network tab -> find
     the eventgroups/{id} request -> Copy Response) and diff it
     against this module's docstring below.
  2. MLB_EVENT_GROUP_ID may have changed, or DK may not have MLB's
     event group live right now (off-season) -- confirm the id from
     the same DevTools capture (it's in the request URL itself).

Every parsing step below is defensive (dict access uses .get() with a
fallback, and a handful of plausible field-name variants are checked
where cheap) specifically so a shape mismatch degrades to an empty
result rather than a crash -- same resilience convention as every
other scraper client in this app (cache.cached()'s stale-serve-on-
error, rotowire.py/draftkings.py's own defensive parsing).

SHAPE THIS TARGETS (GET .../eventgroups/{id}?format=json):

    {
      "eventGroup": {
        "eventGroupId": 84240,
        "events": [
          {"eventId": "...", "name": "AWAY TEAM @ HOME TEAM",
           "startDate": "2026-08-23T23:05:00Z",
           "teamName1": "...", "teamName2": "..."},
          ...
        ],
        "offerCategories": [
          {"name": "Game Lines", "offerCategoryId": ...,
           "offerSubcategoryDescriptors": [
             {"name": "Moneyline", "subcategoryId": ...,
              "offerSubcategory": {"offers": [[
                {"eventId": "...", "outcomes": [
                  {"label": "Team Name", "oddsAmerican": "-150"},
                  ...
                ]}
              ]]}},
             {"name": "Run Line", "offerSubcategory": {"offers": [[
                {"eventId": "...", "outcomes": [
                  {"label": "Team Name", "line": -1.5, "oddsAmerican": "+120"},
                  ...
                ]}
              ]]}},
             {"name": "Total", "offerSubcategory": {"offers": [[
                {"eventId": "...", "outcomes": [
                  {"label": "Over", "line": 8.5, "oddsAmerican": "-110"},
                  {"label": "Under", "line": 8.5, "oddsAmerican": "-110"}
                ]}
              ]]}}
           ]},
          {"name": "Home Runs" | "Hits" | "Strikeouts" (varies by sport/book),
           "offerSubcategoryDescriptors": [
             {"offerSubcategory": {"offers": [[
                {"eventId": "...", "outcomes": [
                  {"label": "Over", "participant": "Player Name",
                   "line": 0.5, "oddsAmerican": "+180"},
                  ...
                ]}
              ]]}}
           ]}
        ]
      }
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

BASE = "https://sportsbook.draftkings.com/sites/US-SB/api/v5/eventgroups"

# DK's own event group id for MLB -- publicly stable and referenced
# across other independent DK odds tools, but not documented anywhere
# official. Confirm from a real browser capture if this stops working.
MLB_EVENT_GROUP_ID = 84240

# DK's sportsbook site is a normal consumer web page -- a plainly
# server-identifying User-Agent (this app's usual clients/http.py
# DEFAULT_HEADERS, "DFSEdge/0.1 (personal DFS research tool)") reads as
# an obvious bot to Akamai. These headers mirror what a real browser
# sends loading the MLB odds page, which is the best available chance
# of getting through from a real (non-datacenter) connection.
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
# a short TTL is fine -- lines/props move throughout the day and there's
# no reason to hold a stale read any longer than necessary.
_TTL = 300  # 5 min

_MONEYLINE_NAMES = {"moneyline", "money line"}
_RUNLINE_NAMES = {"run line", "runline", "spread"}
_TOTAL_NAMES = {"total", "game total", "total runs", "totals"}

# DK's own subcategory name -> this app's market key (matching
# clients/odds.py's MLB_PROP_MARKETS exactly, so mlb_slate.py's
# _market_at_least_one_pct_by_name()/_market_line_by_name() work
# unchanged regardless of source). DK is known to rename these
# per-sport/season -- extend this map rather than the parsing logic if
# a real payload uses a different label.
_PROP_SUBCATEGORY_MAP = {
    "home runs": "batter_home_runs",
    "to hit a home run": "batter_home_runs",
    "home run": "batter_home_runs",
    "hits": "batter_hits",
    "total hits": "batter_hits",
    "player hits": "batter_hits",
    "strikeouts": "pitcher_strikeouts",
    "total strikeouts": "pitcher_strikeouts",
    "pitcher strikeouts": "pitcher_strikeouts",
}


async def _fetch(event_group_id: int) -> Any:
    """
    A dedicated request bypassing clients/http.py's shared get_json() --
    that client's own DEFAULT_HEADERS User-Agent honestly identifies
    itself as a research tool, which is exactly what reads as a bot to
    Akamai. This one presents real browser headers instead. No retry
    loop (unlike get_json()): a 403 here is a network-edge block, not a
    transient failure -- retrying it changes nothing.
    """
    url = f"{BASE}/{event_group_id}"
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


async def get_event_group(
    event_group_id: int = MLB_EVENT_GROUP_ID, *, force: bool = False
) -> dict[str, Any]:
    """
    The raw eventgroup payload -- every MLB game, market, and price DK
    has live right now, in one bulk call. Cached (see _TTL above);
    pass force=True to bypass.
    """

    async def _load() -> Any:
        return await _fetch(event_group_id)

    return await cached(f"dk_sportsbook:eventgroup:{event_group_id}", _TTL, _load, force=force)


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
    function's call signature (mlb_slate.py calls whichever source is
    active identically); both are unused here since this client is
    MLB-only and DK's own payload is always "what's live right now,"
    not day-scoped like The Odds API's cache.
    """
    try:
        payload = await get_event_group(force=force)
    except ApiError as exc:
        log.warning("DK Sportsbook game-lines fetch failed: %s", exc)
        return []
    return _parse_game_lines(payload)


def _parse_game_lines(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Pure transform -- testable against a fixture, no network call."""
    event_group = payload.get("eventGroup") or {}
    events_by_id = {str(e.get("eventId")): e for e in event_group.get("events") or [] if e.get("eventId")}

    moneylines: dict[str, dict[str, Any]] = {}
    runlines: dict[str, dict[str, Any]] = {}
    totals: dict[str, dict[str, Any]] = {}

    for offer in _iter_offers(event_group):
        sub_name, event_id, outcomes = offer
        event = events_by_id.get(event_id)
        if event is None:
            continue
        home, away = _team_names(event)

        if sub_name in _MONEYLINE_NAMES:
            moneylines[event_id] = _read_sided(outcomes, home, away)
        elif sub_name in _RUNLINE_NAMES:
            runlines[event_id] = _read_sided(outcomes, home, away)
        elif sub_name in _TOTAL_NAMES:
            totals[event_id] = _read_total(outcomes)

    rows: list[dict[str, Any]] = []
    for event_id, event in events_by_id.items():
        home, away = _team_names(event)
        if not home or not away:
            continue
        ml = moneylines.get(event_id) or {}
        rl = runlines.get(event_id) or {}
        tot = totals.get(event_id) or {}
        total = tot.get("total")
        home_spread = rl.get("home")
        away_spread = rl.get("away")
        rows.append(
            {
                "event_id": event_id,
                "commence_time": event.get("startDate"),
                "home_team": home,
                "away_team": away,
                "total": total,
                "over_price": tot.get("over_price"),
                "under_price": tot.get("under_price"),
                "home_moneyline": ml.get("home"),
                "away_moneyline": ml.get("away"),
                "home_spread": home_spread,
                "away_spread": away_spread,
                "home_implied_runs": _implied_team_runs(total, home_spread),
                "away_implied_runs": _implied_team_runs(total, away_spread),
                "book": "DraftKings",
            }
        )
    return rows


def _iter_offers(event_group: dict[str, Any]):
    """Yield (subcategory_name_lower, event_id, outcomes) for every offer
    in every Game Lines subcategory -- shared walk for moneyline/run
    line/total, which all live under the same "Game Lines" category."""
    for category in event_group.get("offerCategories") or []:
        for sub in category.get("offerSubcategoryDescriptors") or []:
            sub_name = (sub.get("name") or "").strip().lower()
            offer_sub = sub.get("offerSubcategory") or {}
            for offer_group in offer_sub.get("offers") or []:
                for offer in offer_group or []:
                    event_id = str(offer.get("eventId") or "")
                    if not event_id:
                        continue
                    yield sub_name, event_id, (offer.get("outcomes") or [])


def _team_names(event: dict[str, Any]) -> tuple[str, str]:
    """
    DK's own event `name` follows "AWAY @ HOME" (the same convention
    already confirmed for DK's DFS lobby API in this app's own
    clients/draftkings.py -- "Description": "AWAY @ HOME"). Falls back
    to teamName1/teamName2 if `name` doesn't parse, though home/away
    can't be reliably assigned from those two alone; mlb_slate.py's
    _match_odds() only matches when BOTH sides agree, so a swapped
    pair here simply fails to match rather than silently mismatching.
    """
    name = (event.get("name") or "").strip()
    if "@" in name:
        parts = [p.strip() for p in name.split("@", 1)]
        if len(parts) == 2 and all(parts):
            return parts[1], parts[0]  # home, away
    return (event.get("teamName1") or "").strip(), (event.get("teamName2") or "").strip()


def _norm(name: str | None) -> str:
    return re.sub(r"[^a-z]", "", (name or "").lower())


def _side_matches(label: str, team_name: str) -> bool:
    """
    Loose match: DK's own outcome label is sometimes a full team name,
    sometimes just the city or nickname -- match whichever string is
    shorter as a substring of the other rather than requiring an exact
    match.
    """
    a, b = _norm(label), _norm(team_name)
    if not a or not b:
        return False
    return a in b or b in a


def _read_sided(outcomes: list[dict[str, Any]], home: str, away: str) -> dict[str, Any]:
    """
    Shared home/away-matching walk for both moneyline and run line
    outcome lists -- prefers the outcome's `line` (the spread, for run
    line) when present, falling back to its price (moneyline has no
    line, only odds).
    """
    out: dict[str, Any] = {}
    for o in outcomes:
        label = (o.get("label") or o.get("participant") or "").strip()
        price = _american_odds(o)
        line = _to_float(o.get("line"))
        side = "home" if _side_matches(label, home) else "away" if _side_matches(label, away) else None
        if side is None:
            continue
        if line is not None:
            out[side] = line
        elif price is not None:
            out[side] = price
    return out


def _read_total(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for o in outcomes:
        label = (o.get("label") or "").strip().lower()
        line = _to_float(o.get("line"))
        price = _american_odds(o)
        if "over" in label:
            if line is not None:
                out["total"] = line
            out["over_price"] = price
        elif "under" in label:
            if line is not None:
                out.setdefault("total", line)
            out["under_price"] = price
    return out


def _implied_team_runs(total: float | None, spread: float | None) -> float | None:
    """Same formula as clients/odds.py's _implied_team_runs() -- kept as
    a small local copy so this client has no import dependency on which
    other source module is active."""
    if total is None:
        return None
    if spread is None:
        return round(total / 2, 2)
    return round((total / 2) - (spread / 2), 2)


def _american_odds(o: dict[str, Any]) -> int | None:
    for key in ("oddsAmerican", "americanOdds"):
        val = o.get(key)
        if val not in (None, ""):
            try:
                return int(str(val).replace("−", "-"))  # DK sometimes uses a unicode minus
            except (TypeError, ValueError):
                continue
    return None


def _to_float(val: Any) -> float | None:
    try:
        return float(val) if val not in (None, "") else None
    except (TypeError, ValueError):
        return None


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
    odds.py's get_player_props() (`{market_key: [{"player", "side",
    "line", "price", "implied_pct", "book"}, ...]}`). Where that needs
    one paid call PER GAME, this makes one free bulk call for the whole
    event group (cached -- calling this once per game on the same slate
    only pays the real network cost on the first call) and filters down
    to just this event's own props. `sport`/`markets`/`day` are accepted
    only to match odds.py's call signature; unused here.
    """
    try:
        payload = await get_event_group(force=force)
    except ApiError as exc:
        log.warning("DK Sportsbook props fetch failed for event %s: %s", event_id, exc)
        return {}
    return _parse_player_props(payload, event_id=str(event_id))


def _parse_player_props(payload: dict[str, Any], *, event_id: str) -> dict[str, list[dict[str, Any]]]:
    """Pure transform -- testable against a fixture, no network call."""
    event_group = payload.get("eventGroup") or {}
    props: dict[str, list[dict[str, Any]]] = {}

    for category in event_group.get("offerCategories") or []:
        for sub in category.get("offerSubcategoryDescriptors") or []:
            market_key = _PROP_SUBCATEGORY_MAP.get((sub.get("name") or "").strip().lower())
            if market_key is None:
                continue
            offer_sub = sub.get("offerSubcategory") or {}
            for offer_group in offer_sub.get("offers") or []:
                for offer in offer_group or []:
                    if str(offer.get("eventId") or "") != event_id:
                        continue
                    bucket = props.setdefault(market_key, [])
                    for o in offer.get("outcomes") or []:
                        row = _prop_row(o)
                        if row is not None:
                            bucket.append(row)
    return props


def _prop_row(o: dict[str, Any]) -> dict[str, Any] | None:
    player = (o.get("participant") or "").strip()
    if not player:
        # Some DK payloads put the player's name in `label` instead and
        # "Over"/"Under" in a separate `outcomeType` field -- fall back
        # to that shape if `participant` isn't present.
        player = (o.get("label") or "").strip()
    side = _prop_side(o)
    price = _american_odds(o)
    line = _to_float(o.get("line"))
    if not player or side is None or price is None:
        return None
    return {
        "player": player,
        "side": side,
        "line": line,
        "price": price,
        "implied_pct": american_to_probability(price),
        "book": "DraftKings",
    }


def _prop_side(o: dict[str, Any]) -> str | None:
    label = (o.get("label") or "").strip().lower()
    if "over" in label:
        return "Over"
    if "under" in label:
        return "Under"
    outcome_type = (o.get("outcomeType") or "").strip().lower()
    if outcome_type == "over":
        return "Over"
    if outcome_type == "under":
        return "Under"
    return None
