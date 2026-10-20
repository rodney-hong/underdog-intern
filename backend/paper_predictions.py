"""
paper_predictions.py
Paper-mode record of the FROZEN 2026-27 Points, Rebounds, Assists and 3PM models
(experiments/preregistration.md). H1/H2 are Points-only; the other stats are exploratory.

After each successful scheduled pre-tip pull, one row per Underdog line per stat in that
pull: that stat's frozen model's P(over), regardless of confidence, tagged with the model
file's SHA-256 and whether it matches the hash recorded in preregistration.md. No betting
logic, no thresholds, no API calls: every input is read from the local database
(line_snapshots, player_stats, schedule).

  app_path   what the app itself would have used for this player: "model" (>= 10
             counted games), "heuristic" (1-9; the heuristic scorer, whose output
             depends on ESPN injury notes) or "insufficient_data" (0; a fixed OVER 0.52
             answer). The frozen model's
             P(over) is logged for every row either way; "heuristic" rows are
             outside the model's training distribution (training requires 10 prior games).
  status     "ok", or why no probability could be produced (unmatched player, no
             counted games, team mismatch).

Separate table from `predictions` so it never mixes with user picks or the UI history.
resolve_paper_predictions() fills actual_value (and actual_points, Points rows only) /
result from box scores later (for the December check), using each stat's own column.
"""

import os
import json
import sqlite3
import hashlib
import datetime as dt

import joblib
import numpy as np
import pandas as pd

from database import DB_PATH
from line_logger import STAT_MARKETS

# Per stat: frozen model file, its SHA-256 as recorded in experiments/preregistration.md
# ("Frozen 2026-27 NBA model"), and the player_stats box-score column it is graded on.
#
# Underdog's lines arrive mostly in each stat's _alternate market (probe 2026-10-20) but
# also in its main market (opening-day check pull, 2026-10-20), so both are read
# (STAT_MARKETS); ud_market records which one each line came from. FanDuel/DraftKings
# fd_*/dk_* columns keep the stat's main-market line; their alternate ladders stay in
# line_snapshots.
STATS = {
    "Points":   {"model_file": "model_Points.pkl",   "column": "points",
                 "sha256": "50a26f530ef7116b7431d982da1c1f4523294a043034268d61ecc7fd5070a8aa"},
    "Rebounds": {"model_file": "model_Rebounds.pkl", "column": "reboundsTotal",
                 "sha256": "71d16a7239c7f6f7147b8484d67ccf9daa68a08fd8d737cc264394cceba05ee6"},
    "Assists":  {"model_file": "model_Assists.pkl",  "column": "assists",
                 "sha256": "19b0ac96bedae5b09fa6421e907cf7d3264cd1bb30ff73a2abd57353ae7cbb74"},
    "3PM":      {"model_file": "model_3PM.pkl",      "column": "threePointersMade",
                 "sha256": "b5e94bb660619f9d316f8a270dda66b7781abc6c9d6efa2456d9cf250208bcd8"},
}
MODEL_DIR = os.path.dirname(os.path.abspath(__file__))

_models: dict = {}   # stat -> (model, sha256 of the file as loaded)


