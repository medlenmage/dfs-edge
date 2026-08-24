"""HTTP routes for NFL."""

from __future__ import annotations

from datetime import date as date_cls
from typing import Any

from fastapi import APIRouter, Body, File, HTTPException, Query, UploadFile

from app.clients import nfl, rotowire_nfl
from app.services import nfl_optimizer, nfl_slate, nfl_variance, player_match, projections, salaries

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


@router.post("/projections/refresh-rotowire")
async def refresh_rotowire_projections(
    season: int | None = Query(None),
    week: int | None = Query(None),
    refresh: bool = Body(
        False, embed=True,
        description="Bypass the cache and re-pull live from RotoWire -- use close to lock for newly confirmed lineups",
    ),
) -> dict[str, Any]:
    """
    Pull RotoWire's own live NFL optimizer player pool directly from
    their site (clients/rotowire_nfl.py) instead of a manual CSV
    download/upload -- their main Classic DK slate. Unlike MLB's
    equivalent endpoint, this does NOT also derive DK salaries from the
    same pull: RotoWire's NFL export has no DK numeric player id, and
    NFL's optimizer output genuinely needs one (see
    salaries.from_rotowire_rows()'s own docstring for why that helper
    is MLB-only). Upload or refresh a real DK salary CSV separately.
    """
    resolved_season, resolved_week = await _resolve_season_week(season, week)
    try:
        slate = await rotowire_nfl.get_current_slate(force=refresh)
        rows = await rotowire_nfl.get_slate_players(slate["slateID"], force=refresh)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Couldn't reach RotoWire: {exc}") from exc
    if not rows:
        raise HTTPException(
            status_code=400,
            detail="No players found in RotoWire's live slate -- it may not be posted yet.",
        )

    week_key = nfl_slate.week_key(resolved_season, resolved_week)
    projections.store(week_key, rows)
    result: dict[str, Any] = {"season": resolved_season, "week": resolved_week, "players_loaded": len(rows)}

    existing_salaries = salaries.load(week_key)
    if existing_salaries:
        # A DK salary CSV is already loaded for this week -- run the
        # same name-matching used for the live slate now, so a
        # RotoWire/DraftKings spelling mismatch shows up immediately
        # instead of silently leaving that player's projection blank.
        lookup = salaries.build_lookup(existing_salaries)
        bad = player_match.unmatched(rows, lookup, fuzzy=True)
        result["matched_to_slate"] = len(rows) - len(bad)
        if bad:
            result["unmatched"] = bad
    return result


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
    simulate: bool = Body(
        False, embed=True,
        description="Run a Monte Carlo simulation on the generated lineups and attach floor/median/ceiling",
    ),
    num_trials: int = Body(2000, embed=True, description="Simulated trials per lineup, if simulate=true"),
) -> dict[str, Any]:
    """
    Up to `num_lineups` distinct, highest-projected DraftKings Classic
    NFL lineups (QB, RB, RB, WR, WR, WR, TE, FLEX, DST), built from
    whatever salary + projections CSVs are loaded for the week.

    `simulate=true` runs each generated lineup through
    nfl_variance.simulate_batch() -- real per-player outcome pools
    bootstrapped from nfl.PRIOR_SEASON's actual game logs (2025 as of
    writing, not the week being drafted -- that season hasn't been
    played yet), correlated by team/opponent (see nfl_variance.py's own
    module docstring for exactly what that does and doesn't model) --
    and attaches a `simulated` floor/median/ceiling/mean to each
    lineup, a real data-driven range instead of only the single
    `projected_points` point estimate.
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

    if simulate and result["lineups"]:
        try:
            pools = await nfl_variance.player_pools_for_entries(result["lineups"], nfl.PRIOR_SEASON)
            sims = nfl_variance.simulate_batch(result["lineups"], pools, num_trials=num_trials)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Couldn't simulate: {exc}") from exc
        for lineup, trial_row in zip(result["lineups"], sims):
            ordered = sorted(trial_row.tolist())
            n = len(ordered)
            lineup["simulated"] = {
                "data_season": nfl.PRIOR_SEASON,
                "floor": round(ordered[round(0.10 * (n - 1))], 2),
                "median": round(ordered[round(0.50 * (n - 1))], 2),
                "ceiling": round(ordered[round(0.90 * (n - 1))], 2),
                "mean": round(sum(ordered) / n, 2),
            }
    return {"season": resolved_season, "week": resolved_week, **result}
