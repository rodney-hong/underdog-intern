"""
backfill.py
Historical OddsAPI lines for past NBA games (Test A, experiments/preregistration.md).
Run by hand from the backend folder:

    python backfill.py --dry-run --max-credits 15000
    python backfill.py --max-credits 202 --game-ids 22500580,22300571 \\
        --markets player_points,...,player_steals,player_blocks

One snapshot per game, at T-30 minutes before the event's commence_time (OddsAPI
returns the closest snapshot at or before that time), with the live logger's 10 named
bookmakers (one region-equivalent) and includeMultipliers=true. Games come from
player_stats (counted game types only, game_types.py), newest first. Event ids and
commence times come from one historical events call per ET date (1 credit), cached in
backfill_log so a resumed run doesn't pay for it again.

Writes only two tables, historical_snapshots (phase = 'backfill') and backfill_log, in
their own file, HIST_DB_PATH (backend/historical.db, WAL mode). predictions.db is only
read (player_stats, schedule), on a read-only connection. Nothing is written to
odds_pull_log, line_snapshots or paper_predictions, so the live logger's
ODDS_MONTHLY_CREDIT_CAP never sees these credits; the account-wide floor below is what
protects the live slate.

Game <-> event match: a Kaggle game matches the one OddsAPI event whose commence_time,
converted to ET, falls on the game's ET date and whose home and away team nicknames
equal the game's (match_event). A 22:30 ET tip has a next-day UTC commence_time and
still matches its ET date.

Analysis reads both files from one connection, the box scores read-only:

    # uri=True is required: without it the ATTACH string is taken as a file name.
    conn = sqlite3.connect("file:backend/historical.db?mode=ro", uri=True)
    conn.execute("ATTACH DATABASE 'file:backend/predictions.db?mode=ro' AS p")
    conn.execute('''
        SELECT h.*, s.points, s.numMinutes
        FROM historical_snapshots h
        JOIN p.player_stats s ON s.gameId = h.kaggle_game_id
         AND s.firstName || ' ' || s.lastName = h.player_name
        WHERE h.market_key = 'player_points' AND h.bookmaker IN ('fanduel', 'draftkings')''')

Key: ODDS_API_KEY, the paid plan the live logger uses. ODDS_API_HISTORICAL_KEY is
never read.

Checked before every paid call, each stopping the run:
  - per-run budget: credits spent this run + the call's maximum cost <= --max-credits;
  - floor: x-requests-remaining (from the previous response) - the call's maximum cost
    >= live games until the next quota reset x 16 + FLOOR_BUFFER. The reset is assumed
    to be the 20th (the plan started on 2026-10-20), the worse case if it is the 1st;
  - live guard: no live pre-tip pull due within LIVE_GUARD_MINUTES (from `schedule`).
The first remaining value comes from the live events endpoint, which costs 0 credits.

Resumable: a game with an 'ok' or 'empty' event_odds row in backfill_log is skipped.
Each game's snapshot rows and its log row are committed together.
"""

import argparse
import datetime as dt
import json
import os
import sqlite3

import httpx
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import game_types
import line_logger
from database import DB_PATH, _nba_season

HIST_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "historical.db")
API_KEY = os.getenv("ODDS_API_KEY", "")   # paid plan; never ODDS_API_HISTORICAL_KEY
_BASE = "https://api.the-odds-api.com/v4"
LIVE_EVENTS_URL = f"{_BASE}/sports/basketball_nba/events"   # 0 credits
HIST_EVENTS_URL = f"{_BASE}/historical/sports/basketball_nba/events"
HIST_EVENT_ODDS_URL = f"{_BASE}/historical/sports/basketball_nba/events/{{event_id}}/odds"

