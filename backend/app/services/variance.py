"""
Per-player DK-fantasy-point outcome distributions, built from real
game logs (clients/mlb.get_player_game_log() + mlb_dk_points.py) --
Phase 2 of the player-outcome variance model (see
.claude/plans/clever-strolling-hearth.md for the full roadmap).

Returns a bootstrap resampling POOL per player: a list of real,
observed DK-point values from games they actually played, meant to be
sampled from (with replacement) rather than fit to a parametric shape
(normal, log-normal, ...) that could misrepresent a streaky or
boom/bust player's real behavior. Phase 4's Monte Carlo engine draws
from this pool once per simulated trial.

THIN SAMPLES
------------
A rookie call-up or a player who missed most of the season has too few
of his own games to trust alone -- the same problem scoring.py's
_shrink() solves for split-based components, just applied to a whole
distribution instead of a single scalar. Instead of shrinking toward a
single neutral number, this blends in real games from OTHER players at
the same DK roster slot: a shared, cache-accumulated "position pool"
that grows organically as the app is used (every call contributes its
player's own games to it), rather than eagerly fetching every league
player's game log up front just to build it, which would be dozens of
extra API calls before any of this could return an answer. The blend
weight shifts from "mostly position pool" to "almost entirely the
player's own history" as his own game count approaches
MIN_GAMES_FULL_TRUST -- a thin-sample player queried before the shared
pool has warmed up just falls back to his own (still real, if sparse)
games, never a fabricated number.
"""

from __future__ import annotations

import asyncio
import math
import random
from typing import Any

import numpy as np

from app import cache
from app.clients import mlb
from app.config import get_settings
from app.services import mlb_dk_points
from app.services.lineup_export import SLOT_LABELS, players_in_slot_order
from app.services.optimizer import SLOT_REQUIREMENTS

# Games before a player's own history is trusted on its own -- pitchers
# get a lower bar since even a full-time starter only makes ~30 starts
# a season, versus an everyday hitter's ~140+ games.
MIN_GAMES_FULL_TRUST = {"hitter": 50, "pitcher": 15}

# Size of the returned resampling pool -- large enough for a Monte
# Carlo sampler to draw from without visibly repeating, small enough
# to stay cheap to store and resample from.
POOL_SIZE = 200

# How many of the most recently contributed games a shared
# same-position pool keeps -- bounds its growth across a long-running
# server process instead of accumulating forever.
MAX_POSITION_POOL_SIZE = 2000

PITCHER_SLOT = "P"


def player_kind(position: str) -> str:
    return "pitcher" if position == PITCHER_SLOT else "hitter"


def own_games(game_log: list[dict[str, Any]], kind: str) -> list[float]:
    """DK points for every game the player actually appeared in --
    a zero-PA/zero-out row is a non-appearance, not a real outcome."""
    if kind == "pitcher":
        return [
            mlb_dk_points.pitcher_game_points(g) for g in game_log if g.get("outs", 0) > 0
        ]
    return [
        mlb_dk_points.hitter_game_points(g)
        for g in game_log
        if g.get("plate_appearances", 0) > 0
    ]


def _position_pool_key(position: str, season: int) -> str:
    return f"variance:position_pool:{season}:{position}"


def contribute_to_position_pool(position: str, season: int, games: list[float]) -> None:
    """
    Read-modify-write into the shared same-position pool -- same
    pattern lineup_watch.py already uses for its own per-day
    accumulation. Fine without locking: this is a single-user local
    app, not a service with real write concurrency.
    """
    if not games:
        return
    key = _position_pool_key(position, season)
    existing = cache.get(key) or []
    combined = (existing + games)[-MAX_POSITION_POOL_SIZE:]
    cache.put(key, combined, get_settings().ttl_game_logs)


def position_pool(position: str, season: int) -> list[float]:
    return cache.get(_position_pool_key(position, season)) or []


