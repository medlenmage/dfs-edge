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

from app.services.optimizer import SALARY_CAP, SLOT_REQUIREMENTS, SLOT_TYPES, build_player_pool

MAX_SAMPLE_SIZE = 5000
MAX_FIELD_SIZE = 200_000
MAX_USER_LINEUPS = 10_000
_OWNERSHIP_FLOOR = 0.5  # even an unowned player gets a sliver of sampling weight

# How hard entry generation leans toward higher-projected players.
# fpts is raised to this power before sampling -- aggressive enough
# that entries are genuinely strong (not just plausible), gentle
# enough that weaker/cheaper players still get picked when a stronger
# one has already been used or doesn't fit the remaining budget.
_FPTS_SAMPLING_EXPONENT = 3.0
_FPTS_FLOOR = 0.1

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


def _sample_one_lineup(
    candidates_by_slot: dict[str, list[dict[str, Any]]],
    slot_order: list[str],
    rng: random.Random,
    weight_fn: Callable[[dict[str, Any]], float],
    *,
    excluded_ids: frozenset[int] = frozenset(),
) -> dict[str, Any] | None:
    """
    Build one randomly-weighted lineup within the salary cap. At each
    slot, the pick is weighted by `weight_fn` (ownership% for the
    opponent field, projected points for the user's own entries) but
    constrained to what still leaves enough budget for the cheapest
    possible player at every remaining slot -- a standard feasible
    random-roster-construction technique. `excluded_ids` removes
    players entirely (e.g. ones that have hit an exposure cap).
    Returns None if this particular random walk couldn't complete; the
    caller just retries.
    """
    used_ids: set[int] = set()
    picks: list[dict[str, Any]] = []
    salary_so_far = 0

    for i, slot in enumerate(slot_order):
        remaining_slots = slot_order[i + 1 :]
        eligible = [
            p for p in candidates_by_slot[slot] if p["id"] not in used_ids and p["id"] not in excluded_ids
        ]
        if not eligible:
            return None

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

    return {
        "salary_used": salary_so_far,
        "projected_points": round(sum(p["projected_fpts"] for p in picks), 2),
        "total_ownership_pct": round(sum(p["ownership_pct"] for p in picks), 1),
        "players": [
            {"id": p["id"], "name": p["name"], "team": p["team"], "salary": p["salary"]}
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

    rng = random.Random(seed)
    field: list[dict[str, Any]] = []
    for _ in range(sample_size):
        lineup = None
        for _ in range(max_attempts_per_lineup):
            lineup = _sample_one_lineup(candidates_by_slot, slot_order, rng, _ownership_weight)
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

    def _cap_to_count(pct: float) -> int:
        return max(1, round(pct / 100 * num_lineups))

    rng = random.Random(seed)
    exposure_count: dict[int, int] = {}
    capped_ids: set[int] = set()
    seen_signatures: set[frozenset[int]] = set()
    entries: list[dict[str, Any]] = []

    for _ in range(num_lineups):
        lineup = None
        for _ in range(max_attempts_per_lineup):
            candidate = _sample_one_lineup(
                candidates_by_slot, slot_order, rng, _fpts_weight, excluded_ids=frozenset(capped_ids)
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
    request. `seed`, if given, offsets the opponent field's own seed by
    one so the two random walks aren't identical.
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
        # The aggregate `summary` above already covers the whole batch;
        # per-lineup detail is only useful for spot-checking, so it's
        # capped rather than shipping up to 10,000 rows in one response.
        "results": evaluation["results"][:200],
        "sample_entries": entries[:200],
        "note": (
            "Entries are built by fast randomized construction weighted toward "
            "projected points, not an exact solve -- individually strong and "
            "mutually distinct, not guaranteed optimal. Cash rate and payout are "
            "projected-points estimates against a sampled opponent field, not "
            "simulated real-world outcomes."
        ),
    }
