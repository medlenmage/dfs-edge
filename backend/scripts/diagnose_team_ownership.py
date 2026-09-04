"""
What real team-level ownership looks like, and how far the model is off.

The per-player diagnostic (diagnose_ownership_multislate.py) measures each
hitter against his own real %Drafted. That misses the failure that
actually shows up on a board: MLB ownership is spent TEAM-first -- the
field picks stacks and then picks bats out of them -- so the model can be
roughly right per player and still put 144% of an 800% hitter pool on one
team, which no real field ever does.

Three things measured here, one per claim worth testing:

  TEAM TOTAL     summed hitter ownership per team, model vs real. Tests
                 whether a per-team cap is needed and where it belongs.
  SPREAD         top-team share and max/min ratio. Tests whether the
                 environment weighting is too aggressive -- if the real
                 field's best-to-worst ratio is 10x and the model's is
                 80x, that is a temperature problem, not a ranking one.
  WITHIN-TEAM    each lineup slot's share OF ITS OWN TEAM's total. Tests
                 whether batting-order decay needs to apply inside a
                 stack, which the global term cannot do: a 9-hole hitter
                 on a chalky team currently inherits that team's whole
                 environment bonus.

Real ownership comes from one contest per date, chosen the same way as
the per-player diagnostic -- see its docstring for why averaging across
contests on one date is wrong.

Usage (from backend/):
    .venv/Scripts/python.exe -m scripts.diagnose_team_ownership
"""

from __future__ import annotations

import asyncio
import statistics
from collections import defaultdict
from datetime import date as date_cls
from typing import Any

from app import history_db
from app.clients.http import close_client
from app.services import inhouse_projections
from scripts.diagnose_ownership_multislate import HITTER_SLOTS, batting_orders


def spread(values: list[float]) -> dict[str, float]:
    v = sorted(values, reverse=True)
    if not v:
        return {}
    total = sum(v) or 1.0
    return {
        "top": v[0],
        "top_share": 100 * v[0] / total,
        "bottom8": statistics.mean(v[-8:]) if len(v) >= 8 else statistics.mean(v),
        "ratio": v[0] / max(v[-1], 0.01),
        "total": total,
    }


async def main() -> None:
    pool = await history_db._get_pool()
    if not pool:
        print("No SUPABASE_DB_URL.")
        return

    real_teams: list[dict[str, float]] = []
    model_teams: list[dict[str, float]] = []
    real_slot_share: dict[int, list[float]] = defaultdict(list)
    model_slot_share: dict[int, list[float]] = defaultdict(list)
    per_slate: list[tuple[str, dict, dict]] = []

    async with pool.acquire() as conn:
        pd = {str(r["date"]) for r in await conn.fetch("select distinct date from slate_projections")}
        cd = {str(r["date"]) for r in await conn.fetch("select distinct date from contest_player_results")}
        for day in sorted(pd & cd):
            dp = date_cls.fromisoformat(day)
            players = await conn.fetch("select * from slate_projections where date = $1", dp)
            context = await conn.fetch(
                "select team, implied_runs from slate_team_context where date = $1", dp)
            contests = await conn.fetch(
                "select contest_id, normalized_name, ownership_pct "
                "from contest_player_results where date = $1", dp)

            implied = {r["team"]: float(r["implied_runs"]) for r in context if r["implied_runs"]}
            orders = await batting_orders(day)
            archived = {p["normalized_name"] for p in players}

            by_contest: dict[str, dict[str, float]] = defaultdict(dict)
            for r in contests:
                if r["ownership_pct"] is not None:
                    by_contest[r["contest_id"]][r["normalized_name"]] = float(r["ownership_pct"])
            if not by_contest:
                continue
            best = max(by_contest, key=lambda c: (len(set(by_contest[c]) & archived), len(by_contest[c])))
            real_own = by_contest[best]

            rows = []
            for i, p in enumerate(players):
                pos = (p["position"] or "").upper()
                if not pos or not p["salary"]:
                    continue
                if not (set(pos.replace("/", " ").split()) & HITTER_SLOTS):
                    continue
                fpts = p["inhouse_fpts"] if p["inhouse_fpts"] is not None else p["rotowire_fpts"]
                if fpts is None:
                    continue
                rows.append({
                    "id": i, "normalized_name": p["normalized_name"], "position": pos,
                    "positions": pos.replace("/", " ").split(),
                    "team": p["team"], "salary": float(p["salary"]), "fpts": float(fpts),
                    "implied_runs": implied.get(p["team"]),
                    "batting_order": orders.get(p["normalized_name"]),
                    "projected_batting_order": None,
                })
            if not rows:
                continue

            projected = inhouse_projections.project_ownership(rows)

            r_team: dict[str, float] = defaultdict(float)
            m_team: dict[str, float] = defaultdict(float)
            r_slot: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
            m_slot: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
            for p in rows:
                team = p["team"]
                m = projected.get(p["id"], 0.0)
                m_team[team] += m
                a = real_own.get(p["normalized_name"])
                if a is not None:
                    r_team[team] += a
                slot = p["batting_order"]
                if slot:
                    m_slot[team][slot] += m
                    if a is not None:
                        r_slot[team][slot] += a

            # Only teams the contest actually covered, so a team whose
            # players simply weren't in the export can't read as 0%.
            covered = {t for t in r_team if r_team[t] > 0}
            if len(covered) < 4:
                continue
            rs = spread([r_team[t] for t in covered])
            ms = spread([m_team[t] for t in covered])
            real_teams.append(rs)
            model_teams.append(ms)
            per_slate.append((day, rs, ms))

            for team in covered:
                rt = sum(r_slot[team].values())
                mt = sum(m_slot[team].values())
                for slot in range(1, 10):
                    if rt > 0 and r_slot[team].get(slot):
                        real_slot_share[slot].append(100 * r_slot[team][slot] / rt)
                    if mt > 0 and m_slot[team].get(slot):
                        model_slot_share[slot].append(100 * m_slot[team][slot] / mt)

    await close_client()

    if not real_teams:
        print("Nothing matched.")
        return

    print(f"{len(per_slate)} slates\n")
    print(f"{'date':12} {'real top':>9} {'model top':>10} {'real ratio':>11} {'model ratio':>12}")
    for day, rs, ms in per_slate:
        print(f"{day:12} {rs['top']:9.1f} {ms['top']:10.1f} {rs['ratio']:11.1f} {ms['ratio']:12.1f}")

    def agg(rows: list[dict], key: str) -> float:
        return statistics.mean(r[key] for r in rows if key in r)

    print(f"\n{'':16}{'REAL':>10} {'MODEL':>10}")
    for label, key in (
        ("top team %", "top"),
        ("top team share", "top_share"),
        ("bottom-8 mean", "bottom8"),
        ("max/min ratio", "ratio"),
    ):
        print(f"{label:16}{agg(real_teams, key):10.1f} {agg(model_teams, key):10.1f}")

    print("\nWITHIN-TEAM: each slot's share of its own team's total ownership")
    print(f"  {'slot':>5} {'real %':>9} {'model %':>9} {'model/real':>11}")
    for slot in range(1, 10):
        r = real_slot_share.get(slot)
        m = model_slot_share.get(slot)
        if not r or not m:
            continue
        rm, mm = statistics.mean(r), statistics.mean(m)
        print(f"  {slot:5} {rm:9.2f} {mm:9.2f} {mm/rm:11.2f}x")


if __name__ == "__main__":
    asyncio.run(main())