async def player_outcome_pool(
    player_id: int, position: str, season: int, *, seed: int | None = None, as_of_date: str | None = None
) -> list[float]:
    """
    A POOL_SIZE-length bootstrap resampling pool of DK-point outcomes
    for one player -- draw from it uniformly (with replacement) to
    simulate one game's result for them. Blends in the shared
    same-position pool for thin samples; a player with a full season's
    worth of games draws almost entirely from his own real history.

    `as_of_date` (an ISO date string), if given, excludes any game on
    or after it -- for backtesting only: projecting a real historical
    date's outcomes from the season's FULL game log (the normal,
    correct behavior for a live request) would let games that hadn't
    happened yet leak into the "prediction," which is exactly the kind
    of look-ahead bias that can make a backtest look better calibrated
    than the live model actually is. Skips contributing to the shared
    same-position pool when set, too -- a backtest re-deriving a past
    snapshot shouldn't mutate the live shared pool other real (present-
    day) requests draw from. Cached under a date-scoped key so this
    never collides with (or gets served) a live, unfiltered pool.
    """
    settings = get_settings()

    async def _load() -> list[float]:
        kind = player_kind(position)
        group = "pitching" if kind == "pitcher" else "hitting"
        game_log = await mlb.get_player_game_log(player_id, season, group=group)
        if as_of_date is not None:
            game_log = [g for g in game_log if (g.get("date") or "") < as_of_date]
        own = own_games(game_log, kind)
        if as_of_date is None:
            contribute_to_position_pool(position, season, own)

        if not own:
            # No games of his own yet this season (hasn't debuted, or
            # a rookie with zero PA/IP so far) -- nothing but the
            # shared pool to draw from.
            pool = position_pool(position, season)
            return pool[:POOL_SIZE] if pool else [0.0]

        full_trust = MIN_GAMES_FULL_TRUST[kind]
        trust = min(1.0, len(own) / full_trust)
        # Fall back to his own games if the shared pool hasn't warmed
        # up yet -- better than blending in nothing at all.
        shared_pool = position_pool(position, season) or own

        rng = random.Random(seed)
        return [
            rng.choice(own) if rng.random() < trust else rng.choice(shared_pool)
            for _ in range(POOL_SIZE)
        ]

    cache_key = f"variance:pool:{player_id}:{season}:{position}"
    if as_of_date is not None:
        cache_key += f":asof{as_of_date}"
    return await cache.cached(cache_key, settings.ttl_game_logs, _load)


# ---------------------------------------------------------------------------
# Boom / bust
# ---------------------------------------------------------------------------
#
# "Boom" and "bust" are direct tail reads of a player's own bootstrap
# outcome pool against TODAY'S projection -- what fraction of his real,
# already-played games would have cleared 1.5x the number he's projected
# for today, and what fraction were the nightmare night. No new model:
# the pool is the exact same one the Monte Carlo simulator draws from
# (player_outcome_pool above), so these percentages and the simulator
# can never disagree about what a player's distribution looks like.
#
# The projection is the denominator on purpose. A pool whose MEAN sits
# above today's projection booms often (the market/projection is asking
# less of him than he usually delivers); one priced for perfection booms
# rarely. Measured on the real 2026-08-30 slate: Parker Messick
# (projected 15.5, pool mean 19.9) boomed in 49.5% of his real games,
# while Tyler Glasnow (projected 20.1, pool mean 17.5) boomed in 10.5%
# -- which is exactly the leverage signal the number is meant to carry.

# "Big margin" over the projection. 1.5x is the headline; the 1.75x/2x
# ladder ships alongside for the tooltip. Measured medians on a real
# slate: pitchers 30% / 10% at 1.5x / 2x, hitters 14% / 5.5% -- real
# spread in both directions, so the thresholds discriminate rather than
# pinning everyone to the same number.
BOOM_MULTIPLIERS = (1.5, 1.75, 2.0)

# A hitter's bust is the 0-for-4-with-nothing night: exactly 0 DK
# points (a hitter can't go negative under DK Classic MLB scoring).
# Median 17% on a real slate, spread 9-25%.
HITTER_BUST_MAX = 0.0
# A pitcher's bust is getting shelled -- knocked out in the 2nd or 3rd,
# negative score included. 3 IP with any earned runs already sits below
# 5 DK points (2.25/IP minus 2/ER and 0.6 per baserunner), so <= 5
# captures "the start was a disaster" while <= 0 alone would only catch
# the very worst of them. Median 20% on a real slate, spread 0-37.5%
# (Messick has never scored <= 5 this season; Scherzer does it 37.5%
# of the time).
PITCHER_BUST_MAX = 5.0


