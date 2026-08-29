"""
Client for RotoWire's own MLB DFS lineup optimizer.

Free, no API key -- the same two JSON endpoints rotowire.com/daily/mlb/
optimizer.php itself calls in your browser to build that page's player
table, found via its network requests rather than documented anywhere.
Confirmed served to an anonymous (logged-out) browser session -- this
doesn't bypass a login wall or paywall, it reads the same public data a
free visitor's browser already loads. It IS still automated access to
a third party's site, something most sites' terms of service formally
restrict even for public pages -- worth stating plainly. This client
identifies itself honestly (the same DEFAULT_HEADERS User-Agent every
other client in this app already uses, no browser-spoofing) and is
meant to be triggered by an explicit user action (a "Refresh" button),
not polled on a loop. Like clients/draftkings.py's use of DK's own
undocumented lobby API, this is unsupported and could change without
notice -- everything here goes through the cache layer so a bad
response never takes the app down, and parsing is defensive.

Two calls cover everything a manual RotoWire projections CSV upload
used to:

  1. get_live_classic_slates() -- EVERY Classic DraftKings slate
     window RotoWire currently has live (All / Early / Afternoon /
     Turbo / Night / Late Night), skipping whichever windows don't
     exist today. There's no way to ask for a specific past date; this
     always reflects "right now". Each slate's own `startDateOnly` is
     the real date its projections belong to -- trusted over whatever
     date this app's UI happens to have selected, since RotoWire's
     slate is the actual source of the data.

     Confirmed live rather than assumed: there is no dedicated
     per-window endpoint. slate-list.php returns every window at once
     and they're told apart by a `slateName` field -- except the main
     slate, which is flagged by `defaultSlate` instead of named. Which
     windows exist genuinely varies by day (a real 2026-08-29 list had
     All/Turbo/Afternoon/Night/Late Night and no Early slate at all),
     so a missing window is the normal case, not an error, and
     pick_classic_slate() returns None for one rather than raising.
     Because all six come from ONE slate-list response, scraping every
     window costs exactly the same single fetch as scraping one.

     get_current_slate() remains as the narrow "just the main slate"
     shortcut the rest of the app already uses.
  2. get_slate_players(slate_id) -- every player in that slate's pool:
     salary, position eligibility, opponent, batting-order slot
     (projected or confirmed -- RotoWire tracks both, exposed here as
     the same LINEUP number semantics services/projections.py's CSV
     parser already produces, "" for bench, "SP" for a pitcher), FPTS
     projection, and rostership% -- returned in the exact row shape
     projections.parse_rotowire_csv() produces from a manual upload, so
     it plugs into projections.store() with no downstream changes
     needed anywhere (mlb_slate.py, atbat_sim.py, the optimizer, the
     contest generator all keep working unmodified).
"""

from __future__ import annotations

import logging
from typing import Any

from app.cache import cached
from app.clients.http import ApiError, get_json
from app.services.player_match import normalize_name

log = logging.getLogger(__name__)

BASE = "https://www.rotowire.com/daily/mlb/api"

# siteID=1 is DraftKings on RotoWire's own site/contest-type picker --
# this app only ever wants DK Classic.
DK_SITE_ID = 1

# Slate list (which slates exist today) rarely changes once posted;
# a slate's own players (salary/lineup/projections) can, especially
# close to lock -- so players is cached for less time, and the UI's own
# "Refresh" button (force=True) is meant to bypass either on demand.
_SLATE_LIST_TTL = 900  # 15 min
_PLAYERS_TTL = 300  # 5 min


# Every real RotoWire Classic DraftKings slate window, in the order
# they actually run. Confirmed live against slate-list.php rather than
# guessed: there's no dedicated per-window endpoint, just a `slateName`
# field over the same slate list, and "All" is identified by its own
# `defaultSlate` flag rather than by name.
#
# Which of these exist varies by DAY, and that's normal, not an error --
# a real 2026-08-29 list carried All/Turbo/Afternoon/Night/Late Night
# and no Early slate at all. Callers loop and skip what isn't there.
CLASSIC_SLATE_NAMES: tuple[str, ...] = (
    "All",
    "Early",
    "Afternoon",
    "Turbo",
    "Night",
    "Late Night",
)

# The main slate is flagged rather than named, so it needs picking by a
# different rule than every other window.
MAIN_SLATE_NAME = "All"


