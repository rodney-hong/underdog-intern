"""
proc_lock.py
Cross-process locks on lock files next to predictions.db (database.DB_PATH):

  etl       run_etl / morning maintenance / Kaggle retry: never two loads at once,
            whether they run in the backend, a second backend or a manual
            `python -c "...run_morning_maintenance()"` (audit 2026-10-23: two
            overlapping run_etl calls duplicated 180,000 of 200,000 rows).
  instance  held for the backend's whole life, so a second uvicorn start never
            starts a second scheduler.

OS file locks (msvcrt on Windows, fcntl elsewhere) are dropped by the OS when the
process exits, even on a crash, so a lock file left behind is never a stale lock.
A ProcessLock is re-entrant within one thread (maintenance -> run_etl) and
exclusive across threads and processes.
"""

import os
import sys
import threading
import time

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

POLL_SECONDS = 0.5


def lock_path(name: str) -> str:
    import database
    return f"{database.DB_PATH}.{name}.lock"


class ProcessLock:
    def __init__(self, name: str):
        self.name = name
        self._thread_lock = threading.RLock()
        self._depth = 0
        self._fh = None

    def _try_os_lock(self) -> bool:
        fh = open(lock_path(self.name), "a+")
        try:
            if sys.platform == "win32":
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def acquire(self, timeout: float | None = None, who: str = "") -> bool:
        """
        True once held. timeout None waits forever, 0 tries once. Prints one line when
        it has to wait and one when it gets the lock (or gives up).
        """
        start = time.monotonic()
        deadline = None if timeout is None else start + timeout
        if not self._thread_lock.acquire(blocking=False):
            print(f"[lock] {who or self.name}: waiting for the {self.name} lock (held in this process)",
                  flush=True)
            got = self._thread_lock.acquire(timeout=-1 if deadline is None
                                            else max(0.0, deadline - time.monotonic()))
            if not got:
                print(f"[lock] {who or self.name}: gave up on the {self.name} lock after "
                      f"{time.monotonic() - start:.1f}s", flush=True)
                return False
        if self._depth:
            self._depth += 1
            return True
        waited = False
        while not self._try_os_lock():
            if not waited:
                print(f"[lock] {who or self.name}: waiting for the {self.name} lock "
                      f"(held by another process)", flush=True)
                waited = True
            if deadline is not None and time.monotonic() >= deadline:
                self._thread_lock.release()
                print(f"[lock] {who or self.name}: gave up on the {self.name} lock after "
                      f"{time.monotonic() - start:.1f}s", flush=True)
                return False
            time.sleep(POLL_SECONDS)
        if waited or time.monotonic() - start > 1:
            print(f"[lock] {who or self.name}: got the {self.name} lock after "
                  f"{time.monotonic() - start:.1f}s", flush=True)
        self._depth = 1
        return True

    @property
    def held(self) -> bool:
        return self._depth > 0

    def release(self) -> None:
        self._depth -= 1
        if self._depth == 0:
            try:
                if sys.platform == "win32":
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None
        self._thread_lock.release()


ETL_LOCK = ProcessLock("etl")
INSTANCE_LOCK = ProcessLock("instance")
