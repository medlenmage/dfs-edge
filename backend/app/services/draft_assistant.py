"""
Live draft assistant -- reads a Sleeper draft in progress and says who
to take next, and why.

Sleeper has no websocket or push for drafts, so "live" here means
polling `/draft/<id>/picks` (clients/sleeper.py caches it for 5s, which
is what keeps a UI on a short timer from becoming real request volume).
Every commercial draft-sync tool works exactly the same way.

The recommendation is not just "highest VORP left." That alone gives
bad advice in a real draft, because it ignores the only thing that
actually makes a pick urgent: what will still be there when you pick
again. The three signals combined here are the ones every serious
draft tool uses:

  1. VALUE     -- VORP off the season_long board.
  2. SCARCITY  -- how much worse off you are at this position if you
                  pass. Measured concretely: estimate how many players
                  at the position go before your next turn (from this
                  room's own recent picks, shrunk toward what the
                  league's roster requirements demand), look up who you
                  would actually be left with that far down the list,
                  and take the gap. Deep in a position that gap is
                  nearly nothing; at a cliff it is the whole reason to
                  reach.
  3. NEED      -- how far this league's own starting requirements are
                  from being filled on your roster.

Plus two hard flags that override a marginal pick: a tier about to run
out, and a bye-week pileup.

Read-only. This module cannot make a pick.
"""

from __future__ import annotations

import logging
from typing import Any

from app.clients import sleeper
from app.services import season_long

log = logging.getLogger(__name__)

# How much each signal moves a player's draft score. VORP is the base
# and stays at 1.0; the others are expressed as multiples of a point of
# VORP so the weights are directly interpretable -- "filling an empty
# starting slot is worth about 3 points of projected per-game value" is
# a claim you can argue with, which is the point of writing it this way.
_NEED_WEIGHT = 3.0
_SCARCITY_WEIGHT = 1.0
# A position whose starters are already filled still has bench value,
# but much less. Applied to the need term, not to VORP.
_DEPTH_DISCOUNT = 0.25

# Flag a tier as running out at or below this many players left.
_TIER_THIN = 2
# Three starters sharing a bye is the point where a week is genuinely
# hard to field; two is normal and not worth nagging about.
_BYE_PILEUP = 3

# How many suggestions to return.
_SUGGESTION_COUNT = 8


# ------------------------------------------------------------------ draft state


def _pick_number(round_no: int, slot: int, teams: int, snake: bool) -> int:
    """Overall pick number for a draft slot in a given round."""
    if snake and round_no % 2 == 0:
        slot = teams - slot + 1
    return (round_no - 1) * teams + slot


def _my_slot(draft: dict[str, Any], user_id: str | None) -> int | None:
    """Which draft slot belongs to this user."""
    if not user_id:
        return None
    order = draft.get("draft_order") or {}
    slot = order.get(user_id)
    return int(slot) if slot else None


