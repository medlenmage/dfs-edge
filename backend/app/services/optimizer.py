"""
DraftKings Classic MLB lineup optimizer.

Turns the slate's uploaded salary + projection data into a set of
highest-scoring, legal, DISTINCT lineups -- mixed-integer linear
programs (MILP) solved one at a time with PuLP's bundled CBC solver,
free and pure-Python.

This app isn't building its own FPTS projections yet (see the README's
wishlist), so it requires a RotoWire projections CSV already loaded for
the date and maximizes against RotoWire's own FPTS numbers. Salary comes
from a DraftKings salary CSV, same as everywhere else salary shows up in
this app. Both are matched onto each hitter/pitcher already by
mlb_slate.build_slate() -- this module just reads that, it doesn't do
any matching of its own.

MULTI-LINEUP STRATEGY
----------------------
Generating N lineups for GPP mass entry isn't one big joint optimization
-- it's N sequential solves, each one forbidding an exact repeat of any
earlier lineup (a "no-good cut": at least one of the 10 players must
differ) and, once a player hits a user-set exposure cap, excluding them
from the pool entirely for every remaining solve. This is faster than a
joint formulation and easy to reason about, at the cost of not being
provably optimal across the whole set -- each lineup is still the best
available *given* what's already been generated, not the best possible
N-lineup portfolio in some global sense. Good enough for building a
real, diverse slate of entries without the complexity of a much larger
joint MILP.
"""

from __future__ import annotations

from typing import Any

import pulp

# DraftKings Classic MLB: one salary cap, ten roster slots.
SALARY_CAP = 50_000
SLOT_REQUIREMENTS = {
    "P": 2,
    "C": 1,
    "1B": 1,
    "2B": 1,
    "3B": 1,
    "SS": 1,
    "OF": 3,
}
SLOT_TYPES = list(SLOT_REQUIREMENTS)
ROSTER_SIZE = sum(SLOT_REQUIREMENTS.values())
MAX_HITTERS = ROSTER_SIZE - SLOT_REQUIREMENTS["P"]

# A safety ceiling, not a real-world limit anyone should hit -- each
# lineup is its own MILP solve, and this keeps a mistaken request (or a
# slate too thin to support many unique lineups) from hanging the app.
MAX_LINEUPS = 150


class OptimizerError(ValueError):
    """No legal lineup could be built -- missing data or an infeasible
    constraint. Routed to an HTTP 400 by the caller."""


def _eligible_slots(dk_position: str) -> list[str]:
    """
    DK's own position string (e.g. '1B/3B', 'OF', 'P') into the roster
    slot types a player can fill. This is DK's multi-eligibility column
    from the salary CSV -- deliberately not the single MLB-primary
    position living elsewhere on the player dict.
    """
    raw = {p.strip().upper() for p in (dk_position or "").split("/") if p.strip()}
    if raw & {"P", "SP", "RP"}:
        return ["P"]
    return [slot for slot in SLOT_TYPES if slot in raw]


