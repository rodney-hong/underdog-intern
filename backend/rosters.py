"""
rosters.py
Current NBA rosters from ESPN's public team endpoints (free; not OddsAPI):
    teams list   ESPN_TEAMS_URL
    one roster   ESPN_ROSTER_URL.format(espn_team_id=...)

refresh_rosters() runs at startup and in the morning maintenance job. Each team that
fetched successfully replaces its own rows, and any row for the same athletes on
other teams (a trade moves the player); a team whose fetch failed keeps its previous
rows. ESPN teams are mapped to NBA team ids and names through the `schedule` table
by nickname, so a roster team reads exactly like a box-score team.

current_team() is the player's current team for data_fetcher.get_player_context (the
app and paper_predictions). It returns None, so callers fall back to the box-score
team, when the player is not on any roster, the name matches more than one team, or
the matching rows are older than ROSTER_MAX_AGE_HOURS.
"""

import re
import sqlite3
import unicodedata
import datetime as dt

import httpx

from database import DB_PATH

ESPN_TEAMS_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams"
ESPN_ROSTER_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{espn_team_id}/roster"
ROSTER_MAX_AGE_HOURS = 48   # older rows are ignored (box-score team is used instead)

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_tables(conn) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS rosters (
            athlete_id     TEXT PRIMARY KEY,   -- ESPN athlete id
            player_name    TEXT NOT NULL,      -- ESPN displayName
            name_key       TEXT NOT NULL,      -- name_key(player_name)
            espn_team_id   TEXT NOT NULL,
            team_abbrev    TEXT,               -- ESPN abbreviation
            nba_team_id    INTEGER NOT NULL,   -- schedule.homeTeamId
            team_city      TEXT NOT NULL,      -- schedule.homeTeamCity
            team_name      TEXT NOT NULL,      -- schedule.homeTeamName (nickname)
            position       TEXT,
            jersey         TEXT,
            season         TEXT,               -- ESPN season displayName, e.g. 2026-27
            fetched_at     TEXT NOT NULL       -- UTC ISO
        );
        CREATE INDEX IF NOT EXISTS idx_rosters_name_key ON rosters (name_key);
    """)
    conn.commit()


def name_key(name: str) -> str:
    """Accent-, case- and punctuation-free name without Jr./II-style suffixes."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[.'’\-]", "", s.lower())
    words = [w for w in s.split() if w not in _SUFFIXES]
    return " ".join(words)


def _nba_teams(conn) -> dict:
    """{nickname: (nba_team_id, city)} from the schedule table."""
    return {r["homeTeamName"]: (int(r["homeTeamId"]), r["homeTeamCity"])
            for r in conn.execute("SELECT DISTINCT homeTeamId, homeTeamCity, homeTeamName "
                                  "FROM schedule WHERE homeTeamId != 0")}


def _athletes(payload: dict) -> list[dict]:
    """Roster athletes; flattens the grouped-by-position shape if ESPN returns it."""
    out = []
    for a in payload.get("athletes", []):
        out.extend(a["items"] if isinstance(a, dict) and "items" in a else [a])
    return out


def refresh_rosters(client: httpx.Client | None = None) -> dict:
    """
    Fetch all 30 rosters and store them (see module docstring). Raises if the teams
    list cannot be fetched or a team cannot be mapped to the schedule; a failed
    single-team fetch is reported and that team keeps its previous rows.
    """
    own = client is None
    client = client or httpx.Client(timeout=10)
    try:
        resp = client.get(ESPN_TEAMS_URL)
        resp.raise_for_status()
        teams = [t["team"] for t in resp.json()["sports"][0]["leagues"][0]["teams"]]
        fetched, failed = {}, []
        for t in teams:
            try:
                r = client.get(ESPN_ROSTER_URL.format(espn_team_id=t["id"]))
                r.raise_for_status()
                fetched[t["id"]] = (t, r.json())
            except Exception as exc:
                failed.append(f"{t.get('abbreviation')}: {exc!r}"[:120])
    finally:
        if own:
            client.close()

    now = dt.datetime.now(dt.timezone.utc).isoformat()
    conn = get_db()
    init_tables(conn)
    nba = _nba_teams(conn)
    unmapped = [t["name"] for t, _ in fetched.values() if t["name"] not in nba]
    if unmapped:
        conn.close()
        raise RuntimeError(f"ESPN teams not in schedule: {unmapped}")
    n = 0
    try:
        for espn_id, (t, payload) in fetched.items():
            nba_id, city = nba[t["name"]]
            season = (payload.get("season") or {}).get("displayName")
            rows = [(str(a["id"]), a.get("displayName") or a.get("fullName") or "",
                     name_key(a.get("displayName") or a.get("fullName") or ""), str(espn_id),
                     t.get("abbreviation"), nba_id, city, t["name"],
                     (a.get("position") or {}).get("abbreviation"), a.get("jersey"), season, now)
                    for a in _athletes(payload) if a.get("id")]
            ids = [r[0] for r in rows]
            conn.execute("DELETE FROM rosters WHERE espn_team_id = ?", (str(espn_id),))
            if ids:
                conn.execute(f"DELETE FROM rosters WHERE athlete_id IN ({','.join('?' * len(ids))})", ids)
            conn.executemany("INSERT INTO rosters VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            n += len(rows)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    out = {"teams_ok": len(fetched), "teams_failed": failed, "players": n}
    print(f"[rosters] refreshed: {out}")
    return out


def current_team(player_name: str, person_id: int | None = None) -> dict | None:
    """
    {"team_id", "team_city", "team_name", "team_full"} for the player's current team,
    matched by name_key on the given name and, if person_id is given, on the
    players-table name for that id. None when not found, ambiguous or stale.
    """
    conn = get_db()
    try:
        keys = {name_key(player_name)}
        if person_id is not None:
            p = conn.execute("SELECT firstName, lastName FROM players WHERE personId = ?",
                             (person_id,)).fetchone()
            if p:
                keys.add(name_key(f"{p['firstName']} {p['lastName']}"))
        keys.discard("")
        if not keys:
            return None
        cutoff = (dt.datetime.now(dt.timezone.utc)
                  - dt.timedelta(hours=ROSTER_MAX_AGE_HOURS)).isoformat()
        rows = conn.execute(
            f"SELECT DISTINCT nba_team_id, team_city, team_name FROM rosters "
            f"WHERE name_key IN ({','.join('?' * len(keys))}) AND fetched_at >= ?",
            (*keys, cutoff)).fetchall()
    except sqlite3.OperationalError:            # no rosters table yet
        return None
    finally:
        conn.close()
    if len(rows) != 1:
        return None
    r = rows[0]
    return {"team_id": int(r["nba_team_id"]), "team_city": r["team_city"],
            "team_name": r["team_name"], "team_full": f"{r['team_city']} {r['team_name']}"}
