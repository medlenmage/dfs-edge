"""
Per-player (and per-team, for DST) DK-fantasy-point outcome
distributions for NFL, built from real 2025 game logs
(clients/nfl.get_player_game_log() / get_team_game_log() +
nfl_dk_points.py) -- the NFL sibling of services/variance.py (MLB).
See that module's own docstring for the full "why a bootstrap pool,
not a parametric shape" rationale, which applies unchanged here.

SCOPED DELIBERATELY LEANER THAN MLB's VERSION -- STATED PLAINLY
------------------------------------------------------------------
- No own-matchup-quality signal (MLB's `own_edge`, from scoring.py's
  composite) -- not wired in for this first pass. Every player samples
  from their own real history, correlated only through their team's
  (or, for DST, their opponent's) shared day.
- No cross-game "bring-back" correlation (MLB's later addition on top
  of an already-shipped team-correlation phase) -- not built here.
- Team correlation applies at position-specific sensitivities, not one
  flat multiplier: a QB and his own team's pass-catchers (WR/TE) share
  the team's day at full strength -- the real DFS "stack" mechanic
  this exists to model -- a team's RBs get a reduced pull (game script
  cuts both ways: protecting a lead can mean MORE rushing, or a
  blowout can mean less, as garbage time favors the pass), and a DST
  reacts to the OPPOSING team's day with the opposite sign (a quiet
  day for the offense it's facing is a good day for it), mirroring
  MLB's own pitcher/opponent anti-correlation.

WHY 2025-ONLY, FOR NOW
------------------------
Scoped to a single season's real game logs per an explicit request
("use data from 2025 game logs for the moment") -- multi-season
pooling (mixing rosters and scheme changes across years) is a real
design question, deliberately left for later rather than decided by
default here.

THIN SAMPLES
------------
An NFL season is at most 17 real games -- a much lower ceiling than
MLB's ~150 (hitters) or ~30 (pitchers) -- so the same-position shared
pool blend (see variance.py's own note on this) is proportionally more
load-bearing here. MIN_GAMES_FULL_TRUST is set lower than MLB's own
values to reflect that a full, healthy NFL season is already "thin" by
MLB's standard.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

import numpy as np

from app import cache
from app.clients import nfl
from app.config import get_settings
from app.services import nfl_dk_points

OFFENSIVE_POSITIONS = {"QB", "RB", "WR", "TE"}

# Games before a player's/team's own history is trusted on its own.
# Lower than MLB's MIN_GAMES_FULL_TRUST across the board -- a 17-game
# season has no equivalent of a hitter's ~140+ games to lean on. DST
# gets the highest threshold of the group since a team's defense
# "plays" every single game (no bench/rotation concept the way an
# offensive skill player has), so its own sample is the least likely
# to be thin for reasons unrelated to real ability.
MIN_GAMES_FULL_TRUST = {"QB": 12, "RB": 10, "WR": 10, "TE": 10, "DST": 14}

# Size of the returned resampling pool -- same rationale as variance.py.
POOL_SIZE = 200

# How many of the most recently contributed games a shared same-position
# (or league-wide DST) pool keeps -- bounds its growth across a
# long-running server process.
MAX_POSITION_POOL_SIZE = 2000


def own_games(game_log: list[dict[str, Any]]) -> list[float]:
    """
    DK points for every game a player actually appeared in. A row with
    zero passing attempts, carries, AND targets is a non-appearance
    (inactive, or a bye week that slipped through), not a real
    outcome -- excluded the same defensive way variance.py excludes a
    zero-PA/zero-out MLB row.
    """
    return [
        nfl_dk_points.game_points(g)
        for g in game_log
        if (g.get("attempts", 0.0) + g.get("carries", 0.0) + g.get("targets", 0.0)) > 0
    ]


def _position_pool_key(position: str, season: int) -> str:
    return f"nfl_variance:position_pool:{season}:{position}"


def contribute_to_position_pool(position: str, season: int, games: list[float]) -> None:
    """Read-modify-write into the shared same-position pool -- same
    pattern variance.py already uses for MLB. Fine without locking:
    this is a single-user local app, not a service with real write
    concurrency."""
    if not games:
        return
    key = _position_pool_key(position, season)
    existing = cache.get(key) or []
    combined = (existing + games)[-MAX_POSITION_POOL_SIZE:]
    cache.put(key, combined, get_settings().ttl_game_logs)


def position_pool(position: str, season: int) -> list[float]:
    return cache.get(_position_pool_key(position, season)) or []


async def player_outcome_pool(
    player_id: str, position: str, season: int, *, seed: int | None = None
) -> list[float]:
    """
    A POOL_SIZE-length bootstrap resampling pool of DK-point outcomes
    for one offensive player (QB/RB/WR/TE) -- draw from it uniformly
    (with replacement) to simulate one game's result for them. Blends
    in the shared same-position pool for thin samples; a player with a
    full season's worth of games draws almost entirely from his own
    real history. See dst_outcome_pool() for the DST equivalent.
    """
    settings = get_settings()
    pos = (position or "").strip().upper()
    if pos not in OFFENSIVE_POSITIONS:
        # An unrecognized offensive position (rare -- e.g. a fullback
        # tagged "FB") falls back to the broadest skill-position pool
        # rather than refusing to simulate at all.
        pos = "WR"

    async def _load() -> list[float]:
        game_log = await nfl.get_player_game_log(player_id, season)
        own = own_games(game_log)
        contribute_to_position_pool(pos, season, own)

        if not own:
            pool = position_pool(pos, season)
            return pool[:POOL_SIZE] if pool else [0.0]

        full_trust = MIN_GAMES_FULL_TRUST[pos]
        trust = min(1.0, len(own) / full_trust)
        shared_pool = position_pool(pos, season) or own

        rng = random.Random(seed)
        return [
            rng.choice(own) if rng.random() < trust else rng.choice(shared_pool)
            for _ in range(POOL_SIZE)
        ]

    cache_key = f"nfl_variance:pool:{player_id}:{season}:{pos}"
    return await cache.cached(cache_key, settings.ttl_game_logs, _load)


def _dst_pool_key(season: int) -> str:
    return f"nfl_variance:dst_pool:{season}"


def contribute_to_dst_pool(season: int, games: list[float]) -> None:
    if not games:
        return
    key = _dst_pool_key(season)
    existing = cache.get(key) or []
    combined = (existing + games)[-MAX_POSITION_POOL_SIZE:]
    cache.put(key, combined, get_settings().ttl_game_logs)


def dst_pool(season: int) -> list[float]:
    return cache.get(_dst_pool_key(season)) or []


async def dst_outcome_pool(team: str, season: int, *, seed: int | None = None) -> list[float]:
    """
    Same bootstrap-pool idea as player_outcome_pool(), for one team's
    DST -- blends in a shared, league-wide DST pool (every team's own
    real games, accumulated the same organic way as the offensive
    position pools) for a team with a thin sample of its own so far.
    """
    settings = get_settings()

    async def _load() -> list[float]:
        game_log = await nfl.get_team_game_log(team, season)
        own = [nfl_dk_points.dst_game_points(g) for g in game_log]
        contribute_to_dst_pool(season, own)

        if not own:
            pool = dst_pool(season)
            return pool[:POOL_SIZE] if pool else [0.0]

        trust = min(1.0, len(own) / MIN_GAMES_FULL_TRUST["DST"])
        shared_pool = dst_pool(season) or own

        rng = random.Random(seed)
        return [
            rng.choice(own) if rng.random() < trust else rng.choice(shared_pool)
            for _ in range(POOL_SIZE)
        ]

    cache_key = f"nfl_variance:dst_pool_single:{team}:{season}"
    return await cache.cached(cache_key, settings.ttl_game_logs, _load)


def ceiling_from_pool(pool: list[float], percentile: float = 0.9) -> float:
    """The `percentile`-th percentile of a player's/team's own outcome
    pool -- a real, data-driven ceiling, reusing the exact same
    bootstrap pool the Monte Carlo simulator draws from."""
    if not pool:
        return 0.0
    ordered = sorted(pool)
    idx = round(percentile * (len(ordered) - 1))
    return ordered[idx]


async def player_pools_for_entries(
    entries: list[dict[str, Any]], season: int
) -> dict[str, list[float]]:
    """
    Fetch (and cache-populate) an outcome pool for every unique player
    id across a batch of generated lineups (nfl_optimizer.py's own
    `{"slots": {...}}` shape) -- builds the `player_pools` dict
    simulate_batch() needs. Position/team are read directly off each
    player's own lineup-slot entry (nfl_optimizer.py's _solve_one()
    carries both), not looked up separately.
    """
    positions: dict[str, str] = {}
    teams: dict[str, str] = {}
    for entry in entries:
        for p in _flatten_lineup(entry):
            positions.setdefault(p["id"], p["position"])
            teams.setdefault(p["id"], p["team"])

    async def _pool_for(pid: str) -> list[float]:
        if positions[pid] == "DST":
            return await dst_outcome_pool(teams[pid], season)
        return await player_outcome_pool(pid, positions[pid], season)

    ids = list(positions)
    pools = await asyncio.gather(*(_pool_for(pid) for pid in ids))
    return dict(zip(ids, pools))


# --------------------------------------------------------------------------
# Monte Carlo simulation engine
# --------------------------------------------------------------------------
#
# Vectorized with numpy, same rationale as variance.py's own engine:
# every trial for every player is one array element, not one
# Python-level draw.

TEAM_MULTIPLIER_MEAN = 1.0
TEAM_MULTIPLIER_STD = 0.30
TEAM_MULTIPLIER_MIN = 0.25
TEAM_MULTIPLIER_MAX = 2.25

# How strongly each position's outcome reacts to its own team's shared
# day (1.0 = full pull, same strength as the QB/pass-catcher stack this
# whole mechanic exists to model). RB is damped -- see the module
# docstring's note on why game script cuts both ways for the run game.
TEAM_SENSITIVITY = {"QB": 1.0, "WR": 1.0, "TE": 1.0, "RB": 0.5}

# How strongly a DST reacts to its OPPONENT's shared day, applied with
# the opposite sign (a big day for the offense it's facing pulls a DST
# toward the worse end of its own history, and vice versa).
DST_OPPONENT_SENSITIVITY = 0.7

# Stdev of random jitter around the target percentile, as a fraction of
# the pool length -- keeps real game-to-game randomness even on a good
# or bad team day.
JITTER_FRACTION = 0.22


def team_environment_multiplier(rng: random.Random) -> float:
    """One team's overall day for one Monte Carlo trial -- sample once
    per team per trial, reused for every one of that team's players in
    that trial."""
    m = rng.gauss(TEAM_MULTIPLIER_MEAN, TEAM_MULTIPLIER_STD)
    return max(TEAM_MULTIPLIER_MIN, min(TEAM_MULTIPLIER_MAX, m))


def _flatten_lineup(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Accepts both lineup shapes this codebase produces -- nfl_optimizer.py's
    `{"slots": {"QB": [...], "RB": [...], ...}}` and nfl_contest.py's flat
    `{"players": [...]}` -- so callers don't need to know or care which
    generator produced a given entry (same duality MLB's
    lineup_export.players_in_slot_order() already handles between
    optimizer.py and contest.py). Every player carries their OWN real
    position (not just which slot they filled -- FLEX doesn't say
    whether it's an RB/WR/TE).
    """
    if "players" in entry:
        return [
            {
                "id": p["id"],
                "team": p.get("team"),
                "opponent": p.get("opponent"),
                "position": p.get("position"),
            }
            for p in entry["players"]
        ]
    out: list[dict[str, Any]] = []
    for slot, players in (entry.get("slots") or {}).items():
        for p in players:
            out.append(
                {
                    "id": p["id"],
                    "team": p.get("team"),
                    "opponent": p.get("opponent"),
                    "position": p.get("position") or slot,
                }
            )
    return out


