"""
Missed-pull safety net (migration plan v2, step 0.2): the 15-minute pre-tip self-check
re-adds a deleted pre-tip job within one cycle and leaves alone games that are pending,
running, already attempted or started; with today's slate never scheduled it re-runs the
free scheduling call (at most hourly). daily_check.py flags a game with an open pull but no
pre-tip pull, and a pre-tip pull without paper or challenger rows. Temp DBs, no network.
"""
import datetime as dt
import os
import sqlite3
import sys

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

import daily_check   # noqa: E402
import database      # noqa: E402
import line_logger   # noqa: E402

ET = line_logger.EASTERN


def _event(eid, tip_utc, home="Boston Celtics", away="New York Knicks"):
    return {"id": eid, "commence_time": tip_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "home_team": home, "away_team": away}


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = str(tmp_path / "t.db")
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(line_logger, "DB_PATH", path)
    line_logger.init_tables()
    monkeypatch.setattr(line_logger, "_known_events", {})
    monkeypatch.setattr(line_logger, "_running_pretips", set())
    monkeypatch.setattr(line_logger, "_selfcheck_schedule_calls", {})
    sched = BackgroundScheduler(timezone=ET)          # never started: jobs stay pending
    now = dt.datetime.now(dt.timezone.utc)
    # a game 2 hours out, unless that is tomorrow in ET (then 5 minutes out)
    tip = now + dt.timedelta(hours=2)
    if tip.astimezone(ET).date() != now.astimezone(ET).date():
        tip = now + dt.timedelta(minutes=5)
    ev = _event("evtA", tip)
    line_logger._remember_events([ev], slate_known=True)
    line_logger._schedule_pretip(sched, ev)
    return {"sched": sched, "event": ev, "path": path, "game_date": tip.astimezone(ET).date().isoformat()}


def test_a_deleted_pretip_job_is_readded_within_one_cycle(env, capsys):
    sched = env["sched"]
    assert line_logger.pretip_self_check(sched)["readded"] == []           # nothing missing
    sched.remove_job("pretip:evtA")
    out = line_logger.pretip_self_check(sched)
    assert out["readded"] == ["New York Knicks @ Boston Celtics"]
    assert sched.get_job("pretip:evtA") is not None
    assert "[self-check] WARNING: pre-tip job for New York Knicks @ Boston Celtics" in capsys.readouterr().out
    assert line_logger.pretip_self_check(sched)["readded"] == []           # once only


def test_running_retrying_attempted_or_started_games_are_left_alone(env):
    sched = env["sched"]
    sched.remove_job("pretip:evtA")
    line_logger._running_pretips.add("evtA")                               # waiting for the pre-tip lock
    assert line_logger.pretip_self_check(sched)["readded"] == []
    line_logger._running_pretips.clear()
    sched.add_job(print, "date", run_date=dt.datetime.now(ET) + dt.timedelta(hours=1), id="pretip-retry:evtA")
    assert line_logger.pretip_self_check(sched)["readded"] == []
    sched.remove_job("pretip-retry:evtA")
    line_logger.log_skip(endpoint="event_odds", phase="pretip", game_id="evtA", game_date=env["game_date"],
                         reason="monthly cap reached")                     # attempted: refused by the cap
    assert line_logger.pretip_self_check(sched)["readded"] == []
    assert sched.get_job("pretip:evtA") is None


def test_a_started_game_is_not_readded(env):
    sched = env["sched"]
    past = _event("evtOld", dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1))
    if past["commence_time"][:10] == dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"):
        line_logger._remember_events([past])
    assert line_logger.pretip_self_check(sched)["readded"] == []


def test_unscheduled_slate_is_scheduled_again_at_most_hourly(env, monkeypatch):
    sched = env["sched"]
    line_logger._known_events.clear()                                      # 06:00 job and retries all failed
    calls = []
    monkeypatch.setattr(line_logger, "SCHEDULE_HOUR_ET", 0)
    monkeypatch.setattr(line_logger, "_last_tip_today_utc",
                        lambda: dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30))
    monkeypatch.setattr(line_logger, "schedule_todays_pretips", lambda scheduler=None: calls.append(1) or 3)
    assert line_logger.pretip_self_check(sched)["rescheduled"] == 3
    assert line_logger.pretip_self_check(sched)["rescheduled"] is None     # not again within the hour
    assert calls == [1]


