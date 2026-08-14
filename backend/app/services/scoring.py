"""
The edge model: how a hitter's matchup gets turned into a single number.

DESIGN PRINCIPLE
----------------
Every component is visible and every weight lives in one place. A black
box you can't argue with is useless for DFS -- you need to be able to
look at a 78 and say "that's mostly park and weather, and I don't trust
the wind read tonight."

Each component is expressed as a MULTIPLIER around 1.00, where 1.00 is
"league average / neutral". They get combined with weights, then mapped
onto a 0-100 display score.

TUNE THESE. The weights below are a reasonable starting point, not
gospel. If you find that park factors are overrated on your slates,
drop that weight and see how your results change.
"""

from __future__ import annotations

from typing import Any

# --------------------------------------------------------------------------
# Weights -- these must sum to 1.0
# --------------------------------------------------------------------------
WEIGHTS = {
    "platoon": 0.26,      # how the hitter performs vs this pitcher's hand
    "pitcher": 0.24,      # how vulnerable this pitcher is to this hand
    "team_total": 0.20,   # Vegas implied runs for his team
    "park": 0.12,         # ballpark HR factor for his handedness
    "weather": 0.08,      # temperature + wind
    "form": 0.06,         # last 15 games vs season baseline
    "home_road": 0.04,    # his own home/road split
}

# Baselines used when a component is missing entirely.
NEUTRAL = 1.0

# League-average implied team total. Roughly half a typical 8.8 game total.
LEAGUE_IMPLIED_RUNS = 4.4

# Minimum plate appearances before we trust a split at face value.
MIN_PA_FULL_TRUST = 120
MIN_BF_FULL_TRUST = 150


def _shrink(value: float, sample: float, full_trust: float) -> float:
    """
    Regress a small-sample number toward neutral.

    A hitter who is 6-for-12 against lefties is not a 500-hitter vs LHP.
    With 12 PA out of a 120 PA trust threshold we keep 10% of the
    observed edge and assign the other 90% to league average.
    """
    if sample <= 0:
        return NEUTRAL
    trust = min(1.0, sample / full_trust)
    return NEUTRAL + (value - NEUTRAL) * trust


def _ratio(value: float | None, baseline: float | None, cap: float = 0.45) -> float:
    """Turn a stat into a multiplier vs the league baseline, capped."""
    if value is None or not baseline:
        return NEUTRAL
    raw = value / baseline
    return max(1 - cap, min(1 + cap, raw))


# --------------------------------------------------------------------------
# Individual components
# --------------------------------------------------------------------------

def platoon_component(
    split_stat: dict[str, Any] | None,
    league_avg_ops: float | None,
) -> dict[str, Any]:
    """How good is this hitter against this pitcher's handedness?"""
    if not split_stat or split_stat.get("ops") is None:
        return {"value": NEUTRAL, "detail": "no split data", "sample": 0}

    pa = split_stat.get("pa") or 0
    raw = _ratio(split_stat["ops"], league_avg_ops)
    value = _shrink(raw, pa, MIN_PA_FULL_TRUST)

    return {
        "value": round(value, 3),
        "ops": split_stat["ops"],
        "iso": split_stat.get("iso"),
        "k_pct": split_stat.get("k_pct"),
        "sample": pa,
        "detail": f"{split_stat['ops']:.3f} OPS in {pa} PA",
    }


def pitcher_component(
    pitcher_split: dict[str, Any] | None,
    league_avg_ops_against: float | None,
) -> dict[str, Any]:
    """
    How vulnerable is the pitcher to batters of this hand?

    Higher OPS-against means a BETTER matchup for the hitter, so this
    reads in the same direction as everything else.
    """
    if not pitcher_split or pitcher_split.get("ops_against") is None:
        return {"value": NEUTRAL, "detail": "no pitcher split data", "sample": 0}

    bf = pitcher_split.get("bf") or 0
    raw = _ratio(pitcher_split["ops_against"], league_avg_ops_against)
    value = _shrink(raw, bf, MIN_BF_FULL_TRUST)

    return {
        "value": round(value, 3),
        "ops_against": pitcher_split["ops_against"],
        "k_pct": pitcher_split.get("k_pct"),
        "hr_per_9": pitcher_split.get("hr_per_9"),
        "sample": bf,
        "detail": f"{pitcher_split['ops_against']:.3f} OPS allowed in {bf} BF",
    }


def team_total_component(implied_runs: float | None) -> dict[str, Any]:
    """
    Vegas implied team total, normalised.

    A 5.6-run implied total vs the 4.4 baseline gives 1.27 -- a strong
    signal, because the betting market prices in things you haven't
    thought of (bullpen usage, a late scratch, sharp money).
    """
    if implied_runs is None:
        return {"value": NEUTRAL, "detail": "no line available"}

    value = max(0.65, min(1.40, implied_runs / LEAGUE_IMPLIED_RUNS))
    return {
        "value": round(value, 3),
        "implied_runs": implied_runs,
        "detail": f"{implied_runs} implied runs",
    }


def park_component(hr_factor: float, runs_factor: float) -> dict[str, Any]:
    """Blend the HR factor (weighted more) with overall run environment."""
    value = 0.65 * hr_factor + 0.35 * runs_factor
    return {
        "value": round(value, 3),
        "hr_factor": hr_factor,
        "runs_factor": runs_factor,
        "detail": f"{hr_factor:.2f}x HR, {runs_factor:.2f}x runs",
    }


