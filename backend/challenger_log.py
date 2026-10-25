"""
challenger_log.py
Live record of the challenger (backend/challenger/live.py), as pre-registered in
experiments/preregistration.md (entry of commit a760c8c: after a pass, the challenger logs
its predictions to its own table from the next game day; the frozen 2026-27 models keep
logging paper_predictions unchanged).

At each pre-tip pull, after paper_predictions are logged and inside the same pre-tip lock
(line_logger._run_pretip_pull), one row per Underdog line per stat: every distinct
(market_key, point) Underdog offers that player in the stat's main and alternate markets,
with its prices. The challenger gives P(stat > L) for any line, so every rung is scored.
Effective from game_date EFFECTIVE_DATE.

  status   'ok'; 'insufficient_history' (fewer than 5 earlier played regular-season
           games: no prediction, as in the Stage 2 test population); or why no prediction
           was possible ('unmatched player', 'no team context', 'team mismatch...',
           'error: ...').

Reads the local database only (line_snapshots, player_stats, schedule, rosters). The
frozen file is loaded with its SHA-256 and fingerprint checked; a failed check logs nothing.
resolve_challenger_predictions() fills actual_value / result from box scores.
"""

import datetime as dt
import json
import sqlite3

from database import DB_PATH, DNP_COUNTING_COLS, played
from line_logger import STAT_MARKETS

EFFECTIVE_DATE = "2026-10-25"
COLUMNS = {"Points": "points", "Rebounds": "reboundsTotal", "Assists": "assists", "3PM": "threePointersMade"}

_DDL = """
    CREATE TABLE IF NOT EXISTS challenger_predictions (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at          TEXT NOT NULL,
        captured_at         TEXT NOT NULL,   -- line_snapshots pull this row comes from
        game_id             TEXT NOT NULL,   -- OddsAPI event id
        game_date           TEXT NOT NULL,
        commence_time       TEXT,
        tip                 TEXT,            -- feature cutoff: scheduled tip (ET)
        home_team           TEXT,
        away_team           TEXT,
        player_name         TEXT NOT NULL,   -- as OddsAPI writes it
        person_id           INTEGER,
        player_team         TEXT,
        opponent_team       TEXT,
        is_home             INTEGER,
        stat_type           TEXT NOT NULL,   -- Points / Rebounds / Assists / 3PM
        line                REAL NOT NULL,
        market_key          TEXT NOT NULL,
        over_price          REAL,
        under_price         REAL,
        n_prior             INTEGER,         -- earlier played regular-season games
        mu                  REAL,            -- predicted mean
        alpha               REAL,            -- NB2 dispersion: Var = mu + alpha mu^2
        p_over              REAL,            -- P(stat > line)
        expected_minutes    REAL,
        rate_per_minute     REAL,
        model_sha256        TEXT,
        model_fingerprint   TEXT,
        data_as_of          TEXT,            -- newest box score before tip used
        features_json       TEXT,
        status              TEXT NOT NULL,
        actual_value        REAL,
        result              TEXT,            -- OVER / UNDER / PUSH / DNP
        resolved_at         TEXT,
        UNIQUE (game_id, player_name, stat_type, market_key, line, captured_at)
    );
    CREATE INDEX IF NOT EXISTS idx_challenger_pred_date ON challenger_predictions (game_date);
"""


