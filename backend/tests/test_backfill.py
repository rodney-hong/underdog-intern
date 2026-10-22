"""
backfill.py against a temp DB with mocked OddsAPI responses (httpx.MockTransport):
nothing touches predictions.db or the network.
"""
import datetime as dt
import json
import os
import sqlite3
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backfill
import line_logger

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 22, 19, 0, tzinfo=UTC)          # 15:00 ET
KEY = "paid-key-for-tests"

# Kaggle games: (gameId, ET tip, gameType, home, away)
GAMES = [
    (22500586, "2026-01-15 22:30:00", "Regular Season", "Lakers", "Hornets"),   # next UTC day
    (22500580, "2026-01-15 19:30:00", "Regular Season", "Heat", "Celtics"),
    (22500584, "2026-01-15 22:00:00", "Regular Season", "Warriors", "Knicks"),
    (22400100, "2025-01-10 20:00:00", "Regular Season", "Lakers", "Suns"),
    (22300571, "2024-01-17 19:30:00", "Regular Season", "Celtics", "Spurs"),
    (12500001, "2025-10-05 19:00:00", "Preseason", "Bulls", "Bucks"),        # not counted
    (22200999, "2023-03-01 19:00:00", "Regular Season", "Nets", "Magic"),    # before props exist
]
# OddsAPI events, UTC commence times
EVENTS = {
    "2026-01-15": [
        {"id": "ev_mia", "commence_time": "2026-01-16T00:30:00Z", "home_team": "Miami Heat", "away_team": "Boston Celtics"},
        {"id": "ev_gsw", "commence_time": "2026-01-16T03:00:00Z", "home_team": "Golden State Warriors", "away_team": "New York Knicks"},
        {"id": "ev_lal", "commence_time": "2026-01-16T03:30:00Z", "home_team": "Los Angeles Lakers", "away_team": "Charlotte Hornets"},
        # same teams the next ET day (16 Jan, 22:30 ET): must not match the 15 Jan game
        {"id": "ev_lal_next_day", "commence_time": "2026-01-17T03:30:00Z", "home_team": "Los Angeles Lakers", "away_team": "Charlotte Hornets"},
    ],
    "2025-01-10": [
        {"id": "ev_lal_2025", "commence_time": "2025-01-11T01:00:00Z", "home_team": "Los Angeles Lakers", "away_team": "Phoenix Suns"},
    ],
    "2024-01-17": [
        {"id": "ev_bos", "commence_time": "2024-01-18T00:30:00Z", "home_team": "Boston Celtics", "away_team": "San Antonio Spurs"},
    ],
}


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "test.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE player_stats (gameId INTEGER, gameDateTimeEst TEXT, gameType TEXT, "
                 "home INTEGER, playerteamName TEXT, opponentteamName TEXT)")
    for gid, tip, gtype, home, away in GAMES:
        conn.execute("INSERT INTO player_stats VALUES (?, ?, ?, 1, ?, ?)", (gid, tip, gtype, home, away))
        conn.execute("INSERT INTO player_stats VALUES (?, ?, ?, 0, ?, ?)", (gid, tip, gtype, away, home))
    conn.execute("CREATE TABLE schedule (gameId INTEGER, gameDateTimeEst TEXT)")
    conn.execute("CREATE TABLE odds_pull_log (id INTEGER PRIMARY KEY, credits_used INTEGER)")
    conn.execute("INSERT INTO odds_pull_log (credits_used) VALUES (8)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(backfill, "DB_PATH", path)
    monkeypatch.setattr(backfill, "HIST_DB_PATH", str(tmp_path / "historical.db"))
    monkeypatch.setattr(backfill, "API_KEY", KEY)
    return path


def hist(sql):
    return rows(backfill.HIST_DB_PATH, sql)


def add_schedule(path, *tips_et):
    conn = sqlite3.connect(path)
    conn.executemany("INSERT INTO schedule VALUES (?, ?)", [(i, t) for i, t in enumerate(tips_et)])
    conn.commit()
    conn.close()


def odds_body(event_id, requested, books=("underdog", "fanduel", "draftkings")):
    markets = []
    for key in ("player_points", "player_points_alternate"):
        markets.append({"key": key, "last_update": "2026-01-16T00:00:00Z", "outcomes": [
            {"name": "Over", "description": "Jayson Tatum", "price": -115, "point": 27.5, "multiplier": 1.03},
            {"name": "Under", "description": "Jayson Tatum", "price": -105, "point": 27.5, "link": "x"},
        ]})
    return {"timestamp": requested, "previous_timestamp": None, "next_timestamp": None,
            "data": {"id": event_id, "bookmakers": [{"key": b, "markets": markets} for b in books]}}


class FakeOddsAPI:
    """Routes the three endpoints; tracks x-requests-remaining; records every request."""

    def __init__(self, remaining=19775, odds_cost=20, status=200):
        self.remaining, self.odds_cost, self.status = remaining, odds_cost, status
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, q = request.url.path, request.url.params
        if "/historical/" in path and path.endswith("/events"):
            date_et = dt.datetime.fromisoformat(q["date"].replace("Z", "+00:00")).astimezone(line_logger.EASTERN).date()
            cost, body = 1, {"timestamp": q["date"], "data": EVENTS.get(date_et.isoformat(), [])}
        elif path.endswith("/sports/basketball_nba/events"):
            cost, body = 0, []
        elif path.endswith("/odds"):
            if self.status != 200:
                return httpx.Response(self.status, text="nope",
                                      headers={"x-requests-remaining": str(self.remaining), "x-requests-last": "0"})
            cost, body = self.odds_cost, odds_body(path.split("/")[-2], q["date"])
        else:
            return httpx.Response(404)
        self.remaining -= cost
        return httpx.Response(200, json=body, headers={"x-requests-remaining": str(self.remaining),
                                                       "x-requests-last": str(cost)})

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def paths(self):
        return [r.url.path for r in self.requests]


def rows(path, sql):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def go(path, api, **kw):
    kw.setdefault("max_credits", 1000)
    return backfill.run(client=api.client(), now_fn=lambda: NOW, **kw)


def test_plan_is_counted_games_newest_first_in_season(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    games = backfill.planned_games(conn, now=NOW)
    conn.close()
    assert [g["kaggle_game_id"] for g in games] == [22500586, 22500584, 22500580, 22400100, 22300571]
    assert {g["season"] for g in games} == {"2025-26", "2024-25", "2023-24"}


def test_dry_run_makes_no_call_and_writes_nothing(db, capsys):
    out = backfill.dry_run(markets=["player_points", "player_points_alternate"], max_credits=202, now=NOW)
    assert len(out["games"]) == 5
    assert out["events_calls"] == 3
    assert out["estimated_credits"] == 5 * 20 + 3
    assert not os.path.exists(backfill.HIST_DB_PATH)
    assert "estimated maximum cost 103 credits" in capsys.readouterr().out


def test_run_pulls_t_minus_30_with_live_books_multipliers_and_paid_key(db, monkeypatch):
    monkeypatch.setenv("ODDS_API_HISTORICAL_KEY", "free-key-must-not-be-used")
    api = FakeOddsAPI()
    out = go(db, api, game_ids={22500580, 22300571}, markets=["player_points", "player_points_alternate"])
    assert out["stopped"] is None and out["pulled"] == 2
    odds = [r for r in api.requests if r.url.path.endswith("/odds")]
    assert [r.url.path.split("/")[-2] for r in odds] == ["ev_mia", "ev_bos"]       # newest first
    q = odds[0].url.params
    assert q["date"] == "2026-01-16T00:00:00Z"                                      # tip 00:30Z - 30 min
    assert q["bookmakers"] == line_logger.BOOKMAKERS
    assert q["includeMultipliers"] == "true"
    assert q["markets"] == "player_points,player_points_alternate"
    assert {r.url.params["apiKey"] for r in api.requests} == {KEY}
    snap = hist("SELECT event_id, kaggle_game_id, game_date, bookmaker, market_key, side, point, "
                    "price, multiplier, extra_json, phase FROM historical_snapshots "
                    "WHERE event_id = 'ev_mia' AND bookmaker = 'underdog' AND market_key = 'player_points' "
                    "ORDER BY outcome_index")
    assert snap == [("ev_mia", 22500580, "2026-01-15", "underdog", "player_points", "Over", 27.5, -115.0, 1.03, None, "backfill"),
                    ("ev_mia", 22500580, "2026-01-15", "underdog", "player_points", "Under", 27.5, -105.0, None, '{"link": "x"}', "backfill")]
    assert hist("SELECT COUNT(*) FROM historical_snapshots")[0][0] == 2 * 3 * 2 * 2
    log = hist("SELECT endpoint, game_date, credits_used, remaining_after, markets_requested, "
                   "markets_returned, books_returned, status FROM backfill_log ORDER BY id")
    assert log[0][:4] == ("live_events", None, 0, 19775)
    assert ("event_odds", "2026-01-15", 20, 19754, "player_points,player_points_alternate",
            "player_points,player_points_alternate", "draftkings,fanduel,underdog", "ok") in log
    assert out["spent"] == 2 + 2 * 20
    # predictions.db is only read: no new table, odds_pull_log untouched.
    assert rows(db, "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY 1") == [
        ("odds_pull_log",), ("player_stats",), ("schedule",)]
    assert rows(db, "SELECT credits_used FROM odds_pull_log") == [(8,)]
    assert hist("PRAGMA journal_mode") == [("wal",)]


def test_resume_skips_done_games_and_reuses_cached_events(db):
    go(db, FakeOddsAPI(), game_ids={22500580})
    api = FakeOddsAPI()
    out = go(db, api, game_ids={22500580, 22500584})
    assert out["pulled"] == 1
    assert [p for p in api.paths() if "/historical/" in p] == [
        "/v4/historical/sports/basketball_nba/events/ev_gsw/odds"]                  # no events re-call


def test_per_run_budget_stops_before_overspending(db):
    api = FakeOddsAPI(odds_cost=80)
    out = go(db, api, max_credits=100)            # 1 events + 80 fits; the next 80 does not
    assert out["pulled"] == 1 and out["spent"] == 81
    assert "per-run budget" in out["stopped"]
    assert hist("SELECT status FROM backfill_log WHERE endpoint = 'guard'") == [("stopped",)]


def test_floor_stops_before_any_paid_call(db):
    # 10 live games before the 2026-11-20 reset: floor = 10 x 16 + 1500 = 1660.
    add_schedule(db, *[f"2026-10-{d:02d} 19:00:00" for d in range(23, 33) if d <= 31], "2026-11-01 19:00:00")
    api = FakeOddsAPI(remaining=1660 + 79)        # one 80-credit call would cross the floor
    out = go(db, api, markets=[f"m{i}" for i in range(8)])
    assert "floor" in out["stopped"] and "1660" in out["stopped"]
    assert not [p for p in api.paths() if p.endswith("/odds")]


def test_floor_counts_games_to_the_20th(db):
    add_schedule(db, "2026-11-19 19:00:00", "2026-11-20 19:00:00")
    conn = sqlite3.connect(db)
    fl = backfill.credit_floor(conn, NOW)
    conn.close()
    assert fl == {"floor": 1 * 16 + 1500, "live_games": 1, "reset": "2026-11-20"}


def test_live_guard_refuses_near_a_pretip_pull(db):
    add_schedule(db, "2026-10-22 16:00:00")       # tips 16:00 ET: pre-tip at 15:30, now 15:00
    api = FakeOddsAPI()
    out = go(db, api)
    assert "pre-tip" in out["stopped"]
    assert api.requests == []                     # not even the free call


def test_live_guard_allows_a_pretip_more_than_45_min_away(db):
    add_schedule(db, "2026-10-22 16:20:00")       # pre-tip at 15:50: 50 min away
    out = go(db, FakeOddsAPI(), game_ids={22300571})
    assert out["stopped"] is None and out["pulled"] == 1


def test_unmatched_game_is_logged_without_an_odds_call(db):
    global EVENTS
    saved = EVENTS["2025-01-10"]
    EVENTS["2025-01-10"] = []
    try:
        api = FakeOddsAPI()
        out = go(db, api, game_ids={22400100})
    finally:
        EVENTS["2025-01-10"] = saved
    assert out["pulled"] == 0
    assert not [p for p in api.paths() if p.endswith("/odds")]
    assert hist("SELECT status FROM backfill_log WHERE endpoint = 'event_odds'") == [("unmatched",)]


def test_auth_error_stops_the_run(db):
    api = FakeOddsAPI(status=401)
    out = go(db, api)
    assert "HTTP 401" in out["stopped"]
    assert len([p for p in api.paths() if p.endswith("/odds")]) == 1


def test_snapshot_time_and_quota_reset():
    assert backfill.snapshot_time("2024-01-18T00:30:00Z") == "2024-01-18T00:00:00Z"
    et = line_logger.EASTERN
    assert backfill.next_quota_reset(dt.datetime(2026, 10, 22, tzinfo=et)).date() == dt.date(2026, 11, 20)
    assert backfill.next_quota_reset(dt.datetime(2026, 11, 3, tzinfo=et)).date() == dt.date(2026, 11, 20)
    assert backfill.next_quota_reset(dt.datetime(2026, 12, 25, tzinfo=et)).date() == dt.date(2027, 1, 20)


def _game(gid):
    conn = sqlite3.connect(backfill.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return next(g for g in backfill.planned_games(conn, now=NOW) if g["kaggle_game_id"] == gid)
    finally:
        conn.close()


def test_late_tip_matches_its_et_date_not_the_utc_date(db):
    game = _game(22500586)                        # 2026-01-15 22:30 ET = 2026-01-16T03:30Z
    assert backfill.match_event(game, EVENTS["2026-01-15"])["id"] == "ev_lal"
    api = FakeOddsAPI()
    out = go(db, api, game_ids={22500586})
    assert out["pulled"] == 1
    odds = [r for r in api.requests if r.url.path.endswith("/odds")]
    assert odds[0].url.path.split("/")[-2] == "ev_lal"
    assert odds[0].url.params["date"] == "2026-01-16T03:00:00Z"
    assert hist("SELECT DISTINCT game_date FROM historical_snapshots") == [("2026-01-15",)]


def test_two_games_on_one_date_match_their_own_events_with_one_events_call(db):
    events = EVENTS["2026-01-15"]
    assert backfill.match_event(_game(22500580), events)["id"] == "ev_mia"
    assert backfill.match_event(_game(22500584), events)["id"] == "ev_gsw"
    api = FakeOddsAPI()
    out = go(db, api, game_ids={22500580, 22500584})
    assert out["pulled"] == 2
    assert [p.split("/")[-2] for p in api.paths() if p.endswith("/odds")] == ["ev_gsw", "ev_mia"]
    assert len([p for p in api.paths() if "/historical/" in p and p.endswith("/events")]) == 1
    assert hist("SELECT kaggle_game_id, event_id FROM backfill_log WHERE endpoint = 'event_odds' "
                "ORDER BY kaggle_game_id") == [(22500580, "ev_mia"), (22500584, "ev_gsw")]
