"""
Offline tests for the process layer: data/process_rules.py,
services/contest_audit.py, services/build_audit.py, services/briefs.py.

No network, no Claude call -- the briefs' Claude step is stubbed and
the scheduler is driven with a fake clock.

Run it with:
    cd backend
    .venv/bin/python -m tests.test_process
"""

from __future__ import annotations

import asyncio
import csv
import io
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import cache  # noqa: E402
from app.data import process_rules as rules  # noqa: E402
from app.services import (  # noqa: E402
    briefs,
    build_audit,
    contest_audit,
    contest_results,
    lineup_export,
)

PASS: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASS.append(name)
        print(f"  ok   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name}  {detail}")


# ------------------------------------------------------------------ fixtures


def _standings_csv() -> str:
    """A 10-entry contest. medlen1215 has 4 entries: two built the
    'right' way (chalk arms, top-of-order 4-stack) and two the 'wrong'
    way (leverage arm, sub-3% filler, bottom-of-order). The pool table
    gives each player real-looking ownership and points."""
    pool = [
        ("Ace Chalk", "P", 40.0, 30.0),
        ("Sneaky Arm", "P", 4.0, 2.0),
        ("Solid Two", "P", 25.0, 22.0),
        ("Lead Off", "OF", 20.0, 18.0),
        ("Two Hole", "SS", 18.0, 15.0),
        ("Three Hole", "1B", 22.0, 20.0),
        ("Clean Up", "3B", 19.0, 12.0),
        ("Five Spot", "2B", 12.0, 9.0),
        ("Bench Guy", "C", 1.0, 0.0),
        ("Nine Hole", "OF", 2.0, 0.0),
        ("Eight Hole", "OF", 2.5, 3.0),
        ("Other Star", "OF", 15.0, 16.0),
        ("Other Bat", "2B", 9.0, 7.0),
        ("Other Catcher", "C", 8.0, 5.0),
    ]
    good = "P Ace Chalk P Solid Two C Other Catcher 1B Three Hole 2B Five Spot 3B Clean Up SS Two Hole OF Lead Off OF Other Star OF Eight Hole"
    bad = "P Sneaky Arm P Solid Two C Bench Guy 1B Three Hole 2B Other Bat 3B Clean Up SS Two Hole OF Nine Hole OF Eight Hole OF Other Star"
    entries = [
        (1, "111", "sharky (1/1)", 180.0, good),
        (2, "222", "medlen1215 (1/4)", 150.0, good),
        (3, "333", "someone (1/1)", 140.0, good),
        (4, "444", "someone2 (1/1)", 120.0, good),
        (5, "555", "grinder (1/1)", 110.0, bad),
        (6, "666", "medlen1215 (3/4)", 90.0, bad),
        (7, "777", "dude (1/1)", 85.0, bad),
        (8, "888", "medlen1215 (4/4)", 70.0, bad),
        (9, "999", "x (1/1)", 60.0, bad),
        (10, "1010", "medlen1215 (2/4)", 50.0, bad),
    ]
    rows = ["Rank,EntryId,EntryName,TimeRemaining,Points,Lineup,,Player,Roster Position,%Drafted,FPTS"]
    for i in range(max(len(entries), len(pool))):
        e = entries[i] if i < len(entries) else None
        p = pool[i] if i < len(pool) else None
        left = f"{e[0]},{e[1]},{e[2]},0,{e[3]},{e[4]}" if e else ",,,,,"
        right = f"{p[0]},{p[1]},{p[2]}%,{p[3]}" if p else ",,,"
        rows.append(f"{left},,{right}")
    return "\n".join(rows) + "\n"


TEAM_BY_NAME = {
    contest_results.normalize_name(n): t
    for n, t in [
        ("Lead Off", "AAA"), ("Two Hole", "AAA"), ("Three Hole", "AAA"), ("Clean Up", "AAA"),
        ("Five Spot", "AAA"), ("Nine Hole", "AAA"), ("Eight Hole", "AAA"), ("Bench Guy", "AAA"),
        ("Other Star", "BBB"), ("Other Bat", "BBB"), ("Other Catcher", "BBB"),
    ]
}


