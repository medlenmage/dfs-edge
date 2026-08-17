import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { localTime } from '../format'

/**
 * Mass multi-entry contest generator -- separate from the Lineups tab's
 * exact MILP optimizer (capped at 150, one precise best lineup at a
 * time). This builds up to 10,000 of your own entries in one shot by
 * fast randomized construction weighted toward projected points, then
 * ranks the whole batch against a simulated opponent field for
 * cash-rate/payout economics -- by default against the field's
 * projected points (fast), or via the "Simulate" toggle against a real
 * Monte Carlo outcome distribution (slower, genuine cash probabilities).
 * See the caveat rendered next to the economics numbers below.
 */
export function ContestGeneratorPanel({ date, slate }) {
  const [contestTypes, setContestTypes] = useState(null)
  const [contestType, setContestType] = useState('gpp_large')
  const [numLineups, setNumLineups] = useState(500)
  const [maxExposure, setMaxExposure] = useState('')
  const [simulate, setSimulate] = useState(false)
  const [numTrials, setNumTrials] = useState(1000)
  const [showSlateGames, setShowSlateGames] = useState(false)
  const [includedGames, setIncludedGames] = useState(new Set())
  const [state, setState] = useState({ status: 'idle' })

  useEffect(() => {
    api
      .contestTypes()
      .then((d) => setContestTypes(d.contest_types))
      .catch(() => {})
  }, [])

  const slateGames = useMemo(
    () =>
      (slate?.games || [])
        .filter((g) => g.game_pk != null)
        .map((g) => ({
          pk: g.game_pk,
          away: g.away?.abbrev,
          home: g.home?.abbrev,
          time: g.game_time_utc,
          inSlate: g.in_slate,
        })),
    [slate],
  )
  const slateDetected = slateGames.some((g) => g.inSlate != null)
  const slateGamePks = slateGames.map((g) => g.pk).join(',')

  useEffect(() => {
    setIncludedGames(new Set(slateGames.filter((g) => g.inSlate !== false).map((g) => g.pk)))
  }, [slateGamePks])

  function toggleGame(pk) {
    setIncludedGames((prev) => {
      const next = new Set(prev)
      next.has(pk) ? next.delete(pk) : next.add(pk)
      return next
    })
  }

  const preset = contestTypes?.[contestType]
  const overRequested = preset && numLineups > preset.field_size

  async function run() {
    setState({ status: 'loading' })
    try {
      const opts = {
        maxExposurePct: maxExposure.trim() ? Number(maxExposure) : null,
        includedGamePks:
          slateGames.length && includedGames.size < slateGames.length ? [...includedGames] : null,
      }
      const result = simulate
        ? await api.buildContestEntriesSimulated(date, contestType, numLineups, { ...opts, numTrials })
        : await api.buildContestEntries(date, contestType, numLineups, opts)
      setState({ status: 'ready', simulated: simulate, ...result })
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
            {contestTypes &&
              Object.entries(contestTypes).map(([key, c]) => (
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
            onChange={(e) => setNumLineups(Math.max(1, Math.min(10000, Number(e.target.value) || 1)))}
            style={{ width: 80 }}
          />
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
        <label
          className="dim"
          style={{ fontSize: 13, display: 'flex', alignItems: 'center', gap: 6 }}
          title="Rank entries against real Monte Carlo simulated outcomes (each player's own historical game log, resampled) instead of projected points -- slower, but gives a genuine cash probability and payout range."
        >
          <input type="checkbox" checked={simulate} onChange={(e) => setSimulate(e.target.checked)} />
          Simulate
        </label>
        {simulate && (
          <label className="dim" style={{ fontSize: 13 }}>
            Trials{' '}
            <select value={numTrials} onChange={(e) => setNumTrials(Number(e.target.value))}>
              {[500, 1000, 2000, 5000].map((n) => (
                <option key={n} value={n}>
                  {n.toLocaleString()}
                </option>
              ))}
            </select>
          </label>
        )}
        {slateGames.length > 0 && (
          <button onClick={() => setShowSlateGames((v) => !v)}>
            {showSlateGames ? 'Hide slate games' : 'Slate games'} ({includedGames.size} of{' '}
            {slateGames.length})
          </button>
        )}
        <button className="primary" onClick={run} disabled={state.status === 'loading'}>
          {state.status === 'loading'
            ? simulate
              ? 'Simulating…'
              : 'Building…'
            : `${simulate ? 'Simulate' : 'Build'} ${numLineups.toLocaleString()} entries`}
        </button>
      </div>

      {preset && overRequested && (
        <div className="notice" style={{ marginBottom: 14 }}>
          {preset.label}'s field only holds {preset.field_size.toLocaleString()} entries — your own
          entries are part of that field, not additional to it. Lower "Entries to build" to at most{' '}
          {preset.field_size.toLocaleString()}, or pick a bigger contest.
        </div>
      )}

      {showSlateGames && (
        <div className="card" style={{ marginBottom: 14 }}>
          <div className="sub-line" style={{ marginBottom: 8 }}>
            {slateDetected
              ? 'Auto-detected from your uploaded DK salary CSV -- untick a game to leave it out, or tick one back in.'
              : 'No DK salary CSV uploaded yet, so every game is included by default -- upload one to auto-detect your slate.'}
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            {slateGames.map((g) => (
              <label
                key={g.pk}
                className="dim"
                style={{ fontSize: 13, display: 'flex', alignItems: 'center', gap: 8 }}
              >
                <input
                  type="checkbox"
                  checked={includedGames.has(g.pk)}
                  onChange={() => toggleGame(g.pk)}
                />
                {g.away} @ {g.home}
                <span className="dim" style={{ fontSize: 12 }}>
                  {localTime(g.time)}
                </span>
                {g.inSlate === true && <span className="badge ok">in DK slate</span>}
                {g.inSlate === false && <span className="badge">not in DK slate</span>}
              </label>
            ))}
          </div>
        </div>
      )}

      {state.status === 'idle' && (
        <p style={{ marginTop: 0, color: 'var(--text-secondary)' }}>
          Builds up to 10,000 of your own DraftKings Classic MLB entries in one shot -- each
          individually strong (weighted heavily toward projected points) but genuinely distinct
          from every other entry in the batch. Separate from the Lineups tab's exact optimizer,
          which is built for a small number of provably-best lineups, not mass multi-entry. Upload
          a DraftKings salary CSV and a RotoWire projections CSV for this date first.
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
          {state.num_entries_built < state.num_entries_requested && (
            <div className="notice" style={{ marginBottom: 12 }}>
              Only built {state.num_entries_built.toLocaleString()} of{' '}
              {state.num_entries_requested.toLocaleString()} requested — the pool ran out of room
              for more distinct, legal entries under the current exposure cap.
            </div>
          )}

          <div className="controls" style={{ marginBottom: 12, flexWrap: 'wrap' }}>
            <span className="badge ok">{state.num_entries_built.toLocaleString()} entries built</span>
            <span className="badge">{state.field_size.toLocaleString()}-entry contest</span>
            <span className="badge">${state.contest.entry_fee.toLocaleString()} entry</span>
            <span className="badge">{state.paid_count.toLocaleString()} paid</span>
            <span className="badge">${state.prize_pool.toLocaleString()} prize pool</span>
            {state.simulated && (
              <span className="badge">{state.num_trials.toLocaleString()} sim trials</span>
            )}
            <a
              href={api.contestEntriesCsvUrl(state.batch_id)}
              title={`Download all ${state.num_entries_built.toLocaleString()} entries as a CSV -- for handing off to an external simulator`}
            >
              <button>Download full batch (CSV)</button>
            </a>
          </div>

          <div className="card" style={{ marginBottom: 14 }}>
            <div className="sub-line" style={{ marginBottom: 8 }}>
              {state.simulated ? (
                <>
                  Simulated economics — {state.num_trials.toLocaleString()} Monte Carlo trials of
                  each player's own real historical outcomes, with team correlation for hitters.
                  Cash probability and payout are genuine simulated results, not a single
                  projected-points estimate against the field.
                </>
              ) : (
                <>
                  Estimated economics — <strong>not a prediction.</strong> This is your batch's
                  projected points ranked against a projected-points model of the field, not
                  simulated real-world results. Toggle "Simulate" above for real Monte Carlo cash
                  probabilities.
                </>
              )}
            </div>
            {state.simulated ? (
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
                <span className="badge">
                  ${state.summary.total_expected_payout.toLocaleString()} expected payout
                </span>
                <span className={`badge ${state.summary.estimated_net_profit >= 0 ? 'ok' : 'risk'}`}>
                  {state.summary.estimated_net_profit >= 0 ? '+' : ''}$
                  {state.summary.estimated_net_profit.toLocaleString()} est. net
                </span>
              </div>
            ) : (
              <div className="controls" style={{ flexWrap: 'wrap' }}>
                <span className="badge">
                  {state.summary.cashing_count.toLocaleString()} cashing ({state.summary.cashing_pct}%)
                </span>
                <span className="badge">${state.summary.total_entry_cost.toLocaleString()} cost</span>
                <span className="badge">
                  ${state.summary.total_estimated_payout.toLocaleString()} est. payout
                </span>
                <span className={`badge ${state.summary.estimated_net_profit >= 0 ? 'ok' : 'risk'}`}>
                  {state.summary.estimated_net_profit >= 0 ? '+' : ''}$
                  {state.summary.estimated_net_profit.toLocaleString()} est. net
                </span>
                <span className="badge">{state.summary.avg_projected_points.toFixed(1)} avg proj FPTS</span>
                <span className="badge">
                  {state.summary.min_projected_points.toFixed(1)}–{state.summary.max_projected_points.toFixed(1)} range
                </span>
                <span className="badge">{state.summary.avg_total_ownership_pct.toFixed(1)}% avg ownership</span>
              </div>
            )}
          </div>

          {state.exposure.length > 0 && (
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

          {state.sample_entries.length > 0 && (
            <div className="card table-wrap" style={{ marginBottom: 8 }}>
              <div className="sub-line" style={{ marginBottom: 8 }}>
                Sample entries — showing {state.sample_entries.length} of{' '}
                {state.num_entries_built.toLocaleString()}
              </div>
              <table>
                <thead>
                  <tr>
                    <th className="num">#</th>
                    <th className="num">Salary</th>
                    <th className="num">Proj FPTS</th>
                    {state.simulated && (
                      <th className="num" title="10th-90th percentile simulated points across every trial">
                        Sim floor–ceiling
                      </th>
                    )}
                    <th className="num">Own%</th>
                    {state.simulated ? (
                      <>
                        <th className="num">Cash %</th>
                        <th className="num" title="How often this lineup finished 1st out of the whole simulated contest">
                          1st %
                        </th>
                        <th className="num" title="How often this lineup finished in the top 1% of the whole simulated contest">
                          Top 1%
                        </th>
                        <th className="num" title="How often this lineup finished in the top 10% of the whole simulated contest">
                          Top 10%
                        </th>
                        <th className="num">Exp. payout</th>
                        <th className="num" title="(expected payout - entry fee) / entry fee">
                          ROI %
                        </th>
                      </>
                    ) : (
                      <>
                        <th className="num">Rank</th>
                        <th>Cashing?</th>
                      </>
                    )}
                  </tr>
                </thead>
                <tbody>
                  {state.sample_entries.map((e, i) => {
                    const r = state.results[i]
                    return (
                      <tr key={i}>
                        <td className="num">{i + 1}</td>
                        <td className="num">${e.salary_used.toLocaleString()}</td>
                        <td className="num">{e.projected_points.toFixed(1)}</td>
                        {state.simulated && (
                          <td className="num dim" style={{ fontSize: 12 }}>
                            {r
                              ? `${r.simulated_points_p10.toFixed(0)}–${r.simulated_points_p90.toFixed(0)}`
                              : '—'}
                          </td>
                        )}
                        <td className="num">{e.total_ownership_pct.toFixed(1)}%</td>
                        {state.simulated ? (
                          <>
                            <td className="num">{r ? `${r.cash_probability_pct}%` : '—'}</td>
                            <td className="num">{r ? `${r.first_place_pct}%` : '—'}</td>
                            <td className="num">{r ? `${r.top_1pct_pct}%` : '—'}</td>
                            <td className="num">{r ? `${r.top_10pct_pct}%` : '—'}</td>
                            <td className="num">{r ? `$${r.expected_payout.toFixed(2)}` : '—'}</td>
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
                          </>
                        ) : (
                          <>
                            <td className="num">{r ? r.estimated_rank.toLocaleString() : '—'}</td>
                            <td>
                              {r ? (
                                r.in_the_money ? (
                                  <span className="badge ok">cashes</span>
                                ) : (
                                  <span className="badge risk">misses</span>
                                )
                              ) : (
                                '—'
                              )}
                            </td>
                          </>
                        )}
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

          <button onClick={run}>Rebuild</button>
        </>
      )}
    </div>
  )
}
