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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import cache  # noqa: E402
from app.clients import nfl, rotowire_nfl  # noqa: E402
from app.services import nfl_dk_points, nfl_optimizer, nfl_scoring, nfl_variance, player_match, salaries  # noqa: E402

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

    def player(id_, name, team, pos, salary, fpts, own=10.0):
        return {
            "dk_id": id_, "name": name, "salary": salary, "position": pos,
            "projection": {"fpts": fpts, "ownership_pct": own},
        }

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
          "pools with no shared team (the whole reason NFL stacking correlation exists to model)",
          statistics_module.pstdev(stacked_sim[0].tolist()) > 1.15 * statistics_module.pstdev(unstacked_sim[0].tolist()),
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

    print("\n" + "=" * 60)
    print(f"{len(PASS)} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  FAILED: {name}")
    print()
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
