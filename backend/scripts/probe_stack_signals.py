"""Probe: which team-level signals predict the REAL 4+ stack rate?

Successor to probe_stack_features.py, which could only measure against
summed player ownership on 4 slates. This measures against the real
share of the field that actually stacked each team 4+ deep, across
every archived contest (15 across 11 dates) -- see
scripts/archive_contest_stacks.py for where that ground truth comes
from.

Candidates include signals this app already computes but the ownership
model has never used -- notably the Stacks tab's own `stack_score` and
the venue park factors.

Run from backend/:  .venv/Scripts/python.exe -m scripts.probe_stack_signals
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from app import history_db
from app.services import inhouse_projections as ip
from app.services import mlb_slate, player_match
from scripts.backtest_stacks import _spearman, _stack_scores


async def _signals(day: str) -> dict[str, dict[str, float]]:
    """team -> {signal name: value} for one real past date."""
    slate = await mlb_slate.build_slate(day, include_hitters=True)
    archived = await history_db.get_archived_salaries(day)
    out: dict[str, dict[str, float]] = {}

    for g in slate.get("games", []):
        venue = g.get("venue") or {}
        pf = venue.get("park_factors") or {}
        for side in ("home", "away"):
            opp_side = "away" if side == "home" else "home"
            me, opp = g[side], g[opp_side]
            sp = opp.get("probable_pitcher") or {}
            sal = (archived.get(player_match.normalize_name(sp.get("name") or "")) or {}).get("salary")

            hitters = me.get("hitters") or []
            sals = sorted(
                (archived.get(player_match.normalize_name(h["name"])) or {}).get("salary") or 0
                for h in hitters
            )
            sals = [s for s in sals if s]

            out[me["abbrev"]] = {
                "implied_runs": me.get("implied_runs"),
                "stack_score": me.get("stack_score"),
                "park_runs": pf.get("runs"),
                "park_hr": pf.get("hr"),
                "opp_sp_edge": (sp.get("edge") or {}).get("score"),
                "opp_sp_salary": sal,
                "cheap5_salary": sum(sals[:5]) if len(sals) >= 5 else None,
                "moneyline": me.get("moneyline"),
            }
    return out


async def main() -> int:
    rows = await history_db.get_contest_stack_results()
    if not rows:
        print("No archived stack results -- run scripts.archive_contest_stacks first.")
        return 1

    by_contest = defaultdict(list)
    for r in rows:
        by_contest[(r["date"], r["contest_id"], r["contest_name"])].append(r)

    sig_cache: dict[str, dict[str, dict[str, float]]] = {}
    score_cache: dict[str, dict[str, float]] = {}
    per_signal = defaultdict(list)

    for (day, cid, name), team_rows in sorted(by_contest.items()):
        day_str = day.isoformat()
        if day_str not in sig_cache:
            try:
                sig_cache[day_str] = await _signals(day_str)
                score_cache[day_str] = await _stack_scores(day_str)
            except Exception as exc:
                print(f"{day_str} SKIPPED ({exc})")
                continue
        sigs, scores = sig_cache[day_str], score_cache[day_str]

        field = max(r["field_size"] for r in team_rows)
        real_4plus = defaultdict(int)
        for r in team_rows:
            if r["stack_size"] >= 4:
                real_4plus[r["team"]] += r["entry_count"]
        if len(real_4plus) < 4:
            continue

        teams = list(real_4plus)
        real = [100 * real_4plus[t] / field for t in teams]

        cur = [scores.get(t) for t in teams]
        ok = [(c, r_) for c, r_ in zip(cur, real) if c is not None]
        if len(ok) >= 4:
            r_ = _spearman([c for c, _ in ok], [x for _, x in ok])
            if r_ is not None:
                per_signal["** current _team_stack_scores **"].append(r_)

        names = [
            "implied_runs", "stack_score", "park_runs", "park_hr",
            "opp_sp_edge", "opp_sp_salary", "cheap5_salary", "moneyline",
        ]
        for sname in names:
            xs, ys = [], []
            for t, rv in zip(teams, real):
                v = (sigs.get(t) or {}).get(sname)
                if v is not None:
                    xs.append(float(v))
                    ys.append(rv)
            if len(xs) >= 4:
                r_ = _spearman(xs, ys)
                if r_ is not None:
                    per_signal[sname].append(r_)

    print("\n### Signal vs REAL 4+ stack rate (Spearman, averaged over contests)\n")
    print(f"  {'signal':<32} {'avg r':>7}  {'contests':>8}")
    for sname, rs in sorted(per_signal.items(), key=lambda kv: -abs(sum(kv[1]) / len(kv[1]))):
        print(f"  {sname:<32} {sum(rs)/len(rs):>+7.3f}  {len(rs):>8}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
