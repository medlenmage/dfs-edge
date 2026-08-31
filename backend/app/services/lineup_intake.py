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

from app.services import projections, salaries
from app.services.lineup_export import players_in_slot_order, stack_info
from app.services.optimizer import (
    ROSTER_SIZE,
    SALARY_CAP,
    SLOT_REQUIREMENTS,
    SLOT_TYPES,
    build_player_pool,
)
from app.services.optimizer import _eligible_slots as eligible_slots
from app.services.player_match import normalize_name, normalize_team

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
    include_bench: bool = False,
) -> dict[str, Any]:
    """
    Index this slate's optimizable pool so a lineup can identify its
    players however the source happens to.

    NAME IS THE PRIMARY KEY, on purpose. Matching on DraftKings ids
    turned out to be a trap: a DK entries file's roster cells hold
    per-draft-group DRAFTABLE ids, while the ID column of a DK salary
    CSV can hold DraftKings' stable PLAYER ids -- measured on a real
    file, the two spaces did not overlap on a single one of 128 pool
    players, so an id join silently matched nothing. Names are the one
    thing every source agrees on, and player_match.normalize_name
    already handles the accents, suffixes and nicknames that make them
    awkward. Team is folded in where it is known, since it is what makes
    a name unambiguous.

    This app's OWN player ids stay usable, because they are ours and
    there is no second id space to disagree with. The DK id index is
    kept only as a last resort for a source that has nothing else.

    The pool is `optimizer.build_player_pool()` -- deliberately the same
    pool the optimizer itself builds from, so a lineup the optimizer
    just produced can always be matched back, and so a player it
    excluded (no salary, no projection, scratched) is equally
    unmatchable here rather than entering the sim with no way to score
    him.

    `include_bench` widens that for lineups you have ALREADY ENTERED.
    The slate carries a team's confirmed starting nine, so a lineup
    built before lineups posted can reference a player who is now on the
    bench -- real, draftable, with a salary and a projection, just not
    starting. Refusing the whole lineup is the wrong answer there: the
    entry exists on DraftKings whether this app likes it or not, and the
    useful thing is to take it in, mark the benched player, and let late
    swap deal with him. Benched players are tagged `bench=True` and are
    only ever reachable through this flag -- the optimizer's own pool is
    untouched, so nothing can DRAFT one.

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

    if include_bench:
        pool = pool + _bench_pool(slate, pool, projection_source=projection_source)

    by_dk: dict[str, dict[str, Any]] = {}
    by_id: dict[int, dict[str, Any]] = {}
    by_team_name: dict[tuple[str, str], dict[str, Any]] = {}
    name_hits: dict[str, list[dict[str, Any]]] = {}
    for p in pool:
        if p.get("dk_id"):
            by_dk[str(p["dk_id"])] = p
        by_id[p["id"]] = p
        normalized = normalize_name(p.get("name") or "")
        by_team_name[(normalize_team(p.get("team") or ""), normalized)] = p
        name_hits.setdefault(normalized, []).append(p)

    return {
        "pool": pool,
        "by_dk_id": by_dk,
        "by_id": by_id,
        "by_team_name": by_team_name,
        "by_name": {n: hits[0] for n, hits in name_hits.items() if len(hits) == 1},
        "ambiguous_names": {n for n, hits in name_hits.items() if len(hits) > 1},
    }


def _bench_pool(
    slate: dict[str, Any],
    starters: list[dict[str, Any]],
    *,
    projection_source: str,
) -> list[dict[str, Any]]:
    """
    Draftable players on this slate's teams who are NOT in the optimizer
    pool -- overwhelmingly, bats sitting tonight.

    Built from the two uploads that already describe them: the DK salary
    file (salary, DK position eligibility, team) and the projections
    file (projected points, ownership). Both cover the whole draft
    group, not just tonight's starters, which is why a benched player
    can be reconstructed at all.

    The MLB player id is taken from the slate where the team's roster
    happens to include him, and left None otherwise. That is honest
    rather than convenient: without a real id the Monte Carlo engine has
    no game log to sample, so a lineup holding him can be STORED and
    AUDITED but not meaningfully simulated -- which is exactly what
    `bench=True` is there to let callers see.
    """
    known = {p["id"] for p in starters}
    known_names = {normalize_name(p["name"] or "") for p in starters}

    teams: dict[str, int | None] = {}
    for g in slate.get("games") or []:
        for side in ("home", "away"):
            t = g.get(side) or {}
            if t.get("abbrev"):
                teams[normalize_team(t["abbrev"])] = g.get("game_pk")
    if not teams:
        return []

    fpts_key = "inhouse_fpts" if projection_source == "inhouse" else "fpts"
    own_key = "inhouse_ownership_pct" if projection_source == "inhouse" else "ownership_pct"
    day = slate.get("date")
    salary_rows = salaries.load(day) or []
    proj_by_name = {
        r["normalized_name"]: r for r in (projections.load(day) or []) if r.get("normalized_name")
    }

    out: list[dict[str, Any]] = []
    for row in salary_rows:
        team = normalize_team(row.get("team") or "")
        if team not in teams:
            continue
        name_norm = row.get("normalized_name") or normalize_name(row.get("name") or "")
        if name_norm in known_names:
            continue
        proj = proj_by_name.get(name_norm) or {}
        fpts = proj.get(fpts_key)
        if fpts is None or not row.get("salary"):
            continue
        slots = eligible_slots(row.get("position") or "")
        if not slots:
            continue
        out.append(
            {
                "id": None,
                "name": row.get("name"),
                "team": team,
                "opponent": None,
                "game_pk": teams[team],
                "dk_id": row.get("dk_id") or "",
                "salary": row["salary"],
                "projected_fpts": float(fpts),
                "ownership_pct": proj.get(own_key) or 0,
                "slots": slots,
                "edge_composite": None,
                # The flag every consumer keys off: he is rosterable on
                # DraftKings, but he is not in a confirmed lineup, so
                # nothing here should treat him as a normal pool player.
                "bench": True,
            }
        )
    return [p for p in out if p["id"] not in known]


def resolve_player(
    token: Any, lookup: dict[str, Any], *, team: str | None = None
) -> tuple[dict[str, Any] | None, str | None]:
    """
    One player reference -> the pool entry, or (None, reason).

    Name first, and with `team` when the caller knows it -- see
    build_lookup for why an id join across two systems is not
    trustworthy here. This app's own player id is honoured next (it is
    ours, so it cannot disagree with itself), and a DraftKings id is the
    last resort for a source that supplied nothing else.
    """
    if token is None or (isinstance(token, str) and not token.strip()):
        return None, "empty roster slot"

    if isinstance(token, dict):
        team = team or token.get("team")
        for key in ("name", "id", "player_id", "dk_id"):
            if token.get(key):
                return resolve_player(token[key], lookup, team=team)
        return None, "player object carried no name or id"

    text = str(token).strip()
    normalized = normalize_name(text)

    if team:
        hit = lookup["by_team_name"].get((normalize_team(team), normalized))
        if hit:
            return hit, None
    if normalized in lookup["by_name"]:
        return lookup["by_name"][normalized], None
    if normalized in lookup["ambiguous_names"]:
        return (
            None,
            f"'{text}' matches more than one player on this slate -- say which team he's on",
        )

    if text.isdigit() and int(text) in lookup["by_id"]:
        return lookup["by_id"][int(text)], None
    if text in lookup["by_dk_id"]:
        return lookup["by_dk_id"][text], None
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
    bench = [p["name"] for p in players if p.get("bench")]
    return {
        "salary_used": sum(p["salary"] for p in players),
        # Players on this entry who are not in a confirmed lineup today.
        # Empty for anything the optimizer built; non-empty means the
        # entry is live on DraftKings with a bat that is sitting, which
        # is a late-swap job and is why the entry was accepted rather
        # than rejected.
        "non_starters": bench,
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
                "bench": bool(p.get("bench")),
            }
            for p in players
        ],
        # Identity-keyed, not id-keyed: bench players have no MLB id, and
        # collapsing them all onto None would make two different lineups
        # look like duplicates of each other.
        "player_ids": frozenset(_identity(p) for p in players),
    }


def _identity(player: dict[str, Any]) -> Any:
    """
    A stable identity for one rostered player.

    Bench players carry no MLB id (they have no confirmed lineup spot,
    so there is nothing to look one up from), and comparing on a bare
    `id` would make every pair of them look like the same person -- a
    real false "rostered twice" on any lineup holding two bats that are
    sitting. Falls back to the DK id, then the name.
    """
    if player.get("id") is not None:
        return ("mlb", player["id"])
    if player.get("dk_id"):
        return ("dk", str(player["dk_id"]))
    return ("name", normalize_name(player.get("name") or ""))


def _validate(players: list[dict[str, Any]]) -> list[str]:
    """Everything wrong with this roster, in the order a person would
    want to hear it. Empty means it is legal."""
    problems: list[str] = []
    if len(players) != ROSTER_SIZE:
        problems.append(f"{len(players)} players, needs exactly {ROSTER_SIZE}")
        return problems

    ids = [_identity(p) for p in players]
    if len(set(ids)) != len(ids):
        dupes = sorted({p["name"] for p in players if ids.count(_identity(p)) > 1})
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
        teams = roster.get("teams") or []
        players: list[dict[str, Any]] = []
        problems: list[str] = []
        for i_t, token in enumerate(tokens):
            player, reason = resolve_player(
                token, lookup, team=teams[i_t] if i_t < len(teams) else None
            )
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
    (dk_entries.pool_lookup), and it is what makes this work at all. A
    roster cell holds nothing but a per-draft-group DRAFTABLE id -- no
    name -- and that number is not the one a DK salary CSV's ID column
    carries, so there is no id path from the cell to this slate. The
    file's own pool translates its ids into names and teams, which then
    resolve exactly the way a typed-in lineup does. It is the file
    describing itself.
    """
    rosters = []
    for e in parsed:
        if contest_id and str(e.get("contest_id")) != str(contest_id):
            continue
        picks = e.get("picks") or []
        if not picks or any(p is None for p in picks):
            continue
        teams: list[str | None] = []
        if file_pool:
            # Hand the resolver a NAME and a TEAM wherever the file's own
            # pool knows this id, so nothing depends on two id spaces
            # agreeing -- see build_lookup for what that cost.
            resolved = [file_pool["by_dk_id"].get(str(pid)) or {} for pid in picks]
            picks = [r.get("name") or pid for r, pid in zip(resolved, picks)]
            teams = [r.get("team") for r in resolved]
        rosters.append(
            {"players": picks, "teams": teams, "label": f"DK entry {e.get('entry_id')}"}
        )
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
