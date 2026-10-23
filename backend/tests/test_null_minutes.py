"""
NULL numMinutes (Kaggle v523 blanked them for game 22600088): the ETL keeps the
previous version's minutes, and the paper resolver grades NULL-minute rows with
counting stats as played. Temp DBs and temp CSVs only: nothing touches
predictions.db, predictions_backup.csv or the network.
"""
import os
import sqlite3
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import paper_predictions

GAME, NEW_GAME = 22600088, 22600099


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "test.db")
    monkeypatch.setattr(database, "DB_PATH", path)
    database.init_db()
    return path


# --- ETL guard --------------------------------------------------------------------

def _stats_row(game, pid, minutes, points, date="2026-10-21 19:30:00", rebounds=3):
    row = {c: 0 for c in database._STATS_COLS}
    row.update(firstName="P", lastName=str(pid), personId=pid, gameId=game,
               gameDateTimeEst=date, gameType="Regular Season", playerteamName="Bulls",
               opponentteamName="Raptors", numMinutes=minutes, points=points,
               reboundsTotal=rebounds)
    return row


def _write_version(tmp_path, rows):
    """CSV files for one Kaggle version, shaped like download_kaggle_files' result."""
    d = tmp_path / f"v{len(list(tmp_path.glob('v*')))}"
    d.mkdir()
    pd.DataFrame(rows, columns=database._STATS_COLS).to_csv(d / "stats.csv", index=False)
    pd.DataFrame([{"personId": r["personId"], "firstName": "P", "lastName": str(r["personId"])}
                  for r in rows]).to_csv(d / "players.csv", index=False)
    pd.DataFrame([{"gameId": GAME, "gameDateTimeEst": "2026-10-21 19:30:00", "homeTeamId": 1,
                   "awayTeamId": 2, "homeTeamName": "Raptors", "homeTeamCity": "Toronto",
                   "awayTeamName": "Bulls", "awayTeamCity": "Chicago"}]).to_csv(d / "sched.csv", index=False)
    return {"stats": str(d / "stats.csv"), "players": str(d / "players.csv"),
            "schedule": str(d / "sched.csv")}


def _load(monkeypatch, version, files):
    monkeypatch.setattr(database, "backup_predictions_to_csv", lambda: None)
    monkeypatch.setattr(database, "get_latest_kaggle_version", lambda: version)
    monkeypatch.setattr(database, "download_kaggle_files", lambda v: files)
    assert database.run_etl() is True


def _minutes(path):
    conn = sqlite3.connect(path)
    try:
        return dict(((g, p), m) for g, p, m in conn.execute(
            "SELECT gameId, personId, numMinutes FROM player_stats"))
    finally:
        conn.close()


def test_new_version_blanking_minutes_is_restored_from_previous(db, tmp_path, monkeypatch, capsys):
    _load(monkeypatch, 522, _write_version(tmp_path, [
        _stats_row(GAME, 1, 31.5, 18), _stats_row(GAME, 2, 12.0, 4), _stats_row(GAME, 3, None, 0)]))
    capsys.readouterr()
    _load(monkeypatch, 523, _write_version(tmp_path, [
        _stats_row(GAME, 1, None, 18), _stats_row(GAME, 2, None, 4), _stats_row(GAME, 3, None, 0)]))
    out = capsys.readouterr().out
    assert _minutes(db) == {(GAME, 1): 31.5, (GAME, 2): 12.0, (GAME, 3): None}
    assert f"kept the previous value for 2 row(s) in 1 game(s): {{{GAME}: 2}}" in out
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT points FROM player_stats WHERE personId = 1").fetchone()[0] == 18
    assert conn.execute("SELECT version FROM kaggle_version").fetchone()[0] == 523
    conn.close()


def test_new_version_value_wins_when_present(db, tmp_path, monkeypatch):
    _load(monkeypatch, 522, _write_version(tmp_path, [_stats_row(GAME, 1, 31.5, 18)]))
    _load(monkeypatch, 523, _write_version(tmp_path, [_stats_row(GAME, 1, 32.0, 18)]))
    assert _minutes(db) == {(GAME, 1): 32.0}


