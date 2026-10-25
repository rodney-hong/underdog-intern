"""
Player track record follows History's model filter: challenger / frozen / challenger_retro /
all (= challenger + frozen), over the rows the stats cards count (current, not limited-data,
resolved OVER/UNDER); the all-players baseline uses the same model and filters. Temp DB.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database  # noqa: E402

COLS = ("timestamp", "player_name", "stat_type", "stat_line", "opponent_team", "predicted_outcome", "confidence",
        "explanation", "actual_result", "game_date", "league", "is_limited_data", "model", "is_current")


def _row(player, model, pick, actual, *, limited=0, current=1, date="2026-10-21", line=20.5, stat="Points"):
    return (f"{date}T12:00:00", player, stat, line, "Boston Celtics", pick, 0.6, "e", actual, date, "NBA",
            limited, model, current)


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "tr.db")
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(database, "backfill_limited_data", lambda conn: {})
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE predictions (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, "
                 "player_name TEXT NOT NULL, stat_type TEXT NOT NULL, stat_line REAL NOT NULL, opponent_team TEXT NOT NULL, "
                 "predicted_outcome TEXT NOT NULL, confidence REAL NOT NULL, explanation TEXT NOT NULL, actual_result TEXT)")
    conn.commit()
    conn.close()
    database.init_db()
    rows = [
        # Jay: challenger 2-1, frozen 1-1, retro 0-2
        _row("Jay", "challenger", "OVER", "OVER"), _row("Jay", "challenger", "UNDER", "UNDER", date="2026-10-22"),
        _row("Jay", "challenger", "OVER", "UNDER", date="2026-10-23"),
        _row("Jay", "frozen", "OVER", "OVER"), _row("Jay", "frozen", "OVER", "UNDER", date="2026-10-22"),
        _row("Jay", "challenger_retro", "UNDER", "OVER"), _row("Jay", "challenger_retro", "UNDER", "OVER", date="2026-10-22"),
        # never counted: superseded, limited-data, pending, DNP, another stat
        _row("Jay", "challenger", "UNDER", "UNDER", current=0, line=19.5),
        _row("Jay", "challenger", "UNDER", "UNDER", limited=1, date="2026-10-24"),
        _row("Jay", "challenger", "UNDER", None, date="2026-10-25"),
        _row("Jay", "challenger", "UNDER", "DNP", date="2026-10-20"),
        _row("Jay", "challenger", "OVER", "OVER", stat="Rebounds"),
        # another player, for the baseline: challenger 0-1, frozen 1-0
        _row("Kim", "challenger", "OVER", "UNDER"), _row("Kim", "frozen", "OVER", "OVER")]
    conn = sqlite3.connect(path)
    conn.executemany(f"INSERT INTO predictions ({', '.join(COLS)}) VALUES ({', '.join('?' * len(COLS))})", rows)
    conn.commit()
    conn.close()
    return path


def _wl(model, player="Jay", **kw):
    tr = database.player_track_record(player, "Points", "NBA", model=model, **kw)
    return tr["wins"], tr["losses"], tr["overall_n"], tr["overall_rate"]


def test_each_model_counts_only_its_own_current_resolved_rows(db):
    assert _wl("challenger") == (2, 1, 4, 0.5)           # baseline: Jay 2-1 + Kim 0-1
    assert _wl("frozen") == (1, 1, 3, 2 / 3)             # Jay 1-1 + Kim 1-0
    assert _wl("challenger_retro") == (0, 2, 2, 0.0)
    assert _wl("all") == (3, 2, 7, 4 / 7)                # challenger + frozen, never retro


def test_default_is_all_and_before_still_applies(db):
    assert database.player_track_record("Jay", "Points")["n"] == 5
    w, l, n, _ = _wl("challenger", before="2026-10-23T16:00:00")      # naive = UTC; 12:00 ET on 10-23
    assert (w, l) == (2, 0)                              # the 10-23 loss is on/after the cutoff date


def test_unknown_model_is_refused(db):
    with pytest.raises(ValueError):
        database.player_track_record("Jay", "Points", model="paper")


def test_endpoint_passes_the_model(db):
    from fastapi.testclient import TestClient
    import main
    client = TestClient(main.app)
    body = client.get("/players/Jay/track-record", params={"stat": "Points", "model": "frozen"}).json()
    assert (body["wins"], body["losses"], body["model"], body["overall_n"]) == (1, 1, "frozen", 3)
    assert client.get("/players/Jay/track-record", params={"stat": "Points"}).json()["n"] == 5
    assert client.get("/players/Jay/track-record", params={"model": "paper"}).status_code == 422
