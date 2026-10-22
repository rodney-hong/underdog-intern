import sqlite3
import os
import unicodedata
import pandas as pd
from datetime import datetime, date, timedelta

DB_PATH = os.path.join(os.path.dirname(__file__), "predictions.db")


def _ascii_normalize(s: str | None) -> str | None:
    """Strip diacritics so accented names match their ASCII equivalents."""
    if s is None:
        return None
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


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


def find_schedule_csv(dataset_path: str) -> str:
    """
    Return the current-season schedule file in a Kaggle dataset version, e.g.
    LeagueSchedule26_27.csv. The dataset renames it each season, so pick the
    LeagueSchedule<YY>_<YY>.csv with the latest season rather than hardcoding it.
    """
    present = sorted(os.listdir(dataset_path))
    name = latest_schedule_name(present)
    if name is None:
        raise FileNotFoundError(f"No LeagueSchedule*.csv in {dataset_path}; files present: {present}")
    return os.path.join(dataset_path, name)


def latest_schedule_name(names) -> str | None:
    """The LeagueSchedule<YY>_<YY>.csv with the latest season among file names, or None."""
    import re
    files = [n for n in names if n.startswith("LeagueSchedule") and n.endswith(".csv")]
    if not files:
        return None

    def season(f: str) -> tuple:
        m = re.search(r"LeagueSchedule(\d{2})_(\d{2})", f)
        return (int(m.group(1)), int(m.group(2))) if m else (-1, -1)

    return max(files, key=lambda f: (season(f), f))


def read_schedule_csv(schedule_path: str) -> pd.DataFrame:
    """Read the schedule columns we use, matching header names case-insensitively."""
    header = pd.read_csv(schedule_path, nrows=0).columns
    by_lower = {c.lower(): c for c in header}
    missing = [c for c in _SCHEDULE_COLS if c.lower() not in by_lower]
    if missing:
        raise ValueError(f"{os.path.basename(schedule_path)} is missing columns {missing}; "
                         f"columns present: {list(header)}")
    df = pd.read_csv(schedule_path, usecols=[by_lower[c.lower()] for c in _SCHEDULE_COLS])
    return df.rename(columns={by_lower[c.lower()]: c for c in _SCHEDULE_COLS})


def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def enable_wal() -> str:
    """
    Switch predictions.db to WAL journaling (persistent; a no-op once set). Readers
    then never wait on a writer. Backups must use the SQLite backup API (or copy the
    -wal file too); a plain copy of predictions.db alone can miss recent writes.
    """
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    finally:
        conn.close()
    print(f"[startup] SQLite journal_mode={mode}")
    if mode.lower() != "wal":
        raise RuntimeError(f"journal_mode is {mode!r}, not 'wal' (another connection may hold a lock)")
    return mode


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


# ---------------------------------------------------------------------------
# Per-player model track record (app `predictions` table only; display-only —
# it never feeds the model's confidence, outcome or explanation)
# ---------------------------------------------------------------------------

TRACK_RECORD_K = 30          # shrinkage weight: pseudo-picks at the overall rate
TRACK_RECORD_MIN_N = 20      # below this the record is flagged as a small sample


