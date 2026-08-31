"""
Post-contest PROCESS audit of the user's own entries in one real DK
contest-standings export.

contest_results.py answers "what happened" (every player's real
ownership and points, the user's rank). This answers "what did the
user DO, and was it the right process" -- scored against the rules in
data/process_rules.py, and always shown next to the same measurement
for the whole field and for the contest's top-1% lineups, so a number
never floats without a reference.

Everything here is computable from the export alone. The one thing
the file doesn't carry is which TEAM each hitter plays for, which is
what stack analysis needs -- callers pass `team_by_name`
(normalized name -> team abbrev), built the same way
scripts/archive_contest_stacks.py builds it (from the MLB Stats API
slate for the contest's date, so it works for any past date). Without
it, the stack section is skipped rather than guessed.
"""

from __future__ import annotations

import statistics
from collections import Counter
from typing import Any

from app.data import process_rules as rules
from app.services.player_match import normalize_name


def _bucket(value: float | None, buckets: tuple[tuple[str, float, float], ...]) -> str | None:
    if value is None:
        return None
    for label, lo, hi in buckets:
        if lo <= value < hi:
            return label
    return None


def _mean(xs: list[float]) -> float | None:
    return round(statistics.fmean(xs), 2) if xs else None


def _pool_lookup(player_pool: list[dict[str, Any]]) -> dict[str, dict[str, float | None]]:
    """normalized name -> {ownership_pct, actual_fpts}. A player can be
    listed once per eligible roster position; the numbers are the same
    on every row, so the last one wins harmlessly."""
    out: dict[str, dict[str, float | None]] = {}
    for p in player_pool:
        out[p["normalized_name"]] = {
            "ownership_pct": p.get("ownership_pct"),
            "actual_fpts": p.get("actual_fpts"),
        }
    return out


def _is_mine(entry: dict[str, Any], handle: str | None, entry_ids: set[str]) -> bool:
    if entry.get("entry_id") and entry["entry_id"] in entry_ids:
        return True
    if handle:
        base = (entry.get("entry_name") or "").split(" (")[0].strip().lower()
        return base == handle
    return False


def _slots(entry: dict[str, Any], pool: dict[str, dict[str, float | None]]) -> list[dict[str, Any]]:
    """One row per roster slot of an entry, joined to the real
    ownership/points from the pool table."""
    rows = []
    for s in entry.get("lineup") or []:
        info = pool.get(s["normalized_name"]) or {}
        rows.append(
            {
                "slot": s["slot"],
                "name": s["name"],
                "normalized_name": s["normalized_name"],
                "is_pitcher": s["slot"] == "P",
                "ownership_pct": info.get("ownership_pct"),
                "actual_fpts": info.get("actual_fpts"),
            }
        )
    return rows