def _slate() -> dict:
    """A one-game slate with confirmed batting orders for AAA and a
    projected (unconfirmed) order for BBB."""
    aaa = [
        {"id": 1, "name": "Lead Off", "batting_order": 1},
        {"id": 2, "name": "Two Hole", "batting_order": 2},
        {"id": 3, "name": "Three Hole", "batting_order": 3},
        {"id": 4, "name": "Clean Up", "batting_order": 4},
        {"id": 5, "name": "Five Spot", "batting_order": 5},
        {"id": 8, "name": "Eight Hole", "batting_order": 8},
        {"id": 9, "name": "Nine Hole", "batting_order": 9},
        {"id": 10, "name": "Bench Guy", "batting_order": None},
    ]
    bbb = [
        {"id": 21, "name": "Other Star", "batting_order": None, "projected_batting_order": 2},
        {"id": 22, "name": "Other Bat", "batting_order": None, "projected_batting_order": 7},
        {"id": 23, "name": "Other Catcher", "batting_order": None, "projected_batting_order": 8},
    ]
    return {
        "date": "2026-08-30",
        "games": [
            {
                "game_pk": 1,
                "game_time_utc": "2026-08-30T23:10:00Z",
                "in_slate": True,
                "home": {"abbrev": "AAA", "lineup_confirmed": True, "hitters": aaa, "probable_pitcher": {"id": 100, "name": "Ace Chalk"}},
                "away": {"abbrev": "BBB", "lineup_confirmed": False, "hitters": bbb, "probable_pitcher": {"id": 101, "name": "Sneaky Arm"}},
            }
        ],
    }


def _player(pid, name, team, salary, fpts, own):
    return {"id": pid, "name": name, "team": team, "salary": salary, "projected_fpts": fpts, "ownership_pct": own, "game_pk": 1}


# A batch that is CLEAN lineup by lineup but unfocused as a portfolio:
# six teams, seven arms, every combination built. This is the shape the
# process rules exist to fix -- "20 entries spread over 12-15 stacks and
# 11-14 pitchers, so the best idea never carried weight" -- and it is
# the case a per-entry trim cannot fix, because nothing is individually
# wrong with any of them.
_WIDE_TEAMS = ["T0", "T1", "T2", "T3", "T4", "T5"]
# Descending projection, so the "top arms" the rules ask for are known.
_WIDE_ARMS = [(f"Arm{i}", 26.0 - 2.0 * i, 22.0) for i in range(6)]
# ...plus one genuinely bad idea: a sub-10%-owned arm, the single most
# expensive habit the rules were written to stop.
_WIDE_ARMS.append(("Leverage Arm", 25.0, 4.0))


def _wide_slate() -> dict:
    """Six teams, five confirmed top-of-order bats each, seven probable
    pitchers -- deep enough that a portfolio selection has real choices
    to make."""
    games = []
    for g, (home, away) in enumerate(zip(_WIDE_TEAMS[::2], _WIDE_TEAMS[1::2])):
        side = {}
        for which, team in (("home", home), ("away", away)):
            idx = _WIDE_TEAMS.index(team)
            side[which] = {
                "abbrev": team,
                "lineup_confirmed": True,
                "hitters": [
                    {"id": 1000 + idx * 10 + spot, "name": f"{team} bat{spot}", "batting_order": spot}
                    for spot in range(1, 6)
                ],
                "probable_pitcher": {"id": 2000 + idx, "name": _WIDE_ARMS[idx][0]},
            }
        games.append({"game_pk": 10 + g, "game_time_utc": "2026-08-30T23:10:00Z", "in_slate": True, **side})
    return {"date": "2026-08-30", "games": games}


def _wide_hitter(team, spot, fpts):
    idx = _WIDE_TEAMS.index(team)
    return {
        "id": 1000 + idx * 10 + spot,
        "name": f"{team} bat{spot}",
        "team": team,
        "salary": 3725,
        "projected_fpts": fpts,
        "ownership_pct": 12.0,
        "game_pk": 10 + idx // 2,
    }


def _wide_arm(i):
    name, fpts, own = _WIDE_ARMS[i]
    return {
        "id": 2000 + i,
        "name": name,
        "team": _WIDE_TEAMS[i] if i < len(_WIDE_TEAMS) else "T0",
        "salary": 10000,
        "projected_fpts": fpts,
        "ownership_pct": own,
        "game_pk": 10 + min(i, 5) // 2,
    }


