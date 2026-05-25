"""
data_fetcher.py
Queries local SQLite (populated once by database.run_etl) instead of nba_api.
All function signatures are identical to the previous version so predictor.py
and main.py require no changes beyond the startup call.
"""

import os
import json
import sqlite3
import time
import difflib
import datetime as dt
from zoneinfo import ZoneInfo

import httpx
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

from database import DB_PATH


# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Teams — hardcoded 30-team mapping (static, never changes mid-season)
# Replaces nba_teams_static; predictor.py imports full_to_abbrev from here.
# ---------------------------------------------------------------------------

_TEAMS: dict[str, str] = {
    "Atlanta Hawks":        "ATL",
    "Boston Celtics":       "BOS",
    "Brooklyn Nets":        "BKN",
    "Charlotte Hornets":    "CHA",
    "Chicago Bulls":        "CHI",
    "Cleveland Cavaliers":  "CLE",
    "Dallas Mavericks":     "DAL",
    "Denver Nuggets":       "DEN",
    "Detroit Pistons":      "DET",
    "Golden State Warriors":"GSW",
    "Houston Rockets":      "HOU",
    "Indiana Pacers":       "IND",
    "Los Angeles Clippers": "LAC",
    "Los Angeles Lakers":   "LAL",
    "Memphis Grizzlies":    "MEM",
    "Miami Heat":           "MIA",
    "Milwaukee Bucks":      "MIL",
    "Minnesota Timberwolves":"MIN",
    "New Orleans Pelicans": "NOP",
    "New York Knicks":      "NYK",
    "Oklahoma City Thunder":"OKC",
    "Orlando Magic":        "ORL",
    "Philadelphia 76ers":   "PHI",
    "Phoenix Suns":         "PHX",
    "Portland Trail Blazers":"POR",
    "Sacramento Kings":     "SAC",
    "San Antonio Spurs":    "SAS",
    "Toronto Raptors":      "TOR",
    "Utah Jazz":            "UTA",
    "Washington Wizards":   "WAS",
}
_TEAMS_BY_ABBREV: dict[str, str] = {v: k for k, v in _TEAMS.items()}


def full_to_abbrev(full_name: str) -> str:
    """Return the 3-letter abbreviation for a full team name."""
    return _TEAMS.get(full_name, full_name)


def abbrev_to_full(abbrev: str) -> str:
    """Return the full team name for a 3-letter abbreviation."""
    return _TEAMS_BY_ABBREV.get(abbrev.upper(), abbrev)


def get_team_names() -> list[str]:
    """Return sorted list of all NBA team full names from the schedule table."""
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT homeTeamCity || ' ' || homeTeamName AS team "
        "FROM schedule WHERE homeTeamId != 0 ORDER BY team"
    ).fetchall()
    conn.close()
    return [r["team"] for r in rows]


# ---------------------------------------------------------------------------
# Players
# ---------------------------------------------------------------------------

def search_players(query: str, limit: int = 10) -> list[str]:
    """Return up to `limit` full names containing `query` (case-insensitive)."""
    q = query.strip()
    if not q:
        return []
    conn = get_db()
    rows = conn.execute(
        """
        SELECT DISTINCT p.firstName, p.lastName
        FROM players p
        WHERE (p.firstName || ' ' || p.lastName) LIKE ?
          AND EXISTS (
              SELECT 1 FROM player_stats ps
              WHERE ps.personId = p.personId
                AND ps.gameDateTimeEst >= '2025-10-01'
          )
        LIMIT ?
        """,
        (f"%{q}%", limit),
    ).fetchall()
    conn.close()
    return [f"{r['firstName']} {r['lastName']}" for r in rows]


def get_player_id(player_name: str) -> int | None:
    """
    Resolve a display name → personId.
    1. Exact firstName / lastName split match.
    2. LIKE match on the full concatenated name.
    3. difflib fuzzy match against all names (cutoff 0.80).
    """
    name = player_name.strip()
    parts = name.split(" ", 1)
    first = parts[0]
    last  = parts[1] if len(parts) > 1 else ""

    conn = get_db()

    # 1. Exact split match
    row = conn.execute(
        """
        SELECT p.personId FROM players p
        WHERE p.firstName = ? AND p.lastName = ?
          AND EXISTS (
              SELECT 1 FROM player_stats ps
              WHERE ps.personId = p.personId
                AND ps.gameDateTimeEst >= '2025-10-01'
          )
        """,
        (first, last),
    ).fetchone()
    if row:
        conn.close()
        return int(row["personId"])

    # 2. LIKE on full name
    row = conn.execute(
        """
        SELECT p.personId FROM players p
        WHERE (p.firstName || ' ' || p.lastName) LIKE ?
          AND EXISTS (
              SELECT 1 FROM player_stats ps
              WHERE ps.personId = p.personId
                AND ps.gameDateTimeEst >= '2025-10-01'
          )
        """,
        (f"%{name}%",),
    ).fetchone()
    if row:
        conn.close()
        return int(row["personId"])

    # 3. difflib fuzzy fallback — current-season players only
    all_rows = conn.execute(
        """
        SELECT DISTINCT p.personId, p.firstName, p.lastName
        FROM players p
        WHERE EXISTS (
            SELECT 1 FROM player_stats ps
            WHERE ps.personId = p.personId
              AND ps.gameDateTimeEst >= '2025-10-01'
        )
        """
    ).fetchall()
    conn.close()

    all_names = [f"{r['firstName']} {r['lastName']}" for r in all_rows]
    close = difflib.get_close_matches(name, all_names, n=1, cutoff=0.80)
    if close:
        for r in all_rows:
            if f"{r['firstName']} {r['lastName']}" == close[0]:
                return int(r["personId"])

    return None


