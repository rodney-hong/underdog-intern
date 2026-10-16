"""
news_ingest.py
Raw news ingestion (Phase 2). No LLM, no betting logic, nothing here feeds
predictions. ESPN is polled every 20 minutes; the official report 2 minutes
after each of its 15-minute publishing slots.

  espn_injuries        ESPN injuries API (data_fetcher._fetch_espn_injuries).
                       One item per player entry; a new item whenever the
                       entry's text or date changes.
  nba_injury_report    Official NBA injury report PDF, published every 15 min
                       on game days at
                       ak-static.cms.nba.com/referee/injury/Injury-Report_<date>_<HH>_<MM><AM|PM>.pdf
                       One item per player row. The hash excludes the report
                       time, so an unchanged row in the next report is a
                       duplicate and a new item means the row changed.

  derived              Deterministic drop-off rows: a player in the previous
                       official report (same report day) is absent from the
                       next one while their game is still listed. url = the
                       newer report, related_url = the previous one.

Dedupe: news_items.hash is UNIQUE; inserts are INSERT OR IGNORE.

CLI:
    python news_ingest.py --poll-once
    python news_ingest.py --run            # blocking poller, for running without the API server
"""

import os
import re
import io
import time
import json
import hashlib
import sqlite3
import argparse
import datetime as dt
from zoneinfo import ZoneInfo

import httpx

from database import DB_PATH

EASTERN = ZoneInfo("America/New_York")
POLL_MINUTES = int(os.getenv("NEWS_POLL_MINUTES", "20"))   # ESPN
# Official report publishes on :00/:15/:30/:45; poll 2 minutes after each slot.
NBA_REPORT_CRON_MINUTES = os.getenv("NBA_REPORT_CRON_MINUTES", "2,17,32,47")

