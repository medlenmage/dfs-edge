import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { ContestSimulatorPanel } from './ContestSimulatorPanel'
import { LineupsPanel } from './LineupsPanel'
import { MyLineupsCard } from './MyLineupsCard'

/**
 * The Build workspace, from the v2 redesign: one page where you set
 * your rules once, generate a contest, and simulate it -- instead of
 * three separate tabs each carrying its own copy of the settings.
 *
 * The three former Build views map onto this as:
 *
 *   Contest mode (default) -- a staged flow across the top (Settings ->
 *     Lineups -> Simulate) with a persistent settings rail on the left.
 *     Stage 1 and 2 are the contest generator, stage 3 the simulator;
 *     they already shared a batch, they just didn't share a screen.
 *   Single-lineup mode -- the MILP optimizer, which answers a genuinely
 *     different question (one provably optimal lineup, not a contest),
 *     so it keeps its own panel rather than being forced into the
 *     staged flow.
 *
 * `stackIntent` is the hand-off from the Stacks table's "Add N-stack to
 * build" action: it arrives as {team, size} and pre-selects the primary
 * stack here, which is the whole point of making those rows expandable.
 */
// DK Classic MLB's roster order. Contest entries are built slot by slot
// in exactly this sequence (contest.py's _sample_one_lineup walks
// `slot_order`), but the player dicts don't carry the slot itself --
// so the card labels it by position in the list, which is the same
// convention lineup_export.SLOT_LABELS uses server-side.
const MLB_SLOT_LABELS = ['P', 'P', 'C', '1B', '2B', '3B', 'SS', 'OF', 'OF', 'OF']

