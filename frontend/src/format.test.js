import { test } from 'node:test'
import assert from 'node:assert/strict'
import { formatCentral, formatGameDate, parseUtc } from './format.js'

test('a zone-less backend timestamp is UTC, shown in Central time', () => {
  // 19:39 UTC on Oct 25 2026 is 14:39 CDT (UTC-5)
  assert.equal(formatCentral('2026-10-25T19:39:00.253072'), 'Oct 25, 2:39 PM CT')
  // after the switch to standard time (Nov 1 2026): UTC-6
  assert.equal(formatCentral('2026-11-20T03:05:00'), 'Nov 19, 9:05 PM CT')
  assert.equal(formatCentral('2026-11-20T03:05:00', { suffix: false }), 'Nov 19, 9:05 PM')
})

test('timestamps that carry a zone are respected', () => {
  assert.equal(parseUtc('2026-10-25T19:39:00Z').toISOString(), '2026-10-25T19:39:00.000Z')
  assert.equal(parseUtc('2026-10-25T14:39:00-05:00').toISOString(), '2026-10-25T19:39:00.000Z')
})

test('bad or missing values never throw', () => {
  assert.equal(formatCentral('not a date'), 'not a date')
  assert.equal(formatCentral(null), '')
  assert.equal(formatGameDate(null), '—')
  assert.equal(formatGameDate('2026-10-25'), 'Oct 25')
})
