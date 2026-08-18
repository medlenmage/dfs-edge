"""
One-time (safe-to-rerun) migration: creates the history_db.py tables in
Supabase Postgres. Needs SUPABASE_DB_URL set in .env first.

Run from backend/:
    .venv/Scripts/python.exe -m scripts.migrate_history_db
"""

from __future__ import annotations

import asyncio

import asyncpg

from app.config import get_settings

_DDL = """
-- Archived RotoWire uploads, one row per player per slate. Overwrites
-- of the same (date, normalized_name, team) are expected -- RotoWire
-- ownership numbers shift as a slate fills, and re-uploading the same
-- day's file is a normal real workflow.
CREATE TABLE IF NOT EXISTS slate_projections (
    date                     DATE NOT NULL,
    name                     TEXT NOT NULL,
    normalized_name          TEXT NOT NULL,
    team                     TEXT NOT NULL,
    position                 TEXT,
    salary                   INTEGER,
    rotowire_fpts            NUMERIC,
    rotowire_ownership_pct   NUMERIC,
    inhouse_fpts             NUMERIC,
    inhouse_ownership_pct    NUMERIC,
    archived_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (date, normalized_name, team)
);
CREATE INDEX IF NOT EXISTS idx_slate_projections_date ON slate_projections(date);

-- One row per player per real game once it's final -- the ground-truth
-- DK points a projection model gets graded against. Not archived yet
-- as of this migration (see README); the table exists so Phase 5's
-- backtest calibration has somewhere to land once that archiver ships.
CREATE TABLE IF NOT EXISTS player_game_results (
    date         DATE NOT NULL,
    player_id    INTEGER NOT NULL,
    name         TEXT NOT NULL,
    team         TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('hitter', 'pitcher')),
    dk_points    NUMERIC NOT NULL,
    raw_stats    JSONB NOT NULL,
    archived_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (date, player_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_player_game_results_date ON player_game_results(date);

-- Joins back to slate_projections by (date, player_id) to answer "how
-- far off was each model" -- also not archived yet, same as above.
CREATE TABLE IF NOT EXISTS player_actual_results (
    date              DATE NOT NULL,
    player_id         INTEGER NOT NULL,
    dk_points_actual  NUMERIC NOT NULL,
    archived_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (date, player_id)
);
CREATE INDEX IF NOT EXISTS idx_player_actual_results_date ON player_actual_results(date);
"""


async def main() -> None:
    settings = get_settings()
    if not settings.supabase_db_url:
        raise SystemExit("SUPABASE_DB_URL is not set in .env -- add it first, then rerun this.")

    conn = await asyncpg.connect(settings.supabase_db_url)
    try:
        await conn.execute(_DDL)
    finally:
        await conn.close()

    print("Migration complete: slate_projections, player_game_results, player_actual_results")


if __name__ == "__main__":
    asyncio.run(main())
