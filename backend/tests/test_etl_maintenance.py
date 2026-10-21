"""
Kaggle 404 path and tip-off guard for the ETL / morning maintenance.
Every test runs against a temp DB with Kaggle, backups, rosters and resolvers stubbed:
nothing touches predictions.db or the network.
"""
import datetime as dt
import os
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
