"""
Pitcher skill metrics that ERA can't see.

ERA is a RESULT. It carries the defense behind him, the sequencing of
when the hits happened to land, and the share of fly balls that happened
to clear the wall -- none of which is the pitcher's own repeatable skill,
and all of which regresses hard. Ranking starters by ERA is how a
4.38-ERA Gausman grades out behind a 2.86-ERA Skubal by a far wider
margin than their actual stuff justifies.

Three metrics here, each stripping out a different piece of luck:

  csw_pct    Called Strikes + Whiffs, over total pitches. The best
             single read on strikeout SKILL, and it settles long before
             K/9 does. It beats swinging-strike rate alone because it
             also credits the pitcher who freezes hitters on the corners
             rather than only the one who makes them miss. League
             average sits near 27-28%; above 30% is elite.

  xfip       FIP with actual home runs replaced by the home runs his
             fly-ball rate would be EXPECTED to yield at the league's own
             HR/FB rate. HR/FB is notoriously unstable for pitchers, so
             this is the sharper forward-looking number.

  siera      Skill-Interactive ERA. Like xFIP, but it also reads batted-
             ball MIX and treats strikeouts non-linearly -- an extra
             strikeout is worth more to a pitcher who already misses a
             lot of bats, because the balls he does allow in play are the
             ones a defense converts. Below 3.50 is strong, above 4.50
             poor.

All three are scale-free skill reads, so they slot in BESIDE the matchup
components in scoring.py rather than replacing the Vegas, park and
weather signal that speaks to what tonight specifically looks like.
"""

from __future__ import annotations

from typing import Any

# Used only when a real league pool can't be computed. Anchored to the
# public consensus range rather than invented.
DEFAULT_LEAGUE_CSW = 0.275
DEFAULT_CFIP = 3.10
DEFAULT_LEAGUE_HR_FB = 0.11


def csw_pct(called_strikes: int, whiffs: int, pitches: int) -> float | None:
    """
    Called strikes plus whiffs, over every pitch thrown.

    Whiffs here are swinging strikes INCLUDING blocked swinging strikes
    and foul tips -- a foul tip into the mitt is a swing and a miss that
    happened to be caught, and the standard CSW definition counts it.
    """
    if not pitches:
        return None
    return round((called_strikes + whiffs) / pitches, 4)


def swstr_pct(swing_pct: float | None, whiff_pct: float | None) -> float | None:
    """
    Swinging-strike rate -- whiffs per PITCH -- from Savant's two
    published rates, which are measured against different denominators:
    swing_pct is swings/pitches and whiff_pct is whiffs/SWINGS, so the
    product is exactly whiffs/pitches.

    Not an approximation. Verified against directly-counted whiffs on
    real pitchers and it agrees to within 0.1pp.

    Both arrive from Savant as percentages (40.7 meaning 40.7%).
    """
    if swing_pct is None or whiff_pct is None:
        return None
    return round(swing_pct * whiff_pct / 10000, 4)


def balls_in_play(batters_faced: int, k: int, bb: int, hbp: int) -> int:
    """
    The batted balls behind a pitcher's line.

    Savant reports its batted-ball rates as a share of BATTED BALLS, and
    that count isn't published directly, so it's reconstructed as what's
    left of the batters he faced once the outcomes that never become a
    batted ball are removed. Sacrifices and catcher's interference make
    this very slightly low, which is acceptable: it moves every pitcher
    the same direction, and the league constants below are computed from
    the same definition, so comparisons between pitchers stay clean.
    """
    return max(0, batters_faced - k - bb - hbp)


def expected_fly_balls(
    batters_faced: int, k: int, bb: int, hbp: int, fly_ball_pct: float | None
) -> float | None:
    """How many fly balls he allowed, from his batted-ball rate."""
    if fly_ball_pct is None or not batters_faced:
        return None
    bip = balls_in_play(batters_faced, k, bb, hbp)
    if bip <= 0:
        return None
    return bip * fly_ball_pct / 100


