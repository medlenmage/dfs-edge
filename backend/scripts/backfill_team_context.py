"""
Backfill slate_team_context for past dates whose slate can still be
rebuilt.

Going forward every slate load archives its own team context (see the
/slate route), but that does nothing for dates already in the contest
archive. This walks the dates that have archived contest results and
re-builds each slate, archiving whatever market context survives.

Honest about its limits: Vegas lines are fetched per-day and are not
retrievable for a date whose odds cache has expired, so older dates
will archive partial context (game totals and opposing starters, but no
implied runs) or nothing at all. A date that yields nothing is reported
rather than silently written as zeros -- a false zero is worse than a
known gap, because the backtest can skip a gap but will happily fit a
zero.

    backend/.venv/Scripts/python.exe -m scripts.backfill_team_context [date ...]
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import history_db  # noqa: E402
from app.services import mlb_slate  # noqa: E402


async def main() -> None:
    days = sys.argv[1:]
    if not days:
        cpr = await history_db.get_contest_player_results()
        days = sorted({str(r["date"]) for r in cpr})

    print(f"{len(days)} date(s) to try\n")
    print(f"{'date':12} {'teams':>6} {'w/ implied':>11} {'w/ opp SP':>10}")
    total = 0
    for day in days:
        try:
            slate = await mlb_slate.build_slate(day, include_hitters=False)
        except Exception as exc:  # noqa: BLE001
            print(f"{day:12} {'-':>6} {'-':>11} {'-':>10}  build failed: {str(exc)[:60]}")
            continue
        rows = mlb_slate.team_context_rows(slate)
        if not rows:
            print(f"{day:12} {0:>6} {0:>11} {0:>10}  (no market context left for this date)")
            continue
        implied = sum(1 for r in rows if r.get("implied_runs") is not None)
        opp = sum(1 for r in rows if r.get("opposing_pitcher_id") is not None)
        await history_db.archive_slate_team_context(day, rows)
        total += len(rows)
        print(f"{day:12} {len(rows):>6} {implied:>11} {opp:>10}")

    print(f"\narchived {total} team-rows")


if __name__ == "__main__":
    asyncio.run(main())
