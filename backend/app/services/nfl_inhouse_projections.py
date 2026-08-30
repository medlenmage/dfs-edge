"""
In-house DK FPTS and ownership% projections for NFL -- the sibling of
services/inhouse_projections.py (MLB). Same two-part shape: a real
per-player production baseline scaled by today's matchup, and a
separate relative-to-the-field ownership model.

Built almost entirely from machinery that already existed for the NFL
simulator, rather than a parallel stack:

  - Real per-game DK outcomes from nflverse game logs
    (clients/nfl.get_player_game_log() / get_team_game_log()) scored by
    nfl_dk_points.py, with nfl_variance.py's own same-position shared
    pool carrying thin samples.
  - nfl_scoring.score_player()'s matchup score, read through its
    `composite` multiplier.

ONE THING THAT HAD TO BE FIXED FIRST
-------------------------------------
DraftKings identifies players by its own numeric id; nflverse by GSIS
id. Nothing bridged them, so every lookup against a real game log
silently missed and fell back to a same-position average -- confirmed
live on a real week-1 slate, where six of six checked players returned
a pool with exactly one distinct value. clients/nfl.resolve_player_id()
now does that matching by name, and every player on the slate carries a
resolved `nflverse_id`. A player who genuinely has no prior-season
history (a rookie, or someone who missed the whole season) resolves to
None and correctly leans entirely on the position pool.

WHY THE OWNERSHIP MODEL IS A HEURISTIC, NOT A FIT
---------------------------------------------------
There is no archive of real NFL contest ownership in this app to train
against, the same reason MLB's own ownership model is still a
transparent weighted formula. The structure here is built so that a
real fit can replace the hand-set weights later without rewriting the
model: every signal is named, separately weighted, and normalized onto
a comparable scale.

A KNOWN LIMITATION, STATED PLAINLY: QB OWNERSHIP RUNS FLAT
------------------------------------------------------------
On a real 16-game slate the chalkiest QB models around 9%, where real
large-field GPP chalk is typically higher. The cause is measurable
rather than mysterious: a DK NFL salary file carries every rostered
QB (88 on that slate, against 32 real starters), and a backup with no
prior-season log is projected at his position's replacement level
rather than zero -- correctly, since the model genuinely can't tell a
third-stringer from a rookie who will start. Those 50-odd
never-going-to-play QBs still absorb part of the group's 100%.

Fixing it properly needs a real starter/depth-chart signal, which no
free source this app reads provides. Tuning a separate per-position
temperature to paper over it would be fitting a number to one slate
with nothing to validate against, so it isn't done here. The ranking
within the QB group is unaffected -- only the levels are compressed.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

from app.clients import nfl
from app.services import nfl_dk_points, nfl_scoring, nfl_variance

# Per-game exponential recency decay for the rolling multi-season log
# below -- the most recent game gets weight 1.0, the one before it
# 0.93, and so on (a game a full season back carries ~0.3x). Replaces
# the old flat season-average + 40%-recent-5 blend, whose recency
# window was mostly noise (the review's point: the last 5 games of a
# PRIOR season are often Week 17-18 rest games, the worst possible
# signal) and whose baseline was pinned to the prior season forever --
# in-season games never updated anyone, and a player who changed teams
# was projected in his old role all year.
_GAME_DECAY = 0.93

# The prior season's Week 18 is dropped from every rolling log:
# clinched teams rest starters and playoff-bound players sit, so it's
# systematically unrepresentative. Detecting exactly WHICH teams had
# clinched would need standings data this app doesn't fetch -- dropping
# the week outright for everyone is the stated approximation.
_PRIOR_SEASON_DROP_WEEK = 18

# The percentile of the shared same-position pool used as the shrinkage
# target for a thin-sample or no-history player -- NOT the pool's mean.
#
# This is a correction for real survivorship bias, and it matters a lot.
# The position pool is built from games players actually appeared in, so
# its mean is the average of a *starter's* game (a real week-1 WR pool
# measured 7.6). Regressing a player toward that says "absent evidence,
# assume he produces like a starter," which is backwards: a player with
# no prior-season log is, empirically, a rookie or a deep reserve.
# Before this correction, six min-priced bench WRs with 0.0 projections
# were each handed the pool mean and came out ranked above Ja'Marr
# Chase, because their invented production divided by a $3,000 salary
# was the best points-per-dollar in the group.
#
# The 25th percentile is replacement level for the position (2.2 for
# WRs on that same real pool), which is the honest prior for a player
# the model has no record of. A player with a real full season of games
# is trusted entirely on his own history and never touches this.
_PRIOR_PERCENTILE = 0.25


# Positional FPTS floors for the OWNERSHIP pool (see nfl_slate.py's
# use): a player projecting below his position's floor isn't someone
# the field rosters, and letting him into the pool splits the group's
# fixed softmax total across phantom candidates. Floors from the
# review's own suggested thresholds; DST has only 32 real candidates
# and every one is genuinely rosterable, so no floor.
OWNERSHIP_FPTS_FLOOR: dict[str, float] = {"QB": 8.0, "RB": 4.0, "WR": 4.0, "TE": 3.0}


def _pool_prior(pool: list[float]) -> float | None:
    if not pool:
        return None
    ordered = sorted(pool)
    return ordered[round(_PRIOR_PERCENTILE * (len(ordered) - 1))]


def _decayed_mean(values: list[float]) -> float:
    """Recency-weighted mean of a chronological series -- the newest
    value carries weight 1.0, each step back multiplies by _GAME_DECAY.
    Once the current season has a handful of real games, they dominate
    the prior season's on their own, with no hand-set crossover point.
    """
    n = len(values)
    total = weight_sum = 0.0
    for i, v in enumerate(values):
        w = _GAME_DECAY ** (n - 1 - i)
        total += v * w
        weight_sum += w
    return total / weight_sum if weight_sum else 0.0


async def rolling_game_log(nflverse_id: str, current_season: int) -> list[dict[str, Any]]:
    """
    A player's game log spanning the PRIOR season plus the CURRENT
    season to date, in chronological order -- the rolling window the
    baseline actually needs, instead of a prior season pinned forever.

    The prior season's Week 18 is dropped (see _PRIOR_SEASON_DROP_WEEK).
    The current season's file may simply not exist yet (nflverse
    publishes it once real games are played) -- that's the normal
    pre-season case, not an error, and it degrades to prior-season-only.
    """
    prior = await nfl.get_player_game_log(nflverse_id, current_season - 1)
    prior = [g for g in prior if g.get("week") != _PRIOR_SEASON_DROP_WEEK]
    try:
        current = await nfl.get_player_game_log(nflverse_id, current_season)
    except Exception:
        current = []
    return prior + current


async def baseline_dk_points(
    nflverse_id: str | None, position: str, season: int
) -> float:
    """
    One expected-DK-points-per-game rate for an offensive player,
    blending his season average with recent form and shrinking toward
    the shared same-position pool for thin samples -- the same
    shrink-toward-a-prior technique nfl_variance.py and MLB's own
    baseline both already use.

    A player with no prior-season history at all (nflverse_id is None)
    gets the position pool's own average, which is the honest answer:
    the model knows nothing about him specifically.
    """
    pos = (position or "").strip().upper()
    if pos not in nfl_variance.OFFENSIVE_POSITIONS:
        pos = "WR"

    own: list[float] = []
    if nflverse_id:
        game_log = await rolling_game_log(nflverse_id, season)
        own = nfl_variance.own_games(game_log)
        nfl_variance.contribute_to_position_pool(pos, season, own)

    prior = _pool_prior(nfl_variance.position_pool(pos, season))

    if not own:
        return round(prior, 2) if prior is not None else 0.0

    blended = _decayed_mean(own)

    # Trust from the RAW game count (how much evidence exists), decay
    # only shapes the estimate itself -- otherwise a full prior season
    # would count as "half a season" of trust purely because its games
    # are older.
    trust = min(1.0, len(own) / nfl_variance.MIN_GAMES_FULL_TRUST[pos])
    if prior is None:
        return round(blended, 2)
    return round(prior + (blended - prior) * trust, 2)


async def dst_baseline_dk_points(team: str, season: int) -> float:
    """
    The DST equivalent -- a team's own defense scores every week, so
    there's no appearance/inactive concept to filter out the way there
    is for a skill player.

    That same fact is why this shrinks toward the DST pool's MEAN while
    baseline_dk_points() above deliberately doesn't: every team plays
    every week, so the league-wide DST pool has none of the survivorship
    bias that makes the skill-position pool's mean a bad prior. Its mean
    really is the average team-defense game.
    """
    game_log = await nfl.get_team_game_log(team, season - 1)
    game_log = [g for g in game_log if g.get("week") != _PRIOR_SEASON_DROP_WEEK]
    try:
        game_log += await nfl.get_team_game_log(team, season)
    except Exception:
        pass  # current season not published yet -- the normal pre-season case
    own = [nfl_dk_points.dst_game_points(g) for g in game_log]
    nfl_variance.contribute_to_dst_pool(season, own)

    prior_pool = nfl_variance.dst_pool(season)
    prior = sum(prior_pool) / len(prior_pool) if prior_pool else None

    if not own:
        return round(prior, 2) if prior is not None else 0.0

    blended = _decayed_mean(own)

    trust = min(1.0, len(own) / nfl_variance.MIN_GAMES_FULL_TRUST["DST"])
    if prior is None:
        return round(blended, 2)
    return round(prior + (blended - prior) * trust, 2)


def project_fpts(baseline: float, composite: float) -> float:
    """baseline production rate x today's matchup-quality multiplier."""
    return round(baseline * composite, 2)


