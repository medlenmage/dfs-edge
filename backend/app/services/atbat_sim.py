"""
At-bat-level MLB game simulation.

variance.py's existing Monte Carlo engine bootstraps a whole GAME's
worth of DK points as one atomic draw per player per trial, with
same-team correlation bolted on afterward via a shared per-trial team
multiplier. This module takes a genuinely different approach: simulate
a game PLATE APPEARANCE by PLATE APPEARANCE, tracking real base-out
state through 9 innings for both teams. Every participating player's
counting stats emerge from the SAME simulated game, so correlation --
a big inning lifting every hitter who batted in it, a whole lineup's
day getting tougher once the bullpen it's facing implodes -- is a
natural CONSEQUENCE of shared game state, not a separately-bolted-on
mechanism, and disaster/blowout innings show up on their own instead
of needing to be hand-modeled.

Deliberately simplified for a first version -- documented here rather
than silently assumed, so nobody mistakes "simplified" for "wrong":

  - No stolen bases modeled in-engine. Real SB attempt/success rates
    depend on the specific runner, situation, and catcher in ways this
    doesn't attempt to capture.
  - Baserunner advancement uses fixed, historically-typical
    probabilities (see _advance_runners()'s own docstring for the
    exact numbers), not real batted-ball location, exit velocity, or
    runner speed.
  - A starting pitcher's innings are ONE deterministic estimate (his
    own season IP/start, not itself stochastic) -- see
    starter_outs_target(). Real start-to-start variance in how long a
    starter goes isn't modeled yet.
  - The bullpen that relieves him is ONE aggregate per-PA outcome
    distribution built from the team's real bullpen ERA/K9/BB9 (see
    bullpen_pa_rates()), not real reliever-by-reliever matchups.
  - A game tied after 9 innings is called there for simulation
    purposes, not carried into real extra innings.
"""

from __future__ import annotations

import random
from typing import Any

PA_EVENTS: tuple[str, ...] = ("K", "BB", "HBP", "OUT", "1B", "2B", "3B", "HR")

# A reasonable modern-era MLB average PA-outcome breakdown -- the
# neutral blend target for a thin-sample batter, and the starting
# point bullpen_pa_rates() adjusts from a team's real ERA/K9/BB9.
LEAGUE_AVG_PA_RATES: dict[str, float] = {
    "K": 0.225, "BB": 0.085, "HBP": 0.010, "OUT": 0.453,
    "1B": 0.145, "2B": 0.045, "3B": 0.004, "HR": 0.033,
}
assert abs(sum(LEAGUE_AVG_PA_RATES.values()) - 1.0) < 1e-9, "LEAGUE_AVG_PA_RATES must sum to 1.0"


def pa_outcome_rates(game_log: list[dict[str, Any]]) -> dict[str, float]:
    """
    Season-aggregate empirical rates for each PA outcome TYPE, from a
    real hitter game log (clients/mlb.get_player_game_log()) -- the
    same "true talent, bootstrapped from real games" philosophy
    variance.py's whole-game pool already uses, just broken down to
    the plate-appearance level instead of the whole-game level.
    """
    pa = sum(g.get("plate_appearances") or 0 for g in game_log)
    if not pa:
        return {}
    k = sum(g.get("strikeouts") or 0 for g in game_log)
    bb = sum(g.get("walks") or 0 for g in game_log)
    hbp = sum(g.get("hit_by_pitch") or 0 for g in game_log)
    doubles = sum(g.get("doubles") or 0 for g in game_log)
    triples = sum(g.get("triples") or 0 for g in game_log)
    hr = sum(g.get("home_runs") or 0 for g in game_log)
    hits = sum(g.get("hits") or 0 for g in game_log)
    singles = max(0, hits - doubles - triples - hr)
    out = max(0, pa - (k + bb + hbp + hits))

    raw = {"K": k, "BB": bb, "HBP": hbp, "OUT": out, "1B": singles, "2B": doubles, "3B": triples, "HR": hr}
    total = sum(raw.values())
    return {event: count / total for event, count in raw.items()} if total else {}


