"""Backtests the team-stack layer against REAL stack rates.

diagnose_ownership.py can only cover dates whose DK salaries survive
(the local cache is 7-day, the durable archive starts 2026-08-18), which
left 4 usable contests. This one needs no salary at all: it compares
_team_stack_scores()'s ranking against the real share of the field that
actually stacked each team 4+ deep, recovered from the standings
exports' own `Lineup` column (scripts/archive_contest_stacks.py). That
covers every archived contest -- 15 across 11 dates -- which is the
first sample here big enough to say anything honest about the layer.

The headline metric is the doc-standard one for this: stack-rank
recall. Of the teams the field actually stacked most, how many did the
model put in its own top few? Getting the ORDER of stacks right matters
more than the levels, because that's what drives every leverage call.

Run from backend/:  .venv/Scripts/python.exe -m scripts.backtest_stacks
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from app import history_db
from app.services import inhouse_projections as ip
from app.services import mlb_slate


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


async def _stack_scores(day: str) -> dict[str, float]:
    """The production team-stack score per team for one real past date.

    Builds the same shape of pool project_ownership() gets, but sourcing
    the opposing starter's price from the durable archive when the local
    salary cache has expired -- and falling back to his matchup score
    when even that predates the archive, since the two measured almost
    equally well as opposing-arm-quality proxies (r=-0.72 vs -0.67).
    """
    slate = await mlb_slate.build_slate(day, include_hitters=True)
    archived = await history_db.get_archived_salaries(day)

    pool = []
    for g in slate.get("games", []):
        for side in ("home", "away"):
            opp_side = "away" if side == "home" else "home"
            opp_sp = g[opp_side].get("probable_pitcher") or {}
            opp_id = opp_sp.get("id")
            if opp_id is not None:
                from app.services import player_match

                sal = (archived.get(player_match.normalize_name(opp_sp.get("name") or "")) or {}).get("salary")
                if not sal:
                    # No archived price this far back: the starter's own
                    # matchup score stands in, scaled into a comparable
                    # range so percentile ranking still behaves.
                    score = (opp_sp.get("edge") or {}).get("score")
                    sal = int(score * 100) if score else None
                if sal:
                    pool.append({"id": opp_id, "position": "P", "salary": sal, "fpts": 0.0})
            pool.append(
                {
                    "id": -abs(hash(g[side]["abbrev"])) % 10**9,
                    "position": "OF",
                    "salary": 4000,
                    "fpts": 0.0,
                    "implied_runs": g[side].get("implied_runs"),
                    "opponent_pitcher_id": opp_id,
                    "team": g[side]["abbrev"],
                }
            )
    return ip._team_stack_scores(pool)


async def main() -> int:
    rows = await history_db.get_contest_stack_results()
    if not rows:
        print("No archived stack results -- run scripts.archive_contest_stacks first.")
        return 1

    by_contest = defaultdict(list)
    for r in rows:
        by_contest[(r["date"], r["contest_id"], r["contest_name"])].append(r)

    correlations, recalls = [], []
    score_cache: dict[str, dict[str, float]] = {}

    for (day, cid, name), team_rows in sorted(by_contest.items()):
        day_str = day.isoformat()
        if day_str not in score_cache:
            try:
                score_cache[day_str] = await _stack_scores(day_str)
            except Exception as exc:
                print(f"{day_str} {name[:44]:<44} SKIPPED ({exc})")
                continue
        scores = score_cache[day_str]

        field = max(r["field_size"] for r in team_rows)
        real_4plus = defaultdict(int)
        for r in team_rows:
            if r["stack_size"] >= 4:
                real_4plus[r["team"]] += r["entry_count"]

        teams = [t for t in real_4plus if t in scores]
        if len(teams) < 4:
            print(f"{day_str} {name[:44]:<44} SKIPPED (only {len(teams)} teams matched)")
            continue

        real = [100 * real_4plus[t] / field for t in teams]
        pred = [scores[t] for t in teams]
        r = _spearman(pred, real)

        top_real = {t for t, _ in sorted(zip(teams, real), key=lambda kv: -kv[1])[:3]}
        top_pred = {t for t, _ in sorted(zip(teams, pred), key=lambda kv: -kv[1])[:3]}
        recall = len(top_real & top_pred) / len(top_real)
        recalls.append(recall)
        if r is not None:
            correlations.append(r)

        best = sorted(zip(teams, real, pred), key=lambda kv: -kv[1])[:4]
        detail = "  ".join(f"{t} {rr:.0f}%(p{pp:.2f})" for t, rr, pp in best)
        print(
            f"{day_str} {name[:42]:<42} n={len(teams):<3} r={r:+.3f} "
            f"top3recall={recall:.2f}  {detail}"
        )

    if correlations:
        print(
            f"\n{len(correlations)} contest(s) -- avg Spearman vs real 4+ stack rate "
            f"= {sum(correlations)/len(correlations):+.3f}"
        )
        print(f"avg top-3 stack-rank recall = {sum(recalls)/len(recalls):.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
