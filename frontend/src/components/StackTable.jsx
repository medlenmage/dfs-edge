import { useEffect, useMemo, useState } from 'react'
import { ScoreMeter } from './ScoreMeter'
import { localTime } from '../format'

/** Average a stat across a team's top 5 bats -- same set stack_score uses. */
function avgOf(hitters, field) {
  const values = hitters
    .slice(0, 5)
    .map((h) => h.edge?.components?.contact_quality?.[field])
    .filter((v) => v != null)
  if (!values.length) return null
  return values.reduce((a, b) => a + b, 0) / values.length
}

/**
 * Teams ranked by how attractive they are to stack.
 *
 * "Stacking" = rostering several hitters from the same team so one big
 * inning pays you multiple times. The stack score is the average matchup
 * score of a team's five best bats.
 */
export function StackTable({ slate }) {
  const [showGames, setShowGames] = useState(false)
  const [includedGames, setIncludedGames] = useState(new Set())

  // Same auto-detect-from-uploaded-DK-salary-CSV pattern already used by
  // the Lineups/Contest Generator tabs' own slate-game checklists -- lets
  // this default to "just today's real DK slate" instead of every game
  // MLB's schedule returns, while still allowing a fully manual pick.
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
    // Unlike the Lineups/Contest Generator tabs (where restricting to a
    // real DK salary slate matters for correctness), this tab is purely
    // informational -- if the in_slate auto-detect would leave nothing
    // selected (e.g. a salary file loaded for a different date than
    // today's real slate), showing everything beats silently showing
    // nothing.
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
        // FantasyLabs (see clients/fantasylabs.py) tracks both the
        // originally-set (open) implied total and today's live one --
        // "Implied runs" stays the opening number so it doesn't shift
        // under someone mid-session, "Live" is the separate up-to-date
        // read for whenever FantasyLabs updates it. Falls back to the
        // single implied_runs field (pre-FantasyLabs slates/tests)
        // when the open/current split isn't available.
        impliedRuns: t.vegas_implied_runs_open ?? t.implied_runs,
        boomPct: t.stack_boom_pct ?? null,
        bustPct: t.stack_bust_pct ?? null,
        liveImpliedRuns: t.vegas_implied_runs_current ?? t.implied_runs,
        avgXwoba: avgOf(t.hitters || [], 'xwoba'),
        avgBarrel: avgOf(t.hitters || [], 'barrel_pct'),
        confirmed: t.lineup_confirmed,
        venue: g.venue.name,
        parkHr: g.venue.park_factors.hr,
        roofClosed: g.venue.roof_closed,
        rain: g.weather?.precip_chance_pct,
        wind: g.weather?.wind_effect,
        pitcher: opp.probable_pitcher?.name,
        pitcherHand: opp.probable_pitcher?.throws,
        pitcherEra: opp.probable_pitcher?.season?.era,
        // Every hitter on this team faces the same opposing bullpen, so
        // any one of them carries the number.
        bullpenEra: (t.hitters || [])[0]?.edge?.components?.bullpen?.era ?? null,
        // Recent workload (last 2 days) is a separate signal from season
        // ERA above -- a fine bullpen can still be gassed from an
        // extra-inning game, and a bad one can happen to be rested.
        bullpenRecentOuts: (t.hitters || [])[0]?.edge?.components?.bullpen_workload?.outs ?? null,
        topBats: (t.hitters || []).slice(0, 4),
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
            : 'No stack data yet for this date.'}
        </div>
      ) : (
      <div className="card table-wrap">
      <table>
        <thead>
          <tr>
            <th>Team</th>
            <th>Start</th>
            <th>Stack score</th>
            <th className="num" title="The implied run total as originally set -- doesn't move once loaded">
              Implied runs
            </th>
            <th className="num" title="Today's live implied run total, from FantasyLabs -- updates as the line moves">
              Live
            </th>
            <th
              className="num"
              title="Chance this team's top-5 stack combines for 1.5x or more of its combined projection -- a real correlated Monte Carlo over the five bats' own game-by-game outcome pools, teammates sharing each trial's team environment exactly as the full simulator correlates them. Needs in-house projections loaded."
            >
              Boom%
            </th>
            <th
              className="num"
              title="Chance this team's top-5 stack combines for HALF its combined projection or less -- the offense-got-shut-down night that kills every lineup built on it. Same correlated Monte Carlo as Boom%. Needs in-house projections loaded."
            >
              Bust%
            </th>
            <th className="num">Contact quality</th>
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
              <td className="num">
                {r.liveImpliedRuns != null ? (
                  <span
                    style={
                      r.impliedRuns != null && r.liveImpliedRuns !== r.impliedRuns
                        ? { color: r.liveImpliedRuns > r.impliedRuns ? 'var(--good)' : 'var(--critical)' }
                        : undefined
                    }
                  >
                    {r.liveImpliedRuns.toFixed(1)}
                    {r.impliedRuns != null && r.liveImpliedRuns !== r.impliedRuns && (
                      <> {r.liveImpliedRuns > r.impliedRuns ? '▲' : '▼'}</>
                    )}
                  </span>
                ) : (
                  '—'
                )}
              </td>
              <td className="num">
                {r.boomPct != null ? (
                  <span className={`badge ${r.boomPct >= 28 ? 'ok' : ''}`}>{r.boomPct}%</span>
                ) : (
                  '—'
                )}
              </td>
              <td className="num">
                {r.bustPct != null ? (
                  <span className={`badge ${r.bustPct >= 38 ? 'risk' : ''}`}>{r.bustPct}%</span>
                ) : (
                  '—'
                )}
              </td>
              <td className="num">
                {r.avgXwoba != null ? r.avgXwoba.toFixed(3) : '—'}
                {r.avgBarrel != null && (
                  <div className="sub-line">{r.avgBarrel.toFixed(1)}% barrels</div>
                )}
              </td>
              <td>
                <div>{r.pitcher || <span className="dim">TBD</span>}</div>
                <div className="sub-line">
                  {r.pitcherHand ? `${r.pitcherHand}HP` : ''}
                  {r.pitcherEra != null ? ` · ${r.pitcherEra} ERA` : ''}
                </div>
                {r.bullpenEra != null && (
                  <div className="sub-line">
                    {r.bullpenEra.toFixed(2)} bullpen ERA
                    {r.bullpenEra >= 4.5 && <span className="badge risk" style={{ marginLeft: 4 }}>shaky pen</span>}
                  </div>
                )}
                {r.bullpenRecentOuts != null && (
                  <div className="sub-line dim" style={{ fontSize: 12 }}>
                    {(r.bullpenRecentOuts / 3).toFixed(1)} pen IP last 2 days
                    {r.bullpenRecentOuts >= 30 && <span className="badge risk" style={{ marginLeft: 4 }}>taxed pen</span>}
                  </div>
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
              <td>
                <div className="sub-line">
                  {r.topBats
                    .map((b) => {
                      const xwoba = b.edge?.components?.contact_quality?.xwoba
                      return `${b.name} (${Math.round(b.edge.score)}${xwoba != null ? `, ${xwoba.toFixed(3).slice(1)}` : ''})`
                    })
                    .join(', ') || '—'}
                </div>
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
