"""
Ollama card pipeline (detect the card grid, crop, upscale, read each card twice), the
cross-check against today's logged Underdog lines, the line_checks table and its
report. Synthetic images only (tests/synthetic_screenshot.py, built in memory); Ollama
is stubbed by mapping each crop's exact pixels to its card, so thread order doesn't matter.
"""
import datetime as dt
import hashlib
import io
import os
import sqlite3
import sys
import threading
import time

import httpx
import pytest
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import database
import line_logger
import line_checks_report
import screenshot_import as si
import synthetic_screenshot as ss

LABEL = {"Points": "Points", "Rebounds": "Rebounds", "Assists": "Assists", "3PM": "3-Pointers Made"}


def _img(data: bytes):
    return Image.open(io.BytesIO(data)).convert("RGB")


# --- card detection -------------------------------------------------------------------

@pytest.mark.parametrize("cols", [1, 2, 3])
def test_grid_cards_are_detected_in_reading_order_without_the_header(cols):
    img = _img(ss.render_grid(cols=cols))
    boxes = si.detect_cards(img)
    assert len(boxes) == len(ss.GRID_CARDS)
    assert all(y0 > 80 for _, y0, _, _ in boxes)              # the header bar is not a card
    order = sorted(boxes, key=lambda b: (b[1] // 100, b[0]))
    assert boxes == order
    w = boxes[0][2] - boxes[0][0]
    assert all(abs((b[2] - b[0]) - w) <= 3 for b in boxes)      # one card size


def test_single_column_list_is_detected():
    assert len(si.detect_cards(_img(ss.render()))) == len(ss.CARDS)


@pytest.mark.parametrize("img", [Image.new("RGB", (900, 600), (14, 14, 18)),          # blank page
                                 Image.new("RGB", (900, 600), (240, 240, 240))])       # one flat panel
def test_no_cards_detected(img):
    assert si.detect_cards(img) == []


# --- stubbed Ollama -------------------------------------------------------------------

def _digest(img) -> str:
    return hashlib.sha1(img.tobytes() + repr(img.size).encode()).hexdigest()


@pytest.fixture
def fake_ollama(monkeypatch):
    """_ollama_json stand-in: each crop (at each scale) is mapped to its card by pixels;
    `override[(card, scale)]` replaces a read; calls and peak concurrency are recorded."""
    data = ss.render_grid()
    img = _img(data)
    boxes = si.detect_cards(img)
    crops = {}
    for i, (x0, y0, x1, y1) in enumerate(boxes):
        card = img.crop((max(0, x0 - 4), max(0, y0 - 4), min(img.width, x1 + 4), min(img.height, y1 + 4)))
        for f in si.READ_SCALES:
            crops[_digest(si._upscale(card, f))] = (i, f)
    state = {"calls": [], "override": {}, "live": 0, "peak": 0, "lock": threading.Lock(), "delay": 0}

    def fake(im, prompt):
        with state["lock"]:
            state["live"] += 1
            state["peak"] = max(state["peak"], state["live"])
        try:
            time.sleep(state["delay"])
            if prompt == si.SHEET_PROMPT:
                state["calls"].append(("sheet", im.size))
                return {"cards": [{"player_name": c["player_name"], "stat_label": LABEL[c["stat"]],
                                   "line": str(c["line"])} for c in ss.GRID_CARDS]}
            i, f = crops[_digest(im)]
            state["calls"].append((i, f))
            c = ss.GRID_CARDS[i]
            read = {"player_name": c["player_name"], "stat_label": LABEL[c["stat"]], "line": f"{c['line']:g}"}
            return state["override"].get((i, f), read)
        finally:
            with state["lock"]:
                state["live"] -= 1

    monkeypatch.setenv("SCREENSHOT_EXTRACTOR", "ollama")
    monkeypatch.setattr(si, "_ollama_json", fake)
    state["data"] = data
    return state


def test_each_card_is_read_twice_and_mapped(fake_ollama):
    name, rows = si.extract_rows(fake_ollama["data"])
    assert name == "ollama"
    assert sorted(fake_ollama["calls"]) == sorted((i, f) for i in range(6) for f in si.READ_SCALES)
    assert [(r["player_name"], r["stat"], r["line"]) for r in rows] == \
        [(c["player_name"], c["stat"], c["line"]) for c in ss.GRID_CARDS]
    assert rows[1]["stat_text"] == "3-Pointers Made" and rows[1]["stat"] == "3PM"
    assert all(r["higher_multiplier"] is None and r["lower_multiplier"] is None for r in rows)
    assert all(r["reads_agree"] and r["alerts"] == [] for r in rows)


@pytest.mark.parametrize("field, value, what", [("line", "25.5", "line"),
                                                ("stat_label", "Rebounds", "stat"),
                                                ("player_name", "Brandon Millar", "name")])
def test_reads_that_disagree_flag_the_row(fake_ollama, field, value, what):
    c = ss.GRID_CARDS[1]
    read = {"player_name": c["player_name"], "stat_label": LABEL[c["stat"]], "line": "2.5", field: value}
    fake_ollama["override"][(1, si.READ_SCALES[1])] = read
    _, rows = si.extract_rows(fake_ollama["data"])
    assert rows[1]["reads_agree"] is False
    assert [a.split(":")[0] for a in rows[1]["alerts"]] == [f"two reads disagree on {what}"]
    assert rows[1]["line"] == 2.5                              # first read kept; the user decides
    assert all(r["reads_agree"] for i, r in enumerate(rows) if i != 1)


def test_unreadable_first_read_falls_back_to_the_second(fake_ollama):
    fake_ollama["override"][(0, si.READ_SCALES[0])] = {"player_name": None, "stat_label": None, "line": None}
    _, rows = si.extract_rows(fake_ollama["data"])
    assert rows[0]["player_name"] == "Giannis Antetokounmpo" and rows[0]["reads_agree"] is False


def test_no_cards_detected_reads_the_whole_image_twice(fake_ollama, monkeypatch):
    monkeypatch.setattr(si, "detect_cards", lambda img: [])
    _, rows = si.extract_rows(fake_ollama["data"])
    assert [c[0] for c in fake_ollama["calls"]] == ["sheet", "sheet"]
    full = _img(fake_ollama["data"]).size
    assert fake_ollama["calls"][0][1] == full and fake_ollama["calls"][1][1] == (int(full[0] * 1.5), int(full[1] * 1.5))
    assert len(rows) == 6 and all(r["reads_agree"] for r in rows)


def test_reads_run_in_parallel_up_to_the_setting(fake_ollama, monkeypatch):
    fake_ollama["delay"] = 0.05
    monkeypatch.setattr(si, "OLLAMA_PARALLEL", 4)
    si.extract_rows(fake_ollama["data"])
    assert fake_ollama["peak"] == 4
    fake_ollama["peak"] = 0
    monkeypatch.setattr(si, "OLLAMA_PARALLEL", 1)
    si.extract_rows(fake_ollama["data"])
    assert fake_ollama["peak"] == 1


# --- the Ollama request itself ---------------------------------------------------------

def test_ollama_request_temperature_0_thinking_off_json(monkeypatch):
    sent = []

    def post(url, json=None, timeout=None):
        sent.append(json)
        return httpx.Response(200, request=httpx.Request("POST", url), json={"message": {
            "content": '```json\n{"player_name": "A B", "stat_label": "Points", "line": "1.5"}\n```'}})

    monkeypatch.setattr(si.httpx, "post", post)
    out = si._ollama_json(Image.new("RGB", (40, 30)), si.CARD_PROMPT)
    assert out == {"player_name": "A B", "stat_label": "Points", "line": "1.5"}      # fence tolerated
    body = sent[0]
    assert body["model"] == si.OLLAMA_MODEL and body["think"] is False and body["format"] == "json"
    assert body["options"]["temperature"] == 0 and len(body["messages"][0]["images"]) == 1
    assert "multipliers" in body["messages"][0]["content"]                             # told to ignore them


def test_ollama_5xx_is_retried_once_but_4xx_is_not(monkeypatch):
    codes = iter([500, 200])

    def post(url, json=None, timeout=None):
        code = next(codes)
        return httpx.Response(code, request=httpx.Request("POST", url),
                              json={"message": {"content": '{"player_name": "A"}'}})

    monkeypatch.setattr(si.httpx, "post", post)
    monkeypatch.setattr(si.time, "sleep", lambda s: None)
    assert si._ollama_json(Image.new("RGB", (10, 10)), si.CARD_PROMPT) == {"player_name": "A"}
    monkeypatch.setattr(si.httpx, "post", lambda url, json=None, timeout=None: httpx.Response(
        404, request=httpx.Request("POST", url), json={"error": "model not found"}))
    with pytest.raises(si.ExtractorUnavailable, match="404"):
        si._ollama_json(Image.new("RGB", (10, 10)), si.CARD_PROMPT)


# --- cross-check vs logged Underdog lines, line_checks ------------------------------------

TODAY = dt.datetime.now(line_logger.EASTERN).date().isoformat()


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "test.db")
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(line_logger, "DB_PATH", path)
    database._person_index_cache.clear()
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE rosters (athlete_id TEXT, player_name TEXT, name_key TEXT, team_abbrev TEXT, "
                 "team_city TEXT, team_name TEXT)")
    conn.executemany("INSERT INTO rosters VALUES (?,?,?,?,?,?)", [
        ("1", "Giannis Antetokounmpo", "giannis antetokounmpo", "MIA", "Miami", "Heat"),
        ("2", "Brandon Miller", "brandon miller", "CHA", "Charlotte", "Hornets"),
        ("3", "Cade Cunningham", "cade cunningham", "DET", "Detroit", "Pistons")])
    conn.execute("CREATE TABLE player_stats (personId INTEGER, firstName TEXT, lastName TEXT, "
                 "gameDateTimeEst TEXT, gameType TEXT, opponentteamName TEXT, points REAL, "
                 "reboundsTotal REAL, assists REAL, threePointersMade REAL)")
    conn.execute("CREATE TABLE players (personId INTEGER, firstName TEXT, lastName TEXT)")
    conn.executemany("INSERT INTO players VALUES (?,?,?)",
                     [(203507, "Giannis", "Antetokounmpo"), (1641706, "Brandon", "Miller"),
                      (1630595, "Cade", "Cunningham")])
    conn.executemany("INSERT INTO player_stats VALUES (?,?,?,?,?,?,?,?,?,?)",
                     [(pid, f, l, f"2026-03-{d:02d} 19:30:00", "Regular Season", "Knicks", 5 + d, 3, 4, d % 5)
                      for pid, f, l in ((203507, "Giannis", "Antetokounmpo"), (1641706, "Brandon", "Miller"),
                                        (1630595, "Cade", "Cunningham"))
                      for d in range(1, 29)])
    conn.commit()
    conn.close()
    database.init_db()
    line_logger.init_tables()
    return path


def _snap(path, player, market, point, captured, phase, home="Charlotte Hornets", away="Miami Heat",
          game_date=TODAY):
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO line_snapshots (game_id, game_date, home_team, away_team, player_name, "
                 "market_key, bookmaker, side, point, captured_at, pull_phase) "
                 "VALUES ('g1', ?, ?, ?, ?, ?, 'underdog', 'Over', ?, ?, ?)",
                 (game_date, home, away, player, market, point, captured, phase))
    conn.commit()
    conn.close()


def _ctx(name):
    return {"next_opponent_full": "Charlotte Hornets", "is_home": False, "is_back_to_back": False,
            "next_game_date": TODAY}


def _rows(*cards):
    return [si.normalize_row({"player_name": n, "stat": s, "line": l, "reads_agree": True}) for n, s, l in cards]


def _checks(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT person_id, stat_type, screenshot_line, logged_line, logged_pull_phase, "
                            "matches, reads_agree FROM line_checks ORDER BY id").fetchall()
    finally:
        conn.close()


def test_latest_logged_line_is_used_and_a_difference_flags_the_row(db):
    _snap(db, "Giannis Antetokounmpo", "player_points_alternate", 26.5, "2026-10-24T14:00:00+00:00", "open")
    _snap(db, "Giannis Antetokounmpo", "player_points", 27.5, "2026-10-24T21:30:00+00:00", "pretip")
    [same] = si.confirmation_rows(_rows(("Giannis Antetokounmpo", "Points", 27.5)), context_fn=_ctx)
    assert same["logged_line"] == {"line": 27.5, "captured_at": "2026-10-24T21:30:00+00:00", "pull_phase": "pretip"}
    assert same["alerts"] == []
    [diff] = si.confirmation_rows(_rows(("Giannis Antetokounmpo", "Points", 28.5)), context_fn=_ctx)
    assert diff["line"] == 28.5                                 # the screenshot line is what's predicted
    assert diff["alerts"] == ["logged Underdog line 27.5 (pretip pull) differs from 28.5"]


def test_no_logged_line_two_points_or_another_teams_game(db):
    _snap(db, "Brandon Miller", "player_threes", 2.5, "2026-10-24T21:00:00+00:00", "catchup")
    _snap(db, "Brandon Miller", "player_threes_alternate", 3.5, "2026-10-24T21:00:00+00:00", "catchup")
    _snap(db, "Cade Cunningham", "player_assists", 8.5, "2026-10-24T21:00:00+00:00", "pretip",
          home="Boston Celtics", away="New York Knicks")      # not a Pistons game
    _snap(db, "Giannis Antetokounmpo", "player_points", 20.5, "2026-10-23T21:00:00+00:00", "pretip",
          game_date="2026-10-23")                              # yesterday
    rows = si.confirmation_rows(_rows(("Brandon Miller", "3PM", 2.5), ("Cade Cunningham", "Assists", 8.5),
                                      ("Giannis Antetokounmpo", "Points", 27.5)), context_fn=_ctx)
    assert [r["logged_line"] for r in rows] == [None, None, None]
    assert all(r["alerts"] == [] for r in rows)


def test_every_card_is_recorded_in_line_checks(db):
    _snap(db, "Giannis Antetokounmpo", "player_points", 27.5, "2026-10-24T21:30:00+00:00", "pretip")
    _snap(db, "Brandon Miller", "player_threes_alternate", 3.5, "2026-10-24T21:00:00+00:00", "catchup")
    rows = _rows(("Giannis Antetokounmpo", "Points", 27.5), ("Brandon Miller", "3-Pointers Made", 2.5),
                 ("Cade Cunningham", "Assists", 8.5), ("Nobody Known", "Points", 10.5))
    rows[2]["reads_agree"] = False
    si.confirmation_rows(rows, context_fn=_ctx)
    assert _checks(db) == [
        (203507, "Points", 27.5, 27.5, "pretip", 1, 1),
        (1641706, "3PM", 2.5, 3.5, "catchup", 0, 1),
        (1630595, "Assists", 8.5, None, None, None, 0),
        (None, "Points", 10.5, None, None, None, 1)]
    conn = sqlite3.connect(db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(line_checks)")]
    conn.close()
    assert not any("image" in c or "multiplier" in c for c in cols)   # numbers only


def test_record_false_writes_nothing(db):
    si.confirmation_rows(_rows(("Giannis Antetokounmpo", "Points", 27.5)), context_fn=_ctx, record=False)
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'line_checks'").fetchone()[0]
    conn.close()
    assert n == 0


def test_endpoint_records_line_checks_and_predicts_nothing(db, fake_ollama, monkeypatch):
    from fastapi.testclient import TestClient
    import data_fetcher
    import main
    monkeypatch.setattr(main, "LOCAL_HOSTS", main.LOCAL_HOSTS | {"testclient"})
    monkeypatch.setattr(data_fetcher, "get_player_context", _ctx)
    r = TestClient(main.app).post("/screenshot/extract",
                                  files={"file": ("grid.png", fake_ollama["data"], "image/png")})
    assert r.status_code == 200 and r.json()["extractor"] == "ollama"
    rows = r.json()["rows"]
    assert len(rows) == 6 and all(x["higher_multiplier"] is None for x in rows)
    assert len(_checks(db)) == 6
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 0
    conn.close()


# --- report ---------------------------------------------------------------------------------

def test_report_match_rate_by_stat_and_minutes_since_pull(db):
    si.record_line_checks(sqlite3.connect(db), [
        {"person_id": 1, "stat": "Points", "line": 27.5, "reads_agree": True,
         "logged_line": {"line": 27.5, "captured_at": "2026-10-24T21:00:00+00:00", "pull_phase": "pretip"}},
        {"person_id": 2, "stat": "Points", "line": 20.5, "reads_agree": True,
         "logged_line": {"line": 21.5, "captured_at": "2026-10-24T18:00:00+00:00", "pull_phase": "open"}},
        {"person_id": 3, "stat": "3PM", "line": 2.5, "reads_agree": False,
         "logged_line": {"line": 2.5, "captured_at": "2026-10-24T21:15:00+00:00", "pull_phase": "pretip"}},
        {"person_id": 4, "stat": "Assists", "line": 8.5, "reads_agree": True, "logged_line": None},
    ], "2026-10-24T21:20:00+00:00")
    r = line_checks_report.report(db)
    assert r["total"] == 4 and r["compared"] == 3 and r["overall"] == (2, 3) and r["reads_disagree"] == 1
    assert r["by_stat"] == {"3PM": (1, 1), "Points": (1, 2)}
    assert r["by_age"]["0-30 min"] == (2, 2) and r["by_age"]["120-240 min"] == (0, 1)


def test_report_without_the_table(tmp_path):
    path = str(tmp_path / "empty.db")
    sqlite3.connect(path).close()
    assert line_checks_report.report(path)["total"] == 0
