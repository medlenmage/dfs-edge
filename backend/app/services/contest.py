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

import math
import random
from collections import Counter
from collections.abc import Callable
from typing import Any

import numpy as np

from app.services import atbat_sim, variance
from app.services.lineup_export import players_in_slot_order, stack_info
from app.services.optimizer import (
    MAX_HITTERS,
    ONE_OFF_QUALITY_MIN,
    ONE_OFF_QUALITY_RATIO,
    SALARY_CAP,
    SLOT_REQUIREMENTS,
    SLOT_TYPES,
    OptimizerError,
    build_player_pool,
)

MAX_SAMPLE_SIZE = 5000
MAX_FIELD_SIZE = 200_000
MAX_USER_LINEUPS = 10_000
_OWNERSHIP_FLOOR = 0.5  # even an unowned player gets a sliver of sampling weight

# "Floor"/"ceiling" report the 5th/95th percentile of a lineup's own
# simulated trials, not the true min/max -- the literal single most
# extreme outcome out of num_trials (usually 10,000) draws reads as an
# implausible freak event even under a genuinely well-calibrated model
# (confirmed with the user after a real diagnostic: the 10th/90th
# percentile on a real batch already landed in a realistic range, e.g.
# ~50-60 to ~130, while the true max routinely reached 220-260+, a gap
# consistent with a fatter-than-normal right tail regardless of any
# labeling choice). p5/p95 is still a genuinely extreme outcome -- one
# trial in twenty -- just not the single rarest of thousands.
FLOOR_CEILING_PERCENTILE = 5

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

# Entry generation used to be entirely ownership-BLIND: pick by
# projection and let cumulative ownership fall where it may. Measured
# against real contests, that produced a systematic contrarian lean --
# on 9/2 the generated entries averaged 112.5% cumulative ownership
# against a real field at 130.8%, roughly 18 points light, and the same
# gap (-14 to -31 points vs the modelled field) showed up on every
# slate checked.
#
# That lean would only be right if real winners were contrarian. They
# are not. Across 22 archived contests, the top-10 finishers average
# -1.2 points of cumulative ownership against THEIR OWN field (median
# +1.7, ratio 1.01x) -- winners land on the field, and whether chalk or
# contrarian wins is a slate-by-slate coin flip with enormous variance
# (-130pp to +60pp). Building every lineup 18 points below the field is
# therefore a standing bet on one side of that coin.
#
# An ownership term recentres it:
#
#     exp     entries vs modelled field, 4 slates      dupes
#     0.0     -14.2  -19.6  -15.1  -30.9               5.9%
#     0.4      -3.1   -4.0   +0.2   -4.3               7.4%
#     0.5      +1.7   -2.5   +3.1   +9.2              11.1%
#
# 0.4 lands consistently just under the field, which is where the
# winners actually sit; 0.5 both overshoots and scatters, and doubles
# the rate at which the batch duplicates itself.
#
# BE HONEST ABOUT WHAT THIS BUYS. It does not make the batch score
# better. Measured across k = 0.0 / 0.25 / 0.40 / 0.55 on a real slate,
# average top-1% rate is flat (1.33, 1.36, 1.35, 1.33) and ROI moves
# only within its own noise. That is the EXPECTED result, and a good
# sign rather than a bad one: the simulator now scores ownership close
# to neutrally (corr(own, top-1%) ~ +0.05 after the field calibration),
# so moving a batch's ownership should not move its simulated finish.
# If it did, the sim would still be carrying an ownership bias.
#
# What it buys is that the batch stops making an unpaid directional
# bet. Building every lineup 18 points under the field only pays if
# chalk systematically loses, and across 22 real contests it does not.
# Projected points also come up slightly (99.7 -> 101.7), because
# ownership and projection agree about most of a slate.
#
# And it is not a mere nudge at the extremes: across ownership's real
# range (floor to ~43%) the term is worth ~5.8x, so a player has to
# project about 1.8x higher to outweigh the chalkiest option. Across
# each factor's FULL real range projection still dominates comfortably
# (~27x for a 5-to-15-point spread against ~5.8x), and the pathological
# case -- a high projection at near-zero ownership -- is rare precisely
# because ownership tracks projection. The measured batch-level
# projection going UP rather than down is the evidence that it does not
# fire often enough to matter.
#
# Degrades to a no-op when a slate carries no ownership data at all:
# every player floors to the same value, so the term is a constant and
# cancels out of the sampling weights.
_ENTRY_OWNERSHIP_EXPONENT = 0.40

# How hard a lineup in progress is steered toward SPENDING the cap.
# The affordability check below has always guarded one direction only
# (never overspend); nothing ever stopped a random walk drifting cheap,
# so a real batch came back with entries thousands of dollars under the
# cap -- money that buys real projected points. At each slot the pick's
# sampling weight is multiplied by (salary / cheapest_affordable) raised
# to this strength times a `pressure` term measuring how far behind the
# cap's own pace the lineup already is (0 = on pace and no reshaping of
# the weights at all, 1 = can only reach the cap by taking the most
# expensive option everywhere). A lineup that has already paid up is
# left alone; one that fell behind pulls itself back.
#
# Swept on a real slate against both salary AND projected points -- see
# scripts/sweep_salary_pacing.py. Strength is a genuine free lunch here
# rather than a trade-off, because spending the cap generally buys
# better players: the sweep raised BOTH at once.
# Swept over 2,000 real entries on a real slate (2026-08-30):
#
# strength   median salary   under $47k   avg proj pts   top player exposure
#    0.0        48,800         24.4%          97.82             26%
#    4.0        49,700          3.9%          99.95             40%
#    6.0        49,800          2.1%         100.39             44%   <- chosen
#    8.0        49,800          0.6%         100.75             50%
#   10.0        49,900          0.5%         101.17             55%
#
# Salary and points climb together, so the real trade-off isn't between
# them -- it's diversity. Weighting harder toward salary concentrates
# the whole contest onto the same expensive players, which matters a
# lot now that the batch IS the field rather than a handful of entries
# in someone else's. 6.0 more than halves the under-cap tail versus
# 4.0 while keeping the chalkiest player in a realistic low-40s share;
# 8.0 buys another 1.5 points of tail for six points of concentration,
# and by 10.0 distinct builds start dropping off too.
_SALARY_PACING_STRENGTH = 6.0

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
# The COMPLETE constraint-distinct shape space, not a curated subset.
# With 8 hitter slots and DK's 5-hitters-per-team cap, every buildable
# hitter composition is an integer partition of <= 8 into parts of 2-5
# (a singleton isn't a constraint -- one player has nothing to stack
# with -- so "5-2-1" collapses to [5, 2], "4-3-1" to [4, 3], "5-1-1-1"
# to [5], and so on). Enumerating that space fully gives exactly the
# seventeen shapes below; the previous nine-shape list was missing
# 3-2-2, 2-2-2-2, 3-2, 2-2-2, 2-2 and the single-group mini shapes,
# which real fields genuinely build -- mini-stack and scatter
# constructions are a documented small-slate differentiation play
# (Stokastic/RotoGrinders both describe 2-man mini-stacks and unusual
# constructions as real GPP tools), and on a 2-3 game slate they're a
# meaningful share of the actual field.
#
# Ordered by how often each shape wins/appears in real large-field
# GPPs: the classic primary+secondary builds first (5-3, 5-2-1 -- the
# canonical DK tournament shapes, since one 5-man rally pays every
# slot), then 4-primary builds, then the 3-primary and mini/scatter
# tail.
STACK_SHAPES: list[list[int]] = [
    [5, 3],
    [5, 2],        # "5-2-1"
    [4, 4],
    [4, 3],        # "4-3-1"
    [4, 2, 2],
    [5],           # "5-1-1-1"
    [4, 2],        # "4-2-1-1"
    [3, 3, 2],
    [3, 3],        # "3-3-1-1"
    [3, 2, 2],     # "3-2-2-1"
    [2, 2, 2, 2],  # game mini-stacks across the slate
    [4],           # "4-1-1-1-1"
    [3, 2],
    [2, 2, 2],
    [3],
    [2, 2],
    [2],           # one lone mini-stack, the most scattered real build
]
# Rank-based decay, first-listed shape heaviest. 0.8 over seventeen
# shapes puts the scatter tail at ~3% of the 5-3's weight -- present
# the way it is in real fields, never dominant.
_STACK_SHAPE_DECAY = 0.8
STACK_SHAPE_WEIGHTS: list[float] = [_STACK_SHAPE_DECAY**i for i in range(len(STACK_SHAPES))]

# Named presets covering the common DK contest shapes. `rake_pct` and
# the payout curve below are a simplified, clearly-approximate model of
# how real payout tables behave (top-heavy for GPPs, flat for
# double-ups) -- not scraped or hardcoded from any specific live
# contest, which would go stale immediately and varies contest to
# contest anyway.
# `sizes` is the list of real contest sizes each preset actually comes
# in, and it's the ONLY size control the generator exposes: the number
# of lineups built and the contest's own field size are the same number
# now, because the generator builds a CONTEST, not a handful of entries
# to drop into someone else's. `field_size` is each preset's default
# pick out of its own `sizes`.
CONTEST_TYPES: dict[str, dict[str, Any]] = {
    "double_up": {
        "label": "Double-up / 50-50",
        "field_size": 100,
        "sizes": [50, 100],
        "entry_fee": 10.0,
        "payout_pct": 0.45,
        "shape": "flat",
    },
    "gpp_small": {
        "label": "Small-field GPP (3-max)",
        "field_size": 500,
        "sizes": [100, 500, 999],
        "entry_fee": 10.0,
        "payout_pct": 0.20,
        "shape": "top_heavy",
        "first_place_pct": 15.0,
    },
    "gpp_mid": {
        "label": "Mid-field GPP (1K-5K)",
        "field_size": 3_000,
        "sizes": [1_000, 2_000, 3_000, 4_000, 5_000],
        "entry_fee": 10.0,
        "payout_pct": 0.19,
        "shape": "top_heavy",
        "first_place_pct": 15.0,
    },
    "gpp_large": {
        "label": "Large-field GPP",
        "field_size": 10_000,
        "sizes": [6_000, 7_000, 8_000, 9_000, 10_000],
        "entry_fee": 5.0,
        "payout_pct": 0.18,
        "shape": "top_heavy",
        "first_place_pct": 15.0,
    },
    "gpp_milly": {
        "label": "Massive-field GPP (millionaire-maker style)",
        "field_size": 100_000,
        "sizes": [12_500, 15_000, 20_000, 25_000, 50_000, 100_000],
        "entry_fee": 20.0,
        "payout_pct": 0.15,
        "shape": "top_heavy",
        "first_place_pct": 15.0,
    },
}
# 1st place's real share of the prize pool in a genuine large-field GPP
# (10-20% per published real DK payout distributions) is far more
# concentrated than the smooth `1/(rank+1)^0.7` power-law curve alone
# produces (confirmed: gpp_large's curve without an override put 1st
# at just 3.5% of the pool) -- the curve is otherwise a close match to
# real payout norms at every other checkpoint (top 0.1%/1%/5%/10%/the
# min-cash line all land within the published real ranges), so only
# the very top needed a real anchor. 15.0 is the midpoint of that
# published 10-20% range, applied uniformly since the reference isn't
# field-size-specific and the curve's SHAPE (not the field size) is
# what determines every other rank's payout as a fraction of the pool.
RAKE_PCT = 0.15
TOP_HEAVY_EXPONENT = 0.7


class ContestError(ValueError):
    """Bad contest params or nothing to build a field from -- routed to an HTTP 400."""


def _validate_salary_range(min_salary: int, max_salary: int) -> None:
    if max_salary > SALARY_CAP:
        raise ContestError(f"max_salary ({max_salary}) can't be more than the ${SALARY_CAP} salary cap.")
    if min_salary > max_salary:
        raise ContestError("min_salary can't be more than max_salary.")


def _one_off_quality_ids(candidates_by_slot: dict[str, list[dict[str, Any]]]) -> frozenset[int]:
    """
    Same "payup or high-FPTS" definition as optimizer.py's own
    _one_off_quality_ids() (salary or projected FPTS within
    ONE_OFF_QUALITY_RATIO of the best available at that slot type),
    computed from this module's per-slot candidate lists instead of
    optimizer.py's flat pool shape.
    """
    hitter_slot_types = set(SLOT_TYPES) - {"P"}
    best_salary = {s: max((p["salary"] for p in candidates_by_slot[s]), default=0) for s in hitter_slot_types}
    best_fpts = {
        s: max((p["projected_fpts"] for p in candidates_by_slot[s]), default=0) for s in hitter_slot_types
    }
    qualifying: set[int] = set()
    for slot in hitter_slot_types:
        for p in candidates_by_slot[slot]:
            if (
                p["salary"] >= ONE_OFF_QUALITY_RATIO * best_salary[slot]
                or p["projected_fpts"] >= ONE_OFF_QUALITY_RATIO * best_fpts[slot]
            ):
                qualifying.add(p["id"])
    return frozenset(qualifying)


