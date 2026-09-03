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

Each hitter's own blended PA-outcome rates are further nudged (see
_apply_edge_composite(), DAMPED -- applying it at full strength
measurably distorted single-PA probabilities) by scoring.py's
edge.composite -- the same matchup-quality signal (park, weather,
bullpen quality, contact quality, recent form, Vegas implied team
total) that already drives every projected_points number elsewhere in
this app. Without ANY of this, a lineup's simulated results only
weakly tracked its own projected points (confirmed live: r=0.48 on a
real batch). This closed part of that gap, not all of it -- a
controlled live comparison against variance.py's already-validated
bootstrap engine, on the exact same entries, still shows meaningfully
weaker correlation and a wider, less-contained ROI spread. Genuinely
open, not yet root-caused to one specific mechanism.

Deliberately simplified for a first version -- documented here rather
than silently assumed, so nobody mistakes "simplified" for "wrong":

  - No stolen bases modeled in-engine. Real SB attempt/success rates
    depend on the specific runner, situation, and catcher in ways this
    doesn't attempt to capture.
  - Baserunner advancement uses fixed, historically-typical
    probabilities (see _advance_runners()'s own docstring for the
    exact numbers), not real batted-ball location, exit velocity, or
    runner speed.
  - A starting pitcher's innings are resampled each trial from his own
    real starts this season (see starter_outs_pool()) -- genuine
    start-to-start variance (an early hook on a bad night, saving the
    bullpen on a great one), not a single fixed estimate every trial
    repeats. Still doesn't condition that length on how THIS trial's
    start is actually going (a disaster 1st inning realistically raises
    the odds of an early hook, which this doesn't model) -- draws
    independently of the trial's own simulated events.
  - The bullpen that relieves him is ONE aggregate per-PA outcome
    distribution built from the team's real bullpen ERA/K9/BB9 (see
    bullpen_pa_rates()), not real reliever-by-reliever matchups.
  - A game tied after 9 innings is called there for simulation
    purposes, not carried into real extra innings.
  - A starter's Win is credited by a simplified rule -- his team
    out-scored the opponent and he recorded at least 15 outs (5 IP) --
    not MLB's real "leading when he left, and his team never
    relinquished that lead" rule, which needs inning-by-inning score
    tracking this engine doesn't do.
  - Without a CONFIRMED real lineup yet, a team's batting order falls
    back to RotoWire's own PROJECTED one (see _batting_order_for_side())
    -- someone else's guess, not a fact, and it can turn out wrong (a
    projected starter scratches, a projected bench bat unexpectedly
    plays). Lets a slate simulate well before real lineups lock, at
    that real accuracy cost.
  - Same fallback for the starting pitcher (see
    mlb_slate._projected_starter() / _pitcher_id_for_side()): with no
    real MLB probable pitcher announced yet, falls back to RotoWire's
    own highest-projected pitcher-position player for that team,
    resolved to a real MLB id by name-matching against the active
    roster -- also a guess, and also kept clearly separate from the
    real `probable_pitcher` field.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

from app.clients import mlb
from app.services import mlb_dk_points

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


def pitcher_allowed_rates(pitcher_game_log: list[dict[str, Any]]) -> dict[str, float]:
    """
    Same per-PA outcome-type breakdown as pa_outcome_rates(), but from
    the PITCHER's own game log (clients/mlb.get_player_game_log(...,
    group="pitching")) -- what he allows, not what a hitter does.

    The pitching-log row uses different field names for the same raw
    counts (hits_against/walks_against/hit_batsmen instead of hits/
    walks/hit_by_pitch), since get_player_game_log() deliberately keeps
    the two groups' fields distinct -- see that function's own
    docstring for why. plate_appearances there is battersFaced, the
    pitching side's real PA-against denominator.
    """
    pa = sum(g.get("plate_appearances") or 0 for g in pitcher_game_log)
    if not pa:
        return {}
    k = sum(g.get("strikeouts") or 0 for g in pitcher_game_log)
    bb = sum(g.get("walks_against") or 0 for g in pitcher_game_log)
    hbp = sum(g.get("hit_batsmen") or 0 for g in pitcher_game_log)
    doubles = sum(g.get("doubles") or 0 for g in pitcher_game_log)
    triples = sum(g.get("triples") or 0 for g in pitcher_game_log)
    hr = sum(g.get("home_runs") or 0 for g in pitcher_game_log)
    hits = sum(g.get("hits_against") or 0 for g in pitcher_game_log)
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
    pitcher_pa: int = 0,
    league_rates: dict[str, float] = LEAGUE_AVG_PA_RATES,
    full_trust_pa: int = 300,
    full_trust_pitcher_pa: int = 400,
) -> dict[str, float]:
    """
    Blends the batter's own PA-outcome rates with the opposing
    pitcher's own allowed-rates -- each event's final probability moves
    in whatever direction BOTH signals agree on, each ratio
    individually capped so one extreme signal can't blow the blended
    distribution out to an unrealistic shape, then renormalized to sum
    to exactly 1.0.

    BOTH sides are shrunk toward league average for thin samples, not
    just the batter. Pitchers were originally never shrunk at all -- a
    3-start call-up with zero HR allowed hit the 0.3x floor on the HR
    event in every single trial, an absurdly confident read of ~40
    batters faced. `pitcher_pa` is his real batters-faced count;
    ~400 BF (a bit over a half-season of starts) earns full trust,
    mirroring the batter's own 300-PA bar.
    """
    if not batter_rates:
        batter_rates = league_rates
    trust = min(1.0, batter_pa / full_trust_pa) if full_trust_pa else 1.0
    pitcher_trust = (
        min(1.0, pitcher_pa / full_trust_pitcher_pa) if full_trust_pitcher_pa else 1.0
    )

    combined: dict[str, float] = {}
    for event in PA_EVENTS:
        base = league_rates[event]
        batter_ratio = (batter_rates.get(event, base) / base) if base else 1.0
        batter_ratio = 1.0 + (batter_ratio - 1.0) * trust  # thin samples pull toward neutral
        batter_ratio = max(0.3, min(3.0, batter_ratio))
        if pitcher_rates:
            pitcher_ratio = (pitcher_rates.get(event, base) / base) if base else 1.0
            pitcher_ratio = 1.0 + (pitcher_ratio - 1.0) * pitcher_trust
            pitcher_ratio = max(0.3, min(3.0, pitcher_ratio))
        else:
            pitcher_ratio = 1.0
        combined[event] = base * batter_ratio * pitcher_ratio

    total = sum(combined.values())
    return {event: v / total for event, v in combined.items()} if total else dict(league_rates)


