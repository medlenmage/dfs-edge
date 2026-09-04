"""
NFL team-level draws -- layer 2, feeding nfl_shares.py's allocation.

Produces one `TeamDraws` per team per sim, with both teams of a matchup
drawn JOINTLY, because every interesting correlation on a slate is a
property of the game rather than of either team: opposing RBs are
negatively correlated through game script, a DST's score depends
entirely on the other team's draw, and a bring-back stack only makes
sense if both offences live in the same simulated game.

ANCHORED TO THE MARKET, NOT TO OUR OWN VIEW

Implied totals come from the closing total and spread. We are not
trying to beat the market on game environment -- the edge lives in
layer 3's allocation and in ownership, not in disagreeing with a
closing number. `implied_offset` exists for a deliberate, small,
switchable disagreement; it defaults to zero.

Sign conventions on spread_line are documented inconsistently across
sources, so implied totals come from clients/nfl.implied_team_totals(),
which derives them from the moneyline (which unambiguously says who is
favoured) rather than from the sign of the spread.

WHAT THIS MODEL DOES DIFFERENTLY FROM THE SPEC IT WAS BUILT TO

Five things were measured on 2022-2025 pbp (2,174 team-games; see
scripts/fit_nfl_team_draws.py) and came back contradicting the design
brief. In each case this follows the data.

1. SCORING COUNTS ARE UNDER-DISPERSED, NOT OVER-DISPERSED. The brief
   calls for a negative binomial "since Poisson will understate your
   ceiling". Measured var/mean is 0.824 for team TDs, 0.895 for FGs and
   0.883 for passing TDs -- all BELOW 1, where a negative binomial can
   only produce values at or above 1. It is structurally the wrong
   family and would fatten the tail in the wrong direction.

   Modelled instead as a binomial over scoring opportunities, which is
   the physical process and is naturally under-dispersed. Rushing TDs,
   sacks and INTs are near or above Poisson and are drawn separately,
   so the family is per quantity rather than one choice. See
   DRIVES_PER_TEAM for why that count must be fitted WITHIN
   implied-total buckets.

2. LEADING TEAMS RUN SLIGHTLY MORE PLAYS, NOT FEWER. The brief says to
   give the trailing team a play-count bump. Measured, the relationship
   runs the other way and is weak: blowout losers average 58.8 plays
   and blowout winners 61.8, peaking at 63.2 near a one-score lead. A
   team being blown out is not running a hurry-up all game, it is
   getting stopped and losing possessions. Slope is +0.062 plays per
   point of margin.

3. PLAY COUNTS BETWEEN OPPONENTS ARE NEGATIVELY CORRELATED (-0.48), not
   positively. Possessions inside a fixed clock are close to zero-sum:
   more snaps for one team is fewer for the other. Total game plays are
   barely more variable than one team's (sd 8.53 against 8.37), which
   is exactly what that correlation implies.

4. THE SHARED ENVIRONMENT IS THE MARKET TOTAL. The brief adds a latent
   z_game loading positively on both teams. Net of the closing lines,
   the measured residual correlation between opponents' points is only
   +0.052 -- almost all of the shared environment is already priced
   into the implied totals this model is anchored to. A latent with any
   real loading would double-count it. It is kept, at the measured
   size, rather than dropped.

5. QB-WR1 CORRELATION IS ~0.35, NOT 0.60-0.70. The brief names that band
   as the validation target for the whole model. Measured across 123
   real team-seasons of DK points (2022-2025): mean +0.349, median
   +0.370, quartiles +0.160 to +0.553. Tuning the allocation until it
   hit 0.65 would have meant roughly doubling the real number, and the
   only way to get there is to strip out the share variance that makes
   layer 3 worth having. The other same-team pairs measure QB-WR2
   +0.332, QB-TE1 +0.280, QB-RB1 +0.084, WR1-WR2 -0.004.

The brief is right about the thing it says matters most: the residual.
Margin explains only 19% of a team's pass lean, so the noise term is
most of the signal -- it stands in for the drive-by-drive path a team
took to its final margin, which a final score cannot see.

KNOWN GAP

Opponents' points come out correlated about +0.09 against a real -0.03.
The bring-back channel is calibrated on touchdowns (+0.117 against a
real +0.121), which is the number that matters for stacking; reality
offsets that with a negative field-goal coupling this model does not
have, because field-goal opportunities here do not depend on the play
count. It affects DST scoring slightly and nothing else. Left visible
rather than papered over with a correction term that would not
correspond to anything real.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from app.services.nfl_shares import TeamDraws

# --------------------------------------------------------------------------
# Fitted constants -- 2022-2025 regular season, 2,174 team-games.
# scripts/fit_nfl_team_draws.py re-derives every one of these.
# --------------------------------------------------------------------------

# Scoring, as a multinomial over scoring opportunities. The count is held
# fixed and the per-opportunity conversion rates carry the implied total.
#
# FIT THIS WITHIN IMPLIED-TOTAL BUCKETS, NOT POOLED. Pooling every game
# and solving n = mean/(1 - var/mean) gives 13.4, because the pooled
# variance is inflated by the spread of MEANS across implied totals
# rather than being the within-game variance the binomial is supposed to
# reproduce. Done pooled, simulated team points come out at sd 11.7
# against a real 9.0. Within buckets the answer is 8-9:
#
#     implied    TD mean   TD var   implied n
#     18-21        2.00     1.49       7.8
#     21-24        2.30     1.66       8.3
#     24-27        2.83     1.96       9.2
#     27-40        3.40     2.04       8.5
#
# The bucket median is 8.4; 9.0 is used because the quantity that
# actually has to come out right is team points SD, which is stable at
# 8.95-9.37 in reality across every bucket, and 9.0 lands it at 9.2
# where 8.4 overshoots. Fitting the count directly and then checking
# points is the right order -- the reverse would let this absorb an
# error from somewhere else.
#
# 9 is fewer than a team's ~12 real possessions, and should be read as
# scoring opportunities rather than drives: most three-and-outs were
# never going to produce points and carry no scoring variance.
DRIVES_PER_TEAM = 9.0
#   TDs = -0.767 + 0.1420 * implied_total      (steep)
#   FGs = +1.469 + 0.0102 * implied_total      (nearly flat)
# The two are emphatically NOT proportional: across implied totals of 17
# to 30 the TD-to-FG ratio runs 1.08 to 2.15, because higher totals come
# from red-zone conversion rather than from more field goals. A shared
# multiplier would systematically misprice high totals.
TD_INTERCEPT, TD_SLOPE = -0.767, 0.1420
FG_INTERCEPT, FG_SLOPE = 1.469, 0.0102

# points - (7*TD + 3*FG), which absorbs missed extra points, two-point
# conversions, safeties and defensive scores.
POINTS_RESIDUAL_MEAN = 0.776
POINTS_RESIDUAL_SD = 2.533

# Plays. Per team mean 61.72 sd 8.37; per GAME mean 123.42 sd 8.53.
# The game total being barely wider than one team's is the -0.48
# opponent correlation showing up: drawing the game total and splitting
# it reproduces that correlation without imposing it.
GAME_PLAYS_MEAN, GAME_PLAYS_SD = 123.42, 8.53
TEAM_PLAYS_SPLIT_SD = 7.20          # solves Var(team) = Var(game)/4 + s^2
PLAYS_PER_MARGIN = 0.0617

# Pass share of plays. This, not PROE, is what layer 3's script bends.
# The brief warns the two can diverge and they do: PROE deliberately
# conditions away down, distance and field position, which is exactly
# the situational volume effect that drives attempt counts. Regressed on
# PROE the margin slope is a fifth the size and explains 5% of variance;
# on pass share it explains 19%.
# DROPBACK share, not attempt share. A sack is a pass play: the split
# this drives is dropbacks vs rushes, and dropbacks = attempts + sacks.
# Using the attempt-based share (0.5564) here loses the sacks off the
# pass side of the ledger and hands them to the run game -- measured, it
# produced 31.2 attempts and 28.4 carries against a real 33.0 and 26.3.
#   real: (32.99 attempts + 2.44 sacks) / 61.72 plays = 0.5741
PASS_SHARE_MEAN, PASS_SHARE_SD = 0.5741, 0.1061
PASS_SHARE_PER_MARGIN = -0.003318
PASS_SHARE_RESIDUAL_SD = 0.0954

# Opponents' pass shares are NEGATIVELY correlated (-0.241): the seesaw
# dominates. Of that, -0.19 is mechanical (both sides see the same
# margin with opposite sign) and the small remainder is a genuine
# residual coupling. The brief expects a positive "both teams pass more
# in a shootout" term at this level; measured, the shootout effect
# arrives through both teams having high implied totals, not through
# their pass shares moving together.
PASS_SHARE_RESIDUAL_CORR = -0.060

# Per-attempt rates.
SACK_RATE_PER_DROPBACK = 0.0689
INT_RATE_PER_ATTEMPT = 0.0225
FUMBLE_LOST_RATE_PER_PLAY = 0.0073

# Rush share of a team's offensive TDs, by margin: goal-line volume
# rises with a lead. Measured 0.298 when trailing by more than a score,
# 0.427 when leading by more.
RUSH_TD_SHARE_BASE = 0.363
RUSH_TD_SHARE_PER_MARGIN = 0.0032

# Efficiency. A team that scored above its implied total also moved the
# ball efficiently -- corr(points above implied, TDs) is +0.814 -- so
# efficiency loads on the same latent as scoring rather than being drawn
# independently. Drawing them separately produces incoherent games (8.5
# yards an attempt with one touchdown) and a compressed joint tail.
EFF_LOGNORMAL_SD = 0.18
EFF_SCORING_LOADING = 0.55

# Turnovers load NEGATIVELY on that same latent: corr(INTs, points above
# implied) is -0.235, sacks -0.308.
INT_SCORING_LOADING = -0.235
SACK_SCORING_LOADING = -0.308

# Residual correlation between opponents' points once the lines are
# accounted for. Almost everything the brief's z_game was meant to carry
# is already in the implied totals.
SHARED_ENVIRONMENT_CORR = 0.052

# THE SHOOTOUT IS IN SCORING, NOT IN VOLUME -- which decides where the
# bring-back correlation has to come from. Measured between opponents:
#
#     pass TDs        +0.140      <- both teams score more
#     TDs             +0.121
#     points          -0.026
#     pass attempts   -0.119      <- and neither throws more
#     pass share      -0.241
#     plays           -0.480
#
# and the pass-attempt correlation gets MORE negative in high-total
# games (-0.201 above a 48 total, -0.042 below 41), which is the exact
# opposite of the brief's premise that a shootout makes both teams pass
# more. A double-stack is worth playing because both offences find the
# END ZONE together, not because both throw more often. The shared
# latent therefore loads on scoring; volume is left to the mechanical
# negative channels above.
# Correlation between the two teams' scoring copula draws, set to
# reproduce the measured +0.12 opponent TD correlation. It sits on the
# copula rather than on the conversion rate, so it costs no marginal
# accuracy.
TD_COPULA_CORR = 0.125

# How many scoring opportunities a touchdown consumes, for the purpose of
# field goals. 1.0 is the plain shared budget, and at 9 opportunities
# that is already right: measured cov(TD, FG) is -0.421 against the
# -0.445 a plain budget produces, a ratio of 0.95. An earlier 1.2 here
# was fitted against the pooled 13.4 opportunity count and became wrong
# the moment that was corrected -- constants fitted against a wrong
# constant do not survive fixing it.
FG_OPPORTUNITY_COST = 1.0


def _standard_normal_pair(rng: np.random.Generator, num_sims: int, corr: float):
    """Two correlated standard normals, for the shared-environment term."""
    a = rng.standard_normal(num_sims)
    b = corr * a + np.sqrt(max(1.0 - corr * corr, 0.0)) * rng.standard_normal(num_sims)
    return a, b


def _binomial_pmf(n: int, k: int, p: float) -> float:
    """Exact binomial pmf. n is single digit here, so this is trivial."""
    coeff = 1.0
    for i in range(k):
        coeff = coeff * (n - i) / (i + 1)
    return coeff * (p ** k) * ((1.0 - p) ** (n - k))


def _erf(x: np.ndarray) -> np.ndarray:
    """
    Abramowitz & Stegun 7.1.26, vectorized -- the normal CDF for the
    copula. numpy has no erf and scipy is not a dependency of this app;
    max error is 1.5e-7, far below the resolution of a 10-point discrete
    margin.
    """
    a1, a2, a3, a4, a5, p = (
        0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429, 0.3275911,
    )
    sign = np.sign(x)
    ax = np.abs(x)
    t = 1.0 / (1.0 + p * ax)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * np.exp(-ax * ax)
    return sign * y


def _drive_outcomes(
    rng: np.random.Generator,
    implied: float,
    latent: np.ndarray,
    num_sims: int,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Touchdowns and field goals as a multinomial over drives.

    Every drive ends in a touchdown, a field goal, or neither, so the
    two counts are drawn from one drive budget. That is what makes them
    correctly negatively correlated with each other AND under-dispersed
    relative to Poisson, which is what the real counts are.
    """
    td_mean = max(TD_INTERCEPT + TD_SLOPE * implied, 0.2)
    fg_mean = max(FG_INTERCEPT + FG_SLOPE * implied, 0.2)
    n = int(round(DRIVES_PER_TEAM))

    p_td = float(np.clip(td_mean / DRIVES_PER_TEAM, 1e-4, 0.85))
    p_fg = float(np.clip(fg_mean / DRIVES_PER_TEAM, 1e-4, 0.85))
    if p_td + p_fg > 0.95:
        scale = 0.95 / (p_td + p_fg)
        p_td, p_fg = p_td * scale, p_fg * scale

    # Touchdowns come from a Gaussian copula on an EXACT binomial
    # margin, not from tilting the conversion rate by a latent.
    #
    # Tilting was the obvious way to correlate the two teams and it does
    # not work: multiplying p_td by exp(sigma * z) adds n^2 p^2
    # (e^{sigma^2} - 1) to the variance, so buying the measured +0.12
    # opponent correlation that way pushed simulated team points to sd
    # 11.2 against a real 9.0. A copula moves the correlation without
    # touching the marginal at all -- the same reason MLB's variance.py
    # uses one. With 9 opportunities the binomial CDF is a 10-element
    # vector, so inverting it is exact and costs nothing.
    cdf = np.cumsum(
        [_binomial_pmf(n, k, p_td) for k in range(n + 1)]
    )
    uniform = 0.5 * (1.0 + _erf(latent / np.sqrt(2.0)))
    tds = np.searchsorted(cdf, np.clip(uniform, 0.0, 1.0 - 1e-12), side="right")
    tds = np.minimum(tds, n).astype(np.int64)

    # Field goals come from the opportunities a touchdown did NOT use.
    # FG_OPPORTUNITY_COST above 1 is the extra tradeoff beyond the shared
    # budget: a team converting in the red zone is not also kicking there.
    remaining = np.maximum(n - FG_OPPORTUNITY_COST * tds, 0.0)
    p_fg_given = float(np.clip(p_fg / max(1.0 - p_td, 1e-9), 0.0, 1.0))
    fgs = rng.binomial(np.round(remaining).astype(np.int64), p_fg_given)
    return tds, fgs


