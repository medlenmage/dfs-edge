"""
Client for the official MLB Stats API (statsapi.mlb.com).

It is free, needs no API key, and is the same feed that powers MLB.com.
Be polite with it: everything here goes through the cache layer.

The one trick worth knowing: instead of asking for one player's splits at
a time (which would be 300+ requests for a full slate), we pull the
ENTIRE league's splits in a single request and index them by player id.
Four requests then cover vs-LHP, vs-RHP, home and away for everybody.
"""

from __future__ import annotations

import logging
from datetime import date as date_cls
from typing import Any

from app.cache import cached
from app.clients.http import get_json
from app.config import get_settings

log = logging.getLogger(__name__)

BASE = "https://statsapi.mlb.com/api/v1"
SPORT_ID = 1  # 1 = MLB


# --------------------------------------------------------------------------
# Schedule / games
# --------------------------------------------------------------------------

async def get_schedule(day: str, *, force: bool = False) -> list[dict[str, Any]]:
    """
    Every MLB game on `day` (YYYY-MM-DD), with probable pitchers attached.
    """
    settings = get_settings()

    async def _load() -> Any:
        return await get_json(
            f"{BASE}/schedule",
            params={
                "sportId": SPORT_ID,
                "date": day,
                "hydrate": "probablePitcher,team,venue,linescore,weather",
            },
            source="MLB Stats API",
        )

    payload = await cached(
        f"mlb:schedule:{day}", settings.ttl_schedule, _load, force=force
    )
    dates = payload.get("dates") or []
    if not dates:
        return []
    return dates[0].get("games") or []


async def get_lineups(game_pk: int, *, force: bool = False) -> dict[str, Any]:
    """
    Confirmed batting order for a game, if it has been posted yet.

    Teams usually post lineups 2-4 hours before first pitch. Before that
    this returns empty lists -- which is itself useful information, so
    the UI can show "lineup not out yet" instead of stale guesses.
    """
    settings = get_settings()

    async def _load() -> Any:
        return await get_json(
            f"{BASE}/schedule",
            params={"sportId": SPORT_ID, "gamePk": game_pk, "hydrate": "lineups"},
            source="MLB Stats API",
        )

    payload = await cached(
        f"mlb:lineups:{game_pk}", 300, _load, force=force
    )
    for d in payload.get("dates") or []:
        for game in d.get("games") or []:
            if game.get("gamePk") == game_pk:
                lineups = game.get("lineups") or {}
                return {
                    "home": [p.get("id") for p in lineups.get("homePlayers") or []],
                    "away": [p.get("id") for p in lineups.get("awayPlayers") or []],
                }
    return {"home": [], "away": []}


# --------------------------------------------------------------------------
# People (handedness, position, name)
# --------------------------------------------------------------------------

async def get_people(person_ids: list[int]) -> dict[int, dict[str, Any]]:
    """
    Bio info for a batch of players, keyed by player id.

    We care about batSide / pitchHand -- those drive every platoon edge.
    """
    if not person_ids:
        return {}

    settings = get_settings()
    unique = sorted(set(int(pid) for pid in person_ids if pid))
    out: dict[int, dict[str, Any]] = {}

    # The API caps URL length, so chunk the id list.
    CHUNK = 100
    for i in range(0, len(unique), CHUNK):
        chunk = unique[i : i + CHUNK]
        key = f"mlb:people:{hash(tuple(chunk)) & 0xFFFFFFFF}"

        async def _load(ids: list[int] = chunk) -> Any:
            return await get_json(
                f"{BASE}/people",
                params={"personIds": ",".join(str(x) for x in ids)},
                source="MLB Stats API",
            )

        payload = await cached(key, settings.ttl_stats, _load)
        for person in payload.get("people") or []:
            pid = person.get("id")
            if pid is None:
                continue
            out[pid] = {
                "id": pid,
                "name": person.get("fullName"),
                "bats": (person.get("batSide") or {}).get("code"),
                "throws": (person.get("pitchHand") or {}).get("code"),
                "position": (person.get("primaryPosition") or {}).get("abbreviation"),
                "team_id": (person.get("currentTeam") or {}).get("id"),
            }
    return out


async def get_active_roster(team_id: int, season: int) -> list[int]:
    """Player ids on a team's active roster."""
    settings = get_settings()

    async def _load() -> Any:
        return await get_json(
            f"{BASE}/teams/{team_id}/roster",
            params={"rosterType": "active", "season": season},
            source="MLB Stats API",
        )

    payload = await cached(
        f"mlb:roster:{team_id}:{season}", settings.ttl_stats, _load
    )
    return [
        (entry.get("person") or {}).get("id")
        for entry in payload.get("roster") or []
        if (entry.get("person") or {}).get("id")
    ]



# Roster status codes on the injured list (the Stats API prefixes every IL
# tier with "D" -- D7, D10, D15, D60). Everything else that isn't "A"ctive
# is a roster move (optioned to the minors, restricted list, suspended,
# bereavement/paternity), not an injury, so it's excluded.
_IL_STATUS_PREFIX = "D"


