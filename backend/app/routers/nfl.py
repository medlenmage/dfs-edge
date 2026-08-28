"""HTTP routes for NFL."""

from __future__ import annotations

from datetime import date as date_cls
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Body, File, HTTPException, Query, Response, UploadFile

from app import cache
from app.clients import nfl, rotowire_nfl
from app.services import (
    nfl_contest,
    nfl_optimizer,
    nfl_slate,
    nfl_stack_rating,
    nfl_variance,
    player_match,
    projections,
    salaries,
)

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
    force: bool = Query(False, description="Bypass the FantasyLabs vegas-line cache and pull a fresh read"),
) -> dict[str, Any]:
    """The week's slate: games, Vegas-implied context, weather, and (once uploaded) every rostered player."""
    resolved_season, resolved_week = await _resolve_season_week(season, week)
    return await nfl_slate.build_slate(resolved_season, resolved_week, force_refresh=force)


@router.get("/stacks")
async def get_stack_ratings(
    season: int | None = Query(None, description="e.g. 2026 -- defaults to the current NFL season"),
    week: int | None = Query(None, description="1-18 -- defaults to the current week"),
) -> dict[str, Any]:
    """
    Every team's QB-stack rating for the week, best-first -- Vegas
    environment, real PROE/pass-funnel, and empirically-correlated
    recommended partners (own top pass-catchers + a bring-back).
    Requires a DK salary CSV loaded for the week (same as /lineups).
    """
    resolved_season, resolved_week = await _resolve_season_week(season, week)
    return await nfl_stack_rating.build_stack_ratings(resolved_season, resolved_week)


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


# Same trial count and batch-cache TTL as the MLB side's own
# /contest-entries-simulated -- always run, not user-configurable, so
# results are directly comparable run to run.
_NFL_SIM_TRIALS = 10_000
_NFL_ENTRIES_PREVIEW_CAP = 200
_NFL_CONTEST_BATCH_TTL = 3600


@router.get("/contest-types")
async def get_contest_types() -> dict[str, Any]:
    """Named contest presets the field generator can build against --
    same DK archetypes contest.py's MLB version uses."""
    return {"contest_types": nfl_contest.CONTEST_TYPES}


@router.post("/contest-entries")
async def build_contest_entries(
    season: int | None = Body(None, embed=True),
    week: int | None = Body(None, embed=True),
    contest_type: str = Body(..., embed=True, description="One of GET /contest-types' keys"),
    num_lineups: int = Body(..., embed=True, description=f"How many of your own entries to build, up to {nfl_contest.MAX_USER_LINEUPS:,}"),
    max_exposure_pct: float | None = Body(
        None, embed=True, description="Cap how often any one player appears across the whole batch"
    ),
    field_size: int | None = Body(
        None, embed=True, description="Override the preset's real contest size (entries) -- must be >= num_lineups"
    ),
    sample_size: int | None = Body(
        None, embed=True, description="How many synthetic opponent lineups to actually build (capped)"
    ),
    min_salary: int = Body(0, embed=True, description="Floor on each entry's total salary"),
    max_salary: int = Body(
        nfl_optimizer.SALARY_CAP, embed=True, description="Ceiling on each entry's total salary"
    ),
    allow_duplicates: bool = Body(
        False, embed=True,
        description="Allow exact duplicate entries in the batch (a real GPP move). Each entry reports duplicate_count.",
    ),
    field_sharpness: str = Body(
        "marquee", embed=True,
        description="How sharp the simulated opponent field is: 'low', 'marquee' (default), or 'high'.",
    ),
) -> dict[str, Any]:
    """
    The deterministic mass multi-entry contest generator -- build up to
    `num_lineups` of your own entries for a named contest type, ranked
    against a simulated opponent field's *projected* points. See
    POST /contest-entries-simulated for the real Monte Carlo alternative.
    """
    resolved_season, resolved_week = await _resolve_season_week(season, week)
    slate = await nfl_slate.build_slate(resolved_season, resolved_week)
    try:
        result = await nfl_contest.build_contest_entries(
            slate, contest_type, num_lineups,
            season=nfl.PRIOR_SEASON,
            max_exposure_pct=max_exposure_pct, field_size=field_size, sample_size=sample_size,
            min_salary=min_salary, max_salary=max_salary,
            allow_duplicates=allow_duplicates, field_sharpness=field_sharpness,
        )
    except nfl_contest.ContestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    full_entries = result.pop("entries")
    full_results = result["results"]
    batch_id = uuid4().hex
    cache.put(
        f"nfl_contest_batch:{batch_id}",
        {"entries": full_entries, "results": full_results},
        _NFL_CONTEST_BATCH_TTL,
    )
    result["batch_id"] = batch_id
    result["results"] = full_results[:_NFL_ENTRIES_PREVIEW_CAP]
    result["sample_entries"] = full_entries[:_NFL_ENTRIES_PREVIEW_CAP]
    return {"season": resolved_season, "week": resolved_week, **result}


