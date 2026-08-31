"""
Calibration diagnostic for in-house ownership, on the FULL live pipeline
(mlb_slate.build_slate with include_inhouse=True) rather than the
archive reconstruction -- so the team-stack layer, which is the model's
heaviest signal and needs Vegas implied runs, is actually active.

The existing backtest_ownership.py reports Spearman only, and Spearman
is exactly the metric that hides this class of error: a model can rank
players in very nearly the right ORDER while compressing every
prediction into a narrow band, so the chalk is never called chalk. That
is a spread failure, and it shows up in calibration buckets and chalk
MAE, not in rank correlation.

    backend/.venv/Scripts/python.exe -m scripts.diagnose_ownership_calibration [date ...]
"""

from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import history_db  # noqa: E402
from app.services import mlb_slate  # noqa: E402

DEFAULT_DAYS = ["2026-08-24", "2026-08-25", "2026-08-30"]
HITTER_SLOTS = ("C", "1B", "2B", "3B", "SS", "OF", "DH")


async def main():
    days = sys.argv[1:] or DEFAULT_DAYS

    cpr = await history_db.get_contest_player_results()
    real_by_date = defaultdict(dict)
    for r in cpr:
        if r.get("ownership_pct") is not None:
            real_by_date[str(r["date"])][r["normalized_name"]] = float(r["ownership_pct"])

    allpairs = []
    rw_pairs = []
    for day in days:
        real = real_by_date.get(day)
        if not real:
            print(f"{day}: no archived contest results")
            continue
        slate = await mlb_slate.build_slate(day, include_inhouse=True)
        pairs, rwp = [], []
        for g in slate.get("games", []):
            for side in ("home", "away"):
                s = g[side]
                people = list(s.get("hitters") or [])
                p = s.get("probable_pitcher")
                if p:
                    people.append(p)
                for pl in people:
                    proj = pl.get("projection") or {}
                    ih = proj.get("inhouse_ownership_pct")
                    from app.services import player_match

                    key = player_match.normalize_name(pl.get("name") or "")
                    r = real.get(key)
                    if r is None:
                        continue
                    if ih is not None:
                        pairs.append((ih, r))
                    rw = proj.get("ownership_pct")
                    if rw is not None:
                        rwp.append((float(rw), r))
        allpairs += pairs
        rw_pairs += rwp
        top = sorted(pairs, key=lambda x: -x[1])[:20]
        print(
            f"{day}: n={len(pairs):>4}  "
            f"chalk real avg {sum(r for _, r in top) / max(len(top), 1):>5.1f}%  "
            f"predicted {sum(p for p, _ in top) / max(len(top), 1):>5.1f}%  "
            f"max predicted {max((p for p, _ in pairs), default=0):>5.1f}%"
        )

    if not allpairs:
        print("nothing to score")
        return

    print(f"\nPooled n={len(allpairs)}")
    for label, pairs in (("IN-HOUSE", allpairs), ("ROTOWIRE", rw_pairs)):
        if not pairs:
            continue
        top = sorted(pairs, key=lambda x: -x[1])[:30]
        mae = sum(abs(p - r) for p, r in pairs) / len(pairs)
        cmae = sum(abs(p - r) for p, r in top) / len(top)
        print(
            f"  {label:9} MAE={mae:>5.2f}  chalk MAE(top30)={cmae:>6.2f}  "
            f"max pred={max(p for p, _ in pairs):>5.1f}%  "
            f"pred on chalk={sum(p for p, _ in top) / len(top):>5.1f}% "
            f"(real {sum(r for _, r in top) / len(top):>5.1f}%)"
        )

    print("\nin-house calibration by REAL ownership bucket (full pipeline):")
    for lo, hi in [(0, 1), (1, 3), (3, 6), (6, 10), (10, 20), (20, 40), (40, 101)]:
        sel = [(p, r) for p, r in allpairs if lo <= r < hi]
        if not sel:
            continue
        mp = sum(p for p, _ in sel) / len(sel)
        mr = sum(r for _, r in sel) / len(sel)
        print(
            f"  real {lo:>3}-{hi:<3}%  n={len(sel):>4}  real avg {mr:>5.1f}%  "
            f"predicted avg {mp:>5.1f}%  bias {mp - mr:>+6.1f}"
        )


if __name__ == "__main__":
    asyncio.run(main())
