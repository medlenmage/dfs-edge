import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api'
import { slateSummary } from './components/StatTile'
import { StackTable } from './components/StackTable'
import { HitterTable } from './components/HitterTable'
import { PitcherTable } from './components/PitcherTable'
import { GameGrid } from './components/GameCard'
import { AnalysisPanel } from './components/AnalysisPanel'
import { LineupsPanel } from './components/LineupsPanel'
import { ContestGeneratorPanel } from './components/ContestGeneratorPanel'
import { ContestSimulatorPanel } from './components/ContestSimulatorPanel'
import { ResultsPanel } from './components/ResultsPanel'
import { NflPanel } from './components/NflPanel'
import { ScoreLegend } from './components/ScoreMeter'
import { DkSlatePicker } from './components/DkSlatePicker'

// The rail's grouped navigation, from the v2 redesign: research the
// slate, build against it, review what happened. The four research
// views (Stacks/Hitters/Pitchers/Games) collapse into one "Slate" nav
// entry with its own segmented sub-tabs, which is what stopped the top
// of the app being a nine-item tab strip.
const NAV_GROUPS = [
  {
    group: 'Research',
    items: [{ id: 'slate', label: 'Slate' }],
  },
  {
    group: 'Build',
    items: [
      { id: 'lineups', label: 'Lineup optimizer' },
      { id: 'contest', label: 'Contest generator' },
      { id: 'simulator', label: 'Simulator' },
    ],
  },
  {
    group: 'Review',
    items: [
      { id: 'results', label: 'Results' },
      { id: 'ai', label: 'AI read on the slate' },
    ],
  },
]

// Sub-tabs inside the Slate view.
const SLATE_TABS = [
  { id: 'stacks', label: 'Stacks' },
  { id: 'hitters', label: 'Hitters' },
  { id: 'pitchers', label: 'Pitchers' },
  { id: 'games', label: 'Games' },
]

// One title/blurb per view, so every page opens the same way instead of
// each panel inventing its own heading.
const VIEW_HEADINGS = {
  slate: {
    title: 'Slate research',
    blurb: 'Rank stacks, hitters and pitchers by matchup edge, then send picks straight to the optimizer.',
  },
  lineups: {
    title: 'Lineup optimizer',
    blurb: 'DraftKings Classic MLB — one provably optimal lineup per click.',
  },
  contest: {
    title: 'Contest generator',
    blurb: 'Build a whole contest: lineups only, no economics. Send it to the simulator to price it.',
  },
  simulator: {
    title: 'Simulator',
    blurb: 'Price a contest the generator already built — entry cost and payout curve are set here.',
  },
  results: {
    title: 'Results',
    blurb: 'Upload a DraftKings standings export to archive real ownership and track how your entries did.',
  },
  ai: {
    title: 'AI read on the slate',
    blurb: "Claude's narrative summary of everything the tables show.",
  },
}

// DK Classic MLB roster-slot positions, in DK's own order, as sub-tabs
// under Hitters -- the same nested-tab shape NflPanel.jsx uses for the
// NFL Players tab. A hitter's real DK salary position wins when a
// salary CSV is loaded (the first slot of a multi-eligible string like
// "1B/3B"); before that, HitterTable falls back to a normalized read
// of his MLB bio position, which splits the outfield into LF/CF/RF and
// has no DH slot at all. "All" keeps the unsegmented view the position
// dropdown this replaced offered.
const HITTER_SUB_TABS = [
  { id: 'ALL', label: 'All', positions: null },
  { id: 'C', label: 'C', positions: ['C'] },
  { id: '1B', label: '1B', positions: ['1B'] },
  { id: '2B', label: '2B', positions: ['2B'] },
  { id: '3B', label: '3B', positions: ['3B'] },
  { id: 'SS', label: 'SS', positions: ['SS'] },
  { id: 'OF', label: 'OF', positions: ['OF'] },
  { id: 'DH', label: 'DH', positions: ['DH'] },
]

