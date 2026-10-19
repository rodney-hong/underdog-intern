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
    pretip  — PRETIP_MINUTES (30) before each game's commence_time, catching the
              official injury report's late updates; one retry PRETIP_RETRY_MINUTES
              later if the call itself fails (not if it was refused by the cap)

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
PRETIP_MINUTES     = int(os.getenv("LINE_LOG_PRETIP_MINUTES", "30"))
PRETIP_RETRY_MINUTES = 5   # one retry after a FAILED pre-tip call (error / non-200),
                           # only if it still lands >= 1 minute before tip

SCHEDULED_PULLS_PER_GAME = 2      # open + pretip
EVENT_PULLS_PER_DAY_MAX  = 3      # ceiling for news-triggered pulls, not a quota
RESERVE_BUFFER           = 0.10   # reserve = remaining scheduled pulls x (1 + buffer)

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
            error             TEXT,
            skip_reason       TEXT,           -- set when a pull was refused before calling
            note              TEXT            -- free text, e.g. what triggered an event pull
        );
    """)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(odds_pull_log)")}
    for col in ("skip_reason", "note"):
        if col not in cols:
            conn.execute(f"ALTER TABLE odds_pull_log ADD COLUMN {col} TEXT")
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


def last_known_remaining() -> int | None:
    conn = get_db()
    row = conn.execute(
        "SELECT remaining_after FROM odds_pull_log WHERE remaining_after IS NOT NULL "
        "ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    return row[0] if row else None


def record_call(resp: httpx.Response, *, endpoint: str, phase: str,
                remaining_before: int | None, game_id: str | None = None,
                game_date: str | None = None, rows: int | None = None,
                books: str | None = None, note: str | None = None) -> tuple[int, int | None]:
    """
    Log one completed OddsAPI call to odds_pull_log. Used by this module and by
    data_fetcher's slip path so the monthly cap sees every credit spent.
    Returns (credits_used, x-requests-remaining after).
    """
    remaining_after = _int_header(resp, "x-requests-remaining")
    last            = _int_header(resp, "x-requests-last")
    if last is not None:
        credits = last
    elif remaining_before is not None and remaining_after is not None:
        credits = remaining_before - remaining_after
    else:
        credits = 0 if endpoint == "events" else 1  # don't undercount odds calls
    _log_pull(endpoint=endpoint, game_id=game_id, game_date=game_date, pull_phase=phase,
              http_status=resp.status_code, remaining_before=remaining_before,
              remaining_after=remaining_after, requests_last=last, credits_used=credits,
              rows_inserted=rows, books_returned=books, note=note,
              error=None if resp.status_code == 200 else resp.text[:300])
    return credits, remaining_after


def log_skip(*, endpoint: str, phase: str, reason: str, game_id: str | None = None,
             game_date: str | None = None, remaining: int | None = None,
             note: str | None = None) -> None:
    """Record a pull that was refused before any API call was made."""
    print(f"[line_logger] SKIP {phase} {game_id or ''}: {reason}")
    _log_pull(endpoint=endpoint, game_id=game_id, game_date=game_date, pull_phase=phase,
              remaining_before=remaining, credits_used=0, skip_reason=reason, note=note)


# ---------------------------------------------------------------------------
# Budget reserve for unscheduled spend (news-triggered pulls, slip endpoint)
# ---------------------------------------------------------------------------

def _last_season_daily_rate(month: int) -> float:
    """
    Games per calendar day for this month last season, from the `schedule` table
    (regular season, play-in, playoffs, cup final; excludes preseason and
    All-Star). Rate is games / days spanned by that month's first-to-last game.
    """
    conn = get_db()
    row = conn.execute("""
        SELECT COUNT(*), MIN(substr(gameDateTimeEst,1,10)), MAX(substr(gameDateTimeEst,1,10))
        FROM schedule
        WHERE CAST(substr(gameDateTimeEst,6,2) AS INTEGER) = ?
          AND substr(printf('%010d', gameId), 3, 1) IN ('2','4','5','6')
    """, (month,)).fetchone()
    conn.close()
    n, first, last = row
    if not n:
        return 0.0
    span = (dt.date.fromisoformat(last) - dt.date.fromisoformat(first)).days + 1
    return n / span


def remaining_scheduled_pulls(all_events: list[dict] | None) -> dict:
    """
    Project the scheduled (open + pretip) pulls still to come this ET month.
      today:  pulls not yet made for today's unstarted games, from the feed
      later:  per day, max(games in feed that day, last season's daily rate for
              the month), counting only days on/after the first game in the feed.
    With no feed (call failed), every remaining day uses the historical rate.
    """
    now_utc  = dt.datetime.now(dt.timezone.utc)
    today_et = now_utc.astimezone(EASTERN).date()

    feed_by_day: dict[dt.date, int] = {}
    today_pending = 0
    for e in all_events or []:
        commence = _commence(e)
        day = commence.astimezone(EASTERN).date()
        feed_by_day[day] = feed_by_day.get(day, 0) + 1
        if day == today_et and commence > now_utc:
            gd = day.isoformat()
            today_pending += (not _already_pulled(e["id"], "open", gd)) + \
                             (not _already_pulled(e["id"], "pretip", gd))

    season_start = min(feed_by_day) if feed_by_day else None
    rate = _last_season_daily_rate(today_et.month)
    later_games = 0.0
    day = today_et + dt.timedelta(days=1)
    while day.month == today_et.month:
        if all_events is None or (season_start is not None and day >= season_start):
            later_games += max(feed_by_day.get(day, 0), rate)
        day += dt.timedelta(days=1)

    later_pulls = later_games * SCHEDULED_PULLS_PER_GAME
    return {
        "today_pending_pulls": today_pending,
        "later_pulls":         round(later_pulls, 1),
        "total_pulls":         today_pending + later_pulls,
        "daily_rate_last_season": round(rate, 2),
    }


def budget_snapshot(all_events: list[dict] | None) -> dict:
    used      = credits_used_this_month()
    remaining = last_known_remaining()
    available = MONTHLY_CREDIT_CAP - used
    if remaining is not None:
        available = min(available, remaining)
    proj    = remaining_scheduled_pulls(all_events)
    reserve = int(-(-proj["total_pulls"] * (1 + RESERVE_BUFFER) // 1))  # ceil
    return {"used_this_month": used, "cap": MONTHLY_CREDIT_CAP,
            "last_remaining_header": remaining, "available": available,
            "reserve": reserve, "spendable": available - reserve, **proj}


def check_unscheduled_spend(cost: int, all_events: list[dict] | None) -> tuple[bool, str, dict]:
    """True if `cost` credits can be spent without dipping into the scheduled-pull reserve."""
    snap = budget_snapshot(all_events)
    if cost <= snap["spendable"]:
        return True, "", snap
    reason = (f"would dip into reserve: cost {cost} > spendable {snap['spendable']} "
              f"(available {snap['available']}, reserve {snap['reserve']} = "
              f"{snap['total_pulls']:.1f} remaining scheduled pulls x {1 + RESERVE_BUFFER:.1f})")
    return False, reason, snap


def todays_underdog_lines(game_date: str) -> dict:
    """
    Latest logged Underdog line per player/market for game_date (ET), in the
    shape data_fetcher.get_todays_lines() returns. Read-only; no API call.
    Only Points is logged, so only player_points appears.
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT s.player_name, s.market_key, s.point, s.game_id, s.home_team, s.away_team
        FROM line_snapshots s
        JOIN (SELECT player_name, market_key, MAX(captured_at) AS cap
              FROM line_snapshots
              WHERE game_date = ? AND bookmaker = 'underdog' AND side = 'Over'
              GROUP BY player_name, market_key) m
          ON s.player_name = m.player_name AND s.market_key = m.market_key
         AND s.captured_at = m.cap
        WHERE s.game_date = ? AND s.bookmaker = 'underdog' AND s.side = 'Over'
    """, (game_date, game_date)).fetchall()
    conn.close()
    lines: dict = {}
    for r in rows:
        entry = lines.setdefault(r["player_name"], {
            "game_id": r["game_id"], "home_team": r["home_team"], "away_team": r["away_team"]})
        entry[r["market_key"]] = {"value": r["point"], "source": "underdog"}
    return lines


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

def fetch_events(phase: str | None = None) -> tuple[list[dict] | None, int | None]:
    """
    Return (all upcoming NBA events, x-requests-remaining), or (None, None) on
    failure. The events endpoint costs 0 credits; it is logged so that is checked.
    """
    if not ODDS_API_KEY:
        print("[line_logger] ODDS_API_KEY not set — skipping")
        return None, None
    today = dt.datetime.now(EASTERN).date().isoformat()
    try:
        resp = httpx.get(EVENTS_URL, params={"apiKey": ODDS_API_KEY}, timeout=15)
    except Exception as exc:
        print(f"[line_logger] events call failed: {exc!r}")
        _log_pull(endpoint="events", game_date=today, pull_phase=phase, error=repr(exc))
        return None, None
    _, remaining = record_call(resp, endpoint="events", phase=phase,
                               remaining_before=None, game_date=today)
    if resp.status_code != 200:
        print(f"[line_logger] events HTTP {resp.status_code}: {resp.text[:200]}")
        return None, remaining
    return resp.json(), remaining


def todays(events: list[dict] | None) -> list[dict]:
    today_et = dt.datetime.now(EASTERN).date()
    return [e for e in events or [] if _commence(e).astimezone(EASTERN).date() == today_et]


def fetch_todays_events(phase: str | None = None) -> tuple[list[dict], int | None]:
    events, remaining = fetch_events(phase)
    return todays(events), remaining


def pull_event(event: dict, phase: str, remaining_before: int | None,
               note: str | None = None) -> tuple[int, int | None]:
    """
    One OddsAPI call for one event: Points, all three books. Stores every
    outcome. Returns (rows_inserted, x-requests-remaining after the call).
    Enforces the hard monthly credit cap before calling; the reserve rule for
    unscheduled pulls is applied by the caller (try_event_pull).
    """
    game_id   = event["id"]
    commence  = _commence(event)
    game_date = commence.astimezone(EASTERN).date().isoformat()

    used = credits_used_this_month()
    # Expected cost is 1 (1 market x 1 region-equivalent); refuse if that would breach the cap.
    if used + 1 > MONTHLY_CREDIT_CAP:
        log_skip(endpoint="event_odds", phase=phase, game_id=game_id, game_date=game_date,
                 remaining=remaining_before, note=note,
                 reason=f"monthly cap reached ({used}/{MONTHLY_CREDIT_CAP})")
        return 0, remaining_before
    if remaining_before is not None and remaining_before < 1:
        log_skip(endpoint="event_odds", phase=phase, game_id=game_id, game_date=game_date,
                 remaining=remaining_before, note=note,
                 reason=f"account quota exhausted (remaining={remaining_before})")
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
                  pull_phase=phase, remaining_before=remaining_before, error=repr(exc),
                  note=note)
        return 0, remaining_before

    if resp.status_code != 200:
        print(f"[line_logger] {game_id} HTTP {resp.status_code}: {resp.text[:200]}")
        _, remaining_after = record_call(resp, endpoint="event_odds", phase=phase,
                                         remaining_before=remaining_before,
                                         game_id=game_id, game_date=game_date, note=note)
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
    credits, remaining_after = record_call(
        resp, endpoint="event_odds", phase=phase, remaining_before=remaining_before,
        game_id=game_id, game_date=game_date, rows=inserted, books=books, note=note)
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


def _last_attempt(game_id: str, phase: str, game_date: str):
    conn = get_db()
    row = conn.execute(
        "SELECT http_status, skip_reason, error FROM odds_pull_log WHERE endpoint = 'event_odds' "
        "AND game_id = ? AND pull_phase = ? AND game_date = ? ORDER BY id DESC LIMIT 1",
        (game_id, phase, game_date)).fetchone()
    conn.close()
    return row


def _log_paper_predictions(game_id: str) -> None:
    """Frozen-model paper predictions for this pre-tip pull (paper_predictions.py).
    Reads the database only; never allowed to break line logging."""
    if os.getenv("PAPER_PREDICTIONS_ENABLED", "1") != "1":
        return
    try:
        import paper_predictions
        res = paper_predictions.log_for_pull(game_id)
        print(f"[line_logger] paper predictions for {game_id}: {res}")
    except Exception as exc:
        print(f"[line_logger] paper predictions FAILED for {game_id}: {exc!r}")


def run_pretip_pull(event: dict, attempt: int = 1) -> None:
    init_tables()
    commence  = _commence(event)
    game_date = commence.astimezone(EASTERN).date().isoformat()
    if commence <= dt.datetime.now(dt.timezone.utc):
        return
    if _already_pulled(event["id"], "pretip", game_date):
        return
    # Free events call gives a fresh x-requests-remaining "before" value.
    _, remaining = fetch_todays_events()
    pull_event(event, "pretip", remaining, note=None if attempt == 1 else f"retry {attempt}")

    if _already_pulled(event["id"], "pretip", game_date):
        _log_paper_predictions(event["id"])

    # One retry, only when the call itself failed. A success (even with no Underdog
    # lines) or a refusal by the cap/reserve is final: retrying would spend or be refused.
    last = _last_attempt(event["id"], "pretip", game_date)
    failed = last is not None and last["skip_reason"] is None and last["http_status"] != 200
    retry_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=PRETIP_RETRY_MINUTES)
    if failed and attempt == 1 and retry_at <= commence - dt.timedelta(minutes=1):
        if _scheduler is not None:
            _scheduler.add_job(run_pretip_pull, "date", run_date=retry_at, args=[event],
                               kwargs={"attempt": 2}, id=f"pretip-retry:{event['id']}",
                               replace_existing=True, misfire_grace_time=60)
            print(f"[line_logger] pretip failed for {event['id']}; retry at {retry_at:%H:%M} UTC")
        return retry_at
    return None


def event_pulls_today() -> int:
    """News-triggered pulls that reached the API today (ET). Skips don't count."""
    today = dt.datetime.now(EASTERN).date().isoformat()
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) FROM odds_pull_log WHERE endpoint = 'event_odds' "
        "AND pull_phase = 'event' AND game_date = ? AND http_status IS NOT NULL",
        (today,)).fetchone()
    conn.close()
    return int(row[0])


