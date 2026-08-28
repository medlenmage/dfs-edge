import { useEffect, useMemo, useState } from 'react'
import { ScoreMeter } from './ScoreMeter'
import { SalaryRangeFilter, salaryBounds, withinSalaryRange } from './SalaryRangeFilter'

const DRIVER_LABELS = {
  implied_total: 'Vegas total',
  game_script: 'game script',
  weather: 'weather',
  defense_vs_position: 'opponent D',
  pace: 'pace',
}

// A real DK NFL salary CSV carries every rostered player, most of whom
// are deep-bench names nobody can roster meaningfully -- a real week 1
// pool has WRs at 0.05 and 1.9 projected points sitting alongside the
// actual starters. Nothing in the slate data flags "starter" (no depth
// chart exists in any free source this app reads), so the real,
// available proxy is production: a genuine starter has either a real
// projection or a real DK season average, and a bench player has
// neither. Taking the max of the two means the table still fills in
// before RotoWire projections are uploaded (DK's own AvgPointsPerGame
// carries it) and stays correct for a rookie with no DK history but a
// real projection.
const MIN_RELEVANT_FPTS = 3


/**
 * Every player at one or more DFS positions, ranked by real matchup
 * score -- the NFL sibling of HitterTable.jsx/PitcherTable.jsx (MLB).
 * `positions` is an array so one component can serve every sub-tab,
 * including FLEX (`['RB','WR','TE']` -- not a real position, just the
 * union DK's own FLEX roster slot allows).
 *
 * Reads fields already fully computed by nfl_slate.py's
 * `_team_players()` (edge.score/components, salary, projection) --
 * zero new backend work, this is the same data the Lineups tab's
 * optimizer pool already gets, just not yet browsable/sortable/
 * position-grouped on its own.
 */
