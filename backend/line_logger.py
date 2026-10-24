"""
line_logger.py
Forward line logging for market-structure tests A/B/D
(see experiments/logging_plan.md). Paper mode only: this module records lines,
it never feeds predictions, models or slips.

Scope: NBA Points, Rebounds, Assists and 3PM (STAT_MARKETS: each stat's main and
_alternate market, 8 markets), 10 named bookmakers (BOOKMAKERS) in ONE call per
event (CREDITS_PER_PULL = 8 at most; OddsAPI bills only the markets returned).
Underdog's lines arrive mostly in the _alternate markets (Points: probe 2026-10-20,
experiments/probe_20261020T014326Z.json) and sometimes in the main market;
FanDuel/DraftKings appear in both, with full alternate ladders. Stores every
outcome, both sides, with price, under its real market key. Keyed so it never
overwrites or merges rows:
    (game_id, player_name, market_key, bookmaker, side, point, outcome_index, captured_at)
outcome_index is the outcome's position in its market: FanDuel's alternate market
repeats (player, side, point) at different prices, so point alone is not unique.

Schedule (two pulls per game):
    open    — OPEN_PULL_HOUR_ET (10:00 ET), every not-yet-started game on today's
              slate. ON by default (LINE_LOG_OPEN_PULL_ENABLED=0 turns it off).
              After a restart, caught up only until OPEN_CATCHUP_HOURS past 10:00.
    pretip  — PRETIP_MINUTES (30) before each game's commence_time, catching the
              official injury report's late updates; one retry PRETIP_RETRY_MINUTES
              later if the call itself fails (not if it was refused by the cap).
              Scheduled by a free events-only job at SCHEDULE_HOUR_ET (06:00, and on
              startup) whether or not the open pull is on; if that events call fails
              it retries every 20 min until the last tip-off.

Every pull is recorded in odds_pull_log with x-requests-remaining before and
after. A monthly credit cap (ODDS_MONTHLY_CREDIT_CAP, default 500) is enforced
before every pull from that log; set it in .env to the plan's monthly credits.

CLI:
    python line_logger.py --pull-now     # one open-phase pull of today's slate
    python line_logger.py --status       # credits used this month + projection
"""

import os
import sqlite3
import argparse
import threading
import time
import datetime as dt
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

load_dotenv()

from database import DB_PATH

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
EVENTS_URL   = "https://api.the-odds-api.com/v4/sports/basketball_nba/events"
EVENT_ODDS_URL = "https://api.the-odds-api.com/v4/sports/basketball_nba/events/{event_id}/odds"

# stat_type -> (main market, alternate market). Keys to be confirmed with
# experiments/market_keys_probe.py (one event-markets call, 1 credit).
STAT_MARKETS = {
    "Points":   ("player_points",   "player_points_alternate"),
    "Rebounds": ("player_rebounds", "player_rebounds_alternate"),
    "Assists":  ("player_assists",  "player_assists_alternate"),
    "3PM":      ("player_threes",   "player_threes_alternate"),
}
MARKETS = ",".join(k for keys in STAT_MARKETS.values() for k in keys)
# Exactly 10 named books: up to 10 count as one region-equivalent, so cost stays
# CREDITS_PER_PULL. underdog/fanduel/draftkings feed paper_predictions and H2; the
# other seven are logged for exploratory use only.
BOOKMAKERS = ("underdog,fanduel,draftkings,prizepicks,novig,prophetx,kalshi,"
              "betmgm,espnbet,betrivers")
UNDERDOG_POINTS_MARKET = "player_points_alternate"  # where most Underdog Points lines come in
# ...but some arrive in player_points (opening-day check pull, 2026-10-20): read both,
# and likewise both markets of every stat.
UNDERDOG_POINTS_MARKETS = STAT_MARKETS["Points"]
UNDERDOG_MARKETS = tuple(k for keys in STAT_MARKETS.values() for k in keys)
MARKET_STAT = {k: stat for stat, keys in STAT_MARKETS.items() for k in keys}

# OddsAPI cost per event-odds call = markets returned x region-equivalents; up to 10
# named bookmakers count as one region-equivalent. Budgeted at the maximum: every
# requested market returned.
CREDITS_PER_PULL = len(MARKETS.split(","))

MONTHLY_CREDIT_CAP = int(os.getenv("ODDS_MONTHLY_CREDIT_CAP", "500"))
OPEN_PULL_ENABLED  = os.getenv("LINE_LOG_OPEN_PULL_ENABLED", "1") == "1"
OPEN_PULL_HOUR_ET  = int(os.getenv("LINE_LOG_OPEN_HOUR_ET", "10"))
OPEN_CATCHUP_HOURS = 3   # restart catch-up window for the open pull (= its misfire grace)
SCHEDULE_HOUR_ET   = int(os.getenv("LINE_LOG_SCHEDULE_HOUR_ET", "6"))  # free pre-tip scheduling job
EVENTS_RETRY_MINUTES = 20  # retry a FAILED scheduling events call until today's last tip-off
MAINTENANCE_HOUR_ET  = 7   # daily run_sync: backup -> rosters -> Kaggle check -> ETL -> resolve
KAGGLE_RETRY_MINUTES = 60      # re-check for a new Kaggle version this often while nothing newer is out
PULL_WINDOW_MINUTES  = 10      # no ETL starts within this many minutes of a scheduled pull (open or pre-tip)
PRETIP_LOCK_WAIT_MINUTES = 15  # longest an ETL waits for a running pre-tip job before deferring
PULL_JOB_PREFIXES = ("line_open_pull", "line_open_catchup", "pretip:", "pretip-retry:")
BACKUP_DIR  = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "backups")
BACKUP_KEEP = 3
PRETIP_MINUTES     = int(os.getenv("LINE_LOG_PRETIP_MINUTES", "30"))
PRETIP_RETRY_MINUTES = 5   # one retry after a FAILED pre-tip call (error / non-200),
                           # only if it still lands >= 1 minute before tip

SCHEDULED_PHASES = ("open", "pretip") if OPEN_PULL_ENABLED else ("pretip",)
SCHEDULED_PULLS_PER_GAME = len(SCHEDULED_PHASES)
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


