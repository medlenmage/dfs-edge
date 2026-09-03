"""
Measure the MLB variance model against reality -- run before touching
any correlation constant in services/variance.py.

Three sections:

1. REAL teammate correlation, measured directly from this season's
   game logs (same-date DK-point pairs across several real rosters).
   This is the target MATE_CORRELATION exists to reproduce.
2. The SIM's effective correlations through the real sampling path
   (teammate, hitter vs opposing starter), to compare against (1) and
   against the open-source chanzer0/MLB-DFS-Tools fitted matrix
   (teammates +0.12..0.20, hitter vs opposing starter -0.26..-0.31).
3. A controlled stacked-vs-spread pair, to answer whether stacking
   actually buys a ceiling and whether the at-bat engine buys more of
   one than the assumed-coefficient engine. Grouping an export by
   stack_type cannot answer this -- see stack_separation()'s docstring.
4. A simulated ownership-weighted field's cross-sectional score
   distribution next to the real archived DK contests in
   "Contest Data/" -- mean/std/p95/p99/winning score.

    backend/.venv/Scripts/python.exe -m scripts.measure_mlb_sim_correlations
"""

import asyncio
import csv
import glob
import io
import random
import sys
import zipfile
from datetime import date as date_cls
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clients import mlb  # noqa: E402
from app.services import contest, mlb_dk_points, mlb_slate, variance  # noqa: E402

TEAMS = {"ATL": 144, "SEA": 136, "LAD": 119, "NYY": 147, "MIL": 158, "BOS": 111}
CONTEST_DATA = Path(__file__).resolve().parents[2] / "Contest Data"


async def real_teammate_correlation(season: int) -> None:
    pairs = []
    for tid in TEAMS.values():
        active = await mlb.get_active_roster(tid, season)
        bios = await mlb.get_people(active)
        series = {}
        for pid in active:
            b = bios.get(pid) or {}
            if (b.get("position") or "") in ("P", "SP", "RP", "TWP"):
                continue
            log = await mlb.get_player_game_log(pid, season, group="hitting")
            s = {
                g.get("date") or g.get("game_date"): mlb_dk_points.hitter_game_points(g)
                for g in log
                if (g.get("plate_appearances") or 0) > 0 and (g.get("date") or g.get("game_date"))
            }
            if len(s) >= 40:
                series[pid] = s
        ids = list(series)
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = series[ids[i]], series[ids[j]]
                common = sorted(set(a) & set(b))
                if len(common) >= 30:
                    pairs.append(np.corrcoef([a[d] for d in common], [b[d] for d in common])[0, 1])
    print(
        f"1. REAL teammate DK-point correlation: mean {np.mean(pairs):+.3f} "
        f"median {np.median(pairs):+.3f} (n={len(pairs)} pairs)"
    )


async def sim_effective_correlations(slate: dict, season: int) -> None:
    g = next(g for g in slate["games"] if g.get("in_slate"))
    home, away = g["home"], g["away"]
    h5 = [x for x in home["hitters"] if (x.get("projection") or {}).get("fpts")][:5]
    ph = [sorted(await variance.player_outcome_pool(h["id"], h["position"], season)) for h in h5]
    opp = sorted(await variance.player_outcome_pool(away["probable_pitcher"]["id"], "P", season))
    rng = random.Random(5)
    trials = 8000
    H = np.zeros((5, trials))
    OP = np.zeros(trials)
    for t in range(trials):
        tm = variance.team_environment_multiplier(rng)
        for i, pool in enumerate(ph):
            H[i, t] = variance.sample_correlated_outcome(pool, rng, team_multiplier=tm)
        OP[t] = variance.sample_correlated_outcome(opp, rng, opponent_multiplier=tm)
    mate = np.mean([np.corrcoef(H[i], H[j])[0, 1] for i in range(5) for j in range(i + 1, 5)])
    vs_op = np.mean([np.corrcoef(H[i], OP)[0, 1] for i in range(5)])
    print(f"2. SIM effective: teammate {mate:+.3f} (target +0.10) | vs opposing starter {vs_op:+.3f} (target ~-0.28)")


async def field_vs_real_contests(slate: dict, season: int) -> None:
    field = contest.generate_field(slate, 3000, seed=2)
    pools = await variance.player_pools_for_entries(field, season)
    sim = variance.simulate_batch(field, pools, num_trials=200, seed=4)
    print(
        f"3. SIM 3,000-lineup field: mean {sim.mean(axis=0).mean():.1f} "
        f"std {sim.std(axis=0).mean():.1f} p95 {np.percentile(sim, 95, axis=0).mean():.1f} "
        f"p99 {np.percentile(sim, 99, axis=0).mean():.1f} winner {sim.max(axis=0).mean():.1f}"
    )
    for f in sorted(glob.glob(str(CONTEST_DATA / "*")))[:50]:
        try:
            if f.endswith(".zip"):
                with zipfile.ZipFile(f) as z:
                    name = next(n for n in z.namelist() if n.endswith(".csv"))
                    raw = z.read(name).decode("utf-8-sig", errors="replace")
            else:
                raw = open(f, encoding="utf-8-sig", errors="replace").read()
            pts = np.array(
                [float(r["Points"]) for r in csv.DictReader(io.StringIO(raw)) if r.get("Points")]
            )
            if len(pts):
                print(
                    f"   real {Path(f).name[:48]:50} n={len(pts):>6} mean {pts.mean():>6.1f} "
                    f"std {pts.std():>5.1f} p95 {np.percentile(pts, 95):>6.1f} "
                    f"p99 {np.percentile(pts, 99):>6.1f} win {pts.max():>6.1f}"
                )
        except Exception:  # noqa: BLE001 -- a malformed archive shouldn't kill the report
            continue


