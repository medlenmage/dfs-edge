import { useEffect, useRef, useState } from 'react'
import { api } from '../api'

/**
 * The NFL contest generator: builds a whole DraftKings Classic NFL
 * contest -- lineups, and nothing else. Building lineups and pricing
 * them are still different questions, and the inputs that answer the
 * second one (entry cost, payout curve) have nothing to do with how the
 * lineups get built -- so the simulator remains its own component.
 *
 * It is no longer its own TAB, though. It renders through the
 * `simulator` slot, between the build summary and the detail tables,
 * the same way MLB's BuildWorkspace stacks the two: the batch is handed
 * over the moment it's built rather than after a click that also
 * navigates you away, and pricing sits above the tables you scroll
 * rather than below them.
 *
 * No salary, exposure or duplicate knobs either: every entry is built
 * toward spending the cap (nfl_contest._SALARY_PACING_STRENGTH) because
 * unspent salary is unspent projected points, and duplicates are always
 * allowed because a real contest field contains them.
 *
 * Contest size is ONE control, not two -- this builds a CONTEST, so the
 * field size and the number of entries in it are the same number,
 * picked from the real sizes the selected contest type comes in.
 *
 * Every entry is built toward a real, weighted NFL stack archetype (see
 * nfl_contest.py's module docstring) -- not a control here, it's baked
 * into generation. STACK_LABELS is just how those codes are displayed.
 */
const STACK_LABELS = {
  qb_naked: 'QB (naked)',
  qb_1: 'QB+1',
  qb_2: 'QB+2',
  qb_3: 'QB+3',
  rb_dst: 'RB+DST',
  none: 'no stack',
}

// e.g. "DAL(primary), WSH + GB(secondary)" -- primary_team/secondary_teams
// are real facts from generation time (nfl_contest.py), not re-derived
// from roster composition.
function stackTeamsLabel(entry) {
  const primary = entry.primary_team
  const secondary = entry.secondary_teams || []
  if (!primary) return '—'
  const parts = [`${primary}(primary)`]
  if (secondary.length) parts.push(`${secondary.join(' + ')}(secondary)`)
  return parts.join(', ')
}