def starter_outs_pool(pitcher_game_log: list[dict[str, Any]]) -> list[int]:
    """
    Every recorded outs-total from this pitcher's own real starts this
    season -- a bootstrap resampling pool (the same "draw from real
    observed games" philosophy variance.py's own outcome pools already
    use), sampled once per simulated trial by the caller
    (simulate_slate_trials()) instead of averaged into one fixed
    estimate. A real start has genuine game-to-game variance in how
    long it goes (an early hook on a bad night, saving the bullpen on a
    great one) that a single deterministic number can't produce at all
    -- confirmed as a real, measurable gap: a real backtest against
    archived contest data (scripts/backtest_atbat_engine.py) found
    starting pitchers showed ZERO innings variance across every
    simulated trial when this was a fixed average, and their
    calibration against real outcomes was measurably worse than
    hitters' as a direct result.

    Falls back to [15] (5.0 IP, a reasonable league-average start) with
    no real starts logged -- still a genuine (if single-valued) pool,
    so the caller's own random.choice() never needs a separate empty-
    pool branch.
    """
    starts = [g["outs"] for g in pitcher_game_log if (g.get("outs") or 0) > 0]
    return starts or [15]


def _empty_line() -> dict[str, int]:
    return {
        "plate_appearances": 0, "hits": 0, "doubles": 0, "triples": 0, "home_runs": 0,
        "rbi": 0, "runs": 0, "walks": 0, "hit_by_pitch": 0, "strikeouts": 0,
    }


def _empty_pitcher_line() -> dict[str, int]:
    """Same field names mlb_dk_points.pitcher_game_points() reads off a
    real game-log row -- a simulated starter's line converts through it
    exactly like a real one does."""
    return {
        "outs": 0, "strikeouts": 0, "earned_runs": 0, "hits_against": 0,
        "walks_against": 0, "hit_batsmen": 0, "wins": 0, "complete_games": 0,
        "shutouts": 0,
    }


