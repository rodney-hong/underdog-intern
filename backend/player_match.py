"""
player_match.py
App-side player matching for third-party names (ESPN injuries, official injury report,
today's line names). Audit of 2026-10-23: league-wide difflib matching showed 18
players another player's injury and gave 3 players another player's line. Rules here:

  ids first      ESPN injury entries carry an athlete id (in the playercard link).
                 espn_person_map ties each ESPN roster athlete to one NBA personId:
                 same name_key, same team, exactly one candidate; anything else is
                 stored with status 'flag' and never used.
  names second   exact rosters.name_key (no difflib, no LIKE), and the team must agree
                 when the caller knows it.
  collisions     player_name_collisions lists pairs of active players a name could be
                 confused with (same name_key, same last name + first initial, edit
                 distance <= 2, same last name on the same team). A name lookup with
                 no exact, team-confirmed match that lands on a collision returns
                 'ambiguous' with a flag instead of guessing.

rebuild(conn) recomputes both tables; it runs after every roster refresh (startup and
morning maintenance). Nothing here feeds the frozen models or paper predictions.
"""

import itertools
import re
import sqlite3
import unicodedata

from rosters import name_key

ACTIVE_SINCE = "2025-10-01"     # box-score rows that make a player "active"
_ID_RE = re.compile(r"/id/(\d+)(?:/|$)")


def espn_athlete_id(athlete: dict) -> str | None:
    """ESPN athlete id from its playercard link (the injuries API has no id field)."""
    for link in (athlete or {}).get("links", []):
        if "playercard" in link.get("rel", []):
            m = _ID_RE.search(link.get("href", ""))
            if m:
                return m.group(1)
    return None


def athlete_id_from_url(url: str | None) -> str | None:
    m = _ID_RE.search(url or "")
    return m.group(1) if m else None


def _ascii_lower(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower().strip()


def _lev(a: str, b: str, cap: int = 2) -> int:
    """Levenshtein distance, stopping early once it exceeds cap."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

def init_tables(conn) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS espn_person_map (
            athlete_id   TEXT PRIMARY KEY,   -- ESPN athlete id (rosters.athlete_id)
            player_name  TEXT NOT NULL,      -- ESPN displayName
            team_name    TEXT NOT NULL,      -- roster team nickname
            person_id    INTEGER,            -- NBA personId when status = 'ok'
            status       TEXT NOT NULL,      -- 'ok' / 'flag'
            reason       TEXT                -- why flagged
        );
        CREATE INDEX IF NOT EXISTS idx_espn_person_map_pid ON espn_person_map (person_id);

        CREATE TABLE IF NOT EXISTS player_name_collisions (
            name_key_a TEXT NOT NULL, name_a TEXT NOT NULL, team_a TEXT,
            name_key_b TEXT NOT NULL, name_b TEXT NOT NULL, team_b TEXT,
            reasons    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_collisions_a ON player_name_collisions (name_key_a);
        CREATE INDEX IF NOT EXISTS idx_collisions_b ON player_name_collisions (name_key_b);
    """)
    conn.commit()


def _active_players(conn) -> dict:
    """{personId: {"names": set, "teams": set (this season's box-score teams), "team": latest}}."""
    out: dict = {}
    for pid, name, team, last in conn.execute(
            "SELECT personId, firstName || ' ' || lastName, playerteamName, MAX(gameDateTimeEst) "
            "FROM player_stats WHERE gameDateTimeEst >= ? AND personId IS NOT NULL "
            "GROUP BY personId, firstName, lastName, playerteamName", (ACTIVE_SINCE,)):
        p = out.setdefault(int(pid), {"names": set(), "teams": set(), "team": None, "_last": ""})
        p["names"].add(name)
        if team:
            p["teams"].add(team)
            if last > p["_last"]:
                p["_last"], p["team"] = last, team
    for pid, first, last in conn.execute("SELECT personId, firstName, lastName FROM players"):
        if pid is not None and int(pid) in out:
            out[int(pid)]["names"].add(f"{first} {last}")
    return out


