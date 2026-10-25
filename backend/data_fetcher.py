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
import line_logger
import game_types
import rosters


# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
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

# NBA franchise ids (player_stats.playerteamId / rosters.nba_team_id): 1610612737..1610612766.
NBA_TEAM_ID_MIN, NBA_TEAM_ID_MAX = 1610612737, 1610612766
SEARCH_RECENT_DAYS = 365          # an NBA regular-season game this recently keeps a player searchable
SEARCH_MINUTES_DAYS = 30          # ranking: minutes played over this window


def search_players(query: str, limit: int = 10) -> list[dict]:
    """
    Up to `limit` current NBA players matching `query` (case-insensitive substring of the
    name, an ESPN roster name or a player_aliases alias, e.g. "deuce" -> Miles McBride), as
    {"name", "person_id", "team"} so same-named players can be told apart.
      current   on a current ESPN NBA roster (espn_person_map), or at least one NBA
                regular-season game in the last SEARCH_RECENT_DAYS; players with only
                preseason/exhibition rows or a non-NBA team (Melbourne United) are left out
      ranking   roster players first; then a first- or last-name prefix match before a
                substring-only match; then minutes over the last SEARCH_MINUTES_DAYS days
      team      the current roster team (rosters.current_team), else the latest NBA
                regular-season box-score team
    App path only (manual form, screenshot table); get_player_id is unchanged.
    """
    import datetime as dt
    q = query.strip().lower()
    if not q:
        return []
    like = f"%{q}%"
    today = dt.date.today()
    recent = (today - dt.timedelta(days=SEARCH_RECENT_DAYS)).isoformat()
    month = (today - dt.timedelta(days=SEARCH_MINUTES_DAYS)).isoformat()
    rs = "gameType IN ('Regular Season', 'NBA Emirates Cup')"
    nba = f"playerteamId BETWEEN {NBA_TEAM_ID_MIN} AND {NBA_TEAM_ID_MAX}"
    conn = get_db()
    try:
        names = {}                                   # person_id -> names it can be matched by
        for pid, first, last in conn.execute(
                "SELECT personId, firstName, lastName FROM players WHERE LOWER(firstName || ' ' || lastName) LIKE ?",
                (like,)):
            names.setdefault(int(pid), set()).add(f"{first} {last}")
        on_roster = {}                               # person_id -> roster display name
        try:
            for pid, rname in conn.execute(
                    f"SELECT m.person_id, r.player_name FROM espn_person_map m JOIN rosters r "
                    f"ON r.athlete_id = m.athlete_id WHERE m.status = 'ok' AND r.nba_team_id BETWEEN "
                    f"{NBA_TEAM_ID_MIN} AND {NBA_TEAM_ID_MAX}"):
                on_roster[int(pid)] = rname
                if q in rname.lower():
                    names.setdefault(int(pid), set()).add(rname)
        except sqlite3.OperationalError:             # no rosters / espn_person_map yet
            pass
        try:
            for pid, alias in conn.execute("SELECT person_id, alias_name FROM player_aliases WHERE "
                                           "LOWER(alias_name) LIKE ?", (like,)):
                names.setdefault(int(pid), set()).add(alias)
        except sqlite3.OperationalError:             # no alias table yet
            pass
        if not names:
            return []
        marks = ",".join("?" * len(names))
        ids = list(names)
        active = {r[0]: r[1] for r in conn.execute(
            f"SELECT personId, MAX(gameDateTimeEst) FROM player_stats WHERE personId IN ({marks}) AND {rs} "
            f"AND {nba} AND gameDateTimeEst >= ? GROUP BY personId", (*ids, recent))}
        minutes = {r[0]: r[1] or 0.0 for r in conn.execute(
            f"SELECT personId, SUM(numMinutes) FROM player_stats WHERE personId IN ({marks}) AND {rs} "
            f"AND {nba} AND gameDateTimeEst >= ? GROUP BY personId", (*ids, month))}
        current = [pid for pid in ids if pid in on_roster or pid in active]
        display = {r[0]: (r[1], r[2]) for r in conn.execute(
            f"SELECT personId, firstName, lastName FROM players WHERE personId IN ({marks})", ids)}
        box_team = {}
        for pid in current:
            r = conn.execute(f"SELECT playerteamCity || ' ' || playerteamName FROM player_stats WHERE personId = ? "
                             f"AND {rs} AND {nba} ORDER BY gameDateTimeEst DESC LIMIT 1", (pid,)).fetchone()
            box_team[pid] = r[0] if r else None
    finally:
        conn.close()

    def prefix(pid):
        return any(part.startswith(q) for n in names[pid] for part in n.lower().split())

    current.sort(key=lambda pid: (pid not in on_roster, not prefix(pid), -minutes.get(pid, 0.0)))
    out = []
    for pid in current[:limit]:
        first_last = display.get(pid)
        name = f"{first_last[0]} {first_last[1]}" if first_last else on_roster[pid]
        roster = rosters.current_team(name, pid)
        out.append({"name": name, "person_id": pid, "team": roster["team_full"] if roster else box_team.get(pid)})
    return out


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
    Return a DataFrame of the player's last `last_n` COUNTED games (game_types.py:
    no preseason, All-Star or Cup final) that he played (numMinutes > 0), matching
    the rows ml_trainer trains on.

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
        WHERE personId = ? AND gameType IN ({game_types.COUNTED_SQL})
          AND numMinutes IS NOT NULL AND numMinutes > 0
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

