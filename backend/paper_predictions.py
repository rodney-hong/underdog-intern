"""
paper_predictions.py
Paper-mode record of the FROZEN 2026-27 Points model (experiments/preregistration.md).

After each successful scheduled pre-tip pull, one row per Underdog Points line in that
pull: the frozen model's P(over), regardless of confidence, tagged with the model
file's SHA-256. No betting logic, no thresholds, no API calls: every input is read from
the local database (line_snapshots, player_stats, schedule).

  app_path   what the app itself would have used for this player: "model" (>= 10
             counted games), "heuristic" (1-9; the heuristic scorer, whose output
             depends on ESPN injury notes) or "insufficient_data" (0; a fixed OVER 0.52
             answer). The frozen model's
             P(over) is logged for every row either way; "heuristic" rows are
             outside the model's training distribution (training requires 10 prior games).
  status     "ok", or why no probability could be produced (unmatched player, no
             counted games, team mismatch).

Separate table from `predictions` so it never mixes with user picks or the UI history.
resolve_paper_predictions() fills actual_points / result from box scores later (for the
December check).
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

STAT_TYPE = "Points"
MARKET_KEY = "player_points"
MODEL_FILE = "model_Points.pkl"
# Recorded in experiments/preregistration.md for the frozen 2026-27 model.
FROZEN_SHA256 = "50a26f530ef7116b7431d982da1c1f4523294a043034268d61ecc7fd5070a8aa"
MODEL_DIR = os.path.dirname(os.path.abspath(__file__))

_model = None
_model_sha = None


def get_db(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_tables(conn) -> None:
    conn.executescript("""
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
            opponent_team       TEXT,
            is_home             INTEGER,
            is_back_to_back     INTEGER,
            stat_type           TEXT NOT NULL,
            ud_line             REAL,
            ud_over_price       REAL,
            ud_under_price      REAL,
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
            actual_points       REAL,
            result              TEXT,            -- OVER / UNDER / PUSH / DNP
            resolved_at         TEXT,
            UNIQUE (game_id, player_name, captured_at)
        );
        CREATE INDEX IF NOT EXISTS idx_paper_pred_date ON paper_predictions (game_date);
    """)
    conn.commit()


def _load_model():
    global _model, _model_sha
    if _model is None:
        path = os.path.join(MODEL_DIR, MODEL_FILE)
        with open(path, "rb") as f:
            _model_sha = hashlib.sha256(f.read()).hexdigest()
        _model = joblib.load(path)
    return _model, _model_sha


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


def log_for_pull(game_id: str, captured_at: str | None = None, conn=None) -> dict:
    """
    One paper prediction per Underdog Points line in the pull (game_id, captured_at).
    captured_at defaults to the game's latest pretip capture. Idempotent.
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

    snap = conn.execute(
        "SELECT * FROM line_snapshots WHERE game_id = ? AND captured_at = ? AND market_key = ?",
        (game_id, captured_at, MARKET_KEY)).fetchall()
    ud_players = sorted({r["player_name"] for r in snap if r["bookmaker"] == "underdog"})
    model, sha = _load_model()
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    logged = 0
    for player in ud_players:
        rows = [r for r in snap if r["player_name"] == player]
        g = rows[0]
        ud = _book_lines(rows, "underdog")
        fd = _book_lines(rows, "fanduel")
        dk = _book_lines(rows, "draftkings")
        rec = {
            "created_at": now, "captured_at": captured_at, "game_id": game_id,
            "game_date": g["game_date"], "commence_time": g["commence_time"],
            "home_team": g["home_team"], "away_team": g["away_team"], "player_name": player,
            "stat_type": STAT_TYPE,
            "ud_line": ud[0], "ud_over_price": ud[1], "ud_under_price": ud[2],
            "fd_line": fd[0], "fd_over_price": fd[1], "fd_under_price": fd[2],
            "dk_line": dk[0], "dk_over_price": dk[1], "dk_under_price": dk[2],
            "model_file": MODEL_FILE, "model_sha256": sha, "model_sha_ok": int(sha == FROZEN_SHA256),
        }
        try:
            rec.update(_predict_one(player, g, ud[0], model, data_fetcher, predictor, FEATURE_COLS))
        except Exception as exc:                       # one bad row never stops the rest
            rec["status"] = f"error: {exc!r}"[:300]
        cols = ", ".join(rec)
        cur = conn.execute(f"INSERT OR IGNORE INTO paper_predictions ({cols}) "
                           f"VALUES ({', '.join('?' * len(rec))})", tuple(rec.values()))
        logged += cur.rowcount
    conn.commit()
    if own:
        conn.close()
    return {"logged": logged, "lines": len(ud_players), "captured_at": captured_at,
            "model_sha_ok": sha == FROZEN_SHA256}


