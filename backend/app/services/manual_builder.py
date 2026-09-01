"""
Build lineups by hand -- or hand the slate to another Claude and have
it build them for you.

Two halves, and they are deliberately symmetric:

  1. `slate_brief()` renders everything needed to build a legal DK
     Classic MLB lineup into one self-contained block of markdown: the
     roster and cap rules, this app's own process rules, and every
     draftable player grouped by roster slot with salary, projection,
     ownership and batting order. Paste it into any Claude -- one with
     no access to this machine, no tools, no context -- and it has
     enough to do the job.

  2. `parse_lineups()` reads what comes back. It is deliberately
     FORGIVING about format (numbered lists, CSV rows, "P: Name" slot
     labels, blank-line-separated blocks) and deliberately STRICT about
     content -- the names go through the same validation every other
     intake path uses (services/lineup_intake.py), so a manual lineup
     is legal by the same standard as an optimizer one or a DK export,
     or it comes back rejected with the reason.

Why format-forgiving and content-strict: a model asked for ten names
will produce them in whatever shape its own prose habits favour, and
rejecting a good lineup over a stray bullet character wastes a round
trip. Rejecting an ILLEGAL lineup is never a waste, because entering
one costs real money.

The output feeds the same per-day pool the optimizer and the DK
entries import feed, so a hand-built lineup reaches the simulator, the
build audit and the daily brief by exactly the same route as any other.
"""

from __future__ import annotations

import re
from typing import Any

from app.data import process_rules as rules
from app.services.optimizer import SALARY_CAP, SLOT_REQUIREMENTS

# Slot order and labels a human (or a model) reads naturally, as
# opposed to the flat index order the wire format uses.
_SLOT_LABELS = {
    "P": "Pitchers (2)",
    "C": "Catcher (1)",
    "1B": "First base (1)",
    "2B": "Second base (1)",
    "3B": "Third base (1)",
    "SS": "Shortstop (1)",
    "OF": "Outfield (3)",
}

# Enough of each slot to build from without pasting the whole board.
# Depth matters more where the roster needs more bodies.
_DEPTH = {"P": 30, "C": 18, "1B": 18, "2B": 18, "3B": 18, "SS": 18, "OF": 45}


def _fmt_player(p: dict[str, Any]) -> str:
    order = p.get("batting_order")
    bits = [
        f"{p['name']} ({p['team']}",
        f"vs {p['opponent']})" if p.get("opponent") else ")",
    ]
    head = " ".join(bits).replace(" )", ")")
    own = p.get("ownership_pct")
    return (
        f"  {head}  ${p['salary']:,}  {p['projected_fpts']:.1f} pts"
        + (f"  {own:.0f}% own" if own else "")
        + (f"  bats {order}" if order else "")
    )


