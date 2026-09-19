// The control API is a separate service. Everything the interface knows about
// the engine goes through these calls.

async function request(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  const text = await response.text()
  const body = text ? JSON.parse(text) : null
  if (!response.ok) {
    const detail = body?.detail
    throw new Error(
      typeof detail === 'string' ? detail : JSON.stringify(detail ?? 'Request failed'),
    )
  }
  return body
}

export const api = {
  health: () => request('/health'),
  settings: () => request('/settings'),
  updateSettings: (settings) =>
    request('/settings', { method: 'PUT', body: JSON.stringify(settings) }),
  preview: (instruction) =>
    request('/policy/preview', { method: 'POST', body: JSON.stringify({ instruction }) }),
  draft: (instruction) =>
    request('/policy/draft', { method: 'POST', body: JSON.stringify({ instruction }) }),
  refine: (answers) =>
    request('/policy/refine', { method: 'POST', body: JSON.stringify({ answers }) }),
  confirm: () =>
    request('/policy/confirm', { method: 'POST', body: JSON.stringify({ confirmed: true }) }),
  policy: () => request('/policy'),
  tighten: (payload) =>
    request('/policy/tighten', { method: 'POST', body: JSON.stringify(payload) }),
  revoke: () => request('/policy', { method: 'DELETE' }),
  // POST starts a scenario. GET reads the one already run — they are not
  // interchangeable: re-posting replays every purchase and refills the inbox.
  run: (scenarioId) => request(`/runs/${scenarioId}`, { method: 'POST' }),
  readRun: (scenarioId) => request(`/runs/${scenarioId}`),
  pending: () => request('/pending'),
  resolve: (authorizationId, decision, message = '') =>
    request(`/pending/${authorizationId}/resolve`, {
      method: 'POST',
      body: JSON.stringify({ decision, message }),
    }),
  reset: () => request('/session/reset', { method: 'POST' }),
}