def _wide_batch():
    """
    Every (5-stack team) x (pitcher pair) x (secondary stack)
    combination -- 126 lineups. Deep enough that a portfolio meeting
    every rule genuinely EXISTS inside it, which is the point: the test
    then asks whether the selection finds it, not whether it can cope
    with a batch that has no good answer (there is a separate check for
    that).

    Projection varies by team and by arm so "the best block of lineups"
    has a knowable right answer, and every lineup lands at $49,800
    against the $50,000 cap so salary never decides anything.
    """
    entries = []
    for t, team in enumerate(_WIDE_TEAMS):
        for offset in (1, 2, 3):
            secondary = _WIDE_TEAMS[(t + offset) % len(_WIDE_TEAMS)]
            for a in range(7):
                b = (a + 1) % 7
                bump = (6 - t) * 0.4 - offset * 0.05
                hitters = [_wide_hitter(team, spot, 9.0 + bump) for spot in range(1, 6)]
                hitters += [_wide_hitter(secondary, spot, 8.0) for spot in (1, 2, 3)]
                entries.append(
                    _entry([_wide_arm(a), _wide_arm(b)], hitters, "5-3", f"{team},{secondary}")
                )
    return entries


def _entry(pitchers, hitters, stack_type, stack):
    players = pitchers + hitters
    return {
        "salary_used": sum(p["salary"] for p in players),
        "stack_type": stack_type,
        "stack": stack,
        "projected_points": round(sum(p["projected_fpts"] for p in players), 2),
        "total_ownership_pct": round(sum(p["ownership_pct"] for p in players), 1),
        "players": players,
    }


ACE = _player(100, "Ace Chalk", "CCC", 10500, 24.0, 40.0)
TWO = _player(102, "Solid Two", "DDD", 9000, 18.0, 25.0)
SNEAK = _player(101, "Sneaky Arm", "BBB", 6000, 12.0, 4.0)
AAA_TOP = [
    _player(3, "Three Hole", "AAA", 6500, 10.0, 22.0),  # C slot stand-in
    _player(1, "Lead Off", "AAA", 6000, 9.0, 20.0),
    _player(2, "Two Hole", "AAA", 6200, 9.5, 18.0),
    _player(4, "Clean Up", "AAA", 6300, 9.0, 19.0),
    _player(5, "Five Spot", "AAA", 5200, 7.0, 12.0),
]
BBB_FILL = [
    _player(21, "Other Star", "BBB", 5200, 8.0, 15.0),
    _player(22, "Other Bat", "BBB", 3200, 5.0, 9.0),
    _player(23, "Other Catcher", "BBB", 2600, 4.0, 8.0),
]
AAA_BOTTOM = [
    _player(10, "Bench Guy", "AAA", 2500, 3.0, 1.0),
    _player(9, "Nine Hole", "AAA", 3200, 4.0, 2.0),
    _player(8, "Eight Hole", "AAA", 3300, 4.5, 2.5),
    _player(4, "Clean Up", "AAA", 5300, 9.0, 19.0),
    _player(21, "Other Star", "BBB", 5000, 8.0, 15.0),
]


# ------------------------------------------------------------------- tests


