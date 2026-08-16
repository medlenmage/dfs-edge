"""HTTP routes for MLB."""

from __future__ import annotations

from datetime import date as date_cls
from typing import Any

from fastapi import APIRouter, Body, File, HTTPException, Query, UploadFile

from app.services import analysis, mlb_slate, optimizer, projections, salaries

router = APIRouter(prefix="/api/mlb", tags=["mlb"])


@router.get("/slate")
async def get_slate(
    date: str | None = Query(None, description="YYYY-MM-DD, defaults to today"),
    refresh: bool = Query(False, description="Bypass the cache"),
    hitters: bool = Query(True, description="Include per-hitter matchup scores"),
) -> dict[str, Any]:
    """
    The full daily slate: games, environment, pitchers, and hitter edges.

    This is the one endpoint the dashboard needs.
    """
    day = date or date_cls.today().isoformat()
    try:
        return await mlb_slate.build_slate(
            day, force_refresh=refresh, include_hitters=hitters
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/games")
async def get_games(
    date: str | None = Query(None),
    refresh: bool = Query(False),
) -> dict[str, Any]:
    """Lightweight version: games and environment, no hitter scoring. Fast."""
    day = date or date_cls.today().isoformat()
    return await mlb_slate.build_slate(day, force_refresh=refresh, include_hitters=False)


@router.get("/hitters")
async def get_top_hitters(
    date: str | None = Query(None),
    limit: int = Query(40, ge=1, le=300),
    min_score: float = Query(0, ge=0, le=100),
) -> dict[str, Any]:
    """Every hitter on the slate, flattened and ranked by edge score."""
    day = date or date_cls.today().isoformat()
    slate = await mlb_slate.build_slate(day)

    rows = []
    for game in slate.get("games") or []:
        for side in ("home", "away"):
            team = game[side]
            opp = game["away" if side == "home" else "home"]
            for h in team.get("hitters") or []:
                if h["edge"]["score"] < min_score:
                    continue
                rows.append(
                    {
                        **h,
                        "team": team.get("abbrev"),
                        "opponent": opp.get("abbrev"),
                        "is_home": side == "home",
                        "venue": game["venue"]["name"],
                        "game_time_utc": game.get("game_time_utc"),
                        "implied_runs": team.get("implied_runs"),
                        "opposing_pitcher": (opp.get("probable_pitcher") or {}).get("name"),
                    }
                )

    rows.sort(key=lambda r: r["edge"]["score"], reverse=True)
    return {"date": slate.get("date"), "count": len(rows), "hitters": rows[:limit]}


@router.get("/stacks")
async def get_stacks(date: str | None = Query(None)) -> dict[str, Any]:
    """Teams ranked by how attractive they are to stack."""
    day = date or date_cls.today().isoformat()
    slate = await mlb_slate.build_slate(day)

    stacks = []
    for game in slate.get("games") or []:
        for side in ("home", "away"):
            team = game[side]
            opp = game["away" if side == "home" else "home"]
            if team.get("stack_score") is None:
                continue
            stacks.append(
                {
                    "team": team.get("name"),
                    "abbrev": team.get("abbrev"),
                    "opponent": opp.get("abbrev"),
                    "is_home": side == "home",
                    "stack_score": team.get("stack_score"),
                    "implied_runs": team.get("implied_runs"),
                    "lineup_confirmed": team.get("lineup_confirmed"),
                    "venue": game["venue"]["name"],
                    "park_hr_factor": game["venue"]["park_factors"]["hr"],
                    "opposing_pitcher": (opp.get("probable_pitcher") or {}).get("name"),
                    "opposing_pitcher_throws": (opp.get("probable_pitcher") or {}).get("throws"),
                    "top_bats": [
                        {"name": h["name"], "score": h["edge"]["score"]}
                        for h in (team.get("hitters") or [])[:5]
                    ],
                }
            )

    stacks.sort(key=lambda s: s["stack_score"], reverse=True)
    return {"date": slate.get("date"), "stacks": stacks}


@router.get("/pitchers")
async def get_top_pitchers(date: str | None = Query(None)) -> dict[str, Any]:
    """Today's probable starters, ranked by matchup edge."""
    day = date or date_cls.today().isoformat()
    slate = await mlb_slate.build_slate(day)

    rows = []
    for game in slate.get("games") or []:
        for side in ("home", "away"):
            team = game[side]
            opp = game["away" if side == "home" else "home"]
            pitcher = team.get("probable_pitcher")
            if not pitcher or not pitcher.get("edge"):
                continue
            rows.append(
                {
                    **pitcher,
                    "team": team.get("abbrev"),
                    "opponent": opp.get("abbrev"),
                    "is_home": side == "home",
                    "venue": game["venue"]["name"],
                    "game_time_utc": game.get("game_time_utc"),
                    "implied_runs_against": opp.get("implied_runs"),
                }
            )

    rows.sort(key=lambda r: r["edge"]["score"], reverse=True)
    return {"date": slate.get("date"), "pitchers": rows}


@router.get("/injuries")
async def get_injuries(date: str | None = Query(None)) -> dict[str, Any]:
    """Every player currently off the active roster, by team."""
    day = date or date_cls.today().isoformat()
    slate = await mlb_slate.build_slate(day)

    rows = []
    for game in slate.get("games") or []:
        for side in ("home", "away"):
            team = game[side]
            for player in team.get("injuries") or []:
                rows.append(
                    {
                        **player,
                        "team": team.get("abbrev"),
                    }
                )

    return {"date": slate.get("date"), "injuries": rows}


@router.get("/scratches")
async def get_scratches(date: str | None = Query(None)) -> dict[str, Any]:
    """Players the background lineup watcher has caught dropping out of a
    confirmed lineup today -- see services/lineup_watch.py."""
    day = date or date_cls.today().isoformat()
    slate = await mlb_slate.build_slate(day)

    rows = []
    for game in slate.get("games") or []:
        for side in ("home", "away"):
            rows.extend(game[side].get("scratches") or [])

    return {"date": slate.get("date"), "scratches": rows}


@router.post("/salaries")
async def upload_salaries(
    date: str | None = Query(None, description="Slate date this CSV is for, defaults to today"),
    file: UploadFile = File(..., description="DraftKings 'DKSalaries.csv' export"),
) -> dict[str, Any]:
    """
    Upload a DraftKings salary CSV for a slate.

    Cached until you upload a new one for the same date -- there's no
    live salary feed to pull from, so this is how they get in.
    """
    day = date or date_cls.today().isoformat()
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Couldn't read that as text: {exc}") from exc

    rows = salaries.parse_dk_csv(text)
    if not rows:
        raise HTTPException(
            status_code=400,
            detail="No players found in that file -- is it a DraftKings salary export?",
        )
    salaries.store(day, rows)
    return {"date": day, "players_loaded": len(rows)}


@router.get("/salaries")
async def get_salaries(date: str | None = Query(None)) -> dict[str, Any]:
    """Whatever salary data is currently loaded for a date."""
    day = date or date_cls.today().isoformat()
    rows = salaries.load(day)
    return {"date": day, "loaded": bool(rows), "players": rows}


@router.post("/projections")
async def upload_projections(
    date: str | None = Query(None, description="Slate date this CSV is for, defaults to today"),
    file: UploadFile = File(..., description="RotoWire player-pool CSV export"),
) -> dict[str, Any]:
    """
    Upload a RotoWire FPTS/ownership projections CSV for a slate.

    Reference data only -- see services/projections.py for why this
    isn't blended into the matchup score.
    """
    day = date or date_cls.today().isoformat()
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Couldn't read that as text: {exc}") from exc

    rows = projections.parse_rotowire_csv(text)
    if not rows:
        raise HTTPException(
            status_code=400,
            detail="No players found in that file -- is it a RotoWire player-pool export?",
        )
    projections.store(day, rows)
    return {"date": day, "players_loaded": len(rows)}


@router.get("/projections")
async def get_projections(date: str | None = Query(None)) -> dict[str, Any]:
    """Whatever projections data is currently loaded for a date."""
    day = date or date_cls.today().isoformat()
    rows = projections.load(day)
    return {"date": day, "loaded": bool(rows), "players": rows}


@router.get("/analysis")
async def get_analysis(
    date: str | None = Query(None),
    refresh: bool = Query(False),
) -> dict[str, Any]:
    """Claude's written read on the slate."""
    day = date or date_cls.today().isoformat()
    slate = await mlb_slate.build_slate(day)
    return await analysis.analyse_slate(slate, force=refresh)


@router.post("/ask")
async def ask(
    question: str = Body(..., embed=True),
    date: str | None = Body(None, embed=True),
) -> dict[str, Any]:
    """Ask Claude a follow-up about today's slate."""
    day = date or date_cls.today().isoformat()
    slate = await mlb_slate.build_slate(day)
    return await analysis.ask_about_slate(slate, question)


@router.post("/lineups")
async def generate_lineups(
    date: str | None = Body(None, embed=True),
    num_lineups: int = Body(1, embed=True, description="How many distinct lineups to build"),
    stack_groups: list[int] | None = Body(
        None, embed=True, description="Hitter-group sizes to force, e.g. [4, 2, 2] for a 4-2-2 stack"
    ),
    stack_teams: list[str | None] | None = Body(
        None, embed=True, description="One team per stack group (or null for auto), same length as stack_groups"
    ),
    max_exposure_pct: float | None = Body(
        None, embed=True, description="Cap how often any one player appears across the set"
    ),
) -> dict[str, Any]:
    """
    Up to `num_lineups` distinct, highest-projected DraftKings Classic
    MLB lineups, built from whatever salary + projections CSVs are
    loaded for the date.

    Requires both -- this app doesn't build its own FPTS projections
    yet, so a RotoWire projections file is the objective function this
    optimizes against.
    """
    day = date or date_cls.today().isoformat()
    slate = await mlb_slate.build_slate(day)
    try:
        result = optimizer.generate_lineups(
            slate,
            num_lineups=num_lineups,
            stack_groups=stack_groups,
            stack_teams=stack_teams,
            max_exposure_pct=max_exposure_pct,
        )
    except optimizer.OptimizerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"date": day, **result}
