"""
Season-long NFL: the draft board, VORP, tiers, and roster analysis.

This is the shared engine behind both the pre-draft board and the live
draft assistant. It is deliberately separate from everything under the
DFS side of the app -- season-long asks a different question (who is
worth the most over a whole season relative to what you could get for
free) than a DFS slate does (who is worth the most today relative to
salary).

WHERE THE NUMBERS COME FROM, stated plainly because it matters:

  - Projections are DraftKings' own, published on the Best Ball draft
    board (clients/dk_bestball.py). They are fantasy points PER GAME
    under DK scoring, which is full PPR.
  - Rank comes from two independent sources that are cross-checked
    against each other: DK's own board order and Sleeper's search_rank.
    Neither publishes a true measured ADP, so the merged number is
    called `consensus_rank`, not ADP.
  - There is no in-house season-long projection model here yet. Nothing
    below invents one or dresses DK's number up as ours.

THE ONE REAL CAVEAT: a league whose scoring is not full PPR is being
valued with full-PPR projections. Half-PPR and standard leagues push
pass-catching backs and high-volume slot receivers down relative to
this board. DK publishes only the final points-per-game number, not the
underlying receptions/yards, so there is no honest way to convert it --
the mismatch is DETECTED and reported on the board's `scoring_warning`
rather than silently papered over with a made-up adjustment.
"""

from __future__ import annotations

import logging
from typing import Any

from app.clients import dk_bestball, sleeper
from app.services.player_match import normalize_name

log = logging.getLogger(__name__)

# The positions a fantasy roster actually drafts.
FANTASY_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DST")

# Sleeper writes team defenses as DEF; DK writes DST. One canonical form.
_POSITION_ALIASES = {"DEF": "DST", "D/ST": "DST", "PK": "K"}

# Which positions a plain FLEX slot can hold. SUPER_FLEX adds QB.
FLEX_POSITIONS = ("RB", "WR", "TE")
SUPERFLEX_POSITIONS = ("QB", "RB", "WR", "TE")

# Tiers are cut at the largest real drops inside a position's draftable
# depth. Both numbers were set by running the real 2026 board: a
# median-gap threshold was tried first and rejected -- gaps deep in a
# position collapse to ~0.1, which drags any median-based threshold down
# far enough that nearly every top-15 player becomes his own tier, which
# is useless advice. Cutting at the N largest gaps instead controls the
# tier count directly and puts the breaks where the board actually
# cliffs.
_TIER_COUNT = 8
_TIER_SAMPLE_DEPTH = 48

# DraftKings Best Ball, expressed in the same shape a Sleeper league
# uses so it can go through league_shape() unchanged: 12 drafters, a
# 20-round snake, and a weekly scoring lineup of QB/2RB/3WR/TE/FLEX that
# DK fills for you. Scoring is full PPR, which is exactly what DK's own
# projections are -- so unlike a redraft league there is no scoring
# mismatch to warn about here.
BEST_BALL_LEAGUE = {
    "name": "DraftKings Best Ball",
    "total_rosters": 12,
    "roster_positions": ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX"]
    + ["BN"] * 12,
    "scoring_settings": {"rec": 1.0},
}

# Sleeper's default league is 12 teams; used only when a league's own
# settings are unavailable.
_DEFAULT_TEAMS = 12
_DEFAULT_STARTERS = {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "K": 1, "DST": 1}
_DEFAULT_FLEX = 1


def canonical_position(position: str | None) -> str | None:
    if not position:
        return None
    pos = str(position).strip().upper()
    return _POSITION_ALIASES.get(pos, pos)


# ---------------------------------------------------------------- league shape


