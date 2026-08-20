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

-- One row per player per real GPP contest, from a real DK contest-
-- standings export (post-contest results -- rank/points/lineup per
-- entry plus real final ownership%/actual FPTS per player, a
-- different file than the pre-contest salary CSV). This is genuine
-- market ground truth this app has never had access to before: real
-- ownership as the field actually rostered, not RotoWire's number or
-- this app's own heuristic -- the exact data a future ownership-model
-- calibration pass needs, and eventually real historical context the
-- AI Analysis feature could draw on instead of only ever reasoning
-- about today's slate in isolation.
CREATE TABLE IF NOT EXISTS contest_player_results (
    date              DATE NOT NULL,
    contest_id        TEXT NOT NULL,
    contest_name      TEXT NOT NULL,
    player_name       TEXT NOT NULL,
    normalized_name   TEXT NOT NULL,
    position          TEXT,
    ownership_pct     NUMERIC,
    actual_fpts       NUMERIC,
    archived_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (contest_id, normalized_name)
);
CREATE INDEX IF NOT EXISTS idx_contest_player_results_date ON contest_player_results(date);

-- One row per real contest uploaded -- field size, entry fee, and
-- (once identified, either by an exact EntryId or a best-effort
-- handle match against EntryName) the user's own rank/points, for a
-- real bankroll/results-over-time view. No $ payout here -- a contest-
-- standings export has no payout-table data at all, so this tracks
-- what's actually knowable (rank, points, cost) rather than guessing
-- at winnings.
CREATE TABLE IF NOT EXISTS contest_uploads (
    contest_id        TEXT NOT NULL PRIMARY KEY,
    date              DATE NOT NULL,
    contest_name      TEXT NOT NULL,
    field_size        INTEGER NOT NULL,
    my_entry_id       TEXT,
    my_rank           INTEGER,
    my_points         NUMERIC,
    entry_fee         NUMERIC,
    archived_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_contest_uploads_date ON contest_uploads(date);
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

    print(
        "Migration complete: slate_projections, player_game_results, "
        "player_actual_results, contest_player_results, contest_uploads"
    )


if __name__ == "__main__":
    asyncio.run(main())
