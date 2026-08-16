"""
DraftKings salary CSV parsing and matching.

WHY THIS IS A MANUAL UPLOAD, NOT ANOTHER FREE API
---------------------------------------------------
Salaries are set per contest, not published as a season-wide feed the
way stats and odds are -- you export a CSV from the specific contest
you're building for on DraftKings and upload it here. It's cached per
date until you upload a new one, so re-uploading only matters when you
switch slates or a late swap changes who's in the player pool.

The score alone tells you who's in a good spot; it says nothing about
whether he's worth his salary. A 72 at $3,800 beats an 80 at $6,200
most nights -- that's what "value" (edge score per $1,000 of salary)
is for.

MATCHING
--------
DraftKings' own player IDs don't correspond to anything MLB's Stats API
knows about, so matching is done by normalised name + team (shared with
the projections upload -- see services/player_match.py). Good enough
for a personal tool; a genuine mismatch just shows no salary for that
player instead of guessing wrong.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from app.cache import get, put
from app.services.player_match import build_lookup, match, normalize_name

__all__ = [
    "build_lookup",
    "match",
    "normalize_name",
    "parse_dk_csv",
    "store",
    "load",
    "value_score",
]

_CACHE_PREFIX = "salaries"
# A week -- long enough that an upload survives you closing the tab,
# short enough that a stale slate doesn't linger forever.
_TTL = 60 * 60 * 24 * 7


def parse_dk_csv(text: str) -> list[dict[str, Any]]:
    """
    Parse a DraftKings 'DKSalaries.csv' export into a flat list.

    Expected columns (DK's standard classic-contest export): Position,
    Name + ID, Name, ID, Roster Position, Salary, Game Info, TeamAbbrev,
    AvgPointsPerGame. Rows missing a name or salary are skipped rather
    than raising, since a malformed row shouldn't sink the whole upload.
    """
    rows: list[dict[str, Any]] = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        name = (row.get("Name") or "").strip()
        salary_raw = row.get("Salary")
        if not name or not salary_raw:
            continue
        try:
            salary = int(float(salary_raw))
        except (TypeError, ValueError):
            continue

        avg_points_raw = row.get("AvgPointsPerGame")
        try:
            avg_points = float(avg_points_raw) if avg_points_raw not in (None, "") else None
        except ValueError:
            avg_points = None

        rows.append(
            {
                "name": name,
                "normalized_name": normalize_name(name),
                "team": (row.get("TeamAbbrev") or "").strip().upper(),
                "position": (row.get("Position") or "").strip(),
                "salary": salary,
                "avg_points": avg_points,
            }
        )
    return rows


def store(day: str, rows: list[dict[str, Any]]) -> None:
    put(f"{_CACHE_PREFIX}:{day}", rows, _TTL)


def load(day: str) -> list[dict[str, Any]]:
    return get(f"{_CACHE_PREFIX}:{day}") or []


def value_score(edge_score: float | None, salary: int | None) -> float | None:
    """Edge score per $1,000 of salary -- higher is better value."""
    if edge_score is None or not salary:
        return None
    return round(edge_score / (salary / 1000), 2)
