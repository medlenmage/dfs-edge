/**
 * A min/max DK-salary range control, shared by the MLB Hitters tab and
 * the NFL Players tab so both behave identically rather than growing
 * two slightly different versions of the same filter.
 *
 * Both bounds are optional -- an empty box means "no bound on that
 * side", so this is off by default and never silently hides anyone
 * until the user actually sets something. Placeholders show the real
 * salary range present in the current pool, which doubles as a cheap
 * way to see what a slate's prices actually look like before typing.
 */
export function SalaryRangeFilter({ min, max, onMinChange, onMaxChange, bounds }) {
  return (
    <label className="dim" style={{ fontSize: 13, display: 'inline-flex', alignItems: 'center', gap: 6 }}>
      Salary{' '}
      <input
        type="number"
        inputMode="numeric"
        value={min}
        onChange={(e) => onMinChange(e.target.value)}
        placeholder={bounds?.lo != null ? `${bounds.lo}` : 'min'}
        title="Minimum DK salary -- leave blank for no lower bound"
        style={{ width: 78 }}
      />
      <span aria-hidden="true">–</span>
      <input
        type="number"
        inputMode="numeric"
        value={max}
        onChange={(e) => onMaxChange(e.target.value)}
        placeholder={bounds?.hi != null ? `${bounds.hi}` : 'max'}
        title="Maximum DK salary -- leave blank for no upper bound"
        style={{ width: 78 }}
      />
      {(min !== '' || max !== '') && (
        <button
          onClick={() => {
            onMinChange('')
            onMaxChange('')
          }}
          title="Clear the salary range"
          style={{ padding: '2px 8px' }}
        >
          clear
        </button>
      )}
    </label>
  )
}

/**
 * Whether one player's salary passes the range. A player with no
 * salary at all (no DK CSV loaded, or an unmatched name) passes while
 * the filter is unset, but is excluded once either bound is set --
 * an unknown salary genuinely can't be shown to satisfy a real range,
 * and quietly keeping them would misrepresent the filter.
 */
export function withinSalaryRange(salary, min, max) {
  const lo = min === '' ? null : Number(min)
  const hi = max === '' ? null : Number(max)
  if (lo == null && hi == null) return true
  if (salary == null) return false
  if (lo != null && !Number.isNaN(lo) && salary < lo) return false
  if (hi != null && !Number.isNaN(hi) && salary > hi) return false
  return true
}

/** The real min/max salary present in a set of rows, for placeholders. */
export function salaryBounds(rows) {
  const salaries = rows.map((r) => r.salary).filter((s) => s != null)
  if (!salaries.length) return null
  return { lo: Math.min(...salaries), hi: Math.max(...salaries) }
}
