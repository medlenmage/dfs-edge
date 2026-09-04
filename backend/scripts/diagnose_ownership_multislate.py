"""
Where the in-house ownership model is biased, measured across EVERY real
archived slate rather than one.

The existing diagnose_ownership.py works a slate at a time, which is how
a bias gets spotted but not how it gets trusted -- a single board can put
a whole stack in one bucket and make a team-level miss look like a
salary-tier law. This rebuilds each archived slate's real pool from
Supabase (salary, projections, implied runs), refetches the CONFIRMED
batting orders from the MLB API (a settled historical fact, and the one
model input that was never archived), reprojects ownership with the
current model, and pools the errors.

Reported by salary tier and by batting-order slot, because those are the
two cuts a projection is most likely to be systematically wrong on, and
systematic error is the kind worth fixing -- a model that is 3 points
high on everyone is fine, one that is 12 points high on cheap bottom-
of-the-order bats is buying lineups nobody else is buying.

Real ownership comes from ONE contest per date, not an average across
all of them. Two contests on the same calendar date are routinely
different SLATES -- DK runs "(Early)", "(Night)" and "(Turbo)" contests
over a subset of the day's games -- and a player in a 4-game early slate
carries far higher ownership than the same player on the 13-game main
slate. Averaging those together invents a number neither field produced.
The contest kept per date is the one whose player pool best covers the
archived slate, with field size breaking ties.

This was not a hypothetical: averaging first put six of the eight worst
"misses" on 2026-08-19, which had a main slate and an (Early) contest
archived together.

Usage (from backend/):
    .venv/Scripts/python.exe -m scripts.diagnose_ownership_multislate
"""

from __future__ import annotations

import asyncio
import statistics
from collections import defaultdict
from datetime import date as date_cls
from typing import Any

from app import history_db
from app.clients import mlb
from app.clients.http import close_client
from app.services import inhouse_projections, player_match

# DK hitter slots. Pitchers are excluded throughout: pitcher ownership is
# a separate, already-better-calibrated group, and batting order is
# meaningless for them.
HITTER_SLOTS = {"C", "1B", "2B", "3B", "SS", "OF"}

SALARY_TIERS = [
    ("<$2.5K", 0, 2500),
    ("$2.5-3K", 2500, 3000),
    ("$3-4K", 3000, 4000),
    ("$4-5K", 4000, 5000),
    ("$5-6K", 5000, 6000),
    ("$6K+", 6000, 10**9),
]


def tier_of(salary: float) -> str:
    for label, lo, hi in SALARY_TIERS:
        if lo <= salary < hi:
            return label
    return "?"


async def batting_orders(day: str) -> dict[str, int]:
    """normalized name -> confirmed batting-order slot (1-9) for that day."""
    try:
        games = await mlb.get_schedule(day)
    except Exception:  # noqa: BLE001
        return {}
    order_by_id: dict[int, int] = {}
    for g in games:
        gp = g.get("gamePk")
        if not gp:
            continue
        try:
            lu = await mlb.get_lineups(gp)
        except Exception:  # noqa: BLE001
            continue
        for side in ("home", "away"):
            for slot, pid in enumerate(lu.get(side) or [], start=1):
                order_by_id.setdefault(pid, slot)
    if not order_by_id:
        return {}
    people = await mlb.get_people(list(order_by_id))
    return {
        player_match.normalize_name(bio["name"]): order_by_id[pid]
        for pid, bio in people.items()
        if bio.get("name")
    }


