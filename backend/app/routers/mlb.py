"""HTTP routes for MLB."""

from __future__ import annotations

import json
import zlib
from collections import Counter
from datetime import date as date_cls
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Body, File, HTTPException, Query, Response, UploadFile

import asyncio

from app import cache, history_db
from app.config import get_settings
from app.clients import draftkings, rotowire
from app.services import (
    briefs,
    build_audit,
    contest_audit,
    analysis,
    contest,
    contest_results,
    dk_entries,
    dk_entry_manager,
    late_swap as late_swap_service,
    lineup_export,
    lineup_intake,
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


def _sim_seed(day: str, contest_type: str, source: str, engine: str, reroll: int) -> int:
    """
    A deterministic seed derived from the settings that actually shape
    a batch, so identical requests reproduce identical results. Without
    this, every click generated different entries, a different opponent
    field, AND different Monte Carlo draws -- the results table
    reshuffled even when the model and inputs hadn't changed at all,
    which reads as instability rather than what it was: unseeded RNG.

    zlib.crc32 rather than hash(): Python salts hash() per process, so
    it would break the whole point across a backend restart.
    """
    key = f"{day}|{contest_type}|{source}|{engine}|{reroll}"
    return zlib.crc32(key.encode()) & 0x7FFFFFFF


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
        slate = await mlb_slate.build_slate(
            day, force_refresh=refresh, include_hitters=hitters, include_inhouse=inhouse
        )
        # Archive this day's per-team market context permanently, so a
        # future backtest can rebuild the slate with the team-stack
        # layer's real inputs instead of neutralising it. Fire and
        # forget, exactly like the projections archiver -- it must never
        # delay or break a dashboard load.
        rows = mlb_slate.team_context_rows(slate)
        if rows:
            asyncio.create_task(history_db.archive_slate_team_context(day, rows))
        # Same for the in-house numbers, whenever they were computed --
        # so a future backtest can rebuild the slate on the projection
        # the model actually runs on rather than RotoWire's.
        ih_rows = mlb_slate.inhouse_projection_rows(slate)
        if ih_rows:
            asyncio.create_task(history_db.archive_inhouse_projections(day, ih_rows))
        return slate
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


# One scraped slate's rows, cached under its OWN slate id so switching
# between windows the same scrape already pulled costs nothing. The
# day-keyed store (projections.store) still holds exactly one ACTIVE
# slate, since everything downstream reads by date.
_ROTOWIRE_SLATE_TTL = 900


def _rotowire_slate_key(slate_id: Any) -> str:
    return f"rotowire:slate-rows:{slate_id}"


async def _activate_rotowire_slate(
    day: str, rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """Make one already-scraped slate the active one for its date --
    the same day-keyed store a manual CSV upload writes to."""
    projections.store(day, rows)
    asyncio.create_task(history_db.archive_slate_projections(day, rows))
    result: dict[str, Any] = {"date": day, "players_loaded": len(rows)}

    existing_salaries = salaries.load(day)
    if existing_salaries:
        # A DK slate is already loaded for this date -- run the same
        # name-matching used for the live slate now, so a RotoWire/
        # DraftKings spelling mismatch shows up immediately instead of
        # silently leaving that player's projection blank.
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


@router.post("/projections/refresh-rotowire")
async def refresh_rotowire_projections(
    slate_name: str | None = Body(
        None, embed=True,
        description="Which scraped window to make ACTIVE (e.g. 'Late Night'). Defaults to the main 'All' slate, or the first one found if there's no main slate today.",
    ),
    refresh: bool = Body(
        False, embed=True,
        description="Bypass the cache and re-pull live from RotoWire -- use close to lock for newly confirmed lineups",
    ),
) -> dict[str, Any]:
    """
    Pull RotoWire's own live optimizer player pool directly from their
    site (clients/rotowire.py) instead of a manual CSV download/upload.

    Scrapes EVERY Classic slate window RotoWire has live right now --
    All, Early, Afternoon, Turbo, Night, Late Night -- in one call.
    Which windows exist genuinely varies by day (a real 2026-08-29 list
    carried no Early slate at all), so a missing one is skipped rather
    than treated as a failure, and a window that errors on its own is
    reported alongside the ones that worked instead of taking the whole
    refresh down with it.

    All six windows come from a single slate-list response, so scraping
    everything costs the same one fetch as scraping one. Each window's
    rows are cached under its own slate id, so switching which one is
    active afterwards needs no new network call at all.

    Exactly one slate is ACTIVE per date, because everything downstream
    (mlb_slate, the optimizer, the contest generator) reads projections
    by date. `slate_name` picks which; the response lists every window
    found so the caller can offer the choice.
    """
    try:
        slates = await rotowire.get_live_classic_slates(force=refresh)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Couldn't reach RotoWire: {exc}") from exc

    found: list[dict[str, Any]] = []
    rows_by_window: dict[str, list[dict[str, Any]]] = {}
    teams_by_window: dict[str, set[str]] = {}
    window_dates: dict[str, str | None] = {}
    for slate in slates:
        window = slate["windowName"]
        entry: dict[str, Any] = {
            "slate_name": window,
            "slate_id": slate.get("slateID"),
            "date": slate.get("startDateOnly"),
        }
        try:
            rows = await rotowire.get_slate_players(slate["slateID"], force=refresh)
        except Exception as exc:  # noqa: BLE001
            # One window failing is not the whole refresh failing -- say
            # so and keep going, which is the entire point of looping.
            entry["error"] = str(exc)
            found.append(entry)
            continue
        if not rows:
            entry["error"] = "no players posted yet"
            found.append(entry)
            continue
        rows_by_window[window] = rows
        teams_by_window[window] = {
            player_match.normalize_team(r["team"]) for r in rows if r.get("team")
        }
        window_dates[window] = slate.get("startDateOnly")
        cache.put(_rotowire_slate_key(slate["slateID"]), rows, _ROTOWIRE_SLATE_TTL)
        entry["players"] = len(rows)
        # Teams, not games -- a RotoWire player row carries its own team
        # but no opponent, so a game count would be a guess. This is the
        # real number, and it's what makes the windows distinguishable
        # at a glance (a Late Night slate is a handful of teams).
        entry["teams"] = len({r["team"] for r in rows if r.get("team")})
        found.append(entry)

    if not rows_by_window:
        raise HTTPException(
            status_code=400,
            detail="RotoWire has slates listed but no players posted in any of them yet.",
        )

    # Which one becomes active. An explicitly requested window always
    # wins. With no request, AUTO-MATCH against the loaded DK salary
    # slate's own teams: a user working DK's Late Night slate who
    # clicks Refresh wants the Late Night window's projections, and
    # activating "All" (which doesn't even contain the late-night-only
    # games) silently loaded a pool that missed their entire slate --
    # the exact reported bug this exists to prevent. Only when no DK
    # slate is loaded (or nothing covers at least half of it) does the
    # old default -- the main "All" window -- apply.
    active = slate_name if slate_name in rows_by_window else None
    auto_matched = False
    if active is None:
        dk_dates = {d for d in window_dates.values() if d}
        for dk_day in sorted(dk_dates):
            dk_teams = {
                player_match.normalize_team(g[side])
                for g in salaries.slate_games(salaries.load(dk_day))
                for side in ("away", "home")
            }
            matchable = {
                w: teams for w, teams in teams_by_window.items()
                if window_dates.get(w) == dk_day
            }
            match = salaries.pick_best_team_match(matchable, dk_teams)
            if match is not None:
                active = match
                auto_matched = True
                break
    if active is None:
        active = (
            rotowire.MAIN_SLATE_NAME
            if rotowire.MAIN_SLATE_NAME in rows_by_window
            else next(iter(rows_by_window))
        )
    active_slate = next(s for s in slates if s["windowName"] == active)

    result = await _activate_rotowire_slate(active_slate["startDateOnly"], rows_by_window[active])
    result["active_slate"] = active
    if auto_matched:
        result["note"] = (
            f"Auto-matched to the '{active}' window -- its games line up with the DK slate "
            "you have loaded. Use the Slate picker to override."
        )
    result["slates"] = found
    if slate_name and slate_name != active:
        result["note"] = (
            f"'{slate_name}' has no players posted right now -- loaded '{active}' instead."
        )
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
    field_sharpness: str = Body(
        "marquee",
        embed=True,
        description="How sharp the simulated public field is: 'low' (softer/chalkier, more dispersed ownership), 'marquee' (default, a realistic large-field GPP), or 'high' (sharp bettors converging on the best pure value plays).",
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
            field_sharpness=field_sharpness,
        )
    except contest.ContestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"date": day, **result}


@router.post("/contest-entries")
async def build_contest_entries(
    date: str | None = Body(None, embed=True),
    contest_type: str = Body(..., embed=True, description="One of GET /contest-types' keys"),
    contest_size: int = Body(
        ...,
        embed=True,
        description=(
            "The contest's size -- one of the selected preset's own `sizes`. This is the single "
            "size control: it is both the contest's field size AND how many lineups get built, "
            "since the generator builds a contest rather than a handful of entries to drop into "
            f"someone else's. Building is capped at {contest.MAX_USER_LINEUPS:,}, so a larger "
            "contest gets that many lineups standing in for the full field -- the response "
            "reports num_entries_built alongside field_size rather than conflating them."
        ),
    ),
    projection_source: str = Body(
        "rotowire", embed=True, description="Which FPTS/ownership numbers to build against: 'rotowire' or 'inhouse'"
    ),
    included_game_pks: list[int] | None = Body(
        None, embed=True, description="Restrict the pool to these games only -- e.g. to match a specific DK slate"
    ),
    use_my_lineups: bool = Body(
        False,
        embed=True,
        description=(
            "Lead the batch with the lineups you set aside for the day (GET /my-lineups) and "
            "fill the rest of the field around them, instead of generating the whole contest. "
            "This is how a portfolio that OBEYS the process rules gets simulated and audited, "
            "rather than one the generator was merely steered toward."
        ),
    ),
    reroll: int = Body(
        0,
        embed=True,
        description="Bump to get a genuinely different random draw for otherwise-identical settings. At the default 0, the same settings on the same date always reproduce the same contest.",
    ),
) -> dict[str, Any]:
    """
    The contest generator: build a whole DraftKings Classic MLB contest
    in one request -- lineups and nothing else.

    Deliberately has no economics. Cash rate, payouts and ROI belong to
    the simulator, which runs afterwards on the batch this returns
    (POST /contest-entries/{batch_id}/simulate) with its own inputs --
    the entry cost and the payout curve's percent-to-first, neither of
    which has anything to do with how the lineups themselves get built.

    Equally deliberately, there are no salary, exposure or duplicate
    knobs. Every entry is built toward spending the cap (see
    contest._SALARY_PACING_STRENGTH) because a lineup leaving salary
    unspent is leaving projected points behind, and duplicates are
    always allowed because a real contest field contains them. Requires
    a DraftKings salary CSV and either a RotoWire projections CSV or the
    in-house model loaded for the date, same as the optimizer.

    The full batch is cached under a `batch_id` returned in the response
    -- the simulator, the late-swap endpoint, the Entry Manager and GET
    /contest-entries/{batch_id}/csv all read it from there, rather than
    it being re-sent (the response itself previews only the first 200).
    """
    day = date or date_cls.today().isoformat()
    # Always includes in-house data -- see build_contest_field's own
    # comment above; the pool needs a real ownership fallback wherever
    # RotoWire's export doesn't cover a player.
    slate = await mlb_slate.build_slate(day, include_inhouse=True)
    injected = get_my_lineups(day) if use_my_lineups else []
    if use_my_lineups and not injected:
        raise HTTPException(
            status_code=400,
            detail=(
                "No lineups set aside for that date. Build some in the optimizer, or read "
                "them out of an uploaded DK entries file, before asking the contest to use them."
            ),
        )
    try:
        result = contest.build_contest_lineups(
            slate,
            contest_type,
            contest_size,
            projection_source=projection_source,
            included_game_pks=included_game_pks,
            # Your own lineups lead the batch; the generator fills the
            # field behind them. See lineup_intake.py for why the two
            # engines are used together rather than one or the other.
            injected_entries=injected,
            # Identical settings reproduce the identical contest (see
            # _sim_seed); `reroll` bumps into a genuinely new draw.
            seed=_sim_seed(day, contest_type, projection_source, "build", reroll),
        )
    except contest.ContestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    full_entries = result.pop("entries")
    batch_id = uuid4().hex
    cache.put(
        f"contest_batch:{batch_id}",
        {
            "entries": full_entries,
            "results": [],
            "field": [],
            "contest": result.get("contest"),
            "contest_type": contest_type,
            "projection_source": projection_source,
            "included_game_pks": included_game_pks,
        },
        _CONTEST_BATCH_TTL,
    )
    briefs.remember_latest_batch(day, batch_id, full_entries, source="build")

    result["batch_id"] = batch_id
    result["sample_entries"] = full_entries[:200]
    return {"date": day, **result}


@router.post("/contest-entries/{batch_id}/simulate")
async def simulate_contest_batch(
    batch_id: str,
    date: str | None = Body(None, embed=True),
    entry_fee: float | None = Body(
        None,
        embed=True,
        description="What one entry costs. This sets the prize pool (field size x entry fee, less rake), so it drives every payout and every ROI in the result. Defaults to the contest preset's own fee.",
    ),
    first_place_pct: float | None = Body(
        None,
        embed=True,
        description="What share of the prize pool 1st place wins. A lower value flattens the payout curve -- more spread across the paid ranks, less concentrated at 1st -- which changes every entry's simulated ROI. Defaults to the contest preset's own value.",
    ),
    self_play: bool = Body(
        True,
        embed=True,
        description="Default: rank the contest against ITSELF -- the generator builds the whole contest, so the batch IS the field. Set false to rank it against a separately-sampled, ownership-weighted public field instead, which answers the different question of how these lineups would fare against real public rosters.",
    ),
    field_sharpness: str = Body(
        "marquee",
        embed=True,
        description="How sharp the sampled public field is: 'low', 'marquee' (default) or 'high'. Only used when self_play is false -- self-play never samples a separate field.",
    ),
    engine: str = Body(
        "bootstrap",
        embed=True,
        description="'bootstrap' (default) samples each player's own historical DK-point outcome pool. 'atbat' instead runs genuine plate-appearance-by-plate-appearance simulated games for the whole slate, but requires a CONFIRMED lineup on both sides and a resolvable probable pitcher for every game on the slate.",
    ),
    reroll: int = Body(
        0, embed=True, description="Bump for a genuinely different set of simulated draws on the same batch."
    ),
) -> dict[str, Any]:
    """
    Simulate an already-built contest -- the simulator half of the
    generator/simulator split.

    Takes a `batch_id` from POST /contest-entries and runs
    _SIM_TRIALS Monte Carlo trials over it, so the same contest can be
    simulated repeatedly under different economics (a different entry
    cost, a flatter or more top-heavy payout curve) without rebuilding a
    single lineup. The simulated batch is cached under a NEW batch_id,
    leaving the original build intact and re-simulatable.
    """
    cached = cache.get(f"contest_batch:{batch_id}")
    if not cached or not cached.get("entries"):
        raise HTTPException(
            status_code=404,
            detail="That batch has expired or was never built -- build the contest again.",
        )

    day = date or date_cls.today().isoformat()
    season = int(day[:4])
    slate = await mlb_slate.build_slate(day, include_inhouse=True)
    try:
        result = await contest.simulate_contest_batch(
            cached["entries"],
            cached["contest"],
            season=season,
            contest_type=cached.get("contest_type", ""),
            slate=slate,
            num_trials=_SIM_TRIALS,
            entry_fee=entry_fee,
            first_place_pct=first_place_pct,
            self_play=self_play,
            field=cached.get("field") or None,
            field_sharpness=field_sharpness,
            engine=engine,
            projection_source=cached.get("projection_source", "rotowire"),
            included_game_pks=cached.get("included_game_pks"),
            seed=_sim_seed(day, batch_id, str(entry_fee), engine, reroll),
        )
    except contest.ContestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    full_entries = result.pop("entries")
    full_results = result["results"]
    full_field = result.pop("field", [])
    new_batch_id = uuid4().hex
    cache.put(
        f"contest_batch:{new_batch_id}",
        {
            "entries": full_entries,
            "results": full_results,
            "field": full_field,
            "contest": result.get("contest"),
            "contest_type": cached.get("contest_type", ""),
            "projection_source": cached.get("projection_source", "rotowire"),
            "included_game_pks": cached.get("included_game_pks"),
        },
        _CONTEST_BATCH_TTL,
    )
    briefs.remember_latest_batch(day, new_batch_id, full_entries, source="simulate")

    result["batch_id"] = new_batch_id
    result["source_batch_id"] = batch_id
    result["results"] = full_results[:200]
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
        0,
        embed=True,
        description="Floor on each entry's salary. Off by default: a hard floor makes whole stack shapes infeasible and stalls a batch, and entries are already built toward spending the cap (contest._SALARY_PACING_STRENGTH).",
    ),
    max_salary: int = Body(
        optimizer.SALARY_CAP, embed=True, description="Ceiling on each entry's salary"
    ),
    allow_duplicates: bool = Body(
        False,
        embed=True,
        description="Allow exact duplicate entries in the batch (a real GPP move -- entering a signature build multiple times). Duplicates' cash probability/payout/ROI are averaged across the tied group, matching DK's real tie-payout split. Each entry reports duplicate_count",
    ),
    max_duplication_risk: float | None = Body(
        None,
        embed=True,
        description="Reject any entry whose cumulative (log-product) ownership exceeds this -- a chalk filter distinct from a salary/exposure cap, catching a lineup where every player is moderately chalky together (the real 'exact duplicate' risk) even when its SUMMED total_ownership_pct looks unremarkable. More negative = stricter (closer to 0 = looser). Unset by default (unconstrained).",
    ),
    self_play: bool = Body(
        False,
        embed=True,
        description="Rank this batch against ITSELF instead of a separately-sampled public field -- every lineup competing against every other lineup you generated, in the same simulated trial. Use this to see how your own stacks/builds compare to each other; leave off (default) to see how your batch fares against a realistic public field.",
    ),
    engine: str = Body(
        "bootstrap",
        embed=True,
        description="'bootstrap' (default) samples each player's own historical DK-point outcome pool. 'atbat' instead runs genuine at-bat-level (plate-appearance by plate-appearance) simulated games for the whole slate -- correlation is a natural consequence of shared simulated game state rather than a team multiplier, but it requires a CONFIRMED lineup on both sides and a resolvable probable pitcher for every game on the slate, and fails with a clear error otherwise.",
    ),
    field_sharpness: str = Body(
        "marquee",
        embed=True,
        description="How sharp the simulated opponent field is: 'low' (softer/chalkier, more dispersed ownership), 'marquee' (default, a realistic large-field GPP), or 'high' (sharp bettors converging on the best pure value plays). Ignored when self_play=True -- self-play never samples a separate field.",
    ),
    first_place_pct: float | None = Body(
        None,
        embed=True,
        description="Override the contest preset's own percent-to-first (% of the prize pool 1st place wins) for this run -- a lower value flattens the payout curve, which changes every entry's simulated ROI. Defaults to the preset's own value when omitted.",
    ),
    reroll: int = Body(
        0,
        embed=True,
        description="Bump to get a genuinely different random draw (new entries, new field, new sim trials) for otherwise-identical settings. At the default 0, the same settings on the same date always reproduce the same batch.",
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
            max_duplication_risk=max_duplication_risk,
            self_play=self_play,
            engine=engine,
            field_sharpness=field_sharpness,
            first_place_pct=first_place_pct,
            # A deterministic seed derived from the settings, so the
            # same request on the same date reproduces the same batch
            # -- without it every click generated different entries, a
            # different field AND different sim draws, and the table
            # reshuffled even when nothing changed. `reroll` bumps into
            # a genuinely new draw on demand.
            seed=_sim_seed(day, contest_type, projection_source, engine, reroll),
        )
    except contest.ContestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    full_entries = result.pop("entries")
    full_results = result["results"]
    # The field and contest are cached alongside the entries purely so a
    # later late swap can re-rank against the SAME opponent field rather
    # than resampling a different one -- they're popped off the response
    # since the frontend has no use for thousands of opponent lineups.
    full_field = result.pop("field", [])
    batch_id = uuid4().hex
    cache.put(
        f"contest_batch:{batch_id}",
        {
            "entries": full_entries,
            "results": full_results,
            "field": full_field,
            "contest": result.get("contest"),
        },
        _CONTEST_BATCH_TTL,
    )
    briefs.remember_latest_batch(day, batch_id, full_entries, source="build")

    result["batch_id"] = batch_id
    result["results"] = full_results[:200]
    result["sample_entries"] = full_entries[:200]
    return {"date": day, **result}


