"""
Sync on startup and Sync now (line_logger.run_sync, POST /sync), under the relaxed ETL
guards: the pre-tip lock and the 10-minute pull window. The real run_etl loads temp
CSVs into a temp DB (so rows added are measured); Kaggle, backups of the CSV kind,
rosters and the resolvers are stubbed. Nothing touches predictions.db or the network.
"""
import datetime as dt
import os
import sys
import sqlite3
import threading
import time
import types

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import line_logger
import proc_lock

ET = line_logger.EASTERN
APP_PENDING = 47
PAPER_RESOLVED = 120


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


class _FakeScheduler:
    """Records add_job calls; get_jobs returns the given jobs."""
    def __init__(self, jobs=()):
        self.jobs, self.added = list(jobs), []

    def get_jobs(self):
        return self.jobs

    def add_job(self, fn, trigger, **kw):
        self.added.append(kw)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """
    Temp DB with Kaggle v520 (3 player_stats rows) loaded by the real run_etl and
    APP_PENDING unresolved app predictions. Kaggle now offers v521 (5 rows). The app
    resolver stub grades every pending row; the paper resolver reports PAPER_RESOLVED.
    """
    path = str(tmp_path / "test.db")
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(line_logger, "DB_PATH", path)
    monkeypatch.setattr(line_logger, "BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(line_logger, "OPEN_PULL_ENABLED", False)   # no 10:00 window
    monkeypatch.setattr(line_logger, "_last_sync", None)
    database.init_db()

    files = {520: _version_files(tmp_path, 3, "v520"), 521: _version_files(tmp_path, 5, "v521")}
    state = {"latest": 520, "etl_calls": [], "pretip_locked_during_etl": None}
    monkeypatch.setattr(database, "backup_predictions_to_csv", lambda: None)
    monkeypatch.setattr(database, "get_latest_kaggle_version", lambda: state["latest"])
    monkeypatch.setattr(database, "download_kaggle_files", lambda v: files[v])

    def app_resolve():
        conn = sqlite3.connect(path)
        n = conn.execute("UPDATE predictions SET actual_result = 'OVER' "
                         "WHERE actual_result IS NULL").rowcount
        conn.commit()
        conn.close()
        return n

    monkeypatch.setattr(database, "resolve_pending_predictions", app_resolve)
    assert database.run_etl() is True                     # v520 is the loaded version
    state["latest"] = 521

    conn = sqlite3.connect(path)
    conn.executemany(
        "INSERT INTO predictions (timestamp, player_name, stat_type, stat_line, opponent_team, "
        "predicted_outcome, confidence, explanation, game_date) VALUES (?,?,?,?,?,?,?,?,?)",
        [("2026-10-21T12:00:00", f"Player {i}", "Points", 10.5, "Knicks", "OVER", 0.6, "x",
          "2026-10-21") for i in range(APP_PENDING)])
    conn.commit()
    conn.close()

    real_run_etl = database.run_etl

    def run_etl():
        state["etl_calls"].append(time.monotonic())
        state["pretip_locked_during_etl"] = line_logger._PRETIP_LOCK.locked()
        return real_run_etl()

    monkeypatch.setattr(database, "run_etl", run_etl)
    monkeypatch.setitem(sys.modules, "rosters", types.SimpleNamespace(refresh_rosters=lambda: {"teams": 30}))
    monkeypatch.setitem(sys.modules, "player_match", types.SimpleNamespace(rebuild_now=dict))
    monkeypatch.setitem(sys.modules, "paper_predictions",
                        types.SimpleNamespace(resolve_paper_predictions=lambda: PAPER_RESOLVED))
    state["path"] = path
    return state


def _add_game(path, tip: dt.datetime):
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO schedule (gameId, gameDateTimeEst) VALUES (?, ?)",
                 (22600085, tip.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()


def _version(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT version FROM kaggle_version").fetchone()[0]
    finally:
        conn.close()


# --- startup sync -----------------------------------------------------------------

def test_startup_sync_loads_a_new_version_immediately(env, monkeypatch, capsys):
    monkeypatch.setattr(line_logger, "_scheduler", _FakeScheduler())
    t0 = time.monotonic()
    line_logger.start_sync_in_background().join(30)
    out = line_logger.last_sync()
    assert out["status"] == "loaded" and out["trigger"] == "startup"
    assert out["version_loaded"] == 521 and _version(env["path"]) == 521
    assert out["rows_added"] == 2                          # 5 rows in v521, 3 in v520
    assert out["app_resolved"] == APP_PENDING and out["paper_resolved"] == PAPER_RESOLVED
    assert len(env["etl_calls"]) == 1 and env["etl_calls"][0] - t0 < 10   # no 15-minute delay
    assert env["pretip_locked_during_etl"] is True         # pre-tip jobs queue behind the load
    assert out["rosters"] == "refreshed at startup"
    log = capsys.readouterr().out
    assert "[sync] start (startup)" in log
    for step in ("1/5 backup OK", "2/5 rosters", "3/5 Kaggle newest Ready v521, loaded v520",
                 "4/5 ETL OK: v521 loaded, 2 player_stats rows added", "5/5 resolved"):
        assert f"[sync] {step}" in log
    assert (f"[sync] Kaggle v521 loaded, 2 rows, {APP_PENDING} app + {PAPER_RESOLVED} "
            f"paper predictions resolved") in log


def test_startup_event_starts_the_sync_when_startup_completes(env, monkeypatch):
    import main
    steps = []

    def record(name, fn, *a, **kw):
        steps.append(name)
        return False, None

    monkeypatch.setattr(main, "_startup_step", record)
    try:
        main.startup_event()
    finally:
        if proc_lock.INSTANCE_LOCK.held:
            proc_lock.INSTANCE_LOCK.release()
    assert steps[-1] == "line_logger.start_sync_in_background"
    assert "database.run_etl" not in steps                 # no separate startup ETL


def test_startup_sync_with_nothing_newer_keeps_the_hourly_retry(env, monkeypatch):
    env["latest"] = 520
    sched = _FakeScheduler()
    out = line_logger.run_sync(sched, trigger="startup", refresh_rosters=False)
    assert out["status"] == "up to date" and env["etl_calls"] == []
    assert out["app_resolved"] == APP_PENDING              # resolve still runs
    [job] = sched.added
    assert job["id"] == "kaggle_version_retry" and job["kwargs"]["baseline"] == 520
    assert out["next_check"].startswith("check 1 at")


def test_sync_reuses_todays_backup_unless_an_etl_loaded_since(env, monkeypatch):
    taken = []
    real = line_logger.backup_database
    monkeypatch.setattr(line_logger, "backup_database", lambda: taken.append(1) or real())
    env["latest"] = 520
    line_logger.run_sync(None, trigger="a")
    line_logger.run_sync(None, trigger="b")
    assert taken == [1]                                    # today's exists: not retaken
    env["latest"] = 521
    line_logger.run_sync(None, trigger="c")                # loads v521
    line_logger.run_sync(None, trigger="d")                # backup now predates v521: retaken
    assert taken == [1, 1]


# --- pre-tip lock -----------------------------------------------------------------

def test_sync_waits_for_a_running_pretip_job(env, capsys):
    holding, released = threading.Event(), {}

    def pretip_job():
        with line_logger._PRETIP_LOCK:
            holding.set()
            time.sleep(1.0)
            released["at"] = time.monotonic()

    t = threading.Thread(target=pretip_job)
    t.start()
    holding.wait(5)
    out = line_logger.run_sync(_FakeScheduler(), trigger="sync now")
    t.join()
    assert out["status"] == "loaded" and out["version_loaded"] == 521
    [etl_at] = env["etl_calls"]
    assert etl_at >= released["at"]                        # never overlapped the job
    log = capsys.readouterr().out
    assert "[sync] ETL waiting for a running pre-tip job" in log
    assert "[sync] pre-tip job finished after" in log


def test_pretip_job_still_running_after_the_wait_defers(env, monkeypatch):
    monkeypatch.setattr(line_logger, "PRETIP_LOCK_WAIT_MINUTES", 0.01)   # 0.6 s
    line_logger._PRETIP_LOCK.acquire()
    try:
        sched = _FakeScheduler()
        out = line_logger.run_sync(sched, trigger="sync now")
    finally:
        line_logger._PRETIP_LOCK.release()
    assert out["status"] == "waited" and env["etl_calls"] == [] and _version(env["path"]) == 520
    assert "pre-tip job still running" in out["reason"]
    [job] = sched.added
    assert job["id"] == "kaggle_version_retry"


# --- 10-minute pull window -------------------------------------------------------------

def test_sync_skips_within_10_min_of_a_pretip_pull(env, capsys):
    tip = dt.datetime.now(ET) + dt.timedelta(minutes=line_logger.PRETIP_MINUTES + 5)
    _add_game(env["path"], tip)                            # its pre-tip pull is 5 min away
    sched = _FakeScheduler()
    out = line_logger.run_sync(sched, trigger="sync now")
    assert out["status"] == "waited" and env["etl_calls"] == [] and _version(env["path"]) == 520
    assert out["reason"] == f"within {line_logger.PULL_WINDOW_MINUTES} min of a scheduled pull"
    [job] = sched.added                                    # deferred to the end of the window
    pull = tip.replace(microsecond=0) - dt.timedelta(minutes=line_logger.PRETIP_MINUTES)
    assert job["run_date"] == pull + dt.timedelta(minutes=line_logger.PULL_WINDOW_MINUTES)
    assert job["kwargs"]["baseline"] == 520
    assert out["app_resolved"] == APP_PENDING              # resolve still runs
    assert "[sync] Kaggle v521 ready but the ETL waited (within 10 min of a scheduled pull)" in \
        capsys.readouterr().out


def test_sync_skips_within_10_min_of_a_scheduled_pretip_job(env):
    job = types.SimpleNamespace(id="pretip:abc", next_run_time=dt.datetime.now(dt.timezone.utc)
                                + dt.timedelta(minutes=9))
    out = line_logger.run_sync(_FakeScheduler([job]), trigger="sync now")
    assert out["status"] == "waited" and env["etl_calls"] == []


def test_sync_runs_11_min_before_a_pull_and_45_min_before_tip(env):
    """Outside the window it runs: the old 90/60-minute rules would have skipped this."""
    _add_game(env["path"], dt.datetime.now(ET) + dt.timedelta(minutes=line_logger.PRETIP_MINUTES + 15))
    job = types.SimpleNamespace(id="pretip:abc", next_run_time=dt.datetime.now(dt.timezone.utc)
                                + dt.timedelta(minutes=11))
    out = line_logger.run_sync(_FakeScheduler([job]), trigger="sync now")
    assert out["status"] == "loaded" and _version(env["path"]) == 521


# --- Sync now: POST /sync --------------------------------------------------------------

@pytest.fixture
def client(env, monkeypatch):
    from fastapi.testclient import TestClient
    import main
    monkeypatch.setattr(line_logger, "_scheduler", _FakeScheduler())
    monkeypatch.setattr(main, "LOCAL_HOSTS", main.LOCAL_HOSTS | {"testclient"})
    return TestClient(main.app)                            # no `with`: startup does not run


def test_sync_now_runs_and_reports(client):
    r = client.post("/sync", headers={"Origin": "http://localhost:5173"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "loaded" and body["trigger"] == "sync now"
    assert body["version_loaded"] == 521 and body["rows_added"] == 2
    assert body["app_resolved"] == APP_PENDING and body["paper_resolved"] == PAPER_RESOLVED
    assert body["summary"] == (f"Kaggle v521 loaded, 2 rows, {APP_PENDING} app + "
                               f"{PAPER_RESOLVED} paper predictions resolved")
    assert client.get("/sync").json()["last"]["summary"] == body["summary"]


def test_sync_now_reports_why_it_waited(client, env):
    _add_game(env["path"], dt.datetime.now(ET) + dt.timedelta(minutes=line_logger.PRETIP_MINUTES + 5))
    body = client.post("/sync").json()
    assert body["status"] == "waited"
    assert body["reason"] == f"within {line_logger.PULL_WINDOW_MINUTES} min of a scheduled pull"
    assert body["next_check"].startswith("check 1 at")


def test_sync_now_is_busy_while_another_sync_runs(client, env):
    held, done = threading.Event(), threading.Event()

    def other_sync():
        proc_lock.ETL_LOCK.acquire(who="test")
        held.set()
        done.wait(10)
        proc_lock.ETL_LOCK.release()

    t = threading.Thread(target=other_sync)
    t.start()
    held.wait(5)
    try:
        t0 = time.monotonic()
        body = client.post("/sync").json()
        assert time.monotonic() - t0 < 5                   # returns at once, doesn't queue
    finally:
        done.set()
        t.join()
    assert body["status"] == "busy" and env["etl_calls"] == []


def test_sync_now_is_localhost_only(env, monkeypatch):
    from fastapi.testclient import TestClient
    import main
    c = TestClient(main.app)                               # client host "testclient": not local
    assert c.post("/sync").status_code == 403
    monkeypatch.setattr(main, "LOCAL_HOSTS", main.LOCAL_HOSTS | {"testclient"})
    assert c.post("/sync", headers={"Origin": "https://evil.example"}).status_code == 403
    assert env["etl_calls"] == []
