"""
daily_check.py
One-minute daily report (cloud migration plan v2, steps 0.2, 5.6, 5.7 and Phase 6). Opens
predictions.db read-only; reads no outcome column (only pull logs, row counts and status).

  python daily_check.py [YYYY-MM-DD]     # ET game date, default today

Per game on that date (games come from odds_pull_log, plus any schedule-table game with no
pull at all), and a problem list:
  - a game with an open (or catch-up) pull but no pre-tip pull, once its pre-tip time is past;
  - a pre-tip pull with no paper_predictions rows or (from CHALLENGER_FROM) no
    challenger_predictions rows;
  - a scheduled game with no pull at all, once its pre-tip time is past.
Also: credits this month, last x-requests-remaining, backend restarts that day
(startup_log), challenger status counts (insufficient_history share), disk and backups.
Exit code 1 when there is a problem, so it can run from cron or by hand.
"""

import datetime as dt
import json
import os
import shutil
import sqlite3
import sys
from zoneinfo import ZoneInfo

from database import DB_PATH

EASTERN = ZoneInfo("America/New_York")
PRETIP_MINUTES = int(os.getenv("LINE_LOG_PRETIP_MINUTES", "30"))
CHALLENGER_FROM = "2026-10-25"   # challenger_log.EFFECTIVE_DATE: no challenger rows expected before it
GRACE_MINUTES = 20        # a pre-tip pull (with its +5 min retry) is "missed" this long after its time


def _ro(path: str = DB_PATH):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _has_table(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def nickname_key(away: str, home: str) -> tuple[str, str]:
    """('Clippers', 'Lakers'): the schedule table writes 'LA Clippers', OddsAPI 'Los Angeles Clippers'."""
    nick = lambda t: "Trail Blazers" if "Blazers" in (t or "") else (t or "").split()[-1] if t else ""
    return nick(away), nick(home)


def _count(conn, table: str, game_id: str, day: str) -> int:
    if not _has_table(conn, table):
        return 0
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE game_id = ? AND game_date = ?",
                        (game_id, day)).fetchone()[0]


