"""
The NFL contest generator + Monte Carlo simulator -- the NFL sibling of
contest.py (MLB), tied to nfl_variance.py the same way contest.py is
tied to variance.py.

Reuses contest.py's genuinely sport-agnostic pieces directly rather
than re-deriving them: the payout-curve math (_payout_curve,
_custom_payout_curve, _block_average_payouts -- pure functions of
rank/prize_pool/shape, no MLB-specific concept anywhere in them),
field_exposure(), _split_duplicate_payouts(), _attach_duplicate_counts(),
_field_baseline(), _evaluate_batch_against_field(), CONTEST_TYPES (the
same DK contest archetypes -- double-up/GPP-small/large/milly -- apply
to any DK sport), and ContestError. Only the genuinely sport-specific
pieces are rewritten here: the 9-slot roster, drawing from
nfl_optimizer.build_player_pool() instead of optimizer.py's version,
and nfl_variance.player_pools_for_entries()/simulate_batch() instead
of variance.py's.

SCOPED DELIBERATELY NARROWER THAN contest.py -- STATED PLAINLY
------------------------------------------------------------------
Per an explicit checklist from the user (allow_duplicates, percent-to-
first, self_play, field_sharpness, min/max salary, max_exposure,
field_size, entries to build), NOT every feature contest.py has grown
over many separate passes was ported:

  - No `engine="atbat"` alternative -- MLB's at-bat-level slate
    simulator (atbat_sim.py) has no NFL analog; this always uses the
    bootstrap outcome-pool engine (nfl_variance.py).
  - No DK-entries-file import/mirroring (contest.py's
    build_dk_entries_simulated) and no post-hoc reshape/filter step
    (contest.py's reshape_batch).

Player ids are strings throughout (DK's own numeric id, matching
nfl_optimizer.py's convention), not the ints contest.py's MLB player
ids are.

STACK ARCHETYPES (built in, not user-selectable)
--------------------------------------------------
Every generated lineup (both the user's own entries and the sampled
opponent field) is built toward a real, weighted NFL GPP stack shape,
matching an explicit real-world construction taxonomy the user gave
directly rather than left to unconstrained per-slot sampling:

  PRIMARY (same team -- the QB stack, or a non-QB game-correlated pair):
    qb_naked  -- a QB alone, no pass-catchers. Restricted to real
                 RUNNING quarterbacks only (see _classify_pool() below)
                 -- his own rushing floor is the real "stack" instead
                 of a teammate, not a construction choice that makes
                 sense for a pure pocket passer.
    qb_1/2/3  -- a QB + N of his own team's pass-catchers (WR, TE, or a
                 real pass-catching RB -- see PASS_CATCHING_RB_TARGETS_
                 PER_GAME below). qb_1/qb_2 are weighted heaviest
                 (PRIMARY_STACK_WEIGHTS) -- the most common real
                 construction and the one that wins the most real
                 tournaments -- every type still gets built sometimes.
    rb_dst    -- an unrelated primary type: one team's RB + that same
                 team's own DST, no QB involved at all.

  SECONDARY (1-2 mini-groups from team(s) OTHER than the primary's own
  team, filling the rest of the roster -- a real mix of WR/TE/RB from
  the same team/game, not restricted to pass-catchers the way the
  primary is): "1+1" (two different teams, one player each), "2+1"/
  "1+2" (one team gets 2, another gets 1 -- kept as two separate listed
  shapes since a bring-back bias can land on either group, not because
  they're functionally different once assigned), "2" (one team gets 2,
  no second secondary group).

  BRING-BACK: with real, deliberate probability (BRINGBACK_PROBABILITY,
  not left to chance), a lineup's first secondary group is biased
  toward the PRIMARY stack's own real opponent -- rostering the other
  side of that specific game alongside the stack, the classic "betting
  on a shootout" GPP move. Every generated entry reports the real,
  as-built (not just as-intended) result as `has_bringback` -- true
  whenever ANY rostered player's team is the primary stack's real
  opponent, computed from the actual finished roster, not merely
  whether the bias was applied.

Any roster slots left after the primary + secondary groups are filled
as ordinary weighted one-off picks, same as before this feature.
"""

from __future__ import annotations

import csv
import io
import random
from collections import Counter
from collections.abc import Callable
from typing import Any

import numpy as np

from app.clients import nfl
from app.services import nfl_structural, nfl_variance
from app.services.contest import (
    CONTEST_TYPES,
    FIELD_SHARPNESS_LEVELS,
    FLOOR_CEILING_PERCENTILE,
    MAX_FIELD_SIZE,
    MAX_SAMPLE_SIZE,
    MAX_USER_LINEUPS,
    RAKE_PCT,
    ContestError,
    _attach_duplicate_counts,
    _block_average_payouts,
    _custom_payout_curve,
    _evaluate_batch_against_field,
    _field_baseline,
    _duplication_risk,
    _field_weight_fn,
    _ownership_weight,
    _split_duplicate_payouts,
    field_exposure,
)
from app.services.nfl_optimizer import (
    DEFAULT_MIN_SALARY,
    SALARY_CAP,
    SLOT_REQUIREMENTS,
    SLOT_TYPES,
    build_player_pool,
)

# How hard entry generation leans toward higher-projected players --
# same sampling technique and same exponent as contest.py's own
# _FPTS_SAMPLING_EXPONENT.
_FPTS_SAMPLING_EXPONENT = 3.0
_FPTS_FLOOR = 0.1


def _validate_salary_range(min_salary: int, max_salary: int) -> None:
    if max_salary > SALARY_CAP:
        raise ContestError(f"max_salary ({max_salary}) can't be more than the ${SALARY_CAP} salary cap.")
    if min_salary > max_salary:
        raise ContestError("min_salary can't be more than max_salary.")


def _fpts_weight(p: dict[str, Any]) -> float:
    return max(p["projected_fpts"], _FPTS_FLOOR) ** _FPTS_SAMPLING_EXPONENT


# --------------------------------------------------------------------------
# Stack archetypes -- see the module docstring for the full taxonomy.
# --------------------------------------------------------------------------

PRIMARY_STACK_TYPES = ("qb_naked", "qb_1", "qb_2", "qb_3", "rb_dst")
# qb_1/qb_2 weighted heaviest per the user's own real-world framing;
# every type still gets built sometimes. A real, stated, tunable
# approximation -- not derived from real win-rate data, same
# "clearly-labeled" convention contest.py's own STACK_SHAPE_WEIGHTS uses.
PRIMARY_STACK_WEIGHTS = (1.0, 3.0, 3.0, 1.0, 1.0)

SECONDARY_STACK_TYPES = ("1+1", "2+1", "1+2", "2")
SECONDARY_STACK_GROUPS: dict[str, list[int]] = {"1+1": [1, 1], "2+1": [2, 1], "1+2": [2, 1], "2": [2]}
SECONDARY_STACK_WEIGHTS = (1.0, 1.0, 1.0, 1.0)

# Real, deliberate chance a lineup's secondary stack is biased toward
# the primary stack's own real opponent -- a real, common GPP move
# ("betting on a shootout"), not left entirely to chance.
BRINGBACK_PROBABILITY = 0.4

# A QB's average real rushing volume (nfl.PRIOR_SEASON game logs) above
# which he counts as a genuine "running QB" -- calibrated against real
# 2025 names the user gave as examples (Josh Allen, Lamar Jackson,
# Jayden Daniels all comfortably clear this on their real season
# rushing volume; a pure pocket passer sits well under it).
RUNNING_QB_CARRIES_PER_GAME = 5.0

# A RB's average real target volume above which he counts as a genuine
# receiving threat, eligible to fill a QB stack's pass-catcher slot the
# same way a WR/TE would.
PASS_CATCHING_RB_TARGETS_PER_GAME = 3.5


async def _classify_pool(
    candidates_by_slot: dict[str, list[dict[str, Any]]], season: int
) -> tuple[frozenset[str], frozenset[str]]:
    """
    Real running-QB and pass-catching-RB classification from
    nfl.PRIOR_SEASON's actual game logs -- run ONCE per contest-
    generation call, not per lineup, and passed down as a plain lookup
    into the (synchronous) sampling functions below.

    Uses clients/nfl.get_grouped_season_stats() directly (one parsed-
    and-cached structure for the whole season) rather than looping
    get_player_game_log() per player -- a real, measured difference at
    real slate size: classifying a full week's 60-100+ QB/RB pool one
    per-player call at a time took over two minutes the first time this
    was built; one shared grouped fetch is a single pass regardless of
    how many players get classified.
    """
    qbs = {p["id"]: p for p in candidates_by_slot.get("QB", [])}
    rbs = {p["id"]: p for p in candidates_by_slot.get("RB", [])}

    grouped = await nfl.get_grouped_season_stats(season)

    def _avg(pid: str, field: str) -> float:
        log = grouped.get(pid) or []
        if not log:
            return 0.0
        return sum(g.get(field, 0.0) for g in log) / len(log)

    qb_ids = list(qbs)
    rb_ids = list(rbs)
    qb_carries = [_avg(pid, "carries") for pid in qb_ids]
    rb_targets = [_avg(pid, "targets") for pid in rb_ids]

    running_qb_ids = frozenset(pid for pid, c in zip(qb_ids, qb_carries) if c >= RUNNING_QB_CARRIES_PER_GAME)
    pass_catching_rb_ids = frozenset(
        pid for pid, t in zip(rb_ids, rb_targets) if t >= PASS_CATCHING_RB_TARGETS_PER_GAME
    )
    return running_qb_ids, pass_catching_rb_ids


