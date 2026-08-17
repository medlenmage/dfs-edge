"""
Builds the daily MLB slate: every game, its environment, and every
hitter's matchup edge.

THE FLOW
--------
1. Pull today's schedule (games, venues, probable pitchers).
2. Pull the whole league's splits in a handful of requests, not one per
   player.
3. Pull betting lines and match them to games by team name.
4. Pull a weather forecast per outdoor park.
5. For each hitter, assemble the components and score the matchup.

Steps 2-4 run concurrently because they don't depend on each other.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date as date_cls
from datetime import datetime
from typing import Any

from app import cache
from app.clients import mlb, odds, savant, weather
from app.data.parks import get_park, hr_factor_for_hand
from app.services import projections, salaries, scoring

log = logging.getLogger(__name__)

PITCHER_POSITIONS = {"P", "SP", "RP", "TWP"}


# --------------------------------------------------------------------------
# Team-name matching between MLB and the sportsbooks
# --------------------------------------------------------------------------

def _norm(name: str | None) -> str:
    return re.sub(r"[^a-z]", "", (name or "").lower())


def _nickname(name: str | None) -> str:
    """Last word of a team name -- 'Yankees', 'Athletics', 'Red Sox' -> 'sox'."""
    parts = (name or "").split()
    return _norm(parts[-1]) if parts else ""


def _match_odds(
    lines: list[dict[str, Any]], home_name: str, away_name: str
) -> dict[str, Any] | None:
    """Find the betting line for a game, tolerating naming differences."""
    h, a = _norm(home_name), _norm(away_name)
    for line in lines:
        if _norm(line.get("home_team")) == h and _norm(line.get("away_team")) == a:
            return line

    hn, an = _nickname(home_name), _nickname(away_name)
    for line in lines:
        if (
            _nickname(line.get("home_team")) == hn
            and _nickname(line.get("away_team")) == an
        ):
            return line
    return None


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------

async def build_slate(
    day: str | None = None,
    *,
    force_refresh: bool = False,
    include_hitters: bool = True,
) -> dict[str, Any]:
    day = day or date_cls.today().isoformat()
    season = int(day[:4])

    games = await mlb.get_schedule(day, force=force_refresh)
    if not games:
        return {
            "date": day,
            "season": season,
            "games": [],
            "message": f"No MLB games scheduled for {day}.",
        }

    # --- Fetch everything that doesn't depend on anything else, at once ---
    tasks = {
        "hit_vl": mlb.get_league_splits(season, "vl", "hitting"),
        "hit_vr": mlb.get_league_splits(season, "vr", "hitting"),
        "hit_home": mlb.get_league_splits(season, "h", "hitting"),
        "hit_away": mlb.get_league_splits(season, "a", "hitting"),
        "hit_season": mlb.get_league_season(season, "hitting"),
        "hit_recent": mlb.get_recent_form(season, 15, "hitting"),
        "pit_vl": mlb.get_league_splits(season, "vl", "pitching"),
        "pit_vr": mlb.get_league_splits(season, "vr", "pitching"),
        "pit_season": mlb.get_league_season(season, "pitching"),
        "lines": odds.get_game_lines("mlb", force=force_refresh),
        "savant_hit": savant.get_hitter_batted_ball(season),
        "savant_pitch": savant.get_pitcher_batted_ball(season),
        "bullpen": mlb.get_bullpen_stats(season),
    }
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    data: dict[str, Any] = {}
    warnings: list[str] = []
    for key, result in zip(tasks.keys(), results):
        if isinstance(result, Exception):
            log.warning("Fetch %s failed: %s", key, result)
            warnings.append(f"{key}: {result}")
            data[key] = [] if key == "lines" else {}
        else:
            data[key] = result

    lines: list[dict[str, Any]] = data["lines"]

    # --- League baselines, computed from the data itself ---
    baselines = {
        "hitter_ops_vl": scoring.league_average(data["hit_vl"], "ops", 40, "pa"),
        "hitter_ops_vr": scoring.league_average(data["hit_vr"], "ops", 80, "pa"),
        "pitcher_ops_vl": scoring.league_average(data["pit_vl"], "ops_against", 50, "bf"),
        "pitcher_ops_vr": scoring.league_average(data["pit_vr"], "ops_against", 50, "bf"),
        "pitcher_era": scoring.league_average(data["pit_season"], "era", 20, "ip"),
        "pitcher_k9": scoring.league_average(data["pit_season"], "k_per_9", 20, "ip"),
        "hitter_k_pct": scoring.league_average(data["hit_season"], "k_pct", 80, "pa"),
        "hitter_sb_per_pa": scoring.league_average(data["hit_season"], "sb_per_pa", 100, "pa"),
        # Savant's leaderboard is already filtered to a minimum sample
        # (see clients/savant.py), so no further sample-size gate here.
        "hitter_barrel": scoring.league_average(data["savant_hit"], "barrel_pct", 0, "barrel_pct"),
        "hitter_hard_hit": scoring.league_average(data["savant_hit"], "hard_hit_pct", 0, "hard_hit_pct"),
        "hitter_xwoba": scoring.league_average(data["savant_hit"], "xwoba", 0, "xwoba"),
        "pitcher_barrel": scoring.league_average(data["savant_pitch"], "barrel_pct", 0, "barrel_pct"),
        "pitcher_hard_hit": scoring.league_average(data["savant_pitch"], "hard_hit_pct", 0, "hard_hit_pct"),
        "pitcher_xwoba": scoring.league_average(data["savant_pitch"], "xwoba", 0, "xwoba"),
        # get_bullpen_stats() already filters to teams with a trustworthy
        # sample of relief innings, so no further gate here either.
        "bullpen_era": scoring.league_average(data["bullpen"], "era", 0, "era"),
    }

    # Salaries and projections are manual uploads, not a fetch -- see
    # services/salaries.py and services/projections.py. Whatever's
    # cached for this date (possibly nothing) gets matched in.
    salary_rows = salaries.load(day)
    salary_lookup = salaries.build_lookup(salary_rows)
    projection_lookup = projections.build_lookup(projections.load(day))

    # Which games the uploaded DK salary CSV actually covers, so each
    # game can be flagged as in/out of that slate for the optimizer (and
    # anything else that wants it) -- None (not True/False) when no
    # salary CSV is loaded yet, since there's nothing to detect against.
    detected_slate_pairs = (
        {frozenset((g["away"], g["home"])) for g in salaries.slate_games(salary_rows)}
        if salary_rows
        else None
    )

    built = await asyncio.gather(
        *[
            _build_game(
                g, season, data, baselines, lines, include_hitters,
                salary_lookup, projection_lookup, day, detected_slate_pairs,
            )
            for g in games
        ],
        return_exceptions=True,
    )

    out_games = []
    for game, result in zip(games, built):
        if isinstance(result, Exception):
            log.exception("Failed to build game %s", game.get("gamePk"))
            warnings.append(f"game {game.get('gamePk')}: {result}")
            continue
        out_games.append(result)

    out_games.sort(key=lambda g: g.get("game_time_utc") or "")

    return {
        "date": day,
        "season": season,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "baselines": baselines,
        "games": out_games,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------
# One game
# --------------------------------------------------------------------------

async def _build_game(
    game: dict[str, Any],
    season: int,
    data: dict[str, Any],
    baselines: dict[str, Any],
    lines: list[dict[str, Any]],
    include_hitters: bool,
    salary_lookup: dict[tuple[str, str], dict[str, Any]],
    projection_lookup: dict[tuple[str, str], dict[str, Any]],
    day: str,
    detected_slate_pairs: set[frozenset[str]] | None,
) -> dict[str, Any]:
    game_pk = game.get("gamePk")
    teams = game.get("teams") or {}
    home_t = (teams.get("home") or {}).get("team") or {}
    away_t = (teams.get("away") or {}).get("team") or {}
    venue = game.get("venue") or {}

    home_abbrev = home_t.get("abbreviation") or ""
    away_abbrev = away_t.get("abbreviation") or ""
    park = get_park(home_abbrev, venue.get("name"))
    game_time = game.get("gameDate") or ""

    # Whether the uploaded DK salary CSV's slate covers this game --
    # None when no CSV is loaded yet (nothing to detect against).
    in_slate = (
        frozenset((away_abbrev, home_abbrev)) in detected_slate_pairs
        if detected_slate_pairs is not None
        else None
    )

    # --- Weather ---
    roof_closed = park["roof"] == "dome"
    wx = None
    if not roof_closed and park["lat"]:
        wx = await weather.get_game_weather(park["lat"], park["lon"], game_time)

    temp_fx = weather.temperature_effect((wx or {}).get("temp_f")) if wx else None
    wind_fx = (
        weather.wind_effect(
            (wx or {}).get("wind_dir_deg"),
            (wx or {}).get("wind_mph"),
            park.get("orientation_deg"),
        )
        if wx
        else None
    )
    # A retractable roof is usually shut in extreme heat or rain.
    if park["roof"] == "retractable" and wx:
        if (wx.get("precip_chance_pct") or 0) >= 50 or (wx.get("temp_f") or 0) >= 95:
            roof_closed = True

    # --- Betting line ---
    line = _match_odds(lines, home_t.get("name"), away_t.get("name"))

    # --- Probable pitchers ---
    home_pp = (teams.get("home") or {}).get("probablePitcher") or {}
    away_pp = (teams.get("away") or {}).get("probablePitcher") or {}
    pitcher_ids = [p.get("id") for p in (home_pp, away_pp) if p.get("id")]
    pitcher_bios = await mlb.get_people(pitcher_ids) if pitcher_ids else {}

    home_pitcher = _pitcher_card(home_pp, pitcher_bios, data)
    away_pitcher = _pitcher_card(away_pp, pitcher_bios, data)

    result: dict[str, Any] = {
        "game_pk": game_pk,
        "game_time_utc": game_time,
        "status": ((game.get("status") or {}).get("detailedState")),
        "in_slate": in_slate,
        "venue": {
            "name": venue.get("name") or park["name"],
            "roof": park["roof"],
            "roof_closed": roof_closed,
            "elevation_ft": park["elevation_ft"],
            "park_factors": {
                "runs": park["runs"],
                "hr": park["hr"],
                "hr_lhb": park["hr_lhb"],
                "hr_rhb": park["hr_rhb"],
            },
        },
        "weather": (
            {**wx, "temperature_effect": temp_fx, "wind_effect": wind_fx}
            if wx
            else {"note": "roof closed" if roof_closed else "forecast unavailable"}
        ),
        "betting": line or {"note": "no line available"},
        "home": {
            "team_id": home_t.get("id"),
            "abbrev": home_abbrev,
            "name": home_t.get("name"),
            "implied_runs": (line or {}).get("home_implied_runs"),
            "probable_pitcher": home_pitcher,
        },
        "away": {
            "team_id": away_t.get("id"),
            "abbrev": away_t.get("abbreviation"),
            "name": away_t.get("name"),
            "implied_runs": (line or {}).get("away_implied_runs"),
            "probable_pitcher": away_pitcher,
        },
    }

    if not include_hitters:
        return result

    # --- Hitters for both sides ---
    env = {
        "park": park,
        "roof_closed": roof_closed,
        "temp_fx": temp_fx,
        "wind_fx": wind_fx,
    }
    lineups = await mlb.get_lineups(game_pk) if game_pk else {"home": [], "away": []}

    home_hitters, away_hitters, home_injuries, away_injuries = await asyncio.gather(
        _team_hitters(
            home_t.get("id"), season, data, baselines, env,
            opposing_pitcher=away_pitcher, opponent_team_id=away_t.get("id"),
            is_home=True,
            implied_runs=(line or {}).get("home_implied_runs"),
            confirmed=lineups.get("home") or [],
            team_abbrev=home_abbrev, salary_lookup=salary_lookup,
            projection_lookup=projection_lookup,
        ),
        _team_hitters(
            away_t.get("id"), season, data, baselines, env,
            opposing_pitcher=home_pitcher, opponent_team_id=home_t.get("id"),
            is_home=False,
            implied_runs=(line or {}).get("away_implied_runs"),
            confirmed=lineups.get("away") or [],
            team_abbrev=away_t.get("abbreviation") or "", salary_lookup=salary_lookup,
            projection_lookup=projection_lookup,
        ),
        mlb.get_team_injuries(home_t.get("id"), season),
        mlb.get_team_injuries(away_t.get("id"), season),
    )
    result["home"]["hitters"] = home_hitters
    result["away"]["hitters"] = away_hitters
    result["home"]["lineup_confirmed"] = bool(lineups.get("home"))
    result["away"]["lineup_confirmed"] = bool(lineups.get("away"))
    result["home"]["injuries"] = home_injuries
    result["away"]["injuries"] = away_injuries

    # Late scratches the lineup watcher has caught for this game today --
    # see services/lineup_watch.py. Purely additive: this cache key is
    # only ever populated by the background poller, never by a request.
    day_scratches = cache.get(f"scratches:{day}") or []
    game_scratches = [s for s in day_scratches if s.get("game_pk") == game_pk]
    result["home"]["scratches"] = [s for s in game_scratches if s.get("team") == home_abbrev]
    result["away"]["scratches"] = [
        s for s in game_scratches if s.get("team") == (away_t.get("abbreviation") or "")
    ]

    # Team-level stack score = average of the top 5 hitters' scores.
    result["home"]["stack_score"] = _stack_score(home_hitters)
    result["away"]["stack_score"] = _stack_score(away_hitters)

    # Pitcher edge = the mirror image of the hitter model: the home
    # pitcher faces the AWAY lineup and is trying to hold down the AWAY
    # team's implied total, and vice versa.
    home_edge = _pitcher_edge(
        home_pitcher, away_hitters, result["away"]["implied_runs"], env, baselines,
        data["savant_pitch"],
    )
    away_edge = _pitcher_edge(
        away_pitcher, home_hitters, result["home"]["implied_runs"], env, baselines,
        data["savant_pitch"],
    )
    if home_edge:
        result["home"]["probable_pitcher"]["edge"] = home_edge
        result["home"]["probable_pitcher"]["salary"] = _salary_info(
            salary_lookup, home_pitcher.get("name"), home_abbrev, home_edge["score"]
        )
        result["home"]["probable_pitcher"]["projection"] = _projection_info(
            projection_lookup, home_pitcher.get("name"), home_abbrev
        )
    if away_edge:
        result["away"]["probable_pitcher"]["edge"] = away_edge
        result["away"]["probable_pitcher"]["salary"] = _salary_info(
            salary_lookup, away_pitcher.get("name"), away_t.get("abbreviation") or "", away_edge["score"]
        )
        result["away"]["probable_pitcher"]["projection"] = _projection_info(
            projection_lookup, away_pitcher.get("name"), away_t.get("abbreviation") or ""
        )

    return result


def _projection_info(
    lookup: dict[tuple[str, str], dict[str, Any]],
    name: str | None,
    team_abbrev: str,
) -> dict[str, Any] | None:
    """RotoWire's FPTS/ownership projection for one player, or None if no
    projections file is loaded for this date or nothing matched. Purely
    informational -- see services/projections.py for why this never
    touches the edge score."""
    if not lookup or not name:
        return None
    row = projections.match(lookup, name, team_abbrev)
    if not row:
        return None
    return {"fpts": row["fpts"], "ownership_pct": row["ownership_pct"]}


def _salary_info(
    lookup: dict[tuple[str, str], dict[str, Any]],
    name: str | None,
    team_abbrev: str,
    edge_score: float | None,
) -> dict[str, Any] | None:
    """Salary + value for one player, or None if no salary file is loaded
    for this date or the name/team didn't match anything in it."""
    if not lookup or not name:
        return None
    row = salaries.match(lookup, name, team_abbrev)
    if not row:
        return None
    return {
        "salary": row["salary"],
        "avg_points": row["avg_points"],
        "value": salaries.value_score(edge_score, row["salary"]),
        # DK's own position string (e.g. "1B/3B") -- distinct from the
        # single MLB-primary-position on the player dict itself, and the
        # one the lineup optimizer needs for roster-slot eligibility.
        "position": row["position"],
    }


