"""HTTP routes for MLB."""

from __future__ import annotations

from datetime import date as date_cls
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Body, File, HTTPException, Query, Response, UploadFile

import asyncio

from app import cache, history_db
from app.clients import draftkings
from app.services import (
    analysis,
    contest,
    contest_results,
    dk_entries,
    lineup_export,
    mlb_slate,
    optimizer,
    player_match,
    projections,
    salaries,
)

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
    inhouse: bool = Query(
        False,
        description=(
            "Also compute in-house FPTS projections. Off by default -- "
            "real per-player game-log fetches for the whole slate, "
            "adds real latency to the first call of the day."
        ),
    ),
) -> dict[str, Any]:
    """
    The full daily slate: games, environment, pitchers, and hitter edges.

    This is the one endpoint the dashboard needs.
    """
    day = date or date_cls.today().isoformat()
    try:
        return await mlb_slate.build_slate(
            day, force_refresh=refresh, include_hitters=hitters, include_inhouse=inhouse
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


@router.get("/dk-slates")
async def get_dk_slates(
    date: str | None = Query(None, description="Slate date, defaults to today"),
    refresh: bool = Query(False, description="Bypass the 15-minute cache and re-pull from DraftKings"),
) -> dict[str, Any]:
    """
    Every real Classic MLB slate DraftKings has live for a date (Early,
    Main, Night, single-game pools, ...), pulled from DraftKings' own
    public lobby feed -- no manual salary CSV needed. Pick one and pass
    its draft_group_id to POST /dk-slates/load to pull that specific
    slate's players and salaries.
    """
    day = date or date_cls.today().isoformat()
    try:
        slates = await draftkings.get_slates(day, force=refresh)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Couldn't reach DraftKings: {exc}") from exc
    return {"date": day, "slates": slates}


@router.post("/dk-slates/load")
async def load_dk_slate(
    date: str | None = Body(None, embed=True),
    draft_group_id: int = Body(..., embed=True, description="From GET /dk-slates' draft_group_id"),
    refresh: bool = Body(
        False, embed=True, description="Bypass the 10-minute cache and re-pull live -- use for late scratches/swaps close to lock"
    ),
) -> dict[str, Any]:
    """
    Pull players + salaries for one specific DraftKings slate directly
    from their live API and store it the same way a manual salary CSV
    upload would -- everything downstream (in_slate detection, the
    optimizer, the contest generator) works unchanged either way.
    """
    day = date or date_cls.today().isoformat()
    try:
        rows = await draftkings.get_draftables(draft_group_id, force=refresh)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Couldn't reach DraftKings: {exc}") from exc
    if not rows:
        raise HTTPException(
            status_code=400,
            detail="No players found for that slate -- it may not be live yet, or the draft_group_id is stale (re-fetch GET /dk-slates).",
        )
    salaries.store(day, rows)
    return {"date": day, "draft_group_id": draft_group_id, "players_loaded": len(rows)}


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
    asyncio.create_task(history_db.archive_slate_projections(day, rows))
    result = {"date": day, "players_loaded": len(rows)}

    existing_salaries = salaries.load(day)
    if existing_salaries:
        # A DK slate is already loaded for this date -- run the same
        # name-matching used for the live slate now, at upload time, so
        # a RotoWire/DraftKings spelling mismatch (nicknames, accents,
        # a real typo) shows up immediately instead of silently leaving
        # that player's projection blank on the Hitters/Pitchers tabs.
        lookup = salaries.build_lookup(existing_salaries)
        bad = player_match.unmatched(rows, lookup, fuzzy=True)
        result["matched_to_slate"] = len(rows) - len(bad)
        if bad:
            result["unmatched"] = bad
    else:
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
    projection_source: str = Body(
        "rotowire", embed=True, description="Which FPTS/ownership numbers to optimize against: 'rotowire' or 'inhouse'"
    ),
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
        optimizer.DEFAULT_MIN_SALARY,
        embed=True,
        description="Floor on total lineup salary, symmetric with the $50,000 cap. Defaults to $47,000 -- pass 0 to disable.",
    ),
    max_salary: int | None = Body(
        None, embed=True, description="Ceiling on total lineup salary, below the fixed $50,000 cap"
    ),
    min_unique_players: int = Body(
        1, embed=True, description="Minimum number of players that must differ between any two lineups. 0 allows exact duplicates"
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
    MLB lineups, built from whatever salary CSV is loaded and either
    RotoWire's uploaded projections (default) or this app's own
    in-house FPTS/ownership model (`projection_source="inhouse"`).
    """
    day = date or date_cls.today().isoformat()
    slate = await mlb_slate.build_slate(day, include_inhouse=(projection_source == "inhouse"))
    try:
        result = optimizer.generate_lineups(
            slate,
            num_lineups=num_lineups,
            projection_source=projection_source,
            stack_groups=stack_groups,
            stack_teams=stack_teams,
            max_exposure_pct=max_exposure_pct,
            exposure_by_slot=exposure_by_slot,
            team_exposure_cap=team_exposure_cap,
            locked_ids=locked_ids,
            excluded_ids=excluded_ids,
            min_salary=min_salary,
            max_salary=max_salary,
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


@router.post("/late-swap")
async def late_swap(
    date: str | None = Body(None, embed=True),
    picks: list[dict[str, Any]] = Body(
        ...,
        embed=True,
        description=(
            "Exactly 10 entries in fixed roster order (P, P, C, 1B, 2B, 3B, SS, OF, OF, OF), "
            "each {'player_id': int, 'game_pk': int} -- game_pk from the SAME slate response "
            "the lineup was originally built from"
        ),
    ),
    projection_source: str = Body(
        "rotowire", embed=True, description="Which FPTS/ownership numbers to fill open slots with: 'rotowire' or 'inhouse'"
    ),
) -> dict[str, Any]:
    """
    Re-optimize an already-built lineup's still-open slots (games that
    haven't started yet) -- exactly what real DK late swap allows.
    Players whose games have already locked stay exactly as they were;
    open slots get refilled from the CURRENT slate, so a scratch or a
    last-minute lineup change gets picked up without rebuilding the
    whole lineup from scratch.
    """
    day = date or date_cls.today().isoformat()
    slate = await mlb_slate.build_slate(day, include_inhouse=(projection_source == "inhouse"))
    try:
        result = optimizer.late_swap(slate, picks, projection_source=projection_source)
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
    projection_source: str = Body(
        "rotowire", embed=True, description="Which ownership% to sample the field by: 'rotowire' or 'inhouse'"
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
    min_salary: int = Body(
        optimizer.DEFAULT_MIN_SALARY,
        embed=True,
        description="Floor on each sampled field lineup's salary. Defaults to $47,000 -- pass 0 to disable.",
    ),
    max_salary: int = Body(
        optimizer.SALARY_CAP, embed=True, description="Ceiling on each sampled field lineup's salary"
    ),
) -> dict[str, Any]:
    """
    Build a synthetic public field for a named contest type, sampled by
    ownership% (RotoWire's or this app's own in-house model), and rank
    the given lineup(s) against it.

    Not a lineup simulator -- there's no player-outcome variance model
    yet, so this ranks by projected points against the field's
    projected points, not a distribution of real-world outcomes. Still
    useful for chalk exposure and roughly where a build would land.
    """
    day = date or date_cls.today().isoformat()
    # Always includes in-house data (regardless of projection_source) so
    # optimizer.build_player_pool()'s ownership fallback has something
    # real to use whenever RotoWire's own export doesn't cover a player
    # -- otherwise that player silently floors to ~0% owned in the
    # sampled field, which is closer to random than realistic.
    slate = await mlb_slate.build_slate(day, include_inhouse=True)
    try:
        result = contest.build_contest_field(
            slate,
            contest_type,
            lineups,
            projection_source=projection_source,
            field_size=field_size,
            sample_size=sample_size,
            included_game_pks=included_game_pks,
            min_salary=min_salary,
            max_salary=max_salary,
        )
    except contest.ContestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"date": day, **result}


@router.post("/contest-entries")
async def build_contest_entries(
    date: str | None = Body(None, embed=True),
    contest_type: str = Body(..., embed=True, description="One of GET /contest-types' keys"),
    num_lineups: int = Body(..., embed=True, description=f"How many of your own entries to build, up to {contest.MAX_USER_LINEUPS:,}"),
    projection_source: str = Body(
        "rotowire", embed=True, description="Which FPTS/ownership numbers to build against: 'rotowire' or 'inhouse'"
    ),
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
    min_salary: int = Body(
        optimizer.DEFAULT_MIN_SALARY,
        embed=True,
        description="Floor on each entry's salary. Defaults to $47,000 -- pass 0 to disable.",
    ),
    max_salary: int = Body(
        optimizer.SALARY_CAP, embed=True, description="Ceiling on each entry's salary"
    ),
    allow_duplicates: bool = Body(
        False,
        embed=True,
        description="Allow exact duplicate entries in the batch (a real GPP move -- entering a signature build multiple times). Each entry reports duplicate_count",
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
    distinct rather than provably optimal. Requires a DraftKings salary
    CSV and either a RotoWire projections CSV or the in-house model
    loaded for the date, same as the optimizer.

    The full batch is cached under a `batch_id` returned in the
    response -- GET /contest-entries/{batch_id}/csv downloads all of
    it (not just the `sample_entries`/`results` preview capped at 200
    below), e.g. to hand off to an external simulator.
    """
    day = date or date_cls.today().isoformat()
    # Always includes in-house data -- see build_contest_field's own
    # comment above; the opponent field this batch gets ranked against
    # needs a real ownership fallback wherever RotoWire's export doesn't
    # cover a player.
    slate = await mlb_slate.build_slate(day, include_inhouse=True)
    try:
        result = contest.build_contest_entries(
            slate,
            contest_type,
            num_lineups,
            projection_source=projection_source,
            max_exposure_pct=max_exposure_pct,
            field_size=field_size,
            sample_size=sample_size,
            included_game_pks=included_game_pks,
            min_salary=min_salary,
            max_salary=max_salary,
            allow_duplicates=allow_duplicates,
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
    projection_source: str = Body(
        "rotowire", embed=True, description="Which FPTS/ownership numbers to build against: 'rotowire' or 'inhouse'"
    ),
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
    min_salary: int = Body(
        optimizer.DEFAULT_MIN_SALARY,
        embed=True,
        description="Floor on each entry's salary. Defaults to $47,000 -- pass 0 to disable.",
    ),
    max_salary: int = Body(
        optimizer.SALARY_CAP, embed=True, description="Ceiling on each entry's salary"
    ),
    allow_duplicates: bool = Body(
        False,
        embed=True,
        description="Allow exact duplicate entries in the batch (a real GPP move -- entering a signature build multiple times). Duplicates' cash probability/payout/ROI are averaged across the tied group, matching DK's real tie-payout split. Each entry reports duplicate_count",
    ),
    self_play: bool = Body(
        False,
        embed=True,
        description="Rank this batch against ITSELF instead of a separately-sampled public field -- every lineup competing against every other lineup you generated, in the same simulated trial. Use this to see how your own stacks/builds compare to each other; leave off (default) to see how your batch fares against a realistic public field.",
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
    # Always includes in-house data -- see build_contest_field's own
    # comment above; the simulated field this batch gets ranked against
    # needs a real ownership fallback wherever RotoWire's export doesn't
    # cover a player.
    slate = await mlb_slate.build_slate(day, include_inhouse=True)
    try:
        result = await contest.build_contest_entries_simulated(
            slate,
            contest_type,
            num_lineups,
            season=season,
            projection_source=projection_source,
            num_trials=_SIM_TRIALS,
            max_exposure_pct=max_exposure_pct,
            field_size=field_size,
            sample_size=sample_size,
            included_game_pks=included_game_pks,
            min_salary=min_salary,
            max_salary=max_salary,
            allow_duplicates=allow_duplicates,
            self_play=self_play,
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
    field_size: int = Body(
        ..., embed=True, description="The real contest's total entry count -- a representative sample gets simulated and projected onto this size"
    ),
    prize_pool: float = Body(..., embed=True, description="The real contest's total prize pool"),
    first_place_pct: float = Body(..., embed=True, description="% of the prize pool 1st place wins"),
    payout_pct: float = Body(0.20, embed=True, description="Fraction of field_size that cashes"),
    shape: str = Body("top_heavy", embed=True, description="'top_heavy' (GPP) or 'flat' (double-up/50-50)"),
    projection_source: str = Body(
        "rotowire", embed=True, description="Which ownership% to sample the field by: 'rotowire' or 'inhouse'"
    ),
    sample_size: int | None = Body(
        None, embed=True, description="How many lineups to actually simulate as the field sample (capped)"
    ),
    included_game_pks: list[int] | None = Body(
        None, embed=True, description="Restrict the pool to these games only"
    ),
    min_salary: int = Body(
        optimizer.DEFAULT_MIN_SALARY,
        embed=True,
        description="Floor on each sampled field lineup's salary. Defaults to $47,000 -- pass 0 to disable.",
    ),
    max_salary: int = Body(
        optimizer.SALARY_CAP, embed=True, description="Ceiling on each sampled field lineup's salary"
    ),
) -> dict[str, Any]:
    """
    Mirror and simulate a real contest's whole field from an uploaded
    DK entries file (POST /dk-entries): the file's only real job is
    supplying entry_fee (the one economic fact it actually contains).
    This app builds an ownership-weighted representative sample of the
    real contest (ranked against itself, not a separate "your entries"
    batch) and returns every sampled lineup's own simulated cash
    probability/ROI so you can browse the results and pick which ones
    to actually submit yourself. prize_pool/first_place_pct/field_size/
    payout_pct/shape are hand-entered since a bulk entries export has
    no payout-table data or true field size at all. Always runs 10,000
    trials, same as /contest-entries-simulated.
    """
    day = date or date_cls.today().isoformat()
    text = dk_entries.load(day)
    if not text:
        raise HTTPException(
            status_code=404,
            detail="No DK entries file uploaded for that date yet -- upload one via POST /dk-entries first.",
        )
    season = int(day[:4])
    # Always includes in-house data -- see build_contest_field's own
    # comment above; the mirrored field this batch gets ranked against
    # needs a real ownership fallback wherever RotoWire's export doesn't
    # cover a player.
    slate = await mlb_slate.build_slate(day, include_inhouse=True)

    parsed = dk_entries.parse_entries_csv(text)
    contest_row = next((c for c in dk_entries.contest_summary(parsed) if c["contest_id"] == contest_id), None)
    if contest_row is None:
        raise HTTPException(
            status_code=404,
            detail="That contest wasn't found in the uploaded file -- re-upload via POST /dk-entries.",
        )

    try:
        result = await contest.build_dk_entries_simulated(
            slate,
            season=season,
            entry_fee=contest_row["entry_fee"] or 0.0,
            field_size=field_size,
            prize_pool=prize_pool,
            first_place_pct=first_place_pct,
            payout_pct=payout_pct,
            shape=shape,
            projection_source=projection_source,
            num_trials=_SIM_TRIALS,
            sample_size=sample_size,
            included_game_pks=included_game_pks,
            min_salary=min_salary,
            max_salary=max_salary,
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


@router.post("/contest-results")
async def upload_contest_results(
    date: str | None = Query(None, description="Date this contest was played, defaults to today"),
    contest_name: str | None = Query(None, description="A name you recognize this contest by (DK doesn't put one in the file itself)"),
    entry_fee: float | None = Query(None, description="Real entry fee, for a running total-cost figure -- the file has no payout data at all"),
    my_entry_id: str | None = Query(None, description="Your own EntryId, if you know it -- the reliable way to identify your entry"),
    my_handle: str | None = Query(None, description="Your own DK handle, as a fallback if you don't know your EntryId -- a best-effort name match, not guaranteed in a big public field"),
    file: UploadFile = File(..., description="A real DK contest-standings export -- the .zip DK gives you, or the .csv inside it"),
) -> dict[str, Any]:
    """
    Upload a real, completed DraftKings contest's post-contest
    standings export -- different from the pre-contest salary CSV or
    the bulk-entries upload template. Archives every player's real
    final ownership%/actual FPTS, and (once identified) your own
    rank/points, permanently into Supabase (a no-op if SUPABASE_DB_URL
    isn't set) -- real market ground truth for a future ownership-model
    calibration pass, and a real bankroll/results-over-time history.
    """
    raw = await file.read()
    try:
        text = contest_results.extract_csv_text(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"Couldn't read that file: {exc}") from exc

    parsed = contest_results.parse_contest_standings(text)
    if not parsed["entries"]:
        raise HTTPException(
            status_code=400,
            detail="No contest entries found -- is this a real DK contest-standings export?",
        )

    day = date or date_cls.today().isoformat()
    # DK's own export has no contest id/name in the file itself -- the
    # caller supplies a name they recognize, and the (date, name) pair
    # doubles as a stable id for upserting a re-upload of the same contest.
    name = contest_name or "Contest"
    cid = f"{day}:{name}"
    my_entry = contest_results.find_my_entry(parsed["entries"], entry_id=my_entry_id, handle=my_handle)

    asyncio.create_task(
        history_db.archive_contest_results(
            day, cid, name, parsed["player_pool"],
            field_size=len(parsed["entries"]), entry_fee=entry_fee, my_entry=my_entry,
        )
    )

    return {
        "date": day,
        "contest_id": cid,
        "field_size": len(parsed["entries"]),
        "players_found": len(parsed["player_pool"]),
        "my_entry": my_entry,
    }


@router.get("/contest-results/history")
async def get_contest_results_history() -> dict[str, Any]:
    """Every previously-uploaded real contest with an identified entry
    of yours, for a bankroll/results-over-time view."""
    contests = await history_db.get_my_contest_history()
    return {
        "contests": contests,
        "total_entries": len(contests),
        "total_cost": round(sum(float(c.get("entry_fee") or 0) for c in contests), 2),
    }
