// History summary figures. Limited-data rows (is_limited_data: rookies, fewer than 10
// counted games before the game, or the fixed "Insufficient game log data" answer) and
// superseded rows (is_current = 0: an earlier pick for the same player, stat, game date
// and model) stay in the table but are left out of Total, Accuracy and the confidence
// buckets. DNP rows are never resolved picks.

export const CONFIDENCE_TIERS = [
  { label: '50–60%', min: 0.5, max: 0.6 },
  { label: '60–70%', min: 0.6, max: 0.7 },
  { label: '70%+',   min: 0.7, max: 1.01 },
]

// How many rows DELETE /predictions/duplicates would remove, from GET /predictions/history:
// the backend keeps the newest row (timestamp, then id) of each player_name, stat_type,
// stat_line, opponent_team, game_date (NULL as '') and model (NULL as 'frozen') group and deletes
// the rest (main.py delete_duplicate_predictions), so every group of n rows loses n - 1.
export function duplicateCount(predictions) {
  const groups = new Map()
  for (const p of predictions) {
    const key = JSON.stringify([p.player_name, p.stat_type, p.stat_line, p.opponent_team, p.game_date ?? '',
                                p.model ?? 'frozen'])
    groups.set(key, (groups.get(key) ?? 0) + 1)
  }
  let n = 0
  for (const c of groups.values()) n += c - 1
  return n
}

const isResolved = (p) => p.actual_result !== null && p.actual_result !== 'DNP'
const isCorrect = (p) => p.predicted_outcome === p.actual_result

export function historySummary(predictions) {
  const current = predictions.filter((p) => p.is_current !== 0)
  const counted = current.filter((p) => !p.is_limited_data)
  const resolved = counted.filter(isResolved)
  const correct = resolved.filter(isCorrect)
  return {
    total: counted.length,
    current: current.length,          // not replaced by a later pick (dimmed rows are the rest)
    all: predictions.length,          // every row the table can show
    limitedCount: current.length - counted.length,
    resolved: resolved.length,
    correct: correct.length,
    accuracy: resolved.length > 0 ? `${(correct.length / resolved.length * 100).toFixed(1)}%` : '—',
    tiers: CONFIDENCE_TIERS.map((tier) => {
      const t = resolved.filter((p) => p.confidence >= tier.min && p.confidence < tier.max)
      const c = t.filter(isCorrect)
      return {
        label: tier.label,
        value: t.length ? `${(c.length / t.length * 100).toFixed(0)}%` : '—',
        sub: `${t.length} game${t.length !== 1 ? 's' : ''}`,
      }
    }),
  }
}
