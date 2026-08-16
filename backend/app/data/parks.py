"""
Ballpark reference data: location (for weather), roof status, elevation,
and run/HR park factors.

WHAT PARK FACTORS MEAN
----------------------
A park factor of 1.00 is league average. 1.15 means roughly 15% MORE of
that thing happens here than at a neutral park; 0.85 means ~15% less.

  runs      - overall scoring environment (drives game totals, stacks)
  hr        - home run rate, both sides combined
  hr_lhb    - home run rate for LEFT-handed batters
  hr_rhb    - home run rate for RIGHT-handed batters

The lefty/righty split is the useful one for DFS: Yankee Stadium's short
right-field porch inflates lefty power far more than righty power, and
Fenway does the opposite via the Green Monster.

KEEPING THESE CURRENT
---------------------
These are approximate multi-year rolling values and they drift. Refresh
them once a season from Baseball Savant's park factors page:
    https://baseballsavant.mlb.com/leaderboard/statcast-park-factors
Savant publishes them on a 100-scale (100 = average); divide by 100 to
get the numbers used here.

If a venue is not in this table the app falls back to neutral (1.00) and
logs a warning rather than guessing -- so an unknown or relocated park
never silently skews your numbers.

ORIENTATION AND WIND
---------------------
`orientation_deg` is the compass bearing (0=N, 90=E, 180=S, 270=W,
clockwise) from home plate through the pitcher's mound toward centre
field. It's what turns a raw wind direction into "blowing out" vs
"blowing in" vs "cross wind" -- see `weather.wind_effect()`.

Sourced from ballparks.com's orientation diagrams, cross-checked against
independently reported facts for several parks (e.g. Comerica Park's
well-documented ~145 degree/SE orientation, Globe Life Field's published
"east-northeast" alignment). Truist Park and loanDepot park -- both built
after that source's data -- are set from qualitative "southeast" reports
rather than a precise degree, since no numeric source was found; treat
those two as the roughest values in this table. A park with no orientation
data at all gets `None`, which `wind_effect()` treats as an honest "don't
know" rather than guessing north.
"""

from __future__ import annotations

from typing import TypedDict


class Park(TypedDict):
    name: str
    lat: float
    lon: float
    roof: str  # "open" | "retractable" | "dome"
    elevation_ft: int
    orientation_deg: int | None  # compass bearing, home plate -> CF
    runs: float
    hr: float
    hr_lhb: float
    hr_rhb: float


NEUTRAL: Park = {
    "name": "Unknown Park",
    "lat": 0.0,
    "lon": 0.0,
    "roof": "open",
    "elevation_ft": 0,
    "orientation_deg": None,
    "runs": 1.00,
    "hr": 1.00,
    "hr_lhb": 1.00,
    "hr_rhb": 1.00,
}


