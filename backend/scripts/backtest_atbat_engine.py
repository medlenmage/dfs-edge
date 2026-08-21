"""
Backtests the at-bat simulation engine's per-player outcome
distributions (atbat_sim.simulate_slate_trials()) against real archived
contest data -- run by hand, not part of the main app (same convention
as backtest_outcome_pools.py, which this directly parallels for the
OTHER engine).

Same real data, same probability-integral-transform technique
(contest_results.outcome_percentile()/calibration_summary()): for
every archived (player, real contest date) pair with a real
`actual_fpts`, check where that real score lands within the at-bat
engine's own simulated outcome distribution for that player on that
date. A well-calibrated engine should show real scores landing at
every percentile with roughly equal likelihood; a look-ahead-free
`as_of_date` pass (see atbat_sim.simulate_slate_trials()'s own
`as_of_date` parameter, added specifically for this) is the only
number that matters -- the CURRENT/live pass backtest_outcome_pools.py
reports for comparison purposes doesn't apply cleanly here the same
way, since the at-bat engine also needs each game's real CONFIRMED
lineup for that specific historical date (via mlb.get_lineups()),
which is inherently backtest-safe already (a real historical lineup
that already posted, not a future one) -- the only look-ahead risk is
in the season-long RATE data (game logs), which as_of_date closes.

Real, stated limitation this doesn't close: mlb.get_bullpen_stats()
has no as_of_date support at all (it's a season aggregate, not
per-game logs) -- a team's bullpen rates during a backtest reflect
their WHOLE season, not just games before the contest date. This
affects only the smaller fraction of plate appearances that happen
after a starter's own target innings are exhausted, so it's a real but
secondary source of look-ahead in this backtest, left open rather than
silently ignored.

A slate's per-date game list has no DK salary CSV to derive `in_slate`
from (that only exists for TODAY's real live upload) -- every game on
that date's real MLB schedule is passed explicitly via
`included_game_pks` instead, filtered to whichever games' lineups/
pitchers were actually resolvable that day (a real early-season
call-up, a scratched game, or a data gap in older Stats API rows can
still make a specific game unusable; those are skipped rather than
failing the whole date).

Usage (from backend/):
    .venv/Scripts/python.exe -m scripts.backtest_atbat_engine
"""

from __future__ import annotations

import asyncio
import sys
from collections import defaultdict

from app import history_db
from app.services import atbat_sim, contest_results, mlb_slate, player_match

NUM_TRIALS = 1000


async def _slate_for_date(day: str) -> tuple[dict, list[int]]:
    """The real slate for one historical date, plus the game_pks that
    are actually at-bat-simulatable that day (confirmed lineups both
    sides, resolvable probable pitchers -- see atbat_sim._side_ready()/
    _pitcher_id_for_side())."""
    slate = await mlb_slate.build_slate(day)
    ready_pks = [
        g["game_pk"] for g in slate["games"]
        if atbat_sim._side_ready(g["home"]) and atbat_sim._side_ready(g["away"])
        and atbat_sim._pitcher_id_for_side(g["home"]) and atbat_sim._pitcher_id_for_side(g["away"])
    ]
    return slate, ready_pks


def _name_to_player_id(slate: dict) -> dict[str, int]:
    """Every hitter/pitcher's normalized_name -> real MLB player_id for one real date's slate --
    same approach backtest_outcome_pools.py already uses."""
    lookup: dict[str, int] = {}
    for game in slate["games"]:
        for side in ("home", "away"):
            for h in game[side]["hitters"]:
                lookup.setdefault(player_match.normalize_name(h["name"]), h["id"])
            pitcher = game[side]["probable_pitcher"]
            if pitcher and pitcher.get("id"):
                lookup.setdefault(player_match.normalize_name(pitcher["name"]), pitcher["id"])
    return lookup


async def main() -> int:
    rows = await history_db.get_contest_player_results()
    if not rows:
        print("No archived contest_player_results found -- nothing to backtest. "
              "(SUPABASE_DB_URL unset, or no contest-standings files uploaded yet.)")
        return 1

    by_date: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_date[r["date"].isoformat()].append(r)

    print(f"{len(rows)} archived player results across {len(by_date)} real dates: "
          f"{', '.join(sorted(by_date))}\n")

    percentiles: list[float] = []
    hitter_percentiles: list[float] = []
    pitcher_percentiles: list[float] = []
    unmatched = 0
    no_pool = 0
    not_simulatable_dates = 0

    for day, day_rows in sorted(by_date.items()):
        print(f"-- {day} ({len(day_rows)} archived players) --")
        try:
            slate, ready_pks = await _slate_for_date(day)
        except Exception as exc:  # noqa: BLE001
            print(f"   couldn't rebuild this date's slate ({exc}) -- skipping")
            continue
        if not ready_pks:
            print("   no at-bat-simulatable games this date (no confirmed lineups/pitchers "
                  "resolvable) -- skipping")
            not_simulatable_dates += 1
            continue

        name_to_id = _name_to_player_id(slate)
        try:
            player_trials = await atbat_sim.simulate_slate_trials(
                slate, int(day[:4]), num_trials=NUM_TRIALS, seed=hash(day) % (2**31),
                included_game_pks=ready_pks, as_of_date=day,
            )
        except atbat_sim.SlateNotSimulatableError as exc:
            print(f"   {exc} -- skipping")
            not_simulatable_dates += 1
            continue

        print(f"   simulated {len(ready_pks)} of {len(slate['games'])} real games this date "
              f"({len(player_trials)} players with a real outcome distribution)")

        for r in day_rows:
            if r["actual_fpts"] is None:
                continue
            pid = name_to_id.get(r["normalized_name"])
            if pid is None:
                unmatched += 1
                continue
            pool = player_trials.get(pid)
            if not pool:
                no_pool += 1
                continue
            position = r["position"] or "OF"
            pct = contest_results.outcome_percentile(float(r["actual_fpts"]), pool)
            percentiles.append(pct)
            (pitcher_percentiles if position == "P" else hitter_percentiles).append(pct)

    print(f"\n{not_simulatable_dates} of {len(by_date)} real dates had no at-bat-simulatable "
          f"games at all (postponements, no confirmed lineups resolvable historically, etc).")
    print(f"Matched {len(percentiles)} of {len(rows)} archived players to a real at-bat-engine "
          f"outcome distribution ({unmatched} name-match misses, {no_pool} not part of a "
          f"simulatable game that date).\n")

    print("-- AS-OF (games before the contest date only, no look-ahead) --")
    print("  Overall: ", contest_results.calibration_summary(percentiles))
    print("  Hitters: ", contest_results.calibration_summary(hitter_percentiles))
    print("  Pitchers:", contest_results.calibration_summary(pitcher_percentiles))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
