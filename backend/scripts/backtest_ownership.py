"""
Backtests inhouse_projections.project_ownership() against real archived
DK contest-standings data -- run by hand, not part of the main app
(same convention as backtest_outcome_pools.py/migrate_history_db.py).

For every real archived contest (contest_player_results in Supabase,
uploaded via POST /api/mlb/contest-results), this rebuilds that date's
real slate with in-house ownership computed (mlb_slate.build_slate(day,
include_hitters=True, include_inhouse=True)), matches players by
normalized name against the contest's own real final %Drafted, and
reports the Spearman rank correlation between the two -- the same check
that originally caught the min-max-normalization bug in
project_ownership() (see its own module docstring, "WHY VALUE IS
NORMALISED"). A well-calibrated model should show a real positive
correlation on every real slate, not just the ones it happened to be
tuned against.

Relies on the LOCAL salary/projections cache still holding that date's
upload (day-keyed, 7-day TTL from upload time -- see salaries.py/
projections.py) -- unlike backtest_outcome_pools.py, ownership can't be
computed at all without a matched real DK salary, so a date whose local
cache has already expired is skipped and reported, not silently zeroed.

Usage (from backend/):
    .venv/Scripts/python.exe -m scripts.backtest_ownership
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from app import history_db
from app.services import mlb_slate, player_match


def _rank(values: list[float]) -> list[float]:
    """Standard competition ranking with average ranks for ties (1-indexed)."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return ranks


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    rx, ry = _rank(xs), _rank(ys)
    n = len(rx)
    mean_x, mean_y = sum(rx) / n, sum(ry) / n
    var_x = sum((x - mean_x) ** 2 for x in rx)
    var_y = sum((y - mean_y) ** 2 for y in ry)
    if var_x == 0 or var_y == 0:
        return None
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(rx, ry))
    return cov / (var_x**0.5 * var_y**0.5)


async def _inhouse_ownership_by_name(day: str) -> dict[str, float]:
    """normalized_name -> inhouse_ownership_pct for one real date's slate,
    from whatever salary/projections upload is still in the local cache."""
    slate = await mlb_slate.build_slate(day, include_hitters=True, include_inhouse=True)
    lookup: dict[str, float] = {}
    for game in slate.get("games", []):
        for side in ("home", "away"):
            for h in game[side]["hitters"]:
                own = (h.get("projection") or {}).get("inhouse_ownership_pct")
                if own is not None:
                    lookup[player_match.normalize_name(h["name"])] = own
            pitcher = game[side]["probable_pitcher"]
            if pitcher:
                own = (pitcher.get("projection") or {}).get("inhouse_ownership_pct")
                if own is not None:
                    lookup[player_match.normalize_name(pitcher["name"])] = own
    return lookup


async def main() -> int:
    rows = await history_db.get_contest_player_results()
    if not rows:
        print("No archived contest results found (Supabase not configured, or nothing uploaded yet).")
        return 1

    by_contest: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        by_contest[(r["date"], r["contest_id"], r["contest_name"])].append(r)

    print(f"{len(by_contest)} archived contest(s), {len(rows)} real player results total\n")

    correlations: list[float] = []
    for (day, contest_id, name), players in sorted(by_contest.items()):
        day_str = day.isoformat()
        try:
            own_by_name = await _inhouse_ownership_by_name(day_str)
        except Exception as exc:
            print(f"{day_str}  {name:<32} SKIPPED (couldn't rebuild slate: {exc})")
            continue

        if not own_by_name:
            print(f"{day_str}  {name:<32} SKIPPED (no local salary/projections cache left for this date -- "
                  f"expired or never uploaded on this machine)")
            continue

        real_xs, model_ys = [], []
        for p in players:
            model_own = own_by_name.get(p["normalized_name"])
            if model_own is not None and p["ownership_pct"] is not None:
                real_xs.append(p["ownership_pct"])
                model_ys.append(model_own)

        if len(real_xs) < 5:
            print(f"{day_str}  {name:<32} SKIPPED (only {len(real_xs)} players matched, too thin to trust)")
            continue

        corr = _spearman(real_xs, model_ys)
        correlations.append(corr if corr is not None else 0.0)
        print(f"{day_str}  {name:<32} n={len(real_xs):<4} Spearman r={corr:+.3f}" if corr is not None
              else f"{day_str}  {name:<32} n={len(real_xs):<4} Spearman r=n/a")

    if correlations:
        avg = sum(correlations) / len(correlations)
        print(f"\n{len(correlations)} slate(s) backtested -- average Spearman r={avg:+.3f}")
    else:
        print("\nNo slate had a matchable local salary/projections cache -- nothing backtested.")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
