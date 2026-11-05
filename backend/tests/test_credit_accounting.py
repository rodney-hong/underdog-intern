"""
Credits logged for failed OddsAPI calls. On 2026-11-04 two 10:00 ET open pulls got a
gateway 502 with no cost headers and were logged as credits_used=8, while
x-requests-remaining showed no charge. A non-200 response is now charged what its
headers say (x-requests-last, else the x-requests-remaining delta), or 0 when it has
none; a 200 with no headers is still charged the expected cost. Checked through
record_call, pull_event, the monthly cap, daily_check and backfill. Temp DB, stubbed
HTTP: no network.
"""
import datetime as dt
import sqlite3

import httpx
import pytest

import backfill
import daily_check
import database
import line_logger


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "credits.db")
    monkeypatch.setattr(line_logger, "DB_PATH", path)
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(line_logger, "MONTHLY_CREDIT_CAP", 10_000)
    line_logger.init_tables()
    conn = sqlite3.connect(path)       # empty: daily_check counts rows per game only
    conn.execute("CREATE TABLE paper_predictions (game_id TEXT, game_date TEXT, actual_value REAL)")
    conn.execute("CREATE TABLE challenger_predictions (game_id TEXT, game_date TEXT, status TEXT, result TEXT)")
    conn.execute("CREATE TABLE schedule (gameId INTEGER, gameDateTimeEst TEXT, homeTeamId INTEGER, awayTeamId INTEGER, "
                 "homeTeamName TEXT, homeTeamCity TEXT, awayTeamName TEXT, awayTeamCity TEXT)")
    conn.commit()
    conn.close()
    return path


def _logged(path):
    conn = sqlite3.connect(path)
    rows = conn.execute("SELECT pull_phase, http_status, credits_used, remaining_after "
                        "FROM odds_pull_log ORDER BY id").fetchall()
    conn.close()
    return rows


@pytest.mark.parametrize("status, headers, before, expected", [
    (502, {}, 19640, 0),                                                  # gateway error, no headers
    (502, {}, None, 0),
    (429, {"x-requests-last": "0", "x-requests-remaining": "19640"}, 19640, 0),
    (500, {"x-requests-remaining": "19632"}, 19640, 8),                   # headers say it was billed
    (200, {}, 19640, line_logger.CREDITS_PER_PULL),                       # success: never undercount
    (200, {"x-requests-last": "6", "x-requests-remaining": "19634"}, 19640, 6),
])
def test_record_call_charge(db, status, headers, before, expected):
    credits, _ = line_logger.record_call(httpx.Response(status, text="x", headers=headers),
                                         endpoint="event_odds", phase="open", remaining_before=before,
                                         game_id="g", game_date="2026-11-04",
                                         expected_cost=line_logger.CREDITS_PER_PULL)
    assert credits == expected
    assert _logged(db)[-1][2] == expected


def test_failed_open_pull_is_not_charged(db, monkeypatch):
    """A 502 open pull, then a successful one: the month, the cap and daily_check see 8, not 16."""
    tip = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=5)
    event = {"id": "ev1", "commence_time": tip.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "home_team": "Boston Celtics", "away_team": "New York Knicks"}
    outcomes = [{"name": s, "description": "Jalen Brunson", "point": 26.5, "price": -110} for s in ("Over", "Under")]
    ok_body = {"bookmakers": [{"key": "underdog", "markets": [{"key": "player_points", "outcomes": outcomes}]}]}
    responses = [httpx.Response(502, text="Bad Gateway"),
                 httpx.Response(200, json=ok_body, headers={"x-requests-last": "8", "x-requests-remaining": "19632"})]
    monkeypatch.setattr(line_logger.httpx, "get", lambda *a, **kw: responses.pop(0))

    line_logger.pull_event(event, "open", 19640)
    assert line_logger.credits_used_this_month() == 0
    line_logger.pull_event(event, "open", 19640)

    assert [r[1:] for r in _logged(db)] == [(502, 0, None), (200, 8, 19632)]
    assert line_logger.credits_used_this_month() == 8
    assert line_logger.budget_snapshot([])["used_this_month"] == 8

    day = tip.astimezone(line_logger.EASTERN).date().isoformat()
    rep = daily_check.report(day, db_path=db)
    assert rep["credits"] == {"used_this_month": 8, "last_remaining": 19632}
    (game,) = rep["games"]
    assert game["pulls"] == {"open": 1} and game["failed"] == ["open"]


def test_cap_counts_only_billed_calls(db, monkeypatch):
    """Failed calls logged at 0 don't move the monthly cap toward refusing the next pull."""
    monkeypatch.setattr(line_logger, "MONTHLY_CREDIT_CAP", line_logger.CREDITS_PER_PULL)
    tip = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=5)
    event = {"id": "ev1", "commence_time": tip.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "home_team": "Boston Celtics", "away_team": "New York Knicks"}
    calls = []
    monkeypatch.setattr(line_logger.httpx, "get",
                        lambda *a, **kw: calls.append(1) or httpx.Response(502, text="Bad Gateway"))
    for _ in range(3):
        line_logger.pull_event(event, "open", 19640)
    assert len(calls) == 3                                  # never refused by the cap
    assert [r[2] for r in _logged(db)] == [0, 0, 0]


def test_backfill_account(monkeypatch):
    run = backfill.Run(None, None, None, [], max_credits=100, now_fn=None)
    run.remaining = 19640
    assert run._account(httpx.Response(502, text="Bad Gateway"), cost_if_unknown=8) == 0
    assert run.spent == 0 and run.remaining == 19640
    assert run._account(httpx.Response(200, json={}), cost_if_unknown=8) == 8
    assert run._account(httpx.Response(500, headers={"x-requests-last": "3", "x-requests-remaining": "19637"}),
                        cost_if_unknown=8) == 3
    assert run.spent == 11 and run.remaining == 19637
