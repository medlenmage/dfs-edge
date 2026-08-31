"""
Offline test of the season-long NFL side -- draft board maths, VORP,
tiers, roster filling and the live draft assistant, all against
synthetic pools with no network.

Kept separate from the DFS suites for the same reason the code is: this
subsystem shares only player_match.py with them.

Run it with:
    cd backend
    .venv/Scripts/python.exe -m tests.test_season_long
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.clients import dk_bestball, sleeper  # noqa: E402
from app.services import draft_assistant, season_long  # noqa: E402

PASS, FAILED = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAILED).append(name)
    mark = "PASS" if condition else "FAIL"
    print(f"  {mark}  {name}" + (f"  -- {detail}" if detail else ""))


# --------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------

STANDARD_LEAGUE = {
    "name": "Test Redraft",
    "total_rosters": 10,
    "roster_positions": ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "K", "DEF"]
    + ["BN"] * 6,
    "scoring_settings": {"rec": 1.0},
}

SUPERFLEX_HALF_PPR = {
    "name": "Test Superflex",
    "total_rosters": 12,
    "roster_positions": ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "SUPER_FLEX"]
    + ["BN"] * 8,
    "scoring_settings": {"rec": 0.5},
}


def _dk_row(rank, name, pos, team, proj, bye=7):
    return {
        "dk_id": 1000 + rank,
        "name": name,
        "position": pos,
        "team": team,
        "board_rank": rank,
        "dk_projection": proj,
        "bye_week": bye,
        "status": None,
        "news_status": None,
    }


def _synthetic_board():
    """A small but realistically shaped pool: a steep top at each
    position flattening into a long interchangeable tail."""
    rows, rank = [], 0
    for pos, top, count in (("QB", 24.0, 30), ("RB", 25.0, 60), ("WR", 25.0, 70), ("TE", 19.0, 30)):
        for i in range(count):
            rank += 1
            # Steep for the first handful, then a shallow tail -- the
            # shape that makes tiering a real question.
            proj = round(top - (i**0.75) * 1.6, 1)
            rows.append(
                _dk_row(rank, f"{pos}{i}", pos, f"T{i % 32}", max(proj, 1.0), bye=(i % 14) + 4)
            )
    return rows


SLEEPER_PLAYERS = {
    "s1": {
        "player_id": "s1",
        "full_name": "RB0",
        "position": "RB",
        "team": "T0",
        "search_rank": 1,
        "age": 24,
        "injury_status": None,
        "years_exp": 3,
        "depth_chart_order": 1,
    },
    "s2": {
        "player_id": "s2",
        "full_name": "WR0",
        "position": "WR",
        "team": "T0",
        "search_rank": 3,
        "age": 26,
        "injury_status": "Questionable",
        "years_exp": 5,
        "depth_chart_order": 1,
    },
    # Same normalized name and position as the real WR0, but unranked --
    # the merge must prefer the ranked one.
    "s3": {
        "player_id": "s3",
        "full_name": "wr0",
        "position": "WR",
        "team": "T9",
        "search_rank": None,
        "age": 22,
        "injury_status": None,
        "years_exp": 0,
        "depth_chart_order": 5,
    },
}


def _build(league):
    """The pure half of build_board(), with the two network calls' worth
    of data supplied directly."""
    rows = season_long._merge_sources(_synthetic_board(), SLEEPER_PLAYERS)
    season_long._consensus_rank(rows)
    shape = season_long.league_shape(league)
    by_pos = {}
    for r in rows:
        by_pos.setdefault(r["position"], []).append(r)
    for pool in by_pos.values():
        pool.sort(key=lambda p: (p.get("projection") is None, -(p.get("projection") or 0)))
        for i, p in enumerate(pool, start=1):
            p["position_rank"] = i
        season_long._assign_tiers(pool)
    levels = season_long._replacement_levels(by_pos, shape)
    for pos, pool in by_pos.items():
        base = levels[pos]["replacement_points"]
        for p in pool:
            p["vorp"] = round(p["projection"] - base, 2)
    rows.sort(key=lambda p: -p["vorp"])
    for i, r in enumerate(rows, start=1):
        r["overall_rank"] = i
    return {"players": rows, "positions": levels, "shape": shape}, by_pos


def main() -> int:
    print("Season-long NFL test (offline, no network)\n" + "=" * 60)

    # ----------------------------------------------------------------
    # league shape
    # ----------------------------------------------------------------
    print("\nleague shape")
    std = season_long.league_shape(STANDARD_LEAGUE)
    check(
        "roster_positions parses into starters, flex and bench correctly",
        std["teams"] == 10
        and std["starters"] == {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "K": 1, "DST": 1}
        and std["flex_slots"] == 1
        and std["superflex_slots"] == 0,
        f"{std['starters']} flex={std['flex_slots']}",
    )
    check("Sleeper's DEF is canonicalised to DST", season_long.canonical_position("DEF") == "DST")

    sf = season_long.league_shape(SUPERFLEX_HALF_PPR)
    check(
        "SUPER_FLEX is counted separately from a plain FLEX",
        sf["flex_slots"] == 1 and sf["superflex_slots"] == 1,
        f"flex={sf['flex_slots']} superflex={sf['superflex_slots']}",
    )

    none_shape = season_long.league_shape(None)
    check(
        "no league falls back to a standard 12-team shape AND flags it as assumed",
        none_shape["teams"] == 12 and none_shape["assumed"] is True,
    )

    # ----------------------------------------------------------------
    # scoring honesty
    # ----------------------------------------------------------------
    print("\nscoring mismatch is reported, not silently converted")
    check(
        "a full-PPR league produces no warning (DK's projections already match)",
        season_long.scoring_warning(std) is None,
    )
    warn = season_long.scoring_warning(sf)
    check(
        "a half-PPR league is warned that the projections are full PPR",
        warn is not None and "half-PPR" in warn,
        (warn or "")[:60],
    )
    check(
        "an ASSUMED league is not warned -- we don't know its scoring, so claiming a "
        "mismatch would be a guess",
        season_long.scoring_warning(none_shape) is None,
    )

    # ----------------------------------------------------------------
    # merging two sources
    # ----------------------------------------------------------------
    print("\nmerging DraftKings and Sleeper")
    board, by_pos = _build(STANDARD_LEAGUE)
    rb0 = next(p for p in board["players"] if p["name"] == "RB0")
    wr0 = next(p for p in board["players"] if p["name"] == "WR0")
    check(
        "a DK player matched to Sleeper carries Sleeper's context",
        rb0["sleeper_id"] == "s1" and rb0["age"] == 24,
        f"sleeper_id={rb0['sleeper_id']} age={rb0['age']}",
    )
    check(
        "duplicate normalized names resolve to the RANKED player, not a namesake",
        wr0["sleeper_id"] == "s2" and wr0["injury_status"] == "Questionable",
        f"sleeper_id={wr0['sleeper_id']}",
    )
    unmatched = next(p for p in board["players"] if p["name"] == "TE5")
    check(
        "a DK player with no Sleeper match still appears -- DK is the authority on "
        "who is draftable",
        unmatched["sleeper_id"] is None and unmatched["projection"] is not None,
    )
    check(
        "consensus_rank averages the two sources where both rank a player",
        rb0["consensus_rank"] == round((rb0["dk_board_rank"] + 1) / 2, 1),
        f"{rb0['consensus_rank']} from dk={rb0['dk_board_rank']} sleeper=1",
    )
    check(
        "consensus_rank falls back to the single available source",
        unmatched["consensus_rank"] == unmatched["dk_board_rank"],
    )

    # ----------------------------------------------------------------
    # replacement level and VORP
    # ----------------------------------------------------------------
    print("\nreplacement level and VORP")
    lv = board["positions"]
    check(
        "base starters are teams x starting slots before flex is allocated",
        lv["QB"]["starters_league_wide"] == 10,
        f"QB cutoff {lv['QB']['starters_league_wide']}",
    )
    flex_extra = (
        lv["RB"]["starters_league_wide"]
        + lv["WR"]["starters_league_wide"]
        + lv["TE"]["starters_league_wide"]
    ) - (10 * 2 + 10 * 3 + 10 * 1)
    check(
        "every flex slot in the league is allocated to exactly one flex-eligible position",
        flex_extra == 10,
        f"{flex_extra} flex slots distributed",
    )
    check(
        "flex slots go to the position with the best player still unclaimed, not a "
        "hardcoded split",
        lv["RB"]["starters_league_wide"] > 20 or lv["WR"]["starters_league_wide"] > 30,
        f"RB {lv['RB']['starters_league_wide']}, WR {lv['WR']['starters_league_wide']}, "
        f"TE {lv['TE']['starters_league_wide']}",
    )
    repl_rb = by_pos["RB"][lv["RB"]["starters_league_wide"]]
    check(
        "VORP is measured against the first player PAST the starter cutoff",
        abs(repl_rb["vorp"]) < 0.001,
        f"replacement RB {repl_rb['name']} has vorp {repl_rb['vorp']}",
    )
    check(
        "the replacement player himself is worth zero, and everyone below him negative",
        by_pos["RB"][lv["RB"]["starters_league_wide"] + 1]["vorp"] < 0,
    )

    # The whole reason VORP exists: it makes positions comparable. A
    # league starting one QB and three WRs burns through far less of the
    # QB pool, so QB replacement level sits much higher -- and a
    # quarterback scoring the same raw points as a receiver is therefore
    # worth much LESS, because the QB you could have had for free is
    # nearly as good. (This is the real 2026 board's shape too: QB
    # replacement 18.7 against WR replacement 10.8.)
    top_qb = by_pos["QB"][0]
    same_proj_wr = min(by_pos["WR"], key=lambda p: abs(p["projection"] - top_qb["projection"]))
    check(
        "at equal raw projection a player at a SHALLOW-demand position is worth less "
        "than one at a deep-demand position -- the entire point of VORP",
        top_qb["vorp"] < same_proj_wr["vorp"],
        f"QB {top_qb['projection']} -> {top_qb['vorp']} vs WR "
        f"{same_proj_wr['projection']} -> {same_proj_wr['vorp']}",
    )
    check(
        "and that gap is driven by replacement level, not by the projections",
        lv["QB"]["replacement_points"] > lv["WR"]["replacement_points"],
        f"QB repl {lv['QB']['replacement_points']} vs WR {lv['WR']['replacement_points']}",
    )

    # A superflex league must make quarterbacks meaningfully more valuable.
    sf_board, sf_by_pos = _build(SUPERFLEX_HALF_PPR)
    check(
        "superflex raises the QB starter cutoff well past one per team",
        sf_board["positions"]["QB"]["starters_league_wide"] > 12,
        f"{sf_board['positions']['QB']['starters_league_wide']} QBs started in a 12-team superflex",
    )
    check(
        "and therefore lowers QB replacement level, raising every QB's VORP",
        sf_by_pos["QB"][0]["vorp"] > by_pos["QB"][0]["vorp"],
        f"superflex {sf_by_pos['QB'][0]['vorp']} vs 1QB {by_pos['QB'][0]['vorp']}",
    )

    # ----------------------------------------------------------------
    # tiers
    # ----------------------------------------------------------------
    print("\ntiers")
    rb_tiers = [p["tier"] for p in by_pos["RB"][: season_long._TIER_SAMPLE_DEPTH]]
    check(
        "tiers are monotonically non-decreasing down a position's ranking",
        all(b >= a for a, b in zip(rb_tiers, rb_tiers[1:])),
    )
    check(
        "the sampled depth is cut into exactly _TIER_COUNT tiers, not one per player",
        max(rb_tiers) == season_long._TIER_COUNT,
        f"{max(rb_tiers)} tiers over {len(rb_tiers)} RBs",
    )
    check(
        "the steepest part of the board gets the tier breaks -- tier 1 is small",
        rb_tiers.count(1) < rb_tiers.count(max(rb_tiers)),
        f"tier1 has {rb_tiers.count(1)}, last tier has {rb_tiers.count(max(rb_tiers))}",
    )
    check(
        "everything past the sampled depth collapses into one trailing tier rather "
        "than pretending the tail is differentiated",
        len({p["tier"] for p in by_pos["RB"][season_long._TIER_SAMPLE_DEPTH :]}) == 1,
    )

    # ----------------------------------------------------------------
    # lineup filling
    # ----------------------------------------------------------------
    print("\nbest starting lineup")
    roster = [by_pos["QB"][0], by_pos["TE"][0], by_pos["TE"][1]] + by_pos["RB"][:3] + by_pos["WR"][:4]
    filled = season_long._fill_lineup(roster, std)
    slots = sorted(p["slot"] for p in filled["starters"])
    check(
        "every startable slot the roster can fill is filled, and none twice",
        slots == ["FLEX", "QB", "RB", "RB", "TE", "WR", "WR", "WR"],
        str(slots),
    )
    check(
        "no player is started in two slots at once",
        len({p["name"] for p in filled["starters"]}) == len(filled["starters"]),
    )
    flex = next(p for p in filled["starters"] if p["slot"] == "FLEX")
    leftovers = [p for p in roster if p["name"] not in {s["name"] for s in filled["starters"]}]
    check(
        "the flex takes the best remaining eligible player, not an arbitrary one",
        all(flex["projection"] >= p["projection"] for p in leftovers if p["position"] in season_long.FLEX_POSITIONS),
        f"flex={flex['name']} {flex['projection']}",
    )
    check(
        "a roster with no kicker or defense still produces a legal partial lineup "
        "rather than failing",
        "K" not in slots and "DST" not in slots and filled["starting_points"] > 0,
    )

    # ----------------------------------------------------------------
    # draft assistant -- snake maths
    # ----------------------------------------------------------------
    print("\ndraft snake maths")
    check(
        "odd rounds run forward from slot 1",
        [draft_assistant._pick_number(1, s, 10, True) for s in (1, 5, 10)] == [1, 5, 10],
    )
    check(
        "even rounds reverse -- slot 1 picks last",
        [draft_assistant._pick_number(2, s, 10, True) for s in (1, 5, 10)] == [20, 16, 11],
        str([draft_assistant._pick_number(2, s, 10, True) for s in (1, 5, 10)]),
    )
    check(
        "a linear (non-snake) draft does not reverse",
        [draft_assistant._pick_number(2, s, 10, False) for s in (1, 10)] == [11, 20],
    )
    check(
        "the turn is the real cost of an early slot: pick 1 waits 18 picks, pick 10 waits 1",
        (draft_assistant._pick_number(2, 1, 10, True) - draft_assistant._pick_number(1, 1, 10, True))
        == 19
        and (
            draft_assistant._pick_number(2, 10, 10, True)
            - draft_assistant._pick_number(1, 10, 10, True)
        )
        == 1,
    )

    # ----------------------------------------------------------------
    # draft assistant -- need, scarcity, suggestions
    # ----------------------------------------------------------------
    print("\ndraft advice")
    empty_need = draft_assistant._need_scores({}, std)
    check(
        "an empty roster needs every starting position fully",
        empty_need["RB"] == 1.0 and empty_need["WR"] == 1.0 and empty_need["QB"] == 1.0,
    )
    partial = draft_assistant._need_scores({"RB": 1, "WR": 3, "QB": 1, "TE": 1}, std)
    check(
        "a half-filled position reads partial need",
        partial["RB"] == 0.5,
        f"RB need {partial['RB']}",
    )
    check(
        "a position whose starters are full still reads some need while the flex is open",
        0 < partial["WR"] < 1.0,
        f"WR need {partial['WR']}",
    )
    check(
        "a position with no starting slot open and no flex claim reads zero need",
        draft_assistant._need_scores({"QB": 1}, std)["QB"] == 0.0,
    )

    structural = draft_assistant._structural_rate(std)
    check(
        "the structural rate falls out of the league's own starting requirements -- "
        "3 WR starters draw more picks than 1 TE starter",
        structural["WR"] > structural["RB"] > structural["TE"],
        {k: round(v, 3) for k, v in structural.items()},
    )
    check(
        "structural rates are a distribution over positions",
        abs(sum(structural.values()) - 1.0) < 1e-9,
    )

    run_picks = [{"position": "TE"}] * 6 + [{"position": "WR"}] * 2
    rate = draft_assistant._position_run_rate(run_picks, 4, std)
    check(
        "a positional run in THIS room pushes that position's rate above its "
        "structural share",
        rate["TE"] > structural["TE"] and rate["TE"] > rate["WR"],
        f"TE {rate['TE']:.2f} (structural {structural['TE']:.2f}) vs WR {rate['WR']:.2f}",
    )
    empty_rate = draft_assistant._position_run_rate([], 10, std)
    check(
        "before any pick is made the rate IS the structural share -- a zero-evidence "
        "reading of 'no receiver will be taken' would be nonsense",
        empty_rate == structural,
    )
    thin = draft_assistant._position_run_rate([{"position": "RB"}] * 3, 10, std)
    check(
        "three picks of evidence barely move the rate; a position unseen so far still "
        "reads a real chance of going",
        thin["WR"] > structural["WR"] * 0.6 and thin["RB"] > structural["RB"],
        f"after 3 RB picks: RB {thin['RB']:.2f}, WR {thin['WR']:.2f}",
    )
    check(
        "expected losses before your turn scale with how long you wait",
        draft_assistant._expected_gone("TE", rate, 20)
        > draft_assistant._expected_gone("TE", rate, 5),
    )

    # Two identical-value players, one at a position of real need.
    state = {
        "picks": [],
        "teams": 10,
        "picks_until_my_turn": 12,
        "my_next_pick": 13,
    }
    board_for_advice = {
        "players": [p for p in board["players"] if p["vorp"] is not None],
        "shape": std,
    }
    advice = draft_assistant.suggest(board_for_advice, state, "me", limit=200)
    top = advice["suggestions"][0]
    check(
        "with an empty roster the top suggestion is a real fantasy starter, not a kicker",
        top["position"] in ("RB", "WR", "QB", "TE") and top["vorp"] > 0,
        f"{top['name']} ({top['position']}) vorp {top['vorp']}",
    )
    check(
        "every suggestion shows its work rather than a bare score",
        set(top["why"]) >= {"vorp", "fills_need", "scarcity_bonus", "same_tier_remaining"},
    )

    # A drafted player must disappear from the board.
    taken = advice["suggestions"][0]
    state_after = {
        **state,
        "picks": [
            {
                "player_id": taken["sleeper_id"],
                "name": taken["name"],
                "position": taken["position"],
                "picked_by": "someone_else",
            }
        ],
    }
    after = draft_assistant.suggest(board_for_advice, state_after, "me", limit=200)
    check(
        "a player already drafted is removed from the suggestions -- including one with "
        "no Sleeper id, matched by name",
        all(s["name"] != taken["name"] for s in after["suggestions"]),
        f"removed {taken['name']}",
    )

    # Need must actually change the advice.
    rb_heavy = {
        **state,
        "picks": [
            {"player_id": None, "name": f"RB{i}", "position": "RB", "picked_by": "me"}
            for i in range(2)
        ]
        + [
            {"player_id": None, "name": f"QB{i}", "position": "QB", "picked_by": "me"}
            for i in range(1)
        ],
    }
    rb_heavy_advice = draft_assistant.suggest(board_for_advice, rb_heavy, "me", limit=200)
    check(
        "after filling RB and QB the assistant pivots to the unfilled positions",
        rb_heavy_advice["suggestions"][0]["position"] in ("WR", "TE"),
        f"suggests {rb_heavy_advice['suggestions'][0]['name']} "
        f"({rb_heavy_advice['suggestions'][0]['position']})",
    )
    check(
        "roster counts and remaining needs are reported back so the advice is auditable",
        rb_heavy_advice["roster_counts"]["RB"] == 2
        and "WR" in rb_heavy_advice["needs"]
        and "QB" not in rb_heavy_advice["needs"],
        str(rb_heavy_advice["needs"]),
    )

    # The same player is worth more when a run is emptying his position.
    calm = draft_assistant.suggest(
        board_for_advice, {**state, "picks": []}, "me", limit=500
    )
    run_state = {
        **state,
        "picks": [
            {"player_id": None, "name": f"TEx{i}", "position": "TE", "picked_by": "other"}
            for i in range(18)
        ],
    }
    running = draft_assistant.suggest(board_for_advice, run_state, "me", limit=500)

    def _score(res, pos):
        return next(s["why"]["scarcity_bonus"] for s in res["suggestions"] if s["position"] == pos)

    check(
        "a positional run raises the scarcity pressure on that position's remaining players",
        _score(running, "TE") > _score(calm, "TE"),
        f"during a TE run {_score(running, 'TE')} vs calm {_score(calm, 'TE')}",
    )
    check(
        "a run at ONE position does not inflate scarcity everywhere",
        _score(running, "WR") <= _score(calm, "WR"),
        f"WR during the TE run {_score(running, 'WR')} vs calm {_score(calm, 'WR')}",
    )

    # Scarcity has to measure what you'd actually lose, not just how
    # likely the player is to disappear -- otherwise it says "reach" for
    # every player at a deep position.
    def _row(res, name):
        return next(s for s in res["suggestions"] if s["name"] == name)

    cliff = _row(calm, "RB0")["why"]
    flat = _row(calm, "RB25")["why"]
    check(
        "the reasoning names the player you'd actually fall back to if you wait",
        cliff["fallback_if_you_wait"].startswith("RB")
        and flat["fallback_if_you_wait"].startswith("RB"),
        f"RB0 -> {cliff['fallback_if_you_wait']}, RB25 -> {flat['fallback_if_you_wait']}",
    )
    check(
        "at the same position and the same wait, passing costs far more at the steep "
        "top of the board than in its flat middle",
        cliff["value_lost_if_you_wait"] > 2 * flat["value_lost_if_you_wait"],
        f"RB0 loses {cliff['value_lost_if_you_wait']}, RB25 loses {flat['value_lost_if_you_wait']}",
    )
    # And separately: the faster a position is going, the further you
    # fall while you wait. TE goes slowly here (one starter per team),
    # WR quickly (three), so an equally-ranked WR slides further.
    check(
        "a position the room is burning through faster costs more to wait on",
        _row(calm, "WR10")["why"]["expected_gone_before_your_pick"]
        > _row(calm, "TE10")["why"]["expected_gone_before_your_pick"],
        f"WR {_row(calm, 'WR10')['why']['expected_gone_before_your_pick']} vs "
        f"TE {_row(calm, 'TE10')['why']['expected_gone_before_your_pick']}",
    )
    check(
        "the chance a player is gone falls the further down his position he sits",
        _row(calm, "RB0")["why"]["chance_he_is_gone"]
        > _row(calm, "RB30")["why"]["chance_he_is_gone"],
        f"RB0 {_row(calm, 'RB0')['why']['chance_he_is_gone']} vs "
        f"RB30 {_row(calm, 'RB30')['why']['chance_he_is_gone']}",
    )
    check(
        "with no wait at all there is no scarcity pressure on anyone",
        all(
            s["why"]["scarcity_bonus"] == 0
            for s in draft_assistant.suggest(
                board_for_advice, {**state, "picks_until_my_turn": 0}, "me", limit=50
            )["suggestions"]
        ),
    )

    # Bye pileups.
    bye_state = {
        **state,
        "picks": [
            {"player_id": p["sleeper_id"], "name": p["name"], "position": p["position"], "picked_by": "me"}
            for p in [x for x in board["players"] if x.get("bye_week") == 4][:2]
        ],
    }
    bye_advice = draft_assistant.suggest(board_for_advice, bye_state, "me", limit=500)
    flagged = [
        s for s in bye_advice["suggestions"] if any("week 4 bye" in f for f in s["flags"])
    ]
    check(
        "a third starter on the same bye week is flagged",
        len(flagged) > 0,
        f"{len(flagged)} players flagged for the week 4 pileup",
    )
    check(
        "two on a bye is NOT flagged -- that is normal and nagging about it is noise",
        not any(
            "week 4 bye" in f
            for s in draft_assistant.suggest(
                board_for_advice,
                {**state, "picks": bye_state["picks"][:1]},
                "me",
                limit=500,
            )["suggestions"]
            for f in s["flags"]
        ),
    )

    # ----------------------------------------------------------------
    # end-to-end wiring, with the two network clients stubbed
    # ----------------------------------------------------------------
    # Everything above exercises the maths in isolation. This drives the
    # two functions the HTTP routes actually call, all the way through,
    # so a shape mismatch between the board and the league/draft layers
    # is caught here rather than in front of a live draft.
    print("\nend-to-end (stubbed network)")
    asyncio.run(_wiring_checks())

    print("\n" + "=" * 60)
    print(f"{len(PASS)} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  FAILED: {name}")
    print()
    return 1 if FAILED else 0


# --------------------------------------------------------------------
# stubs for the end-to-end pass
# --------------------------------------------------------------------

_STUB_LEAGUE = {
    "league_id": "L1",
    "name": "Wiring League",
    "season": "2026",
    "status": "in_season",
    "total_rosters": 3,
    "roster_positions": ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "K", "DEF"]
    + ["BN"] * 6,
    "scoring_settings": {"rec": 0.5},
}

_STUB_SLEEPER_PLAYERS = {
    f"s{i}": {
        "player_id": f"s{i}",
        "full_name": name,
        "position": pos,
        "team": team,
        "search_rank": i,
        "age": 25,
        "years_exp": 3,
        "injury_status": "Questionable" if i == 2 else None,
        "depth_chart_order": 1,
    }
    for i, (name, pos, team) in enumerate(
        [
            ("Alpha Back", "RB", "ATL"),
            ("Bravo Wide", "WR", "BUF"),
            ("Charlie Tight", "TE", "CHI"),
            ("Delta Quarter", "QB", "DAL"),
            ("Echo Back", "RB", "DET"),
            ("Foxtrot Wide", "WR", "GB"),
            ("Golf Wide", "WR", "HOU"),
            ("Hotel Tight", "TE", "IND"),
            ("India Quarter", "QB", "JAX"),
            ("Juliet Back", "RB", "KC"),
            ("Kilo Wide", "WR", "LV"),
            ("Lima Back", "RB", "MIA"),
        ],
        start=1,
    )
}

_STUB_DK_BOARD = [
    {
        "dk_id": 100 + i,
        "name": rec["full_name"],
        "position": rec["position"],
        "team": rec["team"],
        "board_rank": i,
        "dk_projection": round(26 - i * 1.7, 1),
        "bye_week": 7 if i % 3 else 9,
        "status": None,
        "news_status": None,
    }
    for i, rec in enumerate(_STUB_SLEEPER_PLAYERS.values(), start=1)
]

_STUB_ROSTERS = [
    {
        "roster_id": 1,
        "owner_id": "u1",
        "players": ["s1", "s2", "s3", "s4"],
        "settings": {"wins": 2, "losses": 1},
    },
    {
        "roster_id": 2,
        "owner_id": "u2",
        "players": ["s5", "s6", "s7"],
        "settings": {"wins": 1, "losses": 2},
    },
    {"roster_id": 3, "owner_id": None, "players": [], "settings": {}},
]

_STUB_USERS = [
    {"user_id": "u1", "display_name": "Me"},
    {"user_id": "u2", "display_name": "Rival"},
]

# Slot 2 of a 3-team snake with two picks already in. The next pick
# overall is 3; this user's own next turn is pick 5, because round 2
# runs backwards.
_STUB_DRAFT = {
    "draft_id": "D1",
    "status": "drafting",
    "type": "snake",
    "settings": {"teams": 3, "rounds": 4},
    "draft_order": {"u1": 2},
}
_STUB_PICKS = [
    {
        "pick_no": 1,
        "round": 1,
        "roster_id": 3,
        "picked_by": "u3",
        "player_id": "s1",
        "metadata": {
            "first_name": "Alpha",
            "last_name": "Back",
            "position": "RB",
            "team": "ATL",
        },
    },
    {
        "pick_no": 2,
        "round": 1,
        "roster_id": 1,
        "picked_by": "u1",
        "player_id": "s4",
        "metadata": {
            "first_name": "Delta",
            "last_name": "Quarter",
            "position": "QB",
            "team": "DAL",
        },
    },
]


def _resolved(value):
    async def _f():
        return value

    return _f()


def _install_stubs() -> None:
    sleeper.get_league = lambda lid, **k: _resolved(_STUB_LEAGUE)
    sleeper.get_rosters = lambda lid, **k: _resolved(_STUB_ROSTERS)
    sleeper.get_league_users = lambda lid, **k: _resolved(_STUB_USERS)
    sleeper.get_players = lambda **k: _resolved(_STUB_SLEEPER_PLAYERS)
    sleeper.get_draft = lambda did, **k: _resolved(_STUB_DRAFT)
    sleeper.get_draft_picks = lambda did, **k: _resolved(_STUB_PICKS)
    dk_bestball.get_best_ball_groups = lambda **k: _resolved(
        [{"draft_group_id": 1, "label": "stub"}]
    )
    dk_bestball.get_board = lambda gid, **k: _resolved(_STUB_DK_BOARD)


async def _wiring_checks() -> None:
    _install_stubs()

    analysis = await season_long.analyze_league("L1", "u1")
    teams = analysis["teams"]
    check(
        "analyze_league values every roster and power-ranks them by projected starting points",
        [t["power_rank"] for t in teams] == [1, 2, 3]
        and teams[0]["starting_points"] > teams[1]["starting_points"],
        str([(t["owner"], t["starting_points"]) for t in teams]),
    )
    check(
        "the connected manager's own team is identified",
        bool(analysis["me"]) and analysis["me"]["is_me"] and analysis["me"]["owner"] == "Me",
    )
    check(
        "an unclaimed roster is handled rather than crashing on a null owner",
        teams[-1]["owner"] == "unclaimed" and teams[-1]["starting_points"] == 0,
    )
    check(
        "per-position ranks cover every team, so 'my receivers are 2nd of 3' is sayable",
        all(set(t["position_rank"]) >= {"QB", "RB", "WR", "TE"} for t in teams),
    )
    check(
        "an injury on a roster is surfaced",
        len(analysis["me"]["injuries"]) == 1
        and analysis["me"]["injuries"][0]["status"] == "Questionable",
    )
    rostered = {pid for r in _STUB_ROSTERS for pid in r["players"]}
    free = analysis["free_agents"]
    check(
        "free agents are the unrostered players only, best first",
        bool(free)
        and all(p["sleeper_id"] not in rostered for p in free)
        and free == sorted(free, key=lambda p: -p["vorp"]),
        f"{len(free)} available, top {free[0]['name']}",
    )
    check(
        "the league's half-PPR scoring is carried through to the analysis, not just the board",
        bool(analysis["scoring_warning"]),
    )

    live = await draft_assistant.live("D1", "u1", league=_STUB_LEAGUE)
    st = live["state"]
    check(
        "live() reads the draft's real position in a snake -- slot 2 of 3, two picks in, "
        "own next turn at pick 5",
        (st["my_slot"], st["on_the_clock_pick"], st["my_next_pick"], st["picks_until_my_turn"])
        == (2, 3, 5, 2),
        f"slot {st['my_slot']}, on clock {st['on_the_clock_pick']}, "
        f"mine {st['my_next_pick']}, wait {st['picks_until_my_turn']}",
    )
    check(
        "only this manager's own picks count toward their roster",
        live["roster_counts"] == {"QB": 1},
        str(live["roster_counts"]),
    )
    drafted = {p["player_id"] for p in _STUB_PICKS}
    check(
        "no already-drafted player is ever suggested",
        all(s["sleeper_id"] not in drafted for s in live["suggestions"]),
    )
    check(
        "the board's metadata rides along without the full player list bloating every poll",
        "players" not in live["board_meta"] and "shape" in live["board_meta"],
    )


if __name__ == "__main__":
    raise SystemExit(main())
