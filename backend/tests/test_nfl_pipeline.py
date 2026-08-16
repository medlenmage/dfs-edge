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

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.clients import nfl  # noqa: E402
from app.services import nfl_optimizer, nfl_scoring, player_match, salaries  # noqa: E402

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
    )
    check("score_player returns a 0-100 score with every component and a top_driver",
          0 <= full["score"] <= 100 and set(full["components"]) == {"implied_total", "game_script", "weather"}
          and full["top_driver"] in full["components"],
          str(full))

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

    print("\n" + "=" * 60)
    print(f"{len(PASS)} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  FAILED: {name}")
    print()
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