BOOKMAKERS = line_logger.BOOKMAKERS          # the same 10 books as live: 1 region-equivalent
DEFAULT_MARKETS = line_logger.MARKETS        # the 8 live markets
HIST_COST_PER_MARKET = 10                    # 10 x markets returned x regions
EVENTS_CALL_COST = 1                         # historical events list; 0 if no events
PROPS_AVAILABLE_FROM = dt.datetime(2023, 5, 3, 5, 30, tzinfo=dt.timezone.utc)
SNAPSHOT_MINUTES_BEFORE_TIP = 30
DEFAULT_SEASONS = ("2025-26", "2024-25", "2023-24")

FLOOR_BUFFER = 1500
QUOTA_RESET_DAY = 20
LIVE_CREDITS_PER_GAME = line_logger.CREDITS_PER_PULL * line_logger.SCHEDULED_PULLS_PER_GAME  # 16
LIVE_GUARD_MINUTES = 45
STOP_HTTP = (401, 403, 429)                  # key, plan or rate-limit problems: stop the run

EASTERN = line_logger.EASTERN
PHASE = "backfill"


class StopBackfill(Exception):
    """A guard refused the next call; the run ends cleanly."""


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    """historical.db, the only file this module writes (WAL, as predictions.db)."""
    conn = sqlite3.connect(HIST_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def get_source_db() -> sqlite3.Connection:
    """predictions.db, read-only: player_stats and schedule."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_tables(conn) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS historical_snapshots (
            event_id           TEXT NOT NULL,
            kaggle_game_id     INTEGER,
            game_date          TEXT NOT NULL,     -- ET date of commence_time
            commence_time      TEXT NOT NULL,     -- UTC, from OddsAPI
            home_team          TEXT,
            away_team          TEXT,
            snapshot_requested TEXT NOT NULL,     -- the date parameter sent (T-30)
            snapshot_ts        TEXT NOT NULL,     -- the snapshot OddsAPI returned
            player_name        TEXT NOT NULL,
            market_key         TEXT NOT NULL,
            bookmaker          TEXT NOT NULL,
            side               TEXT NOT NULL,     -- Over / Under
            point              REAL,
            price              REAL,
            multiplier         REAL,              -- includeMultipliers (DFS books), if present
            book_last_update   TEXT,
            outcome_index      INTEGER NOT NULL,  -- position in its market
            extra_json         TEXT,              -- any other outcome fields, verbatim
            phase              TEXT NOT NULL,
            captured_at        TEXT NOT NULL,     -- UTC time of the call
            PRIMARY KEY (event_id, snapshot_ts, bookmaker, market_key, outcome_index)
        );
        CREATE TABLE IF NOT EXISTS backfill_log (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            called_at          TEXT NOT NULL,     -- UTC
            endpoint           TEXT NOT NULL,     -- live_events / events / event_odds / guard
            event_id           TEXT,
            kaggle_game_id     INTEGER,
            game_date          TEXT,              -- ET
            snapshot_requested TEXT,
            snapshot_ts        TEXT,
            markets_requested  TEXT,
            markets_returned   TEXT,
            books_returned     TEXT,
            http_status        INTEGER,
            credits_used       INTEGER,
            remaining_after    INTEGER,
            rows_inserted      INTEGER,
            status             TEXT,              -- ok / empty / unmatched / error / stopped
            events_json        TEXT,              -- events calls: the day's events, for resume
            error              TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_backfill_log_game ON backfill_log (kaggle_game_id);
        CREATE INDEX IF NOT EXISTS idx_backfill_log_date ON backfill_log (endpoint, game_date);
    """)
    conn.commit()


def _log(conn, **fields) -> None:
    fields.setdefault("called_at", dt.datetime.now(dt.timezone.utc).isoformat())
    cols = ", ".join(fields)
    conn.execute(f"INSERT INTO backfill_log ({cols}) VALUES ({', '.join('?' * len(fields))})",
                 tuple(fields.values()))


# ---------------------------------------------------------------------------
# Planning (no API calls)
# ---------------------------------------------------------------------------

def _nickname(team: str) -> str:
    return "" if not team else ("Trail Blazers" if "Blazers" in team else team.split()[-1])


