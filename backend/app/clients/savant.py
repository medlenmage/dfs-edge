"""
Batted-ball quality via Baseball Savant's public leaderboard export.

Free, no key, no docs -- this is the CSV export behind Savant's own
leaderboard pages (https://baseballsavant.mlb.com/leaderboard/custom).
Unofficial, so it could change shape without notice; if it fails we
degrade the same way every other source here does (the caller just gets
neutral scores instead of a broken page).

WHY THIS MATTERS
-----------------
OPS tells you what happened. Barrel rate, hard-hit rate and expected
wOBA tell you how well the ball was actually struck, independent of
whether it found a glove -- and they stabilise in far fewer plate
appearances than OPS does. That is the whole case for adding them: not
a replacement for the platoon/pitcher matchup work already in
scoring.py, but a second, faster-converging read on the same question.

Rows are keyed by MLB Advanced Media's player_id, the same id used
everywhere else in this app.
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Any

from app.cache import cached
from app.clients.http import get_text
from app.config import get_settings

log = logging.getLogger(__name__)

BASE = "https://baseballsavant.mlb.com/leaderboard/custom"

_FIELDS = "barrel_batted_rate,hard_hit_percent,xwoba,xslg,exit_velocity_avg"

# Plate discipline and batted-ball mix, pitchers only. These feed
# services/pitcher_metrics.py: the batted-ball shares are what SIERA
# reads, the fly-ball share is xFIP's expected-homer input, and
# swing/whiff together give swinging-strike rate per PITCH.
#
# WARNING, learned the hard way: this leaderboard echoes back EVERY
# column name you ask for, whether or not it's a real field -- a
# misspelled selection comes back as a present-but-empty column rather
# than an error. Every name below was verified to carry real data across
# all 427 qualified pitchers before being added. Notably there is NO
# called_strike_percent, csw_percent or swinging_strike_percent here
# under any name; those were all checked and all came back empty, which
# is why CSW needs the separate per-pitcher fetch below.
_PITCH_FIELDS = (
    "pitch_count,swing_percent,whiff_percent,k_percent,bb_percent,"
    "groundballs_percent,flyballs_percent,linedrives_percent,popups_percent,"
    "edge_percent,f_strike_percent,in_zone_percent"
)

_SEARCH = "https://baseballsavant.mlb.com/statcast_search/csv"

# The pitch results that make up CSW's numerator. Foul tips count as
# whiffs by the standard definition -- a foul tip is a swing and a miss
# that the catcher happened to hold onto.
_CSW_RESULTS = (
    r"called\.\.strike|swinging\.\.strike|swinging\.\.strike\.\.blocked|foul\.\.tip|"
)
_WHIFF_DESCRIPTIONS = {"swinging_strike", "swinging_strike_blocked", "foul_tip"}

# Minimum sample before a batted-ball profile is trusted -- these stats
# stabilise fast, so this is deliberately looser than the OPS thresholds
# in scoring.py (MIN_PA_FULL_TRUST / MIN_BF_FULL_TRUST).
_MIN_PA = 50
_MIN_IP = 15


def _f(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def _leaderboard(season: int, player_type: str, min_sample: int) -> dict[int, dict[str, Any]]:
    settings = get_settings()

    async def _load() -> str:
        return await get_text(
            BASE,
            params={
                "year": season,
                "type": player_type,
                "filter": "",
                "min": min_sample,
                "selections": _FIELDS,
                "chart": "false",
                "x": "xwoba",
                "y": "xwoba",
                "r": "no",
                "chartType": "beeswarm",
                "csv": "true",
            },
            source="Baseball Savant",
        )

    text = await cached(f"savant:{player_type}:{season}", settings.ttl_stats, _load)

    out: dict[int, dict[str, Any]] = {}
    # Savant's CSV export leads with a UTF-8 BOM, which -- left in place --
    # attaches itself to the first (quoted) header cell and breaks the
    # quote parsing for the whole row.
    for row in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        pid = row.get("player_id")
        if not pid:
            continue
        out[int(pid)] = {
            "barrel_pct": _f(row.get("barrel_batted_rate")),
            "hard_hit_pct": _f(row.get("hard_hit_percent")),
            "xwoba": _f(row.get("xwoba")),
            "xslg": _f(row.get("xslg")),
            "exit_velo": _f(row.get("exit_velocity_avg")),
        }
    return out


async def get_hitter_batted_ball(season: int) -> dict[int, dict[str, Any]]:
    """Each qualified hitter's own batted-ball profile."""
    return await _leaderboard(season, "batter", _MIN_PA)


