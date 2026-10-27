"""
Test-wide settings, applied before any test module imports main:
  - SPORTSBETTR_MODE=dev and WNBA_ENABLED=0, whatever backend/.env says (load_dotenv never
    overrides a variable that is already set);
  - "testserver" (Starlette's TestClient host) added to ALLOWED_HOSTS;
  - no network for the whole process (see _no_network below);
  - every TestClient request is signed in (the auth dependency is overridden), except in
    tests marked `real_auth`, which exercise the login itself (tests/test_auth.py).
"""
import os
import socket
import sys

import pytest

os.environ["SPORTSBETTR_MODE"] = "dev"
os.environ["WNBA_ENABLED"] = "0"
os.environ["ALLOWED_HOSTS"] = "bettr.rodneyhong.com,localhost,127.0.0.1,testserver"
os.environ.pop("API_DOCS_ENABLED", None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def pytest_configure(config):
    config.addinivalue_line("markers", "real_auth: run with the real login check (no signed-in override)")
    config.addinivalue_line("markers", "live_db_readonly: may open the real predictions.db, read-only "
                                       "(file:...?mode=ro) connections only")
    config.addinivalue_line("markers", "slip_parity: slow /slip before/after check on a copy of predictions.db; "
                                       "skipped unless RUN_SLIP_PARITY=1")
    config.addinivalue_line("markers", "live_network: may connect out, and only when RUN_LIVE_EXTRACTOR=1")


def pytest_collection_modifyitems(config, items):
    if os.getenv("RUN_SLIP_PARITY") == "1":
        return
    skip = pytest.mark.skip(reason="set RUN_SLIP_PARITY=1 to run the slow /slip parity check (~6 min)")
    for item in items:
        if item.get_closest_marker("slip_parity"):
            item.add_marker(skip)


_LOOPBACK = {"127.0.0.1", "::1", "localhost"}

# No test reaches OddsAPI, Kaggle, ESPN, NBA, Ollama Cloud or Gemini. The block is installed
# here, at import, for the whole pytest process and every thread in it: a background thread that
# outlives its test (a scheduler job, a daemon thread) is blocked too, between tests and after the
# last one. Every attempt is recorded; the test it happens in fails even when the code under test
# caught the OSError, and an attempt outside any test fails the session. A call from a thread
# leaked by an earlier test is blamed on whichever test is running when it happens.
# Only a test marked live_network, with RUN_LIVE_EXTRACTOR=1, may connect out.
_REAL_CONNECT, _REAL_CONNECT_EX, _REAL_GAI = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo
_net = {"test": None, "allow": False, "hits": []}      # hits: (test nodeid or None, target)


def _blocked(target) -> bool:
    if _net["allow"]:
        return False
    _net["hits"].append((_net["test"], str(target)))
    return True


def _remote(address) -> bool:
    return isinstance(address, tuple) and address[0] not in _LOOPBACK


def _connect(self, address, *a, **k):
    if _remote(address) and _blocked(address):
        raise OSError(f"network blocked in tests: {address}")
    return _REAL_CONNECT(self, address, *a, **k)


def _connect_ex(self, address, *a, **k):
    if _remote(address) and _blocked(address):
        raise OSError(f"network blocked in tests: {address}")
    return _REAL_CONNECT_EX(self, address, *a, **k)


def _getaddrinfo(host, *a, **k):
    if host is not None and host not in _LOOPBACK and _blocked(host):
        raise OSError(f"DNS blocked in tests: {host}")
    return _REAL_GAI(host, *a, **k)


socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = _connect, _connect_ex, _getaddrinfo


@pytest.fixture(autouse=True)
def _no_network(request):
    _net["test"] = request.node.nodeid
    _net["allow"] = (request.node.get_closest_marker("live_network") is not None
                     and os.getenv("RUN_LIVE_EXTRACTOR") == "1")
    start = len(_net["hits"])
    yield
    _net["allow"], _net["test"] = False, None
    mine = _net["hits"][start:]
    if mine:
        pytest.fail(f"made {len(mine)} blocked network call(s): {sorted({t for _, t in mine})}")


def pytest_sessionfinish(session, exitstatus):
    stray = [t for test, t in _net["hits"] if test is None]
    if stray:
        print(f"\nnetwork calls outside any test (a leaked background thread?): {sorted(set(stray))}")
        session.exitstatus = 1


_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LIVE_DBS = {os.path.normcase(os.path.realpath(os.path.join(_BACKEND, n))) for n in ("predictions.db", "historical.db")}


def _db_file(database) -> str:
    """The file a sqlite3.connect target names ('file:...?mode=ro' URIs included)."""
    path = os.fspath(database)
    if path.startswith("file:"):
        path = path[5:].split("?", 1)[0]
    return os.path.normcase(os.path.realpath(path))


@pytest.fixture(autouse=True)
def _no_live_database(request, monkeypatch):
    """No test opens the real predictions.db or historical.db. Modules that did
    `from database import DB_PATH` at import keep the real path unless a test repoints them,
    so a forgotten module would otherwise read (or write) the live database. A test marked
    live_db_readonly may open it read-only (a 'file:...?mode=ro' URI) and nothing else."""
    import sqlite3
    real = sqlite3.connect
    opened = []
    readonly_ok = request.node.get_closest_marker("live_db_readonly") is not None

    def connect(database, *a, **k):
        target = os.fspath(database)
        if readonly_ok and target.startswith("file:") and "mode=ro" in target.split("?", 1)[-1] and k.get("uri"):
            return real(database, *a, **k)
        if _db_file(database) in _LIVE_DBS:
            opened.append(str(database))
            raise AssertionError(f"test opened the live database {database}; repoint the module's DB_PATH")
        return real(database, *a, **k)

    monkeypatch.setattr(sqlite3, "connect", connect)
    yield
    if opened:        # also when the code under test caught the AssertionError
        pytest.fail(f"opened the live database {len(opened)} time(s): {sorted(set(opened))}")


@pytest.fixture(autouse=True)
def _signed_in(request):
    if request.node.get_closest_marker("real_auth"):
        yield
        return
    import auth
    import main
    main.app.dependency_overrides[auth.require_session] = lambda: None
    yield
    main.app.dependency_overrides.pop(auth.require_session, None)
