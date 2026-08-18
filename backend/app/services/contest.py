"""
The contest generator: a fast, large-scale sibling of optimizer.py for
two different jobs that happen to share one algorithm.

1. **Your own entries** (`generate_entries` / `build_contest_entries`).
   optimizer.py's exact MILP solve is the right tool for a handful of
   genuinely-best lineups (capped at 150, see MAX_LINEUPS there), but
   it's the wrong tool for mass multi-entry -- solving a fresh MILP
   thousands of times is both too slow to run in one request and not
   even what you'd want, since a real GPP portfolio wants many
   individually-strong but genuinely different builds, not the same
   "best" lineup solved 5,000 times with weaker and weaker no-good
   cuts. Instead, each entry is built by fast randomized construction,
   weighted heavily toward higher-projected players (see
   `_fpts_weight`), deduplicated against every other entry in the
   batch. Capped at MAX_USER_LINEUPS (10,000).

2. **The opponent field** (`generate_field`). A real contest's field
   isn't a pile of optimal lineups -- it's skewed toward whatever's
   popular. This builds synthetic field entries the same way, just
   weighted by RotoWire ownership% instead (`_ownership_weight`) --
   the signal that actually describes what the public rosters -- and
   deliberately allows duplicate/near-duplicate entries, since real
   chalk-heavy fields cluster that way. Used as the baseline `your own
   entries` gets ranked against for cash-line/payout estimates.

Neither of these is a lineup simulator -- there's no player-outcome
variance model yet, so ranking is against the field's *projected*
points, not a distribution of real-world outcomes. That's a real
limitation, stated plainly rather than dressed up: this tool answers
"is my build structurally different from the field, and roughly where
would it land," which is still a useful input even without a variance
model. The simulator is a bigger, separate follow-up (see the README
roadmap).

A large real contest (thousands to 100,000+ entries) is modeled as a
*sample* for the opponent side, not literally reproduced one entry at
a time -- MAX_SAMPLE_SIZE caps how many synthetic field lineups
actually get built, and a lineup's rank within that sample gets
projected back onto the real field_size statistically (the same idea
as a poll sampling a fraction of a population).

Both generators reuse optimizer.build_player_pool() for the candidate
pool -- same salary/projection/scratch eligibility rules, same
`included_game_pks` slate filter, so everything is drawn from the
exact same DK slate, not every game MLB's schedule happens to return
for the date.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import Any

import numpy as np

from app.services import variance
from app.services.lineup_export import stack_info
from app.services.optimizer import SALARY_CAP, SLOT_REQUIREMENTS, SLOT_TYPES, build_player_pool

MAX_SAMPLE_SIZE = 5000
MAX_FIELD_SIZE = 200_000
MAX_USER_LINEUPS = 10_000
_OWNERSHIP_FLOOR = 0.5  # even an unowned player gets a sliver of sampling weight

# DraftKings Classic MLB's own roster rule: no more than 5 hitters from
# the same team in a single lineup. STACK_SHAPES below never targets
# more than 5 in one group, but a shape's genuine leftover/free picks
# (e.g. "5"'s 3 unconstrained slots) were never prevented from
# coincidentally landing on the same already-stacked team, which could
# silently build an illegal 6-, 7-, or even 8-stack -- enforced as a
# hard cap on every hitter slot's eligible pool below, independent of
# whether a stack shape is even in play.
MAX_HITTERS_PER_TEAM = 5

# How hard entry generation leans toward higher-projected players.
# fpts is raised to this power before sampling -- aggressive enough
# that entries are genuinely strong (not just plausible), gentle
# enough that weaker/cheaper players still get picked when a stronger
# one has already been used or doesn't fit the remaining budget.
_FPTS_SAMPLING_EXPONENT = 3.0
_FPTS_FLOOR = 0.1

# Named GPP stack shapes every generated lineup is deliberately built
# toward -- team-group sizes only, largest first. A trailing size-1
# group (e.g. "5-2-1"'s final "1") isn't a real constraint -- a single
# player has nothing to stack with -- so it's represented the same as
# the shape one size shorter ("5-2-1" -> [5, 2], same as "5-2" would
# be); the leftover slot is just an ordinary free pick like any
# partial shape's leftovers, same as optimizer.py's own stack-shape
# feature already treats them. Left unconstrained (no shape at all)
# isn't offered here on purpose -- without this, independent per-slot
# sampling produced all sorts of shapes nobody would actually build for
# a real GPP (single mega-stacks, no-stack spreads across 8 teams),
# unevenly and without any deliberate control.
STACK_SHAPES: list[list[int]] = [
    [5, 3],
    [5, 2],  # "5-2-1"
    [5],
    [4, 4],
    [4, 3],
    [4, 2, 2],
    [4, 2],
    [3, 3, 2],
    [3, 3],
]
# Weighted toward the shapes that most often win real large-field GPPs
# (5-3, 5-2-1) without ruling the rest out entirely -- simple rank-based
# decay, first-listed shape heaviest, ~5x the last-listed shape's
# weight. Tune the decay constant here if the mix needs to shift;
# nothing else needs to change.
_STACK_SHAPE_DECAY = 0.8
STACK_SHAPE_WEIGHTS: list[float] = [_STACK_SHAPE_DECAY**i for i in range(len(STACK_SHAPES))]

# Named presets covering the common DK contest shapes. `rake_pct` and
# the payout curve below are a simplified, clearly-approximate model of
# how real payout tables behave (top-heavy for GPPs, flat for
# double-ups) -- not scraped or hardcoded from any specific live
# contest, which would go stale immediately and varies contest to
# contest anyway.
CONTEST_TYPES: dict[str, dict[str, Any]] = {
    "double_up": {
        "label": "Double-up / 50-50",
        "field_size": 100,
        "entry_fee": 10.0,
        "payout_pct": 0.45,
        "shape": "flat",
    },
    "gpp_small": {
        "label": "Small-field GPP (3-max)",
        "field_size": 500,
        "entry_fee": 10.0,
        "payout_pct": 0.20,
        "shape": "top_heavy",
    },
    "gpp_large": {
        "label": "Large-field GPP",
        "field_size": 10_000,
        "entry_fee": 5.0,
        "payout_pct": 0.18,
        "shape": "top_heavy",
    },
    "gpp_milly": {
        "label": "Massive-field GPP (millionaire-maker style)",
        "field_size": 100_000,
        "entry_fee": 20.0,
        "payout_pct": 0.15,
        "shape": "top_heavy",
    },
}
RAKE_PCT = 0.15
TOP_HEAVY_EXPONENT = 0.7


class ContestError(ValueError):
    """Bad contest params or nothing to build a field from -- routed to an HTTP 400."""


def _ownership_weight(p: dict[str, Any]) -> float:
    return max(p["ownership_pct"], _OWNERSHIP_FLOOR)


def _fpts_weight(p: dict[str, Any]) -> float:
    return max(p["projected_fpts"], _FPTS_FLOOR) ** _FPTS_SAMPLING_EXPONENT


def _team_hitter_pools(
    candidates_by_slot: dict[str, list[dict[str, Any]]], slot_order: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """
    Distinct hitters (deduplicated by id, since a multi-eligible player
    shows up under more than one slot's candidate list) available per
    team, across every non-pitcher slot -- the pool _pick_stack_teams()
    assigns stack groups from, and a cheap proxy for whether a team can
    plausibly support a given group size at all.
    """
    hitter_slot_types = set(slot_order[SLOT_REQUIREMENTS["P"] :])
    by_team: dict[str, dict[int, dict[str, Any]]] = {}
    for slot in hitter_slot_types:
        for p in candidates_by_slot[slot]:
            by_team.setdefault(p["team"], {})[p["id"]] = p
    return {team: list(players.values()) for team, players in by_team.items()}


def _feasible_stack_shapes(
    team_hitter_pools: dict[str, list[dict[str, Any]]],
) -> tuple[list[list[int]], list[float]]:
    """
    STACK_SHAPES/STACK_SHAPE_WEIGHTS filtered down to only shapes this
    specific candidate pool could possibly satisfy -- a shape needing
    more team-groups than there are distinct teams with any hitters at
    all, or whose biggest group needs more hitters than even the
    deepest team has, can never succeed no matter how many random
    attempts it gets. Excluding those up front matters because a
    structurally-impossible shape would otherwise burn every one of a
    lineup's retry attempts on a guaranteed failure -- and
    generate_field()/generate_entries() give up on the *whole batch*
    once a single lineup slot exhausts its retries, so one unlucky
    infeasible shape draw could silently cut a large request short.
    Real slates have plenty of teams and depth for this to rarely
    matter in practice; a single-game slate (2 teams) or a team with a
    thin confirmed lineup is where it actually kicks in. Pairs each
    shape's groups (sorted largest-first) against the pool's team
    sizes (also sorted largest-first) -- the same best-case pairing
    _pick_stack_teams() would need to succeed, so this is a tight,
    cheap necessary condition, not just a rough guess.
    """
    team_sizes = sorted((len(players) for players in team_hitter_pools.values()), reverse=True)
    shapes: list[list[int]] = []
    weights: list[float] = []
    for shape, weight in zip(STACK_SHAPES, STACK_SHAPE_WEIGHTS):
        sorted_shape = sorted(shape, reverse=True)
        if len(sorted_shape) <= len(team_sizes) and all(
            size <= team_size for size, team_size in zip(sorted_shape, team_sizes)
        ):
            shapes.append(shape)
            weights.append(weight)
    return shapes, weights


def _pick_stack_shape(
    shapes: list[list[int]], weights: list[float], rng: random.Random
) -> list[int] | None:
    """
    One of `shapes` (weighted by `weights`) -- pass the result of
    _feasible_stack_shapes() for this specific candidate pool. Returns
    None if no shape is feasible at all (e.g. even the thinnest team
    has fewer hitters than the smallest shape needs); the caller should
    fall back to fully unconstrained sampling for that lineup rather
    than fail it outright.
    """
    if not shapes:
        return None
    return rng.choices(shapes, weights=weights, k=1)[0]


def _pick_stack_teams(
    team_hitter_pools: dict[str, list[dict[str, Any]]],
    groups: list[int],
    weight_fn: Callable[[dict[str, Any]], float],
    rng: random.Random,
) -> dict[str, int] | None:
    """
    Assign each of `groups`' sizes to a real team, largest group first --
    weighted toward whichever teams carry the most aggregate `weight_fn`
    signal (ownership%/projected points) among their available hitters,
    so a stack still tends to land on the teams that would realistically
    get stacked. A team already claimed by a bigger group in this same
    lineup is excluded from smaller ones; a team without enough distinct
    hitters to plausibly fill a group is excluded outright. Returns None
    when no feasible team exists for some group -- the caller
    (_sample_one_lineup) returns None too, and the per-lineup retry loop
    tries again with a fresh team assignment for the *same* shape (see
    generate_field()/generate_entries() -- the shape itself is picked
    once per lineup, outside this retry loop, precisely so a genuinely
    harder shape like 5-3 gets a fair, dedicated shot instead of losing
    out to whichever easier shape happens to get rolled on a given
    attempt).
    """
    assigned: dict[str, int] = {}
    used_teams: set[str] = set()
    for size in groups:
        candidates = {
            team: players
            for team, players in team_hitter_pools.items()
            if team not in used_teams and len(players) >= size
        }
        if not candidates:
            return None
        teams = list(candidates)
        weights = [sum(weight_fn(p) for p in candidates[t]) for t in teams]
        team = rng.choices(teams, weights=weights, k=1)[0]
        assigned[team] = size
        used_teams.add(team)
    return assigned


def _sample_one_lineup(
    candidates_by_slot: dict[str, list[dict[str, Any]]],
    slot_order: list[str],
    rng: random.Random,
    weight_fn: Callable[[dict[str, Any]], float],
    *,
    excluded_ids: frozenset[int] = frozenset(),
    team_hitter_pools: dict[str, list[dict[str, Any]]] | None = None,
    stack_groups: list[int] | None = None,
) -> dict[str, Any] | None:
    """
    Build one randomly-weighted lineup within the salary cap. At each
    slot, the pick is weighted by `weight_fn` (ownership% for the
    opponent field, projected points for the user's own entries) but
    constrained to what still leaves enough budget for the cheapest
    possible player at every remaining slot -- a standard feasible
    random-roster-construction technique. `excluded_ids` removes
    players entirely (e.g. ones that have hit an exposure cap).

    Every hitter slot also enforces MAX_HITTERS_PER_TEAM (DraftKings'
    own 5-hitters-per-team roster rule) regardless of any stack target
    below -- without this, a shape's genuine leftover/free picks could
    coincidentally land on an already-stacked team and build an illegal
    6+ stack.

    `stack_groups`/`team_hitter_pools`, if both given, constrain this
    lineup's 8 hitters to the caller-chosen shape via _pick_stack_teams()
    -- a needed team's eligible players are preferred at each HITTER
    slot until that team's group is filled (the 2 pitcher slots are
    never constrained by this -- a stack is a hitter-only concept),
    falling back to an ordinary unconstrained pick once every group is
    satisfied (or immediately, for a partial shape's genuine leftover
    slots). If the chosen shape's groups can't all be filled by the time
    every slot is walked, the whole attempt fails just like any other
    infeasible random walk here.

    Returns None if this particular random walk couldn't complete; the
    caller just retries.
    """
    stack_remaining: dict[str, int] = {}
    if stack_groups is not None and team_hitter_pools is not None:
        assignment = _pick_stack_teams(team_hitter_pools, stack_groups, weight_fn, rng)
        if assignment is None:
            return None
        stack_remaining = assignment

    used_ids: set[int] = set()
    picks: list[dict[str, Any]] = []
    salary_so_far = 0
    team_hitter_count: dict[str, int] = {}

    for i, slot in enumerate(slot_order):
        remaining_slots = slot_order[i + 1 :]
        eligible = [
            p for p in candidates_by_slot[slot] if p["id"] not in used_ids and p["id"] not in excluded_ids
        ]
        if not eligible:
            return None

        if slot != "P":
            eligible = [p for p in eligible if team_hitter_count.get(p["team"], 0) < MAX_HITTERS_PER_TEAM]
            if not eligible:
                return None

            needed_teams = {t for t, n in stack_remaining.items() if n > 0}
            if needed_teams:
                restricted = [p for p in eligible if p["team"] in needed_teams]
                if restricted:
                    eligible = restricted

        min_cost_of_rest = sum(
            min(
                (
                    p["salary"]
                    for p in candidates_by_slot[s]
                    if p["id"] not in used_ids and p["id"] not in excluded_ids
                ),
                default=0,
            )
            for s in remaining_slots
        )
        budget = SALARY_CAP - salary_so_far - min_cost_of_rest
        affordable = [p for p in eligible if p["salary"] <= budget]
        if not affordable:
            return None

        weights = [weight_fn(p) for p in affordable]
        pick = rng.choices(affordable, weights=weights, k=1)[0]

        picks.append(pick)
        used_ids.add(pick["id"])
        salary_so_far += pick["salary"]
        if slot != "P":
            team_hitter_count[pick["team"]] = team_hitter_count.get(pick["team"], 0) + 1
        if stack_remaining.get(pick["team"], 0) > 0:
            stack_remaining[pick["team"]] -= 1

    if any(n > 0 for n in stack_remaining.values()):
        return None  # couldn't fully satisfy the chosen shape's team groups -- caller retries

    stack_type, stack = stack_info({"players": picks})
    return {
        "salary_used": salary_so_far,
        "stack_type": stack_type,
        "stack": stack,
        "projected_points": round(sum(p["projected_fpts"] for p in picks), 2),
        "total_ownership_pct": round(sum(p["ownership_pct"] for p in picks), 1),
        "players": [
            {
                "id": p["id"],
                "name": p["name"],
                "team": p["team"],
                "salary": p["salary"],
                "projected_fpts": p["projected_fpts"],
                "ownership_pct": p["ownership_pct"],
            }
            for p in picks
        ],
        "player_ids": frozenset(used_ids),
    }


def _build_candidate_pool(
    slate: dict[str, Any], included_game_pks: list[int] | None
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Shared setup for both generators: the eligible-by-slot pool and
    the fixed 10-slot fill order, or a ContestError if either is empty."""
    pool = build_player_pool(
        slate,
        included_game_pks=set(included_game_pks) if included_game_pks is not None else None,
    )
    if not pool:
        if included_game_pks is not None:
            raise ContestError(
                "No optimizable players in the selected games -- try including more games."
            )
        raise ContestError(
            "No optimizable players for this date -- upload both a "
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


def generate_field(
    slate: dict[str, Any],
    sample_size: int,
    *,
    included_game_pks: list[int] | None = None,
    max_attempts_per_lineup: int = 25,
    seed: int | None = None,
) -> list[dict[str, Any]]:
    """
    Build `sample_size` synthetic opponent lineups, weighted toward
    whatever RotoWire ownership% says the public actually rosters.
    `included_game_pks` restricts the candidate pool the same way it
    does for the optimizer -- pass the same slate-game selection a
    user's own lineups were built against so the field reflects the
    actual DK slate, not every game MLB's schedule returns for the date.
    """
    if sample_size < 1:
        raise ContestError("sample_size must be at least 1.")
    if sample_size > MAX_SAMPLE_SIZE:
        raise ContestError(f"sample_size can't exceed {MAX_SAMPLE_SIZE}.")

    candidates_by_slot, slot_order = _build_candidate_pool(slate, included_game_pks)
    team_hitter_pools = _team_hitter_pools(candidates_by_slot, slot_order)
    feasible_shapes, feasible_weights = _feasible_stack_shapes(team_hitter_pools)

    rng = random.Random(seed)
    field: list[dict[str, Any]] = []
    for _ in range(sample_size):
        # Picked once per lineup, outside the retry loop below, so a
        # genuinely harder shape (5-3, needing 5 salary-expensive
        # hitters from one team) gets max_attempts_per_lineup real
        # shots at a working team assignment instead of losing out to
        # whichever easier shape happens to get rolled on a given retry.
        shape = _pick_stack_shape(feasible_shapes, feasible_weights, rng)
        lineup = None
        for _ in range(max_attempts_per_lineup):
            lineup = _sample_one_lineup(
                candidates_by_slot,
                slot_order,
                rng,
                _ownership_weight,
                team_hitter_pools=team_hitter_pools,
                stack_groups=shape,
            )
            if lineup is not None:
                break
        if lineup is not None:
            field.append(lineup)

    if not field:
        raise ContestError(
            "Couldn't build any legal field lineups from this pool -- the salary "
            "cap or slot requirements may be too tight for the players available."
        )
    return field


def generate_entries(
    slate: dict[str, Any],
    num_lineups: int,
    *,
    max_exposure_pct: float | None = None,
    included_game_pks: list[int] | None = None,
    max_attempts_per_lineup: int = 30,
    seed: int | None = None,
) -> list[dict[str, Any]]:
    """
    Build up to `num_lineups` of the user's OWN entries for a contest --
    each one individually strong (players picked with a heavy lean
    toward higher projected points, via `_fpts_weight`) but genuinely
    distinct from every other entry in the batch, unlike `generate_field`'s
    opponent model, which deliberately allows duplicate/near-duplicate
    lineups to represent real chalk clustering.

    `max_exposure_pct`, if given, caps how often any one player can
    appear across the whole batch -- same idea as optimizer.py's own
    exposure cap, tracked as a running count and enforced by excluding
    a capped player from the candidate pool for the rest of the batch.

    If the pool or the exposure cap can't support the full count
    requested, returns as many distinct legal entries as it could
    build rather than failing the whole request -- only an empty
    result raises.
    """
    if num_lineups < 1:
        raise ContestError("num_lineups must be at least 1.")
    if num_lineups > MAX_USER_LINEUPS:
        raise ContestError(f"num_lineups can't exceed {MAX_USER_LINEUPS:,}.")

    candidates_by_slot, slot_order = _build_candidate_pool(slate, included_game_pks)
    team_hitter_pools = _team_hitter_pools(candidates_by_slot, slot_order)
    feasible_shapes, feasible_weights = _feasible_stack_shapes(team_hitter_pools)

    def _cap_to_count(pct: float) -> int:
        return max(1, round(pct / 100 * num_lineups))

    rng = random.Random(seed)
    exposure_count: dict[int, int] = {}
    capped_ids: set[int] = set()
    seen_signatures: set[frozenset[int]] = set()
    entries: list[dict[str, Any]] = []

    for _ in range(num_lineups):
        # Picked once per lineup, outside the retry loop below -- see
        # generate_field()'s matching comment for why.
        shape = _pick_stack_shape(feasible_shapes, feasible_weights, rng)
        lineup = None
        for _ in range(max_attempts_per_lineup):
            candidate = _sample_one_lineup(
                candidates_by_slot,
                slot_order,
                rng,
                _fpts_weight,
                excluded_ids=frozenset(capped_ids),
                team_hitter_pools=team_hitter_pools,
                stack_groups=shape,
            )
            if candidate is None or candidate["player_ids"] in seen_signatures:
                continue
            lineup = candidate
            break
        if lineup is None:
            break  # ran out of room for more distinct, legal, exposure-legal entries

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
    return entries


def field_exposure(field: list[dict[str, Any]], top_n: int = 15) -> list[dict[str, Any]]:
    """How often each player showed up across the sampled field -- the
    field's own chalk, same shape as the optimizer's exposure report."""
    counts: dict[int, dict[str, Any]] = {}
    for lineup in field:
        for p in lineup["players"]:
            entry = counts.setdefault(
                p["id"], {"id": p["id"], "name": p["name"], "team": p["team"], "count": 0}
            )
            entry["count"] += 1
    ranked = sorted(counts.values(), key=lambda e: -e["count"])[:top_n]
    for e in ranked:
        e["pct"] = round(100 * e["count"] / len(field), 1)
    return ranked


def _payout_curve(paid_count: int, prize_pool: float, shape: str) -> list[float]:
    """
    Payout per paid rank, best finish first. A deliberately simple,
    clearly-approximate model -- not a real published payout table
    (those vary contest to contest and would go stale immediately) --
    just a flat split for double-ups/50-50s and a smooth top-heavy
    decay for GPPs.
    """
    if paid_count <= 0:
        return []
    if shape == "flat":
        each = prize_pool / paid_count
        return [round(each, 2)] * paid_count
    weights = [1 / (rank + 1) ** TOP_HEAVY_EXPONENT for rank in range(paid_count)]
    total_weight = sum(weights)
    return [round(prize_pool * w / total_weight, 2) for w in weights]


def _custom_payout_curve(
    paid_count: int, prize_pool: float, shape: str, first_place_pct: float | None
) -> list[float]:
    """
    Like _payout_curve, but for a real contest imported from a DK
    entries file (see dk_entries.py): `first_place_pct`, if given,
    pins rank 1's payout to exactly that percentage of the pool -- the
    one piece of the payout table a bulk entries export never includes
    -- while every other paid rank still splits the *remaining* pool
    using the same top-heavy decay shape _payout_curve already uses,
    rescaled so the whole curve still sums to prize_pool exactly.
    Falls back to the plain curve when there's no override to apply
    (a flat-split contest, or fewer than 2 paid places, has no "the
    rest" to rescale).
    """
    base = _payout_curve(paid_count, prize_pool, shape)
    if first_place_pct is None or shape != "top_heavy" or paid_count <= 1:
        return base
    first_place_payout = round(prize_pool * first_place_pct / 100, 2)
    remaining_pool = prize_pool - first_place_payout
    rest_weights = [1 / (rank + 1) ** TOP_HEAVY_EXPONENT for rank in range(1, paid_count)]
    total_rest_weight = sum(rest_weights)
    rest_payouts = (
        [round(remaining_pool * w / total_rest_weight, 2) for w in rest_weights]
        if total_rest_weight
        else [0.0] * (paid_count - 1)
    )
    return [first_place_payout, *rest_payouts]


def evaluate_field(
    field: list[dict[str, Any]],
    user_lineups: list[dict[str, Any]],
    contest: dict[str, Any],
) -> dict[str, Any]:
    """
    Rank each user lineup against the sampled field by projected
    points, project that percentile onto the real contest's field_size,
    and read off an estimated payout from the contest's payout curve.
    """
    if not user_lineups:
        raise ContestError("Need at least one lineup to evaluate against the field.")

    field_size = contest["field_size"]
    entry_fee = contest["entry_fee"]
    paid_count = max(1, round(field_size * contest["payout_pct"]))
    prize_pool = round(field_size * entry_fee * (1 - RAKE_PCT), 2)
    payouts = _payout_curve(paid_count, prize_pool, contest["shape"])

    field_points = [lu["projected_points"] for lu in field]
    sample_size = len(field_points)

    results = []
    for i, lu in enumerate(user_lineups):
        points = lu.get("projected_points")
        if points is None:
            raise ContestError(f"Lineup {i} is missing projected_points.")
        beaten = sum(1 for fp in field_points if points > fp)
        percentile = round(100 * beaten / sample_size, 1)
        estimated_rank = max(1, round((1 - beaten / sample_size) * field_size))
        in_the_money = estimated_rank <= paid_count
        payout = payouts[estimated_rank - 1] if in_the_money else 0.0
        results.append(
            {
                "lineup_index": i,
                "projected_points": points,
                "percentile": percentile,
                "estimated_rank": estimated_rank,
                "in_the_money": in_the_money,
                "estimated_payout": payout,
                "estimated_profit": round(payout - entry_fee, 2),
            }
        )

    return {
        "field_size": field_size,
        "sample_size": sample_size,
        "paid_count": paid_count,
        "entry_fee": entry_fee,
        "prize_pool": prize_pool,
        "results": results,
    }


def _evaluate_batch_against_field(
    entries: list[dict[str, Any]],
    field: list[dict[str, Any]],
    contest: dict[str, Any],
) -> dict[str, Any]:
    """
    Ranks a whole BATCH of the user's own entries against the sampled
    opponent field, for mass multi-entry -- unlike `evaluate_field`
    (fine for a handful of lineups), which ranks each one independently
    against the field and lets them each claim the same top spot. At
    thousands-of-entries scale that badly double-counts: if most of a
    10,000-entry batch is individually strong, evaluate_field would
    have nearly all of them "cash" at the SAME top ranks, summing to a
    total payout many times the real prize pool.

    Real contests have a fixed number of paid ranks (1..paid_count) --
    two entries can never occupy the same one. This assigns entries
    real, mutually distinct ranks: sort by projected points descending,
    then walk best-to-worst enforcing each entry's rank is strictly
    greater than the previous (stronger) entry's -- a percentile-based
    rank is still the starting point, it just can't collide with (or
    beat) a better entry from the same batch. `field_size` is a hard
    ceiling: `build_contest_entries` already validates the batch isn't
    larger than the real contest before this ever runs.
    """
    field_size = contest["field_size"]
    entry_fee = contest["entry_fee"]
    paid_count = max(1, round(field_size * contest["payout_pct"]))
    prize_pool = round(field_size * entry_fee * (1 - RAKE_PCT), 2)
    payouts = _payout_curve(paid_count, prize_pool, contest["shape"])

    field_points = [lu["projected_points"] for lu in field]
    sample_size = len(field_points)

    order = sorted(range(len(entries)), key=lambda i: -entries[i]["projected_points"])

    results: list[dict[str, Any] | None] = [None] * len(entries)
    prev_rank = 0
    for i in order:
        points = entries[i]["projected_points"]
        beaten = sum(1 for fp in field_points if points > fp)
        percentile = round(100 * beaten / sample_size, 1)
        percentile_rank = max(1, round((1 - beaten / sample_size) * field_size))
        rank = min(max(percentile_rank, prev_rank + 1), field_size)
        prev_rank = rank

        in_the_money = rank <= paid_count
        payout = payouts[rank - 1] if in_the_money else 0.0
        results[i] = {
            "lineup_index": i,
            "projected_points": points,
            "percentile": percentile,
            "estimated_rank": rank,
            "in_the_money": in_the_money,
            "estimated_payout": payout,
            "estimated_profit": round(payout - entry_fee, 2),
        }

    return {
        "field_size": field_size,
        "sample_size": sample_size,
        "paid_count": paid_count,
        "entry_fee": entry_fee,
        "prize_pool": prize_pool,
        "results": results,
    }


async def evaluate_batch_simulated(
    entries: list[dict[str, Any]],
    field: list[dict[str, Any]],
    contest: dict[str, Any],
    *,
    season: int,
    num_trials: int = 2000,
    seed: int | None = None,
    first_place_pct: float | None = None,
) -> dict[str, Any]:
    """
    Like `_evaluate_batch_against_field`, but ranks against
    variance.simulate_batch()'s real Monte Carlo outcome distribution
    instead of a single projected-points snapshot -- entries and field
    are simulated TOGETHER, trial by trial, so a lineup's cash
    probability is the fraction of trials where it actually lands in
    the paid zone of that specific simulated reality, and its expected
    payout is a genuine average (with a 10th/90th percentile range)
    rather than one point estimate.

    Two entries can never occupy the same paid rank within a single
    trial -- same rule `_evaluate_batch_against_field` enforces for its
    one deterministic ranking, just applied per trial here. The
    sequential "bump forward just enough to stay distinct" walk that
    function does with a Python loop is instead solved with a
    cumulative maximum: for entries sorted best-to-worst within a
    trial, distinct_rank_i = i + running_max_{j<=i}(percentile_rank_j -
    j) is the closed form of that exact recurrence, vectorized across
    every trial at once instead of looping trial by trial in Python.

    `contest["prize_pool"]`, if present, is used directly instead of
    estimating one from field_size * entry_fee -- for a real contest
    imported via dk_entries.py, the real prize pool is known outright
    rather than needing to be inferred. `first_place_pct`, passed
    through to _custom_payout_curve(), does the same for the one paid
    rank a bulk entries export can't tell you anything about.
    """
    if not entries:
        raise ContestError("Need at least one entry to simulate.")
    if not field:
        raise ContestError("Need at least one field lineup to simulate against.")

    field_size = contest["field_size"]
    entry_fee = contest["entry_fee"]
    paid_count = max(1, round(field_size * contest["payout_pct"]))
    prize_pool = contest.get("prize_pool") or round(field_size * entry_fee * (1 - RAKE_PCT), 2)
    payouts = np.array(_custom_payout_curve(paid_count, prize_pool, contest["shape"], first_place_pct))

    num_entries = len(entries)
    sample_size = len(field)
    player_pools = await variance.player_pools_for_entries(entries + field, season)
    sim = variance.simulate_batch(entries + field, player_pools, num_trials=num_trials, seed=seed)
    entry_sim, field_sim = sim[:num_entries], sim[num_entries:]

    # Per trial, how many field lineups each entry beat -- searchsorted
    # against that trial's sorted field column is far cheaper than a
    # full entries x field x trials comparison.
    field_sorted = np.sort(field_sim, axis=0)
    beaten = np.empty((num_entries, num_trials), dtype=np.int64)
    for t in range(num_trials):
        beaten[:, t] = np.searchsorted(field_sorted[:, t], entry_sim[:, t], side="left")
    percentile_rank = np.clip(np.round((1 - beaten / sample_size) * field_size), 1, field_size).astype(
        np.int64
    )

    order = np.argsort(-entry_sim, axis=0)  # best-to-worst per trial
    sorted_pct_rank = np.take_along_axis(percentile_rank, order, axis=0)
    positions = np.arange(num_entries)[:, None]
    distinct_rank_sorted = np.minimum(
        np.maximum.accumulate(sorted_pct_rank - positions, axis=0) + positions, field_size
    )
    final_rank = np.empty_like(distinct_rank_sorted)
    np.put_along_axis(final_rank, order, distinct_rank_sorted, axis=0)

    in_the_money = final_rank <= paid_count
    payout_index = np.clip(final_rank - 1, 0, paid_count - 1)
    payout_per_trial = np.where(in_the_money, payouts[payout_index], 0.0)

    # "Top 1%"/"top 10%" are relative to the real contest's field_size
    # (same scale final_rank already projects onto), not the entry
    # batch or the sampled field -- a rank of 100 in a 10,000-entry
    # contest is top 1% regardless of how many entries you personally
    # submitted.
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
                "simulated_points_floor": round(float(row.min()), 2),
                "simulated_points_ceiling": round(float(row.max()), 2),
            }
        )

    return {
        "field_size": field_size,
        "sample_size": sample_size,
        "paid_count": paid_count,
        "entry_fee": entry_fee,
        "prize_pool": prize_pool,
        "num_trials": num_trials,
        "results": results,
    }