def _accumulate_pitcher_line(total: dict[str, int], half: dict[str, int]) -> None:
    for key in ("outs", "strikeouts", "earned_runs", "hits_against", "walks_against", "hit_batsmen"):
        total[key] += half[key]


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
) -> dict[str, Any]:
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

    Returns `{"box": {player_id: hitting_line}, "home_starter_line":
    ..., "away_starter_line": ..., "home_runs": int, "away_runs": int}`
    -- each starter's own line is in mlb_dk_points.pitcher_game_points()'s
    exact expected shape, decision/complete-game/shutout included (see
    module docstring for the simplified Win rule).
    """
    box: dict[int, dict[str, int]] = {}
    home_idx = away_idx = 0
    away_outs_faced = home_outs_faced = 0  # outs recorded BY each team's OWN starter
    home_starter_line = _empty_pitcher_line()
    away_starter_line = _empty_pitcher_line()
    home_score = away_score = 0

    for inning in range(1, innings + 1):
        # Away bats first (top of the inning) against the HOME starter,
        # using their own blended rates until his out budget runs out --
        # _simulate_half_inning_tracking_starter() itself handles the
        # mid-inning switch to home_bullpen_rates once it does.
        away_idx, outs_by_home_starter, half_home_line, top_runs = (
            _simulate_half_inning_tracking_starter(
                away_order, away_idx, away_pa_rates, box, rng,
                max(0, home_starter_outs - home_outs_faced), home_bullpen_rates,
            )
        )
        home_outs_faced += outs_by_home_starter
        _accumulate_pitcher_line(home_starter_line, half_home_line)
        away_score += top_runs

        # The home team doesn't bat in the bottom of the final inning
        # when it's already ahead -- a real game just ends. The old
        # unconditional bottom half handed home hitters 3-4 phantom
        # plate appearances across the lineup in every game they led.
        if inning == innings and home_score > away_score:
            break

        # In the bottom of the final inning, the game ends the moment
        # the home team takes the lead (a walk-off) -- runs past that
        # can't happen in a real game either.
        walkoff_deficit = (away_score - home_score) if inning == innings else None
        home_idx, outs_by_away_starter, half_away_line, bottom_runs = (
            _simulate_half_inning_tracking_starter(
                home_order, home_idx, home_pa_rates, box, rng,
                max(0, away_starter_outs - away_outs_faced), away_bullpen_rates,
                walkoff_deficit=walkoff_deficit,
            )
        )
        away_outs_faced += outs_by_away_starter
        _accumulate_pitcher_line(away_starter_line, half_away_line)
        home_score += bottom_runs

    home_runs = sum(box.get(pid, {}).get("runs", 0) for pid in home_order)
    away_runs = sum(box.get(pid, {}).get("runs", 0) for pid in away_order)

    # A complete game means the starter recorded every out his side's
    # defense actually played. For the AWAY starter that's legitimately
    # 24 rather than 27 when the home team won -- a winning home team
    # doesn't bat in (or finish) the bottom of the final inning.
    home_starter_line["complete_games"] = int(home_outs_faced >= innings * 3)
    away_cg_outs = (innings - 1) * 3 if home_score > away_score else innings * 3
    away_starter_line["complete_games"] = int(away_outs_faced >= away_cg_outs)
    home_starter_line["shutouts"] = int(home_starter_line["complete_games"] and away_runs == 0)
    away_starter_line["shutouts"] = int(away_starter_line["complete_games"] and home_runs == 0)

    MIN_OUTS_FOR_WIN = 15  # 5.0 IP -- the simplified Win eligibility bar (see module docstring)
    if home_runs > away_runs and home_outs_faced >= MIN_OUTS_FOR_WIN:
        home_starter_line["wins"] = 1
    if away_runs > home_runs and away_outs_faced >= MIN_OUTS_FOR_WIN:
        away_starter_line["wins"] = 1

    return {
        "box": box,
        "home_starter_line": home_starter_line,
        "away_starter_line": away_starter_line,
        "home_runs": home_runs,
        "away_runs": away_runs,
    }


def _simulate_half_inning_tracking_starter(
    order: list[int],
    start_idx: int,
    rates_by_id: dict[int, dict[str, float]],
    box: dict[int, dict[str, int]],
    rng: random.Random,
    starter_outs_remaining: int,
    bullpen_rates: dict[str, float],
    *,
    walkoff_deficit: int | None = None,
) -> tuple[int, int, dict[str, int], int]:
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
    Returns (next_start_idx, outs_charged_to_the_starter, a partial
    _empty_pitcher_line()-shaped dict of what the STARTER (not the
    bullpen) allowed this half-inning only -- simulate_game() sums
    these across all of a starter's half-innings itself, and the runs
    scored this half-inning).

    Earned runs follow MLB's real charging rule: a run is charged to
    whichever pitcher put THAT RUNNER on base, not whoever happens to
    be pitching when he scores. The old version only charged the
    starter when the scoring play itself was his -- runners he left on
    base who scored off the bullpen were never charged to anyone,
    which systematically under-counted starter ER (about -2 DK points
    per inherited runner who came around) and ran every simulated
    pitcher score hot.

    `walkoff_deficit`: when set (the bottom of the final inning), the
    half ends the moment this team's runs exceed it -- a real walk-off.
    """
    bases: tuple[int | None, int | None, int | None] = (None, None, None)
    outs = 0
    idx = start_idx
    starter_outs_charged = 0
    on_bullpen = starter_outs_remaining <= 0
    starter_line = _empty_pitcher_line()
    runs_this_half = 0
    # Which pitcher is responsible for each runner currently aboard --
    # keyed by player id (a player occupies at most one base). Reaching
    # base overwrites any stale entry from an earlier trip.
    reached_vs_starter: dict[int, bool] = {}

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
        pitched_by_starter = not on_bullpen
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
            if pitched_by_starter:
                starter_line["strikeouts"] += 1
        elif event == "BB":
            line["walks"] += 1
            if pitched_by_starter:
                starter_line["walks_against"] += 1
        elif event == "HBP":
            line["hit_by_pitch"] += 1
            if pitched_by_starter:
                starter_line["hit_batsmen"] += 1
        elif event == "OUT":
            outs += 1
            this_play_outs = 1
        elif event in ("1B", "2B", "3B", "HR"):
            line["hits"] += 1
            if pitched_by_starter:
                starter_line["hits_against"] += 1
            if event == "2B":
                line["doubles"] += 1
            elif event == "3B":
                line["triples"] += 1
            elif event == "HR":
                line["home_runs"] += 1

        if event in ("1B", "2B", "3B", "HR", "BB", "HBP"):
            # The batter reached (a HR "reaches" and scores in the same
            # play) -- record which pitcher is responsible for him, for
            # MLB's real earned-run charging rule below.
            reached_vs_starter[batter_id] = pitched_by_starter

        new_bases, scorers, extra_out = _advance_runners(bases, event, batter_id, outs_before, rng)
        bases = new_bases
        if extra_out:
            outs += 1
            this_play_outs += 1
        for scorer_id in scorers:
            box.setdefault(scorer_id, _empty_line())["runs"] += 1
            # MLB's real rule: the run is charged to whoever put THIS
            # RUNNER on base -- an inherited runner scoring off the
            # bullpen is still the starter's earned run.
            if reached_vs_starter.get(scorer_id, pitched_by_starter):
                starter_line["earned_runs"] += 1
        runs_this_half += len(scorers)
        if scorers:
            line["rbi"] += len(scorers)

        if walkoff_deficit is not None and runs_this_half > walkoff_deficit:
            # Walk-off: the game is over the instant the home team
            # leads in the bottom of the final inning.
            if not on_bullpen:
                starter_outs_charged = min(starter_outs_charged, starter_outs_remaining)
            idx += 1
            break

        if not on_bullpen:
            starter_outs_charged += this_play_outs
            if starter_outs_charged >= starter_outs_remaining:
                on_bullpen = True

        idx += 1

    starter_line["outs"] = min(starter_outs_charged, starter_outs_remaining)
    return idx % len(order), starter_line["outs"], starter_line, runs_this_half


# Fewer than this many projected batting-order spots isn't a usable
# stand-in for a real 9-hitter lineup -- see _batting_order_for_side().
MIN_PROJECTED_LINEUP_SIZE = 8


class SlateNotSimulatableError(Exception):
    """
    Raised by simulate_slate_trials() when at least one game on the
    slate isn't ready to at-bat-simulate -- V1 has no partial/hybrid
    fallback (mixing at-bat-simulated players with any other engine's
    players within the same lineup sum would need a whole reconciliation
    layer of its own). A side counts as ready with either a real
    CONFIRMED lineup or, failing that, a usable RotoWire PROJECTED
    lineup (see _batting_order_for_side()) -- so this only actually
    fires when a side has neither: no real lineup posted yet AND no
    projections file uploaded (or RotoWire's own projected lineup for
    that team is too thin to use). Callers should surface this message
    directly -- it already names exactly which game(s) are blocking.
    """