def get_all_active_players() -> list[dict]:
    """
    Return one entry per player reflecting their most recent team.
    Uses a correlated subquery against the idx_stats_person index.
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT personId, firstName, lastName,
               playerteamName, playerteamCity, playerteamId
        FROM player_stats
        WHERE gameDateTimeEst = (
            SELECT MAX(gameDateTimeEst)
            FROM player_stats ps2
            WHERE ps2.personId = player_stats.personId
        )
          AND EXISTS (
              SELECT 1 FROM player_stats ps3
              WHERE ps3.personId = player_stats.personId
                AND ps3.gameDateTimeEst >= '2025-10-01'
          )
    """).fetchall()
    conn.close()
    return [
        {
            "id":        int(r["personId"]),
            "full_name": f"{r['firstName']} {r['lastName']}",
            "team_name": f"{r['playerteamCity']} {r['playerteamName']}",
            "team_id":   r["playerteamId"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Game logs
# ---------------------------------------------------------------------------

def get_game_logs(
    player_name: str,
    last_n: int = 20,
) -> pd.DataFrame:
    """
    Return a DataFrame of the player's last `last_n` games across all game types.

    Columns are renamed to match predictor.py's expectations:
      GAME_DATE, MATCHUP, PTS, REB, AST, FG3M, MIN, PLUS_MINUS, WL
    MATCHUP is built as '<TEAM_ABBREV> vs. <OPP_ABBREV>' so that
    predictor.py's str.contains(opp_abbrev) filter works correctly.
    """
    player_id = get_player_id(player_name)
    if player_id is None:
        return pd.DataFrame()

    _ALWAYS_COLS = [
        "gameDateTimeEst",
        "playerteamName", "playerteamCity",
        "opponentteamName", "opponentteamCity",
        "points", "assists", "reboundsTotal", "reboundsOffensive", "reboundsDefensive",
        "threePointersMade", "numMinutes", "win", "plusMinusPoints",
        "estimatedPace", "usagePercentage", "defensiveRating",
    ]
    _OPTIONAL_COLS = ["blocks", "steals", "turnovers", "doubleDouble", "threePointersAttempted"]

    conn = get_db()
    available = {r[1] for r in conn.execute("PRAGMA table_info(player_stats)").fetchall()}
    select_cols = [c for c in _ALWAYS_COLS if c in available] + \
                  [c for c in _OPTIONAL_COLS if c in available]
    cols_sql = ", ".join(select_cols)
    rows = conn.execute(
        f"""
        SELECT {cols_sql}
        FROM player_stats
        WHERE personId = ?
        ORDER BY gameDateTimeEst DESC
        LIMIT ?
        """,
        (player_id, last_n),
    ).fetchall()
    conn.close()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame([dict(r) for r in rows])

    _RENAME = {
        "gameDateTimeEst":        "GAME_DATE",
        "points":                 "PTS",
        "assists":                "AST",
        "reboundsTotal":          "REB",
        "reboundsOffensive":      "OREB",
        "reboundsDefensive":      "DREB",
        "threePointersMade":      "FG3M",
        "threePointersAttempted": "FG3A",
        "numMinutes":             "MIN",
        "plusMinusPoints":        "PLUS_MINUS",
        "win":                    "WL",
        "estimatedPace":          "PACE",
        "usagePercentage":        "USG",
        "defensiveRating":        "DEF_RTG",
        "blocks":                 "BLK",
        "steals":                 "STL",
        "turnovers":              "TOV",
        "doubleDouble":           "DD",
    }
    df = df.rename(columns={k: v for k, v in _RENAME.items() if k in df.columns})

    df["GAME_DATE"] = pd.to_datetime(df["GAME_DATE"], errors="coerce")

    # Build MATCHUP with abbreviations — predictor searches for opp_abbrev here
    player_team = (df["playerteamCity"] + " " + df["playerteamName"]).map(full_to_abbrev)
    opp_team    = (df["opponentteamCity"] + " " + df["opponentteamName"]).map(full_to_abbrev)
    df["MATCHUP"] = player_team + " vs. " + opp_team

    return df


# ---------------------------------------------------------------------------
# Team defensive rating helper
# ---------------------------------------------------------------------------

def get_team_def_rating_last5(team_name: str) -> float:
    """
    Return the team's average defensive rating across their last 5 distinct games.
    Falls back to 108.0 (approximate NBA league average) if no data is found.
    playerteamName in player_stats stores only the nickname ('Timberwolves', not
    'Minnesota Timberwolves'), so extract the last word of the full name to match.
    'Trail Blazers' is a two-word nickname and is handled as a special case.
    """
    nickname = "Trail Blazers" if "Blazers" in team_name else team_name.split()[-1]
    conn = get_db()
    row = conn.execute(
        """
        SELECT AVG(avg_def) FROM (
            SELECT gameDateTimeEst, AVG(defensiveRating) AS avg_def
            FROM player_stats
            WHERE playerteamName = ?
            GROUP BY gameDateTimeEst
            ORDER BY gameDateTimeEst DESC
            LIMIT 5
        )
        """,
        (nickname,),
    ).fetchone()
    conn.close()
    val = row[0] if row else None
    return float(val) if val is not None else 108.0


# ---------------------------------------------------------------------------
# Player context  (team + next game + back-to-back)
# ---------------------------------------------------------------------------

def get_player_context(player_name: str) -> dict | None:
    """
    Returns team context for the selected player:
      player_team_full / abbrev, next_opponent_full / abbrev,
      is_home, is_back_to_back
    Returns None if the player cannot be found or has no game log data.
    """
    player_id = get_player_id(player_name)
    if player_id is None:
        return None

    conn = get_db()

    # Current team from most recent game in player_stats
    team_row = conn.execute(
        """
        SELECT playerteamId, playerteamName, playerteamCity
        FROM player_stats
        WHERE personId = ?
        ORDER BY gameDateTimeEst DESC
        LIMIT 1
        """,
        (player_id,),
    ).fetchone()

    if not team_row:
        conn.close()
        return None

    team_id     = int(team_row["playerteamId"])
    team_full   = f"{team_row['playerteamCity']} {team_row['playerteamName']}"
    team_abbrev = full_to_abbrev(team_full)

    # Next scheduled game after right now (UTC).
    # Use strftime to produce 'YYYY-MM-DD HH:MM:SS' — the same format SQLite stores
    # datetimes in — so the string comparison is reliable.  .isoformat() produces
    # 'YYYY-MM-DDTHH:MM:SS+00:00' where the 'T' and '+00:00' suffix break ordering
    # against space-separated stored values (space < 'T' in ASCII).
    now = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    game_row = conn.execute(
        """
        SELECT gameDateTimeEst, homeTeamId,
               homeTeamName, homeTeamCity,
               awayTeamName, awayTeamCity
        FROM schedule
        WHERE (CAST(homeTeamId AS INTEGER) = CAST(? AS INTEGER)
               OR CAST(awayTeamId AS INTEGER) = CAST(? AS INTEGER))
          AND gameDateTimeEst > ?
          AND homeTeamId != 0
        ORDER BY gameDateTimeEst ASC
        LIMIT 1
        """,
        (team_id, team_id, now),
    ).fetchone()

    if not game_row:
        conn.close()
        return {
            "player_team_full":     team_full,
            "player_team_abbrev":   team_abbrev,
            "next_opponent_full":   None,
            "next_opponent_abbrev": None,
            "is_home":              False,
            "is_back_to_back":      False,
            "next_game_date":       None,
        }

    is_home = int(game_row["homeTeamId"]) == team_id
    if is_home:
        opp_full = f"{game_row['awayTeamCity']} {game_row['awayTeamName']}"
    else:
        opp_full = f"{game_row['homeTeamCity']} {game_row['homeTeamName']}"
    opp_abbrev = full_to_abbrev(opp_full)

    # Back-to-back: did this team play the day before the next game?
    b2b_row = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM schedule
        WHERE (CAST(homeTeamId AS INTEGER) = CAST(? AS INTEGER)
               OR CAST(awayTeamId AS INTEGER) = CAST(? AS INTEGER))
          AND date(gameDateTimeEst) = date(?, '-1 day')
          AND homeTeamId != 0
        """,
        (team_id, team_id, game_row["gameDateTimeEst"]),
    ).fetchone()
    b2b = b2b_row["cnt"] > 0

    next_game_date = str(game_row["gameDateTimeEst"])[:10] if game_row["gameDateTimeEst"] else None

    conn.close()

    return {
        "player_team_full":     team_full,
        "player_team_abbrev":   team_abbrev,
        "next_opponent_full":   opp_full,
        "next_opponent_abbrev": opp_abbrev,
        "is_home":              is_home,
        "is_back_to_back":      b2b,
        "next_game_date":       next_game_date,
    }


# ---------------------------------------------------------------------------
# ESPN injury feed  (unchanged — no nba_api dependency)
# ---------------------------------------------------------------------------

_ESPN_INJURIES_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"


def get_injury_context(player_name: str) -> str:
    """Return a short injury note for the player, or '' if none found."""
    try:
        resp = httpx.get(_ESPN_INJURIES_URL, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        injuries = data.get("injuries", [])
        name_lower = player_name.lower()
        for entry in injuries:
            athlete = entry.get("athlete", {})
            if name_lower in athlete.get("displayName", "").lower():
                status = entry.get("status", "")
                detail = entry.get("shortComment", entry.get("longComment", ""))
                if status or detail:
                    return f"{status}: {detail}".strip(": ")
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# ESPN injury feed
# ---------------------------------------------------------------------------

_ESPN_INJURIES_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"


def _fetch_espn_injuries() -> list:
    resp = httpx.get(_ESPN_INJURIES_URL, timeout=8)
    resp.raise_for_status()
    return resp.json().get("injuries", [])


def get_player_injury_status(player_name: str) -> dict | None:
    try:
        teams = _fetch_espn_injuries()
        all_players = [
            (player, team)
            for team in teams
            for player in team.get("injuries", [])
            if "athlete" in player
        ]
        names = [p["athlete"]["displayName"] for p, _ in all_players]
        close = difflib.get_close_matches(player_name, names, n=1, cutoff=0.8)
        if not close:
            return None
        matched_player = next(p for p, _ in all_players if p["athlete"]["displayName"] == close[0])
        return {
            "status": matched_player.get("status", ""),
            "reason": matched_player.get("shortComment", ""),
        }
    except Exception:
        return None


def get_opponent_injuries(opponent_team: str) -> list[dict]:
    try:
        teams = _fetch_espn_injuries()
        team_names = [t.get("displayName", "") for t in teams]
        close = difflib.get_close_matches(opponent_team, team_names, n=1, cutoff=0.7)
        if not close:
            return []
        matched_team = next(t for t in teams if t.get("displayName") == close[0])
        result = []
        for player in matched_team.get("injuries", []):
            status = player.get("status", "")
            if status in ("Out", "Questionable"):
                result.append({
                    "player": player.get("athlete", {}).get("displayName", ""),
                    "status": status,
                    "reason": player.get("shortComment", ""),
                })
        return result
    except Exception:
        return []


# ---------------------------------------------------------------------------
# ESPN news feed
# ---------------------------------------------------------------------------

_ESPN_NEWS_URL = "http://site.api.espn.com/apis/site/v2/sports/basketball/nba/news"


def get_player_news(player_name: str) -> str:
    """
    Return the first ESPN headline + description mentioning the player,
    or '' if nothing is found or the request fails.
    """
    try:
        resp = httpx.get(_ESPN_NEWS_URL, timeout=5)
        resp.raise_for_status()
        articles = resp.json().get("articles", [])
        name_lower = player_name.lower()
        for article in articles:
            headline = article.get("headline", "")
            description = article.get("description", "")
            if name_lower in headline.lower() or name_lower in description.lower():
                return f"{headline}: {description}" if description else headline
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# OddsAPI — today's player prop lines
# ---------------------------------------------------------------------------

_ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
_ODDS_EVENTS_URL = "https://api.the-odds-api.com/v4/sports/basketball_nba/events"
_ODDS_PROPS_URL  = "https://api.the-odds-api.com/v4/sports/basketball_nba/events/{event_id}/odds"
_PREFERRED_BOOKS = ["fanduel", "draftkings"]
_PROP_MARKETS    = "player_points,player_rebounds,player_assists,player_threes,player_steals,player_blocks,player_blocks_steals"
_STAT_TYPE_MAP   = {
    "Points":       "player_points",
    "Rebounds":     "player_rebounds",
    "Assists":      "player_assists",
    "3PM":          "player_threes",
    "Steals":       "player_steals",
    "Blocks":       "player_blocks",
    "Blocks+Steals": "player_blocks_steals",  # DraftKings only; FanDuel doesn't carry this market
}
_CACHE_TTL = 6 * 3600  # 6 hours

_lines_cache: dict = {}
_lines_cache_ts: float = 0.0


def get_todays_lines() -> dict:
    """
    Return today's NBA player prop lines keyed by player name:
      {
        "Cade Cunningham": {
          "player_points": {"value": 27.5, "source": "fanduel"},
          ...
          "game_id": "...", "home_team": "...", "away_team": "..."
        }
      }
    Cache hierarchy (6-hour TTL):
      1. In-memory dict  — avoids SQLite reads within the same process lifetime.
      2. SQLite odds_cache table — survives server restarts.
      3. OddsAPI fetch — writes result back to both layers.
    Returns stale memory cache on network/API error; {} if no key configured.
    """
    global _lines_cache, _lines_cache_ts

    if not _ODDS_API_KEY:
        return {}

    now      = time.time()
    eastern  = ZoneInfo("America/New_York")
    today_et = dt.datetime.now(eastern).date()

    # 1. In-memory hit — TTL and same calendar day (ET)
    if _lines_cache and (now - _lines_cache_ts) < _CACHE_TTL:
        cached_date_et = dt.datetime.fromtimestamp(_lines_cache_ts, tz=eastern).date()
        if cached_date_et == today_et:
            return _lines_cache
        # Different calendar day — fall through to refresh

    # 2. SQLite hit — TTL and same calendar day (ET)
    conn = get_db()
    row = conn.execute(
        "SELECT data, cached_at FROM odds_cache "
        "WHERE (julianday('now') - julianday(cached_at)) * 86400 < ? LIMIT 1",
        (_CACHE_TTL,),
    ).fetchone()
    conn.close()
    if row:
        try:
            cached_dt     = dt.datetime.fromisoformat(row["cached_at"])
            cached_dt_utc = cached_dt.replace(tzinfo=dt.timezone.utc)
            if cached_dt_utc.astimezone(eastern).date() == today_et:
                _lines_cache    = json.loads(row["data"])
                _lines_cache_ts = cached_dt_utc.timestamp()
                return _lines_cache
            # Different calendar day — fall through to API fetch
        except Exception:
            pass  # corrupted row — fall through to API fetch

    # 3. Fetch today's events
    try:
        resp = httpx.get(_ODDS_EVENTS_URL, params={"apiKey": _ODDS_API_KEY}, timeout=10)
        resp.raise_for_status()
        events = resp.json()
    except Exception:
        return _lines_cache  # return stale cache rather than crashing

    # Filter to games starting today in US/Eastern time
    today_events = []
    for event in events:
        try:
            commence = dt.datetime.fromisoformat(
                event["commence_time"].replace("Z", "+00:00")
            )
            if commence.astimezone(eastern).date() == today_et:
                today_events.append(event)
        except Exception:
            continue

    # Fetch props for each game and merge into one player map
    new_cache: dict = {}
    for event in today_events:
        game_id   = event["id"]
        home_team = event.get("home_team", "")
        away_team = event.get("away_team", "")
        try:
            resp = httpx.get(
                _ODDS_PROPS_URL.format(event_id=game_id),
                params={
                    "apiKey":      _ODDS_API_KEY,
                    "regions":     "us",
                    "markets":     _PROP_MARKETS,
                    "oddsFormat":  "american",
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            continue

        # Build player_lines for this event.
        # Iterate preferred books in order; first book to set a market wins.
        player_lines: dict = {}
        for book_key in _PREFERRED_BOOKS:
            book = next(
                (b for b in data.get("bookmakers", []) if b["key"] == book_key),
                None,
            )
            if not book:
                continue
            for market in book.get("markets", []):
                market_key = market["key"]
                seen: set = set()
                for outcome in market.get("outcomes", []):
                    player = outcome.get("description", "")
                    point  = outcome.get("point")
                    if not player or point is None or player in seen:
                        continue
                    seen.add(player)
                    if player not in player_lines:
                        player_lines[player] = {
                            "game_id":   game_id,
                            "home_team": home_team,
                            "away_team": away_team,
                        }
                    # First book to provide a market key wins (fanduel > draftkings)
                    if market_key not in player_lines[player]:
                        player_lines[player][market_key] = {
                            "value":  point,
                            "source": book_key,
                        }

        new_cache.update(player_lines)

    # Persist to SQLite so the cache survives restarts
    cached_at = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        conn = get_db()
        conn.execute("DELETE FROM odds_cache")
        conn.execute(
            "INSERT INTO odds_cache (cached_at, data) VALUES (?, ?)",
            (cached_at, json.dumps(new_cache)),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # non-fatal: memory cache still works if the write fails

    _lines_cache    = new_cache
    _lines_cache_ts = now
    return _lines_cache


def get_player_line(player_name: str, stat_type: str) -> tuple[float | None, str | None]:
    """
    Return (line_value, bookmaker_key) for the given player and stat type,
    or (None, None) if unavailable.
    Combined stat types (PRA, PR, PA, RA) always return (None, None).
    Uses fuzzy name matching (cutoff 0.85) to handle minor OddsAPI name differences.

    For Blocks+Steals (player_blocks_steals): FanDuel does not carry this market.
    The cache is populated with _PREFERRED_BOOKS = ["fanduel", "draftkings"], so FanDuel
    is checked first and DraftKings fills in as the fallback — the returned source will
    be "draftkings" for this market.
    """
    market_key = _STAT_TYPE_MAP.get(stat_type)
    if not market_key:
        return None, None

    cache = get_todays_lines()
    if not cache:
        return None, None

    def _extract(name: str) -> tuple[float | None, str | None]:
        entry = cache.get(name, {}).get(market_key)
        if entry:
            return entry["value"], entry["source"]
        # For Blocks+Steals, FanDuel won't carry the market; DraftKings data lands here.
        if stat_type == "Blocks+Steals":
            dk_entry = cache.get(name, {}).get("player_blocks_steals")
            if dk_entry:
                return dk_entry["value"], dk_entry["source"]
        return None, None

    # Exact match first
    value, source = _extract(player_name)
    if value is not None:
        return value, source

    # Fuzzy fallback
    close = difflib.get_close_matches(player_name, list(cache.keys()), n=1, cutoff=0.85)
    if close:
        return _extract(close[0])

    return None, None


# ---------------------------------------------------------------------------
# WNBA — ESPN public API + BallDontLie + OddsAPI
# ---------------------------------------------------------------------------

_WNBA_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"
_WNBA_INJURIES_URL   = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/injuries"
_WNBA_ODDS_EVENTS    = "https://api.the-odds-api.com/v4/sports/basketball_wnba/events"
_WNBA_ODDS_PROPS     = "https://api.the-odds-api.com/v4/sports/basketball_wnba/events/{event_id}/odds"
_BALLDONTLIE_KEY     = os.getenv("BALLDONTLIE_WNBA_KEY", "")

_wnba_lines_cache: dict = {}
_wnba_lines_cache_ts: float = 0.0


def _parse_wnba_minutes(s) -> float:
    try:
        parts = str(s).split(":")
        return float(parts[0]) + (float(parts[1]) / 60 if len(parts) > 1 else 0)
    except Exception:
        return 0.0


def get_wnba_team_names() -> list[str]:
    """Return sorted list of distinct WNBA team names from wnba_player_stats."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT DISTINCT player_team FROM wnba_player_stats ORDER BY player_team"
        ).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    return [r["player_team"] for r in rows if r["player_team"]]


def search_wnba_players(query: str, limit: int = 10) -> list[str]:
    """Return up to limit WNBA player names matching query (DB + BallDontLie)."""
    q = query.strip()
    if not q:
        return []
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT DISTINCT player_name FROM wnba_player_stats "
            "WHERE player_name LIKE ? ORDER BY player_name LIMIT ?",
            (f"%{q}%", limit),
        ).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    db_names = [r["player_name"] for r in rows]

    bdl_names: list[str] = []
    if _BALLDONTLIE_KEY:
        try:
            resp = httpx.get(
                "https://api.balldontlie.io/wnba/v1/players",
                params={"search": q},
                headers={"Authorization": _BALLDONTLIE_KEY},
                timeout=5,
            )
            resp.raise_for_status()
            for p in resp.json().get("data", []):
                name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
                if name:
                    bdl_names.append(name)
        except Exception:
            pass

    seen: set = set()
    result: list[str] = []
    for name in db_names + bdl_names:
        if name not in seen:
            seen.add(name)
            result.append(name)
        if len(result) >= limit:
            break
    return result


def get_wnba_game_logs(player_name: str, last_n: int = 40) -> pd.DataFrame:
    """
    Return DataFrame of last N WNBA games for the player.
    Columns: GAME_DATE, PTS, REB, AST, FG3M, STL, BLK, TOV, OREB, DREB,
             MIN, PLUS_MINUS, MATCHUP, player_team, opponent_team.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT game_date, player_team, opponent_team, home, minutes,
                   points, rebounds, assists, three_pm, steals, blocks,
                   turnovers, oreb, dreb, plus_minus
            FROM wnba_player_stats
            WHERE player_name = ?
            ORDER BY game_date DESC
            LIMIT ?
            """,
            (player_name, last_n),
        ).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()

    if not rows:
        conn = get_db()
        try:
            all_rows = conn.execute(
                "SELECT DISTINCT player_name FROM wnba_player_stats"
            ).fetchall()
        except Exception:
            all_rows = []
        finally:
            conn.close()
        all_names = [r["player_name"] for r in all_rows]
        close = difflib.get_close_matches(player_name, all_names, n=1, cutoff=0.80)
        if close:
            return get_wnba_game_logs(close[0], last_n)
        return pd.DataFrame()

    df = pd.DataFrame([dict(r) for r in rows])
    df = df.rename(columns={
        "game_date":  "GAME_DATE",
        "points":     "PTS",
        "rebounds":   "REB",
        "assists":    "AST",
        "three_pm":   "FG3M",
        "steals":     "STL",
        "blocks":     "BLK",
        "turnovers":  "TOV",
        "oreb":       "OREB",
        "dreb":       "DREB",
        "plus_minus": "PLUS_MINUS",
    })
    df["MIN"] = df["minutes"].apply(_parse_wnba_minutes)
    df["GAME_DATE"] = pd.to_datetime(df["GAME_DATE"], errors="coerce")
    df["MATCHUP"] = df["player_team"] + " vs. " + df["opponent_team"]
    return df


