# DFS Edge

A personal daily-fantasy research dashboard. It pulls real MLB stats,
betting lines, weather and batted-ball data, scores every hitter's and
pitcher's matchup, and hands the whole slate to Claude for a written
read.

Built to run on your own machine for free (or close to it). NFL and NBA
are designed for but not built yet — see [Roadmap](#roadmap).

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
matchup score where 50 is a league-average spot. Nine things feed it:

| Component | Weight | What it asks |
|---|---|---|
| Platoon split | 21% | How does he hit pitchers of this hand? |
| Vegas implied runs | 19% | How many runs is his team expected to score? |
| Pitcher vulnerability | 16% | How does this pitcher do against batters of his hand? |
| Contact quality | 15% | What do his Statcast barrel rate, hard-hit rate and xwOBA say, independent of luck? |
| Park factor | 10% | Does this park help home runs *for his handedness*? |
| Bullpen quality | 8% | How shaky is the relief corps he'll face after the starter leaves? |
| Weather | 6% | Is the ball carrying? Is the wind helping — for real, using the park's actual orientation? |
| Recent form | 3% | Hot or cold over the last 15 games? |
| Home/road split | 2% | Does he travel well? |

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
│   │   │   ├── odds.py       The Odds API
│   │   │   ├── savant.py     Baseball Savant batted-ball CSV export
│   │   │   ├── weather.py    Open-Meteo + wind/temp effects
│   │   │   └── http.py       shared HTTP client with retries
│   │   ├── data/parks.py     park factors, coordinates, roofs, wind orientation
│   │   ├── services/
│   │   │   ├── scoring.py    THE MODEL - all weights live here (hitter + pitcher)
│   │   │   ├── mlb_slate.py  assembles the daily slate
│   │   │   ├── analysis.py   Claude integration
│   │   │   ├── salaries.py   DraftKings salary CSV upload
│   │   │   ├── projections.py  RotoWire FPTS/ownership CSV upload
│   │   │   └── player_match.py  shared name/team matching for both uploads
│   │   └── routers/          HTTP endpoints
│   └── tests/test_pipeline.py  offline test, no API calls
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

## Improving it from here

**1. Track your own results.** Log each night's scores and what actually
happened. After a month you can check whether your weights are any good,
which is the only way to know. A `results` table in the same SQLite file
is enough.

**2. Then NFL.** The architecture already assumes it: add
`clients/nfl.py` (nfl_data_py is excellent and free), `services/nfl_slate.py`,
and a `routers/nfl.py`. The scoring module's component pattern carries
over directly — position vs defense, pace, spread-implied game script.

---

## Roadmap

- [x] MLB: splits, park factors, weather (with real wind orientation), lines, AI analysis
- [x] Batted-ball data from Baseball Savant
- [x] Bullpen strength
- [x] Top Pitchers tab with strikeout-aware scoring
- [x] Key injuries and game start times
- [x] DraftKings salaries + value scores
- [x] RotoWire FPTS/ownership projections
- [ ] Results tracking and weight backtesting
- [ ] NFL
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
