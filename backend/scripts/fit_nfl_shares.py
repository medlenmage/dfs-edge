"""
Fits the constants in services/nfl_shares.py from real nflverse game
logs -- run by hand, not part of the app (same convention as
backtest_ownership.py / measure_mlb_sim_correlations.py).

Run this before changing any constant in nfl_shares.py. The design that
module implements is explicit that the calibration order matters:

    (a) fit k per position from the historical SD of realized share
    (b) fit s per position so per-touch yardage variance matches
    (c) only THEN check simulated fantasy-point SD per player

and that skipping to (c) makes you compensate for an error in one layer
with an offsetting error in another. This script does (a) and (b). Step
(c) needs the team-level layer, which does not exist yet.

WHY (b) IS NOT THE OBVIOUS CALCULATION

The tempting way to fit s is to pool every game's yards-per-touch and
take mean^2 / variance. That is wrong, and wrong by a lot: it ignores
that Var(y/n) shrinks as touches rise, so it comes out inflated by
roughly the average touch count. On 2025 data the naive version reads
WR 3.07 and RB-rushing 2.70; conditioning on n properly gives 1.27 and
0.42.

The model is  y | n ~ Gamma(shape = n*s, scale = ypX/s), so

    Var(y | n) = n * ypX^2 / s      =>      s = n * ypX^2 / Var(y | n)

evaluated within each touch-count bucket and combined by weighted
median. If the model is right, the per-bucket estimates should be
roughly FLAT in n -- that flatness is reported below, and is the
evidence that shape-scaling-with-n describes real yardage.

Usage (from backend/):
    .venv/Scripts/python.exe -m scripts.fit_nfl_shares [season]
"""

from __future__ import annotations

import asyncio
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clients import nfl  # noqa: E402
from app.services import nfl_shares  # noqa: E402

# A player needs this many games before his realized share SD means
# anything, and a share below the floor is noise rather than a role --
# a man targeted twice all season has a "share" whose variance says
# nothing about how a team distributes volume.
MIN_GAMES = 8
MIN_MEAN_SHARE = 0.02
# Team-games thinner than this are blowouts, injuries or weather, where
# a share is not measuring the usual distribution of work.
MIN_TEAM_ATTEMPTS = 15
# Below this many observations a bucket's variance is itself too noisy
# to fit against.
MIN_BUCKET_GAMES = 60


async def main() -> int:
    season = int(sys.argv[1]) if len(sys.argv) > 1 else 2025
    grouped = await nfl.get_grouped_season_stats(season)
    print(f"{season} regular season: {len(grouped)} players\n")

    team_totals: dict[tuple, dict[str, float]] = defaultdict(lambda: {"targets": 0.0, "carries": 0.0})
    usable: dict[str, list[dict]] = {}
    for pid, rows in grouped.items():
        keep = [r for r in rows if r.get("team") and r.get("game_id")]
        if keep:
            usable[pid] = keep
        for row in keep:
            key = (row["game_id"], row["team"])
            team_totals[key]["targets"] += row["targets"]
            team_totals[key]["carries"] += row["carries"]

    target_shares: dict[str, list[float]] = defaultdict(list)
    carry_shares: dict[str, list[float]] = defaultdict(list)
    position_of: dict[str, str] = {}
    catch_rate: dict[str, list[float]] = defaultdict(list)
    rec_by_count: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    rush_by_count: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))

    for pid, rows in usable.items():
        for row in rows:
            position_of[pid] = row.get("position") or "?"
            totals = team_totals[(row["game_id"], row["team"])]
            if totals["targets"] >= MIN_TEAM_ATTEMPTS:
                target_shares[pid].append(row["targets"] / totals["targets"])
            if totals["carries"] >= MIN_TEAM_ATTEMPTS:
                carry_shares[pid].append(row["carries"] / totals["carries"])
            position = position_of[pid]
            receptions, carries = int(row["receptions"]), int(row["carries"])
            if 1 <= receptions <= 12:
                rec_by_count[position][receptions].append(row["receiving_yards"])
            if 1 <= carries <= 25:
                rush_by_count[position][carries].append(row["rushing_yards"])
            if row["targets"] >= 1:
                catch_rate[position].append(row["receptions"] / row["targets"])

    print("(a) DIRICHLET CONCENTRATION k -- from realized weekly share SD")
    print("    higher k = shares hug projection more tightly\n")
    for label, store, positions, current in (
        ("targets", target_shares, ("WR", "TE", "RB"), nfl_shares.TARGET_CONCENTRATION),
        ("carries", carry_shares, ("RB", "QB", "WR"), nfl_shares.CARRY_CONCENTRATION),
    ):
        print(f"  {label}:")
        for position in positions:
            means, sds = [], []
            for pid, shares in store.items():
                if position_of.get(pid) != position or len(shares) < MIN_GAMES:
                    continue
                mean_share = st.mean(shares)
                if mean_share < MIN_MEAN_SHARE:
                    continue
                means.append(mean_share)
                sds.append(st.pstdev(shares))
            if len(means) < 10:
                continue
            fitted = nfl_shares.solve_concentration(np.array(means), np.array(sds))
            print(f"    {position:3s} n={len(means):3d}  mean share {st.mean(means):.3f}  "
                  f"realized sd {st.median(sds):.3f}  ->  k = {fitted:6.1f}   "
                  f"(in use: {current.get(position, float('nan')):.1f})")

    print("\n(b) GAMMA EXPLOSIVENESS s -- per touch-count bucket, s = n*ypX^2 / Var(y|n)")
    print("    estimates should be roughly FLAT across n; that is the model check\n")
    for label, store, current in (
        ("receiving", rec_by_count, nfl_shares.RECEIVING_EXPLOSIVENESS),
        ("rushing", rush_by_count, nfl_shares.RUSHING_EXPLOSIVENESS),
    ):
        print(f"  {label}:")
        for position in ("QB", "RB", "WR", "TE"):
            estimates, weights, shown = [], [], []
            for count, yards in sorted(store.get(position, {}).items()):
                if len(yards) < MIN_BUCKET_GAMES:
                    continue
                mean_y, var_y = st.mean(yards), st.pvariance(yards)
                if var_y <= 0 or mean_y <= 0:
                    continue
                per_touch = mean_y / count
                estimates.append(count * per_touch * per_touch / var_y)
                weights.append(len(yards))
                shown.append(f"n={count}:{estimates[-1]:.2f}")
            if not estimates:
                continue
            order = sorted(range(len(estimates)), key=lambda i: estimates[i])
            half, running, fitted = sum(weights) / 2, 0, estimates[order[-1]]
            for i in order:
                running += weights[i]
                if running >= half:
                    fitted = estimates[i]
                    break
            print(f"    {position:3s} s = {fitted:5.2f}  (in use: "
                  f"{current.get(position, float('nan')):.2f})   {'  '.join(shown[:6])}")

    print("\n  catch rate (used only when a player has no fitted rate of his own):")
    for position in ("RB", "WR", "TE"):
        values = catch_rate.get(position, [])
        if len(values) >= 200:
            print(f"    {position:3s} n={len(values):5d}  {st.mean(values):.3f}   "
                  f"(in use: {nfl_shares.LEAGUE_CATCH_RATE.get(position, float('nan')):.3f})")

    print("\n(c) NOT DONE HERE. Checking simulated fantasy-point SD against history")
    print("    needs the team-level layer to exist first -- it is what supplies the")
    print("    per-sim attempts/TDs/efficiency/script this module allocates.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
