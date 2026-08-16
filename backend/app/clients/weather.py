"""
Weather via Open-Meteo -- free, no API key, no rate limit worth worrying
about.

Why weather matters for MLB DFS:
  * Wind blowing OUT at 10+ mph is worth roughly a 10-20% bump to HR rate.
    Blowing IN kills it by a similar amount.
  * Hot, humid, low-pressure air is less dense, so the ball carries. Cold
    dense air is a power killer.
  * Rain risk means postponement risk, which means your player scores
    zero. On a short slate that can end your night.

Domed and closed-roof parks ignore all of this, which the service layer
handles.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.cache import cached
from app.clients.http import get_json
from app.config import get_settings

BASE = "https://api.open-meteo.com/v1/forecast"


async def get_game_weather(
    lat: float,
    lon: float,
    game_time_utc: str,
) -> dict[str, Any] | None:
    """
    Forecast for the hour closest to first pitch.

    `game_time_utc` is an ISO timestamp like '2026-08-14T23:10:00Z'.
    """
    if not lat and not lon:
        return None

    settings = get_settings()
    hour_key = game_time_utc[:13]  # cache per hour, not per second
    key = f"weather:{round(lat, 2)}:{round(lon, 2)}:{hour_key}"

    async def _load() -> Any:
        return await get_json(
            BASE,
            params={
                "latitude": lat,
                "longitude": lon,
                "hourly": (
                    "temperature_2m,relative_humidity_2m,precipitation_probability,"
                    "wind_speed_10m,wind_direction_10m,surface_pressure,cloud_cover"
                ),
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
                "precipitation_unit": "inch",
                "timezone": "UTC",
                "forecast_days": 3,
            },
            source="Open-Meteo",
        )

    try:
        payload = await cached(key, settings.ttl_weather, _load)
    except Exception:
        return None

    hourly = payload.get("hourly") or {}
    times: list[str] = hourly.get("time") or []
    if not times:
        return None

    idx = _closest_hour_index(times, game_time_utc)
    if idx is None:
        return None

    def at(field: str) -> Any:
        values = hourly.get(field) or []
        return values[idx] if idx < len(values) else None

    return {
        "temp_f": at("temperature_2m"),
        "humidity_pct": at("relative_humidity_2m"),
        "precip_chance_pct": at("precipitation_probability"),
        "wind_mph": at("wind_speed_10m"),
        "wind_dir_deg": at("wind_direction_10m"),
        "pressure_hpa": at("surface_pressure"),
        "cloud_cover_pct": at("cloud_cover"),
        "forecast_time_utc": times[idx],
    }


def _closest_hour_index(times: list[str], target_iso: str) -> int | None:
    try:
        target = datetime.fromisoformat(target_iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None

    best_idx, best_delta = None, None
    for i, t in enumerate(times):
        try:
            when = datetime.fromisoformat(t)
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=target.tzinfo)
        delta = abs((when - target).total_seconds())
        if best_delta is None or delta < best_delta:
            best_idx, best_delta = i, delta
    return best_idx


# --------------------------------------------------------------------------
# Turning raw weather into a DFS-relevant signal
# --------------------------------------------------------------------------

def wind_effect(
    wind_dir_deg: float | None,
    wind_mph: float | None,
    park_orientation_deg: float | None = None,
) -> dict[str, Any]:
    """
    Classify wind as helping or hurting home runs.

    Meteorological wind direction is the direction the wind is coming
    FROM. `park_orientation_deg` is the compass bearing from home plate
    to centre field. If the wind comes from behind home plate it pushes
    the ball out; if it comes from centre field it knocks balls down.

    When a park's true alignment is unknown, pass None -- we fall back to
    treating the wind as blowing along the 0 (north) axis for the sake of
    computing *something*, but flag the result as low confidence rather
    than pretending we know. A park truly oriented due north (a real,
    common value -- see parks.py) still gets full "medium" confidence,
    since that's an actual known orientation, not a missing one.
    """
    if wind_dir_deg is None or wind_mph is None:
        return {"label": "unknown", "hr_multiplier": 1.0, "confidence": "none"}

    orientation = park_orientation_deg if park_orientation_deg is not None else 0.0

    # Angle between where the wind comes from and the home-plate-to-CF line.
    relative = (wind_dir_deg - orientation) % 360
    if relative > 180:
        relative = 360 - relative

    # relative near 0   -> wind from CF toward home  -> blowing IN
    # relative near 180 -> wind from home toward CF  -> blowing OUT
    if relative <= 45:
        label, direction = "blowing in", -1.0
    elif relative >= 135:
        label, direction = "blowing out", 1.0
    else:
        label, direction = "cross wind", 0.0

    # Roughly 1.5% HR swing per mph along the out/in axis, capped.
    strength = min(wind_mph, 25.0)
    multiplier = 1.0 + direction * (strength * 0.015)
    multiplier = max(0.70, min(1.30, multiplier))

    return {
        "label": label,
        "speed_mph": round(wind_mph, 1),
        "hr_multiplier": round(multiplier, 3),
        "confidence": "medium" if park_orientation_deg is not None else "low",
    }


def temperature_effect(temp_f: float | None) -> dict[str, Any]:
    """
    Ball carry vs temperature. The rule of thumb hitters and physicists
    both land on is about 1% more carry distance per 10 degrees F, which
    translates to a bigger swing in HR rate because home runs live at the
    margin of the fence.
    """
    if temp_f is None:
        return {"label": "unknown", "hr_multiplier": 1.0}

    delta = temp_f - 70.0  # 70F as the neutral baseline
    multiplier = max(0.85, min(1.15, 1.0 + delta * 0.004))

    if temp_f >= 85:
        label = "hot - ball carries"
    elif temp_f >= 72:
        label = "warm"
    elif temp_f >= 58:
        label = "mild"
    else:
        label = "cold - ball dies"

    return {
        "label": label,
        "temp_f": round(temp_f),
        "hr_multiplier": round(multiplier, 3),
    }
