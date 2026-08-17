import { useEffect, useState } from 'react'
import { api } from '../api'

/**
 * Tests already-generated lineup(s) against a synthetic public contest
 * field, sampled by RotoWire ownership% rather than re-running the
 * optimizer -- a real field is skewed toward whatever's popular, not a
 * pile of point-maximizing builds. Not a lineup simulator: there's no
 * player-outcome variance model yet, so ranks/payouts here are against
 * the field's *projected* points, not a distribution of real-world
 * outcomes -- still useful for chalk exposure and roughly where a
 * build would land, which is why the optimizer's own lineups tend to
 * show up near the top on this measure (they're maximizing the exact
 * number being ranked on; the field isn't).
 */
export function ContestFieldPanel({ date, lineups, includedGamePks }) {
  const [contestTypes, setContestTypes] = useState(null)
  const [contestType, setContestType] = useState('gpp_large')
  const [fieldSize, setFieldSize] = useState('')
  const [state, setState] = useState({ status: 'idle' })

  useEffect(() => {
    api
      .contestTypes()
      .then((d) => setContestTypes(d.contest_types))
      .catch(() => {})
  }, [])

  async function run() {
    setState({ status: 'loading' })
    try {
      const result = await api.buildContestField(date, contestType, lineups, {
        fieldSize: fieldSize.trim() ? Number(fieldSize) : null,
        includedGamePks,
      })
      setState({ status: 'ready', ...result })
    } catch (err) {
      setState({ status: 'error', message: err.message })
    }
  }

  if (!lineups?.length) return null

  const preset = contestTypes?.[contestType]

  return (
    <div className="card" style={{ marginTop: 16 }}>
      <div className="sub-line" style={{ marginBottom: 8 }}>
        Contest field — test {lineups.length > 1 ? 'these lineups' : 'this lineup'} against a
        synthetic public field sampled by RotoWire ownership%
      </div>
      <div className="controls" style={{ marginBottom: 12, flexWrap: 'wrap' }}>
        <label className="dim" style={{ fontSize: 13 }}>
          Contest{' '}
          <select value={contestType} onChange={(e) => setContestType(e.target.value)}>
            {contestTypes &&
              Object.entries(contestTypes).map(([key, c]) => (
                <option key={key} value={key}>
                  {c.label}
                </option>
              ))}
          </select>
        </label>
        <label className="dim" style={{ fontSize: 13 }}>
          Field size{' '}
          <input
            type="number"
            min="1"
            placeholder={preset ? preset.field_size.toLocaleString() : '—'}
            value={fieldSize}
            onChange={(e) => setFieldSize(e.target.value)}
            style={{ width: 90 }}
          />
        </label>
        <button className="primary" onClick={run} disabled={state.status === 'loading'}>
          {state.status === 'loading' ? 'Sampling…' : 'Test vs. field'}
        </button>
      </div>

      {state.status === 'error' && <div className="notice error">{state.message}</div>}

      {state.status === 'ready' && (
        <>
          <div className="controls" style={{ marginBottom: 12, flexWrap: 'wrap' }}>
            <span className="badge">{state.field_size.toLocaleString()}-entry field</span>
            <span className="badge">${state.entry_fee.toLocaleString()} entry</span>
            <span className="badge">{state.paid_count.toLocaleString()} paid</span>
            <span className="badge">${state.prize_pool.toLocaleString()} prize pool</span>
            <span className="badge">sampled {state.sample_size.toLocaleString()} field lineups</span>
          </div>

          <div className="card table-wrap" style={{ marginBottom: 14 }}>
            <table>
              <thead>
                <tr>
                  <th>Lineup</th>
                  <th className="num">Proj FPTS</th>
                  <th className="num">Percentile</th>
                  <th className="num">Est. rank</th>
                  <th>Cashing?</th>
                  <th className="num">Est. payout</th>
                  <th className="num">Est. profit</th>
                </tr>
              </thead>
              <tbody>
                {state.results.map((r) => (
                  <tr key={r.lineup_index}>
                    <td className="name">Lineup {r.lineup_index + 1}</td>
                    <td className="num">{r.projected_points.toFixed(1)}</td>
                    <td className="num">{r.percentile.toFixed(1)}%</td>
                    <td className="num">{r.estimated_rank.toLocaleString()}</td>
                    <td>
                      {r.in_the_money ? (
                        <span className="badge ok">cashes</span>
                      ) : (
                        <span className="badge risk">misses</span>
                      )}
                    </td>
                    <td className="num">${r.estimated_payout.toFixed(2)}</td>
                    <td className="num">
                      {r.estimated_profit >= 0 ? '+' : ''}
                      {r.estimated_profit.toFixed(2)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="sub-line" style={{ marginBottom: 8 }}>
            Field ownership — avg {state.field_ownership.avg_total_ownership_pct.toFixed(1)}%
            (range {state.field_ownership.min_total_ownership_pct.toFixed(1)}%–
            {state.field_ownership.max_total_ownership_pct.toFixed(1)}%)
          </div>

          {state.field_exposure.length > 0 && (
            <div className="card table-wrap" style={{ marginBottom: 8 }}>
              <table>
                <thead>
                  <tr>
                    <th>Field chalk</th>
                    <th>Team</th>
                    <th className="num">Field %</th>
                  </tr>
                </thead>
                <tbody>
                  {state.field_exposure.map((e) => (
                    <tr key={e.id}>
                      <td>{e.name}</td>
                      <td className="dim">{e.team}</td>
                      <td className="num">{e.pct}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="dim" style={{ fontSize: 12 }}>
            {state.note}
          </div>
        </>
      )}
    </div>
  )
}
