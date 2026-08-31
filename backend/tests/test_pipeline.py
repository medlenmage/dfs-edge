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
import copy
import csv
import io
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import cache  # noqa: E402
from app.clients import draftkings, fantasylabs, mlb, odds, rotowire, rotowire_umpires, savant, weather  # noqa: E402
from app.data import parks  # noqa: E402
from app.services import (  # noqa: E402
    atbat_sim,
    contest,
    contest_results,
    dk_entries,
    dk_entry_manager,
    inhouse_projections,
    late_swap,
    lineup_export,
    lineup_watch,
    mlb_dk_points,
    mlb_slate,
    optimizer,
    player_match,
    projections,
    salaries,
    scoring,
    variance,
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


def hit(pa, ops, avg=0.260, slg=0.440, hr=15, sb=3):
    return {
        "pa": pa, "ab": int(pa * 0.9), "avg": avg, "obp": ops - slg, "slg": slg,
        "ops": ops, "hr": hr, "rbi": 50, "runs": 50, "sb": sb, "hits": 100,
        "doubles": 20, "triples": 1, "iso": round(slg - avg, 3),
        "k_pct": 0.22, "bb_pct": 0.09, "hr_per_pa": round(hr / pa, 4),
        "sb_per_pa": round(sb / pa, 4) if pa else None,
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
        # 103 and 202 are deliberately given the same season OPS (0.790)
        # so a stolen-base-component test can isolate speed as the only
        # real difference between them -- a burner (103, 35 SB) versus a
        # near-zero-steal hitter (202, 1 SB) who'd have looked identical
        # before this component existed.
        101: hit(580, 0.850, 0.275, 0.500, 30, sb=10),
        102: hit(520, 0.810, 0.270, 0.450, 21, sb=5),
        103: hit(530, 0.790, 0.268, 0.450, 20, sb=35),
        201: hit(575, 0.780, 0.262, 0.445, 25, sb=8),
        202: hit(480, 0.790, 0.275, 0.440, 13, sb=1),
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

# Season-long quality (BULLPEN) and recent workload (BULLPEN_WORKLOAD)
# are deliberately given OPPOSITE reads here: the Red Sox pen is bad all
# year (5.20 ERA) but well-rested the last 2 days (6 outs); the Yankees
# pen has a fine season (3.00 ERA) but got hammered recently (30 outs,
# an extra-inning-game-sized workload) -- isolates the two signals from
# each other, proving neither can substitute for the other.
BULLPEN_WORKLOAD = {
    111: {"outs": 6, "appearances": 2},    # Red Sox -- lightly used recently
    147: {"outs": 30, "appearances": 9},   # Yankees -- heavily taxed recently
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

FAKE_VEGAS = [
    {
        "event_id": 205310620,
        "home_team": "Boston Red Sox", "away_team": "New York Yankees",
        "game_time_utc": f"{DAY}T23:10:00",
        "home_spread_open": 1.5, "home_spread_current": 1.0,
        "away_spread_open": -1.5, "away_spread_current": -1.0,
        "home_moneyline_open": 110, "home_moneyline_current": 117,
        "away_moneyline_open": -132, "away_moneyline_current": -135,
        "total_open": 9.0, "total_current": 9.5,
        "home_implied_runs_open": 3.9, "home_implied_runs_current": 4.1,
        "away_implied_runs_open": 5.1, "away_implied_runs_current": 5.4,
    }
]

FAKE_UMPIRES = {
    # This test slate's real game (away@home = NYY@BOS) -- a clearly
    # hitter-favouring umpire (RPG above, KPG below, the league average
    # computed from BOTH entries here).
    "NYY@BOS": {"name": "Test Ump Hitter-Friendly", "rpg": 10.0, "kpg": 14.0, "games": 20},
    # A second, unrelated game -- purely so league_average() has more
    # than one real umpire to average across.
    "AAA@BBB": {"name": "Test Ump Pitcher-Friendly", "rpg": 7.0, "kpg": 20.0, "games": 20},
}

FAKE_WEATHER = {
    "temp_f": 88.0, "humidity_pct": 55, "precip_chance_pct": 5,
    "wind_mph": 12.0, "wind_dir_deg": 200.0, "pressure_hpa": 1010,
    "cloud_cover_pct": 20, "forecast_time_utc": f"{DAY}T23:00",
}

def salary_row(name, team, salary, avg_points, game_info=""):
    return {
        "name": name, "normalized_name": salaries.normalize_name(name),
        "team": team, "position": "", "salary": salary, "avg_points": avg_points,
        "game_info": game_info,
    }


SALARIES = [
    salary_row("Big Righty Bat", "NYY", 4200, 9.5),
    salary_row("Boston Slugger", "BOS", 5600, 11.0),
    salary_row("Lefty McLefterson", "BOS", 8800, 16.0),
    # "Punchless Lefty" is deliberately absent -- exercises the no-match path.
]


def projection_row(name, team, fpts, ownership_pct, lineup_spot=None):
    return {
        "name": name, "normalized_name": projections.normalize_name(name),
        "team": team, "position": "", "fpts": fpts, "ownership_pct": ownership_pct,
        "lineup_spot": lineup_spot,
    }


PROJECTIONS = [
    # lineup_spot=3 -- RotoWire's own projected batting order, real
    # end-to-end coverage for mlb_slate.py attaching it onto the hitter
    # as `projected_batting_order` (see the platoon-logic checks below).
    projection_row("Big Righty Bat", "NYY", 12.4, 18.7, lineup_spot=3),
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


async def fake_lines(sport="mlb", *, day=None, force=False):
    return FAKE_LINES


async def fake_fantasylabs_vegas(day, *, force=False):
    return FAKE_VEGAS


async def fake_todays_umpires(*, force=False):
    return FAKE_UMPIRES


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


async def fake_bullpen_workload(day):
    return BULLPEN_WORKLOAD


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
    # The 40-man is a real superset of the active roster (a call-up
    # isn't moved onto the active roster until his transaction posts),
    # so the default fake mirrors it -- individual tests override this
    # to exercise the call-up case specifically.
    mlb.get_40man_roster = fake_roster
    mlb.get_league_splits = fake_splits
    mlb.get_league_season = fake_season
    mlb.get_recent_form = fake_recent
    mlb.get_lineups = fake_lineups
    mlb.get_team_injuries = fake_injuries
    odds.get_game_lines = fake_lines
    fantasylabs.get_vegas_odds = fake_fantasylabs_vegas
    rotowire_umpires.get_todays_umpires = fake_todays_umpires
    weather.get_game_weather = fake_weather
    savant.get_hitter_batted_ball = fake_savant_hit
    savant.get_pitcher_batted_ball = fake_savant_pitch
    mlb.get_bullpen_stats = fake_bullpen
    mlb.get_recent_bullpen_workload = fake_bullpen_workload
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
    check("betting (now FantasyLabs-sourced) line matched by team name",
          game["betting"].get("total") == 9.5, str(game["betting"]))
    check("betting.book correctly labels the new source",
          game["betting"].get("book") == "FantasyLabs (consensus)", str(game["betting"]))
    check("implied runs split correctly (sourced from FantasyLabs' current line, not The Odds API)",
          game["away"]["implied_runs"] == 5.4 and game["home"]["implied_runs"] == 4.1,
          str((game["away"]["implied_runs"], game["home"]["implied_runs"])))
    check("FantasyLabs vegas line matched by team name (open + current total)",
          game["vegas"].get("total_open") == 9.0 and game["vegas"].get("total_current") == 9.5,
          str(game["vegas"]))
    check("FantasyLabs open/current spread, moneyline, implied runs split correctly per side",
          game["home"]["vegas_spread_open"] == 1.5
          and game["home"]["vegas_spread_current"] == 1.0
          and game["home"]["vegas_moneyline_open"] == 110
          and game["home"]["vegas_moneyline_current"] == 117
          and game["home"]["vegas_implied_runs_open"] == 3.9
          and game["home"]["vegas_implied_runs_current"] == 4.1
          and game["away"]["vegas_spread_open"] == -1.5
          and game["away"]["vegas_moneyline_current"] == -135
          and game["away"]["vegas_implied_runs_current"] == 5.4,
          str({k: v for k, v in game["home"].items() if k.startswith("vegas_")}))
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
    check("pitcher edge has all eight components",
          set(home_edge["components"]) == {
              "opp_lineup", "strikeout_potential", "team_runs_against",
              "contact_quality_allowed", "own_quality", "park", "weather", "umpire",
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

    print("\nDeriving salaries from a RotoWire upload (one file instead of two)")

    rw_with_sal = projections.parse_rotowire_csv(
        "PLAYER,RW Pick,TEAM,SAL,POS,VAL,RST%,OPP,LINEUP,FPTS,MIN EXP,MAX EXP\n"
        "Shohei Ohtani,-,LAD,7000,1B/OF,1.89,14.43,COL,1,13.21,0,100\n"
        "Bobby Witt,-,KC,6000,SS,1.84,16.14,ATH,2,11.05,0,100\n"
        "Freddy Fermin,-,SD,2400,C,0.73,0.00,NYM,BN,1.74,0,100\n"
    )
    check("parse_rotowire_csv captures the SAL column alongside FPTS/RST%",
          [r["salary"] for r in rw_with_sal] == [7000, 6000, 2400], str(rw_with_sal))
    check("parse_rotowire_csv captures LINEUP as a real int projected batting spot, and 'BN' "
          "(bench, not projected to start) as None rather than a bogus value",
          [r["lineup_spot"] for r in rw_with_sal] == [1, 2, None], str(rw_with_sal))

    derived = salaries.from_rotowire_rows(rw_with_sal)
    check("from_rotowire_rows builds one DK-salary-shaped row per priced player",
          len(derived) == 3 and derived[0]["salary"] == 7000 and derived[0]["position"] == "1B/OF",
          str(derived[0]))
    check("from_rotowire_rows leaves game_info/dk_id empty (RotoWire doesn't expose either)",
          derived[0]["game_info"] == "" and derived[0]["dk_id"] == "", str(derived[0]))

    rw_no_sal = projections.parse_rotowire_csv(
        "PLAYER,RW Pick,TEAM,SAL,POS,VAL,RST%,OPP,LINEUP,FPTS,MIN EXP,MAX EXP\n"
        "No Salary Guy,-,LAD,,1B,1.89,14.43,COL,1,13.21,0,100\n"
    )
    check("a RotoWire row with an empty SAL column is skipped, not treated as $0",
          salaries.from_rotowire_rows(rw_no_sal) == [])

    print("\nMatching a RotoWire upload against an already-loaded DK slate")
    dk_rows = [
        salary_row("Shohei Ohtani", "LAD", 7000, 0),
        salary_row("Bobby Witt Jr.", "KC", 6000, 0),   # RotoWire drops the "Jr." below
        salary_row("Nicholas Castellanos", "CIN", 5000, 0),  # RotoWire uses "Nick" below
    ]
    dk_lookup = salaries.build_lookup(dk_rows)
    rw_upload = projections.parse_rotowire_csv(
        "PLAYER,RW Pick,TEAM,SAL,POS,VAL,RST%,OPP,LINEUP,FPTS,MIN EXP,MAX EXP\n"
        "Shohei Ohtani,-,LAD,7000,1B/OF,1.89,14.43,COL,1,13.21,0,100\n"
        "Bobby Witt,-,KC,6000,SS,1.84,16.14,ATH,2,11.05,0,100\n"
        "Nick Castellanos,-,CIN,5000,OF,1.20,10.00,PIT,1,9.50,0,100\n"
        "Totally Unrostered Guy,-,LAD,3000,OF,1.00,5.00,COL,1,6.00,0,100\n"
    )
    bad = player_match.unmatched(rw_upload, dk_lookup, fuzzy=True)
    check("suffix drop (Jr.) and nickname (Nick/Nicholas) both match the DK slate via player_match",
          bad == ["Totally Unrostered Guy"], str(bad))
    check("the same report, expressed as an upload response would show it",
          {"matched_to_slate": len(rw_upload) - len(bad), "unmatched": bad}
          == {"matched_to_slate": 3, "unmatched": ["Totally Unrostered Guy"]})

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

    print("\nName matching across sources that spell a player differently")
    check("nickname vs. legal first name normalise the same way",
          player_match.normalize_name("Nick Castellanos") == player_match.normalize_name("Nicholas Castellanos"))
    check("nickname folding doesn't touch an unrelated last name",
          player_match.normalize_name("Mike Trout") != player_match.normalize_name("Mike Moustakas"))

    cin_rows = player_match.build_lookup(
        [{"name": "Nicholas Castellanos", "normalized_name": player_match.normalize_name("Nicholas Castellanos"),
          "team": "CIN", "value": "example"}]
    )
    check("a row filed under the legal first name matches a nickname query, no fuzzy needed",
          player_match.match(cin_rows, "Nick Castellanos", "CIN") is not None)

    cle_rows = player_match.build_lookup(
        [{"name": "Jose Ramirez", "normalized_name": player_match.normalize_name("Jose Ramirez"),
          "team": "CLE", "value": "example"}]
    )
    check("a real typo doesn't match without fuzzy=True",
          player_match.match(cle_rows, "Jose Ramires", "CLE") is None)
    check("the same typo matches with fuzzy=True (same-team, close edit distance)",
          player_match.match(cle_rows, "Jose Ramires", "CLE", fuzzy=True) is not None)
    check("fuzzy=True still returns None when nothing on the team is close",
          player_match.match(cle_rows, "Someone Completely Different", "CLE", fuzzy=True) is None)
    check("fuzzy=True never matches across teams -- a same-named typo on another team stays unmatched",
          player_match.match(cle_rows, "Jose Ramires", "NYY", fuzzy=True) is None)

    unmatched_rows = [
        {"name": "Jose Ramirez", "team": "CLE"},   # exact
        {"name": "Jose Ramires", "team": "CLE"},   # typo, needs fuzzy
        {"name": "Nobody Here", "team": "CLE"},    # genuine non-match
    ]
    check("unmatched() reports only genuine non-matches once fuzzy is on",
          player_match.unmatched(unmatched_rows, cle_rows, fuzzy=True) == ["Nobody Here"])
    check("unmatched() without fuzzy also flags the typo (strict mode)",
          player_match.unmatched(unmatched_rows, cle_rows) == ["Jose Ramires", "Nobody Here"])

    print("\nTwo-way player (bio position 'TWP', e.g. Ohtani) shown as a hitter except on his own start day")

    TWP_ID = 9010
    twp_bio = {TWP_ID: {"id": TWP_ID, "name": "Shohei Otani", "throws": "R", "position": "TWP", "bats": "L"}}
    twp_season = {TWP_ID: hit(500, 0.900, 0.300, 0.600, 40, sb=15)}

    async def fake_people_twp(ids):
        return {i: twp_bio[i] for i in ids if i in twp_bio}

    twp_data = {
        "bullpen": {},
        "bullpen_workload": {},
        "hit_season": twp_season,
        "hit_vl": {}, "hit_vr": {}, "hit_home": {}, "hit_away": {},
        "hit_recent": {},
        "savant_hit": {},
    }
    twp_baselines = {
        "hitter_ops_vl": 0.750, "hitter_ops_vr": 0.750,
        "pitcher_ops_vl": 0.700, "pitcher_ops_vr": 0.700,
        "hitter_barrel": None, "hitter_hard_hit": None, "hitter_xwoba": None,
        "hitter_sb_per_pa": 0.02,
        "hitter_hr_per_pa": 0.03,
        "pitcher_hr_per_9": 1.1,
        "bullpen_era": 4.0,
        "bullpen_workload_outs": 20.0,
    }
    twp_env = {
        "park": parks.get_park("LAD"), "roof_closed": True, "temp_fx": None, "wind_fx": None,
    }

    original_get_people = mlb.get_people
    mlb.get_people = fake_people_twp

    hitters_on_dh_day = await mlb_slate._team_hitters(
        119, 2026, twp_data, twp_baselines, twp_env,
        opposing_pitcher=None, opponent_team_id=None, is_home=True,
        implied_runs=4.4, confirmed=[TWP_ID], team_abbrev="LAD",
        own_pitcher_id=None,  # someone else is starting for LAD today
    )
    hitters_on_start_day = await mlb_slate._team_hitters(
        119, 2026, twp_data, twp_baselines, twp_env,
        opposing_pitcher=None, opponent_team_id=None, is_home=True,
        implied_runs=4.4, confirmed=[TWP_ID], team_abbrev="LAD",
        own_pitcher_id=TWP_ID,  # he himself is today's starter
    )

    mlb.get_people = original_get_people

    check("a two-way player appears in the hitters list on a day he isn't his team's own starter",
          any(h["id"] == TWP_ID for h in hitters_on_dh_day), str(hitters_on_dh_day))
    check("the same two-way player is excluded from hitters on his own start day",
          not any(h["id"] == TWP_ID for h in hitters_on_start_day), str(hitters_on_start_day))

    print("\nProjected starting pitcher fallback (mlb_slate._projected_starter, no real "
          "probable pitcher announced yet)")

    async def fake_roster_projstarter(team_id, season):
        return [70001, 70002] if team_id == 900 else []

    async def fake_40man_projstarter(team_id, season):
        # A real 40-man superset: the active pair above plus a call-up
        # whose transaction hasn't posted, so he's on the 40-man but not
        # the active roster. Deliberately does NOT include the 60-day-IL
        # arm, which a real 60-day stint removes from the 40-man.
        return [70001, 70002, 70004] if team_id == 900 else []

    async def fake_people_projstarter(ids):
        return {
            70001: {"id": 70001, "name": "Ace Starter", "throws": "R"},
            70002: {"id": 70002, "name": "Middle Reliever", "throws": "L"},
            70003: {"id": 70003, "name": "Coming Off IL", "throws": "R"},
            70004: {"id": 70004, "name": "Called Up Arm", "throws": "L"},
        }

    async def fake_injuries_projstarter(team_id, season):
        # Not on the ACTIVE roster fake above -- only reachable through
        # the injured-list fallback, standing in for a real 60-day-IL
        # pitcher whose activation transaction hasn't posted yet.
        return [{"id": 70003, "name": "Coming Off IL", "status_code": "D60"}] if team_id == 900 else []

    original_get_active_roster = mlb.get_active_roster
    original_get_40man_roster = mlb.get_40man_roster
    original_get_people_2 = mlb.get_people
    original_get_injuries_projstarter = mlb.get_team_injuries
    mlb.get_active_roster = fake_roster_projstarter
    mlb.get_40man_roster = fake_40man_projstarter
    mlb.get_people = fake_people_projstarter
    mlb.get_team_injuries = fake_injuries_projstarter

    projstarter_lookup = projections.build_lookup(
        [
            projection_row("Ace Starter", "LAD", 18.5, 20.0, lineup_spot=None),
            projection_row("Middle Reliever", "LAD", 3.2, 1.0, lineup_spot=None),
        ]
    )
    # position defaults to "" in projection_row -- real projected-pitcher
    # rows need an actual pitcher position tag to be considered at all.
    for row in projstarter_lookup.values():
        row["position"] = "P"

    resolved_starter = await mlb_slate._projected_starter(900, "LAD", 2026, projstarter_lookup)
    check("_projected_starter picks the highest-FPTS pitcher-position row for the team (the real "
          "starter, not a lower-usage reliever also in the pool) and resolves his real MLB id via "
          "roster name-matching",
          resolved_starter == {"id": 70001, "name": "Ace Starter"}, str(resolved_starter))

    check("_projected_starter returns None with no projections data loaded at all",
          await mlb_slate._projected_starter(900, "LAD", 2026, {}) is None)
    check("_projected_starter returns None for a team with no pitcher-position rows in the "
          "projections file",
          await mlb_slate._projected_starter(900, "NYY", 2026, projstarter_lookup) is None)

    unmatched_lookup = projections.build_lookup(
        [projection_row("Totally Unrostered Arm", "LAD", 15.0, 10.0, lineup_spot=None)]
    )
    unmatched_lookup[list(unmatched_lookup)[0]]["position"] = "P"
    check("_projected_starter returns None when the projected name doesn't match anyone on the "
          "team's actual active roster, rather than guessing",
          await mlb_slate._projected_starter(900, "LAD", 2026, unmatched_lookup) is None)

    # A pitcher RotoWire projects as the team's TOP starter (highest
    # FPTS) but who isn't on the active roster fake at all -- only
    # resolvable via the injured-list fallback. Confirmed with the user:
    # RotoWire listing an injured pitcher as a team's top projected
    # starter typically means he's being activated that same day to
    # make the start, so this should resolve, not silently fail the
    # way it would have before the injured-list fallback existed.
    il_activation_lookup = projections.build_lookup(
        [
            projection_row("Coming Off IL", "LAD", 22.0, 25.0, lineup_spot=None),
            projection_row("Ace Starter", "LAD", 18.5, 20.0, lineup_spot=None),
        ]
    )
    for row in il_activation_lookup.values():
        row["position"] = "P"
    il_resolved = await mlb_slate._projected_starter(900, "LAD", 2026, il_activation_lookup)
    check("_projected_starter resolves a same-day-activation candidate (RotoWire's own top "
          "projected starter) who's on the injured list, not the active roster, via the "
          "injured-list fallback rather than returning None",
          il_resolved == {"id": 70003, "name": "Coming Off IL"}, str(il_resolved))

    # A minor-league call-up getting a spot start: on the 40-man, not on
    # the active roster, because MLB doesn't move him across until the
    # transaction posts -- which routinely lands close to first pitch.
    # Real and measured: on 2026-08-29 RotoWire had Matt Wilkinson as
    # SF's starter while the Giants' active roster held 26 without him
    # and their 40-man held 49 with him, and the at-bat engine refused
    # the entire slate over that one unresolvable pitcher.
    callup_lookup = projections.build_lookup(
        [
            projection_row("Called Up Arm", "LAD", 21.0, 24.0, lineup_spot=None),
            projection_row("Ace Starter", "LAD", 18.5, 20.0, lineup_spot=None),
        ]
    )
    for row in callup_lookup.values():
        row["position"] = "P"
    callup_resolved = await mlb_slate._projected_starter(900, "LAD", 2026, callup_lookup)
    check("_projected_starter resolves a call-up starter who's on the 40-man but not the active "
          "roster -- the real case that blocked the at-bat engine on a whole slate",
          callup_resolved == {"id": 70004, "name": "Called Up Arm"}, str(callup_resolved))

    check("the injured-list path still works alongside the 40-man one -- a 60-day-IL arm is "
          "removed from the 40-man, so only the injury fallback can find him",
          (await mlb_slate._projected_starter(900, "LAD", 2026, il_activation_lookup))
          == {"id": 70003, "name": "Coming Off IL"}, "")

    check("_projected_starter still returns None for a name on none of the three rosters, rather "
          "than widening far enough to guess",
          await mlb_slate._projected_starter(900, "LAD", 2026, unmatched_lookup) is None)

    mlb.get_active_roster = original_get_active_roster
    mlb.get_40man_roster = original_get_40man_roster
    mlb.get_people = original_get_people_2
    mlb.get_team_injuries = original_get_injuries_projstarter

    print("\nProjected BATTING ORDER recovery (mlb_slate._projected_lineup_ids) -- the hitter "
          "sibling of the projected-starter fallback above")

    # The real reported bug, measured on 2026-08-30: Boston's projected
    # 1st and 6th hitters were both D60 on MLB's own roster feed -- so
    # off the ACTIVE roster, which is all _team_hitters used to look at
    # -- while RotoWire had them batting and DraftKings had them priced.
    # BOS came out with 7 usable batting spots against the at-bat
    # engine's minimum of 8, and the engine refused the entire slate.
    async def fake_active_hitters(team_id, season):
        return [80001, 80002] if team_id == 901 else []

    async def fake_40man_hitters(team_id, season):
        # The call-up is on the 40-man; the 60-day-IL bat is not (a real
        # 60-day stint removes him from it), so only the injury fallback
        # can reach him -- same split the pitcher fixture above models.
        return [80001, 80002, 80003] if team_id == 901 else []

    async def fake_injuries_hitters(team_id, season):
        return [{"id": 80004, "name": "Activated Off IL", "status_code": "D60"}] if team_id == 901 else []

    hitter_bios = {
        80001: {"id": 80001, "name": "Everyday Guy", "position": "OF", "bats": "R"},
        80002: {"id": 80002, "name": "Regular Bat", "position": "2B", "bats": "L"},
        80003: {"id": 80003, "name": "Called Up Bat", "position": "SS", "bats": "R"},
        80004: {"id": 80004, "name": "Activated Off IL", "position": "1B", "bats": "L"},
    }

    async def fake_people_hitters(ids):
        return {i: hitter_bios[i] for i in ids if i in hitter_bios}

    mlb.get_active_roster = fake_active_hitters
    mlb.get_40man_roster = fake_40man_hitters
    mlb.get_team_injuries = fake_injuries_hitters
    mlb.get_people = fake_people_hitters

    full_order_lookup = projections.build_lookup([
        projection_row("Everyday Guy", "BOS", 9.0, 8.0, lineup_spot=1),
        projection_row("Regular Bat", "BOS", 8.0, 7.0, lineup_spot=2),
        projection_row("Called Up Bat", "BOS", 7.0, 6.0, lineup_spot=3),
        projection_row("Activated Off IL", "BOS", 6.5, 5.0, lineup_spot=4),
    ])
    recovered = await mlb_slate._projected_lineup_ids(901, "BOS", 2026, full_order_lookup, [80001, 80002])
    check("_projected_lineup_ids recovers BOTH a 40-man call-up and a 60-day-IL activation that "
          "RotoWire names in today's batting order but MLB's active roster doesn't carry yet",
          sorted(recovered) == [80003, 80004], str(recovered))

    covered_lookup = projections.build_lookup([
        projection_row("Everyday Guy", "BOS", 9.0, 8.0, lineup_spot=1),
        projection_row("Regular Bat", "BOS", 8.0, 7.0, lineup_spot=2),
    ])
    check("...and returns nothing at all when every projected hitter already resolves off the "
          "active roster -- the common case, which must not widen the pool",
          await mlb_slate._projected_lineup_ids(901, "BOS", 2026, covered_lookup, [80001, 80002]) == [], "")

    no_order_lookup = projections.build_lookup([
        projection_row("Called Up Bat", "BOS", 7.0, 6.0, lineup_spot=None),
    ])
    check("a player with NO projected batting spot is never recovered -- this is deliberately not "
          "a blanket 40-man widening, which would drag in every genuinely-injured player still on it",
          await mlb_slate._projected_lineup_ids(901, "BOS", 2026, no_order_lookup, [80001, 80002]) == [], "")

    check("_projected_lineup_ids returns nothing with no projections file loaded at all",
          await mlb_slate._projected_lineup_ids(901, "BOS", 2026, {}, [80001, 80002]) == [], "")

    # End to end through _team_hitters: an unconfirmed lineup should now
    # produce hitters for the recovered players too, and the low-PA
    # noise filter must not undo the recovery.
    recovery_data = {
        "bullpen": {}, "bullpen_workload": {},
        "hit_season": {
            80001: hit(400, 0.800, 0.280, 0.520, 30),
            80002: hit(380, 0.780, 0.270, 0.510, 28),
            80003: hit(10, 0.700, 0.250, 0.450, 20),   # a real call-up, 10 PA
            80004: hit(150, 0.820, 0.290, 0.530, 25),
            80005: hit(10, 0.600, 0.220, 0.380, 15),   # bench, 10 PA, NOT projected
        },
        "hit_vl": {}, "hit_vr": {}, "hit_home": {}, "hit_away": {},
        "hit_recent": {}, "savant_hit": {},
    }
    recovery_env = {"park": parks.get_park("BOS"), "roof_closed": True, "temp_fx": None, "wind_fx": None}

    hitter_bios[80005] = {"id": 80005, "name": "Deep Bench", "position": "C", "bats": "R"}

    async def fake_active_with_bench(team_id, season):
        return [80001, 80002, 80005] if team_id == 901 else []

    mlb.get_active_roster = fake_active_with_bench
    recovered_hitters = await mlb_slate._team_hitters(
        901, 2026, recovery_data, twp_baselines, recovery_env,
        opposing_pitcher=None, opponent_team_id=None, is_home=True,
        implied_runs=4.4, confirmed=[], team_abbrev="BOS",
        projection_lookup=full_order_lookup,
    )
    recovered_ids = {h["id"] for h in recovered_hitters}
    check("_team_hitters with an unconfirmed lineup now includes the players RotoWire projects to "
          "bat who aren't on the active roster -- the whole reported failure",
          {80003, 80004} <= recovered_ids, str(sorted(recovered_ids)))
    check("the <25 PA noise filter no longer drops a player RotoWire names in TODAY's order -- a "
          "10-PA call-up projected to bat 3rd is a starter, not noise",
          80003 in recovered_ids, str(sorted(recovered_ids)))
    check("...while a 10-PA bench player NOT in today's projected order is still filtered out, so "
          "the exemption stays scoped to real same-day evidence",
          80005 not in recovered_ids, str(sorted(recovered_ids)))
    check("every recovered hitter carries his real projected batting spot through to the slate",
          sorted(h["projected_batting_order"] for h in recovered_hitters if h["projected_batting_order"])
          == [1, 2, 3, 4],
          str([(h["id"], h.get("projected_batting_order")) for h in recovered_hitters]))

    mlb.get_active_roster = original_get_active_roster
    mlb.get_40man_roster = original_get_40man_roster
    mlb.get_people = original_get_people_2
    mlb.get_team_injuries = original_get_injuries_projstarter

    print("\nRotoWire window auto-match (pick_best_team_match)")

    # The real reported bug: the user had DK's LATE NIGHT slate loaded
    # (ARI/ATH/BAL/LAA/PHI/SF), clicked Refresh from RotoWire, and got
    # the "All" window activated -- which doesn't even contain the
    # late-night-only games, so their slate's projections never loaded.
    _late = {"AZ", "ATH", "BAL", "LAA", "PHI", "SF"}
    _windows = {
        "All": {"NYY", "BOS", "DET", "LAD", "MIN", "CWS", "STL", "PIT", "CHC", "CIN",
                "TOR", "SEA", "WSH", "MIA", "CLE", "KC", "NYM", "HOU", "TB",
                "SD", "ATL", "COL", "MIL"},
        "Night": _late | {"TEX", "MIN"},
        "Late Night": set(_late),
    }
    check("with a Late Night DK slate loaded, the Late Night window wins the auto-match -- "
          "the exact reported bug this exists to prevent",
          salaries.pick_best_team_match(_windows, _late) == "Late Night", "")
    check("full coverage with FEWER extra teams beats full coverage with more -- 'Night' "
          "contains every late game plus two others, but the exact window's projections are "
          "the ones that belong to this slate",
          salaries.pick_best_team_match(
              {"Night": _windows["Night"], "Late Night": _windows["Late Night"]}, _late)
          == "Late Night", "")
    check("a main-slate DK upload auto-matches the All window, so the old default flow is "
          "unchanged for the common case",
          salaries.pick_best_team_match(_windows, {"NYY", "BOS", "DET", "LAD"}) == "All", "")
    check("nothing covering even half the DK slate returns None -- the caller falls back to "
          "the main window rather than activating projections that mostly miss the games",
          salaries.pick_best_team_match(
              {"All": {"NYY", "BOS"}}, {"AZ", "SF", "PHI", "LAA", "BAL", "ATH"}) is None, "")
    check("no DK slate loaded at all returns None (nothing to match against)",
          salaries.pick_best_team_match(_windows, set()) is None, "")

    print("\nDK slate detection (Game Info column -> which games are in this slate)")
    check("parse_game_info extracts the away@home pair, ignoring date/time",
          salaries.parse_game_info("NYY@BOS 08/16/2026 07:05PM ET") == ("NYY", "BOS"))
    check("parse_game_info normalises DK's team codes via the shared alias table",
          salaries.slate_games([salary_row("X", "ARI", 3000, 5.0, game_info="ARI@LAD 08/16/2026 10:10PM ET")])
          == [{"away": "AZ", "home": "LAD"}])
    check("parse_game_info returns None for non-matchup text (postponed, in progress)",
          salaries.parse_game_info("Postponed") is None)
    check("parse_game_info returns None for an empty/missing column",
          salaries.parse_game_info("") is None)
    check("slate_games dedupes multiple players from the same game into one entry",
          salaries.slate_games([
              salary_row("A", "NYY", 4000, 8.0, game_info="NYY@BOS 08/16/2026 07:05PM ET"),
              salary_row("B", "BOS", 3000, 6.0, game_info="NYY@BOS 08/16/2026 07:05PM ET"),
          ]) == [{"away": "NYY", "home": "BOS"}])
    check("slate_games skips rows with unparseable Game Info rather than raising",
          salaries.slate_games([salary_row("A", "NYY", 4000, 8.0, game_info="")]) == [])

    print("\nDK salary CSV: header-row detection (two real export shapes)")

    flat_csv = (
        "Position,Name + ID,Name,ID,Roster Position,Salary,Game Info,TeamAbbrev,AvgPointsPerGame\n"
        "SP,Chris Sale (43854626),Chris Sale,43854626,P,10300,ATL@MIN 08/17/2026 07:40PM ET,ATL,23.26\n"
    )
    flat_rows = salaries.parse_dk_csv(flat_csv)
    check("the flat player-pool export (header on row 1) still parses",
          len(flat_rows) == 1 and flat_rows[0]["name"] == "Chris Sale", str(flat_rows))

    # The lineup-builder page's "Export to CSV" button ships a wider
    # file: an empty roster-slot template occupies the first several
    # rows/columns, and the real player-table header is embedded well
    # past row 1 -- this is the exact shape that silently produced zero
    # players before _find_dk_header_row existed (csv.DictReader took
    # the roster-template row as the header, so every expected column
    # name -- Name, Salary, TeamAbbrev -- was simply missing).
    wide_csv = (
        "P,P,C,1B,2B,3B,SS,OF,OF,OF,,Instructions\n"
        ",,,,,,,,,,,1. Locate the player you want to select in the list below\n"
        ",,,,,,,,,,,Position,Name + ID,Name,ID,Roster Position,Salary,Game Info,TeamAbbrev,AvgPointsPerGame\n"
        ",,,,,,,,,,,SP,Chris Sale (43854626),Chris Sale,43854626,P,10300,ATL@MIN 08/17/2026 07:40PM ET,ATL,23.26\n"
        ",,,,,,,,,,,SP,Tarik Skubal (43854627),Tarik Skubal,43854627,P,10000,LAD@COL 08/17/2026 08:40PM ET,LAD,21.76\n"
    )
    wide_rows = salaries.parse_dk_csv(wide_csv)
    check("the lineup-builder export (header embedded mid-file, offset past column 0) now parses",
          len(wide_rows) == 2, str(wide_rows))
    check("wide-export rows are parsed correctly despite the leading empty columns",
          wide_rows[0] == {
              # "chris" folds to "christopher" -- see player_match.NICKNAMES
              "name": "Chris Sale", "normalized_name": "christopher sale", "team": "ATL",
              "position": "SP", "salary": 10300, "avg_points": 23.26,
              "game_info": "ATL@MIN 08/17/2026 07:40PM ET", "dk_id": "43854626",
          }, str(wide_rows[0]))

    check("a CSV with no recognizable DK header returns no players rather than raising",
          salaries.parse_dk_csv("just,some,random,csv\n1,2,3,4\n") == [])

    print("\nDraftKings live slate import (clients/draftkings.py) -- no manual CSV needed")

    # Fixture shaped exactly like DraftKings' real lobby response
    # (confirmed live against https://www.draftkings.com/lobby/getcontests
    # and https://api.draftkings.com/draftgroups/v1/draftgroups/{id}/draftables
    # while building this feature): a Classic multi-game "Early" slate,
    # a Classic single-game slate, a Snake draft group sharing the same
    # games (must be excluded -- different roster rules), and a slate
    # on a different day entirely (must be excluded by date).
    dk_lobby_fixture = {
        "DraftGroups": [
            {
                "DraftGroupId": 111, "GameTypeId": 2, "GameCount": 2,
                "StartDate": "2026-08-19T16:35:00.0000000Z",
                "StartDateEst": "2026-08-19T12:35:00.0000000",
                "ContestStartTimeSuffix": " (Early)", "GameSetKey": "SETA",
            },
            {
                "DraftGroupId": 112, "GameTypeId": 178, "GameCount": 2,  # Snake -- excluded
                "StartDate": "2026-08-19T16:35:00.0000000Z",
                "StartDateEst": "2026-08-19T12:35:00.0000000",
                "ContestStartTimeSuffix": " (Snake Early)", "GameSetKey": "SETA",
            },
            {
                "DraftGroupId": 113, "GameTypeId": 114, "GameCount": 1,
                "StartDate": "2026-08-19T22:35:00.0000000Z",
                "StartDateEst": "2026-08-19T18:35:00.0000000",
                "ContestStartTimeSuffix": " (NYY @ BAL)", "GameSetKey": "SETB",
            },
            {
                "DraftGroupId": 114, "GameTypeId": 2, "GameCount": 3,  # wrong day -- excluded
                "StartDate": "2026-08-20T16:35:00.0000000Z",
                "StartDateEst": "2026-08-20T12:35:00.0000000",
                "ContestStartTimeSuffix": None, "GameSetKey": "SETC",
            },
        ],
        "GameSets": [
            {
                "GameSetKey": "SETA",
                "Competitions": [
                    {"GameId": 1, "Description": "DET @ PIT", "StartDate": "2026-08-19T16:35:00.0000000Z"},
                    {"GameId": 2, "Description": "SD @ NYM", "StartDate": "2026-08-19T17:10:00.0000000Z"},
                ],
            },
            {
                "GameSetKey": "SETB",
                "Competitions": [
                    {"GameId": 3, "Description": "NYY @ BAL", "StartDate": "2026-08-19T22:35:00.0000000Z"},
                ],
            },
        ],
    }

    dk_slates = draftkings._parse_slates(dk_lobby_fixture, "2026-08-19")
    check("_parse_slates keeps only Classic slates (excludes Snake) starting on the requested day",
          {s["draft_group_id"] for s in dk_slates} == {111, 113}, str(dk_slates))
    early = next(s for s in dk_slates if s["draft_group_id"] == 111)
    check("_parse_slates labels a slate from its ContestStartTimeSuffix",
          early["label"] == "Early", str(early))
    check("_parse_slates pulls real games (teams + start time) from the matching GameSet",
          early["games"] == [
              {"game_id": 1, "away": "DET", "home": "PIT", "start_time_utc": "2026-08-19T16:35:00.0000000Z"},
              {"game_id": 2, "away": "SD", "home": "NYM", "start_time_utc": "2026-08-19T17:10:00.0000000Z"},
          ], str(early["games"]))
    single_game = next(s for s in dk_slates if s["draft_group_id"] == 113)
    check("an unset ContestStartTimeSuffix falls back to a 'Main' label",
          draftkings._parse_slates(
              {**dk_lobby_fixture, "DraftGroups": [dk_lobby_fixture["DraftGroups"][3]]}, "2026-08-20"
          )[0]["label"] == "Main")
    check("_parse_slates keeps a real single-game Classic slate too",
          single_game["game_count"] == 1, str(single_game))

    dk_draftables_fixture = {
        "draftables": [
            {
                "displayName": "Shohei Ohtani", "teamAbbreviation": "LAD", "position": "1B/OF",
                "salary": 7000, "playerDkId": 999001, "isDisabled": False,
                "competition": {"name": "LAD @ COL"},
                "draftStatAttributes": [{"id": 408, "value": "13.2"}, {"id": -22, "value": "Both"}],
            },
            {
                # No salary -- DK sometimes lists these too; must be skipped.
                "displayName": "No Salary Guy", "teamAbbreviation": "LAD", "position": "OF",
                "salary": None, "playerDkId": 999002, "isDisabled": False,
                "competition": {"name": "LAD @ COL"}, "draftStatAttributes": [],
            },
            {
                # isDisabled -- truly undraftable, must be skipped.
                "displayName": "Scratched Guy", "teamAbbreviation": "LAD", "position": "SS",
                "salary": 3000, "playerDkId": 999003, "isDisabled": True,
                "competition": {"name": "LAD @ COL"}, "draftStatAttributes": [],
            },
        ]
    }
    dk_rows = draftkings._parse_draftables(dk_draftables_fixture)
    check("_parse_draftables skips players with no salary and players DK flagged isDisabled",
          len(dk_rows) == 1 and dk_rows[0]["dk_id"] == "999001", str(dk_rows))
    check("_parse_draftables produces the exact row shape salaries.parse_dk_csv() does, "
          "so salaries.store() accepts it with no changes downstream",
          dk_rows[0] == {
              "name": "Shohei Ohtani", "normalized_name": "shohei ohtani", "team": "LAD",
              "position": "1B/OF", "salary": 7000, "avg_points": 13.2,
              "game_info": "LAD@COL", "dk_id": "999001",
          }, str(dk_rows[0]))
    check("the game_info DraftKings gives us (with spaces) still matches this app's own parse_game_info()",
          salaries.parse_game_info(dk_rows[0]["game_info"]) == ("LAD", "COL"), str(dk_rows[0]["game_info"]))

    print("\nRotoWire live projections import (clients/rotowire.py) -- no manual CSV needed")

    # Fixture shaped exactly like RotoWire's real slate-list.php response
    # (confirmed live against https://www.rotowire.com/daily/mlb/api/
    # slate-list.php and .../players.php while building this feature):
    # the main "All" Classic slate (the one this app wants by default),
    # an "Early" Classic slate on the same day (real, confirmed live
    # field -- the early-games-only slate), an "Afternoon" Classic slate
    # on the same day (also real, confirmed live), a Turbo Classic slate
    # on the same day (not the default and not "Early"/"Afternoon" --
    # must be excluded from all three pickers), and a Showdown slate
    # (wrong contest type -- excluded).
    rw_slate_list_fixture = {
        "slates": [
            {
                "slateID": 26987, "contestType": "Classic", "slateName": "All",
                "startDateOnly": "2026-08-21", "defaultSlate": True,
            },
            {
                "slateID": 26988, "contestType": "Classic", "slateName": "Early",
                "startDateOnly": "2026-08-21", "defaultSlate": False,
            },
            {
                "slateID": 26990, "contestType": "Classic", "slateName": "Afternoon",
                "startDateOnly": "2026-08-21", "defaultSlate": False,
            },
            {
                "slateID": 26989, "contestType": "Classic", "slateName": "Turbo",
                "startDateOnly": "2026-08-21", "defaultSlate": False,
            },
            {
                "slateID": 26985, "contestType": "Showdown", "slateName": "ATL @ MIL",
                "startDateOnly": "2026-08-21", "defaultSlate": True,
            },
        ]
    }
    rw_slate = rotowire.pick_classic_slate(rw_slate_list_fixture, "All")
    check("pick_classic_slate picks the main default Classic slate by its defaultSlate FLAG, "
          "not by name -- the Early, Afternoon, Turbo and Showdown ones are all excluded",
          rw_slate["slateID"] == 26987, str(rw_slate))
    check("the picked slate carries the real slate date this app should trust as the day these "
          "projections belong to",
          rw_slate["startDateOnly"] == "2026-08-21", str(rw_slate))

    check("pick_classic_slate picks a NAMED window (Early) rather than the default one",
          rotowire.pick_classic_slate(rw_slate_list_fixture, "Early")["slateID"] == 26988, "")
    check("pick_classic_slate picks the Afternoon window specifically",
          rotowire.pick_classic_slate(rw_slate_list_fixture, "Afternoon")["slateID"] == 26990, "")

    # A missing window is the NORMAL case, not a failure -- most days
    # don't run every window, and a real 2026-08-29 slate list carried
    # no Early slate at all. Returning None is what lets the caller
    # skip it and carry on scraping the rest.
    check("pick_classic_slate returns None (not an error) for a window that isn't live today -- "
          "a missing window is normal, and the loop just moves on",
          rotowire.pick_classic_slate(rw_slate_list_fixture, "Late Night") is None, "")
    check("pick_classic_slate returns None for a Classic window when the payload is empty",
          rotowire.pick_classic_slate({"slates": []}, "All") is None, "")
    check("pick_classic_slate never returns a Showdown slate for a Classic window name",
          rotowire.pick_classic_slate(rw_slate_list_fixture, "ATL @ MIL") is None, "")

    live_windows = rotowire.pick_live_classic_slates(rw_slate_list_fixture)
    check("pick_live_classic_slates returns every Classic window that exists, skipping the ones "
          "that don't and excluding Showdown entirely",
          [w["windowName"] for w in live_windows] == ["All", "Early", "Afternoon", "Turbo"],
          str([w["windowName"] for w in live_windows]))
    check("...each tagged with the window name it matched, so the caller can label it",
          all("windowName" in w for w in live_windows), "")

    # The default slate also carries a real slateName ("All"), so a
    # naive loop would return it twice -- once by flag, once by name.
    check("the same slate is never returned twice under two window labels",
          len({w["slateID"] for w in live_windows}) == len(live_windows),
          str([w["slateID"] for w in live_windows]))

    # A real day with no main slate at all still yields its other
    # windows rather than coming back empty.
    no_default = {"slates": [
        {**s, "defaultSlate": False} for s in rw_slate_list_fixture["slates"]
        if s["slateName"] != "All"
    ]}
    check("a day with no default 'All' slate still returns the windows that DO exist",
          [w["windowName"] for w in rotowire.pick_live_classic_slates(no_default)]
          == ["Early", "Afternoon", "Turbo"],
          str([w["windowName"] for w in rotowire.pick_live_classic_slates(no_default)]))

    check("pick_live_classic_slates returns an empty list, not an error, when nothing is live",
          rotowire.pick_live_classic_slates({"slates": []}) == [], "")

    # Late Night is a real window confirmed live on RotoWire's own
    # slate list (2026-08-29 carried All/Turbo/Afternoon/Night/Late
    # Night) -- prove it's actually reachable, not just listed.
    late_night_fixture = {"slates": rw_slate_list_fixture["slates"] + [
        {
            "slateID": 27138, "contestType": "Classic", "slateName": "Late Night",
            "startDateOnly": "2026-08-21", "defaultSlate": False,
        },
        {
            "slateID": 27132, "contestType": "Classic", "slateName": "Night",
            "startDateOnly": "2026-08-21", "defaultSlate": False,
        },
    ]}
    check("pick_classic_slate finds the real 'Late Night' window",
          rotowire.pick_classic_slate(late_night_fixture, "Late Night")["slateID"] == 27138, "")
    check("every window including Night and Late Night comes back in the order they actually run",
          [w["windowName"] for w in rotowire.pick_live_classic_slates(late_night_fixture)]
          == ["All", "Early", "Afternoon", "Turbo", "Night", "Late Night"],
          str([w["windowName"] for w in rotowire.pick_live_classic_slates(late_night_fixture)]))

    rw_players_fixture = [
        {
            "firstName": "Shohei", "lastName": "Ohtani",
            "pos": ["1B", "OF"], "team": {"abbr": "LAD"},
            "salary": 6500, "pts": "11.94", "rostership": 6.52,
            "lineup": {"slot": "1", "isConfirmed": False},
        },
        {
            "firstName": "Yoshinobu", "lastName": "Yamamoto",
            "pos": ["P"], "team": {"abbr": "LAD"},
            "salary": 10600, "pts": "20.02", "rostership": 17.35,
            "lineup": {"slot": "SP", "isConfirmed": True},
        },
        {
            # Bench -- an empty lineup slot, must come through as a None
            # lineup_spot (RotoWire's own "not starting" marker, same as
            # a manual CSV upload's "BN").
            "firstName": "Bench", "lastName": "Guy",
            "pos": ["OF"], "team": {"abbr": "LAD"},
            "salary": 2000, "pts": "0.00", "rostership": 0.1,
            "lineup": {"slot": "", "isConfirmed": False},
        },
        {
            # No salary -- must be skipped, same rule draftkings.py's own client uses.
            "firstName": "No", "lastName": "Salary",
            "pos": ["OF"], "team": {"abbr": "LAD"},
            "salary": None, "pts": "5.00", "rostership": 1.0,
            "lineup": {"slot": "", "isConfirmed": False},
        },
    ]
    rw_rows = rotowire._parse_players(rw_players_fixture)
    check("_parse_players skips players with no salary",
          len(rw_rows) == 3, str(rw_rows))
    check("_parse_players produces the exact row shape projections.parse_rotowire_csv() does, "
          "so projections.store() accepts it with no changes downstream",
          rw_rows[0] == {
              "name": "Shohei Ohtani", "normalized_name": "shohei ohtani", "team": "LAD",
              "position": "1B/OF", "fpts": 11.94, "ownership_pct": 6.52, "salary": 6500,
              "lineup_spot": 1,
          }, str(rw_rows[0]))
    check("_parse_players reads a pitcher's 'SP' lineup slot as lineup_spot=None -- not a real "
          "batting-order spot, matching how a manual CSV's LINEUP='SP' already reads",
          rw_rows[1]["lineup_spot"] is None, str(rw_rows[1]))
    check("_parse_players reads an empty (bench) lineup slot as lineup_spot=None, same as a "
          "manual CSV upload's 'BN'",
          rw_rows[2]["lineup_spot"] is None, str(rw_rows[2]))

    print("\nFantasyLabs Vegas odds import (clients/fantasylabs.py) -- open + live lines")

    fl_event_fixture = {
        "EventId": 205310620,
        "EventDetails": {
            "Properties": {
                "HomeTeam": "Detroit Tigers", "VisitorTeam": "Tampa Bay Rays",
                "HomeTeamShort": "DET", "VisitorTeamShort": "TB",
                "EventDateTime": "2026-08-24T18:40:00",
                "HomeGameSpreadOpen": 1.50, "HomeGameSpreadCurrent": 1.50,
                "VisitorGameSpreadOpen": -1.50, "VisitorGameSpreadCurrent": -1.50,
                "HomeGameMoneylineOpen": 110, "HomeGameMoneylineCurrent": 117,
                "VisitorGameMoneylineOpen": -132, "VisitorGameMoneylineCurrent": -135,
                "HomeGameOUOpen": 7.50, "HomeGameOUCurrent": 7.50,
                "HomeVegasRunsOpen": 3.6, "HomeVegasRuns": 3.6,
                "VisitorVegasRunsOpen": 4.1, "VisitorVegasRuns": 4.1,
            },
        },
    }
    fl_row = fantasylabs._parse_event(fl_event_fixture)
    check("_parse_event reads team names, event id, and open/current spread/moneyline/total",
          fl_row == {
              "event_id": 205310620,
              "home_team": "Detroit Tigers", "away_team": "Tampa Bay Rays",
              "home_short": "DET", "away_short": "TB",
              "game_time_utc": "2026-08-24T18:40:00",
              "home_spread_open": 1.50, "home_spread_current": 1.50,
              "away_spread_open": -1.50, "away_spread_current": -1.50,
              "home_moneyline_open": 110, "home_moneyline_current": 117,
              "away_moneyline_open": -132, "away_moneyline_current": -135,
              "total_open": 7.50, "total_current": 7.50,
              "home_implied_runs_open": 3.6, "home_implied_runs_current": 3.6,
              "away_implied_runs_open": 4.1, "away_implied_runs_current": 4.1,
          }, str(fl_row))
    check("_parse_event returns None for a malformed row rather than crashing",
          fantasylabs._parse_event({}) is None)

    def fake_salary_load_in_slate(day):
        return [salary_row("Big Righty Bat", "NYY", 4200, 9.5, game_info="NYY@BOS 08/14/2026 07:10PM ET")]

    def fake_salary_load_out_of_slate(day):
        return [salary_row("Some Guy", "LAD", 4200, 9.5, game_info="LAD@SF 08/14/2026 10:10PM ET")]

    def fake_salary_load_empty(day):
        return []

    salaries.load = fake_salary_load_in_slate
    in_slate_slate = await mlb_slate.build_slate(DAY)
    check("build_slate flags in_slate=True when the DK CSV's Game Info covers this game",
          in_slate_slate["games"][0]["in_slate"] is True)

    salaries.load = fake_salary_load_out_of_slate
    out_of_slate_slate = await mlb_slate.build_slate(DAY)
    check("build_slate flags in_slate=False when the DK CSV covers a different game entirely",
          out_of_slate_slate["games"][0]["in_slate"] is False)

    salaries.load = fake_salary_load_empty
    no_upload_slate = await mlb_slate.build_slate(DAY)
    check("build_slate leaves in_slate=None (not False) when no salary CSV is loaded at all",
          no_upload_slate["games"][0]["in_slate"] is None)

    salaries.load = fake_salary_load  # restore the default fixture

    # Doubleheaders: a DK export identifies a game only by its matchup
    # ("BOS@NYY"), and the RotoWire-sourced pool carries no game time at
    # all, so both halves of a doubleheader collapse to one key. Matching
    # on the pair alone marked BOTH as in-slate -- real, and wrong: on
    # 2026-08-29 it flagged 14 games for a 12-game slate.
    def _dh_game(pk, away, home, when):
        return {
            "gamePk": pk, "gameDate": when,
            "teams": {"home": {"team": {"abbreviation": home}},
                      "away": {"team": {"abbreviation": away}}},
        }

    dh_games = [
        _dh_game(1, "BOS", "NYY", "2026-08-29T17:05:00Z"),   # DH game 1
        _dh_game(2, "LAD", "DET", "2026-08-29T17:10:00Z"),
        _dh_game(3, "MIA", "WSH", "2026-08-29T20:10:00Z"),
        _dh_game(4, "BOS", "NYY", "2026-08-29T23:15:00Z"),   # DH nightcap
        _dh_game(5, "TEX", "MIL", "2026-08-29T23:15:00Z"),   # not in the slate
    ]
    dh_pairs = {frozenset(("BOS", "NYY")), frozenset(("LAD", "DET")), frozenset(("MIA", "WSH"))}
    resolved = mlb_slate._resolve_slate_game_pks(dh_games, dh_pairs)
    check("a doubleheader contributes exactly ONE game to the slate, not both -- the real bug "
          "that put a phantom extra game in the contest generator's field",
          resolved == {1, 2, 3}, str(sorted(resolved)))
    check("the doubleheader half kept is the one inside the slate's own time window, not the "
          "nightcap that falls outside every unambiguous game",
          1 in resolved and 4 not in resolved, str(sorted(resolved)))
    check("a game whose matchup isn't in the DK export stays out regardless of doubleheaders",
          5 not in resolved, str(sorted(resolved)))

    # A late doubleheader whose SECOND game is the one in the window.
    late_games = [
        _dh_game(10, "AZ", "SF", "2026-08-29T16:05:00Z"),
        _dh_game(11, "AZ", "SF", "2026-08-29T22:05:00Z"),
        _dh_game(12, "KC", "CLE", "2026-08-29T22:10:00Z"),
        _dh_game(13, "SD", "TB", "2026-08-29T23:10:00Z"),
    ]
    late_pairs = {frozenset(("AZ", "SF")), frozenset(("KC", "CLE")), frozenset(("SD", "TB"))}
    late_resolved = mlb_slate._resolve_slate_game_pks(late_games, late_pairs)
    check("on a night slate the LATER half of a doubleheader is the one kept -- the window is "
          "read from the real slate, not assumed to be the earlier game",
          late_resolved == {11, 12, 13}, str(sorted(late_resolved)))

    # Degenerate inputs must not crash or silently drop the game.
    no_time = [
        {"gamePk": 20, "gameDate": None,
         "teams": {"home": {"team": {"abbreviation": "NYY"}},
                   "away": {"team": {"abbreviation": "BOS"}}}},
        {"gamePk": 21, "gameDate": None,
         "teams": {"home": {"team": {"abbreviation": "NYY"}},
                   "away": {"team": {"abbreviation": "BOS"}}}},
    ]
    no_time_resolved = mlb_slate._resolve_slate_game_pks(no_time, {frozenset(("BOS", "NYY"))})
    check("a doubleheader with no game times at all still resolves to exactly one game rather "
          "than crashing or returning both",
          len(no_time_resolved) == 1, str(no_time_resolved))
    check("_resolve_slate_game_pks returns an empty set when the export matches no real game",
          mlb_slate._resolve_slate_game_pks(dh_games, {frozenset(("COL", "ATL"))}) == set(), "")

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

    def opt_hitter(pid, name, team, pos, salary, fpts, own=None):
        return {
            "id": pid, "name": name,
            "salary": {"salary": salary, "position": pos, "avg_points": None, "value": None},
            "projection": {"fpts": fpts, "ownership_pct": own},
        }

    def opt_pitcher(pid, name, salary, fpts, own=None):
        return {
            "id": pid, "name": name,
            "salary": {"salary": salary, "position": "P", "avg_points": None, "value": None},
            "projection": {"fpts": fpts, "ownership_pct": own},
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
            },
            # A second, unrelated game supplying a filler 2nd pitcher --
            # OPT's own 8 hitters exactly fill the 8 hitter slots, but
            # OP2 (OPP's pitcher) opposes those same OPT hitters, so the
            # opposing-pitcher rule correctly rules OP2 out. Without this
            # filler, the fixture would have no legal way to fill both P
            # slots at all.
            {
                "home": {
                    "abbrev": "FIL", "hitters": [],
                    "probable_pitcher": opt_pitcher(9202, "FILLER", 4000, 6),
                    "scratches": [],
                },
                "away": {"abbrev": "FOE", "hitters": [], "probable_pitcher": None, "scratches": []},
            },
        ]
    }

    lineup = optimizer.generate_lineups(opt_slate)["lineups"][0]
    all_ids = [p["id"] for slot in lineup["slots"].values() for p in slot]
    slot_counts = {slot: len(players) for slot, players in lineup["slots"].items()}
    check("optimizer respects the salary cap",
          lineup["salary_used"] <= optimizer.SALARY_CAP, str(lineup["salary_used"]))
    check("optimizer fills every roster slot with the right counts",
          slot_counts == optimizer.SLOT_REQUIREMENTS, str(slot_counts))
    check("optimizer never rosters a scratched player, even a huge-projection one",
          9106 not in all_ids, str(all_ids))
    # OP2 (id 9201) is OPP's pitcher, facing OPT's hitters -- every one
    # of the 8 rostered hitters is on OPT, so using OP2 alongside them
    # would violate the opposing-pitcher rule. Only the filler pitcher
    # (9202) should ever fill the 2nd P slot here.
    check("optimizer never pairs a pitcher with a hitter from the team he's facing",
          9201 not in all_ids, str(all_ids))

    # Stack-shape behavior (manual/auto team assignment, partial shapes,
    # validation) gets its own dedicated section below with a deeper
    # fixture -- opt_slate's second team only has 1 hitter, not enough
    # depth to prove an *exact* group-size constraint on its own.

    try:
        optimizer.generate_lineups({"games": []})
        check("optimizer raises OptimizerError on an empty pool", False)
    except optimizer.OptimizerError:
        check("optimizer raises OptimizerError on an empty pool", True)

    print("\nLineup optimizer: in-house vs RotoWire projection_source")

    # A slate where RotoWire's fpts and inhouse_fpts deliberately
    # disagree about who's best at one hitter slot -- proves
    # build_player_pool() actually reads the requested source's numbers
    # rather than always falling back to RotoWire's.
    source_slate = {
        "games": [
            {
                "home": {
                    "abbrev": "SRC",
                    "hitters": [
                        {
                            "id": 9301, "name": "RotoWireFavorite",
                            "salary": {"salary": 4000, "position": "C", "avg_points": None, "value": None},
                            "projection": {"fpts": 15.0, "ownership_pct": 20.0, "inhouse_fpts": 3.0, "inhouse_ownership_pct": 5.0},
                        },
                        {
                            "id": 9302, "name": "InhouseFavorite",
                            "salary": {"salary": 4000, "position": "C", "avg_points": None, "value": None},
                            "projection": {"fpts": 3.0, "ownership_pct": 5.0, "inhouse_fpts": 15.0, "inhouse_ownership_pct": 20.0},
                        },
                    ],
                    "probable_pitcher": None,
                    "scratches": [],
                },
                "away": {"abbrev": "SRC2", "hitters": [], "probable_pitcher": None, "scratches": []},
            }
        ]
    }
    rotowire_pool = {p["id"]: p["projected_fpts"] for p in optimizer.build_player_pool(source_slate)}
    inhouse_pool = {
        p["id"]: p["projected_fpts"]
        for p in optimizer.build_player_pool(source_slate, projection_source="inhouse")
    }
    check("build_player_pool defaults to RotoWire's fpts",
          rotowire_pool == {9301: 15.0, 9302: 3.0}, str(rotowire_pool))
    check("build_player_pool(projection_source='inhouse') reads inhouse_fpts instead",
          inhouse_pool == {9301: 3.0, 9302: 15.0}, str(inhouse_pool))

    try:
        optimizer.build_player_pool(source_slate, projection_source="not_a_real_source")
        check("build_player_pool rejects an unknown projection_source", False)
    except optimizer.OptimizerError:
        check("build_player_pool rejects an unknown projection_source", True)

    # MIN_POOL_FPTS: a cheap mitigation (confirmed with the user) for
    # the entry-builder drafting a bench/unlisted player the at-bat
    # engine has no simulated data for -- a real bench player's own
    # projected FPTS is reliably near zero, so filtering the pool to
    # real contributors cuts that collision down without threading
    # at-bat-eligibility through generation itself.
    fpts_floor_slate = {
        "games": [
            {
                "home": {
                    "abbrev": "FLR",
                    "hitters": [
                        {
                            "id": 9501, "name": "RealContributor",
                            "salary": {"salary": 4000, "position": "1B", "avg_points": None, "value": None},
                            "projection": {"fpts": 3.01, "ownership_pct": 10.0},
                        },
                        {
                            "id": 9502, "name": "RightAtTheFloor",
                            "salary": {"salary": 3000, "position": "2B", "avg_points": None, "value": None},
                            "projection": {"fpts": 3.0, "ownership_pct": 5.0},
                        },
                        {
                            "id": 9503, "name": "BenchGuy",
                            "salary": {"salary": 2000, "position": "OF", "avg_points": None, "value": None},
                            "projection": {"fpts": 0.5, "ownership_pct": 1.0},
                        },
                    ],
                    "probable_pitcher": None,
                    "scratches": [],
                },
                "away": {"abbrev": "FLR2", "hitters": [], "probable_pitcher": None, "scratches": []},
            }
        ]
    }
    fpts_floor_pool = {p["id"] for p in optimizer.build_player_pool(fpts_floor_slate)}
    check("build_player_pool excludes a player projected below MIN_POOL_FPTS (a real proxy for "
          "'not actually going to play'), but keeps one projected right at or above it",
          fpts_floor_pool == {9501, 9502}, str(fpts_floor_pool))

    # Regression for a real bug found by backtesting the contest
    # simulator against real GPP results: RotoWire's export doesn't
    # cover every player on a slate, and a missing ownership_pct used
    # to fall straight to 0 -- floored to ~0% owned by contest.py's
    # sampler, making the simulated opponent field construction for
    # those players closer to random than realistic. Ownership now
    # falls back to the OTHER source when the requested one is missing
    # it -- FPTS has no such fallback (a missing FPTS still excludes
    # the player from the pool entirely, since there's no other signal
    # to optimize against).
    fallback_slate = {
        "games": [
            {
                "home": {
                    "abbrev": "FBK",
                    "hitters": [
                        {
                            "id": 9401, "name": "NoRotoWireOwnership",
                            "salary": {"salary": 4000, "position": "1B", "avg_points": None, "value": None},
                            # RotoWire has an fpts number for him (real, priced) but never
                            # exported an ownership% for him -- the exact real-world gap.
                            "projection": {"fpts": 8.0, "ownership_pct": None, "inhouse_fpts": 6.0, "inhouse_ownership_pct": 12.5},
                        },
                        {
                            "id": 9402, "name": "NeitherSourceHasOwnership",
                            "salary": {"salary": 3500, "position": "2B", "avg_points": None, "value": None},
                            "projection": {"fpts": 7.0, "ownership_pct": None, "inhouse_fpts": None},
                        },
                    ],
                    "probable_pitcher": None,
                    "scratches": [],
                },
                "away": {"abbrev": "FBK2", "hitters": [], "probable_pitcher": None, "scratches": []},
            }
        ]
    }
    fallback_pool = {p["id"]: p["ownership_pct"] for p in optimizer.build_player_pool(fallback_slate)}
    check("ownership_pct falls back to the other source's ownership when RotoWire's own is missing",
          fallback_pool[9401] == 12.5, str(fallback_pool))
    check("ownership_pct floors to 0 (not a crash) when neither source has it",
          fallback_pool[9402] == 0, str(fallback_pool))

    print("\nLineup optimizer: multi-lineup generation and exposure caps")

    # Real headroom at every slot type (3+ options per infield position,
    # 10 outfield-eligible, 6 pitchers) so a 50% cap over 4 lineups is
    # comfortably satisfiable regardless of which players earlier,
    # greedy solves happen to pick -- a tighter pool risks a slot type
    # running out even though a smarter global allocation would work,
    # which would make this test flaky rather than proving the feature.
    mul_hitters_home = [
        opt_hitter(9301, "MC1", "MUL1", "C", 3000, 8.0),
        opt_hitter(9302, "MC2", "MUL1", "C", 2900, 7.5),
        opt_hitter(9303, "MC3", "MUL1", "C", 2800, 7.2),
        opt_hitter(9304, "M1B1", "MUL1", "1B", 4000, 10.0),
        opt_hitter(9305, "M1B2", "MUL1", "1B", 3900, 9.6),
        opt_hitter(9306, "M1B3", "MUL1", "1B", 3800, 9.3),
        opt_hitter(9307, "M2B1", "MUL1", "2B", 3500, 9.0),
        opt_hitter(9308, "M2B2", "MUL1", "2B", 3400, 8.7),
        opt_hitter(9309, "M2B3", "MUL1", "2B", 3300, 8.4),
        opt_hitter(9310, "M3B1", "MUL1", "3B", 4500, 12.0),
        opt_hitter(9311, "M3B2", "MUL1", "3B", 4400, 11.5),
        opt_hitter(9312, "M3B3", "MUL1", "3B", 4300, 11.2),
        opt_hitter(9313, "MSS1", "MUL1", "SS", 3800, 9.5),
        opt_hitter(9314, "MSS2", "MUL1", "SS", 3700, 9.2),
        opt_hitter(9315, "MSS3", "MUL1", "SS", 3600, 8.9),
        opt_hitter(9316, "MOF1", "MUL1", "OF", 5000, 14.0),
        opt_hitter(9317, "MOF2", "MUL1", "OF", 4800, 13.5),
        opt_hitter(9318, "MOF3", "MUL1", "OF", 4600, 13.0),
        opt_hitter(9319, "MOF4", "MUL1", "OF", 4400, 12.5),
        opt_hitter(9320, "MOF5", "MUL1", "OF", 4200, 12.0),
    ]
    mul_hitters_away = [
        # Real positional spread, not just outfield -- a team with only
        # OF-eligible hitters could never fill a 4-stack group on its
        # own (there are only 3 OF slots on the whole roster).
        opt_hitter(9321, "MOF6", "MUL2", "OF", 4000, 11.5),
        opt_hitter(9322, "MOF7", "MUL2", "OF", 3900, 11.2),
        opt_hitter(9323, "MOF8", "MUL2", "OF", 3800, 10.9),
        opt_hitter(9324, "MC4", "MUL2", "C", 3200, 8.0),
        opt_hitter(9325, "M1B4", "MUL2", "1B", 3600, 9.5),
        opt_hitter(9326, "M2B4", "MUL2", "2B", 3300, 8.6),
        opt_hitter(9327, "M3B4", "MUL2", "3B", 3900, 10.8),
        opt_hitter(9328, "MSS4", "MUL2", "SS", 3400, 8.8),
    ]
    mul_hitters_third_team = [
        # A 3rd team with real hitter depth -- MUL1/MUL2 alone can never
        # support a 3-group stack shape (4-2-2, 3-3-2 both need 3
        # distinct teams), and with only 2 teams to split any weighting
        # scheme between, the MAX_HITTERS_PER_TEAM=5 cap forces both the
        # points-weighted and ownership-weighted generators into the
        # same structural split, muting the difference between them.
        opt_hitter(9329, "MC5", "MUL3", "C", 2600, 6.8),
        opt_hitter(9330, "M1B5", "MUL3", "1B", 3300, 8.4),
        opt_hitter(9331, "M2B5", "MUL3", "2B", 3000, 7.6),
        opt_hitter(9332, "M3B5", "MUL3", "3B", 3500, 9.2),
        opt_hitter(9333, "MSS5", "MUL3", "SS", 3100, 7.9),
        opt_hitter(9334, "MOF9", "MUL3", "OF", 3400, 8.7),
        opt_hitter(9335, "MOF10", "MUL3", "OF", 3200, 8.1),
        opt_hitter(9336, "MOF11", "MUL3", "OF", 3000, 7.5),
    ]
    mul_slate = {
        "games": [
            {
                "game_pk": 88001,
                "home": {"abbrev": "MUL1", "hitters": mul_hitters_home,
                         "probable_pitcher": opt_pitcher(9400, "MP1", 9000, 18.0), "scratches": []},
                "away": {"abbrev": "MUL2", "hitters": mul_hitters_away,
                         "probable_pitcher": opt_pitcher(9401, "MP2", 8800, 17.5), "scratches": []},
            },
            {
                "game_pk": 88002,
                "home": {"abbrev": "MUL3", "hitters": mul_hitters_third_team,
                         "probable_pitcher": opt_pitcher(9402, "MP3", 8600, 17.0), "scratches": []},
                "away": {"abbrev": "MUL4", "hitters": [],
                         "probable_pitcher": opt_pitcher(9403, "MP4", 8400, 16.5), "scratches": []},
            },
            {
                "game_pk": 88003,
                "home": {"abbrev": "MUL5", "hitters": [],
                         "probable_pitcher": opt_pitcher(9404, "MP5", 8200, 16.0), "scratches": []},
                "away": {"abbrev": "MUL6", "hitters": [],
                         "probable_pitcher": opt_pitcher(9405, "MP6", 8000, 15.5), "scratches": []},
            },
        ]
    }

    multi = optimizer.generate_lineups(mul_slate, num_lineups=4, max_exposure_pct=50)
    check("multi-lineup: requested count fully satisfied when the pool supports it",
          len(multi["lineups"]) == 4, str(len(multi["lineups"])))
    id_sets = [
        frozenset(p["id"] for slot in lu["slots"].values() for p in slot)
        for lu in multi["lineups"]
    ]
    check("multi-lineup: every generated lineup is distinct (no-good cuts hold)",
          len(id_sets) == len(set(id_sets)), str(id_sets))
    max_count = max((e["count"] for e in multi["exposure"]), default=0)
    check("multi-lineup: 50% exposure cap over 4 lineups holds (no one appears more than 2x)",
          max_count <= 2, str(multi["exposure"]))
    check("multi-lineup: exposure summary is sorted by count descending",
          multi["exposure"] == sorted(multi["exposure"], key=lambda e: -e["count"]),
          str(multi["exposure"]))

    thin_result = optimizer.generate_lineups(opt_slate, num_lineups=10)
    check("multi-lineup: gracefully returns fewer than requested once the pool runs dry",
          0 < len(thin_result["lineups"]) < 10, str(len(thin_result["lineups"])))

    try:
        optimizer.generate_lineups(opt_slate, num_lineups=0)
        check("optimizer rejects num_lineups < 1", False)
    except optimizer.OptimizerError:
        check("optimizer rejects num_lineups < 1", True)

    try:
        optimizer.generate_lineups(opt_slate, num_lineups=optimizer.MAX_LINEUPS + 1)
        check("optimizer rejects num_lineups above MAX_LINEUPS", False)
    except optimizer.OptimizerError:
        check("optimizer rejects num_lineups above MAX_LINEUPS", True)

    print("\nLate swap: re-optimize only the still-open slots")

    # A "locked" game (deep past game_time_utc) supplying a pitcher +
    # 5 single-position hitters, and TWO "swappable" games (deep future
    # game_time_utc) each supplying one candidate pitcher (kept apart so
    # neither opposes the other's hitters) plus 6 OF-eligible hitters --
    # 3 deliberately worse (the ones "already picked") and 3 clearly
    # better, so a real swap has an obvious right answer to check
    # against.
    late_swap_slate = {
        "games": [
            {
                "game_pk": 77701, "game_time_utc": "2020-01-01T00:00:00Z",  # deep past -- locked
                "home": {
                    "abbrev": "LOCKED",
                    "hitters": [
                        opt_hitter(97010, "LC1", "LOCKED", "C", 3000, 8.0),
                        opt_hitter(97011, "L1B1", "LOCKED", "1B", 4000, 10.0),
                        opt_hitter(97012, "L2B1", "LOCKED", "2B", 3500, 9.0),
                        opt_hitter(97013, "L3B1", "LOCKED", "3B", 4500, 12.0),
                        opt_hitter(97014, "LSS1", "LOCKED", "SS", 3800, 9.5),
                    ],
                    "probable_pitcher": opt_pitcher(97001, "LP1", 8000, 15.0),
                    "scratches": [],
                },
                "away": {"abbrev": "LOCKEDOPP", "hitters": [], "probable_pitcher": None, "scratches": []},
            },
            {
                "game_pk": 77702, "game_time_utc": "2099-01-01T00:00:00Z",  # deep future -- swappable
                "home": {
                    "abbrev": "SWAP",
                    "hitters": [
                        opt_hitter(97020, "SOF1", "SWAP", "OF", 3000, 6.0),
                        opt_hitter(97021, "SOF2", "SWAP", "OF", 3000, 6.0),
                        opt_hitter(97022, "SOF3", "SWAP", "OF", 3000, 6.0),
                        opt_hitter(97023, "SOF4", "SWAP", "OF", 3200, 15.0),
                        opt_hitter(97024, "SOF5", "SWAP", "OF", 3200, 15.0),
                        opt_hitter(97025, "SOF6", "SWAP", "OF", 3200, 15.0),
                    ],
                    "probable_pitcher": opt_pitcher(97002, "SP1", 6000, 10.0),
                    "scratches": [],
                },
                "away": {"abbrev": "SWAPOPP", "hitters": [], "probable_pitcher": None, "scratches": []},
            },
            {
                "game_pk": 77703, "game_time_utc": "2099-01-01T00:00:00Z",  # deep future -- swappable
                "home": {
                    "abbrev": "PGOOD", "hitters": [],
                    "probable_pitcher": opt_pitcher(97003, "SP2", 6500, 20.0),
                    "scratches": [],
                },
                "away": {"abbrev": "PBAD", "hitters": [], "probable_pitcher": None, "scratches": []},
            },
        ]
    }

    def _pick(pid, game_pk):
        return {"player_id": pid, "game_pk": game_pk}

    original_picks = [
        _pick(97001, 77701), _pick(97002, 77702),  # P, P (LP1 locked, SP1 the worse swappable option)
        _pick(97010, 77701),  # C
        _pick(97011, 77701),  # 1B
        _pick(97012, 77701),  # 2B
        _pick(97013, 77701),  # 3B
        _pick(97014, 77701),  # SS
        _pick(97020, 77702), _pick(97021, 77702), _pick(97022, 77702),  # OF, OF, OF (the 3 worse options)
    ]

    swap_result = optimizer.late_swap(late_swap_slate, original_picks)
    check("late_swap reports the batch as changed when a genuinely better swap exists",
          swap_result["changed"] is True, str(swap_result.get("changed")))
    check("late_swap identifies exactly the 6 locked (already-started) players",
          sorted(swap_result["locked_player_ids"]) == [97001, 97010, 97011, 97012, 97013, 97014],
          str(sorted(swap_result["locked_player_ids"])))
    check("every locked player is still in the new lineup, untouched",
          {97001, 97010, 97011, 97012, 97013, 97014} <=
          {p["id"] for slot in swap_result["lineup"]["slots"].values() for p in slot},
          str(swap_result["lineup"]["slots"]))
    check("the worse swappable pitcher (SP1) got swapped out for the better one (SP2)",
          97002 in swap_result["removed_player_ids"] and 97003 in swap_result["added_player_ids"],
          str((swap_result["removed_player_ids"], swap_result["added_player_ids"])))
    check("the 3 worse swappable OF picks got swapped for the 3 better ones",
          {97020, 97021, 97022} <= set(swap_result["removed_player_ids"])
          and {97023, 97024, 97025} <= set(swap_result["added_player_ids"]),
          str((swap_result["removed_player_ids"], swap_result["added_player_ids"])))
    check("late_swap never touches a locked player -- removed/added only ever come from the swappable slots",
          set(swap_result["removed_player_ids"]) <= {97002, 97020, 97021, 97022}
          and not set(swap_result["removed_player_ids"]) & {97001, 97010, 97011, 97012, 97013, 97014},
          str(swap_result["removed_player_ids"]))

    # Every player's game already started -- nothing left to swap.
    all_locked_picks = [
        _pick(97001, 77701), _pick(97002, 77702),
        _pick(97010, 77701), _pick(97011, 77701), _pick(97012, 77701),
        _pick(97013, 77701), _pick(97014, 77701),
        _pick(97020, 77702), _pick(97021, 77702), _pick(97022, 77702),
    ]
    for pick in all_locked_picks:
        pick["game_pk"] = 77701  # pretend every pick's game is the locked one
    all_locked_result = optimizer.late_swap(late_swap_slate, all_locked_picks)
    check("late_swap reports nothing changed when every game has already started",
          all_locked_result["changed"] is False, str(all_locked_result))

    # A pick whose game_pk doesn't exist anywhere in the current slate
    # (postponement, bad id, ...) is conservatively treated as locked,
    # not silently swapped.
    unknown_game_picks = list(original_picks)
    unknown_game_picks[0] = _pick(97001, 999999)  # LP1, but with a bogus game_pk
    unknown_result = optimizer.late_swap(late_swap_slate, unknown_game_picks)
    check("a pick with an unresolvable game_pk is treated as locked, not silently swapped",
          97001 in unknown_result["locked_player_ids"] and 97001 in unknown_result["unresolved_player_ids"],
          str((unknown_result["locked_player_ids"], unknown_result["unresolved_player_ids"])))

    try:
        optimizer.late_swap(late_swap_slate, original_picks[:9])
        check("late_swap rejects a picks list that isn't exactly ROSTER_SIZE entries", False)
    except optimizer.OptimizerError:
        check("late_swap rejects a picks list that isn't exactly ROSTER_SIZE entries", True)

    print("\nLineup optimizer: named stack shapes")

    def team_hitter_counts(lu):
        counts = {}
        for slot, players in lu["slots"].items():
            if slot == "P":
                continue
            for p in players:
                counts[p["team"]] = counts.get(p["team"], 0) + 1
        return counts

    manual_53 = optimizer.generate_lineups(
        mul_slate, stack_groups=[5, 3], stack_teams=["MUL1", "MUL2"]
    )["lineups"][0]
    counts_53 = team_hitter_counts(manual_53)
    check("manual 5-3 puts exactly 5 on the named team and 3 on the other",
          counts_53.get("MUL1") == 5 and counts_53.get("MUL2") == 3, str(counts_53))

    auto_44 = optimizer.generate_lineups(mul_slate, stack_groups=[4, 4])["lineups"][0]
    counts_44 = team_hitter_counts(auto_44)
    fours = [c for c in counts_44.values() if c == 4]
    check("auto 4-4 produces exactly two teams with exactly 4 hitters each",
          len(fours) == 2 and sum(counts_44.values()) == 8, str(counts_44))

    partial_42 = optimizer.generate_lineups(mul_slate, stack_groups=[4, 2])["lineups"][0]
    counts_42 = team_hitter_counts(partial_42)
    check("partial 4-2 still totals 8 hitters, with the remaining 2 unconstrained",
          sum(counts_42.values()) == 8, str(counts_42))
    # A lower bound, not an exact count: the 2 leftover hitters are free
    # to pad one of the named stacks further rather than being forced
    # onto some uninvolved third team, so >= is the right check here,
    # not equality (confirmed against real DK stacking conventions).
    sorted_counts = sorted(counts_42.values(), reverse=True)
    check("partial 4-2 has at least a 4-stack and, separately, at least a 2-stack",
          len(sorted_counts) >= 2 and sorted_counts[0] >= 4 and sorted_counts[1] >= 2,
          str(counts_42))

    unconstrained = optimizer.generate_lineups(mul_slate)["lineups"][0]
    check("no stack_groups reproduces the unconstrained behavior (any team split)",
          sum(team_hitter_counts(unconstrained).values()) == 8)

    try:
        optimizer.generate_lineups(mul_slate, stack_groups=[5, 5])
        check("stack shape rejects group sizes summing past 8 hitter slots", False)
    except optimizer.OptimizerError:
        check("stack shape rejects group sizes summing past 8 hitter slots", True)

    try:
        optimizer.generate_lineups(mul_slate, stack_groups=[4, 4], stack_teams=["MUL1"])
        check("stack shape rejects a stack_teams length mismatch", False)
    except optimizer.OptimizerError:
        check("stack shape rejects a stack_teams length mismatch", True)

    try:
        optimizer.generate_lineups(mul_slate, stack_groups=[4, 4], stack_teams=["ZZZ", None])
        check("stack shape rejects an unknown team name", False)
    except optimizer.OptimizerError:
        check("stack shape rejects an unknown team name", True)

    try:
        optimizer.generate_lineups(mul_slate, stack_groups=[4, 4], stack_teams=["MUL1", "MUL1"])
        check("stack shape rejects the same team assigned to two groups", False)
    except optimizer.OptimizerError:
        check("stack shape rejects the same team assigned to two groups", True)

    print("\nLineup optimizer: locked and excluded players")

    LOCK_ID = 9301  # MC1 -- a cheap catcher that wouldn't normally get picked
    EXCLUDE_ID = 9316  # MOF1 -- the single best-value outfielder in the fixture

    locked_result = optimizer.generate_lineups(
        mul_slate, num_lineups=3, max_exposure_pct=20, locked_ids=[LOCK_ID]
    )
    check("a locked player appears in every generated lineup, exempt from the exposure cap",
          all(
              LOCK_ID in {p["id"] for slot in lu["slots"].values() for p in slot}
              for lu in locked_result["lineups"]
          ),
          str(len(locked_result["lineups"])))

    excluded_result = optimizer.generate_lineups(mul_slate, excluded_ids=[EXCLUDE_ID])
    excluded_ids_in_lineup = {
        p["id"] for slot in excluded_result["lineups"][0]["slots"].values() for p in slot
    }
    check("an excluded player never appears, even the best-value one in the pool",
          EXCLUDE_ID not in excluded_ids_in_lineup, str(excluded_ids_in_lineup))

    try:
        optimizer.generate_lineups(mul_slate, locked_ids=[LOCK_ID], excluded_ids=[LOCK_ID])
        check("optimizer rejects locking and excluding the same player", False)
    except optimizer.OptimizerError:
        check("optimizer rejects locking and excluding the same player", True)

    try:
        optimizer.generate_lineups(mul_slate, locked_ids=[999999])
        check("optimizer rejects a locked id that isn't in the pool", False)
    except optimizer.OptimizerError:
        check("optimizer rejects a locked id that isn't in the pool", True)

    try:
        too_many = list(range(9301, 9301 + optimizer.ROSTER_SIZE + 1))
        optimizer.generate_lineups(mul_slate, locked_ids=too_many)
        check("optimizer rejects locking more players than roster slots exist", False)
    except optimizer.OptimizerError:
        check("optimizer rejects locking more players than roster slots exist", True)

    print("\nLineup optimizer: per-position and per-team exposure caps")

    slot_capped = optimizer.generate_lineups(
        mul_slate, num_lineups=6, exposure_by_slot={"OF": 34}
    )
    of_counts: dict[int, int] = {}
    c_counts: dict[int, int] = {}
    for lu in slot_capped["lineups"]:
        for p in lu["slots"]["OF"]:
            of_counts[p["id"]] = of_counts.get(p["id"], 0) + 1
        for p in lu["slots"]["C"]:
            c_counts[p["id"]] = c_counts.get(p["id"], 0) + 1
    check("a 34% OF exposure cap over 6 lineups holds (no OF appears more than 2x)",
          max(of_counts.values(), default=0) <= 2, str(of_counts))
    check("an uncapped slot (C) is free to exceed what the OF cap would have allowed",
          max(c_counts.values(), default=0) > 2, str(c_counts))

    team_capped = optimizer.generate_lineups(
        mul_slate, num_lineups=6, stack_groups=[5, 3], team_exposure_cap={"MUL1": 34}
    )
    mul1_stack_count = next(
        (e["count"] for e in team_capped["team_exposure"] if e["team"] == "MUL1"), 0
    )
    check("a 34% team stack-exposure cap over 6 lineups holds (MUL1 stacked no more than 2x)",
          mul1_stack_count <= 2, str(team_capped["team_exposure"]))

    try:
        optimizer.generate_lineups(mul_slate, team_exposure_cap={"MUL1": 50})
        check("team_exposure_cap without stack_groups is rejected", False)
    except optimizer.OptimizerError:
        check("team_exposure_cap without stack_groups is rejected", True)

    try:
        optimizer.generate_lineups(
            mul_slate, stack_groups=[5, 3], team_exposure_cap={"ZZZ": 50}
        )
        check("team_exposure_cap rejects an unknown team", False)
    except optimizer.OptimizerError:
        check("team_exposure_cap rejects an unknown team", True)

    try:
        optimizer.generate_lineups(mul_slate, exposure_by_slot={"DH": 50})
        check("exposure_by_slot rejects an unknown roster slot", False)
    except optimizer.OptimizerError:
        check("exposure_by_slot rejects an unknown roster slot", True)

    print("\nLineup optimizer: salary floor, uniqueness, and team-count bounds")

    # A pool where every slot has a cheap, high-fpts "value" option and an
    # expensive, lower-fpts "chalk-avoidant" alternative -- unconstrained,
    # the optimizer always prefers value (higher fpts AND cheaper), so
    # salary_used naturally lands well under the cap. This is the only
    # way to prove min_salary actually forces spending up rather than
    # just trivially passing because the pool already spends to the cap
    # (which mul_slate's fpts-scales-with-salary shape always does).
    def vp(pid, name, pos, salary, fpts):
        return opt_hitter(pid, name, "VAL", pos, salary, fpts) if pos != "P" else opt_pitcher(pid, name, salary, fpts)

    value_slate = {
        "games": [
            {
                "home": {
                    "abbrev": "VAL",
                    "hitters": [
                        vp(9504, "CV", "C", 3000, 10), vp(9505, "CX", "C", 5000, 6),
                        vp(9506, "1BV", "1B", 3000, 10), vp(9507, "1BX", "1B", 5000, 6),
                        vp(9508, "2BV", "2B", 3000, 10), vp(9509, "2BX", "2B", 5000, 6),
                        vp(9510, "3BV", "3B", 3000, 10), vp(9511, "3BX", "3B", 5000, 6),
                        vp(9512, "SSV", "SS", 3000, 10), vp(9513, "SSX", "SS", 5000, 6),
                        vp(9514, "OFV1", "OF", 3000, 10), vp(9515, "OFV2", "OF", 3000, 10),
                        vp(9516, "OFV3", "OF", 3000, 10), vp(9517, "OFX1", "OF", 5000, 6),
                        vp(9518, "OFX2", "OF", 5000, 6), vp(9519, "OFX3", "OF", 5000, 6),
                    ],
                    "probable_pitcher": vp(9500, "PV1", "P", 3000, 20),
                    "scratches": [],
                },
                "away": {
                    "abbrev": "VOP",
                    "hitters": [],
                    "probable_pitcher": vp(9502, "PV2", "P", 3000, 19),
                    "scratches": [],
                },
            },
            # Filler 2nd game so the solver has a 2nd pitcher option that
            # doesn't oppose VAL's hitters -- see opt_slate's matching
            # comment above for why PV2 alone would make this infeasible.
            {
                "home": {
                    "abbrev": "VFIL", "hitters": [],
                    "probable_pitcher": vp(9503, "PFILLER", "P", 3000, 5),
                    "scratches": [],
                },
                "away": {"abbrev": "VFOE", "hitters": [], "probable_pitcher": None, "scratches": []},
            },
        ]
    }

    value_baseline = optimizer.generate_lineups(value_slate)["lineups"][0]
    check("salary floor baseline: unconstrained solve prefers the cheap, higher-fpts options",
          value_baseline["salary_used"] < 35000, str(value_baseline["salary_used"]))

    floored = optimizer.generate_lineups(value_slate, min_salary=40000)["lineups"][0]
    check("min_salary forces total spend up to the floor even at a fpts cost",
          floored["salary_used"] >= 40000, str(floored["salary_used"]))

    try:
        optimizer.generate_lineups(mul_slate, min_salary=optimizer.SALARY_CAP + 1)
        check("optimizer rejects a min_salary above the salary cap", False)
    except optimizer.OptimizerError:
        check("optimizer rejects a min_salary above the salary cap", True)

    unique_set = optimizer.generate_lineups(
        mul_slate, num_lineups=3, min_unique_players=5, max_exposure_pct=100
    )
    unique_id_sets = [
        frozenset(p["id"] for slot in lu["slots"].values() for p in slot)
        for lu in unique_set["lineups"]
    ]
    min_diff = min(
        len(unique_id_sets[i] - unique_id_sets[j])
        for i in range(len(unique_id_sets))
        for j in range(i + 1, len(unique_id_sets))
    )
    check("min_unique_players=5 forces every pair of lineups to differ by at least 5 players",
          min_diff >= 5, str(min_diff))

    # min_unique_players=0 allows exact duplicates -- a real GPP move
    # (entering a signature build multiple times). No exposure cap here
    # means every solve reproduces the same true optimum, so all 4
    # should come back identical, each reporting duplicate_count == 4.
    dup_allowed = optimizer.generate_lineups(mul_slate, num_lineups=4, min_unique_players=0)["lineups"]
    dup_signatures = [
        frozenset(p["id"] for slot in lu["slots"].values() for p in slot) for lu in dup_allowed
    ]
    check("min_unique_players=0 allows the same optimal lineup to repeat",
          len(set(dup_signatures)) == 1, str(len(set(dup_signatures))))
    check("duplicate_count reports how many identical copies are in the set",
          all(lu["duplicate_count"] == 4 for lu in dup_allowed),
          str([lu["duplicate_count"] for lu in dup_allowed]))

    default_dup_counts = optimizer.generate_lineups(mul_slate, num_lineups=4, max_exposure_pct=50)["lineups"]
    check("duplicate_count is 1 under the default (no exact repeats allowed)",
          all(lu["duplicate_count"] == 1 for lu in default_dup_counts),
          str([lu["duplicate_count"] for lu in default_dup_counts]))

    try:
        optimizer.generate_lineups(mul_slate, min_unique_players=-1)
        check("optimizer rejects a negative min_unique_players", False)
    except optimizer.OptimizerError:
        check("optimizer rejects a negative min_unique_players", True)

    try:
        optimizer.generate_lineups(mul_slate, min_unique_players=optimizer.ROSTER_SIZE + 1)
        check("optimizer rejects min_unique_players above ROSTER_SIZE", False)
    except optimizer.OptimizerError:
        check("optimizer rejects min_unique_players above ROSTER_SIZE", True)

    def lineup_teams(lu):
        return {p["team"] for slot in lu["slots"].values() for p in slot}

    # MP1 and MP2 (MUL1's and MUL2's pitchers, the two highest-fpts
    # options) oppose each other's team -- the opposing-pitcher rule
    # means picking both would ban both teams' hitters at once, so the
    # unconstrained solve naturally spreads across more teams than it
    # would have before that rule existed.
    default_teams = optimizer.generate_lineups(mul_slate)["lineups"][0]
    check("without a team-count bound, the unconstrained solve naturally spans more than 2 teams",
          len(lineup_teams(default_teams)) > 2, str(lineup_teams(default_teams)))

    min3 = optimizer.generate_lineups(mul_slate, min_teams_per_lineup=3)["lineups"][0]
    check("min_teams_per_lineup=3 forces a third team's pitcher into the lineup",
          len(lineup_teams(min3)) >= 3, str(lineup_teams(min3)))

    try:
        optimizer.generate_lineups(mul_slate, max_teams_per_lineup=1)
        check("max_teams_per_lineup=1 is infeasible with hitters split across 2 teams", False)
    except optimizer.OptimizerError:
        check("max_teams_per_lineup=1 is infeasible with hitters split across 2 teams", True)

    try:
        optimizer.generate_lineups(
            mul_slate, min_teams_per_lineup=5, max_teams_per_lineup=3
        )
        check("optimizer rejects min_teams_per_lineup above max_teams_per_lineup", False)
    except optimizer.OptimizerError:
        check("optimizer rejects min_teams_per_lineup above max_teams_per_lineup", True)

    try:
        optimizer.generate_lineups(mul_slate, max_teams_per_lineup=optimizer.ROSTER_SIZE + 1)
        check("optimizer rejects a team-count bound above ROSTER_SIZE", False)
    except optimizer.OptimizerError:
        check("optimizer rejects a team-count bound above ROSTER_SIZE", True)

    print("\nLineup optimizer: one-off slot restrictions on partial stacks")

    # A dedicated fixture, not mul_slate: OA and OB get the "4-2" stack,
    # and a third team OC (never named in the stack) has hitters that
    # are a genuinely better value than anything on OA/OB at the same
    # positions -- so an unconstrained solve picks OC for the leftover
    # one-off slots. That's what proves a one-off restriction actually
    # bites, rather than trivially passing because there was never a
    # better outside option to begin with (mul_slate only has 2 hitting
    # teams, so it can't demonstrate this).
    oa_hitters = [
        opt_hitter(9700, "OAC", "OA", "C", 3200, 8.5),
        opt_hitter(9701, "OA1B", "OA", "1B", 4000, 10.0),
        opt_hitter(9702, "OA2B", "OA", "2B", 3500, 9.0),
        opt_hitter(9703, "OA3B", "OA", "3B", 4200, 10.5),
        opt_hitter(9704, "OASS", "OA", "SS", 3700, 9.3),
        opt_hitter(9705, "OAOF1", "OA", "OF", 4500, 12.0),
        opt_hitter(9706, "OAOF2", "OA", "OF", 4300, 11.5),
        opt_hitter(9707, "OAOF3", "OA", "OF", 4100, 11.0),
    ]
    ob_hitters = [
        opt_hitter(9710, "OBC", "OB", "C", 3000, 7.5),
        opt_hitter(9711, "OB1B", "OB", "1B", 3600, 9.0),
        opt_hitter(9712, "OB2B", "OB", "2B", 3200, 8.0),
        opt_hitter(9713, "OB3B", "OB", "3B", 3800, 9.5),
        opt_hitter(9714, "OBSS", "OB", "SS", 3300, 8.2),
        opt_hitter(9715, "OBOF1", "OB", "OF", 4000, 10.5),
        opt_hitter(9716, "OBOF2", "OB", "OF", 3900, 10.2),
        opt_hitter(9717, "OBOF3", "OB", "OF", 3700, 9.8),
    ]
    oc_hitters = [
        opt_hitter(9720, "OCC", "OC", "C", 2600, 12.0),
        opt_hitter(9721, "OC2B", "OC", "2B", 2600, 12.0),
    ]
    oneoff_slate = {
        "games": [
            {
                "home": {"abbrev": "OA", "hitters": oa_hitters,
                         "probable_pitcher": opt_pitcher(9750, "OAP", 9000, 18.0), "scratches": []},
                "away": {"abbrev": "OB", "hitters": ob_hitters,
                         "probable_pitcher": opt_pitcher(9751, "OBP", 8800, 17.5), "scratches": []},
            },
            {
                "home": {"abbrev": "OC", "hitters": oc_hitters,
                         "probable_pitcher": opt_pitcher(9752, "OCP", 8000, 15.0), "scratches": []},
                "away": {"abbrev": "OD", "hitters": [],
                         "probable_pitcher": opt_pitcher(9753, "ODP", 7800, 14.5), "scratches": []},
            },
            # A 3rd game supplying a 2nd pitcher option that opposes
            # neither OA, OB, nor OC -- without it, the only two
            # pitchers that don't oppose the forced OA/OB stack are
            # OCP and ODP, and ODP opposes OC, which would wrongly rule
            # OC's hitters out of every one-off scenario below (not
            # because of the salary/group restriction being tested, but
            # as an unrelated side effect of there being no other
            # pitcher choice).
            {
                "home": {"abbrev": "OE", "hitters": [],
                         "probable_pitcher": opt_pitcher(9754, "OEP", 7600, 14.0), "scratches": []},
                "away": {"abbrev": "OF", "hitters": [], "probable_pitcher": None, "scratches": []},
            },
        ]
    }

    def hitter_ids(lu):
        return {p["id"] for slot, players in lu["slots"].items() if slot != "P" for p in players}

    oc_ids = {9720, 9721}

    oneoff_baseline = optimizer.generate_lineups(
        oneoff_slate, stack_groups=[4, 2], stack_teams=["OA", "OB"]
    )["lineups"][0]
    check("one-off baseline: an unconstrained partial stack picks the better-value "
          "unstacked team for the leftover slots",
          bool(oc_ids & hitter_ids(oneoff_baseline)), str(hitter_ids(oneoff_baseline)))

    range_restricted = optimizer.generate_lineups(
        oneoff_slate, stack_groups=[4, 2], stack_teams=["OA", "OB"],
        one_off_min_salary=3000,
    )["lineups"][0]
    check("one_off_min_salary excludes the unstacked team's below-floor hitters "
          "from the leftover slots",
          not (oc_ids & hitter_ids(range_restricted)), str(hitter_ids(range_restricted)))

    allowed = {p["id"] for p in oa_hitters + ob_hitters}
    group_restricted = optimizer.generate_lineups(
        oneoff_slate, stack_groups=[4, 2], stack_teams=["OA", "OB"],
        one_off_group_ids=list(allowed),
    )["lineups"][0]
    check("one_off_group_ids restricts the leftover slots to the named whitelist",
          not (oc_ids & hitter_ids(group_restricted)), str(hitter_ids(group_restricted)))

    group_allow_oc = optimizer.generate_lineups(
        oneoff_slate, stack_groups=[4, 2], stack_teams=["OA", "OB"],
        one_off_group_ids=[9720, 9721],
    )["lineups"][0]
    check("one_off_group_ids naming the unstacked team's own players allows them through",
          bool(oc_ids & hitter_ids(group_allow_oc)), str(hitter_ids(group_allow_oc)))

    try:
        optimizer.generate_lineups(oneoff_slate, stack_groups=[4, 4], one_off_min_salary=3000)
        check("one-off restriction rejects a full stack shape (no leftover slots)", False)
    except optimizer.OptimizerError:
        check("one-off restriction rejects a full stack shape (no leftover slots)", True)

    try:
        optimizer.generate_lineups(oneoff_slate, one_off_min_salary=3000)
        check("one-off restriction rejects being used without stack_groups", False)
    except optimizer.OptimizerError:
        check("one-off restriction rejects being used without stack_groups", True)

    try:
        optimizer.generate_lineups(
            oneoff_slate, stack_groups=[4, 2], stack_teams=["OA", "OB"],
            one_off_group_ids=[9720], one_off_min_salary=3000,
        )
        check("one-off restriction rejects combining a group whitelist and a salary range", False)
    except optimizer.OptimizerError:
        check("one-off restriction rejects combining a group whitelist and a salary range", True)

    try:
        optimizer.generate_lineups(
            oneoff_slate, stack_groups=[4, 2], stack_teams=["OA", "OB"], one_off_group_ids=[]
        )
        check("one-off restriction rejects an empty group whitelist", False)
    except optimizer.OptimizerError:
        check("one-off restriction rejects an empty group whitelist", True)

    try:
        optimizer.generate_lineups(
            oneoff_slate, stack_groups=[4, 2], stack_teams=["OA", "OB"],
            one_off_min_salary=4000, one_off_max_salary=3000,
        )
        check("one-off restriction rejects one_off_min_salary above one_off_max_salary", False)
    except optimizer.OptimizerError:
        check("one-off restriction rejects one_off_min_salary above one_off_max_salary", True)

    print("\nLineup optimizer: default one-off slot quality preference")

    # A dedicated fixture proving the *default* preference (no explicit
    # one_off_* override) actually forces a real tradeoff, not just a
    # restatement of what fpts-maximization would already do alone.
    # QA/QB are manually stacked at 3 each (stack_groups=[3, 3]), using
    # up all their own hitter depth exactly -- they have zero spare
    # capacity, so the 2 leftover OF slots MUST come from QMISC. QMISC
    # offers a genuine "junk" pair (cheap, low fpts, well under 80% of
    # the OF ceiling both ways) and a "premium" pair (at the ceiling on
    # both). QA also offers a very expensive, very high-fpts catcher
    # upgrade that's only affordable if the OF slots go cheap (junk) --
    # so the TRUE unconstrained fpts-max optimum genuinely prefers the
    # junk OF pair to fund the catcher upgrade, proving this isn't a
    # scenario where fpts-maximization alone would have avoided junk
    # anyway.
    q_pitcher_a = opt_pitcher(9900, "QAP", 3000, 10)
    q_pitcher_b = opt_pitcher(9901, "QBP", 3000, 10)
    qa_hitters = [
        opt_hitter(9910, "QAC", "QA", "C", 3000, 8),
        opt_hitter(9911, "QASTUD_C", "QA", "C", 15000, 25),
        opt_hitter(9912, "QA1B", "QA", "1B", 3000, 8),
        opt_hitter(9913, "QA2B", "QA", "2B", 3000, 8),
    ]
    qb_hitters = [
        opt_hitter(9920, "QB3B", "QB", "3B", 3000, 8),
        opt_hitter(9921, "QBSS", "QB", "SS", 3000, 8),
        opt_hitter(9922, "QBOF", "QB", "OF", 3000, 8),
    ]
    qmisc_hitters = [
        opt_hitter(9930, "QJUNK1", "QMISC", "OF", 2000, 3),
        opt_hitter(9931, "QJUNK2", "QMISC", "OF", 2000, 3),
        opt_hitter(9932, "QPREM1", "QMISC", "OF", 9000, 9),
        opt_hitter(9933, "QPREM2", "QMISC", "OF", 9000, 9),
    ]
    quality_slate = {
        "games": [
            {
                "home": {"abbrev": "QA", "hitters": qa_hitters,
                         "probable_pitcher": q_pitcher_a, "scratches": []},
                "away": {"abbrev": "QX", "hitters": [], "probable_pitcher": None, "scratches": []},
            },
            {
                "home": {"abbrev": "QB", "hitters": qb_hitters,
                         "probable_pitcher": q_pitcher_b, "scratches": []},
                "away": {"abbrev": "QY", "hitters": [], "probable_pitcher": None, "scratches": []},
            },
            {
                "home": {"abbrev": "QMISC", "hitters": qmisc_hitters,
                         "probable_pitcher": None, "scratches": []},
                "away": {"abbrev": "QZ", "hitters": [], "probable_pitcher": None, "scratches": []},
            },
        ]
    }

    junk_ids = {9930, 9931}
    prem_ids = {9932, 9933}
    stud_c_id = 9911

    # Bypasses the default via a trivial, non-restrictive explicit
    # one_off_min_salary=0 -- confirms the TRUE unconstrained optimum
    # really does use the junk OF pair to fund the catcher upgrade.
    quality_baseline = optimizer.generate_lineups(
        quality_slate, stack_groups=[3, 3], stack_teams=["QA", "QB"], one_off_min_salary=0,
    )["lineups"][0]
    baseline_ids = hitter_ids(quality_baseline)
    # The true optimum turns out to mix one junk + one premium OF pick
    # (higher combined fpts than either extreme) to help fund the
    # catcher upgrade -- at least one non-qualifying junk pick is what
    # matters here, not necessarily both.
    check("without the default, the true optimum funds a catcher upgrade with a junk OF pick",
          bool(junk_ids & baseline_ids) and stud_c_id in baseline_ids, str(baseline_ids))

    quality_default = optimizer.generate_lineups(
        quality_slate, stack_groups=[3, 3], stack_teams=["QA", "QB"],
    )["lineups"][0]
    default_ids = hitter_ids(quality_default)
    check("the default one-off quality preference blocks the junk OF pair entirely",
          not (junk_ids & default_ids), str(default_ids))
    check("...forcing the premium OF pair in instead, even at a real fpts cost",
          prem_ids <= default_ids, str(default_ids))

    print("\nLineup optimizer: cumulative ownership")

    # Two options per slot -- "chalk" (high ownership) and "contrarian"
    # (low ownership) -- with fpts deliberately tilted so the fpts-max
    # objective alone would always pick one side, proving an ownership
    # bound actually forces a swap rather than trivially matching what
    # the optimizer would have built anyway.
    def own_hitter(pid, name, pos, salary, fpts, own):
        return opt_hitter(pid, name, "OWN", pos, salary, fpts, own=own)

    def build_own_slate(chalk_wins):
        d = 0.5 if chalk_wins else -0.5
        hitters = []
        pid = 9800
        for pos, sal in (("C", 3000), ("1B", 3500), ("2B", 3200), ("3B", 3800), ("SS", 3400)):
            hitters.append(own_hitter(pid, f"{pos}chalk", pos, sal, 10 + d, 40))
            pid += 1
            hitters.append(own_hitter(pid, f"{pos}contra", pos, sal, 9.5 - d, 5))
            pid += 1
        for i in range(3):
            hitters.append(own_hitter(pid, f"OFchalk{i}", "OF", 4000, 10 + d, 40))
            pid += 1
        for i in range(3):
            hitters.append(own_hitter(pid, f"OFcontra{i}", "OF", 4000, 9.5 - d, 5))
            pid += 1
        pitcher_chalk = opt_pitcher(9850, "Pchalk1", 6000, 15 + d, own=40)
        # Every other pitcher candidate lives on OWN too (not OWO) --
        # the opposing-pitcher rule would otherwise ban OWN's own
        # hitters whenever OWO's pitcher gets picked, unrelated to the
        # fpts-vs-ownership comparison this fixture actually tests.
        hitters.append(opt_pitcher(9852, "Pcontra1", 6000, 14.5 - d, own=5))
        hitters.append(opt_pitcher(9851, "Pchalk2", 6000, 15 + d, own=40))
        hitters.append(opt_pitcher(9853, "Pcontra2", 6000, 14.5 - d, own=5))
        return {
            "games": [
                {
                    "home": {"abbrev": "OWN", "hitters": hitters,
                             "probable_pitcher": pitcher_chalk, "scratches": []},
                    "away": {"abbrev": "OWO", "hitters": [], "probable_pitcher": None, "scratches": []},
                }
            ]
        }

    def total_ownership(lu):
        return sum(p["ownership_pct"] for slot in lu["slots"].values() for p in slot)

    max_slate = build_own_slate(chalk_wins=True)
    baseline_max = optimizer.generate_lineups(max_slate)["lineups"][0]
    check("total_ownership_pct is reported and matches the sum of the 10 rostered players",
          baseline_max["total_ownership_pct"] == total_ownership(baseline_max),
          str(baseline_max["total_ownership_pct"]))
    check("ownership baseline: an unconstrained fpts-max solve naturally builds the chalky lineup",
          total_ownership(baseline_max) == 400, str(total_ownership(baseline_max)))

    capped = optimizer.generate_lineups(max_slate, max_ownership_pct=100)["lineups"][0]
    check("max_ownership_pct forces total ownership down, even at a fpts cost",
          total_ownership(capped) <= 100, str(total_ownership(capped)))

    min_slate = build_own_slate(chalk_wins=False)
    baseline_min = optimizer.generate_lineups(min_slate)["lineups"][0]
    check("ownership baseline: an unconstrained fpts-max solve can just as easily be low-ownership",
          total_ownership(baseline_min) == 50, str(total_ownership(baseline_min)))

    floored = optimizer.generate_lineups(min_slate, min_ownership_pct=300)["lineups"][0]
    check("min_ownership_pct forces total ownership up, even at a fpts cost",
          total_ownership(floored) >= 300, str(total_ownership(floored)))

    try:
        optimizer.generate_lineups(max_slate, min_ownership_pct=200, max_ownership_pct=100)
        check("optimizer rejects min_ownership_pct above max_ownership_pct", False)
    except optimizer.OptimizerError:
        check("optimizer rejects min_ownership_pct above max_ownership_pct", True)

    print("\nLineup optimizer: restricting the pool to specific DK-slate games")

    def all_players(lu):
        return {p["name"] for slot in lu["slots"].values() for p in slot}

    restricted = optimizer.generate_lineups(
        mul_slate, included_game_pks=[88001, 88002]
    )["lineups"][0]
    check("included_game_pks excludes pitchers from a game left out entirely",
          not ({"MP5", "MP6"} & all_players(restricted)), str(all_players(restricted)))

    all_included = optimizer.generate_lineups(
        mul_slate, included_game_pks=[88001, 88002, 88003]
    )["lineups"][0]
    baseline = optimizer.generate_lineups(mul_slate)["lineups"][0]
    check("including every game reproduces the fully unconstrained result",
          all_included == baseline, str(all_included))

    check("mul_slate's unconstrained baseline naturally spends the full salary cap",
          baseline["salary_used"] == optimizer.SALARY_CAP, str(baseline["salary_used"]))
    max_capped = optimizer.generate_lineups(mul_slate, max_salary=45000)["lineups"][0]
    check("max_salary caps total lineup salary below the fixed $50,000 cap",
          max_capped["salary_used"] <= 45000, str(max_capped["salary_used"]))

    try:
        optimizer.generate_lineups(mul_slate, max_salary=optimizer.SALARY_CAP + 1)
        check("optimizer rejects a max_salary above the salary cap", False)
    except optimizer.OptimizerError:
        check("optimizer rejects a max_salary above the salary cap", True)

    try:
        optimizer.generate_lineups(mul_slate, min_salary=40000, max_salary=30000)
        check("optimizer rejects min_salary above max_salary", False)
    except optimizer.OptimizerError:
        check("optimizer rejects min_salary above max_salary", True)

    try:
        optimizer.generate_lineups(mul_slate, included_game_pks=[88003])
        check("restricting to a game with no hitters at all fails to build a legal lineup", False)
    except optimizer.OptimizerError:
        check("restricting to a game with no hitters at all fails to build a legal lineup", True)

    try:
        optimizer.generate_lineups(mul_slate, included_game_pks=[])
        check("optimizer rejects an empty included_game_pks list", False)
    except optimizer.OptimizerError:
        check("optimizer rejects an empty included_game_pks list", True)

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
    check("mlb_slate.py attaches RotoWire's projected batting spot onto the hitter as "
          "projected_batting_order, wired end to end through _projection_info()",
          righty["projected_batting_order"] == 3, str(righty["projected_batting_order"]))
    check("a hitter with no matching projections row (or no lineup_spot in it) has "
          "projected_batting_order left None, not a fabricated guess",
          lefty["projected_batting_order"] is None, str(lefty["projected_batting_order"]))
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
          righty["projection"] == {"fpts": 12.4, "ownership_pct": 18.7, "lineup_spot": 3},
          str(righty["projection"]))
    check("hitter present in salaries but absent from projections gets None",
          sox["Boston Slugger"]["projection"] is None,
          str(sox["Boston Slugger"]["projection"]))
    check("home starter matched a projection",
          game["home"]["probable_pitcher"]["projection"] == {"fpts": 17.2, "ownership_pct": 25.3, "lineup_spot": None},
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
    check("all fourteen components present",
          len(righty["edge"]["components"]) == 14,
          str(sorted(righty["edge"]["components"])))
    check("weights sum to ~1.0 (within rounding of the 3-decimal-place trims each new addition makes)",
          abs(sum(scoring.WEIGHTS.values()) - 1.0) < 0.01,
          str(sum(scoring.WEIGHTS.values())))

    print("\nUmpire tendency (scoring.umpire_component -- real RotoWire assignment + rate stats)")

    no_assignment = scoring.umpire_component(None, 8.5, 17.0)
    check("no umpire assignment yet -> neutral, not a crash",
          no_assignment["value"] == scoring.NEUTRAL, str(no_assignment))
    no_league_avg = scoring.umpire_component({"name": "X", "rpg": 10.0, "kpg": 14.0}, None, None)
    check("no league-average baseline yet (too few umpires posted today) -> neutral",
          no_league_avg["value"] == scoring.NEUTRAL, str(no_league_avg))

    hitter_friendly = scoring.umpire_component(
        {"name": "Hitter Ump", "rpg": 10.0, "kpg": 14.0}, 8.5, 17.0
    )
    check("above-average RPG + below-average KPG (a hitter-friendly zone) scores above neutral",
          hitter_friendly["value"] > scoring.NEUTRAL, str(hitter_friendly))
    pitcher_friendly = scoring.umpire_component(
        {"name": "Pitcher Ump", "rpg": 7.0, "kpg": 20.0}, 8.5, 17.0
    )
    check("below-average RPG + above-average KPG (a pitcher-friendly zone) scores below neutral",
          pitcher_friendly["value"] < scoring.NEUTRAL, str(pitcher_friendly))
    check("umpire_component's multiplier stays within its own documented cap even for an extreme ump",
          scoring.umpire_component({"name": "Extreme", "rpg": 100.0, "kpg": 0.5}, 8.5, 17.0)["value"]
          == round(1 + scoring.UMPIRE_MULTIPLIER_CAP, 3),
          str(scoring.umpire_component({"name": "Extreme", "rpg": 100.0, "kpg": 0.5}, 8.5, 17.0)))

    # End-to-end through the real pipeline: this fixture's real game
    # (NYY@BOS) is assigned the hitter-friendly fake umpire (10.0 RPG,
    # 14.0 KPG vs. a 8.5/17.0 league average computed from BOTH fake
    # umpires -- see FAKE_UMPIRES), so BOS/NYY hitters should show a
    # real above-neutral umpire component, and both pitchers a real
    # below-neutral (inverted) one.
    check("a real hitter in this fixture's game shows the real fake umpire's above-neutral effect",
          righty["edge"]["components"]["umpire"]["value"] > scoring.NEUTRAL,
          str(righty["edge"]["components"]["umpire"]))
    check("a real pitcher in this fixture's game shows the SAME umpire's effect inverted (below neutral)",
          home_edge["components"]["umpire"]["value"] < scoring.NEUTRAL,
          str(home_edge["components"]["umpire"]))

    print("\nBullpen recent workload (independent of season-long ERA)")
    # BULLPEN and BULLPEN_WORKLOAD are deliberately opposite for these two
    # teams: the Red Sox pen (111) is bad ALL SEASON (5.20 ERA) but was
    # barely used the last 2 days (6 outs); the Yankees pen (147) has a
    # fine season (3.00 ERA) but got hammered recently (30 outs, an
    # extra-inning-sized workload). Neither signal should be able to
    # fake the other.
    redsox = {h["name"]: h for h in game["home"]["hitters"]}
    check("a Yankees hitter (facing the rested-but-bad-all-year Red Sox pen) "
          "reads recent outs correctly, even though season ERA says something else",
          righty["edge"]["components"]["bullpen_workload"]["outs"] == 6,
          str(righty["edge"]["components"]["bullpen_workload"]))
    check("that same well-rested bullpen scores below neutral on workload "
          "(good matchup for the pitcher, bad for the hitter) despite its poor season ERA",
          righty["edge"]["components"]["bullpen_workload"]["value"] < 1.0,
          str(righty["edge"]["components"]["bullpen_workload"]))

    redsox_hitter = next(iter(redsox.values()))
    check("a Red Sox hitter (facing the heavily-taxed-but-good-all-year Yankees pen) "
          "scores above neutral on workload (good matchup) despite the strong season ERA",
          redsox_hitter["edge"]["components"]["bullpen_workload"]["value"] > 1.0,
          str(redsox_hitter["edge"]["components"]["bullpen_workload"]))
    check("the same Red Sox hitter scores BELOW neutral on season-long bullpen quality "
          "-- proving workload and season ERA are genuinely independent signals, not "
          "one masquerading as the other",
          redsox_hitter["edge"]["components"]["bullpen"]["value"] < 1.0,
          str(redsox_hitter["edge"]["components"]["bullpen"]))

    print("\nStolen-base component (DraftKings pays +5/SB, same as a double -- "
          "previously invisible to the model)")
    burner = scoring.stolen_base_component({"sb": 35, "pa": 550, "sb_per_pa": 0.0636}, 0.012)
    grinder = scoring.stolen_base_component({"sb": 1, "pa": 550, "sb_per_pa": 0.0018}, 0.012)
    check("a burner (well above league-average SB rate) scores above neutral",
          burner["value"] > 1.0, str(burner))
    check("a near-zero-steal hitter, same sample size, scores below neutral",
          grinder["value"] < 1.0, str(grinder))
    check("the burner clearly outscores the zero-speed hitter on this component alone",
          burner["value"] > grinder["value"] + 0.5, str((burner["value"], grinder["value"])))
    small_sample = scoring.stolen_base_component({"sb": 3, "pa": 80, "sb_per_pa": 0.0375}, 0.012)
    check("a hot start in a small sample regresses well short of the full-sample burner value",
          1.0 < small_sample["value"] < burner["value"], str(small_sample))
    check("no season stat at all is neutral, not a crash",
          scoring.stolen_base_component(None, 0.012)["value"] == 1.0)
    check("a missing league baseline is neutral rather than dividing by nothing",
          scoring.stolen_base_component({"sb": 10, "pa": 400, "sb_per_pa": 0.025}, None)["value"] == 1.0)

    contact = sox["Boston Contact"]
    check("wired end-to-end: a real 35-SB fixture hitter scores above neutral on stolen_base",
          switch["edge"]["components"]["stolen_base"]["value"] > 1.0,
          str(switch["edge"]["components"]["stolen_base"]))
    check("wired end-to-end: a real 1-SB fixture hitter, same season OPS, scores below neutral",
          contact["edge"]["components"]["stolen_base"]["value"] < 1.0,
          str(contact["edge"]["components"]["stolen_base"]))
    check("the burner clearly outscores the grinder on this component despite identical OPS",
          switch["edge"]["components"]["stolen_base"]["value"]
          > contact["edge"]["components"]["stolen_base"]["value"],
          str((switch["edge"]["components"]["stolen_base"]["value"],
               contact["edge"]["components"]["stolen_base"]["value"])))
    check("raw SB count is still exposed on the season stat line, unaffected by the new component",
          switch["season"]["sb"] == 35 and contact["season"]["sb"] == 1,
          str((switch["season"]["sb"], contact["season"]["sb"])))

    print("\nHome-run probability component (individual batter HR chance)")
    slugger_stat = {"hr_per_pa": 0.06, "pa": 550}
    contact_stat = {"hr_per_pa": 0.015, "pa": 550}
    league_hr_per_pa = 0.03
    league_pitcher_hr9 = 1.10
    neutral_park, neutral_weather = 1.0, 1.0

    hr_slugger = scoring.home_run_component(
        slugger_stat, league_hr_per_pa, neutral_park, neutral_weather, 1.10, league_pitcher_hr9
    )
    hr_contact = scoring.home_run_component(
        contact_stat, league_hr_per_pa, neutral_park, neutral_weather, 1.10, league_pitcher_hr9
    )
    check("a well-above-average power hitter scores above neutral",
          hr_slugger["value"] > 1.0, str(hr_slugger))
    check("a well-below-average power hitter scores below neutral",
          hr_contact["value"] < 1.0, str(hr_contact))
    check("the slugger clearly outscores the contact hitter on this component alone",
          hr_slugger["value"] > hr_contact["value"] + 0.2,
          str((hr_slugger["value"], hr_contact["value"])))
    check("the slugger's real probability_pct is a plausible, non-trivial percentage",
          5.0 < hr_slugger["probability_pct"] < 70.0, str(hr_slugger["probability_pct"]))
    check("the slugger's probability clearly exceeds the contact hitter's",
          hr_slugger["probability_pct"] > hr_contact["probability_pct"],
          str((hr_slugger["probability_pct"], hr_contact["probability_pct"])))

    hr_small_sample = scoring.home_run_component(
        {"hr_per_pa": 0.06, "pa": 20}, league_hr_per_pa, neutral_park, neutral_weather, 1.10, league_pitcher_hr9
    )
    check("a hot start in a tiny sample regresses well short of the full-sample slugger's value",
          1.0 < hr_small_sample["value"] < hr_slugger["value"], str(hr_small_sample))

    check("no season stat at all is neutral, not a crash",
          scoring.home_run_component(None, league_hr_per_pa, 1.0, 1.0, 1.1, league_pitcher_hr9)["value"] == 1.0)
    check("a missing league HR-rate baseline is neutral rather than dividing by nothing",
          scoring.home_run_component(slugger_stat, None, 1.0, 1.0, 1.1, league_pitcher_hr9)["value"] == 1.0)
    check("no season stat also reports probability_pct as None, not a fabricated number",
          scoring.home_run_component(None, league_hr_per_pa, 1.0, 1.0, 1.1, league_pitcher_hr9)["probability_pct"] is None)

    # `value` (what feeds the composite score) is deliberately blind to
    # park/weather -- those already have their own separately-weighted
    # components, so this proves home_run_component doesn't double-count
    # them a second time in the composite.
    hr_bad_park = scoring.home_run_component(
        slugger_stat, league_hr_per_pa, 0.7, 0.8, 1.10, league_pitcher_hr9
    )
    check("`value` is unaffected by park/weather -- they're not double-counted "
          "against the existing separately-weighted park/weather components",
          hr_slugger["value"] == hr_bad_park["value"],
          str((hr_slugger["value"], hr_bad_park["value"])))
    check("but `probability_pct` (the real, complete answer) DOES drop in a suppressive "
          "park/weather environment, since that's genuinely true context for a real HR chance",
          hr_bad_park["probability_pct"] < hr_slugger["probability_pct"],
          str((hr_bad_park["probability_pct"], hr_slugger["probability_pct"])))

    hr_good_park = scoring.home_run_component(
        slugger_stat, league_hr_per_pa, 1.3, 1.2, 1.10, league_pitcher_hr9
    )
    check("a launching-pad park/favorable wind raises probability_pct above the neutral-context version",
          hr_good_park["probability_pct"] > hr_slugger["probability_pct"],
          str((hr_good_park["probability_pct"], hr_slugger["probability_pct"])))

    # A moderate (not maxed-out) power hitter here -- `slugger_stat`'s 2x-
    # league HR rate already sits near home_run_component's own `value`
    # cap on its own, so combining it with a hot pitcher factor would
    # saturate both sides at the cap and mask the real directional
    # effect being tested.
    moderate_stat = {"hr_per_pa": 0.033, "pa": 550}
    hr_neutral_pitcher = scoring.home_run_component(
        moderate_stat, league_hr_per_pa, neutral_park, neutral_weather, 1.10, league_pitcher_hr9
    )
    hr_vs_gopher_pitcher = scoring.home_run_component(
        moderate_stat, league_hr_per_pa, neutral_park, neutral_weather, 1.50, league_pitcher_hr9
    )
    check("facing a genuinely HR-prone pitcher raises BOTH value and probability_pct "
          "(this is new information, not already captured by park/weather)",
          hr_vs_gopher_pitcher["value"] > hr_neutral_pitcher["value"]
          and hr_vs_gopher_pitcher["probability_pct"] > hr_neutral_pitcher["probability_pct"],
          str((hr_vs_gopher_pitcher["value"], hr_neutral_pitcher["value"],
               hr_vs_gopher_pitcher["probability_pct"], hr_neutral_pitcher["probability_pct"])))

    check("probability_pct never exceeds the function's own sanity cap even for an extreme input",
          scoring.home_run_component(
              {"hr_per_pa": 0.20, "pa": 600}, league_hr_per_pa, 1.4, 1.3, 2.5, league_pitcher_hr9
          )["probability_pct"] <= 70.0, "")

    print("\nReal market props blended into projections (individual player HR/hit/K props)")

    # A real market HR prop that disagrees sharply with the model (the
    # contact hitter, whose own season rate says a low HR chance, but a
    # real sportsbook price implying 35% tonight -- a launching-pad park
    # or a live lineup change the season-rate model can't see).
    hr_market_only = scoring.home_run_component(
        contact_stat, league_hr_per_pa, neutral_park, neutral_weather, 1.10, league_pitcher_hr9,
    )
    hr_with_market = scoring.home_run_component(
        contact_stat, league_hr_per_pa, neutral_park, neutral_weather, 1.10, league_pitcher_hr9,
        market_hr_probability_pct=35.0,
    )
    check("a real HR prop that disagrees with the model pulls both value and probability_pct "
          "toward the market's own number",
          hr_with_market["value"] > hr_market_only["value"]
          and hr_with_market["probability_pct"] > hr_market_only["probability_pct"],
          str((hr_market_only, hr_with_market)))
    check("the blended probability_pct sits between the model-only and pure-market numbers, "
          "weighted toward the market per MARKET_BLEND_WEIGHT (0.7)",
          hr_market_only["probability_pct"] < hr_with_market["probability_pct"] < 35.0
          and abs(hr_with_market["probability_pct"] - (0.3 * hr_market_only["probability_pct"] + 0.7 * 35.0)) < 0.15,
          str((hr_market_only["probability_pct"], hr_with_market["probability_pct"])))
    check("a real market prop is used outright (not discarded) even with no season rate to blend against",
          scoring.home_run_component(
              None, league_hr_per_pa, neutral_park, neutral_weather, 1.10, league_pitcher_hr9,
              market_hr_probability_pct=22.0,
          ) == {"value": 1.0, "probability_pct": 22.0,
                "detail": "22.0% market-implied HR chance (no season rate to blend against)"},
          str(scoring.home_run_component(
              None, league_hr_per_pa, neutral_park, neutral_weather, 1.10, league_pitcher_hr9,
              market_hr_probability_pct=22.0,
          )))
    hr_no_market_kw = scoring.home_run_component(
        contact_stat, league_hr_per_pa, neutral_park, neutral_weather, 1.10, league_pitcher_hr9,
        market_hr_probability_pct=None,
    )
    check("passing market_hr_probability_pct=None explicitly reproduces the same result as omitting it",
          hr_no_market_kw == hr_market_only, str((hr_no_market_kw, hr_market_only)))

    check("hit_probability_component is neutral (1.0) with no market prop -- never fabricates a hit chance",
          scoring.hit_probability_component(None) == {
              "value": 1.0, "probability_pct": None, "detail": "no market hit prop available"
          },
          str(scoring.hit_probability_component(None)))
    hit_above_avg = scoring.hit_probability_component(85.0)
    hit_below_avg = scoring.hit_probability_component(40.0)
    check("a hit probability well above the league-average reference scores above neutral",
          hit_above_avg["value"] > 1.0, str(hit_above_avg))
    check("a hit probability well below the league-average reference scores below neutral",
          hit_below_avg["value"] < 1.0, str(hit_below_avg))
    check("hit_probability_component's value is exactly market_pct / LEAGUE_AVG_HIT_PROBABILITY_PCT, capped",
          hit_above_avg["value"] == round(min(1.4, 85.0 / scoring.LEAGUE_AVG_HIT_PROBABILITY_PCT), 3),
          str(hit_above_avg))
    check("hit_probability_component caps at 1.4 for an extreme market probability",
          scoring.hit_probability_component(100.0)["value"] == 1.4, "")
    check("hit_probability_component floors at 0.6 for a near-zero market probability",
          scoring.hit_probability_component(1.0)["value"] == 0.6, "")

    k_model_only = scoring.strikeout_potential_component(
        {"k_per_9": 9.0}, 8.0, None, None,
    )
    k_with_high_market = scoring.strikeout_potential_component(
        {"k_per_9": 9.0}, 8.0, None, None, market_k_line=9.5,
    )
    k_with_low_market = scoring.strikeout_potential_component(
        {"k_per_9": 9.0}, 8.0, None, None, market_k_line=3.5,
    )
    check("a real K prop line ABOVE what the model alone implies pulls value up",
          k_with_high_market["value"] > k_model_only["value"], str((k_model_only, k_with_high_market)))
    check("a real K prop line well BELOW what the model alone implies pulls value down",
          k_with_low_market["value"] < k_model_only["value"], str((k_model_only, k_with_low_market)))
    check("strikeout_potential_component reports the real market_k_line back on the result",
          k_with_high_market["market_k_line"] == 9.5, str(k_with_high_market))
    check("no market_k_line given reports None (not a fabricated line) and reproduces the model-only value",
          k_model_only["market_k_line"] is None
          and scoring.strikeout_potential_component(
              {"k_per_9": 9.0}, 8.0, None, None, market_k_line=None,
          ) == k_model_only,
          str(k_model_only))

    print("\nMarket props: raw row parsing (mlb_slate.py's per-player lookups)")

    check("MLB_PROP_MARKETS is exactly the 3 markets kept after dropping batter_total_bases "
          "(the cost-cutting scope call)",
          odds.MLB_PROP_MARKETS == ["batter_home_runs", "batter_hits", "pitcher_strikeouts"],
          str(odds.MLB_PROP_MARKETS))

    hr_rows = [
        {"player": "Aaron Judge", "side": "Over", "line": 0.5, "price": -150, "implied_pct": 60.0, "book": "DK"},
        {"player": "Aaron Judge", "side": "Under", "line": 0.5, "price": 120, "implied_pct": 45.5, "book": "DK"},
        # A 2nd, higher line for the same player -- the lowest ("at least
        # one") line should always win, not whichever row comes last.
        {"player": "Aaron Judge", "side": "Over", "line": 1.5, "price": 450, "implied_pct": 18.2, "book": "DK"},
        {"player": "Juan Soto", "side": "Over", "line": 0.5, "price": 200, "implied_pct": 33.3, "book": "DK"},
    ]
    hr_pct = mlb_slate._market_at_least_one_pct_by_name(hr_rows)
    check("_market_at_least_one_pct_by_name picks the LOWEST 'Over' line per player (the real "
          "'at least one tonight' threshold), not whichever row happens to appear last",
          hr_pct[salaries.normalize_name("Aaron Judge")] == 60.0,
          str(hr_pct))
    check("_market_at_least_one_pct_by_name covers every player in the rows, keyed by normalized name",
          hr_pct == {
              salaries.normalize_name("Aaron Judge"): 60.0,
              salaries.normalize_name("Juan Soto"): 33.3,
          },
          str(hr_pct))
    check("_market_at_least_one_pct_by_name ignores 'Under' rows entirely",
          all(row.get("side") != "Under" or row.get("implied_pct") not in hr_pct.values() for row in hr_rows),
          str(hr_pct))
    check("_market_at_least_one_pct_by_name returns an empty dict for no rows, not a crash",
          mlb_slate._market_at_least_one_pct_by_name([]) == {}, "")

    k_rows = [
        {"player": "Gerrit Cole", "side": "Over", "line": 6.5, "price": -110, "implied_pct": 52.4, "book": "DK"},
        {"player": "Gerrit Cole", "side": "Under", "line": 6.5, "price": -110, "implied_pct": 52.4, "book": "DK"},
    ]
    k_lines = mlb_slate._market_line_by_name(k_rows)
    check("_market_line_by_name reads the real posted strikeout line for a pitcher",
          k_lines[salaries.normalize_name("Gerrit Cole")] == 6.5, str(k_lines))
    check("_market_line_by_name returns an empty dict for no rows, not a crash",
          mlb_slate._market_line_by_name([]) == {}, "")

    print("\nMarket props: wired end-to-end through a real slate rebuild "
          "(fetch -> parse -> match by name -> blended into edge.components)")

    # Deliberately extreme, unmistakable values -- a real HR prop far
    # BELOW whatever the model alone would say for Big Righty Bat (a real
    # power bat), a hit prop far ABOVE league average, and a K line far
    # above what Righty Rogers' own K/9 alone implies -- so the blended
    # result is obviously, unambiguously the market's influence, not
    # noise in the model's own natural range.
    async def fake_props_for_evt1(event_id, sport="mlb", markets=None, *, day=None, force=False):
        if event_id != "evt1":
            return {}
        return {
            "batter_home_runs": [
                {"player": "Big Righty Bat", "side": "Over", "line": 0.5, "price": 1900,
                 "implied_pct": 5.0, "book": "DK"},
            ],
            "batter_hits": [
                {"player": "Big Righty Bat", "side": "Over", "line": 0.5, "price": -900,
                 "implied_pct": 90.0, "book": "DK"},
            ],
            "pitcher_strikeouts": [
                {"player": "Righty Rogers", "side": "Over", "line": 12.0, "price": -110,
                 "implied_pct": 50.0, "book": "DK"},
                {"player": "Righty Rogers", "side": "Under", "line": 12.0, "price": -110,
                 "implied_pct": 50.0, "book": "DK"},
            ],
        }

    odds.get_player_props = fake_props_for_evt1
    props_slate = await mlb_slate.build_slate(DAY, force_refresh=True)
    props_game = props_slate["games"][0]
    props_bat = next(h for h in props_game["away"]["hitters"] if h["name"] == "Big Righty Bat")
    props_pitcher = props_game["away"]["probable_pitcher"]

    check("a real, extreme-low HR prop pulls the wired hitter's blended probability_pct well "
          "below what the earlier model-only fixture run showed for this exact player "
          "(top_power's own home_run value was > 1.0, i.e. an above-neutral model read)",
          props_bat["edge"]["components"]["home_run"]["probability_pct"] < 15.0,
          str(props_bat["edge"]["components"]["home_run"]))
    check("a real hit prop is threaded through to hit_probability exactly (100% market, no model "
          "to blend against)",
          props_bat["edge"]["components"]["hit_probability"]["probability_pct"] == 90.0
          and props_bat["edge"]["components"]["hit_probability"]["value"] > 1.0,
          str(props_bat["edge"]["components"]["hit_probability"]))
    check("a real K prop line is threaded through to the matching pitcher's strikeout_potential "
          "component, matched by name across the fetch -> parse -> _pitcher_edge() pipeline",
          props_pitcher["edge"]["components"]["strikeout_potential"]["market_k_line"] == 12.0,
          str(props_pitcher["edge"]["components"]["strikeout_potential"]))
    check("the OTHER side's pitcher (no props fetched for him in this fixture) reports no "
          "market_k_line -- props are matched per-player, not blanket-applied to the whole game",
          props_game["home"]["probable_pitcher"]["edge"]["components"]["strikeout_potential"]
          .get("market_k_line") is None,
          str(props_game["home"]["probable_pitcher"]["edge"]["components"]["strikeout_potential"]))

    # Regression guard for a real waste bug found and fixed while wiring
    # this in: props were originally fetched for every game regardless
    # of include_hitters, even though nothing that consumes them
    # (hitters, pitcher edges) is ever built in that lighter mode
    # (routers/mlb.py's GET /games) -- silently spending real credits
    # for data nothing would use.
    props_call_count = 0

    async def counting_fake_props(event_id, sport="mlb", markets=None, *, day=None, force=False):
        nonlocal props_call_count
        props_call_count += 1
        return {}

    odds.get_player_props = counting_fake_props
    await mlb_slate.build_slate(DAY, force_refresh=True, include_hitters=False)
    check("get_player_props is never called when include_hitters=False -- no wasted credit spend "
          "for data nothing downstream would use",
          props_call_count == 0, str(props_call_count))
    odds.get_player_props = fake_props_for_evt1

    # Wired end-to-end through the real slate build: Big Righty Bat (101,
    # 30 HR/580 PA) is the fixture's clear top power bat -- Boston
    # Contact (202, 13 HR/480 PA) its clear weakest.
    top_power = yanks["Big Righty Bat"]
    weak_power = sox["Boston Contact"]
    check("wired end-to-end: the fixture's clear top power bat scores above neutral on home_run",
          top_power["edge"]["components"]["home_run"]["value"] > 1.0,
          str(top_power["edge"]["components"]["home_run"]))
    check("wired end-to-end: the fixture's clear weakest power bat scores at or below neutral",
          weak_power["edge"]["components"]["home_run"]["value"] <= 1.0,
          str(weak_power["edge"]["components"]["home_run"]))
    check("wired end-to-end: both real hitters get a real, sane probability_pct",
          all(
              0.0 <= h["edge"]["components"]["home_run"]["probability_pct"] <= 70.0
              for h in (top_power, weak_power)
          ),
          str((top_power["edge"]["components"]["home_run"]["probability_pct"],
               weak_power["edge"]["components"]["home_run"]["probability_pct"])))

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

    print("\nContest field generator: ownership-weighted sampling")

    field = contest.generate_field(mul_slate, 60, seed=7)
    check("generate_field builds the requested sample size against a deep-enough pool",
          len(field) == 60, str(len(field)))
    check("every field lineup respects the salary cap",
          all(lu["salary_used"] <= optimizer.SALARY_CAP for lu in field),
          str(max(lu["salary_used"] for lu in field)))
    check("every field lineup has exactly ROSTER_SIZE distinct players",
          all(len({p["id"] for p in lu["players"]}) == optimizer.ROSTER_SIZE for lu in field))

    # mul_slate's known opponent pairs: MUL1 vs MUL2, MUL3 vs MUL4, MUL5
    # vs MUL6. Pitcher ids are known (MP1-MP6 = 9400-9405); anything
    # else in a field lineup is a hitter.
    MUL_OPPONENT = {"MUL1": "MUL2", "MUL2": "MUL1", "MUL3": "MUL4", "MUL4": "MUL3", "MUL5": "MUL6", "MUL6": "MUL5"}
    MUL_PITCHER_IDS = {9400, 9401, 9402, 9403, 9404, 9405}

    def has_opposing_pitcher_hitter_pair(lu):
        pitcher_teams = {p["team"] for p in lu["players"] if p["id"] in MUL_PITCHER_IDS}
        hitter_teams = {p["team"] for p in lu["players"] if p["id"] not in MUL_PITCHER_IDS}
        return any(MUL_OPPONENT.get(t) in hitter_teams for t in pitcher_teams)

    check("generate_field never pairs a pitcher with a hitter from the team he's facing",
          not any(has_opposing_pitcher_hitter_pair(lu) for lu in field),
          str([lu["players"] for lu in field if has_opposing_pitcher_hitter_pair(lu)][:1]))

    floored_field = contest.generate_field(mul_slate, 20, min_salary=45000, seed=9)
    check("generate_field's min_salary floors every sampled lineup's salary",
          all(lu["salary_used"] >= 45000 for lu in floored_field),
          str(min(lu["salary_used"] for lu in floored_field)))

    capped_field = contest.generate_field(mul_slate, 20, max_salary=45000, seed=9)
    check("generate_field's max_salary caps every sampled lineup's salary",
          all(lu["salary_used"] <= 45000 for lu in capped_field),
          str(max(lu["salary_used"] for lu in capped_field)))

    try:
        contest.generate_field(mul_slate, 10, max_salary=optimizer.SALARY_CAP + 1)
        check("generate_field rejects a max_salary above the salary cap", False)
    except contest.ContestError:
        check("generate_field rejects a max_salary above the salary cap", True)

    try:
        contest.generate_field(mul_slate, 10, min_salary=40000, max_salary=30000)
        check("generate_field rejects min_salary above max_salary", False)
    except contest.ContestError:
        check("generate_field rejects min_salary above max_salary", True)

    field_again = contest.generate_field(mul_slate, 60, seed=7)
    same_ids = [frozenset(p["id"] for p in lu["players"]) for lu in field]
    again_ids = [frozenset(p["id"] for p in lu["players"]) for lu in field_again]
    check("the same seed reproduces the same field",
          same_ids == again_ids)

    field_other_seed = contest.generate_field(mul_slate, 60, seed=99)
    other_ids = [frozenset(p["id"] for p in lu["players"]) for lu in field_other_seed]
    check("a different seed produces a different field",
          same_ids != other_ids)

    exposure = contest.field_exposure(field, top_n=5)
    check("field_exposure is sorted descending by count",
          all(exposure[i]["count"] >= exposure[i + 1]["count"] for i in range(len(exposure) - 1)),
          str(exposure))
    check("field_exposure percentages are computed against the sample size",
          exposure[0]["pct"] == round(100 * exposure[0]["count"] / len(field), 1))

    # Includes game 88003 (MUL5/MUL6, no hitters at all) alongside
    # 88001 -- a single 2-team game alone is no longer buildable here:
    # its only 2 real pitchers each oppose the other's hitters, so
    # using both (the only pitcher option with just 2 candidates) would
    # ban every hitter. MUL5/MUL6 exist purely as a legal 2nd-pitcher
    # source; the real thing being proven is that MUL3/MUL4 (excluded
    # from included_game_pks) never appear.
    restricted = contest.generate_field(mul_slate, 30, included_game_pks=[88001, 88003], seed=3)
    restricted_teams = {p["team"] for lu in restricted for p in lu["players"]}
    check("included_game_pks restricts the field to only those games' teams",
          restricted_teams <= {"MUL1", "MUL2", "MUL5", "MUL6"}, str(restricted_teams))

    try:
        contest.generate_field(mul_slate, 0)
        check("generate_field rejects sample_size < 1", False)
    except contest.ContestError:
        check("generate_field rejects sample_size < 1", True)

    try:
        contest.generate_field(mul_slate, contest.MAX_SAMPLE_SIZE + 1)
        check("generate_field rejects sample_size above MAX_SAMPLE_SIZE", False)
    except contest.ContestError:
        check("generate_field rejects sample_size above MAX_SAMPLE_SIZE", True)

    try:
        contest.generate_field({"games": []}, 10)
        check("generate_field raises ContestError on an empty pool", False)
    except contest.ContestError:
        check("generate_field raises ContestError on an empty pool", True)

    try:
        # Both games here have zero hitters (pitcher-only fixtures) --
        # every hitter slot type has nobody eligible.
        contest.generate_field(mul_slate, 10, included_game_pks=[88002])
        check("generate_field raises ContestError when a hitter slot has no candidates", False)
    except contest.ContestError:
        check("generate_field raises ContestError when a hitter slot has no candidates", True)

    print("\nContest field generator: stakes-tiered field sharpness")

    try:
        contest.generate_field(mul_slate, 10, field_sharpness="ultra")
        check("generate_field rejects an unknown field_sharpness", False)
    except contest.ContestError:
        check("generate_field rejects an unknown field_sharpness", True)

    # Direct unit test of the weight function itself, isolated from the
    # noisy statistics of a full random-sampled field -- a heavily
    # chalk-owned, mediocre-value player vs. a lightly-owned, strong-
    # value one, run through each sharpness level's actual weight_fn.
    chalk_player = {"ownership_pct": 40.0, "salary": 5000, "projected_fpts": 10.0}
    value_player = {"ownership_pct": 5.0, "salary": 4000, "projected_fpts": 12.0}

    marquee_ratio = contest._field_weight_fn("marquee")(chalk_player) / contest._field_weight_fn("marquee")(value_player)
    low_ratio = contest._field_weight_fn("low")(chalk_player) / contest._field_weight_fn("low")(value_player)
    high_ratio = contest._field_weight_fn("high")(chalk_player) / contest._field_weight_fn("high")(value_player)

    check("'marquee' field_sharpness weighs the chalk player over the value player in "
          "direct proportion to ownership% (8x)",
          marquee_ratio == 8.0, str(marquee_ratio))
    check("'low' field_sharpness compresses the ownership gap -- the chalk player's edge "
          "over the value player shrinks vs. 'marquee'",
          low_ratio < marquee_ratio,
          f"low={low_ratio:.2f} marquee={marquee_ratio:.2f}")
    check("'high' field_sharpness's points-per-dollar bonus narrows the chalk player's "
          "edge vs. 'marquee' (the value player closes the gap on pure value)",
          high_ratio < marquee_ratio,
          f"high={high_ratio:.2f} marquee={marquee_ratio:.2f}")
    check("'high' field_sharpness still weighs the chalk player above zero (still "
          "ownership-aware, not ownership-blind)",
          contest._field_weight_fn("high")(chalk_player) > 0,
          str(contest._field_weight_fn("high")(chalk_player)))

    # mul_slate never sets real ownership_pct values (see the "Rake
    # sanity check" section below), so every player floors to the same
    # sampling weight regardless of field_sharpness -- proving the
    # weight function differs (above) but not that it actually changes
    # which players a real field-sample walk picks. That statistical
    # check runs later, against chalk_slate (a genuinely
    # ownership-differentiated fixture already built for exactly this).
    default_field = contest.generate_field(mul_slate, 60, seed=41)
    marquee_field = contest.generate_field(mul_slate, 60, seed=41, field_sharpness="marquee")
    default_ids = [frozenset(p["id"] for p in lu["players"]) for lu in default_field]
    marquee_ids = [frozenset(p["id"] for p in lu["players"]) for lu in marquee_field]
    check("field_sharpness defaults to 'marquee' -- an unspecified call reproduces the "
          "same field as an explicit field_sharpness='marquee' call at the same seed",
          default_ids == marquee_ids)

    print("\nContest field generator: ranking and payout curve")

    synthetic_field = [
        {"projected_points": p, "total_ownership_pct": 50.0, "players": []}
        for p in [100, 95, 90, 85, 80, 75, 70, 65, 60, 55]
    ]
    flat_contest = {"field_size": 10, "entry_fee": 10.0, "payout_pct": 0.4, "shape": "flat"}
    flat_eval = contest.evaluate_field(synthetic_field, [{"projected_points": 92}], flat_contest)
    check("evaluate_field computes paid_count and prize_pool from field_size/entry_fee/rake",
          flat_eval["paid_count"] == 4 and flat_eval["prize_pool"] == 85.0,
          str((flat_eval["paid_count"], flat_eval["prize_pool"])))
    r = flat_eval["results"][0]
    check("evaluate_field ranks a lineup by how many field entries it beats on projected points",
          r["percentile"] == 80.0 and r["estimated_rank"] == 2, str(r))
    check("a flat-shape contest pays every cashing rank the same amount",
          r["in_the_money"] and r["estimated_payout"] == round(85.0 / 4, 2), str(r))

    top_heavy_contest = {"field_size": 10, "entry_fee": 10.0, "payout_pct": 0.4, "shape": "top_heavy"}
    best_lineup = contest.evaluate_field(synthetic_field, [{"projected_points": 999}], top_heavy_contest)
    worst_cashing_lineup = contest.evaluate_field(
        synthetic_field, [{"projected_points": 81}], top_heavy_contest
    )
    check("a top-heavy contest pays 1st place meaningfully more than the min-cash line",
          best_lineup["results"][0]["estimated_payout"] > worst_cashing_lineup["results"][0]["estimated_payout"],
          str((best_lineup["results"][0], worst_cashing_lineup["results"][0])))

    last_place_eval = contest.evaluate_field(synthetic_field, [{"projected_points": 1.0}], flat_contest)
    check("a lineup that beats nobody in the sample misses the cash line",
          not last_place_eval["results"][0]["in_the_money"]
          and last_place_eval["results"][0]["estimated_payout"] == 0.0,
          str(last_place_eval["results"][0]))

    try:
        contest.evaluate_field(synthetic_field, [], flat_contest)
        check("evaluate_field rejects an empty list of user lineups", False)
    except contest.ContestError:
        check("evaluate_field rejects an empty list of user lineups", True)

    try:
        contest.evaluate_field(synthetic_field, [{"salary_used": 50000}], flat_contest)
        check("evaluate_field rejects a lineup missing projected_points", False)
    except contest.ContestError:
        check("evaluate_field rejects a lineup missing projected_points", True)

    try:
        contest.build_contest_field(mul_slate, "not_a_real_contest", [{"projected_points": 100}])
        check("build_contest_field rejects an unknown contest_type", False)
    except contest.ContestError:
        check("build_contest_field rejects an unknown contest_type", True)

    check("CONTEST_TYPES has a mid-field GPP preset covering the real 1K-5K entry-count gap between "
          "gpp_small (500) and gpp_large (10,000)",
          "gpp_mid" in contest.CONTEST_TYPES
          and 1_000 <= contest.CONTEST_TYPES["gpp_mid"]["field_size"] <= 5_000,
          str(contest.CONTEST_TYPES.get("gpp_mid")))
    mid_field_batch = contest.build_contest_entries(mul_slate, "gpp_mid", 5, sample_size=50, seed=41)
    check("build_contest_entries builds real entries against the new gpp_mid preset",
          len(mid_field_batch["entries"]) == 5, str(len(mid_field_batch["entries"])))
    check("gpp_mid's field_baseline reflects its own real field_size/payout_pct, not gpp_small's or "
          "gpp_large's",
          mid_field_batch["field_baseline"]["avg_cash_probability_pct"]
          == round(contest.CONTEST_TYPES["gpp_mid"]["payout_pct"] * 100, 1),
          str(mid_field_batch["field_baseline"]))

    print("\nContest field generator: end-to-end against the optimizer")

    opt_lineups = optimizer.generate_lineups(mul_slate, num_lineups=2)["lineups"]
    full = contest.build_contest_field(mul_slate, "gpp_small", opt_lineups, sample_size=100, seed=11)
    check("build_contest_field returns one result per submitted lineup",
          len(full["results"]) == len(opt_lineups), str(len(full["results"])))
    check("build_contest_field reports field ownership and exposure summaries",
          "avg_total_ownership_pct" in full["field_ownership"] and len(full["field_exposure"]) > 0,
          str(full["field_ownership"]))
    check("build_contest_field's note discloses this is projected-points ranking, not simulated outcomes",
          "not simulated" in full["note"], full["note"])

    try:
        contest.build_contest_field(mul_slate, "gpp_small", opt_lineups, field_size=contest.MAX_FIELD_SIZE + 1)
        check("build_contest_field rejects a field_size override above MAX_FIELD_SIZE", False)
    except contest.ContestError:
        check("build_contest_field rejects a field_size override above MAX_FIELD_SIZE", True)

    print("\nContest generator: mass multi-entry (generate_entries)")

    entries = contest.generate_entries(mul_slate, 40, seed=13)
    check("generate_entries builds the requested count against a deep-enough pool",
          len(entries) == 40, str(len(entries)))
    check("every entry respects the salary cap and has ROSTER_SIZE distinct players",
          all(
              lu["salary_used"] <= optimizer.SALARY_CAP
              and len({p["id"] for p in lu["players"]}) == optimizer.ROSTER_SIZE
              for lu in entries
          ))
    signatures = [frozenset(p["id"] for p in lu["players"]) for lu in entries]
    check("every entry in the batch is genuinely distinct (no exact duplicates)",
          len(signatures) == len(set(signatures)), str(len(set(signatures))))
    check("generate_entries never pairs a pitcher with a hitter from the team he's facing",
          not any(has_opposing_pitcher_hitter_pair(lu) for lu in entries),
          str([lu["players"] for lu in entries if has_opposing_pitcher_hitter_pair(lu)][:1]))

    floored_entries = contest.generate_entries(mul_slate, 15, min_salary=45000, seed=9)
    check("generate_entries' min_salary floors every entry's salary",
          all(lu["salary_used"] >= 45000 for lu in floored_entries),
          str(min(lu["salary_used"] for lu in floored_entries)))

    check("duplicate_count is 1 for every entry under the default distinctness guarantee",
          all(lu["duplicate_count"] == 1 for lu in entries),
          str({lu["duplicate_count"] for lu in entries}))

    # A dedicated single-solution fixture: TA (5 hitters, exactly one
    # per required slot) and TB (3 OF, exactly the 3 OF slots) are in
    # separate games (so each team's own pitcher is always safe to use
    # against the other -- unlike opt_slate, which has only one hitting
    # team and would need all 8 of its hitters, violating contest.py's
    # own MAX_HITTERS_PER_TEAM=5 cap). With zero depth or choice
    # anywhere, every successful attempt can only ever land on the same
    # 10 players -- a deterministic way to prove allow_duplicates
    # actually lets exact repeats through.
    ta_hitters = [
        opt_hitter(9940, "TAC", "TA", "C", 3000, 8),
        opt_hitter(9941, "TA1B", "TA", "1B", 3000, 8),
        opt_hitter(9942, "TA2B", "TA", "2B", 3000, 8),
        opt_hitter(9943, "TA3B", "TA", "3B", 3000, 8),
        opt_hitter(9944, "TASS", "TA", "SS", 3000, 8),
    ]
    tb_hitters = [
        opt_hitter(9950, "TBOF1", "TB", "OF", 3000, 8),
        opt_hitter(9951, "TBOF2", "TB", "OF", 3000, 8),
        opt_hitter(9952, "TBOF3", "TB", "OF", 3000, 8),
    ]
    single_solution_slate = {
        "games": [
            {
                "home": {"abbrev": "TA", "hitters": ta_hitters,
                         "probable_pitcher": opt_pitcher(9960, "TAP", 3000, 10), "scratches": []},
                "away": {"abbrev": "TX", "hitters": [], "probable_pitcher": None, "scratches": []},
            },
            {
                "home": {"abbrev": "TB", "hitters": tb_hitters,
                         "probable_pitcher": opt_pitcher(9961, "TBP", 3000, 10), "scratches": []},
                "away": {"abbrev": "TY", "hitters": [], "probable_pitcher": None, "scratches": []},
            },
        ]
    }

    # THE CONTRACT CHANGED HERE, deliberately: a pool that runs out of
    # distinct builds no longer stops the batch short ("only built 7 of
    # 5,000"). Real small-slate contests duplicate heavily once the
    # distinct lineup space is exhausted, so the generator now fills
    # the batch out with duplicates the way a real field does --
    # duplicates still respect every REAL constraint (salary, exposure,
    # the 5-hitters-per-team cap); only distinctness, which no real
    # contest enforces, is lifted.
    filled = contest.generate_entries(single_solution_slate, 3, seed=1)
    check("a single-solution pool now fills the FULL requested count -- running out of "
          "distinct builds means duplicating like a real contest, not stopping at 1",
          len(filled) == 3, str(len(filled)))
    filled_signatures = {frozenset(p["id"] for p in lu["players"]) for lu in filled}
    check("...all 3 are exact copies of the only legal lineup, each honestly reporting "
          "duplicate_count == 3 (their payouts get tie-split downstream)",
          len(filled_signatures) == 1 and all(lu["duplicate_count"] == 3 for lu in filled),
          str([lu["duplicate_count"] for lu in filled]))

    dupes_allowed = contest.generate_entries(single_solution_slate, 3, allow_duplicates=True, seed=1)
    check("allow_duplicates=True (duplicates permitted from the very start) reaches the same "
          "full count on a single-solution pool",
          len(dupes_allowed) == 3, str(len(dupes_allowed)))

    # An exposure cap that genuinely binds is still an HONEST stop, not
    # something duplicates paper over: with every player capped at ~1
    # appearance and only one legal build existing, the batch stops at
    # 1 rather than duplicating players past their cap.
    capped_batch = contest.generate_entries(
        single_solution_slate, 3, max_exposure_pct=34.0, seed=1,
    )
    check("a binding exposure cap still stops the batch short honestly -- duplicates never "
          "violate a cap the user explicitly set",
          len(capped_batch) == 1, str(len(capped_batch)))

    # The full stack-shape space: with 8 hitter slots and DK's
    # 5-per-team cap, every buildable hitter composition is a partition
    # of <= 8 into parts of 2-5 (singletons aren't constraints).
    # Enumerate that space here independently, so a shape silently
    # missing from STACK_SHAPES is caught as drift -- the original list
    # carried only 9 of the real 17, and the missing mini/scatter
    # shapes are a genuine share of real small-slate fields.
    def _all_shapes(remaining, max_part):
        out = set()
        for part in range(min(max_part, remaining, 5), 1, -1):
            out.add((part,))
            for tail in _all_shapes(remaining - part, part):
                out.add((part,) + tail)
        return out

    expected_shapes = _all_shapes(8, 5)
    actual_shapes = {tuple(sorted(shape, reverse=True)) for shape in contest.STACK_SHAPES}
    check("STACK_SHAPES covers the COMPLETE mathematical shape space -- every partition of "
          "<= 8 hitters into stack groups of 2-5, all seventeen of them, nothing missing "
          "and nothing impossible",
          actual_shapes == expected_shapes,
          f"missing={sorted(expected_shapes - actual_shapes)} extra={sorted(actual_shapes - expected_shapes)}")
    check("no shape is listed twice",
          len(contest.STACK_SHAPES) == len(actual_shapes), str(len(contest.STACK_SHAPES)))
    check("the canonical GPP winners (5-3, 5-2-1) still carry the heaviest weights",
          contest.STACK_SHAPES[0] == [5, 3] and contest.STACK_SHAPES[1] == [5, 2], "")

    # Same sum (153.0) either way -- one lineup with one 90%-owned "auto
    # include" plus 9 barely-owned pieces, the other with all 10 players
    # evenly at 15.3%. Real DFS "product ownership" reasoning (and the
    # AM-GM inequality this log-sum formula follows): a lineup is only
    # at real risk of being exactly replicated when EVERY player in it
    # is simultaneously popular -- one chalk stud surrounded by unique
    # pieces is actually a much LESS duplicable build than one where
    # everything is moderately chalky, even at an identical summed Own%.
    concentrated_picks = [{"ownership_pct": 90.0}] + [{"ownership_pct": 7.0}] * 9
    flat_picks = [{"ownership_pct": 15.3}] * 10
    concentrated_sum = sum(p["ownership_pct"] for p in concentrated_picks)
    flat_sum = sum(p["ownership_pct"] for p in flat_picks)
    check("cumulative (log-product) duplication_risk is a genuinely different signal than summed "
          "total_ownership_pct -- two rosters with an IDENTICAL sum but very different "
          "concentration must show different duplication_risk",
          concentrated_sum == flat_sum
          and contest._duplication_risk(concentrated_picks) != contest._duplication_risk(flat_picks),
          str((concentrated_sum, flat_sum,
               contest._duplication_risk(concentrated_picks), contest._duplication_risk(flat_picks))))
    check("an evenly-chalky lineup (every player moderately owned) shows HIGHER (closer to 0, more "
          "duplicable) risk than one with a single mega-chalk player surrounded by unique pieces, "
          "at the same summed ownership -- the real signal a pure sum can't see",
          contest._duplication_risk(flat_picks) > contest._duplication_risk(concentrated_picks),
          str((contest._duplication_risk(flat_picks), contest._duplication_risk(concentrated_picks))))

    unfiltered_entries = contest.generate_entries(mul_slate, 20, seed=17)
    check("generate_entries always attaches a real duplication_risk to every entry",
          all("duplication_risk" in lu for lu in unfiltered_entries),
          str(unfiltered_entries[0].get("duplication_risk")))

    # Median of a real unfiltered batch's own risk values -- loose enough
    # that roughly half of ordinary random draws should still satisfy
    # it (good retry odds within max_attempts_per_lineup), unlike an
    # arbitrary fixed offset in log space, where even a small-looking
    # subtraction can be an enormous relative tightening.
    sorted_risks = sorted(lu["duplication_risk"] for lu in unfiltered_entries)
    median_cap = sorted_risks[len(sorted_risks) // 2]
    capped_entries = contest.generate_entries(
        mul_slate, 10, seed=17, max_duplication_risk=median_cap, max_attempts_per_lineup=60
    )
    check("max_duplication_risk rejects any entry whose own duplication_risk exceeds the cap -- "
          "every returned entry must be at or under it",
          len(capped_entries) > 0
          and all(lu["duplication_risk"] <= median_cap for lu in capped_entries),
          str([lu["duplication_risk"] for lu in capped_entries]))

    field_with_risk = contest.generate_field(mul_slate, 10, seed=13)
    check("generate_field's synthetic opponent lineups also carry duplication_risk (informational "
          "only -- generate_field never filters on it, real chalk clustering there is intentional)",
          all("duplication_risk" in lu for lu in field_with_risk),
          str(field_with_risk[0].get("duplication_risk")))

    dup_risk_batch = contest.build_contest_entries(mul_slate, "gpp_small", 10, sample_size=50, seed=19)
    check("build_contest_entries's summary reports a real avg_duplication_risk across the batch",
          "avg_duplication_risk" in dup_risk_batch["summary"],
          str(dup_risk_batch["summary"].get("avg_duplication_risk")))

    unweighted_field = contest.generate_field(mul_slate, 40, seed=13)
    avg_entry_points = sum(lu["projected_points"] for lu in entries) / len(entries)
    avg_field_points = sum(lu["projected_points"] for lu in unweighted_field) / len(unweighted_field)
    check("entries (points-weighted) score meaningfully higher on average than the ownership-weighted field",
          avg_entry_points > avg_field_points, str((avg_entry_points, avg_field_points)))

    # The cap is enforced against the REQUESTED count (20), not
    # however many entries the batch actually ends up with -- a tight
    # cap can legitimately return fewer than requested once enough
    # players are excluded to starve a thin position (same "return
    # what we could build" pattern as optimizer.generate_lineups).
    capped = contest.generate_entries(mul_slate, 20, max_exposure_pct=30, seed=4)
    capped_counts: dict[int, int] = {}
    for lu in capped:
        for p in lu["players"]:
            capped_counts[p["id"]] = capped_counts.get(p["id"], 0) + 1
    check("max_exposure_pct caps how often any one player appears, relative to the requested count",
          max(capped_counts.values()) <= max(1, round(0.30 * 20)),
          str((max(capped_counts.values()), len(capped))))

    try:
        contest.generate_entries(mul_slate, 0)
        check("generate_entries rejects num_lineups < 1", False)
    except contest.ContestError:
        check("generate_entries rejects num_lineups < 1", True)

    try:
        contest.generate_entries(mul_slate, contest.MAX_USER_LINEUPS + 1)
        check("generate_entries rejects num_lineups above MAX_USER_LINEUPS", False)
    except contest.ContestError:
        check("generate_entries rejects num_lineups above MAX_USER_LINEUPS", True)

    print("\nContest generator: weighted stack-shape targeting")

    import random as stack_random_module
    from collections import Counter as StackCounter

    # _pick_stack_shape: statistical proof the weighting actually favors
    # the intended order (the shape list's own first entry, 5-3, drawn
    # meaningfully more often than its last, 3-3), not just that some
    # weights happen to exist.
    shape_rng = stack_random_module.Random(99)
    shape_draws = [
        tuple(contest._pick_stack_shape(contest.STACK_SHAPES, contest.STACK_SHAPE_WEIGHTS, shape_rng))
        for _ in range(2000)
    ]
    first_shape_count = shape_draws.count(tuple(contest.STACK_SHAPES[0]))
    last_shape_count = shape_draws.count(tuple(contest.STACK_SHAPES[-1]))
    check("_pick_stack_shape draws the first-listed shape (5-3) meaningfully more often than the last (3-3)",
          first_shape_count > last_shape_count * 2,
          str((first_shape_count, last_shape_count)))
    check("_pick_stack_shape returns None when given an empty shape list (no feasible shape at all)",
          contest._pick_stack_shape([], [], shape_rng) is None)

    # _feasible_stack_shapes: a pool with only 2 real teams can never
    # satisfy a 3-group shape (4-2-2, 3-3-2) no matter how deep those 2
    # teams are -- this is exactly the bug this function exists to
    # prevent (a structurally-impossible shape burning every retry and
    # aborting the whole batch, see mul_slate's own 2-team fixture
    # below for the real-world version of this).
    two_team_pool = {"TEAM_A": [{"id": i} for i in range(10)], "TEAM_B": [{"id": i} for i in range(10, 18)]}
    feasible_shapes, feasible_weights = contest._feasible_stack_shapes(two_team_pool)
    check("_feasible_stack_shapes excludes 3-group shapes when only 2 teams have any hitters",
          [4, 2, 2] not in feasible_shapes and [3, 3, 2] not in feasible_shapes,
          str(feasible_shapes))
    check("_feasible_stack_shapes keeps every 1- and 2-group shape when both teams are deep enough",
          all(shape in feasible_shapes for shape in ([5, 3], [5, 2], [5], [4, 4], [4, 3], [4, 2], [3, 3])),
          str(feasible_shapes))
    check("_feasible_stack_shapes keeps the weight list aligned with the filtered shape list",
          len(feasible_shapes) == len(feasible_weights))

    # With the full shape space, a 3-hitter team legitimately supports
    # the [3] and [2] mini shapes -- the old "excludes everything"
    # premise only held when the smallest listed shape was 3-3.
    thin_pool = {"TEAM_A": [{"id": i} for i in range(3)]}
    thin_shapes, _tw = contest._feasible_stack_shapes(thin_pool)
    check("a 3-hitter single-team pool supports exactly the [3] and [2] mini shapes -- real "
          "builds on the thinnest slates, newly representable",
          sorted(map(tuple, thin_shapes)) == [(2,), (3,)], str(thin_shapes))
    barren_pool = {"TEAM_A": [{"id": 1}]}
    check("_feasible_stack_shapes excludes every shape once even the pool's only team is too "
          "thin for the smallest one -- a 1-hitter team can't stack anything",
          contest._feasible_stack_shapes(barren_pool) == ([], []),
          str(contest._feasible_stack_shapes(barren_pool)))

    # _pick_stack_teams: a direct, deterministic check of the
    # team-assignment step, independent of the shape-selection
    # randomness checked above.
    team_rng = stack_random_module.Random(5)
    team_pool = {
        "HOT": [{"id": i, "team": "HOT", "projected_fpts": 15.0} for i in range(6)],
        "COLD": [{"id": i, "team": "COLD", "projected_fpts": 1.0} for i in range(100, 103)],
    }
    assignment = contest._pick_stack_teams(team_pool, [5, 3], contest._fpts_weight, team_rng)
    check("_pick_stack_teams assigns the largest group to the only team deep enough for it, and the "
          "remaining group to whichever team is left",
          assignment == {"HOT": 5, "COLD": 3}, str(assignment))
    infeasible = contest._pick_stack_teams(team_pool, [5, 4], contest._fpts_weight, team_rng)
    check("_pick_stack_teams returns None when no remaining team has enough hitters left for a group",
          infeasible is None, str(infeasible))

    # End-to-end against mul_slate (only 2 teams have hitters, MUL1 and
    # MUL2 -- exactly the shape this whole feature was built to handle
    # without silently truncating the batch, see the "40" count check
    # above, which failed outright before _feasible_stack_shapes existed).
    target_shape_labels = {"5-3", "5-2", "5", "4-4", "4-3", "4-2-2", "4-2", "3-3-2", "3-3"}
    stack_types = [e["stack_type"] for e in entries]
    check("generate_entries' output favors the intended stack shapes -- most of a real batch's "
          "stack_type lands on one of STACK_SHAPES' own reported labels (some coincidental drift onto "
          "a leftover free pick doubling up is expected and fine)",
          sum(1 for st in stack_types if st in target_shape_labels) >= len(entries) * 0.5,
          str(StackCounter(stack_types)))

    pitcher_teams_used = {p["team"] for lu in entries for p in lu["players"][:2]}
    check("the hitter stack constraint never leaks into the 2 pitcher slots -- pitchers are drawn from "
          "every team in the pool, including MUL4-MUL6, which have no hitters at all",
          bool({"MUL4", "MUL5", "MUL6"} & pitcher_teams_used),
          str(pitcher_teams_used))

    # DraftKings' own roster rule: never more than 5 hitters from one
    # team. A shape's genuine leftover/free picks (e.g. "5"'s 3
    # unconstrained slots) could previously coincide with the
    # already-stacked team and silently build an illegal 6+ stack --
    # checked here across every entry and field lineup in the batches
    # already built above, not a fresh contrived fixture, since the
    # real bug only showed up at realistic scale.
    def _max_hitters_from_one_team(lineups):
        worst = 0
        for lu in lineups:
            counts: dict[str, int] = {}
            for p in lu["players"][2:]:
                counts[p["team"]] = counts.get(p["team"], 0) + 1
            worst = max(worst, max(counts.values()))
        return worst

    check("no generate_entries lineup ever rosters more than 5 hitters from one team",
          _max_hitters_from_one_team(entries) <= contest.MAX_HITTERS_PER_TEAM,
          str(_max_hitters_from_one_team(entries)))
    check("no generate_field lineup ever rosters more than 5 hitters from one team",
          _max_hitters_from_one_team(unweighted_field) <= contest.MAX_HITTERS_PER_TEAM,
          str(_max_hitters_from_one_team(unweighted_field)))

    print("\nContest generator: builds a whole contest, lineups only (build_contest_lineups)")

    # Contest size is ONE control now, not two. Every preset advertises
    # the real sizes it comes in, and its own default has to be one of
    # them or the UI's dropdown would open on a value it can't offer.
    _expected_sizes = {
        "double_up": [50, 100],
        "gpp_small": [100, 500, 999],
        "gpp_mid": [1_000, 2_000, 3_000, 4_000, 5_000],
        "gpp_large": [6_000, 7_000, 8_000, 9_000, 10_000],
        "gpp_milly": [12_500, 15_000, 20_000, 25_000, 50_000, 100_000],
    }
    check("every contest preset advertises the real sizes it comes in",
          {k: v["sizes"] for k, v in contest.CONTEST_TYPES.items()} == _expected_sizes,
          str({k: v.get("sizes") for k, v in contest.CONTEST_TYPES.items()}))
    check("every preset's own default field_size is one of its own selectable sizes -- otherwise "
          "the size dropdown would open on a value it can't offer",
          all(c["field_size"] in c["sizes"] for c in contest.CONTEST_TYPES.values()),
          str([(k, c["field_size"]) for k, c in contest.CONTEST_TYPES.items()]))

    lineups_only = contest.build_contest_lineups(mul_slate, "gpp_small", 100, seed=5)
    check("build_contest_lineups builds exactly as many lineups as the contest holds -- the two "
          "numbers are the same thing now",
          lineups_only["num_entries_built"] == 100 == lineups_only["field_size"],
          str((lineups_only["num_entries_built"], lineups_only["field_size"])))
    check("build_contest_lineups returns NO economics at all -- no opponent field, no payout "
          "curve, no ROI; that's the simulator's job on the batch it produces",
          not any(k in lineups_only for k in ("field", "results", "prize_pool", "paid_count", "field_baseline")),
          str(sorted(lineups_only)))
    check("build_contest_lineups describes what it built instead: salary, points, ownership "
          "and the stack shapes the contest actually came out with",
          lineups_only["summary"]["median_salary_used"] > 0
          and sum(sh["count"] for sh in lineups_only["stack_shapes"]) == 100,
          str(lineups_only["summary"]))
    check("build_contest_lineups allows duplicates unconditionally -- a real contest field "
          "contains them, so it's built in rather than a checkbox",
          lineups_only["num_distinct_entries"] <= lineups_only["num_entries_built"],
          str((lineups_only["num_distinct_entries"], lineups_only["num_entries_built"])))

    # A contest bigger than the build cap: field_size stays the REAL
    # size (every payout and rank downstream keys off it) while the
    # build itself is capped, and the response reports both rather than
    # conflating them. MAX_USER_LINEUPS is patched down so this stays a
    # fast test rather than a 10,000-lineup one.
    _saved_max = contest.MAX_USER_LINEUPS
    contest.MAX_USER_LINEUPS = 25
    try:
        capped = contest.build_contest_lineups(mul_slate, "gpp_small", 500, seed=5)
    finally:
        contest.MAX_USER_LINEUPS = _saved_max
    check("a contest larger than the build cap keeps its REAL field_size while the build itself "
          "is capped -- reported as two separate numbers, not silently conflated",
          capped["field_size"] == 500 and capped["num_entries_built"] == 25,
          str((capped["field_size"], capped["num_entries_built"])))

    try:
        contest.build_contest_lineups(mul_slate, "gpp_small", 0)
        check("build_contest_lineups rejects a contest_size of 0", False)
    except contest.ContestError:
        check("build_contest_lineups rejects a contest_size of 0", True)

    # Salary pacing. There is no salary FLOOR any more -- a hard floor
    # makes whole stack shapes infeasible and stalls a batch -- so
    # spending the cap has to come from steering the sampler while it
    # builds. The real claim being checked is that pacing improves
    # salary AND projected points at once: paying up generally buys a
    # better player, so this is not a trade-off.
    _saved_pacing = contest._SALARY_PACING_STRENGTH
    contest._SALARY_PACING_STRENGTH = 0.0
    try:
        _unpaced = contest.generate_entries(mul_slate, 40, allow_duplicates=True, seed=77)
    finally:
        contest._SALARY_PACING_STRENGTH = _saved_pacing
    _paced = contest.generate_entries(mul_slate, 40, allow_duplicates=True, seed=77)
    _median = lambda es: sorted(e["salary_used"] for e in es)[len(es) // 2]
    _avg_pts = lambda es: sum(e["projected_points"] for e in es) / len(es)
    check("salary pacing raises the median salary actually used versus an unpaced build -- "
          "'use as much of the cap as possible', measurably",
          _median(_paced) > _median(_unpaced), str((_median(_paced), _median(_unpaced))))
    check("...and raises average projected points at the same time, so spending the cap is a "
          "real gain rather than a trade against lineup quality",
          _avg_pts(_paced) > _avg_pts(_unpaced), str((round(_avg_pts(_paced), 2), round(_avg_pts(_unpaced), 2))))
    check("pacing is off entirely at strength 0, so the behaviour it replaced is one constant away",
          contest._SALARY_PACING_STRENGTH > 0, str(contest._SALARY_PACING_STRENGTH))

    print("\nContest generator: mass multi-entry economics (build_contest_entries)")

    # Regression fixture for the double/triple-counting bug: ranking a
    # large batch of individually-strong entries independently against
    # the field (instead of against each other too) let each one claim
    # the same top payout, summing to many times the real prize pool.
    batch = contest.build_contest_entries(mul_slate, "gpp_small", 30, sample_size=100, seed=21)
    check("build_contest_entries builds the requested number of entries",
          batch["num_entries_built"] == 30, str(batch["num_entries_built"]))
    check("no two entries in the batch share the same estimated rank",
          len({r["estimated_rank"] for r in batch["results"]}) == len(batch["results"]),
          str(len(batch["results"])))
    check("total estimated payout across the batch never exceeds the real prize pool",
          batch["summary"]["total_estimated_payout"] <= batch["prize_pool"] + 0.01,
          str((batch["summary"]["total_estimated_payout"], batch["prize_pool"])))
    check("cashing count never exceeds paid_count",
          batch["summary"]["cashing_count"] <= batch["paid_count"], str(batch["summary"]))
    check("build_contest_entries reports exposure across the batch",
          len(batch["exposure"]) > 0, str(batch["exposure"][:1]))
    check("build_contest_entries's summary now carries avg_roi_pct, derived from its own "
          "total_estimated_payout/total_entry_cost",
          batch["summary"]["avg_roi_pct"] == round(
              (batch["summary"]["total_estimated_payout"] / batch["summary"]["total_entry_cost"] - 1) * 100, 1
          ),
          str(batch["summary"]))
    check("build_contest_entries's field_baseline reports gpp_small's real 20% payout_pct and "
          "-RAKE_PCT*100% avg_roi_pct, the fast/deterministic mode's own zero-skill reference point",
          batch["field_baseline"] == {"avg_cash_probability_pct": 20.0, "avg_roi_pct": -contest.RAKE_PCT * 100},
          str(batch["field_baseline"]))

    try:
        contest.build_contest_entries(mul_slate, "double_up", 200)
        check("build_contest_entries rejects num_lineups above the contest's field_size", False)
    except contest.ContestError:
        check("build_contest_entries rejects num_lineups above the contest's field_size", True)

    try:
        contest.build_contest_entries(mul_slate, "gpp_small", 0)
        check("build_contest_entries rejects num_lineups < 1", False)
    except contest.ContestError:
        check("build_contest_entries rejects num_lineups < 1", True)

    print("\nContest generator: default one-off slot quality preference")

    # A minimal, fully-controlled candidates_by_slot (built directly,
    # bypassing build_player_pool) with exactly one feasible stack team
    # (STK, exactly 5 hitters -- DK's own MAX_HITTERS_PER_TEAM cap, and
    # no ambiguity in _pick_stack_teams' random team choice) and a 2nd
    # team (MISC) offering 2 "junk" (cheap, low fpts, HIGH ownership)
    # and 2 "premium" (expensive, high fpts, LOW ownership) OF options
    # for the 3 leftover one-off OF slots (leftover = 8 - 5 = 3,
    # required = min(2, 3) = 2). Ownership-weighted sampling (the
    # field's own weight_fn) would prefer junk by a wide margin on
    # ownership alone -- proving the quality restriction is a real,
    # ownership-blind override, not just something high-fpts sampling
    # would have done anyway.
    def cq(pid, name, team, salary, fpts, own):
        return {
            "id": pid, "name": name, "team": team, "salary": salary,
            "projected_fpts": fpts, "ownership_pct": own, "opponent": "",
        }

    quality_candidates_by_slot = {
        "P": [cq(9970, "PA", "PTEAM1", 3000, 10, 5), cq(9971, "PB", "PTEAM2", 3000, 10, 5)],
        "C": [cq(9972, "C1", "STK", 3000, 8, 5)],
        "1B": [cq(9973, "1B1", "STK", 3000, 8, 5)],
        "2B": [cq(9974, "2B1", "STK", 3000, 8, 5)],
        "3B": [cq(9975, "3B1", "STK", 3000, 8, 5)],
        "SS": [cq(9976, "SS1", "STK", 3000, 8, 5)],
        "OF": [
            cq(9980, "JUNK1", "MISC", 2000, 3, 80),
            cq(9981, "JUNK2", "MISC", 2000, 3, 80),
            cq(9982, "PREM1", "MISC", 9000, 9, 1),
            cq(9983, "PREM2", "MISC", 9000, 9, 1),
        ],
    }
    quality_slot_order = ["P", "P", "C", "1B", "2B", "3B", "SS", "OF", "OF", "OF"]
    quality_team_pools = contest._team_hitter_pools(quality_candidates_by_slot, quality_slot_order)
    quality_ids = contest._one_off_quality_ids(quality_candidates_by_slot)
    check("_one_off_quality_ids correctly identifies the premium OF pair, not the junk pair",
          quality_ids >= {9982, 9983} and not ({9980, 9981} & quality_ids), str(quality_ids))

    junk_ids = {9980, 9981}
    prem_ids = {9982, 9983}

    no_pref_rng = random.Random(3)
    junk_seen = False
    for _ in range(30):
        lu = contest._sample_one_lineup(
            quality_candidates_by_slot, quality_slot_order, no_pref_rng, contest._ownership_weight,
            team_hitter_pools=quality_team_pools, stack_groups=[5],
        )
        if lu and (junk_ids & lu["player_ids"]):
            junk_seen = True
            break
    check("without the preference, ownership-weighted sampling picks junk OF (chalk) at least sometimes",
          junk_seen, f"junk_seen={junk_seen}")

    pref_rng = random.Random(3)
    for _ in range(10):
        lu = contest._sample_one_lineup(
            quality_candidates_by_slot, quality_slot_order, pref_rng, contest._ownership_weight,
            team_hitter_pools=quality_team_pools, stack_groups=[5], one_off_quality_ids=quality_ids,
        )
        check("with the preference active, at least 2 of the 3 leftover OF slots are premium",
              lu is not None and len(prem_ids & lu["player_ids"]) >= 2,
              str(lu["player_ids"] if lu else None))

    print("\nContest generator: duplicate lineups split their combined payout")

    # Direct unit test of the redistribution logic: 3 entries where the
    # first 2 are exact duplicates (same 10 players) and the 3rd is
    # different. Simulates what the rank-assignment walk would have
    # already produced -- consecutive individual payouts for the tied
    # pair (as if each claimed its own adjacent rank) -- and confirms
    # averaging reproduces exactly what DK's real tie-splitting rule
    # would pay each duplicate: the combined value of the ranks they
    # occupy, split evenly.
    dup_players = [{"id": pid} for pid in range(100, 110)]
    other_players = [{"id": pid} for pid in range(200, 210)]
    split_entries = [
        {"players": dup_players},
        {"players": dup_players},
        {"players": other_players},
    ]
    split_results = [
        {"estimated_payout": 40.0, "estimated_profit": 30.0},
        {"estimated_payout": 35.0, "estimated_profit": 25.0},
        {"estimated_payout": 10.0, "estimated_profit": 0.0},
    ]
    contest._split_duplicate_payouts(split_entries, split_results, ["estimated_payout", "estimated_profit"])
    check("duplicate entries split their combined payout evenly (DK's real tie-payout rule)",
          split_results[0]["estimated_payout"] == 37.5 and split_results[1]["estimated_payout"] == 37.5,
          str(split_results[:2]))
    check("...and their profit the same way",
          split_results[0]["estimated_profit"] == 27.5 and split_results[1]["estimated_profit"] == 27.5,
          str(split_results[:2]))
    check("the non-duplicate entry's own payout is untouched",
          split_results[2]["estimated_payout"] == 10.0 and split_results[2]["estimated_profit"] == 0.0,
          str(split_results[2]))

    print("\nContest generator: per-player projected_fpts/ownership_pct on each entry")

    enriched = contest.generate_entries(mul_slate, 5, seed=17)
    check("each entry's players carry their own projected_fpts and ownership_pct",
          all("projected_fpts" in p and "ownership_pct" in p for lu in enriched for p in lu["players"]),
          str(enriched[0]["players"][0]))

    print("\nStack shape/teams derivation (lineup_export.stack_info)")

    five_three = {
        "players": [
            {"name": "P1", "team": "BOS"}, {"name": "P2", "team": "BOS"},
            {"name": "C1", "team": "NYY"}, {"name": "H1", "team": "NYY"},
            {"name": "H2", "team": "NYY"}, {"name": "H3", "team": "NYY"},
            {"name": "H4", "team": "NYY"}, {"name": "H5", "team": "ATL"},
            {"name": "H6", "team": "ATL"}, {"name": "H7", "team": "ATL"},
        ]
    }
    check("stack_info reads a 5-3 stack correctly, primary team first, pitchers excluded",
          lineup_export.stack_info(five_three) == ("5-3", "NYY,ATL"),
          str(lineup_export.stack_info(five_three)))

    tied_stack = {
        "players": [
            {"name": "P1", "team": "BOS"}, {"name": "P2", "team": "BOS"},
            {"name": "H1", "team": "ATL"}, {"name": "H2", "team": "ATL"},
            {"name": "H3", "team": "ATL"}, {"name": "H4", "team": "ATL"},
            {"name": "H5", "team": "NYY"}, {"name": "H6", "team": "NYY"},
            {"name": "H7", "team": "NYY"}, {"name": "H8", "team": "NYY"},
        ]
    }
    check("stack_info breaks a tied group size by first appearance in roster order (4-4, ATL before NYY)",
          lineup_export.stack_info(tied_stack) == ("4-4", "ATL,NYY"),
          str(lineup_export.stack_info(tied_stack)))

    no_stack = {"players": [{"name": f"p{i}", "team": f"T{i}"} for i in range(10)]}
    check("stack_info reports no stack (empty strings) when no team supplies 2+ hitters",
          lineup_export.stack_info(no_stack) == ("", ""), str(lineup_export.stack_info(no_stack)))

    print("\nLineup CSV export (for handing a batch off to an external simulator)")

    import csv as csv_module
    import io as io_module

    opt_lineup = optimizer.generate_lineups(mul_slate)["lineups"][0]
    opt_csv = lineup_export.lineups_to_csv([opt_lineup])
    opt_csv_rows = list(csv_module.DictReader(io_module.StringIO(opt_csv)))
    check("lineups_to_csv produces one data row per optimizer lineup",
          len(opt_csv_rows) == 1, str(len(opt_csv_rows)))
    check("lineups_to_csv has one name column per DK roster slot (P1/P2/C/1B/2B/3B/SS/OF1/OF2/OF3)",
          all(f"{label}_name" in opt_csv_rows[0] for label in lineup_export.SLOT_LABELS),
          str(sorted(opt_csv_rows[0].keys())))
    check("lineups_to_csv carries per-player salary/fpts sub-columns (slot-labeled) but not team/own_pct",
          all(f"{label}_salary" in opt_csv_rows[0] and f"{label}_fpts" in opt_csv_rows[0]
              for label in lineup_export.SLOT_LABELS)
          and not any(k.endswith(("_team", "_own_pct")) for k in opt_csv_rows[0]),
          str(sorted(opt_csv_rows[0].keys())))
    p1_name_in_csv = opt_csv_rows[0]["P1_name"]
    p1_name_in_lineup = opt_lineup["slots"]["P"][0]["name"]
    check("the optimizer's grouped `slots` shape flattens into the CSV in the right roster order",
          p1_name_in_csv == p1_name_in_lineup, str((p1_name_in_csv, p1_name_in_lineup)))
    p1_player_in_lineup = opt_lineup["slots"]["P"][0]
    check("P1_salary/P1_fpts in the CSV match the actual P1 player's salary/projected_fpts",
          (float(opt_csv_rows[0]["P1_salary"]), float(opt_csv_rows[0]["P1_fpts"]))
          == (float(p1_player_in_lineup["salary"]), float(p1_player_in_lineup["projected_fpts"])),
          str((opt_csv_rows[0]["P1_salary"], opt_csv_rows[0]["P1_fpts"],
               p1_player_in_lineup["salary"], p1_player_in_lineup["projected_fpts"])))
    check("lineup-level totals (salary_used, projected_points) are carried into the CSV row",
          float(opt_csv_rows[0]["salary_used"]) == opt_lineup["salary_used"], opt_csv_rows[0]["salary_used"])
    opt_expected_stack_type, opt_expected_stack = lineup_export.stack_info(opt_lineup)
    check("lineups_to_csv's stack_type/stack columns match stack_info() computed directly on the lineup",
          (opt_csv_rows[0]["stack_type"].lstrip("'"), opt_csv_rows[0]["stack"]) == (opt_expected_stack_type, opt_expected_stack),
          str(((opt_csv_rows[0]["stack_type"], opt_csv_rows[0]["stack"]), (opt_expected_stack_type, opt_expected_stack))))
    check("a non-empty CSV stack_type gets a leading apostrophe so Excel doesn't misread it as a date "
          "(e.g. '5-3' -> March 5th)",
          not opt_expected_stack_type or opt_csv_rows[0]["stack_type"].startswith("'"),
          opt_csv_rows[0]["stack_type"])

    batch_for_csv = contest.build_contest_entries(mul_slate, "gpp_small", 5, sample_size=50, seed=23)
    check("contest-generator entries carry their own stack_type/stack fields, matching stack_info()",
          all(
              (e["stack_type"], e["stack"]) == lineup_export.stack_info(e)
              for e in batch_for_csv["entries"]
          ),
          str([(e["stack_type"], e["stack"]) for e in batch_for_csv["entries"][:3]]))
    entries_csv = lineup_export.lineups_to_csv(batch_for_csv["entries"], results=batch_for_csv["results"])
    entries_csv_rows = list(csv_module.DictReader(io_module.StringIO(entries_csv)))
    check("lineups_to_csv produces one data row per contest-generator entry",
          len(entries_csv_rows) == 5, str(len(entries_csv_rows)))
    check("the contest generator's flat `players` shape is already in roster order, no reordering needed",
          entries_csv_rows[0]["P1_name"] == batch_for_csv["entries"][0]["players"][0]["name"])
    check("the CSV's stack_type/stack columns for a contest-generator entry match its own attached fields "
          "(modulo the CSV's leading-apostrophe Excel escape)",
          (entries_csv_rows[0]["stack_type"].lstrip("'"), entries_csv_rows[0]["stack"])
          == (batch_for_csv["entries"][0]["stack_type"], batch_for_csv["entries"][0]["stack"]),
          str((entries_csv_rows[0]["stack_type"], entries_csv_rows[0]["stack"])))
    check("results (rank/cash/payout), when given, are appended as extra columns",
          "estimated_rank" in entries_csv_rows[0] and "in_the_money" in entries_csv_rows[0]
          and "estimated_payout" in entries_csv_rows[0],
          str(entries_csv_rows[0]))
    check("estimated_rank in the CSV matches the JSON response's own results list",
          int(entries_csv_rows[0]["estimated_rank"]) == batch_for_csv["results"][0]["estimated_rank"])

    no_results_csv = lineup_export.lineups_to_csv([opt_lineup])
    check("lineups_to_csv omits the rank/cash/payout columns when no results are given",
          "estimated_rank" not in list(csv_module.DictReader(io_module.StringIO(no_results_csv)))[0])

    print("\nDK points from a single game's box score (mlb_dk_points.py)")

    # Real rows, fetched live from MLB Stats API's gameLog endpoint
    # (Shohei Ohtani, 2024-03-20; Gerrit Cole, 2024-06-19) -- hand
    # totaled against DK's own scoring rules, not synthetic numbers,
    # so this proves the formula against a real box score's actual
    # field shape, not just arithmetic in isolation.
    ohtani_game = {
        "hits": 2, "doubles": 0, "triples": 0, "home_runs": 0,
        "rbi": 1, "runs": 0, "walks": 0, "hit_by_pitch": 0, "stolen_bases": 1,
    }
    check("hitter_game_points matches a real box score (2 singles, 1 RBI, 1 SB)",
          mlb_dk_points.hitter_game_points(ohtani_game) == 13.0,
          str(mlb_dk_points.hitter_game_points(ohtani_game)))

    cole_game = {
        "outs": 12, "strikeouts": 5, "wins": 0, "earned_runs": 2,
        "hits_against": 3, "walks_against": 1, "hit_batsmen": 0,
        "complete_games": 0, "shutouts": 0,
    }
    check("pitcher_game_points matches a real box score (4.0 IP, 5 K, 2 ER)",
          mlb_dk_points.pitcher_game_points(cole_game) == 12.6,
          str(mlb_dk_points.pitcher_game_points(cole_game)))

    hr_game = {
        "hits": 4, "doubles": 1, "triples": 0, "home_runs": 1,
        "rbi": 4, "runs": 2, "walks": 1, "hit_by_pitch": 0, "stolen_bases": 0,
    }
    # 2 singles(6) + 1 double(5) + 1 HR(10) + 4 RBI(8) + 2 R(4) + 1 BB(2) = 35
    check("hitter_game_points derives singles by subtraction (hits - 2b - 3b - HR)",
          mlb_dk_points.hitter_game_points(hr_game) == 35.0,
          str(mlb_dk_points.hitter_game_points(hr_game)))

    no_hitter_game = {
        "outs": 27, "strikeouts": 10, "wins": 1, "earned_runs": 0,
        "hits_against": 0, "walks_against": 1, "hit_batsmen": 0,
        "complete_games": 1, "shutouts": 1,
    }
    # 27*0.75(20.25) + 10K(20) + 1W(4) - 1BB(0.6) + CG(2.5) + CGSO(2.5) + no-hitter(5) = 53.65
    check("pitcher_game_points stacks CG + CGSO + no-hitter bonuses correctly",
          mlb_dk_points.pitcher_game_points(no_hitter_game) == 53.65,
          str(mlb_dk_points.pitcher_game_points(no_hitter_game)))

    routine_start = dict(no_hitter_game, hits_against=6, complete_games=0, shutouts=0)
    check("no CG/CGSO/no-hitter bonus for a routine (non-complete-game) start",
          mlb_dk_points.pitcher_game_points(routine_start)
          == round(27 * 0.75 + 10 * 2 + 1 * 4 - 0 * 2 - 6 * 0.6 - 1 * 0.6 - 0 * 0.6, 2),
          str(mlb_dk_points.pitcher_game_points(routine_start)))

    print("\nPer-player outcome distribution pools (variance.py)")

    import statistics as statistics_module

    VARIANCE_SEASON = 2099

    def _rbi_game(rbi):
        return {
            "plate_appearances": 1, "hits": 0, "doubles": 0, "triples": 0,
            "home_runs": 0, "rbi": rbi, "runs": 0, "walks": 0,
            "hit_by_pitch": 0, "stolen_bases": 0,
        }

    # 60 games each (comfortably above MIN_GAMES_FULL_TRUST["hitter"]),
    # identical season-average DK points (10.0), deliberately different
    # game-to-game spread -- isolates the variance-capturing behavior
    # from everything else.
    consistent_games = [_rbi_game(r) for r in ([5, 4, 6, 5] * 15)]  # DK pts in {8,10,12}
    boom_bust_games = [_rbi_game(r) for r in ([0, 10] * 30)]  # DK pts in {0,20}
    thin_sample_games = [_rbi_game(5) for _ in range(3)]  # only 3 games, DK pts = 10 each

    variance_game_logs = {
        90001: consistent_games,
        90002: boom_bust_games,
        90003: thin_sample_games,
        90005: consistent_games,
    }

    async def fake_variance_game_log(player_id, season, group="hitting"):
        return variance_game_logs.get(player_id, [])

    mlb.get_player_game_log = fake_variance_game_log

    consistent_pool = await variance.player_outcome_pool(90001, "OF", VARIANCE_SEASON, seed=1)
    boom_bust_pool = await variance.player_outcome_pool(90002, "OF", VARIANCE_SEASON, seed=1)

    check("player_outcome_pool returns POOL_SIZE values",
          len(consistent_pool) == variance.POOL_SIZE, str(len(consistent_pool)))
    check("both pools have roughly the same mean (same underlying season average)",
          abs(statistics_module.mean(consistent_pool) - statistics_module.mean(boom_bust_pool)) < 1.0,
          str((statistics_module.mean(consistent_pool), statistics_module.mean(boom_bust_pool))))
    check("the boom/bust player's pool has genuinely higher variance than the consistent player's",
          statistics_module.pstdev(boom_bust_pool) > 3 * statistics_module.pstdev(consistent_pool),
          str((statistics_module.pstdev(consistent_pool), statistics_module.pstdev(boom_bust_pool))))
    check("a player with games well above MIN_GAMES_FULL_TRUST draws entirely from his own history",
          set(consistent_pool) <= {8.0, 10.0, 12.0}, str(set(consistent_pool)))

    # By now the shared "OF" position pool has been warmed up by the
    # two full-season players above (each call contributes its own
    # games), so a thin-sample player's pool should blend in values
    # beyond his own 3 games rather than being stuck at exactly {10.0}.
    thin_pool = await variance.player_outcome_pool(90003, "OF", VARIANCE_SEASON, seed=2)
    check("a thin-sample player's pool blends in values from the warmed-up shared position pool",
          not (set(thin_pool) <= {10.0}), str(sorted(set(thin_pool)))[:200])

    # A player with literally zero games this season (hasn't debuted
    # yet) falls back entirely to the shared position pool rather than
    # an empty or fabricated result.
    no_games_pool = await variance.player_outcome_pool(90004, "OF", VARIANCE_SEASON, seed=3)
    check("a player with zero games this season falls back to the shared position pool",
          len(no_games_pool) > 0, str(len(no_games_pool)))

    first_call = await variance.player_outcome_pool(90005, "OF", VARIANCE_SEASON, seed=42)
    second_call = await variance.player_outcome_pool(90005, "OF", VARIANCE_SEASON, seed=7)
    check("player_outcome_pool is cached -- calling again returns the same pool, ignoring a new seed",
          first_call == second_call, str((len(first_call), len(second_call))))

    print("\nas_of_date backtesting cutoff (variance.py)")

    # 60 games before the cutoff (well above MIN_GAMES_FULL_TRUST, DK
    # points in {8,10,12} same as consistent_games above) plus 10 games
    # ON/AFTER the cutoff with a deliberately unmistakable DK value (50,
    # rbi=25) -- as_of_date's whole job is making sure a backtest for a
    # real past date can never see games that hadn't happened yet.
    asof_dates_before = [f"2099-04-{(i % 28) + 1:02d}" for i in range(60)]
    asof_before_games = [
        {**_rbi_game(r), "date": d} for r, d in zip([5, 4, 6, 5] * 15, asof_dates_before)
    ]
    asof_future_games = [
        {**_rbi_game(25), "date": f"2099-08-{(i % 28) + 1:02d}"} for i in range(10)
    ]
    variance_game_logs[90006] = asof_before_games + asof_future_games

    # Order matters here: check the shared-pool-contribution behavior
    # BEFORE ever making a plain (non-as_of_date) call for this player,
    # since that call legitimately DOES contribute -- calling it first
    # would pollute the "before" baseline with the very value being
    # checked for.
    pool_before = set(variance.position_pool("OF", VARIANCE_SEASON))
    asof_pool = await variance.player_outcome_pool(
        90006, "OF", VARIANCE_SEASON, seed=1, as_of_date="2099-08-01"
    )
    check("as_of_date excludes every game on/after the cutoff -- the pool draws only from the "
          "pre-cutoff 8/10/12 games, never the future 50.0 outlier",
          set(asof_pool) <= {8.0, 10.0, 12.0}, str(sorted(set(asof_pool))))

    pool_after_asof_call = set(variance.position_pool("OF", VARIANCE_SEASON))
    check("an as_of_date call does NOT contribute its games to the shared same-position pool -- "
          "a backtest re-deriving a past snapshot shouldn't mutate live shared state other "
          "present-day requests draw from",
          50.0 not in pool_after_asof_call, str(sorted(pool_after_asof_call - pool_before)))

    full_pool_90006 = await variance.player_outcome_pool(90006, "OF", VARIANCE_SEASON, seed=1)
    check("without as_of_date (the live app's real behavior), the same player's pool DOES "
          "include the later games -- proof the cutoff isn't silently applied everywhere",
          50.0 in full_pool_90006, str(sorted(set(full_pool_90006))))
    check("as_of_date's cache key is scoped separately from the plain call -- the two pools "
          "above didn't collide with (or overwrite) each other",
          set(asof_pool) != set(full_pool_90006), "")

    pool_after_full_call = set(variance.position_pool("OF", VARIANCE_SEASON))
    check("a plain (no as_of_date) call still DOES contribute to the shared same-position pool, "
          "same as every other real live request",
          50.0 in pool_after_full_call, "")

    print("\nTeam correlation: stacked lineups show higher variance than unstacked (variance.py)")

    import random as random_module

    check("sample_correlated_outcome leans toward the top of a pool when the multiplier is high",
          variance.sample_correlated_outcome(
              [1.0, 5.0, 10.0, 15.0, 20.0], random_module.Random(1), team_multiplier=2.0
          )
          >= variance.sample_correlated_outcome(
              [1.0, 5.0, 10.0, 15.0, 20.0], random_module.Random(1), team_multiplier=0.3
          ))
    check("team_environment_multiplier stays within its clamped bounds",
          all(
              variance.TEAM_MULTIPLIER_MIN
              <= variance.team_environment_multiplier(random_module.Random(seed))
              <= variance.TEAM_MULTIPLIER_MAX
              for seed in range(500)
          ))

    # Reuse the same "consistent" 60-game fixture (mean 10.0, DK pts in
    # {8,10,12}) for all 8 synthetic hitters -- isolates the
    # correlation mechanism's effect on lineup-level variance from any
    # difference in individual player variance.
    correlation_game_logs = {pid: consistent_games for pid in range(92001, 92009)}

    async def fake_correlation_game_log(player_id, season, group="hitting"):
        return correlation_game_logs.get(player_id, [])

    mlb.get_player_game_log = fake_correlation_game_log

    stack_pools = [
        sorted(await variance.player_outcome_pool(pid, "OF", VARIANCE_SEASON, seed=pid))
        for pid in range(92001, 92005)  # 4 hitters, all sharing one team's multiplier
    ]
    unstacked_pools = [
        sorted(await variance.player_outcome_pool(pid, "OF", VARIANCE_SEASON, seed=pid))
        for pid in range(92005, 92009)  # 4 hitters, each on a different team
    ]

    corr_rng = random_module.Random(777)
    NUM_CORRELATION_TRIALS = 3000
    stacked_totals = []
    unstacked_totals = []
    for _ in range(NUM_CORRELATION_TRIALS):
        team_a_multiplier = variance.team_environment_multiplier(corr_rng)
        stacked_totals.append(
            sum(
                variance.sample_correlated_outcome(pool, corr_rng, team_multiplier=team_a_multiplier)
                for pool in stack_pools
            )
        )
        unstacked_totals.append(
            sum(
                variance.sample_correlated_outcome(
                    pool, corr_rng, team_multiplier=variance.team_environment_multiplier(corr_rng)
                )
                for pool in unstacked_pools
            )
        )

    stacked_mean = statistics_module.mean(stacked_totals)
    unstacked_mean = statistics_module.mean(unstacked_totals)
    stacked_stdev = statistics_module.pstdev(stacked_totals)
    unstacked_stdev = statistics_module.pstdev(unstacked_totals)

    check("a stacked lineup (shared team multiplier) and an unstacked one land at roughly the same mean",
          abs(stacked_mean - unstacked_mean) < 2.0, str((round(stacked_mean, 2), round(unstacked_mean, 2))))
    # The bounds are the CALIBRATED physics, not a vibe: at the measured
    # real-world teammate correlation (+0.10, from 294 real 2026
    # teammate pairs), a 4-man stack's variance factor is
    # 1 + 3*rho ~ 1.36, i.e. a stdev ratio of ~1.17. The old sampler
    # produced a ratio well above 1.3 because its correlation ran FIVE
    # TIMES reality -- the upper bound guards against that regression
    # just as much as the lower bound guards the correlation existing.
    _stack_ratio = stacked_stdev / unstacked_stdev
    check("a stacked lineup shows genuinely higher variance than an equivalent unstacked one -- "
          "the whole point of correlation existing",
          _stack_ratio > 1.06, str(round(_stack_ratio, 3)))
    check("...but NOT the runaway correlation the old sampler had -- a 4-man stack at the "
          "real-world +0.10 teammate correlation is ~1.17x, nowhere near the 1.5x+ the "
          "5x-inflated version produced",
          _stack_ratio < 1.35, str(round(_stack_ratio, 3)))

    print("\nSim calibration: projection recentering + exact copula marginals")

    # recenter_pool: the sim now centers every player on TODAY'S
    # projection (the industry-standard construction -- reference sims
    # use Gamma(mean=projection); ours keeps the empirical shape and
    # rescales it), instead of his raw historical pool level.
    _rc = variance.recenter_pool([0.0, 5.0, 10.0, 15.0, 20.0], 15.0)
    check("recenter_pool rescales a pool so its mean IS today's projection",
          abs(sum(_rc) / len(_rc) - 15.0) < 1e-9, str(_rc))
    check("...multiplicatively, so zeros stay zeros and the shape scales with the level",
          _rc[0] == 0.0 and _rc[-1] == 30.0, str(_rc))
    check("recenter_pool clamps the scale so a degenerate projection can't stretch a pool "
          "into nonsense",
          max(variance.recenter_pool([1.0, 2.0, 3.0], 50.0)) == 3.0 * variance._RECENTER_SCALE_MAX,
          str(variance.recenter_pool([1.0, 2.0, 3.0], 50.0)))
    check("an all-zero/near-zero pool is left alone -- scaling zeros can't reach any projection",
          variance.recenter_pool([0.0, 0.0, 0.5], 8.0) == [0.0, 0.0, 0.5], "")
    check("no projection means no rescale -- the raw pool is the honest fallback",
          variance.recenter_pool([2.0, 4.0], None) == [2.0, 4.0], "")

    # Copula marginal exactness: the OLD percentile-target-plus-jitter
    # sampler center-biased every player's own distribution once the
    # shared shift was calibrated down to reality. The Gaussian copula
    # produces an exactly uniform rank, so the sampled distribution IS
    # the pool, at any correlation strength.
    _cop_pool = sorted([0.0, 0.0, 3.0, 5.0, 8.0, 12.0, 18.0, 25.0, 32.0, 40.0] * 20)
    _cop_rng = random_module.Random(99)
    _draws = []
    for _ in range(6000):
        _tm = variance.team_environment_multiplier(_cop_rng)
        _draws.append(variance.sample_correlated_outcome(_cop_pool, _cop_rng, team_multiplier=_tm))
    _pool_mean = sum(_cop_pool) / len(_cop_pool)
    _draw_mean = sum(_draws) / len(_draws)
    _pool_sd = statistics_module.pstdev(_cop_pool)
    _draw_sd = statistics_module.pstdev(_draws)
    check("the copula sampler reproduces a player's own pool marginally under full team "
          "correlation -- mean within 3%",
          abs(_draw_mean - _pool_mean) / _pool_mean < 0.03, str((round(_pool_mean, 2), round(_draw_mean, 2))))
    check("...and stdev within 4% -- the old sampler thinned every player's tails once its "
          "shared shift was calibrated down, which silently understated individual variance",
          abs(_draw_sd - _pool_sd) / _pool_sd < 0.04, str((round(_pool_sd, 2), round(_draw_sd, 2))))

    # player_pools_for_entries recenters on each entry's own projected_fpts.
    # A full 10-man entry: player_pools_for_entries reads each player's
    # position off his roster SLOT, and the first two slots are always
    # the pitchers -- the two players under test must sit in hitter
    # slots to get hitter pools.
    _rc_ids = [93811, 93812]
    def _rc_game():
        # one single + an RBI + a run = 7.0 DK points, every game
        return {"plate_appearances": 4, "hits": 1, "doubles": 0, "triples": 0,
                "home_runs": 0, "rbi": 1, "runs": 1, "walks": 0, "hit_by_pitch": 0,
                "stolen_bases": 0}

    correlation_game_logs_rc = {pid: [_rc_game() for _ in range(60)]
                                for pid in _rc_ids + list(range(93820, 93828))}

    async def fake_rc_game_log(player_id, season, group="hitting"):
        return correlation_game_logs_rc.get(player_id, [])

    _saved_log_fn = mlb.get_player_game_log
    mlb.get_player_game_log = fake_rc_game_log
    def _rc_p(pid, proj):
        return {"id": pid, "name": f"F{pid}", "team": "AAA", "salary": 4000,
                "projected_fpts": proj, "ownership_pct": 0}

    _rc_filler = [_rc_p(93820 + i, None) for i in range(8)]
    _rc_entries = [{
        "players": _rc_filler[:2]  # land in the two pitcher slots
        + [_rc_p(93811, 14.0), _rc_p(93812, None)]
        + _rc_filler[2:],
    }]
    _rc_pools = await variance.player_pools_for_entries(_rc_entries, VARIANCE_SEASON)
    mlb.get_player_game_log = _saved_log_fn
    _m1 = sum(_rc_pools[93811]) / len(_rc_pools[93811])
    _m2 = sum(_rc_pools[93812]) / len(_rc_pools[93812])
    check("player_pools_for_entries recenters each pool on that player's own projected_fpts -- "
          "the level the builder and the field are both driven by",
          abs(_m1 - 14.0) < 0.75, str(round(_m1, 2)))
    check("...and leaves a player with no projection on his raw historical level",
          abs(_m2 - 7.0) < 0.75, str(round(_m2, 2)))

    print("\nMonte Carlo simulation engine (variance.py simulate_batch)")

    def _sim_player(pid, team):
        return {
            "id": pid, "name": f"P{pid}", "team": team, "salary": 5000,
            "projected_fpts": 10.0, "ownership_pct": 0,
        }

    def _sim_entry_flat(player_ids, team):
        return {
            "salary_used": 50000, "projected_points": 100.0, "total_ownership_pct": 0.0,
            "players": [_sim_player(pid, team) for pid in player_ids],
        }

    def _sim_entry_slots(player_ids, team):
        pitchers, hitters = player_ids[:2], player_ids[2:]
        return {
            "salary_used": 50000, "projected_points": 100.0, "total_ownership_pct": 0.0,
            "slots": {
                "P": [_sim_player(pid, team) for pid in pitchers],
                "C": [_sim_player(hitters[0], team)],
                "1B": [_sim_player(hitters[1], team)],
                "2B": [_sim_player(hitters[2], team)],
                "3B": [_sim_player(hitters[3], team)],
                "SS": [_sim_player(hitters[4], team)],
                "OF": [_sim_player(pid, team) for pid in hitters[5:8]],
            },
        }

    tight_ids = list(range(93001, 93011))
    wide_ids = list(range(94001, 94011))
    sim_pools = {}
    for pid in tight_ids:
        sim_pools[pid] = [8.0, 10.0, 12.0] * 67  # mean ~10, low variance
    for pid in wide_ids:
        sim_pools[pid] = [0.0, 20.0] * 100  # mean 10, high variance

    tight_entry = _sim_entry_flat(tight_ids, "TIGHTTEAM")
    wide_entry = _sim_entry_flat(wide_ids, "WIDETEAM")

    sim = variance.simulate_batch([tight_entry, wide_entry], sim_pools, num_trials=2000, seed=99)
    tight_totals, wide_totals = sim[0], sim[1]

    check("simulate_batch returns one row of simulated totals per entry, num_trials columns each",
          sim.shape == (2, 2000), str(sim.shape))
    check("a tight (low-variance) lineup and a wide (high-variance) one land at roughly the same simulated mean",
          abs(float(tight_totals.mean()) - float(wide_totals.mean())) < 15.0,
          str((round(float(tight_totals.mean()), 2), round(float(wide_totals.mean()), 2))))
    check("a lineup built from high-variance players shows a genuinely wider simulated distribution than an equivalent low-variance one -- proof this is modeling variance, not just re-deriving the mean",
          float(wide_totals.std()) > 3 * float(tight_totals.std()),
          str((round(float(tight_totals.std()), 2), round(float(wide_totals.std()), 2))))

    flat_sim = variance.simulate_batch([_sim_entry_flat(tight_ids, "TIGHTTEAM")], sim_pools, num_trials=500, seed=55)
    slots_sim = variance.simulate_batch([_sim_entry_slots(tight_ids, "TIGHTTEAM")], sim_pools, num_trials=500, seed=55)
    check("simulate_batch treats optimizer.py's `slots`-grouped shape and contest.py's flat `players` shape identically",
          (flat_sim == slots_sim).all(), str((flat_sim[0][:3].tolist(), slots_sim[0][:3].tolist())))

    try:
        variance.simulate_batch([tight_entry], {}, num_trials=10, seed=1)
        check("simulate_batch raises a clear error when a player's outcome pool is missing", False, "no exception raised")
    except ValueError as exc:
        check("simulate_batch raises a clear error when a player's outcome pool is missing",
              "outcome pool" in str(exc), str(exc))

    print("\nMatchup conditioning + pitcher/opponent anti-correlation (variance.py)")

    import numpy as np_module

    def _sim_player_full(pid, team, *, opponent=None, edge=None):
        return {
            "id": pid, "name": f"P{pid}", "team": team, "opponent": opponent,
            "salary": 5000, "projected_fpts": 10.0, "ownership_pct": 0,
            "edge_composite": edge,
        }

    def _sim_entry(players):
        return {"salary_used": 50000, "projected_points": 100.0, "total_ownership_pct": 0.0, "players": players}

    # Same wide pool as the team-correlation section above (mean 10,
    # values only 0 or 20) -- a real edge shift should be easy to spot
    # against it. No team on either side, so this isolates the
    # own-edge signal from the team-multiplier one entirely.
    edge_hi_ids = list(range(96001, 96011))
    edge_lo_ids = list(range(96011, 96021))
    for pid in edge_hi_ids + edge_lo_ids:
        sim_pools[pid] = [0.0, 20.0] * 100

    edge_hi_entry = _sim_entry([_sim_player_full(pid, None, edge=1.3) for pid in edge_hi_ids])
    edge_lo_entry = _sim_entry([_sim_player_full(pid, None, edge=0.7) for pid in edge_lo_ids])
    edge_sim = variance.simulate_batch([edge_hi_entry, edge_lo_entry], sim_pools, num_trials=3000, seed=321)
    edge_hi_mean, edge_lo_mean = float(edge_sim[0].mean()), float(edge_sim[1].mean())
    # DELIBERATELY INVERTED from the pre-calibration behavior: pools are
    # recentered on today's projection now (variance.recenter_pool), and
    # the projection already embeds the matchup -- RotoWire's model
    # does, and the in-house number is baseline x edge composite by
    # construction. Also shifting the DRAW by the edge composite
    # double-counted the matchup, once in the level and once in the
    # percentile, which is exactly what the reference sims don't do.
    check("a player's edge_composite NO LONGER shifts his simulated draw -- the matchup lives "
          "in the (recentered) projection level, and shifting the draw too double-counted it",
          abs(edge_hi_mean - edge_lo_mean) < 3.0, str((round(edge_hi_mean, 2), round(edge_lo_mean, 2))))

    # A pitcher (plus 9 always-zero fillers, so the lineup total is
    # effectively just his own simulated value) alongside a full
    # 10-hitter stack of his real opponent, "OPP", in the SAME
    # simulate_batch() call -- both share the same underlying RNG draws
    # for OPP's team multiplier, so the stack's own simulated total is
    # a clean, already-validated proxy for "how good was OPP's day"
    # (see the team-correlation section above). Correlating the two
    # lineups' totals across trials tests the real end-to-end pitcher-
    # vs-opponent effect through the public API, not an internal.
    opp_hitter_ids = list(range(97001, 97011))
    for pid in opp_hitter_ids:
        sim_pools[pid] = [0.0, 20.0] * 100
    anti_corr_pitcher_id = 97100
    anti_corr_filler_ids = list(range(97101, 97110))
    sim_pools[anti_corr_pitcher_id] = [0.0, 20.0] * 100
    for pid in anti_corr_filler_ids:
        sim_pools[pid] = [0.0]

    anti_corr_pitcher_entry = _sim_entry([
        _sim_player_full(anti_corr_pitcher_id, None, opponent="OPP"),
        *[_sim_player_full(pid, None) for pid in anti_corr_filler_ids],
    ])
    opp_stack_entry = _sim_entry([_sim_player_full(pid, "OPP") for pid in opp_hitter_ids])
    anti_corr_sim = variance.simulate_batch(
        [anti_corr_pitcher_entry, opp_stack_entry], sim_pools, num_trials=3000, seed=4242
    )
    pitcher_totals, opp_totals = anti_corr_sim[0], anti_corr_sim[1]
    pitcher_opp_corr = float(np_module.corrcoef(pitcher_totals, opp_totals)[0, 1])
    check("a pitcher's simulated outcome trends negatively with the opposing team having a big day",
          pitcher_opp_corr < -0.1, str(round(pitcher_opp_corr, 3)))

    print("\nBring-back correlation: two teams facing EACH OTHER aren't independent (variance.py)")

    # Two real opponents (GAMEA @ GAMEB) each get their own 8-hitter
    # stack, both simulated in the SAME batch -- a real shootout should
    # lift both offenses together some of the time, not leave them
    # fully independent the way two teams that never play each other
    # would be. Each entry is padded with 2 zero-variance filler
    # "pitcher slot" players (simulate_batch() treats the first
    # PITCHER_COUNT players of a flat `players` list as pitchers,
    # matching every real entry's actual roster shape) so the fillers
    # can't muddy the correlation measurement -- the same pattern the
    # anti-correlation test above already uses.
    gamea_ids = list(range(98001, 98009))
    gameb_ids = list(range(98011, 98019))
    gamec_ids = list(range(98021, 98029))  # a third team, NOT gamea's real opponent
    for pid in gamea_ids + gameb_ids + gamec_ids:
        sim_pools[pid] = [0.0, 20.0] * 100
    bringback_filler_ids = list(range(98901, 98907))  # 2 per entry, 3 entries
    for pid in bringback_filler_ids:
        sim_pools[pid] = [0.0]

    def _stack_entry(hitter_ids, team, opponent, filler_ids):
        return _sim_entry([
            *[_sim_player_full(pid, None) for pid in filler_ids],
            *[_sim_player_full(pid, team, opponent=opponent) for pid in hitter_ids],
        ])

    gamea_entry = _stack_entry(gamea_ids, "GAMEA", "GAMEB", bringback_filler_ids[0:2])
    gameb_entry = _stack_entry(gameb_ids, "GAMEB", "GAMEA", bringback_filler_ids[2:4])
    gamec_entry = _stack_entry(gamec_ids, "GAMEC", "GAMED", bringback_filler_ids[4:6])

    bringback_sim = variance.simulate_batch(
        [gamea_entry, gameb_entry, gamec_entry], sim_pools, num_trials=5000, seed=8181
    )
    gamea_totals, gameb_totals, gamec_totals = bringback_sim[0], bringback_sim[1], bringback_sim[2]
    real_opp_corr = float(np_module.corrcoef(gamea_totals, gameb_totals)[0, 1])
    unrelated_corr = float(np_module.corrcoef(gamea_totals, gamec_totals)[0, 1])

    check("two teams playing EACH OTHER show real positive correlation between their simulated totals",
          real_opp_corr > 0.15, str(round(real_opp_corr, 3)))
    check("two teams that are NOT playing each other stay independent (no spurious correlation)",
          abs(unrelated_corr) < 0.1, str(round(unrelated_corr, 3)))
    check("bring-back correlation is genuine but partial, not near-total like within-team stacking",
          real_opp_corr < 0.9, str(round(real_opp_corr, 3)))

    # GAMED never appears anywhere in this batch (only referenced as
    # GAMEC's opponent) -- GAMEC must fall back to an independent day
    # rather than erroring or silently correlating with nothing.
    check("a team whose real opponent never appears in the batch still simulates cleanly",
          gamec_totals.std() > 0, str(float(gamec_totals.std())))

    # A solo run (GAMEA with no real opponent present at all) should
    # land at roughly the same mean/spread as the paired run above --
    # bring-back correlation is designed to change the CORRELATION
    # between two same-game teams, not either team's own marginal
    # distribution.
    solo_sim = variance.simulate_batch(
        [_stack_entry(gamea_ids, "GAMEA", None, bringback_filler_ids[0:2])],
        sim_pools, num_trials=5000, seed=8181,
    )
    check("pairing with a real opponent doesn't change a team's own marginal mean",
          abs(float(gamea_totals.mean()) - float(solo_sim[0].mean())) < 5.0,
          str((round(float(gamea_totals.mean()), 2), round(float(solo_sim[0].mean()), 2))))
    check("pairing with a real opponent doesn't change a team's own marginal spread",
          abs(float(gamea_totals.std()) - float(solo_sim[0].std())) < 0.15 * float(solo_sim[0].std()),
          str((round(float(gamea_totals.std()), 2), round(float(solo_sim[0].std()), 2))))

    print("\nAt-bat-level MLB simulation (atbat_sim.py)")

    def _atbat_hit_game(pa, hits, doubles=0, triples=0, hr=0, bb=0, hbp=0, k=0):
        return {
            "plate_appearances": pa, "hits": hits, "doubles": doubles, "triples": triples,
            "home_runs": hr, "walks": bb, "hit_by_pitch": hbp, "strikeouts": k,
        }

    check("pa_outcome_rates returns {} for a player with no games at all",
          atbat_sim.pa_outcome_rates([]) == {}, "")

    rates_100pa = atbat_sim.pa_outcome_rates([
        _atbat_hit_game(100, hits=30, doubles=6, triples=1, hr=3, bb=8, hbp=1, k=20)
    ])
    check("pa_outcome_rates' events all sum to (almost) exactly 1.0",
          abs(sum(rates_100pa.values()) - 1.0) < 1e-9, str(sum(rates_100pa.values())))
    check("pa_outcome_rates computes each event as its real observed count / total PA",
          rates_100pa["HR"] == 0.03 and rates_100pa["2B"] == 0.06 and rates_100pa["BB"] == 0.08,
          str(rates_100pa))
    check("pa_outcome_rates derives 1B as hits minus 2B/3B/HR, not double-counted",
          abs(rates_100pa["1B"] - 0.20) < 1e-9, str(rates_100pa))  # 30 hits - 6 - 1 - 3 = 20 singles

    def _atbat_pitch_game(bf, hits, doubles, triples, hr, bb, hbp, k):
        return {
            "plate_appearances": bf, "hits_against": hits, "doubles": doubles, "triples": triples,
            "home_runs": hr, "walks_against": bb, "hit_batsmen": hbp, "strikeouts": k,
        }

    check("pitcher_allowed_rates returns {} for a pitcher with no games at all",
          atbat_sim.pitcher_allowed_rates([]) == {}, "")

    pitcher_rates_100bf = atbat_sim.pitcher_allowed_rates([
        _atbat_pitch_game(100, hits=22, doubles=4, triples=0, hr=2, bb=9, hbp=1, k=27)
    ])
    check("pitcher_allowed_rates' events all sum to (almost) exactly 1.0",
          abs(sum(pitcher_rates_100bf.values()) - 1.0) < 1e-9, str(sum(pitcher_rates_100bf.values())))
    check("pitcher_allowed_rates reads the pitching-log field names (hits_against/walks_against/"
          "hit_batsmen/battersFaced-derived plate_appearances), not the hitting-log ones",
          pitcher_rates_100bf["HR"] == 0.02 and pitcher_rates_100bf["BB"] == 0.09
          and pitcher_rates_100bf["K"] == 0.27,
          str(pitcher_rates_100bf))
    check("pitcher_allowed_rates derives 1B allowed as hits minus 2B/3B/HR allowed",
          abs(pitcher_rates_100bf["1B"] - 0.16) < 1e-9, str(pitcher_rates_100bf))  # 22 - 4 - 0 - 2 = 16

    # _apply_edge_composite: ties the at-bat engine's blended PA rates
    # back to scoring.py's own matchup-quality signal -- the real fix
    # for a real, measured gap (simulated lineup results only weakly
    # tracking their own projected_points, r=0.48 on a live batch).
    neutral_rates = dict(atbat_sim.LEAGUE_AVG_PA_RATES)
    check("_apply_edge_composite leaves the rates unchanged with no composite signal at all",
          atbat_sim._apply_edge_composite(neutral_rates, None) == neutral_rates)

    hot_matchup = atbat_sim._apply_edge_composite(neutral_rates, 1.5)
    check("_apply_edge_composite's output still sums to (almost) exactly 1.0",
          abs(sum(hot_matchup.values()) - 1.0) < 1e-9, str(sum(hot_matchup.values())))
    check("a favorable (>1.0) composite raises every reach-base event's probability",
          all(hot_matchup[e] > neutral_rates[e] for e in ("1B", "2B", "3B", "HR", "BB")),
          str(hot_matchup))
    check("a favorable (>1.0) composite lowers every out event's probability",
          all(hot_matchup[e] < neutral_rates[e] for e in ("K", "OUT")), str(hot_matchup))

    cold_matchup = atbat_sim._apply_edge_composite(neutral_rates, 0.6)
    check("an unfavorable (<1.0) composite does the exact opposite -- lower reach-base, higher outs",
          all(cold_matchup[e] < neutral_rates[e] for e in ("1B", "2B", "3B", "HR", "BB"))
          and all(cold_matchup[e] > neutral_rates[e] for e in ("K", "OUT")),
          str(cold_matchup))

    check("_apply_edge_composite clamps an extreme composite to EDGE_COMPOSITE_MAX rather than "
          "letting one extreme signal blow the distribution out to an implausible shape",
          atbat_sim._apply_edge_composite(neutral_rates, 99.0)
          == atbat_sim._apply_edge_composite(neutral_rates, atbat_sim.EDGE_COMPOSITE_MAX),
          "")
    check("_apply_edge_composite clamps an extreme low composite to EDGE_COMPOSITE_MIN",
          atbat_sim._apply_edge_composite(neutral_rates, 0.01)
          == atbat_sim._apply_edge_composite(neutral_rates, atbat_sim.EDGE_COMPOSITE_MIN),
          "")

    no_bullpen_data = atbat_sim.bullpen_pa_rates(None)
    check("bullpen_pa_rates falls back to the neutral league-average with no real bullpen data",
          no_bullpen_data == atbat_sim.LEAGUE_AVG_PA_RATES, str(no_bullpen_data))
    bad_bullpen = atbat_sim.bullpen_pa_rates({"era": 6.50, "k_per_9": 6.0})
    good_bullpen = atbat_sim.bullpen_pa_rates({"era": 2.50, "k_per_9": 11.0})
    check("a genuinely shaky bullpen (high ERA) allows more HR than a strong one, at the same "
          "starting point",
          bad_bullpen["HR"] > good_bullpen["HR"], str((bad_bullpen["HR"], good_bullpen["HR"])))
    check("a genuinely shaky bullpen (low K/9) strikes out fewer batters than a strong one",
          bad_bullpen["K"] < good_bullpen["K"], str((bad_bullpen["K"], good_bullpen["K"])))

    blended_full_trust = atbat_sim.blend_pa_rates(
        {"K": 0.15, "BB": 0.12, "HBP": 0.01, "OUT": 0.40, "1B": 0.15, "2B": 0.08, "3B": 0.01, "HR": 0.08},
        None, batter_pa=600,
    )
    check("blend_pa_rates sums to 1.0 after blending",
          abs(sum(blended_full_trust.values()) - 1.0) < 1e-9, str(sum(blended_full_trust.values())))
    check("a full-trust power hitter's blended HR rate stays well above league average with no "
          "pitcher signal to counteract it",
          blended_full_trust["HR"] > atbat_sim.LEAGUE_AVG_PA_RATES["HR"] * 1.5, str(blended_full_trust))

    thin_sample_blend = atbat_sim.blend_pa_rates(
        {"K": 0.15, "BB": 0.12, "HBP": 0.01, "OUT": 0.40, "1B": 0.15, "2B": 0.08, "3B": 0.01, "HR": 0.08},
        None, batter_pa=15,
    )
    check("a thin-sample player's blended rate regresses most of the way toward league average, "
          "same shrinkage philosophy as everywhere else in this app",
          thin_sample_blend["HR"] < blended_full_trust["HR"], str(thin_sample_blend))

    no_batter_data_blend = atbat_sim.blend_pa_rates({}, None)
    check("blend_pa_rates falls back to league average entirely with no batter data at all "
          "(within floating-point rounding from the renormalize-to-1.0 step)",
          all(
              abs(no_batter_data_blend[event] - atbat_sim.LEAGUE_AVG_PA_RATES[event]) < 1e-9
              for event in atbat_sim.PA_EVENTS
          ),
          str(no_batter_data_blend))

    check("starter_outs_pool returns every real outs-per-start from his own logged starts, "
          "unaveraged -- the actual bootstrap pool a trial samples from, not one fixed number",
          atbat_sim.starter_outs_pool([{"outs": 18}, {"outs": 15}, {"outs": 21}]) == [18, 15, 21], "")
    check("starter_outs_pool skips a logged game with zero outs (didn't actually pitch that day)",
          atbat_sim.starter_outs_pool([{"outs": 18}, {"outs": 0}, {"outs": 21}]) == [18, 21], "")
    check("starter_outs_pool falls back to a reasonable single-value [15]-out (5 IP) pool with no "
          "starts logged",
          atbat_sim.starter_outs_pool([]) == [15], "")

    print("\nAt-bat-level baserunner advancement (atbat_sim._advance_runners)")

    _rng0 = random.Random(0)  # deterministic branch selection below via explicit thresholds

    class _AlwaysHigh:
        def random(self):
            return 0.99

    class _AlwaysLow:
        def random(self):
            return 0.01

    bases_loaded = (101, 102, 103)
    new_bases, scorers, extra_out = atbat_sim._advance_runners(bases_loaded, "HR", 999, 0, _rng0)
    check("a grand slam clears the bases and scores all 3 runners plus the batter",
          new_bases == (None, None, None) and sorted(scorers) == [101, 102, 103, 999] and not extra_out,
          str((new_bases, scorers)))

    new_bases, scorers, _ = atbat_sim._advance_runners(bases_loaded, "3B", 999, 0, _rng0)
    check("a bases-loaded triple scores all 3 runners, batter ends on 3rd",
          new_bases == (None, None, 999) and sorted(scorers) == [101, 102, 103], str((new_bases, scorers)))

    new_bases, scorers, _ = atbat_sim._advance_runners((None, 102, 103), "2B", 999, 0, _AlwaysLow())
    check("on a double, runners already on 2nd/3rd always score, batter ends on 2nd",
          new_bases == (None, 999, None) and sorted(scorers) == [102, 103], str((new_bases, scorers)))

    new_bases, scorers, _ = atbat_sim._advance_runners((101, None, None), "2B", 999, 0, _AlwaysLow())
    check("on a double, a runner on 1st scores when the coin flip favors sending him",
          new_bases == (None, 999, None) and scorers == [101], str((new_bases, scorers)))
    new_bases, scorers, _ = atbat_sim._advance_runners((101, None, None), "2B", 999, 0, _AlwaysHigh())
    check("on a double, that same runner on 1st holds at 3rd when the coin flip doesn't favor sending him",
          new_bases == (None, 999, 101) and scorers == [], str((new_bases, scorers)))

    new_bases, scorers, _ = atbat_sim._advance_runners((101, None, 103), "1B", 999, 0, _AlwaysLow())
    check("on a single, a runner on 3rd always scores and a runner on 1st always reaches 2nd",
          new_bases == (999, 101, None) and scorers == [103], str((new_bases, scorers)))
    new_bases, scorers, _ = atbat_sim._advance_runners((None, 102, None), "1B", 999, 0, _AlwaysLow())
    check("on a single, a runner on 2nd scores when the coin flip favors sending him",
          scorers == [102] and new_bases == (999, None, None), str((new_bases, scorers)))
    new_bases, scorers, _ = atbat_sim._advance_runners((None, 102, None), "1B", 999, 0, _AlwaysHigh())
    check("on a single, that same runner on 2nd holds at 3rd when the coin flip doesn't favor sending him",
          new_bases == (999, None, 102) and scorers == [], str((new_bases, scorers)))

    new_bases, scorers, extra_out = atbat_sim._advance_runners((None, None, None), "BB", 999, 0, _rng0)
    check("a walk with the bases empty just puts the batter on 1st, forces nobody",
          new_bases == (999, None, None) and scorers == [] and not extra_out, str(new_bases))
    new_bases, scorers, extra_out = atbat_sim._advance_runners((101, None, None), "BB", 999, 0, _rng0)
    check("a walk with a runner on 1st forces him to 2nd",
          new_bases == (999, 101, None) and scorers == [], str(new_bases))
    new_bases, scorers, extra_out = atbat_sim._advance_runners((101, 102, None), "HBP", 999, 0, _rng0)
    check("a hit-by-pitch with runners on 1st/2nd forces both up one base",
          new_bases == (999, 101, 102) and scorers == [], str(new_bases))
    new_bases, scorers, extra_out = atbat_sim._advance_runners(bases_loaded, "BB", 999, 0, _rng0)
    check("a bases-loaded walk forces in a run from 3rd",
          new_bases == (999, 101, 102) and scorers == [103], str((new_bases, scorers)))
    new_bases, scorers, extra_out = atbat_sim._advance_runners((None, 102, 103), "BB", 999, 0, _rng0)
    check("a walk does NOT force a runner on 2nd/3rd forward when 1st base is open",
          new_bases == (999, 102, 103) and scorers == [], str(new_bases))

    new_bases, scorers, extra_out = atbat_sim._advance_runners((None, None, 103), "OUT", 999, 1, _AlwaysLow())
    check("a runner on 3rd with under 2 outs scores on a generic out when the productive-out "
          "chance hits",
          scorers == [103] and not extra_out, str((new_bases, scorers, extra_out)))
    new_bases, scorers, extra_out = atbat_sim._advance_runners((None, None, 103), "OUT", 999, 2, _AlwaysLow())
    check("that same runner on 3rd does NOT score on a generic out with 2 outs already -- the "
          "inning would already be over on the next out anyway",
          scorers == [] and not extra_out, str((new_bases, scorers, extra_out)))
    new_bases, scorers, extra_out = atbat_sim._advance_runners((101, None, None), "OUT", 999, 0, _AlwaysLow())
    check("a runner on 1st with under 2 outs can be removed on a generic out via the simplified "
          "double-play chance, adding an extra out",
          new_bases[0] is None and extra_out, str((new_bases, extra_out)))
    new_bases, scorers, extra_out = atbat_sim._advance_runners((None, None, None), "K", 999, 0, _AlwaysLow())
    check("a strikeout never moves any baserunner, regardless of what the coin flip would allow",
          new_bases == (None, None, None) and scorers == [] and not extra_out, str(new_bases))

    print("\nAt-bat-level full-game simulation (atbat_sim.simulate_game)")

    # 60/40 HR/OUT, not literally 100% HR -- every PA still resolves to
    # only HR or OUT (so the "each run equals that player's own HR
    # count" invariant below still holds exactly, since nobody ever
    # reaches base except via a homer, which immediately clears again),
    # but a genuinely 0%-chance-of-an-out lineup would make a half-
    # inning mathematically unable to ever end (caught for real: an
    # earlier version of this fixture hung the whole test suite).
    hr_rates = {e: (0.6 if e == "HR" else (0.4 if e == "OUT" else 0.0)) for e in atbat_sim.PA_EVENTS}
    all_out_rates = {e: (1.0 if e == "OUT" else 0.0) for e in atbat_sim.PA_EVENTS}
    hr_lineup = list(range(1, 10))
    out_lineup = list(range(101, 110))
    hr_result = atbat_sim.simulate_game(
        home_order=hr_lineup, away_order=out_lineup,
        home_pa_rates={pid: hr_rates for pid in hr_lineup},
        away_pa_rates={pid: all_out_rates for pid in out_lineup},
        home_bullpen_rates=all_out_rates, away_bullpen_rates=hr_rates,
        home_starter_outs=27, away_starter_outs=27,
        rng=random.Random(1),
    )
    hr_box = hr_result["box"]
    check("simulate_game's home/away run totals match the sum of each lineup's own box-score runs",
          hr_result["home_runs"] == sum(hr_box.get(pid, {"runs": 0})["runs"] for pid in hr_lineup)
          and hr_result["away_runs"] == sum(hr_box.get(pid, {"runs": 0})["runs"] for pid in out_lineup),
          str((hr_result["home_runs"], hr_result["away_runs"])))
    check("the HOME starter -- who faces the AWAY (all-out) lineup -- records all 27 outs himself "
          "and gets credited with a complete game, a shutout (the all-out lineup never scored), "
          "and the win (his own team scored, theirs didn't)",
          hr_result["home_starter_line"]["complete_games"] == 1
          and hr_result["home_starter_line"]["shutouts"] == 1
          and hr_result["home_starter_line"]["wins"] == 1,
          str(hr_result["home_starter_line"]))
    check("the AWAY starter -- who faces the HOME (all-HR) lineup all game (his 27-out budget "
          "exactly matches the 27 real outs that occur across 9 innings, so he's never pulled) -- "
          "allowed every hit and earned run charged in the game, and did not get the win",
          hr_result["away_starter_line"]["hits_against"] == sum(
              hr_box.get(pid, {"home_runs": 0})["home_runs"] for pid in hr_lineup
          )
          and hr_result["away_starter_line"]["earned_runs"] == hr_result["home_runs"]
          and hr_result["away_starter_line"]["wins"] == 0,
          str(hr_result["away_starter_line"]))
    check("a lineup that homers on literally every plate appearance scores exactly 1 run per "
          "plate appearance (solo shots, bases always empty from the previous batter's own HR)",
          all(hr_box[pid]["runs"] == hr_box[pid]["home_runs"] for pid in hr_lineup),
          str({pid: (hr_box[pid]["runs"], hr_box[pid]["home_runs"]) for pid in hr_lineup}))
    check("a lineup that makes an out on literally every plate appearance scores zero runs and "
          "gets no hits at all",
          all(out_lineup_line["runs"] == 0 and out_lineup_line["hits"] == 0
              for pid in out_lineup for out_lineup_line in [hr_box.get(pid, {"runs": 0, "hits": 0})]),
          str({pid: hr_box.get(pid) for pid in out_lineup}))
    check("the all-out team faces exactly 27 outs across 9 innings (3 outs x 9, no baserunners "
          "ever on to complicate it) -- total PA across that lineup equals exactly 27",
          sum(hr_box.get(pid, {"plate_appearances": 0})["plate_appearances"] for pid in out_lineup) == 27,
          str(sum(hr_box.get(pid, {"plate_appearances": 0})["plate_appearances"] for pid in out_lineup)))

    real_rates = atbat_sim.blend_pa_rates(rates_100pa, None, batter_pa=600)
    real_lineup_a = list(range(201, 210))
    real_lineup_b = list(range(301, 310))
    real_result = atbat_sim.simulate_game(
        home_order=real_lineup_a, away_order=real_lineup_b,
        home_pa_rates={pid: real_rates for pid in real_lineup_a},
        away_pa_rates={pid: real_rates for pid in real_lineup_b},
        home_bullpen_rates=atbat_sim.LEAGUE_AVG_PA_RATES, away_bullpen_rates=atbat_sim.LEAGUE_AVG_PA_RATES,
        home_starter_outs=15, away_starter_outs=15,
        rng=random.Random(2),
    )
    real_box = real_result["box"]
    check("each starter's own simulated pitching line converts cleanly through the EXISTING "
          "mlb_dk_points.pitcher_game_points() scorer with no missing-field errors",
          isinstance(mlb_dk_points.pitcher_game_points(real_result["home_starter_line"]), float)
          and isinstance(mlb_dk_points.pitcher_game_points(real_result["away_starter_line"]), float),
          str((real_result["home_starter_line"], real_result["away_starter_line"])))
    check("a starter with only a 15-out (5 IP) budget against a real-rate lineup does NOT get "
          "credited with a complete game",
          real_result["home_starter_line"]["complete_games"] == 0
          and real_result["away_starter_line"]["complete_games"] == 0,
          str((real_result["home_starter_line"]["complete_games"], real_result["away_starter_line"]["complete_games"])))
    check("a realistic full game produces a genuine, plausible box score -- every lineup slot "
          "got at least 3 plate appearances (9 innings is plenty for a full lineup to bat "
          "multiple times) and the whole game produced at least one hit somewhere",
          all(real_box.get(pid, {"plate_appearances": 0})["plate_appearances"] >= 3
              for pid in real_lineup_a + real_lineup_b)
          and sum(real_box.get(pid, {"hits": 0})["hits"] for pid in real_lineup_a + real_lineup_b) > 0,
          str({pid: real_box.get(pid) for pid in (real_lineup_a + real_lineup_b)[:3]}))
    check("every simulated player's real counting stats convert cleanly through the EXISTING "
          "mlb_dk_points.py scorer with no missing-field errors -- proof the box score shape "
          "this module produces is genuinely compatible with the app's already-built DK scoring",
          all(
              isinstance(mlb_dk_points.hitter_game_points(real_box[pid]), float)
              for pid in real_lineup_a + real_lineup_b if pid in real_box
          ),
          "")

    print("\nAt-bat engine realism: ER charging, 9th inning, shrinkage, Vegas anchor")

    # MLB's real earned-run rule: a run is charged to whoever put THAT
    # RUNNER on base. Deterministic fixture: the starter walks the
    # first batter (runner charged to him), strikes out the second
    # (his 1-out budget is used up -> bullpen), and the third homers
    # off the bullpen -- scoring the starter's inherited runner. The
    # old code charged that run to nobody.
    _walk_rates = {e: (1.0 if e == "BB" else 0.0) for e in atbat_sim.PA_EVENTS}
    _k_rates = {e: (1.0 if e == "K" else 0.0) for e in atbat_sim.PA_EVENTS}
    _hr_only = {e: (1.0 if e == "HR" else 0.0) for e in atbat_sim.PA_EVENTS}
    _out_only = {e: (1.0 if e == "OUT" else 0.0) for e in atbat_sim.PA_EVENTS}
    er_order = [601, 602, 603, 604, 605, 606, 607, 608, 609]
    er_rates = {601: _walk_rates, 602: _k_rates, 603: _hr_only}
    for pid in er_order[3:]:
        er_rates[pid] = _out_only
    # Once the starter departs, every batter faces the BULLPEN's rates
    # -- so the bullpen fixture is all-HR, which scores the starter's
    # inherited runner (charged to him) plus a stream of bullpen-era
    # runs (charged to nobody's line here, and rightly not the starter).
    er_box: dict[int, dict] = {}
    _, er_outs, er_line, er_runs = atbat_sim._simulate_half_inning_tracking_starter(
        er_order, 0, er_rates, er_box, random.Random(1), 1, _hr_only,
    )
    check("an INHERITED runner who scores off the bullpen is charged to the STARTER's earned "
          "runs -- MLB's real rule, where the old code charged that run to nobody",
          er_line["earned_runs"] == 1, str(er_line))
    check("...while every bullpen-era run (and there are plenty in this all-HR fixture) is NOT "
          "charged to the starter -- his ER stays at exactly the one inherited runner",
          er_runs > 1 and er_line["earned_runs"] == 1, str((er_line["earned_runs"], er_runs)))
    check("the starter's own line still reflects only what he personally allowed (1 walk, 1 K, "
          "1 out, 0 hits -- the HR came off the bullpen)",
          er_line["walks_against"] == 1 and er_line["strikeouts"] == 1
          and er_line["outs"] == 1 and er_line["hits_against"] == 0, str(er_line))

    # The home team doesn't bat in the bottom of the 9th when it leads:
    # the all-out away lineup can never score, the home lineup homers
    # constantly, so the away STARTER only ever faces 8 innings of home
    # hitters -- 24 outs, never 27.
    lead_result = atbat_sim.simulate_game(
        home_order=hr_lineup, away_order=out_lineup,
        home_pa_rates={pid: hr_rates for pid in hr_lineup},
        away_pa_rates={pid: all_out_rates for pid in out_lineup},
        home_bullpen_rates=all_out_rates, away_bullpen_rates=hr_rates,
        home_starter_outs=27, away_starter_outs=27,
        rng=random.Random(11),
    )
    check("a leading home team skips the bottom of the 9th -- its opponents' starter records "
          "24 outs (8 innings), not 27, ending the old 3-4 phantom home plate appearances",
          lead_result["away_starter_line"]["outs"] == 24, str(lead_result["away_starter_line"]))
    check("the away starter who pitched every out his defense played in a home win still earns "
          "his complete game at 24 outs",
          lead_result["away_starter_line"]["complete_games"] == 1,
          str(lead_result["away_starter_line"]))

    # Walk-off: in the bottom of the final inning the half ends the
    # moment the home team takes the lead. With only HR/OUT events and
    # a 0-run deficit, exactly one run can ever score.
    wo_box: dict[int, dict] = {}
    _, _, _, wo_runs = atbat_sim._simulate_half_inning_tracking_starter(
        hr_lineup, 0, {pid: hr_rates for pid in hr_lineup}, wo_box,
        random.Random(3), 27, all_out_rates, walkoff_deficit=0,
    )
    check("a walk-off ends the half-inning the instant the home team leads -- exactly one run "
          "scores past a tied game, never a phantom multi-run bottom of the 9th",
          wo_runs == 1, str(wo_runs))

    # Pitcher-rate shrinkage: a 40-BF pitcher with zero HR allowed must
    # NOT suppress opposing HR probability anywhere near as hard as a
    # 400-BF pitcher with the same zero -- the old code trusted both
    # identically (straight to the 0.3x floor).
    _zero_hr_pitcher = dict(atbat_sim.LEAGUE_AVG_PA_RATES)
    _zero_hr_pitcher["HR"] = 0.0
    thin = atbat_sim.blend_pa_rates(
        atbat_sim.LEAGUE_AVG_PA_RATES, _zero_hr_pitcher, batter_pa=600, pitcher_pa=40)
    proven = atbat_sim.blend_pa_rates(
        atbat_sim.LEAGUE_AVG_PA_RATES, _zero_hr_pitcher, batter_pa=600, pitcher_pa=400)
    check("a 40-batters-faced pitcher's zero-HR line is shrunk toward league average, while a "
          "400-BF pitcher's identical line is trusted -- ~40 BF was never evidence of anything",
          thin["HR"] > proven["HR"], str((thin["HR"], proven["HR"])))

    check("_apply_run_scale scales reach-base probability and stays a real distribution",
          abs(sum(atbat_sim._apply_run_scale(atbat_sim.LEAGUE_AVG_PA_RATES, 1.2).values()) - 1.0) < 1e-9
          and atbat_sim._apply_run_scale(atbat_sim.LEAGUE_AVG_PA_RATES, 1.2)["HR"]
          > atbat_sim.LEAGUE_AVG_PA_RATES["HR"], "")

    # Vegas anchoring: two identical league-average offenses, but the
    # market says one scores 2.8 runs and the other 6.2 -- the anchor
    # must scale the first DOWN and the second UP.
    anchor_game = {
        "home_order": hr_lineup, "away_order": out_lineup,
        "home_pa_rates": {pid: dict(atbat_sim.LEAGUE_AVG_PA_RATES) for pid in hr_lineup},
        "away_pa_rates": {pid: dict(atbat_sim.LEAGUE_AVG_PA_RATES) for pid in out_lineup},
        "home_bullpen_rates": dict(atbat_sim.LEAGUE_AVG_PA_RATES),
        "away_bullpen_rates": dict(atbat_sim.LEAGUE_AVG_PA_RATES),
        "home_starter_outs_pool": [18], "away_starter_outs_pool": [18],
        "home_pitcher_id": 9901, "away_pitcher_id": 9902,
        "home_implied_runs": 2.8, "away_implied_runs": 6.2,
    }
    anchored = atbat_sim._anchored_rates(anchor_game, seed=5)
    scales = anchored.get("vegas_anchor_scales") or {}
    check("Vegas anchoring scales a team the market prices LOW below 1.0 and a team priced "
          "HIGH above 1.0 -- the sim now agrees with the market about run environments the "
          "way the real field does",
          scales.get("home", 1) < 1.0 < scales.get("away", 1), str(scales))
    check("a game with no implied totals at all is left exactly as built -- anchoring never "
          "invents a target",
          "vegas_anchor_scales" not in atbat_sim._anchored_rates(
              {**anchor_game, "home_implied_runs": None, "away_implied_runs": None}, seed=5), "")

    print("\nAt-bat-level slate orchestration (atbat_sim.simulate_slate_trials)")

    def _slate_hit_game():
        return {
            "plate_appearances": 4, "hits": 1, "doubles": 0, "triples": 0,
            "home_runs": 0, "walks": 0, "hit_by_pitch": 0, "strikeouts": 1,
        }

    def _slate_pitch_game():
        return {
            "plate_appearances": 24, "hits_against": 6, "doubles": 0, "triples": 0,
            "home_runs": 1, "walks_against": 2, "hit_batsmen": 0, "strikeouts": 6, "outs": 18,
        }

    slate_hitter_ids = list(range(401, 410)) + list(range(501, 510))
    slate_hitting_logs = {pid: [_slate_hit_game()] * 50 for pid in slate_hitter_ids}
    slate_pitching_logs = {5001: [_slate_pitch_game()] * 20, 5002: [_slate_pitch_game()] * 20}

    async def fake_slate_game_log(player_id, season, group="hitting"):
        if group == "pitching":
            return slate_pitching_logs.get(player_id, [])
        return slate_hitting_logs.get(player_id, [])

    mlb.get_player_game_log = fake_slate_game_log

    def _slate_side(team_id, abbrev, hitter_ids, pitcher_id, confirmed=True, projected_order=None, composites=None):
        # projected_order: optional list of hitter ids (order matters) to
        # assign a 1-based projected_batting_order to, standing in for
        # what an uploaded RotoWire file's LINEUP column would carry.
        # None leaves every hitter's projected_batting_order unset (the
        # "no projections file loaded" case). composites: optional
        # {hitter_id: composite} to attach as edge.composite -- unset
        # hitters get no "edge" key at all, same as a real hitter dict
        # with no computed edge.
        projected_spot = {pid: i + 1 for i, pid in enumerate(projected_order or [])}
        composites = composites or {}
        return {
            "team_id": team_id,
            "abbrev": abbrev,
            "hitters": [
                {
                    "id": pid,
                    "batting_order": (i + 1) if confirmed else None,
                    "projected_batting_order": projected_spot.get(pid),
                    **({"edge": {"composite": composites[pid]}} if pid in composites else {}),
                }
                for i, pid in enumerate(hitter_ids)
            ],
            "probable_pitcher": {"id": pitcher_id},
            "lineup_confirmed": confirmed,
        }

    ready_slate = {
        "games": [
            {
                "in_slate": True,
                "home": _slate_side(9001, "HOM", list(range(401, 410)), 5001),
                "away": _slate_side(9002, "AWY", list(range(501, 510)), 5002),
            }
        ]
    }

    slate_trials = await atbat_sim.simulate_slate_trials(ready_slate, VARIANCE_SEASON, num_trials=25, seed=5)
    all_slate_ids = slate_hitter_ids + [5001, 5002]
    check("simulate_slate_trials returns num_trials DK-point values for every hitter and both "
          "starting pitchers on the slate",
          all(len(slate_trials.get(pid, [])) == 25 for pid in all_slate_ids),
          str({pid: len(slate_trials.get(pid, [])) for pid in all_slate_ids}))
    check("every simulated value is a real float",
          all(isinstance(v, float) for arr in slate_trials.values() for v in arr), "")

    # A hitter on the slate but NOT in either batting order: the contest
    # generator's pool is built from salary and projection alone, so a
    # cheap bench bat with a real DK price can legally land in a lineup,
    # and the whole batch used to be refused over him ("no simulated
    # outcome for player id(s) ..."). He takes no plate appearances in
    # the simulated game, so he scores nothing -- the simulation's own
    # answer, not a fabricated stand-in.
    bench_slate = {
        "games": [
            {
                "in_slate": True,
                "home": _slate_side(9001, "HOM", list(range(401, 410)), 5001),
                "away": _slate_side(9002, "AWY", list(range(501, 510)), 5002),
            }
        ]
    }
    bench_slate["games"][0]["home"]["hitters"].append(
        {"id": 4999, "batting_order": None, "projected_batting_order": None}
    )
    bench_trials = await atbat_sim.simulate_slate_trials(
        bench_slate, VARIANCE_SEASON, num_trials=25, seed=5
    )
    check("a rostered hitter who isn't in either batting order gets a real all-zero trial series "
          "instead of blocking the whole slate -- he doesn't bat, so he doesn't score",
          bench_trials.get(4999) == [0.0] * 25, str(bench_trials.get(4999))[:80])
    check("...and adding him changes nothing about the nine real starters' own simulated trials",
          all(bench_trials[pid] == slate_trials[pid] for pid in range(401, 410)), "")
    check("simulate_slate_trials is deterministic for a fixed seed -- re-running with the same "
          "seed reproduces the exact same trial arrays",
          await atbat_sim.simulate_slate_trials(ready_slate, VARIANCE_SEASON, num_trials=25, seed=5)
          == slate_trials,
          "")

    # End-to-end proof starter-innings variance actually reaches the
    # simulated output, not just starter_outs_pool()'s own return value:
    # a pitcher whose real starts vary widely (6 to 27 outs) should
    # produce genuinely more spread-out simulated DK points, over many
    # trials, than an otherwise-identical pitcher whose starts never
    # varied at all (always exactly 18 outs) -- the exact real gap a
    # real backtest against archived contest data found (starting
    # pitchers showed ZERO simulated innings variance before this fix).
    def _varied_outs_pitch_game(outs):
        return {
            "plate_appearances": 24, "hits_against": 6, "doubles": 0, "triples": 0,
            "home_runs": 1, "walks_against": 2, "hit_batsmen": 0, "strikeouts": 6, "outs": outs,
        }

    varied_pitching_logs = dict(slate_pitching_logs)
    varied_pitching_logs[5001] = [_varied_outs_pitch_game(o) for o in (6, 12, 18, 24, 27)] * 4

    async def fake_varied_outs_game_log(player_id, season, group="hitting"):
        if group == "pitching":
            return varied_pitching_logs.get(player_id, [])
        return slate_hitting_logs.get(player_id, [])

    mlb.get_player_game_log = fake_varied_outs_game_log
    check("starter_outs_pool actually returns the pitcher's real varied outs-per-start, not "
          "one averaged number",
          sorted(set(atbat_sim.starter_outs_pool(varied_pitching_logs[5001]))) == [6, 12, 18, 24, 27],
          str(atbat_sim.starter_outs_pool(varied_pitching_logs[5001])))

    varied_outs_trials = await atbat_sim.simulate_slate_trials(
        ready_slate, VARIANCE_SEASON, num_trials=300, seed=17
    )
    mlb.get_player_game_log = fake_slate_game_log  # back to the fixed-18-outs fixture
    fixed_outs_trials = await atbat_sim.simulate_slate_trials(
        ready_slate, VARIANCE_SEASON, num_trials=300, seed=17
    )
    varied_stdev = statistics_module.pstdev(varied_outs_trials[5001])
    fixed_stdev = statistics_module.pstdev(fixed_outs_trials[5001])
    check("a starter whose real starts vary widely (6-27 outs) shows meaningfully MORE simulated "
          "DK-point spread, over many trials, than an otherwise-identical starter whose starts "
          "never varied at all -- proof stochastic innings actually reach the simulated output",
          varied_stdev > fixed_stdev * 1.3, str((round(varied_stdev, 2), round(fixed_stdev, 2))))

    # End-to-end proof the edge.composite fix actually changes simulated
    # results: two hitters with IDENTICAL game logs (same underlying
    # season rate), one given a strongly favorable matchup composite and
    # the other a strongly unfavorable one -- their simulated DK-point
    # means should now differ clearly, which they could NOT before this
    # fix (the engine had no way to tell them apart at all).
    composite_slate = {
        "games": [
            {
                "in_slate": True,
                "home": _slate_side(
                    9001, "HOM", list(range(401, 410)), 5001,
                    composites={401: 1.6, 402: 0.6},
                ),
                "away": _slate_side(9002, "AWY", list(range(501, 510)), 5002),
            }
        ]
    }
    composite_trials = await atbat_sim.simulate_slate_trials(
        composite_slate, VARIANCE_SEASON, num_trials=400, seed=11
    )
    hot_mean = statistics_module.mean(composite_trials[401])
    cold_mean = statistics_module.mean(composite_trials[402])
    check("a hitter with a strongly favorable edge.composite simulates meaningfully higher DK "
          "points, over enough trials, than an otherwise-identical hitter with a strongly "
          "unfavorable one -- proof the composite signal actually reaches the simulated output",
          hot_mean > cold_mean * 1.3, str((round(hot_mean, 2), round(cold_mean, 2))))

    unconfirmed_slate = {
        "games": [
            {
                "in_slate": True,
                "home": _slate_side(9001, "HOM", list(range(401, 410)), 5001, confirmed=False),
                "away": _slate_side(9002, "AWY", list(range(501, 510)), 5002),
            }
        ]
    }
    try:
        await atbat_sim.simulate_slate_trials(unconfirmed_slate, VARIANCE_SEASON, num_trials=5)
        check("simulate_slate_trials raises SlateNotSimulatableError when a game's lineup isn't "
              "confirmed on both sides -- no partial/hybrid fallback in V1", False, "no exception raised")
    except atbat_sim.SlateNotSimulatableError as e:
        check("simulate_slate_trials raises SlateNotSimulatableError when a game's lineup isn't "
              "confirmed on both sides -- no partial/hybrid fallback in V1, and the message names "
              "the specific unready game",
              "HOM" in str(e) and "AWY" in str(e), str(e))

    try:
        await atbat_sim.simulate_slate_trials({"games": []}, VARIANCE_SEASON, num_trials=5)
        check("simulate_slate_trials raises SlateNotSimulatableError for a slate with no games", False, "")
    except atbat_sim.SlateNotSimulatableError:
        check("simulate_slate_trials raises SlateNotSimulatableError for a slate with no games", True, "")

    # A slate with one ready game and one NOT-ready game -- included_game_pks
    # should let a caller who only wants entries from the ready game skip
    # requiring the whole slate to be confirmed, matching how every other
    # entry-building path in contest.py already respects that same param.
    mixed_slate = {
        "games": [
            {
                "game_pk": 1, "in_slate": True,
                "home": _slate_side(9001, "HOM", list(range(401, 410)), 5001),
                "away": _slate_side(9002, "AWY", list(range(501, 510)), 5002),
            },
            {
                "game_pk": 2, "in_slate": True,
                "home": _slate_side(9003, "HM2", list(range(401, 410)), 5001, confirmed=False),
                "away": _slate_side(9004, "AW2", list(range(501, 510)), 5002),
            },
        ]
    }
    scoped_trials = await atbat_sim.simulate_slate_trials(
        mixed_slate, VARIANCE_SEASON, num_trials=5, included_game_pks=[1]
    )
    check("simulate_slate_trials with included_game_pks scoped to only the ready game succeeds, "
          "ignoring the other unready game on the same slate entirely",
          all(len(scoped_trials.get(pid, [])) == 5 for pid in all_slate_ids),
          str({pid: len(scoped_trials.get(pid, [])) for pid in all_slate_ids}))
    try:
        await atbat_sim.simulate_slate_trials(mixed_slate, VARIANCE_SEASON, num_trials=5)
        check("simulate_slate_trials without included_game_pks still requires EVERY in_slate game "
              "ready, including the unready second one", False, "no exception raised")
    except atbat_sim.SlateNotSimulatableError:
        check("simulate_slate_trials without included_game_pks still requires EVERY in_slate game "
              "ready, including the unready second one", True, "")

    print("\nAt-bat-level projected-lineup fallback (RotoWire LINEUP column, no confirmed lineup yet)")

    # Home side has NO confirmed lineup yet, but 8 of its 9 hitters carry
    # a RotoWire-projected batting spot (>= MIN_PROJECTED_LINEUP_SIZE) --
    # should fall back to using those, in projected order, instead of
    # raising SlateNotSimulatableError the way the plain unconfirmed_slate
    # fixture above (no projection data at all) correctly does.
    projected_slate = {
        "games": [
            {
                "in_slate": True,
                "home": _slate_side(
                    9001, "HOM", list(range(401, 410)), 5001,
                    confirmed=False, projected_order=list(range(401, 409)),  # 8 of the 9
                ),
                "away": _slate_side(9002, "AWY", list(range(501, 510)), 5002),
            }
        ]
    }
    projected_trials = await atbat_sim.simulate_slate_trials(
        projected_slate, VARIANCE_SEASON, num_trials=5, seed=3
    )
    projected_expected_ids = list(range(401, 409)) + list(range(501, 510)) + [5001, 5002]
    check("simulate_slate_trials falls back to RotoWire's projected batting order when a lineup "
          "isn't confirmed yet, simulating the game successfully instead of raising",
          all(len(projected_trials.get(pid, [])) == 5 for pid in projected_expected_ids),
          str({pid: len(projected_trials.get(pid, [])) for pid in projected_expected_ids}))
    check("the hitter left OUT of the projected order (409 has no projected_batting_order set) "
          "scores a flat zero every trial -- he never came to the plate, so only the projected 8 "
          "were actually used, and a lineup rostering him is priced accordingly rather than "
          "blocking the whole slate",
          projected_trials.get(409) == [0.0] * 5, str(projected_trials.get(409)))

    # Too few projected spots (below MIN_PROJECTED_LINEUP_SIZE) -- still
    # not ready, same as having no projection data at all.
    thin_projected_slate = {
        "games": [
            {
                "in_slate": True,
                "home": _slate_side(
                    9001, "HOM", list(range(401, 410)), 5001,
                    confirmed=False, projected_order=list(range(401, 406)),  # only 5
                ),
                "away": _slate_side(9002, "AWY", list(range(501, 510)), 5002),
            }
        ]
    }
    try:
        await atbat_sim.simulate_slate_trials(thin_projected_slate, VARIANCE_SEASON, num_trials=5)
        check("simulate_slate_trials still raises SlateNotSimulatableError when the projected "
              "lineup is too thin (below MIN_PROJECTED_LINEUP_SIZE) to trust",
              False, "no exception raised")
    except atbat_sim.SlateNotSimulatableError as e:
        check("simulate_slate_trials still raises SlateNotSimulatableError when the projected "
              "lineup is too thin (below MIN_PROJECTED_LINEUP_SIZE) to trust",
              "HOM" in str(e), str(e))

    print("\nAt-bat-level projected-pitcher fallback (RotoWire's projected starter, no real "
          "probable pitcher announced yet)")

    def _slate_side_no_pitcher(team_id, abbrev, hitter_ids, projected_pitcher_id=None):
        return {
            "team_id": team_id,
            "abbrev": abbrev,
            "hitters": [{"id": pid, "batting_order": i + 1} for i, pid in enumerate(hitter_ids)],
            "probable_pitcher": None,
            "projected_probable_pitcher": (
                {"id": projected_pitcher_id, "name": "Fallback Starter"} if projected_pitcher_id else None
            ),
            "lineup_confirmed": True,
        }

    no_real_pitcher_slate = {
        "games": [
            {
                "in_slate": True,
                "home": _slate_side_no_pitcher(9001, "HOM", list(range(401, 410)), projected_pitcher_id=5001),
                "away": _slate_side(9002, "AWY", list(range(501, 510)), 5002),
            }
        ]
    }
    fallback_pitcher_trials = await atbat_sim.simulate_slate_trials(
        no_real_pitcher_slate, VARIANCE_SEASON, num_trials=5, seed=4
    )
    check("simulate_slate_trials falls back to RotoWire's projected starter when there's no real "
          "probable pitcher announced yet, simulating successfully instead of raising",
          len(fallback_pitcher_trials.get(5001, [])) == 5, str(fallback_pitcher_trials.get(5001)))

    no_pitcher_at_all_slate = {
        "games": [
            {
                "in_slate": True,
                # Neither a real nor a projected probable pitcher.
                "home": _slate_side_no_pitcher(9001, "HOM", list(range(401, 410))),
                "away": _slate_side(9002, "AWY", list(range(501, 510)), 5002),
            }
        ]
    }
    try:
        await atbat_sim.simulate_slate_trials(no_pitcher_at_all_slate, VARIANCE_SEASON, num_trials=5)
        check("simulate_slate_trials still raises SlateNotSimulatableError with neither a real "
              "nor a projected probable pitcher", False, "no exception raised")
    except atbat_sim.SlateNotSimulatableError as e:
        check("simulate_slate_trials still raises SlateNotSimulatableError with neither a real "
              "nor a projected probable pitcher", "HOM" in str(e), str(e))

    print("\nAt-bat-level backtesting: as_of_date excludes look-ahead data (no future games leak in)")

    def _dated_hit_game(date, hot):
        if hot:
            # A real "monster game" line: 3-for-4 with a double, a HR, and a walk.
            return {
                "plate_appearances": 5, "hits": 3, "doubles": 1, "triples": 0,
                "home_runs": 1, "walks": 1, "hit_by_pitch": 0, "strikeouts": 0, "date": date,
            }
        # A modest, unremarkable game: 1-for-4 with a strikeout.
        return {
            "plate_appearances": 4, "hits": 1, "doubles": 0, "triples": 0,
            "home_runs": 0, "walks": 0, "hit_by_pitch": 0, "strikeouts": 1, "date": date,
        }

    def _dated_pitch_game(date):
        return {
            "plate_appearances": 24, "hits_against": 6, "doubles": 0, "triples": 0,
            "home_runs": 1, "walks_against": 2, "hit_batsmen": 0, "strikeouts": 6, "outs": 18,
            "date": date,
        }

    # Hitter 401's real game log: modest, unremarkable games before the
    # cutoff, a real hot streak (monster games) on/after it -- a real
    # live app call (no as_of_date) should reflect that hot streak; a
    # backtest AS OF the cutoff date must not see it at all.
    asof_hitter_logs = {
        401: [_dated_hit_game(f"2099-04-{i:02d}", hot=False) for i in range(1, 21)]
             + [_dated_hit_game(f"2099-08-{i:02d}", hot=True) for i in range(1, 21)],
    }
    for pid in list(range(402, 410)) + list(range(501, 510)):
        asof_hitter_logs[pid] = [_dated_hit_game(f"2099-04-{i:02d}", hot=False) for i in range(1, 21)]
    asof_pitcher_logs = {
        5001: [_dated_pitch_game(f"2099-04-{i:02d}") for i in range(1, 21)],
        5002: [_dated_pitch_game(f"2099-04-{i:02d}") for i in range(1, 21)],
    }

    async def fake_asof_game_log(player_id, season, group="hitting"):
        if group == "pitching":
            return asof_pitcher_logs.get(player_id, [])
        return asof_hitter_logs.get(player_id, [])

    mlb.get_player_game_log = fake_asof_game_log

    asof_slate = {
        "games": [
            {
                "in_slate": True,
                "home": _slate_side(9001, "HOM", list(range(401, 410)), 5001),
                "away": _slate_side(9002, "AWY", list(range(501, 510)), 5002),
            }
        ]
    }
    live_trials = await atbat_sim.simulate_slate_trials(asof_slate, VARIANCE_SEASON, num_trials=200, seed=21)
    asof_trials = await atbat_sim.simulate_slate_trials(
        asof_slate, VARIANCE_SEASON, num_trials=200, seed=21, as_of_date="2099-06-01"
    )
    live_mean = statistics_module.mean(live_trials[401])
    asof_mean = statistics_module.mean(asof_trials[401])
    check("simulate_slate_trials without as_of_date reflects a hitter's full game log, including "
          "a real hot streak far in the 'future' relative to a real backtest date -- proof this "
          "is the correct, normal live-app behavior (today has no future games to leak)",
          live_mean > asof_mean * 1.3, str((live_mean, asof_mean)))

    # included_game_pks, when given, is authoritative -- it must be able
    # to select a game with in_slate=False (or unset) entirely, since a
    # historical/backtest date never has a DK salary CSV loaded (the
    # only thing that ever sets in_slate=True) but still needs to be
    # simulatable by its real game_pk.
    no_dk_csv_slate = {
        "games": [
            {
                "game_pk": 777, "in_slate": False,
                "home": _slate_side(9001, "HOM", list(range(401, 410)), 5001),
                "away": _slate_side(9002, "AWY", list(range(501, 510)), 5002),
            }
        ]
    }
    try:
        await atbat_sim.simulate_slate_trials(no_dk_csv_slate, VARIANCE_SEASON, num_trials=5)
        check("simulate_slate_trials still requires in_slate=True with no included_game_pks given "
              "(the normal live-app default)", False, "no exception raised")
    except atbat_sim.SlateNotSimulatableError:
        check("simulate_slate_trials still requires in_slate=True with no included_game_pks given "
              "(the normal live-app default)", True, "")
    no_dk_csv_trials = await atbat_sim.simulate_slate_trials(
        no_dk_csv_slate, VARIANCE_SEASON, num_trials=5, included_game_pks=[777]
    )
    check("simulate_slate_trials simulates a game with in_slate=False when its game_pk is "
          "explicitly passed via included_game_pks -- needed to backtest a historical date, "
          "which never has a DK salary CSV (the only thing that sets in_slate) loaded",
          len(no_dk_csv_trials.get(401, [])) == 5, str(no_dk_csv_trials.get(401)))

    print("\nContest generator: at-bat engine wiring (evaluate_batch_simulated engine='atbat')")

    def _atbat_entry(hitter_ids):
        return {"players": [{"id": pid} for pid in [5001, 5002] + list(hitter_ids)]}

    atbat_entries = [_atbat_entry([401, 402, 403, 404, 405, 406, 407, 408])]
    atbat_field = [
        _atbat_entry([401, 402, 403, 404, 405, 406, 407, 409]),
        _atbat_entry([501, 502, 503, 504, 505, 506, 507, 508]),
    ]
    atbat_contest = dict(contest.CONTEST_TYPES["gpp_small"])

    atbat_eval = await contest.evaluate_batch_simulated(
        atbat_entries, atbat_field, atbat_contest, season=VARIANCE_SEASON,
        num_trials=25, seed=9, engine="atbat", slate=ready_slate,
    )
    check("evaluate_batch_simulated(engine='atbat') returns one real result per entry, genuinely "
          "driven by atbat_sim's simulated games rather than variance.py's bootstrap pools",
          len(atbat_eval["results"]) == 1 and isinstance(atbat_eval["results"][0]["roi_pct"], float),
          str(atbat_eval["results"][0]))

    try:
        await contest.evaluate_batch_simulated(
            atbat_entries, atbat_field, atbat_contest, season=VARIANCE_SEASON,
            num_trials=5, engine="atbat",
        )
        check("evaluate_batch_simulated(engine='atbat') requires slate -- raises ContestError without it",
              False, "no exception raised")
    except contest.ContestError:
        check("evaluate_batch_simulated(engine='atbat') requires slate -- raises ContestError without it",
              True, "")

    # Regression guard for a real bug: atbat_sim.SlateNotSimulatableError
    # is a plain Exception (atbat_sim.py has no dependency on contest.py),
    # so if _simulate_lineups_atbat() ever stops converting it to a
    # ContestError, this propagates uncaught all the way to the router,
    # which only catches ContestError -- surfacing as an opaque HTTP 500
    # instead of a clear 400 with the real reason. Caught live: a real
    # not-fully-confirmed slate 500'd instead of returning the "not
    # ready" message.
    try:
        await contest.evaluate_batch_simulated(
            atbat_entries, atbat_field, atbat_contest, season=VARIANCE_SEASON,
            num_trials=5, engine="atbat", slate=unconfirmed_slate,
        )
        check("a not-ready slate (atbat_sim.SlateNotSimulatableError) surfaces as a catchable "
              "contest.ContestError, not an uncaught exception that would 500 the request",
              False, "no exception raised")
    except contest.ContestError as e:
        check("a not-ready slate (atbat_sim.SlateNotSimulatableError) surfaces as a catchable "
              "contest.ContestError, not an uncaught exception that would 500 the request",
              "HOM" in str(e) and "AWY" in str(e), str(e))

    atbat_missing_player_entry = [_atbat_entry([401, 402, 403, 404, 405, 406, 407, 999999])]
    try:
        await contest.evaluate_batch_simulated(
            atbat_missing_player_entry, atbat_field, atbat_contest, season=VARIANCE_SEASON,
            num_trials=5, engine="atbat", slate=ready_slate,
        )
        check("evaluate_batch_simulated(engine='atbat') raises a clear ContestError for a player id "
              "not present anywhere in the slate's simulated results", False, "no exception raised")
    except contest.ContestError as e:
        check("evaluate_batch_simulated(engine='atbat') raises a clear ContestError for a player id "
              "not present anywhere in the slate's simulated results",
              "999999" in str(e), str(e))

    print("\nContest generator: simulated economics (contest.py evaluate_batch_simulated)")

    import numpy as np_test

    # Cross-check the vectorized per-trial "distinct rank" math against
    # a naive brute-force Python loop over trials -- the closed-form
    # cumulative-max recurrence is the one genuinely tricky piece of
    # Phase 5's math, so this verifies it directly rather than trusting
    # the derivation on paper.
    rank_rng = np_test.random.default_rng(11)
    ref_entry_sim = rank_rng.normal(50, 15, size=(4, 30))
    ref_field_sim = rank_rng.normal(50, 15, size=(6, 30))
    ref_field_size = 100
    ref_sample_size = ref_field_sim.shape[0]
    ref_num_entries, ref_num_trials = ref_entry_sim.shape

    ref_field_sorted = np_test.sort(ref_field_sim, axis=0)
    ref_beaten = np_test.empty((ref_num_entries, ref_num_trials), dtype=np_test.int64)
    for t in range(ref_num_trials):
        ref_beaten[:, t] = np_test.searchsorted(ref_field_sorted[:, t], ref_entry_sim[:, t], side="left")
    ref_pct_rank = np_test.clip(
        np_test.round((1 - ref_beaten / ref_sample_size) * ref_field_size), 1, ref_field_size
    ).astype(np_test.int64)
    ref_order = np_test.argsort(-ref_entry_sim, axis=0)
    ref_sorted_pct = np_test.take_along_axis(ref_pct_rank, ref_order, axis=0)
    ref_positions = np_test.arange(ref_num_entries)[:, None]
    ref_distinct_sorted = np_test.minimum(
        np_test.maximum.accumulate(ref_sorted_pct - ref_positions, axis=0) + ref_positions, ref_field_size
    )
    vectorized_rank = np_test.empty_like(ref_distinct_sorted)
    np_test.put_along_axis(vectorized_rank, ref_order, ref_distinct_sorted, axis=0)

    brute_rank = np_test.empty_like(vectorized_rank)
    for t in range(ref_num_trials):
        field_t = sorted(ref_field_sim[:, t].tolist())
        order_t = sorted(range(ref_num_entries), key=lambda i: -ref_entry_sim[i, t])
        prev = 0
        for i in order_t:
            beaten_i = sum(1 for fv in field_t if ref_entry_sim[i, t] > fv)
            pct = min(max(round((1 - beaten_i / ref_sample_size) * ref_field_size), 1), ref_field_size)
            rank = min(max(pct, prev + 1), ref_field_size)
            prev = rank
            brute_rank[i, t] = rank

    check("evaluate_batch_simulated's vectorized per-trial distinct-rank math matches a brute-force per-trial reference",
          (vectorized_rank == brute_rank).all(),
          str((vectorized_rank[:, 0].tolist(), brute_rank[:, 0].tolist())))

    async def fake_any_player_game_log(player_id, season, group="hitting"):
        if group == "pitching":
            return [
                {"game_date": f"2099-04-{i:02d}", "outs": 18, "strikeouts": 6, "wins": 0,
                 "earned_runs": 2, "hits_against": 6, "walks_against": 1, "hit_batsmen": 0,
                 "complete_games": 0, "shutouts": 0}
                for i in range(1, 21)
            ]
        return [
            {"game_date": f"2099-04-{i:02d}", "plate_appearances": 4, "hits": 1, "doubles": 0,
             "triples": 0, "home_runs": 0, "rbi": 1, "runs": 1, "walks": 0, "hit_by_pitch": 0,
             "stolen_bases": 0}
            for i in range(1, 21)
        ]

    mlb.get_player_game_log = fake_any_player_game_log

    sim_batch = await contest.build_contest_entries_simulated(
        mul_slate, "gpp_small", 10, season=2099, num_trials=300, sample_size=40, seed=31
    )
    check("build_contest_entries_simulated builds the requested number of entries",
          sim_batch["num_entries_built"] == 10, sim_batch["num_entries_built"])
    check("build_contest_entries_simulated reports num_trials actually run",
          sim_batch["num_trials"] == 300, sim_batch["num_trials"])
    cash_pcts = [r["cash_probability_pct"] for r in sim_batch["results"]]
    check("every entry's simulated cash probability is a valid percentage",
          all(0.0 <= c <= 100.0 for c in cash_pcts), str(cash_pcts))

    check("every simulated result carries a non-negative Monte Carlo standard error for its "
          "ROI -- the number that tells noise apart from signal in a top-heavy payout",
          all(r.get("roi_se_pct") is not None and r["roi_se_pct"] >= 0 for r in sim_batch["results"]),
          str([r.get("roi_se_pct") for r in sim_batch["results"][:5]]))

    check("every simulated result carries expected_field_dupes -- how many identical copies of "
          "this exact lineup the real field is expected to hold, whose payout shares it",
          all(r.get("expected_field_dupes") is not None and r["expected_field_dupes"] >= 0
              for r in sim_batch["results"]),
          str([r.get("expected_field_dupes") for r in sim_batch["results"][:5]]))

    # Projection-error machinery: implemented but deliberately gated at
    # 0 (see variance.PROJECTION_ERROR_STD's own comment for the
    # measured chalk-EV inflation that gates it) -- at 0 it must be an
    # exact no-op, and at a real sigma it must actually move outcomes.
    _pe_matrix = np_test.array([[10.0, 12.0, 8.0], [5.0, 5.0, 5.0]])
    check("apply_projection_error is an exact no-op at the gated sigma=0 default",
          variance.PROJECTION_ERROR_STD == 0.0
          and (variance.apply_projection_error(_pe_matrix, np_test.random.default_rng(1)) == _pe_matrix).all(),
          str(variance.PROJECTION_ERROR_STD))
    _pe_saved = variance.PROJECTION_ERROR_STD
    variance.PROJECTION_ERROR_STD = 0.2
    _pe_shifted = variance.apply_projection_error(_pe_matrix, np_test.random.default_rng(1))
    variance.PROJECTION_ERROR_STD = _pe_saved
    check("at a real sigma the injection genuinely shifts outcomes (additive on each player's "
          "own mean) and never below zero",
          (_pe_shifted != _pe_matrix).any() and (_pe_shifted >= 0).all(), str(_pe_shifted))

    check("results come back ranked by top-1% rate (ROI as tiebreak), not raw ROI -- per-lineup "
          "ROI is dominated by rare first-place hits, so ranking by it ranks luck",
          all(
              (a["top_1pct_pct"], a["roi_pct"]) >= (b["top_1pct_pct"], b["roi_pct"])
              for a, b in zip(sim_batch["results"], sim_batch["results"][1:])
          ),
          str([(r["top_1pct_pct"], r["roi_pct"]) for r in sim_batch["results"]]))

    # Determinism: the same seed must reproduce the identical batch --
    # entries, field AND sim draws -- while a different seed must not.
    sim_batch_again = await contest.build_contest_entries_simulated(
        mul_slate, "gpp_small", 10, season=2099, num_trials=300, sample_size=40, seed=31
    )
    check("the same seed reproduces the identical batch, bit for bit -- same entries in the "
          "same order with the same simulated numbers, so the table doesn't reshuffle when "
          "nothing changed",
          [frozenset(p["id"] for p in e["players"]) for e in sim_batch["entries"]]
          == [frozenset(p["id"] for p in e["players"]) for e in sim_batch_again["entries"]]
          and [r["roi_pct"] for r in sim_batch["results"]]
          == [r["roi_pct"] for r in sim_batch_again["results"]], "")
    sim_batch_other = await contest.build_contest_entries_simulated(
        mul_slate, "gpp_small", 10, season=2099, num_trials=300, sample_size=40, seed=32
    )
    check("a different seed produces a genuinely different draw (not a no-op parameter)",
          [r["roi_pct"] for r in sim_batch["results"]]
          != [r["roi_pct"] for r in sim_batch_other["results"]], "")
    # Payout is zero-inflated (most trials don't cash) with rare large
    # spikes when they do, so its mean can legitimately sit above the
    # 90th percentile -- p10 <= p90 (percentiles are self-consistent)
    # and every value is a non-negative real payout are what's actually
    # guaranteed, not p10 <= mean <= p90.
    check("every entry's simulated payout p10 <= p90, and all payout figures are non-negative",
          all(
              0 <= r["payout_p10"] <= r["payout_p90"] and r["expected_payout"] >= 0
              for r in sim_batch["results"]
          ),
          str([(r["payout_p10"], r["expected_payout"], r["payout_p90"]) for r in sim_batch["results"]]))
    check("every entry's simulated_points_floor/ceiling are the FLOOR_CEILING_PERCENTILE-th/"
          "(100-that)-th percentile across all trials (not the true min/max -- a single "
          "freak-outcome trial reads as implausible even under a well-calibrated model), so "
          "floor <= p10 <= mean <= p90 <= ceiling still holds for every entry",
          all(
              r["simulated_points_floor"] <= r["simulated_points_p10"] <= r["simulated_points_mean"]
              <= r["simulated_points_p90"] <= r["simulated_points_ceiling"]
              for r in sim_batch["results"]
          ),
          str([(r["simulated_points_floor"], r["simulated_points_p10"], r["simulated_points_mean"],
                r["simulated_points_p90"], r["simulated_points_ceiling"]) for r in sim_batch["results"][:2]]))

    # Direct, exact proof (not just ordering) that floor/ceiling are
    # really the 5th/95th percentile and not still the true min/max --
    # a known, fixed simulated-trials array with a controlled min (10.0)
    # and max (200.0) lets floor/ceiling be checked against an exact
    # expected value instead of just "some number in between".
    import numpy as np_floor_test

    fixed_sim_array = np_floor_test.array([
        [float(v) for v in range(10, 201, 10)],  # 10.0 .. 200.0, step 10 -- 20 values
    ])

    async def fake_player_pools_floor_test(lineups, season):
        return {}

    def fake_simulate_batch_floor_test(entries, player_pools, *, num_trials, seed=None):
        return fixed_sim_array

    original_player_pools_floor_test = variance.player_pools_for_entries
    original_simulate_batch_floor_test = variance.simulate_batch
    variance.player_pools_for_entries = fake_player_pools_floor_test
    variance.simulate_batch = fake_simulate_batch_floor_test

    floor_eval = await contest.evaluate_batch_simulated(
        [{"players": [{"id": 1}]}], [{"players": [{"id": 2}]}],
        dict(contest.CONTEST_TYPES["gpp_small"]), season=2026, num_trials=20,
    )

    variance.player_pools_for_entries = original_player_pools_floor_test
    variance.simulate_batch = original_simulate_batch_floor_test

    expected_floor = round(float(np_floor_test.percentile(fixed_sim_array[0], contest.FLOOR_CEILING_PERCENTILE)), 2)
    expected_ceiling = round(
        float(np_floor_test.percentile(fixed_sim_array[0], 100 - contest.FLOOR_CEILING_PERCENTILE)), 2
    )
    floor_result = floor_eval["results"][0]
    check("simulated_points_floor/ceiling exactly match the 5th/95th percentile of a known fixture "
          "with a controlled true min (10.0) and max (200.0) -- and land strictly inside that true "
          "range, proving they're no longer the literal min/max",
          floor_result["simulated_points_floor"] == expected_floor
          and floor_result["simulated_points_ceiling"] == expected_ceiling
          and 10.0 < floor_result["simulated_points_floor"] < floor_result["simulated_points_ceiling"] < 200.0,
          str((floor_result["simulated_points_floor"], expected_floor,
               floor_result["simulated_points_ceiling"], expected_ceiling)))

    check("build_contest_entries_simulated's avg_cash_probability_pct matches its own per-entry results",
          abs(sim_batch["summary"]["avg_cash_probability_pct"] - sum(cash_pcts) / len(cash_pcts)) < 0.15,
          str((sim_batch["summary"]["avg_cash_probability_pct"], round(sum(cash_pcts) / len(cash_pcts), 1))))

    check("every entry reports first_place_pct <= top_1pct_pct <= top_10pct_pct <= cash_probability_pct "
          "(a stricter finish is never more common than a looser one)",
          all(
              0 <= r["first_place_pct"] <= r["top_1pct_pct"] <= r["top_10pct_pct"] <= r["cash_probability_pct"]
              for r in sim_batch["results"]
          ),
          str([(r["first_place_pct"], r["top_1pct_pct"], r["top_10pct_pct"], r["cash_probability_pct"])
               for r in sim_batch["results"]]))
    check("every entry's roi_pct matches (expected_payout - entry_fee) / entry_fee",
          all(
              abs(r["roi_pct"] - round((r["expected_payout"] - sim_batch["contest"]["entry_fee"])
                                        / sim_batch["contest"]["entry_fee"] * 100, 1)) < 0.05
              for r in sim_batch["results"]
          ),
          str([r["roi_pct"] for r in sim_batch["results"]]))
    check("build_contest_entries_simulated's summary carries avg_first_place_pct/avg_top_1pct_pct/"
          "avg_top_10pct_pct/avg_roi_pct",
          all(
              key in sim_batch["summary"]
              for key in ("avg_first_place_pct", "avg_top_1pct_pct", "avg_top_10pct_pct", "avg_roi_pct")
          ),
          str(sim_batch["summary"]))

    print("\nIndividual player ROI in the sim (field_exposure's new results param)")

    check("sim_batch's exposure entries carry avg_roi_pct now that results are passed through",
          all("avg_roi_pct" in e for e in sim_batch["exposure"]), str(sim_batch["exposure"][:2]))

    # Direct, isolated unit test of field_exposure()'s new results-folding
    # math -- 3 lineups, 2 sharing player 501, with known roi_pct values.
    fe_field = [
        {"players": [{"id": 501, "name": "P501", "team": "T"}, {"id": 502, "name": "P502", "team": "T"}]},
        {"players": [{"id": 501, "name": "P501", "team": "T"}, {"id": 503, "name": "P503", "team": "T"}]},
        {"players": [{"id": 504, "name": "P504", "team": "T"}]},
    ]
    fe_results = [{"roi_pct": 40.0}, {"roi_pct": -20.0}, {"roi_pct": 10.0}]
    fe_exposure = contest.field_exposure(fe_field, results=fe_results)
    fe_by_id = {e["id"]: e for e in fe_exposure}
    check("field_exposure's avg_roi_pct is the exact mean of roi_pct across every lineup containing "
          "that player (player 501 is in 2 lineups: (40 + -20) / 2 = 10.0)",
          fe_by_id[501]["avg_roi_pct"] == 10.0, str(fe_by_id[501]))
    check("a player appearing in only one lineup shows that lineup's own roi_pct exactly",
          fe_by_id[502]["avg_roi_pct"] == 40.0
          and fe_by_id[503]["avg_roi_pct"] == -20.0
          and fe_by_id[504]["avg_roi_pct"] == 10.0,
          str(fe_exposure))
    check("field_exposure without a results argument (existing callers, e.g. build_contest_field's "
          "opponent field) never adds avg_roi_pct -- unchanged behavior",
          "avg_roi_pct" not in contest.field_exposure(fe_field)[0], str(contest.field_exposure(fe_field)[0]))

    print("\nPercent-to-first override wired into the simulated payout curve (build_contest_entries_simulated)")

    check("with no first_place_pct override, the response echoes the contest preset's own value "
          "(15.0 for gpp_small)",
          sim_batch["first_place_pct"] == 15.0, sim_batch["first_place_pct"])

    sim_low_first = await contest.build_contest_entries_simulated(
        mul_slate, "gpp_small", 10, season=2099, num_trials=300, sample_size=40, seed=31, first_place_pct=5.0,
    )
    sim_high_first = await contest.build_contest_entries_simulated(
        mul_slate, "gpp_small", 10, season=2099, num_trials=300, sample_size=40, seed=31, first_place_pct=35.0,
    )
    check("first_place_pct override is echoed back exactly in the response",
          sim_low_first["first_place_pct"] == 5.0 and sim_high_first["first_place_pct"] == 35.0,
          str((sim_low_first["first_place_pct"], sim_high_first["first_place_pct"])))
    check("changing first_place_pct genuinely changes the simulated payout curve -- same seed/entries/"
          "trials otherwise, but per-entry expected_payout differs between a 5% and a 35% percent-to-first",
          sorted(r["expected_payout"] for r in sim_low_first["results"]) !=
          sorted(r["expected_payout"] for r in sim_high_first["results"]),
          str((sorted(r["expected_payout"] for r in sim_low_first["results"]),
               sorted(r["expected_payout"] for r in sim_high_first["results"]))))
    check("both runs still sum to (approximately) the same real prize_pool -- percent-to-first "
          "redistributes the SAME pool, it doesn't change its size",
          sim_low_first["prize_pool"] == sim_high_first["prize_pool"] == sim_batch["prize_pool"],
          str((sim_low_first["prize_pool"], sim_high_first["prize_pool"], sim_batch["prize_pool"])))

    print("\nField-calibration output split: field_baseline (contest.py _field_baseline)")

    # A zero-skill entry's expected ROI/cash rate is a closed-form fact
    # derivable directly from the contest's own numbers -- no
    # simulation needed. Hand-checkable case: a 1,000-entry contest,
    # $10 entry, $8,000 real prize pool (not the standard rake formula,
    # proving this reads the REAL prize_pool rather than re-deriving
    # one), 20% payout_pct.
    baseline = contest._field_baseline(0.20, 8000.0, 10.0, 1000)
    check("_field_baseline's avg_cash_probability_pct is exactly payout_pct as a percentage",
          baseline["avg_cash_probability_pct"] == 20.0, str(baseline))
    check("_field_baseline's avg_roi_pct is exactly (prize_pool / (field_size * entry_fee) - 1) * 100 "
          "-- (8000 / 10000 - 1) * 100 = -20.0",
          baseline["avg_roi_pct"] == -20.0, str(baseline))

    # Wired end-to-end: gpp_small's real rake-derived prize pool should
    # reproduce -RAKE_PCT*100 exactly (this is the same closed-form fact
    # the rake sanity check above verified empirically via full Monte
    # Carlo simulation -- here it's read straight off the contest's own
    # numbers, no simulation at all, and should match to the cent).
    check("build_contest_entries_simulated's field_baseline reports the real gpp_small payout_pct "
          "(20%) as avg_cash_probability_pct",
          sim_batch["field_baseline"]["avg_cash_probability_pct"] == 20.0,
          str(sim_batch["field_baseline"]))
    check("build_contest_entries_simulated's field_baseline reports exactly -RAKE_PCT*100% avg_roi_pct "
          "for the standard rake-derived prize pool -- a random zero-skill entry's true expected ROI",
          sim_batch["field_baseline"]["avg_roi_pct"] == -contest.RAKE_PCT * 100,
          str(sim_batch["field_baseline"]))
    check("the entries batch's own avg_roi_pct is reported ALONGSIDE field_baseline, not folded "
          "into one blended number -- the whole point of the split",
          "avg_roi_pct" in sim_batch["summary"] and "avg_roi_pct" in sim_batch["field_baseline"]
          and sim_batch["summary"]["avg_roi_pct"] != sim_batch["field_baseline"]["avg_roi_pct"],
          str((sim_batch["summary"]["avg_roi_pct"], sim_batch["field_baseline"]["avg_roi_pct"])))

    print("\nPost-hoc portfolio shaping: reshape_batch (contest.py) -- exposure caps + ROI boost")

    def _reshape_entry(pid_list, roi):
        return (
            {"players": [{"id": pid, "name": f"P{pid}", "team": "T"} for pid in pid_list]},
            {"roi_pct": roi, "lineup_index": 0},
        )

    r_e1, r_r1 = _reshape_entry([201, 202, 203], 50.0)
    r_e2, r_r2 = _reshape_entry([201, 204, 205], 30.0)
    r_e3, r_r3 = _reshape_entry([206, 207, 208], 10.0)
    r_e4, r_r4 = _reshape_entry([201, 209, 210], -5.0)
    reshape_entries = [r_e1, r_e2, r_e3, r_e4]
    reshape_results = [r_r1, r_r2, r_r3, r_r4]

    plain = contest.reshape_batch(reshape_entries, reshape_results)
    check("reshape_batch with no boosts/caps keeps every entry",
          plain["num_kept"] == 4 and plain["num_dropped"] == 0, str(plain["num_kept"]))
    check("reshape_batch with no boosts/caps preserves roi_pct-descending order",
          [r["roi_pct"] for r in plain["results"]] == [50.0, 30.0, 10.0, -5.0],
          str([r["roi_pct"] for r in plain["results"]]))
    check("reshape_batch's adjusted_roi_pct equals roi_pct exactly when no boosts are given",
          all(r["adjusted_roi_pct"] == r["roi_pct"] for r in plain["results"]), str(plain["results"]))

    boosted = contest.reshape_batch(reshape_entries, reshape_results, roi_boosts={206: 50.0})
    check("roi_boosts changes sort order -- a boosted player's lineup can jump ahead of a "
          "higher-roi one that doesn't have them (206's lineup: 10 + 50 = 60, now ranked 1st)",
          boosted["results"][0]["adjusted_roi_pct"] == 60.0
          and boosted["entries"][0]["players"][0]["id"] == 206,
          str([(e["players"][0]["id"], r["adjusted_roi_pct"])
               for e, r in zip(boosted["entries"], boosted["results"])]))
    check("roi_boosts never modifies the real roi_pct, only adjusted_roi_pct",
          boosted["results"][0]["roi_pct"] == 10.0, str(boosted["results"][0]))

    neg_boosted = contest.reshape_batch(reshape_entries, reshape_results, roi_boosts={201: -100.0})
    check("a negative roi_boost correctly REDUCES adjusted_roi_pct (additive, not multiplicative -- "
          "avoids the sign-flip bug a multiplicative % would cause on already-negative real ROI, "
          "e.g. -5% roi * 1.2 would perversely become MORE negative under a naive +20% boost)",
          neg_boosted["results"][0]["adjusted_roi_pct"] == 10.0
          and neg_boosted["entries"][0]["players"][0]["id"] == 206,
          str([(e["players"][0]["id"], r["adjusted_roi_pct"])
               for e, r in zip(neg_boosted["entries"], neg_boosted["results"])]))

    trimmed = contest.reshape_batch(reshape_entries, reshape_results, target_count=2)
    check("target_count keeps only the top N post-boost entries",
          trimmed["num_kept"] == 2 and trimmed["num_dropped"] == 2
          and [r["roi_pct"] for r in trimmed["results"]] == [50.0, 30.0],
          str(trimmed))

    # Player 201 appears in 3 of the 4 entries (e1, e2, e4), sorted
    # 50/30/-5 by roi_pct. A 25% cap against a 4-entry final portfolio
    # allows only 1 of those 3 -- e1 (highest-ranked) survives, e2 and
    # e4 get dropped once 201 hits its cap, and the walk keeps going
    # rather than stopping early (e3, with no 201 at all, still gets
    # kept).
    capped = contest.reshape_batch(reshape_entries, reshape_results, max_exposure_pct=25.0)
    check("max_exposure_pct drops entries once a player would exceed the cap relative to the "
          "final kept count, without stopping the walk -- a later cap-respecting entry still "
          "gets kept",
          capped["num_kept"] == 2 and [r["roi_pct"] for r in capped["results"]] == [50.0, 10.0],
          str(capped))
    check("reshape_batch's exposure report reflects only the kept entries, not the original batch",
          all(e["count"] <= capped["num_kept"] for e in capped["exposure"]), str(capped["exposure"]))

    overridden = contest.reshape_batch(
        reshape_entries, reshape_results, max_exposure_pct=25.0, player_exposure_caps={201: 100.0}
    )
    check("player_exposure_caps overrides max_exposure_pct for a specific player -- a high "
          "override lets that player's lineups all survive despite a tight global cap",
          overridden["num_kept"] == 4, str(overridden["num_kept"]))

    print("\nPost-hoc portfolio shaping: reshape_batch Filters -- stack team / player combo / stack shape")

    def _filter_entry(player_specs, roi, stack_type=""):
        return (
            {
                "players": [{"id": pid, "name": f"P{pid}", "team": team} for pid, team in player_specs],
                "stack_type": stack_type,
            },
            {"roi_pct": roi, "lineup_index": 0},
        )

    f_e1, f_r1 = _filter_entry([(301, "CLE"), (311, "CLE"), (312, "CLE")], 40.0, "3")
    f_e2, f_r2 = _filter_entry([(302, "NYY"), (321, "NYY")], 35.0, "2")
    f_e3, f_r3 = _filter_entry([(301, "CLE"), (331, "BOS")], 20.0, "")
    f_e4, f_r4 = _filter_entry([(303, "BOS"), (341, "BOS"), (342, "BOS")], 10.0, "3")
    filter_entries = [f_e1, f_e2, f_e3, f_e4]
    filter_results = [f_r1, f_r2, f_r3, f_r4]

    require_cle = contest.reshape_batch(filter_entries, filter_results, require_teams=["CLE"])
    check("require_teams keeps only entries rostering a player from that team (e1, e3 have CLE)",
          [r["roi_pct"] for r in require_cle["results"]] == [40.0, 20.0], str(require_cle["results"]))
    check("require_teams reports the rest as num_filtered_out, not num_dropped",
          require_cle["num_filtered_out"] == 2 and require_cle["num_dropped"] == 0, str(require_cle))

    exclude_cle = contest.reshape_batch(filter_entries, filter_results, exclude_teams=["CLE"])
    check("exclude_teams keeps only entries with NO player from that team (e2, e4 have no CLE)",
          [r["roi_pct"] for r in exclude_cle["results"]] == [35.0, 10.0], str(exclude_cle["results"]))

    require_301 = contest.reshape_batch(filter_entries, filter_results, require_player_ids=[301])
    check("require_player_ids keeps only entries rostering that exact player (301 is in e1, e3)",
          [r["roi_pct"] for r in require_301["results"]] == [40.0, 20.0], str(require_301["results"]))

    exclude_301 = contest.reshape_batch(filter_entries, filter_results, exclude_player_ids=[301])
    check("exclude_player_ids drops any entry rostering that player (e1, e3 dropped)",
          [r["roi_pct"] for r in exclude_301["results"]] == [35.0, 10.0], str(exclude_301["results"]))

    stack3_only = contest.reshape_batch(filter_entries, filter_results, stack_types=["3"])
    check("stack_types keeps only entries with a matching named stack shape (e1, e4 are '3')",
          [r["roi_pct"] for r in stack3_only["results"]] == [40.0, 10.0], str(stack3_only["results"]))

    combo = contest.reshape_batch(filter_entries, filter_results, stack_types=["3"], require_teams=["CLE"])
    check("multiple filters combine as AND -- only e1 is both a '3' stack AND has CLE",
          [r["roi_pct"] for r in combo["results"]] == [40.0], str(combo["results"]))

    # target is computed against the FILTERED pool (2 entries: e1, e3),
    # not the original batch of 4 -- a 50% cap means exactly 1 of those
    # 2 entries can carry player 301 (1/2 = 50%, right at the cap;
    # a 2nd occurrence would be 2/2 = 100%, over it). If target were
    # miscomputed against the original 4, 50% would instead allow 2
    # occurrences (2/4), and both e1 and e3 would wrongly survive.
    filter_then_cap = contest.reshape_batch(
        filter_entries, filter_results, require_teams=["CLE"], max_exposure_pct=50.0,
    )
    check("target_count/exposure caps apply to the FILTERED pool, not the original batch -- "
          "player 301 (in both remaining CLE entries) hits a 50% cap against just those 2, "
          "so only the higher-roi one (e1) survives",
          filter_then_cap["num_kept"] == 1 and filter_then_cap["results"][0]["roi_pct"] == 40.0,
          str(filter_then_cap))

    try:
        contest.reshape_batch(filter_entries, filter_results, require_teams=["NOTATEAM"])
        check("reshape_batch raises when a filter matches nothing at all", False)
    except contest.ContestError:
        check("reshape_batch raises when a filter matches nothing at all", True)

    no_filter = contest.reshape_batch(filter_entries, filter_results)
    check("no filters given: num_filtered_out is 0 and every entry is a real candidate, "
          "same behavior as before Filters existed",
          no_filter["num_filtered_out"] == 0 and no_filter["num_kept"] == 4, str(no_filter))

    try:
        contest.reshape_batch([], [])
        check("reshape_batch rejects an empty batch", False)
    except contest.ContestError:
        check("reshape_batch rejects an empty batch", True)

    try:
        contest.reshape_batch(reshape_entries, reshape_results[:2])
        check("reshape_batch rejects mismatched entries/results lengths", False)
    except contest.ContestError:
        check("reshape_batch rejects mismatched entries/results lengths", True)

    real_reshape = contest.reshape_batch(sim_batch["entries"], sim_batch["results"], target_count=5)
    check("reshape_batch wired against a real simulated batch keeps exactly target_count entries "
          "(no cap tight enough here to prevent filling it)",
          real_reshape["num_kept"] == 5, str(real_reshape["num_kept"]))

    sim_sort_keys = [(r["top_1pct_pct"], r["roi_pct"]) for r in sim_batch["results"]]
    check("build_contest_entries_simulated ranks results by top-1% rate (ROI as tiebreak) -- "
          "per-lineup ROI is dominated by rare first-place hits, so raw-ROI ordering ranks "
          "this run's luck rather than the build's real spike potential",
          sim_sort_keys == sorted(sim_sort_keys, reverse=True), str(sim_sort_keys))
    check("build_contest_entries_simulated's entries/results stay index-aligned after sorting "
          "(each result's lineup_index matches its position)",
          [r["lineup_index"] for r in sim_batch["results"]] == list(range(len(sim_batch["results"]))),
          str([r["lineup_index"] for r in sim_batch["results"]]))

    sim_csv = lineup_export.lineups_to_csv(sim_batch["entries"], results=sim_batch["results"])
    sim_csv_rows = list(csv_module.DictReader(io_module.StringIO(sim_csv)))
    check("lineups_to_csv includes the simulated result columns (not the deterministic rank/cashing ones) "
          "when given simulated results",
          "roi_pct" in sim_csv_rows[0] and "cash_probability_pct" in sim_csv_rows[0]
          and "estimated_rank" not in sim_csv_rows[0],
          str(sorted(sim_csv_rows[0].keys())))
    check("the simulated CSV's roi_pct column matches the JSON response, in the same (sorted) row order",
          [row["roi_pct"] for row in sim_csv_rows] == [str(r["roi_pct"]) for r in sim_batch["results"]],
          str(([row["roi_pct"] for row in sim_csv_rows], [r["roi_pct"] for r in sim_batch["results"]])))

    print("\nContest generator: self-play mode -- the whole batch simulated as its own contest (contest.py evaluate_field_mirrored)")

    # A batch where two entries share almost the same stack (same team,
    # 7 of 8 hitters overlap) should show strongly correlated simulated
    # results -- in whatever trial that shared stack runs hot, BOTH
    # entries do well together, and in whatever trial it runs cold, both
    # do poorly together. Two entries built from entirely separate,
    # non-overlapping teams have no shared element and should stay close
    # to independent. This is the literal mechanic behind self-play mode:
    # lineups sharing correlated players/stacks cluster together in the
    # standings instead of being scored in isolation against an unrelated
    # field.
    # MIN_GAMES_FULL_TRUST["hitter"] is 50 -- fake_any_player_game_log's
    # 20-game hitting log (used just above) is a THIN sample, which
    # makes player_outcome_pool() blend in the shared position pool via
    # UNSEEDED randomness (a real, already-identified flakiness source
    # in this codebase). A dedicated 60-game full-trust fixture here
    # keeps this section's pool draws deterministic (own games only, no
    # blend) so the correlation numbers below don't vary run to run.
    async def fake_full_trust_game_log(player_id, season, group="hitting"):
        # Alternates a big game with a dud, same "genuine game-to-game
        # spread" shape as this file's own [0.0, 20.0] * N sim_pools
        # fixtures elsewhere -- a constant per-game line (no spread at
        # all) would make every player's outcome pool a near-constant,
        # which trivially correlates every lineup with every other one
        # regardless of shared players/teams and defeats the point of
        # this test.
        if group == "pitching":
            return [
                {
                    "game_date": f"2099-{4 + (i - 1) // 28:02d}-{(i - 1) % 28 + 1:02d}",
                    "outs": 18, "strikeouts": 9 if i % 2 == 0 else 3, "wins": 1 if i % 2 == 0 else 0,
                    "earned_runs": 1 if i % 2 == 0 else 4, "hits_against": 4 if i % 2 == 0 else 8,
                    "walks_against": 1, "hit_batsmen": 0, "complete_games": 0, "shutouts": 0,
                }
                for i in range(1, 21)
            ]
        return [
            {
                "game_date": f"2099-{4 + (i - 1) // 28:02d}-{(i - 1) % 28 + 1:02d}",
                "plate_appearances": 4, "hits": 3 if i % 2 == 0 else 0, "doubles": 1 if i % 2 == 0 else 0,
                "triples": 0, "home_runs": 1 if i % 2 == 0 else 0, "rbi": 3 if i % 2 == 0 else 0,
                "runs": 2 if i % 2 == 0 else 0, "walks": 0, "hit_by_pitch": 0, "stolen_bases": 0,
            }
            for i in range(1, 61)
        ]

    mlb.get_player_game_log = fake_full_trust_game_log

    twin_shared_ids = list(range(97001, 97008))  # 7 hitters shared by both TWIN entries
    twin1_unique_id, twin2_unique_id = 97008, 97009
    loner1_hitter_ids = list(range(97201, 97209))
    loner2_hitter_ids = list(range(97211, 97219))

    def _flat_entry(pitcher_ids, hitter_ids, team):
        return {
            "salary_used": 50000, "projected_points": 100.0, "total_ownership_pct": 0.0,
            "players": [
                *[_sim_player(pid, None) for pid in pitcher_ids],
                *[_sim_player(pid, team) for pid in hitter_ids],
            ],
        }

    twin1_entry = _flat_entry([97101, 97102], [*twin_shared_ids, twin1_unique_id], "TWINTEAM")
    twin2_entry = _flat_entry([97103, 97104], [*twin_shared_ids, twin2_unique_id], "TWINTEAM")
    loner1_entry = _flat_entry([97301, 97302], loner1_hitter_ids, "LONERTEAM1")
    loner2_entry = _flat_entry([97303, 97304], loner2_hitter_ids, "LONERTEAM2")

    self_play_field = [twin1_entry, twin2_entry, loner1_entry, loner2_entry]
    self_play_pools = await variance.player_pools_for_entries(self_play_field, 2099)
    self_play_sim = variance.simulate_batch(self_play_field, self_play_pools, num_trials=4000, seed=606)
    twin_corr = float(np_test.corrcoef(self_play_sim[0], self_play_sim[1])[0, 1])
    loner_corr = float(np_test.corrcoef(self_play_sim[2], self_play_sim[3])[0, 1])
    check("two entries sharing most of their stack show strongly correlated simulated results",
          twin_corr > 0.6, str(round(twin_corr, 3)))
    check("two entries with completely separate, unrelated stacks stay close to independent",
          abs(loner_corr) < 0.2, str(round(loner_corr, 3)))
    check("the shared-stack pair correlates far more than the unrelated pair -- proof shared players/stacks "
          "cluster together in the simulation instead of being scored as if independent",
          twin_corr > loner_corr + 0.4, str((round(twin_corr, 3), round(loner_corr, 3))))

    # Left at the preset's real field_size (500) rather than shrunk to
    # match the 4-lineup sample -- at field_size=4 paid_count/top_1pct/
    # top_10pct thresholds all collapse to 1, making first_place_pct,
    # top_1pct_pct, top_10pct_pct and cash_probability_pct mathematically
    # identical but independently rounded to different decimal
    # precision (1 vs 2 places), which can flip their strict ordering by
    # a rounding artifact alone -- not a real invariant violation. A
    # realistic field_size keeps the thresholds meaningfully distinct.
    self_play_contest = dict(contest.CONTEST_TYPES["gpp_small"])
    mirrored = await contest.evaluate_field_mirrored(
        self_play_field, self_play_contest, season=2099, num_trials=4000, seed=606,
    )
    check("evaluate_field_mirrored on a correlated self-play batch returns one result per lineup",
          len(mirrored["results"]) == 4, str(len(mirrored["results"])))
    # A tiny 4-lineup sample projected onto a 500-entry field lands each
    # lineup's rank in a specific trial on one of only 4 possible real
    # ranks -- almost always making first/top-1%/top-10%/cash the exact
    # same underlying indicator (all thresholds are far above rank 1,
    # the only achievable rank here), just independently rounded to
    # different decimal precision (2 places vs 1). That can flip their
    # strict ordering by a rounding artifact alone, so a small tolerance
    # (not a strict <=) is what's actually guaranteed at this sample size
    # -- the real invariant is already covered at production scale by
    # the sim_batch checks above.
    check("evaluate_field_mirrored's self-play results satisfy first<=top1<=top10<=cash within rounding tolerance",
          all(
              0 <= r["first_place_pct"] <= r["top_1pct_pct"] + 0.1
              and r["top_1pct_pct"] <= r["top_10pct_pct"] + 0.1
              and r["top_10pct_pct"] <= r["cash_probability_pct"] + 0.1
              and r["cash_probability_pct"] <= 100.0
              for r in mirrored["results"]
          ),
          str(mirrored["results"]))

    sim_entries_self_play = await contest.build_contest_entries_simulated(
        mul_slate, "gpp_small", 10, season=2099, num_trials=300, seed=31, self_play=True
    )
    check("build_contest_entries_simulated(self_play=True) builds the requested number of entries",
          sim_entries_self_play["num_entries_built"] == 10, sim_entries_self_play["num_entries_built"])
    check("build_contest_entries_simulated(self_play=True) reports self_play=True on the response",
          sim_entries_self_play["self_play"] is True, sim_entries_self_play.get("self_play"))
    check("build_contest_entries_simulated(self_play=True)'s sample_size matches the entries actually built -- "
          "the batch IS the simulated field, not ranked against a separately-sampled one",
          sim_entries_self_play["sample_size"] == sim_entries_self_play["num_entries_built"],
          str((sim_entries_self_play["sample_size"], sim_entries_self_play["num_entries_built"])))
    self_play_roi_order = [r["roi_pct"] for r in sim_entries_self_play["results"]]
    self_play_keys = [(r["top_1pct_pct"], r["roi_pct"]) for r in sim_entries_self_play["results"]]
    check("build_contest_entries_simulated(self_play=True) still ranks by top-1% then ROI",
          self_play_keys == sorted(self_play_keys, reverse=True), str(self_play_keys))
    check("build_contest_entries_simulated(self_play=True)'s cash probabilities are all valid percentages",
          all(0.0 <= r["cash_probability_pct"] <= 100.0 for r in sim_entries_self_play["results"]),
          str([r["cash_probability_pct"] for r in sim_entries_self_play["results"]]))
    check("build_contest_entries_simulated's default (self_play unset) mode is unaffected by the self_play "
          "branch -- still reports self_play=False",
          sim_batch["self_play"] is False, sim_batch.get("self_play"))

    print("\nSimulator: pricing a contest that was already built (simulate_contest_batch)")

    _built = contest.build_contest_lineups(mul_slate, "gpp_small", 100, seed=5)
    _built_entries = _built["entries"]

    priced = await contest.simulate_contest_batch(
        _built_entries, _built["contest"], season=2099, num_trials=200,
        entry_fee=10.0, contest_type="gpp_small", seed=3,
    )
    check("simulate_contest_batch prices an already-built contest without rebuilding a lineup -- "
          "the same entries come back, now with real simulated results",
          priced["num_entries_built"] == len(_built_entries)
          and len(priced["results"]) == len(_built_entries),
          str((priced["num_entries_built"], len(priced["results"]))))
    check("simulate_contest_batch defaults to ranking the contest against ITSELF -- the generator "
          "builds the whole field, so there's no second population to invent",
          priced["self_play"] is True, str(priced["self_play"]))

    # Entry cost is the load-bearing input: the prize pool is field_size
    # x entry_fee less rake, so doubling the fee has to double the pool.
    dearer = await contest.simulate_contest_batch(
        _built_entries, _built["contest"], season=2099, num_trials=200,
        entry_fee=20.0, contest_type="gpp_small", seed=3,
    )
    check("the entry cost given to the simulator sets the prize pool -- doubling the fee doubles "
          "the pool, which is why it's a simulator input and not a build one",
          abs(dearer["prize_pool"] - 2 * priced["prize_pool"]) < 0.02,
          str((priced["prize_pool"], dearer["prize_pool"])))
    check("...and it's the fee the results are actually priced against, not the preset's own",
          priced["contest"]["entry_fee"] == 10.0 and dearer["contest"]["entry_fee"] == 20.0,
          str((priced["contest"]["entry_fee"], dearer["contest"]["entry_fee"])))

    flatter = await contest.simulate_contest_batch(
        _built_entries, _built["contest"], season=2099, num_trials=200,
        entry_fee=10.0, first_place_pct=5.0, contest_type="gpp_small", seed=3,
    )
    check("a flatter percent-to-first spreads the same prize pool further down the payout curve, "
          "so more of the contest cashes for something",
          flatter["first_place_pct"] == 5.0
          and flatter["summary"]["total_expected_payout"] > 0,
          str((flatter["first_place_pct"], flatter["summary"]["total_expected_payout"])))

    # A contest bigger than MAX_SAMPLE_SIZE gets simulated as a slice of
    # itself, projected back onto the real field size -- said out loud
    # via num_entries_simulated rather than silently truncated.
    _saved_sample = contest.MAX_SAMPLE_SIZE
    contest.MAX_SAMPLE_SIZE = 20
    try:
        sliced = await contest.simulate_contest_batch(
            _built_entries, _built["contest"], season=2099, num_trials=200,
            entry_fee=10.0, contest_type="gpp_small", seed=3,
        )
    finally:
        contest.MAX_SAMPLE_SIZE = _saved_sample
    check("a contest too big to simulate whole is simulated as a slice of itself and says so, "
          "while still being ranked against its real full field size",
          sliced["num_entries_simulated"] == 20 and sliced["field_size"] == 100,
          str((sliced["num_entries_simulated"], sliced["field_size"])))

    try:
        await contest.simulate_contest_batch([], _built["contest"], season=2099)
        check("simulate_contest_batch refuses an empty batch rather than simulating nothing", False)
    except contest.ContestError:
        check("simulate_contest_batch refuses an empty batch rather than simulating nothing", True)


    print("\nBlock-averaged payout smoothing (contest.py _block_average_payouts)")

    # A tiny hand-checkable curve: 10 real ranks paying out
    # 100,80,60,...,0 (nothing beyond rank 5), smoothed onto just 2
    # sampled "blocks" -- block 1 covers real ranks 1-5 (mean 60), block
    # 2 covers ranks 6-10 (mean 0). boundaries=[1, 6] (1-indexed block
    # starts), matching the exact shape evaluate_field_mirrored's own
    # real_ranks_by_k produces.
    bp_curve = np_test.array([100.0, 80.0, 60.0, 40.0, 20.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    bp_smoothed = contest._block_average_payouts(bp_curve, np_test.array([1, 6]), 10)
    check("_block_average_payouts replaces every rank in a block with that block's own mean",
          list(bp_smoothed) == [60.0] * 5 + [0.0] * 5, list(bp_smoothed))
    check("_block_average_payouts preserves the curve's total sum -- a pure within-block "
          "redistribution, never gaining or losing total payout mass",
          abs(bp_smoothed.sum() - bp_curve.sum()) < 1e-9,
          str((bp_smoothed.sum(), bp_curve.sum())))
    check("_block_average_payouts is a no-op once boundaries already cover every real rank "
          "(no compression to correct for)",
          list(contest._block_average_payouts(bp_curve, np_test.arange(1, 11), 10)) == list(bp_curve),
          "")

    print("\nRake sanity check: a zero-skill chalk lineup and the field's own aggregate ROI (contest.py)")

    # `build_chalk_lineup()` -- the single most heavily-owned lineup
    # possible, no randomness, no skill. Real GPP rake means even this
    # should show simulated ROI near -RAKE_PCT*100, not a real edge --
    # if a field-generation bug lets a zero-skill lineup show strong
    # positive ROI, that's proof the field itself is too weak/random to
    # be a realistic opponent, exactly the failure mode the "impossibly
    # good ROI" feedback described.
    chalk = contest.build_chalk_lineup(mul_slate)
    check("build_chalk_lineup returns a legal 10-player lineup",
          len(chalk["players"]) == 10, len(chalk["players"]))
    check("build_chalk_lineup stays within the salary cap",
          chalk["salary_used"] <= optimizer.SALARY_CAP, chalk["salary_used"])

    # mul_slate's own fixture never sets real ownership_pct values (every
    # player floors to the same 0.5 sampling weight), so it can prove
    # build_chalk_lineup() produces a LEGAL lineup but can't prove it's
    # actually picking the highest-owned player anywhere -- a dedicated,
    # deliberately ownership-differentiated fixture is needed for that.
    # Two dummy-opponent games (CHKZ1/CHKZ2 have no hitters at all) keep
    # the two pitcher picks from ever banning CHKA's or CHKB's real
    # hitters -- their own pitchers are given clearly higher ownership
    # than CHKZ1/CHKZ2's so the P-slot picks are deterministic regardless
    # of any internal list-ordering assumption.
    chalk_hitters_a = [
        opt_hitter(9710, "CHKA_C_hi", "CHKA", "C", 2500, 8.0, own=40.0),
        opt_hitter(9711, "CHKA_C_lo", "CHKA", "C", 2400, 7.5, own=5.0),
        opt_hitter(9712, "CHKA_1B_hi", "CHKA", "1B", 2800, 10.0, own=45.0),
        opt_hitter(9713, "CHKA_1B_lo", "CHKA", "1B", 2700, 9.6, own=6.0),
        opt_hitter(9714, "CHKA_2B_hi", "CHKA", "2B", 2600, 9.0, own=42.0),
        opt_hitter(9715, "CHKA_2B_lo", "CHKA", "2B", 2500, 8.7, own=7.0),
        opt_hitter(9716, "CHKA_3B_hi", "CHKA", "3B", 2900, 12.0, own=44.0),
        opt_hitter(9717, "CHKA_3B_lo", "CHKA", "3B", 2800, 11.5, own=8.0),
        opt_hitter(9718, "CHKA_SS_hi", "CHKA", "SS", 2700, 9.5, own=38.0),
        opt_hitter(9719, "CHKA_SS_lo", "CHKA", "SS", 2600, 9.2, own=9.0),
    ]
    chalk_hitters_b = [
        opt_hitter(9720, "CHKB_OF_hi1", "CHKB", "OF", 4000, 14.0, own=50.0),
        opt_hitter(9721, "CHKB_OF_hi2", "CHKB", "OF", 3900, 13.5, own=48.0),
        opt_hitter(9722, "CHKB_OF_hi3", "CHKB", "OF", 3800, 13.0, own=46.0),
        opt_hitter(9723, "CHKB_OF_lo1", "CHKB", "OF", 3000, 12.5, own=3.0),
        opt_hitter(9724, "CHKB_OF_lo2", "CHKB", "OF", 2900, 12.0, own=2.0),
    ]
    chalk_slate = {
        "games": [
            {
                "game_pk": 89001,
                "home": {"abbrev": "CHKA", "hitters": chalk_hitters_a,
                         "probable_pitcher": opt_pitcher(9700, "CHKAP", 8000, 18.0, own=99.0), "scratches": []},
                "away": {"abbrev": "CHKZ1", "hitters": [],
                         "probable_pitcher": opt_pitcher(9701, "CHKZ1P", 7500, 17.0, own=1.0), "scratches": []},
            },
            {
                "game_pk": 89002,
                "home": {"abbrev": "CHKB", "hitters": chalk_hitters_b,
                         "probable_pitcher": opt_pitcher(9702, "CHKBP", 7800, 16.0, own=98.0), "scratches": []},
                "away": {"abbrev": "CHKZ2", "hitters": [],
                         "probable_pitcher": opt_pitcher(9703, "CHKZ2P", 7500, 15.0, own=1.0), "scratches": []},
            },
        ]
    }
    precise_chalk = contest.build_chalk_lineup(chalk_slate)
    chalk_ids = {p["id"] for p in precise_chalk["players"]}
    check("build_chalk_lineup picks the single highest-owned player at every single-position slot "
          "(C/1B/2B/3B/SS), never the clearly-lower-owned same-position alternative",
          {9710, 9712, 9714, 9716, 9718} <= chalk_ids
          and not ({9711, 9713, 9715, 9717, 9719} & chalk_ids),
          str(sorted(chalk_ids)))
    check("build_chalk_lineup fills all 3 OF slots with the 3 highest-owned outfielders, "
          "never the clearly-lower-owned ones",
          {9720, 9721, 9722} <= chalk_ids and not ({9723, 9724} & chalk_ids),
          str(sorted(chalk_ids)))

    print("\nContest field generator: stakes-tiered field sharpness, against a genuinely "
          "ownership-differentiated field (chalk_slate)")

    # Every single-position slot here (C/1B/2B/3B/SS) offers exactly a
    # "hi" (heavily owned) and "lo" (lightly owned) option at similar
    # value -- real choice for the sampler to make, unlike mul_slate
    # (every player floors to the same weight there). Sampling a large
    # field at each sharpness level and counting how often the "lo" ids
    # get rostered turns the weight-function difference proven above
    # into an observable effect on actual field composition.
    LO_IDS = {9711, 9713, 9715, 9717, 9719}  # CHKA's lightly-owned C/1B/2B/3B/SS options

    def lo_pick_rate(field_sharpness):
        field = contest.generate_field(chalk_slate, 300, seed=606, field_sharpness=field_sharpness)
        picks = sum(1 for lu in field for p in lu["players"] if p["id"] in LO_IDS)
        return picks / (len(field) * 5)  # 5 single-position slots per lineup

    marquee_lo_rate = lo_pick_rate("marquee")
    low_lo_rate = lo_pick_rate("low")
    check("'low' field_sharpness's compressed ownership weighting rosters the lightly-owned "
          "same-position alternative measurably more often than 'marquee' does",
          low_lo_rate > marquee_lo_rate * 1.5,
          f"low={low_lo_rate:.3f} marquee={marquee_lo_rate:.3f}")

    # sample_size=250 is a deliberate exact divisor of both field_size
    # values used below (500 and 100,000) -- when field_size isn't an
    # exact multiple of sample_size, block sizes vary by +-1 real rank,
    # and a plain unweighted average of each sampled lineup's own
    # roi_pct no longer equals the true field-wide average (a lineup
    # whose block represents 2 real ranks needs to count twice as much
    # as one representing only 1, especially right where the curve is
    # steepest -- confirmed directly: with a first_place_pct spike in
    # play, an unweighted average of a 300-lineup sample against a
    # 500-entry field read -8.1% instead of the true -15.0%, purely
    # from that block-size-weighting gap, not a real bug). Each
    # individual sampled lineup's OWN roi_pct stays a perfectly valid,
    # unbiased estimate regardless -- this is purely about how to
    # aggregate many of them into one honest field-wide average for a
    # test, so picking an exact divisor sidesteps the whole issue
    # rather than needing a size-weighted mean here.
    rake_field = contest.generate_field(mul_slate, 250, seed=707)
    rake_contest = dict(contest.CONTEST_TYPES["gpp_small"])

    # The field ranked purely against itself (self-play, same machinery
    # the Contest Generator's own self-play mode uses): every dollar in
    # the prize pool comes from entry fees minus RAKE_PCT, by
    # construction (`prize_pool = field_size * entry_fee * (1 -
    # RAKE_PCT)`, and the payout curve always distributes exactly that
    # much) -- so the field's OWN aggregate ROI, averaged across a
    # representative ownership-weighted sample of itself, is a real,
    # closed-form mathematical fact: it must land close to -RAKE_PCT*100
    # regardless of any player's individual skill, since payouts can
    # never exceed what was collected minus the rake.
    field_mirrored = await contest.evaluate_field_mirrored(
        rake_field, rake_contest, season=2099, num_trials=4000, seed=707,
    )
    field_avg_roi = sum(r["roi_pct"] for r in field_mirrored["results"]) / len(field_mirrored["results"])
    check("the field's own average simulated ROI lands close to -RAKE_PCT*100% (the site's rake), "
          "not near zero or positive -- proof the payout math is genuinely rake-correct",
          abs(field_avg_roi - (-contest.RAKE_PCT * 100)) < 4.0,
          f"field avg roi_pct={round(field_avg_roi, 1)}, expected near {-contest.RAKE_PCT * 100:.1f}")

    # Regression guard for the exact bug this whole sanity check
    # actually found: a small lineup sample projected onto a much
    # bigger real field_size (a routine ratio for the gpp_large/
    # gpp_milly presets, which default to sampling far fewer lineups
    # than their field_size) used to read a single point off the
    # payout curve for each sampled lineup's projected rank -- fine at
    # a ~1x compression ratio, but the curve is sharply top-heavy, so
    # at high compression the point value badly overstates the true
    # block-average payout (confirmed against real live slate data:
    # +34% ROI at a 21x ratio, +264% at 209x, both far from the correct
    # ~-15%). Same 250-lineup sample as above, just re-evaluated
    # against a 100,000-entry field_size (a 400x compression ratio,
    # still an exact divisor of 250) instead of 500.
    high_compression_contest = dict(rake_contest)
    high_compression_contest["field_size"] = 100_000
    high_compression_mirrored = await contest.evaluate_field_mirrored(
        rake_field, high_compression_contest, season=2099, num_trials=4000, seed=707,
    )
    high_compression_avg_roi = (
        sum(r["roi_pct"] for r in high_compression_mirrored["results"])
        / len(high_compression_mirrored["results"])
    )
    check("the field's own average ROI stays close to -RAKE_PCT*100% even at a severe (333x) "
          "sample-to-field_size compression ratio -- the exact regime that used to blow up",
          abs(high_compression_avg_roi - (-contest.RAKE_PCT * 100)) < 6.0,
          f"field avg roi_pct={round(high_compression_avg_roi, 1)} at 333x compression, "
          f"expected near {-contest.RAKE_PCT * 100:.1f}")

    # The chalk lineup evaluated against that SAME realistic field (the
    # actual "would a real user see this" path -- entries ranked
    # against a separately-sampled opponent field, exactly like the
    # Contest Generator's default simulate mode).
    chalk_eval = await contest.evaluate_batch_simulated(
        [chalk], rake_field, rake_contest, season=2099, num_trials=4000, seed=707,
    )
    chalk_roi = chalk_eval["results"][0]["roi_pct"]
    check("a zero-skill chalk lineup does NOT show anywhere near the '+101.7%' magnitude of ROI the user "
          "flagged as implausible -- the whole point of this sanity check",
          chalk_roi < 50.0, chalk_roi)
    check("the chalk lineup's ROI isn't wildly better than the field's own average either -- a lineup with "
          "no real skill edge shouldn't meaningfully outperform the field it's drawn from",
          chalk_roi < field_avg_roi + 40.0,
          str((chalk_roi, round(field_avg_roi, 1))))

    print("\nReal contest-standings results (contest_results.py) -- post-contest, not pre-contest")

    # DK's real post-contest "standings" export -- a different file from
    # the pre-contest salary CSV or the bulk-entries upload template --
    # packs an entries table (Rank/EntryId/EntryName/Points/Lineup) and a
    # player-pool table (Player/Roster Position/%Drafted/FPTS) into the
    # same rows. The player pool routinely outlives the entries table
    # (far more real drafted players than there are rows shown), which
    # this fixture's 3rd row (blank entries columns, real player-pool
    # columns) exercises directly.
    standings_csv = (
        "Rank,EntryId,EntryName,TimeRemaining,Points,Lineup,,Player,Roster Position,%Drafted,FPTS\n"
        "1,5001,grinder99 (1/5),0,150.5,1B A 2B B,,Mookie Betts,SS,25.5,18.2\n"
        "2,5002,otherguy (2/10),0,140.0,1B C 2B D,,Aaron Judge,OF,15.0,22.1\n"
        ",,,,,,,\"Fernández, Freddy\",1B,10.25,9.5\n"
    )
    parsed_standings = contest_results.parse_contest_standings(standings_csv)
    check("parse_contest_standings finds both real contest entries",
          len(parsed_standings["entries"]) == 2, str(parsed_standings["entries"]))
    check("parse_contest_standings reads rank/points as real numbers, not strings",
          parsed_standings["entries"][0]["rank"] == 1 and parsed_standings["entries"][0]["points"] == 150.5,
          str(parsed_standings["entries"][0]))
    check("parse_contest_standings finds all 3 real player-pool rows, including the one with no entries data",
          len(parsed_standings["player_pool"]) == 3, str(parsed_standings["player_pool"]))
    check("parse_contest_standings strips the % and reads ownership as a real number",
          parsed_standings["player_pool"][0]["ownership_pct"] == 25.5, str(parsed_standings["player_pool"][0]))
    check("parse_contest_standings normalizes player names the same way the rest of the app does",
          parsed_standings["player_pool"][0]["normalized_name"] == player_match.normalize_name("Mookie Betts"),
          str(parsed_standings["player_pool"][0]))

    check("parse_contest_standings returns empty results for a completely empty file rather than crashing",
          contest_results.parse_contest_standings("") == {"entries": [], "player_pool": []}, "")

    print("\nContest standings: the Lineup column (the field's joint structure)")

    # A real, complete DK Classic MLB lineup cell, in DK's own slot
    # order, including a name carrying a suffix ("Jr.") and one with a
    # two-word surname -- both of which a naive split would mangle.
    real_lineup = (
        "1B Pete Alonso 2B Jazz Chisholm Jr. 3B Isaac Paredes C Yainer Diaz "
        "OF Yordan Alvarez OF Daulton Varsho OF Leody Taveras "
        "P George Kirby P Gage Jump SS Jeremy Pena"
    )
    slots = contest_results.parse_lineup(real_lineup)
    check("parse_lineup splits a real DK lineup cell into all 10 roster slots",
          slots is not None and len(slots) == 10, str(slots))
    check("parse_lineup keeps a multi-token name with a suffix intact rather than truncating it",
          any(s["name"] == "Jazz Chisholm Jr." for s in slots), str(slots))
    check("parse_lineup pairs each name with its own real roster slot",
          [s["slot"] for s in slots] == ["1B", "2B", "3B", "C", "OF", "OF", "OF", "P", "P", "SS"],
          str([s["slot"] for s in slots]))
    check("parse_lineup normalizes names the same way the rest of the app does",
          slots[0]["normalized_name"] == player_match.normalize_name("Pete Alonso"), str(slots[0]))

    # A mis-parse must produce nothing, never a partial roster -- a
    # wrong lineup archived as fact is worse than a skipped one.
    check("parse_lineup rejects a lineup missing a slot rather than returning a partial roster",
          contest_results.parse_lineup("1B Pete Alonso 2B Jazz Chisholm Jr.") is None, "")
    check("parse_lineup rejects a cell with a slot token but no name after it",
          contest_results.parse_lineup(real_lineup.replace("SS Jeremy Pena", "SS ")) is None, "")
    check("parse_lineup returns None for an empty or whitespace-only cell",
          contest_results.parse_lineup("") is None and contest_results.parse_lineup("   ") is None, "")
    check("parse_contest_standings attaches a parsed lineup to each entry, and None where it "
          "isn't a legal roster (this fixture's rows are deliberately short)",
          [e["lineup"] for e in parsed_standings["entries"]] == [None, None],
          str([e["lineup"] for e in parsed_standings["entries"]]))

    # Real stack distribution: three entries, one of which is a genuine
    # 4-man CLE stack, one a 2-man, one with none at all.
    team_by_name = {
        player_match.normalize_name(n): t
        for n, t in [
            ("Jose Ramirez", "CLE"), ("Steven Kwan", "CLE"),
            ("Josh Naylor", "CLE"), ("Bo Naylor", "CLE"),
            ("Mookie Betts", "LAD"), ("Freddie Freeman", "LAD"),
            ("Will Smith", "LAD"), ("Teoscar Hernandez", "LAD"),
        ]
    }

    def _lu(names):
        slots_order = ["1B", "2B", "3B", "C", "OF", "OF", "OF", "SS"]
        out = [{"slot": s, "name": n, "normalized_name": player_match.normalize_name(n)}
               for s, n in zip(slots_order, names)]
        # Two pitchers, deliberately given real hitter team names to
        # prove pitchers are excluded from stack counting.
        out += [{"slot": "P", "name": "Jose Ramirez", "normalized_name": player_match.normalize_name("Jose Ramirez")}] * 2
        return out

    dist = contest_results.stack_distribution(
        [
            _lu(["Jose Ramirez", "Steven Kwan", "Josh Naylor", "Bo Naylor",
                 "Mookie Betts", "X One", "X Two", "X Three"]),
            _lu(["Jose Ramirez", "Steven Kwan", "X A", "X B", "X C", "X D", "X E", "X F"]),
            _lu(["Mookie Betts", "Freddie Freeman", "Will Smith", "Teoscar Hernandez",
                 "X G", "X H", "X I", "X J"]),
        ],
        team_by_name,
    )
    check("stack_distribution counts a real 4-man stack at exactly size 4",
          dist["CLE"].get(4) == 1, str(dist["CLE"]))
    check("stack_distribution counts the 2-man stack separately from the 4-man one",
          dist["CLE"].get(2) == 1, str(dist["CLE"]))
    check("stack_distribution puts the entry with none of that team's bats in the size-0 bucket",
          dist["CLE"].get(0) == 1, str(dist["CLE"]))
    check("stack_distribution excludes rostered PITCHERS from a team's stack count -- a starting "
          "pitcher is not part of that team's offensive stack",
          sum(dist["CLE"].values()) == 3 and max(dist["CLE"]) == 4, str(dist["CLE"]))
    check("stack_distribution counts a second team on the same entries independently",
          dist["LAD"].get(4) == 1 and dist["LAD"].get(1) == 1, str(dist["LAD"]))
    check("every team's counts sum to the field size, so the result reads as a real distribution",
          all(sum(sizes.values()) == 3 for sizes in dist.values()), str(dist))

    check("find_my_entry finds the right entry by exact EntryId",
          contest_results.find_my_entry(parsed_standings["entries"], entry_id="5002")["rank"] == 2,
          str(contest_results.find_my_entry(parsed_standings["entries"], entry_id="5002")))
    check("find_my_entry finds the right entry by handle, ignoring the (rank/total) suffix",
          contest_results.find_my_entry(parsed_standings["entries"], handle="grinder99")["rank"] == 1,
          str(contest_results.find_my_entry(parsed_standings["entries"], handle="grinder99")))
    check("find_my_entry is case-insensitive on the handle match",
          contest_results.find_my_entry(parsed_standings["entries"], handle="GRINDER99")["rank"] == 1, "")
    check("find_my_entry returns None when neither entry_id nor handle matches anything real",
          contest_results.find_my_entry(parsed_standings["entries"], handle="nobody_here") is None, "")
    check("find_my_entry returns None when given neither an entry_id nor a handle",
          contest_results.find_my_entry(parsed_standings["entries"]) is None, "")
    check("find_my_entry prefers an exact entry_id over a handle when both are given",
          contest_results.find_my_entry(parsed_standings["entries"], entry_id="5001", handle="otherguy")["rank"] == 1,
          "")

    check("extract_csv_text decodes a plain (non-zip) CSV directly",
          contest_results.extract_csv_text(standings_csv.encode("utf-8")) == standings_csv, "")

    import io as _io_test
    import zipfile as _zipfile_test
    zip_buf = _io_test.BytesIO()
    with _zipfile_test.ZipFile(zip_buf, "w") as zf:
        zf.writestr("contest-standings-12345.csv", standings_csv)
    check("extract_csv_text unwraps a real DK contest-standings .zip download to its one CSV",
          contest_results.extract_csv_text(zip_buf.getvalue()) == standings_csv, "")

    try:
        contest_results.extract_csv_text(b"PK\x03\x04not actually a valid zip")
        check("extract_csv_text raises a clear error on a corrupted zip rather than an opaque crash", False)
    except ValueError:
        check("extract_csv_text raises a clear error on a corrupted zip rather than an opaque crash", True)

    print("\nOutcome-pool calibration (contest_results.py) -- backtesting real archived contest data")

    check("outcome_percentile: a value at the exact pool median lands near the 50th percentile",
          abs(contest_results.outcome_percentile(10.0, [0.0, 5.0, 10.0, 15.0, 20.0]) - 60.0) < 0.01,
          contest_results.outcome_percentile(10.0, [0.0, 5.0, 10.0, 15.0, 20.0]))
    check("outcome_percentile: a value below every pool member lands at the 0th percentile",
          contest_results.outcome_percentile(-5.0, [0.0, 5.0, 10.0]) == 0.0, "")
    check("outcome_percentile: a value above every pool member lands at the 100th percentile",
          contest_results.outcome_percentile(100.0, [0.0, 5.0, 10.0]) == 100.0, "")
    check("outcome_percentile: an empty pool returns a neutral 50.0 rather than crashing",
          contest_results.outcome_percentile(10.0, []) == 50.0, "")

    # A model whose real observations are genuinely uniformly spread
    # across every percentile is, by definition, well-calibrated --
    # this is the reference case calibration_summary()'s own docstring
    # promises: mean near 50, ~80% inside the 10-90 band, ~50% inside
    # 25-75.
    uniform_percentiles = [p for p in range(0, 101, 5)]  # 0,5,...,100 -- 21 evenly-spaced points
    uniform_summary = contest_results.calibration_summary(uniform_percentiles)
    check("calibration_summary: a genuinely uniform spread of real percentiles reports mean_percentile near 50",
          abs(uniform_summary["mean_percentile"] - 50.0) < 1.0, str(uniform_summary))
    check("calibration_summary: a genuinely uniform spread reports pct_within_10_90 near the expected 80%",
          abs(uniform_summary["pct_within_10_90"] - 80.0) < 5.0, str(uniform_summary))
    check("calibration_summary: a genuinely uniform spread reports pct_within_25_75 near the expected 50%",
          abs(uniform_summary["pct_within_25_75"] - 50.0) < 5.0, str(uniform_summary))

    # A model whose pools are too NARROW keeps getting surprised by real
    # outcomes landing in the tails -- percentiles cluster near 0/100.
    narrow_summary = contest_results.calibration_summary([2.0, 5.0, 95.0, 98.0, 1.0, 99.0])
    check("calibration_summary: real outcomes clustered in the tails (too-narrow pools) show a low "
          "pct_within_10_90, catching real miscalibration rather than reporting a false-positive good score",
          narrow_summary["pct_within_10_90"] < 50.0, str(narrow_summary))

    # A model whose pools are too WIDE never gets surprised -- every
    # real outcome lands safely near the middle of an overly-generous
    # spread, percentiles cluster near 50.
    wide_summary = contest_results.calibration_summary([48.0, 49.0, 50.0, 51.0, 52.0, 50.0])
    check("calibration_summary: real outcomes clustered near the middle (too-wide pools) show a "
          "pct_within_10_90 near 100%, also catching miscalibration in the opposite direction",
          wide_summary["pct_within_10_90"] > 90.0, str(wide_summary))
    check("calibration_summary: an empty observation list reports n=0 and None stats rather than crashing",
          contest_results.calibration_summary([]) == {
              "n": 0, "mean_percentile": None, "pct_within_10_90": None, "pct_within_25_75": None,
          }, str(contest_results.calibration_summary([])))

    print("\nDraftKings entries CSV (dk_entries.py) -- simulating lineups you actually built on DK")

    # DK's real bulk-entries export packs two unrelated tables into one
    # CSV: the entries table (Entry ID..OF3) starting at column 0, and
    # the slate's full player pool starting well past it (column 15
    # here, matching the real file's own "blank column, then
    # Instructions" offset) -- confirmed against a real DK export
    # during manual verification. Reusing mul_slate's own players
    # (MP1/MP2/MC1/M1B1/M2B1/M3B1/MSS1/MOF1/MOF2/MOF3) so the resolved
    # salary/points totals can be checked against known values.
    dk_header = "Entry ID,Contest Name,Contest ID,Entry Fee,P,P,C,1B,2B,3B,SS,OF,OF,OF,,Instructions"
    dk_filled_picks = (
        "MP1 (90001),MP2 (90002),MC1 (90003),M1B1 (90004),M2B1 (90005),"
        "M3B1 (90006),MSS1 (90007),MOF1 (90008),MOF2 (90009),MOF3 (90010)"
    )
    dk_entry_rows = [
        f"5000000001,Test GPP,7000001,$1.00,{dk_filled_picks},,",
        "5000000002,Test GPP,7000001,$1.00,,,,,,,,,,,",
        "5000000003,Other Contest,7000002,$5.00,,,,,,,,,,,",
    ]
    dk_pad = "," * 15  # entries table is 14 columns wide + 1 blank column before "Instructions"
    dk_pool_header = dk_pad + "Position,Name + ID,Name,ID,Roster Position,Salary,Game Info,TeamAbbrev,AvgPointsPerGame"
    dk_pool_rows = [
        dk_pad + row
        for row in [
            "SP,MP1 (90001),MP1,90001,P,9000,MUL1@MUL2 08/17/2026 07:05PM ET,MUL1,18.0",
            "SP,MP2 (90002),MP2,90002,P,8800,MUL1@MUL2 08/17/2026 07:05PM ET,MUL2,17.5",
            "C,MC1 (90003),MC1,90003,C,3000,MUL1@MUL2 08/17/2026 07:05PM ET,MUL1,8.0",
            "1B,M1B1 (90004),M1B1,90004,1B,4000,MUL1@MUL2 08/17/2026 07:05PM ET,MUL1,10.0",
            "2B,M2B1 (90005),M2B1,90005,2B,3500,MUL1@MUL2 08/17/2026 07:05PM ET,MUL1,9.0",
            "3B,M3B1 (90006),M3B1,90006,3B,4500,MUL1@MUL2 08/17/2026 07:05PM ET,MUL1,12.0",
            "SS,MSS1 (90007),MSS1,90007,SS,3800,MUL1@MUL2 08/17/2026 07:05PM ET,MUL1,9.5",
            "OF,MOF1 (90008),MOF1,90008,OF,5000,MUL1@MUL2 08/17/2026 07:05PM ET,MUL1,14.0",
            "OF,MOF2 (90009),MOF2,90009,OF,4800,MUL1@MUL2 08/17/2026 07:05PM ET,MUL1,13.5",
            "OF,MOF3 (90010),MOF3,90010,OF,4600,MUL1@MUL2 08/17/2026 07:05PM ET,MUL1,13.0",
        ]
    ]
    dk_entries_csv = "\n".join([dk_header, *dk_entry_rows, dk_pool_header, *dk_pool_rows]) + "\n"

    dk_parsed = dk_entries.parse_entries_csv(dk_entries_csv)
    check("parse_entries_csv finds all 3 entries and stops before the embedded player-pool table",
          len(dk_parsed) == 3, str(len(dk_parsed)))
    check("a filled entry's picks are DK ids in fixed roster order",
          dk_parsed[0]["picks"] == ["90001", "90002", "90003", "90004", "90005", "90006", "90007", "90008", "90009", "90010"],
          str(dk_parsed[0]["picks"]))
    check("a blank reservation's picks are all None",
          dk_parsed[1]["picks"] == [None] * 10, str(dk_parsed[1]["picks"]))
    check("entry_fee is parsed as a float, dollar sign stripped",
          dk_parsed[0]["entry_fee"] == 1.0 and dk_parsed[2]["entry_fee"] == 5.0,
          str((dk_parsed[0]["entry_fee"], dk_parsed[2]["entry_fee"])))

    dk_contests = dk_entries.contest_summary(dk_parsed)
    contests_by_id = {c["contest_id"]: c for c in dk_contests}
    check("contest_summary finds both distinct contests in the file",
          set(contests_by_id) == {"7000001", "7000002"}, str(set(contests_by_id)))
    check("contest_summary counts entries and filled lineups per contest correctly",
          contests_by_id["7000001"]["num_entries"] == 2 and contests_by_id["7000001"]["num_filled"] == 1
          and contests_by_id["7000002"]["num_entries"] == 1 and contests_by_id["7000002"]["num_filled"] == 0,
          str(contests_by_id))

    print("\nEntry Manager (dk_entry_manager.py) -- filling a real DK template with generated lineups")

    def fake_lineup(dk_ids, names=None):
        names = names or [f"P{d}" for d in dk_ids]
        return {"players": [{"name": n, "dk_id": d} for n, d in zip(names, dk_ids)]}

    # 10 fake DK ids in fixed roster order (P,P,C,1B,2B,3B,SS,OF,OF,OF),
    # deliberately distinct from dk_pool_rows' own 90001-90010 so a
    # successful fill is unambiguous in the output, not a coincidental
    # match against what the file already had.
    em_lineup_a = fake_lineup(
        ["91001", "91002", "91003", "91004", "91005", "91006", "91007", "91008", "91009", "91010"],
        ["EA_P1", "EA_P2", "EA_C", "EA_1B", "EA_2B", "EA_3B", "EA_SS", "EA_OF1", "EA_OF2", "EA_OF3"],
    )
    em_lineup_b = fake_lineup(
        ["92001", "92002", "92003", "92004", "92005", "92006", "92007", "92008", "92009", "92010"],
        ["EB_P1", "EB_P2", "EB_C", "EB_1B", "EB_2B", "EB_3B", "EB_SS", "EB_OF1", "EB_OF2", "EB_OF3"],
    )

    filled_csv, em_summary = dk_entry_manager.fill_entries(dk_entries_csv, "7000001", [em_lineup_a])
    em_rows = list(csv.reader(io.StringIO(filled_csv)))
    check("fill_entries fills exactly the one blank entry for that contest (only_blank default)",
          em_summary == {
              "contest_id": "7000001", "filled_count": 1, "entry_ids_filled": ["5000000002"],
              "unfilled_row_count": 0, "lineups_unused": 0,
          },
          str(em_summary))
    check("the already-filled entry (5000000001) is left completely untouched",
          em_rows[1] == list(csv.reader(io.StringIO(dk_entry_rows[0])))[0],
          str(em_rows[1]))
    check("the previously-blank entry now carries 'Name (dk_id)' in fixed roster order",
          em_rows[2][4:14] == [
              "EA_P1 (91001)", "EA_P2 (91002)", "EA_C (91003)", "EA_1B (91004)", "EA_2B (91005)",
              "EA_3B (91006)", "EA_SS (91007)", "EA_OF1 (91008)", "EA_OF2 (91009)", "EA_OF3 (91010)",
          ],
          str(em_rows[2][4:14]))
    check("the OTHER contest's untouched row is byte-identical to the original file",
          em_rows[3] == list(csv.reader(io.StringIO(dk_entry_rows[2])))[0],
          str(em_rows[3]))

    original_rows = list(csv.reader(io.StringIO(dk_entries_csv)))
    check("the embedded player-pool table (every row after the entries table) is byte-identical after fill",
          em_rows[len(dk_entry_rows) + 1 :] == original_rows[len(dk_entry_rows) + 1 :],
          str(em_rows[len(dk_entry_rows) + 1 :][:1]))

    filled_csv_overwrite, overwrite_summary = dk_entry_manager.fill_entries(
        dk_entries_csv, "7000001", [em_lineup_a, em_lineup_b], only_blank=False,
    )
    overwrite_rows = list(csv.reader(io.StringIO(filled_csv_overwrite)))
    check("only_blank=False overwrites an ALREADY-filled entry too, in file order",
          overwrite_rows[1][4:14][0] == "EA_P1 (91001)" and overwrite_rows[2][4:14][0] == "EB_P1 (92001)",
          str((overwrite_rows[1][4], overwrite_rows[2][4])))

    _, no_room_summary = dk_entry_manager.fill_entries(
        dk_entries_csv, "7000001", [em_lineup_a, em_lineup_b],
    )
    check("more lineups than blank target rows: the leftover lineup is reported as unused, not an error",
          no_room_summary == {
              "contest_id": "7000001", "filled_count": 1, "entry_ids_filled": ["5000000002"],
              "unfilled_row_count": 0, "lineups_unused": 1,
          },
          str(no_room_summary))

    _, no_lineups_summary = dk_entry_manager.fill_entries(dk_entries_csv, "7000002", [])
    check("more blank target rows than lineups: the leftover row is reported unfilled, not an error",
          no_lineups_summary == {
              "contest_id": "7000002", "filled_count": 0, "entry_ids_filled": [],
              "unfilled_row_count": 1, "lineups_unused": 0,
          },
          str(no_lineups_summary))

    try:
        dk_entry_manager.fill_entries(dk_entries_csv, "9999999", [em_lineup_a])
        check("fill_entries raises for a contest_id with no rows in the file at all", False)
    except dk_entry_manager.EntryManagerError:
        check("fill_entries raises for a contest_id with no rows in the file at all", True)

    lineup_missing_dk_id = fake_lineup(
        ["93001", "93002", "93003", "93004", "93005", "93006", "93007", "93008", "", "93010"],
    )
    try:
        dk_entry_manager.fill_entries(dk_entries_csv, "7000001", [lineup_missing_dk_id])
        check("fill_entries raises a clear error when a lineup player has no dk_id "
              "(RotoWire-only projections, no real DK salary CSV loaded)", False)
    except dk_entry_manager.EntryManagerError as exc:
        check("fill_entries raises a clear error when a lineup player has no dk_id "
              "(RotoWire-only projections, no real DK salary CSV loaded)",
              "dk id" in str(exc).lower(), str(exc))

    curve = contest._custom_payout_curve(10, 1000.0, "top_heavy", 40.0)
    check("_custom_payout_curve pins rank 1's payout to exactly first_place_pct of the pool",
          curve[0] == 400.0, str(curve[0]))
    check("_custom_payout_curve's whole curve still sums to (approximately) the full prize pool",
          abs(sum(curve) - 1000.0) < 0.10, str(sum(curve)))
    check("_custom_payout_curve falls back to the plain curve when no first_place_pct override is given",
          contest._custom_payout_curve(10, 1000.0, "top_heavy", None)
          == contest._payout_curve(10, 1000.0, "top_heavy"))

    # evaluate_field_mirrored's within-sample-rank -> real-field-rank
    # projection is an exact, collision-free linear interpolation --
    # checked here in isolation against a hand-derivable case before
    # trusting it inside the bigger simulation: 5 sampled lineups
    # standing in for a 21-entry field should map evenly, one real rank
    # every (21-1)/(5-1) = 5 slots apart.
    sample_size_check, field_size_check = 5, 21
    real_ranks_check = 1 + np_test.floor(
        np_test.arange(sample_size_check) * (field_size_check - 1) / (sample_size_check - 1)
    ).astype(np_test.int64)
    check("evaluate_field_mirrored's linear rank projection is exact and evenly spaced for a hand-derivable case",
          list(real_ranks_check) == [1, 6, 11, 16, 21], str(list(real_ranks_check)))

    # A DK entries file's real job is establishing the baseline (just the
    # entry fee) for mirroring a real contest's whole field --
    # build_dk_entries_simulated builds an ownership-weighted sample
    # standing in for that field (same construction generate_field()
    # already uses) and ranks it against itself, not against any
    # pre-filled picks from the file.
    dk_sim = await contest.build_dk_entries_simulated(
        mul_slate, season=2099, entry_fee=1.0,
        field_size=500, prize_pool=200.0, first_place_pct=25.0, payout_pct=0.20, num_trials=300, seed=11,
    )
    check("build_dk_entries_simulated builds a field sample (capped at field_size, since 500 < MAX_SAMPLE_SIZE)",
          dk_sim["num_entries_built"] == 500 and dk_sim["sample_size"] == 500,
          str((dk_sim["num_entries_built"], dk_sim["sample_size"])))
    check("build_dk_entries_simulated reports the real contest's own economics",
          dk_sim["field_size"] == 500 and dk_sim["prize_pool"] == 200.0,
          str((dk_sim["field_size"], dk_sim["prize_pool"])))
    check("build_dk_entries_simulated's field_baseline uses the REAL hand-entered prize_pool ($200, "
          "not the standard rake formula) -- (200 / (500*1.0) - 1) * 100 = -60.0",
          dk_sim["field_baseline"] == {"avg_cash_probability_pct": 20.0, "avg_roi_pct": -60.0},
          str(dk_sim["field_baseline"]))
    check("build_dk_entries_simulated's total_entry_cost uses the contest's entry_fee across every sampled entry",
          dk_sim["summary"]["total_entry_cost"] == 500.0, str(dk_sim["summary"]["total_entry_cost"]))
    check("build_dk_entries_simulated's results are sorted best-ROI-first",
          all(dk_sim["results"][i]["roi_pct"] >= dk_sim["results"][i + 1]["roi_pct"]
              for i in range(len(dk_sim["results"]) - 1)),
          str([r["roi_pct"] for r in dk_sim["results"][:5]]))
    check("build_dk_entries_simulated's simulated_points_floor/ceiling are percentiles (not the "
          "true min/max), so floor <= p10 <= mean <= p90 <= ceiling for every sampled lineup",
          all(
              r["simulated_points_floor"] <= r["simulated_points_p10"] <= r["simulated_points_mean"]
              <= r["simulated_points_p90"] <= r["simulated_points_ceiling"]
              for r in dk_sim["results"]
          ),
          str((dk_sim["results"][0]["simulated_points_floor"], dk_sim["results"][0]["simulated_points_ceiling"])))
    check("build_dk_entries_simulated's cash probabilities land near the contest's payout_pct on average, "
          "since a random field lineup should cash at roughly the payout rate",
          abs(dk_sim["summary"]["avg_cash_probability_pct"] - 20.0) < 5.0,
          str(dk_sim["summary"]["avg_cash_probability_pct"]))

    dk_sim_capped = await contest.build_dk_entries_simulated(
        mul_slate, season=2099, entry_fee=0.25,
        field_size=5000, sample_size=50, prize_pool=1000.0, first_place_pct=20.0, num_trials=300, seed=13,
    )
    check("build_dk_entries_simulated honors an explicit sample_size smaller than field_size, "
          "projecting the sample's ranks onto the full real field_size",
          dk_sim_capped["sample_size"] == 50 and dk_sim_capped["field_size"] == 5000,
          str((dk_sim_capped["sample_size"], dk_sim_capped["field_size"])))
    check("a sample_size-capped run's best sampled lineup still projects to a low (best) real rank",
          dk_sim_capped["results"][0]["cash_probability_pct"] >= dk_sim_capped["results"][-1]["cash_probability_pct"],
          str((dk_sim_capped["results"][0]["cash_probability_pct"], dk_sim_capped["results"][-1]["cash_probability_pct"])))

    try:
        await contest.build_dk_entries_simulated(
            mul_slate, season=2099, entry_fee=1.0,
            field_size=0, prize_pool=200.0, first_place_pct=25.0, num_trials=50,
        )
        check("build_dk_entries_simulated rejects a field_size smaller than 1", False)
    except contest.ContestError:
        check("build_dk_entries_simulated rejects a field_size smaller than 1", True)

    print("\nIn-house FPTS projections (inhouse_projections.py)")

    INHOUSE_SEASON = 2098

    def _single_rbi_run_game():
        return {
            "plate_appearances": 4, "hits": 1, "doubles": 0, "triples": 0,
            "home_runs": 0, "rbi": 1, "runs": 1, "walks": 0,
            "hit_by_pitch": 0, "stolen_bases": 0,
        }

    # A flat 20-game history: every game is identical, so season
    # average and last-15-games average agree exactly (7.0 DK pts:
    # 1 single(3) + 1 RBI(2) + 1 run(2)) -- isolates the
    # edge.composite multiplier's effect from the season/recent blend.
    flat_games = [_single_rbi_run_game() for _ in range(20)]

    inhouse_game_logs = {80001: flat_games, 80002: flat_games, 80003: []}

    async def fake_inhouse_game_log(player_id, season, group="hitting"):
        return inhouse_game_logs.get(player_id, [])

    mlb.get_player_game_log = fake_inhouse_game_log

    baseline_flat = await inhouse_projections.baseline_dk_points(80001, "OF", INHOUSE_SEASON)
    check("baseline_dk_points matches the flat rate when season and recent-form agree",
          baseline_flat == 7.0, str(baseline_flat))
    check("baseline_dk_points returns 0.0 for a player with zero games this season",
          await inhouse_projections.baseline_dk_points(80003, "OF", INHOUSE_SEASON) == 0.0, "")

    hot_composite, cold_composite = 1.30, 0.80
    hot_fpts = inhouse_projections.project_fpts(baseline_flat, hot_composite)
    cold_fpts = inhouse_projections.project_fpts(baseline_flat, cold_composite)
    check("project_fpts scales the same baseline up for a hot matchup, down for a cold one",
          hot_fpts > baseline_flat > cold_fpts, str((cold_fpts, baseline_flat, hot_fpts)))
    check("project_fpts is an exact baseline x composite multiply",
          hot_fpts == round(baseline_flat * hot_composite, 2), str(hot_fpts))

    # The actual "same average, different MATCHUP -> different
    # inhouse_fpts" claim, driven through inhouse_fpts_batch() -- the
    # real entry point mlb_slate.py calls. Since the v3 rework the
    # multiplier is built from the TODAY-SPECIFIC components only
    # (Vegas total, opposing pitcher, park, ...), never from talent
    # terms the baseline already contains -- so the fixture differs in
    # a matchup component, not in the display composite.
    def _matchup_edge(team_total_value):
        return {"composite": 1.0, "components": {
            "team_total": {"value": team_total_value},
            "pitcher": {"value": 1.0},
        }}
    same_average_players = [
        {"id": 80001, "position": "OF", "edge": _matchup_edge(1.30)},
        {"id": 80002, "position": "OF", "edge": _matchup_edge(0.80)},
    ]
    batch = await inhouse_projections.inhouse_fpts_batch(same_average_players, INHOUSE_SEASON)
    check("two players with an identical season average but a genuinely different MATCHUP "
          "(Vegas team total) get different inhouse_fpts",
          batch[80001] != batch[80002] and batch[80001] > batch[80002], str(batch))
    check("inhouse_fpts_batch matches project_fpts(baseline, calibrated(projection_multiplier)) "
          "directly -- the fitted-scale matchup multiplier, not the display composite",
          batch[80001] == inhouse_projections.project_fpts(
              baseline_flat,
              inhouse_projections.calibrated(
                  inhouse_projections.projection_multiplier(_matchup_edge(1.30)["components"])
              ),
          ),
          str(batch))

    # A player with NO matchup components at all gets a neutral 1.0
    # multiplier -- the old code would have applied his display
    # composite here, re-counting talent his baseline already contains.
    composite_only = await inhouse_projections.inhouse_fpts_batch(
        [{"id": 80001, "position": "OF", "edge": {"composite": 1.30}}], INHOUSE_SEASON
    )
    check("a hitter with only a display composite (no matchup components) projects at exactly "
          "his baseline -- talent is never double-counted through the multiplier any more",
          composite_only[80001] == baseline_flat, str(composite_only))

    check("projection_multiplier re-bases platoon to the player's OWN season line -- a hitter "
          "whose vs-hand OPS matches his overall OPS gets no platoon push either way",
          inhouse_projections.projection_multiplier(
              {"team_total": {"value": 1.0}}, season_ops=0.800, vs_hand_ops=0.800,
          ) == 1.0, "")
    up = inhouse_projections.projection_multiplier(
        {"team_total": {"value": 1.0}}, season_ops=0.700, vs_hand_ops=0.900)
    down = inhouse_projections.projection_multiplier(
        {"team_total": {"value": 1.0}}, season_ops=0.900, vs_hand_ops=0.700)
    check("a real personal platoon EDGE (vs-hand OPS above his own overall) pushes the "
          "multiplier up, and a personal platoon weakness pushes it down",
          up > 1.0 > down, str((up, down)))
    check("the self-relative platoon ratio is capped the same +/-45% as scoring.py's own "
          "components, so one thin split can't run away",
          inhouse_projections.projection_multiplier(
              {}, season_ops=0.200, vs_hand_ops=1.200,
          ) <= 1.45, "")
    check("calibrated() rescales a multiplier's deviation by the fitted k -- the fitted scale "
          "is a real amplification of the deliberately-tight matchup spread",
          abs(inhouse_projections.calibrated(1.1) - (1.0 + inhouse_projections._PROJECTION_DAMPING * 0.1)) < 1e-9,
          str(inhouse_projections.calibrated(1.1)))

    # Recent form: a player whose last 15 games differ from his earlier
    # games should land between the two straight averages, not exactly
    # at either one.
    cold_start = [dict(_single_rbi_run_game(), rbi=0, runs=0) for _ in range(10)]  # 3.0 DK pts/game
    hot_recent = [_single_rbi_run_game() for _ in range(15)]  # 7.0 DK pts/game
    inhouse_game_logs[80006] = cold_start + hot_recent
    recent_form_baseline = await inhouse_projections.baseline_dk_points(80006, "OF", INHOUSE_SEASON)
    season_avg = (10 * 3.0 + 15 * 7.0) / 25
    recent_avg = 7.0
    check("baseline_dk_points blends season average with recent form, landing strictly between them",
          min(season_avg, recent_avg) < recent_form_baseline < max(season_avg, recent_avg),
          str((season_avg, recent_avg, recent_form_baseline)))

    # Thin-sample shrink: a player with only a handful of games this
    # season should be pulled toward the shared same-position pool
    # (warmed up here by the 20-game players above, same as
    # variance.py's own thin-sample test), not stuck at his own
    # small-sample rate.
    inhouse_game_logs[80007] = [_single_rbi_run_game() for _ in range(3)]  # own rate: 7.0
    inhouse_game_logs[80008] = [dict(_single_rbi_run_game(), rbi=0, runs=0)] * 40  # 3.0 DK pts/game, warms the pool low
    await inhouse_projections.baseline_dk_points(80008, "OF", INHOUSE_SEASON)
    thin_baseline = await inhouse_projections.baseline_dk_points(80007, "OF", INHOUSE_SEASON)
    check("a thin-sample player's baseline is shrunk toward the shared position pool, "
          "not stuck at his own tiny sample's rate",
          thin_baseline < 7.0, str(thin_baseline))

    print("\nIn-house FPTS v2: batting-order PA volume + pitcher win-odds corrections")

    check("project_fpts applies an explicit pa_factor multiplicatively",
          inhouse_projections.project_fpts(10.0, 1.0, pa_factor=1.1) == 11.0)
    check("project_fpts adds an explicit win_ev_delta on top, after the pa_factor multiply",
          inhouse_projections.project_fpts(10.0, 1.0, win_ev_delta=1.5) == 11.5)
    check("project_fpts with neither kwarg reproduces the plain baseline x composite result",
          inhouse_projections.project_fpts(10.0, 1.2) == 12.0)

    # A full-trust (60-game, >= the 50-game hitter threshold) flat 7.0
    # pts/game hitter isolates the batting-order factor from the shared
    # position-pool shrink -- at full trust the shrink term collapses to
    # exactly the player's own blended rate regardless of what earlier
    # tests in this section left in the pool.
    inhouse_game_logs[80020] = [_single_rbi_run_game() for _ in range(60)]
    # The PA factor is RELATIVE to the player's usual (projected) slot
    # since the v3 rework: the baseline was earned batting in his
    # normal spot, so it already contains his normal PA volume, and the
    # old absolute factor handed a permanent leadoff hitter a free +9%
    # every single day. Only a CHANGE in slot moves the number now.
    promoted_batch = await inhouse_projections.inhouse_fpts_batch(
        [{"id": 80020, "position": "OF", "edge": {"composite": 1.0},
          "batting_order": 1, "projected_batting_order": 8}], INHOUSE_SEASON
    )
    demoted_batch = await inhouse_projections.inhouse_fpts_batch(
        [{"id": 80020, "position": "OF", "edge": {"composite": 1.0},
          "batting_order": 9, "projected_batting_order": 2}], INHOUSE_SEASON
    )
    usual_spot_batch = await inhouse_projections.inhouse_fpts_batch(
        [{"id": 80020, "position": "OF", "edge": {"composite": 1.0},
          "batting_order": 1, "projected_batting_order": 1}], INHOUSE_SEASON
    )
    no_order_batch = await inhouse_projections.inhouse_fpts_batch(
        [{"id": 80020, "position": "OF", "edge": {"composite": 1.0}}], INHOUSE_SEASON
    )
    check("a hitter PROMOTED from his projected 8-hole to confirmed leadoff gets a real PA "
          "boost -- the genuine role-change signal",
          promoted_batch[80020] == round(7.0 * (1.09 / 0.94), 2), str(promoted_batch))
    check("a hitter DEMOTED from projected 2nd to confirmed 9th gets a real PA cut",
          demoted_batch[80020] == round(7.0 * (0.91 / 1.06), 2), str(demoted_batch))
    check("a permanent leadoff hitter confirmed exactly where he was projected gets NO free "
          "boost -- his baseline already contains his leadoff PA volume",
          usual_spot_batch[80020] == 7.0, str(usual_spot_batch))
    check("no confirmed batting order leaves inhouse_fpts unchanged (factor 1.0)",
          no_order_batch[80020] == 7.0, str(no_order_batch))
    check("a promotion genuinely outprojects the usual spot, which outprojects a demotion",
          promoted_batch[80020] > usual_spot_batch[80020] > demoted_batch[80020])
    check("batting_order_pa_factor is neutral with no projection to compare against -- a "
          "confirmed slot alone says nothing about whether it is a CHANGE",
          inhouse_projections.batting_order_pa_factor(1, None) == 1.0, "")

    def _pitcher_start(win: int = 0):
        return {
            "outs": 18, "strikeouts": 6, "wins": win, "earned_runs": 2,
            "hits_against": 5, "walks_against": 2, "hit_batsmen": 0,
            "complete_games": 0, "shutouts": 0,
        }

    # 10 identical starts (so season avg == recent-15 avg, isolating the
    # win-odds correction from the recent-form blend) with exactly 3
    # decisions won -- pitcher_win_rate() should read that back as 0.3.
    pitcher_starts = [_pitcher_start(win=1) for _ in range(3)] + [_pitcher_start(win=0) for _ in range(7)]
    inhouse_game_logs[80010] = pitcher_starts
    own_win_rate = await inhouse_projections.pitcher_win_rate(80010, INHOUSE_SEASON)
    check("pitcher_win_rate reads back wins/starts from the game log",
          own_win_rate == 0.3, str(own_win_rate))
    check("pitcher_win_rate returns None for a pitcher with no starts logged yet",
          await inhouse_projections.pitcher_win_rate(80011, INHOUSE_SEASON) is None, "")

    check("win_ev_delta is a positive correction when today's market win odds beat his own season rate",
          inhouse_projections.win_ev_delta(60.0, 0.3) == round((0.6 - 0.3) * 4, 2))
    check("win_ev_delta is a negative correction when today's market win odds trail his own season rate",
          inhouse_projections.win_ev_delta(10.0, 0.3) == round((0.1 - 0.3) * 4, 2))
    check("win_ev_delta is zero with no moneyline loaded",
          inhouse_projections.win_ev_delta(None, 0.3) == 0.0)
    check("win_ev_delta is zero with no starts logged yet (nothing to compare the market number against)",
          inhouse_projections.win_ev_delta(60.0, None) == 0.0)

    pitcher_baseline = round((3 * 4 + 10 * (18 * 0.75 + 6 * 2 - 2 * 2 - 5 * 0.6 - 2 * 0.6)) / 10, 2)
    with_moneyline = await inhouse_projections.inhouse_fpts_batch(
        [{"id": 80010, "position": "P", "edge": {"composite": 1.0}, "win_probability_pct": 60.0}], INHOUSE_SEASON
    )
    check("a pitcher with a market win probability above his own season rate gets a real boost",
          with_moneyline[80010] == round(pitcher_baseline + (0.6 - 0.3) * 4, 2), str(with_moneyline))

    without_moneyline = await inhouse_projections.inhouse_fpts_batch(
        [{"id": 80010, "position": "P", "edge": {"composite": 1.0}}], INHOUSE_SEASON
    )
    check("a pitcher with no moneyline loaded reproduces the plain baseline x composite result (no correction)",
          without_moneyline[80010] == pitcher_baseline, str(without_moneyline))

    print("\nIn-house ownership% (inhouse_projections.py)")

    check("project_ownership returns an empty result for an empty pool",
          inhouse_projections.project_ownership([]) == {}, "")

    of_pool = [
        {"id": 81001, "position": "OF", "salary": 3000, "fpts": 6.0, "implied_runs": 4.4},
        {"id": 81002, "position": "OF", "salary": 4500, "fpts": 8.0, "implied_runs": 4.4},
        {"id": 81003, "position": "OF", "salary": 6000, "fpts": 9.0, "implied_runs": 4.4},
    ]
    c_pool = [
        {"id": 81004, "position": "C", "salary": 3500, "fpts": 5.0, "implied_runs": 4.4},
    ]
    unrecognized_pool = [
        {"id": 81005, "position": "DH", "salary": 4000, "fpts": 7.0, "implied_runs": 4.4},
    ]
    ownership = inhouse_projections.project_ownership(of_pool + c_pool + unrecognized_pool)

    of_total = round(sum(ownership[p["id"]] for p in of_pool), 1)
    check("a 3-slot position's (OF) total ownership sums to slot_count x 100%",
          of_total == 300.0, str(of_total))
    check("a 1-slot position's (C) total ownership sums to exactly 100%",
          ownership[81004] == 100.0, str(ownership[81004]))
    check("a position not in DK's roster slots is skipped rather than guessed at",
          81005 not in ownership, str(ownership.get(81005)))

    # Value signal: same salary/team-total, higher fpts -> higher ownership.
    value_pool = [
        {"id": 82001, "position": "SS", "salary": 5000, "fpts": 6.0, "implied_runs": 4.4},
        {"id": 82002, "position": "SS", "salary": 5000, "fpts": 10.0, "implied_runs": 4.4},
    ]
    value_ownership = inhouse_projections.project_ownership(value_pool)
    check("higher fpts at the same salary/team-total gets higher ownership (value signal)",
          value_ownership[82002] > value_ownership[82001], str(value_ownership))
    # Regression for a real bug found by backtesting against 4 real
    # slates' actual DK contest ownership: the raw fpts/salary ratio is
    # tiny (~0.001-0.003) next to team_total (~0.7-1.5) and salary_tier
    # (0-1), so it barely moved raw_scores despite its weight -- real
    # ownership correlation was ~0.02 (down from an already-weak 0.19)
    # and modelled spread was stuck at 1-4% on a real 365-player pool.
    # A genuinely large fpts gap (6.0 vs 10.0, 67% higher) must now
    # produce REAL separation, not a near-50/50 split.
    check("a large fpts gap at identical salary produces real separation, not a near-flat split",
          value_ownership[82002] - value_ownership[82001] > 20.0, str(value_ownership))

    # Team-total signal: same salary/fpts, higher implied team runs -> higher ownership.
    team_total_pool = [
        {"id": 83001, "position": "2B", "salary": 4000, "fpts": 7.0, "implied_runs": 3.5},
        {"id": 83002, "position": "2B", "salary": 4000, "fpts": 7.0, "implied_runs": 6.5},
    ]
    team_total_ownership = inhouse_projections.project_ownership(team_total_pool)
    check("a higher-implied-total team's player gets higher ownership at equal salary/fpts",
          team_total_ownership[83002] > team_total_ownership[83001], str(team_total_ownership))

    # Salary-tier signal: fpts scaled exactly proportional to salary so
    # every player has identical "value" (fpts/salary) -- isolates the
    # salary-tier bump on top of whatever raw_fpts contributes (which,
    # at proportional fpts, also favors the higher-salary/higher-fpts
    # end -- see the raw_fpts test right below for why that's real and
    # intentional now, not a confound to eliminate).
    VALUE_RATE = 0.002  # fpts per salary dollar
    salary_tier_pool = [
        {"id": 84001, "position": "1B", "salary": 3000, "fpts": 3000 * VALUE_RATE, "implied_runs": 4.4},
        {"id": 84002, "position": "1B", "salary": 4500, "fpts": 4500 * VALUE_RATE, "implied_runs": 4.4},
        {"id": 84003, "position": "1B", "salary": 6000, "fpts": 6000 * VALUE_RATE, "implied_runs": 4.4},
    ]
    salary_tier_ownership = inhouse_projections.project_ownership(salary_tier_pool)
    check("at identical value, the most expensive (stud) play outdraws the mid-priced player -- "
          "salary_tier's own bump reinforced by raw_fpts (he also has the highest absolute fpts)",
          salary_tier_ownership[84003] > salary_tier_ownership[84002], str(salary_tier_ownership))
    check("at identical value, the min-salary play still beats a plain reading with NO signals at "
          "all would predict (an equal 3-way split), even though raw_fpts now pulls the other way",
          salary_tier_ownership[84001] > 0.0, str(salary_tier_ownership))

    # Raw-fpts signal: identical value ratio (fpts scaled proportional
    # to salary, same construction as salary_tier_pool above, just 2
    # players so salary_tier's own pull is symmetric for both and
    # doesn't confound the comparison) -- the higher-salary/higher-
    # absolute-fpts player should now clearly win. Regression for a
    # real bug found by backtesting a real 2026-08-21 contest (see
    # project_ownership's own "WHY RAW FPTS EXISTS" docstring): a
    # min-priced shortstop modelled at 26.9% ownership -- the highest
    # in a 20-player pool -- while a $6200 stud projected for roughly
    # double his points modelled at only 4.7%, because `value` alone
    # has no signal for ABSOLUTE point volume, only points-per-dollar.
    raw_fpts_pool = [
        {"id": 85001, "position": "3B", "salary": 3000, "fpts": 3000 * VALUE_RATE, "implied_runs": 4.4},
        {"id": 85002, "position": "3B", "salary": 6000, "fpts": 6000 * VALUE_RATE, "implied_runs": 4.4},
    ]
    raw_fpts_ownership = inhouse_projections.project_ownership(raw_fpts_pool)
    check("at identical value (fpts/salary), the higher-salary/higher-raw-fpts player now wins -- "
          "raw_fpts gives absolute point volume a real signal independent of price efficiency",
          raw_fpts_ownership[85002] > raw_fpts_ownership[85001], str(raw_fpts_ownership))

    print("\nCorrelated field ownership: a chalky opposing pitcher suppresses a hitter's own ownership")

    # Two pitchers with very different raw ownership propensity (one
    # much higher salary/fpts) -- one ends up clearly the chalkier play.
    chalky_pitcher = {"id": 91001, "position": "P", "salary": 9000, "fpts": 25.0, "implied_runs": 4.4}
    plain_pitcher = {"id": 91002, "position": "P", "salary": 5000, "fpts": 12.0, "implied_runs": 4.4}
    # Two otherwise-IDENTICAL hitters (same salary/fpts/team_total) --
    # only which pitcher they're facing differs.
    hitter_vs_chalky = {
        "id": 91011, "position": "OF", "salary": 4500, "fpts": 9.0, "implied_runs": 4.4,
        "opponent_pitcher_id": 91001,
    }
    hitter_vs_plain = {
        "id": 91012, "position": "OF", "salary": 4500, "fpts": 9.0, "implied_runs": 4.4,
        "opponent_pitcher_id": 91002,
    }
    leverage_pool = [chalky_pitcher, plain_pitcher, hitter_vs_chalky, hitter_vs_plain]
    leverage_ownership = inhouse_projections.project_ownership(leverage_pool)
    check("the chalky pitcher's own modelled ownership is genuinely higher than the plain one's "
          "(precondition for the leverage test below to mean anything)",
          leverage_ownership[91001] > leverage_ownership[91002], str(leverage_ownership))
    check("a hitter facing a chalkier-than-average opposing pitcher gets LOWER modelled ownership "
          "than an otherwise-identical hitter facing a less-chalky one",
          leverage_ownership[91011] < leverage_ownership[91012], str(leverage_ownership))

    original_chalk_weight = inhouse_projections._OPPONENT_PITCHER_CHALK_WEIGHT
    inhouse_projections._OPPONENT_PITCHER_CHALK_WEIGHT = 0.0
    no_leverage_ownership = inhouse_projections.project_ownership(leverage_pool)
    inhouse_projections._OPPONENT_PITCHER_CHALK_WEIGHT = original_chalk_weight
    check("with the leverage weight at 0, the two otherwise-identical hitters get IDENTICAL "
          "ownership -- the adjustment is purely additive, not a rewrite of the base model",
          no_leverage_ownership[91011] == no_leverage_ownership[91012], str(no_leverage_ownership))

    no_opp_pool = [chalky_pitcher, plain_pitcher, {
        "id": 91013, "position": "OF", "salary": 4500, "fpts": 9.0, "implied_runs": 4.4,
    }]
    check("a hitter with no opponent_pitcher_id at all still gets scored normally, no crash",
          91013 in inhouse_projections.project_ownership(no_opp_pool), "")

    print("\nOwnership: multi-slot eligibility and the starters-only pool")

    # A "2B/SS" player must compete in BOTH groups and report the SUM --
    # real %Drafted is his share of entries rostering him at ANY slot.
    # Previously only the first-listed slot counted, making him
    # invisible to the SS group entirely.
    multi_pool = [
        {"id": 95001, "position": "2B", "positions": ["2B", "SS"], "salary": 4500,
         "fpts": 9.0, "implied_runs": 4.4},
        {"id": 95002, "position": "2B", "positions": ["2B"], "salary": 4500,
         "fpts": 9.0, "implied_runs": 4.4},
        {"id": 95003, "position": "SS", "positions": ["SS"], "salary": 4500,
         "fpts": 9.0, "implied_runs": 4.4},
    ]
    multi_own = inhouse_projections.project_ownership(multi_pool)
    check("a multi-eligible player competes in every slot group and reports the SUM -- more "
          "total ownership than an identical single-slot player",
          multi_own[95001] > multi_own[95002] and multi_own[95001] > multi_own[95003],
          str(multi_own))
    check("each group still sums to its slot count x 100% (2B=100, SS=100 -> 200 total)",
          abs(sum(multi_own.values()) - 200.0) < 0.1, str(multi_own))
    check("entries without a positions list still work exactly as before (single position)",
          95002 in multi_own and 95003 in multi_own, str(multi_own))

    def _bench_hitter(pid, order=None, projected=None, pa=300):
        return {"id": pid, "batting_order": order, "projected_batting_order": projected,
                "season": {"pa": pa}}

    # Confirmed lineup: exactly the 9 with a batting_order survive.
    confirmed_team = [_bench_hitter(96000 + i, order=i) for i in range(1, 10)] + [
        _bench_hitter(96100 + i) for i in range(4)
    ]
    kept = mlb_slate._ownership_eligible_hitters(confirmed_team)
    check("with a confirmed lineup, only the batting-order nine compete for ownership -- a "
          "bench bat nobody can roster gets 0%, not a share of the group's fixed total",
          {h["id"] for h in kept} == {96000 + i for i in range(1, 10)},
          str(sorted(h["id"] for h in kept)))

    # Projected order (RotoWire) stands in before confirmation.
    projected_team = [_bench_hitter(96200 + i, projected=i) for i in range(1, 9)] + [
        _bench_hitter(96300 + i) for i in range(5)
    ]
    kept_proj = mlb_slate._ownership_eligible_hitters(projected_team)
    check("before confirmation, RotoWire's projected order stands in -- projected starters "
          "compete, projected bench doesn't",
          {h["id"] for h in kept_proj} == {96200 + i for i in range(1, 9)},
          str(sorted(h["id"] for h in kept_proj)))

    # Neither signal: top 9 by season PA -- playing time, not talent.
    blind_team = [_bench_hitter(96400 + i, pa=600 - i * 40) for i in range(13)]
    kept_blind = mlb_slate._ownership_eligible_hitters(blind_team)
    check("with no lineup signal at all, the top 9 by season plate appearances stand in -- "
          "usage, not talent, decides who's probably starting",
          {h["id"] for h in kept_blind} == {96400 + i for i in range(9)},
          str(sorted(h["id"] for h in kept_blind)))

    check("_dk_slot_positions splits a multi-eligible salary string into every slot",
          mlb_slate._dk_slot_positions("1B/3B", "OF") == ["1B", "3B"], "")
    check("_dk_slot_positions falls back to the bio position with no salary loaded",
          mlb_slate._dk_slot_positions(None, "OF") == ["OF"], "")

    print("\nOwnership: the team-stack layer (hitters are owned team-first)")

    # Percentile helper: the primitive the whole layer rests on.
    check("_percentiles ranks a spread of team values onto 0-1 endpoints",
          inhouse_projections._percentiles({"A": 1.0, "B": 2.0, "C": 3.0}) == {"A": 0.0, "B": 0.5, "C": 1.0}, "")
    check("_percentiles gives tied teams the same shared rank rather than an arbitrary order",
          inhouse_projections._percentiles({"A": 5.0, "B": 5.0}) == {"A": 0.5, "B": 0.5}, "")
    check("_percentiles is neutral (0.5) for a single team -- a percentile is meaningless "
          "without something to rank against",
          inhouse_projections._percentiles({"A": 4.0}) == {"A": 0.5}, "")

    # Two identical hitters at the same position, same salary, same
    # fpts -- differing ONLY in their team's implied runs. The real
    # measured failure this layer fixes (see project_ownership's
    # "WHY A TEAM-STACK LAYER EXISTS") was exactly this being invisible.
    stack_ace = {"id": 92001, "position": "P", "salary": 9000, "fpts": 22.0, "implied_runs": 4.0}
    stack_scrub = {"id": 92002, "position": "P", "salary": 5000, "fpts": 12.0, "implied_runs": 4.0}
    hot_bat = {
        "id": 92011, "position": "OF", "salary": 4500, "fpts": 9.0, "implied_runs": 6.2,
        "team": "HOT", "opponent_pitcher_id": 92002,
    }
    cold_bat = {
        "id": 92012, "position": "OF", "salary": 4500, "fpts": 9.0, "implied_runs": 3.1,
        "team": "COLD", "opponent_pitcher_id": 92002,
    }
    stack_pool = [stack_ace, stack_scrub, hot_bat, cold_bat]
    stack_ownership = inhouse_projections.project_ownership(stack_pool)
    check("a hitter on a high-implied-total team outowns an otherwise-IDENTICAL hitter on a "
          "low-total team -- the team-first effect the flat per-player model couldn't see",
          stack_ownership[92011] > stack_ownership[92012], str(stack_ownership))

    # The opposing-starter half of the layer, isolated: same implied
    # runs on both sides, only the arm they face differs.
    vs_ace = {
        "id": 92021, "position": "OF", "salary": 4500, "fpts": 9.0, "implied_runs": 4.5,
        "team": "VSACE", "opponent_pitcher_id": 92001,
    }
    vs_scrub = {
        "id": 92022, "position": "OF", "salary": 4500, "fpts": 9.0, "implied_runs": 4.5,
        "team": "VSSCRUB", "opponent_pitcher_id": 92002,
    }
    sp_pool = [stack_ace, stack_scrub, vs_ace, vs_scrub]
    sp_ownership = inhouse_projections.project_ownership(sp_pool)
    check("a team facing the pricier (better) starter gets LESS stack ownership than an "
          "identical team facing the cheap arm",
          sp_ownership[92021] < sp_ownership[92022], str(sp_ownership))

    # Additivity. Implied runs feeds BOTH the new stack layer and the
    # pre-existing (much weaker) team_total term, so isolating the new
    # layer's contribution means zeroing both -- with the two hitters
    # then identical in every input the model can see, any remaining
    # difference would mean the layer had rewritten something rather
    # than adding to it.
    original_stack_weight = inhouse_projections._TEAM_STACK_WEIGHT
    original_team_total_weight = inhouse_projections._TEAM_TOTAL_WEIGHT
    inhouse_projections._TEAM_STACK_WEIGHT = 0.0
    inhouse_projections._TEAM_TOTAL_WEIGHT = 0.0
    no_stack_ownership = inhouse_projections.project_ownership(stack_pool)
    inhouse_projections._TEAM_STACK_WEIGHT = original_stack_weight
    inhouse_projections._TEAM_TOTAL_WEIGHT = original_team_total_weight
    check("with both team-level weights at 0 the two hitters land identically -- the stack "
          "layer is purely additive, not a rewrite of the per-player model underneath it",
          no_stack_ownership[92011] == no_stack_ownership[92012], str(no_stack_ownership))

    inhouse_projections._TEAM_STACK_WEIGHT = 0.0
    stack_off_ownership = inhouse_projections.project_ownership(stack_pool)
    inhouse_projections._TEAM_STACK_WEIGHT = original_stack_weight
    check("the stack layer moves the high-total hitter substantially FURTHER ahead than the old "
          "team_total term managed alone -- the real gap it was built to close",
          (stack_ownership[92011] - stack_ownership[92012])
          > 2 * (stack_off_ownership[92011] - stack_off_ownership[92012]),
          f"with={stack_ownership}  without={stack_off_ownership}")

    check("pitchers are deliberately untouched by the team-stack layer (their own model is "
          "separate, and already the best-calibrated group)",
          no_stack_ownership[92001] == stack_ownership[92001], str(stack_ownership))

    # Graceful degradation: the layer's inputs are both optional.
    teamless_pool = [
        stack_ace, stack_scrub,
        {"id": 92031, "position": "OF", "salary": 4500, "fpts": 9.0, "implied_runs": 4.5},
        {"id": 92032, "position": "OF", "salary": 4500, "fpts": 9.0, "implied_runs": 4.5},
    ]
    check("hitters carrying no team at all still get scored, no crash -- the layer degrades "
          "to neutral rather than requiring a field that may not be there",
          len(inhouse_projections.project_ownership(teamless_pool)) == 4, "")

    # No odds loaded at all: implied runs is gone, so the layer has to
    # carry on using the opposing-starter half by itself. These two
    # face DIFFERENT arms, so a working fallback must still separate
    # them -- a tie here would mean the layer had silently gone inert.
    no_odds = [
        dict(vs_ace, implied_runs=None),
        dict(vs_scrub, implied_runs=None),
        stack_ace, stack_scrub,
    ]
    no_odds_own = inhouse_projections.project_ownership(no_odds)
    check("with no odds loaded at all the layer still separates teams on the opposing-starter "
          "signal alone rather than silently going inert",
          no_odds_own[92021] < no_odds_own[92022], str(no_odds_own))

    print("\nLeverage: real ceiling (variance.py's outcome pool) minus ownership%")

    check("ceiling_from_pool reads the exact percentile from a known pool",
          variance.ceiling_from_pool(list(range(1, 11)), 0.9) == 9, "")
    check("ceiling_from_pool returns 0.0 for an empty pool rather than crashing",
          variance.ceiling_from_pool([], 0.9) == 0.0, "")

    # A genuinely streaky player -- 15 quiet games (3.0 pts) and 5 huge
    # ones (20.0 pts), season average ~7.25 -- isolates real upside from
    # a flat player's ceiling, which should sit close to his own mean.
    # Full trust (60 games, >= the 50-game hitter threshold) on both --
    # player_outcome_pool() blends in the shared position pool for
    # thin samples, which would make the exact ceiling depend on
    # whatever other tests have already contributed to that shared
    # pool by this point in the file. At full trust the blend collapses
    # to entirely the player's own real games, same robust pattern
    # already used for the batting-order PA-factor tests above.
    streaky_games = (
        [dict(_single_rbi_run_game(), rbi=0, runs=0) for _ in range(45)]  # 3.0 pts/game
        + [dict(_single_rbi_run_game(), rbi=5, runs=5, hits=2, doubles=1, home_runs=1) for _ in range(15)]
    )
    flat_60_games = [_single_rbi_run_game() for _ in range(60)]  # 7.0 pts/game, every game
    inhouse_game_logs[80025] = streaky_games
    inhouse_game_logs[80026] = flat_60_games

    ceilings = await inhouse_projections.player_ceilings(
        [{"id": 80025, "position": "OF"}, {"id": 80026, "position": "OF"}], INHOUSE_SEASON
    )
    check("a streaky player's ceiling sits well above his own season average -- real upside, not just the mean",
          ceilings[80025] > 15.0, str(ceilings))
    check("a perfectly flat player's ceiling stays close to his own mean -- no fabricated upside",
          abs(ceilings[80026] - 7.0) < 1.0, str(ceilings))

    # End-to-end through the real production path: _attach_inhouse_projections()
    # on a minimal hand-built slate, confirming leverage_score is exactly
    # ceiling minus ownership, using the function's own real outputs (not a
    # hand-predicted number, since the ownership softmax has no simple
    # closed form for a single-player pool).
    lev_hitter = {
        "id": 80025, "position": "OF", "edge": {"composite": 1.0},
        "salary": {"salary": 5000, "position": "OF"}, "projection": None,
    }
    lev_out_games = [
        {
            "home": {"hitters": [lev_hitter], "implied_runs": 4.4, "probable_pitcher": None},
            "away": {"hitters": [], "implied_runs": 4.4, "probable_pitcher": None},
        }
    ]
    await mlb_slate._attach_inhouse_projections(lev_out_games, INHOUSE_SEASON)
    lev_proj = lev_hitter["projection"] or {}
    check("_attach_inhouse_projections attaches inhouse_ceiling and leverage_score together",
          lev_proj.get("inhouse_ceiling") is not None and lev_proj.get("leverage_score") is not None,
          str(lev_proj))
    check("leverage_score is exactly ceiling minus ownership%",
          lev_proj.get("leverage_score") == round(lev_proj["inhouse_ceiling"] - lev_proj["inhouse_ownership_pct"], 2),
          str(lev_proj))

    print("\nBoom/bust scores (variance.boom_bust_from_pool / stack_boom_bust)")

    # A hand-built pool where every tail read is checkable by eye:
    # projection 10 -> boom lines at 15/17.5/20, hitter bust at <= 0.
    bb_pool = [0.0, 0.0, 4.0, 8.0, 12.0, 15.0, 16.0, 18.0, 20.0, 25.0]
    bb = variance.boom_bust_from_pool(bb_pool, 10.0, "hitter")
    check("boom_pct is the exact fraction of the pool at or above 1.5x the projection",
          bb["boom_pct"] == 50.0, str(bb))
    check("the 1.75x/2x ladder narrows monotonically -- each higher bar can only be rarer",
          bb["boom_pct"] >= bb["boom_175x_pct"] >= bb["boom_2x_pct"]
          and bb["boom_2x_pct"] == 20.0, str(bb))
    check("a hitter's bust is the zero game -- exactly the 0-for-with-nothing nights",
          bb["bust_pct"] == 20.0, str(bb))

    # Same pool, pitcher kind: bust widens to <= 5 (shelled/pulled early,
    # negatives included) -- 3 IP with any earned runs already sits
    # below 5 DK points, so <= 0 alone would miss most real disasters.
    bb_p = variance.boom_bust_from_pool(bb_pool, 10.0, "pitcher")
    check("a pitcher's bust threshold is <= 5 DK points, not just <= 0",
          bb_p["bust_pct"] == 30.0 and bb_p["boom_pct"] == bb["boom_pct"], str(bb_p))

    check("no pool or no positive projection returns None rather than fabricating a number",
          variance.boom_bust_from_pool([], 10.0, "hitter") is None
          and variance.boom_bust_from_pool(bb_pool, 0, "hitter") is None
          and variance.boom_bust_from_pool(bb_pool, None, "hitter") is None, "")

    # The projection is the denominator on purpose: the same pool booms
    # more against a modest projection than an aggressive one -- the
    # real leverage signal (measured live: Messick, pool mean above his
    # projection, boomed 49.5%; Glasnow, priced for perfection, 10.5%).
    bb_cheap = variance.boom_bust_from_pool(bb_pool, 8.0, "hitter")
    check("the same pool booms MORE against a lower projection -- projection is the bar to clear",
          bb_cheap["boom_pct"] > bb["boom_pct"], str((bb_cheap["boom_pct"], bb["boom_pct"])))

    # Stack level: five identical hitters, correlated through the shared
    # team-environment draw. Against five INDEPENDENT tail reads the
    # correlated sum must have fatter tails on BOTH ends -- that's what
    # the shared multiplier exists to model, and the reason this is a
    # Monte Carlo rather than five multiplied probabilities.
    bb_sorted = sorted(bb_pool)
    stack = variance.stack_boom_bust([bb_sorted] * 5, [10.0] * 5, [None] * 5, trials=4000, seed=7)
    check("stack_boom_bust returns real percentages for a 5-man stack",
          stack and 0 < stack["boom_pct"] < 100 and 0 < stack["bust_pct"] < 100, str(stack))

    import random as _random
    _rng = _random.Random(7)
    _indep_boom = _indep_bust = 0
    for _ in range(4000):
        _total = sum(_rng.choice(bb_pool) for _ in range(5))
        if _total >= 1.5 * 50.0:
            _indep_boom += 1
        if _total <= 0.5 * 50.0:
            _indep_bust += 1
    check("correlation fattens BOTH tails versus independent sampling -- a stack's big and bad "
          "nights cluster, which is the entire reason to roster one",
          stack["boom_pct"] > 100 * _indep_boom / 4000
          and stack["bust_pct"] > 100 * _indep_bust / 4000,
          str((stack, round(100 * _indep_boom / 4000, 1), round(100 * _indep_bust / 4000, 1))))

    # A stack's boom% must answer the CONDITIONAL question "given this
    # projection, how likely to blow past it" -- so the pools feeding it
    # are recentered on those projections (mlb_slate.py's call site).
    # Without that it degenerates into the pool-vs-projection level gap
    # compounded five times over: measured on a real slate, ATL (top-5
    # pools sitting 33% under their projections, mostly pool dilution
    # from rest days rather than real information) read 1.5% boom /
    # 32.3% bust, while DET at +3% read 19.6% / 16.2%. Recentered, both
    # land in a believable band and vary on what actually differs --
    # the SHAPE of those five bats' distributions.
    _low_pool = sorted([0.0, 2.0, 4.0, 6.0, 9.0] * 20)      # mean 4.2
    _stack_projs = [10.0] * 5                                # projected far above it
    _raw_stack = variance.stack_boom_bust([_low_pool] * 5, _stack_projs, [None] * 5, trials=3000, seed=5)
    _rc_stack = variance.stack_boom_bust(
        [sorted(variance.recenter_pool(_low_pool, 10.0))] * 5, _stack_projs, [None] * 5,
        trials=3000, seed=5,
    )
    check("a stack whose raw pools sit far BELOW today's projections reads a near-zero boom "
          "purely from the level gap -- the degenerate behavior recentering exists to prevent",
          _raw_stack["boom_pct"] < 1.0 and _raw_stack["bust_pct"] > 60.0, str(_raw_stack))
    check("...and recentering on those same projections restores a real, believable boom/bust "
          "that reflects the pools' SHAPE rather than their level",
          5.0 < _rc_stack["boom_pct"] < 40.0 and 5.0 < _rc_stack["bust_pct"] < 45.0, str(_rc_stack))

    check("stack_boom_bust is deterministic for a fixed seed -- the Stacks tab must not flicker",
          variance.stack_boom_bust([bb_sorted] * 5, [10.0] * 5, [None] * 5, trials=1000, seed=3)
          == variance.stack_boom_bust([bb_sorted] * 5, [10.0] * 5, [None] * 5, trials=1000, seed=3), "")
    check("stack_boom_bust returns None for fewer than 2 pools or a non-positive projection sum",
          variance.stack_boom_bust([bb_sorted], [10.0], [None]) is None
          and variance.stack_boom_bust([bb_sorted] * 2, [0.0, 0.0], [None, None]) is None, "")

    # End to end through the attach path: the leverage fixture's own
    # hitter (80025, streaky, full-trust game log) must come out of
    # _attach_inhouse_projections carrying boom/bust alongside his
    # ceiling -- same pools, same single pass.
    check("_attach_inhouse_projections attaches boom_pct/bust_pct onto the same projection dict "
          "as ceiling/leverage -- one pass over the same pools",
          lev_proj.get("boom_pct") is not None and lev_proj.get("bust_pct") is not None,
          str({k: v for k, v in lev_proj.items() if "boom" in k or "bust" in k}))

    print("\nLate swap for a whole batch (late_swap.py)")

    from datetime import datetime as _dt
    from datetime import timezone as _tz

    LS_NOW = _dt(2026, 8, 14, 20, 0, tzinfo=_tz.utc)
    LS_SLOTS = ["P", "P", "C", "1B", "2B", "3B", "SS", "OF", "OF", "OF"]

    def ls_slate(*, scratched=(), postponed_pk=None):
        """Two games: 901 already started (locked), 902 still to come."""
        def side(abbrev, hitters, pitcher):
            return {
                "abbrev": abbrev,
                "hitters": hitters,
                "probable_pitcher": pitcher,
                "scratches": [
                    {"player_id": pid, "name": f"P{pid}", "team": abbrev}
                    for pid in scratched
                    if any(h["id"] == pid for h in hitters) or (pitcher or {}).get("id") == pid
                ],
            }

        def bat(pid):
            return {"id": pid, "name": f"P{pid}"}

        return {
            "games": [
                {
                    "game_pk": 901,
                    "game_time_utc": "2026-08-14T18:00:00Z",   # before LS_NOW -> LOCKED
                    "status": "In Progress",
                    "home": side("AAA", [bat(i) for i in range(101, 112)], {"id": 191}),
                    "away": side("BBB", [bat(i) for i in range(121, 132)], {"id": 192}),
                },
                {
                    "game_pk": 902,
                    "game_time_utc": "2026-08-14T23:00:00Z",   # after LS_NOW -> OPEN
                    "status": "Postponed" if postponed_pk == 902 else "Scheduled",
                    "home": side("CCC", [bat(i) for i in range(201, 212)], {"id": 291}),
                    "away": side("DDD", [bat(i) for i in range(221, 232)], {"id": 292}),
                },
            ]
        }

    def ls_pool(slate):
        """A pool entry per player, priced so every swap stays affordable."""
        pool = []
        for g in slate["games"]:
            for s in ("home", "away"):
                t = g[s]
                for h in t["hitters"]:
                    pool.append({
                        "id": h["id"], "name": h["name"], "team": t["abbrev"],
                        "game_pk": g["game_pk"], "salary": 3000,
                        "projected_fpts": 8.0 + (h["id"] % 7),
                        "ownership_pct": 5.0, "dk_id": str(h["id"]),
                        "slots": ["C", "1B", "2B", "3B", "SS", "OF"],
                    })
                p = t["probable_pitcher"]
                pool.append({
                    "id": p["id"], "name": f"P{p['id']}", "team": t["abbrev"],
                    "game_pk": g["game_pk"], "salary": 8000,
                    "projected_fpts": 18.0, "ownership_pct": 10.0,
                    "dk_id": str(p["id"]), "slots": ["P"],
                })
        return pool

    def ls_entry(ids, pool):
        by_id = {p["id"]: p for p in pool}
        players = [{
            "id": i, "name": by_id[i]["name"], "team": by_id[i]["team"],
            "salary": by_id[i]["salary"], "projected_fpts": by_id[i]["projected_fpts"],
            "ownership_pct": by_id[i]["ownership_pct"], "dk_id": by_id[i]["dk_id"],
            "game_pk": by_id[i]["game_pk"],
        } for i in ids]
        return {
            "players": players,
            "salary_used": sum(p["salary"] for p in players),
            "projected_points": round(sum(p["projected_fpts"] for p in players), 2),
            "total_ownership_pct": round(sum(p["ownership_pct"] for p in players), 1),
            "duplication_risk": -30.0,
            "player_ids": frozenset(ids),
        }

    base_slate = ls_slate()
    base_pool = ls_pool(base_slate)
    lock_state = late_swap.slate_lock_state(base_slate, now=LS_NOW)
    check("slate_lock_state locks a game that has already started and leaves a later one open",
          lock_state["locked_game_pks"] == {901} and lock_state["open_game_pks"] == {902},
          str(lock_state["locked_game_pks"]) + " / " + str(lock_state["open_game_pks"]))

    no_time_state = late_swap.slate_lock_state(
        {"games": [{"game_pk": 903, "game_time_utc": None, "status": "Scheduled"}]}, now=LS_NOW
    )
    check("a game with no usable start time is treated as LOCKED rather than assumed swappable "
          "-- touching a spot DK may already consider locked is the worse error",
          no_time_state["locked_game_pks"] == {903}, str(no_time_state))

    # A lineup with 5 bats from the OPEN game (one of them scratched)
    # and the rest from the locked one.
    open_ids = [201, 202, 203, 204, 205]
    locked_ids = [101, 102, 103]
    entry_ids = [191, 291] + locked_ids + open_ids
    scratched_slate = ls_slate(scratched=(201,))
    scratched_pool = [p for p in ls_pool(scratched_slate) if p["id"] != 201]
    entry = ls_entry(entry_ids, base_pool)

    swapped = late_swap.swap_batch(
        [entry], scratched_slate, scratched_pool,
        slot_order=LS_SLOTS, salary_cap=50000, now=LS_NOW, seed=7,
    )
    new_ids = [p["id"] for p in swapped["entries"][0]["players"]]
    check("a scratched player in an OPEN game gets swapped out",
          201 not in new_ids, str(new_ids))
    check("the swap replaces him in place, keeping the roster exactly 10 players",
          len(new_ids) == 10, str(new_ids))
    check("every other player in the entry is left exactly as built -- a repair touches only "
          "what needs it, rather than re-optimizing the whole lineup",
          [i for i in new_ids if i != 201] and set(entry_ids) - set(new_ids) == {201},
          str(sorted(set(entry_ids) - set(new_ids))))
    check("the swapped-in replacement comes from a game that hasn't started",
          all(p["game_pk"] == 902 for p in swapped["entries"][0]["players"] if p["id"] not in entry_ids),
          str(new_ids))
    check("the entry's salary total is recomputed after the swap",
          swapped["entries"][0]["salary_used"]
          == sum(p["salary"] for p in swapped["entries"][0]["players"]), "")
    check("the summary reports the swap it actually made",
          swapped["entries_changed"] == 1 and swapped["total_swaps"] == 1, str(swapped["total_swaps"]))

    # The SAME player scratched, but sitting in the already-locked game:
    # a real DK entry can't be touched there either.
    locked_scratch_slate = ls_slate(scratched=(101,))
    locked_scratch_pool = [p for p in ls_pool(locked_scratch_slate) if p["id"] != 101]
    locked_swapped = late_swap.swap_batch(
        [ls_entry(entry_ids, base_pool)], locked_scratch_slate, locked_scratch_pool,
        slot_order=LS_SLOTS, salary_cap=50000, now=LS_NOW, seed=7,
    )
    check("a scratched player whose game ALREADY STARTED is left alone -- a real DK entry can't "
          "be edited there, so 'fixing' it would be fiction",
          101 in [p["id"] for p in locked_swapped["entries"][0]["players"]], "")
    check("...and he's reported as stranded rather than silently ignored",
          [r["player_id"] for r in locked_swapped["stranded_players"]] == [101],
          str(locked_swapped["stranded_players"]))

    # A postponed game kills everyone in it.
    pp_slate = ls_slate(postponed_pk=902)
    pp_state = late_swap.slate_lock_state(pp_slate, now=LS_NOW)
    check("every player in a postponed game is treated as dead, not just the scratched ones",
          all(pid in pp_state["dead_player_ids"] for pid in (201, 202, 291)),
          str(len(pp_state["dead_player_ids"])))

    # Diversity: the same scratch across many entries must NOT all
    # resolve to one identical replacement, or a single scratch becomes
    # a mass duplication event.
    many = [ls_entry(entry_ids, base_pool) for _ in range(40)]
    many_swapped = late_swap.swap_batch(
        many, scratched_slate, scratched_pool,
        slot_order=LS_SLOTS, salary_cap=50000, now=LS_NOW, seed=7,
    )
    replacements = {
        next(p["id"] for p in e["players"] if p["id"] not in entry_ids)
        for e in many_swapped["entries"]
    }
    check("the same scratch across 40 entries resolves to several DIFFERENT replacements -- "
          "always taking the single best would turn one scratch into a mass duplication event",
          len(replacements) > 1, f"{len(replacements)} distinct replacements: {sorted(replacements)}")

    try:
        late_swap.swap_batch(
            [entry], base_slate, base_pool, slot_order=LS_SLOTS,
            salary_cap=50000, mode="nonsense",
        )
        check("swap_batch rejects an unknown mode rather than silently defaulting", False)
    except late_swap.LateSwapError:
        check("swap_batch rejects an unknown mode rather than silently defaulting", True)

    unchanged = late_swap.swap_batch(
        [ls_entry(entry_ids, base_pool)], base_slate, base_pool,
        slot_order=LS_SLOTS, salary_cap=50000, now=LS_NOW, seed=7,
    )
    check("with nothing dead, repair mode changes nothing at all",
          unchanged["entries_changed"] == 0 and unchanged["total_swaps"] == 0, str(unchanged["total_swaps"]))

    # refresh mode: a player nobody scratched, whose projection has
    # simply collapsed since the entry was built.
    demoted_pool = [
        {**p, "projected_fpts": 1.0} if p["id"] == 205 else p for p in ls_pool(base_slate)
    ]
    refreshed = late_swap.swap_batch(
        [ls_entry(entry_ids, base_pool)], base_slate, demoted_pool,
        slot_order=LS_SLOTS, salary_cap=50000, mode="refresh", now=LS_NOW, seed=7,
    )
    check("refresh mode swaps a player whose projection has collapsed since the entry was built "
          "(the real 'confirmed batting 8th after being projected leadoff' case)",
          205 not in [p["id"] for p in refreshed["entries"][0]["players"]],
          str([p["id"] for p in refreshed["entries"][0]["players"]]))
    repair_only = late_swap.swap_batch(
        [ls_entry(entry_ids, base_pool)], base_slate, demoted_pool,
        slot_order=LS_SLOTS, salary_cap=50000, mode="repair", now=LS_NOW, seed=7,
    )
    check("...and repair mode deliberately leaves that same demoted player alone -- he's worse, "
          "not dead",
          205 in [p["id"] for p in repair_only["entries"][0]["players"]], "")

    print("\nJSON serialisation")
    import json

    try:
        blob = json.dumps(slate, default=str)
        check("slate serialises to JSON", len(blob) > 500, f"{len(blob):,} bytes")
    except Exception as exc:  # noqa: BLE001
        check("slate serialises to JSON", False, str(exc))

    print("\nAnalysis billing provider (subscription via Claude Code CLI vs API key)")
    from app.config import get_settings
    from app.services import analysis as analysis_mod

    _settings = get_settings()
    _saved = (_settings.analysis_provider, _settings.claude_code_bin, _settings.anthropic_api_key)

    def _reset_provider(provider, bin_path, api_key):
        _settings.analysis_provider = provider
        _settings.claude_code_bin = bin_path
        _settings.anthropic_api_key = api_key
        analysis_mod.find_claude_code.cache_clear()

    _fake_cli = __file__  # any real existing file stands in for claude.exe
    try:
        _reset_provider("auto", _fake_cli, "sk-test")
        check("auto prefers the subscription-billed Claude Code CLI even when an API key is "
              "also configured -- paying API dollars on top of a subscription is paying twice",
              analysis_mod._resolve_provider() == "claude-code", str(analysis_mod._resolve_provider()))

        _reset_provider("auto", "", "sk-test")
        # No CLI on this test path unless one is genuinely installed; force
        # the not-found case by pointing at a nonexistent explicit binary.
        _reset_provider("auto", r"Z:\nope\claude.exe", "sk-test")
        check("auto falls back to the API key when no CLI is found",
              analysis_mod._resolve_provider() == "api", str(analysis_mod._resolve_provider()))

        _reset_provider("api", _fake_cli, "sk-test")
        check("ANALYSIS_PROVIDER=api forces API billing even with a CLI installed",
              analysis_mod._resolve_provider() == "api", "")

        _reset_provider("api", _fake_cli, "")
        check("...and yields no provider at all when forced to api with no key",
              analysis_mod._resolve_provider() is None, "")

        _reset_provider("claude-code", r"Z:\nope\claude.exe", "sk-test")
        check("ANALYSIS_PROVIDER=claude-code with no CLI yields no provider rather than "
              "silently spending API dollars the user opted out of",
              analysis_mod._resolve_provider() is None, "")

        # The critical safety property: the child environment must not
        # carry the API key, or the CLI would prefer it over the stored
        # subscription login and silently route back onto API billing.
        import subprocess as _sp
        _captured = {}

        def _fake_run(cmd, **kwargs):
            _captured["env"] = kwargs.get("env")
            _captured["cmd"] = cmd
            class R:
                returncode = 0
                stdout = json.dumps({
                    "is_error": False, "result": "ok",
                    "usage": {"input_tokens": 5, "cache_read_input_tokens": 10,
                              "cache_creation_input_tokens": 0, "output_tokens": 3},
                })
                stderr = ""
            return R()

        _orig_run = _sp.run
        _os_mod = analysis_mod.os
        _orig_environ = _os_mod.environ
        try:
            _sp.run = _fake_run
            analysis_mod.subprocess.run = _fake_run
            _os_mod.environ = {**_orig_environ, "ANTHROPIC_API_KEY": "sk-leak", "ANTHROPIC_AUTH_TOKEN": "tok-leak"}
            _result = analysis_mod._run_claude_code(_fake_cli, "prompt", "system", "claude-sonnet-5")
        finally:
            _sp.run = _orig_run
            analysis_mod.subprocess.run = _orig_run
            _os_mod.environ = _orig_environ

        check("the CLI subprocess env carries NEITHER the API key nor an auth token -- either "
              "would silently route the run back onto API billing",
              "ANTHROPIC_API_KEY" not in _captured["env"] and "ANTHROPIC_AUTH_TOKEN" not in _captured["env"], "")
        check("the CLI is invoked headless with tools disabled and a single turn -- a pure "
              "completion, not an agent",
              "-p" in _captured["cmd"] and "--disallowedTools" in _captured["cmd"]
              and "--max-turns" in _captured["cmd"], str(_captured["cmd"]))
        check("a subscription-billed run reports zero marginal dollar cost and total input "
              "tokens including cache reads",
              _result["estimated_cost_usd"] == 0.0 and _result["billing"] == "subscription"
              and _result["input_tokens"] == 15 and _result["output_tokens"] == 3, str(_result))
    finally:
        _reset_provider(*_saved)

    print("\nAnalysis payload")
    from app.services.analysis import _compact_slate

    compact = _compact_slate(slate)
    check("compact payload built", bool(compact["games"]))
    check("compact payload is much smaller than the full slate",
          len(json.dumps(compact, default=str)) < len(json.dumps(slate, default=str)),
          f"{len(json.dumps(compact, default=str)):,} vs {len(json.dumps(slate, default=str)):,} bytes")

    # Regression for a real, observed AI-analysis failure mode: the
    # model read a team's own pitcher and own hitters -- nested
    # together in one JSON object purely because they share a team --
    # as if the pitcher faced those hitters, when he's actually their
    # teammate and faces the OTHER side's hitters instead. Each side's
    # explicit opposing_pitcher_these_hitters_actually_face field must
    # name the OTHER side's starter, never its own.
    compact_game = compact["games"][0]
    home_own_pitcher = compact_game["home"][
        "this_teams_own_starting_pitcher_NOT_an_opponent_of_the_hitters_below"
    ]
    away_own_pitcher = compact_game["away"][
        "this_teams_own_starting_pitcher_NOT_an_opponent_of_the_hitters_below"
    ]
    check("a home team's hitters are shown facing the AWAY pitcher, not their own team's pitcher",
          compact_game["home"]["opposing_pitcher_these_hitters_actually_face"]
          == (away_own_pitcher or {}).get("name"),
          str((compact_game["home"]["opposing_pitcher_these_hitters_actually_face"], away_own_pitcher)))
    check("an away team's hitters are shown facing the HOME pitcher, not their own team's pitcher",
          compact_game["away"]["opposing_pitcher_these_hitters_actually_face"]
          == (home_own_pitcher or {}).get("name"),
          str((compact_game["away"]["opposing_pitcher_these_hitters_actually_face"], home_own_pitcher)))
    check("the home and away starting pitchers are two different real players in this fixture",
          (home_own_pitcher or {}).get("name") != (away_own_pitcher or {}).get("name"),
          str(((home_own_pitcher or {}).get("name"), (away_own_pitcher or {}).get("name"))))

    # Regression for a second real failure: on a 12-game day with a
    # 7-game DK slate, a brief ranked an off-slate game as the
    # second-best environment and named an off-slate hitter as the
    # day's trap. Those players cannot be rostered, so an off-slate
    # game in the prompt is not context, it is a trap. Every consumer
    # of _compact_slate is a prompt about lineups being entered.
    # The base fixture is a single game, so build a four-game day out of
    # it -- two on the DK slate, two not -- which is the shape the real
    # failure happened on (12 MLB games, 7 on the draft group).
    def _relabel(game, tag, in_slate):
        clone = copy.deepcopy(game)
        clone["in_slate"] = in_slate
        clone["game_pk"] = (clone.get("game_pk") or 0) + hash(tag) % 1000
        for side in ("home", "away"):
            clone[side]["name"] = f"{tag}{side[0].upper()}"
            clone[side]["abbrev"] = f"{tag}{side[0].upper()}"
        return clone

    _base_game = (slate.get("games") or [])[0]
    _slate_games = [
        _relabel(_base_game, "ON1", True),
        _relabel(_base_game, "OFF1", False),
        _relabel(_base_game, "ON2", True),
        _relabel(_base_game, "OFF2", False),
    ]
    _mixed = {**slate, "games": _slate_games}
    _on = [g for g in _mixed["games"] if g["in_slate"] is not False]
    _compact_mixed = _compact_slate(_mixed)
    check("_compact_slate drops games that are NOT on the DK slate being played",
          len(_compact_mixed["games"]) == len(_on) and len(_on) < len(_slate_games),
          f"{len(_compact_mixed['games'])} of {len(_slate_games)} games kept")
    _off_names = {
        f"{g['away']['name']} @ {g['home']['name']}"
        for g in _mixed["games"]
        if g["in_slate"] is False
    }
    check("no off-slate matchup survives anywhere in the compact payload",
          not (_off_names & {e["matchup"] for e in _compact_mixed["games"]})
          and not any(n in json.dumps(_compact_mixed, default=str) for n in _off_names),
          str(sorted(_off_names))[:80])
    check("and the payload SAYS games were withheld, so 'not listed' can't read as "
          "'not playing today'",
          "note" in _compact_mixed and "cannot be rostered" in _compact_mixed["note"],
          _compact_mixed.get("note", "")[:70])

    # in_slate is None, not False, when no DK slate has been selected --
    # then there is nothing to filter against and every game belongs.
    _no_dk = {**slate, "games": [{**g, "in_slate": None} for g in _slate_games]}
    # ...and if the DK slate mapped to NOTHING, an empty prompt would be
    # far worse than an unfiltered one, so every game comes back with a
    # warning instead.
    _none_matched = _compact_slate({**slate, "games": [{**g, "in_slate": False} for g in _slate_games]})
    check("a DK slate that matched no games at all falls back to every game AND warns, "
          "rather than handing Claude an empty prompt",
          len(_none_matched["games"]) == len(_slate_games)
          and "WARNING" in _none_matched.get("note", ""),
          f"{len(_none_matched['games'])} games, note: {_none_matched.get('note', '')[:50]}")
    _compact_no_dk = _compact_slate(_no_dk)
    check("with no DK slate loaded every game is still included, and nothing is claimed "
          "to have been withheld",
          len(_compact_no_dk["games"]) == len(_slate_games) and "note" not in _compact_no_dk,
          f"{len(_compact_no_dk['games'])} games")

    # The briefs add their own pitcher/implied-run blocks with their own
    # filter. The bug was that only THAT half filtered, so the prompt
    # disagreed with itself. Both halves must now agree.
    from app.services.briefs import _compact_for_brief

    _brief_compact = _compact_for_brief(_mixed)
    _brief_teams = {r["team"] for r in _brief_compact["implied_runs"]}
    _games_teams = {
        _e[side]["team"] for _e in _brief_compact["games"] for side in ("home", "away")
    }
    check("a brief's games block and its implied-run/pitcher blocks cover the SAME games -- "
          "the two halves can no longer disagree",
          len(_brief_compact["games"]) == len(_on)
          and all(
              any(t and t.endswith(bt) or bt in (t or "") for t in _games_teams)
              for bt in _brief_teams
          ) is not None
          and len(_brief_teams) == 2 * len(_on),
          f"{len(_brief_compact['games'])} games, {len(_brief_teams)} teams in implied runs")

    print("\n" + "=" * 60)
    print(f"{len(PASS)} passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  FAILED: {name}")
    print()
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
