"""
v2 inference parity (preregistration entry of 2026-10-23): on a synthetic season in a
temp DB, the v2 feature_defs helpers reproduce ml_trainer.build_training_data's frame
exactly; paper rows carry v2 p_over plus v1 shadow columns and data_as_of /
data_stale; startup catches up a missed maintenance. Nothing touches predictions.db.
"""
import datetime as dt
import json
import os
import sqlite3
import sys

import numpy as np
import pandas as pd
import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
sys.path.insert(0, os.path.join(BACKEND, "experiments"))

import data_fetcher
import database
import feature_defs
import line_logger
import ml_trainer
import paper_predictions
import predictor
import rosters

TEAMS = [(1610612738, "Boston", "Celtics"), (1610612752, "New York", "Knicks"),
         (1610612748, "Miami", "Heat"), (1610612741, "Chicago", "Bulls")]
LAST_GAME = None   # set by _season: datetime of the last synthetic game


def _season(path, seed=7, n_games=70):
    """A synthetic season: 4 teams x 5 players, gaps (incl. 20 days), 0-minute rows,
    NULL defensive ratings, preseason games; plus players and schedule tables."""
    global LAST_GAME
    rng = np.random.default_rng(seed)
    rows, sched = [], []
    t = dt.datetime(2025, 10, 21, 19, 0)
    for g in range(n_games):
        t = t + dt.timedelta(days=20 if g == 40 else int(rng.choice([1, 1, 2, 3])))
        tip = t.replace(hour=int(rng.choice([19, 19, 20, 22])), minute=int(rng.choice([0, 30])))
        h, a = (int(x) for x in rng.choice(4, 2, replace=False))
        gid = 22500001 + g
        gtype = "Preseason" if g % 17 == 5 else "Regular Season"
        sched.append({"gameId": gid, "gameDateTimeEst": tip.strftime("%Y-%m-%d %H:%M:%S"),
                      "homeTeamId": TEAMS[h][0], "awayTeamId": TEAMS[a][0],
                      "homeTeamName": TEAMS[h][2], "homeTeamCity": TEAMS[h][1],
                      "awayTeamName": TEAMS[a][2], "awayTeamCity": TEAMS[a][1]})
        for home, tm, opp in ((1, h, a), (0, a, h)):
            for k in range(5):
                pid = 1000 + tm * 10 + k
                rows.append({
                    "personId": pid, "firstName": f"P{pid}", "lastName": "Test", "gameId": gid,
                    "gameDateTimeEst": tip.strftime("%Y-%m-%d %H:%M:%S"), "gameType": gtype,
                    "home": home, "playerteamId": TEAMS[tm][0], "playerteamCity": TEAMS[tm][1],
                    "playerteamName": TEAMS[tm][2], "opponentteamId": TEAMS[opp][0],
                    "opponentteamCity": TEAMS[opp][1], "opponentteamName": TEAMS[opp][2],
                    "numMinutes": 0.0 if rng.random() < 0.08 else float(rng.uniform(8, 38)),
                    "points": float(rng.integers(0, 35)), "assists": float(rng.integers(0, 10)),
                    "reboundsTotal": float(rng.integers(0, 14)),
                    "reboundsOffensive": 1.0, "reboundsDefensive": 2.0,
                    "threePointersMade": float(rng.integers(0, 6)),
                    "plusMinusPoints": 0.0, "win": 1, "estimatedPace": 99.0, "usagePercentage": 0.2,
                    "defensiveRating": None if rng.random() < 0.05 else float(rng.normal(110, 6)),
                    "blocks": 0.0, "steals": 1.0, "turnovers": 1.0, "doubleDouble": 0,
                    "threePointersAttempted": 4.0,
                })
        LAST_GAME = tip
    conn = sqlite3.connect(path)
    pd.DataFrame(rows).to_sql("player_stats", conn, index=False)
    pd.DataFrame(sched).to_sql("schedule", conn, index=False)
    pd.DataFrame({"personId": [1000 + t * 10 + k for t in range(4) for k in range(5)],
                  "firstName": [f"P{1000 + t * 10 + k}" for t in range(4) for k in range(5)],
                  "lastName": "Test"}).to_sql("players", conn, index=False)
    conn.close()


@pytest.fixture
def season(tmp_path, monkeypatch):
    path = str(tmp_path / "season.db")
    for m in (database, data_fetcher, predictor, paper_predictions, line_logger, rosters, ml_trainer):
        monkeypatch.setattr(m, "DB_PATH", path)
    monkeypatch.setattr(ml_trainer, "USE_REAL_LINES", False)    # no historical_player_props here
    _season(path)
    return path


