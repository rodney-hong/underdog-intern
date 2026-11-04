"""
Prod schedulers across DST (APScheduler 3.11). On Nov 1 2026 the hourly official-report
cron (news_ingest, minute=2,17,32,47 ET) looped forever in Job._get_run_times from the
01:47 EDT fire: 3.11 adds 1 us to the previous fire time on wall-clock time, which drops
`fold`, so 01:02 EST became 01:02 EDT and the trigger cycled through the repeated hour.

The real jobs from line_logger.start_scheduler() and news_ingest.start_scheduler() (with
start() stubbed: nothing runs, no thread) are driven through the scheduler's own logic
(MemoryJobStore picks due jobs by timestamp, then Job._get_run_times and
trigger.get_next_fire_time, as in BaseScheduler._process_jobs) on a fake clock, from
starts before, inside and after the transition. Asserts every fire time, one run per
wake (no pile-up) and a bounded number of trigger calls (no runaway). No network.
"""
import datetime as dt
import types

import pytest
from apscheduler.job import Job
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import database
import line_logger
import news_ingest
import offsite_backup

UTC = dt.timezone.utc
ET = line_logger.EASTERN
FALL_BACK = dt.datetime(2027, 11, 7, 6, 0, tzinfo=UTC)       # 02:00 EDT -> 01:00 EST
SPRING_FORWARD = dt.datetime(2027, 3, 14, 7, 0, tzinfo=UTC)  # 02:00 EST -> 03:00 EDT

# ET wall-clock fields of each cron job: (hours or None for every hour, minutes)
CRON = {
    "line_pretip_scheduler": ({line_logger.SCHEDULE_HOUR_ET}, {0}),
    "line_open_pull":        ({line_logger.OPEN_PULL_HOUR_ET}, {0}),
    "morning_maintenance":   ({line_logger.MAINTENANCE_HOUR_ET}, {0}),
    "offsite_backup":        ({offsite_backup.BACKUP_HOUR_ET}, {offsite_backup.BACKUP_MINUTE_ET}),
    "news_nba_report":       (None, {int(m) for m in news_ingest.NBA_REPORT_CRON_MINUTES.split(",")}),
}
INTERVAL = {"pretip_self_check": line_logger.SELF_CHECK_MINUTES, "news_espn": news_ingest.POLL_MINUTES}
MAX_TRIGGER_CALLS = 50          # per wake; the Nov 1 loop never ends


class Runaway(Exception):
    pass


