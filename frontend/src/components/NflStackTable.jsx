import { useEffect, useState } from 'react'
import { api } from '../api'
import { ScoreMeter } from './ScoreMeter'

const SLOT_ORDER = ['WR1', 'WR2', 'TE1', 'RB1']

function partnerLabel(p) {
  const corr = p.correlation != null ? ` r=${p.correlation.toFixed(2)}` : ''
  const fpts = p.projected_fpts != null ? ` ${p.projected_fpts.toFixed(1)}p` : ''
  return `${p.name} (${p.slot}${fpts}${corr})`
}

/**
 * NFL's answer to MLB's Stacks tab: every team ranked by how good a
 * QB-stack environment they're in this week, with the strongest real
 * correlation partner recommended for each -- not a guess at "pair the
 * QB with a receiver," but ranked WR1 > WR2 > TE1 > RB1 because that's
 * what a real season of DK-scored game logs actually shows (see the
 * correlation coefficients in the header). PROE and the opponent's
 * pass-funnel read come from real play-by-play, not plain pass rate.
 */
export function NflStackTable({ season, week }) {
  const [state, setState] = useState({ status: 'idle' })

  useEffect(() => {
    let cancelled = false
    setState({ status: 'loading' })
    api
      .nflStacks(season, week)
      .then((data) => {
        if (!cancelled) setState({ status: 'ready', data })
      })
      .catch((err) => {
        if (!cancelled) setState({ status: 'error', message: err.message })
      })
    return () => {
      cancelled = true
    }
  }, [season, week])

  if (state.status === 'loading' || state.status === 'idle') {
    return (
      <div className="card">
        <div className="skeleton" style={{ width: '40%', marginBottom: 12 }} />
        <div className="skeleton" style={{ width: '100%', marginBottom: 8 }} />
        <div className="skeleton" style={{ width: '88%' }} />
      </div>
    )
  }

  if (state.status === 'error') {
    return <div className="notice error">Couldn't load stack ratings: {state.message}</div>
  }

  const { data } = state
  const teams = data.teams || []
  const corr = data.correlations || {}

  return (
    <>
      <div
        className="notice"
        style={{ marginBottom: 14 }}
        title="Computed once per correlation_season from a full season of real DK-scored game logs -- not an assumed weight."
      >
        Real {data.correlation_season} correlation: QB-WR1 r={corr.qb_wr1?.correlation ?? '—'} · QB-WR2 r=
        {corr.qb_wr2?.correlation ?? '—'} · QB-TE1 r={corr.qb_te1?.correlation ?? '—'} · QB-RB1 r=
        {corr.qb_rb1?.correlation ?? '—'} · bring-back r={corr.qb_bring_back_wr1?.correlation ?? '—'}
      </div>

      {!teams.length ? (
        <div className="notice">{data.message || 'No stack data yet for this week.'}</div>
      ) : (
        <div className="card table-wrap">
          <table>
            <thead>
              <tr>
                <th>Team</th>
                <th>Rating</th>
                <th className="num">Environment</th>
                <th className="num">PROE / funnel</th>
                <th>Recommended stack</th>
                <th>Bring-back</th>
                <th className="num">Combined value</th>
              </tr>
            </thead>
            <tbody>
              {teams.map((t) => {
                const partners = {}
                for (const p of t.partners || []) partners[p.slot] = p
                return (
                  <tr key={`${t.team}-${t.opponent}`}>
                    <td>
                      <div className="name">
                        {t.team} <span className="dim">{t.is_home ? 'vs' : '@'} {t.opponent}</span>
                      </div>
                    </td>
                    <td style={{ minWidth: 120 }}>
                      <ScoreMeter score={t.rating} />
                    </td>
                    <td className="num">
                      {t.components.environment.implied_total != null
                        ? `${t.components.environment.implied_total.toFixed(1)} pts`
                        : '—'}
                      {t.components.game_total.spread != null && (
                        <div style={{ marginTop: 2 }}>
                          <span className={`badge ${t.components.game_total.favored ? 'ok' : ''}`}>
                            {t.components.game_total.favored === true
                              ? `Fav −${t.components.game_total.spread}`
                              : t.components.game_total.favored === false
                              ? `Dog +${t.components.game_total.spread}`
                              : `±${t.components.game_total.spread} (favorite unknown)`}
                          </span>
                        </div>
                      )}
                      <div className="sub-line">{t.components.game_total.detail}</div>
                    </td>
                    <td className="num">
                      {t.components.proe.detail}
                      <div className="sub-line">{t.components.pass_funnel.detail}</div>
                    </td>
                    <td>
                      <div className="sub-line">
                        {SLOT_ORDER.map((slot) => partners[slot])
                          .filter(Boolean)
                          .map(partnerLabel)
                          .join(', ') || <span className="dim">no salary/projections loaded</span>}
                      </div>
                    </td>
                    <td>
                      {t.bring_back ? (
                        <div className="sub-line">{partnerLabel(t.bring_back)}</div>
                      ) : (
                        <span className="dim">—</span>
                      )}
                    </td>
                    <td className="num">
                      {t.top_stack_value ? (
                        <>
                          ${t.top_stack_value.combined_salary.toLocaleString()}
                          <div className="sub-line">
                            {t.top_stack_value.combined_projected_fpts.toFixed(1)}p ·{' '}
                            {t.top_stack_value.value_per_1000?.toFixed(2)}p/$1k
                            {t.top_stack_value.combined_ownership_pct != null &&
                              ` · ${t.top_stack_value.combined_ownership_pct.toFixed(1)}% own`}
                          </div>
                        </>
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
    </>
  )
}