def _stack_score(hitters: list[dict[str, Any]]) -> float | None:
    """
    How good is it to stack this offence?

    Stacking means rostering several hitters from the same team so that a
    big inning pays you multiple times. We average the top five matchup
    scores rather than the whole roster, because a stack only needs five
    good bats.
    """
    scores = sorted((h["edge"]["score"] for h in hitters), reverse=True)[:5]
    if not scores:
        return None
    return round(sum(scores) / len(scores), 1)


def _pitcher_edge(
    pitcher_card: dict[str, Any] | None,
    facing_hitters: list[dict[str, Any]],
    implied_runs_against: float | None,
    env: dict[str, Any],
    baselines: dict[str, Any],
    savant_pitch: dict[int, dict[str, Any]],
) -> dict[str, Any] | None:
    """
    A pitcher's matchup score -- the same component/weight machinery as
    the hitter model, run in reverse: the batting environment (park,
    weather, opponent's implied total) is inverted around 1.00, and the
    opposing lineup's own matchup strength against this exact pitcher
    (already computed by `_team_hitters`) is reused rather than refetched.
    """
    if not pitcher_card:
        return None

    park = env["park"]

    runs_against = scoring.team_total_component(implied_runs_against)
    runs_against = {**runs_against, "value": scoring.invert_for_pitcher(runs_against["value"])}

    park_comp = scoring.park_component(park["hr"], park["runs"])
    park_comp = {**park_comp, "value": scoring.invert_for_pitcher(park_comp["value"])}

    weather_comp = scoring.weather_component(env["temp_fx"], env["wind_fx"], env["roof_closed"])
    weather_comp = {**weather_comp, "value": scoring.invert_for_pitcher(weather_comp["value"])}

    components = {
        "opp_lineup": scoring.opp_lineup_component(facing_hitters),
        "strikeout_potential": scoring.strikeout_potential_component(
            pitcher_card.get("season"),
            baselines.get("pitcher_k9"),
            facing_hitters,
            baselines.get("hitter_k_pct"),
        ),
        "team_runs_against": runs_against,
        "contact_quality_allowed": scoring.contact_quality_allowed_component(
            savant_pitch.get(pitcher_card.get("id")),
            baselines.get("pitcher_barrel"),
            baselines.get("pitcher_hard_hit"),
            baselines.get("pitcher_xwoba"),
        ),
        "own_quality": scoring.own_quality_component(
            pitcher_card.get("season"), baselines.get("pitcher_era")
        ),
        "park": park_comp,
        "weather": weather_comp,
    }
    edge = scoring.combine(components, scoring.PITCHER_WEIGHTS)
    return {**edge, "components": components}