def boom_bust_from_pool(
    pool: list[float], projection: float | None, kind: str
) -> dict[str, float] | None:
    """
    Boom%/bust% for one player from his own outcome pool vs today's
    projection -- see the section note above for what each threshold
    means and how it was calibrated. `kind` is player_kind()'s output;
    it only changes which bust threshold applies. Returns None when
    there's nothing real to read (no pool, or no positive projection to
    measure against).
    """
    if not pool or not projection or projection <= 0:
        return None
    n = len(pool)
    bust_max = PITCHER_BUST_MAX if kind == "pitcher" else HITTER_BUST_MAX
    out = {
        f"boom_{str(m).replace('.', '')}x_pct": round(
            100 * sum(1 for x in pool if x >= m * projection) / n, 1
        )
        for m in BOOM_MULTIPLIERS
    }
    return {
        "boom_pct": out["boom_15x_pct"],
        "boom_175x_pct": out["boom_175x_pct"],
        "boom_2x_pct": out["boom_20x_pct"],
        "bust_pct": round(100 * sum(1 for x in pool if x <= bust_max) / n, 1),
    }


# Trials for the stack-level Monte Carlo below. 2,000 puts the standard
# error of a ~25% probability at ~1 point -- plenty for a two-decimal-
# free display column -- while keeping a full slate's 18 team stacks
# under a couple of seconds combined.
STACK_BOOM_TRIALS = 2000

# A stack's bust is the offense getting shut down: the top-5 bats
# combining for no more than HALF their combined projection. Half
# rather than the individual thresholds because a 5-man SUM almost
# never hits literal zero -- someone scratches out a single -- but
# landing at half the projected total is precisely the "this stack
# killed my lineups" night. Measured spread on a real slate: 18-44%.
STACK_BUST_FRACTION = 0.5


def stack_boom_bust(
    sorted_pools: list[list[float]],
    projections: list[float],
    edges: list[float | None],
    *,
    trials: int = STACK_BOOM_TRIALS,
    seed: int | None = 11,
) -> dict[str, float] | None:
    """
    Boom%/bust% for a team's top-5 stack, from a real correlated Monte
    Carlo over the five hitters' own outcome pools -- NOT five
    independent tail reads multiplied together. Teammates share each
    trial's team_environment_multiplier() exactly as the full simulator
    correlates them, which is what makes a stack a stack: the whole
    point of rostering five bats together is that their big nights
    cluster, and independent sampling would thin both tails and
    understate boom AND bust alike.

    `sorted_pools` must each already be sorted ascending (the same
    contract sample_correlated_outcome() has). Boom uses the same 1.5x
    headline threshold the per-player numbers use, against the five
    projections' SUM; bust is STACK_BUST_FRACTION of that sum.

    Deterministic for a fixed seed so the Stacks tab doesn't flicker a
    fraction of a point on every refresh.
    """
    if len(sorted_pools) < 2 or len(sorted_pools) != len(projections):
        return None
    proj_sum = sum(projections)
    if proj_sum <= 0:
        return None
    rng = random.Random(seed)
    boom = bust = 0
    boom_line = 1.5 * proj_sum
    bust_line = STACK_BUST_FRACTION * proj_sum
    for _ in range(trials):
        team_mult = team_environment_multiplier(rng)
        total = 0.0
        for pool, edge in zip(sorted_pools, edges):
            total += sample_correlated_outcome(
                pool, rng, team_multiplier=team_mult, own_edge=edge
            )
        if total >= boom_line:
            boom += 1
        if total <= bust_line:
            bust += 1
    return {
        "boom_pct": round(100 * boom / trials, 1),
        "bust_pct": round(100 * bust / trials, 1),
    }


def ceiling_from_pool(pool: list[float], percentile: float = 0.9) -> float:
    """
    The `percentile`-th percentile of a player's own outcome pool -- a
    real, data-driven ceiling (not a guess or a flat multiple of the
    mean), reusing the exact same bootstrap resampling pool the Monte
    Carlo simulator already draws from. The "upside" half of a leverage
    score (ceiling - ownership%) -- see inhouse_projections.py's
    player_ceilings().
    """
    if not pool:
        return 0.0
    ordered = sorted(pool)
    idx = round(percentile * (len(ordered) - 1))
    return ordered[idx]


