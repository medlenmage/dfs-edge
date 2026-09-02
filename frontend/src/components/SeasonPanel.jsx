import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { api } from '../api'

// The Sleeper account, once connected, is worth remembering between
// sessions -- it is a public username, not a credential, and retyping it
// every time you open the app during draft season would be tedious.
const STORE_KEY = 'dfsedge.season.sleeper'

function loadSaved() {
  try {
    return JSON.parse(localStorage.getItem(STORE_KEY) || 'null')
  } catch {
    return null
  }
}

function save(value) {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(value))
  } catch {
    /* private window or storage disabled -- the app works without it */
  }
}

const POSITIONS = ['ALL', 'QB', 'RB', 'WR', 'TE']

function Note({ children, kind = 'warn' }) {
  if (!children) return null
  return (
    <div className="card" style={{ borderLeft: '3px solid var(--warning)', marginBottom: 14 }}>
      <span className={`badge ${kind}`}>heads up</span>{' '}
      <span style={{ fontSize: 13 }}>{children}</span>
    </div>
  )
}

function Sources({ board }) {
  const s = board?.source
  if (!s) return null
  return (
    <p style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 10 }}>
      Projections: {s.projections}
      {s.draft_group_label ? ` — DK draft group ${s.draft_group_id} (${s.draft_group_label})` : ''}.
      <br />
      Rank: {s.rank}.
    </p>
  )
}

// ------------------------------------------------------------------ connect

