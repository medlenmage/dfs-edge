import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'

/**
 * The lineups you have set aside to actually enter today.
 *
 * The contest generator builds by weighted random construction, which
 * is the right model for an OPPONENT FIELD but the wrong way to produce
 * the entries you play: it can be steered toward the process rules, not
 * instructed to follow them. The optimizer can be instructed. This card
 * is where optimizer output (or lineups you already built inside
 * DraftKings) gets set aside, so the contest can be built AROUND them --
 * they lead the batch, the generator fills the field behind them, and
 * the simulator, the build audit and the daily brief all read the same
 * batch while still being able to tell yours from the field.
 */

export function MyLineupsCard({ date, optimizerLineups, onChange }) {
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
    if (result.rejected?.length) bits.push(`${result.rejected.length} rejected`)
    setMessage({
      text: bits.join(', '),
      // Rejections are shown in full: a lineup that silently vanished
      // would leave the portfolio quietly short.
      rejected: result.rejected || [],
      note: result.note,
    })
  }

  async function addOptimizer() {
    if (!optimizerLineups?.length) return
    setBusy('optimizer')
    setMessage(null)
    try {
      const lineups = optimizerLineups.map((lu, i) => ({
        players: Object.values(lu.slots || {})
          .flat()
          .map((p) => p.id),
        label: `optimizer #${i + 1}${lu.stack ? ` (${lu.stack_type} ${lu.stack})` : ''}`,
      }))
      report(await api.addMyLineups(date, lineups, { source: 'optimizer' }))
      await load()
    } catch (err) {
      setMessage({ text: err.message, rejected: [] })
    } finally {
      setBusy(null)
    }
  }

  async function addFromDk() {
    setBusy('dk')
    setMessage(null)
    try {
      report(await api.myLineupsFromDkEntries(date))
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
        Lineups you&rsquo;ll actually enter. Build the contest with &ldquo;Use my lineups&rdquo; and
        these lead the batch while the generator fills the field around them — so the audit and the
        daily brief work from lineups that <em>follow</em> the process rules instead of ones the
        generator was steered toward.
      </div>

      <div className="controls" style={{ gap: 6 }}>
        <button
          className="sm"
          onClick={addOptimizer}
          disabled={busy || !optimizerLineups?.length}
          title={
            optimizerLineups?.length
              ? `Set aside the ${optimizerLineups.length} lineups currently in Single lineup mode`
              : 'Build lineups in Single lineup mode first'
          }
        >
          {busy === 'optimizer' ? 'Adding…' : `Add ${optimizerLineups?.length || 0} from optimizer`}
        </button>
        <button
          className="sm"
          onClick={addFromDk}
          disabled={busy}
          title="Read lineups you already built on DraftKings out of an uploaded entries CSV"
        >
          {busy === 'dk' ? 'Reading…' : 'From DK entries file'}
        </button>
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
