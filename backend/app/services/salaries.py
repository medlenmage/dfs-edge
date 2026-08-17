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
import re
from typing import Any

from app.cache import get, put
from app.services.player_match import build_lookup, match, normalize_name, normalize_team

__all__ = [
    "build_lookup",
    "match",
    "normalize_name",
    "parse_dk_csv",
    "parse_game_info",
    "slate_games",
    "store",
    "load",
    "value_score",
]

_CACHE_PREFIX = "salaries"
# A week -- long enough that an upload survives you closing the tab,
# short enough that a stale slate doesn't linger forever.
_TTL = 60 * 60 * 24 * 7

# DK's "Game Info" column starts with "AWAY@HOME" (e.g. "NYY@BOS
# 08/16/2026 07:05PM ET") -- everything after that (date, time,
# occasionally a live status like "In Progress") isn't needed here.
_GAME_INFO_RE = re.compile(r"^([A-Z]{2,4})@([A-Z]{2,4})\b")


def _find_dk_header_row(all_rows: list[list[str]]) -> int | None:
    """
    DK ships salary data in two different CSV shapes depending on where
    you export from. The player-pool page gives a flat file with the
    real header on row 1. The lineup-builder page's "Export to CSV"
    button gives a wider file: an empty roster-slot template
    ("P,P,C,1B,2B,3B,SS,OF,OF,OF,,Instructions") occupies the first
    several rows/columns, with the actual player table's header --
    Position, Name + ID, Name, ID, Roster Position, Salary, Game Info,
    TeamAbbrev, AvgPointsPerGame -- embedded partway down, offset well
    past column 0. Blindly treating row 1 as the header (what a plain
    `csv.DictReader` does) finds none of those columns in that second
    shape, so every row gets skipped and the whole upload silently
    reports zero players. Scanning for the row containing "Salary" and
    "TeamAbbrev" -- two column names unique enough to DK's export that
    they won't appear elsewhere -- finds the real header regardless of
    which shape or how far down the file it landed.
    """
    for i, row in enumerate(all_rows):
        if "Salary" in row and "TeamAbbrev" in row:
            return i
    return None


def parse_dk_csv(text: str) -> list[dict[str, Any]]:
    """
    Parse a DraftKings 'DKSalaries.csv' export into a flat list.

    Expected columns (DK's standard classic-contest export): Position,
    Name + ID, Name, ID, Roster Position, Salary, Game Info, TeamAbbrev,
    AvgPointsPerGame -- see `_find_dk_header_row` for why this doesn't
    just assume they're on row 1. Rows missing a name or salary are
    skipped rather than raising, since a malformed row shouldn't sink
    the whole upload.
    """
    all_rows = list(csv.reader(io.StringIO(text)))
    header_idx = _find_dk_header_row(all_rows)
    if header_idx is None:
        return []
    header = all_rows[header_idx]

    rows: list[dict[str, Any]] = []
    for data_row in all_rows[header_idx + 1 :]:
        row = dict(zip(header, data_row))
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
                "game_info": (row.get("Game Info") or "").strip(),
                # DK's own numeric player id. Unused for MLB (Stats API's
                # own player id is the authoritative one there) but
                # NFL/DST rows have no equivalent separate roster fetch
                # in this app, so this is their only stable identity.
                "dk_id": (row.get("ID") or "").strip(),
            }
        )
    return rows


def from_rotowire_rows(projection_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Build DK-salary-shaped rows from a RotoWire projections upload that
    already carries a SAL column (RotoWire's player-pool export pulls
    salary straight from DK, so it's the same number a separate DK
    upload would give -- just bundled into one file). Lets a
    projections-only upload seed the salary store too, sparing a
    separate DK CSV when the file already has everything needed. See
    routers/mlb.py's upload_projections for where this gets called,
    and why it only fires when no salary data is loaded yet.

    Two real gaps versus an actual DK export, both handled gracefully
    rather than guessed at: no `game_info` (RotoWire doesn't expose the
    away@home matchup string DK's own export does, so DK-slate
    auto-detection just has nothing to detect until a real DK file is
    uploaded -- same as the state before any salary file exists), and
    no `dk_id` (RotoWire's file has no equivalent to DK's own numeric
    player id, which only matters for NFL's optimizer output, not
    MLB's -- this function is MLB-only for exactly that reason).
    """
    rows: list[dict[str, Any]] = []
    for r in projection_rows:
        salary = r.get("salary")
        if not salary:
            continue
        rows.append(
            {
                "name": r["name"],
                "normalized_name": r["normalized_name"],
                "team": r["team"],
                "position": r["position"],
                "salary": salary,
                "avg_points": None,
                "game_info": "",
                "dk_id": "",
            }
        )
    return rows


def parse_game_info(game_info: str) -> tuple[str, str] | None:
    """
    Pull the (away, home) team codes out of DK's "Game Info" column,
    e.g. "NYY@BOS 08/16/2026 07:05PM ET" -> ("NYY", "BOS"). Returns
    None for anything that doesn't start with that pattern -- DK shows
    non-matchup text here sometimes (a postponed game, "In Progress").
    """
    m = _GAME_INFO_RE.match((game_info or "").strip())
    return (m.group(1), m.group(2)) if m else None


def slate_games(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """
    The distinct games actually represented in a salary upload, derived
    from "Game Info" -- e.g. [{"away": "NYY", "home": "BOS"}, ...].

    This is what "the slate" means: a DraftKings CSV export only
    contains players from the specific games in the contest/slate it
    was exported from, and Game Info is the one column that says
    exactly which games those are (team abbreviations normalised the
    same way matching already does, so DK's "ARI" lines up with MLB's
    own "AZ").
    """
    seen: dict[frozenset[str], dict[str, str]] = {}
    for r in rows:
        parsed = parse_game_info(r.get("game_info") or "")
        if not parsed:
            continue
        away, home = normalize_team(parsed[0]), normalize_team(parsed[1])
        seen.setdefault(frozenset((away, home)), {"away": away, "home": home})
    return list(seen.values())


def store(day: str, rows: list[dict[str, Any]]) -> None:
    put(f"{_CACHE_PREFIX}:{day}", rows, _TTL)


def load(day: str) -> list[dict[str, Any]]:
    return get(f"{_CACHE_PREFIX}:{day}") or []


def value_score(edge_score: float | None, salary: int | None) -> float | None:
    """Edge score per $1,000 of salary -- higher is better value."""
    if edge_score is None or not salary:
        return None
    return round(edge_score / (salary / 1000), 2)
