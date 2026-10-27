"""
Screenshot import: image validation (memory only), the swappable extractor (Claude /
Ollama, both stubbed), normalization, player resolution (exact name_key + team), the
historical-range flag, and POST /screenshot/extract. The sample image is synthetic
(tests/synthetic_screenshot.py) and built in memory; nothing is written to disk.
Set RUN_LIVE_EXTRACTOR=1 to also run the configured extractor on it for real.
"""
import io
import json
import os
import random
import sqlite3
import sys
import tempfile
import types

import pytest
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import database
import screenshot_import as si
import synthetic_screenshot as ss

RAW = [  # what an extractor returns for ss.CARDS (strings and labels as a model writes them)
    {"player_name": "Giannis Antetokounmpo", "team": "MIA", "stat": "Points", "line": 27.5,
     "higher_multiplier": "1.03x", "lower_multiplier": 0.88},
    {"player_name": "Brandon Miller", "team": "CHA", "stat": "3-Pointers Made", "line": "2.5",
     "higher_multiplier": None, "lower_multiplier": None},
    {"player_name": "Jalen Williams", "team": "OKC", "stat": "Rebounds", "line": 5.5,
     "higher_multiplier": 0.92, "lower_multiplier": 1.05},
    {"player_name": "Cade Cunningham", "team": None, "stat": "Assists", "line": 8.5,
     "higher_multiplier": 1.0, "lower_multiplier": 1.0},
]


# --- image handling ------------------------------------------------------------------

def test_synthetic_png_is_accepted_and_reencoded():
    png, media = si.prepare_image(ss.render())
    assert media == "image/png" and Image.open(io.BytesIO(png)).format == "PNG"


def test_jpeg_is_accepted_and_large_images_are_scaled_down():
    buf = io.BytesIO()
    Image.new("RGB", (4000, 1000), (20, 20, 20)).save(buf, format="JPEG")
    png, _ = si.prepare_image(buf.getvalue())
    assert max(Image.open(io.BytesIO(png)).size) == si.MAX_IMAGE_SIDE


@pytest.mark.parametrize("data", [b"", b"not an image at all", b"%PDF-1.4 ..."])
def test_non_images_are_rejected(data):
    with pytest.raises(si.BadImage):
        si.prepare_image(data)


def test_oversized_upload_is_rejected(monkeypatch):
    monkeypatch.setattr(si, "MAX_IMAGE_BYTES", 100)
    with pytest.raises(si.BadImage, match="over"):
        si.prepare_image(ss.render())


# --- normalization ------------------------------------------------------------------------

def test_rows_are_normalized():
    rows = [si.normalize_row(r) for r in RAW]
    assert [(r["player_name"], r["team"], r["stat"], r["line"], r["higher_multiplier"], r["lower_multiplier"])
            for r in rows] == [
        ("Giannis Antetokounmpo", "MIA", "Points", 27.5, 1.03, 0.88),
        ("Brandon Miller", "CHA", "3PM", 2.5, None, None),
        ("Jalen Williams", "OKC", "Rebounds", 5.5, 0.92, 1.05),
        ("Cade Cunningham", None, "Assists", 8.5, 1.0, 1.0)]


def test_rows_without_a_name_or_line_are_dropped_and_odd_stats_kept_unmapped():
    assert si.normalize_row({"player_name": "", "line": 3}) is None
    assert si.normalize_row({"player_name": "X", "line": None}) is None
    r = si.normalize_row({"player_name": "X", "stat": "Fantasy Points", "line": 40.5})
    assert r["stat"] is None and r["stat_text"] == "Fantasy Points"


# --- swappable extractor ----------------------------------------------------------------

def test_extractor_setting(monkeypatch):
    monkeypatch.delenv("SCREENSHOT_EXTRACTOR", raising=False)
    assert si.extractor_name() == "ollama" and si.OLLAMA_MODEL == "gemma4:12b"
    monkeypatch.setenv("SCREENSHOT_EXTRACTOR", "Claude")
    assert si.extractor_name() == "claude"
    monkeypatch.setenv("SCREENSHOT_EXTRACTOR", "tesseract")
    with pytest.raises(si.ExtractorUnavailable, match="SCREENSHOT_EXTRACTOR"):
        si.extractor_name()