async def stack_separation(slate: dict, season: int) -> None:
    """
    Does stacking actually buy a bigger ceiling, and does the at-bat
    engine buy MORE of one than the assumed-coefficient engine?

    WHY THIS IS A CONTROLLED TEST AND NOT A GROUPBY

    The obvious way to ask is to group an exported batch by stack_type
    and compare ceilings. That is observational, and it reads almost
    flat: lineups labelled 5-3 differ from lineups labelled 4-2 in
    projection, salary, which teams they use and how owned they are,
    all at once, and the export's `ceiling` column is p95 -- the part
    of the tail where the stack effect is smallest. Reviewers have
    twice concluded from that table that inning-level correlation "is
    not doing much work".

    Holding everything else fixed says otherwise. Same two pitchers,
    same three filler bats, and five hitters that are either one team's
    or five different teams' -- matched one-for-one on projection, so
    the two lineups project within a point or two of each other and the
    ONLY difference is whether those five share a game.
    """
    from collections import defaultdict

    teams: dict[str, list[dict]] = defaultdict(list)
    pitchers: list[dict] = []
    for game in slate["games"]:
        for side_key in ("home", "away"):
            side = game[side_key]
            opponent = game["away" if side_key == "home" else "home"]["abbrev"]
            for p in side.get("hitters", []):
                fpts = (p.get("projection") or {}).get("fpts")
                if fpts:
                    teams[side["abbrev"]].append({
                        "id": p["id"], "team": side["abbrev"], "opponent": opponent,
                        "projected_fpts": float(fpts), "ownership_pct": 1.0,
                    })
            starter = side.get("probable_pitcher")
            if starter and (starter.get("projection") or {}).get("fpts"):
                pitchers.append({
                    "id": starter["id"], "team": side["abbrev"], "opponent": opponent,
                    "projected_fpts": float(starter["projection"]["fpts"]),
                    "ownership_pct": 1.0,
                })

    deep = [t for t, v in teams.items() if len(v) >= 6]
    if len(deep) < 6 or len(pitchers) < 2:
        print()
        print("STACK SEPARATION: slate too thin to build a controlled pair, skipped")
        return

    arms = pitchers[:2]
    lineups: list[dict] = []
    labels: list[str] = []
    for team in deep[:6]:
        stack = teams[team][:5]
        others = [u for u in deep if u != team][:5]
        spread = [
            min(teams[u], key=lambda q: abs(q["projected_fpts"] - h["projected_fpts"]))
            for h, u in zip(stack, others)
        ]
        filler = [teams[deep[-1]][0], teams[deep[-2]][0], teams[deep[-3]][0]]
        lineups.append({"players": arms + stack + filler})
        labels.append("stacked")
        lineups.append({"players": arms + spread + filler})
        labels.append("spread")

    pools = await variance.player_pools_for_entries(lineups, season)
    runs = [("bootstrap", variance.simulate_batch(lineups, pools, num_trials=6000, seed=3))]
    try:
        runs.append(("at-bat", await contest._simulate_lineups_atbat(
            lineups, slate, season, num_trials=6000, seed=3)))
    except Exception as exc:                      # noqa: BLE001 -- report, don't abort
        print()
        print(f"  at-bat engine unavailable for this slate: {type(exc).__name__}: {exc}")

    print()
    print("STACK SEPARATION (matched-projection 10-man lineups, 5 stacked vs 5 spread)")
    print("  The p99 column is the one that matters -- tournaments are won in the far tail,")
    print("  and that is exactly where an exported p95 ceiling stops showing the difference.")
    for label, sim in runs:
        def summarise(kind: str) -> tuple[float, float, float]:
            idx = [i for i, l in enumerate(labels) if l == kind]
            means = np.array([sim[i].mean() for i in idx])
            sds = np.array([sim[i].std() for i in idx])
            p95 = np.array([np.percentile(sim[i], 95) for i in idx])
            p99 = np.array([np.percentile(sim[i], 99) for i in idx])
            return sds.mean(), (p95 - means).mean(), (p99 - means).mean()

        s_sd, s_95, s_99 = summarise("stacked")
        d_sd, d_95, d_99 = summarise("spread")
        print(f"  {label:10s} stacked sd {s_sd:5.2f}  p95+{s_95:5.1f}  p99+{s_99:5.1f}   "
              f"spread sd {d_sd:5.2f}  p95+{d_95:5.1f}  p99+{d_99:5.1f}")
        print(f"  {'':10s} STACK GAIN  sd {s_sd - d_sd:+5.2f}   p95 {s_95 - d_95:+5.1f}   "
              f"p99 {s_99 - d_99:+5.1f}")


async def main() -> None:
    day = sys.argv[1] if len(sys.argv) > 1 else date_cls.today().isoformat()
    season = int(day[:4])
    slate = await mlb_slate.build_slate(day, include_inhouse=True)
    await real_teammate_correlation(season)
    await sim_effective_correlations(slate, season)
    await field_vs_real_contests(slate, season)
    await stack_separation(slate, season)


if __name__ == "__main__":
    asyncio.run(main())
