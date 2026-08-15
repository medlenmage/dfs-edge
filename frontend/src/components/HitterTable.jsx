import { useMemo, useState } from 'react'
import { ScoreMeter } from './ScoreMeter'

const COLUMNS = [
  { key: 'score', label: 'Matchup', sortable: true, width: 130 },
  { key: 'name', label: 'Hitter', sortable: true },
  { key: 'team', label: 'Team', sortable: true },
  { key: 'opposing_pitcher', label: 'vs Pitcher', sortable: false },
  { key: 'vs_hand_ops', label: 'OPS vs hand', sortable: true, num: true },
  { key: 'season_ops', label: 'Season OPS', sortable: true, num: true },
  { key: 'xwoba', label: 'xwOBA', sortable: true, num: true },
  { key: 'implied_runs', label: 'Team runs', sortable: true, num: true },
  { key: 'why', label: 'Biggest factor', sortable: false },
]

const DRIVER_LABELS = {
  platoon: 'his platoon split',
  pitcher: 'the pitcher',
  team_total: 'Vegas total',
  contact_quality: 'his contact quality',
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

  const rows = useMemo(() => {
    const out = []
    for (const g of slate?.games || []) {
      for (const side of ['home', 'away']) {
        const team = g[side]
        const opp = g[side === 'home' ? 'away' : 'home']
        for (const h of team.hitters || []) {
          out.push({
            id: h.id,
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
            vs_hand_ops: h.vs_hand?.ops ?? null,
            vs_hand_pa: h.vs_hand?.pa ?? null,
            xwoba: h.edge.components.contact_quality?.xwoba ?? null,
            barrel_pct: h.edge.components.contact_quality?.barrel_pct ?? null,
            implied_runs: team.implied_runs ?? null,
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
  }, [rows, sortKey, sortDir, minScore, search, limit])

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
                <td className="num">
                  {r.xwoba != null ? r.xwoba.toFixed(3) : '—'}
                  {r.barrel_pct != null && (
                    <div className="sub-line">{r.barrel_pct}% barrels</div>
                  )}
                </td>
                <td className="num">
                  {r.implied_runs != null ? r.implied_runs.toFixed(1) : '—'}
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
