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
