export function StatTile({ label, value, sub }) {
  return (
    <div className="card tile">
      <div className="label">{label}</div>
      <div className="value">{value ?? '—'}</div>
      {sub ? <div className="sub">{sub}</div> : null}
    </div>
  )
}

/**
 * The slate's headline numbers, computed once and shared by both the
 * tile row and the redesign's slate-bar KPI strip -- two renderings of
 * the same facts, so they can't drift apart.
 */
export function slateSummary(slate) {
  const allGames = slate?.games || []
  if (!allGames.length) return null

  // Same auto-detect-from-the-loaded-DK-slate pattern the tables use:
  // once a DK slate is loaded these should reflect just that slate,
  // falling back to every game when nothing is flagged in-slate yet.
  const inSlateGames = allGames.filter((g) => g.in_slate !== false)
  const games = inSlateGames.length ? inSlateGames : allGames

  const totals = games.map((g) => g.betting?.total).filter((t) => typeof t === 'number')
  const avgTotal = totals.length
    ? (totals.reduce((a, b) => a + b, 0) / totals.length).toFixed(1)
    : null

  let bestTeam = null
  let bestStack = null
  for (const g of games) {
    for (const side of ['home', 'away']) {
      const t = g[side]
      if (t.implied_runs != null && (!bestTeam || t.implied_runs > bestTeam.runs)) {
        bestTeam = { name: t.abbrev || t.name, runs: t.implied_runs, venue: g.venue?.name }
      }
      if (t.stack_score != null && (!bestStack || t.stack_score > bestStack.score)) {
        bestStack = { name: t.abbrev || t.name, score: t.stack_score }
      }
    }
  }

  const confirmed = games.reduce(
    (n, g) => n + (g.home.lineup_confirmed ? 1 : 0) + (g.away.lineup_confirmed ? 1 : 0),
    0,
  )
  return { games, avgTotal, bestTeam, bestStack, confirmed }
}

export function SlateTiles({ slate }) {
  const summary = slateSummary(slate)
  if (!summary) return null
  const { games, avgTotal, bestTeam, bestStack, confirmed } = summary

  return (
    <div className="tiles">
      <StatTile label="Games on the slate" value={games.length} />
      <StatTile
        label="Average game total"
        value={avgTotal ?? '—'}
        sub={avgTotal ? 'combined runs, Vegas' : 'no FantasyLabs line yet'}
      />
      <StatTile
        label="Highest implied team"
        value={bestTeam ? bestTeam.runs.toFixed(1) : '—'}
        sub={bestTeam ? `${bestTeam.name} at ${bestTeam.venue}` : undefined}
      />
      <StatTile
        label="Best stack"
        value={bestStack ? Math.round(bestStack.score) : '—'}
        sub={bestStack ? bestStack.name : undefined}
      />
      <StatTile
        label="Lineups confirmed"
        value={`${confirmed}/${games.length * 2}`}
        sub={confirmed === 0 ? 'usually posted 2–4 hrs out' : undefined}
      />
    </div>
  )
}
