import sqlite3
import os
import time
import unicodedata
import pandas as pd
from datetime import datetime, date, timedelta, timezone

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


def init_db(maintain_flags: bool = True):
    """Create missing tables and columns. With maintain_flags (prod), also recompute
    is_current and the limited-data flags; dev and dry only make sure the schema exists."""
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
    if "is_limited_data" not in existing_cols:
        conn.execute("ALTER TABLE predictions ADD COLUMN is_limited_data INTEGER")
    if "model" not in existing_cols:
        # Which model made the pick: 'challenger' (from Stage 3, challenger/live.py) or
        # 'frozen' (the 2026-27 XGBoost / heuristic path). Every earlier row is 'frozen'.
        conn.execute("ALTER TABLE predictions ADD COLUMN model TEXT")
        conn.execute("UPDATE predictions SET model = 'frozen' WHERE model IS NULL")
    if "is_current" not in existing_cols:
        conn.execute("ALTER TABLE predictions ADD COLUMN is_current INTEGER")
    if "retro_of" not in existing_cols:
        # model = 'challenger_retro' rows: the frozen app prediction they re-score
        # (experiments/retro_challenger.py); exploratory, never in the default stats
        conn.execute("ALTER TABLE predictions ADD COLUMN retro_of INTEGER")
    conn.commit()
    if not maintain_flags:
        conn.close()
        return
    refresh_current(conn)          # recomputed for every row on each start (idempotent)
    conn.commit()
    try:   # display-only flag; a failure must never stop startup
        out = backfill_limited_data(conn)
        print(f"Limited-data flags on app predictions: {out}")
    except Exception as exc:
        print(f"Limited-data backfill FAILED: {exc!r} (flags unchanged)")
    conn.close()


