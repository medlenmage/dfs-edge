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
    "platoon": 0.21,          # how the hitter performs vs this pitcher's hand
    "pitcher": 0.16,          # how vulnerable this pitcher is to this hand
    "team_total": 0.19,       # Vegas implied runs for his team
    "contact_quality": 0.15,  # Statcast barrel/hard-hit/xwOBA vs league average
    "bullpen": 0.08,          # opposing team's relief corps, for the innings after the starter leaves
    "park": 0.10,             # ballpark HR factor for his handedness
    "weather": 0.06,          # temperature + wind
    "form": 0.03,             # last 15 games vs season baseline
    "home_road": 0.02,        # his own home/road split
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


def bullpen_component(
    opp_bullpen: dict[str, Any] | None, league_avg_bullpen_era: float | None
) -> dict[str, Any]:
    """
    How exposed is this hitter to a shaky bullpen once the starter is out?

    The `pitcher` component already covers the starter matchup -- this is
    a second, smaller read on the innings after he leaves. A starter only
    goes five or six innings, and a 5.20 bullpen ERA against a ~4.00
    league average is a real edge late, independent of how tough the
    starter himself is. Hitter-only: a probable starter's own fantasy
    line isn't affected by his team's bullpen, since he's out of the
    game by the time it pitches.
    """
    era = (opp_bullpen or {}).get("era")
    if not era or not league_avg_bullpen_era:
        return {"value": NEUTRAL, "detail": "no bullpen data"}

    value = round(max(0.8, min(1.2, era / league_avg_bullpen_era)), 3)
    return {"value": value, "era": era, "detail": f"{era} opposing bullpen ERA"}


def contact_quality_component(
    profile: dict[str, Any] | None,
    league_avg_barrel: float | None,
    league_avg_hard_hit: float | None,
    league_avg_xwoba: float | None,
) -> dict[str, Any]:
    """
    Statcast's read on contact quality: barrel rate, hard-hit rate and
    expected wOBA, each vs the league average among qualified hitters.

    OPS tells you what happened. These tell you how well the ball was
    actually struck, independent of whether it found a fielder -- and
    they stabilise in far fewer plate appearances than OPS does. No
    sample-size shrink here; Savant's own leaderboard already filters to
    a minimum PA (see `clients/savant.py`).
    """
    if not profile:
        return {"value": NEUTRAL, "detail": "no Statcast data"}

    ratios = []
    if profile.get("barrel_pct") is not None and league_avg_barrel:
        ratios.append(profile["barrel_pct"] / league_avg_barrel)
    if profile.get("hard_hit_pct") is not None and league_avg_hard_hit:
        ratios.append(profile["hard_hit_pct"] / league_avg_hard_hit)
    if profile.get("xwoba") is not None and league_avg_xwoba:
        ratios.append(profile["xwoba"] / league_avg_xwoba)

    if not ratios:
        return {"value": NEUTRAL, "detail": "no Statcast data"}

    value = round(max(0.6, min(1.4, sum(ratios) / len(ratios))), 3)
    return {
        "value": value,
        "barrel_pct": profile.get("barrel_pct"),
        "hard_hit_pct": profile.get("hard_hit_pct"),
        "xwoba": profile.get("xwoba"),
        "detail": (
            f"{profile.get('barrel_pct')}% barrels, "
            f"{profile.get('hard_hit_pct')}% hard-hit, "
            f"{profile.get('xwoba')} xwOBA"
        ),
    }


# --------------------------------------------------------------------------
# Pitcher components -- same 1.00-centred multiplier convention as above,
# reusing the hitter components wherever the underlying signal is
# identical (park, weather, opponent's implied total) and just flipping
# the sign, since what's good for a hitter is bad for the pitcher facing
# him.
# --------------------------------------------------------------------------
PITCHER_WEIGHTS = {
    "opp_lineup": 0.20,               # strength of the lineup he's facing, vs him specifically
    "strikeout_potential": 0.17,      # his K stuff + how whiff-prone the lineup is
    "team_runs_against": 0.17,        # Vegas implied total for the team he's facing
    "contact_quality_allowed": 0.14,  # Statcast barrel/hard-hit/xwOBA allowed
    "own_quality": 0.12,              # his season ERA vs league average
    "park": 0.11,                     # ballpark run/HR suppression
    "weather": 0.09,                  # temperature + wind, suppression side
}


def invert_for_pitcher(value: float, cap: float = 0.4) -> float:
    """
    Flip a hitter-favouring multiplier onto the pitcher's side of 1.00.

    A hitter component of 1.20 (20% above average, good for the hitter)
    becomes 0.80 for the pitcher facing him, and vice versa.
    """
    return round(max(1 - cap, min(1 + cap, 2 - value)), 3)


def opp_lineup_component(opposing_hitters: list[dict[str, Any]] | None) -> dict[str, Any]:
    """
    How tough is the lineup this pitcher is facing?

    Reuses the opposing hitters' own composite scores (already computed
    against this exact pitcher by the hitter model) rather than
    recomputing anything -- their top five bats' average matchup
    strength, inverted.
    """
    if not opposing_hitters:
        return {"value": NEUTRAL, "detail": "no opposing lineup data"}

    top = sorted((h["edge"]["composite"] for h in opposing_hitters), reverse=True)[:5]
    if not top:
        return {"value": NEUTRAL, "detail": "no opposing lineup data"}

    avg = sum(top) / len(top)
    value = invert_for_pitcher(avg)
    return {
        "value": value,
        "opp_avg_composite": round(avg, 3),
        "detail": f"opposing top bats average {avg:.2f}x vs this pitcher",
    }


