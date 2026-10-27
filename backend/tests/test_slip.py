"""
/slip on the server (open item from migration plan v2): one slip at a time (a second request
while one is being built gets 429), none while a pre-tip pull waits or runs (503), models
loaded once per process, and the same slip as before the change on a fixed copy of the
database (experiments/slip_parity.py has the old code). Mocked: no network, no models read
except in the parity test, which reads a backup-API copy of predictions.db.

The parity test is marked slip_parity and skipped by default (about 6 minutes: four full
slips). Run it from backend/ with
  $env:RUN_SLIP_PARITY = "1"; .\.venv\Scripts\python.exe -m pytest tests/test_slip.py -m slip_parity
(bash: RUN_SLIP_PARITY=1 python -m pytest tests/test_slip.py -m slip_parity).
"""
import os
import sqlite3
import sys
import threading

import pytest
from fastapi.testclient import TestClient

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
sys.path.insert(0, os.path.join(BACKEND, "experiments"))

import data_fetcher   # noqa: E402
import line_logger    # noqa: E402
import main           # noqa: E402
import predictor      # noqa: E402

SLIP = {"picks": [], "parlay_confidence": 0.0}


@pytest.fixture
def no_secondary(monkeypatch):
    started = []
    monkeypatch.setattr(main, "_start_secondary_logging", lambda lines: started.append(lines))
    monkeypatch.setattr(line_logger, "_running_pretips", set())
    return started


def test_two_concurrent_slips_give_exactly_one_429(monkeypatch, no_secondary):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def slow_build(size):
        calls.append(size)
        entered.set()
        release.wait(10)
        return SLIP, {}

    monkeypatch.setattr(main, "_build_slip", slow_build)
    c = TestClient(main.app)
    codes = []
    first = threading.Thread(target=lambda: codes.append(c.get("/slip?size=2").status_code))
    first.start()
    assert entered.wait(10)
    r = c.get("/slip?size=3")                              # while the first is still building
    release.set()
    first.join(10)
    assert r.status_code == 429
    assert r.json()["detail"] == main.SLIP_BUSY_DETAIL
    assert codes == [200] and calls == [2]                 # the refused request built nothing
    assert c.get("/slip").status_code == 200               # the lock is free again afterwards


def test_lock_released_when_the_build_fails(monkeypatch, no_secondary):
    def boom(size):
        raise RuntimeError("build failed")

    monkeypatch.setattr(main, "_build_slip", boom)
    c = TestClient(main.app, raise_server_exceptions=False)
    assert c.get("/slip").status_code == 500
    assert main._SLIP_LOCK.acquire(blocking=False)
    main._SLIP_LOCK.release()


def test_refused_while_a_pretip_pull_is_in_progress(monkeypatch, no_secondary):
    monkeypatch.setattr(main, "_build_slip", lambda size: pytest.fail("slip built during a pre-tip pull"))
    monkeypatch.setattr(line_logger, "_running_pretips", {"evt1"})
    r = TestClient(main.app).get("/slip")
    assert r.status_code == 503
    assert r.json()["detail"] == main.SLIP_PRETIP_DETAIL
    assert r.headers["retry-after"] == "300"
    assert no_secondary == []


def test_secondary_logging_runs_once_at_a_time(monkeypatch):
    """A slip's background secondary-stat logging is not started again while one is running."""
    gate = threading.Event()
    monkeypatch.setattr(line_logger, "_running_pretips", set())
    monkeypatch.setattr(data_fetcher, "get_player_context", lambda name: gate.wait(10) and None)
    main._start_secondary_logging({"A": {}})
    assert not main._SECONDARY_LOCK.acquire(blocking=False)       # held by the running thread
    main._start_secondary_logging({"A": {}})                        # skipped, not queued
    gate.set()
    for _ in range(100):
        if main._SECONDARY_LOCK.acquire(timeout=0.1):
            main._SECONDARY_LOCK.release()
            break
    else:
        pytest.fail("secondary logging never released its lock")


class _FakeModel:
    def predict_proba(self, X):
        return [[0.2, 0.8]]