def _stack_groups(slots: list[dict[str, Any]], team_by_name: dict[str, str]) -> list[tuple[str, int]]:
    """[(team, hitters from that team), ...] largest first. Pitchers
    excluded -- a rostered SP isn't part of an offensive stack."""
    counts: Counter[str] = Counter()
    for s in slots:
        if s["is_pitcher"]:
            continue
        team = team_by_name.get(s["normalized_name"])
        if team:
            counts[team] += 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def _group_profile(
    entries: list[dict[str, Any]],
    pool: dict[str, dict[str, float | None]],
) -> dict[str, Any]:
    """Pitcher and hitter production/ownership profile for a set of
    entries -- the same shape for 'mine', 'field', and 'top 1%' so the
    three can sit side by side."""
    p_own: list[float] = []
    p_fp: list[float] = []
    h_own: list[float] = []
    h_fp: list[float] = []
    h_filler_fp: list[float] = []
    h_filler = 0
    h_total = 0
    h_zero = 0
    p_bust = 0
    p_total = 0
    for e in entries:
        for s in _slots(e, pool):
            own, fp = s["ownership_pct"], s["actual_fpts"]
            if s["is_pitcher"]:
                p_total += 1
                if own is not None:
                    p_own.append(own)
                if fp is not None:
                    p_fp.append(fp)
                    if fp < rules.PITCHER_BUST_FPTS:
                        p_bust += 1
            else:
                h_total += 1
                if own is not None:
                    h_own.append(own)
                    if own < rules.FILLER_OWN_PCT:
                        h_filler += 1
                        if fp is not None:
                            h_filler_fp.append(fp)
                if fp is not None:
                    h_fp.append(fp)
                    if fp <= 0:
                        h_zero += 1
    return {
        "entries": len(entries),
        "pitcher_avg_ownership_pct": _mean(p_own),
        "pitcher_avg_fpts": _mean(p_fp),
        "pitcher_bust_rate_pct": round(100 * p_bust / p_total, 1) if p_total else None,
        "hitter_avg_ownership_pct": _mean(h_own),
        "hitter_avg_fpts": _mean(h_fp),
        "hitter_zero_rate_pct": round(100 * h_zero / h_total, 1) if h_total else None,
        "hitter_filler_share_pct": round(100 * h_filler / h_total, 1) if h_total else None,
        "hitter_filler_avg_fpts": _mean(h_filler_fp),
    }


def _by_ownership_bucket(
    entries: list[dict[str, Any]],
    pool: dict[str, dict[str, float | None]],
    *,
    pitchers: bool,
) -> list[dict[str, Any]]:
    buckets = rules.PITCHER_OWNERSHIP_BUCKETS if pitchers else rules.OWNERSHIP_BUCKETS
    acc: dict[str, list[float]] = {label: [] for label, _, _ in buckets}
    zeros: dict[str, int] = {label: 0 for label, _, _ in buckets}
    for e in entries:
        for s in _slots(e, pool):
            if s["is_pitcher"] != pitchers:
                continue
            label = _bucket(s["ownership_pct"], buckets)
            if label is None or s["actual_fpts"] is None:
                continue
            acc[label].append(s["actual_fpts"])
            if s["actual_fpts"] <= 0:
                zeros[label] += 1
    out = []
    for label, _, _ in buckets:
        n = len(acc[label])
        out.append(
            {
                "bucket": label,
                "slots": n,
                "avg_fpts": _mean(acc[label]),
                "zero_rate_pct": round(100 * zeros[label] / n, 1) if n else None,
            }
        )
    return out


