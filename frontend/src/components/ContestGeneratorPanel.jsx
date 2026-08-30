import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { FieldSnapshot } from './FieldSnapshot'
import { localTime } from '../format'

/**
 * Mass multi-entry contest generator -- separate from the Lineups tab's
 * exact MILP optimizer (capped at 150, one precise best lineup at a
 * time). This builds up to 10,000 of your own entries in one shot by
 * fast randomized construction weighted toward projected points, then
 * ranks the whole batch against a simulated opponent field for
 * cash-rate/payout economics -- by default against the field's
 * projected points (fast), or via the "Simulate" toggle against a real
 * Monte Carlo outcome distribution (slower, genuine cash probabilities).
 * See the caveat rendered next to the economics numbers below.
 */
export function ContestGeneratorPanel({ date, slate, projectionSource = 'rotowire' }) {
  const [mode, setMode] = useState('generate') // 'generate' | 'dk-entries'
  const [contestTypes, setContestTypes] = useState(null)
  const [contestType, setContestType] = useState('gpp_large')
  const [numLineups, setNumLineups] = useState(500)
  // Overrides the selected contest preset's own real field_size (e.g.
  // "Large-field GPP" defaults to 10,000) -- '' means "use the
  // preset's own value." Lets a user build against a real contest of
  // whatever exact size they're actually entering, not just one of
  // the 4 preset sizes.
  const [fieldSizeOverride, setFieldSizeOverride] = useState('')
  const [maxExposure, setMaxExposure] = useState('')
  // Defaults to $47,000 -- a lineup with a lot of unspent salary is
  // almost always leaving real projected points on the table.
  const [minSalary, setMinSalary] = useState('47000')
  const [maxSalary, setMaxSalary] = useState('')
  const [allowDuplicates, setAllowDuplicates] = useState(false)
  // Cumulative (log-product, not summed) ownership cap -- a chalk
  // filter distinct from total_ownership_pct, catching a lineup where
  // every player is moderately chalky TOGETHER. Values are negative
  // (closer to 0 = looser); '' means unconstrained.
  const [maxDuplicationRisk, setMaxDuplicationRisk] = useState('')
  const [fieldSharpness, setFieldSharpness] = useState('marquee')
  // Percent-to-first override for the SIMULATED payout curve, Stokastic-
  // style -- '' means "use the contest preset's own value." Only ever
  // sent when Simulate is on; the fast deterministic path doesn't
  // support an override today.
  const [firstPlacePct, setFirstPlacePct] = useState('')
  const [simulate, setSimulate] = useState(false)
  const [selfPlay, setSelfPlay] = useState(false)
  const [atbatEngine, setAtbatEngine] = useState(false)
  const [showSlateGames, setShowSlateGames] = useState(false)
  const [includedGames, setIncludedGames] = useState(new Set())
  const [state, setState] = useState({ status: 'idle' })

  // Portfolio shaping (post-hoc, on an already-simulated batch's real
  // results -- no rebuild). `originalReady` is the untouched full
  // batch a "Reshape" always starts back from, so shaping twice in a
  // row (trying a different cap, say) never compounds on top of an
  // already-trimmed set.
  const [originalReady, setOriginalReady] = useState(null)
  const [roiBoosts, setRoiBoosts] = useState({})
  const [exposureCaps, setExposureCaps] = useState({})
  const [targetCount, setTargetCount] = useState('')
  const [globalMaxExposure, setGlobalMaxExposure] = useState('')
  const [reshaping, setReshaping] = useState(false)
  const [swapping, setSwapping] = useState(false)
  // Bumped by the Re-roll button. At 0, identical settings reproduce the
  // identical batch (the backend derives a deterministic seed from them),
  // so the table doesn't reshuffle on every click for no reason.
  const [reroll, setReroll] = useState(0)
  const [swapMode, setSwapMode] = useState('repair')
  const [swapResult, setSwapResult] = useState(null)

  // Filters -- post-hoc, surgical include/exclude by stack team, a
  // specific player combo, or a named stack shape. Runs BEFORE the
  // boost/cap ranking above, narrowing the candidate pool first.
  const [requirePlayers, setRequirePlayers] = useState(new Set())
  const [excludePlayers, setExcludePlayers] = useState(new Set())
  const [requireTeams, setRequireTeams] = useState(new Set())
  const [excludeTeams, setExcludeTeams] = useState(new Set())
  const [stackTypeFilter, setStackTypeFilter] = useState(new Set())

  const [dkUploading, setDkUploading] = useState(false)
  const [dkUploadError, setDkUploadError] = useState('')
  const [dkContests, setDkContests] = useState(null)
  const [dkContestId, setDkContestId] = useState('')
  const [dkFieldSize, setDkFieldSize] = useState(1000)
  const [dkPrizePool, setDkPrizePool] = useState(1000)
  const [dkFirstPlacePct, setDkFirstPlacePct] = useState(20)

  // Entry Manager -- fills an already-uploaded real DK bulk-entries
  // template with lineups from an already-built batch. Reuses the same
  // dkContests/dkContestId state the "My DK entries" mode's upload
  // already populates (one upload, usable from either mode).
  const [emOnlyBlank, setEmOnlyBlank] = useState(true)

  function switchMode(m) {
    setMode(m)
    setState({ status: 'idle' })
  }

  useEffect(() => {
    api
      .contestTypes()
      .then((d) => setContestTypes(d.contest_types))
      .catch(() => {})
  }, [])

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

  const preset = contestTypes?.[contestType]
  const effectiveFieldSize = fieldSizeOverride.trim() ? Number(fieldSizeOverride) : preset?.field_size
  const overRequested = effectiveFieldSize != null && numLineups > effectiveFieldSize

  // Filters' own option lists -- derived from the untouched original
  // batch (not whatever's currently reshaped/kept) so a filter option
  // never disappears just because an earlier filter already trimmed
  // it out of view.
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

  // Shared by run() and runDkEntries(): a fresh build/simulate is always
  // the new "original" a Reshape starts back from, so any in-progress
  // boosts/caps from a previous batch don't silently carry over.
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
  }

  function toggleSetMember(setter, value) {
    setter((prev) => {
      const next = new Set(prev)
      next.has(value) ? next.delete(value) : next.add(value)
      return next
    })
  }

  async function run(rerollOverride = null) {
    setState({ status: 'loading' })
    try {
      const opts = {
        reroll: rerollOverride ?? reroll,
        projectionSource,
        fieldSize: fieldSizeOverride.trim() ? Number(fieldSizeOverride) : null,
        maxExposurePct: maxExposure.trim() ? Number(maxExposure) : null,
        minSalary: minSalary.trim() ? Number(minSalary) : 0,
        maxSalary: maxSalary.trim() ? Number(maxSalary) : 50000,
        allowDuplicates,
        maxDuplicationRisk: maxDuplicationRisk.trim() ? Number(maxDuplicationRisk) : null,
        fieldSharpness,
        includedGamePks:
          slateGames.length && includedGames.size < slateGames.length ? [...includedGames] : null,
        ...(simulate
          ? {
              selfPlay,
              engine: atbatEngine ? 'atbat' : 'bootstrap',
              firstPlacePct: firstPlacePct === '' ? null : Number(firstPlacePct),
            }
          : {}),
      }
      const result = simulate
        ? await api.buildContestEntriesSimulated(date, contestType, numLineups, opts)
        : await api.buildContestEntries(date, contestType, numLineups, opts)
      applyReady({ status: 'ready', simulated: simulate, mode: 'generate', ...result })
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
        includedGamePks:
          slateGames.length && includedGames.size < slateGames.length ? [...includedGames] : null,
        engine: atbatEngine ? 'atbat' : 'bootstrap',
        fieldSharpness,
      })
      applyReady({ status: 'ready', simulated: true, mode: 'dk-entries', ...result })
    } catch (err) {
      setState({ status: 'error', message: err.message })
    }
  }

  // Recomputes the same aggregate shape build_contest_entries_simulated's
  // own summary uses, from whatever subset of results survived a
  // reshape -- entry_fee comes from the untouched original batch, since
  // reshaping never changes what contest this is.
  function summarizeResults(results, entryFee) {
    const n = results.length
    const avg = (key) => results.reduce((sum, r) => sum + r[key], 0) / n
    const totalExpectedPayout = results.reduce((sum, r) => sum + r.expected_payout, 0)
    const totalEntryCost = n * entryFee
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
        includedGamePks: includedGames.size ? [...includedGames] : null,
      })
      setState((prev) => ({
        ...prev,
        batch_id: result.batch_id,
        exposure: result.exposure,
        sample_entries: result.sample_entries,
        results: result.results,
        // The server computes this over the whole batch (only 200
        // entries ship as a preview) and in whichever of the two real
        // summary shapes matches this batch -- keep the previous one if
        // nothing moved and there's nothing new to describe.
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

  return (
    <div className="card">
      <div className="controls" style={{ marginBottom: 14 }}>
        <button className={mode === 'generate' ? 'primary' : ''} onClick={() => switchMode('generate')}>
          Generate entries
        </button>
        <button className={mode === 'dk-entries' ? 'primary' : ''} onClick={() => switchMode('dk-entries')}>
          My DK entries
        </button>
        {slateGames.length > 0 && (
          <button onClick={() => setShowSlateGames((v) => !v)}>
            {showSlateGames ? 'Hide slate games' : 'Slate games'} ({includedGames.size} of{' '}
            {slateGames.length})
          </button>
        )}
      </div>

      <FieldSnapshot slate={slate} />

      {mode === 'generate' && (
      <div className="controls" style={{ marginBottom: 14, flexWrap: 'wrap' }}>
        <label className="dim" style={{ fontSize: 13 }}>
          Contest{' '}
          <select value={contestType} onChange={(e) => setContestType(e.target.value)}>
            {contestTypes &&
              Object.entries(contestTypes).map(([key, c]) => (
                <option key={key} value={key}>
                  {c.label}
                </option>
              ))}
          </select>
        </label>
        <label className="dim" style={{ fontSize: 13 }}>
          Entries to build{' '}
          <input
            type="number"
            min="1"
            max="10000"
            value={numLineups}
            onChange={(e) => setNumLineups(Math.max(1, Math.min(10000, Number(e.target.value) || 1)))}
            style={{ width: 80 }}
          />
        </label>
        <label
          className="dim"
          style={{ fontSize: 13 }}
          title="Overrides the selected contest's own real field size (e.g. Large-field GPP defaults to 10,000) -- build against a real contest of whatever exact size you're actually entering, not just one of the 4 preset sizes. Leave blank to use the preset's own value."
        >
          Field size{' '}
          <input
            type="number"
            min="1"
            max="200000"
            placeholder={preset ? String(preset.field_size) : '—'}
            value={fieldSizeOverride}
            onChange={(e) => setFieldSizeOverride(e.target.value)}
            style={{ width: 90 }}
          />
        </label>
        <label className="dim" style={{ fontSize: 13 }}>
          Max exposure{' '}
          <input
            type="number"
            min="1"
            max="100"
            placeholder="none"
            value={maxExposure}
            onChange={(e) => setMaxExposure(e.target.value)}
            style={{ width: 55 }}
          />
          %
        </label>
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
          title="How sharp the simulated opponent field is. Low: softer/chalkier, ownership spread out more evenly. Marquee (default): a realistic large-field GPP. High: sharp bettors converging tightly on the best pure points-per-dollar plays."
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
          title="Allow exact duplicate entries in the batch -- a real GPP move (entering a signature build multiple times). Duplicates report how many identical copies are in the batch, and their cash probability/payout/ROI are averaged across the tied group, matching how DK actually splits a payout between literally identical lineups."
        >
          <input
            type="checkbox"
            checked={allowDuplicates}
            onChange={(e) => setAllowDuplicates(e.target.checked)}
          />
          Allow duplicates
        </label>
        <label
          className="dim"
          style={{ fontSize: 13 }}
          title="Reject any entry whose cumulative (log-product) ownership exceeds this -- catches a lineup where every player is moderately chalky TOGETHER even when its summed Own% looks unremarkable, the real 'the field will build this exact lineup too' risk. Values are negative; closer to 0 is looser. Leave blank for unconstrained."
        >
          Max duplication risk{' '}
          <input
            type="number"
            step="0.5"
            placeholder="none"
            value={maxDuplicationRisk}
            onChange={(e) => setMaxDuplicationRisk(e.target.value)}
            style={{ width: 70 }}
          />
        </label>
        <label
          className="dim"
          style={{ fontSize: 13, display: 'flex', alignItems: 'center', gap: 6 }}
          title="Rank entries against real Monte Carlo simulated outcomes (each player's own historical game log, resampled) instead of projected points -- slower, but gives a genuine cash probability and payout range."
        >
          <input type="checkbox" checked={simulate} onChange={(e) => setSimulate(e.target.checked)} />
          Simulate
        </label>
        {simulate && (
          <label
            className="dim"
            style={{ fontSize: 13 }}
            title="Override the contest's own percent-to-first (share of the prize pool 1st place wins) for this simulated run. A lower value flattens the payout curve -- more spread across the paid ranks, less concentrated at 1st -- which changes every entry's simulated ROI. Defaults to the contest preset's own value."
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
        )}
        {simulate && (
          <label
            className="dim"
            style={{ fontSize: 13, display: 'flex', alignItems: 'center', gap: 6 }}
            title="Off (default): rank this batch against a separately-sampled realistic public field -- your entries are one voice in a field mostly made of other builds. On: rank this batch against ITSELF -- every lineup you generated competes against every other one in the same simulated trial, so lineups sharing correlated players/stacks naturally cluster together when those players run hot or cold."
          >
            <input type="checkbox" checked={selfPlay} onChange={(e) => setSelfPlay(e.target.checked)} />
            Self-play (batch vs. itself)
          </label>
        )}
        {simulate && (
          <label
            className="dim"
            style={{ fontSize: 13, display: 'flex', alignItems: 'center', gap: 6 }}
            title="Off (default): sample each player's own historical DK-point outcome pool, with a team-multiplier for correlation. On: run genuine at-bat-level (plate-appearance by plate-appearance) simulated games for the whole slate instead -- correlation is a natural consequence of shared simulated game state. Requires a CONFIRMED lineup on both sides and a probable pitcher for every game on the slate, or the build fails with a clear error."
          >
            <input
              type="checkbox"
              checked={atbatEngine}
              onChange={(e) => setAtbatEngine(e.target.checked)}
            />
            At-bat-level engine (beta)
          </label>
        )}
        <button className="primary" onClick={() => run()} disabled={state.status === 'loading'}>
          {state.status === 'loading'
            ? simulate
              ? 'Simulating…'
              : 'Building…'
            : `${simulate ? 'Simulate' : 'Build'} ${numLineups.toLocaleString()} entries`}
        </button>
        {state.status === 'ready' && state.mode === 'generate' && (
          <button
            onClick={() => {
              const next = reroll + 1
              setReroll(next)
              run(next)
            }}
            disabled={state.status === 'loading'}
            title="Identical settings always reproduce the identical batch (a deterministic seed), so results are stable click to click. Re-roll draws a genuinely new random batch, field, and simulation for the same settings."
          >
            Re-roll
          </button>
        )}
      </div>
      )}

      {mode === 'dk-entries' && (
      <div className="controls" style={{ marginBottom: 14, flexWrap: 'wrap' }}>
        <input
          type="file"
          accept=".csv"
          id="dk-entries-file"
          style={{ display: 'none' }}
          onChange={(e) => e.target.files[0] && uploadDkFile(e.target.files[0])}
        />
        <button onClick={() => document.getElementById('dk-entries-file').click()} disabled={dkUploading}>
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
                    {c.contest_name} (${c.entry_fee?.toFixed(2)} entry, {c.num_entries} of your own entries reserved)
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
                onChange={(e) => setDkFirstPlacePct(Math.max(0, Math.min(100, Number(e.target.value) || 0)))}
                style={{ width: 70 }}
              />
            </label>
            <label
              className="dim"
              style={{ fontSize: 13, display: 'flex', alignItems: 'center', gap: 6 }}
              title="Off (default): sample each player's own historical DK-point outcome pool, with a team-multiplier for correlation. On: run genuine at-bat-level simulated games for the whole slate instead -- requires a CONFIRMED lineup on both sides and a probable pitcher for every game on the slate, or the build fails with a clear error."
            >
              <input
                type="checkbox"
                checked={atbatEngine}
                onChange={(e) => setAtbatEngine(e.target.checked)}
              />
              At-bat-level engine (beta)
            </label>
            <label
              className="dim"
              style={{ fontSize: 13 }}
              title="How sharp the simulated field is. Low: softer/chalkier, ownership spread out more evenly. Marquee (default): a realistic large-field GPP. High: sharp bettors converging tightly on the best pure points-per-dollar plays."
            >
              Field sharpness{' '}
              <select value={fieldSharpness} onChange={(e) => setFieldSharpness(e.target.value)}>
                <option value="low">Low</option>
                <option value="marquee">Marquee</option>
                <option value="high">High</option>
              </select>
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

      {mode === 'generate' && preset && overRequested && (
        <div className="notice" style={{ marginBottom: 14 }}>
          This contest's field only holds {effectiveFieldSize.toLocaleString()} entries — your own
          entries are part of that field, not additional to it. Lower "Entries to build" to at most{' '}
          {effectiveFieldSize.toLocaleString()}, raise "Field size" above, or pick a bigger contest.
        </div>
      )}

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

      {state.status === 'idle' && mode === 'generate' && (
        <p style={{ marginTop: 0, color: 'var(--text-secondary)' }}>
          Builds up to 10,000 of your own DraftKings Classic MLB entries in one shot -- each
          individually strong (weighted heavily toward projected points) but genuinely distinct
          from every other entry in the batch. Separate from the Lineups tab's exact optimizer,
          which is built for a small number of provably-best lineups, not mass multi-entry. Upload
          a DraftKings salary CSV and a RotoWire projections CSV for this date first.
        </p>
      )}

      {state.status === 'idle' && mode === 'dk-entries' && (
        <p style={{ marginTop: 0, color: 'var(--text-secondary)' }}>
          Upload the entries CSV DraftKings gives you (the same "bulk entries" export/upload file
          from their site) so this can read the entry fee for a real contest you've reserved
          entries into -- that's the file's only real job here. This then builds an
          ownership-weighted sample standing in for that contest's <em>entire field</em> (the same
          construction "Generate entries" mode uses to model what real public rosters look like)
          and simulates it as one self-contained population -- every lineup ranked against every
          other lineup, not against a separate "your entries" batch. Browse the results below to
          see which archetypes actually perform, then pick whichever ones you want to submit
          yourself on DraftKings. Total contest entries, prize pool, and 1st-place % are
          hand-entered since a bulk entries export has no payout-table data or true field size at
          all. A file can span more than one contest -- pick which one once it's uploaded.
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
          <button style={{ marginTop: 12 }} onClick={mode === 'dk-entries' ? runDkEntries : run}>
            Try again
          </button>
        </>
      )}

      {state.status === 'ready' && (
        <>
          {!state.reshaped && state.num_entries_built < state.num_entries_requested && (
            <div className="notice" style={{ marginBottom: 12 }}>
              Only built {state.num_entries_built.toLocaleString()} of{' '}
              {state.num_entries_requested.toLocaleString()} requested — the salary range or
              exposure cap is the binding constraint (running out of distinct builds no longer
              stops a batch; it fills with duplicates like a real contest would).
            </div>
          )}
          {!state.reshaped &&
            state.num_distinct_entries != null &&
            state.num_distinct_entries < state.num_entries_built && (
              <div className="notice" style={{ marginBottom: 12 }}>
                {state.num_distinct_entries.toLocaleString()} distinct builds +{' '}
                {(state.num_entries_built - state.num_distinct_entries).toLocaleString()}{' '}
                duplicates — this slate&apos;s pool can&apos;t support{' '}
                {state.num_entries_built.toLocaleString()} unique lineups, so the batch fills out
                with duplicates the way a real contest field does. Duplicate entries split their
                payouts (the ×N badge), exactly like DK&apos;s real tie rule.
              </div>
            )}

          <div className="controls" style={{ marginBottom: 12, flexWrap: 'wrap' }}>
            <span className="badge ok">{state.num_entries_built.toLocaleString()} entries built</span>
            {state.mode === 'dk-entries' && (
              <span
                className="badge"
                title="How many field lineups were actually simulated, standing in for the real contest's full field_size above"
              >
                {state.sample_size.toLocaleString()}-lineup sample
              </span>
            )}
            <span className="badge">{state.field_size.toLocaleString()}-entry contest</span>
            {state.field_sharpness && state.field_sharpness !== 'marquee' && (
              <span
                className="badge"
                title="How sharp the simulated field was built to be for this batch"
              >
                {state.field_sharpness} sharpness
              </span>
            )}
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
            {state.simulated && (
              <span className="badge">{state.num_trials.toLocaleString()} sim trials</span>
            )}
            {state.simulated && state.engine === 'atbat' && (
              <span
                className="badge"
                title="Every trial is a genuine plate-appearance-by-plate-appearance simulated game for the whole slate, not a resampled historical outcome pool."
              >
                at-bat engine
              </span>
            )}
            {state.simulated && state.mode === 'generate' && (
              <span
                className="badge"
                title={
                  state.self_play
                    ? 'Every lineup in this batch ranked against every other lineup in the batch, in the same simulated trial -- no separate field sampled.'
                    : 'This batch ranked against a separately-sampled realistic public field.'
                }
              >
                {state.self_play ? 'self-play' : 'vs. public field'}
              </span>
            )}
            <a
              href={api.contestEntriesCsvUrl(state.batch_id)}
              title={`Download all ${state.num_entries_built.toLocaleString()} entries as a CSV -- for handing off to an external simulator`}
            >
              <button>Download full batch (CSV)</button>
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
              {state.simulated ? (
                <>
                  Simulated economics — {state.num_trials.toLocaleString()} Monte Carlo trials of
                  each player's own real historical outcomes, with team correlation for hitters.
                  {state.self_play
                    ? ' Every lineup in this batch is ranked against every other lineup in the same simulated trial -- no separate field, so lineups sharing correlated players/stacks naturally cluster together when those players run hot or cold.'
                    : ' Cash probability and payout are genuine simulated results against a separately-sampled realistic public field.'}
                </>
              ) : (
                <>
                  Estimated economics — <strong>not a prediction.</strong> This is your batch's
                  projected points ranked against a projected-points model of the field, not
                  simulated real-world results. Toggle "Simulate" above for real Monte Carlo cash
                  probabilities.
                </>
              )}
            </div>
            {state.simulated ? (
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
                <span className="badge">${state.summary.total_entry_cost.toLocaleString()} cost</span>
                <span className="badge">
                  ${state.summary.total_expected_payout.toLocaleString()} expected payout
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
            ) : (
              <div className="controls" style={{ flexWrap: 'wrap' }}>
                <span className="badge">
                  {state.summary.cashing_count.toLocaleString()} cashing ({state.summary.cashing_pct}%)
                </span>
                <span className="badge">${state.summary.total_entry_cost.toLocaleString()} cost</span>
                <span className="badge">
                  ${state.summary.total_estimated_payout.toLocaleString()} est. payout
                </span>
                <span className={`badge ${state.summary.estimated_net_profit >= 0 ? 'ok' : 'risk'}`}>
                  {state.summary.estimated_net_profit >= 0 ? '+' : ''}$
                  {state.summary.estimated_net_profit.toLocaleString()} est. net
                </span>
                <span className="badge">{state.summary.avg_projected_points.toFixed(1)} avg proj FPTS</span>
                <span className="badge">
                  {state.summary.min_projected_points.toFixed(1)}–{state.summary.max_projected_points.toFixed(1)} range
                </span>
                <span className="badge">{state.summary.avg_total_ownership_pct.toFixed(1)}% avg ownership</span>
                <span
                  className="badge"
                  title="Cumulative (log-product) ownership, averaged across the batch -- how consistently chalky a typical entry's players are TOGETHER, distinct from summed ownership%."
                >
                  {state.summary.avg_duplication_risk} avg duplication risk
                </span>
              </div>
            )}

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
                    field they're compared against. In self-play mode (and
                    always, in "My DK entries" mode -- it's simulating the
                    whole field's own archetypes, never "your" submitted
                    picks) the entries ARE the field, ranked against
                    itself, so this number is tautologically ~0 -- showing
                    it there reads as "my results are meaningless" rather
                    than "not applicable here." */}
                {!state.self_play && state.mode !== 'dk-entries' && (
                  <span
                    className={`badge ${
                      state.summary.avg_roi_pct - state.field_baseline.avg_roi_pct >= 0 ? 'ok' : 'risk'
                    }`}
                    title="Your batch's own avg ROI minus the field baseline's -- the real, field-beating edge your entries show, isolated from what the field's own rake-driven baseline already accounts for."
                  >
                    your edge: {state.summary.avg_roi_pct - state.field_baseline.avg_roi_pct >= 0 ? '+' : ''}
                    {(state.summary.avg_roi_pct - state.field_baseline.avg_roi_pct).toFixed(1)} pts ROI
                  </span>
                )}
              </div>
            )}
          </div>

          {state.batch_id && (
            <div className="card" style={{ marginBottom: 14 }}>
              <div className="sub-line" style={{ marginBottom: 8 }}>
                Entry Manager — fill a real DraftKings bulk-entries template (the same "bulk
                entries" export/upload file DK gives you) with this batch's own lineups, strongest
                first, and download a file ready to literally reupload to DraftKings. Requires a
                real DraftKings salary CSV to have been loaded for this slate (not just RotoWire
                projections) -- DK's own reupload format needs each player's real DK id.
              </div>
              <div className="controls" style={{ flexWrap: 'wrap' }}>
                <input
                  type="file"
                  accept=".csv"
                  id="dk-entries-file-manager"
                  style={{ display: 'none' }}
                  onChange={(e) => e.target.files[0] && uploadDkFile(e.target.files[0])}
                />
                <button
                  onClick={() => document.getElementById('dk-entries-file-manager').click()}
                  disabled={dkUploading}
                >
                  {dkUploading
                    ? 'Uploading…'
                    : dkContests
                      ? 'Re-upload DK entries CSV'
                      : 'Upload DK entries CSV'}
                </button>
                {dkContests && dkContests.length > 0 && (
                  <>
                    <label className="dim" style={{ fontSize: 13 }}>
                      Contest{' '}
                      <select value={dkContestId} onChange={(e) => setDkContestId(e.target.value)}>
                        {dkContests.map((c) => (
                          <option key={c.contest_id} value={c.contest_id}>
                            {c.contest_name} ({c.num_entries - c.num_filled} of {c.num_entries} blank)
                          </option>
                        ))}
                      </select>
                    </label>
                    <label
                      className="dim"
                      style={{ fontSize: 13, display: 'flex', alignItems: 'center', gap: 6 }}
                      title="On (default): only fill entry rows with no picks yet, never overwriting one you already filled. Off: overwrite every entry row for this contest, blank or not."
                    >
                      <input
                        type="checkbox"
                        checked={emOnlyBlank}
                        onChange={(e) => setEmOnlyBlank(e.target.checked)}
                      />
                      Only fill blank entries
                    </label>
                    <a
                      href={api.fillDkEntriesUrl(date, dkContestId, state.batch_id, emOnlyBlank)}
                      title="Fill this contest's entries with lineups from this batch, strongest first, and download the completed CSV"
                    >
                      <button className="primary" disabled={!dkContestId}>
                        Fill & download
                      </button>
                    </a>
                  </>
                )}
              </div>
            </div>
          )}

          {state.simulated && (
            <div className="card" style={{ marginBottom: 14 }}>
              <div className="sub-line" style={{ marginBottom: 8 }}>
                Shape your portfolio — no rebuild, this re-ranks and re-filters the real simulated
                results above. Filters narrow the pool first; then an ROI boost or exposure cap
                (set per player in the table below) ranks and trims what's left. Then Reshape.
              </div>
              {(availableTeams.length > 0 || availableStackTypes.length > 0) && (
                <div className="controls" style={{ flexWrap: 'wrap', marginBottom: 8 }}>
                  {availableTeams.length > 0 && (
                    <>
                      <label className="dim" style={{ fontSize: 13 }} title="Keep only entries rostering a player from EVERY team selected">
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
                      <label className="dim" style={{ fontSize: 13 }} title="Drop any entry rostering a player from ANY team selected">
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
                    <label className="dim" style={{ fontSize: 13 }} title="Keep only entries whose own stack shape (e.g. 5-3, 4-4) is one of these">
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
                    <span className="badge" title="Set per-player in the Req/Excl columns of the exposure table below">
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
          )}

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
                    {state.simulated && (
                      <>
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
                      </>
                    )}
                  </tr>
                </thead>
                <tbody>
                  {state.exposure.map((e) => (
                    <tr key={e.id}>
                      <td>{e.name}</td>
                      <td className="dim">{e.team}</td>
                      <td className="num">{e.count}</td>
                      <td className="num">{e.pct}%</td>
                      {state.simulated && (
                        <>
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
                        </>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {state.sample_entries.length > 0 && (
            <div className="card table-wrap" style={{ marginBottom: 8 }}>
              <div className="sub-line" style={{ marginBottom: 8 }}>
                Sample entries — showing {state.sample_entries.length} of{' '}
                {state.num_entries_built.toLocaleString()}
              </div>
              <table>
                <thead>
                  <tr>
                    <th className="num">#</th>
                    <th
                      className="num"
                      title="How many exact copies of this lineup (same 10 players) are in the batch -- only ever more than 1 when Allow duplicates is on"
                    >
                      Dup
                    </th>
                    <th className="num">Salary</th>
                    <th title="Stack shape, e.g. 5-3 = 5 hitters from one team + 3 from another">Stack</th>
                    <th title="Teams in the stack, largest group first (primary, secondary, tertiary...)">Teams</th>
                    <th className="num">Proj FPTS</th>
                    {state.simulated && (
                      <>
                        <th className="num" title="5th percentile -- this lineup scored below this in only 1 of 20 simulated trials. Not the true minimum, which is a single freak outcome even under a well-calibrated model.">
                          Floor
                        </th>
                        <th className="num" title="95th percentile -- this lineup scored above this in only 1 of 20 simulated trials. Not the true maximum, which is a single freak outcome even under a well-calibrated model.">
                          Ceiling
                        </th>
                      </>
                    )}
                    <th className="num">Own%</th>
                    <th
                      className="num"
                      title="Cumulative (log-product) ownership -- how consistently chalky every player in this lineup is TOGETHER, distinct from Own% (a sum). Closer to 0 means the field is more likely to build this exact combination too."
                    >
                      Dup. risk
                    </th>
                    {state.simulated ? (
                      <>
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
                        <th className="num">Exp. payout</th>
                        <th
                          className="num"
                          title="(expected payout - entry fee) / entry fee, ± its Monte Carlo standard error. Top-heavy payouts make per-lineup ROI dominated by rare first-place hits, so a value smaller than its own ± is noise (greyed out) -- that's also why rows are ranked by Top 1% rather than ROI."
                        >
                          ROI % (±SE)
                        </th>
                      </>
                    ) : (
                      <>
                        <th className="num">Rank</th>
                        <th>Cashing?</th>
                      </>
                    )}
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
                        {state.simulated && (
                          <>
                            <td className="num dim">{r ? r.simulated_points_floor.toFixed(1) : '—'}</td>
                            <td className="num dim">{r ? r.simulated_points_ceiling.toFixed(1) : '—'}</td>
                          </>
                        )}
                        <td className="num">{e.total_ownership_pct.toFixed(1)}%</td>
                        <td className="num">{e.duplication_risk}</td>
                        {state.simulated ? (
                          <>
                            <td className="num">{r ? `${r.cash_probability_pct}%` : '—'}</td>
                            <td className="num">{r ? `${r.first_place_pct}%` : '—'}</td>
                            <td className="num">{r ? `${r.top_1pct_pct}%` : '—'}</td>
                            <td className="num">{r ? `${r.top_10pct_pct}%` : '—'}</td>
                            <td className="num">{r ? `$${r.expected_payout.toFixed(2)}` : '—'}</td>
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
                                    {r.roi_se_pct != null && (
                                      <span className="dim"> ±{r.roi_se_pct}</span>
                                    )}
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
                          </>
                        ) : (
                          <>
                            <td className="num">{r ? r.estimated_rank.toLocaleString() : '—'}</td>
                            <td>
                              {r ? (
                                r.in_the_money ? (
                                  <span className="badge ok">cashes</span>
                                ) : (
                                  <span className="badge risk">misses</span>
                                )
                              ) : (
                                '—'
                              )}
                            </td>
                          </>
                        )}
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

          <button onClick={state.mode === 'dk-entries' ? runDkEntries : run}>Rebuild</button>
        </>
      )}
    </div>
  )
}