def _batting_order_for_side(side: dict[str, Any]) -> list[int]:
    """
    The hitter ids for one side of a game, in batting-order sequence --
    the REAL confirmed order if the lineup has posted
    (`lineup_confirmed`), or RotoWire's own PROJECTED batting spot
    (`projected_batting_order`, from an uploaded projections file's
    LINEUP column) as a fallback when it hasn't. This is what lets
    simulate_slate_trials() run well before real lineups lock -- at the
    real cost of simulating against someone else's lineup GUESS instead
    of the confirmed truth, which can turn out wrong (a projected
    starter gets a late scratch, a projected bench bat unexpectedly
    starts). Falling back is opt-in in effect only, in that it happens
    automatically whenever a real lineup isn't confirmed yet and a
    projections file is loaded -- there's no separate flag to force it
    off, since a caller who wants confirmed-only can just wait.
    """
    key = "batting_order" if side.get("lineup_confirmed") else "projected_batting_order"
    ordered = sorted((h for h in side.get("hitters", []) if h.get(key)), key=lambda h: h[key])
    return [h["id"] for h in ordered]


def _side_ready(side: dict[str, Any]) -> bool:
    if side.get("lineup_confirmed"):
        return True
    return len(_batting_order_for_side(side)) >= MIN_PROJECTED_LINEUP_SIZE


def _composites_for_side(side: dict[str, Any]) -> dict[int, float | None]:
    """Each hitter's own scoring.py matchup-quality composite (1.0 =
    league-average matchup) -- see _apply_edge_composite()."""
    return {h["id"]: (h.get("edge") or {}).get("composite") for h in side.get("hitters", [])}


# Bounds mirror the same "one extreme signal can't blow the blend out
# to an implausible shape" clamp philosophy blend_pa_rates() already
# uses for its own batter/pitcher ratios.
EDGE_COMPOSITE_MIN = 0.5
EDGE_COMPOSITE_MAX = 1.8

# Applying the raw composite directly (a 1.0-centered SEASON/roster-
# construction-level multiplier) to single-PA reach-base/out
# probabilities turned out to be far too strong a signal -- measured
# directly: an undamped composite of just 1.3 (a good, not extreme,
# matchup) pushed reach-base rate from a league-average 31.2% to 43.3%,
# an OBP-equivalent no real hitter sustains for a full SEASON, let
# alone deserves from one game's matchup edge. Damping to a fraction of
# the raw composite's deviation from 1.0 before applying it keeps the
# signal's DIRECTION and RELATIVE ordering intact (still the fix for
# the original weak-correlation problem) while keeping single-PA
# probabilities in a plausible range -- confirmed by the same direct
# measurement: a damped 1.3 composite only reaches ~35% reach-base, a
# few realistic points above league average rather than twelve.
#
# NOTE: this alone is a real, necessary fix but not a complete one. A
# controlled live comparison (identical entries, identical field, fixed
# seed, only this value varied 0.0-1.0) found correlation between
# projected_points and simulated_points_mean barely moves with damping
# strength (~0.34-0.42 throughout) -- well short of variance.py's
# already-validated bootstrap engine on the SAME entries (~0.55, with a
# far more contained ROI range). Something beyond this one signal --
# most likely how much variance compounds across per-PA draws, or the
# shared-game-state correlation strength -- still needs its own
# investigation; not something one scalar here can fix alone.
EDGE_COMPOSITE_DAMPING = 0.35

_REACH_BASE_EVENTS = ("1B", "2B", "3B", "HR", "BB")
_OUT_EVENTS = ("K", "OUT")


def _apply_edge_composite(rates: dict[str, float], composite: float | None) -> dict[str, float]:
    """
    Nudges an already-blended PA-outcome distribution using the SAME
    matchup-quality signal (scoring.py's edge.composite -- platoon,
    park, weather, bullpen quality, contact quality, recent form,
    Vegas implied team total) that already drives projected_points
    everywhere else in this app. Without this, the at-bat engine had no
    way to know a batter is specifically favored or disfavored TODAY
    beyond his own season rate vs. the opposing starter's own season
    rate -- confirmed as a real, measured gap: a live 30-lineup at-bat-
    engine batch showed simulated lineup results only weakly correlated
    with their own projected_points (r=0.48, when both nominally
    describe the same thing and should track closely), so lineups the
    rest of the app ranks highest could simulate as mediocre or worse.

    A composite above 1.0 (a favorable matchup) scales every reach-base
    event UP and every out event DOWN by the same DAMPED factor (see
    EDGE_COMPOSITE_DAMPING), renormalized back to 1.0 -- the same "one
    scalar nudges the whole distribution" approximation variance.py's
    own target-percentile bias already uses for its bootstrap pool, not
    a claim that every real signal composite blends affects every event
    type equally.
    """
    if composite is None:
        return rates
    composite = max(EDGE_COMPOSITE_MIN, min(EDGE_COMPOSITE_MAX, composite))
    damped = 1.0 + (composite - 1.0) * EDGE_COMPOSITE_DAMPING
    adjusted = dict(rates)
    for event in _REACH_BASE_EVENTS:
        adjusted[event] *= damped
    for event in _OUT_EVENTS:
        adjusted[event] /= damped
    total = sum(adjusted.values())
    return {event: v / total for event, v in adjusted.items()} if total else rates


def _pitcher_id_for_side(side: dict[str, Any]) -> int | None:
    """
    The pitcher id to simulate for one side -- MLB's real probable
    pitcher if announced, or (see mlb_slate._projected_starter())
    RotoWire's own projected starter as a fallback when it hasn't been
    yet. Same confirmed-vs-projected pattern _batting_order_for_side()
    already uses for hitters.
    """
    real = (side.get("probable_pitcher") or {}).get("id")
    if real:
        return real
    return (side.get("projected_probable_pitcher") or {}).get("id")