# Base stat columns, and combo sums written in the same order as ml_trainer's
# _sum_* columns, so inference reproduces training's float arithmetic exactly.
_HISTORY_STAT_EXPR = {
    "Points":   lambda h: h["points"],
    "Rebounds": lambda h: h["reboundsTotal"],
    "Assists":  lambda h: h["assists"],
    "3PM":      lambda h: h["threePointersMade"],
    "PRA":      lambda h: h["points"] + h["reboundsTotal"] + h["assists"],
    "PR":       lambda h: h["points"] + h["reboundsTotal"],
    "PA":       lambda h: h["points"] + h["assists"],
    "RA":       lambda h: h["reboundsTotal"] + h["assists"],
}


def get_player_history(player_name: str, stat_type: str) -> pd.DataFrame:
    """
    The player's full counted-game history, chronological, with the same row filters
    as ml_trainer.build_training_data (numMinutes > 0, counted game types, valid date):
    columns gameDateTimeEst, opponentteamId, _stat. Empty if unknown player/stat.
    """
    expr = _HISTORY_STAT_EXPR.get(stat_type)
    player_id = get_player_id(player_name)
    if expr is None or player_id is None:
        return pd.DataFrame()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    h = pd.read_sql_query(
        f"SELECT gameDateTimeEst, opponentteamId, points, reboundsTotal, assists, threePointersMade "
        f"FROM player_stats WHERE personId = ? AND numMinutes IS NOT NULL AND numMinutes > 0 "
        f"AND gameType IN ({game_types.COUNTED_SQL})",
        conn, params=(player_id,))
    conn.close()
    h["gameDateTimeEst"] = pd.to_datetime(h["gameDateTimeEst"], errors="coerce")
    h = h.dropna(subset=["gameDateTimeEst"]).sort_values("gameDateTimeEst").reset_index(drop=True)
    h["_stat"] = expr(h)
    return h[["gameDateTimeEst", "opponentteamId", "_stat"]]


def get_player_history_v2(person_id: int) -> pd.DataFrame:
    """
    The rows ml_trainer.build_training_data keeps for one player (counted game types,
    numMinutes > 0, valid date), in gameDateTimeEst order, with the columns the v2
    feature_defs helpers read: gameDateTimeEst, home, numMinutes and the four stats.
    """
    conn = sqlite3.connect(DB_PATH, timeout=30)
    h = pd.read_sql_query(
        f"SELECT gameDateTimeEst, home, numMinutes, points, reboundsTotal, assists, threePointersMade "
        f"FROM player_stats WHERE personId = ? AND numMinutes IS NOT NULL AND numMinutes > 0 "
        f"AND gameType IN ({game_types.COUNTED_SQL})",
        conn, params=(int(person_id),))
    conn.close()
    h["gameDateTimeEst"] = pd.to_datetime(h["gameDateTimeEst"], errors="coerce")
    return h.dropna(subset=["gameDateTimeEst"]).sort_values("gameDateTimeEst").reset_index(drop=True)


