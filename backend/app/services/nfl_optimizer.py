"""
DraftKings Classic NFL lineup optimizer.

Same MILP-per-lineup approach as optimizer.py (MLB) -- see that module's
docstring for the full multi-lineup rationale, which applies unchanged
here. What's different is the roster shape and the one NFL-specific
mechanic worth having from day one: QB stacking. Pairing a QB with his
own team's pass-catcher is the standard NFL GPP move, for the same
reason MLB hitter-stacking is -- when the QB throws a touchdown, the
receiver who caught it scores too, so their fantasy points are
positively correlated in a way an unstacked build doesn't capture.

Deliberately leaner than optimizer.py's full feature set (no one-off
slot restrictions, no per-lineup ownership bounds, no DK-slate game
filtering yet) -- this ships the core that matters (multi-lineup,
exposure caps, locks/excludes, salary floor, QB stacking) rather than
holding up an NFL optimizer entirely until every MLB-side feature has
an NFL equivalent.
"""

from __future__ import annotations

from typing import Any

import pulp

# DraftKings Classic NFL: one salary cap, nine roster slots.
SALARY_CAP = 50_000
SLOT_REQUIREMENTS = {
    "QB": 1,
    "RB": 2,
    "WR": 3,
    "TE": 1,
    "FLEX": 1,
    "DST": 1,
}
SLOT_TYPES = list(SLOT_REQUIREMENTS)
ROSTER_SIZE = sum(SLOT_REQUIREMENTS.values())
FLEX_POSITIONS = {"RB", "WR", "TE"}

MAX_LINEUPS = 150


class OptimizerError(ValueError):
    """No legal lineup could be built -- missing data or an infeasible
    constraint. Routed to an HTTP 400 by the caller."""


def _eligible_slots(position: str) -> list[str]:
    pos = (position or "").strip().upper()
    if pos == "QB":
        return ["QB"]
    if pos == "DST":
        return ["DST"]
    if pos in FLEX_POSITIONS:
        return [pos, "FLEX"]
    return []


