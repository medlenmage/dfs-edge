"""
Assembles a weekly NFL slate: games, Vegas-implied context, weather for
outdoor games, and -- once uploaded -- each team's salary-CSV player
pool decorated with a matchup score and their RotoWire projection.

Unlike mlb_slate.py, there's no separately-fetched roster here. A DK
salary CSV already defines who's in the pool each week (name, team,
position, salary) the same way it always has for the optimizer; what
this adds is matchup context (implied team total, game script, weather)
attached to each of that team's rostered players.

NFL slates are weekly, not daily, so salaries.py/projections.py --
both already just generic date-string-keyed stores under the hood --
get keyed by a week label ("nfl-2026-wk1") instead of an ISO date. That
reuses the exact same upload/cache machinery MLB already has rather
than building a parallel NFL-specific version of it.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.clients import nfl, weather as weather_client
from app.data import nfl_stadiums
from app.services import nfl_scoring, player_match, projections, salaries

__all__ = ["build_slate", "week_key"]


def week_key(season: int, week: int) -> str:
    """The cache/upload key a given week's salary + projections CSVs are stored under."""
    return f"nfl-{season}-wk{week}"


def _game_time_utc(game: dict[str, Any]) -> str | None:
    gameday = game.get("gameday")
    gametime = game.get("gametime")
    if not gameday or not gametime:
        return None
    stadium = nfl_stadiums.get_stadium(game.get("home_team") or "")
    tz_name = stadium.get("tz")
    if not tz_name:
        return None
    try:
        local = datetime.strptime(f"{gameday} {gametime}", "%Y-%m-%d %H:%M")
        local = local.replace(tzinfo=ZoneInfo(tz_name))
        return local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, KeyError):
        return None


def _team_players(
    team: str,
    salary_rows: list[dict[str, Any]],
    projection_lookup: dict[tuple[str, str], dict[str, Any]],
    *,
    implied_total: float | None,
    is_home: bool,
    spread: float | None,
    favored: bool | None,
    wind_mph: float | None,
    precip_chance_pct: float | None,
) -> list[dict[str, Any]]:
    team_norm = player_match.normalize_team(team)
    players = []
    for row in salary_rows:
        if player_match.normalize_team(row["team"]) != team_norm:
            continue
        proj = player_match.match(projection_lookup, row["name"], row["team"])
        edge = (
            nfl_scoring.score_player(
                row["position"],
                implied_total=implied_total,
                is_home=is_home,
                spread=spread,
                favored=bool(favored),
                wind_mph=wind_mph,
                precip_chance_pct=precip_chance_pct,
            )
            if favored is not None
            else None
        )
        players.append(
            {
                "dk_id": row.get("dk_id") or None,
                "name": row["name"],
                "team": team,
                "position": row["position"],
                "salary": row["salary"],
                "avg_points": row.get("avg_points"),
                "value": salaries.value_score(edge["score"] if edge else None, row["salary"]),
                "edge": edge,
                "projection": {"fpts": proj["fpts"], "ownership_pct": proj["ownership_pct"]} if proj else None,
            }
        )
    players.sort(key=lambda p: (p["edge"]["score"] if p["edge"] else -1), reverse=True)
    return players


async def _build_game(
    game: dict[str, Any],
    salary_rows: list[dict[str, Any]],
    projection_lookup: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    home, away = game["home_team"], game["away_team"]
    implied = nfl.implied_team_totals(game)
    spread = abs(game["spread_line"]) if game.get("spread_line") is not None else None
    home_favored = (
        game["home_moneyline"] < 0 if game.get("home_moneyline") is not None else None
    )

    wx = None
    roof = (game.get("roof") or "").lower()
    if roof == "outdoors":
        stadium = nfl_stadiums.get_stadium(home)
        game_time_utc = _game_time_utc(game)
        if stadium.get("lat") and game_time_utc:
            wx = await weather_client.get_game_weather(stadium["lat"], stadium["lon"], game_time_utc)

    wind_mph = (wx or {}).get("wind_mph")
    precip_pct = (wx or {}).get("precip_chance_pct")

    home_players = _team_players(
        home, salary_rows, projection_lookup,
        implied_total=implied["home"], is_home=True, spread=spread,
        favored=home_favored, wind_mph=wind_mph, precip_chance_pct=precip_pct,
    )
    away_favored = None if home_favored is None else not home_favored
    away_players = _team_players(
        away, salary_rows, projection_lookup,
        implied_total=implied["away"], is_home=False, spread=spread,
        favored=away_favored, wind_mph=wind_mph, precip_chance_pct=precip_pct,
    )

    return {
        "game_id": game["game_id"],
        "gameday": game["gameday"],
        "gametime": game["gametime"],
        "weekday": game.get("weekday"),
        "stadium": game.get("stadium"),
        "roof": game.get("roof"),
        "surface": game.get("surface"),
        "div_game": game.get("div_game"),
        "betting": {
            "spread_line": game.get("spread_line"),
            "total_line": game.get("total_line"),
            "home_moneyline": game.get("home_moneyline"),
            "away_moneyline": game.get("away_moneyline"),
        },
        "weather": wx or ({"note": "forecast unavailable"} if roof == "outdoors" else {"note": "roof closed"}),
        "home": {"abbrev": home, "implied_total": implied["home"], "favored": home_favored, "players": home_players},
        "away": {"abbrev": away, "implied_total": implied["away"], "favored": away_favored, "players": away_players},
    }


async def build_slate(season: int, week: int) -> dict[str, Any]:
    games = await nfl.get_schedule(season, week=week)
    if not games:
        return {
            "season": season,
            "week": week,
            "games": [],
            "message": f"No NFL games found for week {week}, {season}.",
        }

    key = week_key(season, week)
    salary_rows = salaries.load(key)
    projection_rows = projections.load(key)
    projection_lookup = projections.build_lookup(projection_rows)

    built = await asyncio.gather(
        *[_build_game(g, salary_rows, projection_lookup) for g in games]
    )

    return {
        "season": season,
        "week": week,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "games": list(built),
    }
