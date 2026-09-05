"""
Real post-contest DraftKings results -- the "contest-standings" export
DK gives you AFTER a contest completes, a different file from the
pre-contest salary CSV (services/salaries.py) or the bulk-entries
upload template (services/dk_entries.py). It packs two unrelated
tables into one CSV the same way DK's other exports do: the entries
table (one row per contest entry -- rank, entry id, entry name,
points) and, further right on the same rows, a player-pool table (one
row per real drafted player -- name, roster position, real final
%Drafted ownership, real actual FPTS scored).

This is genuine market ground truth this app never had access to
before -- real ownership as the field actually rostered (not
RotoWire's number or this app's own heuristic) and real final DK
points, both tied to one specific real contest. Parsing it is step
one; history_db.archive_contest_results() is what makes it
permanently useful.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from typing import Any

from app.services.player_match import normalize_name


# The 10 DK Classic MLB roster slots, as they appear as literal tokens
# inside the standings export's own `Lineup` column.
_SLOT_TOKENS = ("P", "C", "1B", "2B", "3B", "SS", "OF")

# What a complete, valid Classic lineup must contain -- used to reject a
# mis-parse rather than archive a wrong one (see parse_lineup).
_EXPECTED_SLOTS = {"P": 2, "C": 1, "1B": 1, "2B": 1, "3B": 1, "SS": 1, "OF": 3}

_SLOT_RE = re.compile(r"\b(" + "|".join(sorted(_SLOT_TOKENS, key=len, reverse=True)) + r")\b")

# The same thing for DK Classic NFL: 9 slots. Note FLEX is a real roster
# slot here rather than a way of describing eligibility, so it turns up
# in the export as its own token alongside the position it was filled by.
_NFL_EXPECTED_SLOTS = {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "FLEX": 1, "DST": 1}
_NFL_SLOT_RE = re.compile(
    r"\b(" + "|".join(sorted(_NFL_EXPECTED_SLOTS, key=len, reverse=True)) + r")\b"
)

# Keyed by sport so one parser serves both boards. Nothing below this
# point is baseball-specific any more; adding a sport is adding a row.
_SPORT_SLOTS = {
    "MLB": (_SLOT_RE, _EXPECTED_SLOTS),
    "NFL": (_NFL_SLOT_RE, _NFL_EXPECTED_SLOTS),
}


def detect_sport(raw: str) -> str:
    """
    Which sport an entry's `Lineup` cell belongs to.

    Decided on which slot vocabulary actually parses into a COMPLETE
    roster rather than on counting token hits. The two vocabularies
    overlap enough in practice (a stray "C" or "TE" inside a name) that
    guessing on hit-count would be fragile, whereas "does this yield a
    legal roster" is unambiguous -- parse_lineup already refuses to
    return a partial one.
    """
    for sport in _SPORT_SLOTS:
        if parse_lineup(raw, sport=sport) is not None:
            return sport
    return "MLB"


def parse_lineup(raw: str, sport: str = "MLB") -> list[dict[str, str]] | None:
    """
    One entry's `Lineup` cell -> [{slot, name, normalized_name}, ...].

    DK packs a whole roster into a single space-delimited cell as
    `1B Pete Alonso 2B Jackson Holliday 3B Isaac Paredes ...`, with no
    delimiter between a player's name and the next slot token. Splitting
    on the slot tokens themselves is the only way in, and it's why this
    format is generally described as resisting naive parsing.

    Returns None -- never a partial roster -- if what comes back isn't
    exactly a legal Classic lineup (2 P, 1 C, 1 1B, 1 2B, 1 3B, 1 SS,
    3 OF). A real player surname that happened to collide with a slot
    token would corrupt the split silently otherwise, and a wrong
    lineup archived as fact is worse than a skipped one.
    """
    if not raw or not raw.strip():
        return None

    slot_re, expected = _SPORT_SLOTS.get(sport, _SPORT_SLOTS["MLB"])
    matches = list(slot_re.finditer(raw))
    if not matches:
        return None

    slots: list[dict[str, str]] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        name = raw[m.end():end].strip()
        if not name:
            return None
        slots.append({"slot": m.group(1), "name": name, "normalized_name": normalize_name(name)})

    counts: dict[str, int] = {}
    for s in slots:
        counts[s["slot"]] = counts.get(s["slot"], 0) + 1
    if counts != expected:
        return None
    return slots


def stack_distribution(
    lineups: list[list[dict[str, str]]], team_by_name: dict[str, str]
) -> dict[str, dict[int, int]]:
    """
    team -> {stack_size: how many entries used exactly that many of
    that team's HITTERS}.

    This is the quantity real MLB field behaviour is actually organised
    around, and the direct training/evaluation target for a team-stack model:
    the field picks a team to stack, then picks bats. Pitchers are
    excluded from the count because a rostered SP is not part of a
    team's offensive stack (and DK's own 5-hitters-per-team cap counts
    only hitters).

    An entry using none of a team's hitters contributes to that team's
    size-0 bucket, so each team's counts sum to the field size and the
    result reads directly as a distribution.
    """
    per_team: dict[str, dict[int, int]] = {}
    teams = set(team_by_name.values())
    for team in teams:
        per_team[team] = {}

    for slots in lineups:
        counts: dict[str, int] = {}
        for s in slots:
            if s["slot"] == "P":
                continue
            team = team_by_name.get(s["normalized_name"])
            if team:
                counts[team] = counts.get(team, 0) + 1
        for team in teams:
            k = counts.get(team, 0)
            per_team[team][k] = per_team[team].get(k, 0) + 1
    return per_team


def _looks_like_zip(raw: bytes) -> bool:
    return raw[:4] == b"PK\x03\x04"


def extract_csv_text(raw: bytes) -> str:
    """
    Unwrap a real DK contest-standings .zip download to its one CSV,
    or just decode the bytes directly if it's already a plain CSV --
    DK's own "download standings" button gives you a zip, but a user
    who's already extracted it themselves should be able to upload the
    CSV directly too.
    """
    if _looks_like_zip(raw):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
                if not names:
                    raise ValueError("That zip file doesn't contain a CSV.")
                raw = zf.read(names[0])
        except zipfile.BadZipFile as exc:
            raise ValueError(f"That doesn't look like a valid zip file: {exc}") from exc
    return raw.decode("utf-8-sig")


def parse_contest_standings(text: str) -> dict[str, list[dict[str, Any]]]:
    """
    Parses both tables out of one contest-standings CSV: `entries`
    (rank/entry_id/entry_name/points, one row per real contest entry --
    field size is just len(entries)) and `player_pool` (name/position/
    ownership_pct/actual_fpts, one row per real drafted player).
    Missing columns degrade gracefully rather than raising -- DK's
    export shape has stayed stable so far, but a header rename
    shouldn't take the whole upload down.
    """
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return {"entries": [], "player_pool": []}
    header = rows[0]

    def idx(name: str) -> int | None:
        try:
            return header.index(name)
        except ValueError:
            return None

    rank_i, entry_id_i = idx("Rank"), idx("EntryId")
    entry_name_i, points_i, lineup_i = idx("EntryName"), idx("Points"), idx("Lineup")
    player_i, pos_i = idx("Player"), idx("Roster Position")
    pct_i, fpts_i = idx("%Drafted"), idx("FPTS")

    def cell(row: list[str], i: int | None) -> str:
        return row[i].strip() if i is not None and len(row) > i else ""

    entries: list[dict[str, Any]] = []
    player_pool: list[dict[str, Any]] = []
    for row in rows[1:]:
        rank_raw = cell(row, rank_i)
        if rank_i is not None and rank_raw:
            try:
                rank = int(rank_raw)
            except ValueError:
                rank = None
            points_raw = cell(row, points_i)
            try:
                points = float(points_raw) if points_raw else None
            except ValueError:
                points = None
            entries.append(
                {
                    "rank": rank,
                    "entry_id": cell(row, entry_id_i),
                    "entry_name": cell(row, entry_name_i),
                    "points": points,
                    # The full roster this entry actually used. None when
                    # the column is absent or doesn't parse to a legal
                    # lineup -- callers that need joint lineup structure
                    # (stack sizes, duplication) must skip those rather
                    # than treat a partial roster as real.
                    "lineup": parse_lineup(cell(row, lineup_i)),
                }
            )

        name = cell(row, player_i)
        if player_i is not None and name:
            pct_raw = cell(row, pct_i).rstrip("%")
            try:
                ownership_pct = float(pct_raw) if pct_raw else None
            except ValueError:
                ownership_pct = None
            fpts_raw = cell(row, fpts_i)
            try:
                actual_fpts = float(fpts_raw) if fpts_raw else None
            except ValueError:
                actual_fpts = None
            player_pool.append(
                {
                    "name": name,
                    "normalized_name": normalize_name(name),
                    "position": cell(row, pos_i),
                    "ownership_pct": ownership_pct,
                    "actual_fpts": actual_fpts,
                }
            )

    return {"entries": entries, "player_pool": player_pool}


def find_my_entry(
    entries: list[dict[str, Any]],
    *,
    entry_id: str | None = None,
    handle: str | None = None,
) -> dict[str, Any] | None:
    """
    Find the user's own entry -- by exact EntryId if given (unambiguous,
    the reliable option, found in the user's own DK entry history/
    notifications), or a best-effort match of `handle` against
    EntryName's own "handle (rank/total)" format otherwise. A large
    public field routinely has hundreds of overlapping/similar handles
    across different real grinders, so the handle match is a real,
    stated limitation, not a guaranteed identification -- prefer
    entry_id whenever it's known.
    """
    if entry_id:
        return next((e for e in entries if e["entry_id"] == entry_id), None)
    if handle:
        handle_lower = handle.strip().lower()
        for e in entries:
            base = e["entry_name"].split(" (")[0].strip().lower()
            if base == handle_lower:
                return e
    return None


def outcome_percentile(actual_value: float, pool: list[float]) -> float:
    """
    Where a real observed value falls within a modeled outcome pool, as
    a percentile (0-100). Standard "probability integral transform"
    machinery for checking whether a probabilistic model is honest: if
    `pool` is a well-calibrated distribution for what actually happened,
    the real value should land anywhere in [0, 100] with roughly EQUAL
    likelihood across many independent checks -- clustered near 50 means
    the model's spread is too wide (real outcomes always land near the
    middle), clustered near the edges means it's too narrow (real
    outcomes keep landing in the tails), and a systematic skew toward
    one end means the model's mean itself is biased in that direction.
    See calibration_summary() for what that check looks like aggregated
    across many real observations.
    """
    if not pool:
        return 50.0
    below = sum(1 for v in pool if v <= actual_value)
    return round(100 * below / len(pool), 2)


def calibration_summary(percentiles: list[float]) -> dict[str, Any]:
    """
    Aggregates many outcome_percentile() results into a single
    calibration report. For a genuinely well-calibrated model, real
    percentiles should be roughly UNIFORMLY distributed across [0, 100]:
    `mean_percentile` should sit near 50 (no systematic over/under-
    projection bias), and `pct_within_10_90`/`pct_within_25_75` should
    land near 80%/50% respectively (a uniform distribution puts exactly
    that much of its mass inside those bands by construction) -- if real
    outcomes are landing inside the middle band far MORE often than
    that, the model's spread is too wide (overconfident about variance
    existing when it doesn't); far LESS often means the model is too
    narrow (real results keep surprising it in the tails, understating
    real-world variance).
    """
    if not percentiles:
        return {
            "n": 0, "mean_percentile": None,
            "pct_within_10_90": None, "pct_within_25_75": None,
        }
    n = len(percentiles)
    within_10_90 = sum(1 for p in percentiles if 10.0 <= p <= 90.0)
    within_25_75 = sum(1 for p in percentiles if 25.0 <= p <= 75.0)
    return {
        "n": n,
        "mean_percentile": round(sum(percentiles) / n, 2),
        "pct_within_10_90": round(100 * within_10_90 / n, 1),
        "pct_within_25_75": round(100 * within_25_75 / n, 1),
    }