# --- the four v2 features equal the training frame ------------------------------------

def test_v2_helpers_equal_training_frame(season):
    from trainer_bridge import _capture_build_training_data
    data, per_stat, src = _capture_build_training_data()
    checked = 0
    for stat in ml_trainer._ALL_MODEL_STATS:
        canon = data[data["stat_type"] == stat].reset_index(drop=True)
        ident = src.loc[per_stat[stat]["valid"], ["personId", "gameDateTimeEst", "opponentteamId",
                                                  "home"]].reset_index(drop=True)
        assert len(canon) > 40
        for k in range(0, len(canon), max(1, len(canon) // 40)):
            cutoff = pd.Timestamp(ident.at[k, "gameDateTimeEst"])
            h = data_fetcher.get_player_history_v2(ident.at[k, "personId"])
            h = h[h["gameDateTimeEst"] < cutoff].reset_index(drop=True)
            h["_stat"] = data_fetcher._HISTORY_STAT_EXPR[stat](h)
            win = feature_defs.next_game_window_features(h, cutoff)
            got = {
                "games_played_last15": win["games_played_last15"],
                "fatigue_score": win["fatigue_score"],
                "home_away_split": feature_defs.next_game_home_away_split(h, "_stat", bool(ident.at[k, "home"])),
                "opp_def_rating_last5": feature_defs.opp_def_rating_before(
                    data_fetcher.get_team_rows_v2(ident.at[k, "opponentteamId"], cutoff), cutoff),
            }
            for feat, val in got.items():
                want = canon.at[k, feat]
                assert (pd.isna(want) and pd.isna(val)) or want == val, (stat, k, feat, want, val)
            checked += 1
    assert checked > 300


def test_v1_and_v2_differ_after_a_long_gap(season):
    """The 20-day gap: v1 anchors at the last game, v2 (as training) at the target game."""
    pid = 1000
    h = data_fetcher.get_player_history_v2(pid)
    cutoff = pd.Timestamp(LAST_GAME + dt.timedelta(days=25))
    win = feature_defs.next_game_window_features(h, cutoff)
    assert pd.isna(win["games_played_last15"])          # empty window: NaN in training, 0 at input
    feats = predictor._v2_features("P1000 Test", "Miami Heat", "Points", True, cutoff)
    assert feats["games_played_last15"] == 0.0 and feats["fatigue_score"] == 0.0


# --- paper rows: v2 p_over, v1 shadow, freshness ---------------------------------------

class _StubModel:
    """P(over) depends on every feature, so v1 and v2 inputs give different values."""
    def predict_proba(self, X):
        z = float(np.tanh(X.to_numpy().sum() / 1000.0)) * 0.4
        return np.array([[0.5 - z, 0.5 + z]])


def _upcoming_game(path, extra_slate_day=False):
    """Schedule an upcoming Celtics vs Heat game after the season, plus its pre-tip lines."""
    tip = LAST_GAME + dt.timedelta(days=2)
    conn = sqlite3.connect(path)
    rows = [(22500999, tip.strftime("%Y-%m-%d %H:%M:%S"))]
    if extra_slate_day:     # a slate the day before whose box scores are not loaded
        rows.append((22500998, (tip - dt.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")))
    for gid, when in rows:
        conn.execute("INSERT INTO schedule VALUES (?, ?, ?, ?, 'Celtics', 'Boston', 'Heat', 'Miami')",
                     (gid, when, TEAMS[0][0], TEAMS[2][0]))
    conn.commit()
    conn.close()
    line_logger.init_tables()
    commence = (tip + dt.timedelta(hours=4, minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")  # EDT -> UTC
    captured = "2030-01-01T00:00:00+00:00"
    conn = sqlite3.connect(path)
    for pid in (1000, 1001, 1020):
        for side in ("Over", "Under"):
            conn.execute(
                "INSERT INTO line_snapshots (game_id, game_date, commence_time, home_team, away_team, "
                "player_name, market_key, bookmaker, side, point, price, captured_at, pull_phase, "
                "outcome_index) VALUES ('evt1', ?, ?, 'Boston Celtics', 'Miami Heat', ?, "
                "'player_points_alternate', 'underdog', ?, 12.5, -110, ?, 'pretip', 0)",
                (tip.strftime("%Y-%m-%d"), commence, f"P{pid} Test", side, captured))
    conn.commit()
    conn.close()
    return tip, captured


@pytest.fixture
def stub_models(monkeypatch):
    monkeypatch.setattr(paper_predictions, "_load_model",
                        lambda stat: (_StubModel(), paper_predictions.STATS[stat]["sha256"]))


def _paper_rows(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:   # never selects actual_value / result
        return conn.execute("SELECT player_name, status, p_over, p_over_v1, feature_def, features_json, "
                            "features_v1_json, data_as_of, data_stale FROM paper_predictions").fetchall()
    finally:
        conn.close()


@pytest.fixture
def v2_from_synthetic_season(monkeypatch):
    """The synthetic season is dated 2025-26: move the v2 switch before it."""
    monkeypatch.setattr(predictor, "V2_EFFECTIVE_DATE", "2025-01-01")


def test_paper_rows_log_v2_with_v1_shadow(season, stub_models, v2_from_synthetic_season):
    tip, captured = _upcoming_game(season)
    res = paper_predictions.log_for_pull("evt1", captured)
    assert res["logged"] == 3
    rows = _paper_rows(season)
    assert all(r["status"] == "ok" for r in rows)
    for r in rows:
        v2, v1 = json.loads(r["features_json"]), json.loads(r["features_v1_json"])
        assert r["feature_def"] == "v2" and r["p_over_v1"] is not None
        assert r["data_as_of"] == LAST_GAME.strftime("%Y-%m-%d") and r["data_stale"] == 0
        assert v2["games_played_last15"] != v1["games_played_last15"] or \
            v2["fatigue_score"] != v1["fatigue_score"]
        assert r["p_over"] != r["p_over_v1"]
        # v2 features are exactly predictor's v2 at the scheduled tip
        pid = int(r["player_name"][1:5])
        team = "Boston Celtics" if pid < 1010 else "Miami Heat"
        opp = "Miami Heat" if team == "Boston Celtics" else "Boston Celtics"
        want = predictor._v2_features(r["player_name"], opp, "Points", team == "Boston Celtics",
                                      pd.Timestamp(tip))
        assert {k: v2[k] for k in want} == want


def test_paper_rows_flag_stale_data(season, stub_models):
    _, captured = _upcoming_game(season, extra_slate_day=True)
    paper_predictions.log_for_pull("evt1", captured)
    assert {r["data_stale"] for r in _paper_rows(season)} == {1}


def test_data_freshness_cases(season):
    conn = sqlite3.connect(season)
    last = LAST_GAME.strftime("%Y-%m-%d")
    nxt = (LAST_GAME + dt.timedelta(days=1)).strftime("%Y-%m-%d")
    assert paper_predictions.data_freshness(conn, nxt) == {"data_as_of": last, "data_stale": 0}
    assert paper_predictions.data_freshness(conn, "2025-10-01") == {"data_as_of": last, "data_stale": 0}
    conn.execute("DELETE FROM player_stats WHERE substr(gameDateTimeEst, 1, 10) = ?", (last,))
    assert paper_predictions.data_freshness(conn, nxt)["data_stale"] == 1
    conn.close()


def test_paper_insert_does_not_hold_the_write_lock_during_feature_work(season, stub_models, monkeypatch,
                                                                         v2_from_synthetic_season):
    """Rows are written in one short transaction after all features are computed."""
    _, captured = _upcoming_game(season)
    in_txn = []
    real = predictor._v2_features

    def spy(*a, **kw):
        probe = sqlite3.connect(season, timeout=0)
        try:
            probe.execute("BEGIN IMMEDIATE")      # fails if log_for_pull holds the write lock
            probe.execute("ROLLBACK")
            in_txn.append(False)
        except sqlite3.OperationalError:
            in_txn.append(True)
        finally:
            probe.close()
        return real(*a, **kw)

    monkeypatch.setattr(predictor, "_v2_features", spy)
    paper_predictions.log_for_pull("evt1", captured)
    assert in_txn and not any(in_txn)


# --- startup maintenance catch-up ------------------------------------------------------

class _Sched:
    def __init__(self):
        self.added = []

    def add_job(self, fn, trigger, **kw):
        self.added.append((fn, kw))


def test_startup_catches_up_missed_maintenance(tmp_path, monkeypatch):
    monkeypatch.setattr(line_logger, "BACKUP_DIR", str(tmp_path))
    s = _Sched()
    at = dt.datetime(2026, 10, 24, 9, 30, tzinfo=line_logger.EASTERN)
    assert line_logger._schedule_maintenance_catchup(s, at) is True
    [(fn, kw)] = s.added
    assert fn is line_logger.run_morning_maintenance and kw["id"] == "morning_maintenance_catchup"
    assert kw["run_date"] == at + dt.timedelta(minutes=line_logger.PULL_WINDOW_MINUTES)


def test_startup_no_catchup_when_maintenance_ran_or_too_early(tmp_path, monkeypatch):
    monkeypatch.setattr(line_logger, "BACKUP_DIR", str(tmp_path))
    s = _Sched()
    assert line_logger._schedule_maintenance_catchup(
        s, dt.datetime(2026, 10, 24, 6, 50, tzinfo=line_logger.EASTERN)) is False
    (tmp_path / "predictions_2026-10-24.db").write_bytes(b"")
    assert line_logger._schedule_maintenance_catchup(
        s, dt.datetime(2026, 10, 24, 9, 30, tzinfo=line_logger.EASTERN)) is False
    assert s.added == []


# --- v2 effective-date switch (V2_EFFECTIVE_DATE, ET game date) -----------------------

def test_v2_in_effect_boundary():
    assert predictor.V2_EFFECTIVE_DATE == "2026-10-24"
    assert not predictor.v2_in_effect("2026-10-23")
    assert predictor.v2_in_effect("2026-10-24")
    assert predictor.v2_in_effect("2026-10-25")
    assert not predictor.v2_in_effect("2026-10-23 23:59:59")   # ET wall time, date part only


def test_paper_rows_before_effective_date_are_v1_exactly_as_before(season, stub_models, monkeypatch):
    """Game date before the switch: p_over/features_json are the v1 ones, no v2 work,
    shadow columns NULL. (The synthetic game is dated 2025-26, before 2026-10-24.)"""
    _, captured = _upcoming_game(season)
    called = []
    monkeypatch.setattr(predictor, "_v2_features", lambda *a, **k: called.append(1) or {})
    assert paper_predictions.log_for_pull("evt1", captured)["logged"] == 3
    conn = sqlite3.connect(season)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT player_name, home_team, away_team, game_date, p_over, features_json, "
                        "feature_def, p_over_v1, features_v1_json FROM paper_predictions").fetchall()
    conn.close()
    assert called == []
    for r in rows:
        assert r["feature_def"] == "v1" and r["p_over_v1"] is None and r["features_v1_json"] is None
        pid = int(r["player_name"][1:5])
        home = pid < 1010
        opp = "Miami Heat" if home else "Boston Celtics"
        want = predictor._compute_ml_features_v1(r["player_name"], opp, "Points", 12.5, home,
                                                  False, min_games=1)
        assert json.loads(r["features_json"]) == json.loads(json.dumps(
            {k: (None if pd.isna(v) else v) for k, v in want.items()}, default=float))
        X = pd.DataFrame([want])[ml_trainer.FEATURE_COLS].fillna(0).astype(float)
        assert r["p_over"] == float(_StubModel().predict_proba(X)[0][1])


def test_paper_rows_on_effective_date_are_v2(season, stub_models, monkeypatch):
    tip, captured = _upcoming_game(season)
    monkeypatch.setattr(predictor, "V2_EFFECTIVE_DATE", tip.strftime("%Y-%m-%d"))   # the game's own date
    paper_predictions.log_for_pull("evt1", captured)
    assert {r["feature_def"] for r in _paper_rows(season)} == {"v2"}


def test_app_features_switch_on_game_date(season, monkeypatch):
    tip = pd.Timestamp(LAST_GAME + dt.timedelta(days=2))
    args = ("P1000 Test", "Miami Heat", "Points", 12.5, True, False)
    v1 = predictor._compute_ml_features_v1(*args)
    monkeypatch.setattr(predictor, "V2_EFFECTIVE_DATE", (tip + pd.Timedelta(days=1)).strftime("%Y-%m-%d"))
    assert predictor._compute_ml_features(*args, cutoff=tip) == v1                     # before: v1
    monkeypatch.setattr(predictor, "V2_EFFECTIVE_DATE", tip.strftime("%Y-%m-%d"))
    v2 = predictor._compute_ml_features(*args, cutoff=tip)                               # on: v2
    assert v2 != v1 and v2["games_played_last15"] == predictor._v2_features(
        "P1000 Test", "Miami Heat", "Points", True, tip)["games_played_last15"]
