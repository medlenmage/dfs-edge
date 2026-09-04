"""
Raw DraftKings Classic NFL scoring, computed directly from a single
game's counting stats (one row from clients/nfl.py's
get_player_game_log() or its internal _parse_stat_row(), already
numeric) -- not to be confused with nfl_scoring.py's matchup "edge"
score, a completely different 0-100 model built from Vegas/weather/
game-script/defense-vs-position signals. This is the literal DK
scoring formula, moved out of clients/nfl.py (where it lived as a
private helper used only by the prior-season defense-vs-position
aggregate) into its own standalone module, mirroring mlb_dk_points.py's
role -- the building block a future NFL variance/outcome-pool model
would reuse the same way MLB's variance.py already reuses
mlb_dk_points.py.

DK Classic NFL scoring (offensive players):
  Passing: 0.04 pt/yard, TD +4, INT -1, 300+ yard bonus +3.
  Rushing: 0.1 pt/yard, TD +6, 100+ yard bonus +3.
  Receiving: reception (PPR) +1, 0.1 pt/yard, TD +6, 100+ yard bonus +3.
  Fumbles lost: -1 each. 2pt conversions (any type): +2 each.
  Special-teams TD: +6.

DK Classic NFL scoring (DST -- see dst_game_points()):
  Sack +1, INT +2, fumble recovery +2, defensive/ST TD +6, safety +2,
  blocked kick +2. Points allowed: 0 -> +10, 1-6 -> +7, 7-13 -> +4,
  14-20 -> +1, 21-27 -> 0, 28-34 -> -1, 35+ -> -4.
"""

from __future__ import annotations

from typing import Any


def game_points(row: dict[str, Any]) -> float:
    """DK points for one game from a player's game-log row (already
    numeric -- see clients/nfl.py's _parse_stat_row())."""
    pts = 0.0
    py = row.get("passing_yards", 0.0)
    pts += py * 0.04 + row.get("passing_tds", 0.0) * 4 - row.get("passing_interceptions", 0.0)
    if py >= 300:
        pts += 3

    ry = row.get("rushing_yards", 0.0)
    pts += ry * 0.1 + row.get("rushing_tds", 0.0) * 6
    if ry >= 100:
        pts += 3

    rey = row.get("receiving_yards", 0.0)
    pts += row.get("receptions", 0.0) + rey * 0.1 + row.get("receiving_tds", 0.0) * 6
    if rey >= 100:
        pts += 3

    fumbles_lost = (
        row.get("sack_fumbles_lost", 0.0)
        + row.get("rushing_fumbles_lost", 0.0)
        + row.get("receiving_fumbles_lost", 0.0)
    )
    pts -= fumbles_lost

    two_pt = (
        row.get("passing_2pt_conversions", 0.0)
        + row.get("rushing_2pt_conversions", 0.0)
        + row.get("receiving_2pt_conversions", 0.0)
    )
    pts += two_pt * 2

    pts += row.get("special_teams_tds", 0.0) * 6
    return round(pts, 2)


def dst_game_points(row: dict[str, Any]) -> float:
    """
    DK points for one game from a team's DST game-log row (already
    numeric -- see clients/nfl.py's _parse_team_stat_row()).

    Total defensive/special-teams touchdowns is `def_tds +
    special_teams_tds` -- nflverse keeps these as two disjoint
    top-level buckets (turnover-return TDs vs. kick/punt-return TDs),
    with more granular fields like fumble_recovery_tds/pt_return_tds
    nested inside one or the other rather than additive on top of it
    (confirmed by inspecting real rows: a parent and its likely child
    field are never both nonzero in a way that implies two separate
    events, only ever consistent with nesting) -- so those granular
    fields are deliberately NOT added again here to avoid
    double-counting.
    """
    pts = 0.0
    pts += row.get("def_sacks", 0.0) * 1
    pts += row.get("def_interceptions", 0.0) * 2
    pts += row.get("fumble_recovery_opp", 0.0) * 2
    pts += (row.get("def_tds", 0.0) + row.get("special_teams_tds", 0.0)) * 6
    pts += row.get("def_safeties", 0.0) * 2
    pts += (
        row.get("def_punt_blocks", 0.0)
        + row.get("def_pat_blocks", 0.0)
        + row.get("def_fg_blocks", 0.0)
    ) * 2

    pa = row.get("points_allowed")
    if pa is not None:
        if pa == 0:
            pts += 10
        elif pa <= 6:
            pts += 7
        elif pa <= 13:
            pts += 4
        elif pa <= 20:
            pts += 1
        elif pa <= 27:
            pts += 0
        elif pa <= 34:
            pts -= 1
        else:
            pts -= 4
    return round(pts, 2)


