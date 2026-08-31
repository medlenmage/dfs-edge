"""
Backtest the in-house FPTS projection against real archived DK actuals,
with RotoWire's own projection scored on exactly the same players as
the baseline.

`contest_player_results` carries every player's REAL actual_fpts from a
finished contest, so this is a genuine accuracy test rather than a
self-consistency check. Runs on the full live pipeline so the in-house
number is the real one (baseline x matchup composite), not a
reconstruction.

Reported per source:
  MAE / RMSE            -- raw accuracy
  correlation           -- does it rank the slate correctly
  bias                  -- systematically high or low
  MAE on the top 30 by ACTUAL points -- the players who decided the
    contest, where being wrong actually costs something

    backend/.venv/Scripts/python.exe -m scripts.backtest_projections_archive [date ...]
"""

from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import history_db  # noqa: E402
from app.services import mlb_slate, player_match  # noqa: E402

DEFAULT_DAYS = ["2026-08-24", "2026-08-25", "2026-08-30"]


def pearson(a, b):
    n = len(a)
    if n < 3:
        return float("nan")
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = sum((x - ma) ** 2 for x in a) ** 0.5
    db = sum((y - mb) ** 2 for y in b) ** 0.5
    return num / (da * db) if da and db else float("nan")


def report(label, pairs):
    if not pairs:
        print(f"  {label:10} no data")
        return
    pred = [p for p, _ in pairs]
    act = [a for _, a in pairs]
    n = len(pairs)
    mae = sum(abs(p - a) for p, a in pairs) / n
    rmse = (sum((p - a) ** 2 for p, a in pairs) / n) ** 0.5
    bias = sum(p - a for p, a in pairs) / n
    top = sorted(pairs, key=lambda x: -x[1])[:30]
    top_mae = sum(abs(p - a) for p, a in top) / len(top)
    print(
        f"  {label:10} n={n:>4}  MAE={mae:>5.2f}  RMSE={rmse:>5.2f}  r={pearson(pred, act):>5.3f}  "
        f"bias={bias:>+5.2f}  MAE@top30={top_mae:>5.2f}"
    )


async def main():
    days = sys.argv[1:] or DEFAULT_DAYS

    cpr = await history_db.get_contest_player_results()
    actual = defaultdict(dict)
    for r in cpr:
        if r.get("actual_fpts") is not None:
            actual[str(r["date"])][r["normalized_name"]] = float(r["actual_fpts"])

    pooled = {"in-house": [], "rotowire": []}
    for day in days:
        act = actual.get(day)
        if not act:
            print(f"{day}: no archived actuals")
            continue
        slate = await mlb_slate.build_slate(day, include_inhouse=True)
        per = {"in-house": [], "rotowire": []}
        for g in slate.get("games", []):
            for side in ("home", "away"):
                s = g[side]
                people = list(s.get("hitters") or [])
                p = s.get("probable_pitcher")
                if p:
                    people.append(p)
                for pl in people:
                    proj = pl.get("projection") or {}
                    a = act.get(player_match.normalize_name(pl.get("name") or ""))
                    if a is None:
                        continue
                    if proj.get("inhouse_fpts") is not None:
                        per["in-house"].append((float(proj["inhouse_fpts"]), a))
                    if proj.get("fpts") is not None:
                        per["rotowire"].append((float(proj["fpts"]), a))
        print(f"\n{day}")
        for k in ("in-house", "rotowire"):
            report(k, per[k])
            pooled[k] += per[k]

    print("\nPOOLED")
    for k in ("in-house", "rotowire"):
        report(k, pooled[k])


if __name__ == "__main__":
    asyncio.run(main())
