// Run: node --test src/historyStats.test.js (from frontend/). History summary figures
// leave out limited-data rows.
import test from 'node:test'
import assert from 'node:assert/strict'
import { duplicateCount, historySummary } from './historyStats.js'

const row = (confidence, predicted, actual, limited = 0) =>
  ({ confidence, predicted_outcome: predicted, actual_result: actual, is_limited_data: limited })

test('limited-data rows are excluded from Total, Accuracy and every bucket', () => {
  const s = historySummary([
    row(0.55, 'OVER', 'OVER'),
    row(0.65, 'OVER', 'UNDER'),
    row(0.75, 'UNDER', 'UNDER'),
    row(0.75, 'OVER', 'UNDER', 1),    // rookie: wrong, high confidence
    row(0.55, 'OVER', 'UNDER', 1),
    row(0.62, 'OVER', null, 1),       // pending rookie
  ])
  assert.equal(s.total, 3)
  assert.equal(s.limitedCount, 3)
  assert.equal(s.resolved, 3)
  assert.equal(s.accuracy, '66.7%')
  assert.deepEqual(s.tiers.map((t) => [t.value, t.sub]),
    [['100%', '1 game'], ['0%', '1 game'], ['100%', '1 game']])
})

test('DNP and pending rows count in Total but not as resolved picks', () => {
  const s = historySummary([row(0.6, 'OVER', 'DNP'), row(0.6, 'OVER', null), row(0.6, 'OVER', 'OVER')])
  assert.equal(s.total, 3)
  assert.equal(s.resolved, 1)
  assert.equal(s.limitedCount, 0)
  assert.equal(s.accuracy, '100.0%')
})

test('rows without the flag (null) are counted; no resolved rows gives dashes', () => {
  const s = historySummary([{ ...row(0.7, 'OVER', null), is_limited_data: null }])
  assert.equal(s.total, 1)
  assert.equal(s.accuracy, '—')
  assert.ok(s.tiers.every((t) => t.value === '—'))
})

test('superseded rows are excluded from Total, Accuracy and every bucket', () => {
  const sup = (c, pr, a) => ({ ...row(c, pr, a), is_current: 0 })
  const s = historySummary([
    sup(0.55, 'OVER', 'UNDER'),       // earlier pick, same player/stat/game/model
    { ...row(0.65, 'UNDER', 'UNDER'), is_current: 1 },
    row(0.75, 'OVER', 'OVER'),        // rows without the field count as current
  ])
  assert.equal(s.total, 2)
  assert.equal(s.resolved, 2)
  assert.equal(s.accuracy, '100.0%')
  assert.deepEqual(s.tiers.map((t) => t.sub), ['0 games', '1 game', '1 game'])
  assert.equal(s.limitedCount, 0)
})

test('duplicateCount follows the backend rule: n rows per group lose n - 1', () => {
  const p = (player, line, extra = {}) => ({ player_name: player, stat_type: 'Points', stat_line: line,
    opponent_team: 'Utah Jazz', game_date: '2026-10-25', model: 'challenger', ...extra })
  assert.equal(duplicateCount([]), 0)
  assert.equal(duplicateCount([p('A', 20.5), p('A', 21.5), p('B', 20.5)]), 0)
  assert.equal(duplicateCount([p('A', 20.5), p('A', 20.5), p('A', 20.5), p('B', 9.5), p('B', 9.5)]), 3)
  // a challenger row never removes a frozen one; NULL model is 'frozen', NULL game date is ''
  assert.equal(duplicateCount([p('A', 20.5), p('A', 20.5, { model: 'frozen' })]), 0)
  assert.equal(duplicateCount([p('A', 20.5, { model: null }), p('A', 20.5, { model: 'frozen' })]), 1)
  assert.equal(duplicateCount([p('A', 20.5, { game_date: null }), p('A', 20.5, { game_date: null })]), 1)
  assert.equal(duplicateCount([p('A', 20.5, { game_date: null }), p('A', 20.5)]), 0)
})

test('the summary separates current picks from every row', () => {
  const s = historySummary([{ ...row(0.6, 'OVER', null), is_current: 1 }, { ...row(0.6, 'OVER', null), is_current: 0 },
                            { ...row(0.6, 'OVER', null), is_current: 1, is_limited_data: 1 }])
  assert.deepEqual([s.total, s.current, s.all], [1, 2, 3])
})
