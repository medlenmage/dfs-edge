import { useEffect, useState } from 'react'
import { api } from '../api'

/**
 * The NFL simulator: takes a contest the generator already built and
 * works out what it would actually pay. The NFL sibling of
 * ContestSimulatorPanel.jsx (MLB).
 *
 * Split out of the generator on purpose -- the same built contest can
 * be re-priced under different economics (a different entry cost, a
 * flatter payout curve) without rebuilding a single lineup.
 *
 * Entry cost is the load-bearing input: the prize pool is the contest's
 * size times the entry fee (less rake), so it sets every payout and
 * therefore every ROI on the page.
 */
const STACK_LABELS = {
  qb_naked: 'QB (naked)',
  qb_1: 'QB+1',
  qb_2: 'QB+2',
  qb_3: 'QB+3',
  rb_dst: 'RB+DST',
  none: 'no stack',
}

function stackTeamsLabel(entry) {
  const primary = entry.primary_team
  const secondary = entry.secondary_teams || []
  if (!primary) return '—'
  const parts = [`${primary}(primary)`]
  if (secondary.length) parts.push(`${secondary.join(' + ')}(secondary)`)
  return parts.join(', ')
}

export function NflContestSimulatorPanel({ batch, onOpenGenerator }) {
  const [entryFee, setEntryFee] = useState('')
  const [firstPlacePct, setFirstPlacePct] = useState('')
  const [selfPlay, setSelfPlay] = useState(true)
  const [fieldSharpness, setFieldSharpness] = useState('marquee')
  const [reroll, setReroll] = useState(0)
  const [state, setState] = useState({ status: 'idle' })

  // Entry cost seeds from whatever the built contest's preset assumed,
  // so the first run is sane without typing.
  useEffect(() => {
    if (batch?.contest?.entry_fee != null) setEntryFee(String(batch.contest.entry_fee))
    setState({ status: 'idle' })
  }, [batch?.batch_id])

  async function run(rerollOverride = null) {
    if (!batch?.batch_id) return
    setState({ status: 'loading' })
    try {
      const result = await api.nflSimulateContestBatch(batch.batch_id, {
        entryFee: entryFee.trim() ? Number(entryFee) : null,
        firstPlacePct: firstPlacePct === '' ? null : Number(firstPlacePct),
        selfPlay,
        fieldSharpness,
        reroll: rerollOverride ?? reroll,
      })
      setState({ status: 'ready', ...result })
    } catch (err) {
      setState({ status: 'error', message: err.message })
    }
  }

  const feeNumber = entryFee.trim() ? Number(entryFee) : (batch?.contest?.entry_fee ?? 0)
  const projectedPool = batch && feeNumber ? Math.round(batch.field_size * feeNumber * 0.85) : null

  if (!batch) {
    return (
      <div className="card">
        <p style={{ marginTop: 0, color: 'var(--text-secondary)' }}>
          Nothing to simulate yet. Build a contest in the Contest Generator tab, then hit
          &quot;Simulate this contest&quot; there to hand it over here.
        </p>
        {onOpenGenerator && <button onClick={onOpenGenerator}>Go to Contest Generator</button>}
      </div>
    )
  }

  return (
    <div className="card">
      <div className="controls" style={{ marginBottom: 10, flexWrap: 'wrap' }}>
        <span className="badge ok">{batch.num_entries_built.toLocaleString()} lineups loaded</span>
        <span className="badge">{batch.field_size.toLocaleString()}-entry contest</span>
        <span className="badge">{batch.contest?.label}</span>
      </div>

      <div className="controls" style={{ marginBottom: 14, flexWrap: 'wrap' }}>
        <label
          className="dim"
          style={{ fontSize: 13 }}
          title="What one entry costs. The prize pool is the contest's size times this, less rake -- so this single number sets every payout and every ROI below."
        >
          Entry cost ${' '}
          <input
            type="number"
            min="0"
            step="0.25"
            value={entryFee}
            onChange={(e) => setEntryFee(e.target.value)}
            style={{ width: 80 }}
          />
        </label>
        <label
          className="dim"
          style={{ fontSize: 13 }}
          title="What share of the prize pool 1st place wins. A lower value flattens the payout curve -- more spread across the paid ranks, less concentrated at 1st -- which changes every entry's simulated ROI."
        >
          % to first{' '}
          <select value={firstPlacePct} onChange={(e) => setFirstPlacePct(e.target.value)}>
            <option value="">preset default</option>
            {[5, 10, 15, 20, 25, 30, 35].map((n) => (
              <option key={n} value={n}>
                {n}%
              </option>
            ))}
          </select>
        </label>
        {projectedPool != null && (
          <span
            className="badge"
            title="Contest size x entry cost, less the 15% rake this app models -- what the simulator will pay out across the whole payout curve."
          >
            ~${projectedPool.toLocaleString()} prize pool
          </span>
        )}
        <label
          className="dim"
          style={{ fontSize: 13, display: 'flex', alignItems: 'center', gap: 6 }}
          title="On (default): the generator built the whole contest, so this ranks every lineup in it against every other one in the same simulated trial. Off: rank them against a separately-sampled realistic public field instead."
        >
          <input type="checkbox" checked={selfPlay} onChange={(e) => setSelfPlay(e.target.checked)} />
          This contest vs. itself
        </label>
        {!selfPlay && (
          <label
            className="dim"
            style={{ fontSize: 13 }}
            title="Contest stakes, and so who is in the field. Low: a cheap contest -- newer, safer entrants, the chalkiest lineups. Marquee (default): a milly-maker or other massive field, a mix of both. High: high stakes, where players limit chalk and hunt low-owned plays that have a real matchup edge behind them."
          >
            Field sharpness{' '}
            <select value={fieldSharpness} onChange={(e) => setFieldSharpness(e.target.value)}>
              <option value="low">Low</option>
              <option value="marquee">Marquee</option>
              <option value="high">High</option>
            </select>
          </label>
        )}
        <button className="primary" onClick={() => run()} disabled={state.status === 'loading'}>
          {state.status === 'loading' ? 'Simulating…' : 'Run simulation'}
        </button>
        {state.status === 'ready' && (
          <button
            onClick={() => {
              const next = reroll + 1
              setReroll(next)
              run(next)
            }}
            title="Identical settings reproduce identical draws. Re-roll runs a genuinely new set of simulated trials on the same contest."
          >
            Re-roll
          </button>
        )}
      </div>

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
          <div className="controls" style={{ marginBottom: 12, flexWrap: 'wrap' }}>
            <span className="badge ok">
              {state.num_entries_built.toLocaleString()} lineups simulated
            </span>
            <span className="badge">{state.field_size.toLocaleString()}-entry contest</span>
            <span className="badge">${state.contest.entry_fee.toLocaleString()} entry</span>
            <span className="badge">{state.paid_count.toLocaleString()} paid</span>
            <span className="badge">${state.prize_pool.toLocaleString()} prize pool</span>
            {state.first_place_pct != null && (
              <span className="badge" title="Share of the prize pool 1st place wins in this run's payout curve">
                {state.first_place_pct}% to first
              </span>
            )}
            <span className="badge">{state.num_trials.toLocaleString()} sim trials</span>
            <span
              className="badge"
              title={
                state.self_play
                  ? 'Every lineup in this contest ranked against every other lineup in it, in the same simulated trial.'
                  : 'This contest ranked against a separately-sampled realistic public field.'
              }
            >
              {state.self_play ? 'vs. itself' : 'vs. public field'}
            </span>
            <a
              href={api.nflContestEntriesCsvUrl(state.batch_id)}
              title="Download every simulated lineup as a CSV"
            >
              <button>Download results (CSV)</button>
            </a>
          </div>

          <div className="card" style={{ marginBottom: 14 }}>
            <div className="sub-line" style={{ marginBottom: 8 }}>
              Simulated economics — {state.num_trials.toLocaleString()} Monte Carlo trials of each
              player&apos;s own real historical outcome pool, with QB/pass-catcher and DST/opponent
              correlation.
              {state.self_play
                ? ' Every lineup in this contest is ranked against every other lineup in the same simulated trial.'
                : ' Ranked against a separately-sampled realistic public field.'}
            </div>
            <div className="controls" style={{ flexWrap: 'wrap' }}>
              <span className="badge">
                {state.summary.avg_cash_probability_pct}% avg cash probability
              </span>
              <span className="badge">{state.summary.avg_first_place_pct}% avg 1st place</span>
              <span className="badge">{state.summary.avg_top_1pct_pct}% avg top 1%</span>
              <span className="badge">{state.summary.avg_top_10pct_pct}% avg top 10%</span>
              <span className={`badge ${state.summary.avg_roi_pct >= 0 ? 'ok' : 'risk'}`}>
                {state.summary.avg_roi_pct >= 0 ? '+' : ''}
                {state.summary.avg_roi_pct}% avg ROI
              </span>
              <span className="badge">${state.summary.total_entry_cost.toLocaleString()} cost</span>
              <span
                className="badge"
                title="Every lineup's own average payout, summed. Unlike a single lineup's average -- carried by rare hits -- a sum across the whole contest is stable: it lands on the prize pool by construction."
              >
                ${state.summary.total_expected_payout.toLocaleString()} avg total payout
              </span>
              <span className={`badge ${state.summary.estimated_net_profit >= 0 ? 'ok' : 'risk'}`}>
                {state.summary.estimated_net_profit >= 0 ? '+' : ''}$
                {state.summary.estimated_net_profit.toLocaleString()} est. net
              </span>
            </div>

            {state.field_baseline && (
              <div className="controls" style={{ flexWrap: 'wrap', marginTop: 8 }}>
                <span
                  className="badge"
                  title="What ANY random, zero-skill entry should expect from this exact contest -- a closed-form fact from its entry fee, prize pool and payout%, not a simulation."
                >
                  field baseline: {state.field_baseline.avg_cash_probability_pct}% cash,{' '}
                  {state.field_baseline.avg_roi_pct >= 0 ? '+' : ''}
                  {state.field_baseline.avg_roi_pct}% ROI
                </span>
                {/* "Your edge" only means anything when the entries being
                    averaged are a genuinely DIFFERENT population from the
                    field they're compared against. Ranking a contest
                    against ITSELF makes it tautologically ~0. */}
                {!state.self_play && (
                  <span
                    className={`badge ${
                      state.summary.avg_roi_pct - state.field_baseline.avg_roi_pct >= 0 ? 'ok' : 'risk'
                    }`}
                    title="This batch's own avg ROI minus the field baseline's -- the real field-beating edge."
                  >
                    your edge:{' '}
                    {state.summary.avg_roi_pct - state.field_baseline.avg_roi_pct >= 0 ? '+' : ''}
                    {(state.summary.avg_roi_pct - state.field_baseline.avg_roi_pct).toFixed(1)} pts ROI
                  </span>
                )}
              </div>
            )}
          </div>

          {state.exposure?.length > 0 && (
            <div className="card table-wrap" style={{ marginBottom: 14 }}>
              <div className="sub-line" style={{ marginBottom: 8 }}>
                Exposure across the batch
              </div>
              <table>
                <thead>
                  <tr>
                    <th>Player</th>
                    <th>Team</th>
                    <th className="num">Entries</th>
                    <th className="num">Exposure</th>
                    <th
                      className="num"
                      title="This player's own average simulated ROI across every lineup rostering him -- which INDIVIDUAL players drive the batch's real simulated payoff."
                    >
                      Avg ROI%
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {state.exposure.map((e) => (
                    <tr key={e.id}>
                      <td>{e.name}</td>
                      <td className="dim">{e.team}</td>
                      <td className="num">{e.count.toLocaleString()}</td>
                      <td className="num">{e.pct}%</td>
                      <td className="num">
                        {e.avg_roi_pct != null ? (
                          <span className={`badge ${e.avg_roi_pct >= 0 ? 'ok' : 'risk'}`}>
                            {e.avg_roi_pct >= 0 ? '+' : ''}
                            {e.avg_roi_pct}%
                          </span>
                        ) : (
                          <span className="dim">—</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {state.sample_entries?.length > 0 && (
            <div className="card table-wrap" style={{ marginBottom: 8 }}>
              <div className="sub-line" style={{ marginBottom: 8 }}>
                Best lineups — showing {state.sample_entries.length} of{' '}
                {state.num_entries_built.toLocaleString()}, ranked by top-1% rate
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
                    <th className="num" title="5th percentile -- this lineup scored below this in only 1 of 20 simulated trials.">
                      Floor
                    </th>
                    <th className="num" title="95th percentile -- this lineup scored above this in only 1 of 20 simulated trials.">
                      Ceiling
                    </th>
                    <th className="num">Cash %</th>
                    <th className="num" title="How often this lineup finished in the top 1% of the whole simulated contest">
                      Top 1%
                    </th>
                    <th
                      className="num"
                      title="This lineup's MEAN payout across all simulated trials -- not what a typical run returns. Payouts are enormously right-skewed, so a lineup's median payout is usually $0 and the average is carried by the runs where it hits."
                    >
                      Avg payout
                    </th>
                    <th className="num">ROI %</th>
                  </tr>
                </thead>
                <tbody>
                  {state.sample_entries.map((e, i) => {
                    const r = state.results[i]
                    return (
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
                        <td className="num dim">{r ? r.simulated_points_floor.toFixed(1) : '—'}</td>
                        <td className="num dim">{r ? r.simulated_points_ceiling.toFixed(1) : '—'}</td>
                        <td className="num">{r ? `${r.cash_probability_pct}%` : '—'}</td>
                        <td className="num">{r ? `${r.top_1pct_pct}%` : '—'}</td>
                        <td
                          className="num"
                          title={
                            r
                              ? `Mean of ${state.num_trials.toLocaleString()} simulated trials. 10th-90th percentile: $${r.payout_p10.toFixed(2)} - $${r.payout_p90.toFixed(2)}.`
                              : undefined
                          }
                        >
                          {r ? `$${r.expected_payout.toFixed(2)}` : '—'}
                        </td>
                        <td className="num">
                          {r ? (
                            <span className={`badge ${r.roi_pct >= 0 ? 'ok' : 'risk'}`}>
                              {r.roi_pct >= 0 ? '+' : ''}
                              {r.roi_pct}%
                            </span>
                          ) : (
                            '—'
                          )}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}

          <div className="dim" style={{ fontSize: 12, marginBottom: 12 }}>
            {state.note}
          </div>
        </>
      )}
    </div>
  )
}