def _wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson score interval for wins/n, or None when n == 0."""
    if n == 0:
        return None
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def _track_record_cutoff(conn, before: str | None) -> str | None:
    """
    ET date before which a pick's game must fall to count, from `before`
    (a prediction id, or an ISO timestamp; naive timestamps are UTC like the
    predictions table). Results are known only after the game, so a pick counts
    as "resolved before" the reference when its game date is strictly earlier
    than the reference's ET date. Raises KeyError / ValueError on a bad `before`.
    """
    if before is None:
        return None
    from zoneinfo import ZoneInfo
    if before.isdigit():
        row = conn.execute("SELECT timestamp FROM predictions WHERE id = ?", (int(before),)).fetchone()
        if row is None:
            raise KeyError(f"prediction {before} not found")
        before = row["timestamp"]
    ts = datetime.fromisoformat(before.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=ZoneInfo("UTC"))
    return ts.astimezone(ZoneInfo("America/New_York")).date().isoformat()


def player_track_record(player_name: str, stat_type: str = "Points", league: str = "NBA",
                        before: str | None = None) -> dict:
    """
    The app's own record on this player and stat: resolved picks only (actual_result
    OVER/UNDER; DNP and pending excluded), one row per (player, stat, line,
    opponent, game date) — the latest id when duplicated — and, with `before`,
    only games dated before the reference prediction's ET date. A pick wins when
    predicted_outcome == actual_result. overall_rate uses the same filters for
    every player; adjusted_rate shrinks raw_rate toward it with TRACK_RECORD_K.
    """
    conn = get_connection()
    try:
        cutoff = _track_record_cutoff(conn, before)
        sql = """
            SELECT player_name, predicted_outcome, actual_result FROM (
                SELECT player_name, predicted_outcome, actual_result,
                       ROW_NUMBER() OVER (
                           PARTITION BY player_name, stat_type, stat_line, opponent_team,
                                        COALESCE(game_date, substr(timestamp, 1, 10))
                           ORDER BY id DESC) AS rn
                FROM predictions
                WHERE stat_type = ? AND COALESCE(league, 'NBA') = ?
                  AND actual_result IN ('OVER', 'UNDER')
                  AND (? IS NULL OR COALESCE(game_date, substr(timestamp, 1, 10)) < ?)
            ) WHERE rn = 1
        """
        rows = conn.execute(sql, (stat_type, league, cutoff, cutoff)).fetchall()
    finally:
        conn.close()

    all_w = sum(r["predicted_outcome"] == r["actual_result"] for r in rows)
    all_n = len(rows)
    mine = [r for r in rows if r["player_name"] == player_name]
    wins = sum(r["predicted_outcome"] == r["actual_result"] for r in mine)
    n = len(mine)
    overall = all_w / all_n if all_n else None
    adjusted = (wins + TRACK_RECORD_K * overall) / (n + TRACK_RECORD_K) if overall is not None else None
    ci = _wilson_interval(wins, n)
    return {
        "player_name": player_name, "stat_type": stat_type, "league": league,
        "before_date": cutoff,
        "wins": wins, "losses": n - wins, "n": n,
        "raw_rate": wins / n if n else None,
        "overall_rate": overall, "overall_n": all_n,
        "adjusted_rate": adjusted, "k": TRACK_RECORD_K,
        "raw_rate_ci95": list(ci) if ci else None,
        "enough": n >= TRACK_RECORD_MIN_N, "min_n": TRACK_RECORD_MIN_N,
    }


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

RESOLVE_BATCH_SIZE = 50  # pending rows per commit, so the write lock is never held for long


def _pending_predictions() -> list:
    """App predictions with no result whose ET game date is before today."""
    from zoneinfo import ZoneInfo
    today_et = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT id, timestamp, player_name, stat_type, stat_line, opponent_team, game_date "
            "FROM predictions WHERE actual_result IS NULL"
            "  AND (game_date IS NULL OR game_date < ?)",
            (today_et,),
        ).fetchall()
    finally:
        conn.close()


def _nba_season(d: date) -> str:
    """NBA season label for a date, as ESPN writes it: 2026-10-21 -> '2026-27'."""
    start = d.year if d.month >= 7 else d.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def _roster_season(conn) -> str | None:
    """The rosters table's season, or None if there is no table or it mixes seasons."""
    try:
        seasons = [r[0] for r in conn.execute("SELECT DISTINCT season FROM rosters")]
    except sqlite3.OperationalError:
        return None
    return seasons[0] if len(seasons) == 1 else None