def test_self_check_job_is_in_the_prod_scheduler(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setattr(line_logger, "DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("SPORTSBETTR_MODE", "prod")
    for k in ("SPACES_KEY", "SPACES_SECRET", "SPACES_BUCKET", "SPACES_ENDPOINT"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setattr(line_logger, "_scheduler", None)
    fired = []                                        # the startup catch-up job fires at once: no OddsAPI
    monkeypatch.setattr(line_logger, "ODDS_API_KEY", "")
    monkeypatch.setattr(line_logger, "fetch_events", lambda phase=None: fired.append(phase) or (None, None))
    monkeypatch.setattr(line_logger, "schedule_todays_pretips", lambda scheduler=None: fired.append("sched") or 0)
    sched = line_logger.start_scheduler()
    try:
        ids = {j.id for j in sched.get_jobs()}
        assert {"pretip_self_check", "offsite_backup", "line_pretip_scheduler", "morning_maintenance"} <= ids
        trig = sched.get_job("pretip_self_check").trigger
        assert trig.interval == dt.timedelta(minutes=15)
    finally:
        sched.remove_all_jobs()            # nothing may fire (the catch-up job would call OddsAPI)
        line_logger.stop_scheduler()


# --- daily_check.py ---------------------------------------------------------------------

def _daily_db(path, day):
    line_logger.init_tables()
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE paper_predictions (game_id TEXT, game_date TEXT, actual_value REAL)")
    conn.execute("CREATE TABLE challenger_predictions (game_id TEXT, game_date TEXT, status TEXT, result TEXT)")
    conn.execute("CREATE TABLE schedule (gameId INTEGER, gameDateTimeEst TEXT, homeTeamId INTEGER, awayTeamId INTEGER, "
                 "homeTeamName TEXT, homeTeamCity TEXT, awayTeamName TEXT, awayTeamCity TEXT)")

    def game(gid, tip_et, home, away, phases, paper, chall, in_schedule=True):
        tip_utc = tip_et.astimezone(dt.timezone.utc)
        conn.execute("INSERT INTO line_snapshots (game_id, game_date, commence_time, home_team, away_team, "
                     "player_name, market_key, bookmaker, side, captured_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (gid, day, tip_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), home, away, "P", "m", "underdog", "Over", "t"))
        for ph in phases:
            conn.execute("INSERT INTO odds_pull_log (started_at, game_date, game_id, endpoint, pull_phase, "
                         "http_status, credits_used) VALUES (?,?,?,?,?,?,?)", ("t", day, gid, "event_odds", ph, 200, 8))
        conn.executemany("INSERT INTO paper_predictions VALUES (?,?,NULL)", [(gid, day)] * paper)
        conn.executemany("INSERT INTO challenger_predictions VALUES (?,?,'ok',NULL)", [(gid, day)] * chall)
        if in_schedule:
            hc, hn = home.rsplit(" ", 1)
            ac, an = away.rsplit(" ", 1)
            conn.execute("INSERT INTO schedule VALUES (1, ?, 1, 2, ?, ?, ?, ?)",
                         (tip_et.strftime("%Y-%m-%d %H:%M:%S"), hn, hc.replace("Los Angeles", "LA"), an, ac))

    d = dt.date.fromisoformat(day)
    at = lambda h: dt.datetime.combine(d, dt.time(h, 0), ET)
    game("g1", at(19), "Boston Celtics", "New York Knicks", ["open", "pretip"], 40, 40)
    game("g2", at(19), "Miami Heat", "Orlando Magic", ["open"], 0, 0)                    # missed pre-tip
    game("g3", at(20), "Utah Jazz", "Los Angeles Clippers", ["catchup", "pretip"], 30, 0)  # no challenger rows
    game("g4", at(22), "Phoenix Suns", "Denver Nuggets", ["open"], 0, 0)                 # pre-tip not due yet
    conn.execute("INSERT INTO schedule VALUES (9, ?, 1, 2, 'Kings', 'Sacramento', 'Spurs', 'San Antonio')",
                 (at(19).strftime("%Y-%m-%d %H:%M:%S"),))                                # never pulled
    conn.commit()
    conn.close()


def test_daily_check_flags_missed_pretips_and_missing_rows(tmp_path, monkeypatch):
    path = str(tmp_path / "d.db")
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(line_logger, "DB_PATH", path)
    day = "2026-11-03"
    _daily_db(path, day)
    now = dt.datetime.combine(dt.date.fromisoformat(day), dt.time(21, 0), ET)
    r = daily_check.report(day, now=now, db_path=path)
    p = "\n".join(r["problems"])
    assert "Orlando Magic @ Miami Heat: no pre-tip pull" in p
    assert "Los Angeles Clippers @ Utah Jazz: pre-tip pull but no challenger_predictions rows" in p
    assert "San Antonio Spurs @ Sacramento Kings: in the schedule but never pulled" in p
    assert "Boston Celtics" not in p and "Phoenix Suns" not in p          # complete / not due yet
    assert "Clippers @ Utah Jazz: in the schedule" not in p              # 'LA Clippers' matched by nickname
    assert len(r["problems"]) == 3
    assert r["credits"]["used_this_month"] >= 0


def test_daily_check_reads_no_outcome_column(tmp_path, monkeypatch):
    """The report runs with the outcome columns made unreadable."""
    path = str(tmp_path / "d.db")
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(line_logger, "DB_PATH", path)
    _daily_db(path, "2026-11-03")
    real = sqlite3.connect

    def guarded(*a, **k):
        conn = real(*a, **k)

        def auth(action, arg1, arg2, dbname, source):
            if action == sqlite3.SQLITE_READ and arg2 in ("actual_value", "result", "resolved_at", "actual_points"):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        conn.set_authorizer(auth)
        return conn

    monkeypatch.setattr(daily_check.sqlite3, "connect", guarded)
    daily_check.report("2026-11-03", db_path=path)
