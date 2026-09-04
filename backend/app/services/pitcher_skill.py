"""
Assemble the per-pitcher skill table a slate needs.

pitcher_metrics.py holds the formulas and knows nothing about fetching;
this joins them to the two real sources -- MLB's counting stats and
Savant's batted-ball/plate-discipline export -- and decides who is worth
spending a CSW fetch on.

That last part is the whole reason this module exists. SIERA and xFIP
come free with data the slate already pulls league-wide, but CSW has to
be counted from pitch-level rows one pitcher at a time (see
clients/savant.get_pitcher_csw for why there is no league-wide route).
Running that for all 400-odd pitchers on the board would be absurd; it
runs for the dozen or two probable starters actually being scored.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.clients import savant
from app.services import pitcher_metrics

log = logging.getLogger(__name__)

# How many CSW fetches run at once. Each is a real request to Savant for
# a few hundred kilobytes; this is deliberately polite rather than fast.
_CSW_CONCURRENCY = 4

# Below this the season is too short for a skill read to mean anything,
# and the component falls back to ERA rather than pretending.
_MIN_IP = 20.0


def build_skill_table(
    season_stats: dict[int, dict[str, Any]],
    plate_skills: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """
    Every pitcher's SIERA and xFIP, plus the league constants they were
    computed against.

    Returns {"pitchers": {id: {...}}, "league": {...}}. The league block
    carries the real HR/FB and FIP constant for THIS season rather than a
    hardcoded value from some other one, and the average SIERA the
    scoring component grades against.
    """
    pool: list[dict[str, Any]] = []
    for pid, stat in season_stats.items():
        skills = plate_skills.get(pid)
        if not skills or not stat:
            continue
        ip = stat.get("ip") or 0.0
        bf = stat.get("bf") or 0
        if ip < _MIN_IP or not bf:
            continue
        k = stat.get("k") or 0
        bb = stat.get("bb") or 0
        hbp = stat.get("hbp") or 0
        pool.append(
            {
                "id": pid,
                "ip": ip,
                "bf": bf,
                "k": k,
                "bb": bb,
                "hbp": hbp,
                "hr": stat.get("hr") or 0,
                "er": stat.get("er") or 0,
                "fly_balls": pitcher_metrics.expected_fly_balls(
                    bf, k, bb, hbp, skills.get("fb_pct")
                ),
                "skills": skills,
            }
        )

    league = pitcher_metrics.league_constants(pool)

    out: dict[int, dict[str, Any]] = {}
    for p in pool:
        skills = p["skills"]
        out[p["id"]] = {
            "xfip": pitcher_metrics.xfip(
                p["ip"], p["k"], p["bb"], p["hbp"], p["fly_balls"],
                league["hr_fb"], league["cfip"],
            ),
            "siera": pitcher_metrics.siera(
                p["k"], p["bb"], p["bf"],
                skills.get("gb_pct"), skills.get("fb_pct"), skills.get("pu_pct"),
            ),
            "swstr_pct": pitcher_metrics.swstr_pct(
                skills.get("swing_pct"), skills.get("whiff_pct")
            ),
            "gb_pct": skills.get("gb_pct"),
            "fb_pct": skills.get("fb_pct"),
            "edge_pct": skills.get("edge_pct"),
            "first_strike_pct": skills.get("first_strike_pct"),
            "pitches": skills.get("pitches"),
        }

    sieras = [v["siera"] for v in out.values() if v["siera"] is not None]
    xfips = [v["xfip"] for v in out.values() if v["xfip"] is not None]
    league["siera"] = round(sum(sieras) / len(sieras), 3) if sieras else None
    league["xfip"] = round(sum(xfips) / len(xfips), 3) if xfips else None

    return {"pitchers": out, "league": league}


async def attach_csw(
    table: dict[str, Any], pitcher_ids: list[int], season: int
) -> dict[str, Any]:
    """
    Count called strikes and whiffs for the given pitchers and fold CSW
    into the table in place.

    A failed fetch is logged and skipped, never raised: CSW is one input
    to one component, and a pitcher missing it falls back to K/9 the same
    way a pitcher with no Statcast profile already does. Losing Savant
    should cost a little precision, not the whole slate.
    """
    wanted = [
        pid for pid in dict.fromkeys(pitcher_ids)
        if pid and table["pitchers"].get(pid, {}).get("pitches")
    ]
    if not wanted:
        return table

    semaphore = asyncio.Semaphore(_CSW_CONCURRENCY)

    async def _one(pid: int) -> tuple[int, dict[str, Any] | None]:
        async with semaphore:
            try:
                return pid, await savant.get_pitcher_csw(pid, season)
            except Exception:
                log.warning("CSW fetch failed for pitcher %s", pid, exc_info=True)
                return pid, None

    for pid, counts in await asyncio.gather(*(_one(p) for p in wanted)):
        if not counts:
            continue
        entry = table["pitchers"][pid]
        entry["called_strikes"] = counts["called_strikes"]
        entry["whiffs"] = counts["whiffs"]
        entry["csw_pct"] = pitcher_metrics.csw_pct(
            counts["called_strikes"], counts["whiffs"], int(entry["pitches"])
        )

    csws = [
        v["csw_pct"] for v in table["pitchers"].values() if v.get("csw_pct") is not None
    ]
    # The league CSW average is taken over whoever was actually fetched --
    # today's starters. That is a starter-weighted baseline rather than a
    # true league one, which is the right comparison here anyway, since
    # every pitcher graded against it is himself a starter. With too few
    # to average, fall back to the published consensus figure rather than
    # grading one starter against himself.
    table["league"]["csw"] = (
        round(sum(csws) / len(csws), 4)
        if len(csws) >= 4
        else pitcher_metrics.DEFAULT_LEAGUE_CSW
    )
    return table