# --------------------------------------------------------------------------
# Team correlation (Phase 3) + matchup conditioning and pitcher/opponent
# anti-correlation (Phase 6)
# --------------------------------------------------------------------------
#
# A team's whole offense having a big or small day together is why
# stacking exists as a GPP strategy in the first place -- a variance
# model that samples every player fully independently would make it
# pointless, since a big Yankees game wouldn't make Yankees hitters any
# more likely to all do well together. This is a deliberately simple
# v1 (same "clearly-labeled approximation, not a data-derived model"
# philosophy contest.py's payout curve already uses, not a full
# paired-game covariance model): a team's day is a single multiplier,
# drawn once per Monte Carlo trial, that biases which percentile of
# EACH of that team's hitters' own outcome pool gets sampled that
# trial -- correlated *within* a team and trial, independent *across*
# teams and trials.
#
# Two more signals bias that same percentile target, reusing the exact
# mechanism above rather than inventing a new one:
#
#   - Own matchup quality (scoring.py's `edge.composite`, already
#     computed for every hitter and pitcher -- platoon, park, weather,
#     opposing pitcher/bullpen quality) nudges a player toward the
#     better or worse end of HIS OWN real history for today's specific
#     matchup, instead of sampling blind to it. Applies to hitters and
#     pitchers alike.
#   - A pitcher's OWN team multiplier never applies to him (a start is
#     a mostly-independent event from his own team's hitting day), but
#     the OPPOSING team's multiplier does, with the opposite sign: a
#     hot day for the lineup he's facing pulls him toward the worse end
#     of his own history, and vice versa -- the real DFS truth that a
#     big home run is the same at-bat scored two opposite ways for the
#     pitcher who allowed it.
#
# Every value here (multiplied to a real historical outcome the player
# actually achieved, never a fabricated point total) is still just
# "which of his own real games gets picked this trial," biased by three
# additive, independently-tunable pulls -- same "clearly-labeled
# approximation" philosophy as everywhere else in this module.

TEAM_MULTIPLIER_MEAN = 1.0
TEAM_MULTIPLIER_STD = 0.28
TEAM_MULTIPLIER_MIN = 0.25
TEAM_MULTIPLIER_MAX = 2.25

# BRING-BACK CORRELATION: two teams playing EACH OTHER in the same game
# don't have independent days -- a real shootout (weather, park, a soft
# pitching matchup on both sides) tends to lift both offenses together,
# which is exactly why rostering 1-2 hitters from the opposing team
# ("bring-back") alongside a main stack is a real GPP construction
# technique. `simulate_batch()` models this as a shared per-trial game
# factor blended with each team's own independent residual, weighted so
# the MARGINAL distribution of any single team's multiplier (mean, STD,
# clamped bounds) is exactly unchanged from the uncorrelated case --
# only the correlation BETWEEN two same-game teams' multipliers moves,
# and only when both sides of that game actually appear in the same
# simulate_batch() call. Approximate and clearly labeled, like every
# other constant in this module -- a real bring-back correlation is
# genuine but partial (each team's day still depends heavily on its own,
# team-specific pitching matchup), nowhere near the near-total
# correlation within a single team's own stack.
GAME_CORRELATION = 0.40

# ---------------------------------------------------------------------------
# Correlation strengths -- Gaussian copula, calibrated against MEASURED
# reality rather than left at the strengths the machinery shipped with
# (the same at-birth calibration NFL's variance model got and MLB's
# never did). Each constant IS the target rank-level correlation, not
# an abstract sensitivity; measured outcome-level (Pearson) correlation
# comes out slightly lower on skewed pools. Targets and provenance:
#
#   same-team hitter-hitter DK-point correlation: +0.10
#     (measured directly from real 2026 game logs -- 294 teammate
#      pairs across 6 real rosters, mean +0.097, median +0.090; the
#      open-source chanzer0/MLB-DFS-Tools fitted batting-order
#      correlation matrix agrees at +0.12..0.20)
#   hitter vs OPPOSING starter: about -0.28
#     (that sim's fitted matrix, -0.26..-0.31 -- structural: the same
#      at-bat is scored oppositely on the two sides)
#   hitter vs opposing HITTERS: small positive, ~+0.05
#     (shared park/weather/game environment; falls out of
#      GAME_CORRELATION x MATE_CORRELATION, no constant of its own)
#
# The shipped strengths produced teammate correlation of +0.50 -- FIVE
# TIMES reality. That overstated every stack's variance in both
# directions, so the simulator systematically over-rated max-stack
# lineups' top-1% rates and ROI relative to how often real stacks
# actually spike -- and the lineups it ranked highest carried far more
# correlation risk than the sim believed.
MATE_CORRELATION = 0.12
# Looks high next to MATE_CORRELATION, and should: under the shared
# one-factor structure, the hitter side only loads sqrt(0.12) on the
# team's day, so reaching the fitted -0.28 hitter-vs-opposing-starter
# correlation needs the pitcher side to carry sqrt(0.12 * 0.70) ~ 0.29
# of shared weight -- and mechanically a starter's DK score IS close to
# an inverse function of what the opposing offense does against him.
OPP_PITCHER_CORRELATION = 0.70

