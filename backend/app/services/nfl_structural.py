"""
The NFL structural simulator -- layer 2 (nfl_team_draws) and layer 3
(nfl_shares) joined into one per-player outcome matrix, so the contest
path can rank lineups against it exactly as it does against
nfl_variance's bootstrap pools.

WHAT IT DOES DIFFERENTLY FROM THE BOOTSTRAP ENGINE

nfl_variance resamples each player's own history and multiplies by a
shared team factor, with the correlation set by hand. This simulates
the game instead: a team's scoring, plays and pass lean are drawn from
the market's implied totals, then that volume is ALLOCATED among the
team's players. Correlation is a consequence rather than a parameter --
teammates' touchdowns come out negatively correlated because they are
dividing a fixed team total, a pass-catching back's targets rise in the
sims where his team is trailing, and a DST's score falls out of the
opponent's own draw in the same simulated game.

WHERE THE PRIORS COME FROM

Usage shares are historical (each player's own share of his team's real
targets and carries), scaled by how his projection compares with what
he has actually been producing, then renormalized. Both halves matter:
history knows the shape of a team's usage, and only the projection
knows that today's WR1 is starting because the usual one is out. A
player with no history at all falls back to a projection-implied share
of his position group.

Rate stats -- catch rate, yards per reception, yards per carry -- are
historical only. They are properties of a player, not of a week.

CALIBRATION STATUS

Layer 2 and layer 3 constants are fitted (scripts/fit_nfl_team_draws.py
and scripts/fit_nfl_shares.py). Two things are still open, both flagged
in their own modules: `td_coupling`, which needs a joint volume-share
against TD-count target rather than a marginal one, and step (c) of the
calibration order -- simulated per-player fantasy-point SD against
history. Until those are done this is an opt-in engine, not the default.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

import numpy as np

from app.clients import nfl
from app.services import nfl_dk_points, nfl_shares, nfl_team_draws

log = logging.getLogger(__name__)

# Fitted on 2022-2025: d(log usage share) / d(script), with script the
# standardized pass lean layer 2 produces.
#
#   target share    RB +0.105    WR +0.008    TE -0.042
#   carry share     RB -0.001    QB +0.043    WR +0.001
#
# The only real effect is the pass-catching back. Everything else is
# indistinguishable from zero, and the carry betas especially so.
#
# THAT IS NOT A NULL RESULT, IT IS THE LAYER BOUNDARY. The design brief
# suggests carry betas around -0.18 for an early-down back. Applying
# that here would DOUBLE-COUNT: layer 2 already cuts a trailing team's
# rush attempts (its pass share moves -0.0033 per point of margin), so
# the back's carries have already fallen before layer 3 sees them.
# Layer 3's beta redistributes WITHIN a category, and within the run
# game a trailing team's carries are shared out much as they always are.
SCRIPT_BETA_TARGETS: dict[str, float] = {"RB": 0.105, "WR": 0.008, "TE": -0.042, "QB": 0.0}
SCRIPT_BETA_CARRIES: dict[str, float] = {"RB": 0.0, "QB": 0.043, "WR": 0.0, "TE": 0.0}

# A player needs some real usage before his share means anything.
MIN_TEAM_VOLUME = 8.0
# How far a projection may move a historical share. A projection that
# disagrees with history by more than this is usually a role change the
# history cannot see, but it is also where a bad projection would do the
# most damage, so it is bounded.
PROJECTION_SCALE_MIN, PROJECTION_SCALE_MAX = 0.35, 3.0


def _usage_from_logs(rows: list[dict[str, Any]]) -> dict[str, float]:
    """A player's season totals, in the quantities the priors need."""
    keys = ("targets", "receptions", "receiving_yards", "receiving_tds",
            "carries", "rushing_yards", "rushing_tds")
    out = {k: 0.0 for k in keys}
    for row in rows:
        for k in keys:
            out[k] += row.get(k) or 0.0
    out["games"] = float(len(rows))
    out["dk_points"] = sum(nfl_dk_points.game_points(r) for r in rows)
    return out


async def _season_usage(season: int) -> tuple[dict[str, dict[str, float]], int]:
    """
    Per-player season usage, from the most recent season that has any.

    Week 1 of a new season has no games yet, so this falls back a year
    rather than returning nothing -- last year's usage is a far better
    prior than none.
    """
    for candidate in (season, season - 1):
        try:
            grouped = await nfl.get_grouped_season_stats(candidate)
        except Exception as exc:                       # noqa: BLE001 -- fall back, don't fail
            log.warning("nfl_structural: no stats for %s (%s)", candidate, exc)
            continue
        if not grouped:
            continue
        usage = {pid: _usage_from_logs(rows) for pid, rows in grouped.items() if rows}
        if any(u["targets"] + u["carries"] > 0 for u in usage.values()):
            return usage, candidate
    return {}, season