@router.post("/contest-entries/{batch_id}/late-swap")
async def late_swap_contest_entries(
    batch_id: str,
    date: str | None = Body(None, embed=True),
    mode: str = Body(
        "repair",
        embed=True,
        description="'repair' swaps only DEAD players (scratched, or in a postponed game); "
        "'refresh' also swaps anyone whose projection has fallen materially since the batch was built",
    ),
    projection_source: str = Body("rotowire", embed=True),
    included_game_pks: list[int] | None = Body(None, embed=True),
    swap_field: bool = Body(
        True,
        embed=True,
        description="Also late-swap the simulated OPPONENT field before re-ranking. On by default "
        "because leaving the field holding scratched players while your own entries get repaired "
        "overstates your ROI -- the real field swaps too",
    ),
    resimulate: bool = Body(
        True, embed=True, description="Re-run the Monte Carlo against the swapped entries and field"
    ),
    season: int | None = Body(None, embed=True),
) -> dict[str, Any]:
    """
    Late-swap a whole already-built batch against the CURRENT slate.

    DraftKings locks each roster spot at that player's own game start,
    so a slate spanning several hours of first pitches leaves real
    editing time after the contest has begun -- and neither DK nor
    FanDuel auto-replaces a scratched or postponed player, so an entry
    still holding one just scores zero there.

    This repairs rather than re-optimizes: only spots that genuinely
    need swapping AND are still legally swappable get touched, so a
    deliberately diverse batch doesn't collapse toward the same handful
    of best available players (which would spike duplication in exactly
    the way a large-field GPP punishes hardest). See
    services/late_swap.py for the full reasoning.

    Caches the swapped batch under its OWN new batch_id, same as the
    reshape endpoint, so the original stays intact and the existing CSV
    download works on the result unchanged.
    """
    cached = cache.get(f"contest_batch:{batch_id}")
    if not cached:
        raise HTTPException(
            status_code=404,
            detail="That batch has expired or doesn't exist -- generate a new one and try again.",
        )

    day = date or _today()
    resolved_season = season or date_cls.fromisoformat(day).year
    slate = await mlb_slate.build_slate(day, include_hitters=True)
    try:
        pool = optimizer.build_player_pool(
            slate,
            included_game_pks=set(included_game_pks) if included_game_pks is not None else None,
            projection_source=projection_source,
        )
    except optimizer.OptimizerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    slot_order: list[str] = []
    for slot, count in optimizer.SLOT_REQUIREMENTS.items():
        slot_order.extend([slot] * count)

    try:
        swapped = late_swap_service.swap_batch(
            cached["entries"], slate, pool,
            slot_order=slot_order, salary_cap=optimizer.SALARY_CAP, mode=mode, seed=0,
        )
    except late_swap_service.LateSwapError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    entries = swapped.pop("entries")
    # stack_type/stack are a property of which teams ended up rostered,
    # so a swap can genuinely change them -- re-derive rather than
    # carrying the pre-swap label forward as if nothing moved.
    for entry in entries:
        entry["stack_type"], entry["stack"] = lineup_export.stack_info(entry)

    results = cached["results"]
    field = cached.get("field") or []
    if swap_field and field:
        field_swapped = late_swap_service.swap_batch(
            field, slate, pool,
            slot_order=slot_order, salary_cap=optimizer.SALARY_CAP, mode=mode, seed=1,
        )
        field = field_swapped["entries"]
        swapped["field_entries_changed"] = field_swapped["entries_changed"]

    # The cached results describe the PRE-swap entries, so once anything
    # has moved they're stale by definition -- re-evaluate rather than
    # hand back numbers that no longer refer to these lineups. A
    # simulated re-run when asked for and possible; otherwise the same
    # fast deterministic ranking the batch was originally built with.
    # Whether this batch was BUILT with Monte Carlo on. A batch built in
    # the fast deterministic mode stays deterministic through a swap:
    # silently upgrading it would change what every number on screen
    # means, and cost a full simulation the user never asked for.
    was_simulated = bool(cached["results"]) and "roi_pct" in cached["results"][0]

    swapped["resimulated"] = False
    if swapped["total_swaps"] and cached.get("contest") and field:
        if resimulate and was_simulated:
            try:
                rerun = await contest.evaluate_batch_simulated(
                    entries, field, cached["contest"],
                    season=resolved_season, num_trials=_SIM_TRIALS,
                    slate=slate, included_game_pks=included_game_pks,
                )
                results = rerun["results"]
                swapped["resimulated"] = True
            except contest.ContestError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        else:
            results = contest.evaluate_batch(entries, field, cached["contest"])["results"]
    elif swapped["total_swaps"]:
        # Nothing to re-rank against (an older batch cached before the
        # field was kept). Say so rather than showing stale numbers.
        results = []

    new_batch_id = uuid4().hex
    cache.put(
        f"contest_batch:{new_batch_id}",
        {
            "entries": entries,
            "results": results,
            "field": field,
            "contest": cached.get("contest"),
        },
        _CONTEST_BATCH_TTL,
    )
    briefs.remember_latest_batch(day, new_batch_id, entries, source="late-swap")

    return {
        "date": day,
        "batch_id": new_batch_id,
        **swapped,
        "exposure": contest.field_exposure(entries),
        # Computed over the WHOLE batch here -- only the first 200
        # entries ship as a preview, so the frontend can't derive this
        # correctly for a bigger batch.
        "summary": contest.batch_summary(entries, results, cached.get("contest") or {}),
        "sample_entries": entries[:200],
        "results": results[:200],
    }


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
    # A build-only batch has no results yet (the simulator is a
    # separate step now) -- pass None rather than an empty list, which
    # would not line up with the entries it is meant to be aligned to.
    csv_text = lineup_export.lineups_to_csv(
        cached["entries"], results=cached.get("results") or None
    )
    return _spreadsheet_csv(csv_text, f"contest-entries-{batch_id}.csv")


