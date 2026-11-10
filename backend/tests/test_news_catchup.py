"""
The startup official-report catch-up (news_ingest.catch_up_nba_reports_job) failed on every
startup ~36 s in with "database is locked": it ran as soon as the news scheduler started, while
startup still held a write transaction, and gave up after the connection's 30 s busy timeout.
Now it starts from main._startup_complete (start_catch_up), and a still-locked catch-up
reschedules itself (CATCHUP_RETRY_MINUTES, at most CATCHUP_MAX_ATTEMPTS) instead of being lost.

The lock is reproduced with a real held write transaction (BEGIN IMMEDIATE on a second
connection). news_ingest.get_db's busy timeout is shortened so the test doesn't wait 30 s;
report downloads are stubbed (404), so nothing touches the network.
"""
import datetime as dt
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import news_ingest  # noqa: E402


class FakeScheduler:
    """Records add_job calls; nothing runs on its own."""

    def __init__(self):
        self.jobs = {}
        self.timezone = dt.timezone.utc

    def add_job(self, func, trigger=None, id=None, next_run_time=None, kwargs=None, replace_existing=False, **kw):
        if id in self.jobs and not replace_existing:
            raise AssertionError(f"job {id} already scheduled")
        self.jobs[id] = {"func": func, "trigger": trigger, "next_run_time": next_run_time, "kwargs": kwargs or {}}

    def start(self):
        pass

    def shutdown(self, wait=False):
        pass


class NotFound:
    status_code = 404
    content = b""


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "news.db")
    monkeypatch.setattr(news_ingest, "DB_PATH", path)
    real_get_db = news_ingest.get_db

    def short_timeout_db():                     # same connection, 0.2 s busy timeout instead of 30 s
        conn = real_get_db()
        conn.execute("PRAGMA busy_timeout = 200")
        return conn

    monkeypatch.setattr(news_ingest, "get_db", short_timeout_db)
    monkeypatch.setattr(news_ingest.httpx, "get", lambda *a, **k: NotFound())
    monkeypatch.setattr(news_ingest, "_scheduler", None)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")      # as in production (database.enable_wal)
    conn.close()
    news_ingest.init_tables()
    return path


@pytest.fixture
def held_write(db):
    """A connection holding a write transaction, as startup did."""
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO news_poll_log (polled_at, source) VALUES ('2026-11-09T00:00:00', 'startup')")
    yield conn
    if conn.in_transaction:
        conn.execute("ROLLBACK")
    conn.close()


def _poll_log(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT source FROM news_poll_log").fetchall()
    finally:
        conn.close()


def test_a_held_write_transaction_reproduces_the_lock(db, held_write):
    # The failure as it happened: the catch-up's insert into news_poll_log, while another
    # connection holds a write transaction, after the busy timeout.
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        news_ingest._job(lambda conn: news_ingest.poll_nba_report(conn, lookback_slots=2))


def test_the_scheduler_no_longer_starts_the_catch_up_during_startup(db, monkeypatch):
    import runmode
    import sched_tz
    fake = FakeScheduler()
    monkeypatch.setattr(runmode, "is_prod", lambda: True)
    monkeypatch.setenv("NEWS_INGEST_ENABLED", "1")
    monkeypatch.setattr(sched_tz, "new_scheduler", lambda: fake)
    news_ingest.start_scheduler()
    try:
        assert "news_nba_catchup" not in fake.jobs                     # was: next_run_time=now here
        assert {"news_espn", "news_nba_report"} <= set(fake.jobs)      # regular polls unchanged
        assert news_ingest.start_catch_up() is True                    # main._startup_complete
        job = fake.jobs["news_nba_catchup"]
        assert job["func"] is news_ingest.catch_up_nba_reports_job and job["kwargs"] == {}
        assert abs((job["next_run_time"] - dt.datetime.now(dt.timezone.utc)).total_seconds()) < 5
    finally:
        news_ingest.stop_scheduler()


def test_start_catch_up_without_the_poller_does_nothing(db):
    assert news_ingest.start_catch_up() is False


def test_a_locked_catch_up_reschedules_itself_then_succeeds(db, held_write, monkeypatch):
    fake = FakeScheduler()
    monkeypatch.setattr(news_ingest, "_scheduler", fake)
    assert news_ingest.catch_up_nba_reports_job() == 0                 # no exception: the lock is held
    retry = fake.jobs["news_nba_catchup"]
    assert retry["kwargs"] == {"attempt": 2}
    wait = (retry["next_run_time"] - dt.datetime.now(dt.timezone.utc)).total_seconds()
    assert abs(wait - news_ingest.CATCHUP_RETRY_MINUTES * 60) < 5

    held_write.execute("COMMIT")                                       # startup's transaction ends
    assert retry["func"](**retry["kwargs"]) == 0                       # nothing to ingest (404s) ...
    assert ("nba_injury_report",) in _poll_log(db)                     # ... and the poll is logged


def test_the_catch_up_gives_up_after_the_last_attempt(db, held_write, monkeypatch):
    fake = FakeScheduler()
    monkeypatch.setattr(news_ingest, "_scheduler", fake)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        news_ingest.catch_up_nba_reports_job(attempt=news_ingest.CATCHUP_MAX_ATTEMPTS)
    assert fake.jobs == {}


def test_other_database_errors_are_not_retried(db, monkeypatch):
    fake = FakeScheduler()
    monkeypatch.setattr(news_ingest, "_scheduler", fake)

    def broken(conn, **kw):
        raise sqlite3.OperationalError("no such table: nba_report_index")

    monkeypatch.setattr(news_ingest, "poll_nba_report", broken)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        news_ingest.catch_up_nba_reports_job()
    assert fake.jobs == {}


def test_catch_up_and_regular_report_poll_share_one_lock(db, monkeypatch):
    seen = []

    def record(conn, **kw):
        seen.append(news_ingest._REPORT_LOCK.locked())
        return 0

    monkeypatch.setattr(news_ingest, "poll_nba_report", record)
    news_ingest.catch_up_nba_reports_job()
    news_ingest.poll_nba_report_job()
    assert seen == [True, True]
