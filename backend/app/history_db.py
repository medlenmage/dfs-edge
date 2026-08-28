"""
Durable historical data store -- Postgres via Supabase, parallel to
cache.py's local SQLite TTL cache but for data meant to survive
forever, not just a few days.

cache.py's `projections:{day}` key gets overwritten on every new
RotoWire upload and expires after a week -- there is no way to look
back at what a slate's real projections/ownership looked like once
that week is up. This module archives every real upload permanently,
so a statistically-calibrated projection/ownership model becomes
possible later (see the README roadmap's "Results tracking and weight
backtesting" item) once enough slates have accumulated.

Entirely optional: every function here is a no-op if SUPABASE_DB_URL
isn't set, and every write swallows its own errors (logged, never
raised) -- same resilience convention as cache.cached()'s
stale-serve-on-error behavior. Archiving history must never break the
live dashboard or a real upload that already succeeded.

Run scripts/migrate_history_db.py once (from backend/) to create the
tables before this can write anything.
"""

from __future__ import annotations

import logging
from datetime import date as date_cls
from typing import Any

import asyncpg

from app.config import get_settings

log = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def _get_pool() -> asyncpg.Pool | None:
    global _pool
    settings = get_settings()
    if not settings.supabase_db_url:
        return None
    if _pool is None:
        _pool = await asyncpg.create_pool(settings.supabase_db_url, min_size=1, max_size=3)
    return _pool


async def archive_slate_projections(day: str, rows: list[dict[str, Any]]) -> None:
    """
    Archive one day's RotoWire upload permanently. Upserts on
    (date, normalized_name, team) so re-uploading the same day's file
    (a common real workflow -- RotoWire updates ownership as a slate
    fills) overwrites rather than duplicates.
    """
    pool = await _get_pool()
    if pool is None or not rows:
        return
    try:
        parsed_day = date_cls.fromisoformat(day)
        async with pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO slate_projections
                    (date, name, normalized_name, team, position, salary,
                     rotowire_fpts, rotowire_ownership_pct)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (date, normalized_name, team) DO UPDATE SET
                    name = EXCLUDED.name,
                    position = EXCLUDED.position,
                    salary = EXCLUDED.salary,
                    rotowire_fpts = EXCLUDED.rotowire_fpts,
                    rotowire_ownership_pct = EXCLUDED.rotowire_ownership_pct,
                    archived_at = now()
                """,
                [
                    (
                        parsed_day,
                        row.get("name"),
                        row.get("normalized_name"),
                        row.get("team"),
                        row.get("position"),
                        row.get("salary"),
                        row.get("fpts"),
                        row.get("ownership_pct"),
                    )
                    for row in rows
                ],
            )
    except Exception:
        log.exception("Failed to archive slate_projections for %s", day)


async def archive_contest_results(
    day: str,
    contest_id: str,
    contest_name: str,
    player_pool: list[dict[str, Any]],
    *,
    field_size: int,
    entry_fee: float | None = None,
    my_entry: dict[str, Any] | None = None,
) -> None:
    """
    Archive one real contest's post-contest results permanently: every
    player's real final ownership%/actual FPTS (contest_player_results),
    plus the contest itself -- field size, entry fee, and the user's own
    rank/points once identified (contest_uploads). Upserts on contest_id
    so re-uploading the same contest's export overwrites rather than
    duplicates.
    """
    pool = await _get_pool()
    if pool is None:
        return
    try:
        parsed_day = date_cls.fromisoformat(day)
        async with pool.acquire() as conn:
            if player_pool:
                await conn.executemany(
                    """
                    INSERT INTO contest_player_results
                        (date, contest_id, contest_name, player_name,
                         normalized_name, position, ownership_pct, actual_fpts)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (contest_id, normalized_name) DO UPDATE SET
                        ownership_pct = EXCLUDED.ownership_pct,
                        actual_fpts = EXCLUDED.actual_fpts,
                        archived_at = now()
                    """,
                    [
                        (
                            parsed_day,
                            contest_id,
                            contest_name,
                            row["name"],
                            row["normalized_name"],
                            row.get("position"),
                            row.get("ownership_pct"),
                            row.get("actual_fpts"),
                        )
                        for row in player_pool
                    ],
                )
            await conn.execute(
                """
                INSERT INTO contest_uploads
                    (contest_id, date, contest_name, field_size,
                     my_entry_id, my_rank, my_points, entry_fee)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (contest_id) DO UPDATE SET
                    field_size = EXCLUDED.field_size,
                    my_entry_id = EXCLUDED.my_entry_id,
                    my_rank = EXCLUDED.my_rank,
                    my_points = EXCLUDED.my_points,
                    entry_fee = EXCLUDED.entry_fee,
                    archived_at = now()
                """,
                contest_id,
                parsed_day,
                contest_name,
                field_size,
                (my_entry or {}).get("entry_id"),
                (my_entry or {}).get("rank"),
                (my_entry or {}).get("points"),
                entry_fee,
            )
    except Exception:
        log.exception("Failed to archive contest results for %s", contest_id)


async def archive_contest_stacks(
    day: str,
    contest_id: str,
    contest_name: str,
    distribution: dict[str, dict[int, int]],
    *,
    field_size: int,
) -> None:
    """
    Archive how often the real field stacked each team, and at what
    size, for one contest -- the aggregate derived from the standings
    export's own per-entry `Lineup` column (see
    contest_results.stack_distribution).

    Stored as the distribution rather than the raw lineups it came from,
    on purpose -- see the table's own comment in migrate_history_db.py
    for the storage arithmetic behind that. Upserts on
    (contest_id, team, stack_size) so re-processing the same export
    overwrites rather than duplicates.
    """
    pool = await _get_pool()
    if pool is None:
        return
    rows = [
        (contest_id, date_cls.fromisoformat(day), contest_name, team, size, count, field_size)
        for team, sizes in distribution.items()
        for size, count in sizes.items()
    ]
    if not rows:
        return
    try:
        async with pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO contest_stack_results
                    (contest_id, date, contest_name, team, stack_size,
                     entry_count, field_size)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (contest_id, team, stack_size) DO UPDATE SET
                    entry_count = EXCLUDED.entry_count,
                    field_size = EXCLUDED.field_size,
                    archived_at = now()
                """,
                rows,
            )
    except Exception:
        log.exception("Failed to archive contest stacks for %s", contest_id)


