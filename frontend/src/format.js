// Display formatting shared by the pages.

// predictions.timestamp is UTC written without a zone ("2026-10-25T19:39:00.253072"), which
// Date would read as local time: treat a zone-less timestamp as UTC.
export function parseUtc(ts) {
  if (!ts) return null
  const s = String(ts)
  const zoned = /([zZ]|[+-]\d\d:?\d\d)$/.test(s)
  const d = new Date(zoned ? s : `${s.replace(' ', 'T')}Z`)
  return Number.isNaN(d.getTime()) ? null : d
}

// "Oct 25, 2:39 PM CT" (America/Chicago, daylight saving included); the raw value if unparseable.
// suffix=false drops " CT" where a heading already says so (History's "Date (CT)" column).
export function formatCentral(ts, { suffix = true } = {}) {
  const d = parseUtc(ts)
  if (!d) return ts ?? ''
  const date = d.toLocaleDateString('en-US', { timeZone: 'America/Chicago', month: 'short', day: 'numeric' })
  const time = d.toLocaleTimeString('en-US', { timeZone: 'America/Chicago', hour: 'numeric', minute: '2-digit' })
  return `${date}, ${time}${suffix ? ' CT' : ''}`
}

// "Oct 25" for a game date (YYYY-MM-DD, a calendar date: no time zone), "—" when missing
export function formatGameDate(gameDate) {
  if (!gameDate) return '—'
  const [y, m, d] = gameDate.split('-').map(Number)
  return new Date(y, m - 1, d).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}
