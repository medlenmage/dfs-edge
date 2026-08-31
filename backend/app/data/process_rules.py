"""
The process rules DFS Edge audits against -- one place, so the
post-contest audit (services/contest_audit.py), the pre-entry build
audit (services/build_audit.py), and the scheduled briefs
(services/briefs.py) can never disagree about what "good" means.

These came out of a review of 435 real entries across 19 contests
(2026-08-12 to 08-30). The three leaks that review found, in order of
cost, and the rule each one became:

1. Leverage at PITCHER. Sub-10%-owned arms averaged 12.6 DK pts; 25%+
   arms averaged 23.5. Top-1% lineups had a sub-10-pt pitcher 5% of
   the time; the user's did 27% (same as the field). -> Pitching is
   the predictable part of the slate: use the top 2-3 arms and take
   leverage with bats, not arms.

2. Right team, wrong hitters. Stacks were often the correct team but
   built from the 6-9 spots around one star (KC: Witt + Rave/Rojas/
   Loftin = 18 pts, while Witt + Caglianone + Massey went 63-70). ->
   Stack the 1-5 spots.

3. Sub-3%-owned filler. 28% of hitter slots were <3% owned; they
   averaged 5.2 pts and 34% scored ZERO. 15%+-owned hitters averaged
   9.7. -> Low ownership is only leverage if the player has a ceiling.

Plus a structural one: 20-entry portfolios spread over 12-15 stacks
and 11-14 different pitchers, so the best idea never carried weight.
-> 3-4 stacks at 5-7 lineups each, 2-3 pitcher combos.

Every number below is a threshold for FLAGGING, not a hard constraint
the generator enforces -- the audits report, the human decides.
"""

from __future__ import annotations

# --- Pitching -------------------------------------------------------------

# Below this projected ownership, a pitcher pick is "leverage at P" --
# the single most expensive habit the review found.
PITCHER_LEVERAGE_OWN_PCT = 10.0

# In a multi-entry portfolio, more distinct pitchers than this means
# the pitcher core isn't a core. Scaled by portfolio size in the audit
# (see max_distinct_pitchers()).
PITCHER_CORE_MAX_DISTINCT_20 = 4  # for a 20-entry contest

# A pitcher outing under this many DK points is a bust in the
# post-contest read (top-1% lineups almost never carry one).
PITCHER_BUST_FPTS = 10.0

# --- Hitting ----------------------------------------------------------------

# Batting order 1-5 is the target; 6 is tolerated; 7-9 is flagged.
HITTER_MAX_BATTING_ORDER_OK = 5
HITTER_MAX_BATTING_ORDER_TOLERATED = 6

# Below this projected ownership a hitter is "filler" unless he's in a
# stack AND batting 1-5 (then it's deliberate leverage).
FILLER_OWN_PCT = 3.0
MAX_FILLERS_PER_LINEUP = 1

# --- Stacking / conviction -------------------------------------------------

# A real GPP stack.
STACK_MIN_SIZE = 4

# Conviction: in an N-entry portfolio the most-used primary stack
# should carry at least this share of entries, and there shouldn't be
# more than N / STACK_ENTRIES_PER distinct primary stacks.
TOP_STACK_MIN_SHARE = 0.25
STACK_ENTRIES_PER = 5  # "3-4 stacks x 5-7 lineups" for 20 entries

# --- Salary -------------------------------------------------------------------

SALARY_CAP = 50_000
SALARY_UNUSED_MAX = 300  # unused money over this is flagged

# --- Ownership buckets used in the post-contest read -----------------------

OWNERSHIP_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("<3%", 0.0, 3.0),
    ("3-8%", 3.0, 8.0),
    ("8-15%", 8.0, 15.0),
    ("15%+", 15.0, 1e9),
)

PITCHER_OWNERSHIP_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("<10%", 0.0, 10.0),
    ("10-25%", 10.0, 25.0),
    ("25%+", 25.0, 1e9),
)

# --- Cash-line approximation ------------------------------------------------

# A DK standings export carries no payout table. The audit treats the
# top this-fraction of the field as "cashed" -- DK's real line in the
# GPPs the user plays sits at ~20-23%.
CASH_LINE_FRACTION = 0.20
TOP_SLICE_FRACTION = 0.01  # "top 1%" comparison group (min 20 entries)


def max_distinct_pitchers(num_entries: int) -> int:
    """How many distinct pitchers a portfolio of this size should use
    before the audit calls it unfocused: 4 for 20 entries, scaling
    with sqrt so 150 entries allows ~11, not 30."""
    if num_entries <= 1:
        return 2
    return max(3, round(PITCHER_CORE_MAX_DISTINCT_20 * (num_entries / 20) ** 0.5))


def max_distinct_primary_stacks(num_entries: int) -> int:
    """Distinct primary (largest) stacks a portfolio should carry: 4
    for 20 entries."""
    return max(2, round(num_entries / STACK_ENTRIES_PER))


# --- Prompt text ------------------------------------------------------------
# Plain-language version of the same rules, injected into the brief
# prompts so the written read argues from the same standard the audits
# score against.

RULES_TEXT = f"""PROCESS RULES (from a review of this user's own real contest history):

PITCHING -- the predictable part of the slate. Nail it first.
- Use the top 2-3 arms on the slate by K%, opponent K%, and Vegas (implied runs against, moneyline).
- Do NOT take leverage at pitcher. In this user's history, pitchers under {PITCHER_LEVERAGE_OWN_PCT:.0f}% owned averaged 12.6 DK pts vs 23.5 for pitchers over 25% owned. Chalk arms are chalk because they're the best arms.
- A 20-entry portfolio should run 2-3 pitcher combinations, not 11-14 different pitchers.

HITTING -- stack the top of the order, not around a star.
- Every hitter should bat 1-{HITTER_MAX_BATTING_ORDER_OK}. Batting {HITTER_MAX_BATTING_ORDER_TOLERATED} is tolerated inside a stack. 7-9 is a mistake unless he is a salary punt under $3,000.
- No hitter under {FILLER_OWN_PCT:.0f}% projected ownership unless he is in a stack AND batting 1-5. Sub-3% filler in this user's history averaged 5.2 pts and scored zero 34% of the time.
- Stacks are {STACK_MIN_SIZE}-5 CONSECUTIVE hitters from the 1-5 spots of a team with implied runs 5+, facing a bad starter (xFIP 4.5+ or K% under 20%).

CONVICTION -- weight the best ideas.
- 20 entries = 3-4 primary stacks at 5-7 lineups each. Not 15 stacks x1.
- Use the whole salary (within ${SALARY_UNUSED_MAX} of the ${SALARY_CAP:,} cap).

LEVERAGE -- take it with bats, never arms. The second-best stack on the slate at 4% owned beats the best stack at 35% only if its ceiling is comparable.
"""