def simulate_game(
    home_implied: float,
    away_implied: float,
    *,
    num_sims: int,
    seed: int | None = None,
    implied_offset: tuple[float, float] = (0.0, 0.0),
) -> dict[str, TeamDraws]:
    """
    Draw `num_sims` simulated versions of one game, both teams together.

    Returns {"home": TeamDraws, "away": TeamDraws}, each array shape
    (num_sims,), ready for nfl_shares.allocate_passing/allocate_rushing.

    SEED ONCE PER SLATE, NOT PER LINEUP. Every candidate lineup has to be
    scored against the same simulated outcomes (common random numbers),
    or lineup rankings reshuffle between runs for no reason.

    `implied_offset` is the deliberate, switchable disagreement with the
    market described in the module docstring -- (home, away) points added
    to the closing implied totals. Default is no disagreement.
    """
    rng = np.random.default_rng(seed)
    implied = (home_implied + implied_offset[0], away_implied + implied_offset[1])

    # Shared environment, at the size the data actually supports once the
    # market's own view is already priced into the implied totals.
    # Two separate channels, because they are separately measured. The
    # scoring copula carries the bring-back (+0.12 opponent TD
    # correlation); the performance latents carry efficiency, sacks and
    # turnovers, and are only weakly shared (+0.05).
    score_home, score_away = _standard_normal_pair(rng, num_sims, TD_COPULA_CORR)
    latent_home, latent_away = _standard_normal_pair(
        rng, num_sims, SHARED_ENVIRONMENT_CORR
    )

    tds, fgs, points = {}, {}, {}
    for side, z_score, imp in (("home", score_home, implied[0]), ("away", score_away, implied[1])):
        td, fg = _drive_outcomes(rng, imp, z_score, num_sims)
        tds[side], fgs[side] = td, fg
        points[side] = (
            7.0 * td
            + 3.0 * fg
            + POINTS_RESIDUAL_MEAN
            + rng.normal(0.0, POINTS_RESIDUAL_SD, num_sims)
        )

    margin = {"home": points["home"] - points["away"]}
    margin["away"] = -margin["home"]

    # Plays: draw the GAME's total, then split. Splitting a shared total
    # is what produces the measured -0.48 opponent correlation, rather
    # than imposing a correlation on two independent draws.
    game_plays = rng.normal(GAME_PLAYS_MEAN, GAME_PLAYS_SD, num_sims)
    split = rng.normal(0.0, TEAM_PLAYS_SPLIT_SD, num_sims)

    # Pass-share residuals, correlated across the two teams.
    eps_home, eps_away = _standard_normal_pair(rng, num_sims, PASS_SHARE_RESIDUAL_CORR)

    out: dict[str, TeamDraws] = {}
    for side, latent, sign, eps in (
        ("home", latent_home, +1.0, eps_home),
        ("away", latent_away, -1.0, eps_away),
    ):
        m = margin[side]
        plays = game_plays / 2.0 + sign * split + PLAYS_PER_MARGIN * m
        plays = np.clip(np.round(plays), 40, 90).astype(np.int64)

        share = np.clip(
            PASS_SHARE_MEAN
            + PASS_SHARE_PER_MARGIN * m
            + PASS_SHARE_RESIDUAL_SD * eps,
            0.20,
            0.85,
        )
        dropbacks = rng.binomial(plays, share)
        rush_attempts = plays - dropbacks

        sack_rate = np.clip(
            SACK_RATE_PER_DROPBACK * np.exp(SACK_SCORING_LOADING * latent * 0.30), 0.0, 0.4
        )
        sacks = rng.binomial(dropbacks, sack_rate)
        pass_attempts = dropbacks - sacks

        int_rate = np.clip(
            INT_RATE_PER_ATTEMPT * np.exp(INT_SCORING_LOADING * latent * 0.30), 0.0, 0.25
        )
        ints = rng.binomial(pass_attempts, int_rate)
        fumbles_lost = rng.binomial(plays, FUMBLE_LOST_RATE_PER_PLAY)

        rush_share = np.clip(
            RUSH_TD_SHARE_BASE + RUSH_TD_SHARE_PER_MARGIN * m, 0.05, 0.80
        )
        rush_tds = rng.binomial(tds[side], rush_share)
        pass_tds = tds[side] - rush_tds

        # script is standardized pass lean, unit SD by construction --
        # layer 3's betas are calibrated against that scale.
        script = (share - PASS_SHARE_MEAN) / PASS_SHARE_SD

        eff = np.exp(
            EFF_LOGNORMAL_SD * (EFF_SCORING_LOADING * latent)
            - 0.5 * (EFF_LOGNORMAL_SD * EFF_SCORING_LOADING) ** 2
        )

        out[side] = TeamDraws(
            pass_attempts=pass_attempts,
            pass_tds=pass_tds,
            pass_eff=eff,
            rush_attempts=rush_attempts,
            rush_tds=rush_tds,
            rush_eff=eff,
            script=script,
            sacks=sacks,
            ints=ints,
            fumbles_lost=fumbles_lost,
            points=points[side],
        )
    return out