function today() {
  // Local date, not UTC -- otherwise the slate flips over at 7pm Central.
  const d = new Date()
  const off = d.getTimezoneOffset() * 60000
  return new Date(d - off).toISOString().slice(0, 10)
}

export default function App() {
  const [sport, setSport] = useState('mlb')
  const [date, setDate] = useState(today())
  const [tab, setTab] = useState('slate')
  const [slateTab, setSlateTab] = useState('stacks')
  const [dataMenuOpen, setDataMenuOpen] = useState(false)
  const [hitterSubTab, setHitterSubTab] = useState('ALL')
  const [slate, setSlate] = useState(null)
  const [health, setHealth] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const [projectionMsg, setProjectionMsg] = useState(null)
  const [rotowireLoading, setRotowireLoading] = useState(false)
  // Every Classic slate window the last RotoWire scrape actually found,
  // and which one is currently active for this date.
  const [rotowireSlates, setRotowireSlates] = useState([])
  const [activeRotowireSlate, setActiveRotowireSlate] = useState(null)
  const projectionInputRef = useRef(null)
  // Which FPTS/ownership numbers feed the optimizer and contest
  // generator -- independent of whether the tables have fetched the
  // in-house columns yet, since those two endpoints fetch their own
  // in-house-augmented slate server-side when asked.
  const [projSource, setProjSource] = useState('rotowire')
  // The contest the generator last built, handed to the Simulator tab.
  // Lives up here rather than inside either panel because it's the one
  // thing the two of them share: the generator produces it, the
  // simulator prices it, and neither owns the other.
  const [contestBatch, setContestBatch] = useState(null)
  const [inhouseLoading, setInhouseLoading] = useState(false)

  // The in-house pass is what carries in-house FPTS/ownership, leverage,
  // AND the Boom%/Bust% columns -- all four come from the same real
  // per-player game-log fetch across the whole slate. It's genuinely
  // slow the first time each day (measured: 0.5s for the plain slate,
  // ~4.5s warm with in-house, ~17s cold), which is why it can't just be
  // folded into the initial load.
  //
  // So it runs automatically in the BACKGROUND instead: the dashboard
  // renders instantly off the fast slate, then this fills the in-house
  // and boom/bust columns in behind it a few seconds later. Before this,
  // those columns simply read "—" forever unless you happened to know
  // about the "Load in-house projections" button -- and any Refresh,
  // date change or projections upload silently wiped them back out,
  // since every one of those reloads went through the fast path.
  const loadInhouse = useCallback(
    async ({ background = false } = {}) => {
      if (!background) setInhouseLoading(true)
      try {
        const data = await api.slate(date, { inhouse: true })
        // Only adopt it if the user hasn't since moved to another date
        // -- a slow background response must never overwrite a newer
        // slate with stale data.
        setSlate((prev) => (prev && prev.date !== data.date ? prev : data))
      } catch (err) {
        // A background failure is not worth an error banner over the
        // whole dashboard: the fast slate is already rendered and
        // perfectly usable, just without the in-house columns.
        if (!background) setError(err.message)
      } finally {
        if (!background) setInhouseLoading(false)
      }
    },
    [date],
  )

  const load = useCallback(
    async (refresh = false) => {
      setLoading(true)
      setError(null)
      try {
        const data = await api.slate(date, { refresh })
        setSlate(data)
      } catch (err) {
        setError(err.message)
      } finally {
        setLoading(false)
      }
      // Fill the in-house/boom-bust columns in behind the fast render,
      // every time the slate reloads -- so they survive a Refresh or a
      // projections upload instead of vanishing.
      loadInhouse({ background: true })
    },
    [date, loadInhouse],
  )

  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth(null))
  }, [])

  useEffect(() => {
    load(false)
  }, [load])

  // Shared shape for both CSV uploads: pick a file, POST it, reload the
  // slate so the match shows up, report success/failure transiently.
  function makeUploadHandler(uploadFn, setMsg, label) {
    return async (e) => {
      const file = e.target.files?.[0]
      e.target.value = '' // allow re-selecting the same file later
      if (!file) return
      setMsg('Uploading…')
      try {
        const result = await uploadFn(date, file)
        const derived = result.salaries_derived
          ? ` (salaries pulled from the same file for ${result.salaries_derived} of them)`
          : ''
        const unmatched = result.unmatched?.length
          ? ` -- ${result.unmatched.length} didn't match today's DK slate by name (${result.unmatched
              .slice(0, 5)
              .join(', ')}${result.unmatched.length > 5 ? ', …' : ''})`
          : ''
        setMsg(`Loaded ${result.players_loaded} ${label}${derived}${unmatched}`)
        load(true)
      } catch (err) {
        setMsg(`Upload failed: ${err.message}`)
      }
    }
  }

  const handleProjectionUpload = makeUploadHandler(api.uploadProjections, setProjectionMsg, 'projections')

  // Pulls RotoWire's own live optimizer player pool directly -- no
  // manual CSV download/upload. One call scrapes EVERY Classic slate
  // window RotoWire has live (All / Early / Afternoon / Turbo / Night /
  // Late Night); windows that don't exist today are skipped rather than
  // treated as failures. Always stored under THAT slate's own real date
  // (RotoWire's, not whatever date is currently selected here), so if it
  // differs, switch the date picker to match and reload -- otherwise a
  // successful refresh would silently not show up anywhere.
  //
  // `slateName` picks which scraped window becomes ACTIVE. Switching is
  // cheap: the backend already cached every window's rows, so it needs
  // no new network call.
  async function refreshFromRotowire(slateName = null) {
    setRotowireLoading(true)
    setProjectionMsg(
      slateName
        ? `Switching to RotoWire's ${slateName} slate…`
        : 'Scraping every live RotoWire slate…',
    )
    try {
      const result = await api.refreshRotowireProjections({ refresh: !slateName, slateName })
      setRotowireSlates(result.slates || [])
      setActiveRotowireSlate(result.active_slate || null)

      const derived = result.salaries_derived
        ? ` (salaries pulled from the same data for ${result.salaries_derived} of them)`
        : ''
      const unmatched = result.unmatched?.length
        ? ` -- ${result.unmatched.length} didn't match today's DK slate by name (${result.unmatched
            .slice(0, 5)
            .join(', ')}${result.unmatched.length > 5 ? ', …' : ''})`
        : ''
      const others = (result.slates || []).filter((s) => s.players && s.slate_name !== result.active_slate)
      const alsoFound = others.length
        ? ` Also scraped: ${others.map((s) => `${s.slate_name} (${s.players})`).join(', ')}.`
        : ''
      const skipped = (result.slates || []).filter((s) => s.error)
      const skippedMsg = skipped.length
        ? ` Skipped: ${skipped.map((s) => `${s.slate_name} (${s.error})`).join(', ')}.`
        : ''
      setProjectionMsg(
        `Loaded ${result.players_loaded} live RotoWire projections from the ${result.active_slate} slate for ${result.date}${derived}${unmatched}.${alsoFound}${skippedMsg}` +
          (result.note ? ` ${result.note}` : ''),
      )
      if (result.date !== date) {
        setDate(result.date)
      } else {
        load(true)
      }
    } catch (err) {
      setProjectionMsg(`RotoWire refresh failed: ${err.message}`)
    } finally {
      setRotowireLoading(false)
    }
  }

  const features = health?.features || {}

  const summary = slateSummary(slate)
  const rotowireChoices = rotowireSlates.filter((s) => s.players)
  const activeNav = NAV_GROUPS.flatMap((g) => g.items).find((i) => i.id === tab)
  const heading = VIEW_HEADINGS[tab]

  function stepDate(days) {
    const d = new Date(`${date}T12:00:00`)
    d.setDate(d.getDate() + days)
    setDate(d.toISOString().slice(0, 10))
  }

  return (
    <div className="shell">
      {/* ---------------------------------------------- rail */}
      <aside className="rail">
        <div className="brand">
          <strong>DFS Edge</strong>
          <span>v2</span>
        </div>

        <div className="sport" role="tablist" aria-label="Sport">
          <button className={sport === 'mlb' ? 'on' : ''} onClick={() => setSport('mlb')}>
            MLB
          </button>
          <button className={sport === 'nfl' ? 'on' : ''} onClick={() => setSport('nfl')}>
            NFL
          </button>
        </div>

        {sport === 'mlb' &&
          NAV_GROUPS.map((g) => (
            <div key={g.group}>
              <div className="group">{g.group}</div>
              <nav>
                {g.items.map((item) => (
                  <button
                    key={item.id}
                    className={tab === item.id ? 'on' : ''}
                    onClick={() => setTab(item.id)}
                  >
                    {item.label}
                    {item.id === 'slate' && summary && <small>{summary.games.length} games</small>}
                  </button>
                ))}
              </nav>
            </div>
          ))}

        <div className="rail-foot">
          {health?.odds_api_credits?.remaining
            ? `${Number(health.odds_api_credits.remaining).toLocaleString()} odds credits`
            : 'DFS Edge'}
          <br />
          MLB Stats · Open-Meteo · FantasyLabs · The Odds API
        </div>
      </aside>

      {/* ---------------------------------------------- main */}
      <div className="main">
        {sport === 'mlb' && (
          <header className="slatebar">
            <div className="datestep">
              <button onClick={() => stepDate(-1)} aria-label="Previous day">
                ‹
              </button>
              <input type="date" value={date} onChange={(e) => setDate(e.target.value)} />
              <button onClick={() => stepDate(1)} aria-label="Next day">
                ›
              </button>
            </div>

            {rotowireChoices.length > 1 && (
              <select
                value={activeRotowireSlate || ''}
                disabled={rotowireLoading}
                onChange={(e) => refreshFromRotowire(e.target.value)}
                title="Which scraped RotoWire window is active for this date. Everything downstream reads one slate per date, so switching replaces the loaded pool."
              >
                {rotowireChoices.map((s) => (
                  <option key={s.slate_name} value={s.slate_name}>
                    {s.slate_name} ({s.players})
                  </option>
                ))}
              </select>
            )}

            {summary && (
              <div className="kpis" aria-label="Slate summary">
                <div className="kpi">
                  <b>{summary.avgTotal ?? '—'}</b>
                  <span>avg total</span>
                </div>
                <div className="kpi">
                  <b>
                    {summary.bestTeam
                      ? `${summary.bestTeam.name} ${summary.bestTeam.runs.toFixed(1)}`
                      : '—'}
                  </b>
                  <span>top implied</span>
                </div>
                <div className="kpi">
                  <b>
                    {summary.bestStack
                      ? `${summary.bestStack.name} ${Math.round(summary.bestStack.score)}`
                      : '—'}
                  </b>
                  <span>best stack</span>
                </div>
                <div className="kpi">
                  <b>
                    {summary.confirmed}/{summary.games.length * 2}
                  </b>
                  <span>confirmed</span>
                </div>
              </div>
            )}

            <div className="grow" />

            <div className="status">
              <span className={`dot ${loading ? 'warn' : ''}`} />
              {loading ? 'Loading…' : 'Data loaded'}
            </div>

            {/* Every load/refresh/upload control lives in here now. The
                header used to carry six buttons, two selects and a file
                input in one row; they are the same actions, just no
                longer competing with the slate itself for attention. */}
            <div className="menu-wrap">
              <button onClick={() => setDataMenuOpen((v) => !v)} aria-expanded={dataMenuOpen}>
                Data ▾
              </button>
              {dataMenuOpen && (
                <div className="menu" role="menu" onMouseLeave={() => setDataMenuOpen(false)}>
                  <div className="menu-row">
                    <div>
                      <div className="t">Matchups &amp; lineups</div>
                      <div className="s">Scores, park/weather, betting lines, confirmed lineups</div>
                    </div>
                    <button className="sm" onClick={() => load(true)} disabled={loading}>
                      {loading ? 'Loading…' : 'Refresh'}
                    </button>
                  </div>

                  <div className="menu-row">
                    <div>
                      <div className="t">DraftKings salaries</div>
                      <div className="s">Pull a real live DK slate — no manual CSV</div>
                    </div>
                    <DkSlatePicker date={date} onLoaded={() => load(true)} />
                  </div>

                  <div className="menu-row">
                    <div>
                      <div className="t">RotoWire projections</div>
                      <div className="s">
                        Scrapes every live Classic window and auto-matches the loaded DK slate
                      </div>
                    </div>
                    <button
                      className="sm"
                      onClick={() => refreshFromRotowire()}
                      disabled={rotowireLoading}
                    >
                      {rotowireLoading ? 'Loading…' : 'Refresh'}
                    </button>
                  </div>

                  <div className="menu-row">
                    <div>
                      <div className="t">In-house projections</div>
                      <div className="s">
                        Also fills the Boom%/Bust% columns — loads automatically in the background
                      </div>
                    </div>
                    <button className="sm" onClick={() => loadInhouse()} disabled={inhouseLoading}>
                      {inhouseLoading ? 'Computing…' : 'Rebuild'}
                    </button>
                  </div>

                  <div className="menu-row">
                    <div>
                      <div className="t">Upload a projections CSV</div>
                      <div className="s">A RotoWire FPTS/ownership export for this date</div>
                    </div>
                    <button className="sm" onClick={() => projectionInputRef.current?.click()}>
                      Choose file
                    </button>
                  </div>

                  <div className="menu-sep" />
                  <div className="menu-foot">
                    <span>Optimizer uses</span>
                    <select value={projSource} onChange={(e) => setProjSource(e.target.value)}>
                      <option value="rotowire">RotoWire</option>
                      <option value="inhouse">In-house</option>
                    </select>
                  </div>
                </div>
              )}
            </div>

            <input
              ref={projectionInputRef}
              type="file"
              accept=".csv"
              onChange={handleProjectionUpload}
              style={{ display: 'none' }}
            />
          </header>
        )}

        <div className="content">
      {sport === 'nfl' && <NflPanel />}

      {sport === 'mlb' && (
        <>
          {heading && (
            <div className="ph">
              <div>
                <h1>{heading.title}</h1>
                <p>{heading.blurb}</p>
              </div>
              {tab === 'slate' && (
                <div className="subtabs">
                  {SLATE_TABS.map((t) => (
                    <button
                      key={t.id}
                      className={slateTab === t.id ? 'on' : ''}
                      onClick={() => setSlateTab(t.id)}
                    >
                      {t.label}
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}
      {projectionMsg && (
        <div className="dim" style={{ fontSize: 13, marginBottom: 12 }}>
          {projectionMsg}
        </div>
      )}

      {health && !features.player_props && (
        <div className="notice" style={{ marginBottom: 16 }}>
          Player props are off — game totals and implied team runs come free
          from FantasyLabs regardless, but home run/hit/strikeout market
          probabilities need <code>ODDS_API_KEY</code> (and{' '}
          <code>ODDS_FETCH_PROPS=true</code>) in <code>.env</code>.
        </div>
      )}

      {error && (
        <div className="notice error" style={{ marginBottom: 16 }}>
          Couldn’t load the slate: {error}
          <div style={{ marginTop: 8 }}>
            <button onClick={() => load(true)}>Retry</button>
          </div>
        </div>
      )}

      {slate?.warnings?.length > 0 && (
        <details className="notice" style={{ marginBottom: 16 }}>
          <summary>
            {slate.warnings.length} data source(s) had trouble — click for details
          </summary>
          <ul style={{ marginBottom: 0 }}>
            {slate.warnings.map((w, i) => (
              <li key={i} style={{ fontSize: 12.5 }}>{w}</li>
            ))}
          </ul>
        </details>
      )}

      {loading && !slate && (
        <div className="card">
          <div className="skeleton" style={{ width: '40%', marginBottom: 12 }} />
          <div className="skeleton" style={{ width: '100%', marginBottom: 8 }} />
          <div className="skeleton" style={{ width: '95%', marginBottom: 8 }} />
          <div className="skeleton" style={{ width: '88%' }} />
          <p className="dim" style={{ fontSize: 13, marginTop: 14 }}>
            First load of the day pulls a full season of league splits — give it
            20–30 seconds. After that it’s cached and instant.
          </p>
        </div>
      )}

      {slate && tab === 'slate' && slateTab === 'stacks' && (
        <section>
          <StackTable slate={slate} />
          <div style={{ marginTop: 10 }}>
            <ScoreLegend />
          </div>
        </section>
      )}

      {slate && tab === 'slate' && slateTab === 'hitters' && (
        <section>
          <div className="subtabs">
            {HITTER_SUB_TABS.map((t) => (
              <button
                key={t.id}
                className={hitterSubTab === t.id ? 'on' : ''}
                onClick={() => setHitterSubTab(t.id)}
              >
                {t.label}
              </button>
            ))}
          </div>
          <HitterTable
            slate={slate}
            positions={HITTER_SUB_TABS.find((t) => t.id === hitterSubTab).positions}
            limit={80}
          />
          <div style={{ marginTop: 10 }}>
            <ScoreLegend />
          </div>
        </section>
      )}

      {slate && tab === 'slate' && slateTab === 'pitchers' && (
        <section>
          <PitcherTable slate={slate} />
          <div style={{ marginTop: 10 }}>
            <ScoreLegend />
          </div>
        </section>
      )}

      {slate && tab === 'slate' && slateTab === 'games' && (
        <section>
          <GameGrid slate={slate} />
        </section>
      )}

      {slate && tab === 'lineups' && (
        <section>
          <LineupsPanel date={date} slate={slate} projectionSource={projSource} />
        </section>
      )}

      {slate && tab === 'contest' && (
        <section>
          <ContestGeneratorPanel
            date={date}
            slate={slate}
            projectionSource={projSource}
            onSimulate={(batch) => {
              setContestBatch(batch)
              setTab('simulator')
            }}
          />
        </section>
      )}

      {slate && tab === 'simulator' && (
        <section>
          <ContestSimulatorPanel
            date={date}
            batch={contestBatch}
            projectionSource={projSource}
            onOpenGenerator={() => setTab('contest')}
          />
        </section>
      )}

      {slate && tab === 'results' && (
        <section>
          <ResultsPanel date={date} />
        </section>
      )}

      {slate && tab === 'ai' && (
        <section>
          <AnalysisPanel date={date} enabled={!!features.ai_analysis} />
        </section>
      )}

      <footer
        className="dim"
        style={{ fontSize: 12, marginTop: 40, paddingTop: 16, borderTop: '1px solid var(--gridline)' }}
      >
        Data: MLB Stats API · Open-Meteo · FantasyLabs
        {features.player_props ? ' · The Odds API (props)' : ''}
        {slate?.generated_at && ` · built ${new Date(slate.generated_at).toLocaleTimeString()}`}
        {health?.odds_api_credits?.remaining &&
          ` · ${health.odds_api_credits.remaining} odds credits left`}
      </footer>
        </>
      )}
        </div>
      </div>
    </div>
  )
}
