"""
Shared name/team matching for third-party CSV uploads (salaries,
projections) against MLB's own player and team data.

Every DFS-adjacent CSV export identifies players by name rather than
MLB's own player id, so matching is inherently fuzzy. Two failure modes
matter enough to handle explicitly rather than just showing "no match":

  1. Accented names. MLB's Stats API keeps them (Yandy Díaz); some
     exports don't (Yandy Diaz). Fold both sides the same way or you
     silently lose every Díaz, Muñoz, Núñez, Peña, Rodríguez...

  2. Team abbreviation dialects. MLB's own abbreviation isn't always
     the one DFS sites use for the same team -- confirmed so far with
     Arizona (MLB: AZ, most DFS sites: ARI). TEAM_ALIASES maps a
     third-party code to MLB's own; extend it if another mismatch
     turns up.

A genuine non-match (a name spelled differently than either of the
above, a very recent trade) just means that player shows no data
instead of guessing wrong -- there's no fuzzy scoring here, only exact
match after normalisation.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

_SUFFIXES = re.compile(r"\b(jr|sr|ii|iii|iv)\b")
_PUNCTUATION = re.compile(r"[.'\-]")
_WHITESPACE = re.compile(r"\s+")

TEAM_ALIASES = {
    "ARI": "AZ",   # Diamondbacks -- MLB uses AZ, most DFS sites use ARI
    "WAS": "WSH",  # Nationals -- some sites use WAS instead of WSH
}


def _strip_accents(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", name)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_name(name: str) -> str:
    """Lowercase, strip accents/punctuation/generational suffixes, for matching."""
    name = _strip_accents((name or "").lower())
    name = _PUNCTUATION.sub("", name)
    name = _SUFFIXES.sub("", name)
    return _WHITESPACE.sub(" ", name).strip()


def normalize_team(team: str) -> str:
    team = (team or "").strip().upper()
    return TEAM_ALIASES.get(team, team)


def build_lookup(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Index by (normalized team, normalized name) for fast matching against the slate."""
    return {(normalize_team(r["team"]), r["normalized_name"]): r for r in rows}


def match(
    lookup: dict[tuple[str, str], dict[str, Any]], name: str, team: str
) -> dict[str, Any] | None:
    return lookup.get((normalize_team(team), normalize_name(name)))
