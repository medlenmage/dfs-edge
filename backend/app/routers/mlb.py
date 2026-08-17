"""HTTP routes for MLB."""

from __future__ import annotations

from datetime import date as date_cls
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Body, File, HTTPException, Query, Response, UploadFile

from app import cache
from app.services import analysis, contest, dk_entries, lineup_export, mlb_slate, optimizer, projections, salaries

# How long a generated contest-entries batch stays downloadable as CSV
# after the fact -- long enough to cover "generate, look it over, then
# download," short enough not to pile up disk cache forever.
_CONTEST_BATCH_TTL = 3600

# Every simulated run uses the same trial count -- not user-configurable,
# so results are always directly comparable across runs and there's no
# "how many trials should I pick" decision to make.
_SIM_TRIALS = 10_000

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

    If the file's own SAL column has salary data (RotoWire's
    player-pool export pulls it straight from DK) and no salary file
    has been uploaded for this date yet, that salary data seeds the
    salary store too -- one upload instead of two. A real DK upload,
    now or later, still takes priority and is never overwritten by a
    projections re-upload.
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
    result = {"date": day, "players_loaded": len(rows)}

    if not salaries.load(day):
        derived = salaries.from_rotowire_rows(rows)
        if derived:
            salaries.store(day, derived)
            result["salaries_derived"] = len(derived)

    return result


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
    exposure_by_slot: dict[str, float] | None = Body(
        None, embed=True, description="Per-slot exposure cap overrides, e.g. {'OF': 40}"
    ),
    team_exposure_cap: dict[str, float] | None = Body(
        None, embed=True, description="Cap how often a team is used AS THE STACK, e.g. {'NYY': 30}"
    ),
    locked_ids: list[int] | None = Body(
        None, embed=True, description="Player ids that must appear in every generated lineup"
    ),
    excluded_ids: list[int] | None = Body(
        None, embed=True, description="Player ids removed from the pool entirely"
    ),
    min_salary: int | None = Body(
        None, embed=True, description="Floor on total lineup salary, symmetric with the $50,000 cap"
    ),
    min_unique_players: int = Body(
        1, embed=True, description="Minimum number of players that must differ between any two lineups"
    ),
    min_teams_per_lineup: int | None = Body(
        None, embed=True, description="Minimum distinct teams among a single lineup's 10 players"
    ),
    max_teams_per_lineup: int | None = Body(
        None, embed=True, description="Maximum distinct teams among a single lineup's 10 players"
    ),
    one_off_group_ids: list[int] | None = Body(
        None, embed=True, description="Whitelist of player ids eligible for the leftover one-off hitter slots in a partial stack shape"
    ),
    one_off_min_salary: int | None = Body(
        None, embed=True, description="Minimum salary for a player to fill a one-off slot in a partial stack shape"
    ),
    one_off_max_salary: int | None = Body(
        None, embed=True, description="Maximum salary for a player to fill a one-off slot in a partial stack shape"
    ),
    min_ownership_pct: float | None = Body(
        None, embed=True, description="Floor on a lineup's cumulative RotoWire ownership%"
    ),
    max_ownership_pct: float | None = Body(
        None, embed=True, description="Ceiling on a lineup's cumulative RotoWire ownership%"
    ),
    included_game_pks: list[int] | None = Body(
        None, embed=True, description="Restrict the pool to these games only -- e.g. to match a specific DK slate"
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
            exposure_by_slot=exposure_by_slot,
            team_exposure_cap=team_exposure_cap,
            locked_ids=locked_ids,
            excluded_ids=excluded_ids,
            min_salary=min_salary,
            min_unique_players=min_unique_players,
            min_teams_per_lineup=min_teams_per_lineup,
            max_teams_per_lineup=max_teams_per_lineup,
            one_off_group_ids=one_off_group_ids,
            one_off_min_salary=one_off_min_salary,
            one_off_max_salary=one_off_max_salary,
            min_ownership_pct=min_ownership_pct,
            max_ownership_pct=max_ownership_pct,
            included_game_pks=included_game_pks,
        )
    except optimizer.OptimizerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"date": day, **result}


