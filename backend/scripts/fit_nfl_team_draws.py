"""
Fits every constant in services/nfl_team_draws.py from real nflverse
play-by-play and schedules, and prints them next to what the module is
currently using -- run by hand before changing any of them.

It also re-derives the five places where the measurement contradicted
the design brief this layer was built to, so those are checkable rather
than taken on trust:

  1. Scoring counts are UNDER-dispersed (var/mean 0.82), so a negative
     binomial is structurally the wrong family.
  2. Leading teams run slightly MORE plays, not fewer.
  3. Opponents' play counts are negatively correlated (-0.48).
  4. The shared environment is already in the market total: residual
     opponent point correlation is only +0.05.
  5. QB-WR1 DK-point correlation is ~0.35, not the 0.60-0.70 the brief
     names as the validation target.

TWO TRAPS THIS SCRIPT EXISTS TO AVOID

Fit the scoring-opportunity count WITHIN implied-total buckets. Pooled,
n comes out 13.4 because the pooled variance is inflated by the spread
of MEANS across totals rather than the within-game variance a binomial
is meant to reproduce; simulated team points then land at sd 11.7
against a real 9.0. Within buckets it is 8-9.

Use the DROPBACK share, not the attempt share. A sack is a pass play, so
the split being modelled is dropbacks against rushes. Using attempts
hands the sacks to the run game and produces 31.2 attempts against a
real 33.0.

Usage (from backend/):
    .venv/Scripts/python.exe -m scripts.fit_nfl_team_draws [seasons...]
"""

from __future__ import annotations

import asyncio
import csv
import gzip
import io
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clients import nfl  # noqa: E402
from app.clients.http import get_bytes  # noqa: E402
from app.clients.nfl_pbp import PBP_URL_TEMPLATE  # noqa: E402
from app.services import nfl_dk_points as dk  # noqa: E402
from app.services import nfl_team_draws as L2  # noqa: E402

DEFAULT_SEASONS = (2022, 2023, 2024, 2025)
MIN_PLAYS = 30


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ols(x, y):
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    design = np.column_stack([np.ones_like(x), x])
    beta, *_ = np.linalg.lstsq(design, y, rcond=None)
    return float(beta[0]), float(beta[1]), float((y - design @ beta).std())


async def team_games(seasons) -> list[dict]:
    """One record per (game, team), with everything layer 2 draws."""
    out: list[dict] = []
    for season in seasons:
        schedule = {g["game_id"]: g for g in await nfl.get_schedule(season)}
        raw = await get_bytes(PBP_URL_TEMPLATE.format(season=season), source="nflverse pbp")
        text = gzip.decompress(raw).decode("utf-8")
        agg: dict = defaultdict(lambda: defaultdict(float))

        for row in csv.DictReader(io.StringIO(text)):
            gid, pos = row.get("game_id"), row.get("posteam")
            if not gid or not pos or gid not in schedule:
                continue
            a = agg[(gid, pos)]
            play_type = row.get("play_type")
            live = (
                play_type in ("pass", "run")
                and row.get("qb_kneel") != "1"
                and row.get("qb_spike") != "1"
            )
            if live:
                a["plays"] += 1
                if play_type == "pass":
                    if row.get("sack") == "1":
                        a["sacks"] += 1
                    else:
                        a["pass_att"] += 1
                    if row.get("interception") == "1":
                        a["ints"] += 1
                else:
                    a["rush_att"] += 1
                if row.get("fumble_lost") == "1":
                    a["fumbles_lost"] += 1
            if row.get("field_goal_result") == "made":
                a["fgs"] += 1
            if row.get("touchdown") == "1" and row.get("td_team") == pos:
                a["tds"] += 1
                if play_type == "pass":
                    a["pass_tds"] += 1
                elif play_type == "run":
                    a["rush_tds"] += 1

        for (gid, pos), a in agg.items():
            game = schedule[gid]
            home_score, away_score = game.get("home_score"), game.get("away_score")
            if home_score is None or away_score is None or a["plays"] < MIN_PLAYS:
                continue
            is_home = game["home_team"] == pos
            implied = nfl.implied_team_totals(game)
            own_implied = implied["home"] if is_home else implied["away"]
            if own_implied is None:
                continue
            points = home_score if is_home else away_score
            opponent = away_score if is_home else home_score
            out.append({
                **{k: a[k] for k in ("plays", "pass_att", "rush_att", "sacks", "ints",
                                     "fumbles_lost", "tds", "fgs", "pass_tds", "rush_tds")},
                "game_id": gid, "team": pos, "points": points,
                "margin": points - opponent, "implied": own_implied,
                "total": game.get("total_line"),
            })
    return out


def _row(label: str, measured: float, in_use: float, fmt: str = "8.3f") -> None:
    print(f"  {label:38s} measured {measured:{fmt}}   in use {in_use:{fmt}}")