function ConnectCard({ saved, onConnect, onDisconnect }) {
  const [username, setUsername] = useState(saved?.username || '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function connect(e) {
    e.preventDefault()
    if (!username.trim()) return
    setBusy(true)
    setError(null)
    try {
      const data = await api.seasonUser(username.trim())
      onConnect(data)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  if (saved?.user) {
    return (
      <div className="card" style={{ marginBottom: 16 }}>
        <div className="controls" style={{ justifyContent: 'space-between' }}>
          <div style={{ fontSize: 13 }}>
            Connected to Sleeper as <strong>{saved.user.display_name || saved.user.username}</strong>{' '}
            <span className="badge">{saved.leagues?.length || 0} leagues</span>{' '}
            <span className="badge">{saved.drafts?.length || 0} drafts</span>
          </div>
          <button className="ghost" onClick={onDisconnect}>
            Disconnect
          </button>
        </div>
      </div>
    )
  }

  return (
    <form className="card" style={{ marginBottom: 16 }} onSubmit={connect}>
      <div style={{ fontSize: 13, marginBottom: 8 }}>
        <strong>Connect Sleeper.</strong> Sleeper&rsquo;s API is public and read-only — your
        username is all it needs. No password, no OAuth, nothing to authorise, and nothing here
        can change your roster or make a pick.
      </div>
      <div className="controls">
        <input
          type="text"
          placeholder="Sleeper username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          style={{ minWidth: 220 }}
        />
        <button className="primary" disabled={busy || !username.trim()}>
          {busy ? 'Looking up…' : 'Connect'}
        </button>
      </div>
      {error && (
        <p style={{ color: 'var(--critical-ink)', fontSize: 13, marginTop: 8 }}>{error}</p>
      )}
    </form>
  )
}

// ------------------------------------------------------------------ board

function BoardTable({ players, limit = 250 }) {
  return (
    <div className="card table-wrap">
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>Player</th>
            <th>Pos</th>
            <th>Team</th>
            <th title="DraftKings' projected fantasy points per game">Proj/g</th>
            <th title="Value over replacement: points per game above the best player who will not be started anywhere in this league">
              VORP
            </th>
            <th>Pos rank</th>
            <th title="Players inside a tier are close enough to be interchangeable; a tier break is a real cliff">
              Tier
            </th>
            <th title="Blend of DraftKings' board order and Sleeper's rank. Not measured ADP.">
              Cons. rank
            </th>
            <th>Bye</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {players.slice(0, limit).map((p) => (
            <tr key={`${p.dk_id}-${p.name}`}>
              <td>{p.overall_rank}</td>
              <td>{p.name}</td>
              <td>{p.position}</td>
              <td>{p.team}</td>
              <td>{p.projection?.toFixed(1) ?? '—'}</td>
              <td>
                <strong>{p.vorp?.toFixed(1) ?? '—'}</strong>
              </td>
              <td>
                {p.position}
                {p.position_rank}
              </td>
              <td>
                <span className="badge">T{p.tier}</span>
              </td>
              <td>{p.consensus_rank ?? '—'}</td>
              <td>{p.bye_week ?? '—'}</td>
              <td>
                {p.injury_status ? (
                  <span className="badge risk">{p.injury_status}</span>
                ) : (
                  <span style={{ color: 'var(--text-muted)' }}>—</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function BoardView({ leagueId, bestBall }) {
  const [board, setBoard] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [pos, setPos] = useState('ALL')

  const load = useCallback(
    async (force = false) => {
      setLoading(true)
      setError(null)
      try {
        setBoard(bestBall ? await api.seasonBestBall(force) : await api.seasonBoard(leagueId, force))
      } catch (err) {
        setError(err.message)
      } finally {
        setLoading(false)
      }
    },
    [leagueId, bestBall],
  )

  useEffect(() => {
    load()
  }, [load])

  if (loading && !board) return <p style={{ color: 'var(--text-muted)' }}>Loading the board…</p>
  if (error) return <p style={{ color: 'var(--critical-ink)' }}>{error}</p>
  if (!board) return null
  if (!board.players.length)
    return <p style={{ color: 'var(--text-muted)' }}>{board.note || 'No board available.'}</p>

  const shown = pos === 'ALL' ? board.players : board.players.filter((p) => p.position === pos)
  const shape = board.shape || {}

  return (
    <>
      <Note>{board.scoring_warning}</Note>
      <Note>{board.missing_positions_note}</Note>

      <div className="tiles">
        <div className="card tile">
          <div className="label">Valued for</div>
          <div className="value" style={{ fontSize: 16 }}>
            {shape.name || `${shape.teams}-team league`}
          </div>
          <div className="sub">
            {Object.entries(shape.starters || {})
              .map(([k, v]) => `${v}${k}`)
              .join(' · ')}
            {shape.flex_slots ? ` · ${shape.flex_slots}FLEX` : ''}
            {shape.superflex_slots ? ` · ${shape.superflex_slots}SFLEX` : ''}
          </div>
        </div>
        {Object.entries(board.positions || {}).map(([p, lv]) => (
          <div className="card tile" key={p}>
            <div className="label">{p} replacement level</div>
            <div className="value">{lv.replacement_points}</div>
            <div className="sub">
              {lv.starters_league_wide} started league-wide · {lv.replacement_player}
            </div>
          </div>
        ))}
      </div>

      {shape.assumed && (
        <p style={{ fontSize: 12.5, color: 'var(--text-muted)', marginBottom: 10 }}>
          No league selected, so this is valued for a standard 12-team 1QB/2RB/3WR/1TE/1FLEX
          league. Pick one of your leagues above to re-value it for that league&rsquo;s real
          settings — superflex in particular changes the board completely.
        </p>
      )}

      <div className="controls" style={{ marginBottom: 12 }}>
        <div className="subtabs" style={{ marginBottom: 0 }}>
          {POSITIONS.map((p) => (
            <button key={p} className={pos === p ? 'on' : ''} onClick={() => setPos(p)}>
              {p}
            </button>
          ))}
        </div>
        <button className="ghost" onClick={() => load(true)} disabled={loading}>
          {loading ? 'Refreshing…' : 'Refresh'}
        </button>
      </div>

      <BoardTable players={shown} />
      <Sources board={board} />
    </>
  )
}

// ------------------------------------------------------------------ my league

function LeagueView({ leagueId, userId }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (!leagueId) return
    let live = true
    setLoading(true)
    setError(null)
    api
      .seasonLeague(leagueId, userId)
      .then((d) => live && setData(d))
      .catch((err) => live && setError(err.message))
      .finally(() => live && setLoading(false))
    return () => {
      live = false
    }
  }, [leagueId, userId])

  if (!leagueId)
    return (
      <p style={{ color: 'var(--text-muted)' }}>
        Connect Sleeper and pick a league to see it broken down.
      </p>
    )
  if (loading) return <p style={{ color: 'var(--text-muted)' }}>Reading the league…</p>
  if (error) return <p style={{ color: 'var(--critical-ink)' }}>{error}</p>
  if (!data) return null

  const me = data.me
  return (
    <>
      <Note>{data.scoring_warning}</Note>
      <Note>{data.missing_positions_note}</Note>

      {me && (
        <div className="tiles">
          <div className="card tile">
            <div className="label">Your team</div>
            <div className="value">#{me.power_rank}</div>
            <div className="sub">
              of {data.teams.length} on projected starting points ({me.starting_points})
            </div>
          </div>
          {['QB', 'RB', 'WR', 'TE'].map((p) => (
            <div className="card tile" key={p}>
              <div className="label">{p}</div>
              <div className="value">#{me.position_rank?.[p]}</div>
              <div className="sub">{me.by_position?.[p]} projected pts/wk from starters</div>
            </div>
          ))}
        </div>
      )}

      {me && Object.keys(me.bye_pileups || {}).length > 0 && (
        <Note>
          Bye pileups on your roster:{' '}
          {Object.entries(me.bye_pileups)
            .map(([wk, n]) => `week ${wk} (${n} starters)`)
            .join(', ')}
          .
        </Note>
      )}

      {me && me.injuries?.length > 0 && (
        <div className="card" style={{ marginBottom: 16 }}>
          <strong style={{ fontSize: 13 }}>Injury flags on your roster</strong>
          <div className="controls" style={{ marginTop: 8 }}>
            {me.injuries.map((i) => (
              <span className="badge risk" key={i.name}>
                {i.name} ({i.position}) {i.status}
              </span>
            ))}
          </div>
        </div>
      )}

      <h2 style={{ fontSize: 15, margin: '18px 0 8px' }}>Power ranking</h2>
      <div className="card table-wrap">
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>Manager</th>
              <th>Record</th>
              <th>Starting pts/wk</th>
              <th>QB</th>
              <th>RB</th>
              <th>WR</th>
              <th>TE</th>
              <th>Roster</th>
            </tr>
          </thead>
          <tbody>
            {data.teams.map((t) => (
              <tr key={t.roster_id} style={t.is_me ? { background: 'var(--edge-soft)' } : undefined}>
                <td>{t.power_rank}</td>
                <td>
                  {t.owner}
                  {t.is_me && ' (you)'}
                </td>
                <td>
                  {t.record.wins != null ? `${t.record.wins}-${t.record.losses}` : '—'}
                </td>
                <td>
                  <strong>{t.starting_points}</strong>
                </td>
                {['QB', 'RB', 'WR', 'TE'].map((p) => (
                  <td key={p}>
                    {t.by_position?.[p] ?? 0}{' '}
                    <span style={{ color: 'var(--text-muted)' }}>#{t.position_rank?.[p]}</span>
                  </td>
                ))}
                <td>
                  {t.valued_count}/{t.player_count} valued
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 8 }}>
        &ldquo;Valued&rdquo; counts the players on a roster that appear on the DraftKings board.
        Kickers, defenses and deep bench bodies are not on it, so they contribute nothing to these
        totals for anyone — the comparison stays fair, it is just not a complete roster value.
      </p>

      <h2 style={{ fontSize: 15, margin: '22px 0 8px' }}>Best available (unrostered)</h2>
      <BoardTable players={data.free_agents} limit={40} />
    </>
  )
}

// ------------------------------------------------------------------ live draft

function DraftView({ drafts, leagueId, userId, connected }) {
  const [draftId, setDraftId] = useState(drafts?.[0]?.draft_id || '')
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [autoPoll, setAutoPoll] = useState(true)
  const timer = useRef(null)

  const poll = useCallback(async () => {
    if (!draftId) return
    try {
      setData(await api.seasonDraft(draftId, userId, leagueId))
      setError(null)
    } catch (err) {
      setError(err.message)
    }
  }, [draftId, userId, leagueId])

  useEffect(() => {
    poll()
  }, [poll])

  useEffect(() => {
    // Sleeper publishes no push feed for drafts, so this polls. The
    // backend caches picks for 5s, which is what keeps a 6s timer from
    // turning into real request volume against Sleeper.
    if (!autoPoll || !draftId) return undefined
    timer.current = setInterval(poll, 6000)
    return () => clearInterval(timer.current)
  }, [autoPoll, draftId, poll])

  if (!connected)
    return (
      <p style={{ color: 'var(--text-muted)' }}>
        Connect Sleeper above to follow a draft.
      </p>
    )
  if (!drafts?.length)
    return (
      <p style={{ color: 'var(--text-muted)' }}>
        No drafts on this Sleeper account for the season yet. Start a mock draft on Sleeper and it
        will appear here — mocks are the way to rehearse this before the real thing.
      </p>
    )

  const st = data?.state
  return (
    <>
      <div className="controls" style={{ marginBottom: 14 }}>
        <select value={draftId} onChange={(e) => setDraftId(e.target.value)}>
          {drafts.map((d) => (
            <option key={d.draft_id} value={d.draft_id}>
              {d.status} · {d.type} · {d.teams} teams × {d.rounds} rounds · {d.draft_id}
            </option>
          ))}
        </select>
        <label style={{ fontSize: 13, display: 'flex', alignItems: 'center', gap: 6 }}>
          <input
            type="checkbox"
            checked={autoPoll}
            onChange={(e) => setAutoPoll(e.target.checked)}
          />
          Follow live
        </label>
        <button className="ghost" onClick={poll}>
          Refresh now
        </button>
      </div>

      {error && <p style={{ color: 'var(--critical-ink)' }}>{error}</p>}
      {!data && !error && <p style={{ color: 'var(--text-muted)' }}>Reading the draft…</p>}

      {st && (
        <div className="tiles">
          <div className="card tile">
            <div className="label">Status</div>
            <div className="value" style={{ fontSize: 18 }}>
              {st.status}
            </div>
            <div className="sub">
              round {st.current_round} of {st.rounds} · {st.picks_made} picks made
            </div>
          </div>
          <div className="card tile">
            <div className="label">Your next pick</div>
            <div className="value">{st.my_next_pick ?? '—'}</div>
            <div className="sub">
              {st.my_slot ? `slot ${st.my_slot}` : 'slot unknown'}
              {st.my_following_pick ? ` · then ${st.my_following_pick}` : ''}
            </div>
          </div>
          <div className="card tile">
            <div className="label">Picks until your turn</div>
            <div className="value">
              {st.on_the_clock_is_me ? "You're up" : (st.picks_until_my_turn ?? '—')}
            </div>
            <div className="sub">on the clock: pick {st.on_the_clock_pick}</div>
          </div>
        </div>
      )}

      {data && (
        <>
          <div className="controls" style={{ margin: '4px 0 12px' }}>
            {Object.entries(data.needs || {}).map(([p, v]) => (
              <span className="badge warn" key={p}>
                need {p} {Math.round(v * 100)}%
              </span>
            ))}
            {Object.entries(data.roster_counts || {}).map(([p, n]) => (
              <span className="badge" key={p}>
                {n}× {p}
              </span>
            ))}
          </div>

          <div className="card table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Take</th>
                  <th>Pos</th>
                  <th>Team</th>
                  <th>VORP</th>
                  <th title="VORP plus the value of filling a hole plus what waiting would cost">
                    Draft score
                  </th>
                  <th title="Chance he is gone before your next pick">Gone by then</th>
                  <th title="How much worse the player you would actually get instead is">
                    Cost of waiting
                  </th>
                  <th>Fallback</th>
                  <th>Bye</th>
                  <th>Notes</th>
                </tr>
              </thead>
              <tbody>
                {data.suggestions.map((s, i) => (
                  <tr key={`${s.dk_id}-${s.name}`}>
                    <td>
                      {i === 0 ? <strong>{s.name}</strong> : s.name}
                    </td>
                    <td>
                      {s.position} <span className="badge">T{s.tier}</span>
                    </td>
                    <td>{s.team}</td>
                    <td>{s.vorp?.toFixed(1)}</td>
                    <td>
                      <strong>{s.draft_score?.toFixed(1)}</strong>
                    </td>
                    <td>{Math.round((s.why?.chance_he_is_gone ?? 0) * 100)}%</td>
                    <td>{s.why?.value_lost_if_you_wait?.toFixed(1)}</td>
                    <td style={{ color: 'var(--text-muted)' }}>{s.why?.fallback_if_you_wait}</td>
                    <td>{s.bye_week ?? '—'}</td>
                    <td style={{ whiteSpace: 'normal', maxWidth: 320 }}>
                      {s.flags?.length
                        ? s.flags.map((f) => (
                            <span className="badge risk" key={f} style={{ marginRight: 4 }}>
                              {f}
                            </span>
                          ))
                        : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <p style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 10 }}>
            Draft score = value over replacement + what filling this hole is worth + what passing
            would cost you at the position. The last of those is measured concretely: it estimates
            how many at the position go before your next turn, looks up who you would actually be
            left with that far down, and takes the gap. This is advice, not an autodraft — nothing
            here can make a pick.
          </p>

          <h2 style={{ fontSize: 15, margin: '22px 0 8px' }}>Picks so far</h2>
          <div className="card table-wrap">
            <table>
              <thead>
                <tr>
                  <th>#</th>
                  <th>Rd</th>
                  <th>Player</th>
                  <th>Pos</th>
                  <th>Team</th>
                </tr>
              </thead>
              <tbody>
                {[...(st?.picks || [])].reverse().slice(0, 30).map((p) => (
                  <tr key={p.pick_no}>
                    <td>{p.pick_no}</td>
                    <td>{p.round}</td>
                    <td>{p.name?.trim() || '—'}</td>
                    <td>{p.position}</td>
                    <td>{p.team}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </>
  )
}

// ------------------------------------------------------------------ best ball

function BestBallView() {
  return (
    <>
      <div className="card" style={{ marginBottom: 16, fontSize: 13 }}>
        <strong>DraftKings Best Ball.</strong> Twenty rounds, no weekly lineups — DraftKings scores
        your best legal combination every week automatically. That makes bye weeks cost far less
        than in a redraft league and pushes the value toward raw upside over floor. The board below
        is DraftKings&rsquo; own live draft pool, valued for best ball&rsquo;s roster shape.
      </div>
      <BoardView bestBall />
    </>
  )
}

// ------------------------------------------------------------------ panel

const HEADINGS = {
  board: {
    title: 'Season-long draft board',
    blurb:
      'Every draftable player ranked by value over replacement for your league’s own settings, with the tier cliffs marked.',
  },
  league: {
    title: 'My league',
    blurb:
      'Every team valued on the same board: power ranking, where you are strong, where the holes are, and who is still unrostered.',
  },
  draft: {
    title: 'Live draft assistant',
    blurb:
      'Follows your Sleeper draft as it happens and says who to take next — and shows exactly why.',
  },
  bestball: {
    title: 'Best ball',
    blurb: 'The live DraftKings Best Ball pool, valued for the format’s own scoring and roster.',
  },
}

export function SeasonPanel({ tab, onTabChange, headerSlot }) {
  const [account, setAccount] = useState(loadSaved)
  const [leagueId, setLeagueId] = useState(loadSaved()?.selectedLeagueId || '')

  function connect(data) {
    const next = { ...data, selectedLeagueId: data.leagues?.[0]?.league_id || '' }
    setAccount(next)
    setLeagueId(next.selectedLeagueId)
    save(next)
  }

  function disconnect() {
    setAccount(null)
    setLeagueId('')
    save(null)
  }

  function chooseLeague(id) {
    setLeagueId(id)
    if (account) save({ ...account, selectedLeagueId: id })
  }

  const heading = HEADINGS[tab] || HEADINGS.board
  const userId = account?.user?.user_id
  const connected = Boolean(userId)
  // A draft attached to the selected league is the one that matters; a
  // mock draft on the account is still offered, since rehearsing is the
  // whole point of having this before draft night.
  const drafts = account?.drafts || []

  // Which league you're looking at is the Season section's equivalent
  // of MLB's date -- it re-frames every number on the page -- so it
  // belongs in the sticky header rather than in a card you scroll past.
  // The connect FORM stays in the body: it's a form, and it only shows
  // until you're connected.
  const header =
    headerSlot && tab !== 'bestball'
      ? createPortal(
          connected ? (
            <>
              {account.leagues?.length > 0 && (
                <label className="dim" style={{ fontSize: 12.5 }}>
                  League{' '}
                  <select value={leagueId} onChange={(e) => chooseLeague(e.target.value)}>
                    <option value="">(no league — standard 12-team value)</option>
                    {account.leagues.map((lg) => (
                      <option key={lg.league_id} value={lg.league_id}>
                        {lg.name} · {lg.total_rosters} teams · {lg.status}
                      </option>
                    ))}
                  </select>
                </label>
              )}

              <div className="kpis" aria-label="Sleeper account">
                <div className="kpi">
                  <b>{account.user.display_name || account.user.username}</b>
                  <span>sleeper</span>
                </div>
                <div className="kpi">
                  <b>{account.leagues?.length || 0}</b>
                  <span>leagues</span>
                </div>
                <div className="kpi">
                  <b>{account.drafts?.length || 0}</b>
                  <span>drafts</span>
                </div>
              </div>

              <div className="grow" />

              <div className="status">
                <span className="dot" />
                Connected
              </div>
              <button className="sm" onClick={disconnect}>
                Disconnect
              </button>
            </>
          ) : (
            <>
              <div className="status">
                <span className="dot warn" />
                Not connected
              </div>
              <span className="dim" style={{ fontSize: 12.5 }}>
                connect a Sleeper username below to see your own leagues and drafts
              </span>
              <div className="grow" />
            </>
          ),
          headerSlot,
        )
      : null

  return (
    <>
      {header}

      <div className="ph">
        <div>
          <h1>{heading.title}</h1>
          <p>{heading.blurb}</p>
        </div>
      </div>

      {tab !== 'bestball' && !connected && (
        <ConnectCard saved={account} onConnect={connect} onDisconnect={disconnect} />
      )}

      {tab === 'board' && <BoardView leagueId={leagueId} />}
      {tab === 'league' && <LeagueView leagueId={leagueId} userId={userId} />}
      {tab === 'draft' && (
        <DraftView
          drafts={drafts}
          leagueId={leagueId}
          userId={userId}
          connected={Boolean(userId)}
        />
      )}
      {tab === 'bestball' && <BestBallView />}
    </>
  )
}