def league_shape(league: dict[str, Any] | None) -> dict[str, Any]:
    """
    Reduce a Sleeper league to the few facts that change a player's
    value: how many teams, how many of each position start, how many
    flex slots (and whether one of them is a superflex), and whether
    scoring is full PPR.

    A missing or unreadable league falls back to a 12-team, 1QB/2RB/3WR/
    1TE/1FLEX league -- the most common shape -- and says so, so the
    board still works before any league is connected.
    """
    if not league:
        return {
            "teams": _DEFAULT_TEAMS,
            "starters": dict(_DEFAULT_STARTERS),
            "flex_slots": _DEFAULT_FLEX,
            "superflex_slots": 0,
            "ppr": 1.0,
            "name": None,
            "assumed": True,
        }

    starters: dict[str, int] = {}
    flex_slots = 0
    superflex_slots = 0
    for slot in league.get("roster_positions") or []:
        code = str(slot).strip().upper()
        if code in ("BN", "IR", "TAXI"):
            continue
        if code in ("FLEX", "WRRB_FLEX", "REC_FLEX", "WRRB", "WRTE_FLEX"):
            flex_slots += 1
            continue
        if code in ("SUPER_FLEX", "SUPERFLEX", "QB_FLEX"):
            superflex_slots += 1
            continue
        pos = canonical_position(code)
        if pos in FANTASY_POSITIONS:
            starters[pos] = starters.get(pos, 0) + 1

    scoring = league.get("scoring_settings") or {}
    try:
        ppr = float(scoring.get("rec", 0.0))
    except (TypeError, ValueError):
        ppr = 0.0

    return {
        "teams": int(league.get("total_rosters") or _DEFAULT_TEAMS),
        "starters": starters or dict(_DEFAULT_STARTERS),
        "flex_slots": flex_slots,
        "superflex_slots": superflex_slots,
        "ppr": ppr,
        "name": league.get("name"),
        "assumed": False,
    }


def scoring_warning(shape: dict[str, Any]) -> str | None:
    """DK's projections are full PPR. Say so when the league isn't."""
    if shape.get("assumed"):
        return None
    ppr = shape.get("ppr", 1.0)
    if ppr >= 0.99:
        return None
    kind = "half-PPR" if ppr >= 0.4 else ("standard (non-PPR)" if ppr <= 0.01 else f"{ppr} PPR")
    return (
        f"This league is {kind}, but the projections below are DraftKings' own, "
        "which are full PPR. Pass-catching backs and high-volume slot receivers "
        "are worth less here than this board shows. DraftKings publishes only "
        "the final points-per-game figure, not receptions, so there is no way "
        "to convert it accurately -- adjust those players down by judgement."
    )


# ---------------------------------------------------------------- the board


