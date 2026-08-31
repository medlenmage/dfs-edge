import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'

/**
 * The lineups you have set aside to actually enter today.
 *
 * The contest generator builds by weighted random construction, which
 * is the right model for an OPPONENT FIELD but the wrong way to produce
 * the entries you play: it can be steered toward the process rules, not
 * instructed to follow them. The optimizer can be instructed. This card
 * is the contest side of the pool built in the optimizer (LineupPool):
 * it reports what is in the pool, can pull in lineups you already built
 * inside DraftKings, and gates the toggle that makes the contest build
 * AROUND them -- they lead the batch, the generator fills the field
 * behind them, and the simulator, the build audit and the daily brief
 * all read the same batch while still telling yours from the field.
 *
 * Saving optimizer lineups happens in LineupPool, not here, so there is
 * exactly one place that does it -- and it is the place where you can
 * pick WHICH of a run's lineups are worth keeping.
 */

export function MyLineupsCard({ date, onChange }) {
  const fileRef = useRef(null)
  const [tray, setTray] = useState({ status: 'loading' })
  const [busy, setBusy] = useState(null)
  const [message, setMessage] = useState(null)

  const load = useCallback(async () => {
    try {
      const data = await api.myLineups(date)
      setTray({ status: 'ready', ...data })
      onChange?.(data.count)
    } catch (err) {
      setTray({ status: 'error', message: err.message })
    }
  }, [date, onChange])

  useEffect(() => {
    load()
  }, [load])

  function report(result) {
    const bits = [`${result.accepted} added`]
    if (result.duplicates_skipped) bits.push(`${result.duplicates_skipped} already in the pool`)
    if (result.rejected?.length) bits.push(`${result.rejected.length} rejected`)
    setMessage({
      text: bits.join(', '),
      // Rejections are shown in full: a lineup that silently vanished
      // would leave the portfolio quietly short.
      rejected: result.rejected || [],
      note: result.note,
    })
  }

  // Take the file here rather than expecting one to have been uploaded
  // somewhere else first. The button says "upload", so it uploads: pick
  // the CSV, it is stored for the date the same way the Data menu
  // stores it, and its lineups are read straight back out in one step.
  async function uploadAndImport(e) {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    setBusy('dk')
    setMessage(null)
    try {
      await api.uploadDkEntries(date, file)
      const result = await api.myLineupsFromDkEntries(date)
      report(result)
      await load()
    } catch (err) {
      setMessage({ text: err.message, rejected: [] })
    } finally {
      setBusy(null)
    }
  }

  async function clear() {
    setBusy('clear')
    setMessage(null)
    try {
      await api.clearMyLineups(date)
      await load()
    } finally {
      setBusy(null)
    }
  }

  const count = tray.count || 0

  return (
    <div className="field">
      <label>My lineups ({count})</label>
      <div className="sub-line" style={{ marginBottom: 8, fontSize: 12 }}>
        Lineups you&rsquo;ll actually enter. Build them under <strong>Single lineup</strong> and
        save the keepers to the pool there — solve, change your locks, solve again, until the pool
        is the size you want. Then tick &ldquo;Use my lineups&rdquo; below: they lead the batch
        while the generator fills the field around them, so the audit and the daily brief work from
        lineups that <em>follow</em> the process rules rather than ones the generator was steered
        toward.
      </div>

      <div className="controls" style={{ gap: 6 }}>
        <button
          className="sm"
          onClick={() => fileRef.current?.click()}
          disabled={busy}
          title="Upload a DraftKings bulk-entries CSV whose lineups you've already filled in on DK"
        >
          {busy === 'dk' ? 'Reading…' : 'Upload DK entries file'}
        </button>
        <input
          ref={fileRef}
          type="file"
          accept=".csv"
          onChange={uploadAndImport}
          style={{ display: 'none' }}
        />
        {count > 0 && (
          <button className="sm" onClick={clear} disabled={busy}>
            Clear
          </button>
        )}
      </div>

      {tray.status === 'error' && (
        <div className="notice error" style={{ marginTop: 8 }}>
          {tray.message}
        </div>
      )}

      {message && (
        <div className="notice" style={{ marginTop: 8, fontSize: 12 }}>
          {message.text}
          {message.note && <div style={{ marginTop: 4 }}>{message.note}</div>}
          {message.rejected.length > 0 && (
            <ul style={{ margin: '6px 0 0', paddingLeft: 16 }}>
              {message.rejected.slice(0, 6).map((r) => (
                <li key={r.index}>
                  <strong>{r.label}</strong>: {r.problems.join('; ')}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {count > 0 && (
        <div style={{ marginTop: 8, display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {(tray.sources || []).map((s) => (
            <span className="badge" key={s.source}>
              {s.count} {s.source}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}