export function NflContestGeneratorPanel({ season, week, onBuilt, simulator }) {
  const [contestTypes, setContestTypes] = useState({})
  const [contestType, setContestType] = useState('gpp_small')
  const [contestSize, setContestSize] = useState(500)
  const [state, setState] = useState({ status: 'idle' })
  // Bumped by Re-roll. At 0, identical settings reproduce the identical
  // contest -- the backend derives a deterministic seed from them, so
  // the table no longer reshuffles on every click for no reason.
  const [reroll, setReroll] = useState(0)
  const simulatorRef = useRef(null)

  useEffect(() => {
    api.nflContestTypes().then((r) => setContestTypes(r.contest_types)).catch(() => {})
  }, [])

  const preset = contestTypes[contestType]
  const sizes = preset?.sizes || []

  // Every contest type comes in its own set of real sizes, so switching
  // type has to land on one this type actually offers.
  useEffect(() => {
    if (!sizes.length) return
    if (!sizes.includes(contestSize)) setContestSize(preset.field_size ?? sizes[sizes.length - 1])
  }, [contestType, contestTypes])

  async function run(rerollOverride = null) {
    setState({ status: 'loading' })
    try {
      const result = await api.nflBuildContestEntries(season, week, {
        contestType,
        contestSize,
        reroll: rerollOverride ?? reroll,
      })
      setState({ status: 'ready', ...result })
      // Hand it straight over. The old flow needed a click on
      // "Simulate this contest" purely to move the batch across a tab
      // boundary that no longer exists.
      onBuilt?.(result)
    } catch (err) {
      setState({ status: 'error', message: err.message })
    }
  }

  const sizeLabel = (n) => (n >= 1000 && n % 1000 === 0 ? `${n / 1000}K` : n.toLocaleString())
  const sampled = state.status === 'ready' && state.num_entries_built < state.field_size

  return (
    <>
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
        <label
          className="dim"
          style={{ fontSize: 13 }}
          title="How big the contest is -- and therefore how many lineups get built. One number, not two: this builds a contest, so the field size and the number of entries in it are the same thing. The options are the real sizes this contest type actually comes in."
        >
          Contest size{' '}
          <select value={contestSize} onChange={(e) => setContestSize(Number(e.target.value))}>
            {sizes.map((n) => (
              <option key={n} value={n}>
                {sizeLabel(n)}
              </option>
            ))}
          </select>
        </label>
        <button className="primary" onClick={() => run()} disabled={state.status === 'loading'}>
          {state.status === 'loading' ? 'Building…' : `Build ${sizeLabel(contestSize)} contest`}
        </button>
        {state.status === 'ready' && (
          <button
            onClick={() => {
              const next = reroll + 1
              setReroll(next)
              run(next)
            }}
            title="Identical settings always reproduce the identical contest (a deterministic seed), so results are stable click to click. Re-roll draws a genuinely new one for the same settings."
          >
            Re-roll
          </button>
        )}
      </div>

      {state.status === 'idle' && (
        <p style={{ marginTop: 0, color: 'var(--text-secondary)' }}>
          Builds an entire DraftKings Classic NFL contest in one shot — every lineup
          individually strong (weighted heavily toward projected points, and toward actually
          spending the salary cap), across the real NFL GPP stack archetypes, duplicates
          included the way a real field produces them. No economics until it&apos;s built —
          then the simulator appears right below with cash probability, payouts and ROI.
          Requires both a DK salary CSV and RotoWire projections loaded for the week.
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
          <button style={{ marginTop: 12 }} onClick={() => run()}>
            Try again
          </button>
        </>
      )}

      {state.status === 'ready' && (
        <>
          {sampled && (
            <div className="notice" style={{ marginBottom: 12 }}>
              {state.num_entries_built.toLocaleString()} lineups built for a{' '}
              {state.field_size.toLocaleString()}-entry contest — building is capped, so this
              batch stands in for the full field. Every payout, rank and ROI the simulator
              produces still keys off the real {state.field_size.toLocaleString()}-entry size.
            </div>
          )}
          {state.num_distinct_entries < state.num_entries_built && (
            <div className="notice" style={{ marginBottom: 12 }}>
              {state.num_distinct_entries.toLocaleString()} distinct builds +{' '}
              {(state.num_entries_built - state.num_distinct_entries).toLocaleString()} duplicates
              — this slate&apos;s pool can&apos;t support {state.num_entries_built.toLocaleString()}{' '}
              unique lineups, so the contest fills out with duplicates the way a real field does.
            </div>
          )}

          <div className="controls" style={{ marginBottom: 12, flexWrap: 'wrap' }}>
            <span className="badge ok">
              {state.num_entries_built.toLocaleString()} lineups built
            </span>
            <span className="badge">{state.field_size.toLocaleString()}-entry contest</span>
            <span className="badge">{state.num_distinct_entries.toLocaleString()} distinct</span>
            <span
              className="badge"
              title="Median salary actually used. Every entry is built toward spending the cap -- there's no floor rejecting cheap builds, the sampler is steered toward salary as it goes."
            >
              ${state.summary.median_salary_used.toLocaleString()} median salary
            </span>
            <span className="badge">
              ${state.summary.min_salary_used.toLocaleString()}–$
              {state.summary.max_salary_used.toLocaleString()} range
            </span>
            <span className="badge">
              {state.summary.avg_projected_points.toFixed(1)} avg proj FPTS
            </span>
            <span className="badge">
              {state.summary.avg_total_ownership_pct.toFixed(1)}% avg ownership
            </span>
          </div>

          <div className="controls" style={{ marginBottom: 0, flexWrap: 'wrap' }}>
            <button
              className="primary"
              onClick={() =>
                simulatorRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
              }
            >
              Simulate this contest ↓
            </button>
            <a
              href={api.nflContestEntriesCsvUrl(state.batch_id)}
              title={`Download all ${state.num_entries_built.toLocaleString()} lineups as a CSV`}
            >
              <button>Download full contest (CSV)</button>
            </a>
            <span className="dim" style={{ fontSize: 12 }}>
              already loaded below — entry cost and payout curve are set there
            </span>
          </div>
        </>
      )}
      </div>

      {/* Pricing sits between the build summary and the detail tables,
          not underneath a page of them. */}
      {state.status === 'ready' && simulator && (
        <div ref={simulatorRef} style={{ margin: '14px 0' }}>{simulator}</div>
      )}

      {state.status === 'ready' && (
        <>
          {state.stack_shapes?.length > 0 && (
            <div className="card table-wrap" style={{ marginBottom: 14 }}>
              <div className="sub-line" style={{ marginBottom: 8 }}>
                Stack archetypes in this contest — the real NFL GPP constructions, in the
                proportions a real field builds them
              </div>
              <table>
                <thead>
                  <tr>
                    <th>Archetype</th>
                    <th className="num">Lineups</th>
                    <th className="num">Share</th>
                  </tr>
                </thead>
                <tbody>
                  {state.stack_shapes.map((s) => (
                    <tr key={s.shape}>
                      <td>{STACK_LABELS[s.shape] || s.shape}</td>
                      <td className="num">{s.count.toLocaleString()}</td>
                      <td className="num">{s.pct}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {state.exposure?.length > 0 && (
            <div className="card table-wrap" style={{ marginBottom: 14 }}>
              <div className="sub-line" style={{ marginBottom: 8 }}>
                Exposure across the contest
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
                      <td className="num">{e.count.toLocaleString()}</td>
                      <td className="num">{e.pct}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {state.sample_entries?.length > 0 && (
            <div className="card table-wrap" style={{ marginBottom: 14 }}>
              <div className="sub-line" style={{ marginBottom: 8 }}>
                Sample lineups — showing {state.sample_entries.length} of{' '}
                {state.num_entries_built.toLocaleString()}
              </div>
              <table>
                <thead>
                  <tr>
                    <th className="num">#</th>
                    <th>Stack</th>
                    <th>Teams</th>
                    <th>Bring-back</th>
                    <th className="num">Salary</th>
                    <th className="num">Proj FPTS</th>
                    <th className="num">Own%</th>
                  </tr>
                </thead>
                <tbody>
                  {state.sample_entries.map((e, i) => (
                    <tr key={i}>
                      <td className="num">{i + 1}</td>
                      <td className="dim">
                        {STACK_LABELS[e.primary_stack] || e.primary_stack || '—'}
                      </td>
                      <td className="dim">{stackTeamsLabel(e)}</td>
                      <td className={e.has_bringback ? 'ok' : 'dim'}>
                        {e.has_bringback ? 'Yes' : 'No'}
                      </td>
                      <td className="num">${e.salary_used.toLocaleString()}</td>
                      <td className="num">{e.projected_points.toFixed(1)}</td>
                      <td className="num">{e.total_ownership_pct.toFixed(1)}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="dim" style={{ fontSize: 12, marginBottom: 12 }}>
            {state.note}
          </div>

          <button onClick={() => run()}>Rebuild</button>
        </>
      )}
    </>
  )
}
