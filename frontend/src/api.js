/**
 * Every call to the Python backend goes through here.
 *
 * In development Vite proxies /api to http://127.0.0.1:8000 (see
 * vite.config.js), so there is nothing to configure.
 */

const BASE = import.meta.env.VITE_API_BASE || ''

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const body = await res.json()
      detail = body.detail || detail
    } catch {
      /* response wasn't JSON - keep the status code */
    }
    throw new Error(detail)
  }
  return res.json()
}

// Not JSON -- multipart file upload, so this bypasses request()'s default
// Content-Type (the browser sets the multipart boundary itself).
async function uploadFile(path, file) {
  const body = new FormData()
  body.append('file', file)
  const res = await fetch(`${BASE}${path}`, { method: 'POST', body })
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      detail = (await res.json()).detail || detail
    } catch {
      /* response wasn't JSON - keep the status code */
    }
    throw new Error(detail)
  }
  return res.json()
}

export const api = {
  health: () => request('/api/health'),

  slate: (date, { refresh = false } = {}) =>
    request(`/api/mlb/slate?date=${date}${refresh ? '&refresh=true' : ''}`),

  stacks: (date) => request(`/api/mlb/stacks?date=${date}`),

  hitters: (date, { limit = 60, minScore = 0 } = {}) =>
    request(`/api/mlb/hitters?date=${date}&limit=${limit}&min_score=${minScore}`),

  pitchers: (date) => request(`/api/mlb/pitchers?date=${date}`),

  injuries: (date) => request(`/api/mlb/injuries?date=${date}`),

  uploadSalaries: (date, file) => uploadFile(`/api/mlb/salaries?date=${date}`, file),

  uploadProjections: (date, file) => uploadFile(`/api/mlb/projections?date=${date}`, file),

  analysis: (date, { refresh = false } = {}) =>
    request(`/api/mlb/analysis?date=${date}${refresh ? '&refresh=true' : ''}`),

  ask: (date, question) =>
    request('/api/mlb/ask', {
      method: 'POST',
      body: JSON.stringify({ question, date }),
    }),

  generateLineups: (
    date,
    {
      numLineups = 1,
      stackGroups = null,
      stackTeams = null,
      maxExposurePct = null,
      exposureBySlot = null,
      teamExposureCap = null,
      lockedIds = null,
      excludedIds = null,
      minSalary = null,
      minUniquePlayers = 1,
      minTeamsPerLineup = null,
      maxTeamsPerLineup = null,
    } = {},
  ) =>
    request('/api/mlb/lineups', {
      method: 'POST',
      body: JSON.stringify({
        date,
        num_lineups: numLineups,
        stack_groups: stackGroups,
        stack_teams: stackTeams,
        max_exposure_pct: maxExposurePct,
        exposure_by_slot: exposureBySlot,
        team_exposure_cap: teamExposureCap,
        locked_ids: lockedIds,
        excluded_ids: excludedIds,
        min_salary: minSalary,
        min_unique_players: minUniquePlayers,
        min_teams_per_lineup: minTeamsPerLineup,
        max_teams_per_lineup: maxTeamsPerLineup,
      }),
    }),

  clearCache: (prefix) =>
    request(`/api/cache/clear${prefix ? `?prefix=${prefix}` : ''}`, {
      method: 'POST',
    }),
}