async def main() -> int:
    seasons = [int(a) for a in sys.argv[1:]] or list(DEFAULT_SEASONS)
    rows = await team_games(seasons)
    by_game: dict = defaultdict(list)
    for r in rows:
        by_game[r["game_id"]].append(r)
    pairs = [(g[0], g[1]) for g in by_game.values() if len(g) == 2]
    print(f"{len(rows)} team-games across {seasons}\n")

    print("MARGINALS")
    plays = [r["plays"] for r in rows]
    _row("plays per team (mean)", st.mean(plays), L2.GAME_PLAYS_MEAN / 2)
    game_plays = [x["plays"] + y["plays"] for x, y in pairs]
    _row("plays per GAME (mean)", st.mean(game_plays), L2.GAME_PLAYS_MEAN)
    _row("plays per GAME (sd)", st.pstdev(game_plays), L2.GAME_PLAYS_SD)
    implied_split = (st.pstdev(plays) ** 2 - st.pstdev(game_plays) ** 2 / 4) ** 0.5
    _row("team play split sd", implied_split, L2.TEAM_PLAYS_SPLIT_SD)
    _row("plays per point of margin", _ols([r["margin"] for r in rows], plays)[1],
         L2.PLAYS_PER_MARGIN)

    dropback_share = [(r["pass_att"] + r["sacks"]) / r["plays"] for r in rows]
    _row("DROPBACK share of plays (mean)", st.mean(dropback_share), L2.PASS_SHARE_MEAN)
    _row("dropback share (sd)", st.pstdev(dropback_share), L2.PASS_SHARE_SD)
    a, b, sd = _ols([r["margin"] for r in rows], dropback_share)
    _row("dropback share per margin point", b, L2.PASS_SHARE_PER_MARGIN)
    _row("dropback share residual sd", sd, L2.PASS_SHARE_RESIDUAL_SD)
    attempt_share = [r["pass_att"] / (r["pass_att"] + r["rush_att"]) for r in rows]
    print(f"  (attempt share would be {st.mean(attempt_share):.4f} -- using it here loses "
          f"the sacks off the pass side)")

    _row("sacks per dropback",
         sum(r["sacks"] for r in rows) / sum(r["sacks"] + r["pass_att"] for r in rows),
         L2.SACK_RATE_PER_DROPBACK, "8.4f")
    _row("ints per attempt",
         sum(r["ints"] for r in rows) / sum(r["pass_att"] for r in rows),
         L2.INT_RATE_PER_ATTEMPT, "8.4f")
    _row("fumbles lost per play",
         sum(r["fumbles_lost"] for r in rows) / sum(plays),
         L2.FUMBLE_LOST_RATE_PER_PLAY, "8.4f")

    a_td, b_td, _ = _ols([r["implied"] for r in rows], [r["tds"] for r in rows])
    a_fg, b_fg, _ = _ols([r["implied"] for r in rows], [r["fgs"] for r in rows])
    _row("TD intercept", a_td, L2.TD_INTERCEPT)
    _row("TD slope per implied point", b_td, L2.TD_SLOPE, "8.4f")
    _row("FG intercept", a_fg, L2.FG_INTERCEPT)
    _row("FG slope per implied point", b_fg, L2.FG_SLOPE, "8.4f")
    print("  (the two are NOT proportional -- the TD/FG ratio runs 1.08 to 2.15 across totals)")

    residual = [7 * r["tds"] + 3 * r["fgs"] - r["points"] for r in rows]
    _row("points residual mean", -st.mean(residual), L2.POINTS_RESIDUAL_MEAN)
    _row("points residual sd", st.pstdev(residual), L2.POINTS_RESIDUAL_SD)

    rush_share_lo = [r for r in rows if r["margin"] < -7 and r["tds"] > 0]
    rush_share_hi = [r for r in rows if r["margin"] > 7 and r["tds"] > 0]
    lo = sum(r["rush_tds"] for r in rush_share_lo) / max(sum(r["tds"] for r in rush_share_lo), 1)
    hi = sum(r["rush_tds"] for r in rush_share_hi) / max(sum(r["tds"] for r in rush_share_hi), 1)
    print(f"  rush TD share: trailing {lo:.3f}  leading {hi:.3f}   "
          f"in use {L2.RUSH_TD_SHARE_BASE:.3f} +{L2.RUSH_TD_SHARE_PER_MARGIN:.4f}/pt")

    print("\nSCORING OPPORTUNITIES -- fit WITHIN implied buckets, never pooled")
    pooled_td = [r["tds"] for r in rows]
    pooled_n = st.mean(pooled_td) / (1 - st.pvariance(pooled_td) / st.mean(pooled_td))
    print(f"  pooled would say n = {pooled_n:.1f}  <- WRONG, inflated by the spread of means")
    within = []
    for lo_b, hi_b in [(18, 21), (21, 24), (24, 27), (27, 40)]:
        sel = [r["tds"] for r in rows if lo_b <= r["implied"] < hi_b]
        if len(sel) < 40:
            continue
        mean_td, var_td = st.mean(sel), st.pvariance(sel)
        p = 1 - var_td / mean_td
        if p > 0:
            within.append(mean_td / p)
            print(f"    implied {lo_b}-{hi_b}: mean {mean_td:.2f} var {var_td:.2f} "
                  f"-> n = {mean_td/p:5.1f}")
    if within:
        _row("scoring opportunities (median)", st.median(within), L2.DRIVES_PER_TEAM, "8.1f")

    td, fg = [r["tds"] for r in rows], [r["fgs"] for r in rows]
    cov = float(np.cov(td, fg, bias=True)[0, 1])
    n = L2.DRIVES_PER_TEAM
    plain = -n * (st.mean(td) / n) * (st.mean(fg) / n)
    print(f"  cov(TD, FG) {cov:+.3f}; a plain shared budget gives {plain:+.3f}")
    _row("FG opportunity cost of a TD", cov / plain if plain else float("nan"),
         L2.FG_OPPORTUNITY_COST)

    print("\nDISPERSION -- which distribution family each count needs")
    for key in ("tds", "fgs", "pass_tds", "rush_tds", "sacks", "ints"):
        v = [r[key] for r in rows]
        ratio = st.pvariance(v) / st.mean(v)
        verdict = ("UNDER-dispersed: a negative binomial cannot represent this"
                   if ratio < 0.95 else "at or above Poisson")
        print(f"  {key:10s} var/mean {ratio:5.3f}   {verdict}")

    print("\nJOINT STRUCTURE -- what the model must reproduce")
    def corr(key):
        return float(np.corrcoef([x[key] for x, _ in pairs], [y[key] for _, y in pairs])[0, 1])
    _row("corr(plays_A, plays_B)", corr("plays"), -0.480)
    _row("corr(points_A, points_B)", corr("points"), -0.028)
    _row("corr(TDs_A, TDs_B)  <- bring-back", corr("tds"), 0.121)
    _row("corr(pass attempts_A, _B)", corr("pass_att"), -0.119)
    share_a = [(x["pass_att"] + x["sacks"]) / x["plays"] for x, _ in pairs]
    share_b = [(y["pass_att"] + y["sacks"]) / y["plays"] for _, y in pairs]
    _row("corr(dropback share_A, _B)", float(np.corrcoef(share_a, share_b)[0, 1]), -0.241)
    resid_a = [x["points"] - x["implied"] for x, _ in pairs]
    resid_b = [y["points"] - y["implied"] for _, y in pairs]
    _row("corr(points-implied A, B)", float(np.corrcoef(resid_a, resid_b)[0, 1]),
         L2.SHARED_ENVIRONMENT_CORR)
    print("  NOTE the shootout is in SCORING, not volume: opponents' TDs correlate +0.12")
    print("  while their pass attempts correlate -0.12, and MORE negatively in high-total")
    print("  games. A double-stack pays because both offences score, not because both throw.")

    print("\nQB-WR1 AND FRIENDS -- the brief's validation target, measured")
    await _player_correlations(seasons)
    return 0


