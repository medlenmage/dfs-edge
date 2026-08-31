"""
Entry Manager: fill a real DraftKings bulk-entries template CSV back in
with this app's own generated lineups, producing a file ready to
literally reupload to DraftKings -- no manual copy/paste of 10 player
names per entry.

Reuses dk_entries.py's own parsing conventions (same file, same "Entry
ID, Contest Name, Contest ID, Entry Fee, then 10 roster-slot cells"
layout) but keeps the FULL raw row grid in memory (not just the parsed
facts dk_entries.parse_entries_csv() extracts) so every other column --
including the embedded full-slate player-pool table further right on
each row, which this module never touches -- survives untouched in the
output, byte-for-byte except the slot cells actually filled.

DK's reupload format expects each filled slot cell as "Player Name
(draftable id)" -- the exact same format dk_entries.py's own _ID_RE already
parses back OUT of an already-filled cell. This only works for a
player whose real DK numeric id is known, which requires a DK salary
CSV to have been uploaded for the slate (optimizer.build_player_pool()/
contest.py's generated lineups carry `dk_id` from that upload, empty
string when only a RotoWire file has been loaded) -- a real,
explicitly-checked precondition, not silently guessed at.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from app.services import dk_entries
from app.services.lineup_export import players_in_slot_order
from app.services import player_match
from app.services.optimizer import ROSTER_SIZE


class EntryManagerError(Exception):
    pass


def fill_entries(
    text: str,
    contest_id: str,
    lineups: list[dict[str, Any]],
    *,
    only_blank: bool = True,
) -> tuple[str, dict[str, Any]]:
    """
    Fill `contest_id`'s entry rows in the real DK template `text` with
    `lineups` (this app's own generated lineups/entries, in the order
    to assign them -- typically an already-ranked batch, so entry 1
    gets the strongest lineup), one lineup per row, in file order.

    `only_blank` (default True): only fill rows with no picks yet --
    the safe default, never overwriting an entry you already filled by
    hand or in an earlier run. False fills every row for the contest,
    blank or not.

    Returns (filled_csv_text, summary) where summary is
    {contest_id, filled_count, entry_ids_filled, unfilled_row_count,
    lineups_unused} -- `unfilled_row_count` is target rows left blank
    because there weren't enough lineups; `lineups_unused` is the
    reverse, lineups left over because there weren't enough target
    rows. Neither is an error -- both are just real, worth-surfacing
    facts about the fill.

    Raises EntryManagerError if `contest_id` has no rows in the file
    at all, or if any lineup contains a player with no `dk_id` (can't
    produce a valid "Name (id)" cell for DraftKings to accept without
    one -- names alone aren't enough, since DK's own reupload parser
    matches by id).
    """
    all_rows = list(csv.reader(io.StringIO(text)))
    if not all_rows:
        raise EntryManagerError("That file has no rows to fill.")
    header = all_rows[0]
    try:
        entry_id_i = header.index("Entry ID")
        contest_id_i = header.index("Contest ID")
        entry_fee_i = header.index("Entry Fee")
    except ValueError as exc:
        raise EntryManagerError(
            "That doesn't look like a DraftKings bulk-entries template -- "
            "missing an expected column."
        ) from exc
    slot_start = entry_fee_i + 1

    target_row_idxs: list[int] = []
    for i, row in enumerate(all_rows[1:], start=1):
        if len(row) <= entry_id_i or not row[entry_id_i].strip():
            continue
        if len(row) <= contest_id_i or row[contest_id_i].strip() != contest_id:
            continue
        cells = row[slot_start : slot_start + ROSTER_SIZE]
        is_blank = all(not c.strip() for c in cells)
        if only_blank and not is_blank:
            continue
        target_row_idxs.append(i)

    if not target_row_idxs:
        # Distinguish "no such contest at all" from "every row for this
        # contest is already filled" -- the second is a real, common,
        # non-error state (nothing left to do), not a malformed request.
        any_row_for_contest = any(
            len(row) > contest_id_i and row[contest_id_i].strip() == contest_id
            for row in all_rows[1:]
        )
        if not any_row_for_contest:
            raise EntryManagerError(
                f"No entries found for contest_id '{contest_id}' in that file."
            )

    # The id to write is the one THIS FILE's draft group uses, and the
    # file carries its own player pool saying what those are. That is
    # not the same number as the `dk_id` a lineup is carrying from an
    # uploaded salary CSV: a roster cell holds a per-draft-group
    # DRAFTABLE id (43983736), while a salary file's ID column can hold
    # DraftKings' stable PLAYER id (110839). Writing the second where
    # the first belongs produces a file DraftKings rejects -- confirmed
    # against a real export where the two id spaces did not overlap at
    # all. So the file's own pool wins, matched by name, and the
    # lineup's dk_id is only the fallback for a file that carries no
    # pool (older exports, hand-made templates).
    # Players are matched BY NAME against the pool this file carries,
    # using the same matcher every other third-party join in this app
    # goes through -- accents, generational suffixes and nicknames folded,
    # with a same-team fuzzy fallback for genuine spelling drift. An id
    # join is not usable here: a roster cell's id is a per-draft-group
    # draftable id, which is a different number from the one a DK salary
    # CSV's ID column carries (measured on a real file: no overlap at all
    # across 128 players).
    #
    # The cell is still WRITTEN as "Name (id)", because DraftKings' own
    # instructions inside the file say a bare name is not accepted.
    pool_rows = dk_entries.parse_player_pool(text)
    pool_lookup = player_match.build_lookup(pool_rows) if pool_rows else {}
    # A name-only index too, for the same reason player_match keeps one
    # scoped by team: a lineup does not always carry a team (an
    # optimizer lineup does, a hand-made one may not), and without this
    # a perfectly matchable player falls through to the id fallback.
    # Ambiguous names are excluded rather than resolved by coin flip.
    _name_hits: dict[str, list[dict[str, Any]]] = {}
    for r in pool_rows:
        _name_hits.setdefault(r["normalized_name"], []).append(r)
    by_name_only = {n: v[0] for n, v in _name_hits.items() if len(v) == 1}
    fell_back: list[str] = []

    def _cell(player: dict[str, Any]) -> str | None:
        name, team = player.get("name") or "", player.get("team") or ""
        row = player_match.match(pool_lookup, name, team, fuzzy=True) if pool_lookup else None
        row = row or by_name_only.get(player_match.normalize_name(name))
        if row:
            return f"{row['name']} ({row['dk_id']})"
        # Last resort: the id the lineup itself carries. Right for a file
        # with no embedded pool (older exports, hand-made templates), and
        # a guess for a player whose name simply didn't match -- so it is
        # RECORDED and reported on the summary rather than passing
        # silently, since that is exactly how the wrong-id-space bug went
        # unnoticed.
        if player.get("dk_id"):
            if pool_rows:
                fell_back.append(name or "unknown player")
            return f"{name} ({player['dk_id']})"
        return None

    unresolvable: list[str] = []
    for lu in lineups[: len(target_row_idxs)]:
        for p in players_in_slot_order(lu):
            if _cell(p) is None:
                unresolvable.append(p.get("name") or "unknown player")
    if unresolvable:
        raise EntryManagerError(
            "Can't fill real DK entries -- no DK id for these players, in that file's own "
            "player pool or on the lineup itself (upload a DraftKings salary CSV for this "
            f"slate, not just RotoWire projections): {', '.join(sorted(set(unresolvable)))}."
        )

    entry_ids_filled: list[str] = []
    n_filled = min(len(target_row_idxs), len(lineups))
    for row_idx, lineup in zip(target_row_idxs, lineups):
        row = all_rows[row_idx]
        while len(row) < slot_start + ROSTER_SIZE:
            row.append("")
        for offset, p in enumerate(players_in_slot_order(lineup)):
            row[slot_start + offset] = _cell(p)
        entry_ids_filled.append(row[entry_id_i])

    buf = io.StringIO()
    csv.writer(buf).writerows(all_rows)

    return buf.getvalue(), {
        "contest_id": contest_id,
        # Names that could not be found in this file's own player pool
        # and so were written with the lineup's own id instead. Empty is
        # the expected state; anything here is a cell DraftKings may
        # reject, and it is surfaced rather than left to be discovered on
        # upload.
        "unmatched_in_file_pool": sorted(set(fell_back)),
        "filled_count": n_filled,
        "entry_ids_filled": entry_ids_filled,
        "unfilled_row_count": max(0, len(target_row_idxs) - len(lineups)),
        "lineups_unused": max(0, len(lineups) - len(target_row_idxs)),
    }