def _attach_duplicate_counts(lineups: list[dict[str, Any]]) -> None:
    """
    How many identical copies of each exact lineup (same 10 players)
    ended up in this set -- attached to each lineup as
    `duplicate_count`. Always 1 for generate_entries() under its
    default distinctness guarantee; meaningful once allow_duplicates
    lets exact repeats through, and generate_field() already allows
    duplicates by design (real chalk-heavy fields cluster that way).
    """
    signatures = [frozenset(p["id"] for p in lu["players"]) for lu in lineups]
    counts = Counter(signatures)
    for lu, sig in zip(lineups, signatures):
        lu["duplicate_count"] = counts[sig]


def _split_duplicate_payouts(
    entries: list[dict[str, Any]], results: list[dict[str, Any]], fields: list[str]
) -> None:
    """
    Real DK entries that are exact duplicates (same 10 players) always
    score identically in the real world -- they genuinely tie for
    whichever consecutive block of ranks they land in, and DK's own
    tie-breaking rule splits the combined payout across those ranks
    evenly among the tied entries, rather than each claiming a full
    individual payout as if it alone occupied its rank.

    The rank-assignment already run before this is called places
    duplicate entries at consecutive ranks (identical projected/
    simulated scores always sort adjacent to each other), so each
    duplicate's individually-computed value in `fields` already
    reflects the payout for one specific rank in that contiguous block
    -- averaging those values across the group reproduces exactly the
    "split the block's combined payout evenly" result, with no need to
    touch the rank-assignment logic itself.
    """
    signatures = [frozenset(p["id"] for p in lu["players"]) for lu in entries]
    groups: dict[frozenset[int], list[int]] = {}
    for i, sig in enumerate(signatures):
        groups.setdefault(sig, []).append(i)

    for idxs in groups.values():
        if len(idxs) < 2:
            continue
        for field in fields:
            shared = sum(results[i][field] for i in idxs) / len(idxs)
            for i in idxs:
                results[i][field] = round(shared, 2)


def _ownership_weight(p: dict[str, Any]) -> float:
    return max(p["ownership_pct"], _OWNERSHIP_FLOOR)


def _duplication_risk(picks: list[dict[str, Any]]) -> float:
    """
    Cumulative ("product") ownership, in LOG space -- multiplying 10
    real ownership fractions together underflows fast (they're all well
    under 1.0), so this sums their logs instead: an exactly equivalent
    ranking (log is monotonic) with none of the numerical issues.

    This is the real "how likely is another random entry to be an exact
    duplicate" read -- and it's genuinely different from
    `total_ownership_pct` (the SUM), including in a counter-intuitive
    direction: by the AM-GM inequality, for a FIXED sum, the product
    (and so this log-sum) is HIGHEST when every value is close to
    equal, and lowest when it's dominated by one big outlier. So a
    lineup with one 90%-owned "must play" plus nine barely-owned unique
    pieces actually scores LOWER (more negative, less duplicable) here
    than a lineup where all 10 players are moderately chalky at the
    identical summed ownership -- correctly, since exactly replicating
    the first lineup needs another entry to independently land on the
    same nine unlikely picks, while the second only needs everyone to
    play the obvious plays, which real fields do constantly. Less
    negative (closer to 0) = higher cumulative ownership = more likely
    to be widely duplicated by the field.
    """
    return round(
        sum(math.log(max(p["ownership_pct"], _OWNERSHIP_FLOOR) / 100) for p in picks), 3
    )


def _fpts_weight(p: dict[str, Any]) -> float:
    """
    Sampling weight for lineups YOU would enter -- dominated by
    projected points, with a light ownership tilt so a batch centres on
    the field instead of sitting ~18 points under it (see
    _ENTRY_OWNERSHIP_EXPONENT for the measurement). Distinct from
    _ownership_weight, which models the OPPONENTS.
    """
    # .get, unlike _ownership_weight's strict lookup: this one is also
    # called with hand-built player dicts (stack-team weighting, late
    # swap) that carry no ownership at all. Missing reads as the floor,
    # which is the same constant every player gets on a slate with no
    # ownership data -- so the term cancels rather than misranking.
    return (max(p["projected_fpts"], _FPTS_FLOOR) ** _FPTS_SAMPLING_EXPONENT) * (
        max(p.get("ownership_pct") or 0.0, _OWNERSHIP_FLOOR) ** _ENTRY_OWNERSHIP_EXPONENT
    )


# Field sharpness -- how concentrated the simulated opponent field is
# around the most obvious plays, independent of which real contest
# (field_size/entry_fee/payout) is being modeled.
FIELD_SHARPNESS_LEVELS = ("low", "marquee", "high")
#
# These describe the STAKES of the contest, and therefore who is in it.
# The ordering used to be inverted -- "low" produced the least chalky
# field and "high" the most, which is backwards from how real fields
# behave:
#
#   low      A cheap contest is full of newer and more risk-averse
#            entrants, and they build the chalkiest lineups on the
#            board. This is the MOST concentrated field, not the least.
#
#   marquee  A milly-maker or other massive-field contest, often at a
#            low-to-mid entry fee: a genuine mix of safe entrants and
#            grinders, so it lands between the other two.
#
#   high     High stakes. These players know they have to be different
#            to win, so they limit chalk and hunt lower-owned plays --
#            the LEAST concentrated field.
#
# Exponent > 1 sharpens the ownership curve (chalk pulls even harder);
# exponent < 1 flattens it (chalk still leads, by less).
#
# CALIBRATED AGAINST REAL CONTESTS (scripts/calibrate_sim.py, 4 real DK
# standings exports, 9/1-9/2, 3,539-7,113 entries each). Every one of
# them is a cheap contest ($0.10-$0.25) -- "low stakes" by these very
# definitions -- and every one landed at essentially the same place:
#
#     real cumulative ownership   mean 130.6-134.9%, sd 28-31
#
# The old exponents were set by eye and put the LOW field at 152%, more
# than 20 points chalkier than any real cheap contest measured. That is
# the whole reason the simulator looked anti-chalk: nothing in
# top_1pct_pct penalises ownership (see evaluate_batch_simulated --
# ranks come from simulated points alone), but a field 20 points
# chalkier than reality means a chalky ENTRY moves in lockstep with the
# field it is ranked against and can never separate from it, while a
# contrarian entry is uncorrelated with the field and its rank swings
# free. Over-chalking the opponents IS the ownership penalty, applied
# where nobody thought to look for it.
#
# These three now bracket the measured value rather than sitting above
# it: low 133.9%, marquee 129.6%, high ~122%.
#
# Honest limit on this: every contest we can measure is at the cheap
# end, so `low` is anchored to real data and the other two are still a
# modelled belief about how fields change with stakes -- but a belief
# with a much NARROWER spread than before, because the one point we can
# check came in far below where the model had put it. Re-run
# calibrate_sim.py against a real high-stakes export if one ever turns
# up; that is the number that would move `high`.
_LOW_STAKES_OWNERSHIP_EXPONENT = 0.90
_MARQUEE_OWNERSHIP_EXPONENT = 0.78
_HIGH_STAKES_OWNERSHIP_EXPONENT = 0.5

# But "high stakes" is not "contrarian for its own sake", and modelling
# it as pure ownership-dampening gets the field wrong in a way that
# matters. Measured on a real slate: dampening alone dropped cumulative
# ownership to 108% but only 30.6% of the sub-8%-owned players it
# rostered had an above-average matchup -- WORSE than the marquee
# field's 35.2%. That is a field of random cheap names, which is not
# what a high-stakes player is doing.
#
# A sharp entrant takes low-owned plays that have something behind them:
# the good game environment that isn't the obvious Coors game, the bat
# facing a good pitcher who is on short rest or into a wind that turns
# the park over. `edge_composite` is exactly that signal already --
# scoring.py's matchup multiplier, carrying park, weather, opposing
# starter and bullpen quality, platoon and recent form, centered on 1.00.
#
# Weighting by it lifts the share of GOOD low-owned picks while keeping
# cumulative ownership below the marquee field.
#
# The exponent was 12, chosen because the multiplier's range is narrow
# (0.82-1.20 live) so a gentle one moves nothing. Sweeping it against a
# real slate showed 12 was well past the point of any benefit, and paid
# for the overshoot in duplication:
#
#     edge exp    cumulative own    exact dupes    good low-owned picks
#        2            120.3%            2.3%              20.8%
#        4            121.9%            5.3%              21.7%
#        6            124.1%            5.4%              21.5%
#       12            124.5%           25.5%              21.8%
#
# The thing the exponent exists to buy -- low-owned picks that are
# actually in a good spot -- is flat from 4 upward (21.7% vs 21.8%).
# What kept climbing was duplication: at 12 a quarter of the field is
# an exact copy of another lineup, against 8-11% in the four real
# contests measured. That collapse is self-defeating on its own terms,
# since a field that all finds the same "hidden" edge is no longer
# hunting anything, and it distorts every entry ranked against it.
_HIGH_STAKES_EDGE_EXPONENT = 4.0


