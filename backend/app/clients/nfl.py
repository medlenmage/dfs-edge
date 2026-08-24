"""
NFL schedule and betting-line data via nflverse's public CSV releases
(https://github.com/nflverse/nflverse-data) -- free, no key, no rate
limit worth worrying about. Same "unofficial CSV export" pattern as
clients/savant.py: this is a community data project, not an official
NFL feed, so if the shape changes without notice, callers just get an
empty/neutral result instead of a broken page.

`nfl_data_py` (the README's original suggestion) turned out to be a
dead end here -- it pins pandas<2.0, which has no build for this
project's Python version. Fetching nflverse's own CSV releases directly
sidesteps that entirely and matches how the rest of this app already
handles CSV-export data sources (no pandas anywhere in this codebase).

Unlike MLB, a DK NFL slate is weekly, not daily -- every function here
takes (season, week) rather than a date. "season" means the year the
season *starts* in (the 2026 season runs Sept 2026 - Feb 2027 and is
season=2026 for the whole span, including its January games).
"""

from __future__ import annotations

import csv
import io
from datetime import date as date_cls
from typing import Any

from app.cache import cached
from app.clients.http import get_text
from app.config import get_settings
from app.services import nfl_dk_points

GAMES_URL = "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv"
# The "player_stats" release this used to point at was deprecated by
# nflverse in favor of "stats_player" (confirmed live) and was never
# updated with a 2025 file at all -- a real, live gap, not a
# hypothetical one. "stats_player_week_{season}" is the per-game
# (not season-aggregate) weekly file, one row per player per game,
# covering both 2024 and 2025 as of writing. Its column names drifted
# from the deprecated release in two places _parse_stat_row() below
# accounts for: "recent_team" -> "team", "interceptions" ->
# "passing_interceptions".
PLAYER_STATS_URL_TEMPLATE = (
    "https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{season}.csv"
)

# The most recent FULLY COMPLETED season -- used as the default "prior
# season" context (defense-vs-position, pace) until the current
# season has played enough games of its own to trust in-season
# sampling. Bump this once a year, after the prior season's playoffs
# wrap (not on the calendar new year -- the season named for the
# earlier year runs into February).
PRIOR_SEASON = 2025

# The only position_group values DK's Classic roster scores individually
# (DST is scored as a team, not from these per-player rows).
_DK_RELEVANT_POSITIONS = {"QB", "RB", "WR", "TE"}