async def inhouse_fpts_batch(players: list[dict[str, Any]], season: int) -> dict[str, float]:
    """
    inhouse_fpts keyed by DK id for every player in `players`. Each
    needs `dk_id`, `position`, `team`, and an `edge` carrying
    `composite`; offensive players also need the `nflverse_id` resolved
    onto them by nfl_slate.py. Fetches every baseline concurrently,
    same asyncio.gather pattern as nfl_variance.player_pools_for_entries().
    """
    unique = {p["dk_id"]: p for p in players if p.get("dk_id") and p.get("edge")}
    entries = list(unique.values())

    async def _baseline(p: dict[str, Any]) -> float:
        if p["position"] == "DST":
            return await dst_baseline_dk_points(p["team"], season)
        return await baseline_dk_points(p.get("nflverse_id"), p["position"], season)

    baselines = await asyncio.gather(*(_baseline(p) for p in entries))
    return {
        p["dk_id"]: project_fpts(baseline, p["edge"]["composite"])
        for p, baseline in zip(entries, baselines)
    }


_CEILING_PERCENTILE = 0.9


async def player_ceilings(players: list[dict[str, Any]], season: int) -> dict[str, float]:
    """
    The 90th percentile of each player's own bootstrap outcome pool --
    the exact pool the Monte Carlo simulator draws from, so a ceiling
    here and a simulated ceiling there can never disagree. The "upside"
    half of a leverage score; see nfl_slate._attach_inhouse_projections().
    """
    unique = {p["dk_id"]: p for p in players if p.get("dk_id")}
    entries = list(unique.values())

    async def _pool(p: dict[str, Any]) -> list[float]:
        if p["position"] == "DST":
            return await nfl_variance.dst_outcome_pool(p["team"], season)
        return await nfl_variance.player_outcome_pool(
            p.get("nflverse_id"), p["position"], season
        )

    pools = await asyncio.gather(*(_pool(p) for p in entries))
    return {
        p["dk_id"]: nfl_variance.ceiling_from_pool(pool, _CEILING_PERCENTILE)
        for p, pool in zip(entries, pools)
        if pool
    }


