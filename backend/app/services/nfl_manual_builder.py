"""
Build NFL lineups by hand -- or hand the slate to another Claude and
have it build them for you. The NFL half of manual_builder.py, and
deliberately the same two-part shape:

  1. `slate_brief()` renders everything needed to build a legal DK
     Classic NFL lineup into one self-contained block of markdown: the
     roster and cap rules, this app's own process rules, the games with
     their totals and spreads, and every draftable player grouped by
     roster slot with salary, projection and ownership. Paste it into
     any Claude -- one with no access to this machine, no tools, no
     context -- and it has enough to do the job.

  2. `intake()` reads what comes back, resolves the names against the
     real optimizable pool, and either returns a legal lineup or says
     exactly why it isn't one.

The PARSING half is shared with MLB rather than copied:
`manual_builder.parse_lineups()` is sport-agnostic once its slot-label
prefixes know about QB/RB/WR/TE/FLEX/DST, so both sports get the same
forgiveness about format -- numbered lists, CSV rows, "WR2: Name" slot
labels, blank-line-separated blocks.

Format-forgiving and content-strict, for the same reason as MLB's: a
model asked for nine names will produce them in whatever shape its own
prose habits favour, and rejecting a good lineup over a stray bullet
wastes a round trip. Rejecting an ILLEGAL lineup is never a waste,
because entering one costs real money.

WHY THE SLOT CHECK IS A REAL ASSIGNMENT AND NOT A COUNT

DK Classic NFL has a FLEX, so counting positions does not answer
whether nine players form a legal roster. Three RBs and three WRs is
legal (the third RB takes FLEX); three RBs and four WRs is not, and
both have the same position counts as something legal-looking. So
`_assign_slots()` searches for a real assignment of players to slots,
which is the only way to tell those apart.
"""

from __future__ import annotations

from typing import Any

from app.data import process_rules as rules
from app.services.nfl_optimizer import (
    FLEX_POSITIONS,
    ROSTER_SIZE,
    SALARY_CAP,
    SLOT_REQUIREMENTS,
    SLOT_TYPES,
    _eligible_slots,
)
from app.services.player_match import normalize_name, normalize_team

# Slot order and labels a human (or a model) reads naturally.
_SLOT_LABELS = {
    "QB": "Quarterback (1)",
    "RB": "Running back (2, plus FLEX)",
    "WR": "Wide receiver (3, plus FLEX)",
    "TE": "Tight end (1, plus FLEX)",
    "DST": "Defence / special teams (1)",
}

# Enough of each slot to build from without pasting the whole board.
_DEPTH = {"QB": 24, "RB": 40, "WR": 60, "TE": 26, "DST": 20}


def _fmt_player(p: dict[str, Any]) -> str:
    own = p.get("ownership_pct")
    opponent = f" vs {p['opponent']}" if p.get("opponent") else ""
    return (
        f"  {p['name']} ({p['team']}{opponent})  ${p['salary']:,}  "
        f"{p['projected_fpts']:.1f} pts" + (f"  {own:.0f}% own" if own else "")
    )


