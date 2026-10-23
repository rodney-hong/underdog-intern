"""
Kaggle 404 path and tip-off guard for the ETL / morning maintenance.
Every test runs against a temp DB with Kaggle, backups, rosters and resolvers stubbed:
nothing touches predictions.db or the network.
"""
import datetime as dt
import os
import shutil
import sqlite3
import sys
import types

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import line_logger

ET = line_logger.EASTERN


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "test.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE player_stats (personId INTEGER, gameDateTimeEst TEXT)")
    conn.execute("INSERT INTO player_stats VALUES (1628369, '2026-10-16 19:30:00')")
    conn.execute("CREATE TABLE players (personId INTEGER)")
    conn.execute("INSERT INTO players VALUES (1628369)")
    conn.execute("CREATE TABLE schedule (gameId INTEGER, gameDateTimeEst TEXT)")
    conn.execute("CREATE TABLE kaggle_version (version INTEGER)")
    conn.execute("INSERT INTO kaggle_version VALUES (520)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(line_logger, "DB_PATH", path)
    return path


def _snapshot(path):
    conn = sqlite3.connect(path)
    try:
        return {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
                for t in ("player_stats", "players", "schedule", "kaggle_version")}
    finally:
        conn.close()


def _http_404():
    resp = requests.Response()
    resp.status_code = 404
    return requests.HTTPError("404 Client Error: Not Found", response=resp)


def _add_games(path, *rows):
    conn = sqlite3.connect(path)
    conn.executemany("INSERT INTO schedule VALUES (?, ?)", rows)
    conn.commit()
    conn.close()


# --- 404 path -----------------------------------------------------------------

class _FakeClient:
    """build_kaggle_client() stand-in whose file list returns the given names."""
    def __init__(self, names):
        files = [types.SimpleNamespace(name=n) for n in names]
        api = types.SimpleNamespace(
            list_dataset_files=lambda r: types.SimpleNamespace(dataset_files=files))
        self.datasets = types.SimpleNamespace(dataset_api_client=api)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_download_kaggle_files_404_raises_unavailable(monkeypatch):
    import kagglehub
    import kagglehub.clients
    monkeypatch.setattr(kagglehub.clients, "build_kaggle_client", lambda: _FakeClient(
        ["PlayerStatisticsExtended.csv", "Players.csv", "LeagueSchedule25_26.csv",
         "LeagueSchedule26_27.csv"]))
    requested = []

    def fake_download(handle, path=None, **kw):
        requested.append((handle, path))
        if path == "Players.csv":
            raise _http_404()
        return f"/cache/{path}"

    monkeypatch.setattr(kagglehub, "dataset_download", fake_download)
    with pytest.raises(database.KaggleVersionUnavailable, match="v521: Players.csv returned 404"):
        database.download_kaggle_files(521)
    assert requested[0] == (f"{database.KAGGLE_DATASET}/versions/521", "PlayerStatisticsExtended.csv")
    assert all(p is not None for _, p in requested), "never fall back to the whole-dataset archive"


def test_download_kaggle_files_picks_latest_schedule(monkeypatch):
    import kagglehub
    import kagglehub.clients
    monkeypatch.setattr(kagglehub.clients, "build_kaggle_client", lambda: _FakeClient(
        ["Players.csv", "LeagueSchedule26_27.csv", "LeagueSchedule25_26.csv",
         "PlayerStatisticsExtended.csv"]))
    monkeypatch.setattr(kagglehub, "dataset_download", lambda h, path=None, **kw: f"/cache/{path}")
    files = database.download_kaggle_files(521)
    assert files == {"stats": "/cache/PlayerStatisticsExtended.csv", "players": "/cache/Players.csv",
                     "schedule": "/cache/LeagueSchedule26_27.csv"}


def test_download_kaggle_files_other_http_error_is_not_unavailable(monkeypatch):
    import kagglehub
    import kagglehub.clients
    monkeypatch.setattr(kagglehub.clients, "build_kaggle_client", lambda: _FakeClient(
        ["PlayerStatisticsExtended.csv", "Players.csv", "LeagueSchedule26_27.csv"]))
    resp = requests.Response()
    resp.status_code = 500

    def boom(h, path=None, **kw):
        raise requests.HTTPError("500", response=resp)

    monkeypatch.setattr(kagglehub, "dataset_download", boom)
    with pytest.raises(requests.HTTPError) as ei:
        database.download_kaggle_files(521)
    assert not isinstance(ei.value, database.KaggleVersionUnavailable)


def test_run_etl_404_leaves_tables_untouched(db, monkeypatch):
    before = _snapshot(db)
    monkeypatch.setattr(database, "backup_predictions_to_csv", lambda: None)
    monkeypatch.setattr(database, "get_latest_kaggle_version", lambda: 521)

    def unavailable(version):
        raise database.KaggleVersionUnavailable(f"v{version}: PlayerStatisticsExtended.csv returned 404")

    monkeypatch.setattr(database, "download_kaggle_files", unavailable)
    with pytest.raises(database.KaggleVersionUnavailable):
        database.run_etl()
    assert _snapshot(db) == before  # kaggle_version still 520, so the next run retries


@pytest.fixture
def maintenance(db, monkeypatch):
    """Stub every maintenance step except the ETL decision; record what happened."""
    calls = {"etl": 0, "restore": 0, "resolve": 0}
    monkeypatch.setattr(line_logger, "backup_database", lambda: "/backups/predictions_test.db")
    monkeypatch.setitem(sys.modules, "rosters", types.SimpleNamespace(refresh_rosters=dict))

    def resolve():
        calls["resolve"] += 1
        return 0

    monkeypatch.setitem(sys.modules, "paper_predictions",
                        types.SimpleNamespace(resolve_paper_predictions=resolve))

    def restore(path):
        calls["restore"] += 1
        return {}

    monkeypatch.setattr(line_logger, "restore_etl_tables", restore)

    def set_etl(fn):
        def wrapped():
            calls["etl"] += 1
            return fn()
        monkeypatch.setattr(database, "run_etl", wrapped)

    calls["set_etl"] = set_etl
    return calls


def test_maintenance_404_skips_without_restore(maintenance):
    def unavailable():
        raise database.KaggleVersionUnavailable("v521: PlayerStatisticsExtended.csv returned 404")

    maintenance["set_etl"](unavailable)
    out = line_logger.run_morning_maintenance()
    assert out["etl"].startswith("skipped: newest Kaggle version not downloadable")
    assert "restored" not in out and maintenance["restore"] == 0
    assert maintenance["resolve"] == 1


def test_maintenance_other_failure_still_restores(maintenance):
    def broken():
        raise RuntimeError("partial load")

    maintenance["set_etl"](broken)
    out = line_logger.run_morning_maintenance()
    assert out["etl"].startswith("FAILED")
    assert maintenance["restore"] == 1 and maintenance["resolve"] == 1


# --- tip-off guard ------------------------------------------------------------

NOW = dt.datetime(2026, 10, 21, 17, 30, tzinfo=ET)


def test_tipoffs_within_window(db):
    _add_games(db,
               (1, "2026-10-21 17:00:00"),   # already tipped: not in window
               (2, "2026-10-21 18:59:00"),   # +89 min
               (3, "2026-10-21 19:00:00"),   # +90 min, inclusive
               (4, "2026-10-21 19:01:00"),   # +91 min
               (5, "2026-10-22 18:00:00"))   # tomorrow
    assert line_logger._tipoffs_within(90, now=NOW) == ["2 @ 18:59 ET", "3 @ 19:00 ET"]


def test_tipoffs_within_converts_utc_now_to_et(db):
    _add_games(db, (2, "2026-10-21 18:59:00"))
    assert line_logger._tipoffs_within(90, now=NOW.astimezone(dt.timezone.utc)) == ["2 @ 18:59 ET"]


def test_tipoffs_within_no_schedule_table(tmp_path, monkeypatch):
    monkeypatch.setattr(line_logger, "DB_PATH", str(tmp_path / "empty.db"))
    assert line_logger._tipoffs_within(90, now=NOW) == []


def test_manual_maintenance_skips_etl_before_tipoff(maintenance, db):
    soon = dt.datetime.now(ET) + dt.timedelta(minutes=45)
    _add_games(db, (22600085, soon.strftime("%Y-%m-%d %H:%M:%S")))
    maintenance["set_etl"](lambda: True)
    out = line_logger.run_morning_maintenance()  # no scheduler: manual run
    assert out["etl"].startswith(f"skipped: game(s) tip within {line_logger.ETL_TIPOFF_GUARD_MINUTES} min")
    assert maintenance["etl"] == 0 and maintenance["resolve"] == 1


def test_manual_maintenance_runs_etl_when_no_tipoff_soon(maintenance, db):
    later = dt.datetime.now(ET) + dt.timedelta(minutes=line_logger.ETL_TIPOFF_GUARD_MINUTES + 30)
    _add_games(db, (22600085, later.strftime("%Y-%m-%d %H:%M:%S")))
    maintenance["set_etl"](lambda: False)
    out = line_logger.run_morning_maintenance()
    assert out["etl"] == "up to date" and maintenance["etl"] == 1


# --- version check --------------------------------------------------------------

def test_latest_version_uses_newest_ready_not_lagging_current(monkeypatch):
    import kagglehub.clients
    v = lambda n, s="Ready": types.SimpleNamespace(version_number=n, status=s)
    dataset = types.SimpleNamespace(current_version_number=522,
                                    versions=[v(524, "Pending"), v(523), v(522), v(521)])
    api = types.SimpleNamespace(get_dataset=lambda r: dataset)

    class Client(_FakeClient):
        def __init__(self):
            self.datasets = types.SimpleNamespace(dataset_api_client=api)

    monkeypatch.setattr(kagglehub.clients, "build_kaggle_client", Client)
    assert database.get_latest_kaggle_version() == 523


# --- hourly Kaggle version retry -------------------------------------------------

class _FakeScheduler:
    """Records add_job calls; get_jobs returns the given pre-tip jobs."""
    def __init__(self, jobs=()):
        self.jobs, self.added = list(jobs), []

    def get_jobs(self):
        return self.jobs

    def add_job(self, fn, trigger, **kw):
        self.added.append(kw)


@pytest.fixture
def retry(maintenance, monkeypatch):
    """Retry-job stubs on top of `maintenance`: Kaggle reports calls['latest']; a
    successful ETL writes that version into kaggle_version like run_etl does.
    The implicit 10:00 open-pull window is off so results don't depend on the clock."""
    maintenance.update(latest=520, checks=0)
    monkeypatch.setattr(line_logger, "OPEN_PULL_ENABLED", False)

    def latest():
        maintenance["checks"] += 1
        return maintenance["latest"]

    monkeypatch.setattr(database, "get_latest_kaggle_version", latest)

    def load():
        if line_logger._stored_kaggle_version() == maintenance["latest"]:
            return False
        conn = sqlite3.connect(database.DB_PATH)
        conn.execute("UPDATE kaggle_version SET version = ?", (maintenance["latest"],))
        conn.commit()
        conn.close()
        return True

    maintenance["set_etl"](load)
    return maintenance


def _today():
    # Day of the next check, so "next check falls past today" never trips near midnight.
    return (dt.datetime.now(ET) + dt.timedelta(minutes=line_logger.KAGGLE_RETRY_MINUTES)).date().isoformat()


def test_maintenance_up_to_date_schedules_hourly_retry(retry):
    sched = _FakeScheduler()
    out = line_logger.run_morning_maintenance(scheduler=sched)
    assert out["etl"] == "up to date" and out["kaggle_retry"].startswith("check 1 at")
    [job] = sched.added
    assert job["id"] == "kaggle_version_retry" and job["kwargs"]["baseline"] == 520


def test_retry_no_new_version_reschedules_without_etl(retry):
    sched = _FakeScheduler()
    assert line_logger.run_kaggle_version_retry(sched, "/b.db", 520, _today(), attempt=2) == "up to date"
    assert retry["etl"] == 0 and retry["checks"] == 1
    [job] = sched.added
    assert job["kwargs"]["attempt"] == 3


def test_retry_new_version_runs_etl_and_resolve_then_stops(retry):
    sched = _FakeScheduler()
    retry["latest"] = 521
    assert line_logger.run_kaggle_version_retry(sched, "/b.db", 520, _today(), attempt=3) == "loaded"
    assert retry["etl"] == 1 and retry["resolve"] == 1
    assert sched.added == []  # no further checks once loaded


def test_retry_tipoff_guard_skips_check_and_keeps_retrying(retry, db):
    soon = dt.datetime.now(ET) + dt.timedelta(minutes=45)
    _add_games(db, (22600085, soon.strftime("%Y-%m-%d %H:%M:%S")))
    retry["latest"] = 521
    sched = _FakeScheduler()
    assert line_logger.run_kaggle_version_retry(sched, "/b.db", 520, _today(), attempt=2) == "skipped: guard"
    assert retry["checks"] == 0 and retry["etl"] == 0
    [job] = sched.added                      # not stopped for the day: next check scheduled
    assert job["id"] == "kaggle_version_retry" and job["kwargs"]["attempt"] == 3


def test_retry_pretip_guard_skips_check_and_keeps_retrying(retry):
    job = types.SimpleNamespace(id="pretip:abc", next_run_time=dt.datetime.now(dt.timezone.utc)
                                + dt.timedelta(minutes=30))
    retry["latest"] = 521
    sched = _FakeScheduler([job])
    assert line_logger.run_kaggle_version_retry(sched, "/b.db", 520, _today()) == "skipped: guard"
    assert retry["checks"] == 0 and retry["etl"] == 0
    assert [j["id"] for j in sched.added] == ["kaggle_version_retry"]


def test_retry_after_closed_window_loads_new_version(retry, db):
    """Guard closed at check 1, open at check 2: the new version is loaded at check 2."""
    retry["latest"] = 521
    sched = _FakeScheduler()
    soon = dt.datetime.now(ET) + dt.timedelta(minutes=45)
    _add_games(db, (22600085, soon.strftime("%Y-%m-%d %H:%M:%S")))
    assert line_logger.run_kaggle_version_retry(sched, "/b.db", 520, _today()) == "skipped: guard"
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM schedule")              # that game has tipped and gone
    conn.commit()
    conn.close()
    assert line_logger.run_kaggle_version_retry(sched, "/b.db", 520, _today(), attempt=2) == "loaded"
    assert retry["etl"] == 1 and retry["resolve"] == 1


def test_retry_no_duplicate_etl_once_loaded(retry):
    retry["latest"] = 521
    assert line_logger.run_kaggle_version_retry(_FakeScheduler(), "/b.db", 520, _today()) == "loaded"
    # A stray later check (e.g. a job that was already queued) must not ETL again.
    sched = _FakeScheduler()
    assert line_logger.run_kaggle_version_retry(sched, "/b.db", 520, _today(), attempt=2) == "stopped: already loaded"
    assert retry["etl"] == 1 and retry["checks"] == 1 and sched.added == []


def test_retry_etl_failure_restores_and_stops(retry):
    retry["latest"] = 521

    def broken():
        raise RuntimeError("partial load")

    retry["set_etl"](broken)
    sched = _FakeScheduler()
    assert line_logger.run_kaggle_version_retry(sched, "/b.db", 520, _today()) == "etl failed"
    assert retry["restore"] == 1 and retry["resolve"] == 0 and sched.added == []


def test_maintenance_404_starts_hourly_retry(retry):
    def unavailable():
        raise database.KaggleVersionUnavailable("v521: PlayerStatisticsExtended.csv returned 404")

    retry["set_etl"](unavailable)
    sched = _FakeScheduler()
    out = line_logger.run_morning_maintenance(scheduler=sched)
    assert out["kaggle_retry"].startswith("check 1 at")
    [job] = sched.added
    assert job["id"] == "kaggle_version_retry" and job["kwargs"]["baseline"] == 520


# --- pull windows -----------------------------------------------------------------

def _job(id_, when):
    return types.SimpleNamespace(id=id_, next_run_time=when)


def test_clear_of_pulls_open_pull_window(monkeypatch):
    monkeypatch.setattr(line_logger, "OPEN_PULL_ENABLED", True)
    monkeypatch.setattr(line_logger, "OPEN_PULL_HOUR_ET", 10)
    day = dt.datetime(2026, 10, 23, tzinfo=ET)
    at = lambda h, m: day.replace(hour=h, minute=m)
    # The open pull counts even after it fired (cron next_run_time is tomorrow by then).
    sched = _FakeScheduler([_job("line_open_pull", at(10, 0) + dt.timedelta(days=1))])
    assert line_logger._clear_of_pulls(sched, at(9, 44)) == at(9, 44)
    assert line_logger._clear_of_pulls(sched, at(9, 45)) == at(10, 15)
    assert line_logger._clear_of_pulls(sched, at(10, 14)) == at(10, 15)
    assert line_logger._clear_of_pulls(sched, at(10, 15)) == at(10, 15)


def test_clear_of_pulls_chains_overlapping_windows(monkeypatch):
    monkeypatch.setattr(line_logger, "OPEN_PULL_ENABLED", True)
    monkeypatch.setattr(line_logger, "OPEN_PULL_HOUR_ET", 10)
    day = dt.datetime(2026, 10, 23, tzinfo=ET)
    at = lambda h, m: day.replace(hour=h, minute=m)
    sched = _FakeScheduler([_job("pretip:a", at(10, 20)), _job("pretip-retry:b", at(10, 40)),
                            _job("line_pretip_scheduler", at(10, 5))])  # not a pull
    assert line_logger._clear_of_pulls(sched, at(9, 50)) == at(10, 55)


def test_retry_check_inside_pull_window_is_deferred_not_dropped(retry):
    pull = dt.datetime.now(ET) + dt.timedelta(minutes=5)
    sched = _FakeScheduler([_job("line_open_pull", pull)])
    retry["latest"] = 521
    assert line_logger.run_kaggle_version_retry(sched, "/b.db", 520, _today(), attempt=2) == "deferred: pull window"
    assert retry["checks"] == 0 and retry["etl"] == 0
    [job] = sched.added
    assert job["run_date"] == pull + dt.timedelta(minutes=line_logger.PULL_WINDOW_MINUTES)
    assert job["kwargs"]["attempt"] == 2


def test_next_check_pushed_past_pull_window(retry):
    pull = dt.datetime.now(ET) + dt.timedelta(minutes=line_logger.KAGGLE_RETRY_MINUTES + 10)
    sched = _FakeScheduler([_job("line_open_pull", pull)])
    line_logger._schedule_kaggle_retry(sched, "/b.db", 520, pull.date().isoformat(), attempt=2)
    [job] = sched.added
    assert job["run_date"] == pull + dt.timedelta(minutes=line_logger.PULL_WINDOW_MINUTES)


# --- startup resume -----------------------------------------------------------------

@pytest.fixture
def todays_backup(retry, db, tmp_path, monkeypatch):
    """A copy of the DB (kaggle_version 520) as today's maintenance backup."""
    monkeypatch.setattr(line_logger, "BACKUP_DIR", str(tmp_path / "backups"))
    os.makedirs(line_logger.BACKUP_DIR)
    path = os.path.join(line_logger.BACKUP_DIR,
                        f"predictions_{dt.datetime.now(ET).date().isoformat()}.db")
    shutil.copy(db, path)
    return path


def test_startup_resume_loads_newer_ready_version(retry, todays_backup):
    retry["latest"] = 521
    assert line_logger.resume_kaggle_retry(_FakeScheduler()) == "loaded"
    assert retry["etl"] == 1 and retry["resolve"] == 1


def test_startup_resume_respects_guards(retry, todays_backup, db):
    _add_games(db, (22600085, (dt.datetime.now(ET) + dt.timedelta(minutes=45)).strftime("%Y-%m-%d %H:%M:%S")))
    retry["latest"] = 521
    assert line_logger.resume_kaggle_retry(_FakeScheduler()) == "skipped: guard"
    assert retry["checks"] == 0 and retry["etl"] == 0


def test_startup_resume_skips_if_loaded_since_maintenance(retry, todays_backup, db):
    conn = sqlite3.connect(db)
    conn.execute("UPDATE kaggle_version SET version = 521")
    conn.commit()
    conn.close()
    retry["latest"] = 522
    sched = _FakeScheduler()
    assert line_logger.resume_kaggle_retry(sched) == "already loaded today"
    assert retry["checks"] == 0 and retry["etl"] == 0 and sched.added == []


def test_startup_resume_without_todays_maintenance(retry, tmp_path, monkeypatch):
    monkeypatch.setattr(line_logger, "BACKUP_DIR", str(tmp_path / "none"))
    assert line_logger.resume_kaggle_retry(_FakeScheduler()) == "no maintenance today"
    assert retry["checks"] == 0


# --- failed retry ETL restores only the ETL tables ----------------------------------

NON_ETL_TABLES = ("line_snapshots", "odds_pull_log", "paper_predictions", "predictions",
                  "news_events", "news_items", "news_poll_log", "nba_report_index")


def _rows(path, tables):
    conn = sqlite3.connect(path)
    try:
        return {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() for t in tables}
    finally:
        conn.close()


def test_failed_retry_etl_restores_only_etl_tables(db, tmp_path, monkeypatch):
    conn = sqlite3.connect(db)
    for t in NON_ETL_TABLES:
        conn.execute(f"CREATE TABLE {t} (note TEXT)")
        conn.execute(f"INSERT INTO {t} VALUES ('before 07:00')")
    conn.execute("CREATE INDEX idx_ps_person ON player_stats (personId)")
    conn.commit()
    conn.close()

    # 07:00 maintenance backup (the real one).
    monkeypatch.setattr(line_logger, "BACKUP_DIR", str(tmp_path / "backups"))
    backup = line_logger.backup_database()
    etl_at_7 = _rows(db, line_logger.ETL_TABLES)

    # After 07:00: pulls, paper predictions, predictions and news keep writing.
    conn = sqlite3.connect(db)
    for t in NON_ETL_TABLES:
        conn.execute(f"INSERT INTO {t} VALUES ('after 07:00')")
    conn.commit()
    conn.close()
    after_7 = _rows(db, NON_ETL_TABLES)

    def half_loaded_etl():
        c = sqlite3.connect(database.DB_PATH)
        c.execute("DROP TABLE player_stats")
        c.execute("CREATE TABLE player_stats (personId INTEGER, gameDateTimeEst TEXT, junk TEXT)")
        c.execute("INSERT INTO player_stats VALUES (1, '2026-10-22 19:30:00', 'partial')")
        c.execute("DELETE FROM players")
        c.execute("INSERT INTO schedule VALUES (22600099, '2026-10-24 19:30:00')")
        c.execute("UPDATE kaggle_version SET version = 521")
        c.commit()
        c.close()
        raise RuntimeError("partial load")

    monkeypatch.setattr(line_logger, "OPEN_PULL_ENABLED", False)
    monkeypatch.setattr(database, "get_latest_kaggle_version", lambda: 521)
    monkeypatch.setattr(database, "run_etl", half_loaded_etl)
    out = line_logger.run_kaggle_version_retry(_FakeScheduler(), backup, 520, _today())

    assert out == "etl failed"
    assert _rows(db, line_logger.ETL_TABLES) == etl_at_7
    assert _rows(db, NON_ETL_TABLES) == after_7       # post-07:00 rows kept, nothing rolled back
    conn = sqlite3.connect(db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(player_stats)")]
    idx = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")]
    conn.close()
    assert cols == ["personId", "gameDateTimeEst"] and "idx_ps_person" in idx
