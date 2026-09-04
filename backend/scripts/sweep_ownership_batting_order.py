"""
Fit _BATTING_ORDER_WEIGHT against every real archived slate.

diagnose_ownership_multislate.py showed the model carried a monotone
ownership bias by lineup slot because batting order fed the FPTS
projection and nothing else. This sweeps the weight of the term that
fixes it and reports what each value does to the error, so the constant
that ships is measured rather than picked.

Loads the archived slates once and reprojects at every candidate weight,
because the fetching is the slow part and the projection is cheap.

Scored on three things, not one:
  MAE            average miss, in points of ownership
  slot-1 bias    the worst-affected bucket
  gradient       spread between the best- and worst-biased slot, which
                 is what "systematic" actually means here -- a model
                 evenly wrong everywhere is far less damaging than one
                 wrong in a direction that tracks a feature, because the
                 second kind builds whole lineups nobody else owns

Usage (from backend/):
    .venv/Scripts/python.exe -m scripts.sweep_ownership_batting_order
"""

from __future__ import annotations

import asyncio
import importlib
import os
import statistics
from datetime import date as date_cls
from typing import Any

from app import history_db
from app.clients.http import close_client
from scripts.diagnose_ownership_multislate import HITTER_SLOTS, batting_orders

WEIGHTS = [0.0, 0.2, 0.35, 0.5, 0.55, 0.65, 0.8, 1.0, 1.25, 1.5, 2.0]


async def collect() -> list[dict[str, Any]]:
    """Every archived slate's rebuilt pool plus the real ownership for it."""
    pool = await history_db._get_pool()
    if not pool:
        print("No SUPABASE_DB_URL -- nothing to fit against.")
        return []

    slates: list[dict[str, Any]] = []
    async with pool.acquire() as conn:
        proj_dates = {str(r["date"]) for r in await conn.fetch("select distinct date from slate_projections")}
        cont_dates = {str(r["date"]) for r in await conn.fetch("select distinct date from contest_player_results")}
        for day in sorted(proj_dates & cont_dates):
            dparam = date_cls.fromisoformat(day)
            players = await conn.fetch("select * from slate_projections where date = $1", dparam)
            context = await conn.fetch(
                "select team, implied_runs from slate_team_context where date = $1", dparam)
            contests = await conn.fetch(
                "select contest_id, normalized_name, ownership_pct "
                "from contest_player_results where date = $1", dparam)

            implied = {r["team"]: float(r["implied_runs"]) for r in context if r["implied_runs"]}
            orders = await batting_orders(day)
            archived = {p["normalized_name"] for p in players}

            by_contest: dict[str, dict[str, float]] = {}
            for r in contests:
                if r["ownership_pct"] is not None:
                    by_contest.setdefault(r["contest_id"], {})[r["normalized_name"]] = float(r["ownership_pct"])
            if not by_contest:
                continue
            best = max(by_contest, key=lambda c: (len(set(by_contest[c]) & archived), len(by_contest[c])))
            real_own = by_contest[best]

            rows = []
            for i, p in enumerate(players):
                pos = (p["position"] or "").upper()
                if not pos or not p["salary"]:
                    continue
                fpts = p["inhouse_fpts"] if p["inhouse_fpts"] is not None else p["rotowire_fpts"]
                if fpts is None:
                    continue
                rows.append({
                    "id": i, "normalized_name": p["normalized_name"], "position": pos,
                    "team": p["team"], "salary": float(p["salary"]), "fpts": float(fpts),
                    "implied_runs": implied.get(p["team"]),
                    "batting_order": orders.get(p["normalized_name"]),
                    "projected_batting_order": None,
                })
            if rows:
                slates.append({"day": day, "pool": rows, "real": real_own})

    await close_client()
    return slates


def score(slates: list[dict[str, Any]], weight: float) -> dict[str, Any]:
    os.environ["DFS_BATTING_ORDER_WEIGHT"] = str(weight)
    import app.services.inhouse_projections as ip
    importlib.reload(ip)

    by_slot: dict[Any, list[float]] = {}
    errs: list[float] = []
    for s in slates:
        projected = ip.project_ownership(s["pool"])
        for p in s["pool"]:
            actual = s["real"].get(p["normalized_name"])
            if actual is None:
                continue
            if not (set(p["position"].replace("/", " ").split()) & HITTER_SLOTS):
                continue
            err = projected.get(p["id"], 0.0) - actual
            errs.append(err)
            by_slot.setdefault(p["batting_order"] or "none", []).append(err)

    slot_bias = {k: statistics.mean(v) for k, v in by_slot.items()}
    real_slots = [slot_bias[k] for k in by_slot if isinstance(k, int)]
    return {
        "weight": weight,
        "n": len(errs),
        "mae": statistics.mean(abs(e) for e in errs),
        "bias": statistics.mean(errs),
        "slot1": slot_bias.get(1, 0.0),
        "gradient": max(real_slots) - min(real_slots) if real_slots else 0.0,
    }


async def main() -> None:
    slates = await collect()
    if not slates:
        return
    n = sum(len(s["pool"]) for s in slates)
    print(f"{len(slates)} slates, {n} pool rows\n")
    print(f"{'weight':>7} {'n':>6} {'MAE':>7} {'bias':>8} {'slot-1 bias':>12} {'gradient':>10}")
    results = []
    for w in WEIGHTS:
        r = score(slates, w)
        results.append(r)
        print(f"{r['weight']:7.2f} {r['n']:6} {r['mae']:7.3f} {r['bias']:+8.3f} "
              f"{r['slot1']:+12.3f} {r['gradient']:10.3f}")

    best_mae = min(results, key=lambda r: r["mae"])
    best_grad = min(results, key=lambda r: r["gradient"])
    print(f"\nlowest MAE      : weight {best_mae['weight']} (MAE {best_mae['mae']:.3f})")
    print(f"flattest gradient: weight {best_grad['weight']} (gradient {best_grad['gradient']:.3f})")
    os.environ.pop("DFS_BATTING_ORDER_WEIGHT", None)


if __name__ == "__main__":
    asyncio.run(main())
