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
# Applied automatically unless the caller overrides it (0 disables the
# floor entirely) -- a lineup leaving thousands of dollars unspent is
# almost always leaving real projected points on the table, so this is
# a sensible default rather than an opt-in. Mirrors MLB's own
# optimizer.DEFAULT_MIN_SALARY, which NFL was missing entirely: every
# NFL entry point defaulted the floor to 0, so 23% of a real generated
# batch came back under $47,000 (worst: $40,500 -- nearly $10k unspent).
DEFAULT_MIN_SALARY = 47_000
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


# Which numbers a pool is built from. Mirrors optimizer.py's own map
# exactly, so "inhouse" means the same thing on both sports.
PROJECTION_SOURCES = {
    "rotowire": ("fpts", "ownership_pct"),
    "inhouse": ("inhouse_fpts", "inhouse_ownership_pct"),
}


def build_player_pool(
    slate: dict[str, Any],
    *,
    included_game_pks: list[Any] | None = None,
    projection_source: str = "rotowire",
) -> list[dict[str, Any]]:
    """
    Flatten every rostered player across the slate's games into one
    optimizable pool. Skips anyone missing a matched salary or
    projection, or with a position that doesn't map to a roster slot.

    `included_game_pks`, if given, narrows the pool to those games. It
    matters more in football than in baseball: a week has 16 games but
    the Main DK slate has 12, so without it the optimizer would happily
    roster a Thursday player into a Sunday-only lineup.

    Each entry also carries `edge_composite` -- nfl_scoring's matchup
    multiplier -- which neither lineup engine optimizes against, but
    which the contest field sampler reads to model a sharp field.
    Mirrors MLB's pool exactly.
    """
    if projection_source not in PROJECTION_SOURCES:
        raise OptimizerError(
            f"Unknown projection_source '{projection_source}'. "
            f"Expected one of: {sorted(PROJECTION_SOURCES)}."
        )
    fpts_key, ownership_key = PROJECTION_SOURCES[projection_source]

    wanted = {str(g) for g in included_game_pks} if included_game_pks else None
    pool: list[dict[str, Any]] = []
    for game in slate.get("games") or []:
        if wanted is not None and str(game.get("game_id")) not in wanted:
            continue
        for side in ("home", "away"):
            team = game[side]
            opponent = game["away" if side == "home" else "home"]["abbrev"]
            for p in team.get("players") or []:
                if not p.get("dk_id") or p.get("salary") is None:
                    continue
                proj = p.get("projection")
                if not proj or proj.get(fpts_key) is None:
                    continue
                position = (p.get("position") or "").strip().upper()
                slots = _eligible_slots(position)
                if not slots:
                    continue
                pool.append(
                    {
                        "id": p["dk_id"],
                        "nflverse_id": p.get("nflverse_id"),
                        "name": p["name"],
                        "team": team["abbrev"],
                        "opponent": opponent,
                        "position": position,
                        "salary": p["salary"],
                        "projected_fpts": proj[fpts_key],
                        "ownership_pct": proj.get(ownership_key) or 0,
                        "slots": slots,
                        # nfl_scoring's own matchup multiplier (1.00 =
                        # dead average), under the same name MLB's pool
                        # uses so anything reading a pool entry works for
                        # either sport. Neither lineup engine optimizes
                        # against it; the contest field sampler reads it
                        # to model a SHARP field, which needs to tell a
                        # low-owned player in a good spot from a
                        # low-owned player in a bad one. Without it that
                        # model degrades to picking cheap names, which
                        # measured worse than an ordinary field.
                        "edge_composite": (p.get("edge") or {}).get("composite"),
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
    max_salary: int | None,
    min_unique_players: int,
    qb_stack_min: int,
    bring_back_min: int,
    stack_team: str | None,
    banned_stack_teams: set[str] | None = None,
    min_teams_per_lineup: int | None,
    max_teams_per_lineup: int | None,
    min_ownership_pct: float | None,
    max_ownership_pct: float | None,
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

    spend = pulp.lpSum(p["salary"] * x[(p["id"], slot)] for p in usable for slot in p["slots"])
    prob += spend <= min(max_salary, SALARY_CAP) if max_salary is not None else spend <= SALARY_CAP
    if min_salary is not None:
        prob += spend >= min_salary

    # Cumulative ownership, the same lever MLB's optimizer carries: a
    # linear sum over the roster, bounded either side.
    if min_ownership_pct is not None or max_ownership_pct is not None:
        owned = pulp.lpSum(
            p["ownership_pct"] * x[(p["id"], slot)] for p in usable for slot in p["slots"]
        )
        if min_ownership_pct is not None:
            prob += owned >= min_ownership_pct
        if max_ownership_pct is not None:
            prob += owned <= max_ownership_pct

    all_teams = sorted({p["team"] for p in usable})

    # Force the stack onto one specific team, by forcing that team's QB
    # into the lineup. Everything qb_stack_min/bring_back_min already do
    # then hangs off him, so this is one constraint rather than a
    # parallel set.
    if stack_team:
        stack_qbs = [
            p for p in usable if p["team"] == stack_team and "QB" in p["slots"]
        ]
        if not stack_qbs:
            return None
        prob += pulp.lpSum(x[(p["id"], "QB")] for p in stack_qbs) == 1

    # A team that has hit its stack cap can still supply one-offs and
    # bring-backs; it just can't be the team the lineup is built around,
    # which is the team whose QB is rostered.
    if banned_stack_teams:
        banned_qbs = [
            p for p in usable if p["team"] in banned_stack_teams and "QB" in p["slots"]
        ]
        if banned_qbs:
            prob += pulp.lpSum(x[(p["id"], "QB")] for p in banned_qbs) == 0

    # BRING-BACK: players from the other side of the stacked QB's own
    # game. This is the NFL counterpart to MLB's secondary stack group,
    # and the reason it is expressed in opponents rather than in a
    # second team is that a bring-back is only a bring-back if it is the
    # SAME game -- a runner-up stack from an unrelated game correlates
    # with nothing.
    if bring_back_min > 0:
        for t in all_teams:
            qb_from_team = pulp.lpSum(
                x[(p["id"], "QB")] for p in usable if p["team"] == t and "QB" in p["slots"]
            )
            opponents = {p["opponent"] for p in usable if p["team"] == t and p.get("opponent")}
            bring_back = pulp.lpSum(
                x[(p["id"], slot)]
                for p in usable
                if p["team"] in opponents and p["position"] != "DST"
                for slot in p["slots"]
            )
            prob += bring_back >= bring_back_min * qb_from_team

    # How many distinct teams a lineup may draw from. y_t is 1 exactly
    # when team t contributes anybody, pinned from both sides so it
    # cannot float.
    if min_teams_per_lineup is not None or max_teams_per_lineup is not None:
        y = {t: pulp.LpVariable(f"team_{t}", cat="Binary") for t in all_teams}
        for t in all_teams:
            from_team = [p for p in usable if p["team"] == t]
            used = pulp.lpSum(x[(p["id"], slot)] for p in from_team for slot in p["slots"])
            prob += used <= ROSTER_SIZE * y[t]
            prob += used >= y[t]
        if min_teams_per_lineup is not None:
            prob += pulp.lpSum(y.values()) >= min_teams_per_lineup
        if max_teams_per_lineup is not None:
            prob += pulp.lpSum(y.values()) <= max_teams_per_lineup

    if qb_stack_min > 0:
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
                        "nflverse_id": p.get("nflverse_id"),
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
    projection_source: str = "rotowire",
    max_exposure_pct: float | None = None,
    exposure_by_slot: dict[str, float] | None = None,
    team_exposure_cap: dict[str, float] | None = None,
    locked_ids: list[str] | None = None,
    excluded_ids: list[str] | None = None,
    min_salary: int | None = None,
    max_salary: int | None = None,
    min_unique_players: int = 1,
    qb_stack_min: int = 0,
    bring_back_min: int = 0,
    stack_team: str | None = None,
    min_teams_per_lineup: int | None = None,
    max_teams_per_lineup: int | None = None,
    min_ownership_pct: float | None = None,
    max_ownership_pct: float | None = None,
    included_game_pks: list[Any] | None = None,
) -> dict[str, Any]:
    """
    Generate up to `num_lineups` distinct legal DK Classic NFL lineups
    (QB, RB, RB, WR, WR, WR, TE, FLEX, DST -- $50,000 cap).

    `qb_stack_min`, if given, forces at least that many of the rostered
    QB's own WR/TEs into the same lineup (the standard NFL GPP stack).

    See optimizer.py's `generate_lineups()` for the shared semantics of
    `max_exposure_pct`, `exposure_by_slot`, `locked_ids`/`excluded_ids`,
    `min_salary`/`max_salary`, `min_unique_players`,
    `min_teams_per_lineup`/`max_teams_per_lineup`,
    `min_ownership_pct`/`max_ownership_pct` and `included_game_pks` --
    identical here.

    STACKING IS EXPRESSED IN FOOTBALL, NOT IN MLB'S SHAPES. MLB names a
    stack by how many bats come from each team ("5-3", "4-2-2"), because
    a baseball stack is a batting order. That vocabulary does not
    transfer: an NFL stack is a QUARTERBACK plus his own pass catchers,
    and its second half is a bring-back from the other side of that same
    game. So the controls here are `qb_stack_min` (how many of the
    rostered QB's own WR/TEs come with him), `bring_back_min` (how many
    from the opponent he is actually playing), and `stack_team` (which
    team to build it around). Between them they express every real NFL
    stack shape -- and `bring_back_min` is defined against the QB's
    OPPONENT rather than a second named team, because a runner-up stack
    from an unrelated game is not a bring-back and correlates with
    nothing.

    `team_exposure_cap` caps how often a team is used AS THE STACK --
    the team whose QB is rostered -- not how often its players appear
    incidentally, matching MLB's own meaning of the same argument.
    """
    if num_lineups < 1:
        raise OptimizerError("num_lineups must be at least 1.")
    if num_lineups > MAX_LINEUPS:
        raise OptimizerError(f"Generating more than {MAX_LINEUPS} lineups at once isn't supported.")

    pool = build_player_pool(
        slate, included_game_pks=included_game_pks, projection_source=projection_source
    )
    if not pool:
        if included_game_pks:
            # Worth saying separately: a week has more games than a DK
            # slate does, so narrowing to a game that is not ON the
            # loaded slate is a normal mistake with a very confusing
            # generic message.
            raise OptimizerError(
                "No optimizable players in the selected game(s) -- they may not be part "
                "of the DraftKings slate that is currently loaded."
            )
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

    if bring_back_min < 0 or bring_back_min > 3:
        raise OptimizerError("bring_back_min must be between 0 and 3.")

    if max_salary is not None:
        if max_salary > SALARY_CAP:
            raise OptimizerError(f"max_salary can't exceed the ${SALARY_CAP} cap.")
        if min_salary is not None and min_salary > max_salary:
            raise OptimizerError(f"min_salary ({min_salary}) is above max_salary ({max_salary}).")

    pool_teams = {p["team"] for p in pool}
    if stack_team and stack_team not in pool_teams:
        raise OptimizerError(
            f"stack_team {stack_team!r} isn't on this slate. Available: {sorted(pool_teams)}."
        )
    if stack_team and not any(
        p["team"] == stack_team and "QB" in p["slots"] for p in pool
    ):
        raise OptimizerError(
            f"{stack_team} has no optimizable quarterback, so a stack can't be built around it."
        )

    for label, value in (("min_teams_per_lineup", min_teams_per_lineup),
                         ("max_teams_per_lineup", max_teams_per_lineup)):
        if value is not None and not (1 <= value <= ROSTER_SIZE):
            raise OptimizerError(f"{label} must be between 1 and {ROSTER_SIZE}.")
    if (min_teams_per_lineup is not None and max_teams_per_lineup is not None
            and min_teams_per_lineup > max_teams_per_lineup):
        raise OptimizerError("min_teams_per_lineup is above max_teams_per_lineup.")

    if (min_ownership_pct is not None and max_ownership_pct is not None
            and min_ownership_pct > max_ownership_pct):
        raise OptimizerError("min_ownership_pct is above max_ownership_pct.")

    if team_exposure_cap:
        unknown = set(team_exposure_cap) - pool_teams
        if unknown:
            raise OptimizerError(f"team_exposure_cap names team(s) not on this slate: {sorted(unknown)}.")

    def _cap_to_count(pct: float) -> int:
        return max(1, round(pct / 100 * num_lineups))

    exposure_count: dict[str, int] = {}
    stack_team_count: dict[str, int] = {}
    excluded: set[str] = set()
    no_good_cuts: list[set[str]] = []
    lineups: list[dict[str, Any]] = []
    # Teams that have hit their stack cap. Excluded from being the STACK
    # team only -- their players stay eligible as one-offs and
    # bring-backs, which is what "team exposure" means here and on the
    # MLB side.
    capped_stack_teams: set[str] = set()

    for i in range(num_lineups):
        allowed_stack = stack_team
        if team_exposure_cap and not allowed_stack:
            available = sorted(pool_teams - capped_stack_teams)
            if not available:
                break
        result = _solve_one(
            pool,
            excluded_ids=excluded,
            no_good_cuts=no_good_cuts,
            locked_ids=locked,
            min_salary=min_salary,
            max_salary=max_salary,
            min_unique_players=min_unique_players,
            qb_stack_min=qb_stack_min,
            bring_back_min=bring_back_min,
            stack_team=allowed_stack,
            banned_stack_teams=capped_stack_teams if team_exposure_cap else None,
            min_teams_per_lineup=min_teams_per_lineup,
            max_teams_per_lineup=max_teams_per_lineup,
            min_ownership_pct=min_ownership_pct,
            max_ownership_pct=max_ownership_pct,
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

        # Whose stack this actually turned out to be -- read off the
        # rostered QB rather than off what was asked for, so the cap
        # counts what was built.
        qbs = result["slots"].get("QB") or []
        built_stack_team = qbs[0]["team"] if qbs else None
        result["stack_team"] = built_stack_team
        if built_stack_team:
            stack_team_count[built_stack_team] = stack_team_count.get(built_stack_team, 0) + 1
            cap_pct = (team_exposure_cap or {}).get(built_stack_team)
            if cap_pct is not None and stack_team_count[built_stack_team] >= _cap_to_count(cap_pct):
                capped_stack_teams.add(built_stack_team)

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

    stack_exposure = [
        {
            "team": team,
            "count": count,
            "pct": round(100 * count / len(lineups), 1) if lineups else 0.0,
        }
        for team, count in sorted(stack_team_count.items(), key=lambda kv: -kv[1])
    ]

    return {"lineups": lineups, "exposure": exposure, "stack_exposure": stack_exposure}