def build_contest_field(
    slate: dict[str, Any],
    contest_type: str,
    user_lineups: list[dict[str, Any]],
    *,
    field_size: int | None = None,
    sample_size: int | None = None,
    included_game_pks: list[int] | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    """
    Top-level entry point: build a synthetic field for `contest_type`
    (optionally overriding its preset field_size) and rank
    `user_lineups` against it. `included_game_pks` should be whatever
    slate-game selection was used to build `user_lineups` in the first
    place -- otherwise the field could be drawn from games that aren't
    even part of the slate being entered.
    """
    if contest_type not in CONTEST_TYPES:
        raise ContestError(
            f"Unknown contest_type '{contest_type}'. Choose one of: {', '.join(CONTEST_TYPES)}."
        )
    contest = dict(CONTEST_TYPES[contest_type])
    if field_size is not None:
        if not (1 <= field_size <= MAX_FIELD_SIZE):
            raise ContestError(f"field_size must be between 1 and {MAX_FIELD_SIZE}.")
        contest["field_size"] = field_size

    size = sample_size or min(contest["field_size"], MAX_SAMPLE_SIZE)
    field = generate_field(slate, size, included_game_pks=included_game_pks, seed=seed)
    evaluation = evaluate_field(field, user_lineups, contest)

    return {
        "contest_type": contest_type,
        "contest": contest,
        **evaluation,
        "field_ownership": {
            "avg_total_ownership_pct": round(
                sum(lu["total_ownership_pct"] for lu in field) / len(field), 1
            ),
            "min_total_ownership_pct": min(lu["total_ownership_pct"] for lu in field),
            "max_total_ownership_pct": max(lu["total_ownership_pct"] for lu in field),
        },
        "field_exposure": field_exposure(field),
        "note": (
            "Field lineups are sampled by RotoWire ownership%, not simulated "
            "outcomes -- ranks and payouts are projected-points estimates, not "
            "win probabilities."
        ),
    }


def _build_entries_and_field(
    slate: dict[str, Any],
    contest_type: str,
    num_lineups: int,
    *,
    max_exposure_pct: float | None,
    field_size: int | None,
    sample_size: int | None,
    included_game_pks: list[int] | None,
    seed: int | None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Shared setup for build_contest_entries and
    build_contest_entries_simulated: validate the contest type/size,
    build the user's own entries, and sample an opponent field to rank
    them against. `seed`, if given, offsets the opponent field's own
    seed by one so the two random walks aren't identical.
    """
    if contest_type not in CONTEST_TYPES:
        raise ContestError(
            f"Unknown contest_type '{contest_type}'. Choose one of: {', '.join(CONTEST_TYPES)}."
        )
    contest = dict(CONTEST_TYPES[contest_type])
    if field_size is not None:
        if not (1 <= field_size <= MAX_FIELD_SIZE):
            raise ContestError(f"field_size must be between 1 and {MAX_FIELD_SIZE}.")
        contest["field_size"] = field_size

    # Your entries are part of the real contest's field_size, not
    # additional to it -- you can't submit more lineups than the
    # contest holds. Catching this here, before generating anything,
    # gives a clear "pick a bigger contest or fewer lineups" error
    # instead of nonsense economics later (paid_count/prize_pool are
    # both derived straight from field_size).
    if num_lineups > contest["field_size"]:
        raise ContestError(
            f"num_lineups ({num_lineups:,}) can't exceed the contest's field_size "
            f"({contest['field_size']:,}) -- your own entries are part of the field, not "
            "additional to it. Pick a bigger contest, override field_size, or lower num_lineups."
        )

    entries = generate_entries(
        slate,
        num_lineups,
        max_exposure_pct=max_exposure_pct,
        included_game_pks=included_game_pks,
        seed=seed,
    )

    field_sample = sample_size or min(contest["field_size"], MAX_SAMPLE_SIZE)
    field = generate_field(
        slate,
        field_sample,
        included_game_pks=included_game_pks,
        seed=(seed + 1) if seed is not None else None,
    )
    return contest, entries, field


def build_contest_entries(
    slate: dict[str, Any],
    contest_type: str,
    num_lineups: int,
    *,
    max_exposure_pct: float | None = None,
    field_size: int | None = None,
    sample_size: int | None = None,
    included_game_pks: list[int] | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    """
    The contest generator's main entry point for mass multi-entry: build
    up to `num_lineups` (max MAX_USER_LINEUPS) of the user's own entries
    for a named contest type -- each individually strong, genuinely
    distinct from the rest of the batch -- then evaluate the whole batch
    against a simulated opponent field for cash-rate/payout economics.

    Unrelated to optimizer.py's exact single/small-batch MILP solver;
    this is the fast, large-scale path, sacrificing per-lineup
    optimality for the ability to build thousands of entries in one
    request. Deterministic and fast, ranking against the field's
    *projected* points -- see build_contest_entries_simulated() for the
    real Monte Carlo alternative.
    """
    contest, entries, field = _build_entries_and_field(
        slate,
        contest_type,
        num_lineups,
        max_exposure_pct=max_exposure_pct,
        field_size=field_size,
        sample_size=sample_size,
        included_game_pks=included_game_pks,
        seed=seed,
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
            "avg_salary_used": round(sum(e["salary_used"] for e in entries) / len(entries)),
            "avg_projected_points": round(sum(points) / len(points), 2),
            "min_projected_points": min(points),
            "max_projected_points": max(points),
            "avg_total_ownership_pct": round(
                sum(e["total_ownership_pct"] for e in entries) / len(entries), 1
            ),
        },
        "exposure": field_exposure(entries, top_n=20),
        # Full batch, deliberately not capped here -- routers/mlb.py
        # decides how much of this goes into the JSON response (the
        # aggregate `summary` above already covers the whole batch, so
        # per-lineup detail there is only for spot-checking and gets
        # capped) versus a CSV download, which needs everything.
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
    included_game_pks: list[int] | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    """
    Like build_contest_entries, but ranks the batch against
    evaluate_batch_simulated()'s real Monte Carlo cash probabilities and
    payout distributions instead of a single projected-points snapshot
    against the field. A separate function rather than a flag on
    build_contest_entries -- simulation is real additional compute
    (fetching every player's own outcome pool, then running num_trials
    simulated realities) on top of an already-large mass-generation
    call, and needs `season` to know which year's game logs to pull.
    """
    contest, entries, field = _build_entries_and_field(
        slate,
        contest_type,
        num_lineups,
        max_exposure_pct=max_exposure_pct,
        field_size=field_size,
        sample_size=sample_size,
        included_game_pks=included_game_pks,
        seed=seed,
    )
    evaluation = await evaluate_batch_simulated(
        entries,
        field,
        contest,
        season=season,
        num_trials=num_trials,
        seed=(seed + 2) if seed is not None else None,
    )

    # Highest simulated ROI first -- the whole point of running the
    # simulation is finding which of your own entries actually pays
    # off, so that should lead the results rather than whatever
    # arbitrary order generate_entries() built them in. entries and
    # results are re-ordered together (same permutation) so every
    # downstream consumer -- the JSON response, the sample-entries
    # preview, and the cached batch behind the CSV download -- gets
    # the sorted order for free, with no separate sort step anywhere
    # else.
    order = sorted(range(len(entries)), key=lambda i: -evaluation["results"][i]["roi_pct"])
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
        "num_entries_requested": num_lineups,
        "num_entries_built": len(entries),
        "field_size": evaluation["field_size"],
        "sample_size": evaluation["sample_size"],
        "paid_count": evaluation["paid_count"],
        "prize_pool": evaluation["prize_pool"],
        "num_trials": evaluation["num_trials"],
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
        "exposure": field_exposure(entries, top_n=20),
        "entries": entries,
        "results": evaluation["results"],
        "note": (
            f"Cash probability and expected payout come from {evaluation['num_trials']:,} "
            "real Monte Carlo simulated trials of each player's own historical outcome "
            "pool, with team correlation for hitters -- a genuine probability, not a "
            "single projected-points estimate against the field."
        ),
    }


async def evaluate_field_mirrored(
    field_lineups: list[dict[str, Any]],
    contest: dict[str, Any],
    *,
    season: int,
    num_trials: int = 10_000,
    seed: int | None = None,
    first_place_pct: float | None = None,
) -> dict[str, Any]:
    """
    Simulate `field_lineups` -- an ownership-weighted SAMPLE standing in
    for a real contest's entire field (same construction generate_field()
    already uses to model what actual public rosters look like: chalk-
    heavy, duplicates allowed) -- as one self-contained population: every
    lineup ranked against every OTHER lineup in the same simulated trial,
    not against a separately generated "my entries" batch the way
    evaluate_batch_simulated works. Every sampled lineup gets its own
    cash probability/ROI/etc., meant for browsing the whole simulated
    field to see which archetypes actually perform -- not for ranking a
    specific handful of "your" entries.

    `contest["field_size"]` is the REAL contest's total entry count,
    almost always larger than `len(field_lineups)` (capped for
    performance, same "simulate a sample, project onto the real size"
    approach the rest of this module already uses for a field too large
    to fully enumerate). Each sampled lineup's rank *within the sample*
    is projected onto the real field_size by simple linear
    interpolation -- rank k of sample_size maps to
    round(1 + (k-1) * (field_size-1) / (sample_size-1)) -- which is
    exact and collision-free (strictly increasing in k) as long as
    field_size >= sample_size, unlike evaluate_batch_simulated's
    situation of reconciling two independently-sampled populations.
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
    payouts = np.array(_custom_payout_curve(paid_count, prize_pool, contest["shape"], first_place_pct))

    player_pools = await variance.player_pools_for_entries(field_lineups, season)
    sim = variance.simulate_batch(field_lineups, player_pools, num_trials=num_trials, seed=seed)

    order = np.argsort(-sim, axis=0)  # best-to-worst lineup index per trial
    if sample_size > 1:
        real_ranks_by_k = 1 + np.floor(
            np.arange(sample_size) * (field_size - 1) / (sample_size - 1)
        ).astype(np.int64)
    else:
        real_ranks_by_k = np.array([1])
    final_rank = np.empty_like(order)
    np.put_along_axis(final_rank, order, real_ranks_by_k[:, None], axis=0)

    top_1pct_threshold = max(1, round(0.01 * field_size))
    top_10pct_threshold = max(1, round(0.10 * field_size))
    in_the_money = final_rank <= paid_count
    first_place = final_rank == 1
    top_1pct = final_rank <= top_1pct_threshold
    top_10pct = final_rank <= top_10pct_threshold
    payout_index = np.clip(final_rank - 1, 0, paid_count - 1)
    payout_per_trial = np.where(in_the_money, payouts[payout_index], 0.0)

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
                "simulated_points_floor": round(float(row.min()), 2),
                "simulated_points_ceiling": round(float(row.max()), 2),
            }
        )

    return {
        "field_size": field_size,
        "sample_size": sample_size,
        "paid_count": paid_count,
        "entry_fee": entry_fee,
        "prize_pool": prize_pool,
        "num_trials": num_trials,
        "results": results,
    }


