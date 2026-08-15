import { ScoreMeter } from './ScoreMeter'
import { localTime } from '../format'

function TeamRow({ team, opponent, isHome }) {
  const p = opponent.probable_pitcher
  return (
    <div className="team-row">
      <div>
        <div className="tname">
          {team.name}
          {isHome ? <span className="dim" style={{ fontWeight: 400 }}> (home)</span> : null}
          {team.lineup_confirmed ? ' ✓' : ''}
        </div>
        <div className="pitcher">
          vs {p?.name || 'TBD'}
          {p?.throws ? ` (${p.throws}HP)` : ''}
          {p?.season?.era != null ? ` · ${p.season.era} ERA` : ''}
        </div>
        {team.injuries?.length > 0 && (
          <div className="sub-line">
            <span className="badge risk">{team.injuries.length} on IL</span>{' '}
            {team.injuries.slice(0, 3).map((i) => i.name).join(', ')}
            {team.injuries.length > 3 ? `, +${team.injuries.length - 3} more` : ''}
          </div>
        )}
      </div>
      <div className="runs">
        {team.implied_runs != null ? `${team.implied_runs.toFixed(1)} R` : '—'}
      </div>
      <ScoreMeter score={team.stack_score} />
    </div>
  )
}

export function GameCard({ game }) {
  const v = game.venue
  const w = game.weather || {}
  const b = game.betting || {}

  return (
    <div className="card">
      <div className="game-head">
        <div className="matchup">
          {game.away.abbrev} @ {game.home.abbrev}
        </div>
        <div className="time">{localTime(game.game_time_utc)}</div>
      </div>

      <div className="sub-line" style={{ marginBottom: 8 }}>{v.name}</div>

      <div className="env-row">
        {b.total != null && <span className="badge">O/U {b.total}</span>}
        <span className="badge">{v.park_factors.hr.toFixed(2)}× HR park</span>
        {v.elevation_ft >= 3000 && (
          <span className="badge warn">{v.elevation_ft.toLocaleString()} ft</span>
        )}
        {v.roof_closed ? (
          <span className="badge">roof closed</span>
        ) : (
          <>
            {w.temp_f != null && <span className="badge">{Math.round(w.temp_f)}°F</span>}
            {w.wind_effect?.label && w.wind_effect.label !== 'unknown' && (
              <span className="badge">
                wind {w.wind_effect.label} {w.wind_effect.speed_mph}mph
              </span>
            )}
            {w.precip_chance_pct >= 40 && (
              <span className="badge risk">{w.precip_chance_pct}% rain</span>
            )}
          </>
        )}
      </div>

      <TeamRow team={game.away} opponent={game.home} isHome={false} />
      <TeamRow team={game.home} opponent={game.away} isHome={true} />
    </div>
  )
}

export function GameGrid({ slate }) {
  const games = slate?.games || []
  if (!games.length) {
    return <div className="notice">{slate?.message || 'No games scheduled.'}</div>
  }
  return (
    <div className="games">
      {games.map((g) => (
        <GameCard key={g.game_pk} game={g} />
      ))}
    </div>
  )
}
