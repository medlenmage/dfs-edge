import { ScoreMeter } from './ScoreMeter'
import { localTime } from '../format'

const DRIVER_LABELS = {
  opp_lineup: 'the lineup he’s facing',
  strikeout_potential: 'strikeout upside',
  team_runs_against: 'the Vegas total against him',
  contact_quality_allowed: 'contact quality allowed',
  own_quality: 'his season ERA',
  park: 'the ballpark',
  weather: 'weather',
}

/**
 * Today's probable starters, ranked by matchup edge -- the mirror image
 * of the Stacks tab: instead of "who's good to roster", this is
 * "whose start looks good tonight".
 */
export function PitcherTable({ slate }) {
  const rows = []
  for (const g of slate?.games || []) {
    for (const side of ['home', 'away']) {
      const team = g[side]
      const opp = g[side === 'home' ? 'away' : 'home']
      const p = team.probable_pitcher
      if (!p?.edge) continue
      rows.push({
        key: `${g.game_pk}-${side}`,
        name: p.name,
        throws: p.throws,
        team: team.abbrev,
        opponent: opp.abbrev,
        isHome: side === 'home',
        startTime: localTime(g.game_time_utc),
        score: p.edge.score,
        driver: p.edge.top_driver,
        era: p.season?.era,
        k9: p.season?.k_per_9,
        impliedRunsAgainst: opp.implied_runs,
        venue: g.venue.name,
        parkHr: g.venue.park_factors.hr,
        roofClosed: g.venue.roof_closed,
        rain: g.weather?.precip_chance_pct,
      })
    }
  }
  rows.sort((a, b) => b.score - a.score)

  if (!rows.length) {
    return <div className="notice">No pitcher data yet for this date.</div>
  }

  return (
    <div className="card table-wrap">
      <table>
        <thead>
          <tr>
            <th>Pitcher</th>
            <th>Start</th>
            <th>Edge</th>
            <th className="num">Season</th>
            <th className="num">Runs against</th>
            <th>Park / conditions</th>
            <th>Biggest factor</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key}>
              <td>
                <div className="name">{r.name}</div>
                <div className="sub-line">
                  {r.throws ? `${r.throws}HP · ` : ''}
                  {r.team} <span className="dim">{r.isHome ? 'vs' : '@'} {r.opponent}</span>
                </div>
              </td>
              <td className="sub-line">{r.startTime || '—'}</td>
              <td style={{ minWidth: 120 }}>
                <ScoreMeter score={r.score} />
              </td>
              <td className="num">
                {r.era != null ? `${r.era} ERA` : '—'}
                {r.k9 != null && <div className="sub-line">{r.k9} K/9</div>}
              </td>
              <td className="num">
                {r.impliedRunsAgainst != null ? r.impliedRunsAgainst.toFixed(1) : '—'}
              </td>
              <td>
                <div className="sub-line">{r.venue}</div>
                <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', marginTop: 3 }}>
                  <span className="badge">{r.parkHr.toFixed(2)}× HR</span>
                  {r.roofClosed && <span className="badge">roof closed</span>}
                  {r.rain >= 40 && <span className="badge risk">{r.rain}% rain</span>}
                </div>
              </td>
              <td className="sub-line">
                {DRIVER_LABELS[r.driver] || r.driver || '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
