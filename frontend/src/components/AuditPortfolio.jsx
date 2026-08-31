import { useRef, useState } from 'react'
import { api } from '../api'

/**
 * What the build audit decided, made usable.
 *
 * The audit's own text is insightful but you can't act on prose: the
 * whole point of it is the SELECTED PORTFOLIO -- the specific entries
 * to play, chosen so the surviving set obeys the portfolio rules. This
 * renders that selection as a table, downloads it as a CSV, and takes
 * a real DraftKings bulk-entries template so the selection can be
 * written straight into it and reuploaded to DK.
 *
 * The last part needs no new backend: the audit caches its selection as
 * a normal batch (`keep_batch_id`), and the existing entry filler takes
 * any batch id.
 */

function slotName(entry, i) {
  const p = (entry.players || [])[i]
  return p ? p.name : '—'
}

// Contest entries carry no position field, so slots are labelled by
// index against DK's roster order -- the same convention the server-side
// exporter uses.
const SLOTS = ['P', 'P', 'C', '1B', '2B', '3B', 'SS', 'OF', 'OF', 'OF']

function Compliance({ selection }) {
  const c = selection.compliance
  const ok = (pass) => (pass ? 'badge ok' : 'badge risk')
  return (
    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 10 }}>
      <span className={ok(c.distinct_pitchers <= c.distinct_pitchers_target)}>
        {c.distinct_pitchers} arms (target ≤{c.distinct_pitchers_target})
      </span>
      <span className={ok(c.distinct_stacks <= c.distinct_stacks_target)}>
        {c.distinct_stacks} stacks (target ≤{c.distinct_stacks_target})
      </span>
      <span className={ok(c.top_stack_share_pct >= c.top_stack_share_target_pct)}>
        top stack {c.top_stack_share_pct}% (target {c.top_stack_share_target_pct}%+)
      </span>
      <span className={ok(c.leverage_pitchers.length === 0)}>
        {c.leverage_pitchers.length
          ? `leverage arm: ${c.leverage_pitchers.join(', ')}`
          : 'no leverage arms'}
      </span>
      <span className="badge">{c.entries_with_4plus_pct}% with a 4+ stack</span>
    </div>
  )
}

function DkFill({ date, keepBatchId, count }) {
  const fileRef = useRef(null)
  const [contests, setContests] = useState(null)
  const [contestId, setContestId] = useState('')
  const [onlyBlank, setOnlyBlank] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function upload(e) {
    const file = e.target.files?.[0]
    if (!file) return
    setBusy(true)
    setError(null)
    try {
      const res = await api.uploadDkEntries(date, file)
      setContests(res.contests || [])
      setContestId(res.contests?.[0]?.contest_id || '')
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
      e.target.value = ''
    }
  }

  const chosen = (contests || []).find((c) => String(c.contest_id) === String(contestId))

  return (
    <div style={{ marginTop: 16, paddingTop: 14, borderTop: '1px solid var(--gridline)' }}>
      <div style={{ fontWeight: 600, fontSize: 13.5 }}>Fill a DraftKings entries template</div>
      <div className="sub-line" style={{ marginBottom: 10 }}>
        Upload the bulk-entries CSV DraftKings gives you, and the {count} selected lineups get
        written into its blank rows in the order above. The file that comes back is the one you
        reupload to DraftKings — no copy/paste. Needs a real DK salary file loaded for the slate,
        since DK's reupload format wants each player's numeric DK id.
      </div>

      <div className="controls">
        <button onClick={() => fileRef.current?.click()} disabled={busy}>
          {busy ? 'Reading…' : contests ? 'Upload a different template' : 'Upload DK entries CSV'}
        </button>
        <input
          ref={fileRef}
          type="file"
          accept=".csv"
          onChange={upload}
          style={{ display: 'none' }}
        />
        {contests?.length > 0 && (
          <>
            <select value={contestId} onChange={(e) => setContestId(e.target.value)}>
              {contests.map((c) => (
                <option key={c.contest_id} value={c.contest_id}>
                  {c.contest_name} — {c.num_entries} entries
                  {c.num_filled != null ? ` (${c.num_entries - c.num_filled} blank)` : ''}
                </option>
              ))}
            </select>
            <label style={{ fontSize: 13, display: 'flex', alignItems: 'center', gap: 6 }}>
              <input
                type="checkbox"
                checked={onlyBlank}
                onChange={(e) => setOnlyBlank(e.target.checked)}
              />
              Only fill blank rows
            </label>
            <a
              className="btn primary"
              href={api.fillDkEntriesUrl(date, contestId, keepBatchId, onlyBlank)}
            >
              Fill &amp; download
            </a>
          </>
        )}
      </div>

      {contests?.length === 0 && (
        <div className="notice" style={{ marginTop: 10 }}>
          That file parsed, but no contests were found in it.
        </div>
      )}
      {chosen && chosen.num_entries < count && (
        <div className="notice" style={{ marginTop: 10 }}>
          This contest has {chosen.num_entries} entry rows but the portfolio has {count} lineups — the
          extras won&rsquo;t be written. Re-run the audit with &ldquo;entries you&rsquo;ll play&rdquo;
          set to {chosen.num_entries}.
        </div>
      )}
      {error && <div className="notice error" style={{ marginTop: 10 }}>{error}</div>}
    </div>
  )
}

