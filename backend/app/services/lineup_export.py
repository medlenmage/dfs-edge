"""
CSV export for a batch of generated lineups/entries -- for handing a
full batch off to an external process (a Monte Carlo simulator, another
Claude session working from the file, a spreadsheet) rather than
working through the small JSON payload the UI itself renders.

One row per lineup. One column-group per DK Classic MLB roster slot
(name, team, salary, projected points, ownership%), plus lineup-level
totals and -- when the batch was ranked against a contest field -- the
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


def lineups_to_csv(
    lineups: list[dict[str, Any]],
    *,
    results: list[dict[str, Any]] | None = None,
) -> str:
    """
    Serialize a batch of lineups/entries to CSV text, one row each.

    `results`, if given, must be the same length as `lineups` and
    index-aligned (as returned by contest.py's field-evaluation
    functions) -- adds estimated_rank/in_the_money/estimated_payout
    columns from testing the batch against a simulated contest field.
    """
    buf = io.StringIO()
    fieldnames = ["lineup_index", "salary_used", "projected_points", "total_ownership_pct"]
    for label in SLOT_LABELS:
        fieldnames += [f"{label}_name", f"{label}_team", f"{label}_salary", f"{label}_proj_fpts", f"{label}_own_pct"]
    if results is not None:
        fieldnames += ["estimated_rank", "in_the_money", "estimated_payout"]

    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()

    for i, lineup in enumerate(lineups):
        players = players_in_slot_order(lineup)
        row: dict[str, Any] = {
            "lineup_index": i,
            "salary_used": lineup.get("salary_used"),
            "projected_points": lineup.get("projected_points"),
            "total_ownership_pct": lineup.get("total_ownership_pct"),
        }
        for label, p in zip(SLOT_LABELS, players):
            row[f"{label}_name"] = p.get("name")
            row[f"{label}_team"] = p.get("team")
            row[f"{label}_salary"] = p.get("salary")
            row[f"{label}_proj_fpts"] = p.get("projected_fpts")
            row[f"{label}_own_pct"] = p.get("ownership_pct")
        if results is not None:
            r = results[i]
            row["estimated_rank"] = r.get("estimated_rank")
            row["in_the_money"] = r.get("in_the_money")
            row["estimated_payout"] = r.get("estimated_payout")
        writer.writerow(row)

    return buf.getvalue()
