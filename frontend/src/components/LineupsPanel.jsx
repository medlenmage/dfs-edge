import { useState } from 'react'
import { api } from '../api'
import { LineupsTable } from './LineupsTable'

/**
 * Generates one optimal DraftKings Classic MLB lineup from whatever
 * salary + projections CSVs are loaded for the date.
 */
export function LineupsPanel({ date }) {
  const [state, setState] = useState({ status: 'idle' })
  const [minStack, setMinStack] = useState('')

  async function run() {
    setState({ status: 'loading' })
    try {
      const stack = minStack.trim() ? Number(minStack) : null
      const lineup = await api.generateLineup(date, { minStack: stack })
      setState({ status: 'ready', lineup })
    } catch (err) {
      setState({ status: 'error', message: err.message })
    }
  }

  return (
    <div className="card">
      <div className="controls" style={{ marginBottom: 14 }}>
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
        <button className="primary" onClick={run} disabled={state.status === 'loading'}>
          {state.status === 'loading' ? 'Solving…' : 'Generate lineup'}
        </button>
      </div>

      {state.status === 'idle' && (
        <p style={{ marginTop: 0, color: 'var(--text-secondary)' }}>
          Builds the single highest-projected lineup that fits DraftKings'
          $50,000 salary cap and Classic MLB roster, using whatever salary
          and projections CSVs are loaded for this date. Upload both first
          — the optimizer needs a real salary and a real projection for
          every player it considers.
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
          <LineupsTable lineup={state.lineup} />
          <button style={{ marginTop: 12 }} onClick={run}>
            Regenerate
          </button>
        </>
      )}
    </div>
  )
}
