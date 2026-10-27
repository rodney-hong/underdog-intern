// Run: node --test src/screenshotRows.test.js (from frontend/).
import test from 'node:test'
import assert from 'node:assert/strict'
import { toEditable, isReady, predictBody, selectPlayer, typePlayer, unresolvedNote, opponentPlaceholder } from './screenshotRows.js'

const extracted = {
  player_name: 'Giannis Antetokounmpo', resolved_player: 'Giannis Antetokounmpo', stat: 'Points',
  line: 27.5, higher_multiplier: 1.03, lower_multiplier: 0.88, opponent_team: 'Charlotte Hornets',
  is_home: false, is_back_to_back: true, game_date: '2026-10-24', flags: [], match: 'ok', person_id: 203507,
}
const unresolved = { ...extracted, resolved_player: '', match: 'not_found', person_id: null,
                     opponent_team: '', is_home: false, is_back_to_back: false, game_date: null }

test('a resolved row becomes the same body the manual form sends', () => {
  const r = toEditable(extracted, 0)
  assert.ok(isReady(r))
  assert.deepEqual(predictBody(r), {
    player_name: 'Giannis Antetokounmpo', opponent_team: 'Charlotte Hornets', stat_line: 27.5,
    stat_type: 'Points', is_home: false, is_back_to_back: true, game_date: '2026-10-24', league: 'NBA',
    person_id: 203507,
  })
})

test('unresolved player, unsupported stat or missing opponent is not ready', () => {
  assert.equal(isReady(toEditable({ ...extracted, resolved_player: '' }, 0)), false)
  assert.equal(isReady(toEditable({ ...extracted, stat: null }, 0)), false)
  assert.equal(isReady(toEditable({ ...extracted, opponent_team: '' }, 0)), false)
  assert.equal(isReady({ ...toEditable(extracted, 0), stat_line: '0' }), false)
})

test('an unresolved row is ready only once a player is picked in its search', () => {
  const typed = typePlayer({ ...toEditable(unresolved, 0) }, 'Luka Don')
  assert.equal(isReady(typed), false)
  const team = { player_team: { full: 'Los Angeles Lakers' }, next_opponent: { full: 'Utah Jazz' },
                 is_home: false, is_back_to_back: true, next_game_date: '2026-10-25' }
  const r = selectPlayer(typed, 'Luka Doncic', { name: 'Luka Doncic', person_id: 1629029, team: 'LAL' }, team)
  assert.ok(isReady(r))
  assert.deepEqual(predictBody(r), {
    player_name: 'Luka Doncic', opponent_team: 'Utah Jazz', stat_line: 27.5, stat_type: 'Points',
    is_home: false, is_back_to_back: true, game_date: '2026-10-25', league: 'NBA', person_id: 1629029,
  })
  assert.equal(isReady(typePlayer(r, 'Luka')), false)        // typing again un-picks
})

test('a resolved row keeps edits to line and stat', () => {
  const r = { ...toEditable(extracted, 0), stat_line: '5.5' }
  assert.ok(isReady(r))
  assert.equal(predictBody(r).stat_line, 5.5)
})

test('alerts (read disagreements, unresolved player) are kept for the flag tooltip', () => {
  const alerts = ['player not resolved: no exact roster match']
  assert.deepEqual(toEditable({ ...extracted, alerts }, 0).alerts, alerts)
  assert.deepEqual(toEditable(extracted, 0).alerts, [])
})

test('the no-history notice is hidden; other flags stay', () => {
  const range = 'line 75.5 is far outside the range 18–44 (last 82 games)'
  assert.deepEqual(toEditable({ ...extracted, flags: ['no game history for this stat'] }, 0).flags, [])
  assert.deepEqual(toEditable({ ...extracted, flags: ['no game history for this stat', range] }, 0).flags, [range])
  assert.deepEqual(toEditable({ ...extracted, flags: undefined }, 0).flags, [])
})

test('an unresolved row says what it needs until a player is picked', () => {
  const r = toEditable(unresolved, 0)
  assert.equal(unresolvedNote(r), 'Player not found: pick from the search')
  assert.equal(opponentPlaceholder(r), 'Opponent: pick the player first')
  assert.equal(unresolvedNote(toEditable({ ...unresolved, match: 'ambiguous' }, 0)),
               'Several players match: pick the right one from the search')
  assert.equal(unresolvedNote(toEditable({ ...unresolved, match: 'no_game_header' }, 0)),
               'Couldn’t read the game’s two teams: pick from the search')
  assert.equal(unresolvedNote(toEditable({ ...unresolved, match: 'game_mismatch' }, 0)),
               'Not his next scheduled game (another day’s screenshot?): pick from the search')
  const picked = selectPlayer(r, 'Giannis Antetokounmpo', { person_id: 203507 }, null)
  assert.equal(unresolvedNote(picked), null)
  assert.equal(opponentPlaceholder(picked), 'Choose the opponent')
  assert.equal(unresolvedNote(toEditable(extracted, 0)), null)
})
