"""
HTTP routes for season-long NFL fantasy.

Deliberately its own router under its own prefix rather than more
endpoints bolted onto /api/nfl: season-long shares almost nothing with
the DFS side beyond the sport's name, and the UI keeps them in separate
sections for the same reason.

Everything here is read-only against Sleeper and DraftKings. Nothing in
this router can make a pick, set a lineup, or change a roster.
"""

from __future__ import annotations

from datetime import date as date_cls
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.clients import sleeper
from app.clients.http import ApiError
from app.services import draft_assistant, season_long

router = APIRouter(prefix="/api/season", tags=["season-long"])


def _default_season() -> str:
    """The NFL season a date belongs to -- a new season's league ids
    appear well before Week 1, so anything from March on is next season."""
    today = date_cls.today()
    return str(today.year if today.month >= 3 else today.year - 1)


@router.get("/state")
async def get_state() -> dict[str, Any]:
    """Sleeper's own view of where the NFL season currently is."""
    return await sleeper.get_state()


@router.get("/user/{username}")
async def get_user(
    username: str,
    season: str | None = Query(None, description="Defaults to the current NFL season"),
) -> dict[str, Any]:
    """
    Resolve a Sleeper username and list that account's leagues and
    drafts for the season. This is the entry point -- Sleeper needs no
    password, no OAuth and no linking; a username is enough to read
    everything public about an account.
    """
    user = await sleeper.get_user(username)
    if not user:
        raise HTTPException(404, f"No Sleeper account found for '{username}'.")

    yr = season or _default_season()
    user_id = user["user_id"]
    leagues = await sleeper.get_leagues(user_id, yr)
    drafts = await sleeper.get_user_drafts(user_id, yr)
    return {
        "user": {
            "user_id": user_id,
            "username": user.get("username"),
            "display_name": user.get("display_name"),
            "avatar": user.get("avatar"),
        },
        "season": yr,
        "leagues": [
            {
                "league_id": lg.get("league_id"),
                "name": lg.get("name"),
                "status": lg.get("status"),
                "total_rosters": lg.get("total_rosters"),
                "draft_id": lg.get("draft_id"),
                "roster_positions": lg.get("roster_positions"),
            }
            for lg in leagues
        ],
        "drafts": [
            {
                "draft_id": d.get("draft_id"),
                "league_id": d.get("league_id"),
                "status": d.get("status"),
                "type": d.get("type"),
                "start_time": d.get("start_time"),
                "teams": (d.get("settings") or {}).get("teams"),
                "rounds": (d.get("settings") or {}).get("rounds"),
            }
            for d in drafts
        ],
    }


@router.get("/board")
async def get_board(
    league_id: str | None = Query(
        None, description="Value players for this Sleeper league's own shape"
    ),
    force: bool = Query(False, description="Bypass the DK/Sleeper caches"),
) -> dict[str, Any]:
    """
    The draft board: every draftable player with DraftKings' projection,
    a consensus rank, VORP against replacement level, position rank and
    tier.

    Without a league_id a standard 12-team league is assumed (and the
    response says so on `shape.assumed`).
    """
    try:
        league = await sleeper.get_league(league_id) if league_id else None
    except ApiError as exc:
        if exc.status == 404:
            raise HTTPException(404, f"Sleeper has no league with id '{league_id}'.") from exc
        raise
    return await season_long.build_board(league, force=force)


@router.get("/league/{league_id}")
async def get_league_analysis(
    league_id: str,
    user_id: str | None = Query(None, description="Break out this manager's own team"),
    force: bool = Query(False),
) -> dict[str, Any]:
    """
    Every team in the league valued on the same board: power ranking,
    per-position rank, best starting lineup, bye pileups, injuries, and
    the best unrostered players available.
    """
    try:
        return await season_long.analyze_league(league_id, user_id, force=force)
    except ApiError as exc:
        if exc.status == 404:
            raise HTTPException(404, f"Sleeper has no league with id '{league_id}'.") from exc
        raise


@router.get("/draft/{draft_id}")
async def get_draft(
    draft_id: str,
    user_id: str | None = Query(None, description="Whose turn to track and advise"),
    league_id: str | None = Query(None, description="Value the board for this league's shape"),
) -> dict[str, Any]:
    """
    Live draft: current state plus ranked suggestions for this user's
    next pick, each with the reasoning behind it.

    Sleeper publishes no push feed for drafts, so a UI polls this. The
    underlying picks call is cached for 5 seconds, which keeps a short
    poll interval well inside Sleeper's rate limit.
    """
    league = await sleeper.get_league(league_id) if league_id else None
    try:
        return await draft_assistant.live(draft_id, user_id, league=league)
    except ApiError as exc:
        # A mistyped or expired draft id is an ordinary user event, not a
        # server fault -- say which it was. Anything else (Sleeper down
        # mid-draft) must keep surfacing as a real error rather than
        # being disguised as "no such draft".
        if exc.status == 404:
            raise HTTPException(404, f"Sleeper has no draft with id '{draft_id}'.") from exc
        raise


@router.get("/bestball")
async def get_bestball_board(
    force: bool = Query(False, description="Bypass the DraftKings cache"),
) -> dict[str, Any]:
    """
    DraftKings Best Ball specifically: the live draft group and its
    board, in DK's own ordering.

    Best Ball on DK is a 20-round snake with no weekly lineup setting --
    the highest-scoring legal combination is scored for you every week.
    That makes it a pure "accumulate upside and cover byes" format, so
    the board is returned with tiers and VORP under DK's own best-ball
    roster shape rather than a redraft league's.
    """
    board = await season_long.build_board(season_long.BEST_BALL_LEAGUE, force=force)
    board["format"] = {
        "name": "DraftKings Best Ball",
        "rounds": 20,
        "note": (
            "No weekly lineups -- DK scores your best legal combination automatically, "
            "so bye weeks cost far less here than in a redraft league and raw upside is "
            "worth more than floor."
        ),
    }
    return board
