"""
historical_odds_etl.py
Standalone one-time script — run manually from the backend folder:
    python historical_odds_etl.py

Pulls NBA and NFL historical player prop lines from OddsAPI and writes them
to predictions.db in two tables:
  - historical_player_props    (one row per player/game/market)
  - historical_etl_progress    (checkpoint — lets the script resume if interrupted)

Requires ODDS_API_HISTORICAL_KEY in .env (separate paid-plan key).
"""

import os
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "predictions.db")
API_KEY = os.getenv("ODDS_API_HISTORICAL_KEY", "")
_BASE   = "https://api.the-odds-api.com"

# ---------------------------------------------------------------------------
# Market definitions
# ---------------------------------------------------------------------------

_NBA_MARKETS = (
    "player_points,player_rebounds,player_assists,player_threes,"
    "player_steals,player_blocks"
)
_NFL_MARKETS = (
    "player_passing_yards,player_rushing_yards,player_receiving_yards,"
    "player_receptions,player_passing_tds,player_rushing_tds,player_receiving_tds"
)

_MARKET_TO_STAT = {
    "player_points":          "Points",
    "player_rebounds":        "Rebounds",
    "player_assists":         "Assists",
    "player_threes":          "3PM",
    "player_steals":          "Steals",
    "player_blocks":          "Blocks",
    "player_passing_yards":   "Passing Yards",
    "player_rushing_yards":   "Rushing Yards",
    "player_receiving_yards": "Receiving Yards",
    "player_receptions":      "Receptions",
    "player_passing_tds":     "Passing TDs",
    "player_rushing_tds":     "Rushing TDs",
    "player_receiving_tds":   "Receiving TDs",
}

# Processing order: most recent / most valuable first
SEASONS = [
    # (sport,                   season,    start,               end,                markets)
    ("basketball_nba",       "2025-26", date(2025, 10, 22), date(2026,  5, 24), _NBA_MARKETS),
    ("basketball_nba",       "2024-25", date(2024, 10, 22), date(2025,  6, 22), _NBA_MARKETS),
    ("americanfootball_nfl", "2024",    date(2024,  9,  5), date(2025,  2,  9), _NFL_MARKETS),
    ("basketball_nba",       "2023-24", date(2023, 10, 24), date(2024,  6, 23), _NBA_MARKETS),
    ("americanfootball_nfl", "2023",    date(2023,  9,  7), date(2024,  2, 11), _NFL_MARKETS),
]

# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------

