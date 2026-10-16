"""
line_logger.py
Forward line logging for market-structure tests A/B/D
(see experiments/logging_plan.md). Paper mode only: this module records lines,
it never feeds predictions, models or slips.

Scope: NBA Points only, bookmakers=underdog,fanduel,draftkings in ONE call per
event. Stores both sides with price. Keyed so it never overwrites itself:
    (game_id, player_name, market_key, bookmaker, side, captured_at)

Schedule (two pulls per game):
    open    — daily at OPEN_PULL_HOUR_ET for every game on today's ET slate
    pretip  — PRETIP_MINUTES before each game's commence_time

Every pull is recorded in odds_pull_log with x-requests-remaining before and
after. A monthly credit cap (ODDS_MONTHLY_CREDIT_CAP, default 500) is enforced
before every pull from that log.

CLI:
    python line_logger.py --pull-now     # one open-phase pull of today's slate
    python line_logger.py --status       # credits used this month + projection
"""

import os
import sqlite3
import argparse
import datetime as dt
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

load_dotenv()

from database import DB_PATH

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
EVENTS_URL   = "https://api.the-odds-api.com/v4/sports/basketball_nba/events"
EVENT_ODDS_URL = "https://api.the-odds-api.com/v4/sports/basketball_nba/events/{event_id}/odds"

MARKETS    = "player_points"
BOOKMAKERS = "underdog,fanduel,draftkings"

MONTHLY_CREDIT_CAP = int(os.getenv("ODDS_MONTHLY_CREDIT_CAP", "500"))
OPEN_PULL_HOUR_ET  = int(os.getenv("LINE_LOG_OPEN_HOUR_ET", "10"))
PRETIP_MINUTES     = int(os.getenv("LINE_LOG_PRETIP_MINUTES", "45"))

