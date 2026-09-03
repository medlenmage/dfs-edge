"""
Offline test of the NFL pipeline -- pure functions and the optimizer
against synthetic pools, no network. Kept separate from
test_pipeline.py (the MLB suite) since this is a genuinely separate
subsystem sharing only player_match.py.

Run it with:
    cd backend
    .venv/bin/python -m tests.test_nfl_pipeline
"""

from __future__ import annotations

import asyncio
import copy
import inspect as inspect_module
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import cache  # noqa: E402
from app.clients import fantasylabs, nfl, nfl_pbp, rotowire_nfl  # noqa: E402
from app.services import (  # noqa: E402
    nfl_contest,
    nfl_correlations,
    nfl_dk_points,
    nfl_inhouse_projections,
    nfl_optimizer,
    nfl_scoring,
    nfl_slate,
    nfl_stack_rating,
    nfl_variance,
    player_match,
    salaries,
)

_FAKE_CACHE: dict[str, object] = {}


def _fake_cache_get(key):
    return _FAKE_CACHE.get(key)


def _fake_cache_put(key, value, ttl):
    _FAKE_CACHE[key] = value

PASS, FAILED = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAILED).append(name)
    mark = "PASS" if condition else "FAIL"
    print(f"  {mark}  {name}" + (f"  -- {detail}" if detail else ""))


