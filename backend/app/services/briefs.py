"""
Scheduled briefs -- the two reads a day that turn the process rules
into a routine instead of a document.

MORNING (default 11:00 America/Chicago): the slate read. Ranks the
offensive environments, names the 2-3 pitcher core, flags traps, and
-- when yesterday's contest exports were uploaded -- opens with the
post-contest audit so the day starts from what actually happened.

PRE-LOCK (default 60 minutes before the DK Main slate locks): the
gatekeeper. Refreshes the slate, audits the latest contest build
against the rules (services/build_audit.py), lists scratches and line
movement since the morning, and asks for a cut/keep verdict.

Both are plain completions on services/analysis.complete() -- so they
run on the same subscription-billed Claude Code path the AI-analysis
tab uses -- and both are stored in the SQLite cache for two weeks
under `brief:{day}:{kind}`, with a rolling index so the Briefs tab can
list them. Either can also be run on demand from the API.

The timer is an asyncio loop wired into main.py's lifespan, the same
pattern as lineup_watch._poll_loop() and cache._housekeeping_loop().
It only runs while the backend is up -- which is the stated deal for
this app (runs on the owner's machine, left running). A fired brief is
recorded (`brief_fired:{day}:{kind}`) so a restart doesn't fire it
twice, and a missed window fires late rather than never: the morning
brief will still run at 2pm after a reboot, the pre-lock brief only
while the slate is still open.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from app import cache, history_db
from app.clients import draftkings
from app.config import get_settings
from app.data import process_rules as rules
from app.services import analysis, build_audit, mlb_slate
from app.services.analysis import _compact_slate

log = logging.getLogger(__name__)

_BRIEF_TTL = 14 * 24 * 3600
_INDEX_KEY = "briefs:index"
_INDEX_MAX = 60
_FIRED_TTL = 36 * 3600
_LATEST_BATCH_TTL = 20 * 3600

MORNING = "morning"
PRELOCK = "prelock"

SYSTEM_PROMPT = (
    analysis.SYSTEM_PROMPT
    + "\n\nYou are also this user's process coach. "
    + rules.RULES_TEXT
    + "\nWhen you make a recommendation, say which rule it comes from. When the data "
    "and a rule disagree, say so and pick a side."
)

_MORNING_TEMPLATE = """Morning brief for {date}. Lock is at {lock_local} local ({slate_label} slate, {game_count} games).

{results_block}

SLATE DATA (every number you use must come from here):
{data}

Write the brief in this structure, tight and scannable:

## Yesterday
If contest audits are present above, two to four sentences: what the process did right, what it did wrong, and the one rule to hold today. If none are present, say "No contests uploaded yesterday." and move on.

## Environments (ranked)
The 3-5 offensive spots worth stacking, ranked. For each: implied runs (open -> live), the starter they face and why he's targetable, park/weather, and which batting spots (1-5 only) to build from. Note unconfirmed lineups.

## Pitcher core
The 2-3 arms to build every lineup from, and why. Name the popular arm you would NOT fade. Name any arm the field will like that you would avoid, and say why in one line.

## Traps
What looks good on paper and isn't. Weather, thin samples, a stack whose ownership will be far above its ceiling.

## Build plan
Concrete: "N entries: stack A in X lineups, stack B in Y, pitcher combos P1+P2 and P1+P3." Follow the conviction rule.

## Check before lock
Three to five specific things to verify at the pre-lock read (a questionable lineup, a rain risk, a line to watch)."""

_PRELOCK_TEMPLATE = """Pre-lock brief for {date}. Lock in {minutes_to_lock} minutes ({lock_local} local, {slate_label} slate).

MORNING BRIEF SAID:
{morning_excerpt}

CHANGES SINCE THIS MORNING:
{changes_block}

BUILD AUDIT OF THE LATEST CONTEST BATCH:
{audit_block}

SLATE DATA (current):
{data}

Write the brief in this structure:

## Verdict
One paragraph. The audit has already SELECTED a portfolio -- the entries listed under "Portfolio to enter". Say whether that selection is worth entering as it stands, and if not, what has to change first. Do not re-derive the selection; argue with it.

## Fix before entering
A numbered list of specific actions on the SELECTED portfolio: which of its entries to drop, which pitcher to remove from the core, which stack to reweight, which hitter batting 7th to swap for the 2-hole bat on the same team. Reference the entry numbers the audit prints. If the audit says the selection misses a rule, lead with that.

