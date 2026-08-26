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
    "platoon": 0.136,          # how the hitter performs vs this pitcher's hand
    "team_total": 0.155,       # Vegas implied runs for his team
    "pitcher": 0.126,          # how vulnerable this pitcher is to this hand
    "contact_quality": 0.116,  # Statcast barrel/hard-hit/xwOBA vs league average
    "stolen_base": 0.068,      # his own season-long stolen-base rate vs league average
    "park": 0.078,             # ballpark HR factor for his handedness
    "bullpen": 0.049,          # opposing team's relief corps' SEASON-long quality (ERA vs league)
    "bullpen_workload": 0.039, # opposing bullpen's RECENT usage (last 2 days) -- independent of season quality
    "weather": 0.058,          # temperature + wind
    "form": 0.029,             # last 15 games vs season baseline
    "home_road": 0.010,        # his own home/road split
    "home_run": 0.058,         # his own individual HR probability vs this specific pitcher, blended with a real market HR prop when one exists
    "hit_probability": 0.049,  # real market "will he get a hit tonight" prop -- neutral (no opinion) when no prop line was fetched
    "umpire": 0.03,            # today's home-plate umpire's own season RPG/KPG -- neutral until RotoWire posts the assignment
}
# Fourteen weights, each trimmed a little (never gutted) to make room
# for the newest addition -- same philosophy each prior addition here
# used (see `home_run`'s own original note): every existing signal
# loses a little ground, none loses most of it.

# Baselines used when a component is missing entirely.
NEUTRAL = 1.0

# League-average implied team total. Roughly half a typical 8.8 game total.
LEAGUE_IMPLIED_RUNS = 4.4

# Minimum plate appearances before we trust a split at face value.
MIN_PA_FULL_TRUST = 120
MIN_BF_FULL_TRUST = 150
# Stolen-base attempts are a much rarer event than a plate appearance,
# and depend as much on a manager's green light as raw speed -- a
# 120-PA sample that's enough to trust an OPS split isn't enough to
# call someone a burner (or rule it out). Needs roughly half a season.
MIN_PA_FULL_TRUST_SB = 300


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


