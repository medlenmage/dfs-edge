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

from app.clients import fantasylabs, nfl, weather as weather_client
from app.data import nfl_stadiums
from app.services import (
    nfl_inhouse_projections,
    nfl_scoring,
    player_match,
    projections,
    salaries,
)

__all__ = ["build_slate", "week_key"]


def week_key(season: int, week: int) -> str:
    """The cache/upload key a given week's salary + projections CSVs are stored under."""
    return f"nfl-{season}-wk{week}"


def _match_fantasylabs(
    rows: list[dict[str, Any]], home_abbrev: str, away_abbrev: str
) -> dict[str, Any] | None:
    """A direct abbreviation lookup -- confirmed live that FantasyLabs'
    own HomeTeamShort/VisitorTeamShort already match this app's
    nflverse team codes exactly, so (unlike mlb_slate._match_odds())
    this never needs fuzzy full-name matching."""
    for row in rows:
        if row.get("home_short") == home_abbrev and row.get("away_short") == away_abbrev:
            return row
    return None


def _fantasylabs_has_line(vegas: dict[str, Any] | None) -> bool:
    """True only when a matched FantasyLabs row actually has at least
    one real current value -- a real, live-confirmed distinction from
    "a row matched at all": far enough ahead of a real game, FantasyLabs
    returns a real, correctly-matched row where every field is still
    null (lines haven't posted yet). A matched-but-empty row must never
    be reported as the real source of a game's line."""
    if not vegas:
        return False
    return any(
        vegas.get(key) is not None
        for key in ("home_spread_current", "total_current", "home_moneyline_current", "away_moneyline_current")
    )


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
    opp: str,
    salary_rows: list[dict[str, Any]],
    projection_lookup: dict[tuple[str, str], dict[str, Any]],
    *,
    implied_total: float | None,
    is_home: bool,
    spread: float | None,
    favored: bool | None,
    wind_mph: float | None,
    precip_chance_pct: float | None,
    prior_season: dict[str, Any],
    id_lookup: dict[str, Any],
) -> list[dict[str, Any]]:
    team_norm = player_match.normalize_team(team)
    defense_vs_position = prior_season.get("defense_vs_position") or {}
    league_avg_defense = prior_season.get("league_avg_defense_vs_position") or {}
    pace = prior_season.get("pace") or {}
    league_avg_pace = prior_season.get("league_avg_pace")

    players = []
    for row in salary_rows:
        if player_match.normalize_team(row["team"]) != team_norm:
            continue
        position = row["position"]
        proj = player_match.match(projection_lookup, row["name"], row["team"])
        if proj is None and position == "DST":
            # A DST is identified by its TEAM, not by a name that the
            # two sources spell differently: DraftKings lists "Panthers"
            # where RotoWire lists "Carolina Panthers", so every single
            # defence failed to match and came through with no
            # projection. DST is a required roster slot, so that alone
            # made it impossible to build any NFL lineup at all from
            # live data -- the optimizer reported "no legal lineup"
            # with a full pool of 429 skill players and zero defences.
            proj = next(
                (
                    r
                    for (row_team, _), r in projection_lookup.items()
                    if row_team == team_norm
                    and (r.get("position") or "").upper().startswith("DST")
                ),
                None,
            )
        # DST has no defense-vs-DST data to lean on; its own useful pace
        # signal is the *opponent's* plays run, not this team's own.
        if position == "DST":
            defense_allowed = None
            pace_plays = pace.get(opp)
        else:
            defense_allowed = (defense_vs_position.get(opp) or {}).get(position)
            pace_plays = pace.get(team)
        edge = (
            nfl_scoring.score_player(
                position,
                implied_total=implied_total,
                is_home=is_home,
                spread=spread,
                favored=bool(favored),
                wind_mph=wind_mph,
                precip_chance_pct=precip_chance_pct,
                defense_allowed_per_game=defense_allowed,
                league_avg_defense_allowed=league_avg_defense.get(position),
                pace_plays_per_game=pace_plays,
                league_avg_pace=league_avg_pace,
            )
            if favored is not None
            else None
        )
        players.append(
            {
                "dk_id": row.get("dk_id") or None,
                # nflverse's own id for this player, the key every real
                # game log is filed under -- None for a rookie or anyone
                # with no prior-season history, which callers treat as
                # "no own history, lean on the position pool."
                "nflverse_id": nfl.resolve_player_id(id_lookup, row["name"], row["team"]),
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
    prior_season: dict[str, Any],
    fantasylabs_lines: list[dict[str, Any]],
    id_lookup: dict[str, Any],
) -> dict[str, Any]:
    home, away = game["home_team"], game["away_team"]

    # FantasyLabs (free, live, has the open line too -- see
    # clients/fantasylabs.py) is the primary source for score/total/
    # spread/moneyline, same swap MLB already made. nflverse's own
    # schedule-embedded line stays as a fallback for whichever games
    # FantasyLabs doesn't have a line for yet (e.g. a week far enough
    # out that lines haven't posted).
    vegas = _match_fantasylabs(fantasylabs_lines, home, away)
    line = dict(game)
    used_fantasylabs = _fantasylabs_has_line(vegas)
    if used_fantasylabs:
        if vegas.get("home_spread_current") is not None:
            line["spread_line"] = vegas["home_spread_current"]
        if vegas.get("total_current") is not None:
            line["total_line"] = vegas["total_current"]
        if vegas.get("home_moneyline_current") is not None:
            line["home_moneyline"] = vegas["home_moneyline_current"]
        if vegas.get("away_moneyline_current") is not None:
            line["away_moneyline"] = vegas["away_moneyline_current"]

    implied = nfl.implied_team_totals(line)
    spread = abs(line["spread_line"]) if line.get("spread_line") is not None else None
    home_favored = (
        line["home_moneyline"] < 0 if line.get("home_moneyline") is not None else None
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
        home, away, salary_rows, projection_lookup,
        implied_total=implied["home"], is_home=True, spread=spread,
        favored=home_favored, wind_mph=wind_mph, precip_chance_pct=precip_pct,
        prior_season=prior_season, id_lookup=id_lookup,
    )
    away_favored = None if home_favored is None else not home_favored
    away_players = _team_players(
        away, home, salary_rows, projection_lookup,
        implied_total=implied["away"], is_home=False, spread=spread,
        favored=away_favored, wind_mph=wind_mph, precip_chance_pct=precip_pct,
        prior_season=prior_season, id_lookup=id_lookup,
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
            "spread_line": line.get("spread_line"),
            "total_line": line.get("total_line"),
            "home_moneyline": line.get("home_moneyline"),
            "away_moneyline": line.get("away_moneyline"),
            "source": "FantasyLabs (consensus)" if used_fantasylabs else "nflverse (static)",
        },
        # Open vs. live -- see clients/fantasylabs.py. None on both
        # sides whenever FantasyLabs has no line for this game yet.
        "vegas": vegas or {"note": "no FantasyLabs line available"},
        "weather": wx or ({"note": "forecast unavailable"} if roof == "outdoors" else {"note": "roof closed"}),
        "home": {"abbrev": home, "implied_total": implied["home"], "favored": home_favored, "players": home_players},
        "away": {"abbrev": away, "implied_total": implied["away"], "favored": away_favored, "players": away_players},
    }