def _normalize(values: np.ndarray) -> np.ndarray:
    total = float(values.sum())
    if total <= 0:
        return np.full(len(values), 1.0 / max(len(values), 1))
    return values / total


def _projection_scaled(
    historical: np.ndarray, projected: np.ndarray, per_game: np.ndarray
) -> np.ndarray:
    """
    Move each historical share toward what today's projection implies.

    A player projected at twice his season-long output is being given a
    bigger role this week -- an injury ahead of him, a matchup, a return
    from one of his own. History cannot see that; the projection is the
    only thing on the slate that can. Equally, history is the only thing
    that knows the SHAPE of a team's usage, so this scales rather than
    replaces, and the scale is bounded.
    """
    scale = np.ones(len(historical))
    usable = (per_game > 0.5) & (projected > 0)
    scale[usable] = np.clip(
        projected[usable] / per_game[usable], PROJECTION_SCALE_MIN, PROJECTION_SCALE_MAX
    )
    # A player with no usable history leans entirely on his projection,
    # relative to the rest of his group.
    unseen = ~usable & (projected > 0)
    if unseen.any() and projected[usable].sum() > 0:
        historical = historical.copy()
        typical = float(np.median(historical[usable])) if usable.any() else 0.0
        historical[unseen] = max(typical, 0.01)
    return _normalize(historical * scale)


def build_team_priors(
    players: list[dict[str, Any]],
    usage: dict[str, dict[str, float]],
) -> tuple[nfl_shares.PassPriors | None, nfl_shares.RushPriors | None, list[dict[str, Any]]]:
    """
    Build one team's receiving and rushing priors from its slate players.

    Returns (PassPriors, RushPriors, players) with the player list in
    the same order as the prior arrays, so a caller can map columns back
    to ids. Either prior is None when the team has nobody who does that.
    """
    if not players:
        return None, None, []

    stats = [usage.get(p.get("nflverse_id") or "", {}) for p in players]
    positions = [p.get("position") or "WR" for p in players]
    projected = np.array([
        float(((p.get("projection") or {}).get("fpts")) or p.get("projected_fpts") or 0.0)
        for p in players
    ])
    per_game = np.array([
        (s.get("dk_points", 0.0) / s["games"]) if s.get("games") else 0.0 for s in stats
    ])

    # ---- receiving -------------------------------------------------
    targets = np.array([s.get("targets", 0.0) for s in stats])
    receivers = [i for i, pos in enumerate(positions) if pos in ("WR", "TE", "RB")]
    passing = None
    if receivers and (targets[receivers].sum() > 0 or projected[receivers].sum() > 0):
        idx = np.array(receivers)
        # A team whose players have no usable history at all -- an
        # expansion of rookies, or simply nobody resolved to an
        # nflverse id -- still has to produce outcomes, or every lineup
        # containing one of them is refused. Projections are the
        # fallback basis: worse than real usage at describing the shape
        # of a team, but far better than nothing.
        base = (
            _normalize(targets[idx]) if targets[idx].sum() >= MIN_TEAM_VOLUME
            else _normalize(projected[idx])
        )
        share = _projection_scaled(base, projected[idx], per_game[idx])
        catch = np.array([
            (s["receptions"] / s["targets"]) if s.get("targets", 0) >= 5
            else nfl_shares.LEAGUE_CATCH_RATE.get(positions[i], 0.65)
            for i, s in zip(idx, [stats[j] for j in idx])
        ])
        ypr = np.array([
            (s["receiving_yards"] / s["receptions"]) if s.get("receptions", 0) >= 5 else 10.5
            for s in [stats[j] for j in idx]
        ])
        rec_tds = np.array([stats[j].get("receiving_tds", 0.0) for j in idx])
        td_share = _normalize(rec_tds) if rec_tds.sum() > 0 else share.copy()
        passing = nfl_shares.PassPriors(
            names=[players[j]["name"] for j in idx],
            positions=[positions[j] for j in idx],
            target_share=share,
            td_share=td_share,
            yards_per_rec=np.clip(ypr, 4.0, 20.0),
            script_beta=np.array([SCRIPT_BETA_TARGETS.get(positions[j], 0.0) for j in idx]),
            catch_rate=np.clip(catch, 0.35, 0.95),
        )
        passing_index = list(idx)
    else:
        passing_index = []

    # ---- rushing ---------------------------------------------------
    carries = np.array([s.get("carries", 0.0) for s in stats])
    runners = [i for i, pos in enumerate(positions) if pos in ("RB", "QB", "WR")]
    rushing = None
    rushing_index: list[int] = []
    if runners and (carries[runners].sum() > 0 or projected[runners].sum() > 0):
        idx = np.array(runners)
        base = (
            _normalize(carries[idx]) if carries[idx].sum() >= MIN_TEAM_VOLUME
            else _normalize(projected[idx])
        )
        share = _projection_scaled(base, projected[idx], per_game[idx])
        ypc = np.array([
            (s["rushing_yards"] / s["carries"]) if s.get("carries", 0) >= 5 else 4.2
            for s in [stats[j] for j in idx]
        ])
        rush_tds = np.array([stats[j].get("rushing_tds", 0.0) for j in idx])
        rushing = nfl_shares.RushPriors(
            names=[players[j]["name"] for j in idx],
            positions=[positions[j] for j in idx],
            rush_share=share,
            td_share=_normalize(rush_tds) if rush_tds.sum() > 0 else share.copy(),
            yards_per_carry=np.clip(ypc, 2.5, 7.0),
            script_beta=np.array([SCRIPT_BETA_CARRIES.get(positions[j], 0.0) for j in idx]),
        )
        rushing_index = list(idx)

    return passing, rushing, [
        {"player": players[i], "pass_col": passing_index.index(i) if i in passing_index else None,
         "rush_col": rushing_index.index(i) if i in rushing_index else None}
        for i in range(len(players))
    ]