## What moved
Scratches, lineup confirmations that changed a stack's value, line movement. Two to five bullets.

## Final pitcher core and stack weights
The exact core and weights to enter with."""


# ---------------------------------------------------------------- storage --


def _key(day: str, kind: str) -> str:
    return f"brief:{day}:{kind}"


def get_brief(day: str, kind: str) -> dict[str, Any] | None:
    return cache.get(_key(day, kind))


def list_briefs() -> list[dict[str, Any]]:
    return cache.get(_INDEX_KEY) or []


def _store(day: str, kind: str, payload: dict[str, Any]) -> None:
    cache.put(_key(day, kind), payload, _BRIEF_TTL)
    index = [e for e in (cache.get(_INDEX_KEY) or []) if not (e["date"] == day and e["kind"] == kind)]
    index.insert(0, {"date": day, "kind": kind, "generated_at": payload.get("generated_at")})
    cache.put(_INDEX_KEY, index[:_INDEX_MAX], _BRIEF_TTL)


def remember_latest_batch(day: str, batch_id: str, entries: list[dict[str, Any]], *, source: str) -> None:
    """Called by the router whenever a contest batch is built or
    reshaped, so the pre-lock brief knows what the user is about to
    play. Keeps the first 500 entries -- an audit of a 10,000-lineup
    contest build is a different question from an audit of the 20-150
    the user will enter, and the reshaped/kept portfolio is what
    lands here last."""
    # Your OWN lineups always survive the 500-entry cap, whatever their
    # simulated rank. Without this they are just 20 rows in a re-sorted
    # 5,000-lineup batch, and a portfolio that happened to simulate
    # poorly would silently drop out of the snapshot -- so the brief
    # would audit the field and never mention the lineups you were
    # actually about to enter. They lead the snapshot; the generated
    # field fills the rest in its existing order.
    mine = [e for e in entries if (e.get("source") or "generated") != "generated"]
    rest = [e for e in entries if (e.get("source") or "generated") == "generated"]
    snapshot = (mine + rest)[:500] if mine else entries[:500]

    cache.put(
        f"contest_batch_latest:{day}",
        {
            "batch_id": batch_id,
            "source": source,
            "entries": snapshot,
            "num_injected": len(mine),
            "total_entries": len(entries),
            "saved_at": datetime.now(timezone.utc).isoformat(),
        },
        _LATEST_BATCH_TTL,
    )
    cache.put(f"contest_batch_day:{batch_id}", day, _LATEST_BATCH_TTL)


def latest_batch(day: str) -> dict[str, Any] | None:
    return cache.get(f"contest_batch_latest:{day}")


def day_of_batch(batch_id: str) -> str | None:
    """Which day a batch id was recorded for (remember_latest_batch
    also writes a batch->day pointer), so a reshape of it lands on
    the same day's slot."""
    return cache.get(f"contest_batch_day:{batch_id}")


# ------------------------------------------------------------ slate info --


async def main_slate(day: str) -> dict[str, Any] | None:
    """The DK slate the briefs anchor to: the one labelled Main, else
    the biggest Classic slate of the day."""
    try:
        slates = await draftkings.get_slates(day)
    except Exception:  # noqa: BLE001
        log.exception("Couldn't fetch DK slates for %s", day)
        return None
    if not slates:
        return None
    main = next((s for s in slates if (s.get("label") or "").lower() == "main"), None)
    return main or max(slates, key=lambda s: s.get("game_count") or 0)


def _lock_time(slate: dict[str, Any] | None) -> datetime | None:
    raw = (slate or {}).get("start_time_utc")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _local(dt: datetime | None, tz: ZoneInfo) -> str:
    if not dt:
        return "unknown"
    # No %-I: Windows' strftime doesn't support it.
    return dt.astimezone(tz).strftime("%I:%M %p %Z").lstrip("0")