def _field_weight_fn(field_sharpness: str) -> Callable[[dict[str, Any]], float]:
    """The per-player sampling weight generate_field() should use for
    one sharpness level -- see FIELD_SHARPNESS_LEVELS' own comment."""
    if field_sharpness == "low":
        # Cheap contest, safer entrants, chalk pulls hardest.
        return lambda p: _ownership_weight(p) ** _LOW_STAKES_OWNERSHIP_EXPONENT
    if field_sharpness == "high":

        def _high_stakes_weight(p: dict[str, Any]) -> float:
            # Chalk is limited rather than abandoned -- a sharp field
            # still plays the obvious plays, just less of them -- and
            # what it reaches for instead is a real matchup edge, not
            # merely a low ownership number.
            edge = p.get("edge_composite")
            edge = 1.0 if edge is None else max(edge, 0.01)
            return (_ownership_weight(p) ** _HIGH_STAKES_OWNERSHIP_EXPONENT) * (
                edge ** _HIGH_STAKES_EDGE_EXPONENT
            )

        return _high_stakes_weight
    # Marquee: a milly-maker's mix of both, so it sits between them.
    return lambda p: _ownership_weight(p) ** _MARQUEE_OWNERSHIP_EXPONENT


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
    min_salary: int = 0,
    max_salary: int = SALARY_CAP,
    one_off_quality_ids: frozenset[int] | None = None,
    max_duplication_risk: float | None = None,
) -> dict[str, Any] | None:
    """
    Build one randomly-weighted lineup within the salary cap (and the
    narrower [min_salary, max_salary] range, if given -- checked once
    the full lineup is built rather than budgeted for slot-by-slot,
    since the existing remaining-budget affordability check already
    keeps every attempt salary-legal; a lineup outside the requested
    range just fails and the caller retries, same as any other
    infeasible random walk here). At each slot, the pick is weighted by
    `weight_fn` (ownership% for the
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

    `one_off_quality_ids`, if given (a partial shape's default "prefer
    payup or high-FPTS" behavior -- see optimizer.py's own version of
    this), restricts the first min(2, leftover) genuine one-off hitter
    picks (slots where no stack group still needs filling) to that set,
    falling back to the ordinary unrestricted pool if the restricted
    one is empty for that slot -- a soft preference, not a hard
    constraint, since a failed restriction here just means one retry
    attempt didn't get a top-quality pick, not that no legal lineup
    exists at all (unlike optimizer.py's exact solve, this sampler has
    no way to know in advance whether the restriction is even
    satisfiable, so it degrades gracefully instead of burning retries
    on a guaranteed failure).

    `max_duplication_risk`, if given, rejects a completed lineup whose
    cumulative (log-product) ownership exceeds it -- see
    _duplication_risk()'s own docstring. Only ever passed by
    generate_entries() for the user's OWN batch; generate_field()'s
    synthetic opponent field deliberately never filters on this, since
    real chalk clustering in the field is the whole point of that
    sample, not something to prune away.

    Returns None if this particular random walk couldn't complete; the
    caller just retries.
    """
    stack_remaining: dict[str, int] = {}
    one_off_required = 0
    if stack_groups is not None and one_off_quality_ids is not None:
        leftover = MAX_HITTERS - sum(stack_groups)
        one_off_required = min(ONE_OFF_QUALITY_MIN, leftover) if leftover > 0 else 0
    one_off_filled = 0
    if stack_groups is not None and team_hitter_pools is not None:
        assignment = _pick_stack_teams(team_hitter_pools, stack_groups, weight_fn, rng)
        if assignment is None:
            return None
        stack_remaining = assignment

    used_ids: set[int] = set()
    picks: list[dict[str, Any]] = []
    salary_so_far = 0
    team_hitter_count: dict[str, int] = {}
    # Teams whose hitters can't join once the pitcher facing them is
    # picked -- a real strikeout/home run is the same at-bat scored two
    # opposite ways, so pairing them is a strict handicap, never a real
    # strategy. Both "P" slots are always filled first (slot_order's own
    # ordering), so this is fully populated before any hitter slot is
    # decided.
    banned_hitter_teams: set[str] = set()

    for i, slot in enumerate(slot_order):
        remaining_slots = slot_order[i + 1 :]
        eligible = [
            p for p in candidates_by_slot[slot] if p["id"] not in used_ids and p["id"] not in excluded_ids
        ]
        if not eligible:
            return None

        needed_teams: set[str] = set()
        if slot != "P":
            eligible = [p for p in eligible if team_hitter_count.get(p["team"], 0) < MAX_HITTERS_PER_TEAM]
            if not eligible:
                return None
            if banned_hitter_teams:
                eligible = [p for p in eligible if p["team"] not in banned_hitter_teams]
                if not eligible:
                    return None

            needed_teams = {t for t, n in stack_remaining.items() if n > 0}
            if needed_teams:
                restricted = [p for p in eligible if p["team"] in needed_teams]
                if restricted:
                    eligible = restricted
            elif one_off_filled < one_off_required:
                quality_eligible = [p for p in eligible if p["id"] in one_off_quality_ids]
                if quality_eligible:
                    eligible = quality_eligible

        remaining_pool = {
            s: [
                p
                for p in candidates_by_slot[s]
                if p["id"] not in used_ids and p["id"] not in excluded_ids
            ]
            for s in remaining_slots
        }
        min_cost_of_rest = sum(
            min((p["salary"] for p in remaining_pool[s]), default=0) for s in remaining_slots
        )
        budget = SALARY_CAP - salary_so_far - min_cost_of_rest
        affordable = [p for p in eligible if p["salary"] <= budget]
        if not affordable:
            return None

        weights = [weight_fn(p) for p in affordable]
        # Salary pacing -- see _SALARY_PACING_STRENGTH. `max_cost_of_rest`
        # is the symmetric counterpart to min_cost_of_rest above: the most
        # this lineup could still spend if it took the priciest option at
        # every slot from here (this one included). `pressure` is how much
        # of that remaining headroom the lineup MUST use to finish at the
        # cap -- 0 when it's already on pace, 1 when only the most
        # expensive path still gets there. Nothing is forbidden; expensive
        # picks are just weighted up in proportion to how far behind the
        # walk has fallen.
        if _SALARY_PACING_STRENGTH:
            max_cost_of_rest = max(
                (p["salary"] for p in affordable), default=0
            ) + sum(max((p["salary"] for p in remaining_pool[s]), default=0) for s in remaining_slots)
            if max_cost_of_rest > 0:
                pressure = (SALARY_CAP - salary_so_far) / max_cost_of_rest
                pressure = max(0.0, min(1.0, pressure))
                if pressure > 0:
                    cheapest = min(p["salary"] for p in affordable) or 1
                    exponent = _SALARY_PACING_STRENGTH * pressure
                    weights = [
                        w * (p["salary"] / cheapest) ** exponent
                        for w, p in zip(weights, affordable)
                    ]
        pick = rng.choices(affordable, weights=weights, k=1)[0]

        picks.append(pick)
        used_ids.add(pick["id"])
        salary_so_far += pick["salary"]
        if slot == "P":
            if pick.get("opponent"):
                banned_hitter_teams.add(pick["opponent"])
        else:
            team_hitter_count[pick["team"]] = team_hitter_count.get(pick["team"], 0) + 1
            if not needed_teams and one_off_quality_ids and pick["id"] in one_off_quality_ids:
                one_off_filled += 1
        if stack_remaining.get(pick["team"], 0) > 0:
            stack_remaining[pick["team"]] -= 1

    if any(n > 0 for n in stack_remaining.values()):
        return None  # couldn't fully satisfy the chosen shape's team groups -- caller retries
    if not (min_salary <= salary_so_far <= max_salary):
        return None  # outside the requested salary range -- caller retries

    duplication_risk = _duplication_risk(picks)
    if max_duplication_risk is not None and duplication_risk > max_duplication_risk:
        return None  # too chalky -- caller retries

    stack_type, stack = stack_info({"players": picks})
    return {
        "salary_used": salary_so_far,
        "stack_type": stack_type,
        "stack": stack,
        "projected_points": round(sum(p["projected_fpts"] for p in picks), 2),
        "total_ownership_pct": round(sum(p["ownership_pct"] for p in picks), 1),
        "duplication_risk": duplication_risk,
        "players": [
            {
                "id": p["id"],
                "name": p["name"],
                "team": p["team"],
                "salary": p["salary"],
                "projected_fpts": p["projected_fpts"],
                "ownership_pct": p["ownership_pct"],
                "edge_composite": p.get("edge_composite"),
                # DK's own numeric player id, carried through for
                # dk_entry_manager.py's real-CSV export -- empty string
                # when no DK salary file is loaded (optimizer.build_player_pool()).
                "dk_id": p.get("dk_id") or "",
                # Which real game this player is in -- late_swap.py
                # needs it to tell a locked roster spot from a still-
                # swappable one, and a scratched player can vanish
                # from the current pool entirely, so it has to be
                # captured here at build time rather than looked up
                # from the live slate later.
                "game_pk": p.get("game_pk"),
            }
            for p in picks
        ],
        "player_ids": frozenset(used_ids),
    }