def slate_brief(
    pool: list[dict[str, Any]],
    *,
    season: int,
    week: int,
    games: list[dict[str, Any]] | None = None,
    include_rules: bool = True,
) -> str:
    """
    The whole slate as one pasteable brief.

    `pool` is `nfl_optimizer.build_player_pool()`'s output -- the same
    pool every builder in this app draws from, so a lineup built off
    this brief can never reference a player the app would then reject
    as unavailable.
    """
    lines = [
        f"# DraftKings Classic NFL slate — {season} week {week}",
        "",
        "You are building DFS lineups from the board below. Everything you need is here.",
        "",
        "## Roster and salary rules",
        "",
        f"- Exactly {ROSTER_SIZE} players: 1 QB, 2 RB, 3 WR, 1 TE, 1 FLEX, 1 DST.",
        "- FLEX takes a RB, WR or TE — so a legal roster is (for example) 3 RB / 3 WR / 1 TE, "
        "or 2 RB / 4 WR / 1 TE, or 2 RB / 3 WR / 2 TE.",
        f"- Total salary must not exceed ${SALARY_CAP:,}.",
        "- No player twice in the same lineup.",
        "- Each player fills ONE slot, even where he is eligible for more than one.",
    ]

    # Only the games the BOARD actually contains. A football week has
    # more games than a DK slate does -- a Main slate is 12 of 16 -- so
    # listing the whole week would invite lineups built around players
    # who are not on the board at all.
    if games:
        board_teams = {p["team"] for p in pool}
        games = [
            g
            for g in games
            if (g.get("home") or {}).get("abbrev") in board_teams
            and (g.get("away") or {}).get("abbrev") in board_teams
        ]
    if games:
        lines += ["", f"## Games ({len(games)} on this slate)", ""]
        for g in games:
            away, home = g.get("away") or {}, g.get("home") or {}
            bits = f"- {away.get('abbrev')} @ {home.get('abbrev')}"
            total = g.get("total_line") or (g.get("betting") or {}).get("total")
            if total:
                bits += f"  O/U {total}"
            if away.get("implied_total") is not None:
                bits += (
                    f"  ({away.get('abbrev')} {away.get('implied_total')} / "
                    f"{home.get('abbrev')} {home.get('implied_total')} implied)"
                )
            lines.append(bits)

    if include_rules:
        lines += ["", "## How to build (this account's own process rules)", "", rules.RULES_TEXT]

    lines += ["", "## The player board", ""]
    for slot, label in _SLOT_LABELS.items():
        eligible = [p for p in pool if (p.get("position") or "").upper() == slot]
        eligible.sort(key=lambda p: -(p.get("projected_fpts") or 0))
        if not eligible:
            continue
        depth = _DEPTH.get(slot, 20)
        lines += [f"### {label}", ""]
        lines += [_fmt_player(p) for p in eligible[:depth]]
        if len(eligible) > depth:
            lines.append(f"  …and {len(eligible) - depth} more, all lower projected.")
        lines.append("")

    lines += [
        "## What to send back",
        "",
        f"One lineup per block, {ROSTER_SIZE} player names, in roster order "
        "(QB, RB, RB, WR, WR, WR, TE, FLEX, DST). Separate lineups with a blank line.",
        "Names exactly as written above. No salaries, no commentary inside a block.",
        "",
        "Example:",
        "",
        "```",
        "Lineup 1",
        "The Quarterback",
        "Running Back One",
        "Running Back Two",
        "Receiver One",
        "Receiver Two",
        "Receiver Three",
        "The Tight End",
        "Flex Player",
        "Some Defence",
        "",
        "Lineup 2",
        "…",
        "```",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------- intake


def build_lookup(pool: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Index the optimizable pool for name resolution.

    Keyed by (team, name) first and by bare name second, so a name that
    is unique on the slate resolves even when the model wrote no team,
    while a name shared by two players still resolves correctly when it
    carries one.
    """
    by_team_name: dict[tuple[str, str], dict[str, Any]] = {}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for p in pool:
        by_team_name[(normalize_team(p["team"]), normalize_name(p["name"]))] = p
        by_name.setdefault(normalize_name(p["name"]), []).append(p)
    return {"by_team_name": by_team_name, "by_name": by_name}


def resolve_player(lookup: dict[str, Any], name: str, team: str | None = None):
    """One written name -> a real pool player, or None with a reason."""
    norm = normalize_name(name)
    if team:
        hit = lookup["by_team_name"].get((normalize_team(team), norm))
        if hit:
            return hit, None
    matches = lookup["by_name"].get(norm) or []
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        return None, f"{name!r} isn't on this slate"
    teams = sorted({m["team"] for m in matches})
    return None, f"{name!r} is ambiguous — {', '.join(teams)} all have one"


def _assign_slots(players: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]] | None:
    """
    Find a real assignment of players to roster slots, or None.

    Counting positions is not enough once a FLEX exists: 3 RB / 4 WR / 1
    TE has the same look as a legal roster and is not one. This searches
    (the roster is nine players, so exhaustively is cheap) and returns
    the first assignment that fills every slot exactly.
    """
    if len(players) != ROSTER_SIZE:
        return None
    slots_for = [(_eligible_slots(p.get("position") or ""), p) for p in players]
    if any(not s for s, _ in slots_for):
        return None
    # Fill the most constrained players first -- a DST or QB has exactly
    # one home, so placing them last would waste the search.
    slots_for.sort(key=lambda sp: len(sp[0]))

    filled: dict[str, list[dict[str, Any]]] = {slot: [] for slot in SLOT_TYPES}

    def place(i: int) -> bool:
        if i == len(slots_for):
            return all(len(filled[s]) == n for s, n in SLOT_REQUIREMENTS.items())
        eligible, player = slots_for[i]
        for slot in eligible:
            if len(filled[slot]) < SLOT_REQUIREMENTS.get(slot, 0):
                filled[slot].append(player)
                if place(i + 1):
                    return True
                filled[slot].pop()
        return False

    return filled if place(0) else None


def intake(
    lineups: list[dict[str, Any]],
    pool: list[dict[str, Any]],
    *,
    source: str = "manual",
) -> dict[str, Any]:
    """
    Resolve and validate parsed lineups against the real pool.

    Returns {"accepted": [...], "rejected": [...]}. A rejection always
    carries the reason, because a lineup that silently vanishes is worse
    than one that comes back explained -- the user is left wondering
    what happened to it.
    """
    lookup = build_lookup(pool)
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for raw in lineups:
        names = raw.get("players") or []
        label = raw.get("label") or "manual"
        if len(names) != ROSTER_SIZE:
            rejected.append({
                "label": label, "players": names,
                "reason": f"{len(names)} players, needs exactly {ROSTER_SIZE}",
            })
            continue

        players, problems = [], []
        for name in names:
            player, why = resolve_player(lookup, name)
            if player is None:
                problems.append(why)
            else:
                players.append(player)
        if problems:
            rejected.append({"label": label, "players": names, "reason": "; ".join(problems[:3])})
            continue

        ids = [p["id"] for p in players]
        if len(set(ids)) != len(ids):
            dupes = sorted({p["name"] for p in players if ids.count(p["id"]) > 1})
            rejected.append({
                "label": label, "players": names,
                "reason": f"same player rostered twice: {', '.join(dupes)}",
            })
            continue

        salary = sum(p["salary"] for p in players)
        if salary > SALARY_CAP:
            rejected.append({
                "label": label, "players": names,
                "reason": f"${salary:,} is over the ${SALARY_CAP:,} cap",
            })
            continue

        slots = _assign_slots(players)
        if slots is None:
            counts: dict[str, int] = {}
            for p in players:
                counts[p["position"]] = counts.get(p["position"], 0) + 1
            shape = ", ".join(f"{n} {pos}" for pos, n in sorted(counts.items()))
            rejected.append({
                "label": label, "players": names,
                "reason": (
                    f"{shape} can't fill 1 QB / 2 RB / 3 WR / 1 TE / 1 FLEX / 1 DST "
                    "— FLEX only takes a RB, WR or TE"
                ),
            })
            continue

        accepted.append({
            "label": label,
            "source": source,
            "salary_used": salary,
            "salary_remaining": SALARY_CAP - salary,
            "projected_points": round(sum(p["projected_fpts"] for p in players), 2),
            "total_ownership_pct": round(sum(p.get("ownership_pct") or 0 for p in players), 1),
            "slots": {
                slot: [
                    {
                        "id": p["id"], "name": p["name"], "team": p["team"],
                        "opponent": p.get("opponent"), "position": p["position"],
                        "salary": p["salary"], "projected_fpts": p["projected_fpts"],
                        "ownership_pct": p.get("ownership_pct") or 0,
                        "nflverse_id": p.get("nflverse_id"),
                    }
                    for p in filled
                ]
                for slot, filled in slots.items()
            },
            "players": [
                {
                    "id": p["id"], "name": p["name"], "team": p["team"],
                    "opponent": p.get("opponent"), "position": p["position"],
                    "salary": p["salary"], "projected_fpts": p["projected_fpts"],
                    "ownership_pct": p.get("ownership_pct") or 0,
                    "nflverse_id": p.get("nflverse_id"),
                }
                for p in players
            ],
        })

    return {"accepted": accepted, "rejected": rejected}
