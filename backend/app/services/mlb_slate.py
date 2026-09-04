"""
Builds the daily MLB slate: every game, its environment, and every
hitter's matchup edge.

THE FLOW
--------
1. Pull today's schedule (games, venues, probable pitchers).
2. Pull the whole league's splits in a handful of requests, not one per
   player.
3. Pull FantasyLabs' free Vegas lines (score/total/implied runs, open
   and current) and The Odds API's event ids (needed only for player
   props), matching both to games by team name.
4. Pull a weather forecast per outdoor park.
5. For each hitter, assemble the components and score the matchup.

Steps 2-4 run concurrently because they don't depend on each other.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date as date_cls
from datetime import datetime
from typing import Any

from app import cache
from app.clients import fantasylabs, mlb, odds, rotowire_umpires, savant, weather
from app.data.parks import get_park, hr_factor_for_hand
from app.services import (
    inhouse_projections,
    pitcher_skill,
    projections,
    salaries,
    scoring,
    variance,
)

log = logging.getLogger(__name__)

PITCHER_POSITIONS = {"P", "SP", "RP", "TWP"}


# --------------------------------------------------------------------------
# Team-name matching between MLB and the sportsbooks
# --------------------------------------------------------------------------

def _norm(name: str | None) -> str:
    return re.sub(r"[^a-z]", "", (name or "").lower())


def _nickname(name: str | None) -> str:
    """Last word of a team name -- 'Yankees', 'Athletics', 'Red Sox' -> 'sox'."""
    parts = (name or "").split()
    return _norm(parts[-1]) if parts else ""


def _match_odds(
    lines: list[dict[str, Any]], home_name: str, away_name: str
) -> dict[str, Any] | None:
    """Find the betting line for a game, tolerating naming differences."""
    h, a = _norm(home_name), _norm(away_name)
    for line in lines:
        if _norm(line.get("home_team")) == h and _norm(line.get("away_team")) == a:
            return line

    hn, an = _nickname(home_name), _nickname(away_name)
    for line in lines:
        if (
            _nickname(line.get("home_team")) == hn
            and _nickname(line.get("away_team")) == an
        ):
            return line
    return None


# --------------------------------------------------------------------------
# Player props -> per-player market signals, keyed by normalized name so
# _team_hitters()/_pitcher_edge() can look a specific player up against
# their own already-fetched roster bios (see clients/odds.get_player_props
# for the raw {player, side, line, price, implied_pct} row shape).
# --------------------------------------------------------------------------

def _market_at_least_one_pct_by_name(rows: list[dict[str, Any]]) -> dict[str, float]:
    """
    From an Over/Under market (batter_home_runs, batter_hits), the
    market-implied probability of clearing the LOWEST available line for
    each player -- almost always "Over 0.5", i.e. "at least one tonight",
    the DFS-relevant threshold for both markets. A player occasionally
    priced at multiple lines (0.5 AND 1.5) correctly picks the 0.5 row.
    """
    best_line: dict[str, float] = {}
    pct: dict[str, float] = {}
    for row in rows:
        if row.get("side") != "Over":
            continue
        line, implied = row.get("line"), row.get("implied_pct")
        name = row.get("player")
        if line is None or implied is None or not name:
            continue
        key = salaries.normalize_name(name)
        if key not in best_line or line < best_line[key]:
            best_line[key] = line
            pct[key] = implied
    return pct


def _market_line_by_name(rows: list[dict[str, Any]]) -> dict[str, float]:
    """
    From an Over/Under market (pitcher_strikeouts), each player's own
    posted line -- both the Over and Under rows share the same line
    value, so either one gives the real number.
    """
    out: dict[str, float] = {}
    for row in rows:
        line, name = row.get("line"), row.get("player")
        if line is None or not name:
            continue
        out[salaries.normalize_name(name)] = line
    return out


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------

async def build_slate(
    day: str | None = None,
    *,
    force_refresh: bool = False,
    include_hitters: bool = True,
    include_inhouse: bool = False,
) -> dict[str, Any]:
    day = day or date_cls.today().isoformat()
    season = int(day[:4])

    games = await mlb.get_schedule(day, force=force_refresh)
    if not games:
        return {
            "date": day,
            "season": season,
            "games": [],
            "message": f"No MLB games scheduled for {day}.",
        }

    # --- Fetch everything that doesn't depend on anything else, at once ---
    tasks = {
        "hit_vl": mlb.get_league_splits(season, "vl", "hitting"),
        "hit_vr": mlb.get_league_splits(season, "vr", "hitting"),
        "hit_home": mlb.get_league_splits(season, "h", "hitting"),
        "hit_away": mlb.get_league_splits(season, "a", "hitting"),
        "hit_season": mlb.get_league_season(season, "hitting"),
        "hit_recent": mlb.get_recent_form(season, 15, "hitting"),
        "pit_vl": mlb.get_league_splits(season, "vl", "pitching"),
        "pit_vr": mlb.get_league_splits(season, "vr", "pitching"),
        "pit_season": mlb.get_league_season(season, "pitching"),
        "lines": odds.get_game_lines("mlb", day=day, force=force_refresh),
        "fantasylabs_vegas": fantasylabs.get_vegas_odds(day, force=force_refresh),
        "umpires": rotowire_umpires.get_todays_umpires(force=force_refresh),
        "savant_hit": savant.get_hitter_batted_ball(season),
        "savant_pitch": savant.get_pitcher_batted_ball(season),
        "savant_pitch_skills": savant.get_pitcher_plate_skills(season),
        "bullpen": mlb.get_bullpen_stats(season),
        "bullpen_workload": mlb.get_recent_bullpen_workload(day),
    }
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    data: dict[str, Any] = {}
    warnings: list[str] = []
    for key, result in zip(tasks.keys(), results):
        if isinstance(result, Exception):
            log.warning("Fetch %s failed: %s", key, result)
            warnings.append(f"{key}: {result}")
            data[key] = [] if key in ("lines", "fantasylabs_vegas") else {}
        else:
            data[key] = result

    lines: list[dict[str, Any]] = data["lines"]

    # --- League baselines, computed from the data itself ---
    baselines = {
        "hitter_ops_vl": scoring.league_average(data["hit_vl"], "ops", 40, "pa"),
        "hitter_ops_vr": scoring.league_average(data["hit_vr"], "ops", 80, "pa"),
        "pitcher_ops_vl": scoring.league_average(data["pit_vl"], "ops_against", 50, "bf"),
        "pitcher_ops_vr": scoring.league_average(data["pit_vr"], "ops_against", 50, "bf"),
        "pitcher_era": scoring.league_average(data["pit_season"], "era", 20, "ip"),
        "pitcher_k9": scoring.league_average(data["pit_season"], "k_per_9", 20, "ip"),
        "hitter_k_pct": scoring.league_average(data["hit_season"], "k_pct", 80, "pa"),
        "hitter_sb_per_pa": scoring.league_average(data["hit_season"], "sb_per_pa", 100, "pa"),
        "hitter_hr_per_pa": scoring.league_average(data["hit_season"], "hr_per_pa", 80, "pa"),
        "pitcher_hr_per_9": scoring.league_average(data["pit_season"], "hr_per_9", 20, "ip"),
        # Savant's leaderboard is already filtered to a minimum sample
        # (see clients/savant.py), so no further sample-size gate here.
        "hitter_barrel": scoring.league_average(data["savant_hit"], "barrel_pct", 0, "barrel_pct"),
        "hitter_hard_hit": scoring.league_average(data["savant_hit"], "hard_hit_pct", 0, "hard_hit_pct"),
        "hitter_xwoba": scoring.league_average(data["savant_hit"], "xwoba", 0, "xwoba"),
        "pitcher_barrel": scoring.league_average(data["savant_pitch"], "barrel_pct", 0, "barrel_pct"),
        "pitcher_hard_hit": scoring.league_average(data["savant_pitch"], "hard_hit_pct", 0, "hard_hit_pct"),
        "pitcher_xwoba": scoring.league_average(data["savant_pitch"], "xwoba", 0, "xwoba"),
        # get_bullpen_stats() already filters to teams with a trustworthy
        # sample of relief innings, so no further gate here either.
        "bullpen_era": scoring.league_average(data["bullpen"], "era", 0, "era"),
        "bullpen_workload_outs": scoring.league_average(data["bullpen_workload"], "outs", 0, "outs"),
        # Self-calibrating league-average umpire RPG/KPG from whatever
        # RotoWire has posted TODAY (not a fixed guessed constant) --
        # None (not a fake number) until at least 5 real games' worth
        # of umpire-games have posted, same "don't trust a thin sample"
        # gate every other league_average() call here already uses.
        "umpire_avg_rpg": scoring.league_average(data["umpires"], "rpg", 5, "games"),
        "umpire_avg_kpg": scoring.league_average(data["umpires"], "kpg", 5, "games"),
    }

    # --- Pitcher skill: SIERA, xFIP and CSW ---
    # ERA is a result, not an ability, and ranking starters on it is how
    # a pitcher who has been unlucky in front of a bad defense grades out
    # behind one who hasn't. SIERA and xFIP come free from data already
    # fetched above; CSW does not, so it is counted only for the pitchers
    # actually starting tonight -- see services/pitcher_skill.py.
    skill_table = pitcher_skill.build_skill_table(
        data["pit_season"], data["savant_pitch_skills"]
    )
    starter_ids = [
        pid
        for g in games
        for side in ("home", "away")
        if (pid := (((g.get("teams") or {}).get(side) or {}).get("probablePitcher") or {}).get("id"))
    ]
    try:
        skill_table = await pitcher_skill.attach_csw(skill_table, starter_ids, season)
    except Exception as exc:  # noqa: BLE001 -- CSW is one input to one component
        log.warning("CSW pass failed, falling back to K/9: %s", exc)
        warnings.append(f"csw: {exc}")
    data["pitcher_skill"] = skill_table["pitchers"]
    baselines["pitcher_siera"] = skill_table["league"].get("siera")
    baselines["pitcher_csw"] = skill_table["league"].get("csw")
    # This season's real HR/FB, not a hardcoded one -- the at-bat sim
    # uses it to replace each starter's realised homer rate with the one
    # his fly-ball rate implies.
    baselines["pitcher_hr_fb"] = skill_table["league"].get("hr_fb")

    # Salaries and projections are manual uploads, not a fetch -- see
    # services/salaries.py and services/projections.py. Whatever's
    # cached for this date (possibly nothing) gets matched in.
    salary_rows = salaries.load(day)
    salary_lookup = salaries.build_lookup(salary_rows)
    projection_lookup = projections.build_lookup(projections.load(day))

    # Which games the uploaded DK salary CSV actually covers, so each
    # game can be flagged as in/out of that slate for the optimizer (and
    # anything else that wants it) -- None (not True/False) when no
    # salary CSV is loaded yet, since there's nothing to detect against.
    detected_slate_pairs = (
        {frozenset((g["away"], g["home"])) for g in salaries.slate_games(salary_rows)}
        if salary_rows
        else None
    )
    in_slate_pks = (
        _resolve_slate_game_pks(games, detected_slate_pairs)
        if detected_slate_pairs is not None
        else None
    )

    built = await asyncio.gather(
        *[
            _build_game(
                g, season, data, baselines, lines, include_hitters,
                salary_lookup, projection_lookup, day, in_slate_pks,
                force_refresh=force_refresh,
            )
            for g in games
        ],
        return_exceptions=True,
    )

    out_games = []
    for game, result in zip(games, built):
        if isinstance(result, Exception):
            log.exception("Failed to build game %s", game.get("gamePk"))
            warnings.append(f"game {game.get('gamePk')}: {result}")
            continue
        out_games.append(result)

    out_games.sort(key=lambda g: g.get("game_time_utc") or "")

    # In-house FPTS projections are opt-in: computing them means a real
    # per-player game-log fetch for every hitter/pitcher on the slate
    # (a couple hundred players on a full day), which would otherwise
    # silently add real latency to every plain dashboard refresh. Cheap
    # once cached, but the first call of the day pays for it, so this
    # stays off unless explicitly asked for.
    if include_inhouse and include_hitters:
        await _attach_inhouse_projections(out_games, season)

    return {
        "date": day,
        "season": season,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "baselines": baselines,
        "games": out_games,
        "warnings": warnings,
    }


def team_context_rows(slate: dict[str, Any]) -> list[dict[str, Any]]:
    """
    One row per team on a built slate: its Vegas implied runs, the game
    total, and the starter it faces.

    Pulled out as its own function so history_db can archive exactly
    what project_ownership()'s team-stack layer consumes -- without
    these, a historical rebuild feeds that layer None and it goes
    neutral, which silently understates the model (see
    history_db.archive_slate_team_context). Only teams with something
    real to record are returned; a slate built before odds land yields
    nothing rather than a row of nulls that would later look like
    genuine zeros.
    """
    rows: list[dict[str, Any]] = []
    for g in slate.get("games") or []:
        for side in ("home", "away"):
            t = g.get(side) or {}
            opp = g.get("away" if side == "home" else "home") or {}
            team = t.get("abbrev")
            if not team:
                continue
            opp_pitcher = opp.get("probable_pitcher") or {}
            implied = t.get("vegas_implied_runs_current") or t.get("implied_runs")
            row = {
                "team": team,
                "opponent": opp.get("abbrev"),
                "implied_runs": implied,
                "implied_runs_open": t.get("vegas_implied_runs_open"),
                "game_total": (g.get("betting") or {}).get("total"),
                "opposing_pitcher_id": opp_pitcher.get("id"),
                "opposing_pitcher_salary": (opp_pitcher.get("salary") or {}).get("salary"),
                "lineup_confirmed": bool(t.get("lineup_confirmed")),
            }
            if any(
                row[k] is not None
                for k in ("implied_runs", "implied_runs_open", "game_total", "opposing_pitcher_id")
            ):
                rows.append(row)
    return rows


def inhouse_projection_rows(slate: dict[str, Any]) -> list[dict[str, Any]]:
    """
    One row per player carrying this slate's computed in-house FPTS and
    ownership, keyed the way slate_projections is (normalized name +
    team), for history_db.archive_inhouse_projections().

    Only players that actually have an in-house number are returned --
    a slate built without include_inhouse yields nothing, so calling
    this unconditionally is safe.
    """
    from app.services import player_match

    rows: list[dict[str, Any]] = []
    for g in slate.get("games") or []:
        for side in ("home", "away"):
            t = g.get(side) or {}
            team = t.get("abbrev")
            if not team:
                continue
            people = list(t.get("hitters") or [])
            pitcher = t.get("probable_pitcher")
            if pitcher:
                people.append(pitcher)
            for pl in people:
                proj = pl.get("projection") or {}
                fpts = proj.get("inhouse_fpts")
                own = proj.get("inhouse_ownership_pct")
                if fpts is None and own is None:
                    continue
                name = pl.get("name")
                if not name:
                    continue
                rows.append(
                    {
                        "normalized_name": player_match.normalize_name(name),
                        "team": team,
                        "inhouse_fpts": fpts,
                        "inhouse_ownership_pct": own,
                    }
                )
    return rows


def _dk_slot_position(salary_position: str | None, fallback: str) -> str:
    """The first-listed DK roster-slot code from a multi-eligible salary
    string (e.g. "1B/3B" -> "1B"), or `fallback` if no salary CSV is
    loaded for this player. Kept for callers that genuinely need one
    slot; ownership uses _dk_slot_positions (plural) below instead."""
    if not salary_position:
        return fallback
    return salary_position.split("/")[0].strip() or fallback


def _dk_slot_positions(salary_position: str | None, fallback: str) -> list[str]:
    """
    EVERY DK roster-slot code a player is eligible at ("1B/3B" ->
    ["1B", "3B"]), or [fallback] with no salary CSV loaded.

    Ownership needs all of them, not just the first: a player's real
    %Drafted is his share of entries rostering him at ANY eligible
    slot, so a scarce shortstop DK lists as "2B/SS" was previously
    invisible to the SS group entirely -- his ownership went missing
    and the remaining SS chalk was overstated to fill the group's 100%.
    """
    if not salary_position:
        return [fallback]
    slots = [s.strip() for s in salary_position.split("/") if s.strip()]
    return slots or [fallback]


def _ownership_eligible_hitters(hitters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    The hitters who should actually compete for ownership on one team:
    the (confirmed or projected) STARTING NINE, not the whole roster.

    Each position group's softmax total is fixed at slots x 100%, so
    before lineups confirm, letting every rostered hitter into the pool
    split e.g. 300% of OF ownership across ~13 outfielders per team
    instead of the ~6 who will actually play -- roughly halving every
    real starter's number, then re-sharpening it unpredictably and
    JUMPING when lineups locked. A bench bat nobody can roster in a
    real lineup gets 0%, not a share.

    Confirmed batting order wins; RotoWire's projected order stands in
    before confirmation (the same fallback atbat_sim.py already uses);
    a team with neither falls back to its top 9 by season plate
    appearances -- playing time, not talent, since "who starts" is a
    usage question.
    """
    in_lineup = [
        h for h in hitters
        if h.get("batting_order") is not None or h.get("projected_batting_order") is not None
    ]
    if in_lineup:
        return in_lineup
    by_pa = sorted(hitters, key=lambda h: (h.get("season") or {}).get("pa") or 0, reverse=True)
    return by_pa[:9]


