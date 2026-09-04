"""
Bulk-archive every real DK contest-standings export sitting in the local
"Contest Data" folder.

The app already ingests these one at a time through POST
/api/mlb/contest-results, which is the right shape for the day-to-day
habit of uploading last night's contest. It is the wrong shape for
loading three weeks of accumulated history in one pass, which is what a
calibration run needs -- hence this script, run by hand, same convention
as the other backtest/migrate scripts here.

The date comes out of the filename, because DK's export does not contain
one. Two layouts show up in real files -- MMDDYYYY and YYYYMMDD -- and a
handful have a stray digit from a manual rename, so the parser checks
that whatever it extracts is a real date near the season rather than
trusting position alone. A file whose date can't be read is REPORTED and
skipped, never guessed at: archiving a contest under the wrong date
would silently poison every calibration that reads it afterwards.

The contest folder is gitignored on purpose (real money, real handles)
and stays that way -- this only ever reads it.

Usage (from backend/):
    .venv/Scripts/python.exe -m scripts.ingest_contest_archive [--dry-run]
"""

from __future__ import annotations

import asyncio
import re
import sys
import zipfile
from datetime import date as date_cls
from pathlib import Path
from typing import Any

from app import history_db
from app.services import contest_results

CONTEST_DIR = Path(__file__).resolve().parents[2] / "Contest Data"

# Real files run 2026-08-12 to 2026-09-03. A parsed date outside a
# generous window around that is a misread, not a real contest.
_EARLIEST = date_cls(2026, 1, 1)
_LATEST = date_cls(2027, 12, 31)


def _candidate_dates(stem: str) -> list[date_cls]:
    """Every plausible date reading of a filename, best first."""
    out: list[date_cls] = []
    for digits in re.findall(r"\d{8,9}", stem):
        # A 9-digit run is a typo'd 8 (one real file reads "082892026");
        # try dropping each position rather than guessing which.
        variants = [digits] if len(digits) == 8 else [
            digits[:i] + digits[i + 1:] for i in range(len(digits))
        ]
        for v in variants:
            for fmt in ("%m%d%Y", "%Y%m%d"):
                try:
                    parsed = date_cls(
                        *(
                            (int(v[4:]), int(v[:2]), int(v[2:4]))
                            if fmt == "%m%d%Y"
                            else (int(v[:4]), int(v[4:6]), int(v[6:]))
                        )
                    )
                except ValueError:
                    continue
                if _EARLIEST <= parsed <= _LATEST and parsed not in out:
                    out.append(parsed)
    return out


def _contest_name(stem: str) -> str:
    """The filename minus its date run, which is how a human names it."""
    return re.sub(r"\s*\d{8,9}\s*", " ", stem).strip(" -_") or "Contest"


def _read(path: Path) -> str:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            member = next((m for m in z.namelist() if m.lower().endswith(".csv")), None)
            if not member:
                raise ValueError("no CSV inside the zip")
            return contest_results.extract_csv_text(z.read(member))
    return contest_results.extract_csv_text(path.read_bytes())


async def main() -> None:
    dry_run = "--dry-run" in sys.argv
    if not CONTEST_DIR.is_dir():
        print(f"No contest folder at {CONTEST_DIR}")
        return

    files = sorted(
        p for p in CONTEST_DIR.iterdir()
        if p.suffix.lower() in (".zip", ".csv") and not p.name.startswith("~")
    )
    print(f"{len(files)} contest files in {CONTEST_DIR}\n")

    ok = skipped = 0
    per_date: dict[str, int] = {}
    for path in files:
        dates = _candidate_dates(path.stem)
        if not dates:
            print(f"  SKIP  {path.name}\n        -> no readable date in the filename")
            skipped += 1
            continue
        day = dates[0].isoformat()
        name = _contest_name(path.stem)

        try:
            parsed = contest_results.parse_contest_standings(_read(path))
        except Exception as exc:  # noqa: BLE001 -- one bad file must not stop the run
            print(f"  SKIP  {path.name}\n        -> {type(exc).__name__}: {exc}")
            skipped += 1
            continue

        entries = parsed["entries"]
        pool = parsed["player_pool"]
        if not entries or not pool:
            print(f"  SKIP  {path.name}\n        -> parsed to {len(entries)} entries / {len(pool)} players")
            skipped += 1
            continue

        print(f"  {day}  {len(entries):6} entries  {len(pool):4} players   {name[:52]}")
        per_date[day] = per_date.get(day, 0) + 1
        ok += 1
        if not dry_run:
            await history_db.archive_contest_results(
                day, f"{day}:{name}", name, pool, field_size=len(entries)
            )

    print(f"\n{ok} archived{' (DRY RUN -- nothing written)' if dry_run else ''}, {skipped} skipped")
    print(f"{len(per_date)} distinct dates: {sorted(per_date)}")


if __name__ == "__main__":
    asyncio.run(main())
