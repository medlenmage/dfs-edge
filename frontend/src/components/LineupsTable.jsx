const SALARY_CAP = 50_000

// Display order for the 10 DK Classic MLB slots -- P and OF appear more
// than once since the roster needs two pitchers and three outfielders.
const SLOT_ORDER = ['P', 'P', 'C', '1B', '2B', '3B', 'SS', 'OF', 'OF', 'OF']

/**
 * One generated DraftKings Classic MLB lineup: the 10 roster slots,
 * salary used against the $50,000 cap, and total projected points.
 */
export function LineupsTable({ lineup }) {
  const remaining = SALARY_CAP - lineup.salary_used

  // Slots come back grouped by type (e.g. slots.OF = [p1, p2, p3]) --
  // flatten them into the fixed display order above, tracking how many
  // of each type we've already placed so multi-slot types don't repeat.
  const used = {}
  const rows = SLOT_ORDER.map((slotType, i) => {
    const idx = used[slotType] || 0
    used[slotType] = idx + 1
    const player = (lineup.slots[slotType] || [])[idx]
    return { key: `${slotType}-${i}`, slotType, player }
  })

  return (
    <div className="card table-wrap">
      <div style={{ display: 'flex', gap: 16, marginBottom: 12, flexWrap: 'wrap' }}>
        <span className="badge ok">
          ${lineup.salary_used.toLocaleString()} used / ${remaining.toLocaleString()} left
        </span>
        <span className="badge">{lineup.projected_points.toFixed(1)} projected points</span>
        {lineup.total_ownership_pct != null && (
          <span className="badge">{lineup.total_ownership_pct.toFixed(1)}% cumulative ownership</span>
        )}
        {lineup.duplicate_count > 1 && (
          <span
            className="badge"
            title="This exact lineup (same 10 players) appears more than once in the generated set -- a real DK contest would tie and split whatever payout the ranks it occupies are worth."
          >
            ×{lineup.duplicate_count} duplicate
          </span>
        )}
      </div>
      <table>
        <thead>
          <tr>
            <th>Slot</th>
            <th>Player</th>
            <th>Team</th>
            <th className="num">Salary</th>
            <th className="num">Proj FPTS</th>
            <th className="num">Own%</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key}>
              <td className="name">{r.slotType}</td>
              <td>{r.player?.name || <span className="dim">—</span>}</td>
              <td className="dim">{r.player?.team || '—'}</td>
              <td className="num">
                {r.player ? `$${r.player.salary.toLocaleString()}` : '—'}
              </td>
              <td className="num">
                {r.player ? r.player.projected_fpts.toFixed(1) : '—'}
              </td>
              <td className="num">
                {r.player?.ownership_pct != null ? `${r.player.ownership_pct.toFixed(1)}%` : '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
