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
    fieldnames = ["lineup_index", "salary_used", "projected_points", "total_ownership_pct"]
    for label in SLOT_LABELS:
        fieldnames += [f"{label}_name", f"{label}_team", f"{label}_salary", f"{label}_proj_fpts", f"{label}_own_pct"]
    simulated = bool(results) and "cash_probability_pct" in results[0]
    if results is not None:
        fieldnames += _SIMULATED_RESULT_FIELDS if simulated else _DETERMINISTIC_RESULT_FIELDS

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
            for field in _SIMULATED_RESULT_FIELDS if simulated else _DETERMINISTIC_RESULT_FIELDS:
                row[field] = r.get(field)
        writer.writerow(row)

    return buf.getvalue()