def _compact_for_brief(slate: dict[str, Any]) -> dict[str, Any]:
    """analysis._compact_slate() plus the pieces the briefs argue
    from that it drops: pitchers ranked by edge with ownership, and
    each side's open->live implied runs.

    Both halves filter to the DK slate being played -- _compact_slate
    does it for the games block, the loop below for these additions.
    They have to agree: when only this half filtered, briefs ranked
    environments and named traps from games that weren't on the slate."""
    compact = _compact_slate(slate, top_n=6)
    pitchers = []
    implied = []
    confirmed = 0
    total_sides = 0
    for g in slate.get("games") or []:
        if g.get("in_slate") is False:
            continue
        for side in ("home", "away"):
            t = g.get(side) or {}
            total_sides += 1
            confirmed += 1 if t.get("lineup_confirmed") else 0
            implied.append(
                {
                    "team": t.get("abbrev"),
                    "implied_open": t.get("vegas_implied_runs_open"),
                    "implied_live": t.get("implied_runs"),
                    "lineup_confirmed": bool(t.get("lineup_confirmed")),
                }
            )
            p = t.get("probable_pitcher") or {}
            if p:
                proj = p.get("projection") or {}
                pitchers.append(
                    {
                        "name": p.get("name"),
                        "team": t.get("abbrev"),
                        "vs": (g.get("away" if side == "home" else "home") or {}).get("abbrev"),
                        "edge": (p.get("edge") or {}).get("score"),
                        "salary": (p.get("salary") or {}).get("salary"),
                        "proj_fpts": proj.get("fpts"),
                        "own_pct": proj.get("ownership_pct"),
                        "inhouse_own_pct": proj.get("inhouse_ownership_pct"),
                        "k_note": ((p.get("edge") or {}).get("components") or {}).get("strikeout_potential", {}).get("detail")
                        if isinstance(((p.get("edge") or {}).get("components") or {}).get("strikeout_potential"), dict)
                        else None,
                    }
                )
    pitchers.sort(key=lambda r: -(r["edge"] or 0))
    compact["pitchers_ranked"] = pitchers[:14]
    compact["implied_runs"] = sorted(implied, key=lambda r: -(r["implied_live"] or 0))
    compact["lineups_confirmed"] = f"{confirmed}/{total_sides}"
    compact["scratches"] = cache.get(f"scratches:{slate.get('date')}") or []
    return compact


async def _yesterday_results_block(day: str) -> str:
    """The post-contest audits uploaded for the previous day, if any,
    already rendered as markdown by the upload endpoint."""
    prev = (date_cls.fromisoformat(day) - timedelta(days=1)).isoformat()
    audits = cache.get(f"contest_audits:{prev}") or []
    if not audits:
        return "CONTEST AUDITS FROM YESTERDAY: none uploaded."
    parts = [f"CONTEST AUDITS FROM YESTERDAY ({prev}):"]
    for a in audits[:6]:
        parts.append(a.get("markdown") or "")
    # Running record, if Supabase is on.
    try:
        history = await history_db.get_my_contest_history()
    except Exception:  # noqa: BLE001
        history = []
    if history:
        recent = history[:10]
        parts.append(
            "RUNNING RECORD (last 10 uploaded contests): "
            + "; ".join(f"{c.get('date')} rank {c.get('my_rank')}/{c.get('field_size')}" for c in recent)
        )
    return "\n".join(parts)


# --------------------------------------------------------------- briefs --


async def run_morning(day: str | None = None, *, force: bool = False) -> dict[str, Any]:
    settings = get_settings()
    tz = ZoneInfo(settings.brief_timezone)
    day = day or datetime.now(tz).date().isoformat()
    existing = get_brief(day, MORNING)
    if existing and not force:
        return existing

    slate_meta = await main_slate(day)
    lock = _lock_time(slate_meta)
    slate = await mlb_slate.build_slate(day, include_hitters=True, include_inhouse=True)
    compact = _compact_for_brief(slate)
    results_block = await _yesterday_results_block(day)

    prompt = _MORNING_TEMPLATE.format(
        date=day,
        lock_local=_local(lock, tz),
        slate_label=(slate_meta or {}).get("label") or "Main",
        game_count=(slate_meta or {}).get("game_count") or "?",
        results_block=results_block,
        data=json.dumps(compact, indent=1, default=str),
    )
    result = await analysis.complete(prompt, system_prompt=SYSTEM_PROMPT, max_tokens=4000)
    payload = {
        "kind": MORNING,
        "date": day,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lock_time_utc": lock.isoformat() if lock else None,
        "slate_label": (slate_meta or {}).get("label"),
        "lineups_confirmed": compact.get("lineups_confirmed"),
        **result,
    }
    _store(day, MORNING, payload)
    return payload


