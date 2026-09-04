"""
In-house DK FPTS projection (Phase 2 of the projections/history plan --
see .claude/plans/clever-strolling-hearth.md for the full roadmap).

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

TWO VOLUME/MARKET CORRECTIONS (v2)
-----------------------------------
The multiply-by-composite model above assumes a player gets his own
typical share of playing time today. Two real, independently-available
signals say otherwise often enough to be worth a targeted correction,
applied on top of the v1 baseline rather than replacing it:

  1. Hitters: `edge.composite` has no PA-volume signal in it at all --
     it's entirely about the QUALITY of each plate appearance (platoon,
     park, weather, ...), not how MANY he gets. Batting order is a real,
     already-fetched signal for this (mlb_slate.py's `_team_hitters()`
     already attaches `batting_order` once lineups confirm) -- a
     leadoff hitter gets meaningfully more PA per game than a 9-hole
     hitter. `_BATTING_ORDER_PA_FACTOR` scales the baseline rate by
     that gap. Before lineups confirm (`batting_order` is None), this
     is a no-op (factor 1.0) -- same real, stated limitation as before.

  2. Pitchers: DK's +4 win bonus is a discrete team-dependent event, not
     a per-inning rate -- `baseline_dk_points()` bakes in this pitcher's
     own HISTORICAL win rate (however many of his starts this season
     turned into wins), which says nothing about whether HIS TEAM is
     favoured tonight. The betting market's moneyline (already fetched
     alongside the game total/spread) is a genuine, independent signal
     for that. `win_ev_delta()` is a small correction -- today's
     market-implied win probability minus his own season win rate,
     times DK's +4 -- added on top, not a full separate win-bonus term
     (that would double-count the win rate the baseline already has
     baked in). Zero when no moneyline is loaded, same "unaffected
     unless the signal exists" convention as the batting-order factor.
"""

from __future__ import annotations

import asyncio
import math
import os
from typing import Any

from app.clients import mlb
from app.services import scoring, variance
from app.services.optimizer import SLOT_REQUIREMENTS

# Recent form gets real, bounded weight -- a hot or cold last 15 games
# matters, but shouldn't swamp a full season's signal the way a naive
# last-N-only average would.
# 0.2, down from an original 0.4: over 15 games a hitter's DK-points
# mean carries a standard error of roughly +/-2 around a true mean of
# ~7-8, so 40% weight let pure noise dominate the baseline. Published
# hot/cold-streak work finds little predictive value once you regress;
# 0.15-0.20 keeps a real (small) recency signal without chasing it.
_RECENT_WEIGHT = 0.2
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


# THE MATCHUP-ONLY PROJECTION MULTIPLIER (v3)
# --------------------------------------------
# v1/v2 multiplied the baseline by edge.composite -- but the baseline
# is the player's OWN DK pts/game, which already fully contains how
# good he is, and several composite components measure his ABSOLUTE
# quality vs the league (platoon vs league OPS, contact quality vs
# league, his own SB rate, recent form, home/road). Multiplying the two
# double-counted talent: a stud got high-baseline x >1 composite, a
# scrub low-baseline x <1, and the spread exaggerated at both ends.
#
# The projection multiplier is built from only the TODAY-SPECIFIC
# components (Vegas team total, the opposing pitcher, park, bullpen +
# its recent workload, weather, umpire, and the two market props),
# renormalized over whichever are present -- exactly combine()'s
# machinery, restricted. Platoon is re-based to the player's own
# season OPS (vs-hand / overall = his actual split EDGE, centered on
# 1.0 for a player with no split) instead of vs the league. Contact
# quality, SB rate, form and home/road are deliberately excluded: the
# baseline already contains his real results in all four (form
# doubly so -- the baseline itself blends recent form).
_PROJECTION_COMPONENTS = (
    "team_total", "pitcher", "park", "bullpen", "bullpen_workload",
    "weather", "home_run", "hit_probability", "umpire",
)

# How far the multiplier's deviation from 1.0 actually moves expected
# DK points: mult = 1 + k * (raw - 1). FITTED, not guessed --
# scripts/fit_projection_damping.py regressed real archived actual
# FPTS (1,803 player-days across 10 contest dates, with look-ahead-safe
# baselines built from each player's game log strictly before each
# date) against baseline * (multiplier - 1): k = 2.34, SE 0.35,
# 95% CI (1.65, 3.03). 2.0 sits on the CI's conservative side.
#
# That k AMPLIFIES rather than damps surprised us until the spread
# explained it: stripped of the talent terms, the remaining matchup
# multiplier is tight (p10-p90 of 0.92-1.08 on real slates -- the
# component caps were tuned for the full fourteen-signal composite),
# and scaling +/-8% by ~2 lands exactly in the +/-15-20% range real
# single-game matchup effects actually span. The old full composite had
# the opposite problem at the same root -- display-tuned units, never
# FPTS units -- which is why the at-bat sim independently had to shrink
# IT to 0.35 of itself. Neither number was wrong; neither was ever
# calibrated until now.
_PROJECTION_DAMPING = 2.0

# The self-relative platoon ratio gets the same cap discipline as
# scoring.py's own components (+/-45%) so one thin split can't run away.
_PLATOON_CAP = 0.45


