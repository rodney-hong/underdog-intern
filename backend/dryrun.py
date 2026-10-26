"""
dryrun.py
SPORTSBETTR_MODE=dry (cloud migration plan v2, steps 0.1 and 5.1): log every job prod would
run, with its ET time, and never run any of them. No network calls and no writes: the
slate comes from the local `schedule` table (read-only), not from OddsAPI.

  at startup     today's full plan ("[dry] plan ..."), including what the startup sync would do
  00:05 ET       the new day's plan
  at each time   "[dry] HH:MM ET would run: ..." for every timed job (pulls, syncs, retries),
                 so the log can be compared line by line with the collecting machine's
                 odds_pull_log and [sync] / [kaggle-retry] lines

Pre-tip times use the schedule table's tip (gameDateTimeEst, ET); the collecting machine
uses OddsAPI's commence_time, which normally agrees to the minute. The hourly Kaggle retry
is listed until midnight: in prod it stops once a newer version loads, which a dry run
can't know. News pollers and the 15-minute self-check are listed once, not per run.
"""

import datetime as dt
import sqlite3

import database
import line_logger as ll

EASTERN = ll.EASTERN
_scheduler = None


def _ro():
    return sqlite3.connect(f"file:{database.DB_PATH}?mode=ro", uri=True, timeout=30)


def games_on(day: dt.date) -> list[dict]:
    """[{"tip": aware ET datetime, "label": "Away @ Home"}] from the schedule table."""
    conn = _ro()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(schedule)")}
        names = ({"awayTeamCity", "awayTeamName", "homeTeamCity", "homeTeamName"} <= cols)
        away, home = (("awayTeamCity || ' ' || awayTeamName", "homeTeamCity || ' ' || homeTeamName") if names
                      else ("'team ' || awayTeamId", "'team ' || homeTeamId"))
        rows = conn.execute(f"SELECT gameDateTimeEst, {away}, {home} FROM schedule "
                            "WHERE substr(gameDateTimeEst, 1, 10) = ? ORDER BY gameDateTimeEst",
                            (day.isoformat(),)).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    out = []
    for tip, away, home in rows:
        try:
            t = dt.datetime.fromisoformat(str(tip)[:19].replace("T", " ")).replace(tzinfo=EASTERN)
        except ValueError:
            continue
        out.append({"tip": t, "label": f"{away} @ {home}"})
    return out


def _pulled_today(label: str, day: dt.date) -> bool:
    """A successful pull of this game on `day` in this database's odds_pull_log (read-only),
    matched on team nicknames (the schedule table and OddsAPI spell some cities differently)."""
    from daily_check import nickname_key
    away, home = label.split(" @ ")
    conn = _ro()
    try:
        rows = conn.execute(
            "SELECT DISTINCT s.away_team, s.home_team FROM odds_pull_log p JOIN line_snapshots s "
            "ON s.game_id = p.game_id WHERE p.endpoint = 'event_odds' AND p.http_status = 200 "
            "AND p.game_date = ?", (day.isoformat(),)).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    return nickname_key(away, home) in {nickname_key(a, h) for a, h in rows}