def bullpen_pa_rates(bullpen_stat: dict[str, Any] | None) -> dict[str, float]:
    """
    A crude but real per-PA outcome distribution for "whichever
    relievers finish this game" -- there's no real per-PA bullpen
    breakdown available (clients/mlb.get_bullpen_stats() only has ERA/
    WHIP/K9), so this scales the league-average rates by how far the
    team's real bullpen ERA and K/9 deviate from league norms, same
    ratio-vs-baseline technique scoring.py's own components already
    use. Falls back to a neutral league-average bullpen with no data.
    """
    if not bullpen_stat or not bullpen_stat.get("era") or not bullpen_stat.get("k_per_9"):
        return dict(LEAGUE_AVG_PA_RATES)

    league_era, league_k9 = 4.00, 8.5
    era_ratio = max(0.6, min(1.6, bullpen_stat["era"] / league_era))
    k9_ratio = max(0.6, min(1.6, bullpen_stat["k_per_9"] / league_k9))

    rates = dict(LEAGUE_AVG_PA_RATES)
    rates["K"] *= k9_ratio
    # A worse (higher) ERA bullpen allows more of everything hit-related.
    for event in ("1B", "2B", "3B", "HR", "BB"):
        rates[event] *= era_ratio
    total = sum(rates.values())
    return {event: v / total for event, v in rates.items()}


def blend_pa_rates(
    batter_rates: dict[str, float],
    pitcher_rates: dict[str, float] | None,
    *,
    batter_pa: int = 0,
    league_rates: dict[str, float] = LEAGUE_AVG_PA_RATES,
    full_trust_pa: int = 300,
) -> dict[str, float]:
    """
    Blends the batter's own PA-outcome rates (shrunk toward league
    average for a thin sample, same shrinkage philosophy used
    everywhere else in this app) with the opposing pitcher's own
    allowed-rates -- each event's final probability moves in whatever
    direction BOTH signals agree on, each ratio individually capped so
    one extreme signal can't blow the blended distribution out to an
    unrealistic shape, then renormalized to sum to exactly 1.0.
    """
    if not batter_rates:
        batter_rates = league_rates
    trust = min(1.0, batter_pa / full_trust_pa) if full_trust_pa else 1.0

    combined: dict[str, float] = {}
    for event in PA_EVENTS:
        base = league_rates[event]
        batter_ratio = (batter_rates.get(event, base) / base) if base else 1.0
        batter_ratio = 1.0 + (batter_ratio - 1.0) * trust  # thin samples pull toward neutral
        batter_ratio = max(0.3, min(3.0, batter_ratio))
        if pitcher_rates:
            pitcher_ratio = (pitcher_rates.get(event, base) / base) if base else 1.0
            pitcher_ratio = max(0.3, min(3.0, pitcher_ratio))
        else:
            pitcher_ratio = 1.0
        combined[event] = base * batter_ratio * pitcher_ratio

    total = sum(combined.values())
    return {event: v / total for event, v in combined.items()} if total else dict(league_rates)


def starter_outs_target(pitcher_game_log: list[dict[str, Any]]) -> int:
    """
    How many outs tonight's starter is assumed to record before the
    bullpen takes over -- his own season average outs/start, rounded.
    A single deterministic estimate for V1 (see module docstring);
    falls back to 15 outs (5.0 IP, a reasonable league-average start)
    with no real log to draw from.
    """
    starts = [g for g in pitcher_game_log if (g.get("outs") or 0) > 0]
    if not starts:
        return 15
    return round(sum(g["outs"] for g in starts) / len(starts))


def _empty_line() -> dict[str, int]:
    return {
        "plate_appearances": 0, "hits": 0, "doubles": 0, "triples": 0, "home_runs": 0,
        "rbi": 0, "runs": 0, "walks": 0, "hit_by_pitch": 0, "strikeouts": 0,
    }


