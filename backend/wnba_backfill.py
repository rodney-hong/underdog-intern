"""
wnba_backfill.py — one-time WNBA historical backfill (run manually).

Loads WNBA box scores from ESPN's public API for every season from
_WNBA_BACKFILL_START_YEAR (2022) through the current year and stores them in
wnba_player_stats. Reuses the same ESPN-parsing helpers as the startup ETL in
database.py, so the two never drift.

Idempotent: games already present are skipped (dedupe by game_id), so it is safe
to re-run or to resume after an interruption — it will only fetch what's missing.

WARNING: a full first run takes 15–30 minutes due to ESPN rate-limit sleeping
(0.5 s per scoreboard day + 0.5 s per game summary).

Usage:  python wnba_backfill.py
"""

import sqlite3
from datetime import date

import database


def main() -> None:
    conn = sqlite3.connect(database.DB_PATH)
    database._wnba_ensure_table(conn)

    start = date(database._WNBA_BACKFILL_START_YEAR, *database._WNBA_SEASON_START_MD)
    today = date.today()

    print(f"WNBA backfill: scanning ESPN scoreboard {start} → {today} for game IDs…")
    print("  (~0.5 s per in-season day; several minutes)")
    game_ids = database._wnba_collect_game_ids(start, today, verbose=True)

    stored = database._wnba_stored_game_ids(conn)
    missing = len(game_ids - stored)
    print(f"Found {len(game_ids)} game IDs; {len(stored)} already stored, {missing} to fetch.")

    inserted = database._wnba_fetch_and_store(conn, game_ids, skip_ids=stored, verbose=True)

    final = conn.execute("SELECT COUNT(*) FROM wnba_player_stats").fetchone()[0]
    watermark = database._wnba_watermark(conn)
    conn.close()

    print(f"WNBA backfill complete: {inserted:,} new rows inserted, "
          f"{final:,} total, watermark now {watermark}.")


if __name__ == "__main__":
    main()
