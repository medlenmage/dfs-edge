"""
NFL player-level volume allocation -- the structural alternative to
sampling each player independently and imposing a correlation constant
afterwards.

WHAT THIS IS FOR

`nfl_variance.py` builds each player's outcomes by resampling his own
history and then multiplying by a shared team factor, with correlation
set by hand (TEAM_SENSITIVITY, GAME_CORRELATION). That reproduces an
average correlation but not its SHAPE: it cannot express "one guy ate
the entire game", because a fixed share means the WR1 who draws a 40%
target share in a sim is not a different player from the WR1 who draws
18%. Those single-player explosions are a large part of what wins a
GPP, and a fixed-share model underweights them by construction.

This module allocates a team's volume among its players per simulated
game, so the correlation is a CONSEQUENCE of the allocation rather than
a parameter:

  - Shares come from a Dirichlet around projection, so usage varies.
  - Counts come from an exact multinomial, so player targets sum to team
    attempts and player TDs sum to team TDs. That is what produces the
    correct NEGATIVE correlation between teammates on touchdowns -- one
    man scoring means another did not.
  - Game script bends mean shares BEFORE the draw, so a pass-catching
    back's target share rises in exactly the sims where his team is
    trailing. A static correlation matrix cannot represent that.

Everything is vectorized over sims: team quantities are (S,), player
parameters are (P,), outputs are (S, P).

STATUS: not yet wired into the NFL simulator. This is layer 3 of three
-- it consumes per-sim TEAM draws (attempts, TDs, efficiency, script)
that a team-level layer has to produce first. It is landed separately,
calibrated and tested, because the calibration below is a prerequisite
for that layer regardless of how it gets built, and because fitting it
against real data is the step the design most warns against skipping.

CALIBRATION

The order that works, from the design this implements:
  (a) fit k per position from the historical SD of realized share
  (b) fit s per position so simulated per-touch yardage variance matches
  (c) only THEN check simulated fantasy-point SD per player
Skipping to (c) compensates for an error in one layer with an
offsetting error in another.

(a) and (b) are done -- see the constants below and
`scripts/fit_nfl_shares.py`, which re-derives them from real game logs.
(c) needs the team layer, so it is genuinely open.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

EPS = 1e-12


# --------------------------------------------------------------------------
# Measured constants
# --------------------------------------------------------------------------
#
# DIRICHLET CONCENTRATION. For Dirichlet(k * p), Var(share_i) =
# p_i(1 - p_i) / (k + 1), so k is recoverable from the historical SD of
# a player's realized weekly share around his own mean. Higher k =
# shares hug projection more tightly.
#
# Fitted on the 2025 regular season (see scripts/fit_nfl_shares.py),
# players with 8+ games and a mean share above 2% (below that a "share"
# is noise, not a role), team-games with 15+ attempts:
#
#   targets    WR  n=145  k = 24.8      carries   RB  n=89  k = 10.3
#              TE  n= 81  k = 36.3                QB  n=37  k = 21.6
#              RB  n= 77  k = 26.7                WR  n=15  k = 26.5
#
# TE target share being the stickiest of the three and RB carries the
# loosest is the expected ordering: a backfield is genuinely a
# committee that game script and goal-line work move around, while a
# team's route participation is close to a fixed role.
TARGET_CONCENTRATION: dict[str, float] = {"WR": 24.8, "TE": 36.3, "RB": 26.7, "QB": 27.0}
CARRY_CONCENTRATION: dict[str, float] = {"RB": 10.3, "QB": 21.6, "WR": 26.5, "TE": 20.0}
DEFAULT_TARGET_CONCENTRATION = 27.2   # all-position median
DEFAULT_CARRY_CONCENTRATION = 16.0

# GAMMA EXPLOSIVENESS. yards | n ~ Gamma(shape = n*s, scale = ypX/s),
# which gives mean exactly n*ypX and Var(y | n) = n * ypX^2 / s. Lower
# s = fatter tail. Because shape scales with n, a 12-catch game is
# proportionally tighter than a 2-catch game, which is how real
# yardage behaves.
#
# MEASURING THIS CORRECTLY MATTERS. The naive estimate -- pool every
# game's yards-per-touch and take mean^2/variance -- is inflated by
# roughly the average touch count, because it ignores that Var(y/n)
# shrinks with n. Done that way the same 2025 data reads WR 3.07 and
# RB-rushing 2.70; conditioning on n properly gives 1.27 and 0.42. The
# fitted values below come from s = n * ypX^2 / Var(y | n) evaluated
# per touch-count bucket and combined by weighted median, and they are
# stable across buckets (WR receiving reads 1.18 / 1.27 / 1.44 / 1.27 /
# 1.49 at n = 1..5), which is itself evidence the shape-scales-with-n
# model is right.
#
# The single defaults this design ships with (0.8 receiving, 0.7
# rushing) are materially off in both directions on real data: they
# would overstate WR and TE receiving-yardage variance by 60-100% and
# understate rushing variance by 40-60%. Per position, not one number.
RECEIVING_EXPLOSIVENESS: dict[str, float] = {"RB": 0.70, "WR": 1.27, "TE": 1.59, "QB": 1.27}
RUSHING_EXPLOSIVENESS: dict[str, float] = {"QB": 0.31, "RB": 0.42, "WR": 0.54, "TE": 0.45}
DEFAULT_RECEIVING_EXPLOSIVENESS = 1.20
DEFAULT_RUSHING_EXPLOSIVENESS = 0.45

# Measured 2025 catch rates, used only when a player has no fitted rate
# of his own. RB 0.772 / WR 0.619 / TE 0.718.
LEAGUE_CATCH_RATE: dict[str, float] = {"RB": 0.772, "WR": 0.619, "TE": 0.718, "QB": 0.650}

# How hard touchdown odds follow a volume-share surprise. Without this,
# a sim where the WR1 draws a 40% target share still gives him only his
# baseline TD odds, which flattens the upper tail exactly where GPP
# equity lives. 0 decouples them; 1 moves TD odds one-for-one with the
# share surprise. Not yet fitted against real data -- it needs the team
# layer to check a simulated fantasy-point distribution against
# history, which is calibration step (c).
DEFAULT_TD_COUPLING = 0.75

# Game-script elasticity magnitudes that are defensible out loud: at
# script = +2 (heavily trailing) a beta of 0.35 multiplies a player's
# weight by e^0.7 ~ 2.0 before renormalizing. Anything past this is
# claiming a usage split nobody would defend.
MAX_ABS_SCRIPT_BETA = 0.35


def _per_position(values: dict[str, float], positions: list[str], default: float) -> np.ndarray:
    return np.array([values.get(p, default) for p in positions], dtype=float)


def blended_concentration(
    values: dict[str, float],
    positions: list[str],
    shares: np.ndarray,
    default: float,
) -> float:
    """
    Collapse per-position concentrations into the ONE scalar a Dirichlet
    can take, weighted by how much of the volume each player carries.

    WHY THIS CANNOT BE PER-PLAYER. It is tempting to give each player his
    own fitted k, since the measured values differ (TE targets 36.3
    against RB carries 10.3). A Dirichlet will not have it: its mean is
    alpha_i / sum(alpha), so alpha_i = k_i * p_i means the mean share
    becomes k_i*p_i / sum(k_j*p_j), which is NOT p_i unless every k is
    equal. Doing it per-player silently drags share toward whichever
    positions were fitted tightest -- on a four-man group that moved a
    34% target share to 30.3%, a 3.7-point bias in the central case
    before any variance was added.

    Only alpha_i proportional to p_i preserves the mean, so the
    concentration is scalar by construction. Weighting by share is the
    reasonable collapse: it makes the group's overall share variance
    match the players who actually carry the volume. Genuinely
    per-player variance control needs a different distribution (a
    generalized Dirichlet or a logistic-normal), which is a real change
    of model rather than a parameter tweak.
    """
    weights = np.asarray(shares, dtype=float)
    total = weights.sum()
    if total <= 0:
        return default
    per_player = _per_position(values, positions, default)
    return float((per_player * weights).sum() / total)


# --------------------------------------------------------------------------
# Calibration helper
# --------------------------------------------------------------------------


def solve_concentration(mean_share: np.ndarray, realized_sd: np.ndarray) -> float:
    """
    Back out the Dirichlet concentration k from historical share data.

    Inverts Var = p(1-p)/(k+1) per player and takes the median, so one
    unusual usage profile cannot drag the fit. Players whose implied k
    is non-finite or non-positive (a share SD wider than the binomial
    bound allows, which happens on tiny samples) are dropped rather
    than clipped -- clipping them would quietly pull the median toward
    whatever bound was chosen.
    """
    p = np.asarray(mean_share, dtype=float)
    v = np.asarray(realized_sd, dtype=float) ** 2
    k = p * (1.0 - p) / np.maximum(v, EPS) - 1.0
    usable = k[np.isfinite(k) & (k > 0)]
    if usable.size == 0:
        return float("nan")
    return float(np.median(usable))


# --------------------------------------------------------------------------
# Core primitives
# --------------------------------------------------------------------------


def script_adjusted_shares(
    base_share: np.ndarray,   # (P,)
    beta: np.ndarray,         # (P,) elasticity to game script
    script: np.ndarray,       # (S,) positive = trailing / pass-leaning, ~z units
) -> np.ndarray:
    """
    Bend mean shares by game script, then renormalize. Returns (S, P).

    beta > 0 grows the share when trailing (pass-down back, slot WR);
    beta < 0 shrinks it (early-down grinder, blocking TE); 0 is neutral.

    This is the piece a static correlation matrix cannot express. The
    same draw that set the team's pass volume also sets `script`, so a
    back's target share rises in exactly the sims where his team is
    behind -- the correlation falls out of the mechanism instead of
    being asserted.
    """
    log_w = np.log(np.maximum(base_share, EPS))[None, :] + beta[None, :] * script[:, None]
    log_w -= log_w.max(axis=1, keepdims=True)          # stabilize before exp
    w = np.exp(log_w)
    return w / np.maximum(w.sum(axis=1, keepdims=True), EPS)


def draw_dirichlet(rng: np.random.Generator, alpha: np.ndarray) -> np.ndarray:
    """
    Row-wise Dirichlet for a per-sim alpha matrix of shape (S, P).

    Generator.dirichlet only accepts a 1-D alpha, which is no use here
    because game script makes alpha vary by sim. Normalizing
    independent Gammas is the exact vectorized equivalent.
    """
    g = rng.gamma(shape=np.maximum(alpha, EPS), scale=1.0)
    return g / np.maximum(g.sum(axis=1, keepdims=True), EPS)


def multinomial_rows(rng: np.random.Generator, n: np.ndarray, p: np.ndarray) -> np.ndarray:
    """
    Exact multinomial with per-row probabilities. n: (S,), p: (S, P).

    Conditional-binomial decomposition: P numpy calls, no (S, n, P)
    intermediate, and rows sum to exactly n. That exactness is the
    point -- it is what makes teammates' touchdowns correctly NEGATIVELY
    correlated, since a team's TDs are a fixed quantity being divided.
    """
    _, num_players = p.shape
    out = np.zeros(p.shape, dtype=np.int64)
    remaining = np.asarray(n, dtype=np.int64).copy()
    p_left = np.ones(p.shape[0], dtype=float)

    for j in range(num_players - 1):
        pj = np.where(p_left > EPS, p[:, j] / np.maximum(p_left, EPS), 0.0)
        draw = rng.binomial(remaining, np.clip(pj, 0.0, 1.0))
        out[:, j] = draw
        remaining -= draw
        p_left -= p[:, j]

    out[:, num_players - 1] = remaining
    return out


def gamma_yards(
    rng: np.random.Generator,
    counts: np.ndarray,      # (S, P) touches
    per_touch: np.ndarray,   # (P,) yards per touch
    team_eff: np.ndarray,    # (S,) team efficiency multiplier
    s: np.ndarray,           # (P,) explosiveness; lower = fatter tail
) -> np.ndarray:
    """
    Yardage conditional on touch count. Returns (S, P), exactly 0 where
    a player got no touches.

    `s` is per-player rather than one scalar because the measured value
    differs by position by more than a factor of two (see
    RECEIVING_EXPLOSIVENESS / RUSHING_EXPLOSIVENESS).
    """
    s_row = np.asarray(s, dtype=float)[None, :]
    shape = counts * s_row
    scale = per_touch[None, :] * team_eff[:, None] / s_row
    drawn = rng.gamma(shape=np.maximum(shape, EPS), scale=scale)
    return np.where(counts > 0, drawn, 0.0)


# --------------------------------------------------------------------------
# Containers
# --------------------------------------------------------------------------


@dataclass
class PassPriors:
    """Per-player receiving priors for one team. Every array is (P,)."""

    names: list[str]
    positions: list[str]
    target_share: np.ndarray          # sums to 1
    td_share: np.ndarray              # receiving-TD prior, sums to 1
    yards_per_rec: np.ndarray
    script_beta: np.ndarray
    catch_rate: np.ndarray | None = None
    k: float | None = None
    s: np.ndarray | None = None
    td_coupling: float = DEFAULT_TD_COUPLING

    def __post_init__(self) -> None:
        if self.catch_rate is None:
            self.catch_rate = _per_position(LEAGUE_CATCH_RATE, self.positions, 0.65)
        if self.k is None:
            self.k = blended_concentration(
                TARGET_CONCENTRATION, self.positions, self.target_share,
                DEFAULT_TARGET_CONCENTRATION,
            )
        if self.s is None:
            self.s = _per_position(
                RECEIVING_EXPLOSIVENESS, self.positions, DEFAULT_RECEIVING_EXPLOSIVENESS
            )


@dataclass
class RushPriors:
    """Per-player rushing priors for one team. Every array is (P,)."""

    names: list[str]
    positions: list[str]
    rush_share: np.ndarray
    td_share: np.ndarray
    yards_per_carry: np.ndarray
    script_beta: np.ndarray
    k: float | None = None
    s: np.ndarray | None = None
    td_coupling: float = DEFAULT_TD_COUPLING

    def __post_init__(self) -> None:
        if self.k is None:
            self.k = blended_concentration(
                CARRY_CONCENTRATION, self.positions, self.rush_share,
                DEFAULT_CARRY_CONCENTRATION,
            )
        if self.s is None:
            self.s = _per_position(
                RUSHING_EXPLOSIVENESS, self.positions, DEFAULT_RUSHING_EXPLOSIVENESS
            )


@dataclass
class TeamDraws:
    """Per-sim team-level draws from the team layer, all shape (S,)."""

    pass_attempts: np.ndarray
    pass_tds: np.ndarray
    pass_eff: np.ndarray
    rush_attempts: np.ndarray
    rush_tds: np.ndarray
    rush_eff: np.ndarray
    script: np.ndarray


# --------------------------------------------------------------------------
# Allocation
# --------------------------------------------------------------------------


def _coupled_td_weights(
    td_share: np.ndarray,     # (P,)
    drawn: np.ndarray,        # (S, P) drawn volume shares
    base: np.ndarray,         # (S, P) script-adjusted mean shares
    coupling: float,
) -> np.ndarray:
    """
    Tilt touchdown weights toward whoever drew an unusually large volume
    share in that sim, so the biggest usage games are also the biggest
    scoring chances -- which is where tournament equity actually lives.
    """
    tilt = (np.maximum(drawn, EPS) / np.maximum(base, EPS)) ** coupling
    weights = td_share[None, :] * tilt
    return weights / np.maximum(weights.sum(axis=1, keepdims=True), EPS)


def allocate_passing(
    rng: np.random.Generator, team: TeamDraws, priors: PassPriors
) -> dict[str, Any]:
    """Allocate a team's per-sim pass volume and receiving TDs."""
    base = script_adjusted_shares(priors.target_share, priors.script_beta, team.script)
    shares = draw_dirichlet(rng, float(priors.k) * base)

    targets = multinomial_rows(rng, team.pass_attempts, shares)
    receptions = rng.binomial(targets, np.clip(np.asarray(priors.catch_rate), 0.0, 1.0)[None, :])
    yards = gamma_yards(rng, receptions, priors.yards_per_rec, team.pass_eff, priors.s)

    td_weights = _coupled_td_weights(priors.td_share, shares, base, priors.td_coupling)
    tds = multinomial_rows(rng, team.pass_tds, td_weights)

    return {
        "names": priors.names,
        "target_share": shares,
        "targets": targets,
        "receptions": receptions,
        "rec_yards": yards,
        "rec_tds": tds,
    }


def allocate_rushing(
    rng: np.random.Generator, team: TeamDraws, priors: RushPriors
) -> dict[str, Any]:
    """Allocate a team's per-sim rush volume and rushing TDs."""
    base = script_adjusted_shares(priors.rush_share, priors.script_beta, team.script)
    shares = draw_dirichlet(rng, float(priors.k) * base)

    carries = multinomial_rows(rng, team.rush_attempts, shares)
    yards = gamma_yards(rng, carries, priors.yards_per_carry, team.rush_eff, priors.s)

    td_weights = _coupled_td_weights(priors.td_share, shares, base, priors.td_coupling)
    tds = multinomial_rows(rng, team.rush_tds, td_weights)

    return {
        "names": priors.names,
        "rush_share": shares,
        "carries": carries,
        "rush_yards": yards,
        "rush_tds": tds,
    }
