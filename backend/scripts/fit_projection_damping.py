"""Fits _PROJECTION_DAMPING against real archived contest results.

The projection is baseline x (1 + k*(multiplier - 1)). The multiplier's
components were tuned so the 0-100 DISPLAY score spreads nicely -- no
one ever checked that a 1.20 multiplier means "+20% expected DK
points". This measures k directly: for every archived (player, date)
with a real actual FPTS from a DK contest-standings export, compute the
player's own look-ahead-safe baseline (game log strictly BEFORE that
date) and his raw matchup multiplier, then solve the least-squares k in

    actual - baseline = k * baseline * (multiplier - 1)

which has the closed form k = sum(r*x) / sum(x*x) with
r = actual - baseline and x = baseline*(multiplier - 1). A grid of MAEs
around the optimum is printed too, because a single closed-form number
with no context invites over-trusting it.

The at-bat sim independently had to shrink the same composite to 0.35
of itself (EDGE_COMPOSITE_DAMPING) to keep PA probabilities sane -- if
this lands anywhere near that, two unrelated methods agree.

Run from backend/:  .venv/Scripts/python.exe -m scripts.fit_projection_damping
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from app import history_db
from app.clients import mlb
from app.services import inhouse_projections as ip
from app.services import mlb_slate, player_match, variance


async def _cutoff_baseline(player_id: int, position: str, season: int, before: str) -> float | None:
    """baseline_dk_points, but from games strictly BEFORE `before` --
    the look-ahead-safe version a backtest needs (the production one
    happily includes the very game being predicted)."""
    kind = variance.player_kind(position)
    group = "pitching" if kind == "pitcher" else "hitting"
    log = await mlb.get_player_game_log(player_id, season, group=group)
    log = [g for g in log if (g.get("date") or "9999") < before]
    own = variance.own_games(log, kind)
    if len(own) < 10:
        return None  # too thin to call a baseline at all
    season_avg = sum(own) / len(own)
    recent = own[-ip._RECENT_GAMES:]
    return (1 - ip._RECENT_WEIGHT) * season_avg + ip._RECENT_WEIGHT * (sum(recent) / len(recent))


async def main() -> int:
    rows = await history_db.get_contest_player_results()
    actual_by_day: dict[str, dict[str, float]] = defaultdict(dict)
    for r in rows:
        if r["actual_fpts"] is not None:
            actual_by_day[r["date"].isoformat()][r["normalized_name"]] = float(r["actual_fpts"])

    samples: list[tuple[float, float, float]] = []  # (baseline, raw_mult, actual)
    for day, actuals in sorted(actual_by_day.items()):
        try:
            slate = await mlb_slate.build_slate(day, include_hitters=True)
        except Exception as exc:
            print(f"{day} SKIPPED ({exc})")
            continue
        season = int(day[:4])
        day_n = 0
        for g in slate.get("games", []):
            for side in ("home", "away"):
                for h in g[side].get("hitters") or []:
                    actual = actuals.get(player_match.normalize_name(h["name"]))
                    if actual is None:
                        continue
                    raw = ip.projection_multiplier(
                        (h.get("edge") or {}).get("components") or {},
                        season_ops=(h.get("season") or {}).get("ops"),
                        vs_hand_ops=(h.get("vs_hand") or {}).get("ops"),
                    )
                    baseline = await _cutoff_baseline(h["id"], h["position"], season, day)
                    if baseline is None or baseline <= 0:
                        continue
                    samples.append((baseline, raw, actual))
                    day_n += 1
        print(f"{day}: {day_n} hitter samples")

    if len(samples) < 50:
        print(f"Only {len(samples)} samples -- not enough to fit honestly.")
        return 1

    num = sum((a - b) * b * (m - 1) for b, m, a in samples)
    den = sum((b * (m - 1)) ** 2 for b, m, a in samples)
    k_ls = num / den if den else 0.0

    print(f"\n{len(samples)} (player, day) samples")
    print(f"least-squares k = {k_ls:+.3f}")

    print("\nMAE by k (lower is better):")
    for k in [0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, round(k_ls, 2)]:
        mae = sum(abs(a - b * (1 + k * (m - 1))) for b, m, a in samples) / len(samples)
        print(f"  k={k:<5} MAE={mae:.3f}")

    raws = sorted(m for _, m, _ in samples)
    print(f"\nraw multiplier spread: p10={raws[len(raws)//10]:.3f}  "
          f"median={raws[len(raws)//2]:.3f}  p90={raws[9*len(raws)//10]:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