def setup_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS historical_player_props (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sport       TEXT,
            season      TEXT,
            game_id     TEXT,
            game_date   TEXT,
            home_team   TEXT,
            away_team   TEXT,
            player_name TEXT,
            stat_type   TEXT,
            line        REAL,
            bookmaker   TEXT,
            inserted_at TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_hist_props
        ON historical_player_props(sport, season, player_name, stat_type)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS historical_etl_progress (
            game_id            TEXT PRIMARY KEY,
            sport              TEXT,
            processed_at       TEXT,
            snapshot_timestamp TEXT
        )
    """)
    # Migrate existing table if snapshot_timestamp column is missing
    existing = {r[1] for r in conn.execute("PRAGMA table_info(historical_etl_progress)").fetchall()}
    if "snapshot_timestamp" not in existing:
        conn.execute("ALTER TABLE historical_etl_progress ADD COLUMN snapshot_timestamp TEXT")
    conn.commit()
    conn.close()


def _get_processed_ids(sport: str) -> set:
    conn = sqlite3.connect(DB_PATH)
    ids = {r[0] for r in conn.execute(
        "SELECT game_id FROM historical_etl_progress WHERE sport = ?", (sport,)
    ).fetchall()}
    conn.close()
    return ids

# ---------------------------------------------------------------------------
# Step 2 — Events scan
# ---------------------------------------------------------------------------

def scan_events(
    sport: str,
    season: str,
    start: date,
    end: date,
    processed: set,
) -> list[tuple[str, str, str, str, str]]:
    """
    Walk the date range day by day, querying the historical events endpoint at
    T18:00:00Z (2pm ET / 6pm UTC — before most games tip off).  Stores the
    actual snapshot timestamp returned by the API alongside each game_id so
    that the props call can use the exact same snapshot.

    Returns a deduplicated list of
    (game_id, snapshot_timestamp, game_date, home_team, away_team)
    not yet in `processed`.
    """
    # game_map: game_id -> (snapshot_timestamp, game_date, home_team, away_team)
    game_map: dict[str, tuple[str, str, str, str]] = {}
    cur = start

    while cur <= end:
        try:
            resp = httpx.get(
                f"{_BASE}/v4/historical/sports/{sport}/events",
                params={"apiKey": API_KEY, "date": f"{cur.isoformat()}T18:00:00Z"},
                timeout=15,
            )
            if resp.status_code == 200:
                body = resp.json()
                snapshot_ts = body.get("timestamp", f"{cur.isoformat()}T18:00:00Z")
                for event in body.get("data", []):
                    gid = event["id"]
                    if gid not in game_map:
                        game_map[gid] = (
                            snapshot_ts,
                            event["commence_time"][:10],
                            event["home_team"],
                            event["away_team"],
                        )
            else:
                print(f"  [events scan] {cur}: HTTP {resp.status_code} — {resp.text[:120]}")
        except Exception as exc:
            print(f"  [events scan] {cur}: {exc}")

        cur += timedelta(days=1)
        time.sleep(0.3)

    new_games = [
        (gid, snap_ts, gdate, home, away)
        for gid, (snap_ts, gdate, home, away) in game_map.items()
        if gid not in processed
    ]
    already = len(game_map) - len(new_games)
    print(
        f"  [{sport} {season}] {len(game_map)} unique game IDs — "
        f"{already} already processed, {len(new_games)} to pull"
    )
    return new_games

# ---------------------------------------------------------------------------
# Step 3 — Props pull for one game
# ---------------------------------------------------------------------------

def pull_game_props(
    sport: str,
    season: str,
    game_id: str,
    snapshot_timestamp: str,
    game_date: str,
    home_team: str,
    away_team: str,
    markets: str,
) -> tuple[int, str | None]:
    """
    Fetch props for a single game.  Uses snapshot_timestamp (the exact timestamp
    returned by the events scan) as the date parameter so the event ID is valid.
    Tries FanDuel first; falls back to DraftKings if FanDuel returns no outcomes.
    Always marks the game as processed so it is skipped on resume.

    Returns (rows_inserted, credits_remaining_header_value).
    """
    rows: list[tuple] = []
    credits_remaining: str | None = None

    for bookmaker in ("fanduel", "draftkings"):
        try:
            resp = httpx.get(
                f"{_BASE}/v4/historical/sports/{sport}/events/{game_id}/odds",
                params={
                    "apiKey":     API_KEY,
                    "date":       snapshot_timestamp,
                    "regions":    "us",
                    "markets":    markets,
                    "oddsFormat": "american",
                    "bookmakers": bookmaker,
                },
                timeout=15,
            )
            credits_remaining = resp.headers.get("x-requests-remaining")

            if resp.status_code != 200:
                print(
                    f"  [props] {game_id} ({bookmaker}): "
                    f"HTTP {resp.status_code} — {resp.text[:120]}"
                )
                break  # mark processed, skip game

            bk_list = resp.json().get("data", {}).get("bookmakers", [])
            has_outcomes = (
                bk_list
                and any(
                    outcome
                    for m in bk_list[0].get("markets", [])
                    for outcome in m.get("outcomes", [])
                )
            )

            if not has_outcomes:
                if bookmaker == "fanduel":
                    time.sleep(0.5)
                    continue  # try DraftKings
                break  # DraftKings also empty — mark processed and move on

            # Parse Over outcomes
            now_str = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
            bk_key  = bk_list[0].get("key", bookmaker)
            for market in bk_list[0].get("markets", []):
                stat_type = _MARKET_TO_STAT.get(market["key"])
                if not stat_type:
                    continue
                for outcome in market.get("outcomes", []):
                    if outcome.get("name") != "Over":
                        continue
                    rows.append((
                        sport, season, game_id, game_date,
                        home_team, away_team,
                        outcome.get("description", ""),
                        stat_type,
                        outcome.get("point"),
                        bk_key,
                        now_str,
                    ))
            break  # got a usable response from this book

        except Exception as exc:
            print(f"  [props] {game_id} ({bookmaker}): {exc}")
            break

    # Commit rows + mark progress in one transaction
    now_str = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    conn = sqlite3.connect(DB_PATH)
    if rows:
        conn.executemany(
            """
            INSERT INTO historical_player_props
                (sport, season, game_id, game_date, home_team, away_team,
                 player_name, stat_type, line, bookmaker, inserted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    conn.execute(
        "INSERT OR IGNORE INTO historical_etl_progress "
        "(game_id, sport, processed_at, snapshot_timestamp) VALUES (?, ?, ?, ?)",
        (game_id, sport, now_str, snapshot_timestamp),
    )
    conn.commit()
    conn.close()

    return len(rows), credits_remaining

# ---------------------------------------------------------------------------
# Season runner
# ---------------------------------------------------------------------------

def run_season(
    sport: str,
    season: str,
    start: date,
    end: date,
    markets: str,
) -> tuple[int, str | None, str | None]:
    """
    Scan events then pull props for one sport/season.
    Returns (props_stored, first_credits_remaining, last_credits_remaining).
    """
    print(f"\n{'=' * 60}")
    print(f"  {sport}  |  season {season}  |  {start} → {end}")
    print("=" * 60)

    processed = _get_processed_ids(sport)
    games     = scan_events(sport, season, start, end, processed)
    total     = len(games)

    if total == 0:
        print("  Nothing new to process.")
        return 0, None, None

    props_stored = 0
    first_cr: str | None = None
    last_cr:  str | None = None

    for i, (game_id, snapshot_timestamp, game_date, home_team, away_team) in enumerate(games, 1):
        n, cr = pull_game_props(
            sport, season, game_id, snapshot_timestamp, game_date, home_team, away_team, markets
        )
        props_stored += n

        if cr is not None:
            if first_cr is None:
                first_cr = cr
            last_cr = cr

        if i % 50 == 0:
            print(
                f"  [{sport} {season}] {i}/{total} games processed, "
                f"{props_stored:,} props stored"
            )
        if i % 100 == 0 and last_cr is not None:
            print(f"  Credits remaining: {last_cr}")

        time.sleep(0.5)

    print(
        f"  [{sport} {season}] Done — "
        f"{props_stored:,} props stored from {total} games"
    )
    return props_stored, first_cr, last_cr

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if not API_KEY:
        print("ERROR: ODDS_API_HISTORICAL_KEY is not set in .env — aborting.")
        return

    print("=" * 60)
    print("Historical Odds ETL")
    print(f"DB: {DB_PATH}")
    print("=" * 60)
    print("Setting up tables...")
    setup_db()

    grand_total      = 0
    first_cr_global: str | None = None
    last_cr_global:  str | None = None
    breakdown: list[str] = []

    for sport, season, start, end, markets in SEASONS:
        props, first_cr, last_cr = run_season(sport, season, start, end, markets)
        grand_total += props
        if first_cr is not None and first_cr_global is None:
            first_cr_global = first_cr
        if last_cr is not None:
            last_cr_global = last_cr
        breakdown.append(f"  {sport} {season}: {props:,} props")

    print(f"\n{'=' * 60}")
    print("ETL Complete")
    print(f"Total props stored: {grand_total:,}")
    if first_cr_global is not None and last_cr_global is not None:
        try:
            used = int(first_cr_global) - int(last_cr_global)
            print(f"Credits used:       {used:,}")
        except ValueError:
            pass
        print(f"Credits remaining:  {last_cr_global}")
    print("Breakdown by sport/season:")
    for line in breakdown:
        print(line)
    print("=" * 60)


if __name__ == "__main__":
    main()