# --------------------------------------------------------------------------
# In-house ownership%
# --------------------------------------------------------------------------

# DK Classic NFL is 9 roster slots, so ownership across the whole slate
# sums to 900% -- a hard structural constraint, and enforcing it is free
# accuracy: an error in one player's estimate then correctly propagates
# as an offsetting adjustment to the players he competes with for the
# same slot, instead of floating free.
#
# FLEX is the wrinkle. It's a roster slot, not a position, so its single
# 100% can't be modelled as its own group -- it has to be distributed
# across the RB/WR/TE pools that actually fill it. This split is an
# approximation of real DK NFL FLEX usage (WR most often, RB close
# behind, TE rarely), stated rather than fitted for the same reason the
# weights below are: no archive of real NFL FLEX usage exists here yet.
_FLEX_SHARE = {"RB": 0.40, "WR": 0.50, "TE": 0.10}
_BASE_SLOTS = {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "DST": 1}
SLOT_TARGETS = {
    position: count + _FLEX_SHARE.get(position, 0.0)
    for position, count in _BASE_SLOTS.items()
}

# Carried over unchanged from MLB's own model, where they were tuned by
# sweeping against real DK contest-standings exports: value and raw
# projected points dominate, team total and salary tier are real but
# secondary. These signals are sport-agnostic (points per dollar means
# the same thing in both), so reusing validated numbers beats inventing
# fresh unvalidated ones.
_VALUE_WEIGHT = 1.0
_RAW_FPTS_WEIGHT = 1.0
_TEAM_TOTAL_WEIGHT = 0.4
_SALARY_TIER_WEIGHT = 0.3