def _cutoff(game_log: list[dict[str, Any]], as_of_date: str | None) -> list[dict[str, Any]]:
    """
    Excludes any game on or after `as_of_date` (an ISO date string) --
    for backtesting only, same purpose and mechanism as
    variance.player_outcome_pool()'s own `as_of_date`: projecting a real
    historical date's outcomes from a player's FULL current game log
    (the correct, normal live-app behavior, where "today" has no future
    games to leak) would let games that hadn't happened yet as of that
    date leak into the "prediction" -- exactly the look-ahead bias a
    real backtest against archived historical results needs to rule
    out. A no-op (returns `game_log` unchanged) when `as_of_date` is
    None, which is every real live call this app itself ever makes.
    """
    if as_of_date is None:
        return game_log
    return [g for g in game_log if (g.get("date") or "") < as_of_date]


async def _game_pa_rates(
    game: dict[str, Any],
    season: int,
    bullpen_by_team: dict[int, dict[str, Any]],
    *,
    as_of_date: str | None = None,
) -> dict[str, Any]:
    """
    Fetches and blends everything simulate_game() needs for one slate
    game: both lineups' own blended PA-outcome rates (against the
    opposing starter), both bullpens' aggregate rates, and both
    starters' outs targets. Assumes the game has already been confirmed
    simulatable (see simulate_slate_trials()) -- both sides have a
    usable (confirmed or projected) batting order and a resolvable
    probable_pitcher id.

    `as_of_date`: see _cutoff() -- backtesting only.
    """
    home, away = game["home"], game["away"]
    home_pitcher_id = _pitcher_id_for_side(home)
    away_pitcher_id = _pitcher_id_for_side(away)

    home_order = _batting_order_for_side(home)
    away_order = _batting_order_for_side(away)
    home_composites = _composites_for_side(home)
    away_composites = _composites_for_side(away)

    home_pitcher_log, away_pitcher_log = await asyncio.gather(
        mlb.get_player_game_log(home_pitcher_id, season, group="pitching"),
        mlb.get_player_game_log(away_pitcher_id, season, group="pitching"),
    )
    home_pitcher_log = _cutoff(home_pitcher_log, as_of_date)
    away_pitcher_log = _cutoff(away_pitcher_log, as_of_date)
    home_pitcher_allowed = pitcher_allowed_rates(home_pitcher_log)
    away_pitcher_allowed = pitcher_allowed_rates(away_pitcher_log)
    home_starter_outs_pool = starter_outs_pool(home_pitcher_log)
    away_starter_outs_pool = starter_outs_pool(away_pitcher_log)

    home_bullpen_rates = bullpen_pa_rates(bullpen_by_team.get(home["team_id"]))
    away_bullpen_rates = bullpen_pa_rates(bullpen_by_team.get(away["team_id"]))

    home_pitcher_pa = sum(g.get("plate_appearances") or 0 for g in home_pitcher_log)
    away_pitcher_pa = sum(g.get("plate_appearances") or 0 for g in away_pitcher_log)

    async def _hitter_rate(
        hitter_id: int,
        opposing_pitcher_rates: dict[str, float],
        opposing_pitcher_pa: int,
        composites: dict[int, float | None],
    ) -> tuple[int, dict[str, float]]:
        game_log = await mlb.get_player_game_log(hitter_id, season, group="hitting")
        game_log = _cutoff(game_log, as_of_date)
        own = pa_outcome_rates(game_log)
        batter_pa = sum(g.get("plate_appearances") or 0 for g in game_log)
        blended = blend_pa_rates(
            own, opposing_pitcher_rates, batter_pa=batter_pa, pitcher_pa=opposing_pitcher_pa
        )
        return hitter_id, _apply_edge_composite(blended, composites.get(hitter_id))

    home_rate_pairs, away_rate_pairs = await asyncio.gather(
        asyncio.gather(*(
            _hitter_rate(pid, away_pitcher_allowed, away_pitcher_pa, home_composites)
            for pid in home_order
        )),
        asyncio.gather(*(
            _hitter_rate(pid, home_pitcher_allowed, home_pitcher_pa, away_composites)
            for pid in away_order
        )),
    )

    return {
        "home_order": home_order,
        "away_order": away_order,
        "home_pa_rates": dict(home_rate_pairs),
        "away_pa_rates": dict(away_rate_pairs),
        "home_bullpen_rates": home_bullpen_rates,
        "away_bullpen_rates": away_bullpen_rates,
        "home_starter_outs_pool": home_starter_outs_pool,
        "away_starter_outs_pool": away_starter_outs_pool,
        "home_pitcher_id": home_pitcher_id,
        "away_pitcher_id": away_pitcher_id,
        # For the Vegas run anchoring in simulate_slate_trials -- the
        # market's own implied team totals, already attached to every
        # slate game.
        "home_implied_runs": home.get("implied_runs"),
        "away_implied_runs": away.get("implied_runs"),
    }


# --------------------------------------------------------------------------
# Vegas run anchoring
# --------------------------------------------------------------------------

# How many quick games each calibration round simulates, and how many
# damped correction rounds run. 300 x 5 = 1,500 extra sims per game --
# ~15% of a real 10,000-trial run, for the single most valuable
# calibration the engine gets.
_ANCHOR_TRIALS = 300
_ANCHOR_ROUNDS = 5
# Correction damping: each round multiplies a side's scale by
# (target/measured)^this. Below 1 so a single noisy round can't
# overshoot, chosen with run-scoring convexity in mind (runs respond
# super-linearly to reach-base probability, so a full-strength ratio
# correction overshoots even off a perfect measurement).
_ANCHOR_STEP = 0.5
# A team's scale is bounded so anchoring can correct genuine engine
# bias without being able to invent a cartoon offense when a market
# number disagrees wildly with the roster-level inputs.
_ANCHOR_SCALE_MIN = 0.75
_ANCHOR_SCALE_MAX = 1.35
# Close enough -- inside the market's own half-run quoting granularity.
_ANCHOR_TOLERANCE = 0.15


