"""
Sweep contest.py's _SALARY_PACING_STRENGTH against a real slate.

The contest generator has no salary FLOOR any more -- a hard floor makes
whole stack plans infeasible and stalls a batch, and a floor is the wrong
tool anyway for "use as much of the cap as possible." Pacing is the right
tool: it reshapes each slot's sampling weights toward salary in
proportion to how far behind the cap's own pace the lineup already is.

This prints, for each candidate strength, the salary distribution AND the
average projected points, because the two could in principle trade off
against each other (spending more on a worse player would be a real
regression). Run it before changing the constant.

    backend/.venv/Scripts/python.exe -m scripts.sweep_salary_pacing
"""

import asyncio
import sys
from collections import Counter
from datetime import date as date_cls
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import contest, mlb_slate  # noqa: E402

STRENGTHS = [0.0, 4.0, 5.0, 6.0, 8.0, 10.0]
N = 2000


async def main() -> None:
    day = sys.argv[1] if len(sys.argv) > 1 else date_cls.today().isoformat()
    slate = await mlb_slate.build_slate(day, include_inhouse=True)
    saved = contest._SALARY_PACING_STRENGTH
    print(f"{day}: sweeping over {N} entries per strength\n")
    print(
        f"{'strength':>9} {'min':>8} {'p25':>8} {'median':>8} {'max':>8} {'<47k':>6} "
        f"{'avg pts':>9} {'distinct':>9} {'top exp%':>9} {'shapes':>7}"
    )
    try:
        for strength in STRENGTHS:
            contest._SALARY_PACING_STRENGTH = strength
            entries = contest.generate_entries(
                slate, N, allow_duplicates=True, min_salary=0, seed=7
            )
            sal = sorted(e["salary_used"] for e in entries)
            pts = [e["projected_points"] for e in entries]
            under = sum(1 for s in sal if s < 47_000)
            distinct = len({frozenset(p["id"] for p in e["players"]) for e in entries})
            counts = Counter(p["id"] for e in entries for p in e["players"])
            top_exp = 100 * counts.most_common(1)[0][1] / len(entries)
            shapes = len({e["stack_type"] for e in entries})
            print(
                f"{strength:>9.1f} {sal[0]:>8,} {sal[len(sal)//4]:>8,} {sal[len(sal)//2]:>8,} "
                f"{sal[-1]:>8,} {under:>6} {sum(pts)/len(pts):>9.2f} {distinct:>9} "
                f"{top_exp:>9.1f} {shapes:>7}"
            )
    finally:
        contest._SALARY_PACING_STRENGTH = saved


if __name__ == "__main__":
    asyncio.run(main())
