// Run: node --test src/historyStats.test.js (from frontend/). History summary figures
// leave out limited-data rows.
import test from 'node:test'
import assert from 'node:assert/strict'
import { historySummary } from './historyStats.js'

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
