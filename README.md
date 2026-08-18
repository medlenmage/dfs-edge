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