@pytest.fixture
def prod_jobs(monkeypatch, tmp_path):
    """(scheduler timezone, {job id: trigger}) for the time-based jobs both prod schedulers add."""
    db = str(tmp_path / "dst.db")
    for mod in (database, line_logger, news_ingest):
        monkeypatch.setattr(mod, "DB_PATH", db)
    monkeypatch.setenv("SPORTSBETTR_MODE", "prod")
    for k in ("SPACES_KEY", "SPACES_SECRET", "SPACES_BUCKET", "SPACES_ENDPOINT"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setattr(BackgroundScheduler, "start", lambda self, *a, **kw: None)
    monkeypatch.setattr(line_logger, "_scheduler", None)
    monkeypatch.setattr(news_ingest, "_scheduler", None)
    monkeypatch.setattr(line_logger, "OPEN_PULL_ENABLED", True)
    tzs, jobs = set(), {}
    for sched in (line_logger.start_scheduler(), news_ingest.start_scheduler()):
        tzs.add(sched.timezone)
        jobs.update({j.id: j.trigger for j in sched.get_jobs()
                     if isinstance(j.trigger, (CronTrigger, IntervalTrigger))})
    assert len(tzs) == 1
    assert set(jobs) == set(CRON) | set(INTERVAL)
    return tzs.pop(), jobs


def _simulate(trigger, sched_tz, start: dt.datetime, end: dt.datetime) -> list[dt.datetime]:
    """Fire instants (UTC) in [start, end) as the scheduler would produce them."""
    calls = 0

    def next_fire(prev, now):
        nonlocal calls
        calls += 1
        if calls > MAX_TRIGGER_CALLS:
            raise Runaway(f"{calls} trigger calls in one wake at {now} (prev {prev})")
        return trigger.get_next_fire_time(prev, now)

    guarded = types.SimpleNamespace(get_next_fire_time=next_fire)
    fires = []
    nrt = trigger.get_next_fire_time(None, start.astimezone(sched_tz))
    while nrt is not None and nrt.timestamp() < end.timestamp():
        calls = 0
        # The job store hands the job over once its timestamp has passed; the scheduler
        # then reads datetime.now(scheduler timezone), here 0.25 s after the due time.
        now = dt.datetime.fromtimestamp(nrt.timestamp() + 0.25, sched_tz)
        run_times = Job._get_run_times(types.SimpleNamespace(next_run_time=nrt, trigger=guarded), now)
        assert len(run_times) == 1, f"woken on time at {now}, got run times {run_times}"
        fires.append(run_times[0].astimezone(UTC))
        nrt = next_fire(run_times[-1], now)
        assert nrt is None or nrt.timestamp() > run_times[-1].timestamp(), \
            f"next fire {nrt} is not after {run_times[-1]}"
    return fires


def _expected_cron(hours, minutes, start, end) -> list[dt.datetime]:
    """Every real minute in [start, end) whose ET wall clock matches the fields."""
    t = start.astimezone(UTC).replace(second=0, microsecond=0)
    if t < start:
        t += dt.timedelta(minutes=1)
    out = []
    while t < end:
        local = t.astimezone(ET)
        if local.minute in minutes and (hours is None or local.hour in hours):
            out.append(t)
        t += dt.timedelta(minutes=1)
    return out


def _starts(transition):
    """Process start times: a day before, then every 10 min from 2 h before to 2 h after."""
    yield transition - dt.timedelta(hours=20)
    for k in range(25):
        yield transition - dt.timedelta(hours=2) + dt.timedelta(minutes=10 * k, seconds=7)


@pytest.mark.parametrize("transition", [FALL_BACK, SPRING_FORWARD], ids=["fall_back", "spring_forward"])
def test_prod_jobs_across_dst(prod_jobs, transition):
    sched_tz, jobs = prod_jobs
    end = transition + dt.timedelta(hours=30)
    for start in _starts(transition):
        for job_id, trigger in jobs.items():
            label = f"{job_id} started {start.astimezone(ET):%Y-%m-%d %H:%M %Z}"
            try:
                fires = _simulate(trigger, sched_tz, start, end)
            except Runaway as exc:
                pytest.fail(f"{label}: runaway: {exc}")
            if job_id in CRON:
                assert fires == _expected_cron(*CRON[job_id], start, end), label
            else:
                step = dt.timedelta(minutes=INTERVAL[job_id])
                assert fires and fires[0] - start <= step, label
                assert all(b - a == step for a, b in zip(fires, fires[1:])), label
                assert end - fires[-1] <= step, label


def test_report_cron_fires_through_the_repeated_hour(prod_jobs):
    """Fall-back: the 01:xx slots fire in both the EDT and the EST hour, then 02:02 EST."""
    sched_tz, jobs = prod_jobs
    fires = _simulate(jobs["news_nba_report"], sched_tz,
                      FALL_BACK - dt.timedelta(hours=1, minutes=10), FALL_BACK + dt.timedelta(hours=1, minutes=5))
    assert [f"{f.astimezone(ET):%H:%M %Z}" for f in fires] == [
        "01:02 EDT", "01:17 EDT", "01:32 EDT", "01:47 EDT",
        "01:02 EST", "01:17 EST", "01:32 EST", "01:47 EST", "02:02 EST"]


def test_report_cron_skips_the_missing_hour(prod_jobs):
    sched_tz, jobs = prod_jobs
    fires = _simulate(jobs["news_nba_report"], sched_tz,
                      SPRING_FORWARD - dt.timedelta(minutes=20), SPRING_FORWARD + dt.timedelta(minutes=20))
    assert [f"{f.astimezone(ET):%H:%M %Z}" for f in fires] == ["01:47 EST", "03:02 EDT", "03:17 EDT"]


@pytest.mark.parametrize("now_utc", [
    dt.datetime(2027, 11, 7, 5, 30, tzinfo=UTC),   # 01:30 EDT, first pass
    dt.datetime(2027, 11, 7, 6, 30, tzinfo=UTC),   # 01:30 EST, repeated hour
    dt.datetime(2027, 3, 14, 6, 30, tzinfo=UTC),   # 01:30 EST, 30 min before spring-forward
], ids=["fall_first_pass", "fall_repeat", "spring"])
def test_kaggle_retry_is_one_real_hour_later(monkeypatch, now_utc):
    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return now_utc.astimezone(tz)

    monkeypatch.setattr(line_logger, "dt", types.SimpleNamespace(
        datetime=Clock, timedelta=dt.timedelta, timezone=dt.timezone))
    monkeypatch.setattr(line_logger, "_clear_of_pulls", lambda scheduler, t: t)
    added = {}
    fake = types.SimpleNamespace(add_job=lambda func, trigger, **kw: added.update(kw))
    line_logger._schedule_kaggle_retry(fake, "b", 1, "2027-11-07", 2)
    assert added["run_date"].timestamp() == (now_utc + dt.timedelta(minutes=line_logger.KAGGLE_RETRY_MINUTES)).timestamp()
