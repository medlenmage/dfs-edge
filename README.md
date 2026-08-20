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
│   │   │   ├── mlb_dk_points.py  raw DK points from one game's box score (variance model, in progress)
│   │   │   ├── variance.py   per-player outcome distribution pools (variance model, in progress)
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
- **Fixed: DK salary upload rejecting a valid export.** DraftKings'
  lineup-builder page's "Export to CSV" button ships a wider file than
  the flat player-pool export this app was originally built against --
  an empty roster-slot template occupies the first several
  rows/columns, with the real player table's header embedded well past
  row 1. `csv.DictReader` always takes row 1 as the header, so against
  that shape it found none of the expected columns and every row got
  silently skipped, even though the file was perfectly valid.
  `salaries._find_dk_header_row()` scans for the row instead of
  assuming its position.
- **Salary from a RotoWire upload, when it's already there.**
  RotoWire's player-pool export pulls salary straight from DK into its
  own SAL column -- the same number a separate DK upload would give,
  just bundled into one file. If no salary file is loaded yet for a
  date, uploading projections now seeds the salary store from that SAL
  column too (`salaries.from_rotowire_rows()`), so a file with both
  projections and salaries only needs uploading once. A real DK upload,
  now or later, is never overwritten by a projections re-upload -- the
  two gaps versus an actual DK export (`game_info` for DK-slate
  auto-detection, `dk_id` for NFL's optimizer) are handled by leaving
  them empty rather than guessed at, and this is MLB-only for exactly
  the `dk_id` reason.
- **CSV export, for handing lineups off to something outside this app.**
  Both the optimizer's Lineups tab and the mass Contest Generator now
  have a "Download CSV" button -- one row per lineup, one column-group
  per DK roster slot (name, team, salary, projected points,
  ownership%), plus the estimated rank/cash/payout when the batch was
  ranked against a contest field. Meant for a Monte Carlo simulator, a
  spreadsheet, or another Claude session working from the file, not for
  DK's own bulk-upload format (that needs a contest-specific template
  with entry/contest IDs this app has no way to get). The optimizer's
  export is client-side (its lineups, capped at 150, are already fully
  loaded in the browser); the Contest Generator's is server-side --
  `GET /api/mlb/contest-entries/{batch_id}/csv` downloads the *entire*
  generated batch (up to 10,000 rows), not just the 200-row preview the
  JSON response caps itself at, and the batch is cached under that id
  for an hour so the CSV always matches exactly what was reviewed on
  screen rather than silently re-rolling a different random batch.

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
- [x] MLB: lineup simulator (player-outcome variance model + Monte Carlo contest simulation) -- shipped in 6 phases (see `.claude/plans/` for the full roadmap). Phase 1 shipped: `clients/mlb.get_player_game_log()` (a per-game stat fetcher nothing else in this app had) and `services/mlb_dk_points.py` (raw DK points from one game's box score). Phase 2 shipped: `services/variance.py`'s `player_outcome_pool()` -- a bootstrap resampling pool of real DK-point outcomes per player, not a fitted parametric shape, blended with a shared same-position pool for thin samples (a rookie call-up leans on it heavily; an everyday player barely touches it). Verified against real 2024 data: 10 independent fresh samples of Ohtani's pool averaged 13.20 (true season mean 13.06), and a synthetic same-mean "boom/bust" player's pool showed ~6.6x the standard deviation of a "consistent" player's -- proof the model captures real spread, not just the average. Phase 3 shipped: `team_environment_multiplier()` and `sample_correlated_outcome()` -- a per-trial, per-team multiplier shared by every hitter on that team, biasing which percentile of each hitter's own pool gets sampled, so a real stack shows fatter-tailed outcomes than the same players simulated independently. Verified against real 2024 Dodgers hitters (Ohtani, Betts, Freeman, Hernandez, Smith): a stacked simulation landed at essentially the same mean as the unstacked equivalent (57.53 vs 56.56) but with 1.64x the standard deviation -- proof the correlation mechanism is doing real work, not just adding noise. Phase 4 shipped: `numpy` (confirmed installing cleanly on this project's Python 3.14 `.venv`) and `services/variance.py`'s `simulate_batch()` -- a vectorized Monte Carlo engine that samples every *unique* player across a whole batch of lineups once per trial (not once per lineup containing them), applying Phase 3's team correlation to hitters and independent uniform sampling to pitchers. Accepts both lineup shapes already in this codebase (optimizer.py's `slots`-grouped lineups and contest.py's flat `players` entries) via the same normalization `lineup_export.py`'s CSV export already relies on. Verified against real 2024 data: two lineups sharing 5 of 6 players (Cole + 5 Dodgers hitters vs. Strider + the same 5 hitters) showed 0.89 correlation between their simulated trial-by-trial totals -- proof shared players genuinely link outcomes across lineups in the same simulated batch, not just within one lineup. Also benchmarked at realistic scale: 2,000 lineups x 2,000 trials in 0.07s. Phase 5 shipped: `contest.py`'s `evaluate_batch_simulated()` and `build_contest_entries_simulated()` -- a genuine Monte Carlo alternative to the deterministic `build_contest_entries()` (kept as-is, unchanged, as the fast default), simulating a user's entries and the sampled opponent field together and reporting each entry's real cash **probability** (the fraction of trials it actually lands in the paid zone) and an expected-payout range (10th/90th percentile), not a single projected-points-vs-field estimate. Two entries can never share a paid rank within the same simulated trial -- enforced per trial via a vectorized closed-form of the same "distinct ranks" rule `_evaluate_batch_against_field` already used once, checked directly against a brute-force per-trial reference implementation. Verified live against today's real slate: 15 entries simulated against a 200-lineup sampled field over 1,000 trials in ~4s, landing at a sane 16-32% cash probability range for a small GPP with a 20% payout line. Phase 6 shipped: `POST /api/mlb/contest-entries-simulated` and a "Simulate" toggle in the Contest Generator tab (trial count selectable: 500/1,000/2,000/5,000) -- when on, the economics card shows genuine average cash probability and expected payout instead of the deterministic estimate, and the sample-entries table swaps in a "Sim floor-ceiling" column (10th-90th percentile simulated points) plus per-entry cash%/expected payout in place of rank/cashing. The fast deterministic default is untouched and still one click away. Verified live in the browser against today's real slate and uploaded DK/RotoWire files: toggling Simulate produced a 25.2% avg cash probability and a $49.49 estimated net over 10 real entries at 500 trials; toggling back off reproduced the original deterministic numbers exactly, confirming no regression to the existing fast path. Since then: simulated results also report each entry's 1st-place/top-1%/top-10% finish rates and simulated ROI% (color-coded green/red), sorted highest-ROI-first, with a working CSV export for the simulated shape -- and the simulator now always runs a fixed 10,000 trials (no more picking a trial count) and gained a second mode, "My DK entries": upload the actual bulk-entries CSV DraftKings gives you (`services/dk_entries.py` parses it, matching each roster cell's DK player id back to this app's own player pool via the file's own embedded player-pool table, then by name/team against the live slate) to simulate the lineups you actually built or reserved against one real contest's own economics -- entry fee comes straight from the file; total contest entries, prize pool, and 1st-place % are hand-entered since a bulk entries export has no payout-table data at all, and `contest.py`'s `_custom_payout_curve()` pins 1st place to exactly that percentage while the rest of the field still decays with the same top-heavy shape used elsewhere. The file's real job is establishing the baseline (how many entries you have in a contest and what each one costs), not requiring pre-filled picks: a freshly-reserved contest -- the common real case -- has every entry generate a fresh lineup, and any entries you'd already built yourself are simulated as-is alongside the generated ones. Verified against a real DraftKings entries export (20 reserved entries, all still blank): end to end, all 20 got generated lineups and simulated correctly, with `total_entry_cost` matching the real $0.25 entry fee x 20 exactly, both via direct calls and the real HTTP upload/simulate endpoints. Since then: "My DK entries" was redesigned again to mirror a real contest's *entire field*, not just the entries you've personally reserved -- the uploaded file's only remaining job is supplying `entry_fee`; `services/dk_entries.py`'s old DK-id-to-internal-player matching (`resolve_entries()`) was removed entirely, since there's no per-entry roster to resolve anymore. `contest.py`'s new `evaluate_field_mirrored()` builds an ownership-weighted sample standing in for the real contest's whole field (`generate_field()`'s existing chalk-heavy, duplicates-allowed construction, per the user's explicit choice over the individually-strong-and-distinct construction "Generate entries" uses) and ranks every sampled lineup against every *other* sampled lineup in the same simulated trial -- via `np.argsort` for a best-to-worst order per trial -- rather than against a separately generated "your entries" batch. Each lineup's rank within the sample is projected onto the real (usually much larger) `field_size` by an exact, collision-free linear interpolation: the k-th best of `sample_size` lineups maps to `1 + floor(k * (field_size-1) / (sample_size-1))`, strictly increasing whenever `field_size >= sample_size` -- checked directly against a hand-derivable case (5 sampled lineups standing in for a 21-entry field map to ranks `[1, 6, 11, 16, 21]`, evenly spaced by exactly 5). `build_dk_entries_simulated()` was rewritten around this: it auto-caps the sample at the existing `MAX_SAMPLE_SIZE` (5,000) for large real fields, the same "simulate a sample, project onto the real size" philosophy this module already used for a synthetic opponent field, now applied to the whole field rather than just one side of it. Benchmarked live against a real slate and the user's stated real-world scale (`field_size=9,512`, `sample_size` auto-capped to 5,000, 10,000 trials): 5.61 seconds, with sane validating statistics (~20% average cash probability, matching the 20% payout line; slightly negative average ROI, reflecting the house rake; strong best-vs-worst differentiation among sampled lineups). The frontend's "My DK entries" mode now reads "mirrors the contest, simulates the whole field, browse the results to pick your own entries" instead of "generates lineups for your blank reservations," and dropped the now-inapplicable max-exposure control (a full mirrored field has no "your batch" to cap exposure within). Since then: every lineup/entry (from the optimizer, the contest generator, and the DK entries mirror) now carries its own `stack_type`/`stack` fields -- `lineup_export.py`'s new `stack_info()` derives them purely from a lineup's actual hitter-team composition (any team supplying 2+ of the 8 non-pitcher slots, ordered largest group first, e.g. 5 Yankees + 3 Braves is `stack_type="5-3"`, `stack="NYY,ATL"`), not from any intended stack-shape input, so it works for a lineup built any way. `lineups_to_csv()`'s output was also simplified to match a user-provided reference file exactly: the per-player `_team`/`_salary`/`_proj_fpts`/`_own_pct` sub-columns were dropped (keeping just each slot's `_name`), and `stack_type`/`stack` were added right after `salary_used` -- `frontend/src/csv.js`'s client-side exporter (used by the optimizer's own Lineups tab) was updated to match column-for-column. The Contest Generator's sample-entries table gained matching Stack/Teams columns. Verified live against the real slate: contest-generator entries showed real, varied stacks (`"4-2" "KC,NYM"`, `"2-2-2" "CHC,NYM,ATL"`, etc.) end to end through the actual upload/build/download flow, not just offline fixtures. Since then: the sample-entries table's old "Sim floor-ceiling" column (which was actually the 10th/90th percentile, not a true floor/ceiling) was replaced with separate Floor and Ceiling columns showing each lineup's actual lowest and highest simulated point total across every trial (`row.min()`/`row.max()` on the same simulated array the percentiles are already computed from, in both `evaluate_batch_simulated()` and `evaluate_field_mirrored()`), added to the CSV export too. Verified live: a real sampled lineup's floor briefly dipped negative (-3.75) across 10,000 trials -- expected, not a bug, since DK pitcher scoring includes real penalties (earned runs, hits/walks against) and a true min across that many trials will occasionally land on a genuinely bad simulated night, which the smoothed p10 alone would never surface. Since then: both randomized generators (`generate_field()` and `generate_entries()`) were limited to 9 named GPP stack shapes (5-3, 5-2-1, 5, 4-4, 4-3, 4-2-2, 4-2, 3-3-2, 3-3), weighted toward 5-3/5-2-1 (the shapes that most often win real large-field tournaments) without ruling the others out -- previously every lineup's hitters were sampled fully independently per slot, producing all sorts of shapes nobody would actually build (mega-stacks, 8-team spreads, etc.). `_pick_stack_teams()` assigns each shape's groups to real teams, largest first, weighted toward whichever teams carry the most aggregate ownership%/projected-points signal among their hitters. Two real bugs surfaced and got fixed during live-data verification: (1) the needed-team restriction was accidentally applying to the 2 pitcher slots too, even though a stack is a hitter-only concept -- fixed by skipping the restriction whenever `slot == "P"`; (2) every retry attempt was re-rolling a brand-new random shape instead of retrying the *same* target shape, so harder, genuinely salary-tight shapes (5-3 needs 5 relatively expensive hitters from one team) kept losing out to easier ones through survivorship bias in the retry loop -- fixed by picking the shape once per lineup, outside the retry loop, so each shape gets `max_attempts_per_lineup` real dedicated shots. A third issue, only visible on a thin candidate pool (a small offline test fixture with just 2 real teams), also needed fixing: a shape needing more team-groups than exist in the pool at all (e.g. a 3-team shape when only 2 teams have any hitters) is structurally impossible and would burn every retry attempt, causing generation to give up on the *whole remaining batch* early -- `_feasible_stack_shapes()` now pre-filters the shape list down to what a given candidate pool could possibly satisfy before ever picking one. Benchmarked live against the real slate: 5,000 lineups built in ~1s with 100% success, and the resulting stack_type distribution closely tracked the intended weighting (5-2/5-3/4-3 the top 3 shapes at 16-18% each, all 9 target shapes well represented, only modest coincidental drift into adjacent shapes from ordinary leftover free picks). Since then: fixed a genuine DraftKings rules violation the coincidental drift above could produce -- a shape's genuine leftover/free picks were never prevented from landing on an already-stacked team, so a "5" target could silently drift to a 6-, 7-, or even 8-stack, which DK's own Classic MLB rules (max 5 hitters from one team) would reject outright. `MAX_HITTERS_PER_TEAM = 5` is now enforced as a hard cap on every hitter slot's eligible pool in `_sample_one_lineup()`, independent of whether a stack shape is even targeted. Fixing this also surfaced a fixture gap: `mul_slate`'s offline test fixture only had 2 teams with any hitters, so the new cap forced both the points-weighted and ownership-weighted generators into the same 5-3 split, muting a pre-existing test's claim that points-weighted entries score higher on average -- fixed by giving the fixture a real 3rd team (`MUL3`), which also lets its 3-team stack shapes (4-2-2, 3-3-2) actually succeed in tests now. Verified live against the real slate: 5,000 entries and 5,000 field lineups built with a hard maximum of exactly 5 hitters from any one team, zero violations, and the stack_type distribution still tracked the intended weighting. Since then: the local SQLite cache file (`backend/data/dfsedge.db`) had grown to 223MB because nothing ever reclaimed the disk space left behind by expired rows -- `cache.purge_expired()` existed but was only reachable via a manual endpoint, and `VACUUM` never ran anywhere. Large short-TTL entries (`contest_batch:*`, up to 10,000 lineups each, 1-hour TTL) were the main contributor. Fixed with a new `cache.vacuum()` helper and a daily `cache._housekeeping_loop()` (same per-iteration try/except resilience convention as `lineup_watch._poll_loop()`) purging and reclaiming space once a day, wired into `main.py`'s `lifespan()` alongside a one-time startup purge. A one-time manual purge+VACUUM immediately shrank the file from 223,156,608 bytes to 852,448 bytes. This is standalone groundwork for a larger planned move of historical data to a cloud store (Supabase), so the local cache stays lean regardless.
- [x] In-house DK FPTS/ownership projections + Supabase historical data store -- shipped in phases (see `.claude/plans/` for the full roadmap). Phase 0 shipped: see the cache-cleanup entry above. Phase 1 shipped: `app/history_db.py`, a durable Postgres store via Supabase, parallel to `cache.py`'s local SQLite TTL cache but for data meant to survive forever. Three tables created by `scripts/migrate_history_db.py`: `slate_projections` (archives every real RotoWire upload permanently -- `cache.py`'s own `projections:{day}` key gets overwritten on the next upload and expires after a week, so without this there was no way to look back at a past slate's real numbers), plus `player_game_results`/`player_actual_results` (schemas created now, archiver not wired yet -- deferred until Phase 5's backtest calibration actually needs them). The upload endpoint (`POST /api/mlb/projections`) fires the archive off as a background task (`asyncio.create_task`) so a Supabase hiccup can never break an upload that already succeeded -- same resilience convention as `cache.cached()`'s stale-serve-on-error behavior. Entirely optional: every function in `history_db.py` no-ops if `SUPABASE_DB_URL` isn't set in `.env`. Caught a real bug during live verification: asyncpg needs a Python `date` object for a `DATE` column, not a plain `"YYYY-MM-DD"` string -- passing the raw string raised `AttributeError: 'str' object has no attribute 'toordinal'` deep in asyncpg's encoder; fixed by parsing with `date.fromisoformat()` before binding. Verified end to end against the real running app and the user's real Supabase project: uploaded a test CSV through the actual `POST /api/mlb/projections` endpoint, confirmed the archived rows landed in Supabase with correct values via a direct query, then deleted the test rows so they don't pollute the real archive. Phase 2 shipped: `services/inhouse_projections.py`'s `baseline_dk_points()` -- a real per-player DK-points-per-game rate built from `clients/mlb.get_player_game_log()` + `mlb_dk_points.py` (the exact scoring formulas), blending season-to-date average with last-15-games recent form and shrinking thin samples toward the same shared same-position pool `variance.py`'s Phase 2 already accumulates (promoted `variance.py`'s `player_kind()`/`own_games()`/`position_pool()`/`contribute_to_position_pool()` from private to shared helpers so both modules draw on the identical pool, warmed up by whichever one runs first for a given position/season). `project_fpts()` multiplies that baseline by `scoring.py`'s already-attached `edge.composite` -- the matchup-quality multiplier (platoon, Vegas total, opposing pitcher/bullpen, Savant contact quality, park, weather, recent form) reused as-is rather than re-derived. `mlb_slate.py` attaches the result as `projection.inhouse_fpts`, additive alongside whatever RotoWire projection is already there. Deliberately opt-in: a real per-player game-log fetch for every hitter/pitcher on the slate (a couple hundred players on a full day) would otherwise add real latency to every plain dashboard refresh, so `build_slate()` only computes it when `include_inhouse=True` (`GET /api/mlb/slate?inhouse=true`) -- confirmed live the default path stays untouched (0.19s, `projection: null`) while the opt-in path costs real but bounded time (7.8s cold across ~300 real players, 2.7s once each player's game log is cached) and produces plausible real numbers (hitters 2.7-9.1 DK pts, pitchers 2-24 DK pts, 401 players covered across today's full real slate). Caught a bug via a naive sed-based rename mid-implementation: a local variable in `variance.py`'s `player_outcome_pool()` ended up shadowing the newly-public `position_pool()` function it needed to call, causing an `UnboundLocalError` -- fixed by renaming the local to `shared_pool`. Phase 3 shipped: `inhouse_projections.py`'s `project_ownership()` -- a transparent heuristic, not a statistical fit (no historical ownership dataset exists yet to fit against), combining three signals into one propensity score per player -- value (`fpts/salary`), team total (Vegas implied runs vs league average), and a salary-tier bump for both ends of a position's own salary range (cheap punt plays and expensive studs both tend to get overowned relative to the middle) -- then softmax-normalizing *within* each DK roster-slot group (P/C/1B/2B/3B/SS/OF) so each group's total lands at exactly `slot_count x 100%` (e.g. OF sums to 300%, C to 100%), which also captures position scarcity for free without a separate term. `mlb_slate.py` attaches the result as `projection.inhouse_ownership_pct` wherever a DK salary is also loaded (ownership is meaningless without a real salary-capped contest to be owned in) -- pitchers without a matched salary row are correctly excluded rather than guessed at. Verified live against the real running app and the user's real Supabase-connected slate with real loaded salaries: confirmed every position group's ownership sums to exactly its slot count x 100% (1B/2B/3B/C/SS all ~100.0%, OF ~300.0%, rounding only). Sanity-checked against the same slate's real uploaded RotoWire ownership numbers (the plan's own stated verification bar, since no historical dataset exists to fit against yet): a weak-but-real positive Spearman rank correlation (0.191 across 171 real players) -- the model correctly flags real industry chalk (Mookie Betts, Kyle Tucker, Bobby Witt Jr., Alex Bregman, ...) as above-average owned, but at a much flatter magnitude than RotoWire's crowd-sourced 15-36% peaks for those same players, since a pure value/team-total/salary-tier heuristic has no way to capture the name-recognition/consensus signal real market ownership is heavily driven by. This gap is expected and explicitly named in the plan -- Phase 5 (backtest-calibrated weights, deferred until Phase 1's archive holds enough real slates) is where that gets tightened, not this v1. Phase 4 shipped: `HitterTable.jsx`/`PitcherTable.jsx` gained adjacent "In-house FPTS"/"In-house Own%" columns next to RotoWire's own (never replacing them), populated by a new opt-in "Load in-house projections" button in the header (`api.slate(date, {inhouse: true})`) -- kept as an explicit action rather than an automatic background fetch, since that would quietly turn Phase 2's whole "opt-in, don't slow down the default dashboard" design back into "always eventually fetched" at the same real backend cost. Separately, a new "Optimizer/generator source" dropdown (RotoWire/In-house) feeds a `projection_source` parameter threaded through the whole backend chain: `optimizer.build_player_pool()`/`generate_lineups()` and `contest.py`'s `generate_field()`/`generate_entries()` (the two shared choke points every higher-level contest function -- `build_contest_field`, `build_contest_entries`, `build_contest_entries_simulated`, `build_dk_entries_simulated` -- already funnels through) now read `projection.inhouse_fpts`/`inhouse_ownership_pct` instead of RotoWire's when asked, validated with a clear `OptimizerError` on an unrecognized source. Every router endpoint that builds lineups or a contest field (`/lineups`, `/contest-field`, `/contest-entries`, `/contest-entries-simulated`, `/dk-entries/simulate`) gained the same `projection_source` field and now calls `mlb_slate.build_slate(..., include_inhouse=(projection_source == "inhouse"))` itself, so choosing "In-house" in the dropdown doesn't require the tables to have fetched it first. Verified live in the browser: adjacent columns render correctly with real computed values (in-house FPTS populated for essentially every player regardless of salary match, in-house ownership% correctly blank for anyone without a matched DK salary, matching the "meaningless without a real salary-capped contest" design). End-to-end lineup generation against real data hit a pre-existing, unrelated blocker -- the salary file loaded earlier in this session has zero probable pitchers matched by name against today's real slate, so neither RotoWire nor in-house source can build a legal 2-pitcher lineup right now -- confirmed by direct inspection (0 usable pitchers either way) and by both sources failing with the *identical* error, proving no new failure mode. The actual "does the dropdown change what the optimizer optimizes against" claim is covered directly by three new offline tests (`test_pipeline.py`) using a synthetic slate where RotoWire and in-house numbers deliberately disagree about who's best at a slot -- `build_player_pool()` picks up the requested source's numbers exactly, confirmed both directions. Since then: the user found a description of a bottoms-up projection engine (explicit PA volume + wOBA/ISO/park/weather-adjusted rate, explicit pitcher K/innings/win-odds modeling) and asked whether it would help -- comparing against what's actually built, `scoring.py`'s `edge.composite` already fuses almost the same ingredients (platoon, park HR/runs factors, weather, opposing pitcher/bullpen, Statcast contact quality) into the existing multiplier, so the real, genuinely missing pieces were volume: neither a hitter's plate-appearance count nor a pitcher's win-bonus odds were modeled independently of his own season average. Added both as targeted corrections on top of the existing v1 baseline rather than a rewrite. **Hitters**: `_BATTING_ORDER_PA_FACTOR` (1.09 leadoff down to 0.91 in the 9-hole, a widely-cited real gap) scales a hitter's baseline rate by his actual confirmed batting-order slot -- `mlb_slate.py`'s `_team_hitters()` already attached `batting_order` once lineups post, previously computed and then thrown away; now `inhouse_fpts_batch()` reads it. **Pitchers**: DK's discrete +4 win bonus is a team-record event the baseline's own historical win rate can't see is stale for TODAY specifically -- `pitcher_win_rate()` computes a pitcher's own wins-per-start this season, and `win_ev_delta()` corrects the projection by `(today's market-implied win probability - his own season win rate) x 4`, using the moneyline The Odds API already fetches for every game (newly threaded onto each side as `game.home/away.moneyline`, alongside the existing `implied_runs`) via the already-existing `odds.american_to_probability()` helper. Both corrections default to a no-op (factor 1.0 / delta 0.0) whenever the underlying signal isn't available yet (lineup not posted, no odds loaded), reproducing the exact pre-change numbers -- confirmed by the full existing test suite passing unchanged. 15 new offline tests cover the batting-order factor (leadoff genuinely outprojects the same player at 9th), `pitcher_win_rate()`'s win/start math, `win_ev_delta()`'s sign in both directions and its None-handling, and the end-to-end `inhouse_fpts_batch()` wiring for both corrections. Verified live against today's real slate: Paul Skenes (season win rate 36%, but a +400-moneyline underdog tonight against a tough Detroit lineup, market-implied win probability only 20%) dropped from an uncorrected 18.08 to a corrected 17.44 -- the model now genuinely prices in that his own team is unlikely to back him up tonight, not just how well he's pitching. A leadoff hitter and the same lineup's 9-hole hitter with comparable matchup composites showed real, directionally-correct separation (e.g. 5.52 vs 3.56) purely from where they bat. Since then: fixed a real bug behind the exact "ownership stays flat across players" complaint the user raised -- they'd uploaded 13 real DraftKings contest-standings exports (post-contest CSVs, a new format this app had never ingested: real final rank/points/lineup per entry plus each slate's actual `%Drafted` ownership and actual FPTS). Cross-referencing the ones with real dates against salary/projections data still sitting in the local cache from that week let 4 of them actually be backtested end to end: real per-slate player pools rebuilt from cached data, run through the live `project_ownership()`, and compared against real `%Drafted`. The result: essentially zero rank correlation with real ownership (0.023 average Spearman across the 4 slates, down from an already-weak 0.191), and a real 365-player pool topped out at 4.2% modelled ownership for anyone, against real slates routinely showing 20-33%+ on genuine chalk. Root cause: `value` (`fpts/salary`) is a tiny raw number (~0.001-0.003) added directly into the same sum as `team_total` (~0.7-1.5) and `salary_tier` (0-1) -- despite being weighted highest and documented as "the dominant real-world driver," its magnitude was 100-1000x smaller than the other two signals, so it contributed essentially nothing to the actual ranking; ownership was really being driven almost entirely by salary_tier (distance from the position group's own midpoint), which is exactly what near-zero correlation with real ownership looks like. Fixed by min-max normalising `value` onto the same 0-1 scale `salary_tier` already uses, within each position group, before weighting. Re-running the identical 4-slate backtest after the fix: average Spearman jumped to 0.236 -- better than the original 0.191 baseline. `_SOFTMAX_TEMPERATURE` (which only ever affected concentration, never ranking, so it couldn't have fixed the correlation on its own) was separately retuned from 2.5 to 0.3 by sweeping it against the same real 365-player pool: 2.5 topped out at 4.2% for anyone, 0.3 reaches ~28% for the top play (close to the real 20-33% range) without collapsing to one dominant player the way anything below ~0.2 started to (52%+ ownership on a single player, which real large-field ownership essentially never does). New regression test locks in the fix directly: a large fpts gap at identical salary must now produce over 20 points of real separation, not the near-50/50 split the bug produced. Verified live against today's real cached slate (2026-08-19, 174 players): ownership now spans 0.42% to 51.68%, a genuinely wide, realistic distribution instead of the old ~1-4% band.
- [x] MLB: games filter on the Stacks tab -- `StackTable.jsx` gained its own "Games" checklist (same auto-detect-from-uploaded-DK-salary-CSV pattern already used by the Lineups/Contest Generator tabs), so a specific slate can be focused on without the noise of every game MLB's schedule returns. Unlike those other tabs, an in_slate-derived default that would leave *zero* games selected (e.g. a salary file loaded for a different date than today's real slate) falls back to selecting everything instead of silently showing an empty tab -- this tab is purely informational, so there's no correctness reason to ever default to nothing. Caught exactly that scenario live: the salary CSV loaded earlier in the session doesn't match today's real slate, so without the fallback the tab defaulted to "0 of 15" and rendered nothing. Since then: the identical Games checklist (including the same empty-default fallback) was added to `HitterTable.jsx` and `PitcherTable.jsx` too, so all three informational tabs (Stacks/Hitters/Pitchers) can be focused on the same slate independently -- `PitcherTable.jsx` had no React state at all before this (a plain function component looping straight over `slate.games`), so this also converted it to use `useState`/`useMemo`/`useEffect` for the first time. `HitterTable.jsx` already had a two-stage rows-then-filtered pipeline, so the games predicate slotted into the existing `filtered` `useMemo` alongside the search/min-score filters rather than the row-loop-`continue` approach the other two tabs use. Verified live in the browser against today's real slate: both tabs render "Games (15 of 15)" by default, and unticking the NYY @ BAL game correctly dropped both Carlos Rodón and Shane Baz (that game's two probable pitchers) from the Pitchers tab, with the matching hitters disappearing from the Hitters tab too.
- [x] Optimizer/sim refinements Phase 1: opposing-pitcher exclusion + configurable salary floor/ceiling -- shipped across both lineup-building engines (`optimizer.py`'s exact MILP solver and `contest.py`'s fast randomized sampler, which share `optimizer.build_player_pool()`). **Opposing-pitcher exclusion**: a lineup can no longer roster a hitter alongside the pitcher he's actually facing (e.g. an ATL pitcher can't share a lineup with HOU batters when ATL is playing HOU) -- a real strikeout or home run is the same at-bat scored two opposite ways, so pairing them was always a strict handicap, never a real strategy. `build_player_pool()` now attaches each player's `opponent` team abbrev; `optimizer.py` enforces it as a pairwise `x_pitcher + x_hitter <= 1` constraint for every (pitcher, opposing hitter) pair in `_opposing_pitcher_constraints()`; `contest.py`'s `_sample_one_lineup()` bans the picked pitchers' opponent teams from every subsequent hitter slot (pitchers are always filled first in slot order, so both bans are known before any hitter is chosen). **Salary floor/ceiling**: `optimizer.generate_lineups()` gained a `max_salary` parameter alongside the existing `min_salary`; `contest.py`'s `generate_field()`/`generate_entries()` (and every function built on them) gained both, checked once a full lineup is sampled rather than budgeted slot-by-slot. The $47,000 default floor lives only at the HTTP API layer (`routers/mlb.py`'s `Body(optimizer.DEFAULT_MIN_SALARY, ...)`) -- the library functions themselves stay opt-in (`None`/`0` = unconstrained), so direct callers and tests are unaffected by the new default; the frontend's own React state defaults to `'47000'` too, so the field shows the real default value rather than relying on an unsent key. Several existing offline test fixtures needed small additions once the opposing-pitcher rule went in: a single 2-team game fixture (`opt_slate`, `value_slate`) could no longer legally fill both pitcher slots at all (both real pitchers opposed the only hitter source), so each gained a 3rd, hitter-less "filler" game supplying a safe 2nd pitcher option -- the same real-world shape any actual multi-game DK slate has. `mul_slate`'s own unconstrained baseline changed too: its two highest-fpts pitchers (MUL1's and MUL2's) oppose each other's hitters, so the natural optimum now spans more than the 2 teams it used to. New regression tests confirm the rule holds across hundreds of generated lineups (`optimizer.generate_lineups`, `contest.generate_field`, `contest.generate_entries`) via a `has_opposing_pitcher_hitter_pair()` helper checking every sampled lineup by known pitcher/opponent team pairs. Verified live against today's real 15-game slate: a generated lineup used $49,300 of salary (above the new $47k floor) with two pitchers (Kyle Harrison/MIL, Kevin Gausman/CHC) whose actual opponents (SEA, CWS) never appeared among the rostered hitters; the Contest Generator's 500-entry build also completed successfully with the same defaults applied.
- [x] Optimizer/sim refinements Phase 2: allow duplicate lineups (tracked and reported, with payout correctly split across identical entries sharing a rank) and a default preference for high-salary/high-FPTS players in a partial stack's leftover one-off slots. **Duplicate lineups**: `optimizer.generate_lineups()`'s existing `min_unique_players` (which controlled the no-good-cut distinctness requirement) now accepts 0, meaning "allow exact duplicates" -- the no-good-cut constraint `sum(prior lineup's players) <= ROSTER_SIZE - min_unique_players` was already a no-op at 0, so this only needed relaxing the validation bound. `contest.py`'s `generate_entries()` gained a matching `allow_duplicates` flag that skips its `seen_signatures` distinctness check. Every returned lineup/entry (both engines, plus `generate_field()`, which already allowed duplicates by design) now carries `duplicate_count` -- computed via a `Counter` over each lineup's player-id signature -- surfaced in the Lineups tab as a "×N duplicate" badge and in the Contest Generator's sample-entries table as a "Dup" column, and threaded through both CSV exporters (`lineup_export.py` and the frontend's `csv.js`). **Payout splitting**: real duplicate DK entries score identically in the real world and genuinely tie for whichever consecutive block of ranks they land in, with DK's own tie-breaking rule splitting the combined payout evenly across the tied entries -- discovered that the existing rank-assignment logic (in `_evaluate_batch_against_field`, `evaluate_batch_simulated`, and `evaluate_field_mirrored`) already places duplicates at consecutive ranks (identical scores always sort adjacent), so a duplicate group's already-individually-computed payout/profit/cash-probability/ROI values just needed averaging across the group to reproduce DK's real split exactly -- no changes to the rank-assignment math itself, just a `_split_duplicate_payouts()` post-processing step. **One-off slot quality preference**: for a partial stack shape (leftover hitter slots outside the forced groups), at least `min(2, leftover)` of those slots now default to a "payup or high-FPTS" player (salary or projected FPTS within 80% of the best available at that slot type) unless the caller already gave an explicit one-off restriction. `optimizer.py`'s MILP version (`_one_off_quality_constraint()`) needed a genuine correctness fix mid-implementation: a naive "qualifying one-offs >= required" floor would make an ordinary 2-team manual stack with plenty of depth on both teams (e.g. a 5-3 between two juicy offenses) infeasible whenever it's cheapest to pad both stacks past their minimums rather than reach a 3rd team, since every leftover slot would then be exempt stack padding with zero genuine one-off picks to ever satisfy the floor -- fixed by relaxing the floor by exactly one for every leftover slot that turns out to be padding rather than a genuine one-off pick (`qualifying_one_offs >= required - (leftover - total_one_offs)`), so a fully-padded stack trivially satisfies it while a partial one still gets real pressure. `contest.py`'s sampler version restricts the first `min(2, leftover)` genuine one-off picks (slots where no stack group still needs filling) to the qualifying set, falling back to the unrestricted pool if that set is ever empty for a slot -- a soft preference, not a hard constraint, since the sampler has no way to know in advance whether a restriction is satisfiable. New tests prove the rule does real work, not just something fpts-maximization would have done anyway: a dedicated optimizer.py fixture where the true unconstrained optimum funds a big catcher upgrade by using a cheap, low-FPTS "junk" outfielder (91 fpts) shows the default forces the premium alternative instead once active (86 fpts, a real cost); a dedicated contest.py fixture proves the same thing against ownership-weighted sampling specifically (junk has high ownership, premium has low -- the restriction is shown to be ownership-blind, not just an artifact of FPTS-weighted sampling already avoiding weak plays). All 343 offline tests pass (up from 317). Verified live: 3 requested lineups with `min_unique_players=0` on today's real slate came back as 3 identical copies of the same $49,300 optimal lineup, each correctly reporting `duplicate_count: 3` and rendering the "×3 duplicate" badge; the Contest Generator's "Allow duplicates" checkbox and "Dup" column render correctly, and a 500-entry build with it unchecked completed normally with every entry showing "—" (no duplicates, as expected against today's real, sufficiently deep player pool).
- [x] Simulator: matchup-conditioned means + pitcher/opponent anti-correlation (`variance.py`) -- two of the biggest gaps identified when auditing the Monte Carlo engine against a proper MLB DFS variance-modeling framework the user shared: player outcomes were sampled purely from raw historical games, blind to today's actual matchup, and pitchers were sampled fully independently of the team they're facing. Both fixes reuse machinery that already existed rather than adding a new signal pipeline. **Matchup-conditioned means**: `optimizer.build_player_pool()` now attaches each player's `edge_composite` (scoring.py's already-computed, already-tuned platoon/park/weather/bullpen matchup multiplier, 1.0 = neutral) to every pool entry, threaded through into the final lineup/entry player dicts on both engines (`_solve_one()` and `_sample_one_lineup()`) so it survives into whatever gets simulated later. `variance.py`'s percentile-shift mechanism (previously team-correlation-only) gained a second additive pull: `target_pct = 0.5 + (team_multiplier - 1)*TEAM_SENSITIVITY + (own_edge - 1)*EDGE_SENSITIVITY`, biasing which percentile of a player's own real history gets sampled toward his own matchup quality today, not just his team's shared day. **Pitcher/opponent anti-correlation**: pitchers previously never reacted to anything -- now they get pulled the OPPOSITE direction by the team they're facing having a big or small day, reusing that team's already-computed per-trial multiplier (no new signal needed, just a lookup via `opponent`, already attached to every pool entry from the earlier opposing-pitcher-exclusion work): `target_pct = 0.5 + (own_edge - 1)*EDGE_SENSITIVITY - (opponent_multiplier - 1)*OPPONENT_SENSITIVITY`. Two new tunable constants (`EDGE_SENSITIVITY = 1.0`, `OPPONENT_SENSITIVITY = 0.6`) join the existing `TEAM_MULTIPLIER_STD`, picked as a starting point and checked against real data rather than derived analytically -- same philosophy as everywhere else in this module. `sample_correlated_outcome()`'s signature changed from a single positional `multiplier` to keyword-only `team_multiplier` / `own_edge` / `opponent_multiplier`, shared by both the scalar reference function and the vectorized `simulate_batch()` path. New offline tests: a player's own `edge_composite` shifts their simulated mean even with an identical outcome pool and no team at all; a pitcher's simulated outcome correlates negatively with his opponent's simulated total in the same batch (a stack of that opponent's real hitters standing in as an already-validated proxy for "how good was their day," rather than reaching into `simulate_batch()`'s internals). 345 tests pass (up from 343). Verified against real 2026 slate data: today's real `edge_composite` spread across 295 players had a stdev of only 0.084 (min 0.782, max 1.191) -- confirming `EDGE_SENSITIVITY=1.0` produces a real but not overwrought shift, not the much larger swing the offline unit test deliberately used (0.7/1.3) to make the effect provable. Simulating one real player (Pete Crow-Armstrong, 200 real games, own historical mean 8.35) at today's actual slate-wide best and worst observed matchup composites (1.191 and 0.819) produced simulated means of 13.91 and 2.23 respectively -- a large, directionally-correct, real-data-driven spread. The pitcher/opponent correlation test happened to land at -0.431, inside the real-world -0.30 to -0.45 range the auditing framework itself suggested, without having been tuned to hit it deliberately.
- [x] AI analysis: full-day persistence + a real same-team pitcher/hitter confusion bug fixed -- the user noticed Claude claiming hitters like Shohei Ohtani and Max Muncy were "facing" Blake Snell despite all three playing for LAD. Root cause, confirmed by inspecting `analysis.py`'s `_compact_slate()`: each game's JSON grouped a team's own pitcher and own hitters together as siblings under one `"home"`/`"away"` object purely because they share a team, which a model reading that structure could easily (and, per the report, did) misread as "this pitcher faces these hitters." Fixed by making the actual matchup explicit instead of relying on the model to correctly cross-reference two separate team objects: each side now carries an `opposing_pitcher_these_hitters_actually_face` field naming the OTHER side's starter, plus a new system-prompt ground rule stating outright that a team's hitters never face their own team's pitcher. New offline regression test (`_compact_slate`) confirms each side's field always names the other side's real starter, never its own, and that the two starters in the fixture are genuinely different players. **Persistence**: `analyse_slate()`'s cache TTL went from 30 minutes to a full day (`_ANALYSIS_TTL = 86400`) -- the cache key is already scoped to the date, so a new day's slate always gets a fresh read regardless. `AnalysisPanel.jsx` now auto-loads on mount/date-change (`useEffect`) instead of requiring an explicit "Analyse this slate" click every single time the tab is revisited or the app is reopened -- free (no new Claude call) whenever a same-day write-up already exists, matching the user's actual complaint ("everytime I leave the AI analysis page the information goes away"). Caught and fixed a real risk while implementing this: React 18's dev-mode StrictMode intentionally double-invokes effects, which would have fired two real (paid) Claude calls back-to-back the first time a date with no cached analysis was opened -- guarded with a `useRef` tracking which date already has a fetch in flight/completed for this mount. Verified live: reloading the whole page and opening the AI analysis tab rendered the exact same write-up (identical token counts, `59,288 in / 3,255 out`) in about 2 seconds instead of the normal 15-40s generation, and the regenerated analysis's own text now correctly self-corrects mid-sentence on a real Phillies/Marlins matchup ("he's pitching, not facing hitters here... actually wait, Nola is the Phillies' own arm, so the play is: Miami's bats face Nola") -- direct evidence the structural fix changed the model's actual reasoning, not just the surrounding prose. 348 tests pass (up from 345).
- [x] Live DraftKings slate import (`clients/draftkings.py`) -- no more manual salary CSV upload. The user proposed pulling real slate/player/salary data directly from DraftKings' own public lobby API; confirmed live against the real endpoints before building anything (`https://www.draftkings.com/lobby/getcontests?sport=MLB` and `https://api.draftkings.com/draftgroups/v1/draftgroups/{id}/draftables`, same unsupported-but-public category as clients/mlb.py's existing MLB Stats API dependency) -- free, no API key, and richer than a CSV export (includes each player's real opponent/game directly, not just a text column to reparse). `get_slates(day)` returns every real Classic MLB slate live for a date (Early/Main/Night/single-game pools, filtered to `GameTypeId` 2 and 114 -- Snake/Tiers/Home-Run-Showdown use roster rules this app doesn't support) with real game counts and start times pulled from the matching `GameSets[].Competitions[]`, matching exactly what was asked for ("early slate has 4 games... main slate is games starting at 6"). `get_draftables(draft_group_id)` returns players/salaries/positions/matchups for one specific slate in the *exact* row shape `salaries.parse_dk_csv()` already produces, so `salaries.store()` accepts it directly with zero changes anywhere downstream (`in_slate` detection, the optimizer, the contest generator all kept working unmodified). Both calls go through the existing disk cache (`get_slates` 15 min, `get_draftables` 10 min) with a `force`/`refresh` flag for an explicit re-pull -- satisfying "check if we have it, only call the API if we don't." New `GET /dk-slates` and `POST /dk-slates/load` endpoints, and a new `DkSlatePicker.jsx` component (a "Browse DK slates" dropdown next to the existing manual-upload buttons, which stay as a fallback) showing every live slate with its real games/times, one click to load a slate's players+salaries, plus a separate "Refresh" button that re-pulls the *currently loaded* slate live -- the second explicit ask, for late scratches/swaps close to lock. 8 new offline tests (`_parse_slates`/`_parse_draftables`) against fixture payloads shaped exactly like the real confirmed responses: Classic-only filtering (a Snake draft group sharing the same games is correctly excluded), day filtering, label/game extraction, isDisabled/no-salary player exclusion, and byte-for-byte row-shape compatibility with the CSV parser. 356 tests pass (up from 348). Verified live end to end against today's real slate: browsing showed all 7 real live Classic slates (Early/4 games, four single-game pools, Main/9 games, Night/3 games) with correct real matchups and times; picking "Main" loaded 891 real players with live salaries (Andy Pages $5,700, Elly De La Cruz $6,000, Bobby Witt Jr. $6,100, ...) into the Hitters tab with zero manual upload, and the Games filter correctly flipped to "9 of 15" in_slate; the Refresh button's force-refetch confirmed working via a live network check. Since then: now that salaries load live from DraftKings, the manual "Upload salaries" button/file input was removed from the header entirely (`services/salaries.py`'s CSV parser and the `POST /api/mlb/salaries` endpoint are untouched, just no longer wired to a UI button) -- "Upload projections" (RotoWire) stays, since that's still the only source for FPTS/ownership reference data. With two "Refresh" buttons now doing genuinely different things (one reloads matchup data -- scores/park/weather/lines/lineups -- the other re-pulls just the loaded DK slate's players/salaries), both got renamed and given cross-referencing tooltips to say so explicitly: the header button is now "Refresh matchups", `DkSlatePicker.jsx`'s own is "Refresh DK salaries". Since then: added real name matching between a RotoWire projections upload and the live DK slate, since the two sources don't always spell a player the same way. `services/player_match.py` (already shared by both salaries.py and projections.py for exact match-after-normalisation against MLB's own canonical names) gained a `NICKNAMES` table folding common short/legal-name pairs to the same canonical form (Nick/Nicholas, Mike/Michael, Josh/Joshua, ~40 pairs total) applied inside `normalize_name()` itself, and an opt-in `fuzzy=True` fallback on `match()` for genuine typos/spelling drift -- deliberately scoped to a same-team same-difflib-cutoff (0.85) check so it can never cross-match two different players sharing a last name across the league. Both `mlb_slate.py`'s `_projection_info()`/`_salary_info()` (the live per-player join powering every tab) now pass `fuzzy=True`. `POST /api/mlb/projections` also runs this matching immediately at upload time against whatever DK slate is already loaded, and reports it back in the response (`matched_to_slate`, and an `unmatched` list of any names that still didn't line up) instead of leaving a mismatch to show up silently as a blank projection later -- surfaced in the upload status message in the UI. 11 new offline tests cover nickname folding (both the false-positive-safety case and the real fold), the fuzzy same-team-only fallback (a typo matches with `fuzzy=True`, doesn't without it, and never crosses team boundaries even with it on), and the upload-time match report end to end. Verified live against the real running app and today's real loaded DK slate (ARI @ BOS): an uploaded test file with an exact name, a one-letter typo ("Jarren Doran" for the slate's real "Jarren Duran"), and a genuinely fake name came back `{"matched_to_slate": 2, "unmatched": ["Totally Fake Player"]}` -- both the exact and the fuzzy-corrected typo matched, only the real non-match got flagged.
- [x] Fix: two-way players (Ohtani) missing from the Hitters tab entirely -- the user asked why Ohtani never showed up. Root cause: `mlb_slate.py`'s `PITCHER_POSITIONS = {"P", "SP", "RP", "TWP"}` set is used to skip pitchers when building a team's hitters list, but "TWP" (MLB Stats API's year-round bio position for a two-way player) is Ohtani's *permanent* position label regardless of whether he's pitching that specific day -- so `_team_hitters()` silently excluded him from the Hitters tab on every date, including his DH-only days when he's a fully rosterable DK hitter. Fixed by threading each team's own probable-pitcher id (`home_pp`/`away_pp`, already fetched for the pitcher cards) into `_team_hitters()` as `own_pitcher_id`, and only applying the pitcher-position skip to a "TWP" player when he himself is that specific game's starter (`position != "TWP" or pid == own_pitcher_id`) -- a plain "P"/"SP"/"RP" pitcher is still always excluded, unchanged. Downstream consumers needed no changes: `variance.py`'s `player_kind()` and `inhouse_projections.py`'s pitcher/hitter branch both already key off `position == "P"` specifically (not the broader pitcher set), so a "TWP" hitter entry was already correctly treated as a hitter everywhere except this one exclusion check; the optimizer/contest generator read a matched DK salary row's own position string (e.g. "OF/1B", never "TWP") for roster eligibility, not the raw MLB bio position, so lineup-building was never affected by this bug in the first place -- only the informational Hitters tab was. 2 new offline tests build a minimal two-way-player fixture and call `_team_hitters()` directly with `own_pitcher_id` set both ways, confirming he appears as a hitter on a non-start day and is excluded on his own start day. Verified live against today's real slate: Shohei Ohtani now appears in the real Hitters tab response (`position: "TWP"`), where he was completely absent before this fix.
- [x] Bullpen recent-workload signal, separate from season-long ERA -- the user asked whether "shaky pen" already accounts for a genuinely bad bullpen; it turned out to already be exactly that (`bullpen_component`/the Stacks tab's "shaky pen" badge are both pure season-long ERA from `get_bullpen_stats(season)`), and what was actually missing was the thing they described as *causing* "shaky" in their own head: a bullpen worn down from heavy RECENT usage (an extra-inning marathon, a bullpen game, a short start pulled early) independent of how the pen has pitched all year -- a real, well-known DFS heuristic this app had no signal for at all. New `clients/mlb.get_recent_bullpen_workload(day)` reuses `get_bullpen_stats()`'s exact fetch-and-filter shape (same `/stats` leaders endpoint, same "everyone with zero starts is a reliever, sum outs by team" aggregation) but with `stats=byDateRange` and an explicit 2-day trailing window instead of `stats=season` -- confirmed live that MLB Stats API genuinely supports `byDateRange` with `startDate`/`endDate` before building anything. New `scoring.bullpen_workload_component()` scores a team's recent bullpen outs against the league-average recent workload, capped at ±15% (matching the existing `bullpen_component`'s modest-swing convention) -- added to `WEIGHTS` at 0.04, funded by trimming `platoon` (0.19→0.17) and `bullpen` (0.07→0.05) so the total still sums to exactly 1.0. Threaded through `mlb_slate.py` the identical way `bullpen`/`bullpen_era` already are: fetched once per slate build, a league baseline computed, attached per-hitter as `edge.components.bullpen_workload`. `StackTable.jsx` gained a second sub-line under the existing bullpen-ERA/"shaky pen" row -- "X.X pen IP last 2 days", with its own independent "taxed pen" badge (>=30 recent outs, calibrated against today's real 18-team spread of 15-44 outs, league average 23.7) so a team can show both, either, or neither badge depending on whether its bullpen is bad, tired, or both. 6 new offline tests use a fixture where the two signals are deliberately given OPPOSITE reads for the same two teams (one bullpen bad all year but rested the last 2 days, the other fine all year but hammered recently), proving neither signal can substitute for the other. Verified live against today's real slate: real per-team recent-outs values matched the `bullpen_workload_outs` baseline and rendered correctly in the Stacks tab, with a team (ATL, 14.7 recent bullpen innings) correctly showing "taxed pen" alongside a perfectly normal 4.17 season ERA (no "shaky pen"), and another team (Great American Ballpark's STL/CIN matchup) showing "shaky pen" from a bad 4.66 season ERA with a completely normal recent workload -- exactly the independence the fix was built to prove.
- [x] Fix: header summary tiles (Games on the slate, Average game total, Highest implied team, Best stack, Lineups confirmed) didn't follow the currently-loaded DK slate -- `StatTile.jsx`'s `SlateTiles` computed across every game MLB's schedule returned for the date, not just whichever DK slate (Early/Main/Night/...) was actually picked via `DkSlatePicker`, so picking a 3-game Night slate still showed stats averaged across all 9 of the day's games. Fixed the same way the Stacks/Hitters/Pitchers tabs' own Games checklists already scope themselves: filter to `in_slate !== false` before computing anything, falling back to every game only when nothing's detected as in-slate yet. Verified live: picking "Night" (3 games) correctly dropped "Games on the slate" from 9 to 3 and updated "Highest implied team"/"Best stack" to the right team (HOU) and figures. Caught and fixed a related, adjacent staleness bug while verifying live: those same three tabs' own Games checklists have a `useEffect` keyed only on the day's list of game_pks, which doesn't change when switching between DK slates on the same date (only which games are `in_slate` changes) -- so the checklist's own default selection silently stayed stuck on the *previous* slate's games after switching. Fixed by keying the effect on each game's `pk:inSlate` pair instead of just `pk`, so a slate switch (a real `in_slate` flip with no game_pk change) now correctly re-triggers the default-selection recompute too.
- [x] Contest simulator trust audit + ownership fallback for field realism (Phase 1 of a multi-part review) -- the user reported implausible simulated results (+101.7% avg ROI, 24% cash rate on a 20-entry sample) and asked whether the field is built realistically or from something closer to the app's own chalk. Investigated every specific claim against the real code rather than guessing: two turned out to already be true and NOT the cause -- `contest.py`'s `generate_field()` already weights every pick by real ownership% (`_ownership_weight`, distinct from `_fpts_weight` used for the user's own entries) and already limits stack construction to 9 named GPP shapes weighted toward 5-3/5-2-1 with DK's 5-hitter-per-team cap enforced; `variance.py`'s `sample_correlated_outcome()` already pulls a pitcher's simulated outcome the opposite direction from the opposing team's per-trial multiplier, and a pitcher can never be rostered alongside the hitters he's facing at all (hard constraint), so "pitcher-vs-stack negative correlation" was already covered. The real, confirmed bug: `optimizer.build_player_pool()` set `ownership_pct = proj_info.get(ownership_key) or 0` -- any player RotoWire's export doesn't cover (real and common; RotoWire doesn't project every rostered player on a slate) silently floors to `contest.py`'s `_OWNERSHIP_FLOOR` (0.5%), making the simulated opponent field's construction for those specific players closer to random than realistic, which would inflate a skill-based entry's simulated edge. Fixed by falling back to the OTHER projection source's ownership when the requested one is missing it (RotoWire missing → try in-house, and vice versa) -- FPTS deliberately has no such fallback, since a missing FPTS still excludes a player from the pool entirely (no other signal to optimize against). The four field/simulation router endpoints (`/contest-field`, `/contest-entries`, `/contest-entries-simulated`, `/dk-entries/simulate`) now always fetch in-house data regardless of the chosen `projection_source`, purely so a real fallback signal exists to fall back to -- `/lineups` (the pure optimizer, no synthetic field) deliberately left unchanged. 3 new offline tests cover the fallback both directions and the "floors to 0 when neither source has it" edge case. Verified against real cached slate data (2026-08-19): on this specific date RotoWire's coverage was actually complete for the subset of players that survive `_team_hitters()`'s own active/PA filter, so the fallback didn't change anything for today's numbers specifically -- the bug is real and now fixed regardless, for whichever future slate has a genuine RotoWire gap. Confirmed via direct code reading (not assumed) that stack correlation is same-team-only: `team_environment_multiplier()` draws each team's per-trial environment fully independently, with no shared component linking both teams in the same game -- real bring-back correlation (a shootout lifting both offenses together) is the next phase, prioritized by the user over a leverage column, late swap, and a results/bankroll tracker (all confirmed genuinely absent from the codebase, no false starts anywhere).
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