def try_event_pull(game_id: str, note: str | None = None) -> tuple[int, str]:
    """
    One extra, news-triggered pull for a game on today's slate (Phase 4).
    Refused, with the reason logged to odds_pull_log, when:
      - the game is not on today's ET slate or has already started
      - EVENT_PULLS_PER_DAY_MAX event pulls already reached the API today
      - the 1-credit cost would dip into the scheduled-pull reserve
    Returns (rows_inserted, skip_reason or "").
    """
    init_tables()
    today = dt.datetime.now(EASTERN).date().isoformat()

    def _skip(reason: str, remaining=None) -> tuple[int, str]:
        log_skip(endpoint="event_odds", phase="event", game_id=game_id, game_date=today,
                 remaining=remaining, reason=reason, note=note)
        return 0, reason

    n_today = event_pulls_today()
    if n_today >= EVENT_PULLS_PER_DAY_MAX:
        return _skip(f"daily event-pull ceiling reached ({n_today}/{EVENT_PULLS_PER_DAY_MAX})")

    all_events, remaining = fetch_events(phase="event")
    if all_events is None:
        return _skip("events feed unavailable", remaining)
    event = next((e for e in todays(all_events) if e["id"] == game_id), None)
    if event is None:
        return _skip("game not on today's ET slate", remaining)
    if _commence(event) <= dt.datetime.now(dt.timezone.utc):
        return _skip("game already started", remaining)

    ok, reason, _ = check_unscheduled_spend(1, all_events)
    if not ok:
        return _skip(reason, remaining)

    rows, _ = pull_event(event, "event", remaining, note=note)
    return rows, ""