def simulate_batch(
    entries: list[dict[str, Any]],
    player_pools: dict[str, list[float]],
    *,
    num_trials: int,
    seed: int | None = None,
) -> np.ndarray:
    """
    Simulate a batch of lineups together across `num_trials` Monte
    Carlo trials. Returns a `(len(entries), num_trials)` array of
    simulated DK-point totals.

    Every *unique* player across the whole batch is sampled once per
    trial, not once per lineup containing them -- two lineups sharing
    most of their 9 players see correlated results between them in the
    same simulated "reality," for free.

    `player_pools` must have an entry (from player_outcome_pool() /
    dst_outcome_pool()) for every player id appearing anywhere in
    `entries`.
    """
    flattened = [_flatten_lineup(entry) for entry in entries]

    unique_players: dict[str, dict[str, Any]] = {}
    for players in flattened:
        for p in players:
            unique_players.setdefault(p["id"], p)

    missing = sorted(pid for pid in unique_players if pid not in player_pools)
    if missing:
        preview = missing[:5]
        suffix = "..." if len(missing) > 5 else ""
        raise ValueError(f"simulate_batch: no outcome pool for player id(s) {preview}{suffix}")

    rng = np.random.default_rng(seed)

    relevant_teams = sorted(
        {info["team"] for info in unique_players.values() if info.get("team")}
        | {info["opponent"] for info in unique_players.values() if info.get("opponent")}
    )
    team_multipliers = {
        team: np.clip(
            rng.normal(TEAM_MULTIPLIER_MEAN, TEAM_MULTIPLIER_STD, size=num_trials),
            TEAM_MULTIPLIER_MIN,
            TEAM_MULTIPLIER_MAX,
        )
        for team in relevant_teams
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
        position = info["position"]

        if position == "DST":
            mult = team_multipliers.get(info.get("opponent"))
            sensitivity = -DST_OPPONENT_SENSITIVITY
        else:
            mult = team_multipliers.get(info.get("team"))
            sensitivity = TEAM_SENSITIVITY.get(position, 0.5)

        if mult is None:
            idx = rng.integers(0, n, size=num_trials)
        else:
            delta = (mult - 1.0) * sensitivity
            target_pct = np.clip(0.5 + delta, 0.0, 1.0)
            target_idx = target_pct * (n - 1)
            jitter = rng.normal(0.0, JITTER_FRACTION * n, size=num_trials)
            idx = np.clip(np.round(target_idx + jitter).astype(int), 0, n - 1)
        outcomes[i] = pool[idx]

    lineup_indices = np.array(
        [[player_index[p["id"]] for p in players] for players in flattened]
    )
    return outcomes[lineup_indices].sum(axis=1)