ESPN_INJURIES_API = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
NBA_REPORT_URL    = "https://ak-static.cms.nba.com/referee/injury/Injury-Report_{date}_{hh}_{mm}{ampm}.pdf"
# Until mid-December 2025 reports were hourly with no minutes ("..._05PM.pdf"); the
# 15-minute format above replaced it. On-the-hour slots try both, since it is not
# known which format the 2026-27 season starts with.
NBA_REPORT_URL_HOURLY = "https://ak-static.cms.nba.com/referee/injury/Injury-Report_{date}_{hh}{ampm}.pdf"
NBA_REPORT_LOOKBACK_SLOTS = 4   # try the latest 15-min slot, then up to 3 earlier ones
_HEADERS = {"User-Agent": "Mozilla/5.0"}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_tables(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS news_items (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            source       TEXT NOT NULL,   -- espn_injuries / nba_injury_report / derived
            url          TEXT,            -- for derived rows: the report the player is absent from
            raw_text     TEXT NOT NULL,
            published_at TEXT,            -- UTC ISO; source's own timestamp
            fetched_at   TEXT NOT NULL,   -- UTC ISO
            hash         TEXT NOT NULL UNIQUE,
            related_url  TEXT             -- derived rows: the previous report the player was in
        );
        CREATE INDEX IF NOT EXISTS idx_news_items_fetched ON news_items (fetched_at);

        CREATE TABLE IF NOT EXISTS news_poll_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            polled_at   TEXT NOT NULL,
            source      TEXT NOT NULL,
            url         TEXT,
            http_status INTEGER,
            items_seen  INTEGER,
            items_new   INTEGER,
            error       TEXT
        );

        -- Full player roster of every ingested official report: news_items only keeps
        -- changed rows, so this is what "previous report" comparisons read from.
        CREATE TABLE IF NOT EXISTS nba_report_index (
            url          TEXT PRIMARY KEY,
            published_at TEXT NOT NULL,   -- UTC ISO, from the report title
            report_day   TEXT NOT NULL,   -- ET date of published_at
            rows_json    TEXT NOT NULL    -- [{game_date, matchup, team, player, status, reason}]
        );
    """)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(news_items)")}
    if "related_url" not in cols:
        conn.execute("ALTER TABLE news_items ADD COLUMN related_url TEXT")
    conn.commit()
    if own:
        conn.close()


def _hash(source: str, text: str) -> str:
    return hashlib.sha256(f"{source}\n{text}".encode("utf-8")).hexdigest()


def _store(conn, source: str, items: list[dict], fetched_at: str) -> int:
    """items: dicts with url, raw_text, published_at, hash_text. Returns new rows."""
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO news_items "
        "(source, url, raw_text, published_at, fetched_at, hash, related_url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(source, it["url"], it["raw_text"], it["published_at"], fetched_at,
          _hash(source, it["hash_text"]), it.get("related_url")) for it in items],
    )
    conn.commit()
    return conn.total_changes - before


def _log_poll(conn, **f) -> None:
    f.setdefault("polled_at", dt.datetime.now(dt.timezone.utc).isoformat())
    conn.execute(f"INSERT INTO news_poll_log ({', '.join(f)}) VALUES ({', '.join('?' * len(f))})",
                 tuple(f.values()))
    conn.commit()


# ---------------------------------------------------------------------------
# ESPN injuries
# ---------------------------------------------------------------------------

def _espn_published(date_str: str | None) -> str | None:
    if not date_str:
        return None
    try:
        return dt.datetime.fromisoformat(date_str.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return date_str


def espn_items(teams: list[dict]) -> list[dict]:
    items = []
    for team in teams:
        team_name = team.get("displayName", "")
        for e in team.get("injuries", []):
            athlete = e.get("athlete", {})
            name = athlete.get("displayName", "")
            if not name:
                continue
            url = next((l.get("href") for l in athlete.get("links", [])
                        if "playercard" in l.get("rel", [])), None)
            text = " | ".join([
                team_name, name, e.get("status", ""),
                e.get("shortComment", ""), e.get("longComment", ""),
            ])
            items.append({
                "url": url or f"{ESPN_INJURIES_API}#{e.get('id', '')}",
                "raw_text": text,
                "published_at": _espn_published(e.get("date")),
                # date included: ESPN re-dating an entry is a new update, not a duplicate
                "hash_text": f"{e.get('id', '')}|{e.get('date', '')}|{text}",
            })
    return items


def poll_espn(conn) -> int:
    import data_fetcher  # existing ESPN fetch; imported lazily to keep this module light
    fetched_at = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        teams = data_fetcher._fetch_espn_injuries()
    except Exception as exc:
        _log_poll(conn, source="espn_injuries", url=ESPN_INJURIES_API, error=repr(exc)[:300])
        print(f"[news] espn_injuries failed: {exc!r}")
        return 0
    items = espn_items(teams)
    new = _store(conn, "espn_injuries", items, fetched_at)
    _log_poll(conn, source="espn_injuries", url=ESPN_INJURIES_API, http_status=200,
              items_seen=len(items), items_new=new)
    print(f"[news] espn_injuries: {len(items)} seen, {new} new")
    return new


# ---------------------------------------------------------------------------
# Official NBA injury report (PDF)
# ---------------------------------------------------------------------------

def report_url(slot: dt.datetime) -> str:
    """slot: an ET datetime on a 15-minute boundary."""
    hh = slot.strftime("%I")
    return NBA_REPORT_URL.format(date=slot.strftime("%Y-%m-%d"), hh=hh,
                                 mm=slot.strftime("%M"), ampm=slot.strftime("%p"))


def report_urls(slot: dt.datetime) -> list[str]:
    urls = [report_url(slot)]
    if slot.minute == 0:
        urls.append(NBA_REPORT_URL_HOURLY.format(date=slot.strftime("%Y-%m-%d"),
                                                 hh=slot.strftime("%I"), ampm=slot.strftime("%p")))
    return urls


def latest_slots(now_et: dt.datetime, n: int) -> list[dt.datetime]:
    base = now_et.replace(minute=now_et.minute // 15 * 15, second=0, microsecond=0)
    return [base - dt.timedelta(minutes=15 * i) for i in range(n)]


_COLS = ["game_date", "game_time", "matchup", "team", "player", "status", "reason"]

# A Reason cell starts with one of these categories; any other Reason line is a
# wrapped continuation of the previous cell, even across a page break.
_REASON_START = re.compile(
    r"^(Injury/Illness|G League|NOT YET SUBMITTED|Rest\b|Not With Team|Personal Reasons|"
    r"League Suspension|Suspension|Team Suspension|Concussion|Health and Safety|"
    r"Return to Competition|Coach|Trade Pending|Team Decision|-$)")


def parse_report(pdf_bytes: bytes) -> list[dict]:
    """
    Parse the official report table by word position.

    Column x bounds come from the header row (printed on page 1 only). Rows are
    anchored on the Current Status column (every player row has one) or on a team
    line reading "NOT YET SUBMITTED". Game date/time/matchup and team are printed
    once per group, so they carry forward.

    Reason cells wrap and are vertically centred on their row, and a wrapped line
    can spill onto the next page. So Reason lines are first grouped into cells by
    category prefix (_REASON_START), across pages, and each cell is then attached
    to the row nearest its centre on the page where the cell starts.
    """
    import pdfplumber

    x: dict | None = None

    def col(w) -> str:
        c = "game_date"
        for name in _COLS:
            if w["x0"] >= x[name]:
                c = name
        return c

    anchors: list[dict] = []        # {"page", "top", "cells"} in reading order
    reason_lines: list[tuple] = []  # (page, top, text) in reading order

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for pi, page in enumerate(pdf.pages):
            words = page.extract_words(x_tolerance=1.5, y_tolerance=2)
            matchup = next((w for w in words if w["text"] == "Matchup"), None)
            if matchup is not None:
                hdr = [w for w in words if abs(w["top"] - matchup["top"]) < 2]
                hdr_top = matchup["top"]
                games = sorted(w["x0"] for w in hdr if w["text"] == "Game")
                x = {
                    "game_date": 0.0,
                    "game_time": games[1] - 3 if len(games) > 1 else 115,
                    "matchup": matchup["x0"] - 3,
                    "team":    next(w["x0"] for w in hdr if w["text"] == "Team") - 3,
                    "player":  next(w["x0"] for w in hdr if w["text"] == "Player") - 3,
                    "status":  next(w["x0"] for w in hdr if w["text"] == "Current") - 3,
                    "reason":  next(w["x0"] for w in hdr if w["text"] == "Reason") - 3,
                }
            else:
                if x is None:
                    continue
                title = [w for w in words if w["text"] == "Report:"]
                hdr_top = title[0]["top"] if title else 0.0

            body = [w for w in words if w["top"] > hdr_top + 5]
            body = [w for w in body if not any(          # drop "Page N of M" footer line
                f["text"] == "Page" and abs(f["top"] - w["top"]) < 2 for f in body)]

            lines: dict[float, list] = {}
            for w in body:
                key = next((k for k in lines if abs(k - w["top"]) < 2), w["top"])
                lines.setdefault(key, []).append(w)

            for top in sorted(lines):
                ws = sorted(lines[top], key=lambda w: w["x0"])
                by_col = {c: " ".join(w["text"] for w in ws if col(w) == c) for c in _COLS}
                if by_col["reason"]:
                    reason_lines.append((pi, top, by_col["reason"]))
                if by_col["status"] or (by_col["team"] and by_col["reason"] == "NOT YET SUBMITTED"):
                    anchors.append({"page": pi, "top": top, "cells": by_col})
                elif anchors and anchors[-1]["page"] == pi:
                    # non-anchor line: carry its game/team text onto the nearest row
                    near = min((a for a in anchors if a["page"] == pi),
                               key=lambda a: abs(a["top"] - top))
                    for c in ("game_date", "game_time", "matchup", "team", "player"):
                        if by_col[c] and not near["cells"][c]:
                            near["cells"][c] = by_col[c]

    # Group Reason lines into cells, then attach each cell to its row.
    blocks: list[list[tuple]] = []
    for ln in reason_lines:
        if not blocks or _REASON_START.match(ln[2]):
            blocks.append([ln])
        else:
            blocks[-1].append(ln)
    reasons: dict[int, list[str]] = {}
    for b in blocks:
        pg = b[0][0]
        on_page = [t for p, t, _ in b if p == pg]
        centre = sum(on_page) / len(on_page)
        cands = [i for i, a in enumerate(anchors) if a["page"] == pg]
        if not cands:
            continue
        i = min(cands, key=lambda i: abs(anchors[i]["top"] - centre))
        reasons.setdefault(i, []).append(" ".join(t for _, _, t in b))

    rows: list[dict] = []
    carry = {"game_date": "", "game_time": "", "matchup": "", "team": ""}
    for i, a in enumerate(anchors):
        c = a["cells"]
        for k in ("game_date", "game_time", "matchup"):
            if c[k]:
                carry[k] = c[k]
                if k == "matchup":
                    carry["team"] = ""
        if c["team"]:
            carry["team"] = c["team"]
        rows.append({**carry, "player": c["player"], "status": c["status"],
                     "reason": " ".join(reasons.get(i, []))})
    return rows


def report_published(pdf_bytes: bytes, slot: dt.datetime) -> str:
    """Report time from the PDF title ('Injury Report: 04/10/26 05:30 PM'), else the slot."""
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            head = pdf.pages[0].extract_text() or ""
        m = re.search(r"Injury Report:\s*(\d{2}/\d{2}/\d{2})\s*(\d{2}:\d{2})\s*(AM|PM)", head)
        if m:
            t = dt.datetime.strptime(" ".join(m.groups()), "%m/%d/%y %I:%M %p").replace(tzinfo=EASTERN)
            return t.astimezone(dt.timezone.utc).isoformat()
    except Exception:
        pass
    return slot.astimezone(dt.timezone.utc).isoformat()


def report_items(rows: list[dict], url: str, published_at: str) -> list[dict]:
    items = []
    for r in rows:
        text = " | ".join([f"{r['game_date']} {r['game_time']} {r['matchup']}".strip(),
                           r["team"], r["player"], r["status"], r["reason"]])
        # report time is NOT in the hash: the same row in the next report is a duplicate
        items.append({"url": url, "raw_text": text, "published_at": published_at,
                      "hash_text": text})
    return items


def _row_key(r: dict) -> tuple:
    return (r["game_date"], r["matchup"], r["team"], r["player"])


def dropped_items(prev_rows: list[dict], prev_url: str, cur_rows: list[dict],
                  cur_url: str, published_at: str) -> list[dict]:
    """
    Deterministic drop-off detection: a player listed in the previous report is
    absent from this one, while their game is still on this report (a game
    leaving the report after tip-off is not a drop for its players).
    """
    cur_keys  = {_row_key(r) for r in cur_rows}
    cur_games = {(r["game_date"], r["matchup"]) for r in cur_rows}
    items = []
    for r in prev_rows:
        if not r["player"]:
            continue                          # NOT YET SUBMITTED placeholder rows
        if (r["game_date"], r["matchup"]) not in cur_games or _row_key(r) in cur_keys:
            continue
        game = f"{r['game_date']} {r.get('game_time', '')} {r['matchup']}".replace("  ", " ").strip()
        items.append({
            "url": cur_url,
            "related_url": prev_url,
            "raw_text": " | ".join([game, r["team"], r["player"], "DROPPED OFF REPORT",
                                    f"previously {r['status']}: {r['reason']}"]),
            "published_at": published_at,
            "hash_text": f"dropped|{prev_url}|{cur_url}|{'|'.join(_row_key(r))}",
        })
    return items


def ingest_report(conn, url: str, pdf_bytes: bytes, slot: dt.datetime, fetched_at: str) -> tuple[int, int]:
    """Store one report's changed rows, its roster, and any drop-offs. Returns (new, dropped)."""
    rows = parse_report(pdf_bytes)
    published_at = report_published(pdf_bytes, slot)
    report_day = dt.datetime.fromisoformat(published_at).astimezone(EASTERN).date().isoformat()

    new = _store(conn, "nba_injury_report", report_items(rows, url, published_at), fetched_at)

    prev = conn.execute(
        "SELECT url, rows_json FROM nba_report_index WHERE report_day = ? AND published_at < ? "
        "ORDER BY published_at DESC LIMIT 1", (report_day, published_at)).fetchone()
    dropped = 0
    if prev is not None:
        dropped = _store(conn, "derived",
                         dropped_items(json.loads(prev["rows_json"]), prev["url"], rows, url,
                                       published_at), fetched_at)

    conn.execute("INSERT OR REPLACE INTO nba_report_index (url, published_at, report_day, rows_json) "
                 "VALUES (?, ?, ?, ?)", (url, published_at, report_day, json.dumps(rows)))
    conn.commit()
    _log_poll(conn, source="nba_injury_report", url=url, http_status=200,
              items_seen=len(rows), items_new=new + dropped)
    print(f"[news] nba_injury_report {slot:%H:%M}: {len(rows)} rows, {new} new, {dropped} dropped off")
    return new, dropped