def test_claude_without_a_key_is_unavailable(monkeypatch):
    monkeypatch.setenv("SCREENSHOT_EXTRACTOR", "claude")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(si.ExtractorUnavailable, match="ANTHROPIC_API_KEY"):
        si.extract_rows(ss.render())


def test_claude_request_sends_the_image_and_schema_and_parses_json(monkeypatch):
    import anthropic
    sent = {}

    class FakeMessages:
        def create(self, **kw):
            sent.update(kw)
            return types.SimpleNamespace(stop_reason="end_turn", content=[
                types.SimpleNamespace(type="text", text=json.dumps({"rows": RAW}))])

    class FakeClient:
        def __init__(self, *a, **kw):
            self.beta = types.SimpleNamespace(messages=FakeMessages())

    monkeypatch.setenv("SCREENSHOT_EXTRACTOR", "claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    name, rows = si.extract_rows(ss.render())
    assert name == "claude" and [r["stat"] for r in rows] == ["Points", "3PM", "Rebounds", "Assists"]
    assert sent["model"] == si.CLAUDE_MODEL
    assert sent["output_config"]["format"] == {"type": "json_schema", "schema": si.SCHEMA}
    image, text = sent["messages"][0]["content"]
    assert image["type"] == "image" and image["source"]["media_type"] == "image/png"
    assert Image.open(io.BytesIO(__import__("base64").b64decode(image["source"]["data"]))).format == "PNG"
    assert "never infer" in text["text"] and "predict" not in text["text"].lower()


def test_claude_refusal_is_unavailable(monkeypatch):
    import anthropic

    class FakeClient:
        def __init__(self, *a, **kw):
            self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(
                create=lambda **kw: types.SimpleNamespace(stop_reason="refusal", content=[])))

    monkeypatch.setenv("SCREENSHOT_EXTRACTOR", "claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    with pytest.raises(si.ExtractorUnavailable, match="declined"):
        si.extract_rows(ss.render())


def test_ollama_down_is_unavailable(monkeypatch):
    import httpx

    def post(url, **kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setenv("SCREENSHOT_EXTRACTOR", "ollama")
    monkeypatch.setattr(si.httpx, "post", post)
    with pytest.raises(si.ExtractorUnavailable, match="Ollama"):
        si.extract_rows(ss.render())


# --- resolution and range flags (temp DB) ------------------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "test.db")
    monkeypatch.setattr(database, "DB_PATH", path)
    database._person_index_cache.clear()
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE rosters (athlete_id TEXT, player_name TEXT, name_key TEXT, team_abbrev TEXT, "
                 "team_city TEXT, team_name TEXT)")
    conn.executemany("INSERT INTO rosters VALUES (?,?,?,?,?,?)", [
        ("1", "Giannis Antetokounmpo", "giannis antetokounmpo", "MIA", "Miami", "Heat"),
        ("2", "Brandon Miller", "brandon miller", "CHA", "Charlotte", "Hornets"),
        ("3", "Jalen Williams", "jalen williams", "OKC", "Oklahoma City", "Thunder"),
        ("4", "Jaylin Williams", "jaylin williams", "OKC", "Oklahoma City", "Thunder"),
        ("5", "Cade Cunningham", "cade cunningham", "DET", "Detroit", "Pistons"),
        ("6", "Jalen Smith", "jalen smith", "CHI", "Chicago", "Bulls"),
        ("7", "Jalen Smith", "jalen smith", "PHX", "Phoenix", "Suns"),
    ])
    conn.execute("CREATE TABLE player_stats (personId INTEGER, firstName TEXT, lastName TEXT, "
                 "gameDateTimeEst TEXT, gameType TEXT, opponentteamName TEXT, points REAL, "
                 "reboundsTotal REAL, assists REAL, threePointersMade REAL)")
    conn.execute("CREATE TABLE players (personId INTEGER, firstName TEXT, lastName TEXT)")
    random.seed(1)
    rows = []
    for pid, first, last, pts in ((10, "Giannis", "Antetokounmpo", (18, 44)), (20, "Brandon", "Miller", (8, 30)),
                                  (30, "Jalen", "Williams", (10, 32))):
        conn.execute("INSERT INTO players VALUES (?,?,?)", (pid, first, last))
        for g in range(90):
            rows.append((pid, first, last, f"2026-0{1 + g % 4}-{1 + g % 28:02d} 19:30:00", "Regular Season",
                         "Knicks", random.randint(*pts), random.randint(2, 12), random.randint(1, 9),
                         random.randint(0, 5)))
    conn.executemany("INSERT INTO player_stats VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    database.init_db()
    return path


def _resolve(name, team):
    conn = database.get_connection()
    try:
        return si.resolve_player(conn, name, team)
    finally:
        conn.close()


def test_exact_name_and_team_resolves_one_player(db):
    assert _resolve("Jalen Williams", "OKC") == {"status": "ok", "player_name": "Jalen Williams",
                                                 "team_abbrev": "OKC", "team_name": "Oklahoma City Thunder"}
    assert _resolve("Cade Cunningham", None)["status"] == "ok"          # no team on the card: one roster match


def test_wrong_team_or_two_matches_stay_blank(db):
    # on a roster, not on the card's team: still blank, with where he is for the flag tooltip
    assert _resolve("Jalen Williams", "DEN") == {"status": "team_mismatch", "roster_teams": ["Oklahoma City Thunder"]}
    assert _resolve("Jalen Smith", None) == {"status": "ambiguous"}       # two Jalen Smiths, no team shown
    assert _resolve("Jalen Smith", "PHX")["status"] == "ok"               # the card's team settles it
    assert _resolve("Jalen Wiliams", "OKC") == {"status": "not_found"}    # no fuzzy matching


def test_nba_style_card_abbreviations_match_espn_roster_abbreviations():
    """ESPN rosters say UTAH / GS / NY / SA / NO / WSH; pick'em cards print UTA / GSW / NYK / ..."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE rosters (player_name TEXT, name_key TEXT, team_abbrev TEXT, team_city TEXT, "
                 "team_name TEXT, nba_team_id INTEGER)")
    conn.executemany("INSERT INTO rosters VALUES (?,?,?,?,?,?)", [
        ("Jaxson Hayes", "jaxson hayes", "UTAH", "Utah", "Jazz", 1610612762),
        ("Stephen Curry", "stephen curry", "GS", "Golden State", "Warriors", 1610612744),
        ("Jalen Brunson", "jalen brunson", "NY", "New York", "Knicks", 1610612752)])
    for name, card in (("Jaxson Hayes", "UTA"), ("Jaxson Hayes", "UTAH"), ("Jaxson Hayes", "JAZZ"),
                       ("Stephen Curry", "GSW"), ("Jalen Brunson", "NYK")):
        assert si.resolve_player(conn, name, card)["status"] == "ok", (name, card)
    assert si.resolve_player(conn, "Jaxson Hayes", "LAL") == {"status": "team_mismatch",
                                                                             "roster_teams": ["Utah Jazz"]}


def _range_db(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE player_stats (personId INTEGER, gameDateTimeEst TEXT, gameType TEXT, numMinutes REAL, "
                 "points REAL, reboundsTotal REAL, assists REAL, threePointersMade REAL, blocks REAL, steals REAL, "
                 "turnovers REAL)")
    conn.executemany("INSERT INTO player_stats VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    return conn


def _g(date, pts, gtype="Regular Season", minutes=30.0, pid=1):
    return (pid, f"{date} 19:30:00", gtype, minutes, pts, 5, 3, 1, 0, 1, 2)


def test_range_window_spans_seasons_regular_season_played_games_only():
    rows = [_g(f"2024-{1 + i % 4:02d}-{1 + i % 28:02d}", 10 + i % 5) for i in range(40)]       # 2023-24
    rows += [_g(f"2025-0{1 + i % 3}-{1 + i % 28:02d}", 20 + i % 5) for i in range(30)]         # 2024-25
    rows += [_g(f"2026-10-{20 + i}", 30) for i in range(3)]                                     # 2026-27
    rows += [_g("2025-05-01", 60, "Playoffs"), _g("2025-04-16", 55, "Play-in Tournament"),      # not regular season
             _g("2026-10-23", 0, minutes=0.0), _g("2023-11-14", 12, "NBA Emirates Cup")]
    rows[-2] = (1, "2026-10-23 19:30:00", "Regular Season", 0.0, 0, 0, 0, 0, 0, 0, 0)            # a DNP row
    rng = si.historical_range(_range_db(rows), 1, "Points")
    assert rng["games"] == 74                     # 40 + 30 + 3 + the 2023 Cup game; no playoffs, play-in or DNP
    assert rng["max"] == 30.0 and rng["min"] == 10.0
    many = [_g(f"20{20 + i // 60}-{1 + i % 12:02d}-{1 + i % 28:02d}", i) for i in range(200)]
    assert si.historical_range(_range_db(many), 1, "Points")["games"] == si.RANGE_GAMES       # last 82, any season


def test_line_flag_needs_ten_games_and_an_implausible_line():
    nine = {"min": 10.0, "max": 20.0, "games": 9}
    ten = {**nine, "games": 10}
    assert not si.implausible_line(99.5, nine)                    # fewer than 10 games: never
    assert si.implausible_line(30.5, ten)                         # above 1.5 x 20
    assert not si.implausible_line(29.5, ten)                     # outside the range, but plausible
    assert not si.implausible_line(5.5, ten)                      # half the min exactly: not below it
    assert si.implausible_line(4.5, ten)                          # below 0.5 x 10
    assert not si.implausible_line(15.5, None)


def test_confirmation_rows_skip_the_flag_for_a_thin_history(db):
    conn = sqlite3.connect(database.DB_PATH)
    conn.execute("DELETE FROM player_stats WHERE personId = 20 AND rowid NOT IN "
                 "(SELECT rowid FROM player_stats WHERE personId = 20 LIMIT 9)")       # Brandon Miller: 9 games
    conn.commit()
    conn.close()
    rows = [si.normalize_row(r) for r in RAW]
    rows[1]["line"] = 99.5
    b = si.confirmation_rows(rows, context_fn=_context)[1]
    assert b["resolved_player"] == "Brandon Miller" and b["range"]["games"] == 9 and b["flags"] == []


def _context(name):
    return {"next_opponent_full": {"Giannis Antetokounmpo": "Charlotte Hornets"}.get(name, "Miami Heat"),
            "is_home": False, "is_back_to_back": True, "next_game_date": "2026-10-24"}


def test_confirmation_rows_resolve_fill_context_and_flag_ranges(db):
    rows = [si.normalize_row(r) for r in RAW]
    rows[0]["line"] = 75.5                                               # misread: > 1.5x his max (44)
    out = si.confirmation_rows(rows + [si.normalize_row({"player_name": "Jalen Smith", "stat": "Points",
                                                         "line": 9.5})], context_fn=_context)
    g, b, j, c, js = out
    assert g["resolved_player"] == "Giannis Antetokounmpo" and g["opponent_team"] == "Charlotte Hornets"
    assert g["game_date"] == "2026-10-24" and g["is_back_to_back"] is True
    assert any("far outside the range" in f for f in g["flags"]) and g["range"]["games"] == 82
    assert b["flags"] == [] and j["flags"] == []                         # lines inside their ranges
    assert c["resolved_player"] == "Cade Cunningham" and c["flags"] == ["no game history for this stat"]
    assert js["resolved_player"] == "" and js["match"] == "ambiguous" and js["opponent_team"] == ""


def test_unsupported_stat_is_flagged(db):
    [r] = si.confirmation_rows([si.normalize_row({"player_name": "Brandon Miller", "team": "CHA",
                                                  "stat": "Fantasy Points", "line": 30.5})], context_fn=_context)
    assert r["stat"] is None and r["flags"][0].startswith("stat 'Fantasy Points' is not one of")


# --- POST /screenshot/extract -----------------------------------------------------------------

@pytest.fixture
def client(db, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import main
    import data_fetcher
    monkeypatch.setenv("SCREENSHOT_EXTRACTOR", "claude")
    monkeypatch.setitem(si._EXTRACT, "claude", lambda png, media: RAW)
    monkeypatch.setattr(data_fetcher, "get_player_context", _context)
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(spool))           # where an upload over 1 MB spools
    return TestClient(main.app), spool


def _predictions():
    conn = sqlite3.connect(database.DB_PATH)
    try:
        return conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
    finally:
        conn.close()


def test_extract_returns_rows_and_predicts_nothing(client):
    c, _ = client
    r = c.post("/screenshot/extract", files={"file": ("cards.png", ss.render(), "image/png")})
    assert r.status_code == 200
    body = r.json()
    assert body["extractor"] == "claude"
    assert [x["resolved_player"] for x in body["rows"]] == ["Giannis Antetokounmpo", "Brandon Miller",
                                                            "Jalen Williams", "Cade Cunningham"]
    assert [x["stat"] for x in body["rows"]] == ["Points", "3PM", "Rebounds", "Assists"]
    assert _predictions() == 0                                     # nothing until Predict is clicked


def test_large_upload_leaves_no_file_behind(client):
    c, spool = client
    buf = io.BytesIO()
    Image.frombytes("RGB", (1000, 1000), os.urandom(3_000_000)).save(buf, format="PNG")
    assert buf.tell() > 1024 * 1024                                # big enough to spool to disk
    assert c.post("/screenshot/extract", files={"file": ("big.png", buf.getvalue(), "image/png")}).status_code == 200
    assert list(spool.iterdir()) == []


def test_extract_errors(client, monkeypatch):
    c, _ = client
    assert c.post("/screenshot/extract", files={"file": ("x.txt", b"hello", "text/plain")}).status_code == 400

    def unavailable(png, media):
        raise si.ExtractorUnavailable("ANTHROPIC_API_KEY is not set in backend/.env")

    monkeypatch.setitem(si._EXTRACT, "claude", unavailable)
    r = c.post("/screenshot/extract", files={"file": ("cards.png", ss.render(), "image/png")})
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]
    def unreadable(png, media):
        return json.loads("this is not json")                      # model wrote prose, not JSON

    monkeypatch.setitem(si._EXTRACT, "claude", unreadable)
    r = c.post("/screenshot/extract", files={"file": ("c.png", ss.render(), "image/png")})
    assert r.status_code == 502 and "unreadable" in r.json()["detail"]
    assert _predictions() == 0


@pytest.mark.real_auth
def test_extract_needs_the_login(db):
    from fastapi.testclient import TestClient
    import main
    r = TestClient(main.app).post("/screenshot/extract", files={"file": ("c.png", ss.render(), "image/png")})
    assert r.status_code == 401


# --- optional: the real extractor on the synthetic image ------------------------------------

@pytest.mark.live_network
@pytest.mark.skipif(os.getenv("RUN_LIVE_EXTRACTOR") != "1", reason="set RUN_LIVE_EXTRACTOR=1 to call the real extractor")
@pytest.mark.parametrize("image, cards", [(ss.render, ss.CARDS), (ss.render_grid, ss.GRID_CARDS)],
                         ids=["list", "grid"])
def test_live_extractor_reads_the_synthetic_screenshot(image, cards):
    name, rows = si.extract_rows(image())
    got = [(r["player_name"], r["stat"], r["line"]) for r in rows]
    assert got == [(c["player_name"], c["stat"], c["line"]) for c in cards], f"{name} misread: {got}"
    if name == "claude":                                       # the Ollama path doesn't read multipliers
        assert [(r["higher_multiplier"], r["lower_multiplier"]) for r in rows] == \
            [(c["higher"], c["lower"]) for c in cards]
    else:
        assert all(r["reads_agree"] for r in rows)
