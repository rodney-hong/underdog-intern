// Per-player model track record (GET /players/{name}/track-record).
// Display-only: it is shown next to a prediction and never changes it.

import { apiFetch } from './api'

const cache = new Map()

// model: challenger | frozen | challenger_retro | all (challenger + frozen), as History's filter
export function fetchTrackRecord({ player, stat, league = 'NBA', before, model = 'all' }) {
  const qs = new URLSearchParams({ stat, league, model })
  if (before != null) qs.set('before', String(before))
  const url = `/players/${encodeURIComponent(player)}/track-record?${qs}`
  if (!cache.has(url)) {
    const req = apiFetch(url)
      .then((r) => {
        if (!r.ok) throw new Error(`track record ${r.status}`)
        return r.json()
      })
      .catch((e) => {
        cache.delete(url)   // don't cache failures
        throw e
      })
    cache.set(url, req)
  }
  return cache.get(url)
}

export const pct = (x) => (x == null ? '—' : `${Math.round(x * 100)}%`)

// 95% Wilson interval on the raw hit rate, e.g. "41–63%"
export const rangeStr = (tr) =>
  tr?.raw_rate_ci95
    ? `${Math.round(tr.raw_rate_ci95[0] * 100)}–${Math.round(tr.raw_rate_ci95[1] * 100)}%`
    : '—'

// History's model filter label -> the track record's model parameter
export const TRACK_MODEL = { Challenger: 'challenger', Frozen: 'frozen', 'Challenger retro': 'challenger_retro',
                             All: 'all' }
