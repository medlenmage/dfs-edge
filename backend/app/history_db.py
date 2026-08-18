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
