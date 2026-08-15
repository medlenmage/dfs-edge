"""
Claude-powered slate analysis.

WHAT THIS DOES AND DOESN'T DO
-----------------------------
It does NOT ask Claude to invent statistics. Everything Claude sees is
already computed by the rest of the app -- real splits, real lines, real
weather. Claude's job is the part models are bad at: reading the whole
slate at once, spotting where the numbers disagree with each other, and
explaining the trade-offs in a sentence you can act on.

Feeding a model raw numbers and asking it to reason is reliable. Asking
it to recall a player's batting average from memory is not, and this
module never does that.

COST
----
A full slate summary is roughly 15-30k input tokens. On Sonnet that is
a few cents per run. The response is cached for 30 minutes so clicking
around the dashboard doesn't re-bill you.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.cache import cached
from app.config import get_settings

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a sharp, plain-spoken daily fantasy sports analyst \
helping one person build MLB lineups. You are looking at a single day's slate.

Ground rules:
- Every number you cite must come from the data provided. Never recall stats \
from memory and never invent a number. If something isn't in the data, say so.
- Write like you are talking to a friend who plays DFS seriously but is not a \
statistician. Short sentences. No jargon without a quick gloss.
- Be concrete. "Stack the Rockies" is useless. "Stack the Rockies -- 6.1 \
implied runs, Coors, and their top four bats all rate 70+ against a righty \
who allows a .310 OBP to lefties" is useful.
- Flag risk honestly: unconfirmed lineups, thin sample sizes, rain risk, a \
score driven mostly by park factor rather than skill.
- Distinguish cash-game plays (safe floor, high contact, top-of-order, good \
pitcher matchup) from tournament plays (upside, leverage, likely low-owned).
- If the slate is boring, say so. Do not manufacture excitement."""

USER_TEMPLATE = """Here is today's MLB slate data ({date}).

Every hitter has an "edge score" from 0-100 where 50 is a league-average \
matchup. It blends: how the hitter does against this pitcher's handedness, \
how vulnerable that pitcher is to that handedness, the team's Vegas implied \
run total, ballpark HR factor for that batter's hand, weather, recent form, \
and home/road split. The components are shown so you can see what is driving \
each score.

"stack_score" is the average edge score of a team's top five hitters -- \
higher means stacking that offence is more attractive.

DATA:
{data}

Write the analysis in this structure:

## The Slate in One Paragraph
What kind of slate is this? High scoring or suppressed? Where is the action?

## Top Stacks
The 2-3 offences worth stacking, with the specific reasons. Note if a lineup \
is not yet confirmed.

## Individual Bats I Like
4-6 hitters worth rostering, each with one line on why. Mark each as CASH, \
TOURNAMENT, or BOTH.

## Pitching
Which starting pitchers set up well, and which offences you want to avoid \
because of who is on the mound.

## Traps and Cautions
Where the edge score is misleading, thin samples, weather risk, anything \
that looks better on paper than it is.

## The One Thing
If you only take one idea off this page, what is it?"""