@router.get("/dk-entries/fill")
async def fill_dk_entries(
    date: str | None = Query(None, description="The date the uploaded DK entries template (POST /dk-entries) was stored under"),
    contest_id: str = Query(..., description="One of GET /dk-entries's returned contest_id values"),
    batch_id: str = Query(..., description="A batch_id from POST /contest-entries or /contest-entries-simulated -- its own already-ranked order is used, strongest lineup first"),
    only_blank: bool = Query(True, description="Only fill entry rows with no picks yet (default) -- false overwrites every row for this contest, blank or not"),
) -> Response:
    """
    Entry Manager: fill a real DraftKings bulk-entries template (already
    uploaded via POST /dk-entries for this date) with lineups from an
    already-built batch, one lineup per blank entry row in the batch's
    own order, and return the completed CSV -- ready to literally
    reupload to DraftKings, no manual copy/paste.

    Requires a real DK salary CSV to have been loaded for the slate (not
    just RotoWire projections) -- DraftKings' own reupload format needs
    each player's real numeric DK id, which only a DK salary file
    carries.
    """
    day = date or date_cls.today().isoformat()
    text = dk_entries.load(day)
    if not text:
        raise HTTPException(
            status_code=404,
            detail="No DK entries template uploaded for that date yet -- upload one via POST /dk-entries first.",
        )
    cached = cache.get(f"contest_batch:{batch_id}")
    if not cached:
        raise HTTPException(
            status_code=404,
            detail="That batch has expired or doesn't exist -- generate a new one first.",
        )
    try:
        filled_csv, summary = dk_entry_manager.fill_entries(
            text, contest_id, cached["entries"], only_blank=only_blank,
        )
    except dk_entry_manager.EntryManagerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return Response(
        content=filled_csv,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="dk-entries-filled-{contest_id}.csv"',
            "X-Fill-Summary": json.dumps(summary),
        },
    )


