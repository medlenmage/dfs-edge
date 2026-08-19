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

A third failure mode matters enough to handle too:

  3. Nickname vs. legal first name. RotoWire, DraftKings, and MLB's own
     Stats API don't consistently agree on which form a player goes by
     (Nick Castellanos vs. Nicholas Castellanos, Mike Trout vs. Michael
     Trout). NICKNAMES canonicalises the first token of a name so both
     forms fold to the same string; extend it if another mismatch turns
     up.

A genuine non-match (a name spelled differently than any of the above
handles, a very recent trade) is handled by the opt-in `fuzzy` fallback
on `match()` -- a same-team-only, small-edit-distance check, deliberately
scoped to one team's roster (a couple dozen players) so it can't cross-
match two different people who happen to share a last name across the
league. Even then, a genuine non-match just means that player shows no
data instead of guessing wrong.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Any

_SUFFIXES = re.compile(r"\b(jr|sr|ii|iii|iv)\b")
_PUNCTUATION = re.compile(r"[.'\-]")
_WHITESPACE = re.compile(r"\s+")

TEAM_ALIASES = {
    "ARI": "AZ",   # Diamondbacks -- MLB uses AZ, most DFS sites use ARI
    "WAS": "WSH",  # Washington -- MLB Nationals and NFL nflverse both use WAS, DFS sites use WSH
    "LAR": "LA",   # Rams -- DFS sites use LAR, nflverse's own code is LA
}

# First-name variants seen across RotoWire/DraftKings/MLB exports for
# real MLB players. Maps a short/nickname form to the canonical form
# both sides get folded to, so it doesn't matter which one a given
# source happens to use.
NICKNAMES = {
    "nick": "nicholas", "mike": "michael", "alex": "alexander", "will": "william",
    "josh": "joshua", "matt": "matthew", "chris": "christopher", "jake": "jacob",
    "zack": "zachary", "zach": "zachary", "danny": "daniel", "dan": "daniel",
    "joe": "joseph", "sam": "samuel", "tommy": "thomas", "tom": "thomas",
    "bobby": "robert", "rob": "robert", "bob": "robert", "ronnie": "ronald",
    "ron": "ronald", "jimmy": "james", "jim": "james", "steve": "steven",
    "andy": "andrew", "drew": "andrew", "tony": "anthony", "vinny": "vincent",
    "vince": "vincent", "eddie": "edward", "ted": "edward", "ken": "kenneth",
    "kenny": "kenneth", "larry": "lawrence", "gabe": "gabriel", "manny": "manuel",
    "nate": "nathan", "pat": "patrick", "rich": "richard", "rick": "richard",
    "ricky": "richard", "charlie": "charles", "chuck": "charles", "cam": "cameron",
}

# Same-team fuzzy fallback only kicks in above this similarity, keeping
# real typos/spelling drift matchable without loosening things enough
# to risk pairing two different players.
_FUZZY_CUTOFF = 0.85


def _strip_accents(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", name)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_name(name: str) -> str:
    """Lowercase, strip accents/punctuation/generational suffixes, fold
    nicknames to their canonical form, for matching."""
    name = _strip_accents((name or "").lower())
    name = _PUNCTUATION.sub("", name)
    name = _SUFFIXES.sub("", name)
    name = _WHITESPACE.sub(" ", name).strip()
    first, sep, rest = name.partition(" ")
    if first in NICKNAMES:
        name = NICKNAMES[first] + sep + rest
    return name


def normalize_team(team: str) -> str:
    team = (team or "").strip().upper()
    return TEAM_ALIASES.get(team, team)


def build_lookup(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Index by (normalized team, normalized name) for fast matching against the slate."""
    return {(normalize_team(r["team"]), r["normalized_name"]): r for r in rows}


def match(
    lookup: dict[tuple[str, str], dict[str, Any]],
    name: str,
    team: str,
    *,
    fuzzy: bool = False,
) -> dict[str, Any] | None:
    """Exact match after normalisation; with fuzzy=True, falls back to the
    closest same-team name above _FUZZY_CUTOFF when nothing matched
    exactly. Scoped to one team's roster on purpose -- a small edit
    distance across the whole league risks pairing two unrelated players
    who share a last name."""
    team_norm = normalize_team(team)
    name_norm = normalize_name(name)
    row = lookup.get((team_norm, name_norm))
    if row is not None or not fuzzy:
        return row

    candidates = {n: r for (t, n), r in lookup.items() if t == team_norm}
    if not candidates:
        return None
    close = difflib.get_close_matches(name_norm, candidates.keys(), n=1, cutoff=_FUZZY_CUTOFF)
    return candidates[close[0]] if close else None


def unmatched(
    rows: list[dict[str, Any]],
    lookup: dict[tuple[str, str], dict[str, Any]],
    *,
    fuzzy: bool = False,
) -> list[str]:
    """Names from `rows` that don't match anything in `lookup` -- lets an
    upload report exactly which players it couldn't line up against the
    other side (salaries vs. projections), instead of silently showing
    no data for them."""
    return [r["name"] for r in rows if match(lookup, r["name"], r["team"], fuzzy=fuzzy) is None]