async def main() -> int:
    for prefix in ("brief:", "briefs:", "brief_fired:", "contest_batch_latest:", "contest_batch_day:"):
        cache.clear(prefix)

    print("\n== process_rules")
    check("20 entries -> 4 distinct pitchers", rules.max_distinct_pitchers(20) == 4, str(rules.max_distinct_pitchers(20)))
    check("150 entries scale sub-linearly (sqrt)", 8 <= rules.max_distinct_pitchers(150) <= 12, str(rules.max_distinct_pitchers(150)))
    check("20 entries -> 4 primary stacks", rules.max_distinct_primary_stacks(20) == 4)
    check("rules text names the pitcher-leverage number", f"{rules.PITCHER_LEVERAGE_OWN_PCT:.0f}%" in rules.RULES_TEXT)

    print("\n== contest_audit")
    parsed = contest_results.parse_contest_standings(_standings_csv())
    check("fixture parses 10 entries with lineups", len(parsed["entries"]) == 10 and all(e["lineup"] for e in parsed["entries"]))
    audit = contest_audit.audit_contest(parsed, handle="MEDLEN1215", team_by_name=TEAM_BY_NAME)
    check("finds all 4 of the user's entries by handle, case-insensitively", audit["found"] and audit["summary"]["my_entries"] == 4)
    s = audit["summary"]
    check("best rank and cash count", s["best_rank"] == 2 and s["cashed"] == 1, str(s))
    check("cash line at top 20% of a 10-entry field is rank 2", s["cash_line_rank"] == 2)
    mine, top, field = audit["profiles"]["mine"], audit["profiles"]["top_1pct"], audit["profiles"]["field"]
    # A 10-entry field's "top 1%" slice is the min-20 slice = the whole field.
    check("pitcher bust rate: mine (3 Sneaky slots of 8) > top slice (6 of 20)", mine["pitcher_bust_rate_pct"] == 37.5 and top["pitcher_bust_rate_pct"] == 30.0,
          f"{mine['pitcher_bust_rate_pct']} vs {top['pitcher_bust_rate_pct']}")
    check("filler share counts sub-3% hitters (3 per bad entry, 1 per good)",
          mine["hitter_filler_share_pct"] == round(100 * 10 / 32, 1), str(mine["hitter_filler_share_pct"]))
    pb = {b["bucket"]: b for b in audit["pitcher_buckets"]}
    check("pitcher bucket <10% averages the sneaky arm's 2 pts", pb["<10%"]["avg_fpts"] == 2.0 and pb["25%+"]["avg_fpts"] > 20)
    check("my pitchers are listed with counts", audit["my_pitchers"][0]["name"] == "Solid Two" and audit["my_pitchers"][0]["entries"] == 4)
    check("best arms on the slate are ranked by actual points", audit["best_pitchers_on_slate"][0]["name"] == "Ace Chalk")
    st = audit["stacks"]
    check("stacks resolve via team map: every entry carries a 4+ AAA stack", st is not None and st["entries_with_4plus_stack_pct"] == 100.0, str(st))
    check("right-team-wrong-bats: my AAA avg trails the top 1%'s AAA avg", st["my_primary_stacks"][0]["avg_stack_fpts"] < st["top_1pct_primary_stacks"][0]["avg_stack_fpts"])
    areas = {f["area"] for f in audit["flags"]}
    check("flags include pitching (leverage at P) and hitters (filler)", {"pitching", "hitters"} <= areas, str(audit["flags"]))
    md = contest_audit.audit_to_markdown(audit, contest_name="Test GPP")
    check("markdown renders the three-column table and the flags", "| Mine | Top 1% | Field |" in md and "**Flags:**" in md)
    none = contest_audit.audit_contest(parsed, handle="nobody")
    check("unknown handle -> found=False, not an exception", none["found"] is False)
    no_teams = contest_audit.audit_contest(parsed, handle="medlen1215")
    check("without a team map the stack section is skipped, not guessed", no_teams["found"] and no_teams["stacks"] is None)

    print("\n== build_audit")
    slate = _slate()
    good1 = _entry([ACE, TWO], AAA_TOP, "5-0", "AAA")
    good2 = _entry([ACE, TWO], AAA_TOP[:4] + [BBB_FILL[0]], "4-1", "AAA,BBB")
    bad1 = _entry([SNEAK, TWO], AAA_BOTTOM, "4-1", "AAA,BBB")
    bad2 = _entry([SNEAK, ACE], BBB_FILL + AAA_TOP[:2], "3-2", "BBB,AAA")
    one = build_audit.audit_entry(good1, build_audit.slate_lookup(slate))
    check("a clean entry has no problems", one["problems"] == [], str(one["problems"]))
    one_bad = build_audit.audit_entry(bad1, build_audit.slate_lookup(slate))
    check("leverage at P is named", any(p.startswith("leverage at P: Sneaky Arm") for p in one_bad["problems"]), str(one_bad["problems"]))
    check("non-punt bats batting 6+ are named (Nine Hole $3,200 bats 9)", any("Nine Hole bats 9" in p for p in one_bad["problems"]), str(one_bad["problems"]))
    check("a $2,500 punt batting 9th is NOT a batting-order problem", not any("Bench Guy bats" in p for p in one_bad["problems"]))
    check("sub-3% players outside the 1-5 spots are filler", any("filler" in p and "Bench Guy" in p for p in one_bad["problems"]), str(one_bad["problems"]))
    check("unconfirmed projected order is labelled (proj)", any("(proj)" in p for p in build_audit.audit_entry(bad2, build_audit.slate_lookup(slate))["problems"]))

    batch = build_audit.audit_batch([good1, good2, bad1, bad2, good1, good2], slate, target_count=4)
    check("batch keeps target_count and cuts the rest", batch["keep"] == 4 and batch["cut"] == 2, f"{batch['keep']}/{batch['cut']}")
    cut_idx = {v["index"] for v in batch["verdicts"] if v["verdict"] == "cut"}
    check("the two bad entries are the ones cut", cut_idx == {2, 3}, str(cut_idx))
    check("pitcher usage is counted", batch["pitchers"]["distinct"] == 3 and batch["pitchers"]["usage"][0]["name"] == "Ace Chalk")
    check("primary-stack usage is counted", batch["stacks"]["usage"][0]["team"] == "AAA")
    check("markdown renders the cut list", "**Cut:**" in build_audit.audit_to_markdown(batch))
    empty = build_audit.audit_batch([], slate)
    check("empty batch is a flag, not a crash", empty["entries"] == 0 and empty["flags"][0]["severity"] == "high")

    print("\n== build_audit: portfolio selection")
    wide_slate, wide = _wide_slate(), _wide_batch()
    wide_audit = build_audit.audit_batch(wide, wide_slate, target_count=20)
    sel = wide_audit["selection"]
    comp = sel["compliance"]
    p_target = rules.max_distinct_pitchers(20)
    s_target = rules.max_distinct_primary_stacks(20)

    # The batch itself is the problem the rules describe: nothing is
    # wrong with any single lineup, but it is spread over everything.
    check(
        "the source batch is individually clean but sprawls across every arm and stack",
        wide_audit["pitchers"]["distinct"] == 7 and wide_audit["stacks"]["distinct_primary"] == 6,
        f"{wide_audit['pitchers']['distinct']} arms, {wide_audit['stacks']['distinct_primary']} stacks",
    )
    check(
        "and a per-entry trim would NOT fix it -- nothing is individually wrong with the "
        "lineups built on real arms",
        sum(
            1
            for v in wide_audit["verdicts"]
            if not v["problems"] and "Leverage Arm" not in v["pitchers"]
        )
        > 20,
    )

    check(
        "the selection returns exactly the number of entries asked for",
        len(sel["indices"]) == 20,
        str(len(sel["indices"])),
    )
    check(
        "the SELECTED portfolio concentrates onto a pitcher core -- the thing a per-entry "
        "trim cannot do",
        comp["distinct_pitchers"] <= p_target,
        f"{comp['distinct_pitchers']} arms (target {p_target})",
    )
    check(
        "and onto a handful of stacks",
        comp["distinct_stacks"] <= s_target,
        f"{comp['distinct_stacks']} stacks (target {s_target})",
    )
    check(
        "the best idea carries real weight rather than one lineup each",
        comp["top_stack_share_pct"] >= comp["top_stack_share_target_pct"],
        f"top stack {comp['top_stack_share_pct']}% (target {comp['top_stack_share_target_pct']}%)",
    )
    check(
        "no sub-10%-owned arm survives selection -- the most expensive habit in the review",
        comp["leverage_pitchers"] == [],
        str(comp["leverage_pitchers"]),
    )
    check(
        "the core is built from the highest-projected arms, which is what the rule says",
        [p["name"] for p in sel["pitcher_core"]][:2] == ["Arm0", "Arm1"],
        str([p["name"] for p in sel["pitcher_core"]]),
    )
    check(
        "a portfolio that meets every rule says so, and the flag list leads with it",
        sel["passes_rules"] and wide_audit["flags"][0]["area"] == "portfolio"
        and wide_audit["flags"][0]["severity"] == "ok",
        str(wide_audit["flags"][0]),
    )
    kept = [v for v in wide_audit["verdicts"] if v["verdict"] == "keep"]
    check(
        "every kept entry is ranked, so there is an order to enter them in",
        sorted(v["keep_rank"] for v in kept) == list(range(20)),
    )
    check(
        "every cut entry carries a reason in the language of the rules, not a score",
        all(
            v["reason"] and not v["reason"][0].isdigit()
            for v in wide_audit["verdicts"]
            if v["verdict"] == "cut"
        ),
    )
    off_core_cut = [
        v
        for v in wide_audit["verdicts"]
        if v["verdict"] == "cut" and "off the pitcher core" in v["reason"]
    ]
    check(
        "clean lineups cut purely for being off the core say exactly that",
        len(off_core_cut) > 0,
        f"{len(off_core_cut)} cut off-core",
    )
    check(
        "the markdown now hands over the portfolio to enter, not just what was cut",
        "**Portfolio to enter" in build_audit.audit_to_markdown(wide_audit)
        and "**Entering, best first:**" in build_audit.audit_to_markdown(wide_audit),
    )

    # A batch that genuinely cannot satisfy the rules must SAY so rather
    # than returning a portfolio that quietly fails them.
    only_bad = [e for i, e in enumerate(wide) if "Leverage Arm" in [p["name"] for p in e["players"][:2]]]
    thin = build_audit.audit_batch(only_bad, wide_slate, target_count=20)
    check(
        "when every lineup carries a leverage arm the audit refuses to pretend otherwise",
        not thin["selection"]["passes_rules"]
        and thin["flags"][0]["area"] == "portfolio"
        and "Rebuild" in thin["flags"][0]["text"],
        str(thin["flags"][0]["text"])[:90],
    )
    check(
        "and it explains that too few lineups clear the per-entry rules",
        any("clear the per-entry rules" in n for n in thin["selection"]["notes"]),
        str(thin["selection"]["notes"]),
    )

    trimmed = build_audit.trim_for_response(wide_audit, max_cuts=6)
    check(
        "trim_for_response caps the cut list but never drops a KEEP -- those are the portfolio",
        len([v for v in trimmed["verdicts"] if v["verdict"] == "keep"]) == 20
        and len([v for v in trimmed["verdicts"] if v["verdict"] == "cut"]) == 6
        and trimmed["cut_verdicts_omitted"] == len(wide) - 20 - 6,
        f'{len(wide)} audited, {trimmed.get("cut_verdicts_omitted")} omitted',
    )
    check(
        "the headline keep/cut counts still describe the WHOLE batch after trimming",
        trimmed["keep"] == wide_audit["keep"] and trimmed["cut"] == wide_audit["cut"],
        f'{trimmed["keep"]}/{trimmed["cut"]}',
    )
    check(
        "a batch small enough to send whole is returned untouched, with no omitted count",
        "cut_verdicts_omitted" not in build_audit.trim_for_response(wide_audit, max_cuts=10**6),
    )

    print("\n== build_audit: CSV export")
    keep_rows = sorted(kept, key=lambda v: v["keep_rank"])
    csv_text = lineup_export.lineups_to_csv(
        [wide[v["index"]] for v in keep_rows],
        extra_columns=[
            {"verdict": v["verdict"], "enter_order": v["keep_rank"] + 1, "audit_reason": v["reason"]}
            for v in keep_rows
        ],
    )
    csv_rows = list(csv.DictReader(io.StringIO(csv_text)))
    check(
        "the export carries one row per kept lineup with the audit's own columns",
        len(csv_rows) == 20
        and {"verdict", "enter_order", "audit_reason"} <= set(csv_rows[0]),
        str(sorted(set(csv_rows[0]) - set()))[:80],
    )
    check(
        "rows come out in the order to enter them",
        [int(r["enter_order"]) for r in csv_rows] == list(range(1, 21)),
    )
    check(
        "and still carry every roster slot, so the file is usable on its own",
        all(csv_rows[0][f"{label}_name"] for label in lineup_export.SLOT_LABELS),
    )
    check(
        "a batch with no extra columns is unchanged by the new parameter",
        set(csv.DictReader(io.StringIO(lineup_export.lineups_to_csv(wide[:2]))).fieldnames)
        == set(csv.DictReader(io.StringIO(lineup_export.lineups_to_csv(wide[:2], extra_columns=None))).fieldnames),
    )

    print("\n== briefs: batch pointer")
    briefs.remember_latest_batch("2026-08-30", "abc123", [good1, good2], source="build")
    latest = briefs.latest_batch("2026-08-30")
    check("latest batch is recorded for the day", latest and latest["batch_id"] == "abc123" and latest["total_entries"] == 2)
    check("batch -> day pointer resolves (used by reshape)", briefs.day_of_batch("abc123") == "2026-08-30")
    briefs.remember_latest_batch("2026-08-30", "def456", [good1] * 700, source="reshape")
    latest = briefs.latest_batch("2026-08-30")
    check("a later reshape replaces it and caps stored entries at 500", latest["batch_id"] == "def456" and len(latest["entries"]) == 500 and latest["total_entries"] == 700)

    print("\n== briefs: scheduler (fake clock, stubbed Claude)")
    tz = ZoneInfo("America/Chicago")
    calls: list[str] = []

    async def fake_morning(day, *, force=False):
        calls.append(f"morning:{day}")
        return {"kind": "morning", "date": day, "text": "stub"}

    async def fake_prelock(day, *, force=False, target_count=None):
        calls.append(f"prelock:{day}")
        return {"kind": "prelock", "date": day, "text": "stub"}

    lock_utc = datetime(2026, 8, 30, 23, 10, tzinfo=timezone.utc)  # 6:10 PM CDT

    async def fake_main_slate(day):
        return {"label": "Main", "game_count": 9, "start_time_utc": lock_utc.isoformat()}

    orig = (briefs.run_morning, briefs.run_prelock, briefs.main_slate)
    briefs.run_morning, briefs.run_prelock, briefs.main_slate = fake_morning, fake_prelock, fake_main_slate
    try:
        t = datetime(2026, 8, 30, 10, 30, tzinfo=tz)
        check("10:30 local: nothing fires", await briefs.tick(t.astimezone(timezone.utc)) == [] and calls == [])
        t = datetime(2026, 8, 30, 11, 0, tzinfo=tz)
        check("11:00 local: morning fires", await briefs.tick(t.astimezone(timezone.utc)) == ["morning"] and calls == ["morning:2026-08-30"])
        t = datetime(2026, 8, 30, 11, 1, tzinfo=tz)
        check("11:01: morning does NOT fire twice", await briefs.tick(t.astimezone(timezone.utc)) == [] and len(calls) == 1)
        t = datetime(2026, 8, 30, 17, 0, tzinfo=tz)
        check("5:00 PM (70 min before lock): pre-lock not yet", await briefs.tick(t.astimezone(timezone.utc)) == [])
        t = datetime(2026, 8, 30, 17, 11, tzinfo=tz)
        check("5:11 PM (59 min before lock): pre-lock fires", await briefs.tick(t.astimezone(timezone.utc)) == ["prelock"] and calls[-1] == "prelock:2026-08-30")
        t = datetime(2026, 8, 30, 17, 30, tzinfo=tz)
        check("5:30 PM: pre-lock does NOT fire twice", await briefs.tick(t.astimezone(timezone.utc)) == [] and len(calls) == 2)
        for prefix in ("brief_fired:",):
            cache.clear(prefix)
        t = datetime(2026, 8, 30, 18, 30, tzinfo=tz)
        check("after lock, an unfired pre-lock brief is NOT fired late", "prelock" not in await briefs.tick(t.astimezone(timezone.utc)))
        cache.clear("brief_fired:")
        t = datetime(2026, 8, 30, 14, 0, tzinfo=tz)
        check("after a restart at 2 PM, the morning brief still fires (late is better than never)", "morning" in await briefs.tick(t.astimezone(timezone.utc)))
        cache.clear("brief_fired:")
        t = datetime(2026, 8, 30, 20, 0, tzinfo=tz)
        check("but not at 8 PM (past the 8-hour window)", "morning" not in await briefs.tick(t.astimezone(timezone.utc)))
    finally:
        briefs.run_morning, briefs.run_prelock, briefs.main_slate = orig

    print("\n== briefs: storage")
    briefs._store("2026-08-30", "morning", {"kind": "morning", "date": "2026-08-30", "text": "hello", "generated_at": "x"})
    check("stored brief is readable", (briefs.get_brief("2026-08-30", "morning") or {}).get("text") == "hello")
    check("index lists it once", sum(1 for e in briefs.list_briefs() if e["date"] == "2026-08-30" and e["kind"] == "morning") == 1)
    briefs._store("2026-08-30", "morning", {"kind": "morning", "date": "2026-08-30", "text": "again", "generated_at": "y"})
    check("re-storing replaces rather than duplicates in the index", sum(1 for e in briefs.list_briefs() if e["date"] == "2026-08-30") == 1)
    status_lock = briefs._lock_time({"start_time_utc": "2026-08-30T23:10:00Z"})
    check("lock time parses DK's ISO string as UTC", status_lock == datetime(2026, 8, 30, 23, 10, tzinfo=timezone.utc))
    check("local time formatting is Windows-safe (no %-I)", briefs._local(status_lock, ZoneInfo("America/Chicago")) == "6:10 PM CDT", briefs._local(status_lock, ZoneInfo("America/Chicago")))

    for prefix in ("brief:", "briefs:", "brief_fired:", "contest_batch_latest:", "contest_batch_day:"):
        cache.clear(prefix)

    print("\n" + "=" * 60)
    print(f"{len(PASS)} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  FAILED: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