@router.post("/contest-entries/{batch_id}/reshape")
async def reshape_contest_entries(
    batch_id: str,
    target_count: int | None = Body(
        None, embed=True, description="How many entries to keep in the final shaped portfolio -- defaults to the whole batch"
    ),
    max_exposure_pct: float | None = Body(
        None, embed=True, description="Cap how often any one player appears in the FINAL shaped portfolio (not the original batch)"
    ),
    player_exposure_caps: dict[str, float] | None = Body(
        None, embed=True, description="Per-player exposure cap overrides, keyed by player id as a string -- overrides max_exposure_pct for that player"
    ),
    roi_boosts: dict[str, float] | None = Body(
        None, embed=True, description="Per-player ROI nudge in PERCENTAGE POINTS (additive, may be negative), keyed by player id as a string -- re-ranks results, never changes the real simulated roi_pct"
    ),
    require_teams: list[str] | None = Body(
        None, embed=True, description="Filter: keep only entries rostering a player from EVERY team listed (e.g. require a specific team be part of the stack)"
    ),
    exclude_teams: list[str] | None = Body(
        None, embed=True, description="Filter: drop any entry rostering a player from ANY team listed"
    ),
    require_player_ids: list[int] | None = Body(
        None, embed=True, description="Filter: keep only entries rostering EVERY player id listed (a specific combo, e.g. two hitters stacked together)"
    ),
    exclude_player_ids: list[int] | None = Body(
        None, embed=True, description="Filter: drop any entry rostering ANY player id listed"
    ),
    stack_types: list[str] | None = Body(
        None, embed=True, description="Filter: keep only entries whose own stack shape (e.g. '5-3', '4-4') is one of these -- an entry with no stack at all has stack_type ''"
    ),
) -> dict[str, Any]:
    """
    Re-rank and/or re-filter an already-built batch's real results on
    the results screen -- no new Monte Carlo run, just a genuine
    reshape of numbers that are already simulated. Needs `roi_pct` on
    every result, so this only works on a batch built through one of
    the "Simulate" paths (POST /contest-entries-simulated,
    POST /dk-entries/simulate) -- the fast deterministic mode's results
    don't carry a per-entry roi_pct today.

    Filters (require/exclude teams, require/exclude specific player
    combos, named stack shapes) run FIRST, narrowing the batch to only
    entries matching every one given -- see contest.reshape_batch()'s
    own docstring. `target_count`/exposure caps then apply to that
    narrowed pool, not the original batch.

    Caches the reshaped batch under its OWN new batch_id (same TTL as
    any other build) so the existing CSV download endpoint works on it
    unchanged -- the original batch is left untouched, so reshaping
    more than once (trying a different cap, say) always starts back
    from the real, full simulated batch rather than compounding.
    """
    cached = cache.get(f"contest_batch:{batch_id}")
    if not cached:
        raise HTTPException(
            status_code=404,
            detail="That batch has expired or doesn't exist -- generate a new one and try again.",
        )
    if not cached["results"] or "roi_pct" not in cached["results"][0]:
        raise HTTPException(
            status_code=400,
            detail="This batch has no roi_pct to reshape by -- reshaping only works on a batch "
            "built with Simulate on.",
        )

    try:
        reshaped = contest.reshape_batch(
            cached["entries"],
            cached["results"],
            target_count=target_count,
            max_exposure_pct=max_exposure_pct,
            player_exposure_caps=(
                {int(pid): pct for pid, pct in player_exposure_caps.items()} if player_exposure_caps else None
            ),
            roi_boosts={int(pid): pts for pid, pts in roi_boosts.items()} if roi_boosts else None,
            require_teams=require_teams,
            exclude_teams=exclude_teams,
            require_player_ids=require_player_ids,
            exclude_player_ids=exclude_player_ids,
            stack_types=stack_types,
        )
    except contest.ContestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    new_batch_id = uuid4().hex
    cache.put(
        f"contest_batch:{new_batch_id}",
        {"entries": reshaped["entries"], "results": reshaped["results"]},
        _CONTEST_BATCH_TTL,
    )
    # A reshape has no date of its own -- it inherits the day the source
    # batch was built for, which briefs recorded alongside it.
    _src_day = (briefs.day_of_batch(batch_id) or date_cls.today().isoformat())
    briefs.remember_latest_batch(_src_day, new_batch_id, reshaped["entries"], source="reshape")

    return {
        "batch_id": new_batch_id,
        "num_kept": reshaped["num_kept"],
        "num_dropped": reshaped["num_dropped"],
        "num_filtered_out": reshaped["num_filtered_out"],
        "exposure": reshaped["exposure"],
        "sample_entries": reshaped["entries"][:200],
        "results": reshaped["results"][:200],
    }


