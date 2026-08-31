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
per-entry verdict (`keep` / `cut`, with the reasons), and a list of
flags -- plus a markdown rendering the briefs hand to Claude.
"""

from __future__ import annotations

import statistics
from collections import Counter
from typing import Any

from app.data import process_rules as rules
from app.services.lineup_export import players_in_slot_order

_PITCHER_SLOTS = 2  # the first two players of a Classic entry


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
    lookup = slate_lookup(slate) if slate else {}
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

    # Verdicts: cut the worst until the portfolio fits target_count,
    # and always cut anything with a hard problem. Order is by penalty,
    # then by weaker projection.
    ranked = sorted(
        range(n),
        key=lambda i: (-per_entry[i]["penalty"], per_entry[i]["projected_points"] or 0),
    )
    hard = {i for i in range(n) if per_entry[i]["penalty"] >= 3}
    to_cut: set[int] = set(hard)
    for i in ranked:
        if n - len(to_cut) <= n_play:
            break
        to_cut.add(i)
    verdicts = []
    for i, r in enumerate(per_entry):
        verdicts.append({"index": i, "verdict": "cut" if i in to_cut else "keep", **r})

    return {
        "entries": n,
        "target_count": n_play,
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
        "keep": sum(1 for v in verdicts if v["verdict"] == "keep"),
        "cut": len(to_cut),
    }


def audit_to_markdown(audit: dict[str, Any], *, max_verdicts: int = 40) -> str:
    if not audit.get("entries"):
        return "_No entries to audit._\n"
    p, s, h, sal = audit["pitchers"], audit["stacks"], audit["hitters"], audit["salary"]
    lines = [
        f"**Batch:** {audit['entries']} entries, playing {audit['target_count']}. Keep {audit['keep']}, cut {audit['cut']}.",
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
    cuts = [v for v in audit["verdicts"] if v["verdict"] == "cut"][:max_verdicts]
    if cuts:
        lines += ["", "**Cut:**"]
        for v in cuts:
            lines.append(
                f"- #{v['index'] + 1} {v['stack_type'] or ''} {v['stack'] or ''} / {'+'.join(v['pitchers'])}: "
                + ("; ".join(v["problems"]) or "portfolio trim")
            )
    return "\n".join(lines) + "\n"
