"""
App name matching (player_match): ESPN ids, team checks, collision flags, injury
parsing. Temp DB with a small synthetic league; ESPN and lines are stubbed: nothing
touches predictions.db or the network.
"""
import datetime as dt
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data_fetcher
import database
import news_events
import news_ingest
import player_match
import rosters

THUNDER, CLIPPERS, BULLS, NETS = (1610612760, "Oklahoma City", "Thunder"), \
    (1610612746, "LA", "Clippers"), (1610612741, "Chicago", "Bulls"), (1610612751, "Brooklyn", "Nets")
ESPN_TEAM = {"Thunder": "25", "Clippers": "12", "Bulls": "4", "Nets": "17"}
# personId, first, last, box-score team, ESPN athlete id, roster team (None = not on a roster)
PLAYERS = [
    (1631114, "Jalen", "Williams", THUNDER, "4593803", "Thunder"),
    (1631119, "Jaylin", "Williams", THUNDER, "4432823", "Thunder"),
    (1629026, "Kenrich", "Williams", THUNDER, "3134908", "Thunder"),
    (1630530, "Trey", "Murphy III", CLIPPERS, "4397227", "Clippers"),
    (1629651, "Nic", "Claxton", NETS, "4278067", "Bulls"),        # traded, no Bulls box score yet
    (1630558, "Davion", "Mitchell", BULLS, "4278053", "Bulls"),
]


@pytest.fixture
def league(tmp_path, monkeypatch):
    path = str(tmp_path / "league.db")
    for m in (database, data_fetcher, rosters):
        monkeypatch.setattr(m, "DB_PATH", path)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE players (personId INTEGER, firstName TEXT, lastName TEXT)")
    conn.execute("CREATE TABLE player_stats (personId INTEGER, firstName TEXT, lastName TEXT, "
                 "gameDateTimeEst TEXT, gameType TEXT, playerteamId INTEGER, playerteamCity TEXT, "
                 "playerteamName TEXT, numMinutes REAL)")
    conn.execute("CREATE TABLE schedule (gameId INTEGER, gameDateTimeEst TEXT, homeTeamId INTEGER, "
                 "awayTeamId INTEGER, homeTeamName TEXT, homeTeamCity TEXT, awayTeamName TEXT, "
                 "awayTeamCity TEXT)")
    for t in (THUNDER, CLIPPERS, BULLS, NETS):     # one future game per team so they map
        conn.execute("INSERT INTO schedule VALUES (1, '2026-12-01 19:00:00', ?, 0, ?, ?, '', '')",
                     (t[0], t[2], t[1]))
    rosters.init_tables(conn)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for pid, first, last, team, aid, rteam in PLAYERS:
        conn.execute("INSERT INTO players VALUES (?, ?, ?)", (pid, first, last))
        conn.execute("INSERT INTO player_stats VALUES (?, ?, ?, '2026-04-10 19:00:00', "
                     "'Regular Season', ?, ?, ?, 30)", (pid, first, last, *team))
        if rteam:
            rt = next(t for t in (THUNDER, CLIPPERS, BULLS, NETS) if t[2] == rteam)
            conn.execute("INSERT INTO rosters VALUES (?, ?, ?, ?, '', ?, ?, ?, '', '', '2026-27', ?)",
                         (aid, f"{first} {last}", rosters.name_key(f"{first} {last}"),
                          ESPN_TEAM[rteam], rt[0], rt[1], rteam, now))
    conn.commit()
    player_match.rebuild(conn)
    conn.close()
    return path


def _athlete(name, aid):
    return {"displayName": name,
            "links": [{"rel": ["playercard", "desktop"],
                       "href": f"https://www.espn.com/nba/player/_/id/{aid}/x"}]}


@pytest.fixture
def espn(monkeypatch):
    """ESPN's team-grouped injuries: only Jaylin Williams (OKC) and Trey Murphy (LAC) hurt."""
    payload = [
        {"id": "25", "displayName": "Oklahoma City Thunder", "injuries": [
            {"status": "Out", "shortComment": "Jaylin: ankle",
             "athlete": _athlete("Jaylin Williams", "4432823")}]},
        {"id": "12", "displayName": "LA Clippers", "injuries": [
            {"status": "Questionable", "shortComment": "Murphy: knee",
             "athlete": _athlete("Trey Murphy", "4397227")}]},      # ESPN drops the 'III'
    ]
    monkeypatch.setattr(data_fetcher, "_fetch_espn_injuries", lambda: payload)
    return payload


def _rows(path, sql):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


# --- tables ----------------------------------------------------------------------------

def test_espn_person_map(league):
    rows = dict(((r[0], (r[1], r[2])) for r in _rows(
        league, "SELECT athlete_id, person_id, status FROM espn_person_map")))
    assert rows["4593803"] == (1631114, "ok") and rows["4432823"] == (1631119, "ok")
    assert rows["4278067"] == (None, "flag")       # Claxton: roster Bulls, no Bulls box score
    reason = _rows(league, "SELECT reason FROM espn_person_map WHERE athlete_id='4278067'")[0][0]
    assert "same name elsewhere: [1629651]" in reason


def test_collision_table(league):
    pairs = {frozenset((a, b)): r for a, b, r in _rows(
        league, "SELECT name_a, name_b, reasons FROM player_name_collisions")}
    r = pairs[frozenset(("Jalen Williams", "Jaylin Williams"))]
    assert "edit distance <= 2" in r and "same last name, same team" in r
    assert frozenset(("Kenrich Williams", "Jalen Williams")) in pairs


# --- injuries by ESPN id -----------------------------------------------------------------

