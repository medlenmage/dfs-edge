#!/usr/bin/env python3
"""
Demo mode: run the whole app on fake data.

Useful for two things:
  * seeing what the dashboard looks like before you've added any API keys
  * poking at the UI in the off-season, when there is no live slate

    cd backend
    .venv/bin/python ../scripts/preview.py       # then open http://127.0.0.1:8001

It serves the BUILT frontend (run `npm run build` in frontend/ first),
so this is also a decent way to check a production build.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "tests"))

# Demo mode ships fake betting lines, so tell the UI lines are available
# even though there's no real key. The AI panel stays honestly disabled
# unless you actually have a key set.
os.environ.setdefault("ODDS_API_KEY", "demo-mode-fake-key")
os.environ.setdefault("DB_PATH", "data/preview.db")

import test_pipeline as fx  # noqa: E402

# --- Widen the fixture to a few more games so the UI has something to show ---

fx.FAKE_GAMES += [
    {
        "gamePk": 777002,
        "gameDate": f"{fx.DAY}T01:10:00Z",
        "status": {"detailedState": "Scheduled"},
        "venue": {"name": "Coors Field"},
        "teams": {
            "home": {
                "team": {"id": 115, "name": "Colorado Rockies", "abbreviation": "COL"},
                "probablePitcher": {"id": 9003, "fullName": "Altitude Adams"},
            },
            "away": {
                "team": {"id": 119, "name": "Los Angeles Dodgers", "abbreviation": "LAD"},
                "probablePitcher": {"id": 9004, "fullName": "Ace Alvarez"},
            },
        },
    },
    {
        "gamePk": 777003,
        "gameDate": f"{fx.DAY}T23:40:00Z",
        "status": {"detailedState": "Scheduled"},
        "venue": {"name": "Tropicana Field"},
        "teams": {
            "home": {
                "team": {"id": 139, "name": "Tampa Bay Rays", "abbreviation": "TB"},
                "probablePitcher": {"id": 9005, "fullName": "Domed Dawson"},
            },
            "away": {
                "team": {"id": 141, "name": "Toronto Blue Jays", "abbreviation": "TOR"},
                "probablePitcher": {"id": 9006, "fullName": "Northern Nolan"},
            },
        },
    },
]

fx.FAKE_PEOPLE.update({
    9003: {"id": 9003, "name": "Altitude Adams", "throws": "R", "position": "P", "bats": "R"},
    9004: {"id": 9004, "name": "Ace Alvarez", "throws": "L", "position": "P", "bats": "L"},
    9005: {"id": 9005, "name": "Domed Dawson", "throws": "R", "position": "P", "bats": "R"},
    9006: {"id": 9006, "name": "Northern Nolan", "throws": "L", "position": "P", "bats": "L"},
    301: {"id": 301, "name": "Rocky Mountain Ray", "bats": "R", "position": "1B", "throws": "R"},
    302: {"id": 302, "name": "Thin Air Thomas", "bats": "L", "position": "RF", "throws": "L"},
    303: {"id": 303, "name": "Mile High Mike", "bats": "R", "position": "3B", "throws": "R"},
    401: {"id": 401, "name": "Dodger Blue Dave", "bats": "L", "position": "CF", "throws": "L"},
    402: {"id": 402, "name": "Chavez Ravine Chris", "bats": "R", "position": "SS", "throws": "R"},
    501: {"id": 501, "name": "Rays Rookie", "bats": "L", "position": "2B", "throws": "R"},
    502: {"id": 502, "name": "Tampa Tony", "bats": "R", "position": "DH", "throws": "R"},
    601: {"id": 601, "name": "Blue Jay Bill", "bats": "R", "position": "1B", "throws": "R"},
    602: {"id": 602, "name": "Toronto Tim", "bats": "L", "position": "LF", "throws": "L"},
})

fx.ROSTERS.update({
    115: [301, 302, 303, 9003],
    119: [401, 402, 9004],
    139: [501, 502, 9005],
    141: [601, 602, 9006],
})

_extra_hitters = [301, 302, 303, 401, 402, 501, 502, 601, 602]
_profiles = {
    301: (0.930, 0.870), 302: (0.700, 0.880), 303: (0.880, 0.760),
    401: (0.690, 0.910), 402: (0.870, 0.800),
    501: (0.620, 0.740), 502: (0.810, 0.700),
    601: (0.900, 0.790), 602: (0.660, 0.850),
}
for pid in _extra_hitters:
    vl, vr = _profiles[pid]
    fx.SPLITS[("hitting", "vl")][pid] = fx.hit(160, vl)
    fx.SPLITS[("hitting", "vr")][pid] = fx.hit(400, vr)
    fx.SPLITS[("hitting", "h")][pid] = fx.hit(300, (vl + vr) / 2 + 0.03)
    fx.SPLITS[("hitting", "a")][pid] = fx.hit(300, (vl + vr) / 2 - 0.03)
    fx.SEASON["hitting"][pid] = fx.hit(560, (vl + vr) / 2)
    fx.RECENT[pid] = fx.hit(62, (vl + vr) / 2 + 0.04)

for pid, ops_l, ops_r, era in [
    (9003, 0.860, 0.900, 5.40),
    (9004, 0.640, 0.690, 2.85),
    (9005, 0.720, 0.760, 3.90),
    (9006, 0.700, 0.830, 4.10),
]:
    fx.SPLITS[("pitching", "vl")][pid] = fx.pitch(300, ops_l, era)
    fx.SPLITS[("pitching", "vr")][pid] = fx.pitch(300, ops_r, era)
    fx.SEASON["pitching"][pid] = fx.pitch(600, (ops_l + ops_r) / 2, era)

fx.FAKE_LINES += [
    {
        "event_id": "evt2", "commence_time": f"{fx.DAY}T01:10:00Z",
        "home_team": "Colorado Rockies", "away_team": "Los Angeles Dodgers",
        "total": 12.5, "over_price": -105, "under_price": -115,
        "home_moneyline": 175, "away_moneyline": -210,
        "home_spread": 1.5, "away_spread": -1.5,
        "home_implied_runs": 5.5, "away_implied_runs": 7.0, "book": "DraftKings",
    },
    {
        "event_id": "evt3", "commence_time": f"{fx.DAY}T23:40:00Z",
        "home_team": "Tampa Bay Rays", "away_team": "Toronto Blue Jays",
        "total": 7.5, "over_price": -110, "under_price": -110,
        "home_moneyline": 105, "away_moneyline": -125,
        "home_spread": 1.5, "away_spread": -1.5,
        "home_implied_runs": 3.0, "away_implied_runs": 4.5, "book": "DraftKings",
    },
]


async def _weather(lat, lon, when):
    # Coors: hot and windy out. Fenway: warm. Trop: dome (never asked).
    if abs(lat - 39.7559) < 0.1:
        return {**fx.FAKE_WEATHER, "temp_f": 94.0, "wind_mph": 14.0,
                "wind_dir_deg": 190.0, "precip_chance_pct": 45}
    return fx.FAKE_WEATHER


def mount_ui(app, dist: Path) -> None:
    """
    Serve the built frontend from the same server as the API.

    The API defines its own GET / route, and a route always wins over a
    mount at the same path -- so we drop that route first, otherwise the
    browser gets the API's JSON index instead of index.html.
    """
    from fastapi.staticfiles import StaticFiles

    app.router.routes = [
        r for r in app.router.routes if getattr(r, "path", None) != "/"
    ]
    app.mount("/", StaticFiles(directory=str(dist), html=True), name="ui")


def main() -> None:
    fx.patch()
    from app.clients import weather

    weather.get_game_weather = _weather

    import uvicorn

    from app.main import app

    dist = ROOT / "frontend" / "dist"
    if dist.exists():
        mount_ui(app, dist)
        print(f"\n  Demo running:  http://127.0.0.1:8001")
    else:
        print("\n  frontend/dist not found - run `npm run build` in frontend/ first.")
        print("  API-only demo:  http://127.0.0.1:8001/docs")

    print(f"  Fake slate date: {fx.DAY}\n")
    uvicorn.run(app, host="127.0.0.1", port=8001, log_level="warning")


if __name__ == "__main__":
    main()
