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
import math
from typing import Any

from app.clients import mlb
from app.services import scoring, variance
from app.services.optimizer import SLOT_REQUIREMENTS

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


# --------------------------------------------------------------------------
# In-house ownership% v1 (Phase 3)
# --------------------------------------------------------------------------
#
# No historical ownership data exists anywhere in this app yet (see
# history_db.py's Phase 1 -- it just started archiving real RotoWire
# uploads going forward) -- there's nothing to statistically fit
# against on day one. This is a transparent heuristic instead, same
# "clearly-approximate, documented" philosophy contest.py's own payout
# curve already uses. Revisit with a real fit once Phase 1's archive
# holds enough real slates (the README roadmap's "Results tracking and
# weight backtesting" item).

# How much each signal moves a player's ownership propensity relative
# to the others -- value is the dominant real-world driver, team total
# and salary tier are real but secondary.
_VALUE_WEIGHT = 1.0
_TEAM_TOTAL_WEIGHT = 0.4
_SALARY_TIER_WEIGHT = 0.3

# Softmax temperature: how concentrated ownership gets on the top
# play(s) within a position group. Lower = more chalk-heavy (the best
# play dominates); higher = flatter, more evenly spread.
_SOFTMAX_TEMPERATURE = 2.5


def project_ownership(pool: list[dict[str, Any]]) -> dict[int, float]:
    """
    Ownership% for every player in `pool`, all at once -- ownership is
    inherently relative to the rest of the field, not something a
    single player has in isolation.

    Each pool entry needs: id, position (a single DK roster-slot code
    -- P, C, 1B, 2B, 3B, SS, or OF; a multi-eligible player should pass
    whichever position he's being considered at), salary, fpts
    (whichever projection is driving this -- inhouse_fpts if
    available, RotoWire's otherwise), and implied_runs (that player's
    team's Vegas implied run total, or None if no odds are loaded).

    Combines three signals into one raw "ownership propensity" score,
    then softmax-normalizes WITHIN each DK roster-slot group so every
    group's total lands at slot_count x 100% (e.g. OF sums to 300% --
    3 real roster spots; C sums to 100% -- 1 spot). Grouping by
    position also captures scarcity for free: a shallow position (few
    good options) naturally concentrates ownership on its top play(s)
    without a separate scarcity term.

      - value: fpts / salary -- the standard points-per-dollar signal
        every real DFS player looks at first.
      - team total: that player's team's Vegas implied runs relative
        to league average -- popular high-total teams get stacked (and
        owned) more.
      - salary tier: a mild bump for both ends of the position's own
        salary range -- rock-bottom "punt" plays get overowned for the
        salary relief they free up elsewhere in a lineup, and the
        most-expensive "safe" studs get overowned as the low-risk
        default pick. Distance from the position's own mid-salary,
        not the whole slate's, since "expensive" means something
        different for a catcher than for an outfielder.
    """
    ownership: dict[int, float] = {}

    groups: dict[str, list[dict[str, Any]]] = {}
    for p in pool:
        groups.setdefault(p["position"], []).append(p)

    for position, players in groups.items():
        slot_count = SLOT_REQUIREMENTS.get(position)
        if not slot_count:
            continue  # not a real DK roster slot -- skip rather than guess

        player_salaries = [p["salary"] for p in players if p.get("salary")]
        if not player_salaries:
            continue
        min_salary, max_salary = min(player_salaries), max(player_salaries)
        mid_salary = (min_salary + max_salary) / 2
        salary_half_span = max((max_salary - min_salary) / 2, 1)

        raw_scores = []
        for p in players:
            salary = p.get("salary") or 0
            fpts = p.get("fpts") or 0.0
            value = fpts / salary if salary else 0.0

            implied_runs = p.get("implied_runs") or scoring.LEAGUE_IMPLIED_RUNS
            team_total = implied_runs / scoring.LEAGUE_IMPLIED_RUNS

            salary_tier = abs(salary - mid_salary) / salary_half_span

            raw_scores.append(
                _VALUE_WEIGHT * value
                + _TEAM_TOTAL_WEIGHT * team_total
                + _SALARY_TIER_WEIGHT * salary_tier
            )

        # Softmax, shifted by the group's own max for numerical
        # stability -- doesn't change the resulting distribution.
        peak = max(raw_scores)
        weights = [math.exp((s - peak) / _SOFTMAX_TEMPERATURE) for s in raw_scores]
        total_weight = sum(weights)

        for p, weight in zip(players, weights):
            ownership[p["id"]] = round((weight / total_weight) * slot_count * 100, 2)

    return ownership
