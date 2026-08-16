"""
RotoWire FPTS/ownership projections.

Same shape as salaries.py -- a manual CSV upload (RotoWire's own player
pool export, not a live feed), cached per date, matched by normalised
name + team (see services/player_match.py).

Deliberately NOT folded into the matchup score. This app's whole design
principle is that every component of the edge score is visible and
arguable -- you can look at a 78 and say "that's mostly park and
weather." RotoWire's FPTS number is somebody else's model with none of
that visibility, so blending it in would quietly undermine the one
thing that makes the score trustworthy. It's surfaced as reference data
instead: what he's projected to score and how heavily he'll be
rostered, for you to weigh against the model's own read yourself.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from app.cache import get, put
from app.services.player_match import build_lookup, match, normalize_name

__all__ = ["build_lookup", "match", "normalize_name", "parse_rotowire_csv", "store", "load"]

_CACHE_PREFIX = "projections"
# A week -- long enough that an upload survives you closing the tab,
# short enough that a stale slate doesn't linger forever.
_TTL = 60 * 60 * 24 * 7


def _f(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def parse_rotowire_csv(text: str) -> list[dict[str, Any]]:
    """
    Parse a RotoWire player-pool export into a flat list.

    Expected columns: PLAYER, RW Pick, TEAM, SAL, POS, VAL, RST%, OPP,
    LINEUP, FPTS, MIN EXP, MAX EXP. Only PLAYER/TEAM/POS/FPTS/RST% are
    used here -- salary is its own upload (services/salaries.py), and
    the rest (RW Pick, VAL, OPP, LINEUP, exposure caps) are RotoWire's
    own lineup-building fields, not projections. Rows missing a name
    are skipped rather than raising.
    """
    rows: list[dict[str, Any]] = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        name = (row.get("PLAYER") or "").strip()
        if not name:
            continue
        rows.append(
            {
                "name": name,
                "normalized_name": normalize_name(name),
                "team": (row.get("TEAM") or "").strip().upper(),
                "position": (row.get("POS") or "").strip(),
                "fpts": _f(row.get("FPTS")),
                "ownership_pct": _f(row.get("RST%")),
            }
        )
    return rows


def store(day: str, rows: list[dict[str, Any]]) -> None:
    put(f"{_CACHE_PREFIX}:{day}", rows, _TTL)


def load(day: str) -> list[dict[str, Any]]:
    return get(f"{_CACHE_PREFIX}:{day}") or []