def planned_games(conn, seasons=DEFAULT_SEASONS, game_ids=None, now=None) -> list[dict]:
    """
    Counted games from player_stats, newest first: ET tip time, home/away nicknames,
    season. Only finished games in `seasons` (or exactly `game_ids`) dated after
    player props became available historically. Already-done games are not removed here.
    """
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(EASTERN)
    rows = conn.execute(f"""
        SELECT gameId, MIN(gameDateTimeEst) AS tip,
               MAX(CASE WHEN home = 1 THEN playerteamName END)   AS home,
               MAX(CASE WHEN home = 1 THEN opponentteamName END) AS away
        FROM player_stats
        WHERE gameDateTimeEst >= ? AND gameDateTimeEst < ?
          AND gameType IN ({game_types.COUNTED_SQL})
        GROUP BY gameId
        ORDER BY tip DESC, gameId DESC
    """, (PROPS_AVAILABLE_FROM.astimezone(EASTERN).strftime("%Y-%m-%d %H:%M:%S"),
          now.strftime("%Y-%m-%d"))).fetchall()
    out = []
    for r in rows:
        if not (r["tip"] and r["home"] and r["away"]):
            continue
        d = dt.date.fromisoformat(r["tip"][:10])
        if game_ids is not None:
            if r["gameId"] not in game_ids:
                continue
        elif _nba_season(d) not in seasons:
            continue
        out.append({"kaggle_game_id": r["gameId"], "tip_et": r["tip"], "game_date": r["tip"][:10],
                    "home": r["home"], "away": r["away"], "season": _nba_season(d)})
    return out


def done_game_ids(conn) -> set:
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT kaggle_game_id FROM backfill_log WHERE endpoint = 'event_odds' "
        "AND status IN ('ok', 'empty') AND kaggle_game_id IS NOT NULL")}


def cached_events(conn, game_date: str) -> list[dict] | None:
    row = conn.execute(
        "SELECT events_json FROM backfill_log WHERE endpoint = 'events' AND game_date = ? "
        "AND status = 'ok' ORDER BY id DESC LIMIT 1", (game_date,)).fetchone()
    return json.loads(row[0]) if row else None


def _table_exists(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (name,)).fetchone() is not None


def next_quota_reset(now_et: dt.datetime) -> dt.datetime:
    """The next QUOTA_RESET_DAY, 00:00 ET, after now_et."""
    reset = now_et.replace(day=QUOTA_RESET_DAY, hour=0, minute=0, second=0, microsecond=0)
    if now_et >= reset:
        reset = (reset + dt.timedelta(days=32)).replace(day=QUOTA_RESET_DAY)
    return reset


def credit_floor(conn, now=None) -> dict:
    """Live games from now to the next quota reset x LIVE_CREDITS_PER_GAME + FLOOR_BUFFER."""
    now_et = (now or dt.datetime.now(dt.timezone.utc)).astimezone(EASTERN)
    reset = next_quota_reset(now_et)
    games = conn.execute(
        "SELECT COUNT(*) FROM schedule WHERE gameDateTimeEst >= ? AND gameDateTimeEst < ?",
        (now_et.strftime("%Y-%m-%d %H:%M:%S"), reset.strftime("%Y-%m-%d %H:%M:%S"))).fetchone()[0]
    return {"floor": games * LIVE_CREDITS_PER_GAME + FLOOR_BUFFER, "live_games": games,
            "reset": reset.strftime("%Y-%m-%d")}


