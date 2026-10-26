"""
deploy_guard.py
Is now a safe time to restart the backend? (cloud migration plan v2, step 0.8 and ground
rule 8; deploy.sh runs this first and again just before the restart.) A restart re-runs the
startup sync and catch-up pulls, and kills a running pre-tip job after its credits are
spent, so it is refused when:

  - a pre-tip pull (tip - 30 min) is within PRETIP_GUARD_MINUTES (45) before or after now;
  - it is 10:00-10:05 ET (the open pull);
  - the ETL lock is held (a sync, ETL or Kaggle retry is running).

Tip times come from the local schedule table (read-only), the same times the pre-tip jobs
use to within a minute. Exit code 0 = safe, 1 = refused (the reason is printed).
  python deploy_guard.py
"""

import datetime as dt
import sqlite3
import sys
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
PRETIP_MINUTES = 30
PRETIP_GUARD_MINUTES = 45
OPEN_WINDOW = (dt.time(10, 0), dt.time(10, 5))


def pretip_times(db_path: str, around: dt.datetime) -> list[dt.datetime]:
    """Pre-tip pull times of games on the ET dates of around-1 day .. around+1 day."""
    d = around.astimezone(EASTERN).date()
    days = [(d + dt.timedelta(days=k)).isoformat() for k in (-1, 0, 1)]
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        rows = conn.execute("SELECT gameDateTimeEst FROM schedule WHERE substr(gameDateTimeEst, 1, 10) "
                            "IN (?, ?, ?)", days).fetchall()
    finally:
        conn.close()
    out = []
    for (tip,) in rows:
        try:
            t = dt.datetime.fromisoformat(str(tip)[:19].replace("T", " ")).replace(tzinfo=EASTERN)
        except ValueError:
            continue
        out.append(t - dt.timedelta(minutes=PRETIP_MINUTES))
    return sorted(out)


def check(now: dt.datetime, db_path: str, etl_lock_free=None) -> tuple[bool, str]:
    """(safe, reason). etl_lock_free: callable -> bool (default: try proc_lock.ETL_LOCK once)."""
    now_et = now.astimezone(EASTERN)
    if OPEN_WINDOW[0] <= now_et.time() < OPEN_WINDOW[1]:
        return False, f"it is {now_et:%H:%M} ET, inside the 10:00-10:05 open-pull window"
    guard = dt.timedelta(minutes=PRETIP_GUARD_MINUTES)
    for p in pretip_times(db_path, now):
        if p - guard <= now_et <= p + guard:
            return False, (f"a pre-tip pull is at {p:%H:%M} ET (tip {p + dt.timedelta(minutes=PRETIP_MINUTES):%H:%M}); "
                           f"no restart within {PRETIP_GUARD_MINUTES} min of it")
    if etl_lock_free is None:
        etl_lock_free = _etl_lock_free
    if not etl_lock_free():
        return False, "the ETL lock is held: a sync, ETL or Kaggle retry is running"
    return True, "no pull or sync nearby"


def _etl_lock_free() -> bool:
    import proc_lock
    if proc_lock.ETL_LOCK.acquire(timeout=0, who="deploy guard"):
        proc_lock.ETL_LOCK.release()
        return True
    return False


if __name__ == "__main__":
    from database import DB_PATH
    ok, why = check(dt.datetime.now(dt.timezone.utc), DB_PATH)
    print(f"[deploy-guard] {'OK' if ok else 'REFUSED'}: {why}")
    sys.exit(0 if ok else 1)
