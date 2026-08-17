"""
NFL matchup score -- deliberately fewer components than scoring.py's
MLB model, and that's a decision, not a shortcut. MLB's platoon,
contact-quality, and bullpen signals all come from a full season of
granular Statcast-grade data. That doesn't exist in a comparably free,
reliable form for NFL -- especially this early, before a single 2026
game has been played, when there's no "this year's" defense-vs-position
data to lean on yet. Rather than pad this out with something that
*looks* like a nine-component model but is really guesswork wearing a
number, this ships with the handful of things genuinely knowable from
free data today: Vegas implied team total (the strongest signal in the
MLB model too), game script from the spread, home field, and weather
for outdoor games -- plus defense-vs-position and pace, computed from a
full prior completed season's real box scores (see
`clients/nfl.get_prior_season_context()`) as a static prior. That's the
same regression-to-a-baseline idea as MLB's small-sample shrinkage, just
shrunk all the way to "trust last season entirely" because there's zero
current-season sample yet.

Built the same way as scoring.py -- named, weighted, nothing hidden --
so it's easy to extend the same way the MLB model was (bullpen and
batted-ball quality both arrived after the first version).
"""

from __future__ import annotations

from typing import Any

# League-average implied team total, used as the neutral (score = 50)
# baseline -- roughly where a real NFL Vegas total splits a game.
LEAGUE_AVG_IMPLIED_TOTAL = 22.0

# How many score points one point of implied total above/below average
# is worth. 4 points per implied point means a team implied for 27
# (5 above average) scores a 70 on this component alone.
IMPLIED_TOTAL_SENSITIVITY = 4.0

HOME_BONUS = 3.0

# Game-script lean per position: how many score points to add (or, for
# an underdog, subtract) per "unit" of spread, where a 7-point spread is
# one unit. Favored teams run more (good for RB, whoever's covering
# their own lead), trailing teams throw more (good for QB/WR/TE
# volume). DST leans favored too -- a defense playing with a lead faces
# more predictable, sack-able passing downs late.
GAME_SCRIPT_LEAN = {
    "QB": -1.0,
    "RB": 1.8,
    "WR": -1.3,
    "TE": -0.8,
    "FLEX": -0.5,
    "DST": 1.4,
}
SPREAD_UNIT = 7.0
MAX_SPREAD_UNITS = 2.0  # cap the lean at a 14-point spread's worth

WEATHER_WIND_PENALTIES = (
    (20.0, -6.0),
    (15.0, -3.0),
)
WEATHER_PRECIP_THRESHOLD_PCT = 50
WEATHER_PRECIP_PENALTY = -3.0
# Wind barely touches a rushing game; a messier ball in the air can
# even mean more fumbles/tips for a defense to feed on.
WEATHER_POSITION_MULTIPLIER = {"RB": 0.3, "DST": -0.5}

# Defense-vs-position, from a full prior season of real box scores:
# how many DK pts/gm this opponent has allowed to this position, vs.
# league average. Calibrated so the most extreme real matchup in 2024
# (Carolina allowing ~40% more than average to RBs) lands in the same
# ballpark as game script's own extremes -- a real edge, not the whole
# story.
DEFENSE_VS_POSITION_SENSITIVITY = 10.0
DEFENSE_VS_POSITION_MAX_ADJUSTMENT = 8.0

# Pace, from the same prior season (plays run per game). A more diffuse
# signal than defense-vs-position -- more plays means more opportunity
# for everyone on that offense, not a specific mismatch -- so it's
# capped smaller.
PACE_SENSITIVITY = 40.0
PACE_MAX_ADJUSTMENT = 4.0


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def team_environment_score(implied_total: float | None, *, is_home: bool) -> dict[str, Any]:
    """
    The base 0-100 read on a team's spot tonight, before any
    position-specific lean: mostly Vegas implied total, with a small
    home-field bump. Neutral (50) when there's no line to work from.
    """
    if implied_total is None:
        return {"score": 50.0, "implied_total": None, "detail": "no betting line available"}

    score = 50.0 + (implied_total - LEAGUE_AVG_IMPLIED_TOTAL) * IMPLIED_TOTAL_SENSITIVITY
    score += HOME_BONUS if is_home else -HOME_BONUS
    return {
        "score": round(_clamp(score), 1),
        "implied_total": implied_total,
        "detail": f"{implied_total:g} implied points, {'home' if is_home else 'road'}",
    }


def game_script_component(position: str, spread: float | None, *, favored: bool) -> dict[str, Any]:
    """
    How much this position's outlook shifts based on expected game flow.
    `spread` is the game's spread magnitude (always positive -- sign
    doesn't matter here, only who's favored does).
    """
    if spread is None:
        return {"value": 0.0, "detail": "no spread available"}

    lean = GAME_SCRIPT_LEAN.get(position, 0.0)
    units = min(abs(spread) / SPREAD_UNIT, MAX_SPREAD_UNITS)
    adjustment = lean * units
    if not favored:
        adjustment = -adjustment

    role = "favored" if favored else "underdog"
    return {
        "value": round(adjustment, 1),
        "detail": f"{role} by {abs(spread):g}, {position} game-script lean",
    }