def projection_multiplier(
    components: dict[str, Any],
    *,
    season_ops: float | None = None,
    vs_hand_ops: float | None = None,
) -> float:
    """
    The RAW (undamped) matchup multiplier for one hitter today -- see
    the block comment above for what's in, what's out, and why.
    Callers apply _PROJECTION_DAMPING on top (project_fpts does).
    """
    total = 0.0
    used = 0.0
    for name in _PROJECTION_COMPONENTS:
        comp = components.get(name)
        weight = scoring.WEIGHTS.get(name)
        if comp is None or weight is None:
            continue
        total += comp["value"] * weight
        used += weight

    # Platoon relative to HIS OWN overall line -- the actual edge of
    # facing this hand, not a restatement of how good a hitter he is.
    if season_ops and vs_hand_ops and season_ops > 0:
        ratio = vs_hand_ops / season_ops
        ratio = max(1 - _PLATOON_CAP, min(1 + _PLATOON_CAP, ratio))
        weight = scoring.WEIGHTS["platoon"]
        total += ratio * weight
        used += weight

    return total / used if used else 1.0


def calibrated(multiplier: float) -> float:
    """mult -> 1 + k(mult - 1): the raw multiplier rescaled into real
    expected-FPTS units by the fitted k (see _PROJECTION_DAMPING)."""
    return 1.0 + _PROJECTION_DAMPING * (multiplier - 1.0)


def project_fpts(
    baseline: float,
    composite: float,
    *,
    pa_factor: float = 1.0,
    win_ev_delta: float = 0.0,
) -> float:
    """
    inhouse_fpts = baseline rate x today's matchup-quality multiplier,
    x a PA-volume correction (hitters, defaults to a no-op), + a
    win-odds correction (pitchers, defaults to a no-op).
    """
    return round(baseline * composite * pa_factor + win_ev_delta, 2)


# Plate appearances by batting-order slot, relative to a hitter's own
# typical PA/game -- a widely-cited real gap (leadoff hitters bat
# roughly one extra time per week versus a 9-hole hitter over a full
# season) applied as a multiplier on top of THIS player's own baseline
# rate, not a league-flat number, so it respects a platoon/part-time
# player's own normal workload. Approximate by design -- no historical
# PA-by-slot dataset exists in this app to fit against yet.
_BATTING_ORDER_PA_FACTOR = {
    1: 1.09, 2: 1.06, 3: 1.04, 4: 1.02, 5: 1.00,
    6: 0.98, 7: 0.96, 8: 0.94, 9: 0.91,
}


def batting_order_pa_factor(confirmed: int | None, projected: int | None) -> float:
    """
    PA-volume factor for TODAY'S slot relative to the player's USUAL
    one -- not the absolute slot factor. The baseline was earned from
    games where he batted in his normal spot, so it already contains
    his normal PA volume; applying the absolute factor handed a
    permanent leadoff hitter a free +9% every single day (and a
    permanent 9-hole hitter a standing -9%) for information the
    baseline already priced in. Only a CHANGE in slot moves real PA
    expectation vs. his own history.

    "Usual" is RotoWire's projected slot for him -- a projected lineup
    is precisely a statement of a player's normal role -- so a player
    confirmed exactly where he was projected gets 1.0, and a player
    projected 8th but confirmed leadoff (a real role change) gets the
    full ~+16% swing. With no projection loaded there's nothing to
    compare against, and with no confirmed order there's nothing to
    react to; both fall back to a neutral 1.0.
    """
    if confirmed is None or projected is None:
        return 1.0
    today = _BATTING_ORDER_PA_FACTOR.get(confirmed)
    usual = _BATTING_ORDER_PA_FACTOR.get(projected)
    if not today or not usual:
        return 1.0
    return today / usual


async def pitcher_win_rate(player_id: int, season: int) -> float | None:
    """
    Wins per start this season -- what a pitcher's own win-odds
    correction (win_ev_delta) measures TODAY's market-implied win
    probability against. None with no starts logged yet (nothing to
    compare a market number to).
    """
    game_log = await mlb.get_player_game_log(player_id, season, group="pitching")
    starts = [g for g in game_log if g.get("outs", 0) > 0]
    if not starts:
        return None
    return sum(g.get("wins", 0) or 0 for g in starts) / len(starts)


def win_ev_delta(win_probability_pct: float | None, own_win_rate: float | None) -> float:
    """
    DK points to add/subtract for today's market-implied win odds vs.
    this pitcher's own historical win rate per start -- a CORRECTION on
    top of baseline_dk_points (which already has his historical win
    rate baked in), not a standalone win-bonus term, so a favourite
    pitching poorly all season nets a real boost and a good pitcher on
    a bad team nets a real penalty. Zero (no correction) when either
    side of the comparison is missing -- no moneyline loaded, or no
    starts logged yet this season.
    """
    if win_probability_pct is None or own_win_rate is None:
        return 0.0
    today_prob = win_probability_pct / 100
    return round((today_prob - own_win_rate) * 4, 2)


