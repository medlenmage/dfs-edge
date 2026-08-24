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

  - No named stack-shape system (contest.py's STACK_SHAPES/5-3/5-2-1/
    etc., or DK's own 5-hitter-per-team rule -- NFL has no equivalent
    DK roster restriction to enforce). Entries are built by plain
    per-slot weighted random sampling, same distinctness/exposure-cap
    mechanics, just no deliberate team-stacking bias. nfl_optimizer.py's
    own MILP-based qb_stack_min is the real stacking tool for a small,
    exact batch; this mass-generation path doesn't have an equivalent
    yet.
  - No `engine="atbat"` alternative -- MLB's at-bat-level slate
    simulator (atbat_sim.py) has no NFL analog; this always uses the
    bootstrap outcome-pool engine (nfl_variance.py).
  - No DK-entries-file import/mirroring (contest.py's
    build_dk_entries_simulated) and no post-hoc reshape/filter step
    (contest.py's reshape_batch) -- neither was on the requested list.
  - No `included_game_pks`/`projection_source` params --
    nfl_optimizer.build_player_pool() doesn't support either yet (NFL
    has no in-house projection model and no per-game slate filter),
    so there's nothing to thread through here either.

Player ids are strings throughout (DK's own numeric id, matching
nfl_optimizer.py's convention), not the ints contest.py's MLB player
ids are.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import Any

import numpy as np

from app.services import nfl_variance
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
    _field_weight_fn,
    _ownership_weight,
    _split_duplicate_payouts,
    field_exposure,
)
from app.services.nfl_optimizer import SALARY_CAP, SLOT_REQUIREMENTS, SLOT_TYPES, build_player_pool

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


