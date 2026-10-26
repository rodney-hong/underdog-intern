"""
Test-wide settings, applied before any test module imports main:
  - SPORTSBETTR_MODE=dev and WNBA_ENABLED=0, whatever backend/.env says (load_dotenv never
    overrides a variable that is already set);
  - "testserver" (Starlette's TestClient host) added to ALLOWED_HOSTS;
  - every TestClient request is signed in (the auth dependency is overridden), except in
    tests marked `real_auth`, which exercise the login itself (tests/test_auth.py).
"""
import os
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


_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """No test reaches OddsAPI, Kaggle, ESPN, NBA, Ollama Cloud or Gemini: any connection or DNS
    lookup that is not loopback raises. (RUN_LIVE_EXTRACTOR=1 lifts it for that opt-in test.)"""
    if os.getenv("RUN_LIVE_EXTRACTOR") == "1":
        yield
        return
    import socket
    real_connect, real_gai = socket.socket.connect, socket.getaddrinfo

    def connect(self, address, *a, **k):
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(address, tuple) and host not in _LOOPBACK:
            raise OSError(f"network blocked in tests: {address}")
        return real_connect(self, address, *a, **k)

    def getaddrinfo(host, *a, **k):
        if host not in _LOOPBACK and host is not None:
            raise OSError(f"DNS blocked in tests: {host}")
        return real_gai(host, *a, **k)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    yield


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