def team_environment_multiplier(rng: random.Random) -> float:
    """One team's overall day for one Monte Carlo trial -- sample once
    per team per trial, not per player, and reuse the same value for
    every hitter on that team in that trial."""
    m = rng.gauss(TEAM_MULTIPLIER_MEAN, TEAM_MULTIPLIER_STD)
    return max(TEAM_MULTIPLIER_MIN, min(TEAM_MULTIPLIER_MAX, m))


def _norm_cdf(x: "np.ndarray") -> "np.ndarray":
    """
    Standard normal CDF, vectorized, without a scipy dependency --
    Abramowitz & Stegun 7.1.26 (max abs error ~1.5e-7, far below the
    1/POOL_SIZE index resolution it feeds).
    """
    sign = np.sign(x)
    ax = np.abs(x) / math.sqrt(2.0)
    t = 1.0 / (1.0 + 0.3275911 * ax)
    poly = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 + t * (-1.453152027 + t * 1.061405429))))
    erf = 1.0 - poly * np.exp(-ax * ax)
    return 0.5 * (1.0 + sign * erf)


def _copula_index(rho: float, z_shared: float, z_own: float, n: int) -> int:
    """One correlated pool index: Gaussian copula with weight sqrt(rho)
    on the shared factor. The uniform comes out EXACTLY uniform, so the
    player's own empirical distribution is reproduced marginally no
    matter the correlation strength -- the property the old
    percentile-target-plus-jitter sampler lost (it center-biased every
    marginal once the shared shift was calibrated down to reality)."""
    z = math.sqrt(rho) * z_shared + math.sqrt(1.0 - rho) * z_own
    u = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    return min(n - 1, int(u * n))


def sample_correlated_outcome(
    sorted_pool: list[float],
    rng: random.Random,
    *,
    team_multiplier: float = 1.0,
    own_edge: float | None = None,
    opponent_multiplier: float | None = None,
) -> float:
    """
    One player's simulated outcome for a trial, correlated to the
    shared team/game environment through a Gaussian copula -- the same
    construction the open-source reference sim uses (multivariate
    normal -> CDF -> quantile), with this player's own empirical
    bootstrap pool as the quantile function instead of a fitted gamma.

    `team_multiplier` (hitters) or `opponent_multiplier` (pitchers,
    opposite sign -- the team he's FACING having a big day pulls him
    down) supplies the shared factor, converted back to the standard
    normal it was drawn from. `own_edge` is accepted for backward
    compatibility and IGNORED: pools are recentered on today's
    projection (see recenter_pool), and the projection already embeds
    the matchup -- shifting the draw by the edge composite as well
    double-counted it, once in the level and once in the percentile.

    `sorted_pool` must already be sorted ascending -- sort once, reuse
    across trials.
    """
    n = len(sorted_pool)
    if n <= 1:
        return sorted_pool[0] if sorted_pool else 0.0
    if opponent_multiplier is not None:
        rho = OPP_PITCHER_CORRELATION
        z_shared = -(opponent_multiplier - TEAM_MULTIPLIER_MEAN) / TEAM_MULTIPLIER_STD
    else:
        rho = MATE_CORRELATION
        z_shared = (team_multiplier - TEAM_MULTIPLIER_MEAN) / TEAM_MULTIPLIER_STD
    return sorted_pool[_copula_index(rho, z_shared, rng.gauss(0.0, 1.0), n)]



async def player_pools_for_entries(
    lineups: list[dict[str, Any]], season: int
) -> dict[int, list[float]]:
    """
    Fetch (and cache-populate) player_outcome_pool() for every unique
    player id appearing anywhere across a batch of lineups/entries --
    builds the `player_pools` dict simulate_batch() needs. Position is
    read off each player's own roster slot in the lineup (via
    lineup_export.SLOT_LABELS, stripped of its numbering -- "OF2" ->
    "OF") rather than looked up separately, since that's the same
    "assigned slot" every other part of this app already uses for a
    multi-eligible player.
    """
    positions: dict[int, str] = {}
    projections: dict[int, float] = {}
    for lineup in lineups:
        for label, p in zip(SLOT_LABELS, players_in_slot_order(lineup)):
            positions.setdefault(p["id"], label.rstrip("0123456789"))
            proj = p.get("projected_fpts")
            if proj and p["id"] not in projections:
                projections[p["id"]] = float(proj)

    ids = list(positions)
    pools = await asyncio.gather(*(player_outcome_pool(pid, positions[pid], season) for pid in ids))
    return {
        pid: recenter_pool(pool, projections.get(pid))
        for pid, pool in zip(ids, pools)
    }