def _advance_runners(
    bases: tuple[int | None, int | None, int | None],
    event: str,
    batter_id: int,
    outs_before: int,
    rng: random.Random,
) -> tuple[tuple[int | None, int | None, int | None], list[int], bool]:
    """
    One plate appearance's effect on the bases. `bases` is
    (runner_on_1st, runner_on_2nd, runner_on_3rd), each a player_id or
    None -- tracked by player so a later run can be credited to the
    right hitter's own Runs stat. Returns (new_bases, scorers,
    extra_out) -- `scorers` is which player_id(s) crossed the plate on
    this play, `extra_out` is True only for a simplified double-play
    case (runner on 1st, under 2 outs, removed along with the batter).

    Fixed, historically-typical advancement rates (not modeled per
    batted-ball): on a single, a runner on 2nd scores 60% of the time
    (else holds at 3rd), a runner on 1st always reaches 2nd; on a
    double, a runner on 1st scores 45% of the time (else holds at
    3rd); a runner on 3rd scores on any other (non-strikeout) out 35%
    of the time with under 2 outs (a sac-fly/productive-out
    approximation); a runner on 1st is removed via a simplified 12%
    double-play chance on the same kind of out.
    """
    b1, b2, b3 = bases
    scorers: list[int] = []

    if event == "HR":
        scorers = [r for r in (b1, b2, b3) if r is not None] + [batter_id]
        return (None, None, None), scorers, False

    if event == "3B":
        scorers = [r for r in (b1, b2, b3) if r is not None]
        return (None, None, batter_id), scorers, False

    if event == "2B":
        scorers = [r for r in (b2, b3) if r is not None]
        new3 = None
        if b1 is not None:
            if rng.random() < 0.45:
                scorers.append(b1)
            else:
                new3 = b1
        return (None, batter_id, new3), scorers, False

    if event == "1B":
        if b3 is not None:
            scorers.append(b3)
        new3 = None
        if b2 is not None:
            if rng.random() < 0.60:
                scorers.append(b2)
            else:
                new3 = b2
        new2 = b1
        return (batter_id, new2, new3), scorers, False

    if event in ("BB", "HBP"):
        new1 = batter_id
        new2, new3 = b2, b3
        if b1 is not None:
            new2 = b1
            if b2 is not None:
                new3 = b2
                if b3 is not None:
                    scorers.append(b3)
        return (new1, new2, new3), scorers, False

    if event == "OUT":
        new_bases = bases
        extra_out = False
        if outs_before < 2:
            if b3 is not None and rng.random() < 0.35:
                scorers.append(b3)
                new_bases = (new_bases[0], new_bases[1], None)
            if new_bases[0] is not None and rng.random() < 0.12:
                extra_out = True
                new_bases = (None, new_bases[1], new_bases[2])
        return new_bases, scorers, extra_out

    # "K" -- no baserunner movement at all.
    return bases, [], False


def simulate_game(
    home_order: list[int],
    away_order: list[int],
    home_pa_rates: dict[int, dict[str, float]],
    away_pa_rates: dict[int, dict[str, float]],
    home_bullpen_rates: dict[str, float],
    away_bullpen_rates: dict[str, float],
    home_starter_outs: int,
    away_starter_outs: int,
    rng: random.Random,
    *,
    innings: int = 9,
) -> dict[int, dict[str, int]]:
    """
    A full simulated game's real box score, one plate appearance at a
    time, for both lineups. `home_pa_rates`/`away_pa_rates` are each
    team's OWN hitters' blended PA-outcome rates (see blend_pa_rates())
    against the OPPOSING starter; once that team's own starter has
    recorded `{home,away}_starter_outs` outs, the OPPOSING offense
    switches to facing `{home,away}_bullpen_rates` instead (applied
    uniformly to every hitter still due up, since the bullpen aggregate
    doesn't vary by which specific hitter is up -- see module
    docstring's documented simplifications).
    """
    box: dict[int, dict[str, int]] = {}
    home_idx = away_idx = 0
    away_outs_faced = home_outs_faced = 0  # outs recorded BY each team's OWN starter

    for _ in range(innings):
        # Away bats first (top of the inning) against the HOME starter,
        # using their own blended rates until his out budget runs out --
        # _simulate_half_inning_tracking_starter() itself handles the
        # mid-inning switch to home_bullpen_rates once it does.
        away_idx, outs_by_home_starter = _simulate_half_inning_tracking_starter(
            away_order, away_idx, away_pa_rates, box, rng,
            max(0, home_starter_outs - home_outs_faced), home_bullpen_rates,
        )
        home_outs_faced += outs_by_home_starter

        home_idx, outs_by_away_starter = _simulate_half_inning_tracking_starter(
            home_order, home_idx, home_pa_rates, box, rng,
            max(0, away_starter_outs - away_outs_faced), away_bullpen_rates,
        )
        away_outs_faced += outs_by_away_starter

    return box


