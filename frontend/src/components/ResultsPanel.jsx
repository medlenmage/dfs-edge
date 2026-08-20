import { useEffect, useRef, useState } from 'react'
import { api } from '../api'

/**
 * Upload real, completed DraftKings contest-standings exports (the
 * .zip DK gives you after a contest finishes, or the .csv inside it --
 * different from the pre-contest salary CSV or the bulk-entries upload
 * template) and see a running history of your own results over time.
 *
 * Every upload archives permanently: every player's real final
 * ownership%/actual FPTS (real market ground truth this app has never
 * had before), and -- once your own entry is identified, either by its
 * exact EntryId or a best-effort handle match -- your own rank/points
 * for a real bankroll history. No $ payout tracking here: a contest-
 * standings export has no payout-table data at all, so this shows what's
 * actually knowable (rank, points, entry cost) rather than guessing at
 * winnings.
 */
export function ResultsPanel({ date }) {
  const fileInputRef = useRef(null)
  const [contestName, setContestName] = useState('')
  const [entryFee, setEntryFee] = useState('')
  const [myEntryId, setMyEntryId] = useState('')
  const [myHandle, setMyHandle] = useState('')
  const [upload, setUpload] = useState({ status: 'idle' })
  const [history, setHistory] = useState({ status: 'loading' })

  async function loadHistory() {
    setHistory({ status: 'loading' })
    try {
      const result = await api.contestResultsHistory()
      setHistory({ status: 'ready', ...result })
    } catch (err) {
      setHistory({ status: 'error', message: err.message })
    }
  }

  useEffect(() => {
    loadHistory()
  }, [])

  async function handleUpload(e) {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    setUpload({ status: 'loading' })
    try {
      const result = await api.uploadContestResults(date, file, {
        contestName: contestName.trim() || null,
        entryFee: entryFee.trim() ? Number(entryFee) : null,
        myEntryId: myEntryId.trim() || null,
        myHandle: myHandle.trim() || null,
      })
      setUpload({ status: 'ready', ...result })
      loadHistory()
    } catch (err) {
      setUpload({ status: 'error', message: err.message })
    }
  }

  return (
    <div>
      <div className="card" style={{ marginBottom: 16 }}>
        <div className="sub-line" style={{ marginBottom: 10 }}>
          Upload a real DraftKings contest-standings export (the .zip you download after a
          contest finishes, or the .csv inside it) to archive real ownership%/actual FPTS
          permanently, and track your own results over time.
        </div>
        <div className="controls" style={{ flexWrap: 'wrap', marginBottom: 10 }}>
          <input
            placeholder="Contest name (e.g. $5K Solo Shot)"
            value={contestName}
            onChange={(e) => setContestName(e.target.value)}
            style={{ minWidth: 200 }}
          />
          <input
            placeholder="Entry fee"
            value={entryFee}
            onChange={(e) => setEntryFee(e.target.value)}
            style={{ width: 90 }}
            title="Real entry fee, for a running total-cost figure -- the file has no payout data at all"
          />
          <input
            placeholder="Your EntryId (optional)"
            value={myEntryId}
            onChange={(e) => setMyEntryId(e.target.value)}
            style={{ width: 160 }}
            title="The reliable way to identify your own entry -- find it in your DK entry history/notifications"
          />
          <input
            placeholder="Your DK handle (optional)"
            value={myHandle}
            onChange={(e) => setMyHandle(e.target.value)}
            style={{ width: 160 }}
            title="Best-effort fallback if you don't know your EntryId -- not guaranteed in a big public field with similar handles"
          />
        </div>
        <input
          ref={fileInputRef}
          type="file"
          accept=".csv,.zip"
          onChange={handleUpload}
          style={{ display: 'none' }}
        />
        <button onClick={() => fileInputRef.current?.click()} disabled={upload.status === 'loading'}>
          {upload.status === 'loading' ? 'Uploading…' : 'Upload contest results'}
        </button>

        {upload.status === 'error' && (
          <div className="notice error" style={{ marginTop: 10 }}>{upload.message}</div>
        )}
        {upload.status === 'ready' && (
          <div className="notice" style={{ marginTop: 10 }}>
            Archived {upload.field_size} entries, {upload.players_found} real players.{' '}
            {upload.my_entry ? (
              <>Your entry: rank {upload.my_entry.rank}, {upload.my_entry.points} points.</>
            ) : (
              myEntryId || myHandle ? (
                <>Couldn't find your entry -- double check the EntryId or handle.</>
              ) : (
                <>No EntryId/handle given, so your own entry wasn't identified -- this contest's ownership/FPTS data still archived.</>
              )
            )}
          </div>
        )}
      </div>

      <div className="card table-wrap">
        <div className="sub-line" style={{ marginBottom: 8 }}>
          Your results over time
        </div>
        {history.status === 'loading' && <div className="dim">Loading…</div>}
        {history.status === 'error' && <div className="notice error">{history.message}</div>}
        {history.status === 'ready' && history.contests.length === 0 && (
          <div className="dim" style={{ fontSize: 13 }}>
            No identified entries yet -- upload a contest above with your EntryId or handle to
            start tracking results here.
          </div>
        )}
        {history.status === 'ready' && history.contests.length > 0 && (
          <>
            <div style={{ display: 'flex', gap: 16, marginBottom: 12, flexWrap: 'wrap' }}>
              <span className="badge">{history.total_entries} contests tracked</span>
              <span className="badge">${history.total_cost.toFixed(2)} total entry cost</span>
            </div>
            <table>
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Contest</th>
                  <th className="num">Field size</th>
                  <th className="num">Your rank</th>
                  <th className="num">Your points</th>
                  <th className="num">Entry fee</th>
                </tr>
              </thead>
              <tbody>
                {history.contests.map((c) => (
                  <tr key={c.contest_id}>
                    <td className="dim">{c.date}</td>
                    <td className="name">{c.contest_name}</td>
                    <td className="num">{c.field_size?.toLocaleString?.() ?? c.field_size}</td>
                    <td className="num">{c.my_rank ?? '—'}</td>
                    <td className="num">{c.my_points ?? '—'}</td>
                    <td className="num">{c.entry_fee != null ? `$${c.entry_fee}` : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </div>
    </div>
  )
}