def audit_contest(
    parsed: dict[str, list[dict[str, Any]]],
    *,
    handle: str | None = None,
    entry_ids: list[str] | None = None,
    team_by_name: dict[str, str] | None = None,
    cash_line_fraction: float = rules.CASH_LINE_FRACTION,
) -> dict[str, Any]:
    """
    Audit the user's entries in one parsed standings export
    (contest_results.parse_contest_standings()).

    Identifies the user's entries by `entry_ids` (exact) and/or
    `handle` (EntryName's "handle (rank/total)" base, case-insensitive,
    the same best-effort match contest_results.find_my_entry() makes
    -- except this returns ALL of the user's entries, since the whole
    point is the portfolio, not one lineup).
    """
    entries = [e for e in parsed.get("entries") or [] if e.get("rank") is not None]
    pool = _pool_lookup(parsed.get("player_pool") or [])
    ids = set(entry_ids or [])
    handle_l = handle.strip().lower() if handle else None
    mine = [e for e in entries if _is_mine(e, handle_l, ids)]
    field_size = len(entries)

    if not mine:
        return {
            "found": False,
            "field_size": field_size,
            "reason": "None of the entries matched the handle / entry ids given.",
        }

    ranked = sorted(entries, key=lambda e: e["rank"])
    top_n = max(20, int(field_size * rules.TOP_SLICE_FRACTION))
    top_slice = [e for e in ranked if e["rank"] <= top_n]
    cash_rank = max(1, int(field_size * cash_line_fraction))
    field_points = [e["points"] for e in entries if e.get("points") is not None]
    my_points = [e["points"] for e in mine if e.get("points") is not None]
    cashed = sum(1 for e in mine if e["rank"] <= cash_rank)

    summary = {
        "field_size": field_size,
        "my_entries": len(mine),
        "my_unique_lineups": len({tuple(s["normalized_name"] for s in (e.get("lineup") or [])) for e in mine if e.get("lineup")}),
        "best_rank": min(e["rank"] for e in mine),
        "best_points": max(my_points) if my_points else None,
        "winning_points": ranked[0]["points"] if ranked else None,
        "cash_line_rank": cash_rank,
        "cash_line_points": ranked[cash_rank - 1]["points"] if len(ranked) >= cash_rank else None,
        "cashed": cashed,
        "cash_rate_pct": round(100 * cashed / len(mine), 1),
        "avg_percentile": round(100 * statistics.fmean(e["rank"] / field_size for e in mine), 1),
        "my_median_points": round(statistics.median(my_points), 1) if my_points else None,
        "field_median_points": round(statistics.median(field_points), 1) if field_points else None,
    }

    profiles = {
        "mine": _group_profile(mine, pool),
        "top_1pct": _group_profile(top_slice, pool),
        "field": _group_profile(entries, pool),
    }

    # --- pitchers: who, how often, how they did ---
    p_counter: Counter[str] = Counter()
    p_display: dict[str, str] = {}
    for e in mine:
        for s in _slots(e, pool):
            if s["is_pitcher"]:
                p_counter[s["normalized_name"]] += 1
                p_display[s["normalized_name"]] = s["name"]
    my_pitchers = [
        {
            "name": p_display[k],
            "entries": n,
            "ownership_pct": (pool.get(k) or {}).get("ownership_pct"),
            "actual_fpts": (pool.get(k) or {}).get("actual_fpts"),
        }
        for k, n in p_counter.most_common()
    ]
    slate_pitchers = sorted(
        (
            {"name": p["name"], "ownership_pct": p.get("ownership_pct"), "actual_fpts": p.get("actual_fpts")}
            for p in parsed.get("player_pool") or []
            if (p.get("position") or "").upper() == "P" and p.get("actual_fpts") is not None
        ),
        key=lambda r: -(r["actual_fpts"] or 0),
    )
    # dedupe by name, keep first (highest)
    seen: set[str] = set()
    best_pitchers = []
    for r in slate_pitchers:
        if r["name"] in seen:
            continue
        seen.add(r["name"])
        best_pitchers.append(r)
        if len(best_pitchers) == 5:
            break

    # --- hitters: heaviest exposures ---
    h_counter: Counter[str] = Counter()
    h_display: dict[str, str] = {}
    for e in mine:
        for s in _slots(e, pool):
            if not s["is_pitcher"]:
                h_counter[s["normalized_name"]] += 1
                h_display[s["normalized_name"]] = s["name"]
    top_exposures = [
        {
            "name": h_display[k],
            "entries": n,
            "ownership_pct": (pool.get(k) or {}).get("ownership_pct"),
            "actual_fpts": (pool.get(k) or {}).get("actual_fpts"),
            "team": (team_by_name or {}).get(k),
        }
        for k, n in h_counter.most_common(12)
    ]

    # --- stacks (needs teams) ---
    stacks: dict[str, Any] | None = None
    if team_by_name:
        primary: Counter[str] = Counter()
        primary_pts: dict[str, list[float]] = {}
        max_sizes: list[int] = []
        four_plus = 0
        for e in mine:
            slots = _slots(e, pool)
            groups = _stack_groups(slots, team_by_name)
            if not groups:
                continue
            team, size = groups[0]
            max_sizes.append(size)
            if size >= rules.STACK_MIN_SIZE:
                four_plus += 1
                primary[team] += 1
                pts = sum(
                    (s["actual_fpts"] or 0.0)
                    for s in slots
                    if not s["is_pitcher"] and team_by_name.get(s["normalized_name"]) == team
                )
                primary_pts.setdefault(team, []).append(pts)

        top_primary: Counter[str] = Counter()
        top_primary_pts: dict[str, list[float]] = {}
        for e in top_slice:
            slots = _slots(e, pool)
            groups = _stack_groups(slots, team_by_name)
            if groups and groups[0][1] >= rules.STACK_MIN_SIZE:
                team = groups[0][0]
                top_primary[team] += 1
                pts = sum(
                    (s["actual_fpts"] or 0.0)
                    for s in slots
                    if not s["is_pitcher"] and team_by_name.get(s["normalized_name"]) == team
                )
                top_primary_pts.setdefault(team, []).append(pts)

        n_mine = len(mine)
        top_share = (primary.most_common(1)[0][1] / n_mine) if primary and n_mine else 0.0
        stacks = {
            "entries_with_4plus_stack_pct": round(100 * four_plus / n_mine, 1) if n_mine else None,
            "avg_max_stack": _mean([float(x) for x in max_sizes]),
            "distinct_primary_stacks": len(primary),
            "recommended_max_distinct": rules.max_distinct_primary_stacks(n_mine),
            "top_stack_share_pct": round(100 * top_share, 1),
            "my_primary_stacks": [
                {"team": t, "entries": n, "avg_stack_fpts": _mean(primary_pts.get(t, []))}
                for t, n in primary.most_common(8)
            ],
            "top_1pct_primary_stacks": [
                {"team": t, "entries": n, "avg_stack_fpts": _mean(top_primary_pts.get(t, []))}
                for t, n in top_primary.most_common(5)
            ],
        }

    audit = {
        "found": True,
        "summary": summary,
        "profiles": profiles,
        "pitcher_buckets": _by_ownership_bucket(mine, pool, pitchers=True),
        "hitter_buckets": _by_ownership_bucket(mine, pool, pitchers=False),
        "my_pitchers": my_pitchers,
        "best_pitchers_on_slate": best_pitchers,
        "top_exposures": top_exposures,
        "stacks": stacks,
    }
    audit["flags"] = _flags(audit)
    return audit