def _build_candidate_pool(slate: dict[str, Any]) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Shared setup: the eligible-by-slot pool and the fixed 9-slot fill
    order, or a ContestError if either is empty."""
    pool = build_player_pool(slate)
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


def _sample_one_lineup(
    candidates_by_slot: dict[str, list[dict[str, Any]]],
    slot_order: list[str],
    rng: random.Random,
    weight_fn: Callable[[dict[str, Any]], float],
    *,
    excluded_ids: frozenset[str] = frozenset(),
    min_salary: int = 0,
    max_salary: int = SALARY_CAP,
) -> dict[str, Any] | None:
    """
    Build one randomly-weighted lineup within the salary cap (and the
    narrower [min_salary, max_salary] range, if given). At each slot,
    the pick is weighted by `weight_fn` (ownership% for the opponent
    field, projected points for the user's own entries) but constrained
    to what still leaves enough budget for the cheapest possible player
    at every remaining slot. `excluded_ids` removes players entirely
    (e.g. ones that have hit an exposure cap). Returns None if this
    particular random walk couldn't complete; the caller retries.
    """
    used_ids: set[str] = set()
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

    if not (min_salary <= salary_so_far <= max_salary):
        return None

    return {
        "salary_used": salary_so_far,
        "projected_points": round(sum(p["projected_fpts"] for p in picks), 2),
        "total_ownership_pct": round(sum(p["ownership_pct"] for p in picks), 1),
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
    min_salary: int = 0,
    max_salary: int = SALARY_CAP,
    seed: int | None = None,
    field_sharpness: str = "marquee",
) -> list[dict[str, Any]]:
    """
    Build `sample_size` synthetic opponent lineups, weighted toward
    whatever RotoWire's ownership% says the public actually rosters --
    see contest.py's generate_field() for the full rationale, which
    applies unchanged here. `field_sharpness`: see FIELD_SHARPNESS_LEVELS.
    """
    if sample_size < 1:
        raise ContestError("sample_size must be at least 1.")
    if sample_size > MAX_SAMPLE_SIZE:
        raise ContestError(f"sample_size can't exceed {MAX_SAMPLE_SIZE}.")
    if field_sharpness not in FIELD_SHARPNESS_LEVELS:
        raise ContestError(
            f"Unknown field_sharpness '{field_sharpness}'. Choose one of: "
            f"{', '.join(FIELD_SHARPNESS_LEVELS)}."
        )
    _validate_salary_range(min_salary, max_salary)

    candidates_by_slot, slot_order = _build_candidate_pool(slate)
    field_weight_fn = _field_weight_fn(field_sharpness)

    rng = random.Random(seed)
    field: list[dict[str, Any]] = []
    for _ in range(sample_size):
        lineup = None
        for _ in range(max_attempts_per_lineup):
            lineup = _sample_one_lineup(
                candidates_by_slot, slot_order, rng, field_weight_fn,
                min_salary=min_salary, max_salary=max_salary,
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
    _attach_duplicate_counts(field)
    return field


def generate_entries(
    slate: dict[str, Any],
    num_lineups: int,
    *,
    max_exposure_pct: float | None = None,
    max_attempts_per_lineup: int = 30,
    min_salary: int = 0,
    max_salary: int = SALARY_CAP,
    allow_duplicates: bool = False,
    seed: int | None = None,
) -> list[dict[str, Any]]:
    """
    Build up to `num_lineups` of the user's OWN entries -- see
    contest.py's generate_entries() for the full rationale (fast
    randomized construction weighted toward projected points,
    exposure-capped, distinct by default unless allow_duplicates),
    which applies unchanged here. Returns as many legal entries as the
    pool/exposure cap could support rather than failing the whole
    request short of the count; only an empty result raises.
    """
    if num_lineups < 1:
        raise ContestError("num_lineups must be at least 1.")
    if num_lineups > MAX_USER_LINEUPS:
        raise ContestError(f"num_lineups can't exceed {MAX_USER_LINEUPS:,}.")
    _validate_salary_range(min_salary, max_salary)

    candidates_by_slot, slot_order = _build_candidate_pool(slate)

    def _cap_to_count(pct: float) -> int:
        return max(1, round(pct / 100 * num_lineups))

    rng = random.Random(seed)
    exposure_count: dict[str, int] = {}
    capped_ids: set[str] = set()
    seen_signatures: set[frozenset[str]] = set()
    entries: list[dict[str, Any]] = []

    for _ in range(num_lineups):
        lineup = None
        for _ in range(max_attempts_per_lineup):
            candidate = _sample_one_lineup(
                candidates_by_slot, slot_order, rng, _fpts_weight,
                excluded_ids=frozenset(capped_ids),
                min_salary=min_salary, max_salary=max_salary,
            )
            if candidate is None:
                continue
            if not allow_duplicates and candidate["player_ids"] in seen_signatures:
                continue
            lineup = candidate
            break
        if lineup is None:
            break  # ran out of room for more legal, exposure-legal entries

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
    min_salary: int = 0,
    max_salary: int = SALARY_CAP,
    allow_duplicates: bool = False,
    seed: int | None,
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
        allow_duplicates=allow_duplicates, seed=seed,
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
    min_salary: int = 0,
    max_salary: int = SALARY_CAP,
    allow_duplicates: bool = False,
    seed: int | None,
    field_sharpness: str = "marquee",
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Shared setup for the default (vs-a-separate-field) mode: build
    the user's own entries and sample an opponent field to rank them
    against."""
    contest, entries = _build_contest_and_entries(
        slate, contest_type, num_lineups,
        max_exposure_pct=max_exposure_pct, field_size=field_size,
        min_salary=min_salary, max_salary=max_salary,
        allow_duplicates=allow_duplicates, seed=seed,
    )

    field_sample = sample_size or min(contest["field_size"], MAX_SAMPLE_SIZE)
    field = generate_field(
        slate, field_sample,
        min_salary=min_salary, max_salary=max_salary,
        seed=(seed + 1) if seed is not None else None,
        field_sharpness=field_sharpness,
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
    min_salary: int = 0,
    max_salary: int = SALARY_CAP,
    allow_duplicates: bool = False,
    seed: int | None = None,
    field_sharpness: str = "marquee",
) -> dict[str, Any]:
    """
    The deterministic contest generator: build up to `num_lineups` of
    the user's own entries for a named contest type, ranked against a
    simulated opponent field's *projected* points -- see
    build_contest_entries_simulated() for the real Monte Carlo
    alternative. Mirrors contest.py's build_contest_entries() output
    shape.
    """
    contest, entries, field = _build_entries_and_field(
        slate, contest_type, num_lineups,
        max_exposure_pct=max_exposure_pct, field_size=field_size, sample_size=sample_size,
        min_salary=min_salary, max_salary=max_salary,
        allow_duplicates=allow_duplicates, seed=seed, field_sharpness=field_sharpness,
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
    Ranks `entries` against nfl_variance.simulate_batch()'s real Monte
    Carlo outcome distribution instead of a single projected-points
    snapshot -- entries and field are simulated TOGETHER, trial by
    trial. See contest.py's evaluate_batch_simulated() for the full
    "distinct ranks within one trial" rationale, which applies
    unchanged here (only the simulation source differs: nfl_variance
    instead of variance.py, no `engine="atbat"` alternative).
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

    player_pools = await nfl_variance.player_pools_for_entries(entries + field, season)
    sim = nfl_variance.simulate_batch(entries + field, player_pools, num_trials=num_trials, seed=seed)
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

    player_pools = await nfl_variance.player_pools_for_entries(field_lineups, season)
    sim = nfl_variance.simulate_batch(field_lineups, player_pools, num_trials=num_trials, seed=seed)

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
    min_salary: int = 0,
    max_salary: int = SALARY_CAP,
    allow_duplicates: bool = False,
    seed: int | None = None,
    self_play: bool = False,
    field_sharpness: str = "marquee",
    first_place_pct: float | None = None,
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
    if self_play:
        contest, entries = _build_contest_and_entries(
            slate, contest_type, num_lineups,
            max_exposure_pct=max_exposure_pct, field_size=field_size,
            min_salary=min_salary, max_salary=max_salary,
            allow_duplicates=allow_duplicates, seed=seed,
        )
        evaluation = await evaluate_field_mirrored(
            entries, contest, season=season, num_trials=num_trials, seed=seed,
            first_place_pct=first_place_pct,
        )
    else:
        contest, entries, field = _build_entries_and_field(
            slate, contest_type, num_lineups,
            max_exposure_pct=max_exposure_pct, field_size=field_size, sample_size=sample_size,
            min_salary=min_salary, max_salary=max_salary,
            allow_duplicates=allow_duplicates, seed=seed, field_sharpness=field_sharpness,
        )
        evaluation = await evaluate_batch_simulated(
            entries, field, contest, season=season, num_trials=num_trials,
            seed=(seed + 2) if seed is not None else None,
            first_place_pct=first_place_pct,
        )

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
