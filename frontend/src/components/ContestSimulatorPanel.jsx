import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'

/**
 * The simulator: takes a contest the generator already built and works
 * out what it would actually pay.
 *
 * Split out of the contest generator on purpose. Building lineups and
 * pricing them are two different questions, and the inputs that answer
 * the second one -- what an entry costs, and how top-heavy the payout
 * curve is -- have nothing to do with how the lineups themselves were
 * constructed. Keeping them here means the same built contest can be
 * re-simulated under different economics without rebuilding a single
 * lineup.
 *
 * Entry cost is the load-bearing input: the prize pool is the contest's
 * size times the entry fee (less rake), so it sets every payout and
 * therefore every ROI on the page.
 */
export function ContestSimulatorPanel({ date, batch, projectionSource = 'rotowire', onOpenGenerator }) {
  const [mode, setMode] = useState('built') // 'built' | 'dk-entries'

  // Simulation inputs. Entry cost seeds from whatever the built
  // contest's preset assumed, so the first run is sane without typing.
  const [entryFee, setEntryFee] = useState('')
  const [firstPlacePct, setFirstPlacePct] = useState('')
  const [selfPlay, setSelfPlay] = useState(true)
  const [fieldSharpness, setFieldSharpness] = useState('marquee')
  const [atbatEngine, setAtbatEngine] = useState(false)
  const [reroll, setReroll] = useState(0)
  const [state, setState] = useState({ status: 'idle' })

  useEffect(() => {
    if (batch?.contest?.entry_fee != null) setEntryFee(String(batch.contest.entry_fee))
    setState({ status: 'idle' })
  }, [batch?.batch_id])

  // Portfolio shaping (post-hoc, on an already-simulated batch's real
  // results -- no rebuild). `originalReady` is the untouched full batch
  // a "Reshape" always starts back from, so shaping twice in a row
  // never compounds on top of an already-trimmed set.
  const [originalReady, setOriginalReady] = useState(null)
  const [roiBoosts, setRoiBoosts] = useState({})
  const [exposureCaps, setExposureCaps] = useState({})
  const [targetCount, setTargetCount] = useState('')
  const [globalMaxExposure, setGlobalMaxExposure] = useState('')
  const [reshaping, setReshaping] = useState(false)
  const [swapping, setSwapping] = useState(false)
  const [swapMode, setSwapMode] = useState('repair')
  const [swapResult, setSwapResult] = useState(null)

  const [requirePlayers, setRequirePlayers] = useState(new Set())
  const [excludePlayers, setExcludePlayers] = useState(new Set())
  const [requireTeams, setRequireTeams] = useState(new Set())
  const [excludeTeams, setExcludeTeams] = useState(new Set())
  const [stackTypeFilter, setStackTypeFilter] = useState(new Set())

  // "My DK entries" -- mirrors a real contest you've reserved entries
  // into and simulates its whole field. A simulation feature, so it
  // lives here rather than alongside the lineup builder.
  const [dkUploading, setDkUploading] = useState(false)
  const [dkUploadError, setDkUploadError] = useState('')
  const [dkContests, setDkContests] = useState(null)
  const [dkContestId, setDkContestId] = useState('')
  const [dkFieldSize, setDkFieldSize] = useState(1000)
  const [dkPrizePool, setDkPrizePool] = useState(1000)
  const [dkFirstPlacePct, setDkFirstPlacePct] = useState(20)

  const availableTeams = useMemo(() => {
    const teams = new Set()
    for (const e of originalReady?.sample_entries || []) {
      for (const p of e.players || []) if (p.team) teams.add(p.team)
    }
    return [...teams].sort()
  }, [originalReady])
  const availableStackTypes = useMemo(() => {
    const shapes = new Set()
    for (const e of originalReady?.sample_entries || []) {
      if (e.stack_type) shapes.add(e.stack_type)
    }
    return [...shapes].sort()
  }, [originalReady])

  function applyReady(ready) {
    setState(ready)
    setOriginalReady(ready)
    setRoiBoosts({})
    setExposureCaps({})
    setTargetCount('')
    setGlobalMaxExposure('')
    setRequirePlayers(new Set())
    setExcludePlayers(new Set())
    setRequireTeams(new Set())
    setExcludeTeams(new Set())
    setStackTypeFilter(new Set())
    setSwapResult(null)
  }

  function toggleSetMember(setter, value) {
    setter((prev) => {
      const next = new Set(prev)
      next.has(value) ? next.delete(value) : next.add(value)
      return next
    })
  }

  async function run(rerollOverride = null) {
    if (!batch?.batch_id) return
    setState({ status: 'loading' })
    try {
      const result = await api.simulateContestBatch(batch.batch_id, {
        date,
        entryFee: entryFee.trim() ? Number(entryFee) : null,
        firstPlacePct: firstPlacePct === '' ? null : Number(firstPlacePct),
        selfPlay,
        fieldSharpness,
        engine: atbatEngine ? 'atbat' : 'bootstrap',
        reroll: rerollOverride ?? reroll,
      })
      applyReady({ status: 'ready', simulated: true, mode: 'built', ...result })
    } catch (err) {
      setState({ status: 'error', message: err.message })
    }
  }

  async function uploadDkFile(file) {
    setDkUploading(true)
    setDkUploadError('')
    try {
      const result = await api.uploadDkEntries(date, file)
      setDkContests(result.contests)
      setDkContestId(result.contests[0]?.contest_id || '')
    } catch (err) {
      setDkUploadError(err.message)
      setDkContests(null)
    } finally {
      setDkUploading(false)
    }
  }

  const selectedDkContest = dkContests?.find((c) => c.contest_id === dkContestId)

  async function runDkEntries() {
    setState({ status: 'loading' })
    try {
      const result = await api.simulateDkEntries(date, dkContestId, {
        fieldSize: dkFieldSize,
        prizePool: dkPrizePool,
        firstPlacePct: dkFirstPlacePct,
        projectionSource,
        engine: atbatEngine ? 'atbat' : 'bootstrap',
        fieldSharpness,
      })
      applyReady({ status: 'ready', simulated: true, mode: 'dk-entries', ...result })
    } catch (err) {
      setState({ status: 'error', message: err.message })
    }
  }

  // Recomputes the same aggregate shape the simulator's own summary
  // uses, from whatever subset of results survived a reshape.
  function summarizeResults(results, fee) {
    const n = results.length
    const avg = (key) => results.reduce((sum, r) => sum + r[key], 0) / n
    const totalExpectedPayout = results.reduce((sum, r) => sum + r.expected_payout, 0)
    const totalEntryCost = n * fee
    const round = (v, d) => Math.round(v * 10 ** d) / 10 ** d
    return {
      avg_cash_probability_pct: round(avg('cash_probability_pct'), 1),
      avg_first_place_pct: round(avg('first_place_pct'), 2),
      avg_top_1pct_pct: round(avg('top_1pct_pct'), 2),
      avg_top_10pct_pct: round(avg('top_10pct_pct'), 2),
      avg_roi_pct: round(avg('roi_pct'), 1),
      total_entry_cost: round(totalEntryCost, 2),
      total_expected_payout: round(totalExpectedPayout, 2),
      estimated_net_profit: round(totalExpectedPayout - totalEntryCost, 2),
    }
  }

  async function reshape() {
    if (!originalReady) return
    setReshaping(true)
    try {
      const result = await api.reshapeContestEntries(originalReady.batch_id, {
        targetCount: targetCount.trim() ? Number(targetCount) : null,
        maxExposurePct: globalMaxExposure.trim() ? Number(globalMaxExposure) : null,
        playerExposureCaps: Object.keys(exposureCaps).length ? exposureCaps : null,
        roiBoosts: Object.keys(roiBoosts).length ? roiBoosts : null,
        requireTeams: requireTeams.size ? [...requireTeams] : null,
        excludeTeams: excludeTeams.size ? [...excludeTeams] : null,
        requirePlayerIds: requirePlayers.size ? [...requirePlayers] : null,
        excludePlayerIds: excludePlayers.size ? [...excludePlayers] : null,
        stackTypes: stackTypeFilter.size ? [...stackTypeFilter] : null,
      })
      setState({
        ...originalReady,
        batch_id: result.batch_id,
        num_entries_built: result.num_kept,
        num_dropped: result.num_dropped,
        num_filtered_out: result.num_filtered_out,
        exposure: result.exposure,
        sample_entries: result.sample_entries,
        results: result.results,
        summary: summarizeResults(result.results, originalReady.contest.entry_fee),
        reshaped: true,
      })
    } catch (err) {
      setState({ status: 'error', message: err.message })
    } finally {
      setReshaping(false)
    }
  }

  // Late swap: repair this batch against the CURRENT slate. Runs off
  // whatever batch is showing (reshaped or not) rather than the
  // original, since a swap is about right now, not about undoing
  // earlier shaping.
  async function lateSwap() {
    if (!state.batch_id) return
    setSwapping(true)
    setSwapResult(null)
    try {
      const result = await api.lateSwapContestEntries(state.batch_id, {
        date,
        mode: swapMode,
        projectionSource,
      })
      setState((prev) => ({
        ...prev,
        batch_id: result.batch_id,
        exposure: result.exposure,
        sample_entries: result.sample_entries,
        results: result.results,
        summary: Object.keys(result.summary || {}).length ? result.summary : prev.summary,
      }))
      setSwapResult(result)
    } catch (err) {
      setSwapResult({ error: err.message })
    } finally {
      setSwapping(false)
    }
  }

  function resetShaping() {
    setRoiBoosts({})
    setExposureCaps({})
    setTargetCount('')
    setGlobalMaxExposure('')
    setRequirePlayers(new Set())
    setExcludePlayers(new Set())
    setRequireTeams(new Set())
    setExcludeTeams(new Set())
    setStackTypeFilter(new Set())
    if (originalReady) setState(originalReady)
  }

  function setRoiBoost(id, value) {
    setRoiBoosts((prev) => {
      const next = { ...prev }
      if (value.trim() === '' || Number(value) === 0) delete next[id]
      else next[id] = Number(value)
      return next
    })
  }

  function setExposureCap(id, value) {
    setExposureCaps((prev) => {
      const next = { ...prev }
      if (value.trim() === '') delete next[id]
      else next[id] = Number(value)
      return next
    })
  }

  const feeNumber = entryFee.trim() ? Number(entryFee) : (batch?.contest?.entry_fee ?? 0)
  const projectedPool =
    batch && feeNumber ? Math.round(batch.field_size * feeNumber * 0.85) : null

  return (
    <div className="card">
      <div className="controls" style={{ marginBottom: 14 }}>
        <button className={mode === 'built' ? 'primary' : ''} onClick={() => setMode('built')}>
          Built contest
        </button>
        <button
          className={mode === 'dk-entries' ? 'primary' : ''}
          onClick={() => setMode('dk-entries')}
        >
          My DK entries
        </button>
      </div>

      {mode === 'built' && !batch && (
        <>
          <p style={{ marginTop: 0, color: 'var(--text-secondary)' }}>
            Nothing to simulate yet. Build a contest in the Contest Generator tab, then hit
            &quot;Simulate this contest&quot; there to hand it over here.
          </p>
          {onOpenGenerator && <button onClick={onOpenGenerator}>Go to Contest Generator</button>}
        </>
      )}

      {mode === 'built' && batch && (
        <>
          <div className="controls" style={{ marginBottom: 10, flexWrap: 'wrap' }}>
            <span className="badge ok">
              {batch.num_entries_built.toLocaleString()} lineups loaded
            </span>
            <span className="badge">{batch.field_size.toLocaleString()}-entry contest</span>
            <span className="badge">{batch.contest?.label}</span>
          </div>

          <div className="controls" style={{ marginBottom: 14, flexWrap: 'wrap' }}>
            <label
              className="dim"
              style={{ fontSize: 13 }}
              title="What one entry costs. The prize pool is the contest's size times this, less rake -- so this single number sets every payout and every ROI below."
            >
              Entry cost ${' '}
              <input
                type="number"
                min="0"
                step="0.25"
                value={entryFee}
                onChange={(e) => setEntryFee(e.target.value)}
                style={{ width: 80 }}
              />
            </label>
            <label
              className="dim"
              style={{ fontSize: 13 }}
              title="What share of the prize pool 1st place wins. A lower value flattens the payout curve -- more spread across the paid ranks, less concentrated at 1st -- which changes every entry's simulated ROI."
            >
              % to first{' '}
              <select value={firstPlacePct} onChange={(e) => setFirstPlacePct(e.target.value)}>
                <option value="">preset default</option>
                {[5, 10, 15, 20, 25, 30, 35].map((n) => (
                  <option key={n} value={n}>
                    {n}%
                  </option>
                ))}
              </select>
            </label>
            {projectedPool != null && (
              <span
                className="badge"
                title="Contest size x entry cost, less the 15% rake this app models -- what the simulator will pay out across the whole payout curve."
              >
                ~${projectedPool.toLocaleString()} prize pool
              </span>
            )}
            <label
              className="dim"
              style={{ fontSize: 13, display: 'flex', alignItems: 'center', gap: 6 }}
              title="On (default): the generator built the whole contest, so this ranks every lineup in it against every other one in the same simulated trial. Off: rank them against a separately-sampled realistic public field instead -- a different question, closer to 'how would these do against real public rosters'."
            >
              <input
                type="checkbox"
                checked={selfPlay}
                onChange={(e) => setSelfPlay(e.target.checked)}
              />
              This contest vs. itself
            </label>
            {!selfPlay && (
              <label
                className="dim"
                style={{ fontSize: 13 }}
                title="Contest stakes, and so who is in the field. Low: a cheap contest -- newer, safer entrants, the chalkiest lineups. Marquee (default): a milly-maker or other massive field, a mix of both. High: high stakes, where players limit chalk and hunt low-owned plays that have a real matchup edge behind them."
              >
                Field sharpness{' '}
                <select value={fieldSharpness} onChange={(e) => setFieldSharpness(e.target.value)}>
                  <option value="low">Low</option>
                  <option value="marquee">Marquee</option>
                  <option value="high">High</option>
                </select>
              </label>
            )}
            <label
              className="dim"
              style={{ fontSize: 13, display: 'flex', alignItems: 'center', gap: 6 }}
              title="Off (default): sample each player's own historical DK-point outcome pool, with a team-multiplier for correlation. On: run genuine at-bat-level (plate-appearance by plate-appearance) simulated games for the whole slate instead -- correlation is a natural consequence of shared simulated game state. Requires a CONFIRMED lineup on both sides and a probable pitcher for every game on the slate, or the run fails with a clear error."
            >
              <input
                type="checkbox"
                checked={atbatEngine}
                onChange={(e) => setAtbatEngine(e.target.checked)}
              />
              At-bat-level engine (beta)
            </label>
            <button className="primary" onClick={() => run()} disabled={state.status === 'loading'}>
              {state.status === 'loading' ? 'Simulating…' : 'Run simulation'}
            </button>
            {state.status === 'ready' && (
              <button
                onClick={() => {
                  const next = reroll + 1
                  setReroll(next)
                  run(next)
                }}
                title="Identical settings reproduce identical draws. Re-roll runs a genuinely new set of simulated trials on the same contest."
              >
                Re-roll
              </button>
            )}
          </div>

          {batch.num_entries_built > 5000 && (
            <div className="notice" style={{ marginBottom: 14 }}>
              This contest has {batch.num_entries_built.toLocaleString()} lineups; the simulator
              runs 10,000 Monte Carlo trials over a 5,000-lineup slice of it and projects the
              results back onto the full {batch.field_size.toLocaleString()}-entry field.
            </div>
          )}
        </>
      )}

      {mode === 'dk-entries' && (
        <div className="controls" style={{ marginBottom: 14, flexWrap: 'wrap' }}>
          <input
            type="file"
            accept=".csv"
            id="dk-entries-file-sim"
            style={{ display: 'none' }}
            onChange={(e) => e.target.files[0] && uploadDkFile(e.target.files[0])}
          />
          <button onClick={() => document.getElementById('dk-entries-file-sim').click()} disabled={dkUploading}>
            {dkUploading ? 'Uploading…' : dkContests ? 'Re-upload DK entries CSV' : 'Upload DK entries CSV'}
          </button>
          {dkContests && dkContests.length > 0 && (
            <>
              <label
                className="dim"
                style={{ fontSize: 13 }}
                title="Only the entry fee is read from the file -- field size, prize pool, and 1st place % below describe the real contest and are hand-entered"
              >
                Contest{' '}
                <select value={dkContestId} onChange={(e) => setDkContestId(e.target.value)}>
                  {dkContests.map((c) => (
                    <option key={c.contest_id} value={c.contest_id}>
                      {c.contest_name} (${c.entry_fee?.toFixed(2)} entry, {c.num_entries} of your own
                      entries reserved)
                    </option>
                  ))}
                </select>
              </label>
              {selectedDkContest && (
                <span className="badge" title="Read straight from the uploaded file">
                  ${selectedDkContest.entry_fee?.toFixed(2)} entry fee
                </span>
              )}
              <label className="dim" style={{ fontSize: 13 }}>
                Total contest entries{' '}
                <input
                  type="number"
                  min="1"
                  value={dkFieldSize}
                  onChange={(e) => setDkFieldSize(Math.max(1, Number(e.target.value) || 1))}
                  style={{ width: 90 }}
                />
              </label>
              <label className="dim" style={{ fontSize: 13 }}>
                Prize pool{' '}
                <input
                  type="number"
                  min="0"
                  step="0.01"
                  value={dkPrizePool}
                  onChange={(e) => setDkPrizePool(Math.max(0, Number(e.target.value) || 0))}
                  style={{ width: 100 }}
                />
              </label>
              <label className="dim" style={{ fontSize: 13 }}>
                1st place %{' '}
                <input
                  type="number"
                  min="0"
                  max="100"
                  step="0.1"
                  value={dkFirstPlacePct}
                  onChange={(e) =>
                    setDkFirstPlacePct(Math.max(0, Math.min(100, Number(e.target.value) || 0)))
                  }
                  style={{ width: 70 }}
                />
              </label>
              <label
                className="dim"
                style={{ fontSize: 13 }}
                title="Contest stakes, and so who is in the field. Low: a cheap contest -- newer, safer entrants, the chalkiest lineups. Marquee (default): a milly-maker or other massive field, a mix of both. High: high stakes, where players limit chalk and hunt low-owned plays that have a real matchup edge behind them."
              >
                Field sharpness{' '}
                <select value={fieldSharpness} onChange={(e) => setFieldSharpness(e.target.value)}>
                  <option value="low">Low</option>
                  <option value="marquee">Marquee</option>
                  <option value="high">High</option>
                </select>
              </label>
              <label
                className="dim"
                style={{ fontSize: 13, display: 'flex', alignItems: 'center', gap: 6 }}
                title="Off (default): sample each player's own historical DK-point outcome pool. On: run genuine at-bat-level simulated games for the whole slate instead -- requires a CONFIRMED lineup on both sides and a probable pitcher for every game."
              >
                <input
                  type="checkbox"
                  checked={atbatEngine}
                  onChange={(e) => setAtbatEngine(e.target.checked)}
                />
                At-bat-level engine (beta)
              </label>
              <button
                className="primary"
                onClick={runDkEntries}
                disabled={state.status === 'loading' || !dkContestId}
              >
                {state.status === 'loading' ? 'Simulating…' : 'Mirror & simulate contest'}
              </button>
            </>
          )}
        </div>
      )}

      {dkUploadError && <div className="notice error" style={{ marginBottom: 14 }}>{dkUploadError}</div>}

      {state.status === 'idle' && mode === 'dk-entries' && (
        <p style={{ marginTop: 0, color: 'var(--text-secondary)' }}>
          Upload the entries CSV DraftKings gives you (the same &quot;bulk entries&quot;
          export/upload file from their site) so this can read the entry fee for a real contest
          you&apos;ve reserved entries into — that&apos;s the file&apos;s only real job here. This
          then builds an ownership-weighted sample standing in for that contest&apos;s{' '}
          <em>entire field</em> and simulates it as one self-contained population: every lineup
          ranked against every other lineup. Browse the results to see which archetypes actually
          perform. Total contest entries, prize pool, and 1st-place % are hand-entered since a bulk
          entries export has no payout-table data or true field size at all.
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
          <button style={{ marginTop: 12 }} onClick={mode === 'dk-entries' ? runDkEntries : () => run()}>
            Try again
          </button>
        </>
      )}

      {state.status === 'ready' && (
        <>
          <div className="controls" style={{ marginBottom: 12, flexWrap: 'wrap' }}>
            <span className="badge ok">
              {state.num_entries_built.toLocaleString()} lineups simulated
            </span>
            <span className="badge">{state.field_size.toLocaleString()}-entry contest</span>
            <span className="badge">${state.contest.entry_fee.toLocaleString()} entry</span>
            <span className="badge">{state.paid_count.toLocaleString()} paid</span>
            <span className="badge">${state.prize_pool.toLocaleString()} prize pool</span>
            {state.first_place_pct != null && (
              <span
                className="badge"
                title="Share of the prize pool 1st place wins in this run's simulated payout curve"
              >
                {state.first_place_pct}% to first
              </span>
            )}
            <span className="badge">{state.num_trials.toLocaleString()} sim trials</span>
            {state.engine === 'atbat' && (
              <span
                className="badge"
                title="Every trial is a genuine plate-appearance-by-plate-appearance simulated game for the whole slate, not a resampled historical outcome pool."
              >
                at-bat engine
              </span>
            )}
            {state.mode === 'built' && (
              <span
                className="badge"
                title={
                  state.self_play
                    ? 'Every lineup in this contest ranked against every other lineup in it, in the same simulated trial.'
                    : 'This contest ranked against a separately-sampled realistic public field.'
                }
              >
                {state.self_play ? 'vs. itself' : 'vs. public field'}
              </span>
            )}
            <a
              href={api.contestEntriesCsvUrl(state.batch_id)}
              title="Download every simulated lineup as a CSV"
            >
              <button>Download results (CSV)</button>
            </a>
          </div>

          {/* Late swap. DK locks each roster spot at that player's OWN
              game start, so a slate spanning several hours of first
              pitches leaves real editing time after the contest has
              begun -- and neither DK nor FanDuel auto-replaces a
              scratched or postponed player, so an entry still holding
              one just scores zero there. */}
          <div className="card" style={{ marginBottom: 14 }}>
            <div className="controls" style={{ flexWrap: 'wrap' }}>
              <strong style={{ fontSize: 13 }}>Late swap</strong>
              <label className="dim" style={{ fontSize: 13 }}>
                Mode{' '}
                <select value={swapMode} onChange={(e) => setSwapMode(e.target.value)}>
                  <option value="repair">Repair (scratched / postponed only)</option>
                  <option value="refresh">Refresh (also drop big projection falls)</option>
                </select>
              </label>
              <button className="primary" onClick={lateSwap} disabled={swapping}>
                {swapping ? 'Swapping…' : 'Late swap this batch'}
              </button>
              <span className="dim" style={{ fontSize: 12 }}>
                only replaces players whose game hasn’t started
              </span>
            </div>

            {swapResult?.error && (
              <div className="notice error" style={{ marginTop: 8 }}>{swapResult.error}</div>
            )}

            {swapResult && !swapResult.error && (
              <div className="sub-line" style={{ marginTop: 8 }}>
                {swapResult.total_swaps === 0 ? (
                  <>
                    Nothing to swap — no rostered player is scratched or in a postponed game
                    {swapResult.open_game_count === 0
                      ? ', and every game has already locked.'
                      : ` across the ${swapResult.open_game_count} game${swapResult.open_game_count === 1 ? '' : 's'} still open.`}
                  </>
                ) : (
                  <>
                    Made <strong>{swapResult.total_swaps.toLocaleString()}</strong> swap
                    {swapResult.total_swaps === 1 ? '' : 's'} across{' '}
                    <strong>{swapResult.entries_changed.toLocaleString()}</strong> entr
                    {swapResult.entries_changed === 1 ? 'y' : 'ies'}
                    {swapResult.replaced_players?.length > 0 && (
                      <>
                        {' '}— out:{' '}
                        {swapResult.replaced_players
                          .slice(0, 5)
                          .map((p) => `${p.name} (${p.entry_count})`)
                          .join(', ')}
                      </>
                    )}
                    .{swapResult.resimulated ? ' Re-simulated against the swapped field.' : ''}
                  </>
                )}
                {swapResult.stranded_players?.length > 0 && (
                  <div className="badge risk" style={{ marginTop: 6 }}>
                    {swapResult.stranded_players
                      .map((p) => `${p.name} (${p.entry_count})`)
                      .join(', ')}{' '}
                    — dead but already locked, can’t be swapped
                  </div>
                )}
                {swapResult.unfillable_players?.length > 0 && (
                  <div className="badge risk" style={{ marginTop: 6 }}>
                    {swapResult.unfillable_players
                      .map((p) => `${p.name} (${p.entry_count})`)
                      .join(', ')}{' '}
                    — no affordable legal replacement available
                  </div>
                )}
              </div>
            )}
          </div>

          <div className="card" style={{ marginBottom: 14 }}>
            <div className="sub-line" style={{ marginBottom: 8 }}>
              Simulated economics — {state.num_trials.toLocaleString()} Monte Carlo trials of each
              player&apos;s own real historical outcomes, with team correlation for hitters.
              {state.self_play
                ? ' Every lineup in this contest is ranked against every other lineup in the same simulated trial.'
                : ' Cash probability and payout are genuine simulated results against a separately-sampled realistic public field.'}
            </div>
            <div className="controls" style={{ flexWrap: 'wrap' }}>
              <span className="badge">
                {state.summary.avg_cash_probability_pct}% avg cash probability
              </span>
              <span className="badge">{state.summary.avg_first_place_pct}% avg 1st place</span>
              <span className="badge">{state.summary.avg_top_1pct_pct}% avg top 1%</span>
              <span className="badge">{state.summary.avg_top_10pct_pct}% avg top 10%</span>
              <span className={`badge ${state.summary.avg_roi_pct >= 0 ? 'ok' : 'risk'}`}>
                {state.summary.avg_roi_pct >= 0 ? '+' : ''}
                {state.summary.avg_roi_pct}% avg ROI
              </span>
              {state.simulated_winning_score && (
                <span
                  className="badge"
                  title="The best score in the whole field, per simulated contest -- what it actually takes to WIN this one. Not comparable to any single lineup's ceiling: a ceiling is a statistic about one lineup, a winning score is the maximum over thousands of them, so it is always far higher."
                >
                  {state.simulated_winning_score.p50} wins it (
                  {state.simulated_winning_score.p10}&ndash;{state.simulated_winning_score.p90})
                </span>
              )}
              <span className="badge">${state.summary.total_entry_cost.toLocaleString()} cost</span>
              <span
                className="badge"
                title="Every lineup's own average payout, summed. Unlike a single lineup's average -- which is carried by rare hits and reads $0 in most trials -- a sum across thousands of lineups is a genuinely stable number: it lands on the prize pool, which is what the contest pays out by construction."
              >
                ${state.summary.total_expected_payout.toLocaleString()} avg total payout
              </span>
              <span className={`badge ${state.summary.estimated_net_profit >= 0 ? 'ok' : 'risk'}`}>
                {state.summary.estimated_net_profit >= 0 ? '+' : ''}$
                {state.summary.estimated_net_profit.toLocaleString()} est. net
              </span>
              <span
                className="badge"
                title="Cumulative (log-product) ownership, averaged across the batch -- how consistently chalky a typical entry's players are TOGETHER, distinct from summed ownership%."
              >
                {state.summary.avg_duplication_risk} avg duplication risk
              </span>
            </div>

            {state.field_baseline && (
              <div className="controls" style={{ flexWrap: 'wrap', marginTop: 8 }}>
                <span
                  className="badge"
                  title="What ANY random, zero-skill entry should expect from this exact contest, on average -- a closed-form fact from the contest's own entry fee, prize pool, and payout%, not a simulation."
                >
                  field baseline: {state.field_baseline.avg_cash_probability_pct}% cash,{' '}
                  {state.field_baseline.avg_roi_pct >= 0 ? '+' : ''}
                  {state.field_baseline.avg_roi_pct}% ROI
                </span>
                {/* "Your edge" only means anything when the entries being
                    averaged are a genuinely DIFFERENT population from the
                    field they're compared against. Ranking a contest
                    against ITSELF makes this tautologically ~0. */}
                {!state.self_play && state.mode !== 'dk-entries' && (
                  <span
                    className={`badge ${
                      state.summary.avg_roi_pct - state.field_baseline.avg_roi_pct >= 0 ? 'ok' : 'risk'
                    }`}
                    title="This batch's own avg ROI minus the field baseline's -- the real, field-beating edge, isolated from what the field's own rake-driven baseline already accounts for."
                  >
                    your edge: {state.summary.avg_roi_pct - state.field_baseline.avg_roi_pct >= 0 ? '+' : ''}
                    {(state.summary.avg_roi_pct - state.field_baseline.avg_roi_pct).toFixed(1)} pts ROI
                  </span>
                )}
              </div>
            )}
          </div>

          <div className="card" style={{ marginBottom: 14 }}>
            <div className="sub-line" style={{ marginBottom: 8 }}>
              Shape your portfolio — no rebuild, this re-ranks and re-filters the real simulated
              results above. Filters narrow the pool first; then an ROI boost or exposure cap (set
              per player in the table below) ranks and trims what&apos;s left. Then Reshape.
            </div>
            {(availableTeams.length > 0 || availableStackTypes.length > 0) && (
              <div className="controls" style={{ flexWrap: 'wrap', marginBottom: 8 }}>
                {availableTeams.length > 0 && (
                  <>
                    <label
                      className="dim"
                      style={{ fontSize: 13 }}
                      title="Keep only entries rostering a player from EVERY team selected"
                    >
                      Require team(s){' '}
                      <select
                        multiple
                        size={Math.min(4, availableTeams.length)}
                        value={[...requireTeams]}
                        onChange={(e) =>
                          setRequireTeams(new Set([...e.target.selectedOptions].map((o) => o.value)))
                        }
                        style={{ minWidth: 90 }}
                      >
                        {availableTeams.map((t) => (
                          <option key={t} value={t}>
                            {t}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label
                      className="dim"
                      style={{ fontSize: 13 }}
                      title="Drop any entry rostering a player from ANY team selected"
                    >
                      Exclude team(s){' '}
                      <select
                        multiple
                        size={Math.min(4, availableTeams.length)}
                        value={[...excludeTeams]}
                        onChange={(e) =>
                          setExcludeTeams(new Set([...e.target.selectedOptions].map((o) => o.value)))
                        }
                        style={{ minWidth: 90 }}
                      >
                        {availableTeams.map((t) => (
                          <option key={t} value={t}>
                            {t}
                          </option>
                        ))}
                      </select>
                    </label>
                  </>
                )}
                {availableStackTypes.length > 0 && (
                  <label
                    className="dim"
                    style={{ fontSize: 13 }}
                    title="Keep only entries whose own stack shape (e.g. 5-3, 4-4) is one of these"
                  >
                    Stack shape(s){' '}
                    <select
                      multiple
                      size={Math.min(4, availableStackTypes.length)}
                      value={[...stackTypeFilter]}
                      onChange={(e) =>
                        setStackTypeFilter(new Set([...e.target.selectedOptions].map((o) => o.value)))
                      }
                      style={{ minWidth: 70 }}
                    >
                      {availableStackTypes.map((s) => (
                        <option key={s} value={s}>
                          {s}
                        </option>
                      ))}
                    </select>
                  </label>
                )}
                {(requirePlayers.size > 0 || excludePlayers.size > 0) && (
                  <span
                    className="badge"
                    title="Set per-player in the Req/Excl columns of the exposure table below"
                  >
                    {requirePlayers.size} required player(s), {excludePlayers.size} excluded player(s)
                  </span>
                )}
              </div>
            )}
            <div className="controls" style={{ flexWrap: 'wrap' }}>
              <label className="dim" style={{ fontSize: 13 }}>
                Keep top{' '}
                <input
                  type="number"
                  min="1"
                  placeholder={String(originalReady?.num_entries_built ?? '')}
                  value={targetCount}
                  onChange={(e) => setTargetCount(e.target.value)}
                  style={{ width: 80 }}
                />
              </label>
              <label
                className="dim"
                style={{ fontSize: 13 }}
                title="Caps any player's exposure across the FINAL kept portfolio (not the original batch) -- overridden per-player by the Cap column below"
              >
                Max exposure{' '}
                <input
                  type="number"
                  min="0"
                  max="100"
                  placeholder="—"
                  value={globalMaxExposure}
                  onChange={(e) => setGlobalMaxExposure(e.target.value)}
                  style={{ width: 70 }}
                />
                %
              </label>
              <button className="primary" onClick={reshape} disabled={reshaping || !originalReady}>
                {reshaping ? 'Reshaping…' : 'Reshape'}
              </button>
              {state.reshaped && <button onClick={resetShaping}>Reset to full batch</button>}
              {state.reshaped && (
                <span className="badge" title="How many of the original batch's entries survived this reshape">
                  {state.num_entries_built} kept, {state.num_dropped} dropped
                  {state.num_filtered_out > 0 ? `, ${state.num_filtered_out} filtered out` : ''}
                </span>
              )}
            </div>
          </div>

          {state.exposure.length > 0 && (
            <div className="card table-wrap" style={{ marginBottom: 14 }}>
              <div className="sub-line" style={{ marginBottom: 8 }}>
                Exposure across the batch
              </div>
              <table>
                <thead>
                  <tr>
                    <th>Player</th>
                    <th>Team</th>
                    <th className="num">Entries</th>
                    <th className="num">Exposure</th>
                    <th
                      className="num"
                      title="This player's own average simulated ROI, across every lineup in the batch that rosters him -- which INDIVIDUAL PLAYERS are actually driving the batch's real simulated payoff, not just who's rostered the most."
                    >
                      ROI
                    </th>
                    <th
                      className="num"
                      title="ROI percentage points to add to every lineup containing this player, for RANKING purposes only -- the real simulated roi_pct is never changed. Negative nudges a player's lineups down."
                    >
                      Boost (pts)
                    </th>
                    <th
                      className="num"
                      title="This player's own exposure cap in the reshaped portfolio -- overrides the global Max exposure above"
                    >
                      Cap %
                    </th>
                    <th className="num" title="Filter: keep only entries rostering this player">
                      Req
                    </th>
                    <th className="num" title="Filter: drop any entry rostering this player">
                      Excl
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {state.exposure.map((e) => (
                    <tr key={e.id}>
                      <td>{e.name}</td>
                      <td className="dim">{e.team}</td>
                      <td className="num">{e.count}</td>
                      <td className="num">{e.pct}%</td>
                      <td className="num">
                        {e.avg_roi_pct != null ? (
                          <span className={`badge ${e.avg_roi_pct >= 0 ? 'ok' : 'risk'}`}>
                            {e.avg_roi_pct >= 0 ? '+' : ''}
                            {e.avg_roi_pct}%
                          </span>
                        ) : (
                          <span className="dim">—</span>
                        )}
                      </td>
                      <td className="num">
                        <input
                          type="number"
                          step="1"
                          placeholder="—"
                          value={roiBoosts[e.id] ?? ''}
                          onChange={(ev) => setRoiBoost(e.id, ev.target.value)}
                          style={{ width: 64 }}
                        />
                      </td>
                      <td className="num">
                        <input
                          type="number"
                          min="0"
                          max="100"
                          placeholder="—"
                          value={exposureCaps[e.id] ?? ''}
                          onChange={(ev) => setExposureCap(e.id, ev.target.value)}
                          style={{ width: 56 }}
                        />
                      </td>
                      <td className="num">
                        <input
                          type="checkbox"
                          checked={requirePlayers.has(e.id)}
                          onChange={() => toggleSetMember(setRequirePlayers, e.id)}
                        />
                      </td>
                      <td className="num">
                        <input
                          type="checkbox"
                          checked={excludePlayers.has(e.id)}
                          onChange={() => toggleSetMember(setExcludePlayers, e.id)}
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {state.sample_entries.length > 0 && (
            <div className="card table-wrap" style={{ marginBottom: 8 }}>
              <div className="sub-line" style={{ marginBottom: 8 }}>
                Best lineups — showing {state.sample_entries.length} of{' '}
                {state.num_entries_built.toLocaleString()}, ranked by top-1% rate
              </div>
              <table>
                <thead>
                  <tr>
                    <th className="num">#</th>
                    <th className="num" title="How many exact copies of this lineup are in the batch">
                      Dup
                    </th>
                    <th className="num">Salary</th>
                    <th title="Stack shape, e.g. 5-3 = 5 hitters from one team + 3 from another">Stack</th>
                    <th title="Teams in the stack, largest group first">Teams</th>
                    <th className="num">Proj FPTS</th>
                    <th className="num" title="5th percentile -- this lineup scored below this in only 1 of 20 simulated trials.">
                      Floor
                    </th>
                    <th className="num" title="95th percentile -- this lineup scored above this in only 1 of 20 simulated trials.">
                      Ceiling
                    </th>
                    <th className="num">Own%</th>
                    <th
                      className="num"
                      title="Cumulative (log-product) ownership -- how consistently chalky every player in this lineup is TOGETHER, distinct from Own% (a sum)."
                    >
                      Dup. risk
                    </th>
                    <th className="num">Cash %</th>
                    <th className="num" title="How often this lineup finished 1st out of the whole simulated contest">
                      1st %
                    </th>
                    <th className="num" title="How often this lineup finished in the top 1% of the whole simulated contest">
                      Top 1%
                    </th>
                    <th className="num" title="How often this lineup finished in the top 10% of the whole simulated contest">
                      Top 10%
                    </th>
                    <th
                      className="num"
                      title="This lineup's MEAN payout across all simulated trials -- not what a typical run returns. Payouts are enormously right-skewed: a lineup cashing 29% of the time collects nothing in the other 71%, so its median payout is $0 and the average is carried entirely by the runs where it hits. Hover a cell for that lineup's own 10th-90th percentile range."
                    >
                      Avg payout
                    </th>
                    <th
                      className="num"
                      title="(expected payout - entry fee) / entry fee, ± its Monte Carlo standard error. Top-heavy payouts make per-lineup ROI dominated by rare first-place hits, so a value smaller than its own ± is noise (greyed out) -- that's also why rows are ranked by Top 1% rather than ROI."
                    >
                      ROI % (±SE)
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {state.sample_entries.map((e, i) => {
                    const r = state.results[i]
                    return (
                      <tr key={i}>
                        <td className="num">{i + 1}</td>
                        <td className="num">
                          {e.duplicate_count > 1 ? (
                            <span className="badge">×{e.duplicate_count}</span>
                          ) : (
                            <span className="dim">—</span>
                          )}
                        </td>
                        <td className="num">${e.salary_used.toLocaleString()}</td>
                        <td>{e.stack_type || <span className="dim">—</span>}</td>
                        <td>{e.stack || <span className="dim">—</span>}</td>
                        <td className="num">{e.projected_points.toFixed(1)}</td>
                        <td className="num dim">{r ? r.simulated_points_floor.toFixed(1) : '—'}</td>
                        <td className="num dim">{r ? r.simulated_points_ceiling.toFixed(1) : '—'}</td>
                        <td className="num">{e.total_ownership_pct.toFixed(1)}%</td>
                        <td className="num">{e.duplication_risk}</td>
                        <td className="num">{r ? `${r.cash_probability_pct}%` : '—'}</td>
                        <td className="num">{r ? `${r.first_place_pct}%` : '—'}</td>
                        <td className="num">{r ? `${r.top_1pct_pct}%` : '—'}</td>
                        <td className="num">{r ? `${r.top_10pct_pct}%` : '—'}</td>
                        <td
                          className="num"
                          title={
                            r
                              ? `Mean of ${state.num_trials.toLocaleString()} simulated trials. ` +
                                `10th-90th percentile: $${r.payout_p10.toFixed(2)} - $${r.payout_p90.toFixed(2)}.`
                              : undefined
                          }
                        >
                          {r ? `$${r.expected_payout.toFixed(2)}` : '—'}
                        </td>
                        <td className="num">
                          {r ? (
                            <>
                              {/* An ROI smaller than its own standard error is
                                  indistinguishable from luck in this run's draws --
                                  show it neutrally instead of coloring it as a real
                                  win/loss signal. */}
                              <span
                                className={
                                  r.roi_se_pct != null && Math.abs(r.roi_pct) < r.roi_se_pct
                                    ? 'badge'
                                    : `badge ${r.roi_pct >= 0 ? 'ok' : 'risk'}`
                                }
                                title={
                                  r.roi_se_pct != null && Math.abs(r.roi_pct) < r.roi_se_pct
                                    ? 'Smaller than its own simulation noise -- treat as ~0'
                                    : undefined
                                }
                              >
                                {r.roi_pct >= 0 ? '+' : ''}
                                {r.roi_pct}%
                                {r.roi_se_pct != null && <span className="dim"> ±{r.roi_se_pct}</span>}
                              </span>
                              {r.adjusted_roi_pct != null && r.adjusted_roi_pct !== r.roi_pct && (
                                <div
                                  className="sub-line"
                                  title="Real roi_pct plus any ROI boosts on this lineup's players -- ranking only, not a real simulated number"
                                >
                                  {r.adjusted_roi_pct >= 0 ? '+' : ''}
                                  {r.adjusted_roi_pct.toFixed(1)}% boosted
                                </div>
                              )}
                            </>
                          ) : (
                            '—'
                          )}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}

          <div className="dim" style={{ fontSize: 12, marginBottom: 12 }}>
            {state.note}
          </div>
        </>
      )}
    </div>
  )
}