# How far a pool is allowed to be rescaled to meet today's projection.
# A confirmed starter projected well above a pool diluted by pinch-hit
# and rest days needs up to ~1.7x (measured: Cal Raleigh, projection
# 11.2 over a 6.5-mean pool); the clamp exists so a degenerate
# projection or a thin shared-pool blend can't stretch a distribution
# into nonsense.
_RECENTER_SCALE_MIN = 0.4
_RECENTER_SCALE_MAX = 2.5
# Below this pool mean there is nothing meaningful to rescale (an
# all-zero rookie pool, a degenerate fallback) -- multiplying zeros by
# any scale still can't reach a projection, so leave it alone.
_RECENTER_MIN_POOL_MEAN = 1.0


def recenter_pool(pool: list[float], projection: float | None) -> list[float]:
    """
    Rescale a player's outcome pool so its MEAN equals today's
    projection, preserving its empirical shape (skew, zeros, fat right
    tail scale with it).

    This is the single biggest architectural correction from comparing
    this simulator against the industry: every reference sim examined
    (SaberSim's play-by-play engine conceptually, and the open-source
    chanzer0/MLB-DFS-Tools implementation explicitly -- hitters ~
    Gamma(mean=projection, sd=0.5x projection), pitchers ~
    Normal(mean=projection, sd=0.3x projection)) centers each player's
    outcome distribution on TODAY'S projection and uses history only
    for variance/shape. This sim centered on the player's raw
    historical pool instead, which embeds two real biases, both
    measured on a live slate (158 players):

      - Levels lagged today's information: pool mean vs projection had
        only 0.83 correlation, was off by >25% for 23% of players, and
        averaged -9% low -- pools include pinch-hit and rest-day games,
        while today's projection knows the player is starting.
      - The builder and the field are both driven by projections, so
        grading them on historical levels ranked lineups substantially
        by "whose history disagrees with the projection" rather than by
        structure/leverage -- entering the sim's favorites into real
        contests then underperforms exactly the way stale information
        does.

    Multiplicative rather than additive so the shape scales with the
    level (a hitter's zero games stay zeros; scoring variance grows
    with scoring mean), matching how the reference sims' gamma
    parameterization behaves.
    """
    if not pool or not projection or projection <= 0:
        return pool
    mean = sum(pool) / len(pool)
    if mean < _RECENTER_MIN_POOL_MEAN:
        return pool
    scale = min(_RECENTER_SCALE_MAX, max(_RECENTER_SCALE_MIN, projection / mean))
    return [x * scale for x in pool]


# --------------------------------------------------------------------------
# Monte Carlo simulation engine (Phase 4)
# --------------------------------------------------------------------------
#
# Vectorized with numpy: every trial for every player is one array
# element, not one Python-level draw, since Phase 5's contest-generator
# batches can run thousands of lineups x thousands of trials. Accepts
# both lineup shapes already in this codebase (optimizer.py's
# `slots`-grouped lineups and contest.py's flat `players` entries) via
# lineup_export.py's players_in_slot_order() -- the same normalization
# its CSV export already relies on -- so callers don't need to know or
# care which generator produced a given entry.

# The first PITCHER_COUNT players in DK roster order (per
# players_in_slot_order()) are always the two pitcher slots, since "P"
# is the first key in optimizer.py's SLOT_REQUIREMENTS.
PITCHER_COUNT = SLOT_REQUIREMENTS["P"]


# How wrong our own projection of a player's TRUE TALENT might be, as a
# fraction of it -- drawn per player per trial and applied to entries
# and the opponent field alike, so the simulator stops treating its own
# inputs as gospel (without it, entries built by maximizing the same
# numbers the sim is nudged by are by construction the lineups the sim
# believes in most).
#
# DELIBERATELY 0.0 FOR NOW -- implemented, measured, and gated off,
# because the current grading scheme can't absorb it honestly. An
# entry is ranked per-trial against a SAMPLED field and read off a
# fixed payout curve; the field's own payouts are never re-graded under
# the same draws, so the scheme is not zero-sum -- symmetric extra
# variance mints new rank-1 probability for a top lineup without taking
# it from anyone, and a top-heavy curve converts that into free EV.
# Measured directly on the rake sanity fixture: a zero-skill chalk
# lineup went from -33% ROI to +60..+78% at sigma 0.10-0.20 (both
# additive-on-mean and multiplicative forms), which is exactly the
# implausible-ROI failure mode this app's own regression test guards.
# Turning this on requires grading entries and field JOINTLY per trial
# (real zero-sum ranks over the union) -- the review's own SS7.4-style
# refactor -- and that has to come first. ~0.20 is the reviewer's
# suggested scale once it can be used.
PROJECTION_ERROR_STD = 0.0
# One draw is clipped at +/-2.5 sigma so a single extreme draw can't
# hand a player an absurd talent level (or a negative one).
_PROJECTION_ERROR_CLIP = 2.5


