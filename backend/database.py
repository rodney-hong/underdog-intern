import sqlite3
import os
import pandas as pd
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "predictions.db")

# Columns to load from each CSV (subset of available columns)
_STATS_COLS = [
    "firstName", "lastName", "personId", "gameId", "gameDateTimeEst",
    "gameType", "home", "playerteamId", "playerteamName", "playerteamCity",
    "opponentteamId", "opponentteamName", "opponentteamCity",
    "numMinutes", "points", "assists", "reboundsTotal", "reboundsOffensive",
    "reboundsDefensive", "threePointersMade", "plusMinusPoints", "win",
    "estimatedPace", "usagePercentage", "defensiveRating",
    "blocks", "steals", "turnovers", "doubleDouble", "threePointersAttempted",
]
_SCHEDULE_COLS = [
    "gameId", "gameDateTimeEst", "homeTeamId", "awayTeamId",
    "homeTeamName", "homeTeamCity", "awayTeamName", "awayTeamCity",
]


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            player_name TEXT NOT NULL,
            stat_type TEXT NOT NULL,
            stat_line REAL NOT NULL,
            opponent_team TEXT NOT NULL,
            predicted_outcome TEXT NOT NULL,
            confidence REAL NOT NULL,
            explanation TEXT NOT NULL,
            actual_result TEXT
        )
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS kaggle_version (version INTEGER)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS odds_cache (
            id INTEGER PRIMARY KEY,
            cached_at TEXT,
            data TEXT
        )
    """)
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(predictions)").fetchall()}
    if "game_date" not in existing_cols:
        conn.execute("ALTER TABLE predictions ADD COLUMN game_date TEXT")
    if "league" not in existing_cols:
        conn.execute("ALTER TABLE predictions ADD COLUMN league TEXT DEFAULT 'NBA'")
        conn.execute("UPDATE predictions SET league = 'NBA' WHERE league IS NULL")
    conn.commit()
    conn.close()


def log_prediction(
    player_name: str,
    stat_type: str,
    stat_line: float,
    opponent_team: str,
    predicted_outcome: str,
    confidence: float,
    explanation: str,
    game_date: str | None = None,
    league: str = "NBA",
) -> int:
    conn = get_connection()
    existing = conn.execute(
        """
        SELECT id, confidence FROM predictions
        WHERE player_name = ? AND stat_type = ? AND stat_line = ? AND opponent_team = ?
          AND COALESCE(game_date, '') = COALESCE(?, '')
        ORDER BY timestamp DESC LIMIT 1
        """,
        (player_name, stat_type, stat_line, opponent_team, game_date),
    ).fetchone()
    if existing:
        if abs(existing["confidence"] - confidence) > 1e-6:
            conn.execute(
                "UPDATE predictions SET confidence = ?, explanation = ?, timestamp = ? WHERE id = ?",
                (confidence, explanation, datetime.utcnow().isoformat(), existing["id"]),
            )
            conn.commit()
        conn.close()
        return existing["id"]
    cursor = conn.execute(
        """
        INSERT INTO predictions
            (timestamp, player_name, stat_type, stat_line, opponent_team,
             predicted_outcome, confidence, explanation, actual_result, game_date, league)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
        """,
        (
            datetime.utcnow().isoformat(),
            player_name,
            stat_type,
            stat_line,
            opponent_team,
            predicted_outcome,
            confidence,
            explanation,
            game_date,
            league,
        ),
    )
    row_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return row_id


_RESOLVE_STAT_COLS = {
    "Points":               ["points"],
    "Rebounds":             ["reboundsTotal"],
    "Assists":              ["assists"],
    "3PM":                  ["threePointersMade"],
    "PRA":                  ["points", "reboundsTotal", "assists"],
    "PR":                   ["points", "reboundsTotal"],
    "PA":                   ["points", "assists"],
    "RA":                   ["reboundsTotal", "assists"],
    "Blocks":               ["blocks"],
    "Steals":               ["steals"],
    "Blocks+Steals":        ["blocks", "steals"],
    "Turnovers":            ["turnovers"],
    "Offensive Rebounds":   ["reboundsOffensive"],
    "Defensive Rebounds":   ["reboundsDefensive"],
    "Double Double":        ["doubleDouble"],
    "3PA":                  ["threePointersAttempted"],
}


def resolve_pending_predictions() -> int:
    conn = get_connection()
    pending = conn.execute(
        "SELECT id, timestamp, player_name, stat_type, stat_line, opponent_team "
        "FROM predictions WHERE actual_result IS NULL"
    ).fetchall()
    conn.close()

    if not pending:
        return 0

    resolved = 0
    conn = get_connection()
    try:
        for row in pending:
            stat_cols = _RESOLVE_STAT_COLS.get(row["stat_type"])
            if not stat_cols:
                continue

            parts = row["player_name"].strip().rsplit(" ", 1)
            if len(parts) < 2:
                continue
            first_name, last_name = parts

            opp = row["opponent_team"]
            opp_nickname = "Trail Blazers" if "Blazers" in opp else opp.strip().split()[-1]

            try:
                pred_ts = datetime.fromisoformat(row["timestamp"])
            except ValueError:
                continue

            available_cols = {
                r[1] for r in conn.execute("PRAGMA table_info(player_stats)").fetchall()
            }
            needed = stat_cols + ["gameDateTimeEst", "numMinutes"]
            select_cols = [c for c in needed if c in available_cols]
            cols_sql = ", ".join(select_cols)
            game = conn.execute(
                f"SELECT {cols_sql} FROM player_stats "
                "WHERE firstName = ? AND lastName = ? AND opponentteamName = ? "
                "ORDER BY ABS(julianday(gameDateTimeEst) - julianday(?)) LIMIT 1",
                (first_name, last_name, opp_nickname, pred_ts.isoformat()),
            ).fetchone()

            if not game:
                continue

            try:
                game_dt = datetime.fromisoformat(
                    str(game["gameDateTimeEst"]).replace("Z", "").replace("+00:00", "")
                )
            except ValueError:
                continue

            if abs((game_dt - pred_ts).total_seconds()) > 86400:
                continue

            minutes_val = game["numMinutes"] if "numMinutes" in select_cols else None
            if minutes_val is None or float(minutes_val or 0) == 0:
                actual_result = "DNP"
            else:
                actual_value = sum(float(game[c] or 0) for c in stat_cols if c in select_cols)
                actual_result = "OVER" if actual_value > row["stat_line"] else "UNDER"

            conn.execute(
                "UPDATE predictions SET actual_result = ? WHERE id = ?",
                (actual_result, row["id"]),
            )
            resolved += 1

        conn.commit()
    finally:
        conn.close()

    if resolved:
        print(f"Resolved {resolved} pending prediction(s).")
    return resolved


def get_latest_kaggle_version() -> int:
    """
    Ask kagglehub for the latest dataset version and return it as an integer.
    Parses the version from the last numeric path component (e.g. …/versions/469 → 469).
    Downloads the dataset first if a newer version is available on Kaggle.
    """
    import kagglehub
    path = kagglehub.dataset_download("eoinamoore/historical-nba-data-and-player-box-scores")
    for part in reversed(path.replace("\\", "/").rstrip("/").split("/")):
        if part.isdigit():
            return int(part)
    raise ValueError(f"Could not parse version number from Kaggle path: {path}")


def run_etl() -> bool:
    """
    Download the Kaggle NBA dataset and load it into SQLite.
    Skips if the stored dataset version already matches the latest Kaggle version.
    Drops and recreates player_stats, players, and schedule when a new version is found.
    The predictions and kaggle_version tables are never touched.
    Returns True if ETL ran, False if skipped.
    """
    import kagglehub

    # --- Version check ---
    latest_version = get_latest_kaggle_version()
    conn = sqlite3.connect(DB_PATH)
    skip = False
    try:
        stored = conn.execute("SELECT version FROM kaggle_version LIMIT 1").fetchone()
        skip = bool(stored and stored[0] == latest_version)
    except sqlite3.OperationalError:
        pass  # table may not exist yet on first run
    finally:
        conn.close()

    if skip:
        print(f"Dataset up to date (version {latest_version}), skipping ETL.")
        resolve_pending_predictions()
        return False

    # --- Download ---
    print("Downloading Kaggle dataset (eoinamoore/historical-nba-data-and-player-box-scores)...")
    path = kagglehub.dataset_download("eoinamoore/historical-nba-data-and-player-box-scores")
    print(f"Dataset path: {path}")

    # --- PlayerStatisticsExtended.csv → player_stats ---
    stats_path = os.path.join(path, "PlayerStatisticsExtended.csv")
    print("Loading PlayerStatisticsExtended.csv in chunks...")
    total_rows = 0
    conn = sqlite3.connect(DB_PATH)
    for i, chunk in enumerate(
        pd.read_csv(stats_path, usecols=_STATS_COLS, chunksize=10_000)
    ):
        chunk.to_sql(
            "player_stats", conn,
            if_exists="replace" if i == 0 else "append",
            index=False,
        )
        total_rows += len(chunk)
        if (i + 1) % 10 == 0:
            print(f"  ...{total_rows:,} rows loaded")
    conn.close()
    print(f"  Done — {total_rows:,} rows total")

    # --- Players.csv → players ---
    players_path = os.path.join(path, "Players.csv")
    print("Loading Players.csv...")
    players_df = pd.read_csv(
        players_path, usecols=["personId", "firstName", "lastName"]
    )
    conn = sqlite3.connect(DB_PATH)
    players_df.to_sql("players", conn, if_exists="replace", index=False)
    conn.close()
    print(f"  Done — {len(players_df):,} players")

    # --- LeagueSchedule25_26.csv → schedule ---
    schedule_path = os.path.join(path, "LeagueSchedule25_26.csv")
    print("Loading LeagueSchedule25_26.csv...")
    schedule_df = pd.read_csv(schedule_path, usecols=_SCHEDULE_COLS)
    schedule_df = schedule_df[schedule_df["homeTeamId"] != 0]
    conn = sqlite3.connect(DB_PATH)
    schedule_df.to_sql("schedule", conn, if_exists="replace", index=False)
    conn.close()
    print(f"  Done — {len(schedule_df):,} games")

    # --- Indexes ---
    print("Creating indexes...")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stats_person ON player_stats(personId)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stats_date   ON player_stats(gameDateTimeEst)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_schedule_home ON schedule(homeTeamId)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_schedule_away ON schedule(awayTeamId)")
    conn.commit()
    conn.close()

    # --- Store the version we just loaded ---
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM kaggle_version")
    conn.execute("INSERT INTO kaggle_version (version) VALUES (?)", (latest_version,))
    conn.commit()
    conn.close()

    print("ETL complete.")
    resolve_pending_predictions()
    return True
