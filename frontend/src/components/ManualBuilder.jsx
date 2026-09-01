import { useState } from 'react'
import { api } from '../api'

/**
 * Build lineups by hand -- or hand the slate to another Claude and have
 * it build them for you.
 *
 * Two steps, deliberately symmetric: copy a self-contained slate brief
 * out, paste the lineups back in. The brief carries the roster rules,
 * this account's process rules and the whole player board, so it works
 * in a Claude with no access to this machine.
 *
 * Paste-back is forgiving about format and strict about content: the
 * names go through the same validation as an optimizer lineup or a DK
 * export, and land in the same per-day pool -- so a hand-built lineup
 * reaches the simulator, the audit and the daily brief by exactly the
 * same route.
 */

export function ManualBuilder({ date, onPoolChange }) {
  const [copied, setCopied] = useState(false)
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(null)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)

  async function copyBrief() {
    setBusy('brief')
    setError(null)
    try {
      const brief = await api.manualBrief(date)
      await navigator.clipboard.writeText(brief)
      setCopied(true)
      setTimeout(() => setCopied(false), 4000)
    } catch (err) {
      setError(`${err.message} — you can also open it directly at ${api.manualBriefUrl(date)}`)
    } finally {
      setBusy(null)
    }
  }

  async function run(save) {
    if (!text.trim()) return
    setBusy(save ? 'save' : 'check')
    setError(null)
    setResult(null)
    try {
      const res = await api.myLineupsFromText(date, text, { save })
      setResult({ ...res, saved: save })
      if (save) {
        onPoolChange?.(res.count)
        if (res.accepted) setText('')
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="card" style={{ marginTop: 16 }}>
      <div style={{ fontWeight: 600 }}>Build by hand, or hand it to another Claude</div>
      <div className="sub-line" style={{ marginBottom: 10 }}>
        Copy the slate brief, paste it into any Claude — it carries the roster rules, your process
        rules and the whole player board, so it works somewhere with no access to this machine.
        Paste the lineups back below and they join the same pool the optimizer feeds.
      </div>

      <div className="controls" style={{ marginBottom: 12 }}>
        <button className="primary sm" onClick={copyBrief} disabled={busy}>
          {busy === 'brief' ? 'Building…' : copied ? '✓ Copied' : 'Copy slate brief'}
        </button>
        <a className="btn sm" href={api.manualBriefUrl(date)} target="_blank" rel="noreferrer">
          Open it
        </a>
      </div>

      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={8}
        placeholder={
          'Paste the lineups back here.\n\n' +
          'Numbered lists, "P: Name" slot labels, CSV rows and blank-line-separated\n' +
          'blocks all work — ten names per lineup is the only thing that matters.'
        }
        style={{ width: '100%', fontFamily: 'inherit', fontSize: 13, resize: 'vertical' }}
      />

      <div className="controls" style={{ marginTop: 8 }}>
        <button className="sm" onClick={() => run(false)} disabled={busy || !text.trim()}>
          {busy === 'check' ? 'Checking…' : 'Check without saving'}
        </button>
        <button className="primary sm" onClick={() => run(true)} disabled={busy || !text.trim()}>
          {busy === 'save' ? 'Saving…' : 'Validate & add to pool'}
        </button>
      </div>

      {error && (
        <div className="notice error" style={{ marginTop: 10 }}>
          {error}
        </div>
      )}

      {result && (
        <div className="notice" style={{ marginTop: 10, fontSize: 12.5 }}>
          Read {result.parsed} lineup{result.parsed === 1 ? '' : 's'} —{' '}
          {result.saved
            ? `${result.accepted} added` +
              (result.duplicates_skipped ? `, ${result.duplicates_skipped} already in the pool` : '')
            : `${result.would_accept} would be accepted (nothing saved)`}
          {result.rejected?.length ? `, ${result.rejected.length} rejected` : ''}.
          {result.rejected?.length > 0 && (
            <ul style={{ margin: '6px 0 0', paddingLeft: 16 }}>
              {result.rejected.slice(0, 8).map((r) => (
                <li key={r.index}>
                  <strong>{r.label}</strong>: {r.problems.join('; ')}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}