async def get_contest_stack_results() -> list[dict[str, Any]]:
    """
    Every archived real team-stack distribution across every processed
    contest. The training/evaluation target for a team-stack ownership
    model. Returns [] rather than raising if Supabase isn't configured,
    same convention as everything else here.
    """
    pool = await _get_pool()
    if pool is None:
        return []
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT date, contest_id, contest_name, team, stack_size,
                       entry_count, field_size
                FROM contest_stack_results
                ORDER BY date, contest_id, team, stack_size
                """
            )
        return [dict(r) for r in rows]
    except Exception:
        log.exception("Failed to read archived contest stacks")
        return []


async def get_archived_salaries(day: str) -> dict[str, dict[str, Any]]:
    """
    normalized_name -> {salary, team, position, rotowire_fpts} for one
    past date, from the permanent slate_projections archive.

    The local salary/projections cache is day-keyed with a 7-day TTL, so
    backtesting anything more than a week old used to be impossible even
    though the contest results were archived permanently -- 11 of 15
    archived contests were unusable for exactly this reason. This reads
    the durable copy instead.
    """
    pool = await _get_pool()
    if pool is None:
        return {}
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT normalized_name, name, team, position, salary, rotowire_fpts
                FROM slate_projections
                WHERE date = $1 AND salary IS NOT NULL
                """,
                date_cls.fromisoformat(day),
            )
        return {r["normalized_name"]: dict(r) for r in rows}
    except Exception:
        log.exception("Failed to read archived salaries for %s", day)
        return {}


async def get_contest_player_results() -> list[dict[str, Any]]:
    """
    Every archived real player result across every uploaded contest --
    date, contest, player identity, real final ownership%, and real
    actual FPTS. The raw feed for backtesting anything against genuine
    historical outcomes (e.g. checking whether the Monte Carlo outcome
    pools' predicted spread is well-calibrated against what these
    players actually scored) -- unlike get_my_contest_history(), this
    is player-level, not just the user's own single identified entry
    per contest. Returns [] (not an error) if Supabase isn't configured
    or the query fails, same resilience convention as every other
    function here.
    """
    pool = await _get_pool()
    if pool is None:
        return []
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT date, contest_id, contest_name, player_name,
                       normalized_name, position, ownership_pct, actual_fpts
                FROM contest_player_results
                ORDER BY date ASC
                """
            )
            return [dict(r) for r in rows]
    except Exception:
        log.exception("Failed to load contest player results")
        return []


async def get_my_contest_history() -> list[dict[str, Any]]:
    """
    Every uploaded contest with a real identified entry (my_entry_id
    set), oldest first -- the raw feed for a bankroll/results-over-time
    view. Returns [] (not an error) if Supabase isn't configured or the
    query fails, same resilience convention as every other function
    here -- history is a nice-to-have, never something that can break
    the live dashboard.
    """
    pool = await _get_pool()
    if pool is None:
        return []
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT contest_id, date, contest_name, field_size,
                       my_rank, my_points, entry_fee
                FROM contest_uploads
                WHERE my_entry_id IS NOT NULL
                ORDER BY date ASC
                """
            )
            return [dict(r) for r in rows]
    except Exception:
        log.exception("Failed to load contest history")
        return []