def live_pretips_near(conn, now=None, minutes: int = LIVE_GUARD_MINUTES) -> list[str]:
    """
    Live pre-tip pulls (tip - PRETIP_MINUTES, plus the retry PRETIP_RETRY_MINUTES later)
    due within `minutes` of now, from the `schedule` table (ET wall time).
    """
    now_et = (now or dt.datetime.now(dt.timezone.utc)).astimezone(EASTERN)
    lead = line_logger.PRETIP_MINUTES
    lo = now_et + dt.timedelta(minutes=lead - line_logger.PRETIP_RETRY_MINUTES - 1)
    hi = now_et + dt.timedelta(minutes=lead + minutes)
    rows = conn.execute(
        "SELECT gameId, gameDateTimeEst FROM schedule WHERE gameDateTimeEst BETWEEN ? AND ? "
        "ORDER BY gameDateTimeEst",
        (lo.strftime("%Y-%m-%d %H:%M:%S"), hi.strftime("%Y-%m-%d %H:%M:%S"))).fetchall()
    return [f"{gid} tips {tip[11:16]} ET" for gid, tip in rows]


def max_odds_cost(markets: list[str]) -> int:
    return HIST_COST_PER_MARKET * len(markets)


# ---------------------------------------------------------------------------
# Parsing (pure)
# ---------------------------------------------------------------------------

_OUTCOME_FIELDS = {"name", "description", "price", "point", "multiplier"}


def _et_date(commence_time: str) -> str:
    t = dt.datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
    return t.astimezone(EASTERN).date().isoformat()


def match_event(game: dict, events: list[dict]) -> dict | None:
    """The OddsAPI event with the game's ET date and home/away nicknames, if exactly one."""
    hits = [e for e in events
            if _et_date(e["commence_time"]) == game["game_date"]
            and _nickname(e.get("home_team")) == game["home"]
            and _nickname(e.get("away_team")) == game["away"]]
    return hits[0] if len(hits) == 1 else None


def snapshot_time(commence_time: str) -> str:
    t = dt.datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
    t -= dt.timedelta(minutes=SNAPSHOT_MINUTES_BEFORE_TIP)
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_snapshot(event: dict, game: dict, body: dict, requested: str, captured_at: str) -> list[tuple]:
    """historical_snapshots rows from one historical event-odds response. No I/O."""
    data = body.get("data") or {}
    snap_ts = body.get("timestamp") or requested
    rows = []
    for book in data.get("bookmakers", []):
        for market in book.get("markets", []):
            last_update = market.get("last_update") or book.get("last_update")
            for idx, o in enumerate(market.get("outcomes", [])):
                player, side = o.get("description"), o.get("name")
                if not player or side not in ("Over", "Under"):
                    continue
                extra = {k: v for k, v in o.items() if k not in _OUTCOME_FIELDS}
                rows.append((
                    event["id"], game["kaggle_game_id"], game["game_date"], event["commence_time"],
                    event.get("home_team"), event.get("away_team"), requested, snap_ts,
                    player, market["key"], book["key"], side, o.get("point"), o.get("price"),
                    o.get("multiplier"), last_update, idx,
                    json.dumps(extra) if extra else None, PHASE, captured_at,
                ))
    return rows


