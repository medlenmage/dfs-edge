// Client-side CSV export for a batch of lineups already in memory (the
// optimizer's Lineups tab: at most 150, always fully loaded already,
// so no server round-trip is needed to export all of it). The mass
// Contest Generator downloads its own CSV from the backend instead,
// since only a 200-row preview of a batch that can run to 10,000 ever
// makes it into the browser.

const SLOT_ORDER = ['P', 'P', 'C', '1B', '2B', '3B', 'SS', 'OF', 'OF', 'OF']

function slotLabels() {
  const counts = {}
  return SLOT_ORDER.map((slot) => {
    const total = SLOT_ORDER.filter((s) => s === slot).length
    counts[slot] = (counts[slot] || 0) + 1
    return total > 1 ? `${slot}${counts[slot]}` : slot
  })
}

function csvCell(value) {
  if (value == null) return ''
  const s = String(value)
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
}

/** Same stack-shape derivation as the backend's lineup_export.stack_info():
 * any team supplying 2+ of the 8 non-pitcher slots is a "stack", ordered
 * largest group first (ties broken by first appearance in roster order). */
function stackInfo(lineup) {
  const teamsSeen = []
  const counts = {}
  for (const slot of Object.keys(lineup.slots)) {
    if (slot === 'P') continue
    for (const player of lineup.slots[slot]) {
      const team = player.team
      if (!(team in counts)) teamsSeen.push(team)
      counts[team] = (counts[team] || 0) + 1
    }
  }
  const groups = teamsSeen.filter((t) => counts[t] >= 2).map((t) => [counts[t], t])
  groups.sort((a, b) => b[0] - a[0])
  return { stackType: groups.map(([size]) => size).join('-'), stack: groups.map(([, team]) => team).join(',') }
}

/** Same column shape as the backend's lineup_export.lineups_to_csv(), so a
 * downloaded optimizer CSV and a downloaded contest-generator CSV line up. */
export function lineupsToCsv(lineups) {
  const labels = slotLabels()
  const header = ['lineup_index', 'salary_used', 'stack_type', 'stack', 'projected_points', 'total_ownership_pct']
  for (const label of labels) {
    header.push(`${label}_name`)
  }

  const rows = [header]
  lineups.forEach((lineup, i) => {
    const used = {}
    const { stackType, stack } = stackInfo(lineup)
    // Leading apostrophe forces Excel to keep this cell as text -- without
    // it, a value like "5-3" gets auto-read as a date (March 5th) on open.
    const row = [
      i,
      lineup.salary_used,
      stackType ? `'${stackType}` : '',
      stack,
      lineup.projected_points,
      lineup.total_ownership_pct,
    ]
    for (const label of labels) {
      const slot = label.replace(/\d+$/, '')
      const idx = used[slot] || 0
      used[slot] = idx + 1
      const player = (lineup.slots[slot] || [])[idx]
      row.push(player?.name ?? '')
    }
    rows.push(row)
  })

  return rows.map((row) => row.map(csvCell).join(',')).join('\r\n')
}

export function downloadCsv(filename, csvText) {
  const blob = new Blob([csvText], { type: 'text/csv;charset=utf-8;' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}