def report(day: str, now: dt.datetime | None = None, db_path: str = DB_PATH) -> dict:
    """{"games": [...], "problems": [...], "credits": {...}, "restarts": [...], "challenger": {...}}"""
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    conn = _ro(db_path)
    try:
        games: dict[str, dict] = {}
        for r in conn.execute(
                "SELECT game_id, pull_phase, http_status, skip_reason FROM odds_pull_log "
                "WHERE endpoint = 'event_odds' AND game_date = ? AND game_id IS NOT NULL ORDER BY id", (day,)):
            g = games.setdefault(r["game_id"], {"game_id": r["game_id"], "pulls": {}, "failed": [], "skipped": []})
            if r["http_status"] == 200:
                g["pulls"][r["pull_phase"]] = g["pulls"].get(r["pull_phase"], 0) + 1
            elif r["skip_reason"]:
                g["skipped"].append(f"{r['pull_phase']}: {r['skip_reason']}")
            else:
                g["failed"].append(r["pull_phase"])
        for gid, g in games.items():
            snap = conn.execute("SELECT commence_time, home_team, away_team FROM line_snapshots "
                                "WHERE game_id = ? LIMIT 1", (gid,)).fetchone()
            g["label"] = f"{snap['away_team']} @ {snap['home_team']}" if snap else gid
            g["key"] = nickname_key(snap["away_team"], snap["home_team"]) if snap else None
            g["tip"] = (dt.datetime.fromisoformat(snap["commence_time"].replace("Z", "+00:00"))
                        if snap and snap["commence_time"] else None)
            g["paper_rows"] = _count(conn, "paper_predictions", gid, day)
            g["challenger_rows"] = _count(conn, "challenger_predictions", gid, day)

        problems = []
        for g in games.values():
            due = g["tip"] is not None and now >= g["tip"] - dt.timedelta(minutes=PRETIP_MINUTES - GRACE_MINUTES)
            has_pretip = "pretip" in g["pulls"]
            if not has_pretip and due:
                problems.append(f"{g['label']}: no pre-tip pull (pulls: {g['pulls'] or 'none'}; "
                                f"failed: {g['failed'] or 'none'}; refused: {g['skipped'] or 'none'})")
            if has_pretip and g["paper_rows"] == 0:
                problems.append(f"{g['label']}: pre-tip pull but no paper_predictions rows")
            if has_pretip and g["challenger_rows"] == 0 and day >= CHALLENGER_FROM:
                problems.append(f"{g['label']}: pre-tip pull but no challenger_predictions rows")

        # Scheduled games that no pull ever touched (matched on team names via line_snapshots).
        pulled = {g["key"] for g in games.values()}
        missing = []
        if _has_table(conn, "schedule"):
            for s in conn.execute("SELECT gameDateTimeEst, awayTeamCity || ' ' || awayTeamName AS away, "
                                  "homeTeamCity || ' ' || homeTeamName AS home FROM schedule "
                                  "WHERE substr(gameDateTimeEst, 1, 10) = ?", (day,)):
                label = f"{s['away']} @ {s['home']}"
                tip = dt.datetime.fromisoformat(str(s["gameDateTimeEst"])[:19].replace("T", " ")).replace(tzinfo=EASTERN)
                if nickname_key(s["away"], s["home"]) not in pulled and now >= tip - dt.timedelta(minutes=PRETIP_MINUTES - GRACE_MINUTES):
                    missing.append(label)
        problems += [f"{m}: in the schedule but never pulled" for m in missing]

        month = now.strftime("%Y-%m")
        used = conn.execute("SELECT COALESCE(SUM(credits_used), 0) FROM odds_pull_log WHERE started_at LIKE ?",
                            (month + "%",)).fetchone()[0]
        last = conn.execute("SELECT remaining_after FROM odds_pull_log WHERE remaining_after IS NOT NULL "
                            "ORDER BY id DESC LIMIT 1").fetchone()
        restarts = []
        if _has_table(conn, "startup_log"):
            start = dt.datetime.combine(dt.date.fromisoformat(day), dt.time(0), EASTERN).astimezone(dt.timezone.utc)
            end = start + dt.timedelta(days=1)
            restarts = [dict(r) for r in conn.execute(
                "SELECT started_at, mode, host, git_commit, challenger_ok FROM startup_log "
                "WHERE started_at >= ? AND started_at < ? ORDER BY id", (start.isoformat(), end.isoformat()))]
            problems += [f"startup at {r['started_at']}: challenger file failed its check"
                         for r in restarts if not r["challenger_ok"]]
        challenger = {}
        if _has_table(conn, "challenger_predictions"):
            challenger = {r[0]: r[1] for r in conn.execute(
                "SELECT status, COUNT(*) FROM challenger_predictions WHERE game_date = ? GROUP BY status", (day,))}
    finally:
        conn.close()

    backups_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups")
    offsite = None
    try:
        with open(os.path.join(backups_dir, "offsite_last.json"), encoding="utf-8") as f:
            offsite = json.load(f)
        if not offsite.get("ok"):
            problems.append(f"last off-server backup FAILED at {offsite.get('at')}: {offsite.get('error')}")
    except (OSError, ValueError):
        pass
    disk = shutil.disk_usage(os.path.dirname(os.path.abspath(db_path)))
    if disk.used / disk.total > 0.8:
        problems.append(f"disk {disk.used / disk.total:.0%} full")
    return {"day": day, "games": sorted(games.values(), key=lambda g: (g["tip"] is None, g["tip"] or now)),
            "problems": problems, "credits": {"used_this_month": used, "last_remaining": last[0] if last else None},
            "restarts": restarts, "challenger": challenger, "offsite_backup": offsite,
            "disk": {"used_pct": round(100 * disk.used / disk.total, 1), "free_gb": round(disk.free / 1e9, 1)},
            "db_gb": round(os.path.getsize(db_path) / 1e9, 2)}


def main(argv: list[str]) -> int:
    day = argv[0] if argv else dt.datetime.now(EASTERN).date().isoformat()
    r = report(day)
    print(f"SportsBettr daily check for {day}")
    for g in r["games"]:
        tip = f"{g['tip'].astimezone(EASTERN):%H:%M} ET" if g["tip"] else "?"
        print(f"  {tip:>9}  {g['label']:<50} pulls {g['pulls'] or '-'}  paper {g['paper_rows']}  "
              f"challenger {g['challenger_rows']}")
    ch = r["challenger"]
    total = sum(ch.values())
    if total:
        print(f"  challenger rows by status: {ch} (insufficient_history {ch.get('insufficient_history', 0) / total:.0%})")
    print(f"  credits this month {r['credits']['used_this_month']}, last remaining {r['credits']['last_remaining']}")
    print(f"  restarts: {len(r['restarts'])}" + "".join(f"\n    {x['started_at']} {x['mode']} {x['host']} "
                                                     f"{(x['git_commit'] or '')[:8]}" for x in r["restarts"]))
    ob = r["offsite_backup"]
    print(f"  off-server backup: {'none recorded' if ob is None else ('OK' if ob.get('ok') else 'FAILED') + ' at ' + str(ob.get('at'))}")
    print(f"  disk {r['disk']['used_pct']}% used, {r['disk']['free_gb']} GB free; predictions.db {r['db_gb']} GB")
    if r["problems"]:
        print("PROBLEMS:")
        for p in r["problems"]:
            print(f"  - {p}")
        return 1
    print("No problems.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