def log_startup(mode: str, wnba_enabled: bool, challenger_ok: bool) -> int:
    """One startup_log row per backend start in prod (dev and dry write nothing), so
    daily_check.py can show restarts. Returns the row id."""
    import socket
    import sys
    conn = get_connection()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS startup_log (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at     TEXT NOT NULL,   -- UTC ISO
                mode           TEXT NOT NULL,
                host           TEXT,
                pid            INTEGER,
                git_commit     TEXT,
                python         TEXT,
                wnba_enabled   INTEGER,
                challenger_ok  INTEGER
            )""")
        cur = conn.execute(
            "INSERT INTO startup_log (started_at, mode, host, pid, git_commit, python, wnba_enabled, "
            "challenger_ok) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), mode, socket.gethostname(), os.getpid(),
             _git_commit(), sys.version.split()[0], int(wnba_enabled), int(challenger_ok)))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _git_commit() -> str | None:
    """HEAD's commit hash read from .git (no subprocess), or None."""
    git = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".git")
    try:
        with open(os.path.join(git, "HEAD")) as f:
            head = f.read().strip()
        if not head.startswith("ref: "):
            return head
        ref = head[5:]
        path = os.path.join(git, *ref.split("/"))
        if os.path.exists(path):
            with open(path) as f:
                return f.read().strip()
        with open(os.path.join(git, "packed-refs")) as f:
            for line in f:
                if line.rstrip().endswith(" " + ref):
                    return line.split()[0]
    except OSError:
        pass
    return None


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
    model: str = "frozen",
) -> int:
    conn = get_connection()
    try:   # display-only flag (is_limited_data); never blocks logging
        limited = 0 if league != "NBA" else int(is_limited_data(
            player_data_counts(conn, player_name, game_date, opponent_team), explanation))
    except Exception:
        limited = None
    # A repeat at the same line, with the same model, replaces that row's pick in full
    # (outcome, confidence, explanation, time); a different line or model is a new row.
    existing = conn.execute(
        """
        SELECT id FROM predictions
        WHERE player_name = ? AND stat_type = ? AND stat_line = ? AND opponent_team = ?
          AND COALESCE(game_date, '') = COALESCE(?, '') AND COALESCE(model, 'frozen') = ?
        ORDER BY timestamp DESC, id DESC LIMIT 1
        """,
        (player_name, stat_type, stat_line, opponent_team, game_date, model),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE predictions SET predicted_outcome = ?, confidence = ?, explanation = ?, timestamp = ?, "
            "is_limited_data = ?, league = ?, model = ? WHERE id = ?",
            (predicted_outcome, confidence, explanation, datetime.utcnow().isoformat(), limited, league,
             model, existing["id"]),
        )
        refresh_current(conn, player_name, stat_type, game_date, model)
        conn.commit()
        conn.close()
        return existing["id"]
    cursor = conn.execute(
        """
        INSERT INTO predictions
            (timestamp, player_name, stat_type, stat_line, opponent_team,
             predicted_outcome, confidence, explanation, actual_result, game_date, league,
             is_limited_data, model)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
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
            limited,
            model,
        ),
    )
    row_id = cursor.lastrowid
    refresh_current(conn, player_name, stat_type, game_date, model)
    conn.commit()
    conn.close()
    return row_id


# is_current: 1 for the latest prediction (timestamp, then id) per (player_name, stat_type,
# game_date, model); 0 for the earlier ones it superseded. Every row is kept; History stats
# count current rows only. A row without game_date is never grouped (each stays current).
_CURRENT_SQL = """
    UPDATE predictions SET is_current = CASE WHEN game_date IS NULL OR id = (
        SELECT p2.id FROM predictions p2
        WHERE p2.player_name = predictions.player_name AND p2.stat_type = predictions.stat_type
          AND p2.game_date = predictions.game_date
          AND COALESCE(p2.model, 'frozen') = COALESCE(predictions.model, 'frozen')
        ORDER BY p2.timestamp DESC, p2.id DESC LIMIT 1) THEN 1 ELSE 0 END
"""


def refresh_current(conn, player_name: str | None = None, stat_type: str | None = None,
                    game_date: str | None = None, model: str | None = None) -> None:
    """Recompute is_current for one (player, stat, game_date, model) group, or for every row."""
    if player_name is None:
        conn.execute(_CURRENT_SQL)
    else:
        conn.execute(_CURRENT_SQL + " WHERE player_name = ? AND stat_type = ? AND "
                     "COALESCE(game_date, '') = COALESCE(?, '') AND COALESCE(model, 'frozen') = ?",
                     (player_name, stat_type, game_date, model))


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


# History's model filter: which predictions.model values each choice covers ('all' = the live
# picks, challenger + frozen; retro rows only under their own choice).
TRACK_RECORD_MODELS = {"challenger": ("challenger",), "frozen": ("frozen",),
                       "challenger_retro": ("challenger_retro",), "all": ("challenger", "frozen")}


def player_track_record(player_name: str, stat_type: str = "Points", league: str = "NBA",
                        before: str | None = None, model: str = "all") -> dict:
    """
    The app's own record on this player and stat, over the rows History's stats cards count:
    current (not superseded), not limited-data, resolved (actual_result OVER/UNDER; DNP and
    pending excluded), for `model` (TRACK_RECORD_MODELS: challenger, frozen, challenger_retro,
    or all = challenger + frozen). With `before`, only games dated before the reference
    prediction's ET date. A pick wins when predicted_outcome == actual_result. overall_rate
    uses the same filters for every player; adjusted_rate shrinks raw_rate toward it with
    TRACK_RECORD_K. ValueError for an unknown model.
    """
    if model not in TRACK_RECORD_MODELS:
        raise ValueError(f"model must be one of {sorted(TRACK_RECORD_MODELS)}")
    models = TRACK_RECORD_MODELS[model]
    conn = get_connection()
    try:
        cutoff = _track_record_cutoff(conn, before)
        sql = f"""
            SELECT player_name, predicted_outcome, actual_result FROM predictions
            WHERE stat_type = ? AND COALESCE(league, 'NBA') = ?
              AND actual_result IN ('OVER', 'UNDER')
              AND COALESCE(is_current, 1) = 1 AND COALESCE(is_limited_data, 0) = 0
              AND COALESCE(model, 'frozen') IN ({', '.join('?' * len(models))})
              AND (? IS NULL OR COALESCE(game_date, substr(timestamp, 1, 10)) < ?)
        """
        rows = conn.execute(sql, (stat_type, league, *models, cutoff, cutoff)).fetchall()
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
        "player_name": player_name, "stat_type": stat_type, "league": league, "model": model,
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

# Counting stats that show a player was on the floor even when numMinutes is missing.
DNP_COUNTING_COLS = ("points", "reboundsTotal", "assists", "threePointersMade",
                     "blocks", "steals", "turnovers")


def played(minutes, counting) -> bool:
    """
    False (DNP) only if minutes are 0/NULL and every counting stat is 0/NULL. NULL
    minutes with any counting stat means he played: Kaggle v523 blanked numMinutes
    for all 27 rows of 22600088 (2026-10-21) and kept every other column.
    Shared by the app resolver and paper_predictions.resolve_paper_predictions.
    """
    return bool(minutes) or any(bool(v) for v in counting)


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


def _person_index(conn) -> tuple[dict, dict]:
    """
    Name -> {personId} from every player_stats and players name: one dict keyed by
    the accent-free full name, one by rosters.name_key (no case, punctuation or
    Jr./II suffix). A personId can sit under several names: the 2026-27 box scores
    write 1626171 as 'Bobby Portis Jr.', earlier seasons and players as 'Bobby Portis'.
    """
    import rosters
    exact, loose = {}, {}
    for pid, first, last in conn.execute(
            "SELECT DISTINCT personId, firstName, lastName FROM player_stats WHERE personId IS NOT NULL "
            "UNION SELECT personId, firstName, lastName FROM players WHERE personId IS NOT NULL"):
        full = f"{first or ''} {last or ''}".strip()
        exact.setdefault(_ascii_normalize(full), set()).add(int(pid))
        loose.setdefault(rosters.name_key(full), set()).add(int(pid))
    return exact, loose


def _person_id(conn, player_name: str, index: tuple[dict, dict], day: tuple[str, str],
               opp_nickname: str) -> tuple[int | None, str | None]:
    """
    (personId, None) for a prediction's player name, or (None, why not). An exact
    full-name match wins; only a name with no exact match falls back to name_key.
    Several personIds under one name (two Jaylin Williams; stray players-table ids
    for Luka Doncic) narrow to the one who played the stored opponent that day,
    else who played that day, else the only one with a counted game in the year
    before, else the only one with any box score in the year before (stray ids
    carry a single preseason exhibition row). Still more than one: ambiguous,
    never guessed.
    """
    import game_types
    import rosters
    exact, loose = index
    ids = exact.get(_ascii_normalize(player_name)) or loose.get(rosters.name_key(player_name))
    if not ids:
        return None, "name matches no player_stats or players row (check the name)"
    if len(ids) == 1:
        return next(iter(ids)), None
    marks = ",".join("?" * len(ids))
    for cond, args in (
            ("gameDateTimeEst >= ? AND gameDateTimeEst < ? AND opponentteamName = ?", (*day, opp_nickname)),
            ("gameDateTimeEst >= ? AND gameDateTimeEst < ?", day),
            (f"gameDateTimeEst >= date(?, '-365 days') AND gameDateTimeEst < ? "
             f"AND gameType IN ({game_types.COUNTED_SQL})", (day[0], day[0])),
            ("gameDateTimeEst >= date(?, '-365 days') AND gameDateTimeEst < ?", (day[0], day[0]))):
        try:
            found = {r[0] for r in conn.execute(
                f"SELECT DISTINCT personId FROM player_stats WHERE personId IN ({marks}) AND {cond}",
                (*ids, *args))}
        except sqlite3.OperationalError:          # a table without gameType: skip that step
            continue
        if len(found) == 1:
            return found.pop(), None
        if found:
            break
    return None, f"name matches {len(ids)} personIds {sorted(ids)}: ambiguous"


# ---------------------------------------------------------------------------
# Limited-data app predictions (rookies, thin samples): kept and shown, but left out
# of the History accuracy figures. Display-only: never feeds the model.
# ---------------------------------------------------------------------------

REGULAR_SEASON_GAME_TYPES = ("Regular Season", "NBA Emirates Cup")   # game_types' regular-season labels
SEASON_2026_27_START = "2026-10-01"     # "before the 2026-27 season" (preseason is not counted anyway)
LIMITED_DATA_MIN_GAMES = 10             # fewer counted games before the game -> limited data
INSUFFICIENT_DATA_PREFIX = "Insufficient game log data"   # predictor's fixed low-data answer

_person_index_cache: dict = {}


def _cached_person_index(conn) -> tuple[dict, dict]:
    """_person_index, rebuilt only when the loaded Kaggle version (or DB) changes."""
    try:
        version = conn.execute("SELECT version FROM kaggle_version LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        version = None
    key = (DB_PATH, version[0] if version else None)
    if key not in _person_index_cache:
        _person_index_cache.clear()
        _person_index_cache[key] = _person_index(conn)
    return _person_index_cache[key]


def _day_and_opponent(game_date: str | None, opponent_team: str) -> tuple[tuple[str, str], str]:
    from zoneinfo import ZoneInfo
    gd = game_date or datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    opp = (opponent_team or "").strip()
    return ((gd, (date.fromisoformat(gd) + timedelta(days=1)).isoformat()),
            "Trail Blazers" if "Blazers" in opp else (opp.split()[-1] if opp else ""))


def resolve_person_id(conn, player_name: str, game_date: str | None = None,
                      opponent_team: str = "") -> int | None:
    """The one personId for a player name in a game (_person_id's rules), or None. Used by
    /predict (player_data_counts), the screenshot table (range flag, person_id) and the
    retro comparison, so all of them read the same player's history."""
    day, opp_nickname = _day_and_opponent(game_date, opponent_team)
    return _person_id(conn, player_name.strip(), _cached_person_index(conn), day, opp_nickname)[0]


def player_data_counts(conn, player_name: str, game_date: str | None, opponent_team: str = "",
                       index: tuple[dict, dict] | None = None, person_id: int | None = None) -> dict:
    """
    From player_stats, by personId (the resolver's exact-name mapping, _person_id):
      rs_total             regular-season games loaded, any date;
      rs_before_season     regular-season games before SEASON_2026_27_START;
      counted_before_game  counted games (game_types.COUNTED_SQL) before game_date.
    person_id None (name matches no player, or several and they can't be narrowed):
    the before-counts are None and rs_total is the most any candidate has. A given
    person_id (a player chosen by personId in the app) is used as is.
    """
    import game_types
    import rosters
    from zoneinfo import ZoneInfo
    index = index or _cached_person_index(conn)
    day, opp_nickname = _day_and_opponent(game_date, opponent_team)
    gd = day[0]
    if person_id is None:
        person_id, _ = _person_id(conn, player_name.strip(), index, day, opp_nickname)
    rs = ", ".join(f"'{t}'" for t in REGULAR_SEASON_GAME_TYPES)

    def rs_total(pid):
        return conn.execute(f"SELECT COUNT(*) FROM player_stats WHERE personId = ? AND gameType IN ({rs})",
                            (pid,)).fetchone()[0]

    if person_id is None:
        exact, loose = index
        ids = exact.get(_ascii_normalize(player_name.strip())) or loose.get(rosters.name_key(player_name)) or set()
        return {"person_id": None, "rs_total": max((rs_total(i) for i in ids), default=0),
                "rs_before_season": None, "counted_before_game": None}
    before_season = conn.execute(
        f"SELECT COUNT(*) FROM player_stats WHERE personId = ? AND gameType IN ({rs}) "
        f"AND gameDateTimeEst < ?", (person_id, SEASON_2026_27_START)).fetchone()[0]
    before_game = conn.execute(
        f"SELECT COUNT(*) FROM player_stats WHERE personId = ? AND gameType IN ({game_types.COUNTED_SQL}) "
        f"AND gameDateTimeEst < ?", (person_id, gd)).fetchone()[0]
    return {"person_id": person_id, "rs_total": rs_total(person_id),
            "rs_before_season": before_season, "counted_before_game": before_game}


def is_limited_data(counts: dict, explanation: str = "") -> bool:
    """No regular-season game before 2026-27, fewer than LIMITED_DATA_MIN_GAMES counted
    games before the game, a name that can't be pinned to one player, or the predictor's
    fixed 'Insufficient game log data' answer."""
    if (explanation or "").startswith(INSUFFICIENT_DATA_PREFIX):
        return True
    if counts["person_id"] is None:
        return True
    return counts["rs_before_season"] == 0 or counts["counted_before_game"] < LIMITED_DATA_MIN_GAMES


def _prediction_game_date(game_date: str | None, timestamp: str | None) -> str | None:
    """The row's game_date, else the ET date of its (naive UTC) timestamp."""
    if game_date:
        return game_date
    from zoneinfo import ZoneInfo
    try:
        return (datetime.fromisoformat(timestamp).replace(tzinfo=ZoneInfo("UTC"))
                .astimezone(ZoneInfo("America/New_York")).date().isoformat())
    except (TypeError, ValueError):
        return None


def backfill_limited_data(conn=None) -> dict:
    """
    (Re)compute predictions.is_limited_data for every NBA row from player_stats, only
    games before each row's game date; WNBA rows are 0. Startup runs it (init_db), so
    rows logged before a box score was loaded are corrected. Returns counts.
    """
    own = conn is None
    conn = conn or get_connection()
    try:
        try:
            conn.execute("SELECT 1 FROM player_stats LIMIT 1")
        except sqlite3.OperationalError:
            return {"skipped": "no player_stats"}
        index = _cached_person_index(conn)
        rows = conn.execute("SELECT id, player_name, opponent_team, game_date, timestamp, explanation, "
                            "COALESCE(league, 'NBA') AS league FROM predictions").fetchall()
        updates, n_limited = [], 0
        for r in rows:
            if r["league"] != "NBA":
                flag = 0
            else:
                gd = _prediction_game_date(r["game_date"], r["timestamp"])
                counts = player_data_counts(conn, r["player_name"], gd, r["opponent_team"], index)
                flag = int(is_limited_data(counts, r["explanation"]))
            n_limited += flag
            updates.append((flag, r["id"]))
        conn.executemany("UPDATE predictions SET is_limited_data = ? WHERE id = ?", updates)
        conn.commit()
        return {"rows": len(rows), "limited": n_limited}
    finally:
        if own:
            conn.close()


def _resolve_decision(conn, row, index: tuple[dict, dict], available_cols: set) -> dict | None:
    """
    What the resolver does with one pending prediction (writes nothing).
    The name is mapped to a personId once (_person_id), and box scores are matched
    by personId, so a box-score name change (a new 'Jr.') can't hide a game.
    Returns None for rows it can't evaluate (unmapped stat type, bad date), else a
    dict with "result" (OVER / UNDER / DNP, or None to leave pending), "kind",
    "matched" (the player_stats row used, or the matchup checked for a DNP) and
    "warning" (why a row the resolver could see is left pending).
    """
    from zoneinfo import ZoneInfo
    stat_cols = _RESOLVE_STAT_COLS.get(row["stat_type"])
    if not stat_cols:
        return None

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

    person_id, why = _person_id(conn, player_name, index, day, opp_nickname)
    out = {"id": row["id"], "player": player_name, "person_id": person_id,
           "stat": row["stat_type"], "game_date": game_date_val, "opponent": opp,
           "result": None, "kind": "pending", "matched": None, "warning": why}
    if person_id is None:
        return out

    select_cols = [c for c in dict.fromkeys(stat_cols + ["gameDateTimeEst", "numMinutes",
                                                         *DNP_COUNTING_COLS])
                   if c in available_cols]
    day_rows = conn.execute(
        f"SELECT {', '.join(select_cols)}, playerteamName, opponentteamName "
        "FROM player_stats WHERE gameDateTimeEst >= ? AND gameDateTimeEst < ? AND personId = ?",
        (*day, person_id),
    ).fetchall()

    game = next((g for g in day_rows if g["opponentteamName"] == opp_nickname), None)
    if game:
        minutes_val = game["numMinutes"] if "numMinutes" in select_cols else None
        out["matched"] = {"date": game["gameDateTimeEst"], "team": game["playerteamName"],
                          "opponent": game["opponentteamName"], "minutes": minutes_val}
        if not played(minutes_val, [game[c] for c in DNP_COUNTING_COLS if c in select_cols]):
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

    # No row for his personId that day. DNP only if his team played the stored
    # opponent that day (that game is loaded and he is not in it).
    last = conn.execute(
        "SELECT playerteamName FROM player_stats WHERE gameDateTimeEst < ? AND personId = ? "
        "ORDER BY gameDateTimeEst DESC LIMIT 1",
        (game_date_val, person_id),
    ).fetchone()
    # His team mirrors the predictor (data_fetcher.get_player_context): the ESPN
    # roster team from rosters.current_team(), else his latest box-score team. The
    # roster is a current snapshot, so it is used only when game_date falls in the
    # roster's season.
    import rosters
    team, team_source = None, None
    if _nba_season(gd) == _roster_season(conn):
        roster = rosters.current_team(player_name, person_id)
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
    # Name -> personId index, read once before any write lock is taken.
    index = _person_index(conn)
    for row in pending:
        d = _resolve_decision(conn, row, index, available_cols)
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
    try:
        return [d for _, d in _resolve_decisions(conn, pending) if d]
    finally:
        conn.close()


def resolve_pending_predictions() -> int:
    """
    Fill predictions.actual_result (OVER / UNDER / DNP) from player_stats.
    The player name is mapped once to a personId (exact full name, else without
    Jr./II suffix; no match or two personIds: pending with a warning), and box
    scores are matched by that personId, never by name.
    A prediction is graded only against the player's row on game_date (ET) whose
    opponent is the stored opponent. If he has a row that day against another
    team, or has none and the stored opponent didn't play his team that day, the
    row is left pending with a warning: never graded, never marked DNP.
    No row for his personId while that matchup is loaded means DNP (absent from
    box score); his team is the ESPN roster team when
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
    """
    Ask Kaggle for the dataset's newest Ready version number (metadata only, no download).
    current_version_number lags the versions list: on 2026-10-23 at 09:46 ET it still
    said 522 while v523 (created 09:34 ET) was listed as Ready. So take the newest Ready
    entry in `versions`, never lower than current_version_number.
    """
    from kagglehub.clients import build_kaggle_client
    from kagglesdk.datasets.types.dataset_api_service import ApiGetDatasetRequest
    with build_kaggle_client() as c:
        d = c.datasets.dataset_api_client.get_dataset(_kaggle_dataset_request(ApiGetDatasetRequest))
    ready = [int(v.version_number) for v in d.versions or [] if v.status == "Ready"]
    return max([int(d.current_version_number), *ready])


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


def kaggle_dataset_cache_dir() -> str:
    """Where kagglehub keeps this dataset's versions: KAGGLEHUB_CACHE (kagglehub 1.0.1,
    kagglehub/config.py; default ~/.cache/kagglehub) / datasets / owner / slug."""
    from kagglehub.config import get_cache_folder
    owner, slug = KAGGLE_DATASET.split("/")
    return os.path.join(get_cache_folder(), "datasets", owner, slug)


def prune_kaggle_cache(keep_version: int, dataset_dir: str | None = None) -> dict:
    """
    After a successful load of keep_version, delete every OLDER cached version: its
    versions/<N> folder and kagglehub's completion markers (<N>.complete for whole-dataset
    downloads, .complete/<N>/ for per-file ones). keep_version and anything newer stay.
    Never raises: a failed delete is reported and the ETL result stands.
    Returns {"removed": [N, ...], "freed_bytes": int, "errors": [...]}.
    """
    import shutil
    base = dataset_dir or kaggle_dataset_cache_dir()
    out = {"removed": [], "freed_bytes": 0, "errors": []}
    vdir = os.path.join(base, "versions")
    if not os.path.isdir(vdir):
        return out
    for name in sorted(os.listdir(vdir)):
        if not name.isdigit() or int(name) >= keep_version:
            continue
        path = os.path.join(vdir, name)
        size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(path) for f in fs)
        try:
            shutil.rmtree(path)
            for marker in (os.path.join(base, f"{name}.complete"), os.path.join(base, ".complete", name)):
                if os.path.isdir(marker):
                    shutil.rmtree(marker)
                elif os.path.exists(marker):
                    os.remove(marker)
        except OSError as exc:
            out["errors"].append(f"v{name}: {exc!r}")
            continue
        out["removed"].append(int(name))
        out["freed_bytes"] += size
    if out["removed"] or out["errors"]:
        print(f"Kaggle cache pruned: removed {out['removed']} ({out['freed_bytes'] / 1e9:.1f} GB), "
              f"kept v{keep_version}+{'; errors: ' + '; '.join(out['errors']) if out['errors'] else ''}")
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


def _snapshot_minutes(conn) -> bool:
    """
    Copy (gameId, personId, numMinutes) of the loaded player_stats into TEMP
    prev_minutes before run_etl replaces it. False if there is no player_stats yet.
    """
    conn.execute("DROP TABLE IF EXISTS temp.prev_minutes")
    try:
        conn.execute("CREATE TEMP TABLE prev_minutes AS "
                     "SELECT gameId, personId, numMinutes FROM main.player_stats")
    except sqlite3.OperationalError:
        return False
    conn.execute("CREATE INDEX temp.idx_prev_minutes ON prev_minutes(gameId, personId)")
    return True


def _guard_null_minutes(conn, table: str = "player_stats") -> dict:
    """
    After a new version is loaded into `table` (the staging table, while the old
    player_stats is still live): where numMinutes is NULL but the previous version
    had a value for the same (gameId, personId), keep the previous value (Kaggle v523
    blanked all 27 rows of 22600088). Also warn about games new in this version with
    NULL minutes and counting stats present. Prints one warning per case; returns
    {"restored": {gameId: rows}, "new_null_games": {gameId: rows}}.
    """
    prev = ("SELECT p.numMinutes FROM temp.prev_minutes p WHERE p.gameId = s.gameId "
            "AND p.personId = s.personId AND p.numMinutes IS NOT NULL")
    restored = dict(conn.execute(
        f'SELECT gameId, COUNT(*) FROM main."{table}" s WHERE numMinutes IS NULL '
        f"AND EXISTS ({prev}) GROUP BY gameId").fetchall())
    if restored:
        conn.execute(f'UPDATE main."{table}" AS s SET numMinutes = ({prev} LIMIT 1) '
                     f"WHERE numMinutes IS NULL AND EXISTS ({prev})")
        print(f"  WARNING: new Kaggle version has NULL numMinutes where the previous one had a "
              f"value: kept the previous value for {sum(restored.values())} row(s) in "
              f"{len(restored)} game(s): {restored}", flush=True)
    has_stats = " OR ".join(f"s.{c} > 0" for c in DNP_COUNTING_COLS)
    new_null = dict(conn.execute(
        f'SELECT gameId, COUNT(*) FROM main."{table}" s WHERE numMinutes IS NULL '
        f"AND ({has_stats}) AND NOT EXISTS "
        f"(SELECT 1 FROM temp.prev_minutes p WHERE p.gameId = s.gameId) GROUP BY gameId").fetchall())
    if new_null:
        print(f"  WARNING: {sum(new_null.values())} row(s) in {len(new_null)} game(s) new in this "
              f"version have NULL numMinutes with counting stats present: {new_null}", flush=True)
    conn.commit()
    conn.execute("DROP TABLE temp.prev_minutes")
    return {"restored": restored, "new_null_games": new_null}


ETL_SWAP_TABLES = ("player_stats", "players", "schedule")
STAGING_SUFFIX = "__new"


def run_etl() -> bool:
    """
    _run_etl under proc_lock.ETL_LOCK (cross-process). A second caller waits, then
    finds the version already loaded and skips: two loads never run in parallel.
    Re-entrant, so morning maintenance (which holds the lock) can call it.
    """
    import proc_lock
    proc_lock.ETL_LOCK.acquire(who="run_etl")
    try:
        return _run_etl()
    finally:
        proc_lock.ETL_LOCK.release()


def _swap_in(conn, version: int) -> None:
    """
    In ONE transaction: replace player_stats, players and schedule with their staging
    tables (which already carry their indexes) and record `version`. Readers (WAL)
    see the old tables until COMMIT and the new ones after, never a partial load.
    """
    conn.commit()
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for t in ETL_SWAP_TABLES:
                conn.execute(f'DROP TABLE IF EXISTS main."{t}"')
                conn.execute(f'ALTER TABLE main."{t}{STAGING_SUFFIX}" RENAME TO "{t}"')
            conn.execute("CREATE TABLE IF NOT EXISTS kaggle_version (version INTEGER)")
            conn.execute("DELETE FROM kaggle_version")
            conn.execute("INSERT INTO kaggle_version (version) VALUES (?)", (version,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.isolation_level = ""


def _drop_staging(conn) -> None:
    for t in ETL_SWAP_TABLES:
        conn.execute(f'DROP TABLE IF EXISTS main."{t}{STAGING_SUFFIX}"')
    conn.commit()


def _run_etl() -> bool:
    """
    Download the Kaggle NBA dataset and load it into SQLite.
    Skips if the stored dataset version already matches the latest Kaggle version.
    A new version is loaded into staging tables (player_stats__new, players__new,
    schedule__new), indexed, NULL-minutes-guarded against the live player_stats, then
    swapped in with kaggle_version in one transaction (_swap_in). Live tables are never
    partial; a failure before the swap drops the staging tables and changes nothing.
    The predictions table is never touched.
    Every file is downloaded before any table is written, so a KaggleVersionUnavailable
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

    stats_new, players_new, schedule_new = (f"{t}{STAGING_SUFFIX}" for t in ETL_SWAP_TABLES)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        _drop_staging(conn)                       # leftovers from a failed run

        # --- PlayerStatisticsExtended.csv → player_stats__new ---
        stats_path = files["stats"]
        print("Loading PlayerStatisticsExtended.csv in chunks (staging)...")
        total_rows = 0
        had_previous = _snapshot_minutes(conn)    # from the live player_stats
        for i, chunk in enumerate(
            pd.read_csv(stats_path, usecols=_STATS_COLS, chunksize=10_000)
        ):
            chunk.to_sql(
                stats_new, conn,
                if_exists="replace" if i == 0 else "append",
                index=False,
            )
            total_rows += len(chunk)
            if (i + 1) % 10 == 0:
                print(f"  ...{total_rows:,} rows loaded")
        if had_previous:
            _guard_null_minutes(conn, stats_new)
        print(f"  Done — {total_rows:,} rows total")

        # --- Players.csv → players__new ---
        players_path = files["players"]
        print("Loading Players.csv (staging)...")
        players_df = pd.read_csv(
            players_path, usecols=["personId", "firstName", "lastName"]
        )
        players_df.to_sql(players_new, conn, if_exists="replace", index=False)
        print(f"  Done — {len(players_df):,} players")

        # --- LeagueSchedule<current season>.csv → schedule__new ---
        schedule_path = files["schedule"]
        print(f"Loading {os.path.basename(schedule_path)} (staging)...")
        schedule_df = read_schedule_csv(schedule_path)
        schedule_df = schedule_df[schedule_df["homeTeamId"] != 0]
        schedule_df.to_sql(schedule_new, conn, if_exists="replace", index=False)
        print(f"  Done — {len(schedule_df):,} games")

        # --- Indexes, on the staging tables (index names must be unique per DB; they
        # move with their table on rename, and drop with the old table) ---
        print("Creating indexes...")
        sfx = f"{time.time_ns()}"
        conn.execute(f"CREATE INDEX idx_stats_person_{sfx} ON {stats_new}(personId)")
        conn.execute(f"CREATE INDEX idx_stats_date_{sfx}   ON {stats_new}(gameDateTimeEst)")
        conn.execute(f"CREATE INDEX idx_schedule_home_{sfx} ON {schedule_new}(homeTeamId)")
        conn.execute(f"CREATE INDEX idx_schedule_away_{sfx} ON {schedule_new}(awayTeamId)")
        conn.commit()

        # --- Swap in, with the version we just loaded, in one transaction ---
        _swap_in(conn, latest_version)
    except BaseException:
        try:
            _drop_staging(conn)
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

    print("ETL complete.")
    prune_kaggle_cache(latest_version)    # older cached versions are never read again
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
