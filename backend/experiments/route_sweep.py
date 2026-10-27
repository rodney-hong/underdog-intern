#!/usr/bin/env python
"""Live auth sweep for SportsBettr (Cloud Migration Plan v2, Phase 4 steps 1 and 6).

Run from the backend/ directory with the backend's venv:

  dump       Import the app (no server start, no lifespan) and write its routes to JSON.
             Network blocked except loopback. Run on the server, never on the collecting PC.
  sweep      Call every route with no session and no body. All must return 401 except
             /health and the login route; /docs, /redoc, /openapi.json must return 404.
  ratelimit  10 wrong passwords from fake client IP A (via X-Forwarded-For), then one wrong
             password from fake IP B. Expect A: 5 refusals then 429s; B: not locked.
             --check-password also confirms the right password is refused from locked IP A,
             accepted from fresh IP C, and prints the cookie flags (never the value).

Standard library only. Never prints passwords or cookie values.
"""
import argparse
import getpass
import json
import os
import random
import re
import secrets
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

DOC_PATHS = ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"]
PARAM = re.compile(r"\{[^}]+\}")
LOCAL = {"127.0.0.1", "::1", "localhost"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(_NoRedirect)


def call(method, url, body=None, headers=None, timeout=30):
    h = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with OPENER.open(req, timeout=timeout) as r:
            return r.status, r.headers, r.read(300)
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read(300)


def require_backend_dir():
    if not Path("main.py").is_file():
        sys.exit("Run this from the backend/ directory (main.py not found).")


def find_login(routes_file, login_path):
    if login_path:
        return login_path
    routes = json.loads(Path(routes_file).read_text())
    hits = [r["path"] for r in routes
            if r["method"] == "POST" and r["path"].rstrip("/").endswith("login")]
    if len(hits) != 1:
        sys.exit(f"Could not pick the login route from {hits}; pass --login-path.")
    return hits[0]


# ---------------------------------------------------------------- dump
def dump(args):
    require_backend_dir()
    os.environ["SPORTSBETTR_MODE"] = "dev"
    os.environ.pop("API_DOCS_ENABLED", None)

    real_gai = socket.getaddrinfo
    real_connect = socket.socket.connect

    def gai(host, *a, **k):
        if host is not None and host not in LOCAL:
            raise OSError(f"network blocked during route dump: {host}")
        return real_gai(host, *a, **k)

    def connect(self, addr):
        if isinstance(addr, tuple) and addr[0] not in LOCAL:
            raise OSError(f"network blocked during route dump: {addr[0]}")
        return real_connect(self, addr)

    socket.getaddrinfo = gai
    socket.socket.connect = connect

    sys.path.insert(0, str(Path.cwd()))
    from main import app  # noqa: E402  (import only; lifespan never runs)
    from starlette.routing import Mount, Route, WebSocketRoute

    routes = []
    for r in app.routes:
        if isinstance(r, Route):
            for m in sorted((r.methods or {"GET"}) - {"HEAD", "OPTIONS"}):
                routes.append({"method": m, "path": r.path})
        elif isinstance(r, Mount):
            routes.append({"method": "GET", "path": r.path.rstrip("/") + "/", "mount": True})
        elif isinstance(r, WebSocketRoute):
            routes.append({"method": "WS", "path": r.path})
    Path(args.out).write_text(json.dumps(routes, indent=1))
    print(f"{len(routes)} method+path pairs written to {args.out}", file=sys.stderr)


# ---------------------------------------------------------------- sweep
def sweep(args):
    base = args.base.rstrip("/")
    routes = json.loads(Path(args.routes).read_text())
    login = find_login(args.routes, args.login_path)
    public = {"/health", login}
    fails, warns = [], []

    print(f"Sweeping {len(routes)} method+path pairs at {base} with no session\n")
    for r in routes:
        m, p = r["method"], r["path"]
        if m == "WS":
            warns.append(f"WS {p}: websocket not swept; check by hand")
            continue
        status, _, body = call(m, base + PARAM.sub("1", p), timeout=args.timeout)
        if r.get("mount"):
            line = f"{status}  {m:6} {p}  (mount: bypasses app dependencies)"
            if status not in (401, 404):
                fails.append(line)
            else:
                warns.append(line + " - confirm it serves nothing private")
            print(line)
            continue
        if p in public:
            print(f"{status}  {m:6} {p}  (public)  body={body[:120]!r}")
            continue
        mark = "ok " if status == 401 else "FAIL"
        print(f"{status}  {m:6} {p}  {mark}")
        if status != 401:
            fails.append(f"{status} {m} {p}")

    print()
    for p in DOC_PATHS:
        status, _, _ = call("GET", base + p, timeout=args.timeout)
        print(f"{status}  GET    {p}  {'ok ' if status == 404 else 'FAIL'} (docs must be 404)")
        if status != 404:
            fails.append(f"{status} GET {p} (docs)")
    status, _, _ = call("GET", base + "/route-sweep-does-not-exist", timeout=args.timeout)
    print(f"{status}  GET    /route-sweep-does-not-exist  (info: unknown path)")

    print("\nWARNINGS:" if warns else "", *warns, sep="\n  ")
    if fails:
        print(f"\nRESULT: FAIL ({len(fails)})", *fails, sep="\n  ")
        sys.exit(1)
    print("\nRESULT: PASS - every non-public route refused without a session; docs are 404.")


# ---------------------------------------------------------------- ratelimit
def ratelimit(args):
    base = args.base.rstrip("/")
    url = base + find_login(args.routes, args.login_path)
    a, b, c = random.sample(range(10, 250), 3)
    ip_a, ip_b, ip_c = (f"203.0.113.{n}" for n in (a, b, c))

    def attempt(ip, password):
        return call("POST", url, {args.field: password},
                    {"X-Forwarded-For": ip}, timeout=args.timeout)

    print(f"Login route {url}; fake client IPs A={ip_a} B={ip_b} C={ip_c}\n")
    codes = []
    for i in range(10):
        status, _, body = attempt(ip_a, "wrong-" + secrets.token_hex(8))
        if i == 0 and status == 422:
            sys.exit(f"First attempt got 422: the login body isn't {{'{args.field}': ...}}. "
                     f"Check auth.py and pass --field. Body: {body[:200]!r}")
        codes.append(status)
    print(f"A, 10 wrong passwords: {codes}")
    status_b, _, _ = attempt(ip_b, "wrong-" + secrets.token_hex(8))
    print(f"B, 1 wrong password:   {status_b}")

    first_five_refused = all(s != 429 and not 200 <= s < 300 for s in codes[:5])
    rest_locked = all(s == 429 for s in codes[5:])
    ok = True
    if first_five_refused and rest_locked and status_b != 429:
        print("\nPASS: limit trips after 5 failures and is counted per client IP.\n"
              "uvicorn is applying X-Forwarded-For from 127.0.0.1, so behind Caddy each\n"
              "device gets its own counter as long as Caddy overwrites client-sent XFF.")
    elif rest_locked and status_b == 429:
        ok = False
        print("\nFAIL: B is locked too, so X-Forwarded-For is ignored and every client shares\n"
              "one counter (127.0.0.1). Behind Caddy one stranger would lock you out.\n"
              "127.0.0.1 is now locked for 15 minutes.")
    else:
        ok = False
        print("\nFAIL: the lockout pattern isn't 5 refusals then 429s. Check auth.py's limiter.")

    if args.check_password and ok:
        pw = getpass.getpass("\nAdmin password (not echoed, not stored): ")
        status, _, _ = attempt(ip_a, pw)
        print(f"A (locked), right password: {status}  "
              f"{'ok' if status == 429 else 'FAIL: lockout should hold even for the right password'}")
        ok &= status == 429
        status, headers, body = attempt(ip_c, pw)
        print(f"C (fresh), right password:  {status}  "
              f"{'ok' if 200 <= status < 300 else f'FAIL  body={body[:200]!r}'}")
        ok &= 200 <= status < 300
        cookie_header = None
        for sc in headers.get_all("Set-Cookie") or []:
            parts = [x.strip() for x in sc.split(";")]
            name, _, value = parts[0].partition("=")
            attrs = {x.split("=", 1)[0].lower(): (x.split("=", 1)[1] if "=" in x else True)
                     for x in parts[1:]}
            print(f"  cookie {name}: HttpOnly={attrs.get('httponly', False)} "
                  f"Secure={attrs.get('secure', False)} SameSite={attrs.get('samesite')} "
                  f"Max-Age={attrs.get('max-age')} Expires={attrs.get('expires')} "
                  f"Path={attrs.get('path')}")
            cookie_header = f"{name}={value}"
        if args.authed_get and cookie_header:
            status, _, _ = call("GET", base + args.authed_get,
                                headers={"Cookie": cookie_header, "X-Forwarded-For": ip_c},
                                timeout=args.timeout)
            print(f"GET {args.authed_get} with that session: {status}  "
                  f"{'ok' if status == 200 else 'FAIL'}")
            ok &= status == 200
    sys.exit(0 if ok else 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dump")
    d.add_argument("--out", required=True)
    for name in ("sweep", "ratelimit"):
        s = sub.add_parser(name)
        s.add_argument("--base", required=True, help="e.g. http://127.0.0.1:8000")
        s.add_argument("--routes", required=True, help="JSON written by dump")
        s.add_argument("--login-path", help="default: the one POST route ending in 'login'")
        s.add_argument("--timeout", type=float, default=30)
    r = sub.choices["ratelimit"]
    r.add_argument("--field", default="password", help="JSON field the login body uses")
    r.add_argument("--check-password", action="store_true")
    r.add_argument("--authed-get", help="a read-only GET path to try with the new session")
    for name in ("sweep", "ratelimit"):
        sub.choices[name].add_argument(
            "--cafile", help="trust only this CA bundle (e.g. certifi), not the OS store")
    args = ap.parse_args()
    if getattr(args, "cafile", None):
        global OPENER
        import ssl
        ctx = ssl.create_default_context(cafile=args.cafile)
        OPENER = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ctx))
    {"dump": dump, "sweep": sweep, "ratelimit": ratelimit}[args.cmd](args)


if __name__ == "__main__":
    main()