async def get_pitcher_batted_ball(season: int) -> dict[int, dict[str, Any]]:
    """Each qualified pitcher's batted-ball profile ALLOWED."""
    return await _leaderboard(season, "pitcher", _MIN_IP)


async def get_pitcher_plate_skills(season: int) -> dict[int, dict[str, Any]]:
    """
    Every qualified pitcher's plate-discipline and batted-ball mix.

    Separate from get_pitcher_batted_ball() rather than folded into it
    because the two answer different questions and are wanted in
    different places: that one is about the QUALITY of contact allowed
    (barrels, xwOBA) and feeds the matchup score, this one is about the
    SHAPE of his skill (whiffs, ground balls, command) and feeds the
    skill metrics in services/pitcher_metrics.py.
    """
    settings = get_settings()

    async def _load() -> str:
        return await get_text(
            BASE,
            params={
                "year": season,
                "type": "pitcher",
                "filter": "",
                "min": _MIN_IP,
                "selections": _PITCH_FIELDS,
                "chart": "false",
                "x": "k_percent",
                "y": "k_percent",
                "r": "no",
                "chartType": "beeswarm",
                "csv": "true",
            },
            source="Baseball Savant",
        )

    text = await cached(f"savant:pitch_skills:{season}", settings.ttl_stats, _load)

    out: dict[int, dict[str, Any]] = {}
    for row in csv.DictReader(io.StringIO(text.lstrip("\ufeff"))):
        pid = row.get("player_id")
        if not pid:
            continue
        out[int(pid)] = {
            "pitches": _f(row.get("pitch_count")),
            "swing_pct": _f(row.get("swing_percent")),
            "whiff_pct": _f(row.get("whiff_percent")),
            "k_pct": _f(row.get("k_percent")),
            "bb_pct": _f(row.get("bb_percent")),
            "gb_pct": _f(row.get("groundballs_percent")),
            "fb_pct": _f(row.get("flyballs_percent")),
            "ld_pct": _f(row.get("linedrives_percent")),
            "pu_pct": _f(row.get("popups_percent")),
            "edge_pct": _f(row.get("edge_percent")),
            "first_strike_pct": _f(row.get("f_strike_percent")),
            "in_zone_pct": _f(row.get("in_zone_percent")),
        }
    return out


async def get_pitcher_csw(player_id: int, season: int) -> dict[str, Any] | None:
    """
    One pitcher's called strikes and whiffs for the season.

    CSW isn't on any aggregate leaderboard -- it has to be counted from
    pitch-level data. Doing that league-wide is not an option: the search
    endpoint hard-caps at 25,000 rows and a season of called strikes
    across the league runs an order of magnitude past that. Filtered to
    ONE pitcher and to only the four pitch results that make up the
    numerator, it's about 800 rows and half a megabyte -- small enough to
    run for the dozen or two probable starters on a slate.

    Only the three counts are cached, not the half-megabyte CSV they came
    from. The raw response is enormous relative to what's actually wanted
    from it, and this cache has been allowed to bloat before.

    The pitch count denominator is NOT here -- it lives on
    get_pitcher_plate_skills(), which is one league-wide fetch. Callers
    join the two; see services/pitcher_metrics.csw_pct().
    """
    settings = get_settings()

    async def _load() -> dict[str, Any]:
        text = await get_text(
            _SEARCH,
            params={
                "all": "true",
                "hfGT": "R|",
                "hfPR": _CSW_RESULTS,
                "hfSea": f"{season}|",
                "player_type": "pitcher",
                "pitchers_lookup[]": str(player_id),
                "min_pitches": "0",
                "min_results": "0",
                "min_pas": "0",
                "type": "details",
                "csv": "true",
            },
            source="Baseball Savant",
        )
        called = 0
        whiffs = 0
        for row in csv.DictReader(io.StringIO(text.lstrip("\ufeff"))):
            description = row.get("description")
            if description == "called_strike":
                called += 1
            elif description in _WHIFF_DESCRIPTIONS:
                whiffs += 1
        return {"called_strikes": called, "whiffs": whiffs}

    return await cached(f"savant:csw:{season}:{player_id}", settings.ttl_stats, _load)