# NFL-specific. Cheap enablers draw ownership out of proportion to their
# group-wide value rank, because their real appeal is the salary they
# free up elsewhere in a lineup rather than their own production. Ranking
# value a second time among only the sub-threshold players surfaces
# that; a single group-wide value rank buries it.
_PUNT_SALARY_THRESHOLD = 4000
_PUNT_VALUE_WEIGHT = 0.35

# NFL-specific, and the structural counterpart to MLB's opposing-pitcher
# leverage penalty. The field stacks a popular QB with his own
# pass-catchers, so a chalky QB pulls his WRs and TEs UP -- the opposite
# direction from MLB, where a chalky pitcher pushes the hitters he faces
# DOWN. Applied one-directionally (a below-average-owned QB produces no
# adjustment at all, rather than a symmetric penalty) and only to WR/TE:
# a team's RBs aren't part of the stack the field is actually building,
# and real game script makes them closer to anti-correlated with it.
_QB_STACK_WEIGHT = 0.30
_STACKABLE_POSITIONS = {"WR", "TE"}

# How concentrated ownership gets on the top plays within a group.
# Temperature never changes WHICH players rank highest (that's entirely
# the weighted signals above) -- only how much of the group's total
# lands on them -- so it was swept separately against a real 16-game
# slate. Starting from MLB's own 0.6 and checking each group's peak
# against real large-field GPP ownership, 0.6 was also the right answer
# here: it puts the chalkiest RB at 23.8%, TE at 21.3%, and DST at
# 15.9%, all squarely in real territory, with only the single top WR
# reaching the cap below. Every lower value tested (0.5, 0.42) pushed
# three or more groups into the cap at once, which is the model
# over-concentrating rather than the field genuinely piling in.
_SOFTMAX_TEMPERATURE = 0.6

# No single player is ever rostered by more than a fraction of a real
# large-field GPP field, however obvious the play. This is a real,
# observed structural property, not a modelling convenience -- the
# highest actual %Drafted across every real DK contest export archived
# in this app is 36.8%. A bare softmax has no such ceiling: on a real
# slate the top WR came out at 54.9% simply because he led his group on
# every input at once, which no real field ever does.
#
# Excess above the cap is redistributed proportionally across the
# players who aren't capped, so the group still sums to exactly its
# share of DK's 9 roster slots. Iterated, because redistributing can
# push a second player over the cap.
_MAX_PLAYER_OWNERSHIP = 40.0


def _apply_ownership_cap(shares: list[float], cap: float) -> list[float]:
    # The cap is only satisfiable when the group's own average share is
    # under it -- otherwise capping every player would leave the group
    # summing to less than its real slot allocation, quietly breaking
    # the 900% constraint that's the more fundamental of the two. A
    # small enough group (a short slate's DSTs, say) genuinely has to
    # average above the cap, and there the structural sum wins.
    if not shares or sum(shares) / len(shares) > cap:
        return shares

    capped: set[int] = set()
    for _ in range(20):
        newly = [i for i, s in enumerate(shares) if s > cap + 1e-9 and i not in capped]
        if not newly:
            break
        excess = sum(shares[i] - cap for i in newly)
        for i in newly:
            shares[i] = cap
            capped.add(i)
        free = [i for i in range(len(shares)) if i not in capped]
        free_total = sum(shares[i] for i in free)
        if not free or free_total <= 0:
            break
        for i in free:
            shares[i] += excess * shares[i] / free_total
    return shares