def slate_brief(
    pool: list[dict[str, Any]],
    *,
    date: str,
    games: list[dict[str, Any]] | None = None,
    include_rules: bool = True,
) -> str:
    """
    The whole slate as one pasteable brief.

    `pool` is `optimizer.build_player_pool()`'s output -- the same pool
    every builder in this app draws from, so a lineup built off this
    brief can never reference a player the app would then reject as
    unavailable.
    """
    lines = [
        f"# DraftKings Classic MLB slate — {date}",
        "",
        "You are building DFS lineups from the board below. Everything you need is here.",
        "",
        "## Roster and salary rules",
        "",
        f"- Exactly {sum(SLOT_REQUIREMENTS.values())} players: "
        + ", ".join(f"{n} {slot}" for slot, n in SLOT_REQUIREMENTS.items()),
        f"- Total salary must not exceed ${SALARY_CAP:,}. Spend within ${rules.SALARY_UNUSED_MAX} "
        "of it — unused salary is projected points left on the table.",
        "- No player twice in the same lineup.",
        "- A player is only eligible at the slots listed under his own heading below. Some "
        "players appear under more than one heading; he still fills only ONE slot.",
    ]

    # Only the games the BOARD actually contains. Deriving this from the
    # pool rather than from the slate's full game list is what keeps the
    # two halves from disagreeing: on a 12-game day with a 7-game DK
    # slate, listing all twelve would invite lineups built around
    # players who are not on the board at all -- the same trap the daily
    # brief hit (see analysis._compact_slate).
    if games:
        board_pks = {p.get("game_pk") for p in pool}
        games = [g for g in games if g.get("game_pk") in board_pks]
    if games:
        lines += ["", f"## Games ({len(games)} on this slate)", ""]
        for g in games:
            away, home = g.get("away") or {}, g.get("home") or {}
            total = (g.get("betting") or {}).get("total")
            lines.append(
                f"- {away.get('abbrev')} @ {home.get('abbrev')}"
                + (f"  O/U {total}" if total else "")
                + (
                    f"  ({away.get('abbrev')} {away.get('implied_runs')} / "
                    f"{home.get('abbrev')} {home.get('implied_runs')} implied)"
                    if away.get("implied_runs") is not None
                    else ""
                )
            )

    if include_rules:
        lines += ["", "## How to build (this account's own process rules)", "", rules.RULES_TEXT]

    lines += ["", "## The player board", ""]
    for slot, label in _SLOT_LABELS.items():
        eligible = [p for p in pool if slot in (p.get("slots") or [])]
        eligible.sort(key=lambda p: -(p.get("projected_fpts") or 0))
        if not eligible:
            continue
        lines += [f"### {label}", ""]
        lines += [_fmt_player(p) for p in eligible[: _DEPTH.get(slot, 20)]]
        if len(eligible) > _DEPTH.get(slot, 20):
            lines.append(f"  …and {len(eligible) - _DEPTH.get(slot, 20)} more, all lower projected.")
        lines.append("")

    lines += [
        "## What to send back",
        "",
        "One lineup per block, ten player names, in roster order "
        "(P, P, C, 1B, 2B, 3B, SS, OF, OF, OF). Separate lineups with a blank line.",
        "Names exactly as written above. No salaries, no commentary inside a block.",
        "",
        "Example:",
        "",
        "```",
        "Lineup 1",
        "Ace Pitcher",
        "Second Pitcher",
        "The Catcher",
        "First Baseman",
        "Second Baseman",
        "Third Baseman",
        "The Shortstop",
        "Outfielder One",
        "Outfielder Two",
        "Outfielder Three",
        "",
        "Lineup 2",
        "…",
        "```",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------- parsing

# A line that is a header/divider rather than a player: "Lineup 3",
# "Entry 2", "---", "```", or a bare number.
_NOISE = re.compile(
    r"^\s*(?:#+\s*)?(?:lineup|entry|build|option|#)\s*\d*\s*[:.)-]?\s*$|^\s*[-=_`*]{2,}\s*$|^\s*\d+\s*$",
    re.IGNORECASE,
)
# Leading list markers and slot labels: "1.", "- ", "* ", "P:", "OF3 -".
_PREFIX = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:\d+\s*[.)]\s*)?(?:(?:P|SP|RP|C|1B|2B|3B|SS|OF)\d?\s*[:\-]\s*)?",
    re.IGNORECASE,
)
# Trailing junk a model likes to add: "($5,400)", "- 12.3 pts", "(NYY)".
_TRAILING = re.compile(r"\s*[\(\[][^)\]]*[\)\]]\s*$|\s*[-–—]\s*[\d.,$].*$|\s*\$[\d,]+.*$")


def _clean(line: str) -> str:
    line = line.strip().strip("|").strip()
    line = _PREFIX.sub("", line, count=1)
    prev = None
    while prev != line:
        prev = line
        line = _TRAILING.sub("", line).strip()
    return line.strip(" ,;\t")


def parse_lineups(text: str, *, roster_size: int) -> list[dict[str, Any]]:
    """
    Free text -> `[{players: [name, ...], label}]`, ready for
    `lineup_intake.intake()`.

    Handles the shapes a model actually produces: blank-line-separated
    blocks, numbered lists, "P: Name" slot labels, CSV rows, and names
    with the salary or team trailing. Nothing here decides whether a
    lineup is LEGAL -- that is intake's job, on the same terms as every
    other source.

    A comma-separated line holding roster_size names is treated as one
    whole lineup, since that is what a CSV row is.
    """
    lineups: list[list[str]] = []
    current: list[str] = []
    # A block well short of a roster is prose, not an attempt at a
    # lineup -- "Here are my thoughts on the slate." should parse to
    # nothing rather than to a one-man lineup. A block that is CLOSE is
    # a real attempt and is kept, so intake can reject it by name ("9
    # players, needs exactly 10") instead of the text silently vanishing
    # and the user wondering what happened to it.
    keep_partial_from = max(2, roster_size // 2)

    def flush(*, partial_ok: bool = True) -> None:
        if current and (len(current) >= keep_partial_from or not partial_ok):
            lineups.append(list(current))
        current.clear()

    for raw in (text or "").splitlines():
        if not raw.strip():
            # A blank line ends a block, but only once it holds a full
            # roster -- models put blank lines inside a list too.
            if len(current) >= roster_size:
                flush()
            continue
        if _NOISE.match(raw):
            if len(current) >= roster_size:
                flush()
            continue

        # A CSV-style row: many names on one line.
        parts = [_clean(c) for c in raw.split(",")] if raw.count(",") >= roster_size - 1 else None
        if parts and len([p for p in parts if p]) >= roster_size:
            flush()
            lineups.append([p for p in parts if p][:roster_size])
            continue

        name = _clean(raw)
        if not name:
            continue
        current.append(name)
        if len(current) == roster_size:
            flush()

    flush()
    return [
        {"players": names, "label": f"manual #{i + 1}"}
        for i, names in enumerate(lineups)
        if names
    ]
