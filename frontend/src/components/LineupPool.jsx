import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'

/**
 * The lineup pool, sitting inside the optimizer where it's built.
 *
 * The workflow this exists for: solve a few lineups, add the ones worth
 * keeping, change what's locked, solve a few more, and repeat until the
 * pool is the size you want -- then hand the whole thing to the contest
 * and the simulator. No single optimizer run can do that, because each
 * run only knows about its own lineups.
 *
 * Which is also why POOL EXPOSURE is shown rather than left to the
 * per-run exposure report already on this page: after a run, the
 * question is which players are already over-represented across
 * EVERYTHING pooled so far, and that is the number that tells you what
 * to lock (or exclude) on the next pass.
 *
 * The pool is the same per-day tray the Contest tab's "Use my lineups"
 * consumes -- deliberately one concept, not two.
 */

const SLOTS = ['P', 'P', 'C', '1B', '2B', '3B', 'SS', 'OF', 'OF', 'OF']

function PoolExposure({ exposure, poolSize }) {
  if (!exposure?.length) return null
  return (
    <div style={{ marginTop: 12 }}>
      <div className="sub-line" style={{ marginBottom: 6 }}>
        Exposure across the pool — who to lock, or back off, on the next run
      </div>
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
        {exposure.slice(0, 18).map((e) => (
          <span
            key={e.id}
            className={`badge${e.pct >= 60 ? ' risk' : e.pct >= 35 ? ' warn' : ''}`}
            title={`${e.count} of ${poolSize} lineups`}
          >
            {e.name} {e.pct}%
          </span>
        ))}
      </div>
    </div>
  )
}