_LINE_SNAPSHOTS_DDL = """
        CREATE TABLE IF NOT EXISTS line_snapshots (
            game_id          TEXT NOT NULL,
            game_date        TEXT NOT NULL,   -- ET date of commence_time
            commence_time    TEXT,
            home_team        TEXT,
            away_team        TEXT,
            player_name      TEXT NOT NULL,
            market_key       TEXT NOT NULL,   -- a STAT_MARKETS key, e.g. player_rebounds_alternate
            bookmaker        TEXT NOT NULL,
            side             TEXT NOT NULL,   -- 'Over' / 'Under'
            point            REAL,
            price            REAL,            -- american odds as returned
            book_last_update TEXT,            -- bookmaker/market last_update from OddsAPI
            captured_at      TEXT NOT NULL,   -- UTC ISO, shared by all rows of one pull
            pull_phase       TEXT,            -- open / pretip / manual / event / check
            outcome_index    INTEGER NOT NULL DEFAULT 0,  -- position within its market's outcomes
            PRIMARY KEY (game_id, player_name, market_key, bookmaker, side, point,
                         outcome_index, captured_at)
        )"""
_LINE_SNAPSHOTS_BACKUP = "line_snapshots_pre_alt_key"


def _migrate_line_snapshots(conn: sqlite3.Connection) -> None:
    """
    One-time, atomic migration from the old key (no point, no outcome_index), under
    which alternate-ladder rows would collapse to one per player/side. The old table
    is kept, renamed, as a backup; its rows are copied with outcome_index = 0 (unique
    under the old key, so unique under the new one).
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(line_snapshots)")]
    if not cols or "outcome_index" in cols:
        return
    old_cols = ", ".join(cols)
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(f"ALTER TABLE line_snapshots RENAME TO {_LINE_SNAPSHOTS_BACKUP}")
        conn.execute("DROP INDEX IF EXISTS idx_line_snapshots_date")  # moved with the rename
        conn.execute(_LINE_SNAPSHOTS_DDL)
        conn.execute(f"INSERT INTO line_snapshots ({old_cols}, outcome_index) "
                     f"SELECT {old_cols}, 0 FROM {_LINE_SNAPSHOTS_BACKUP}")
        n_old = conn.execute(f"SELECT COUNT(*) FROM {_LINE_SNAPSHOTS_BACKUP}").fetchone()[0]
        n_new = conn.execute("SELECT COUNT(*) FROM line_snapshots").fetchone()[0]
        if n_old != n_new:
            raise RuntimeError(f"line_snapshots migration copied {n_new} of {n_old} rows")
        conn.execute("COMMIT")
        print(f"[line_logger] line_snapshots migrated to the point/outcome_index key "
              f"({n_new} rows; old table kept as {_LINE_SNAPSHOTS_BACKUP})")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.isolation_level = ""


def init_tables() -> None:
    conn = get_db()
    _migrate_line_snapshots(conn)
    conn.executescript(_LINE_SNAPSHOTS_DDL + """;
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
                books: str | None = None, note: str | None = None,
                expected_cost: int = 1) -> tuple[int, int | None]:
    """
    Log one completed OddsAPI call to odds_pull_log. Used by this module and by
    data_fetcher's slip path so the monthly cap sees every credit spent.
    expected_cost is charged only when the response carries no usable cost headers.
    Returns (credits_used, x-requests-remaining after).
    """
    remaining_after = _int_header(resp, "x-requests-remaining")
    last            = _int_header(resp, "x-requests-last")
    if last is not None:
        credits = last
    elif remaining_before is not None and remaining_after is not None:
        credits = remaining_before - remaining_after
    else:
        credits = 0 if endpoint == "events" else expected_cost  # don't undercount odds calls
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
    Project the scheduled pulls (SCHEDULED_PHASES) still to come this ET month.
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
            today_pending += sum(not _already_pulled(e["id"], ph, gd) for ph in SCHEDULED_PHASES)

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
    reserve = int(-(-proj["total_pulls"] * CREDITS_PER_PULL * (1 + RESERVE_BUFFER) // 1))  # ceil, credits
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
              f"{snap['total_pulls']:.1f} remaining scheduled pulls x {CREDITS_PER_PULL} credits "
              f"x {1 + RESERVE_BUFFER:.1f})")
    return False, reason, snap


def todays_underdog_lines(game_date: str) -> dict:
    """
    Latest logged Underdog line per player and stat for game_date (ET), in the
    shape data_fetcher.get_todays_lines() returns. Read-only; no API call.
    Each stat's Underdog lines are stored under its main and _alternate market
    (STAT_MARKETS); both are read and returned under the main key
    (player_points, player_rebounds, player_assists, player_threes), the keys the
    slip path looks up. A player/stat with more than one distinct Underdog point
    across the two markets in its latest pull is left out rather than guessed.
    """
    qs = ", ".join("?" * len(UNDERDOG_MARKETS))
    conn = get_db()
    rows = conn.execute(f"""
        SELECT player_name, market_key, point, captured_at, game_id, home_team, away_team
        FROM line_snapshots
        WHERE game_date = ? AND bookmaker = 'underdog' AND market_key IN ({qs})
    """, (game_date, *UNDERDOG_MARKETS)).fetchall()
    conn.close()
    latest: dict = {}  # (player, stat) -> (captured_at, rows of that pull)
    for r in rows:
        key = (r["player_name"], MARKET_STAT[r["market_key"]])
        cap, group = latest.get(key, (None, []))
        if cap is None or r["captured_at"] > cap:
            latest[key] = (r["captured_at"], [r])
        elif r["captured_at"] == cap:
            group.append(r)
    lines: dict = {}
    for (player, stat), (_, group) in sorted(latest.items()):
        pts = {r["point"] for r in group}
        if len(pts) != 1:
            continue
        r = group[0]
        entry = lines.setdefault(player, {"game_id": r["game_id"], "home_team": r["home_team"],
                                          "away_team": r["away_team"]})
        entry[STAT_MARKETS[stat][0]] = {"value": next(iter(pts)), "source": "underdog"}
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


_INSERT_SNAPSHOT_SQL = """
    INSERT OR IGNORE INTO line_snapshots
    (game_id, game_date, commence_time, home_team, away_team, player_name,
     market_key, bookmaker, side, point, price, book_last_update,
     captured_at, pull_phase, outcome_index)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def parse_event_odds(event: dict, data: dict, captured_at: str, phase: str) -> list[tuple]:
    """
    Rows for line_snapshots from one event-odds response: every bookmaker, every
    market, every Over/Under outcome, each under its own market key and its
    position in that market (outcome_index). Pure: no I/O.
    """
    game_date = _commence(event).astimezone(EASTERN).date().isoformat()
    rows = []
    for book in data.get("bookmakers", []):
        for market in book.get("markets", []):
            last_update = market.get("last_update") or book.get("last_update")
            for idx, o in enumerate(market.get("outcomes", [])):
                player = o.get("description")
                side   = o.get("name")
                if not player or side not in ("Over", "Under"):
                    continue
                rows.append((
                    event["id"], game_date, event["commence_time"],
                    event.get("home_team"), event.get("away_team"),
                    player, market["key"], book["key"], side,
                    o.get("point"), o.get("price"), last_update,
                    captured_at, phase, idx,
                ))
    return rows


def pull_event(event: dict, phase: str, remaining_before: int | None,
               note: str | None = None) -> tuple[int, int | None]:
    """
    One OddsAPI call for one event: MARKETS (4 stats, main + alternate), BOOKMAKERS.
    Stores every outcome. Returns (rows_inserted, x-requests-remaining after the call).
    Enforces the hard monthly credit cap before calling; the reserve rule for
    unscheduled pulls is applied by the caller (try_event_pull).
    """
    game_id   = event["id"]
    commence  = _commence(event)
    game_date = commence.astimezone(EASTERN).date().isoformat()

    used = credits_used_this_month()
    # Maximum cost is CREDITS_PER_PULL (8 markets x 1 region-equivalent); refuse if
    # that would breach the cap.
    if used + CREDITS_PER_PULL > MONTHLY_CREDIT_CAP:
        log_skip(endpoint="event_odds", phase=phase, game_id=game_id, game_date=game_date,
                 remaining=remaining_before, note=note,
                 reason=f"monthly cap reached ({used}/{MONTHLY_CREDIT_CAP})")
        return 0, remaining_before
    if remaining_before is not None and remaining_before < CREDITS_PER_PULL:
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
                                         game_id=game_id, game_date=game_date, note=note,
                                         expected_cost=CREDITS_PER_PULL)
        return 0, remaining_after

    data = resp.json()
    rows = parse_event_odds(event, data, captured_at, phase)

    conn = get_db()
    before = conn.total_changes
    conn.executemany(_INSERT_SNAPSHOT_SQL, rows)
    conn.commit()
    inserted = conn.total_changes - before
    conn.close()

    books = ",".join(sorted(b["key"] for b in data.get("bookmakers", [])))
    ud_stats = {MARKET_STAT[r[6]] for r in rows if r[7] == "underdog" and r[6] in MARKET_STAT}
    missing = [stat for stat in STAT_MARKETS if stat not in ud_stats]
    if missing:
        print(f"[line_logger] {game_id}: no Underdog rows for {', '.join(missing)} in this pull")
    credits, remaining_after = record_call(
        resp, endpoint="event_odds", phase=phase, remaining_before=remaining_before,
        game_id=game_id, game_date=game_date, rows=inserted, books=books, note=note,
        expected_cost=CREDITS_PER_PULL)
    # OddsAPI bills only the markets returned, so a cost below CREDITS_PER_PULL is
    # normal (e.g. no alternate ladder posted yet). Only a HIGHER cost breaks the budget.
    last = _int_header(resp, "x-requests-last")
    if last is not None and last > CREDITS_PER_PULL:
        print("!" * 78)
        print(f"[line_logger] WARNING {game_id}: x-requests-last={last}, above the expected "
              f"maximum {CREDITS_PER_PULL} for {CREDITS_PER_PULL} markets with "
              f"{len(BOOKMAKERS.split(','))} named books. Credit projection and reserve "
              f"assume at most {CREDITS_PER_PULL}/pull.")
        print("!" * 78)
    print(f"[line_logger] {phase} {event.get('away_team')} @ {event.get('home_team')}: "
          f"{inserted} rows, books=[{books}], credits={credits}, "
          f"remaining {remaining_before} -> {remaining_after}")
    return inserted, remaining_after


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def run_open_pull(phase: str = "open", scheduler=None) -> int:
    """
    Pull every not-yet-started game on today's slate once. Also (re)schedules each
    game's pre-tip pull; schedule_todays_pretips does that too, independently, so
    pre-tip pulls never depend on this job succeeding.
    """
    init_tables()
    events, remaining = fetch_todays_events()
    now = dt.datetime.now(dt.timezone.utc)
    total = 0
    for event in events:
        commence  = _commence(event)
        game_date = commence.astimezone(EASTERN).date().isoformat()
        if commence <= now:
            continue  # started — lines are live/pulled, no longer pre-game
        if scheduler is not None:
            _schedule_pretip(scheduler, event)  # same job id: idempotent
        if phase == "open" and _already_pulled(event["id"], "open", game_date):
            continue
        n, remaining = pull_event(event, phase, remaining)
        total += n
    if not events:
        print("[line_logger] no NBA events on today's ET slate")
    return total


def _last_tip_today_utc() -> dt.datetime | None:
    """Latest tip-off on today's ET slate from the `schedule` table (ET wall-clock), or None."""
    today = dt.datetime.now(EASTERN).date().isoformat()
    conn = get_db()
    try:
        row = conn.execute("SELECT MAX(gameDateTimeEst) FROM schedule "
                           "WHERE substr(gameDateTimeEst, 1, 10) = ?", (today,)).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        conn.close()
    if not row or not row[0]:
        return None
    try:
        naive = dt.datetime.fromisoformat(str(row[0])[:19].replace("T", " "))
    except ValueError:
        return None
    return naive.replace(tzinfo=EASTERN).astimezone(dt.timezone.utc)


def _retry_scheduling(scheduler, now: dt.datetime) -> None:
    """After a failed events call: retry in EVENTS_RETRY_MINUTES while today's last game hasn't started."""
    last_tip = _last_tip_today_utc()
    if last_tip is None:  # schedule unknown: keep trying until midnight ET
        tomorrow = dt.datetime.now(EASTERN).date() + dt.timedelta(days=1)
        last_tip = dt.datetime.combine(tomorrow, dt.time(0, 0), EASTERN).astimezone(dt.timezone.utc)
    retry_at = now + dt.timedelta(minutes=EVENTS_RETRY_MINUTES)
    if scheduler is None:
        print("[line_logger] pre-tip scheduling FAILED: events call failed; no scheduler, no retry")
    elif retry_at >= last_tip:
        print(f"[line_logger] pre-tip scheduling FAILED: events call failed; no retry "
              f"(today's last game starts {last_tip:%H:%M} UTC)")
    else:
        scheduler.add_job(schedule_todays_pretips, "date", run_date=retry_at,
                          kwargs={"scheduler": scheduler}, id="line_pretip_scheduler_retry",
                          replace_existing=True, misfire_grace_time=10 * 60)
        print(f"[line_logger] pre-tip scheduling FAILED: events call failed; "
              f"retry at {retry_at:%H:%M} UTC (every {EVENTS_RETRY_MINUTES} min until "
              f"{last_tip:%H:%M} UTC)")


def schedule_todays_pretips(scheduler=None) -> int:
    """
    Free (events-only) daily job, run whether or not the open pull is on: schedule a
    pre-tip pull for every not-yet-started game on today's ET slate. Spends no
    credits. Returns the number of games scheduled, or -1 if the events call
    failed (logged in odds_pull_log by fetch_events; retried every
    EVENTS_RETRY_MINUTES until today's last game has started).
    """
    init_tables()
    all_events, _ = fetch_events(phase="schedule")
    now = dt.datetime.now(dt.timezone.utc)
    if all_events is None:
        _retry_scheduling(scheduler, now)
        return -1
    events = todays(all_events)
    n = 0
    for event in events:
        if _commence(event) <= now:
            continue
        if scheduler is not None:
            _schedule_pretip(scheduler, event)
        n += 1
    print(f"[line_logger] {n} pre-tip pull(s) scheduled for today's ET slate"
          if events else "[line_logger] no NBA events on today's ET slate")
    return n


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


# One pre-tip job (pull + paper predictions) at a time in this process. log_for_pull
# holds a write transaction for ~25-30 s per game; with several games sharing a tip
# time, concurrent jobs hit the 30 s busy timeout ("database is locked") and lost
# snapshots or paper rows (audit 2026-10-23: 2 of 5 failed on a DB copy).
_PRETIP_LOCK = threading.Lock()


def run_pretip_pull(event: dict, attempt: int = 1):
    """_run_pretip_pull under _PRETIP_LOCK; logs how long it waited for another game's job."""
    label = f"{event.get('away_team')} @ {event.get('home_team')} ({event['id']})"
    if not _PRETIP_LOCK.acquire(blocking=False):
        t0 = time.monotonic()
        print(f"[line_logger] pretip {label}: waiting for another pre-tip job")
        _PRETIP_LOCK.acquire()
        print(f"[line_logger] pretip {label}: waited {time.monotonic() - t0:.1f}s")
    try:
        return _run_pretip_pull(event, attempt)
    finally:
        _PRETIP_LOCK.release()


def _run_pretip_pull(event: dict, attempt: int = 1):
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
      - the CREDITS_PER_PULL cost would dip into the scheduled-pull reserve
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

    ok, reason, _ = check_unscheduled_spend(CREDITS_PER_PULL, all_events)
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
# Backups and restore (used by run_sync and the Kaggle retry)
# ---------------------------------------------------------------------------

def backup_database(today_et: dt.date | None = None) -> str:
    """
    Online backup of predictions.db (SQLite backup API, safe while other connections
    write) to BACKUP_DIR/predictions_YYYY-MM-DD.db, checked with PRAGMA quick_check
    before it replaces any file. Then keeps the newest BACKUP_KEEP backups.
    Raises on any failure; nothing older is deleted unless the new backup is good.
    """
    today_et = today_et or dt.datetime.now(EASTERN).date()
    os.makedirs(BACKUP_DIR, exist_ok=True)
    final = os.path.join(BACKUP_DIR, f"predictions_{today_et.isoformat()}.db")
    partial = final + ".partial"
    if os.path.exists(partial):
        os.remove(partial)
    src = sqlite3.connect(DB_PATH, timeout=30)
    dst = sqlite3.connect(partial, timeout=30)
    try:
        src.backup(dst)
        check = dst.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        dst.close()
        src.close()
    if check != "ok":
        os.remove(partial)
        raise RuntimeError(f"backup failed quick_check: {check}")
    os.replace(partial, final)

    backups = sorted(f for f in os.listdir(BACKUP_DIR)
                     if f.startswith("predictions_") and f.endswith(".db"))
    for old in backups[:-BACKUP_KEEP]:
        os.remove(os.path.join(BACKUP_DIR, old))
    return final


ETL_TABLES = ("player_stats", "players", "schedule", "kaggle_version")  # what run_etl replaces


def restore_etl_tables(backup_path: str) -> dict:
    """
    Put back ONLY the tables run_etl replaces (ETL_TABLES) from backup_path, in one
    transaction: ATTACH the backup, then per table DROP, re-CREATE with the backup's
    own DDL, copy every row, and re-create the backup's indexes on it. Row counts are
    checked before COMMIT; any error rolls the whole restore back. Every other table
    (line_snapshots, odds_pull_log, paper_predictions, news_*, ...) is untouched.
    A table absent from the backup is left as it is. Returns {table: rows restored}.
    """
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.isolation_level = None                       # explicit transaction control
    restored: dict = {}
    try:
        conn.execute("ATTACH DATABASE ? AS bk", (backup_path,))
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for t in ETL_TABLES:
                    ddl = conn.execute("SELECT sql FROM bk.sqlite_master WHERE type = 'table' "
                                       "AND name = ?", (t,)).fetchone()
                    if not ddl:
                        continue
                    idx = [r[0] for r in conn.execute(
                        "SELECT sql FROM bk.sqlite_master WHERE type = 'index' AND tbl_name = ? "
                        "AND sql IS NOT NULL", (t,))]
                    conn.execute(f'DROP TABLE IF EXISTS main."{t}"')
                    conn.execute(ddl[0])                  # unqualified -> created in main
                    conn.execute(f'INSERT INTO main."{t}" SELECT * FROM bk."{t}"')
                    for sql in idx:
                        conn.execute(sql)
                    n_main = conn.execute(f'SELECT COUNT(*) FROM main."{t}"').fetchone()[0]
                    n_bk = conn.execute(f'SELECT COUNT(*) FROM bk."{t}"').fetchone()[0]
                    if n_main != n_bk:
                        raise RuntimeError(f"{t}: restored {n_main} of {n_bk} rows")
                    restored[t] = n_main
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.execute("DETACH DATABASE bk")
    finally:
        conn.close()
    return restored


# ---------------------------------------------------------------------------
# ETL guards. The load goes into staging tables and is swapped in with one short
# transaction (database._swap_in), so readers (WAL) only ever see the old tables or
# the new ones, never a partial load. What is left to protect is the write lock:
# the only long writer is a pre-tip job (~30 s per game, busy timeout 30 s). So the
# ETL holds _PRETIP_LOCK for the load (waits for a running pre-tip job, and any that
# comes due queues behind it), and never starts within PULL_WINDOW_MINUTES of a
# scheduled pull. That replaces the old 90-min-before-tip / 60-min-before-pre-tip rules.
# ---------------------------------------------------------------------------

def _schedule_pretip_times(day: dt.datetime) -> list[dt.datetime]:
    """
    Pre-tip pull times (tip - PRETIP_MINUTES) of `day`'s and the next day's games in
    the schedule table (gameDateTimeEst is ET wall time). They count even before the
    scheduler has today's pre-tip jobs (startup) or with no scheduler (a manual run).
    """
    d = day.astimezone(EASTERN).date()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        rows = conn.execute("SELECT gameDateTimeEst FROM schedule WHERE substr(gameDateTimeEst, 1, 10) "
                            "IN (?, ?)", (d.isoformat(), (d + dt.timedelta(days=1)).isoformat())).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    times = []
    for (tip,) in rows:
        try:
            naive = dt.datetime.fromisoformat(str(tip)[:19].replace("T", " "))
        except ValueError:
            continue
        times.append(naive.replace(tzinfo=EASTERN) - dt.timedelta(minutes=PRETIP_MINUTES))
    return times


def _scheduled_pull_times(scheduler, day: dt.datetime) -> list[dt.datetime]:
    """
    Run times of scheduled pulls: pending open / pre-tip jobs, `day`'s open-pull time
    (once the open-pull cron fires its next_run_time is already tomorrow), and the
    pre-tip times implied by the schedule table.
    """
    times = _schedule_pretip_times(day)
    if OPEN_PULL_ENABLED:
        times.append(day.astimezone(EASTERN).replace(hour=OPEN_PULL_HOUR_ET, minute=0,
                                                     second=0, microsecond=0))
    if scheduler is not None:
        times += [j.next_run_time for j in scheduler.get_jobs()
                  if j.id.startswith(PULL_JOB_PREFIXES) and j.next_run_time is not None]
    return times


def _clear_of_pulls(scheduler, t: dt.datetime) -> dt.datetime:
    """Earliest time >= t that is not within PULL_WINDOW_MINUTES of a scheduled pull."""
    w = dt.timedelta(minutes=PULL_WINDOW_MINUTES)
    pulls = _scheduled_pull_times(scheduler, t)
    moved = True
    while moved:                                  # windows can chain (pulls < 2 windows apart)
        moved = False
        for p in pulls:
            if p - w <= t < p + w:
                t, moved = (p + w).astimezone(EASTERN), True
    return t


def _player_stats_rows() -> int:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        return conn.execute("SELECT COUNT(*) FROM player_stats").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def _guarded_etl(scheduler, tag: str, version: int) -> dict:
    """
    database.run_etl() for Kaggle `version` under the ETL guards (see the section note):
      0. download its files first (no table touched, no lock held): run_etl then reads
         them from kagglehub's cache, so the pre-tip lock is held for the load only;
      1. not within PULL_WINDOW_MINUTES of a scheduled pull, else
         {"etl": "waited", "clear_at": end of that window};
      2. take _PRETIP_LOCK, waiting up to PRETIP_LOCK_WAIT_MINUTES for a running
         pre-tip job; re-check 1 after waiting.
    Raises what download_kaggle_files / run_etl raise (KaggleVersionUnavailable on a 404).
    Returns {"etl": "ran" | "up to date" | "waited", "rows_added", "reason", "clear_at"}.
    """
    import database
    database.download_kaggle_files(version)
    print(f"{tag} v{version} files downloaded")

    def window_closed():
        now = dt.datetime.now(EASTERN)
        clear = _clear_of_pulls(scheduler, now)
        if clear <= now:
            return None
        reason = f"within {PULL_WINDOW_MINUTES} min of a scheduled pull"
        print(f"{tag} ETL waiting: {reason}; moved to {clear:%H:%M} ET")
        return {"etl": "waited", "reason": reason, "clear_at": clear, "rows_added": 0}

    closed = window_closed()
    if closed:
        return closed
    if not _PRETIP_LOCK.acquire(blocking=False):
        print(f"{tag} ETL waiting for a running pre-tip job")
        t0 = time.monotonic()
        if not _PRETIP_LOCK.acquire(timeout=PRETIP_LOCK_WAIT_MINUTES * 60):
            reason = f"pre-tip job still running after {PRETIP_LOCK_WAIT_MINUTES} min"
            clear = dt.datetime.now(EASTERN) + dt.timedelta(minutes=PULL_WINDOW_MINUTES)
            print(f"{tag} ETL waiting: {reason}; moved to {clear:%H:%M} ET")
            return {"etl": "waited", "reason": reason, "clear_at": clear, "rows_added": 0}
        print(f"{tag} pre-tip job finished after {time.monotonic() - t0:.0f}s")
        closed = window_closed()
        if closed:
            _PRETIP_LOCK.release()
            return closed
    try:
        before = _player_stats_rows()
        ran = database.run_etl()
        added = _player_stats_rows() - before if ran else 0
        return {"etl": "ran" if ran else "up to date", "rows_added": added}
    finally:
        _PRETIP_LOCK.release()


# ---------------------------------------------------------------------------
# Sync: backup -> rosters -> Kaggle version check -> ETL if newer -> resolve.
# Runs right after startup, daily at MAINTENANCE_HOUR_ET, and from POST /sync.
# ---------------------------------------------------------------------------

MAINTENANCE_LOCK_WAIT_MINUTES = 30  # wait this long for another ETL/sync, then skip
_last_sync: dict | None = None


def last_sync() -> dict | None:
    """Result of the latest finished sync in this process (None before the first)."""
    return _last_sync


def _todays_backup_path() -> str:
    return os.path.join(BACKUP_DIR, f"predictions_{dt.datetime.now(EASTERN).date().isoformat()}.db")


def _backup_kaggle_version(path: str) -> int | None:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        row = conn.execute("SELECT version FROM kaggle_version LIMIT 1").fetchone()
    except sqlite3.Error:
        row = None
    finally:
        conn.close()
    return row[0] if row else None


def _ensure_backup() -> tuple[str, str]:
    """
    Today's backup: taken if it doesn't exist, or retaken if an ETL has loaded a version
    since (its kaggle_version differs), so restoring after a failed ETL puts back the
    tables that ETL started from. Returns (path, "OK" | "exists").
    """
    path = _todays_backup_path()
    if os.path.exists(path) and _backup_kaggle_version(path) == _stored_kaggle_version():
        return path, "exists"
    return backup_database(), "OK"


def _resolved_app_count() -> int:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        return conn.execute("SELECT COUNT(*) FROM predictions WHERE actual_result IS NOT NULL").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def _resolve_all(out: dict, app_before: int, tag: str) -> None:
    """App predictions (database) then paper predictions; counts into out. Never raises."""
    import database
    try:
        database.resolve_pending_predictions()
        out["app_resolved"] = _resolved_app_count() - app_before   # includes any run_etl resolved
    except Exception as exc:
        out["app_resolved"] = f"FAILED: {exc!r}"
        print(f"{tag} app predictions resolve FAILED: {exc!r}")
    try:
        import paper_predictions
        out["paper_resolved"] = paper_predictions.resolve_paper_predictions()
        out["resolve"] = out["paper_resolved"]
    except Exception as exc:
        out["paper_resolved"] = out["resolve"] = f"FAILED: {exc!r}"
        print(f"{tag} paper predictions resolve FAILED: {exc!r}")
    print(f"{tag} 5/5 resolved: {out['app_resolved']} app + {out['paper_resolved']} paper predictions")


def run_sync(scheduler=None, trigger: str = "manual", refresh_rosters: bool = True,
             lock_wait_minutes: float | None = None) -> dict:
    """
    _run_sync under proc_lock.ETL_LOCK (cross-process), so it never overlaps another
    sync, ETL or Kaggle retry, in this process or another. Waits up to
    lock_wait_minutes (default MAINTENANCE_LOCK_WAIT_MINUTES; 0 for Sync now), then
    returns {"status": "busy"}.
    """
    global _last_sync
    import proc_lock
    if lock_wait_minutes is None:
        lock_wait_minutes = MAINTENANCE_LOCK_WAIT_MINUTES
    if not proc_lock.ETL_LOCK.acquire(timeout=lock_wait_minutes * 60, who=f"sync ({trigger})"):
        summary = "another sync, ETL or maintenance is running; try again when it finishes"
        print(f"[sync] {trigger}: {summary}")
        return {"status": "busy", "trigger": trigger, "summary": summary, "skipped": "etl lock held"}
    try:
        out = _run_sync(scheduler, trigger, refresh_rosters)
    finally:
        proc_lock.ETL_LOCK.release()
    _last_sync = out
    return out


def _run_sync(scheduler, trigger: str, refresh_rosters: bool) -> dict:
    """
      1. back up predictions.db unless today's backup exists (_ensure_backup); abort on failure;
      2. refresh ESPN rosters and the player_match tables (a failure is logged, sync continues);
      3. ask Kaggle for the newest Ready version;
      4. if it is newer than the loaded one, _guarded_etl. If the pull window or a pre-tip
         job makes it wait, the Kaggle retry is scheduled for when the window ends. A 404
         (KaggleVersionUnavailable) changes no table and retries hourly. Any other failure
         restores ETL_TABLES from the step-1 backup; if that fails too, stop before step 5.
         Nothing newer: the hourly Kaggle retry runs while the server is up;
      5. resolve app predictions and paper predictions.
    One "[sync]" line per step and a summary line. Returns the outcome (also kept for
    last_sync()); "status" is loaded / up to date / waited / unavailable / check failed / failed.
    """
    import database
    tag = "[sync]"
    started = dt.datetime.now(EASTERN)
    today = started.date().isoformat()
    out: dict = {"trigger": trigger, "started_at": started.isoformat(timespec="seconds"),
                 "status": None, "kaggle_latest": None, "version_before": _stored_kaggle_version(),
                 "version_loaded": None, "rows_added": 0, "app_resolved": 0, "paper_resolved": 0,
                 "reason": None, "next_check": None}
    print(f"{tag} start ({trigger})")

    def finish(status: str, summary: str) -> dict:
        out["status"], out["summary"] = status, summary
        out["finished_at"] = dt.datetime.now(EASTERN).isoformat(timespec="seconds")
        print(f"{tag} {summary}")
        return out

    def retry(run_at=None):
        out["kaggle_retry"] = _schedule_kaggle_retry(scheduler, path, _stored_kaggle_version(),
                                                     today, attempt=1, run_at=run_at)
        out["next_check"] = out["kaggle_retry"]

    try:
        path, how = _ensure_backup()
        out["backup"] = path
        print(f"{tag} 1/5 backup {how}: {path} (keeping newest {BACKUP_KEEP})")
    except Exception as exc:
        out["backup"] = f"FAILED: {exc!r}"
        return finish("failed", f"backup FAILED: {exc!r}. Aborting: no ETL, no resolve.")

    if not refresh_rosters:
        out["rosters"] = "refreshed at startup"
        print(f"{tag} 2/5 rosters: refreshed at startup")
    else:
        try:
            import rosters
            out["rosters"] = rosters.refresh_rosters()
            print(f"{tag} 2/5 rosters refreshed: {out['rosters']}")
        except Exception as exc:
            out["rosters"] = f"FAILED: {exc!r}"
            print(f"{tag} 2/5 rosters refresh FAILED: {exc!r} (continuing)")
        else:
            try:   # app name matching tables follow the rosters (player_match); never blocks sync
                import player_match
                out["player_match"] = player_match.rebuild_now()
            except Exception as exc:
                out["player_match"] = f"FAILED: {exc!r}"
                print(f"{tag} player_match rebuild FAILED: {exc!r} (continuing)")

    app_before = _resolved_app_count()
    stored = out["version_before"]
    try:
        latest = database.get_latest_kaggle_version()
    except Exception as exc:
        out["etl"] = f"check failed: {exc!r}"
        print(f"{tag} 3/5 Kaggle version check FAILED: {exc!r}")
        retry()
        _resolve_all(out, app_before, tag)
        return finish("check failed", f"Kaggle version check FAILED; v{stored} kept, "
                      f"{out['app_resolved']} app + {out['paper_resolved']} paper predictions "
                      f"resolved; next check: {out['next_check']}")
    out["kaggle_latest"] = latest
    print(f"{tag} 3/5 Kaggle newest Ready v{latest}, loaded v{stored}")

    status = "up to date"
    if stored is not None and latest <= stored:
        out["etl"] = "up to date"
        print(f"{tag} 4/5 ETL not needed: v{stored} is current")
        retry()
    else:
        try:
            g = _guarded_etl(scheduler, tag, latest)
        except database.KaggleVersionUnavailable as exc:
            out["etl"] = f"skipped: newest Kaggle version not downloadable: {exc}"
            out["reason"] = f"v{latest} not downloadable yet"
            print(f"{tag} 4/5 ETL SKIPPED: v{latest} not downloadable ({exc}); tables untouched, retrying hourly")
            retry()
            status = "unavailable"
        except Exception as exc:
            out["etl"] = f"FAILED: {exc!r}"
            print(f"{tag} 4/5 ETL FAILED: {exc!r}")
            status = "failed"
            try:
                out["restored"] = restore_etl_tables(path)
                print(f"{tag} ETL FAILED, tables restored from {path}")
            except Exception as rexc:
                out["restored"] = f"FAILED: {rexc!r}"
                return finish("failed", f"ETL FAILED and table restore FAILED: {rexc!r}. "
                              f"player_stats/players/schedule may be partial; restore them from "
                              f"{path}. Skipping resolve.")
        else:
            out["rows_added"] = g["rows_added"]
            if g["etl"] == "waited":
                out["etl"] = f"waited: {g['reason']}"
                out["reason"] = g["reason"]
                retry(run_at=g["clear_at"])
                status = "waited"
            else:
                out["etl"] = g["etl"]
                out["version_loaded"] = _stored_kaggle_version() if g["etl"] == "ran" else None
                status = "loaded" if g["etl"] == "ran" else "up to date"
                if status == "loaded":
                    print(f"{tag} 4/5 ETL OK: v{out['version_loaded']} loaded, "
                          f"{out['rows_added']:,} player_stats rows added")
                else:                       # loaded by someone else between check and lock
                    print(f"{tag} 4/5 ETL not needed: v{_stored_kaggle_version()} already loaded")
                    retry()

    _resolve_all(out, app_before, tag)
    resolved = f"{out['app_resolved']} app + {out['paper_resolved']} paper predictions resolved"
    if status == "loaded":
        return finish(status, f"Kaggle v{out['version_loaded']} loaded, {out['rows_added']:,} rows, {resolved}")
    if status == "up to date":
        return finish(status, f"Kaggle v{latest} is the newest Ready version, already loaded; "
                      f"{resolved}; next check: {out['next_check']}")
    if status == "waited":
        return finish(status, f"Kaggle v{latest} ready but the ETL waited ({out['reason']}); "
                      f"{resolved}; ETL retry: {out['next_check']}")
    if status == "unavailable":
        return finish(status, f"Kaggle v{latest} listed but not downloadable yet; v{stored} kept, "
                      f"{resolved}; next check: {out['next_check']}")
    return finish(status, f"Kaggle v{latest} ETL FAILED ({out['etl']}); tables restored from "
                  f"{path}; {resolved}")


def run_morning_maintenance(scheduler=None) -> dict:
    """The daily MAINTENANCE_HOUR_ET job: run_sync (backup, rosters, Kaggle, ETL, resolve)."""
    return run_sync(scheduler, trigger="maintenance")


def start_sync_in_background(trigger: str = "startup", refresh_rosters: bool = False) -> threading.Thread:
    """run_sync in a daemon thread (startup: rosters were just refreshed). Logs any crash."""
    def target():
        import traceback
        try:
            run_sync(_scheduler, trigger=trigger, refresh_rosters=refresh_rosters)
        except Exception:
            print(f"[sync] {trigger} sync crashed:")
            traceback.print_exc()

    t = threading.Thread(target=target, name=f"sync-{trigger}", daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Kaggle version retry: hourly while nothing newer is out, same guards
# ---------------------------------------------------------------------------

def _stored_kaggle_version() -> int | None:
    """The Kaggle version run_etl last loaded, or None if there is no kaggle_version table."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        row = conn.execute("SELECT version FROM kaggle_version LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return row[0] if row else None


def _schedule_kaggle_retry(scheduler, backup_path: str, baseline: int | None, day: str,
                           attempt: int, run_at: dt.datetime | None = None) -> str:
    """
    Schedule check `attempt` at run_at (default KAGGLE_RETRY_MINUTES from now), pushed
    to the end of any scheduled pull's window. `day` is the ET date the series started;
    it keeps going past midnight while the server is up (the next sync replaces it).
    """
    if scheduler is None:
        return "no scheduler, no retry"
    run_at = _clear_of_pulls(scheduler, run_at or dt.datetime.now(EASTERN)
                             + dt.timedelta(minutes=KAGGLE_RETRY_MINUTES))
    scheduler.add_job(
        run_kaggle_version_retry, "date", run_date=run_at,
        kwargs={"scheduler": scheduler, "backup_path": backup_path, "baseline": baseline,
                "day": day, "attempt": attempt},
        id="kaggle_version_retry", replace_existing=True, max_instances=1,
        misfire_grace_time=30 * 60,
    )
    return f"check {attempt} at {run_at:%H:%M} ET"


def run_kaggle_version_retry(scheduler=None, backup_path: str = "", baseline: int | None = None,
                             day: str = "", attempt: int = 1) -> str:
    """
    _run_kaggle_version_retry under proc_lock.ETL_LOCK. If another ETL or sync holds
    it, this check is skipped (one log line) and the next one is scheduled.
    """
    import proc_lock
    if not proc_lock.ETL_LOCK.acquire(timeout=0, who=f"kaggle-retry check {attempt}"):
        print(f"[kaggle-retry] check {attempt}: another ETL or sync is running; "
              f"next check in {KAGGLE_RETRY_MINUTES} min")
        _schedule_kaggle_retry(scheduler, backup_path, baseline, day, attempt + 1)
        return "deferred: etl lock"
    try:
        return _run_kaggle_version_retry(scheduler, backup_path, baseline, day, attempt)
    finally:
        proc_lock.ETL_LOCK.release()


def _run_kaggle_version_retry(scheduler=None, backup_path: str = "", baseline: int | None = None,
                              day: str = "", attempt: int = 1) -> str:
    """
    One cheap version check (metadata only). If Kaggle has a newer version than
    `baseline` (the version loaded when the sync found nothing new), _guarded_etl then
    resolve app and paper predictions. Stops (schedules nothing more) only when a
    version newer than `baseline` is already loaded (by this job or anything else) or
    the ETL fails; otherwise the next check is scheduled. An ETL that has to wait (pull
    window, running pre-tip job) is moved to the end of that window, not dropped.
    An ETL failure restores ETL_TABLES from the backup at `backup_path`, which still
    matches them because nothing was loaded since. Each check logs one line.
    """
    now = dt.datetime.now(EASTERN)
    tag = f"[kaggle-retry] check {attempt} @ {now:%H:%M} ET:"
    stored = _stored_kaggle_version()
    if stored != baseline:
        print(f"{tag} v{stored} already loaded (was v{baseline}); stopping")
        return "stopped: already loaded"

    import database
    try:
        latest = database.get_latest_kaggle_version()
    except Exception as exc:
        print(f"{tag} version check FAILED ({exc!r}); next check in {KAGGLE_RETRY_MINUTES} min")
        _schedule_kaggle_retry(scheduler, backup_path, baseline, day, attempt + 1)
        return "check failed"
    if stored is not None and latest <= stored:
        print(f"{tag} Kaggle v{latest}, loaded v{stored}: nothing new; next check in {KAGGLE_RETRY_MINUTES} min")
        _schedule_kaggle_retry(scheduler, backup_path, baseline, day, attempt + 1)
        return "up to date"

    print(f"{tag} Kaggle v{latest} > loaded v{stored}; running ETL + resolve")
    app_before = _resolved_app_count()
    try:
        g = _guarded_etl(scheduler, tag, latest)
    except database.KaggleVersionUnavailable as exc:
        print(f"[kaggle-retry] v{latest} not downloadable yet ({exc}); tables untouched, "
              f"next check in {KAGGLE_RETRY_MINUTES} min")
        _schedule_kaggle_retry(scheduler, backup_path, baseline, day, attempt + 1)
        return "unavailable"
    except Exception as exc:
        print(f"[kaggle-retry] ETL FAILED: {exc!r}; no more checks")
        try:
            restore_etl_tables(backup_path)
            print(f"[kaggle-retry] tables restored from {backup_path}")
        except Exception as rexc:
            print(f"[kaggle-retry] table restore FAILED: {rexc!r}. "
                  f"player_stats/players/schedule may be partial; restore them from {backup_path}.")
        return "etl failed"
    if g["etl"] == "waited":
        _schedule_kaggle_retry(scheduler, backup_path, baseline, day, attempt, run_at=g["clear_at"])
        return "deferred: pull window"
    if g["etl"] == "up to date":
        print(f"{tag} v{_stored_kaggle_version()} already loaded; stopping")
        return "stopped: already loaded"

    out: dict = {}
    _resolve_all(out, app_before, "[kaggle-retry]")
    print(f"[kaggle-retry] Kaggle v{_stored_kaggle_version()} loaded, {g['rows_added']:,} rows, "
          f"{out['app_resolved']} app + {out['paper_resolved']} paper predictions resolved")
    return "loaded"


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
    # Pre-tip pulls are always scheduled by a free daily events call, independent of
    # the open pull (which also schedules them, under the same job ids).
    sched.add_job(
        schedule_todays_pretips, "cron", hour=SCHEDULE_HOUR_ET, minute=0,
        kwargs={"scheduler": sched},
        id="line_pretip_scheduler", replace_existing=True, misfire_grace_time=3 * 3600,
        coalesce=True,
    )
    # Restart catch-up, any hour: today's pre-tip jobs live only in memory.
    # run_pretip_pull skips games already pulled, so this never double-spends.
    sched.add_job(schedule_todays_pretips, kwargs={"scheduler": sched},
                  id="line_pretip_scheduler_catchup", replace_existing=True)
    if OPEN_PULL_ENABLED:
        sched.add_job(
            run_open_pull, "cron", hour=OPEN_PULL_HOUR_ET, minute=0,
            kwargs={"phase": "open", "scheduler": sched},
            id="line_open_pull", replace_existing=True,
            misfire_grace_time=OPEN_CATCHUP_HOURS * 3600, coalesce=True,
        )
        # Restart catch-up, only within the misfire window: a later "open" pull would
        # not be a morning snapshot. The pull log makes this a no-op for games pulled.
        if OPEN_PULL_HOUR_ET <= dt.datetime.now(EASTERN).hour < OPEN_PULL_HOUR_ET + OPEN_CATCHUP_HOURS:
            sched.add_job(run_open_pull, kwargs={"phase": "open", "scheduler": sched},
                          id="line_open_catchup", replace_existing=True)
    # Daily sync (backup -> rosters -> Kaggle -> ETL under the guards -> resolve).
    # 12 h grace: a machine asleep at 07:00 still runs it on waking. Startup runs its
    # own sync as soon as startup completes (main.py -> start_sync_in_background).
    sched.add_job(
        run_morning_maintenance, "cron", hour=MAINTENANCE_HOUR_ET, minute=0,
        kwargs={"scheduler": sched},
        id="morning_maintenance", replace_existing=True, misfire_grace_time=12 * 3600,
        coalesce=True, max_instances=1,
    )
    sched.start()
    _scheduler = sched
    print(f"[line_logger] scheduler started: open pull {'ON ' + str(OPEN_PULL_HOUR_ET) + ':00 ET' if OPEN_PULL_ENABLED else 'OFF'}, "
          f"pre-tip scheduling {SCHEDULE_HOUR_ET}:00 ET, "
          f"pretip T-{PRETIP_MINUTES}m, markets={MARKETS} (max {CREDITS_PER_PULL} credits/pull), "
          f"cap {MONTHLY_CREDIT_CAP}/month, sync on startup + {MAINTENANCE_HOUR_ET}:00 ET")
    _warn_if_cap_too_low()
    return sched


def monthly_projection() -> dict:
    """
    Scheduled credits per month at the maximum cost, from the `schedule` table
    (games per month, same game types as _last_season_daily_rate). No API call.
    {"YYYY-MM": (games, credits)}
    """
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT substr(gameDateTimeEst,1,7), COUNT(*) FROM schedule
            WHERE substr(printf('%010d', gameId), 3, 1) IN ('2','4','5','6')
            GROUP BY 1 ORDER BY 1""").fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    per_game = SCHEDULED_PULLS_PER_GAME * CREDITS_PER_PULL
    return {m: (n, n * per_game) for m, n in rows}


def _warn_if_cap_too_low() -> None:
    proj = monthly_projection()
    if not proj:
        return
    month, (games, credits) = max(proj.items(), key=lambda kv: kv[1][1])
    if credits > MONTHLY_CREDIT_CAP:
        print("!" * 78)
        print(f"[line_logger] WARNING: peak month {month} projects {credits} credits "
              f"({games} games x {SCHEDULED_PULLS_PER_GAME} pulls x {CREDITS_PER_PULL}) "
              f"but ODDS_MONTHLY_CREDIT_CAP is {MONTHLY_CREDIT_CAP}. Pulls will be refused "
              f"once the cap is reached; set ODDS_MONTHLY_CREDIT_CAP in .env.")
        print("!" * 78)


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
    print(f"projection ({SCHEDULED_PULLS_PER_GAME} pulls/game x max {CREDITS_PER_PULL} credits):")
    for m, (games, credits) in monthly_projection().items():
        print(f"  {m}: {games} games -> {credits} credits")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pull-now", action="store_true", help="one manual pull of today's slate")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    if args.pull_now:
        run_open_pull(phase="manual")
    if args.status or not args.pull_now:
        _status()
