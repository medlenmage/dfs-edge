"""
Offline test of the whole slate pipeline using fake API responses.

This proves the wiring works -- schedule -> splits -> odds -> weather ->
scoring -> JSON -- without touching the network or spending credits.

Run it with:
    cd backend
    .venv/bin/python -m tests.test_pipeline
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import cache  # noqa: E402
from app.clients import mlb, odds, savant, weather  # noqa: E402
from app.data import parks  # noqa: E402
from app.services import (  # noqa: E402
    lineup_watch,
    mlb_slate,
    optimizer,
    player_match,
    projections,
    salaries,
    scoring,
)

DAY = "2026-08-14"

# --------------------------------------------------------------------------
# Fixtures: a Yankees @ Red Sox game with a lefty on the mound for Boston
# --------------------------------------------------------------------------

FAKE_GAMES = [
    {
        "gamePk": 777001,
        "gameDate": f"{DAY}T23:10:00Z",
        "status": {"detailedState": "Scheduled"},
        "venue": {"name": "Fenway Park"},
        "teams": {
            "home": {
                "team": {"id": 111, "name": "Boston Red Sox", "abbreviation": "BOS"},
                "probablePitcher": {"id": 9001, "fullName": "Lefty McLefterson"},
            },
            "away": {
                "team": {"id": 147, "name": "New York Yankees", "abbreviation": "NYY"},
                "probablePitcher": {"id": 9002, "fullName": "Righty Rogers"},
            },
        },
    }
]

FAKE_PEOPLE = {
    9001: {"id": 9001, "name": "Lefty McLefterson", "throws": "L", "position": "P", "bats": "L"},
    9002: {"id": 9002, "name": "Righty Rogers", "throws": "R", "position": "P", "bats": "R"},
    101: {"id": 101, "name": "Big Righty Bat", "bats": "R", "position": "1B", "throws": "R"},
    102: {"id": 102, "name": "Punchless Lefty", "bats": "L", "position": "CF", "throws": "L"},
    103: {"id": 103, "name": "Switch Hitter Sam", "bats": "S", "position": "SS", "throws": "R"},
    201: {"id": 201, "name": "Boston Slugger", "bats": "R", "position": "LF", "throws": "R"},
    202: {"id": 202, "name": "Boston Contact", "bats": "L", "position": "2B", "throws": "R"},
}

ROSTERS = {147: [101, 102, 103, 9002], 111: [201, 202, 9001]}


def hit(pa, ops, avg=0.260, slg=0.440, hr=15):
    return {
        "pa": pa, "ab": int(pa * 0.9), "avg": avg, "obp": ops - slg, "slg": slg,
        "ops": ops, "hr": hr, "rbi": 50, "runs": 50, "sb": 3, "hits": 100,
        "doubles": 20, "triples": 1, "iso": round(slg - avg, 3),
        "k_pct": 0.22, "bb_pct": 0.09, "hr_per_pa": round(hr / pa, 4),
    }


def pitch(bf, ops_against, era=4.00):
    return {
        "ip": bf / 4.3, "bf": bf, "era": era, "whip": 1.25, "avg_against": 0.250,
        "obp_against": 0.320, "slg_against": ops_against - 0.320,
        "ops_against": ops_against, "k": int(bf * 0.23), "bb": int(bf * 0.08),
        "hr": 12, "k_pct": 0.23, "bb_pct": 0.08, "hr_per_9": 1.3, "k_per_9": 9.1,
    }


SPLITS = {
    ("hitting", "vl"): {
        101: hit(180, 0.980, 0.300, 0.580, 12),   # crushes lefties
        102: hit(90, 0.560, 0.200, 0.290, 1),     # helpless vs lefties
        103: hit(150, 0.760, 0.265, 0.430, 6),
        201: hit(170, 0.820, 0.275, 0.470, 9),
        202: hit(60, 0.610, 0.220, 0.330, 2),
    },
    ("hitting", "vr"): {
        101: hit(400, 0.790, 0.265, 0.460, 18),
        102: hit(430, 0.850, 0.285, 0.480, 20),
        103: hit(380, 0.800, 0.270, 0.455, 14),
        201: hit(410, 0.760, 0.255, 0.440, 16),
        202: hit(420, 0.810, 0.280, 0.450, 11),
    },
    ("hitting", "h"): {k: hit(300, 0.830) for k in (101, 102, 103, 201, 202)},
    ("hitting", "a"): {k: hit(300, 0.770) for k in (101, 102, 103, 201, 202)},
    ("pitching", "vl"): {9001: pitch(300, 0.610, 3.10), 9002: pitch(280, 0.880, 4.90)},
    ("pitching", "vr"): {9001: pitch(320, 0.910, 3.10), 9002: pitch(300, 0.700, 4.90)},
}

SEASON = {
    "hitting": {
        101: hit(580, 0.850, 0.275, 0.500, 30),
        102: hit(520, 0.810, 0.270, 0.450, 21),
        103: hit(530, 0.790, 0.268, 0.450, 20),
        201: hit(575, 0.780, 0.262, 0.445, 25),
        202: hit(480, 0.790, 0.275, 0.440, 13),
    },
    "pitching": {9001: pitch(620, 0.760, 3.10), 9002: pitch(580, 0.790, 4.90)},
}

RECENT = {
    101: hit(65, 1.020, 0.330, 0.620, 5),   # hot
    102: hit(60, 0.640, 0.210, 0.330, 1),   # cold
    103: hit(62, 0.795, 0.270, 0.450, 2),
    201: hit(64, 0.800, 0.265, 0.450, 3),
    202: hit(58, 0.780, 0.270, 0.435, 1),
}

def savant_hit(barrel, hard_hit, xwoba):
    return {"barrel_pct": barrel, "hard_hit_pct": hard_hit, "xwoba": xwoba,
            "xslg": xwoba + 0.05, "exit_velo": 89.0}


SAVANT_HIT = {
    101: savant_hit(12.0, 45.0, 0.360),  # Big Righty Bat -- best contact of the bunch
    102: savant_hit(8.0, 38.0, 0.320),   # Punchless Lefty
    103: savant_hit(7.5, 36.0, 0.310),   # Switch Hitter Sam
    201: savant_hit(10.0, 42.0, 0.340),  # Boston Slugger
    202: savant_hit(5.0, 30.0, 0.290),   # Boston Contact
}

SAVANT_PITCH = {
    9001: savant_hit(5.5, 32.0, 0.290),  # Lefty McLefterson -- allows weak contact
    9002: savant_hit(9.5, 42.0, 0.340),  # Righty Rogers -- allows hard contact
}

BULLPEN = {
    111: {"era": 5.20, "whip": 1.55, "k_per_9": 8.0, "ip": 200.0},   # Red Sox -- shaky
    147: {"era": 3.00, "whip": 1.10, "k_per_9": 9.5, "ip": 210.0},   # Yankees -- strong
}

FAKE_LINES = [
    {
        "event_id": "evt1", "commence_time": f"{DAY}T23:10:00Z",
        "home_team": "Boston Red Sox", "away_team": "New York Yankees",
        "total": 9.5, "over_price": -110, "under_price": -110,
        "home_moneyline": 120, "away_moneyline": -142,
        "home_spread": 1.5, "away_spread": -1.5,
        "home_implied_runs": 4.0, "away_implied_runs": 5.5,
        "book": "DraftKings",
    }
]

FAKE_WEATHER = {
    "temp_f": 88.0, "humidity_pct": 55, "precip_chance_pct": 5,
    "wind_mph": 12.0, "wind_dir_deg": 200.0, "pressure_hpa": 1010,
    "cloud_cover_pct": 20, "forecast_time_utc": f"{DAY}T23:00",
}

def salary_row(name, team, salary, avg_points):
    return {
        "name": name, "normalized_name": salaries.normalize_name(name),
        "team": team, "position": "", "salary": salary, "avg_points": avg_points,
    }


SALARIES = [
    salary_row("Big Righty Bat", "NYY", 4200, 9.5),
    salary_row("Boston Slugger", "BOS", 5600, 11.0),
    salary_row("Lefty McLefterson", "BOS", 8800, 16.0),
    # "Punchless Lefty" is deliberately absent -- exercises the no-match path.
]


def projection_row(name, team, fpts, ownership_pct):
    return {
        "name": name, "normalized_name": projections.normalize_name(name),
        "team": team, "position": "", "fpts": fpts, "ownership_pct": ownership_pct,
    }


PROJECTIONS = [
    projection_row("Big Righty Bat", "NYY", 12.4, 18.7),
    projection_row("Lefty McLefterson", "BOS", 17.2, 25.3),
    # "Punchless Lefty" absent here too, and "Boston Slugger" is
    # deliberately absent from THIS list (but present in SALARIES) to
    # prove the two uploads match independently of each other.
]


# --------------------------------------------------------------------------
# Monkeypatch the clients
# --------------------------------------------------------------------------

async def fake_schedule(day, force=False):
    return FAKE_GAMES


async def fake_people(ids):
    return {i: FAKE_PEOPLE[i] for i in ids if i in FAKE_PEOPLE}


async def fake_roster(team_id, season):
    return ROSTERS.get(team_id, [])


async def fake_splits(season, sit_code, group="hitting"):
    return SPLITS.get((group, sit_code), {})


async def fake_season(season, group="hitting"):
    return SEASON.get(group, {})


async def fake_recent(season, games=15, group="hitting"):
    return RECENT


async def fake_lineups(game_pk, force=False):
    return {"home": [], "away": []}


async def fake_lines(sport="mlb", force=False):
    return FAKE_LINES


async def fake_weather(lat, lon, when):
    return FAKE_WEATHER


INJURIES = {
    111: [
        {"id": 301, "name": "Hurt Reliever", "position": "P",
         "status_code": "D10", "status_description": "10-Day Injured List"},
    ],
}


async def fake_injuries(team_id, season):
    return INJURIES.get(team_id, [])


async def fake_savant_hit(season):
    return SAVANT_HIT


async def fake_savant_pitch(season):
    return SAVANT_PITCH


async def fake_bullpen(season):
    return BULLPEN


def fake_salary_load(day):
    return SALARIES


def fake_projection_load(day):
    return PROJECTIONS


# In-memory stand-in for app.cache so lineup_watch.py (and mlb_slate.py's
# read of the scratches it writes) never touch the real on-disk SQLite
# cache during tests.
_FAKE_CACHE: dict[str, object] = {}


def fake_cache_get(key):
    return _FAKE_CACHE.get(key)


def fake_cache_put(key, value, ttl):
    _FAKE_CACHE[key] = value


def patch() -> None:
    mlb.get_schedule = fake_schedule
    mlb.get_people = fake_people
    mlb.get_active_roster = fake_roster
    mlb.get_league_splits = fake_splits
    mlb.get_league_season = fake_season
    mlb.get_recent_form = fake_recent
    mlb.get_lineups = fake_lineups
    mlb.get_team_injuries = fake_injuries
    odds.get_game_lines = fake_lines
    weather.get_game_weather = fake_weather
    savant.get_hitter_batted_ball = fake_savant_hit
    savant.get_pitcher_batted_ball = fake_savant_pitch
    mlb.get_bullpen_stats = fake_bullpen
    salaries.load = fake_salary_load
    projections.load = fake_projection_load
    cache.get = fake_cache_get
    cache.put = fake_cache_put


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

PASS, FAILED = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAILED).append(name)
    mark = "PASS" if condition else "FAIL"
    print(f"  {mark}  {name}" + (f"  -- {detail}" if detail else ""))


async def main() -> int:
    patch()
    print("\nSlate pipeline test (offline fixtures)\n" + "=" * 60)

    slate = await mlb_slate.build_slate(DAY)
    game = slate["games"][0]

    print("\nStructure")
    check("slate builds without error", bool(slate.get("games")))
    check("one game returned", len(slate["games"]) == 1)
    check("no warnings", not slate.get("warnings"), str(slate.get("warnings")))

    print("\nGame environment")
    check("venue resolved to Fenway", game["venue"]["name"] == "Fenway Park",
          game["venue"]["name"])
    check("Fenway park factors loaded",
          game["venue"]["park_factors"]["runs"] == 1.10,
          str(game["venue"]["park_factors"]))
    check("betting line matched by team name",
          game["betting"].get("total") == 9.5)
    check("implied runs split correctly",
          game["away"]["implied_runs"] == 5.5 and game["home"]["implied_runs"] == 4.0)
    check("weather attached", game["weather"].get("temp_f") == 88.0)
    check("hot weather boosts carry",
          game["weather"]["temperature_effect"]["hr_multiplier"] > 1.0,
          str(game["weather"]["temperature_effect"]))
    check("Fenway's real orientation reaches wind_effect (medium confidence)",
          game["weather"]["wind_effect"]["confidence"] == "medium",
          str(game["weather"]["wind_effect"]))

    print("\nPitchers")
    check("home starter is left-handed",
          game["home"]["probable_pitcher"]["throws"] == "L")
    check("away starter is right-handed",
          game["away"]["probable_pitcher"]["throws"] == "R")
    check("pitchers excluded from hitter list",
          all(h["position"] != "P" for h in game["away"]["hitters"]))

    print("\nPitcher edge score")
    home_edge = game["home"]["probable_pitcher"]["edge"]
    away_edge = game["away"]["probable_pitcher"]["edge"]
    check("home pitcher has an edge score", 0 <= home_edge["score"] <= 100,
          str(home_edge["score"]))
    check("away pitcher has an edge score", 0 <= away_edge["score"] <= 100,
          str(away_edge["score"]))
    check("pitcher edge has all seven components",
          set(home_edge["components"]) == {
              "opp_lineup", "strikeout_potential", "team_runs_against",
              "contact_quality_allowed", "own_quality", "park", "weather",
          },
          str(set(home_edge["components"])))
    check("lower ERA scores better on own_quality",
          home_edge["components"]["own_quality"]["value"]
          > away_edge["components"]["own_quality"]["value"],
          f"{home_edge['components']['own_quality']} vs {away_edge['components']['own_quality']}")
    check("pitcher who allows weaker contact scores better on contact_quality_allowed",
          home_edge["components"]["contact_quality_allowed"]["value"]
          > away_edge["components"]["contact_quality_allowed"]["value"],
          f"{home_edge['components']['contact_quality_allowed']} vs "
          f"{away_edge['components']['contact_quality_allowed']}")

    print("\nSalaries")
    home_salary = game["home"]["probable_pitcher"]["salary"]
    check("home starter matched a salary", home_salary is not None and home_salary["salary"] == 8800,
          str(home_salary))
    check("pitcher value is edge score per $1000",
          home_salary["value"] == round(home_edge["score"] / 8.8, 2),
          f"{home_salary['value']} vs expected {round(home_edge['score'] / 8.8, 2)}")

    print("\nInjuries")
    check("Red Sox injury report includes the hurt reliever",
          any(p["name"] == "Hurt Reliever" for p in game["home"]["injuries"]))
    check("Yankees have no injuries in the fixture",
          game["away"]["injuries"] == [])

    print("\nTeam abbreviation aliasing (uploads don't always match MLB's own code)")
    ari_rows = player_match.build_lookup(
        [{"name": "Geraldo Perdomo", "normalized_name": player_match.normalize_name("Geraldo Perdomo"),
          "team": "ARI", "value": "example"}]
    )
    check("a row tagged ARI matches when queried with MLB's own AZ",
          player_match.match(ari_rows, "Geraldo Perdomo", "AZ") is not None)
    check("querying with the uploader's own ARI still matches too",
          player_match.match(ari_rows, "Geraldo Perdomo", "ARI") is not None)

    print("\nLineup watcher (catching scratches between polls)")
    _scratch_poll = {"n": 0}

    async def fake_lineups_scratch_scenario(game_pk, force=False):
        _scratch_poll["n"] += 1
        if _scratch_poll["n"] == 1:
            return {"home": [101, 102], "away": [9002]}
        # Poll 2 onward: "Big Righty Bat" (101) has dropped out of BOS's
        # confirmed lineup -- everyone else is unchanged.
        return {"home": [102], "away": [9002]}

    mlb.get_lineups = fake_lineups_scratch_scenario

    first_poll = await lineup_watch.poll_once(DAY)
    check("first poll just establishes a baseline, no false-positive scratches",
          first_poll == [], str(first_poll))

    second_poll = await lineup_watch.poll_once(DAY)
    check("second poll catches the player who dropped out",
          len(second_poll) == 1 and second_poll[0]["player_id"] == 101,
          str(second_poll))
    check("scratch event carries the right name and team",
          second_poll[0]["name"] == "Big Righty Bat" and second_poll[0]["team"] == "BOS",
          str(second_poll))

    third_poll = await lineup_watch.poll_once(DAY)
    check("a poll with no further changes reports zero new events",
          third_poll == [], str(third_poll))

    mlb.get_lineups = fake_lineups  # restore before touching the slate again

    rebuilt = await mlb_slate.build_slate(DAY)
    rebuilt_home = rebuilt["games"][0]["home"]
    check("a rebuilt slate surfaces the scratch on the right team",
          rebuilt_home["scratches"] == second_poll, str(rebuilt_home["scratches"]))
    check("the other side has no scratches",
          rebuilt["games"][0]["away"]["scratches"] == [])

    print("\nLineup optimizer (DraftKings Classic MLB)")

    def opt_hitter(pid, name, team, pos, salary, fpts):
        return {
            "id": pid, "name": name,
            "salary": {"salary": salary, "position": pos, "avg_points": None, "value": None},
            "projection": {"fpts": fpts, "ownership_pct": None},
        }

    def opt_pitcher(pid, name, salary, fpts):
        return {
            "id": pid, "name": name,
            "salary": {"salary": salary, "position": "P", "avg_points": None, "value": None},
            "projection": {"fpts": fpts, "ownership_pct": None},
        }

    # Deliberately more hitter depth than the shared fixture has, since a
    # legal DK roster needs 8 hitters + 2 pitchers across 7 slot types --
    # this stands alone rather than stretching FAKE_GAMES to cover it.
    opt_home_hitters = [
        opt_hitter(9101, "OC1", "OPT", "C", 3000, 8),
        opt_hitter(9102, "O1B1", "OPT", "1B", 4000, 10),
        opt_hitter(9103, "O2B1", "OPT", "2B", 3500, 9),
        opt_hitter(9104, "O3B1", "OPT", "3B", 4500, 12),
        opt_hitter(9105, "OSS1", "OPT", "SS", 3800, 9.5),
        opt_hitter(9106, "OOF1-scratched", "OPT", "OF", 5000, 99),
        opt_hitter(9107, "OOF2", "OPT", "OF", 4200, 11),
        opt_hitter(9108, "OOF3", "OPT", "OF", 3900, 10.5),
        opt_hitter(9109, "OOF4", "OPT", "OF", 3600, 9.8),
    ]
    opt_away_hitters = [opt_hitter(9110, "OC2", "OPP", "C", 2800, 7)]
    opt_slate = {
        "games": [
            {
                "home": {
                    "abbrev": "OPT", "hitters": opt_home_hitters,
                    "probable_pitcher": opt_pitcher(9200, "OP1", 9000, 18),
                    "scratches": [{"player_id": 9106, "name": "OOF1-scratched"}],
                },
                "away": {
                    "abbrev": "OPP", "hitters": opt_away_hitters,
                    "probable_pitcher": opt_pitcher(9201, "OP2", 8500, 17),
                    "scratches": [],
                },
            }
        ]
    }

    lineup = optimizer.generate_lineup(opt_slate)
    all_ids = [p["id"] for slot in lineup["slots"].values() for p in slot]
    slot_counts = {slot: len(players) for slot, players in lineup["slots"].items()}
    check("optimizer respects the salary cap",
          lineup["salary_used"] <= optimizer.SALARY_CAP, str(lineup["salary_used"]))
    check("optimizer fills every roster slot with the right counts",
          slot_counts == optimizer.SLOT_REQUIREMENTS, str(slot_counts))
    check("optimizer never rosters a scratched player, even a huge-projection one",
          9106 not in all_ids, str(all_ids))

    stacked = optimizer.generate_lineup(opt_slate, min_stack=5)
    opt_hitter_count = sum(
        1
        for slot, players in stacked["slots"].items()
        if slot != "P"
        for p in players
        if p["team"] == "OPT"
    )
    check("min_stack constraint is honored", opt_hitter_count >= 5, str(opt_hitter_count))

    try:
        optimizer.generate_lineup({"games": []})
        check("optimizer raises OptimizerError on an empty pool", False)
    except optimizer.OptimizerError:
        check("optimizer raises OptimizerError on an empty pool", True)

    print("\nPark orientation and wind (real alignment vs the old 0-degree guess)")
    check("Yankee Stadium's real orientation is loaded",
          parks.get_park("NYY")["orientation_deg"] == 75,
          str(parks.get_park("NYY")["orientation_deg"]))
    check("an unrecognised team falls back to unknown orientation, not a guess",
          parks.get_park("ZZZ")["orientation_deg"] is None)

    # Wind FROM the far side of centre field, blowing straight back toward
    # home at Yankee Stadium (orientation 75). With the real orientation
    # this is correctly "blowing out"; the old hardcoded 0-degree default
    # would have called this a cross wind -- wrong direction entirely.
    real_out = weather.wind_effect(255.0, 14.0, park_orientation_deg=75)
    naive_out = weather.wind_effect(255.0, 14.0, park_orientation_deg=None)
    check("real orientation correctly reads this as blowing out",
          real_out["label"] == "blowing out", str(real_out))
    check("the old no-orientation default got this one wrong (cross wind)",
          naive_out["label"] == "cross wind", str(naive_out))

    # Wind FROM behind home plate toward centre field at Yankee Stadium --
    # correctly "blowing in" with real orientation, misread as a cross
    # wind under the old default.
    real_in = weather.wind_effect(80.0, 15.0, park_orientation_deg=75)
    naive_in = weather.wind_effect(80.0, 15.0, park_orientation_deg=None)
    check("real orientation correctly reads this as blowing in",
          real_in["label"] == "blowing in", str(real_in))
    check("the old no-orientation default got this one wrong too",
          naive_in["label"] == "cross wind", str(naive_in))
    check("known orientation is 'medium' confidence even when it's due north (0deg)",
          weather.wind_effect(10.0, 10.0, park_orientation_deg=0)["confidence"] == "medium")
    check("unknown orientation stays 'low' confidence rather than guessing",
          naive_out["confidence"] == "low")

    print("\nPlatoon logic (Yankees facing a LHP)")
    yanks = {h["name"]: h for h in game["away"]["hitters"]}
    righty = yanks["Big Righty Bat"]
    lefty = yanks["Punchless Lefty"]

    check("righty who crushes LHP outscores lefty who can't hit LHP",
          righty["edge"]["score"] > lefty["edge"]["score"],
          f"{righty['edge']['score']} vs {lefty['edge']['score']}")
    check("righty gets the vs-LHP split, not vs-RHP",
          righty["vs_hand"]["ops"] == 0.980)
    check("righty's score is above average", righty["edge"]["score"] > 50,
          str(righty["edge"]["score"]))
    check("righty's score is not clipped at the ceiling",
          righty["edge"]["score"] < 100, str(righty["edge"]["score"]))
    check("lefty's score is below average", lefty["edge"]["score"] < 50,
          str(lefty["edge"]["score"]))

    check("better batted-ball profile scores above average on contact_quality",
          righty["edge"]["components"]["contact_quality"]["value"] > 1.0,
          str(righty["edge"]["components"]["contact_quality"]))
    check("Yankees hitter benefits from the Red Sox's shaky bullpen",
          righty["edge"]["components"]["bullpen"]["value"] > 1.0,
          str(righty["edge"]["components"]["bullpen"]))

    sox = {h["name"]: h for h in game["home"]["hitters"]}
    check("Red Sox hitter is hurt by the Yankees' strong bullpen",
          sox["Boston Slugger"]["edge"]["components"]["bullpen"]["value"] < 1.0,
          str(sox["Boston Slugger"]["edge"]["components"]["bullpen"]))

    check("hitter matched a salary by name + team",
          righty["salary"] is not None and righty["salary"]["salary"] == 4200,
          str(righty["salary"]))
    check("hitter value is edge score per $1000",
          righty["salary"]["value"] == round(righty["edge"]["score"] / 4.2, 2),
          f"{righty['salary']['value']} vs expected {round(righty['edge']['score'] / 4.2, 2)}")
    check("hitter with no salary match returns None, not a crash",
          lefty["salary"] is None, str(lefty["salary"]))

    check("hitter matched a projection by name + team",
          righty["projection"] == {"fpts": 12.4, "ownership_pct": 18.7},
          str(righty["projection"]))
    check("hitter present in salaries but absent from projections gets None",
          sox["Boston Slugger"]["projection"] is None,
          str(sox["Boston Slugger"]["projection"]))
    check("home starter matched a projection",
          game["home"]["probable_pitcher"]["projection"] == {"fpts": 17.2, "ownership_pct": 25.3},
          str(game["home"]["probable_pitcher"]["projection"]))
    check("projection never leaks into the edge score components",
          "fpts" not in righty["edge"]["components"]
          and "ownership_pct" not in righty["edge"]["components"],
          str(list(righty["edge"]["components"])))

    switch = yanks["Switch Hitter Sam"]
    check("switch hitter gets pitcher's vs-RHB split (bats right vs LHP)",
          switch["edge"]["components"]["pitcher"]["ops_against"] == 0.910,
          str(switch["edge"]["components"]["pitcher"].get("ops_against")))

    print("\nScoring internals")
    check("all nine components present",
          len(righty["edge"]["components"]) == 9,
          str(sorted(righty["edge"]["components"])))
    check("weights sum to 1.0",
          abs(sum(scoring.WEIGHTS.values()) - 1.0) < 1e-9,
          str(sum(scoring.WEIGHTS.values())))
    check("scores stay within 0-100",
          all(0 <= h["edge"]["score"] <= 100
              for side in ("home", "away")
              for h in game[side]["hitters"]))
    check("top driver identified", righty["edge"]["top_driver"] is not None,
          righty["edge"]["top_driver"])
    check("hot hitter's form component is above 1.0",
          righty["edge"]["components"]["form"]["value"] > 1.0,
          righty["edge"]["components"]["form"]["detail"])
    check("cold hitter's form component is below 1.0",
          lefty["edge"]["components"]["form"]["value"] < 1.0,
          lefty["edge"]["components"]["form"]["detail"])

    print("\nSmall-sample regression")
    # A hitter 40% above average on a full sample keeps that edge; the same
    # 40% on a tiny sample gets pulled most of the way back to neutral.
    full = scoring._shrink(1.40, sample=240, full_trust=scoring.MIN_PA_FULL_TRUST)
    tiny = scoring._shrink(1.40, sample=12, full_trust=scoring.MIN_PA_FULL_TRUST)
    check("full-sample edge is kept intact", abs(full - 1.40) < 1e-9, f"{full}")
    check("tiny-sample edge is regressed toward 1.0",
          1.02 < tiny < 1.06, f"12 PA at 1.40 -> {tiny:.3f}")
    check("regression is monotonic in sample size", tiny < full)

    # And it shows up end-to-end: the lefty's 90-PA vs-LHP split is not
    # taken at face value.
    lefty_platoon = lefty["edge"]["components"]["platoon"]
    check("90-PA split is partially regressed in the live pipeline",
          lefty_platoon["value"] > 0.75,
          f"value={lefty_platoon['value']} from {lefty_platoon['detail']}")

    print("\nScore scaling")
    def score_for(c):
        return scoring.combine({"platoon": {"value": c}})["score"]

    check("neutral composite maps to 50", abs(score_for(1.0) - 50) < 0.01,
          str(score_for(1.0)))
    check("elite matchup lands high but never clips at 100",
          85 < score_for(1.25) < 100, str(round(score_for(1.25), 1)))
    check("extreme matchup still below 100 (ordering preserved)",
          score_for(1.60) < 100 and score_for(1.60) > score_for(1.25),
          f"{score_for(1.60):.2f} > {score_for(1.25):.2f}")
    check("terrible matchup lands low but above 0",
          0 < score_for(0.75) < 15, str(round(score_for(0.75), 1)))
    check("scale is monotonic",
          all(score_for(c) < score_for(c + 0.05) for c in
              [0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.4]))

    print("\nPark handedness")
    # Fenway suppresses lefty HR (0.90) and helps righties (1.04).
    r_park = righty["edge"]["components"]["park"]["hr_factor"]
    l_park = lefty["edge"]["components"]["park"]["hr_factor"]
    check("righty gets Fenway's RHB HR factor", r_park == 1.04, str(r_park))
    check("lefty gets Fenway's LHB HR factor", l_park == 0.90, str(l_park))

    print("\nStacks & ordering")
    check("stack scores computed",
          game["away"]["stack_score"] is not None and game["home"]["stack_score"] is not None,
          f"NYY {game['away']['stack_score']} / BOS {game['home']['stack_score']}")
    check("hitters sorted by score descending",
          all(
              game["away"]["hitters"][i]["edge"]["score"]
              >= game["away"]["hitters"][i + 1]["edge"]["score"]
              for i in range(len(game["away"]["hitters"]) - 1)
          ))

    print("\nJSON serialisation")
    import json

    try:
        blob = json.dumps(slate, default=str)
        check("slate serialises to JSON", len(blob) > 500, f"{len(blob):,} bytes")
    except Exception as exc:  # noqa: BLE001
        check("slate serialises to JSON", False, str(exc))

    print("\nAnalysis payload")
    from app.services.analysis import _compact_slate

    compact = _compact_slate(slate)
    check("compact payload built", bool(compact["games"]))
    check("compact payload is much smaller than the full slate",
          len(json.dumps(compact, default=str)) < len(json.dumps(slate, default=str)),
          f"{len(json.dumps(compact, default=str)):,} vs {len(json.dumps(slate, default=str)):,} bytes")

    print("\n" + "=" * 60)
    print(f"{len(PASS)} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  FAILED: {name}")
    print()
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