def _changes_block(day: str, slate: dict[str, Any], morning: dict[str, Any] | None) -> str:
    lines = []
    scratches = cache.get(f"scratches:{day}") or []
    if scratches:
        lines.append("Scratches: " + ", ".join(f"{s.get('name')} ({s.get('team')})" for s in scratches))
    moves = []
    for g in slate.get("games") or []:
        if g.get("in_slate") is False:
            continue
        for side in ("home", "away"):
            t = g.get(side) or {}
            o, c = t.get("vegas_implied_runs_open"), t.get("implied_runs")
            if o is not None and c is not None and abs(c - o) >= 0.3:
                moves.append(f"{t.get('abbrev')} {o} -> {c}")
    if moves:
        lines.append("Implied-run moves of 0.3+: " + ", ".join(moves))
    if morning:
        lines.append(f"Lineups confirmed this morning: {morning.get('lineups_confirmed')}")
    return "\n".join(lines) or "Nothing notable."


# The audited portfolio is cached under the same key shape every other
# batch uses (routers/mlb.py's `contest_batch:{id}`), so the CSV
# download and the DraftKings entry filler work on it with no special
# case for "this batch came out of an audit". One hour matches the
# generator's own batches -- long enough to act on before lock, short
# enough that yesterday's portfolio can't be filled into today's
# template by accident.
_SELECTION_TTL = 3600


def _cache_selection(
    day: str, entries: list[dict[str, Any]], audit: dict[str, Any] | None
) -> str | None:
    """Store the audit's chosen portfolio as its own batch and return
    its id, so the pre-lock brief hands over something enterable rather
    than only something readable."""
    indices = ((audit or {}).get("selection") or {}).get("indices") or []
    kept = [entries[i] for i in indices if i < len(entries)]
    if not kept:
        return None
    batch_id = uuid4().hex
    cache.put(f"contest_batch:{batch_id}", {"entries": kept, "results": None}, _SELECTION_TTL)
    cache.put(f"contest_batch_day:{batch_id}", day, _SELECTION_TTL)
    return batch_id


