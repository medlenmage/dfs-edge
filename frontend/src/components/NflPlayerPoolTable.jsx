import { useMemo, useState } from 'react'

/**
 * Every player the NFL optimizer could actually roster this week (has
 * both a matched salary and projection) -- lock one into every
 * generated lineup, or exclude them entirely. Mirrors the eligibility
 * filter in nfl_optimizer.py's build_player_pool() so nothing shown
 * here is a player the backend would silently skip anyway.
 */
export function NflPlayerPoolTable({ slate, locked, excluded, onToggleLock, onToggleExclude }) {
  const [sortKey, setSortKey] = useState('fpts')
  const [sortDir, setSortDir] = useState('desc')
  const [search, setSearch] = useState('')

  const rows = useMemo(() => {
    const out = []
    for (const g of slate?.games || []) {
      for (const side of ['home', 'away']) {
        const team = g[side]
        for (const p of team.players || []) {
          if (!p.dk_id || p.salary == null || p.projection?.fpts == null) continue
          out.push({
            id: p.dk_id,
            name: p.name,
            team: team.abbrev,
            position: p.position || '',
            salary: p.salary,
            fpts: p.projection.fpts,
          })
        }
      }
    }
    return out
  }, [slate])

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    const list = rows.filter(
      (r) => !q || r.name?.toLowerCase().includes(q) || r.team?.toLowerCase().includes(q),
    )
    const dir = sortDir === 'asc' ? 1 : -1
    list.sort((a, b) => {
      const av = a[sortKey]
      const bv = b[sortKey]
      if (typeof av === 'string') return av.localeCompare(bv) * dir
      return (av - bv) * dir
    })
    return list
  }, [rows, sortKey, sortDir, search])

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
        No optimizable players yet — upload a salary and a projections CSV for this week.
      </div>
    )
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
        <span className="dim" style={{ fontSize: 13 }}>
          {filtered.length} of {rows.length} players
          {locked.size > 0 && ` · ${locked.size} locked`}
          {excluded.size > 0 && ` · ${excluded.size} excluded`}
        </span>
      </div>

      <div className="card table-wrap" style={{ maxHeight: 420, overflowY: 'auto' }}>
        <table>
          <thead>
            <tr>
              <th>Lock / exclude</th>
              <th className="sortable" onClick={() => toggleSort('name')}>
                Player{sortKey === 'name' ? (sortDir === 'desc' ? ' ↓' : ' ↑') : ''}
              </th>
              <th className="sortable" onClick={() => toggleSort('team')}>
                Team{sortKey === 'team' ? (sortDir === 'desc' ? ' ↓' : ' ↑') : ''}
              </th>
              <th>Position</th>
              <th className="num sortable" onClick={() => toggleSort('salary')}>
                Salary{sortKey === 'salary' ? (sortDir === 'desc' ? ' ↓' : ' ↑') : ''}
              </th>
              <th className="num sortable" onClick={() => toggleSort('fpts')}>
                Proj FPTS{sortKey === 'fpts' ? (sortDir === 'desc' ? ' ↓' : ' ↑') : ''}
              </th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((r) => (
              <tr key={r.id}>
                <td>
                  <div style={{ display: 'flex', gap: 4 }}>
                    <button
                      className={`pill-toggle lock${locked.has(r.id) ? ' active' : ''}`}
                      disabled={excluded.has(r.id)}
                      onClick={() => onToggleLock(r.id)}
                    >
                      {locked.has(r.id) ? '✓ locked' : 'lock'}
                    </button>
                    <button
                      className={`pill-toggle exclude${excluded.has(r.id) ? ' active' : ''}`}
                      disabled={locked.has(r.id)}
                      onClick={() => onToggleExclude(r.id)}
                    >
                      {excluded.has(r.id) ? '✓ excluded' : 'exclude'}
                    </button>
                  </div>
                </td>
                <td className="name">{r.name}</td>
                <td className="dim">{r.team}</td>
                <td className="dim">{r.position}</td>
                <td className="num">${r.salary.toLocaleString()}</td>
                <td className="num">{r.fpts.toFixed(1)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}
