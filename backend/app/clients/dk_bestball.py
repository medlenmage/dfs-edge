"""
DraftKings Best Ball board -- the real draft pool, free, no key.

Reached through exactly the same two public endpoints clients/
draftkings.py already uses for DFS salaries, just filtered to
GameTypeId 145 ("Best Ball -- Draft a 20-player team in a slow or fast
Snake Draft"). No new access, no scraping of a rendered page.

WHAT THE BOARD ACTUALLY GIVES, verified against a live 2026 draft group
rather than assumed:

  - the full drafted player pool (~1,566 unique players)
  - DK's OWN season projection, as draftStatAttributes id 90 --
    confirmed by reading it across the board: Bijan Robinson 23.2,
    Christian McCaffrey 24.8, and a clean monotonic decline to 5.7 by
    the 300th player. It is fantasy points per game, the same quantity
    DK's own draft UI shows.
  - bye week (draftStatAttributes id -2, and again on playerAttributes)
  - the board ORDER, which DK sorts by its own ranking

WHAT IT DOES NOT GIVE: a real measured ADP. DK publishes no
average-draft-position field anywhere in this payload. The board order
is DK's own ranking of the pool, which correlates with ADP and is what
a drafter actually sees, but calling it "ADP" would be inventing a
measurement. It is surfaced as `board_rank` and labelled that way
everywhere, and a real ADP -- if one is ever loaded from drafts this
app can see -- belongs in a separate field beside it.
"""

from __future__ import annotations

import logging
from typing import Any

from app.cache import cached
from app.clients.http import get_json

log = logging.getLogger(__name__)

LOBBY_URL = "https://www.draftkings.com/lobby/getcontests?sport=NFL"
DRAFTABLES_URL = "https://api.draftkings.com/draftgroups/v1/draftgroups/{}/draftables"

# DK's own GameTypeId for Best Ball.
BEST_BALL_GAME_TYPE_ID = 145

# DK's projected fantasy points per game.
_PROJECTION_ATTRIBUTE_ID = 90
# DK's bye-week attribute (negative ids are DK's "informational" ones).
_BYE_ATTRIBUTE_ID = -2

_GROUPS_TTL = 3_600
_BOARD_TTL = 3_600


async def get_best_ball_groups(*, force: bool = False) -> list[dict[str, Any]]:
    """Every live Best Ball draft group DK is currently running."""

    async def _load() -> Any:
        return await get_json(LOBBY_URL, source="DraftKings")

    payload = await cached("dk:bestball:groups", _GROUPS_TTL, _load, force=force) or {}
    out = []
    for g in payload.get("DraftGroups") or []:
        if g.get("GameTypeId") != BEST_BALL_GAME_TYPE_ID:
            continue
        out.append(
            {
                "draft_group_id": g.get("DraftGroupId"),
                "label": (g.get("ContestStartTimeSuffix") or "").strip("() ") or "Best Ball",
                "starts_at": g.get("StartDateEst"),
                "games": g.get("GameCount"),
            }
        )
    return out


def _stat(player: dict[str, Any], attribute_id: int) -> str | None:
    for s in player.get("draftStatAttributes") or []:
        if s.get("id") == attribute_id:
            return s.get("value")
    return None


def _clean(value: Any) -> str | None:
    """DK writes the string "None" where it means null."""
    if value is None or str(value).strip() in ("", "None"):
        return None
    return str(value)


def _to_float(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f


async def get_board(draft_group_id: int, *, force: bool = False) -> list[dict[str, Any]]:
    """
    The Best Ball draft board for one group, one row per unique player
    in DK's own board order.

    DK returns one draftable row per player PER WEEK (~4,500 rows for
    ~1,566 players), so this dedupes on the player's DK id, keeping the
    first occurrence -- which preserves DK's ordering. `board_rank` is
    that position, 1-indexed. See the module docstring for why it is
    not called ADP.
    """

    async def _load() -> Any:
        return await get_json(DRAFTABLES_URL.format(draft_group_id), source="DraftKings")

    payload = await cached(
        f"dk:bestball:board:{draft_group_id}", _BOARD_TTL, _load, force=force
    ) or {}

    out: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for p in payload.get("draftables") or []:
        pid = p.get("playerDkId") or p.get("playerId")
        if pid is None or pid in seen:
            continue
        seen.add(pid)
        out.append(
            {
                "dk_id": pid,
                "name": p.get("displayName"),
                "position": p.get("position"),
                "team": p.get("teamAbbreviation"),
                "board_rank": len(out) + 1,
                "dk_projection": _to_float(_stat(p, _PROJECTION_ATTRIBUTE_ID)),
                "bye_week": _bye_week(p),
                # DK sends the literal STRING "None" here, not a null, so
                # a plain passthrough would make every healthy player look
                # like he carries a status.
                "status": _clean(p.get("status")),
                "news_status": _clean(p.get("newsStatus")),
            }
        )
    return out


def _bye_week(player: dict[str, Any]) -> int | None:
    """
    DK publishes the bye twice: as a named playerAttribute ("ByeWeek" ->
    "11") and as draft stat -2, which is formatted for display as an
    ordinal ("11th"). The named attribute is read first because it needs
    no string surgery; the stat is the fallback. Both are absent for the
    deep end of the board (~600 of 1,568 players -- practice-squad bodies
    with no real role), which is reported as None rather than guessed.
    """
    for attr in player.get("playerAttributes") or []:
        if attr.get("name") == "ByeWeek":
            digits = "".join(c for c in str(attr.get("value") or "") if c.isdigit())
            if digits:
                return int(digits)
    digits = "".join(c for c in str(_stat(player, _BYE_ATTRIBUTE_ID) or "") if c.isdigit())
    return int(digits) if digits else None