_INSERT_SQL = """
    INSERT OR IGNORE INTO historical_snapshots
    (event_id, kaggle_game_id, game_date, commence_time, home_team, away_team,
     snapshot_requested, snapshot_ts, player_name, market_key, bookmaker, side, point, price,
     multiplier, book_last_update, outcome_index, extra_json, phase, captured_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


# ---------------------------------------------------------------------------
# Calls, with the guards
# ---------------------------------------------------------------------------

def _int_header(resp: httpx.Response, name: str) -> int | None:
    val = resp.headers.get(name)
    try:
        return int(float(val)) if val is not None else None
    except ValueError:
        return None


class Run:
    """conn: historical.db (written). src: predictions.db (read-only: schedule)."""

    def __init__(self, conn, src, client: httpx.Client, markets: list[str], max_credits: int, now_fn):
        self.conn, self.src, self.client, self.markets = conn, src, client, markets
        self.max_credits, self.now_fn = max_credits, now_fn
        self.spent = 0
        self.remaining: int | None = None

    def guard(self, cost: int) -> None:
        """Raise StopBackfill unless a call costing up to `cost` credits is allowed now."""
        if self.spent + cost > self.max_credits:
            raise StopBackfill(f"per-run budget: spent {self.spent} + up to {cost} "
                               f"> --max-credits {self.max_credits}")
        if self.remaining is None:
            raise StopBackfill("no x-requests-remaining header seen; cannot check the floor")
        fl = credit_floor(self.src, self.now_fn())
        if self.remaining - cost < fl["floor"]:
            raise StopBackfill(
                f"floor: remaining {self.remaining} - up to {cost} < floor {fl['floor']} "
                f"({fl['live_games']} live games to {fl['reset']} x {LIVE_CREDITS_PER_GAME} "
                f"+ {FLOOR_BUFFER})")
        near = live_pretips_near(self.src, self.now_fn())
        if near:
            raise StopBackfill(f"live pre-tip pull due within {LIVE_GUARD_MINUTES} min: {near}")

    def _account(self, resp: httpx.Response, cost_if_unknown: int) -> int:
        """Credits this response cost; updates remaining and spent."""
        remaining_after = _int_header(resp, "x-requests-remaining")
        last = _int_header(resp, "x-requests-last")
        if last is not None:
            credits = last
        elif self.remaining is not None and remaining_after is not None:
            credits = self.remaining - remaining_after
        else:
            credits = cost_if_unknown                 # never undercount
        if remaining_after is not None:
            self.remaining = remaining_after
        self.spent += credits
        return credits

    def read_remaining(self) -> None:
        """Free live events call (0 credits) for the first x-requests-remaining."""
        resp = self.client.get(LIVE_EVENTS_URL, params={"apiKey": API_KEY})
        credits = self._account(resp, 0)
        _log(self.conn, endpoint="live_events", http_status=resp.status_code,
             credits_used=credits, remaining_after=self.remaining,
             status="ok" if resp.status_code == 200 else "error",
             error=None if resp.status_code == 200 else resp.text[:300])
        self.conn.commit()
        print(f"[backfill] remaining {self.remaining} (live events call, {credits} credits)")
        if resp.status_code in STOP_HTTP:
            raise StopBackfill(f"live events HTTP {resp.status_code}: {resp.text[:200]}")

    def events_for(self, game_date: str) -> list[dict] | None:
        """The day's OddsAPI events, from the cache or one historical events call."""
        cached = cached_events(self.conn, game_date)
        if cached is not None:
            return cached
        self.guard(EVENTS_CALL_COST)
        # 06:00 ET on game_date: before any tip, so every game that day is still listed.
        d = dt.date.fromisoformat(game_date)
        at = dt.datetime(d.year, d.month, d.day, 6, tzinfo=EASTERN).astimezone(dt.timezone.utc)
        params = {"apiKey": API_KEY, "date": at.strftime("%Y-%m-%dT%H:%M:%SZ")}
        resp = self.client.get(HIST_EVENTS_URL, params=params)
        credits = self._account(resp, EVENTS_CALL_COST)
        ok = resp.status_code == 200
        events = [e for e in (resp.json().get("data") or []) if _et_date(e["commence_time"]) == game_date] if ok else None
        _log(self.conn, endpoint="events", game_date=game_date, snapshot_requested=params["date"],
             snapshot_ts=resp.json().get("timestamp") if ok else None,
             http_status=resp.status_code, credits_used=credits, remaining_after=self.remaining,
             status="ok" if ok else "error", events_json=json.dumps(events) if ok else None,
             error=None if ok else resp.text[:300])
        self.conn.commit()
        print(f"[backfill] events {game_date}: {len(events) if ok else 'HTTP ' + str(resp.status_code)}"
              f", credits {credits}, remaining {self.remaining}")
        if resp.status_code in STOP_HTTP:
            raise StopBackfill(f"events HTTP {resp.status_code}: {resp.text[:200]}")
        return events

    def pull_game(self, game: dict, event: dict) -> None:
        cost = max_odds_cost(self.markets)
        self.guard(cost)
        requested = snapshot_time(event["commence_time"])
        params = {"apiKey": API_KEY, "date": requested, "bookmakers": BOOKMAKERS,
                  "markets": ",".join(self.markets), "oddsFormat": "american",
                  "includeMultipliers": "true"}
        captured_at = dt.datetime.now(dt.timezone.utc).isoformat()
        resp = self.client.get(HIST_EVENT_ODDS_URL.format(event_id=event["id"]), params=params)
        credits = self._account(resp, cost)
        base = dict(endpoint="event_odds", event_id=event["id"], kaggle_game_id=game["kaggle_game_id"],
                    game_date=game["game_date"], snapshot_requested=requested,
                    markets_requested=params["markets"], http_status=resp.status_code,
                    credits_used=credits, remaining_after=self.remaining)
        if resp.status_code != 200:
            _log(self.conn, **base, status="error", error=resp.text[:300])
            self.conn.commit()
            print(f"[backfill] {game['kaggle_game_id']} HTTP {resp.status_code}, credits {credits}")
            if resp.status_code in STOP_HTTP:
                raise StopBackfill(f"event odds HTTP {resp.status_code}: {resp.text[:200]}")
            return
        body = resp.json()
        rows = parse_snapshot(event, game, body, requested, captured_at)
        books = sorted({b["key"] for b in (body.get("data") or {}).get("bookmakers", [])})
        markets = sorted({m["key"] for b in (body.get("data") or {}).get("bookmakers", [])
                          for m in b.get("markets", [])})
        before = self.conn.total_changes
        self.conn.executemany(_INSERT_SQL, rows)
        inserted = self.conn.total_changes - before
        _log(self.conn, **base, snapshot_ts=body.get("timestamp"), markets_returned=",".join(markets),
             books_returned=",".join(books), rows_inserted=inserted,
             status="ok" if rows else "empty")
        self.conn.commit()                          # the game's rows and its log row together
        print(f"[backfill] {game['game_date']} {game['away']} @ {game['home']} "
              f"({game['kaggle_game_id']}): {inserted} rows, books=[{','.join(books)}], "
              f"markets={len(markets)}, credits {credits}, remaining {self.remaining}, "
              f"run total {self.spent}")


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def dry_run(seasons=DEFAULT_SEASONS, game_ids=None, markets=None, max_credits=0,
            show: int = 20, now=None) -> dict:
    """Planned games and the maximum credit cost. No API call and no write."""
    markets = markets or DEFAULT_MARKETS.split(",")
    src = get_source_db()
    hist = (sqlite3.connect(f"file:{HIST_DB_PATH}?mode=ro", uri=True)
            if os.path.exists(HIST_DB_PATH) else None)    # never created by a dry run
    try:
        games = planned_games(src, seasons, game_ids, now)
        logged = hist is not None and _table_exists(hist, "backfill_log")
        done = done_game_ids(hist) if logged else set()
        todo = [g for g in games if g["kaggle_game_id"] not in done]
        dates = sorted({g["game_date"] for g in todo}, reverse=True)
        uncached = [d for d in dates if not (logged and cached_events(hist, d) is not None)]
        fl = credit_floor(src, now)
    finally:
        src.close()
        if hist is not None:
            hist.close()
    per_game = max_odds_cost(markets)
    est = len(todo) * per_game + len(uncached) * EVENTS_CALL_COST
    by_season: dict = {}
    for g in todo:
        by_season[g["season"]] = by_season.get(g["season"], 0) + 1
    print(f"[backfill] DRY RUN: {len(todo)} games to pull ({len(games) - len(todo)} already done), "
          f"{len(markets)} markets, up to {per_game} credits/game; "
          f"{len(uncached)} events calls x {EVENTS_CALL_COST}")
    for s, n in by_season.items():
        print(f"  {s}: {n} games")
    for g in todo[:show]:
        print(f"  {g['tip_et']} ET  {g['away']} @ {g['home']}  ({g['kaggle_game_id']})")
    if len(todo) > show:
        print(f"  ... {len(todo) - show} more")
    print(f"[backfill] estimated maximum cost {est} credits; --max-credits {max_credits} "
          f"covers about {max(0, max_credits - len(uncached) * EVENTS_CALL_COST) // per_game} games")
    print(f"[backfill] floor {fl['floor']} = {fl['live_games']} live games to {fl['reset']} "
          f"x {LIVE_CREDITS_PER_GAME} + {FLOOR_BUFFER}")
    return {"games": todo, "estimated_credits": est, "events_calls": len(uncached), **fl}