export function LineupPool({ date, lineups, onPoolChange }) {
  const [pool, setPool] = useState({ status: 'loading' })
  const [picked, setPicked] = useState(() => new Set())
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState(null)

  const load = useCallback(async () => {
    try {
      const data = await api.myLineups(date)
      setPool({ status: 'ready', ...data })
      onPoolChange?.(data.count)
    } catch (err) {
      setPool({ status: 'error', message: err.message })
    }
  }, [date, onPoolChange])

  useEffect(() => {
    load()
  }, [load])

  // A fresh solve is a fresh set of candidates, so nothing carries over
  // selected from the previous run.
  useEffect(() => {
    setPicked(new Set((lineups || []).map((_, i) => i)))
    setMessage(null)
  }, [lineups])

  function toggle(i) {
    setPicked((prev) => {
      const next = new Set(prev)
      next.has(i) ? next.delete(i) : next.add(i)
      return next
    })
  }

  async function addPicked() {
    const chosen = [...picked].sort((a, b) => a - b)
    if (!chosen.length) return
    setBusy(true)
    setMessage(null)
    try {
      const payload = chosen.map((i) => {
        const lu = lineups[i]
        return {
          players: Object.values(lu.slots || {})
            .flat()
            .map((p) => p.id),
          label: `optimizer${lu.stack ? ` ${lu.stack_type} ${lu.stack}` : ''}`,
        }
      })
      const res = await api.addMyLineups(date, payload, { source: 'optimizer' })
      setPool({ status: 'ready', ...res })
      onPoolChange?.(res.count)
      setMessage({
        text:
          `${res.accepted} added` +
          (res.duplicates_skipped
            ? `, ${res.duplicates_skipped} already in the pool`
            : '') +
          (res.rejected?.length ? `, ${res.rejected.length} rejected` : ''),
        rejected: res.rejected || [],
      })
    } catch (err) {
      setMessage({ text: err.message, rejected: [] })
    } finally {
      setBusy(false)
    }
  }

  async function remove(entryId) {
    setBusy(true)
    try {
      const res = await api.removeMyLineups(date, [entryId])
      setPool({ status: 'ready', ...res })
      onPoolChange?.(res.count)
    } finally {
      setBusy(false)
    }
  }

  async function clearAll() {
    setBusy(true)
    try {
      const res = await api.clearMyLineups(date)
      setPool({ status: 'ready', ...res })
      onPoolChange?.(res.count)
    } finally {
      setBusy(false)
    }
  }

  const entries = pool.entries || []
  const count = pool.count || 0

  return (
    <div className="card" style={{ marginTop: 16 }}>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'baseline',
          gap: 12,
          flexWrap: 'wrap',
        }}
      >
        <div>
          <div style={{ fontWeight: 600 }}>Lineup pool — {count} saved</div>
          <div className="sub-line">
            Save the builds worth keeping, change your locks, solve again, and keep adding. When the
            pool is the size you want, switch to Contest and tick &ldquo;Use my lineups&rdquo; — the
            whole pool leads the batch and gets simulated against the generated field.
          </div>
        </div>
        {count > 0 && (
          <button className="sm" onClick={clearAll} disabled={busy}>
            Clear pool
          </button>
        )}
      </div>

      {lineups?.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div className="controls" style={{ marginBottom: 8 }}>
            <button className="primary sm" onClick={addPicked} disabled={busy || !picked.size}>
              {busy ? 'Saving…' : `Save ${picked.size} of ${lineups.length} to pool`}
            </button>
            <button
              className="sm"
              onClick={() => setPicked(new Set(lineups.map((_, i) => i)))}
              disabled={busy}
            >
              All
            </button>
            <button className="sm" onClick={() => setPicked(new Set())} disabled={busy}>
              None
            </button>
          </div>

          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th style={{ width: 28 }}></th>
                  <th>This run</th>
                  <th>Stack</th>
                  <th>Salary</th>
                  <th>Proj</th>
                  <th>Own%</th>
                </tr>
              </thead>
              <tbody>
                {lineups.map((lu, i) => (
                  <tr key={i}>
                    <td>
                      <input
                        type="checkbox"
                        checked={picked.has(i)}
                        onChange={() => toggle(i)}
                        aria-label={`Save lineup ${i + 1}`}
                      />
                    </td>
                    <td>#{i + 1}</td>
                    <td>
                      <span className="badge">{lu.stack_type || 'none'}</span> {lu.stack}
                    </td>
                    <td>${lu.salary_used?.toLocaleString()}</td>
                    <td>{lu.projected_points?.toFixed(1)}</td>
                    <td>{lu.total_ownership_pct?.toFixed(0)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {message && (
        <div className="notice" style={{ marginTop: 10, fontSize: 12.5 }}>
          {message.text}
          {message.rejected.length > 0 && (
            <ul style={{ margin: '6px 0 0', paddingLeft: 16 }}>
              {message.rejected.slice(0, 5).map((r) => (
                <li key={r.index}>
                  <strong>{r.label}</strong>: {r.problems.join('; ')}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {pool.status === 'error' && (
        <div className="notice error" style={{ marginTop: 10 }}>
          {pool.message}
        </div>
      )}

      {count > 0 && (
        <>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 14 }}>
            <span className="badge">avg ${pool.summary?.avg_salary_used?.toLocaleString()}</span>
            <span className="badge">avg {pool.summary?.avg_projected_points} proj</span>
            <span className="badge">avg {pool.summary?.avg_total_ownership_pct}% own</span>
            {(pool.stack_shapes || []).slice(0, 6).map((s) => (
              <span className="badge" key={s.shape}>
                {s.count}× {s.shape}
              </span>
            ))}
          </div>

          <PoolExposure exposure={pool.exposure} poolSize={count} />

          <div className="table-wrap" style={{ marginTop: 12 }}>
            <table>
              <thead>
                <tr>
                  <th>#</th>
                  <th>Stack</th>
                  {SLOTS.map((slot, i) => (
                    <th key={i}>{slot}</th>
                  ))}
                  <th>Salary</th>
                  <th>Proj</th>
                  <th>Own%</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {entries.map((e, i) => (
                  <tr key={e.entry_id || i}>
                    <td>{i + 1}</td>
                    <td>
                      <span className="badge">{e.stack_type || 'none'}</span> {e.stack}
                    </td>
                    {SLOTS.map((_, si) => (
                      <td key={si}>{e.players?.[si]?.name || '—'}</td>
                    ))}
                    <td>${e.salary_used?.toLocaleString()}</td>
                    <td>{e.projected_points?.toFixed(1)}</td>
                    <td>{e.total_ownership_pct?.toFixed(0)}%</td>
                    <td>
                      <button
                        className="sm"
                        onClick={() => remove(e.entry_id)}
                        disabled={busy || !e.entry_id}
                        title="Drop this lineup from the pool"
                      >
                        ✕
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  )
}
