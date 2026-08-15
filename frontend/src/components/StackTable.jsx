import { ScoreMeter } from './ScoreMeter'
import { localTime } from '../format'

/**
 * Teams ranked by how attractive they are to stack.
 *
 * "Stacking" = rostering several hitters from the same team so one big
 * inning pays you multiple times. The stack score is the average matchup
 * score of a team's five best bats.
 */
export function StackTable({ slate }) {
  const rows = []
  for (const g of slate?.games || []) {
    for (const side of ['home', 'away']) {
      const t = g[side]
      const opp = g[side === 'home' ? 'away' : 'home']
      if (t.stack_score == null) continue
      rows.push({
        key: `${g.game_pk}-${side}`,
        team: t.abbrev || t.name,
        fullName: t.name,
        isHome: side === 'home',
        opponent: opp.abbrev,
        startTime: localTime(g.game_time_utc),
        score: t.stack_score,
        impliedRuns: t.implied_runs,
        confirmed: t.lineup_confirmed,
        venue: g.venue.name,
        parkHr: g.venue.park_factors.hr,
        roofClosed: g.venue.roof_closed,
        rain: g.weather?.precip_chance_pct,
        pitcher: opp.probable_pitcher?.name,
        pitcherHand: opp.probable_pitcher?.throws,
        pitcherEra: opp.probable_pitcher?.season?.era,
        topBats: (t.hitters || []).slice(0, 4),
      })
    }
  }
  rows.sort((a, b) => b.score - a.score)

  if (!rows.length) {
    return <div className="notice">No stack data yet for this date.</div>
  }

  return (
    <div className="card table-wrap">
      <table>
        <thead>
          <tr>
            <th>Team</th>
            <th>Start</th>
            <th>Stack score</th>
            <th className="num">Implied runs</th>
            <th>Opposing starter</th>
            <th>Park / conditions</th>
            <th>Top bats</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key}>
              <td>
                <div className="name">
                  {r.team} <span className="dim">{r.isHome ? 'vs' : '@'} {r.opponent}</span>
                </div>
                <div className="sub-line">
                  {r.confirmed ? (
                    <span className="badge ok">lineup confirmed</span>
                  ) : (
                    <span className="badge">projected lineup</span>
                  )}
                </div>
              </td>
              <td className="sub-line">{r.startTime || '—'}</td>
              <td style={{ minWidth: 120 }}>
                <ScoreMeter score={r.score} />
              </td>
              <td className="num">
                {r.impliedRuns != null ? r.impliedRuns.toFixed(1) : '—'}
              </td>
              <td>
                <div>{r.pitcher || <span className="dim">TBD</span>}</div>
                <div className="sub-line">
                  {r.pitcherHand ? `${r.pitcherHand}HP` : ''}
                  {r.pitcherEra != null ? ` · ${r.pitcherEra} ERA` : ''}
                </div>
              </td>
              <td>
                <div className="sub-line">{r.venue}</div>
                <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', marginTop: 3 }}>
                  <span className="badge">{r.parkHr.toFixed(2)}× HR</span>
                  {r.roofClosed && <span className="badge">roof closed</span>}
                  {r.rain >= 40 && <span className="badge risk">{r.rain}% rain</span>}
                </div>
              </td>
              <td>
                <div className="sub-line">
                  {r.topBats.map((b) => `${b.name} (${Math.round(b.edge.score)})`).join(', ') || '—'}
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
