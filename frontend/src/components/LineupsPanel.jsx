import { useMemo, useState } from 'react'
import { api } from '../api'
import { LineupsTable } from './LineupsTable'
import { PlayerPoolTable } from './PlayerPoolTable'

// Named stack shapes, largest group first. Shapes that sum to 8 use
// every hitter slot; "4-2" and "3-3" leave 2 hitters as free picks.
const STACK_SHAPES = {
  'no stack': [],
  '5-3': [5, 3],
  '5-2-1': [5, 2, 1],
  '4-4': [4, 4],
  '4-3-1': [4, 3, 1],
  '4-2-2': [4, 2, 2],
  '4-2': [4, 2],
  '3-3-2': [3, 3, 2],
  '3-3': [3, 3],
}

function teamsOnSlate(slate) {
  const teams = new Set()
  for (const g of slate?.games || []) {
    if (g.home?.abbrev) teams.add(g.home.abbrev)
    if (g.away?.abbrev) teams.add(g.away.abbrev)
  }
  return [...teams].sort()
}

const ROSTER_SLOTS = ['P', 'C', '1B', '2B', '3B', 'SS', 'OF']

/**
 * Generates one or many optimal DraftKings Classic MLB lineups from
 * whatever salary + projections CSVs are loaded for the date.
 */
