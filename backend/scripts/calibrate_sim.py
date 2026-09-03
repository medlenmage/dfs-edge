"""
Grades the SIMULATOR against real DK contest standings -- run by hand,
not part of the main app (same convention as backtest_ownership.py).

The question it answers is the one that is otherwise invisible: does a
lineup's cumulative ownership relate to how it finishes the same way in
our simulation as it does in a real contest? A simulator can have a
perfectly good game model and still rank lineups wrongly, because rank
is relative -- it depends entirely on the FIELD each lineup is measured
against, and the field is modelled.

WHAT THIS CAUGHT

The sim was reading strongly anti-chalk: corr(cumulative ownership,
top-1% rate) ran -0.24 to -0.62 across six exports, while across 20
real archived contests the same correlation sits near zero (-0.24 to
+0.20, median +0.01). It would have been easy to conclude the ranking
layer applies an ownership penalty. It does not -- `evaluate_batch_
simulated` computes every finish probability from simulated points rank
alone (there is a test pinning that). Ownership enters only at the
payout step, divided by expected duplicates, which is the one mechanism
by which chalk legitimately loses value.

The bias was in the FIELD. On 9/2 the modelled low-stakes field sat at
152% cumulative ownership against 132% in the three real contests on
that same slate. A field 20 points chalkier than reality is itself an
ownership penalty, just applied where nobody would look for it: a
chalky entry moves in lockstep with an over-chalky field and can never
separate from it, while a contrarian entry is uncorrelated with the
field and its rank swings free. Recalibrating the sharpness exponents
against these exports moved corr(own, top-1%) from -0.51 to +0.07
without touching the ranking layer at all.

A trap worth naming, since it is what the original investigation hit:
ownership correlates with real POINTS far more strongly (up to +0.65)
than with a real top-1% FINISH (near zero). Comparing the sim's
finish-probability metric against the real points correlation makes the
sim look far more broken than it is. Grade like against like.

USAGE (from backend/)

    .venv/Scripts/python.exe -m scripts.calibrate_sim

    # also rebuild a field for a date and compare it to the real ones
    .venv/Scripts/python.exe -m scripts.calibrate_sim --date 2026-09-02

Reads real contest-standings exports (the .zip DraftKings hands you,
or an unzipped .csv) from `Contest Data/` and `~/Downloads`, and sim
exports (`contest-entries-*.csv`, the Contest Generator's own download)
from `~/Downloads`. Point it elsewhere with --standings-dir/--sim-dir.

Exits non-zero when the sim's ownership correlation differs from the
real-world MEDIAN by more than TOLERANCE, so this can be wired into a
check later rather than only read by eye.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import glob
import io
import math
import os
import statistics as st
import sys
import zipfile
from collections import Counter

# How far the sim's corr(ownership, top-1%) may sit outside the range
# the real contests show before this is called a failure. The real
# range is itself wide and slate-dependent (see the caveat at the
# bottom of the report), so this is deliberately not a tight bound --
# it is here to catch a sign flip, which is what actually went wrong,
# not to pin a decimal.
TOLERANCE = 0.30

ROSTER_SLOTS = {"P", "C", "1B", "2B", "3B", "SS", "OF"}


# ----------------------------------------------------------------- utils
def pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return float("nan")
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return float("nan")
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def _norm(name: str) -> str:
    return "".join(c for c in name.lower().strip() if c.isalnum() or c == " ")


def _split_lineup(lineup: str) -> list[str]:
    """
    DK's `Lineup` column is one flat string -- "1B Kyle Stowers 2B Thomas
    Saggese 3B ..." -- with no delimiter but the slot codes themselves.
    """
    names: list[str] = []
    current: list[str] = []
    for token in lineup.split():
        if token in ROSTER_SLOTS:
            if current:
                names.append(" ".join(current))
                current = []
        else:
            current.append(token)
    if current:
        names.append(" ".join(current))
    return names


# ------------------------------------------------------------- real side
def read_standings(path: str) -> dict | None:
    """
    One real DK contest-standings export -> per-entry cumulative
    ownership and real points, plus the field's own shape.

    The export is two tables sharing one CSV: entries on the left
    (Rank/EntryId/Points/Lineup) and a per-player summary on the right
    (Player/%Drafted/FPTS). The right-hand table is where real
    ownership comes from -- it is the actual field's %Drafted, not a
    projection, which is what makes this ground truth.
    """
    if path.endswith(".zip"):
        with zipfile.ZipFile(path) as zf:
            inner = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not inner:
                return None
            text = zf.read(inner[0]).decode("utf-8-sig", errors="replace")
    else:
        text = open(path, encoding="utf-8-sig", errors="replace").read()

    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows or "Lineup" not in rows[0]:
        return None

    ownership: dict[str, float] = {}
    for r in rows:
        player = (r.get("Player") or "").strip()
        drafted = (r.get("%Drafted") or "").strip().rstrip("%")
        if player and drafted:
            try:
                ownership[_norm(player)] = float(drafted)
            except ValueError:
                pass
    if not ownership:
        return None

    cums: list[float] = []
    points: list[float] = []
    signatures: list[frozenset] = []
    for r in rows:
        lineup = (r.get("Lineup") or "").strip()
        pts = (r.get("Points") or "").strip()
        if not lineup or not pts:
            continue
        names = _split_lineup(lineup)
        # A late-swap or incomplete entry has fewer than a full roster
        # and its cumulative ownership is not comparable.
        if len(names) != 10:
            continue
        try:
            points.append(float(pts))
        except ValueError:
            continue
        cums.append(sum(ownership.get(_norm(n), 0.0) for n in names))
        signatures.append(frozenset(_norm(n) for n in names))

    if len(cums) < 100:
        return None

    n = len(cums)
    counts = Counter(signatures)
    cutoff = sorted(points, reverse=True)[max(1, round(0.01 * n)) - 1]
    top1 = [1.0 if p >= cutoff else 0.0 for p in points]
    best = sorted(range(n), key=lambda i: -points[i])[:10]

    return {
        "name": os.path.basename(path),
        "n": n,
        "own_mean": st.mean(cums),
        "own_sd": st.pstdev(cums),
        "own_p5": sorted(cums)[n // 20],
        "own_p95": sorted(cums)[19 * n // 20],
        "dupe_pct": 100 * sum(v for v in counts.values() if v > 1) / n,
        "corr_own_points": pearson(cums, points),
        "corr_own_top1": pearson(cums, top1),
        "top10_own": (min(cums[i] for i in best), max(cums[i] for i in best)),
    }


# -------------------------------------------------------------- sim side
def read_sim_export(path: str) -> dict | None:
    """One Contest Generator CSV export -> the same correlations."""
    rows = list(csv.DictReader(io.StringIO(open(path, encoding="utf-8-sig").read())))
    if not rows or "top_1pct_pct" not in rows[0]:
        return None

    def col(key: str) -> list[float] | None:
        try:
            return [float(r[key]) for r in rows]
        except (KeyError, ValueError, TypeError):
            return None

    own, top1 = col("total_ownership_pct"), col("top_1pct_pct")
    proj, roi = col("projected_points"), col("roi_pct")
    sim_mean = col("simulated_points_mean")
    if not own or not top1 or len(own) != len(top1):
        return None

    return {
        "name": os.path.basename(path),
        "n": len(rows),
        "own_mean": st.mean(own),
        "corr_own_top1": pearson(own, top1),
        "corr_own_roi": pearson(own, roi) if roi else float("nan"),
        "corr_proj_top1": pearson(proj, top1) if proj else float("nan"),
        # Whether the game model is even simulating the player the
        # lineup was built on. The bootstrap engine recenters every
        # outcome pool on today's projection, so this reads ~1.00
        # there; the at-bat engine has its own opinion and reads much
        # lower, which is expected rather than broken -- but it does
        # mean a projection-driven metric will look weaker on it.
        "corr_proj_simpts": pearson(proj, sim_mean) if proj and sim_mean else float("nan"),
    }


# ------------------------------------------------------------ field side
async def compare_field(day: str, real_mean: float) -> None:
    """Rebuild a field for `day` at each sharpness and print where it lands."""
    from app.services import contest, mlb_slate

    slate = await mlb_slate.build_slate(day, include_hitters=True)
    print(f"\nMODELLED FIELD for {day} (1,500 lineups per level)")
    print(f"  real contests that day averaged {real_mean:.1f}% cumulative ownership")
    print(f"  {'level':10s} {'own mean':>9s} {'sd':>6s} {'p95':>7s} {'dupes':>7s}   gap vs real")
    for level in ("low", "marquee", "high"):
        field = contest.generate_field(slate, 1500, seed=7, field_sharpness=level)
        cums = [lu["total_ownership_pct"] for lu in field]
        sigs: dict = {}
        for lu in field:
            sig = lu.get("player_ids") or frozenset(p["id"] for p in lu["players"])
            sigs[sig] = sigs.get(sig, 0) + 1
        dupes = 100 * sum(v for v in sigs.values() if v > 1) / len(field)
        mean = st.mean(cums)
        print(
            f"  {level:10s} {mean:8.1f}% {st.pstdev(cums):6.1f} "
            f"{sorted(cums)[19 * len(cums) // 20]:6.1f}% {dupes:6.1f}%   {mean - real_mean:+.1f}"
        )


# ----------------------------------------------------------------- main
def main() -> int:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    repo = os.path.dirname(here)
    downloads = os.path.expanduser("~/Downloads")

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--standings-dir", action="append", default=None)
    ap.add_argument("--sim-dir", default=downloads)
    ap.add_argument("--date", default=None, help="also rebuild and grade a field for this date")
    args = ap.parse_args()

    standings_dirs = args.standings_dir or [os.path.join(repo, "Contest Data"), downloads]

    paths: list[str] = []
    for d in standings_dirs:
        paths += sorted(glob.glob(os.path.join(d, "*.zip")))
        paths += sorted(glob.glob(os.path.join(d, "contest-standings-*.csv")))

    real = [r for r in (read_standings(p) for p in paths) if r]
    sims = [
        s
        for s in (read_sim_export(p) for p in sorted(glob.glob(os.path.join(args.sim_dir, "contest-entries-*.csv"))))
        if s
    ]

    if not real:
        print("No readable contest-standings exports found in:")
        for d in standings_dirs:
            print(f"  {d}")
        return 2

    print("=" * 100)
    print("REAL CONTESTS -- ground truth")
    print("=" * 100)
    print(f"{'contest':46s} {'n':>6s} {'own':>7s} {'sd':>5s} {'dup':>6s} "
          f"{'own/pts':>8s} {'own/top1':>9s}  best-10 own")
    for r in real:
        lo, hi = r["top10_own"]
        print(f"{r['name'][:44]:46s} {r['n']:6d} {r['own_mean']:6.1f}% {r['own_sd']:5.1f} "
              f"{r['dupe_pct']:5.1f}% {r['corr_own_points']:+8.2f} {r['corr_own_top1']:+9.2f}  "
              f"{lo:.0f}-{hi:.0f}%")

    real_top1 = [r["corr_own_top1"] for r in real]
    # The MEDIAN, not the min/max. One unusual contest (a 2-game turbo, a
    # heavily-duplicated mini-MAX) can stretch the observed range far
    # enough to wave through a sim that is plainly anti-chalk -- which is
    # exactly what happened the first time this ran. The median over a
    # real archive is stable near zero.
    real_centre = st.median(real_top1)
    real_lo, real_hi = min(real_top1), max(real_top1)
    print(f"\n  real corr(own, top-1%): median {real_centre:+.2f}, "
          f"range {real_lo:+.2f} .. {real_hi:+.2f}, across {len(real)} contests")
    print("  Ownership tracks real POINTS strongly on some slates (own/pts reaches +0.65) but")
    print("  its relationship with a top-1% FINISH is near zero almost everywhere. Those are")
    print("  different questions, and only the second is what the sim's headline metric")
    print("  estimates -- do not calibrate the sim against the first.")
    print("  NOTE: a real contest gives ONE realised finish per entry, so its correlation is")
    print("  attenuated by single-draw noise. The sim reports a probability across many")
    print("  trials, which is not. Read the real numbers as a floor on the true relationship.")
    print("  NOTE: cumulative ownership is NOT comparable across slates -- it scales with how")
    print("  many games are on the board (86% on a big main slate, 297% on a 2-game turbo).")
    print("  Only ever compare a modelled field to real contests on the SAME date.")

    if not sims:
        print("\nNo sim exports found -- nothing to grade. Download a batch from the")
        print("Contest Generator (contest-entries-*.csv) and re-run.")
        return 0

    print("\n" + "=" * 100)
    print("SIM EXPORTS -- graded against that")
    print("=" * 100)
    print(f"{'export':40s} {'n':>6s} {'own':>7s} {'own/top1':>9s} {'own/roi':>8s} "
          f"{'proj/top1':>10s} {'proj/simpts':>12s}  verdict")

    failures = 0
    for s in sims:
        c = s["corr_own_top1"]
        if c < real_centre - TOLERANCE:
            verdict = f"ANTI-CHALK by {real_centre - c:.2f}"
            failures += 1
        elif c > real_centre + TOLERANCE:
            verdict = f"PRO-CHALK by {c - real_centre:.2f}"
            failures += 1
        else:
            verdict = "ok"
        print(f"{s['name'][:38]:40s} {s['n']:6d} {s['own_mean']:6.1f}% {c:+9.2f} "
              f"{s['corr_own_roi']:+8.2f} {s['corr_proj_top1']:+10.2f} "
              f"{s['corr_proj_simpts']:+12.2f}  {verdict}")

    print("\n  proj/simpts near 1.00 = the bootstrap engine (pools recentered on today's")
    print("  projection). Well below that = the at-bat engine, which forms its own view of")
    print("  a player and will legitimately show a weaker proj/top1 as a result.")

    if args.date:
        # Same-date contests only: cumulative ownership scales with the
        # size of the board, so a cross-slate average is not a target.
        stamp = args.date.replace("-", "")
        mmddyyyy = stamp[4:] + stamp[:4]
        same_day = [r for r in real if mmddyyyy in r["name"] or stamp in r["name"]]
        if not same_day:
            print(f"\nNo real contest found for {args.date} -- cannot grade that field, since")
            print("cumulative ownership is only comparable within a slate.")
        else:
            print(f"\n  grading against {len(same_day)} real contest(s) on {args.date}")
            asyncio.run(compare_field(args.date, st.mean([r["own_mean"] for r in same_day])))

    print()
    if failures:
        print(f"FAIL: {failures} of {len(sims)} sim exports sit more than {TOLERANCE:.2f} "
              f"outside the real range.")
        print("The field is the first place to look -- rank comes from simulated points")
        print("alone, so an ownership slope this size means the modelled OPPONENTS are")
        print("mis-calibrated, not the metric. Re-run with --date to see where the field lands.")
        return 1

    print(f"PASS: all {len(sims)} sim exports sit within {TOLERANCE:.2f} of the real median.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
