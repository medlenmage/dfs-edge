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

export const api = {
  health: () => request('/api/health'),

  slate: (date, { refresh = false } = {}) =>
    request(`/api/mlb/slate?date=${date}${refresh ? '&refresh=true' : ''}`),

  stacks: (date) => request(`/api/mlb/stacks?date=${date}`),

  hitters: (date, { limit = 60, minScore = 0 } = {}) =>
    request(`/api/mlb/hitters?date=${date}&limit=${limit}&min_score=${minScore}`),

  pitchers: (date) => request(`/api/mlb/pitchers?date=${date}`),

  injuries: (date) => request(`/api/mlb/injuries?date=${date}`),

  analysis: (date, { refresh = false } = {}) =>
    request(`/api/mlb/analysis?date=${date}${refresh ? '&refresh=true' : ''}`),

  ask: (date, question) =>
    request('/api/mlb/ask', {
      method: 'POST',
      body: JSON.stringify({ question, date }),
    }),

  clearCache: (prefix) =>
    request(`/api/cache/clear${prefix ? `?prefix=${prefix}` : ''}`, {
      method: 'POST',
    }),
}
