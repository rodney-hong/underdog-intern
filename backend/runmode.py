"""
runmode.py
SPORTSBETTR_MODE: which machine collects (cloud migration plan v2, step 0.1; ground rule 1:
one machine runs collection, ever).

  prod  everything automatic: startup sync (catch-up pulls, backup, rosters, Kaggle ETL,
        resolvers), the line logger's schedule (open and pre-tip pulls, paper and
        challenger logging, hourly Kaggle retry, pre-tip self-check), flag recompute,
        news pollers, the nightly off-server backup.
  dry   nothing automatic runs: the backend logs every job prod would run, with its ET
        time, at startup and again each day (dryrun.py). No network calls, no writes.
  dev   (default when unset) nothing automatic runs and nothing is planned. The API
        works for the app (predict, history, screenshot import); manual scripts such as
        backfill.py still run by hand.

WNBA_ENABLED (default 0, step 0.11): the WNBA routes, the startup WNBA data load and WNBA
training run only when it is 1.
"""

import os

from dotenv import load_dotenv

load_dotenv()

MODES = ("prod", "dry", "dev")
DEFAULT_MODE = "dev"


class BadMode(ValueError):
    pass


def mode() -> str:
    """The current mode, read from the environment on every call (tests change it)."""
    m = os.getenv("SPORTSBETTR_MODE", DEFAULT_MODE).strip().lower() or DEFAULT_MODE
    if m not in MODES:
        raise BadMode(f"SPORTSBETTR_MODE={m!r}; use one of {MODES}")
    return m


def is_prod() -> bool:
    return mode() == "prod"


def is_dry() -> bool:
    return mode() == "dry"


def wnba_enabled() -> bool:
    return os.getenv("WNBA_ENABLED", "0").strip() == "1"


_WHAT = {
    "prod": "COLLECTING: pulls spend OddsAPI credits; sync, ETL, loggers, pollers and backups run",
    "dry":  "DRY RUN: nothing runs; every job prod would run is logged with its ET time",
    "dev":  "DEVELOPMENT: nothing automatic runs; the API serves the app only",
}


def banner() -> str:
    m = mode()
    lines = ["=" * 78,
             f"  SPORTSBETTR_MODE = {m.upper()}",
             f"  {_WHAT[m]}",
             f"  WNBA {'ON' if wnba_enabled() else 'OFF'} (WNBA_ENABLED)",
             "=" * 78]
    return "\n".join(lines)
