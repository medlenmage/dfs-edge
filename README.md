# DFS Edge

A personal daily-fantasy research dashboard. It pulls real MLB stats,
betting lines, weather and batted-ball data, scores every hitter's and
pitcher's matchup, and hands the whole slate to Claude for a written
read.

Built to run on your own machine for free (or close to it). NFL is now
in too, alongside MLB — see [NFL](#nfl) below. NBA is designed for but
not built yet — see [Roadmap](#roadmap).

![Stacks view](docs/screenshots/stacks.png)

---

## What it actually does

**Pulls data you'd otherwise gather by hand:**

| Source | What it gives you | Cost |
|---|---|---|
| MLB Stats API | schedules, probable pitchers, confirmed lineups, every player's splits vs LHP/RHP and home/road, last-15-game form, roster status (injuries), bullpen ERA | free, no key |
| Open-Meteo | temperature, wind speed and direction, rain probability at each park | free, no key |
| Baseball Savant | barrel rate, hard-hit rate, expected wOBA (real contact quality, not just outcomes) | free, no key |
| The Odds API | game totals, moneylines, run lines → implied team runs | free tier, or $30/mo |
| Anthropic API | the written slate analysis | pay per use, ~2-5¢ a run |

**Turns it into one number per hitter.** Every hitter gets a 0-100
matchup score where 50 is a league-average spot. Ten things feed it:

| Component | Weight | What it asks |
|---|---|---|
| Platoon split | 19% | How does he hit pitchers of this hand? |
| Vegas implied runs | 18% | How many runs is his team expected to score? |
| Pitcher vulnerability | 15% | How does this pitcher do against batters of his hand? |
| Contact quality | 14% | What do his Statcast barrel rate, hard-hit rate and xwOBA say, independent of luck? |
| Stolen-base rate | 8% | Is he actually going to run — DraftKings pays +5 for a steal, same as a double, and nothing else here measures it |
| Park factor | 9% | Does this park help home runs *for his handedness*? |
| Bullpen quality | 7% | How shaky is the relief corps he'll face after the starter leaves? |
| Weather | 6% | Is the ball carrying? Is the wind helping — for real, using the park's actual orientation? |
| Recent form | 3% | Hot or cold over the last 15 games? |
| Home/road split | 1% | Does he travel well? |

Nothing is hidden. Click into any player in the API response and you see
each component's value, its sample size, and a plain-English reason.

**The same model runs in reverse for pitchers.** The Top Pitchers tab
scores every probable starter with the mirror-image logic:

| Component | Weight | What it asks |
|---|---|---|
| Opposing lineup | 20% | How do these hitters, specifically, score against him? |
| Strikeout potential | 17% | His K stuff, blended with how whiff-prone this lineup is |
| Vegas implied runs against | 17% | How many runs is the team facing him expected to score? |
| Contact quality allowed | 14% | Barrel/hard-hit/xwOBA he's allowing, independent of luck |
| Own quality | 12% | His season ERA vs league average |
| Park factor | 11% | Does this park suppress runs and home runs? |
| Weather | 9% | Suppression side of the same wind/temperature read |

So a start looking good tonight is scored with the same rigor as a bat.

**Then Claude reads the whole slate.** It only sees numbers the app has
already computed — it is never asked to recall a stat from memory. Its
job is the part models are actually good at: looking at 15 games at once,
spotting where the signals disagree, and telling you which edges are real
versus which are park factor in a trench coat.

**Two things it doesn't compute for you, but will show you.** Upload a
DraftKings salary CSV and every player gets a Salary and Value (edge
score per $1,000) column — the score tells you who's in a good spot, the
value column tells you who's worth it. Upload a RotoWire projections CSV
and you get their FPTS and ownership% projections as reference columns.
Neither feeds the matchup score itself — they're someone else's numbers
(a price, a projection), not a component this app can vouch for the way
it can vouch for a platoon split it computed itself.

---

## Getting it running

You need **Python 3.11+** and **Node 18+**. Check what you have:

```bash
python3 --version
node --version
```

If either is missing: [python.org/downloads](https://www.python.org/downloads/)
and [nodejs.org](https://nodejs.org/).

### 1. Get the code and set up the backend

```bash
cd dfs-edge

cd backend
python3 -m venv .venv                    # isolated Python environment
.venv/bin/pip install -r requirements.txt
cd ..
```

> **What's a venv?** A private folder of Python packages just for this
> project, so installing something here can't break anything else on your
> machine. You never "activate" it if you don't want to — just call
> `.venv/bin/python` and `.venv/bin/pip` directly, as above.

### 2. Add your API keys

```bash
cp .env.example .env
```

Open `.env` in any text editor and fill in what you have. **Every key is
optional** — the app starts without any of them and simply switches off
the features that need one.

- **`ANTHROPIC_API_KEY`** — from [console.anthropic.com](https://console.anthropic.com).
  Enables the AI analysis tab.
- **`ODDS_API_KEY`** — from [the-odds-api.com](https://the-odds-api.com).
  The free tier is 500 credits/month, which is only a handful of pulls per
  day. The **$30/month plan is 20,000 credits**, which comfortably covers
  refreshing lines every 10 minutes all season.

Implied team runs are one of the two strongest signals in the whole model,
so the Odds API key is the one worth paying for.

### 3. Check everything works

```bash
cd backend
.venv/bin/python ../scripts/doctor.py
```

This tells you which pieces are live and which need a key, in plain
language, before you try to start anything.

### 4. Set up the frontend

```bash
cd frontend
npm install
```

### 5. Run it

You need **two terminal windows**, one for each half.

Terminal 1 — the API:

```bash
cd backend
.venv/bin/uvicorn app.main:app --reload
```

Terminal 2 — the dashboard:

```bash
cd frontend
npm run dev
```

Open **http://localhost:5173**.

> The first load of a given day takes 20-30 seconds — it's pulling a full
> season of league-wide splits. After that everything is cached and loads
> instantly. Cache lifetimes are configurable in `.env`.

### Want to look around before adding any keys?

```bash
cd frontend && npm run build && cd ../backend
.venv/bin/python ../scripts/preview.py
```

Open **http://127.0.0.1:8001** for the full dashboard running on fake
data. Useful in the off-season too.

---

## Using it

**Stacks tab** — teams ranked by how good it is to stack them, with each
game's start time. Stacking means rostering 4-5 hitters from one team so
a big inning pays you multiple times. The stack score is the average
matchup score of a team's five best bats.

**Hitters tab** — every hitter on the slate, sortable by any column,
including salary/value and FPTS/ownership once you've uploaded those
CSVs. Filter by minimum score to cut to the good spots.

**Pitchers tab** — today's probable starters ranked by matchup edge, the
mirror image of Stacks: strikeout upside, the lineup he's facing, the
Vegas total against him, and the rest of the pitcher-side model.

**Games tab** — the environment for each game: park factors, weather
(including a real in/out/cross-wind read, not a guess), the betting
total, each side's implied runs, and key injuries for both teams.

**AI analysis tab** — Claude's writeup, plus a box to ask follow-ups
("who's the best cheap bat in a good park tonight?").

**Upload salaries / Upload projections** (top-right of every tab) — drop
in a DraftKings salary CSV or a RotoWire player-pool CSV for the day's
slate. Both are cached per date and re-matched automatically on refresh.

### The rhythm that works

1. **Morning** — open it, look at the Games tab. Where are the high
   totals? Any weather that'll matter?
2. **Afternoon** — check Stacks. Note which teams look good but haven't
   confirmed lineups yet.
3. **~2 hours before first pitch** — lineups drop. Hit **Refresh**. Now
   the batting orders are real, and a guy hitting 2nd is worth far more
   than the same guy hitting 8th.
4. **Then** — run the AI analysis and read it against what you already
   decided. Where it disagrees with you is the interesting part.

### Reading the score honestly

A 78 is not a projection and it is not a lock. It says "this matchup is
well above average on the nine things we measure." Things it does not
know about: a hitter playing hurt, a bullpen that threw 40 pitches last
night, a manager who rests people on getaway days, DFS salary and
ownership.

Two specific cautions:

- **Small samples get regressed, but not to zero.** A hitter with 40 PA
  against lefties keeps about a third of his observed edge; the rest is
  assumed league-average. Check the PA column before you trust a split.
- **A score driven by park and weather is weaker than one driven by
  skill.** The `top_driver` field tells you which. Everyone else on the
  slate can see that Coors is Coors; not everyone has looked at the
  platoon splits.

---

## Cost

| | Free tier | Recommended |
|---|---|---|
| MLB data | free forever | free forever |
| Weather | free forever | free forever |
| Betting lines | 500 credits/mo | $30/mo for 20,000 |
| AI analysis | ~2-5¢ per run | ~$3-8/month if you run it daily |
| Hosting | $0 (your machine) | $0 |
| **Total** | **~$2/month** | **~$35-40/month** |

Keep an eye on your Odds API balance at `http://localhost:8000/api/health`
— it's also shown in the dashboard footer.

**Turning on player props** (`ODDS_FETCH_PROPS=true`) is the one thing
that can burn credits fast: props are billed per game per market, so one
refresh across a 15-game slate costs ~60 credits. Leave it off until you
want it.

---

## How the code is laid out

```
dfs-edge/
├── .env                      your keys (never committed)
├── .env.example              the template
│
├── backend/
│   ├── app/
│   │   ├── main.py           FastAPI app, CORS, startup
│   │   ├── config.py         reads .env
│   │   ├── cache.py          SQLite response cache
│   │   ├── clients/          one file per external API
│   │   │   ├── mlb.py        MLB Stats API (splits, rosters, injuries, bullpens)
│   │   │   ├── nfl.py        nflverse schedule + betting-line CSV export
│   │   │   ├── odds.py       The Odds API
│   │   │   ├── savant.py     Baseball Savant batted-ball CSV export
│   │   │   ├── weather.py    Open-Meteo + wind/temp effects
│   │   │   └── http.py       shared HTTP client with retries
│   │   ├── data/parks.py     park factors, coordinates, roofs, wind orientation
│   │   ├── data/nfl_stadiums.py  stadium coordinates + timezones
│   │   ├── services/
│   │   │   ├── scoring.py    THE MODEL - all weights live here (hitter + pitcher)
│   │   │   ├── nfl_scoring.py  the NFL matchup model
│   │   │   ├── mlb_slate.py  assembles the daily MLB slate
│   │   │   ├── nfl_slate.py  assembles the weekly NFL slate
│   │   │   ├── optimizer.py  MLB DraftKings Classic lineup optimizer
│   │   │   ├── contest.py    MLB contest generator: opponent field + mass multi-entry
│   │   │   ├── nfl_optimizer.py  NFL DraftKings Classic lineup optimizer
│   │   │   ├── analysis.py   Claude integration
│   │   │   ├── salaries.py   DraftKings salary CSV upload (MLB + NFL)
│   │   │   ├── projections.py  RotoWire FPTS/ownership CSV upload (MLB + NFL)
│   │   │   └── player_match.py  shared name/team matching for both uploads
│   │   └── routers/          HTTP endpoints (mlb.py, nfl.py, system.py)
│   └── tests/
│       ├── test_pipeline.py      MLB offline test, no API calls
│       └── test_nfl_pipeline.py  NFL offline test, no API calls
│
├── frontend/src/
│   ├── App.jsx               layout and tabs
│   ├── api.js                every backend call
│   └── components/           tiles, meters, tables, cards
│
└── scripts/
    ├── doctor.py             connectivity check - run this first
    ├── preview.py            demo mode on fake data
    └── screenshots.py        regenerate the docs images
```

**If you want to change how the model thinks, there is one file:**
`backend/app/services/scoring.py`. The weights are at the top. Change a
number, save, and the API reloads.

### Running the tests

```bash
cd backend
.venv/bin/python -m tests.test_pipeline
```

70 checks covering the whole pipeline on fake data — no network, no
credits spent. Run it after you touch the scoring model.

---

## Shipped since the first version

All of these started as items on this list. Leaving them here, marked
off, so it's clear what changed and why rather than just quietly
vanishing from the doc:

- **Batted-ball quality.** `clients/savant.py` pulls Baseball Savant's
  barrel rate, hard-hit rate and xwOBA CSV export; `contact_quality`
  (hitter) and `contact_quality_allowed` (pitcher) fold it into both
  scores and into the Stacks tab.
- **Bullpen quality.** `bullpen` component — the opposing team's relief
  corps ERA, since a starter only throws ~5 innings.
- **Top Pitchers tab.** A full mirror-image scoring model for probable
  starters — see `PITCHER_WEIGHTS` in `scoring.py` — with real weight on
  strikeout upside: the pitcher's own K stuff blended with how
  whiff-prone the lineup he's facing actually is.
- **Key injuries and game start times.** Roster status from the MLB
  Stats API on the Games tab; start times on Stacks.
- **DFS salaries and value.** Upload a DraftKings CSV, get Salary and
  Value (edge score per $1,000) columns.
- **FPTS/ownership projections.** Upload a RotoWire player-pool CSV, get
  their projections as reference columns — deliberately kept out of the
  edge score itself; see the note above on why.
- **Real park orientations for wind.** `wind_effect()` always accepted a
  park orientation but nothing passed one in, so every wind read
  defaulted to "north" and flagged itself low-confidence — sometimes
  just wrong. `data/parks.py` now has real bearings for all 30 parks.
- **Stolen-base rate.** DraftKings pays +5 for a steal, the same as a
  double, but every prior version of the model was purely OPS- and
  contact-quality-based — a low-power, high-steal hitter got zero
  credit for an entire category of his real DK value. `stolen_base`
  compares season-long SB rate against the league average, regressed
  by sample size the same way every other split-based component is.
- **MLB contest field generator.** The optimizer only ever answered "is
  this the best lineup" — never "how does it compare to what everyone
  else is rostering." `services/contest.py` builds a synthetic public
  field by randomly sampling each roster slot weighted by RotoWire
  ownership% (the signal that actually describes what the public plays,
  unlike another optimizer solve, which would just build a pile of
  near-identical near-optimal lineups). A large real contest
  (thousands to 100,000+ entries) is modeled as a statistical *sample*
  capped at `MAX_SAMPLE_SIZE` (5,000, chosen after benchmarking — a
  5,000-lineup field builds in about a second even on a full slate),
  with a lineup's rank projected back onto the real contest size the
  way a poll projects from a sample. On the Lineups tab, generate a
  lineup as usual, then use the new "Contest field" section to test it
  against a double-up, small-field GPP, large-field GPP, or
  millionaire-maker-style preset — same slate-game filter as the
  optimizer, so the field is always drawn from the actual games in
  play. **Not a lineup simulator**: there's no player-outcome variance
  model yet, so ranks and payouts are the field's *projected* points,
  not a distribution of real-world outcomes — which is also why an
  optimizer-built lineup (itself point-maximizing) tends to rank near
  the top of the field on this measure. The payout curve is a
  deliberately simple, clearly-labeled approximation (flat for
  double-ups, a smooth top-heavy decay for GPPs), not a scraped or
  hardcoded real payout table.
- **Contest Generator: mass multi-entry.** The field generator above
  models *opponents*. This is the other half: a standalone "Contest
  Generator" tab (separate from the Lineups tab's exact optimizer) that
  builds up to 10,000 of *your own* entries in one request via
  `contest.generate_entries()` -- fast randomized construction weighted
  toward projected points (not ownership%), deduplicated so no two
  entries in a batch are identical. The exact MILP optimizer is the
  right tool for a handful of provably-best lineups (capped at 150),
  the wrong one for mass entry: solving a fresh MILP thousands of times
  is both too slow for one request and not what a real GPP portfolio
  wants anyway (many individually-strong, genuinely different builds,
  not the same "best" lineup re-solved with weaker and weaker no-good
  cuts). The batch gets ranked against a simulated opponent field for
  cash-rate/payout economics, same presets as the field generator.
  Caught and fixed a real bug during this build: ranking each entry of
  a large batch *independently* against the field let thousands of
  individually-strong entries each claim the same top payout, summing
  to many times the real prize pool (one test case: $2.5M "profit"
  against a $42,500 pool). `_evaluate_batch_against_field()` fixes this
  by ranking the whole batch against the field *and against each
  other* for the same limited paid ranks -- and `num_lineups` is now
  validated against the contest's real `field_size` up front (your
  entries are part of the field, not additional to it).

## NFL

Click **NFL** next to the app name to switch sports. It's a weekly
slate, not a daily one — pick a season and week (defaults to whichever
week's games haven't finished yet) instead of a date.

**Data sources, all free, no key:**

| Source | What it gives you |
|---|---|
| [nflverse](https://github.com/nflverse/nflverse-data) schedule export | every game, closing spread/total/moneylines, roof, surface |
| Open-Meteo | same weather pull as MLB, for outdoor games |

(`nfl_data_py`, the README's original plan for this, turned out to pin
an old pandas that won't build on a current Python — fetching
nflverse's own CSV releases directly sidesteps that and matches how
`clients/savant.py` already does CSV-export data anyway.)

**Matchups tab** shows each game with its Vegas-implied team totals,
spread, and weather. **Lineups tab** is a DraftKings Classic NFL
optimizer (QB, RB, RB, WR, WR, WR, TE, FLEX, DST) with multi-lineup
generation, exposure caps, a salary floor, and QB stacking (force at
least N of the rostered QB's own WR/TEs into the lineup — the standard
NFL GPP correlation play, the same idea as MLB's hitter stacking).
Upload a DraftKings salary CSV and a RotoWire projections CSV for the
week the same way you would for MLB.

**Still smaller than the MLB model, deliberately.** MLB's matchup score
leans on a full season of granular per-player split data. NFL's runs on
Vegas implied team total, game script from the spread, home field, and
weather — plus, as of this component, **defense-vs-position and pace**,
computed from a full prior completed season's real box scores rather
than in-season data that doesn't exist before Week 1. Used as a static
prior the same way a small-sample MLB stat gets shrunk toward league
average, just shrunk all the way there since there's zero current-season
sample yet:

| Source | What it gives you |
|---|---|
| [nflverse](https://github.com/nflverse/nflverse-data) `player_stats` export | one full season's per-player, per-game box scores |

`clients/nfl.get_prior_season_context()` fetches that CSV once (cached),
computes DraftKings points for every player-game with DK's own scoring
rules (not the export's generic PPR column, which has different
bonuses/penalties), and aggregates two things per team: DK points
allowed per game to each of QB/RB/WR/TE (the classic "defense vs.
position" signal), and offensive plays run per game (pace — more snaps
means more opportunity for everyone on that offense). `PRIOR_SEASON` in
that same file is the one line to bump once nflverse publishes the next
season's stats — **it's currently pinned to 2024**, since nflverse
hadn't published 2025 numbers yet as of when this shipped.

See `services/nfl_scoring.py` for exactly what's in and why.

## Improving it from here

**1. Track your own results.** Log each night's scores and what actually
happened. After a month you can check whether your weights are any good,
which is the only way to know. A `results` table in the same SQLite file
is enough.

**2. NFL: bump `PRIOR_SEASON`.** Once nflverse publishes a season closer
to the current one, change the one constant in `clients/nfl.py` — the
defense-vs-position and pace pipeline doesn't otherwise change.

---

## Roadmap

- [x] MLB: splits, park factors, weather (with real wind orientation), lines, AI analysis
- [x] Batted-ball data from Baseball Savant
- [x] Bullpen strength
- [x] Top Pitchers tab with strikeout-aware scoring
- [x] Key injuries and game start times
- [x] DraftKings salaries + value scores
- [x] RotoWire FPTS/ownership projections
- [x] Lineup optimizer (MLB and NFL), including a lineup-confirmation watcher for MLB
- [x] NFL: weekly matchups + Classic lineup optimizer with QB stacking
- [x] NFL: pace and defense-vs-position scoring components (prior-season prior)
- [x] MLB: contest field generator (ownership-weighted synthetic field, ranked by projected points)
- [x] MLB: standalone Contest Generator tab for mass multi-entry (up to 10,000 of your own entries, points-weighted, ranked against the field for cash/payout economics)
- [ ] MLB: lineup simulator (player-outcome variance model + Monte Carlo contest simulation) -- the harder follow-up to both contest-generator features above; needs a real per-player variance model, not just a point estimate, before it's worth building
- [ ] Results tracking and weight backtesting
- [ ] NBA

---

## Troubleshooting

**"Could not reach MLB Stats API"** — usually just internet. Run
`scripts/doctor.py` to confirm.

**Dashboard loads but everything is empty** — check there are actually
games that day, and that the backend terminal isn't showing errors. The
date picker uses your local date.

**"The Odds API rejected your key (401)"** — the key in `.env` is wrong
or has a stray space. Copy it again from the-odds-api.com dashboard.

**"422 - unsupported market"** — you turned on player props on the free
plan. Props need a paid tier. Set `ODDS_FETCH_PROPS=false`.

**Stale data after lineups drop** — hit Refresh, or
`curl -X POST "http://localhost:8000/api/cache/clear?prefix=mlb:lineups"`.

**Everything is slow** — first load of a day pulls a season of splits.
Subsequent loads are cached. If it's slow every time, your cache
database may not be writable; `doctor.py` checks this.

---

## A note on what this is

This is a research tool, not a betting system. It organises public
information faster than you could by hand and gives you a consistent
framework for comparing spots. It does not predict outcomes, and no
model does — DFS has enormous variance and most entrants lose money over
time. Play with money you're fine losing, and treat the AI writeup as
one more opinion rather than an answer.