def get_db(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_tables(conn) -> None:
    conn.executescript(_DDL)
    conn.commit()


def _lines(rows: list) -> list[dict]:
    """Distinct Underdog (market_key, point) for one player and stat, with both prices."""
    out = {}
    for r in rows:
        if r["bookmaker"] != "underdog":
            continue
        k = (r["market_key"], r["point"])
        d = out.setdefault(k, {"market_key": k[0], "line": k[1], "over": None, "under": None})
        d["over" if r["side"] == "Over" else "under"] = r["price"]
    return sorted(out.values(), key=lambda d: (d["market_key"], d["line"]))


def _player_context(player, g, cache, data_fetcher, conn) -> dict:
    """Stat-independent work, once per player per pull: id, team, features."""
    from challenger import live
    from paper_predictions import _cutoff
    if player in cache:
        return cache[player]
    out = {}
    person_id = data_fetcher.get_player_id(player)
    if person_id is None:
        out["status"] = "unmatched player"
    else:
        out["person_id"] = person_id
        ctx = data_fetcher.get_player_context(player)
        team = live.nickname((ctx or {}).get("player_team_full", ""))
        if ctx is None:
            out["status"] = "no team context"
        elif team == live.nickname(g["home_team"]):
            out.update(player_team=g["home_team"], opponent_team=g["away_team"], is_home=1)
        elif team == live.nickname(g["away_team"]):
            out.update(player_team=g["away_team"], opponent_team=g["home_team"], is_home=0)
        else:
            out.update(player_team=ctx.get("player_team_full"),
                       status="team mismatch: player's current team is not in this game")
        if "status" not in out:
            tip = cache.setdefault("_tip", _cutoff(g).strftime("%Y-%m-%d %H:%M:%S"))
            out["tip"] = tip
            try:
                out["F"], out["data_as_of"] = live.features(conn, person_id, out["player_team"],
                                                            out["opponent_team"], out["is_home"], tip)
            except Exception as exc:
                out["status"] = f"error: {exc!r}"[:300]
    cache[player] = out
    return out


def log_for_pull(game_id: str, captured_at: str | None = None, conn=None) -> dict:
    """
    Challenger rows for the pull (game_id, captured_at); captured_at defaults to the game's
    latest pretip capture. Idempotent. Games before EFFECTIVE_DATE are skipped.
    """
    import data_fetcher
    from challenger import live

    own = conn is None
    conn = conn or get_db()
    try:
        init_tables(conn)
        if captured_at is None:
            captured_at = conn.execute("SELECT MAX(captured_at) FROM line_snapshots WHERE game_id = ? "
                                       "AND pull_phase = 'pretip'", (game_id,)).fetchone()[0]
        if captured_at is None:
            return {"logged": 0, "reason": "no pretip capture for game"}
        markets = [k for stat in COLUMNS for k in STAT_MARKETS[stat]]
        snap = conn.execute(
            f"SELECT * FROM line_snapshots WHERE game_id = ? AND captured_at = ? AND bookmaker = 'underdog' "
            f"AND market_key IN ({', '.join('?' * len(markets))}) ORDER BY market_key, outcome_index",
            (game_id, captured_at, *markets)).fetchall()
        if not snap:
            return {"logged": 0, "lines": 0, "captured_at": captured_at}
        if snap[0]["game_date"] < EFFECTIVE_DATE:
            return {"logged": 0, "reason": f"game_date before {EFFECTIVE_DATE}"}
        m = live.load_frozen()               # hash + fingerprint; raises before anything is written
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        cache, pending = {}, []
        for stat in COLUMNS:
            stat_rows = [r for r in snap if r["market_key"] in STAT_MARKETS[stat]]
            for player in sorted({r["player_name"] for r in stat_rows}):
                rows = [r for r in stat_rows if r["player_name"] == player]
                g = rows[0]
                pc = _player_context(player, g, cache, data_fetcher, conn)
                lines = _lines(rows)
                base = {"created_at": now, "captured_at": captured_at, "game_id": game_id,
                        "game_date": g["game_date"], "commence_time": g["commence_time"],
                        "home_team": g["home_team"], "away_team": g["away_team"], "player_name": player,
                        "stat_type": stat, "model_sha256": live.FROZEN["sha256"],
                        "model_fingerprint": live.FROZEN["fingerprint"],
                        **{k: pc.get(k) for k in ("person_id", "player_team", "opponent_team", "is_home",
                                                  "tip", "data_as_of")}}
                pred = {"status": pc["status"]} if "status" in pc else None
                if pred is None:
                    try:
                        pred = live.predict_from_features(pc["F"], stat, [d["line"] for d in lines], m)
                    except Exception as exc:
                        pred = {"status": f"error: {exc!r}"[:300]}
                for d in lines:
                    rec = {**base, "line": d["line"], "market_key": d["market_key"],
                           "over_price": d["over"], "under_price": d["under"], "status": pred["status"],
                           "n_prior": pred.get("n_prior")}
                    if pred["status"] == "ok":
                        rec.update(mu=pred["mu"], alpha=pred["alpha"], p_over=pred["p_over"][float(d["line"])],
                                   expected_minutes=pred["expected_minutes"], rate_per_minute=pred["rate"],
                                   features_json=json.dumps(pred["features"]))
                    pending.append(rec)
        logged = 0
        for rec in pending:                  # one short write transaction at the end
            cur = conn.execute(f"INSERT OR IGNORE INTO challenger_predictions ({', '.join(rec)}) "
                               f"VALUES ({', '.join('?' * len(rec))})", tuple(rec.values()))
            logged += cur.rowcount
        conn.commit()
        statuses = {}
        for rec in pending:
            statuses[rec["status"]] = statuses.get(rec["status"], 0) + 1
        return {"logged": logged, "lines": len(pending), "captured_at": captured_at, "statuses": statuses}
    finally:
        if own:
            conn.close()


def resolve_challenger_predictions(conn=None) -> int:
    """Fill actual_value / result once the player's team has a box score for game_date
    (DNP as database.played: minutes and every counting stat 0/NULL)."""
    import game_types
    from challenger.live import nickname
    own = conn is None
    conn = conn or get_db()
    try:
        init_tables(conn)
        pending = conn.execute("SELECT id, person_id, game_date, stat_type, line, player_team "
                               "FROM challenger_predictions WHERE result IS NULL AND status = 'ok'").fetchall()
        now, n = dt.datetime.now(dt.timezone.utc).isoformat(), 0
        for r in pending:
            if not conn.execute(f"SELECT 1 FROM player_stats WHERE substr(gameDateTimeEst,1,10) = ? "
                                f"AND playerteamName = ? AND gameType IN ({game_types.COUNTED_SQL}) LIMIT 1",
                                (r["game_date"], nickname(r["player_team"]))).fetchone():
                continue
            row = conn.execute(f"SELECT {COLUMNS[r['stat_type']]} AS value, numMinutes, "
                               f"{', '.join(DNP_COUNTING_COLS)} FROM player_stats "
                               "WHERE personId = ? AND substr(gameDateTimeEst,1,10) = ?",
                               (r["person_id"], r["game_date"])).fetchone()
            if row is None or not played(row["numMinutes"], [row[c] for c in DNP_COUNTING_COLS]):
                actual, result = None, "DNP"
            else:
                actual = float(row["value"])
                result = "PUSH" if actual == r["line"] else ("OVER" if actual > r["line"] else "UNDER")
            conn.execute("UPDATE challenger_predictions SET actual_value = ?, result = ?, resolved_at = ? "
                         "WHERE id = ?", (actual, result, now, r["id"]))
            n += 1
        conn.commit()
        return n
    finally:
        if own:
            conn.close()
