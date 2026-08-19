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
  { id: 'ai', label: 'AI analysis' },
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
  const [slate, setSlate] = useState(null)
  const [health, setHealth] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [theme, setTheme] = useState(
    () => document.documentElement.dataset.theme || 'auto',
  )
  const [projectionMsg, setProjectionMsg] = useState(null)
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

      {health && !features.betting_lines && (
        <div className="notice" style={{ marginBottom: 16 }}>
          Betting lines are off — add <code>ODDS_API_KEY</code> to <code>.env</code>{' '}
          to turn on game totals and implied team runs. Everything else works
          without it, but implied runs are one of the strongest signals here.
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
          <HitterTable slate={slate} limit={80} />
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
        Data: MLB Stats API · Open-Meteo
        {features.betting_lines ? ' · The Odds API' : ''}
        {slate?.generated_at && ` · built ${new Date(slate.generated_at).toLocaleTimeString()}`}
        {health?.odds_api_credits?.remaining &&
          ` · ${health.odds_api_credits.remaining} odds credits left`}
      </footer>
        </>
      )}
    </div>
  )
}
