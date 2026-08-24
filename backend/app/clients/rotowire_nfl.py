"""
Client for RotoWire's own NFL DFS lineup optimizer -- the NFL sibling
of clients/rotowire.py (MLB). Same free, no-API-key, "read the same
public JSON the browser already loads" approach, and the same real
caveats: unsupported, could change without notice, everything goes
through the cache layer so a bad response never takes the app down,
and this is meant to be triggered by an explicit user action, not
polled on a loop.

NFL's players.php response has a genuinely different shape from MLB's
(confirmed live, not assumed) -- no `lineup` field at all (NFL has no
batting order), a `pos` list that already includes the "FLEX"
eligibility DK itself assigns (e.g. ["RB", "FLEX"]), and team-defense
rows carry firstName/lastName as the city/nickname pair (e.g.
"Jacksonville"/"Jaguars") instead of a real player's name. Kept as its
own module rather than folded into clients/rotowire.py, since the two
payloads don't actually line up field-for-field.

Deliberately does NOT derive DK salaries from this pull the way MLB's
services/salaries.from_rotowire_rows() does for MLB -- RotoWire's own
export has no DK numeric player id, and NFL's optimizer output
genuinely needs one (see that function's own docstring: "this function
is MLB-only for exactly that reason" -- NFL has no separate roster/ID
fetch, so DK's own id is the only stable player key it has). A real DK
salary CSV is still needed separately for NFL; this only fills in
FPTS/ownership.

  1. get_current_slate() -- RotoWire's main Classic DraftKings NFL
     slate. Unlike MLB, RotoWire hadn't flagged any Classic slate
     `defaultSlate: true` yet as of building this (confirmed live,
     August 2026, ahead of the season's real September opener) --
     falls back to the slate named "All" when nothing is flagged
     default, since that's the same name MLB's real default slate
     always carries once one exists.
  2. get_slate_players(slate_id) -- every player in that slate's pool:
     salary, position eligibility (including DK's own FLEX tag),
     opponent, FPTS projection, and rostership% -- returned in the
     same row shape services/projections.parse_rotowire_csv() produces
     from a manual upload (minus `lineup_spot`, which NFL has no
     equivalent for), so it plugs into projections.store() with no
     downstream changes needed.
"""

from __future__ import annotations

from typing import Any

from app.cache import cached
from app.clients.http import ApiError, get_json
from app.services.player_match import normalize_name

BASE = "https://www.rotowire.com/daily/nfl/api"

# siteID=1 is DraftKings on RotoWire's own site/contest-type picker --
# this app only ever wants DK Classic.
DK_SITE_ID = 1

_SLATE_LIST_TTL = 900  # 15 min -- which slates exist rarely changes
_PLAYERS_TTL = 300  # 5 min -- salary/projections can, especially near lock


async def get_current_slate(*, force: bool = False) -> dict[str, Any]:
    """
    RotoWire's own main Classic DraftKings NFL slate. Raises ApiError
    if RotoWire has no such slate live right now (off-season with
    nothing posted at all, or a real outage).
    """

    async def _load() -> Any:
        return await get_json(
            f"{BASE}/slate-list.php", params={"siteID": DK_SITE_ID}, source="RotoWire"
        )

    payload = await cached(f"rotowire_nfl:slate-list:{DK_SITE_ID}", _SLATE_LIST_TTL, _load, force=force)
    return _pick_classic_slate(payload)


def _pick_classic_slate(payload: dict[str, Any]) -> dict[str, Any]:
    """The pure transformation half of get_current_slate() -- split out
    so it's directly testable against a fixture payload, with no
    network call."""
    slates = payload.get("slates") or []
    candidates = [s for s in slates if s.get("contestType") == "Classic" and s.get("defaultSlate")]
    if not candidates:
        # No slate flagged default yet -- a real, observed state in the
        # preseason/offseason window, not a hypothetical. Fall back to
        # the slate RotoWire names "All", the same name their real
        # default slate always carries once the season is under way.
        candidates = [
            s for s in slates if s.get("contestType") == "Classic" and s.get("slateName") == "All"
        ]
    candidates.sort(key=lambda s: s.get("startDateOnly") or "")
    if not candidates:
        raise ApiError("RotoWire has no main Classic DraftKings NFL slate live right now.", source="RotoWire")
    return candidates[0]


async def get_slate_players(slate_id: int, *, force: bool = False) -> list[dict[str, Any]]:
    """
    Every player in one slate's pool (from get_current_slate()'s
    slateID), returned in the same row shape services/
    projections.parse_rotowire_csv() produces from a manual upload
    (`lineup_spot` always None -- NFL has no batting-order equivalent),
    so projections.store(week_key, rows) accepts this directly.
    """

    async def _load() -> Any:
        return await get_json(
            f"{BASE}/players.php", params={"slateID": slate_id}, source="RotoWire"
        )

    payload = await cached(f"rotowire_nfl:players:{slate_id}", _PLAYERS_TTL, _load, force=force)
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

        rows.append(
            {
                "name": name,
                "normalized_name": normalize_name(name),
                "team": ((p.get("team") or {}).get("abbr") or "").strip().upper(),
                "position": "/".join(p.get("pos") or []),
                "fpts": _f(p.get("pts")),
                "ownership_pct": _f(p.get("rostership")),
                "salary": int(salary),
                "lineup_spot": None,
            }
        )
    return rows


def _f(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