async def build_dk_entries_simulated(
    slate: dict[str, Any],
    *,
    season: int,
    entry_fee: float,
    field_size: int,
    prize_pool: float,
    first_place_pct: float,
    payout_pct: float = 0.20,
    shape: str = "top_heavy",
    num_trials: int = 10_000,
    sample_size: int | None = None,
    included_game_pks: list[int] | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    """
    Mirror a real DraftKings contest and simulate its whole field: an
    ownership-weighted representative sample of field_size lineups
    (generate_field() -- the same construction this app already uses to
    model what a real public field actually looks like, chalk-heavy with
    duplicates, not this app's own "individually strong and distinct"
    construction), simulated as one self-contained population via
    evaluate_field_mirrored() -- every lineup ranked against every OTHER
    sampled lineup, not against a separately generated "my own entries"
    batch. Returns every sampled lineup's own simulated cash probability/
    ROI/etc., meant for browsing the results to see which archetypes
    actually perform well in this specific contest and slate, and
    picking which ones to submit yourself -- not a batch of entries this
    app is claiming are "yours."

    entry_fee/field_size/prize_pool/first_place_pct describe the real
    contest: entry_fee comes straight from the DK entries file (see
    dk_entries.contest_summary()), the rest are hand-entered since a
    bulk entries export has no payout-table data or field-size
    information at all.
    """
    if field_size < 1:
        raise ContestError("field_size must be at least 1.")

    field_sample = sample_size or min(field_size, MAX_SAMPLE_SIZE)
    field_lineups = generate_field(slate, field_sample, included_game_pks=included_game_pks, seed=seed)

    contest = {
        "entry_fee": entry_fee,
        "field_size": field_size,
        "payout_pct": payout_pct,
        "shape": shape,
        "prize_pool": prize_pool,
    }
    evaluation = await evaluate_field_mirrored(
        field_lineups,
        contest,
        season=season,
        num_trials=num_trials,
        seed=(seed + 2) if seed is not None else None,
        first_place_pct=first_place_pct,
    )

    order = sorted(range(len(field_lineups)), key=lambda i: -evaluation["results"][i]["roi_pct"])
    entries = [field_lineups[i] for i in order]
    evaluation = {
        **evaluation,
        "results": [{**evaluation["results"][i], "lineup_index": new_i} for new_i, i in enumerate(order)],
    }

    cash_probs = [r["cash_probability_pct"] for r in evaluation["results"]]
    first_place_pcts_sim = [r["first_place_pct"] for r in evaluation["results"]]
    top_1pct_pcts = [r["top_1pct_pct"] for r in evaluation["results"]]
    top_10pct_pcts = [r["top_10pct_pct"] for r in evaluation["results"]]
    roi_pcts = [r["roi_pct"] for r in evaluation["results"]]
    expected_payouts = [r["expected_payout"] for r in evaluation["results"]]
    total_cost = round(len(entries) * entry_fee, 2)
    total_expected_payout = round(sum(expected_payouts), 2)

    return {
        "contest": contest,
        "num_entries_built": len(entries),
        "field_size": evaluation["field_size"],
        "sample_size": evaluation["sample_size"],
        "paid_count": evaluation["paid_count"],
        "prize_pool": evaluation["prize_pool"],
        "num_trials": evaluation["num_trials"],
        "summary": {
            "avg_cash_probability_pct": round(sum(cash_probs) / len(cash_probs), 1),
            "avg_first_place_pct": round(sum(first_place_pcts_sim) / len(first_place_pcts_sim), 2),
            "avg_top_1pct_pct": round(sum(top_1pct_pcts) / len(top_1pct_pcts), 2),
            "avg_top_10pct_pct": round(sum(top_10pct_pcts) / len(top_10pct_pcts), 2),
            "avg_roi_pct": round(sum(roi_pcts) / len(roi_pcts), 1),
            "total_entry_cost": total_cost,
            "total_expected_payout": total_expected_payout,
            "estimated_net_profit": round(total_expected_payout - total_cost, 2),
        },
        "exposure": field_exposure(entries, top_n=20),
        "entries": entries,
        "results": evaluation["results"],
        "note": (
            f"A {len(entries):,}-lineup ownership-weighted sample standing in for this real "
            f"contest's full {field_size:,}-entry field, simulated over {evaluation['num_trials']:,} "
            "Monte Carlo trials with each lineup ranked against every other lineup in the same "
            "simulated reality (not against a separate 'your entries' batch) -- browse the results "
            "below to see which archetypes actually perform, then pick whichever ones you want to "
            "submit yourself. prize_pool, first_place_pct, and field_size are hand-entered, since a "
            "DraftKings entries export doesn't include the contest's payout table or true size."
        ),
    }
