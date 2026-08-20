import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { downloadCsv, lineupsToCsv } from '../csv'
import { localTime } from '../format'
import { ContestFieldPanel } from './ContestFieldPanel'
import { LineupsTable } from './LineupsTable'
import { PlayerPoolTable } from './PlayerPoolTable'

// Named stack shapes, largest group first. Shapes that sum to 8 use
// every hitter slot; "4-2" and "3-3" leave 2 hitters as free picks.
const STACK_SHAPES = {
  'no stack': [],
  '5-3': [5, 3],
  '5-2-1': [5, 2, 1],
  '4-4': [4, 4],
  '4-3-1': [4, 3, 1],
  '4-2-2': [4, 2, 2],
  '4-2': [4, 2],
  '3-3-2': [3, 3, 2],
  '3-3': [3, 3],
}

function teamsOnSlate(slate) {
  const teams = new Set()
  for (const g of slate?.games || []) {
    if (g.home?.abbrev) teams.add(g.home.abbrev)
    if (g.away?.abbrev) teams.add(g.away.abbrev)
  }
  return [...teams].sort()
}

const ROSTER_SLOTS = ['P', 'C', '1B', '2B', '3B', 'SS', 'OF']
const MAX_HITTERS = 8

// Same fixed display/roster order LineupsTable.jsx flattens `slots`
// into -- late swap needs the identical order to build its `picks`.
const SLOT_ORDER = ['P', 'P', 'C', '1B', '2B', '3B', 'SS', 'OF', 'OF', 'OF']

function lineupToPicks(lineup) {
  const used = {}
  return SLOT_ORDER.map((slotType) => {
    const idx = used[slotType] || 0
    used[slotType] = idx + 1
    const player = (lineup.slots[slotType] || [])[idx]
    return { player_id: player?.id, game_pk: player?.game_pk }
  })
}

/**
 * Generates one or many optimal DraftKings Classic MLB lineups from
 * whatever salary + projections CSVs are loaded for the date.
 */