async def draft_state(draft_id: str, user_id: str | None = None) -> dict[str, Any]:
    """
    Everything about a draft that matters for advice: who has been
    taken, whose turn it is, and when this user picks next.
    """
    draft = await sleeper.get_draft(draft_id)
    picks = await sleeper.get_draft_picks(draft_id)

    settings = draft.get("settings") or {}
    teams = int(settings.get("teams") or draft.get("total_rosters") or 12)
    rounds = int(settings.get("rounds") or 15)
    snake = (draft.get("type") or "snake") == "snake"

    made = len(picks)
    on_the_clock_pick = made + 1
    current_round = min((on_the_clock_pick - 1) // teams + 1, rounds) if teams else 1

    slot = _my_slot(draft, user_id)
    upcoming: list[int] = []
    if slot:
        for r in range(1, rounds + 1):
            n = _pick_number(r, slot, teams, snake)
            if n >= on_the_clock_pick:
                upcoming.append(n)
        upcoming = upcoming[:3]

    return {
        "draft_id": draft_id,
        "status": draft.get("status"),
        "type": draft.get("type"),
        "teams": teams,
        "rounds": rounds,
        "picks_made": made,
        "current_round": current_round,
        "on_the_clock_pick": on_the_clock_pick,
        "my_slot": slot,
        "my_next_pick": upcoming[0] if upcoming else None,
        "my_following_pick": upcoming[1] if len(upcoming) > 1 else None,
        "picks_until_my_turn": (upcoming[0] - on_the_clock_pick) if upcoming else None,
        "on_the_clock_is_me": bool(upcoming and upcoming[0] == on_the_clock_pick),
        "picks": [
            {
                "pick_no": p.get("pick_no"),
                "round": p.get("round"),
                "roster_id": p.get("roster_id"),
                "picked_by": p.get("picked_by"),
                "player_id": p.get("player_id"),
                "name": (p.get("metadata") or {}).get("first_name", "")
                + " "
                + (p.get("metadata") or {}).get("last_name", ""),
                "position": season_long.canonical_position(
                    (p.get("metadata") or {}).get("position")
                ),
                "team": (p.get("metadata") or {}).get("team"),
            }
            for p in picks
        ],
    }


# ------------------------------------------------------------------ my roster


def _my_picks(state: dict[str, Any], user_id: str | None) -> list[dict[str, Any]]:
    if not user_id:
        return []
    return [p for p in state["picks"] if p.get("picked_by") == user_id]


def _roster_counts(picks: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for p in picks:
        pos = p.get("position")
        if pos:
            counts[pos] = counts.get(pos, 0) + 1
    return counts


def _need_scores(counts: dict[str, int], shape: dict[str, Any]) -> dict[str, float]:
    """
    How badly each position still needs a body, as a 0-1 fraction of
    that position's unfilled starting slots.

    Flex is folded in by treating the flex-eligible positions as sharing
    the extra slots -- a roster that has its 2 RB and 3 WR filled still
    reads a little need at RB/WR/TE while the flex is open.
    """
    need: dict[str, float] = {}
    flex_open = max(
        0,
        shape.get("flex_slots", 0)
        - max(0, sum(counts.get(p, 0) - shape["starters"].get(p, 0) for p in season_long.FLEX_POSITIONS)),
    )
    for pos in season_long.FANTASY_POSITIONS:
        required = shape["starters"].get(pos, 0)
        have = counts.get(pos, 0)
        if required and have < required:
            need[pos] = (required - have) / required
        elif flex_open and pos in season_long.FLEX_POSITIONS:
            need[pos] = 0.5
        else:
            need[pos] = 0.0
    return need


# ------------------------------------------------------------------ scarcity


def _structural_rate(shape: dict[str, Any]) -> dict[str, float]:
    """
    The share of picks each position should get if the room simply drafts
    to fill its starting lineups.

    Not a guess -- it falls straight out of the league's own settings. A
    league that starts three receivers and one tight end will spend
    roughly three times as many picks on receivers, and that is knowable
    before a single pick is made. Flex slots are split across the
    flex-eligible positions in proportion to their own starter counts.
    """
    base = {pos: float(n) for pos, n in shape["starters"].items() if n}
    flex_total = shape.get("flex_slots", 0) + shape.get("superflex_slots", 0)
    if flex_total:
        eligible = {
                p: base.get(p, 0.0)
                for p in season_long.SUPERFLEX_POSITIONS
                if base.get(p)
            }
        denom = sum(eligible.values())
        if denom:
            for pos, weight in eligible.items():
                base[pos] = base[pos] + flex_total * (weight / denom)
    total = sum(base.values())
    return {pos: n / total for pos, n in base.items()} if total else {}


def _position_run_rate(
    picks: list[dict[str, Any]], teams: int, shape: dict[str, Any]
) -> dict[str, float]:
    """
    How fast each position is coming off the board, blending what THIS
    room has actually been doing with what the league's roster
    requirements say it must eventually do.

    Runs are real -- four straight tight ends go and the position is
    suddenly gone -- and only the observed picks can show one happening.
    But the raw observed rate alone is badly behaved early: three picks
    into a draft it reads "100% running back, 0% receiver", and a 0%
    receiver rate would tell the assistant that no receiver will be
    taken before your next turn, which is nonsense.

    So the observed rate is shrunk toward the structural rate, trusting
    the room more as the evidence accumulates -- the same
    regress-toward-a-prior technique used for thin samples throughout
    this codebase. One full round of picks is the half-way point.
    """
    prior = _structural_rate(shape)
    window = picks[-(teams * 2) :] if picks else []

    observed: dict[str, float] = {}
    for p in window:
        pos = p.get("position")
        if pos:
            observed[pos] = observed.get(pos, 0) + 1

    n = len(window)
    weight = teams or 1  # one round's worth of picks to move halfway
    rate: dict[str, float] = {}
    for pos in set(prior) | set(observed):
        obs = (observed.get(pos, 0) / n) if n else 0.0
        rate[pos] = (n * obs + weight * prior.get(pos, 0.0)) / (n + weight)
    return rate


def _expected_gone(pos: str, run_rate: dict[str, float], picks_until: int | None) -> float:
    """Expected number of players at `pos` taken before your next turn."""
    if not picks_until:
        return 0.0
    return run_rate.get(pos, 0.0) * picks_until


def _escape_probability(rank_in_remaining: int, gone: float) -> float:
    """
    Roughly how likely this player is to be gone before your next turn.

    He is taken if enough of the picks at his position land at or above
    him, so the chance rises with how many are expected to go and falls
    with how many better players at his position sit ahead of him and
    would absorb those picks first. Modelled as 1 - exp(-gone / rank),
    which is continuous, always between 0 and 1, and -- unlike a capped
    ratio -- keeps responding once a run is genuinely underway instead
    of pinning at certainty and going flat.
    """
    if gone <= 0:
        return 0.0
    return 1.0 - pow(2.718281828459045, -gone / max(rank_in_remaining, 1))


# ------------------------------------------------------------------ suggestions


def suggest(
    board: dict[str, Any],
    state: dict[str, Any],
    user_id: str | None,
    *,
    limit: int = _SUGGESTION_COUNT,
) -> dict[str, Any]:
    """
    Rank the best available players for this user's next pick.

    Every suggestion carries the reasoning that produced it -- the VORP,
    the need it fills, the scarcity pressure, and any hard flag -- rather
    than a bare number. A draft pick is the user's call; this says what
    the board thinks and why, and shows its work.
    """
    shape = board.get("shape") or season_long.league_shape(None)
    taken_sleeper_ids = {p["player_id"] for p in state["picks"] if p.get("player_id")}
    taken_names = {
        (p.get("name") or "").strip().lower() for p in state["picks"] if p.get("name")
    }

    available = [
        p
        for p in board["players"]
        if p.get("vorp") is not None
        and p.get("sleeper_id") not in taken_sleeper_ids
        and (p.get("name") or "").strip().lower() not in taken_names
    ]

    mine = _my_picks(state, user_id)
    counts = _roster_counts(mine)
    need = _need_scores(counts, shape)
    run_rate = _position_run_rate(state["picks"], state["teams"], shape)
    picks_until = state.get("picks_until_my_turn")

    # Who is actually left at each position, best-first. This is the
    # backbone of the scarcity read: it lets the question be asked
    # concretely -- if you pass on this player, who do you get instead?
    remaining: dict[str, list[dict[str, Any]]] = {}
    for p in sorted(available, key=lambda x: -x["vorp"]):
        remaining.setdefault(p["position"], []).append(p)
    rank_in_remaining = {
        id(p): i for pool in remaining.values() for i, p in enumerate(pool, start=1)
    }
    pos_left = {pos: len(pool) for pos, pool in remaining.items()}

    tier_left: dict[tuple[str, int], int] = {}
    for p in available:
        key = (p["position"], p.get("tier") or 0)
        tier_left[key] = tier_left.get(key, 0) + 1

    my_byes: dict[int, int] = {}
    for p in mine:
        row = next((b for b in board["players"] if b.get("sleeper_id") == p.get("player_id")), None)
        bye = (row or {}).get("bye_week")
        if bye:
            my_byes[bye] = my_byes.get(bye, 0) + 1

    scored = []
    for p in available:
        pos = p["position"]
        vorp = float(p["vorp"])

        need_frac = need.get(pos, 0.0)
        # Once the starters at a position are filled, additional bodies
        # are bench depth -- real, but worth far less than a hole.
        need_term = _NEED_WEIGHT * (need_frac if need_frac > 0 else _DEPTH_DISCOUNT)

        gone = _expected_gone(pos, run_rate, picks_until)
        pool = remaining[pos]
        k = rank_in_remaining[id(p)]

        # What you would actually end up with at this position if you
        # passed and the expected number of picks went by: the player
        # sitting `gone` places further down the same list. The gap
        # between him and this player is the real cost of waiting --
        # small in the middle of a deep position, large at a cliff.
        fallback_idx = min(k - 1 + max(int(round(gone)), 1), len(pool) - 1)
        value_at_risk = max(0.0, vorp - pool[fallback_idx]["vorp"])
        escape = _escape_probability(k, gone)
        scarcity_term = (
            _SCARCITY_WEIGHT * escape * value_at_risk * max(need_frac, _DEPTH_DISCOUNT)
        )

        same_tier_left = tier_left.get((pos, p.get("tier") or 0), 0)

        flags = []
        if same_tier_left <= _TIER_THIN and need_frac > 0:
            flags.append(
                f"Last {same_tier_left} in tier {p.get('tier')} at {pos}"
                + (f"; about {gone:.0f} {pos}s likely gone before your next pick" if gone >= 1 else "")
            )
        bye = p.get("bye_week")
        if bye and my_byes.get(bye, 0) + 1 >= _BYE_PILEUP:
            flags.append(f"Would be your {my_byes[bye] + 1}th starter on the week {bye} bye")
        if p.get("injury_status"):
            flags.append(f"Injury status: {p['injury_status']}")

        score = vorp + need_term + scarcity_term
        scored.append(
            {
                **p,
                "draft_score": round(score, 2),
                "why": {
                    "vorp": round(vorp, 2),
                    "fills_need": round(need_frac, 2),
                    "need_bonus": round(need_term, 2),
                    "scarcity_bonus": round(scarcity_term, 2),
                    "position_run_rate": round(run_rate.get(pos, 0.0), 2),
                    "expected_gone_before_your_pick": round(gone, 1),
                    "chance_he_is_gone": round(escape, 2),
                    "value_lost_if_you_wait": round(value_at_risk, 2),
                    "fallback_if_you_wait": pool[fallback_idx]["name"],
                    "same_tier_remaining": same_tier_left,
                },
                "flags": flags,
            }
        )

    scored.sort(key=lambda p: -p["draft_score"])

    return {
        "suggestions": scored[:limit],
        "roster_counts": counts,
        "needs": {k: v for k, v in need.items() if v > 0},
        "position_run_rate": run_rate,
        "bye_counts": my_byes,
        "my_picks": mine,
        "available_by_position": pos_left,
    }


async def live(
    draft_id: str, user_id: str | None = None, *, league: dict[str, Any] | None = None
) -> dict[str, Any]:
    """One call for the UI's poll loop: current state plus fresh advice."""
    board = await season_long.build_board(league)
    state = await draft_state(draft_id, user_id)
    return {"state": state, "board_meta": {k: v for k, v in board.items() if k != "players"}, **suggest(board, state, user_id)}
