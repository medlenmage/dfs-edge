"""Diagnostic: WHERE does in-house ownership error actually live?

backtest_ownership.py reports one number -- global Spearman rank
correlation across the whole pool. That number is genuinely misleading
on its own, and this script exists because of a real case where it was:
at a respectable-looking r=+0.601, the model was simultaneously missing
an entire real 154%-owned CLE stack by more than 100pp. A metric that
averages over 250 near-zero-owned players will always look fine while
the handful of players who decide a contest are badly wrong.

So this reports the breakdowns that actually matter:

  - chalk MAE      -- restricted to the top 20 by REAL ownership, i.e.
                      the players where money is actually won or lost
  - team-stack MAE -- summed hitter ownership per team, the quantity
                      MLB field behavior is really organized around
  - pitcher MAE    -- separately, since it's a tiny pool with huge
                      concentration and its own model
  - calibration by bucket -- the compression pattern (models
                      systematically under-call chalk and over-call the
                      long tail); read the `bias` column
  - per-slate worst chalk misses, named, so a regression is diagnosable
    rather than just visible as a number moving

Run from backend/:  .venv/Scripts/python.exe -m scripts.diagnose_ownership
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from app import history_db
from app.services import mlb_slate, player_match


async def _model_by_name(day: str):
    """normalized_name -> (own_pct, team, position, salary, fpts)"""
    slate = await mlb_slate.build_slate(day, include_hitters=True, include_inhouse=True)
    out = {}
    for game in slate.get("games", []):
        for side in ("home", "away"):
            team = game[side]["abbrev"]
            for h in game[side]["hitters"]:
                proj = h.get("projection") or {}
                own = proj.get("inhouse_ownership_pct")
                if own is not None:
                    out[player_match.normalize_name(h["name"])] = {
                        "own": own, "team": team, "kind": "H",
                        "salary": (h.get("salary") or {}).get("salary"),
                        "fpts": proj.get("inhouse_fpts"),
                    }
            p = game[side]["probable_pitcher"]
            if p:
                proj = p.get("projection") or {}
                own = proj.get("inhouse_ownership_pct")
                if own is not None:
                    out[player_match.normalize_name(p["name"])] = {
                        "own": own, "team": team, "kind": "P",
                        "salary": (p.get("salary") or {}).get("salary"),
                        "fpts": proj.get("inhouse_fpts"),
                    }
    return out


BUCKETS = [(0, 2), (2, 5), (5, 10), (10, 20), (20, 35), (35, 200)]


async def main() -> int:
    rows = await history_db.get_contest_player_results()
    by_contest = defaultdict(list)
    for r in rows:
        by_contest[(r["date"], r["contest_id"], r["contest_name"])].append(r)

    bucket_acc = {b: [] for b in BUCKETS}
    all_chalk_err, all_overall_err = [], []
    stack_err = []

    for (day, cid, name), players in sorted(by_contest.items()):
        day_str = day.isoformat()
        try:
            model = await _model_by_name(day_str)
        except Exception:
            continue
        if not model:
            continue

        matched = []
        for p in players:
            m = model.get(p["normalized_name"])
            if m and p["ownership_pct"] is not None:
                matched.append((float(p["ownership_pct"]), m))
        if len(matched) < 5:
            continue

        print(f"\n=== {day_str}  {name}  (n={len(matched)}) ===")

        # Compression: max/mean predicted vs actual
        actual = [a for a, _ in matched]
        pred = [m["own"] for _, m in matched]
        print(f"  actual  max={max(actual):5.1f}%  mean={sum(actual)/len(actual):5.2f}%  sum={sum(actual):7.1f}%")
        print(f"  model   max={max(pred):5.1f}%  mean={sum(pred)/len(pred):5.2f}%  sum={sum(pred):7.1f}%")

        # Chalk MAE -- top 20 by ACTUAL ownership
        top = sorted(matched, key=lambda t: -t[0])[:20]
        chalk_err = [abs(m["own"] - a) for a, m in top]
        overall_err = [abs(m["own"] - a) for a, m in matched]
        all_chalk_err += chalk_err
        all_overall_err += overall_err
        print(f"  chalk MAE (top20 actual) = {sum(chalk_err)/len(chalk_err):5.2f}pp"
              f"   overall MAE = {sum(overall_err)/len(overall_err):5.2f}pp")

        # Worst chalk misses
        worst = sorted(top, key=lambda t: -abs(t[1]["own"] - t[0]))[:6]
        for a, m in worst:
            print(f"     actual {a:5.1f}%  model {m['own']:5.1f}%  ({m['kind']} {m['team']}"
                  f" ${m['salary']} {m['fpts']:.1f}fp)" if m["salary"] else
                  f"     actual {a:5.1f}%  model {m['own']:5.1f}%  ({m['kind']} {m['team']})")

        # Pitchers separately
        pit = [(a, m) for a, m in matched if m["kind"] == "P"]
        if pit:
            pe = [abs(m["own"] - a) for a, m in pit]
            print(f"  pitchers: n={len(pit)}  MAE={sum(pe)/len(pe):5.2f}pp"
                  f"  actual sum={sum(a for a,_ in pit):6.1f}%  model sum={sum(m['own'] for _,m in pit):6.1f}%")

        # Team-stack level: sum of each team's hitter ownership
        act_team, mod_team = defaultdict(float), defaultdict(float)
        for a, m in matched:
            if m["kind"] == "H":
                act_team[m["team"]] += a
                mod_team[m["team"]] += m["own"]
        teams = sorted(act_team, key=lambda t: -act_team[t])
        print("  team-stack ownership (sum of that team's hitters):")
        for t in teams[:6]:
            err = mod_team[t] - act_team[t]
            stack_err.append(abs(err))
            print(f"     {t:4} actual {act_team[t]:6.1f}%   model {mod_team[t]:6.1f}%   err {err:+6.1f}pp")
        for t in teams[6:]:
            stack_err.append(abs(mod_team[t] - act_team[t]))

        # Calibration buckets keyed on ACTUAL
        for a, m in matched:
            for b in BUCKETS:
                if b[0] <= a < b[1]:
                    bucket_acc[b].append((a, m["own"]))
                    break

    print("\n\n########## AGGREGATE ##########")
    print(f"chalk MAE (top20 by actual, all slates) = {sum(all_chalk_err)/len(all_chalk_err):.2f}pp")
    print(f"overall MAE                             = {sum(all_overall_err)/len(all_overall_err):.2f}pp")
    print(f"team-stack MAE                          = {sum(stack_err)/len(stack_err):.2f}pp")
    print("\ncalibration by ACTUAL ownership bucket:")
    print(f"  {'bucket':>12}  {'n':>4}  {'mean actual':>11}  {'mean model':>10}  {'bias':>7}")
    for b in BUCKETS:
        vals = bucket_acc[b]
        if not vals:
            continue
        ma = sum(a for a, _ in vals) / len(vals)
        mm = sum(p for _, p in vals) / len(vals)
        print(f"  {str(b[0])+'-'+str(b[1])+'%':>12}  {len(vals):>4}  {ma:>10.2f}%  {mm:>9.2f}%  {mm-ma:>+6.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