@router.post("/contest-entries-simulated")
async def build_contest_entries_simulated(
    season: int | None = Body(None, embed=True),
    week: int | None = Body(None, embed=True),
    contest_type: str = Body(..., embed=True, description="One of GET /contest-types' keys"),
    num_lineups: int = Body(..., embed=True, description=f"How many of your own entries to build, up to {nfl_contest.MAX_USER_LINEUPS:,}"),
    max_exposure_pct: float | None = Body(
        None, embed=True, description="Cap how often any one player appears across the whole batch"
    ),
    field_size: int | None = Body(
        None, embed=True, description="Override the preset's real contest size (entries) -- must be >= num_lineups"
    ),
    sample_size: int | None = Body(
        None, embed=True, description="How many synthetic opponent lineups to actually build (capped)"
    ),
    min_salary: int = Body(0, embed=True, description="Floor on each entry's total salary"),
    max_salary: int = Body(
        nfl_optimizer.SALARY_CAP, embed=True, description="Ceiling on each entry's total salary"
    ),
    allow_duplicates: bool = Body(
        False, embed=True,
        description="Allow exact duplicate entries in the batch. Duplicates' cash probability/payout/ROI are averaged across the tied group, matching DK's real tie-payout split.",
    ),
    self_play: bool = Body(
        False, embed=True,
        description="Rank this batch against ITSELF instead of a separately-sampled public field -- every lineup competing against every other lineup you generated, in the same simulated trial.",
    ),
    field_sharpness: str = Body(
        "marquee", embed=True,
        description="How sharp the simulated opponent field is: 'low', 'marquee' (default), or 'high'. Ignored when self_play=True.",
    ),
    first_place_pct: float | None = Body(
        None, embed=True,
        description="Override the contest preset's own percent-to-first for this run. Defaults to the preset's own value when omitted.",
    ),
) -> dict[str, Any]:
    """
    Like POST /contest-entries, but ranks the batch against a genuine
    Monte Carlo simulation (nfl_contest.build_contest_entries_simulated())
    using real per-player/DST outcome pools bootstrapped from
    nfl.PRIOR_SEASON's actual 2025 game logs, correlated by team/opponent
    (see nfl_variance.py's own module docstring) -- each entry's
    cash_probability_pct is the real fraction of simulated trials it
    lands in the paid zone, not a point estimate.
    """
    resolved_season, resolved_week = await _resolve_season_week(season, week)
    slate = await nfl_slate.build_slate(resolved_season, resolved_week)
    try:
        result = await nfl_contest.build_contest_entries_simulated(
            slate, contest_type, num_lineups,
            season=nfl.PRIOR_SEASON, num_trials=_NFL_SIM_TRIALS,
            max_exposure_pct=max_exposure_pct, field_size=field_size, sample_size=sample_size,
            min_salary=min_salary, max_salary=max_salary,
            allow_duplicates=allow_duplicates, self_play=self_play,
            field_sharpness=field_sharpness, first_place_pct=first_place_pct,
        )
    except nfl_contest.ContestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    full_entries = result.pop("entries")
    full_results = result["results"]
    batch_id = uuid4().hex
    cache.put(
        f"nfl_contest_batch:{batch_id}",
        {"entries": full_entries, "results": full_results},
        _NFL_CONTEST_BATCH_TTL,
    )
    result["batch_id"] = batch_id
    result["results"] = full_results[:_NFL_ENTRIES_PREVIEW_CAP]
    result["sample_entries"] = full_entries[:_NFL_ENTRIES_PREVIEW_CAP]
    return {"season": resolved_season, "week": resolved_week, **result}


@router.get("/contest-entries/{batch_id}/csv")
async def download_contest_entries_csv(batch_id: str) -> Response:
    """
    The full batch from a POST /contest-entries or
    /contest-entries-simulated call, as a CSV -- one row per entry, one
    column per NFL roster slot (name), plus the primary/secondary
    stack and bring-back facts and the rank/cash/payout estimate from
    that batch's evaluation. The NFL sibling of MLB's own contest-
    entries CSV download; meant for a batch bigger than the 200-entry
    JSON preview covers.
    """
    cached = cache.get(f"nfl_contest_batch:{batch_id}")
    if not cached:
        raise HTTPException(
            status_code=404,
            detail="That batch has expired or doesn't exist -- generate a new one and download again.",
        )
    csv_text = nfl_contest.lineups_to_csv(cached["entries"], results=cached["results"])
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="nfl-contest-entries-{batch_id}.csv"'},
    )
