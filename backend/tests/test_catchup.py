"""
Late-start catch-up pulls (line_logger.run_catchup_pulls, pull_phase 'catchup') and the
app's explanation for a missing line (data_fetcher.missing_line_reason, GET /lines).
Temp DB; OddsAPI events and odds stubbed: no network, no predictions.db.
"""
import datetime as dt
import os
import sqlite3
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import data_fetcher
import line_logger

ET = line_logger.EASTERN


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _odds_body(player="Giannis Antetokounmpo"):
    outcomes = [{"name": side, "description": player, "point": 27.5, "price": -110}
                for side in ("Over", "Under")]
    return {"bookmakers": [{"key": "underdog", "markets": [
        {"key": "player_points_alternate", "outcomes": outcomes}]}]}


@pytest.fixture
def slate(tmp_path, monkeypatch):
    """
    Today's slate: 'far' (pre-tip pull hours away, never pulled), 'near' (pre-tip pull in
    10 min), 'done' (already has a pre-tip pull), 'live' (started). Open pull off, so the
    10:00-13:00 open catch-up window doesn't apply unless a test turns it on.
    """
    now = dt.datetime.now(ET)
    if now.hour >= 23:
        pytest.skip("slate would cross midnight ET")
    path = str(tmp_path / "test.db")
    monkeypatch.setattr(line_logger, "DB_PATH", path)
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(data_fetcher, "DB_PATH", path)
    monkeypatch.setattr(line_logger, "OPEN_PULL_ENABLED", False)
    monkeypatch.setattr(line_logger, "MONTHLY_CREDIT_CAP", 10_000)
    line_logger.init_tables()
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schedule (gameId INTEGER, gameDateTimeEst TEXT, homeTeamName TEXT, "
                 "awayTeamName TEXT)")
    conn.commit()
    conn.close()

    end_of_day = now.replace(hour=23, minute=59, second=0, microsecond=0)
    tips = {"far": min(now + dt.timedelta(hours=3), end_of_day),
            "near": now + dt.timedelta(minutes=line_logger.PRETIP_MINUTES + 10),
            "done": now + dt.timedelta(minutes=50),
            "live": now - dt.timedelta(minutes=20)}
    events = [{"id": k, "commence_time": _iso(t), "home_team": f"Home {k}", "away_team": f"Away {k}"}
              for k, t in tips.items()]
    line_logger._log_pull(endpoint="event_odds", game_id="done", game_date=now.date().isoformat(),
                          pull_phase="pretip", http_status=200, credits_used=8)
    calls = []
    monkeypatch.setattr(line_logger, "fetch_events", lambda phase=None: (events, 1000))

    def get(url, **kw):
        calls.append(url)
        return httpx.Response(200, json=_odds_body(), request=httpx.Request("GET", url),
                              headers={"x-requests-remaining": "992", "x-requests-last": "8"})

    monkeypatch.setattr(httpx, "get", get)
    return {"path": path, "calls": calls, "tips": tips}


def _log(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT game_id, pull_phase, http_status, skip_reason FROM odds_pull_log "
                            "WHERE endpoint = 'event_odds' ORDER BY id").fetchall()
    finally:
        conn.close()


def test_catchup_pulls_only_unpulled_games_far_from_their_pretip(slate):
    out = line_logger.run_catchup_pulls()
    assert [c.split("/events/")[1].split("/")[0] for c in slate["calls"]] == ["far"]
    assert out["pulled"] == ["Away far @ Home far (2 rows)"] and out["credits"] == 8
    assert out["skipped"]["Away near @ Home near"].startswith("pre-tip pull at")
    assert out["skipped"]["Away done @ Home done"] == "already pulled today"
    assert out["skipped"]["Away live @ Home live"] == "started"
    assert ("far", "catchup", 200, None) in _log(slate["path"])
    assert not any(phase == "open" for _, phase, _, _ in _log(slate["path"]))   # 'open' = 10:00 only
    conn = sqlite3.connect(slate["path"])
    assert conn.execute("SELECT DISTINCT pull_phase FROM line_snapshots").fetchall() == [("catchup",)]
    conn.close()
    lines = line_logger.todays_underdog_lines(dt.datetime.now(ET).date().isoformat())
    assert lines["Giannis Antetokounmpo"]["player_points"] == {"value": 27.5, "source": "underdog"}


def test_catchup_is_once_per_game(slate):
    line_logger.run_catchup_pulls()
    out = line_logger.run_catchup_pulls()                  # e.g. Sync now after startup
    assert len(slate["calls"]) == 1 and out["pulled"] == []
    assert out["skipped"]["Away far @ Home far"] == "already pulled today"