def _compact_slate(slate: dict[str, Any], top_n: int = 9) -> dict[str, Any]:
    """
    Trim the slate down to what's worth paying tokens for.

    The full slate object with every roster player and every component is
    enormous. We keep the top N hitters per team and drop the verbose
    component internals, keeping just the human-readable 'detail' strings.
    """
    games = []
    for g in slate.get("games") or []:
        entry: dict[str, Any] = {
            "matchup": f"{g['away']['name']} @ {g['home']['name']}",
            "time_utc": g.get("game_time_utc"),
            "venue": g["venue"]["name"],
            "roof_closed": g["venue"]["roof_closed"],
            "park_factors": g["venue"]["park_factors"],
            "elevation_ft": g["venue"]["elevation_ft"],
        }

        wx = g.get("weather") or {}
        if wx.get("temp_f") is not None:
            entry["weather"] = {
                "temp_f": wx.get("temp_f"),
                "wind": (wx.get("wind_effect") or {}).get("label"),
                "wind_mph": (wx.get("wind_effect") or {}).get("speed_mph"),
                "rain_chance_pct": wx.get("precip_chance_pct"),
                "carry": (wx.get("temperature_effect") or {}).get("label"),
            }

        bet = g.get("betting") or {}
        if bet.get("total") is not None:
            entry["betting"] = {
                "total": bet.get("total"),
                "home_ml": bet.get("home_moneyline"),
                "away_ml": bet.get("away_moneyline"),
            }

        for side in ("home", "away"):
            s = g[side]
            pitcher = s.get("probable_pitcher") or {}
            entry[side] = {
                "team": s.get("name"),
                "implied_runs": s.get("implied_runs"),
                "stack_score": s.get("stack_score"),
                "lineup_confirmed": s.get("lineup_confirmed"),
                "starting_pitcher": (
                    {
                        "name": pitcher.get("name"),
                        "throws": pitcher.get("throws"),
                        "era": (pitcher.get("season") or {}).get("era"),
                        "whip": (pitcher.get("season") or {}).get("whip"),
                        "k_per_9": (pitcher.get("season") or {}).get("k_per_9"),
                        "ops_allowed_vs_lhb": (pitcher.get("vs_lhb") or {}).get("ops_against"),
                        "ops_allowed_vs_rhb": (pitcher.get("vs_rhb") or {}).get("ops_against"),
                    }
                    if pitcher
                    else None
                ),
                "hitters": [
                    {
                        "name": h["name"],
                        "pos": h["position"],
                        "bats": h["bats"],
                        "order": h.get("batting_order"),
                        "score": h["edge"]["score"],
                        "top_driver": h["edge"]["top_driver"],
                        "season_ops": (h.get("season") or {}).get("ops"),
                        "season_pa": (h.get("season") or {}).get("pa"),
                        "vs_hand": h["edge"]["components"]["platoon"]["detail"],
                        "vs_pitcher": h["edge"]["components"]["pitcher"]["detail"],
                        "form": h["edge"]["components"]["form"]["detail"],
                    }
                    for h in (s.get("hitters") or [])[:top_n]
                ],
            }
        games.append(entry)

    return {"date": slate.get("date"), "games": games}


async def analyse_slate(
    slate: dict[str, Any], *, force: bool = False, question: str | None = None
) -> dict[str, Any]:
    """Run the slate past Claude and return the written analysis."""
    settings = get_settings()
    if not settings.has_claude:
        return {
            "available": False,
            "reason": "No ANTHROPIC_API_KEY set. Add one to .env to enable AI analysis.",
        }

    compact = _compact_slate(slate)
    cache_key = f"analysis:mlb:{slate.get('date')}:{hash(question or '') & 0xFFFF}"

    async def _load() -> Any:
        return await _call_claude(compact, question)

    try:
        result = await cached(cache_key, 1800, _load, force=force)
    except Exception as exc:  # noqa: BLE001
        log.exception("Claude analysis failed")
        return {"available": False, "reason": f"Analysis failed: {exc}"}

    return {"available": True, **result}


async def _call_claude(compact: dict[str, Any], question: str | None) -> dict[str, Any]:
    from anthropic import AsyncAnthropic

    settings = get_settings()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    prompt = USER_TEMPLATE.format(
        date=compact.get("date"),
        data=json.dumps(compact, indent=1, default=str),
    )
    if question:
        prompt += (
            f"\n\nThe user also asked specifically: {question}\n"
            "Answer that directly at the top, then give the full analysis."
        )

    message = await client.messages.create(
        model=settings.anthropic_model,
        max_tokens=4000,
        thinking={"type": "disabled"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    text = "".join(
        block.text for block in message.content if getattr(block, "type", "") == "text"
    )

    return {
        "text": text,
        "model": settings.anthropic_model,
        "input_tokens": message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
        "estimated_cost_usd": round(
            message.usage.input_tokens / 1_000_000 * 3
            + message.usage.output_tokens / 1_000_000 * 15,
            4,
        ),
    }


async def ask_about_slate(slate: dict[str, Any], question: str) -> dict[str, Any]:
    """Free-form follow-up question about the current slate."""
    settings = get_settings()
    if not settings.has_claude:
        return {"available": False, "reason": "No ANTHROPIC_API_KEY set."}

    compact = _compact_slate(slate, top_n=12)

    async def _load() -> Any:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        message = await client.messages.create(
            model=settings.anthropic_model,
            max_tokens=2000,
            thinking={"type": "disabled"},
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Today's slate data:\n{json.dumps(compact, indent=1, default=str)}"
                        f"\n\nQuestion: {question}\n\n"
                        "Answer using only the data above. Be direct and brief."
                    ),
                }
            ],
        )
        text = "".join(
            b.text for b in message.content if getattr(b, "type", "") == "text"
        )
        return {"text": text, "model": settings.anthropic_model}

    key = f"analysis:ask:{slate.get('date')}:{abs(hash(question)) & 0xFFFFFF}"
    try:
        result = await cached(key, 1800, _load)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": str(exc)}
    return {"available": True, **result}
