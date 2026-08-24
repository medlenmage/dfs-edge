import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { NflContestGeneratorPanel } from './NflContestGeneratorPanel'
import { NflLineupsPanel } from './NflLineupsPanel'
import { NflStackTable } from './NflStackTable'

function currentNflSeason() {
  const d = new Date()
  return d.getMonth() + 1 >= 3 ? d.getFullYear() : d.getFullYear() - 1
}

function NflGameCard({ game }) {
  const b = game.betting || {}
  const w = game.weather || {}
  return (
    <div className="card">
      <div className="game-head">
        <div className="matchup">
          {game.away.abbrev} @ {game.home.abbrev}
        </div>
        <div className="time">
          {game.weekday} {game.gameday} {game.gametime}
        </div>
      </div>

      <div className="sub-line" style={{ marginBottom: 8 }}>{game.stadium}</div>

      <div className="env-row">
        {b.total_line != null && <span className="badge">O/U {b.total_line}</span>}
        {b.spread_line != null && <span className="badge">spread {Math.abs(b.spread_line)}</span>}
        {game.roof === 'outdoors' ? (
          <>
            {w.temp_f != null && <span className="badge">{Math.round(w.temp_f)}°F</span>}
            {w.wind_mph != null && <span className="badge">wind {w.wind_mph}mph</span>}
            {w.precip_chance_pct >= 40 && (
              <span className="badge risk">{w.precip_chance_pct}% rain</span>
            )}
            {w.note && <span className="badge">{w.note}</span>}
          </>
        ) : (
          <span className="badge">{game.roof || 'roof unknown'}</span>
        )}
      </div>

      {[game.away, game.home].map((side) => (
        <div key={side.abbrev} className="team-row">
          <div>
            <div className="tname">
              {side.abbrev}
              {side.favored ? <span className="dim" style={{ fontWeight: 400 }}> (favored)</span> : null}
            </div>
            {side.players?.length > 0 && (
              <div className="sub-line">
                {side.players.slice(0, 3).map((p) => `${p.name} (${p.position})`).join(', ')}
                {side.players.length > 3 ? `, +${side.players.length - 3} more` : ''}
              </div>
            )}
          </div>
          <div className="runs">
            {side.implied_total != null ? `${side.implied_total.toFixed(1)} pts` : '—'}
          </div>
        </div>
      ))}
    </div>
  )
}

function NflMatchups({ slate }) {
  const games = slate?.games || []
  if (!games.length) {
    return <div className="notice">{slate?.message || 'No games scheduled.'}</div>
  }
  return (
    <div className="games">
      {games.map((g) => (
        <NflGameCard key={g.game_id} game={g} />
      ))}
    </div>
  )
}

/**
 * NFL section: weekly matchups (Vegas-implied context + weather) and a
 * DraftKings Classic NFL lineup optimizer. Self-contained -- unlike the
 * MLB side of the app, an NFL slate is keyed by season+week, not a
 * calendar date, so this manages its own state rather than sharing
 * App.jsx's date-driven slate fetch.
 */
