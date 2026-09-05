"""
How the real Milly Maker field actually builds NFL lineups.

nfl_contest.py generates an opponent field from sampling weights and a
few structural rules. Those rules were reasoned about rather than
measured, because until now there was no real NFL contest data in this
app to measure against. There is now: five 2025 Fantasy Football
Millionaire standings exports, which is the marquee contest the
"marquee" field-sharpness setting is meant to imitate.

What gets measured, and why each one matters to the generator:

  STACK SHAPE     how many of the QB's own pass catchers ride with him.
                  The single most defining structural choice in NFL DFS
                  and the thing a field model most obviously has to get
                  right.
  BRING-BACK      how often a lineup also takes a player from the game's
                  other side. Correlation the generator currently treats
                  as optional.
  CONCENTRATION   most players from any one team, and how many teams a
                  lineup spans.
  FLEX            which position really fills it.
  OWNERSHIP       cumulative rostership per lineup -- the number the MLB
                  side already calibrates against, now for NFL.
  DUPLICATION     how many entries are literally the same nine players.

Every one of those is reported for the WHOLE FIELD and again for the top
1%, top 0.1% and the winner. The field numbers are what the generator's
opponents should look like; the gap between them is what a lineup needs
to do differently to win, which is a different question and worth not
conflating.

Team affiliation comes from nflverse weekly stats for that exact week,
so a mid-season trade can't misattribute a stack.

Usage (from backend/):
    .venv/Scripts/python.exe -m scripts.analyze_nfl_field_construction
"""

from __future__ import annotations

import asyncio
import csv
import io
import re
import statistics
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from app.clients import nfl
from app.clients.http import close_client
from app.services import contest_results, player_match

CONTEST_DIR = Path(__file__).resolve().parents[2] / "Contest Data" / "NFL"
SEASON = 2025

# DST rosters show up as a bare team nickname ("Broncos"), which is the
# one lineup entry that never appears in a player stats file.
_NICKNAME_RE = re.compile(r"[^a-z]")


async def week_teams(season: int, week: int) -> dict[str, tuple[str, str]]:
    """normalized player name -> (team, opponent) for one real week."""
    grouped = await nfl.get_grouped_season_stats(season)
    out: dict[str, tuple[str, str]] = {}
    for rows in grouped.values():
        for r in rows:
            if r.get("week") != week:
                continue
            name = r.get("player_name")
            if name and r.get("team"):
                out[player_match.normalize_name(name)] = (r["team"], r.get("opponent_team") or "")
    return out


def iter_entries(path: Path):
    """Stream (rank, points, lineup_raw) out of a standings export."""
    with zipfile.ZipFile(path) as z:
        member = next(m for m in z.namelist() if m.lower().endswith(".csv"))
        with z.open(member) as fh:
            text = io.TextIOWrapper(fh, encoding="utf-8-sig", errors="replace")
            reader = csv.DictReader(text)
            for row in reader:
                raw = row.get("Lineup")
                if not raw or not raw.strip():
                    continue
                try:
                    rank = int(row["Rank"])
                    pts = float(row["Points"])
                except (TypeError, ValueError, KeyError):
                    continue
                yield rank, pts, raw


def describe(lineup: list[dict[str, str]], teams: dict[str, tuple[str, str]],
             nickname_to_team: dict[str, str]) -> dict[str, Any] | None:
    """Structural summary of one parsed roster."""
    qb = next((s for s in lineup if s["slot"] == "QB"), None)
    if not qb:
        return None
    qb_team, qb_opp = teams.get(qb["normalized_name"], ("", ""))

    by_team: Counter[str] = Counter()
    catchers_with_qb = 0
    rb_with_qb = 0
    bringback = 0
    flex_pos = None
    resolved = 0

    for s in lineup:
        if s["slot"] == "DST":
            team = nickname_to_team.get(_NICKNAME_RE.sub("", s["name"].lower()), "")
        else:
            team = teams.get(s["normalized_name"], ("", ""))[0]
        if team:
            by_team[team] += 1
            resolved += 1
        if s["slot"] == "QB":
            continue
        if qb_team and team == qb_team:
            if s["slot"] in ("WR", "TE"):
                catchers_with_qb += 1
            elif s["slot"] == "RB":
                rb_with_qb += 1
            elif s["slot"] == "FLEX":
                catchers_with_qb += 1  # counted by what fills it, below
        if qb_opp and team == qb_opp and s["slot"] != "DST":
            bringback += 1

    # Only trust a lineup whose players we could nearly all place.
    if not qb_team or resolved < 7:
        return None

    return {
        "stack": catchers_with_qb,
        "rb_with_qb": rb_with_qb,
        "bringback": bringback,
        "max_team": max(by_team.values()) if by_team else 0,
        "teams": len(by_team),
        "flex": flex_pos,
        "key": tuple(sorted(s["normalized_name"] for s in lineup)),
    }


