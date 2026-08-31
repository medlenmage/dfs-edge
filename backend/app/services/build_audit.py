"""
Pre-entry PROCESS audit of a generated contest batch -- the check that
runs BEFORE lineups are entered, against the same rules the
post-contest audit scores after the fact (data/process_rules.py).

Input is the batch shape every generator in this app already produces
(contest.py's flat `players` list in fixed DK slot order: P, P, C, 1B,
2B, 3B, SS, OF, OF, OF; each player carrying id/name/team/salary/
projected_fpts/ownership_pct/game_pk) plus the built slate, which is
where batting order and lineup-confirmation status live. Nothing is
re-fetched; this is pure arithmetic over data the app already has.

Output is a batch-level report (pitcher concentration, primary-stack
conviction, batting-order compliance, filler share, salary use), a
list of flags, and -- the part that is actually actionable -- a
SELECTED PORTFOLIO: the specific `target_count` entries to enter,
chosen so the surviving set obeys the portfolio rules rather than
merely dropping individually-broken lineups.

That distinction is the whole point of `select_portfolio()`. Trimming
by per-entry penalty alone leaves a set that can still fail every
batch-level rule the audit flags in the same breath -- 40 clean
lineups spread over 14 pitchers and 15 stacks is exactly the leak the
rules exist to close. So the selection CONSTRUCTS the portfolio: pick
the pitcher core, pick the stacks, allocate entries across them with
the best idea carrying real weight, then fill each allocation with its
strongest lineups. Whatever it could not achieve is reported rather
than quietly accepted.

The selected entries come back as real lineup dicts, so they can be
written straight to a CSV or re-cached as their own batch and pushed
into a DraftKings entries template.
"""

from __future__ import annotations

import statistics
from collections import Counter
from typing import Any

from app.data import process_rules as rules
from app.services.lineup_export import players_in_slot_order

_PITCHER_SLOTS = 2  # the first two players of a Classic entry

# A per-entry penalty at or above this is a lineup with a real process
# problem, not just a weaker one -- it is excluded from the portfolio
# before selection even starts. Three is one leverage pitcher, or a
# missing stack plus a filler bat.
_HARD_PENALTY = 3.0


def _mean(xs: list[float]) -> float | None:
    return round(statistics.fmean(xs), 2) if xs else None


def slate_lookup(slate: dict[str, Any]) -> dict[Any, dict[str, Any]]:
    """player id -> {batting_order, projected_batting_order,
    lineup_confirmed, team, name, is_pitcher, game_pk, game_time_utc}
    for every hitter and probable pitcher on a built slate."""
    out: dict[Any, dict[str, Any]] = {}
    for game in slate.get("games") or []:
        for side in ("home", "away"):
            t = game.get(side) or {}
            confirmed = bool(t.get("lineup_confirmed"))
            for h in t.get("hitters") or []:
                out[h.get("id")] = {
                    "name": h.get("name"),
                    "team": t.get("abbrev"),
                    "is_pitcher": False,
                    "batting_order": h.get("batting_order"),
                    "projected_batting_order": h.get("projected_batting_order"),
                    "lineup_confirmed": confirmed,
                    "game_pk": game.get("game_pk"),
                    "game_time_utc": game.get("game_time_utc"),
                }
            p = t.get("probable_pitcher")
            if p and p.get("id") is not None:
                out[p["id"]] = {
                    "name": p.get("name"),
                    "team": t.get("abbrev"),
                    "is_pitcher": True,
                    "batting_order": None,
                    "projected_batting_order": None,
                    "lineup_confirmed": confirmed,
                    "game_pk": game.get("game_pk"),
                    "game_time_utc": game.get("game_time_utc"),
                }
    return out


def _effective_order(info: dict[str, Any] | None) -> tuple[int | None, bool]:
    """(batting spot, is_confirmed). Confirmed order wins; RotoWire's
    projected spot is the fallback the rest of the app already uses."""
    if not info:
        return None, False
    if info.get("batting_order") is not None:
        return int(info["batting_order"]), True
    if info.get("projected_batting_order") is not None:
        return int(info["projected_batting_order"]), False
    return None, False


