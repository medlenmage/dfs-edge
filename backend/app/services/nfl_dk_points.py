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