# Keyed by the HOME team's abbreviation as returned by the MLB Stats API.
PARKS: dict[str, Park] = {
    "ARI": {"name": "Chase Field", "lat": 33.4455, "lon": -112.0667, "roof": "retractable", "elevation_ft": 1059, "orientation_deg": 0, "runs": 1.04, "hr": 1.03, "hr_lhb": 1.04, "hr_rhb": 1.02},
    "ATH": {"name": "Sutter Health Park", "lat": 38.5800, "lon": -121.5133, "roof": "open", "elevation_ft": 26, "orientation_deg": 20, "runs": 1.06, "hr": 1.08, "hr_lhb": 1.10, "hr_rhb": 1.06},
    "ATL": {"name": "Truist Park", "lat": 33.8907, "lon": -84.4677, "roof": "open", "elevation_ft": 1057, "orientation_deg": 135, "runs": 1.01, "hr": 1.02, "hr_lhb": 1.01, "hr_rhb": 1.03},
    "BAL": {"name": "Oriole Park at Camden Yards", "lat": 39.2839, "lon": -76.6217, "roof": "open", "elevation_ft": 20, "orientation_deg": 30, "runs": 0.98, "hr": 0.95, "hr_lhb": 1.05, "hr_rhb": 0.85},
    "BOS": {"name": "Fenway Park", "lat": 42.3467, "lon": -71.0972, "roof": "open", "elevation_ft": 20, "orientation_deg": 45, "runs": 1.10, "hr": 0.97, "hr_lhb": 0.90, "hr_rhb": 1.04},
    "CHC": {"name": "Wrigley Field", "lat": 41.9484, "lon": -87.6553, "roof": "open", "elevation_ft": 600, "orientation_deg": 30, "runs": 1.01, "hr": 1.02, "hr_lhb": 1.02, "hr_rhb": 1.02},
    "CIN": {"name": "Great American Ball Park", "lat": 39.0975, "lon": -84.5069, "roof": "open", "elevation_ft": 490, "orientation_deg": 120, "runs": 1.06, "hr": 1.21, "hr_lhb": 1.24, "hr_rhb": 1.18},
    "CLE": {"name": "Progressive Field", "lat": 41.4962, "lon": -81.6852, "roof": "open", "elevation_ft": 660, "orientation_deg": 0, "runs": 0.97, "hr": 0.96, "hr_lhb": 0.94, "hr_rhb": 0.98},
    "COL": {"name": "Coors Field", "lat": 39.7559, "lon": -104.9942, "roof": "open", "elevation_ft": 5200, "orientation_deg": 0, "runs": 1.32, "hr": 1.14, "hr_lhb": 1.13, "hr_rhb": 1.15},
    "CWS": {"name": "Rate Field", "lat": 41.8299, "lon": -87.6338, "roof": "open", "elevation_ft": 595, "orientation_deg": 135, "runs": 1.02, "hr": 1.12, "hr_lhb": 1.13, "hr_rhb": 1.11},
    "DET": {"name": "Comerica Park", "lat": 42.3390, "lon": -83.0485, "roof": "open", "elevation_ft": 585, "orientation_deg": 145, "runs": 0.97, "hr": 0.93, "hr_lhb": 0.92, "hr_rhb": 0.94},
    "HOU": {"name": "Daikin Park", "lat": 29.7572, "lon": -95.3555, "roof": "retractable", "elevation_ft": 45, "orientation_deg": 345, "runs": 1.01, "hr": 1.04, "hr_lhb": 0.98, "hr_rhb": 1.09},
    "KC": {"name": "Kauffman Stadium", "lat": 39.0517, "lon": -94.4803, "roof": "open", "elevation_ft": 750, "orientation_deg": 45, "runs": 1.01, "hr": 0.89, "hr_lhb": 0.88, "hr_rhb": 0.90},
    "LAA": {"name": "Angel Stadium", "lat": 33.8003, "lon": -117.8827, "roof": "open", "elevation_ft": 160, "orientation_deg": 45, "runs": 0.99, "hr": 1.03, "hr_lhb": 1.02, "hr_rhb": 1.04},
    "LAD": {"name": "Dodger Stadium", "lat": 34.0739, "lon": -118.2400, "roof": "open", "elevation_ft": 510, "orientation_deg": 30, "runs": 0.97, "hr": 1.08, "hr_lhb": 1.07, "hr_rhb": 1.09},
    "MIA": {"name": "loanDepot park", "lat": 25.7781, "lon": -80.2197, "roof": "retractable", "elevation_ft": 10, "orientation_deg": 135, "runs": 0.95, "hr": 0.90, "hr_lhb": 0.89, "hr_rhb": 0.91},
    "MIL": {"name": "American Family Field", "lat": 43.0280, "lon": -87.9712, "roof": "retractable", "elevation_ft": 635, "orientation_deg": 135, "runs": 1.02, "hr": 1.09, "hr_lhb": 1.10, "hr_rhb": 1.08},
    "MIN": {"name": "Target Field", "lat": 44.9817, "lon": -93.2776, "roof": "open", "elevation_ft": 815, "orientation_deg": 90, "runs": 0.99, "hr": 0.98, "hr_lhb": 0.97, "hr_rhb": 0.99},
    "NYM": {"name": "Citi Field", "lat": 40.7571, "lon": -73.8458, "roof": "open", "elevation_ft": 20, "orientation_deg": 30, "runs": 0.97, "hr": 0.97, "hr_lhb": 0.96, "hr_rhb": 0.98},
    "NYY": {"name": "Yankee Stadium", "lat": 40.8296, "lon": -73.9262, "roof": "open", "elevation_ft": 55, "orientation_deg": 75, "runs": 1.02, "hr": 1.13, "hr_lhb": 1.28, "hr_rhb": 1.00},
    "PHI": {"name": "Citizens Bank Park", "lat": 39.9061, "lon": -75.1665, "roof": "open", "elevation_ft": 20, "orientation_deg": 15, "runs": 1.03, "hr": 1.12, "hr_lhb": 1.14, "hr_rhb": 1.10},
    "PIT": {"name": "PNC Park", "lat": 40.4469, "lon": -80.0057, "roof": "open", "elevation_ft": 730, "orientation_deg": 120, "runs": 0.97, "hr": 0.88, "hr_lhb": 0.84, "hr_rhb": 0.92},
    "SD": {"name": "Petco Park", "lat": 32.7073, "lon": -117.1566, "roof": "open", "elevation_ft": 20, "orientation_deg": 0, "runs": 0.94, "hr": 0.95, "hr_lhb": 0.94, "hr_rhb": 0.96},
    "SEA": {"name": "T-Mobile Park", "lat": 47.5914, "lon": -122.3325, "roof": "retractable", "elevation_ft": 10, "orientation_deg": 45, "runs": 0.92, "hr": 0.95, "hr_lhb": 0.93, "hr_rhb": 0.97},
    "SF": {"name": "Oracle Park", "lat": 37.7786, "lon": -122.3893, "roof": "open", "elevation_ft": 10, "orientation_deg": 90, "runs": 0.94, "hr": 0.87, "hr_lhb": 0.79, "hr_rhb": 0.94},
    "STL": {"name": "Busch Stadium", "lat": 38.6226, "lon": -90.1928, "roof": "open", "elevation_ft": 465, "orientation_deg": 60, "runs": 0.97, "hr": 0.92, "hr_lhb": 0.93, "hr_rhb": 0.91},
    "TB": {"name": "Tropicana Field", "lat": 27.7683, "lon": -82.6534, "roof": "dome", "elevation_ft": 15, "orientation_deg": 45, "runs": 0.96, "hr": 0.95, "hr_lhb": 0.96, "hr_rhb": 0.94},
    "TEX": {"name": "Globe Life Field", "lat": 32.7473, "lon": -97.0847, "roof": "retractable", "elevation_ft": 550, "orientation_deg": 68, "runs": 0.98, "hr": 0.97, "hr_lhb": 0.98, "hr_rhb": 0.96},
    "TOR": {"name": "Rogers Centre", "lat": 43.6414, "lon": -79.3894, "roof": "retractable", "elevation_ft": 250, "orientation_deg": 0, "runs": 1.02, "hr": 1.06, "hr_lhb": 1.05, "hr_rhb": 1.07},
    "WSH": {"name": "Nationals Park", "lat": 38.8730, "lon": -77.0074, "roof": "open", "elevation_ft": 25, "orientation_deg": 30, "runs": 1.00, "hr": 1.00, "hr_lhb": 1.01, "hr_rhb": 0.99},
}