def test_catchup_respects_the_scheduled_pull_reserve(slate, monkeypatch):
    monkeypatch.setattr(line_logger, "MONTHLY_CREDIT_CAP", 20)   # reserve for the pre-tip pulls > 12
    out = line_logger.run_catchup_pulls()
    assert slate["calls"] == [] and out["pulled"] == []
    assert out["skipped"]["Away far @ Home far"].startswith("would dip into reserve")
    [(gid, phase, status, reason)] = [r for r in _log(slate["path"]) if r[0] == "far"]
    assert (phase, status) == ("catchup", None) and reason.startswith("would dip into reserve")


def test_catchup_respects_the_monthly_cap(slate, monkeypatch):
    monkeypatch.setattr(line_logger, "check_unscheduled_spend", lambda cost, ev: (True, "", {}))
    monkeypatch.setattr(line_logger, "MONTHLY_CREDIT_CAP", 10)   # 8 used + 8 > 10
    out = line_logger.run_catchup_pulls()
    assert slate["calls"] == []
    assert out["skipped"]["Away far @ Home far"] == "pull failed or refused (see odds_pull_log)"
    assert any(r[0] == "far" and r[3].startswith("monthly cap reached") for r in _log(slate["path"]))


def test_no_catchup_while_the_open_pull_can_still_cover_the_slate(slate, monkeypatch):
    monkeypatch.setattr(line_logger, "OPEN_PULL_ENABLED", True)
    monkeypatch.setattr(line_logger, "OPEN_PULL_HOUR_ET", dt.datetime.now(ET).hour)
    out = line_logger.run_catchup_pulls()
    assert "open pull" in out["skipped_all"] and slate["calls"] == []


def test_catchup_waits_for_a_running_pretip_job(slate):
    import threading
    import time
    order = []
    holding = threading.Event()

    def pretip_job():
        with line_logger._PRETIP_LOCK:
            holding.set()
            time.sleep(0.5)
            order.append("pretip done")

    t = threading.Thread(target=pretip_job)
    t.start()
    holding.wait(5)
    line_logger.run_catchup_pulls()
    order.append("catchup pulled")
    t.join()
    assert order == ["pretip done", "catchup pulled"]


# --- explaining a missing line --------------------------------------------------------

@pytest.fixture
def heat_game(slate, monkeypatch):
    """Heat @ Hornets today, 3 hours out (or 23:59 ET); the player is on the Heat."""
    tip = slate["tips"]["far"]
    conn = sqlite3.connect(slate["path"])
    conn.execute("INSERT INTO schedule VALUES (22600099, ?, 'Hornets', 'Heat')",
                 (tip.strftime("%Y-%m-%d %H:%M:%S"),))
    conn.commit()
    conn.close()
    monkeypatch.setattr(data_fetcher, "get_player_context",
                        lambda name: {"player_team_full": "Miami Heat"})
    return tip


def test_reason_no_pull_yet_names_the_pretip_time(heat_game):
    pretip = heat_game - dt.timedelta(minutes=line_logger.PRETIP_MINUTES)
    assert data_fetcher.missing_line_reason("Giannis Antetokounmpo", "Points") == (
        f"no lines logged yet for this game (Heat @ Hornets, {heat_game:%H:%M} ET); "
        f"pre-tip pull at {pretip:%H:%M} ET")


def test_reason_pulled_but_no_line_for_this_player(heat_game, slate):
    conn = sqlite3.connect(slate["path"])
    conn.execute("INSERT INTO line_snapshots (game_id, game_date, home_team, away_team, player_name, "
                 "market_key, bookmaker, side, point, captured_at, pull_phase) VALUES "
                 "('g', ?, 'Charlotte Hornets', 'Miami Heat', 'Someone Else', 'player_points', "
                 "'underdog', 'Over', 10.5, ?, 'catchup')",
                 (dt.datetime.now(ET).date().isoformat(), dt.datetime.now(dt.timezone.utc).isoformat()))
    conn.commit()
    conn.close()
    reason = data_fetcher.missing_line_reason("Giannis Antetokounmpo", "Points")
    assert reason.startswith("no Underdog Points line for Giannis Antetokounmpo in the latest pull")


def test_reason_no_game_today_and_unlogged_stat(slate, monkeypatch):
    monkeypatch.setattr(data_fetcher, "get_player_context",
                        lambda name: {"player_team_full": "Utah Jazz"})
    assert data_fetcher.missing_line_reason("X", "Points") == \
        "no Jazz game on today's schedule; enter manually"
    assert data_fetcher.missing_line_reason("X", "Steals") == \
        "no lines are logged for Steals; enter manually"


def test_lines_endpoint_returns_the_reason(heat_game, monkeypatch):
    from fastapi.testclient import TestClient
    import main
    monkeypatch.setattr(data_fetcher, "get_player_line", lambda p, s: (None, None))
    body = TestClient(main.app).get("/lines/Giannis%20Antetokounmpo/Points").json()
    assert body["line"] is None and body["reason"].startswith("no lines logged yet for this game")
    monkeypatch.setattr(data_fetcher, "get_player_line", lambda p, s: (None, "ambiguous"))
    assert TestClient(main.app).get("/lines/X/Points").json()["reason"] is None
