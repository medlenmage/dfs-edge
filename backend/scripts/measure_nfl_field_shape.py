"""
What shape the generated NFL opponent field actually comes out as.

nfl_contest.py's PRIMARY_STACK_WEIGHTS state an INTENT. What a lineup
sampler actually produces is a different thing -- an archetype it can't
fill legally under the cap gets abandoned, so realized shape drifts from
intended shape and only measurement says by how much.

Scored with the same describe() the real-contest analysis uses, so the
generated field and the real Milly Maker field are read by identical
code and the comparison means something.

Usage (from backend/):
    .venv/Scripts/python.exe -m scripts.measure_nfl_field_shape [n]
"""

from __future__ import annotations

import asyncio
import statistics
import sys
from collections import Counter

from app.clients.http import close_client
from app.services import nfl_contest, nfl_slate

# Pooled across 1,486,422 real entries in five 2025 Milly Makers.
REAL = {
    "stack": {0: 19.8, 1: 51.2, 2: 26.9, 3: 2.0},
    "bringback_any": 43.0,
    "max_team": {1: 5.6, 2: 52.4, 3: 34.0, 4: 6.6, 5: 0.9},
    "teams": 6.88,
}


async def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    season = int(sys.argv[2]) if len(sys.argv) > 2 else 2026
    week = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    slate = await nfl_slate.build_slate(season, week)

    # Classify the pool the way production does. Without this,
    # running_qb_ids is empty and the qb_naked archetype is skipped
    # outright -- which is exactly the artefact that made an early run of
    # this script read 0% naked QBs.
    pool, _order = nfl_contest._build_candidate_pool(slate)
    running_qb_ids, pass_catching_rb_ids = await nfl_contest._classify_pool(pool, season)
    print(f"running QBs in pool: {len(running_qb_ids)}, "
          f"pass-catching RBs: {len(pass_catching_rb_ids)}")
    entries = nfl_contest.generate_field(
        slate, n, seed=7, field_sharpness="marquee",
        running_qb_ids=running_qb_ids, pass_catching_rb_ids=pass_catching_rb_ids,
    )
    print(f"generated {len(entries)} entries for {season} W{week}\n")

    # Opponent map comes from the slate's own games rather than nflverse:
    # the generated pool already carries each player's team, and a slate
    # for a week that hasn't been played has no stats file to read.
    opp_of: dict[str, str] = {}
    for g in slate.get("games") or []:
        h = (g.get("home") or {}).get("abbrev")
        a = (g.get("away") or {}).get("abbrev")
        if h and a:
            opp_of[h] = a
            opp_of[a] = h

    stacks: Counter[int] = Counter()
    bring: Counter[int] = Counter()
    maxt: Counter[int] = Counter()
    team_counts: list[int] = []
    for e in entries:
        picks = e.get("players") or []
        qb = next((p for p in picks if (p.get("position") or "").upper() == "QB"), None)
        if not qb:
            continue
        qteam = qb.get("team")
        qopp = qb.get("opponent") or opp_of.get(qteam, "")
        by_team: Counter[str] = Counter()
        s = b = 0
        for p in picks:
            t = p.get("team")
            if t:
                by_team[t] += 1
            if p is qb:
                continue
            pos = (p.get("position") or "").upper()
            if t and t == qteam and pos in ("WR", "TE"):
                s += 1
            if qopp and t == qopp and pos != "DST":
                b += 1
        stacks[min(s, 3)] += 1
        bring[min(b, 3)] += 1
        maxt[min(max(by_team.values()) if by_team else 0, 5)] += 1
        team_counts.append(len(by_team))

    tot = sum(stacks.values()) or 1
    print(f"{'stack size':12} {'generated':>10} {'real':>8}")
    for k in range(4):
        print(f"{k:<12} {100*stacks[k]/tot:9.1f}% {REAL['stack'].get(k, 0):7.1f}%")
    any_bring = 100 * (tot - bring[0]) / tot
    print(f"\n{'bring-back':12} {any_bring:9.1f}% {REAL['bringback_any']:7.1f}%  (any)")
    print(f"\n{'max 1 team':12} {'generated':>10} {'real':>8}")
    for k in range(1, 6):
        print(f"{k:<12} {100*maxt[k]/tot:9.1f}% {REAL['max_team'].get(k, 0):7.1f}%")
    print(f"\nteams per lineup  generated {statistics.mean(team_counts):.2f}  "
          f"real {REAL['teams']:.2f}")
    await close_client()


if __name__ == "__main__":
    asyncio.run(main())