def _f(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _i(value: Any) -> int | None:
    f = _f(value)
    return int(f) if f is not None else None


async def _load_games_csv() -> str:
    settings = get_settings()

    async def _load() -> str:
        return await get_text(GAMES_URL, source="nflverse schedule")

    # The season's full schedule barely changes week to week (mostly
    # just which games have a final score) -- the schedule TTL is
    # generous, but this is a much bigger, slower-changing file than
    # MLB's daily schedule pull, so caching it is worth more here.
    return await cached("nfl:games:raw", settings.ttl_schedule, _load)


async def get_schedule(
    season: int, week: int | None = None, game_type: str = "REG"
) -> list[dict[str, Any]]:
    """
    Games for a season (optionally filtered to one week). `game_type`
    defaults to "REG" (regular season) -- nflverse also uses "PRE" for
    preseason (not present in this release as of writing) and
    "WC"/"DIV"/"CON"/"SB" for the playoff rounds.
    """
    text = await _load_games_csv()
    games: list[dict[str, Any]] = []
    for row in csv.DictReader(io.StringIO(text)):
        if _i(row.get("season")) != season or row.get("game_type") != game_type:
            continue
        row_week = _i(row.get("week"))
        if week is not None and row_week != week:
            continue
        games.append(
            {
                "game_id": row.get("game_id"),
                "season": season,
                "week": row_week,
                "game_type": row.get("game_type"),
                "gameday": row.get("gameday"),
                "gametime": row.get("gametime"),
                "weekday": row.get("weekday"),
                "away_team": row.get("away_team"),
                "home_team": row.get("home_team"),
                "away_moneyline": _f(row.get("away_moneyline")),
                "home_moneyline": _f(row.get("home_moneyline")),
                "spread_line": _f(row.get("spread_line")),
                "total_line": _f(row.get("total_line")),
                # Real final scores, once played -- None until the game
                # is final. Used by get_team_game_log() to derive DST
                # points-allowed (nflverse's own team-stats release has
                # no points-scored/allowed column of its own, only the
                # counting stats -- the real total lives here, in the
                # schedule's own result).
                "away_score": _i(row.get("away_score")),
                "home_score": _i(row.get("home_score")),
                "div_game": row.get("div_game") == "1",
                "roof": row.get("roof") or None,
                "surface": row.get("surface") or None,
                "stadium": row.get("stadium") or None,
            }
        )
    games.sort(key=lambda g: (g.get("gameday") or "", g.get("gametime") or ""))
    return games


def season_for_date(day: date_cls) -> int:
    """
    Which NFL season a calendar date falls in. The season is named for
    the year it starts -- a January/February date is still part of the
    season that kicked off the previous September.
    """
    return day.year if day.month >= 3 else day.year - 1


def current_week(season_games: list[dict[str, Any]], today_iso: str) -> int:
    """
    The week to show by default: the first one whose games aren't all
    in the past yet. Falls back to week 1 if the season hasn't started
    (every week is still upcoming) or the last week if it's fully over.
    """
    if not season_games:
        return 1
    weeks = sorted({g["week"] for g in season_games if g.get("week") is not None})
    for w in weeks:
        week_days = [g["gameday"] for g in season_games if g["week"] == w and g.get("gameday")]
        if week_days and max(week_days) >= today_iso:
            return w
    return weeks[-1] if weeks else 1


def implied_team_totals(game: dict[str, Any]) -> dict[str, float | None]:
    """
    Each side's implied point total from the closing total + spread --
    the same "how many is Vegas expecting from this team" signal MLB's
    implied runs already gives hitters, just for a different sport.

    Deliberately doesn't rely on knowing which direction nflverse's own
    `spread_line` sign convention points (documented inconsistently
    across sources, and getting it backwards would silently produce a
    wrong number for every game). Instead: the moneyline sign
    unambiguously says who's favored, and the standard total+spread
    split (half the total, plus or minus half the spread) is applied
    from there.
    """
    total = game.get("total_line")
    spread = game.get("spread_line")
    home_ml = game.get("home_moneyline")
    if total is None or spread is None or home_ml is None:
        return {"home": None, "away": None}

    half_total = total / 2
    half_spread = abs(spread) / 2
    if home_ml < 0:  # home team favored
        return {"home": round(half_total + half_spread, 1), "away": round(half_total - half_spread, 1)}
    return {"home": round(half_total - half_spread, 1), "away": round(half_total + half_spread, 1)}


def _parse_stat_row(row: dict[str, str]) -> dict[str, Any]:
    """
    One raw nflverse player_stats CSV row -> a clean dict with every
    counting stat converted to a real number -- shared by
    get_player_game_log() and get_prior_season_context() so both work
    from identically-typed data, and nfl_dk_points.game_points() never
    has to re-parse strings (matching mlb_dk_points.py's own
    already-numeric-input convention).
    """
    return {
        "player_id": row.get("player_id"),
        "player_name": row.get("player_display_name") or row.get("player_name"),
        "season": _i(row.get("season")),
        "week": _i(row.get("week")),
        "season_type": row.get("season_type"),
        "game_id": row.get("game_id"),
        "team": row.get("team"),
        "opponent_team": row.get("opponent_team"),
        "position": row.get("position"),
        "position_group": row.get("position_group"),
        "attempts": _f(row.get("attempts")) or 0.0,
        "completions": _f(row.get("completions")) or 0.0,
        "passing_yards": _f(row.get("passing_yards")) or 0.0,
        "passing_tds": _f(row.get("passing_tds")) or 0.0,
        "passing_interceptions": _f(row.get("passing_interceptions")) or 0.0,
        "carries": _f(row.get("carries")) or 0.0,
        "rushing_yards": _f(row.get("rushing_yards")) or 0.0,
        "rushing_tds": _f(row.get("rushing_tds")) or 0.0,
        "receptions": _f(row.get("receptions")) or 0.0,
        "targets": _f(row.get("targets")) or 0.0,
        "receiving_yards": _f(row.get("receiving_yards")) or 0.0,
        "receiving_tds": _f(row.get("receiving_tds")) or 0.0,
        "sack_fumbles_lost": _f(row.get("sack_fumbles_lost")) or 0.0,
        "rushing_fumbles_lost": _f(row.get("rushing_fumbles_lost")) or 0.0,
        "receiving_fumbles_lost": _f(row.get("receiving_fumbles_lost")) or 0.0,
        "passing_2pt_conversions": _f(row.get("passing_2pt_conversions")) or 0.0,
        "rushing_2pt_conversions": _f(row.get("rushing_2pt_conversions")) or 0.0,
        "receiving_2pt_conversions": _f(row.get("receiving_2pt_conversions")) or 0.0,
        "special_teams_tds": _f(row.get("special_teams_tds")) or 0.0,
    }


async def _load_player_stats_csv(season: int, *, force: bool = False) -> str:
    settings = get_settings()

    async def _load() -> str:
        url = PLAYER_STATS_URL_TEMPLATE.format(season=season)
        return await get_text(url, source="nflverse player stats")

    # A completed season's box scores never change -- cache far longer
    # than the in-season TTLs above. A season still in progress gets
    # re-pulled at the same cadence regardless (nflverse republishes
    # this file weekly during the season); `force` bypasses it either
    # way for an explicit refresh.
    return await cached(f"nfl:player_stats:{season}:raw", settings.ttl_stats * 4, _load, force=force)


async def get_grouped_season_stats(
    season: int, *, force: bool = False
) -> dict[str, list[dict[str, Any]]]:
    """
    The full season's regular-season player_stats, parsed ONCE and
    grouped by player_id (each player's own rows sorted by week) --
    the shared building block get_player_game_log() and a real bulk
    classification pass (e.g. nfl_contest._classify_pool(), checking
    running-QB/pass-catching-RB status across every QB/RB in a whole
    week's pool at once) both use, instead of each re-scanning every
    row in the ~9,000-row season file from scratch. A real, measured
    cost this replaces: classifying a full week's 60-100+ QB/RB pool
    one get_player_game_log() call at a time (the original
    implementation) took over two minutes; grouping once and reusing
    it is a single pass over the file regardless of how many players
    get looked up afterward.

    Cached as this parsed structure (not just the raw CSV text), same
    TTL as the raw file.
    """
    settings = get_settings()

    async def _load() -> dict[str, list[dict[str, Any]]]:
        text = await _load_player_stats_csv(season, force=force)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in csv.DictReader(io.StringIO(text)):
            if row.get("season_type") != "REG":
                continue
            pid = row.get("player_id")
            if not pid:
                continue
            grouped.setdefault(pid, []).append(_parse_stat_row(row))
        for rows in grouped.values():
            rows.sort(key=lambda r: r["week"] or 0)
        return grouped

    return await cached(f"nfl:player_stats:{season}:grouped", settings.ttl_stats * 4, _load, force=force)


async def get_player_game_log(
    player_id: str, season: int, *, force: bool = False
) -> list[dict[str, Any]]:
    """
    One row per regular-season game a player appeared in during
    `season` -- raw counting stats (already numeric, see
    _parse_stat_row()), sorted by week. The building block a future
    NFL variance/outcome-pool model would use the same way MLB's
    variance.py already uses clients/mlb.get_player_game_log() +
    mlb_dk_points.py: feed each row to nfl_dk_points.game_points() to
    get a real per-game DK-point outcome.

    Postseason games are excluded -- matching get_prior_season_context()'s
    own REG-only convention, since not every team makes the playoffs, so
    mixing them in would make some players' logs deeper than others for
    a reason unrelated to their actual week-to-week volatility.
    """
    grouped = await get_grouped_season_stats(season, force=force)
    return grouped.get(player_id, [])


async def get_prior_season_context(season: int = PRIOR_SEASON) -> dict[str, Any]:
    """
    Defense-vs-position and pace, computed from one full completed
    season of real box scores -- the same "how has this defense
    performed against this position" and "how many plays does this
    offense run" checks a DFS player does by hand, automated. Used as a
    static prior until the current season has played enough games of
    its own to trust in-season sampling (nothing else in this model has
    that yet either -- see nfl_scoring.py's module docstring).

    Team codes here are nflverse's own, matching `get_schedule()`'s
    `home_team`/`away_team` directly -- no DK-alias translation needed,
    since both come from the same source.
    """

    async def _load() -> dict[str, Any]:
        text = await _load_player_stats_csv(season)

        # allowed[def_team][position_group][week] = DK pts allowed that game
        allowed: dict[str, dict[str, dict[int, float]]] = {}
        # plays[team][week] = offensive plays run that game
        plays: dict[str, dict[int, float]] = {}

        for raw_row in csv.DictReader(io.StringIO(text)):
            if raw_row.get("season_type") != "REG":
                continue
            row = _parse_stat_row(raw_row)
            week = row["week"]
            if week is None:
                continue

            team = row["team"] or ""
            if team:
                team_plays = plays.setdefault(team, {})
                snaps = row["attempts"] + row["carries"]
                team_plays[week] = team_plays.get(week, 0.0) + snaps

            pos_group = row["position_group"] or ""
            opp = row["opponent_team"] or ""
            if opp and pos_group in _DK_RELEVANT_POSITIONS:
                pos_allowed = allowed.setdefault(opp, {}).setdefault(pos_group, {})
                pos_allowed[week] = pos_allowed.get(week, 0.0) + nfl_dk_points.game_points(row)

        defense_vs_position = {
            team: {pos: round(sum(weeks.values()) / len(weeks), 2) for pos, weeks in by_pos.items()}
            for team, by_pos in allowed.items()
        }
        pace = {team: round(sum(weeks.values()) / len(weeks), 2) for team, weeks in plays.items()}

        league_avg_defense_vs_position = {
            pos: round(sum(v) / len(v), 2)
            for pos in _DK_RELEVANT_POSITIONS
            if (v := [by_pos[pos] for by_pos in defense_vs_position.values() if pos in by_pos])
        }
        league_avg_pace = round(sum(pace.values()) / len(pace), 2) if pace else None

        return {
            "season": season,
            "defense_vs_position": defense_vs_position,
            "league_avg_defense_vs_position": league_avg_defense_vs_position,
            "pace": pace,
            "league_avg_pace": league_avg_pace,
        }

    settings = get_settings()
    return await cached(f"nfl:prior_season_context:{season}", settings.ttl_stats * 4, _load)


# --------------------------------------------------------------------------
# Team (DST) game logs -- nflverse's "stats_team" release. DK's DST
# scoring needs team-level defensive/special-teams counting stats
# (sacks, INTs, fumble recoveries, defensive/ST TDs, blocked kicks) and
# points allowed -- none of which live in the per-PLAYER file above.
# Confirmed live: "stats_team" has no points-scored/allowed column of
# its own, so points_allowed is joined in from get_schedule()'s real
# final scores (the opponent's own score that game).
# --------------------------------------------------------------------------

TEAM_STATS_URL_TEMPLATE = (
    "https://github.com/nflverse/nflverse-data/releases/download/stats_team/stats_team_week_{season}.csv"
)


def _parse_team_stat_row(row: dict[str, str]) -> dict[str, Any]:
    return {
        "team": row.get("team"),
        "season": _i(row.get("season")),
        "week": _i(row.get("week")),
        "season_type": row.get("season_type"),
        "game_id": row.get("game_id"),
        "opponent_team": row.get("opponent_team"),
        "def_sacks": _f(row.get("def_sacks")) or 0.0,
        "def_interceptions": _f(row.get("def_interceptions")) or 0.0,
        "fumble_recovery_opp": _f(row.get("fumble_recovery_opp")) or 0.0,
        "def_tds": _f(row.get("def_tds")) or 0.0,
        "special_teams_tds": _f(row.get("special_teams_tds")) or 0.0,
        "def_safeties": _f(row.get("def_safeties")) or 0.0,
        "def_punt_blocks": _f(row.get("def_punt_blocks")) or 0.0,
        "def_pat_blocks": _f(row.get("def_pat_blocks")) or 0.0,
        "def_fg_blocks": _f(row.get("def_fg_blocks")) or 0.0,
    }


async def _load_team_stats_csv(season: int, *, force: bool = False) -> str:
    settings = get_settings()

    async def _load() -> str:
        url = TEAM_STATS_URL_TEMPLATE.format(season=season)
        return await get_text(url, source="nflverse team stats")

    return await cached(f"nfl:team_stats:{season}:raw", settings.ttl_stats * 4, _load, force=force)


async def get_team_game_log(team: str, season: int, *, force: bool = False) -> list[dict[str, Any]]:
    """
    One row per regular-season game a team's defense/special-teams unit
    played during `season` -- real counting stats plus `points_allowed`
    (joined from get_schedule()'s real final scores), sorted by week.
    The DST equivalent of get_player_game_log(); feeds
    nfl_dk_points.dst_game_points().
    """
    text = await _load_team_stats_csv(season, force=force)
    games = await get_schedule(season)
    games_by_id = {g["game_id"]: g for g in games if g.get("game_id")}

    rows = []
    for raw_row in csv.DictReader(io.StringIO(text)):
        if raw_row.get("team") != team or raw_row.get("season_type") != "REG":
            continue
        row = _parse_team_stat_row(raw_row)
        game = games_by_id.get(row["game_id"])
        if game and game.get("home_score") is not None and game.get("away_score") is not None:
            row["points_allowed"] = (
                game["away_score"] if game["home_team"] == team else game["home_score"]
            )
        else:
            row["points_allowed"] = None
        rows.append(row)

    rows.sort(key=lambda r: r["week"] or 0)
    return rows