def get_db(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


_PAPER_DDL = """
        CREATE TABLE IF NOT EXISTS paper_predictions (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at          TEXT NOT NULL,
            captured_at         TEXT NOT NULL,   -- line_snapshots pull this row comes from
            game_id             TEXT NOT NULL,
            game_date           TEXT NOT NULL,
            commence_time       TEXT,
            home_team           TEXT,
            away_team           TEXT,
            player_name         TEXT NOT NULL,   -- as OddsAPI writes it
            person_id           INTEGER,         -- matched player_stats personId
            player_team         TEXT,
            team_source         TEXT,            -- 'roster' / 'box_score' (get_player_context)
            opponent_team       TEXT,
            is_home             INTEGER,
            is_back_to_back     INTEGER,
            stat_type           TEXT NOT NULL,   -- Points / Rebounds / Assists / 3PM
            ud_line             REAL,
            ud_over_price       REAL,
            ud_under_price      REAL,
            ud_market           TEXT,            -- market(s) the Underdog line came from
            fd_line             REAL,
            fd_over_price       REAL,
            fd_under_price      REAL,
            dk_line             REAL,
            dk_over_price       REAL,
            dk_under_price      REAL,
            counted_games       INTEGER,         -- in the 40-game log the app reads
            app_path            TEXT,            -- 'model' / 'heuristic' / 'insufficient_data' / NULL
            p_over              REAL,            -- frozen model, every line
            nan_features        TEXT,            -- features that were NaN and filled with 0
            features_json       TEXT,
            model_file          TEXT,
            model_sha256        TEXT,
            model_sha_ok        INTEGER,         -- 1 if it matches the recorded frozen hash
            status              TEXT NOT NULL,
            actual_points       REAL,            -- Points rows only (kept for older queries)
            actual_value        REAL,            -- box-score value of stat_type
            result              TEXT,            -- OVER / UNDER / PUSH / DNP
            resolved_at         TEXT,
            UNIQUE (game_id, player_name, stat_type, captured_at)
        )"""
_PAPER_BACKUP = "paper_predictions_pre_stat_key"
_OLD_KEY = ("game_id", "player_name", "captured_at")


def _unique_keys(conn, table: str) -> list[tuple]:
    keys = []
    for idx in conn.execute(f"PRAGMA index_list({table})").fetchall():
        if idx[2]:                                     # unique
            keys.append(tuple(r[2] for r in conn.execute(f"PRAGMA index_info('{idx[1]}')")))
    return keys


def _migrate_stat_key(conn: sqlite3.Connection) -> None:
    """
    One-time, atomic migration from UNIQUE (game_id, player_name, captured_at), under
    which INSERT OR IGNORE would silently drop every stat after the first for a player
    in a pull, to UNIQUE (game_id, player_name, stat_type, captured_at). In ONE
    transaction: the old table is renamed to _PAPER_BACKUP and kept as a backup, the
    new table is created under the real name, every row is copied (ids kept; plain
    INSERT, so a conflict raises instead of dropping a row), actual_value is filled
    from actual_points (every existing row is Points), and row counts are compared
    before COMMIT. Any error rolls the whole migration back.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(paper_predictions)")]
    if not cols or _OLD_KEY not in _unique_keys(conn, "paper_predictions"):
        return
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (_PAPER_BACKUP,)).fetchone():
        raise RuntimeError(f"{_PAPER_BACKUP} already exists; not migrating over it")
    if conn.in_transaction:
        raise RuntimeError("paper_predictions migration needs a connection with no open transaction")
    new_cols = [c for c in cols if c != "actual_value"]
    col_list = ", ".join(new_cols)
    extra_dst, extra_src = (", actual_value", ", actual_points") if "actual_points" in cols else ("", "")
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        n_old = conn.execute("SELECT COUNT(*) FROM paper_predictions").fetchone()[0]
        conn.execute(f"ALTER TABLE paper_predictions RENAME TO {_PAPER_BACKUP}")
        conn.execute("DROP INDEX IF EXISTS idx_paper_pred_date")  # moved with the rename
        conn.execute(_PAPER_DDL)
        conn.execute(f"INSERT INTO paper_predictions ({col_list}{extra_dst}) "
                     f"SELECT {col_list}{extra_src} FROM {_PAPER_BACKUP} ORDER BY id")
        n_bk = conn.execute(f"SELECT COUNT(*) FROM {_PAPER_BACKUP}").fetchone()[0]
        n_new = conn.execute("SELECT COUNT(*) FROM paper_predictions").fetchone()[0]
        if not (n_old == n_bk == n_new):
            raise RuntimeError(f"paper_predictions migration: {n_old} rows before, "
                               f"{n_bk} in backup, {n_new} copied")
        conn.execute("COMMIT")
        print(f"[paper_predictions] migrated to UNIQUE (game_id, player_name, stat_type, "
              f"captured_at): {n_new} rows copied; old table kept as {_PAPER_BACKUP}")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.isolation_level = ""


def init_tables(conn) -> None:
    _migrate_stat_key(conn)
    conn.executescript(_PAPER_DDL + """;
        CREATE INDEX IF NOT EXISTS idx_paper_pred_date ON paper_predictions (game_date);
    """)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(paper_predictions)")}
    for col, typ in (("ud_market", "TEXT"), ("actual_value", "REAL"), ("team_source", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE paper_predictions ADD COLUMN {col} {typ}")
    conn.commit()


def _load_model(stat: str):
    if stat not in _models:
        path = os.path.join(MODEL_DIR, STATS[stat]["model_file"])
        with open(path, "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()
        _models[stat] = (joblib.load(path), sha)
    return _models[stat]


def _nickname(team: str) -> str:
    """Team nickname, as player_stats stores it ('Trail Blazers' is two words)."""
    if not team:
        return ""
    return "Trail Blazers" if "Blazers" in team else team.split()[-1]


def _book_lines(rows: list, book: str) -> tuple:
    over = next((r for r in rows if r["bookmaker"] == book and r["side"] == "Over"), None)
    under = next((r for r in rows if r["bookmaker"] == book and r["side"] == "Under"), None)
    line = over["point"] if over else (under["point"] if under else None)
    return line, over["price"] if over else None, under["price"] if under else None


def _underdog_line(rows: list) -> dict:
    """
    One player's Underdog rows for one stat, across its main and alternate markets. One
    distinct point -> that line, the market(s) it came from, and per side the price if
    every Underdog row for that side agrees (NULL if the two markets price it
    differently). More than one distinct point -> no line ("multiple" lists them).
    """
    ud = [r for r in rows if r["bookmaker"] == "underdog"]
    points = sorted({r["point"] for r in ud})
    markets = ",".join(sorted({r["market_key"] for r in ud}))
    if len(points) != 1:
        return {"line": None, "over": None, "under": None, "market": markets, "multiple": points}

    def side_price(side):
        prices = {r["price"] for r in ud if r["side"] == side}
        return prices.pop() if len(prices) == 1 else None

    return {"line": points[0], "over": side_price("Over"), "under": side_price("Under"),
            "market": markets, "multiple": None}


def log_for_pull(game_id: str, captured_at: str | None = None, conn=None) -> dict:
    """
    One paper prediction per Underdog line per stat (STATS) in the pull
    (game_id, captured_at). captured_at defaults to the game's latest pretip capture.
    Idempotent. A stat whose model cannot be loaded is reported in by_stat and skipped;
    the other stats are still logged.
    """
    import data_fetcher
    import predictor
    from ml_trainer import FEATURE_COLS

    own = conn is None
    conn = conn or get_db()
    init_tables(conn)
    if captured_at is None:
        r = conn.execute("SELECT MAX(captured_at) FROM line_snapshots WHERE game_id = ? "
                         "AND pull_phase = 'pretip'", (game_id,)).fetchone()
        captured_at = r[0]
    if captured_at is None:
        if own:
            conn.close()
        return {"logged": 0, "reason": "no pretip capture for game"}

    markets = [k for stat in STATS for k in STAT_MARKETS[stat]]
    qs = ", ".join("?" * len(markets))
    snap = conn.execute(
        f"SELECT * FROM line_snapshots WHERE game_id = ? AND captured_at = ? "
        f"AND bookmaker IN ('underdog', 'fanduel', 'draftkings') AND market_key IN ({qs}) "
        f"ORDER BY market_key, outcome_index",
        (game_id, captured_at, *markets)).fetchall()
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    player_cache: dict = {}          # player -> context shared by every stat
    by_stat: dict = {}
    for stat in STATS:
        main_key, alt_key = STAT_MARKETS[stat]
        stat_rows = [r for r in snap if
                     (r["bookmaker"] == "underdog" and r["market_key"] in (main_key, alt_key))
                     or (r["bookmaker"] in ("fanduel", "draftkings") and r["market_key"] == main_key)]
        ud_players = sorted({r["player_name"] for r in stat_rows if r["bookmaker"] == "underdog"})
        if not ud_players:
            by_stat[stat] = {"logged": 0, "lines": 0}
            continue
        try:
            model, sha = _load_model(stat)
        except Exception as exc:
            by_stat[stat] = {"logged": 0, "lines": len(ud_players), "error": f"model load: {exc!r}"[:300]}
            continue
        sha_ok = sha == STATS[stat]["sha256"]
        logged = 0
        for player in ud_players:
            rows = [r for r in stat_rows if r["player_name"] == player]
            g = rows[0]
            ud = _underdog_line(rows)
            fd = _book_lines(rows, "fanduel")
            dk = _book_lines(rows, "draftkings")
            rec = {
                "created_at": now, "captured_at": captured_at, "game_id": game_id,
                "game_date": g["game_date"], "commence_time": g["commence_time"],
                "home_team": g["home_team"], "away_team": g["away_team"], "player_name": player,
                "stat_type": stat,
                "ud_line": ud["line"], "ud_over_price": ud["over"], "ud_under_price": ud["under"],
                "ud_market": ud["market"],
                "fd_line": fd[0], "fd_over_price": fd[1], "fd_under_price": fd[2],
                "dk_line": dk[0], "dk_over_price": dk[1], "dk_under_price": dk[2],
                "model_file": STATS[stat]["model_file"], "model_sha256": sha,
                "model_sha_ok": int(sha_ok),
            }
            if ud["multiple"]:
                # No rule picks among several Underdog points (across both markets): the
                # row is recorded without a line or prediction.
                rec["status"] = f"multiple underdog lines: {ud['multiple']}"
            else:
                try:
                    rec.update(_predict_one(player, g, stat, ud["line"], model, player_cache,
                                            data_fetcher, predictor, FEATURE_COLS))
                except Exception as exc:                   # one bad row never stops the rest
                    rec["status"] = f"error: {exc!r}"[:300]
            cols = ", ".join(rec)
            cur = conn.execute(f"INSERT OR IGNORE INTO paper_predictions ({cols}) "
                               f"VALUES ({', '.join('?' * len(rec))})", tuple(rec.values()))
            logged += cur.rowcount
        by_stat[stat] = {"logged": logged, "lines": len(ud_players), "model_sha_ok": sha_ok}
    conn.commit()
    if own:
        conn.close()
    return {"logged": sum(v["logged"] for v in by_stat.values()),
            "lines": sum(v["lines"] for v in by_stat.values()),
            "captured_at": captured_at,
            "model_sha_ok": all(v.get("model_sha_ok", True) and "error" not in v
                                for v in by_stat.values()),
            "by_stat": by_stat}


def _player_context(player, data_fetcher, cache) -> dict:
    """Player lookups that do not depend on the stat, computed once per pull."""
    if player not in cache:
        person_id = data_fetcher.get_player_id(player)
        ctx = data_fetcher.get_player_context(player) if person_id is not None else None
        logs = data_fetcher.get_game_logs(player, last_n=40) if ctx is not None else None
        cache[player] = {"person_id": person_id, "ctx": ctx, "logs": logs}
    return cache[player]


def _predict_one(player, g, stat, line, model, cache, data_fetcher, predictor, FEATURE_COLS) -> dict:
    pc = _player_context(player, data_fetcher, cache)
    person_id, ctx = pc["person_id"], pc["ctx"]
    if person_id is None:
        return {"status": "unmatched player"}
    if ctx is None:
        return {"person_id": person_id, "status": "no team context"}
    # Current team: ESPN roster, else latest box score (data_fetcher.get_player_context).
    team_nick = _nickname(ctx.get("player_team_full", ""))
    src = {"team_source": ctx.get("team_source")}
    if team_nick == _nickname(g["home_team"]):
        is_home, player_team, opp = 1, g["home_team"], g["away_team"]
    elif team_nick == _nickname(g["away_team"]):
        is_home, player_team, opp = 0, g["away_team"], g["home_team"]
    else:
        return {"person_id": person_id, "player_team": ctx.get("player_team_full"), **src,
                "status": "team mismatch: player's current team is not in this game"}
    b2b = int(bool(ctx.get("is_back_to_back")))
    logs = pc["logs"]
    counted = 0 if logs is None or logs.empty else len(logs)
    base = {"person_id": person_id, "player_team": player_team, **src, "opponent_team": opp,
            "is_home": is_home, "is_back_to_back": b2b, "counted_games": counted,
            # mirrors predictor.predict(): 0 games -> fixed "insufficient data" answer,
            # 1-9 -> heuristic scorer, >= 10 -> model
            "app_path": "model" if counted >= 10 else ("heuristic" if counted else "insufficient_data")}
    feats = predictor._compute_ml_features(player, opp, stat, line, bool(is_home),
                                           bool(b2b), min_games=1)
    if feats is None:
        return {**base, "status": "no counted games"}
    X = pd.DataFrame([feats])[FEATURE_COLS]
    nan_cols = [c for c in FEATURE_COLS if pd.isna(X.at[0, c])]
    X = X.fillna(0).astype(float)                      # training convention (ml_trainer)
    p_over = float(model.predict_proba(X)[0][1])
    return {**base, "p_over": p_over, "nan_features": ",".join(nan_cols) or None,
            "features_json": json.dumps({k: (None if pd.isna(v) else v) for k, v in feats.items()},
                                        default=float),
            "status": "ok"}


def resolve_paper_predictions(conn=None) -> int:
    """
    Fill actual_value / result from player_stats once that game's box score exists,
    using each stat's own column (STATS[stat]["column"]). actual_points is also filled
    for Points rows, as before.
    """
    import game_types
    own = conn is None
    conn = conn or get_db()
    init_tables(conn)
    pending = conn.execute("SELECT id, person_id, game_date, stat_type, ud_line, player_team "
                           "FROM paper_predictions WHERE result IS NULL AND person_id IS NOT NULL "
                           "AND ud_line IS NOT NULL").fetchall()
    n = 0
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for r in pending:
        if r["stat_type"] not in STATS:
            continue
        column = STATS[r["stat_type"]]["column"]
        team_played = conn.execute(
            f"SELECT 1 FROM player_stats WHERE substr(gameDateTimeEst,1,10) = ? AND playerteamName = ? "
            f"AND gameType IN ({game_types.COUNTED_SQL}) LIMIT 1",
            (r["game_date"], _nickname(r["player_team"]))).fetchone()
        if not team_played:
            continue                                   # box score not loaded yet
        row = conn.execute(
            f"SELECT {column} AS value, numMinutes FROM player_stats WHERE personId = ? "
            f"AND substr(gameDateTimeEst,1,10) = ?", (r["person_id"], r["game_date"])).fetchone()
        if row is None or not row["numMinutes"]:
            actual, result = None, "DNP"
        else:
            actual = float(row["value"])
            result = "PUSH" if actual == r["ud_line"] else ("OVER" if actual > r["ud_line"] else "UNDER")
        conn.execute("UPDATE paper_predictions SET actual_value = ?, actual_points = ?, result = ?, "
                     "resolved_at = ? WHERE id = ?",
                     (actual, actual if r["stat_type"] == "Points" else None, result, now, r["id"]))
        n += 1
    conn.commit()
    if own:
        conn.close()
    return n