def _fake_model_dir(tmp_path, monkeypatch):
    """Empty model_<stat>.pkl files in a temp dir; joblib.load is stubbed, the real files untouched."""
    for stat in predictor._ML_STAT_TYPES:
        (tmp_path / f"model_{stat}.pkl").write_bytes(b"")
    monkeypatch.setattr(predictor, "_MODEL_DIR", str(tmp_path))


def test_models_load_once_across_requests(monkeypatch, tmp_path, no_secondary):
    loads = []
    monkeypatch.setattr(predictor, "_MODELS", {})
    monkeypatch.setattr(predictor, "_MODELS_LOADED", False)
    _fake_model_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(predictor.joblib, "load", lambda p: loads.append(p) or _FakeModel())
    monkeypatch.setattr(predictor, "compute_features", lambda *a, **k: {"games_played": 20})
    monkeypatch.setattr(predictor, "_compute_ml_features",
                        lambda *a: {**{c: 0.0 for c in predictor.FEATURE_COLS}, "opp_game_count": 0,
                                    "line_hits_last10": 0, "games_last10": 10})
    monkeypatch.setattr(predictor, "build_explanation", lambda *a: "")
    lines = {f"Player {i}": {"player_points": {"value": 10.5, "source": "underdog"},
                             "player_rebounds": {"value": 4.5, "source": "underdog"},
                             "game_id": f"g{i}", "home_team": f"H{i}", "away_team": f"A{i}"} for i in range(4)}
    monkeypatch.setattr(data_fetcher, "get_todays_lines", lambda: lines)
    monkeypatch.setattr(data_fetcher, "get_player_context",
                        lambda name: {"next_opponent_full": "Opp", "is_home": True, "is_back_to_back": False})
    c = TestClient(main.app)
    for size in (1, 2, 3):
        r = c.get(f"/slip?size={size}")
        assert r.status_code == 200 and len(r.json()["picks"]) == size
    assert len(loads) == len(predictor._ML_STAT_TYPES)               # one load per model file, ever


def test_concurrent_first_loads_read_each_model_once(monkeypatch, tmp_path):
    loads = []
    monkeypatch.setattr(predictor, "_MODELS", {})
    monkeypatch.setattr(predictor, "_MODELS_LOADED", False)
    _fake_model_dir(tmp_path, monkeypatch)

    def slow_load(p):
        loads.append(p)
        threading.Event().wait(0.05)
        return _FakeModel()

    monkeypatch.setattr(predictor.joblib, "load", slow_load)
    threads = [threading.Thread(target=predictor._load_models) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(loads) == len(predictor._ML_STAT_TYPES)


# --- same slip as before the change, on a fixed copy of the real database -------------------

_LIVE = os.path.join(BACKEND, "predictions.db")


@pytest.fixture
def db_copy(tmp_path):
    if not os.path.exists(_LIVE):
        pytest.skip("no predictions.db on this machine")
    import slip_parity
    path = str(tmp_path / "copy.db")
    src = sqlite3.connect(f"file:{_LIVE}?mode=ro", uri=True)
    dst = sqlite3.connect(path)
    src.backup(dst)
    dst.close()
    src.close()
    return path, slip_parity.latest_lines_date(path)


@pytest.mark.slip_parity
@pytest.mark.live_db_readonly
def test_same_slip_as_before_on_a_fixed_copy(db_copy, monkeypatch):
    import database
    import rosters
    import slip_parity
    path, date = db_copy
    for mod in (database, data_fetcher, line_logger, predictor, rosters):
        monkeypatch.setattr(mod, "DB_PATH", path)
    lines = line_logger.todays_underdog_lines(date)
    assert lines, f"no Underdog lines on {date} in the copy"
    monkeypatch.setattr(data_fetcher, "get_todays_lines", lambda: lines)
    for size in (2, 5):
        old = slip_parity.build_slip_before(size)
        new, _ = main._build_slip(size)
        assert new == old, size
        assert old["picks"], f"no picks on {date}: the comparison would prove nothing"
