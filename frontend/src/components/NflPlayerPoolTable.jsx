import { useMemo, useState } from 'react'

/**
 * Every player the NFL optimizer could actually roster this week (has
 * both a matched salary and projection) -- lock one into every
 * generated lineup, or exclude them entirely. Mirrors the eligibility
 * filter in nfl_optimizer.py's build_player_pool() so nothing shown
 * here is a player the backend would silently skip anyway.
 *
 * Both projection sources sit side by side rather than behind a toggle,
 * because the question the pool gets used for is often "where do these
 * two disagree" -- and that comparison is invisible if you have to
 * switch between them. In-house columns only carry numbers when the
 * slate was fetched with in-house projections computed; otherwise they
 * read as em-dashes, so "not computed" never looks like "projected at
 * nothing".
 */

// FLEX is a roster slot rather than a position, so it is a filter here
// and not a tab that any player "is". Filtering to it answers the
// question actually being asked -- who can fill the flex -- which is
// every back, receiver and tight end at once.
const NFL_TABS = [
  { key: 'QB', label: 'QB', match: (p) => p === 'QB' },
  { key: 'RB', label: 'RB', match: (p) => p === 'RB' },
  { key: 'WR', label: 'WR', match: (p) => p === 'WR' },
  { key: 'TE', label: 'TE', match: (p) => p === 'TE' },
  {
    key: 'FLEX',
    label: 'FLEX',
    match: (p) => p === 'RB' || p === 'WR' || p === 'TE',
    title: 'Everyone eligible for the flex — every RB, WR and TE',
  },
  { key: 'DST', label: 'DST', match: (p) => p === 'DST' },
]

function num(value, digits = 1) {
  return value == null ? <span className="dim">—</span> : value.toFixed(digits)
}

export function NflPlayerPoolTable({ slate, locked, excluded, onToggleLock, onToggleExclude }) {
  const [sortKey, setSortKey] = useState('fpts')
  const [sortDir, setSortDir] = useState('desc')
  const [search, setSearch] = useState('')
  const [tab, setTab] = useState('ALL')

  const rows = useMemo(() => {
    const out = []
    for (const g of slate?.games || []) {
      for (const side of ['home', 'away']) {
        const team = g[side]
        for (const p of team.players || []) {
          if (!p.dk_id || p.salary == null || p.projection?.fpts == null) continue
          const proj = p.projection || {}
          out.push({
            id: p.dk_id,
            name: p.name,
            team: team.abbrev,
            position: (p.position || '').toUpperCase(),
            salary: p.salary,
            fpts: proj.fpts,
            inhouseFpts: proj.inhouse_fpts ?? null,
            own: proj.ownership_pct ?? null,
            inhouseOwn: proj.inhouse_ownership_pct ?? null,
          })
        }
      }
    }
    return out
  }, [slate])

  const counts = useMemo(() => {
    const out = { ALL: rows.length }
    for (const t of NFL_TABS) out[t.key] = rows.filter((r) => t.match(r.position)).length
    return out
  }, [rows])

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    const active = NFL_TABS.find((t) => t.key === tab)
    const list = rows.filter(
      (r) =>
        (!active || active.match(r.position)) &&
        (!q || r.name?.toLowerCase().includes(q) || r.team?.toLowerCase().includes(q)),
    )
    const dir = sortDir === 'asc' ? 1 : -1
    list.sort((a, b) => {
      const av = a[sortKey]
      const bv = b[sortKey]
      if (typeof av === 'string') return av.localeCompare(bv) * dir
      // A missing number sorts last either way -- an unscored player is
      // not the best or the worst, he is unknown.
      if (av == null && bv == null) return 0
      if (av == null) return 1
      if (bv == null) return -1
      return (av - bv) * dir
    })
    return list
  }, [rows, sortKey, sortDir, search, tab])

  function toggleSort(key) {
    if (key === sortKey) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    } else {
      setSortKey(key)
      setSortDir('desc')
    }
  }

  function arrow(key) {
    return sortKey === key ? (sortDir === 'desc' ? ' ↓' : ' ↑') : ''
  }

  if (!rows.length) {
    return (
      <div className="notice">
        No optimizable players yet — load a DraftKings slate and projections for this week.
      </div>
    )
  }

  return (
    <>
      <div className="controls" style={{ marginBottom: 8, gap: 10 }}>
        <input
          type="text"
          placeholder="Filter by player or team…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          style={{ minWidth: 200 }}
        />
        <div className="pos-tabs">
          <button
            className={tab === 'ALL' ? 'active' : ''}
            onClick={() => setTab('ALL')}
            title="Every optimizable player"
          >
            All<span className="count">{counts.ALL}</span>
          </button>
          {NFL_TABS.map((t) => (
            <button
              key={t.key}
              className={tab === t.key ? 'active' : ''}
              onClick={() => setTab(t.key)}
              disabled={!counts[t.key]}
              title={t.title || `Players at ${t.label}`}
            >
              {t.label}
              <span className="count">{counts[t.key]}</span>
            </button>
          ))}
        </div>
      </div>

      <div className="controls dim" style={{ marginBottom: 8, fontSize: 12.5 }}>
        {filtered.length} of {rows.length} players
        {tab !== 'ALL' && ` · ${tab} only`}
        {locked.size > 0 && ` · ${locked.size} locked`}
        {excluded.size > 0 && ` · ${excluded.size} excluded`}
      </div>

      <div className="card table-wrap" style={{ maxHeight: 460, overflowY: 'auto' }}>
        <table className="compact">
          <thead>
            <tr>
              <th>Lock / exclude</th>
              <th className="sortable" onClick={() => toggleSort('name')}>
                Player{arrow('name')}
              </th>
              <th className="sortable" onClick={() => toggleSort('team')}>
                Team{arrow('team')}
              </th>
              <th>Pos</th>
              <th className="num sortable" onClick={() => toggleSort('salary')}>
                Salary{arrow('salary')}
              </th>
              <th className="num sortable" onClick={() => toggleSort('fpts')} title="RotoWire's projected fantasy points">
                RW proj{arrow('fpts')}
              </th>
              <th
                className="num sortable"
                onClick={() => toggleSort('inhouseFpts')}
                title="This app's own projected fantasy points, from real game logs. Blank unless the slate was loaded with in-house projections computed."
              >
                IH proj{arrow('inhouseFpts')}
              </th>
              <th className="num sortable" onClick={() => toggleSort('own')} title="RotoWire's projected rostership">
                RW own{arrow('own')}
              </th>
              <th
                className="num sortable"
                onClick={() => toggleSort('inhouseOwn')}
                title="This app's own modelled rostership. Blank unless in-house projections were computed."
              >
                IH own{arrow('inhouseOwn')}
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
                <td className="num">{num(r.fpts)}</td>
                <td className="num">{num(r.inhouseFpts)}</td>
                <td className="num">{num(r.own)}</td>
                <td className="num">{num(r.inhouseOwn)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}