async def _attach_inhouse_projections(out_games: list[dict[str, Any]], season: int) -> None:
    """
    Adds projection["inhouse_fpts"] onto every hitter and probable
    pitcher across every built game, additive alongside whatever
    RotoWire projection is already there (or None), then
    projection["inhouse_ownership_pct"] on top of that wherever a DK
    salary is also loaded (ownership is meaningless without a real
    salary-capped contest to be owned in), then
    projection["inhouse_ceiling"]/["leverage_score"] (ceiling minus
    ownership -- real upside the field is under-rostering) wherever
    both a ceiling and an ownership number exist. Mutates in place.

    A probable pitcher only gets an "edge" key when _pitcher_edge()
    could actually compute one (missing season stats, etc. skip it) --
    inhouse_fpts_batch() needs edge["composite"] for every player it's
    given, so pitchers without one are excluded here rather than
    passed through and KeyError'd inside the batch call.
    """
    all_players: list[dict[str, Any]] = []
    for g in out_games:
        for side in ("home", "away"):
            all_players.extend(g[side]["hitters"])
            pitcher = g[side]["probable_pitcher"]
            if pitcher and pitcher.get("edge"):
                # This pitcher's own team's market-implied win probability
                # (moneyline, already fetched alongside the game's total/
                # spread) -- inhouse_fpts_batch() uses it to correct the
                # baseline's own historical win rate toward what the
                # market thinks TODAY, rather than assuming his season-
                # long average win rate applies to every start.
                win_probability_pct = odds.american_to_probability(g[side].get("moneyline"))
                all_players.append(
                    {**pitcher, "position": "P", "win_probability_pct": win_probability_pct}
                )

    inhouse = await inhouse_projections.inhouse_fpts_batch(all_players, season)
    if not inhouse:
        return

    # Keyed by player id rather than a flat list: on a DOUBLEHEADER day
    # the same real player appears on two games' rosters, and letting
    # both occurrences into the pool scored him twice -- diluting his
    # groupmates' shares and (once multi-slot summing landed) double-
    # counting his own. One entry per player, preferring the occurrence
    # from the game that's actually in the loaded DK slate.
    ownership_pool_by_id: dict[int, dict[str, Any]] = {}
    for g in out_games:
        for side in ("home", "away"):
            implied_runs = g[side]["implied_runs"]
            # The pitcher this side's hitters actually face -- already
            # sitting right there in the same already-built game dict,
            # no new fetch needed. Feeds project_ownership()'s opposing-
            # pitcher-chalk leverage adjustment.
            opp_side = "away" if side == "home" else "home"
            opponent_pitcher = g[opp_side]["probable_pitcher"]
            opponent_pitcher_id = opponent_pitcher.get("id") if opponent_pitcher else None
            for hitter in _ownership_eligible_hitters(g[side]["hitters"]):
                fpts = inhouse.get(hitter["id"])
                salary_info = hitter.get("salary")
                if fpts is not None and salary_info:
                    if hitter["id"] in ownership_pool_by_id and not g.get("in_slate"):
                        continue
                    ownership_pool_by_id[hitter["id"]] = (
                        {
                            "id": hitter["id"],
                            "position": _dk_slot_position(salary_info.get("position"), hitter["position"]),
                            # Every eligible slot, not just the first --
                            # see _dk_slot_positions. project_ownership
                            # scores him in each group and reports the
                            # sum, matching what %Drafted really means.
                            "positions": _dk_slot_positions(salary_info.get("position"), hitter["position"]),
                            "salary": salary_info["salary"],
                            "fpts": fpts,
                            "implied_runs": implied_runs,
                            "opponent_pitcher_id": opponent_pitcher_id,
                            # Feeds project_ownership()'s team-stack layer --
                            # MLB hitter ownership is driven team-first, so
                            # teammates have to be scored together. Optional
                            # by design: the layer degrades to a neutral
                            # team score rather than raising, so a game dict
                            # assembled without an abbrev can still get
                            # in-house projections attached.
                            "team": g[side].get("abbrev"),
                        }
                    )
            pitcher = g[side]["probable_pitcher"]
            if pitcher and pitcher.get("edge"):
                fpts = inhouse.get(pitcher["id"])
                salary_info = pitcher.get("salary")
                if fpts is not None and salary_info:
                    # Same doubleheader dedupe as the hitters above --
                    # keyed under a "P:"-prefixed id purely so a two-way
                    # player's pitching entry can't clobber his hitting
                    # one.
                    if f"P:{pitcher['id']}" not in ownership_pool_by_id or g.get("in_slate"):
                        ownership_pool_by_id[f"P:{pitcher['id']}"] = {
                            "id": pitcher["id"],
                            "position": "P",
                            "salary": salary_info["salary"],
                            "fpts": fpts,
                            "implied_runs": implied_runs,
                        }

    ownership = inhouse_projections.project_ownership(list(ownership_pool_by_id.values()))
    # Real, data-driven ceilings for the same batch -- the "upside" half
    # of a leverage score. Computed for everyone with an edge/composite
    # (not just the ones that made it into ownership_pool), so leverage
    # can still show up even before a DK salary is loaded.
    ceilings = await inhouse_projections.player_ceilings(all_players, season)
    # Boom/bust tail reads of the same pools -- what fraction of each
    # player's real games would have cleared 1.5x today's projection,
    # and what fraction were the nightmare night (see
    # variance.boom_bust_from_pool for thresholds and calibration).
    boom_bust = await inhouse_projections.player_boom_bust(all_players, season, inhouse)

    def _leverage(ceiling: float | None, own_pct: float | None) -> float | None:
        if ceiling is None or own_pct is None:
            return None
        return round(ceiling - own_pct, 2)

    for g in out_games:
        for side in ("home", "away"):
            for hitter in g[side]["hitters"]:
                value = inhouse.get(hitter["id"])
                if value is not None:
                    hitter["projection"] = {**(hitter["projection"] or {}), "inhouse_fpts": value}
                bb = boom_bust.get(hitter["id"])
                if bb:
                    hitter["projection"] = {**(hitter["projection"] or {}), **bb}
                own_pct = ownership.get(hitter["id"])
                if own_pct is not None:
                    hitter["projection"] = {**(hitter["projection"] or {}), "inhouse_ownership_pct": own_pct}
                ceiling = ceilings.get(hitter["id"])
                leverage = _leverage(ceiling, own_pct)
                if leverage is not None:
                    hitter["projection"] = {
                        **(hitter["projection"] or {}),
                        "inhouse_ceiling": ceiling,
                        "leverage_score": leverage,
                    }
            pitcher = g[side]["probable_pitcher"]
            if pitcher and pitcher.get("edge"):
                value = inhouse.get(pitcher["id"])
                if value is not None:
                    pitcher["projection"] = {**(pitcher["projection"] or {}), "inhouse_fpts": value}
                bb = boom_bust.get(pitcher["id"])
                if bb:
                    pitcher["projection"] = {**(pitcher["projection"] or {}), **bb}
                own_pct = ownership.get(pitcher["id"])
                if own_pct is not None:
                    pitcher["projection"] = {**(pitcher["projection"] or {}), "inhouse_ownership_pct": own_pct}
                ceiling = ceilings.get(pitcher["id"])
                leverage = _leverage(ceiling, own_pct)
                if leverage is not None:
                    pitcher["projection"] = {
                        **(pitcher["projection"] or {}),
                        "inhouse_ceiling": ceiling,
                        "leverage_score": leverage,
                    }

    # Stack-level boom/bust for the Stacks tab: a real correlated Monte
    # Carlo over each side's top-5 bats' own outcome pools (teammates
    # share each trial's team-environment draw, exactly as the full
    # simulator correlates them) -- NOT five independent tail reads
    # multiplied together, which would thin both tails. Top 5 by the
    # same displayed projection the per-player numbers measure against.
    for g in out_games:
        for side in ("home", "away"):
            scored = [
                h for h in g[side]["hitters"]
                if ((h.get("projection") or {}).get("fpts")
                    or (h.get("projection") or {}).get("inhouse_fpts"))
            ]
            top5 = sorted(
                scored,
                key=lambda h: -(h["projection"].get("fpts") or h["projection"]["inhouse_fpts"]),
            )[:5]
            if len(top5) < 5:
                continue
            pools = await asyncio.gather(
                *(
                    variance.player_outcome_pool(h["id"], h["position"], season)
                    for h in top5
                )
            )
            stack_projections = [
                h["projection"].get("fpts") or h["projection"]["inhouse_fpts"] for h in top5
            ]
            # RECENTERED on today's projections, unlike the per-player
            # boom/bust above -- and the difference is deliberate,
            # because the two answer different questions.
            #
            # A single player's boom% asks "how often has HIS OWN real
            # history cleared what he's being asked for today", so it
            # must read his raw pool; that gap IS the signal.
            #
            # A stack's boom% asks the conditional question "GIVEN this
            # projection, how likely is this offense to blow past it" --
            # so the simulated outcomes have to be centered on the same
            # projection the threshold is derived from, or the answer is
            # just the level gap compounded five times over. Measured on
            # a real slate, that is exactly what it had become: boom%
            # tracked the pool-vs-projection gap almost perfectly and
            # nothing else (ATL, whose top-5 pools sit 33% under their
            # projections, read 1.5% boom / 32.3% bust, versus DET at
            # +3% reading 19.6% / 16.2%). Most of that gap is pool
            # dilution -- rest days and pinch-hit appearances are in a
            # hitter's pool, while today's projection knows he's
            # starting -- not real information about the offense.
            # Recentered, the column varies on what actually differs
            # between stacks: the SHAPE of those five bats' outcome
            # distributions (KC 20.4% vs MIL 15.2%).
            result = variance.stack_boom_bust(
                [
                    sorted(variance.recenter_pool(pool, projection))
                    for pool, projection in zip(pools, stack_projections)
                ],
                stack_projections,
                [(h.get("edge") or {}).get("composite") for h in top5],
            )
            if result:
                g[side]["stack_boom_pct"] = result["boom_pct"]
                g[side]["stack_bust_pct"] = result["bust_pct"]