def get_wnba_player_context(player_name: str) -> dict | None:
    """
    Return team context dict for a WNBA player (same shape as get_player_context).
    Team from most recent wnba_player_stats row; next game from ESPN WNBA scoreboard.
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT player_team FROM wnba_player_stats WHERE player_name = ? "
            "ORDER BY game_date DESC LIMIT 1",
            (player_name,),
        ).fetchone()
    except Exception:
        row = None
    finally:
        conn.close()

    if not row:
        conn = get_db()
        try:
            all_rows = conn.execute("SELECT DISTINCT player_name FROM wnba_player_stats").fetchall()
        except Exception:
            all_rows = []
        finally:
            conn.close()
        all_names = [r["player_name"] for r in all_rows]
        close = difflib.get_close_matches(player_name, all_names, n=1, cutoff=0.80)
        if close:
            return get_wnba_player_context(close[0])
        return None

    team_full = row["player_team"]

    eastern  = ZoneInfo("America/New_York")
    today_et = dt.datetime.now(eastern).date()

    def _fetch_wnba_scoreboard(date_str: str) -> list:
        try:
            r = httpx.get(_WNBA_SCOREBOARD_URL, params={"dates": date_str}, timeout=8)
            r.raise_for_status()
            return r.json().get("events", [])
        except Exception:
            return []

    today_events     = _fetch_wnba_scoreboard(today_et.strftime("%Y%m%d"))
    yesterday_events = _fetch_wnba_scoreboard(
        (today_et - dt.timedelta(days=1)).strftime("%Y%m%d")
    )

    next_opponent_full = None
    is_home = False
    next_game_date = None

    for event in today_events:
        try:
            comps = event.get("competitions", [{}])[0]
            competitors = comps.get("competitors", [])
            team_names = [c["team"]["displayName"] for c in competitors]
            close = difflib.get_close_matches(team_full, team_names, n=1, cutoff=0.70)
            if not close:
                continue
            matched = close[0]
            home_comp = next((c for c in competitors if c["homeAway"] == "home"), None)
            away_comp = next((c for c in competitors if c["homeAway"] == "away"), None)
            if not home_comp or not away_comp:
                continue
            if matched == home_comp["team"]["displayName"]:
                is_home = True
                next_opponent_full = away_comp["team"]["displayName"]
            else:
                is_home = False
                next_opponent_full = home_comp["team"]["displayName"]
            next_game_date = today_et.isoformat()
            break
        except Exception:
            continue

    b2b = False
    for event in yesterday_events:
        try:
            comps = event.get("competitions", [{}])[0]
            team_names = [c["team"]["displayName"] for c in comps.get("competitors", [])]
            if difflib.get_close_matches(team_full, team_names, n=1, cutoff=0.70):
                b2b = True
                break
        except Exception:
            continue

    return {
        "player_team_full":     team_full,
        "player_team_abbrev":   team_full,
        "next_opponent_full":   next_opponent_full,
        "next_opponent_abbrev": next_opponent_full,
        "is_home":              is_home,
        "is_back_to_back":      b2b,
        "next_game_date":       next_game_date,
    }


def _fetch_wnba_injuries() -> list:
    r = httpx.get(_WNBA_INJURIES_URL, timeout=8)
    r.raise_for_status()
    return r.json().get("injuries", [])


def get_wnba_injury_status(player_name: str) -> dict | None:
    try:
        teams = _fetch_wnba_injuries()
        all_players = [
            (p, t)
            for t in teams
            for p in t.get("injuries", [])
            if "athlete" in p
        ]
        names = [p["athlete"]["displayName"] for p, _ in all_players]
        close = difflib.get_close_matches(player_name, names, n=1, cutoff=0.8)
        if not close:
            return None
        matched = next(p for p, _ in all_players if p["athlete"]["displayName"] == close[0])
        return {"status": matched.get("status", ""), "reason": matched.get("shortComment", "")}
    except Exception:
        return None


def get_wnba_opponent_injuries(opponent_team: str) -> list[dict]:
    try:
        teams = _fetch_wnba_injuries()
        team_names = [t.get("displayName", "") for t in teams]
        close = difflib.get_close_matches(opponent_team, team_names, n=1, cutoff=0.7)
        if not close:
            return []
        matched_team = next(t for t in teams if t.get("displayName") == close[0])
        result = []
        for p in matched_team.get("injuries", []):
            status = p.get("status", "")
            if status in ("Out", "Questionable"):
                result.append({
                    "player": p.get("athlete", {}).get("displayName", ""),
                    "status": status,
                    "reason": p.get("shortComment", ""),
                })
        return result
    except Exception:
        return []


def get_wnba_todays_lines() -> dict:
    """
    Same as get_todays_lines() but uses OddsAPI basketball_wnba sport key
    and persists to the wnba_odds_cache SQLite table.
    """
    global _wnba_lines_cache, _wnba_lines_cache_ts

    if not _ODDS_API_KEY:
        return {}

    now      = time.time()
    eastern  = ZoneInfo("America/New_York")
    today_et = dt.datetime.now(eastern).date()

    # 1. In-memory hit
    if _wnba_lines_cache and (now - _wnba_lines_cache_ts) < _CACHE_TTL:
        cached_date_et = dt.datetime.fromtimestamp(_wnba_lines_cache_ts, tz=eastern).date()
        if cached_date_et == today_et:
            return _wnba_lines_cache

    # 2. SQLite hit
    conn = get_db()
    row = conn.execute(
        "SELECT data, cached_at FROM wnba_odds_cache "
        "WHERE (julianday('now') - julianday(cached_at)) * 86400 < ? LIMIT 1",
        (_CACHE_TTL,),
    ).fetchone()
    conn.close()
    if row:
        try:
            cached_dt = dt.datetime.fromisoformat(row["cached_at"]).replace(tzinfo=dt.timezone.utc)
            if cached_dt.astimezone(eastern).date() == today_et:
                _wnba_lines_cache    = json.loads(row["data"])
                _wnba_lines_cache_ts = cached_dt.timestamp()
                return _wnba_lines_cache
        except Exception:
            pass

    # 3. Fetch from OddsAPI
    try:
        resp = httpx.get(_WNBA_ODDS_EVENTS, params={"apiKey": _ODDS_API_KEY}, timeout=10)
        resp.raise_for_status()
        events = resp.json()
    except Exception:
        return _wnba_lines_cache

    today_events = []
    for event in events:
        try:
            commence = dt.datetime.fromisoformat(event["commence_time"].replace("Z", "+00:00"))
            if commence.astimezone(eastern).date() == today_et:
                today_events.append(event)
        except Exception:
            continue

    new_cache: dict = {}
    for event in today_events:
        game_id   = event["id"]
        home_team = event.get("home_team", "")
        away_team = event.get("away_team", "")
        try:
            resp = httpx.get(
                _WNBA_ODDS_PROPS.format(event_id=game_id),
                params={"apiKey": _ODDS_API_KEY, "regions": "us",
                        "markets": _PROP_MARKETS, "oddsFormat": "american"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            continue

        player_lines: dict = {}
        for book_key in _PREFERRED_BOOKS:
            book = next((b for b in data.get("bookmakers", []) if b["key"] == book_key), None)
            if not book:
                continue
            for market in book.get("markets", []):
                market_key = market["key"]
                seen: set = set()
                for outcome in market.get("outcomes", []):
                    player = outcome.get("description", "")
                    point  = outcome.get("point")
                    if not player or point is None or player in seen:
                        continue
                    seen.add(player)
                    if player not in player_lines:
                        player_lines[player] = {"game_id": game_id,
                                                "home_team": home_team, "away_team": away_team}
                    if market_key not in player_lines[player]:
                        player_lines[player][market_key] = {"value": point, "source": book_key}
        new_cache.update(player_lines)

    cached_at = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        conn = get_db()
        conn.execute("DELETE FROM wnba_odds_cache")
        conn.execute("INSERT INTO wnba_odds_cache (cached_at, data) VALUES (?, ?)",
                     (cached_at, json.dumps(new_cache)))
        conn.commit()
        conn.close()
    except Exception:
        pass

    _wnba_lines_cache    = new_cache
    _wnba_lines_cache_ts = now
    return _wnba_lines_cache


def get_wnba_player_line(player_name: str, stat_type: str) -> tuple[float | None, str | None]:
    """Same as get_player_line but reads from the WNBA lines cache."""
    market_key = _STAT_TYPE_MAP.get(stat_type)
    if not market_key:
        return None, None
    cache = get_wnba_todays_lines()
    if not cache:
        return None, None

    def _extract(name: str) -> tuple[float | None, str | None]:
        entry = cache.get(name, {}).get(market_key)
        return (entry["value"], entry["source"]) if entry else (None, None)

    value, source = _extract(player_name)
    if value is not None:
        return value, source
    close = difflib.get_close_matches(player_name, list(cache.keys()), n=1, cutoff=0.85)
    if close:
        return _extract(close[0])
    return None, None