@router.get("/contest-types")
async def get_contest_types() -> dict[str, Any]:
    """Named contest presets the field generator can build against."""
    return {"contest_types": contest.CONTEST_TYPES}


@router.post("/contest-field")
async def build_contest_field(
    date: str | None = Body(None, embed=True),
    contest_type: str = Body(..., embed=True, description="One of GET /contest-types' keys"),
    lineups: list[dict[str, Any]] = Body(
        ..., embed=True, description="Lineup objects as returned by POST /lineups' 'lineups' array"
    ),
    field_size: int | None = Body(
        None, embed=True, description="Override the preset's real contest size (entries)"
    ),
    sample_size: int | None = Body(
        None, embed=True, description="How many synthetic field lineups to actually build (capped)"
    ),
    included_game_pks: list[int] | None = Body(
        None, embed=True, description="Restrict the field's player pool to these games -- pass the same selection used to build `lineups`"
    ),
) -> dict[str, Any]:
    """
    Build a synthetic public field for a named contest type, sampled by
    RotoWire ownership%, and rank the given lineup(s) against it.

    Not a lineup simulator -- there's no player-outcome variance model
    yet, so this ranks by projected points against the field's
    projected points, not a distribution of real-world outcomes. Still
    useful for chalk exposure and roughly where a build would land.
    """
    day = date or date_cls.today().isoformat()
    slate = await mlb_slate.build_slate(day)
    try:
        result = contest.build_contest_field(
            slate,
            contest_type,
            lineups,
            field_size=field_size,
            sample_size=sample_size,
            included_game_pks=included_game_pks,
        )
    except contest.ContestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"date": day, **result}


@router.post("/contest-entries")
async def build_contest_entries(
    date: str | None = Body(None, embed=True),
    contest_type: str = Body(..., embed=True, description="One of GET /contest-types' keys"),
    num_lineups: int = Body(..., embed=True, description=f"How many of your own entries to build, up to {contest.MAX_USER_LINEUPS:,}"),
    max_exposure_pct: float | None = Body(
        None, embed=True, description="Cap how often any one player appears across the whole batch"
    ),
    field_size: int | None = Body(
        None, embed=True, description="Override the preset's real contest size (entries) -- must be >= num_lineups"
    ),
    sample_size: int | None = Body(
        None, embed=True, description="How many synthetic opponent lineups to actually build (capped)"
    ),
    included_game_pks: list[int] | None = Body(
        None, embed=True, description="Restrict the pool to these games only -- e.g. to match a specific DK slate"
    ),
) -> dict[str, Any]:
    """
    The mass multi-entry contest generator: build up to MAX_USER_LINEUPS
    of your own entries for a named contest type in one request, then
    rank the whole batch against a simulated opponent field for
    cash-rate and payout economics.

    Separate from both the small/exact optimizer (POST /lineups, MILP,
    capped at 150) and the single-lineup field test (POST
    /contest-field) -- this is the fast, large-scale path: entries are
    built by randomized construction weighted toward projected points,
    not an exact solve, so they're individually strong and mutually
    distinct rather than provably optimal. Requires both a DraftKings
    salary CSV and a RotoWire projections CSV loaded for the date, same
    as the optimizer.

    The full batch is cached under a `batch_id` returned in the
    response -- GET /contest-entries/{batch_id}/csv downloads all of
    it (not just the `sample_entries`/`results` preview capped at 200
    below), e.g. to hand off to an external simulator.
    """
    day = date or date_cls.today().isoformat()
    slate = await mlb_slate.build_slate(day)
    try:
        result = contest.build_contest_entries(
            slate,
            contest_type,
            num_lineups,
            max_exposure_pct=max_exposure_pct,
            field_size=field_size,
            sample_size=sample_size,
            included_game_pks=included_game_pks,
        )
    except contest.ContestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    full_entries = result.pop("entries")
    full_results = result["results"]  # keep the full list cached...
    batch_id = uuid4().hex
    cache.put(
        f"contest_batch:{batch_id}",
        {"entries": full_entries, "results": full_results},
        _CONTEST_BATCH_TTL,
    )

    result["batch_id"] = batch_id
    result["results"] = full_results[:200]  # ...but only preview it here
    result["sample_entries"] = full_entries[:200]
    return {"date": day, **result}


