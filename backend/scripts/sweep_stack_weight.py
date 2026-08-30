"""Sweeps the team-stack layer's weights against real archived DK
contest standings, reporting the metrics that actually matter (stack
MAE and chalk MAE) rather than global rank correlation alone -- global
Spearman sat at a respectable +0.60 while the model was missing a real
154%-owned stack by over 100pp, which is exactly why it can't be the
headline metric.

Run from backend/:  .venv/Scripts/python.exe -m scripts.sweep_stack_weight
"""

from __future__ import annotations

import asyncio
import itertools
from collections import defaultdict

from app import history_db
from app.services import inhouse_projections as ip
from app.services import mlb_slate, player_match


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


def _spearman(xs, ys):
    if len(xs) < 3:
        return None
    rx, ry = _rank(xs), _rank(ys)
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    vx = sum((x - mx) ** 2 for x in rx)
    vy = sum((y - my) ** 2 for y in ry)
    if vx == 0 or vy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(rx, ry)) / (vx**0.5 * vy**0.5)


async def _slate_data(day: str):
    """(pool, name->team/kind, ) rebuilt once per date and reused for
    every weight combination -- rebuilding the slate per sweep step
    would make this take hours instead of a minute."""
    slate = await mlb_slate.build_slate(day, include_hitters=True, include_inhouse=True)
    meta = {}
    for game in slate.get("games", []):
        for side in ("home", "away"):
            team = game[side]["abbrev"]
            for h in game[side].get("hitters") or []:
                meta[player_match.normalize_name(h["name"])] = {"id": h["id"], "team": team, "kind": "H"}
            p = game[side].get("probable_pitcher")
            if p:
                meta[player_match.normalize_name(p["name"])] = {"id": p["id"], "team": team, "kind": "P"}
    return slate, meta


def _rebuild_pool(slate):
    """Reconstructs exactly the pool mlb_slate feeds project_ownership."""
    pool = []
    for g in slate.get("games", []):
        for side in ("home", "away"):
            opp_side = "away" if side == "home" else "home"
            opp_p = g[opp_side].get("probable_pitcher")
            opp_id = opp_p.get("id") if opp_p else None
            for h in mlb_slate._ownership_eligible_hitters(g[side].get("hitters") or []):
                proj = h.get("projection") or {}
                sal = h.get("salary")
                if proj.get("inhouse_fpts") is not None and sal:
                    pool.append({
                        "id": h["id"],
                        "position": mlb_slate._dk_slot_position(sal.get("position"), h["position"]),
                        "positions": mlb_slate._dk_slot_positions(sal.get("position"), h["position"]),
                        "salary": sal["salary"], "fpts": proj["inhouse_fpts"],
                        "implied_runs": g[side]["implied_runs"],
                        "opponent_pitcher_id": opp_id, "team": g[side]["abbrev"],
                    })
            p = g[side].get("probable_pitcher")
            if p and p.get("edge"):
                proj = p.get("projection") or {}
                sal = p.get("salary")
                if proj.get("inhouse_fpts") is not None and sal:
                    pool.append({
                        "id": p["id"], "position": "P", "salary": sal["salary"],
                        "fpts": proj["inhouse_fpts"], "implied_runs": g[side]["implied_runs"],
                    })
    return pool


async def main() -> int:
    rows = await history_db.get_contest_player_results()
    by_contest = defaultdict(list)
    for r in rows:
        by_contest[(r["date"], r["contest_id"], r["contest_name"])].append(r)

    slates = []
    for (day, cid, name), players in sorted(by_contest.items()):
        try:
            slate, meta = await _slate_data(day.isoformat())
        except Exception:
            continue
        pool = _rebuild_pool(slate)
        if len(pool) < 30:
            continue
        actual = {}
        for p in players:
            m = meta.get(p["normalized_name"])
            if m and p["ownership_pct"] is not None:
                actual[m["id"]] = (float(p["ownership_pct"]), m["team"], m["kind"])
        if len(actual) < 20:
            continue
        slates.append((f"{day} {name}", pool, actual))

    print(f"{len(slates)} usable slate(s)\n")
    if not slates:
        return 1

    def evaluate():
        chalk, stack, spear = [], [], []
        for _, pool, actual in slates:
            model = ip.project_ownership(pool)
            pairs = [(a, model[i], t, k) for i, (a, t, k) in actual.items() if i in model]
            if len(pairs) < 10:
                continue
            top = sorted(pairs, key=lambda x: -x[0])[:20]
            chalk += [abs(m - a) for a, m, _, _ in top]
            r = _spearman([a for a, _, _, _ in pairs], [m for _, m, _, _ in pairs])
            if r is not None:
                spear.append(r)
            at, mt = defaultdict(float), defaultdict(float)
            for a, m, t, k in pairs:
                if k == "H":
                    at[t] += a
                    mt[t] += m
            stack += [abs(mt[t] - at[t]) for t in at]
        return (sum(chalk) / len(chalk), sum(stack) / len(stack), sum(spear) / len(spear))

    base = (ip._TEAM_STACK_WEIGHT, ip._STACK_IMPLIED_RUNS_WEIGHT, ip._STACK_OPPOSING_SP_WEIGHT, ip._SOFTMAX_TEMPERATURE)

    ip._TEAM_STACK_WEIGHT = 0.0
    c, s, r = evaluate()
    print(f"{'BASELINE (no stack layer)':<44} chalkMAE={c:5.2f}  stackMAE={s:6.2f}  spearman={r:+.3f}")

    print()
    results = []
    for w, imp, temp in itertools.product(
        [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0],
        [0.5, 0.6, 0.7, 0.8, 1.0],
        [0.8, 0.9, 1.0, 1.2, 1.4, 1.7, 2.0],
    ):
        ip._TEAM_STACK_WEIGHT = w
        ip._STACK_IMPLIED_RUNS_WEIGHT = imp
        ip._STACK_OPPOSING_SP_WEIGHT = 1.0 - imp
        ip._SOFTMAX_TEMPERATURE = temp
        c, s, r = evaluate()
        results.append((s, c, r, w, imp, temp))

    results.sort()
    print("top 15 by stack MAE:")
    print(f"  {'stackW':>6} {'impliedW':>8} {'temp':>5}   {'chalkMAE':>8} {'stackMAE':>8} {'spearman':>8}")
    for s, c, r, w, imp, temp in results[:15]:
        print(f"  {w:>6.1f} {imp:>8.2f} {temp:>5.2f}   {c:>8.2f} {s:>8.2f} {r:>+8.3f}")

    print("\ntop 15 by chalk MAE:")
    for s, c, r, w, imp, temp in sorted(results, key=lambda x: x[1])[:15]:
        print(f"  {w:>6.1f} {imp:>8.2f} {temp:>5.2f}   {c:>8.2f} {s:>8.2f} {r:>+8.3f}")

    ip._TEAM_STACK_WEIGHT, ip._STACK_IMPLIED_RUNS_WEIGHT, ip._STACK_OPPOSING_SP_WEIGHT, ip._SOFTMAX_TEMPERATURE = base
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
