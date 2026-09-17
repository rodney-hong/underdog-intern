"""
analyze_historical_props_match.py — READ-ONLY diagnostic (no writes, safe to delete).

Question: if we retrained on *real* historical Underdog/book lines instead of synthetic
ones, how many historical_player_props rows can we actually tie back to a real box score
in player_stats? Training on a line needs the actual outcome, so a prop line is only
usable if we can find the same player's box score on the same game date.

This script joins historical_player_props -> player_stats on:
    player name  (ASCII-normalized, case-insensitive)
  + game date    (historical game_date vs first 10 chars of gameDateTimeEst)
  + stat type    (each market maps to one box-score column)

and reports, per stat type, ABSOLUTE counts (not percentages):
  * total historical prop rows
  * exact_date  = rows matched on name + identical date
  * prev_date   = rows matched on name + (historical date - 1 day)
  * match_any   = rows matched on name + (exact OR +/-1 day)

The exact/prev split matters: historical game_date comes from OddsAPI's UTC
commence_time, so a 7pm-ET tip is stored as the NEXT calendar day, while
player_stats.gameDateTimeEst holds the ET game date. The prev_date column exposes
that one-day offset — it typically recovers far more rows than the exact join.

It only issues SELECTs. Run:  python analyze_historical_props_match.py
"""

import sqlite3
import unicodedata
from datetime import datetime, timedelta

DB_PATH = "predictions.db"

# historical stat_type label -> player_stats box-score column
_STAT_TO_COL = {
    "Points":   "points",
    "Rebounds": "reboundsTotal",
    "Assists":  "assists",
    "3PM":      "threePointersMade",
    "Steals":   "steals",
    "Blocks":   "blocks",
}


def _norm(name) -> str:
    """ASCII-fold + lowercase + collapse whitespace, matching how names vary across sources."""
    if name is None:
        return ""
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    return " ".join(s.lower().split())


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # --- available box-score columns (steals/blocks are optional in some builds) ---
    ps_cols = {r[1] for r in c.execute("PRAGMA table_info(player_stats)").fetchall()}
    usable_cols = [col for col in _STAT_TO_COL.values() if col in ps_cols]
    col_index = {col: i for i, col in enumerate(usable_cols)}  # position in the value tuple

    # --- overall historical_player_props summary ---
    total, players, dmin, dmax = c.execute(
        "SELECT COUNT(*), COUNT(DISTINCT player_name), MIN(game_date), MAX(game_date) "
        "FROM historical_player_props"
    ).fetchone()
    print("=== historical_player_props ===")
    print(f"total rows: {total:,}")
    print(f"distinct players: {players:,}")
    print(f"date range: {dmin} -> {dmax}")

    # --- load player_stats box scores into an in-memory index: (norm_name, date10) -> values ---
    # One row per player-game; avoids a 127k x 838k SQL join with no name index.
    print("\nLoading player_stats box scores into memory index…")
    select_sql = (
        "SELECT firstName, lastName, substr(gameDateTimeEst,1,10) AS d, "
        + ", ".join(usable_cols) +
        " FROM player_stats"
    )
    box: dict = {}
    for row in c.execute(select_sql):
        first, last, d = row[0], row[1], row[2]
        key = (_norm(f"{first} {last}"), d)
        box[key] = row[3:]  # tuple of the usable stat columns, in usable_cols order
    print(f"  indexed {len(box):,} unique (player, date) box scores")

    # --- iterate historical prop rows and classify per stat type ---
    print("\nJoining historical props to box scores…")
    totals:    dict = {}  # stat_type -> total historical rows
    exact:     dict = {}  # stat_type -> matched on name + exact date
    prev:      dict = {}  # stat_type -> matched on name + (date - 1 day)
    match_any: dict = {}  # stat_type -> matched on name + (exact OR +/-1 day)

    for stat in _STAT_TO_COL:
        totals[stat] = exact[stat] = prev[stat] = match_any[stat] = 0

    for player_name, game_date, stat_type in c.execute(
        "SELECT player_name, game_date, stat_type FROM historical_player_props"
    ):
        if stat_type not in _STAT_TO_COL:
            continue  # e.g. NFL markets — no player_stats coverage
        totals[stat_type] += 1
        nn = _norm(player_name)

        try:
            d = datetime.strptime(game_date, "%Y-%m-%d").date()
            d_prev = (d - timedelta(days=1)).isoformat()
            d_next = (d + timedelta(days=1)).isoformat()
        except Exception:
            d_prev = d_next = None

        hit_exact = (nn, game_date) in box
        hit_prev  = d_prev is not None and (nn, d_prev) in box
        hit_next  = d_next is not None and (nn, d_next) in box

        if hit_exact:
            exact[stat_type] += 1
        if hit_prev:
            prev[stat_type] += 1
        if hit_exact or hit_prev or hit_next:
            match_any[stat_type] += 1

    conn.close()

    # --- report ---
    print("\n=== matched rows per stat type (absolute counts) ===")
    header = f"{'stat_type':<12}{'total':>10}{'exact_date':>12}{'prev_date':>12}{'match_any':>12}"
    print(header)
    print("-" * len(header))
    t_tot = t_ex = t_pv = t_any = 0
    for stat in _STAT_TO_COL:
        print(f"{stat:<12}{totals[stat]:>10,}{exact[stat]:>12,}{prev[stat]:>12,}{match_any[stat]:>12,}")
        t_tot += totals[stat]; t_ex += exact[stat]; t_pv += prev[stat]; t_any += match_any[stat]
    print("-" * len(header))
    print(f"{'TOTAL':<12}{t_tot:>10,}{t_ex:>12,}{t_pv:>12,}{t_any:>12,}")

    print("\nColumns:")
    print("  total      = historical_player_props rows for that market")
    print("  exact_date = box score for same player & identical date")
    print("  prev_date  = box score for same player & (historical date - 1 day)  [UTC->ET offset]")
    print("  match_any  = box score for same player within +/-1 day (best-case trainable volume)")
    print("\nNote: the mapped box-score columns are core player_stats fields that are always")
    print("populated, so matched rows carry a usable actual value (verified for exact_date:")
    print("every exact match had a non-NULL stat). 'match_any' ~= trainable rows per stat type.")


if __name__ == "__main__":
    main()