async def _attach_inhouse_projections(out_games: list[dict[str, Any]], season: int) -> None:
    """
    Adds projection["inhouse_fpts"], then ["inhouse_ownership_pct"]
    wherever a real DK salary is also loaded (ownership is meaningless
    without a salary-capped contest to be owned in), then
    ["inhouse_ceiling"]/["leverage_score"] (real upside minus the
    ownership the field is giving it). Mutates in place, additive
    alongside whatever RotoWire projection is already there.

    Baselines come from the PRIOR completed season -- the same static
    prior nfl_scoring.py's defense/pace components already lean on, and
    for the same stated reason: there's no current-season sample until
    real games are played.
    """
    all_players: list[dict[str, Any]] = []
    for g in out_games:
        for side in ("home", "away"):
            all_players.extend(
                p for p in (g[side]["players"] or []) if p.get("dk_id") and p.get("edge")
            )
    if not all_players:
        return

    # The slate's OWN season, not PRIOR_SEASON: the baseline now rolls
    # prior season + current season to date internally (see
    # nfl_inhouse_projections.rolling_game_log), so in-season games
    # update projections instead of 2025 being pinned forever.
    inhouse = await nfl_inhouse_projections.inhouse_fpts_batch(all_players, season)
    if not inhouse:
        return

    ownership_pool: list[dict[str, Any]] = []
    for g in out_games:
        for side in ("home", "away"):
            implied_total = g[side]["implied_total"]
            for p in g[side]["players"] or []:
                fpts = inhouse.get(p.get("dk_id"))
                if fpts is None or p.get("salary") is None:
                    continue
                # Backups dilute every group's fixed softmax total the
                # same way MLB bench bats did: 88 QBs on a real salary
                # file, 32 starters. There's no depth-chart feed, but
                # the projection itself already knows -- a backup's
                # rolling baseline projects near zero -- so a
                # positional FPTS floor keeps the pool to players the
                # field could actually roster.
                floor = nfl_inhouse_projections.OWNERSHIP_FPTS_FLOOR.get(p["position"])
                if floor is not None and fpts < floor:
                    continue
                ownership_pool.append(
                    {
                        "dk_id": p["dk_id"],
                        "position": p["position"],
                        "team": g[side]["abbrev"],
                        "salary": p["salary"],
                        "fpts": fpts,
                        "implied_total": implied_total,
                    }
                )

    ownership = nfl_inhouse_projections.project_ownership(ownership_pool)
    ceilings = await nfl_inhouse_projections.player_ceilings(all_players, nfl.PRIOR_SEASON)

    for g in out_games:
        for side in ("home", "away"):
            for p in g[side]["players"] or []:
                dk_id = p.get("dk_id")
                if dk_id is None:
                    continue
                fpts = inhouse.get(dk_id)
                if fpts is not None:
                    p["projection"] = {**(p["projection"] or {}), "inhouse_fpts": fpts}
                own_pct = ownership.get(dk_id)
                if own_pct is not None:
                    p["projection"] = {
                        **(p["projection"] or {}),
                        "inhouse_ownership_pct": own_pct,
                    }
                ceiling = ceilings.get(dk_id)
                if ceiling is not None and own_pct is not None:
                    p["projection"] = {
                        **(p["projection"] or {}),
                        "inhouse_ceiling": round(ceiling, 2),
                        "leverage_score": round(ceiling - own_pct, 2),
                    }


