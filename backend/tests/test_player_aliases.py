"""
Screenshot-import aliases (player_aliases): a seeded alias resolves when its player is on the
card's team and not otherwise; a pick in an unmatched row is learned and resolves the next
time; collision names and roster names are never learned, and an alias that collides is
ignored; a wrong alias can be deleted. Temp DB; nothing touches predictions.db.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database                                   # noqa: E402
import player_aliases as pa                       # noqa: E402
import screenshot_import as si                    # noqa: E402

MILES, HERB, KAT, BOS_GUY = 1630540, 1630529, 1626157, 1629000


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "aliases.db")
    monkeypatch.setattr(database, "DB_PATH", path)
    database._person_index_cache.clear()
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE rosters (athlete_id TEXT, player_name TEXT, name_key TEXT, espn_team_id TEXT, team_abbrev TEXT,
                              nba_team_id INTEGER, team_city TEXT, team_name TEXT, position TEXT, jersey TEXT,
                              season TEXT, fetched_at TEXT);
        CREATE TABLE espn_person_map (athlete_id TEXT, player_name TEXT, team_name TEXT, person_id INTEGER,
                                      status TEXT, reason TEXT);
        CREATE TABLE player_name_collisions (name_key_a TEXT, name_a TEXT, team_a TEXT, name_key_b TEXT, name_b TEXT,
                                             team_b TEXT, reasons TEXT);
        CREATE TABLE players (personId INTEGER, firstName TEXT, lastName TEXT);
        CREATE TABLE player_stats (personId INTEGER, firstName TEXT, lastName TEXT, gameDateTimeEst TEXT,
                                   gameType TEXT, playerteamName TEXT, opponentteamName TEXT, numMinutes REAL,
                                   points REAL, reboundsTotal REAL, assists REAL, threePointersMade REAL,
                                   blocks REAL, steals REAL, turnovers REAL);
    """)
    conn.executemany("INSERT INTO rosters VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
        ("1", "Miles McBride", "miles mcbride", "18", "NY", 1610612752, "New York", "Knicks", "G", "2", "2026-27", "x"),
        ("2", "Herbert Jones", "herbert jones", "3", "NO", 1610612740, "New Orleans", "Pelicans", "F", "5", "2026-27", "x"),
        ("3", "Karl-Anthony Towns", "karl anthony towns", "18", "NY", 1610612752, "New York", "Knicks", "C", "32",
         "2026-27", "x"),
        ("4", "Jaylen Brown", "jaylen brown", "2", "BOS", 1610612738, "Boston", "Celtics", "G", "7", "2026-27", "x")])
    conn.executemany("INSERT INTO espn_person_map VALUES (?,?,?,?,?,?)", [
        ("1", "Miles McBride", "Knicks", MILES, "ok", None), ("2", "Herbert Jones", "Pelicans", HERB, "ok", None),
        ("3", "Karl-Anthony Towns", "Knicks", KAT, "ok", None), ("4", "Jaylen Brown", "Celtics", BOS_GUY, "ok", None)])
    conn.executemany("INSERT INTO players VALUES (?,?,?)", [(MILES, "Miles", "McBride"), (HERB, "Herbert", "Jones"),
                                                           (KAT, "Karl-Anthony", "Towns"), (BOS_GUY, "Jaylen", "Brown")])
    conn.executemany("INSERT INTO player_stats VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        (pid, f, l, f"2026-0{1 + i % 3}-{1 + i:02d} 19:30:00", "Regular Season", team, "Heat", 30, 10 + i % 7, 4, 3, 1,
         0, 1, 1) for pid, f, l, team in ((MILES, "Miles", "McBride", "Knicks"), (HERB, "Herbert", "Jones", "Pelicans"),
                                         (KAT, "Karl-Anthony", "Towns", "Knicks"), (BOS_GUY, "Jaylen", "Brown", "Celtics"))
        for i in range(20)])
    # "Jay Brown" collides with Jaylen Brown (same last name + first initial)
    conn.execute("INSERT INTO player_name_collisions VALUES ('jay brown', 'Jay Brown', 'Nets', 'jaylen brown', "
                 "'Jaylen Brown', 'Celtics', 'same last name + first initial')")
    conn.commit()
    pa.init_table(conn)
    conn.close()
    return path