def build_player_pool(slate: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Flatten every rostered player across the slate's games into one
    optimizable pool. Skips anyone missing a matched salary or
    projection, or with a position that doesn't map to a roster slot.
    """
    pool: list[dict[str, Any]] = []
    for game in slate.get("games") or []:
        for side in ("home", "away"):
            team = game[side]
            opponent = game["away" if side == "home" else "home"]["abbrev"]
            for p in team.get("players") or []:
                if not p.get("dk_id") or p.get("salary") is None:
                    continue
                proj = p.get("projection")
                if not proj or proj.get("fpts") is None:
                    continue
                position = (p.get("position") or "").strip().upper()
                slots = _eligible_slots(position)
                if not slots:
                    continue
                pool.append(
                    {
                        "id": p["dk_id"],
                        "name": p["name"],
                        "team": team["abbrev"],
                        "opponent": opponent,
                        "position": position,
                        "salary": p["salary"],
                        "projected_fpts": proj["fpts"],
                        "ownership_pct": proj.get("ownership_pct") or 0,
                        "slots": slots,
                        "is_pass_catcher": p.get("position") in ("WR", "TE"),
                    }
                )
    return pool


def _solve_one(
    pool: list[dict[str, Any]],
    *,
    excluded_ids: set[str],
    no_good_cuts: list[set[str]],
    locked_ids: set[str],
    min_salary: int | None,
    min_unique_players: int,
    qb_stack_min: int,
) -> dict[str, Any] | None:
    usable = [p for p in pool if p["id"] not in excluded_ids]
    if not usable:
        return None

    usable_ids = {p["id"] for p in usable}
    if not locked_ids <= usable_ids:
        return None

    prob = pulp.LpProblem("dk_classic_nfl", pulp.LpMaximize)

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

    prob += pulp.lpSum(p["salary"] * x[(p["id"], slot)] for p in usable for slot in p["slots"]) <= SALARY_CAP
    if min_salary is not None:
        prob += pulp.lpSum(p["salary"] * x[(p["id"], slot)] for p in usable for slot in p["slots"]) >= min_salary

    if qb_stack_min > 0:
        all_teams = sorted({p["team"] for p in usable})
        for t in all_teams:
            qb_from_team = pulp.lpSum(
                x[(p["id"], "QB")] for p in usable if p["team"] == t and "QB" in p["slots"]
            )
            pass_catchers_from_team = pulp.lpSum(
                x[(p["id"], slot)]
                for p in usable
                if p["team"] == t and p["is_pass_catcher"]
                for slot in p["slots"]
            )
            # If team t's QB is the one rostered, at least qb_stack_min of
            # his own WR/TE must be too. qb_from_team is 0 or 1 (exactly
            # one QB slot exists), so this is a direct linear constraint --
            # no big-M gating needed, unlike MLB's multi-group stacking.
            prob += pass_catchers_from_team >= qb_stack_min * qb_from_team

    for prior_ids in no_good_cuts:
        prior_in_pool = [p for p in usable if p["id"] in prior_ids]
        if len(prior_in_pool) < ROSTER_SIZE:
            continue
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
    player_ids: set[str] = set()
    for p in usable:
        for slot in p["slots"]:
            if round(x[(p["id"], slot)].value() or 0) == 1:
                slots_out[slot].append(
                    {
                        "id": p["id"],
                        "name": p["name"],
                        "team": p["team"],
                        "opponent": p["opponent"],
                        "position": p["position"],
                        "salary": p["salary"],
                        "projected_fpts": p["projected_fpts"],
                        "ownership_pct": p["ownership_pct"],
                    }
                )
                salary_used += p["salary"]
                projected_points += p["projected_fpts"]
                total_ownership_pct += p["ownership_pct"]
                player_ids.add(p["id"])

    return {
        "salary_used": salary_used,
        "salary_remaining": SALARY_CAP - salary_used,
        "projected_points": round(projected_points, 2),
        "total_ownership_pct": round(total_ownership_pct, 1),
        "slots": slots_out,
        "_player_ids": player_ids,
    }


def generate_lineups(
    slate: dict[str, Any],
    *,
    num_lineups: int = 1,
    max_exposure_pct: float | None = None,
    exposure_by_slot: dict[str, float] | None = None,
    locked_ids: list[str] | None = None,
    excluded_ids: list[str] | None = None,
    min_salary: int | None = None,
    min_unique_players: int = 1,
    qb_stack_min: int = 0,
) -> dict[str, Any]:
    """
    Generate up to `num_lineups` distinct legal DK Classic NFL lineups
    (QB, RB, RB, WR, WR, WR, TE, FLEX, DST -- $50,000 cap).

    `qb_stack_min`, if given, forces at least that many of the rostered
    QB's own WR/TEs into the same lineup (the standard NFL GPP stack).

    See optimizer.py's `generate_lineups()` for the shared semantics of
    `max_exposure_pct`, `exposure_by_slot`, `locked_ids`/`excluded_ids`,
    `min_salary`, and `min_unique_players` -- identical here.
    """
    if num_lineups < 1:
        raise OptimizerError("num_lineups must be at least 1.")
    if num_lineups > MAX_LINEUPS:
        raise OptimizerError(f"Generating more than {MAX_LINEUPS} lineups at once isn't supported.")

    pool = build_player_pool(slate)
    if not pool:
        raise OptimizerError(
            "No optimizable players for this week -- upload both a "
            "DraftKings salary CSV and a RotoWire projections CSV first."
        )

    locked = {str(i) for i in (locked_ids or [])}
    user_excluded = {str(i) for i in (excluded_ids or [])}
    overlap = locked & user_excluded
    if overlap:
        raise OptimizerError(f"Can't both lock and exclude the same player(s): {sorted(overlap)}.")

    pool = [p for p in pool if p["id"] not in user_excluded]
    if not pool:
        raise OptimizerError("Excluding those players leaves nobody left to build a lineup from.")

    pool_ids = {p["id"] for p in pool}
    missing_locks = locked - pool_ids
    if missing_locks:
        raise OptimizerError(
            f"Locked player id(s) aren't in this week's optimizable pool: {sorted(missing_locks)}."
        )
    if len(locked) > ROSTER_SIZE:
        raise OptimizerError(f"Locked {len(locked)} players, but a lineup only has {ROSTER_SIZE} slots.")

    if exposure_by_slot:
        bad_slots = set(exposure_by_slot) - set(SLOT_TYPES)
        if bad_slots:
            raise OptimizerError(f"Unknown roster slot(s) in exposure_by_slot: {sorted(bad_slots)}.")

    if min_salary is not None and min_salary > SALARY_CAP:
        raise OptimizerError(f"min_salary ({min_salary}) can't be more than the ${SALARY_CAP} salary cap.")

    if not (1 <= min_unique_players <= ROSTER_SIZE):
        raise OptimizerError(f"min_unique_players must be between 1 and {ROSTER_SIZE}.")

    if qb_stack_min < 0 or qb_stack_min > 3:
        raise OptimizerError("qb_stack_min must be between 0 and 3 (a team only has so many pass-catchers).")

    def _cap_to_count(pct: float) -> int:
        return max(1, round(pct / 100 * num_lineups))

    exposure_count: dict[str, int] = {}
    excluded: set[str] = set()
    no_good_cuts: list[set[str]] = []
    lineups: list[dict[str, Any]] = []

    for i in range(num_lineups):
        result = _solve_one(
            pool,
            excluded_ids=excluded,
            no_good_cuts=no_good_cuts,
            locked_ids=locked,
            min_salary=min_salary,
            min_unique_players=min_unique_players,
            qb_stack_min=qb_stack_min,
        )
        if result is None:
            if i == 0:
                raise OptimizerError(
                    "Couldn't build a legal lineup with the current player pool and "
                    "constraints -- try loosening the exposure cap, salary floor, "
                    "stack requirement, or locked players."
                )
            break

        player_ids: set[str] = result.pop("_player_ids")
        no_good_cuts.append(player_ids)
        lineups.append(result)

        slot_of: dict[str, str] = {
            p["id"]: slot for slot, players in result["slots"].items() for p in players
        }
        for pid in player_ids:
            exposure_count[pid] = exposure_count.get(pid, 0) + 1
            if pid in locked:
                continue
            slot_cap_pct = (exposure_by_slot or {}).get(slot_of[pid], max_exposure_pct)
            if slot_cap_pct is not None and exposure_count[pid] >= _cap_to_count(slot_cap_pct):
                excluded.add(pid)

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
