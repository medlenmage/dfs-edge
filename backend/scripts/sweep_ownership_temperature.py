"""
Sweep inhouse_projections._SOFTMAX_TEMPERATURE on the FULL live
pipeline, against real archived contest ownership.

Must be done on the full pipeline, not the Supabase archive
reconstruction: the archive has no historical Vegas implied runs, which
neutralises the team-stack layer -- the model's heaviest signal at
weight 4.0. Sweeping a parameter while its biggest input is dead fits
the wrong optimum. (Measured: the same dates score rho 0.36-0.43
handicapped vs 0.54-0.75 with the layer live.)

The slate is built once per date and the pool reused across every
temperature, so this costs one build per date rather than one per
(date, temperature).

    backend/.venv/Scripts/python.exe -m scripts.sweep_ownership_temperature
"""

from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import history_db  # noqa: E402
from app.services import inhouse_projections as ip  # noqa: E402
from app.services import mlb_slate, player_match  # noqa: E402

DAYS = ["2026-08-24", "2026-08-25", "2026-08-30"]


def build_pool(slate):
    """The same ownership pool mlb_slate._attach_inhouse_projections
    assembles, rebuilt here so the sweep scores the real thing."""
    pool, keys = [], {}
    for g in slate.get("games", []):
        for side in ("home", "away"):
            s = g[side]
            opp = g["away" if side == "home" else "home"]
            opp_p = opp.get("probable_pitcher") or {}
            for h in s.get("hitters") or []:
                proj = h.get("projection") or {}
                fpts = proj.get("inhouse_fpts") or proj.get("fpts")
                sal = h.get("salary") or {}
                if fpts is None or not sal.get("salary"):
                    continue
                raw = (sal.get("position") or h.get("position") or "").upper()
                positions = [p.strip() for p in raw.split("/") if p.strip()] or [
                    (h.get("position") or "OF").upper()
                ]
                pool.append(
                    {
                        "id": h["id"],
                        "position": positions[0],
                        "positions": positions,
                        "salary": sal["salary"],
                        "fpts": fpts,
                        "implied_runs": s.get("implied_runs"),
                        "opponent_pitcher_id": opp_p.get("id"),
                        "team": s.get("abbrev"),
                    }
                )
                keys[h["id"]] = player_match.normalize_name(h.get("name") or "")
            p = s.get("probable_pitcher")
            if p and p.get("edge"):
                proj = p.get("projection") or {}
                fpts = proj.get("inhouse_fpts") or proj.get("fpts")
                sal = p.get("salary") or {}
                if fpts is not None and sal.get("salary"):
                    pool.append(
                        {
                            "id": p["id"],
                            "position": "P",
                            "positions": ["P"],
                            "salary": sal["salary"],
                            "fpts": fpts,
                            "implied_runs": s.get("implied_runs"),
                            "team": s.get("abbrev"),
                        }
                    )
                    keys[p["id"]] = player_match.normalize_name(p.get("name") or "")
    return pool, keys


def score(pairs):
    top = sorted(pairs, key=lambda x: -x[1])[:30]
    hi = [(p, r) for p, r in pairs if r >= 20]
    lo = [(p, r) for p, r in pairs if r < 1]
    return {
        "mae": sum(abs(p - r) for p, r in pairs) / len(pairs),
        "chalk_mae": sum(abs(p - r) for p, r in top) / len(top),
        "pred_chalk": sum(p for p, _ in top) / len(top),
        "bias_hi": (sum(p - r for p, r in hi) / len(hi)) if hi else float("nan"),
        "bias_lo": (sum(p - r for p, r in lo) / len(lo)) if lo else float("nan"),
        "max_pred": max(p for p, _ in pairs),
    }


async def main():
    cpr = await history_db.get_contest_player_results()
    real_by_date = defaultdict(dict)
    for r in cpr:
        if r.get("ownership_pct") is not None:
            real_by_date[str(r["date"])][r["normalized_name"]] = float(r["ownership_pct"])

    built = []
    for day in DAYS:
        slate = await mlb_slate.build_slate(day, include_inhouse=True)
        pool, keys = build_pool(slate)
        built.append((day, pool, keys, real_by_date.get(day, {})))
        print(f"{day}: pool={len(pool)}")

    real_chalk = None
    saved = ip._SOFTMAX_TEMPERATURE
    print(
        f"\n{'temp':>6} {'MAE':>6} {'chalkMAE':>9} {'pred@chalk':>11} "
        f"{'bias>=20%':>10} {'bias<1%':>9} {'maxpred':>8}"
    )
    try:
        for t in (1.4, 1.1, 0.9, 0.75, 0.6, 0.5, 0.4, 0.3):
            ip._SOFTMAX_TEMPERATURE = t
            pairs = []
            for day, pool, keys, real in built:
                own = ip.project_ownership(pool)
                for pid, v in own.items():
                    r = real.get(keys.get(pid))
                    if r is not None:
                        pairs.append((v, r))
            s = score(pairs)
            if real_chalk is None:
                top = sorted(pairs, key=lambda x: -x[1])[:30]
                real_chalk = sum(r for _, r in top) / len(top)
            print(
                f"{t:>6.2f} {s['mae']:>6.2f} {s['chalk_mae']:>9.2f} {s['pred_chalk']:>10.1f}% "
                f"{s['bias_hi']:>+10.1f} {s['bias_lo']:>+9.1f} {s['max_pred']:>7.1f}%"
            )
    finally:
        ip._SOFTMAX_TEMPERATURE = saved
    print(f"\n(real ownership on those top-30 chalk players: {real_chalk:.1f}%)")


if __name__ == "__main__":
    asyncio.run(main())
