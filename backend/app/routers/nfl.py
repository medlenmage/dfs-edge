"""HTTP routes for NFL."""

from __future__ import annotations

from datetime import date as date_cls
from typing import Any

from fastapi import APIRouter, Body, File, HTTPException, Query, UploadFile

from app.clients import nfl
from app.services import nfl_optimizer, nfl_slate, projections, salaries

router = APIRouter(prefix="/api/nfl", tags=["nfl"])


async def _resolve_season_week(season: int | None, week: int | None) -> tuple[int, int]:
    today_iso = date_cls.today().isoformat()
    resolved_season = season if season is not None else nfl.season_for_date(date_cls.today())
    if week is not None:
        return resolved_season, week
    games = await nfl.get_schedule(resolved_season)
    return resolved_season, nfl.current_week(games, today_iso)


@router.get("/slate")
async def get_slate(
    season: int | None = Query(None, description="e.g. 2026 -- defaults to the current NFL season"),
    week: int | None = Query(None, description="1-18 -- defaults to the current week"),
) -> dict[str, Any]:
    """The week's slate: games, Vegas-implied context, weather, and (once uploaded) every rostered player."""
    resolved_season, resolved_week = await _resolve_season_week(season, week)
    return await nfl_slate.build_slate(resolved_season, resolved_week)


@router.post("/salaries")
async def upload_salaries(
    season: int | None = Query(None),
    week: int | None = Query(None),
    file: UploadFile = File(..., description="DraftKings 'DKSalaries.csv' export"),
) -> dict[str, Any]:
    resolved_season, resolved_week = await _resolve_season_week(season, week)
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
    salaries.store(nfl_slate.week_key(resolved_season, resolved_week), rows)
    return {"season": resolved_season, "week": resolved_week, "players_loaded": len(rows)}


@router.get("/salaries")
async def get_salaries(season: int | None = Query(None), week: int | None = Query(None)) -> dict[str, Any]:
    resolved_season, resolved_week = await _resolve_season_week(season, week)
    rows = salaries.load(nfl_slate.week_key(resolved_season, resolved_week))
    return {"season": resolved_season, "week": resolved_week, "loaded": bool(rows), "players": rows}


@router.post("/projections")
async def upload_projections(
    season: int | None = Query(None),
    week: int | None = Query(None),
    file: UploadFile = File(..., description="RotoWire player-pool CSV export"),
) -> dict[str, Any]:
    resolved_season, resolved_week = await _resolve_season_week(season, week)
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
    projections.store(nfl_slate.week_key(resolved_season, resolved_week), rows)
    return {"season": resolved_season, "week": resolved_week, "players_loaded": len(rows)}


@router.get("/projections")
async def get_projections(season: int | None = Query(None), week: int | None = Query(None)) -> dict[str, Any]:
    resolved_season, resolved_week = await _resolve_season_week(season, week)
    rows = projections.load(nfl_slate.week_key(resolved_season, resolved_week))
    return {"season": resolved_season, "week": resolved_week, "loaded": bool(rows), "players": rows}


@router.post("/lineups")
async def generate_lineups(
    season: int | None = Body(None, embed=True),
    week: int | None = Body(None, embed=True),
    num_lineups: int = Body(1, embed=True, description="How many distinct lineups to build"),
    max_exposure_pct: float | None = Body(
        None, embed=True, description="Cap how often any one player appears across the set"
    ),
    exposure_by_slot: dict[str, float] | None = Body(
        None, embed=True, description="Per-slot exposure cap overrides, e.g. {'WR': 40}"
    ),
    locked_ids: list[str] | None = Body(
        None, embed=True, description="Player ids (DK's own numeric id) that must appear in every lineup"
    ),
    excluded_ids: list[str] | None = Body(
        None, embed=True, description="Player ids removed from the pool entirely"
    ),
    min_salary: int | None = Body(None, embed=True, description="Floor on total lineup salary"),
    min_unique_players: int = Body(
        1, embed=True, description="Minimum number of players that must differ between any two lineups"
    ),
    qb_stack_min: int = Body(
        0, embed=True, description="Force at least this many of the QB's own WR/TE into the same lineup"
    ),
) -> dict[str, Any]:
    """
    Up to `num_lineups` distinct, highest-projected DraftKings Classic
    NFL lineups (QB, RB, RB, WR, WR, WR, TE, FLEX, DST), built from
    whatever salary + projections CSVs are loaded for the week.
    """
    resolved_season, resolved_week = await _resolve_season_week(season, week)
    slate = await nfl_slate.build_slate(resolved_season, resolved_week)
    try:
        result = nfl_optimizer.generate_lineups(
            slate,
            num_lineups=num_lineups,
            max_exposure_pct=max_exposure_pct,
            exposure_by_slot=exposure_by_slot,
            locked_ids=locked_ids,
            excluded_ids=excluded_ids,
            min_salary=min_salary,
            min_unique_players=min_unique_players,
            qb_stack_min=qb_stack_min,
        )
    except nfl_optimizer.OptimizerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"season": resolved_season, "week": resolved_week, **result}
