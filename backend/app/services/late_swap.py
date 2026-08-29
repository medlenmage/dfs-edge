"""
Late swap for a whole batch of contest entries.

DraftKings locks each roster spot at THAT PLAYER'S OWN game start, not
at slate start -- so a main slate spanning 7:05 and 10:10 ET first
pitches leaves three hours of continuous roster editing after the
contest has technically begun. Neither DK nor FanDuel auto-replaces a
scratched or postponed player, so an entry still holding one simply
scores zero in that spot. Late swap isn't a convenience here; it's core
play.

`optimizer.late_swap()` already does this for ONE lineup by re-solving
a MILP with the locked players pinned. That cannot be the batch
mechanism, for two separate reasons:

  - Cost. A real batch here runs to 10,000 entries, and each MILP solve
    is expensive.
  - Diversity. Re-optimizing every entry against the same current slate
    drives them all toward the same handful of best available players.
    That collapses a deliberately diverse batch into near-duplicates,
    which is precisely what a large-field GPP punishes hardest -- see
    contest._duplication_risk() for why cumulative ownership, not
    summed ownership, is the thing that hurts.

So this repairs instead of re-optimizing: it touches only the spots
that genuinely need it, and leaves everything else exactly as built.

WHAT COUNTS AS NEEDING A SWAP
-----------------------------
  - DEAD -- the real trigger. Confirmed scratched (lineup_watch.py's
    own detection, surfaced as `team["scratches"]`), in a postponed /
    cancelled / suspended game, or vanished from the current player
    pool entirely.
  - DOWNGRADED -- optional, `mode="refresh"`. His CURRENT projection
    has fallen materially below what it was when the entry was built.
    Entries store `projected_fpts` as of build time and the live slate
    carries today's number, so this is directly measurable rather than
    inferred. Catches the real "projected to lead off, confirmed
    batting 8th" case that a pure scratch check misses entirely.

Only spots whose game has NOT started are swappable. A dead player in
an already-locked spot is a real sunk loss -- reported in the summary
rather than quietly "fixed", because a real DK entry can't be touched
there either.

NBA READINESS
-------------
Nothing here is baseball-specific. Lock state comes from game start
times, eligibility from roster-slot codes, and affordability from a
salary cap -- all three of which the caller supplies. The MLB-only
knowledge (which slot codes exist, what the cap is) stays in the
caller, so an NBA build reuses this module rather than forking it.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "SWAP_MODES",
    "LateSwapError",
    "slate_lock_state",
    "swap_batch",
]

SWAP_MODES = ("repair", "refresh")

# How far a player's projection must have fallen since the entry was
# built before `refresh` mode treats him as worth replacing. Deliberately
# blunt: a small drift is noise (and re-shuffling a lineup over noise
# just burns diversity for nothing), while a real demotion in the
# batting order moves a projection much further than this.
_DOWNGRADE_DROP_PCT = 25.0

# How many of the best affordable replacements to sample from, rather
# than always taking the single best. If 500 entries all held the same
# scratched player, always taking the top replacement would drive all
# 500 to the identical fix -- turning one scratch into a mass
# duplication event. Sampling across a realistic shortlist keeps the
# batch's diversity roughly where it started.
_REPLACEMENT_SHORTLIST = 8

# A replacement from the outgoing player's own team is worth a real
# premium when he was part of the entry's stack: keeping the stack
# intact preserves the correlation the lineup was built around, which
# is worth more than a small raw-projection edge from an uncorrelated
# bat.
_SAME_TEAM_STACK_BONUS = 1.25

# MLB statuses that mean nobody in this game will score. Checked as a
# lowercase substring so "Postponed", "Suspended: Rain" and
# "Cancelled" all match without enumerating every real variant.
_DEAD_GAME_STATUSES = ("postponed", "cancelled", "canceled", "suspended")


class LateSwapError(ValueError):
    """Raised for a request that can't be honoured as asked."""


