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
        kind = player_kind(position)
        group = "pitching" if kind == "pitcher" else "hitting"
        game_log = await mlb.get_player_game_log(player_id, season, group=group)
        own = own_games(game_log, kind)
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
    for lineup in lineups:
        for label, p in zip(SLOT_LABELS, players_in_slot_order(lineup)):
            positions.setdefault(p["id"], label.rstrip("0123456789"))

    ids = list(positions)
    pools = await asyncio.gather(*(player_outcome_pool(pid, positions[pid], season) for pid in ids))
    return dict(zip(ids, pools))


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
    simulated as if it existed in isolation. Hitters on the same team
    share that trial's `team_environment_multiplier()`; pitchers are
    sampled independently and uniformly from their own pool (a start is
    a mostly-independent event from the team's own hitting day -- see
    the Phase 3 note above).

    `player_pools` must have an entry (from `player_outcome_pool()`) for
    every player id appearing anywhere in `entries`.
    """
    flattened = [players_in_slot_order(entry) for entry in entries]

    unique_players: dict[int, dict[str, Any]] = {}
    for players in flattened:
        for slot_index, p in enumerate(players):
            unique_players.setdefault(
                p["id"], {"team": p.get("team"), "is_pitcher": slot_index < PITCHER_COUNT}
            )

    missing = sorted(pid for pid in unique_players if pid not in player_pools)
    if missing:
        preview = missing[:5]
        suffix = "..." if len(missing) > 5 else ""
        raise ValueError(f"simulate_batch: no outcome pool for player id(s) {preview}{suffix}")

    rng = np.random.default_rng(seed)

    hitter_teams = sorted(
        {info["team"] for info in unique_players.values() if not info["is_pitcher"] and info.get("team")}
    )
    team_multipliers = {
        team: np.clip(
            rng.normal(TEAM_MULTIPLIER_MEAN, TEAM_MULTIPLIER_STD, size=num_trials),
            TEAM_MULTIPLIER_MIN,
            TEAM_MULTIPLIER_MAX,
        )
        for team in hitter_teams
    }

    player_ids = list(unique_players)
    player_index = {pid: i for i, pid in enumerate(player_ids)}
    outcomes = np.zeros((len(player_ids), num_trials))

    for pid, info in unique_players.items():
        pool = np.array(sorted(player_pools[pid]), dtype=float)
        n = len(pool)
        if n == 0:
            continue
        i = player_index[pid]
        if info["is_pitcher"] or not info.get("team"):
            idx = rng.integers(0, n, size=num_trials)
        else:
            multiplier = team_multipliers[info["team"]]
            target_pct = np.clip(0.5 + (multiplier - 1.0) * PERCENTILE_SENSITIVITY, 0.0, 1.0)
            target_idx = target_pct * (n - 1)
            jitter = rng.normal(0.0, JITTER_FRACTION * n, size=num_trials)
            idx = np.clip(np.round(target_idx + jitter).astype(int), 0, n - 1)
        outcomes[i] = pool[idx]

    lineup_indices = np.array(
        [[player_index[p["id"]] for p in players] for players in flattened]
    )
    return outcomes[lineup_indices].sum(axis=1)