def _schedule_pretip(scheduler, event: dict) -> None:
    run_at = _commence(event) - dt.timedelta(minutes=PRETIP_MINUTES)
    now    = dt.datetime.now(dt.timezone.utc)
    if run_at <= now:
        run_at = now + dt.timedelta(seconds=30)  # inside the window already
    scheduler.add_job(
        run_pretip_pull, "date", run_date=run_at, args=[event],
        id=f"pretip:{event['id']}", replace_existing=True,
        misfire_grace_time=(PRETIP_MINUTES - 5) * 60,
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
    init_tables()  # odds_pull_log must exist for the slip path even if logging is disabled
    if os.getenv("LINE_LOGGER_ENABLED", "1") != "1":
        print("[line_logger] disabled via LINE_LOGGER_ENABLED")
        return None

    from apscheduler.schedulers.background import BackgroundScheduler

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
    events, _ = fetch_events(phase="status")
    snap = budget_snapshot(events)
    print(f"reserve: {snap['reserve']} credits for {snap['total_pulls']:.1f} remaining scheduled "
          f"pulls (+{RESERVE_BUFFER:.0%}); available {snap['available']}; "
          f"spendable on unscheduled pulls: {snap['spendable']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pull-now", action="store_true", help="one manual pull of today's slate")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    if args.pull_now:
        run_open_pull(phase="manual")
    if args.status or not args.pull_now:
        _status()