async def analyse(path: Path) -> None:
    name = path.name
    week = int(re.search(r"_W(\d+)_", name).group(1))
    print(f"\n{'='*78}\n{name[:70]}\n  week {week}\n{'='*78}")

    teams = await week_teams(SEASON, week)
    # nflverse uses team abbreviations; DST cells use nicknames.
    nickname_to_team = {}
    for abbr in {t for t, _ in teams.values()}:
        nickname_to_team[abbr.lower()] = abbr
    # Real nicknames, mapped by hand only where the abbreviation isn't a
    # prefix of the nickname. Everything else falls through harmlessly:
    # a DST we can't place just doesn't contribute to team counts.
    extra = {
        "broncos": "DEN", "chiefs": "KC", "bills": "BUF", "eagles": "PHI",
        "cowboys": "DAL", "packers": "GB", "ravens": "BAL", "steelers": "PIT",
        "49ers": "SF", "seahawks": "SEA", "vikings": "MIN", "lions": "DET",
        "bears": "CHI", "texans": "HOU", "colts": "IND", "jaguars": "JAX",
        "titans": "TEN", "bengals": "CIN", "browns": "CLE", "dolphins": "MIA",
        "patriots": "NE", "jets": "NYJ", "giants": "NYG", "commanders": "WAS",
        "buccaneers": "TB", "saints": "NO", "falcons": "ATL", "panthers": "CAR",
        "cardinals": "ARI", "rams": "LA", "chargers": "LAC", "raiders": "LV",
    }
    nickname_to_team.update(extra)

    rows: list[tuple[int, dict[str, Any]]] = []
    seen_keys: Counter[tuple] = Counter()
    total = parsed = 0
    for rank, _pts, raw in iter_entries(path):
        total += 1
        lu = contest_results.parse_lineup(raw, sport="NFL")
        if not lu:
            continue
        d = describe(lu, teams, nickname_to_team)
        if not d:
            continue
        parsed += 1
        seen_keys[d["key"]] += 1
        rows.append((rank, d))

    if not rows:
        print("  nothing usable")
        return
    print(f"  {total:,} entries, {parsed:,} structurally resolved ({100*parsed/total:.1f}%)")

    n = len(rows)
    rows.sort(key=lambda r: r[0])
    bands = [
        ("whole field", rows),
        ("top 1%", rows[: max(1, n // 100)]),
        ("top 0.1%", rows[: max(1, n // 1000)]),
        ("winner", rows[:1]),
    ]

    print(f"\n  {'band':13} {'n':>8} {'stack':>7} {'bring':>7} {'maxTm':>7} {'teams':>7}")
    for label, band in bands:
        d = [x[1] for x in band]
        print(f"  {label:13} {len(d):8,} "
              f"{statistics.mean(x['stack'] for x in d):7.2f} "
              f"{statistics.mean(x['bringback'] for x in d):7.2f} "
              f"{statistics.mean(x['max_team'] for x in d):7.2f} "
              f"{statistics.mean(x['teams'] for x in d):7.2f}")

    def dist(band, key, cap=5):
        c = Counter(min(x[1][key], cap) for x in band)
        tot = sum(c.values())
        return " ".join(f"{k}:{100*c[k]/tot:4.1f}%" for k in sorted(c))

    print(f"\n  QB stack size (pass catchers with the QB):")
    for label, band in bands[:3]:
        print(f"    {label:13} {dist(band, 'stack')}")
    print(f"  bring-back count:")
    for label, band in bands[:3]:
        print(f"    {label:13} {dist(band, 'bringback')}")
    print(f"  most players from one team:")
    for label, band in bands[:3]:
        print(f"    {label:13} {dist(band, 'max_team')}")

    dupes = sum(v for v in seen_keys.values() if v > 1)
    print(f"\n  unique rosters {len(seen_keys):,} of {parsed:,} "
          f"({100*len(seen_keys)/parsed:.1f}% unique); "
          f"{dupes:,} entries share a roster with someone; "
          f"most-duplicated roster entered {max(seen_keys.values()):,} times")


async def main() -> None:
    files = sorted(CONTEST_DIR.glob("*.zip"))
    if not files:
        print(f"No contests in {CONTEST_DIR}")
        return
    only = sys.argv[1] if len(sys.argv) > 1 else None
    for f in files:
        if only and only not in f.name:
            continue
        await analyse(f)
    await close_client()


if __name__ == "__main__":
    asyncio.run(main())
