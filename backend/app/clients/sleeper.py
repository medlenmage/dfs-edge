"""
Sleeper fantasy client -- season-long leagues, rosters and live drafts.

Free and completely unauthenticated: Sleeper's read API needs no key,
no OAuth and no account linking, which is why the season-long side of
this app starts here rather than with Yahoo (see routers/season.py for
what Yahoo would require). Documented limit is 1,000 calls/minute; the
TTLs below sit far inside that even with a draft being polled.

Everything is read-only. This client cannot draft, trade, or change a
roster, and deliberately exposes no way to try.

Two ids matter and are easy to confuse:
  - `user_id`  -- a Sleeper account. Look it up from a username once
                  (get_user) and reuse it; usernames can change.
  - `league_id`/`draft_id` -- per season. A league's id changes every
                  year, so anything cached against one is season-scoped
                  automatically.
"""

from __future__ import annotations

import logging
from typing import Any

from app.cache import cached
from app.clients.http import get_json

log = logging.getLogger(__name__)

BASE = "https://api.sleeper.app/v1"

# Identity and league structure barely move during a season; rosters
# move on waivers; a live draft moves every few seconds. Hence three
# very different TTLs rather than one.
_USER_TTL = 86_400
_LEAGUE_TTL = 3_600
_ROSTER_TTL = 900
_STATE_TTL = 900
# The full player dictionary is ~14MB and Sleeper explicitly asks
# callers not to pull it more than once a day.
_PLAYERS_TTL = 86_400
# A draft in progress: short enough to feel live, long enough that a
# polling UI can't melt the rate limit.
_DRAFT_TTL = 5


async def _get(path: str, *, key: str, ttl: int, force: bool = False) -> Any:
    async def _load() -> Any:
        return await get_json(f"{BASE}{path}", source="Sleeper")

    return await cached(key, ttl, _load, force=force)


async def get_state(*, force: bool = False) -> dict[str, Any]:
    """The NFL's current season/week per Sleeper -- the authority for
    which season a league id should be looked up under."""
    return await _get("/state/nfl", key="sleeper:state:nfl", ttl=_STATE_TTL, force=force)


async def get_user(username_or_id: str, *, force: bool = False) -> dict[str, Any] | None:
    """Resolve a Sleeper username (or id) to its account record. Returns
    None for an unknown username rather than raising -- a typo in a
    username box is a normal event, not an error condition."""
    try:
        user = await _get(
            f"/user/{username_or_id}",
            key=f"sleeper:user:{username_or_id.lower()}",
            ttl=_USER_TTL,
            force=force,
        )
    except Exception:  # noqa: BLE001
        return None
    return user or None


async def get_leagues(user_id: str, season: str, *, force: bool = False) -> list[dict[str, Any]]:
    return await _get(
        f"/user/{user_id}/leagues/nfl/{season}",
        key=f"sleeper:leagues:{user_id}:{season}",
        ttl=_LEAGUE_TTL,
        force=force,
    ) or []


async def get_league(league_id: str, *, force: bool = False) -> dict[str, Any]:
    return await _get(
        f"/league/{league_id}", key=f"sleeper:league:{league_id}", ttl=_LEAGUE_TTL, force=force
    ) or {}


async def get_rosters(league_id: str, *, force: bool = False) -> list[dict[str, Any]]:
    return await _get(
        f"/league/{league_id}/rosters",
        key=f"sleeper:rosters:{league_id}",
        ttl=_ROSTER_TTL,
        force=force,
    ) or []


async def get_league_users(league_id: str, *, force: bool = False) -> list[dict[str, Any]]:
    """Every manager in the league -- needed to turn a roster's owner_id
    into a name a human recognises."""
    return await _get(
        f"/league/{league_id}/users",
        key=f"sleeper:leagueusers:{league_id}",
        ttl=_LEAGUE_TTL,
        force=force,
    ) or []