def reconcile(
    team: TeamDraws,
    *,
    target_share: np.ndarray,
    catch_rate: np.ndarray,
    yards_per_rec: np.ndarray,
    projected_team_pass_yards: float,
    tolerance: float = 0.02,
) -> dict[str, Any]:
    """
    THE CHECK THAT OTHERWISE CORRUPTS CALIBRATION SILENTLY.

    Layer 3 computes receiving yards as receptions x yards_per_rec x
    pass_eff, so team passing yards is an EMERGENT SUM of player draws --
    not something this layer sets. Which means at pass_eff = 1.0 the
    player priors must already imply the projected team passing yards.

    If they don't, pass_eff quietly absorbs a level bias, and
    calibration step (c) -- matching per-player fantasy-point SD --
    "fixes" it by distorting variance to compensate. The result is
    plausible-looking per-player marginals sitting on wrong
    correlations: invisible in the output, fatal in lineup rankings.
    Check the level BEFORE touching the spread.

    Returns the comparison rather than raising, so a caller can log it
    per slate; `ok` is False when the drift exceeds `tolerance`.
    """
    implied_yards = float(
        np.mean(team.pass_attempts)
        * float(np.sum(np.asarray(target_share) * np.asarray(catch_rate) * np.asarray(yards_per_rec)))
    )
    if projected_team_pass_yards <= 0:
        return {"ok": False, "implied": implied_yards, "projected": projected_team_pass_yards,
                "drift": float("nan")}
    drift = (implied_yards - projected_team_pass_yards) / projected_team_pass_yards
    return {
        "ok": abs(drift) <= tolerance,
        "implied": round(implied_yards, 1),
        "projected": round(projected_team_pass_yards, 1),
        "drift": round(drift, 4),
    }
