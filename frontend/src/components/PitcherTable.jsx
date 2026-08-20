import { useEffect, useMemo, useState } from 'react'
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
  const [showGames, setShowGames] = useState(false)
  const [includedGames, setIncludedGames] = useState(new Set())

  // Same auto-detect-from-uploaded-DK-salary-CSV pattern already used by
  // the Stacks/Hitters/Lineups/Contest Generator tabs' own slate-game
  // checklists.
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
  // Includes each game's inSlate flag, not just its pk -- switching to a
  // different loaded DK slate (Early/Main/Night/...) on the same date
  // changes which games are in_slate without changing the day's own
  // list of game_pks, so a pk-only key would never re-run this effect.
  const slateGamesKey = slateGames.map((g) => `${g.pk}:${g.inSlate}`).join(',')

  useEffect(() => {
    // Purely informational tab -- if the in_slate auto-detect would
    // leave nothing selected (e.g. a salary file loaded for a
    // different date than today's real slate), show everything
    // instead of silently showing nothing.
    const detected = slateGames.filter((g) => g.inSlate !== false).map((g) => g.pk)
    setIncludedGames(new Set(detected.length ? detected : slateGames.map((g) => g.pk)))
  }, [slateGamesKey])

  function toggleGame(pk) {
    setIncludedGames((prev) => {
      const next = new Set(prev)
      next.has(pk) ? next.delete(pk) : next.add(pk)
      return next
    })
  }

  const rows = []
  for (const g of slate?.games || []) {
    if (slateGames.length && !includedGames.has(g.game_pk)) continue
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
        salary: p.salary?.salary ?? null,
        value: p.salary?.value ?? null,
        avgPoints: p.salary?.avg_points ?? null,
        fpts: p.projection?.fpts ?? null,
        ownershipPct: p.projection?.ownership_pct ?? null,
        inhouseFpts: p.projection?.inhouse_fpts ?? null,
        inhouseOwnershipPct: p.projection?.inhouse_ownership_pct ?? null,
        leverage: p.projection?.leverage_score ?? null,
        venue: g.venue.name,
        parkHr: g.venue.park_factors.hr,
        roofClosed: g.venue.roof_closed,
        rain: g.weather?.precip_chance_pct,
        wind: g.weather?.wind_effect,
      })
    }
  }
  rows.sort((a, b) => b.score - a.score)

  return (
    <>
      {slateGames.length > 0 && (
        <div className="controls" style={{ marginBottom: 12 }}>
          <button onClick={() => setShowGames((s) => !s)}>
            {showGames ? 'Hide games' : 'Games'} ({includedGames.size} of {slateGames.length})
          </button>
        </div>
      )}

      {showGames && (
        <div className="card" style={{ marginBottom: 14 }}>
          <div className="sub-line" style={{ marginBottom: 8 }}>
            {slateDetected
              ? 'Auto-detected from your uploaded DK salary CSV -- untick a game to leave it out, or tick one back in.'
              : 'No DK salary CSV uploaded yet, so every game is included by default -- untick any you want to ignore.'}
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

      {!rows.length ? (
        <div className="notice">
          {slateGames.length && includedGames.size === 0
            ? 'No games selected -- tick at least one above.'
            : 'No pitcher data yet for this date.'}
        </div>
      ) : (
      <div className="card table-wrap">
      <table>
        <thead>
          <tr>
            <th>Pitcher</th>
            <th>Start</th>
            <th>Edge</th>
            <th className="num">Season</th>
            <th className="num">Runs against</th>
            <th className="num">Salary</th>
            <th className="num">Value</th>
            <th className="num">Proj FPTS</th>
            <th className="num">Own%</th>
            <th className="num">In-house FPTS</th>
            <th className="num">In-house Own%</th>
            <th className="num">Leverage</th>
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
              <td className="num">
                {r.salary != null ? `$${r.salary.toLocaleString()}` : '—'}
                {r.avgPoints != null && (
                  <div className="sub-line">{r.avgPoints.toFixed(1)} avg pts</div>
                )}
              </td>
              <td className="num">
                {r.value != null ? r.value.toFixed(1) : '—'}
              </td>
              <td className="num">
                {r.fpts != null ? r.fpts.toFixed(1) : '—'}
              </td>
              <td className="num">
                {r.ownershipPct != null ? `${r.ownershipPct.toFixed(1)}%` : '—'}
              </td>
              <td className="num">
                {r.inhouseFpts != null ? r.inhouseFpts.toFixed(1) : '—'}
              </td>
              <td className="num">
                {r.inhouseOwnershipPct != null ? `${r.inhouseOwnershipPct.toFixed(1)}%` : '—'}
              </td>
              <td className="num">
                {r.leverage != null ? (
                  <span className={`badge ${r.leverage >= 0 ? 'ok' : 'risk'}`}>
                    {r.leverage >= 0 ? '+' : ''}
                    {r.leverage.toFixed(1)}
                  </span>
                ) : (
                  '—'
                )}
              </td>
              <td>
                <div className="sub-line">{r.venue}</div>
                <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', marginTop: 3 }}>
                  <span className="badge">{r.parkHr.toFixed(2)}× HR</span>
                  {r.roofClosed && <span className="badge">roof closed</span>}
                  {r.wind?.label && r.wind.label !== 'unknown' && (
                    <span className="badge">
                      wind {r.wind.label} {r.wind.speed_mph}mph
                    </span>
                  )}
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
      )}
    </>
  )
}
