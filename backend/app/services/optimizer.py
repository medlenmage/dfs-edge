"""
DraftKings Classic MLB lineup optimizer.

Turns the slate's uploaded salary + projection data into a set of
highest-scoring, legal, DISTINCT lineups -- mixed-integer linear
programs (MILP) solved one at a time with PuLP's bundled CBC solver,
free and pure-Python.

Requires a projections source already loaded/computed for the date --
either an uploaded RotoWire CSV, or (via `projection_source="inhouse"`)
`inhouse_projections.py`'s own FPTS/ownership model, computed by
mlb_slate.build_slate() when called with `include_inhouse=True`.
Salary comes from a DraftKings salary CSV, same as everywhere else
salary shows up in this app. Both are matched onto each hitter/pitcher
already by mlb_slate.build_slate() -- this module just reads that, it
doesn't do any matching of its own.

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

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Callable

import pulp

# DraftKings Classic MLB: one salary cap, ten roster slots.
SALARY_CAP = 50_000
# Applied automatically unless the caller overrides it (0 disables the
# floor entirely) -- a lineup with a lot of unspent salary is almost
# always leaving real projected points on the table, so this is a
# sensible default rather than an opt-in.
DEFAULT_MIN_SALARY = 47_000

# A cheap, temporary mitigation (confirmed with the user, revisit
# later) for a real gap: this pool has no idea which players the
# at-bat simulation engine can actually score (it needs a confirmed or
# projected batting-order spot -- see atbat_sim.py), so it could draft
# a bench/unlisted player into an entry that engine then can't
# simulate at all. A real bench/deep-reserve player's own projected
# FPTS is reliably near zero -- filtering the pool to real contributors
# cuts that collision down sharply without threading at-bat-eligibility
# through generation itself, which is the real, more precise fix for a
# later pass.
MIN_POOL_FPTS = 3.0
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
    banned_stack_teams: set[str],
) -> dict[tuple[int, str], pulp.LpVariable]:
    """
    Force at least `stack_groups` hitters onto each of that many distinct
    teams (e.g. [4, 2, 2] for a "4-2-2" -- at least 4 from one team, at
    least 2 from another, at least 2 from a third). A group whose
    `stack_teams` entry is a real team name gets a direct constraint on
    that team; a group left as None ("auto") gets an indicator-variable
    assignment so the solver picks the best team for that slot -- never
    colliding with another group's team, manual or auto, and never
    picking a team in `banned_stack_teams` (a team that's hit its own
    stack-exposure cap in an earlier lineup -- manual picks are exempt,
    since naming a team explicitly is a deliberate override each time).

    Deliberately a LOWER bound, not an exact count: for shapes whose
    groups already sum to all 8 hitter slots (5-3, 4-4, etc.) there's no
    room left for any group to exceed its target, so this behaves like
    an exact split anyway. For partial shapes (4-2, 3-3) it lets the
    leftover 2 hitters land anywhere -- including padding one of the
    named stacks further -- rather than artificially requiring a THIRD
    team to exist just to soak up slots nobody asked to constrain.

    Returns the auto-assignment indicator variables (`y[i, t]`) so the
    caller can read back, after solving, which team ended up filling
    each auto group -- needed for stack-exposure tracking. Empty if
    there are no auto groups.
    """
    all_teams = sorted({p["team"] for p in usable})
    manual_teams = {t for t in stack_teams if t is not None}
    auto_teams = [t for t in all_teams if t not in manual_teams and t not in banned_stack_teams]
    auto_group_idx = [i for i, t in enumerate(stack_teams) if t is None]

    def hitter_count_for_team(t: str) -> pulp.LpAffineExpression:
        hitters_on_team = [p for p in usable if p["team"] == t and "P" not in p["slots"]]
        return pulp.lpSum(x[(p["id"], slot)] for p in hitters_on_team for slot in p["slots"])

    for size, team in zip(stack_groups, stack_teams):
        if team is not None:
            prob += hitter_count_for_team(team) >= size

    if not auto_group_idx:
        return {}

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

    return y


def _one_off_constraints(
    prob: pulp.LpProblem,
    x: dict[tuple[int, str], pulp.LpVariable],
    usable: list[dict[str, Any]],
    stack_groups: list[int],
    stack_teams: list[str | None],
    stack_auto_y: dict[tuple[int, str], pulp.LpVariable],
    banned_stack_teams: set[str],
    one_off_eligible: Callable[[dict[str, Any]], bool],
) -> None:
    """
    Restrict who can fill the "one-off" hitter slots -- the leftover
    slots a partial stack shape (4-2, 3-3, ...) doesn't claim -- to
    whichever pool `one_off_eligible(player)` allows.

    A hitter whose team ends up assigned to a stack group is exempt no
    matter what: their team's hitter count is already governed by the
    `>=` stack constraint, so any of them landing in a roster slot is a
    stack pick, not a one-off pick, even if that particular hitter isn't
    one of the "minimum" ones. A hitter on a team that can never be
    assigned to any group (not named manually, and not a candidate for
    auto-assignment) who also fails the filter is barred outright --
    there's no other way for them to end up in the lineup.
    """
    all_teams = sorted({p["team"] for p in usable})
    manual_teams = {t for t in stack_teams if t is not None}
    auto_teams = [t for t in all_teams if t not in manual_teams and t not in banned_stack_teams]
    auto_group_idx = [i for i, t in enumerate(stack_teams) if t is None]

    for p in usable:
        if "P" in p["slots"] or one_off_eligible(p):
            continue
        t = p["team"]
        if t in manual_teams:
            continue  # this team's slot count is already forced by the stack constraint
        picked = pulp.lpSum(x[(p["id"], slot)] for slot in p["slots"])
        if t in auto_teams:
            team_stacked = pulp.lpSum(
                stack_auto_y[(i, t)] for i in auto_group_idx if (i, t) in stack_auto_y
            )
            prob += picked <= team_stacked
        else:
            prob += picked == 0


# A player qualifies as a "payup or high-FPTS" pick for one-off slots
# if their salary or projected FPTS is within this fraction of the
# best available at their own slot type in today's pool.
ONE_OFF_QUALITY_RATIO = 0.8
# How many of a partial stack's leftover one-off slots must go to a
# qualifying player -- a floor, not a fraction, so a shape with a lot
# of leftover slots (e.g. a lone "5" stack, 3 free) still only needs 2
# of them to be strong, not all of them.
ONE_OFF_QUALITY_MIN = 2


def _one_off_quality_ids(pool: list[dict[str, Any]]) -> set[int]:
    """
    Which hitters count as "payup or high-FPTS" for one-off-slot
    purposes: salary or projected FPTS within ONE_OFF_QUALITY_RATIO of
    the best available at their own slot type (a catcher is judged
    against the best available catcher, not the best available
    outfielder) among today's whole pool -- not scoped to any
    particular lineup's actual candidates, so it's stable across every
    solve in a multi-lineup batch.
    """
    best_salary: dict[str, float] = {}
    best_fpts: dict[str, float] = {}
    for p in pool:
        if "P" in p["slots"]:
            continue
        for slot in p["slots"]:
            best_salary[slot] = max(best_salary.get(slot, 0), p["salary"])
            best_fpts[slot] = max(best_fpts.get(slot, 0), p["projected_fpts"])

    qualifying: set[int] = set()
    for p in pool:
        if "P" in p["slots"]:
            continue
        if any(
            p["salary"] >= ONE_OFF_QUALITY_RATIO * best_salary[slot]
            or p["projected_fpts"] >= ONE_OFF_QUALITY_RATIO * best_fpts[slot]
            for slot in p["slots"]
        ):
            qualifying.add(p["id"])
    return qualifying


def _one_off_quality_constraint(
    prob: pulp.LpProblem,
    x: dict[tuple[int, str], pulp.LpVariable],
    usable: list[dict[str, Any]],
    stack_groups: list[int],
    stack_teams: list[str | None],
    stack_auto_y: dict[tuple[int, str], pulp.LpVariable],
    banned_stack_teams: set[str],
    qualifying_ids: set[int],
) -> None:
    """
    Default preference for a partial stack's leftover one-off slots:
    at least ONE_OFF_QUALITY_MIN of them must go to a qualifying
    ("payup or high-FPTS") player. Only called when the caller hasn't
    already given an explicit one-off restriction (one_off_group_ids /
    one_off_min_salary / one_off_max_salary) -- that's a stronger,
    deliberate override and takes priority over this default.

    Mirrors _one_off_constraints' own team-exemption logic (a hitter on
    a stack-assigned team is never a "one-off" pick regardless of
    whether they personally qualify) -- a genuine one-off pick from an
    auto-assigned team needs its own binary indicator per player (the
    standard AND linearization: is_one_off <= picked, is_one_off <=
    1 - team_stacked, is_one_off >= picked - team_stacked) since
    whether that team ends up stack-assigned isn't known until the
    solve.

    A flat "qualifying one-offs >= required" floor would make an
    ordinary 2-team manual stack with plenty of depth on both teams
    (e.g. a 5-3 between two juicy offenses) INFEASIBLE whenever it's
    cheapest to pad both stacks past their minimums rather than reach
    for a 3rd team -- every leftover slot would then be exempt stack
    padding, with zero genuine one-off picks to ever satisfy the floor.
    Relaxed instead: `qualifying_one_offs >= required - (leftover -
    total_one_offs)`, i.e. the floor drops by exactly one for every
    leftover slot that turns out to be stack padding rather than a
    genuine one-off pick -- if every leftover slot ends up as padding,
    the floor relaxes to 0 (trivially satisfied); if all `leftover`
    slots are genuine one-offs, it's the full `required`.
    """
    leftover = MAX_HITTERS - sum(stack_groups)
    if leftover <= 0:
        return
    required = min(ONE_OFF_QUALITY_MIN, leftover)

    all_teams = sorted({p["team"] for p in usable})
    manual_teams = {t for t in stack_teams if t is not None}
    auto_teams = [t for t in all_teams if t not in manual_teams and t not in banned_stack_teams]
    auto_group_idx = [i for i, t in enumerate(stack_teams) if t is None]

    total_one_off_terms = []
    qualifying_one_off_terms = []
    for p in usable:
        if "P" in p["slots"]:
            continue
        t = p["team"]
        if t in manual_teams:
            continue  # always a stack pick on this team, never a one-off pick
        picked = pulp.lpSum(x[(p["id"], slot)] for slot in p["slots"])
        if t in auto_teams:
            team_stacked = pulp.lpSum(
                stack_auto_y[(i, t)] for i in auto_group_idx if (i, t) in stack_auto_y
            )
            is_one_off = pulp.LpVariable(f"oneoff_{p['id']}", cat="Binary")
            prob += is_one_off <= picked
            prob += is_one_off <= 1 - team_stacked
            prob += is_one_off >= picked - team_stacked
        else:
            is_one_off = picked  # never a candidate for any stack group -- always exempt-free
        total_one_off_terms.append(is_one_off)
        if p["id"] in qualifying_ids:
            qualifying_one_off_terms.append(is_one_off)

    prob += (
        pulp.lpSum(qualifying_one_off_terms) - pulp.lpSum(total_one_off_terms)
        >= required - leftover
    )


def _team_count_constraints(
    prob: pulp.LpProblem,
    x: dict[tuple[int, str], pulp.LpVariable],
    usable: list[dict[str, Any]],
    min_teams: int | None,
    max_teams: int | None,
) -> None:
    """
    Bound how many DISTINCT teams appear among the 10 selected players in
    a single lineup. `team_used[t]` is pinned to 1 exactly when at least
    one player from team t is selected, via the standard two-sided
    "presence indicator" pair: ROSTER_SIZE is a safe, tight big-M since
    no team can ever supply more than all 10 slots.
    """
    all_teams = sorted({p["team"] for p in usable})
    team_used = {t: pulp.LpVariable(f"teamused_{t}", cat="Binary") for t in all_teams}
    for t in all_teams:
        team_players = [p for p in usable if p["team"] == t]
        total = pulp.lpSum(x[(p["id"], slot)] for p in team_players for slot in p["slots"])
        prob += total <= ROSTER_SIZE * team_used[t]
        prob += total >= team_used[t]

    total_teams = pulp.lpSum(team_used[t] for t in all_teams)
    if min_teams is not None:
        prob += total_teams >= min_teams
    if max_teams is not None:
        prob += total_teams <= max_teams


def _opposing_pitcher_constraints(
    prob: pulp.LpProblem,
    x: dict[tuple[int, str], pulp.LpVariable],
    usable: list[dict[str, Any]],
) -> None:
    """
    A lineup can never roster a hitter alongside the pitcher he's
    actually facing that day -- a real strikeout or a real home run is
    the same at-bat scored two opposite ways, so pairing them is a
    strict handicap, never a legitimate build. Enforced as a pairwise
    x_pitcher + x_hitter <= 1 for every (pitcher, opposing hitter) pair
    rather than one aggregate constraint -- CBC handles a larger number
    of simple binary pairs just as easily, and it reads as exactly what
    it is. A pitcher's OWN team's hitters are untouched (stacking your
    own pitcher's offense is a normal, encouraged strategy).
    """
    pitchers = [p for p in usable if "P" in p["slots"]]
    hitters_by_team: dict[str, list[dict[str, Any]]] = {}
    for h in usable:
        if "P" not in h["slots"]:
            hitters_by_team.setdefault(h["team"], []).append(h)

    for pitcher in pitchers:
        for h in hitters_by_team.get(pitcher.get("opponent") or "", []):
            prob += x[(pitcher["id"], "P")] + pulp.lpSum(x[(h["id"], s)] for s in h["slots"]) <= 1


PROJECTION_SOURCES = {
    "rotowire": ("fpts", "ownership_pct"),
    "inhouse": ("inhouse_fpts", "inhouse_ownership_pct"),
}


def build_player_pool(
    slate: dict[str, Any],
    *,
    included_game_pks: set[int] | None = None,
    projection_source: str = "rotowire",
) -> list[dict[str, Any]]:
    """
    Flatten every hitter and probable pitcher across the slate into one
    optimizable pool. Skips anyone missing a matched salary or
    projection, anyone projected below MIN_POOL_FPTS (see its own
    comment -- a cheap stand-in for "is this player actually going to
    play"), anyone with a DK position we can't map to a roster slot,
    and anyone the lineup watcher has flagged as scratched today.

    `included_game_pks`, if given, restricts the pool to those specific
    games -- e.g. to match a particular DK slate rather than every game
    MLB's schedule returns for the date.

    `projection_source` picks which FPTS/ownership numbers to build the
    pool from -- `"rotowire"` (the default, `projection.fpts`) or
    `"inhouse"` (`projection.inhouse_fpts`, only present when the slate
    was built with `include_inhouse=True`).

    Ownership falls back to the OTHER source when the chosen one is
    missing it (e.g. RotoWire's export doesn't cover every player on a
    slate) -- a real bug found by backtesting the contest simulator
    against real GPP results: without this, any player RotoWire simply
    didn't export gets treated as ~0% owned (`contest.py`'s ownership
    floor), silently making the simulated opponent field construction
    for those players closer to random than realistic, which is exactly
    the kind of gap that inflates a skill-based entry's simulated edge.
    FPTS has no such fallback -- a missing FPTS excludes the player from
    the pool entirely (below), since there's no other signal to
    optimize against.

    Each pool entry also carries `edge_composite` -- scoring.py's own
    matchup-quality multiplier, used only by variance.py's simulator to
    condition each player's Monte Carlo outcome on today's actual
    matchup rather than sampling blind to it. Not used by either
    lineup-building engine's own optimization objective.
    """
    if projection_source not in PROJECTION_SOURCES:
        raise OptimizerError(
            f"Unknown projection_source '{projection_source}'. "
            f"Choose one of: {', '.join(PROJECTION_SOURCES)}."
        )
    fpts_key, ownership_key = PROJECTION_SOURCES[projection_source]
    other_source = next(s for s in PROJECTION_SOURCES if s != projection_source)
    _, fallback_ownership_key = PROJECTION_SOURCES[other_source]

    # DOUBLEHEADERS PUT THE SAME PLAYER ON THE BOARD TWICE. A team
    # playing two games in a day appears in two slate entries, so every
    # one of its players would land in the pool once per game -- 18 of
    # 262 on a real 2026-09-04 board, the whole Cleveland roster.
    #
    # That is not merely cosmetic. The MILP keys its variables on
    # (player id, slot), so two pool entries for one player SHARE a
    # variable and then add it to that slot's count constraint twice:
    # the slot reads him as two players, and locking him is outright
    # infeasible (measured -- OptimizerError, on a player who is plainly
    # rosterable). Salary double-counts the same way.
    #
    # Deduplicating on id, after the game filter so a narrowed slate
    # still keeps the right copy, is what makes one player one player.
    seen_ids: set[Any] = set()
    pool: list[dict[str, Any]] = []
    for game in slate.get("games") or []:
        if included_game_pks is not None and game.get("game_pk") not in included_game_pks:
            continue
        for side in ("home", "away"):
            team = game[side]
            abbrev = team.get("abbrev") or team.get("name") or ""
            opp_team = game["away" if side == "home" else "home"]
            opp_abbrev = opp_team.get("abbrev") or opp_team.get("name") or ""
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
                if not proj_info or proj_info.get(fpts_key) is None:
                    continue
                if proj_info[fpts_key] < MIN_POOL_FPTS:
                    continue
                slots = _eligible_slots(salary_info.get("position") or "")
                if not slots:
                    continue
                if pid in seen_ids:
                    continue
                seen_ids.add(pid)
                pool.append(
                    {
                        "id": pid,
                        "name": p.get("name"),
                        "team": abbrev,
                        "opponent": opp_abbrev,
                        # Which real game this player belongs to -- late_swap()
                        # needs it to know when a rostered player's game
                        # locks, and it's cheap to carry through everywhere
                        # else too (lineup exports, the frontend) rather than
                        # making late_swap the only consumer that needs a
                        # separate slate lookup.
                        "game_pk": game.get("game_pk"),
                        # DK's own numeric player id, verbatim from the
                        # uploaded DK salary CSV (empty string when only a
                        # RotoWire file is loaded -- see
                        # salaries.from_rotowire_rows()) -- needed to fill
                        # a real DK contest-entry template CSV back in
                        # (services/dk_entry_manager.py), never used for
                        # this app's own MLB Stats API-keyed matching.
                        "dk_id": salary_info.get("dk_id") or "",
                        "salary": salary_info["salary"],
                        "projected_fpts": proj_info[fpts_key],
                        "ownership_pct": proj_info.get(ownership_key) or proj_info.get(fallback_ownership_key) or 0,
                        "slots": slots,
                        # scoring.py's matchup-quality multiplier (1.0 =
                        # dead average), reused by variance.py's Monte
                        # Carlo engine to condition each player's
                        # simulated outcome on today's actual matchup --
                        # platoon, park, weather, opposing pitcher/bullpen
                        # quality -- rather than sampling blind to it.
                        # None (not 1.0) when no edge was computed, so
                        # the simulator can tell "no signal" apart from
                        # "confirmed neutral."
                        "edge_composite": (p.get("edge") or {}).get("composite"),
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
    banned_stack_teams: set[str],
    min_salary: int | None = None,
    max_salary: int | None = None,
    min_unique_players: int = 1,
    min_teams_per_lineup: int | None = None,
    max_teams_per_lineup: int | None = None,
    one_off_eligible: Callable[[dict[str, Any]], bool] | None = None,
    one_off_quality_ids: set[int] | None = None,
    min_ownership_pct: float | None = None,
    max_ownership_pct: float | None = None,
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

    _opposing_pitcher_constraints(prob, x, usable)

    total_salary = pulp.lpSum(
        p["salary"] * x[(p["id"], slot)] for p in usable for slot in p["slots"]
    )
    prob += total_salary <= SALARY_CAP
    if min_salary is not None:
        prob += total_salary >= min_salary
    if max_salary is not None:
        prob += total_salary <= max_salary

    if min_ownership_pct is not None or max_ownership_pct is not None:
        total_ownership = pulp.lpSum(
            p["ownership_pct"] * x[(p["id"], slot)] for p in usable for slot in p["slots"]
        )
        if min_ownership_pct is not None:
            prob += total_ownership >= min_ownership_pct
        if max_ownership_pct is not None:
            prob += total_ownership <= max_ownership_pct

    stack_auto_y: dict[tuple[int, str], pulp.LpVariable] = {}
    if stack_groups:
        stack_auto_y = _stack_constraints(
            prob, x, usable, stack_groups, stack_teams, banned_stack_teams
        )
        if one_off_eligible is not None:
            _one_off_constraints(
                prob, x, usable, stack_groups, stack_teams, stack_auto_y,
                banned_stack_teams, one_off_eligible,
            )
        elif one_off_quality_ids is not None:
            _one_off_quality_constraint(
                prob, x, usable, stack_groups, stack_teams, stack_auto_y,
                banned_stack_teams, one_off_quality_ids,
            )

    if min_teams_per_lineup is not None or max_teams_per_lineup is not None:
        _team_count_constraints(prob, x, usable, min_teams_per_lineup, max_teams_per_lineup)

    # No-good cuts: forbid reproducing any earlier lineup too closely --
    # at least `min_unique_players` of its 10 players must differ.
    for prior_ids in no_good_cuts:
        prior_in_pool = [p for p in usable if p["id"] in prior_ids]
        if len(prior_in_pool) < ROSTER_SIZE:
            continue  # some of that lineup's players are excluded now anyway
        prob += (
            pulp.lpSum(x[(p["id"], slot)] for p in prior_in_pool for slot in p["slots"])
            <= ROSTER_SIZE - min_unique_players
        )

    prob.solve(pulp.PULP_CBC_CMD(msg=0))
    if pulp.LpStatus[prob.status] != "Optimal":
        return None

    slots_out: dict[str, list[dict[str, Any]]] = {slot: [] for slot in SLOT_TYPES}
    salary_used = 0
    projected_points = 0.0
    total_ownership_pct = 0.0
    player_ids: set[int] = set()
    for p in usable:
        for slot in p["slots"]:
            if round(x[(p["id"], slot)].value() or 0) == 1:
                slots_out[slot].append(
                    {
                        "id": p["id"],
                        "name": p["name"],
                        "team": p["team"],
                        "game_pk": p["game_pk"],
                        "salary": p["salary"],
                        "projected_fpts": p["projected_fpts"],
                        "ownership_pct": p["ownership_pct"],
                        "edge_composite": p["edge_composite"],
                        "dk_id": p.get("dk_id") or "",
                    }
                )
                salary_used += p["salary"]
                projected_points += p["projected_fpts"]
                total_ownership_pct += p["ownership_pct"]
                player_ids.add(p["id"])

    # Which team(s) the solver actually picked for each auto stack
    # group, read back from the solved indicator variables -- exact,
    # not a guess, since we already forced exactly one y[i,t]==1 per
    # auto group.
    auto_stack_teams = {t for (i, t), var in stack_auto_y.items() if round(var.value() or 0) == 1}

    return {
        "salary_used": salary_used,
        "salary_remaining": SALARY_CAP - salary_used,
        "projected_points": round(projected_points, 2),
        "total_ownership_pct": round(total_ownership_pct, 1),
        "slots": slots_out,
        "_player_ids": player_ids,
        "_auto_stack_teams": auto_stack_teams,
    }


def generate_lineups(
    slate: dict[str, Any],
    *,
    num_lineups: int = 1,
    projection_source: str = "rotowire",
    stack_groups: list[int] | None = None,
    stack_teams: list[str | None] | None = None,
    max_exposure_pct: float | None = None,
    exposure_by_slot: dict[str, float] | None = None,
    team_exposure_cap: dict[str, float] | None = None,
    locked_ids: list[int] | None = None,
    excluded_ids: list[int] | None = None,
    min_salary: int | None = None,
    max_salary: int | None = None,
    min_unique_players: int = 1,
    min_teams_per_lineup: int | None = None,
    max_teams_per_lineup: int | None = None,
    one_off_group_ids: list[int] | None = None,
    one_off_min_salary: int | None = None,
    one_off_max_salary: int | None = None,
    min_ownership_pct: float | None = None,
    max_ownership_pct: float | None = None,
    included_game_pks: list[int] | None = None,
) -> dict[str, Any]:
    """
    Generate up to `num_lineups` distinct legal lineups.

    `exposure_by_slot`, if given, overrides `max_exposure_pct` for
    specific roster slots -- e.g. `{"OF": 40}` caps outfield exposure at
    40% while everything else still uses the general cap (or is
    unlimited, if `max_exposure_pct` wasn't set either). The cap applied
    to a player is based on whichever slot they actually filled in that
    particular lineup, since a multi-eligible player can occupy
    different slots across the set.

    `team_exposure_cap`, if given, caps how often a team is used AS THE
    STACK -- not incidental one-off appearances of its players -- across
    the generated set, e.g. `{"NYY": 30}` means an auto-assigned stack
    group lands on NYY in no more than 30% of lineups. Only applies to
    auto-assigned groups (`stack_teams` entries left as `None`); a team
    you explicitly name in `stack_teams` is a deliberate override each
    time and is never banned by this cap. Requires `stack_groups`.

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

    `min_salary`, if given, is a floor on total lineup salary, symmetric
    with the fixed $50,000 cap -- unset (None) here means no floor, same
    as every other optional constraint on this function; the HTTP API
    (routers/mlb.py) applies a $47,000 default unless the caller
    overrides it, but this library function itself stays opt-in.
    `max_salary`, if given, is an additional ceiling below the fixed
    $50,000 cap (e.g. to deliberately leave room under a punt-heavy
    build's actual spend).

    `min_unique_players`, if given (default 1), is how many of a
    lineup's 10 players must differ from every earlier lineup in the
    set -- 1 is today's default behavior (just not an exact repeat); a
    higher value forces more genuinely different builds. 0 allows exact
    duplicates -- a real, sometimes-deliberate GPP move (e.g. entering
    a signature build multiple times) -- with each returned lineup
    carrying a `duplicate_count` reporting how many identical copies
    ended up in the set.

    `min_teams_per_lineup` / `max_teams_per_lineup`, if given, bound how
    many distinct teams appear among a single lineup's 10 players.

    `one_off_group_ids` / `one_off_min_salary` / `one_off_max_salary`,
    if given, restrict who can fill the "one-off" hitter slots -- the
    leftover slots a partial stack shape (4-2, 3-3, ...) doesn't claim.
    A hitter whose team ends up assigned to a stack group is always
    exempt (their team's count is already governed by the stack
    constraint); a hitter on any other team must either be in
    `one_off_group_ids` or fall within the salary range to be eligible
    for one of those leftover slots. Use one or the other, not both --
    a group whitelist and a salary range are two different ways to
    answer the same question. Requires a partial `stack_groups` shape
    (one summing to fewer than the 8 hitter slots); a full shape has no
    leftover slots for this to apply to.

    When none of those three are given but the shape is still partial,
    a default preference kicks in instead: at least `min(2, leftover)`
    one-off slots must go to a "payup or high-FPTS" player (salary or
    projected FPTS within 80% of the best available at that slot type
    in today's pool) -- a cheap punt play filling every leftover slot
    is rarely the right GPP construction. An explicit restriction above
    always overrides this default rather than stacking with it.

    `min_ownership_pct` / `max_ownership_pct`, if given, bound each
    lineup's cumulative ownership -- the sum of the 10 rostered
    players' RotoWire `ownership_pct`, the DFS-community-standard
    measure of how "chalky" a build is. Players missing an ownership
    number (not every RotoWire export has one for everyone) count as 0.

    `included_game_pks`, if given, restricts the pool to those specific
    games -- e.g. to match a particular DK slate (a subset of the day's
    full MLB schedule) rather than every game returned for the date.

    Returns `{"lineups": [...], "exposure": [...]}`. If the pool or
    constraints can't support the full count requested, returns as many
    as it could build rather than failing the whole request -- only an
    empty result (not even one legal lineup) raises.
    """
    if num_lineups < 1:
        raise OptimizerError("num_lineups must be at least 1.")
    if num_lineups > MAX_LINEUPS:
        raise OptimizerError(f"Generating more than {MAX_LINEUPS} lineups at once isn't supported.")

    if included_game_pks is not None and not included_game_pks:
        raise OptimizerError("included_game_pks can't be empty.")

    pool = build_player_pool(
        slate,
        included_game_pks=set(included_game_pks) if included_game_pks is not None else None,
        projection_source=projection_source,
    )
    if not pool:
        if included_game_pks is not None:
            raise OptimizerError(
                "No optimizable players in the selected games -- try including more games."
            )
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

    if exposure_by_slot:
        bad_slots = set(exposure_by_slot) - set(SLOT_TYPES)
        if bad_slots:
            raise OptimizerError(f"Unknown roster slot(s) in exposure_by_slot: {sorted(bad_slots)}.")

    if team_exposure_cap:
        if not stack_groups:
            raise OptimizerError("team_exposure_cap requires stack_groups to be set.")
        pool_teams = {p["team"] for p in pool}
        unknown = set(team_exposure_cap) - pool_teams
        if unknown:
            raise OptimizerError(f"Unknown team(s) in team_exposure_cap: {sorted(unknown)}.")

    if min_salary is not None and min_salary > SALARY_CAP:
        raise OptimizerError(
            f"min_salary ({min_salary}) can't be more than the ${SALARY_CAP} salary cap."
        )
    if max_salary is not None and max_salary > SALARY_CAP:
        raise OptimizerError(
            f"max_salary ({max_salary}) can't be more than the ${SALARY_CAP} salary cap."
        )
    if min_salary is not None and max_salary is not None and min_salary > max_salary:
        raise OptimizerError("min_salary can't be more than max_salary.")

    if not (0 <= min_unique_players <= ROSTER_SIZE):
        raise OptimizerError(f"min_unique_players must be between 0 and {ROSTER_SIZE}.")

    for label, value in (
        ("min_teams_per_lineup", min_teams_per_lineup),
        ("max_teams_per_lineup", max_teams_per_lineup),
    ):
        if value is not None and not (1 <= value <= ROSTER_SIZE):
            raise OptimizerError(f"{label} must be between 1 and {ROSTER_SIZE}.")
    if (
        min_teams_per_lineup is not None
        and max_teams_per_lineup is not None
        and min_teams_per_lineup > max_teams_per_lineup
    ):
        raise OptimizerError("min_teams_per_lineup can't be more than max_teams_per_lineup.")

    one_off_active = (
        one_off_group_ids is not None
        or one_off_min_salary is not None
        or one_off_max_salary is not None
    )
    one_off_eligible: Callable[[dict[str, Any]], bool] | None = None
    if one_off_active:
        if not stack_groups:
            raise OptimizerError("One-off slot restrictions require stack_groups to be set.")
        if sum(stack_groups) >= MAX_HITTERS:
            raise OptimizerError(
                "One-off slot restrictions only apply to a partial stack shape "
                f"(stack_groups summing to fewer than {MAX_HITTERS} hitters) -- "
                "this shape already claims every hitter slot."
            )
        if one_off_group_ids is not None and (
            one_off_min_salary is not None or one_off_max_salary is not None
        ):
            raise OptimizerError(
                "Use either one_off_group_ids or a one-off salary range, not both."
            )
        if one_off_group_ids is not None:
            if not one_off_group_ids:
                raise OptimizerError("one_off_group_ids can't be empty.")
            allowed_ids = set(one_off_group_ids)
            one_off_eligible = lambda p: p["id"] in allowed_ids  # noqa: E731
        else:
            if (
                one_off_min_salary is not None
                and one_off_max_salary is not None
                and one_off_min_salary > one_off_max_salary
            ):
                raise OptimizerError(
                    "one_off_min_salary can't be more than one_off_max_salary."
                )
            lo = one_off_min_salary if one_off_min_salary is not None else 0
            hi = one_off_max_salary if one_off_max_salary is not None else SALARY_CAP
            one_off_eligible = lambda p: lo <= p["salary"] <= hi  # noqa: E731

    # Default preference (not an opt-in): a partial stack's leftover
    # one-off slots lean toward "payup or high-FPTS" players unless the
    # caller already gave an explicit one-off restriction above (a
    # stronger, deliberate override that always wins).
    one_off_quality_ids: set[int] | None = None
    if stack_groups and sum(stack_groups) < MAX_HITTERS and not one_off_active:
        one_off_quality_ids = _one_off_quality_ids(pool)

    if (
        min_ownership_pct is not None
        and max_ownership_pct is not None
        and min_ownership_pct > max_ownership_pct
    ):
        raise OptimizerError("min_ownership_pct can't be more than max_ownership_pct.")

    def _cap_to_count(pct: float) -> int:
        return max(1, round(pct / 100 * num_lineups))

    exposure_count: dict[int, int] = {}
    team_stack_count: dict[str, int] = {}
    excluded_ids: set[int] = set()
    banned_stack_teams: set[str] = set()
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
            banned_stack_teams=banned_stack_teams,
            min_salary=min_salary,
            max_salary=max_salary,
            min_unique_players=min_unique_players,
            min_teams_per_lineup=min_teams_per_lineup,
            max_teams_per_lineup=max_teams_per_lineup,
            one_off_eligible=one_off_eligible,
            one_off_quality_ids=one_off_quality_ids,
            min_ownership_pct=min_ownership_pct,
            max_ownership_pct=max_ownership_pct,
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
        auto_stack_teams: set[str] = result.pop("_auto_stack_teams")
        no_good_cuts.append(player_ids)
        lineups.append(result)

        slot_of: dict[int, str] = {
            p["id"]: slot for slot, players in result["slots"].items() for p in players
        }

        for pid in player_ids:
            exposure_count[pid] = exposure_count.get(pid, 0) + 1
            # Locked players are exempt from the exposure cap -- a lock
            # is a stronger, more explicit instruction than a general
            # exposure limit, and they're guaranteed to reappear anyway.
            if pid in locked:
                continue
            slot_cap_pct = (exposure_by_slot or {}).get(slot_of[pid], max_exposure_pct)
            if slot_cap_pct is not None and exposure_count[pid] >= _cap_to_count(slot_cap_pct):
                excluded_ids.add(pid)

        for t in auto_stack_teams:
            team_stack_count[t] = team_stack_count.get(t, 0) + 1
            cap_pct = (team_exposure_cap or {}).get(t)
            if cap_pct is not None and team_stack_count[t] >= _cap_to_count(cap_pct):
                banned_stack_teams.add(t)

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
    team_exposure = [
        {"team": t, "count": count, "pct": round(100 * count / len(lineups), 1)}
        for t, count in sorted(team_stack_count.items(), key=lambda kv: -kv[1])
    ]

    # How many identical copies of each exact lineup ended up in the
    # set -- always 1 under the default min_unique_players>=1 (no exact
    # repeats allowed), meaningful once min_unique_players=0 lets them
    # through.
    signature_counts = Counter(frozenset(ids) for ids in no_good_cuts)
    for lu, ids in zip(lineups, no_good_cuts):
        lu["duplicate_count"] = signature_counts[frozenset(ids)]

    return {"lineups": lineups, "exposure": exposure, "team_exposure": team_exposure}


def _flatten_slots(slots: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Every player across every slot, in SLOT_TYPES order -- doesn't
    import lineup_export.players_in_slot_order() to avoid a circular
    import (that module imports SLOT_REQUIREMENTS from this one)."""
    out: list[dict[str, Any]] = []
    for slot in SLOT_TYPES:
        out.extend(slots.get(slot, []))
    return out


def late_swap(
    slate: dict[str, Any],
    picks: list[dict[str, Any]],
    *,
    projection_source: str = "rotowire",
) -> dict[str, Any]:
    """
    Given an already-built 10-player lineup and the CURRENT slate,
    re-optimize just the slots whose game hasn't locked yet -- exactly
    what real DK "late swap" allows, no more. A player whose game has
    already started stays exactly as he was (a real DK entry can't be
    touched there either, whether he's playing great or just got
    scratched); a player in a still-open game is fair game to replace,
    whether he's confirmed scratched, a last-minute lineup change, or
    just a worse matchup than when the lineup was first built.

    `picks` is exactly ROSTER_SIZE entries in fixed roster order (P, P,
    C, 1B, 2B, 3B, SS, OF, OF, OF), each `{"player_id": int, "game_pk":
    int}` -- game_pk comes from the SAME slate response the original
    lineup was built from, not re-derived from the player id, since a
    genuinely scratched player can vanish from the current roster
    listing entirely (his team is still playing; the CURRENT slate is
    the source of truth for when that game starts, not for whether this
    specific player is still on it).

    A pick whose game_pk isn't found anywhere in the current slate
    (postponement, a bad id, ...) is conservatively treated as locked
    -- there's no safe way to confirm it's actually still swappable, and
    silently touching a player DK might already consider locked would
    be worse than being over-cautious.
    """
    if len(picks) != ROSTER_SIZE:
        raise OptimizerError(f"late_swap needs exactly {ROSTER_SIZE} picks, got {len(picks)}.")

    game_start_by_pk: dict[int, str] = {
        g.get("game_pk"): g.get("game_time_utc") for g in slate.get("games") or []
    }
    now = datetime.now(timezone.utc)

    locked_ids: list[int] = []
    swappable = False
    unresolved_ids: list[int] = []
    for pick in picks:
        pid = pick["player_id"]
        start_raw = game_start_by_pk.get(pick.get("game_pk"))
        if start_raw is None:
            unresolved_ids.append(pid)
            locked_ids.append(pid)
            continue
        start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        if start <= now:
            locked_ids.append(pid)
        else:
            swappable = True

    if not swappable:
        return {
            "changed": False,
            "message": "Every player's game has already locked -- nothing left to swap.",
            "locked_player_ids": locked_ids,
            "unresolved_player_ids": unresolved_ids,
        }

    result = generate_lineups(
        slate, num_lineups=1, projection_source=projection_source, locked_ids=locked_ids,
    )
    new_lineup = result["lineups"][0]
    new_ids = {p["id"] for p in _flatten_slots(new_lineup["slots"])}
    old_ids = {pick["player_id"] for pick in picks}

    return {
        "changed": new_ids != old_ids,
        "lineup": new_lineup,
        "removed_player_ids": sorted(old_ids - new_ids),
        "added_player_ids": sorted(new_ids - old_ids),
        "locked_player_ids": locked_ids,
        "unresolved_player_ids": unresolved_ids,
    }
