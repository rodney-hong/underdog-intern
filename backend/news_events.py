"""
news_events.py
Deterministic events from official NBA injury report rows, and the Phase 4
trigger that turns status changes into extra line pulls. No LLM.

Paper mode: the only side effect is an OddsAPI pull through
line_logger.try_event_pull, which enforces the reserve and the 3/day ceiling.

Rules
  player       "Last, First" flipped to "First Last"; suffix kept
               ("Butler III, Jimmy" -> "Jimmy Butler III")
  team/status  copied from the row; status lower-cased
  is_change    True only when the status differs from this player's row in
               the previous report. A player with no previous row (first
               listing) is False.
  minutes_outlook  always "unknown" (the official report never states it)
  source_time  the report's published time

Triggers
  Only official-report events whose change crosses the play/no-play line
  trigger (is_trigger_change): any status -> out, or questionable / doubtful /
  out -> available. Other changes (e.g. questionable -> probable) are recorded
  with is_change=True but do not trigger. ESPN items are
  stored as news_items and trigger nothing. Drop-off rows (source='derived')
  are recorded as events but do not trigger: they carry no new status.
  A changed player triggers only if his name matches exactly (rosters.name_key,
  player_match.match_name; no fuzzy matching) a Points line logged today in
  line_snapshots whose game is the same matchup as the report row (both teams
  agree; guards against same-name players and mis-dated lines). A name with no
  exact match that collides with a line name is recorded as ambiguous and flagged,
  never guessed. Changes are coalesced to one pull per game per report.

EVENT_TRIGGERS_ENABLED (default "0") gates the pull itself; events are always
recorded.
"""

import os
import datetime as dt
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")