def get_team_rows_v2(team_id: int, before: pd.Timestamp) -> pd.DataFrame:
    """
    One team's player rows before `before` with training's filters, in training's
    row order (personId, gameDateTimeEst), so per-game means sum in the same order.
    No team id (a few historical rows lack opponentteamId): no rows, as training's
    groupby drops missing team ids, so the feature is NaN there too.
    """
    if team_id is None or pd.isna(team_id):
        return pd.DataFrame({"personId": pd.Series(dtype="int64"),
                             "gameDateTimeEst": pd.Series(dtype="datetime64[ns]"),
                             "defensiveRating": pd.Series(dtype="float64")})
    conn = sqlite3.connect(DB_PATH, timeout=30)
    t = pd.read_sql_query(
        f"SELECT personId, gameDateTimeEst, defensiveRating FROM player_stats "
        f"WHERE playerteamId = ? AND numMinutes IS NOT NULL AND numMinutes > 0 "
        f"AND gameType IN ({game_types.COUNTED_SQL}) AND gameDateTimeEst < ?",
        conn, params=(int(team_id), before.strftime("%Y-%m-%d %H:%M:%S")))
    conn.close()
    t["gameDateTimeEst"] = pd.to_datetime(t["gameDateTimeEst"], errors="coerce")
    return t.dropna(subset=["gameDateTimeEst"]).sort_values(
        ["personId", "gameDateTimeEst"]).reset_index(drop=True)


def scheduled_tip(team_a: str, team_b: str, on_or_after: str) -> pd.Timestamp | None:
    """
    gameDateTimeEst (ET, the time training anchors its windows to) of the first
    scheduled game between the two teams on or after `on_or_after` ('YYYY-MM-DD HH:MM:SS'
    or a date), or None. OddsAPI's commence_time is usually ~10 min later.
    """
    nick = lambda t: "Trail Blazers" if "Blazers" in t else t.split()[-1]
    a, b = nick(team_a), nick(team_b)
    conn = get_db()
    row = conn.execute(
        "SELECT gameDateTimeEst FROM schedule WHERE gameDateTimeEst >= ? "
        "AND ((homeTeamName = ? AND awayTeamName = ?) OR (homeTeamName = ? AND awayTeamName = ?)) "
        "ORDER BY gameDateTimeEst LIMIT 1", (on_or_after, a, b, b, a)).fetchone()
    conn.close()
    return pd.Timestamp(row[0]) if row else None


def team_id_for(team_name: str):
    """NBA team id for a full team name, from the schedule table (None if unknown)."""
    nickname = "Trail Blazers" if "Blazers" in team_name else team_name.split()[-1]
    conn = get_db()
    row = conn.execute("SELECT homeTeamId FROM schedule WHERE homeTeamName = ? LIMIT 1",
                       (nickname,)).fetchone()
    conn.close()
    return row[0] if row else None


def is_playoff_matchup(opponent_team: str) -> int:
    """
    1 if the next scheduled game against `opponent_team` (today or later, ET) is a
    playoff game, else 0. Mirrors ml_trainer's is_playoff (gameType == 'Playoffs');
    the schedule has no gameType, so it is read from the gameId type digit.
    """
    nickname = "Trail Blazers" if "Blazers" in opponent_team else opponent_team.split()[-1]
    today = dt.datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    conn = get_db()
    row = conn.execute(
        "SELECT gameId FROM schedule WHERE (homeTeamName = ? OR awayTeamName = ?) "
        "AND substr(gameDateTimeEst, 1, 10) >= ? ORDER BY gameDateTimeEst LIMIT 1",
        (nickname, nickname, today)).fetchone()
    conn.close()
    return int(bool(row) and game_types.schedule_game_is_playoff(row[0]))


