import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api'
import { SlateTiles } from './components/StatTile'
import { StackTable } from './components/StackTable'
import { HitterTable } from './components/HitterTable'
import { PitcherTable } from './components/PitcherTable'
import { GameGrid } from './components/GameCard'
import { AnalysisPanel } from './components/AnalysisPanel'
import { LineupsPanel } from './components/LineupsPanel'
import { ContestGeneratorPanel } from './components/ContestGeneratorPanel'
import { ResultsPanel } from './components/ResultsPanel'
import { NflPanel } from './components/NflPanel'
import { ScoreLegend } from './components/ScoreMeter'
import { DkSlatePicker } from './components/DkSlatePicker'

const TABS = [
  { id: 'stacks', label: 'Stacks' },
  { id: 'hitters', label: 'Hitters' },
  { id: 'pitchers', label: 'Pitchers' },
  { id: 'games', label: 'Games' },
  { id: 'lineups', label: 'Lineups' },
  { id: 'contest', label: 'Contest Generator' },
  { id: 'results', label: 'Results' },
  { id: 'ai', label: 'AI analysis' },
]

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
  const [tab, setTab] = useState('stacks')
  const [hitterSubTab, setHitterSubTab] = useState('ALL')
  const [slate, setSlate] = useState(null)
  const [health, setHealth] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [theme, setTheme] = useState(
    () => document.documentElement.dataset.theme || 'auto',
  )
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
  const [inhouseLoading, setInhouseLoading] = useState(false)

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
    },
    [date],
  )

  // In-house FPTS/ownership are opt-in on the backend (a real per-player
  // game-log fetch for every hitter/pitcher on the slate) -- fetched only
  // when explicitly asked for, not on every plain dashboard load.
  const loadInhouse = useCallback(async () => {
    setInhouseLoading(true)
    try {
      const data = await api.slate(date, { inhouse: true })
      setSlate(data)
    } catch (err) {
      setError(err.message)
    } finally {
      setInhouseLoading(false)
    }
  }, [date])

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

  function cycleTheme() {
    const next = theme === 'auto' ? 'light' : theme === 'light' ? 'dark' : 'auto'
    setTheme(next)
    if (next === 'auto') delete document.documentElement.dataset.theme
    else document.documentElement.dataset.theme = next
  }

  const features = health?.features || {}

  return (
    <div className="app">
      <header className="header">
        <h1>DFS Edge</h1>
        <div className="tabs" style={{ marginBottom: 0 }}>
          <button className={`tab ${sport === 'mlb' ? 'active' : ''}`} onClick={() => setSport('mlb')}>
            MLB
          </button>
          <button className={`tab ${sport === 'nfl' ? 'active' : ''}`} onClick={() => setSport('nfl')}>
            NFL
          </button>
        </div>
        <div className="spacer" />
        <div className="controls">
          {sport === 'mlb' && (
            <>
              <input
                type="date"
                value={date}
                onChange={(e) => setDate(e.target.value)}
              />
              <button
                onClick={() => load(true)}
                disabled={loading}
                title="Reload matchup data -- scores, park/weather, betting lines, lineups. Does not touch DraftKings salaries; use DkSlatePicker's own refresh for those"
              >
                {loading ? 'Loading…' : 'Refresh matchups'}
              </button>
              <DkSlatePicker date={date} onLoaded={() => load(true)} />
              <input
                ref={projectionInputRef}
                type="file"
                accept=".csv"
                onChange={handleProjectionUpload}
                style={{ display: 'none' }}
              />
              <button
                onClick={() => projectionInputRef.current?.click()}
                title="Upload a RotoWire FPTS/ownership projections CSV for this date"
              >
                Upload projections
              </button>
              <button
                onClick={() => refreshFromRotowire()}
                disabled={rotowireLoading}
                title="Pull RotoWire's own live optimizer player pool directly -- salary, position, opponent, projected/confirmed batting order, FPTS, and rostership% -- with no manual CSV download/upload. Scrapes EVERY Classic slate window RotoWire has live in one go (All, Early, Afternoon, Turbo, Night, Late Night), skipping any that aren't running today. Always bypasses the cache for genuinely live data (newly confirmed lineups, late scratches). Stored under that slate's own real date, switching the date picker to match if it differs."
              >
                {rotowireLoading ? 'Loading…' : 'Refresh from RotoWire'}
              </button>
              {rotowireSlates.filter((s) => s.players).length > 1 && (
                <label className="dim" style={{ fontSize: 13 }}>
                  Slate{' '}
                  <select
                    value={activeRotowireSlate || ''}
                    disabled={rotowireLoading}
                    onChange={(e) => refreshFromRotowire(e.target.value)}
                    title="Which of the scraped RotoWire windows is active for this date. Everything downstream (tables, optimizer, contest generator) reads one slate per date, so switching replaces the loaded pool -- but the rows are already cached, so it needs no new fetch."
                  >
                    {rotowireSlates
                      .filter((s) => s.players)
                      .map((s) => (
                        <option key={s.slate_name} value={s.slate_name}>
                          {s.slate_name} ({s.players})
                        </option>
                      ))}
                  </select>
                </label>
              )}
              <button
                onClick={loadInhouse}
                disabled={inhouseLoading}
                title="Compute this app's own in-house FPTS/ownership projections for this date (real per-player game-log fetches -- takes a few seconds)"
              >
                {inhouseLoading ? 'Computing…' : 'Load in-house projections'}
              </button>
              <label className="dim" style={{ fontSize: 13 }}>
                Optimizer/generator source{' '}
                <select value={projSource} onChange={(e) => setProjSource(e.target.value)}>
                  <option value="rotowire">RotoWire</option>
                  <option value="inhouse">In-house</option>
                </select>
              </label>
            </>
          )}
          <button onClick={cycleTheme} title="Theme">
            {theme === 'auto' ? 'Auto' : theme === 'light' ? 'Light' : 'Dark'}
          </button>
        </div>
      </header>

      {sport === 'nfl' && <NflPanel />}

      {sport === 'mlb' && (
        <>
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

      <section>
        <SlateTiles slate={slate} />
      </section>

      <div className="tabs">
        {TABS.map((t) => (
          <button
            key={t.id}
            className={`tab ${tab === t.id ? 'active' : ''}`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

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

      {slate && tab === 'stacks' && (
        <section>
          <div className="section-head">
            <h2>Best offences to stack</h2>
            <span className="hint">
              average matchup score of each team’s five best bats
            </span>
          </div>
          <StackTable slate={slate} />
          <div style={{ marginTop: 10 }}>
            <ScoreLegend />
          </div>
        </section>
      )}

      {slate && tab === 'hitters' && (
        <section>
          <div className="section-head">
            <h2>Every hitter, ranked</h2>
            <span className="hint">click a column heading to re-sort</span>
          </div>
          <div className="tabs" style={{ marginBottom: 14 }}>
            {HITTER_SUB_TABS.map((t) => (
              <button
                key={t.id}
                className={`tab ${hitterSubTab === t.id ? 'active' : ''}`}
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

      {slate && tab === 'pitchers' && (
        <section>
          <div className="section-head">
            <h2>Top pitchers</h2>
            <span className="hint">today's probable starters, ranked by matchup edge</span>
          </div>
          <PitcherTable slate={slate} />
          <div style={{ marginTop: 10 }}>
            <ScoreLegend />
          </div>
        </section>
      )}

      {slate && tab === 'games' && (
        <section>
          <div className="section-head">
            <h2>Game environments</h2>
            <span className="hint">park, weather and the betting line</span>
          </div>
          <GameGrid slate={slate} />
        </section>
      )}

      {slate && tab === 'lineups' && (
        <section>
          <div className="section-head">
            <h2>Lineup optimizer</h2>
            <span className="hint">DraftKings Classic MLB — one optimal lineup per click</span>
          </div>
          <LineupsPanel date={date} slate={slate} projectionSource={projSource} />
        </section>
      )}

      {slate && tab === 'contest' && (
        <section>
          <div className="section-head">
            <h2>Contest generator</h2>
            <span className="hint">mass multi-entry — up to 10,000 of your own entries per contest</span>
          </div>
          <ContestGeneratorPanel date={date} slate={slate} projectionSource={projSource} />
        </section>
      )}

      {slate && tab === 'results' && (
        <section>
          <div className="section-head">
            <h2>Your contest results</h2>
            <span className="hint">upload real DK contest-standings exports to track results and archive market ownership data</span>
          </div>
          <ResultsPanel date={date} />
        </section>
      )}

      {slate && tab === 'ai' && (
        <section>
          <div className="section-head">
            <h2>Claude’s read on the slate</h2>
          </div>
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
  )
}
