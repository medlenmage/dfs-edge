import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { FieldSnapshot } from './FieldSnapshot'
import { localTime } from '../format'

/**
 * The contest generator: builds a whole DraftKings Classic MLB contest
 * -- lineups, and nothing else.
 *
 * Deliberately has no economics and no per-entry knobs. Cash rate,
 * payouts and ROI live in the Simulator tab, which runs on the batch
 * this builds; the entry cost and payout curve that produce those
 * numbers have nothing to do with how the lineups themselves get built,
 * so they belong there rather than here. There's no salary floor,
 * exposure cap, duplicate toggle or duplication-risk filter either:
 * every entry is built toward spending the cap because unspent salary
 * is unspent projected points, and duplicates are always allowed
 * because a real contest field genuinely contains them.
 *
 * Contest size is one control, not two. This builds a CONTEST, so how
 * many lineups get built and how big the contest is are the same
 * number -- picked from the real sizes the selected contest type
 * actually comes in.
 */
export function ContestGeneratorPanel({ date, slate, projectionSource, onSimulate }) {
  const [contestTypes, setContestTypes] = useState(null)
  const [contestType, setContestType] = useState('gpp_large')
  const [contestSize, setContestSize] = useState(10000)
  const [showSlateGames, setShowSlateGames] = useState(false)
  const [includedGames, setIncludedGames] = useState(new Set())
  const [state, setState] = useState({ status: 'idle' })
  // Bumped by the Re-roll button. At 0, identical settings reproduce the
  // identical contest (the backend derives a deterministic seed from
  // them), so the table doesn't reshuffle on every click for no reason.
  const [reroll, setReroll] = useState(0)

  // Entry Manager -- fills a real, already-uploaded DK bulk-entries
  // template with this batch's lineups. Lives here rather than in the
  // simulator because it's about exporting what was built, not about
  // how it would pay.
  const [dkUploading, setDkUploading] = useState(false)
  const [dkUploadError, setDkUploadError] = useState('')
  const [dkContests, setDkContests] = useState(null)
  const [dkContestId, setDkContestId] = useState('')
  const [emOnlyBlank, setEmOnlyBlank] = useState(true)

  useEffect(() => {
    api
      .contestTypes()
      .then((d) => setContestTypes(d.contest_types))
      .catch(() => {})
  }, [])

  const preset = contestTypes?.[contestType]
  const sizes = preset?.sizes || []

  // Every contest type comes in its own set of real sizes, so switching
  // type has to land on one this type actually offers -- its own default
  // (field_size) unless the current pick happens to be valid for it too.
  useEffect(() => {
    if (!sizes.length) return
    if (!sizes.includes(contestSize)) setContestSize(preset.field_size ?? sizes[sizes.length - 1])
  }, [contestType, contestTypes])

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

  async function run(rerollOverride = null) {
    setState({ status: 'loading' })
    try {
      const result = await api.buildContestEntries(date, contestType, contestSize, {
        projectionSource,
        reroll: rerollOverride ?? reroll,
        includedGamePks:
          slateGames.length && includedGames.size < slateGames.length ? [...includedGames] : null,
      })
      setState({ status: 'ready', ...result })
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

  const sizeLabel = (n) => (n >= 1000 && n % 1000 === 0 ? `${n / 1000}K` : n.toLocaleString())
  // A contest bigger than the build cap gets a smaller batch standing in
  // for the full field -- said out loud rather than quietly conflated.
  const sampled = state.status === 'ready' && state.num_entries_built < state.field_size

  return (
    <div className="card">
      <FieldSnapshot slate={slate} />

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
        <label
          className="dim"
          style={{ fontSize: 13 }}
          title="How big the contest is -- and therefore how many lineups get built. One number, not two: this builds a contest, so the field size and the number of entries in it are the same thing. The options are the real sizes this contest type actually comes in."
        >
          Contest size{' '}
          <select value={contestSize} onChange={(e) => setContestSize(Number(e.target.value))}>
            {sizes.map((n) => (
              <option key={n} value={n}>
                {sizeLabel(n)}
              </option>
            ))}
          </select>
        </label>
        {slateGames.length > 0 && (
          <button onClick={() => setShowSlateGames((v) => !v)}>
            {showSlateGames ? 'Hide slate games' : 'Slate games'} ({includedGames.size} of{' '}
            {slateGames.length})
          </button>
        )}
        <button className="primary" onClick={() => run()} disabled={state.status === 'loading'}>
          {state.status === 'loading' ? 'Building…' : `Build ${sizeLabel(contestSize)} contest`}
        </button>
        {state.status === 'ready' && (
          <button
            onClick={() => {
              const next = reroll + 1
              setReroll(next)
              run(next)
            }}
            title="Identical settings always reproduce the identical contest (a deterministic seed), so results are stable click to click. Re-roll draws a genuinely new one for the same settings."
          >
            Re-roll
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

      {state.status === 'idle' && (
        <p style={{ marginTop: 0, color: 'var(--text-secondary)' }}>
          Builds an entire DraftKings Classic MLB contest in one shot — every lineup individually
          strong (weighted heavily toward projected points, and toward actually spending the
          salary cap), across every stack shape a real MLB field builds, duplicates included the
          way a short slate genuinely produces them. No economics here: once it&apos;s built, send
          it to the Simulator for cash probability, payouts and ROI. Upload a DraftKings salary
          CSV and a RotoWire projections CSV for this date first.
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
          <button style={{ marginTop: 12 }} onClick={() => run()}>
            Try again
          </button>
        </>
      )}

      {state.status === 'ready' && (
        <>
          {sampled && (
            <div className="notice" style={{ marginBottom: 12 }}>
              {state.num_entries_built.toLocaleString()} lineups built for a{' '}
              {state.field_size.toLocaleString()}-entry contest — building is capped at 10,000, so
              this batch stands in for the full field. Every payout, rank and ROI the simulator
              produces still keys off the real {state.field_size.toLocaleString()}-entry size.
            </div>
          )}
          {state.num_distinct_entries < state.num_entries_built && (
            <div className="notice" style={{ marginBottom: 12 }}>
              {state.num_distinct_entries.toLocaleString()} distinct builds +{' '}
              {(state.num_entries_built - state.num_distinct_entries).toLocaleString()} duplicates
              — this slate&apos;s pool can&apos;t support{' '}
              {state.num_entries_built.toLocaleString()} unique lineups, so the contest fills out
              with duplicates the way a real field does. Duplicates split their payouts (the ×N
              badge), exactly like DK&apos;s real tie rule.
            </div>
          )}

          <div className="controls" style={{ marginBottom: 12, flexWrap: 'wrap' }}>
            <span className="badge ok">
              {state.num_entries_built.toLocaleString()} lineups built
            </span>
            <span className="badge">{state.field_size.toLocaleString()}-entry contest</span>
            <span className="badge">{state.num_distinct_entries.toLocaleString()} distinct</span>
            <span
              className="badge"
              title="Median salary actually used. Every entry is built toward spending the cap -- there's no floor rejecting cheap builds, the sampler is steered toward salary as it goes."
            >
              ${state.summary.median_salary_used.toLocaleString()} median salary
            </span>
            <span className="badge">
              ${state.summary.min_salary_used.toLocaleString()}–$
              {state.summary.max_salary_used.toLocaleString()} range
            </span>
            <span className="badge">
              {state.summary.avg_projected_points.toFixed(1)} avg proj FPTS
            </span>
            <span className="badge">
              {state.summary.avg_total_ownership_pct.toFixed(1)}% avg ownership
            </span>
            <span
              className="badge"
              title="Cumulative (log-product) ownership, averaged across the contest -- how consistently chalky a typical entry's players are TOGETHER, distinct from summed ownership%."
            >
              {state.summary.avg_duplication_risk} avg duplication risk
            </span>
          </div>

          <div className="controls" style={{ marginBottom: 14, flexWrap: 'wrap' }}>
            <button className="primary" onClick={() => onSimulate?.(state)}>
              Simulate this contest →
            </button>
            <a
              href={api.contestEntriesCsvUrl(state.batch_id)}
              title={`Download all ${state.num_entries_built.toLocaleString()} lineups as a CSV`}
            >
              <button>Download full contest (CSV)</button>
            </a>
            <span className="dim" style={{ fontSize: 12 }}>
              hands this batch to the Simulator tab — entry cost and payout curve are set there
            </span>
          </div>

          {state.stack_shapes?.length > 0 && (
            <div className="card table-wrap" style={{ marginBottom: 14 }}>
              <div className="sub-line" style={{ marginBottom: 8 }}>
                Stack shapes in this contest — every buildable MLB shape, in the proportions a real
                field builds them
              </div>
              <table>
                <thead>
                  <tr>
                    <th>Shape</th>
                    <th className="num">Lineups</th>
                    <th className="num">Share</th>
                  </tr>
                </thead>
                <tbody>
                  {state.stack_shapes.map((s) => (
                    <tr key={s.shape}>
                      <td>{s.shape}</td>
                      <td className="num">{s.count.toLocaleString()}</td>
                      <td className="num">{s.pct}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {state.exposure.length > 0 && (
            <div className="card table-wrap" style={{ marginBottom: 14 }}>
              <div className="sub-line" style={{ marginBottom: 8 }}>
                Exposure across the contest
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
                      <td className="num">{e.count.toLocaleString()}</td>
                      <td className="num">{e.pct}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {state.sample_entries.length > 0 && (
            <div className="card table-wrap" style={{ marginBottom: 14 }}>
              <div className="sub-line" style={{ marginBottom: 8 }}>
                Sample lineups — showing {state.sample_entries.length} of{' '}
                {state.num_entries_built.toLocaleString()}
              </div>
              <table>
                <thead>
                  <tr>
                    <th className="num">#</th>
                    <th
                      className="num"
                      title="How many exact copies of this lineup (same 10 players) are in the contest"
                    >
                      Dup
                    </th>
                    <th className="num">Salary</th>
                    <th title="Stack shape, e.g. 5-3 = 5 hitters from one team + 3 from another">
                      Stack
                    </th>
                    <th title="Teams in the stack, largest group first (primary, secondary, tertiary...)">
                      Teams
                    </th>
                    <th className="num">Proj FPTS</th>
                    <th className="num">Own%</th>
                    <th
                      className="num"
                      title="Cumulative (log-product) ownership -- how consistently chalky every player in this lineup is TOGETHER, distinct from Own% (a sum). Closer to 0 means the field is more likely to build this exact combination too."
                    >
                      Dup. risk
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {state.sample_entries.map((e, i) => (
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
                      <td className="num">{e.total_ownership_pct.toFixed(1)}%</td>
                      <td className="num">{e.duplication_risk}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="card" style={{ marginBottom: 14 }}>
            <div className="sub-line" style={{ marginBottom: 8 }}>
              Entry Manager — fill a real DraftKings bulk-entries template (the same &quot;bulk
              entries&quot; export/upload file DK gives you) with this contest&apos;s own lineups,
              strongest first, and download a file ready to reupload to DraftKings. Requires a real
              DraftKings salary CSV to have been loaded for this slate — DK&apos;s reupload format
              needs each player&apos;s real DK id.
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
                      Fill &amp; download
                    </button>
                  </a>
                </>
              )}
            </div>
            {dkUploadError && (
              <div className="notice error" style={{ marginTop: 8 }}>
                {dkUploadError}
              </div>
            )}
          </div>

          <div className="dim" style={{ fontSize: 12, marginBottom: 12 }}>
            {state.note}
          </div>

          <button onClick={() => run()}>Rebuild</button>
        </>
      )}
    </div>
  )
}
