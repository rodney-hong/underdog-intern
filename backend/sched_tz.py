"""
APScheduler 3.11 across DST, for the prod schedulers (line_logger, news_ingest).

3.11 compares and adds aware datetimes that share a tzinfo on wall-clock time, which
ignores `fold` (EDT vs EST in the repeated 01:00 hour on fall-back day). With the
scheduler in America/New_York:
  - CronTrigger.get_next_fire_time adds 1 us to the previous fire time, which resets
    fold to 0. After 01:47 EDT an hourly cron yields 01:02 EST, then 01:02 EDT, ...
    Job._get_run_times sees each as <= now and cycles forever, appending to a list
    (Nov 1 2026: 100% CPU, no log, OOM-killed).
  - Job._get_run_times treats the EST hour as already due during the EDT hour, so an
    interval job runs once early and then skips the repeated hour.

Fix: the scheduler runs in UTC, so `now` and every comparison use real time, and cron jobs
use EasternCron (the same ET fields, with the previous fire time converted to UTC
before 3.11's arithmetic). No fire time changes. tests/test_dst.py checks both DST days.
"""
import datetime as dt
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

EASTERN = ZoneInfo("America/New_York")


class EasternCron(CronTrigger):
    """CronTrigger on ET wall-clock fields whose next fire time is computed from UTC instants."""

    def __init__(self, **fields):
        super().__init__(timezone=EASTERN, **fields)

    def get_next_fire_time(self, previous_fire_time, now):
        if previous_fire_time is not None:
            previous_fire_time = previous_fire_time.astimezone(dt.timezone.utc)
        return super().get_next_fire_time(previous_fire_time, now.astimezone(dt.timezone.utc))


def new_scheduler() -> BackgroundScheduler:
    return BackgroundScheduler(timezone=dt.timezone.utc)
