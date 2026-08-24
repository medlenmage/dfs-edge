"""
NFL stack rating -- the QB+pass-catcher analog to MLB's per-team stack
score (scoring.py's stack_score + the Stacks tab). One row per team per
week: how good a QB-stack environment this offense is in, plus which of
its own pass-catchers (and which opposing one, for a bring-back) is the
strongest real correlation partner.

Built from a mix of what nfl_slate.py already computes (Vegas implied
total, game total, spread, prior-season pace) and two genuinely new
signals built specifically for this feature:

  * Real PROE (clients/nfl_pbp.py) -- this team's own pass rate over
    expectation, and the opponent's DEFENSIVE PROE-allowed (a pass-
    funnel read: do opposing offenses pass more than expected against
    this defense).
  * Empirical QB/pass-catcher correlation (nfl_correlations.py) --
    real Pearson correlation from a full season of DK-scored game
    logs, so QB-WR1/QB-WR2 outweighing QB-RB is a measured fact here,
    not an assumed weight.

Every component is named and weighted, same transparency contract as
scoring.py/nfl_scoring.py -- nothing folded into the score without a
visible reason.
"""

from __future__ import annotations

from typing import Any

from app.clients import nfl, nfl_pbp
from app.services import nfl_correlations, nfl_scoring, nfl_slate

# Neutral (0-bonus) game total -- roughly where a real NFL O/U splits.
LEAGUE_AVG_GAME_TOTAL = 44.0
GAME_TOTAL_SENSITIVITY = 1.5
GAME_TOTAL_MAX_ADJUSTMENT = 9.0

# The user's own spec: a high total combined with a close (0-7.5 pt)
# spread is the "back-and-forth shootout" combination that most
# reliably drives real passing volume -- worth a bonus beyond what the
# total and spread each contribute independently.
SHOOTOUT_SPREAD_MAX = 7.5
SHOOTOUT_MIN_TOTAL = 47.0
SHOOTOUT_BONUS = 6.0

PROE_SENSITIVITY = 1.2
PROE_MAX_ADJUSTMENT = 8.0

# Pass-funnel: the OPPONENT's defensive PROE-allowed. Positive means
# opposing offenses pass more than a neutral model expects against this
# defense -- exactly the "stout run D, vulnerable through the air"
# signal the user described.
FUNNEL_SENSITIVITY = 1.2
FUNNEL_MAX_ADJUSTMENT = 8.0

# Ranked pass-catcher slots worth surfacing as a recommended stack
# partner, in priority order -- mirrors nfl_correlations.STACK_TYPES.
_PARTNER_SLOTS: list[tuple[str, str, int, str]] = [
    ("qb_wr1", "WR", 0, "WR1"),
    ("qb_wr2", "WR", 1, "WR2"),
    ("qb_te1", "TE", 0, "TE1"),
    ("qb_rb1", "RB", 0, "RB1"),
]


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def _spread_label(spread_magnitude: float | None, favored: bool | None) -> str:
    """A plain-English favored/underdog read, not just a bare number --
    the same spread magnitude means something very different for a team
    favored by 3.5 vs. an underdog by 3.5."""
    if spread_magnitude is None:
        return "no spread available"
    if spread_magnitude == 0:
        return "pick'em"
    if favored is None:
        return f"{spread_magnitude:g}-pt spread (favorite unknown)"
    return f"favored by {spread_magnitude:g}" if favored else f"underdog by {spread_magnitude:g}"