def _pick_primary(
    candidates_by_slot: dict[str, list[dict[str, Any]]],
    running_qb_ids: frozenset[str],
    weight_fn: Callable[[dict[str, Any]], float],
    rng: random.Random,
) -> dict[str, Any] | None:
    """One of PRIMARY_STACK_TYPES for this lineup, weighted, restricted
    to what the real pool can actually support (qb_naked needs a real
    running QB available; rb_dst needs a team with both an eligible RB
    and DST). Returns None if literally nothing is buildable."""
    qbs = candidates_by_slot.get("QB", [])
    if not qbs:
        return None
    dst_teams = {p["team"] for p in candidates_by_slot.get("DST", [])}
    rb_dst_teams = {p["team"] for p in candidates_by_slot.get("RB", []) if p["team"] in dst_teams}

    types, weights = [], []
    for t, w in zip(PRIMARY_STACK_TYPES, PRIMARY_STACK_WEIGHTS):
        if t == "qb_naked" and not any(p["id"] in running_qb_ids for p in qbs):
            continue
        if t == "rb_dst" and not rb_dst_teams:
            continue
        types.append(t)
        weights.append(w)
    if not types:
        return None
    chosen = rng.choices(types, weights=weights, k=1)[0]

    if chosen == "rb_dst":
        team = rng.choice(sorted(rb_dst_teams))
        return {"type": chosen, "team": team, "qb_id": None, "pass_catcher_count": 0}

    pool = [p for p in qbs if p["id"] in running_qb_ids] if chosen == "qb_naked" else qbs
    qb_weights = [weight_fn(p) for p in pool]
    qb = rng.choices(pool, weights=qb_weights, k=1)[0]
    count = {"qb_naked": 0, "qb_1": 1, "qb_2": 2, "qb_3": 3}[chosen]
    return {"type": chosen, "team": qb["team"], "qb_id": qb["id"], "pass_catcher_count": count}


def _pick_secondary_teams(
    candidates_by_slot: dict[str, list[dict[str, Any]]],
    primary_team: str,
    bringback_team: str | None,
    shape_groups: list[int],
    weight_fn: Callable[[dict[str, Any]], float],
    rng: random.Random,
) -> list[tuple[str, int]] | None:
    """
    Assign each of `shape_groups`' sizes to a real team OTHER than the
    primary's own, weighted toward whichever teams carry the most
    aggregate `weight_fn` signal among their available players -- same
    technique contest.py's own _pick_stack_teams() uses for MLB.
    `bringback_team`, if given, is preferred for the FIRST group when
    it has enough eligible players -- the deliberate bring-back bias
    (see BRINGBACK_PROBABILITY). Returns None if some group has no
    feasible team left.
    """
    team_pool: dict[str, dict[str, dict[str, Any]]] = {}
    for players in candidates_by_slot.values():
        for p in players:
            if p["team"] != primary_team:
                team_pool.setdefault(p["team"], {})[p["id"]] = p
    teams_available = {t: list(v.values()) for t, v in team_pool.items()}

    assigned: list[tuple[str, int]] = []
    used_teams: set[str] = set()
    for idx, size in enumerate(shape_groups):
        candidates = {
            t: players for t, players in teams_available.items() if t not in used_teams and len(players) >= size
        }
        if not candidates:
            return None
        if idx == 0 and bringback_team and bringback_team in candidates:
            team = bringback_team
        else:
            teams = list(candidates)
            weights = [sum(weight_fn(p) for p in candidates[t]) for t in teams]
            team = rng.choices(teams, weights=weights, k=1)[0]
        assigned.append((team, size))
        used_teams.add(team)
    return assigned


def _pick_stack_plan(
    candidates_by_slot: dict[str, list[dict[str, Any]]],
    running_qb_ids: frozenset[str],
    weight_fn: Callable[[dict[str, Any]], float],
    rng: random.Random,
) -> tuple[dict[str, Any] | None, list[tuple[str, int]] | None]:
    """
    One lineup's full stack plan -- a primary archetype (or None if
    nothing buildable at all, e.g. every QB was excluded) and its
    secondary team assignment (or None if no feasible secondary teams
    exist for the chosen shape, in which case the caller falls back to
    an unconstrained secondary for that one lineup rather than failing
    it outright). Picked once per lineup, outside the retry loop below
    -- same reason contest.py's own MLB stack-shape picks a shape once
    per lineup: a genuinely harder shape gets a fair, dedicated shot at
    a working team assignment instead of losing out to whichever easier
    shape happens to get rolled on a given retry attempt.
    """
    primary = _pick_primary(candidates_by_slot, running_qb_ids, weight_fn, rng)
    if primary is None:
        return None, None

    shape = rng.choices(SECONDARY_STACK_TYPES, weights=SECONDARY_STACK_WEIGHTS, k=1)[0]
    groups = SECONDARY_STACK_GROUPS[shape]

    bringback_team = None
    if rng.random() < BRINGBACK_PROBABILITY:
        primary_team_players = [
            p for players in candidates_by_slot.values() for p in players if p["team"] == primary["team"]
        ]
        if primary_team_players:
            bringback_team = primary_team_players[0].get("opponent")

    secondary_teams = _pick_secondary_teams(
        candidates_by_slot, primary["team"], bringback_team, groups, weight_fn, rng
    )
    return primary, secondary_teams


