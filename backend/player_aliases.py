"""
player_aliases.py
Name aliases for the screenshot import only (app path). get_player_id, player_match's
lookups and the paper path never read this table.

  player_aliases   alias_key (rosters.name_key of the card's name) -> person_id, with the
                   player's team when it was saved, source 'seed' (SEEDS) or 'user' (learned
                   when an unmatched screenshot row is resolved by picking a player).

Resolution (screenshot_import.resolve_player): exact name_key + card team first, as before;
only then an alias, and only when the alias's player is on the card's team now (ESPN
roster via espn_person_map, else his latest box score). No fuzzy matching. An alias whose
name is in player_name_collisions, or is some roster player's exact name, is ignored and is
never learned.

Remove a wrong alias: DELETE /screenshot/alias?name=...&person_id=... (local only), or
    python player_aliases.py --list
    python player_aliases.py --delete "Deuce McBride" [--person-id 1630540]
"""

import datetime as dt
import sqlite3
import sys

from rosters import name_key

# Pick'em names that are not a roster name (checked 2026-10-25 against every Underdog name
# in line_snapshots: the only unresolved ones, each with exactly one same-team candidate).
SEEDS = [("Deuce McBride", 1630540, "Knicks"),        # Miles McBride
         ("Herb Jones", 1630529, "Pelicans"),          # Herbert Jones
         ("Ron Holland", 1641842, "Pistons"),          # Ronald Holland II
         ("Lu Dort", 1629652, "Hawks")]                # Luguentz Dort (added 2026-10-26)

_DDL = """
    CREATE TABLE IF NOT EXISTS player_aliases (
        alias_key    TEXT NOT NULL,        -- rosters.name_key(alias_name)
        alias_name   TEXT NOT NULL,        -- as the card shows it
        person_id    INTEGER NOT NULL,
        team         TEXT,                 -- team nickname when saved
        source       TEXT NOT NULL CHECK (source IN ('seed', 'user')),
        created_at   TEXT NOT NULL,
        PRIMARY KEY (alias_key, person_id)
    )"""


def init_table(conn) -> None:
    conn.execute(_DDL)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    conn.executemany("INSERT OR IGNORE INTO player_aliases VALUES (?, ?, ?, ?, 'seed', ?)",
                     [(name_key(n), n, pid, team, now) for n, pid, team in SEEDS])
    conn.commit()


def _collides(conn, key: str) -> bool:
    import player_match
    return bool(player_match.collides_with(conn, key))


def _is_roster_name(conn, key: str) -> bool:
    try:
        return conn.execute("SELECT 1 FROM rosters WHERE name_key = ? LIMIT 1", (key,)).fetchone() is not None
    except sqlite3.OperationalError:
        return False


def current_teams(conn, person_id: int) -> set:
    """Team nicknames the player is on now: ESPN roster (espn_person_map), else latest box score."""
    try:
        teams = {r[0] for r in conn.execute(
            "SELECT team_name FROM espn_person_map WHERE person_id = ? AND status = 'ok'", (int(person_id),))}
    except sqlite3.OperationalError:
        teams = set()
    if not teams:
        r = conn.execute("SELECT playerteamName FROM player_stats WHERE personId = ? AND playerteamName IS NOT NULL "
                         "ORDER BY gameDateTimeEst DESC LIMIT 1", (int(person_id),)).fetchone()
        teams = {r[0]} if r else set()
    return teams


def card_team_nicknames(conn, team: str | None) -> set:
    """Team nicknames a card's team text (ESPN/NBA abbreviation, nickname, full name) names."""
    from screenshot_import import _team_names
    if not team:
        return set()
    t = team.upper()
    out = set()
    for cols in ("team_abbrev, team_city, team_name, nba_team_id", "team_abbrev, team_city, team_name"):
        try:
            rows = conn.execute(f"SELECT DISTINCT {cols} FROM rosters").fetchall()
            break
        except sqlite3.OperationalError:
            rows = []
    for r in rows:
        h = dict(zip(("team_abbrev", "team_city", "team_name", "nba_team_id"), r))
        if t in _team_names(h):
            out.add(h["team_name"])
    return out


