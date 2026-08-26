"""
Today's MLB umpire assignments + season rate stats, via RotoWire's own
public "Today's MLB Umpire Stats" page
(rotowire.com/baseball/umpire-stats-daily.php) -- the same free,
no-key, JSON-behind-the-page pattern as clients/rotowire.py, confirmed
live via the page's own network requests, not documented anywhere.

Real, live-confirmed 2026 context worth stating: MLB's 2026 rule change
is a CHALLENGE system (ABS), not full robo-umpiring -- the human home
plate umpire still calls every pitch; each team gets 2 challenges/game
(retained on success), so only the most egregious misses get overturned.
Umpire tendency is a damped-but-still-real DFS signal this season, not
an obsolete one.

One real, live-confirmed limitation: `umpFirstName`/`umpLastName` (and
the rate stats alongside them) are BLANK until RotoWire itself knows
the assignment -- confirmed empty for every game checked mid-morning on
a day with 1pm+ first pitches. Exact lead time wasn't pinned down
further; treat a missing team-pair as "not assigned yet," same as this
app's other late-arriving signals (odds, lineups), not an error.
"""

from __future__ import annotations

import logging
from typing import Any

from app.cache import cached
from app.clients.http import get_json

log = logging.getLogger(__name__)

URL = "https://www.rotowire.com/baseball/tables/umpire-stats-today.php"

_TTL = 900  # 15 min -- matches fantasylabs.py's own "live, same-day" convention


def _f(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _parse_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Keyed by f"{away_abbrev}@{home_abbrev}" -- RotoWire's own team
    codes here already match this app's existing DK/MLB Stats API
    abbreviations, no name-matching layer needed. A game whose umpire
    isn't posted yet is simply omitted -- see module docstring."""
    umpires: dict[str, dict[str, Any]] = {}
    for row in rows:
        first = (row.get("umpFirstName") or "").strip()
        last = (row.get("umpLastName") or "").strip()
        if not first and not last:
            continue
        away, home = row.get("visitTeam"), row.get("homeTeam")
        if not away or not home:
            continue
        umpires[f"{away}@{home}"] = {
            "name": f"{first} {last}".strip(),
            "rpg": _f(row.get("umpRPG")),
            "kpg": _f(row.get("umpKPG")),
            "games": _f(row.get("games")),
        }
    return umpires


async def get_todays_umpires(*, force: bool = False) -> dict[str, dict[str, Any]]:
    """
    Every game today with a posted home-plate umpire assignment --
    real name, plus that umpire's own season runs/game and
    strikeouts/game (RotoWire's own rate stats, already computed).
    Games without a posted assignment yet just don't appear in the
    result (see module docstring).
    """
    async def _load() -> Any:
        return await get_json(URL, source="RotoWire (umpires)")

    try:
        rows = await cached("rotowire:umpires:today", _TTL, _load, force=force)
    except Exception as exc:
        log.warning("RotoWire umpire fetch failed: %s", exc)
        return {}

    return _parse_rows(rows or [])
