"""
DraftKings Classic MLB lineup optimizer.

Turns the slate's uploaded salary + projection data into the single
highest-scoring lineup that's legal under DK's roster rules -- a
mixed-integer linear program (MILP) solved with PuLP's bundled CBC
solver, free and pure-Python.

This app isn't building its own FPTS projections yet (see the README's
wishlist), so it requires a RotoWire projections CSV already loaded for
the date and maximizes against RotoWire's own FPTS numbers. Salary comes
from a DraftKings salary CSV, same as everywhere else salary shows up in
this app. Both are matched onto each hitter/pitcher already by
mlb_slate.build_slate() -- this module just reads that, it doesn't do
any matching of its own.
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


def generate_lineup(
    slate: dict[str, Any],
    *,
    min_stack: int | None = None,
) -> dict[str, Any]:
    """
    Solve for the single highest-projected-points lineup that fits DK's
    Classic MLB roster and salary cap.

    `min_stack`, if given, requires at least one team to contribute that
    many hitters (pitchers never count toward a stack).
    """
    pool = build_player_pool(slate)
    if not pool:
        raise OptimizerError(
            "No optimizable players for this date -- upload both a "
            "DraftKings salary CSV and a RotoWire projections CSV first."
        )

    prob = pulp.LpProblem("dk_classic_mlb", pulp.LpMaximize)

    # One binary decision variable per (player, eligible slot type) --
    # a multi-eligible player gets one variable for each slot they could
    # fill, and at most one of those is allowed to be 1.
    x = {
        (p["id"], slot): pulp.LpVariable(f"x_{p['id']}_{slot}", cat="Binary")
        for p in pool
        for slot in p["slots"]
    }

    prob += pulp.lpSum(
        p["projected_fpts"] * x[(p["id"], slot)] for p in pool for slot in p["slots"]
    )

    for p in pool:
        prob += pulp.lpSum(x[(p["id"], slot)] for slot in p["slots"]) <= 1

    for slot, count in SLOT_REQUIREMENTS.items():
        eligible = [p for p in pool if slot in p["slots"]]
        prob += pulp.lpSum(x[(p["id"], slot)] for p in eligible) == count

    prob += (
        pulp.lpSum(p["salary"] * x[(p["id"], slot)] for p in pool for slot in p["slots"])
        <= SALARY_CAP
    )

    if min_stack:
        teams = {p["team"] for p in pool}
        # is_stack_team[t] = 1 means "team t is the stack" -- forcing at
        # least one team to hit the min_stack threshold without pinning
        # down which one in advance.
        is_stack_team = {t: pulp.LpVariable(f"stack_{t}", cat="Binary") for t in teams}
        for t in teams:
            hitters_on_team = [p for p in pool if p["team"] == t and "P" not in p["slots"]]
            hitter_count = pulp.lpSum(
                x[(p["id"], slot)] for p in hitters_on_team for slot in p["slots"]
            )
            prob += hitter_count >= min_stack * is_stack_team[t]
        prob += pulp.lpSum(is_stack_team.values()) >= 1

    prob.solve(pulp.PULP_CBC_CMD(msg=0))

    if pulp.LpStatus[prob.status] != "Optimal":
        raise OptimizerError(
            "Couldn't build a legal lineup with the current player pool and "
            "constraints -- try loosening the stack requirement."
        )

    slots_out: dict[str, list[dict[str, Any]]] = {slot: [] for slot in SLOT_TYPES}
    salary_used = 0
    projected_points = 0.0
    for p in pool:
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

    return {
        "salary_used": salary_used,
        "salary_remaining": SALARY_CAP - salary_used,
        "projected_points": round(projected_points, 2),
        "slots": slots_out,
    }
