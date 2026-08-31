"""
Backtest inhouse_projections.project_ownership() against every archived
real DK contest, reading the DURABLE Supabase archive rather than the
7-day local cache -- so it covers the whole archive, not just whatever
was uploaded in the last week.

Reports the metrics that actually matter for a DFS ownership model, and
deliberately not just the global Spearman: a model can look fine at
+0.60 rank correlation while missing a whole team stack by 100
percentage points, which is exactly what a real diagnostic found here
once before.

  - Spearman rank correlation (kept for continuity with past runs)
  - CHALK MAE -- mean absolute error on the 20 most-owned players, the
    ones that actually decide whether a lineup is contrarian
  - TEAM-STACK MAE -- summed hitter ownership per team, predicted vs
    real; the quantity MLB field behaviour is organised around
  - Calibration by real-ownership bucket -- where the model runs hot
    or cold

RotoWire's own archived projected ownership is scored on exactly the
same players by exactly the same metrics, as a baseline. A model that
can't beat the number already sitting in the projections file isn't
earning its place.

    backend/.venv/Scripts/python.exe -m scripts.backtest_ownership_archive
"""

from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from datetime import date as date_cls
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import history_db  # noqa: E402
from app.services import inhouse_projections  # noqa: E402

HITTER_SLOTS = ("C", "1B", "2B", "3B", "SS", "OF")


