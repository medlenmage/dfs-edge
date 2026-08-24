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

DK Classic NFL scoring:
  Passing: 0.04 pt/yard, TD +4, INT -1, 300+ yard bonus +3.
  Rushing: 0.1 pt/yard, TD +6, 100+ yard bonus +3.
  Receiving: reception (PPR) +1, 0.1 pt/yard, TD +6, 100+ yard bonus +3.
  Fumbles lost: -1 each. 2pt conversions (any type): +2 each.
  Special-teams TD: +6.
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