def _resolve_decision(conn, row, known_players: set, available_cols: set) -> dict | None:
    """
    What the resolver does with one pending prediction (writes nothing).
    Returns None for rows it can't evaluate (unmapped stat type, bad date), else a
    dict with "result" (OVER / UNDER / DNP, or None to leave pending), "kind",
    "matched" (the player_stats row used, or the matchup checked for a DNP) and
    "warning" (why a row the resolver could see is left pending).
    """
    from zoneinfo import ZoneInfo
    stat_cols = _RESOLVE_STAT_COLS.get(row["stat_type"])
    if not stat_cols:
        return None

    # Full-name match: splitting at the last space breaks suffixes
    # ("Jaren Jackson Jr." is firstName 'Jaren', lastName 'Jackson Jr.').
    player_name = row["player_name"].strip()
    opp = row["opponent_team"]
    opp_nickname = "Trail Blazers" if "Blazers" in opp else opp.strip().split()[-1]

    game_date_val = row["game_date"]
    if not game_date_val:
        try:   # timestamp is naive UTC; the game date is the ET date
            game_date_val = (datetime.fromisoformat(row["timestamp"])
                             .replace(tzinfo=ZoneInfo("UTC"))
                             .astimezone(ZoneInfo("America/New_York")).date().isoformat())
        except ValueError:
            return None
    try:
        gd = date.fromisoformat(game_date_val)
    except ValueError:
        return None
    # gameDateTimeEst is ET wall time 'YYYY-MM-DD HH:MM:SS': [game_date, next day)
    # is the ET date, as an index range.
    day = (game_date_val, (gd + timedelta(days=1)).isoformat())

    out = {"id": row["id"], "player": player_name, "stat": row["stat_type"],
           "game_date": game_date_val, "opponent": opp,
           "result": None, "kind": "pending", "matched": None, "warning": None}

    select_cols = [c for c in stat_cols + ["gameDateTimeEst", "numMinutes"]
                   if c in available_cols]
    day_rows = conn.execute(
        f"SELECT {', '.join(select_cols)}, playerteamName, opponentteamName "
        "FROM player_stats WHERE gameDateTimeEst >= ? AND gameDateTimeEst < ? "
        "  AND ascii_norm(firstName || ' ' || lastName) = ascii_norm(?)",
        (*day, player_name),
    ).fetchall()

    game = next((g for g in day_rows if g["opponentteamName"] == opp_nickname), None)
    if game:
        minutes_val = game["numMinutes"] if "numMinutes" in select_cols else None
        out["matched"] = {"date": game["gameDateTimeEst"], "team": game["playerteamName"],
                          "opponent": game["opponentteamName"], "minutes": minutes_val}
        if minutes_val is None or float(minutes_val or 0) == 0:
            out["result"], out["kind"] = "DNP", "dnp_zero_min"
        else:
            actual_value = sum(float(game[c] or 0) for c in stat_cols if c in select_cols)
            out["result"] = "OVER" if actual_value > row["stat_line"] else "UNDER"
            out["kind"] = "graded"
        return out

    if day_rows:
        # Played that day, but not against the stored opponent: never grade it.
        g = day_rows[0]
        out["matched"] = {"date": g["gameDateTimeEst"], "team": g["playerteamName"],
                          "opponent": g["opponentteamName"], "minutes": g["numMinutes"]}
        out["warning"] = (f"played for the {g['playerteamName']} vs the {g['opponentteamName']} "
                          f"on {game_date_val}, stored opponent {opp}")
        return out

    # No row that day. DNP only for a known player whose team played the stored
    # opponent that day.
    if _ascii_normalize(player_name) not in known_players:
        out["warning"] = "name matches no player_stats row (check the name)"
        return out
    last = conn.execute(
        "SELECT playerteamName, personId FROM player_stats WHERE gameDateTimeEst < ? "
        "  AND ascii_norm(firstName || ' ' || lastName) = ascii_norm(?) "
        "ORDER BY gameDateTimeEst DESC LIMIT 1",
        (game_date_val, player_name),
    ).fetchone()
    # His team mirrors the predictor (data_fetcher.get_player_context): the ESPN
    # roster team from rosters.current_team(), else his latest box-score team. The
    # roster is a current snapshot, so it is used only when game_date falls in the
    # roster's season.
    import rosters
    team, team_source = None, None
    if _nba_season(gd) == _roster_season(conn):
        roster = rosters.current_team(player_name, last["personId"] if last else None)
        if roster:
            team, team_source = roster["team_name"], "roster"
    if team is None and last:
        team, team_source = last["playerteamName"], "box score"
    if team is None:
        out["warning"] = f"no roster team and no box score before {game_date_val}: team unknown"
        return out
    out["matched"] = {"date": game_date_val, "team": team, "team_source": team_source,
                      "opponent": opp_nickname, "minutes": None}
    matchup = conn.execute(
        "SELECT 1 FROM player_stats WHERE gameDateTimeEst >= ? AND gameDateTimeEst < ? "
        "  AND playerteamName = ? AND opponentteamName = ? LIMIT 1",
        (*day, team, opp_nickname),
    ).fetchone()
    if matchup:
        out["result"], out["kind"] = "DNP", "dnp_absent"
        return out
    loaded = conn.execute(
        "SELECT 1 FROM player_stats WHERE gameDateTimeEst >= ? AND gameDateTimeEst < ? LIMIT 1",
        day,
    ).fetchone()
    if loaded:
        out["warning"] = (f"the {opp_nickname} did not play the {team} (his {team_source} team) "
                          f"on {game_date_val}")
    # else: no box scores for that date are loaded yet: quietly pending
    return out


