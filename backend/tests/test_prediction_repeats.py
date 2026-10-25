"""
Repeated app predictions: a same-line repeat replaces the row's pick in full (outcome
included) without mixing models; every row is kept and only the latest per (player, stat,
game_date, model) is current; existing rows are backfilled on startup; Clean Duplicates
never removes a row of another model. Temp DB; nothing touches predictions.db.
"""
import os
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database  # noqa: E402

DDL = ("CREATE TABLE predictions (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, "
       "player_name TEXT NOT NULL, stat_type TEXT NOT NULL, stat_line REAL NOT NULL, "
       "opponent_team TEXT NOT NULL, predicted_outcome TEXT NOT NULL, confidence REAL NOT NULL, "
       "explanation TEXT NOT NULL, actual_result TEXT, game_date TEXT, league TEXT, model TEXT)")


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "app.db")
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(database, "backfill_limited_data", lambda conn: {})
    monkeypatch.setattr(database, "player_data_counts", lambda *a, **k: {})
    monkeypatch.setattr(database, "is_limited_data", lambda *a, **k: False)
    return path


def _log(line, outcome="OVER", conf=0.6, model="frozen", date="2026-10-25", player="Jay Test", stat="Points"):
    time.sleep(0.002)                                  # distinct timestamps
    return database.log_prediction(player, stat, line, "New York Knicks", outcome, conf, f"{outcome} {conf}",
                                   game_date=date, model=model)


def _rows(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT id, stat_line, predicted_outcome, confidence, explanation, model, is_current "
                            "FROM predictions ORDER BY id").fetchall()
    finally:
        conn.close()


def test_different_line_keeps_both_and_supersedes_the_earlier(db):
    database.init_db()
    a = _log(20.5)
    b = _log(22.5, "UNDER")
    assert [(r[0], r[6]) for r in _rows(db)] == [(a, 0), (b, 1)]


def test_same_line_repeat_updates_every_field_including_the_outcome(db):
    database.init_db()
    a = _log(20.5, "OVER", 0.58)
    assert _log(20.5, "UNDER", 0.66) == a
    assert _rows(db) == [(a, 20.5, "UNDER", 0.66, "UNDER 0.66", "frozen", 1)]


def test_same_line_repeat_makes_an_older_line_current_again(db):
    database.init_db()
    a = _log(20.5)
    b = _log(22.5)
    _log(20.5, "UNDER")                                # repeat of the first line, now the latest
    assert [(r[0], r[2], r[6]) for r in _rows(db)] == [(a, "UNDER", 1), (b, "OVER", 0)]


def test_a_challenger_repeat_of_a_frozen_row_is_a_new_row(db):
    database.init_db()
    f = _log(20.5, "OVER", 0.6, "frozen")
    c = _log(20.5, "UNDER", 0.7, "challenger")
    assert c != f
    assert _rows(db) == [(f, 20.5, "OVER", 0.6, "OVER 0.6", "frozen", 1),
                         (c, 20.5, "UNDER", 0.7, "UNDER 0.7", "challenger", 1)]


def test_groups_are_per_game_date_and_stat(db):
    database.init_db()
    a = _log(20.5, date="2026-10-25")
    b = _log(20.5, date="2026-10-27")
    c = _log(5.5, stat="Rebounds")
    d = _log(21.5, date=None)                          # no game date: never grouped
    e = _log(19.5, date=None)
    assert {r[0]: r[6] for r in _rows(db)} == {a: 1, b: 1, c: 1, d: 1, e: 1}


def test_backfill_flags_existing_rows(db):
    conn = sqlite3.connect(db)
    conn.execute(DDL)
    rows = [("2026-10-21T10:00", "A", "Points", 20.5, "OVER", "2026-10-21", "frozen"),
            ("2026-10-21T11:00", "A", "Points", 21.5, "UNDER", "2026-10-21", "frozen"),
            ("2026-10-21T12:00", "A", "Points", 21.5, "UNDER", "2026-10-21", None),     # NULL model = frozen
            ("2026-10-21T09:00", "A", "Points", 22.5, "OVER", "2026-10-21", "challenger"),
            ("2026-10-21T09:00", "B", "Points", 10.5, "OVER", "2026-10-21", "frozen"),
            ("2026-10-21T08:00", "B", "Points", 11.5, "OVER", None, "frozen")]
    conn.executemany("INSERT INTO predictions (timestamp, player_name, stat_type, stat_line, opponent_team, "
                     "predicted_outcome, confidence, explanation, game_date, model) "
                     "VALUES (?, ?, ?, ?, 'X', ?, 0.6, 'e', ?, ?)", [r[:4] + r[4:] for r in rows])
    conn.commit()
    conn.close()
    database.init_db()
    assert [r[6] for r in _rows(db)] == [0, 0, 1, 1, 1, 1]
    database.init_db()                                 # idempotent
    assert [r[6] for r in _rows(db)] == [0, 0, 1, 1, 1, 1]


def test_history_endpoint_and_clean_duplicates(db, monkeypatch):
    from fastapi.testclient import TestClient
    import main
    database.init_db()
    f = _log(20.5, model="frozen")
    _log(20.5, model="challenger")
    conn = sqlite3.connect(db)                         # an exact frozen duplicate (older code paths)
    conn.execute("INSERT INTO predictions (timestamp, player_name, stat_type, stat_line, opponent_team, "
                 "predicted_outcome, confidence, explanation, game_date, model) "
                 "VALUES ('2026-10-20T00:00', 'Jay Test', 'Points', 20.5, 'New York Knicks', 'OVER', 0.5, 'e', "
                 "'2026-10-25', 'frozen')")
    conn.commit()
    conn.close()
    client = TestClient(main.app)
    assert client.delete("/predictions/duplicates").json() == {"deleted": 1}
    hist = client.get("/predictions/history").json()
    assert sorted((h["model"], h["is_current"]) for h in hist) == [("challenger", 1), ("frozen", 1)]
    assert [h["id"] for h in hist if h["model"] == "frozen"] == [f]      # the newest one is kept