def _display(conn, person_id: int, team: str) -> dict:
    """The roster's name and team for the player (what /predict and the table show)."""
    try:
        r = conn.execute("SELECT m.player_name, r.team_abbrev, r.team_city, r.team_name FROM espn_person_map m "
                         "JOIN rosters r ON r.athlete_id = m.athlete_id WHERE m.person_id = ? AND m.status = 'ok' "
                         "AND r.team_name = ? LIMIT 1", (int(person_id), team)).fetchone()
    except sqlite3.OperationalError:
        r = None
    if r:
        return {"player_name": r[0], "team_abbrev": r[1], "team_name": f"{r[2]} {r[3]}"}
    p = conn.execute("SELECT firstName || ' ' || lastName FROM players WHERE personId = ? LIMIT 1",
                     (int(person_id),)).fetchone()
    return {"player_name": p[0] if p else "", "team_abbrev": None, "team_name": team}


def resolve(conn, name: str, team: str | None) -> dict | None:
    """
    {"status": "ok", "player_name", "team_abbrev", "team_name", "person_id", "via": "alias"} when
    exactly one alias for this name has its player on the card's team now (with no team on the
    card: on the team stored with the alias). None otherwise, including collision names.
    """
    key = name_key(name)
    try:
        rows = conn.execute("SELECT person_id, team FROM player_aliases WHERE alias_key = ?", (key,)).fetchall()
    except sqlite3.OperationalError:
        return None
    if not rows or _collides(conn, key) or _is_roster_name(conn, key):
        return None
    card = card_team_nicknames(conn, team)
    hits = []
    for pid, saved in rows:
        now = current_teams(conn, pid)
        agree = (now & card) if team else (now & {saved})
        if agree:
            hits.append((pid, sorted(agree)[0]))
    if len(hits) != 1:
        return None
    pid, t = hits[0]
    return {"status": "ok", **_display(conn, pid, t), "person_id": int(pid), "via": "alias"}


def learn(conn, name: str, person_id: int) -> dict:
    """Save name -> person_id as source 'user' (a pick in an unmatched screenshot row), unless the
    name collides, is a roster player's exact name, or already names this player."""
    key = name_key(name)
    if not key:
        return {"saved": False, "reason": "empty name"}
    if _collides(conn, key):
        return {"saved": False, "reason": "name collision"}
    if _is_roster_name(conn, key):
        return {"saved": False, "reason": "a roster player's exact name"}
    names = {name_key(r[0]) for r in conn.execute(
        "SELECT firstName || ' ' || lastName FROM players WHERE personId = ? UNION "
        "SELECT DISTINCT firstName || ' ' || lastName FROM player_stats WHERE personId = ?",
        (int(person_id), int(person_id)))}
    if key in names:
        return {"saved": False, "reason": "already his name"}
    teams = current_teams(conn, person_id)
    cur = conn.execute("INSERT OR IGNORE INTO player_aliases VALUES (?, ?, ?, ?, 'user', ?)",
                       (key, name.strip(), int(person_id), sorted(teams)[0] if teams else None,
                        dt.datetime.now(dt.timezone.utc).isoformat()))
    conn.commit()
    return {"saved": cur.rowcount == 1, "reason": None if cur.rowcount else "already saved"}


def delete(conn, name: str, person_id: int | None = None) -> int:
    key = name_key(name)
    if person_id is None:
        cur = conn.execute("DELETE FROM player_aliases WHERE alias_key = ?", (key,))
    else:
        cur = conn.execute("DELETE FROM player_aliases WHERE alias_key = ? AND person_id = ?", (key, int(person_id)))
    conn.commit()
    return cur.rowcount


def _main(argv: list[str]) -> int:
    import argparse
    import database
    ap = argparse.ArgumentParser(description="List or delete screenshot-import player aliases.")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--delete", metavar="NAME")
    ap.add_argument("--person-id", type=int)
    a = ap.parse_args(argv)
    conn = sqlite3.connect(database.DB_PATH, timeout=30)
    try:
        init_table(conn)
        if a.delete:
            print(f"deleted {delete(conn, a.delete, a.person_id)} alias row(s) for {a.delete!r}")
        for r in conn.execute("SELECT alias_name, person_id, team, source, created_at FROM player_aliases "
                              "ORDER BY source, alias_key"):
            print(" | ".join(str(x) for x in r))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