# Your own lineups for the day, kept where every downstream step can
# find them. One hour is the contest-batch TTL; this is a slate-day
# working set, so it matches the salary/projection uploads instead.
_MY_LINEUPS_TTL = 60 * 60 * 12


def _my_lineups_key(day: str) -> str:
    return f"my_lineups:{day}"


def get_my_lineups(day: str) -> list[dict[str, Any]]:
    return cache.get(_my_lineups_key(day)) or []


async def _intake_lookup(day: str, projection_source: str, included_game_pks: list[int] | None):
    slate = await mlb_slate.build_slate(
        day, include_inhouse=(projection_source == "inhouse")
    )
    try:
        return slate, lineup_intake.build_lookup(
            slate,
            projection_source=projection_source,
            included_game_pks=included_game_pks,
        )
    except lineup_intake.IntakeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _store_my_lineups(day: str, entries: list[dict[str, Any]], *, replace: bool) -> list[dict[str, Any]]:
    """
    Append to (or replace) the day's tray, dropping exact repeats.

    De-duped on the roster itself rather than on the label, because the
    same ten players arriving twice -- once from the optimizer, once out
    of a DK entries file you already filled from it -- is one lineup you
    are entering once, and counting it twice would misstate both the
    portfolio size and its cost.
    """
    existing = [] if replace else get_my_lineups(day)
    seen = {frozenset(p["id"] for p in e["players"]) for e in existing}
    merged = list(existing)
    for e in entries:
        key = frozenset(p["id"] for p in e["players"])
        if key in seen:
            continue
        seen.add(key)
        merged.append(e)
    cache.put(_my_lineups_key(day), merged, _MY_LINEUPS_TTL)
    return merged