async def get_team_injuries(team_id: int, season: int) -> list[dict[str, Any]]:
    """
    Players on this team's 40-man roster currently on the injured list.

    This is just a roster-status flag (which IL tier) -- no body part or
    expected return date, since the Stats API doesn't expose that. Good
    enough to warn "this guy might not play."
    """
    settings = get_settings()

    async def _load() -> Any:
        return await get_json(
            f"{BASE}/teams/{team_id}/roster",
            params={"rosterType": "40Man", "season": season},
            source="MLB Stats API",
        )

    payload = await cached(
        f"mlb:injuries:{team_id}:{season}", settings.ttl_injuries, _load
    )
    out: list[dict[str, Any]] = []
    for entry in payload.get("roster") or []:
        status = entry.get("status") or {}
        code = status.get("code") or ""
        if not code.startswith(_IL_STATUS_PREFIX):
            continue
        person = entry.get("person") or {}
        if not person.get("id"):
            continue
        out.append(
            {
                "id": person.get("id"),
                "name": person.get("fullName"),
                "position": (entry.get("position") or {}).get("abbreviation"),
                "status_code": code,
                "status_description": status.get("description"),
            }
        )
    return out


# --------------------------------------------------------------------------
# League-wide splits (the efficient bit)
# --------------------------------------------------------------------------

# sitCodes the MLB API understands:
#   vl = vs Left-handed pitcher/batter    vr = vs Right-handed
#   h  = home                              a  = away
SIT_CODES = {"vl": "vs LHP", "vr": "vs RHP", "h": "home", "a": "away"}


async def get_league_splits(
    season: int,
    sit_code: str,
    group: str = "hitting",
) -> dict[int, dict[str, Any]]:
    """
    Every qualified player's stat line for one situational split,
    keyed by player id.

    group is "hitting" or "pitching".
    """
    settings = get_settings()

    async def _load() -> Any:
        return await get_json(
            f"{BASE}/stats",
            params={
                "stats": "statSplits",
                "sitCodes": sit_code,
                "group": group,
                "season": season,
                "sportId": SPORT_ID,
                "playerPool": "All",
                "limit": 2000,
                "gameType": "R",
            },
            source="MLB Stats API",
        )

    payload = await cached(
        f"mlb:splits:{season}:{group}:{sit_code}", settings.ttl_stats, _load
    )

    out: dict[int, dict[str, Any]] = {}
    for block in payload.get("stats") or []:
        for split in block.get("splits") or []:
            player = split.get("player") or {}
            pid = player.get("id")
            if pid is None:
                continue
            out[pid] = _normalise_stat(split.get("stat") or {}, group)
    return out


async def get_league_season(
    season: int, group: str = "hitting"
) -> dict[int, dict[str, Any]]:
    """Every player's overall season line, keyed by player id."""
    settings = get_settings()

    async def _load() -> Any:
        return await get_json(
            f"{BASE}/stats",
            params={
                "stats": "season",
                "group": group,
                "season": season,
                "sportId": SPORT_ID,
                "playerPool": "All",
                "limit": 2000,
                "gameType": "R",
            },
            source="MLB Stats API",
        )

    payload = await cached(
        f"mlb:season:{season}:{group}", settings.ttl_stats, _load
    )

    out: dict[int, dict[str, Any]] = {}
    for block in payload.get("stats") or []:
        for split in block.get("splits") or []:
            player = split.get("player") or {}
            pid = player.get("id")
            if pid is None:
                continue
            out[pid] = _normalise_stat(split.get("stat") or {}, group)
    return out


async def get_recent_form(
    season: int, games: int = 15, group: str = "hitting"
) -> dict[int, dict[str, Any]]:
    """Rolling last-N-games line for every player -- the 'is he hot?' view."""
    settings = get_settings()

    async def _load() -> Any:
        return await get_json(
            f"{BASE}/stats",
            params={
                "stats": "lastXGames",
                "group": group,
                "season": season,
                "sportId": SPORT_ID,
                "playerPool": "All",
                "limit": 2000,
                "gameType": "R",
                "numberOfGames": games,
            },
            source="MLB Stats API",
        )

    payload = await cached(
        f"mlb:lastx:{season}:{group}:{games}", 3600, _load
    )

    out: dict[int, dict[str, Any]] = {}
    for block in payload.get("stats") or []:
        for split in block.get("splits") or []:
            player = split.get("player") or {}
            pid = player.get("id")
            if pid is None:
                continue
            out[pid] = _normalise_stat(split.get("stat") or {}, group)
    return out


# --------------------------------------------------------------------------
# Bullpen quality -- relievers only, aggregated by team
# --------------------------------------------------------------------------

# Minimum bullpen innings logged before a team's aggregate is trusted.
MIN_BULLPEN_IP = 20