def poll_nba_report(conn, now_et: dt.datetime | None = None) -> int:
    """
    Ingest every not-yet-ingested report in the lookback window, oldest first, so a
    report published late is not skipped and consecutive reports are compared in order.
    """
    now_et = now_et or dt.datetime.now(EASTERN)
    fetched_at = dt.datetime.now(dt.timezone.utc).isoformat()
    total, fetched_any, last_status = 0, False, None
    for slot in reversed(latest_slots(now_et, NBA_REPORT_LOOKBACK_SLOTS)):
        candidates = report_urls(slot)
        if any(conn.execute("SELECT 1 FROM nba_report_index WHERE url = ?", (u,)).fetchone()
               for u in candidates):
            continue
        resp = None
        for url in candidates:
            try:
                r = httpx.get(url, headers=_HEADERS, timeout=20)
            except Exception as exc:
                last_status = repr(exc)[:300]
                continue
            if r.status_code == 200:
                resp = r
                break
            last_status = r.status_code
        if resp is None:
            continue
        fetched_any = True
        try:
            new, dropped = ingest_report(conn, url, resp.content, slot, fetched_at)
            total += new + dropped
        except Exception as exc:
            _log_poll(conn, source="nba_injury_report", url=url, http_status=200,
                      error=f"parse failed: {exc!r}"[:300])
            print(f"[news] nba_injury_report parse failed for {url}: {exc!r}")
    if not fetched_any:
        # Nothing new in the window: normal between reports, on non-game days and in preseason.
        _log_poll(conn, source="nba_injury_report", url=report_url(latest_slots(now_et, 1)[0]),
                  http_status=last_status if isinstance(last_status, int) else None,
                  items_seen=0, items_new=0,
                  error=None if last_status is None or isinstance(last_status, int) else last_status)
        print(f"[news] nba_injury_report: nothing new (last status {last_status})")
    return total


