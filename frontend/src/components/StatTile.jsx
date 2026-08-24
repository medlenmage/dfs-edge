export function StatTile({ label, value, sub }) {
  return (
    <div className="card tile">
      <div className="label">{label}</div>
      <div className="value">{value ?? '—'}</div>
      {sub ? <div className="sub">{sub}</div> : null}
    </div>
  )
}

export function SlateTiles({ slate }) {
  const allGames = slate?.games || []
  if (!allGames.length) return null

  // Same auto-detect-from-the-loaded-DK-slate pattern already used by
  // the Stacks/Hitters/Pitchers tabs' own Games checklists: once a DK
  // slate is loaded, these headline numbers should reflect just that
  // slate (e.g. picking the "Early" 4-game slate shouldn't still show
  // stats averaged across the whole day's 15 games) -- falling back to
  // every game when nothing's detected as in-slate yet, same "showing
  // everything beats silently showing nothing" reasoning those tabs use.
  const inSlateGames = allGames.filter((g) => g.in_slate !== false)
  const games = inSlateGames.length ? inSlateGames : allGames

  const totals = games
    .map((g) => g.betting?.total)
    .filter((t) => typeof t === 'number')

  const avgTotal = totals.length
    ? (totals.reduce((a, b) => a + b, 0) / totals.length).toFixed(1)
    : null

  // Highest implied team total on the slate - the single best pointer
  // toward where the runs are expected to come from.
  let bestTeam = null
  for (const g of games) {
    for (const side of ['home', 'away']) {
      const t = g[side]
      if (t.implied_runs == null) continue
      if (!bestTeam || t.implied_runs > bestTeam.runs) {
        bestTeam = { name: t.abbrev || t.name, runs: t.implied_runs, venue: g.venue.name }
      }
    }
  }

  let bestStack = null
  for (const g of games) {
    for (const side of ['home', 'away']) {
      const t = g[side]
      if (t.stack_score == null) continue
      if (!bestStack || t.stack_score > bestStack.score) {
        bestStack = { name: t.abbrev || t.name, score: t.stack_score }
      }
    }
  }

  const confirmed = games.reduce(
    (n, g) => n + (g.home.lineup_confirmed ? 1 : 0) + (g.away.lineup_confirmed ? 1 : 0),
    0,
  )

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