def apply_projection_error(
    outcomes: "np.ndarray", rng: "np.random.Generator"
) -> "np.ndarray":
    """
    Shift a (players x trials) outcome matrix by per-player, per-trial
    projection-error draws, ADDITIVE on each player's own mean level --
    outcome + eps * mean, eps ~ N(0, PROJECTION_ERROR_STD).

    Additive-on-the-mean rather than multiplicative-on-the-outcome, and
    the distinction genuinely matters: projection error is uncertainty
    about a player's TRUE TALENT (the center of his distribution), not
    about how big his big games are. A multiplicative version was tried
    first and rewrote the right tail -- a 40-point outcome drawn at
    +1.5x became 60 -- which in a top-heavy payout inflated every
    chalk lineup's EV so hard it tripped the rake sanity check
    (+69.9% ROI for a zero-skill chalk lineup). Shifting the center
    leaves the shape of game-to-game variance where the real pools put
    it. Draws are shared across every lineup in the batch (entries and
    field alike): within one simulated reality a player has ONE true
    talent, whoever rostered him. Results are floored at zero, since
    DK Classic MLB hitter scoring has no negative outcomes.
    """
    if PROJECTION_ERROR_STD <= 0:
        return outcomes
    means = outcomes.mean(axis=1, keepdims=True)
    error = np.clip(
        rng.normal(0.0, PROJECTION_ERROR_STD, size=outcomes.shape),
        -_PROJECTION_ERROR_CLIP * PROJECTION_ERROR_STD,
        _PROJECTION_ERROR_CLIP * PROJECTION_ERROR_STD,
    )
    return np.maximum(outcomes + error * means, 0.0)