def test_new_game_with_null_minutes_and_stats_warns(db, tmp_path, monkeypatch, capsys):
    _load(monkeypatch, 522, _write_version(tmp_path, [_stats_row(GAME, 1, 31.5, 18)]))
    capsys.readouterr()
    _load(monkeypatch, 523, _write_version(tmp_path, [
        _stats_row(GAME, 1, 31.5, 18),
        _stats_row(NEW_GAME, 1, None, 9, date="2026-10-23 16:00:00"),
        _stats_row(NEW_GAME, 2, None, 0, date="2026-10-23 16:00:00", rebounds=0)]))  # true DNP: no warning
    out = capsys.readouterr().out
    assert f"1 row(s) in 1 game(s) new in this version have NULL numMinutes" in out
    assert f"{{{NEW_GAME}: 1}}" in out
    assert _minutes(db)[(NEW_GAME, 1)] is None  # nothing to restore from


def test_first_load_has_no_guard(db, tmp_path, monkeypatch, capsys):
    _load(monkeypatch, 522, _write_version(tmp_path, [_stats_row(GAME, 1, None, 18)]))
    assert "WARNING" not in capsys.readouterr().out
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT name FROM sqlite_temp_master").fetchall() == []
    conn.close()


# --- paper resolver ---------------------------------------------------------------

@pytest.fixture
def paper(db):
    conn = paper_predictions.get_db(db)
    paper_predictions.init_tables(conn)
    conn.execute("CREATE TABLE player_stats (personId INTEGER, gameDateTimeEst TEXT, gameType TEXT, "
                 "playerteamName TEXT, numMinutes REAL, points REAL, reboundsTotal REAL, "
                 "assists REAL, threePointersMade REAL, blocks REAL, steals REAL, turnovers REAL)")
    conn.commit()
    yield conn
    conn.close()


def _paper_row(conn, pid, stat="Points", line=15.5):
    cur = conn.execute(
        "INSERT INTO paper_predictions (created_at, captured_at, game_id, game_date, player_name, "
        "person_id, player_team, stat_type, ud_line, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("2026-10-21T23:00:00", "2026-10-21T23:00:00", "evt", "2026-10-21", f"P {pid}", pid,
         "Chicago Bulls", stat, line, "ok"))
    conn.commit()
    return cur.lastrowid


def _box(conn, pid, minutes, points, rebounds=0, assists=0, threes=0, blocks=0, steals=0, tov=0):
    conn.execute("INSERT INTO player_stats VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (pid, "2026-10-21 19:30:00", "Regular Season", "Bulls", minutes, points,
                  rebounds, assists, threes, blocks, steals, tov))
    conn.commit()


def _result(conn, rid):
    return conn.execute("SELECT result, actual_value IS NULL FROM paper_predictions WHERE id = ?",
                        (rid,)).fetchone()


def test_paper_null_minutes_with_stats_is_graded_as_played(paper):
    _box(paper, 1, None, 18, rebounds=9)
    rid = _paper_row(paper, 1)
    assert paper_predictions.resolve_paper_predictions(paper) == 1
    assert tuple(_result(paper, rid)) == ("OVER", 0)


def test_paper_null_minutes_with_only_a_steal_is_played(paper):
    _box(paper, 1, None, 0, steals=1)
    rid = _paper_row(paper, 1)
    paper_predictions.resolve_paper_predictions(paper)
    assert tuple(_result(paper, rid)) == ("UNDER", 0)


def test_paper_null_minutes_without_stats_is_dnp(paper):
    _box(paper, 1, None, 0)
    _box(paper, 2, 30, 20)  # his team played that day
    rid = _paper_row(paper, 1)
    paper_predictions.resolve_paper_predictions(paper)
    assert tuple(_result(paper, rid)) == ("DNP", 1)


def test_paper_absent_from_box_score_is_dnp(paper):
    _box(paper, 2, 30, 20)
    rid = _paper_row(paper, 1)
    paper_predictions.resolve_paper_predictions(paper)
    assert tuple(_result(paper, rid)) == ("DNP", 1)
