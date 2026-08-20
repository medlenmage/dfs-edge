"""
Backtests the Monte Carlo engine's per-player outcome pools
(variance.player_outcome_pool()) against real archived contest data --
run by hand, not part of the main app (same convention as
migrate_history_db.py).

Why this exists: a real DK contest-standings export has no payout
table and no per-entry lineup data at all (confirmed by directly
reading the file format), so "replay a real contest's real field and
compare simulated cash%/ROI to what actually happened" -- the most
literal reading of a contest simulator backtest -- isn't something this
data source can support, ever, regardless of how much of it
accumulates. What IS genuinely available and worth checking: every
archived contest's real players' real final `actual_fpts`
(contest_player_results in Supabase). This script checks whether the
Monte Carlo engine's own outcome pools -- the thing that actually
drives every downstream cash%/ROI number -- are honestly calibrated
against what real players really scored on real dates, using the
standard probability-integral-transform technique
(contest_results.outcome_percentile/calibration_summary): if the
model's spread is honest, real scores should land at every percentile
of their own pool with roughly equal likelihood, not cluster near the
middle (pools too wide) or the edges (pools too narrow).

Player identity (name -> MLB player_id) is resolved by rebuilding that
date's real slate via mlb_slate.build_slate() -- a live MLB Stats API
call, independent of whatever's left in the local salary/projection
upload cache (which expires 7 days from UPLOAD time, not the slate
date, and may already be gone for some of these dates).

Usage (from backend/):
    .venv/Scripts/python.exe -m scripts.backtest_outcome_pools
"""

from __future__ import annotations

import asyncio
import sys
from collections import defaultdict

from app import history_db
from app.services import contest_results, mlb_slate, player_match, variance


async def _name_to_player_id_for_date(day: str) -> dict[str, int]:
    """Every hitter/pitcher's normalized_name -> real MLB player_id for one real date's slate."""
    slate = await mlb_slate.build_slate(day)
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

    # Two parallel passes: `current` reproduces exactly what the LIVE
    # app does today (a player's full season game log, no date
    # awareness at all -- the correct behavior for a real slate, since
    # "today" has no future games to leak); `asof` additionally cuts
    # off any game on or after the archived contest's own date, closing
    # the look-ahead gap a naive backtest would otherwise have (every
    # one of these archived contests is now in the past relative to
    # "today," so the CURRENT pass's season-long pool necessarily
    # includes games that hadn't happened yet as of that real contest).
    percentiles: dict[str, list[float]] = {"current": [], "asof": []}
    hitter_percentiles: dict[str, list[float]] = {"current": [], "asof": []}
    pitcher_percentiles: dict[str, list[float]] = {"current": [], "asof": []}
    unmatched = 0
    no_pool = 0

    for day, day_rows in sorted(by_date.items()):
        print(f"-- {day} ({len(day_rows)} archived players) --")
        try:
            name_to_id = await _name_to_player_id_for_date(day)
        except Exception as exc:
            print(f"   couldn't rebuild this date's slate ({exc}) -- skipping")
            continue

        season = int(day[:4])
        for r in day_rows:
            if r["actual_fpts"] is None:
                continue
            pid = name_to_id.get(r["normalized_name"])
            if pid is None:
                unmatched += 1
                continue
            position = r["position"] or "OF"
            current_pool = await variance.player_outcome_pool(pid, position, season)
            asof_pool = await variance.player_outcome_pool(pid, position, season, as_of_date=day)
            if not current_pool or not asof_pool:
                no_pool += 1
                continue
            for label, pool in (("current", current_pool), ("asof", asof_pool)):
                pct = contest_results.outcome_percentile(float(r["actual_fpts"]), pool)
                percentiles[label].append(pct)
                (pitcher_percentiles if position == "P" else hitter_percentiles)[label].append(pct)

    n_matched = len(percentiles["current"])
    print(f"\nMatched {n_matched} of {len(rows)} archived players to a real outcome pool "
          f"({unmatched} name-match misses, {no_pool} with no pool data).\n")

    for label, desc in [("current", "CURRENT (full season, what the live app actually does)"),
                         ("asof", "AS-OF (games before the contest date only, no look-ahead)")]:
        print(f"-- {desc} --")
        print("  Overall: ", contest_results.calibration_summary(percentiles[label]))
        print("  Hitters: ", contest_results.calibration_summary(hitter_percentiles[label]))
        print("  Pitchers:", contest_results.calibration_summary(pitcher_percentiles[label]))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
