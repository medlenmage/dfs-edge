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
    contest,
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
              "name": "Chris Sale", "normalized_name": "chris sale", "team": "ATL",
              "position": "SP", "salary": 10300, "avg_points": 23.26,
              "game_info": "ATL@MIN 08/17/2026 07:40PM ET", "dk_id": "43854626",
          }, str(wide_rows[0]))

    check("a CSV with no recognizable DK header returns no players rather than raising",
          salaries.parse_dk_csv("just,some,random,csv\n1,2,3,4\n") == [])

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
            }
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

    # Stack-shape behavior (manual/auto team assignment, partial shapes,
    # validation) gets its own dedicated section below with a deeper
    # fixture -- opt_slate's second team only has 1 hitter, not enough
    # depth to prove an *exact* group-size constraint on its own.

    try:
        optimizer.generate_lineups({"games": []})
        check("optimizer raises OptimizerError on an empty pool", False)
    except optimizer.OptimizerError:
        check("optimizer raises OptimizerError on an empty pool", True)

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
                "home": {"abbrev": "MUL3", "hitters": [],
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
            }
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

    try:
        optimizer.generate_lineups(mul_slate, min_unique_players=0)
        check("optimizer rejects min_unique_players below 1", False)
    except optimizer.OptimizerError:
        check("optimizer rejects min_unique_players below 1", True)

    try:
        optimizer.generate_lineups(mul_slate, min_unique_players=optimizer.ROSTER_SIZE + 1)
        check("optimizer rejects min_unique_players above ROSTER_SIZE", False)
    except optimizer.OptimizerError:
        check("optimizer rejects min_unique_players above ROSTER_SIZE", True)

    def lineup_teams(lu):
        return {p["team"] for slot in lu["slots"].values() for p in slot}

    default_teams = optimizer.generate_lineups(mul_slate)["lineups"][0]
    check("without a team-count bound, the unconstrained solve naturally uses 2 teams",
          len(lineup_teams(default_teams)) == 2, str(lineup_teams(default_teams)))

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
        pitcher_contra = opt_pitcher(9852, "Pcontra1", 6000, 14.5 - d, own=5)
        hitters.append(opt_pitcher(9851, "Pchalk2", 6000, 15 + d, own=40))
        hitters.append(opt_pitcher(9853, "Pcontra2", 6000, 14.5 - d, own=5))
        return {
            "games": [
                {
                    "home": {"abbrev": "OWN", "hitters": hitters,
                             "probable_pitcher": pitcher_chalk, "scratches": []},
                    "away": {"abbrev": "OWO", "hitters": [],
                             "probable_pitcher": pitcher_contra, "scratches": []},
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
    check("all ten components present",
          len(righty["edge"]["components"]) == 10,
          str(sorted(righty["edge"]["components"])))
    check("weights sum to 1.0",
          abs(sum(scoring.WEIGHTS.values()) - 1.0) < 1e-9,
          str(sum(scoring.WEIGHTS.values())))

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

    restricted = contest.generate_field(mul_slate, 30, included_game_pks=[88001], seed=3)
    restricted_teams = {p["team"] for lu in restricted for p in lu["players"]}
    check("included_game_pks restricts the field to only that game's teams",
          restricted_teams <= {"MUL1", "MUL2"}, str(restricted_teams))

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
