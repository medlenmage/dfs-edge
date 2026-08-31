"""
DraftKings entries CSV parsing -- reads a real contest's baseline (how
many entries it holds, what each one costs) from the same "bulk
entries" export/upload file DK's own site gives you, so
services/contest.py's build_dk_entries_simulated() can mirror and
simulate that real contest's whole field.

DK's bulk-entries template packs two unrelated tables into one CSV.
Your own contest entries start at column 0: Entry ID, Contest Name,
Contest ID, Entry Fee, then 10 roster-slot cells (P, P, C, 1B, 2B, 3B,
SS, OF, OF, OF), blank for a still-empty reservation. A filled cell
comes in either of two forms depending on where the file came from --
the bare numeric id a fresh DK export writes, or the "Player Name
(dk_id)" display form a re-downloaded (or app-filled) file carries. See
_pick_id(). The full player pool for the slate is crammed a few columns further
right, one player per row, unrelated to whichever entry happens to
share that row number -- and it is NOT incidental. It is the only
authority on which numeric ids this draft group accepts, which is why
`parse_player_pool()` reads it: DraftKings' own instructions inside the
file say "Use data from the Name+ID column or the ID column", and those
ids are per-draft-group DRAFTABLE ids (43983736), not the stable player
ids a DK salary CSV's own ID column may carry (110839). Matching the
two up by name against the file's own pool is what makes an import work
-- and what makes a filled reupload one DraftKings will actually
accept.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any

from app.cache import get, put
from app.services.optimizer import ROSTER_SIZE
from app.services.player_match import normalize_name

_CACHE_PREFIX = "dk_entries"
# Same window as salaries/projections uploads -- long enough to survive
# closing the tab, short enough not to linger past the slate it's for.
_TTL = 60 * 60 * 24 * 7

_ID_RE = re.compile(r"\((\d+)\)\s*$")
_BARE_ID_RE = re.compile(r"^\d+$")


def _pick_id(cell: str) -> str | None:
    """
    The DK player id out of one roster cell, or None for a blank
    reservation slot.

    DraftKings writes this cell two different ways in the same file
    format, and both are real. A freshly EXPORTED entries file holds the
    bare numeric id ("43983384"); an entries file that has been through
    DK's own upload/download round trip -- and the file this app's own
    entry filler produces -- holds the display form, "Player Name
    (43983384)". Reading only the second form made every real export
    look like a file of blank reservations, which is exactly how it was
    reported.

    A bare id is accepted only when the cell is ENTIRELY digits. Anything
    else (a name with no id, a stray note) is not a pick, and guessing at
    one would silently roster the wrong player.
    """
    cell = (cell or "").strip()
    if not cell:
        return None
    m = _ID_RE.search(cell)
    if m:
        return m.group(1)
    return cell if _BARE_ID_RE.match(cell) else None


def parse_entries_csv(text: str) -> list[dict[str, Any]]:
    """
    Parse just the entries table (not the embedded player pool) into
    one dict per entry: {entry_id, contest_name, contest_id, entry_fee,
    picks} -- `picks` is a list of ROSTER_SIZE DK player ids (or None
    for a still-blank reservation slot), in the same fixed roster
    order (P, P, C, 1B, 2B, 3B, SS, OF, OF, OF) as everywhere else in
    this app.

    Stops treating a row as an entry once its Entry ID column is
    blank -- past the last real entry, only the embedded player-pool
    table continues on that row, and it has nothing in the entries
    columns at all.
    """
    all_rows = list(csv.reader(io.StringIO(text)))
    if not all_rows:
        return []
    header = all_rows[0]
    try:
        entry_id_i = header.index("Entry ID")
        contest_name_i = header.index("Contest Name")
        contest_id_i = header.index("Contest ID")
        entry_fee_i = header.index("Entry Fee")
    except ValueError:
        return []

    slot_start = entry_fee_i + 1
    entries: list[dict[str, Any]] = []
    for row in all_rows[1:]:
        if len(row) <= entry_id_i or not row[entry_id_i].strip():
            continue

        entry_fee_raw = row[entry_fee_i].strip().lstrip("$") if len(row) > entry_fee_i else ""
        try:
            entry_fee = float(entry_fee_raw) if entry_fee_raw else None
        except ValueError:
            entry_fee = None

        picks: list[str | None] = [
            _pick_id(row[i] if i < len(row) else "")
            for i in range(slot_start, slot_start + ROSTER_SIZE)
        ]

        entries.append(
            {
                "entry_id": row[entry_id_i].strip(),
                "contest_name": row[contest_name_i].strip() if len(row) > contest_name_i else "",
                "contest_id": row[contest_id_i].strip() if len(row) > contest_id_i else "",
                "entry_fee": entry_fee,
                "picks": picks,
            }
        )
    return entries


_POOL_HEADER = "Position"


def parse_player_pool(text: str) -> list[dict[str, Any]]:
    """
    The player pool embedded in the right-hand columns of an entries
    file: one row per draftable player in THIS draft group, carrying the
    id its roster cells actually use.

    Found by locating the row whose cells contain the pool's own header
    (Position / Name + ID / Name / ID / Roster Position / Salary / Game
    Info / TeamAbbrev / AvgPointsPerGame) rather than by a fixed column
    offset, because how far right the block starts depends on how many
    instruction columns DK happened to write.

    Returns [] when the file carries no pool -- older exports and
    hand-made templates don't, and callers fall back to whatever ids
    they already had.
    """
    rows = list(csv.reader(io.StringIO(text)))
    start_row = start_col = None
    for r_i, row in enumerate(rows):
        for c_i, cell in enumerate(row):
            if cell.strip() == _POOL_HEADER and {"Name", "ID"} <= {
                c.strip() for c in row[c_i : c_i + 9]
            }:
                start_row, start_col = r_i, c_i
                break
        if start_row is not None:
            break
    if start_row is None:
        return []

    header = [c.strip() for c in rows[start_row][start_col : start_col + 9]]
    out: list[dict[str, Any]] = []
    for row in rows[start_row + 1 :]:
        cells = row[start_col : start_col + 9]
        if len(cells) < len(header):
            continue
        rec = dict(zip(header, (c.strip() for c in cells)))
        if not rec.get("ID") or not rec.get("Name"):
            continue
        try:
            salary = int(float(rec.get("Salary") or 0))
        except ValueError:
            salary = 0
        out.append(
            {
                "dk_id": rec["ID"],
                "name": rec["Name"],
                "normalized_name": normalize_name(rec["Name"]),
                "team": (rec.get("TeamAbbrev") or "").strip().upper(),
                "position": rec.get("Position") or "",
                "roster_position": rec.get("Roster Position") or "",
                "salary": salary,
            }
        )
    return out


def pool_lookup(pool_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Index a parsed pool both ways: by the draftable id its roster cells
    use, and by normalized name.

    The name index drops any name that is ambiguous within the draft
    group rather than picking one, so a collision surfaces as an
    unmatched player instead of a silently wrong roster.
    """
    by_id = {r["dk_id"]: r for r in pool_rows}
    hits: dict[str, list[dict[str, Any]]] = {}
    for r in pool_rows:
        hits.setdefault(r["normalized_name"], []).append(r)
    return {
        "by_dk_id": by_id,
        "by_name": {n: v[0] for n, v in hits.items() if len(v) == 1},
        "ambiguous_names": {n for n, v in hits.items() if len(v) > 1},
        "rows": pool_rows,
    }


def contest_summary(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    The distinct contests represented in a parsed entries batch, for a
    contest picker -- {contest_id, contest_name, entry_fee,
    num_entries, num_filled}. A single entries export can span more
    than one contest if you've entered several different ones the same
    day, and each one has its own entry fee.
    """
    seen: dict[str, dict[str, Any]] = {}
    for e in entries:
        cid = e["contest_id"]
        if not cid:
            continue
        row = seen.setdefault(
            cid,
            {
                "contest_id": cid,
                "contest_name": e["contest_name"],
                "entry_fee": e["entry_fee"],
                "num_entries": 0,
                "num_filled": 0,
            },
        )
        row["num_entries"] += 1
        if all(p is not None for p in e["picks"]):
            row["num_filled"] += 1
    return list(seen.values())


def store(day: str, text: str) -> None:
    put(f"{_CACHE_PREFIX}:{day}", text, _TTL)


def load(day: str) -> str | None:
    return get(f"{_CACHE_PREFIX}:{day}")
