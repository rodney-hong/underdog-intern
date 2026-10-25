// Screenshot import: rows from POST /screenshot/extract, edited in the confirmation table,
// then sent one by one to the normal /predict. Nothing here predicts.

export const SCREENSHOT_STATS = ['Points', 'Rebounds', 'Assists', '3PM']

// Server notices the table doesn't show: a player with no history gets "Not enough NBA games"
// as the row's result after Predict.
const HIDDEN_FLAGS = new Set(['no game history for this stat'])

// Editable state for one extracted row (the resolved player is blank unless it matched).
// matched: the server resolved the card to one roster player, or the user picked one in the
// row's player search; person_id is that player's personId (sent to /predict).
export function toEditable(row, i) {
  return {
    key: i,
    card_name: row.player_name,
    player_name: row.resolved_player || '',
    matched: row.match === 'ok',
    searchable: row.match !== 'ok',    // unresolved card: the player cell is a player search
    person_id: row.person_id ?? null,
    stat_type: SCREENSHOT_STATS.includes(row.stat) ? row.stat : '',
    stat_line: row.line != null ? String(row.line) : '',
    opponent_team: row.opponent_team || '',
    is_home: !!row.is_home,
    is_back_to_back: !!row.is_back_to_back,
    game_date: row.game_date || null,
    higher: row.higher_multiplier ?? null,
    lower: row.lower_multiplier ?? null,
    flags: (row.flags || []).filter((f) => !HIDDEN_FLAGS.has(f)),
    alerts: row.alerts || [],          // two reads disagree / player not resolved: the flag icon's tooltip
    logged_line: row.logged_line ?? null,
  }
}

// Ready for /predict: a matched player, a supported stat, a positive line and an opponent.
export function isReady(r) {
  const line = parseFloat(r.stat_line)
  return !!r.matched && !!r.player_name.trim() && SCREENSHOT_STATS.includes(r.stat_type) &&
    Number.isFinite(line) && line > 0 && !!r.opponent_team
}

// A player picked in an unresolved row's search: suggestion = {name, person_id, team} from
// /players/search, team = /players/{name}/team (what the manual form reads). Fills opponent,
// home, back-to-back and game date the way the server fills a resolved row.
export function selectPlayer(r, name, suggestion, team) {
  return {
    ...r,
    player_name: name,
    matched: true,
    person_id: suggestion?.person_id ?? null,
    opponent_team: team?.next_opponent?.full || '',
    is_home: !!team?.is_home,
    is_back_to_back: !!team?.is_back_to_back,
    game_date: team?.next_game_date || null,
  }
}

// Typing in an unresolved row's search: no player chosen yet.
export function typePlayer(r, text) {
  return { ...r, player_name: text, matched: false, person_id: null }
}

// The same body the manual Predict form sends.
export function predictBody(r) {
  return {
    player_name: r.player_name.trim(),
    opponent_team: r.opponent_team,
    stat_line: parseFloat(r.stat_line),
    stat_type: r.stat_type,
    is_home: r.is_home,
    is_back_to_back: r.is_back_to_back,
    game_date: r.game_date || null,
    league: 'NBA',
    person_id: r.person_id ?? null,
  }
}