def _simulate_half_inning_tracking_starter(
    order: list[int],
    start_idx: int,
    rates_by_id: dict[int, dict[str, float]],
    box: dict[int, dict[str, int]],
    rng: random.Random,
    starter_outs_remaining: int,
    bullpen_rates: dict[str, float],
) -> tuple[int, int]:
    """
    Simulates one team's half-inning plate appearance by plate
    appearance, mutating `box` (player_id -> counting-stat line, same
    shape _empty_line() returns) in place. `order` is the batting
    order (length 9 for a real lineup); `start_idx` is which slot
    leads off this half-inning, since the order continues across
    innings rather than resetting each one.

    Switches every still-due-up hitter from `rates_by_id` (the
    starter's own blended rates) to `bullpen_rates` (one aggregate
    distribution) mid-inning, the moment the starter's own remaining
    out budget hits zero -- a real start can end mid-inning, and DK
    scoring cares about exactly which outs were the starter's own.
    Returns (next_start_idx, outs_charged_to_the_starter).
    """
    bases: tuple[int | None, int | None, int | None] = (None, None, None)
    outs = 0
    idx = start_idx
    starter_outs_charged = 0
    on_bullpen = starter_outs_remaining <= 0

    # A real half-inning always ends in 3 outs -- rarely more than a
    # dozen or so plate appearances even in a genuine chaotic rally.
    # This cap only matters for a degenerate input (e.g. every event
    # weight pointing at HR/BB/hits, zero chance of ever making an
    # out), which real blend_pa_rates() output can't actually produce
    # (every event keeps a real floor probability), but a bug or a
    # malformed caller-supplied rates dict shouldn't be able to hang a
    # Monte Carlo engine meant to run thousands of trials.
    MAX_PA_PER_HALF_INNING = 40
    pa_this_inning = 0

    while outs < 3 and pa_this_inning < MAX_PA_PER_HALF_INNING:
        pa_this_inning += 1
        batter_id = order[idx % len(order)]
        rates = bullpen_rates if on_bullpen else rates_by_id[batter_id]
        event = rng.choices(PA_EVENTS, weights=[rates[e] for e in PA_EVENTS], k=1)[0]

        line = box.setdefault(batter_id, _empty_line())
        line["plate_appearances"] += 1

        outs_before = outs
        this_play_outs = 0
        if event == "K":
            line["strikeouts"] += 1
            outs += 1
            this_play_outs = 1
        elif event == "BB":
            line["walks"] += 1
        elif event == "HBP":
            line["hit_by_pitch"] += 1
        elif event == "OUT":
            outs += 1
            this_play_outs = 1
        elif event in ("1B", "2B", "3B", "HR"):
            line["hits"] += 1
            if event == "2B":
                line["doubles"] += 1
            elif event == "3B":
                line["triples"] += 1
            elif event == "HR":
                line["home_runs"] += 1

        new_bases, scorers, extra_out = _advance_runners(bases, event, batter_id, outs_before, rng)
        bases = new_bases
        if extra_out:
            outs += 1
            this_play_outs += 1
        for scorer_id in scorers:
            box.setdefault(scorer_id, _empty_line())["runs"] += 1
        if scorers:
            line["rbi"] += len(scorers)

        if not on_bullpen:
            starter_outs_charged += this_play_outs
            if starter_outs_charged >= starter_outs_remaining:
                on_bullpen = True

        idx += 1

    return idx % len(order), min(starter_outs_charged, starter_outs_remaining)