async def main() -> None:
    pool = await history_db._get_pool()
    if not pool:
        print("No SUPABASE_DB_URL -- nothing archived to measure against.")
        return

    async with pool.acquire() as conn:
        proj_dates = {str(r["date"]) for r in await conn.fetch("select distinct date from slate_projections")}
        cont_dates = {str(r["date"]) for r in await conn.fetch("select distinct date from contest_player_results")}
        days = sorted(proj_dates & cont_dates)
        print(f"{len(days)} slates with BOTH archived salaries and real contest results: {days}\n")

        rows: list[dict[str, Any]] = []
        for day in days:
            # asyncpg binds a real date, not an ISO string.
            dparam = date_cls.fromisoformat(day)
            players = await conn.fetch(
                "select * from slate_projections where date = $1", dparam
            )
            context = await conn.fetch(
                "select team, implied_runs from slate_team_context where date = $1", dparam
            )
            contests = await conn.fetch(
                "select contest_id, contest_name, normalized_name, ownership_pct "
                "from contest_player_results where date = $1", dparam
            )
            implied = {r["team"]: float(r["implied_runs"]) for r in context if r["implied_runs"]}
            orders = await batting_orders(day)

            # One contest, not a blend of several -- see the module
            # docstring. "Best covers the archived slate" means the
            # largest share of that contest's players appear in the
            # archived pool AND vice versa, so an early-slate contest
            # scored against a main-slate archive loses to the main one.
            archived_names = {p["normalized_name"] for p in players}
            by_contest: dict[str, dict[str, float]] = defaultdict(dict)
            contest_label: dict[str, str] = {}
            for r in contests:
                if r["ownership_pct"] is None:
                    continue
                by_contest[r["contest_id"]][r["normalized_name"]] = float(r["ownership_pct"])
                contest_label[r["contest_id"]] = r["contest_name"]
            if not by_contest:
                print(f"  {day}: no contest ownership, skipped")
                continue
            # Maximize how much of the ARCHIVED slate the contest covers,
            # not how much of the contest the archive covers. Those are
            # different, and the second one is wrong: a 4-game (Early)
            # contest has nearly all of its players inside a 13-game
            # archived pool, so it scores a near-perfect ratio while
            # representing a completely different slate. Getting this
            # backwards is exactly what put 2026-08-19's (Early) contest
            # up against the full day's board -- 96 of 550 players
            # matched, and six of the eight worst apparent misses.
            best_id = max(
                by_contest,
                key=lambda cid: (len(set(by_contest[cid]) & archived_names), len(by_contest[cid])),
            )
            real_own = by_contest[best_id]
            chosen = contest_label.get(best_id, best_id)

            # Rebuild the pool project_ownership() expects.
            pool_rows = []
            for i, p in enumerate(players):
                pos = (p["position"] or "").upper()
                slots = [s for s in pos.replace("/", " ").split() if s]
                if not slots or not p["salary"]:
                    continue
                fpts = p["inhouse_fpts"] if p["inhouse_fpts"] is not None else p["rotowire_fpts"]
                if fpts is None:
                    continue
                pool_rows.append({
                    "id": i,
                    "name": p["name"],
                    "normalized_name": p["normalized_name"],
                    "position": pos,
                    "team": p["team"],
                    "salary": float(p["salary"]),
                    "fpts": float(fpts),
                    "implied_runs": implied.get(p["team"]),
                    "batting_order": orders.get(p["normalized_name"]),
                    "projected_batting_order": None,
                })
            if not pool_rows:
                print(f"  {day}: no usable pool, skipped")
                continue

            projected = inhouse_projections.project_ownership(pool_rows)
            matched = 0
            for p in pool_rows:
                actual = real_own.get(p["normalized_name"])
                if actual is None:
                    continue
                slots = set(p["position"].replace("/", " ").split())
                if not (slots & HITTER_SLOTS):
                    continue
                matched += 1
                rows.append({
                    "day": day, "name": p["name"], "salary": p["salary"],
                    "order": p["batting_order"],
                    "proj": projected.get(p["id"], 0.0), "actual": actual,
                })
            print(f"  {day}: {len(pool_rows):3} pool, {matched:3} matched, {len(orders):3} batting slots"
                  f"  | {len(by_contest)} contest(s), used: {chosen[:44]}")

    await close_client()

    if not rows:
        print("\nNothing matched.")
        return

    errs = [r["proj"] - r["actual"] for r in rows]
    print(f"\n{'='*74}\n{len(rows)} hitter-slate observations across {len(days)} slates\n{'='*74}")
    print(f"OVERALL  mean error {statistics.mean(errs):+6.2f}pp   "
          f"median {statistics.median(errs):+6.2f}pp   MAE {statistics.mean(abs(e) for e in errs):5.2f}pp")

    def report(title: str, key, order=None):
        print(f"\n{title}")
        print(f"  {'bucket':12} {'n':>5} {'proj':>7} {'actual':>7} {'bias':>8} {'MAE':>7}")
        groups: dict[Any, list[dict]] = defaultdict(list)
        for r in rows:
            groups[key(r)].append(r)
        keys = order or sorted(groups, key=lambda k: (k is None, k))
        for k in keys:
            g = groups.get(k)
            if not g:
                continue
            p = statistics.mean(x["proj"] for x in g)
            a = statistics.mean(x["actual"] for x in g)
            mae = statistics.mean(abs(x["proj"] - x["actual"]) for x in g)
            flag = "  <<<" if abs(p - a) > 4 else ""
            print(f"  {str(k):12} {len(g):5} {p:7.2f} {a:7.2f} {p-a:+8.2f} {mae:7.2f}{flag}")

    report("BY SALARY TIER", lambda r: tier_of(r["salary"]), [t[0] for t in SALARY_TIERS])
    report("BY BATTING-ORDER SLOT", lambda r: r["order"] if r["order"] else "unknown",
           [1, 2, 3, 4, 5, 6, 7, 8, 9, "unknown"])

    # The specific rule proposed: cheap bottom-of-the-order bats.
    cheap_bottom = [r for r in rows if r["order"] and r["order"] >= 7 and r["salary"] < 3000]
    if cheap_bottom:
        over15 = [r for r in cheap_bottom if r["proj"] > 15]
        actual_over15 = [r for r in cheap_bottom if r["actual"] > 15]
        print(f"\nHITTERS BATTING 7-9 UNDER $3,000  (n={len(cheap_bottom)})")
        print(f"  model projects  mean {statistics.mean(r['proj'] for r in cheap_bottom):5.2f}%  "
              f"max {max(r['proj'] for r in cheap_bottom):5.2f}%")
        print(f"  really drafted  mean {statistics.mean(r['actual'] for r in cheap_bottom):5.2f}%  "
              f"max {max(r['actual'] for r in cheap_bottom):5.2f}%")
        print(f"  model says >15% owned: {len(over15):3}   really >15% owned: {len(actual_over15):3}")
        if actual_over15:
            print("  real players who DID clear 15% (so a hard cap would have been wrong):")
            for r in sorted(actual_over15, key=lambda r: -r["actual"])[:6]:
                print(f"    {r['name'][:22]:22} {r['day']}  bat {r['order']}  ${r['salary']:.0f}  "
                      f"real {r['actual']:5.1f}%  model {r['proj']:5.1f}%")

    print("\nWORST 12 OVERPROJECTIONS")
    for r in sorted(rows, key=lambda r: -(r["proj"] - r["actual"]))[:12]:
        print(f"  {r['name'][:22]:22} {r['day']}  bat {str(r['order'] or '-'):>2}  ${r['salary']:6.0f}  "
              f"model {r['proj']:5.1f}%  real {r['actual']:5.1f}%  ({r['proj']-r['actual']:+5.1f})")
    print("\nWORST 8 UNDERPROJECTIONS")
    for r in sorted(rows, key=lambda r: r["proj"] - r["actual"])[:8]:
        print(f"  {r['name'][:22]:22} {r['day']}  bat {str(r['order'] or '-'):>2}  ${r['salary']:6.0f}  "
              f"model {r['proj']:5.1f}%  real {r['actual']:5.1f}%  ({r['proj']-r['actual']:+5.1f})")


if __name__ == "__main__":
    asyncio.run(main())
