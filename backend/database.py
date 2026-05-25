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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wnba_odds_cache (
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
    from zoneinfo import ZoneInfo
    today_et = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    conn = get_connection()
    pending = conn.execute(
        "SELECT id, timestamp, player_name, stat_type, stat_line, opponent_team "
        "FROM predictions WHERE actual_result IS NULL"
        "  AND (game_date IS NULL OR game_date < ?)",
        (today_et,),
    ).fetchall()
    conn.close()

    if not pending:
        return 0

    resolved = 0
    dnp_count = 0
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
                dnp_count += 1
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
        print(f"Resolved {resolved} pending prediction(s) — {dnp_count} DNP(s).")
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


def backup_predictions_to_csv() -> str:
    """Write all rows from the predictions table to predictions_backup.csv."""
    import csv
    backup_path = os.path.join(os.path.dirname(__file__), "predictions_backup.csv")
    conn = get_connection()
    rows = conn.execute("SELECT * FROM predictions").fetchall()
    conn.close()
    if not rows:
        print("No predictions to back up.")
        return backup_path
    with open(backup_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(rows[0].keys())
        writer.writerows(rows)
    print(f"Backed up {len(rows):,} predictions → {backup_path}")
    return backup_path


def restore_predictions_from_csv() -> int:
    """Read predictions_backup.csv and insert rows not already in the predictions table.
    Skips rows that match on player_name, stat_type, stat_line, opponent_team, game_date."""
    import csv
    backup_path = os.path.join(os.path.dirname(__file__), "predictions_backup.csv")
    if not os.path.exists(backup_path):
        print("No predictions_backup.csv found — nothing to restore.")
        return 0

    with open(backup_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        print("predictions_backup.csv is empty — nothing to restore.")
        return 0

    from zoneinfo import ZoneInfo
    today_et = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    conn = get_connection()
    inserted = 0
    skipped = 0
    try:
        for row in rows:
            exists = conn.execute(
                """
                SELECT 1 FROM predictions
                WHERE player_name = ? AND stat_type = ? AND stat_line = ?
                  AND opponent_team = ? AND COALESCE(game_date, '') = COALESCE(?, '')
                LIMIT 1
                """,
                (
                    row["player_name"],
                    row["stat_type"],
                    float(row["stat_line"]),
                    row["opponent_team"],
                    row.get("game_date") or None,
                ),
            ).fetchone()
            if exists:
                skipped += 1
                continue
            game_date_val = row.get("game_date") or None
            actual_result_val = row.get("actual_result") or None
            if game_date_val == "2026-05-24":
                print(f"[restore debug] game_date={game_date_val} today_et={today_et} triggers={actual_result_val is not None and game_date_val >= today_et}")
            if actual_result_val is not None and game_date_val and game_date_val >= today_et:
                actual_result_val = None
            conn.execute(
                """
                INSERT INTO predictions
                    (timestamp, player_name, stat_type, stat_line, opponent_team,
                     predicted_outcome, confidence, explanation, actual_result, game_date, league)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.get("timestamp"),
                    row["player_name"],
                    row["stat_type"],
                    float(row["stat_line"]),
                    row["opponent_team"],
                    row.get("predicted_outcome"),
                    float(row["confidence"]),
                    row.get("explanation", ""),
                    actual_result_val,
                    game_date_val,
                    row.get("league", "NBA"),
                ),
            )
            inserted += 1
        conn.commit()
    finally:
        conn.close()

    print(f"Restore complete: {inserted:,} inserted, {skipped:,} skipped (already existed).")
    return inserted


def run_etl() -> bool:
    """
    Download the Kaggle NBA dataset and load it into SQLite.
    Skips if the stored dataset version already matches the latest Kaggle version.
    Drops and recreates player_stats, players, and schedule when a new version is found.
    The predictions and kaggle_version tables are never touched.
    Returns True if ETL ran, False if skipped.
    """
    backup_predictions_to_csv()

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


def run_wnba_etl() -> bool:
    """
    Fetch WNBA historical box scores from ESPN's public API (no auth required).
    Covers seasons 2022, 2023, 2024 (May 1 – Oct 15 each year).
    Skips if wnba_player_stats already has rows.
    WARNING: first run takes 15–30 minutes due to ESPN rate-limit sleeping.
    Returns True if ETL ran, False if skipped.
    """
    import time
    import datetime as _dt
    import httpx as _httpx

    # --- Skip check ---
    conn = sqlite3.connect(DB_PATH)
    try:
        count = conn.execute("SELECT COUNT(*) FROM wnba_player_stats").fetchone()[0]
        if count > 0:
            print(f"WNBA data already loaded ({count:,} rows), skipping ETL.")
            conn.close()
            return False
    except sqlite3.OperationalError:
        pass  # table doesn't exist yet
    conn.close()

    # --- Create table ---
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wnba_player_stats (
            player_name   TEXT,
            player_id     TEXT,
            game_id       TEXT,
            game_date     TEXT,
            home          INTEGER,
            player_team   TEXT,
            opponent_team TEXT,
            minutes       TEXT,
            points        REAL,
            rebounds      REAL,
            assists       REAL,
            three_pm      REAL,
            steals        REAL,
            blocks        REAL,
            turnovers     REAL,
            oreb          REAL,
            dreb          REAL,
            plus_minus    REAL
        )
    """)
    conn.commit()
    conn.close()

    _SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"
    _SUMMARY    = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary"

    def _safe_float(s, default=0.0):
        try:
            return float(str(s).strip())
        except Exception:
            return default

    def _parse_made(s):
        try:
            return float(str(s).split("-")[0].strip())
        except Exception:
            return 0.0

    # --- Collect all unique game IDs ---
    print("WNBA ETL: scanning ESPN scoreboard for 2022–2024 game IDs…")
    print("  (This takes ~4 minutes — 0.5 s per day × 168 days × 3 seasons)")
    game_ids: set = set()
    for year in [2022, 2023, 2024]:
        start = _dt.date(year, 5, 1)
        end   = _dt.date(year, 10, 15)
        cur   = start
        while cur <= end:
            try:
                r = _httpx.get(_SCOREBOARD, params={"dates": cur.strftime("%Y%m%d")}, timeout=10)
                r.raise_for_status()
                for event in r.json().get("events", []):
                    game_ids.add(event["id"])
            except Exception:
                pass
            cur += _dt.timedelta(days=1)
            time.sleep(0.5)
        print(f"  {year}: {len(game_ids)} unique game IDs so far")

    print(f"WNBA ETL: fetching box scores for {len(game_ids)} games…")
    print("  (This takes ~5–10 minutes — 0.5 s per game)")

    _INSERT_SQL = """
        INSERT INTO wnba_player_stats
            (player_name, player_id, game_id, game_date, home, player_team,
             opponent_team, minutes, points, rebounds, assists, three_pm,
             steals, blocks, turnovers, oreb, dreb, plus_minus)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """

    rows_buf: list = []
    games_done = 0
    conn = sqlite3.connect(DB_PATH)

    for game_id in sorted(game_ids):
        try:
            r = _httpx.get(_SUMMARY, params={"event": game_id}, timeout=10)
            r.raise_for_status()
            data = r.json()
        except Exception:
            games_done += 1
            time.sleep(0.5)
            continue

        try:
            sections = data.get("boxscore", {}).get("players", [])
            if len(sections) < 2:
                raise ValueError("incomplete boxscore")

            try:
                game_date = data["header"]["competitions"][0]["date"][:10]
            except Exception:
                game_date = ""

            # ESPN convention: sections[0]=away, sections[1]=home
            for section, is_home in [(sections[0], 0), (sections[1], 1)]:
                team_name = section.get("team", {}).get("displayName", "")
                opp_name  = (sections[1] if is_home == 0 else sections[0]).get("team", {}).get("displayName", "")
                for group in section.get("statistics", []):
                    for entry in group.get("athletes", []):
                        if entry.get("didNotPlay", False):
                            continue
                        stats = entry.get("stats", [])
                        if len(stats) < 14:
                            continue
                        ath = entry.get("athlete", {})
                        rows_buf.append((
                            ath.get("displayName", ""),
                            str(ath.get("id", "")),
                            game_id,
                            game_date,
                            is_home,
                            team_name,
                            opp_name,
                            stats[0],               # MIN (keep as string "MM:SS")
                            _safe_float(stats[1]),  # PTS
                            _safe_float(stats[5]),  # REB
                            _safe_float(stats[6]),  # AST
                            _parse_made(stats[3]),  # 3PT made from "2-5"
                            _safe_float(stats[8]),  # STL
                            _safe_float(stats[9]),  # BLK
                            _safe_float(stats[7]),  # TO
                            _safe_float(stats[10]), # OREB
                            _safe_float(stats[11]), # DREB
                            _safe_float(stats[13]), # +/-
                        ))
        except Exception:
            pass

        games_done += 1
        if games_done % 100 == 0:
            print(f"  …{games_done}/{len(game_ids)} games processed, {len(rows_buf)} rows buffered")

        if len(rows_buf) >= 500:
            conn.executemany(_INSERT_SQL, rows_buf)
            conn.commit()
            rows_buf.clear()

        time.sleep(0.5)

    if rows_buf:
        conn.executemany(_INSERT_SQL, rows_buf)
        conn.commit()

    conn.execute("CREATE INDEX IF NOT EXISTS idx_wnba_player ON wnba_player_stats(player_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wnba_date   ON wnba_player_stats(game_date)")
    conn.commit()

    final = conn.execute("SELECT COUNT(*) FROM wnba_player_stats").fetchone()[0]
    conn.close()
    print(f"WNBA ETL complete: {games_done} games processed, {final:,} player rows loaded.")
    return True
