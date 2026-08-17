"""
Raw DraftKings Classic MLB scoring, computed directly from a single
game's counting stats (one row from clients/mlb.py's
get_player_game_log()) -- not to be confused with scoring.py's matchup
"edge" score, a completely different 0-100 model built from platoon/
park/weather/bullpen signals. This is the literal DK scoring formula,
the building block variance.py uses to turn a season of real box
scores into a per-player outcome distribution for simulation.

DK Classic MLB scoring (confirmed with the user during the original
optimizer build):
  Hitters: Single +3, Double +5, Triple +8, HR +10, RBI +2, Run +2,
    BB +2, HBP +2, SB +5.
  Pitchers: +2.25/IP (+0.75/out), K +2, Win +4, ER -2, Hit against
    -0.6, BB against -0.6, HBP against -0.6, CG +2.5, CGSO +2.5
    (stacks with CG), No-hitter +5.
"""

from __future__ import annotations

from typing import Any


def hitter_game_points(game_row: dict[str, Any]) -> float:
    """DK points for one game from a hitter's game-log row. `hits` is
    total hits (singles+doubles+triples+HR), so singles are derived by
    subtraction rather than pulled directly -- MLB's stat blob has no
    separate "singles" field."""
    hits = game_row.get("hits", 0)
    doubles = game_row.get("doubles", 0)
    triples = game_row.get("triples", 0)
    home_runs = game_row.get("home_runs", 0)
    singles = hits - doubles - triples - home_runs

    pts = 0.0
    pts += singles * 3
    pts += doubles * 5
    pts += triples * 8
    pts += home_runs * 10
    pts += game_row.get("rbi", 0) * 2
    pts += game_row.get("runs", 0) * 2
    pts += game_row.get("walks", 0) * 2
    pts += game_row.get("hit_by_pitch", 0) * 2
    pts += game_row.get("stolen_bases", 0) * 5
    return round(pts, 2)


def pitcher_game_points(game_row: dict[str, Any]) -> float:
    """
    DK points for one game from a pitcher's game-log row. "No-hitter"
    has no direct flag in MLB's stat blob -- derived here as a complete
    game with zero hits allowed, the practical definition, rather than
    left unscored for lack of an official field.
    """
    outs = game_row.get("outs", 0)
    hits_against = game_row.get("hits_against", 0)
    complete_games = game_row.get("complete_games", 0)

    pts = 0.0
    pts += outs * 0.75  # 2.25/IP == 0.75/out
    pts += game_row.get("strikeouts", 0) * 2
    pts += game_row.get("wins", 0) * 4
    pts -= game_row.get("earned_runs", 0) * 2
    pts -= hits_against * 0.6
    pts -= game_row.get("walks_against", 0) * 0.6
    pts -= game_row.get("hit_batsmen", 0) * 0.6
    if complete_games:
        pts += 2.5
        if game_row.get("shutouts", 0):
            pts += 2.5  # CGSO stacks with CG
        if hits_against == 0:
            pts += 5  # no-hitter
    return round(pts, 2)
