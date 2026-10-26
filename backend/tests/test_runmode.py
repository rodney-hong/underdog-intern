"""
SPORTSBETTR_MODE (migration plan v2, step 0.1) and WNBA_ENABLED (step 0.11).
The real startup hook runs in dev and dry on a temp database with every socket blocked:
it must add or change no row in any table and attempt no network connection; dry must print
today's plan with ET times. In prod (steps recorded, nothing run) the collecting steps run,
and with WNBA off no WNBA load or training. The schedulers refuse to start outside prod.
"""
import datetime as dt
import hashlib
import os
import socket
import sqlite3
import sys

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import database      # noqa: E402
import dryrun        # noqa: E402
import line_logger   # noqa: E402
import news_ingest   # noqa: E402
import proc_lock     # noqa: E402
import runmode       # noqa: E402
from test_challenger import TEAMS, _db   # noqa: E402

ET = line_logger.EASTERN


def _snapshot(path) -> dict:
    """{table: sha256 of every row, in rowid order} for every table."""
    conn = sqlite3.connect(path)
    try:
        out = {}
        for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"):
            h = hashlib.sha256()
            for row in conn.execute(f'SELECT * FROM "{t}" ORDER BY rowid'):
                h.update(repr(row).encode())
            out[t] = h.hexdigest()
        return out
    finally:
        conn.close()


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Temp DB (synthetic box scores, every app table) with two games later today; sockets blocked."""
    path = str(tmp_path / "t.db")
    _db(path)
    for mod in (database, line_logger, news_ingest):
        monkeypatch.setattr(mod, "DB_PATH", path)
    database.init_db()
    line_logger.init_tables()
    conn = sqlite3.connect(path)
    now = dt.datetime.now(ET)
    tips = [(now + dt.timedelta(hours=h)).replace(second=0, microsecond=0, tzinfo=None) for h in (3, 5)]
    for i, tip in enumerate(tips):
        conn.execute("INSERT INTO schedule (gameId, gameDateTimeEst, homeTeamId, awayTeamId) VALUES (?, ?, ?, ?)",
                     (22600100 + i, tip.strftime("%Y-%m-%d %H:%M:%S"), TEAMS[0], TEAMS[1]))
    conn.commit()
    conn.close()
    attempts = []

    def blocked(self, address, *a, **k):
        attempts.append(address)
        raise OSError(f"network blocked in test: {address}")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(OSError("dns blocked")))
    yield {"path": path, "attempts": attempts, "tips": tips}
    dryrun.stop()
    line_logger.stop_scheduler()
    news_ingest.stop_scheduler()
    if proc_lock.INSTANCE_LOCK.held:
        proc_lock.INSTANCE_LOCK.release()


def _start(monkeypatch, mode):
    import main
    monkeypatch.setenv("SPORTSBETTR_MODE", mode)
    main.startup_event()


@pytest.mark.parametrize("mode", ["dev", "dry"])
def test_dev_and_dry_add_no_rows_and_make_no_network_calls(world, monkeypatch, capsys, mode):
    # anything collecting must not even be called
    for mod, fn in ((line_logger, "run_sync"), (line_logger, "run_catchup_pulls"), (line_logger, "fetch_events"),
                    (line_logger, "start_sync_in_background"), (database, "run_wnba_etl"),
                    (database, "run_etl"), (database, "refresh_current"), (database, "backfill_limited_data")):
        monkeypatch.setattr(mod, fn, lambda *a, _n=fn, **k: pytest.fail(f"{_n} called in {mode}"))
    before = _snapshot(world["path"])
    _start(monkeypatch, mode)
    out = capsys.readouterr().out
    assert _snapshot(world["path"]) == before
    assert world["attempts"] == []
    assert line_logger._scheduler is None and news_ingest._scheduler is None
    assert f"SPORTSBETTR_MODE = {mode.upper()}" in out
    if mode == "dry":
        assert dryrun._scheduler is not None
        assert "[dry] plan for" in out
        today = dt.datetime.now(ET).date()
        for tip in [t for t in world["tips"] if t.date() == today]:     # late evening: tomorrow's
            pretip = tip - dt.timedelta(minutes=line_logger.PRETIP_MINUTES)
            assert f"{pretip:%H:%M} ET  pre-tip pull ({line_logger.CREDITS_PER_PULL} credits)" in out
        assert "startup sync:" in out and "Kaggle version retry" in out
    else:
        assert dryrun._scheduler is None and "[dry]" not in out


def test_unknown_mode_stops_startup(monkeypatch):
    import main
    monkeypatch.setenv("SPORTSBETTR_MODE", "production")
    with pytest.raises(runmode.BadMode):
        main.startup_event()
    assert not proc_lock.INSTANCE_LOCK.held


@pytest.mark.parametrize("mode", ["dev", "dry"])
def test_schedulers_refuse_outside_prod(world, monkeypatch, mode):
    monkeypatch.setenv("SPORTSBETTR_MODE", mode)
    assert line_logger.start_scheduler() is None and line_logger._scheduler is None
    assert news_ingest.start_scheduler() is None and news_ingest._scheduler is None


def test_default_mode_is_dev(monkeypatch):
    monkeypatch.delenv("SPORTSBETTR_MODE", raising=False)
    assert runmode.mode() == "dev"


# --- prod: steps recorded, nothing run --------------------------------------------------

class _FakeTrainer:
    FROZEN_MODEL_TYPES = []
    WNBA_MODEL_PATH = "/nonexistent/model_wnba_Points.pkl"
    train_wnba_models = staticmethod(lambda: None)

    @staticmethod
    def model_path(st):
        return st


@pytest.fixture
def prod_steps(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "x.db"))
    monkeypatch.setenv("SPORTSBETTR_MODE", "prod")
    steps = []

    def record(name, fn, *a, **kw):
        steps.append(name)
        return (True, _FakeTrainer) if name == "import ml_trainer" else (True, None)

    monkeypatch.setattr(main, "_startup_step", record)
    yield main, steps
    if proc_lock.INSTANCE_LOCK.held:
        proc_lock.INSTANCE_LOCK.release()


def test_prod_runs_the_collecting_steps(prod_steps, monkeypatch):
    main, steps = prod_steps
    monkeypatch.setenv("WNBA_ENABLED", "0")
    main.startup_event()
    for s in ("database.enable_wal", "challenger.check", "rosters.refresh", "player_match.rebuild",
              "line_logger.start_scheduler", "news_ingest.start_scheduler", "database.init_db",
              "database.log_startup", "line_logger.start_sync_in_background"):
        assert s in steps
    assert "database.run_wnba_etl" not in steps and "ml_trainer.train_wnba_models" not in steps
    assert steps[-1] == "line_logger.start_sync_in_background"


def test_prod_with_wnba_on_loads_and_trains_a_missing_model(prod_steps, monkeypatch):
    main, steps = prod_steps
    monkeypatch.setenv("WNBA_ENABLED", "1")
    main.startup_event()
    assert "database.run_wnba_etl" in steps and "ml_trainer.train_wnba_models" in steps


def test_log_startup_writes_one_row(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "s.db"))
    rid = database.log_startup(mode="prod", wnba_enabled=False, challenger_ok=True)
    conn = sqlite3.connect(database.DB_PATH)
    row = conn.execute("SELECT mode, pid, wnba_enabled, challenger_ok FROM startup_log WHERE id = ?", (rid,)).fetchone()
    conn.close()
    assert row == ("prod", os.getpid(), 0, 1)


# --- WNBA routes ---------------------------------------------------------------------------

def test_wnba_routes_are_off_by_default(monkeypatch):
    from fastapi.testclient import TestClient
    import data_fetcher
    import main
    monkeypatch.setattr(data_fetcher, "get_wnba_player_line",
                        lambda *a: pytest.fail("WNBA lines fetched (spends OddsAPI credits)"))
    c = TestClient(main.app)
    for path in ("/wnba/lines/A%20B/Points", "/wnba/teams", "/wnba/players/search?q=a",
                 "/wnba/players/A/team", "/wnba/players/A/context"):
        r = c.get(path)
        assert r.status_code == 404 and "WNBA is turned off" in r.json()["detail"], path
    r = c.post("/predict", json={"player_name": "A B", "opponent_team": "Las Vegas Aces", "stat_line": 10.5,
                                 "stat_type": "Points", "league": "WNBA"})
    assert r.status_code == 404


def test_wnba_routes_work_when_enabled(monkeypatch):
    from fastapi.testclient import TestClient
    import data_fetcher
    import main
    monkeypatch.setenv("WNBA_ENABLED", "1")
    monkeypatch.setattr(data_fetcher, "get_wnba_player_line", lambda *a: (12.5, "underdog"))
    assert TestClient(main.app).get("/wnba/lines/A%20B/Points").json() == {"line": 12.5, "source": "underdog"}


# --- the dry plan ---------------------------------------------------------------------------

def test_dry_plan_mirrors_the_prod_schedule(world, monkeypatch):
    monkeypatch.setattr(line_logger, "OPEN_PULL_ENABLED", True)
    day = dt.datetime.now(ET).date()
    conn = sqlite3.connect(world["path"])
    conn.execute("DELETE FROM schedule WHERE substr(gameDateTimeEst, 1, 10) = ?", (day.isoformat(),))
    for gid, hhmm in ((22600200, "19:00"), (22600201, "21:30")):
        conn.execute("INSERT INTO schedule (gameId, gameDateTimeEst, homeTeamId, awayTeamId) VALUES (?, ?, ?, ?)",
                     (gid, f"{day} {hhmm}:00", TEAMS[0], TEAMS[1]))
    conn.commit()
    conn.close()
    now = dt.datetime.combine(day, dt.time(8, 0), ET)
    plan = [(f"{t:%H:%M}", what) for t, what in dryrun.plan(now, startup=True)]
    times = [t for t, _ in plan]
    assert ("10:00", f"open pull: 2 game(s) x {line_logger.CREDITS_PER_PULL} credits") in plan
    assert any(t == "18:30" and w.startswith("pre-tip pull") for t, w in plan)
    assert any(t == "21:00" and w.startswith("pre-tip pull") for t, w in plan)
    retries = [t for t, w in plan if w.startswith("Kaggle version retry")]
    # 09:00; 10:00 falls in the open pull's window -> 10:10; then hourly, pushed clear of
    # the 18:30 and 21:00 pre-tip windows (18:10 and 21:10 are clear: 10 min windows)
    assert retries[:3] == ["09:00", "10:10", "11:10"]
    assert "18:30" not in retries and all(not ("20:50" <= r < "21:10") for r in retries)
    assert times == sorted(times)
    assert "06:00" not in times and "07:00" not in times          # already past at 08:00
