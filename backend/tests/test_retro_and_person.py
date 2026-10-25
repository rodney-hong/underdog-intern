"""
One personId everywhere: database.resolve_person_id picks the player with real history
over stray preseason ids (Luka Doncic's two NULL-date players-table ids; a 2026-10-12
exhibition id); the screenshot table's range flag and person_id and /predict use it.
Retro comparison: frozen app picks re-scored by the challenger from data before each tip,
stored as model 'challenger_retro', graded from the same box score, idempotent, and kept
out of track records. Temp DBs; nothing touches predictions.db, the frozen files or the
network.
"""
import os
import sqlite3
import sys

import numpy as np
import pandas as pd
import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
sys.path.insert(0, os.path.join(BACKEND, "experiments"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import data_fetcher                                  # noqa: E402
import database                                      # noqa: E402
import screenshot_import                             # noqa: E402
from challenger import live                          # noqa: E402
from challenger import model as cm                   # noqa: E402

LUKA, STRAY1, STRAY2 = 1629029, 1962937071, 1962937441
JAY, JAY_STRAY = 1631119, 196700042                  # real player + a 2026-10-12 exhibition id


@pytest.fixture
def people(tmp_path, monkeypatch):
    path = str(tmp_path / "people.db")
    monkeypatch.setattr(database, "DB_PATH", path)
    database._person_index_cache.clear()
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE player_stats (personId INTEGER, firstName TEXT, lastName TEXT, gameId INTEGER, "
                 "gameDateTimeEst TEXT, gameType TEXT, opponentteamName TEXT, points REAL, reboundsTotal REAL, "
                 "assists REAL, threePointersMade REAL, numMinutes REAL)")
    conn.execute("CREATE TABLE players (personId INTEGER, firstName TEXT, lastName TEXT)")
    rows = []
    for i in range(30):
        rows.append((LUKA, "Luka", "Doncic", 1000 + i, f"2026-0{1 + i % 3}-{1 + i:02d} 19:30:00", "Regular Season",
                     "Jazz", 20 + i, 8, 8, 3, 36))
        rows.append((JAY, "Jay", "Williams", 2000 + i, f"2026-0{1 + i % 3}-{1 + i:02d} 19:30:00", "Regular Season",
                     "Nets", 5 + i % 4, 6, 1, 0, 20))
    rows.append((LUKA, "Luka", "Dončić", 9000, None, "Pre Season", None, 10, 1, 1, 1, 20))
    rows.append((STRAY1, "Luka", "Doncic", 9001, None, "Pre Season", None, 0, 0, 0, 0, 5))
    rows.append((STRAY2, "Luka", "Doncic", 9002, None, "Pre Season", None, 0, 0, 0, 0, 5))
    rows.append((JAY_STRAY, "Jay", "Williams", 9003, "2026-10-12 16:00:00", "Preseason", "Real Madrid", 2, 1, 0, 0, 9))
    conn.executemany("INSERT INTO player_stats VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.executemany("INSERT INTO players VALUES (?,?,?)", [(LUKA, "Luka", "Doncic"), (STRAY1, "Luka", "Doncic"),
                                                           (STRAY2, "Luka", "Doncic"), (JAY, "Jay", "Williams"),
                                                           (JAY_STRAY, "Jay", "Williams")])
    conn.execute("CREATE TABLE rosters (athlete_id TEXT, player_name TEXT, name_key TEXT, espn_team_id TEXT, "
                 "team_abbrev TEXT, nba_team_id INTEGER, team_city TEXT, team_name TEXT, position TEXT, "
                 "jersey TEXT, season TEXT, fetched_at TEXT)")
    conn.executemany("INSERT INTO rosters VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     [("1", "Luka Doncic", "luka doncic", "13", "LAL", 1610612747, "Los Angeles", "Lakers",
                       "G", "77", "2026-27", "x"),
                      ("2", "Jay Williams", "jay williams", "25", "OKC", 1610612760, "Oklahoma City", "Thunder",
                       "F", "6", "2026-27", "x")])
    conn.execute("CREATE TABLE line_snapshots (game_date TEXT, bookmaker TEXT, market_key TEXT, home_team TEXT, "
                 "away_team TEXT, player_name TEXT, point REAL, captured_at TEXT, pull_phase TEXT, side TEXT, price REAL)")
    conn.commit()
    conn.close()
    return path