def audit_entry(entry: dict[str, Any], lookup: dict[Any, dict[str, Any]]) -> dict[str, Any]:
    """One lineup's process check. `problems` is what would be read out
    loud; `score` is a penalty total used only to order cuts."""
    players = players_in_slot_order(entry)
    pitchers = players[:_PITCHER_SLOTS]
    hitters = players[_PITCHER_SLOTS:]

    problems: list[str] = []
    penalty = 0.0

    # Pitching: leverage at P
    for p in pitchers:
        own = p.get("ownership_pct")
        if own is not None and own < rules.PITCHER_LEVERAGE_OWN_PCT:
            problems.append(f"leverage at P: {p.get('name')} ({own:.1f}% owned)")
            penalty += 3

    # Stack composition from the entry's own hitters
    team_counts: Counter[str] = Counter(h.get("team") or "" for h in hitters)
    primary_team, primary_size = (team_counts.most_common(1)[0] if team_counts else ("", 0))
    stacked_teams = {t for t, n in team_counts.items() if n >= 2}

    late_order = []
    unconfirmed = 0
    fillers = []
    for h in hitters:
        info = lookup.get(h.get("id"))
        spot, confirmed = _effective_order(info)
        if info is not None and not info.get("lineup_confirmed"):
            unconfirmed += 1
        in_stack = (h.get("team") or "") in stacked_teams
        if spot is not None:
            limit = rules.HITTER_MAX_BATTING_ORDER_TOLERATED if in_stack else rules.HITTER_MAX_BATTING_ORDER_OK
            if spot > limit and (h.get("salary") or 0) >= 3000:
                late_order.append(f"{h.get('name')} bats {spot}{'' if confirmed else ' (proj)'}")
                penalty += 1.5 if spot >= 7 else 0.75
        own = h.get("ownership_pct")
        if own is not None and own < rules.FILLER_OWN_PCT:
            deliberate = in_stack and spot is not None and spot <= rules.HITTER_MAX_BATTING_ORDER_OK
            if not deliberate:
                fillers.append(f"{h.get('name')} ({own:.1f}%)")
                penalty += 1
    if late_order:
        problems.append("batting 6+: " + ", ".join(late_order))
    if len(fillers) > rules.MAX_FILLERS_PER_LINEUP:
        problems.append(f"{len(fillers)} sub-{rules.FILLER_OWN_PCT:.0f}% fillers: " + ", ".join(fillers))
    elif fillers:
        problems.append("filler: " + ", ".join(fillers))

    if primary_size < rules.STACK_MIN_SIZE:
        problems.append(f"no {rules.STACK_MIN_SIZE}+ stack (largest {primary_size} {primary_team})")
        penalty += 2

    salary = entry.get("salary_used") or sum(p.get("salary") or 0 for p in players)
    unused = rules.SALARY_CAP - salary
    if unused > rules.SALARY_UNUSED_MAX:
        problems.append(f"${unused:,} unused salary")
        penalty += min(2.0, unused / 1000)  # never a hard cut on its own

    return {
        "pitchers": [p.get("name") for p in pitchers],
        # The full pitcher dicts too -- the portfolio selection needs
        # their projection and ownership, not just their names.
        "pitcher_rows": pitchers,
        "primary_team": primary_team,
        "primary_size": primary_size,
        "stack_type": entry.get("stack_type"),
        "stack": entry.get("stack"),
        "salary_used": salary,
        "projected_points": entry.get("projected_points"),
        "total_ownership_pct": entry.get("total_ownership_pct"),
        "unconfirmed_hitters": unconfirmed,
        "problems": problems,
        "penalty": round(penalty, 2),
    }


def _pitcher_core(
    per_entry: list[dict[str, Any]], eligible: list[int], limit: int, needed: int
) -> tuple[list[str], bool]:
    """
    Choose the arms the portfolio is built on.

    Ranked by the pitcher's own projection, because that is literally
    what the rule says -- "use the top 2-3 arms on the slate", and the
    review that produced these rules found taking leverage at pitcher
    was the single most expensive habit in the user's history. Usage
    within the batch breaks ties, so a pitcher the generator actually
    liked wins over one it used twice.

    Returns the core and whether it had to be widened past the
    recommended limit to field enough lineups -- widening is sometimes
    unavoidable (the batch may simply not contain `needed` lineups
    built on the best few arms) and is reported, never hidden.
    """
    proj: dict[str, float] = {}
    used: Counter[str] = Counter()
    for i in eligible:
        for name, p in zip(per_entry[i]["pitchers"], per_entry[i]["pitcher_rows"]):
            if name is None:
                continue
            proj[name] = max(proj.get(name, 0.0), float(p.get("projected_fpts") or 0))
            used[name] += 1
    ranked = sorted(proj, key=lambda n: (-proj[n], -used[n], n))

    def _covered(core: set[str]) -> int:
        return sum(1 for i in eligible if set(per_entry[i]["pitchers"]) <= core)

    core = set(ranked[:limit])
    widened = False
    for name in ranked[limit:]:
        if _covered(core) >= needed:
            break
        core.add(name)
        widened = True
    return sorted(core, key=lambda n: -proj[n]), widened