export function NflPositionTable({ slate, positions, limit = 100 }) {
  const [sortKey, setSortKey] = useState('score')
  const [sortDir, setSortDir] = useState('desc')
  const [search, setSearch] = useState('')
  const [minSalary, setMinSalary] = useState('')
  const [maxSalary, setMaxSalary] = useState('')
  const [showGames, setShowGames] = useState(false)
  const [includedGames, setIncludedGames] = useState(new Set())

  // Same auto-detect-from-slate pattern every other informational tab
  // (MLB's Stacks/Hitters/Pitchers, NFL's own Matchups) already uses.
  // No real UTC game time is exposed on this dict (nfl_slate.py keeps
  // one internally but only for the weather fetch) -- same plain-text
  // weekday/gameday/gametime display NflGameCard already uses, not
  // localTime() (which expects a real UTC instant, not these).
  const slateGames = useMemo(
    () =>
      (slate?.games || [])
        .filter((g) => g.game_id != null)
        .map((g) => ({
          pk: g.game_id,
          away: g.away?.abbrev,
          home: g.home?.abbrev,
          time: [g.weekday, g.gameday, g.gametime].filter(Boolean).join(' '),
        })),
    [slate],
  )
  const slateGamesKey = slateGames.map((g) => g.pk).join(',')

  useEffect(() => {
    setIncludedGames(new Set(slateGames.map((g) => g.pk)))
  }, [slateGamesKey])

  function toggleGame(pk) {
    setIncludedGames((prev) => {
      const next = new Set(prev)
      next.has(pk) ? next.delete(pk) : next.add(pk)
      return next
    })
  }

  // A salary range means something different at every position -- a
  // $6,000 floor is mid-range for a QB and impossible for a DST -- so
  // switching sub-tabs clears it rather than silently showing an empty
  // table under a bound carried over from a differently-priced group.
  const positionsKey = positions.join(',')
  useEffect(() => {
    setMinSalary('')
    setMaxSalary('')
  }, [positionsKey])

  const [rows, hiddenCount] = useMemo(() => {
    const wanted = new Set(positions)
    const out = []
    let hidden = 0
    for (const g of slate?.games || []) {
      for (const side of ['home', 'away']) {
        const team = g[side]
        const opp = g[side === 'home' ? 'away' : 'home']
        for (const p of team.players || []) {
          if (!wanted.has(p.position)) continue
          // Every team has exactly one DST and it's always rosterable,
          // so it's a starter by definition -- never filtered out.
          if (p.position !== 'DST') {
            const best = Math.max(p.projection?.fpts ?? 0, p.avg_points ?? 0)
            if (best < MIN_RELEVANT_FPTS) {
              hidden += 1
              continue
            }
          }
          const comps = p.edge?.components || {}
          out.push({
            id: p.dk_id || `${g.game_id}-${side}-${p.name}`,
            game_pk: g.game_id,
            name: p.name,
            position: p.position,
            team: team.abbrev,
            opponent: opp.abbrev,
            isHome: side === 'home',
            startTime: g.gameday && g.gametime ? `${g.weekday || ''} ${g.gametime}`.trim() : null,
            score: p.edge?.score ?? null,
            driver: p.edge?.top_driver,
            impliedTotal: comps.implied_total?.implied_total ?? null,
            defenseDetail: comps.defense_vs_position?.detail ?? null,
            paceDetail: comps.pace?.detail ?? null,
            salary: p.salary ?? null,
            avgPoints: p.avg_points ?? null,
            value: p.value ?? null,
            fpts: p.projection?.fpts ?? null,
            ownershipPct: p.projection?.ownership_pct ?? null,
            inhouseFpts: p.projection?.inhouse_fpts ?? null,
            inhouseOwnershipPct: p.projection?.inhouse_ownership_pct ?? null,
            inhouseCeiling: p.projection?.inhouse_ceiling ?? null,
            leverageScore: p.projection?.leverage_score ?? null,
          })
        }
      }
    }
    return [out, hidden]
  }, [slate, positions])

  // The in-house columns only appear once they've actually been
  // computed for this week, so a plain slate load isn't cluttered with
  // a block of empty cells.
  const hasInhouse = useMemo(() => rows.some((r) => r.inhouseFpts != null), [rows])

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    const list = rows.filter(
      (r) =>
        (!slateGames.length || includedGames.has(r.game_pk)) &&
        withinSalaryRange(r.salary, minSalary, maxSalary) &&
        (!q || r.name?.toLowerCase().includes(q) || r.team?.toLowerCase().includes(q)),
    )
    const dir = sortDir === 'asc' ? 1 : -1
    list.sort((a, b) => {
      const av = a[sortKey]
      const bv = b[sortKey]
      if (av == null && bv == null) return 0
      if (av == null) return 1
      if (bv == null) return -1
      if (typeof av === 'string') return av.localeCompare(bv) * dir
      return (av - bv) * dir
    })
    return list.slice(0, limit)
  }, [rows, sortKey, sortDir, search, minSalary, maxSalary, limit, includedGames, slateGames])

  const bounds = useMemo(() => salaryBounds(rows), [rows])

  function toggleSort(key) {
    if (key === sortKey) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    } else {
      setSortKey(key)
      setSortDir('desc')
    }
  }

  if (!rows.length) {
    return (
      <div className="notice">
        {hiddenCount > 0 ? (
          <>
            No {positions.join('/')} on this slate projects for {MIN_RELEVANT_FPTS}+ points
            ({hiddenCount} bench players hidden) -- upload RotoWire projections for a real read.
          </>
        ) : (
          <>
            No {positions.join('/')} data for this week yet -- upload a salary CSV (and ideally
            RotoWire projections) first.
          </>
        )}
      </div>
    )
  }

  return (
    <>
      <div className="controls" style={{ marginBottom: 12, flexWrap: 'wrap' }}>
        {slateGames.length > 0 && (
          <button onClick={() => setShowGames((s) => !s)}>
            {showGames ? 'Hide games' : 'Games'} ({includedGames.size} of {slateGames.length})
          </button>
        )}
        <input
          type="text"
          placeholder="Filter by player or team…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          style={{ minWidth: 220 }}
        />
        <SalaryRangeFilter
          min={minSalary}
          max={maxSalary}
          onMinChange={setMinSalary}
          onMaxChange={setMaxSalary}
          bounds={bounds}
        />
        <span className="dim" style={{ fontSize: 13 }}>
          {filtered.length} of {rows.length} players
          {hiddenCount > 0 && ` · ${hiddenCount} bench hidden (under ${MIN_RELEVANT_FPTS} pts)`}
        </span>
      </div>

      {showGames && (
        <div className="card" style={{ marginBottom: 14 }}>
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
                {g.time && (
                  <span className="dim" style={{ fontSize: 12 }}>
                    {g.time}
                  </span>
                )}
              </label>
            ))}
          </div>
        </div>
      )}

      <div className="card table-wrap">
        <table>
          <thead>
            <tr>
              <th className="sortable" style={{ width: 130 }} onClick={() => toggleSort('score')}>
                Matchup{sortKey === 'score' ? (sortDir === 'desc' ? ' ↓' : ' ↑') : ''}
              </th>
              <th className="sortable" onClick={() => toggleSort('name')}>
                Player{sortKey === 'name' ? (sortDir === 'desc' ? ' ↓' : ' ↑') : ''}
              </th>
              <th className="sortable" onClick={() => toggleSort('team')}>
                Team{sortKey === 'team' ? (sortDir === 'desc' ? ' ↓' : ' ↑') : ''}
              </th>
              <th className="num sortable" onClick={() => toggleSort('impliedTotal')}>
                Implied total{sortKey === 'impliedTotal' ? (sortDir === 'desc' ? ' ↓' : ' ↑') : ''}
              </th>
              <th>Opponent D / pace</th>
              <th className="num sortable" onClick={() => toggleSort('salary')}>
                Salary{sortKey === 'salary' ? (sortDir === 'desc' ? ' ↓' : ' ↑') : ''}
              </th>
              <th className="num sortable" onClick={() => toggleSort('fpts')}>
                Proj FPTS{sortKey === 'fpts' ? (sortDir === 'desc' ? ' ↓' : ' ↑') : ''}
              </th>
              <th className="num sortable" onClick={() => toggleSort('ownershipPct')}>
                Own%{sortKey === 'ownershipPct' ? (sortDir === 'desc' ? ' ↓' : ' ↑') : ''}
              </th>
              {hasInhouse && (
                <>
                  <th className="num sortable" onClick={() => toggleSort('inhouseFpts')} title="This app's own projection: real prior-season production scaled by today's matchup">
                    In-house{sortKey === 'inhouseFpts' ? (sortDir === 'desc' ? ' ↓' : ' ↑') : ''}
                  </th>
                  <th className="num sortable" onClick={() => toggleSort('inhouseOwnershipPct')} title="This app's own modelled ownership%">
                    In-house own%{sortKey === 'inhouseOwnershipPct' ? (sortDir === 'desc' ? ' ↓' : ' ↑') : ''}
                  </th>
                  <th className="num sortable" onClick={() => toggleSort('leverageScore')} title="Ceiling minus modelled ownership -- real upside the field is under-rostering">
                    Leverage{sortKey === 'leverageScore' ? (sortDir === 'desc' ? ' ↓' : ' ↑') : ''}
                  </th>
                </>
              )}
              <th>Biggest factor</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((r) => (
              <tr key={r.id}>
                <td>
                  <ScoreMeter score={r.score} />
                </td>
                <td>
                  <div className="name">{r.name}</div>
                  <div className="sub-line">{r.position}</div>
                </td>
                <td>
                  <span className="name">{r.team}</span>{' '}
                  <span className="dim">
                    {r.isHome ? 'vs' : '@'} {r.opponent}
                  </span>
                  {r.startTime && <div className="sub-line">{r.startTime}</div>}
                </td>
                <td className="num">{r.impliedTotal != null ? r.impliedTotal.toFixed(1) : '—'}</td>
                <td>
                  <div className="sub-line">{r.defenseDetail || '—'}</div>
                  <div className="sub-line dim" style={{ fontSize: 12 }}>
                    {r.paceDetail || ''}
                  </div>
                </td>
                <td className="num">
                  {r.salary != null ? `$${r.salary.toLocaleString()}` : '—'}
                  {r.avgPoints != null && <div className="sub-line">{r.avgPoints.toFixed(1)} avg pts</div>}
                </td>
                <td className="num">{r.fpts != null ? r.fpts.toFixed(1) : '—'}</td>
                <td className="num">{r.ownershipPct != null ? `${r.ownershipPct.toFixed(1)}%` : '—'}</td>
                {hasInhouse && (
                  <>
                    <td className="num">
                      {r.inhouseFpts != null ? r.inhouseFpts.toFixed(1) : '—'}
                      {r.inhouseCeiling != null && (
                        <div className="sub-line">{r.inhouseCeiling.toFixed(1)} ceil</div>
                      )}
                    </td>
                    <td className="num">
                      {r.inhouseOwnershipPct != null ? `${r.inhouseOwnershipPct.toFixed(1)}%` : '—'}
                    </td>
                    <td className="num">
                      {r.leverageScore != null ? r.leverageScore.toFixed(1) : '—'}
                    </td>
                  </>
                )}
                <td className="sub-line">{DRIVER_LABELS[r.driver] || r.driver || '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}