def _pitcher_card(
    pp: dict[str, Any], bios: dict[int, dict[str, Any]], data: dict[str, Any]
) -> dict[str, Any] | None:
    pid = pp.get("id")
    if not pid:
        return None
    bio = bios.get(pid, {})
    return {
        "id": pid,
        "name": pp.get("fullName") or bio.get("name"),
        "throws": bio.get("throws"),
        "season": data["pit_season"].get(pid),
        "vs_lhb": data["pit_vl"].get(pid),
        "vs_rhb": data["pit_vr"].get(pid),
    }


# --------------------------------------------------------------------------
# One team's hitters
# --------------------------------------------------------------------------

async def _team_hitters(
    team_id: int | None,
    season: int,
    data: dict[str, Any],
    baselines: dict[str, Any],
    env: dict[str, Any],
    *,
    opposing_pitcher: dict[str, Any] | None,
    opponent_team_id: int | None,
    is_home: bool,
    implied_runs: float | None,
    confirmed: list[int],
    team_abbrev: str = "",
    salary_lookup: dict[tuple[str, str], dict[str, Any]] | None = None,
    projection_lookup: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if not team_id:
        return []

    opp_bullpen = data["bullpen"].get(opponent_team_id)

    # Prefer the confirmed lineup; fall back to the active roster.
    if confirmed:
        player_ids = confirmed
    else:
        player_ids = await mlb.get_active_roster(team_id, season)

    bios = await mlb.get_people(player_ids)
    p_hand = (opposing_pitcher or {}).get("throws")  # 'L' or 'R'

    # Which league baseline applies depends on the pitcher's hand.
    hitter_baseline = (
        baselines["hitter_ops_vl"] if p_hand == "L" else baselines["hitter_ops_vr"]
    )
    hitter_splits = data["hit_vl"] if p_hand == "L" else data["hit_vr"]

    out: list[dict[str, Any]] = []
    for idx, pid in enumerate(player_ids):
        bio = bios.get(pid)
        if not bio:
            continue
        if (bio.get("position") or "") in PITCHER_POSITIONS:
            continue

        season_stat = data["hit_season"].get(pid)
        # Skip players with almost no playing time -- they add noise.
        if not season_stat or (season_stat.get("pa") or 0) < 25:
            continue

        bats = bio.get("bats") or "R"
        split_stat = hitter_splits.get(pid)

        # Pitcher's split against THIS batter's hand.
        pitcher_split = None
        if opposing_pitcher:
            pitcher_split = (
                opposing_pitcher.get("vs_lhb")
                if bats == "L"
                else opposing_pitcher.get("vs_rhb")
            )
            if bats == "S":
                # A switch hitter always bats opposite the pitcher.
                pitcher_split = (
                    opposing_pitcher.get("vs_rhb")
                    if p_hand == "L"
                    else opposing_pitcher.get("vs_lhb")
                )
        pitcher_baseline = (
            baselines["pitcher_ops_vl"] if bats == "L" else baselines["pitcher_ops_vr"]
        )

        park = env["park"]
        components = {
            "platoon": scoring.platoon_component(split_stat, hitter_baseline),
            "pitcher": scoring.pitcher_component(pitcher_split, pitcher_baseline),
            "team_total": scoring.team_total_component(implied_runs),
            "contact_quality": scoring.contact_quality_component(
                data["savant_hit"].get(pid),
                baselines["hitter_barrel"],
                baselines["hitter_hard_hit"],
                baselines["hitter_xwoba"],
            ),
            "stolen_base": scoring.stolen_base_component(
                season_stat, baselines["hitter_sb_per_pa"]
            ),
            "bullpen": scoring.bullpen_component(opp_bullpen, baselines["bullpen_era"]),
            "park": scoring.park_component(
                hr_factor_for_hand(park, bats), park["runs"]
            ),
            "weather": scoring.weather_component(
                env["temp_fx"], env["wind_fx"], env["roof_closed"]
            ),
            "form": scoring.form_component(
                data["hit_recent"].get(pid), season_stat
            ),
            "home_road": scoring.home_road_component(
                (data["hit_home"] if is_home else data["hit_away"]).get(pid),
                season_stat,
                is_home,
            ),
        }
        edge = scoring.combine(components)

        out.append(
            {
                "id": pid,
                "name": bio.get("name"),
                "position": bio.get("position"),
                "bats": bats,
                "batting_order": idx + 1 if confirmed else None,
                "season": season_stat,
                "vs_hand": split_stat,
                "edge": {**edge, "components": components},
                "salary": _salary_info(salary_lookup, bio.get("name"), team_abbrev, edge["score"]),
                "projection": _projection_info(projection_lookup, bio.get("name"), team_abbrev),
            }
        )

    out.sort(key=lambda h: h["edge"]["score"], reverse=True)
    return out
