"""
data_fetcher.py
Queries local SQLite (populated once by database.run_etl) instead of nba_api.
All function signatures are identical to the previous version so predictor.py
and main.py require no changes beyond the startup call.
"""

import sqlite3
import difflib
import datetime as dt
import httpx
import pandas as pd

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
    game_type: str = "Regular Season",
) -> pd.DataFrame:
    """
    Return a DataFrame of the player's last `last_n` games of `game_type`.

    Columns are renamed to match predictor.py's expectations:
      GAME_DATE, MATCHUP, PTS, REB, AST, FG3M, MIN, PLUS_MINUS, WL
    MATCHUP is built as '<TEAM_ABBREV> vs. <OPP_ABBREV>' so that
    predictor.py's str.contains(opp_abbrev) filter works correctly.
    """
    player_id = get_player_id(player_name)
    if player_id is None:
        return pd.DataFrame()

    conn = get_db()
    rows = conn.execute(
        """
        SELECT gameDateTimeEst,
               playerteamName, playerteamCity,
               opponentteamName, opponentteamCity,
               points, assists, reboundsTotal, threePointersMade,
               numMinutes, win, plusMinusPoints,
               estimatedPace, usagePercentage, defensiveRating
        FROM player_stats
        WHERE personId = ? AND gameType = ?
        ORDER BY gameDateTimeEst DESC
        LIMIT ?
        """,
        (player_id, game_type, last_n),
    ).fetchall()
    conn.close()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame([dict(r) for r in rows])

    df = df.rename(columns={
        "gameDateTimeEst":   "GAME_DATE",
        "points":            "PTS",
        "assists":           "AST",
        "reboundsTotal":     "REB",
        "threePointersMade": "FG3M",
        "numMinutes":        "MIN",
        "plusMinusPoints":   "PLUS_MINUS",
        "win":               "WL",
        "estimatedPace":     "PACE",
        "usagePercentage":   "USG",
        "defensiveRating":   "DEF_RTG",
    })

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

    # Next scheduled game after right now (UTC)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
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

    conn.close()

    return {
        "player_team_full":     team_full,
        "player_team_abbrev":   team_abbrev,
        "next_opponent_full":   opp_full,
        "next_opponent_abbrev": opp_abbrev,
        "is_home":              is_home,
        "is_back_to_back":      b2b,
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
