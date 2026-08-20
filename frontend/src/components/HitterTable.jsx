import { useEffect, useMemo, useState } from 'react'
import { ScoreMeter } from './ScoreMeter'
import { localTime } from '../format'

const COLUMNS = [
  { key: 'score', label: 'Matchup', sortable: true, width: 130 },
  { key: 'name', label: 'Hitter', sortable: true },
  { key: 'team', label: 'Team', sortable: true },
  { key: 'opposing_pitcher', label: 'vs Pitcher', sortable: false },
  { key: 'vs_hand_ops', label: 'OPS vs hand', sortable: true, num: true },
  { key: 'season_ops', label: 'Season OPS', sortable: true, num: true },
  { key: 'sb', label: 'SB', sortable: true, num: true },
  { key: 'xwoba', label: 'xwOBA', sortable: true, num: true },
  { key: 'implied_runs', label: 'Team runs', sortable: true, num: true },
  { key: 'salary', label: 'Salary', sortable: true, num: true },
  { key: 'value', label: 'Value', sortable: true, num: true },
  { key: 'fpts', label: 'Proj FPTS', sortable: true, num: true },
  { key: 'ownershipPct', label: 'Own%', sortable: true, num: true },
  { key: 'inhouseFpts', label: 'In-house FPTS', sortable: true, num: true },
  { key: 'inhouseOwnershipPct', label: 'In-house Own%', sortable: true, num: true },
  { key: 'leverage', label: 'Leverage', sortable: true, num: true },
  { key: 'why', label: 'Biggest factor', sortable: false },
]

const DRIVER_LABELS = {
  platoon: 'his platoon split',
  pitcher: 'the pitcher',
  team_total: 'Vegas total',
  contact_quality: 'his contact quality',
  stolen_base: 'his stolen-base rate',
  bullpen: 'the opposing bullpen',
  park: 'the ballpark',
  weather: 'weather',
  form: 'recent form',
  home_road: 'home/road split',
}

export function HitterTable({ slate, limit = 50 }) {
  const [sortKey, setSortKey] = useState('score')
  const [sortDir, setSortDir] = useState('desc')
  const [minScore, setMinScore] = useState(0)
  const [search, setSearch] = useState('')
  const [showGames, setShowGames] = useState(false)
  const [includedGames, setIncludedGames] = useState(new Set())

  // Same auto-detect-from-uploaded-DK-salary-CSV pattern already used by
  // the Stacks/Lineups/Contest Generator tabs' own slate-game checklists.
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

  const rows = useMemo(() => {
    const out = []
    for (const g of slate?.games || []) {
      for (const side of ['home', 'away']) {
        const team = g[side]
        const opp = g[side === 'home' ? 'away' : 'home']
        for (const h of team.hitters || []) {
          out.push({
            id: h.id,
            game_pk: g.game_pk,
            name: h.name,
            position: h.position,
            bats: h.bats,
            order: h.batting_order,
            team: team.abbrev,
            opponent: opp.abbrev,
            isHome: side === 'home',
            score: h.edge.score,
            driver: h.edge.top_driver,
            opposing_pitcher: opp.probable_pitcher?.name,
            pitcher_hand: opp.probable_pitcher?.throws,
            season_ops: h.season?.ops ?? null,
            season_pa: h.season?.pa ?? null,
            sb: h.season?.sb ?? null,
            vs_hand_ops: h.vs_hand?.ops ?? null,
            vs_hand_pa: h.vs_hand?.pa ?? null,
            xwoba: h.edge.components.contact_quality?.xwoba ?? null,
            barrel_pct: h.edge.components.contact_quality?.barrel_pct ?? null,
            implied_runs: team.implied_runs ?? null,
            salary: h.salary?.salary ?? null,
            value: h.salary?.value ?? null,
            avgPoints: h.salary?.avg_points ?? null,
            fpts: h.projection?.fpts ?? null,
            ownershipPct: h.projection?.ownership_pct ?? null,
            inhouseFpts: h.projection?.inhouse_fpts ?? null,
            inhouseOwnershipPct: h.projection?.inhouse_ownership_pct ?? null,
            leverage: h.projection?.leverage_score ?? null,
            confirmed: team.lineup_confirmed,
            platoonDetail: h.edge.components.platoon.detail,
          })
        }
      }
    }
    return out
  }, [slate])

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    const list = rows.filter(
      (r) =>
        (!slateGames.length || includedGames.has(r.game_pk)) &&
        r.score >= minScore &&
        (!q ||
          r.name?.toLowerCase().includes(q) ||
          r.team?.toLowerCase().includes(q)),
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
  }, [rows, sortKey, sortDir, minScore, search, limit, includedGames, slateGames])

  function toggleSort(key) {
    if (key === sortKey) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    } else {
      setSortKey(key)
      setSortDir('desc')
    }
  }

  if (!rows.length) {
    return <div className="notice">No hitter data for this date yet.</div>
  }

  return (
    <>
      <div className="controls" style={{ marginBottom: 12 }}>
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
        <label className="dim" style={{ fontSize: 13 }}>
          Min score{' '}
          <select
            value={minScore}
            onChange={(e) => setMinScore(Number(e.target.value))}
          >
            {[0, 50, 60, 65, 70, 75].map((v) => (
              <option key={v} value={v}>
                {v || 'any'}
              </option>
            ))}
          </select>
        </label>
        <span className="dim" style={{ fontSize: 13 }}>
          {filtered.length} of {rows.length} hitters
        </span>
      </div>

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

      <div className="card table-wrap">
        <table>
          <thead>
            <tr>
              {COLUMNS.map((c) => (
                <th
                  key={c.key}
                  className={c.sortable ? 'sortable' : undefined}
                  style={c.width ? { width: c.width } : undefined}
                  onClick={c.sortable ? () => toggleSort(c.key) : undefined}
                >
                  {c.label}
                  {sortKey === c.key ? (sortDir === 'desc' ? ' ↓' : ' ↑') : ''}
                </th>
              ))}
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
                  <div className="sub-line">
                    {r.position} · bats {r.bats}
                    {r.order ? ` · batting ${r.order}` : ''}
                  </div>
                </td>
                <td>
                  <span className="name">{r.team}</span>{' '}
                  <span className="dim">
                    {r.isHome ? 'vs' : '@'} {r.opponent}
                  </span>
                </td>
                <td>
                  <div>{r.opposing_pitcher || <span className="dim">TBD</span>}</div>
                  {r.pitcher_hand && (
                    <div className="sub-line">{r.pitcher_hand}HP</div>
                  )}
                </td>
                <td className="num">
                  {r.vs_hand_ops != null ? r.vs_hand_ops.toFixed(3) : '—'}
                  {r.vs_hand_pa != null && (
                    <div className="sub-line">{r.vs_hand_pa} PA</div>
                  )}
                </td>
                <td className="num">
                  {r.season_ops != null ? r.season_ops.toFixed(3) : '—'}
                  {r.season_pa != null && (
                    <div className="sub-line">{r.season_pa} PA</div>
                  )}
                </td>
                <td className="num">{r.sb ?? '—'}</td>
                <td className="num">
                  {r.xwoba != null ? r.xwoba.toFixed(3) : '—'}
                  {r.barrel_pct != null && (
                    <div className="sub-line">{r.barrel_pct}% barrels</div>
                  )}
                </td>
                <td className="num">
                  {r.implied_runs != null ? r.implied_runs.toFixed(1) : '—'}
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
                <td className="sub-line">
                  {DRIVER_LABELS[r.driver] || r.driver || '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}