def _apply_run_scale(rates: dict[str, float], scale: float) -> dict[str, float]:
    """Scale every reach-base event's probability by `scale` and
    renormalize -- the same mechanism _apply_edge_composite uses, with
    a single explicit knob, so anchoring nudges HOW OFTEN a team
    reaches base without reshaping its mix of outcomes."""
    if abs(scale - 1.0) < 1e-9:
        return rates
    out = {
        e: (v * scale if e in _REACH_BASE_EVENTS else v)
        for e, v in rates.items()
    }
    total = sum(out.values())
    return {e: v / total for e, v in out.items()}


def _anchored_rates(g: dict[str, Any], seed: int | None) -> dict[str, Any]:
    """
    Calibrate one game's PA rates so each side's mean simulated runs
    match its Vegas implied total -- the review's "single most valuable
    fix", and the honest replacement for hand-guessing how hard to damp
    the composite. The field prices games off the market's numbers; a
    sim that disagrees with the market about how many runs a team
    scores disagrees with the field about every stack's value.

    Damped stochastic fixed-point iteration: each round simulates
    _ANCHOR_TRIALS quick games on its own INDEPENDENT stream, then
    multiplies each side's scale by (target/measured)^_ANCHOR_STEP.

    Independent streams per round -- deliberately NOT common random
    numbers. CRN was tried first and fails here, because a simulated
    game consumes its random stream sequentially: change one early
    plate appearance's outcome and every later draw in the game shifts
    to a different purpose, so re-running the "same" stream at a
    different scale measures a genuinely different set of games (a real
    trace showed a side's mean jumping half a run between rounds at an
    IDENTICAL scale). Fresh noise each round plus a damped step
    averages out instead of latching onto one stream's quirks.

    A side with no implied total (odds not loaded) keeps scale 1.0 and
    is left exactly as built.
    """
    home_target = g.get("home_implied_runs")
    away_target = g.get("away_implied_runs")
    if home_target is None and away_target is None:
        return g

    home_scale = away_scale = 1.0
    for round_no in range(_ANCHOR_ROUNDS):
        cal_rng = random.Random((seed or 0) * 7919 + 104729 + round_no)
        home_rates = {
            pid: _apply_run_scale(r, home_scale) for pid, r in g["home_pa_rates"].items()
        }
        away_rates = {
            pid: _apply_run_scale(r, away_scale) for pid, r in g["away_pa_rates"].items()
        }
        home_bp = _apply_run_scale(g["home_bullpen_rates"], away_scale)
        away_bp = _apply_run_scale(g["away_bullpen_rates"], home_scale)

        home_total = away_total = 0
        for _ in range(_ANCHOR_TRIALS):
            result = simulate_game(
                g["home_order"], g["away_order"], home_rates, away_rates,
                home_bp, away_bp,
                cal_rng.choice(g["home_starter_outs_pool"]),
                cal_rng.choice(g["away_starter_outs_pool"]),
                cal_rng,
            )
            home_total += result["home_runs"]
            away_total += result["away_runs"]
        home_mean = home_total / _ANCHOR_TRIALS
        away_mean = away_total / _ANCHOR_TRIALS

        done = True
        if home_target is not None and home_mean > 0:
            if abs(home_mean - home_target) > _ANCHOR_TOLERANCE:
                home_scale *= (home_target / home_mean) ** _ANCHOR_STEP
                home_scale = max(_ANCHOR_SCALE_MIN, min(_ANCHOR_SCALE_MAX, home_scale))
                done = False
        if away_target is not None and away_mean > 0:
            if abs(away_mean - away_target) > _ANCHOR_TOLERANCE:
                away_scale *= (away_target / away_mean) ** _ANCHOR_STEP
                away_scale = max(_ANCHOR_SCALE_MIN, min(_ANCHOR_SCALE_MAX, away_scale))
                done = False
        if done:
            break

    return {
        **g,
        "home_pa_rates": {pid: _apply_run_scale(r, home_scale) for pid, r in g["home_pa_rates"].items()},
        "away_pa_rates": {pid: _apply_run_scale(r, away_scale) for pid, r in g["away_pa_rates"].items()},
        "home_bullpen_rates": _apply_run_scale(g["home_bullpen_rates"], away_scale),
        "away_bullpen_rates": _apply_run_scale(g["away_bullpen_rates"], home_scale),
        "vegas_anchor_scales": {"home": round(home_scale, 3), "away": round(away_scale, 3)},
    }


