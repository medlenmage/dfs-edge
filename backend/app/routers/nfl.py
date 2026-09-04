"""HTTP routes for NFL."""

from __future__ import annotations

import zlib
from datetime import date as date_cls
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Body, File, HTTPException, Query, Response, UploadFile

from app import cache
from app.clients import draftkings, nfl, rotowire_nfl
from app.clients.http import ApiError
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
    include_inhouse: bool = Query(
        False,
        description="Also compute this app's own in-house FPTS/ownership/leverage projections (a real game-log read per player)",
    ),
) -> dict[str, Any]:
    """The week's slate: games, Vegas-implied context, weather, and (once uploaded) every rostered player."""
    resolved_season, resolved_week = await _resolve_season_week(season, week)
    return await nfl_slate.build_slate(
        resolved_season, resolved_week, force_refresh=force, include_inhouse=include_inhouse
    )


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


@router.get("/dk-slates")
async def get_dk_slates(
    season: int | None = Query(None),
    week: int | None = Query(None),
    refresh: bool = Query(False, description="Bypass the 15-minute cache and re-pull from DraftKings"),
) -> dict[str, Any]:
    """
    Every live Classic NFL slate for a week, straight from DraftKings'
    own lobby -- so the slate can be picked from a list instead of being
    inferred from whichever CSV happened to get uploaded.

    A football week is not a day, which is the one real difference from
    the MLB version: a Thursday-through-Monday slate and the Sunday-only
    one inside it start on different dates and both belong to the same
    week. The week's real game dates come from the schedule, and every
    slate starting on any of them is returned.
    """
    resolved_season, resolved_week = await _resolve_season_week(season, week)
    try:
        games = await nfl.get_schedule(resolved_season, resolved_week)
    except ApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    days = sorted({g["gameday"] for g in games if g.get("gameday")})
    if not days:
        raise HTTPException(
            status_code=404,
            detail=f"No scheduled games found for {resolved_season} week {resolved_week}.",
        )
    try:
        slates = await draftkings.get_slates(
            days[0], sport="NFL", days=days, force=refresh
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Couldn't reach DraftKings: {exc}") from exc
    return {
        "season": resolved_season,
        "week": resolved_week,
        "days": days,
        "slates": slates,
    }


@router.post("/dk-slates/load")
async def load_dk_slate(
    season: int | None = Body(None, embed=True),
    week: int | None = Body(None, embed=True),
    draft_group_id: int = Body(..., embed=True, description="From GET /dk-slates' draft_group_id"),
    refresh: bool = Body(
        False,
        embed=True,
        description="Bypass the 10-minute cache and re-pull live -- use for late inactives close to lock",
    ),
) -> dict[str, Any]:
    """
    Pull players + salaries for one specific DraftKings NFL slate and
    store them exactly as a manual salary CSV upload would, so
    everything downstream -- the optimizer, the contest generator, both
    simulator engines -- works unchanged either way.
    """
    resolved_season, resolved_week = await _resolve_season_week(season, week)
    try:
        rows = await draftkings.get_draftables(draft_group_id, force=refresh)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Couldn't reach DraftKings: {exc}") from exc
    if not rows:
        raise HTTPException(
            status_code=400,
            detail=(
                "No players found for that slate -- it may not be live yet, or the "
                "draft_group_id is stale (re-fetch GET /dk-slates)."
            ),
        )
    salaries.store(nfl_slate.week_key(resolved_season, resolved_week), rows)
    return {
        "season": resolved_season,
        "week": resolved_week,
        "draft_group_id": draft_group_id,
        "players_loaded": len(rows),
    }


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
    min_salary: int | None = Body(
        nfl_optimizer.DEFAULT_MIN_SALARY,
        embed=True,
        description="Floor on total lineup salary. Defaults to $47,000 -- pass 0 to disable.",
    ),
    min_unique_players: int = Body(
        1, embed=True, description="Minimum number of players that must differ between any two lineups"
    ),
    bring_back_min: int = Body(
        0,
        embed=True,
        description="How many players from the OPPONENT of the stacked QB's own game to force in -- the bring-back half of an NFL stack. Defined against his opponent rather than a second named team because a stack from an unrelated game is not a bring-back and correlates with nothing.",
    ),
    stack_team: str | None = Body(
        None,
        embed=True,
        description="Build the stack around this specific team by forcing its QB into every lineup. qb_stack_min and bring_back_min then hang off him.",
    ),
    team_exposure_cap: dict[str, float] | None = Body(
        None,
        embed=True,
        description="Caps how often a team is used AS THE STACK (the team whose QB is rostered), not how often its players appear incidentally -- e.g. {\"KC\": 40}.",
    ),
    max_salary: int | None = Body(
        None, embed=True, description="Spend no more than this. Defaults to the $50,000 cap."
    ),
    min_teams_per_lineup: int | None = Body(
        None, embed=True, description="Force a lineup to draw from at least this many distinct teams."
    ),
    max_teams_per_lineup: int | None = Body(
        None, embed=True, description="Force a lineup to draw from at most this many distinct teams -- lower means more concentrated."
    ),
    min_ownership_pct: float | None = Body(
        None, embed=True, description="Minimum cumulative ownership across the 9 rostered players."
    ),
    max_ownership_pct: float | None = Body(
        None, embed=True, description="Maximum cumulative ownership across the 9 rostered players -- the contrarian lever."
    ),
    included_game_pks: list[str] | None = Body(
        None,
        embed=True,
        description="Build only from these games. A week has more games than a DK slate does, so this is how a Sunday-only lineup avoids rostering a Thursday player.",
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
            team_exposure_cap=team_exposure_cap,
            min_salary=min_salary,
            max_salary=max_salary,
            min_unique_players=min_unique_players,
            qb_stack_min=qb_stack_min,
            bring_back_min=bring_back_min,
            stack_team=stack_team,
            min_teams_per_lineup=min_teams_per_lineup,
            max_teams_per_lineup=max_teams_per_lineup,
            min_ownership_pct=min_ownership_pct,
            max_ownership_pct=max_ownership_pct,
            included_game_pks=included_game_pks,
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


def _nfl_sim_seed(season: int, week: int, contest_type: str, extra: str, reroll: int) -> int:
    """
    A deterministic seed derived from the request's own settings, so the
    same request reproduces the same contest and the same simulated
    draws -- and `reroll` bumps into a genuinely new draw on demand.

    NFL had no seeding at all: every generator and simulator call went
    out with seed=None, so identical settings produced a different batch
    (and different results) on every click, and nothing was reproducible
    or comparable run to run. Same construction the MLB router's
    _sim_seed() uses -- a stable hash of the settings, masked into a
    positive 32-bit int.
    """
    # zlib.crc32 rather than hash(): Python salts hash() per process, so
    # it would break the whole point across a backend restart.
    key = f"{season}|{week}|{contest_type}|{extra}|{reroll}"
    return zlib.crc32(key.encode()) & 0x7FFFFFFF


@router.post("/contest-entries")
async def build_contest_entries(
    season: int | None = Body(None, embed=True),
    week: int | None = Body(None, embed=True),
    contest_type: str = Body(..., embed=True, description="One of GET /contest-types' keys"),
    contest_size: int = Body(
        ...,
        embed=True,
        description=(
            "The contest's size -- one of the selected preset's own `sizes`. This is the single "
            "size control: it is both the contest's field size AND how many lineups get built, "
            "since the generator builds a contest rather than a handful of entries to drop into "
            f"someone else's. Building is capped at {nfl_contest.MAX_USER_LINEUPS:,}, so a larger "
            "contest gets that many lineups standing in for the full field -- the response "
            "reports num_entries_built alongside field_size rather than conflating them."
        ),
    ),
    field_sharpness: str = Body(
        "marquee",
        embed=True,
        description=(
            "Contest stakes, and so who is in the field: 'low' (a cheap contest -- newer, "
            "safer entrants, the chalkiest lineups), 'marquee' (default, a milly-maker or "
            "other massive field, a mix of both), or 'high' (high stakes, where players "
            "limit chalk and hunt low-owned plays with a real matchup edge behind them). "
            "It belongs on the generator because the generator is what builds the opponents."
        ),
    ),
    reroll: int = Body(
        0,
        embed=True,
        description="Bump to get a genuinely different random draw for otherwise-identical settings. At the default 0, the same settings reproduce the same contest.",
    ),
) -> dict[str, Any]:
    """
    The NFL contest generator: build a whole DraftKings Classic NFL
    contest in one request -- lineups and nothing else.

    Deliberately has no economics. Cash rate, payouts and ROI belong to
    the simulator, which runs afterwards on the batch this returns
    (POST /contest-entries/{batch_id}/simulate) with its own inputs --
    the entry cost and the payout curve's percent-to-first, neither of
    which has anything to do with how the lineups themselves get built.

    `field_sharpness` steers how the OPPONENTS are built, and belongs
    here because the generator is what builds them -- the simulator's
    job is to price the pool it is handed, not to invent a second one.

    Equally deliberately, there are no salary, exposure or duplicate
    knobs. Every entry is built toward spending the cap (see
    nfl_contest._SALARY_PACING_STRENGTH) because a lineup leaving salary
    unspent is leaving projected points behind, and duplicates are
    always allowed because a real contest field contains them.
    """
    resolved_season, resolved_week = await _resolve_season_week(season, week)
    slate = await nfl_slate.build_slate(resolved_season, resolved_week)
    try:
        result = await nfl_contest.build_contest_lineups(
            slate,
            contest_type,
            contest_size,
            season=nfl.PRIOR_SEASON,
            field_sharpness=field_sharpness,
            # Sharpness is in the seed key: a setting that shapes the
            # batch but is missing from it would reproduce the SAME
            # contest under different settings, which looks deterministic
            # while being wrong.
            seed=_nfl_sim_seed(
                resolved_season, resolved_week, contest_type,
                f"build:{contest_size}:{field_sharpness}", reroll,
            ),
        )
    except nfl_contest.ContestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    full_entries = result.pop("entries")
    batch_id = uuid4().hex
    cache.put(
        f"nfl_contest_batch:{batch_id}",
        {
            "entries": full_entries,
            "results": [],
            "contest": result.get("contest"),
            "contest_type": contest_type,
            "season": resolved_season,
            "week": resolved_week,
        },
        _NFL_CONTEST_BATCH_TTL,
    )
    result["batch_id"] = batch_id
    result["sample_entries"] = full_entries[:_NFL_ENTRIES_PREVIEW_CAP]
    return {"season": resolved_season, "week": resolved_week, **result}


@router.post("/contest-entries/{batch_id}/simulate")
async def simulate_contest_batch(
    batch_id: str,
    entry_fee: float | None = Body(
        None,
        embed=True,
        description="What one entry costs. This sets the prize pool (field size x entry fee, less rake), so it drives every payout and every ROI in the result. Defaults to the contest preset's own fee.",
    ),
    first_place_pct: float | None = Body(
        None,
        embed=True,
        description="What share of the prize pool 1st place wins. A lower value flattens the payout curve, which changes every entry's simulated ROI. Defaults to the contest preset's own value.",
    ),
    self_play: bool = Body(
        True,
        embed=True,
        description="Default: rank the contest against ITSELF -- the generator builds the whole contest, so the batch IS the field. Set false to rank it against a separately-sampled, ownership-weighted public field instead.",
    ),
    field_sharpness: str = Body(
        "marquee",
        embed=True,
        description="How sharp the sampled public field is: 'low', 'marquee' (default) or 'high'. Only used when self_play is false.",
    ),
    engine: str = Body(
        "bootstrap",
        embed=True,
        description="'bootstrap' (default) samples each player's own historical DK-point outcome pool and imposes a team correlation. 'structural' instead draws each GAME from its Vegas-implied totals and allocates that volume among the players, so correlation is produced rather than assumed -- teammates divide one team's touchdowns, a pass-catching back's targets rise when his team trails, and a DST scores off the opponent's own draw. Needs a Vegas line for every game on the slate; two of its constants are still unfitted, so it is opt-in.",
    ),
    reroll: int = Body(
        0, embed=True, description="Bump for a genuinely different set of simulated draws on the same batch."
    ),
) -> dict[str, Any]:
    """
    Simulate an already-built NFL contest -- the simulator half of the
    generator/simulator split.

    Takes a `batch_id` from POST /contest-entries and runs the Monte
    Carlo over it, so the same contest can be simulated repeatedly under
    different economics (a different entry cost, a flatter or more
    top-heavy payout curve) without rebuilding a single lineup. The
    simulated batch is cached under a NEW batch_id, leaving the original
    build intact and re-simulatable.
    """
    cached = cache.get(f"nfl_contest_batch:{batch_id}")
    if not cached or not cached.get("entries"):
        raise HTTPException(
            status_code=404,
            detail="That batch has expired or was never built -- build the contest again.",
        )

    resolved_season = cached.get("season")
    resolved_week = cached.get("week")
    slate = None
    # The structural engine needs the slate in BOTH modes -- it draws
    # the games themselves -- where the sampled public field only needs
    # it to build opponents.
    if not self_play or engine == "structural":
        slate = await nfl_slate.build_slate(resolved_season, resolved_week)
    try:
        result = await nfl_contest.simulate_contest_batch(
            cached["entries"],
            cached["contest"],
            season=nfl.PRIOR_SEASON,
            slate=slate,
            contest_type=cached.get("contest_type", ""),
            num_trials=_NFL_SIM_TRIALS,
            entry_fee=entry_fee,
            first_place_pct=first_place_pct,
            self_play=self_play,
            field_sharpness=field_sharpness,
            engine=engine,
            seed=_nfl_sim_seed(
                resolved_season, resolved_week, cached.get("contest_type", ""),
                f"sim:{batch_id}:{entry_fee}:{self_play}:{engine}", reroll,
            ),
        )
    except nfl_contest.ContestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    full_entries = result.pop("entries")
    full_results = result["results"]
    new_batch_id = uuid4().hex
    cache.put(
        f"nfl_contest_batch:{new_batch_id}",
        {
            "entries": full_entries,
            "results": full_results,
            "contest": result.get("contest"),
            "contest_type": cached.get("contest_type", ""),
            "season": resolved_season,
            "week": resolved_week,
        },
        _NFL_CONTEST_BATCH_TTL,
    )
    result["batch_id"] = new_batch_id
    result["source_batch_id"] = batch_id
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