def stolen_base_component(
    season_stat: dict[str, Any] | None,
    league_avg_sb_per_pa: float | None,
) -> dict[str, Any]:
    """
    Stolen-base upside. DraftKings pays +5 for a steal -- the same as a
    double -- but nothing else in this model measures it: a low-power,
    high-average burner used to score purely on OPS/contact-quality/
    platoon and get no credit for an entire category of his real DK
    value, while a slow slugger with an identical OPS looked identical
    on paper despite having zero access to that category at all.

    Season-long rate, not a platoon or recent-form split. Base-stealing
    is a stable player skill (and as much a manager's green light) that
    doesn't swing by pitcher handedness or a hot couple of weeks the
    way batting outcomes do, so the season total is the right baseline
    rather than something split-specific.

    Wider than the +-45% most components cap at -- true talent gaps in
    stolen-base rate run proportionally much larger than an OPS split
    does, and squeezing them into that same band would erase most of
    the signal this exists to add -- but not so wide that one 8%-weight
    component can swamp the other nine; +-60% keeps a burner and a
    zero-steal player roughly +-10 points apart on an otherwise-neutral
    matchup, a real but not dominant swing.
    """
    if not season_stat or season_stat.get("sb_per_pa") is None:
        return {"value": NEUTRAL, "detail": "no stolen-base data", "sample": 0}

    pa = season_stat.get("pa") or 0
    raw = _ratio(season_stat["sb_per_pa"], league_avg_sb_per_pa, cap=0.6)
    value = _shrink(raw, pa, MIN_PA_FULL_TRUST_SB)

    return {
        "value": round(value, 3),
        "sb": season_stat.get("sb"),
        "sb_per_pa": season_stat["sb_per_pa"],
        "sample": pa,
        "detail": f"{season_stat.get('sb')} SB in {pa} PA this season",
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


# Real MLB umpires still call every pitch under 2026's ABS challenge
# system (each team gets 2 challenges/game, retained on success --
# only the most egregious misses get overturned), so umpire tendency
# stays a real, if damped, signal. Capped tighter than most other
# components (+-15%, vs weather's +-30%) since even a real zone
# difference is a much smaller lever on a hitter's night than his own
# matchup quality -- this is meant to nudge, not dominate.
UMPIRE_MULTIPLIER_CAP = 0.15


def umpire_component(
    umpire: dict[str, Any] | None,
    league_avg_rpg: float | None,
    league_avg_kpg: float | None,
) -> dict[str, Any]:
    """
    Today's assigned home-plate umpire's own season rate stats (RotoWire's
    RPG/KPG, see clients/rotowire_umpires.py) against the league average
    among every OTHER umpire posted today -- self-calibrating the same
    way league_average() is elsewhere in this module, rather than a
    guessed fixed baseline. Above-average RPG (a hitter-friendly/small
    zone) is hitter-favouring; above-average KPG (more punchouts) is
    pitcher-favouring, so the two pull in opposite directions and get
    averaged together into one multiplier.

    Neutral (1.00) whenever RotoWire hasn't posted this game's
    assignment yet, or there aren't enough OTHER umpires posted yet
    today to trust a real league average -- both real, common, and
    expected states well before first pitch, not errors.
    """
    if not umpire or not league_avg_rpg or not league_avg_kpg:
        return {"value": NEUTRAL, "detail": "no umpire assignment yet"}

    rpg, kpg = umpire.get("rpg"), umpire.get("kpg")
    if not rpg or not kpg:
        return {"value": NEUTRAL, "detail": f"{umpire.get('name', 'umpire')} -- no rate stats yet"}

    cap = UMPIRE_MULTIPLIER_CAP
    rpg_mult = max(1 - cap, min(1 + cap, rpg / league_avg_rpg))
    # Higher KPG than average hurts hitters -- inverted the same way
    # invert_for_pitcher() flips a hitter-favouring value, just applied
    # here directly since this IS the hitter-side read.
    kpg_mult = max(1 - cap, min(1 + cap, league_avg_kpg / kpg))
    value = (rpg_mult + kpg_mult) / 2

    return {
        "value": round(value, 3),
        "umpire": umpire.get("name"),
        "rpg": rpg,
        "kpg": kpg,
        "detail": f"{umpire.get('name')}: {rpg:g} RPG, {kpg:g} KPG (league avg {league_avg_rpg:g}/{league_avg_kpg:g})",
    }


# How much weight the real market gets vs. this app's own season-rate
# model, whenever a real batter_home_runs/pitcher_strikeouts prop line
# was actually fetched for a player -- weighted toward the market since
# real sportsbook pricing captures live information (today's actual
# lineup construction, weather already baked in by the book, sharp
# money) a season-average-only model has no way to see. Not 100%
# market, though -- a single book's line can move on thin liquidity or
# outright error, so the model still anchors it a little.
MARKET_BLEND_WEIGHT = 0.7


def home_run_component(
    season_stat: dict[str, Any] | None,
    league_avg_hr_per_pa: float | None,
    park_hr_factor: float,
    weather_hr_multiplier: float,
    pitcher_hr_per_9: float | None,
    league_avg_pitcher_hr_per_9: float | None,
    expected_pa: float = 4.3,
    market_hr_probability_pct: float | None = None,
) -> dict[str, Any]:
    """
    Individual home-run probability -- how likely is THIS batter,
    specifically, to hit one out tonight, not just "is it a good HR
    night for offense in general" (that's what `park`/`weather` already
    score at the team/environment level). Blends his own season HR rate
    (shrunk toward league average for a thin sample, same treatment as
    every other per-player rate here) with how HR-prone the pitcher
    he's facing has been this season, then separately layers on
    tonight's park/weather context to turn it into a real "at least one
    home run tonight" probability -- the same framing a real HR prop
    line uses.

    `market_hr_probability_pct`, when a real batter_home_runs prop line
    was fetched for this player tonight (clients/odds.py, gated behind
    ODDS_FETCH_PROPS), blends the market's own implied probability in at
    MARKET_BLEND_WEIGHT -- both the display `probability_pct` and the
    `value` multiplier that actually feeds `combine()`. Neutral (no
    change) whenever no prop line exists for this player.

    Returns two different things by design:
      - `value`: the 1.00-centred multiplier that feeds `combine()`.
        Deliberately excludes park/weather -- those already have their
        own separately-weighted `park`/`weather` components, so folding
        them in again here would double-count the same signal in the
        composite. Only his own power and the opposing pitcher's HR
        vulnerability are genuinely new information at this level.
      - `probability_pct`: the real, complete answer to "what's his
        actual home-run chance tonight" -- DOES include park/weather,
        since a genuine probability estimate shouldn't omit real
        context just because those factors are scored elsewhere too.
    """
    own_rate = (season_stat or {}).get("hr_per_pa")
    pa = (season_stat or {}).get("pa") or 0
    if own_rate is None or not league_avg_hr_per_pa:
        if market_hr_probability_pct is None:
            return {"value": NEUTRAL, "probability_pct": None, "detail": "no home-run rate data"}
        # No model rate to blend against (a very thin or missing season
        # sample) -- the market is the only real signal available, so
        # use it outright rather than discarding it.
        return {
            "value": NEUTRAL,
            "probability_pct": market_hr_probability_pct,
            "detail": f"{market_hr_probability_pct}% market-implied HR chance (no season rate to blend against)",
        }

    # A true HR-rate talent gap between players is much wider, proportionally,
    # than an OPS split -- same reasoning `stolen_base_component` already
    # gives for its own wide +-60% cap.
    own_ratio = _shrink(_ratio(own_rate, league_avg_hr_per_pa, cap=0.9), pa, MIN_PA_FULL_TRUST)
    pitcher_factor = _ratio(pitcher_hr_per_9, league_avg_pitcher_hr_per_9, cap=0.5)

    value = round(max(0.5, min(1.6, own_ratio * pitcher_factor)), 3)

    adjusted_rate = max(
        0.0,
        min(0.5, (league_avg_hr_per_pa * own_ratio) * park_hr_factor * weather_hr_multiplier * pitcher_factor),
    )
    probability_pct = round(max(0.0, min(70.0, (1 - (1 - adjusted_rate) ** expected_pa) * 100)), 1)
    detail = f"{probability_pct}% HR chance tonight ({own_rate:.3f} HR/PA season rate)"

    if market_hr_probability_pct is not None:
        # Compare the market's probability against a LEAGUE-AVERAGE
        # equivalent (the same binomial expansion above, at the league
        # rate) to get a comparable "how much better/worse than average
        # tonight" ratio, the same shape `value` already is -- then
        # blend both the multiplier and the display probability.
        league_avg_probability_pct = max(
            0.0, min(70.0, (1 - (1 - league_avg_hr_per_pa) ** expected_pa) * 100)
        )
        if league_avg_probability_pct:
            market_value = max(0.5, min(1.6, market_hr_probability_pct / league_avg_probability_pct))
            value = round(
                (1 - MARKET_BLEND_WEIGHT) * value + MARKET_BLEND_WEIGHT * market_value, 3
            )
        probability_pct = round(
            (1 - MARKET_BLEND_WEIGHT) * probability_pct + MARKET_BLEND_WEIGHT * market_hr_probability_pct, 1
        )
        detail = f"{probability_pct}% HR chance tonight (blended with a real {market_hr_probability_pct}% market prop)"

    return {
        "value": value,
        "probability_pct": probability_pct,
        "hr_per_pa": own_rate,
        "sample": pa,
        "detail": detail,
    }


# Empirical, roughly league-average probability a starting hitter records
# at least one hit across a typical game's worth of PA -- a fixed
# reference point, same spirit as LEAGUE_IMPLIED_RUNS above. No
# per-slate baseline exists for this today (unlike hr_per_pa/sb_per_pa/
# etc, which mlb_slate.py already computes from real league splits), so
# this component is deliberately market-only rather than inventing a
# from-scratch season-rate model to blend against.
LEAGUE_AVG_HIT_PROBABILITY_PCT = 68.0


def hit_probability_component(market_hit_probability_pct: float | None) -> dict[str, Any]:
    """
    Real market signal only -- unlike `home_run_component`, there's no
    in-house model here to blend against. Converts a real "at least one
    hit tonight" market-implied probability (clients/odds.py's
    batter_hits prop) into a 1.00-centred multiplier against
    LEAGUE_AVG_HIT_PROBABILITY_PCT. Neutral (1.0, no opinion) whenever
    no real prop line was fetched for this player tonight -- this
    component never fabricates a hit probability on its own.
    """
    if market_hit_probability_pct is None:
        return {"value": NEUTRAL, "probability_pct": None, "detail": "no market hit prop available"}
    value = round(max(0.6, min(1.4, market_hit_probability_pct / LEAGUE_AVG_HIT_PROBABILITY_PCT)), 3)
    return {
        "value": value,
        "probability_pct": market_hit_probability_pct,
        "detail": f"{market_hit_probability_pct}% market-implied chance of a hit tonight",
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


def bullpen_workload_component(
    opp_workload: dict[str, Any] | None,
    league_avg_outs: float | None,
    window_days: int,
) -> dict[str, Any]:
    """
    How worn down is the opposing bullpen from HEAVY RECENT usage --
    deliberately separate from `bullpen_component`'s season-long ERA
    read, which can't see a team that just leaned hard on relief the
    last couple days (an extra-inning marathon, a rough start pulled
    early, a bullpen game) independent of how the pen has pitched all
    year. A tired arm is a real short-term edge even for an otherwise
    strong bullpen having a fine season -- and conversely, a genuinely
    bad bullpen that's happened to be rested the last two days isn't
    made worse by this component (that's what `bullpen_component`
    already covers). Hitter-only, same reasoning as `bullpen_component`:
    a probable starter's own line isn't touched by either team's
    bullpen usage.
    """
    outs = (opp_workload or {}).get("outs")
    if not outs or not league_avg_outs:
        return {"value": NEUTRAL, "detail": "no recent bullpen workload data"}

    value = round(max(0.85, min(1.15, outs / league_avg_outs)), 3)
    innings = round(outs / 3, 1)
    return {
        "value": value,
        "outs": outs,
        "detail": f"{innings} bullpen innings in the last {window_days} days",
    }


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
    "opp_lineup": 0.194,               # strength of the lineup he's facing, vs him specifically
    "strikeout_potential": 0.165,      # his K stuff + how whiff-prone the lineup is
    "team_runs_against": 0.165,        # Vegas implied total for the team he's facing
    "contact_quality_allowed": 0.136,  # Statcast barrel/hard-hit/xwOBA allowed
    "own_quality": 0.116,              # his season ERA vs league average
    "park": 0.107,                     # ballpark run/HR suppression
    "weather": 0.087,                  # temperature + wind, suppression side
    "umpire": 0.03,                    # today's home-plate umpire's own season RPG/KPG, inverted -- neutral until posted
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
    market_k_line: float | None = None,
    expected_ip: float = 5.5,
) -> dict[str, Any]:
    """
    Strikeout upside: his own swing-and-miss stuff, blended with how
    strikeout-prone the lineup he's facing is.

    A power arm against a whiff-prone lineup should score well even on
    a night his ERA is nothing special -- that's the point of splitting
    this out from `own_quality` instead of burying it in ERA.

    `market_k_line`, when a real pitcher_strikeouts prop line was
    fetched for this pitcher tonight (clients/odds.py, gated behind
    ODDS_FETCH_PROPS), converts the market's own posted strikeout total
    into an equivalent K/9 pace (assuming `expected_ip` innings, a
    typical current-era start) and blends it into `value` at
    MARKET_BLEND_WEIGHT -- real sportsbook pricing already accounts for
    tonight's specific opponent and any info this app's own model can't
    see. Neutral (no change) whenever no prop line exists for this
    pitcher.
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

    if market_k_line is not None and league_avg_k9 and expected_ip:
        market_k9_pace = market_k_line / expected_ip * 9
        market_factor = max(0.6, min(1.4, market_k9_pace / league_avg_k9))
        value = round(
            (1 - MARKET_BLEND_WEIGHT) * value + MARKET_BLEND_WEIGHT * market_factor, 3
        )
        bits.append(f"market line {market_k_line} Ks")

    return {
        "value": value,
        "own_k_per_9": own_k9,
        "opp_avg_k_pct": round(opp_avg_k_pct, 4) if opp_avg_k_pct is not None else None,
        "market_k_line": market_k_line,
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