def league_constants(pool: list[dict[str, Any]]) -> dict[str, float]:
    """
    The league's own HR/FB rate and FIP constant, computed from the pool
    actually in hand rather than hardcoded to some other season's value.

    Entries carry ip, k, bb, hbp, hr, er and fly_balls.
    """
    tot_ip = sum(p.get("ip") or 0 for p in pool)
    if tot_ip <= 0:
        return {"hr_fb": DEFAULT_LEAGUE_HR_FB, "cfip": DEFAULT_CFIP, "era": 4.00}

    tot_hr = sum(p.get("hr") or 0 for p in pool)
    tot_k = sum(p.get("k") or 0 for p in pool)
    tot_bb = sum(p.get("bb") or 0 for p in pool)
    tot_hbp = sum(p.get("hbp") or 0 for p in pool)
    tot_fb = sum(p.get("fly_balls") or 0 for p in pool)
    tot_er = sum(p.get("er") or 0 for p in pool)

    league_era = tot_er * 9 / tot_ip
    # This comes out around .16 rather than the ~.12 quoted publicly,
    # because Savant reports fly balls and popups as separate categories
    # and the classic HR/FB denominator folds infield popups in. The
    # denominator here is therefore smaller and the rate correspondingly
    # higher. That is fine and deliberate: the SAME fly-ball definition
    # feeds both this constant and each pitcher's expected-HR count, so
    # the two cancel and xFIP still lands on the ERA scale -- measured at
    # a 4.05 mean against a 3.98 league ERA. Don't "fix" this by swapping
    # in a published .12 without also folding popups into fly balls.
    hr_fb = (tot_hr / tot_fb) if tot_fb else DEFAULT_LEAGUE_HR_FB
    # The constant that puts FIP back on an ERA scale for THIS league.
    cfip = league_era - ((13 * tot_hr + 3 * (tot_bb + tot_hbp) - 2 * tot_k) / tot_ip)
    return {"hr_fb": round(hr_fb, 4), "cfip": round(cfip, 3), "era": round(league_era, 3)}


def xfip(
    ip: float,
    k: int,
    bb: int,
    hbp: int,
    fly_balls: float | None,
    league_hr_fb: float,
    cfip: float,
) -> float | None:
    """
    FIP with expected home runs in place of actual ones.

    The only change from FIP is the 13*HR term: instead of the homers he
    gave up, it uses the homers his fly-ball count would produce at the
    LEAGUE's HR/FB rate. A pitcher who allowed nine homers on a normal
    number of fly balls and one who allowed three on the same number come
    out the same here -- which is the point, since the difference between
    them has historically been mostly noise.
    """
    if not ip or fly_balls is None:
        return None
    expected_hr = fly_balls * league_hr_fb
    return round(((13 * expected_hr + 3 * (bb + hbp) - 2 * k) / ip) + cfip, 3)


def siera(
    k: int,
    bb: int,
    pa: int,
    gb_pct: float | None,
    fb_pct: float | None,
    pu_pct: float | None,
) -> float | None:
    """
    Skill-Interactive ERA, on the published FanGraphs coefficients.

    The batted-ball rates arrive as a share of batted balls (Savant's own
    denominator) and the formula wants them as a share of PA, so they get
    rescaled by this pitcher's own balls-in-play share first.

    The squared net-ground-ball term carries a sign that flips with the
    sign of the net rate itself -- subtracted for a ground-ball pitcher,
    added for a fly-ball one. It is a second-order correction, not a
    reversal: measured across a realistic spread it contributes 0.003 to
    0.13 runs against the linear term's 0.04 to 0.26, so SIERA stays
    monotone in ground-ball rate. Sweeping GB from 25% to 66% at a fixed
    K and BB moves SIERA 3.36 -> 2.37 without ever turning back up. An
    extreme fly-ball profile is NOT credited here the way it sometimes
    gets described; what the squared term actually does is taper how much
    each additional ground ball is worth.
    """
    if not pa or gb_pct is None or fb_pct is None or pu_pct is None:
        return None
    bip = pa - k - bb
    if bip <= 0:
        return None
    bip_share = bip / pa

    so_pa = k / pa
    bb_pa = bb / pa
    gb = gb_pct / 100 * bip_share
    fb = fb_pct / 100 * bip_share
    pu = pu_pct / 100 * bip_share
    net_gb = gb - fb - pu

    value = (
        6.145
        - 16.986 * so_pa
        + 11.434 * bb_pa
        - 1.858 * net_gb
        + 7.653 * so_pa**2
        + 10.130 * so_pa * net_gb
        - 5.195 * bb_pa * net_gb
    )
    squared = 6.664 * net_gb**2
    value += -squared if net_gb > 0 else squared
    return round(value, 3)
