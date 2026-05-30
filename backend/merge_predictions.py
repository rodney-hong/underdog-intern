"""
merge_predictions.py
One-time manual script — run from the backend folder:
    python merge_predictions.py

Merges predictions from predictions_other.db into predictions.db.
- Inserts rows whose dedup key is not already in the destination.
- Fills in actual_result on existing rows where the destination has NULL
  and the source has a resolved value.
- Skips everything else.

Neither predictions_other.db nor the backup is deleted.
"""

import os
import shutil
import sqlite3

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
DEST_DB     = os.path.join(BACKEND_DIR, "predictions.db")
SRC_DB      = os.path.join(BACKEND_DIR, "predictions_other.db")
BACKUP      = os.path.join(BACKEND_DIR, "predictions_premerge_backup.db")

DEDUP_COLS  = ("player_name", "stat_type", "stat_line", "opponent_team", "game_date")


def main() -> None:
    # ------------------------------------------------------------------
    # Step 1 — Backup
    # ------------------------------------------------------------------
    shutil.copy(DEST_DB, BACKUP)
    print(f"Backup created: {BACKUP}")

    dest = sqlite3.connect(DEST_DB)
    dest.row_factory = sqlite3.Row
    src  = sqlite3.connect(SRC_DB)
    src.row_factory = sqlite3.Row

    try:
        # ------------------------------------------------------------------
        # Step 2 — Read destination columns dynamically (excluding id)
        # ------------------------------------------------------------------
        dest_cols = [
            row["name"]
            for row in dest.execute("PRAGMA table_info(predictions)").fetchall()
            if row["name"] != "id"
        ]

        src_col_names = {
            row["name"]
            for row in src.execute("PRAGMA table_info(predictions)").fetchall()
        }

        col_list     = ", ".join(dest_cols)
        placeholders = ", ".join("?" * len(dest_cols))
        insert_sql   = f"INSERT INTO predictions ({col_list}) VALUES ({placeholders})"

        # ------------------------------------------------------------------
        # Step 3 — Load existing dedup keys from destination
        # ------------------------------------------------------------------
        existing_keys: set[tuple] = set()
        for row in dest.execute(
            "SELECT player_name, stat_type, stat_line, opponent_team, game_date "
            "FROM predictions"
        ).fetchall():
            existing_keys.add((
                row["player_name"],
                row["stat_type"],
                row["stat_line"],
                row["opponent_team"],
                row["game_date"],
            ))

        # ------------------------------------------------------------------
        # Step 4 — Iterate source rows
        # ------------------------------------------------------------------
        inserted        = 0
        resolved_filled = 0
        skipped         = 0

        for row in src.execute("SELECT * FROM predictions").fetchall():
            key = (
                row["player_name"],
                row["stat_type"],
                row["stat_line"],
                row["opponent_team"],
                row["game_date"],
            )

            if key not in existing_keys:
                # Insert — map source values onto destination column list,
                # using None for any column the source table doesn't have.
                values = tuple(
                    row[col] if col in src_col_names else None
                    for col in dest_cols
                )
                dest.execute(insert_sql, values)
                existing_keys.add(key)
                inserted += 1

            else:
                # Key already exists — fill actual_result if destination is NULL
                # and source has a resolved value.
                incoming_result = row["actual_result"] if "actual_result" in src_col_names else None
                if incoming_result:
                    existing = dest.execute(
                        "SELECT id, actual_result FROM predictions "
                        "WHERE player_name = ? AND stat_type = ? AND stat_line = ? "
                        "  AND opponent_team = ? "
                        "  AND COALESCE(game_date, '') = COALESCE(?, '') "
                        "LIMIT 1",
                        key,
                    ).fetchone()
                    if existing and not existing["actual_result"]:
                        dest.execute(
                            "UPDATE predictions SET actual_result = ? WHERE id = ?",
                            (incoming_result, existing["id"]),
                        )
                        resolved_filled += 1
                    else:
                        skipped += 1
                else:
                    skipped += 1

        # ------------------------------------------------------------------
        # Step 5 — Commit and print summary
        # ------------------------------------------------------------------
        dest.commit()

        total = dest.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        date_row = dest.execute(
            "SELECT MIN(game_date), MAX(game_date) "
            "FROM predictions WHERE game_date IS NOT NULL"
        ).fetchone()

        print(
            f"Merge complete: {inserted:,} inserted, "
            f"{resolved_filled:,} resolved-filled, "
            f"{skipped:,} skipped"
        )
        print(f"Total rows in predictions.db: {total:,}")
        print(f"game_date range: {date_row[0]} → {date_row[1]}")

    finally:
        dest.close()
        src.close()


if __name__ == "__main__":
    main()
