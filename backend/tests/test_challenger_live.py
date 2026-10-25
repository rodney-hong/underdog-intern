"""
Challenger live (Stage 3): at a pre-tip pull, challenger_predictions gets one row per
Underdog line per stat with p_over equal to the NB survival function of the logged mean
and dispersion; players with fewer than 5 earlier games get status 'insufficient_history';
the hook runs after the paper rows, inside the pre-tip lock, and leaves paper_predictions
untouched; /predict uses the challenger for NBA Points/Rebounds/Assists/3PM and stores the
model on every row; a modified model file is refused. Temp DBs and a small challenger
fitted on synthetic data; nothing touches predictions.db, the frozen files or the network.
"""
import os
import pickle
import sqlite3
import sys

import numpy as np
import pandas as pd
import pytest
from scipy import stats as sps

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import challenger_log                              # noqa: E402
import data_fetcher                                # noqa: E402
import database                                    # noqa: E402
import line_logger                                 # noqa: E402
import paper_predictions                           # noqa: E402
from challenger import features as cf              # noqa: E402
from challenger import live                        # noqa: E402
from challenger import model as cm                 # noqa: E402
from test_challenger import TEAMS, _db             # noqa: E402

GAME, CAPTURED, TIP = "evt123", "2026-10-25T23:00:00+00:00", "2026-10-25 19:30:00"
HOME, AWAY = TEAMS[0], TEAMS[1]
ROOKIE = 99999