def strikeout_potential_component(
    season_stat: dict[str, Any] | None,
    league_avg_k9: float | None,
    opposing_hitters: list[dict[str, Any]] | None,
    league_avg_hitter_k_pct: float | None,
) -> dict[str, Any]:
    """
    Strikeout upside: his own swing-and-miss stuff, blended with how
    strikeout-prone the lineup he's facing is.

    A power arm against a whiff-prone lineup should score well here even
    on a night his ERA is nothing special -- that's the point of
    splitting this out from `own_quality` instead of burying it in ERA.
    """
    own_k9 = (season_stat or {}).get("k_per_9")
    pitcher_factor = (
        max(0.6, min(1.4, own_k9 / league_avg_k9))
        if own_k9 and league_avg_k9
        else NEUTRAL
    )

    opp_k_pcts = [
        h["season"]["k_pct"]
        for h in (opposing_hitters or [])
        if (h.get("season") or {}).get("k_pct") is not None
    ]
    if opp_k_pcts and league_avg_hitter_k_pct:
        opp_avg_k_pct = sum(opp_k_pcts) / len(opp_k_pcts)
        opp_factor = max(0.6, min(1.4, opp_avg_k_pct / league_avg_hitter_k_pct))
    else:
        opp_avg_k_pct = None
        opp_factor = NEUTRAL

    value = round(max(0.6, min(1.4, 0.55 * pitcher_factor + 0.45 * opp_factor)), 3)
    bits = []
    if own_k9:
        bits.append(f"{own_k9} K/9")
    if opp_avg_k_pct is not None:
        bits.append(f"opponent strikes out {opp_avg_k_pct:.1%} of PA")
    return {
        "value": value,
        "own_k_per_9": own_k9,
        "opp_avg_k_pct": round(opp_avg_k_pct, 4) if opp_avg_k_pct is not None else None,
        "detail": ", ".join(bits) or "no strikeout data",
    }


def own_quality_component(
    season_stat: dict[str, Any] | None, league_avg_era: float | None
) -> dict[str, Any]:
    """Pure run-prevention: his season ERA vs the league-average starter."""
    era = (season_stat or {}).get("era")
    if not era or not league_avg_era:
        return {"value": NEUTRAL, "detail": "no season ERA"}

    value = round(max(0.6, min(1.4, league_avg_era / era)), 3)
    return {"value": value, "era": era, "detail": f"{era} ERA"}


def contact_quality_allowed_component(
    profile: dict[str, Any] | None,
    league_avg_barrel: float | None,
    league_avg_hard_hit: float | None,
    league_avg_xwoba: float | None,
) -> dict[str, Any]:
    """
    Same Statcast blend as the hitter's `contact_quality_component`, but
    for what this pitcher ALLOWS -- and inverted, since a pitcher who
    gets weak contact is a good matchup, not a bad one.
    """
    if not profile:
        return {"value": NEUTRAL, "detail": "no Statcast data"}

    ratios = []
    if profile.get("barrel_pct") is not None and league_avg_barrel:
        ratios.append(profile["barrel_pct"] / league_avg_barrel)
    if profile.get("hard_hit_pct") is not None and league_avg_hard_hit:
        ratios.append(profile["hard_hit_pct"] / league_avg_hard_hit)
    if profile.get("xwoba") is not None and league_avg_xwoba:
        ratios.append(profile["xwoba"] / league_avg_xwoba)

    if not ratios:
        return {"value": NEUTRAL, "detail": "no Statcast data"}

    raw = sum(ratios) / len(ratios)
    value = invert_for_pitcher(raw)
    return {
        "value": value,
        "barrel_pct": profile.get("barrel_pct"),
        "hard_hit_pct": profile.get("hard_hit_pct"),
        "xwoba": profile.get("xwoba"),
        "detail": (
            f"allows {profile.get('barrel_pct')}% barrels, "
            f"{profile.get('hard_hit_pct')}% hard-hit, "
            f"{profile.get('xwoba')} xwOBA"
        ),
    }


# --------------------------------------------------------------------------
# Putting it together
# --------------------------------------------------------------------------

# How hard the composite multiplier is stretched onto the 0-100 scale.
# Higher = more spread between good and bad matchups.
SCORE_SENSITIVITY = 4.0


def combine(
    components: dict[str, dict[str, Any]],
    weights: dict[str, float] = WEIGHTS,
) -> dict[str, Any]:
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

    `weights` defaults to the hitter WEIGHTS above; pass PITCHER_WEIGHTS
    to score a pitcher's components with the same machinery.
    """
    import math

    composite = 0.0
    used_weight = 0.0

    for name, weight in weights.items():
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
            (name, (comp["value"] - 1.0) * weights[name])
            for name, comp in components.items()
            if name in weights
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
