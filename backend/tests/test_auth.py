"""
Admin login (auth.py, migration plan v2 step 0.3) and the API's outer layer (step 0.4):
every route needs the session except /auth/login and /health (a walk over app.routes);
cookie attributes; logout; 5 failed logins per 15 min per IP; no /docs, /redoc or
/openapi.json unless API_DOCS_ENABLED=1; CORS only in dev; unknown Host refused.
Password hashes are made here; nothing reads backend/.env or the network.
"""
import os
import subprocess
import sys

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

import auth   # noqa: E402
import main   # noqa: E402

PASSWORD = "correct horse battery staple"
pytestmark = pytest.mark.real_auth


@pytest.fixture(scope="module")
def pw_hash():
    return auth.hash_password(PASSWORD)


@pytest.fixture
def configured(monkeypatch, pw_hash):
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", pw_hash)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-" + "x" * 40)
    auth.reset_failures()
    yield
    auth.reset_failures()


def client():
    return TestClient(main.app, base_url="https://testserver")   # https: the cookie is Secure


def _concrete(path: str) -> str:
    """Fill path parameters ({player_name}, {pred_id}, ...) with a harmless value."""
    import re
    return re.sub(r"\{[^}]+\}", "1", path)


def test_every_route_but_login_and_health_needs_the_session(configured):
    c = client()
    checked = []
    for route in main.app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods:
            r = c.request(method, _concrete(route.path))
            if route.path in ("/auth/login", "/health"):
                assert r.status_code != 401, (method, route.path)
            else:
                assert r.status_code == 401, (method, route.path, r.status_code)
            checked.append((method, route.path))
    paths = {p for _, p in checked}
    for must in ("/predictions/duplicates", "/slip", "/sync", "/screenshot/extract", "/screenshot/alias",
                 "/predict", "/wnba/lines/{player_name}/{stat_type}", "/auth/logout", "/auth/me"):
        assert must in paths
    assert ("DELETE", "/predictions/duplicates") in checked
    assert len(checked) >= 25


def test_only_api_routes_exist_no_docs(configured):
    """Every route is an APIRoute (so the walk above covers it); docs are off by default."""
    assert all(isinstance(r, APIRoute) for r in main.app.routes
               if getattr(r, "path", "") not in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"))
    c = client()
    for p in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(p).status_code == 404


def test_health_is_minimal(configured):
    assert client().get("/health").json() == {"status": "ok"}


def test_login_sets_a_30_day_strict_cookie_and_unlocks_routes(configured):
    c = client()
    assert c.get("/auth/me").status_code == 401
    r = c.post("/auth/login", json={"password": PASSWORD})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    for attr in ("HttpOnly", "Secure", "SameSite=strict", f"Max-Age={30 * 86400}", "Path=/"):
        assert attr.lower() in cookie.lower(), cookie
    assert c.get("/auth/me").status_code == 200
    assert c.get("/sync").status_code == 200                # a protected route that reads no database
    assert c.post("/auth/logout").status_code == 200
    assert c.get("/auth/me").status_code == 401


def test_wrong_password_and_tampered_or_expired_tokens(configured):
    c = client()
    assert c.post("/auth/login", json={"password": "nope"}).status_code == 401
    tok = auth.new_token()
    assert auth.token_valid(tok)
    exp, nonce, mac = tok.split(".")
    assert not auth.token_valid(f"{int(exp) + 999}.{nonce}.{mac}")          # edited expiry
    assert not auth.token_valid(tok, now=int(exp) + 1)                        # expired
    assert not auth.token_valid("garbage") and not auth.token_valid(None)


def test_new_password_logs_every_device_out(configured, monkeypatch):
    tok = auth.new_token()
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", auth.hash_password("another password 123"))
    assert not auth.token_valid(tok)


def test_five_failures_per_ip_lock_login_for_15_minutes(configured):
    c = client()
    for _ in range(auth.MAX_FAILURES):
        assert c.post("/auth/login", json={"password": "wrong"}).status_code == 401
    r = c.post("/auth/login", json={"password": PASSWORD})          # right password, still refused
    assert r.status_code == 429
    ip = "testclient"
    later = __import__("time").time() + auth.FAILURE_WINDOW_MINUTES * 60 + 1
    assert auth.locked_out(ip) and not auth.locked_out(ip, now=later)
    assert not auth.locked_out("203.0.113.9")                         # per IP


def test_ten_wrong_passwords_trigger_the_limit(configured):
    codes = [client().post("/auth/login", json={"password": f"w{i}"}).status_code for i in range(10)]
    assert codes[:5] == [401] * 5 and codes[5:] == [429] * 5


def test_login_not_configured_is_503(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD_HASH", raising=False)
    monkeypatch.setenv("SESSION_SECRET", "s")
    assert client().post("/auth/login", json={"password": "x"}).status_code == 503


def test_unknown_host_is_refused(configured):
    r = TestClient(main.app, base_url="https://evil.example").get("/health")
    assert r.status_code == 400


def test_write_env_replaces_only_its_keys(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ODDS_API_KEY=abc\nADMIN_PASSWORD_HASH=old\n# comment\n")
    auth._write_env({"ADMIN_PASSWORD_HASH": "$argon2id$v=19$m=1$x$y", "SESSION_SECRET": "s3"}, str(env))
    lines = env.read_text().splitlines()
    assert lines == ["ODDS_API_KEY=abc", "ADMIN_PASSWORD_HASH='$argon2id$v=19$m=1$x$y'", "# comment",
                     "SESSION_SECRET='s3'"]
    from dotenv import dotenv_values
    assert dotenv_values(env)["ADMIN_PASSWORD_HASH"] == "$argon2id$v=19$m=1$x$y"   # '$' kept literally


PROBE = """
import json, sys
sys.path.insert(0, sys.argv[1])
import main
from fastapi.middleware.cors import CORSMiddleware
print(json.dumps({"cors": any(m.cls is CORSMiddleware for m in main.app.user_middleware),
                  "docs": main.app.docs_url, "openapi": main.app.openapi_url}))
"""


@pytest.mark.parametrize("mode, docs, cors", [("prod", None, False), ("dry", None, False), ("dev", None, True),
                                              ("dev+docs", "/docs", True)])
def test_cors_only_in_dev_and_docs_only_when_enabled(mode, docs, cors):
    import json
    env = {**os.environ, "SPORTSBETTR_MODE": mode.split("+")[0]}
    env["API_DOCS_ENABLED"] = "1" if "+docs" in mode else "0"
    out = subprocess.run([sys.executable, "-c", PROBE, BACKEND], env=env, capture_output=True, text=True,
                         timeout=120, cwd=BACKEND)
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got == {"cors": cors, "docs": docs, "openapi": "/openapi.json" if docs else None}, out.stderr