async def simulate_slate_trials(
    slate: dict[str, Any],
    season: int,
    *,
    num_trials: int,
    seed: int | None = None,
) -> dict[Any, np.ndarray]:
    """
    Per-player DK-point arrays for every player on the slate, shape
    (num_trials,), keyed by the same id the optimizer pool uses.

    Mirrors atbat_sim.simulate_slate_trials()'s contract on the MLB side,
    so the contest path treats the two engines identically.

    SEED ONCE PER SLATE. Every game is drawn from a seed derived from
    this one, so every candidate lineup is scored against the same
    simulated week -- common random numbers, which is what stops lineup
    rankings reshuffling between runs.
    """
    usage, usage_season = await _season_usage(season)
    if usage_season != season:
        log.info("nfl_structural: using %s usage for a %s slate", usage_season, season)

    out: dict[Any, np.ndarray] = {}
    rng = np.random.default_rng(seed)

    for game_index, game in enumerate(slate.get("games", [])):
        home_implied = (game.get("home") or {}).get("implied_total")
        away_implied = (game.get("away") or {}).get("implied_total")
        if home_implied is None or away_implied is None:
            # No line for this game -- it cannot be anchored to a market
            # that is not there, and guessing one would be worse than
            # leaving these players to the bootstrap engine.
            continue

        game_seed = None if seed is None else int(seed) + 1000 * (game_index + 1)
        draws = nfl_team_draws.simulate_game(
            float(home_implied), float(away_implied), num_sims=num_trials, seed=game_seed
        )

        for side_key, opposite in (("home", "away"), ("away", "home")):
            side = game.get(side_key) or {}
            players = [p for p in (side.get("players") or []) if p.get("dk_id")]
            team = draws[side_key]
            opponent = draws[opposite]

            passing, rushing, mapping = build_team_priors(players, usage)
            pass_out = (
                nfl_shares.allocate_passing(rng, team, passing) if passing is not None else None
            )
            rush_out = (
                nfl_shares.allocate_rushing(rng, team, rushing) if rushing is not None else None
            )

            for entry in mapping:
                player = entry["player"]
                position = player.get("position") or ""
                if position == "DST":
                    continue
                kwargs: dict[str, Any] = {}
                if pass_out is not None and entry["pass_col"] is not None:
                    col = entry["pass_col"]
                    kwargs.update(
                        receptions=pass_out["receptions"][:, col],
                        receiving_yards=pass_out["rec_yards"][:, col],
                        receiving_tds=pass_out["rec_tds"][:, col],
                    )
                if rush_out is not None and entry["rush_col"] is not None:
                    col = entry["rush_col"]
                    kwargs.update(
                        rushing_yards=rush_out["rush_yards"][:, col],
                        rushing_tds=rush_out["rush_tds"][:, col],
                    )
                if position == "QB":
                    # The quarterback throws every one of his team's
                    # attempts, so his passing line is the sum of what
                    # his receivers caught -- not an independent draw.
                    if pass_out is not None:
                        kwargs.update(
                            passing_yards=pass_out["rec_yards"].sum(axis=1),
                            passing_tds=team.pass_tds,
                            interceptions=team.ints,
                        )
                if not kwargs:
                    continue
                out[player["dk_id"]] = nfl_dk_points.game_points_vectorized(**kwargs)

            # DST scores off the OTHER team's draw, in this same game.
            for p in side.get("players") or []:
                if (p.get("position") or "") == "DST" and p.get("dk_id"):
                    out[p["dk_id"]] = nfl_dk_points.dst_points_vectorized(
                        points_allowed=opponent.points,
                        sacks=opponent.sacks,
                        interceptions=opponent.ints,
                        fumble_recoveries=opponent.fumbles_lost,
                    )
    return out