export function LineupsPanel({ date, slate }) {
  const [state, setState] = useState({ status: 'idle' })
  const [numLineups, setNumLineups] = useState(1)
  const [stackShape, setStackShape] = useState('no stack')
  const [stackTeams, setStackTeams] = useState([])
  const [maxExposure, setMaxExposure] = useState('')
  const [selected, setSelected] = useState(0)
  const [locked, setLocked] = useState(new Set())
  const [excluded, setExcluded] = useState(new Set())
  const [showPool, setShowPool] = useState(false)
  const [showExposure, setShowExposure] = useState(false)
  const [slotExposure, setSlotExposure] = useState({})
  const [teamExposure, setTeamExposure] = useState([])
  const [newTeamCap, setNewTeamCap] = useState({ team: '', pct: '' })

  const teams = useMemo(() => teamsOnSlate(slate), [slate])
  const groups = STACK_SHAPES[stackShape]

  function setSlotCap(slot, value) {
    setSlotExposure((prev) => {
      const next = { ...prev }
      if (value.trim()) next[slot] = value
      else delete next[slot]
      return next
    })
  }

  function addTeamCap() {
    if (!newTeamCap.team || !newTeamCap.pct.trim()) return
    setTeamExposure((prev) => [
      ...prev.filter((e) => e.team !== newTeamCap.team),
      { team: newTeamCap.team, pct: newTeamCap.pct },
    ])
    setNewTeamCap({ team: '', pct: '' })
  }

  function removeTeamCap(team) {
    setTeamExposure((prev) => prev.filter((e) => e.team !== team))
  }

  function toggleLock(id) {
    setLocked((prev) => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
    setExcluded((prev) => {
      if (!prev.has(id)) return prev
      const next = new Set(prev)
      next.delete(id)
      return next
    })
  }

  function toggleExclude(id) {
    setExcluded((prev) => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
    setLocked((prev) => {
      if (!prev.has(id)) return prev
      const next = new Set(prev)
      next.delete(id)
      return next
    })
  }

  function changeShape(shape) {
    setStackShape(shape)
    setStackTeams(new Array(STACK_SHAPES[shape].length).fill(''))
  }

  function changeStackTeam(i, team) {
    setStackTeams((prev) => {
      const next = [...prev]
      next[i] = team
      return next
    })
  }

  async function run() {
    setState({ status: 'loading' })
    try {
      const result = await api.generateLineups(date, {
        numLineups,
        stackGroups: groups.length ? groups : null,
        stackTeams: groups.length ? stackTeams.map((t) => t || null) : null,
        maxExposurePct: maxExposure.trim() ? Number(maxExposure) : null,
        exposureBySlot: Object.keys(slotExposure).length
          ? Object.fromEntries(Object.entries(slotExposure).map(([k, v]) => [k, Number(v)]))
          : null,
        teamExposureCap: teamExposure.length
          ? Object.fromEntries(teamExposure.map((e) => [e.team, Number(e.pct)]))
          : null,
        lockedIds: locked.size ? [...locked] : null,
        excludedIds: excluded.size ? [...excluded] : null,
      })
      setSelected(0)
      setState({ status: 'ready', ...result })
    } catch (err) {
      setState({ status: 'error', message: err.message })
    }
  }

  return (
    <div className="card">
      <div className="controls" style={{ marginBottom: 14, flexWrap: 'wrap' }}>
        <label className="dim" style={{ fontSize: 13 }}>
          Lineups{' '}
          <input
            type="number"
            min="1"
            max="150"
            value={numLineups}
            onChange={(e) => setNumLineups(Math.max(1, Number(e.target.value) || 1))}
            style={{ width: 60 }}
          />
        </label>
        <label className="dim" style={{ fontSize: 13 }}>
          Stack{' '}
          <select value={stackShape} onChange={(e) => changeShape(e.target.value)}>
            {Object.keys(STACK_SHAPES).map((shape) => (
              <option key={shape} value={shape}>
                {shape}
              </option>
            ))}
          </select>
        </label>
        <label className="dim" style={{ fontSize: 13 }}>
          Max exposure{' '}
          <select value={maxExposure} onChange={(e) => setMaxExposure(e.target.value)}>
            <option value="">none</option>
            {[20, 30, 40, 50, 75].map((n) => (
              <option key={n} value={n}>
                {n}%
              </option>
            ))}
          </select>
        </label>
        <button className="primary" onClick={run} disabled={state.status === 'loading'}>
          {state.status === 'loading'
            ? 'Solving…'
            : `Generate ${numLineups > 1 ? `${numLineups} lineups` : 'lineup'}`}
        </button>
        <button onClick={() => setShowPool((v) => !v)}>
          {showPool ? 'Hide player pool' : 'Player pool'}
          {locked.size + excluded.size > 0 ? ` (${locked.size + excluded.size})` : ''}
        </button>
        <button onClick={() => setShowExposure((v) => !v)}>
          {showExposure ? 'Hide exposure limits' : 'Exposure limits'}
          {Object.keys(slotExposure).length + teamExposure.length > 0
            ? ` (${Object.keys(slotExposure).length + teamExposure.length})`
            : ''}
        </button>
      </div>

      {showPool && (
        <div style={{ marginBottom: 14 }}>
          <PlayerPoolTable
            slate={slate}
            locked={locked}
            excluded={excluded}
            onToggleLock={toggleLock}
            onToggleExclude={toggleExclude}
          />
        </div>
      )}

      {showExposure && (
        <div className="card" style={{ marginBottom: 14 }}>
          <div className="sub-line" style={{ marginBottom: 8 }}>
            Position exposure — overrides the general max exposure for specific slots
          </div>
          <div className="controls" style={{ marginBottom: 14, flexWrap: 'wrap' }}>
            {ROSTER_SLOTS.map((slot) => (
              <label key={slot} className="dim" style={{ fontSize: 13 }}>
                {slot}{' '}
                <input
                  type="number"
                  min="1"
                  max="100"
                  placeholder="—"
                  value={slotExposure[slot] || ''}
                  onChange={(e) => setSlotCap(slot, e.target.value)}
                  style={{ width: 55 }}
                />
                %
              </label>
            ))}
          </div>

          {groups.length > 0 && (
            <>
              <div className="sub-line" style={{ marginBottom: 8 }}>
                Team exposure — caps how often a team is used AS THE STACK (auto-assigned groups only)
              </div>
              <div className="controls" style={{ marginBottom: 8, flexWrap: 'wrap' }}>
                <select
                  value={newTeamCap.team}
                  onChange={(e) => setNewTeamCap((prev) => ({ ...prev, team: e.target.value }))}
                >
                  <option value="">Team…</option>
                  {teams.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>
                <input
                  type="number"
                  min="1"
                  max="100"
                  placeholder="%"
                  value={newTeamCap.pct}
                  onChange={(e) => setNewTeamCap((prev) => ({ ...prev, pct: e.target.value }))}
                  style={{ width: 60 }}
                />
                <button onClick={addTeamCap}>Add</button>
              </div>
              {teamExposure.length > 0 && (
                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                  {teamExposure.map((e) => (
                    <span key={e.team} className="badge">
                      {e.team} ≤ {e.pct}%{' '}
                      <button
                        className="pill-toggle"
                        style={{ padding: '0 4px', border: 'none' }}
                        onClick={() => removeTeamCap(e.team)}
                      >
                        ✕
                      </button>
                    </span>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      )}

      {groups.length > 0 && (
        <div className="controls" style={{ marginBottom: 14, flexWrap: 'wrap' }}>
          {groups.map((size, i) => (
            <label key={i} className="dim" style={{ fontSize: 13 }}>
              {size}-stack team{' '}
              <select value={stackTeams[i] || ''} onChange={(e) => changeStackTeam(i, e.target.value)}>
                <option value="">Auto</option>
                {teams.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            </label>
          ))}
        </div>
      )}

      {state.status === 'idle' && (
        <p style={{ marginTop: 0, color: 'var(--text-secondary)' }}>
          Builds distinct lineups that each fit DraftKings' $50,000 salary
          cap and Classic MLB roster, using whatever salary and
          projections CSVs are loaded for this date. Upload both first —
          the optimizer needs a real salary and a real projection for
          every player it considers. A stack shape like "4-2-2" forces at
          least that many hitters onto each of that many distinct teams
          — leave a group on Auto to let the solver pick the best team
          for it, or choose one yourself. Shapes that use all 8 hitters
          (5-3, 4-4, etc.) come out exact since there's no room left
          over; partial shapes (4-2, 3-3) leave 2 free, which can land
          anywhere — including padding one of the stacks further. Asking
          for more
          than one lineup forces each to differ from the ones before it;
          a max exposure cap keeps any one player from showing up in too
          many of them. Open the player pool to lock someone into every
          lineup or exclude them entirely.
        </p>
      )}

      {state.status === 'loading' && (
        <div>
          <div className="skeleton" style={{ width: '70%', marginBottom: 10 }} />
          <div className="skeleton" style={{ width: '85%', marginBottom: 10 }} />
          <div className="skeleton" style={{ width: '60%' }} />
        </div>
      )}

      {state.status === 'error' && (
        <>
          <div className="notice error">{state.message}</div>
          <button style={{ marginTop: 12 }} onClick={run}>
            Try again
          </button>
        </>
      )}

      {state.status === 'ready' && (
        <>
          {state.lineups.length < numLineups && (
            <div className="notice" style={{ marginBottom: 12 }}>
              Only built {state.lineups.length} of {numLineups} requested — the pool ran out of
              room for more distinct lineups under the current constraints.
            </div>
          )}

          {state.lineups.length > 1 && (
            <div className="controls" style={{ marginBottom: 12 }}>
              <button onClick={() => setSelected((i) => Math.max(0, i - 1))} disabled={selected === 0}>
                ← Prev
              </button>
              <span className="dim" style={{ fontSize: 13 }}>
                Lineup {selected + 1} of {state.lineups.length}
              </span>
              <button
                onClick={() => setSelected((i) => Math.min(state.lineups.length - 1, i + 1))}
                disabled={selected === state.lineups.length - 1}
              >
                Next →
              </button>
            </div>
          )}

          <LineupsTable lineup={state.lineups[selected]} />

          {state.exposure.length > 0 && (
            <div className="card table-wrap" style={{ marginTop: 16 }}>
              <div className="sub-line" style={{ marginBottom: 8 }}>
                Exposure across {state.lineups.length} lineup{state.lineups.length > 1 ? 's' : ''}
              </div>
              <table>
                <thead>
                  <tr>
                    <th>Player</th>
                    <th>Team</th>
                    <th className="num">Lineups</th>
                    <th className="num">Exposure</th>
                  </tr>
                </thead>
                <tbody>
                  {state.exposure.map((e) => (
                    <tr key={e.id}>
                      <td>{e.name}</td>
                      <td className="dim">{e.team}</td>
                      <td className="num">{e.count}</td>
                      <td className="num">{e.pct}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {state.team_exposure?.length > 0 && (
            <div className="card table-wrap" style={{ marginTop: 16 }}>
              <div className="sub-line" style={{ marginBottom: 8 }}>
                Team stack usage across {state.lineups.length} lineup
                {state.lineups.length > 1 ? 's' : ''}
              </div>
              <table>
                <thead>
                  <tr>
                    <th>Team</th>
                    <th className="num">Lineups stacked</th>
                    <th className="num">Exposure</th>
                  </tr>
                </thead>
                <tbody>
                  {state.team_exposure.map((e) => (
                    <tr key={e.team}>
                      <td className="name">{e.team}</td>
                      <td className="num">{e.count}</td>
                      <td className="num">{e.pct}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <button style={{ marginTop: 12 }} onClick={run}>
            Regenerate
          </button>
        </>
      )}
    </div>
  )
}
