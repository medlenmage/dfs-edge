import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { api } from '../api'
import { NflContestGeneratorPanel } from './NflContestGeneratorPanel'
import { NflContestSimulatorPanel } from './NflContestSimulatorPanel'
import { NflLineupsPanel } from './NflLineupsPanel'
import { NflPositionTable } from './NflPositionTable'
import { NflStackTable } from './NflStackTable'

// Sub-tabs within the Players tab. FLEX isn't a real player position --
// it's DK's own roster-slot concept (any RB/WR/TE) -- so it's just the
// union of those three positions, no new data needed.
const PLAYER_SUB_TABS = [
  { id: 'QB', label: 'QB', positions: ['QB'] },
  { id: 'RB', label: 'RB', positions: ['RB'] },
  { id: 'WR', label: 'WR', positions: ['WR'] },
  { id: 'TE', label: 'TE', positions: ['TE'] },
  { id: 'FLEX', label: 'FLEX', positions: ['RB', 'WR', 'TE'] },
  { id: 'DST', label: 'DST', positions: ['DST'] },
]

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
export function NflPanel({ tab: controlledTab, onTabChange, headerSlot }) {
  const [season, setSeason] = useState('')
  const [week, setWeek] = useState('')
  const [slate, setSlate] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  // Tab state is CONTROLLED when App drives it from the rail, and
  // internal otherwise -- so this panel still works standalone (and in
  // isolation) without the rail having to exist.
  const [internalTab, setInternalTab] = useState('matchups')
  const tab = controlledTab ?? internalTab
  const setTab = onTabChange ?? setInternalTab
  // Generator and simulator used to be two rail entries with a tab
  // switch between them; they're one page now. 'simulator' is kept as an
  // alias so a stored/controlled tab id from before still resolves.
  const contestTab = tab === 'contest' || tab === 'simulator'
  const [playerSubTab, setPlayerSubTab] = useState('QB')
  // The contest the generator last built, handed to the Simulator tab.
  // Lives here rather than inside either panel because it's the one
  // thing the two of them share: the generator produces it, the
  // simulator prices it, and neither owns the other.
  const [contestBatch, setContestBatch] = useState(null)
  const [salaryMsg, setSalaryMsg] = useState(null)
  const [projectionMsg, setProjectionMsg] = useState(null)
  const [rotowireLoading, setRotowireLoading] = useState(false)
  const [inhouseLoading, setInhouseLoading] = useState(false)
  const salaryInputRef = useRef(null)
  const projectionInputRef = useRef(null)
  // The NFL side had no header bar at all: the week -- the single most
  // important piece of context on the page -- was a bare number input
  // buried in a row of upload buttons. It now gets the same sticky bar
  // MLB's date has, portalled into the slot App holds open above the
  // scrolling content. State stays here, because an NFL slate is keyed
  // by season+week and this panel is what owns that.
  const [dataMenuOpen, setDataMenuOpen] = useState(false)
  const dataMenuRef = useRef(null)

  useEffect(() => {
    if (!dataMenuOpen) return
    const onPointerDown = (e) => {
      if (!dataMenuRef.current?.contains(e.target)) setDataMenuOpen(false)
    }
    const onKey = (e) => {
      if (e.key === 'Escape') setDataMenuOpen(false)
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [dataMenuOpen])

  async function load(overrideSeason, overrideWeek, { inhouse = false } = {}) {
    setLoading(true)
    setError(null)
    try {
      const data = await api.nflSlate(
        overrideSeason ?? (season || null),
        overrideWeek ?? (week || null),
        { inhouse },
      )
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

  // Opt-in for the same reason MLB's is: computing these reads a real
  // game log for every player on the slate, which a plain refresh
  // shouldn't silently pay for.
  async function loadInhouse() {
    if (!slate) return
    setInhouseLoading(true)
    try {
      await load(slate.season, slate.week, { inhouse: true })
    } finally {
      setInhouseLoading(false)
    }
  }

  // A week stepper needs the week as a number, and NFL regular season
  // is 1-18. Stepping past either end is simply a no-op rather than a
  // request the backend will reject.
  const weekNum = Number(week) || null
  function stepWeek(delta) {
    if (!weekNum) return
    const next = weekNum + delta
    if (next < 1 || next > 18) return
    setWeek(String(next))
    load(season || null, next)
  }

  // Slate summary for the header, from the same numbers the matchup
  // cards already show -- no extra fetch.
  const games = slate?.games || []
  const totals = games.map((g) => g.betting?.total_line).filter((n) => n != null)
  const avgTotal = totals.length
    ? (totals.reduce((a, b) => a + b, 0) / totals.length).toFixed(1)
    : null
  const sides = games.flatMap((g) => [g.away, g.home]).filter((t) => t?.implied_total != null)
  const topTeam = sides.length
    ? sides.reduce((best, t) => (t.implied_total > best.implied_total ? t : best))
    : null

  const header = headerSlot
    ? createPortal(
        <>
          <div className="datestep" aria-label="Week">
            <button onClick={() => stepWeek(-1)} disabled={!weekNum || weekNum <= 1} aria-label="Previous week">
              ‹
            </button>
            <span className="weeklabel">{weekNum ? `Week ${weekNum}` : 'Week —'}</span>
            <button onClick={() => stepWeek(1)} disabled={!weekNum || weekNum >= 18} aria-label="Next week">
              ›
            </button>
          </div>

          <label className="dim" style={{ fontSize: 12.5 }}>
            Season{' '}
            <input
              type="number"
              value={season}
              placeholder={String(currentNflSeason())}
              onChange={(e) => setSeason(e.target.value)}
              onBlur={() => load()}
              style={{ width: 70 }}
            />
          </label>

          {games.length > 0 && (
            <div className="kpis" aria-label="Week summary">
              <div className="kpi">
                <b>{games.length}</b>
                <span>games</span>
              </div>
              <div className="kpi">
                <b>{avgTotal ?? '—'}</b>
                <span>avg total</span>
              </div>
              <div className="kpi">
                <b>
                  {topTeam ? `${topTeam.abbrev} ${topTeam.implied_total.toFixed(1)}` : '—'}
                </b>
                <span>top implied</span>
              </div>
            </div>
          )}

          <div className="grow" />

          <div className="status">
            <span className={`dot ${loading ? 'warn' : ''}`} />
            {loading ? 'Loading…' : 'Data loaded'}
          </div>

          {/* Same one-home-for-every-load-control shape as MLB's. */}
          <div className="menu-wrap" ref={dataMenuRef}>
            <button onClick={() => setDataMenuOpen((v) => !v)} aria-expanded={dataMenuOpen}>
              Data ▾
            </button>
            {dataMenuOpen && (
              <div className="menu" role="menu">
                <div className="menu-row">
                  <div>
                    <div className="t">Matchups &amp; lines</div>
                    <div className="s">Schedule, Vegas totals and spreads, weather</div>
                  </div>
                  <button className="sm" onClick={() => load()} disabled={loading}>
                    {loading ? 'Loading…' : 'Reload'}
                  </button>
                </div>

                <div className="menu-row">
                  <div>
                    <div className="t">DraftKings salaries</div>
                    <div className="s">Upload a DK NFL salary CSV for this week</div>
                  </div>
                  <button className="sm" onClick={() => salaryInputRef.current?.click()}>
                    Choose file
                  </button>
                </div>

                <div className="menu-row">
                  <div>
                    <div className="t">RotoWire projections</div>
                    <div className="s">
                      Live optimizer pool — FPTS and rostership, no CSV. Doesn&rsquo;t touch
                      DK salaries.
                    </div>
                  </div>
                  <button className="sm" onClick={refreshFromRotowire} disabled={rotowireLoading}>
                    {rotowireLoading ? 'Loading…' : 'Refresh'}
                  </button>
                </div>

                <div className="menu-row">
                  <div>
                    <div className="t">In-house projections</div>
                    <div className="s">
                      This app&rsquo;s own FPTS, ownership, ceiling and leverage from real game logs
                    </div>
                  </div>
                  <button className="sm" onClick={loadInhouse} disabled={inhouseLoading || !slate}>
                    {inhouseLoading ? 'Computing…' : 'Compute'}
                  </button>
                </div>

                <div className="menu-row">
                  <div>
                    <div className="t">Upload a projections CSV</div>
                    <div className="s">A RotoWire NFL projections export for this week</div>
                  </div>
                  <button className="sm" onClick={() => projectionInputRef.current?.click()}>
                    Choose file
                  </button>
                </div>
              </div>
            )}
          </div>
        </>,
        headerSlot,
      )
    : null

  return (
    <div>
      {header}

      {/* The visible controls all moved into the header's Data menu;
          these two inputs are only here to be clicked programmatically. */}
      <input
        ref={salaryInputRef}
        type="file"
        accept=".csv"
        onChange={handleSalaryUpload}
        style={{ display: 'none' }}
      />
      <input
        ref={projectionInputRef}
        type="file"
        accept=".csv"
        onChange={handleProjectionUpload}
        style={{ display: 'none' }}
      />

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
          <div className="tabs" style={onTabChange ? { display: 'none' } : undefined}>
            <button className={`tab ${tab === 'matchups' ? 'active' : ''}`} onClick={() => setTab('matchups')}>
              Matchups
            </button>
            <button className={`tab ${tab === 'stacks' ? 'active' : ''}`} onClick={() => setTab('stacks')}>
              Stacks
            </button>
            <button className={`tab ${tab === 'players' ? 'active' : ''}`} onClick={() => setTab('players')}>
              Players
            </button>
            <button className={`tab ${tab === 'lineups' ? 'active' : ''}`} onClick={() => setTab('lineups')}>
              Lineups
            </button>
            <button className={`tab ${contestTab ? 'active' : ''}`} onClick={() => setTab('contest')}>
              Contest &amp; sim
            </button>
          </div>

          {tab === 'matchups' && <NflMatchups slate={slate} />}
          {tab === 'stacks' && <NflStackTable season={slate.season} week={slate.week} />}
          {tab === 'players' && (
            <>
              <div className="tabs" style={{ marginBottom: 14 }}>
                {PLAYER_SUB_TABS.map((t) => (
                  <button
                    key={t.id}
                    className={`tab ${playerSubTab === t.id ? 'active' : ''}`}
                    onClick={() => setPlayerSubTab(t.id)}
                  >
                    {t.label}
                  </button>
                ))}
              </div>
              <NflPositionTable
                slate={slate}
                positions={PLAYER_SUB_TABS.find((t) => t.id === playerSubTab).positions}
              />
            </>
          )}
          {tab === 'lineups' && <NflLineupsPanel season={slate.season} week={slate.week} slate={slate} />}
          {contestTab && (
            <NflContestGeneratorPanel
              season={slate.season}
              week={slate.week}
              onBuilt={setContestBatch}
              simulator={
                contestBatch ? (
                  <>
                    <div className="section-head">
                      <h2>Simulate</h2>
                      <span className="hint">
                        price this contest — entry cost and payout curve set here
                      </span>
                    </div>
                    <NflContestSimulatorPanel batch={contestBatch} />
                  </>
                ) : null
              }
            />
          )}
        </>
      )}
    </div>
  )
}