# --------------------------------------------------------------------------
# One game
# --------------------------------------------------------------------------

def _game_pair(game: dict[str, Any]) -> frozenset[str]:
    teams = game.get("teams") or {}
    home = ((teams.get("home") or {}).get("team") or {}).get("abbreviation") or ""
    away = ((teams.get("away") or {}).get("team") or {}).get("abbreviation") or ""
    return frozenset((away, home))


def _game_start_ts(game: dict[str, Any]) -> float | None:
    """A real game's start as a comparable timestamp, or None if the
    schedule row has no usable date."""
    raw = game.get("gameDate")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _resolve_slate_game_pks(
    games: list[dict[str, Any]], detected_pairs: set[frozenset[str]]
) -> set[int]:
    """
    Which real game_pks the uploaded DK slate actually covers.

    A DK salary export identifies each game only by its matchup string
    ("BOS@NYY"), and the pool this app builds from RotoWire carries no
    game time at all -- so on a DOUBLEHEADER day two genuinely
    different MLB games collapse to one indistinguishable key. Matching
    on the pair alone therefore marked BOTH games of a doubleheader as
    in-slate, which is real and wrong: on 2026-08-29 it flagged 14
    games for a 12-game slate, adding a second BOS@NYY and a second
    AZ@SF the contest generator then treated as live.

    Every pair that matches exactly one real game is unambiguous, and
    those games define the slate's own real time window. A doubleheader
    pair is then resolved to whichever of its games sits CLOSEST to that
    window -- zero distance for anything inside it -- which on that date
    correctly keeps BOS@NYY at 17:05 and drops the 23:15 nightcap.

    Closest-to-window rather than strictly-inside-it, because a real
    slate's doubleheader half often starts just outside the range its
    other games span: on a night slate anchored at 22:10-23:10, a 22:05
    first game is five minutes early but obviously the intended one,
    while a strict window test would reject it and fall back to a 16:05
    afternoon game six hours away.

    Known limitation, stated rather than hidden: if DK ever puts BOTH
    halves of a doubleheader in one slate, this keeps only one, because
    the source data genuinely cannot express the difference. That's the
    safer direction to be wrong in -- an extra phantom game silently
    inflates every field-size and ownership calculation downstream,
    whereas a missing one is visible in the games checklist and can be
    ticked back on by hand.
    """
    by_pair: dict[frozenset[str], list[dict[str, Any]]] = {}
    for g in games:
        pair = _game_pair(g)
        if pair in detected_pairs:
            by_pair.setdefault(pair, []).append(g)

    resolved: set[int] = set()
    ambiguous: list[list[dict[str, Any]]] = []
    anchor_times: list[float] = []
    for candidates in by_pair.values():
        if len(candidates) == 1:
            resolved.add(candidates[0].get("gamePk"))
            when = _game_start_ts(candidates[0])
            if when is not None:
                anchor_times.append(when)
        else:
            ambiguous.append(candidates)

    for candidates in ambiguous:
        timed = [(g, _game_start_ts(g)) for g in candidates]
        timed = [(g, t) for g, t in timed if t is not None]
        if not timed:
            resolved.add(candidates[0].get("gamePk"))
            continue
        if anchor_times:
            earliest, latest = min(anchor_times), max(anchor_times)

            def _distance(t: float) -> float:
                if t < earliest:
                    return earliest - t
                if t > latest:
                    return t - latest
                return 0.0

            pick = min(timed, key=lambda gt: (_distance(gt[1]), gt[1]))[0]
        else:
            pick = min(timed, key=lambda gt: gt[1])[0]
        resolved.add(pick.get("gamePk"))

    return resolved