export function AuditPortfolio({ date, audit, entries, keepBatchId, targetCount }) {
  const [showCut, setShowCut] = useState(false)
  const selection = audit?.selection
  if (!selection?.indices?.length) return null

  const keeps = (audit.verdicts || [])
    .filter((v) => v.verdict === 'keep')
    .sort((a, b) => (a.keep_rank ?? 0) - (b.keep_rank ?? 0))
  const cuts = (audit.verdicts || []).filter((v) => v.verdict === 'cut')
  // `entries` is the audit's own keep_entries: the selected lineups
  // already in keep order, NOT the source batch. So a keep's row is
  // found by its rank, not by its index into the batch it came from --
  // indexing by the latter would silently show the wrong players.
  const keepEntry = (rank) => (entries || [])[rank]

  // Pin the CSV to the batch that was actually audited. Without it the
  // export would re-resolve "the latest build for the date", which can
  // have moved on since -- and would then be a different set of lineups
  // than the table above.
  const csvOpts = { targetCount: targetCount || null, batchId: audit.batch_id || null }

  return (
    <div style={{ marginTop: 14 }}>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'baseline',
          gap: 12,
          flexWrap: 'wrap',
          marginBottom: 8,
        }}
      >
        <div>
          <div style={{ fontWeight: 600 }}>
            Portfolio to enter — {selection.indices.length} lineups
          </div>
          <div className="sub-line">
            {selection.passes_rules
              ? 'Meets every portfolio rule.'
              : 'The best this batch can supply — it still misses a rule below.'}
          </div>
        </div>
        <div className="controls">
          <a className="btn primary" href={api.buildAuditCsvUrl(date, { ...csvOpts, include: 'keep' })}>
            Download portfolio CSV
          </a>
          <a className="btn" href={api.buildAuditCsvUrl(date, { ...csvOpts, include: 'all' })}>
            Download all + cut reasons
          </a>
        </div>
      </div>

      <Compliance selection={selection} />

      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 10, fontSize: 13 }}>
        <span className="dim">Pitcher core:</span>
        {selection.pitcher_core.map((p) => (
          <span className="badge" key={p.name}>
            {p.name} ×{p.entries}
          </span>
        ))}
      </div>
      {selection.stack_allocation.length > 0 && (
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 10, fontSize: 13 }}>
          <span className="dim">Stacks:</span>
          {selection.stack_allocation
            .filter((a) => a.selected)
            .map((a) => (
              <span className="badge" key={a.team}>
                {a.team} ×{a.selected}
                {a.planned !== a.selected ? ` (wanted ${a.planned})` : ''}
              </span>
            ))}
        </div>
      )}
      {(selection.notes || []).map((n, i) => (
        <div className="notice" key={i} style={{ marginBottom: 8 }}>
          {n}
        </div>
      ))}

      <div className="card table-wrap" style={{ marginBottom: 0 }}>
        <table>
          <thead>
            <tr>
              <th title="The order to enter them in">#</th>
              <th>Stack</th>
              <th>Pitchers</th>
              {SLOTS.slice(2).map((slot, i) => (
                <th key={i}>{slot}</th>
              ))}
              <th>Salary</th>
              <th>Proj</th>
              <th>Own%</th>
            </tr>
          </thead>
          <tbody>
            {keeps.map((v) => {
              const e = keepEntry(v.keep_rank ?? 0)
              return (
                <tr key={v.index}>
                  <td>{(v.keep_rank ?? 0) + 1}</td>
                  <td>
                    <span className="badge">{v.stack_type || 'none'}</span> {v.stack}
                  </td>
                  <td>{v.pitchers.filter(Boolean).join(' + ')}</td>
                  {SLOTS.slice(2).map((_, i) => (
                    <td key={i}>{e ? slotName(e, i + 2) : '—'}</td>
                  ))}
                  <td>${(v.salary_used || 0).toLocaleString()}</td>
                  <td>{v.projected_points?.toFixed(1)}</td>
                  <td>{v.total_ownership_pct?.toFixed(0)}%</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {keepBatchId && (
        <DkFill date={date} keepBatchId={keepBatchId} count={selection.indices.length} />
      )}

      {cuts.length > 0 && (
        <div style={{ marginTop: 14 }}>
          <button className="ghost" onClick={() => setShowCut((v) => !v)}>
            {showCut ? 'Hide' : 'Show'} why the other {audit.cut} lineups were cut
          </button>
          {showCut && (
            <div className="card table-wrap" style={{ marginTop: 10 }}>
              <table>
                <thead>
                  <tr>
                    <th>Lineup</th>
                    <th>Stack</th>
                    <th>Pitchers</th>
                    <th>Proj</th>
                    <th>Why it was cut</th>
                  </tr>
                </thead>
                <tbody>
                  {cuts.slice(0, 200).map((v) => (
                    <tr key={v.index}>
                      <td>#{v.index + 1}</td>
                      <td>{v.stack || '—'}</td>
                      <td>{v.pitchers.filter(Boolean).join(' + ')}</td>
                      <td>{v.projected_points?.toFixed(1)}</td>
                      <td style={{ whiteSpace: 'normal', maxWidth: 460 }}>{v.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {audit.cut_verdicts_omitted > 0 && (
                <div className="dim" style={{ padding: 10, fontSize: 12 }}>
                  Showing {cuts.length} of {audit.cut} — the other{' '}
                  {audit.cut_verdicts_omitted} aren&rsquo;t sent to the browser, since a batch this
                  size would be megabytes of JSON nobody reads. The &ldquo;all + cut reasons&rdquo;
                  CSV has every one.
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