async def _slate_list(*, force: bool = False) -> dict[str, Any]:
    """The raw slate list. One shared cache key across every window --
    it's genuinely the same underlying response, just filtered
    differently, so scraping all six windows costs one fetch."""

    async def _load() -> Any:
        return await get_json(
            f"{BASE}/slate-list.php", params={"siteID": DK_SITE_ID}, source="RotoWire"
        )

    return await cached(f"rotowire:slate-list:{DK_SITE_ID}", _SLATE_LIST_TTL, _load, force=force)


def pick_classic_slate(payload: dict[str, Any], slate_name: str) -> dict[str, Any] | None:
    """
    One named Classic slate out of a slate-list payload, or None if
    RotoWire has no such slate live right now.

    Returns None rather than raising because a missing window is the
    normal case, not a failure -- most days don't have every window,
    and the caller's job is to skip it and carry on.
    """
    slates = payload.get("slates") or []
    if slate_name == MAIN_SLATE_NAME:
        candidates = [
            s for s in slates
            if s.get("contestType") == "Classic" and s.get("defaultSlate")
        ]
    else:
        candidates = [
            s for s in slates
            if s.get("contestType") == "Classic" and s.get("slateName") == slate_name
        ]
    candidates.sort(key=lambda s: s.get("startDateOnly") or "")
    return candidates[0] if candidates else None


def pick_live_classic_slates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Every Classic slate window that actually exists in this payload, in
    CLASSIC_SLATE_NAMES order, each tagged with the window name it
    matched. The pure, network-free half of get_live_classic_slates().
    """
    found: list[dict[str, Any]] = []
    seen_ids: set[Any] = set()
    for name in CLASSIC_SLATE_NAMES:
        slate = pick_classic_slate(payload, name)
        if not slate:
            continue
        # The default slate can also carry a real slateName; don't
        # return the same slate twice under two window labels.
        if slate.get("slateID") in seen_ids:
            continue
        seen_ids.add(slate.get("slateID"))
        found.append({**slate, "windowName": name})
    return found


async def get_live_classic_slates(*, force: bool = False) -> list[dict[str, Any]]:
    """
    Every Classic DraftKings slate window RotoWire has live right now.

    One slate-list fetch covers all of them, so looping every window is
    no more expensive than asking for one. Raises ApiError only if
    there's no Classic slate at all (off-season, or a real outage) --
    an individual missing window is normal and simply isn't returned.
    """
    payload = await _slate_list(force=force)
    slates = pick_live_classic_slates(payload)
    if not slates:
        raise ApiError("RotoWire has no Classic DraftKings slate live right now.", source="RotoWire")
    return slates


async def get_current_slate(*, force: bool = False) -> dict[str, Any]:
    """
    RotoWire's own main "All" Classic DraftKings slate -- the one its
    optimizer defaults to. Raises ApiError if RotoWire has no such
    slate live right now (off-season, or a real outage).
    """
    payload = await _slate_list(force=force)
    slate = pick_classic_slate(payload, MAIN_SLATE_NAME)
    if not slate:
        raise ApiError("RotoWire has no default Classic DraftKings slate live right now.", source="RotoWire")
    return slate


async def get_slate_players(slate_id: int, *, force: bool = False) -> list[dict[str, Any]]:
    """
    Every player in one slate's pool (from get_current_slate()'s
    slateID), returned in the exact row shape
    services/projections.parse_rotowire_csv() produces from a manual
    upload, so projections.store(day, rows) accepts this directly.
    """

    async def _load() -> Any:
        return await get_json(
            f"{BASE}/players.php", params={"slateID": slate_id}, source="RotoWire"
        )

    payload = await cached(f"rotowire:players:{slate_id}", _PLAYERS_TTL, _load, force=force)
    return _parse_players(payload)


def _parse_players(payload: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """The pure transformation half of get_slate_players() -- split out
    so it's directly testable against a fixture payload, with no
    network call."""
    rows: list[dict[str, Any]] = []
    for p in payload or []:
        name = f"{(p.get('firstName') or '').strip()} {(p.get('lastName') or '').strip()}".strip()
        salary = p.get("salary")
        if not name or not salary:
            continue

        slot = ((p.get("lineup") or {}).get("slot") or "")
        rows.append(
            {
                "name": name,
                "normalized_name": normalize_name(name),
                "team": ((p.get("team") or {}).get("abbr") or "").strip().upper(),
                "position": "/".join(p.get("pos") or []),
                "fpts": _f(p.get("pts")),
                "ownership_pct": _f(p.get("rostership")),
                "salary": int(salary),
                "lineup_spot": int(slot) if slot.isdigit() else None,
            }
        )
    return rows


def _f(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
