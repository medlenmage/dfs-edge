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

  slate: (date, { refresh = false, inhouse = false } = {}) =>
    request(
      `/api/mlb/slate?date=${date}${refresh ? '&refresh=true' : ''}${inhouse ? '&inhouse=true' : ''}`,
    ),

  stacks: (date) => request(`/api/mlb/stacks?date=${date}`),

  hitters: (date, { limit = 60, minScore = 0 } = {}) =>
    request(`/api/mlb/hitters?date=${date}&limit=${limit}&min_score=${minScore}`),

  pitchers: (date) => request(`/api/mlb/pitchers?date=${date}`),

  injuries: (date) => request(`/api/mlb/injuries?date=${date}`),

  uploadProjections: (date, file) => uploadFile(`/api/mlb/projections?date=${date}`, file),

  refreshRotowireProjections: ({ refresh = false } = {}) =>
    request('/api/mlb/projections/refresh-rotowire', {
      method: 'POST',
      body: JSON.stringify({ refresh }),
    }),

  // Same as refreshRotowireProjections, but RotoWire's own "Early"
  // Classic slate (early-games-only) instead of their main "All" slate.
  refreshRotowireEarlyProjections: ({ refresh = false } = {}) =>
    request('/api/mlb/projections/refresh-rotowire-early', {
      method: 'POST',
      body: JSON.stringify({ refresh }),
    }),

  // Same as refreshRotowireProjections, but RotoWire's own "Afternoon"
  // Classic slate (afternoon-window games only) instead of their main
  // "All" slate.
  refreshRotowireAfternoonProjections: ({ refresh = false } = {}) =>
    request('/api/mlb/projections/refresh-rotowire-afternoon', {
      method: 'POST',
      body: JSON.stringify({ refresh }),
    }),

  dkSlates: (date, { refresh = false } = {}) =>
    request(`/api/mlb/dk-slates?date=${date}${refresh ? '&refresh=true' : ''}`),

  loadDkSlate: (date, draftGroupId, { refresh = false } = {}) =>
    request('/api/mlb/dk-slates/load', {
      method: 'POST',
      body: JSON.stringify({ date, draft_group_id: draftGroupId, refresh }),
    }),

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
      projectionSource = 'rotowire',
      stackGroups = null,
      stackTeams = null,
      maxExposurePct = null,
      exposureBySlot = null,
      teamExposureCap = null,
      lockedIds = null,
      excludedIds = null,
      // Defaults to $47,000 unless a caller explicitly overrides it
      // (pass 0 to disable the floor entirely) -- matches the HTTP
      // API's own default, so a caller that omits this still gets it.
      minSalary = 47000,
      maxSalary = null,
      minUniquePlayers = 1,
      minTeamsPerLineup = null,
      maxTeamsPerLineup = null,
      oneOffGroupIds = null,
      oneOffMinSalary = null,
      oneOffMaxSalary = null,
      minOwnershipPct = null,
      maxOwnershipPct = null,
      includedGamePks = null,
    } = {},
  ) =>
    request('/api/mlb/lineups', {
      method: 'POST',
      body: JSON.stringify({
        date,
        num_lineups: numLineups,
        projection_source: projectionSource,
        stack_groups: stackGroups,
        stack_teams: stackTeams,
        max_exposure_pct: maxExposurePct,
        exposure_by_slot: exposureBySlot,
        team_exposure_cap: teamExposureCap,
        locked_ids: lockedIds,
        excluded_ids: excludedIds,
        min_salary: minSalary,
        max_salary: maxSalary,
        min_unique_players: minUniquePlayers,
        min_teams_per_lineup: minTeamsPerLineup,
        max_teams_per_lineup: maxTeamsPerLineup,
        one_off_group_ids: oneOffGroupIds,
        one_off_min_salary: oneOffMinSalary,
        one_off_max_salary: oneOffMaxSalary,
        min_ownership_pct: minOwnershipPct,
        max_ownership_pct: maxOwnershipPct,
        included_game_pks: includedGamePks,
      }),
    }),

  // `picks` is exactly 10 {player_id, game_pk} entries in fixed roster
  // order (P, P, C, 1B, 2B, 3B, SS, OF, OF, OF) -- game_pk from the
  // SAME slate the lineup was built from, so the backend can tell
  // which of this lineup's players have a game that's already locked.
  lateSwap: (date, picks, { projectionSource = 'rotowire' } = {}) =>
    request('/api/mlb/late-swap', {
      method: 'POST',
      body: JSON.stringify({ date, picks, projection_source: projectionSource }),
    }),

  clearCache: (prefix) =>
    request(`/api/cache/clear${prefix ? `?prefix=${prefix}` : ''}`, {
      method: 'POST',
    }),

  contestTypes: () => request('/api/mlb/contest-types'),

  buildContestField: (
    date,
    contestType,
    lineups,
    {
      projectionSource = 'rotowire',
      fieldSize = null,
      sampleSize = null,
      includedGamePks = null,
      minSalary = 47000,
      maxSalary = 50000,
      fieldSharpness = 'marquee',
    } = {},
  ) =>
    request('/api/mlb/contest-field', {
      method: 'POST',
      body: JSON.stringify({
        date,
        contest_type: contestType,
        lineups,
        projection_source: projectionSource,
        field_size: fieldSize,
        sample_size: sampleSize,
        included_game_pks: includedGamePks,
        min_salary: minSalary,
        max_salary: maxSalary,
        field_sharpness: fieldSharpness,
      }),
    }),

  buildContestEntries: (
    date,
    contestType,
    numLineups,
    {
      projectionSource = 'rotowire',
      maxExposurePct = null,
      fieldSize = null,
      sampleSize = null,
      includedGamePks = null,
      minSalary = 47000,
      maxSalary = 50000,
      allowDuplicates = false,
      maxDuplicationRisk = null,
      fieldSharpness = 'marquee',
    } = {},
  ) =>
    request('/api/mlb/contest-entries', {
      method: 'POST',
      body: JSON.stringify({
        date,
        contest_type: contestType,
        num_lineups: numLineups,
        projection_source: projectionSource,
        max_exposure_pct: maxExposurePct,
        field_size: fieldSize,
        sample_size: sampleSize,
        included_game_pks: includedGamePks,
        min_salary: minSalary,
        max_salary: maxSalary,
        allow_duplicates: allowDuplicates,
        max_duplication_risk: maxDuplicationRisk,
        field_sharpness: fieldSharpness,
      }),
    }),

  // A plain URL, not a fetch() call -- the browser handles the
  // Content-Disposition: attachment download itself when this is set
  // as a link's href, no JS-side blob handling needed.
  contestEntriesCsvUrl: (batchId) => `${BASE}/api/mlb/contest-entries/${batchId}/csv`,

  // Entry Manager -- fills a real, already-uploaded DK bulk-entries
  // template with lineups from an already-built batch and returns the
  // completed CSV. A plain URL (like contestEntriesCsvUrl above), not a
  // fetch() call, so the browser handles the download itself.
  fillDkEntriesUrl: (date, contestId, batchId, onlyBlank = true) => {
    const params = new URLSearchParams({
      date,
      contest_id: contestId,
      batch_id: batchId,
      only_blank: onlyBlank,
    })
    return `${BASE}/api/mlb/dk-entries/fill?${params.toString()}`
  },

  // Re-rank/re-filter an already-simulated batch's real results -- no
  // new Monte Carlo run. playerExposureCaps/roiBoosts are plain
  // objects keyed by player id (as a string, JSON object keys always
  // are) -> number.
  reshapeContestEntries: (
    batchId,
    {
      targetCount = null,
      maxExposurePct = null,
      playerExposureCaps = null,
      roiBoosts = null,
      requireTeams = null,
      excludeTeams = null,
      requirePlayerIds = null,
      excludePlayerIds = null,
      stackTypes = null,
    } = {},
  ) =>
    request(`/api/mlb/contest-entries/${batchId}/reshape`, {
      method: 'POST',
      body: JSON.stringify({
        target_count: targetCount,
        max_exposure_pct: maxExposurePct,
        player_exposure_caps: playerExposureCaps,
        roi_boosts: roiBoosts,
        require_teams: requireTeams,
        exclude_teams: excludeTeams,
        require_player_ids: requirePlayerIds,
        exclude_player_ids: excludePlayerIds,
        stack_types: stackTypes,
      }),
    }),

  buildContestEntriesSimulated: (
    date,
    contestType,
    numLineups,
    {
      projectionSource = 'rotowire',
      maxExposurePct = null,
      fieldSize = null,
      sampleSize = null,
      includedGamePks = null,
      minSalary = 47000,
      maxSalary = 50000,
      allowDuplicates = false,
      maxDuplicationRisk = null,
      selfPlay = false,
      engine = 'bootstrap',
      fieldSharpness = 'marquee',
      firstPlacePct = null,
    } = {},
  ) =>
    request('/api/mlb/contest-entries-simulated', {
      method: 'POST',
      body: JSON.stringify({
        date,
        contest_type: contestType,
        num_lineups: numLineups,
        projection_source: projectionSource,
        max_exposure_pct: maxExposurePct,
        field_size: fieldSize,
        sample_size: sampleSize,
        included_game_pks: includedGamePks,
        min_salary: minSalary,
        max_salary: maxSalary,
        allow_duplicates: allowDuplicates,
        max_duplication_risk: maxDuplicationRisk,
        self_play: selfPlay,
        engine,
        field_sharpness: fieldSharpness,
        first_place_pct: firstPlacePct,
      }),
    }),

  uploadDkEntries: (date, file) => uploadFile(`/api/mlb/dk-entries?date=${date}`, file),

  // A real, completed DK contest's post-contest standings export (the
  // .zip DK gives you, or the .csv inside it) -- different from the
  // pre-contest salary CSV or the bulk-entries upload template.
  uploadContestResults: (
    date,
    file,
    { contestName = null, entryFee = null, myEntryId = null, myHandle = null } = {},
  ) => {
    const params = new URLSearchParams({ date })
    if (contestName) params.set('contest_name', contestName)
    if (entryFee != null) params.set('entry_fee', entryFee)
    if (myEntryId) params.set('my_entry_id', myEntryId)
    if (myHandle) params.set('my_handle', myHandle)
    return uploadFile(`/api/mlb/contest-results?${params.toString()}`, file)
  },

  contestResultsHistory: () => request('/api/mlb/contest-results/history'),

  simulateDkEntries: (
    date,
    contestId,
    {
      fieldSize,
      prizePool,
      firstPlacePct,
      payoutPct,
      shape,
      projectionSource = 'rotowire',
      sampleSize = null,
      includedGamePks = null,
      minSalary = 47000,
      maxSalary = 50000,
      engine = 'bootstrap',
      fieldSharpness = 'marquee',
    } = {},
  ) =>
    request('/api/mlb/dk-entries/simulate', {
      method: 'POST',
      body: JSON.stringify({
        date,
        contest_id: contestId,
        field_size: fieldSize,
        prize_pool: prizePool,
        first_place_pct: firstPlacePct,
        payout_pct: payoutPct,
        shape,
        projection_source: projectionSource,
        sample_size: sampleSize,
        included_game_pks: includedGamePks,
        engine,
        field_sharpness: fieldSharpness,
      }),
    }),

  nflSlate: (season, week, { inhouse = false } = {}) => {
    const params = new URLSearchParams()
    if (season) params.set('season', season)
    if (week) params.set('week', week)
    if (inhouse) params.set('include_inhouse', 'true')
    const qs = params.toString()
    return request(`/api/nfl/slate${qs ? `?${qs}` : ''}`)
  },

  nflStacks: (season, week) => {
    const params = new URLSearchParams()
    if (season) params.set('season', season)
    if (week) params.set('week', week)
    const qs = params.toString()
    return request(`/api/nfl/stacks${qs ? `?${qs}` : ''}`)
  },

  nflUploadSalaries: (season, week, file) =>
    uploadFile(`/api/nfl/salaries?season=${season}&week=${week}`, file),

  nflUploadProjections: (season, week, file) =>
    uploadFile(`/api/nfl/projections?season=${season}&week=${week}`, file),

  // Pulls RotoWire's own live NFL optimizer player pool directly -- no
  // manual CSV download/upload. Unlike the MLB version, this never
  // derives salaries from the same pull (RotoWire's NFL export has no
  // DK numeric player id) -- upload/refresh a real DK salary CSV separately.
  nflRefreshRotowireProjections: (season, week, { refresh = false } = {}) =>
    request(`/api/nfl/projections/refresh-rotowire?season=${season}&week=${week}`, {
      method: 'POST',
      body: JSON.stringify({ refresh }),
    }),

  nflGenerateLineups: (
    season,
    week,
    {
      numLineups = 1,
      maxExposurePct = null,
      exposureBySlot = null,
      lockedIds = null,
      excludedIds = null,
      minSalary = null,
      minUniquePlayers = 1,
      qbStackMin = 0,
      simulate = false,
      numTrials = 2000,
    } = {},
  ) =>
    request('/api/nfl/lineups', {
      method: 'POST',
      body: JSON.stringify({
        season,
        week,
        num_lineups: numLineups,
        max_exposure_pct: maxExposurePct,
        exposure_by_slot: exposureBySlot,
        locked_ids: lockedIds,
        excluded_ids: excludedIds,
        min_salary: minSalary,
        min_unique_players: minUniquePlayers,
        qb_stack_min: qbStackMin,
        simulate,
        num_trials: numTrials,
      }),
    }),

  nflContestTypes: () => request('/api/nfl/contest-types'),

  nflBuildContestEntriesSimulated: (
    season,
    week,
    {
      contestType,
      numLineups,
      maxExposurePct = null,
      fieldSize = null,
      sampleSize = null,
      minSalary = 0,
      maxSalary = 50000,
      allowDuplicates = false,
      selfPlay = false,
      fieldSharpness = 'marquee',
      firstPlacePct = null,
    },
  ) =>
    request('/api/nfl/contest-entries-simulated', {
      method: 'POST',
      body: JSON.stringify({
        season,
        week,
        contest_type: contestType,
        num_lineups: numLineups,
        max_exposure_pct: maxExposurePct,
        field_size: fieldSize,
        sample_size: sampleSize,
        min_salary: minSalary,
        max_salary: maxSalary,
        allow_duplicates: allowDuplicates,
        self_play: selfPlay,
        field_sharpness: fieldSharpness,
        first_place_pct: firstPlacePct,
      }),
    }),

  nflContestEntriesCsvUrl: (batchId) => `${BASE}/api/nfl/contest-entries/${batchId}/csv`,
}
