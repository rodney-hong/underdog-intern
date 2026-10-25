"""
Rookies and thin samples: /predict refuses a player with no counted regular-season game,
app predictions carry is_limited_data (and existing rows are backfilled), and an exact
name + team match to one player still auto-fills its line. Temp DB; predictor, injuries
and lines stubbed. Nothing touches predictions.db or the network.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data_fetcher
import database

VET, ROOKIE, THIN, JAYLIN_A, JAYLIN_B, UNSIGNED = 1, 2, 3, 4, 5, 6


@pytest.fixture
def db(tmp_path, monkeypatch):
    """
    VET      240 regular-season games before 2026-27 (Bucks), 2 this season.
    ROOKIE   no game before 2026-27; 12 this season (Hornets).
    THIN     5 regular-season games in 2025-26 (Heat), nothing since.
    JAYLIN_A / JAYLIN_B  both 'Jaylin Williams'; only A has box scores (Thunder).
    UNSIGNED in players only, no box score at all.
    """
    path = str(tmp_path / "test.db")
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(data_fetcher, "DB_PATH", path)
    database._person_index_cache.clear()
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE player_stats (personId INTEGER, firstName TEXT, lastName TEXT, "
                 "gameDateTimeEst TEXT, gameType TEXT, opponentteamName TEXT)")
    conn.execute("CREATE TABLE players (personId INTEGER, firstName TEXT, lastName TEXT)")
    rows = []
    for i in range(240):
        rows.append((VET, "Vet", "Eran", f"2024-{1 + i % 12:02d}-{1 + i % 28:02d} 19:30:00",
                     "Regular Season", "Knicks"))
    rows.append((VET, "Vet", "Eran", "2025-05-01 19:30:00", "Preseason", "Knicks"))   # never counted
    for d in (21, 23):
        rows.append((VET, "Vet", "Eran", f"2026-10-{d} 19:30:00", "Regular Season", "Knicks"))
    for d in range(10, 22):   # 12 games, the 12th on 2026-11-21
        rows.append((ROOKIE, "Rook", "Ie", f"2026-11-{d:02d} 19:30:00", "Regular Season", "Heat"))
    for d in range(1, 6):
        rows.append((THIN, "Thin", "Sample", f"2025-12-0{d} 19:30:00", "Regular Season", "Nets"))
    for d in range(1, 21):
        rows.append((JAYLIN_A, "Jaylin", "Williams", f"2025-11-{d:02d} 19:30:00", "Regular Season", "Jazz"))
    conn.executemany("INSERT INTO player_stats VALUES (?,?,?,?,?,?)", rows)
    conn.executemany("INSERT INTO players VALUES (?,?,?)",
                     [(VET, "Vet", "Eran"), (ROOKIE, "Rook", "Ie"), (THIN, "Thin", "Sample"),
                      (JAYLIN_A, "Jaylin", "Williams"), (JAYLIN_B, "Jaylin", "Williams"),
                      (UNSIGNED, "Un", "Signed")])
    conn.commit()
    conn.close()
    database.init_db()
    return path


def _counts(name, game_date="2026-11-30", opp="Knicks"):
    conn = database.get_connection()
    try:
        return database.player_data_counts(conn, name, game_date, opp)
    finally:
        conn.close()


def _limited(name, game_date="2026-11-30", explanation=""):
    return database.is_limited_data(_counts(name, game_date), explanation)


# --- the flag ---------------------------------------------------------------------------

def test_veteran_is_not_limited(db):
    c = _counts("Vet Eran", "2026-10-23")
    assert c == {"person_id": VET, "rs_total": 242, "rs_before_season": 240, "counted_before_game": 241}
    assert not _limited("Vet Eran", "2026-10-23")


def test_rookie_is_limited_even_after_10_games(db):
    assert _counts("Rook Ie")["counted_before_game"] == 12
    assert _limited("Rook Ie")                          # no regular-season game before 2026-27


def test_fewer_than_10_games_before_the_game_is_limited(db):
    assert _counts("Thin Sample")["rs_before_season"] == 5
    assert _limited("Thin Sample")


def test_only_games_before_the_game_date_count(db):
    # VET on 2026-10-21: 240 earlier games, not the one that day or the 2026-10-23 one.
    assert _counts("Vet Eran", "2026-10-21")["counted_before_game"] == 240


def test_insufficient_data_answer_is_limited(db):
    assert _limited("Vet Eran", explanation="Insufficient game log data found for Vet Eran this season. ...")


def test_same_name_players_narrow_to_the_one_with_box_scores(db):
    # (the resolver's narrowing: who has a box score in the year before the game)
    c = _counts("Jaylin Williams", "2026-10-30")
    assert c["person_id"] == JAYLIN_A and not _limited("Jaylin Williams", "2026-10-30")


def test_unknown_name_is_limited(db):
    assert _counts("Nobody Here")["person_id"] is None and _limited("Nobody Here")


# --- logging and backfill -----------------------------------------------------------------

def _log(name, game_date, explanation="model", league="NBA"):
    return database.log_prediction(player_name=name, stat_type="Points", stat_line=20.5,
                                   opponent_team="New York Knicks", predicted_outcome="OVER",
                                   confidence=0.6, explanation=explanation, game_date=game_date,
                                   league=league)


def _flag(row_id):
    conn = sqlite3.connect(database.DB_PATH)
    try:
        return conn.execute("SELECT is_limited_data FROM predictions WHERE id = ?", (row_id,)).fetchone()[0]
    finally:
        conn.close()


def test_new_predictions_carry_the_flag(db):
    assert _flag(_log("Vet Eran", "2026-10-23")) == 0
    assert _flag(_log("Rook Ie", "2026-11-30")) == 1


def test_backfill_flags_existing_rows_from_games_before_each_row(db):
    conn = sqlite3.connect(database.DB_PATH)
    conn.executemany(
        "INSERT INTO predictions (timestamp, player_name, stat_type, stat_line, opponent_team, "
        "predicted_outcome, confidence, explanation, actual_result, game_date, league) "
        "VALUES ('2026-10-01T00:00:00', ?, 'Points', 20.5, 'New York Knicks', 'OVER', 0.6, ?, "
        "'OVER', ?, ?)",
        [("Vet Eran", "model", "2026-10-23", "NBA"),
         ("Vet Eran", "Insufficient game log data found for Vet Eran this season.", "2026-10-23", "NBA"),
         ("Rook Ie", "model", "2026-11-30", "NBA"),
         ("Thin Sample", "model", "2026-10-25", "NBA"),
         ("Rook Ie", "model", "2026-11-30", "WNBA")])
    conn.execute("UPDATE predictions SET is_limited_data = NULL")
    conn.commit()
    conn.close()
    assert database.backfill_limited_data() == {"rows": 5, "limited": 3}
    conn = sqlite3.connect(database.DB_PATH)
    flags = [r[0] for r in conn.execute("SELECT is_limited_data FROM predictions ORDER BY id")]
    conn.close()
    assert flags == [0, 1, 1, 1, 0]


def test_init_db_adds_the_column_and_backfills(db):
    conn = sqlite3.connect(database.DB_PATH)
    assert "is_limited_data" in {r[1] for r in conn.execute("PRAGMA table_info(predictions)")}
    conn.close()
    _log("Rook Ie", "2026-11-30")
    database.init_db()                                   # idempotent, recomputes
    conn = sqlite3.connect(database.DB_PATH)
    assert conn.execute("SELECT is_limited_data FROM predictions").fetchall() == [(1,)]
    conn.close()


# --- /predict and /predictions/history ------------------------------------------------------

@pytest.fixture
def client(db, monkeypatch):
    from fastapi.testclient import TestClient
    import main
    import predictor
    monkeypatch.setattr(predictor, "predict", lambda **kw: {"outcome": "OVER", "confidence": 0.61,
                                                            "explanation": "model"})
    monkeypatch.setattr(data_fetcher, "get_opponent_injuries", lambda team: [])
    # Points goes to the challenger from Stage 3; these tests are about the flag, not the model
    monkeypatch.setattr(main, "_challenger_prediction", lambda req, pid: {"outcome": "OVER", "confidence": 0.61,
                                                                          "explanation": "model"})
    return TestClient(main.app)


def _predict(client, name):
    return client.post("/predict", json={"player_name": name, "opponent_team": "New York Knicks",
                                         "stat_line": 20.5, "stat_type": "Points",
                                         "game_date": "2026-11-30", "league": "NBA"})


def _rows():
    conn = sqlite3.connect(database.DB_PATH)
    try:
        return conn.execute("SELECT player_name, is_limited_data FROM predictions").fetchall()
    finally:
        conn.close()


@pytest.mark.parametrize("name", ["Un Signed", "Nobody Here"])
def test_predict_refuses_a_player_with_no_regular_season_game(client, name):
    r = _predict(client, name)
    assert r.status_code == 422 and r.json()["detail"] == "Not enough NBA games"
    assert _rows() == []                                  # nothing logged


def test_predict_logs_rookie_with_games_as_limited(client):
    assert _predict(client, "Rook Ie").status_code == 200
    assert _predict(client, "Vet Eran").status_code == 200
    assert sorted(_rows()) == [("Rook Ie", 1), ("Vet Eran", 0)]
    hist = {r["player_name"]: r["is_limited_data"] for r in client.get("/predictions/history").json()}
    assert hist == {"Rook Ie": 1, "Vet Eran": 0}


# --- lines: one exact name + team match resolves, two stay blank ---------------------------

def _lines(monkeypatch, cache, team="Oklahoma City Thunder"):
    monkeypatch.setattr(data_fetcher, "get_todays_lines", lambda: cache)
    monkeypatch.setattr(data_fetcher, "get_player_context", lambda name: {"player_team_full": team})


def _game(line, home="Oklahoma City Thunder", away="Denver Nuggets"):
    return {"player_points": {"value": line, "source": "underdog"}, "home_team": home, "away_team": away}


def test_exact_name_and_team_to_one_player_auto_fills(db, monkeypatch):
    _lines(monkeypatch, {"Jalen Williams": _game(18.5), "Jaylin Williams": _game(6.5)})
    assert data_fetcher.get_player_line("Jalen Williams", "Points") == (18.5, "underdog")


def test_two_exact_matches_on_the_team_are_not_auto_filled(db, monkeypatch):
    _lines(monkeypatch, {"Jalen Williams": _game(18.5), "Jalen Williams Jr.": _game(4.5)})
    line, _ = data_fetcher.get_player_line("Jalen Williams", "Points")
    assert line is None


def test_lines_endpoint_never_sends_ambiguity_text(db, monkeypatch):
    from fastapi.testclient import TestClient
    import main
    _lines(monkeypatch, {"Jalen Williams": _game(18.5), "Jalen Williams Jr.": _game(4.5)})
    body = TestClient(main.app).get("/lines/Jalen%20Williams/Points").json()
    assert body["line"] is None and body["reason"] is None