async def get_bullpen_stats(season: int) -> dict[int, dict[str, Any]]:
    """
    Each team's bullpen ERA/WHIP/K-9, keyed by team id -- built from every
    pitcher with zero starts this season, not the team's blended staff
    line (which the /teams/{id}/stats endpoint would give you, starters
    and all).

    A starter goes five or six innings; the bullpen covers the rest, and
    a shaky one is a real edge for a hitter facing that team late,
    independent of how good the starter himself is.
    """
    settings = get_settings()

    async def _load() -> Any:
        return await get_json(
            f"{BASE}/stats",
            params={
                "stats": "season",
                "group": "pitching",
                "season": season,
                "sportId": SPORT_ID,
                "playerPool": "All",
                "limit": 2000,
                "gameType": "R",
            },
            source="MLB Stats API",
        )

    payload = await cached(f"mlb:bullpen:{season}", settings.ttl_stats, _load)

    totals: dict[int, dict[str, int]] = {}
    for block in payload.get("stats") or []:
        for split in block.get("splits") or []:
            stat = split.get("stat") or {}
            if _i(stat.get("gamesStarted")) > 0:
                continue  # anyone who has started a game isn't a reliever
            team_id = (split.get("team") or {}).get("id")
            if not team_id:
                continue
            t = totals.setdefault(
                team_id, {"outs": 0, "er": 0, "bb": 0, "k": 0, "hits": 0}
            )
            t["outs"] += _i(stat.get("outs"))
            t["er"] += _i(stat.get("earnedRuns"))
            t["bb"] += _i(stat.get("baseOnBalls"))
            t["k"] += _i(stat.get("strikeOuts"))
            t["hits"] += _i(stat.get("hits"))

    out: dict[int, dict[str, Any]] = {}
    for team_id, t in totals.items():
        if t["outs"] < MIN_BULLPEN_IP * 3:
            continue
        ip = t["outs"] / 3
        out[team_id] = {
            "era": round(t["er"] * 27 / t["outs"], 2),
            "whip": round((t["hits"] + t["bb"]) / ip, 3),
            "k_per_9": round(t["k"] * 27 / t["outs"], 2),
            "ip": round(ip, 1),
        }
    return out


# --------------------------------------------------------------------------
# Stat normalisation
# --------------------------------------------------------------------------

def _f(value: Any) -> float | None:
    """MLB returns rate stats as strings like '.284' or '-.--'."""
    if value in (None, "", "-", ".---", "-.--", "*.**", "INF"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _i(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _normalise_stat(stat: dict[str, Any], group: str) -> dict[str, Any]:
    """
    Turn MLB's raw stat blob into a small, consistent dict with the
    derived rates DFS players actually look at.
    """
    if group == "pitching":
        ip = _f(stat.get("inningsPitched")) or 0.0
        bf = _i(stat.get("battersFaced"))
        k = _i(stat.get("strikeOuts"))
        bb = _i(stat.get("baseOnBalls"))
        hr = _i(stat.get("homeRuns"))
        return {
            "ip": ip,
            "bf": bf,
            "era": _f(stat.get("era")),
            "whip": _f(stat.get("whip")),
            "avg_against": _f(stat.get("avg")),
            "obp_against": _f(stat.get("obp")),
            "slg_against": _f(stat.get("slg")),
            "ops_against": _f(stat.get("ops")),
            "k": k,
            "bb": bb,
            "hr": hr,
            "k_pct": round(k / bf, 4) if bf else None,
            "bb_pct": round(bb / bf, 4) if bf else None,
            "hr_per_9": round(hr * 9 / ip, 3) if ip else None,
            "k_per_9": round(k * 9 / ip, 3) if ip else None,
        }

    # hitting
    pa = _i(stat.get("plateAppearances"))
    ab = _i(stat.get("atBats"))
    avg = _f(stat.get("avg"))
    slg = _f(stat.get("slg"))
    k = _i(stat.get("strikeOuts"))
    bb = _i(stat.get("baseOnBalls"))
    hr = _i(stat.get("homeRuns"))
    sb = _i(stat.get("stolenBases"))
    return {
        "pa": pa,
        "ab": ab,
        "avg": avg,
        "obp": _f(stat.get("obp")),
        "slg": slg,
        "ops": _f(stat.get("ops")),
        "hr": hr,
        "rbi": _i(stat.get("rbi")),
        "runs": _i(stat.get("runs")),
        "sb": sb,
        "hits": _i(stat.get("hits")),
        "doubles": _i(stat.get("doubles")),
        "triples": _i(stat.get("triples")),
        # Derived -- ISO is raw power, stripped of singles.
        "iso": round(slg - avg, 3) if (slg is not None and avg is not None) else None,
        "k_pct": round(k / pa, 4) if pa else None,
        "bb_pct": round(bb / pa, 4) if pa else None,
        "hr_per_pa": round(hr / pa, 4) if pa else None,
        "sb_per_pa": round(sb / pa, 4) if pa and sb is not None else None,
    }
