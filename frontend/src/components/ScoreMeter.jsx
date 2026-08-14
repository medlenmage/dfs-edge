/**
 * A 0-100 matchup score, shown as a filled track plus the number.
 *
 * The score is a MAGNITUDE, so it gets a single-hue sequential ramp
 * (light -> dark blue) rather than a red/green traffic light. Green and
 * red are reserved for genuine status ("lineup confirmed", "rain risk"),
 * so they never get confused with "this guy scores 71".
 *
 * The number is always shown, so the colour is a scanning aid, never the
 * only way to read the value.
 */

const STEPS = [
  { max: 35, varName: '--seq-250' },
  { max: 50, varName: '--seq-350' },
  { max: 65, varName: '--seq-400' },
  { max: 80, varName: '--seq-500' },
  { max: 101, varName: '--seq-600' },
]

function stepFor(score) {
  return (STEPS.find((s) => score < s.max) || STEPS[STEPS.length - 1]).varName
}

export function ScoreMeter({ score, width }) {
  if (score === null || score === undefined) {
    return <span className="dim">—</span>
  }
  const pct = Math.max(0, Math.min(100, score))
  return (
    <div className="meter" style={width ? { width } : undefined}>
      <div
        className="track"
        role="img"
        aria-label={`Matchup score ${score} out of 100`}
      >
        <div
          className="fill"
          style={{ width: `${pct}%`, background: `var(${stepFor(pct)})` }}
        />
      </div>
      <span className="num">{Math.round(score)}</span>
    </div>
  )
}

export function ScoreLegend() {
  return (
    <div className="legend">
      <span>Matchup score</span>
      {[
        ['--seq-250', '0–35'],
        ['--seq-350', '35–50'],
        ['--seq-400', '50–65'],
        ['--seq-500', '65–80'],
        ['--seq-600', '80+'],
      ].map(([v, label]) => (
        <span key={label}>
          <span className="swatch" style={{ background: `var(${v})` }} />
          {label}
        </span>
      ))}
      <span className="dim">50 = league-average matchup</span>
    </div>
  )
}
