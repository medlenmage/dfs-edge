import { useMemo, useState } from 'react'

/**
 * Every player the optimizer could actually roster today (has both a
 * matched salary and projection) -- lock one into every generated
 * lineup, or exclude them entirely. Mirrors the eligibility filter in
 * optimizer.py's build_player_pool() so nothing shown here is a player
 * the backend would silently skip anyway.
 *
 * Both projection sources are shown side by side rather than one at a
 * time, because the decision the pool is actually used for is often
 * "where do these two disagree" -- a player RotoWire likes and the
 * in-house model doesn't is exactly the one worth a second look, and
 * that comparison is invisible if you have to toggle between them.
 * In-house columns only carry numbers once the slate was fetched with
 * inhouse=true; they read as em-dashes otherwise rather than as zeros,
 * so "not computed" never looks like "projected at nothing".
 */

// DK's roster slots, in the order a lineup is built. A player's
// position string can name several ("1B/OF"), so a tab matches when the
// slot appears anywhere in it -- filtering by 1B has to keep the
// 1B/OF-eligible bat, which is precisely the player these tabs exist
// to help find.
const MLB_SLOTS = ['P', 'C', '1B', '2B', '3B', 'SS', 'OF']

function eligibleFor(position, slot) {
  return (position || '')
    .split('/')
    .map((s) => s.trim().toUpperCase())
    .some((s) => (slot === 'P' ? s === 'P' || s === 'SP' || s === 'RP' : s === slot))
}

function num(value, digits = 1) {
  return value == null ? <span className="dim">—</span> : value.toFixed(digits)
}

export function PlayerPoolTable({
  slate,
  locked,
  excluded,
  onToggleLock,
  onToggleExclude,
  oneOff,
  onToggleOneOff,
  showOneOff = false,
}) {
  const [sortKey, setSortKey] = useState('fpts')
  const [sortDir, setSortDir] = useState('desc')
  const [search, setSearch] = useState('')
  const [slot, setSlot] = useState('ALL')

  const rows = useMemo(() => {
    const out = []
    // A team playing a doubleheader appears in two slate games, so each
    // of its players would otherwise be pushed twice. Beyond showing a
    // duplicate row, two rows sharing a React key make the list
    // impossible to reconcile -- filtering to a position left stale rows
    // from the previous render on screen. One player, one row.
    const seen = new Set()
    for (const g of slate?.games || []) {
      for (const side of ['home', 'away']) {
        const team = g[side]
        const candidates = [...(team.hitters || [])]
        if (team.probable_pitcher) candidates.push(team.probable_pitcher)
        for (const p of candidates) {
          if (p.salary?.salary == null || p.projection?.fpts == null) continue
          if (seen.has(p.id)) continue
          seen.add(p.id)
          const proj = p.projection || {}
          out.push({
            id: p.id,
            name: p.name,
            team: team.abbrev,
            position: p.salary.position || '',
            salary: p.salary.salary,
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

  const slotCounts = useMemo(() => {
    const counts = { ALL: rows.length }
    for (const s of MLB_SLOTS) counts[s] = rows.filter((r) => eligibleFor(r.position, s)).length
    return counts
  }, [rows])

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    const list = rows.filter(
      (r) =>
        (slot === 'ALL' || eligibleFor(r.position, slot)) &&
        (!q || r.name?.toLowerCase().includes(q) || r.team?.toLowerCase().includes(q)),
    )
    const dir = sortDir === 'asc' ? 1 : -1
    list.sort((a, b) => {
      const av = a[sortKey]
      const bv = b[sortKey]
      if (typeof av === 'string') return av.localeCompare(bv) * dir
      // A missing number sorts last in either direction -- an unscored
      // player is not the best or the worst, he is unknown.
      if (av == null && bv == null) return 0
      if (av == null) return 1
      if (bv == null) return -1
      return (av - bv) * dir
    })
    return list
  }, [rows, sortKey, sortDir, search, slot])

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
        No optimizable players yet — upload a salary and a projections CSV for this date.
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
            className={slot === 'ALL' ? 'active' : ''}
            onClick={() => setSlot('ALL')}
            title="Every optimizable player"
          >
            All<span className="count">{slotCounts.ALL}</span>
          </button>
          {MLB_SLOTS.map((s) => (
            <button
              key={s}
              className={slot === s ? 'active' : ''}
              onClick={() => setSlot(s)}
              disabled={!slotCounts[s]}
              title={`Players eligible at ${s} — includes multi-position players like 1B/OF`}
            >
              {s}
              <span className="count">{slotCounts[s]}</span>
            </button>
          ))}
        </div>
      </div>

      <div className="controls dim" style={{ marginBottom: 8, fontSize: 12.5 }}>
        {filtered.length} of {rows.length} players
        {slot !== 'ALL' && ` · ${slot} only`}
        {locked.size > 0 && ` · ${locked.size} locked`}
        {excluded.size > 0 && ` · ${excluded.size} excluded`}
        {showOneOff && oneOff.size > 0 && ` · ${oneOff.size} one-off eligible`}
      </div>

      <div className="card table-wrap" style={{ maxHeight: 460, overflowY: 'auto' }}>
        <table className="compact">
          <thead>
            <tr>
              <th>Lock / exclude{showOneOff ? ' / one-off' : ''}</th>
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
                    {showOneOff && (
                      <button
                        className={`pill-toggle lock${oneOff.has(r.id) ? ' active' : ''}`}
                        onClick={() => onToggleOneOff(r.id)}
                      >
                        {oneOff.has(r.id) ? '✓ one-off' : 'one-off'}
                      </button>
                    )}
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
