"""Measures the pairwise correlations the NFL Monte Carlo engine
actually INDUCES between stack partners, against the real league values
nfl_correlations.py computes from game logs.

The review's point: nfl_correlations.py measures the real numbers, but
simulate_batch's hand-set team multiplier never consumed them -- so the
sim's stacks could be arbitrarily far from real correlation, and a
QB+WR stack's simulated ROI meant little. This script is the honest
feedback loop for tuning the sim's constants to the measured targets.

Run from backend/:  .venv/Scripts/python.exe -m scripts.measure_nfl_sim_correlations
"""

from __future__ import annotations

import numpy as np

from app.services import nfl_variance


def _pool(mean: float, spread: float) -> list[float]:
    """A plausible outcome pool: mean +/- spread, floored at zero."""
    raw = np.random.default_rng(1).normal(mean, spread, 200)
    return [max(0.0, float(v)) for v in raw]


def measure(num_trials: int = 40000) -> dict[str, float]:
    players = [
        {"id": "qb", "position": "QB", "team": "AAA", "opponent": "BBB", "projected_fpts": 20.0},
        {"id": "wr1", "position": "WR", "team": "AAA", "opponent": "BBB", "projected_fpts": 16.0},
        {"id": "wr2", "position": "WR", "team": "AAA", "opponent": "BBB", "projected_fpts": 12.0},
        {"id": "te1", "position": "TE", "team": "AAA", "opponent": "BBB", "projected_fpts": 9.0},
        {"id": "rb1", "position": "RB", "team": "AAA", "opponent": "BBB", "projected_fpts": 15.0},
        {"id": "opp_wr1", "position": "WR", "team": "BBB", "opponent": "AAA", "projected_fpts": 16.0},
    ]
    pools = {p["id"]: _pool(p["projected_fpts"], p["projected_fpts"] * 0.6) for p in players}
    entries = [{"players": [p]} for p in players]
    sim = nfl_variance.simulate_batch(entries, pools, num_trials=num_trials, seed=42)

    def corr(a: int, b: int) -> float:
        return float(np.corrcoef(sim[a], sim[b])[0, 1])

    return {
        "qb_wr1": corr(0, 1),
        "qb_wr2": corr(0, 2),
        "qb_te1": corr(0, 3),
        "qb_rb1": corr(0, 4),
        "qb_bring_back_wr1": corr(0, 5),
    }


def main() -> int:
    measured_2025 = {
        "qb_wr1": 0.355, "qb_wr2": 0.353, "qb_te1": 0.290,
        "qb_rb1": 0.042, "qb_bring_back_wr1": 0.134,
    }
    induced = measure()
    print(f"{'pair':<20} {'real 2025':>9} {'sim-induced':>11}")
    for k, real in measured_2025.items():
        print(f"{k:<20} {real:>9.3f} {induced[k]:>11.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