def _game_total_component(
    total_line: float | None, spread_magnitude: float | None, favored: bool | None
) -> dict[str, Any]:
    if total_line is None:
        return {"value": 0.0, "favored": favored, "spread": spread_magnitude, "detail": "no game total available"}

    value = _clamp(
        (total_line - LEAGUE_AVG_GAME_TOTAL) * GAME_TOTAL_SENSITIVITY,
        -GAME_TOTAL_MAX_ADJUSTMENT, GAME_TOTAL_MAX_ADJUSTMENT,
    )
    spread_label = _spread_label(spread_magnitude, favored)
    detail = f"{total_line:g} game total, {spread_label}"
    if (
        total_line >= SHOOTOUT_MIN_TOTAL
        and spread_magnitude is not None
        and spread_magnitude <= SHOOTOUT_SPREAD_MAX
    ):
        value += SHOOTOUT_BONUS
        detail += " (shootout spot)"
    return {
        "value": round(value, 1),
        "favored": favored,
        "spread": spread_magnitude,
        "detail": detail,
    }


def _proe_component(off_proe: float | None) -> dict[str, Any]:
    if off_proe is None:
        return {"value": 0.0, "detail": "no PROE data available"}
    value = _clamp(off_proe * PROE_SENSITIVITY, -PROE_MAX_ADJUSTMENT, PROE_MAX_ADJUSTMENT)
    return {"value": round(value, 1), "off_proe": off_proe, "detail": f"{off_proe:+.1f} pts PROE"}


def _funnel_component(opp_def_proe_allowed: float | None) -> dict[str, Any]:
    if opp_def_proe_allowed is None:
        return {"value": 0.0, "detail": "no opponent PROE data available"}
    value = _clamp(
        opp_def_proe_allowed * FUNNEL_SENSITIVITY, -FUNNEL_MAX_ADJUSTMENT, FUNNEL_MAX_ADJUSTMENT
    )
    label = "pass funnel" if opp_def_proe_allowed > 1.0 else "tough pass D" if opp_def_proe_allowed < -1.0 else "neutral"
    return {
        "value": round(value, 1),
        "opp_def_proe_allowed": opp_def_proe_allowed,
        "detail": f"opp {label}: {opp_def_proe_allowed:+.1f} pts PROE allowed",
    }


def _pick_partners(
    players: list[dict[str, Any]], correlations: dict[str, Any]
) -> list[dict[str, Any]]:
    """This team's real rostered WR/TE/RB pool (already matchup-scored
    and sorted best-first by nfl_slate.py), matched to the strongest-
    correlated slot available -- WR1 before WR2 before TE1 before RB1,
    same priority the real correlation numbers justify."""
    by_position: dict[str, list[dict[str, Any]]] = {}
    for p in players:
        by_position.setdefault(p["position"], []).append(p)

    partners = []
    for stack_type, position, rank, label in _PARTNER_SLOTS:
        pool = by_position.get(position) or []
        if len(pool) <= rank:
            continue
        player = pool[rank]
        corr = (correlations.get(stack_type) or {}).get("correlation")
        partners.append(
            {
                "slot": label,
                "stack_type": stack_type,
                "name": player["name"],
                "salary": player.get("salary"),
                "projected_fpts": (player.get("projection") or {}).get("fpts"),
                "ownership_pct": (player.get("projection") or {}).get("ownership_pct"),
                "correlation": corr,
            }
        )
    return partners


def _pick_bring_back(opp_players: list[dict[str, Any]], correlations: dict[str, Any]) -> dict[str, Any] | None:
    opp_wrs = [p for p in opp_players if p["position"] == "WR"]
    if not opp_wrs:
        return None
    player = opp_wrs[0]
    corr = (correlations.get("qb_bring_back_wr1") or {}).get("correlation")
    return {
        "slot": "opp WR1 (bring-back)",
        "name": player["name"],
        "team": player["team"],
        "salary": player.get("salary"),
        "projected_fpts": (player.get("projection") or {}).get("fpts"),
        "ownership_pct": (player.get("projection") or {}).get("ownership_pct"),
        "correlation": corr,
    }


