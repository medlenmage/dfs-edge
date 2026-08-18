"""
A dead-simple disk cache backed by SQLite.

Why this exists: every external API either costs money per call
(The Odds API) or will rate-limit you if you hammer it (MLB Stats API).
Caching responses for a few minutes means you can refresh the dashboard
as often as you like without burning credits.

Usage:
    from app.cache import cached

    data = await cached("odds:mlb:today", ttl=600, loader=fetch_odds)
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.config import get_settings

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache(expires_at);
"""


def _connect() -> sqlite3.Connection:
    settings = get_settings()
    conn = sqlite3.connect(settings.db_path, timeout=10)
    conn.executescript(_SCHEMA)
    return conn


def get(key: str) -> Any | None:
    """Return the cached value, or None if missing/expired."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT value, expires_at FROM cache WHERE key = ?", (key,)
        ).fetchone()
    if row is None:
        return None
    value, expires_at = row
    if expires_at < time.time():
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def put(key: str, value: Any, ttl: int) -> None:
    """Store a value for `ttl` seconds."""
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO cache (key, value, expires_at) VALUES (?, ?, ?)",
            (key, json.dumps(value, default=str), time.time() + ttl),
        )


def purge_expired() -> int:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM cache WHERE expires_at < ?", (time.time(),))
        return cur.rowcount


def vacuum() -> None:
    """
    Reclaim the disk space `DELETE`s leave behind -- SQLite doesn't
    shrink a file on its own after rows are removed, it just marks the
    freed pages reusable for future writes. Large short-TTL entries
    (e.g. a contest-generator batch of thousands of lineups, gone in an
    hour) can otherwise leave the file many times bigger than its live
    contents indefinitely. Run outside a transaction -- VACUUM can't
    execute inside one.
    """
    conn = _connect()
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()


def clear(prefix: str | None = None) -> int:
    """Wipe the cache, or just the keys starting with `prefix`."""
    with _connect() as conn:
        if prefix:
            cur = conn.execute("DELETE FROM cache WHERE key LIKE ?", (f"{prefix}%",))
        else:
            cur = conn.execute("DELETE FROM cache")
        return cur.rowcount


async def cached(
    key: str,
    ttl: int,
    loader: Callable[[], Awaitable[Any]],
    *,
    force: bool = False,
) -> Any:
    """
    Return `key` from cache if fresh, otherwise call `loader()` and store it.

    If the loader raises but we have a *stale* copy on disk, we serve the
    stale copy rather than failing the whole dashboard. Better to show
    slightly old numbers than a broken page mid-slate.
    """
    if not force:
        hit = get(key)
        if hit is not None:
            return hit

    try:
        value = await loader()
    except Exception:
        stale = _get_ignoring_expiry(key)
        if stale is not None:
            return stale
        raise

    put(key, value, ttl)
    return value


def _get_ignoring_expiry(key: str) -> Any | None:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM cache WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return None


# Nothing stored in this cache is meant to survive more than a week (every
# `_TTL`/`ttl_*` value in this codebase tops out at 7 days), so once a day
# is plenty often to keep the file from growing unbounded.
_HOUSEKEEPING_INTERVAL_SEC = 24 * 60 * 60


async def _housekeeping_loop() -> None:
    """
    Runs for as long as the backend process is up: purges expired rows
    and reclaims their disk space once a day. Without this, deleted
    rows just leave the file exactly as large as it ever was -- this
    cache had grown to 223MB with nothing ever reclaiming the space
    freed by expired `contest_batch:*` entries (up to 10,000 lineups
    each, gone within an hour) and every other TTL'd response. Wrapped
    per-iteration, same resilience convention as
    `lineup_watch._poll_loop()` -- one bad run never kills the loop.
    """
    while True:
        try:
            purged = purge_expired()
            vacuum()
            if purged:
                log.info("Cache housekeeping: purged %d expired row(s), reclaimed disk space", purged)
        except Exception:
            log.exception("Cache housekeeping failed")
        await asyncio.sleep(_HOUSEKEEPING_INTERVAL_SEC)