async def inhouse_fpts_batch(players: list[dict[str, Any]], season: int) -> dict[int, float]:
    """
    inhouse_fpts for every unique player id in `players` (each needs at
    least id/position/edge.composite -- hitters and pitchers straight
    out of mlb_slate.py's build; a hitter's optional `batting_order` and
    a pitcher's optional `win_probability_pct` feed the v2 corrections
    above when present). Fetches every player's baseline concurrently,
    same asyncio.gather concurrency pattern as variance.py's
    player_pools_for_entries(); pitcher win rates (needed only for
    pitchers that actually have a moneyline loaded) are fetched the
    same way as a second, smaller batch.
    """
    unique = {p["id"]: p for p in players if p.get("id")}
    ids = list(unique.values())
    baselines = await asyncio.gather(
        *(baseline_dk_points(p["id"], p["position"], season) for p in ids)
    )

    win_rate_ids = [
        p["id"] for p in ids
        if p["position"] == "P" and p.get("win_probability_pct") is not None
    ]
    win_rates = dict(zip(
        win_rate_ids,
        await asyncio.gather(*(pitcher_win_rate(pid, season) for pid in win_rate_ids)),
    ))

    result: dict[int, float] = {}
    for p, baseline in zip(ids, baselines):
        if p["position"] == "P":
            # Pitchers keep the full composite unscaled for now: the
            # fitted k above was measured on the HITTER matchup
            # multiplier's own (tight) spread, and applying it to the
            # pitcher composite's much wider display-tuned spread would
            # amplify it far past anything measured. Fitting a separate
            # pitcher k needs more archived pitcher actuals than exist
            # yet -- an honest open item, not an oversight.
            delta = win_ev_delta(p.get("win_probability_pct"), win_rates.get(p["id"]))
            result[p["id"]] = project_fpts(baseline, p["edge"]["composite"], win_ev_delta=delta)
        else:
            raw = projection_multiplier(
                (p.get("edge") or {}).get("components") or {},
                season_ops=(p.get("season") or {}).get("ops"),
                vs_hand_ops=(p.get("vs_hand") or {}).get("ops"),
            )
            pa_factor = batting_order_pa_factor(
                p.get("batting_order"), p.get("projected_batting_order")
            )
            result[p["id"]] = project_fpts(baseline, calibrated(raw), pa_factor=pa_factor)
    return result


# Percentile of a player's own outcome pool used as his "ceiling" for
# leverage -- 90th percentile is a real, not-too-extreme upside read
# (the true max of a real season's game log is often one huge outlier
# game that overstates realistic tournament-winning upside).
_CEILING_PERCENTILE = 0.9


async def player_ceilings(players: list[dict[str, Any]], season: int) -> dict[int, float]:
    """
    A real, data-driven ceiling for every unique player id in `players`
    -- the 90th percentile of their own bootstrap outcome pool
    (variance.py's player_outcome_pool(), the exact same pool the Monte
    Carlo simulator draws from). This is the "upside" half of a
    leverage score (ceiling - ownership%) -- see mlb_slate.py's
    _attach_inhouse_projections() for where the two get combined.

    Reuses the same real game-log fetch baseline_dk_points() already
    made for this same batch of players (both go through
    clients/mlb.get_player_game_log()'s cache), so this costs one extra
    cheap cached lookup per player, not a second real fetch.
    """
    unique = {p["id"]: p for p in players if p.get("id")}
    ids = list(unique.values())
    pools = await asyncio.gather(
        *(variance.player_outcome_pool(p["id"], p["position"], season) for p in ids)
    )
    return {
        p["id"]: variance.ceiling_from_pool(pool, _CEILING_PERCENTILE)
        for p, pool in zip(ids, pools)
        if pool
    }


async def player_boom_bust(
    players: list[dict[str, Any]],
    season: int,
    inhouse_fpts: dict[int, float] | None = None,
) -> dict[int, dict[str, float]]:
    """
    Boom%/bust% for every unique player id in `players` -- direct tail
    reads of each player's own bootstrap outcome pool against today's
    projection (variance.boom_bust_from_pool(); the thresholds and their
    calibration live there). Sibling of player_ceilings() above, sharing
    the same already-cached game-log fetch, so this costs one cheap
    cached lookup per player rather than a second real fetch.

    The projection measured against is the RotoWire fpts already
    attached to the player when one is loaded, falling back to the
    in-house number (`inhouse_fpts`, keyed by player id) -- the same
    "whichever projection the tables are actually showing" precedence
    the rest of the slate uses.
    """
    inhouse_fpts = inhouse_fpts or {}
    unique = {p["id"]: p for p in players if p.get("id")}
    ids = list(unique.values())
    pools = await asyncio.gather(
        *(variance.player_outcome_pool(p["id"], p["position"], season) for p in ids)
    )
    out: dict[int, dict[str, float]] = {}
    for p, pool in zip(ids, pools):
        projection = (p.get("projection") or {}).get("fpts") or inhouse_fpts.get(p["id"])
        result = variance.boom_bust_from_pool(
            pool, projection, variance.player_kind(p["position"])
        )
        if result:
            out[p["id"]] = result
    return out


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
# to the others -- value and raw projected points are the dominant
# real-world drivers (weighted equally; see WHY RAW FPTS EXISTS below
# for how 1.0 was picked -- swept 0.0 to 5.0 against a real 2026-08-21
# contest's actual ownership, Spearman correlation peaked at 1.0-1.5
# (~0.58) and fell off past ~2.0, so 1.0 lands right at the plateau
# without overfitting a single real slate's own noise to the exact
# tip), team total and salary tier are real but secondary.
_VALUE_WEIGHT = 1.0
_RAW_FPTS_WEIGHT = 1.0
_TEAM_TOTAL_WEIGHT = 0.4
_SALARY_TIER_WEIGHT = 0.3

