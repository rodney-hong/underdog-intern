// Run: node --test src/screenshotRows.test.js (from frontend/).
import test from 'node:test'
import assert from 'node:assert/strict'
import { toEditable, isReady, predictBody } from './screenshotRows.js'

const extracted = {
  player_name: 'Giannis Antetokounmpo', resolved_player: 'Giannis Antetokounmpo', stat: 'Points',
  line: 27.5, higher_multiplier: 1.03, lower_multiplier: 0.88, opponent_team: 'Charlotte Hornets',
  is_home: false, is_back_to_back: true, game_date: '2026-10-24', flags: [],
}

test('a resolved row becomes the same body the manual form sends', () => {
  const r = toEditable(extracted, 0)
  assert.ok(isReady(r))
  assert.deepEqual(predictBody(r), {
    player_name: 'Giannis Antetokounmpo', opponent_team: 'Charlotte Hornets', stat_line: 27.5,
    stat_type: 'Points', is_home: false, is_back_to_back: true, game_date: '2026-10-24', league: 'NBA',
  })
})

test('unresolved player, unsupported stat or missing opponent is not ready', () => {
  assert.equal(isReady(toEditable({ ...extracted, resolved_player: '' }, 0)), false)
  assert.equal(isReady(toEditable({ ...extracted, stat: null }, 0)), false)
  assert.equal(isReady(toEditable({ ...extracted, opponent_team: '' }, 0)), false)
  assert.equal(isReady({ ...toEditable(extracted, 0), stat_line: '0' }), false)
})

test('edits are what gets predicted', () => {
  const r = { ...toEditable({ ...extracted, resolved_player: '' }, 0), player_name: ' Jalen Williams ', stat_line: '5.5' }
  assert.ok(isReady(r))
  assert.equal(predictBody(r).player_name, 'Jalen Williams')
  assert.equal(predictBody(r).stat_line, 5.5)
})

test('alerts (read disagreements, unresolved player) are kept for the flag tooltip', () => {
  const alerts = ['player not resolved: no exact roster match']
  assert.deepEqual(toEditable({ ...extracted, alerts }, 0).alerts, alerts)
  assert.deepEqual(toEditable(extracted, 0).alerts, [])
})