def build_espn_person_map(conn, active: dict | None = None) -> dict:
    """
    One row per roster athlete: personId if exactly one active player has the same
    name_key and played for the roster team this season (or has it as latest team);
    otherwise status 'flag' with the reason. Returns counts.
    """
    active = active if active is not None else _active_players(conn)
    by_key: dict = {}
    for pid, p in active.items():
        for n in p["names"]:
            by_key.setdefault(name_key(n), set()).add(pid)
    rows, counts = [], {"ok": 0, "flag": 0}
    for aid, name, team in conn.execute("SELECT athlete_id, player_name, team_name FROM rosters"):
        same_name = by_key.get(name_key(name), set())
        cands = [pid for pid in same_name
                 if team in active[pid]["teams"] or active[pid]["team"] == team]
        if len(cands) == 1:
            rows.append((aid, name, team, cands[0], "ok", None))
        else:
            reason = (f"{len(cands)} candidates on {team}: {sorted(cands)}" if cands else
                      f"no active player named {name!r} on {team}"
                      + (f" (same name elsewhere: {sorted(same_name)})" if same_name else ""))
            rows.append((aid, name, team, None, "flag", reason))
        counts[rows[-1][4]] += 1
    conn.execute("DELETE FROM espn_person_map")
    conn.executemany("INSERT INTO espn_person_map VALUES (?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    return counts


def collision_pairs(entities: list[dict]) -> list[tuple]:
    """entities: {"name", "team"}. Returns (a, b, reasons) for every colliding pair."""
    pre = []
    for e in entities:
        k = name_key(e["name"]).split()
        pre.append((e, name_key(e["name"]), _ascii_lower(e["name"]),
                    k[0] if k else "", k[-1] if k else ""))
    out = []
    for (a, ka, la, fa, lna), (b, kb, lb, fb, lnb) in itertools.combinations(pre, 2):
        reasons = []
        if ka == kb:
            reasons.append("same name_key")
        if lna == lnb and fa[:1] == fb[:1]:
            reasons.append("same last name + first initial")
        if lna == lnb and a["team"] and a["team"] == b["team"]:
            reasons.append("same last name, same team")
        if _lev(la, lb) <= 2:
            reasons.append("edit distance <= 2")
        if reasons:
            out.append((a, b, ", ".join(reasons)))
    return out


def build_collisions(conn, active: dict | None = None) -> int:
    """Rebuild player_name_collisions over active box-score players and roster athletes."""
    active = active if active is not None else _active_players(conn)
    roster_team = {}
    for name, team in conn.execute("SELECT player_name, team_name FROM rosters"):
        roster_team.setdefault(name_key(name), set()).add(team)
    ents, seen_keys = [], set()
    for pid, p in active.items():
        name = sorted(p["names"])[0]
        teams = roster_team.get(name_key(name), set())
        ents.append({"name": name, "team": next(iter(teams)) if len(teams) == 1 else p["team"]})
        seen_keys |= {name_key(n) for n in p["names"]}
    for name, team in conn.execute("SELECT player_name, team_name FROM rosters"):
        if name_key(name) not in seen_keys:
            ents.append({"name": name, "team": team})
            seen_keys.add(name_key(name))
    pairs = collision_pairs(ents)
    conn.execute("DELETE FROM player_name_collisions")
    conn.executemany("INSERT INTO player_name_collisions VALUES (?, ?, ?, ?, ?, ?, ?)",
                     [(name_key(a["name"]), a["name"], a["team"], name_key(b["name"]), b["name"],
                       b["team"], r) for a, b, r in pairs])
    conn.commit()
    return len(pairs)


def rebuild(conn) -> dict:
    """Both tables, from the current rosters and box scores (after each roster refresh)."""
    init_tables(conn)
    active = _active_players(conn)
    return {"espn_person_map": build_espn_person_map(conn, active),
            "collisions": build_collisions(conn, active)}


def rebuild_now() -> dict:
    """rebuild() on its own connection to predictions.db (startup / maintenance hook)."""
    import database
    conn = sqlite3.connect(database.DB_PATH, timeout=30)
    try:
        out = rebuild(conn)
    finally:
        conn.close()
    print(f"[player_match] rebuilt: {out}", flush=True)
    return out


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def collides_with(conn, key: str) -> list[str]:
    """Names that `key` (a name_key) is listed as colliding with."""
    try:
        rows = conn.execute(
            "SELECT name_b FROM player_name_collisions WHERE name_key_a = ? "
            "UNION SELECT name_a FROM player_name_collisions WHERE name_key_b = ?", (key, key)).fetchall()
    except sqlite3.OperationalError:
        return []
    return sorted(r[0] for r in rows)


def athlete_ids_for(conn, person_id: int) -> set[str]:
    try:
        return {r[0] for r in conn.execute(
            "SELECT athlete_id FROM espn_person_map WHERE person_id = ? AND status = 'ok'",
            (int(person_id),))}
    except sqlite3.OperationalError:
        return set()


def person_for_name(conn, player_name: str, person_id: int | None) -> dict:
    """
    Confirm that `person_id` (from data_fetcher.get_player_id, which may match fuzzily)
    really carries this name: exact name_key against that person's names. A fuzzy hop
    onto a colliding name is 'ambiguous'.
    """
    if person_id is None:
        return {"status": "not_found", "flag": f"no player matches {player_name!r}"}
    names = {r[0] for r in conn.execute(
        "SELECT firstName || ' ' || lastName FROM players WHERE personId = ? UNION "
        "SELECT DISTINCT firstName || ' ' || lastName FROM player_stats WHERE personId = ?",
        (int(person_id), int(person_id)))}
    if name_key(player_name) in {name_key(n) for n in names}:
        return {"status": "ok", "person_id": int(person_id)}
    near = collides_with(conn, name_key(player_name))
    return {"status": "ambiguous",
            "flag": f"{player_name!r} only matched personId {person_id} fuzzily"
                    + (f"; collides with {near}" if near else "")}


def match_name(conn, player_name: str, candidates: list[str], teams_of: dict | None = None,
               team: str | None = None) -> dict:
    """
    Pick player_name among `candidates` (third-party names): exact name_key only.
    teams_of: {candidate: set of team nicknames it is tied to}; with `team`, a candidate
    must include that team. One survivor -> ok; several -> ambiguous; none -> ambiguous
    if player_name collides with a candidate's name, else not_found.
    """
    key = name_key(player_name)
    exact = [c for c in candidates if name_key(c) == key]
    if team is not None and teams_of is not None:
        exact = [c for c in exact if team in teams_of.get(c, set())]
    if len(exact) == 1:
        return {"status": "ok", "name": exact[0]}
    if len(exact) > 1:
        return {"status": "ambiguous", "flag": f"{player_name!r} matches {sorted(exact)}"}
    near = set(collides_with(conn, key))
    hits = sorted(c for c in candidates if c in near or name_key(c) in {name_key(n) for n in near})
    if hits:
        return {"status": "ambiguous", "flag": f"{player_name!r} has no exact match; collides with {hits}"}
    return {"status": "not_found"}


def flag(msg: str) -> None:
    print(f"[player_match] FLAG: {msg}", flush=True)