def test_luka_and_a_stray_exhibition_id_resolve_to_the_real_player(people):
    conn = database.get_connection()
    try:
        assert database.resolve_person_id(conn, "Luka Doncic", "2026-10-25", "Utah Jazz") == LUKA
        assert database.resolve_person_id(conn, "Luka Dončić", "2026-10-25", "") == LUKA
        # both played in the past year; only the real one has a counted game there
        assert database.resolve_person_id(conn, "Jay Williams", "2026-10-25", "Brooklyn Nets") == JAY
        counts = database.player_data_counts(conn, "Luka Doncic", "2026-10-25", "Utah Jazz")
        assert counts["person_id"] == LUKA and counts["rs_total"] == 30
        assert database.player_data_counts(conn, "Jay Williams", "2026-10-25", "", person_id=JAY_STRAY)["person_id"] \
            == JAY_STRAY                                                      # an explicit pick is used as is
    finally:
        conn.close()


def test_screenshot_range_flag_and_person_id_use_the_same_resolver(people):
    ctx = {"next_opponent_full": "Utah Jazz", "is_home": False, "is_back_to_back": False,
           "next_game_date": "2026-10-25", "player_team_full": "Los Angeles Lakers"}
    rows = [{"player_name": "Luka Doncic", "team": "LAL", "stat": "Points", "stat_text": "Points", "line": 31.5,
             "alerts": []},
            {"player_name": "Jay Williams", "team": "OKC", "stat": "Rebounds", "stat_text": "Rebounds", "line": 6.5,
             "alerts": []}]
    out = screenshot_import.confirmation_rows(rows, context_fn=lambda name: ctx, record=False)
    luka, jay = out
    n = min(30, screenshot_import.RANGE_GAMES)
    assert luka["person_id"] == LUKA and luka["range"] == {"min": float(20 + 30 - n), "max": 49.0, "games": n}
    assert "no game history for this stat" not in luka["flags"]
    assert jay["person_id"] == JAY and jay["range"]["games"] == n
    conn = database.get_connection()
    try:
        assert luka["person_id"] == database.player_data_counts(conn, "Luka Doncic", ctx["next_game_date"],
                                                                ctx["next_opponent_full"])["person_id"]
    finally:
        conn.close()


def test_predict_passes_a_chosen_person_id_and_refuses_an_ambiguous_name(people, monkeypatch):
    from fastapi.testclient import TestClient
    import main
    seen = {}

    def counts(conn, name, gd, opp, index=None, person_id=None):
        seen["person_id"] = person_id
        return {"person_id": person_id, "rs_total": 30, "rs_before_season": 30, "counted_before_game": 30}
    monkeypatch.setattr(database, "player_data_counts", counts)
    monkeypatch.setattr(main, "_challenger_prediction",
                        lambda req, pid: seen.update(used=pid) or {"outcome": "OVER", "confidence": 0.6,
                                                                   "explanation": "x"})
    monkeypatch.setattr(data_fetcher, "get_opponent_injuries", lambda team: [])
    monkeypatch.setattr(database, "log_prediction", lambda **kw: 1)
    client = TestClient(main.app)
    body = {"player_name": "Luka Doncic", "opponent_team": "Utah Jazz", "stat_line": 31.5, "stat_type": "Points",
            "game_date": "2026-10-25", "league": "NBA"}
    assert client.post("/predict", json={**body, "person_id": LUKA}).status_code == 200
    assert seen == {"person_id": LUKA, "used": LUKA}
    r = client.post("/predict", json=body)                       # name alone, and it can't be narrowed
    assert r.status_code == 422 and r.json()["detail"] == "Several players match this name; pick one"


# --- retro comparison ------------------------------------------------------------------------