def _resolve_decisions(conn, pending):
    """Yield (row, decision) for each pending row, printing warnings and DNP-by-absence rows."""
    available_cols = {
        r[1] for r in conn.execute("PRAGMA table_info(player_stats)").fetchall()
    }
    # Normalized full names in player_stats, read before any write lock is taken.
    known_players = {
        _ascii_normalize(f"{f} {l}")
        for f, l in conn.execute(
            "SELECT DISTINCT firstName, lastName FROM player_stats "
            "WHERE firstName IS NOT NULL AND lastName IS NOT NULL")
    }
    for row in pending:
        d = _resolve_decision(conn, row, known_players, available_cols)
        if d and d["warning"]:
            print(f"  WARNING: id {d['id']} {d['player']} ({d['game_date']} vs {d['opponent']}): "
                  f"{d['warning']}; left pending", flush=True)
        elif d and d["kind"] == "dnp_absent":
            print(f"  DNP (absent from box score): id {d['id']} {d['player']}, "
                  f"{d['game_date']}, {d['matched']['team']} vs {d['opponent']}", flush=True)
        yield row, d


def dry_run_pending_predictions() -> list[dict]:
    """resolve_pending_predictions() without writing: the decision for every pending row."""
    pending = _pending_predictions()
    print(f"Dry run: {len(pending)} pending app prediction(s), nothing written.", flush=True)
    conn = get_connection()
    conn.create_function("ascii_norm", 1, _ascii_normalize)
    try:
        return [d for _, d in _resolve_decisions(conn, pending) if d]
    finally:
        conn.close()


def resolve_pending_predictions() -> int:
    """
    Fill predictions.actual_result (OVER / UNDER / DNP) from player_stats.
    A prediction is graded only against the player's row on game_date (ET) whose
    opponent is the stored opponent. If he has a row that day against another
    team, or has none and the stored opponent didn't play his team that day, the
    row is left pending with a warning: never graded, never marked DNP.
    No row while that matchup is loaded means DNP (absent from box score), for a
    name that matches some player_stats row; his team is the ESPN roster team when
    game_date is in the roster's season, else his latest box-score team (as the
    predictor does). Each such row is printed. Nothing loaded for that date means
    the box score isn't in yet: pending.
    Commits every RESOLVE_BATCH_SIZE rows. dry_run_pending_predictions() shows
    the same decisions without writing.
    """
    pending = _pending_predictions()
    if not pending:
        return 0
    print(f"Resolving {len(pending)} pending app prediction(s)...", flush=True)

    resolved      = 0
    dnp_zero_min  = 0
    dnp_absence   = 0
    conn = get_connection()
    conn.create_function("ascii_norm", 1, _ascii_normalize)
    try:
        for i, (row, d) in enumerate(_resolve_decisions(conn, pending)):
            if i and i % RESOLVE_BATCH_SIZE == 0:
                conn.commit()
            if not d or d["result"] is None:
                continue
            conn.execute(
                "UPDATE predictions SET actual_result = ? WHERE id = ?",
                (d["result"], row["id"]),
            )
            resolved += 1
            dnp_zero_min += d["kind"] == "dnp_zero_min"
            dnp_absence  += d["kind"] == "dnp_absent"

        conn.commit()
    finally:
        conn.close()

    if resolved:
        normal = resolved - dnp_zero_min - dnp_absence
        print(
            f"Resolved {resolved} pending prediction(s) — "
            f"{normal} OVER/UNDER, "
            f"{dnp_zero_min} DNP (zero minutes), "
            f"{dnp_absence} DNP (absent from box score).",
            flush=True,
        )
    return resolved


KAGGLE_DATASET = "eoinamoore/historical-nba-data-and-player-box-scores"


class KaggleVersionUnavailable(Exception):
    """The newest Kaggle version is listed but one of its files returns 404."""


