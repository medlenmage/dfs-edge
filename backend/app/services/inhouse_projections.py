"""
In-house DK FPTS projection v1 (Phase 2 of the projections/history plan
-- see .claude/plans/clever-strolling-hearth.md for the full roadmap).

Every "Proj FPTS" in this app has come from an uploaded RotoWire CSV
until now -- there was no in-house model. This builds one from data
the app already fetches, reusing two pieces that already exist rather
than inventing new machinery:

  1. A real per-player baseline rate, built from clients/mlb's
     get_player_game_log() + mlb_dk_points.py (the exact DK-scoring
     formulas) -- season-to-date blended with recent form, shrunk
     toward the same shared same-position pool variance.py's Phase 2
     already accumulates for thin samples (a rookie call-up leans on
     it heavily; an everyday player barely touches it).
  2. scoring.py's `edge.composite` -- the matchup-quality multiplier
     already fusing platoon splits, Vegas implied team total, opposing
     pitcher/bullpen quality, Savant contact quality, park, weather,
     and recent form, already attached to every player on the slate.
     It isn't shaped like fantasy points (a 0-100 ranking score), but
     as a multiplier centered at 1.00 it's the natural lever to scale
     a baseline rate up or down for today's specific matchup, instead
     of re-deriving all of that signal from scratch.

    inhouse_fpts = baseline_dk_points(...) * edge["composite"]

Explicitly not using variance.py's player_outcome_pool() directly --
that returns a bootstrap resampling POOL for Monte Carlo simulation,
not a single point estimate a projection needs. This computes its own
blended rate from the same underlying game log instead.

Batting-order-driven PA volume (only known once lineups are confirmed,
typically 2-4h before lock) isn't modeled separately here -- before
that, a player's own season-average PA/game already has playing time
baked in, which is a real, stated precision limitation, not an
oversight.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.clients import mlb
from app.services import variance

# Recent form gets real, bounded weight -- a hot or cold last 15 games
# matters, but shouldn't swamp a full season's signal the way a naive
# last-N-only average would.
_RECENT_WEIGHT = 0.4
_RECENT_GAMES = 15


async def baseline_dk_points(player_id: int, position: str, season: int) -> float:
    """
    A single expected-DK-points-per-game rate for one player this
    season, blending season-to-date average with recent form and
    shrinking toward the shared same-position pool for thin samples --
    same shrink-toward-a-prior technique as scoring.py's `_shrink()`
    and variance.py's own thin-sample blend, just applied to a rate
    instead of a multiplier or a distribution.
    """
    kind = variance.player_kind(position)
    group = "pitching" if kind == "pitcher" else "hitting"
    game_log = await mlb.get_player_game_log(player_id, season, group=group)
    own = variance.own_games(game_log, kind)
    # Contribute to the same shared same-position pool variance.py's
    # own player_outcome_pool() warms up -- whichever module runs
    # first for a given position/season benefits both.
    variance.contribute_to_position_pool(position, season, own)
    if not own:
        return 0.0

    season_avg = sum(own) / len(own)
    recent = own[-_RECENT_GAMES:]
    recent_avg = sum(recent) / len(recent)
    blended = (1 - _RECENT_WEIGHT) * season_avg + _RECENT_WEIGHT * recent_avg

    full_trust = variance.MIN_GAMES_FULL_TRUST[kind]
    trust = min(1.0, len(own) / full_trust)
    prior_pool = variance.position_pool(position, season)
    # No shared pool warmed up yet (nobody's queried variance.py's
    # simulator today) -- fall back to the player's own blended rate,
    # same "better than blending in nothing" precedent variance.py's
    # own player_outcome_pool() sets.
    prior = sum(prior_pool) / len(prior_pool) if prior_pool else blended

    return round(prior + (blended - prior) * trust, 2)


def project_fpts(baseline: float, composite: float) -> float:
    """inhouse_fpts = baseline rate x today's matchup-quality multiplier."""
    return round(baseline * composite, 2)


async def inhouse_fpts_batch(players: list[dict[str, Any]], season: int) -> dict[int, float]:
    """
    inhouse_fpts for every unique player id in `players` (each needs at
    least id/position/edge.composite -- hitters and pitchers straight
    out of mlb_slate.py's build). Fetches every player's baseline
    concurrently, same asyncio.gather concurrency pattern as
    variance.py's player_pools_for_entries().
    """
    unique = {p["id"]: p for p in players if p.get("id")}
    ids = list(unique.values())
    baselines = await asyncio.gather(
        *(baseline_dk_points(p["id"], p["position"], season) for p in ids)
    )
    return {
        p["id"]: project_fpts(baseline, p["edge"]["composite"])
        for p, baseline in zip(ids, baselines)
    }