# The A's have gone by several abbreviations through the Sacramento move.
_ALIASES = {"OAK": "ATH", "AZ": "ARI", "SDP": "SD", "SFG": "SF", "TBR": "TB", "KCR": "KC", "WSN": "WSH", "CHW": "CWS"}

_unknown_venues: set[str] = set()


def get_park(team_abbrev: str, venue_name: str | None = None) -> Park:
    """
    Look up a park by home-team abbreviation.

    Falls back to a neutral park (all factors 1.00) if we don't recognise
    the team, so relocations and neutral-site games degrade gracefully
    instead of producing wrong numbers.
    """
    key = (team_abbrev or "").upper()
    key = _ALIASES.get(key, key)

    park = PARKS.get(key)
    if park is not None:
        return park

    if venue_name and venue_name not in _unknown_venues:
        _unknown_venues.add(venue_name)
        import logging

        logging.getLogger(__name__).warning(
            "No park factors for %s (%s) - using neutral 1.00. "
            "Add it to backend/app/data/parks.py.",
            venue_name,
            team_abbrev,
        )

    fallback = dict(NEUTRAL)
    if venue_name:
        fallback["name"] = venue_name
    return fallback  # type: ignore[return-value]


def hr_factor_for_hand(park: Park, bats: str) -> float:
    """HR park factor for a batter of this handedness ('L', 'R', or 'S')."""
    hand = (bats or "").upper()
    if hand == "L":
        return park["hr_lhb"]
    if hand == "R":
        return park["hr_rhb"]
    # Switch hitters get the better of the two -- they choose the side.
    return max(park["hr_lhb"], park["hr_rhb"])