async def _build_game(
    game: dict[str, Any],
    season: int,
    data: dict[str, Any],
    baselines: dict[str, Any],
    lines: list[dict[str, Any]],
    include_hitters: bool,
    salary_lookup: dict[tuple[str, str], dict[str, Any]],
    projection_lookup: dict[tuple[str, str], dict[str, Any]],
    day: str,
    in_slate_pks: set[int] | None,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    game_pk = game.get("gamePk")
    teams = game.get("teams") or {}
    home_t = (teams.get("home") or {}).get("team") or {}
    away_t = (teams.get("away") or {}).get("team") or {}
    venue = game.get("venue") or {}

    home_abbrev = home_t.get("abbreviation") or ""
    away_abbrev = away_t.get("abbreviation") or ""
    park = get_park(home_abbrev, venue.get("name"))
    game_time = game.get("gameDate") or ""

    # Whether the uploaded DK salary CSV's slate covers this specific
    # game -- None when no CSV is loaded yet (nothing to detect
    # against). Resolved per game_pk rather than per matchup, so a
    # doubleheader doesn't put both of its games in the slate (see
    # _resolve_slate_game_pks).
    in_slate = game_pk in in_slate_pks if in_slate_pks is not None else None

    # --- Weather ---
    roof_closed = park["roof"] == "dome"
    wx = None
    if not roof_closed and park["lat"]:
        wx = await weather.get_game_weather(park["lat"], park["lon"], game_time)

    temp_fx = weather.temperature_effect((wx or {}).get("temp_f")) if wx else None
    wind_fx = (
        weather.wind_effect(
            (wx or {}).get("wind_dir_deg"),
            (wx or {}).get("wind_mph"),
            park.get("orientation_deg"),
        )
        if wx
        else None
    )
    # A retractable roof is usually shut in extreme heat or rain.
    if park["roof"] == "retractable" and wx:
        if (wx.get("precip_chance_pct") or 0) >= 50 or (wx.get("temp_f") or 0) >= 95:
            roof_closed = True

    # --- Betting line ---
    # `line` (The Odds API) is kept ONLY for its event_id, needed below to
    # fetch player props (a per-event Odds API call with no FantasyLabs
    # equivalent). The actual score/total/spread/moneyline/implied-runs
    # numbers shown throughout the app now come from FantasyLabs instead
    # (`betting`, right below) -- free, no credit cost, and it also
    # carries the opening line `vegas` exposes separately. The Odds API's
    # own line values are no longer read anywhere.
    line = _match_odds(lines, home_t.get("name"), away_t.get("name"))

    # --- FantasyLabs open/current line (spread, moneyline, total, implied
    # runs) ---
    vegas = _match_odds(
        data.get("fantasylabs_vegas") or [], home_t.get("name"), away_t.get("name")
    )
    betting = (
        {
            "total": vegas.get("total_current"),
            "home_moneyline": vegas.get("home_moneyline_current"),
            "away_moneyline": vegas.get("away_moneyline_current"),
            "home_spread": vegas.get("home_spread_current"),
            "away_spread": vegas.get("away_spread_current"),
            "home_implied_runs": vegas.get("home_implied_runs_current"),
            "away_implied_runs": vegas.get("away_implied_runs_current"),
            "book": "FantasyLabs (consensus)",
        }
        if vegas
        else None
    )

    # --- Today's umpire (RotoWire) -- absent until RotoWire itself has
    # a posted assignment, same "not yet known" convention as betting.
    umpire = (data.get("umpires") or {}).get(f"{away_t.get('abbreviation')}@{home_abbrev}")

    # --- Probable pitchers ---
    home_pp = (teams.get("home") or {}).get("probablePitcher") or {}
    away_pp = (teams.get("away") or {}).get("probablePitcher") or {}
    pitcher_ids = [p.get("id") for p in (home_pp, away_pp) if p.get("id")]
    pitcher_bios = await mlb.get_people(pitcher_ids) if pitcher_ids else {}

    home_pitcher = _pitcher_card(home_pp, pitcher_bios, data)
    away_pitcher = _pitcher_card(away_pp, pitcher_bios, data)

    result: dict[str, Any] = {
        "game_pk": game_pk,
        "game_time_utc": game_time,
        "status": ((game.get("status") or {}).get("detailedState")),
        "in_slate": in_slate,
        "venue": {
            "name": venue.get("name") or park["name"],
            "roof": park["roof"],
            "roof_closed": roof_closed,
            "elevation_ft": park["elevation_ft"],
            "park_factors": {
                "runs": park["runs"],
                "hr": park["hr"],
                "hr_lhb": park["hr_lhb"],
                "hr_rhb": park["hr_rhb"],
            },
        },
        "weather": (
            {**wx, "temperature_effect": temp_fx, "wind_effect": wind_fx}
            if wx
            else {"note": "roof closed" if roof_closed else "forecast unavailable"}
        ),
        "betting": betting or {"note": "no line available"},
        "vegas": vegas or {"note": "no FantasyLabs line available"},
        "umpire": umpire or {"note": "not yet assigned"},
        "home": {
            "team_id": home_t.get("id"),
            "abbrev": home_abbrev,
            "name": home_t.get("name"),
            "implied_runs": (betting or {}).get("home_implied_runs"),
            "moneyline": (betting or {}).get("home_moneyline"),
            "vegas_implied_runs_open": (vegas or {}).get("home_implied_runs_open"),
            "vegas_implied_runs_current": (vegas or {}).get("home_implied_runs_current"),
            "vegas_moneyline_open": (vegas or {}).get("home_moneyline_open"),
            "vegas_moneyline_current": (vegas or {}).get("home_moneyline_current"),
            "vegas_spread_open": (vegas or {}).get("home_spread_open"),
            "vegas_spread_current": (vegas or {}).get("home_spread_current"),
            "probable_pitcher": home_pitcher,
        },
        "away": {
            "team_id": away_t.get("id"),
            "abbrev": away_t.get("abbreviation"),
            "name": away_t.get("name"),
            "implied_runs": (betting or {}).get("away_implied_runs"),
            "moneyline": (betting or {}).get("away_moneyline"),
            "vegas_implied_runs_open": (vegas or {}).get("away_implied_runs_open"),
            "vegas_implied_runs_current": (vegas or {}).get("away_implied_runs_current"),
            "vegas_moneyline_open": (vegas or {}).get("away_moneyline_open"),
            "vegas_moneyline_current": (vegas or {}).get("away_moneyline_current"),
            "vegas_spread_open": (vegas or {}).get("away_spread_open"),
            "vegas_spread_current": (vegas or {}).get("away_spread_current"),
            "probable_pitcher": away_pitcher,
        },
    }

    if not include_hitters:
        return result

    # --- Player props (optional, ODDS_FETCH_PROPS -- see clients/odds.py's
    # own credit-cost docs). Needs the matched line's event_id, so this
    # can't fetch anything for a game with no betting line available.
    # Deliberately fetched only in this branch, AFTER the include_hitters
    # early-return above -- nothing below this point that consumes props
    # (hitters' home_run/hit_probability, pitchers' strikeout_potential)
    # ever runs when include_hitters=False (a real, exercised lighter
    # slate-build mode -- see routers/mlb.py's GET /games), so fetching
    # them there would spend real credits for data nothing uses.
    props = (
        await odds.get_player_props(line["event_id"], day=day, force=force_refresh)
        if line and line.get("event_id")
        else {}
    )
    hr_props = _market_at_least_one_pct_by_name(props.get("batter_home_runs") or [])
    hits_props = _market_at_least_one_pct_by_name(props.get("batter_hits") or [])
    k_props = _market_line_by_name(props.get("pitcher_strikeouts") or [])

    # --- Hitters for both sides ---
    env = {
        "park": park,
        "roof_closed": roof_closed,
        "temp_fx": temp_fx,
        "wind_fx": wind_fx,
        "umpire": umpire,
    }
    lineups = await mlb.get_lineups(game_pk) if game_pk else {"home": [], "away": []}

    home_hitters, away_hitters, home_injuries, away_injuries = await asyncio.gather(
        _team_hitters(
            home_t.get("id"), season, data, baselines, env,
            opposing_pitcher=away_pitcher, opponent_team_id=away_t.get("id"),
            is_home=True,
            implied_runs=(betting or {}).get("home_implied_runs"),
            confirmed=lineups.get("home") or [],
            team_abbrev=home_abbrev, salary_lookup=salary_lookup,
            projection_lookup=projection_lookup,
            own_pitcher_id=home_pp.get("id"),
            hr_props=hr_props, hits_props=hits_props,
        ),
        _team_hitters(
            away_t.get("id"), season, data, baselines, env,
            opposing_pitcher=home_pitcher, opponent_team_id=home_t.get("id"),
            is_home=False,
            implied_runs=(betting or {}).get("away_implied_runs"),
            confirmed=lineups.get("away") or [],
            team_abbrev=away_t.get("abbreviation") or "", salary_lookup=salary_lookup,
            projection_lookup=projection_lookup,
            own_pitcher_id=away_pp.get("id"),
            hr_props=hr_props, hits_props=hits_props,
        ),
        mlb.get_team_injuries(home_t.get("id"), season),
        mlb.get_team_injuries(away_t.get("id"), season),
    )
    result["home"]["hitters"] = home_hitters
    result["away"]["hitters"] = away_hitters
    result["home"]["lineup_confirmed"] = bool(lineups.get("home"))
    result["away"]["lineup_confirmed"] = bool(lineups.get("away"))
    result["home"]["injuries"] = home_injuries
    result["away"]["injuries"] = away_injuries

    # Only spend the extra roster-lookup cost when the real probable
    # pitcher is actually missing -- the common case has one already.
    result["home"]["projected_probable_pitcher"] = (
        await _projected_starter(home_t.get("id"), home_abbrev, season, projection_lookup)
        if home_pitcher is None else None
    )
    result["away"]["projected_probable_pitcher"] = (
        await _projected_starter(away_t.get("id"), away_t.get("abbreviation") or "", season, projection_lookup)
        if away_pitcher is None else None
    )

    # Late scratches the lineup watcher has caught for this game today --
    # see services/lineup_watch.py. Purely additive: this cache key is
    # only ever populated by the background poller, never by a request.
    day_scratches = cache.get(f"scratches:{day}") or []
    game_scratches = [s for s in day_scratches if s.get("game_pk") == game_pk]
    result["home"]["scratches"] = [s for s in game_scratches if s.get("team") == home_abbrev]
    result["away"]["scratches"] = [
        s for s in game_scratches if s.get("team") == (away_t.get("abbreviation") or "")
    ]

    # Team-level stack score = average of the top 5 hitters' scores.
    result["home"]["stack_score"] = _stack_score(home_hitters)
    result["away"]["stack_score"] = _stack_score(away_hitters)

    # Pitcher edge = the mirror image of the hitter model: the home
    # pitcher faces the AWAY lineup and is trying to hold down the AWAY
    # team's implied total, and vice versa.
    home_edge = _pitcher_edge(
        home_pitcher, away_hitters, result["away"]["implied_runs"], env, baselines,
        data["savant_pitch"],
        data.get("pitcher_skill"),
        market_k_line=k_props.get(salaries.normalize_name((home_pitcher or {}).get("name") or "")),
    )
    away_edge = _pitcher_edge(
        away_pitcher, home_hitters, result["home"]["implied_runs"], env, baselines,
        data["savant_pitch"],
        data.get("pitcher_skill"),
        market_k_line=k_props.get(salaries.normalize_name((away_pitcher or {}).get("name") or "")),
    )
    # The skill profile rides along on the card, not just inside the edge
    # components -- services/atbat_sim.py regresses a starter's simulated
    # HR and K rates onto it, and reads it from here.
    for side, pitcher in (("home", home_pitcher), ("away", away_pitcher)):
        if result[side].get("probable_pitcher") and pitcher:
            result[side]["probable_pitcher"]["skill"] = (
                (data.get("pitcher_skill") or {}).get(pitcher.get("id"))
            )

    if home_edge:
        result["home"]["probable_pitcher"]["edge"] = home_edge
        result["home"]["probable_pitcher"]["salary"] = _salary_info(
            salary_lookup, home_pitcher.get("name"), home_abbrev, home_edge["score"]
        )
        result["home"]["probable_pitcher"]["projection"] = _projection_info(
            projection_lookup, home_pitcher.get("name"), home_abbrev
        )
    if away_edge:
        result["away"]["probable_pitcher"]["edge"] = away_edge
        result["away"]["probable_pitcher"]["salary"] = _salary_info(
            salary_lookup, away_pitcher.get("name"), away_t.get("abbreviation") or "", away_edge["score"]
        )
        result["away"]["probable_pitcher"]["projection"] = _projection_info(
            projection_lookup, away_pitcher.get("name"), away_t.get("abbreviation") or ""
        )

    return result


def _projection_info(
    lookup: dict[tuple[str, str], dict[str, Any]],
    name: str | None,
    team_abbrev: str,
) -> dict[str, Any] | None:
    """RotoWire's FPTS/ownership projection for one player, or None if no
    projections file is loaded for this date or nothing matched. Purely
    informational -- see services/projections.py for why this never
    touches the edge score."""
    if not lookup or not name:
        return None
    row = projections.match(lookup, name, team_abbrev, fuzzy=True)
    if not row:
        return None
    return {"fpts": row["fpts"], "ownership_pct": row["ownership_pct"], "lineup_spot": row.get("lineup_spot")}


def _salary_info(
    lookup: dict[tuple[str, str], dict[str, Any]],
    name: str | None,
    team_abbrev: str,
    edge_score: float | None,
) -> dict[str, Any] | None:
    """Salary + value for one player, or None if no salary file is loaded
    for this date or the name/team didn't match anything in it."""
    if not lookup or not name:
        return None
    row = salaries.match(lookup, name, team_abbrev, fuzzy=True)
    if not row:
        return None
    return {
        "salary": row["salary"],
        "avg_points": row["avg_points"],
        "value": salaries.value_score(edge_score, row["salary"]),
        # DK's own position string (e.g. "1B/3B") -- distinct from the
        # single MLB-primary-position on the player dict itself, and the
        # one the lineup optimizer needs for roster-slot eligibility.
        "position": row["position"],
        # DK's own numeric player id -- previously dropped here (MLB
        # Stats API's own id is this app's authoritative identity
        # everywhere else), but needed verbatim to fill a real DK
        # contest-entry template CSV back in (services/dk_entry_manager.py).
        # Empty string when no DK salary file is loaded (e.g. a
        # RotoWire-only slate) -- see salaries.from_rotowire_rows().
        "dk_id": row.get("dk_id", ""),
    }


def _stack_score(hitters: list[dict[str, Any]]) -> float | None:
    """
    How good is it to stack this offence?

    Stacking means rostering several hitters from the same team so that a
    big inning pays you multiple times. We average the top five matchup
    scores rather than the whole roster, because a stack only needs five
    good bats.
    """
    scores = sorted((h["edge"]["score"] for h in hitters), reverse=True)[:5]
    if not scores:
        return None
    return round(sum(scores) / len(scores), 1)


def _pitcher_edge(
    pitcher_card: dict[str, Any] | None,
    facing_hitters: list[dict[str, Any]],
    implied_runs_against: float | None,
    env: dict[str, Any],
    baselines: dict[str, Any],
    savant_pitch: dict[int, dict[str, Any]],
    pitcher_skills: dict[int, dict[str, Any]] | None = None,
    *,
    market_k_line: float | None = None,
) -> dict[str, Any] | None:
    """
    A pitcher's matchup score -- the same component/weight machinery as
    the hitter model, run in reverse: the batting environment (park,
    weather, opponent's implied total) is inverted around 1.00, and the
    opposing lineup's own matchup strength against this exact pitcher
    (already computed by `_team_hitters`) is reused rather than refetched.

    `market_k_line`, when a real pitcher_strikeouts prop exists for this
    pitcher tonight, blends the market's own strikeout-total line into
    `strikeout_potential_component()` -- see that function's own
    docstring for how the blend is weighted.
    """
    if not pitcher_card:
        return None

    park = env["park"]
    skill = (pitcher_skills or {}).get(pitcher_card.get("id")) or {}

    runs_against = scoring.team_total_component(implied_runs_against)
    runs_against = {**runs_against, "value": scoring.invert_for_pitcher(runs_against["value"])}

    park_comp = scoring.park_component(park["hr"], park["runs"])
    park_comp = {**park_comp, "value": scoring.invert_for_pitcher(park_comp["value"])}

    weather_comp = scoring.weather_component(env["temp_fx"], env["wind_fx"], env["roof_closed"])
    weather_comp = {**weather_comp, "value": scoring.invert_for_pitcher(weather_comp["value"])}

    umpire_comp = scoring.umpire_component(
        env.get("umpire"), baselines.get("umpire_avg_rpg"), baselines.get("umpire_avg_kpg")
    )
    umpire_comp = {**umpire_comp, "value": scoring.invert_for_pitcher(umpire_comp["value"])}

    components = {
        "opp_lineup": scoring.opp_lineup_component(facing_hitters),
        "strikeout_potential": scoring.strikeout_potential_component(
            pitcher_card.get("season"),
            baselines.get("pitcher_k9"),
            facing_hitters,
            baselines.get("hitter_k_pct"),
            market_k_line=market_k_line,
            csw=(skill or {}).get("csw_pct"),
            league_avg_csw=baselines.get("pitcher_csw"),
        ),
        "team_runs_against": runs_against,
        "contact_quality_allowed": scoring.contact_quality_allowed_component(
            savant_pitch.get(pitcher_card.get("id")),
            baselines.get("pitcher_barrel"),
            baselines.get("pitcher_hard_hit"),
            baselines.get("pitcher_xwoba"),
        ),
        "own_quality": scoring.own_quality_component(
            pitcher_card.get("season"),
            baselines.get("pitcher_era"),
            skill=skill,
            league_avg_skill=baselines.get("pitcher_siera"),
        ),
        "park": park_comp,
        "weather": weather_comp,
        "umpire": umpire_comp,
    }
    edge = scoring.combine(components, scoring.PITCHER_WEIGHTS)
    return {**edge, "components": components}


def _pitcher_card(
    pp: dict[str, Any], bios: dict[int, dict[str, Any]], data: dict[str, Any]
) -> dict[str, Any] | None:
    pid = pp.get("id")
    if not pid:
        return None
    bio = bios.get(pid, {})
    return {
        "id": pid,
        "name": pp.get("fullName") or bio.get("name"),
        "throws": bio.get("throws"),
        "season": data["pit_season"].get(pid),
        "vs_lhb": data["pit_vl"].get(pid),
        "vs_rhb": data["pit_vr"].get(pid),
    }


async def _projected_starter(
    team_id: int | None,
    team_abbrev: str,
    season: int,
    projection_lookup: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any] | None:
    """
    A fallback for when the real MLB API has no probable pitcher listed
    for this team yet (rare, but real -- an undecided rotation slot, a
    bullpen game, or simply too early in the day for MLB to have
    announced one): RotoWire's OWN projected starter, from an uploaded
    projections file, as a stand-in -- same "confirmed vs projected,
    kept clearly separate, never silently blended" pattern
    projected_batting_order already uses for hitters. Only ever called
    when the real probable_pitcher is missing (see _build_game()).

    A projections file identifies players by name only, with no MLB id
    -- picks the highest-FPTS pitcher-position row for this team (a
    real starter reliably outprojects a low-usage reliever also sharing
    the "P" position tag, a simpler and more robust signal than trying
    to parse "SP" vs "RP" labeling that isn't consistent across every
    real export), then resolves a real MLB id for that name by matching
    against the team's roster -- the same name/team fuzzy matching
    salaries.py and projections.py already use elsewhere, just pointed
    at a roster lookup instead of a projections lookup.

    Matches against the active roster, the 40-MAN roster, and the
    injured list -- all three, because MLB doesn't move a player onto
    the active roster until his transaction actually posts, which
    routinely lands close to first pitch.

    Each of the three catches a real case the others miss. The injured
    list covers a starter coming off the 60-day IL that day (confirmed
    with the user: RotoWire listing an injured pitcher as a team's top
    projected starter typically means he's being activated to make the
    start, not that RotoWire's data is stale) -- and he specifically
    won't be on the 40-man, since a 60-day IL stint removes him from
    it. The 40-man covers the opposite case, a minor-league call-up
    getting a spot start: real and measured -- on 2026-08-29 RotoWire
    had Matt Wilkinson as SF's starter, and he sat on the Giants'
    49-man 40-man roster while their active roster held 26 without
    him, so the at-bat engine refused the whole slate over one
    unresolvable pitcher.

    Deliberately NOT the "fullSeason" roster, which would also match
    anyone who appeared for the club at any point this year -- that
    widens the fuzzy-name surface for no gain (SF's own fullSeason list
    carries both a "Matt Wilkinson" and a "Wilkin Ramos"). The 40-man
    is exactly the set of players who can legally be called up and
    start today.

    Returns None with no projections file loaded, no pitcher-position
    row for this team, or no match on any of the three rosters.
    """
    if not team_id or not projection_lookup:
        return None
    team_norm = projections.normalize_team(team_abbrev)
    candidates = [
        row for (t, _name), row in projection_lookup.items()
        if t == team_norm and (row.get("position") or "").upper().split("/")[0] in PITCHER_POSITIONS
    ]
    if not candidates:
        return None
    best = max(candidates, key=lambda row: row.get("fpts") or 0)

    active_ids, forty_man_ids, injuries = await asyncio.gather(
        mlb.get_active_roster(team_id, season),
        mlb.get_40man_roster(team_id, season),
        mlb.get_team_injuries(team_id, season),
    )
    roster_ids = list(
        {*active_ids, *forty_man_ids, *(inj["id"] for inj in injuries if inj.get("id"))}
    )
    if not roster_ids:
        return None
    bios = await mlb.get_people(roster_ids)
    roster_lookup = projections.build_lookup(
        [
            {"team": team_abbrev, "normalized_name": projections.normalize_name(bio["name"]), "id": pid}
            for pid, bio in bios.items()
            if bio.get("name")
        ]
    )
    matched = projections.match(roster_lookup, best["name"], team_abbrev, fuzzy=True)
    if not matched:
        return None
    return {"id": matched["id"], "name": best["name"]}


async def _projected_lineup_ids(
    team_id: int,
    team_abbrev: str,
    season: int,
    projection_lookup: dict[tuple[str, str], dict[str, Any]],
    known_ids: list[int],
) -> list[int]:
    """
    MLB ids for anyone RotoWire puts in TODAY'S projected batting order
    who isn't on the active roster -- resolved off the 40-man roster and
    the injured list, exactly as _projected_starter() already does for a
    projected STARTING PITCHER.

    Same real problem, same user-confirmed reading of it: a roster move
    posts to MLB's feed when the transaction clears, which routinely
    lands close to first pitch, while RotoWire and DraftKings already
    have the player in the lineup and priced. RotoWire listing an
    injured player in today's batting order typically means he's being
    activated to play, not that RotoWire is stale -- and a 60-day IL
    stint is precisely the case where MLB's own "active" roster says no
    while every other source says yes.

    Measured on 2026-08-30: Boston's projected 1st and 6th hitters
    (Roman Anthony, Trevor Story -- both D60 on MLB's feed, both priced
    by DK, both carrying 130+ real plate appearances this season) were
    dropped from the slate entirely, leaving BOS with 7 usable batting
    spots against the at-bat engine's minimum of 8. The whole slate was
    refused over it.

    Deliberately NOT a blanket widening to the 40-man. That would pull
    in every genuinely-injured player who happens to remain on it --
    138 extra hitters league-wide on that same date, including several
    stars nobody was projecting to play at all. Only players RotoWire
    actually names in today's order are recovered, which is exactly the
    set with real same-day evidence behind them.

    Returns only ids NOT already in `known_ids`; an empty list whenever
    every projected hitter already resolves off the active roster, which
    is the common case and costs nothing beyond the (cached) name match.
    """
    if not projection_lookup:
        return []
    team_norm = projections.normalize_team(team_abbrev)
    projected_names = [
        row["name"]
        for (t, _name), row in projection_lookup.items()
        if t == team_norm and row.get("lineup_spot") and row.get("name")
    ]
    if not projected_names:
        return []

    known_bios = await mlb.get_people(known_ids)
    known_lookup = projections.build_lookup(
        [
            {"team": team_abbrev, "normalized_name": projections.normalize_name(bio["name"]), "id": pid}
            for pid, bio in known_bios.items()
            if bio.get("name")
        ]
    )
    missing = [
        name for name in projected_names
        if not projections.match(known_lookup, name, team_abbrev, fuzzy=True)
    ]
    if not missing:
        return []

    forty_man_ids, injuries = await asyncio.gather(
        mlb.get_40man_roster(team_id, season),
        mlb.get_team_injuries(team_id, season),
    )
    candidate_ids = [
        pid
        for pid in {*forty_man_ids, *(inj["id"] for inj in injuries if inj.get("id"))}
        if pid not in set(known_ids)
    ]
    if not candidate_ids:
        return []
    candidate_bios = await mlb.get_people(candidate_ids)
    candidate_lookup = projections.build_lookup(
        [
            {"team": team_abbrev, "normalized_name": projections.normalize_name(bio["name"]), "id": pid}
            for pid, bio in candidate_bios.items()
            if bio.get("name")
        ]
    )
    recovered: list[int] = []
    for name in missing:
        matched = projections.match(candidate_lookup, name, team_abbrev, fuzzy=True)
        if matched and matched["id"] not in recovered:
            recovered.append(matched["id"])
    return recovered


# --------------------------------------------------------------------------
# One team's hitters
# --------------------------------------------------------------------------

async def _team_hitters(
    team_id: int | None,
    season: int,
    data: dict[str, Any],
    baselines: dict[str, Any],
    env: dict[str, Any],
    *,
    opposing_pitcher: dict[str, Any] | None,
    opponent_team_id: int | None,
    is_home: bool,
    implied_runs: float | None,
    confirmed: list[int],
    team_abbrev: str = "",
    salary_lookup: dict[tuple[str, str], dict[str, Any]] | None = None,
    projection_lookup: dict[tuple[str, str], dict[str, Any]] | None = None,
    own_pitcher_id: int | None = None,
    hr_props: dict[str, float] | None = None,
    hits_props: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    if not team_id:
        return []
    hr_props = hr_props or {}
    hits_props = hits_props or {}

    opp_bullpen = data["bullpen"].get(opponent_team_id)
    opp_bullpen_workload = data["bullpen_workload"].get(opponent_team_id)

    # Prefer the confirmed lineup; fall back to the active roster, plus
    # anyone RotoWire's own projected batting order names who isn't on
    # it yet (see _projected_lineup_ids -- an IL activation or call-up
    # whose transaction hasn't posted to MLB's feed).
    if confirmed:
        player_ids = confirmed
    else:
        player_ids = await mlb.get_active_roster(team_id, season)
        if projection_lookup:
            player_ids = player_ids + await _projected_lineup_ids(
                team_id, team_abbrev, season, projection_lookup, player_ids
            )

    bios = await mlb.get_people(player_ids)
    p_hand = (opposing_pitcher or {}).get("throws")  # 'L' or 'R'

    # Which league baseline applies depends on the pitcher's hand.
    hitter_baseline = (
        baselines["hitter_ops_vl"] if p_hand == "L" else baselines["hitter_ops_vr"]
    )
    hitter_splits = data["hit_vl"] if p_hand == "L" else data["hit_vr"]

    out: list[dict[str, Any]] = []
    for idx, pid in enumerate(player_ids):
        bio = bios.get(pid)
        if not bio:
            continue
        position = bio.get("position") or ""
        if position in PITCHER_POSITIONS:
            # A two-way player (Ohtani's official MLB position is "TWP"
            # year-round) is a real hitter on every day he isn't THIS
            # team's own starting pitcher -- a blanket position
            # exclusion would otherwise erase him from the Hitters tab
            # entirely, including his DH-only days.
            if position != "TWP" or pid == own_pitcher_id:
                continue

        season_stat = data["hit_season"].get(pid)
        # Skip players with almost no playing time -- they add noise.
        # Exempt anyone RotoWire actually names in TODAY'S projected
        # batting order: the filter exists to keep bench noise out of
        # the tables, and a named starter is by definition not noise.
        # Measured: on 2026-08-30 the Dodgers' projected 9th hitter sat
        # on 24 plate appearances, one short of the cutoff, so the
        # at-bat engine simulated an 8-man Los Angeles order -- silently,
        # since 8 still clears its readiness minimum. A player with NO
        # season line at all is still skipped; there's nothing to score
        # him with.
        projected_spot = (
            (_projection_info(projection_lookup, bio.get("name"), team_abbrev) or {}).get("lineup_spot")
            if projection_lookup
            else None
        )
        if not season_stat or ((season_stat.get("pa") or 0) < 25 and not projected_spot):
            continue

        bats = bio.get("bats") or "R"
        split_stat = hitter_splits.get(pid)

        # Pitcher's split against THIS batter's hand.
        pitcher_split = None
        if opposing_pitcher:
            pitcher_split = (
                opposing_pitcher.get("vs_lhb")
                if bats == "L"
                else opposing_pitcher.get("vs_rhb")
            )
            if bats == "S":
                # A switch hitter always bats opposite the pitcher.
                pitcher_split = (
                    opposing_pitcher.get("vs_rhb")
                    if p_hand == "L"
                    else opposing_pitcher.get("vs_lhb")
                )
        pitcher_baseline = (
            baselines["pitcher_ops_vl"] if bats == "L" else baselines["pitcher_ops_vr"]
        )

        park = env["park"]
        hr_factor = hr_factor_for_hand(park, bats)
        weather_hr_mult = (
            scoring.NEUTRAL
            if env["roof_closed"]
            else (env["temp_fx"] or {}).get("hr_multiplier", scoring.NEUTRAL)
            * (env["wind_fx"] or {}).get("hr_multiplier", scoring.NEUTRAL)
        )
        name_key = salaries.normalize_name(bio.get("name") or "")
        market_hr_pct = hr_props.get(name_key)
        market_hit_pct = hits_props.get(name_key)
        components = {
            "platoon": scoring.platoon_component(split_stat, hitter_baseline),
            "pitcher": scoring.pitcher_component(pitcher_split, pitcher_baseline),
            "team_total": scoring.team_total_component(implied_runs),
            "contact_quality": scoring.contact_quality_component(
                data["savant_hit"].get(pid),
                baselines["hitter_barrel"],
                baselines["hitter_hard_hit"],
                baselines["hitter_xwoba"],
            ),
            "stolen_base": scoring.stolen_base_component(
                season_stat, baselines["hitter_sb_per_pa"]
            ),
            "bullpen": scoring.bullpen_component(opp_bullpen, baselines["bullpen_era"]),
            "bullpen_workload": scoring.bullpen_workload_component(
                opp_bullpen_workload, baselines["bullpen_workload_outs"], mlb.BULLPEN_WORKLOAD_WINDOW_DAYS
            ),
            "park": scoring.park_component(hr_factor, park["runs"]),
            "weather": scoring.weather_component(
                env["temp_fx"], env["wind_fx"], env["roof_closed"]
            ),
            "home_run": scoring.home_run_component(
                season_stat,
                baselines["hitter_hr_per_pa"],
                hr_factor,
                weather_hr_mult,
                ((opposing_pitcher or {}).get("season") or {}).get("hr_per_9"),
                baselines["pitcher_hr_per_9"],
                market_hr_probability_pct=market_hr_pct,
            ),
            "hit_probability": scoring.hit_probability_component(market_hit_pct),
            "form": scoring.form_component(
                data["hit_recent"].get(pid), season_stat
            ),
            "home_road": scoring.home_road_component(
                (data["hit_home"] if is_home else data["hit_away"]).get(pid),
                season_stat,
                is_home,
            ),
            "umpire": scoring.umpire_component(
                env.get("umpire"), baselines.get("umpire_avg_rpg"), baselines.get("umpire_avg_kpg")
            ),
        }
        edge = scoring.combine(components)
        projection = _projection_info(projection_lookup, bio.get("name"), team_abbrev)

        out.append(
            {
                "id": pid,
                "name": bio.get("name"),
                "position": bio.get("position"),
                "bats": bats,
                "batting_order": idx + 1 if confirmed else None,
                # RotoWire's own PROJECTED batting spot (1-9, from its
                # uploaded LINEUP column), kept separate from the real
                # `batting_order` above -- a guess, not a fact, but
                # services/atbat_sim.py falls back on it to simulate a
                # slate before real lineups confirm. None with no
                # projections file loaded, no match, or RotoWire has
                # this player projected to the bench.
                "projected_batting_order": (projection or {}).get("lineup_spot"),
                "season": season_stat,
                "vs_hand": split_stat,
                "edge": {**edge, "components": components},
                "salary": _salary_info(salary_lookup, bio.get("name"), team_abbrev, edge["score"]),
                "projection": projection,
            }
        )

    out.sort(key=lambda h: h["edge"]["score"], reverse=True)
    return out