def _build_candidate_pool(
    slate: dict[str, Any],
    *,
    included_game_pks: list[Any] | None = None,
    projection_source: str = "rotowire",
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Shared setup: the eligible-by-slot pool and the fixed 9-slot fill
    order, or a ContestError if either is empty."""
    pool = build_player_pool(
        slate,
        included_game_pks=included_game_pks,
        projection_source=projection_source,
    )
    if not pool:
        raise ContestError(
            "No optimizable players for this week -- upload both a "
            "DraftKings salary CSV and a RotoWire projections CSV first."
        )

    candidates_by_slot = {slot: [p for p in pool if slot in p["slots"]] for slot in SLOT_TYPES}
    missing = [slot for slot, players in candidates_by_slot.items() if not players]
    if missing:
        raise ContestError(f"No eligible players for roster slot(s): {', '.join(missing)}.")

    slot_order: list[str] = []
    for slot, count in SLOT_REQUIREMENTS.items():
        slot_order.extend([slot] * count)

    return candidates_by_slot, slot_order


# How hard salary pacing biases a pick toward expensive players when a
# lineup is behind the CAP's pace (see _sample_one_lineup). Re-swept on
# a real Week 1 slate with NO salary floor at all (800 entries per
# strength), because the floor was removed and the old sweep had been
# run with one in place:
#
#   strength   median salary   under $47k   avg proj pts   top player exposure
#     0.0         48,900          23.6%        112.53            16%
#     3.0         49,800           9.9%        115.34            26%
#     5.0         49,900           5.9%        115.81            32%
#     6.0         49,900           5.9%        115.94            33%   <- chosen
#     8.0         49,900           4.0%        116.35            38%
#    10.0         50,000           4.1%        115.98            38%
#
# Salary and points climb together up to 8.0, so the real trade-off is
# diversity: weighting harder concentrates the contest onto the same
# expensive players. 6.0 matches MLB's own constant, sits at the points
# plateau before 10.0 starts regressing (and losing distinct builds),
# and keeps the chalkiest player in the low 30s.
_SALARY_PACING_STRENGTH = 6.0

_PASS_CATCHER_SLOTS = ("WR", "TE", "FLEX")
_RB_DST_ELIGIBLE_SLOTS = ("RB", "FLEX")


def _sample_one_lineup(
    candidates_by_slot: dict[str, list[dict[str, Any]]],
    slot_order: list[str],
    rng: random.Random,
    weight_fn: Callable[[dict[str, Any]], float],
    *,
    excluded_ids: frozenset[str] = frozenset(),
    # The primitive stays policy-neutral (0 = no floor); the public
    # generators above it are where DEFAULT_MIN_SALARY is applied.
    min_salary: int = 0,
    max_salary: int = SALARY_CAP,
    primary: dict[str, Any] | None = None,
    pass_catching_rb_ids: frozenset[str] = frozenset(),
    secondary_teams: list[tuple[str, int]] | None = None,
    max_duplication_risk: float | None = None,
) -> dict[str, Any] | None:
    """
    Build one randomly-weighted lineup within the salary cap (and the
    narrower [min_salary, max_salary] range, if given). At each slot,
    the pick is weighted by `weight_fn` (ownership% for the opponent
    field, projected points for the user's own entries) but constrained
    to what still leaves enough budget for the cheapest possible player
    at every remaining slot. `excluded_ids` removes players entirely
    (e.g. ones that have hit an exposure cap).

    `primary`/`secondary_teams`, if given (see _pick_primary()/
    _pick_secondary_teams()), constrain this lineup toward the chosen
    stack archetype: the primary's own QB (if any) is required outright;
    its pass-catcher count is preferred at WR/TE/FLEX slots (restricted
    to the primary's own team, WR/TE, or a real pass-catching RB) when
    a qualifying player is available at that slot, falling back to an
    ordinary unrestricted pick for that one slot otherwise -- an
    unsatisfied requirement fails the WHOLE attempt at the final check
    below, not silently. Each secondary team similarly prefers filling
    its own remaining need at any RB/WR/TE/FLEX slot.

    Returns None if this particular random walk couldn't complete
    (including an unsatisfied primary/secondary requirement); the
    caller retries.
    """
    used_ids: set[str] = set()
    picks: list[dict[str, Any]] = []
    salary_so_far = 0

    qb_required_id = primary.get("qb_id") if primary else None
    pass_catchers_needed = primary.get("pass_catcher_count", 0) if primary else 0
    primary_team = primary.get("team") if primary else None
    need_rb_dst_rb = primary is not None and primary["type"] == "rb_dst"
    need_rb_dst_dst = primary is not None and primary["type"] == "rb_dst"
    secondary_needed: dict[str, int] = dict(secondary_teams) if secondary_teams else {}

    for i, slot in enumerate(slot_order):
        remaining_slots = slot_order[i + 1 :]
        eligible = [
            p for p in candidates_by_slot[slot] if p["id"] not in used_ids and p["id"] not in excluded_ids
        ]
        if not eligible:
            return None

        if slot == "QB" and qb_required_id is not None:
            eligible = [p for p in eligible if p["id"] == qb_required_id]
            if not eligible:
                return None
        elif slot == "DST" and need_rb_dst_dst:
            restricted = [p for p in eligible if p["team"] == primary_team]
            if restricted:
                eligible = restricted
        elif slot in _RB_DST_ELIGIBLE_SLOTS and need_rb_dst_rb:
            restricted = [p for p in eligible if p["team"] == primary_team and p["position"] == "RB"]
            if restricted:
                eligible = restricted
        elif slot in _PASS_CATCHER_SLOTS and pass_catchers_needed > 0 and primary_team:
            restricted = [
                p for p in eligible
                if p["team"] == primary_team
                and (p["position"] in ("WR", "TE") or p["id"] in pass_catching_rb_ids)
            ]
            if restricted:
                eligible = restricted
        elif secondary_needed and slot in ("RB", "WR", "TE", "FLEX"):
            needy_teams = {t for t, n in secondary_needed.items() if n > 0}
            restricted = [p for p in eligible if p["team"] in needy_teams]
            if restricted:
                eligible = restricted

        remaining_pool = {
            s: [
                p for p in candidates_by_slot[s]
                if p["id"] not in used_ids and p["id"] not in excluded_ids
            ]
            for s in remaining_slots
        }
        min_cost_of_rest = sum(
            min((p["salary"] for p in remaining_pool[s]), default=0) for s in remaining_slots
        )
        # The most the remaining slots could possibly cost -- the
        # symmetric counterpart to min_cost_of_rest, and the piece that
        # makes the salary FLOOR reachable by construction instead of
        # by rejection. Without it the walk only ever guarded against
        # OVERspending: nothing stopped it drifting cheap early, and a
        # lineup that had already fallen too far behind still played
        # out all nine slots before failing the floor check at the end,
        # burning a retry. Pruning picks that can't mathematically
        # reach the floor turns "build then reject" into "only build
        # what can succeed".
        max_cost_of_rest = sum(
            max((p["salary"] for p in remaining_pool[s]), default=0) for s in remaining_slots
        )
        budget = SALARY_CAP - salary_so_far - min_cost_of_rest
        affordable = [p for p in eligible if p["salary"] <= budget]
        if min_salary:
            reachable = [
                p for p in affordable
                if salary_so_far + p["salary"] + max_cost_of_rest >= min_salary
            ]
            # Only apply the floor-aware pruning when it leaves
            # something -- an empty result means this branch was
            # already doomed, and failing here is the same outcome as
            # failing the final check, just sooner.
            if reachable:
                affordable = reachable
        if not affordable:
            return None

        weights = [weight_fn(p) for p in affordable]
        if _SALARY_PACING_STRENGTH and max_cost_of_rest > 0:
            # SALARY PACING, paced against the CAP -- not against a
            # floor. The hard reachability prune above is only a
            # necessary condition; it can't bite until the walk is
            # already nearly doomed, which is why adding a floor alone
            # once dropped the build rate to 5/300 in a real
            # measurement. This steers instead: `pressure` is how much
            # of the remaining slots' MAXIMUM possible spend this
            # lineup still needs to finish at the cap (0 = already
            # there, 1 = must max out every remaining slot), and picks
            # get weighted toward salary in proportion to it. A lineup
            # on pace samples exactly as before; one drifting cheap
            # pulls itself back.
            #
            # Originally keyed off `min_salary`, which made the whole
            # mechanism dead code the moment the floor was removed --
            # measured directly: with min_salary=0 every pacing
            # strength from 0 to 10 produced the identical batch
            # (median $48,900, 23.6% of entries under $47,000). Pacing
            # toward SALARY_CAP is the same construction MLB's
            # generator uses and works with no floor at all.
            pressure = (SALARY_CAP - salary_so_far) / max_cost_of_rest
            pressure = max(0.0, min(1.0, pressure))
            if pressure > 0:
                cheapest = min(p["salary"] for p in affordable) or 1
                weights = [
                    w * (p["salary"] / cheapest) ** (_SALARY_PACING_STRENGTH * pressure)
                    for w, p in zip(weights, affordable)
                ]
        pick = rng.choices(affordable, weights=weights, k=1)[0]

        picks.append(pick)
        used_ids.add(pick["id"])
        salary_so_far += pick["salary"]

        if slot == "DST" and need_rb_dst_dst and pick["team"] == primary_team:
            need_rb_dst_dst = False
        elif slot in _RB_DST_ELIGIBLE_SLOTS and need_rb_dst_rb and pick["team"] == primary_team and pick["position"] == "RB":
            need_rb_dst_rb = False
        elif (
            pass_catchers_needed > 0
            and pick["team"] == primary_team
            and (pick["position"] in ("WR", "TE") or pick["id"] in pass_catching_rb_ids)
        ):
            pass_catchers_needed -= 1
        elif pick["team"] in secondary_needed and secondary_needed[pick["team"]] > 0:
            secondary_needed[pick["team"]] -= 1

    if pass_catchers_needed > 0 or need_rb_dst_rb or need_rb_dst_dst or any(n > 0 for n in secondary_needed.values()):
        return None  # couldn't fully satisfy the chosen stack shape -- caller retries
    if not (min_salary <= salary_so_far <= max_salary):
        return None

    # picks[0] is always whatever filled the QB slot -- NOT necessarily
    # a primary-team player for the rb_dst archetype (QB is picked
    # unconstrained there) -- find a real primary-team player instead.
    primary_team_pick = next((p for p in picks if p["team"] == primary_team), None) if primary else None
    primary_opponent = primary_team_pick.get("opponent") if primary_team_pick else None
    has_bringback = primary_opponent is not None and any(p["team"] == primary_opponent for p in picks)

    # secondary_teams' own sizes were already confirmed fully satisfied
    # by the "couldn't fully satisfy the chosen stack shape" check
    # above -- the team names chosen for it are exactly the real
    # secondary teams that ended up in this roster, no need to re-derive
    # them from the picks.
    secondary_team_names = [t for t, _ in secondary_teams] if secondary_teams else []

    # Cumulative (log-product) ownership: how likely another entry in
    # the real field is an exact copy of this lineup. Rejecting here and
    # letting the caller retry is the same mechanism the salary range
    # uses, and the same one contest.py applies on the MLB side. Only
    # the user's OWN entries filter on it -- the opponent field is
    # supposed to contain its duplicates, because the real one does.
    duplication_risk = _duplication_risk(picks)
    if max_duplication_risk is not None and duplication_risk > max_duplication_risk:
        return None

    return {
        "salary_used": salary_so_far,
        "projected_points": round(sum(p["projected_fpts"] for p in picks), 2),
        "total_ownership_pct": round(sum(p["ownership_pct"] for p in picks), 1),
        "duplication_risk": duplication_risk,
        "primary_stack": primary["type"] if primary else None,
        "primary_team": primary_team,
        "secondary_teams": secondary_team_names,
        "has_bringback": has_bringback,
        "players": [
            {
                "id": p["id"],
                "name": p["name"],
                "team": p["team"],
                "opponent": p.get("opponent"),
                "position": p.get("position"),
                "salary": p["salary"],
                "projected_fpts": p["projected_fpts"],
                "ownership_pct": p["ownership_pct"],
            }
            for p in picks
        ],
        "player_ids": frozenset(used_ids),
    }


def generate_field(
    slate: dict[str, Any],
    sample_size: int,
    *,
    max_attempts_per_lineup: int = 25,
    min_salary: int = DEFAULT_MIN_SALARY,
    max_salary: int = SALARY_CAP,
    projection_source: str = "rotowire",
    included_game_pks: list[Any] | None = None,
    seed: int | None = None,
    field_sharpness: str = "marquee",
    running_qb_ids: frozenset[str] = frozenset(),
    pass_catching_rb_ids: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """
    Build `sample_size` synthetic opponent lineups, weighted toward
    whatever RotoWire's ownership% says the public actually rosters --
    see contest.py's generate_field() for the full rationale, which
    applies unchanged here. `field_sharpness`: see FIELD_SHARPNESS_LEVELS.

    Each lineup is built toward a real, weighted NFL stack archetype
    (see the module docstring) -- `running_qb_ids`/`pass_catching_rb_ids`
    (from _classify_pool(), computed once by the caller from real
    season game logs) decide which QBs a "naked" primary stack can use
    and which RBs count as pass-catchers for a QB stack's pass-catcher
    slots. Left empty (the default), qb_naked is simply never chosen
    and no RB counts as a pass-catcher -- a safe default for callers
    that don't have classification data on hand.
    """
    if sample_size < 1:
        raise ContestError("sample_size must be at least 1.")
    # MAX_USER_LINEUPS, not MAX_SAMPLE_SIZE: this builds the contest
    # itself now (build_contest_lineups), not only a sample for the
    # simulator to rank against. MAX_SAMPLE_SIZE stays the cap on how
    # much of a batch gets SIMULATED, which is a different question.
    if sample_size > MAX_USER_LINEUPS:
        raise ContestError(f"sample_size can't exceed {MAX_USER_LINEUPS:,}.")
    if field_sharpness not in FIELD_SHARPNESS_LEVELS:
        raise ContestError(
            f"Unknown field_sharpness '{field_sharpness}'. Choose one of: "
            f"{', '.join(FIELD_SHARPNESS_LEVELS)}."
        )
    _validate_salary_range(min_salary, max_salary)

    candidates_by_slot, slot_order = _build_candidate_pool(
        slate, included_game_pks=included_game_pks, projection_source=projection_source
    )
    field_weight_fn = _field_weight_fn(field_sharpness)

    rng = random.Random(seed)
    field: list[dict[str, Any]] = []
    for _ in range(sample_size):
        lineup = None
        # Each stack plan still gets its own dedicated retries, but a
        # plan that exhausts them re-rolls to a different one and then
        # to an unconstrained build, instead of costing the sample a
        # lineup. Some plans are genuinely infeasible under a salary
        # floor (measured: certain plans failed all 30 attempts), and
        # without this the whole field came up short.
        plans = [
            _pick_stack_plan(candidates_by_slot, running_qb_ids, field_weight_fn, rng),
            _pick_stack_plan(candidates_by_slot, running_qb_ids, field_weight_fn, rng),
            (None, None),
        ]
        for plan_primary, plan_secondary in plans:
            for _ in range(max_attempts_per_lineup):
                lineup = _sample_one_lineup(
                    candidates_by_slot, slot_order, rng, field_weight_fn,
                    min_salary=min_salary, max_salary=max_salary,
                    primary=plan_primary, pass_catching_rb_ids=pass_catching_rb_ids,
                    secondary_teams=list(plan_secondary) if plan_secondary else None,
                )
                if lineup is not None:
                    break
            if lineup is not None:
                break
        if lineup is None and field:
            # Starved pool: a real field converges onto the same few
            # builds when legal lineups are rare, so duplicate an
            # existing one rather than leaving a hole in the sample.
            source = rng.choices(
                field, weights=[max(lu["total_ownership_pct"], 0.1) for lu in field], k=1
            )[0]
            lineup = {
                **{k: v for k, v in source.items() if k != "duplicate_count"},
                "players": [dict(pl) for pl in source["players"]],
            }
        if lineup is not None:
            field.append(lineup)

    if not field:
        raise ContestError(
            "Couldn't build any legal field lineups from this pool -- the salary "
            "cap or slot requirements may be too tight for the players available."
        )
    _attach_duplicate_counts(field)
    return field


def generate_entries(
    slate: dict[str, Any],
    num_lineups: int,
    *,
    max_exposure_pct: float | None = None,
    max_attempts_per_lineup: int = 30,
    min_salary: int = DEFAULT_MIN_SALARY,
    max_salary: int = SALARY_CAP,
    projection_source: str = "rotowire",
    included_game_pks: list[Any] | None = None,
    allow_duplicates: bool = False,
    max_duplication_risk: float | None = None,
    seed: int | None = None,
    running_qb_ids: frozenset[str] = frozenset(),
    pass_catching_rb_ids: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """
    Build up to `num_lineups` of the user's OWN entries -- see
    contest.py's generate_entries() for the full rationale (fast
    randomized construction weighted toward projected points,
    exposure-capped, distinct by default unless allow_duplicates),
    which applies unchanged here. Returns as many legal entries as the
    pool/exposure cap could support rather than failing the whole
    request short of the count; only an empty result raises.

    Same stack-archetype machinery as generate_field() -- see its own
    docstring for what `running_qb_ids`/`pass_catching_rb_ids` do.
    """
    if num_lineups < 1:
        raise ContestError("num_lineups must be at least 1.")
    if num_lineups > MAX_USER_LINEUPS:
        raise ContestError(f"num_lineups can't exceed {MAX_USER_LINEUPS:,}.")
    _validate_salary_range(min_salary, max_salary)

    candidates_by_slot, slot_order = _build_candidate_pool(
        slate, included_game_pks=included_game_pks, projection_source=projection_source
    )

    def _cap_to_count(pct: float) -> int:
        return max(1, round(pct / 100 * num_lineups))

    rng = random.Random(seed)
    exposure_count: dict[str, int] = {}
    capped_ids: set[str] = set()
    seen_signatures: set[frozenset[str]] = set()
    entries: list[dict[str, Any]] = []
    # Same two-phase behavior as MLB's generate_entries: once the
    # DISTINCT lineup space is exhausted, fill the rest of the batch
    # with duplicates the way a real contest field does, rather than
    # stopping short. Duplicates still respect every real constraint
    # (salary, exposure caps); only distinctness is lifted.
    duplicates_unlocked = allow_duplicates

    for _ in range(num_lineups):
        lineup = None
        legal_duplicate = None
        # Two stack plans then an unconstrained build, each with its own
        # dedicated retries -- a plan that's infeasible under the salary
        # floor re-rolls instead of ending the batch (measured: a single
        # unlucky plan used to stop a 300-entry request at 5).
        plans = [
            _pick_stack_plan(candidates_by_slot, running_qb_ids, _fpts_weight, rng),
            _pick_stack_plan(candidates_by_slot, running_qb_ids, _fpts_weight, rng),
            (None, None),
        ]
        for plan_primary, plan_secondary in plans:
            for _ in range(max_attempts_per_lineup):
                candidate = _sample_one_lineup(
                    candidates_by_slot, slot_order, rng, _fpts_weight,
                    excluded_ids=frozenset(capped_ids),
                    min_salary=min_salary, max_salary=max_salary,
                    primary=plan_primary, pass_catching_rb_ids=pass_catching_rb_ids,
                    secondary_teams=list(plan_secondary) if plan_secondary else None,
                    max_duplication_risk=max_duplication_risk,
                )
                if candidate is None:
                    continue
                if not duplicates_unlocked and candidate["player_ids"] in seen_signatures:
                    legal_duplicate = candidate
                    continue
                lineup = candidate
                break
            if lineup is not None:
                break
        if lineup is None and legal_duplicate is not None:
            duplicates_unlocked = True
            lineup = legal_duplicate
        if lineup is None and entries:
            # Same starved-pool fallback MLB's generator uses: duplicate
            # an already-built entry (weighted toward the stronger ones)
            # rather than abandoning the rest of the contest. Exposure
            # caps stay honored -- only entries with no capped player
            # qualify, and if none do the cap is genuinely binding.
            eligible = [
                e for e in entries
                if not any(pl["id"] in capped_ids for pl in e["players"])
            ]
            if eligible:
                duplicates_unlocked = True
                source = rng.choices(
                    eligible, weights=[e["projected_points"] for e in eligible], k=1
                )[0]
                lineup = {
                    **{k: v for k, v in source.items() if k != "duplicate_count"},
                    "players": [dict(pl) for pl in source["players"]],
                    "player_ids": frozenset(pl["id"] for pl in source["players"]),
                }
        if lineup is None:
            break  # infeasible even with duplicates -- salary/exposure bound

        seen_signatures.add(lineup.pop("player_ids"))
        entries.append(lineup)

        for p in lineup["players"]:
            exposure_count[p["id"]] = exposure_count.get(p["id"], 0) + 1
            if max_exposure_pct is not None and exposure_count[p["id"]] >= _cap_to_count(max_exposure_pct):
                capped_ids.add(p["id"])

    if not entries:
        raise ContestError(
            "Couldn't build any legal entries from this pool -- the salary cap, "
            "slot requirements, or exposure cap may be too tight for the players available."
        )

    _attach_duplicate_counts(entries)
    return entries


def _build_contest_and_entries(
    slate: dict[str, Any],
    contest_type: str,
    num_lineups: int,
    *,
    max_exposure_pct: float | None,
    field_size: int | None,
    min_salary: int = DEFAULT_MIN_SALARY,
    max_salary: int = SALARY_CAP,
    projection_source: str = "rotowire",
    included_game_pks: list[Any] | None = None,
    allow_duplicates: bool = False,
    max_duplication_risk: float | None = None,
    seed: int | None,
    running_qb_ids: frozenset[str] = frozenset(),
    pass_catching_rb_ids: frozenset[str] = frozenset(),
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Shared setup: validate the contest type/size and build the
    user's own entries. Split out so self-play mode can skip sampling
    an opponent field entirely."""
    if contest_type not in CONTEST_TYPES:
        raise ContestError(
            f"Unknown contest_type '{contest_type}'. Choose one of: {', '.join(CONTEST_TYPES)}."
        )
    contest = dict(CONTEST_TYPES[contest_type])
    if field_size is not None:
        if not (1 <= field_size <= MAX_FIELD_SIZE):
            raise ContestError(f"field_size must be between 1 and {MAX_FIELD_SIZE}.")
        contest["field_size"] = field_size

    if num_lineups > contest["field_size"]:
        raise ContestError(
            f"num_lineups ({num_lineups:,}) can't exceed the contest's field_size "
            f"({contest['field_size']:,}) -- your own entries are part of the field, not "
            "additional to it. Pick a bigger contest, override field_size, or lower num_lineups."
        )

    entries = generate_entries(
        slate, num_lineups,
        max_exposure_pct=max_exposure_pct,
        min_salary=min_salary, max_salary=max_salary,
        projection_source=projection_source,
        included_game_pks=included_game_pks,
        allow_duplicates=allow_duplicates,
        max_duplication_risk=max_duplication_risk,
        seed=seed,
        running_qb_ids=running_qb_ids, pass_catching_rb_ids=pass_catching_rb_ids,
    )
    return contest, entries


def _build_entries_and_field(
    slate: dict[str, Any],
    contest_type: str,
    num_lineups: int,
    *,
    max_exposure_pct: float | None,
    field_size: int | None,
    sample_size: int | None,
    min_salary: int = DEFAULT_MIN_SALARY,
    max_salary: int = SALARY_CAP,
    projection_source: str = "rotowire",
    included_game_pks: list[Any] | None = None,
    allow_duplicates: bool = False,
    max_duplication_risk: float | None = None,
    seed: int | None,
    field_sharpness: str = "marquee",
    running_qb_ids: frozenset[str] = frozenset(),
    pass_catching_rb_ids: frozenset[str] = frozenset(),
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Shared setup for the default (vs-a-separate-field) mode: build
    the user's own entries and sample an opponent field to rank them
    against."""
    contest, entries = _build_contest_and_entries(
        slate, contest_type, num_lineups,
        max_exposure_pct=max_exposure_pct, field_size=field_size,
        min_salary=min_salary, max_salary=max_salary,
        projection_source=projection_source,
        included_game_pks=included_game_pks,
        allow_duplicates=allow_duplicates,
        max_duplication_risk=max_duplication_risk,
        seed=seed,
        running_qb_ids=running_qb_ids, pass_catching_rb_ids=pass_catching_rb_ids,
    )

    field_sample = sample_size or min(contest["field_size"], MAX_SAMPLE_SIZE)
    field = generate_field(
        slate, field_sample,
        min_salary=min_salary, max_salary=max_salary,
        projection_source=projection_source,
        included_game_pks=included_game_pks,
        seed=(seed + 1) if seed is not None else None,
        field_sharpness=field_sharpness,
        running_qb_ids=running_qb_ids, pass_catching_rb_ids=pass_catching_rb_ids,
    )
    return contest, entries, field


async def build_contest_lineups(
    slate: dict[str, Any],
    contest_type: str,
    contest_size: int,
    *,
    season: int,
    seed: int | None = None,
    field_sharpness: str = "marquee",
    projection_source: str = "rotowire",
    included_game_pks: list[Any] | None = None,
) -> dict[str, Any]:
    """
    Build a whole NFL CONTEST -- and nothing else. The generator half of
    the generator/simulator split, mirroring contest.build_contest_lineups()
    on the MLB side (see its docstring for the full rationale).

    `contest_size` is the single size control: it IS the contest's field
    size and it IS how many lineups get built, because the generator
    builds a contest rather than a handful of entries to drop into
    someone else's. Above MAX_USER_LINEUPS the build is capped and the
    response reports `num_entries_built` alongside `field_size` rather
    than conflating them.

    No salary floor, no exposure cap, and duplicates always allowed: a
    floor makes whole stack archetypes infeasible and stalls a batch,
    the honest way to spend the cap is to steer the sampler while it
    builds (_SALARY_PACING_STRENGTH), and a real contest field genuinely
    contains duplicates.
    """
    if contest_type not in CONTEST_TYPES:
        raise ContestError(
            f"Unknown contest_type '{contest_type}'. Choose one of: {', '.join(CONTEST_TYPES)}."
        )
    if not (1 <= contest_size <= MAX_FIELD_SIZE):
        raise ContestError(f"contest_size must be between 1 and {MAX_FIELD_SIZE:,}.")

    contest = dict(CONTEST_TYPES[contest_type])
    contest["field_size"] = contest_size
    num_lineups = min(contest_size, MAX_USER_LINEUPS)

    candidates_by_slot, _ = _build_candidate_pool(
        slate, included_game_pks=included_game_pks, projection_source=projection_source
    )
    running_qb_ids, pass_catching_rb_ids = await _classify_pool(candidates_by_slot, season)

    # generate_field, not generate_entries. The two are different
    # models: generate_entries weights by projected points and exists to
    # build lineups YOU would enter; generate_field weights by ownership
    # and builds the lineups the public actually enters. This function
    # claims to build a contest, so it has to use the second -- the same
    # correction MLB's generator got.
    entries = generate_field(
        slate, num_lineups,
        min_salary=0, max_salary=SALARY_CAP,
        projection_source=projection_source,
        included_game_pks=included_game_pks,
        seed=seed, field_sharpness=field_sharpness,
        running_qb_ids=running_qb_ids, pass_catching_rb_ids=pass_catching_rb_ids,
    )

    salaries = sorted(e["salary_used"] for e in entries)
    points = [e["projected_points"] for e in entries]
    shape_counts = Counter(e.get("primary_stack") or "none" for e in entries)
    return {
        "contest_type": contest_type,
        "contest": contest,
        "field_size": contest_size,
        "num_entries_requested": num_lineups,
        "num_entries_built": len(entries),
        # What the opponents were built to look like, recorded so the
        # simulator and the UI report the field that actually exists.
        "field_sharpness": field_sharpness,
        "num_distinct_entries": len(
            {frozenset(pl["id"] for pl in e["players"]) for e in entries}
        ),
        "summary": {
            "avg_salary_used": round(sum(salaries) / len(salaries)),
            "median_salary_used": salaries[len(salaries) // 2],
            "min_salary_used": salaries[0],
            "max_salary_used": salaries[-1],
            "avg_projected_points": round(sum(points) / len(points), 2),
            "min_projected_points": min(points),
            "max_projected_points": max(points),
            "avg_total_ownership_pct": round(
                sum(e["total_ownership_pct"] for e in entries) / len(entries), 1
            ),
        },
        # Which stack archetypes the contest actually came out with,
        # most common first -- the real "does this look like a contest"
        # check no single average can answer.
        "stack_shapes": [
            {"shape": shape, "count": n, "pct": round(100 * n / len(entries), 1)}
            for shape, n in shape_counts.most_common()
        ],
        "exposure": field_exposure(entries, top_n=20),
        "entries": entries,
        "note": (
            "Lineups only -- no simulation has been run yet. Each is built by fast "
            "randomized construction weighted toward projected points and toward "
            "spending the salary cap, across the real NFL GPP stack archetypes, "
            "deliberately allowing the duplicates a real contest field contains. "
            "Send the batch to the simulator for cash probability, payouts and ROI."
        ),
    }


async def build_contest_entries(
    slate: dict[str, Any],
    contest_type: str,
    num_lineups: int,
    *,
    season: int,
    max_exposure_pct: float | None = None,
    field_size: int | None = None,
    sample_size: int | None = None,
    projection_source: str = "rotowire",
    included_game_pks: list[Any] | None = None,
    min_salary: int = DEFAULT_MIN_SALARY,
    max_salary: int = SALARY_CAP,
    allow_duplicates: bool = False,
    max_duplication_risk: float | None = None,
    seed: int | None = None,
    field_sharpness: str = "marquee",
) -> dict[str, Any]:
    """
    The deterministic contest generator: build up to `num_lineups` of
    the user's own entries for a named contest type, ranked against a
    simulated opponent field's *projected* points -- see
    build_contest_entries_simulated() for the real Monte Carlo
    alternative. Mirrors contest.py's build_contest_entries() output
    shape. Now async -- classifying real running QBs/pass-catching RBs
    (see _classify_pool()) needs one real fetch of `season`'s game logs
    before any lineup can be built toward the stack archetypes.
    """
    candidates_by_slot, _ = _build_candidate_pool(
        slate, included_game_pks=included_game_pks, projection_source=projection_source
    )
    running_qb_ids, pass_catching_rb_ids = await _classify_pool(candidates_by_slot, season)

    contest, entries, field = _build_entries_and_field(
        slate, contest_type, num_lineups,
        max_exposure_pct=max_exposure_pct, field_size=field_size, sample_size=sample_size,
        min_salary=min_salary, max_salary=max_salary,
        projection_source=projection_source,
        included_game_pks=included_game_pks,
        allow_duplicates=allow_duplicates,
        max_duplication_risk=max_duplication_risk,
        seed=seed, field_sharpness=field_sharpness,
        running_qb_ids=running_qb_ids, pass_catching_rb_ids=pass_catching_rb_ids,
    )
    evaluation = _evaluate_batch_against_field(entries, field, contest)

    cashing = [r for r in evaluation["results"] if r["in_the_money"]]
    total_payout = round(sum(r["estimated_payout"] for r in evaluation["results"]), 2)
    total_cost = round(len(entries) * contest["entry_fee"], 2)
    points = [e["projected_points"] for e in entries]

    return {
        "contest_type": contest_type,
        "contest": contest,
        "num_entries_requested": num_lineups,
        "num_entries_built": len(entries),
        "field_size": evaluation["field_size"],
        "sample_size": evaluation["sample_size"],
        "paid_count": evaluation["paid_count"],
        "prize_pool": evaluation["prize_pool"],
        "summary": {
            "cashing_count": len(cashing),
            "cashing_pct": round(100 * len(cashing) / len(entries), 1),
            "total_entry_cost": total_cost,
            "total_estimated_payout": total_payout,
            "estimated_net_profit": round(total_payout - total_cost, 2),
            "avg_roi_pct": round((total_payout / total_cost - 1) * 100, 1) if total_cost else 0.0,
            "avg_salary_used": round(sum(e["salary_used"] for e in entries) / len(entries)),
            "avg_projected_points": round(sum(points) / len(points), 2),
            "min_projected_points": min(points),
            "max_projected_points": max(points),
            "avg_total_ownership_pct": round(
                sum(e["total_ownership_pct"] for e in entries) / len(entries), 1
            ),
        },
        "field_baseline": _field_baseline(
            contest["payout_pct"], evaluation["prize_pool"], contest["entry_fee"], evaluation["field_size"]
        ),
        "exposure": field_exposure(entries, top_n=20),
        "field_sharpness": field_sharpness,
        "entries": entries,
        "results": evaluation["results"],
        "note": (
            "Entries are built by fast randomized construction weighted toward "
            "projected points, not an exact solve -- individually strong and "
            "mutually distinct, not guaranteed optimal. Cash rate and payout are "
            "projected-points estimates against a sampled opponent field, not "
            "simulated real-world outcomes."
        ),
    }


async def _simulate_lineups_structural(
    lineups: list[dict[str, Any]],
    slate: dict[str, Any],
    season: int,
    *,
    num_trials: int,
    seed: int | None,
) -> np.ndarray:
    """
    The `engine="structural"` counterpart to nfl_variance's bootstrap
    pools: builds each lineup's per-trial total by summing
    nfl_structural.simulate_slate_trials()'s simulated game results for
    its own players.

    Correlation is already baked into those arrays by construction --
    teammates divide one team's volume, a DST scores off the opponent's
    own draw in the same simulated game -- so unlike the bootstrap path
    there is no separate team-multiplier step. Returns the same
    (len(lineups), num_trials) shape simulate_batch does, so every
    ranking and payout calculation downstream is unchanged.
    """
    trials = await nfl_structural.simulate_slate_trials(
        slate, season, num_trials=num_trials, seed=seed
    )
    if not trials:
        raise ContestError(
            "The structural engine needs Vegas lines for the slate's games -- none of "
            "this week's games have an implied total yet."
        )

    missing = sorted({
        p["id"] for lineup in lineups for p in lineup["players"] if p["id"] not in trials
    })
    if missing:
        preview = ", ".join(str(m) for m in missing[:5])
        suffix = "..." if len(missing) > 5 else ""
        raise ContestError(
            f"The structural engine has no simulated outcome for player id(s) {preview}"
            f"{suffix} -- they are in a lineup but not on the simulated slate."
        )

    out = np.empty((len(lineups), num_trials), dtype=float)
    for i, lineup in enumerate(lineups):
        out[i] = np.sum([trials[p["id"]] for p in lineup["players"]], axis=0)
    return out


async def evaluate_batch_simulated(
    entries: list[dict[str, Any]],
    field: list[dict[str, Any]],
    contest: dict[str, Any],
    *,
    season: int,
    num_trials: int = 2000,
    seed: int | None = None,
    first_place_pct: float | None = None,
    engine: str = "bootstrap",
    slate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Ranks `entries` against nfl_variance.simulate_batch()'s real Monte
    Carlo outcome distribution instead of a single projected-points
    snapshot -- entries and field are simulated TOGETHER, trial by
    trial. See contest.py's evaluate_batch_simulated() for the full
    "distinct ranks within one trial" rationale, which applies
    unchanged here (only the simulation source differs: nfl_variance
    instead of variance.py).

    `engine="structural"` swaps the bootstrap pools for the layer-2 plus
    layer-3 simulator (nfl_structural.py), which draws each game from
    its market-implied totals and then allocates that volume among the
    players, so correlation is produced rather than assumed. It needs
    `slate` -- the full nfl_slate.build_slate() output, not just player
    ids -- and every game needs a Vegas line. Opt-in: two of its
    constants are still unfitted (see nfl_structural's own docstring).
    """
    if not entries:
        raise ContestError("Need at least one entry to simulate.")
    if not field:
        raise ContestError("Need at least one field lineup to simulate against.")

    field_size = contest["field_size"]
    entry_fee = contest["entry_fee"]
    paid_count = max(1, round(field_size * contest["payout_pct"]))
    prize_pool = contest.get("prize_pool") or round(field_size * entry_fee * (1 - RAKE_PCT), 2)

    payouts = np.array(
        _custom_payout_curve(
            paid_count, prize_pool, contest["shape"],
            first_place_pct if first_place_pct is not None else contest.get("first_place_pct"),
        )
    )
    full_payouts = np.zeros(field_size)
    full_payouts[:paid_count] = payouts

    num_entries = len(entries)
    sample_size = len(field)
    beaten_range = np.arange(sample_size + 1)
    ranks_for_beaten = np.clip(
        np.round((1 - beaten_range / sample_size) * field_size), 1, field_size
    ).astype(np.int64)
    smoothing_boundaries = np.unique(ranks_for_beaten)
    smoothed_payouts = _block_average_payouts(full_payouts, smoothing_boundaries, field_size)

    if engine == "structural":
        if slate is None:
            raise ContestError("engine='structural' requires the full slate.")
        sim = await _simulate_lineups_structural(
            entries + field, slate, season, num_trials=num_trials, seed=seed
        )
    else:
        player_pools = await nfl_variance.player_pools_for_entries(entries + field, season)
        sim = nfl_variance.simulate_batch(
            entries + field, player_pools, num_trials=num_trials, seed=seed
        )
    entry_sim, field_sim = sim[:num_entries], sim[num_entries:]

    field_sorted = np.sort(field_sim, axis=0)
    beaten = np.empty((num_entries, num_trials), dtype=np.int64)
    for t in range(num_trials):
        beaten[:, t] = np.searchsorted(field_sorted[:, t], entry_sim[:, t], side="left")
    percentile_rank = np.clip(np.round((1 - beaten / sample_size) * field_size), 1, field_size).astype(
        np.int64
    )

    order = np.argsort(-entry_sim, axis=0)
    sorted_pct_rank = np.take_along_axis(percentile_rank, order, axis=0)
    positions = np.arange(num_entries)[:, None]
    distinct_rank_sorted = np.minimum(
        np.maximum.accumulate(sorted_pct_rank - positions, axis=0) + positions, field_size
    )
    final_rank = np.empty_like(distinct_rank_sorted)
    np.put_along_axis(final_rank, order, distinct_rank_sorted, axis=0)

    in_the_money = final_rank <= paid_count
    payout_per_trial = smoothed_payouts[final_rank - 1]

    top_1pct_threshold = max(1, round(0.01 * field_size))
    top_10pct_threshold = max(1, round(0.10 * field_size))
    first_place = final_rank == 1
    top_1pct = final_rank <= top_1pct_threshold
    top_10pct = final_rank <= top_10pct_threshold

    results = []
    for i in range(num_entries):
        row = entry_sim[i]
        payout_row = payout_per_trial[i]
        expected_payout = float(payout_row.mean())
        results.append(
            {
                "lineup_index": i,
                "cash_probability_pct": round(float(in_the_money[i].mean()) * 100, 1),
                "first_place_pct": round(float(first_place[i].mean()) * 100, 2),
                "top_1pct_pct": round(float(top_1pct[i].mean()) * 100, 2),
                "top_10pct_pct": round(float(top_10pct[i].mean()) * 100, 2),
                "expected_payout": round(expected_payout, 2),
                "payout_p10": round(float(np.percentile(payout_row, 10)), 2),
                "payout_p90": round(float(np.percentile(payout_row, 90)), 2),
                "roi_pct": round((expected_payout - entry_fee) / entry_fee * 100, 1) if entry_fee else 0.0,
                "simulated_points_mean": round(float(row.mean()), 2),
                "simulated_points_p10": round(float(np.percentile(row, 10)), 2),
                "simulated_points_p90": round(float(np.percentile(row, 90)), 2),
                "simulated_points_floor": round(float(np.percentile(row, FLOOR_CEILING_PERCENTILE)), 2),
                "simulated_points_ceiling": round(float(np.percentile(row, 100 - FLOOR_CEILING_PERCENTILE)), 2),
            }
        )

    _split_duplicate_payouts(
        entries, results,
        ["cash_probability_pct", "first_place_pct", "top_1pct_pct", "top_10pct_pct",
         "expected_payout", "payout_p10", "payout_p90", "roi_pct"],
    )

    return {
        "field_size": field_size, "sample_size": sample_size, "paid_count": paid_count,
        "entry_fee": entry_fee, "prize_pool": prize_pool, "num_trials": num_trials, "results": results,
    }


async def evaluate_field_mirrored(
    field_lineups: list[dict[str, Any]],
    contest: dict[str, Any],
    *,
    season: int,
    num_trials: int = 10_000,
    seed: int | None = None,
    first_place_pct: float | None = None,
    engine: str = "bootstrap",
    slate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Self-play: `field_lineups` (your own batch) as one self-contained
    population, every lineup ranked against every OTHER lineup in the
    same simulated trial -- see contest.py's evaluate_field_mirrored()
    for the full rationale, which applies unchanged here.
    """
    if not field_lineups:
        raise ContestError("Need at least one lineup to simulate.")

    field_size = contest["field_size"]
    entry_fee = contest["entry_fee"]
    sample_size = len(field_lineups)
    if sample_size > field_size:
        raise ContestError(
            f"Can't simulate {sample_size:,} lineups against a field_size of only {field_size:,}."
        )
    paid_count = max(1, round(field_size * contest["payout_pct"]))
    prize_pool = contest.get("prize_pool") or round(field_size * entry_fee * (1 - RAKE_PCT), 2)

    payouts = np.array(
        _custom_payout_curve(
            paid_count, prize_pool, contest["shape"],
            first_place_pct if first_place_pct is not None else contest.get("first_place_pct"),
        )
    )
    full_payouts = np.zeros(field_size)
    full_payouts[:paid_count] = payouts

    if sample_size > 1:
        real_ranks_by_k = 1 + np.floor(
            np.arange(sample_size) * (field_size - 1) / (sample_size - 1)
        ).astype(np.int64)
    else:
        real_ranks_by_k = np.array([1])
    smoothed_payouts = _block_average_payouts(full_payouts, real_ranks_by_k, field_size)

    if engine == "structural":
        if slate is None:
            raise ContestError("engine='structural' requires the full slate.")
        sim = await _simulate_lineups_structural(
            field_lineups, slate, season, num_trials=num_trials, seed=seed
        )
    else:
        player_pools = await nfl_variance.player_pools_for_entries(field_lineups, season)
        sim = nfl_variance.simulate_batch(
            field_lineups, player_pools, num_trials=num_trials, seed=seed
        )

    order = np.argsort(-sim, axis=0)
    final_rank = np.empty_like(order)
    np.put_along_axis(final_rank, order, real_ranks_by_k[:, None], axis=0)

    top_1pct_threshold = max(1, round(0.01 * field_size))
    top_10pct_threshold = max(1, round(0.10 * field_size))
    in_the_money = final_rank <= paid_count
    first_place = final_rank == 1
    top_1pct = final_rank <= top_1pct_threshold
    top_10pct = final_rank <= top_10pct_threshold
    payout_per_trial = smoothed_payouts[final_rank - 1]

    results = []
    for i in range(sample_size):
        row = sim[i]
        payout_row = payout_per_trial[i]
        expected_payout = float(payout_row.mean())
        results.append(
            {
                "lineup_index": i,
                "cash_probability_pct": round(float(in_the_money[i].mean()) * 100, 1),
                "first_place_pct": round(float(first_place[i].mean()) * 100, 2),
                "top_1pct_pct": round(float(top_1pct[i].mean()) * 100, 2),
                "top_10pct_pct": round(float(top_10pct[i].mean()) * 100, 2),
                "expected_payout": round(expected_payout, 2),
                "payout_p10": round(float(np.percentile(payout_row, 10)), 2),
                "payout_p90": round(float(np.percentile(payout_row, 90)), 2),
                "roi_pct": round((expected_payout - entry_fee) / entry_fee * 100, 1) if entry_fee else 0.0,
                "simulated_points_mean": round(float(row.mean()), 2),
                "simulated_points_p10": round(float(np.percentile(row, 10)), 2),
                "simulated_points_p90": round(float(np.percentile(row, 90)), 2),
                "simulated_points_floor": round(float(np.percentile(row, FLOOR_CEILING_PERCENTILE)), 2),
                "simulated_points_ceiling": round(float(np.percentile(row, 100 - FLOOR_CEILING_PERCENTILE)), 2),
            }
        )

    _split_duplicate_payouts(
        field_lineups, results,
        ["cash_probability_pct", "first_place_pct", "top_1pct_pct", "top_10pct_pct",
         "expected_payout", "payout_p10", "payout_p90", "roi_pct"],
    )

    return {
        "field_size": field_size, "sample_size": sample_size, "paid_count": paid_count,
        "entry_fee": entry_fee, "prize_pool": prize_pool, "num_trials": num_trials, "results": results,
    }


async def build_contest_entries_simulated(
    slate: dict[str, Any],
    contest_type: str,
    num_lineups: int,
    *,
    season: int,
    num_trials: int = 2000,
    max_exposure_pct: float | None = None,
    field_size: int | None = None,
    sample_size: int | None = None,
    min_salary: int = DEFAULT_MIN_SALARY,
    max_salary: int = SALARY_CAP,
    allow_duplicates: bool = False,
    seed: int | None = None,
    self_play: bool = False,
    field_sharpness: str = "marquee",
    first_place_pct: float | None = None,
    engine: str = "bootstrap",
) -> dict[str, Any]:
    """
    Like build_contest_entries, but ranks the batch against a real
    Monte Carlo simulation. `self_play` picks between the two real
    questions contest.py's own version answers (see its docstring for
    the full rationale, which applies unchanged here): False (default)
    ranks against a separately-sampled realistic public field; True
    ranks the batch against itself. `season` is which year's real game
    logs nfl_variance draws outcome pools from -- pass nfl.PRIOR_SEASON
    from the caller until the season being drafted has its own
    in-progress data worth using instead.
    """
    candidates_by_slot, _ = _build_candidate_pool(slate)
    running_qb_ids, pass_catching_rb_ids = await _classify_pool(candidates_by_slot, season)

    if self_play:
        contest, entries = _build_contest_and_entries(
            slate, contest_type, num_lineups,
            max_exposure_pct=max_exposure_pct, field_size=field_size,
            min_salary=min_salary, max_salary=max_salary,
            allow_duplicates=allow_duplicates, seed=seed,
            running_qb_ids=running_qb_ids, pass_catching_rb_ids=pass_catching_rb_ids,
        )
        evaluation = await evaluate_field_mirrored(
            entries, contest, season=season, num_trials=num_trials, seed=seed,
            first_place_pct=first_place_pct, engine=engine, slate=slate,
        )
    else:
        contest, entries, field = _build_entries_and_field(
            slate, contest_type, num_lineups,
            max_exposure_pct=max_exposure_pct, field_size=field_size, sample_size=sample_size,
            min_salary=min_salary, max_salary=max_salary,
            allow_duplicates=allow_duplicates, seed=seed, field_sharpness=field_sharpness,
            running_qb_ids=running_qb_ids, pass_catching_rb_ids=pass_catching_rb_ids,
        )
        evaluation = await evaluate_batch_simulated(
            entries, field, contest, season=season, num_trials=num_trials,
            seed=(seed + 2) if seed is not None else None,
            first_place_pct=first_place_pct, engine=engine, slate=slate,
        )

    return _rank_and_summarize_simulated(
        entries,
        evaluation,
        contest,
        contest_type=contest_type,
        num_requested=num_lineups,
        self_play=self_play,
        field_sharpness=field_sharpness,
        first_place_pct=first_place_pct,
    )


def _rank_and_summarize_simulated(
    entries: list[dict[str, Any]],
    evaluation: dict[str, Any],
    contest: dict[str, Any],
    *,
    contest_type: str,
    num_requested: int,
    self_play: bool,
    field_sharpness: str,
    first_place_pct: float | None,
) -> dict[str, Any]:
    """
    Shared tail of every simulated-contest path: sort the batch by how
    well it actually simulated, then roll the per-lineup results up into
    one batch summary.

    Split out so build_contest_entries_simulated() (build and simulate
    in one call) and simulate_contest_batch() (simulate a contest that
    was already built -- the generator/simulator hand-off) produce
    byte-for-byte the same response shape rather than two summaries that
    drift apart. Mirrors contest._rank_and_summarize_simulated() on the
    MLB side.
    """
    # Best entries first -- highest ROI down, with top-1% rate as the
    # tiebreak. Same ordering contest.py's MLB version uses, and the
    # same caveat applies: in a top-heavy GPP, per-lineup ROI is
    # dominated by rare first-place hits, so the top of an ROI-sorted
    # list is partly whichever lineups got lucky in THIS run's draws.
    # top_1pct_pct measures the same "can this build spike?" quality
    # from far more trial hits and stays as the tiebreak for that
    # reason; each row's own roi_se_pct shows how much of its ROI is
    # noise.
    order = sorted(
        range(len(entries)),
        key=lambda i: (
            -evaluation["results"][i]["roi_pct"],
            -evaluation["results"][i].get("top_1pct_pct", 0),
        ),
    )
    entries = [entries[i] for i in order]
    evaluation = {
        **evaluation,
        "results": [{**evaluation["results"][i], "lineup_index": new_i} for new_i, i in enumerate(order)],
    }

    cash_probs = [r["cash_probability_pct"] for r in evaluation["results"]]
    first_place_pcts = [r["first_place_pct"] for r in evaluation["results"]]
    top_1pct_pcts = [r["top_1pct_pct"] for r in evaluation["results"]]
    top_10pct_pcts = [r["top_10pct_pct"] for r in evaluation["results"]]
    roi_pcts = [r["roi_pct"] for r in evaluation["results"]]
    expected_payouts = [r["expected_payout"] for r in evaluation["results"]]
    total_cost = round(len(entries) * contest["entry_fee"], 2)
    total_expected_payout = round(sum(expected_payouts), 2)

    return {
        "contest_type": contest_type,
        "contest": contest,
        "num_entries_requested": num_requested,
        "num_entries_built": len(entries),
        "field_sharpness": field_sharpness,
        "field_size": evaluation["field_size"],
        "sample_size": evaluation["sample_size"],
        "paid_count": evaluation["paid_count"],
        "prize_pool": evaluation["prize_pool"],
        "num_trials": evaluation["num_trials"],
        "first_place_pct": first_place_pct if first_place_pct is not None else contest.get("first_place_pct"),
        "summary": {
            "avg_cash_probability_pct": round(sum(cash_probs) / len(cash_probs), 1),
            "avg_first_place_pct": round(sum(first_place_pcts) / len(first_place_pcts), 2),
            "avg_top_1pct_pct": round(sum(top_1pct_pcts) / len(top_1pct_pcts), 2),
            "avg_top_10pct_pct": round(sum(top_10pct_pcts) / len(top_10pct_pcts), 2),
            "avg_roi_pct": round(sum(roi_pcts) / len(roi_pcts), 1),
            "total_entry_cost": total_cost,
            "total_expected_payout": total_expected_payout,
            "estimated_net_profit": round(total_expected_payout - total_cost, 2),
        },
        "field_baseline": _field_baseline(
            contest["payout_pct"], evaluation["prize_pool"], contest["entry_fee"], evaluation["field_size"]
        ),
        "exposure": field_exposure(entries, top_n=20, results=evaluation["results"]),
        "entries": entries,
        "results": evaluation["results"],
        "self_play": self_play,
        "note": (
            f"Cash probability and expected payout come from {evaluation['num_trials']:,} "
            "real Monte Carlo simulated trials of this ENTIRE BATCH ranked against ITSELF -- "
            "every lineup competing against every other lineup you generated, in the same "
            "simulated trial, projected onto the real field_size."
            if self_play
            else
            f"Cash probability and expected payout come from {evaluation['num_trials']:,} "
            "real Monte Carlo simulated trials of each player's own historical (2025) "
            "outcome pool, with QB/pass-catcher and DST/opponent correlation, ranked against "
            "a separately-sampled realistic public field -- a genuine probability, not a "
            "single projected-points estimate against the field."
        ),
    }



async def simulate_contest_batch(
    entries: list[dict[str, Any]],
    contest: dict[str, Any],
    *,
    season: int,
    slate: dict[str, Any] | None = None,
    contest_type: str = "",
    num_trials: int = 2000,
    entry_fee: float | None = None,
    first_place_pct: float | None = None,
    self_play: bool = True,
    field_sharpness: str = "marquee",
    seed: int | None = None,
    engine: str = "bootstrap",
) -> dict[str, Any]:
    """
    Simulate an NFL contest that has ALREADY been built -- the simulator
    half of the generator/simulator split, mirroring
    contest.simulate_contest_batch() on the MLB side.

    `entry_fee`, if given, replaces the contest preset's own. It sets
    the prize pool (field_size x entry_fee, less rake), so it drives
    every payout and therefore every ROI in the result -- which is why
    it's a real simulator input rather than a fixed property of the
    preset.

    `self_play=True` (the default) ranks the contest against ITSELF: the
    generator builds the whole contest, so the batch IS the field and
    there's no second population to invent. `self_play=False` ranks it
    against a separately-sampled, ownership-weighted public field
    instead, which answers the different question of how these lineups
    would fare against real public rosters -- that mode needs `slate` to
    sample the field from.

    A batch bigger than MAX_SAMPLE_SIZE is simulated as a
    MAX_SAMPLE_SIZE-lineup slice of itself, projected back onto the real
    field size. Entries come out of the generator in build order (no
    ranking applied yet), so a leading slice is an unbiased sample.
    """
    if not entries:
        raise ContestError("Nothing to simulate -- build a contest first.")

    contest = dict(contest)
    if entry_fee is not None:
        if entry_fee < 0:
            raise ContestError("entry_fee can't be negative.")
        contest["entry_fee"] = float(entry_fee)
        # Any prize pool carried on the contest was derived from its own
        # entry fee, so a new fee invalidates it.
        contest.pop("prize_pool", None)

    num_requested = len(entries)
    simulated = entries[:MAX_SAMPLE_SIZE]

    if self_play:
        evaluation = await evaluate_field_mirrored(
            simulated, contest, season=season, num_trials=num_trials, seed=seed,
            first_place_pct=first_place_pct, engine=engine, slate=slate,
        )
    else:
        if slate is None:
            raise ContestError(
                "Ranking against a public field needs the slate to sample one from."
            )
        candidates_by_slot, _ = _build_candidate_pool(slate)
        running_qb_ids, pass_catching_rb_ids = await _classify_pool(candidates_by_slot, season)
        field = generate_field(
            slate, min(contest["field_size"], MAX_SAMPLE_SIZE),
            min_salary=0, max_salary=SALARY_CAP,
            seed=(seed + 1) if seed is not None else None,
            field_sharpness=field_sharpness,
            running_qb_ids=running_qb_ids, pass_catching_rb_ids=pass_catching_rb_ids,
        )
        evaluation = await evaluate_batch_simulated(
            simulated, field, contest, season=season, num_trials=num_trials,
            seed=(seed + 2) if seed is not None else None,
            first_place_pct=first_place_pct, engine=engine, slate=slate,
        )

    result = _rank_and_summarize_simulated(
        simulated,
        evaluation,
        contest,
        contest_type=contest_type,
        num_requested=num_requested,
        self_play=self_play,
        field_sharpness=field_sharpness,
        first_place_pct=first_place_pct,
    )
    # How much of the built batch actually got simulated -- equal to
    # num_entries_built at or under MAX_SAMPLE_SIZE, honestly smaller
    # above it.
    result["num_entries_simulated"] = len(simulated)
    return result


# ["QB", "RB1", "RB2", "WR1", "WR2", "WR3", "TE", "FLEX", "DST"] --
# numbered only for slots DK's roster needs more than one of. Mirrors
# lineup_export.py's SLOT_LABELS convention for MLB, built from NFL's
# own SLOT_REQUIREMENTS instead.
_NFL_SLOT_LABELS: list[str] = [
    f"{slot}{i + 1}" if count > 1 else slot
    for slot, count in SLOT_REQUIREMENTS.items()
    for i in range(count)
]

_SIMULATED_RESULT_FIELDS = [
    "cash_probability_pct", "first_place_pct", "top_1pct_pct", "top_10pct_pct",
    "expected_payout", "payout_p10", "payout_p90", "roi_pct",
    "simulated_points_mean", "simulated_points_p10", "simulated_points_p90",
    "simulated_points_floor", "simulated_points_ceiling",
]
_DETERMINISTIC_RESULT_FIELDS = ["estimated_rank", "in_the_money", "estimated_payout"]


def lineups_to_csv(entries: list[dict[str, Any]], *, results: list[dict[str, Any]] | None = None) -> str:
    """
    Serialize a batch of entries to CSV text, one row each -- the NFL
    sibling of lineup_export.lineups_to_csv() (MLB), adapted for the
    9-slot roster and this module's own primary/secondary stack fields
    (kept as real generation-time facts -- which team, which archetype
    -- rather than re-derived from roster composition the way MLB's
    stack_info() does, since NFL's stack model isn't "any team with 2+
    players," it's the specific primary/secondary plan actually built
    toward).

    `results`, if given, must be the same length as `entries` and
    index-aligned; auto-detects the deterministic vs. simulated shape
    the same way the MLB version does.
    """
    buf = io.StringIO()
    fieldnames = [
        "lineup_index", "salary_used", "projected_points", "total_ownership_pct", "duplicate_count",
        "primary_stack", "primary_team", "secondary_teams", "has_bringback",
    ]
    fieldnames += [f"{label}_name" for label in _NFL_SLOT_LABELS]
    simulated = bool(results) and "cash_probability_pct" in results[0]
    if results is not None:
        fieldnames += _SIMULATED_RESULT_FIELDS if simulated else _DETERMINISTIC_RESULT_FIELDS

    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()

    for i, entry in enumerate(entries):
        players = entry.get("players") or []
        row: dict[str, Any] = {
            "lineup_index": i,
            "salary_used": entry.get("salary_used"),
            "projected_points": entry.get("projected_points"),
            "total_ownership_pct": entry.get("total_ownership_pct"),
            "duplicate_count": entry.get("duplicate_count", 1),
            "primary_stack": entry.get("primary_stack") or "",
            "primary_team": entry.get("primary_team") or "",
            "secondary_teams": "+".join(entry.get("secondary_teams") or []),
            "has_bringback": entry.get("has_bringback", False),
        }
        for label, p in zip(_NFL_SLOT_LABELS, players):
            row[f"{label}_name"] = p.get("name")
        if results is not None:
            r = results[i]
            for field in _SIMULATED_RESULT_FIELDS if simulated else _DETERMINISTIC_RESULT_FIELDS:
                row[field] = r.get(field)
        writer.writerow(row)

    return buf.getvalue()