def run(seasons=DEFAULT_SEASONS, game_ids=None, markets=None, max_credits=0,
        client: httpx.Client | None = None, now_fn=None) -> dict:
    """Pull planned games until done or a guard stops the run. Returns a summary."""
    if not API_KEY:
        raise SystemExit("ODDS_API_KEY not set")
    markets = markets or DEFAULT_MARKETS.split(",")
    now_fn = now_fn or (lambda: dt.datetime.now(dt.timezone.utc))
    own = client is None
    client = client or httpx.Client(timeout=30)
    conn = get_db()
    init_tables(conn)
    src = get_source_db()
    r = Run(conn, src, client, markets, max_credits, now_fn)
    stopped = None
    pulled = 0
    try:
        # Refuse before any call, even the free one, near a live pre-tip pull.
        near = live_pretips_near(src, now_fn())
        if near:
            raise StopBackfill(f"live pre-tip pull due within {LIVE_GUARD_MINUTES} min: {near}")
        done = done_game_ids(conn)
        games = [g for g in planned_games(src, seasons, game_ids, now_fn()) if g["kaggle_game_id"] not in done]
        print(f"[backfill] {len(games)} games to pull, newest first; --max-credits {max_credits}")
        r.read_remaining()
        events_by_date: dict = {}
        for g in games:
            if g["game_date"] not in events_by_date:
                events_by_date[g["game_date"]] = r.events_for(g["game_date"])
            events = events_by_date[g["game_date"]]
            if events is None:
                continue                             # events call failed: retried next run
            event = match_event(g, events)
            if event is None:
                _log(conn, endpoint="event_odds", kaggle_game_id=g["kaggle_game_id"],
                     game_date=g["game_date"], credits_used=0, status="unmatched",
                     error=f"no single OddsAPI event for {g['away']} @ {g['home']}")
                conn.commit()
                print(f"[backfill] {g['kaggle_game_id']} {g['away']} @ {g['home']}: no matching event")
                continue
            r.pull_game(g, event)
            pulled += 1
    except StopBackfill as exc:
        stopped = str(exc)
        _log(conn, endpoint="guard", credits_used=0, remaining_after=r.remaining,
             status="stopped", error=stopped)
        conn.commit()
        print(f"[backfill] STOPPED: {stopped}")
    finally:
        conn.close()
        src.close()
        if own:
            client.close()
    print(f"[backfill] done: {pulled} game calls, {r.spent} credits this run, remaining {r.remaining}")
    return {"pulled": pulled, "spent": r.spent, "remaining": r.remaining, "stopped": stopped}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--max-credits", type=int, required=True, help="hard budget for this run")
    ap.add_argument("--seasons", default=",".join(DEFAULT_SEASONS))
    ap.add_argument("--game-ids", help="comma-separated Kaggle gameIds (overrides --seasons)")
    ap.add_argument("--markets", default=DEFAULT_MARKETS)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--show", type=int, default=20, help="dry run: games to list")
    a = ap.parse_args()
    seasons = tuple(s.strip() for s in a.seasons.split(",") if s.strip())
    game_ids = {int(x) for x in a.game_ids.split(",")} if a.game_ids else None
    markets = [m.strip() for m in a.markets.split(",") if m.strip()]
    if a.dry_run:
        dry_run(seasons, game_ids, markets, a.max_credits, a.show)
    else:
        run(seasons, game_ids, markets, a.max_credits)


if __name__ == "__main__":
    main()