export function LineupsPanel({ date, slate, projectionSource = 'rotowire' }) {
  const [state, setState] = useState({ status: 'idle' })
  const [numLineups, setNumLineups] = useState(1)
  const [stackShape, setStackShape] = useState('no stack')
  const [stackTeams, setStackTeams] = useState([])
  const [maxExposure, setMaxExposure] = useState('')
  const [selected, setSelected] = useState(0)
  const [locked, setLocked] = useState(new Set())
  const [excluded, setExcluded] = useState(new Set())
  const [showPool, setShowPool] = useState(false)
  const [showExposure, setShowExposure] = useState(false)
  const [showRules, setShowRules] = useState(false)
  const [slotExposure, setSlotExposure] = useState({})
  const [teamExposure, setTeamExposure] = useState([])
  const [newTeamCap, setNewTeamCap] = useState({ team: '', pct: '' })
  // Defaults to $47,000 -- a lineup with a lot of unspent salary is
  // almost always leaving real projected points on the table. Clear it
  // to disable the floor entirely.
  const [minSalary, setMinSalary] = useState('47000')
  const [maxSalary, setMaxSalary] = useState('')
  const [minUniquePlayers, setMinUniquePlayers] = useState('')
  const [minTeams, setMinTeams] = useState('')
  const [maxTeams, setMaxTeams] = useState('')
  const [oneOffMode, setOneOffMode] = useState('off') // 'off' | 'group' | 'range'
  const [oneOff, setOneOff] = useState(new Set())
  const [oneOffMinSalary, setOneOffMinSalary] = useState('')
  const [oneOffMaxSalary, setOneOffMaxSalary] = useState('')
  const [minOwnership, setMinOwnership] = useState('')
  const [maxOwnership, setMaxOwnership] = useState('')
  const [showSlateGames, setShowSlateGames] = useState(false)
  const [includedGames, setIncludedGames] = useState(new Set())
  const [lateSwapState, setLateSwapState] = useState({ status: 'idle' })

  const teams = useMemo(() => teamsOnSlate(slate), [slate])
  const groups = STACK_SHAPES[stackShape]
  const isPartialStack = groups.length > 0 && groups.reduce((a, b) => a + b, 0) < MAX_HITTERS

  const slateGames = useMemo(
    () =>
      (slate?.games || [])
        .filter((g) => g.game_pk != null)
        .map((g) => ({
          pk: g.game_pk,
          away: g.away?.abbrev,
          home: g.home?.abbrev,
          time: g.game_time_utc,
          inSlate: g.in_slate,
        })),
    [slate],
  )
  const slateDetected = slateGames.some((g) => g.inSlate != null)

  // Whichever games are actually selected right now, in the exact shape
  // the backend expects -- shared between the optimizer call and the
  // contest field generator so the field is always drawn from the same
  // slate the lineups were built against, never a mismatched selection.
  const includedGamePksParam =
    slateGames.length && includedGames.size < slateGames.length ? [...includedGames] : null

  // Re-derive the default selection whenever the slate's own set of
  // games changes (a new date, a refresh) -- default to whatever the
  // uploaded DK CSV's Game Info detected (or everyone, if nothing's
  // been uploaded yet), not whatever the user last toggled for a
  // different day's slate.
  const slateGamePks = slateGames.map((g) => g.pk).join(',')
  useEffect(() => {
    setIncludedGames(new Set(slateGames.filter((g) => g.inSlate !== false).map((g) => g.pk)))
  }, [slateGamePks])

  function toggleGame(pk) {
    setIncludedGames((prev) => {
      const next = new Set(prev)
      next.has(pk) ? next.delete(pk) : next.add(pk)
      return next
    })
  }

  function setSlotCap(slot, value) {
    setSlotExposure((prev) => {
      const next = { ...prev }
      if (value.trim()) next[slot] = value
      else delete next[slot]
      return next
    })
  }

  function addTeamCap() {
    if (!newTeamCap.team || !newTeamCap.pct.trim()) return
    setTeamExposure((prev) => [
      ...prev.filter((e) => e.team !== newTeamCap.team),
      { team: newTeamCap.team, pct: newTeamCap.pct },
    ])
    setNewTeamCap({ team: '', pct: '' })
  }

  function removeTeamCap(team) {
    setTeamExposure((prev) => prev.filter((e) => e.team !== team))
  }

  function toggleLock(id) {
    setLocked((prev) => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
    setExcluded((prev) => {
      if (!prev.has(id)) return prev
      const next = new Set(prev)
      next.delete(id)
      return next
    })
  }

  function toggleExclude(id) {
    setExcluded((prev) => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
    setLocked((prev) => {
      if (!prev.has(id)) return prev
      const next = new Set(prev)
      next.delete(id)
      return next
    })
  }

  function toggleOneOff(id) {
    setOneOff((prev) => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  }

  function changeShape(shape) {
    setStackShape(shape)
    setStackTeams(new Array(STACK_SHAPES[shape].length).fill(''))
    setOneOffMode('off')
  }

  function changeStackTeam(i, team) {
    setStackTeams((prev) => {
      const next = [...prev]
      next[i] = team
      return next
    })
  }

  async function run() {
    setState({ status: 'loading' })
    try {
      const result = await api.generateLineups(date, {
        numLineups,
        projectionSource,
        stackGroups: groups.length ? groups : null,
        stackTeams: groups.length ? stackTeams.map((t) => t || null) : null,
        maxExposurePct: maxExposure.trim() ? Number(maxExposure) : null,
        exposureBySlot: Object.keys(slotExposure).length
          ? Object.fromEntries(Object.entries(slotExposure).map(([k, v]) => [k, Number(v)]))
          : null,
        teamExposureCap: teamExposure.length
          ? Object.fromEntries(teamExposure.map((e) => [e.team, Number(e.pct)]))
          : null,
        lockedIds: locked.size ? [...locked] : null,
        excludedIds: excluded.size ? [...excluded] : null,
        minSalary: minSalary.trim() ? Number(minSalary) : null,
        maxSalary: maxSalary.trim() ? Number(maxSalary) : null,
        minUniquePlayers: minUniquePlayers.trim() ? Number(minUniquePlayers) : 1,
        minTeamsPerLineup: minTeams.trim() ? Number(minTeams) : null,
        maxTeamsPerLineup: maxTeams.trim() ? Number(maxTeams) : null,
        oneOffGroupIds: isPartialStack && oneOffMode === 'group' && oneOff.size ? [...oneOff] : null,
        oneOffMinSalary:
          isPartialStack && oneOffMode === 'range' && oneOffMinSalary.trim()
            ? Number(oneOffMinSalary)
            : null,
        oneOffMaxSalary:
          isPartialStack && oneOffMode === 'range' && oneOffMaxSalary.trim()
            ? Number(oneOffMaxSalary)
            : null,
        minOwnershipPct: minOwnership.trim() ? Number(minOwnership) : null,
        maxOwnershipPct: maxOwnership.trim() ? Number(maxOwnership) : null,
        includedGamePks: includedGamePksParam,
      })
      setSelected(0)
      setLateSwapState({ status: 'idle' })
      setState({ status: 'ready', ...result })
    } catch (err) {
      setState({ status: 'error', message: err.message })
    }
  }

  async function runLateSwap() {
    const lineup = state.lineups[selected]
    if (!lineup) return
    setLateSwapState({ status: 'loading' })
    try {
      const picks = lineupToPicks(lineup)
      // Captured before the swap so removed players' names are still
      // displayable afterward -- they won't be in the new lineup anymore.
      const namesById = {}
      for (const players of Object.values(lineup.slots)) {
        for (const p of players) namesById[p.id] = p.name
      }
      const result = await api.lateSwap(date, picks, { projectionSource })
      if (result.changed) {
        for (const players of Object.values(result.lineup.slots)) {
          for (const p of players) namesById[p.id] = p.name
        }
        setState((prev) => {
          const nextLineups = [...prev.lineups]
          nextLineups[selected] = result.lineup
          return { ...prev, lineups: nextLineups }
        })
      }
      setLateSwapState({ status: 'ready', ...result, namesById })
    } catch (err) {
      setLateSwapState({ status: 'error', message: err.message })
    }
  }

  return (
    <div className="card">
      <div className="controls" style={{ marginBottom: 14, flexWrap: 'wrap' }}>
        <label className="dim" style={{ fontSize: 13 }}>
          Lineups{' '}
          <input
            type="number"
            min="1"
            max="150"
            value={numLineups}
            onChange={(e) => setNumLineups(Math.max(1, Number(e.target.value) || 1))}
            style={{ width: 60 }}
          />
        </label>
        <label className="dim" style={{ fontSize: 13 }}>
          Stack{' '}
          <select value={stackShape} onChange={(e) => changeShape(e.target.value)}>
            {Object.keys(STACK_SHAPES).map((shape) => (
              <option key={shape} value={shape}>
                {shape}
              </option>
            ))}
          </select>
        </label>
        <label className="dim" style={{ fontSize: 13 }}>
          Max exposure{' '}
          <select value={maxExposure} onChange={(e) => setMaxExposure(e.target.value)}>
            <option value="">none</option>
            {[20, 30, 40, 50, 75].map((n) => (
              <option key={n} value={n}>
                {n}%
              </option>
            ))}
          </select>
        </label>
        <button className="primary" onClick={run} disabled={state.status === 'loading'}>
          {state.status === 'loading'
            ? 'Solving…'
            : `Generate ${numLineups > 1 ? `${numLineups} lineups` : 'lineup'}`}
        </button>
        <button onClick={() => setShowPool((v) => !v)}>
          {showPool ? 'Hide player pool' : 'Player pool'}
          {locked.size + excluded.size > 0 ? ` (${locked.size + excluded.size})` : ''}
        </button>
        <button onClick={() => setShowExposure((v) => !v)}>
          {showExposure ? 'Hide exposure limits' : 'Exposure limits'}
          {Object.keys(slotExposure).length + teamExposure.length > 0
            ? ` (${Object.keys(slotExposure).length + teamExposure.length})`
            : ''}
        </button>
        <button onClick={() => setShowRules((v) => !v)}>
          {showRules ? 'Hide lineup rules' : 'Lineup rules'}
          {[minSalary, maxSalary, minUniquePlayers, minTeams, maxTeams, minOwnership, maxOwnership].filter(
            (v) => v.trim(),
          ).length > 0
            ? ` (${
                [
                  minSalary,
                  maxSalary,
                  minUniquePlayers,
                  minTeams,
                  maxTeams,
                  minOwnership,
                  maxOwnership,
                ].filter((v) => v.trim()).length
              })`
            : ''}
        </button>
        {slateGames.length > 0 && (
          <button onClick={() => setShowSlateGames((v) => !v)}>
            {showSlateGames ? 'Hide slate games' : 'Slate games'} ({includedGames.size} of{' '}
            {slateGames.length})
          </button>
        )}
      </div>

      {showSlateGames && (
        <div className="card" style={{ marginBottom: 14 }}>
          <div className="sub-line" style={{ marginBottom: 8 }}>
            {slateDetected
              ? 'Auto-detected from your uploaded DK salary CSV -- untick a game to leave it out, or tick one back in.'
              : 'No DK salary CSV uploaded yet, so every game is included by default -- upload one to auto-detect your slate.'}
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            {slateGames.map((g) => (
              <label
                key={g.pk}
                className="dim"
                style={{ fontSize: 13, display: 'flex', alignItems: 'center', gap: 8 }}
              >
                <input
                  type="checkbox"
                  checked={includedGames.has(g.pk)}
                  onChange={() => toggleGame(g.pk)}
                />
                {g.away} @ {g.home}
                <span className="dim" style={{ fontSize: 12 }}>
                  {localTime(g.time)}
                </span>
                {g.inSlate === true && <span className="badge ok">in DK slate</span>}
                {g.inSlate === false && <span className="badge">not in DK slate</span>}
              </label>
            ))}
          </div>
        </div>
      )}

      {showRules && (
        <div className="card" style={{ marginBottom: 14 }}>
          <div className="controls" style={{ flexWrap: 'wrap' }}>
            <label className="dim" style={{ fontSize: 13 }}>
              Min salary{' '}
              <input
                type="number"
                min="0"
                max={50000}
                step="500"
                placeholder="—"
                value={minSalary}
                onChange={(e) => setMinSalary(e.target.value)}
                style={{ width: 80 }}
              />
            </label>
            <label className="dim" style={{ fontSize: 13 }}>
              Max salary{' '}
              <input
                type="number"
                min="0"
                max={50000}
                step="500"
                placeholder="—"
                value={maxSalary}
                onChange={(e) => setMaxSalary(e.target.value)}
                style={{ width: 80 }}
              />
            </label>
            <label
              className="dim"
              style={{ fontSize: 13 }}
              title="0 allows exact duplicates -- a real GPP move (entering a signature build multiple times). Each lineup then reports how many identical copies are in the set."
            >
              Min unique players between lineups (0 = allow duplicates){' '}
              <input
                type="number"
                min="0"
                max="10"
                placeholder="1"
                value={minUniquePlayers}
                onChange={(e) => setMinUniquePlayers(e.target.value)}
                style={{ width: 55 }}
              />
            </label>
            <label className="dim" style={{ fontSize: 13 }}>
              Min teams per lineup{' '}
              <input
                type="number"
                min="1"
                max="10"
                placeholder="—"
                value={minTeams}
                onChange={(e) => setMinTeams(e.target.value)}
                style={{ width: 55 }}
              />
            </label>
            <label className="dim" style={{ fontSize: 13 }}>
              Max teams per lineup{' '}
              <input
                type="number"
                min="1"
                max="10"
                placeholder="—"
                value={maxTeams}
                onChange={(e) => setMaxTeams(e.target.value)}
                style={{ width: 55 }}
              />
            </label>
            <label className="dim" style={{ fontSize: 13 }}>
              Min ownership{' '}
              <input
                type="number"
                min="0"
                max="1000"
                placeholder="—"
                value={minOwnership}
                onChange={(e) => setMinOwnership(e.target.value)}
                style={{ width: 65 }}
              />
              %
            </label>
            <label className="dim" style={{ fontSize: 13 }}>
              Max ownership{' '}
              <input
                type="number"
                min="0"
                max="1000"
                placeholder="—"
                value={maxOwnership}
                onChange={(e) => setMaxOwnership(e.target.value)}
                style={{ width: 65 }}
              />
              %
            </label>
          </div>
        </div>
      )}

      {showPool && (
        <div style={{ marginBottom: 14 }}>
          <PlayerPoolTable
            slate={slate}
            locked={locked}
            excluded={excluded}
            onToggleLock={toggleLock}
            onToggleExclude={toggleExclude}
            oneOff={oneOff}
            onToggleOneOff={toggleOneOff}
            showOneOff={isPartialStack && oneOffMode === 'group'}
          />
        </div>
      )}

      {showExposure && (
        <div className="card" style={{ marginBottom: 14 }}>
          <div className="sub-line" style={{ marginBottom: 8 }}>
            Position exposure — overrides the general max exposure for specific slots
          </div>
          <div className="controls" style={{ marginBottom: 14, flexWrap: 'wrap' }}>
            {ROSTER_SLOTS.map((slot) => (
              <label key={slot} className="dim" style={{ fontSize: 13 }}>
                {slot}{' '}
                <input
                  type="number"
                  min="1"
                  max="100"
                  placeholder="—"
                  value={slotExposure[slot] || ''}
                  onChange={(e) => setSlotCap(slot, e.target.value)}
                  style={{ width: 55 }}
                />
                %
              </label>
            ))}
          </div>

          {groups.length > 0 && (
            <>
              <div className="sub-line" style={{ marginBottom: 8 }}>
                Team exposure — caps how often a team is used AS THE STACK (auto-assigned groups only)
              </div>
              <div className="controls" style={{ marginBottom: 8, flexWrap: 'wrap' }}>
                <select
                  value={newTeamCap.team}
                  onChange={(e) => setNewTeamCap((prev) => ({ ...prev, team: e.target.value }))}
                >
                  <option value="">Team…</option>
                  {teams.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>
                <input
                  type="number"
                  min="1"
                  max="100"
                  placeholder="%"
                  value={newTeamCap.pct}
                  onChange={(e) => setNewTeamCap((prev) => ({ ...prev, pct: e.target.value }))}
                  style={{ width: 60 }}
                />
                <button onClick={addTeamCap}>Add</button>
              </div>
              {teamExposure.length > 0 && (
                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                  {teamExposure.map((e) => (
                    <span key={e.team} className="badge">
                      {e.team} ≤ {e.pct}%{' '}
                      <button
                        className="pill-toggle"
                        style={{ padding: '0 4px', border: 'none' }}
                        onClick={() => removeTeamCap(e.team)}
                      >
                        ✕
                      </button>
                    </span>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      )}

      {groups.length > 0 && (
        <div className="controls" style={{ marginBottom: 14, flexWrap: 'wrap' }}>
          {groups.map((size, i) => (
            <label key={i} className="dim" style={{ fontSize: 13 }}>
              {size}-stack team{' '}
              <select value={stackTeams[i] || ''} onChange={(e) => changeStackTeam(i, e.target.value)}>
                <option value="">Auto</option>
                {teams.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            </label>
          ))}
        </div>
      )}

      {isPartialStack && (
        <div className="card" style={{ marginBottom: 14 }}>
          <div className="sub-line" style={{ marginBottom: 8 }}>
            One-off slots — this shape leaves {MAX_HITTERS - groups.reduce((a, b) => a + b, 0)} hitter
            slot{MAX_HITTERS - groups.reduce((a, b) => a + b, 0) > 1 ? 's' : ''} outside the stack;
            restrict who can fill them
          </div>
          <div className="controls" style={{ marginBottom: oneOffMode !== 'off' ? 10 : 0, flexWrap: 'wrap' }}>
            {['off', 'group', 'range'].map((mode) => (
              <button
                key={mode}
                className={oneOffMode === mode ? 'primary' : ''}
                onClick={() => setOneOffMode(mode)}
              >
                {mode === 'off' ? 'Unrestricted' : mode === 'group' ? 'By group' : 'By salary range'}
              </button>
            ))}
          </div>
          {oneOffMode === 'group' && (
            <div className="sub-line">
              {oneOff.size > 0
                ? `${oneOff.size} player${oneOff.size > 1 ? 's' : ''} tagged one-off eligible`
                : 'No players tagged yet'}
              — open the player pool above and use the "one-off" pill to tag who's eligible.
            </div>
          )}
          {oneOffMode === 'range' && (
            <div className="controls" style={{ flexWrap: 'wrap' }}>
              <label className="dim" style={{ fontSize: 13 }}>
                Min salary{' '}
                <input
                  type="number"
                  min="0"
                  max={50000}
                  step="500"
                  placeholder="—"
                  value={oneOffMinSalary}
                  onChange={(e) => setOneOffMinSalary(e.target.value)}
                  style={{ width: 80 }}
                />
              </label>
              <label className="dim" style={{ fontSize: 13 }}>
                Max salary{' '}
                <input
                  type="number"
                  min="0"
                  max={50000}
                  step="500"
                  placeholder="—"
                  value={oneOffMaxSalary}
                  onChange={(e) => setOneOffMaxSalary(e.target.value)}
                  style={{ width: 80 }}
                />
              </label>
            </div>
          )}
        </div>
      )}

      {state.status === 'idle' && (
        <p style={{ marginTop: 0, color: 'var(--text-secondary)' }}>
          Builds distinct lineups that each fit DraftKings' $50,000 salary
          cap and Classic MLB roster, using whatever salary and
          projections CSVs are loaded for this date. Upload both first —
          the optimizer needs a real salary and a real projection for
          every player it considers. A stack shape like "4-2-2" forces at
          least that many hitters onto each of that many distinct teams
          — leave a group on Auto to let the solver pick the best team
          for it, or choose one yourself. Shapes that use all 8 hitters
          (5-3, 4-4, etc.) come out exact since there's no room left
          over; partial shapes (4-2, 3-3) leave 2 free, which can land
          anywhere — including padding one of the stacks further. Asking
          for more
          than one lineup forces each to differ from the ones before it;
          a max exposure cap keeps any one player from showing up in too
          many of them. Open the player pool to lock someone into every
          lineup or exclude them entirely.
        </p>
      )}

      {state.status === 'loading' && (
        <div>
          <div className="skeleton" style={{ width: '70%', marginBottom: 10 }} />
          <div className="skeleton" style={{ width: '85%', marginBottom: 10 }} />
          <div className="skeleton" style={{ width: '60%' }} />
        </div>
      )}

      {state.status === 'error' && (
        <>
          <div className="notice error">{state.message}</div>
          <button style={{ marginTop: 12 }} onClick={run}>
            Try again
          </button>
        </>
      )}

      {state.status === 'ready' && (
        <>
          {state.lineups.length < numLineups && (
            <div className="notice" style={{ marginBottom: 12 }}>
              Only built {state.lineups.length} of {numLineups} requested — the pool ran out of
              room for more distinct lineups under the current constraints.
            </div>
          )}

          {state.lineups.length > 1 && (
            <div className="controls" style={{ marginBottom: 12 }}>
              <button
                onClick={() => {
                  setSelected((i) => Math.max(0, i - 1))
                  setLateSwapState({ status: 'idle' })
                }}
                disabled={selected === 0}
              >
                ← Prev
              </button>
              <span className="dim" style={{ fontSize: 13 }}>
                Lineup {selected + 1} of {state.lineups.length}
              </span>
              <button
                onClick={() => {
                  setSelected((i) => Math.min(state.lineups.length - 1, i + 1))
                  setLateSwapState({ status: 'idle' })
                }}
                disabled={selected === state.lineups.length - 1}
              >
                Next →
              </button>
            </div>
          )}

          <LineupsTable lineup={state.lineups[selected]} />

          <div className="controls" style={{ marginTop: 12 }}>
            <button
              onClick={runLateSwap}
              disabled={lateSwapState.status === 'loading'}
              title="Re-optimize just this lineup's still-open slots (games that haven't started yet) -- locked players stay exactly as they are, same as real DK late swap"
            >
              {lateSwapState.status === 'loading' ? 'Checking for swaps…' : 'Late swap'}
            </button>
          </div>
          {lateSwapState.status === 'error' && (
            <div className="notice error" style={{ marginTop: 8 }}>{lateSwapState.message}</div>
          )}
          {lateSwapState.status === 'ready' && (
            <div className="notice" style={{ marginTop: 8 }}>
              {lateSwapState.changed ? (
                <>
                  Swapped {lateSwapState.removed_player_ids.length} player
                  {lateSwapState.removed_player_ids.length === 1 ? '' : 's'}:{' '}
                  {lateSwapState.removed_player_ids
                    .map((id) => lateSwapState.namesById[id] || id)
                    .join(', ')}{' '}
                  → {lateSwapState.added_player_ids
                    .map((id) => lateSwapState.namesById[id] || id)
                    .join(', ')}
                </>
              ) : (
                lateSwapState.message || 'No better swap found -- this lineup is already optimal for its open slots.'
              )}
            </div>
          )}

          {state.exposure.length > 0 && (
            <div className="card table-wrap" style={{ marginTop: 16 }}>
              <div className="sub-line" style={{ marginBottom: 8 }}>
                Exposure across {state.lineups.length} lineup{state.lineups.length > 1 ? 's' : ''}
              </div>
              <table>
                <thead>
                  <tr>
                    <th>Player</th>
                    <th>Team</th>
                    <th className="num">Lineups</th>
                    <th className="num">Exposure</th>
                  </tr>
                </thead>
                <tbody>
                  {state.exposure.map((e) => (
                    <tr key={e.id}>
                      <td>{e.name}</td>
                      <td className="dim">{e.team}</td>
                      <td className="num">{e.count}</td>
                      <td className="num">{e.pct}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {state.team_exposure?.length > 0 && (
            <div className="card table-wrap" style={{ marginTop: 16 }}>
              <div className="sub-line" style={{ marginBottom: 8 }}>
                Team stack usage across {state.lineups.length} lineup
                {state.lineups.length > 1 ? 's' : ''}
              </div>
              <table>
                <thead>
                  <tr>
                    <th>Team</th>
                    <th className="num">Lineups stacked</th>
                    <th className="num">Exposure</th>
                  </tr>
                </thead>
                <tbody>
                  {state.team_exposure.map((e) => (
                    <tr key={e.team}>
                      <td className="name">{e.team}</td>
                      <td className="num">{e.count}</td>
                      <td className="num">{e.pct}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <button style={{ marginTop: 12 }} onClick={run}>
            Regenerate
          </button>
          <button
            style={{ marginTop: 12, marginLeft: 8 }}
            onClick={() => downloadCsv(`lineups-${date}.csv`, lineupsToCsv(state.lineups))}
            title="Download all generated lineups as a CSV -- for handing off to an external simulator"
          >
            Download CSV
          </button>

          <ContestFieldPanel
            date={date}
            lineups={state.lineups}
            includedGamePks={includedGamePksParam}
          />
        </>
      )}
    </div>
  )
}