def _flags(audit: dict[str, Any]) -> list[dict[str, str]]:
    """Plain-language findings, severity-tagged, derived from the
    numbers above against the rules. Kept deterministic so the same
    file always produces the same flags."""
    flags: list[dict[str, str]] = []
    mine = audit["profiles"]["mine"]
    top = audit["profiles"]["top_1pct"]
    field = audit["profiles"]["field"]
    n = audit["summary"]["my_entries"]

    # Pitching
    p_distinct = len(audit["my_pitchers"])
    p_max = rules.max_distinct_pitchers(n)
    if p_distinct > p_max:
        flags.append(
            {
                "severity": "high",
                "area": "pitching",
                "text": f"{p_distinct} different pitchers across {n} entries (target <= {p_max}). The pitcher core isn't a core.",
            }
        )
    lev = [p for p in audit["my_pitchers"] if (p["ownership_pct"] or 0) < rules.PITCHER_LEVERAGE_OWN_PCT]
    lev_entries = sum(p["entries"] for p in lev)
    if lev_entries and n and lev_entries / (2 * n) >= 0.25:
        avg = _mean([p["actual_fpts"] for p in lev if p["actual_fpts"] is not None])
        flags.append(
            {
                "severity": "high",
                "area": "pitching",
                "text": (
                    f"{lev_entries} of {2 * n} pitcher slots were arms under {rules.PITCHER_LEVERAGE_OWN_PCT:.0f}% owned "
                    f"(they averaged {avg} pts). Leverage belongs with bats, not arms."
                ),
            }
        )
    if mine["pitcher_bust_rate_pct"] is not None and top["pitcher_bust_rate_pct"] is not None:
        if mine["pitcher_bust_rate_pct"] >= top["pitcher_bust_rate_pct"] + 10:
            flags.append(
                {
                    "severity": "medium",
                    "area": "pitching",
                    "text": (
                        f"{mine['pitcher_bust_rate_pct']}% of your pitcher slots scored under {rules.PITCHER_BUST_FPTS:.0f} pts; "
                        f"the top 1% had {top['pitcher_bust_rate_pct']}%, the field {field['pitcher_bust_rate_pct']}%."
                    ),
                }
            )

    # Filler
    if mine["hitter_filler_share_pct"] is not None and mine["hitter_filler_share_pct"] >= 20:
        flags.append(
            {
                "severity": "high",
                "area": "hitters",
                "text": (
                    f"{mine['hitter_filler_share_pct']}% of your hitter slots were under {rules.FILLER_OWN_PCT:.0f}% owned "
                    f"(avg {mine['hitter_filler_avg_fpts']} pts). Field: {field['hitter_filler_share_pct']}%, top 1%: {top['hitter_filler_share_pct']}%."
                ),
            }
        )

    # Conviction
    st = audit.get("stacks")
    if st:
        if st["distinct_primary_stacks"] > st["recommended_max_distinct"]:
            flags.append(
                {
                    "severity": "medium",
                    "area": "conviction",
                    "text": (
                        f"{st['distinct_primary_stacks']} distinct primary stacks across {n} entries "
                        f"(target <= {st['recommended_max_distinct']}). Your best idea is carrying too little weight."
                    ),
                }
            )
        if st["top_stack_share_pct"] < 100 * rules.TOP_STACK_MIN_SHARE and n >= 10:
            flags.append(
                {
                    "severity": "medium",
                    "area": "conviction",
                    "text": f"Most-used stack was in only {st['top_stack_share_pct']}% of entries (target {100 * rules.TOP_STACK_MIN_SHARE:.0f}%+).",
                }
            )
        if st["entries_with_4plus_stack_pct"] is not None and st["entries_with_4plus_stack_pct"] < 50 and n >= 5:
            flags.append(
                {
                    "severity": "medium",
                    "area": "stacks",
                    "text": f"Only {st['entries_with_4plus_stack_pct']}% of entries carried a {rules.STACK_MIN_SIZE}+ man stack.",
                }
            )
        # Right team, wrong hitters: a primary stack whose avg points trail
        # what the top 1% got from the SAME team by a wide margin.
        top_by_team = {r["team"]: r["avg_stack_fpts"] for r in st["top_1pct_primary_stacks"]}
        for r in st["my_primary_stacks"]:
            t_pts = top_by_team.get(r["team"])
            if t_pts and r["avg_stack_fpts"] is not None and r["avg_stack_fpts"] < 0.6 * t_pts:
                flags.append(
                    {
                        "severity": "medium",
                        "area": "stacks",
                        "text": (
                            f"{r['team']}: right team, wrong bats -- your {r['team']} stacks averaged {r['avg_stack_fpts']} pts, "
                            f"the top 1%'s {r['team']} stacks averaged {t_pts}."
                        ),
                    }
                )

    if not flags:
        flags.append({"severity": "ok", "area": "process", "text": "No process rule was tripped in this contest."})
    return flags