def get_team_def_rating_last5(team_name: str) -> float:
    """
    Return the team's average defensive rating across their last 5 distinct counted
    games (game_types.py), as in ml_trainer's opp_def_rating_last5.
    Falls back to 108.0 (approximate NBA league average) if no data is found.
    playerteamName in player_stats stores only the nickname ('Timberwolves', not
    'Minnesota Timberwolves'), so extract the last word of the full name to match.
    'Trail Blazers' is a two-word nickname and is handled as a special case.
    """
    nickname = "Trail Blazers" if "Blazers" in team_name else team_name.split()[-1]
    conn = get_db()
    row = conn.execute(
        f"""
        SELECT AVG(avg_def) FROM (
            SELECT gameDateTimeEst, AVG(defensiveRating) AS avg_def
            FROM player_stats
            WHERE playerteamName = ? AND gameType IN ({game_types.COUNTED_SQL})
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

NEXT_GAME_GRACE_HOURS = 3   # a game stays the player's "next game" until 3 h after tip

def get_player_context(player_name: str) -> dict | None:
    """
    Returns team context for the selected player:
      player_team_full / abbrev, team_source, next_opponent_full / abbrev,
      is_home, is_back_to_back, next_game_date
    Current team: today's ESPN roster (rosters.current_team), else the team of the
    player's most recent box score (team_source 'roster' / 'box_score').
    Returns None if the player cannot be found or has no game log data.
    """
    player_id = get_player_id(player_name)
    if player_id is None:
        return None

    conn = get_db()

    # Most recent game in player_stats: the player must have one; its team is the
    # fallback when no current roster row matches.
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

    roster = rosters.current_team(player_name, player_id)
    if roster is not None:
        team_id, team_full, team_source = roster["team_id"], roster["team_full"], "roster"
    elif team_row["playerteamId"] is None:
        # e.g. an unsigned veteran whose last row is a preseason game with no team id
        conn.close()
        return None
    else:
        team_id     = int(team_row["playerteamId"])
        team_full   = f"{team_row['playerteamCity']} {team_row['playerteamName']}"
        team_source = "box_score"
    team_abbrev = full_to_abbrev(team_full)

    # Next scheduled game. schedule.gameDateTimeEst is ET wall-clock text
    # ('YYYY-MM-DD HH:MM:SS'), so compare against the current ET time in that same
    # format. Today's game stays selectable until NEXT_GAME_GRACE_HOURS after tip;
    # a game from an earlier ET date never does.
    now_et = dt.datetime.now(ZoneInfo("America/New_York"))
    cutoff = (now_et - dt.timedelta(hours=NEXT_GAME_GRACE_HOURS)).strftime('%Y-%m-%d %H:%M:%S')
    game_row = conn.execute(
        """
        SELECT gameDateTimeEst, homeTeamId,
               homeTeamName, homeTeamCity,
               awayTeamName, awayTeamCity
        FROM schedule
        WHERE (CAST(homeTeamId AS INTEGER) = CAST(? AS INTEGER)
               OR CAST(awayTeamId AS INTEGER) = CAST(? AS INTEGER))
          AND gameDateTimeEst > ?
          AND substr(gameDateTimeEst, 1, 10) >= ?
          AND homeTeamId != 0
        ORDER BY gameDateTimeEst ASC
        LIMIT 1
        """,
        (team_id, team_id, cutoff, now_et.date().isoformat()),
    ).fetchone()

    if not game_row:
        conn.close()
        return {
            "player_team_full":     team_full,
            "player_team_abbrev":   team_abbrev,
            "team_source":          team_source,
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
        "team_source":          team_source,
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


def _espn_injury_for(player_name: str) -> tuple[dict | None, str | None]:
    """
    (ESPN injury entry, None) for this player, matched by ESPN athlete id through
    player_match.espn_person_map, never by name; (None, flag) when the name only
    resolves fuzzily onto a collision; (None, None) when he has no entry.
    ESPN groups the feed by team: [{"id", "displayName", "injuries": [entry, ...]}].
    """
    import player_match
    conn = get_db()
    try:
        who = player_match.person_for_name(conn, player_name, get_player_id(player_name))
        if who["status"] == "ambiguous":
            return None, who["flag"]
        if who["status"] != "ok":
            return None, None
        ids = player_match.athlete_ids_for(conn, who["person_id"])
    finally:
        conn.close()
    if not ids:
        return None, None              # no ESPN athlete tied to him (not on a roster, or flagged)
    for team in _fetch_espn_injuries():
        for entry in team.get("injuries", []):
            if player_match.espn_athlete_id(entry.get("athlete", {})) in ids:
                return entry, None
    return None, None


def get_injury_context(player_name: str) -> str:
    """Return a short injury note for the player ('status: detail'), or '' if none found."""
    try:
        entry, ambiguous = _espn_injury_for(player_name)
        if ambiguous:
            import player_match
            player_match.flag(f"injury context: {ambiguous}")
            return ""
        if entry:
            status = entry.get("status", "")
            detail = entry.get("shortComment") or entry.get("longComment", "")
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
    """
    {"status", "reason"} from ESPN, matched by athlete id (player_match), or None.
    {"status": "ambiguous", "reason": <flag>} when the name only resolves fuzzily onto
    a colliding name: shown as such instead of another player's injury.
    """
    try:
        entry, ambiguous = _espn_injury_for(player_name)
        if ambiguous:
            import player_match
            player_match.flag(f"injury status: {ambiguous}")
            return {"status": "ambiguous", "reason": ambiguous}
        if entry is None:
            return None
        return {"status": entry.get("status", ""), "reason": entry.get("shortComment", "")}
    except Exception:
        return None


def get_opponent_injuries(opponent_team: str) -> list[dict]:
    """Out / Questionable players of opponent_team, matching ESPN's team by its ESPN id
    (rosters.espn_team_id), else its exact display name; never by fuzzy name."""
    try:
        teams = _fetch_espn_injuries()
        nickname = "Trail Blazers" if "Blazers" in opponent_team else opponent_team.split()[-1]
        conn = get_db()
        try:
            row = conn.execute("SELECT espn_team_id FROM rosters WHERE team_name = ? LIMIT 1",
                               (nickname,)).fetchone()
        except sqlite3.OperationalError:
            row = None
        finally:
            conn.close()
        matched_team = next((t for t in teams if row and str(t.get("id")) == str(row[0])), None) \
            or next((t for t in teams if t.get("displayName") == opponent_team), None)
        if matched_team is None:
            return []
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
# The NBA slip path makes no OddsAPI calls (see get_todays_lines); the URLs, books and
# markets below are used by the WNBA path only.
_PREFERRED_BOOKS = ["underdog"]
# Only the markets Underdog actually carries (confirmed via check_underdog_markets.py,
# WNBA slate 2026-08-25). Underdog carries the combo markets (PR/PA/RA/PRA) but NOT
# threes, steals, blocks, or blocks+steals — requesting those risks a 422 on the whole
# props call, so they are deliberately omitted here and from _STAT_TYPE_MAP below.
_PROP_MARKETS    = "player_points,player_rebounds,player_assists,player_points_rebounds,player_points_assists,player_rebounds_assists,player_points_rebounds_assists"
_STAT_TYPE_MAP   = {
    "Points":   "player_points",
    "Rebounds": "player_rebounds",
    "Assists":  "player_assists",
    "PR":       "player_points_rebounds",
    "PA":       "player_points_assists",
    "RA":       "player_rebounds_assists",
    "PRA":      "player_points_rebounds_assists",
}
# NBA lookups add 3PM: its lines come only from line_snapshots (line_logger logs
# player_threes), never from the WNBA props call above.
_NBA_STAT_TYPE_MAP = {**_STAT_TYPE_MAP, "3PM": "player_threes"}
_CACHE_TTL = 45 * 60  # 45 minutes — DFS lines move with user selections

_lines_cache: dict = {}
_lines_cache_ts: float = 0.0


def _log_underdog_lines(player_lines: dict, game_date: str, league: str) -> None:
    """Persist every line pulled into underdog_lines for future model training."""
    if not player_lines:
        return
    captured_at = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        conn = get_db()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS underdog_lines (
                player_name TEXT, market_key TEXT, stat_line REAL,
                game_date TEXT, game_id TEXT, home_team TEXT, away_team TEXT,
                league TEXT, captured_at TEXT,
                PRIMARY KEY (player_name, market_key, game_date, league)
            )
        """)
        for player, entry in player_lines.items():
            for market_key, val in entry.items():
                if not isinstance(val, dict) or "value" not in val:
                    continue
                conn.execute("""
                    INSERT OR REPLACE INTO underdog_lines
                    (player_name, market_key, stat_line, game_date, game_id,
                     home_team, away_team, league, captured_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (player, market_key, val["value"], game_date,
                      entry.get("game_id"), entry.get("home_team"),
                      entry.get("away_team"), league, captured_at))
        conn.commit()
        conn.close()
    except Exception:
        pass  # non-fatal


def get_todays_lines() -> dict:
    """
    Return today's NBA player prop lines keyed by player name:
      {
        "Cade Cunningham": {
          "player_points": {"value": 24.5, "source": "underdog"},
          "game_id": "...", "home_team": "...", "away_team": "..."
        }
      }
    Makes NO OddsAPI calls (the NBA slip path spends no credits; line_logger's
    scheduled pre-tip pull is the only NBA OddsAPI consumer). Fallback order:
      1. today's logged Underdog lines (line_snapshots via
         line_logger.todays_underdog_lines; Points, Rebounds, Assists, 3PM);
      2. today's cached lines (in-memory, then the SQLite odds_cache row), stale is fine;
      3. {} -> the slip form falls back to manual line entry.
    """
    global _lines_cache, _lines_cache_ts

    eastern  = ZoneInfo("America/New_York")
    today_et = dt.datetime.now(eastern).date()

    # 1. Logged Underdog lines (line_snapshots)
    try:
        logged = line_logger.todays_underdog_lines(today_et.isoformat())
        if logged:
            return logged
    except Exception:
        pass

    # 2a. In-memory cache from today (ET)
    if _lines_cache and dt.datetime.fromtimestamp(_lines_cache_ts, tz=eastern).date() == today_et:
        return _lines_cache

    # 2b. SQLite odds_cache row from today (ET)
    try:
        conn = get_db()
        row = conn.execute("SELECT data, cached_at FROM odds_cache LIMIT 1").fetchone()
        conn.close()
        if row:
            cached_dt = dt.datetime.fromisoformat(row["cached_at"]).replace(tzinfo=dt.timezone.utc)
            if cached_dt.astimezone(eastern).date() == today_et:
                _lines_cache    = json.loads(row["data"])
                _lines_cache_ts = cached_dt.timestamp()
                return _lines_cache
    except Exception:
        pass  # missing table or corrupted row -> manual entry

    # 3. Nothing logged or cached today -> manual entry
    return {}


def get_player_line(player_name: str, stat_type: str) -> tuple[float | None, str | None]:
    """
    Return (line_value, bookmaker_key) for the given player and stat type,
    or (None, None) if unavailable, or (None, "ambiguous") when the name has no exact,
    team-confirmed match and collides with another player's name (flagged).
    Matching (player_match.match_name): exact rosters.name_key, and the line's game
    must include the player's current team. No fuzzy matching: it gave players other
    players' lines (audit of 2026-10-23).

    Lines come from get_todays_lines(), which makes no OddsAPI calls: today's
    logged Underdog lines (Points, Rebounds, Assists, 3PM, from line_logger) or
    today's cache. Other stat types usually return (None, None), i.e. manual entry.
    Stat types not in _NBA_STAT_TYPE_MAP (Steals, Blocks, Blocks+Steals) always
    return (None, None).
    """
    market_key = _NBA_STAT_TYPE_MAP.get(stat_type)
    if not market_key:
        return None, None

    cache = get_todays_lines()
    if not cache:
        return None, None

    def _extract(name: str) -> tuple[float | None, str | None]:
        entry = cache.get(name, {}).get(market_key)
        if entry:
            return entry["value"], entry["source"]
        return None, None

    import player_match
    nick = lambda t: ("Trail Blazers" if "Blazers" in t else t.split()[-1]) if t else ""
    ctx = get_player_context(player_name)
    team = nick(ctx["player_team_full"]) if ctx and ctx.get("player_team_full") else None
    teams_of = {n: {nick(v.get("home_team", "")), nick(v.get("away_team", ""))}
                for n, v in cache.items() if isinstance(v, dict)}
    conn = get_db()
    try:
        m = player_match.match_name(conn, player_name, list(cache), teams_of, team)
    finally:
        conn.close()
    if m["status"] == "ok":
        return _extract(m["name"])
    if m["status"] == "ambiguous":
        player_match.flag(f"line: {m['flag']}")
        return None, "ambiguous"
    return None, None


def missing_line_reason(player_name: str, stat_type: str) -> str:
    """
    Why get_player_line found no line, in words for the app: no logged stat type, no
    game today for the player's team, no pull yet for that game (with the next pull
    time), or a pull without an Underdog line for this player. Read-only; no API call.
    """
    if stat_type not in _NBA_STAT_TYPE_MAP or stat_type not in line_logger.STAT_MARKETS:
        return f"no lines are logged for {stat_type}; enter manually"
    ctx = get_player_context(player_name)
    team_full = ctx.get("player_team_full") if ctx else None
    if not team_full:
        return "player's current team not found; enter manually"
    nick = "Trail Blazers" if "Blazers" in team_full else team_full.split()[-1]
    eastern = ZoneInfo("America/New_York")
    now = dt.datetime.now(eastern)
    today = now.date().isoformat()
    conn = get_db()
    try:
        game = conn.execute(
            "SELECT gameDateTimeEst, awayTeamName, homeTeamName FROM schedule "
            "WHERE substr(gameDateTimeEst, 1, 10) = ? AND (homeTeamName = ? OR awayTeamName = ?) "
            "ORDER BY gameDateTimeEst LIMIT 1", (today, nick, nick)).fetchone()
        last = conn.execute(
            "SELECT MAX(captured_at) FROM line_snapshots WHERE game_date = ? "
            "AND (home_team LIKE ? OR away_team LIKE ?)", (today, f"%{nick}", f"%{nick}")).fetchone()[0]
    except sqlite3.Error:
        game, last = None, None
    finally:
        conn.close()
    if game is None:
        return f"no {nick} game on today's schedule; enter manually"
    tip = dt.datetime.fromisoformat(str(game[0])[:19].replace("T", " ")).replace(tzinfo=eastern)
    pretip = tip - dt.timedelta(minutes=line_logger.PRETIP_MINUTES)
    label = f"{game[1]} @ {game[2]}, {tip:%H:%M} ET"
    if last:
        at = dt.datetime.fromisoformat(last).astimezone(eastern)
        return (f"no Underdog {stat_type} line for {player_name} in the latest pull for this "
                f"game ({label}; pulled {at:%H:%M} ET); enter manually")
    if now < pretip:
        nxt = f"pre-tip pull at {pretip:%H:%M} ET"
        if line_logger.OPEN_PULL_ENABLED and now.hour < line_logger.OPEN_PULL_HOUR_ET:
            nxt = f"open pull at {line_logger.OPEN_PULL_HOUR_ET}:00 ET, {nxt}"
        return f"no lines logged yet for this game ({label}); {nxt}"
    if now < tip:
        return f"no lines logged yet for this game ({label}); pre-tip pull due now ({pretip:%H:%M} ET)"
    return f"no lines were logged for this game ({label}); it has started"


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
                params={"apiKey": _ODDS_API_KEY, "regions": "us_dfs",
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
        _log_underdog_lines(player_lines, today_et.isoformat(), "WNBA")
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