def _kaggle_dataset_request(request_cls, version: int | None = None):
    owner, slug = KAGGLE_DATASET.split("/")
    r = request_cls()
    r.owner_slug, r.dataset_slug = owner, slug
    if version is not None:
        r.dataset_version_number = version
    return r


def get_latest_kaggle_version() -> int:
    """Ask Kaggle for the dataset's current version number (metadata only, no download)."""
    from kagglehub.clients import build_kaggle_client
    from kagglesdk.datasets.types.dataset_api_service import ApiGetDatasetRequest
    with build_kaggle_client() as c:
        d = c.datasets.dataset_api_client.get_dataset(_kaggle_dataset_request(ApiGetDatasetRequest))
    return int(d.current_version_number)


def download_kaggle_files(version: int) -> dict:
    """
    Download, one file at a time, only the files run_etl loads from `version`:
    PlayerStatisticsExtended.csv, Players.csv and the latest LeagueSchedule*.csv.
    Per-file downloads work when the whole-dataset archive does not (v521 on
    2026-10-21: archive 404, every file 200). Returns {"stats", "players",
    "schedule"} -> local path. Raises KaggleVersionUnavailable on a 404.
    """
    import kagglehub
    import requests
    from kagglehub.clients import build_kaggle_client
    from kagglesdk.datasets.types.dataset_api_service import ApiListDatasetFilesRequest

    def is_404(exc: Exception) -> bool:
        resp = getattr(exc, "response", None)
        return isinstance(exc, requests.HTTPError) and resp is not None and resp.status_code == 404

    try:
        r = _kaggle_dataset_request(ApiListDatasetFilesRequest, version)
        r.page_size = 200
        with build_kaggle_client() as c:
            names = [f.name for f in c.datasets.dataset_api_client.list_dataset_files(r).dataset_files or []]
    except Exception as exc:
        if is_404(exc):
            raise KaggleVersionUnavailable(f"v{version}: file list returned 404") from exc
        raise
    schedule = latest_schedule_name(names)
    if schedule is None:
        raise FileNotFoundError(f"No LeagueSchedule*.csv in Kaggle v{version}; files listed: {names}")

    wanted = {"stats": "PlayerStatisticsExtended.csv", "players": "Players.csv", "schedule": schedule}
    out = {}
    for key, name in wanted.items():
        try:
            out[key] = kagglehub.dataset_download(f"{KAGGLE_DATASET}/versions/{version}", path=name)
        except Exception as exc:
            if is_404(exc):
                raise KaggleVersionUnavailable(f"v{version}: {name} returned 404") from exc
            raise
    return out


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
    Every file is downloaded before any table is replaced, so a KaggleVersionUnavailable
    (404 on the newest version) leaves all tables as they were.
    Returns True if ETL ran, False if skipped.
    """
    backup_predictions_to_csv()

    # --- Version check ---
    latest_version = get_latest_kaggle_version()
    conn = sqlite3.connect(DB_PATH, timeout=30)
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

    # --- Download (all files before touching any table) ---
    print(f"Downloading Kaggle dataset ({KAGGLE_DATASET}) v{latest_version}, file by file...")
    files = download_kaggle_files(latest_version)
    print(f"Dataset files: {files}")

    # --- PlayerStatisticsExtended.csv → player_stats ---
    stats_path = files["stats"]
    print("Loading PlayerStatisticsExtended.csv in chunks...")
    total_rows = 0
    conn = sqlite3.connect(DB_PATH, timeout=30)
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
    players_path = files["players"]
    print("Loading Players.csv...")
    players_df = pd.read_csv(
        players_path, usecols=["personId", "firstName", "lastName"]
    )
    conn = sqlite3.connect(DB_PATH, timeout=30)
    players_df.to_sql("players", conn, if_exists="replace", index=False)
    conn.close()
    print(f"  Done — {len(players_df):,} players")

    # --- LeagueSchedule<current season>.csv → schedule ---
    schedule_path = files["schedule"]
    print(f"Loading {os.path.basename(schedule_path)}...")
    schedule_df = read_schedule_csv(schedule_path)
    schedule_df = schedule_df[schedule_df["homeTeamId"] != 0]
    conn = sqlite3.connect(DB_PATH, timeout=30)
    schedule_df.to_sql("schedule", conn, if_exists="replace", index=False)
    conn.close()
    print(f"  Done — {len(schedule_df):,} games")

    # --- Indexes ---
    print("Creating indexes...")
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stats_person ON player_stats(personId)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stats_date   ON player_stats(gameDateTimeEst)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_schedule_home ON schedule(homeTeamId)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_schedule_away ON schedule(awayTeamId)")
    conn.commit()
    conn.close()

    # --- Store the version we just loaded ---
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("DELETE FROM kaggle_version")
    conn.execute("INSERT INTO kaggle_version (version) VALUES (?)", (latest_version,))
    conn.commit()
    conn.close()

    print("ETL complete.")
    resolve_pending_predictions()
    return True


# ---------------------------------------------------------------------------
# WNBA ETL — shared helpers (used by run_wnba_etl() and wnba_backfill.py)
# ---------------------------------------------------------------------------

_WNBA_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"
_WNBA_SUMMARY_URL    = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary"
_WNBA_BACKFILL_START_YEAR    = 2022  # first season the historical backfill covers
_WNBA_SEASON_START_MD        = (5, 1)   # WNBA regular slate opens ~May 1
_WNBA_SEASON_END_MD          = (10, 15)  # …and wraps by mid-October
_WNBA_STARTUP_MAX_DELTA_DAYS = 30    # startup only fetches a delta this small; larger
                                     # gaps defer to wnba_backfill.py so boot never blocks

_WNBA_INSERT_SQL = """
    INSERT INTO wnba_player_stats
        (player_name, player_id, game_id, game_date, home, player_team,
         opponent_team, minutes, points, rebounds, assists, three_pm,
         steals, blocks, turnovers, oreb, dreb, plus_minus)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def _wnba_ensure_table(conn: sqlite3.Connection) -> None:
    """Create wnba_player_stats and its indexes if they don't exist."""
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wnba_player ON wnba_player_stats(player_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wnba_date   ON wnba_player_stats(game_date)")
    conn.commit()


def _wnba_watermark(conn: sqlite3.Connection) -> str | None:
    """Return MAX(game_date) already stored, or None if the table is empty/missing."""
    try:
        row = conn.execute("SELECT MAX(game_date) FROM wnba_player_stats").fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row and row[0] else None


def _wnba_stored_game_ids(conn: sqlite3.Connection) -> set:
    """Return the set of game_ids already stored, for dedupe."""
    try:
        return {r[0] for r in conn.execute("SELECT DISTINCT game_id FROM wnba_player_stats")}
    except sqlite3.OperationalError:
        return set()


def _wnba_safe_float(s, default=0.0):
    try:
        return float(str(s).strip())
    except Exception:
        return default


def _wnba_parse_made(s):
    try:
        return float(str(s).split("-")[0].strip())
    except Exception:
        return 0.0


def _wnba_collect_game_ids(start_date: date, end_date: date, verbose: bool = True) -> set:
    """
    Scan ESPN's scoreboard day-by-day for game IDs between start_date and end_date
    (both inclusive), restricted to each year's WNBA in-season window so off-season
    days aren't queried. 0.5 s sleep per day to respect ESPN rate limits.
    """
    import time
    import httpx as _httpx

    game_ids: set = set()
    for year in range(start_date.year, end_date.year + 1):
        season_start = date(year, *_WNBA_SEASON_START_MD)
        season_end   = date(year, *_WNBA_SEASON_END_MD)
        lo = max(season_start, start_date)
        hi = min(season_end, end_date)
        if lo > hi:
            continue  # requested window doesn't overlap this season
        cur = lo
        while cur <= hi:
            try:
                r = _httpx.get(_WNBA_SCOREBOARD_URL, params={"dates": cur.strftime("%Y%m%d")}, timeout=10)
                r.raise_for_status()
                for event in r.json().get("events", []):
                    game_ids.add(event["id"])
            except Exception:
                pass
            cur += timedelta(days=1)
            time.sleep(0.5)
        if verbose:
            print(f"  {year}: {len(game_ids)} unique game IDs so far")
    return game_ids


def _wnba_fetch_and_store(
    conn: sqlite3.Connection,
    game_ids: set,
    skip_ids: set = frozenset(),
    verbose: bool = True,
) -> int:
    """
    Fetch box scores for every game_id not already in skip_ids and insert player rows.
    Returns the number of rows inserted. 0.5 s sleep per game for ESPN rate limits.
    """
    import time
    import httpx as _httpx

    to_fetch = sorted(g for g in game_ids if g not in skip_ids)
    if verbose:
        print(f"WNBA ETL: fetching box scores for {len(to_fetch)} new games…")
        print("  (~0.5 s per game)")

    rows_buf: list = []
    inserted = 0
    games_done = 0

    for game_id in to_fetch:
        try:
            r = _httpx.get(_WNBA_SUMMARY_URL, params={"event": game_id}, timeout=10)
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
                            stats[0],                     # MIN (keep as string "MM:SS")
                            _wnba_safe_float(stats[1]),   # PTS
                            _wnba_safe_float(stats[5]),   # REB
                            _wnba_safe_float(stats[6]),   # AST
                            _wnba_parse_made(stats[3]),   # 3PT made from "2-5"
                            _wnba_safe_float(stats[8]),   # STL
                            _wnba_safe_float(stats[9]),   # BLK
                            _wnba_safe_float(stats[7]),   # TO
                            _wnba_safe_float(stats[10]),  # OREB
                            _wnba_safe_float(stats[11]),  # DREB
                            _wnba_safe_float(stats[13]),  # +/-
                        ))
        except Exception:
            pass

        games_done += 1
        if len(rows_buf) >= 500:
            conn.executemany(_WNBA_INSERT_SQL, rows_buf)
            conn.commit()
            inserted += len(rows_buf)
            rows_buf.clear()
        if verbose and games_done % 100 == 0:
            print(f"  …{games_done}/{len(to_fetch)} games processed, {inserted + len(rows_buf)} rows so far")

        time.sleep(0.5)

    if rows_buf:
        conn.executemany(_WNBA_INSERT_SQL, rows_buf)
        conn.commit()
        inserted += len(rows_buf)

    return inserted