def _rank(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(a, b):
    if len(a) < 3:
        return float("nan")
    ra, rb = _rank(a), _rank(b)
    n = len(ra)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    da = sum((x - ma) ** 2 for x in ra) ** 0.5
    db = sum((y - mb) ** 2 for y in rb) ** 0.5
    return num / (da * db) if da and db else float("nan")


def mae(pairs):
    return sum(abs(p - r) for p, r in pairs) / len(pairs) if pairs else float("nan")


async def archived_rows(day):
    """slate_projections for one day, including RotoWire's own ownership."""
    pool = await history_db._get_pool()
    if pool is None:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT normalized_name, name, team, position, salary,
                   rotowire_fpts, rotowire_ownership_pct
            FROM slate_projections
            WHERE date = $1 AND salary IS NOT NULL AND rotowire_fpts IS NOT NULL
            """,
            date_cls.fromisoformat(day),
        )
    return [dict(r) for r in rows]


def build_pool(rows):
    """slate_projections rows -> project_ownership()'s input shape.

    implied_runs is left None: historical Vegas totals aren't in the
    archive, and the model degrades to a neutral team score rather than
    failing. That understates the team-stack layer, so the team-stack
    MAE below is a PESSIMISTIC read of the live model rather than a
    flattering one -- worth stating outright rather than quietly
    scoring a handicapped version.
    """
    out = []
    for i, r in enumerate(rows):
        raw = (r["position"] or "").upper()
        pos = raw.split("/")[0].strip()
        if pos in ("SP", "RP"):
            pos = "P"
        if pos not in ("P",) + HITTER_SLOTS:
            continue
        positions = [p.strip() for p in raw.split("/") if p.strip()] or [pos]
        positions = ["P" if p in ("SP", "RP") else p for p in positions]
        out.append(
            {
                "id": i,
                "name": r["name"],
                "normalized_name": r["normalized_name"],
                "position": pos,
                "positions": positions,
                "salary": r["salary"],
                "fpts": float(r["rotowire_fpts"]),
                "implied_runs": None,
                "team": r["team"],
                "rotowire_ownership_pct": r["rotowire_ownership_pct"],
            }
        )
    return out


def score(pairs, by_team):
    pred = [p for p, _ in pairs]
    real = [r for _, r in pairs]
    top = sorted(pairs, key=lambda x: -x[1])[:20]
    stacks = [
        (sum(v[0] for v in vs), sum(v[1] for v in vs))
        for vs in by_team.values()
        if len(vs) >= 3
    ]
    return {
        "n": len(pairs),
        "spearman": spearman(pred, real),
        "mae": mae(pairs),
        "chalk_mae": mae(top),
        "stack_mae": mae(stacks),
    }


async def main():
    cpr = await history_db.get_contest_player_results()
    real_by_date = defaultdict(dict)
    for r in cpr:
        if r.get("ownership_pct") is not None:
            real_by_date[str(r["date"])][r["normalized_name"]] = float(r["ownership_pct"])

    print(f"{'date':11} {'n':>4} | {'------- IN-HOUSE -------':^30} | {'------- ROTOWIRE -------':^30}")
    print(
        f"{'':11} {'':>4} | {'rho':>6} {'MAE':>6} {'chalk':>6} {'stack':>7} | "
        f"{'rho':>6} {'MAE':>6} {'chalk':>6} {'stack':>7}"
    )

    agg = {"inhouse": [], "rotowire": []}
    agg_team = {"inhouse": defaultdict(list), "rotowire": defaultdict(list)}
    skipped = []

    for day in sorted(real_by_date):
        rows = await archived_rows(day)
        if not rows:
            skipped.append(day)
            continue
        pool = build_pool(rows)
        real = real_by_date[day]
        own = inhouse_projections.project_ownership(pool)

        pairs_ih, pairs_rw = [], []
        team_ih, team_rw = defaultdict(list), defaultdict(list)
        for p in pool:
            r = real.get(p["normalized_name"])
            if r is None:
                continue
            ih = own.get(p["id"])
            if ih is not None:
                pairs_ih.append((ih, r))
                agg["inhouse"].append((ih, r))
                if p["position"] in HITTER_SLOTS and p["team"]:
                    team_ih[p["team"]].append((ih, r))
                    agg_team["inhouse"][f"{day}:{p['team']}"].append((ih, r))
            rw = p["rotowire_ownership_pct"]
            if rw is not None:
                pairs_rw.append((float(rw), r))
                agg["rotowire"].append((float(rw), r))
                if p["position"] in HITTER_SLOTS and p["team"]:
                    team_rw[p["team"]].append((float(rw), r))
                    agg_team["rotowire"][f"{day}:{p['team']}"].append((float(rw), r))

        if len(pairs_ih) < 10:
            skipped.append(day)
            continue
        a = score(pairs_ih, team_ih)
        b = score(pairs_rw, team_rw) if len(pairs_rw) >= 10 else None
        rw_txt = (
            f"{b['spearman']:>6.3f} {b['mae']:>6.2f} {b['chalk_mae']:>6.2f} {b['stack_mae']:>7.2f}"
            if b
            else f"{'-':>6} {'-':>6} {'-':>6} {'-':>7}"
        )
        print(
            f"{day:11} {a['n']:>4} | {a['spearman']:>6.3f} {a['mae']:>6.2f} "
            f"{a['chalk_mae']:>6.2f} {a['stack_mae']:>7.2f} | {rw_txt}"
        )

    print()
    for key in ("inhouse", "rotowire"):
        if not agg[key]:
            continue
        s = score(agg[key], agg_team[key])
        print(
            f"ALL {key:9} n={s['n']:>5}  rho={s['spearman']:.3f}  MAE={s['mae']:.2f}  "
            f"chalk MAE={s['chalk_mae']:.2f}  stack MAE={s['stack_mae']:.2f}"
        )

    if skipped:
        print(f"\nskipped (no archived salary/projection rows): {', '.join(skipped)}")

    print("\nin-house calibration by REAL ownership bucket:")
    for lo, hi in [(0, 1), (1, 3), (3, 6), (6, 10), (10, 20), (20, 40), (40, 101)]:
        sel = [(p, r) for p, r in agg["inhouse"] if lo <= r < hi]
        if not sel:
            continue
        mp = sum(p for p, _ in sel) / len(sel)
        mr = sum(r for _, r in sel) / len(sel)
        print(
            f"  real {lo:>3}-{hi:<3}%  n={len(sel):>5}  real avg {mr:>5.1f}%  "
            f"predicted avg {mp:>5.1f}%  bias {mp - mr:>+5.1f}"
        )


if __name__ == "__main__":
    asyncio.run(main())
