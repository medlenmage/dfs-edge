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
knows about, so matching is done by normalised name + team. Good enough
for a personal tool; a genuine mismatch (a name DK spells differently,
a very recent trade) just shows no salary for that player instead of
guessing wrong.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from typing import Any

from app.cache import get, put

_CACHE_PREFIX = "salaries"
# A week -- long enough that an upload survives you closing the tab,
# short enough that a stale slate doesn't linger forever.
_TTL = 60 * 60 * 24 * 7

_SUFFIXES = re.compile(r"\b(jr|sr|ii|iii|iv)\b")
_PUNCTUATION = re.compile(r"[.'\-]")
_WHITESPACE = re.compile(r"\s+")


def _strip_accents(name: str) -> str:
    """
    Fold accented characters to their plain-ASCII form (Diaz vs Díaz,
    Munoz vs Muñoz). MLB's Stats API keeps the accent; DraftKings'
    export sometimes does and sometimes doesn't, so both sides need to
    be folded the same way or matching silently fails for a good chunk
    of the league.
    """
    decomposed = unicodedata.normalize("NFKD", name)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_name(name: str) -> str:
    """Lowercase, strip accents/punctuation/generational suffixes, for matching."""
    name = _strip_accents(name.lower())
    name = _PUNCTUATION.sub("", name)
    name = _SUFFIXES.sub("", name)
    return _WHITESPACE.sub(" ", name).strip()


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


def build_lookup(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Index by (team, normalized name) for fast matching against the slate."""
    return {(r["team"], r["normalized_name"]): r for r in rows}


def match(
    lookup: dict[tuple[str, str], dict[str, Any]], name: str, team: str
) -> dict[str, Any] | None:
    return lookup.get(((team or "").upper(), normalize_name(name or "")))


def value_score(edge_score: float | None, salary: int | None) -> float | None:
    """Edge score per $1,000 of salary -- higher is better value."""
    if edge_score is None or not salary:
        return None
    return round(edge_score / (salary / 1000), 2)