def main() -> int:
    print("NFL pipeline test (offline, no network)\n" + "=" * 60)

    # --------------------------------------------------------------------
    # clients/nfl.py -- pure helpers
    # --------------------------------------------------------------------
    print("\nSeason/week resolution and implied totals")
    from datetime import date as date_cls

    check("a date in August maps to that year's season",
          nfl.season_for_date(date_cls(2026, 8, 16)) == 2026)
    check("a date in January maps to the PRIOR year's season (still that season's playoffs)",
          nfl.season_for_date(date_cls(2027, 1, 20)) == 2026)
    check("a date in February still maps to the prior year",
          nfl.season_for_date(date_cls(2027, 2, 5)) == 2026)
    check("a date in March starts mapping to the new year",
          nfl.season_for_date(date_cls(2027, 3, 1)) == 2027)

    games = [
        {"week": 1, "gameday": "2026-09-10"},
        {"week": 2, "gameday": "2026-09-17"},
        {"week": 3, "gameday": "2026-09-24"},
    ]
    check("current_week returns the week whose games are still upcoming",
          nfl.current_week(games, "2026-09-15") == 2)
    check("current_week falls back to week 1 before the season starts",
          nfl.current_week(games, "2026-08-01") == 1)
    check("current_week falls back to the last week once the season's over",
          nfl.current_week(games, "2026-12-01") == 3)
    check("current_week defaults to 1 with no games at all",
          nfl.current_week([], "2026-09-15") == 1)

    home_favored_game = {"total_line": 44.5, "spread_line": 3.5, "home_moneyline": -185.0}
    implied = nfl.implied_team_totals(home_favored_game)
    check("implied_team_totals splits correctly when the home team is favored (negative moneyline)",
          implied["home"] == 24.0 and implied["away"] == 20.5, str(implied))

    away_favored_game = {"total_line": 46.5, "spread_line": 2.5, "home_moneyline": 124.0}
    implied2 = nfl.implied_team_totals(away_favored_game)
    check("implied_team_totals splits correctly when the away team is favored",
          implied2["home"] == 22.0 and implied2["away"] == 24.5, str(implied2))

    check("implied_team_totals is neutral (both None) when the line is missing",
          nfl.implied_team_totals({"total_line": None, "spread_line": None, "home_moneyline": None})
          == {"home": None, "away": None})

    # --------------------------------------------------------------------
    # services/nfl_scoring.py
    # --------------------------------------------------------------------
    print("\nMatchup scoring")
    high_total = nfl_scoring.team_environment_score(30.0, is_home=True)
    low_total = nfl_scoring.team_environment_score(14.0, is_home=False)
    check("a well-above-average implied total scores well above 50",
          high_total["score"] > 65, str(high_total))
    check("a well-below-average road implied total scores well below 50",
          low_total["score"] < 35, str(low_total))
    check("no betting line at all is neutral (score 50)",
          nfl_scoring.team_environment_score(None, is_home=True)["score"] == 50.0)

    rb_favored = nfl_scoring.game_script_component("RB", 10.0, favored=True)
    rb_dog = nfl_scoring.game_script_component("RB", 10.0, favored=False)
    check("a favored RB gets a positive game-script bump",
          rb_favored["value"] > 0, str(rb_favored))
    check("an underdog RB gets the mirror-image negative adjustment",
          rb_dog["value"] == -rb_favored["value"], str((rb_favored, rb_dog)))

    wr_dog = nfl_scoring.game_script_component("WR", 10.0, favored=False)
    check("a trailing-team WR gets a positive lean (more pass volume)",
          wr_dog["value"] > 0, str(wr_dog))

    windy = nfl_scoring.weather_component("WR", 22.0, 10.0)
    calm = nfl_scoring.weather_component("WR", 5.0, 10.0)
    check("heavy wind penalizes a WR's weather component",
          windy["value"] < calm["value"], str((windy, calm)))
    rb_wind = nfl_scoring.weather_component("RB", 22.0, 10.0)
    check("wind hurts a WR more than it hurts an RB (a rushing game is largely wind-proof)",
          windy["value"] < rb_wind["value"] < 0, str((windy, rb_wind)))

    full = nfl_scoring.score_player(
        "RB", implied_total=27.0, is_home=True, spread=7.0, favored=True,
        wind_mph=5.0, precip_chance_pct=10,
        defense_allowed_per_game=28.0, league_avg_defense_allowed=22.85,
        pace_plays_per_game=63.8, league_avg_pace=60.0,
    )
    check("score_player returns a 0-100 score with every component (including the new pair) and a top_driver",
          0 <= full["score"] <= 100
          and set(full["components"]) == {"implied_total", "game_script", "weather", "defense_vs_position", "pace"}
          and full["top_driver"] in full["components"],
          str(full))

    print("\nDefense-vs-position and pace components (prior-season prior)")
    funnel = nfl_scoring.defense_vs_position_component("RB", 32.1, 22.85)
    tough = nfl_scoring.defense_vs_position_component("RB", 16.4, 22.85)
    check("a defense allowing well above average to a position scores as a funnel matchup",
          funnel["value"] > 0 and "funnel" in funnel["detail"], str(funnel))
    check("a defense allowing well below average scores as a tough matchup",
          tough["value"] < 0 and "tough" in tough["detail"], str(tough))
    check("defense_vs_position_component is neutral with no data",
          nfl_scoring.defense_vs_position_component("RB", None, 22.85)["value"] == 0.0)
    extreme = nfl_scoring.defense_vs_position_component("RB", 200.0, 20.0)
    check("defense_vs_position_component caps its adjustment rather than blowing up on an outlier",
          extreme["value"] == nfl_scoring.DEFENSE_VS_POSITION_MAX_ADJUSTMENT, str(extreme))

    fast = nfl_scoring.pace_component(63.8, 60.0)
    slow = nfl_scoring.pace_component(56.3, 60.0)
    check("a faster-than-average offense gets a positive pace bump",
          fast["value"] > 0, str(fast))
    check("a slower-than-average offense gets a negative pace adjustment",
          slow["value"] < 0, str(slow))
    check("pace_component is neutral with no data",
          nfl_scoring.pace_component(None, 60.0)["value"] == 0.0)
    extreme_pace = nfl_scoring.pace_component(300.0, 60.0)
    check("pace_component caps its adjustment rather than blowing up on an outlier",
          extreme_pace["value"] == nfl_scoring.PACE_MAX_ADJUSTMENT, str(extreme_pace))

    # --------------------------------------------------------------------
    # services/nfl_dk_points.py -- DK fantasy points from raw box-score
    # counting stats (the prior-season defense/pace aggregation's, and
    # a future variance model's, building block). Moved out of
    # clients/nfl.py's own private _dk_fantasy_points() into a
    # standalone module mirroring mlb_dk_points.py -- inputs are
    # already-numeric here, matching clients/nfl._parse_stat_row()'s
    # output and mlb_dk_points.py's own established convention.
    # --------------------------------------------------------------------
    print("\nDK fantasy points from raw box-score stats (nfl_dk_points.py)")
    qb_row = {"passing_yards": 320.0, "passing_tds": 2.0, "passing_interceptions": 1.0}
    check("a 320-yard, 2 TD, 1 INT passing game scores the 300-yard bonus correctly",
          nfl_dk_points.game_points(qb_row) == 22.8, str(nfl_dk_points.game_points(qb_row)))

    rb_row = {"rushing_yards": 105.0, "rushing_tds": 1.0}
    check("a 105-yard, 1 TD rushing game scores the 100-yard bonus correctly",
          nfl_dk_points.game_points(rb_row) == 19.5, str(nfl_dk_points.game_points(rb_row)))

    rb_no_bonus = {"rushing_yards": 80.0, "rushing_tds": 0.0}
    check("rushing under 100 yards doesn't get the bonus",
          nfl_dk_points.game_points(rb_no_bonus) == 8.0, str(nfl_dk_points.game_points(rb_no_bonus)))

    fumble_row = {"rushing_yards": 10.0, "sack_fumbles_lost": 1.0, "rushing_2pt_conversions": 1.0}
    check("lost fumbles and 2pt conversions are scored correctly",
          nfl_dk_points.game_points(fumble_row) == 1.0 - 1 + 2, str(nfl_dk_points.game_points(fumble_row)))

    print("\nDST fantasy points from raw team box-score stats (nfl_dk_points.dst_game_points)")
    dominant_dst = {
        "def_sacks": 4.0, "def_interceptions": 2.0, "fumble_recovery_opp": 1.0,
        "def_tds": 1.0, "special_teams_tds": 0.0, "def_safeties": 0.0,
        "def_punt_blocks": 0.0, "def_pat_blocks": 0.0, "def_fg_blocks": 0.0,
        "points_allowed": 6,
    }
    # 4 sacks(4) + 2 INT(4) + 1 fumble recovery(2) + 1 TD(6) + 1-6 pts allowed tier(7) = 23
    check("a dominant DST game (sacks, INTs, a fumble recovery, a TD, and a shutout-adjacent "
          "points-allowed tier) scores correctly",
          nfl_dk_points.dst_game_points(dominant_dst) == 23.0, str(nfl_dk_points.dst_game_points(dominant_dst)))

    shutout_dst = {"points_allowed": 0}
    check("a real shutout (0 points allowed) scores the top +10 points-allowed tier",
          nfl_dk_points.dst_game_points(shutout_dst) == 10.0, str(nfl_dk_points.dst_game_points(shutout_dst)))

    blowout_loss_dst = {"points_allowed": 41}
    check("allowing 35+ points scores the -4 points-allowed tier",
          nfl_dk_points.dst_game_points(blowout_loss_dst) == -4.0, str(nfl_dk_points.dst_game_points(blowout_loss_dst)))

    mid_dst = {"points_allowed": 24}
    check("allowing 21-27 points scores 0 (the real DK 'neutral' points-allowed tier)",
          nfl_dk_points.dst_game_points(mid_dst) == 0.0, str(nfl_dk_points.dst_game_points(mid_dst)))

    no_score_row = {"def_sacks": 2.0}
    check("points_allowed=None (a game not yet joined to a real final score) contributes no "
          "points-allowed tier at all, rather than defaulting to a fabricated tier",
          nfl_dk_points.dst_game_points(no_score_row) == 2.0, str(nfl_dk_points.dst_game_points(no_score_row)))

    special_teams_td_dst = {"special_teams_tds": 1.0, "points_allowed": 14}
    check("a special-teams (return) TD scores the same +6 as a defensive TD",
          nfl_dk_points.dst_game_points(special_teams_td_dst) == 6.0 + 1.0,
          str(nfl_dk_points.dst_game_points(special_teams_td_dst)))

    print("\nPrior-season defense-vs-position + pace aggregation (fake CSV, no network)")
    # Column names match the LIVE "stats_player_week_{season}" release
    # (clients/nfl.py migrated to it this pass -- the old "player_stats"
    # release it used to read was deprecated by nflverse and never got
    # a 2025 file at all, a real, live gap not a hypothetical one).
    # Two real renames from the old release: recent_team -> team,
    # interceptions -> passing_interceptions.
    fake_csv = (
        "player_id,player_display_name,team,season,week,season_type,opponent_team,"
        "position,position_group,attempts,carries,"
        "passing_yards,passing_tds,passing_interceptions,rushing_yards,rushing_tds,receptions,"
        "receiving_yards,receiving_tds,sack_fumbles_lost,rushing_fumbles_lost,"
        "receiving_fumbles_lost,passing_2pt_conversions,rushing_2pt_conversions,"
        "receiving_2pt_conversions,special_teams_tds\n"
        "9001,RB One,AAA,2099,1,REG,BBB,RB,RB,0,20,0,0,0,100,1,0,0,0,0,0,0,0,0,0\n"
        "9001,RB One,AAA,2099,2,REG,BBB,RB,RB,0,15,0,0,0,50,0,0,0,0,0,0,0,0,0,0\n"
        "9001,RB One,AAA,2099,1,PRE,BBB,RB,RB,0,999,0,0,0,999,1,0,0,0,0,0,0,0,0,0\n"
        "9002,WR One,BBB,2099,1,REG,AAA,WR,WR,30,0,0,0,0,0,0,5,80,1,0,0,0,0,0,0,0\n"
        "9002,WR One,BBB,2099,2,REG,AAA,WR,WR,25,0,0,0,0,0,0,3,40,0,0,0,0,0,0,0,0\n"
    )

    async def _fake_loader(season, *, force=False):
        return fake_csv

    cache.get = _fake_cache_get
    cache.put = _fake_cache_put
    nfl._load_player_stats_csv = _fake_loader
    context = asyncio.run(nfl.get_prior_season_context(season=2099))

    check("preseason rows are excluded from the aggregation",
          context["defense_vs_position"]["BBB"]["RB"] == 12.0, str(context["defense_vs_position"]))
    check("defense-vs-position is a per-game average of DK points allowed to that position",
          context["defense_vs_position"]["AAA"]["WR"] == 13.0, str(context["defense_vs_position"]))
    check("a position never faced isn't in a team's defense-vs-position breakdown",
          "WR" not in context["defense_vs_position"]["BBB"], str(context["defense_vs_position"]))
    check("league_avg_defense_vs_position only covers positions with real data",
          context["league_avg_defense_vs_position"] == {"RB": 12.0, "WR": 13.0},
          str(context["league_avg_defense_vs_position"]))
    check("pace is plays (attempts + carries) per game, averaged across weeks",
          context["pace"] == {"AAA": 17.5, "BBB": 27.5}, str(context["pace"]))
    check("league_avg_pace averages across every team with pace data",
          context["league_avg_pace"] == 22.5, str(context["league_avg_pace"]))

    # --------------------------------------------------------------------
    # clients/nfl.py -- get_player_game_log() (real per-player, per-game
    # rows -- the piece MLB has via clients/mlb.get_player_game_log()
    # that NFL didn't have at all until this pass). Reuses the same fake
    # CSV/loader as the prior-season-context block above.
    # --------------------------------------------------------------------
    print("\nget_player_game_log(): real per-player, per-game rows")
    rb_log = asyncio.run(nfl.get_player_game_log("9001", 2099))
    check("get_player_game_log returns one row per REGULAR-SEASON game, excluding preseason",
          len(rb_log) == 2, str(rb_log))
    check("get_player_game_log's rows are sorted by week",
          [g["week"] for g in rb_log] == [1, 2], str([g["week"] for g in rb_log]))
    check("get_player_game_log's rows have already-numeric stats, ready for "
          "nfl_dk_points.game_points() with no further parsing",
          rb_log[0]["rushing_yards"] == 100.0 and isinstance(rb_log[0]["rushing_yards"], float),
          str(rb_log[0]))
    check("get_player_game_log reads the real team field (the live release's 'team' column, "
          "not the deprecated release's 'recent_team')",
          rb_log[0]["team"] == "AAA" and rb_log[0]["opponent_team"] == "BBB",
          str(rb_log[0]))
    check("get_player_game_log's rows feed nfl_dk_points.game_points() directly and reproduce "
          "the same DK points get_prior_season_context() computed internally for this exact game "
          "(100 rushing yards + 1 TD + the 100-yard bonus == 19.0, matching the 12.0 average "
          "already confirmed above from this same game plus week 2's 5.0)",
          nfl_dk_points.game_points(rb_log[0]) == 19.0, str(nfl_dk_points.game_points(rb_log[0])))
    check("get_player_game_log filters to only the requested player_id, not every player in the file",
          all(g["player_id"] == "9001" for g in rb_log), str(rb_log))
    check("get_player_game_log returns an empty list for a player with no games logged this season",
          asyncio.run(nfl.get_player_game_log("no-such-player", 2099)) == [], "")

    # --------------------------------------------------------------------
    # salaries.py -- dk_id capture (needed since NFL has no separate
    # roster fetch; DK's own numeric id is the only stable player key)
    # --------------------------------------------------------------------
    print("\nDK salary CSV: numeric player id capture")
    csv_text = (
        "Position,Name + ID,Name,ID,Roster Position,Salary,Game Info,TeamAbbrev,AvgPointsPerGame\n"
        "QB,Test QB (555),Test QB,555,QB,6000,SEA@NE 09/09/2026 08:20PM ET,SEA,20\n"
    )
    rows = salaries.parse_dk_csv(csv_text)
    check("parse_dk_csv captures DK's own numeric player id as dk_id",
          rows[0]["dk_id"] == "555", str(rows[0]))

    print("\nTeam alias normalisation (nflverse codes vs DFS-site codes)")
    check("LAR (DFS sites) normalises to LA (nflverse's own Rams code)",
          player_match.normalize_team("LAR") == "LA")
    check("WAS (nflverse's own Washington code) normalises to WSH (DFS sites' code)",
          player_match.normalize_team("WAS") == "WSH")

    # --------------------------------------------------------------------
    # services/nfl_optimizer.py
    # --------------------------------------------------------------------
    print("\nLineup optimizer (DraftKings Classic NFL)")

    def player(id_, name, team, pos, salary, fpts, own=10.0, edge=None):
        p = {
            "dk_id": id_, "name": name, "salary": salary, "position": pos,
            "projection": {"fpts": fpts, "ownership_pct": own},
        }
        if edge is not None:
            # Shaped like nfl_scoring.score_player()'s return, which is
            # where a real slate's composite comes from.
            p["edge"] = {"score": 50.0, "composite": edge}
        return p

    team_a = [
        player("1", "QB_A", "SEA", "QB", 7000, 22),
        player("2", "RB_A1", "SEA", "RB", 7500, 18),
        player("3", "RB_A2", "SEA", "RB", 5000, 12),
        player("4", "WR_A1", "SEA", "WR", 7800, 19),
        player("5", "WR_A2", "SEA", "WR", 6000, 14),
        player("6", "WR_A3", "SEA", "WR", 4500, 10),
        player("7", "TE_A", "SEA", "TE", 4000, 9),
        player("8", "DST_A", "SEA", "DST", 2800, 8),
    ]
    team_b = [
        player("9", "QB_B", "NE", "QB", 6500, 20),
        player("10", "RB_B1", "NE", "RB", 6800, 16),
        player("11", "RB_B2", "NE", "RB", 4200, 10),
        player("12", "WR_B1", "NE", "WR", 6200, 15),
        player("13", "WR_B2", "NE", "WR", 4800, 11),
        player("14", "WR_B3", "NE", "WR", 3800, 9),
        player("15", "TE_B", "NE", "TE", 3500, 8),
        player("16", "DST_B", "NE", "DST", 2600, 7),
    ]
    slate = {"games": [{"home": {"abbrev": "SEA", "players": team_a}, "away": {"abbrev": "NE", "players": team_b}}]}

    pool = nfl_optimizer.build_player_pool(slate)
    check("build_player_pool flattens both teams' rosters",
          len(pool) == 16, str(len(pool)))

    # nfl_scoring computes a matchup multiplier, but the pool never
    # carried it -- so the contest field sampler's "high stakes" model
    # had nothing to tell a low-owned player in a GOOD spot from a
    # low-owned player in a bad one, and degraded to picking cheap
    # names (measured on MLB as WORSE than an ordinary field).
    _edge_slate = {
        "games": [
            {
                "home": {"abbrev": "SEA", "players": [
                    {**team_a[0], "edge": {"score": 70.0, "composite": 1.15}},
                    *team_a[1:],
                ]},
                "away": {"abbrev": "NE", "players": team_b},
            }
        ]
    }
    _edge_pool = nfl_optimizer.build_player_pool(_edge_slate)
    _qb = next(p for p in _edge_pool if p["name"] == "QB_A")
    check("the pool carries nfl_scoring's matchup multiplier as edge_composite, under the "
          "same name MLB's pool uses",
          _qb["edge_composite"] == 1.15, str(_qb.get("edge_composite")))
    check("a player with no computed edge carries None rather than a fabricated 1.0 -- the "
          "field sampler treats missing as neutral itself, and inventing a value here "
          "would hide how much of a slate is actually scored",
          next(p for p in _edge_pool if p["name"] == "RB_A1")["edge_composite"] is None)

    result = nfl_optimizer.generate_lineups(slate, num_lineups=1)
    lineup = result["lineups"][0]
    slot_counts = {slot: len(players) for slot, players in lineup["slots"].items()}
    check("optimizer fills every roster slot with the right counts",
          slot_counts == nfl_optimizer.SLOT_REQUIREMENTS, str(slot_counts))
    check("optimizer respects the $50,000 salary cap",
          lineup["salary_used"] <= nfl_optimizer.SALARY_CAP, str(lineup["salary_used"]))
    check("total_ownership_pct is reported",
          lineup["total_ownership_pct"] > 0, str(lineup["total_ownership_pct"]))

    def lineup_ids(lu):
        return {p["id"] for slot in lu["slots"].values() for p in slot}

    stacked = nfl_optimizer.generate_lineups(slate, num_lineups=1, qb_stack_min=2)["lineups"][0]
    qb = stacked["slots"]["QB"][0]
    same_team_catchers = [
        p for slot in ("WR", "TE", "FLEX") for p in stacked["slots"][slot] if p["team"] == qb["team"]
    ]
    check("qb_stack_min=2 puts at least 2 of the QB's own WR/TE in the lineup",
          len(same_team_catchers) >= 2, str((qb["team"], [p["name"] for p in same_team_catchers])))

    multi = nfl_optimizer.generate_lineups(slate, num_lineups=3, max_exposure_pct=50)
    check("multi-lineup: requested count satisfied when the pool supports it",
          len(multi["lineups"]) == 3, str(len(multi["lineups"])))
    id_sets = [lineup_ids(lu) for lu in multi["lineups"]]
    check("multi-lineup: every generated lineup is distinct",
          len(id_sets) == len(set(frozenset(s) for s in id_sets)), str(id_sets))
    max_count = max((e["count"] for e in multi["exposure"]), default=0)
    check("multi-lineup: 50% exposure cap over 3 lineups holds",
          max_count <= 2, str(multi["exposure"]))

    floored = nfl_optimizer.generate_lineups(slate, num_lineups=1, min_salary=48000)["lineups"][0]
    check("min_salary forces total spend up to the floor",
          floored["salary_used"] >= 48000, str(floored["salary_used"]))

    try:
        nfl_optimizer.generate_lineups(slate, num_lineups=0)
        check("optimizer rejects num_lineups < 1", False)
    except nfl_optimizer.OptimizerError:
        check("optimizer rejects num_lineups < 1", True)

    try:
        nfl_optimizer.generate_lineups(slate, qb_stack_min=5)
        check("optimizer rejects an out-of-range qb_stack_min", False)
    except nfl_optimizer.OptimizerError:
        check("optimizer rejects an out-of-range qb_stack_min", True)

    try:
        nfl_optimizer.generate_lineups(slate, locked_ids=["1"], excluded_ids=["1"])
        check("optimizer rejects locking and excluding the same player", False)
    except nfl_optimizer.OptimizerError:
        check("optimizer rejects locking and excluding the same player", True)

    locked_result = nfl_optimizer.generate_lineups(slate, num_lineups=2, locked_ids=["1"], max_exposure_pct=1)
    check("a locked player appears in every generated lineup, exempt from the exposure cap",
          all("1" in lineup_ids(lu) for lu in locked_result["lineups"]),
          str(len(locked_result["lineups"])))

    try:
        nfl_optimizer.generate_lineups({"games": []})
        check("optimizer raises OptimizerError on an empty pool", False)
    except nfl_optimizer.OptimizerError:
        check("optimizer raises OptimizerError on an empty pool", True)

    # --------------------------------------------------------------------
    # clients/rotowire_nfl.py -- live RotoWire NFL import (no manual CSV
    # needed), the NFL sibling of clients/rotowire.py's MLB version.
    # --------------------------------------------------------------------
    print("\nRotoWire live NFL projections import (clients/rotowire_nfl.py)")

    # Fixture shaped exactly like RotoWire's real slate-list.php response
    # (confirmed live against https://www.rotowire.com/daily/nfl/api/
    # slate-list.php and .../players.php while building this feature):
    # a real, observed preseason/offseason state where NO Classic slate
    # is flagged defaultSlate yet, alongside a Showdown slate (wrong
    # contest type, must be excluded) and a "Preseason" Classic slate
    # (real but not the main slate -- must be excluded in favor of "All").
    rw_nfl_slate_list_no_default = {
        "slates": [
            {"slateID": 9735, "contestType": "Classic", "slateName": "Preseason",
             "startDateOnly": "2026-08-27", "defaultSlate": False},
            {"slateID": 9690, "contestType": "Classic", "slateName": "All",
             "startDateOnly": "2026-09-13", "defaultSlate": False},
            {"slateID": 9738, "contestType": "Showdown", "slateName": "LAR @ LAC",
             "startDateOnly": "2026-08-27", "defaultSlate": False},
        ]
    }
    rw_nfl_slate = rotowire_nfl._pick_classic_slate(rw_nfl_slate_list_no_default)
    check("_pick_classic_slate falls back to the slate named 'All' when nothing is flagged "
          "defaultSlate yet (the real, observed preseason state) -- not the earlier-dated "
          "'Preseason' slate or the Showdown slate",
          rw_nfl_slate["slateID"] == 9690, str(rw_nfl_slate))

    # Once the season is under way, a real defaultSlate flag takes clear
    # priority over the "All" fallback, even if another Classic slate is
    # also confusingly named "All".
    rw_nfl_slate_list_with_default = {
        "slates": [
            {"slateID": 9600, "contestType": "Classic", "slateName": "All",
             "startDateOnly": "2026-09-06", "defaultSlate": False},
            {"slateID": 9690, "contestType": "Classic", "slateName": "All",
             "startDateOnly": "2026-09-13", "defaultSlate": True},
        ]
    }
    rw_nfl_slate_2 = rotowire_nfl._pick_classic_slate(rw_nfl_slate_list_with_default)
    check("_pick_classic_slate prefers a real defaultSlate flag over the 'All'-name fallback",
          rw_nfl_slate_2["slateID"] == 9690, str(rw_nfl_slate_2))

    try:
        rotowire_nfl._pick_classic_slate(
            {"slates": [s for s in rw_nfl_slate_list_no_default["slates"] if s["slateName"] != "All"]}
        )
        check("_pick_classic_slate raises a clear ApiError with no default AND no 'All'-named "
              "Classic slate either", False, "")
    except rotowire_nfl.ApiError:
        check("_pick_classic_slate raises a clear ApiError with no default AND no 'All'-named "
              "Classic slate either", True, "")

    # Fixture shaped exactly like RotoWire's real players.php response for
    # NFL (confirmed live -- genuinely different from MLB's: no `lineup`
    # field, `pos` already includes DK's own "FLEX" tag, and a team-
    # defense row's firstName/lastName is the city/nickname pair).
    rw_nfl_players_fixture = [
        {
            "firstName": "Jahmyr", "lastName": "Gibbs", "team": {"abbr": "det"},
            "pos": ["RB", "FLEX"], "pts": "22.39", "rostership": 49.2, "salary": 8000,
        },
        {
            "firstName": "Jacksonville", "lastName": "Jaguars", "team": {"abbr": "jax"},
            "pos": ["DST"], "pts": "8.54", "rostership": 6.7, "salary": 3400,
        },
        # No salary posted yet -- must be skipped, not crash or default to 0.
        {
            "firstName": "No", "lastName": "Salary", "team": {"abbr": "sea"},
            "pos": ["WR"], "pts": "10.0", "rostership": 5.0, "salary": None,
        },
        # No name at all -- must be skipped.
        {
            "firstName": "", "lastName": "", "team": {"abbr": "sea"},
            "pos": ["WR"], "pts": "5.0", "rostership": 1.0, "salary": 3000,
        },
    ]
    rw_nfl_rows = rotowire_nfl._parse_players(rw_nfl_players_fixture)
    check("_parse_players skips rows with no salary and no name posted yet, keeping only the "
          "2 real ones",
          len(rw_nfl_rows) == 2, str(rw_nfl_rows))
    check("_parse_players joins firstName/lastName into a full name and uppercases the team abbrev",
          rw_nfl_rows[0] == {
              "name": "Jahmyr Gibbs", "normalized_name": player_match.normalize_name("Jahmyr Gibbs"),
              "team": "DET", "position": "RB/FLEX", "fpts": 22.39, "ownership_pct": 49.2,
              "salary": 8000, "lineup_spot": None,
          },
          str(rw_nfl_rows[0]))
    check("_parse_players carries a team-defense row's city/nickname through as its 'name', "
          "same as a real DK salary export would",
          rw_nfl_rows[1]["name"] == "Jacksonville Jaguars" and rw_nfl_rows[1]["position"] == "DST",
          str(rw_nfl_rows[1]))
    check("_parse_players always reports lineup_spot=None -- NFL has no batting-order equivalent, "
          "unlike MLB's real RotoWire import",
          all(r["lineup_spot"] is None for r in rw_nfl_rows), str(rw_nfl_rows))
    check("_parse_players returns an empty list for an empty payload, not a crash",
          rotowire_nfl._parse_players([]) == [] and rotowire_nfl._parse_players(None) == [], "")

    # --------------------------------------------------------------------
    # services/nfl_variance.py -- per-player/DST outcome pools + Monte
    # Carlo simulation, built from real 2025 game logs. The NFL sibling
    # of MLB's variance.py -- see that module's own tests for the same
    # underlying pattern (bootstrap pools, thin-sample position-pool
    # blending) this mirrors.
    # --------------------------------------------------------------------
    print("\nPer-player/DST outcome distribution pools (nfl_variance.py)")

    import statistics as statistics_module

    VARIANCE_SEASON = 2098

    def _qb_game(passing_yards, passing_tds, ints=0.0):
        return {
            "attempts": 30.0, "carries": 0.0, "targets": 0.0,
            "passing_yards": passing_yards, "passing_tds": passing_tds, "passing_interceptions": ints,
            "rushing_yards": 0.0, "rushing_tds": 0.0,
            "receptions": 0.0, "receiving_yards": 0.0, "receiving_tds": 0.0,
            "sack_fumbles_lost": 0.0, "rushing_fumbles_lost": 0.0, "receiving_fumbles_lost": 0.0,
            "passing_2pt_conversions": 0.0, "rushing_2pt_conversions": 0.0, "receiving_2pt_conversions": 0.0,
            "special_teams_tds": 0.0,
        }

    def _wr_game(receiving_yards, receptions=5.0):
        return {
            "attempts": 0.0, "carries": 0.0, "targets": receptions + 2,
            "passing_yards": 0.0, "passing_tds": 0.0, "passing_interceptions": 0.0,
            "rushing_yards": 0.0, "rushing_tds": 0.0,
            "receptions": receptions, "receiving_yards": receiving_yards, "receiving_tds": 0.0,
            "sack_fumbles_lost": 0.0, "rushing_fumbles_lost": 0.0, "receiving_fumbles_lost": 0.0,
            "passing_2pt_conversions": 0.0, "rushing_2pt_conversions": 0.0, "receiving_2pt_conversions": 0.0,
            "special_teams_tds": 0.0,
        }

    # 16 games each (comfortably above MIN_GAMES_FULL_TRUST["QB"]=12),
    # same season-average DK points, deliberately different game-to-game
    # spread -- isolates variance-capturing from everything else.
    consistent_qb_games = [_qb_game(250.0, 2.0)] * 16  # DK pts = 18.0 every game
    boom_bust_qb_games = [_qb_game(100.0, 0.0, 2.0), _qb_game(400.0, 4.0)] * 8  # DK pts in {2.0, 35.0}
    thin_wr_games = [_wr_game(60.0)] * 2  # only 2 games, well below MIN_GAMES_FULL_TRUST["WR"]=10

    boom_bust_wr_games = [_wr_game(10.0, receptions=1.0), _wr_game(150.0, receptions=9.0)] * 8

    nfl_variance_logs = {
        "80001": consistent_qb_games,
        "80002": boom_bust_qb_games,
        "80003": thin_wr_games,
        "80005": consistent_qb_games,
        "80006": boom_bust_wr_games,
    }

    async def fake_nfl_player_game_log(player_id, season, *, force=False):
        return nfl_variance_logs.get(player_id, [])

    nfl.get_player_game_log = fake_nfl_player_game_log

    consistent_qb_pool = asyncio.run(nfl_variance.player_outcome_pool("80001", "QB", VARIANCE_SEASON, seed=1))
    boom_bust_qb_pool = asyncio.run(nfl_variance.player_outcome_pool("80002", "QB", VARIANCE_SEASON, seed=1))

    check("player_outcome_pool returns POOL_SIZE values",
          len(consistent_qb_pool) == nfl_variance.POOL_SIZE, str(len(consistent_qb_pool)))
    check("both QB pools have roughly the same mean (same underlying season average)",
          abs(statistics_module.mean(consistent_qb_pool) - statistics_module.mean(boom_bust_qb_pool)) < 1.5,
          str((statistics_module.mean(consistent_qb_pool), statistics_module.mean(boom_bust_qb_pool))))
    check("the boom/bust QB's pool has genuinely higher variance than the consistent QB's",
          statistics_module.pstdev(boom_bust_qb_pool) > 3 * statistics_module.pstdev(consistent_qb_pool),
          str((statistics_module.pstdev(consistent_qb_pool), statistics_module.pstdev(boom_bust_qb_pool))))
    check("a QB with games well above MIN_GAMES_FULL_TRUST draws entirely from his own history",
          set(consistent_qb_pool) == {18.0}, str(set(consistent_qb_pool)))

    # By now the shared "WR" position pool hasn't been warmed up by
    # anything (no WR queried yet) -- a thin-sample WR should still get
    # a real, non-empty pool by falling back to his own (sparse) games.
    thin_wr_pool = asyncio.run(nfl_variance.player_outcome_pool("80003", "WR", VARIANCE_SEASON, seed=2))
    check("a thin-sample WR with no warmed-up shared pool yet still gets a real pool from his own "
          "sparse games rather than an empty result",
          len(thin_wr_pool) == nfl_variance.POOL_SIZE and set(thin_wr_pool) == {11.0}, str(set(thin_wr_pool)))

    # Warm up the shared QB position pool with the two QBs already
    # queried above, THEN check that a brand-new thin-sample QB blends
    # in values beyond his own tiny sample.
    warm_thin_qb_games = [_qb_game(250.0, 2.0)] * 2  # 2 games, same as consistent (18.0 each)
    nfl_variance_logs["80004"] = warm_thin_qb_games
    thin_qb_pool = asyncio.run(nfl_variance.player_outcome_pool("80004", "QB", VARIANCE_SEASON, seed=3))
    check("a thin-sample QB's pool blends in values from the warmed-up shared position pool "
          "(includes the boom/bust QB's real 2.0/35.0 outcomes, not just his own 18.0)",
          not (set(thin_qb_pool) <= {18.0}), str(sorted(set(thin_qb_pool))[:10]))

    no_games_pool = asyncio.run(nfl_variance.player_outcome_pool("no-such-qb", "QB", VARIANCE_SEASON, seed=4))
    check("a player with zero games this season falls back to the shared position pool rather "
          "than an empty result",
          len(no_games_pool) > 0, str(len(no_games_pool)))

    first_call = asyncio.run(nfl_variance.player_outcome_pool("80005", "QB", VARIANCE_SEASON, seed=42))
    second_call = asyncio.run(nfl_variance.player_outcome_pool("80005", "QB", VARIANCE_SEASON, seed=7))
    check("player_outcome_pool is cached -- calling again returns the same pool, ignoring a new seed",
          first_call == second_call, str((len(first_call), len(second_call))))

    ceiling = nfl_variance.ceiling_from_pool(consistent_qb_pool, 0.9)
    check("ceiling_from_pool reads a real percentile from the pool",
          ceiling == 18.0, str(ceiling))
    check("ceiling_from_pool returns 0.0 for an empty pool rather than crashing",
          nfl_variance.ceiling_from_pool([]) == 0.0, "")

    print("\nDST outcome distribution pools (nfl_variance.py)")

    def _dst_game(pts):
        return {
            "def_sacks": 0.0, "def_interceptions": 0.0, "fumble_recovery_opp": 0.0,
            "def_tds": 0.0, "special_teams_tds": 0.0, "def_safeties": 0.0,
            "def_punt_blocks": 0.0, "def_pat_blocks": 0.0, "def_fg_blocks": 0.0,
            "points_allowed": {10.0: 0, 4.0: 10, 1.0: 20}.get(pts, 24),
        }

    nfl_team_logs = {
        "AAA": [_dst_game(10.0)] * 16,  # comfortably above MIN_GAMES_FULL_TRUST["DST"]=14
        "CCC": [_dst_game(10.0)] * 2,  # thin sample
    }

    async def fake_nfl_team_game_log(team, season, *, force=False):
        return nfl_team_logs.get(team, [])

    nfl.get_team_game_log = fake_nfl_team_game_log

    dst_pool_full = asyncio.run(nfl_variance.dst_outcome_pool("AAA", VARIANCE_SEASON, seed=1))
    check("dst_outcome_pool returns POOL_SIZE values",
          len(dst_pool_full) == nfl_variance.POOL_SIZE, str(len(dst_pool_full)))
    check("a DST with games well above its own MIN_GAMES_FULL_TRUST draws entirely from its own "
          "real history",
          set(dst_pool_full) == {10.0}, str(set(dst_pool_full)))

    dst_pool_thin = asyncio.run(nfl_variance.dst_outcome_pool("CCC", VARIANCE_SEASON, seed=2))
    check("a thin-sample DST's pool blends in values from the warmed-up shared league DST pool",
          len(dst_pool_thin) == nfl_variance.POOL_SIZE, str(len(dst_pool_thin)))

    dst_no_games = asyncio.run(nfl_variance.dst_outcome_pool("no-such-team", VARIANCE_SEASON, seed=3))
    check("a team with zero games logged falls back to the shared league DST pool",
          len(dst_no_games) > 0, str(len(dst_no_games)))

    print("\nMonte Carlo simulation engine (nfl_variance.simulate_batch)")

    def _lineup(qb_id, wr_id, qb_team, wr_team, wr_opponent):
        return {
            "slots": {
                "QB": [{"id": qb_id, "team": qb_team, "opponent": wr_opponent, "position": "QB"}],
                "WR": [{"id": wr_id, "team": wr_team, "opponent": wr_opponent, "position": "WR"}],
            }
        }

    # Both pools need REAL variance of their own for this to test anything
    # meaningful -- a QB tied to a constant (zero-variance) pool would
    # show identical combined variance whether "stacked" or not, since a
    # shared team multiplier can only correlate outcomes that actually
    # move. boom_bust_qb_pool/boom_bust_wr_pool both have real spread.
    boom_bust_wr_pool = asyncio.run(nfl_variance.player_outcome_pool("80006", "WR", VARIANCE_SEASON, seed=1))
    sim_pools = {
        "stack_qb": boom_bust_qb_pool, "stack_wr": boom_bust_wr_pool,
        "solo_qb": boom_bust_qb_pool, "solo_wr": boom_bust_wr_pool,
    }
    stacked_lineup = _lineup("stack_qb", "stack_wr", "STK", "STK", "OPP")
    unstacked_lineup = _lineup("solo_qb", "solo_wr", "STK", "OTH", "OPP")

    stacked_sim = nfl_variance.simulate_batch([stacked_lineup], sim_pools, num_trials=4000, seed=11)
    unstacked_sim = nfl_variance.simulate_batch([unstacked_lineup], sim_pools, num_trials=4000, seed=11)
    check("simulate_batch returns a (len(entries), num_trials) array",
          stacked_sim.shape == (1, 4000), str(stacked_sim.shape))
    check("a real QB+his-own-WR stack shows genuinely higher combined variance than the same two "
          "pools with no shared team (the whole reason NFL stacking correlation exists to model). "
          "The bar is 1.08x -- the REAL measured QB<->WR correlation of 0.355 implies a "
          "sqrt(1+rho) ~ 1.16x ceiling for equal spreads, and the old 1.15x bar was only "
          "reachable because the sim over-correlated the pair at 0.56",
          statistics_module.pstdev(stacked_sim[0].tolist()) > 1.08 * statistics_module.pstdev(unstacked_sim[0].tolist()),
          str((statistics_module.pstdev(stacked_sim[0].tolist()), statistics_module.pstdev(unstacked_sim[0].tolist()))))

    dst_lineup = {"slots": {"DST": [{"id": "aaa_dst", "team": "AAA", "opponent": "OPP", "position": "DST"}]}}
    dst_sim = nfl_variance.simulate_batch([dst_lineup], {"aaa_dst": dst_pool_full}, num_trials=500, seed=5)
    check("simulate_batch runs a DST-only lineup without needing a team multiplier for its own "
          "team (a DST reacts to its OPPONENT's day, not its own)",
          dst_sim.shape == (1, 500), str(dst_sim.shape))

    try:
        nfl_variance.simulate_batch([stacked_lineup], {}, num_trials=10, seed=1)
        check("simulate_batch raises ValueError when a player's pool is missing", False, "")
    except ValueError:
        check("simulate_batch raises ValueError when a player's pool is missing", True, "")

    pools_for_entries = asyncio.run(nfl_variance.player_pools_for_entries([stacked_lineup, dst_lineup], VARIANCE_SEASON))
    check("player_pools_for_entries fetches a pool for every unique player across a batch of "
          "lineups, dispatching offensive players to player_outcome_pool and DST to "
          "dst_outcome_pool",
          set(pools_for_entries) == {"stack_qb", "stack_wr", "aaa_dst"}, str(sorted(pools_for_entries)))

    # --------------------------------------------------------------------
    # services/nfl_contest.py -- the NFL contest generator + Monte Carlo
    # simulator, tied to nfl_variance.py the same way contest.py (MLB)
    # is tied to variance.py. Reuses the 16-player SEA/NE slate fixture
    # already built for the optimizer tests above.
    # --------------------------------------------------------------------
    print("\nContest generator + Monte Carlo simulator (nfl_contest.py)")

    try:
        asyncio.run(nfl_contest.build_contest_entries(slate, "not_a_real_type", 3, season=2098))
        check("build_contest_entries raises ContestError on an unknown contest_type", False, "")
    except nfl_contest.ContestError:
        check("build_contest_entries raises ContestError on an unknown contest_type", True, "")

    det = asyncio.run(nfl_contest.build_contest_entries(slate, "double_up", 2, season=2098, allow_duplicates=True))
    check("build_contest_entries (deterministic) builds real entries against the double_up preset",
          det["num_entries_built"] >= 1 and det["contest"]["field_size"] == 100, str(det["contest"]))
    check("build_contest_entries's summary reports real cash-rate/payout economics",
          "cashing_pct" in det["summary"] and "avg_roi_pct" in det["summary"], str(det["summary"]))
    check("build_contest_entries's entries each carry a real duplicate_count",
          all("duplicate_count" in e for e in det["entries"]), "")

    # --- salary floor -------------------------------------------------
    # The reported bug: NFL defaulted min_salary to 0 at EVERY layer
    # (nfl_contest's signatures, the router's Body defaults, the panel's
    # own input), so a real generated batch came back with 23% of
    # entries under $47,000 -- worst at $40,500, nearly $10k unspent.
    check("nfl_optimizer exposes a DEFAULT_MIN_SALARY of $47,000, matching MLB's own",
          nfl_optimizer.DEFAULT_MIN_SALARY == 47_000, str(nfl_optimizer.DEFAULT_MIN_SALARY))
    check("every public NFL generator defaults its salary floor to it, rather than 0 -- the "
          "actual root cause of the under-cap lineups",
          inspect_module.signature(nfl_contest.generate_entries).parameters["min_salary"].default
          == nfl_optimizer.DEFAULT_MIN_SALARY
          and inspect_module.signature(nfl_contest.generate_field).parameters["min_salary"].default
          == nfl_optimizer.DEFAULT_MIN_SALARY, "")
    check("the low-level _sample_one_lineup primitive stays policy-neutral (0) -- the floor is "
          "a decision the public generators make, not the sampler",
          inspect_module.signature(nfl_contest._sample_one_lineup).parameters["min_salary"].default == 0, "")

    floored = nfl_contest.generate_entries(slate, 12, min_salary=44000, allow_duplicates=True, seed=4)
    check("every generated entry respects the salary floor it was given",
          all(e["salary_used"] >= 44000 for e in floored),
          str(sorted(e["salary_used"] for e in floored)[:3]))

    floored_field = nfl_contest.generate_field(slate, 12, min_salary=44000, seed=4)
    check("the sampled opponent FIELD respects the floor too -- a field of under-cap lineups "
          "would be an unrealistically weak benchmark to measure entries against",
          all(lu["salary_used"] >= 44000 for lu in floored_field),
          str(sorted(lu["salary_used"] for lu in floored_field)[:3]))

    check("passing min_salary=0 still disables the floor entirely, so the old behavior is "
          "one explicit argument away",
          all(e["salary_used"] <= nfl_optimizer.SALARY_CAP
              for e in nfl_contest.generate_entries(slate, 4, min_salary=0, allow_duplicates=True, seed=4)), "")

    # Salary pacing: a lineup drifting cheap pulls itself back toward the
    # floor DURING the walk, rather than being built and then rejected --
    # the hard reachability prune alone is only a necessary condition and
    # can't bite until a walk is already doomed (measured: adding the
    # floor without pacing dropped a 300-entry request to 5 built).
    _paced = nfl_contest.generate_entries(slate, 20, min_salary=44000, allow_duplicates=True, seed=11)
    _saved_pacing = nfl_contest._SALARY_PACING_STRENGTH
    nfl_contest._SALARY_PACING_STRENGTH = 0.0
    _unpaced = nfl_contest.generate_entries(slate, 20, min_salary=0, allow_duplicates=True, seed=11)
    nfl_contest._SALARY_PACING_STRENGTH = _saved_pacing
    check("salary pacing raises the median salary actually used versus an unpaced, unfloored "
          "build -- 'use as much of the cap as possible', measurably",
          sorted(e["salary_used"] for e in _paced)[len(_paced) // 2]
          > sorted(e["salary_used"] for e in _unpaced)[len(_unpaced) // 2],
          str((sorted(e["salary_used"] for e in _paced)[len(_paced) // 2],
               sorted(e["salary_used"] for e in _unpaced)[len(_unpaced) // 2])))

    no_dupes = nfl_contest.generate_entries(slate, 2, allow_duplicates=False, seed=1)
    dupes_allowed = nfl_contest.generate_entries(slate, 2, allow_duplicates=True, seed=1)
    no_dupes_sigs = {frozenset(p["id"] for p in e["players"]) for e in no_dupes}
    check("generate_entries without allow_duplicates never returns two identical lineups",
          len(no_dupes_sigs) == len(no_dupes), str(len(no_dupes)))
    check("generate_entries with allow_duplicates=True can return exact repeats with the same seed",
          len(dupes_allowed) == 2, str(len(dupes_allowed)))

    sim = asyncio.run(nfl_contest.build_contest_entries_simulated(
        slate, "gpp_small", 3, season=2098, num_trials=300, allow_duplicates=True,
        self_play=False, field_sharpness="marquee",
    ))
    check("build_contest_entries_simulated (self_play=False) ranks the batch against a "
          "separately-sampled field and returns real per-entry simulated results",
          sim["self_play"] is False and len(sim["results"]) == sim["num_entries_built"],
          str((sim["self_play"], len(sim["results"]), sim["num_entries_built"])))
    check("every simulated result carries a real cash_probability_pct and roi_pct",
          all("cash_probability_pct" in r and "roi_pct" in r for r in sim["results"]), "")
    # Ranked by top-1% rate with ROI as the tiebreak, NOT raw ROI --
    # in a top-heavy GPP, per-lineup ROI is dominated by rare
    # first-place hits, so sorting by it ranks lineups substantially by
    # which ones got lucky in this run's draws. Same ordering the MLB
    # side already uses.
    check("build_contest_entries_simulated's entries are ranked by ROI, highest first, with "
          "top-1% rate as the tiebreak -- same ordering the MLB side uses",
          all((-sim["results"][i]["roi_pct"], -sim["results"][i]["top_1pct_pct"])
              <= (-sim["results"][i + 1]["roi_pct"], -sim["results"][i + 1]["top_1pct_pct"])
              for i in range(len(sim["results"]) - 1)),
          str([(r["roi_pct"], r["top_1pct_pct"]) for r in sim["results"]]))
    check("first_place_pct defaults to the gpp_small preset's own value (15.0) when not overridden",
          sim["first_place_pct"] == 15.0, str(sim["first_place_pct"]))

    self_play_sim = asyncio.run(nfl_contest.build_contest_entries_simulated(
        slate, "gpp_small", 3, season=2098, num_trials=300, allow_duplicates=True,
        self_play=True, first_place_pct=25.0,
    ))
    check("self_play=True ranks the batch against ITSELF (no separate field) and reports it",
          self_play_sim["self_play"] is True, "")
    check("first_place_pct override is echoed back exactly when given",
          self_play_sim["first_place_pct"] == 25.0, str(self_play_sim["first_place_pct"]))

    check("field_baseline reports the contest's closed-form zero-skill cash rate and ROI",
          sim["field_baseline"]["avg_cash_probability_pct"] == 20.0, str(sim["field_baseline"]))

    check("every real generated entry carries a primary_stack type and a has_bringback boolean",
          all("primary_stack" in e and isinstance(e["has_bringback"], bool) for e in sim["entries"]),
          str(sim["entries"][0]))

    print("\nNFL contest generator/simulator split + deterministic seeding")

    # Contest size is ONE control now: it IS the field size and it IS
    # how many lineups get built. NFL reuses contest.py's CONTEST_TYPES,
    # so it inherits the same real per-preset size tiers.
    check("every NFL contest preset advertises the real sizes it comes in (shared with MLB)",
          all(c.get("sizes") and c["field_size"] in c["sizes"]
              for c in nfl_contest.CONTEST_TYPES.values()),
          str({k: c.get("sizes") for k, c in nfl_contest.CONTEST_TYPES.items()}))

    # The NFL generator built its "contest" with generate_entries --
    # the projected-points model for lineups YOU would enter -- so field
    # sharpness could not affect it at all. Same correction MLB got: the
    # opponents are built with the ownership model now.
    # `slate` here has flat 10% ownership on every player, so the three
    # sharpness levels would be indistinguishable on it. Give the field
    # a real ownership curve -- chalk concentrated on a handful -- or the
    # check proves nothing.
    sharp_slate = copy.deepcopy(slate)
    for _g in sharp_slate["games"]:
        for _side in ("home", "away"):
            for _i, _p in enumerate(_g[_side]["players"]):
                _p["projection"]["ownership_pct"] = 35.0 if _i % 4 == 0 else 3.0

    def _sharp_own(level):
        b = asyncio.run(nfl_contest.build_contest_lineups(
            sharp_slate, "gpp_large", 120, season=2098, seed=9, field_sharpness=level))
        return sum(e["total_ownership_pct"] for e in b["entries"]) / len(b["entries"]), b

    _low_own, _low_b = _sharp_own("low")
    _mar_own, _ = _sharp_own("marquee")
    _high_own, _ = _sharp_own("high")
    check("field sharpness now changes the NFL contest the generator BUILDS, in the "
          "direction real fields run -- a cheap contest is the chalkiest, high stakes the "
          "least",
          _low_own > _mar_own > _high_own,
          f"low {_low_own:.1f}% > marquee {_mar_own:.1f}% > high {_high_own:.1f}%")
    check("and the NFL batch records what its opponents were built to be",
          _low_b["field_sharpness"] == "low", str(_low_b.get("field_sharpness")))

    built = asyncio.run(nfl_contest.build_contest_lineups(slate, "gpp_small", 6, season=2098, seed=5))
    check("build_contest_lineups builds exactly as many lineups as the contest holds -- the two "
          "numbers are the same thing now",
          built["num_entries_built"] == 6 == built["field_size"],
          str((built["num_entries_built"], built["field_size"])))
    check("build_contest_lineups returns NO economics at all -- no opponent field, no payout "
          "curve, no ROI; that's the simulator's job on the batch it produces",
          not any(k in built for k in ("results", "prize_pool", "paid_count", "field_baseline")),
          str(sorted(built)))
    check("build_contest_lineups describes what it built instead: salary, points, ownership and "
          "the NFL stack archetypes the contest actually came out with",
          built["summary"]["median_salary_used"] > 0
          and sum(x["count"] for x in built["stack_shapes"]) == 6,
          str(built["summary"]))

    # SEEDING -- NFL previously passed seed=None everywhere, so every
    # click produced a different contest and nothing was reproducible.
    same = asyncio.run(nfl_contest.build_contest_lineups(slate, "gpp_small", 6, season=2098, seed=5))
    other = asyncio.run(nfl_contest.build_contest_lineups(slate, "gpp_small", 6, season=2098, seed=6))
    check("the same seed reproduces the identical contest -- NFL had no seeding at all before, "
          "so identical settings reshuffled on every click",
          [e["players"] for e in built["entries"]] == [e["players"] for e in same["entries"]], "")
    check("...and a different seed genuinely draws a different one",
          [e["players"] for e in built["entries"]] != [e["players"] for e in other["entries"]], "")

    # A contest larger than the build cap keeps its REAL field size.
    _saved_max = nfl_contest.MAX_USER_LINEUPS
    nfl_contest.MAX_USER_LINEUPS = 4
    try:
        capped = asyncio.run(nfl_contest.build_contest_lineups(slate, "gpp_small", 100, season=2098, seed=5))
    finally:
        nfl_contest.MAX_USER_LINEUPS = _saved_max
    check("a contest larger than the build cap keeps its REAL field_size while the build itself "
          "is capped -- reported as two separate numbers, not silently conflated",
          capped["field_size"] == 100 and capped["num_entries_built"] == 4,
          str((capped["field_size"], capped["num_entries_built"])))

    try:
        asyncio.run(nfl_contest.build_contest_lineups(slate, "gpp_small", 0, season=2098))
        check("build_contest_lineups rejects a contest_size of 0", False)
    except nfl_contest.ContestError:
        check("build_contest_lineups rejects a contest_size of 0", True)

    priced = asyncio.run(nfl_contest.simulate_contest_batch(
        built["entries"], built["contest"], season=2098, contest_type="gpp_small",
        num_trials=300, entry_fee=10.0, seed=3,
    ))
    check("simulate_contest_batch prices an already-built contest without rebuilding a lineup",
          priced["num_entries_built"] == len(built["entries"])
          and len(priced["results"]) == len(built["entries"]),
          str((priced["num_entries_built"], len(priced["results"]))))
    check("simulate_contest_batch defaults to ranking the contest against ITSELF -- the generator "
          "builds the whole field, so there's no second population to invent",
          priced["self_play"] is True, str(priced["self_play"]))

    dearer = asyncio.run(nfl_contest.simulate_contest_batch(
        built["entries"], built["contest"], season=2098, contest_type="gpp_small",
        num_trials=300, entry_fee=20.0, seed=3,
    ))
    check("the entry cost given to the simulator sets the prize pool -- doubling the fee doubles "
          "the pool, which is why it's a simulator input and not a build one",
          abs(dearer["prize_pool"] - 2 * priced["prize_pool"]) < 0.02,
          str((priced["prize_pool"], dearer["prize_pool"])))
    check("...and it's the fee the results are actually priced against, not the preset's own",
          priced["contest"]["entry_fee"] == 10.0 and dearer["contest"]["entry_fee"] == 20.0, "")

    try:
        asyncio.run(nfl_contest.simulate_contest_batch([], built["contest"], season=2098))
        check("simulate_contest_batch refuses an empty batch rather than simulating nothing", False)
    except nfl_contest.ContestError:
        check("simulate_contest_batch refuses an empty batch rather than simulating nothing", True)

    # Salary pacing is CAP-driven now, not floor-driven. It used to be
    # gated on min_salary being non-zero, which made the whole mechanism
    # dead code the moment the floor was removed -- measured on a real
    # Week 1 slate, every strength from 0 to 10 produced the identical
    # batch at min_salary=0.
    _saved_pace = nfl_contest._SALARY_PACING_STRENGTH
    nfl_contest._SALARY_PACING_STRENGTH = 0.0
    try:
        _unpaced = nfl_contest.generate_entries(slate, 10, min_salary=0, allow_duplicates=True, seed=21)
    finally:
        nfl_contest._SALARY_PACING_STRENGTH = _saved_pace
    _paced = nfl_contest.generate_entries(slate, 10, min_salary=0, allow_duplicates=True, seed=21)
    _med = lambda es: sorted(e["salary_used"] for e in es)[len(es) // 2]
    check("salary pacing still works with NO floor at all -- it paces against the CAP now, so "
          "removing the floor doesn't silently disable it",
          _med(_paced) > _med(_unpaced), str((_med(_paced), _med(_unpaced))))

    print("\nStack archetypes (nfl_contest.py's _classify_pool/_pick_primary/_pick_secondary_teams)")

    def _qb_rush_game(carries):
        return {"attempts": 25.0, "carries": carries, "targets": 0.0}

    def _rb_target_game(targets):
        return {"attempts": 0.0, "carries": 12.0, "targets": targets}

    # Real running QB (real 2025 example threshold, 5.0+ carries/game):
    # 6 games averaging 7 carries. A pure pocket passer stays under it.
    nfl_variance_logs["1"] = [_qb_rush_game(7.0)] * 6  # QB_A (SEA) -- running
    nfl_variance_logs["9"] = [_qb_rush_game(1.5)] * 6  # QB_B (NE) -- pocket passer
    # A real receiving-threat RB (3.5+ targets/game) vs. a between-the-
    # tackles runner who stays under it.
    nfl_variance_logs["2"] = [_rb_target_game(5.0)] * 6  # RB_A1 (SEA) -- pass-catcher
    nfl_variance_logs["3"] = [_rb_target_game(1.0)] * 6  # RB_A2 (SEA) -- pure runner

    candidates_by_slot_for_test = {
        slot: [p for p in nfl_contest.build_player_pool(slate) if slot in p["slots"]]
        for slot in nfl_contest.SLOT_TYPES
    }

    # _classify_pool() reads clients/nfl.get_grouped_season_stats()
    # directly (not per-player get_player_game_log() calls) for real
    # bulk-performance reasons -- see that function's own docstring --
    # so it needs its own fake here rather than reusing the earlier
    # get_player_game_log fake, which it now bypasses entirely.
    async def fake_grouped_season_stats(season, *, force=False):
        return dict(nfl_variance_logs)

    nfl.get_grouped_season_stats = fake_grouped_season_stats

    running_qb_ids, pass_catching_rb_ids = asyncio.run(
        nfl_contest._classify_pool(candidates_by_slot_for_test, 2098)
    )
    check("_classify_pool correctly identifies a real running QB from real rushing volume",
          "1" in running_qb_ids, str(running_qb_ids))
    check("_classify_pool correctly excludes a pocket passer from the running-QB set",
          "9" not in running_qb_ids, str(running_qb_ids))
    check("_classify_pool correctly identifies a real pass-catching RB from real target volume",
          "2" in pass_catching_rb_ids, str(pass_catching_rb_ids))
    check("_classify_pool correctly excludes a between-the-tackles runner from the pass-catching set",
          "3" not in pass_catching_rb_ids, str(pass_catching_rb_ids))

    rng = random.Random(3)
    primary_types_seen = set()
    for _ in range(50):
        p = nfl_contest._pick_primary(candidates_by_slot_for_test, running_qb_ids, nfl_contest._fpts_weight, rng)
        if p:
            primary_types_seen.add(p["type"])
    check("_pick_primary produces a real mix of primary stack types across many draws, not just one",
          len(primary_types_seen) >= 3, str(primary_types_seen))

    naked_qb = nfl_contest._pick_primary(
        candidates_by_slot_for_test, frozenset(), nfl_contest._fpts_weight, random.Random(1)
    )
    check("_pick_primary never selects qb_naked when no real running QB is known (empty running_qb_ids)",
          all(
              nfl_contest._pick_primary(
                  candidates_by_slot_for_test, frozenset(), nfl_contest._fpts_weight, random.Random(i)
              )["type"] != "qb_naked"
              for i in range(20)
          ),
          "")

    # This fixture only has 2 teams (SEA/NE) -- a [2, 1] shape needing
    # TWO distinct non-primary teams isn't satisfiable with only one
    # (NE) available, so this uses a single-group [2] shape instead,
    # which is.
    secondary = nfl_contest._pick_secondary_teams(
        candidates_by_slot_for_test, "SEA", "NE", [2], nfl_contest._fpts_weight, random.Random(2)
    )
    check("_pick_secondary_teams biases the first group toward the given bring-back team when it "
          "has enough eligible players",
          secondary is not None and secondary[0][0] == "NE", str(secondary))
    check("_pick_secondary_teams never assigns the primary's own team to a secondary group",
          secondary is not None and all(t != "SEA" for t, _ in secondary), str(secondary))
    check("_pick_secondary_teams returns None when a shape needs more distinct non-primary teams "
          "than the pool actually has (this fixture has only SEA/NE -- a [2, 1] shape needs two)",
          nfl_contest._pick_secondary_teams(
              candidates_by_slot_for_test, "SEA", None, [2, 1], nfl_contest._fpts_weight, random.Random(2)
          ) is None,
          "")

    print("\nPlay-by-play PROE (clients/nfl_pbp.py) -- real xpass-based pass rate over expectation")

    neutral_row = {
        "posteam": "AAA", "defteam": "BBB", "down": 2.0, "pass_oe": 10.0, "wp": 0.5,
        "qb_spike": False, "qb_kneel": False, "half_seconds_remaining": 900.0,
    }
    check("a normal down-2, mid-wp, non-two-minute play is neutral script",
          nfl_pbp._is_neutral_script(neutral_row))
    check("a qb_spike play is excluded from neutral script",
          not nfl_pbp._is_neutral_script({**neutral_row, "qb_spike": True}))
    check("a qb_kneel play is excluded from neutral script",
          not nfl_pbp._is_neutral_script({**neutral_row, "qb_kneel": True}))
    check("a 4th-down play is excluded from neutral script (go/punt/FG, not a pass-vs-run call)",
          not nfl_pbp._is_neutral_script({**neutral_row, "down": 4.0}))
    check("a garbage-time play (wp near 1) is excluded from neutral script",
          not nfl_pbp._is_neutral_script({**neutral_row, "wp": 0.95}))
    check("a two-minute-drill play is excluded from neutral script even at a neutral wp",
          not nfl_pbp._is_neutral_script({**neutral_row, "half_seconds_remaining": 60.0}))
    check("a play with no pass_oe (model couldn't score it) is excluded",
          not nfl_pbp._is_neutral_script({**neutral_row, "pass_oe": None}))

    async def fake_pbp_rows(season):
        # AAA passes a lot more than expected on neutral-script plays;
        # BBB's defense allows a lot more passing than expected (a pass
        # funnel). One qb_kneel play per team proves it's excluded from
        # the aggregate rather than dragging it down.
        rows = []
        for _ in range(5):
            rows.append({"posteam": "AAA", "defteam": "CCC", "down": 1.0, "pass_oe": 20.0, "wp": 0.5,
                         "qb_spike": False, "qb_kneel": False, "half_seconds_remaining": 900.0})
        for _ in range(5):
            rows.append({"posteam": "CCC", "defteam": "AAA", "down": 1.0, "pass_oe": -5.0, "wp": 0.5,
                         "qb_spike": False, "qb_kneel": False, "half_seconds_remaining": 900.0})
        for _ in range(5):
            rows.append({"posteam": "DDD", "defteam": "BBB", "down": 1.0, "pass_oe": 15.0, "wp": 0.5,
                         "qb_spike": False, "qb_kneel": False, "half_seconds_remaining": 900.0})
        rows.append({"posteam": "AAA", "defteam": "CCC", "down": 1.0, "pass_oe": 999.0, "wp": 0.5,
                     "qb_spike": False, "qb_kneel": True, "half_seconds_remaining": 900.0})
        return rows

    nfl_pbp._load_pbp_rows = fake_pbp_rows
    proe = asyncio.run(nfl_pbp.get_team_proe(2099, force=True))
    check("get_team_proe averages a team's own offensive PROE over its neutral-script plays only",
          proe["AAA"]["off_proe"] == 20.0, str(proe.get("AAA")))
    check("get_team_proe correctly aggregates a team's defensive PROE-allowed from the opposing side's plays",
          proe["BBB"]["def_proe_allowed"] == 15.0, str(proe.get("BBB")))
    check("get_team_proe's off_plays_sampled excludes the qb_kneel play (5, not 6)",
          proe["AAA"]["off_plays_sampled"] == 5, str(proe.get("AAA")))

    print("\nEmpirical QB/pass-catcher correlation (nfl_correlations.py) -- real Pearson from game logs")

    _original_game_points = nfl_dk_points.game_points
    nfl_dk_points.game_points = lambda row: row["_test_pts"]

    def corr_row(team, week, opp, pos, pts):
        return {"week": week, "team": team, "opponent_team": opp, "position_group": pos, "_test_pts": pts}

    grouped_corr = {
        # AAA's top-ranked WR's points move in perfect lockstep with the
        # QB's; the second-ranked WR is flat every week (no signal at
        # all); the RB moves in perfect ANTI-lockstep.
        "qbA": [corr_row("AAA", 1, "BBB", "QB", 10.0), corr_row("AAA", 2, "CCC", "QB", 20.0),
                corr_row("AAA", 3, "BBB", "QB", 10.0), corr_row("AAA", 4, "CCC", "QB", 20.0)],
        "wrA1": [corr_row("AAA", 1, "BBB", "WR", 12.0), corr_row("AAA", 2, "CCC", "WR", 22.0),
                 corr_row("AAA", 3, "BBB", "WR", 12.0), corr_row("AAA", 4, "CCC", "WR", 22.0)],
        "wrA2": [corr_row("AAA", 1, "BBB", "WR", 5.0), corr_row("AAA", 2, "CCC", "WR", 5.0),
                 corr_row("AAA", 3, "BBB", "WR", 5.0), corr_row("AAA", 4, "CCC", "WR", 5.0)],
        "rbA1": [corr_row("AAA", 1, "BBB", "RB", 20.0), corr_row("AAA", 2, "CCC", "RB", 10.0),
                 corr_row("AAA", 3, "BBB", "RB", 20.0), corr_row("AAA", 4, "CCC", "RB", 10.0)],
        # BBB/CCC deliberately have no QB of their own in this fixture --
        # only their WR1 is needed (for AAA's bring-back pairing), and a
        # QB here would pool INTO the qb_wr1/qb_wr2/qb_rb1 aggregates
        # below (which are genuinely league-wide, not per-team), diluting
        # the clean signal this fixture is built to prove.
        "wrB1": [corr_row("BBB", 1, "AAA", "WR", 9.0), corr_row("BBB", 3, "AAA", "WR", 25.0)],
        "wrC1": [corr_row("CCC", 2, "AAA", "WR", 7.0), corr_row("CCC", 4, "AAA", "WR", 30.0)],
    }

    async def fake_grouped_for_corr(season, *, force=False):
        return grouped_corr

    nfl.get_grouped_season_stats = fake_grouped_for_corr
    corr = asyncio.run(nfl_correlations.get_league_correlations(2099, force=True))
    nfl_dk_points.game_points = _original_game_points

    check("qb_wr1 shows a strong positive correlation when the QB's own top-scoring WR's points track his",
          corr["qb_wr1"]["correlation"] is not None and corr["qb_wr1"]["correlation"] > 0.9,
          str(corr["qb_wr1"]))
    check("qb_wr2 (flat points every week, no real signal) shows a much weaker read than qb_wr1",
          corr["qb_wr2"]["correlation"] is None or corr["qb_wr2"]["correlation"] < corr["qb_wr1"]["correlation"],
          str((corr["qb_wr1"], corr["qb_wr2"])))
    check("qb_rb1 (inversely paired points) shows a negative correlation -- the real signal this feature "
          "exists to surface (QB-RB is a much weaker/wrong-direction pairing than QB-WR)",
          corr["qb_rb1"]["correlation"] is not None and corr["qb_rb1"]["correlation"] < 0,
          str(corr["qb_rb1"]))
    check("qb_bring_back_wr1 pairs the QB's points with whichever opponent's WR1 he actually faced that "
          "week, not a fixed player -- real paired games exist",
          corr["qb_bring_back_wr1"]["paired_games"] > 0, str(corr["qb_bring_back_wr1"]))

    print("\nNFL stack rating (nfl_stack_rating.py) -- Vegas + PROE + real correlation combined")

    tight = nfl_stack_rating._game_total_component(50.0, 3.5, True)
    wide = nfl_stack_rating._game_total_component(50.0, 10.0, True)
    check("the game-total component now scores the TOTAL only -- the same 50-pt total is worth the "
          "same whatever the spread, because closeness is game script's job and paying for it twice "
          "was worth ~+10 for one 3-pt spread",
          tight["value"] == wide["value"], str((tight["value"], wide["value"])))

    # --- game script: what the scoreboard does to passing volume -------
    script = nfl_stack_rating._game_script_component
    big_fav = script(10.0, True, 48.0)
    check("a big favourite is PENALISED -- going up two scores means running the clock, and the "
          "passing volume a stack is built on evaporates in the half it needs",
          big_fav["value"] < 0 and "blowout" in big_fav["detail"], str(big_fav))
    check("and the penalty grows with the spread rather than being a flat cliff",
          script(14.0, True, 48.0)["value"] < big_fav["value"] < script(7.0, True, 48.0)["value"],
          f"{script(14.0, True, 48.0)['value']} < {big_fav['value']} < {script(7.0, True, 48.0)['value']}")
    check("it is capped, so a 20-point spread doesn't swamp every other component",
          script(20.0, True, 48.0)["value"] == -nfl_stack_rating.BLOWOUT_MAX_PENALTY,
          str(script(20.0, True, 48.0)["value"]))
    check("a favourite just under the threshold is untouched -- 6.5 is not a blowout",
          script(6.5, True, 44.0)["value"] == 0.0, str(script(6.5, True, 44.0)))

    big_dog = script(10.0, False, 48.0)
    check("the same spread for the UNDERDOG is a bonus, not a penalty -- playing from behind forces "
          "the ball into the air",
          big_dog["value"] > 0, str(big_dog))
    check("...and it is smaller than the favourite's penalty, because trailing raises attempts but "
          "lowers efficiency",
          abs(big_dog["value"]) < abs(big_fav["value"]),
          f"dog {big_dog['value']} vs fav {big_fav['value']}")

    close_high = script(3.0, True, 49.0)
    close_low = script(3.0, True, 42.0)
    check("a close game is a bonus, and worth more when the total says both teams can score",
          close_high["value"] > close_low["value"] > 0,
          f"{close_high['value']} > {close_low['value']}")
    check("a pick'em counts as close -- it is the extreme case of a close game, not an edge case "
          "outside it",
          script(0.0, True, 52.0)["value"] == close_high["value"],
          str(script(0.0, True, 52.0)["value"]))
    check("the dead zone between close and blowout scores neither way",
          script(5.5, True, 48.0)["value"] == 0.0, str(script(5.5, True, 48.0)))
    check("no spread available scores nothing rather than guessing a script",
          script(None, None, 48.0)["value"] == 0.0)

    # --- leverage: how obvious the spot is ------------------------------
    crowd = nfl_stack_rating._crowding_component
    check("a high total carries a leverage PENALTY -- the spot the whole field can see costs "
          "duplication in a GPP",
          crowd(52.0)["value"] < 0, str(crowd(52.0)))
    check("an ordinary total carries none",
          crowd(43.0)["value"] == 0.0, str(crowd(43.0)))
    check("the crowding penalty is deliberately SMALLER than the total's own bonus, so a real "
          "shootout still rates well -- the answer to 'best spot, most obvious' is to shade away "
          "from it, not refuse to play it",
          abs(crowd(52.0)["value"])
          < nfl_stack_rating._game_total_component(52.0, 3.0, True)["value"],
          f"{crowd(52.0)['value']} vs {nfl_stack_rating._game_total_component(52.0, 3.0, True)['value']}")

    # The headline case, end to end through the arithmetic: a good total
    # no longer rescues a blowout script.
    def _net(spread, favored, total):
        return (
            nfl_stack_rating._game_total_component(total, spread, favored)["value"]
            + script(spread, favored, total)["value"]
            + crowd(total)["value"]
        )

    check("a 10-point favourite in a 48-total game now nets NEGATIVE, where the total alone would "
          "have made it one of the better spots on the board",
          _net(10.0, True, 48.0) < 0 < _net(3.0, True, 48.0),
          f"fav-10 {_net(10.0, True, 48.0):+.1f} vs fav-3 {_net(3.0, True, 48.0):+.1f}")
    check("...and a 14-point favourite in a 51-total game is negative too -- the trap spot",
          _net(14.0, True, 51.0) < 0, f"{_net(14.0, True, 51.0):+.1f}")
    favored_detail = nfl_stack_rating._game_total_component(44.0, 3.5, True)
    dog_detail = nfl_stack_rating._game_total_component(44.0, 3.5, False)
    unknown_detail = nfl_stack_rating._game_total_component(44.0, 3.5, None)
    check("_game_total_component's detail text and favored field clearly distinguish favored from "
          "underdog at the same spread magnitude, not just a bare number",
          favored_detail["favored"] is True and "favored by 3.5" in favored_detail["detail"]
          and dog_detail["favored"] is False and "underdog by 3.5" in dog_detail["detail"]
          and unknown_detail["favored"] is None and "favorite unknown" in unknown_detail["detail"],
          str((favored_detail, dog_detail, unknown_detail)))

    proe_capped = nfl_stack_rating._proe_component(100.0)
    check("PROE component is clamped at PROE_MAX_ADJUSTMENT even for an extreme value",
          proe_capped["value"] == nfl_stack_rating.PROE_MAX_ADJUSTMENT, str(proe_capped))

    funnel = nfl_stack_rating._funnel_component(5.0)
    tough = nfl_stack_rating._funnel_component(-5.0)
    check("a positive opponent def_proe_allowed (pass funnel) produces a positive rating bump, a "
          "negative one (tough pass D) produces a negative adjustment",
          funnel["value"] > 0 and tough["value"] < 0, str((funnel, tough)))

    stack_players = [
        {"name": "QB One", "position": "QB", "salary": 7000, "projection": {"fpts": 20.0, "ownership_pct": 15.0}},
        {"name": "WR Best", "position": "WR", "salary": 8000, "projection": {"fpts": 18.0, "ownership_pct": 25.0}},
        {"name": "WR Second", "position": "WR", "salary": 5000, "projection": {"fpts": 10.0, "ownership_pct": 8.0}},
        {"name": "TE One", "position": "TE", "salary": 3000, "projection": {"fpts": 6.0, "ownership_pct": 3.0}},
        {"name": "RB One", "position": "RB", "salary": 6000, "projection": {"fpts": 14.0, "ownership_pct": 12.0}},
    ]
    stack_corr = {
        "qb_wr1": {"correlation": 0.4}, "qb_wr2": {"correlation": 0.3},
        "qb_te1": {"correlation": 0.2}, "qb_rb1": {"correlation": 0.05},
        "qb_bring_back_wr1": {"correlation": 0.1},
    }
    partners = nfl_stack_rating._pick_partners(stack_players, stack_corr)
    check("_pick_partners ranks the team's own top WR ahead of the second WR, matching real roster order",
          [p["name"] for p in partners] == ["WR Best", "WR Second", "TE One", "RB One"], str(partners))
    check("each partner carries its own real correlation coefficient from the passed-in correlations",
          partners[0]["correlation"] == 0.4 and partners[3]["correlation"] == 0.05, str(partners))

    stack_opp_players = [
        {"name": "Opp WR1", "position": "WR", "team": "OPP", "salary": 7500,
         "projection": {"fpts": 15.0, "ownership_pct": 10.0}},
    ]
    bring_back = nfl_stack_rating._pick_bring_back(stack_opp_players, stack_corr)
    check("_pick_bring_back picks the opponent's own top WR and tags it with the real bring-back correlation",
          bring_back["name"] == "Opp WR1" and bring_back["correlation"] == 0.1, str(bring_back))

    full_rating = nfl_stack_rating._rate_team(
        {"abbrev": "AAA", "implied_total": 25.0, "is_home": True, "favored": True, "players": stack_players},
        {"abbrev": "OPP", "implied_total": 19.0, "is_home": False, "favored": False, "players": stack_opp_players},
        {"total_line": 50.0, "spread_line": -3.5},
        {"AAA": {"off_proe": 3.0, "def_proe_allowed": -1.0}, "OPP": {"off_proe": -1.0, "def_proe_allowed": 1.0}},
        stack_corr,
    )
    _c = full_rating["components"]
    check("_rate_team's overall rating is the exact sum of its own named components (environment + "
          "game total + game script + PROE + pass funnel + leverage), clamped 0-100 -- nothing is "
          "folded in without a visible reason",
          full_rating["rating"] == round(
              _c["environment"]["score"]
              + _c["game_total"]["value"]
              + _c["game_script"]["value"]
              + _c["proe"]["value"]
              + _c["pass_funnel"]["value"]
              + _c["leverage"]["value"],
              1,
          ),
          str(full_rating))
    check("rating_before_leverage is that same rating with only the crowding penalty removed -- so "
          "'how good is this spot' and 'how obvious is it' stay separable",
          full_rating["rating_before_leverage"]
          == round(full_rating["rating"] - _c["leverage"]["value"], 1),
          f"{full_rating['rating_before_leverage']} vs {full_rating['rating']} "
          f"- ({_c['leverage']['value']})")
    check("_rate_team's top_stack_value combines the QB and top partner's real salary/fpts/ownership",
          full_rating["top_stack_value"]["combined_salary"] == 7000 + 8000
          and full_rating["top_stack_value"]["combined_projected_fpts"] == 38.0
          and full_rating["top_stack_value"]["combined_ownership_pct"] == 40.0,
          str(full_rating["top_stack_value"]))

    no_pool_rating = nfl_stack_rating._rate_team(
        {"abbrev": "AAA", "implied_total": 28.0, "is_home": True, "favored": True, "players": []},
        {"abbrev": "OPP", "implied_total": 20.0, "is_home": False, "favored": False, "players": []},
        {"total_line": 50.0, "spread_line": -3.5},
        {}, stack_corr,
    )
    check("_rate_team degrades gracefully (no partners/bring-back/top_stack_value) when no player pool "
          "is loaded for either side",
          no_pool_rating["partners"] == [] and no_pool_rating["bring_back"] is None
          and no_pool_rating["top_stack_value"] is None,
          str(no_pool_rating))

    print("\nFantasyLabs NFL Vegas odds (clients/fantasylabs.py + nfl_slate.py)")

    nfl_fl_fixture = {
        "EventId": 35546920,
        "EventDetails": {
            "Properties": {
                "HomeTeam": "Seattle Seahawks", "VisitorTeam": "New England Patriots",
                "HomeTeamShort": "SEA", "VisitorTeamShort": "NE",
                "EventDateTime": "2026-09-09T20:20:00",
                "HomeGameSpreadOpen": -2.5, "HomeGameSpreadCurrent": -3.0,
                "VisitorGameSpreadOpen": 2.5, "VisitorGameSpreadCurrent": 3.0,
                "HomeGameMoneylineOpen": -140, "HomeGameMoneylineCurrent": -155,
                "VisitorGameMoneylineOpen": 120, "VisitorGameMoneylineCurrent": 130,
                "HomeGameOUOpen": 44.5, "HomeGameOUCurrent": 45.5,
            },
        },
    }
    fl_nfl_row = fantasylabs._parse_event(nfl_fl_fixture)
    check("_parse_event reads NFL's own HomeTeamShort/VisitorTeamShort abbreviations (shared parsing "
          "with the MLB dashboard -- same endpoint shape, different sport id)",
          fl_nfl_row["home_short"] == "SEA" and fl_nfl_row["away_short"] == "NE", str(fl_nfl_row))
    check("_parse_event reads NFL's open/current spread/moneyline/total correctly",
          fl_nfl_row["home_spread_open"] == -2.5 and fl_nfl_row["home_spread_current"] == -3.0
          and fl_nfl_row["total_open"] == 44.5 and fl_nfl_row["total_current"] == 45.5,
          str(fl_nfl_row))

    fantasylabs_rows = [fl_nfl_row]
    matched = nfl_slate._match_fantasylabs(fantasylabs_rows, "SEA", "NE")
    check("_match_fantasylabs finds a real game by direct abbreviation lookup (no fuzzy name "
          "matching needed for NFL, unlike MLB's own _match_odds())",
          matched is not None and matched["event_id"] == 35546920, str(matched))
    check("_match_fantasylabs returns None for a team pair with no matching FantasyLabs row",
          nfl_slate._match_fantasylabs(fantasylabs_rows, "KC", "DEN") is None, "")
    check("_match_fantasylabs is order-sensitive (home/away can't be swapped and still match)",
          nfl_slate._match_fantasylabs(fantasylabs_rows, "NE", "SEA") is None, "")

    check("_fantasylabs_has_line is True for a real row with an actual current value",
          nfl_slate._fantasylabs_has_line(fl_nfl_row) is True, "")
    check("_fantasylabs_has_line is False for no matched row at all",
          nfl_slate._fantasylabs_has_line(None) is False, "")
    # Real, live-confirmed scenario: a real, correctly-matched row for a
    # game far enough out that FantasyLabs hasn't posted real numbers
    # yet -- caught as a genuine bug (mis-labeled as "FantasyLabs
    # (consensus)" for a line it never actually provided) before shipping.
    empty_but_matched_row = {
        "event_id": 1, "home_short": "KC", "away_short": "DEN",
        "home_spread_current": None, "total_current": None,
        "home_moneyline_current": None, "away_moneyline_current": None,
    }
    check("_fantasylabs_has_line is False for a real matched row whose fields haven't posted yet -- "
          "the exact real bug this guards against (matched != has a real line)",
          nfl_slate._fantasylabs_has_line(empty_but_matched_row) is False, "")

    print("\nNFL sim correlations calibrated to the measured league values")

    import numpy as _np_corr

    def _corr_pool(mean):
        raw = _np_corr.random.default_rng(1).normal(mean, mean * 0.6, 200)
        return [max(0.0, float(v)) for v in raw]

    _corr_players = [
        {"id": "cqb", "position": "QB", "team": "AAA", "opponent": "BBB"},
        {"id": "cwr", "position": "WR", "team": "AAA", "opponent": "BBB"},
        {"id": "crb", "position": "RB", "team": "AAA", "opponent": "BBB"},
        {"id": "cowr", "position": "WR", "team": "BBB", "opponent": "AAA"},
    ]
    _corr_pools = {p["id"]: _corr_pool(15.0) for p in _corr_players}
    _corr_sim = nfl_variance.simulate_batch(
        [{"players": [p]} for p in _corr_players], _corr_pools, num_trials=20000, seed=9,
    )

    def _pair(a, b):
        return float(_np_corr.corrcoef(_corr_sim[a], _corr_sim[b])[0, 1])

    check("the sim's QB<->WR correlation lands near the REAL measured 0.355, not the old "
          "hand-set machinery's 0.56 -- calibrated, not guessed",
          0.28 < _pair(0, 1) < 0.43, str(_pair(0, 1)))
    check("QB<->RB correlation lands near the real measured 0.042 -- the old 0.397 believed "
          "an RB was half a pass-catcher, a ten-fold overstatement",
          -0.03 < _pair(0, 2) < 0.12, str(_pair(0, 2)))
    check("a bring-back (QB + OPPOSING WR) is now genuinely positively correlated (real "
          "measured 0.134) via the shared game factor -- it was ~0 before",
          0.06 < _pair(0, 3) < 0.22, str(_pair(0, 3)))
    check("the correlation ordering matches football reality: own pass-catcher > bring-back > RB",
          _pair(0, 1) > _pair(0, 3) > _pair(0, 2), str((_pair(0, 1), _pair(0, 3), _pair(0, 2))))

    print("\nNFL rolling multi-season baseline + ownership floors")

    check("_decayed_mean weights the newest games hardest -- a cold start with a hot recent "
          "stretch reads above the flat average",
          nfl_inhouse_projections._decayed_mean([2.0] * 10 + [20.0] * 4)
          > sum([2.0] * 10 + [20.0] * 4) / 14,
          str(nfl_inhouse_projections._decayed_mean([2.0] * 10 + [20.0] * 4)))
    check("_decayed_mean of a flat series is exactly that value",
          abs(nfl_inhouse_projections._decayed_mean([7.0] * 17) - 7.0) < 1e-9, "")

    # rolling_game_log: prior season's Week 18 dropped, current season
    # appended when it exists, and a not-yet-published current season
    # (the normal pre-season case) degrades to prior-only.
    _rolling_logs = {
        ("p1", 2025): [{"week": w, "receptions": 5} for w in range(1, 19)],  # includes W18
        ("p1", 2026): [{"week": 1, "receptions": 9}],
    }

    async def _fake_rolling_log(player_id, season, force=False):
        if (player_id, season) == ("p2", 2026):
            raise RuntimeError("stats_player_week_2026.csv not published yet")
        return _rolling_logs.get((player_id, season), [])

    _orig_log = nfl.get_player_game_log
    nfl.get_player_game_log = _fake_rolling_log
    rolled = asyncio.run(nfl_inhouse_projections.rolling_game_log("p1", 2026))
    check("rolling_game_log spans the prior season plus the current season to date, in order",
          len(rolled) == 18 and rolled[-1]["week"] == 1 and rolled[-1]["receptions"] == 9,
          str((len(rolled), rolled[-1])))
    check("the prior season's Week 18 is dropped -- clinched teams rest starters, so it's "
          "systematically unrepresentative",
          all(not (g["week"] == 18 and g["receptions"] == 5) for g in rolled[:-1]),
          str([g["week"] for g in rolled]))
    _rolling_logs[("p2", 2025)] = [{"week": w, "receptions": 4} for w in range(1, 18)]
    rolled_pre = asyncio.run(nfl_inhouse_projections.rolling_game_log("p2", 2026))
    check("a current season nflverse hasn't published yet degrades to prior-season-only "
          "rather than raising -- the normal pre-season case",
          len(rolled_pre) == 17, str(len(rolled_pre)))
    nfl.get_player_game_log = _orig_log

    check("the ownership FPTS floors match the review's own thresholds and leave DST unfloored "
          "(only 32 real candidates, every one rosterable)",
          nfl_inhouse_projections.OWNERSHIP_FPTS_FLOOR == {"QB": 8.0, "RB": 4.0, "WR": 4.0, "TE": 3.0},
          str(nfl_inhouse_projections.OWNERSHIP_FPTS_FLOOR))

    print("\nNFL in-house projections + ownership (nfl_inhouse_projections.py)")

    # --- matchup multiplier -------------------------------------------
    check("composite_from_score maps a neutral 50 to exactly 1.00x",
          nfl_scoring.composite_from_score(50.0) == 1.0, "")
    check("composite_from_score is symmetric and bounded at both extremes",
          (nfl_scoring.composite_from_score(100.0), nfl_scoring.composite_from_score(0.0)) == (1.45, 0.55),
          (nfl_scoring.composite_from_score(100.0), nfl_scoring.composite_from_score(0.0)))
    check("score_player carries a composite alongside its 0-100 score",
          "composite" in nfl_scoring.score_player(
              "WR", implied_total=22.0, is_home=True, spread=None, favored=False),
          "")

    # --- nflverse id resolution (the real bug this unblocked) ----------
    id_lookup = {
        "by_team_name": {"KC|patrick mahomes": "00-0033873", "DET|jared goff": "00-0033106"},
        "by_name": {"patrick mahomes": "00-0033873", "jared goff": "00-0033106"},
    }
    check("resolve_player_id matches a player on the team his log is filed under",
          nfl.resolve_player_id(id_lookup, "Patrick Mahomes", "KC") == "00-0033873", "")
    check("resolve_player_id still finds a player who changed teams in the offseason -- "
          "the real NFL case a team-only match would silently drop",
          nfl.resolve_player_id(id_lookup, "Jared Goff", "LV") == "00-0033106", "")
    check("resolve_player_id returns None for a player with no prior-season history at all (a rookie)",
          nfl.resolve_player_id(id_lookup, "Some Rookie", "KC") is None, "")

    # --- ownership: DK's 900% structure --------------------------------
    def _own_pool(entries):
        return [
            {"dk_id": e[0], "position": e[1], "team": e[2], "salary": e[3],
             "fpts": e[4], "implied_total": e[5]}
            for e in entries
        ]

    # Two full teams' worth of realistic players, enough per group that
    # every position's softmax has something real to distribute.
    # Group sizes matter here: with only a handful of players a group's
    # own average share is already above the ownership ceiling, so the
    # cap correctly declines to apply and every assertion about it
    # becomes vacuous. These counts keep each group's average well under
    # the cap, the way a real multi-game slate does.
    own_entries = []
    for i in range(8):
        own_entries.append((f"qb{i}", "QB", "AAA" if i % 2 else "BBB", 7000 - i * 300, 22.0 - i * 2.0, 26.0 - i))
    for i in range(14):
        own_entries.append((f"rb{i}", "RB", "AAA" if i % 2 else "BBB", 7500 - i * 300, 18.0 - i * 1.0, 26.0 - i * 0.6))
    for i in range(24):
        own_entries.append((f"wr{i}", "WR", "AAA" if i % 2 else "BBB", 8000 - i * 200, 20.0 - i * 0.7, 26.0 - i * 0.4))
    for i in range(10):
        own_entries.append((f"te{i}", "TE", "AAA" if i % 2 else "BBB", 5000 - i * 200, 12.0 - i * 1.0, 26.0 - i * 0.8))
    for i in range(8):
        own_entries.append((f"dst{i}", "DST", "AAA" if i % 2 else "BBB", 3400 - i * 150, 9.0 - i * 0.7, 26.0 - i))

    own = nfl_inhouse_projections.project_ownership(_own_pool(own_entries))
    group_sums = {}
    for e in own_entries:
        group_sums[e[1]] = group_sums.get(e[1], 0.0) + own[e[0]]

    check("ownership honors DK Classic NFL's 9 roster slots -- every group sums to its own share, "
          "and the whole slate to 900%",
          all(abs(group_sums[pos] - nfl_inhouse_projections.SLOT_TARGETS[pos] * 100) < 0.5
              for pos in group_sums) and abs(sum(group_sums.values()) - 900) < 1.0,
          {k: round(v, 1) for k, v in sorted(group_sums.items())})
    check("FLEX's single roster slot is distributed across RB/WR/TE rather than modelled as its own "
          "position -- the shares sum to exactly one slot",
          abs(sum(nfl_inhouse_projections._FLEX_SHARE.values()) - 1.0) < 1e-9,
          nfl_inhouse_projections._FLEX_SHARE)
    check("no player exceeds the real observed ownership ceiling",
          max(own.values()) <= nfl_inhouse_projections._MAX_PLAYER_OWNERSHIP + 1e-6, max(own.values()))

    # --- the cap redistributes rather than discarding -------------------
    capped = nfl_inhouse_projections._apply_ownership_cap([80.0, 10.0, 6.0, 4.0], 40.0)
    check("_apply_ownership_cap redistributes the excess instead of dropping it -- the group total is "
          "preserved exactly",
          abs(sum(capped) - 100.0) < 1e-6 and max(capped) <= 40.0 + 1e-9, capped)

    # --- QB-stack correlation, the NFL-specific field behavior ----------
    # Same WR in both pools; only his own QB's appeal changes. A chalky
    # QB should pull his pass-catchers UP (the field stacks toward him),
    # the opposite direction from MLB's opposing-pitcher penalty.
    def _wr_own_with_qb(qb_salary, qb_fpts):
        entries = list(own_entries)
        entries = [e for e in entries if not (e[1] == "QB" and e[2] == "AAA")]
        entries.append(("qb_aaa", "QB", "AAA", qb_salary, qb_fpts, 26.0))
        return nfl_inhouse_projections.project_ownership(_own_pool(entries))

    chalky = _wr_own_with_qb(6000, 26.0)
    mediocre = _wr_own_with_qb(6000, 8.0)
    check("a chalky QB pulls his own team's WRs UP (the field stacks toward him) -- the NFL counterpart "
          "to MLB's opposing-pitcher-chalk penalty, and in the opposite direction",
          chalky["wr3"] > mediocre["wr3"], (chalky["wr3"], mediocre["wr3"]))
    check("the QB-stack boost does not touch a team's RBs -- they aren't part of the stack the field "
          "is actually building",
          chalky["rb3"] <= mediocre["rb3"] + 1e-9, (chalky["rb3"], mediocre["rb3"]))

    stack_off = nfl_inhouse_projections._QB_STACK_WEIGHT
    try:
        nfl_inhouse_projections._QB_STACK_WEIGHT = 0.0
        neutral_chalky = _wr_own_with_qb(6000, 26.0)
        neutral_mediocre = _wr_own_with_qb(6000, 8.0)
        check("with the stack weight zeroed, a WR's ownership no longer moves with his QB's -- proving "
              "the boost is what's driving it, not a side effect",
              abs(neutral_chalky["wr3"] - neutral_mediocre["wr3"]) < 1e-6,
              (neutral_chalky["wr3"], neutral_mediocre["wr3"]))
    finally:
        nfl_inhouse_projections._QB_STACK_WEIGHT = stack_off

    # --- the survivorship-bias fix (a real bug found in live output) ----
    # A min-priced player the model has no history for must NOT outrank a
    # real stud. Before the replacement-level prior, six $3,000 bench WRs
    # with 0.0 projections each modelled above Ja'Marr Chase, because the
    # position pool's MEAN (a starter's average game) divided by a
    # min salary was the best points-per-dollar in the group.
    bench_entries = list(own_entries) + [
        ("bench_wr", "WR", "BBB", 3000, 2.2, 26.0),  # replacement-level projection
    ]
    bench_own = nfl_inhouse_projections.project_ownership(_own_pool(bench_entries))
    check("a min-priced player projected at replacement level does not outrank a real stud at the same "
          "position -- the exact live bug the replacement-level prior fixes",
          bench_own["bench_wr"] < bench_own["wr0"], (bench_own["bench_wr"], bench_own["wr0"]))

    check("_pool_prior reads the position pool's replacement level, NOT its survivorship-biased mean",
          nfl_inhouse_projections._pool_prior([0.0, 1.0, 2.0, 3.0, 40.0]) == 1.0,
          nfl_inhouse_projections._pool_prior([0.0, 1.0, 2.0, 3.0, 40.0]))
    check("_pool_prior returns None for an empty pool rather than guessing",
          nfl_inhouse_projections._pool_prior([]) is None, "")

    # --- projection arithmetic ------------------------------------------
    check("project_fpts scales a baseline rate by the matchup multiplier in both directions",
          (nfl_inhouse_projections.project_fpts(10.0, 1.45),
           nfl_inhouse_projections.project_fpts(10.0, 0.55)) == (14.5, 5.5),
          (nfl_inhouse_projections.project_fpts(10.0, 1.45),
           nfl_inhouse_projections.project_fpts(10.0, 0.55)))

    print("\n" + "=" * 60)
    print(f"{len(PASS)} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  FAILED: {name}")
    print()
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