def plan(now: dt.datetime, startup: bool = False) -> list[tuple[dt.datetime, str]]:
    """Every job prod would run from `now` to the end of now's ET day, sorted by time."""
    import offsite_backup
    now = now.astimezone(EASTERN)
    day = now.date()
    at = lambda h, m=0: dt.datetime.combine(day, dt.time(h, m), EASTERN)
    games = games_on(day)
    cp = ll.CREDITS_PER_PULL
    jobs: list[tuple[dt.datetime, str]] = []

    syncs = []
    if startup:
        syncs.append(now)
        jobs.append((now, "pre-tip scheduling catch-up (free events call) for today's unstarted games"))
        if ll.OPEN_PULL_ENABLED and ll.OPEN_PULL_HOUR_ET <= now.hour < ll.OPEN_PULL_HOUR_ET + ll.OPEN_CATCHUP_HOURS:
            todo = [g["label"] for g in games if g["tip"] > now and not _pulled_today(g["label"], day)]
            jobs.append((now, f"open-pull catch-up: {len(todo)} game(s) x {cp} credits"
                              + (f" ({', '.join(todo)})" if todo else "")))
        if ll.OPEN_PULL_ENABLED and now.hour < ll.OPEN_PULL_HOUR_ET + ll.OPEN_CATCHUP_HOURS:
            catch = "none (the 10:00 open pull or its catch-up covers today)"
        else:
            todo = [g["label"] for g in games
                    if g["tip"] - dt.timedelta(minutes=ll.PRETIP_MINUTES)
                    > now + dt.timedelta(minutes=ll.CATCHUP_MIN_LEAD_MINUTES)
                    and not _pulled_today(g["label"], day)]
            catch = f"{len(todo)} game(s) x {cp} credits" + (f" ({', '.join(todo)})" if todo else "")
        jobs.append((now, f"startup sync: catch-up pulls {catch}; backup; Kaggle version check; "
                          f"ETL if newer; resolve app, paper and challenger rows"))

    if now <= at(ll.SCHEDULE_HOUR_ET):
        jobs.append((at(ll.SCHEDULE_HOUR_ET), "pre-tip scheduling (free events call)"))
    if now <= at(ll.MAINTENANCE_HOUR_ET):
        syncs.append(at(ll.MAINTENANCE_HOUR_ET))
        jobs.append((at(ll.MAINTENANCE_HOUR_ET), "morning sync: catch-up pulls (if any), backup, rosters, "
                                                 "Kaggle version check, ETL if newer, resolve"))
    if ll.OPEN_PULL_ENABLED and now <= at(ll.OPEN_PULL_HOUR_ET):
        todo = [g["label"] for g in games if g["tip"] > at(ll.OPEN_PULL_HOUR_ET)]
        jobs.append((at(ll.OPEN_PULL_HOUR_ET), f"open pull: {len(todo)} game(s) x {cp} credits"))
    for g in games:
        t = g["tip"] - dt.timedelta(minutes=ll.PRETIP_MINUTES)
        if t >= now:
            jobs.append((t, f"pre-tip pull ({cp} credits) + paper + challenger rows: {g['label']} "
                            f"(tip {g['tip']:%H:%M})"))

    # Hourly Kaggle retry: after each sync, every KAGGLE_RETRY_MINUTES after the previous
    # check, pushed clear of pull windows; a later sync starts a new series.
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0, 0), EASTERN)
    for i, start in enumerate(syncs):
        stop = syncs[i + 1] if i + 1 < len(syncs) else end
        t = start
        while True:
            t = ll._clear_of_pulls(None, t + dt.timedelta(minutes=ll.KAGGLE_RETRY_MINUTES))
            if t >= stop:
                break
            jobs.append((t, "Kaggle version retry (metadata call; ETL + resolve if newer)"))

    if offsite_backup.configured():
        b = at(offsite_backup.BACKUP_HOUR_ET, offsite_backup.BACKUP_MINUTE_ET)
        if b >= now:
            jobs.append((b, "off-server backup of predictions.db to the bucket"))
    jobs.sort(key=lambda j: j[0])
    return jobs


def _print_plan(now: dt.datetime, startup: bool = False) -> list[tuple[dt.datetime, str]]:
    import news_ingest
    jobs = plan(now, startup)
    day = now.astimezone(EASTERN).date()
    print(f"[dry] plan for {day} ({len(games_on(day))} game(s) in the schedule table); nothing below runs:")
    for t, what in jobs:
        print(f"[dry]   {t:%H:%M} ET  {what}")
    print(f"[dry]   every {ll.SELF_CHECK_MINUTES} min: pre-tip self-check; every {news_ingest.POLL_MINUTES} min: "
          f"ESPN injury poll; at :{news_ingest.NBA_REPORT_CRON_MINUTES.replace(',', ', :')}: NBA injury report")
    return jobs


def _would_run(what: str) -> None:
    print(f"[dry] {dt.datetime.now(EASTERN):%H:%M} ET would run: {what}")


def _schedule_day(sched, jobs, now) -> None:
    for i, (t, what) in enumerate(jobs):
        if t > now:
            sched.add_job(_would_run, "date", run_date=t, args=[what], id=f"dry:{t:%Y%m%d%H%M}:{i}",
                          replace_existing=True, misfire_grace_time=600)


def _new_day(sched) -> None:
    now = dt.datetime.now(EASTERN)
    _schedule_day(sched, _print_plan(now), now)


def start():
    """Print today's plan and keep printing: each timed job at its time, and each new day's plan."""
    global _scheduler
    from apscheduler.schedulers.background import BackgroundScheduler
    if _scheduler is not None:
        return _scheduler
    now = dt.datetime.now(EASTERN)
    sched = BackgroundScheduler(timezone=EASTERN)
    _schedule_day(sched, _print_plan(now, startup=True), now)
    sched.add_job(_new_day, "cron", hour=0, minute=5, args=[sched], id="dry:new_day",
                  replace_existing=True, misfire_grace_time=3600)
    sched.start()
    _scheduler = sched
    return sched


def stop() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


if __name__ == "__main__":
    _print_plan(dt.datetime.now(EASTERN), startup=True)