def init_tables(conn) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS news_events (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            news_item_id     INTEGER NOT NULL UNIQUE REFERENCES news_items(id),
            method           TEXT NOT NULL,      -- 'deterministic' (official report) / 'llm'
            kind             TEXT NOT NULL,      -- 'row' / 'dropped'
            player           TEXT,
            team             TEXT,
            status           TEXT,
            minutes_outlook  TEXT,
            is_change        INTEGER NOT NULL,
            source_time      TEXT,
            game_date        TEXT,               -- ISO, from the report row
            matchup          TEXT,
            previous_status  TEXT,
            matched_player   TEXT,               -- line_snapshots name it matched, if any
            game_id          TEXT,               -- OddsAPI event id, if matched
            trigger_result   TEXT,               -- what happened with the Phase 4 trigger
            created_at       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_news_events_change ON news_events (is_change, game_date);
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Deterministic parsing
# ---------------------------------------------------------------------------

def flip_name(name: str) -> str:
    """
    'Butler III, Jimmy' -> 'Jimmy Butler III'; 'Moore Jr., Wendell' -> 'Wendell Moore Jr.';
    'Udeh, Jr., Ernest' -> 'Ernest Udeh Jr.' (the suffix as its own field, 2026-10-22 report).
    """
    parts = [p.strip() for p in name.split(",")]
    if len(parts) < 2:
        return name.strip()
    last, middle, first = parts[0], parts[1:-1], parts[-1]
    return " ".join(p for p in [first, last, *middle] if p)


def _iso_date(mmddyyyy: str) -> str | None:
    try:
        return dt.datetime.strptime(mmddyyyy, "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def row_key(r: dict) -> tuple:
    return (r["game_date"], r["matchup"], r["team"], r["player"])


def parse_official_row(r: dict, prev_row: dict | None, published_at: str) -> dict | None:
    """Event fields for one parsed report row; None for NOT YET SUBMITTED placeholders."""
    if not r["player"]:
        return None
    prev_status = prev_row["status"] if prev_row else None
    return {
        "player": flip_name(r["player"]),
        "team": r["team"],
        "status": r["status"].strip().lower(),
        "minutes_outlook": "unknown",
        "is_change": prev_status is not None and prev_status != r["status"],
        "source_time": published_at,
        "game_date": _iso_date(r["game_date"]),
        "matchup": r["matchup"],
        "previous_status": prev_status.lower() if prev_status else None,
    }


# ---------------------------------------------------------------------------
# Line matching + triggers (injectable for replay)
# ---------------------------------------------------------------------------

TRIGGER_TO_AVAILABLE_FROM = ("questionable", "doubtful", "out")


def is_trigger_change(previous_status: str | None, status: str) -> bool:
    """Play/no-play rule: any status -> out, or questionable/doubtful/out -> available."""
    if previous_status is None or previous_status == status:
        return False
    if status == "out":
        return True
    return status == "available" and previous_status in TRIGGER_TO_AVAILABLE_FROM


def logged_points_lines(conn, game_date: str) -> dict[str, tuple]:
    """{player_name: (game_id, home_team, away_team)} for Points lines logged for game_date."""
    rows = conn.execute(
        "SELECT DISTINCT player_name, game_id, home_team, away_team FROM line_snapshots "
        "WHERE game_date = ? AND market_key = 'player_points'", (game_date,)).fetchall()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def same_matchup(matchup: str, home_team: str, away_team: str) -> bool:
    """Report matchup 'MIN@HOU' vs a line's full team names, either orientation."""
    from data_fetcher import full_to_abbrev
    return {full_to_abbrev(home_team), full_to_abbrev(away_team)} == set(matchup.split("@"))


def match_player(conn, player: str, names: list[str]) -> dict:
    """
    player_match.match_name over this matchup's line names: exact name_key only (no
    difflib). {"status": "ok", "name"} / {"status": "ambiguous", "flag"} / not_found.
    """
    import player_match
    return player_match.match_name(conn, player, names)


def _live_trigger(game_id: str, note: str) -> str:
    if os.getenv("EVENT_TRIGGERS_ENABLED", "0") != "1":
        return "not sent: EVENT_TRIGGERS_ENABLED is off"
    import line_logger
    rows, reason = line_logger.try_event_pull(game_id, note=note)
    return f"pulled: {rows} rows" if not reason else f"skipped: {reason}"


def process_report(conn, *, url: str, rows: list[dict], prev_rows: list[dict] | None,
                   published_at: str, item_ids: dict[str, int], dropped_ids: list[int],
                   today: str | None = None, lines_lookup=None, trigger_fn=None,
                   allow_triggers: bool = True) -> dict:
    """
    Record one event per newly ingested report row (and per drop-off row), then
    fire at most one trigger per game for status changes with a Points line today.

    item_ids:    {hash_text: news_items.id} for this report's rows
    dropped_ids: news_items ids of this report's drop-off rows
    allow_triggers: False when ingesting an old report during startup catch-up;
                    events are recorded but nothing is pulled for stale news
    """
    init_tables(conn)
    today = today or dt.datetime.now(EASTERN).date().isoformat()
    lines_lookup = lines_lookup or (lambda d: logged_points_lines(conn, d))
    trigger_fn = trigger_fn or _live_trigger
    now = dt.datetime.now(dt.timezone.utc).isoformat()

    prev_by_key = {row_key(r): r for r in (prev_rows or [])}
    changes, recorded = [], 0
    for r in rows:
        ev = parse_official_row(r, prev_by_key.get(row_key(r)), published_at)
        if ev is None:
            continue
        item_id = item_ids.get(_row_text(r))
        if item_id is None:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO news_events (news_item_id, method, kind, player, team, status, "
            "minutes_outlook, is_change, source_time, game_date, matchup, previous_status, created_at) "
            "VALUES (?, 'deterministic', 'row', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, ev["player"], ev["team"], ev["status"], ev["minutes_outlook"],
             int(ev["is_change"]), ev["source_time"], ev["game_date"], ev["matchup"],
             ev["previous_status"], now))
        if cur.rowcount != 1:
            continue                                   # event already recorded for this item
        recorded += 1
        if not ev["is_change"]:
            continue
        if not allow_triggers:
            _set_result(conn, cur.lastrowid, None, None, "no trigger: catch-up of an older report")
        elif is_trigger_change(ev["previous_status"], ev["status"]):
            changes.append((cur.lastrowid, ev))
        else:
            _set_result(conn, cur.lastrowid, None, None, "no trigger: not a play/no-play change")

    for item_id in dropped_ids:
        conn.execute(
            "INSERT OR IGNORE INTO news_events (news_item_id, method, kind, is_change, source_time, "
            "trigger_result, created_at) VALUES (?, 'deterministic', 'dropped', 0, ?, ?, ?)",
            (item_id, published_at, "no trigger: drop-off rows carry no new status", now))
    conn.commit()

    # Coalesce: one trigger per game for this report.
    by_game: dict[str, list] = {}
    for ev_id, ev in changes:
        if ev["game_date"] != today:
            _set_result(conn, ev_id, None, None, "no trigger: game not today")
            continue
        lines = {p: v for p, v in lines_lookup(ev["game_date"]).items()
                 if same_matchup(ev["matchup"], v[1], v[2])}
        res = match_player(conn, ev["player"], list(lines))
        if res["status"] == "ambiguous":
            import player_match
            player_match.flag(f"official report: {res['flag']}")
            _set_result(conn, ev_id, None, None, f"no trigger: ambiguous name ({res['flag']})"[:300])
            continue
        if res["status"] != "ok":
            _set_result(conn, ev_id, None, None, "no trigger: no Points line today for this game")
            continue
        m = res["name"]
        by_game.setdefault(lines[m][0], []).append((ev_id, ev, m))

    results = {}
    for game_id, evs in by_game.items():
        note = "; ".join(f"{e['player']} {e['previous_status']}->{e['status']}" for _, e, _ in evs)
        res = trigger_fn(game_id, f"{url} | {note}")
        results[game_id] = res
        for ev_id, _, m in evs:
            _set_result(conn, ev_id, m, game_id, res)
    conn.commit()
    return {"events": recorded, "trigger_changes": len(changes), "games_triggered": results}


def _row_text(r: dict) -> str:
    # Must match news_ingest.report_items' raw_text / hash_text
    return " | ".join([f"{r['game_date']} {r['game_time']} {r['matchup']}".strip(),
                       r["team"], r["player"], r["status"], r["reason"]])


def _set_result(conn, ev_id, matched, game_id, result) -> None:
    conn.execute("UPDATE news_events SET matched_player = ?, game_id = ?, trigger_result = ? "
                 "WHERE id = ?", (matched, game_id, result, ev_id))