async def build_slate(
    season: int, week: int, *, force_refresh: bool = False, include_inhouse: bool = False
) -> dict[str, Any]:
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
    prior_season = await nfl.get_prior_season_context()
    # Reads the same already-cached prior-season stats get_prior_season_
    # context() just pulled, so this costs a single extra pass, not a
    # second fetch.
    id_lookup = await nfl.get_player_id_lookup(nfl.PRIOR_SEASON)

    # Any single real game date from this week works -- FantasyLabs
    # returns the whole week's slate regardless of which of that
    # week's real dates you pass (confirmed live, see
    # clients/fantasylabs.py's own module docstring).
    fantasylabs_lines = await fantasylabs.get_nfl_vegas_odds(
        games[0]["gameday"], force=force_refresh
    )

    built = await asyncio.gather(
        *[
            _build_game(g, salary_rows, projection_lookup, prior_season, fantasylabs_lines, id_lookup)
            for g in games
        ]
    )

    out_games = list(built)

    # Opt-in for the same reason MLB's is: computing these means a real
    # game-log read for every player on the slate. Cheap once cached,
    # but the first call pays for it, so a plain dashboard refresh
    # shouldn't silently carry that cost.
    if include_inhouse:
        await _attach_inhouse_projections(out_games, season)

    return {
        "season": season,
        "week": week,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "games": out_games,
    }
