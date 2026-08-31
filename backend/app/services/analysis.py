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
a few cents per run. The response is cached for a full day (see
_ANALYSIS_TTL) -- keyed by date, so a new day's slate always gets a
fresh read, but reopening the app or revisiting the tab any number of
times the same day reuses the same cached write-up instead of paying
for a new one. The frontend's "Regenerate" button (force=True) is the
only thing that ever triggers a genuinely fresh call within that
window.
"""

from __future__ import annotations

import asyncio
import functools
import glob
import json
import logging
import os
import shutil
import subprocess
from typing import Any

from app.cache import cached
from app.config import get_settings

log = logging.getLogger(__name__)

# A full day, not 30 minutes -- the cache key is already scoped to the
# date, so this only controls how long a SAME-day write-up survives
# being asked for again (reopening the app, revisiting the tab). A
# genuinely fresh read is always one "Regenerate" click away.
_ANALYSIS_TTL = 86400
_ASK_TTL = 86400

SYSTEM_PROMPT = """You are a sharp, plain-spoken daily fantasy sports analyst \
helping one person build MLB lineups. You are looking at a single day's slate.

Ground rules:
- Every number you cite must come from the data provided. Never recall stats \
from memory and never invent a number. If something isn't in the data, say so.
- A team's hitters NEVER face that same team's own starting pitcher -- \
teammates are not each other's opponents. Each game entry's "home" and \
"away" objects each list that side's own pitcher alongside that side's own \
hitters purely because they play for the same team; the hitters actually \
bat against the OTHER side's pitcher, named explicitly in each side's \
"opposing_pitcher_these_hitters_actually_face" field. Always use that field \
to say who a hitter is facing, never the pitcher listed in that same team's \
object.
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

    ONLY the games on the DraftKings slate being played are included.
    Every consumer of this function is a Claude prompt about lineups the
    user is going to enter, and a player in a game that isn't on the
    draft group cannot be rostered -- so an off-slate game is not extra
    context, it is a trap. This was a real, observed failure: on a
    12-game day with a 7-game DK slate, a brief ranked SD @ CIN as the
    second-best environment and named an Atlanta hitter as the day's
    trap, none of which were rosterable. It was subtle because the
    briefs' OWN additions (pitcher rankings, implied-run lists) filtered
    correctly while this shared block did not, so the prompt disagreed
    with itself.

    `in_slate` is None, not False, when no DK slate has been selected --
    then nothing is filtered and every game is included, which is the
    right answer for a day with no draft group loaded.
    """
    all_games = slate.get("games") or []
    on_slate = [g for g in all_games if g.get("in_slate") is not False]
    # If the DK slate mapped to nothing at all -- a draft group whose
    # game_pks didn't match any of today's games -- filtering would hand
    # Claude an empty prompt, which is far worse than an unfiltered one.
    # Fall back to every game and say the mapping failed.
    mapping_failed = bool(all_games) and not on_slate
    if mapping_failed:
        on_slate = all_games
    off_slate = len(all_games) - len(on_slate)

    games = []
    for g in on_slate:
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

        # Built as two independent per-team dicts, each holding ONLY
        # that team's own pitcher and hitters, invited a real,
        # observed failure mode: a model reading "home: {pitcher: X,
        # hitters: [...]}" as one sibling grouping tends to infer X
        # faces those hitters, when X is actually THAT team's own
        # starter -- his real opponents are the OTHER side's hitters.
        # `opposing_pitcher` makes the actual matchup explicit on each
        # side instead of relying on the model to cross-reference two
        # separate objects correctly.
        home_pitcher = (g["home"].get("probable_pitcher") or {}).get("name")
        away_pitcher = (g["away"].get("probable_pitcher") or {}).get("name")

        for side in ("home", "away"):
            s = g[side]
            pitcher = s.get("probable_pitcher") or {}
            opposing_pitcher_name = away_pitcher if side == "home" else home_pitcher
            entry[side] = {
                "team": s.get("name"),
                "implied_runs": s.get("implied_runs"),
                "stack_score": s.get("stack_score"),
                "lineup_confirmed": s.get("lineup_confirmed"),
                "this_teams_own_starting_pitcher_NOT_an_opponent_of_the_hitters_below": (
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
                "opposing_pitcher_these_hitters_actually_face": opposing_pitcher_name,
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

    out = {"date": slate.get("date"), "games": games}
    if mapping_failed:
        out["note"] = (
            "WARNING: none of today's games matched the loaded DraftKings slate, so every MLB "
            "game is listed below and some of them may not be rosterable. Treat the slate "
            "membership as unknown and say so rather than assuming."
        )
    elif off_slate:
        # Stated rather than silently dropped, so a reader of the prompt
        # can tell the difference between "this game doesn't exist" and
        # "this game isn't on your slate".
        out["note"] = (
            f"Only the {len(games)} games on the DraftKings slate being played are listed. "
            f"{off_slate} other MLB game(s) today are NOT on this slate -- their players "
            "cannot be rostered, so do not recommend or reference them."
        )
    return out


# ---------------------------------------------------------------------------
# Billing path: Claude subscription (Claude Code CLI) vs API key
# ---------------------------------------------------------------------------
#
# This is a single-user app running on its owner's own machine, and its
# owner already pays for a Claude subscription. Direct Anthropic API
# calls are always usage-billed on top of that -- a subscription cannot
# pay for them -- but the locally installed Claude Code CLI runs
# headless (`claude -p`) on the SAME login/subscription its interactive
# sessions use. So when a CLI is installed, analysis runs through it and
# the tokens draw on the subscription's own usage allowance instead of
# an API bill; the API-key path stays as the fallback and can be forced
# with ANALYSIS_PROVIDER=api. That trade is only appropriate because
# this is personal, local, single-user use -- anything hosted or
# multi-user belongs on API billing.


@functools.lru_cache(maxsize=1)
def find_claude_code() -> str | None:
    """
    Locate a Claude Code executable: CLAUDE_CODE_BIN if set, a `claude`
    on PATH, else the Claude desktop app's bundled runtime (it keeps
    versioned installs under %APPDATA%/Claude/claude-code/<version>/ --
    newest version wins). Cached: the answer can't change within one
    backend process, and has_claude asks on every /api/health.
    """
    settings = get_settings()
    if settings.claude_code_bin:
        return settings.claude_code_bin if os.path.isfile(settings.claude_code_bin) else None
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    appdata = os.getenv("APPDATA", "")
    if appdata:
        candidates = glob.glob(os.path.join(appdata, "Claude", "claude-code", "*", "claude.exe"))
        if candidates:
            # Version-shaped dirs ("2.1.247") sort correctly as int tuples.
            def _ver(path: str) -> tuple[int, ...]:
                name = os.path.basename(os.path.dirname(path))
                try:
                    return tuple(int(x) for x in name.split("."))
                except ValueError:
                    return (0,)

            return max(candidates, key=_ver)
    return None


def _resolve_provider() -> str | None:
    """"claude-code", "api", or None when neither path is available."""
    settings = get_settings()
    if settings.analysis_provider == "api":
        return "api" if settings.anthropic_api_key else None
    if settings.analysis_provider == "claude-code":
        return "claude-code" if find_claude_code() else None
    # auto: prefer the subscription-billed CLI, fall back to the API key.
    if find_claude_code():
        return "claude-code"
    return "api" if settings.anthropic_api_key else None


def _run_claude_code(binary: str, prompt: str, system_prompt: str, model: str) -> dict[str, Any]:
    """
    One headless Claude Code run, blocking (callers wrap in a thread).

    The prompt goes over STDIN -- a slate is 60-120KB of JSON, far past
    Windows' ~32K command-line limit. --disallowedTools strips the
    harness's file/bash/web tools so this behaves like the pure
    completion the API path makes: the model reads the data it was
    given and writes, nothing else.

    ANTHROPIC_API_KEY is stripped from the child environment on
    purpose -- the CLI prefers an env API key over the stored login
    when both exist, which would silently route this back onto API
    billing, the exact thing this path exists to avoid.
    """
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    proc = subprocess.run(
        [
            binary,
            "-p",
            "--output-format", "json",
            "--model", model,
            "--system-prompt", system_prompt,
            "--disallowedTools", "*",
            "--max-turns", "1",
        ],
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude -p exited {proc.returncode}: {(proc.stderr or proc.stdout or '').strip()[:500]}"
        )
    payload = json.loads(proc.stdout)
    if payload.get("is_error"):
        raise RuntimeError(f"claude -p reported an error: {str(payload.get('result'))[:500]}")
    usage = payload.get("usage") or {}
    return {
        "text": payload.get("result") or "",
        "model": model,
        "provider": "claude-code",
        "input_tokens": (usage.get("input_tokens") or 0)
        + (usage.get("cache_read_input_tokens") or 0)
        + (usage.get("cache_creation_input_tokens") or 0),
        "output_tokens": usage.get("output_tokens") or 0,
        # Covered by the subscription -- the CLI's own total_cost_usd is
        # the list-price equivalent, informational only, so the real
        # marginal dollar cost of this run is zero.
        "estimated_cost_usd": 0.0,
        "billing": "subscription",
    }


async def _call_claude_code(prompt: str, *, max_tokens_hint: int) -> dict[str, Any]:
    """Async wrapper: run the CLI in a worker thread (safe on Windows
    regardless of the event-loop flavor uvicorn picked)."""
    settings = get_settings()
    binary = find_claude_code()
    if not binary:
        raise RuntimeError("No Claude Code CLI found.")
    return await asyncio.to_thread(
        _run_claude_code, binary, prompt, SYSTEM_PROMPT, settings.anthropic_model
    )


async def analyse_slate(
    slate: dict[str, Any], *, force: bool = False, question: str | None = None
) -> dict[str, Any]:
    """Run the slate past Claude and return the written analysis."""
    provider = _resolve_provider()
    if provider is None:
        return {
            "available": False,
            "reason": (
                "No Claude access configured. Either install/log in to Claude Code "
                "(analysis then runs on your subscription) or set ANTHROPIC_API_KEY in .env."
            ),
        }

    compact = _compact_slate(slate)
    cache_key = f"analysis:mlb:{slate.get('date')}:{hash(question or '') & 0xFFFF}"

    async def _load() -> Any:
        if provider == "claude-code":
            prompt = USER_TEMPLATE.format(
                date=compact.get("date"),
                data=json.dumps(compact, indent=1, default=str),
            )
            if question:
                prompt += (
                    f"\n\nThe user also asked specifically: {question}\n"
                    "Answer that directly at the top, then give the full analysis."
                )
            return await _call_claude_code(prompt, max_tokens_hint=4000)
        return await _call_claude(compact, question)

    try:
        result = await cached(cache_key, _ANALYSIS_TTL, _load, force=force)
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
        "provider": "api",
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
    provider = _resolve_provider()
    if provider is None:
        return {"available": False, "reason": "No Claude access configured (Claude Code login or ANTHROPIC_API_KEY)."}

    compact = _compact_slate(slate, top_n=12)

    async def _load() -> Any:
        if provider == "claude-code":
            prompt = (
                f"Today's slate data:\n{json.dumps(compact, indent=1, default=str)}"
                f"\n\nQuestion: {question}\n\n"
                "Answer using only the data above. Be direct and brief."
            )
            return await _call_claude_code(prompt, max_tokens_hint=2000)

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
        return {"text": text, "model": settings.anthropic_model, "provider": "api"}

    key = f"analysis:ask:{slate.get('date')}:{abs(hash(question)) & 0xFFFFFF}"
    try:
        result = await cached(key, _ASK_TTL, _load)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": str(exc)}
    return {"available": True, **result}


async def complete(
    prompt: str,
    *,
    system_prompt: str = SYSTEM_PROMPT,
    max_tokens: int = 4000,
) -> dict[str, Any]:
    """
    One plain completion on whichever billing path is configured --
    the same provider resolution analyse_slate() uses (subscription-
    billed Claude Code CLI first, API key as fallback), but with a
    caller-supplied prompt and system prompt. services/briefs.py
    builds its morning and pre-lock reads on this rather than
    re-implementing the provider dance.

    Raises RuntimeError when no provider is available; callers decide
    whether that's fatal (a scheduled brief just logs and skips).
    """
    provider = _resolve_provider()
    if provider is None:
        raise RuntimeError(
            "No Claude access configured (Claude Code login or ANTHROPIC_API_KEY)."
        )
    settings = get_settings()
    if provider == "claude-code":
        binary = find_claude_code()
        if not binary:
            raise RuntimeError("No Claude Code CLI found.")
        return await asyncio.to_thread(
            _run_claude_code, binary, prompt, system_prompt, settings.anthropic_model
        )

    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    message = await client.messages.create(
        model=settings.anthropic_model,
        max_tokens=max_tokens,
        thinking={"type": "disabled"},
        system=system_prompt,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in message.content if getattr(b, "type", "") == "text")
    return {
        "text": text,
        "model": settings.anthropic_model,
        "provider": "api",
        "input_tokens": message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
        "estimated_cost_usd": round(
            message.usage.input_tokens / 1_000_000 * 3 + message.usage.output_tokens / 1_000_000 * 15, 4
        ),
    }
