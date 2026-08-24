"""
Empirical QB / pass-catcher correlation, computed directly from a real
season of DK-scored game logs (`clients/nfl.get_grouped_season_stats()`)
rather than assumed -- the same "measure it against real data instead of
guessing" discipline as `variance.py`'s (MLB) team-correlation
validation. This is what lets the NFL stack rating mathematically favor
QB-WR1/QB-WR2 over QB-RB, and quantify the "bring-back" (an opposing
pass-catcher stacked alongside the QB) instead of just asserting it's
good.

Methodology: for each team, rank that team's own WRs/TEs/RBs by
season-total DK points (the standard proxy for "who's actually WR1 vs
WR2" -- target share would be more precise but isn't in this app's
existing per-game stat columns), and identify the team's own starting
QB as whoever played the most games at QB. Then, across every team in
the league, pair that QB's weekly DK points with each ranked partner's
weekly DK points for the SAME game, and compute the Pearson correlation
over the whole league's pooled sample -- a single number per stack
type (qb_wr1, qb_wr2, qb_te1, qb_rb1), matching how the reference
material this was built from cited a single "~0.56" bring-back
coefficient rather than a per-team one. The bring-back correlation is
the QB's weekly points against that week's actual OPPONENT's WR1 (not
a fixed player -- the opponent changes every week), which is the real
"his team had to throw to keep up" signal.
"""

from __future__ import annotations

from typing import Any

from app.cache import cached
from app.clients import nfl
from app.config import get_settings
from app.services import nfl_dk_points

# Stack types worth a real correlation read -- rank 0 = that team's own
# top scorer at the position this season.
STACK_TYPES: dict[str, tuple[str, int]] = {
    "qb_wr1": ("WR", 0),
    "qb_wr2": ("WR", 1),
    "qb_te1": ("TE", 0),
    "qb_rb1": ("RB", 0),
}


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0 or var_y == 0:
        return None
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    return cov / (var_x**0.5 * var_y**0.5)


def _build_players(grouped: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """One entry per player: their most common team, position, season
    total DK points, and a week -> (dk_points, opponent_team) map."""
    players: dict[str, dict[str, Any]] = {}
    for pid, rows in grouped.items():
        team_counts: dict[str, int] = {}
        weekly: dict[int, float] = {}
        weekly_opp: dict[int, str] = {}
        position = None
        for r in rows:
            week = r.get("week")
            if week is None:
                continue
            team = r.get("team") or ""
            team_counts[team] = team_counts.get(team, 0) + 1
            weekly[week] = nfl_dk_points.game_points(r)
            weekly_opp[week] = r.get("opponent_team") or ""
            position = r.get("position_group") or position
        if not weekly or not team_counts:
            continue
        players[pid] = {
            "team": max(team_counts, key=team_counts.get),
            "position": position,
            "season_total": sum(weekly.values()),
            "weekly": weekly,
            "weekly_opp": weekly_opp,
        }
    return players


def _rank_by_team_position(players: dict[str, dict[str, Any]]) -> dict[tuple[str, str], list[str]]:
    by_team_pos: dict[tuple[str, str], list[str]] = {}
    for pid, info in players.items():
        by_team_pos.setdefault((info["team"], info["position"]), []).append(pid)
    for ids in by_team_pos.values():
        ids.sort(key=lambda pid: players[pid]["season_total"], reverse=True)
    return by_team_pos


def _paired_weekly(
    qb_info: dict[str, Any], partner_info: dict[str, Any]
) -> tuple[list[float], list[float]]:
    xs, ys = [], []
    for week, qb_pts in qb_info["weekly"].items():
        partner_pts = partner_info["weekly"].get(week)
        if partner_pts is None:
            continue
        xs.append(qb_pts)
        ys.append(partner_pts)
    return xs, ys


async def get_league_correlations(season: int, *, force: bool = False) -> dict[str, Any]:
    """
    League-wide correlation coefficients for QB+WR1, QB+WR2, QB+TE1,
    QB+RB1 (same-team) stacks, plus the QB+opposing-WR1 bring-back --
    each with its own real sample size, so a thin sample is visible
    rather than hidden behind a bare number.
    """
    async def _load() -> dict[str, Any]:
        grouped = await nfl.get_grouped_season_stats(season, force=force)
        players = _build_players(grouped)
        by_team_pos = _rank_by_team_position(players)

        qb_by_team: dict[str, str] = {}
        for (team, pos), ids in by_team_pos.items():
            if pos == "QB" and ids:
                qb_by_team[team] = max(ids, key=lambda pid: len(players[pid]["weekly"]))

        results: dict[str, Any] = {}
        for stack_type, (position, rank) in STACK_TYPES.items():
            all_xs: list[float] = []
            all_ys: list[float] = []
            teams_sampled = 0
            for team, qb_pid in qb_by_team.items():
                partner_ids = by_team_pos.get((team, position)) or []
                if len(partner_ids) <= rank:
                    continue
                xs, ys = _paired_weekly(players[qb_pid], players[partner_ids[rank]])
                if xs:
                    all_xs.extend(xs)
                    all_ys.extend(ys)
                    teams_sampled += 1
            corr = _pearson(all_xs, all_ys)
            results[stack_type] = {
                "correlation": round(corr, 3) if corr is not None else None,
                "paired_games": len(all_xs),
                "teams_sampled": teams_sampled,
            }

        # Bring-back: QB's weekly points vs. that week's actual opponent's
        # WR1 -- the opponent (and therefore which player is "WR1") changes
        # every week, so this can't reuse _paired_weekly's fixed-partner shape.
        bb_xs: list[float] = []
        bb_ys: list[float] = []
        bb_games = 0
        for team, qb_pid in qb_by_team.items():
            qb_info = players[qb_pid]
            for week, qb_pts in qb_info["weekly"].items():
                opp_team = qb_info["weekly_opp"].get(week)
                if not opp_team:
                    continue
                opp_wr_ids = by_team_pos.get((opp_team, "WR")) or []
                if not opp_wr_ids:
                    continue
                opp_pts = players[opp_wr_ids[0]]["weekly"].get(week)
                if opp_pts is None:
                    continue
                bb_xs.append(qb_pts)
                bb_ys.append(opp_pts)
                bb_games += 1
        bb_corr = _pearson(bb_xs, bb_ys)
        results["qb_bring_back_wr1"] = {
            "correlation": round(bb_corr, 3) if bb_corr is not None else None,
            "paired_games": bb_games,
            "teams_sampled": len(qb_by_team),
        }

        results["season"] = season
        return results

    settings = get_settings()
    return await cached(
        f"nfl:correlations:{season}", settings.ttl_stats * 4, _load, force=force
    )
