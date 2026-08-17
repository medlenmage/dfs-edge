"""
DraftKings entries CSV parsing -- reads a real contest's baseline (how
many entries it holds, what each one costs) from the same "bulk
entries" export/upload file DK's own site gives you, so
services/contest.py's build_dk_entries_simulated() can mirror and
simulate that real contest's whole field.

DK's bulk-entries template packs two unrelated tables into one CSV.
Your own contest entries start at column 0: Entry ID, Contest Name,
Contest ID, Entry Fee, then 10 roster-slot cells (P, P, C, 1B, 2B, 3B,
SS, OF, OF, OF) holding "Player Name (dk_id)" once filled in, blank for
a still-empty reservation. The full player pool for the slate is
crammed a few columns further right, one player per row, unrelated to
whichever entry happens to share that row number -- unused here (this
module only needs the entries table itself), but confirmed present in
a real export while building this parser.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any

from app.cache import get, put
from app.services.optimizer import ROSTER_SIZE

_CACHE_PREFIX = "dk_entries"
# Same window as salaries/projections uploads -- long enough to survive
# closing the tab, short enough not to linger past the slate it's for.
_TTL = 60 * 60 * 24 * 7

_ID_RE = re.compile(r"\((\d+)\)\s*$")


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

        picks: list[str | None] = []
        for i in range(slot_start, slot_start + ROSTER_SIZE):
            cell = row[i].strip() if i < len(row) else ""
            m = _ID_RE.search(cell) if cell else None
            picks.append(m.group(1) if m else None)

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
