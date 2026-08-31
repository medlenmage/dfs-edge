"""
Get lineups you built yourself into the contest batch -- from the
optimizer, from a filled DraftKings entries CSV, or typed in by hand.

WHY THIS EXISTS. The contest generator builds lineups by weighted
random construction. That is the right model for an OPPONENT FIELD --
it is what a real public field looks like -- but it is the wrong way to
produce the entries you are going to play, because it can only be
steered, not instructed. Asking it for a portfolio that follows the
process rules (services/../data/process_rules.py) means generating
thousands of lineups and hoping enough rule-abiding ones fall out, then
selecting from whatever did. The optimizer, by contrast, is told: stack
shapes, locks, exclusions, exposure caps, ownership bounds, salary
floor -- it produces compliant lineups by construction.

So the two engines answer different questions and this module lets them
be used together: the optimizer (or your own hand-built lineups) supply
YOUR entries, the generator supplies the FIELD they get simulated
against, and everything downstream -- the simulator, the build audit,
the daily brief -- reads one batch.

Every entry that comes through here is tagged with its `source`
("optimizer", "manual", "dk-csv"), and generated field lineups are
tagged "generated", so the rest of the app can tell "the lineups I am
entering" from "the opponents I am entering against". Nothing else in
the pipeline had a way to express that distinction before.

VALIDATION IS STRICT ON PURPOSE. An invalid lineup that slips through
does not fail loudly -- it silently corrupts every simulated number
that follows, because the sim will happily score a nine-man roster or a
lineup $3,000 over the cap. So a lineup is either fully legal (right
size, no duplicates, every player on this slate, a legal assignment to
DK's roster slots, and inside the salary cap) or it is REJECTED with a
reason naming what was wrong. Nothing is silently dropped and nothing
is silently repaired.
"""

from __future__ import annotations

import logging
from typing import Any

from app.services.lineup_export import players_in_slot_order, stack_info
from app.services.optimizer import (
    ROSTER_SIZE,
    SALARY_CAP,
    SLOT_REQUIREMENTS,
    SLOT_TYPES,
    build_player_pool,
)
from app.services.player_match import normalize_name

log = logging.getLogger(__name__)

SOURCES = ("optimizer", "manual", "dk-csv")
GENERATED = "generated"


class IntakeError(ValueError):
    """The whole request was unusable (no pool, nothing supplied).
    Per-lineup problems are reported per lineup, not raised."""


# ---------------------------------------------------------------- matching


def build_lookup(
    slate: dict[str, Any],
    *,
    projection_source: str = "rotowire",
    included_game_pks: list[int] | None = None,
) -> dict[str, Any]:
    """
    Index this slate's optimizable pool three ways, so a lineup can name
    its players however the source happens to: by DraftKings id (what a
    DK entries CSV carries), by this app's own MLB player id (what the
    optimizer and the frontend carry), or by name (what a human types).

    The pool is `optimizer.build_player_pool()` -- deliberately the same
    pool the optimizer itself builds from, so a lineup the optimizer
    just produced can always be matched back, and so a player it
    excluded (no salary, no projection, scratched) is equally
    unmatchable here rather than entering the sim with no way to score
    him.

    Names are indexed only when UNAMBIGUOUS across the slate. Two
    players who normalize to the same name are both dropped from the
    name index rather than one silently winning -- a lineup that names
    one of them gets a clear "ambiguous" rejection instead of a
    coin-flip roster.
    """
    pool = build_player_pool(
        slate,
        included_game_pks=set(included_game_pks) if included_game_pks else None,
        projection_source=projection_source,
    )
    if not pool:
        raise IntakeError(
            "No optimizable players on this slate -- a DraftKings salary file and a "
            "projections file both need to be loaded before lineups can be brought in."
        )

    by_dk: dict[str, dict[str, Any]] = {}
    by_id: dict[int, dict[str, Any]] = {}
    name_hits: dict[str, list[dict[str, Any]]] = {}
    for p in pool:
        if p.get("dk_id"):
            by_dk[str(p["dk_id"])] = p
        by_id[p["id"]] = p
        name_hits.setdefault(normalize_name(p.get("name") or ""), []).append(p)

    return {
        "pool": pool,
        "by_dk_id": by_dk,
        "by_id": by_id,
        "by_name": {n: hits[0] for n, hits in name_hits.items() if len(hits) == 1},
        "ambiguous_names": {n for n, hits in name_hits.items() if len(hits) > 1},
    }


