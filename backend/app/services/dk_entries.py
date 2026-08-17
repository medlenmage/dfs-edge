"""
DraftKings entries CSV parsing -- for simulating lineups you've
actually built (or reserved) in a real contest, as opposed to
services/contest.py's synthetic entries.

DK's bulk-entries export/upload template packs two unrelated tables
into one CSV. Your own contest entries start at column 0: Entry ID,
Contest Name, Contest ID, Entry Fee, then 10 roster-slot cells (P, P,
C, 1B, 2B, 3B, SS, OF, OF, OF) holding "Player Name (dk_id)" once
filled in, blank for a still-empty reservation. The full player pool
for the slate -- the same shape salaries.parse_dk_csv() already parses
-- is crammed a few columns further right, one player per row,
unrelated to whichever entry happens to share that row number. Once
the real entries run out, only that player-pool table continues, with
every entries-side column blank.

Matching a roster cell's player back to this app's own internal player
id goes through DK's own numeric player id -- present both in each
roster cell ("Name (43854626)") and in the embedded player-pool
table's own ID column -- rather than fuzzy name matching, since DK's
numeric id has no ambiguity the way names do. The remaining hop, from
that DK player-pool row to this app's own MLB Stats API-keyed player
pool, still goes through player_match.match() (DK's id has no MLB
Stats API equivalent), same as salaries.py already does.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any

from app.cache import get, put
from app.services.optimizer import ROSTER_SIZE, SLOT_REQUIREMENTS, build_player_pool
from app.services.player_match import normalize_name, normalize_team
from app.services.salaries import parse_dk_csv

_CACHE_PREFIX = "dk_entries"
# Same window as salaries/projections uploads -- long enough to survive
# closing the tab, short enough not to linger past the slate it's for.
_TTL = 60 * 60 * 24 * 7

_ID_RE = re.compile(r"\((\d+)\)\s*$")


def _roster_slot_labels() -> list[str]:
    labels: list[str] = []
    for slot, count in SLOT_REQUIREMENTS.items():
        labels.extend([slot] * count)
    return labels


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


def resolve_entries(
    text: str, slate: dict[str, Any], *, contest_id: str | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Parse an entries CSV and resolve each entry's roster-cell DK ids
    into this app's own player pool (via optimizer.build_player_pool()),
    producing lineups in the same flat `players` shape contest.py's own
    entries already use -- ready to hand straight to
    variance.simulate_batch() / contest.evaluate_batch_simulated().

    `contest_id`, if given, restricts to just that contest's entries
    (see contest_summary() for the picker this feeds). Returns
    (resolved_entries, warnings) -- an entry with any still-blank or
    unresolvable slot (a player traded/scratched since the file was
    exported, or not part of this app's currently loaded salary/
    projections upload) is skipped rather than silently dropped, since
    "why did my entry count shrink" deserves an explanation, the same
    reasoning build_contest_entries's own `note` field already follows.
    """
    raw_entries = parse_entries_csv(text)
    if contest_id is not None:
        raw_entries = [e for e in raw_entries if e["contest_id"] == contest_id]
    if not raw_entries:
        return [], []

    pool_rows = parse_dk_csv(text)  # the same file's own embedded player-pool table
    by_dk_id = {r["dk_id"]: r for r in pool_rows if r.get("dk_id")}

    slate_pool = build_player_pool(slate)
    by_name_team = {(normalize_team(p["team"]), normalize_name(p["name"])): p for p in slate_pool}

    slot_labels = _roster_slot_labels()
    resolved: list[dict[str, Any]] = []
    warnings: list[str] = []
    for e in raw_entries:
        players: list[dict[str, Any]] = []
        problem: str | None = None
        for slot, dk_id in zip(slot_labels, e["picks"]):
            if dk_id is None:
                problem = "has an empty roster slot (a reservation with no lineup set yet)"
                break
            dk_row = by_dk_id.get(dk_id)
            if dk_row is None:
                problem = f"references a player id ({dk_id}) not found in this file's own player pool"
                break
            internal = by_name_team.get((normalize_team(dk_row["team"]), dk_row["normalized_name"]))
            if internal is None:
                problem = f"'{dk_row['name']}' isn't in today's loaded salary/projections pool"
                break
            players.append(
                {
                    "id": internal["id"],
                    "name": internal["name"],
                    "team": internal["team"],
                    "salary": internal["salary"],
                    "projected_fpts": internal["projected_fpts"],
                    "ownership_pct": internal["ownership_pct"],
                }
            )
        if problem is not None:
            warnings.append(f"Entry {e['entry_id']} skipped -- {problem}.")
            continue
        resolved.append(
            {
                "entry_id": e["entry_id"],
                "contest_name": e["contest_name"],
                "contest_id": e["contest_id"],
                "entry_fee": e["entry_fee"],
                "salary_used": sum(p["salary"] for p in players),
                "projected_points": round(sum(p["projected_fpts"] for p in players), 2),
                "total_ownership_pct": round(sum(p["ownership_pct"] for p in players), 1),
                "players": players,
            }
        )
    return resolved, warnings


def store(day: str, text: str) -> None:
    put(f"{_CACHE_PREFIX}:{day}", text, _TTL)


def load(day: str) -> str | None:
    return get(f"{_CACHE_PREFIX}:{day}")
