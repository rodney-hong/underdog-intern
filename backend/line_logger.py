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
MAINTENANCE_HOUR_ET  = 7   # backup -> ETL -> resolve (run_morning_maintenance)
ETL_PRETIP_GUARD_MINUTES = 60  # skip the ETL if a pre-tip pull is due this soon
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
# Morning maintenance: backup -> ETL (unless a pre-tip pull is near) -> resolve
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


def _pretips_due_within(scheduler, minutes: int) -> list[str]:
    """Pre-tip (and pre-tip retry) jobs scheduled to run within `minutes` (or overdue)."""
    if scheduler is None:
        return []
    horizon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)
    return [f"{j.id} @ {j.next_run_time:%H:%M %Z}" for j in scheduler.get_jobs()
            if j.id.startswith(("pretip:", "pretip-retry:"))
            and j.next_run_time is not None and j.next_run_time <= horizon]


def run_morning_maintenance(scheduler=None) -> dict:
    """
    Daily at MAINTENANCE_HOUR_ET:
      1. back up predictions.db (abort the whole job if that fails);
      1b. refresh ESPN rosters (rosters.refresh_rosters; a failure is logged and the
         job continues: player teams then fall back to box scores once rows age out);
      2. run database.run_etl(), unless a pre-tip pull is scheduled within
         ETL_PRETIP_GUARD_MINUTES (the ETL replaces player_stats/players/schedule
         in place, and a pre-tip paper prediction would read the partial tables).
         If it raises, ETL_TABLES are restored from the step-1 backup (nothing else);
         if that restore fails too, the job stops before step 3;
      3. run paper_predictions.resolve_paper_predictions().
    Each step's outcome is printed. Returns them as a dict.
    """
    out: dict = {}
    print("[maintenance] start")
    try:
        path = backup_database()
        out["backup"] = path
        print(f"[maintenance] 1/3 backup OK: {path} (keeping newest {BACKUP_KEEP})")
    except Exception as exc:
        out["backup"] = f"FAILED: {exc!r}"
        print(f"[maintenance] 1/3 backup FAILED: {exc!r}. Aborting: no ETL, no resolve.")
        return out

    try:
        import rosters
        out["rosters"] = rosters.refresh_rosters()
        print(f"[maintenance] rosters refreshed: {out['rosters']}")
    except Exception as exc:
        out["rosters"] = f"FAILED: {exc!r}"
        print(f"[maintenance] rosters refresh FAILED: {exc!r} (continuing)")

    due = _pretips_due_within(scheduler, ETL_PRETIP_GUARD_MINUTES)
    if due:
        out["etl"] = f"skipped: pre-tip pull(s) within {ETL_PRETIP_GUARD_MINUTES} min: {due}"
        print(f"[maintenance] 2/3 ETL SKIPPED: pre-tip pull(s) within "
              f"{ETL_PRETIP_GUARD_MINUTES} min: {due}")
    else:
        try:
            import database
            ran = database.run_etl()
            out["etl"] = "ran" if ran else "up to date"
            print(f"[maintenance] 2/3 ETL OK: {'new Kaggle version loaded' if ran else 'dataset already up to date'}")
        except Exception as exc:
            out["etl"] = f"FAILED: {exc!r}"
            print(f"[maintenance] 2/3 ETL FAILED: {exc!r}")
            try:
                out["restored"] = restore_etl_tables(path)
                print(f"[maintenance] ETL FAILED, tables restored from {path}")
            except Exception as rexc:
                out["restored"] = f"FAILED: {rexc!r}"
                print(f"[maintenance] ETL FAILED and table restore FAILED: {rexc!r}. "
                      f"player_stats/players/schedule may be partial; restore them from {path}. "
                      f"Skipping resolve.")
                print("[maintenance] done")
                return out

    try:
        import paper_predictions
        n = paper_predictions.resolve_paper_predictions()
        out["resolve"] = n
        print(f"[maintenance] 3/3 paper predictions resolved: {n}")
    except Exception as exc:
        out["resolve"] = f"FAILED: {exc!r}"
        print(f"[maintenance] 3/3 resolve FAILED: {exc!r}")
    print("[maintenance] done")
    return out


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
    # Morning maintenance (backup -> ETL unless a pre-tip pull is near -> resolve).
    sched.add_job(
        run_morning_maintenance, "cron", hour=MAINTENANCE_HOUR_ET, minute=0,
        kwargs={"scheduler": sched},
        id="morning_maintenance", replace_existing=True, misfire_grace_time=2 * 3600,
        coalesce=True, max_instances=1,
    )
    sched.start()
    _scheduler = sched
    print(f"[line_logger] scheduler started: open pull {'ON ' + str(OPEN_PULL_HOUR_ET) + ':00 ET' if OPEN_PULL_ENABLED else 'OFF'}, "
          f"pre-tip scheduling {SCHEDULE_HOUR_ET}:00 ET, "
          f"pretip T-{PRETIP_MINUTES}m, markets={MARKETS} (max {CREDITS_PER_PULL} credits/pull), "
          f"cap {MONTHLY_CREDIT_CAP}/month, maintenance {MAINTENANCE_HOUR_ET}:00 ET")
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
