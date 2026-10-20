// Per-player model track record (GET /players/{name}/track-record).
// Display-only: it is shown next to a prediction and never changes it.

const API = 'http://localhost:8000'
const cache = new Map()

export function fetchTrackRecord({ player, stat, league = 'NBA', before }) {
  const qs = new URLSearchParams({ stat, league })
  if (before != null) qs.set('before', String(before))
  const url = `${API}/players/${encodeURIComponent(player)}/track-record?${qs}`
  if (!cache.has(url)) {
    const req = fetch(url)
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
