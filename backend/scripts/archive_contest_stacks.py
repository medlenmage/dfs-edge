"""Extracts real team-stack distributions from DK contest-standings
exports and archives them permanently.

The standings export carries a `Lineup` column holding every entry's
full roster, which the original upload path parsed past and threw away
-- it only ever kept per-player ownership. That column is the only
record of the field's JOINT structure: which players were rostered
TOGETHER. For MLB that's the whole game, because the field picks a team
to stack and then picks bats from it, so a flat per-player ownership
vector structurally cannot express what the field actually did.

Player names are mapped to teams by rebuilding that date's real slate
from the MLB Stats API, which works for any past date -- unlike DK
salaries, which live in a 7-day local cache. So this recovers stack
data for every archived contest date, including ones far too old to
backtest ownership against.

Usage (from backend/), pointed at a directory of .zip/.csv exports:
    .venv/Scripts/python.exe -m scripts.archive_contest_stacks <dir>

Each file's contest date is read from the standings themselves where
possible, otherwise from the trailing date in the filename (DK's own
export names carry it as either 08212026 or 20260821).
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

from app import history_db
from app.services import contest_results, mlb_slate, player_match

_DATE_PATTERNS = (
    (re.compile(r"(\d{4})(\d{2})(\d{2})"), ("y", "m", "d")),   # 20260824
    (re.compile(r"(\d{2})(\d{2})(\d{4})"), ("m", "d", "y")),   # 08212026
)


def _date_from_filename(name: str) -> str | None:
    """DK's own export filenames carry the contest date in one of two
    orderings. Tried longest-first so a 4-digit year is never chopped."""
    for pattern, order in _DATE_PATTERNS:
        for m in pattern.finditer(name):
            parts = dict(zip(order, m.groups()))
            y, mo, d = parts["y"], parts["m"], parts["d"]
            if len(y) == 4 and 2000 < int(y) < 2100 and 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
                return f"{y}-{mo}-{d}"
    return None


def _contest_id_from_csv_name(inner_name: str) -> str | None:
    m = re.search(r"contest-standings-(\d+)", inner_name)
    return m.group(1) if m else None


async def _team_by_name(day: str) -> dict[str, str]:
    """normalized_name -> team abbrev for one real past date. Uses the
    MLB Stats API slate (no DK salary needed), so this works for dates
    whose local salary cache expired long ago."""
    slate = await mlb_slate.build_slate(day, include_hitters=True)
    out: dict[str, str] = {}
    for game in slate.get("games", []):
        for side in ("home", "away"):
            team = game[side]["abbrev"]
            for h in game[side].get("hitters") or []:
                out[player_match.normalize_name(h["name"])] = team
    return out


async def process(path: Path) -> str:
    raw = path.read_bytes()
    if not raw:
        return f"{path.name:<62} SKIPPED (file is empty -- 0 bytes)"

    try:
        text = contest_results.extract_csv_text(raw)
    except Exception as exc:
        return f"{path.name:<62} SKIPPED (unreadable: {exc})"

    parsed = contest_results.parse_contest_standings(text)
    entries = parsed["entries"]
    lineups = [e["lineup"] for e in entries if e.get("lineup")]
    if not lineups:
        return f"{path.name:<62} SKIPPED (no parseable lineups in the export)"

    day = _date_from_filename(path.name)
    if not day:
        return f"{path.name:<62} SKIPPED (no contest date in the filename)"

    # DK's real contest id lives in the CSV's own filename
    # ("contest-standings-194369447.csv") -- inside the zip for a zipped
    # export, or the file's own name for a bare CSV. Matching the
    # already-archived contest_player_results on the same id is what
    # lets the two tables join.
    contest_id = _contest_id_from_csv_name(path.name)
    if not contest_id and contest_results._looks_like_zip(raw):
        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            for inner in z.namelist():
                contest_id = _contest_id_from_csv_name(inner)
                if contest_id:
                    break
    if not contest_id:
        return f"{path.name:<62} SKIPPED (couldn't determine contest id)"

    try:
        team_by_name = await _team_by_name(day)
    except Exception as exc:
        return f"{path.name:<62} SKIPPED (couldn't rebuild {day}'s slate: {exc})"
    if not team_by_name:
        return f"{path.name:<62} SKIPPED (no slate found for {day})"

    dist = contest_results.stack_distribution(lineups, team_by_name)
    # A team nobody ever stacked at any size is all size-0; keep only
    # teams the field actually used, so the archive stays about signal.
    dist = {t: {k: v for k, v in sizes.items() if k > 0} for t, sizes in dist.items()}
    dist = {t: sizes for t, sizes in dist.items() if sizes}

    contest_name = path.stem
    await history_db.archive_contest_stacks(
        day, contest_id, contest_name, dist, field_size=len(lineups)
    )

    top = sorted(
        ((t, sum(c for k, c in s.items() if k >= 4)) for t, s in dist.items()),
        key=lambda kv: -kv[1],
    )[:3]
    unmatched = sum(
        1
        for lu in lineups[:200]
        for s in lu
        if s["slot"] != "P" and s["normalized_name"] not in team_by_name
    )
    top_str = ", ".join(f"{t} {100*c/len(lineups):.0f}%" for t, c in top)
    return (
        f"{path.name:<62} {day}  {len(lineups):>6} entries, {len(dist):>2} teams"
        f"  top 4+ stacks: {top_str}"
        + (f"  [{unmatched} unmatched bats in first 200]" if unmatched else "")
    )


async def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    directory = Path(sys.argv[1])
    files = [p for p in sorted(directory.iterdir()) if p.suffix.lower() in (".zip", ".csv")]
    if not files:
        print(f"No .zip/.csv exports found in {directory}")
        return 1

    for path in files:
        print(await process(path), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