def _rate_team(
    team_data: dict[str, Any],
    opp_data: dict[str, Any],
    betting: dict[str, Any],
    proe: dict[str, Any],
    correlations: dict[str, Any],
) -> dict[str, Any]:
    team = team_data["abbrev"]
    opp = opp_data["abbrev"]
    implied_total = team_data.get("implied_total")
    total_line = betting.get("total_line")
    spread_magnitude = abs(betting["spread_line"]) if betting.get("spread_line") is not None else None
    favored = team_data.get("favored")

    env = nfl_scoring.team_environment_score(implied_total, is_home=team_data.get("is_home", False))

    game_total = _game_total_component(total_line, spread_magnitude, favored)
    team_proe = (proe.get(team) or {}).get("off_proe")
    opp_def_proe = (proe.get(opp) or {}).get("def_proe_allowed")
    proe_c = _proe_component(team_proe)
    funnel_c = _funnel_component(opp_def_proe)

    rating = _clamp(env["score"] + game_total["value"] + proe_c["value"] + funnel_c["value"])

    partners = _pick_partners(team_data.get("players") or [], correlations)
    bring_back = _pick_bring_back(opp_data.get("players") or [], correlations)

    top_partner = partners[0] if partners else None
    combined = None
    if top_partner and top_partner.get("salary") and team_data.get("players"):
        qbs = [p for p in team_data["players"] if p["position"] == "QB"]
        qb = qbs[0] if qbs else None
        if qb and qb.get("salary") and top_partner.get("projected_fpts") is not None and (qb.get("projection") or {}).get("fpts") is not None:
            combined_salary = qb["salary"] + top_partner["salary"]
            combined_fpts = (qb["projection"]["fpts"] or 0.0) + (top_partner["projected_fpts"] or 0.0)
            combined_own = None
            qb_own = (qb.get("projection") or {}).get("ownership_pct")
            partner_own = top_partner.get("ownership_pct")
            if qb_own is not None and partner_own is not None:
                combined_own = round(qb_own + partner_own, 1)
            combined = {
                "qb_name": qb["name"],
                "partner_name": top_partner["name"],
                "combined_salary": combined_salary,
                "combined_projected_fpts": round(combined_fpts, 2),
                "value_per_1000": round(combined_fpts / (combined_salary / 1000), 2) if combined_salary else None,
                "combined_ownership_pct": combined_own,
            }

    return {
        "team": team,
        "opponent": opp,
        "is_home": team_data.get("is_home", False),
        "rating": round(rating, 1),
        "components": {
            "environment": env,
            "game_total": game_total,
            "proe": proe_c,
            "pass_funnel": funnel_c,
        },
        "partners": partners,
        "bring_back": bring_back,
        "top_stack_value": combined,
    }


async def build_stack_ratings(season: int, week: int) -> dict[str, Any]:
    """
    Every team's stack rating for one week, best-first. Requires a DK
    salary CSV (and ideally RotoWire projections) already loaded for
    the week -- same "nothing to rank without a real pool" requirement
    as the optimizer and contest generator, since recommended partners
    are drawn from that week's real rostered players, not a name guess.
    """
    slate = await nfl_slate.build_slate(season, week)
    if not slate.get("games"):
        return {
            "season": season, "week": week, "teams": [],
            "message": slate.get("message") or "No games found.",
        }

    correlation_season = nfl.PRIOR_SEASON
    proe, correlations = (
        await nfl_pbp.get_team_proe(correlation_season),
        await nfl_correlations.get_league_correlations(correlation_season),
    )

    teams = []
    for game in slate["games"]:
        betting = game.get("betting") or {}
        home = {**game["home"], "is_home": True}
        away = {**game["away"], "is_home": False}
        teams.append(_rate_team(home, away, betting, proe, correlations))
        teams.append(_rate_team(away, home, betting, proe, correlations))

    teams.sort(key=lambda t: t["rating"], reverse=True)
    return {
        "season": season,
        "week": week,
        "correlation_season": correlation_season,
        "correlations": correlations,
        "teams": teams,
    }
