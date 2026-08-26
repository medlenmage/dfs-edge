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
        composite = p["edge"]["composite"]
        if p["position"] == "P":
            delta = win_ev_delta(p.get("win_probability_pct"), win_rates.get(p["id"]))
            result[p["id"]] = project_fpts(baseline, composite, win_ev_delta=delta)
        else:
            pa_factor = _BATTING_ORDER_PA_FACTOR.get(p.get("batting_order"), 1.0)
            result[p["id"]] = project_fpts(baseline, composite, pa_factor=pa_factor)
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
_SOFTMAX_TEMPERATURE = 0.6


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

    groups: dict[str, list[dict[str, Any]]] = {}
    for p in pool:
        groups.setdefault(p["position"], []).append(p)

    def _score_group(
        position: str,
        players: list[dict[str, Any]],
        opponent_penalty: dict[int, float] | None = None,
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
            if opponent_penalty is not None:
                score -= opponent_penalty.get(p["id"], 0.0)
            raw_scores.append(score)

        # Softmax, shifted by the group's own max for numerical
        # stability -- doesn't change the resulting distribution.
        peak = max(raw_scores)
        weights = [math.exp((s - peak) / _SOFTMAX_TEMPERATURE) for s in raw_scores]
        total_weight = sum(weights)

        for p, weight in zip(players, weights):
            ownership[p["id"]] = round((weight / total_weight) * slot_count * 100, 2)

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
        _score_group(position, players, opponent_penalty)

    return ownership
