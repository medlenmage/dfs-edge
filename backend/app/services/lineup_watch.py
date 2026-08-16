"""
Background watcher for confirmed-lineup changes.

Lineups can flip between when they're first confirmed and when a slate
actually locks -- a late scratch, a rainout call, a manager swap. Every
other part of this app is request-driven (nothing updates unless you
click Refresh), so without this nothing would ever notice a player
quietly disappearing from a confirmed lineup after the fact.

This polls mlb.get_lineups() for each of today's not-yet-started games
every `settings.lineup_poll_interval_sec` seconds, diffs the result
against the last snapshot seen for that game, and records anyone who
drops out as a "scratch" event. Wired into main.py's lifespan as a
background task -- it only runs while the backend process is up, which
is an acceptable trade-off for a single-user, run-on-your-own-machine
app (same model as the rest of it).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date as date_cls
from typing import Any

from app import cache
from app.clients import mlb
from app.config import get_settings

log = logging.getLogger(__name__)

# Once a game reaches any of these states, its confirmed lineup no longer
# matters for scratch-watching purposes -- either it's locked (in
# progress or finished) or it isn't happening (postponed/cancelled).
_SKIP_STATES = {
    "Final",
    "Game Over",
    "Completed Early",
    "Cancelled",
    "Postponed",
    "In Progress",
    "Suspended",
}

# A day's worth of scratch events and lineup snapshots outlive the slate
# itself, then self-clear -- comfortably longer than any game could run.
_TTL = 20 * 60 * 60


async def poll_once(day: str) -> list[dict[str, Any]]:
    """
    Re-check every unfinished game's confirmed lineup for `day`, record
    any newly-scratched players, and return the events found this poll.

    An empty list is the expected, common result -- it just means
    nothing changed since the last poll.
    """
    games = await mlb.get_schedule(day)
    all_events: list[dict[str, Any]] = []

    for game in games:
        status = (game.get("status") or {}).get("detailedState") or ""
        if status in _SKIP_STATES:
            continue
        game_pk = game.get("gamePk")
        if not game_pk:
            continue

        teams = game.get("teams") or {}
        abbrevs = {
            "home": ((teams.get("home") or {}).get("team") or {}).get("abbreviation") or "",
            "away": ((teams.get("away") or {}).get("team") or {}).get("abbreviation") or "",
        }

        current = await mlb.get_lineups(game_pk, force=True)
        snapshot_key = f"lineup_watch:{game_pk}"
        previous = cache.get(snapshot_key) or {}

        all_events.extend(await _diff(previous, current, abbrevs, game_pk))
        cache.put(snapshot_key, current, _TTL)

    if all_events:
        day_key = f"scratches:{day}"
        existing = cache.get(day_key) or []
        cache.put(day_key, existing + all_events, _TTL)

    return all_events


async def _diff(
    previous: dict[str, Any],
    current: dict[str, Any],
    abbrevs: dict[str, str],
    game_pk: int,
) -> list[dict[str, Any]]:
    """
    Anyone present in the previous confirmed lineup but missing from the
    current one is a scratch. Anyone newly ADDED is just a lineup being
    posted or filled in -- that's not a scratch, so only look at drops.
    """
    dropped_side: dict[int, str] = {}
    for side in ("home", "away"):
        prev_ids = set(previous.get(side) or [])
        cur_ids = set(current.get(side) or [])
        for pid in prev_ids - cur_ids:
            dropped_side[pid] = side

    if not dropped_side:
        return []

    bios = await mlb.get_people(list(dropped_side))
    events = []
    for pid, side in dropped_side.items():
        bio = bios.get(pid) or {}
        events.append(
            {
                "player_id": pid,
                "name": bio.get("name") or f"Player {pid}",
                "team": abbrevs.get(side, ""),
                "game_pk": game_pk,
            }
        )
    return events


async def _poll_loop() -> None:
    settings = get_settings()
    while True:
        try:
            events = await poll_once(date_cls.today().isoformat())
            if events:
                log.info(
                    "Lineup watcher found %d scratch(es): %s",
                    len(events),
                    ", ".join(f"{e['name']} ({e['team']})" for e in events),
                )
        except Exception:
            log.exception("Lineup watcher poll failed")
        await asyncio.sleep(settings.lineup_poll_interval_sec)
