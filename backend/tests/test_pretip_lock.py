"""
Pre-tip jobs run one at a time (line_logger._PRETIP_LOCK). Five games sharing a tip
time run their jobs at once, as APScheduler's thread pool does; the real pull_event
writes line_snapshots and odds_pull_log, and a stand-in log_for_pull holds a write
transaction like the real one. The busy timeout is cut to 0.5 s so contention shows
in seconds instead of 30. Temp DB, OddsAPI stubbed: no network, no predictions.db.
"""
import datetime as dt
import os
import sqlite3
import sys
import threading
import time
import types

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import line_logger

TIMEOUT = 0.5      # seconds; production uses 30
PAPER_HOLD = 0.8   # seconds a stand-in log_for_pull keeps its write transaction open


def _event(i):
    tip = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=29)
    return {"id": f"evt{i}", "commence_time": tip.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "home_team": f"Home {i}", "away_team": f"Away {i}"}


def _odds_body():
    outcomes = [{"name": side, "description": "Some Player", "point": 10.5, "price": -110}
                for side in ("Over", "Under")]
    return {"bookmakers": [{"key": "underdog", "markets": [
        {"key": "player_points_alternate", "outcomes": outcomes}]}]}


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    path = str(tmp_path / "test.db")
    monkeypatch.setattr(line_logger, "DB_PATH", path)
    monkeypatch.setattr(database, "DB_PATH", path)
    sqlite3.connect(path).execute("PRAGMA journal_mode=WAL").close()

    def get_db():
        conn = sqlite3.connect(path, timeout=TIMEOUT)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(line_logger, "get_db", get_db)
    monkeypatch.setattr(line_logger, "fetch_todays_events", lambda phase=None: ([], 1000))
    monkeypatch.setattr(line_logger, "MONTHLY_CREDIT_CAP", 10_000)
    monkeypatch.setattr(line_logger, "_scheduler", None)
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(
        200, json=_odds_body(), request=httpx.Request("GET", url),
        headers={"x-requests-remaining": "1000", "x-requests-last": "8"}))
    monkeypatch.setenv("PAPER_PREDICTIONS_ENABLED", "1")
    line_logger.init_tables()
    sqlite3.connect(path).execute("CREATE TABLE paper_stub (game_id TEXT)").close()

    state = {"paper_ok": [], "errors": [], "spans": []}

    def log_for_pull(game_id):
        conn = sqlite3.connect(path, timeout=TIMEOUT)
        try:
            conn.execute("INSERT INTO paper_stub VALUES (?)", (game_id,))  # write txn opens here
            time.sleep(PAPER_HOLD)
            conn.commit()
            state["paper_ok"].append(game_id)
        finally:
            conn.close()
        return {"logged": 1}

    monkeypatch.setitem(sys.modules, "paper_predictions",
                        types.SimpleNamespace(log_for_pull=log_for_pull))
    # challenger rows follow the paper rows inside the same lock; the real challenger_log binds
    # DB_PATH at import (the live predictions.db if an earlier test imported it), so stub it
    monkeypatch.setitem(sys.modules, "challenger_log",
                        types.SimpleNamespace(log_for_pull=lambda game_id: {"logged": 0}))

    def run_all(n=5):
        def job(ev):
            t0 = time.monotonic()
            try:
                line_logger.run_pretip_pull(ev)
            except Exception as exc:
                state["errors"].append(repr(exc))
            state["spans"].append((t0, time.monotonic()))

        threads = [threading.Thread(target=job, args=(_event(i),)) for i in range(n)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        conn = sqlite3.connect(path)
        state["pulled"] = conn.execute(
            "SELECT COUNT(DISTINCT game_id) FROM odds_pull_log WHERE pull_phase = 'pretip' "
            "AND http_status = 200").fetchone()[0]
        state["snapshots"] = conn.execute(
            "SELECT COUNT(DISTINCT game_id) FROM line_snapshots").fetchone()[0]
        conn.close()
        return state

    return run_all


def test_five_simultaneous_pretip_jobs_all_succeed(jobs, capsys):
    s = jobs(5)
    assert s["errors"] == []
    assert sorted(s["paper_ok"]) == [f"evt{i}" for i in range(5)]
    assert s["pulled"] == 5 and s["snapshots"] == 5
    out = capsys.readouterr().out
    assert "database is locked" not in out
    assert out.count("waiting for another pre-tip job") == 4   # every job but the first waited
    assert out.count("waited ") == 4


def test_without_the_lock_the_same_jobs_fail(jobs, monkeypatch, capsys):
    """Control: the setup above does reproduce the failure when jobs overlap."""
    class NoLock:
        def acquire(self, blocking=True):
            return True

        def release(self):
            pass

    monkeypatch.setattr(line_logger, "_PRETIP_LOCK", NoLock())
    s = jobs(5)
    failures = s["errors"] + [l for l in capsys.readouterr().out.splitlines() if "locked" in l]
    assert any("database is locked" in f for f in failures)
    assert len(s["paper_ok"]) < 5 or s["snapshots"] < 5


def test_lock_released_after_a_failing_job(jobs, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("pull failed")

    monkeypatch.setattr(line_logger, "_run_pretip_pull", boom)
    with pytest.raises(RuntimeError):
        line_logger.run_pretip_pull(_event(0))
    assert line_logger._PRETIP_LOCK.acquire(blocking=False)
    line_logger._PRETIP_LOCK.release()
