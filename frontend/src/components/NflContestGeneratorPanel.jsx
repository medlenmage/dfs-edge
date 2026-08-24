import { useEffect, useState } from 'react'
import { api } from '../api'

/**
 * NFL contest generator + Monte Carlo simulator -- the NFL sibling of
 * ContestGeneratorPanel.jsx (MLB). Scoped to what nfl_contest.py
 * actually supports: contest type, entries to build, allow duplicates,
 * self-play, field sharpness, min/max salary, max exposure, field size,
 * percent-to-first. No post-hoc reshape, no CSV export yet -- see
 * nfl_contest.py's own module docstring for the full list of what
 * wasn't ported from the MLB version and why.
 *
 * Every entry is built toward a real, weighted NFL stack archetype
 * (also nfl_contest.py's own module docstring) -- not a control here,
 * it's baked into generation. STACK_LABELS below is just how those
 * archetype codes are shown in the entries table.
 */
const STACK_LABELS = {
  qb_naked: 'QB (naked)',
  qb_1: 'QB+1',
  qb_2: 'QB+2',
  qb_3: 'QB+3',
  rb_dst: 'RB+DST',
}

export function NflContestGeneratorPanel({ season, week }) {
  const [contestTypes, setContestTypes] = useState({})
  const [contestType, setContestType] = useState('gpp_small')
  const [numLineups, setNumLineups] = useState(20)
  const [allowDuplicates, setAllowDuplicates] = useState(false)
  const [selfPlay, setSelfPlay] = useState(false)
  const [fieldSharpness, setFieldSharpness] = useState('marquee')
  const [fieldSizeOverride, setFieldSizeOverride] = useState('')
  const [maxExposure, setMaxExposure] = useState('')
  const [minSalary, setMinSalary] = useState('')
  const [maxSalary, setMaxSalary] = useState('')
  const [firstPlacePct, setFirstPlacePct] = useState('')
  const [state, setState] = useState({ status: 'idle' })

  useEffect(() => {
    api.nflContestTypes().then((r) => setContestTypes(r.contest_types)).catch(() => {})
  }, [])

  const preset = contestTypes[contestType]

  async function run() {
    setState({ status: 'loading' })
    try {
      const result = await api.nflBuildContestEntriesSimulated(season, week, {
        contestType,
        numLineups,
        maxExposurePct: maxExposure.trim() ? Number(maxExposure) : null,
        fieldSize: fieldSizeOverride.trim() ? Number(fieldSizeOverride) : null,
        minSalary: minSalary.trim() ? Number(minSalary) : 0,
        maxSalary: maxSalary.trim() ? Number(maxSalary) : 50000,
        allowDuplicates,
        selfPlay,
        fieldSharpness,
        firstPlacePct: firstPlacePct.trim() ? Number(firstPlacePct) : null,
      })
      setState({ status: 'ready', ...result })
    } catch (err) {
      setState({ status: 'error', message: err.message })
    }
  }

  return (
    <div className="card">
      <div className="controls" style={{ marginBottom: 14, flexWrap: 'wrap' }}>
        <label className="dim" style={{ fontSize: 13 }}>
          Contest{' '}
          <select value={contestType} onChange={(e) => setContestType(e.target.value)}>
            {Object.entries(contestTypes).map(([key, c]) => (
              <option key={key} value={key}>
                {c.label}
              </option>
            ))}
          </select>
        </label>
        <label className="dim" style={{ fontSize: 13 }}>
          Entries to build{' '}
          <input
            type="number"
            min="1"
            max="10000"
            value={numLineups}
            onChange={(e) => setNumLineups(Math.max(1, Number(e.target.value) || 1))}
            style={{ width: 80 }}
          />
        </label>
        <label className="dim" style={{ fontSize: 13 }}>
          Field size{' '}
          <input
            type="number"
            min="1"
            max="200000"
            placeholder={preset ? String(preset.field_size) : '—'}
            value={fieldSizeOverride}
            onChange={(e) => setFieldSizeOverride(e.target.value)}
            style={{ width: 90 }}
          />
        </label>
        <label className="dim" style={{ fontSize: 13 }}>
          Field sharpness{' '}
          <select value={fieldSharpness} onChange={(e) => setFieldSharpness(e.target.value)} disabled={selfPlay}>
            <option value="low">Low</option>
            <option value="marquee">Marquee</option>
            <option value="high">High</option>
          </select>
        </label>
        <label className="dim" style={{ fontSize: 13 }}>
          % to first{' '}
          <input
            type="number"
            min="1"
            max="100"
            placeholder={preset?.first_place_pct != null ? String(preset.first_place_pct) : 'flat'}
            value={firstPlacePct}
            onChange={(e) => setFirstPlacePct(e.target.value)}
            style={{ width: 60 }}
          />
          %
        </label>
        <label className="dim" style={{ fontSize: 13 }}>
          Max exposure{' '}
          <input
            type="number"
            min="1"
            max="100"
            placeholder="none"
            value={maxExposure}
            onChange={(e) => setMaxExposure(e.target.value)}
            style={{ width: 55 }}
          />
          %
        </label>
        <label className="dim" style={{ fontSize: 13 }}>
          Min salary{' '}
          <input
            type="number"
            min="0"
            max="50000"
            step="500"
            placeholder="—"
            value={minSalary}
            onChange={(e) => setMinSalary(e.target.value)}
            style={{ width: 80 }}
          />
        </label>
        <label className="dim" style={{ fontSize: 13 }}>
          Max salary{' '}
          <input
            type="number"
            min="0"
            max="50000"
            step="500"
            placeholder="50000"
            value={maxSalary}
            onChange={(e) => setMaxSalary(e.target.value)}
            style={{ width: 80 }}
          />
        </label>
        <label className="dim" style={{ fontSize: 13, display: 'flex', alignItems: 'center', gap: 4 }}>
          <input type="checkbox" checked={allowDuplicates} onChange={(e) => setAllowDuplicates(e.target.checked)} />
          Allow dupes
        </label>
        <label
          className="dim"
          style={{ fontSize: 13, display: 'flex', alignItems: 'center', gap: 4 }}
          title="Rank this batch against ITSELF instead of a separately-sampled public field."
        >
          <input type="checkbox" checked={selfPlay} onChange={(e) => setSelfPlay(e.target.checked)} />
          Self play
        </label>
        <button className="primary" onClick={run} disabled={state.status === 'loading'}>
          {state.status === 'loading' ? 'Simulating…' : `Build ${numLineups} entries`}
        </button>
      </div>

      {state.status === 'idle' && (
        <p style={{ marginTop: 0, color: 'var(--text-secondary)' }}>
          Builds a batch of your own entries (randomized construction weighted toward
          projected points, distinct by default) and runs a real Monte Carlo simulation
          against real 2025 player/DST outcome pools -- cash probability and expected
          payout are genuine simulated probabilities, not a single projected-points
          estimate. Requires both a DK salary CSV and RotoWire projections loaded for the
          week.
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
          <div style={{ display: 'flex', gap: 16, marginBottom: 14, flexWrap: 'wrap' }}>
            <span className="badge">{state.num_entries_built} entries built</span>
            <span className="badge">{state.field_size.toLocaleString()}-entry field</span>
            <span className="badge">${state.prize_pool.toLocaleString()} prize pool</span>
            <span className="badge">{state.summary.avg_cash_probability_pct}% avg cash rate</span>
            <span className={`badge ${state.summary.avg_roi_pct >= 0 ? 'ok' : ''}`}>
              {state.summary.avg_roi_pct}% avg ROI
            </span>
            <span className="badge" title="What a zero-skill random entry should expect from this exact contest">
              baseline: {state.field_baseline.avg_cash_probability_pct}% cash / {state.field_baseline.avg_roi_pct}% ROI
            </span>
          </div>

          <div className="notice" style={{ marginBottom: 14 }}>{state.note}</div>

          <div className="card table-wrap" style={{ marginBottom: 16 }}>
            <div className="sub-line" style={{ marginBottom: 8 }}>
              Entries (top {state.results.length})
            </div>
            <table>
              <thead>
                <tr>
                  <th>#</th>
                  <th>Stack</th>
                  <th>Bring-back</th>
                  <th className="num">Salary</th>
                  <th className="num">Proj FPTS</th>
                  <th className="num">Cash%</th>
                  <th className="num">Exp. payout</th>
                  <th className="num">ROI%</th>
                  <th className="num">Sim floor / ceiling</th>
                </tr>
              </thead>
              <tbody>
                {state.results.map((r, i) => {
                  const entry = state.entries[i]
                  return (
                    <tr key={i}>
                      <td>{i + 1}</td>
                      <td className="dim">{STACK_LABELS[entry.primary_stack] || entry.primary_stack || '—'}</td>
                      <td className={entry.has_bringback ? 'ok' : 'dim'}>{entry.has_bringback ? 'Yes' : 'No'}</td>
                      <td className="num">${entry.salary_used.toLocaleString()}</td>
                      <td className="num">{entry.projected_points.toFixed(1)}</td>
                      <td className="num">{r.cash_probability_pct}%</td>
                      <td className="num">${r.expected_payout.toFixed(2)}</td>
                      <td className={`num ${r.roi_pct >= 0 ? 'ok' : ''}`}>{r.roi_pct}%</td>
                      <td className="num">
                        {r.simulated_points_floor.toFixed(1)} / {r.simulated_points_ceiling.toFixed(1)}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>

          {state.exposure.length > 0 && (
            <div className="card table-wrap">
              <div className="sub-line" style={{ marginBottom: 8 }}>
                Exposure across {state.num_entries_built} entries
              </div>
              <table>
                <thead>
                  <tr>
                    <th>Player</th>
                    <th>Team</th>
                    <th className="num">Count</th>
                    <th className="num">Exposure</th>
                    <th className="num">Avg ROI%</th>
                  </tr>
                </thead>
                <tbody>
                  {state.exposure.map((e) => (
                    <tr key={e.id}>
                      <td>{e.name}</td>
                      <td className="dim">{e.team}</td>
                      <td className="num">{e.count}</td>
                      <td className="num">{e.pct}%</td>
                      <td className={`num ${e.avg_roi_pct >= 0 ? 'ok' : ''}`}>{e.avg_roi_pct}%</td>
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