@router.get("/my-lineups")
async def list_my_lineups(date: str | None = Query(None)) -> dict[str, Any]:
    """
    The lineups you have set aside to actually enter today.

    These are YOURS -- built by the optimizer, read out of a filled
    DraftKings entries file, or typed in -- as opposed to the contest
    generator's field. Build a contest with `use_my_lineups=true` and
    they lead the batch, so the simulator prices them against that
    field and the build audit selects a portfolio from them rather than
    from thousands of randomly-constructed opponents.
    """
    day = date or date_cls.today().isoformat()
    entries = get_my_lineups(day)
    return {
        "date": day,
        "count": len(entries),
        "sources": [
            {"source": src, "count": n}
            for src, n in Counter(e.get("source") for e in entries).most_common()
        ],
        "entries": entries,
    }


@router.delete("/my-lineups")
async def clear_my_lineups(date: str | None = Query(None)) -> dict[str, Any]:
    day = date or date_cls.today().isoformat()
    cache.put(_my_lineups_key(day), [], _MY_LINEUPS_TTL)
    return {"date": day, "count": 0, "entries": []}


@router.post("/my-lineups")
async def add_my_lineups(
    date: str | None = Body(None, embed=True),
    lineups: list[dict[str, Any]] = Body(
        ...,
        embed=True,
        description=(
            "One object per lineup: {players: [...], label?}. Each player may be a "
            "DraftKings id, this app's player id, a name, or a player object carrying "
            "any of those -- so an optimizer lineup can be posted back verbatim."
        ),
    ),
    source: str = Body("manual", embed=True, description="manual or optimizer"),
    replace: bool = Body(False, embed=True, description="Replace the day's tray instead of adding to it"),
    projection_source: str = Body("rotowire", embed=True),
    included_game_pks: list[int] | None = Body(None, embed=True),
) -> dict[str, Any]:
    """
    Add lineups you built yourself to the day's tray.

    Every lineup is validated against this slate before it is accepted:
    ten players, no repeats, all on the slate with a salary and a
    projection, a legal assignment to DK's roster slots, and inside the
    salary cap. Anything that fails comes back in `rejected` with the
    reason -- an invalid lineup is never silently dropped or repaired,
    because a bad roster in the sim doesn't fail loudly, it just quietly
    makes every number that follows wrong.
    """
    day = date or date_cls.today().isoformat()
    if source not in lineup_intake.SOURCES:
        raise HTTPException(
            status_code=400,
            detail=f"source must be one of {', '.join(lineup_intake.SOURCES)}.",
        )
    _, lookup = await _intake_lookup(day, projection_source, included_game_pks)
    result = lineup_intake.intake(lineups, lookup, source=source)
    merged = _store_my_lineups(day, result["entries"], replace=replace)
    return {
        "date": day,
        "accepted": len(result["entries"]),
        "rejected": result["rejected"],
        "count": len(merged),
        "entries": merged,
    }


