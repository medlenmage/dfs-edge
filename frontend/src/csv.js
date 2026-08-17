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

/** Same column shape as the backend's lineup_export.lineups_to_csv(), so a
 * downloaded optimizer CSV and a downloaded contest-generator CSV line up. */
export function lineupsToCsv(lineups) {
  const labels = slotLabels()
  const header = ['lineup_index', 'salary_used', 'projected_points', 'total_ownership_pct']
  for (const label of labels) {
    header.push(`${label}_name`, `${label}_team`, `${label}_salary`, `${label}_proj_fpts`, `${label}_own_pct`)
  }

  const rows = [header]
  lineups.forEach((lineup, i) => {
    const used = {}
    const row = [i, lineup.salary_used, lineup.projected_points, lineup.total_ownership_pct]
    for (const label of labels) {
      const slot = label.replace(/\d+$/, '')
      const idx = used[slot] || 0
      used[slot] = idx + 1
      const player = (lineup.slots[slot] || [])[idx]
      row.push(
        player?.name ?? '',
        player?.team ?? '',
        player?.salary ?? '',
        player?.projected_fpts ?? '',
        player?.ownership_pct ?? '',
      )
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