def resolve_player(token: Any, lookup: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """
    One player reference -> the pool entry, or (None, reason).

    Tries the identifier forms in order of how reliable they are: DK id,
    this app's player id, then name. A bare number could be either kind
    of id, so both are tried before giving up on it.
    """
    if token is None or (isinstance(token, str) and not token.strip()):
        return None, "empty roster slot"

    if isinstance(token, dict):
        for key in ("dk_id", "id", "player_id", "name"):
            if token.get(key):
                return resolve_player(token[key], lookup)
        return None, "player object carried no id or name"

    text = str(token).strip()
    if text in lookup["by_dk_id"]:
        return lookup["by_dk_id"][text], None
    if text.isdigit() and int(text) in lookup["by_id"]:
        return lookup["by_id"][int(text)], None

    normalized = normalize_name(text)
    if normalized in lookup["by_name"]:
        return lookup["by_name"][normalized], None
    if normalized in lookup["ambiguous_names"]:
        return None, f"'{text}' matches more than one player on this slate -- use the id"
    return None, f"'{text}' is not on this slate (or has no salary/projection loaded)"


# ---------------------------------------------------------------- legality


def _assign_slots(players: list[dict[str, Any]]) -> list[str] | None:
    """
    Find a legal assignment of these players to DK's roster slots, or
    None if none exists.

    Multi-eligibility makes this a real question rather than a count: a
    lineup can hold exactly one catcher, one first baseman and so on and
    still be illegal, and it can also LOOK short at a position and be
    fine because a 1B/3B player covers the gap. Ten players over seven
    slot types is tiny, so this is an exact backtracking search over the
    scarcest slot first -- no need for a matching algorithm, and unlike a
    greedy pass it cannot report a legal lineup as illegal.
    """
    if len(players) != ROSTER_SIZE:
        return None

    demand = [slot for slot, n in SLOT_REQUIREMENTS.items() for _ in range(n)]
    # Fill the slots with the fewest eligible players first; that is what
    # keeps the search from exploring branches that were never going to
    # work (a catcher slot with two candidates before an outfield slot
    # with fifteen).
    eligible = {
        i: {s for s in (p.get("slots") or []) if s in SLOT_TYPES} for i, p in enumerate(players)
    }
    demand.sort(key=lambda slot: sum(1 for e in eligible.values() if slot in e))

    assignment: dict[int, str] = {}

    def place(k: int) -> bool:
        if k == len(demand):
            return True
        slot = demand[k]
        for i, p in enumerate(players):
            if i in assignment or slot not in eligible[i]:
                continue
            assignment[i] = slot
            if place(k + 1):
                return True
            del assignment[i]
        return False

    if not place(0):
        return None
    return [assignment[i] for i in range(len(players))]


def _order_for_roster(players: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """
    Put a legal lineup into DK's fixed roster order (P, P, C, 1B, 2B,
    3B, SS, OF, OF, OF), which is the order every consumer in this app
    assumes a contest entry's `players` list is already in.
    """
    slots = _assign_slots(players)
    if slots is None:
        return None
    by_slot: dict[str, list[dict[str, Any]]] = {}
    for p, slot in zip(players, slots):
        by_slot.setdefault(slot, []).append(p)
    ordered: list[dict[str, Any]] = []
    for slot, n in SLOT_REQUIREMENTS.items():
        ordered.extend(by_slot.get(slot, [])[:n])
    return ordered if len(ordered) == ROSTER_SIZE else None


# ---------------------------------------------------------------- conversion


def _to_entry(players: list[dict[str, Any]], source: str, label: str | None) -> dict[str, Any]:
    """Pool players in roster order -> the contest-entry shape every
    other service in this app already consumes."""
    # Imported here rather than at module scope: contest.py imports this
    # module for its injection path, so a top-level import would be
    # circular.
    from app.services.contest import _duplication_risk

    stack_type, stack = stack_info({"players": players})
    return {
        "salary_used": sum(p["salary"] for p in players),
        "stack_type": stack_type,
        "stack": stack,
        "projected_points": round(sum(p["projected_fpts"] for p in players), 2),
        "total_ownership_pct": round(sum(p["ownership_pct"] for p in players), 1),
        "duplication_risk": _duplication_risk(players),
        "source": source,
        "label": label,
        "players": [
            {
                "id": p["id"],
                "name": p["name"],
                "team": p["team"],
                "salary": p["salary"],
                "projected_fpts": p["projected_fpts"],
                "ownership_pct": p["ownership_pct"],
                "edge_composite": p.get("edge_composite"),
                "dk_id": p.get("dk_id") or "",
                "game_pk": p.get("game_pk"),
            }
            for p in players
        ],
        "player_ids": frozenset(p["id"] for p in players),
    }


def _validate(players: list[dict[str, Any]]) -> list[str]:
    """Everything wrong with this roster, in the order a person would
    want to hear it. Empty means it is legal."""
    problems: list[str] = []
    if len(players) != ROSTER_SIZE:
        problems.append(f"{len(players)} players, needs exactly {ROSTER_SIZE}")
        return problems

    ids = [p["id"] for p in players]
    if len(set(ids)) != len(ids):
        dupes = sorted({p["name"] for p in players if ids.count(p["id"]) > 1})
        problems.append("same player rostered twice: " + ", ".join(dupes))

    salary = sum(p["salary"] for p in players)
    if salary > SALARY_CAP:
        problems.append(f"${salary:,} is ${salary - SALARY_CAP:,} over the ${SALARY_CAP:,} cap")

    if _assign_slots(players) is None:
        counts: dict[str, int] = {}
        for p in players:
            for s in p.get("slots") or []:
                counts[s] = counts.get(s, 0) + 1
        short = [s for s, n in SLOT_REQUIREMENTS.items() if counts.get(s, 0) < n]
        problems.append(
            "these players can't legally fill DK's roster slots"
            + (f" (nobody eligible at {', '.join(short)})" if short else "")
        )
    return problems


def intake(
    rosters: list[dict[str, Any]],
    lookup: dict[str, Any],
    *,
    source: str,
) -> dict[str, Any]:
    """
    Turn a list of `{players: [...], label?: str}` rosters into contest
    entries.

    Returns accepted entries AND rejections, each rejection naming the
    lineup and everything wrong with it. A caller decides whether a
    partial result is acceptable; this function does not decide for
    them by dropping the bad ones quietly.
    """
    if source not in SOURCES:
        raise IntakeError(f"source must be one of {', '.join(SOURCES)}.")

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for i, roster in enumerate(rosters):
        label = roster.get("label") or f"{source} #{i + 1}"
        tokens = roster.get("players") or []
        players: list[dict[str, Any]] = []
        problems: list[str] = []
        for token in tokens:
            player, reason = resolve_player(token, lookup)
            if player is None:
                problems.append(reason or "unmatched player")
            else:
                players.append(player)

        # Only validate the roster once every player resolved. Reporting
        # "9 players, needs exactly 10" on top of "'Jon Doe' is not on
        # this slate" is the same problem said twice, and buries the one
        # that can actually be fixed.
        if not problems:
            problems = _validate(players)
        if problems:
            rejected.append({"index": i, "label": label, "problems": problems})
            continue

        ordered = _order_for_roster(players)
        if ordered is None:  # pragma: no cover -- _validate already caught this
            rejected.append({"index": i, "label": label, "problems": ["no legal roster assignment"]})
            continue
        accepted.append(_to_entry(ordered, source, label))

    return {"entries": accepted, "rejected": rejected}


# ---------------------------------------------------------------- sources


def from_optimizer(result: dict[str, Any], lookup: dict[str, Any]) -> dict[str, Any]:
    """
    Optimizer output -> contest entries.

    The optimizer groups players under `slots`; `players_in_slot_order`
    already flattens either shape, and the players are re-resolved
    against the pool rather than trusted as-is, so a stale batch (built
    before a scratch, or against a different projection source) is
    caught here instead of quietly simulating numbers that no longer
    apply.
    """
    rosters = [
        {
            "players": [p.get("id") for p in players_in_slot_order(lu)],
            "label": f"optimizer #{i + 1}"
            + (f" ({lu.get('stack_type')} {lu.get('stack')})" if lu.get("stack") else ""),
        }
        for i, lu in enumerate(result.get("lineups") or [])
    ]
    return intake(rosters, lookup, source="optimizer")


def from_dk_entries(
    parsed: list[dict[str, Any]],
    lookup: dict[str, Any],
    *,
    contest_id: str | None = None,
    file_pool: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    A filled DraftKings bulk-entries CSV -> contest entries.

    This is the "manual lineups" path that costs nothing to use: the
    file DK already gives you, with whatever lineups you have built on
    its own site, read back in. Entries with any blank roster cell are
    reservations that were never filled, so they are skipped rather than
    reported as broken.

    `file_pool` is the player pool embedded in that same file
    (dk_entries.pool_lookup), and it matters more than it sounds. A
    roster cell holds a per-draft-group DRAFTABLE id, which is a
    different number from the player id a DK salary CSV's own ID column
    may carry -- so resolving the cell against the slate's own dk_id
    index can miss every single player even though the file is perfectly
    valid. The file's pool translates its own ids to names, which then
    resolve against the slate the same way a typed-in lineup does. It is
    the file describing itself, and it is the only thing that always
    lines up.
    """
    rosters = []
    for e in parsed:
        if contest_id and str(e.get("contest_id")) != str(contest_id):
            continue
        picks = e.get("picks") or []
        if not picks or any(p is None for p in picks):
            continue
        if file_pool:
            # Hand the resolver a NAME wherever the file's own pool knows
            # this id, so it never depends on two id spaces agreeing.
            picks = [
                (file_pool["by_dk_id"].get(str(pid)) or {}).get("name") or pid for pid in picks
            ]
        rosters.append({"players": picks, "label": f"DK entry {e.get('entry_id')}"})
    if not rosters:
        return {
            "entries": [],
            "rejected": [],
            # Worth spelling out, because a blank template is the NORMAL
            # state of the file DraftKings hands you: it is the thing you
            # fill in, so an unfilled one is not an error, it just has
            # nothing to import. The other path -- filling a blank
            # template FROM this app -- is the build audit's entry
            # filler, which is the opposite direction.
            "note": (
                "That file's entry rows are all still blank, so there are no lineups in it to "
                "import. This path is for a file whose lineups you already built on DraftKings' "
                "own site and then re-exported. To go the other way -- fill a blank template "
                "from lineups built here -- use the entry filler under Daily briefs."
            ),
        }
    result = intake(rosters, lookup, source="dk-csv")
    if file_pool:
        _sharpen_rejections(result["rejected"], file_pool)
    return result


def _sharpen_rejections(rejected: list[dict[str, Any]], file_pool: dict[str, Any]) -> None:
    """
    Replace "not on this slate" with what is actually true, in place.

    A player can be perfectly draftable in the contest you entered and
    still be absent from this app's pool -- the commonest reason by far
    being that he is NOT IN TODAY'S CONFIRMED LINEUP, since the slate
    carries the nine confirmed starters once a lineup posts. Telling
    someone their real entry references a player "not on this slate"
    sends them looking for a missing upload; telling them he is benched
    today is both true and the thing they would actually act on before
    lock.
    """
    known = file_pool.get("by_name") or {}
    for r in rejected:
        fixed = []
        for problem in r["problems"]:
            name = None
            if problem.startswith("'") and "is not on this slate" in problem:
                name = problem.split("'")[1]
            row = known.get(normalize_name(name)) if name else None
            if row:
                fixed.append(
                    f"{name} ({row['team']}) is in the contest's player pool but not in this "
                    "app's -- almost always because he is not in today's confirmed lineup"
                )
            else:
                fixed.append(problem)
        r["problems"] = fixed