EASTERN = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    # Generous timeout: the startup ETL can hold write locks for a while.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_tables() -> None:
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS line_snapshots (
            game_id          TEXT NOT NULL,
            game_date        TEXT NOT NULL,   -- ET date of commence_time
            commence_time    TEXT,
            home_team        TEXT,
            away_team        TEXT,
            player_name      TEXT NOT NULL,
            market_key       TEXT NOT NULL,
            bookmaker        TEXT NOT NULL,
            side             TEXT NOT NULL,   -- 'Over' / 'Under'
            point            REAL,
            price            REAL,            -- american odds as returned
            book_last_update TEXT,            -- bookmaker/market last_update from OddsAPI
            captured_at      TEXT NOT NULL,   -- UTC ISO, shared by all rows of one pull
            pull_phase       TEXT,            -- open / pretip / manual / event
            PRIMARY KEY (game_id, player_name, market_key, bookmaker, side, captured_at)
        );
        CREATE INDEX IF NOT EXISTS idx_line_snapshots_date
            ON line_snapshots (game_date, market_key);

        CREATE TABLE IF NOT EXISTS odds_pull_log (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at        TEXT NOT NULL,  -- UTC ISO
            game_date         TEXT,
            game_id           TEXT,           -- NULL for the (free) events call
            endpoint          TEXT NOT NULL,  -- 'events' / 'event_odds'
            pull_phase        TEXT,
            http_status       INTEGER,
            remaining_before  INTEGER,
            remaining_after   INTEGER,
            requests_last     INTEGER,        -- x-requests-last: cost of this call
            credits_used      INTEGER,
            rows_inserted     INTEGER,
            books_returned    TEXT,
            error             TEXT
        );
    """)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _int_header(resp: httpx.Response, name: str) -> int | None:
    val = resp.headers.get(name)
    try:
        return int(float(val)) if val is not None else None
    except ValueError:
        return None


def _log_pull(**fields) -> None:
    fields.setdefault("started_at", dt.datetime.now(dt.timezone.utc).isoformat())
    cols = ", ".join(fields)
    qs   = ", ".join("?" for _ in fields)
    conn = get_db()
    conn.execute(f"INSERT INTO odds_pull_log ({cols}) VALUES ({qs})", tuple(fields.values()))
    conn.commit()
    conn.close()


def credits_used_this_month() -> int:
    month_prefix = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
    conn = get_db()
    row = conn.execute(
        "SELECT COALESCE(SUM(credits_used), 0) FROM odds_pull_log WHERE started_at LIKE ?",
        (month_prefix + "%",),
    ).fetchone()
    conn.close()
    return int(row[0])


def _already_pulled(game_id: str, phase: str, game_date: str) -> bool:
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM odds_pull_log WHERE game_id = ? AND pull_phase = ? "
        "AND game_date = ? AND http_status = 200 LIMIT 1",
        (game_id, phase, game_date),
    ).fetchone()
    conn.close()
    return row is not None


def _commence(event: dict) -> dt.datetime:
    return dt.datetime.fromisoformat(event["commence_time"].replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------

def fetch_todays_events() -> tuple[list[dict], int | None]:
    """
    Return (today's ET-slate events, x-requests-remaining). The events endpoint
    does not cost credits; it is also logged so that claim is checked on every run.
    """
    if not ODDS_API_KEY:
        print("[line_logger] ODDS_API_KEY not set — skipping")
        return [], None

    today_et = dt.datetime.now(EASTERN).date()
    try:
        resp = httpx.get(EVENTS_URL, params={"apiKey": ODDS_API_KEY}, timeout=15)
    except Exception as exc:
        print(f"[line_logger] events call failed: {exc!r}")
        _log_pull(endpoint="events", game_date=today_et.isoformat(), error=repr(exc))
        return [], None

    remaining = _int_header(resp, "x-requests-remaining")
    last      = _int_header(resp, "x-requests-last")
    _log_pull(
        endpoint="events", game_date=today_et.isoformat(), http_status=resp.status_code,
        remaining_after=remaining, requests_last=last, credits_used=last or 0,
        error=None if resp.status_code == 200 else resp.text[:300],
    )
    if resp.status_code != 200:
        print(f"[line_logger] events HTTP {resp.status_code}: {resp.text[:200]}")
        return [], remaining

    events = [e for e in resp.json() if _commence(e).astimezone(EASTERN).date() == today_et]
    return events, remaining


def pull_event(event: dict, phase: str, remaining_before: int | None) -> tuple[int, int | None]:
    """
    One OddsAPI call for one event: Points, all three books. Stores every
    outcome. Returns (rows_inserted, x-requests-remaining after the call).
    Enforces the monthly credit cap before calling.
    """
    game_id   = event["id"]
    commence  = _commence(event)
    game_date = commence.astimezone(EASTERN).date().isoformat()

    used = credits_used_this_month()
    # Expected cost is 1 (1 market x 1 region-equivalent); refuse if that would breach the cap.
    if used + 1 > MONTHLY_CREDIT_CAP:
        msg = f"monthly cap reached ({used}/{MONTHLY_CREDIT_CAP})"
        print(f"[line_logger] skip {game_id}: {msg}")
        _log_pull(endpoint="event_odds", game_id=game_id, game_date=game_date,
                  pull_phase=phase, remaining_before=remaining_before, error=msg)
        return 0, remaining_before
    if remaining_before is not None and remaining_before < 1:
        msg = f"account quota exhausted (remaining={remaining_before})"
        print(f"[line_logger] skip {game_id}: {msg}")
        _log_pull(endpoint="event_odds", game_id=game_id, game_date=game_date,
                  pull_phase=phase, remaining_before=remaining_before, error=msg)
        return 0, remaining_before

    captured_at = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        resp = httpx.get(
            EVENT_ODDS_URL.format(event_id=game_id),
            params={
                "apiKey":     ODDS_API_KEY,
                "bookmakers": BOOKMAKERS,
                "markets":    MARKETS,
                "oddsFormat": "american",
            },
            timeout=15,
        )
    except Exception as exc:
        print(f"[line_logger] {game_id} request failed: {exc!r}")
        _log_pull(endpoint="event_odds", game_id=game_id, game_date=game_date,
                  pull_phase=phase, remaining_before=remaining_before, error=repr(exc))
        return 0, remaining_before

    remaining_after = _int_header(resp, "x-requests-remaining")
    last            = _int_header(resp, "x-requests-last")
    if last is not None:
        credits = last
    elif remaining_before is not None and remaining_after is not None:
        credits = remaining_before - remaining_after
    else:
        credits = 1  # assume the documented cost rather than undercount

    if resp.status_code != 200:
        print(f"[line_logger] {game_id} HTTP {resp.status_code}: {resp.text[:200]}")
        _log_pull(endpoint="event_odds", game_id=game_id, game_date=game_date,
                  pull_phase=phase, http_status=resp.status_code,
                  remaining_before=remaining_before, remaining_after=remaining_after,
                  requests_last=last, credits_used=credits, error=resp.text[:300])
        return 0, remaining_after

    data = resp.json()
    rows = []
    for book in data.get("bookmakers", []):
        for market in book.get("markets", []):
            last_update = market.get("last_update") or book.get("last_update")
            for o in market.get("outcomes", []):
                player = o.get("description")
                side   = o.get("name")
                if not player or side not in ("Over", "Under"):
                    continue
                rows.append((
                    game_id, game_date, event["commence_time"],
                    event.get("home_team"), event.get("away_team"),
                    player, market["key"], book["key"], side,
                    o.get("point"), o.get("price"), last_update,
                    captured_at, phase,
                ))

    conn = get_db()
    before = conn.total_changes
    conn.executemany("""
        INSERT OR IGNORE INTO line_snapshots
        (game_id, game_date, commence_time, home_team, away_team, player_name,
         market_key, bookmaker, side, point, price, book_last_update,
         captured_at, pull_phase)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)
    conn.commit()
    inserted = conn.total_changes - before
    conn.close()

    books = ",".join(sorted(b["key"] for b in data.get("bookmakers", [])))
    _log_pull(endpoint="event_odds", game_id=game_id, game_date=game_date,
              pull_phase=phase, http_status=200,
              remaining_before=remaining_before, remaining_after=remaining_after,
              requests_last=last, credits_used=credits, rows_inserted=inserted,
              books_returned=books)
    print(f"[line_logger] {phase} {event.get('away_team')} @ {event.get('home_team')}: "
          f"{inserted} rows, books=[{books}], credits={credits}, "
          f"remaining {remaining_before} -> {remaining_after}")
    return inserted, remaining_after


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def run_open_pull(phase: str = "open", scheduler=None) -> int:
    """Pull every not-yet-started game on today's slate once; schedule pre-tip pulls."""
    init_tables()
    events, remaining = fetch_todays_events()
    now = dt.datetime.now(dt.timezone.utc)
    total = 0
    for event in events:
        commence  = _commence(event)
        game_date = commence.astimezone(EASTERN).date().isoformat()
        if commence <= now:
            continue  # started — lines are live/pulled, no longer pre-game
        if phase == "open" and _already_pulled(event["id"], "open", game_date):
            continue
        n, remaining = pull_event(event, phase, remaining)
        total += n
        if scheduler is not None:
            _schedule_pretip(scheduler, event)
    if not events:
        print("[line_logger] no NBA events on today's ET slate")
    return total


