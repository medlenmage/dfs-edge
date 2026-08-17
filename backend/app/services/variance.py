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

import random
from typing import Any

from app import cache
from app.clients import mlb
from app.config import get_settings
from app.services import mlb_dk_points

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


def _player_kind(position: str) -> str:
    return "pitcher" if position == PITCHER_SLOT else "hitter"


def _own_games(game_log: list[dict[str, Any]], kind: str) -> list[float]:
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


def _contribute_to_position_pool(position: str, season: int, games: list[float]) -> None:
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


def _position_pool(position: str, season: int) -> list[float]:
    return cache.get(_position_pool_key(position, season)) or []


async def player_outcome_pool(
    player_id: int, position: str, season: int, *, seed: int | None = None
) -> list[float]:
    """
    A POOL_SIZE-length bootstrap resampling pool of DK-point outcomes
    for one player -- draw from it uniformly (with replacement) to
    simulate one game's result for them. Blends in the shared
    same-position pool for thin samples; a player with a full season's
    worth of games draws almost entirely from his own real history.
    """
    settings = get_settings()

    async def _load() -> list[float]:
        kind = _player_kind(position)
        group = "pitching" if kind == "pitcher" else "hitting"
        game_log = await mlb.get_player_game_log(player_id, season, group=group)
        own = _own_games(game_log, kind)
        _contribute_to_position_pool(position, season, own)

        if not own:
            # No games of his own yet this season (hasn't debuted, or
            # a rookie with zero PA/IP so far) -- nothing but the
            # shared pool to draw from.
            pool = _position_pool(position, season)
            return pool[:POOL_SIZE] if pool else [0.0]

        full_trust = MIN_GAMES_FULL_TRUST[kind]
        trust = min(1.0, len(own) / full_trust)
        # Fall back to his own games if the shared pool hasn't warmed
        # up yet -- better than blending in nothing at all.
        position_pool = _position_pool(position, season) or own

        rng = random.Random(seed)
        return [
            rng.choice(own) if rng.random() < trust else rng.choice(position_pool)
            for _ in range(POOL_SIZE)
        ]

    return await cache.cached(
        f"variance:pool:{player_id}:{season}:{position}", settings.ttl_game_logs, _load
    )


# --------------------------------------------------------------------------
# Team correlation (Phase 3)
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
# teams and trials. Pitchers aren't correlated to their own offense
# this way; a start is a mostly-independent event from the team's own
# hitting day, so Phase 4's simulator should sample a pitcher's outcome
# uniformly from his own pool instead of through these functions.

TEAM_MULTIPLIER_MEAN = 1.0
TEAM_MULTIPLIER_STD = 0.28
TEAM_MULTIPLIER_MIN = 0.25
TEAM_MULTIPLIER_MAX = 2.25

# How much a team multiplier away from 1.0 shifts which percentile of
# a hitter's own history dominates sampling that trial.
PERCENTILE_SENSITIVITY = 1.0

# Stdev of random jitter around that target percentile, as a fraction
# of the pool length -- keeps real day-to-day randomness even on a
# good or bad team day (nobody homers every time their team scores a
# lot, nobody goes hitless every time it doesn't).
JITTER_FRACTION = 0.22


def team_environment_multiplier(rng: random.Random) -> float:
    """One team's overall day for one Monte Carlo trial -- sample once
    per team per trial, not per player, and reuse the same value for
    every hitter on that team in that trial."""
    m = rng.gauss(TEAM_MULTIPLIER_MEAN, TEAM_MULTIPLIER_STD)
    return max(TEAM_MULTIPLIER_MIN, min(TEAM_MULTIPLIER_MAX, m))


def sample_correlated_outcome(
    sorted_pool: list[float], multiplier: float, rng: random.Random
) -> float:
    """
    One hitter's simulated outcome for a trial, biased toward the
    better (multiplier > 1) or worse (multiplier < 1) end of his own
    outcome pool.

    `sorted_pool` must already be sorted ascending -- sort a player's
    pool once and reuse it across every trial in a batch, rather than
    re-sorting on every single draw, since Phase 4 will call this many
    thousands of times per player.
    """
    n = len(sorted_pool)
    if n <= 1:
        return sorted_pool[0] if sorted_pool else 0.0
    target_pct = min(1.0, max(0.0, 0.5 + (multiplier - 1.0) * PERCENTILE_SENSITIVITY))
    target_idx = target_pct * (n - 1)
    idx = round(target_idx + rng.gauss(0, JITTER_FRACTION * n))
    return sorted_pool[min(n - 1, max(0, idx))]