def _resolve(name, team):
    conn = database.get_connection()
    try:
        return si.resolve_player(conn, name, team)
    finally:
        conn.close()


def _rows(name, team, stat="Points", line=9.5):
    ctx = {"next_opponent_full": "Miami Heat", "is_home": True, "is_back_to_back": False,
           "next_game_date": "2026-10-25"}
    return si.confirmation_rows([{"player_name": name, "team": team, "stat": stat, "stat_text": stat, "line": line,
                                  "alerts": []}], context_fn=lambda n: ctx, record=False)[0]


def test_seeded_alias_resolves_on_the_cards_team(db):
    r = _resolve("Deuce McBride", "NYK")
    assert r == {"status": "ok", "player_name": "Miles McBride", "team_abbrev": "NY", "team_name": "New York Knicks",
                 "person_id": MILES, "via": "alias"}
    assert _resolve("Deuce McBride", "Knicks")["person_id"] == MILES
    row = _rows("Deuce McBride", "NYK")
    assert row["match"] == "ok" and row["resolved_player"] == "Miles McBride" and row["person_id"] == MILES
    assert row["range"]["games"] == 20 and row["alerts"] == []
    assert _resolve("Miles McBride", "NYK").get("via") is None              # the exact name still wins first


def test_alias_for_the_wrong_team_does_not_resolve(db):
    assert _resolve("Deuce McBride", "BOS") == {"status": "not_found"}
    conn = sqlite3.connect(db)                                              # traded: no longer a Knick
    conn.execute("UPDATE espn_person_map SET team_name = 'Celtics' WHERE person_id = ?", (MILES,))
    conn.commit()
    conn.close()
    assert _resolve("Deuce McBride", "NYK") == {"status": "not_found"}
    assert _resolve("Deuce McBride", None) == {"status": "not_found"}       # stored team no longer his either
    assert _resolve("Deuce McBride", "BOS")["person_id"] == MILES


def test_a_pick_in_an_unmatched_row_is_learned(db, monkeypatch):
    from fastapi.testclient import TestClient
    import main
    assert _rows("KAT", "NYK")["match"] == "not_found"
    client = TestClient(main.app)
    assert client.post("/screenshot/alias", json={"screenshot_name": "KAT", "person_id": KAT}).json() == \
        {"saved": True, "reason": None}
    row = _rows("KAT", "NYK")
    assert row["match"] == "ok" and row["resolved_player"] == "Karl-Anthony Towns" and row["person_id"] == KAT
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT alias_name, person_id, team, source FROM player_aliases WHERE alias_key = 'kat'"
                        ).fetchall() == [("KAT", KAT, "Knicks", "user")]
    conn.close()
    assert client.delete("/screenshot/alias", params={"name": "KAT"}).json() == {"deleted": 1}
    assert _rows("KAT", "NYK")["match"] == "not_found"


def test_collision_and_roster_names_are_not_learned(db):
    conn = database.get_connection()
    try:
        assert pa.learn(conn, "Jay Brown", BOS_GUY) == {"saved": False, "reason": "name collision"}
        assert pa.learn(conn, "Herbert Jones", MILES) == {"saved": False, "reason": "a roster player's exact name"}
        assert pa.learn(conn, "Miles McBride", MILES)["saved"] is False
        assert conn.execute("SELECT COUNT(*) FROM player_aliases WHERE source = 'user'").fetchone()[0] == 0
        # an alias that a later collision makes unsafe is ignored at resolution
        conn.execute("INSERT INTO player_aliases VALUES ('jay brown', 'Jay Brown', ?, 'Celtics', 'user', 'x')",
                     (BOS_GUY,))
        conn.commit()
    finally:
        conn.close()
    assert _resolve("Jay Brown", "BOS") == {"status": "not_found"}


def test_seeds_and_cli_delete(db, capsys):
    pa._main(["--delete", "Herb Jones", "--person-id", str(HERB)])
    assert "deleted 1" in capsys.readouterr().out
    assert _resolve("Herb Jones", "NOP") == {"status": "not_found"}
    conn = sqlite3.connect(db)
    pa.init_table(conn)                                                     # seeds come back on the next start
    conn.close()
    assert _resolve("Herb Jones", "NOP")["person_id"] == HERB
