"""Probe: which team-level features actually predict real stack ownership?

The MLB ownership doc argues hitter ownership is driven team-first (the
field picks a team to stack, then picks bats), and our own diagnostic
confirms team-stack error is by far our largest (16.9pp team-stack MAE,
with single misses over 100pp). This probe tests WHICH team-level
signals actually separate the stacked teams from the ignored ones on
real archived slates, before any of them get built into the model.

Run from backend/:  .venv/Scripts/python.exe -m scripts.probe_stack_features
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from app import history_db
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


async def _team_features(day: str):
    """abbrev -> team-level features the doc's Model A cares about."""
    slate = await mlb_slate.build_slate(day, include_hitters=True, include_inhouse=True)
    teams = {}
    for game in slate.get("games", []):
        for side in ("home", "away"):
            me, opp = game[side], game["away" if side == "home" else "home"]
            hitters = me.get("hitters") or []
            sal = sorted(
                [(h.get("salary") or {}).get("salary") for h in hitters
                 if (h.get("salary") or {}).get("salary")]
            )
            # The doc's Tier-2 #7: what the cheapest real 5-man stack costs.
            cheap5 = sum(sal[:5]) if len(sal) >= 5 else None
            # ...and what the five BEST bats cost (the stack you'd want).
            by_fpts = sorted(
                [h for h in hitters if (h.get("projection") or {}).get("inhouse_fpts")],
                key=lambda h: -(h["projection"]["inhouse_fpts"]),
            )[:5]
            best5_cost = sum((h.get("salary") or {}).get("salary") or 0 for h in by_fpts) or None
            best5_fpts = sum(h["projection"]["inhouse_fpts"] for h in by_fpts) or None

            sp = opp.get("probable_pitcher") or {}
            teams[me["abbrev"]] = {
                "implied_runs": me.get("implied_runs"),
                "cheap5_salary": cheap5,
                "best5_salary": best5_cost,
                "best5_fpts": best5_fpts,
                "best5_value": (best5_fpts / best5_cost * 1000) if best5_fpts and best5_cost else None,
                "opp_sp_score": (sp.get("edge") or {}).get("score"),
                "opp_sp_salary": (sp.get("salary") or {}).get("salary"),
                "park_hr": (game.get("park") or {}).get("hr_factor"),
                "n_hitters": len(hitters),
            }
    return teams


FEATURES = [
    "implied_runs", "cheap5_salary", "best5_salary", "best5_fpts",
    "best5_value", "opp_sp_score", "opp_sp_salary", "park_hr",
]


async def main() -> int:
    rows = await history_db.get_contest_player_results()
    by_contest = defaultdict(list)
    for r in rows:
        by_contest[(r["date"], r["contest_id"], r["contest_name"])].append(r)

    per_feature = defaultdict(list)

    for (day, cid, name), players in sorted(by_contest.items()):
        day_str = day.isoformat()
        try:
            feats = await _team_features(day_str)
        except Exception:
            continue
        if not feats:
            continue

        # Real stack ownership per team = sum of that team's hitters' %Drafted.
        slate = await mlb_slate.build_slate(day_str, include_hitters=True, include_inhouse=True)
        team_of = {}
        for game in slate.get("games", []):
            for side in ("home", "away"):
                for h in game[side].get("hitters") or []:
                    team_of[player_match.normalize_name(h["name"])] = game[side]["abbrev"]

        actual = defaultdict(float)
        for p in players:
            t = team_of.get(p["normalized_name"])
            if t and p["ownership_pct"] is not None:
                actual[t] += float(p["ownership_pct"])
        if len(actual) < 4:
            continue

        print(f"\n=== {day_str} {name} ===")
        teams = sorted(actual, key=lambda t: -actual[t])
        print(f"  {'team':<5} {'actual':>7} {'impRuns':>8} {'cheap5$':>8} {'best5$':>8} {'best5fp':>8} {'oppSP':>6}")
        for t in teams[:8]:
            f = feats.get(t, {})
            print(f"  {t:<5} {actual[t]:>6.1f}% {str(f.get('implied_runs')):>8}"
                  f" {str(f.get('cheap5_salary')):>8} {str(f.get('best5_salary')):>8}"
                  f" {(f'{f['best5_fpts']:.1f}' if f.get('best5_fpts') else '-'):>8}"
                  f" {str(f.get('opp_sp_score')):>6}")

        for feat in FEATURES:
            xs, ys = [], []
            for t, own in actual.items():
                v = feats.get(t, {}).get(feat)
                if v is not None:
                    xs.append(float(v))
                    ys.append(own)
            r = _spearman(xs, ys)
            if r is not None:
                per_feature[feat].append(r)
                print(f"     {feat:<16} vs real stack ownership: r={r:+.3f} (n={len(xs)})")

    print("\n\n########## WHICH TEAM FEATURE PREDICTS A STACK? ##########")
    print("(Spearman vs. real summed team hitter ownership, averaged over slates)\n")
    ranked = sorted(per_feature.items(), key=lambda kv: -abs(sum(kv[1]) / len(kv[1])))
    for feat, rs in ranked:
        avg = sum(rs) / len(rs)
        print(f"  {feat:<16} avg r={avg:+.3f}   per-slate: {', '.join(f'{r:+.2f}' for r in rs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
