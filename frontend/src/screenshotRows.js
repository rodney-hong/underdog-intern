// Screenshot import: rows from POST /screenshot/extract, edited in the confirmation table,
// then sent one by one to the normal /predict. Nothing here predicts.

export const SCREENSHOT_STATS = ['Points', 'Rebounds', 'Assists', '3PM']

// Editable state for one extracted row (the resolved player is blank unless it matched).
export function toEditable(row, i) {
  return {
    key: i,
    card_name: row.player_name,
    player_name: row.resolved_player || '',
    stat_type: SCREENSHOT_STATS.includes(row.stat) ? row.stat : '',
    stat_line: row.line != null ? String(row.line) : '',
    opponent_team: row.opponent_team || '',
    is_home: !!row.is_home,
    is_back_to_back: !!row.is_back_to_back,
    game_date: row.game_date || null,
    higher: row.higher_multiplier ?? null,
    lower: row.lower_multiplier ?? null,
    flags: row.flags || [],
    alerts: row.alerts || [],          // two reads disagree / player not resolved: the flag icon's tooltip
    logged_line: row.logged_line ?? null,
  }
}

// Ready for /predict: a player, a supported stat, a positive line and an opponent.
export function isReady(r) {
  const line = parseFloat(r.stat_line)
  return !!r.player_name.trim() && SCREENSHOT_STATS.includes(r.stat_type) &&
    Number.isFinite(line) && line > 0 && !!r.opponent_team
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
  }
}