@router.post("/my-lineups/from-dk-entries")
async def add_my_lineups_from_dk(
    date: str | None = Query(None),
    contest_id: str | None = Query(None, description="Only read entries for this contest"),
    replace: bool = Query(False),
    projection_source: str = Query("rotowire"),
) -> dict[str, Any]:
    """
    Read the lineups you have already built inside DraftKings back in,
    from the same bulk-entries CSV you upload for the entry filler.

    This is the zero-effort manual path: no new format to learn and no
    retyping -- if you built lineups on DK's own site, export the
    entries file and they become your tray. Still-blank entry rows are
    reservations, not lineups, so they are skipped rather than reported
    as broken.
    """
    day = date or date_cls.today().isoformat()
    text = dk_entries.load(day)
    if not text:
        raise HTTPException(
            status_code=404,
            detail="No DK entries file uploaded for that date -- upload one via POST /dk-entries first.",
        )
    _, lookup = await _intake_lookup(day, projection_source, None)
    result = lineup_intake.from_dk_entries(
        dk_entries.parse_entries_csv(text), lookup, contest_id=contest_id
    )
    merged = _store_my_lineups(day, result["entries"], replace=replace)
    return {
        "date": day,
        "accepted": len(result["entries"]),
        "rejected": result["rejected"],
        "note": result.get("note"),
        "count": len(merged),
        "entries": merged,
    }


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
    engine: str = Body(
        "bootstrap",
        embed=True,
        description="'bootstrap' (default) samples each player's own historical DK-point outcome pool. 'atbat' instead runs genuine at-bat-level simulated games for the whole slate -- requires a confirmed lineup on both sides and a resolvable probable pitcher for every game on the slate.",
    ),
    field_sharpness: str = Body(
        "marquee",
        embed=True,
        description="How sharp the simulated field is: 'low' (softer/chalkier, more dispersed ownership), 'marquee' (default, a realistic large-field GPP), or 'high' (sharp bettors converging on the best pure value plays).",
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
            engine=engine,
            field_sharpness=field_sharpness,
            # Same determinism as the generator endpoints: identical
            # settings reproduce identical results (see _sim_seed) --
            # the uploaded entries are fixed anyway, so only the field
            # sample and sim draws vary.
            seed=_sim_seed(day, contest_id, projection_source, engine, 0),
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

    # Process audit of ALL of your entries in this contest (not just
    # the one identified above), scored against data/process_rules.py.
    # Team mapping comes from the MLB Stats API slate for that date so
    # stacks resolve even for dates whose salary cache has expired.
    audit = await _audit_standings(parsed, day, name, handle=my_handle, entry_id=my_entry_id)

    return {
        "date": day,
        "contest_id": cid,
        "field_size": len(parsed["entries"]),
        "players_found": len(parsed["player_pool"]),
        "my_entry": my_entry,
        "audit": audit,
    }


async def _audit_standings(
    parsed: dict[str, Any], day: str, contest_name: str, *, handle: str | None, entry_id: str | None
) -> dict[str, Any]:
    settings = get_settings()
    handle = handle or settings.dk_handle or None
    try:
        slate = await mlb_slate.build_slate(day, include_hitters=True)
        team_by_name = contest_audit.team_map_from_slate(slate)
    except Exception:  # noqa: BLE001
        team_by_name = None
    audit = contest_audit.audit_contest(
        parsed, handle=handle, entry_ids=[entry_id] if entry_id else None, team_by_name=team_by_name
    )
    audit["contest_name"] = contest_name
    audit["markdown"] = contest_audit.audit_to_markdown(audit, contest_name=contest_name)
    # Keep the day's audits where the next morning's brief can find them.
    key = f"contest_audits:{day}"
    existing = [a for a in (cache.get(key) or []) if a.get("contest_name") != contest_name]
    existing.insert(0, {k: v for k, v in audit.items() if k != "profiles"})
    cache.put(key, existing[:12], 14 * 24 * 3600)
    return audit


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


# ---------------------------------------------------------------------------
# Process audits and scheduled briefs (services/contest_audit.py,
# services/build_audit.py, services/briefs.py)
# ---------------------------------------------------------------------------


@router.post("/contest-audit")
async def audit_contest_standings(
    date: str | None = Query(None, description="Date the contest was played, defaults to today"),
    contest_name: str | None = Query(None),
    my_handle: str | None = Query(None, description="Your DK handle; defaults to DK_HANDLE in .env"),
    my_entry_id: str | None = Query(None),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    """Process-audit a DK contest-standings export WITHOUT archiving it
    -- the same audit /contest-results runs, for a file you only want
    the read on."""
    raw = await file.read()
    try:
        text = contest_results.extract_csv_text(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"Couldn't read that file: {exc}") from exc
    parsed = contest_results.parse_contest_standings(text)
    if not parsed["entries"]:
        raise HTTPException(status_code=400, detail="No contest entries found in that file.")
    day = date or date_cls.today().isoformat()
    return await _audit_standings(parsed, day, contest_name or "Contest", handle=my_handle, entry_id=my_entry_id)


@router.get("/contest-audits")
async def list_contest_audits(date: str | None = Query(None)) -> dict[str, Any]:
    day = date or date_cls.today().isoformat()
    return {"date": day, "audits": cache.get(f"contest_audits:{day}") or []}


def _spreadsheet_csv(text: str, filename: str, **headers: str) -> Response:
    """
    A CSV download meant to be opened in a spreadsheet.

    Prefixed with a UTF-8 byte-order mark, because Excel on Windows
    otherwise reads a BOM-less file as the system codepage and renders
    every accented name wrong -- "Rodriguez" comes out as "RodrÃ­guez".
    The BOM lives here rather than in lineup_export.lineups_to_csv() so
    the function's output stays clean for programmatic callers; only the
    file handed to a human carries it. The DraftKings entry filler is a
    different path and deliberately gets no BOM -- DK parses that file.
    """
    return Response(
        content="\ufeff" + text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', **headers},
    )


async def _audit_source(day: str, batch_id: str | None) -> tuple[str, list[dict[str, Any]]]:
    """The batch to audit: an explicit id (the full cached batch), else
    the last one built for the day."""
    if batch_id:
        cached = cache.get(f"contest_batch:{batch_id}")
        if not cached:
            raise HTTPException(status_code=404, detail="That batch has expired or was never built.")
        return batch_id, cached["entries"]
    latest = briefs.latest_batch(day)
    if not latest:
        raise HTTPException(status_code=404, detail="No contest batch has been built for that date yet.")
    # The day's pointer stores only the first 500 entries (it exists so
    # the scheduled brief has something to read even after a restart).
    # Those 500 are in build order, not the best 500, so selecting a
    # portfolio out of them would be picking from an arbitrary slice of
    # a 3,000-lineup contest. Prefer the full cached batch and fall back
    # to the snapshot only once it has expired.
    full = cache.get(f"contest_batch:{latest['batch_id']}")
    if full and full.get("entries"):
        return latest["batch_id"], full["entries"]
    return latest["batch_id"], latest["entries"]


async def _run_build_audit(
    day: str, batch_id: str | None, target_count: int | None
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    source_id, entries = await _audit_source(day, batch_id)
    slate = await mlb_slate.build_slate(day, include_hitters=True)
    audit = build_audit.audit_batch(entries, slate, target_count=target_count)
    return source_id, entries, audit


def _selected_entries(entries: list[dict[str, Any]], audit: dict[str, Any]) -> list[dict[str, Any]]:
    """The audit's chosen portfolio as real lineups, strongest first --
    the same order the DK entries filler will write them in."""
    return [entries[i] for i in (audit.get("selection") or {}).get("indices", []) if i < len(entries)]


@router.post("/build-audit")
async def audit_build(
    date: str | None = Query(None),
    batch_id: str | None = Query(None, description="A contest batch id; defaults to the latest batch built for the date"),
    target_count: int | None = Query(None, description="How many entries you actually intend to play"),
) -> dict[str, Any]:
    """
    Pre-entry process audit of a generated batch against the rules --
    pitcher core, stack conviction, batting order, filler, salary.

    The useful half is the SELECTED PORTFOLIO: the specific entries to
    play, constructed so the surviving set obeys the portfolio rules
    rather than just dropping bad lineups. Those entries are cached as
    their own batch under `keep_batch_id`, which means every tool that
    already takes a batch id works on them unchanged -- download it via
    GET /contest-entries/{keep_batch_id}/csv, or push it straight into
    a DraftKings entries template via GET /dk-entries/fill.
    """
    day = date or date_cls.today().isoformat()
    source_id, entries, audit = await _run_build_audit(day, batch_id, target_count)
    audit["markdown"] = build_audit.audit_to_markdown(audit)

    keep_batch_id = None
    kept = _selected_entries(entries, audit)
    if kept:
        keep_batch_id = uuid4().hex
        # Cached in exactly the shape every other batch consumer expects,
        # so the CSV download and the DK entry filler need no special
        # case for an audited portfolio. No results: the selection is a
        # process decision, not a simulated one, and attaching the
        # source batch's ranks would misalign them to these rows.
        cache.put(
            f"contest_batch:{keep_batch_id}",
            {"entries": kept, "results": None},
            _CONTEST_BATCH_TTL,
        )
        # Deliberately NOT remember_latest_batch(): that pointer means
        # "the build the user is about to play", and pointing it at the
        # audit's own output makes the next audit audit its own 20-lineup
        # answer instead of the real 500-lineup build -- a feedback loop
        # that silently shrinks the portfolio every run. The keep batch
        # is addressable by id; it is not the day's build.
        cache.put(f"contest_batch_day:{keep_batch_id}", day, _CONTEST_BATCH_TTL)

    audit = build_audit.trim_for_response(audit)
    return {
        "date": day,
        "batch_id": source_id,
        "keep_batch_id": keep_batch_id,
        # The whole selected portfolio, not a preview -- this is what the
        # UI tables and it is bounded by target_count, which is how many
        # entries a human is going to type in.
        "keep_entries": kept[:500],
        **audit,
    }


@router.get("/build-audit/csv")
async def download_build_audit_csv(
    date: str | None = Query(None),
    batch_id: str | None = Query(None, description="A contest batch id; defaults to the latest batch built for the date"),
    target_count: int | None = Query(None, description="How many entries you actually intend to play"),
    include: str = Query(
        "keep",
        description="'keep' (default) for just the portfolio to enter, 'all' for every audited lineup with its verdict",
    ),
) -> Response:
    """
    The audit as a spreadsheet rather than prose: one row per lineup
    with every roster slot, the salary/projection/ownership totals, and
    the audit's own columns -- verdict, the order to enter them in, and
    the reason in the language of the rules.

    'keep' is the file to work from. 'all' is for seeing what was cut
    and why, which is the question the written brief cannot answer at
    500 lineups.
    """
    day = date or date_cls.today().isoformat()
    source_id, entries, audit = await _run_build_audit(day, batch_id, target_count)
    verdicts = audit.get("verdicts") or []

    if include == "keep":
        rows = [(v, entries[v["index"]]) for v in verdicts if v["verdict"] == "keep" and v["index"] < len(entries)]
        rows.sort(key=lambda r: r[0]["keep_rank"] if r[0]["keep_rank"] is not None else 10**6)
    else:
        rows = [(v, entries[v["index"]]) for v in verdicts if v["index"] < len(entries)]

    csv_text = lineup_export.lineups_to_csv(
        [e for _, e in rows],
        extra_columns=[
            {
                "verdict": v["verdict"],
                "enter_order": (v["keep_rank"] + 1) if v["keep_rank"] is not None else "",
                "audit_reason": v["reason"],
                "source_lineup": v["index"] + 1,
            }
            for v, _ in rows
        ],
    )
    return _spreadsheet_csv(
        csv_text, f"build-audit-{include}-{day}.csv", **{"X-Source-Batch": source_id}
    )


@router.get("/briefs")
async def get_briefs_index() -> dict[str, Any]:
    """Every stored brief (last two weeks) plus what the scheduler will
    do next."""
    return {"briefs": briefs.list_briefs(), "schedule": await briefs.schedule_status()}


@router.get("/briefs/{kind}")
async def get_brief(kind: str, date: str | None = Query(None)) -> dict[str, Any]:
    if kind not in (briefs.MORNING, briefs.PRELOCK):
        raise HTTPException(status_code=404, detail="kind must be 'morning' or 'prelock'")
    day = date or date_cls.today().isoformat()
    brief = briefs.get_brief(day, kind)
    if not brief:
        return {"available": False, "date": day, "kind": kind}
    return {"available": True, **brief}


@router.post("/briefs/{kind}/run")
async def run_brief(
    kind: str,
    date: str | None = Query(None),
    force: bool = Query(True, description="Regenerate even if one exists for the day"),
    target_count: int | None = Query(None, description="Pre-lock only: entries you intend to play"),
) -> dict[str, Any]:
    """Run a brief now rather than waiting for the timer."""
    if kind not in (briefs.MORNING, briefs.PRELOCK):
        raise HTTPException(status_code=404, detail="kind must be 'morning' or 'prelock'")
    day = date or date_cls.today().isoformat()
    try:
        if kind == briefs.MORNING:
            brief = await briefs.run_morning(day, force=force)
        else:
            brief = await briefs.run_prelock(day, force=force, target_count=target_count)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"available": True, **brief}
