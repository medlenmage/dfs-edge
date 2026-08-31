import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { renderMarkdown } from '../markdown'

/**
 * The two scheduled Claude reads a day (services/briefs.py) -- a morning
 * slate read and a pre-lock audit of the latest contest build -- plus
 * the on-demand build audit. Everything here is also produced
 * automatically by the backend timer; the buttons just don't make you
 * wait for it.
 */

function localTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

function Severity({ level }) {
  const cls = level === 'high' ? 'badge risk' : level === 'ok' ? 'badge ok' : 'badge'
  return <span className={cls}>{level}</span>
}

function BriefCard({ kind, title, blurb, date, initial, targetCount }) {
  const [state, setState] = useState(initial ? { status: 'ready', data: initial } : { status: 'empty' })

  useEffect(() => {
    if (initial) setState({ status: 'ready', data: initial })
    else setState({ status: 'empty' })
  }, [initial])

  async function run() {
    setState((s) => ({ ...s, status: 'loading' }))
    try {
      const data = await api.runBrief(kind, date, { targetCount: targetCount || null })
      setState({ status: 'ready', data })
    } catch (err) {
      setState((s) => ({ ...s, status: 'error', message: err.message }))
    }
  }

  const data = state.data
  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 12, flexWrap: 'wrap' }}>
        <div>
          <div style={{ fontWeight: 600 }}>{title}</div>
          <div className="sub-line">{blurb}</div>
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          {data?.generated_at && (
            <span className="dim" style={{ fontSize: 12 }}>
              generated {localTime(data.generated_at)}
              {data.provider === 'claude-code' ? ' · subscription' : data.provider ? ` · ${data.provider}` : ''}
            </span>
          )}
          <button onClick={run} disabled={state.status === 'loading'}>
            {state.status === 'loading' ? 'Running…' : data ? 'Regenerate' : 'Run now'}
          </button>
        </div>
      </div>
      {state.status === 'error' && <div className="notice error" style={{ marginTop: 10 }}>{state.message}</div>}
      {state.status === 'empty' && (
        <div className="dim" style={{ marginTop: 10, fontSize: 13 }}>
          Not generated yet for {date}. The timer will do it, or run it now.
        </div>
      )}
      {data?.audit && (
        <div style={{ marginTop: 12 }}>
          <div className="sub-line" style={{ marginBottom: 6 }}>
            Build audit of batch {data.batch_id?.slice(0, 8)} — keep {data.audit.keep}, cut {data.audit.cut}
          </div>
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {(data.audit.flags || []).map((f, i) => (
              <li key={i} style={{ fontSize: 13 }}>
                <Severity level={f.severity} /> {f.text}
              </li>
            ))}
          </ul>
        </div>
      )}
      {data?.text && (
        <div className="analysis" style={{ marginTop: 14 }} dangerouslySetInnerHTML={{ __html: renderMarkdown(data.text) }} />
      )}
    </div>
  )
}

export function BriefsPanel({ date, enabled }) {
  const [index, setIndex] = useState({ status: 'loading' })
  const [morning, setMorning] = useState(null)
  const [prelock, setPrelock] = useState(null)
  const [targetCount, setTargetCount] = useState('')
  const [audit, setAudit] = useState({ status: 'idle' })

  const load = useCallback(async () => {
    try {
      const [idx, m, p] = await Promise.all([api.briefs(), api.brief('morning', date), api.brief('prelock', date)])
      setIndex({ status: 'ready', ...idx })
      setMorning(m.available ? m : null)
      setPrelock(p.available ? p : null)
    } catch (err) {
      setIndex({ status: 'error', message: err.message })
    }
  }, [date])

  useEffect(() => {
    load()
  }, [load])

  async function runAudit() {
    setAudit({ status: 'loading' })
    try {
      const data = await api.buildAudit(date, { targetCount: targetCount ? Number(targetCount) : null })
      setAudit({ status: 'ready', data })
    } catch (err) {
      setAudit({ status: 'error', message: err.message })
    }
  }

  const sched = index.schedule
  const latest = sched?.latest_batch

  return (
    <div>
      {!enabled && (
        <div className="notice" style={{ marginBottom: 16 }}>
          No Claude access configured -- log in to Claude Code or set ANTHROPIC_API_KEY to turn briefs on.
        </div>
      )}

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="sub-line" style={{ marginBottom: 8 }}>Schedule</div>
        {index.status === 'loading' && <div className="dim">Loading…</div>}
        {index.status === 'error' && <div className="notice error">{index.message}</div>}
        {sched && (
          <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', fontSize: 13 }}>
            <span className={sched.enabled ? 'badge ok' : 'badge risk'}>{sched.enabled ? 'timer on' : 'timer off (BRIEFS_ENABLED=false)'}</span>
            <span className="badge">
              morning {localTime(sched.morning.scheduled_local)} {sched.morning.fired ? '· fired' : ''}
            </span>
            <span className="badge">
              pre-lock {sched.prelock.scheduled_local ? localTime(sched.prelock.scheduled_local) : 'no DK slate found'}
              {sched.prelock.lock_local ? ` (lock ${localTime(sched.prelock.lock_local)}, ${sched.prelock.slate_label})` : ''}
              {sched.prelock.fired ? ' · fired' : ''}
            </span>
            <span className="badge">
              {latest?.batch_id
                ? `latest build: ${latest.total_entries} entries via ${latest.source}`
                : 'no contest build recorded today'}
            </span>
            <span className="dim">{sched.timezone}</span>
          </div>
        )}
      </div>

      <BriefCard
        kind="morning"
        title="Morning brief"
        blurb="Yesterday's process audit (if uploaded), environments ranked, the pitcher core, traps, and a build plan."
        date={date}
        initial={morning}
      />

      <div className="card" style={{ marginBottom: 16 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          <div>
            <div style={{ fontWeight: 600 }}>Build audit</div>
            <div className="sub-line">
              Scores the latest contest build against the process rules -- pitcher core, stack
              conviction, batting order, filler, salary -- with a keep/cut per entry. The pre-lock brief
              runs this automatically.
            </div>
          </div>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <input
              placeholder="Entries you'll play"
              value={targetCount}
              onChange={(e) => setTargetCount(e.target.value)}
              style={{ width: 140 }}
              title="How many entries you actually intend to enter -- the audit trims to this"
            />
            <button onClick={runAudit} disabled={audit.status === 'loading'}>
              {audit.status === 'loading' ? 'Auditing…' : 'Audit latest build'}
            </button>
          </div>
        </div>
        {audit.status === 'error' && <div className="notice error" style={{ marginTop: 10 }}>{audit.message}</div>}
        {audit.status === 'ready' && (
          <div className="analysis" style={{ marginTop: 12 }} dangerouslySetInnerHTML={{ __html: renderMarkdown(audit.data.markdown) }} />
        )}
      </div>

      <BriefCard
        kind="prelock"
        title="Pre-lock brief"
        blurb="Re-pulls the slate, audits the latest build, lists what moved since the morning, and gives a cut/keep verdict."
        date={date}
        initial={prelock}
        targetCount={targetCount ? Number(targetCount) : null}
      />

      {index.briefs?.length > 0 && (
        <div className="card">
          <div className="sub-line" style={{ marginBottom: 6 }}>Recent briefs</div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            {index.briefs.slice(0, 14).map((b) => (
              <span key={`${b.date}-${b.kind}`} className="badge">
                {b.date} {b.kind}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
