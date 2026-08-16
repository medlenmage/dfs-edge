import { useState } from 'react'
import { api } from '../api'
import { LineupsTable } from './LineupsTable'

/**
 * Generates one or many optimal DraftKings Classic MLB lineups from
 * whatever salary + projections CSVs are loaded for the date.
 */
export function LineupsPanel({ date }) {
  const [state, setState] = useState({ status: 'idle' })
  const [numLineups, setNumLineups] = useState(1)
  const [minStack, setMinStack] = useState('')
  const [maxExposure, setMaxExposure] = useState('')
  const [selected, setSelected] = useState(0)

  async function run() {
    setState({ status: 'loading' })
    try {
      const result = await api.generateLineups(date, {
        numLineups,
        minStack: minStack.trim() ? Number(minStack) : null,
        maxExposurePct: maxExposure.trim() ? Number(maxExposure) : null,
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
          Min stack{' '}
          <select value={minStack} onChange={(e) => setMinStack(e.target.value)}>
            <option value="">none</option>
            {[2, 3, 4, 5].map((n) => (
              <option key={n} value={n}>
                {n}+ hitters, one team
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
      </div>

      {state.status === 'idle' && (
        <p style={{ marginTop: 0, color: 'var(--text-secondary)' }}>
          Builds distinct lineups that each fit DraftKings' $50,000 salary
          cap and Classic MLB roster, using whatever salary and
          projections CSVs are loaded for this date. Upload both first —
          the optimizer needs a real salary and a real projection for
          every player it considers. Asking for more than one lineup
          forces each to differ from the ones before it; a max exposure
          cap keeps any one player from showing up in too many of them —
          useful once you're entering the same slate several times.
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

          <button style={{ marginTop: 12 }} onClick={run}>
            Regenerate
          </button>
        </>
      )}
    </div>
  )
}
