"""
CSV export for a batch of generated lineups/entries -- for handing a
full batch off to an external process (a Monte Carlo simulator, another
Claude session working from the file, a spreadsheet) rather than
working through the small JSON payload the UI itself renders.

One row per lineup: each roster slot's player name, lineup-level
totals (salary used, stack shape/teams, projected points, ownership%),
and -- when the batch was ranked against a contest field -- the
per-lineup rank/cash/payout estimate too. Handles both shapes already
in this codebase: optimizer.py's lineups (players grouped under
`slots` by roster position) and contest.py's entries (a flat `players`
list already in DK roster order) -- normalized to the same row format
so one function serializes either.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from app.services.optimizer import SLOT_REQUIREMENTS

# ["P1", "P2", "C", "1B", "2B", "3B", "SS", "OF1", "OF2", "OF3"] --
# numbered only for slots DK's roster needs more than one of.
SLOT_LABELS: list[str] = [
    f"{slot}{i + 1}" if count > 1 else slot
    for slot, count in SLOT_REQUIREMENTS.items()
    for i in range(count)
]


def stack_info(lineup: dict[str, Any]) -> tuple[str, str]:
    """
    Derive a lineup's stack shape and stacked teams from its actual
    hitter composition (the 8 non-pitcher roster slots) -- not from any
    intended stack_groups/stack_teams input, so it works for a lineup
    built any way (optimizer.py's explicit stack constraints,
    contest.py's weighted random construction, or anything else),
    since it's purely a property of which teams ended up in the
    roster. A "stack" is any team supplying 2+ of the 8 hitters,
    ordered largest group first (ties broken by first appearance in
    roster order) the way DFS players talk about a primary/secondary/
    tertiary stack -- e.g. 5 Yankees + 3 Braves is stack_type "5-3",
    stack "NYY,ATL". No team supplying 2+ hitters means no stack --
    both values come back as an empty string.
    """
    hitters = players_in_slot_order(lineup)[SLOT_REQUIREMENTS["P"] :]
    teams_seen: list[str] = []
    counts: dict[str, int] = {}
    for p in hitters:
        team = p.get("team")
        if team not in counts:
            teams_seen.append(team)
        counts[team] = counts.get(team, 0) + 1

    groups = [(counts[t], t) for t in teams_seen if counts[t] >= 2]
    groups.sort(key=lambda g: -g[0])

    stack_type = "-".join(str(size) for size, _ in groups)
    stack = ",".join(team for _, team in groups)
    return stack_type, stack


def players_in_slot_order(lineup: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Normalize either lineup shape into a flat list of players in fixed
    DK roster order (P, P, C, 1B, 2B, 3B, SS, OF, OF, OF).

    contest.py's entries already come back as a flat `players` list in
    exactly that order (built by walking the same slot order during
    generation). optimizer.py's lineups group players under `slots` by
    roster position instead (`{"P": [p1, p2], "C": [p1], ...}`) since a
    multi-eligible player's *assigned* slot matters there -- flatten
    that the same way.
    """
    if "players" in lineup:
        return lineup["players"]
    out: list[dict[str, Any]] = []
    for slot in SLOT_REQUIREMENTS:
        out.extend(lineup["slots"].get(slot, []))
    return out


# Two shapes `results` can come in, detected by which key is present on
# the first row -- contest.py's deterministic field-evaluation
# (_evaluate_batch_against_field/evaluate_field) versus its Monte Carlo
# simulation (evaluate_batch_simulated). Distinct column sets since
# they're answering different questions (a single projected-points
# rank/payout estimate vs. a real distribution of simulated outcomes).
_DETERMINISTIC_RESULT_FIELDS = ["estimated_rank", "in_the_money", "estimated_payout"]
_SIMULATED_RESULT_FIELDS = [
    "cash_probability_pct",
    "first_place_pct",
    "top_1pct_pct",
    "top_10pct_pct",
    "expected_payout",
    "payout_p10",
    "payout_p90",
    "roi_pct",
    "simulated_points_mean",
    "simulated_points_p10",
    "simulated_points_p90",
    "simulated_points_floor",
    "simulated_points_ceiling",
]


def lineups_to_csv(
    lineups: list[dict[str, Any]],
    *,
    results: list[dict[str, Any]] | None = None,
) -> str:
    """
    Serialize a batch of lineups/entries to CSV text, one row each.

    `results`, if given, must be the same length as `lineups` and
    index-aligned. Auto-detects which shape it's in: contest.py's
    deterministic field-evaluation (adds
    estimated_rank/in_the_money/estimated_payout) or its Monte Carlo
    simulation (adds cash probability, 1st/top-1%/top-10% rates,
    payout range, ROI%, and simulated points range) -- either way, the
    row order here is whatever order `lineups`/`results` are already
    in, so a caller that wants a particular sort (e.g. highest ROI
    first) should sort before calling this.
    """
    buf = io.StringIO()
    fieldnames = ["lineup_index", "salary_used", "stack_type", "stack", "projected_points", "total_ownership_pct"]
    fieldnames += [f"{label}_name" for label in SLOT_LABELS]
    simulated = bool(results) and "cash_probability_pct" in results[0]
    if results is not None:
        fieldnames += _SIMULATED_RESULT_FIELDS if simulated else _DETERMINISTIC_RESULT_FIELDS

    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()

    for i, lineup in enumerate(lineups):
        players = players_in_slot_order(lineup)
        stack_type, stack = stack_info(lineup)
        row: dict[str, Any] = {
            "lineup_index": i,
            "salary_used": lineup.get("salary_used"),
            # A leading apostrophe is the standard Excel "force this cell to
            # stay text" escape -- without it, Excel's own CSV auto-typing
            # reads a value like "5-3" as a date (March 5th) and displays
            # the wrong thing, even though the raw field is plain text.
            # Excel hides the apostrophe on display; a non-Excel reader
            # sees it literally and should strip it if it cares.
            "stack_type": f"'{stack_type}" if stack_type else "",
            "stack": stack,
            "projected_points": lineup.get("projected_points"),
            "total_ownership_pct": lineup.get("total_ownership_pct"),
        }
        for label, p in zip(SLOT_LABELS, players):
            row[f"{label}_name"] = p.get("name")
        if results is not None:
            r = results[i]
            for field in _SIMULATED_RESULT_FIELDS if simulated else _DETERMINISTIC_RESULT_FIELDS:
                row[field] = r.get(field)
        writer.writerow(row)

    return buf.getvalue()
