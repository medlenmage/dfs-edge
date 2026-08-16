"""
Coordinates for every NFL team's home stadium -- just enough to feed
weather.get_game_weather(), which only needs lat/lon. Whether a game is
actually exposed to that weather (outdoors vs a dome) comes straight off
each game's own `roof` field from clients/nfl.py's schedule pull, not
from here, so a franchise relocation only ever needs a coordinate
update in this one place.

Team codes match nflverse's own schedule data (see clients/nfl.py) --
notably "LA" for the Rams and "WAS" for Washington, not the "LAR"/"WSH"
some DFS sites use (aliased in services/player_match.py).

`tz` is the home team's IANA timezone -- nflverse's schedule gives
kickoff as a local date + local time with no offset, so converting it
to the UTC timestamp weather.get_game_weather() needs means knowing
each stadium's zone (Arizona doesn't observe DST, which is exactly the
kind of thing that's silently wrong if hardcoded as a fixed UTC offset
instead of a real zone).

One known gap: international-series games (a London/Germany/Australia
game a team hosts as the nominal "home" side) use that team's normal
stadium here, since the schedule doesn't carry a coordinate for the
away venue -- the listed `stadium` name on the game itself will say
where it's actually being played, but the weather pulled for it won't
be. Rare enough (a handful of games a season) not to hold up shipping
this over.
"""

from __future__ import annotations

from typing import Any

STADIUMS: dict[str, dict[str, Any]] = {
    "ARI": {"name": "State Farm Stadium", "lat": 33.5276, "lon": -112.2626, "tz": "America/Phoenix"},
    "ATL": {"name": "Mercedes-Benz Stadium", "lat": 33.7554, "lon": -84.4008, "tz": "America/New_York"},
    "BAL": {"name": "M&T Bank Stadium", "lat": 39.2780, "lon": -76.6227, "tz": "America/New_York"},
    "BUF": {"name": "Highmark Stadium", "lat": 42.7738, "lon": -78.7870, "tz": "America/New_York"},
    "CAR": {"name": "Bank of America Stadium", "lat": 35.2258, "lon": -80.8528, "tz": "America/New_York"},
    "CHI": {"name": "Soldier Field", "lat": 41.8623, "lon": -87.6167, "tz": "America/Chicago"},
    "CIN": {"name": "Paycor Stadium", "lat": 39.0954, "lon": -84.5160, "tz": "America/New_York"},
    "CLE": {"name": "Huntington Bank Field", "lat": 41.5061, "lon": -81.6995, "tz": "America/New_York"},
    "DAL": {"name": "AT&T Stadium", "lat": 32.7473, "lon": -97.0945, "tz": "America/Chicago"},
    "DEN": {"name": "Empower Field at Mile High", "lat": 39.7439, "lon": -105.0201, "tz": "America/Denver"},
    "DET": {"name": "Ford Field", "lat": 42.3400, "lon": -83.0456, "tz": "America/New_York"},
    "GB": {"name": "Lambeau Field", "lat": 44.5013, "lon": -88.0622, "tz": "America/Chicago"},
    "HOU": {"name": "NRG Stadium", "lat": 29.6847, "lon": -95.4107, "tz": "America/Chicago"},
    "IND": {"name": "Lucas Oil Stadium", "lat": 39.7601, "lon": -86.1639, "tz": "America/New_York"},
    "JAX": {"name": "EverBank Stadium", "lat": 30.3239, "lon": -81.6373, "tz": "America/New_York"},
    "KC": {"name": "GEHA Field at Arrowhead Stadium", "lat": 39.0489, "lon": -94.4839, "tz": "America/Chicago"},
    "LA": {"name": "SoFi Stadium", "lat": 33.9535, "lon": -118.3392, "tz": "America/Los_Angeles"},
    "LAC": {"name": "SoFi Stadium", "lat": 33.9535, "lon": -118.3392, "tz": "America/Los_Angeles"},
    "LV": {"name": "Allegiant Stadium", "lat": 36.0909, "lon": -115.1833, "tz": "America/Los_Angeles"},
    "MIA": {"name": "Hard Rock Stadium", "lat": 25.9580, "lon": -80.2389, "tz": "America/New_York"},
    "MIN": {"name": "U.S. Bank Stadium", "lat": 44.9738, "lon": -93.2575, "tz": "America/Chicago"},
    "NE": {"name": "Gillette Stadium", "lat": 42.0909, "lon": -71.2643, "tz": "America/New_York"},
    "NO": {"name": "Caesars Superdome", "lat": 29.9509, "lon": -90.0815, "tz": "America/Chicago"},
    "NYG": {"name": "MetLife Stadium", "lat": 40.8135, "lon": -74.0745, "tz": "America/New_York"},
    "NYJ": {"name": "MetLife Stadium", "lat": 40.8135, "lon": -74.0745, "tz": "America/New_York"},
    "PHI": {"name": "Lincoln Financial Field", "lat": 39.9008, "lon": -75.1675, "tz": "America/New_York"},
    "PIT": {"name": "Acrisure Stadium", "lat": 40.4468, "lon": -80.0158, "tz": "America/New_York"},
    "SEA": {"name": "Lumen Field", "lat": 47.5952, "lon": -122.3316, "tz": "America/Los_Angeles"},
    "SF": {"name": "Levi's Stadium", "lat": 37.4032, "lon": -121.9698, "tz": "America/Los_Angeles"},
    "TB": {"name": "Raymond James Stadium", "lat": 27.9759, "lon": -82.5033, "tz": "America/New_York"},
    "TEN": {"name": "Nissan Stadium", "lat": 36.1665, "lon": -86.7713, "tz": "America/Chicago"},
    "WAS": {"name": "Northwest Stadium", "lat": 38.9078, "lon": -76.8645, "tz": "America/New_York"},
}


def get_stadium(team: str) -> dict[str, Any]:
    return STADIUMS.get((team or "").upper()) or {"name": None, "lat": None, "lon": None, "tz": None}