@router.post("/contest-entries-simulated")
async def build_contest_entries_simulated(
    date: str | None = Body(None, embed=True),
    contest_type: str = Body(..., embed=True, description="One of GET /contest-types' keys"),
    num_lineups: int = Body(..., embed=True, description=f"How many of your own entries to build, up to {contest.MAX_USER_LINEUPS:,}"),
    max_exposure_pct: float | None = Body(
        None, embed=True, description="Cap how often any one player appears across the whole batch"
    ),
    field_size: int | None = Body(
        None, embed=True, description="Override the preset's real contest size (entries) -- must be >= num_lineups"
    ),
    sample_size: int | None = Body(
        None, embed=True, description="How many synthetic opponent lineups to actually build (capped)"
    ),
    included_game_pks: list[int] | None = Body(
        None, embed=True, description="Restrict the pool to these games only -- e.g. to match a specific DK slate"
    ),
) -> dict[str, Any]:
    """
    Like POST /contest-entries, but ranks the batch against a genuine
    Monte Carlo simulation (contest.build_contest_entries_simulated())
    instead of a single projected-points snapshot against the field --
    each entry's cash_probability_pct is the real fraction of simulated
    trials it lands in the paid zone, with an expected_payout range
    (10th/90th percentile), not a point estimate. Always runs
    _SIM_TRIALS trials -- not user-configurable, so results are always
    directly comparable run to run. Slower than /contest-entries
    (fetches every player's own real outcome pool, then runs 10,000
    simulated realities), so this is a separate opt-in endpoint rather
    than a flag on the fast deterministic default.
    """
    day = date or date_cls.today().isoformat()
    season = int(day[:4])
    slate = await mlb_slate.build_slate(day)
    try:
        result = await contest.build_contest_entries_simulated(
            slate,
            contest_type,
            num_lineups,
            season=season,
            num_trials=_SIM_TRIALS,
            max_exposure_pct=max_exposure_pct,
            field_size=field_size,
            sample_size=sample_size,
            included_game_pks=included_game_pks,
        )
    except contest.ContestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    full_entries = result.pop("entries")
    full_results = result["results"]
    batch_id = uuid4().hex
    cache.put(
        f"contest_batch:{batch_id}",
        {"entries": full_entries, "results": full_results},
        _CONTEST_BATCH_TTL,
    )

    result["batch_id"] = batch_id
    result["results"] = full_results[:200]
    result["sample_entries"] = full_entries[:200]
    return {"date": day, **result}


@router.get("/contest-entries/{batch_id}/csv")
async def download_contest_entries_csv(batch_id: str) -> Response:
    """
    The full batch from a POST /contest-entries call, as a CSV: one row
    per entry, one column-group per DK roster slot (name, team, salary,
    projected points, ownership%), plus the rank/cash/payout estimate
    from that batch's field evaluation. Meant for handing off to
    something outside this app -- a Monte Carlo simulator, a
    spreadsheet, another Claude session working from the file -- since
    the JSON response only ever previews the first 200 of what can be
    a 10,000-entry batch.
    """
    cached = cache.get(f"contest_batch:{batch_id}")
    if not cached:
        raise HTTPException(
            status_code=404,
            detail="That batch has expired or doesn't exist -- generate a new one and download again.",
        )
    csv_text = lineup_export.lineups_to_csv(cached["entries"], results=cached["results"])
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="contest-entries-{batch_id}.csv"'},
    )