def run_wnba_etl() -> bool:
    """
    Incremental startup ETL: fetch only WNBA games newer than the stored watermark
    (MAX(game_date)), deduped by game_id.

    The full multi-minute historical backfill lives in wnba_backfill.py and must be run
    manually once — this function never performs it, so server startup is never blocked:
      * empty table            → print a hint to run wnba_backfill.py, return False
      * watermark > 30 days old → defer to wnba_backfill.py, return False
      * otherwise              → fetch the small delta since the watermark

    Returns True only if new player rows were inserted (so the caller retrains).
    """
    conn = sqlite3.connect(DB_PATH, timeout=30)
    _wnba_ensure_table(conn)
    watermark = _wnba_watermark(conn)

    if watermark is None:
        conn.close()
        print("WNBA table is empty — run `python wnba_backfill.py` once to load history. "
              "Skipping startup ETL.")
        return False

    try:
        wm_date = datetime.strptime(watermark[:10], "%Y-%m-%d").date()
    except Exception:
        conn.close()
        print(f"WNBA watermark '{watermark}' is unparseable — run `python wnba_backfill.py`. "
              "Skipping startup ETL.")
        return False

    today = date.today()
    gap_days = (today - wm_date).days
    if gap_days > _WNBA_STARTUP_MAX_DELTA_DAYS:
        conn.close()
        print(f"WNBA data is {gap_days} days behind (watermark {wm_date}); exceeds the "
              f"{_WNBA_STARTUP_MAX_DELTA_DAYS}-day startup cap. Run `python wnba_backfill.py` "
              "to catch up. Skipping startup ETL so boot isn't blocked.")
        return False

    # Scan from the watermark day (inclusive) so games that finished after the last run
    # on that same day are still picked up; game_id dedupe prevents duplicate rows.
    print(f"WNBA ETL: checking for games since {wm_date} (through {today})…")
    game_ids = _wnba_collect_game_ids(wm_date, today, verbose=False)
    stored   = _wnba_stored_game_ids(conn)
    inserted = _wnba_fetch_and_store(conn, game_ids, skip_ids=stored, verbose=False)
    conn.close()

    if inserted:
        print(f"WNBA ETL: added {inserted:,} new player rows since {wm_date}.")
        return True
    print(f"WNBA ETL: no new games since {wm_date}.")
    return False