def _stack_ranking(
    per_entry: list[dict[str, Any]], candidates: list[int], block: int
) -> list[tuple[str, float, int]]:
    """
    Rank the primary stacks in the pool by the strength of the BLOCK of
    lineups each one could actually supply, not by its single best
    lineup and not by raw count.

    A team with one great build and nothing behind it cannot carry a
    5-7 lineup allocation; a team with eight solid ones can. So each
    team is scored on the mean projection of its best `block` entries,
    which is exactly the quantity the allocation will go on to use.
    """
    by_team: dict[str, list[float]] = {}
    for i in candidates:
        r = per_entry[i]
        if r["primary_size"] < rules.STACK_MIN_SIZE or not r["primary_team"]:
            continue
        by_team.setdefault(r["primary_team"], []).append(float(r["projected_points"] or 0))
    out = []
    for team, points in by_team.items():
        points.sort(reverse=True)
        top = points[:block] or [0.0]
        out.append((team, round(statistics.fmean(top), 3), len(points)))
    out.sort(key=lambda t: (-t[1], -t[2], t[0]))
    return out


def _allocate(stacks: list[tuple[str, float, int]], n_play: int) -> dict[str, int]:
    """
    Split the portfolio across the chosen stacks, with the best idea
    carrying real weight.

    The top stack is floored at TOP_STACK_MIN_SHARE of the portfolio --
    the rule that exists because 20-entry portfolios spread over 12-15
    stacks meant the best idea never carried any. The rest is split
    evenly, and any remainder goes to the strongest stacks first. No
    stack is allocated more lineups than it can actually supply.
    """
    if not stacks:
        return {}
    k = len(stacks)
    alloc = {team: 0 for team, _, _ in stacks}

    top_team, _, top_supply = stacks[0]
    floor = min(top_supply, max(1, -(-int(rules.TOP_STACK_MIN_SHARE * n_play) // 1)))
    alloc[top_team] = int(floor)

    remaining = n_play - alloc[top_team]
    if k > 1 and remaining > 0:
        even = remaining // (k - 1)
        for team, _, supply in stacks[1:]:
            alloc[team] = min(supply, even)

    # Anything still unallocated (or freed by a stack that cannot supply
    # its share) goes to the strongest stacks that still have lineups.
    while sum(alloc.values()) < n_play:
        progressed = False
        for team, _, supply in stacks:
            if sum(alloc.values()) >= n_play:
                break
            if alloc[team] < supply:
                alloc[team] += 1
                progressed = True
        if not progressed:
            break
    return {t: n for t, n in alloc.items() if n > 0}


def select_portfolio(
    per_entry: list[dict[str, Any]], n_play: int
) -> dict[str, Any]:
    """
    Construct the portfolio to actually enter, rather than trimming the
    batch down to it.

    Four steps, in the order the process rules put them:
      1. Drop lineups with a real per-entry problem.
      2. Pick the pitcher core, and keep only lineups built on it.
      3. Pick the stacks and allocate entries across them, top idea
         weighted.
      4. Fill each allocation with that stack's strongest lineups.

    Returns the chosen indices best-first, the reasoning, and an
    honest account of anything it could not satisfy.
    """
    n = len(per_entry)
    n_play = max(1, min(n_play, n))
    notes: list[str] = []

    clean = [i for i in range(n) if per_entry[i]["penalty"] < _HARD_PENALTY]
    if len(clean) < n_play:
        # Not enough lineups pass on their own. Take the cleanest, and
        # say so -- silently entering flawed lineups to hit a number is
        # exactly what this audit exists to stop.
        ordered = sorted(range(n), key=lambda i: (per_entry[i]["penalty"], -(per_entry[i]["projected_points"] or 0)))
        notes.append(
            f"Only {len(clean)} of {n} lineups clear the per-entry rules, so the portfolio "
            f"reaches {n_play} by including the cleanest of the rest. Build more lineups "
            "rather than entering these."
        )
        clean = ordered[:n_play]

    p_limit = rules.max_distinct_pitchers(n_play)
    core, widened = _pitcher_core(per_entry, clean, p_limit, n_play)
    core_set = set(core)
    on_core = [i for i in clean if set(per_entry[i]["pitchers"]) <= core_set]
    if widened:
        notes.append(
            f"The pitcher core had to widen to {len(core)} arms (target {p_limit}) -- the batch "
            f"does not contain {n_play} lineups built on fewer. Rebuild with the arms locked."
        )
    if not on_core:
        on_core = clean
        notes.append("No pitcher core could be formed from this batch; selection ignored pitching.")

    s_limit = rules.max_distinct_primary_stacks(n_play)
    block = max(1, -(-n_play // max(s_limit, 1)))
    ranked_all = _stack_ranking(per_entry, on_core, block)

    # Take the best s_limit stacks, and only widen if they genuinely
    # cannot supply the portfolio. Widening by a whole extra STACK is
    # the right failure mode; the wrong one is topping up with single
    # lineups from everywhere, which silently undoes the concentration
    # this step just built and lands back at the sprawl the rules
    # exist to stop.
    k = min(s_limit, len(ranked_all))
    ranked_stacks, alloc = ranked_all[:k], {}
    while ranked_stacks:
        alloc = _allocate(ranked_stacks, n_play)
        if sum(alloc.values()) >= n_play or k >= len(ranked_all):
            break
        k += 1
        ranked_stacks = ranked_all[:k]
    if k > s_limit:
        notes.append(
            f"The best {s_limit} stacks could not supply {n_play} lineups on the pitcher core, "
            f"so the portfolio spreads over {k}. Build more lineups on your best stacks."
        )

    chosen: list[int] = []
    if not ranked_stacks:
        notes.append(
            f"No lineup on the pitcher core carries a {rules.STACK_MIN_SIZE}+ stack, so the "
            "portfolio is ranked on projection alone. This batch is not built to the rules."
        )
        chosen = sorted(on_core, key=lambda i: -(per_entry[i]["projected_points"] or 0))[:n_play]
        alloc: dict[str, int] = {}
    else:
        by_team: dict[str, list[int]] = {}
        for i in on_core:
            r = per_entry[i]
            if r["primary_size"] >= rules.STACK_MIN_SIZE and r["primary_team"] in alloc:
                by_team.setdefault(r["primary_team"], []).append(i)
        for pool in by_team.values():
            pool.sort(key=lambda i: (per_entry[i]["penalty"], -(per_entry[i]["projected_points"] or 0)))
        for team, want in sorted(alloc.items(), key=lambda kv: -kv[1]):
            chosen.extend((by_team.get(team) or [])[:want])
        if len(chosen) < n_play:
            # Every stack in the pool is exhausted. This batch simply
            # does not contain enough rule-abiding lineups, which is a
            # finding, not something to paper over -- but a partial
            # portfolio beats none, so the remainder comes from the
            # best builds left and is called out.
            short = n_play - len(chosen)
            taken = set(chosen)
            spare = [
                i
                for i in sorted(on_core, key=lambda i: (per_entry[i]["penalty"], -(per_entry[i]["projected_points"] or 0)))
                if i not in taken
            ]
            chosen.extend(spare[:short])
            if spare:
                notes.append(
                    f"No stack had lineups left to fill the last {min(short, len(spare))} slots, so "
                    "they came from the next-best builds on the core regardless of stack. Build more."
                )

    chosen = sorted(set(chosen), key=lambda i: (per_entry[i]["penalty"], -(per_entry[i]["projected_points"] or 0)))[:n_play]

    # What the SELECTED portfolio actually achieves -- reported whether
    # or not it hits the targets, since a selection that quietly misses
    # is worse than no selection at all.
    sel = [per_entry[i] for i in chosen]
    sel_pitchers: Counter[str] = Counter()
    for r in sel:
        sel_pitchers.update([p for p in r["pitchers"] if p])
    sel_stacks: Counter[str] = Counter(
        r["primary_team"] for r in sel if r["primary_size"] >= rules.STACK_MIN_SIZE and r["primary_team"]
    )
    top_share = (sel_stacks.most_common(1)[0][1] / len(sel)) if sel and sel_stacks else 0.0
    four_plus = sum(1 for r in sel if r["primary_size"] >= rules.STACK_MIN_SIZE)

    compliance = {
        "distinct_pitchers": len(sel_pitchers),
        "distinct_pitchers_target": p_limit,
        "distinct_stacks": len(sel_stacks),
        "distinct_stacks_target": s_limit,
        "top_stack_share_pct": round(100 * top_share, 1),
        "top_stack_share_target_pct": round(100 * rules.TOP_STACK_MIN_SHARE, 1),
        "entries_with_4plus_pct": round(100 * four_plus / len(sel), 1) if sel else 0.0,
        "leverage_pitchers": sorted(
            {
                p.get("name")
                for r in sel
                for p in r["pitcher_rows"]
                if p.get("ownership_pct") is not None
                and p["ownership_pct"] < rules.PITCHER_LEVERAGE_OWN_PCT
            }
        ),
    }
    passes = (
        compliance["distinct_pitchers"] <= p_limit
        and compliance["distinct_stacks"] <= s_limit
        and top_share >= rules.TOP_STACK_MIN_SHARE
        and not compliance["leverage_pitchers"]
    )

    return {
        "indices": chosen,
        "pitcher_core": [{"name": name, "entries": sel_pitchers.get(name, 0)} for name in core],
        "stack_allocation": [
            {"team": team, "planned": alloc.get(team, 0), "selected": sel_stacks.get(team, 0)}
            for team, _, _ in ranked_stacks
        ]
        if ranked_stacks
        else [],
        "compliance": compliance,
        "passes_rules": passes,
        "notes": notes,
    }


def _verdict_reason(entry: dict[str, Any], kept: bool, selection: dict[str, Any]) -> str:
    """One line saying why this lineup is in or out, in the language of
    the rules rather than of the scoring."""
    if kept:
        bits = []
        if entry["primary_size"] >= rules.STACK_MIN_SIZE:
            bits.append(f"{entry['primary_size']}-stack {entry['primary_team']}")
        core = {p["name"] for p in selection["pitcher_core"]}
        if core and set(entry["pitchers"]) <= core:
            bits.append("core arms " + "+".join(n for n in entry["pitchers"] if n))
        return ", ".join(bits) or "best remaining build"
    if entry["problems"]:
        return "; ".join(entry["problems"])
    core = {p["name"] for p in selection["pitcher_core"]}
    off_core = [n for n in entry["pitchers"] if n and n not in core]
    if off_core:
        return "off the pitcher core: " + ", ".join(off_core)
    allocated = {a["team"] for a in selection["stack_allocation"]}
    if allocated and entry["primary_team"] not in allocated:
        return f"stack {entry['primary_team'] or '(none)'} is not one of the portfolio's stacks"
    return "clean, but the portfolio is full -- weaker than the builds ahead of it"


def audit_batch(
    entries: list[dict[str, Any]],
    slate: dict[str, Any] | None = None,
    *,
    target_count: int | None = None,
) -> dict[str, Any]:
    """
    Audit a whole batch. `target_count` is how many entries the user
    actually intends to play (a 3,000-lineup contest build audited as
    a 20-entry portfolio is a different question); defaults to the
    batch size.
    """
    batch_size = len(entries)
    lookup = slate_lookup(slate) if slate else {}

    # A batch can now hold BOTH the lineups you are entering (built by
    # the optimizer, read out of a DK entries file, or typed in -- see
    # services/lineup_intake.py) and the generated field they will be
    # simulated against. Only the former are yours to audit: scoring a
    # 3,000-lineup opponent field against your process rules answers a
    # question nobody asked, and selecting a portfolio out of it would
    # hand back lineups you never built. When a batch carries no source
    # tags at all -- every batch before this existed, and any pure
    # generated contest -- the whole thing is audited, exactly as before.
    mine = [e for e in entries if (e.get("source") or "generated") != "generated"]
    audited_source = "your own lineups" if mine else "the whole batch"
    if mine:
        entries = mine

    per_entry = [audit_entry(e, lookup) for e in entries]
    n = len(entries)
    n_play = target_count or n

    if n == 0:
        return {"entries": 0, "flags": [{"severity": "high", "area": "batch", "text": "No entries to audit."}]}

    # Pitcher concentration
    p_counter: Counter[str] = Counter()
    for r in per_entry:
        for name in r["pitchers"]:
            p_counter[name] += 1
    p_distinct = len(p_counter)
    p_max = rules.max_distinct_pitchers(n_play)
    combos: Counter[tuple[str, ...]] = Counter(tuple(sorted(r["pitchers"])) for r in per_entry)

    # Primary stacks
    s_counter: Counter[str] = Counter(r["primary_team"] for r in per_entry if r["primary_size"] >= rules.STACK_MIN_SIZE)
    four_plus = sum(1 for r in per_entry if r["primary_size"] >= rules.STACK_MIN_SIZE)
    top_share = (s_counter.most_common(1)[0][1] / n) if s_counter else 0.0
    s_max = rules.max_distinct_primary_stacks(n_play)

    # Order / filler / salary
    with_late = sum(1 for r in per_entry if any(p.startswith("batting 6+") for p in r["problems"]))
    with_filler = sum(1 for r in per_entry if any("filler" in p for p in r["problems"]))
    with_lev_p = sum(1 for r in per_entry if any(p.startswith("leverage at P") for p in r["problems"]))
    unconfirmed_entries = sum(1 for r in per_entry if r["unconfirmed_hitters"] > 0)
    salaries = [float(r["salary_used"]) for r in per_entry]
    under_floor = sum(1 for s in salaries if rules.SALARY_CAP - s > rules.SALARY_UNUSED_MAX)

    flags: list[dict[str, str]] = []
    if p_distinct > p_max:
        flags.append(
            {
                "severity": "high",
                "area": "pitching",
                "text": f"{p_distinct} distinct pitchers for a {n_play}-entry portfolio (target <= {p_max}). Pick a core.",
            }
        )
    if with_lev_p / n >= 0.25:
        flags.append(
            {
                "severity": "high",
                "area": "pitching",
                "text": f"{with_lev_p} of {n} entries carry a pitcher under {rules.PITCHER_LEVERAGE_OWN_PCT:.0f}% owned.",
            }
        )
    if s_counter and len(s_counter) > s_max:
        flags.append(
            {
                "severity": "medium",
                "area": "conviction",
                "text": f"{len(s_counter)} distinct primary stacks (target <= {s_max} for {n_play} entries).",
            }
        )
    if s_counter and top_share < rules.TOP_STACK_MIN_SHARE and n_play >= 10:
        flags.append(
            {
                "severity": "medium",
                "area": "conviction",
                "text": f"Most-used stack is in only {100 * top_share:.0f}% of entries (target {100 * rules.TOP_STACK_MIN_SHARE:.0f}%+).",
            }
        )
    if four_plus / n < 0.6:
        flags.append(
            {
                "severity": "medium",
                "area": "stacks",
                "text": f"Only {100 * four_plus / n:.0f}% of entries carry a {rules.STACK_MIN_SIZE}+ stack.",
            }
        )
    if with_late / n >= 0.3:
        flags.append(
            {
                "severity": "high",
                "area": "hitters",
                "text": f"{with_late} of {n} entries roster a non-punt hitter batting 6th or lower.",
            }
        )
    if with_filler / n >= 0.3:
        flags.append(
            {
                "severity": "medium",
                "area": "hitters",
                "text": f"{with_filler} of {n} entries carry sub-{rules.FILLER_OWN_PCT:.0f}% filler outside a stack.",
            }
        )
    if under_floor / n >= 0.2:
        flags.append(
            {
                "severity": "low",
                "area": "salary",
                "text": f"{under_floor} of {n} entries leave more than ${rules.SALARY_UNUSED_MAX} on the table.",
            }
        )
    if lookup and unconfirmed_entries == n:
        flags.append(
            {
                "severity": "info",
                "area": "lineups",
                "text": "No lineups are confirmed yet -- batting-order checks are against projected spots. Re-run after confirmation.",
            }
        )
    if not flags:
        flags.append({"severity": "ok", "area": "process", "text": "Batch passes every process rule."})

    # The portfolio to actually enter. This CONSTRUCTS the set rather
    # than trimming to it -- see select_portfolio's own docstring for
    # why dropping the worst lineups one at a time leaves a portfolio
    # that still fails every batch-level rule flagged above.
    selection = select_portfolio(per_entry, n_play)
    keep_order = {idx: rank for rank, idx in enumerate(selection["indices"])}
    verdicts = []
    for i, r in enumerate(per_entry):
        kept = i in keep_order
        verdicts.append(
            {
                "index": i,
                "verdict": "keep" if kept else "cut",
                "keep_rank": keep_order.get(i),
                "reason": _verdict_reason(r, kept, selection),
                # pitcher_rows are full player dicts; they are only
                # needed inside the selection maths and would triple
                # the size of a 500-entry audit response.
                **{k: v for k, v in r.items() if k != "pitcher_rows"},
            }
        )

    if not selection["passes_rules"]:
        c = selection["compliance"]
        misses = []
        if c["distinct_pitchers"] > c["distinct_pitchers_target"]:
            misses.append(f"{c['distinct_pitchers']} arms (target {c['distinct_pitchers_target']})")
        if c["distinct_stacks"] > c["distinct_stacks_target"]:
            misses.append(f"{c['distinct_stacks']} stacks (target {c['distinct_stacks_target']})")
        if c["top_stack_share_pct"] < c["top_stack_share_target_pct"]:
            misses.append(
                f"top stack only {c['top_stack_share_pct']}% (target {c['top_stack_share_target_pct']}%)"
            )
        if c["leverage_pitchers"]:
            misses.append("leverage arm: " + ", ".join(c["leverage_pitchers"]))
        if misses:
            flags.insert(
                0,
                {
                    "severity": "high",
                    "area": "portfolio",
                    "text": "Even the best portfolio this batch can supply misses the rules: "
                    + "; ".join(misses)
                    + ". Rebuild rather than entering it.",
                },
            )
    else:
        flags.insert(
            0,
            {
                "severity": "ok",
                "area": "portfolio",
                "text": f"Selected {len(selection['indices'])} entries that meet every portfolio rule.",
            },
        )

    return {
        "entries": n,
        "target_count": n_play,
        # What was audited, and how much of the batch that was -- so a
        # 20-of-3,000 audit can never be mistaken for a 20-of-20 one.
        "audited": audited_source,
        "batch_entries": batch_size,
        "pitchers": {
            "distinct": p_distinct,
            "recommended_max": p_max,
            "usage": [{"name": k, "entries": v} for k, v in p_counter.most_common()],
            "combos": [{"pitchers": list(k), "entries": v} for k, v in combos.most_common(6)],
            "entries_with_leverage_pitcher": with_lev_p,
        },
        "stacks": {
            "entries_with_4plus_pct": round(100 * four_plus / n, 1),
            "distinct_primary": len(s_counter),
            "recommended_max": s_max,
            "top_stack_share_pct": round(100 * top_share, 1),
            "usage": [{"team": k, "entries": v} for k, v in s_counter.most_common()],
        },
        "hitters": {
            "entries_with_late_order": with_late,
            "entries_with_filler": with_filler,
            "entries_with_unconfirmed": unconfirmed_entries,
        },
        "salary": {
            "median": round(statistics.median(salaries), 0) if salaries else None,
            "min": min(salaries) if salaries else None,
            "under_floor": under_floor,
        },
        "flags": flags,
        "verdicts": verdicts,
        "selection": selection,
        "keep": len(selection["indices"]),
        "cut": n - len(selection["indices"]),
    }


# A 6,000-lineup batch produces 6,000 verdicts, which is a ~3MB JSON
# response and, for the pre-lock brief, a ~3MB row written into the
# cache for two weeks. Every kept verdict matters (it is the portfolio);
# the cut ones past the first couple of hundred are a record, not a
# screen. This is the cap for anything held in memory or sent over the
# wire -- the "all + cut reasons" CSV export stays complete.
CUT_VERDICT_PREVIEW = 200


def trim_for_response(audit: dict[str, Any], *, max_cuts: int = CUT_VERDICT_PREVIEW) -> dict[str, Any]:
    """
    The audit with its cut list capped, for responses and storage.

    Every KEEP survives -- those are the portfolio and dropping any of
    them would misstate what to enter. Only cuts are truncated, and the
    result says how many were left out so nothing looks complete when it
    isn't.
    """
    verdicts = audit.get("verdicts") or []
    keeps = [v for v in verdicts if v["verdict"] == "keep"]
    cuts = [v for v in verdicts if v["verdict"] != "keep"]
    if len(cuts) <= max_cuts:
        return audit
    return {
        **audit,
        "verdicts": keeps + cuts[:max_cuts],
        "cut_verdicts_omitted": len(cuts) - max_cuts,
    }


def audit_to_markdown(audit: dict[str, Any], *, max_verdicts: int = 40) -> str:
    if not audit.get("entries"):
        return "_No entries to audit._\n"
    p, s, h, sal = audit["pitchers"], audit["stacks"], audit["hitters"], audit["salary"]
    scope = (
        f" (audited {audit['audited']}, {audit['entries']} of {audit['batch_entries']} in the batch)"
        if audit.get("audited") and audit.get("batch_entries", 0) != audit["entries"]
        else ""
    )
    lines = [
        f"**Batch:** {audit['entries']} entries, playing {audit['target_count']}. "
        f"Keep {audit['keep']}, cut {audit['cut']}.{scope}",
        "",
        f"- Pitchers: {p['distinct']} distinct (target <= {p['recommended_max']}); "
        + ", ".join(f"{u['name']} x{u['entries']}" for u in p["usage"][:8])
        + f". {p['entries_with_leverage_pitcher']} entries with a sub-{rules.PITCHER_LEVERAGE_OWN_PCT:.0f}% arm.",
        f"- Stacks: {s['entries_with_4plus_pct']}% with a 4+ stack; {s['distinct_primary']} distinct primary (target <= {s['recommended_max']}); "
        f"top stack {s['top_stack_share_pct']}% of entries; "
        + ", ".join(f"{u['team']} x{u['entries']}" for u in s["usage"][:8]),
        f"- Hitters: {h['entries_with_late_order']} entries with a 6+ order bat, {h['entries_with_filler']} with filler, "
        f"{h['entries_with_unconfirmed']} with unconfirmed lineups.",
        f"- Salary: median ${sal['median']:,.0f}, min ${sal['min']:,.0f}, {sal['under_floor']} under the floor.",
        "",
        "**Flags:**",
    ]
    for f in audit["flags"]:
        lines.append(f"- [{f['severity']}] {f['text']}")

    sel = audit.get("selection") or {}
    if sel.get("indices"):
        c = sel["compliance"]
        lines += [
            "",
            f"**Portfolio to enter ({len(sel['indices'])} entries)** -- "
            + ("meets every rule." if sel["passes_rules"] else "best this batch can do; see the flag above."),
            f"- Pitcher core: "
            + ", ".join(f"{p['name']} x{p['entries']}" for p in sel["pitcher_core"])
            + f" ({c['distinct_pitchers']} used, target <= {c['distinct_pitchers_target']}).",
            f"- Stacks: "
            + ", ".join(f"{a['team']} x{a['selected']}" for a in sel["stack_allocation"] if a["selected"])
            + f" (top stack {c['top_stack_share_pct']}% of entries, target {c['top_stack_share_target_pct']}%+).",
        ]
        for note in sel.get("notes") or []:
            lines.append(f"- NOTE: {note}")
        lines += ["", "**Entering, best first:**"]
        keeps = sorted(
            (v for v in audit["verdicts"] if v["verdict"] == "keep"),
            key=lambda v: v["keep_rank"] if v["keep_rank"] is not None else 10**6,
        )
        for v in keeps[:max_verdicts]:
            lines.append(
                f"- #{v['index'] + 1} {v['stack_type'] or 'no stack'} {v['stack'] or ''} / "
                f"{'+'.join(n for n in v['pitchers'] if n)} -- "
                f"${v['salary_used']:,.0f}, {v['projected_points']:.1f} pts, "
                f"{(v['total_ownership_pct'] or 0):.0f}% own"
            )

    cuts = [v for v in audit["verdicts"] if v["verdict"] == "cut"][:max_verdicts]
    if cuts:
        lines += ["", "**Cut:**"]
        for v in cuts:
            lines.append(
                f"- #{v['index'] + 1} {v['stack_type'] or ''} {v['stack'] or ''} / "
                f"{'+'.join(n for n in v['pitchers'] if n)}: {v['reason']}"
            )
    return "\n".join(lines) + "\n"