# BATTING-ORDER SPOT. Measured, and it was the largest systematic error
# in this model: reprojecting 10 real archived slates (2,027 hitter
# observations matched to real DK %Drafted) showed a monotone bias by
# lineup slot, in points of ownership --
#
#   slot   1      2      3      4      5      6      7      8      9   none
#   bias -3.48  -2.33  -2.29  -1.58  -1.25  -1.19  +0.10  -0.14  +0.21 +1.10
#
# The model was under-projecting the top of every order and getting the
# bottom right, which is the signature of a feature that simply wasn't
# there: batting order fed the FPTS projection (see
# batting_order_pa_factor) but never the ownership one, even though the
# field reads a lineup card before it buys anyone.
#
# The factor is 1.0 at leadoff and falls to 0 by the 7-hole, matching the
# shape of the measured bias rather than a guess: the error is flat
# across slots 7-9, so a term that kept falling there would invent a
# gradient the data says is not there. A hitter with no known slot gets
# no term at all, which correctly pushes him DOWN relative to confirmed
# starters -- players with no lineup spot were drafted 0.21% of the time
# against the 1.31% the model was giving them.
#
# Weight fitted by sweep against those same 10 slates -- see
# scripts/sweep_ownership_batting_order.py. Two independent criteria
# picked the same value, which is the reassuring outcome: lowest MAE
# (2.337 -> 2.206) and flattest slot gradient (3.656 -> 1.227, a 66% cut
# in the systematic part of the error) both bottom out at 1.0, and both
# get worse past 1.5 where the term starts over-correcting the top of
# the order. The env var is for the sweep, not for tuning in production.
_BATTING_ORDER_WEIGHT = float(os.environ.get("DFS_BATTING_ORDER_WEIGHT", "1.0"))

# A hitter batting 7-9 at under $3,000 is a punt, and the field treats
# him like one. Across those same 10 slates, 224 real hitters fit that
# description and the MOST-owned of them was drafted 13.6% -- not one
# cleared 15%, while the model put two of them above it (one at 21.9%).
# So this is a guard rail against a known failure mode, not a thumb on
# the scale: it has never yet clipped a player the real field actually
# liked. Re-measure it if DK's salary floor moves.
_PUNT_MAX_ORDER_SLOT = 7
_PUNT_MAX_SALARY = 3000
_PUNT_OWNERSHIP_CAP = 15.0


def batting_order_ownership_factor(
    confirmed: int | None, projected: int | None
) -> float | None:
    """
    How much the field's attention a lineup slot is worth, 1.0 at leadoff
    down to 0 from the 7-hole back. None when the slot is unknown.

    Unlike batting_order_pa_factor(), this is the ABSOLUTE slot and not
    a change from his usual one. That difference is deliberate: PA volume
    is relative to a baseline that already contains his normal workload,
    but ownership isn't -- the field looks at tonight's card and buys the
    guy hitting second, whether or not that is where he usually hits.
    """
    slot = confirmed or projected
    if not slot or not 1 <= slot <= 9:
        return None
    return max(0.0, (_PUNT_MAX_ORDER_SLOT - slot) / (_PUNT_MAX_ORDER_SLOT - 1))

# THE TEAM-STACK LAYER (hitters only) -- see WHY A TEAM-STACK LAYER
# EXISTS in project_ownership()'s docstring for the real measured
# failure that motivated it. MLB hitter ownership is driven team-first:
# the field picks a team to stack, then picks bats from it, so
# teammates' ownerships have to move together instead of being scored
# independently. Weighted well above any individual-merit signal
# because that's what the real archived data says: a team's implied
# runs alone rank-correlates with its real summed hitter ownership at
# r=+0.80 across 15 real slates, stronger than any per-player signal in
# this module.
_TEAM_STACK_WEIGHT = 4.0

# The two team-level signals, blended into one 0-1 desirability score.
# Both measured against real archived DK contest standings (see
# scripts/probe_stack_features.py): implied runs r=+0.80, opposing
# starter's salary (a clean, always-available proxy for how good the
# arm the field thinks this offense has to beat is) r=-0.72. Both are
# used as WITHIN-SLATE PERCENTILES, never raw -- a 5.0 implied total
# means something different on a 2-game slate than a 13-game one, and
# rank generalizes where the raw number doesn't.
_STACK_IMPLIED_RUNS_WEIGHT = 0.7
_STACK_OPPOSING_SP_WEIGHT = 0.3