def weather_component(
    position: str, wind_mph: float | None, precip_chance_pct: float | None
) -> dict[str, Any]:
    """Outdoor-game weather penalty. Domed/closed-roof games never call this."""
    if wind_mph is None:
        return {"value": 0.0, "detail": "no forecast available"}

    penalty = 0.0
    for threshold, value in WEATHER_WIND_PENALTIES:
        if wind_mph >= threshold:
            penalty = value
            break

    multiplier = WEATHER_POSITION_MULTIPLIER.get(position, 1.0)
    penalty *= multiplier

    if precip_chance_pct is not None and precip_chance_pct >= WEATHER_PRECIP_THRESHOLD_PCT:
        penalty += WEATHER_PRECIP_PENALTY if multiplier >= 0 else -WEATHER_PRECIP_PENALTY

    detail = f"{wind_mph:g} mph wind"
    if precip_chance_pct is not None and precip_chance_pct >= WEATHER_PRECIP_THRESHOLD_PCT:
        detail += f", {precip_chance_pct:g}% precip chance"

    return {"value": round(penalty, 1), "detail": detail}


def defense_vs_position_component(
    position: str,
    allowed_per_game: float | None,
    league_avg_allowed: float | None,
) -> dict[str, Any]:
    """
    How this week's opponent has performed against this position over a
    full prior season -- the classic "defense vs. position" DFS signal.
    Uses last season's real numbers as a static prior; there's no
    current-season sample to lean on yet for the same reason nothing
    else in this model has one (see module docstring).
    """
    if allowed_per_game is None or not league_avg_allowed:
        return {"value": 0.0, "detail": "no defense-vs-position data available"}

    ratio = allowed_per_game / league_avg_allowed
    adjustment = max(
        -DEFENSE_VS_POSITION_MAX_ADJUSTMENT,
        min(DEFENSE_VS_POSITION_MAX_ADJUSTMENT, (ratio - 1.0) * DEFENSE_VS_POSITION_SENSITIVITY),
    )
    label = "funnel matchup" if ratio > 1.15 else "tough matchup" if ratio < 0.85 else "average matchup"
    return {
        "value": round(adjustment, 1),
        "allowed_per_game": round(allowed_per_game, 1),
        "league_avg": round(league_avg_allowed, 1),
        "detail": f"{label}: opp allows {allowed_per_game:.1f} DK pts/gm to {position} (league avg {league_avg_allowed:.1f})",
    }


def pace_component(plays_per_game: float | None, league_avg_pace: float | None) -> dict[str, Any]:
    """
    More offensive snaps means more opportunities for every skill
    player on that offense -- roughly uniform across positions, unlike
    game script's position-specific lean. For DST, callers should pass
    the *opponent's* pace instead: more plays the other way means more
    defensive snaps and more chances at a sack or turnover.
    """
    if plays_per_game is None or not league_avg_pace:
        return {"value": 0.0, "detail": "no pace data available"}

    ratio = plays_per_game / league_avg_pace
    adjustment = max(-PACE_MAX_ADJUSTMENT, min(PACE_MAX_ADJUSTMENT, (ratio - 1.0) * PACE_SENSITIVITY))
    return {
        "value": round(adjustment, 1),
        "plays_per_game": round(plays_per_game, 1),
        "league_avg": round(league_avg_pace, 1),
        "detail": f"{plays_per_game:.1f} plays/gm (league avg {league_avg_pace:.1f})",
    }


def score_player(
    position: str,
    *,
    implied_total: float | None,
    is_home: bool,
    spread: float | None,
    favored: bool,
    wind_mph: float | None = None,
    precip_chance_pct: float | None = None,
    defense_allowed_per_game: float | None = None,
    league_avg_defense_allowed: float | None = None,
    pace_plays_per_game: float | None = None,
    league_avg_pace: float | None = None,
) -> dict[str, Any]:
    """
    The full 0-100 matchup score for one offensive position (or DST) in
    one game: team environment (Vegas total + home field), a
    position-aware game-script lean, a weather adjustment for outdoor
    games, and defense-vs-position + pace from a prior-season prior.
    Returns the score alongside every component's own value and a
    plain-English reason, same transparency contract as scoring.py's
    hitter/pitcher scores.
    """
    env = team_environment_score(implied_total, is_home=is_home)
    script = game_script_component(position, spread, favored=favored)
    weather = weather_component(position, wind_mph, precip_chance_pct)
    defense = defense_vs_position_component(position, defense_allowed_per_game, league_avg_defense_allowed)
    pace = pace_component(pace_plays_per_game, league_avg_pace)

    components = {
        "implied_total": env,
        "game_script": script,
        "weather": weather,
        "defense_vs_position": defense,
        "pace": pace,
    }
    total = env["score"] + script["value"] + weather["value"] + defense["value"] + pace["value"]

    named = (
        ("game_script", abs(script["value"])),
        ("weather", abs(weather["value"])),
        ("defense_vs_position", abs(defense["value"])),
        ("pace", abs(pace["value"])),
    )
    nonzero = [kv for kv in named if kv[1] > 0]
    top_driver = max(nonzero, key=lambda kv: kv[1])[0] if nonzero else "implied_total"

    return {
        "score": round(_clamp(total), 1),
        "components": components,
        "top_driver": top_driver,
    }
