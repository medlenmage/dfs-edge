"""
Synthetic DFS contest field, for gut-checking a generated lineup against
a realistic public field rather than just its own projected score in
isolation.

This is NOT a lineup simulator -- there's no player outcome variance
model yet, so "how does my lineup compare" here means "against the
field's *projected* points," not "what's my probability of cashing
across a distribution of possible real-world outcomes." That's a real
limitation, stated plainly rather than dressed up: this tool answers
"is my build structurally different from what the field will look
like" (salary usage, chalk exposure, where it'd rank on paper), which
is still a useful leverage-decision input even without a variance
model. The simulator is a bigger, separate follow-up.

FIELD GENERATION
-----------------
A real contest's field isn't a pile of optimal lineups -- it's skewed
toward whatever's popular. This builds synthetic field entries by
randomly sampling each roster slot weighted by RotoWire ownership%,
the signal that actually describes what the public rosters, rather
than re-running the MILP optimizer (which would just produce a pile of
near-identical, near-optimal builds -- the opposite of a real field).

A large real contest (thousands to 100,000+ entries) is modeled as a
*sample*, not literally reproduced one entry at a time -- MAX_SAMPLE_SIZE
caps how many synthetic lineups actually get built, and a user lineup's
rank within that sample gets projected back onto the real field_size
statistically (the same idea as a poll sampling a fraction of a
population). Good enough for the intended reads (chalk exposure, likely
rank range, roughly where the cash line falls) without pretending to
enumerate a 100,000-entry field in one HTTP request.

Reuses optimizer.build_player_pool() for the candidate pool -- same
salary/projection/scratch eligibility rules, same `included_game_pks`
slate filter, so the field is drawn from the exact same DK slate a
user's own lineups were built against, not from every game MLB's
schedule happens to return for the date.
"""

from __future__ import annotations

import random
from typing import Any

from app.services.optimizer import SALARY_CAP, SLOT_REQUIREMENTS, SLOT_TYPES, build_player_pool

MAX_SAMPLE_SIZE = 5000
MAX_FIELD_SIZE = 200_000
_OWNERSHIP_FLOOR = 0.5  # even an unowned player gets a sliver of sampling weight

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


def _sample_one_lineup(
    candidates_by_slot: dict[str, list[dict[str, Any]]],
    slot_order: list[str],
    rng: random.Random,
) -> dict[str, Any] | None:
    """
    Build one ownership-weighted random lineup within the salary cap.
    At each slot, the pick is weighted toward higher-owned players but
    constrained to what still leaves enough budget for the cheapest
    possible player at every remaining slot -- a standard feasible
    random-roster-construction technique. Returns None if this
    particular random walk couldn't complete; the caller just retries.
    """
    used_ids: set[int] = set()
    picks: list[dict[str, Any]] = []
    salary_so_far = 0

    for i, slot in enumerate(slot_order):
        remaining_slots = slot_order[i + 1 :]
        eligible = [p for p in candidates_by_slot[slot] if p["id"] not in used_ids]
        if not eligible:
            return None

        min_cost_of_rest = sum(
            min(
                (p["salary"] for p in candidates_by_slot[s] if p["id"] not in used_ids),
                default=0,
            )
            for s in remaining_slots
        )
        budget = SALARY_CAP - salary_so_far - min_cost_of_rest
        affordable = [p for p in eligible if p["salary"] <= budget]
        if not affordable:
            return None

        weights = [max(p["ownership_pct"], _OWNERSHIP_FLOOR) for p in affordable]
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
    }


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

    rng = random.Random(seed)
    field: list[dict[str, Any]] = []
    for _ in range(sample_size):
        lineup = None
        for _ in range(max_attempts_per_lineup):
            lineup = _sample_one_lineup(candidates_by_slot, slot_order, rng)
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