def _stack_constraints(
    prob: pulp.LpProblem,
    x: dict[tuple[int, str], pulp.LpVariable],
    usable: list[dict[str, Any]],
    stack_groups: list[int],
    stack_teams: list[str | None],
) -> None:
    """
    Force at least `stack_groups` hitters onto each of that many distinct
    teams (e.g. [4, 2, 2] for a "4-2-2" -- at least 4 from one team, at
    least 2 from another, at least 2 from a third). A group whose
    `stack_teams` entry is a real team name gets a direct constraint on
    that team; a group left as None ("auto") gets an indicator-variable
    assignment so the solver picks the best team for that slot -- never
    colliding with another group's team, manual or auto.

    Deliberately a LOWER bound, not an exact count: for shapes whose
    groups already sum to all 8 hitter slots (5-3, 4-4, etc.) there's no
    room left for any group to exceed its target, so this behaves like
    an exact split anyway. For partial shapes (4-2, 3-3) it lets the
    leftover 2 hitters land anywhere -- including padding one of the
    named stacks further -- rather than artificially requiring a THIRD
    team to exist just to soak up slots nobody asked to constrain.
    """
    all_teams = sorted({p["team"] for p in usable})
    manual_teams = {t for t in stack_teams if t is not None}
    auto_teams = [t for t in all_teams if t not in manual_teams]
    auto_group_idx = [i for i, t in enumerate(stack_teams) if t is None]

    def hitter_count_for_team(t: str) -> pulp.LpAffineExpression:
        hitters_on_team = [p for p in usable if p["team"] == t and "P" not in p["slots"]]
        return pulp.lpSum(x[(p["id"], slot)] for p in hitters_on_team for slot in p["slots"])

    for size, team in zip(stack_groups, stack_teams):
        if team is not None:
            prob += hitter_count_for_team(team) >= size

    if not auto_group_idx:
        return

    # y[i, t] = 1 means auto group i is assigned to team t. Each auto
    # group gets exactly one team; each team fills at most one auto
    # group -- the standard "assign distinct options to slots" pattern.
    y = {
        (i, t): pulp.LpVariable(f"stackgrp{i}_{t}", cat="Binary")
        for i in auto_group_idx
        for t in auto_teams
    }
    for i in auto_group_idx:
        prob += pulp.lpSum(y[(i, t)] for t in auto_teams) == 1
    for t in auto_teams:
        prob += pulp.lpSum(y[(i, t)] for i in auto_group_idx) <= 1

    # Big-M (MAX_HITTERS is a tight, safe bound -- no team can ever
    # supply more than 8 hitters) gates the lower-bound constraint so it
    # only bites when that group really is assigned to that team.
    for i in auto_group_idx:
        size = stack_groups[i]
        for t in auto_teams:
            prob += hitter_count_for_team(t) >= size - MAX_HITTERS * (1 - y[(i, t)])