def simulate_batch(
    entries: list[dict[str, Any]],
    player_pools: dict[int, list[float]],
    *,
    num_trials: int,
    seed: int | None = None,
) -> np.ndarray:
    """
    Simulate a batch of lineups/entries together across `num_trials`
    Monte Carlo trials. Returns a `(len(entries), num_trials)` array of
    simulated DK-point totals.

    Every *unique* player across the whole batch is sampled once per
    trial, not once per lineup containing them -- two lineups sharing 8
    of their 10 players see correlated results between them in the same
    simulated "reality", for free, rather than each lineup being
    simulated as if it existed in isolation.

    Three signals bias which percentile of a player's own outcome pool
    gets sampled each trial (see the module-level note above
    team_environment_multiplier() for the full rationale): hitters on
    the same team share that trial's team multiplier; every player
    (hitter or pitcher) gets a further pull from their own
    `edge_composite` (today's actual matchup quality, if known); and a
    pitcher additionally gets pulled the OPPOSITE way by the team he's
    facing having a big or small day -- pitchers never share their own
    team's multiplier (a start is a mostly-independent event from the
    team's own hitting day), only react to their opponent's.

    Two DIFFERENT teams playing each other in the same game also aren't
    fully independent of one another -- see GAME_CORRELATION above --
    whenever both sides of that matchup happen to appear somewhere in
    this same batch (a stack plus a bring-back play, or just two
    entries in the same field that happen to roster opposite sides of
    one game). A team with no known opponent in this batch, or whose
    real opponent doesn't itself appear anywhere in `entries`, gets an
    independent day exactly as before this existed.

    `player_pools` must have an entry (from `player_outcome_pool()`) for
    every player id appearing anywhere in `entries`.
    """
    flattened = [players_in_slot_order(entry) for entry in entries]

    unique_players: dict[int, dict[str, Any]] = {}
    for players in flattened:
        for slot_index, p in enumerate(players):
            unique_players.setdefault(
                p["id"],
                {
                    "team": p.get("team"),
                    "opponent": p.get("opponent"),
                    "is_pitcher": slot_index < PITCHER_COUNT,
                    "edge_composite": p.get("edge_composite"),
                },
            )

    missing = sorted(pid for pid in unique_players if pid not in player_pools)
    if missing:
        preview = missing[:5]
        suffix = "..." if len(missing) > 5 else ""
        raise ValueError(f"simulate_batch: no outcome pool for player id(s) {preview}{suffix}")

    rng = np.random.default_rng(seed)

    # Every team that's either a hitter's own team or a pitcher's
    # opponent needs its own multiplier series -- a pitcher can react
    # to a team's day even if none of that team's hitters are anywhere
    # in this batch.
    relevant_teams = sorted(
        {info["team"] for info in unique_players.values() if not info["is_pitcher"] and info.get("team")}
        | {info["opponent"] for info in unique_players.values() if info["is_pitcher"] and info.get("opponent")}
    )
    # Which team each team is actually facing this batch, from either
    # side's own (team, opponent) pair -- whichever players happen to
    # carry it. Only used to find genuine same-game pairs among
    # `relevant_teams`; a team with no known opponent (or whose opponent
    # isn't itself in this batch) just gets an independent day, exactly
    # as before this feature existed.
    team_to_opponent: dict[str, str] = {}
    for info in unique_players.values():
        team, opponent = info.get("team"), info.get("opponent")
        if team and opponent:
            team_to_opponent[team] = opponent

    game_factors: dict[frozenset, np.ndarray] = {}

    def _shared_game_factor(a: str, b: str) -> np.ndarray:
        key = frozenset((a, b))
        if key not in game_factors:
            game_factors[key] = rng.normal(0.0, TEAM_MULTIPLIER_STD, size=num_trials)
        return game_factors[key]

    game_weight = math.sqrt(GAME_CORRELATION)
    indep_weight = math.sqrt(1.0 - GAME_CORRELATION)

    team_multipliers = {}
    for team in relevant_teams:
        opponent = team_to_opponent.get(team)
        independent = rng.normal(0.0, TEAM_MULTIPLIER_STD, size=num_trials)
        if opponent and opponent in relevant_teams and team_to_opponent.get(opponent) == team:
            residual = game_weight * _shared_game_factor(team, opponent) + indep_weight * independent
        else:
            residual = independent
        team_multipliers[team] = np.clip(
            TEAM_MULTIPLIER_MEAN + residual, TEAM_MULTIPLIER_MIN, TEAM_MULTIPLIER_MAX
        )

    player_ids = list(unique_players)
    player_index = {pid: i for i, pid in enumerate(player_ids)}
    outcomes = np.zeros((len(player_ids), num_trials))

    for pid, info in unique_players.items():
        pool = np.array(sorted(player_pools[pid]), dtype=float)
        n = len(pool)
        if n == 0:
            continue
        i = player_index[pid]

        # Gaussian copula (see sample_correlated_outcome): the shared
        # team/game factor gets weight sqrt(rho), the player's own
        # independent draw sqrt(1-rho), and the resulting uniform is
        # exactly uniform -- his empirical pool is reproduced marginally
        # regardless of correlation strength. A player with no shared
        # factor at all (no team known; a pitcher whose opponent isn't
        # in this batch) is just an independent uniform draw from his
        # own pool. edge_composite is deliberately unused here: pools
        # are recentered on today's projection, which already embeds
        # the matchup.
        if info["is_pitcher"]:
            opponent_multiplier = team_multipliers.get(info.get("opponent"))
            if opponent_multiplier is not None:
                rho = OPP_PITCHER_CORRELATION
                z_shared = -(opponent_multiplier - TEAM_MULTIPLIER_MEAN) / TEAM_MULTIPLIER_STD
            else:
                rho, z_shared = 0.0, 0.0
        elif info.get("team"):
            rho = MATE_CORRELATION
            z_shared = (team_multipliers[info["team"]] - TEAM_MULTIPLIER_MEAN) / TEAM_MULTIPLIER_STD
        else:
            rho, z_shared = 0.0, 0.0

        if rho <= 0.0:
            idx = rng.integers(0, n, size=num_trials)
        else:
            z = math.sqrt(rho) * z_shared + math.sqrt(1.0 - rho) * rng.standard_normal(num_trials)
            u = _norm_cdf(z)
            idx = np.minimum((u * n).astype(int), n - 1)
        outcomes[i] = pool[idx]

    outcomes = apply_projection_error(outcomes, rng)

    lineup_indices = np.array(
        [[player_index[p["id"]] for p in players] for players in flattened]
    )
    return outcomes[lineup_indices].sum(axis=1)