def weather_component(
    temp_effect: dict[str, Any] | None,
    wind: dict[str, Any] | None,
    roof_closed: bool,
) -> dict[str, Any]:
    """Temperature and wind, or a flat 1.00 under a closed roof."""
    if roof_closed:
        return {"value": NEUTRAL, "detail": "roof closed - weather neutral"}

    temp_mult = (temp_effect or {}).get("hr_multiplier", NEUTRAL)
    wind_mult = (wind or {}).get("hr_multiplier", NEUTRAL)
    value = temp_mult * wind_mult

    bits = []
    if temp_effect and temp_effect.get("label") != "unknown":
        bits.append(temp_effect["label"])
    if wind and wind.get("label") != "unknown":
        bits.append(f"{wind['label']} {wind.get('speed_mph', '?')} mph")

    return {
        "value": round(max(0.75, min(1.30, value)), 3),
        "detail": ", ".join(bits) or "no forecast",
    }


def form_component(
    recent: dict[str, Any] | None,
    season: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    Last 15 games vs season baseline.

    Deliberately the smallest weight in the model. Hot streaks are mostly
    noise, but they do move DFS ownership, which matters for tournaments.
    """
    if not recent or not season:
        return {"value": NEUTRAL, "detail": "no recent data"}
    if recent.get("ops") is None or not season.get("ops"):
        return {"value": NEUTRAL, "detail": "no recent data"}

    raw = _ratio(recent["ops"], season["ops"], cap=0.30)
    value = _shrink(raw, recent.get("pa") or 0, 60)

    trend = "hot" if value > 1.06 else "cold" if value < 0.94 else "steady"
    return {
        "value": round(value, 3),
        "recent_ops": recent["ops"],
        "season_ops": season["ops"],
        "detail": f"{trend} ({recent['ops']:.3f} last 15 vs {season['ops']:.3f} season)",
    }


def home_road_component(
    split_stat: dict[str, Any] | None,
    season: dict[str, Any] | None,
    is_home: bool,
) -> dict[str, Any]:
    """The hitter's own home/road tendency."""
    if not split_stat or not season:
        return {"value": NEUTRAL, "detail": "no home/road data"}
    if split_stat.get("ops") is None or not season.get("ops"):
        return {"value": NEUTRAL, "detail": "no home/road data"}

    raw = _ratio(split_stat["ops"], season["ops"], cap=0.25)
    value = _shrink(raw, split_stat.get("pa") or 0, 100)
    where = "home" if is_home else "road"
    return {
        "value": round(value, 3),
        "detail": f"{split_stat['ops']:.3f} OPS at {where}",
    }


# --------------------------------------------------------------------------
# Putting it together
# --------------------------------------------------------------------------

# How hard the composite multiplier is stretched onto the 0-100 scale.
# Higher = more spread between good and bad matchups.
SCORE_SENSITIVITY = 4.0


def combine(components: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """
    Weighted blend of the components into a 0-100 display score.

    A composite multiplier of 1.00 (dead average in every category) maps
    to exactly 50. From there the curve is a tanh, which matters: a hard
    linear scale clips, so the ten best matchups on a slate all show 100
    and you can't tell them apart. tanh compresses smoothly toward the
    ends instead, so the ordering is always preserved.

    Roughly:
        composite 0.85 -> 23     composite 1.10 -> 69
        composite 0.90 -> 31     composite 1.20 -> 83
        composite 1.00 -> 50     composite 1.30 -> 92
    """
    import math

    composite = 0.0
    used_weight = 0.0

    for name, weight in WEIGHTS.items():
        comp = components.get(name)
        if comp is None:
            continue
        composite += comp["value"] * weight
        used_weight += weight

    if used_weight == 0:
        composite = NEUTRAL
    else:
        # Renormalise so missing components don't drag the score toward zero.
        composite = composite / used_weight

    score = 50 + 50 * math.tanh((composite - 1.0) * SCORE_SENSITIVITY)
    score = max(0.0, min(100.0, score))

    # Which single factor is doing the most work, for the "why" blurb.
    drivers = sorted(
        (
            (name, (comp["value"] - 1.0) * WEIGHTS[name])
            for name, comp in components.items()
            if name in WEIGHTS
        ),
        key=lambda pair: abs(pair[1]),
        reverse=True,
    )

    return {
        "score": round(score, 1),
        "composite": round(composite, 3),
        "top_driver": drivers[0][0] if drivers else None,
        "drivers": [
            {"factor": name, "contribution": round(delta, 4)}
            for name, delta in drivers[:3]
        ],
    }


def league_average(stats: dict[int, dict[str, Any]], field: str, min_sample: int, sample_field: str) -> float | None:
    """
    Compute the league average for a field across everyone with enough
    playing time. Self-calibrating, so the model stays honest as run
    environment changes year to year.
    """
    values = [
        s[field]
        for s in stats.values()
        if s.get(field) is not None and (s.get(sample_field) or 0) >= min_sample
    ]
    if not values:
        return None
    return round(sum(values) / len(values), 4)
