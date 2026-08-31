"""
Backfill slate_projections' in-house columns for past dates.

Going forward the /slate route archives these whenever in-house is
computed. This fills in the dates already sitting in the archive, so a
historical ownership backtest can run project_ownership() on the
projection the model actually uses rather than on RotoWire's.

Only annotates rows the day's real DK/RotoWire upload already created
-- a date with no archived slate_projections rows is reported and
skipped rather than invented.

    backend/.venv/Scripts/python.exe -m scripts.backfill_inhouse_projections [date ...]
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date as date_cls
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import history_db  # noqa: E402
from app.services import mlb_slate  # noqa: E402


async def days_with_rows() -> list[str]:
    pool = await history_db._get_pool()
    if pool is None:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT DISTINCT date FROM slate_projections ORDER BY date")
    return [str(r["date"]) for r in rows]


async def main() -> None:
    days = sys.argv[1:] or await days_with_rows()
    print(f"{len(days)} date(s) with archived projection rows\n")
    print(f"{'date':12} {'computed':>9} {'w/ fpts':>8} {'w/ own':>7}")
    for day in days:
        try:
            slate = await mlb_slate.build_slate(day, include_inhouse=True)
        except Exception as exc:  # noqa: BLE001
            print(f"{day:12} {'-':>9} {'-':>8} {'-':>7}  build failed: {str(exc)[:50]}")
            continue
        rows = mlb_slate.inhouse_projection_rows(slate)
        if not rows:
            print(f"{day:12} {0:>9} {0:>8} {0:>7}  (no in-house numbers computed)")
            continue
        fp = sum(1 for r in rows if r.get("inhouse_fpts") is not None)
        ow = sum(1 for r in rows if r.get("inhouse_ownership_pct") is not None)
        await history_db.archive_inhouse_projections(day, rows)
        print(f"{day:12} {len(rows):>9} {fp:>8} {ow:>7}")


if __name__ == "__main__":
    asyncio.run(main())