def test_injury_status_is_matched_by_id_not_name(league, espn):
    # Jalen Williams is healthy: no fuzzy hop onto Jaylin's injury (audit: shown before)
    assert data_fetcher.get_player_injury_status("Jalen Williams") is None
    assert data_fetcher.get_player_injury_status("Jaylin Williams") == {"status": "Out", "reason": "Jaylin: ankle"}
    # ESPN writes 'Trey Murphy'; the id still finds 'Trey Murphy III'
    assert data_fetcher.get_player_injury_status("Trey Murphy III")["status"] == "Questionable"


def test_fuzzy_name_onto_a_collision_is_ambiguous(league, espn):
    out = data_fetcher.get_player_injury_status("Jalen Wiliams")      # typo: fuzzy in get_player_id
    assert out["status"] == "ambiguous"


def test_injury_context_parses_team_grouped_feed(league, espn):
    assert data_fetcher.get_injury_context("Jaylin Williams") == "Out: Jaylin: ankle"
    assert data_fetcher.get_injury_context("Jalen Williams") == ""


def test_opponent_injuries_match_team_by_espn_id(league, espn):
    # app name "Los Angeles Clippers" vs ESPN "LA Clippers": matched by ESPN team id
    out = data_fetcher.get_opponent_injuries("Los Angeles Clippers")
    assert [p["player"] for p in out] == ["Trey Murphy"]
    assert data_fetcher.get_opponent_injuries("Chicago Bulls") == []


# --- lines -----------------------------------------------------------------------------

@pytest.fixture
def lines(monkeypatch):
    cache = {
        "Jaylin Williams": {"player_points": {"value": 6.5, "source": "underdog"},
                            "home_team": "Oklahoma City Thunder", "away_team": "Chicago Bulls"},
        "Davion Mitchell": {"player_points": {"value": 9.5, "source": "underdog"},
                            "home_team": "Oklahoma City Thunder", "away_team": "Chicago Bulls"},
    }
    monkeypatch.setattr(data_fetcher, "get_todays_lines", lambda: cache)
    return cache


def test_line_exact_name_and_team(league, lines):
    assert data_fetcher.get_player_line("Jaylin Williams", "Points") == (6.5, "underdog")
    assert data_fetcher.get_player_line("Davion Mitchell", "Points") == (9.5, "underdog")


def test_line_no_fuzzy_onto_a_colliding_player(league, lines):
    # Jalen Williams has no line; Jaylin (a collision) does: ambiguous, never Jaylin's line
    assert data_fetcher.get_player_line("Jalen Williams", "Points") == (None, "ambiguous")


def test_line_requires_the_players_team_in_the_game(league, lines):
    lines["Trey Murphy III"] = {"player_points": {"value": 18.5, "source": "underdog"},
                                "home_team": "Oklahoma City Thunder", "away_team": "Chicago Bulls"}
    assert data_fetcher.get_player_line("Trey Murphy III", "Points") == (None, None)  # he's a Clipper


# --- official report names --------------------------------------------------------------

def test_flip_name():
    assert news_events.flip_name("Butler III, Jimmy") == "Jimmy Butler III"
    assert news_events.flip_name("Moore Jr., Wendell") == "Wendell Moore Jr."
    assert news_events.flip_name("Udeh, Jr., Ernest") == "Ernest Udeh Jr."
    assert news_events.flip_name("Nance Jr., Larry") == "Larry Nance Jr."
    assert news_events.flip_name("Nene") == "Nene"


def test_match_player_exact_or_flagged(league):
    conn = sqlite3.connect(league)
    names = ["Jaylin Williams", "Davion Mitchell"]
    assert news_events.match_player(conn, "Jaylin Williams", names) == {"status": "ok", "name": "Jaylin Williams"}
    assert news_events.match_player(conn, "Jalen Williams", names)["status"] == "ambiguous"
    assert news_events.match_player(conn, "Nic Claxton", names) == {"status": "not_found"}
    conn.close()


# --- search / ESPN ingest --------------------------------------------------------------

def test_search_returns_person_id_and_team(league):
    out = data_fetcher.search_players("Williams")
    by_id = {r["person_id"]: r for r in out}
    assert set(by_id) == {1631114, 1631119, 1629026}
    assert by_id[1631119] == {"name": "Jaylin Williams", "person_id": 1631119, "team": "Oklahoma City Thunder"}


def test_espn_items_store_athlete_id(espn):
    items = news_ingest.espn_items(espn)
    assert [i["athlete_id"] for i in items] == ["4432823", "4397227"]


def test_news_items_backfill_athlete_id_from_url(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "n.db"))
    conn.execute("CREATE TABLE news_items (id INTEGER PRIMARY KEY, source TEXT NOT NULL, url TEXT, "
                 "raw_text TEXT NOT NULL, published_at TEXT, fetched_at TEXT NOT NULL, "
                 "hash TEXT NOT NULL UNIQUE, related_url TEXT)")
    conn.execute("INSERT INTO news_items VALUES (1, 'espn_injuries', "
                 "'https://www.espn.com/nba/player/_/id/4585618/keshon-gilbert', 't', NULL, 'f', 'h1', NULL)")
    conn.execute("INSERT INTO news_items VALUES (2, 'nba_injury_report', 'https://x/report.pdf', "
                 "'t', NULL, 'f', 'h2', NULL)")
    conn.commit()
    news_ingest.init_tables(conn)
    assert conn.execute("SELECT id, athlete_id FROM news_items ORDER BY id").fetchall() == \
        [(1, "4585618"), (2, None)]
    conn.close()