export function BuildWorkspace({ date, slate, projectionSource, stackIntent, onClearIntent }) {
  const [mode, setMode] = useState('contest') // 'contest' | 'single'
  const [contestTypes, setContestTypes] = useState(null)
  const [contestType, setContestType] = useState('gpp_large')
  const [contestSize, setContestSize] = useState(10000)
  const [reroll, setReroll] = useState(0)
  const [build, setBuild] = useState({ status: 'idle' })
  const [collapsed, setCollapsed] = useState(false)
  // Lineups the optimizer built this session, and whether the contest
  // should be built AROUND the day's set-aside tray rather than
  // generating the whole field.
  const [optimizerLineups, setOptimizerLineups] = useState([])
  const [useMyLineups, setUseMyLineups] = useState(false)
  const [trayCount, setTrayCount] = useState(0)

  // Slate-game filter, same auto-detect pattern every other panel uses.
  const [showSlateGames, setShowSlateGames] = useState(false)
  const [includedGames, setIncludedGames] = useState(new Set())

  useEffect(() => {
    api
      .contestTypes()
      .then((d) => setContestTypes(d.contest_types))
      .catch(() => {})
  }, [])

  const preset = contestTypes?.[contestType]
  const sizes = preset?.sizes || []

  useEffect(() => {
    if (!sizes.length) return
    if (!sizes.includes(contestSize)) setContestSize(preset.field_size ?? sizes[sizes.length - 1])
  }, [contestType, contestTypes])

  const slateGames = useMemo(
    () =>
      (slate?.games || [])
        .filter((g) => g.game_pk != null)
        .map((g) => ({ pk: g.game_pk, away: g.away?.abbrev, home: g.home?.abbrev, inSlate: g.in_slate })),
    [slate],
  )
  const slateGamePks = slateGames.map((g) => g.pk).join(',')
  useEffect(() => {
    setIncludedGames(new Set(slateGames.filter((g) => g.inSlate !== false).map((g) => g.pk)))
  }, [slateGamePks])

  // A stack sent over from the Stacks table lands here. The generator
  // builds every archetype itself rather than taking a forced primary,
  // so this is surfaced as an explicit, dismissible intent rather than
  // silently pretending it constrained the build.
  useEffect(() => {
    if (stackIntent) setMode('contest')
  }, [stackIntent])

  async function runBuild(rerollOverride = null) {
    setBuild({ status: 'loading' })
    try {
      const result = await api.buildContestEntries(date, contestType, contestSize, {
        projectionSource,
        reroll: rerollOverride ?? reroll,
        includedGamePks:
          slateGames.length && includedGames.size < slateGames.length ? [...includedGames] : null,
        useMyLineups,
      })
      setBuild({ status: 'ready', ...result })
    } catch (err) {
      setBuild({ status: 'error', message: err.message })
    }
  }

  const sizeLabel = (n) => (n >= 1000 && n % 1000 === 0 ? `${n / 1000}K` : n.toLocaleString())
  const built = build.status === 'ready'

  // Which lineups to show as cards, and (if the intent named a team)
  // the ones actually featuring it first.
  const cards = useMemo(() => {
    const entries = built ? build.sample_entries || [] : []
    if (!stackIntent) return entries.slice(0, 24)
    const featuring = entries.filter((e) => (e.stack || '').includes(stackIntent.team))
    return [...featuring, ...entries.filter((e) => !featuring.includes(e))].slice(0, 24)
  }, [built, build, stackIntent])

  return (
    <>
      <div className="subtabs" style={{ marginBottom: 14 }}>
        <button className={mode === 'contest' ? 'on' : ''} onClick={() => setMode('contest')}>
          Contest
        </button>
        <button className={mode === 'single' ? 'on' : ''} onClick={() => setMode('single')}>
          Single lineup
        </button>
      </div>

      {mode === 'single' && (
        <LineupsPanel
          date={date}
          slate={slate}
          projectionSource={projectionSource}
          onLineups={setOptimizerLineups}
        />
      )}

      {mode === 'contest' && (
        <>
          {/* ------------------------------------------- stages */}
          <div className="stages" aria-label="Build stages">
            <div className={`stage ${built ? 'done' : 'on'}`}>
              <div className="k">{built ? '✓' : '1'}</div>
              <div>
                <div className="t">Settings</div>
                <div className="s">
                  {preset ? `${preset.label} · ${sizeLabel(contestSize)}` : 'Pick a contest'}
                </div>
              </div>
            </div>
            <div className={`stage ${built ? 'done' : ''}`}>
              <div className="k">{built ? '✓' : '2'}</div>
              <div>
                <div className="t">Lineups</div>
                <div className="s">
                  {built
                    ? `${build.num_entries_built.toLocaleString()} built · ${projectionSource}`
                    : 'Not built yet'}
                </div>
              </div>
            </div>
            <div className={`stage ${built ? 'on' : ''}`}>
              <div className="k">3</div>
              <div>
                <div className="t">Simulate</div>
                <div className="s">{built ? 'Ready to price' : 'Build a contest first'}</div>
              </div>
            </div>
          </div>

          {stackIntent && (
            <div className="notice" style={{ marginBottom: 14 }}>
              Sent from the Stacks table: <strong>{stackIntent.size}× {stackIntent.team}</strong>.
              The generator builds every stack archetype itself rather than forcing one, so this
              doesn&apos;t constrain the build — lineups featuring {stackIntent.team} are pulled to
              the front of the grid below instead.{' '}
              <button className="sm" style={{ marginLeft: 8 }} onClick={onClearIntent}>
                Dismiss
              </button>
            </div>
          )}

          <div className="build">
            {/* ----------------------------------------- settings rail */}
            <aside className="panel">
              <div className="panel-h">
                Settings
                <button className="sm" onClick={() => setCollapsed((v) => !v)}>
                  {collapsed ? 'Expand' : 'Collapse'}
                </button>
              </div>

              {!collapsed && (
                <>
                  <div className="field">
                    <label>Contest</label>
                    <select value={contestType} onChange={(e) => setContestType(e.target.value)}>
                      {contestTypes &&
                        Object.entries(contestTypes).map(([key, c]) => (
                          <option key={key} value={key}>
                            {c.label}
                          </option>
                        ))}
                    </select>
                  </div>

                  <div className="field">
                    <label>Contest size</label>
                    <select
                      value={contestSize}
                      onChange={(e) => setContestSize(Number(e.target.value))}
                    >
                      {sizes.map((n) => (
                        <option key={n} value={n}>
                          {sizeLabel(n)}
                        </option>
                      ))}
                    </select>
                    <div className="hint" style={{ marginTop: 6 }}>
                      One number: the field size <em>and</em> how many lineups get built.
                    </div>
                  </div>

                  <div className="field">
                    <label>Projections</label>
                    <div className="hint">
                      {projectionSource === 'inhouse' ? 'In-house model' : 'RotoWire'} — change in
                      the Data menu.
                    </div>
                  </div>

                  <div className="field">
                    <div className="rowline">
                      <span>Slate games</span>
                      <button className="sm" onClick={() => setShowSlateGames((v) => !v)}>
                        {includedGames.size} of {slateGames.length}
                      </button>
                    </div>
                    {showSlateGames && (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginTop: 8 }}>
                        {slateGames.map((g) => (
                          <label key={g.pk} className="dim" style={{ fontSize: 12.5 }}>
                            <input
                              type="checkbox"
                              checked={includedGames.has(g.pk)}
                              onChange={() =>
                                setIncludedGames((prev) => {
                                  const next = new Set(prev)
                                  next.has(g.pk) ? next.delete(g.pk) : next.add(g.pk)
                                  return next
                                })
                              }
                            />
                            {g.away} @ {g.home}
                          </label>
                        ))}
                      </div>
                    )}
                  </div>

                  <MyLineupsCard
                    date={date}
                    optimizerLineups={optimizerLineups}
                    onChange={setTrayCount}
                  />

                  {trayCount > 0 && (
                    <div className="field">
                      <label className="rowline" style={{ cursor: 'pointer' }}>
                        <span>Use my lineups</span>
                        <input
                          type="checkbox"
                          checked={useMyLineups}
                          onChange={(e) => setUseMyLineups(e.target.checked)}
                        />
                      </label>
                      <div className="hint">
                        Your {trayCount} lineup{trayCount === 1 ? '' : 's'} lead the batch; the
                        generator builds the remaining {Math.max(contestSize - trayCount, 0).toLocaleString()}{' '}
                        as the field they&rsquo;re simulated against. The build audit then scores
                        yours instead of the field.
                      </div>
                    </div>
                  )}

                  <div className="field">
                    <div className="hint">
                      No salary floor, exposure cap or duplicate toggle: every entry is built toward
                      spending the cap, and a real contest field contains duplicates.
                    </div>
                  </div>

                  <div className="panel-foot">
                    <button
                      className="primary"
                      onClick={() => runBuild()}
                      disabled={build.status === 'loading'}
                    >
                      {build.status === 'loading'
                        ? 'Building…'
                        : `Build ${sizeLabel(contestSize)} contest`}
                    </button>
                    {built && (
                      <button
                        onClick={() => {
                          const next = reroll + 1
                          setReroll(next)
                          runBuild(next)
                        }}
                        title="Identical settings reproduce the identical contest. Re-roll draws a genuinely new one."
                      >
                        Re-roll
                      </button>
                    )}
                  </div>
                </>
              )}
            </aside>

            {/* ----------------------------------------- main column */}
            <div>
              {build.status === 'idle' && (
                <div className="card">
                  <p style={{ marginTop: 0, color: 'var(--text-secondary)' }}>
                    Set the contest and its size on the left, then build. Every lineup is
                    individually strong and built toward spending the cap, across every stack shape
                    a real field builds. No economics until you simulate.
                  </p>
                </div>
              )}

              {build.status === 'loading' && (
                <div className="card">
                  <div className="skeleton" style={{ width: '70%', marginBottom: 10 }} />
                  <div className="skeleton" style={{ width: '85%', marginBottom: 10 }} />
                  <div className="skeleton" style={{ width: '60%' }} />
                </div>
              )}

              {build.status === 'error' && (
                <>
                  <div className="notice error">{build.message}</div>
                  <button style={{ marginTop: 12 }} onClick={() => runBuild()}>
                    Try again
                  </button>
                </>
              )}

              {built && (
                <>
                  <div className="sim-grid">
                    <div className="stat">
                      <span>Lineups</span>
                      <b>{build.num_entries_built.toLocaleString()}</b>
                      <small>
                        {build.num_distinct_entries.toLocaleString()} distinct · $
                        {build.summary.median_salary_used.toLocaleString()} median
                      </small>
                    </div>
                    <div className="stat">
                      <span>Avg projection</span>
                      <b>{build.summary.avg_projected_points.toFixed(1)}</b>
                      <small>
                        range {build.summary.min_projected_points.toFixed(0)}–
                        {build.summary.max_projected_points.toFixed(0)}
                      </small>
                    </div>
                    <div className="stat">
                      <span>Top stack usage</span>
                      <b>
                        {build.stack_shapes?.[0]
                          ? `${build.stack_shapes[0].shape} ${build.stack_shapes[0].pct}%`
                          : '—'}
                      </b>
                      <small>
                        {build.stack_shapes
                          ?.slice(1, 3)
                          .map((s) => `${s.shape} ${s.pct}%`)
                          .join(' · ')}
                      </small>
                    </div>
                    <div className="stat">
                      <span>Avg ownership</span>
                      <b>{build.summary.avg_total_ownership_pct.toFixed(0)}%</b>
                      <small>summed across the 10 rostered</small>
                    </div>
                  </div>

                  <div className="controls" style={{ marginBottom: 12 }}>
                    <a href={api.contestEntriesCsvUrl(build.batch_id)}>
                      <button>Download full contest (CSV)</button>
                    </a>
                    <span className="dim" style={{ fontSize: 12 }}>
                      showing {cards.length} of {build.num_entries_built.toLocaleString()} lineups
                    </span>
                  </div>

                  <div className="lineups" style={{ marginBottom: 18 }}>
                    {cards.map((e, i) => (
                      <div className="lu" key={i}>
                        <div className="lu-h">
                          <b>
                            #{i + 1}{' '}
                            {e.stack_type && <span className="stack-tag">{e.stack_type}</span>}
                          </b>
                          <span>${e.salary_used.toLocaleString()}</span>
                        </div>
                        <ul>
                          {(e.players || []).map((p, slot) => (
                            <li key={p.id}>
                              <span className="pos">
                                {p.position || MLB_SLOT_LABELS[slot] || ''}
                              </span>
                              <span className="nm">{p.name}</span>
                              <span className="sal">
                                {p.salary ? `${(p.salary / 1000).toFixed(1)}k` : ''}
                              </span>
                              <span className="pr">
                                {p.projected_fpts != null ? p.projected_fpts.toFixed(1) : ''}
                              </span>
                            </li>
                          ))}
                        </ul>
                        <div className="lu-f">
                          <span>Proj {e.projected_points.toFixed(1)}</span>
                          <span>Own {e.total_ownership_pct.toFixed(0)}%</span>
                          <span>{e.stack || '—'}</span>
                        </div>
                      </div>
                    ))}
                  </div>

                  {/* Stage 3 lives in the same page rather than a
                      separate tab -- it operates on the batch built
                      directly above it. */}
                  <div className="section-head">
                    <h2>Simulate</h2>
                    <span className="hint">
                      price this contest — entry cost and payout curve set here
                    </span>
                  </div>
                  <ContestSimulatorPanel
                    date={date}
                    batch={build}
                    projectionSource={projectionSource}
                  />
                </>
              )}
            </div>
          </div>
        </>
      )}
    </>
  )
}