def project_ownership(pool: list[dict[str, Any]]) -> dict[str, float]:
    """
    Ownership% for every player in `pool` at once -- ownership is
    relative to the rest of the field, never a property one player has
    alone.

    Each entry needs: dk_id, position (QB/RB/WR/TE/DST), team, salary,
    fpts (whichever projection is driving this), and implied_total (that
    player's team's Vegas implied points, or None).

    Scored per position group and softmax-normalized so each group lands
    at its share of DK's 9 roster slots (see SLOT_TARGETS). Grouping by
    position also captures positional scarcity for free: a thin group
    concentrates ownership on its top plays without a separate term.

    Two passes, so the QB-stack correlation below can read real
    just-computed QB ownership rather than a proxy for it.
    """
    ownership: dict[str, float] = {}

    groups: dict[str, list[dict[str, Any]]] = {}
    for p in pool:
        groups.setdefault(p["position"], []).append(p)

    def _score_group(
        position: str,
        players: list[dict[str, Any]],
        stack_boost: dict[str, float] | None = None,
    ) -> None:
        slot_target = SLOT_TARGETS.get(position)
        if not slot_target:
            return  # not a real DK roster slot -- skip rather than guess

        salaries = [p["salary"] for p in players if p.get("salary")]
        if not salaries:
            return
        min_salary, max_salary = min(salaries), max(salaries)
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

        punt_values = [
            v for p, v in zip(players, raw_values)
            if (p.get("salary") or 0) <= _PUNT_SALARY_THRESHOLD
        ]
        punt_min = min(punt_values) if punt_values else 0.0
        punt_span = max((max(punt_values) - punt_min) if punt_values else 0.0, 1e-9)

        raw_scores = []
        for p, raw_value, fpts in zip(players, raw_values, raw_fpts):
            salary = p.get("salary") or 0
            value = (raw_value - min_value) / value_span
            fpts_norm = (fpts - min_fpts) / fpts_span

            implied = p.get("implied_total") or nfl_scoring.LEAGUE_AVG_IMPLIED_TOTAL
            team_total = implied / nfl_scoring.LEAGUE_AVG_IMPLIED_TOTAL

            # Salary tier is deliberately non-monotone -- both the
            # priciest "safe stud" and the cheap value play draw
            # ownership, and the middle of a position's range is the
            # dead zone. But the cheap half of that, and the punt bonus
            # below, only describe a cheap player who can actually
            # SCORE. A DK NFL pool carries every rostered player, so
            # without gating on real production these two terms hand a
            # 3rd-string QB the same "cheap and therefore popular" bonus
            # as a genuine min-priced starter -- measured on a real
            # slate, that flattened the QB group so far that the
            # chalkiest QB modelled at 6.7% (real week-1 chalk is
            # multiples of that), because 80-odd unrosterable backups
            # were each collecting cheapness credit. Scaling both by the
            # player's own normalized projection means the field punts
            # toward cheap players who can score, and never toward cheap
            # players who can't.
            if salary >= mid_salary:
                salary_tier = (salary - mid_salary) / salary_half_span
            else:
                salary_tier = (mid_salary - salary) / salary_half_span * fpts_norm

            punt_value = (
                (raw_value - punt_min) / punt_span * fpts_norm
                if salary and salary <= _PUNT_SALARY_THRESHOLD
                else 0.0
            )

            score = (
                _VALUE_WEIGHT * value
                + _RAW_FPTS_WEIGHT * fpts_norm
                + _TEAM_TOTAL_WEIGHT * team_total
                + _SALARY_TIER_WEIGHT * salary_tier
                + _PUNT_VALUE_WEIGHT * punt_value
            )
            if stack_boost is not None:
                score += stack_boost.get(p["dk_id"], 0.0)
            raw_scores.append(score)

        # Shifted by the group's own max for numerical stability --
        # doesn't change the resulting distribution.
        peak = max(raw_scores)
        weights = [math.exp((s - peak) / _SOFTMAX_TEMPERATURE) for s in raw_scores]
        total_weight = sum(weights)

        shares = [(w / total_weight) * slot_target * 100 for w in weights]
        shares = _apply_ownership_cap(shares, _MAX_PLAYER_OWNERSHIP)

        for p, share in zip(players, shares):
            ownership[p["dk_id"]] = round(share, 2)

    # Pass 1: QBs, scored with nothing downstream able to affect them.
    qb_players = groups.pop("QB", [])
    if qb_players:
        _score_group("QB", qb_players)

    qb_ownerships = [ownership[p["dk_id"]] for p in qb_players if p["dk_id"] in ownership]
    avg_qb_ownership = sum(qb_ownerships) / len(qb_ownerships) if qb_ownerships else None

    # Pass 2: everyone else, with the stack boost applied to the
    # pass-catchers of whichever QBs came out above the QB-group average.
    stack_boost: dict[str, float] = {}
    if avg_qb_ownership:
        team_qb_ownership: dict[str, float] = {}
        for p in qb_players:
            own = ownership.get(p["dk_id"])
            if own is not None and p.get("team"):
                team_qb_ownership[p["team"]] = max(team_qb_ownership.get(p["team"], 0.0), own)
        for p in pool:
            if p["position"] not in _STACKABLE_POSITIONS:
                continue
            qb_own = team_qb_ownership.get(p.get("team"))
            if qb_own is None:
                continue
            stack_boost[p["dk_id"]] = _QB_STACK_WEIGHT * max(
                0.0, (qb_own - avg_qb_ownership) / avg_qb_ownership
            )

    for position, players in groups.items():
        _score_group(position, players, stack_boost)

    return ownership
