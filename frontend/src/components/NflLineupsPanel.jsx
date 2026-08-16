import { useState } from 'react'
import { api } from '../api'

const SLOT_ORDER = ['QB', 'RB', 'RB', 'WR', 'WR', 'WR', 'TE', 'FLEX', 'DST']
const SALARY_CAP = 50_000

/**
 * Generates one or many optimal DraftKings Classic NFL lineups from
 * whatever salary + projections CSVs are loaded for the week.
 */
export function NflLineupsPanel({ season, week }) {
  const [state, setState] = useState({ status: 'idle' })
  const [numLineups, setNumLineups] = useState(1)
  const [maxExposure, setMaxExposure] = useState('')
  const [minSalary, setMinSalary] = useState('')
  const [minUniquePlayers, setMinUniquePlayers] = useState('')
  const [qbStackMin, setQbStackMin] = useState('0')
  const [selected, setSelected] = useState(0)

  async function run() {
    setState({ status: 'loading' })
    try {
      const result = await api.nflGenerateLineups(season, week, {
        numLineups,
        maxExposurePct: maxExposure.trim() ? Number(maxExposure) : null,
        minSalary: minSalary.trim() ? Number(minSalary) : null,
        minUniquePlayers: minUniquePlayers.trim() ? Number(minUniquePlayers) : 1,
        qbStackMin: Number(qbStackMin),
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
          QB stack{' '}
          <select value={qbStackMin} onChange={(e) => setQbStackMin(e.target.value)}>
            <option value="0">none</option>
            <option value="1">1+ pass-catcher</option>
            <option value="2">2+ pass-catchers</option>
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
        <label className="dim" style={{ fontSize: 13 }}>
          Min salary{' '}
          <input
            type="number"
            min="0"
            max={50000}
            step="500"
            placeholder="—"
            value={minSalary}
            onChange={(e) => setMinSalary(e.target.value)}
            style={{ width: 80 }}
          />
        </label>
        <label className="dim" style={{ fontSize: 13 }}>
          Min unique between lineups{' '}
          <input
            type="number"
            min="1"
            max="9"
            placeholder="1"
            value={minUniquePlayers}
            onChange={(e) => setMinUniquePlayers(e.target.value)}
            style={{ width: 55 }}
          />
        </label>
        <button className="primary" onClick={run} disabled={state.status === 'loading'}>
          {state.status === 'loading'
            ? 'Solving…'
            : `Generate ${numLineups > 1 ? `${numLineups} lineups` : 'lineup'}`}
        </button>
      </div>

      {state.status === 'idle' && (
        <p style={{ marginTop: 0, color: 'var(--text-secondary)' }}>
          Builds distinct lineups that each fit DraftKings' $50,000 salary cap and
          Classic NFL roster (QB, RB, RB, WR, WR, WR, TE, FLEX, DST), using
          whatever salary and projections CSVs are loaded for this week. Upload
          both first. A QB stack forces at least that many of the rostered QB's
          own WR/TEs into the same lineup — the standard NFL GPP correlation
          play. Asking for more than one lineup forces each to differ from the
          ones before it; a max exposure cap keeps any one player from showing
          up in too many of them.
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
          {state.lineups.length === 0 && (
            <div className="notice">No legal lineup could be built with the current pool.</div>
          )}

          {state.lineups.length > 0 && (
            <>
              {state.lineups.length < numLineups && (
                <div className="notice" style={{ marginBottom: 12 }}>
                  Only built {state.lineups.length} of {numLineups} requested — the pool ran out
                  of room for more distinct lineups under the current constraints.
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

              <NflLineupTable lineup={state.lineups[selected]} />

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

              <button style={{ marginTop: 12 }} onClick={run}>
                Regenerate
              </button>
            </>
          )}
        </>
      )}
    </div>
  )
}

function NflLineupTable({ lineup }) {
  const remaining = SALARY_CAP - lineup.salary_used
  const used = {}
  const rows = SLOT_ORDER.map((slotType, i) => {
    const idx = used[slotType] || 0
    used[slotType] = idx + 1
    const player = (lineup.slots[slotType] || [])[idx]
    return { key: `${slotType}-${i}`, slotType, player }
  })

  return (
    <div className="card table-wrap">
      <div style={{ display: 'flex', gap: 16, marginBottom: 12, flexWrap: 'wrap' }}>
        <span className="badge ok">
          ${lineup.salary_used.toLocaleString()} used / ${remaining.toLocaleString()} left
        </span>
        <span className="badge">{lineup.projected_points.toFixed(1)} projected points</span>
        {lineup.total_ownership_pct != null && (
          <span className="badge">{lineup.total_ownership_pct.toFixed(1)}% cumulative ownership</span>
        )}
      </div>
      <table>
        <thead>
          <tr>
            <th>Slot</th>
            <th>Player</th>
            <th>Team</th>
            <th className="num">Salary</th>
            <th className="num">Proj FPTS</th>
            <th className="num">Own%</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key}>
              <td className="name">{r.slotType}</td>
              <td>{r.player?.name || <span className="dim">—</span>}</td>
              <td className="dim">{r.player?.team || '—'}</td>
              <td className="num">{r.player ? `$${r.player.salary.toLocaleString()}` : '—'}</td>
              <td className="num">{r.player ? r.player.projected_fpts.toFixed(1) : '—'}</td>
              <td className="num">
                {r.player?.ownership_pct != null ? `${r.player.ownership_pct.toFixed(1)}%` : '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