async def simulate_slate_trials(
    slate: dict[str, Any],
    season: int,
    *,
    num_trials: int,
    seed: int | None = None,
    included_game_pks: list[int] | None = None,
    as_of_date: str | None = None,
) -> dict[int, list[float]]:
    """
    Real per-player DK-point arrays (length `num_trials`, one value per
    trial) for every hitter and both starting pitchers across every
    game on the slate, built by actually simulating `num_trials` full
    at-bat-level games per matchup (simulate_game()) rather than
    resampling from a precomputed pool -- correlation between teammates
    (and, within one game, between the two starters and their own
    opponent's lineup) is a natural consequence of sharing the same
    simulated game each trial, not a separately-modeled multiplier.

    V1 scope (see module docstring and SlateNotSimulatableError): EVERY
    game in `slate["games"]` marked `in_slate` must have a USABLE
    batting order on both sides -- a real confirmed lineup, or (see
    _batting_order_for_side()) RotoWire's own projected one as a
    fallback -- and a resolvable `probable_pitcher["id"]`, or this
    raises rather than silently mixing engines within a lineup.
    `included_game_pks`, if given, is authoritative -- selects exactly
    those games regardless of `in_slate` (which requires a DK salary
    CSV to have been loaded for this date; a historical/backtest date
    never has one, so this is also how a caller simulates a past date's
    real games at all, not just how live callers narrow a real slate).

    `as_of_date`: see _cutoff() -- excludes any game log entry on or
    after this date from every rate calculation, for backtesting a real
    past date without look-ahead bias (the correct, normal live-app
    behavior passes None, since "today" has no future games to leak).

    A trial index `t` means "one simulated game" for whichever game a
    given player belongs to -- consistent (and therefore correlated)
    across every player who shares a game, but NOT a meaningfully
    paired draw across two different games, since each game's own
    trials are simulated independently of every other game's, exactly
    like real MLB games on the same slate.
    """
    if included_game_pks is not None:
        games = [g for g in slate.get("games", []) if g.get("game_pk") in included_game_pks]
    else:
        games = [g for g in slate.get("games", []) if g.get("in_slate")]
    if not games:
        raise SlateNotSimulatableError("No slate games to simulate.")

    not_ready = [
        g for g in games
        if not (
            _side_ready(g["home"])
            and _side_ready(g["away"])
            and _pitcher_id_for_side(g["home"])
            and _pitcher_id_for_side(g["away"])
        )
    ]
    if not_ready:
        names = ", ".join(
            f"{g['away'].get('abbrev', '?')} @ {g['home'].get('abbrev', '?')}" for g in not_ready
        )
        raise SlateNotSimulatableError(
            "At-bat simulation needs a usable batting order (confirmed, or a projected one from "
            "an uploaded RotoWire file with at least "
            f"{MIN_PROJECTED_LINEUP_SIZE} projected starters) on both sides, and a resolvable "
            "probable pitcher (real, or RotoWire's own projected starter), for every slate game "
            f"-- not yet ready: {names}."
        )

    bullpen_by_team = await mlb.get_bullpen_stats(season)
    per_game = await asyncio.gather(
        *(_game_pa_rates(g, season, bullpen_by_team, as_of_date=as_of_date) for g in games)
    )

    player_trials: dict[int, list[float]] = {}
    rng = random.Random(seed)

    # Anchor every game's simulated run environment to the market's
    # implied totals before burning 10,000 trials on it -- see
    # _anchored_rates.
    per_game = [_anchored_rates(g, seed) for g in per_game]

    for g in per_game:
        home_ids, away_ids = g["home_order"], g["away_order"]
        for pid in home_ids + away_ids + [g["home_pitcher_id"], g["away_pitcher_id"]]:
            player_trials.setdefault(pid, [])

        for _ in range(num_trials):
            # Resampled every trial (not once per game) -- real
            # start-to-start variance in how long a starter goes, not
            # one fixed estimate every trial repeats. See
            # starter_outs_pool()'s own docstring for why this matters.
            home_starter_outs = rng.choice(g["home_starter_outs_pool"])
            away_starter_outs = rng.choice(g["away_starter_outs_pool"])
            result = simulate_game(
                home_ids, away_ids,
                g["home_pa_rates"], g["away_pa_rates"],
                g["home_bullpen_rates"], g["away_bullpen_rates"],
                home_starter_outs, away_starter_outs,
                rng,
            )
            box = result["box"]
            for pid in home_ids + away_ids:
                player_trials[pid].append(mlb_dk_points.hitter_game_points(box.get(pid, {})))
            player_trials[g["home_pitcher_id"]].append(
                mlb_dk_points.pitcher_game_points(result["home_starter_line"])
            )
            player_trials[g["away_pitcher_id"]].append(
                mlb_dk_points.pitcher_game_points(result["away_starter_line"])
            )

    # Everyone else on a simulated game's two rosters is, by this
    # slate's own batting orders, NOT starting -- so he takes no plate
    # appearances and scores nothing. That's the simulation's own
    # answer, not a fabricated stand-in: simulate_game() gives a
    # non-participant no PAs, and DK gives no PAs no points.
    #
    # This matters because the contest generator's pool is built from
    # salary and projection alone, so a cheap bench bat with a real DK
    # price and a low RotoWire projection can legally land in a lineup.
    # Without this, the whole batch was refused ("no simulated outcome
    # for player id(s) ...") over a couple of players the model was
    # already saying wouldn't play.
    #
    # Deliberately gated on the side having a usable order (which
    # _side_ready already guarantees for every game reaching this
    # point): "not in the order" then means the lineup data positively
    # says he's benched, rather than that we failed to resolve him. A
    # genuine resolution failure still surfaces as the loud error it
    # should -- that's exactly how a real bug was caught, where two
    # projected BOS starters were being dropped from the slate and the
    # engine refused rather than silently scoring them zero.
    zeros = [0.0] * num_trials
    for g in games:
        for side_key in ("home", "away"):
            side = g.get(side_key) or {}
            for hitter in side.get("hitters") or []:
                pid = hitter.get("id")
                if pid and pid not in player_trials:
                    player_trials[pid] = list(zeros)

    return player_trials


# --------------------------------------------------------------------------
# Recentering the engine's marginals on today's projection
# --------------------------------------------------------------------------
#
# Measured on a real slate (9/2, 120 players): the engine reproduces
# today's projection well in LEVEL but badly in SPREAD, and only for
# hitters.
#
#            n     proj mean   sim mean   slope   sd(proj)  sd(sim)
#   hitters  108      7.32        6.96      0.37     1.90     1.05
#   pitchers  12     15.05       14.91      1.30     3.78     6.61
#
# A hitter slope of 0.37 means the engine flattens the lineup toward
# its own middle: batting slots 1-7 all simulate BELOW their projection
# and slots 8-9 ABOVE, and the players it disagrees with most are
# exactly the best ones (Ohtani 12.06 -> 9.83). That is regression
# toward the engine's own league-average priors, and it does real
# damage downstream -- a batch of good lineups reads ~7 points light,
# and because the compression is proportional to how good a lineup is,
# corr(projected points, top-1% rate) collapses to +0.005. The engine
# is not "wrong about who is good" in a way reality supports either:
# real 9/2 lineups beat their projection by ~+15 points at EVERY
# projection quintile, so the projection-to-outcome relationship in
# reality has slope >= 1, not 0.37.
#
# The fix is the same one the bootstrap engine got: keep the engine's
# DEPENDENCE structure, replace its MARGINALS. Scaling each player's
# own trial array by a constant leaves every pairwise correlation
# exactly unchanged (Pearson correlation is scale-invariant), so this
# preserves the entire reason to run an at-bat engine at all -- a big
# inning still lifts consecutive hitters together, the lineup still
# turns over, the starter is still anti-correlated with the offence he
# faced -- while taking the levels from the source that actually knows
# today (park, weather, platoon, recent form, confirmed batting order).
#
# That is exactly the copula split already used elsewhere here:
# marginals from projections, dependence from the simulation.
#
# Deliberately NOT applied inside simulate_slate_trials(): backtesting
# the engine against real outcomes (scripts/backtest_atbat_engine.py)
# has to see the engine's own unaided opinion, or it would be grading
# the projections instead.
_RECENTER_SCALE_MIN = 0.4
_RECENTER_SCALE_MAX = 2.5
_RECENTER_MIN_MEAN = 0.5