def _merge_sources(
    dk_board: list[dict[str, Any]], sleeper_players: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """
    Join DK's draft board to Sleeper's player records.

    Matched on normalized name plus position. Sleeper's ids to other
    systems are mostly null on its player records (verified -- Bijan
    Robinson carries no gsis_id or espn_id), so a name join is the only
    option; adding position to the key keeps it from pairing, say, a WR
    and a DB who share a name.

    A DK player with no Sleeper match still appears -- DK's board is the
    authority on who is actually draftable. The Sleeper side only adds
    context (age, experience, depth-chart order, injury status) and the
    second opinion on rank.
    """
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for pid, rec in sleeper_players.items():
        pos = canonical_position(rec.get("position"))
        name = rec.get("full_name") or f"{rec.get('first_name') or ''} {rec.get('last_name') or ''}"
        if not pos or not name.strip():
            continue
        key = (normalize_name(name), pos)
        prior = index.get(key)
        # Two players can normalize to the same name+position. Prefer the
        # one Sleeper ranks (an actual fantasy asset) over a namesake
        # buried on a practice squad.
        if prior is None or (rec.get("search_rank") or 10**9) < (prior.get("search_rank") or 10**9):
            index[key] = {**rec, "player_id": pid}

    merged = []
    for row in dk_board:
        pos = canonical_position(row.get("position"))
        if pos not in FANTASY_POSITIONS:
            continue
        s = index.get((normalize_name(row.get("name") or ""), pos)) or {}
        merged.append(
            {
                "name": row.get("name"),
                "position": pos,
                "team": row.get("team"),
                "bye_week": row.get("bye_week"),
                "dk_id": row.get("dk_id"),
                "sleeper_id": s.get("player_id"),
                "projection": row.get("dk_projection"),
                "dk_board_rank": row.get("board_rank"),
                "sleeper_rank": s.get("search_rank"),
                "injury_status": s.get("injury_status") or row.get("status"),
                "news_status": row.get("news_status"),
                "age": s.get("age"),
                "years_exp": s.get("years_exp"),
                "depth_chart_order": s.get("depth_chart_order"),
            }
        )
    return merged


def _consensus_rank(rows: list[dict[str, Any]]) -> None:
    """
    Blend DK's board order with Sleeper's search_rank into one number,
    in place.

    Both are rankings of the same pool from independent sources, and on
    the live 2026 board they agree closely at the top (both have Bijan
    Robinson 1st, Gibbs 2nd, Chase 3rd, Nacua 4th) -- which is the
    reason to trust the blend at all. Averaging the two ranks damps the
    idiosyncrasies of either. Where only one source ranks a player, that
    one is used alone.

    This is NOT ADP. Neither source publishes average draft position;
    both publish their own ordering. Named accordingly everywhere.
    """
    for r in rows:
        ranks = [x for x in (r.get("dk_board_rank"), r.get("sleeper_rank")) if x]
        r["consensus_rank"] = round(sum(ranks) / len(ranks), 1) if ranks else None


def _replacement_levels(
    by_position: dict[str, list[dict[str, Any]]], shape: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """
    Find each position's replacement level: the projection of the best
    player who will NOT be a weekly starter somewhere in the league.

    Base starters are simply teams x starters-at-that-position. Flex
    slots are then handed out one at a time, each going to whichever
    eligible position currently has the highest-projected player still
    unclaimed. That is a real allocation driven by this board's own
    numbers, rather than a hardcoded "flex is 60% RB" guess -- and it
    self-corrects for a season where, say, receivers are unusually deep.
    """
    counts = {pos: shape["teams"] * n for pos, n in shape["starters"].items()}
    for pos in by_position:
        counts.setdefault(pos, 0)

    flex_rounds = [
        (FLEX_POSITIONS, shape["teams"] * shape.get("flex_slots", 0)),
        (SUPERFLEX_POSITIONS, shape["teams"] * shape.get("superflex_slots", 0)),
    ]
    for eligible, total in flex_rounds:
        for _ in range(total):
            best_pos, best_val = None, None
            for pos in eligible:
                pool = by_position.get(pos) or []
                idx = counts.get(pos, 0)
                if idx >= len(pool):
                    continue
                val = pool[idx].get("projection")
                if val is None:
                    continue
                if best_val is None or val > best_val:
                    best_pos, best_val = pos, val
            if best_pos is None:
                break
            counts[best_pos] += 1

    levels: dict[str, dict[str, Any]] = {}
    for pos, pool in by_position.items():
        cutoff = counts.get(pos, 0)
        # The replacement player is the first one past the starter cutoff.
        # If a position's pool is shallower than the league starts, the
        # last ranked player is the floor -- and that position is scarce
        # by definition.
        if not pool:
            continue
        idx = min(cutoff, len(pool) - 1)
        baseline = pool[idx].get("projection")
        if baseline is None:
            known = [p["projection"] for p in pool if p.get("projection") is not None]
            baseline = known[-1] if known else 0.0
        levels[pos] = {
            "starters_league_wide": cutoff,
            "replacement_points": round(float(baseline), 2),
            "replacement_player": pool[idx].get("name"),
        }
    return levels


def _assign_tiers(pool: list[dict[str, Any]]) -> None:
    """
    Break a position's ranked list into tiers at its real cliffs.

    Draft strategy lives in those cliffs: they are the difference between
    "I can wait a round, six more like him are coming" and "he is the last
    one at this level, take him now."

    Method: look at the drop between each pair of neighbours inside the
    position's draftable depth, take the _TIER_COUNT - 1 largest drops,
    and cut there. Everything past the sampled depth becomes one final
    tier -- by that point the players are genuinely interchangeable and
    pretending otherwise would be false precision.
    """
    scored = [p for p in pool if p.get("projection") is not None]
    if len(scored) < 3:
        for p in pool:
            p["tier"] = 1
        return

    sample = scored[:_TIER_SAMPLE_DEPTH]
    gaps = [
        (sample[i]["projection"] - sample[i + 1]["projection"], i + 1)
        for i in range(len(sample) - 1)
    ]
    # Cut positions are the indices just after the biggest drops. Ties in
    # gap size break on the earlier index so the cut order is stable.
    cuts = {
        idx
        for _, idx in sorted(gaps, key=lambda g: (-g[0], g[1]))[: max(_TIER_COUNT - 1, 0)]
        if _ > 0
    }

    tier = 1
    for i, p in enumerate(scored):
        if i in cuts:
            tier += 1
        p["tier"] = tier
    # Past the sampled depth, and for anyone with no projection at all,
    # one shared trailing tier.
    trailing = tier + 1
    for p in scored[_TIER_SAMPLE_DEPTH:]:
        p["tier"] = trailing
    for p in pool:
        if p.get("projection") is None:
            p["tier"] = trailing


async def build_board(
    league: dict[str, Any] | None = None, *, force: bool = False
) -> dict[str, Any]:
    """
    The full season-long draft board: every draftable player with a
    projection, a consensus rank, VORP against this league's own
    replacement level, a position rank, and a tier.

    Pass a Sleeper league dict to value players for THAT league's shape
    (team count, starting requirements, superflex). Without one, a
    standard 12-team 1QB/2RB/3WR/1TE/1FLEX league is assumed and said so.
    """
    groups = await dk_bestball.get_best_ball_groups(force=force)
    if not groups:
        return {
            "players": [],
            "positions": {},
            "shape": league_shape(league),
            "source": None,
            "note": "DraftKings has no Best Ball draft group open right now.",
        }

    group = groups[0]
    dk_board, sleeper_players = await dk_bestball.get_board(
        group["draft_group_id"], force=force
    ), await sleeper.get_players(force=force)

    rows = _merge_sources(dk_board, sleeper_players)
    _consensus_rank(rows)

    shape = league_shape(league)

    by_position: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_position.setdefault(r["position"], []).append(r)
    for pos, pool in by_position.items():
        # Rank within position by projection; unprojected players sort last.
        pool.sort(key=lambda p: (p.get("projection") is None, -(p.get("projection") or 0)))
        for i, p in enumerate(pool, start=1):
            p["position_rank"] = i
        _assign_tiers(pool)

    levels = _replacement_levels(by_position, shape)
    for pos, pool in by_position.items():
        baseline = (levels.get(pos) or {}).get("replacement_points")
        for p in pool:
            proj = p.get("projection")
            p["vorp"] = round(proj - baseline, 2) if (proj is not None and baseline is not None) else None

    # Overall board order is VORP -- that is the whole point of computing
    # it. A player with no projection can't be valued and sorts last on
    # consensus rank so he is still findable.
    rows.sort(
        key=lambda p: (
            p.get("vorp") is None,
            -(p.get("vorp") or 0),
            p.get("consensus_rank") or 10**6,
        )
    )
    for i, r in enumerate(rows, start=1):
        r["overall_rank"] = i

    # DraftKings' Best Ball board is QB/RB/WR/TE only -- verified against
    # the live 2026 group, which carries 1,568 players and not a single
    # kicker or defense. A league that starts K or DST gets no projection
    # for those slots from this board, and is told so rather than being
    # shown a silently incomplete draft board.
    missing = [
        pos
        for pos in shape["starters"]
        if shape["starters"].get(pos) and pos not in by_position
    ]

    return {
        "players": rows,
        "positions": levels,
        "shape": shape,
        "missing_positions": missing,
        "missing_positions_note": (
            "DraftKings' Best Ball board covers QB/RB/WR/TE only, so this league's "
            + "/".join(missing)
            + " slots have no projection here. Draft those off your own read."
        )
        if missing
        else None,
        "scoring_warning": scoring_warning(shape),
        "source": {
            "projections": "DraftKings Best Ball board (points per game, full PPR)",
            "draft_group_id": group["draft_group_id"],
            "draft_group_label": group.get("label"),
            "rank": "Blend of DraftKings board order and Sleeper search rank -- not measured ADP",
        },
    }


# ---------------------------------------------------------------- roster analysis


def _fill_lineup(players: list[dict[str, Any]], shape: dict[str, Any]) -> dict[str, Any]:
    """
    Slot a roster into its best legal starting lineup by projection.

    Straight greedy by position and then by flex, which is optimal here
    because the slots are nested: every flex-eligible player is also
    eligible for exactly one dedicated slot, so taking the best at each
    dedicated position first can never strand a better flex option than
    the one greedy leaves behind.
    """
    pool = sorted(
        [p for p in players if p.get("projection") is not None],
        key=lambda p: -p["projection"],
    )
    used: set[int] = set()
    lineup: list[dict[str, Any]] = []

    for pos, count in shape["starters"].items():
        picked = 0
        for i, p in enumerate(pool):
            if picked >= count:
                break
            if i in used or p["position"] != pos:
                continue
            used.add(i)
            lineup.append({**p, "slot": pos})
            picked += 1

    for slot_name, eligible, count in (
        ("FLEX", FLEX_POSITIONS, shape.get("flex_slots", 0)),
        ("SUPERFLEX", SUPERFLEX_POSITIONS, shape.get("superflex_slots", 0)),
    ):
        picked = 0
        for i, p in enumerate(pool):
            if picked >= count:
                break
            if i in used or p["position"] not in eligible:
                continue
            used.add(i)
            lineup.append({**p, "slot": slot_name})
            picked += 1

    bench = [p for i, p in enumerate(pool) if i not in used]
    bench += [p for p in players if p.get("projection") is None]
    return {
        "starters": lineup,
        "bench": bench,
        "starting_points": round(sum(p["projection"] for p in lineup), 1),
    }


async def analyze_league(
    league_id: str, user_id: str | None = None, *, force: bool = False
) -> dict[str, Any]:
    """
    Full season-long read on a Sleeper league: every team's roster
    valued on the same board, this user's own team broken out, where it
    is strong and where it has holes relative to the rest of the league,
    bye-week pileups, and the best players nobody has rostered.

    Team strength is ranked WITHIN the league rather than reported as a
    raw number, because that is the only comparison that matters -- a
    projection total means nothing except against the eleven teams you
    actually have to beat.
    """
    league = await sleeper.get_league(league_id, force=force)
    rosters = await sleeper.get_rosters(league_id, force=force)
    members = await sleeper.get_league_users(league_id, force=force)
    board = await build_board(league, force=force)

    shape = board["shape"]
    by_sleeper_id = {p["sleeper_id"]: p for p in board["players"] if p.get("sleeper_id")}
    display = {u["user_id"]: (u.get("display_name") or u.get("username")) for u in members}

    rostered: set[str] = set()
    teams: list[dict[str, Any]] = []
    for r in rosters:
        ids = r.get("players") or []
        rostered.update(ids)
        players = [by_sleeper_id[i] for i in ids if i in by_sleeper_id]
        filled = _fill_lineup(players, shape)

        byes: dict[int, int] = {}
        for p in filled["starters"]:
            if p.get("bye_week"):
                byes[p["bye_week"]] = byes.get(p["bye_week"], 0) + 1

        teams.append(
            {
                "roster_id": r.get("roster_id"),
                "owner_id": r.get("owner_id"),
                "owner": display.get(r.get("owner_id")) or "unclaimed",
                "is_me": bool(user_id) and r.get("owner_id") == user_id,
                "record": {
                    "wins": (r.get("settings") or {}).get("wins"),
                    "losses": (r.get("settings") or {}).get("losses"),
                    "ties": (r.get("settings") or {}).get("ties"),
                },
                "player_count": len(ids),
                "valued_count": len(players),
                "starting_points": filled["starting_points"],
                "starters": filled["starters"],
                "bench": filled["bench"],
                "by_position": {
                    pos: round(
                        sum(p["projection"] for p in filled["starters"] if p["position"] == pos), 1
                    )
                    for pos in FANTASY_POSITIONS
                },
                "bye_pileups": {wk: n for wk, n in sorted(byes.items()) if n >= 3},
                "injuries": [
                    {"name": p["name"], "position": p["position"], "status": p["injury_status"]}
                    for p in players
                    if p.get("injury_status")
                ],
            }
        )

    teams.sort(key=lambda t: -t["starting_points"])
    for i, t in enumerate(teams, start=1):
        t["power_rank"] = i

    # Per-position rank within the league, so "my WRs are 9th of 12" is
    # sayable rather than just "my WRs total 51.3".
    for pos in FANTASY_POSITIONS:
        order = sorted(teams, key=lambda t: -t["by_position"].get(pos, 0))
        for i, t in enumerate(order, start=1):
            t.setdefault("position_rank", {})[pos] = i

    free_agents = [
        p
        for p in board["players"]
        if p.get("sleeper_id") not in rostered and p.get("vorp") is not None
    ][:40]

    me = next((t for t in teams if t["is_me"]), None)
    return {
        "league": {
            "id": league_id,
            "name": league.get("name"),
            "season": league.get("season"),
            "status": league.get("status"),
            "shape": shape,
        },
        "teams": teams,
        "me": me,
        "free_agents": free_agents,
        "scoring_warning": board.get("scoring_warning"),
        "missing_positions_note": board.get("missing_positions_note"),
        "source": board.get("source"),
    }