def _build_candidate_pool(
    slate: dict[str, Any],
    included_game_pks: list[int] | None,
    projection_source: str = "rotowire",
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Shared setup for both generators: the eligible-by-slot pool and
    the fixed 10-slot fill order, or a ContestError if either is empty."""
    try:
        pool = build_player_pool(
            slate,
            included_game_pks=set(included_game_pks) if included_game_pks is not None else None,
            projection_source=projection_source,
        )
    except OptimizerError as exc:
        raise ContestError(str(exc)) from exc
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


def build_chalk_lineup(
    slate: dict[str, Any],
    *,
    projection_source: str = "rotowire",
    included_game_pks: list[int] | None = None,
    min_salary: int = 0,
    max_salary: int = SALARY_CAP,
) -> dict[str, Any]:
    """
    The single most heavily-chalk lineup possible: at every slot, the
    highest-owned affordable player, picked greedily -- no randomness,
    no stack-shape preference, no attempt to be good, just maximally
    "the obvious plays." A deliberately zero-skill baseline, not a real
    lineup anyone would build on purpose.

    This exists for one reason: real DFS rake means the site keeps
    RAKE_PCT of every dollar entered regardless of who wins, so even
    the most popular possible lineup on a slate should show a simulated
    ROI close to -RAKE_PCT*100 (the site's cut) once run through
    evaluate_batch_simulated() against a realistic field -- not a real
    edge. If it instead comes back showing strong positive ROI, that's
    proof the field-generation model itself is broken (too weak, or
    insufficiently correlated with real ownership), not that this
    lineup found genuine value. See the "chalk lineup" sanity checks in
    test_pipeline.py for the automated version of this claim.
    """
    candidates_by_slot, slot_order = _build_candidate_pool(
        slate, included_game_pks, projection_source
    )

    used_ids: set[int] = set()
    picks: list[dict[str, Any]] = []
    salary_so_far = 0
    team_hitter_count: dict[str, int] = {}
    banned_hitter_teams: set[str] = set()
    # Teams a picked pitcher is facing -- a *second* pitcher pick whose
    # own team is one of these would be the literal opposing starter of
    # a game already represented in this lineup. _sample_one_lineup
    # never hits this in practice (randomized + retried, so two
    # opposing starters landing in the same 2-pitcher lineup back to
    # back is a rare coincidence it just retries away); a deliberately
    # deterministic, non-randomized greedy walk has no such luck to
    # rely on, so it's excluded explicitly here -- picking both starters
    # of one game would ban HITTERS from both teams at once (via
    # banned_hitter_teams below), which can starve every hitter slot on
    # a thin slate.
    picked_pitcher_opponents: set[str] = set()

    for i, slot in enumerate(slot_order):
        remaining_slots = slot_order[i + 1 :]
        eligible = [p for p in candidates_by_slot[slot] if p["id"] not in used_ids]
        if slot == "P":
            if picked_pitcher_opponents:
                non_opposing = [p for p in eligible if p["team"] not in picked_pitcher_opponents]
                if non_opposing:
                    eligible = non_opposing
        else:
            eligible = [p for p in eligible if team_hitter_count.get(p["team"], 0) < MAX_HITTERS_PER_TEAM]
            if banned_hitter_teams:
                eligible = [p for p in eligible if p["team"] not in banned_hitter_teams]
        if not eligible:
            raise ContestError(
                f"Couldn't build a chalk lineup -- no eligible players left for the {slot} slot."
            )

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
            raise ContestError("Couldn't build a chalk lineup within the salary cap.")

        pick = max(affordable, key=_ownership_weight)
        picks.append(pick)
        used_ids.add(pick["id"])
        salary_so_far += pick["salary"]
        if slot == "P":
            if pick.get("opponent"):
                banned_hitter_teams.add(pick["opponent"])
                picked_pitcher_opponents.add(pick["opponent"])
        else:
            team_hitter_count[pick["team"]] = team_hitter_count.get(pick["team"], 0) + 1

    if not (min_salary <= salary_so_far <= max_salary):
        raise ContestError(
            f"The most-chalk lineup available (${salary_so_far:,}) falls outside the "
            f"requested salary range (${min_salary:,}-${max_salary:,})."
        )

    stack_type, stack = stack_info({"players": picks})
    return {
        "salary_used": salary_so_far,
        "stack_type": stack_type,
        "stack": stack,
        "projected_points": round(sum(p["projected_fpts"] for p in picks), 2),
        "total_ownership_pct": round(sum(p["ownership_pct"] for p in picks), 1),
        "duplication_risk": _duplication_risk(picks),
        "players": [
            {
                "id": p["id"],
                "name": p["name"],
                "team": p["team"],
                "salary": p["salary"],
                "projected_fpts": p["projected_fpts"],
                "ownership_pct": p["ownership_pct"],
                "edge_composite": p.get("edge_composite"),
                # DK's own numeric player id, carried through for
                # dk_entry_manager.py's real-CSV export -- empty string
                # when no DK salary file is loaded (optimizer.build_player_pool()).
                "dk_id": p.get("dk_id") or "",
                # Which real game this player is in -- late_swap.py
                # needs it to tell a locked roster spot from a still-
                # swappable one, and a scratched player can vanish
                # from the current pool entirely, so it has to be
                # captured here at build time rather than looked up
                # from the live slate later.
                "game_pk": p.get("game_pk"),
            }
            for p in picks
        ],
        "player_ids": frozenset(used_ids),
    }


def generate_field(
    slate: dict[str, Any],
    sample_size: int,
    *,
    projection_source: str = "rotowire",
    included_game_pks: list[int] | None = None,
    max_attempts_per_lineup: int = 25,
    min_salary: int = 0,
    max_salary: int = SALARY_CAP,
    seed: int | None = None,
    field_sharpness: str = "marquee",
) -> list[dict[str, Any]]:
    """
    Build `sample_size` synthetic opponent lineups, weighted toward
    whatever ownership% says the public actually rosters -- RotoWire's
    by default, or `inhouse_projections.py`'s own model via
    `projection_source="inhouse"`. `included_game_pks` restricts the
    candidate pool the same way it does for the optimizer -- pass the
    same slate-game selection a user's own lineups were built against
    so the field reflects the actual DK slate, not every game MLB's
    schedule returns for the date. `min_salary`/`max_salary` bound each
    sampled lineup's total salary (unconstrained by default here; the
    HTTP API applies a $47,000 floor unless overridden).

    `field_sharpness`: one of FIELD_SHARPNESS_LEVELS -- see its own
    comment for what "low"/"marquee"/"high" each actually change about
    the sample.
    """
    if sample_size < 1:
        raise ContestError("sample_size must be at least 1.")
    # MAX_USER_LINEUPS, not MAX_SAMPLE_SIZE: this builds the contest
    # itself now (build_contest_lineups), not only a sample for the
    # simulator to rank against. MAX_SAMPLE_SIZE remains the cap on how
    # much of a batch gets SIMULATED, which is a different question and
    # still enforced in simulate_contest_batch.
    if sample_size > MAX_USER_LINEUPS:
        raise ContestError(f"sample_size can't exceed {MAX_USER_LINEUPS:,}.")
    if field_sharpness not in FIELD_SHARPNESS_LEVELS:
        raise ContestError(
            f"Unknown field_sharpness '{field_sharpness}'. Choose one of: "
            f"{', '.join(FIELD_SHARPNESS_LEVELS)}."
        )
    _validate_salary_range(min_salary, max_salary)

    candidates_by_slot, slot_order = _build_candidate_pool(
        slate, included_game_pks, projection_source
    )
    team_hitter_pools = _team_hitter_pools(candidates_by_slot, slot_order)
    feasible_shapes, feasible_weights = _feasible_stack_shapes(team_hitter_pools)
    one_off_quality_ids = _one_off_quality_ids(candidates_by_slot)
    field_weight_fn = _field_weight_fn(field_sharpness)

    rng = random.Random(seed)
    field: list[dict[str, Any]] = []
    for _ in range(sample_size):
        # Picked once per lineup, outside the retry loop below, so a
        # genuinely harder shape (5-3, needing 5 salary-expensive
        # hitters from one team) gets max_attempts_per_lineup real
        # shots at a working team assignment instead of losing out to
        # whichever easier shape happens to get rolled on a given retry.
        lineup = None
        # A shape still gets max_attempts_per_lineup dedicated shots --
        # but a shape that exhausts them no longer costs the batch a
        # lineup. Re-roll a different shape (twice), then fall back to
        # fully unconstrained sampling, so one hard multi-group draw on
        # a thin slate can't leave a hole in the field.
        shape_plans = [
            _pick_stack_shape(feasible_shapes, feasible_weights, rng),
            _pick_stack_shape(feasible_shapes, feasible_weights, rng),
            None,
        ]
        for shape in shape_plans:
            for _ in range(max_attempts_per_lineup):
                lineup = _sample_one_lineup(
                    candidates_by_slot,
                    slot_order,
                    rng,
                    field_weight_fn,
                    team_hitter_pools=team_hitter_pools,
                    stack_groups=shape,
                    min_salary=min_salary,
                    max_salary=max_salary,
                    one_off_quality_ids=one_off_quality_ids,
                )
                if lineup is not None:
                    break
            if lineup is not None:
                break
        if lineup is None and field:
            # Same starved-pool fallback as generate_entries: a real
            # field converges onto the same few builds when legal
            # lineups are rare, so duplicate an existing one rather
            # than leaving a hole in the sampled field.
            source = rng.choices(
                field, weights=[max(lu["total_ownership_pct"], 0.1) for lu in field], k=1
            )[0]
            lineup = {
                **{k: v for k, v in source.items() if k != "duplicate_count"},
                "players": [dict(p) for p in source["players"]],
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
    projection_source: str = "rotowire",
    max_exposure_pct: float | None = None,
    included_game_pks: list[int] | None = None,
    max_attempts_per_lineup: int = 30,
    min_salary: int = 0,
    max_salary: int = SALARY_CAP,
    allow_duplicates: bool = False,
    max_duplication_risk: float | None = None,
    seed: int | None = None,
) -> list[dict[str, Any]]:
    """
    Build up to `num_lineups` of the user's OWN entries for a contest --
    each one individually strong (players picked with a heavy lean
    toward higher projected points, via `_fpts_weight`) but, by
    default, genuinely distinct from every other entry in the batch,
    unlike `generate_field`'s opponent model, which deliberately allows
    duplicate/near-duplicate lineups to represent real chalk
    clustering.

    `max_exposure_pct`, if given, caps how often any one player can
    appear across the whole batch -- same idea as optimizer.py's own
    exposure cap, tracked as a running count and enforced by excluding
    a capped player from the candidate pool for the rest of the batch.

    `min_salary`/`max_salary` bound each entry's total salary
    (unconstrained by default here; the HTTP API applies a $47,000
    floor unless overridden).

    `max_duplication_risk`, if given, rejects any candidate entry whose
    cumulative (log-product) ownership -- see `_duplication_risk()` --
    exceeds it, retrying like any other infeasible attempt. Unlike
    `total_ownership_pct` (a sum), this catches a lineup where every
    player is moderately chalky TOGETHER even when its SUM looks
    unremarkable -- the real signal for "the field will build this
    exact lineup many times over," which a GPP wants to avoid on its
    own entries even when the field itself (generate_field()) should
    keep modelling that clustering honestly.

    `allow_duplicates`, if set, lets exact repeats of an earlier entry
    in this batch through -- a real, sometimes-deliberate GPP move
    (entering a signature build multiple times). Every entry carries a
    `duplicate_count` reporting how many identical copies ended up in
    the batch (always 1 under the default distinctness guarantee).

    If the pool or the exposure cap can't support the full count
    requested, returns as many legal entries as it could build rather
    than failing the whole request -- only an empty result raises.
    """
    if num_lineups < 1:
        raise ContestError("num_lineups must be at least 1.")
    if num_lineups > MAX_USER_LINEUPS:
        raise ContestError(f"num_lineups can't exceed {MAX_USER_LINEUPS:,}.")
    _validate_salary_range(min_salary, max_salary)

    candidates_by_slot, slot_order = _build_candidate_pool(
        slate, included_game_pks, projection_source
    )
    team_hitter_pools = _team_hitter_pools(candidates_by_slot, slot_order)
    feasible_shapes, feasible_weights = _feasible_stack_shapes(team_hitter_pools)
    one_off_quality_ids = _one_off_quality_ids(candidates_by_slot)

    def _cap_to_count(pct: float) -> int:
        return max(1, round(pct / 100 * num_lineups))

    rng = random.Random(seed)
    exposure_count: dict[int, int] = {}
    capped_ids: set[int] = set()
    seen_signatures: set[frozenset[int]] = set()
    entries: list[dict[str, Any]] = []
    # Flips to True the first time the pool runs out of DISTINCT builds
    # -- from then on the batch fills with duplicates, the way a real
    # contest does. On a 2-3 game slate the distinct lineup space is
    # genuinely small and real fields duplicate heavily (a chalky build
    # appears dozens of times); stopping the whole batch at the point
    # of first duplication -- the old behavior, "only built 7 of
    # 5,000" -- modeled a contest that doesn't exist. Duplicates still
    # respect every REAL constraint (salary, exposure caps, the
    # 5-hitters-per-team rule); only distinctness, which no real
    # contest enforces, is lifted. _attach_duplicate_counts and the
    # payout tie-splitting already price duplicates honestly.
    duplicates_unlocked = allow_duplicates

    for _ in range(num_lineups):
        # Each shape still gets max_attempts_per_lineup dedicated shots
        # (see generate_field()'s matching comment) -- but a shape that
        # exhausts them re-rolls to a different one, then to fully
        # unconstrained sampling, before the batch ever gives up on the
        # lineup.
        lineup = None
        legal_duplicate = None
        shape_plans = [
            _pick_stack_shape(feasible_shapes, feasible_weights, rng),
            _pick_stack_shape(feasible_shapes, feasible_weights, rng),
            None,
        ]
        for shape in shape_plans:
            for _ in range(max_attempts_per_lineup):
                candidate = _sample_one_lineup(
                    candidates_by_slot,
                    slot_order,
                    rng,
                    _fpts_weight,
                    excluded_ids=frozenset(capped_ids),
                    team_hitter_pools=team_hitter_pools,
                    stack_groups=shape,
                    min_salary=min_salary,
                    max_salary=max_salary,
                    one_off_quality_ids=one_off_quality_ids,
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
            # Every retry produced only already-seen builds: the
            # distinct space is exhausted. Build the contest out with
            # duplicates from here, like the real field would.
            duplicates_unlocked = True
            lineup = legal_duplicate
        if lineup is None and entries:
            # The sampler couldn't complete ANY legal lineup this round
            # -- on a starved pool (a tight salary window over a tiny
            # slate) legal builds can be so rare the random walk misses
            # them for dozens of attempts even though several already
            # exist. A real field in that spot converges onto the same
            # few builds, so duplicate an already-built entry (weighted
            # toward the stronger ones) rather than abandoning the rest
            # of the contest. Exposure caps stay honored: only entries
            # containing no capped player are eligible, and if none
            # qualify the cap is genuinely binding and we stop.
            eligible = [
                e for e in entries
                if not any(p["id"] in capped_ids for p in e["players"])
            ]
            if eligible:
                duplicates_unlocked = True
                source = rng.choices(
                    eligible, weights=[e["projected_points"] for e in eligible], k=1
                )[0]
                lineup = {
                    **{k: v for k, v in source.items() if k != "duplicate_count"},
                    "players": [dict(p) for p in source["players"]],
                    "player_ids": frozenset(p["id"] for p in source["players"]),
                }
        if lineup is None:
            # Genuinely infeasible even WITH duplicates -- the salary
            # range or exposure cap is the binding constraint, not
            # distinctness. That's a real stop, reported honestly.
            break

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


def field_exposure(
    field: list[dict[str, Any]],
    top_n: int = 15,
    *,
    results: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """
    How often each player showed up across the sampled field -- the
    field's own chalk, same shape as the optimizer's exposure report.

    `results`, if given (index-aligned with `field`, each carrying a
    real simulated `roi_pct`), also folds in each player's own average
    ROI across every lineup containing them -- not the lineup-level
    ROI already shown elsewhere, but which INDIVIDUAL PLAYERS are
    actually driving a batch's simulated payoff. A player rostered in
    a few lineups that all cash big shows a real, high avg_roi_pct
    here even if his own ownership% is low.
    """
    counts: dict[int, dict[str, Any]] = {}
    roi_sums: dict[int, float] = {}
    for i, lineup in enumerate(field):
        roi = results[i]["roi_pct"] if results is not None else None
        for p in lineup["players"]:
            entry = counts.setdefault(
                p["id"], {"id": p["id"], "name": p["name"], "team": p["team"], "count": 0}
            )
            entry["count"] += 1
            if roi is not None:
                roi_sums[p["id"]] = roi_sums.get(p["id"], 0.0) + roi
    ranked = sorted(counts.values(), key=lambda e: -e["count"])[:top_n]
    for e in ranked:
        e["pct"] = round(100 * e["count"] / len(field), 1)
        if results is not None:
            e["avg_roi_pct"] = round(roi_sums[e["id"]] / e["count"], 1)
    return ranked


def _entry_passes_filters(
    entry: dict[str, Any],
    *,
    require_teams: frozenset[str],
    exclude_teams: frozenset[str],
    require_player_ids: frozenset[int],
    exclude_player_ids: frozenset[int],
    stack_types: frozenset[str] | None,
) -> bool:
    """
    One entry's pass/fail against reshape_batch()'s Filters -- surgical
    include/exclude by stack team, specific player combo, or named
    stack shape, applied BEFORE ranking/exposure-capping so those steps
    only ever see entries that already satisfy every filter.
    """
    entry_teams = {p["team"] for p in entry["players"]}
    entry_player_ids = {p["id"] for p in entry["players"]}
    if require_teams and not require_teams <= entry_teams:
        return False
    if exclude_teams and entry_teams & exclude_teams:
        return False
    if require_player_ids and not require_player_ids <= entry_player_ids:
        return False
    if exclude_player_ids and entry_player_ids & exclude_player_ids:
        return False
    if stack_types is not None and entry.get("stack_type") not in stack_types:
        return False
    return True


def reshape_batch(
    entries: list[dict[str, Any]],
    results: list[dict[str, Any]],
    *,
    target_count: int | None = None,
    max_exposure_pct: float | None = None,
    player_exposure_caps: dict[int, float] | None = None,
    roi_boosts: dict[int, float] | None = None,
    require_teams: list[str] | None = None,
    exclude_teams: list[str] | None = None,
    require_player_ids: list[int] | None = None,
    exclude_player_ids: list[int] | None = None,
    stack_types: list[str] | None = None,
) -> dict[str, Any]:
    """
    Re-rank and/or re-filter an ALREADY-SIMULATED batch's real results on
    the results screen -- no new Monte Carlo run, this is a pure
    post-hoc reshape over numbers that are already genuine, so a user
    can shape their actual submitted portfolio without a full rebuild
    every time they want to try a different exposure cap.

    Filters (`require_teams`/`exclude_teams`/`require_player_ids`/
    `exclude_player_ids`/`stack_types`) run FIRST, narrowing the
    candidate pool to only entries that satisfy every one given (see
    `_entry_passes_filters`) -- e.g. "only 5-3 shapes stacking CLE" is
    `stack_types=["5-3"], require_teams=["CLE"]`. `target_count`/
    exposure caps below then apply to that narrowed pool, not the
    original batch -- "keep the top N of what's left after filtering,"
    the natural reading of combining a filter with a portfolio size.

    `roi_boosts` (player_id -> ROI PERCENTAGE POINTS, additive, may be
    negative) nudges the SORT ORDER only: each entry's real `roi_pct`
    is never modified, only a separate `adjusted_roi_pct` used to decide
    ranking -- deliberately additive rather than a multiplicative %,
    since multiplying an already-negative roi_pct (the common case --
    most entries in a real GPP lose money) by a positive boost would
    perversely make it MORE negative, exactly backwards from "nudge this
    player's lineups up the rankings."

    `max_exposure_pct`/`player_exposure_caps` (the latter overrides the
    former for specific players) then greedily keep entries -- highest
    `adjusted_roi_pct` first -- up to `target_count` (defaults to the
    whole FILTERED pool), dropping any entry that would push a player's
    exposure over their cap relative to `target_count` (the size of the
    FINAL portfolio being shaped) -- same incremental exposure-counting
    technique already used during generation (`_sample_one_lineup`'s
    `exposure_count`), just applied to a pre-sorted candidate list
    instead of random sampling. A dropped entry doesn't stop the walk --
    a lower-ranked entry further down the list may still fit within the
    caps.
    """
    if not entries or not results:
        raise ContestError("Need at least one entry to reshape.")
    if len(entries) != len(results):
        raise ContestError("entries and results must be the same length (index-aligned).")

    roi_boosts = roi_boosts or {}
    player_exposure_caps = player_exposure_caps or {}
    original_count = len(entries)

    require_teams_set = frozenset(require_teams or ())
    exclude_teams_set = frozenset(exclude_teams or ())
    require_player_ids_set = frozenset(require_player_ids or ())
    exclude_player_ids_set = frozenset(exclude_player_ids or ())
    stack_types_set = frozenset(stack_types) if stack_types else None

    if require_teams_set or exclude_teams_set or require_player_ids_set or exclude_player_ids_set or stack_types_set is not None:
        filtered = [
            (entry, result)
            for entry, result in zip(entries, results)
            if _entry_passes_filters(
                entry,
                require_teams=require_teams_set,
                exclude_teams=exclude_teams_set,
                require_player_ids=require_player_ids_set,
                exclude_player_ids=exclude_player_ids_set,
                stack_types=stack_types_set,
            )
        ]
        entries = [e for e, _ in filtered]
        results = [r for _, r in filtered]
        if not entries:
            raise ContestError("No entries in this batch match every filter given.")

    target = len(entries) if target_count is None else max(1, min(target_count, len(entries)))

    scored = []
    for entry, result in zip(entries, results):
        player_ids = [p["id"] for p in entry["players"]]
        boost = sum(roi_boosts.get(pid, 0.0) for pid in player_ids)
        scored.append((result["roi_pct"] + boost, entry, result, player_ids))
    scored.sort(key=lambda t: -t[0])

    kept_entries: list[dict[str, Any]] = []
    kept_results: list[dict[str, Any]] = []
    exposure_count: dict[int, int] = {}

    for adjusted, entry, result, player_ids in scored:
        if len(kept_entries) >= target:
            break
        fits = True
        for pid in player_ids:
            cap = player_exposure_caps.get(pid, max_exposure_pct)
            if cap is not None and (exposure_count.get(pid, 0) + 1) / target * 100 > cap + 1e-9:
                fits = False
                break
        if not fits:
            continue
        for pid in player_ids:
            exposure_count[pid] = exposure_count.get(pid, 0) + 1
        kept_entries.append(entry)
        kept_results.append({**result, "adjusted_roi_pct": round(adjusted, 1)})

    return {
        "entries": kept_entries,
        "results": kept_results,
        "exposure": field_exposure(kept_entries, top_n=20, results=kept_results),
        "num_kept": len(kept_entries),
        "num_dropped": len(entries) - len(kept_entries),
        # How many of the ORIGINAL batch (before any filter ran) never
        # even reached the ranking/exposure-cap walk -- distinct from
        # num_dropped above, which only counts entries that DID reach
        # that walk but got capped out. Zero when no filter was given.
        "num_filtered_out": original_count - len(entries),
    }


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


def _block_average_payouts(payouts: np.ndarray, boundaries: np.ndarray, field_size: int) -> np.ndarray:
    """
    A length-`field_size` real per-rank payout array (0 beyond the paid
    ranks), smoothed so every rank within one block reads that block's
    own average payout instead of one single rank's payout. `boundaries`
    is the ascending, 1-indexed real rank where each block STARTS --
    the exact same rank values the caller's own sample-to-field_size
    projection formula already produces (`real_ranks_by_k` in
    evaluate_field_mirrored, an analogous array in
    evaluate_batch_simulated) -- each block runs from one boundary up
    to (not including) the next.

    Why this matters: both simulated-ranking functions below project a
    real contest's `field_size` down onto a much smaller Monte Carlo
    `sample_size` (routine for the large-field GPP presets -- gpp_large
    is 10,000 real entries simulated from a few thousand sampled
    lineups at most, gpp_milly is 100,000 from the same cap). A rank
    achieved *within the sample* -- "3rd best of the 500 lineups
    simulated" -- doesn't correspond to one single real rank; it stands
    in for a whole BLOCK of real ranks (roughly field_size/sample_size
    of them) that would have finished between the sample's 2nd- and
    3rd-best entries in the real, fully-populated contest. Reading off
    just that block's single best-case payout -- what a naive point
    lookup does -- is a real, provable bug: real payout curves are
    sharply convex/top-heavy (`_payout_curve`'s `1/(rank+1)^0.7` decay),
    so the block's best rank pays meaningfully more than its average,
    and the overstatement compounds every trial. Confirmed empirically
    against real slate data: a field ranked purely against itself
    should show an average simulated ROI near -RAKE_PCT*100 at ANY
    compression ratio (a closed-form fact -- aggregate payouts can
    never exceed entry fees minus rake, regardless of any lineup's
    skill) -- the point-lookup version showed roughly correct numbers
    at a 1-2x field_size/sample_size ratio, but drifted to +34% ROI at
    21x and +264% at 209x, exactly the ratios the gpp_large/gpp_milly
    presets' default sample sizes produce.

    Boundaries must come from the CALLER's own rank-projection formula,
    not an independently-derived partition (e.g. `np.array_split`) --
    two different partition schemes covering the same range don't align
    rank-for-rank, so a caller reading off a boundary point that isn't
    exactly a boundary in the smoothing scheme silently reintroduces a
    smaller version of the same bias. Sum-preserving by construction
    (each block replaced by its own mean is a pure within-block
    redistribution), so the smoothed curve still sums to exactly the
    same total (prize_pool) as the original.
    """
    if len(boundaries) >= field_size:
        return payouts
    starts = boundaries
    ends = np.append(starts[1:], field_size + 1)
    cumsum = np.concatenate([[0.0], np.cumsum(payouts)])
    block_means = (cumsum[ends - 1] - cumsum[starts - 1]) / (ends - starts)
    smoothed = np.empty(field_size)
    for k in range(len(starts)):
        smoothed[starts[k] - 1 : ends[k] - 1] = block_means[k]
    return smoothed


def _field_baseline(
    payout_pct: float, prize_pool: float, entry_fee: float, field_size: int
) -> dict[str, float]:
    """
    What ANY random entry -- zero skill, zero edge -- should expect
    from this contest, on average, by definition: exactly `payout_pct`
    of the field cashes (that's the literal meaning of the number), and
    the average dollar in equals `prize_pool / (field_size * entry_fee)`
    dollars out, since the whole prize pool gets split among exactly
    that many real entries. Both are closed-form facts derivable
    directly from the contest's own numbers -- no simulation needed,
    and true regardless of any player's or lineup's actual skill.

    This is the number a real edge should be measured AGAINST: "my
    batch shows +40% ROI" means very different things next to a
    baseline of -15% (a genuine ~55-point edge over an average random
    entry) versus next to +10% (a suspiciously generous field that
    would make +40% look far less impressive by comparison) -- see the
    "field-beating edge" side of the contest generator's summary output
    for the actual comparison.
    """
    return {
        "avg_cash_probability_pct": round(payout_pct * 100, 1),
        "avg_roi_pct": round((prize_pool / (field_size * entry_fee) - 1) * 100, 1),
    }


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
    payouts = _custom_payout_curve(paid_count, prize_pool, contest["shape"], contest.get("first_place_pct"))

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


def batch_summary(
    entries: list[dict[str, Any]],
    results: list[dict[str, Any]],
    contest: dict[str, Any],
) -> dict[str, Any]:
    """
    The batch-level summary block, in whichever of the two real shapes
    matches these results -- simulated (cash probability / ROI, keyed by
    `roi_pct` being present) or deterministic (cashing count / estimated
    payout).

    Exists so a late swap can hand back a summary describing the entries
    it actually produced. It has to be computed here rather than in the
    frontend: the API only ever ships the first 200 entries as a
    preview, so a batch-wide average taken from that sample would be
    quietly wrong for any batch bigger than that.
    """
    n = len(entries)
    if not n or not results:
        return {}
    entry_fee = contest.get("entry_fee") or 0
    total_cost = round(n * entry_fee, 2)
    points = [e["projected_points"] for e in entries]

    def avg(key: str, rows: list[dict[str, Any]], digits: int) -> float:
        return round(sum(r[key] for r in rows) / len(rows), digits)

    if "roi_pct" in results[0]:
        total_expected = round(sum(r["expected_payout"] for r in results), 2)
        return {
            "avg_cash_probability_pct": avg("cash_probability_pct", results, 1),
            "avg_first_place_pct": avg("first_place_pct", results, 2),
            "avg_top_1pct_pct": avg("top_1pct_pct", results, 2),
            "avg_top_10pct_pct": avg("top_10pct_pct", results, 2),
            "avg_roi_pct": avg("roi_pct", results, 1),
            "total_entry_cost": total_cost,
            "total_expected_payout": total_expected,
            "estimated_net_profit": round(total_expected - total_cost, 2),
            "avg_duplication_risk": round(sum(e["duplication_risk"] for e in entries) / n, 3),
        }

    cashing = [r for r in results if r.get("in_the_money")]
    total_payout = round(sum(r["estimated_payout"] for r in results), 2)
    return {
        "cashing_count": len(cashing),
        "cashing_pct": round(100 * len(cashing) / n, 1),
        "total_entry_cost": total_cost,
        "total_estimated_payout": total_payout,
        "estimated_net_profit": round(total_payout - total_cost, 2),
        "avg_roi_pct": round((total_payout / total_cost - 1) * 100, 1) if total_cost else 0.0,
        "avg_salary_used": round(sum(e["salary_used"] for e in entries) / n),
        "avg_projected_points": round(sum(points) / n, 2),
        "min_projected_points": min(points),
        "max_projected_points": max(points),
        "avg_total_ownership_pct": round(sum(e["total_ownership_pct"] for e in entries) / n, 1),
        "avg_duplication_risk": round(sum(e["duplication_risk"] for e in entries) / n, 3),
    }


def evaluate_batch(
    entries: list[dict[str, Any]],
    field: list[dict[str, Any]],
    contest: dict[str, Any],
) -> dict[str, Any]:
    """
    Public entry point for the batch-vs-field ranking below -- used to
    re-rank a batch whose entries have changed since it was built (a
    late swap), where the originally-cached results no longer describe
    the lineups they came with.
    """
    return _evaluate_batch_against_field(entries, field, contest)


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
    payouts = _custom_payout_curve(paid_count, prize_pool, contest["shape"], contest.get("first_place_pct"))

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

    _split_duplicate_payouts(entries, results, ["estimated_payout", "estimated_profit"])

    return {
        "field_size": field_size,
        "sample_size": sample_size,
        "paid_count": paid_count,
        "entry_fee": entry_fee,
        "prize_pool": prize_pool,
        "results": results,
    }


async def _simulate_lineups_atbat(
    lineups: list[dict[str, Any]],
    slate: dict[str, Any],
    season: int,
    *,
    num_trials: int,
    seed: int | None,
    included_game_pks: list[int] | None = None,
) -> np.ndarray:
    """
    The `engine="atbat"` counterpart to variance.player_pools_for_entries()
    + variance.simulate_batch(): builds each lineup's per-trial simulated
    point total by summing atbat_sim.simulate_slate_trials()'s real,
    at-bat-level simulated game results for its own 10 players, instead
    of resampling from each player's independent bootstrap pool.
    Correlation (teammates, a starter vs. the lineup he actually faced)
    is already baked into those per-trial arrays by construction -- no
    separate team-multiplier step needed here, unlike the bootstrap path.

    Returns the same `(len(lineups), num_trials)` shape
    variance.simulate_batch() does, so every downstream ranking/payout
    calculation in evaluate_batch_simulated()/evaluate_field_mirrored()
    is completely unchanged regardless of which engine produced it.
    """
    try:
        player_trials = await atbat_sim.simulate_slate_trials(
            slate, season, num_trials=num_trials, seed=seed, included_game_pks=included_game_pks
        )
        # Marginals from today's projection, dependence from the
        # simulation. The engine reproduces the projection's LEVEL well
        # but compresses its SPREAD badly for hitters (measured slope
        # 0.37), which read a batch of good lineups ~7 points light and
        # flattened corr(projected points, top-1%) to +0.005. Scaling
        # each player's own trials is correlation-preserving, so this
        # keeps every reason to run this engine and fixes only the part
        # it gets wrong -- see recenter_trials_on_projections().
        player_trials = atbat_sim.recenter_trials_on_projections(player_trials, slate)
    except atbat_sim.SlateNotSimulatableError as exc:
        # A plain Exception, not a ContestError, since atbat_sim.py has no
        # dependency on contest.py -- re-raised as one here so the router's
        # existing `except ContestError` catch turns this into a clean 400
        # instead of an uncaught 500.
        raise ContestError(str(exc)) from exc
    flattened = [players_in_slot_order(lineup) for lineup in lineups]

    missing = sorted({p["id"] for players in flattened for p in players if p["id"] not in player_trials})
    if missing:
        preview = missing[:5]
        suffix = "..." if len(missing) > 5 else ""
        raise ContestError(
            f"At-bat simulation has no simulated outcome for player id(s) {preview}{suffix} -- "
            "every rostered player must be part of a confirmed lineup on this slate."
        )

    # The same per-player projection-error injection the bootstrap
    # engine applies (see variance.apply_projection_error) -- the
    # at-bat engine nudges every PA by the same composite the optimizer
    # maximized, so without this it too validates its own inputs.
    unique_ids = sorted({p["id"] for players in flattened for p in players})
    id_index = {pid: i for i, pid in enumerate(unique_ids)}
    outcome_matrix = np.array([player_trials[pid] for pid in unique_ids], dtype=float)
    outcome_matrix = variance.apply_projection_error(
        outcome_matrix, np.random.default_rng(seed)
    )

    sim = np.zeros((len(lineups), num_trials))
    for i, players in enumerate(flattened):
        for p in players:
            sim[i] += outcome_matrix[id_index[p["id"]]]
    return sim


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
    included_game_pks: list[int] | None = None,
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

    `engine="bootstrap"` (default) samples each player's own historical
    outcome pool (variance.py). `engine="atbat"` instead runs genuine
    at-bat-level game simulations for the whole slate (atbat_sim.py) and
    sums each lineup's own players' simulated results -- requires
    `slate` (the full mlb_slate.build_slate() output, not just player
    ids) since that engine needs real lineup/pitcher/game structure, and
    every game on the slate must have a confirmed lineup on both sides
    and a resolvable probable pitcher (see atbat_sim.SlateNotSimulatableError).
    """
    if not entries:
        raise ContestError("Need at least one entry to simulate.")
    if not field:
        raise ContestError("Need at least one field lineup to simulate against.")
    if engine == "atbat" and slate is None:
        raise ContestError("engine='atbat' requires the full slate.")

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
    # The exact same formula percentile_rank below uses to project a
    # count of field lineups beaten (0..sample_size) onto a real rank --
    # smoothing boundaries built from any OTHER partition scheme
    # wouldn't align with the ranks this function actually produces,
    # silently reintroducing a smaller version of the point-lookup bias
    # _block_average_payouts exists to fix.
    beaten_range = np.arange(sample_size + 1)
    ranks_for_beaten = np.clip(
        np.round((1 - beaten_range / sample_size) * field_size), 1, field_size
    ).astype(np.int64)
    smoothing_boundaries = np.unique(ranks_for_beaten)
    smoothed_payouts = _block_average_payouts(full_payouts, smoothing_boundaries, field_size)
    if engine == "atbat":
        sim = await _simulate_lineups_atbat(
            entries + field, slate, season, num_trials=num_trials, seed=seed,
            included_game_pks=included_game_pks,
        )
    else:
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
    # smoothed_payouts already reads 0 for a rank whose whole block sits
    # below the paid cutoff, and a genuine partial-credit blend for a
    # block straddling it -- no separate in_the_money gate needed here,
    # unlike the naive per-rank lookup this replaced.
    payout_per_trial = smoothed_payouts[final_rank - 1]

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

    # Field-duplication model: a chalky build in a big contest is
    # duplicated by the FIELD, not just within your own batch, and DK
    # splits a rank's payout across every identical entry. The dupes
    # estimate counts exact matches in the sampled field (the field's
    # real joint structure) and floors it with the independence product
    # implied by the lineup's own cumulative-ownership duplication_risk
    # -- the count catches heavy chalk the product understates by an
    # order of magnitude, and the product covers builds too rare to
    # show up in a few-thousand-lineup sample. Not modeling this at all
    # inflated ROI most for exactly the lineups the generator likes
    # (high projection, high ownership).
    field_signatures: dict[frozenset, int] = {}
    for lu in field:
        sig = lu.get("player_ids") or frozenset(p["id"] for p in lu["players"])
        field_signatures[sig] = field_signatures.get(sig, 0) + 1

    expected_field_dupes: list[float] = []
    for entry in entries:
        sig = entry.get("player_ids") or frozenset(p["id"] for p in entry["players"])
        sampled_rate = field_signatures.get(sig, 0) / sample_size if sample_size else 0.0
        risk = entry.get("duplication_risk")
        independent_rate = math.exp(risk) if risk is not None else 0.0
        expected_field_dupes.append(max(sampled_rate, independent_rate) * field_size)

    results = []
    for i in range(num_entries):
        row = entry_sim[i]
        # Every payout is shared with the field's expected copies of
        # this exact lineup -- they score identically by definition.
        payout_row = payout_per_trial[i] / (1.0 + expected_field_dupes[i])
        expected_payout = float(payout_row.mean())
        results.append(
            {
                "lineup_index": i,
                "expected_field_dupes": round(expected_field_dupes[i], 2),
                "cash_probability_pct": round(float(in_the_money[i].mean()) * 100, 1),
                "first_place_pct": round(float(first_place[i].mean()) * 100, 2),
                "top_1pct_pct": round(float(top_1pct[i].mean()) * 100, 2),
                "top_10pct_pct": round(float(top_10pct[i].mean()) * 100, 2),
                "expected_payout": round(expected_payout, 2),
                "payout_p10": round(float(np.percentile(payout_row, 10)), 2),
                "payout_p90": round(float(np.percentile(payout_row, 90)), 2),
                "roi_pct": round((expected_payout - entry_fee) / entry_fee * 100, 1) if entry_fee else 0.0,
                # Monte Carlo standard error of roi_pct, in the same
                # percentage-point units. Top-heavy GPP payouts are
                # dominated by rare first-place hits -- a play worth
                # ~0.02% P(1st) lands 2 hits in 10,000 trials, and ONE
                # extra hit swings ROI by 100+ points -- so an ROI whose
                # SE rivals its value is noise, not signal, and the UI
                # should say so rather than rank by it.
                "roi_se_pct": round(
                    float(payout_row.std()) / (num_trials ** 0.5) / entry_fee * 100, 1
                ) if entry_fee else 0.0,
                "simulated_points_mean": round(float(row.mean()), 2),
                "simulated_points_p10": round(float(np.percentile(row, 10)), 2),
                "simulated_points_p90": round(float(np.percentile(row, 90)), 2),
                "simulated_points_floor": round(float(np.percentile(row, FLOOR_CEILING_PERCENTILE)), 2),
                "simulated_points_ceiling": round(float(np.percentile(row, 100 - FLOOR_CEILING_PERCENTILE)), 2),
            }
        )

    _split_duplicate_payouts(
        entries,
        results,
        [
            "cash_probability_pct", "first_place_pct", "top_1pct_pct", "top_10pct_pct",
            "expected_payout", "payout_p10", "payout_p90", "roi_pct", "roi_se_pct",
        ],
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
    projection_source: str = "rotowire",
    field_size: int | None = None,
    sample_size: int | None = None,
    included_game_pks: list[int] | None = None,
    min_salary: int = 0,
    max_salary: int = SALARY_CAP,
    seed: int | None = None,
    field_sharpness: str = "marquee",
) -> dict[str, Any]:
    """
    Top-level entry point: build a synthetic field for `contest_type`
    (optionally overriding its preset field_size) and rank
    `user_lineups` against it. `included_game_pks` should be whatever
    slate-game selection was used to build `user_lineups` in the first
    place -- otherwise the field could be drawn from games that aren't
    even part of the slate being entered. `field_sharpness`: see
    FIELD_SHARPNESS_LEVELS.
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
    field = generate_field(
        slate, size, projection_source=projection_source,
        included_game_pks=included_game_pks, min_salary=min_salary, max_salary=max_salary,
        seed=seed, field_sharpness=field_sharpness,
    )
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
        "field_sharpness": field_sharpness,
        "note": (
            f"Field lineups are sampled by {projection_source} ownership%, not "
            "simulated outcomes -- ranks and payouts are projected-points "
            "estimates, not win probabilities."
        ),
    }


def _build_contest_and_entries(
    slate: dict[str, Any],
    contest_type: str,
    num_lineups: int,
    *,
    projection_source: str = "rotowire",
    max_exposure_pct: float | None,
    field_size: int | None,
    included_game_pks: list[int] | None,
    min_salary: int = 0,
    max_salary: int = SALARY_CAP,
    allow_duplicates: bool = False,
    max_duplication_risk: float | None = None,
    seed: int | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """
    Shared setup for build_contest_entries/build_contest_entries_simulated
    (in both their vs-a-separate-field and self-play modes): validate the
    contest type/size and build the user's own entries. Split out from
    _build_entries_and_field() so self-play mode can skip sampling an
    opponent field entirely -- it doesn't need one.
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
        projection_source=projection_source,
        max_exposure_pct=max_exposure_pct,
        included_game_pks=included_game_pks,
        min_salary=min_salary,
        max_salary=max_salary,
        allow_duplicates=allow_duplicates,
        max_duplication_risk=max_duplication_risk,
        seed=seed,
    )
    return contest, entries


def _build_entries_and_field(
    slate: dict[str, Any],
    contest_type: str,
    num_lineups: int,
    *,
    projection_source: str = "rotowire",
    max_exposure_pct: float | None,
    field_size: int | None,
    sample_size: int | None,
    included_game_pks: list[int] | None,
    min_salary: int = 0,
    max_salary: int = SALARY_CAP,
    allow_duplicates: bool = False,
    max_duplication_risk: float | None = None,
    seed: int | None,
    field_sharpness: str = "marquee",
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Shared setup for build_contest_entries and
    build_contest_entries_simulated's default (vs-a-separate-field) mode:
    validate, build the user's own entries, and sample an opponent field
    to rank them against. `seed`, if given, offsets the opponent field's
    own seed by one so the two random walks aren't identical.
    `field_sharpness`: see FIELD_SHARPNESS_LEVELS. `max_duplication_risk`
    only applies to the user's own entries -- never passed to
    generate_field() below, which should keep modelling real chalk
    clustering honestly (see generate_entries()'s own docstring).
    """
    contest, entries = _build_contest_and_entries(
        slate,
        contest_type,
        num_lineups,
        projection_source=projection_source,
        max_exposure_pct=max_exposure_pct,
        field_size=field_size,
        included_game_pks=included_game_pks,
        min_salary=min_salary,
        max_salary=max_salary,
        allow_duplicates=allow_duplicates,
        max_duplication_risk=max_duplication_risk,
        seed=seed,
    )

    field_sample = sample_size or min(contest["field_size"], MAX_SAMPLE_SIZE)
    field = generate_field(
        slate,
        field_sample,
        projection_source=projection_source,
        included_game_pks=included_game_pks,
        min_salary=min_salary,
        max_salary=max_salary,
        seed=(seed + 1) if seed is not None else None,
        field_sharpness=field_sharpness,
    )
    return contest, entries, field


def build_contest_lineups(
    slate: dict[str, Any],
    contest_type: str,
    contest_size: int,
    *,
    projection_source: str = "rotowire",
    included_game_pks: list[int] | None = None,
    seed: int | None = None,
    injected_entries: list[dict[str, Any]] | None = None,
    field_sharpness: str = "marquee",
) -> dict[str, Any]:
    """
    Build a whole CONTEST -- and nothing else.

    This is the generator half of the generator/simulator split: it
    produces lineups and describes what it produced (salary, projected
    points, ownership, stack shapes, exposure), with no opponent field,
    no payout curve and no ROI. Those are the simulator's job, run
    afterwards on a batch that already exists (`simulate_contest_batch`),
    so the two questions -- "what does this contest look like?" and "how
    would it pay?" -- stop being answered by one indivisible call.

    `contest_size` is a single number doing the job the old
    num_lineups/field_size pair used to split between them: it IS the
    contest's field size, and it IS how many lineups get built. They
    were never meaningfully independent once the generator's job became
    building the contest rather than building a handful of entries to
    drop into someone else's.

    The one place the two numbers separate is at the very top end.
    Building is capped at MAX_USER_LINEUPS, so a contest larger than
    that gets a MAX_USER_LINEUPS-lineup build standing in for the full
    field, with every economics number downstream still keyed off the
    real `contest_size` -- the same "build a sample, project it onto the
    real size" approach this module already uses everywhere. The
    response says so explicitly (`num_entries_built` vs `field_size`)
    rather than quietly pretending the whole field was enumerated.

    No salary floor and no exposure cap: a floor makes whole stack
    shapes infeasible and stalls a batch, and the honest way to spend
    the cap is to steer the sampler toward it while it builds (see
    _SALARY_PACING_STRENGTH), not to reject what it already produced.
    Duplicates are always allowed, because a real contest field
    genuinely contains them -- especially on a short slate, where the
    distinct-lineup space runs out well before the entry count does.

    `injected_entries` are lineups you built yourself -- from the
    optimizer, a filled DK entries file, or by hand (see
    services/lineup_intake.py) -- placed at the FRONT of the batch, with
    generated lineups filling the rest of the field behind them. This is
    the difference between steering the sampler toward the process rules
    and simply obeying them: the optimizer can be TOLD a stack shape, an
    ownership bound and a salary floor, where the generator can only be
    weighted and hoped at. Every entry carries a `source`, so the
    simulator, the build audit and the daily brief can all tell the
    lineups you are entering from the field you are entering against --
    a distinction this pipeline previously had no way to express.

    Front placement is deliberate and load-bearing: the simulator
    samples a batch bigger than MAX_SAMPLE_SIZE by taking a leading
    slice, so anything at the front is guaranteed to be simulated.

    `field_sharpness` steers how the OPPONENTS are built -- and it
    belongs here, on the generator, because the generator is the thing
    that builds them. The simulator's job is to simulate the pool it is
    given, not to invent a second one.

    This is also why the generated lineups come from `generate_field()`
    rather than `generate_entries()`. The two are different models:
    generate_entries weights by projected points (`_fpts_weight`) and
    exists to build lineups YOU would enter; generate_field weights by
    ownership (`_ownership_weight`) and exists to build the lineups the
    public actually enters. This function claims to build a contest, so
    it has to use the second. Measured on a real slate, the difference
    is not cosmetic: the old construction averaged 106.6% cumulative
    ownership against 133.3% for a marquee field -- a field roughly 27
    points less chalky than any real one, which made every self-play
    contest harder to beat than reality and quietly understated what a
    genuinely differentiated portfolio is worth.
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

    injected = list(injected_entries or [])
    if len(injected) > num_lineups:
        raise ContestError(
            f"{len(injected)} lineups were supplied but this contest only builds "
            f"{num_lineups}. Raise the contest size or supply fewer."
        )

    generated = (
        generate_field(
            slate,
            num_lineups - len(injected),
            projection_source=projection_source,
            included_game_pks=included_game_pks,
            min_salary=0,
            max_salary=SALARY_CAP,
            seed=seed,
            field_sharpness=field_sharpness,
        )
        if num_lineups - len(injected) > 0
        else []
    )
    for e in generated:
        e.setdefault("source", "generated")
    entries = injected + generated

    salaries = sorted(e["salary_used"] for e in entries)
    points = [e["projected_points"] for e in entries]
    shape_counts = Counter(e["stack_type"] or "none" for e in entries)
    return {
        "contest_type": contest_type,
        "contest": contest,
        "field_size": contest_size,
        "num_entries_requested": num_lineups,
        "num_entries_built": len(entries),
        "num_injected": len(injected),
        # What the opponents were built to look like. Recorded on the
        # batch so the simulator and the UI report the field that
        # actually exists rather than a default they assume.
        "field_sharpness": field_sharpness,
        # Which lineups are YOURS rather than field, most common source
        # first. A batch with nothing injected is all "generated".
        "sources": [
            {"source": src, "count": n}
            for src, n in Counter(e.get("source") or "generated" for e in entries).most_common()
        ],
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
            "avg_duplication_risk": round(
                sum(e["duplication_risk"] for e in entries) / len(entries), 3
            ),
        },
        # Which stack shapes the contest actually came out with, most
        # common first -- the real check on "does this look like a
        # contest" that no single average can answer.
        "stack_shapes": [
            {"shape": shape, "count": n, "pct": round(100 * n / len(entries), 1)}
            for shape, n in shape_counts.most_common()
        ],
        "exposure": field_exposure(entries, top_n=20),
        "entries": entries,
        "note": (
            (
                f"{len(injected)} lineup(s) you built yourself lead the batch; the other "
                f"{len(generated)} are the field they will be simulated against. "
            )
            if injected
            else ""
        )
        + (
            f"Opponents are built as a '{field_sharpness}' public field -- weighted toward "
            "what ownership says the public actually rosters, spending the salary cap, and "
            "deliberately allowing the duplicates a real contest field contains. No "
            "simulation has been run yet -- send the batch to the simulator for cash "
            "probability, payouts and ROI."
        ),
    }


def build_contest_entries(
    slate: dict[str, Any],
    contest_type: str,
    num_lineups: int,
    *,
    projection_source: str = "rotowire",
    max_exposure_pct: float | None = None,
    field_size: int | None = None,
    sample_size: int | None = None,
    included_game_pks: list[int] | None = None,
    min_salary: int = 0,
    max_salary: int = SALARY_CAP,
    allow_duplicates: bool = False,
    max_duplication_risk: float | None = None,
    seed: int | None = None,
    field_sharpness: str = "marquee",
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
    real Monte Carlo alternative. `field_sharpness`: see
    FIELD_SHARPNESS_LEVELS. `max_duplication_risk`: see
    generate_entries()'s own docstring.
    """
    contest, entries, field = _build_entries_and_field(
        slate,
        contest_type,
        num_lineups,
        projection_source=projection_source,
        max_exposure_pct=max_exposure_pct,
        field_size=field_size,
        sample_size=sample_size,
        included_game_pks=included_game_pks,
        min_salary=min_salary,
        max_salary=max_salary,
        allow_duplicates=allow_duplicates,
        max_duplication_risk=max_duplication_risk,
        seed=seed,
        field_sharpness=field_sharpness,
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
        # How many of those are structurally unique builds -- the rest
        # are deliberate duplicates, the way a real small-slate contest
        # field duplicates once the distinct lineup space runs out.
        "num_distinct_entries": len({frozenset(p["id"] for p in e["players"]) for e in entries}),
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
            "avg_duplication_risk": round(
                sum(e["duplication_risk"] for e in entries) / len(entries), 3
            ),
        },
        "field_baseline": _field_baseline(
            contest["payout_pct"], evaluation["prize_pool"], contest["entry_fee"], evaluation["field_size"]
        ),
        "exposure": field_exposure(entries, top_n=20),
        # The sampled opponent field this batch was ranked against --
        # carried out so a later late swap can re-rank against the SAME
        # field (swapping it too) rather than resampling a different
        # one, which would make the before/after comparison meaningless.
        "field": field,
        "field_sharpness": field_sharpness,
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
    projection_source: str = "rotowire",
    num_trials: int = 2000,
    max_exposure_pct: float | None = None,
    field_size: int | None = None,
    sample_size: int | None = None,
    included_game_pks: list[int] | None = None,
    min_salary: int = 0,
    max_salary: int = SALARY_CAP,
    allow_duplicates: bool = False,
    max_duplication_risk: float | None = None,
    seed: int | None = None,
    self_play: bool = False,
    engine: str = "bootstrap",
    field_sharpness: str = "marquee",
    first_place_pct: float | None = None,
) -> dict[str, Any]:
    """
    Like build_contest_entries, but ranks the batch against a real Monte
    Carlo simulation instead of a single projected-points snapshot
    against the field. A separate function rather than a flag on
    build_contest_entries -- simulation is real additional compute
    (fetching every player's own outcome pool, then running num_trials
    simulated realities) on top of an already-large mass-generation
    call, and needs `season` to know which year's game logs to pull.

    Two genuinely different questions, both real and useful, picked via
    `self_play`:

      - `self_play=False` (default): "how do MY entries fare against a
        REALISTIC public field?" -- ranks the batch against a separate,
        ownership-weighted sample standing in for the OTHER real
        entries in the contest (`evaluate_batch_simulated`). Right when
        your own batch is a modest slice of a much bigger real contest
        (e.g. 300 entries in a 5,000-entry GPP) -- you're one voice in
        a field mostly made of other real people's builds.

      - `self_play=True`: "how does MY OWN BATCH perform against
        ITSELF?" -- every lineup ranked against every other lineup in
        the SAME batch, in the SAME simulated trial, with no separate
        field sampled at all (`evaluate_field_mirrored`, the same
        self-play mechanic already built for "My DK entries" mirroring
        a real contest's whole field). Answers a different, real
        question: which of YOUR OWN builds/stacks are relatively
        strongest, and how would the leaderboard look if the whole
        field played like variations of your own strategy -- useful
        for portfolio construction and exposure decisions, not a
        substitute for the realistic-public-field default.

    `engine="bootstrap"` (default) or `"atbat"` -- see
    evaluate_batch_simulated()'s own docstring for what each means;
    `"atbat"` requires every game on `slate` to have a confirmed lineup
    on both sides and a resolvable probable pitcher.

    `field_sharpness` (see generate_field()'s own docstring) only
    matters for the `self_play=False` default -- self_play=True never
    samples a separate opponent field, it mirrors your own batch
    against itself.

    `first_place_pct`, if given, overrides `contest_type`'s own preset
    percent-to-first (what share of the prize pool 1st place wins) for
    this run only -- same override mechanism `evaluate_batch_simulated`/
    `evaluate_field_mirrored`/`build_dk_entries_simulated` already
    accept. A lower percent-to-first flattens the payout curve (more
    spread across the paid ranks, less concentrated at 1st), which
    changes every entry's simulated ROI -- letting a user see how
    sensitive their batch's ROI actually is to the real payout
    structure, rather than only the preset's own baked-in assumption.
    """
    if self_play:
        contest, entries = _build_contest_and_entries(
            slate,
            contest_type,
            num_lineups,
            projection_source=projection_source,
            max_exposure_pct=max_exposure_pct,
            field_size=field_size,
            included_game_pks=included_game_pks,
            min_salary=min_salary,
            max_salary=max_salary,
            allow_duplicates=allow_duplicates,
            max_duplication_risk=max_duplication_risk,
            seed=seed,
        )
        evaluation = await evaluate_field_mirrored(
            entries, contest, season=season, num_trials=num_trials, seed=seed,
            first_place_pct=first_place_pct,
            engine=engine, slate=slate, included_game_pks=included_game_pks,
        )
        # Self-play ranks the batch against ITSELF, so there's no
        # separate opponent field to carry forward.
        field = []
    else:
        contest, entries, field = _build_entries_and_field(
            slate,
            contest_type,
            num_lineups,
            projection_source=projection_source,
            max_exposure_pct=max_exposure_pct,
            field_size=field_size,
            sample_size=sample_size,
            included_game_pks=included_game_pks,
            min_salary=min_salary,
            max_salary=max_salary,
            allow_duplicates=allow_duplicates,
            max_duplication_risk=max_duplication_risk,
            seed=seed,
            field_sharpness=field_sharpness,
        )
        evaluation = await evaluate_batch_simulated(
            entries,
            field,
            contest,
            season=season,
            num_trials=num_trials,
            seed=(seed + 2) if seed is not None else None,
            first_place_pct=first_place_pct,
            engine=engine,
            slate=slate,
            included_game_pks=included_game_pks,
        )

    return _rank_and_summarize_simulated(
        entries,
        evaluation,
        contest,
        field,
        contest_type=contest_type,
        num_requested=num_lineups,
        self_play=self_play,
        engine=engine,
        field_sharpness=field_sharpness,
        first_place_pct=first_place_pct,
    )


def _source_breakout(
    entries: list[dict[str, Any]],
    results: list[dict[str, Any]],
    contest: dict[str, Any],
) -> dict[str, Any] | None:
    """
    Split a simulated batch into the lineups you are entering and the
    field you are entering against, and summarize each.

    Only meaningful once something has been injected -- a pure generated
    contest is all field by definition, and reporting a "yours" section
    covering the whole batch would be a lie of framing. Returns None in
    that case rather than a section that says nothing.

    `entry_cost` counts only YOUR entries: the field lineups are
    opponents, not entries you paid for, so including them would
    overstate what the portfolio costs by three orders of magnitude.
    """
    mine = [
        (e, r) for e, r in zip(entries, results) if (e.get("source") or "generated") != "generated"
    ]
    if not mine:
        return None
    field = [
        (e, r) for e, r in zip(entries, results) if (e.get("source") or "generated") == "generated"
    ]

    def roll(rows: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any] | None:
        if not rows:
            return None
        n = len(rows)
        return {
            "count": n,
            "avg_cash_probability_pct": round(sum(r["cash_probability_pct"] for _, r in rows) / n, 1),
            "avg_first_place_pct": round(sum(r["first_place_pct"] for _, r in rows) / n, 2),
            "avg_top_1pct_pct": round(sum(r["top_1pct_pct"] for _, r in rows) / n, 2),
            "avg_top_10pct_pct": round(sum(r["top_10pct_pct"] for _, r in rows) / n, 2),
            "avg_roi_pct": round(sum(r["roi_pct"] for _, r in rows) / n, 1),
            "avg_projected_points": round(sum(e["projected_points"] for e, _ in rows) / n, 2),
            "avg_total_ownership_pct": round(sum(e["total_ownership_pct"] for e, _ in rows) / n, 1),
        }

    yours = roll(mine)
    expected = round(sum(r["expected_payout"] for _, r in mine), 2)
    cost = round(len(mine) * contest["entry_fee"], 2)
    return {
        **yours,
        "sources": [
            {"source": src, "count": n}
            for src, n in Counter(e.get("source") for e, _ in mine).most_common()
        ],
        "total_entry_cost": cost,
        "total_expected_payout": expected,
        "estimated_net_profit": round(expected - cost, 2),
        "field": roll(field),
        "lineup_indexes": [r["lineup_index"] for _, r in mine],
        # Said out loud because the number is otherwise very easy to
        # misread. Optimizer-built lineups maximize PROJECTED points,
        # and variance.py recenters every player's outcome distribution
        # on that same projection -- so a lineup selected for the
        # highest projection is being graded by a simulator that treats
        # the projection as truth. Measured on a real slate: 20
        # optimizer lineups came out at +175% simulated ROI against a
        # field at -12.7% (which is just the rake). The gap is real
        # ONLY to the extent the projections are; it is not a measured
        # edge, and it is not comparable to a backtest against actual
        # results.
        "caveat": (
            "Your lineups are scored by a simulator whose player distributions are centered "
            "on the same projections they were built to maximize, so their simulated ROI is "
            "optimistic by construction. Read it as a structural comparison against the "
            "field -- ownership, stack shape, exposure -- not as expected profit."
        )
        if any((e.get("source") or "") == "optimizer" for e, _ in mine)
        else None,
    }


def _rank_and_summarize_simulated(
    entries: list[dict[str, Any]],
    evaluation: dict[str, Any],
    contest: dict[str, Any],
    field: list[dict[str, Any]],
    *,
    contest_type: str,
    num_requested: int,
    self_play: bool,
    engine: str,
    field_sharpness: str,
    first_place_pct: float | None,
) -> dict[str, Any]:
    """
    Shared tail of every simulated-contest path: sort the batch by how
    well it actually simulated, then roll the per-lineup results up into
    one batch summary.

    Split out so build_contest_entries_simulated() (build and simulate in
    one call) and simulate_contest_batch() (simulate a contest that was
    already built, the generator/simulator hand-off) produce byte-for-byte
    the same response shape rather than two summaries that drift apart.
    """
    # Best entries first -- ranked by top-1% rate with ROI as the
    # tiebreak, NOT by raw ROI. In a top-heavy GPP, per-lineup ROI is
    # dominated by rare first-place hits (a play worth ~0.02% P(1st)
    # lands 2 hits in 10,000 trials, and one extra hit swings ROI by
    # 100+ points), so sorting by ROI ranks lineups substantially by
    # which ones got lucky in THIS run's draws. top_1pct_pct measures
    # the same "can this build spike?" quality from ~100x more trial
    # hits, so it's far more stable draw to draw. Each row still
    # carries its roi_pct (and its roi_se_pct, so the noise is visible
    # rather than hidden). entries and results are re-ordered together
    # (same permutation) so every downstream consumer -- the JSON
    # response, the sample-entries preview, and the cached batch behind
    # the CSV download -- gets the sorted order for free.
    order = sorted(
        range(len(entries)),
        key=lambda i: (
            -evaluation["results"][i].get("top_1pct_pct", 0),
            -evaluation["results"][i]["roi_pct"],
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
        # How many of those are structurally unique builds -- the rest
        # are deliberate duplicates, the way a real small-slate contest
        # field duplicates once the distinct lineup space runs out.
        "num_distinct_entries": len({frozenset(p["id"] for p in e["players"]) for e in entries}),
        "field_sharpness": field_sharpness,
        "field_size": evaluation["field_size"],
        "sample_size": evaluation["sample_size"],
        "paid_count": evaluation["paid_count"],
        "prize_pool": evaluation["prize_pool"],
        "num_trials": evaluation["num_trials"],
        # The percent-to-first actually used this run -- either the
        # override given, or contest_type's own preset value when none
        # was given, so the frontend can show what was really applied
        # rather than echoing back a possibly-null override.
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
            "avg_duplication_risk": round(
                sum(e["duplication_risk"] for e in entries) / len(entries), 3
            ),
        },
        # When the batch contains lineups you built yourself (see
        # lineup_intake.py), the batch-wide averages above answer the
        # wrong question -- they are dominated by the thousands of field
        # lineups you are not entering. This is the same roll-up over
        # only YOUR entries, plus the field's, so the comparison that
        # actually matters is stated rather than left to be eyeballed.
        # None when nothing was injected, since then the batch summary
        # already is the answer.
        "your_entries": _source_breakout(entries, evaluation["results"], contest),
        "field_baseline": _field_baseline(
            contest["payout_pct"], evaluation["prize_pool"], contest["entry_fee"], evaluation["field_size"]
        ),
        "exposure": field_exposure(entries, top_n=20, results=evaluation["results"]),
        "entries": entries,
        # The simulated opponent field this batch was ranked against.
        # Carried out so a later late swap can re-rank against the SAME
        # field (swapping it too, since the real field late-swaps as
        # well) instead of resampling a different one, which would make
        # the before/after comparison meaningless. Empty under
        # self_play, where the batch is ranked against itself.
        "field": field,
        "results": evaluation["results"],
        "self_play": self_play,
        "engine": engine,
        "note": (
            (
                f"Cash probability and expected payout come from {evaluation['num_trials']:,} "
                "real Monte Carlo simulated trials of this ENTIRE BATCH ranked against ITSELF "
                "-- every lineup competing against every other lineup you generated, in the "
                "same simulated trial, projected onto the real field_size. Lineups that share "
                "correlated players/stacks will naturally cluster together at the top or "
                "bottom when those players run hot or cold in a given trial, same as a real "
                "correlated public field would."
                if self_play
                else
                f"Cash probability and expected payout come from {evaluation['num_trials']:,} "
                "real Monte Carlo simulated trials of each player's own historical outcome "
                "pool, with team correlation for hitters, ranked against a separately-sampled "
                "realistic public field -- a genuine probability, not a single projected-points "
                "estimate against the field."
            )
            + (
                " Simulated at the AT-BAT level -- every trial is a genuine plate-appearance-by-"
                "plate-appearance simulated game for the whole slate, so correlation (including "
                "starter-vs-lineup matchups) is a natural consequence of shared game state rather "
                "than a separately-modeled multiplier."
                if engine == "atbat"
                else ""
            )
        ),
    }



async def simulate_contest_batch(
    entries: list[dict[str, Any]],
    contest: dict[str, Any],
    *,
    season: int,
    contest_type: str = "",
    slate: dict[str, Any] | None = None,
    num_trials: int = 10_000,
    entry_fee: float | None = None,
    first_place_pct: float | None = None,
    engine: str = "bootstrap",
    self_play: bool = True,
    field: list[dict[str, Any]] | None = None,
    field_sharpness: str = "marquee",
    projection_source: str = "rotowire",
    included_game_pks: list[int] | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    """
    Simulate a contest that has ALREADY been built -- the simulator half
    of the generator/simulator split.

    `entry_fee`, if given, replaces the contest preset's own. It's the
    number that sets the prize pool (field_size x entry_fee, less rake),
    so it's what actually determines every payout and therefore every
    ROI in the result -- which is why it's a real input here rather than
    a fixed property of the preset.

    `self_play=True` (the default) ranks the batch against ITSELF: the
    generator now builds the whole contest, so the batch IS the field
    and there's no second population to invent. `self_play=False` falls
    back to the older model -- rank the batch against a separately
    sampled, ownership-weighted public field -- which is still the
    honest comparison if what you want to know is how these lineups
    would do against real public rosters rather than against each other.
    That mode needs `slate` to sample the field from.

    A batch bigger than MAX_SAMPLE_SIZE is simulated as a
    MAX_SAMPLE_SIZE-lineup slice of itself, projected back onto the real
    field size. Generated entries come out in build order with no
    ranking applied, so a leading slice of them is an unbiased sample of
    the batch rather than its best or worst end.

    Lineups you injected yourself sit at the FRONT of the batch
    (build_contest_lineups), which means the slice always contains all
    of them -- deliberate, since simulating a contest while leaving out
    the entries you are actually going to play would be useless. The
    field portion of the slice stays an unbiased sample of the
    generated remainder.
    """
    if not entries:
        raise ContestError("Nothing to simulate -- build a contest first.")

    contest = dict(contest)
    if entry_fee is not None:
        if entry_fee < 0:
            raise ContestError("entry_fee can't be negative.")
        contest["entry_fee"] = float(entry_fee)
        # Any prize pool carried on the contest was derived from its own
        # entry fee, so a new fee invalidates it -- drop it and let the
        # evaluators recompute field_size x fee less rake.
        contest.pop("prize_pool", None)

    num_requested = len(entries)
    simulated = entries[:MAX_SAMPLE_SIZE]

    if self_play:
        evaluation = await evaluate_field_mirrored(
            simulated, contest, season=season, num_trials=num_trials, seed=seed,
            first_place_pct=first_place_pct, engine=engine, slate=slate,
            included_game_pks=included_game_pks,
        )
        field = []
    else:
        if not field:
            if slate is None:
                raise ContestError(
                    "Ranking against a public field needs the slate to sample one from."
                )
            field = generate_field(
                slate,
                min(contest["field_size"], MAX_SAMPLE_SIZE),
                projection_source=projection_source,
                included_game_pks=included_game_pks,
                seed=(seed + 1) if seed is not None else None,
                field_sharpness=field_sharpness,
            )
        evaluation = await evaluate_batch_simulated(
            simulated, field, contest, season=season, num_trials=num_trials,
            seed=(seed + 2) if seed is not None else None,
            first_place_pct=first_place_pct, engine=engine, slate=slate,
            included_game_pks=included_game_pks,
        )

    result = _rank_and_summarize_simulated(
        simulated,
        evaluation,
        contest,
        field,
        contest_type=contest_type,
        num_requested=num_requested,
        self_play=self_play,
        engine=engine,
        field_sharpness=field_sharpness,
        first_place_pct=first_place_pct,
    )
    # How much of the built batch actually got simulated -- equal to
    # num_entries_built for every contest at or under MAX_SAMPLE_SIZE,
    # and honestly smaller above it.
    result["num_entries_simulated"] = len(simulated)
    return result


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
    included_game_pks: list[int] | None = None,
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
    if engine == "atbat" and slate is None:
        raise ContestError("engine='atbat' requires the full slate.")

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

    if engine == "atbat":
        sim = await _simulate_lineups_atbat(
            field_lineups, slate, season, num_trials=num_trials, seed=seed,
            included_game_pks=included_game_pks,
        )
    else:
        player_pools = await variance.player_pools_for_entries(field_lineups, season)
        sim = variance.simulate_batch(field_lineups, player_pools, num_trials=num_trials, seed=seed)

    order = np.argsort(-sim, axis=0)  # best-to-worst lineup index per trial
    final_rank = np.empty_like(order)
    np.put_along_axis(final_rank, order, real_ranks_by_k[:, None], axis=0)

    top_1pct_threshold = max(1, round(0.01 * field_size))
    top_10pct_threshold = max(1, round(0.10 * field_size))
    in_the_money = final_rank <= paid_count
    first_place = final_rank == 1
    top_1pct = final_rank <= top_1pct_threshold
    top_10pct = final_rank <= top_10pct_threshold
    # smoothed_payouts already reads 0 for a rank whose whole block sits
    # below the paid cutoff, and a genuine partial-credit blend for a
    # block straddling it -- no separate in_the_money gate needed here,
    # unlike the naive per-rank lookup this replaced.
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
                # Monte Carlo standard error of roi_pct, in the same
                # percentage-point units. Top-heavy GPP payouts are
                # dominated by rare first-place hits -- a play worth
                # ~0.02% P(1st) lands 2 hits in 10,000 trials, and ONE
                # extra hit swings ROI by 100+ points -- so an ROI whose
                # SE rivals its value is noise, not signal, and the UI
                # should say so rather than rank by it.
                "roi_se_pct": round(
                    float(payout_row.std()) / (num_trials ** 0.5) / entry_fee * 100, 1
                ) if entry_fee else 0.0,
                "simulated_points_mean": round(float(row.mean()), 2),
                "simulated_points_p10": round(float(np.percentile(row, 10)), 2),
                "simulated_points_p90": round(float(np.percentile(row, 90)), 2),
                "simulated_points_floor": round(float(np.percentile(row, FLOOR_CEILING_PERCENTILE)), 2),
                "simulated_points_ceiling": round(float(np.percentile(row, 100 - FLOOR_CEILING_PERCENTILE)), 2),
            }
        )

    _split_duplicate_payouts(
        field_lineups,
        results,
        [
            "cash_probability_pct", "first_place_pct", "top_1pct_pct", "top_10pct_pct",
            "expected_payout", "payout_p10", "payout_p90", "roi_pct", "roi_se_pct",
        ],
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
    projection_source: str = "rotowire",
    payout_pct: float = 0.20,
    shape: str = "top_heavy",
    num_trials: int = 10_000,
    sample_size: int | None = None,
    included_game_pks: list[int] | None = None,
    min_salary: int = 0,
    max_salary: int = SALARY_CAP,
    seed: int | None = None,
    engine: str = "bootstrap",
    field_sharpness: str = "marquee",
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

    `field_sharpness` -- see generate_field()'s own docstring -- controls
    how sharp the simulated field is assumed to be.
    """
    if field_size < 1:
        raise ContestError("field_size must be at least 1.")

    field_sample = sample_size or min(field_size, MAX_SAMPLE_SIZE)
    field_lineups = generate_field(
        slate, field_sample, projection_source=projection_source,
        included_game_pks=included_game_pks, min_salary=min_salary, max_salary=max_salary,
        seed=seed, field_sharpness=field_sharpness,
    )

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
        engine=engine,
        slate=slate,
        included_game_pks=included_game_pks,
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
        # How many of those are structurally unique builds -- the rest
        # are deliberate duplicates, the way a real small-slate contest
        # field duplicates once the distinct lineup space runs out.
        "num_distinct_entries": len({frozenset(p["id"] for p in e["players"]) for e in entries}),
        "field_sharpness": field_sharpness,
        "field_size": evaluation["field_size"],
        "sample_size": evaluation["sample_size"],
        "paid_count": evaluation["paid_count"],
        "prize_pool": evaluation["prize_pool"],
        "num_trials": evaluation["num_trials"],
        "first_place_pct": first_place_pct,
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
        "field_baseline": _field_baseline(
            contest["payout_pct"], evaluation["prize_pool"], entry_fee, evaluation["field_size"]
        ),
        "exposure": field_exposure(entries, top_n=20, results=evaluation["results"]),
        "entries": entries,
        "results": evaluation["results"],
        "engine": engine,
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