# ---------------------------------------------------------------------------
# Poll job + scheduler
# ---------------------------------------------------------------------------

def _job(fn) -> int:
    conn = get_db()
    try:
        init_tables(conn)
        return fn(conn)
    finally:
        conn.close()


def poll_espn_job() -> int:
    return _job(poll_espn)


def poll_nba_report_job() -> int:
    return _job(poll_nba_report)


def poll_once() -> dict:
    return {"espn_injuries": poll_espn_job(), "nba_injury_report": poll_nba_report_job()}


_scheduler = None


def start_scheduler():
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    init_tables()
    if os.getenv("NEWS_INGEST_ENABLED", "1") != "1":
        print("[news] disabled via NEWS_INGEST_ENABLED")
        return None
    from apscheduler.schedulers.background import BackgroundScheduler
    sched = BackgroundScheduler(timezone=EASTERN)
    now = dt.datetime.now(EASTERN)
    sched.add_job(poll_espn_job, "interval", minutes=POLL_MINUTES, id="news_espn",
                  next_run_time=now, coalesce=True, max_instances=1,
                  misfire_grace_time=POLL_MINUTES * 60)
    sched.add_job(poll_nba_report_job, "cron", minute=NBA_REPORT_CRON_MINUTES, id="news_nba_report",
                  next_run_time=now, coalesce=True, max_instances=1, misfire_grace_time=15 * 60)
    sched.start()
    _scheduler = sched
    print(f"[news] poller started: ESPN every {POLL_MINUTES} min, "
          f"official report at :{NBA_REPORT_CRON_MINUTES.replace(',', ',:')}")
    return sched


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--poll-once", action="store_true")
    ap.add_argument("--run", action="store_true", help="blocking poller (no API server)")
    args = ap.parse_args()
    if args.run:
        start_scheduler()
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            stop_scheduler()
    else:
        print(poll_once())
