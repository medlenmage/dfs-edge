# Process layer — audits and scheduled briefs

This is the handoff for the process layer added on 2026-08-30. It was
built and tested against the public repo in a sandbox that could not
reach the running app, so **the offline suite is green but nothing here
has been verified live yet.** The "Verify live" section at the bottom
is the checklist for Claude Code to run in the real environment.

## Why

A review of 435 real entries across 19 DK contests (2026-08-12 → 08-30,
handle `medlen1215`) found three leaks, in order of cost:

1. **Leverage at pitcher.** Sub-10%-owned arms averaged 12.6 DK pts;
   25%+ arms averaged 23.5. Top-1% lineups had a sub-10-pt pitcher 5%
   of the time; ours did 27% (same as the field).
2. **Right team, wrong hitters.** Stacks were the right team built
   from the 6–9 spots around one star.
3. **Sub-3%-owned filler.** 28% of hitter slots; averaged 5.2 pts, 34%
   scored zero.

Plus no conviction: 20-entry portfolios spread over 12–15 stacks and
11–14 pitchers. The full three-sport guide set is in this folder.

## What was added

### Backend

| File | What it does |
|---|---|
| `app/data/process_rules.py` | Every threshold the audits and briefs score against, plus `RULES_TEXT` (the same rules in prose for the Claude prompts). One place, so nothing can disagree. |
| `app/services/contest_audit.py` | **Post-contest** audit of the user's own entries in a DK standings export: cash rate vs. an approximate line, pitcher/hitter production by ownership bucket, filler share, pitcher core size, stack conviction and "right team, wrong bats" — always next to the same numbers for the field and the top 1%. `audit_to_markdown()` renders it. Needs a `team_by_name` map for the stack section (built from `mlb_slate.build_slate()` for the contest's date, the same way `scripts/archive_contest_stacks.py` does); skips stacks rather than guessing without one. |
| `app/services/build_audit.py` | **Pre-entry** audit of a generated batch (the flat `players` shape every generator emits) against a built slate for batting order / confirmation status. Batch-level report + per-entry `keep`/`cut` verdict + flags + markdown. `target_count` trims a big build to what will actually be entered. |
| `app/services/briefs.py` | The two scheduled reads. `run_morning()` / `run_prelock()` assemble the data (slate compacted via `analysis._compact_slate` + ranked pitchers + open→live implied runs + scratches + yesterday's contest audits + the build audit), call `analysis.complete()`, and store under `brief:{day}:{kind}` (14-day TTL) with a rolling index. `tick()` is the scheduler pass (testable with a fake clock); `_schedule_loop()` runs it every 60s from `main.py`'s lifespan. `remember_latest_batch()` records what the user is about to play. |
| `app/services/analysis.py` | New `complete(prompt, system_prompt, max_tokens)` — the provider dance (Claude Code CLI on subscription first, API key fallback) with a caller-supplied prompt. Nothing existing changed. |
| `app/config.py` | `BRIEFS_ENABLED`, `BRIEF_TIMEZONE`, `BRIEF_MORNING_LOCAL_TIME`, `BRIEF_PRELOCK_LEAD_MIN`, `DK_HANDLE`. |
| `app/main.py` | Starts/cancels `briefs._schedule_loop()` alongside the lineup watcher and cache housekeeping. |
| `app/routers/mlb.py` | `POST /contest-results` now also runs the process audit and returns it as `audit` (and caches it under `contest_audits:{day}` for the next morning's brief). New: `POST /contest-audit` (audit without archiving), `GET /contest-audits`, `POST /build-audit`, `GET /briefs`, `GET /briefs/{kind}`, `POST /briefs/{kind}/run`. Every batch build/simulate/late-swap/reshape site now calls `briefs.remember_latest_batch()`. |
| `tests/test_process.py` | 48 offline checks: rules, both audits on fixtures, the batch pointer, the scheduler with a fake clock (fires once, not twice, not after lock, late-but-not-too-late after a restart), storage, Windows-safe time formatting. |

### Frontend

| File | What it does |
|---|---|
| `src/markdown.js` | The tiny renderer that lived inside `AnalysisPanel.jsx`, extracted and taught pipe tables (the audits emit one). |
| `src/components/BriefsPanel.jsx` | New **Daily briefs** view under Review: schedule status (next fire times, whether each fired, latest build recorded), the morning brief, the on-demand build audit, the pre-lock brief (with its audit flags), recent briefs. |
| `src/components/ResultsPanel.jsx` | Renders the process audit under a successful upload. |
| `src/api.js`, `src/App.jsx` | Wiring. |

### Scheduling

The timer is **inside the backend**, not Windows Task Scheduler. The
app is left running 24/7 on the owner's machine (stated), and the
lineup watcher already established the pattern. Firing is idempotent
per day (`brief_fired:{day}:{kind}`), a restart doesn't refire, and a
missed window fires late rather than never (morning within 8 hours;
pre-lock only while the slate is still open).

The pre-lock time comes from `clients/draftkings.get_slates(day)`: the
slate labelled **Main**, else the biggest Classic slate. The pre-lock
brief audits whatever `remember_latest_batch()` recorded last for the
day — a **reshape** (keep-top-N) is what the user actually plays, so it
overwrites the full build.

## Verify live (Claude Code)

Run from the repo root with the backend and frontend already up.

1. `cd backend && .venv/bin/python -m tests.test_process` → 48/0.
   `.venv/bin/python -m tests.test_pipeline` → 841/0 (unchanged).
2. Add to `.env`: `DK_HANDLE=medlen1215`. Restart the backend. The
   startup log should show `Briefs scheduler on: morning at 11:00
   America/Chicago, pre-lock 60 min before the DK Main slate`.
3. `GET /api/mlb/briefs` — `schedule.enabled` true, `morning.scheduled_local`
   today at 11:00, `prelock.lock_local` matches DK's Main slate.
4. Upload one of the real standings zips through the Results tab
   (e.g. the 8/21 $1.5K Quarter Jukebox with date 2026-08-21). The
   response's `audit.summary.best_rank` should be 4 and the markdown
   should show 15 distinct pitchers flagged. Confirm `audit.stacks` is
   populated (team map resolved) — if it's `null`, the MLB Stats API
   slate build for that date failed; check the log.
5. Build a contest in the Build view, then `POST /api/mlb/build-audit?date=<today>&target_count=20`.
   Confirm verdicts and that `hitters.entries_with_unconfirmed` drops
   after lineups post.
6. `POST /api/mlb/briefs/morning/run?date=<today>` — expect a written
   brief with the six sections; `provider` should be `claude-code`.
   Then `POST /api/mlb/briefs/prelock/run?date=<today>&target_count=20`
   — the response's `audit` should be the build audit of step 5.
7. Open the Daily briefs tab; both briefs render, the audit flags show
   with severity badges, and the build audit table renders (pipe table
   → HTML).
8. Leave it running past 11:00 tomorrow and past T-60 of the Main
   slate; confirm both fire once (log lines, `fired: true` in the
   schedule status).

## Known limits / next

- The morning brief's `include_inhouse=True` slate build costs ~5–17s;
  fine on a timer, slow if hammered from the UI.
- The team map for the post-contest audit is rebuilt per upload from
  the MLB Stats API (a few seconds). Could be cached per date.
- No payout table in a DK export → cash line is top 20% (rule
  `CASH_LINE_FRACTION`). Real lines are 20–23%.
- The batting-order check uses confirmed order, then RotoWire's
  projected spot, then nothing. A player the slate can't see is
  neither flagged nor cleared.
- NFL is untouched. `build_audit.py` assumes the 2-pitcher MLB shape.