export function NflPanel() {
  const [season, setSeason] = useState('')
  const [week, setWeek] = useState('')
  const [slate, setSlate] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [tab, setTab] = useState('matchups')
  const [salaryMsg, setSalaryMsg] = useState(null)
  const [projectionMsg, setProjectionMsg] = useState(null)
  const [rotowireLoading, setRotowireLoading] = useState(false)
  const salaryInputRef = useRef(null)
  const projectionInputRef = useRef(null)

  async function load(overrideSeason, overrideWeek) {
    setLoading(true)
    setError(null)
    try {
      const data = await api.nflSlate(overrideSeason ?? (season || null), overrideWeek ?? (week || null))
      setSlate(data)
      setSeason(String(data.season))
      setWeek(String(data.week))
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function makeUploadHandler(uploadFn, setMsg, label) {
    return async (e) => {
      const file = e.target.files?.[0]
      e.target.value = ''
      if (!file || !slate) return
      setMsg('Uploading…')
      try {
        const result = await uploadFn(slate.season, slate.week, file)
        setMsg(`Loaded ${result.players_loaded} ${label}`)
        load(slate.season, slate.week)
      } catch (err) {
        setMsg(`Upload failed: ${err.message}`)
      }
    }
  }
  const handleSalaryUpload = makeUploadHandler(api.nflUploadSalaries, setSalaryMsg, 'salaries')
  const handleProjectionUpload = makeUploadHandler(api.nflUploadProjections, setProjectionMsg, 'projections')

  // Pulls RotoWire's own live optimizer player pool directly -- no
  // manual CSV download/upload. Doesn't touch salaries (see api.js's
  // own comment on why NFL's version can't derive them the way MLB's does).
  async function refreshFromRotowire() {
    if (!slate) return
    setRotowireLoading(true)
    setProjectionMsg('Pulling live projections from RotoWire…')
    try {
      const result = await api.nflRefreshRotowireProjections(slate.season, slate.week, { refresh: true })
      const unmatched = result.unmatched?.length
        ? ` -- ${result.unmatched.length} didn't match the loaded salary file by name (${result.unmatched
            .slice(0, 5)
            .join(', ')}${result.unmatched.length > 5 ? ', …' : ''})`
        : ''
      setProjectionMsg(`Loaded ${result.players_loaded} live RotoWire projections${unmatched}`)
      load(slate.season, slate.week)
    } catch (err) {
      setProjectionMsg(`RotoWire refresh failed: ${err.message}`)
    } finally {
      setRotowireLoading(false)
    }
  }

  return (
    <div>
      <div className="controls" style={{ marginBottom: 14, flexWrap: 'wrap' }}>
        <label className="dim" style={{ fontSize: 13 }}>
          Season{' '}
          <input
            type="number"
            value={season}
            placeholder={String(currentNflSeason())}
            onChange={(e) => setSeason(e.target.value)}
            style={{ width: 70 }}
          />
        </label>
        <label className="dim" style={{ fontSize: 13 }}>
          Week{' '}
          <input
            type="number"
            min="1"
            max="18"
            value={week}
            onChange={(e) => setWeek(e.target.value)}
            style={{ width: 55 }}
          />
        </label>
        <button onClick={() => load()} disabled={loading}>
          {loading ? 'Loading…' : 'Load'}
        </button>
        <input
          ref={salaryInputRef}
          type="file"
          accept=".csv"
          onChange={handleSalaryUpload}
          style={{ display: 'none' }}
        />
        <button
          onClick={() => salaryInputRef.current?.click()}
          title="Upload a DraftKings NFL salary CSV for this week"
        >
          Upload salaries
        </button>
        <input
          ref={projectionInputRef}
          type="file"
          accept=".csv"
          onChange={handleProjectionUpload}
          style={{ display: 'none' }}
        />
        <button
          onClick={() => projectionInputRef.current?.click()}
          title="Upload a RotoWire NFL projections CSV for this week"
        >
          Upload projections
        </button>
        <button
          onClick={refreshFromRotowire}
          disabled={rotowireLoading}
          title="Pull RotoWire's own live optimizer player pool directly -- salary, position, opponent, FPTS, and rostership% -- with no manual CSV download/upload. Always bypasses the cache for genuinely live data. Doesn't touch DK salaries -- RotoWire's NFL export has no DK numeric player id, so a real DK salary CSV is still needed separately."
        >
          {rotowireLoading ? 'Loading…' : 'Refresh from RotoWire'}
        </button>
      </div>

      {(salaryMsg || projectionMsg) && (
        <div className="dim" style={{ fontSize: 13, marginBottom: 12 }}>
          {[salaryMsg, projectionMsg].filter(Boolean).join(' · ')}
        </div>
      )}

      {error && (
        <div className="notice error" style={{ marginBottom: 16 }}>
          Couldn't load the slate: {error}
          <div style={{ marginTop: 8 }}>
            <button onClick={() => load()}>Retry</button>
          </div>
        </div>
      )}

      {loading && !slate && (
        <div className="card">
          <div className="skeleton" style={{ width: '40%', marginBottom: 12 }} />
          <div className="skeleton" style={{ width: '100%', marginBottom: 8 }} />
          <div className="skeleton" style={{ width: '88%' }} />
        </div>
      )}

      {slate && (
        <>
          <div className="tabs">
            <button className={`tab ${tab === 'matchups' ? 'active' : ''}`} onClick={() => setTab('matchups')}>
              Matchups
            </button>
            <button className={`tab ${tab === 'stacks' ? 'active' : ''}`} onClick={() => setTab('stacks')}>
              Stacks
            </button>
            <button className={`tab ${tab === 'lineups' ? 'active' : ''}`} onClick={() => setTab('lineups')}>
              Lineups
            </button>
            <button className={`tab ${tab === 'contest' ? 'active' : ''}`} onClick={() => setTab('contest')}>
              Contest Generator
            </button>
          </div>

          {tab === 'matchups' && <NflMatchups slate={slate} />}
          {tab === 'stacks' && <NflStackTable season={slate.season} week={slate.week} />}
          {tab === 'lineups' && <NflLineupsPanel season={slate.season} week={slate.week} slate={slate} />}
          {tab === 'contest' && <NflContestGeneratorPanel season={slate.season} week={slate.week} />}
        </>
      )}
    </div>
  )
}
