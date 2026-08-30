"""
DFS Edge -- FastAPI application entry point.

Run it with:
    cd backend
    uvicorn app.main:app --reload

Then open http://127.0.0.1:8000/docs for interactive API documentation.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import cache
from app.clients.http import close_client
from app.config import get_settings
from app.routers import mlb, nfl, system
from app.services import lineup_watch

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dfsedge")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    log.info("DFS Edge starting up")
    log.info("  betting lines : %s", "ON" if settings.has_odds else "off (no ODDS_API_KEY)")
    log.info(
        "  AI analysis   : %s",
        "ON" if settings.has_claude else "off (no Claude Code login or ANTHROPIC_API_KEY)",
    )
    log.info("  cache db      : %s", settings.db_path)
    log.info("  lineup watch  : polling every %ds for scratches", settings.lineup_poll_interval_sec)
    cache.purge_expired()
    watch_task = asyncio.create_task(lineup_watch._poll_loop())
    housekeeping_task = asyncio.create_task(cache._housekeeping_loop())
    yield
    watch_task.cancel()
    housekeeping_task.cancel()
    await close_client()
    log.info("DFS Edge shut down")


app = FastAPI(
    title="DFS Edge",
    description=(
        "Personal daily-fantasy research API. Pulls MLB stats, betting "
        "lines and weather, scores every hitter's matchup, and runs the "
        "slate past Claude for a written read."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(system.router)
app.include_router(mlb.router)
app.include_router(nfl.router)


@app.get("/")
async def root() -> dict[str, object]:
    return {
        "app": "DFS Edge",
        "docs": "/docs",
        "endpoints": [
            "/api/health",
            "/api/mlb/slate",
            "/api/mlb/games",
            "/api/mlb/hitters",
            "/api/mlb/stacks",
            "/api/mlb/analysis",
            "/api/nfl/slate",
            "/api/nfl/lineups",
        ],
    }
