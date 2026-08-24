"""
Play-by-play data via nflverse's "pbp" release
(https://github.com/nflverse/nflverse-data/releases/tag/pbp) -- the same
free, no-key, community CSV-export pattern as clients/nfl.py, but a
genuinely different (much larger) dataset: one row per PLAY, not per
player-game, ~45,000 rows and 372 columns for a full season. Fetched as
the gzipped variant (~19MB) rather than the plain .csv nflverse also
publishes (~98MB for the same season) -- same data, a fifth of the
transfer.

The one thing this client exists for: nflfastR's own `xpass` model
(the community-standard expected-pass-rate model, already computed by
nflverse for every play) gives a real, non-guessed Pass Rate Over
Expectation per play via `pass_oe = (actual pass/no-pass - xpass) * 100`.
Aggregating that per team over "neutral script" plays (see
`_is_neutral_script` below) is the standard PROE methodology -- how much
MORE or LESS an offense passes than a neutral, down/distance/score/time
-aware model expects, stripped of the "they're passing because they're
losing" confound plain pass rate can't separate out.

Only the ~10 columns actually needed are kept per play (this is parsed
down to a small, cached, per-team aggregate immediately -- see
`get_team_proe()` -- never the raw 45,000-row list), same "cache the
clean derived thing, not the giant blob" discipline as
`nfl.get_grouped_season_stats()`.
"""

from __future__ import annotations

import csv
import gzip
import io
import logging
from collections import defaultdict
from typing import Any

from app.cache import cached
from app.clients.http import ApiError, get_bytes
from app.config import get_settings

log = logging.getLogger(__name__)

PBP_URL_TEMPLATE = (
    "https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.csv.gz"
)

_PBP_COLUMNS = [
    "posteam", "defteam", "down", "pass_oe", "wp",
    "qb_spike", "qb_kneel", "half_seconds_remaining",
]

# "Neutral script" -- the standard PROE filter, stripping out plays where
# the pass/run call is driven by something other than the offense's own
# real intent: garbage time (win probability near 0 or 1), the two-minute
# drill (pass rate spikes regardless of script), spikes/kneels (not real
# play calls), and 4th down (a go/punt/FG decision, not pass-vs-run).
NEUTRAL_SCRIPT_MIN_WP = 0.10
NEUTRAL_SCRIPT_MAX_WP = 0.90
TWO_MINUTE_DRILL_SECONDS = 120.0

_TTL_MULTIPLIER = 4  # matches nfl.get_prior_season_context()'s own ttl_stats*4


def _f(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _is_neutral_script(row: dict[str, Any]) -> bool:
    if row.get("qb_spike") or row.get("qb_kneel"):
        return False
    if row.get("down") not in (1.0, 2.0, 3.0):
        return False
    half_secs = row.get("half_seconds_remaining")
    if half_secs is not None and half_secs <= TWO_MINUTE_DRILL_SECONDS:
        return False
    wp = row.get("wp")
    if wp is not None and not (NEUTRAL_SCRIPT_MIN_WP <= wp <= NEUTRAL_SCRIPT_MAX_WP):
        return False
    return row.get("pass_oe") is not None


async def _load_pbp_rows(season: int) -> list[dict[str, Any]]:
    url = PBP_URL_TEMPLATE.format(season=season)
    raw = await get_bytes(url, source="nflverse play-by-play")
    text = gzip.decompress(raw).decode("utf-8")

    rows = []
    for raw_row in csv.DictReader(io.StringIO(text)):
        rows.append(
            {
                "posteam": raw_row.get("posteam") or None,
                "defteam": raw_row.get("defteam") or None,
                "down": _f(raw_row.get("down")),
                "pass_oe": _f(raw_row.get("pass_oe")),
                "wp": _f(raw_row.get("wp")),
                "qb_spike": raw_row.get("qb_spike") == "1",
                "qb_kneel": raw_row.get("qb_kneel") == "1",
                "half_seconds_remaining": _f(raw_row.get("half_seconds_remaining")),
            }
        )
    return rows


async def get_team_proe(season: int, *, force: bool = False) -> dict[str, dict[str, Any]]:
    """
    Per-team Pass Rate Over Expectation for a season, both sides of the
    ball:

      * `off_proe` -- this team's OWN offense, averaged over its neutral-
        script plays. Positive means they pass more than a neutral model
        expects (a pass-funnel or aggressive-playcalling offense);
        negative means they lean run-heavy even in neutral spots.
      * `def_proe_allowed` -- the plays this team's DEFENSE faced,
        averaged the same way. Positive means opposing offenses pass
        MORE than expected when playing this defense -- the "vulnerable
        through the air, tough against the run" pass-funnel signal.

    Both are in percentage points (a `pass_oe` of 8.0 means 8 points of
    pass rate above what the model expected). Cached at the same
    long-lived cadence as `nfl.get_prior_season_context()`, since this is
    a genuinely expensive fetch (~19MB) meant to be pulled once per
    season, not per request.
    """
    async def _load() -> dict[str, Any]:
        try:
            rows = await _load_pbp_rows(season)
        except ApiError as exc:
            log.warning("PBP fetch failed for %s: %s", season, exc)
            return {}

        off_sum: dict[str, float] = defaultdict(float)
        off_n: dict[str, int] = defaultdict(int)
        def_sum: dict[str, float] = defaultdict(float)
        def_n: dict[str, int] = defaultdict(int)

        for row in rows:
            if not _is_neutral_script(row):
                continue
            proe = row["pass_oe"]
            if row["posteam"]:
                off_sum[row["posteam"]] += proe
                off_n[row["posteam"]] += 1
            if row["defteam"]:
                def_sum[row["defteam"]] += proe
                def_n[row["defteam"]] += 1

        teams = set(off_sum) | set(def_sum)
        return {
            team: {
                "off_proe": round(off_sum[team] / off_n[team], 2) if off_n.get(team) else None,
                "off_plays_sampled": off_n.get(team, 0),
                "def_proe_allowed": round(def_sum[team] / def_n[team], 2) if def_n.get(team) else None,
                "def_plays_sampled": def_n.get(team, 0),
            }
            for team in teams
        }

    settings = get_settings()
    return await cached(f"nfl:proe:{season}", settings.ttl_stats * _TTL_MULTIPLIER, _load, force=force)