def audit_to_markdown(audit: dict[str, Any], *, contest_name: str = "Contest") -> str:
    """Compact, readable report -- the same thing the Results tab
    renders, and the block the briefs feed to Claude."""
    if not audit.get("found"):
        return f"## {contest_name}\n\n_{audit.get('reason', 'no entries found')}_\n"
    s = audit["summary"]
    pm, pt, pf = audit["profiles"]["mine"], audit["profiles"]["top_1pct"], audit["profiles"]["field"]
    lines = [
        f"## {contest_name}",
        "",
        f"- {s['my_entries']} entries in a {s['field_size']:,}-entry field; best rank {s['best_rank']} ({s['best_points']} pts, winner {s['winning_points']}).",
        f"- Cashed {s['cashed']}/{s['my_entries']} ({s['cash_rate_pct']}%) at an approximate cash line of rank {s['cash_line_rank']} ({s['cash_line_points']} pts).",
        f"- Your median {s['my_median_points']} vs field median {s['field_median_points']}; average finish percentile {s['avg_percentile']}.",
        "",
        "| | Mine | Top 1% | Field |",
        "|---|---|---|---|",
        f"| Pitcher avg pts | {pm['pitcher_avg_fpts']} | {pt['pitcher_avg_fpts']} | {pf['pitcher_avg_fpts']} |",
        f"| Pitcher bust rate (<{rules.PITCHER_BUST_FPTS:.0f}) | {pm['pitcher_bust_rate_pct']}% | {pt['pitcher_bust_rate_pct']}% | {pf['pitcher_bust_rate_pct']}% |",
        f"| Pitcher avg own% | {pm['pitcher_avg_ownership_pct']} | {pt['pitcher_avg_ownership_pct']} | {pf['pitcher_avg_ownership_pct']} |",
        f"| Hitter avg pts | {pm['hitter_avg_fpts']} | {pt['hitter_avg_fpts']} | {pf['hitter_avg_fpts']} |",
        f"| Hitter zero rate | {pm['hitter_zero_rate_pct']}% | {pt['hitter_zero_rate_pct']}% | {pf['hitter_zero_rate_pct']}% |",
        f"| Hitter slots <{rules.FILLER_OWN_PCT:.0f}% owned | {pm['hitter_filler_share_pct']}% | {pt['hitter_filler_share_pct']}% | {pf['hitter_filler_share_pct']}% |",
        "",
        "**Pitchers used:** "
        + ", ".join(
            f"{p['name']} x{p['entries']} ({p['ownership_pct']}% / {p['actual_fpts']})" for p in audit["my_pitchers"]
        ),
        "**Best arms on the slate:** "
        + ", ".join(f"{p['name']} ({p['ownership_pct']}% / {p['actual_fpts']})" for p in audit["best_pitchers_on_slate"]),
        "",
        "**Pitcher points by ownership bucket:** "
        + ", ".join(f"{b['bucket']}: {b['avg_fpts']} avg over {b['slots']} slots" for b in audit["pitcher_buckets"] if b["slots"]),
        "**Hitter points by ownership bucket:** "
        + ", ".join(
            f"{b['bucket']}: {b['avg_fpts']} avg, {b['zero_rate_pct']}% zero ({b['slots']})"
            for b in audit["hitter_buckets"]
            if b["slots"]
        ),
    ]
    st = audit.get("stacks")
    if st:
        lines += [
            "",
            f"**Stacks:** {st['entries_with_4plus_stack_pct']}% of entries had a 4+ stack; "
            f"{st['distinct_primary_stacks']} distinct primary stacks (target <= {st['recommended_max_distinct']}); "
            f"top stack in {st['top_stack_share_pct']}% of entries.",
            "- Mine: " + ", ".join(f"{r['team']} x{r['entries']} ({r['avg_stack_fpts']} pts)" for r in st["my_primary_stacks"]),
            "- Top 1%: " + ", ".join(f"{r['team']} x{r['entries']} ({r['avg_stack_fpts']} pts)" for r in st["top_1pct_primary_stacks"]),
        ]
    lines += ["", "**Flags:**"]
    for f in audit["flags"]:
        lines.append(f"- [{f['severity']}] {f['text']}")
    return "\n".join(lines) + "\n"


def team_map_from_slate(slate: dict[str, Any]) -> dict[str, str]:
    """normalized hitter name -> team abbrev from a built slate (the
    same thing scripts/archive_contest_stacks.py does, exposed so the
    router and the briefs can share it)."""
    out: dict[str, str] = {}
    for game in slate.get("games") or []:
        for side in ("home", "away"):
            team = (game.get(side) or {}).get("abbrev")
            if not team:
                continue
            for h in (game[side].get("hitters") or []):
                out[normalize_name(h["name"])] = team
    return out
