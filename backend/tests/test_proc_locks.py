"""
Cross-process ETL lock, single-instance startup lock, atomic ETL table swap.
Temp DBs and temp CSVs; Kaggle stubbed; real separate processes where the claim is
"cross-process". Nothing touches predictions.db or the network.
"""
import datetime as dt
import os
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time

import pandas as pd
import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

import database
import line_logger
import proc_lock


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "test.db")
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(line_logger, "DB_PATH", path)
    database.init_db()
    return path


def _version_files(tmp_path, n, tag):
    """CSV files for one Kaggle version with n player_stats rows, one per personId."""
    d = tmp_path / tag
    d.mkdir()
    df = pd.DataFrame([{c: 0 for c in database._STATS_COLS}] * n)
    df["personId"] = range(n)
    df["gameId"] = 22600001
    df["gameDateTimeEst"] = "2026-10-21 19:30:00"
    df["gameType"] = "Regular Season"
    df["numMinutes"] = 30
    df.to_csv(d / "stats.csv", index=False)
    pd.DataFrame({"personId": [1], "firstName": ["A"], "lastName": ["B"]}).to_csv(d / "p.csv", index=False)
    pd.DataFrame([{c: 1 for c in database._SCHEDULE_COLS}]).to_csv(d / "sc.csv", index=False)
    return {"stats": str(d / "stats.csv"), "players": str(d / "p.csv"), "schedule": str(d / "sc.csv")}


def _count(path, table="player_stats"):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


# --- #2: cross-process ETL lock --------------------------------------------------

ETL_CHILD = textwrap.dedent("""
    import json, sys, time
    backend, db, files, marker = sys.argv[1:5]
    sys.path.insert(0, backend)
    import database
    database.DB_PATH = db
    database.backup_predictions_to_csv = lambda: None
    database.resolve_pending_predictions = lambda: 0
    database.get_latest_kaggle_version = lambda: 523
    def download(v):
        with open(marker, "a") as f:
            f.write("load\\n")
        time.sleep(1.5)            # hold the lock long enough for the other process to arrive
        return json.loads(files)
    database.download_kaggle_files = download
    print("RESULT", database.run_etl(), flush=True)
""")


def test_two_concurrent_run_etl_processes_load_once(db, tmp_path):
    import json
    files = _version_files(tmp_path, 30_000, "v523")
    marker = tmp_path / "loads.txt"
    script = tmp_path / "child.py"
    script.write_text(ETL_CHILD)
    args = [sys.executable, str(script), BACKEND, db, json.dumps(files), str(marker)]
    a = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(0.3)
    b = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out_a, out_b = a.communicate(timeout=120)[0], b.communicate(timeout=120)[0]
    assert a.returncode == 0 and b.returncode == 0, out_a + out_b
    assert marker.read_text().count("load") == 1                 # exactly one download/load
    assert "RESULT True" in out_a and "RESULT False" in out_b    # the second found it loaded
    assert "waiting for the etl lock (held by another process)" in out_b
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM player_stats").fetchone()[0] == 30_000
    assert conn.execute("SELECT COUNT(*) FROM (SELECT personId FROM player_stats "
                        "GROUP BY personId HAVING COUNT(*) > 1)").fetchone()[0] == 0
    assert conn.execute("SELECT version FROM kaggle_version").fetchone()[0] == 523
    conn.close()


HOLD_CHILD = textwrap.dedent("""
    import sys, time
    backend, db, name, ready = sys.argv[1:5]
    sys.path.insert(0, backend)
    import database, proc_lock
    database.DB_PATH = db
    lock = proc_lock.ETL_LOCK if name == "etl" else proc_lock.INSTANCE_LOCK
    assert lock.acquire(timeout=0)
    open(ready, "w").close()
    time.sleep(60)
""")


@pytest.fixture
def held_by_other_process(db, tmp_path):
    """Start a process holding lock `name` (for the temp DB); kill it afterwards."""
    procs = []

    def hold(name):
        script = tmp_path / f"hold_{name}.py"
        script.write_text(HOLD_CHILD)
        ready = tmp_path / f"ready_{name}"
        p = subprocess.Popen([sys.executable, str(script), BACKEND, db, name, str(ready)])
        procs.append(p)
        for _ in range(200):
            if ready.exists():
                return p
            time.sleep(0.05)
        raise RuntimeError("lock holder did not start")

    yield hold
    for p in procs:
        p.kill()
        p.wait()


def test_maintenance_skips_while_another_process_holds_the_etl_lock(db, held_by_other_process,
                                                                     monkeypatch, capsys):
    held_by_other_process("etl")
    monkeypatch.setattr(line_logger, "MAINTENANCE_LOCK_WAIT_MINUTES", 0.02)   # ~1 s
    ran = []
    monkeypatch.setattr(line_logger, "_run_morning_maintenance", lambda s=None: ran.append(1))
    assert line_logger.run_morning_maintenance() == {"skipped": "etl lock held"}
    assert ran == []
    out = capsys.readouterr().out
    assert "waiting for the etl lock (held by another process)" in out
    assert "[maintenance] SKIPPED" in out