async def get_league_drafts(league_id: str, *, force: bool = False) -> list[dict[str, Any]]:
    return await _get(
        f"/league/{league_id}/drafts",
        key=f"sleeper:leaguedrafts:{league_id}",
        ttl=_LEAGUE_TTL,
        force=force,
    ) or []


async def get_user_drafts(user_id: str, season: str, *, force: bool = False) -> list[dict[str, Any]]:
    """Every draft this account is in for the season -- including mock
    drafts, which is what makes it possible to rehearse the assistant
    before the real thing."""
    return await _get(
        f"/user/{user_id}/drafts/nfl/{season}",
        key=f"sleeper:userdrafts:{user_id}:{season}",
        ttl=_LEAGUE_TTL,
        force=force,
    ) or []


async def get_draft(draft_id: str, *, force: bool = False) -> dict[str, Any]:
    """Draft settings and status: type (snake/linear/auction), rounds,
    slot-to-roster mapping, and whether it's pre_draft/drafting/complete."""
    return await _get(
        f"/draft/{draft_id}", key=f"sleeper:draft:{draft_id}", ttl=_DRAFT_TTL, force=force
    ) or {}


async def get_draft_picks(draft_id: str, *, force: bool = False) -> list[dict[str, Any]]:
    """
    Every pick made so far, in order. Sleeper has no websocket or push
    for drafts, so a live assistant polls this -- which is exactly what
    every commercial draft-sync tool does. The 5s TTL keeps a UI polling
    on a short timer from turning into real request volume.
    """
    return await _get(
        f"/draft/{draft_id}/picks",
        key=f"sleeper:draftpicks:{draft_id}",
        ttl=_DRAFT_TTL,
        force=force,
    ) or []


# The only fields of Sleeper's player record this app has any use for.
# The raw response carries dozens more (college, high school, rotowire
# ids, practice participation...) which would otherwise be JSON-encoded
# into the SQLite cache and re-parsed on every single read.
_PLAYER_FIELDS = (
    "player_id",
    "full_name",
    "first_name",
    "last_name",
    "position",
    "fantasy_positions",
    "team",
    "age",
    "years_exp",
    "injury_status",
    "depth_chart_order",
    "depth_chart_position",
    "status",
    "active",
    "search_rank",
    "number",
    # Sleeper's own normalized name, handy as a second key when matching
    # against this app's other player sources.
    "search_full_name",
)

# Sleeper carries no bye week on the player record (verified against the
# raw payload -- no field containing "bye" exists on it). Bye weeks come
# from the DraftKings best-ball board instead, which does publish them.


async def get_players(*, force: bool = False) -> dict[str, dict[str, Any]]:
    """
    Sleeper's player dictionary, keyed by their own player_id.

    The raw response is ~14MB across ~12,000 players (every practice-squad
    body and retired name Sleeper has ever carried), and Sleeper explicitly
    asks callers not to pull it more than once a day -- hence the 24h TTL.

    It is trimmed to _PLAYER_FIELDS BEFORE being cached. The cache is
    JSON-in-SQLite, so storing the raw blob would mean re-parsing 14MB on
    every read for the sake of a handful of fields.

    `search_rank` is Sleeper's own overall ranking (Bijan Robinson = 1);
    it stands in as a consensus-ADP proxy when nothing better is loaded.
    Note that ids to other systems (gsis_id, espn_id) are frequently null
    here, so joining to this app's other player data goes through
    `player_match.normalize_name`, not an id.
    """

    async def _load() -> Any:
        raw = await get_json(f"{BASE}/players/nfl", source="Sleeper")
        if not isinstance(raw, dict):
            return {}
        return {
            pid: {k: rec.get(k) for k in _PLAYER_FIELDS}
            for pid, rec in raw.items()
            if isinstance(rec, dict)
        }

    return await cached("sleeper:players:nfl", _PLAYERS_TTL, _load, force=force) or {}