def build_player_pool(slate: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Flatten every hitter and probable pitcher across the slate into one
    optimizable pool. Skips anyone missing a matched salary or
    projection, anyone with a DK position we can't map to a roster slot,
    and anyone the lineup watcher has flagged as scratched today.
    """
    pool: list[dict[str, Any]] = []
    for game in slate.get("games") or []:
        for side in ("home", "away"):
            team = game[side]
            abbrev = team.get("abbrev") or team.get("name") or ""
            scratched_ids = {s.get("player_id") for s in (team.get("scratches") or [])}

            candidates = list(team.get("hitters") or [])
            pitcher = team.get("probable_pitcher")
            if pitcher:
                candidates.append(pitcher)

            for p in candidates:
                pid = p.get("id")
                if not pid or pid in scratched_ids:
                    continue
                salary_info = p.get("salary")
                proj_info = p.get("projection")
                if not salary_info or not salary_info.get("salary"):
                    continue
                if not proj_info or proj_info.get("fpts") is None:
                    continue
                slots = _eligible_slots(salary_info.get("position") or "")
                if not slots:
                    continue
                pool.append(
                    {
                        "id": pid,
                        "name": p.get("name"),
                        "team": abbrev,
                        "salary": salary_info["salary"],
                        "projected_fpts": proj_info["fpts"],
                        "slots": slots,
                    }
                )
    return pool


def _solve_one(
    pool: list[dict[str, Any]],
    *,
    stack_groups: list[int],
    stack_teams: list[str | None],
    excluded_ids: set[int],
    no_good_cuts: list[set[int]],
    locked_ids: set[int],
) -> dict[str, Any] | None:
    """
    Solve a single lineup against `pool`, minus anyone in `excluded_ids`
    (exposure-capped out) and forbidding an exact repeat of any lineup in
    `no_good_cuts`. Returns None on infeasibility rather than raising --
    the caller decides whether that's fatal or just "ran out of room."
    """
    usable = [p for p in pool if p["id"] not in excluded_ids]
    if not usable:
        return None

    usable_ids = {p["id"] for p in usable}
    if not locked_ids <= usable_ids:
        return None  # a locked player isn't available for this solve

    prob = pulp.LpProblem("dk_classic_mlb", pulp.LpMaximize)

    # One binary decision variable per (player, eligible slot type) --
    # a multi-eligible player gets one variable for each slot they could
    # fill, and at most one of those is allowed to be 1.
    x = {
        (p["id"], slot): pulp.LpVariable(f"x_{p['id']}_{slot}", cat="Binary")
        for p in usable
        for slot in p["slots"]
    }

    prob += pulp.lpSum(
        p["projected_fpts"] * x[(p["id"], slot)] for p in usable for slot in p["slots"]
    )

    for p in usable:
        required = 1 if p["id"] in locked_ids else None
        if required:
            prob += pulp.lpSum(x[(p["id"], slot)] for slot in p["slots"]) == required
        else:
            prob += pulp.lpSum(x[(p["id"], slot)] for slot in p["slots"]) <= 1

    for slot, count in SLOT_REQUIREMENTS.items():
        eligible = [p for p in usable if slot in p["slots"]]
        prob += pulp.lpSum(x[(p["id"], slot)] for p in eligible) == count

    prob += (
        pulp.lpSum(p["salary"] * x[(p["id"], slot)] for p in usable for slot in p["slots"])
        <= SALARY_CAP
    )

    if stack_groups:
        _stack_constraints(prob, x, usable, stack_groups, stack_teams)

    # No-good cuts: forbid exactly reproducing any earlier lineup by
    # capping how many of its 10 players can reappear together.
    for prior_ids in no_good_cuts:
        prior_in_pool = [p for p in usable if p["id"] in prior_ids]
        if len(prior_in_pool) < ROSTER_SIZE:
            continue  # some of that lineup's players are excluded now anyway
        prob += (
            pulp.lpSum(x[(p["id"], slot)] for p in prior_in_pool for slot in p["slots"])
            <= ROSTER_SIZE - 1
        )

    prob.solve(pulp.PULP_CBC_CMD(msg=0))
    if pulp.LpStatus[prob.status] != "Optimal":
        return None

    slots_out: dict[str, list[dict[str, Any]]] = {slot: [] for slot in SLOT_TYPES}
    salary_used = 0
    projected_points = 0.0
    player_ids: set[int] = set()
    for p in usable:
        for slot in p["slots"]:
            if round(x[(p["id"], slot)].value() or 0) == 1:
                slots_out[slot].append(
                    {
                        "id": p["id"],
                        "name": p["name"],
                        "team": p["team"],
                        "salary": p["salary"],
                        "projected_fpts": p["projected_fpts"],
                    }
                )
                salary_used += p["salary"]
                projected_points += p["projected_fpts"]
                player_ids.add(p["id"])

    return {
        "salary_used": salary_used,
        "salary_remaining": SALARY_CAP - salary_used,
        "projected_points": round(projected_points, 2),
        "slots": slots_out,
        "_player_ids": player_ids,
    }


def generate_lineups(
    slate: dict[str, Any],
    *,
    num_lineups: int = 1,
    stack_groups: list[int] | None = None,
    stack_teams: list[str | None] | None = None,
    max_exposure_pct: float | None = None,
    locked_ids: list[int] | None = None,
    excluded_ids: list[int] | None = None,
) -> dict[str, Any]:
    """
    Generate up to `num_lineups` distinct legal lineups.

    `locked_ids`, if given, are player ids that must appear in EVERY
    generated lineup, exempt from the exposure cap (a lock is a
    stronger, more explicit instruction than a general exposure limit).
    `excluded_ids`, if given, are player ids removed from the pool
    entirely before anything else runs -- distinct from the exposure
    mechanism's own internal exclusion-once-capped bookkeeping, which
    only ever applies to non-locked players.

    `stack_groups` is a list of minimum hitter-group sizes to force,
    largest first -- e.g. `[4, 2, 2]` for a "4-2-2" stack (at least 4
    hitters from one team, at least 2 from another, at least 2 from a
    third). Groups that sum to fewer than 8 (DK's hitter-slot count)
    leave the remainder free -- those slots can land anywhere, including
    padding one of the named stacks further; groups summing to 8 come
    out exact since there's no room left over. `stack_teams`, if given,
    must have one entry per group -- a real team name to force that
    group onto a specific team, or `None` to let the solver pick the
    best available team for that group (never
    colliding with another group's team, manual or auto).

    `max_exposure_pct`, if given, caps how often any one player can
    appear across the whole generated set -- e.g. 50 means no player
    shows up in more than half the lineups.

    Returns `{"lineups": [...], "exposure": [...]}`. If the pool or
    constraints can't support the full count requested, returns as many
    as it could build rather than failing the whole request -- only an
    empty result (not even one legal lineup) raises.
    """
    if num_lineups < 1:
        raise OptimizerError("num_lineups must be at least 1.")
    if num_lineups > MAX_LINEUPS:
        raise OptimizerError(f"Generating more than {MAX_LINEUPS} lineups at once isn't supported.")

    pool = build_player_pool(slate)
    if not pool:
        raise OptimizerError(
            "No optimizable players for this date -- upload both a "
            "DraftKings salary CSV and a RotoWire projections CSV first."
        )

    locked = set(locked_ids or [])
    user_excluded = set(excluded_ids or [])
    overlap = locked & user_excluded
    if overlap:
        raise OptimizerError(
            f"Can't both lock and exclude the same player(s): {sorted(overlap)}."
        )

    pool = [p for p in pool if p["id"] not in user_excluded]
    if not pool:
        raise OptimizerError("Excluding those players leaves nobody left to build a lineup from.")

    pool_ids = {p["id"] for p in pool}
    missing_locks = locked - pool_ids
    if missing_locks:
        raise OptimizerError(
            f"Locked player id(s) aren't in today's optimizable pool "
            f"(scratched, or missing a salary/projection match): {sorted(missing_locks)}."
        )
    if len(locked) > ROSTER_SIZE:
        raise OptimizerError(
            f"Locked {len(locked)} players, but a lineup only has {ROSTER_SIZE} slots."
        )

    stack_groups = stack_groups or []
    if stack_groups:
        if any(not isinstance(size, int) or size < 1 for size in stack_groups):
            raise OptimizerError("Stack group sizes must be positive whole numbers.")
        if sum(stack_groups) > MAX_HITTERS:
            raise OptimizerError(
                f"Stack groups add up to {sum(stack_groups)} hitters, but a DK "
                f"Classic MLB roster only has {MAX_HITTERS} hitter slots."
            )
        if stack_teams is not None and len(stack_teams) != len(stack_groups):
            raise OptimizerError(
                "stack_teams needs exactly one entry per stack group (use null for auto)."
            )
        stack_teams = list(stack_teams) if stack_teams is not None else [None] * len(stack_groups)
        named = [t for t in stack_teams if t is not None]
        if len(named) != len(set(named)):
            raise OptimizerError("Each stack group must be assigned to a different team.")
        pool_teams = {p["team"] for p in pool}
        unknown = [t for t in named if t not in pool_teams]
        if unknown:
            raise OptimizerError(f"Unknown team(s) for stacking: {', '.join(unknown)}.")
    else:
        stack_teams = []

    exposure_cap = (
        max(1, round(max_exposure_pct / 100 * num_lineups))
        if max_exposure_pct is not None
        else None
    )

    exposure_count: dict[int, int] = {}
    excluded_ids: set[int] = set()
    no_good_cuts: list[set[int]] = []
    lineups: list[dict[str, Any]] = []

    for i in range(num_lineups):
        result = _solve_one(
            pool,
            stack_groups=stack_groups,
            stack_teams=stack_teams,
            excluded_ids=excluded_ids,
            no_good_cuts=no_good_cuts,
            locked_ids=locked,
        )
        if result is None:
            if i == 0:
                raise OptimizerError(
                    "Couldn't build a legal lineup with the current player pool and "
                    "constraints -- try loosening the stack requirement, exposure cap, "
                    "or locked players."
                )
            break  # ran out of room for more unique/exposure-legal lineups

        player_ids: set[int] = result.pop("_player_ids")
        no_good_cuts.append(player_ids)
        lineups.append(result)

        for pid in player_ids:
            exposure_count[pid] = exposure_count.get(pid, 0) + 1
            # Locked players are exempt from the exposure cap -- a lock
            # is a stronger, more explicit instruction than a general
            # exposure limit, and they're guaranteed to reappear anyway.
            if pid in locked:
                continue
            if exposure_cap is not None and exposure_count[pid] >= exposure_cap:
                excluded_ids.add(pid)

    by_id = {p["id"]: p for p in pool}
    exposure = [
        {
            "id": pid,
            "name": by_id[pid]["name"],
            "team": by_id[pid]["team"],
            "count": count,
            "pct": round(100 * count / len(lineups), 1),
        }
        for pid, count in sorted(exposure_count.items(), key=lambda kv: -kv[1])
    ]

    return {"lineups": lineups, "exposure": exposure}