@pytest.fixture
def retro_env(tmp_path, monkeypatch):
    from test_challenger import TEAMS, _db
    from test_challenger_live import _small_challenger
    import retro_challenger
    path = str(tmp_path / "retro.db")
    _db(path)
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(database, "backfill_limited_data", lambda conn: {})
    database.init_db()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    games = conn.execute("SELECT personId, gameDateTimeEst, playerteamId, opponentteamId, points FROM player_stats "
                         "WHERE gameDateTimeEst < '2025-07-01' AND numMinutes > 0 AND gameType = 'Regular Season' "
                         "ORDER BY gameDateTimeEst DESC, personId LIMIT 8").fetchall()[::-1]
    start = games[0]["gameDateTimeEst"][:10]
    ins = ("INSERT INTO predictions (timestamp, player_name, stat_type, stat_line, opponent_team, predicted_outcome, "
           "confidence, explanation, actual_result, game_date, league, is_limited_data, model, is_current) "
           "VALUES (?, ?, ?, ?, ?, ?, ?, 'frozen', ?, ?, 'NBA', ?, ?, 1)")
    ids = []
    for k, g in enumerate(games):
        line = float(g["points"]) + (0.5 if k % 2 else -0.5)
        result = "OVER" if g["points"] > line else "UNDER"
        cur = conn.execute(ins, ("2025-01-01T00:00", f"P{g['personId']}", "Points", line, f"City N{g['opponentteamId']}",
                                 "OVER", 0.6, result, g["gameDateTimeEst"][:10], 0, "frozen"))
        ids.append(cur.lastrowid)
    g = games[0]
    excluded = [  # limited, a live challenger pick, another stat, unresolved, pending (all older picks)
        ("P%d" % g["personId"], "Points", 9.5, "OVER", 0.6, "OVER", g["gameDateTimeEst"][:10], 1, "frozen"),
        ("P%d" % g["personId"], "Points", 9.5, "OVER", 0.6, "OVER", g["gameDateTimeEst"][:10], 0, "challenger"),
        ("P%d" % g["personId"], "PRA", 30.5, "OVER", 0.6, "OVER", g["gameDateTimeEst"][:10], 0, "frozen"),
        ("Nobody", "Points", 9.5, "OVER", 0.6, "OVER", g["gameDateTimeEst"][:10], 0, "frozen"),
        ("P%d" % g["personId"], "Assists", 2.5, "OVER", 0.6, None, g["gameDateTimeEst"][:10], 0, "frozen")]
    for e in excluded:
        conn.execute(ins, ("2024-12-31T00:00", e[0], e[1], e[2], f"City N{g['opponentteamId']}", e[3], e[4], e[5],
                           e[6], e[7], e[8]))
    conn.commit()
    conn.close()
    ch = _small_challenger(path)
    monkeypatch.setattr(retro_challenger, "SEASON_START", start)
    monkeypatch.setattr(database, "resolve_person_id",
                        lambda conn, name, gd=None, opp="": int(name[1:]) if name.startswith("P") else None)
    return {"path": path, "ids": ids, "model": ch, "rc": retro_challenger, "games": games}


def test_retro_rows_are_scored_before_tip_graded_and_idempotent(retro_env):
    rc, ch = retro_env["rc"], retro_env["model"]
    conn = database.get_connection()
    recs, skipped = rc.run(conn, ch)
    assert skipped["inserted"] == len(retro_env["ids"]) and len(recs) == len(retro_env["ids"])
    assert skipped["player not resolved to one personId"] == 1
    rows = [dict(r) for r in conn.execute("SELECT * FROM predictions WHERE model = 'challenger_retro' ORDER BY retro_of")]
    assert [r["retro_of"] for r in rows] == retro_env["ids"]
    for r, g in zip(rows, retro_env["games"]):
        frozen = conn.execute("SELECT * FROM predictions WHERE id = ?", (r["retro_of"],)).fetchone()
        assert (r["player_name"], r["stat_type"], r["stat_line"], r["opponent_team"], r["game_date"]) == \
            (frozen["player_name"], frozen["stat_type"], frozen["stat_line"], frozen["opponent_team"], frozen["game_date"])
        assert r["actual_result"] == frozen["actual_result"] and r["is_current"] == 1 and r["is_limited_data"] == 0
        # the same live path at the box score's tip gives this pick and confidence
        team = conn.execute("SELECT playerteamName, opponentteamName, home FROM player_stats WHERE personId = ? "
                            "AND gameDateTimeEst = ?", (g["personId"], g["gameDateTimeEst"])).fetchone()
        F, as_of = live.features(conn, g["personId"], team[0], team[1], bool(team[2]), g["gameDateTimeEst"])
        assert as_of < g["gameDateTimeEst"]
        p = live.predict_from_features(F, "Points", [r["stat_line"]], ch)["p_over"][r["stat_line"]]
        assert r["predicted_outcome"] == ("OVER" if p > 0.5 else "UNDER") and r["confidence"] == round(max(p, 1 - p), 4)
    recs2, skipped2 = rc.run(conn, ch)                                   # idempotent
    assert skipped2["inserted"] == 0 and len(recs2) == len(recs)
    assert [x["p_challenger"] for x in recs2] == pytest.approx([x["p_challenger"] for x in recs], abs=1e-4)
    conn.close()
    s = rc.summarize(recs, boot=False)
    assert s["n"] == len(recs) and s["agree"]["n"] + s["disagree"]["n"] == s["n"]


def test_retro_rows_stay_out_of_track_records(retro_env):
    rc = retro_env["rc"]
    conn = database.get_connection()
    name = conn.execute("SELECT player_name FROM predictions WHERE id = ?", (retro_env["ids"][0],)).fetchone()[0]
    conn.close()
    before = database.player_track_record(name, "Points", "NBA")
    conn = database.get_connection()
    rc.run(conn, retro_env["model"])
    conn.execute("UPDATE predictions SET predicted_outcome = CASE predicted_outcome WHEN 'OVER' THEN 'UNDER' "
                 "ELSE 'OVER' END WHERE model = 'challenger_retro'")     # retro picks all disagree
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM predictions WHERE model = 'challenger_retro' AND player_name = ?",
                        (name,)).fetchone()[0] >= 1
    conn.close()
    assert database.player_track_record(name, "Points", "NBA") == before