# A real, well-known field-behavior pattern the four signals above
# can't see on their own: when the pitcher a hitter is facing is
# himself heavily owned (a "safe," popular pitcher play), public
# attention -- and roster spots -- concentrate on that pitcher at the
# expense of the offense he's shutting down. Modest by design (0.25 --
# roughly a quarter of value/raw_fpts' own weight): this is a real but
# secondary leverage signal, not meant to swamp the hitter's own
# matchup/projection quality. Applied AFTER pitchers' own group is
# scored (see the two-pass structure in project_ownership() below) as
# a subtraction scaled by how far the opposing pitcher's OWN modelled
# ownership sits above the pitcher group's average -- a pitcher at
# exactly league-average ownership contributes no adjustment at all.
_OPPONENT_PITCHER_CHALK_WEIGHT = 0.25

# Softmax temperature: how concentrated ownership gets on the top
# play(s) within a position group. Lower = more chalk-heavy (the best
# play dominates); higher = flatter, more evenly spread. Temperature
# only affects concentration, never which players rank highest (that's
# entirely raw_scores, fixed above) -- so this was tuned separately,
# empirically. Originally set to 0.3 against 4 real slates predating
# raw_fpts (see WHY RAW FPTS EXISTS above); re-swept after adding
# raw_fpts (which widens the realistic range of raw_scores, since a
# player can now hit BOTH value's and raw_fpts' ceiling at once) against
# the same real 2026-08-21 contest used to tune raw_fpts's own weight:
# 0.3 let a real top play hit 68.7% modelled ownership (this slate's
# real max was 36.8% -- no real large-field GPP concentrates anywhere
# near that hard), and Spearman correlation ALSO kept improving as
# temperature rose, peaking at 0.6 (0.594, vs 0.3's 0.583) before
# falling off past ~0.8 -- a rare case where the more realistic choice
# and the better-correlated choice were the same value.
#
# Re-swept a third time when the team-stack layer landed (below), and
# moved 0.6 -> 1.0. That's expected, not a surprise: adding a real new
# term widens the range raw_scores can span, so the same temperature
# now concentrates harder than it used to. Swept jointly with the stack
# weights across 4 real archived contests over 0.9-2.5; the optimum is
# a broad plateau (stack weight 2.0-2.5, implied-runs split 0.6-0.8,
# temperature 1.0-1.2) rather than a knife-edge, and the values chosen
# sit at its centre rather than at its exact argmin -- with only 4
# slates the 1-2% between them is noise, and the lower, rounder values
# are the more conservative read of a signal this new.
#
# Re-swept a FOURTH time (jointly with the stack weight) when the
# ownership pool was restricted to the starting nine and multi-slot
# eligibility landed. Both changes reshape every group -- fewer, more
# concentrated players per group, some competing in two groups at once
# -- so the old constants, tuned against the diluted
# every-rostered-hitter pool, genuinely mis-fit the new one (chalk MAE
# 8.00 at the old 2.0/1.0 vs 7.03 here, on the same slates). The
# optimum moved along a weight/temperature diagonal and was probed
# past the sweep grid's edge to confirm it turns (stack MAE bottoms at
# weight ~4.0-4.5 then rises); 4.0/1.4 sits at the balance point
# rather than either metric's argmin.
# RE-SWEPT (2026-08-31) against the full archive, now 19 real
# contests / 13 dates rather than the 4 slates the 1.4 above was fitted
# on. Measured on the LIVE pipeline (build_slate with include_inhouse),
# not the Supabase reconstruction -- the archive has no historical Vegas
# implied runs, which neutralises the team-stack layer, this module's
# heaviest signal at weight 4.0, and sweeping a parameter with its
# biggest input dead fits the wrong optimum entirely (the same dates
# score rho 0.36-0.43 handicapped vs 0.54-0.75 with the layer live).
#
#  temp    MAE   chalk MAE   pred on chalk   bias on >=20% owned
#  1.40    3.18     10.93        15.4%            -11.9
#  1.10    3.31     10.26        18.0%             -9.5   <- chosen
#  0.90    3.55     10.78        20.6%             -7.0
#  0.75    3.83     11.96        23.3%             -4.3
#  0.60    4.22     13.99        27.1%             -0.6
#  0.50    4.61     16.51        30.3%             +2.5
#  (real ownership on those top-30 chalk players: 25.7%)
#
# CHALK MAE is the metric that decides whether a lineup is genuinely
# contrarian, and it bottoms at 1.10 -- so that is the pick. Overall
# MAE is 4% worse there, which is the right trade: overall MAE is
# dominated by the hundreds of sub-1%-owned players where a
# point-and-a-half of error changes no decision, while the chalk is
# where being wrong actually costs entries.
#
# Note what the rest of the curve says, because it is the more
# important finding: the chalk bias only reaches zero around temp 0.60,
# and by then chalk MAE has got 28% WORSE and the max prediction has
# run to 81%. Sharpening amplifies whoever the model already thinks is
# top, so it only helps while that ordering is right. The residual
# under-calling of extreme chalk is therefore a RANKING limitation, not
# a spread one, and no temperature fixes it -- see the README entry for
# what would.
_SOFTMAX_TEMPERATURE = 1.1


def _percentiles(values: dict[str, float]) -> dict[str, float]:
    """Rank each key's value into a 0-1 within-slate percentile, ties
    sharing the average rank. A single-entry input is neutral (0.5) --
    a percentile is meaningless without something to rank against."""
    if len(values) < 2:
        return {k: 0.5 for k in values}
    order = sorted(values, key=lambda k: values[k])
    out: dict[str, float] = {}
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        rank = (i + j) / 2
        for k in range(i, j + 1):
            out[order[k]] = rank / (len(order) - 1)
        i = j + 1
    return out