def _parse_utc(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def slate_lock_state(slate: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """
    Which games are locked, which are still open, and who is dead.

    A game with no usable start time is treated as LOCKED, matching
    optimizer.late_swap()'s existing convention: there's no safe way to
    confirm a spot is still swappable, and touching a player DK might
    already consider locked is worse than being over-cautious.
    """
    now = now or datetime.now(timezone.utc)

    locked: set[int] = set()
    open_games: set[int] = set()
    dead_games: set[int] = set()
    dead_players: dict[int, str] = {}

    for game in slate.get("games") or []:
        pk = game.get("game_pk")
        if pk is None:
            continue
        status = (game.get("status") or "").lower()
        is_dead_game = any(s in status for s in _DEAD_GAME_STATUSES)
        if is_dead_game:
            dead_games.add(pk)

        start = _parse_utc(game.get("game_time_utc"))
        if start is None or start <= now:
            locked.add(pk)
        else:
            open_games.add(pk)

        for side in ("home", "away"):
            team = game.get(side) or {}
            for scratch in team.get("scratches") or []:
                pid = scratch.get("player_id")
                if pid is not None:
                    dead_players[pid] = "scratched"
            if is_dead_game:
                for p in list(team.get("hitters") or []) + [team.get("probable_pitcher")]:
                    if p and p.get("id") is not None:
                        dead_players.setdefault(p["id"], "game postponed")

    return {
        "now": now.isoformat().replace("+00:00", "Z"),
        "locked_game_pks": locked,
        "open_game_pks": open_games,
        "dead_game_pks": dead_games,
        "dead_player_ids": dead_players,
    }


def _entry_stack_teams(players: list[dict[str, Any]], pitcher_slots: int) -> set[str]:
    """Teams supplying 2+ of this entry's HITTERS -- i.e. the teams it's
    actually stacked on, so a repair can preserve that structure."""
    counts: dict[str, int] = {}
    for p in players[pitcher_slots:]:
        team = p.get("team")
        if team:
            counts[team] = counts.get(team, 0) + 1
    return {t for t, n in counts.items() if n >= 2}


def swap_batch(
    entries: list[dict[str, Any]],
    slate: dict[str, Any],
    pool: list[dict[str, Any]],
    *,
    slot_order: list[str],
    salary_cap: int,
    mode: str = "repair",
    now: datetime | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    """
    Repair every entry in `entries` against the CURRENT slate, swapping
    only roster spots that both need it and are still legally
    swappable.

    `entries` are contest.py-shaped (a flat `players` list already in
    roster-slot order); `pool` is optimizer.build_player_pool()'s output
    for the same slate; `slot_order` is the parallel list of slot codes
    (e.g. ["P","P","C","1B","2B","3B","SS","OF","OF","OF"]).

    Returns the swapped entries plus a summary honest about what could
    NOT be fixed -- dead players already locked in, and spots with no
    affordable legal replacement available.
    """
    if mode not in SWAP_MODES:
        raise LateSwapError(f"Unknown swap mode '{mode}'. Choose one of: {', '.join(SWAP_MODES)}.")
    if not entries:
        raise LateSwapError("No entries to late-swap.")

    state = slate_lock_state(slate, now=now)
    open_games = state["open_game_pks"]
    dead_ids = state["dead_player_ids"]

    pool_by_id = {p["id"]: p for p in pool}
    # Only players in a game that hasn't started can be swapped IN --
    # rostering someone whose game already began isn't a legal DK edit.
    available = [
        p for p in pool
        if p.get("game_pk") in open_games and p["id"] not in dead_ids
    ]
    by_slot: dict[str, list[dict[str, Any]]] = {}
    for p in available:
        for slot in p.get("slots") or ():
            by_slot.setdefault(slot, []).append(p)
    for players in by_slot.values():
        players.sort(key=lambda p: -p["projected_fpts"])

    pitcher_slots = sum(1 for s in slot_order if s == "P")
    rng = random.Random(seed if seed is not None else 0)

    swapped_entries: list[dict[str, Any]] = []
    entries_changed = 0
    total_swaps = 0
    stranded: dict[int, int] = {}      # dead but already locked -- a real loss
    unfillable: dict[int, int] = {}    # swappable, but nothing legal to put there
    replaced_counts: dict[int, int] = {}

    for index, entry in enumerate(entries):
        players = list(entry["players"])
        if len(players) != len(slot_order):
            swapped_entries.append(entry)
            continue

        entry_rng = random.Random((seed or 0) * 1_000_003 + index)
        stack_teams = _entry_stack_teams(players, pitcher_slots)
        used_ids = {p["id"] for p in players}
        salary_used = sum(p["salary"] for p in players)
        changed = False

        for i, (player, slot) in enumerate(zip(list(players), slot_order)):
            pid = player["id"]
            current = pool_by_id.get(pid)
            # The entry's own captured game_pk wins: a scratched player
            # can be gone from the current pool entirely, and that's
            # exactly the player whose lock state matters most. Falls
            # back to the live pool for entries built before game_pk was
            # carried on lineups at all.
            game_pk = player.get("game_pk") or (current.get("game_pk") if current else None)

            is_dead = pid in dead_ids or current is None
            is_downgraded = (
                mode == "refresh"
                and current is not None
                and player.get("projected_fpts")
                and current["projected_fpts"]
                < player["projected_fpts"] * (1 - _DOWNGRADE_DROP_PCT / 100)
            )
            if not (is_dead or is_downgraded):
                continue

            # A player whose game already started can't be touched, even
            # though he's dead -- that's a real, unrecoverable loss.
            if game_pk not in open_games:
                if is_dead:
                    stranded[pid] = stranded.get(pid, 0) + 1
                continue

            budget = salary_cap - (salary_used - player["salary"])
            candidates = [
                c for c in by_slot.get(slot, [])
                if c["id"] not in used_ids and c["salary"] <= budget
            ]
            if not candidates:
                unfillable[pid] = unfillable.get(pid, 0) + 1
                continue

            shortlist = candidates[:_REPLACEMENT_SHORTLIST]
            weights = [
                max(c["projected_fpts"], 0.1)
                * (_SAME_TEAM_STACK_BONUS if c["team"] in stack_teams and c["team"] == player.get("team") else 1.0)
                for c in shortlist
            ]
            pick = entry_rng.choices(shortlist, weights=weights, k=1)[0]

            players[i] = {
                "id": pick["id"],
                "name": pick["name"],
                "team": pick["team"],
                "salary": pick["salary"],
                "projected_fpts": pick["projected_fpts"],
                "ownership_pct": pick["ownership_pct"],
                "edge_composite": pick.get("edge_composite"),
                "dk_id": pick.get("dk_id") or "",
                "game_pk": pick.get("game_pk"),
            }
            used_ids.discard(pid)
            used_ids.add(pick["id"])
            salary_used = salary_used - player["salary"] + pick["salary"]
            replaced_counts[pid] = replaced_counts.get(pid, 0) + 1
            total_swaps += 1
            changed = True

        if changed:
            entries_changed += 1
            swapped_entries.append({**entry, "players": players, **_recomputed(players)})
        else:
            swapped_entries.append(entry)

    return {
        "entries": swapped_entries,
        "mode": mode,
        "now": state["now"],
        "entries_changed": entries_changed,
        "total_swaps": total_swaps,
        "open_game_count": len(open_games),
        "locked_game_count": len(state["locked_game_pks"]),
        "postponed_game_count": len(state["dead_game_pks"]),
        "replaced_players": _named(replaced_counts, pool_by_id, slate),
        "stranded_players": _named(stranded, pool_by_id, slate),
        "unfillable_players": _named(unfillable, pool_by_id, slate),
    }


def _recomputed(players: list[dict[str, Any]]) -> dict[str, Any]:
    """The lineup-level aggregates that move when a player is swapped.

    Deliberately does NOT recompute stack_type/stack -- those come from
    lineup_export.stack_info(), and the caller re-derives them so this
    module stays free of any roster-shape knowledge it doesn't need.
    """
    return {
        "salary_used": sum(p["salary"] for p in players),
        "projected_points": round(sum(p["projected_fpts"] for p in players), 2),
        "total_ownership_pct": round(sum(p["ownership_pct"] for p in players), 1),
        "player_ids": frozenset(p["id"] for p in players),
    }


def _named(
    counts: dict[int, int], pool_by_id: dict[int, dict[str, Any]], slate: dict[str, Any]
) -> list[dict[str, Any]]:
    """Turn {player_id: entry_count} into named rows for the UI. Falls
    back to the slate when a player has left the pool entirely, which is
    exactly the case for someone scratched out of the roster listing."""
    if not counts:
        return []
    names: dict[int, dict[str, str]] = {}
    for game in slate.get("games") or []:
        for side in ("home", "away"):
            team = game.get(side) or {}
            abbrev = team.get("abbrev") or ""
            for p in list(team.get("hitters") or []) + [team.get("probable_pitcher")]:
                if p and p.get("id") is not None:
                    names[p["id"]] = {"name": p.get("name") or "", "team": abbrev}
            for scratch in team.get("scratches") or []:
                if scratch.get("player_id") is not None:
                    names[scratch["player_id"]] = {
                        "name": scratch.get("name") or "",
                        "team": scratch.get("team") or abbrev,
                    }
    out = []
    for pid, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        meta = names.get(pid) or {}
        pooled = pool_by_id.get(pid) or {}
        out.append(
            {
                "player_id": pid,
                "name": meta.get("name") or pooled.get("name") or f"Player {pid}",
                "team": meta.get("team") or pooled.get("team") or "",
                "entry_count": n,
            }
        )
    return out