async def _player_correlations(seasons) -> None:
    pair_corrs: dict = defaultdict(list)
    for season in seasons:
        grouped = await nfl.get_grouped_season_stats(season)
        by_team: dict = defaultdict(lambda: defaultdict(dict))
        volume: dict = defaultdict(lambda: defaultdict(float))
        for pid, rows in grouped.items():
            for r in rows:
                team, week = r.get("team"), r.get("week")
                if not team or not week:
                    continue
                by_team[team][pid][week] = r
                position = r.get("position") or "?"
                if position == "QB":
                    volume[team]["QB:" + pid] += r["attempts"]
                elif position in ("WR", "TE", "RB"):
                    volume[team][position + ":" + pid] += r["targets"]

        for team, players in by_team.items():
            ranked = sorted(volume[team].items(), key=lambda kv: -kv[1])
            def top(prefix, rank=0):
                hits = [k.split(":", 1)[1] for k, _ in ranked if k.startswith(prefix)]
                return hits[rank] if len(hits) > rank else None

            qb = top("QB:")
            if not qb:
                continue
            for label, other in (("WR1", top("WR:", 0)), ("WR2", top("WR:", 1)),
                                 ("TE1", top("TE:", 0)), ("RB1", top("RB:", 0))):
                if not other:
                    continue
                weeks = sorted(set(players[qb]) & set(players[other]))
                if len(weeks) < 8:
                    continue
                a = [dk.game_points(players[qb][w]) for w in weeks]
                b = [dk.game_points(players[other][w]) for w in weeks]
                if np.std(a) > 0 and np.std(b) > 0:
                    pair_corrs[label].append(float(np.corrcoef(a, b)[0, 1]))

    for label in ("WR1", "WR2", "TE1", "RB1"):
        v = pair_corrs.get(label, [])
        if len(v) < 20:
            continue
        v.sort()
        print(f"  QB-{label:4s} n={len(v):4d}  mean {st.mean(v):+.3f}  median {st.median(v):+.3f}"
              f"  quartiles {v[len(v)//4]:+.3f} to {v[3*len(v)//4]:+.3f}")
    print("  The brief asserts 0.60-0.70 for QB-WR1. It is roughly half that, and getting")
    print("  there would mean stripping out the share variance layer 3 exists to provide.")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