async def run_prelock(day: str | None = None, *, force: bool = False, target_count: int | None = None) -> dict[str, Any]:
    settings = get_settings()
    tz = ZoneInfo(settings.brief_timezone)
    day = day or datetime.now(tz).date().isoformat()
    existing = get_brief(day, PRELOCK)
    if existing and not force:
        return existing

    slate_meta = await main_slate(day)
    lock = _lock_time(slate_meta)
    minutes = int((lock - datetime.now(timezone.utc)).total_seconds() // 60) if lock else None
    slate = await mlb_slate.build_slate(day, force_refresh=True, include_hitters=True, include_inhouse=True)
    compact = _compact_for_brief(slate)
    morning = get_brief(day, MORNING)
    morning_text = (morning or {}).get("text") or "(no morning brief was generated today)"

    batch = latest_batch(day)
    keep_batch_id = None
    if batch and batch.get("entries"):
        audit = build_audit.audit_batch(batch["entries"], slate, target_count=target_count)
        audit_block = (
            f"Batch {batch['batch_id']} from the {batch['source']} step ({batch['total_entries']} entries, "
            f"first {len(batch['entries'])} audited).\n" + build_audit.audit_to_markdown(audit)
        )
        keep_batch_id = _cache_selection(day, batch["entries"], audit)
    else:
        audit = None
        audit_block = "No contest batch has been built for today. The generator hasn't been run, or it was run before the backend last restarted."

    prompt = _PRELOCK_TEMPLATE.format(
        date=day,
        minutes_to_lock=minutes if minutes is not None else "?",
        lock_local=_local(lock, tz),
        slate_label=(slate_meta or {}).get("label") or "Main",
        morning_excerpt=morning_text[:6000],
        changes_block=_changes_block(day, slate, morning),
        audit_block=audit_block,
        data=json.dumps(compact, indent=1, default=str),
    )
    result = await analysis.complete(prompt, system_prompt=SYSTEM_PROMPT, max_tokens=3000)
    payload = {
        "kind": PRELOCK,
        "date": day,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lock_time_utc": lock.isoformat() if lock else None,
        "slate_label": (slate_meta or {}).get("label"),
        "lineups_confirmed": compact.get("lineups_confirmed"),
        # Trimmed: a brief is stored for two weeks, and the full cut
        # list of a big batch is megabytes of JSON nobody reads back.
        # The markdown above (already in the prompt) and the CSV export
        # are where the complete record lives.
        "audit": build_audit.trim_for_response(audit) if audit else None,
        "keep_entries": ((audit or {}).get("selection") or {}).get("indices")
        and [batch["entries"][i] for i in audit["selection"]["indices"] if i < len(batch["entries"])]
        or None,
        "batch_id": (batch or {}).get("batch_id"),
        "keep_batch_id": keep_batch_id,
        **result,
    }
    _store(day, PRELOCK, payload)
    return payload


# ------------------------------------------------------------ scheduler --


def _parse_hhmm(raw: str) -> tuple[int, int]:
    h, m = raw.strip().split(":")
    return int(h), int(m)


async def schedule_status() -> dict[str, Any]:
    """What the loop is going to do next -- surfaced on the Briefs tab
    so 'is it going to fire?' is never a guess."""
    settings = get_settings()
    tz = ZoneInfo(settings.brief_timezone)
    now = datetime.now(tz)
    day = now.date().isoformat()
    h, m = _parse_hhmm(settings.brief_morning_local_time)
    morning_at = now.replace(hour=h, minute=m, second=0, microsecond=0)
    slate_meta = await main_slate(day)
    lock = _lock_time(slate_meta)
    prelock_at = (lock - timedelta(minutes=settings.brief_prelock_lead_min)) if lock else None
    return {
        "enabled": settings.briefs_enabled,
        "timezone": settings.brief_timezone,
        "now_local": now.isoformat(),
        "morning": {
            "scheduled_local": morning_at.isoformat(),
            "fired": bool(cache.get(f"brief_fired:{day}:{MORNING}")),
            "exists": get_brief(day, MORNING) is not None,
        },
        "prelock": {
            "slate_label": (slate_meta or {}).get("label"),
            "lock_local": lock.astimezone(tz).isoformat() if lock else None,
            "scheduled_local": prelock_at.astimezone(tz).isoformat() if prelock_at else None,
            "lead_minutes": settings.brief_prelock_lead_min,
            "fired": bool(cache.get(f"brief_fired:{day}:{PRELOCK}")),
            "exists": get_brief(day, PRELOCK) is not None,
        },
        "latest_batch": {k: v for k, v in (latest_batch(day) or {}).items() if k != "entries"},
    }


async def tick(now_utc: datetime | None = None) -> list[str]:
    """One pass of the scheduler; returns which briefs it fired. Split
    from the loop so it's testable with a fake clock."""
    settings = get_settings()
    if not settings.briefs_enabled:
        return []
    tz = ZoneInfo(settings.brief_timezone)
    now = (now_utc or datetime.now(timezone.utc)).astimezone(tz)
    day = now.date().isoformat()
    fired: list[str] = []

    h, m = _parse_hhmm(settings.brief_morning_local_time)
    morning_at = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if now >= morning_at and now < morning_at + timedelta(hours=8) and not cache.get(f"brief_fired:{day}:{MORNING}"):
        cache.put(f"brief_fired:{day}:{MORNING}", True, _FIRED_TTL)
        try:
            await run_morning(day, force=True)
            fired.append(MORNING)
            log.info("Morning brief generated for %s", day)
        except Exception:  # noqa: BLE001
            log.exception("Morning brief failed for %s", day)

    slate_meta = await main_slate(day)
    lock = _lock_time(slate_meta)
    if lock:
        prelock_at = lock - timedelta(minutes=settings.brief_prelock_lead_min)
        if now >= prelock_at.astimezone(tz) and now < lock.astimezone(tz) and not cache.get(f"brief_fired:{day}:{PRELOCK}"):
            cache.put(f"brief_fired:{day}:{PRELOCK}", True, _FIRED_TTL)
            try:
                await run_prelock(day, force=True)
                fired.append(PRELOCK)
                log.info("Pre-lock brief generated for %s (lock %s)", day, lock.isoformat())
            except Exception:  # noqa: BLE001
                log.exception("Pre-lock brief failed for %s", day)
    return fired


async def _schedule_loop() -> None:
    settings = get_settings()
    if not settings.briefs_enabled:
        log.info("Briefs scheduler disabled (BRIEFS_ENABLED=false)")
        return
    log.info(
        "Briefs scheduler on: morning at %s %s, pre-lock %d min before the DK Main slate",
        settings.brief_morning_local_time,
        settings.brief_timezone,
        settings.brief_prelock_lead_min,
    )
    while True:
        try:
            await tick()
        except Exception:  # noqa: BLE001
            log.exception("Briefs scheduler tick failed")
        await asyncio.sleep(60)
