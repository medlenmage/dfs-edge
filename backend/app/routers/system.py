"""Health, config and cache-management routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app import cache
from app.clients import odds
from app.config import get_settings

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
async def health() -> dict[str, Any]:
    """
    What's configured and what isn't.

    The dashboard calls this on load so it can grey out panels that need
    a key you haven't added yet, instead of showing a broken widget.
    """
    settings = get_settings()
    return {
        "status": "ok",
        "features": {
            "mlb_data": True,          # free, always available
            "weather": True,           # free, always available
            "injuries": True,          # free, always available (MLB roster status)
            "pitcher_edge": True,      # free, always available
            "betting_lines": settings.has_odds,
            "ai_analysis": settings.has_claude,
            "player_props": settings.has_odds and settings.odds_fetch_props,
        },
        "config": {
            "model": settings.anthropic_model if settings.has_claude else None,
            "bookmakers": settings.odds_bookmakers if settings.has_odds else [],
            "cache_ttl_seconds": {
                "schedule": settings.ttl_schedule,
                "stats": settings.ttl_stats,
                "odds": settings.ttl_odds,
                "weather": settings.ttl_weather,
                "injuries": settings.ttl_injuries,
            },
        },
        "odds_api_credits": odds.get_usage(),
    }


@router.post("/cache/clear")
async def clear_cache(prefix: str | None = Query(None)) -> dict[str, Any]:
    """Wipe cached responses. Useful after lineups drop."""
    removed = cache.clear(prefix)
    return {"cleared": removed, "prefix": prefix}


@router.post("/cache/purge")
async def purge_cache() -> dict[str, Any]:
    return {"purged": cache.purge_expired()}