# --------------------------------------------------------------------------
# Vectorized forms, for the simulator
# --------------------------------------------------------------------------
#
# game_points()/dst_game_points() take one dict and are the definition.
# These take numpy arrays and must agree with them exactly -- a test
# feeds random stat lines through both and asserts equality, because a
# scorer that silently disagrees with the one used everywhere else would
# make every simulated ranking wrong in a way nothing else would catch.
#
# The two documented mismatch points are handled the same way here as
# above: full PPR (1.0 per reception, not 0.5), and the 100/300-yard
# bonuses are per category and stack, so a 120-rush/110-receive game
# collects both.
#
# Deliberately NOT rounded, unlike the scalar versions. Rounding to two
# decimals is right for display; inside a simulation it would quantize
# every one of millions of draws for no benefit. The equality test
# compares against the rounded scalar within that tolerance.


def game_points_vectorized(
    *,
    passing_yards=None,
    passing_tds=None,
    interceptions=None,
    rushing_yards=None,
    rushing_tds=None,
    receptions=None,
    receiving_yards=None,
    receiving_tds=None,
    fumbles_lost=None,
    two_point_conversions=None,
    special_teams_tds=None,
):
    """Array form of game_points(). Every argument is optional and
    defaults to zero, so a receiver-only call needs no passing stats."""
    import numpy as np

    arrays = [
        a for a in (
            passing_yards, passing_tds, interceptions, rushing_yards, rushing_tds,
            receptions, receiving_yards, receiving_tds, fumbles_lost,
            two_point_conversions, special_teams_tds,
        ) if a is not None
    ]
    if not arrays:
        raise ValueError("game_points_vectorized needs at least one stat array")
    zero = np.zeros_like(np.asarray(arrays[0], dtype=float))

    def _a(value):
        return zero if value is None else np.asarray(value, dtype=float)

    py, ry, rey = _a(passing_yards), _a(rushing_yards), _a(receiving_yards)
    pts = py * 0.04 + _a(passing_tds) * 4.0 - _a(interceptions)
    pts = pts + np.where(py >= 300, 3.0, 0.0)
    pts = pts + ry * 0.1 + _a(rushing_tds) * 6.0 + np.where(ry >= 100, 3.0, 0.0)
    pts = pts + _a(receptions) + rey * 0.1 + _a(receiving_tds) * 6.0
    pts = pts + np.where(rey >= 100, 3.0, 0.0)
    pts = pts - _a(fumbles_lost) + _a(two_point_conversions) * 2.0
    return pts + _a(special_teams_tds) * 6.0


# DK's points-allowed tiers, as (upper_bound_inclusive, points). The last
# entry catches everything above.
DST_POINTS_ALLOWED_TIERS = ((0, 10.0), (6, 7.0), (13, 4.0), (20, 1.0), (27, 0.0), (34, -1.0))
DST_POINTS_ALLOWED_WORST = -4.0


def dst_points_vectorized(
    *,
    points_allowed,
    sacks=None,
    interceptions=None,
    fumble_recoveries=None,
    defensive_tds=None,
    safeties=None,
    blocked_kicks=None,
):
    """
    Array form of dst_game_points().

    `points_allowed` is the OPPONENT's simulated points, which is the
    single strongest reason the team layer draws both sides of a matchup
    together -- a DST's score is mostly a function of the other team's
    draw, and it is the most correlated position on a slate.
    """
    import numpy as np

    pa = np.asarray(points_allowed, dtype=float)
    zero = np.zeros_like(pa)

    def _a(value):
        return zero if value is None else np.asarray(value, dtype=float)

    pts = (
        _a(sacks)
        + _a(interceptions) * 2.0
        + _a(fumble_recoveries) * 2.0
        + _a(defensive_tds) * 6.0
        + _a(safeties) * 2.0
        + _a(blocked_kicks) * 2.0
    )
    tier = np.full_like(pa, DST_POINTS_ALLOWED_WORST)
    for bound, value in reversed(DST_POINTS_ALLOWED_TIERS):
        tier = np.where(pa <= bound, value, tier)
    return pts + tier