@router.post("/dk-entries")
async def upload_dk_entries(
    date: str | None = Query(None, description="Slate date this file is for, defaults to today"),
    file: UploadFile = File(..., description="DraftKings bulk entries export/upload CSV"),
) -> dict[str, Any]:
    """
    Upload a DraftKings entries CSV -- the same "bulk entries" export/
    upload file DK's own site gives you, one row per contest entry you
    reserved or already built a lineup for. Cached until you upload a
    new one for the same date, same as the salary/projections uploads.

    Returns the distinct contests found in the file (a single export
    can span more than one, if you entered several the same day) so
    the frontend can offer a picker -- POST /dk-entries/simulate takes
    the contest_id from here.
    """
    day = date or date_cls.today().isoformat()
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Couldn't read that as text: {exc}") from exc

    entries = dk_entries.parse_entries_csv(text)
    if not entries:
        raise HTTPException(
            status_code=400,
            detail="No entries found in that file -- is it a DraftKings bulk entries export?",
        )
    dk_entries.store(day, text)
    return {"date": day, "contests": dk_entries.contest_summary(entries)}


@router.post("/dk-entries/simulate")
async def simulate_dk_entries(
    date: str | None = Body(None, embed=True),
    contest_id: str = Body(..., embed=True, description="One of GET /dk-entries's returned contest_id values"),
    field_size: int = Body(..., embed=True, description="The real contest's total entry count"),
    prize_pool: float = Body(..., embed=True, description="The real contest's total prize pool"),
    first_place_pct: float = Body(..., embed=True, description="% of the prize pool 1st place wins"),
    payout_pct: float = Body(0.20, embed=True, description="Fraction of field_size that cashes"),
    shape: str = Body("top_heavy", embed=True, description="'top_heavy' (GPP) or 'flat' (double-up/50-50)"),
    sample_size: int | None = Body(
        None, embed=True, description="How many synthetic opponent lineups to actually build (capped)"
    ),
    included_game_pks: list[int] | None = Body(
        None, embed=True, description="Restrict the opponent field to these games only"
    ),
) -> dict[str, Any]:
    """
    Simulate the lineups you actually built for one real contest from
    an uploaded DK entries file (POST /dk-entries), against that
    contest's real economics -- entry fee comes straight from the
    file, prize_pool/first_place_pct/field_size/payout_pct/shape are
    hand-entered since a bulk entries export has no payout-table data
    at all. Always runs 10,000 trials, same as /contest-entries-simulated.
    """
    day = date or date_cls.today().isoformat()
    text = dk_entries.load(day)
    if not text:
        raise HTTPException(
            status_code=404,
            detail="No DK entries file uploaded for that date yet -- upload one via POST /dk-entries first.",
        )
    season = int(day[:4])
    slate = await mlb_slate.build_slate(day)
    resolved, warnings = dk_entries.resolve_entries(text, slate, contest_id=contest_id)
    if not resolved:
        raise HTTPException(
            status_code=400,
            detail="None of that contest's entries could be simulated -- every one was either an "
            "empty reservation or referenced a player not in today's loaded salary/projections pool.",
        )

    try:
        result = await contest.build_dk_entries_simulated(
            slate,
            resolved,
            season=season,
            field_size=field_size,
            prize_pool=prize_pool,
            first_place_pct=first_place_pct,
            payout_pct=payout_pct,
            shape=shape,
            num_trials=_SIM_TRIALS,
            sample_size=sample_size,
            included_game_pks=included_game_pks,
        )
    except contest.ContestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    full_entries = result.pop("entries")
    full_results = result["results"]
    batch_id = uuid4().hex
    cache.put(
        f"contest_batch:{batch_id}",
        {"entries": full_entries, "results": full_results},
        _CONTEST_BATCH_TTL,
    )

    result["batch_id"] = batch_id
    result["results"] = full_results[:200]
    result["sample_entries"] = full_entries[:200]
    result["warnings"] = warnings
    return {"date": day, **result}