def _small_challenger(db_path):
    """A real Challenger object (HGB minutes x rate, NB) fitted on the synthetic seasons."""
    conn = sqlite3.connect(db_path)
    df = cf.load_all(conn)
    conn.close()
    F = cf.batch_features(df)
    D = pd.concat([df[["numMinutes", *cf.STAT_COLS.values()]], F], axis=1)
    D = D[D["n_prior"] >= 1]
    p = {"learning_rate": 0.1, "max_iter": 20, "max_leaf_nodes": 7, "min_samples_leaf": 20}
    mf = ["min_h3", "min_h10", "min_h30", "rest_days", "b2b", "home", "games_since_absence", "missed_prev"]
    rf = {s: [f"rate_{c}_h10", f"opp_rel_{c}", "home"] for s, c in cf.STAT_COLS.items()}
    ch = cm.Challenger(mf, p, rf, {s: p for s in rf}, {"test": True})
    ch.minutes_model = cm.fit_minutes(D[mf], D["numMinutes"].values, p)
    for s, c in cf.STAT_COLS.items():
        ch.rate_models[s] = cm.fit_rate(D[rf[s]], D[c].values, D["numMinutes"].values, p)
        ch.alpha[s] = 0.15 + 0.01 * len(s)
    return ch


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Synthetic box scores + one pre-tip Underdog pull for HOME vs AWAY on 2026-10-25."""
    path = str(tmp_path / "live.db")
    _db(path)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE line_snapshots (captured_at TEXT, pull_phase TEXT, game_id TEXT, game_date TEXT, "
                 "commence_time TEXT, home_team TEXT, away_team TEXT, bookmaker TEXT, market_key TEXT, "
                 "player_name TEXT, side TEXT, point REAL, price REAL, outcome_index INTEGER)")
    latest = dict(conn.execute("SELECT personId, playerteamId FROM player_stats GROUP BY personId "
                               "HAVING gameDateTimeEst = MAX(gameDateTimeEst)").fetchall())
    home_players = [p for p, t in latest.items() if t == HOME][:2]
    snaps, i = [], 0

    def add(player, market, point, over, under, book="underdog"):
        nonlocal i
        for side, price in (("Over", over), ("Under", under)):
            snaps.append((CAPTURED, "pretip", GAME, "2026-10-25", "2026-10-25T23:40:00Z", f"City N{HOME}",
                          f"City N{AWAY}", book, market, player, side, point, price, i))
            i += 1
    a, b = (f"P{p}" for p in home_players)
    add(a, "player_points", 18.5, 1.0, 1.0)
    add(a, "player_points_alternate", 14.5, 0.85, 1.1)
    add(a, "player_points_alternate", 24.5, 1.4, 0.7)
    add(a, "player_threes", 1.5, 1.0, 1.0)
    add(b, "player_rebounds", 6.5, 1.0, 1.0)
    add(b, "player_points", 11.5, 1.0, 1.0, book="fanduel")        # not Underdog: no row
    add(f"P{ROOKIE}", "player_points", 4.5, 1.0, 1.0)
    conn.executemany("INSERT INTO line_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", snaps)
    conn.commit()
    conn.close()

    ids = {f"P{p}": p for p in latest} | {f"P{ROOKIE}": ROOKIE}
    monkeypatch.setattr(data_fetcher, "get_player_id", lambda name: ids.get(name))
    monkeypatch.setattr(data_fetcher, "get_player_context",
                        lambda name: {"player_team_full": f"City N{latest.get(ids.get(name), HOME)}"})
    monkeypatch.setattr(paper_predictions, "_cutoff", lambda g: pd.Timestamp(TIP))
    ch = _small_challenger(path)
    monkeypatch.setattr(live, "load_frozen", lambda *a, **k: ch)
    return {"path": path, "players": (a, b), "model": ch}


def _rows(path, table="challenger_predictions"):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
    finally:
        conn.close()


# --- logging at a pre-tip pull ---------------------------------------------------------------

def test_one_row_per_underdog_line_with_p_over_from_mean_and_dispersion(env):
    conn = challenger_log.get_db(env["path"])
    res = challenger_log.log_for_pull(GAME, conn=conn)
    conn.close()
    rows = _rows(env["path"])
    a, b = env["players"]
    keys = sorted((r["player_name"], r["stat_type"], r["market_key"], r["line"]) for r in rows)
    assert keys == sorted([(a, "Points", "player_points", 18.5), (a, "Points", "player_points_alternate", 14.5),
                           (a, "Points", "player_points_alternate", 24.5), (a, "3PM", "player_threes", 1.5),
                           (b, "Rebounds", "player_rebounds", 6.5), (f"P{ROOKIE}", "Points", "player_points", 4.5)])
    assert res["logged"] == 6
    ok = [r for r in rows if r["status"] == "ok"]
    assert len(ok) == 5
    for r in ok:
        n = 1.0 / r["alpha"]
        assert r["p_over"] == pytest.approx(sps.nbinom.sf(np.floor(r["line"]), n, n / (n + r["mu"])), abs=1e-12)
        assert r["alpha"] == env["model"].alpha[r["stat_type"]]
        assert r["expected_minutes"] > 0 and r["mu"] == pytest.approx(r["expected_minutes"] * r["rate_per_minute"])
        assert r["model_sha256"] == live.FROZEN["sha256"] and r["model_fingerprint"] == live.FROZEN["fingerprint"]
        assert r["data_as_of"] < TIP and r["tip"] == TIP and r["is_home"] == 1
        assert r["features_json"] and r["n_prior"] >= 5
    pts = {r["line"]: r for r in ok if r["stat_type"] == "Points"}
    assert len({r["mu"] for r in pts.values()}) == 1                    # one distribution, three lines
    assert pts[14.5]["p_over"] > pts[18.5]["p_over"] > pts[24.5]["p_over"]
    assert pts[14.5]["over_price"] == 0.85 and pts[14.5]["under_price"] == 1.1
    rookie = [r for r in rows if r["player_name"] == f"P{ROOKIE}"][0]
    assert rookie["status"] == "insufficient_history" and rookie["p_over"] is None and rookie["mu"] is None
    conn = challenger_log.get_db(env["path"])
    assert challenger_log.log_for_pull(GAME, conn=conn)["logged"] == 0        # idempotent
    conn.close()


def test_features_at_the_pull_equal_the_shared_builder(env):
    """The live row's features are build_features()'s values for a placeholder at the tip."""
    conn = challenger_log.get_db(env["path"])
    challenger_log.log_for_pull(GAME, conn=conn)
    r = [x for x in _rows(env["path"]) if x["status"] == "ok" and x["stat_type"] == "Points"][0]
    F, _ = live.features(conn, r["person_id"], f"N{HOME}", f"N{AWAY}", True, TIP)
    conn.close()
    import json
    for k, v in json.loads(r["features_json"]).items():
        assert (v is None and pd.isna(F.iloc[0][k])) or v == F.iloc[0][k], k


def test_before_effective_date_nothing_is_logged(env):
    conn = sqlite3.connect(env["path"])
    conn.execute("UPDATE line_snapshots SET game_date = '2026-10-24'")
    conn.commit()
    conn.close()
    conn = challenger_log.get_db(env["path"])
    assert challenger_log.log_for_pull(GAME, conn=conn)["logged"] == 0
    conn.close()


def test_hook_runs_after_paper_rows_inside_the_pretip_lock(monkeypatch):
    import datetime as dt
    calls = []
    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
    pulled = iter([False, True])
    monkeypatch.setattr(line_logger, "init_tables", lambda: None)
    monkeypatch.setattr(line_logger, "_commence", lambda e: future)
    monkeypatch.setattr(line_logger, "_already_pulled", lambda *a: next(pulled))
    monkeypatch.setattr(line_logger, "fetch_todays_events", lambda *a, **k: ([], 100))
    monkeypatch.setattr(line_logger, "pull_event", lambda *a, **k: calls.append("pull"))
    monkeypatch.setattr(line_logger, "_last_attempt", lambda *a: None)
    monkeypatch.setattr(line_logger, "_log_paper_predictions",
                        lambda gid: calls.append(("paper", gid, line_logger._PRETIP_LOCK.locked())))
    monkeypatch.setattr(challenger_log, "log_for_pull",
                        lambda gid: calls.append(("challenger", gid, line_logger._PRETIP_LOCK.locked())) or {})
    line_logger.run_pretip_pull({"id": GAME, "home_team": "H", "away_team": "A"})
    assert calls == ["pull", ("paper", GAME, True), ("challenger", GAME, True)]


def test_hook_failure_never_breaks_the_pull(monkeypatch):
    def boom(gid):
        raise live.FrozenModelError("bad hash")
    monkeypatch.setattr(challenger_log, "log_for_pull", boom)
    line_logger._log_challenger_predictions(GAME)          # prints, does not raise


def test_paper_rows_unchanged_by_challenger_logging(env):
    conn = paper_predictions.get_db(env["path"])
    paper_predictions.init_tables(conn)
    conn.execute("INSERT INTO paper_predictions (created_at, captured_at, game_id, game_date, player_name, "
                 "stat_type, ud_line, p_over, model_sha256, status) VALUES "
                 "('x', ?, ?, '2026-10-25', ?, 'Points', 18.5, 0.4321, 'frozen-sha', 'ok')",
                 (CAPTURED, GAME, env["players"][0]))
    conn.commit()
    before = _rows(env["path"], "paper_predictions")
    challenger_log.log_for_pull(GAME, conn=conn)
    conn.close()
    assert _rows(env["path"], "paper_predictions") == before
    assert len(_rows(env["path"])) == 6


# --- the integrity check refuses a modified file ------------------------------------------

def test_hash_and_fingerprint_checks_refuse_a_modified_file(env, tmp_path, monkeypatch):
    monkeypatch.undo()                                       # the real load_frozen
    ch = env["model"]
    path = str(tmp_path / "frozen.pkl")
    sha = cm.save(ch, path)
    fp = cm.fingerprint(ch)
    live._loaded.clear()
    assert live.load_frozen(path, sha, fp) is not None
    live._loaded.clear()
    with open(path, "ab") as f:
        f.write(b"\0")                                       # one byte appended
    with pytest.raises(live.FrozenModelError, match="SHA-256"):
        live.load_frozen(path, sha, fp)
    ch.alpha["Points"] += 1e-9                               # same code path, different content
    path2 = str(tmp_path / "frozen2.pkl")
    sha2 = cm.save(ch, path2)
    with pytest.raises(live.FrozenModelError, match="fingerprint"):
        live.load_frozen(path2, sha2, fp)
    with pytest.raises(live.FrozenModelError, match="missing"):
        live.load_frozen(str(tmp_path / "nope.pkl"), sha, fp)
    live._loaded.clear()


def test_refused_model_writes_no_rows(env, monkeypatch):
    def refuse(*a, **k):
        raise live.FrozenModelError("SHA-256 mismatch")
    monkeypatch.setattr(live, "load_frozen", refuse)
    conn = challenger_log.get_db(env["path"])
    with pytest.raises(live.FrozenModelError):
        challenger_log.log_for_pull(GAME, conn=conn)
    conn.close()
    assert _rows(env["path"]) == []


@pytest.mark.skipif(not os.path.exists(live.FROZEN["path"]), reason="needs the frozen challenger file")
def test_real_frozen_file_passes_both_checks():
    live._loaded.clear()
    assert live.load_frozen().meta["train_seasons"] == ["2012-13", "2024-25"]


# --- the app ---------------------------------------------------------------------------------

@pytest.fixture
def app(tmp_path, monkeypatch):
    path = str(tmp_path / "app.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE predictions (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, "
                 "player_name TEXT NOT NULL, stat_type TEXT NOT NULL, stat_line REAL NOT NULL, "
                 "opponent_team TEXT NOT NULL, predicted_outcome TEXT NOT NULL, confidence REAL NOT NULL, "
                 "explanation TEXT NOT NULL, actual_result TEXT)")
    conn.execute("INSERT INTO predictions (timestamp, player_name, stat_type, stat_line, opponent_team, "
                 "predicted_outcome, confidence, explanation) VALUES ('2026-10-21', 'Old Row', 'Points', 20.5, "
                 "'Boston Celtics', 'OVER', 0.6, 'old')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(data_fetcher, "DB_PATH", path)
    monkeypatch.setattr(database, "backfill_limited_data", lambda conn: {})
    database.init_db()
    from fastapi.testclient import TestClient
    import main
    import predictor
    monkeypatch.setattr(database, "player_data_counts",
                        lambda *a, **k: {"person_id": 7, "rs_total": 300, "rs_before_season": 290,
                                         "counted_before_game": 299})
    monkeypatch.setattr(database, "is_limited_data", lambda *a, **k: False)
    monkeypatch.setattr(data_fetcher, "get_opponent_injuries", lambda team: [])
    monkeypatch.setattr(data_fetcher, "get_player_context", lambda name: {"player_team_full": "Boston Celtics"})
    monkeypatch.setattr(data_fetcher, "scheduled_tip", lambda a, b, d: pd.Timestamp("2026-10-25 19:30:00"))
    monkeypatch.setattr(predictor, "predict", lambda **kw: {"outcome": "OVER", "confidence": 0.61,
                                                            "explanation": "frozen path"})
    seen = {}

    def fake_live(conn, person_id, team, opp, is_home, tip, stat, lines):
        seen.update(person_id=person_id, team=team, opp=opp, tip=tip, stat=stat, lines=lines)
        if seen.get("insufficient"):
            return {"status": "insufficient_history", "n_prior": 3}
        mu, alpha = 17.2, 0.3
        return {"status": "ok", "n_prior": 300, "mu": mu, "alpha": alpha, "expected_minutes": 31.0,
                "rate": mu / 31.0, "p_over": {float(L): float(cm.p_over(mu, alpha, L)) for L in lines},
                "features": {"min_h3": 32.0, "min_h30": 30.5, "rate_points_h10": 0.55, "opp_rel_points": 1.08,
                             "games_since_absence": 10.0, "b2b": 0.0}}
    monkeypatch.setattr(live, "predict", fake_live)
    return {"client": TestClient(main.app), "seen": seen, "path": path}


def _post(client, stat="Points", line=20.5):
    return client.post("/predict", json={"player_name": "Jay Test", "opponent_team": "New York Knicks",
                                         "stat_line": line, "stat_type": stat, "is_home": True,
                                         "game_date": "2026-10-25", "league": "NBA"})


def _models(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT player_name, stat_type, model, predicted_outcome, confidence "
                            "FROM predictions ORDER BY id").fetchall()
    finally:
        conn.close()


def test_existing_app_rows_are_frozen(app):
    assert _models(app["path"]) == [("Old Row", "Points", "frozen", "OVER", 0.6)]


def test_predict_uses_the_challenger_and_stores_the_model(app):
    r = _post(app["client"])
    assert r.status_code == 200
    body = r.json()
    p = float(cm.p_over(17.2, 0.3, 20.5))
    assert body["model"] == "challenger" and body["outcome"] == "UNDER"
    assert body["p_over"] == pytest.approx(p) and body["confidence"] == round(max(p, 1 - p) * 100, 1)
    assert body["explanation"].startswith("Projected Points: 17.2 against the 20.5 line")
    assert "31.0 minutes" in body["explanation"] and "frozen path" not in body["explanation"]
    assert app["seen"] == {"person_id": 7, "team": "Boston Celtics", "opp": "New York Knicks",
                           "tip": "2026-10-25 19:30:00", "stat": "Points", "lines": [20.5]}
    rows = _models(app["path"])
    assert rows[-1][:3] == ("Jay Test", "Points", "challenger")
    hist = app["client"].get("/predictions/history").json()
    assert {h["player_name"]: h["model"] for h in hist} == {"Old Row": "frozen", "Jay Test": "challenger"}


def test_other_stats_stay_on_the_frozen_path(app):
    body = _post(app["client"], stat="PRA", line=30.5).json()
    assert body["model"] == "frozen" and body["explanation"] == "frozen path"
    assert _models(app["path"])[-1][:3] == ("Jay Test", "PRA", "frozen")


def test_fewer_than_five_games_is_refused(app):
    app["seen"]["insufficient"] = True
    r = _post(app["client"], stat="3PM", line=1.5)
    assert r.status_code == 422 and r.json()["detail"] == "Not enough NBA games"
    assert len(_models(app["path"])) == 1                  # only the old row


def test_refused_model_file_is_not_replaced_by_the_frozen_path(app, monkeypatch):
    def refuse(*a, **k):
        raise live.FrozenModelError("SHA-256 mismatch")
    monkeypatch.setattr(live, "predict", refuse)
    r = _post(app["client"])
    assert r.status_code == 503
    assert len(_models(app["path"])) == 1
