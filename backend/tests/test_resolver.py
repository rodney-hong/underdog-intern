"""
App-prediction resolver: names map to a personId once, box scores match by personId.
Every test runs against a temp DB (no rosters table, so teams come from box scores):
nothing touches predictions.db.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database

GAME_DAY = "2026-10-21"


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "test.db")
    monkeypatch.setattr(database, "DB_PATH", path)
    database.init_db()
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE player_stats (personId INTEGER, firstName TEXT, lastName TEXT, "
                 "gameDateTimeEst TEXT, playerteamName TEXT, opponentteamName TEXT, "
                 "numMinutes REAL, points REAL, reboundsTotal REAL, threePointersMade REAL)")
    conn.execute("CREATE TABLE players (personId INTEGER, firstName TEXT, lastName TEXT)")
    conn.commit()
    conn.close()
    return path


def _stats(path, *rows):
    conn = sqlite3.connect(path)
    conn.executemany("INSERT INTO player_stats VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def _players(path, *rows):
    conn = sqlite3.connect(path)
    conn.executemany("INSERT INTO players VALUES (?,?,?)", rows)
    conn.commit()
    conn.close()


def _predict(path, player, stat, line, opponent):
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO predictions (timestamp, player_name, stat_type, stat_line, opponent_team, "
        "predicted_outcome, confidence, explanation, game_date) VALUES (?,?,?,?,?,?,?,?,?)",
        ("2026-10-21T20:30:00", player, stat, line, opponent, "OVER", 0.6, "", GAME_DAY))
    conn.commit()
    conn.close()
    return cur.lastrowid


def _decisions():
    return {d["id"]: d for d in database.dry_run_pending_predictions()}


def _teammate(path, team="Heat", opp="Timberwolves", pid=1):
    """Someone on `team` played `opp` that day, so the matchup is loaded."""
    _stats(path, (pid, "Some", "Teammate", f"{GAME_DAY} 19:30:00", team, opp, 30, 10, 5, 1))


def test_box_score_name_change_still_grades_by_person_id(db):
    # Earlier seasons and players: 'Bobby Portis'; 2026-27 box scores: 'Bobby Portis Jr.'
    _players(db, (1626171, "Bobby", "Portis"))
    _stats(db,
           (1626171, "Bobby", "Portis", "2026-03-23 22:30:00", "Bucks", "Knicks", 20, 12, 6, 2),
           (1626171, "Bobby", "Portis Jr.", f"{GAME_DAY} 19:30:00", "Heat", "Timberwolves",
            24.58, 7, 4, 1))
    pid = _predict(db, "Bobby Portis", "Points", 9.5, "Minnesota Timberwolves")
    d = _decisions()[pid]
    assert (d["result"], d["kind"], d["person_id"]) == ("UNDER", "graded", 1626171)


def test_absent_person_id_in_loaded_game_is_dnp(db):
    _stats(db, (1628370, "Malik", "Monk", "2026-04-10 22:00:00", "Kings", "Suns", 25, 14, 2, 2),
           (2, "Some", "King", f"{GAME_DAY} 22:00:00", "Kings", "Clippers", 30, 10, 5, 1))
    pid = _predict(db, "Malik Monk", "Points", 12.5, "LA Clippers")
    d = _decisions()[pid]
    assert (d["result"], d["kind"]) == ("DNP", "dnp_absent")


def test_game_not_loaded_stays_pending(db):
    _stats(db, (1628370, "Malik", "Monk", "2026-04-10 22:00:00", "Kings", "Suns", 25, 14, 2, 2))
    pid = _predict(db, "Malik Monk", "Points", 12.5, "LA Clippers")
    d = _decisions()[pid]
    assert d["result"] is None and d["warning"] is None


def test_same_name_players_narrowed_by_opponent(db):
    _players(db, (1585, "Brandon", "Williams"), (1630314, "Brandon", "Williams"))
    _stats(db,
           (1585, "Brandon", "Williams", "2004-10-27 20:00:00", "Hawks", "Bucks", 10, 2, 1, 0),
           (1630314, "Brandon", "Williams", f"{GAME_DAY} 22:00:00", "Mavericks", "Suns",
            22, 15, 3, 3))
    pid = _predict(db, "Brandon Williams", "Points", 11.5, "Phoenix Suns")
    d = _decisions()[pid]
    assert (d["result"], d["person_id"]) == ("OVER", 1630314)


def test_same_name_absence_uses_the_active_player(db):
    # 1642444 exists only in players; 1631119 is the Thunder's Jaylin Williams.
    _players(db, (1631119, "Jaylin", "Williams"), (1642444, "Jaylin", "Williams"))
    _stats(db, (1631119, "Jaylin", "Williams", "2026-10-18 19:00:00", "Thunder", "Jazz", 18, 6, 5, 1))
    _teammate(db, team="Thunder", opp="Nuggets")
    pid = _predict(db, "Jaylin Williams", "Rebounds", 4.5, "Denver Nuggets")
    d = _decisions()[pid]
    assert (d["result"], d["kind"], d["person_id"]) == ("DNP", "dnp_absent", 1631119)


def test_two_active_same_name_players_both_absent_is_ambiguous(db):
    _stats(db,
           (10, "Jalen", "Johnson", "2026-10-18 19:00:00", "Hawks", "Nets", 30, 20, 8, 1),
           (11, "Jalen", "Johnson", "2026-10-18 19:00:00", "Spurs", "Jazz", 5, 2, 1, 0))
    _teammate(db, team="Hawks", opp="Magic")
    pid = _predict(db, "Jalen Johnson", "Points", 19.5, "Orlando Magic")
    d = _decisions()[pid]
    assert d["result"] is None and "ambiguous" in d["warning"]


def test_unknown_name_stays_pending(db):
    _teammate(db)
    pid = _predict(db, "Nobody Atall", "Points", 9.5, "Minnesota Timberwolves")
    d = _decisions()[pid]
    assert d["result"] is None and "matches no" in d["warning"]


def test_null_minutes_with_stats_is_graded_as_played(db):
    # Kaggle v523: numMinutes NULL, every other column present.
    _stats(db, (1630581, "Josh", "Giddey", f"{GAME_DAY} 19:30:00", "Bulls", "Raptors",
                None, 18, 9, 2))
    pid = _predict(db, "Josh Giddey", "Points", 16.5, "Toronto Raptors")
    d = _decisions()[pid]
    assert (d["result"], d["kind"]) == ("OVER", "graded")


def test_null_minutes_without_stats_is_dnp(db):
    _stats(db, (1630581, "Josh", "Giddey", f"{GAME_DAY} 19:30:00", "Bulls", "Raptors",
                None, 0, None, 0))
    pid = _predict(db, "Josh Giddey", "Points", 16.5, "Toronto Raptors")
    d = _decisions()[pid]
    assert (d["result"], d["kind"]) == ("DNP", "dnp_zero_min")


def test_zero_minutes_without_stats_is_dnp(db):
    _stats(db, (1630581, "Josh", "Giddey", f"{GAME_DAY} 19:30:00", "Bulls", "Raptors",
                0, 0, 0, 0))
    pid = _predict(db, "Josh Giddey", "Rebounds", 7.5, "Toronto Raptors")
    assert _decisions()[pid]["result"] == "DNP"


def test_played_rule():
    assert database.played(24.6, [7, 4, 1])
    assert database.played(None, [0, None, 2])
    assert not database.played(None, [0, None, 0])
    assert not database.played(0, [])


def test_resolve_writes_graded_result_for_renamed_player(db):
    _stats(db, (1626171, "Bobby", "Portis Jr.", f"{GAME_DAY} 19:30:00", "Heat", "Timberwolves",
                24.58, 7, 4, 1))
    pid = _predict(db, "Bobby Portis", "3PM", 1.5, "Minnesota Timberwolves")
    assert database.resolve_pending_predictions() == 1
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT actual_result FROM predictions WHERE id = ?", (pid,)).fetchone()[0] == "UNDER"
    conn.close()
