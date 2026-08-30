import { useMemo, useState } from 'react'

/**
 * A compact "what does the model think the field looks like" sanity
 * card: the top plays by projected ownership, summed hitter ownership
 * per team (the stack view -- the quantity MLB field behaviour is
 * actually organised around), and each position group's total, which
 * has a KNOWN correct value (slots x 100%: C=100, OF=300, P=200...).
 *
 * The whole point is making ownership-model failures visible at a
 * glance instead of buried in a table column: a group summing to 260%
 * instead of 300%, a bench bat in the top ten, or a top stack that
 * looks nothing like the slate's Vegas board all jump out here.
 * Prefers the in-house ownership number, falls back to RotoWire's.
 */
export function FieldSnapshot({ slate }) {
  const [open, setOpen] = useState(false)

  const snapshot = useMemo(() => {
    const players = []
    for (const g of slate?.games || []) {
      for (const side of ['home', 'away']) {
        const team = g[side]
        const opp = g[side === 'home' ? 'away' : 'home']
        const roster = [...(team.hitters || [])]
        if (team.probable_pitcher) roster.push({ ...team.probable_pitcher, __pitcher: true })
        for (const p of roster) {
          const proj = p.projection || {}
          const own = proj.inhouse_ownership_pct ?? proj.ownership_pct
          if (own == null) continue
          const dkPos = p.__pitcher
            ? 'P'
            : (p.salary?.position || p.position || '').split('/')[0].trim()
          players.push({
            name: p.name,
            team: team.abbrev,
            opponent: opp.abbrev,
            position: dkPos,
            own,
            isPitcher: !!p.__pitcher,
            source: proj.inhouse_ownership_pct != null ? 'in-house' : 'rotowire',
          })
        }
      }
    }
    if (!players.length) return null

    const topPlays = [...players].sort((a, b) => b.own - a.own).slice(0, 10)

    const teamTotals = new Map()
    for (const p of players) {
      if (p.isPitcher) continue
      teamTotals.set(p.team, (teamTotals.get(p.team) || 0) + p.own)
    }
    const topStacks = [...teamTotals.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8)

    const groupTotals = new Map()
    for (const p of players) {
      if (!p.position) continue
      groupTotals.set(p.position, (groupTotals.get(p.position) || 0) + p.own)
    }

    const source = players.some((p) => p.source === 'in-house') ? 'in-house' : 'RotoWire'
    return { topPlays, topStacks, groupTotals: [...groupTotals.entries()], source }
  }, [slate])

  if (!snapshot) return null

  // The correct total for each DK Classic MLB roster-slot group. A
  // multi-eligible player is summed across his groups, so a group
  // holding such players can legitimately read slightly under its slot
  // total -- far under (or a group missing entirely) is the failure
  // this exists to surface.
  const EXPECTED = { P: 200, C: 100, '1B': 100, '2B': 100, '3B': 100, SS: 100, OF: 300 }

  return (
    <div className="card" style={{ marginBottom: 14 }}>
      <div className="controls" style={{ flexWrap: 'wrap' }}>
        <strong style={{ fontSize: 13 }}>Field snapshot</strong>
        <span className="dim" style={{ fontSize: 12 }}>
          what the {snapshot.source} ownership model thinks the field does
        </span>
        <button onClick={() => setOpen((o) => !o)}>{open ? 'Hide' : 'Show'}</button>
      </div>

      {open && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 24, marginTop: 10 }}>
          <div>
            <div className="sub-line" style={{ marginBottom: 6 }}>Top projected ownership</div>
            {snapshot.topPlays.map((p) => (
              <div key={`${p.name}-${p.team}`} className="dim" style={{ fontSize: 13 }}>
                <strong>{p.own.toFixed(1)}%</strong> {p.name}{' '}
                <span className="dim">
                  ({p.position} {p.team} vs {p.opponent})
                </span>
              </div>
            ))}
          </div>

          <div>
            <div
              className="sub-line"
              style={{ marginBottom: 6 }}
              title="Summed hitter ownership per team -- the stack view. This should broadly track the slate's Vegas board; a top stack nobody's implied total supports means the model has misread the slate."
            >
              Stack ownership (hitters, summed)
            </div>
            {snapshot.topStacks.map(([team, total]) => (
              <div key={team} className="dim" style={{ fontSize: 13 }}>
                <strong>{total.toFixed(0)}%</strong> {team}
              </div>
            ))}
          </div>

          <div>
            <div
              className="sub-line"
              style={{ marginBottom: 6 }}
              title="Each roster-slot group's total has a KNOWN correct value: slots x 100%. A group far under its target (or missing) means the pool construction is broken -- exactly the failure class this card exists to catch."
            >
              Group totals (sanity)
            </div>
            {snapshot.groupTotals.map(([pos, total]) => {
              const expected = EXPECTED[pos]
              const off = expected != null && Math.abs(total - expected) > expected * 0.1
              return (
                <div key={pos} className="dim" style={{ fontSize: 13 }}>
                  <span className={off ? 'badge risk' : undefined}>
                    {pos}: {total.toFixed(0)}%
                  </span>{' '}
                  {expected != null && <span className="dim">/ {expected}%</span>}
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