def test_kaggle_retry_defers_while_maintenance_runs_in_another_thread(db, monkeypatch):
    class Sched:
        added = []

        def get_jobs(self):
            return []

        def add_job(self, fn, trigger, **kw):
            self.added.append(kw)

    sched = Sched()
    monkeypatch.setattr(line_logger, "OPEN_PULL_ENABLED", False)
    inside, release = threading.Event(), threading.Event()

    def slow_maintenance(scheduler=None):
        inside.set()
        release.wait(10)
        return {}

    monkeypatch.setattr(line_logger, "_run_morning_maintenance", slow_maintenance)
    t = threading.Thread(target=line_logger.run_morning_maintenance)
    t.start()
    inside.wait(10)
    checked = []
    monkeypatch.setattr(line_logger, "_run_kaggle_version_retry", lambda *a: checked.append(1))
    day = (dt.datetime.now(line_logger.EASTERN)
           + dt.timedelta(minutes=line_logger.KAGGLE_RETRY_MINUTES)).date().isoformat()
    assert line_logger.run_kaggle_version_retry(sched, "/b.db", 522, day, attempt=2) == "deferred: etl lock"
    release.set()
    t.join()
    assert checked == [] and sched.added[0]["kwargs"]["attempt"] == 3


def test_etl_lock_is_reentrant_for_maintenance_calling_run_etl(db, monkeypatch):
    calls = []
    monkeypatch.setattr(database, "_run_etl", lambda: calls.append(1) or False)
    assert proc_lock.ETL_LOCK.acquire(timeout=0)
    try:
        assert database.run_etl() is False        # same thread: no deadlock
    finally:
        proc_lock.ETL_LOCK.release()
    assert calls == [1] and not proc_lock.ETL_LOCK.held


# --- #6: single-instance startup lock --------------------------------------------

@pytest.fixture
def startup(db, monkeypatch):
    """main.startup_event with every step recorded instead of run."""
    import main
    steps = []

    def record(name, fn, *a, **kw):
        steps.append(name)
        return False, None          # also ends startup early (ml_trainer "failed")

    monkeypatch.setattr(main, "_startup_step", record)
    yield main, steps
    if proc_lock.INSTANCE_LOCK.held:
        proc_lock.INSTANCE_LOCK.release()


def test_second_instance_starts_no_scheduler(startup, held_by_other_process, capsys):
    main, steps = startup
    held_by_other_process("instance")
    with pytest.raises(RuntimeError, match="another backend instance"):
        main.startup_event()
    assert steps == []                         # no WAL, rosters, scheduler, ETL, resolve
    assert "[startup] REFUSED" in capsys.readouterr().out


def test_first_instance_takes_the_lock_before_starting_anything(startup):
    main, steps = startup
    main.startup_event()
    assert proc_lock.INSTANCE_LOCK.held
    assert "line_logger.start_scheduler" in steps


# --- #4: atomic swap ------------------------------------------------------------

@pytest.fixture
def stub_kaggle(monkeypatch):
    monkeypatch.setattr(database, "backup_predictions_to_csv", lambda: None)
    monkeypatch.setattr(database, "resolve_pending_predictions", lambda: 0)

    def load(version, files):
        monkeypatch.setattr(database, "get_latest_kaggle_version", lambda: version)
        monkeypatch.setattr(database, "download_kaggle_files", lambda v: files)
        return database.run_etl()

    return load


def test_reader_sees_only_old_or_new_table_during_a_load(db, tmp_path, stub_kaggle):
    old, new = 40_000, 150_000
    stub_kaggle(522, _version_files(tmp_path, old, "v522"))
    assert _count(db) == old
    seen, done = set(), threading.Event()

    def reader():
        while not done.is_set():
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
            try:
                seen.add(c.execute("SELECT COUNT(*) FROM player_stats").fetchone()[0])
            finally:
                c.close()
            time.sleep(0.002)

    t = threading.Thread(target=reader)
    t.start()
    try:
        assert stub_kaggle(523, _version_files(tmp_path, new, "v523")) is True
    finally:
        done.set()
        t.join()
    assert seen <= {old, new} and old in seen
    assert _count(db) == new


def test_swap_leaves_no_staging_tables_and_indexes_live_tables(db, tmp_path, stub_kaggle):
    stub_kaggle(522, _version_files(tmp_path, 100, "v522"))
    stub_kaggle(523, _version_files(tmp_path, 120, "v523"))
    conn = sqlite3.connect(db)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    idx = {r[0] for r in conn.execute("SELECT tbl_name FROM sqlite_master WHERE type = 'index'")}
    plan = " ".join(r[3] for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM player_stats WHERE personId = 5"))
    conn.close()
    assert not any(n.endswith(database.STAGING_SUFFIX) for n in names)
    assert {"player_stats", "schedule"} <= idx
    assert "USING INDEX" in plan


def test_failed_load_keeps_old_tables_and_drops_staging(db, tmp_path, stub_kaggle, monkeypatch):
    stub_kaggle(522, _version_files(tmp_path, 100, "v522"))
    files = _version_files(tmp_path, 120, "v523")

    def broken_schedule(path):
        raise ValueError("schedule csv broken")

    monkeypatch.setattr(database, "read_schedule_csv", broken_schedule)
    with pytest.raises(ValueError):
        stub_kaggle(523, files)
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM player_stats").fetchone()[0] == 100
    assert conn.execute("SELECT version FROM kaggle_version").fetchone()[0] == 522
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name LIKE '%__new'").fetchone()
    conn.close()
