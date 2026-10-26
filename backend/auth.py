"""
auth.py
One admin login for the whole API (cloud migration plan v2, step 0.3).

  ADMIN_PASSWORD_HASH  argon2id hash of the admin password, in backend/.env, set by
                       `python auth.py set-password` (prompts; never echoes or logs it)
  SESSION_SECRET       random key that signs session cookies (set by the same command if
                       missing); changing it, or the password, logs every device out

Session: a signed cookie (HttpOnly, Secure, SameSite=Strict) valid SESSION_DAYS (30), so a
phone stays logged in. Logout clears it on that device. require_session is an app-wide
dependency (main.py), so a new route is protected unless added to EXEMPT_PATHS.
Failed logins: at most MAX_FAILURES per FAILURE_WINDOW_MINUTES per client IP (behind Caddy,
uvicorn's proxy headers give the real IP); after that every login from that IP is refused
with 429 until the window passes, even with the right password.
"""

import base64
import getpass
import hashlib
import hmac
import os
import secrets
import sys
import threading
import time

from dotenv import load_dotenv
from fastapi import HTTPException, Request

load_dotenv()

COOKIE_NAME = "sb_session"
SESSION_DAYS = 30
MAX_FAILURES = 5
FAILURE_WINDOW_MINUTES = 15
EXEMPT_PATHS = frozenset({"/auth/login", "/health"})
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _password_hash() -> str:
    return os.getenv("ADMIN_PASSWORD_HASH", "").strip()


def _secret() -> bytes:
    return os.getenv("SESSION_SECRET", "").strip().encode()


def configured() -> bool:
    return bool(_password_hash()) and bool(_secret())


# --- password -------------------------------------------------------------------------

def hash_password(password: str) -> str:
    from argon2 import PasswordHasher
    return PasswordHasher().hash(password)


def verify_password(password: str) -> bool:
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
    h = _password_hash()
    if not h:
        return False
    try:
        return PasswordHasher().verify(h, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# --- session tokens: "<expires>.<nonce>.<mac>" ----------------------------------------

def _mac(payload: str) -> str:
    # The password hash is part of the key: a new password invalidates every session.
    key = _secret() + b"|" + _password_hash().encode()
    return base64.urlsafe_b64encode(hmac.new(key, payload.encode(), hashlib.sha256).digest()).decode().rstrip("=")


def new_token(now: float | None = None) -> str:
    expires = int((now or time.time()) + SESSION_DAYS * 86400)
    payload = f"{expires}.{secrets.token_urlsafe(16)}"
    return f"{payload}.{_mac(payload)}"


def token_valid(token: str | None, now: float | None = None) -> bool:
    if not token or not configured():
        return False
    try:
        expires, nonce, mac = token.split(".")
        exp = int(expires)
    except ValueError:
        return False
    if not hmac.compare_digest(mac, _mac(f"{expires}.{nonce}")):
        return False
    return (now or time.time()) < exp


# --- failed-login limit ---------------------------------------------------------------

_failures: dict[str, list[float]] = {}
_failures_lock = threading.Lock()


def _recent(ip: str, now: float) -> list[float]:
    cutoff = now - FAILURE_WINDOW_MINUTES * 60
    times = [t for t in _failures.get(ip, []) if t > cutoff]
    if times:
        _failures[ip] = times
    else:
        _failures.pop(ip, None)
    return times


def locked_out(ip: str, now: float | None = None) -> bool:
    with _failures_lock:
        return len(_recent(ip, now or time.time())) >= MAX_FAILURES


def record_failure(ip: str, now: float | None = None) -> None:
    now = now or time.time()
    with _failures_lock:
        _recent(ip, now)
        _failures.setdefault(ip, []).append(now)


def reset_failures() -> None:
    with _failures_lock:
        _failures.clear()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# --- FastAPI pieces -------------------------------------------------------------------

def require_session(request: Request) -> None:
    """App-wide dependency: 401 without a valid session, except on EXEMPT_PATHS."""
    if request.url.path in EXEMPT_PATHS:
        return
    if not token_valid(request.cookies.get(COOKIE_NAME)):
        raise HTTPException(status_code=401, detail="Not logged in")


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(COOKIE_NAME, token, max_age=SESSION_DAYS * 86400, httponly=True,
                        secure=True, samesite="strict", path="/")


def clear_session_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, httponly=True, secure=True, samesite="strict", path="/")


# --- CLI: set the password ------------------------------------------------------------

def _write_env(updates: dict, path: str = ENV_PATH) -> None:
    """Replace or append KEY='value' lines in .env, keeping every other line as it is.
    Values are single-quoted: python-dotenv and systemd then read '$' literally."""
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    seen = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in updates:
            lines[i] = f"{key}='{updates[key]}'"
            seen.add(key)
    lines += [f"{k}='{v}'" for k, v in updates.items() if k not in seen]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    if sys.platform != "win32":
        os.chmod(path, 0o600)


def set_password_cli(path: str = ENV_PATH) -> None:
    pw = getpass.getpass("New admin password: ")
    if len(pw) < 12:
        sys.exit("Use at least 12 characters.")
    if getpass.getpass("Repeat it: ") != pw:
        sys.exit("The two entries differ; nothing changed.")
    updates = {"ADMIN_PASSWORD_HASH": hash_password(pw)}
    if not _secret():
        updates["SESSION_SECRET"] = secrets.token_urlsafe(48)
    _write_env(updates, path)
    print(f"Saved ADMIN_PASSWORD_HASH{' and SESSION_SECRET' if 'SESSION_SECRET' in updates else ''} "
          f"to {path}. Restart the backend to use it; every device logs in again.")


if __name__ == "__main__":
    if sys.argv[1:] == ["set-password"]:
        set_password_cli()
    else:
        sys.exit("usage: python auth.py set-password")