# WHY THIS IS A REGRESSION FIT AND NOT A PER-PLAYER PIN
#
# The first version of this scaled every player's array so his own mean
# landed exactly on his own projection. That removed the compression,
# but it also removed everything else: if every player's mean IS his
# projection, then a lineup's simulated mean is the sum of its
# projections, and corr(projected points, simulated mean) is 1.000 by
# construction. Measured at exactly that, across 108 lineups. At that
# point the engine is a deterministic function of its input and there
# is no reason to run it -- the bootstrap engine is far cheaper and
# gives the same answer.
#
# The compression has two parts and only one of them is wrong:
#
#   systematic   the group-wide slope (0.37 for hitters), which says
#                the engine marks good players down and weak players
#                up. Reality does not support it: real lineups beat
#                their projection by ~+15 points at EVERY projection
#                quintile, flat, so the real slope is >= 1.
#
#   idiosyncratic this player's own departure from that line, which is
#                what actually came out of simulating his plate
#                appearances against today's pitcher. That is real
#                information the projection does not contain.
#
# Fitting the line per group and re-centring on it removes the first
# and keeps the second. A player the engine likes more than the line
# expects still simulates above his projection afterwards.
#
# _RESIDUAL_TRUST scales how much of that disagreement survives. 1.0
# keeps it whole, which is the honest default: the residual is a
# measurement, not a guess, and damping it would be a second opinion
# about an opinion. Lower it only if the residuals are ever shown to be
# noise rather than signal.
_RESIDUAL_TRUST = 1.0

# Below this many players in a group there is no slope worth fitting,
# and the fallback pins on the projection instead.
_RECENTER_MIN_SAMPLE = 8


def recenter_trials_on_projections(
    player_trials: dict[int, list[float]],
    slate: dict[str, Any],
) -> dict[int, list[float]]:
    """
    Rescale each player's simulated trial array so its mean equals
    today's projected fantasy points, preserving shape and every
    cross-player correlation. Players with no projection, or whose
    simulated mean is too small to rescale meaningfully, pass through
    untouched.
    """
    projections: dict[int, float] = {}
    groups: dict[int, str] = {}
    for game in slate.get("games", []):
        for side_key in ("home", "away"):
            side = game.get(side_key) or {}
            people = [(p, "H") for p in (side.get("hitters") or []) if p]
            starter = side.get("probable_pitcher")
            if starter:
                people.append((starter, "P"))
            for person, group in people:
                pid = person.get("id")
                fpts = (person.get("projection") or {}).get("fpts")
                if pid is not None and fpts:
                    projections[pid] = float(fpts)
                    groups[pid] = group

    # Fit the systematic part per GROUP. Hitters and pitchers sit on
    # completely different lines -- measured slope 0.37 for hitters
    # against 1.30 for pitchers -- so one fit across both would correct
    # neither.
    fits: dict[str, tuple[float, float]] = {}
    for group in ("H", "P"):
        pairs = [
            (projections[pid], sum(t) / len(t))
            for pid, t in player_trials.items()
            if t and groups.get(pid) == group and pid in projections
            and sum(t) / len(t) >= _RECENTER_MIN_MEAN
        ]
        if len(pairs) < _RECENTER_MIN_SAMPLE:
            continue
        n = len(pairs)
        mx = sum(x for x, _ in pairs) / n
        my = sum(y for _, y in pairs) / n
        sxx = sum((x - mx) ** 2 for x, _ in pairs)
        if sxx <= 0:
            continue
        beta = sum((x - mx) * (y - my) for x, y in pairs) / sxx
        fits[group] = (my - beta * mx, beta)

    recentered: dict[int, list[float]] = {}
    for pid, trials in player_trials.items():
        projection = projections.get(pid)
        if not trials or not projection or projection <= 0:
            recentered[pid] = trials
            continue
        mean = sum(trials) / len(trials)
        if mean < _RECENTER_MIN_MEAN:
            # Nothing meaningful to rescale -- multiplying ~zeros by any
            # factor still cannot reach a projection, and the implied
            # scale would be enormous.
            recentered[pid] = trials
            continue

        fit = fits.get(groups.get(pid, ""))
        if fit is None:
            # Too few players in this group to fit a line. Fall back to
            # pinning this one player on his projection: cruder, but a
            # slate this thin has no systematic slope to estimate.
            target = projection
        else:
            alpha, beta = fit
            # The engine's own disagreement with the projection for THIS
            # player, measured against the group's fitted line rather
            # than against the projection itself. Keeping it is the
            # whole point: it is the part that came from simulating his
            # plate appearances, and it is what makes this engine worth
            # running instead of the bootstrap one.
            residual = mean - (alpha + beta * projection)
            target = projection + _RESIDUAL_TRUST * residual

        if target <= 0:
            recentered[pid] = trials
            continue
        scale = min(_RECENTER_SCALE_MAX, max(_RECENTER_SCALE_MIN, target / mean))
        recentered[pid] = [value * scale for value in trials]
    return recentered