def _team_stack_scores(pool: list[dict[str, Any]]) -> dict[str, float]:
    """A 0-1 "how much does the field want to stack this team" score per
    team, from the two team-level signals that real archived contest
    data says actually drive it (see the weights above).

    Both inputs are optional: a slate with no odds loaded still gets a
    real opposing-starter signal, and vice versa. A team missing both
    lands at a neutral 0.5 rather than being pushed to either extreme.
    """
    salary_by_id = {p["id"]: p.get("salary") for p in pool}

    implied_by_team: dict[str, float] = {}
    opp_sp_salary_by_team: dict[str, float] = {}
    for p in pool:
        team = p.get("team")
        if not team:
            continue
        if p.get("implied_runs") is not None:
            implied_by_team[team] = p["implied_runs"]
        opp_salary = salary_by_id.get(p.get("opponent_pitcher_id"))
        if opp_salary:
            opp_sp_salary_by_team[team] = opp_salary

    implied_pct = _percentiles(implied_by_team)
    opp_sp_pct = _percentiles(opp_sp_salary_by_team)

    scores: dict[str, float] = {}
    for team in {*implied_by_team, *opp_sp_salary_by_team}:
        parts, weights = [], []
        if team in implied_pct:
            parts.append(_STACK_IMPLIED_RUNS_WEIGHT * implied_pct[team])
            weights.append(_STACK_IMPLIED_RUNS_WEIGHT)
        if team in opp_sp_pct:
            # Inverted: a better (pricier) opposing arm suppresses the
            # field's appetite for stacking this offense.
            parts.append(_STACK_OPPOSING_SP_WEIGHT * (1.0 - opp_sp_pct[team]))
            weights.append(_STACK_OPPOSING_SP_WEIGHT)
        scores[team] = sum(parts) / sum(weights) if weights else 0.5
    return scores


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
    A hitter entry may also carry `opponent_pitcher_id` (the id of the
    starting pitcher he's actually facing) to enable the leverage
    adjustment described below -- optional, a hitter missing it (or
    whose opposing pitcher isn't itself in `pool`) just skips it.

    Combines four signals into one raw "ownership propensity" score,
    then softmax-normalizes WITHIN each DK roster-slot group so every
    group's total lands at slot_count x 100% (e.g. OF sums to 300% --
    3 real roster spots; C sums to 100% -- 1 spot). Grouping by
    position also captures scarcity for free: a shallow position (few
    good options) naturally concentrates ownership on its top play(s)
    without a separate scarcity term.

      - value: fpts / salary -- the standard points-per-dollar signal
        every real DFS player looks at first. Min-max normalised to a
        0-1 scale within the position group before weighting (see WHY
        VALUE IS NORMALISED below) -- the raw ratio alone is far too
        small a number to compete with the other signals.
      - raw fpts: the player's own projected points, ALSO min-max
        normalised within the group, independent of price -- see WHY
        RAW FPTS EXISTS below for the real gap this closes.
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

    WHY VALUE IS NORMALISED (a real bug, found via backtesting)
    -------------------------------------------------------------
    Backtested against 4 real slates' actual DK contest ownership
    (`%Drafted` from real contest-standings exports): this model
    originally used a raw fpts/salary ratio for `value` -- typically
    ~0.001-0.003 -- added straight into the same sum as `team_total`
    (~0.7-1.5) and `salary_tier` (0-1) via `_VALUE_WEIGHT * value`
    (weight 1.0). Despite being weighted highest and documented as
    "the dominant real-world driver," value's raw magnitude was
    100-1000x smaller than the other two signals, so it contributed
    essentially nothing to which players actually got ranked highest --
    ownership was really being driven almost entirely by salary_tier
    (distance from the group's own midpoint), which is exactly why the
    backtest showed near-zero rank correlation with real ownership
    (0.023 average Spearman across 4 real slates) and a suspiciously
    flat spread (1-4% on a real 365-player pool, vs. real ownership's
    20-33%+ for genuine chalk). Min-max normalising value onto the
    same 0-1 scale salary_tier already uses fixes the actual ranking,
    not just the spread -- softmax temperature only changes how
    concentrated the final distribution is, never which players end up
    on top, so temperature alone could never have fixed this.

    WHY RAW FPTS EXISTS (a second real bug, found the same way)
    -------------------------------------------------------------
    Backtested against a real contest-standings export (2026-08-21,
    joined against that same day's still-cached live projections --
    the freshest possible same-day comparison): a min-priced shortstop
    (Brock Rodden, $2300, 5.93 projected points) modelled at 26.9%
    ownership -- the single highest in a 20-player SS pool -- while
    Bobby Witt Jr. ($6200, 11.21 projected points, essentially double
    Rodden's own projection) modelled at only 4.7%, backwards from any
    real GPP field. Root cause: `value` alone has NO signal for
    absolute point volume, only points-PER-DOLLAR -- and real MLB DFS
    pricing means a cheap complementary player routinely has a
    slightly BETTER raw points-per-dollar ratio than a $6000+ stud
    (points don't scale linearly with salary at the top of a range),
    so min-max normalising value alone can hand the single highest
    score in a group to a min-priced role player over a real stud,
    every time. Real ownership doesn't work that way -- name-brand/
    high-floor studs draw serious chalk ownership independent of pure
    salary efficiency (this same real slate: Mookie Betts, Xander
    Bogaerts, and Corey Seager all drew real double-digit-or-close
    ownership on unremarkable value ratios, purely on raw projection/
    reputation). `raw_fpts`, min-max normalised the same way `value`
    is, gives studs a real signal that isn't purely price-relative,
    without discarding `value`'s own genuine role for the min-priced
    end of a position group.

    WHY A TEAM-STACK LAYER EXISTS (the largest real error found so far)
    -------------------------------------------------------------
    The four signals above are all PER-PLAYER: every hitter is scored
    on his own merit and normalized against others at his position.
    That is the wrong shape for MLB. Real DFS fields pick a TEAM to
    stack first and bats second, so a chalk team's hitters all get
    owned together, whatever their individual value ranks say.

    Measured, not assumed. Against 4 real archived DK contests, global
    Spearman looked respectable at +0.601 -- while the model was
    missing entire real stacks by more than its whole ownership range:

        2026-08-21  CLE  real 154.8% summed hitter ownership, model 47.0%
        2026-08-24  MIN  real 180.3%,                         model 121.2%
        2026-08-25  MIN  real 129.8%,                         model  47.7%

    On that 08-21 slate five separate CLE bats ran 18-30% real
    ownership against 2-8% modelled. Their individual merit was priced
    roughly right; what the model had no way to see was that the field
    had piled onto that one offense. This is exactly why global rank
    correlation can't be the headline metric here (see
    scripts/diagnose_ownership.py, which reports the ones that matter).

    The fix is a real team-level layer (see _team_stack_scores()),
    added to every hitter's raw score before his position group's
    softmax so teammates move together. Its two inputs were chosen by
    measuring which team-level signals actually separate stacked teams
    from ignored ones on real archived slates -- not by picking
    plausible-sounding ones (scripts/probe_stack_features.py):

        implied team runs      r=+0.80   (15 real slates)
        opposing SP salary     r=-0.72   (a clean, always-available
                                          proxy for the quality of arm
                                          the field thinks this offense
                                          has to beat)
        best-5-bats fpts       r=+0.47   } real but secondary, and
        cheapest-5 stack cost  r=+0.42   } largely redundant with the
                                         } two above -- not built
        best-5 points-per-$    r=-0.13   -- no signal, deliberately
                                            NOT built despite sounding
                                            like it should work

    Both inputs are used as WITHIN-SLATE PERCENTILES rather than raw
    values, since a 5.0 implied total means something different on a
    2-game slate than on a 13-game one.

    Measured result across the same 4 real contests:

                        before      after
        chalk MAE       9.08pp  ->  7.26pp   (top 20 by real ownership)
        team-stack MAE 16.88pp  -> 11.36pp
        Spearman        +0.601  ->  +0.745

    What this deliberately is NOT: a full hierarchical stack model
    (predict P(team, stack_size), then distribute within the stack).
    That's the right long-term shape, but honestly fitting a stack-size
    distribution needs far more than 4 archived slates -- so this
    captures the dominant measured effect with the data that actually
    exists, and the fuller version waits for the archive to grow.

    LEVERAGE: A CHALKY OPPOSING PITCHER SUPPRESSES A HITTER'S OWNERSHIP
    -------------------------------------------------------------
    A real, well-documented field-behavior pattern the four signals
    above have no way to see on their own: public attention (and roster
    spots) concentrate on a popular "safe pitcher" play at the expense
    of the offense he's shutting down. Applied as a two-pass process --
    the `P` group is scored first and completely unaffected by anything
    below, then every hitter's raw score gets `_OPPONENT_PITCHER_CHALK_
    WEIGHT * max(0, (opposing pitcher's own just-computed ownership% -
    the P group's average) / that average)` subtracted before ITS
    group's own softmax -- a real, asymmetric penalty (only ever a
    penalty, never a symmetric bonus for a below-average-owned
    opponent) scaled by how far above pitcher-average the specific
    opposing arm actually sits, zero for a pitcher at or below it.
    """
    ownership: dict[int, float] = {}

    # A multi-eligible player ("1B/3B") competes in EVERY slot group
    # he's eligible for, and his reported ownership is the sum across
    # them -- real %Drafted is his share of entries rostering him at
    # ANY slot. Keeping only the first-listed slot made a "2B/SS"
    # player invisible to the SS group entirely, understating him and
    # overstating the remaining SS chalk (each group always sums to
    # slots x 100%, so his missing share got redistributed). Entries
    # carry `positions` (all slots) when the caller knows them, falling
    # back to the single `position` everywhere else.
    groups: dict[str, list[dict[str, Any]]] = {}
    for p in pool:
        for slot in (p.get("positions") or [p["position"]]):
            groups.setdefault(slot, []).append(p)

    team_stack = _team_stack_scores(pool)

    def _score_group(
        position: str,
        players: list[dict[str, Any]],
        opponent_penalty: dict[int, float] | None = None,
        *,
        use_team_stack: bool = False,
    ) -> None:
        slot_count = SLOT_REQUIREMENTS.get(position)
        if not slot_count:
            return  # not a real DK roster slot -- skip rather than guess

        player_salaries = [p["salary"] for p in players if p.get("salary")]
        if not player_salaries:
            return
        min_salary, max_salary = min(player_salaries), max(player_salaries)
        mid_salary = (min_salary + max_salary) / 2
        salary_half_span = max((max_salary - min_salary) / 2, 1)

        raw_values = [
            (p.get("fpts") or 0.0) / p["salary"] if p.get("salary") else 0.0
            for p in players
        ]
        min_value, max_value = min(raw_values), max(raw_values)
        value_span = max(max_value - min_value, 1e-9)

        raw_fpts = [p.get("fpts") or 0.0 for p in players]
        min_fpts, max_fpts = min(raw_fpts), max(raw_fpts)
        fpts_span = max(max_fpts - min_fpts, 1e-9)

        raw_scores = []
        for p, raw_value, fpts in zip(players, raw_values, raw_fpts):
            salary = p.get("salary") or 0
            value = (raw_value - min_value) / value_span
            fpts_norm = (fpts - min_fpts) / fpts_span

            implied_runs = p.get("implied_runs") or scoring.LEAGUE_IMPLIED_RUNS
            team_total = implied_runs / scoring.LEAGUE_IMPLIED_RUNS

            salary_tier = abs(salary - mid_salary) / salary_half_span

            score = (
                _VALUE_WEIGHT * value
                + _RAW_FPTS_WEIGHT * fpts_norm
                + _TEAM_TOTAL_WEIGHT * team_total
                + _SALARY_TIER_WEIGHT * salary_tier
            )
            order_factor = batting_order_ownership_factor(
                p.get("batting_order"), p.get("projected_batting_order")
            )
            if order_factor is not None:
                score += _BATTING_ORDER_WEIGHT * order_factor
            # Hitters only -- pitcher ownership is a separate model
            # (see Pass 1) and is already the best-calibrated group here.
            if use_team_stack:
                score += _TEAM_STACK_WEIGHT * team_stack.get(p.get("team"), 0.5)
            if opponent_penalty is not None:
                score -= opponent_penalty.get(p["id"], 0.0)
            raw_scores.append(score)

        # Softmax, shifted by the group's own max for numerical
        # stability -- doesn't change the resulting distribution.
        peak = max(raw_scores)
        weights = [math.exp((s - peak) / _SOFTMAX_TEMPERATURE) for s in raw_scores]
        total_weight = sum(weights)

        shares = [(weight / total_weight) * slot_count * 100 for weight in weights]

        # Punt guard rail. Clipped ownership is handed back to the rest of
        # the group rather than dropped, so the group still sums to the
        # roster spots it represents -- silently losing it would make
        # every OTHER player in the group read low.
        excess = 0.0
        keep = []
        for p, share in zip(players, shares):
            slot = p.get("batting_order") or p.get("projected_batting_order")
            capped = (
                slot is not None
                and slot >= _PUNT_MAX_ORDER_SLOT
                and (p.get("salary") or 0) < _PUNT_MAX_SALARY
                and share > _PUNT_OWNERSHIP_CAP
            )
            if capped:
                excess += share - _PUNT_OWNERSHIP_CAP
            keep.append(0.0 if capped else share)
        if excess > 0 and sum(keep) > 0:
            scale = 1 + excess / sum(keep)
            shares = [
                _PUNT_OWNERSHIP_CAP if k == 0.0 and s_ > _PUNT_OWNERSHIP_CAP else k * scale
                for k, s_ in zip(keep, shares)
            ]

        for p, share in zip(players, shares):
            # Accumulate, not overwrite -- a multi-eligible player's
            # total is his share summed across every group he's in.
            ownership[p["id"]] = round(ownership.get(p["id"], 0.0) + share, 2)

    # Pass 1: pitchers, scored exactly as any other group -- nothing
    # downstream can affect a pitcher's own ownership.
    pitcher_players = groups.pop("P", [])
    if pitcher_players:
        _score_group("P", pitcher_players)
    pitcher_ownerships = [ownership[p["id"]] for p in pitcher_players if p["id"] in ownership]
    avg_pitcher_ownership = sum(pitcher_ownerships) / len(pitcher_ownerships) if pitcher_ownerships else None

    # Pass 2: every other group, with a real (one-directional) leverage
    # penalty for any hitter whose own opposing pitcher just scored
    # above the pitcher group's own average.
    opponent_penalty: dict[int, float] = {}
    if avg_pitcher_ownership:
        for p in pool:
            opp_id = p.get("opponent_pitcher_id")
            opp_own = ownership.get(opp_id) if opp_id is not None else None
            if opp_own is not None:
                opponent_penalty[p["id"]] = _OPPONENT_PITCHER_CHALK_WEIGHT * max(
                    0.0, (opp_own - avg_pitcher_ownership) / avg_pitcher_ownership
                )

    for position, players in groups.items():
        _score_group(position, players, opponent_penalty, use_team_stack=True)

    return ownership
