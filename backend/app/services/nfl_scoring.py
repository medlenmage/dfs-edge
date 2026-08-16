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
for outdoor games.

Built the same way as scoring.py -- named, weighted, nothing hidden --
so it's easy to extend the same way the MLB model was (bullpen and
batted-ball quality both arrived after the first version). The natural
next additions, once there's real in-season data to compute them from:
pace (plays per game), and opponent defense allowed by position.
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


def score_player(
    position: str,
    *,
    implied_total: float | None,
    is_home: bool,
    spread: float | None,
    favored: bool,
    wind_mph: float | None = None,
    precip_chance_pct: float | None = None,
) -> dict[str, Any]:
    """
    The full 0-100 matchup score for one offensive position (or DST) in
    one game: team environment (Vegas total + home field) plus a
    position-aware game-script lean plus a weather adjustment for
    outdoor games. Returns the score alongside every component's own
    value and a plain-English reason, same transparency contract as
    scoring.py's hitter/pitcher scores.
    """
    env = team_environment_score(implied_total, is_home=is_home)
    script = game_script_component(position, spread, favored=favored)
    weather = weather_component(position, wind_mph, precip_chance_pct)

    components = {
        "implied_total": env,
        "game_script": script,
        "weather": weather,
    }
    total = env["score"] + script["value"] + weather["value"]
    top_driver = max(
        (("game_script", abs(script["value"])), ("weather", abs(weather["value"]))),
        key=lambda kv: kv[1],
    )[0] if (abs(script["value"]) > 0 or abs(weather["value"]) > 0) else "implied_total"

    return {
        "score": round(_clamp(total), 1),
        "components": components,
        "top_driver": top_driver,
    }