def run_pretip_pull(event: dict) -> None:
    init_tables()
    commence  = _commence(event)
    game_date = commence.astimezone(EASTERN).date().isoformat()
    if commence <= dt.datetime.now(dt.timezone.utc):
        return
    if _already_pulled(event["id"], "pretip", game_date):
        return
    # Free events call gives a fresh x-requests-remaining "before" value.
    _, remaining = fetch_todays_events()
    pull_event(event, "pretip", remaining)


def _schedule_pretip(scheduler, event: dict) -> None:
    run_at = _commence(event) - dt.timedelta(minutes=PRETIP_MINUTES)
    now    = dt.datetime.now(dt.timezone.utc)
    if run_at <= now:
        run_at = now + dt.timedelta(seconds=30)  # inside the window already
    scheduler.add_job(
        run_pretip_pull, "date", run_date=run_at, args=[event],
        id=f"pretip:{event['id']}", replace_existing=True, misfire_grace_time=30 * 60,
    )


# ---------------------------------------------------------------------------
# Scheduler (started from main.py)
# ---------------------------------------------------------------------------

_scheduler = None


def start_scheduler():
    """Start APScheduler in-process. Idempotent. Catches up on startup if past open time."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    if os.getenv("LINE_LOGGER_ENABLED", "1") != "1":
        print("[line_logger] disabled via LINE_LOGGER_ENABLED")
        return None

    from apscheduler.schedulers.background import BackgroundScheduler

    init_tables()
    sched = BackgroundScheduler(timezone=EASTERN)
    sched.add_job(
        run_open_pull, "cron", hour=OPEN_PULL_HOUR_ET, minute=0,
        kwargs={"phase": "open", "scheduler": sched},
        id="line_open_pull", replace_existing=True, misfire_grace_time=3 * 3600,
        coalesce=True,
    )
    # Restart catch-up: if we're past the open time today, run now. The pull log
    # makes this a no-op for games already pulled.
    if dt.datetime.now(EASTERN).hour >= OPEN_PULL_HOUR_ET:
        sched.add_job(run_open_pull, kwargs={"phase": "open", "scheduler": sched},
                      id="line_open_catchup", replace_existing=True)
    sched.start()
    _scheduler = sched
    print(f"[line_logger] scheduler started: open {OPEN_PULL_HOUR_ET}:00 ET, "
          f"pretip T-{PRETIP_MINUTES}m, cap {MONTHLY_CREDIT_CAP}/month")
    return sched


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _status() -> None:
    init_tables()
    conn = get_db()
    month = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
    r = conn.execute("""
        SELECT COUNT(*) AS pulls, COALESCE(SUM(credits_used),0) AS credits,
               COUNT(DISTINCT game_date) AS days
        FROM odds_pull_log WHERE endpoint='event_odds' AND http_status=200
          AND started_at LIKE ?""", (month + "%",)).fetchone()
    last = conn.execute(
        "SELECT remaining_after FROM odds_pull_log WHERE remaining_after IS NOT NULL "
        "ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    print(f"month {month}: {r['pulls']} event pulls over {r['days']} game days, "
          f"{r['credits']} credits (cap {MONTHLY_CREDIT_CAP})")
    print(f"last x-requests-remaining: {last[0] if last else 'n/a'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pull-now", action="store_true", help="one manual pull of today's slate")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    if args.pull_now:
        run_open_pull(phase="manual")
    if args.status or not args.pull_now:
        _status()
