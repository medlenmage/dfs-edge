import { useEffect, useState } from 'react'
import { api } from '../api'
import { localTime } from '../format'

/**
 * Loads a real, live DraftKings Classic MLB slate -- players and
 * salaries -- directly from DraftKings' own public API, no manual
 * salary CSV needed. "Browse slates" lists every real slate live for
 * the date (Early, Main, Night, single-game pools, ...) so you can
 * pick exactly the one you're actually playing; once one's loaded,
 * "Refresh" re-pulls that same slate's players/salaries live (for
 * late scratches or swaps close to lock) without needing to re-pick.
 */
export function DkSlatePicker({ date, onLoaded }) {
  const [open, setOpen] = useState(false)
  const [slates, setSlates] = useState(null)
  const [browsing, setBrowsing] = useState(false)
  const [loadingId, setLoadingId] = useState(null)
  const [refreshing, setRefreshing] = useState(false)
  const [loaded, setLoaded] = useState(null) // { draftGroupId, label, playersLoaded }
  const [error, setError] = useState(null)

  // This component stays mounted across a date change (App.jsx doesn't
  // remount it), so a stale "DK: Early (250)" label from the previous
  // date would otherwise linger and mislead.
  useEffect(() => {
    setOpen(false)
    setSlates(null)
    setLoaded(null)
    setError(null)
  }, [date])

  async function browse() {
    setOpen((v) => !v)
    if (slates || browsing) return
    setBrowsing(true)
    setError(null)
    try {
      const result = await api.dkSlates(date)
      setSlates(result.slates)
    } catch (err) {
      setError(err.message)
    } finally {
      setBrowsing(false)
    }
  }

  async function pick(slate) {
    setLoadingId(slate.draft_group_id)
    setError(null)
    try {
      const result = await api.loadDkSlate(date, slate.draft_group_id)
      setLoaded({
        draftGroupId: slate.draft_group_id,
        label: slate.label,
        playersLoaded: result.players_loaded,
      })
      setOpen(false)
      onLoaded?.()
    } catch (err) {
      setError(err.message)
    } finally {
      setLoadingId(null)
    }
  }

  async function refresh() {
    if (!loaded) return
    setRefreshing(true)
    setError(null)
    try {
      const result = await api.loadDkSlate(date, loaded.draftGroupId, { refresh: true })
      setLoaded((prev) => ({ ...prev, playersLoaded: result.players_loaded }))
      onLoaded?.()
    } catch (err) {
      setError(err.message)
    } finally {
      setRefreshing(false)
    }
  }

  return (
    <div style={{ position: 'relative', display: 'inline-flex', gap: 8 }}>
      <button onClick={browse} title="Pull real, live DraftKings slates -- players and salaries -- with no manual CSV upload">
        {loaded ? `DK: ${loaded.label} (${loaded.playersLoaded})` : 'Browse DK slates'}
      </button>

      {loaded && (
        <button
          onClick={refresh}
          disabled={refreshing}
          title="Re-pull THIS slate's players/salaries live from DraftKings -- use close to lock for late scratches or swaps. Doesn't touch matchup data (scores, weather, lines) -- that's the header's own Refresh matchups button"
        >
          {refreshing ? 'Refreshing salaries…' : 'Refresh DK salaries'}
        </button>
      )}

      {open && (
        <div
          className="card"
          // Anchored to the RIGHT edge, not the left: this pops open
          // inside the Data menu, which is itself flush against the
          // right edge of the window, so a left-anchored 320px list ran
          // straight off-screen and its game times were unreadable.
          style={{
            position: 'absolute',
            top: '100%',
            right: 0,
            marginTop: 4,
            zIndex: 20,
            width: 'max(320px, 100%)',
            maxHeight: 360,
            overflowY: 'auto',
          }}
        >
          {browsing && <div className="dim" style={{ fontSize: 13 }}>Loading slates from DraftKings…</div>}
          {error && <div className="notice error" style={{ fontSize: 13 }}>{error}</div>}
          {!browsing && slates && slates.length === 0 && (
            <div className="dim" style={{ fontSize: 13 }}>
              No live DraftKings Classic slates found for this date yet.
            </div>
          )}
          {!browsing &&
            slates?.map((s) => (
              <button
                key={s.draft_group_id}
                onClick={() => pick(s)}
                disabled={loadingId === s.draft_group_id}
                style={{
                  display: 'block',
                  width: '100%',
                  textAlign: 'left',
                  marginBottom: 4,
                  padding: '6px 8px',
                }}
              >
                <div className="name">
                  {s.label} — {s.game_count} game{s.game_count === 1 ? '' : 's'}
                  {loadingId === s.draft_group_id && ' · loading…'}
                </div>
                <div className="sub-line dim" style={{ fontSize: 12 }}>
                  {s.games.map((g) => `${g.away}@${g.home} ${localTime(g.start_time_utc)}`).join(', ')}
                </div>
              </button>
            ))}
        </div>
      )}
    </div>
  )
}