def _predict_one(player, g, line, model, data_fetcher, predictor, FEATURE_COLS) -> dict:
    person_id = data_fetcher.get_player_id(player)
    if person_id is None:
        return {"status": "unmatched player"}
    ctx = data_fetcher.get_player_context(player)
    if ctx is None:
        return {"person_id": person_id, "status": "no team context"}
    team_nick = _nickname(ctx.get("player_team_full", ""))
    if team_nick == _nickname(g["home_team"]):
        is_home, player_team, opp = 1, g["home_team"], g["away_team"]
    elif team_nick == _nickname(g["away_team"]):
        is_home, player_team, opp = 0, g["away_team"], g["home_team"]
    else:
        return {"person_id": person_id, "player_team": ctx.get("player_team_full"),
                "status": "team mismatch: player's current team is not in this game"}
    b2b = int(bool(ctx.get("is_back_to_back")))
    logs = data_fetcher.get_game_logs(player, last_n=40)
    counted = 0 if logs.empty else len(logs)
    base = {"person_id": person_id, "player_team": player_team, "opponent_team": opp,
            "is_home": is_home, "is_back_to_back": b2b, "counted_games": counted,
            # mirrors predictor.predict(): 0 games -> fixed "insufficient data" answer,
            # 1-9 -> heuristic scorer, >= 10 -> model
            "app_path": "model" if counted >= 10 else ("heuristic" if counted else "insufficient_data")}
    feats = predictor._compute_ml_features(player, opp, STAT_TYPE, line, bool(is_home),
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
    """Fill actual_points / result from player_stats once that game's box score exists."""
    import game_types
    own = conn is None
    conn = conn or get_db()
    init_tables(conn)
    pending = conn.execute("SELECT id, person_id, game_date, ud_line, player_team FROM paper_predictions "
                           "WHERE result IS NULL AND person_id IS NOT NULL").fetchall()
    n = 0
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for r in pending:
        team_played = conn.execute(
            f"SELECT 1 FROM player_stats WHERE substr(gameDateTimeEst,1,10) = ? AND playerteamName = ? "
            f"AND gameType IN ({game_types.COUNTED_SQL}) LIMIT 1",
            (r["game_date"], _nickname(r["player_team"]))).fetchone()
        if not team_played:
            continue                                   # box score not loaded yet
        row = conn.execute(
            "SELECT points, numMinutes FROM player_stats WHERE personId = ? "
            "AND substr(gameDateTimeEst,1,10) = ?", (r["person_id"], r["game_date"])).fetchone()
        if row is None or not row["numMinutes"]:
            actual, result = None, "DNP"
        else:
            actual = float(row["points"])
            result = "PUSH" if actual == r["ud_line"] else ("OVER" if actual > r["ud_line"] else "UNDER")
        conn.execute("UPDATE paper_predictions SET actual_points = ?, result = ?, resolved_at = ? "
                     "WHERE id = ?", (actual, result, now, r["id"]))
        n += 1
    conn.commit()
    if own:
        conn.close()
    return n
