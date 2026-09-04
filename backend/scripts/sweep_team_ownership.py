"""
Fit _TEAM_STACK_WEIGHT against real archived team-level ownership.

diagnose_team_ownership.py showed the model concentrates far harder than
a real field does: the top team lands at 152.8% against a real 122.3%,
and -- the bigger error -- the bottom eight teams get 13.7% against a
real 21.6%. The team-stack term is the lever, because it is worth
_TEAM_STACK_WEIGHT points of score spread between the best and worst
team, and a softmax turns that into an exponential ownership ratio.

Sweeps that weight and scores each value against three real targets at
once. Ranked on the SUM of relative errors rather than any single one,
because it is easy to nail the top team by starving everything else --
the failure the model already has.

Collects the archive once and reprojects, since fetching is the slow part.

Usage (from backend/):
    .venv/Scripts/python.exe -m scripts.sweep_team_ownership
"""

from __future__ import annotations

import asyncio
import importlib
import os
import statistics
from collections import defaultdict
from datetime import date as date_cls
from typing import Any

from app import history_db
from app.clients.http import close_client
from scripts.diagnose_ownership_multislate import HITTER_SLOTS, batting_orders
from scripts.diagnose_team_ownership import spread

WEIGHTS = ["4.0", "3.0", "2.5", "2.0", "1.6", "1.3", "1.0", "0.8", "0.5"]


async def collect() -> list[dict[str, Any]]:
    pool = await history_db._get_pool()
    if not pool:
        return []
    slates = []
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
                    "positions": pos.replace("/", " ").split(), "team": p["team"],
                    "salary": float(p["salary"]), "fpts": float(fpts),
                    "implied_runs": implied.get(p["team"]),
                    "batting_order": orders.get(p["normalized_name"]),
                    "projected_batting_order": None,
                })
            if rows:
                slates.append({"day": day, "pool": rows, "real": by_contest[best]})
    await close_client()
    return slates


def measure(slates: list[dict[str, Any]], ip) -> dict[str, float]:
    reals, models = [], []
    for s in slates:
        projected = ip.project_ownership(s["pool"])
        r_team: dict[str, float] = defaultdict(float)
        m_team: dict[str, float] = defaultdict(float)
        for p in s["pool"]:
            m_team[p["team"]] += projected.get(p["id"], 0.0)
            a = s["real"].get(p["normalized_name"])
            if a is not None:
                r_team[p["team"]] += a
        covered = {t for t in r_team if r_team[t] > 0}
        if len(covered) < 4:
            continue
        reals.append(spread([r_team[t] for t in covered]))
        models.append(spread([m_team[t] for t in covered]))
    out = {}
    for key in ("top", "top_share", "bottom8"):
        out[f"real_{key}"] = statistics.mean(r[key] for r in reals)
        out[f"model_{key}"] = statistics.mean(m[key] for m in models)
    return out


async def main() -> None:
    slates = await collect()
    if not slates:
        print("No archive.")
        return
    import app.services.inhouse_projections as ip

    print(f"{len(slates)} slates\n")
    print(f"{'weight':>7} {'top':>8} {'top%':>7} {'bot8':>8} {'total err':>10}")
    results = []
    for w in WEIGHTS:
        os.environ["DFS_TEAM_STACK_WEIGHT"] = w
        importlib.reload(ip)
        m = measure(slates, ip)
        err = sum(
            abs(m[f"model_{k}"] - m[f"real_{k}"]) / m[f"real_{k}"]
            for k in ("top", "top_share", "bottom8")
        )
        results.append((err, w, m))
        print(f"{float(w):7.1f} {m['model_top']:8.1f} {m['model_top_share']:7.1f} "
              f"{m['model_bottom8']:8.1f} {err:10.3f}")
    m = results[0][2]
    print(f"{'REAL':>7} {m['real_top']:8.1f} {m['real_top_share']:7.1f} {m['real_bottom8']:8.1f}")
    best = min(results)
    print(f"\nbest: weight {best[1]} (total relative error {best[0]:.3f})")
    os.environ.pop("DFS_TEAM_STACK_WEIGHT", None)


if __name__ == "__main__":
    asyncio.run(main())
