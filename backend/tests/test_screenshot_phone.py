"""
Underdog's iPhone game page (Players tab): telling it from desktop screenshots, its two
layouts (a stat tab's Higher / Lower list, Popular's tiles), cut-off items, the header's two
teams and the selected tab, initial + last-name resolution against those two rosters only,
and a header that disagrees with the schedule. Synthetic images only
(tests/synthetic_screenshot.py, built in memory); the reader is stubbed by the marker colours
each item carries, so a crop is "read" only when it holds one item's name and line.
"""
import io
import os
import sqlite3
import sys
import threading

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import database                                   # noqa: E402
import player_aliases as pa                       # noqa: E402
import rosters                                    # noqa: E402
import screenshot_import as si                    # noqa: E402
import synthetic_screenshot as ss                 # noqa: E402

U = ss.PHONE_W / 721
ROWS = [{"player_name": f"A. Player{i}", "line": 10.5 + i} for i in range(15)]
TILES = [{"player_name": f"A. PLAYER{i}", "stat": "Points", "line": 10.5 + i} for i in range(8)]


def _prepared(data: bytes):
    return Image.open(io.BytesIO(si.prepare_image(data)[0])).convert("RGB")


def _list_cut_bottom(frac):     # the 10th row shows `frac` of its height above the bottom
    return ss.render_phone("list", items=ROWS, first_y=1568 - 9 * ss.ROW_PITCH - frac * ss.ROW_H)


def _tiles_cut_bottom(frac):    # the third tile row shows `frac` of its height
    return ss.render_phone("tiles", items=TILES, first_y=1568 - 2 * ss.TILE_PITCH - frac * ss.TILE_H)


def _list_cut_top(frac):        # the 2nd row loses `frac` of its height under the sticky header
    return ss.render_phone("list", items=ROWS, first_y=ss.PHONE_CONTENT_TOP - frac * ss.ROW_H - ss.ROW_PITCH)


def _tiles_cut_top(frac):
    return ss.render_phone("tiles", first_y=ss.PHONE_CONTENT_TOP - frac * ss.TILE_H)


# --- phone or desktop -------------------------------------------------------------------

@pytest.mark.parametrize("data", [ss.render_grid(cols=1), ss.render(cards=ss.CARDS * 3), ss.render_grid(),
                                  ss.render_underdog(cols=1), ss.render_underdog(crop_top=60)],
                         ids=["tall-grid-1col", "tall-list", "grid", "underdog-1col", "underdog-cut"])
def test_desktop_screenshots_never_take_the_phone_branch(data):
    assert si.detect_phone(_prepared(data)) is None


def test_a_tall_narrow_desktop_crop_is_read_as_cards(monkeypatch):
    data = ss.render_grid(cols=1)                       # 428 x 2054: taller than any phone
    img = _prepared(data)
    assert img.height > si.PHONE_MIN_ASPECT * img.width
    prompts = []

    def fake(im, prompt):
        prompts.append(prompt)
        return {"player_name": "A B", "stat_label": "Points", "line": "1.5"}

    monkeypatch.setenv("SCREENSHOT_EXTRACTOR", "ollama")
    monkeypatch.setattr(si, "_ollama_json", fake)
    si.extract_rows(data)
    assert set(prompts) == {si.CARD_PROMPT}
    assert len(prompts) == len(ss.GRID_CARDS) * len(si.READ_SCALES)


def test_phone_layouts_header_and_selected_tab():
    lst = si.detect_phone(_prepared(ss.render_phone("list", tab="Rebounds")))
    assert lst["layout"] == "list" and len(lst["items"]) == len(ss.PHONE_ROWS)
    assert all(it["action"] == "read" for it in lst["items"])
    x0, y0, x1, y1 = lst["tab"]                          # the Rebounds pill (3rd), in 920-px coordinates
    s = 920 / ss.PHONE_W
    assert abs(x0 - (28 + 167 + 14 + 116 + 14) * U * s) < 6 and abs(y0 - 430 * U * s) < 6
    assert lst["header"] == (0, 0, 920, y0)
    tiles = si.detect_phone(_prepared(ss.render_phone("tiles", first_y=530)))
    assert tiles["layout"] == "tiles" and len(tiles["items"]) == 4
    boxes = [it["box"] for it in tiles["items"]]
    assert boxes[0][0] < boxes[1][0] and boxes[0][1] == boxes[1][1] and boxes[2][1] > boxes[0][1]


def test_no_selected_tab_and_no_header():
    assert si.detect_phone(_prepared(ss.render_phone(tab="none of them")))["tab"] is None
    top = si.detect_phone(_prepared(ss.render_phone(tabs_y=120, first_y=595)))
    assert top["header"] is None and top["items"]


@pytest.mark.parametrize("data, actions", [
    (_tiles_cut_top(0.02), ["flag", "flag", "read", "read"]),            # name, line, stat whole
    (_tiles_cut_top(0.2), ["unreadable", "unreadable", "read", "read"]),  # name clipped
    (_tiles_cut_top(0.6), ["skip", "skip", "read", "read"]),              # line clipped
    (_tiles_cut_bottom(0.8), ["skip", "skip", "read", "read", "flag", "flag"]),
    (_tiles_cut_bottom(0.6), ["unreadable", "unreadable", "read", "read", "skip", "skip"]),
    (_list_cut_top(0.05), ["flag"] + ["read"] * 8 + ["flag"]),
    (_list_cut_top(0.3), ["skip"] + ["read"] * 9),
    (_list_cut_bottom(0.7), ["read"] * 9 + ["flag"]),
    (_list_cut_bottom(0.4), ["read"] * 9 + ["skip"]),
], ids=["tile-top-2", "tile-top-20", "tile-top-60", "tile-bottom-80", "tile-bottom-60",
        "row-top-5", "row-top-30", "row-bottom-70", "row-bottom-40"])
def test_cut_items(data, actions):
    assert [it["action"] for it in si.detect_phone(_prepared(data))["items"]] == actions


# --- stubbed reader -----------------------------------------------------------------------

def _pack(c):
    return c[0] << 16 | c[1] << 8 | c[2]


def _colours(im) -> dict:
    a = np.asarray(im.convert("RGB")).reshape(-1, 3).astype(np.int64)
    vals, counts = np.unique(a[:, 0] << 16 | a[:, 1] << 8 | a[:, 2], return_counts=True)
    return {int(v): int(c) for v, c in zip(vals, counts) if c >= 20}


@pytest.fixture
def reader(monkeypatch):
    """_ollama_json stand-in. Header: BKN / BOS when the crop shows the header mark and no item.
    Tab: state['labels'] (one per read, in call order) when the crop is mostly the selected pill.
    Tile / row: its text when the crop holds exactly one item's name and line marks, else nulls."""
    state = {"items": None, "labels": None, "teams": ("BKN", "BOS"), "calls": [], "lock": threading.Lock()}

    def fake(im, prompt):
        seen = _colours(im)
        items = state["items"]
        names = [i for i in range(len(items)) if _pack(ss.name_mark(i)) in seen]
        lines = [i for i in range(len(items)) if _pack(ss.line_mark(i)) in seen]
        with state["lock"]:
            state["calls"].append((prompt, names[0] if names == lines and len(names) == 1 else None))
            n_tab = sum(p == si.TAB_PROMPT for p, _ in state["calls"])
        if prompt == si.HEADER_PROMPT:
            ok = _pack(ss.PHONE_HEADER_MARK) in seen and not names and not lines
            return dict(zip(("left_team", "right_team"), state["teams"] if ok else (None, None)))
        if prompt == si.TAB_PROMPT:
            on = seen.get(_pack(ss.PHONE_PILL_ON), 0) / (im.width * im.height)
            return {"label": state["labels"][n_tab - 1] if on > 0.4 else None}
        if names != lines or len(names) != 1:
            return {"player_name": None, "line": None}
        c = items[names[0]]
        if prompt == si.ROW_PROMPT:
            return {"player_name": c["player_name"], "line": f"{c['line']:g}", "arrows": ["up", "down"]}
        assert prompt == si.CARD_PROMPT
        return {"player_name": c["player_name"], "stat_label": c["stat"], "line": f"{c['line']:g}",
                "buttons": ["Higher", "Lower"]}

    monkeypatch.setenv("SCREENSHOT_EXTRACTOR", "ollama")
    monkeypatch.setattr(si, "_ollama_json", fake)
    return state


def _prompts(state):
    return [p for p, _ in state["calls"]]


def test_list_rows_take_the_stat_from_the_selected_tab_and_the_teams_from_the_header(reader):
    reader.update(items=ss.PHONE_ROWS, labels=["3-Pointers", "3-Pointers"])
    _, rows = si.extract_rows(ss.render_phone("list", tab="3-Pointers"))
    assert [(r["player_name"], r["line"], r["stat"], r["stat_text"]) for r in rows] == \
        [(c["player_name"], c["line"], "3PM", "3-Pointers") for c in ss.PHONE_ROWS]
    assert all(r["game_teams"] == ["BKN", "BOS"] and r["sides_offered"] == "both" for r in rows)
    assert all(r["reads_agree"] and r["alerts"] == [] for r in rows)
    assert all(r["higher_multiplier"] is None and r["lower_multiplier"] is None for r in rows)
    p = _prompts(reader)
    assert p.count(si.HEADER_PROMPT) == 2 and p.count(si.TAB_PROMPT) == 2
    assert p.count(si.ROW_PROMPT) == 2 * len(ss.PHONE_ROWS)
    assert sorted(i for q, i in reader["calls"] if q == si.ROW_PROMPT) == sorted(list(range(4)) * 2)


def test_popular_tiles_carry_their_own_stat_and_the_same_player_twice(reader):
    reader.update(items=ss.PHONE_TILES)
    _, rows = si.extract_rows(ss.render_phone("tiles", first_y=530))
    assert [(r["player_name"], r["stat"], r["line"]) for r in rows] == \
        [(c["player_name"], c["stat"], c["line"]) for c in ss.PHONE_TILES]
    assert all(r["game_teams"] == ["BKN", "BOS"] for r in rows)
    assert si.TAB_PROMPT not in _prompts(reader) and si.ROW_PROMPT not in _prompts(reader)


@pytest.mark.parametrize("labels", [["Pts+Rebs+Asts"] * 2, ["Points", "Rebounds"], [None, None]],
                         ids=["unsupported-tab", "reads-disagree", "not-read"])
def test_a_tab_that_is_not_one_stat_gives_no_stat(reader, labels):
    reader.update(items=ss.PHONE_ROWS, labels=labels)
    _, rows = si.extract_rows(ss.render_phone("list"))
    assert len(rows) == 4 and all(r["stat"] is None for r in rows)
    assert {r["stat_text"] for r in rows} == {labels[0] if labels[0] == labels[1] and labels[0] else ""}


def test_partly_cut_items_are_read_and_flagged_and_clipped_ones_are_not_guessed(reader):
    reader.update(items=ss.PHONE_TILES)
    _, rows = si.extract_rows(_tiles_cut_top(0.02))
    assert [r["player_name"] for r in rows] == [c["player_name"] for c in ss.PHONE_TILES]
    assert [r["alerts"] for r in rows] == [[si.PARTLY_CUT]] * 2 + [[]] * 2

    reader.update(items=ss.PHONE_TILES, calls=[])
    _, rows = si.extract_rows(_tiles_cut_top(0.2))                # names clipped: unreadable, not read
    assert [r.get("unreadable", False) for r in rows] == [True, True, False, False]
    assert sorted(i for p, i in reader["calls"] if p == si.CARD_PROMPT) == [2, 2, 3, 3]

    reader.update(items=ROWS, labels=["Points"] * 2, calls=[])
    _, rows = si.extract_rows(_list_cut_bottom(0.4))              # last row's line clipped: skipped
    assert [r["player_name"] for r in rows] == [c["player_name"] for c in ROWS[:9]]
    assert 9 not in [i for _, i in reader["calls"]]

    reader.update(calls=[])
    _, rows = si.extract_rows(_list_cut_bottom(0.7))
    assert [r["player_name"] for r in rows] == [c["player_name"] for c in ROWS[:10]]
    assert rows[-1]["alerts"] == [si.PARTLY_CUT] and all(r["alerts"] == [] for r in rows[:-1])


@pytest.mark.parametrize("data, header_reads", [(ss.render_phone(header=False), 2),
                                                (ss.render_phone(tabs_y=120, first_y=595), 0)],
                         ids=["blank-header", "no-header-above-the-tabs"])
def test_no_header_leaves_game_teams_empty(reader, data, header_reads):
    reader.update(items=ss.PHONE_ROWS, labels=["Points"] * 2)
    _, rows = si.extract_rows(data)
    assert rows and all(r["game_teams"] is None for r in rows)
    assert _prompts(reader).count(si.HEADER_PROMPT) == header_reads


def test_header_reads_that_disagree_leave_game_teams_empty(reader, monkeypatch):
    reader.update(items=ss.PHONE_ROWS, labels=["Points"] * 2)
    answers = iter([{"left_team": "BKN", "right_team": "BOS"}, {"left_team": "BKN", "right_team": "BOX"}])
    lock = threading.Lock()
    inner = si._ollama_json

    def fake(im, prompt):
        if prompt == si.HEADER_PROMPT:
            with lock:
                return next(answers)
        return inner(im, prompt)

    monkeypatch.setattr(si, "_ollama_json", fake)
    _, rows = si.extract_rows(ss.render_phone("list"))
    assert all(r["game_teams"] is None for r in rows)


def test_phone_images_and_crops_are_never_written_to_disk(reader, monkeypatch):
    real_save = Image.Image.save

    def save(self, fp, *a, **k):
        assert isinstance(fp, io.BytesIO), f"image saved to {fp!r}"
        return real_save(self, fp, *a, **k)

    monkeypatch.setattr(Image.Image, "save", save)
    reader.update(items=ss.PHONE_ROWS, labels=["Points"] * 2)
    data = ss.render_phone("list")
    _, rows = si.extract_rows(data)
    assert len(rows) == 4


# --- resolution against the header's two teams ------------------------------------------

DORT, MILES = 1629652, 1630540
TEAMS = {"BKN": (1610612751, "Brooklyn", "Nets"), "BOS": (1610612738, "Boston", "Celtics"),
         "CLE": (1610612739, "Cleveland", "Cavaliers"), "OKC": (1610612760, "Oklahoma City", "Thunder"),
         "DAL": (1610612742, "Dallas", "Mavericks"), "NY": (1610612752, "New York", "Knicks"),
         "ATL": (1610612737, "Atlanta", "Hawks")}
ROSTER = [("BKN", "Michael Porter Jr."), ("BKN", "Ziaire Williams"), ("BOS", "Jayson Tatum"),
          ("BOS", "Paul George"), ("BOS", "Robert Williams III"), ("CLE", "Marcus Porter"),
          ("OKC", "Shai Gilgeous-Alexander"), ("DAL", "P.J. Washington"), ("NY", "OG Anunoby"),
          ("NY", "Miles McBride"), ("NY", "James Johnson"), ("ATL", "Luguentz Dort"), ("ATL", "Jalen Johnson")]


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "phone.db")
    monkeypatch.setattr(database, "DB_PATH", path)
    database._person_index_cache.clear()
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE rosters (athlete_id TEXT, player_name TEXT, name_key TEXT, espn_team_id TEXT, team_abbrev TEXT,
                              nba_team_id INTEGER, team_city TEXT, team_name TEXT, position TEXT, jersey TEXT,
                              season TEXT, fetched_at TEXT);
        CREATE TABLE espn_person_map (athlete_id TEXT, player_name TEXT, team_name TEXT, person_id INTEGER,
                                      status TEXT, reason TEXT);
        CREATE TABLE player_name_collisions (name_key_a TEXT, name_a TEXT, team_a TEXT, name_key_b TEXT, name_b TEXT,
                                             team_b TEXT, reasons TEXT);
        CREATE TABLE players (personId INTEGER, firstName TEXT, lastName TEXT);
        CREATE TABLE player_stats (personId INTEGER, firstName TEXT, lastName TEXT, gameDateTimeEst TEXT,
                                   gameType TEXT, playerteamName TEXT, opponentteamName TEXT, numMinutes REAL,
                                   points REAL, reboundsTotal REAL, assists REAL, threePointersMade REAL,
                                   blocks REAL, steals REAL, turnovers REAL);
    """)
    conn.executemany("INSERT INTO rosters VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
        (str(i), name, rosters.name_key(name), "0", abbr, TEAMS[abbr][0], TEAMS[abbr][1], TEAMS[abbr][2],
         "G", "0", "2026-27", "x") for i, (abbr, name) in enumerate(ROSTER)])
    ids = {name: str(i) for i, (_, name) in enumerate(ROSTER)}
    conn.executemany("INSERT INTO espn_person_map VALUES (?,?,?,?,?,?)", [
        (ids["Luguentz Dort"], "Luguentz Dort", "Hawks", DORT, "ok", None),
        (ids["Miles McBride"], "Miles McBride", "Knicks", MILES, "ok", None)])
    conn.executemany("INSERT INTO players VALUES (?,?,?)", [(DORT, "Luguentz", "Dort"), (MILES, "Miles", "McBride")])
    conn.commit()
    pa.init_table(conn)                     # seeds 'Lu Dort' and 'Deuce McBride'
    conn.close()
    return path


def _on_game(name, teams):
    conn = database.get_connection()
    try:
        return si.resolve_on_game(conn, name, teams)
    finally:
        conn.close()


@pytest.mark.parametrize("name, teams, player, opponent", [
    ("J. Tatum", ["BKN", "BOS"], "Jayson Tatum", "Brooklyn Nets"),
    ("Jayson Tatum", ["BKN", "BOS"], "Jayson Tatum", "Brooklyn Nets"),          # a full name: exact
    ("M. Porter Jr.", ["BKN", "BOS"], "Michael Porter Jr.", "Boston Celtics"),   # Marcus Porter (CLE) not on the game
    ("M. PORTER JR.", ["BKN", "BOS"], "Michael Porter Jr.", "Boston Celtics"),
    ("R. Williams III", ["BKN", "BOS"], "Robert Williams III", "Brooklyn Nets"),  # Ziaire Williams: other initial
    ("S. Gilgeous-Alexander", ["OKC", "DAL"], "Shai Gilgeous-Alexander", "Dallas Mavericks"),
    ("P. Washington", ["OKC", "DAL"], "P.J. Washington", "Oklahoma City Thunder"),
    ("O. Anunoby", ["NYK", "ATL"], "OG Anunoby", "Atlanta Hawks"),              # NBA abbreviation in the header
    ("L. Dort", ["NY", "ATL"], "Luguentz Dort", "New York Knicks"),             # roster + alias 'Lu Dort': one player
    ("D. McBride", ["NY", "ATL"], "Miles McBride", "Atlanta Hawks"),            # only via the alias 'Deuce McBride'
])
def test_initial_and_last_name_on_the_two_teams(db, name, teams, player, opponent):
    who = _on_game(name, teams)
    assert (who["status"], who["player_name"], who["game_opponent"]) == ("ok", player, opponent)
    if name == "D. McBride":
        assert who["person_id"] == MILES and who["via"] == "alias"


@pytest.mark.parametrize("name, teams, status", [
    ("J. Johnson", ["NY", "ATL"], "ambiguous"),          # James (NY) and Jalen (ATL): never auto-matched
    ("M. Porter Jr.", ["BKN", "CLE"], "ambiguous"),      # Michael and Marcus, both on the game
    ("S. Gilgeous-Alexander", ["BKN", "BOS"], "not_found"),   # never league-wide
    ("D. McBride", ["BOS", "ATL"], "not_found"),         # the alias's player is not on the game
    ("J. Tatu", ["BKN", "BOS"], "not_found"),
    ("J. Tatum", None, "no_game_header"),
    ("J. Tatum", ["BKN", "XYZ"], "no_game_header"),
    ("J. Tatum", ["BOS", "BOS"], "no_game_header"),
    ("J. Tatum", ["BOS"], "no_game_header"),
])
def test_unresolved_on_the_two_teams(db, name, teams, status):
    assert _on_game(name, teams)["status"] == status


def _phone_rows(name, teams, ctx):
    return si.confirmation_rows([{"player_name": name, "team": None, "stat": "Points", "stat_text": "Points",
                                  "line": 24.5, "alerts": [], "game_teams": teams}],
                                context_fn=lambda n: ctx, record=False)[0]


NEXT = {"next_opponent_full": "Brooklyn Nets", "is_home": True, "is_back_to_back": False, "next_game_date": "2026-10-27"}


def test_a_resolved_phone_row_takes_its_game_from_the_schedule_as_on_desktop(db):
    r = _phone_rows("J. Tatum", ["BKN", "BOS"], NEXT)
    assert (r["match"], r["resolved_player"], r["opponent_team"], r["is_home"], r["game_date"]) == \
        ("ok", "Jayson Tatum", "Brooklyn Nets", True, "2026-10-27")
    assert r["alerts"] == []


@pytest.mark.parametrize("ctx, says", [
    ({**NEXT, "next_opponent_full": "Cleveland Cavaliers", "next_game_date": "2026-10-29"},
     "(Cleveland Cavaliers, 2026-10-29)"),
    ({**NEXT, "next_opponent_full": None, "next_game_date": None}, "(none found)"),
    (None, "(none found)"),
])
def test_a_header_that_is_not_his_next_game_leaves_the_row_unresolved(db, ctx, says):
    r = _phone_rows("J. Tatum", ["BKN", "BOS"], ctx)
    assert (r["match"], r["resolved_player"], r["opponent_team"], r["person_id"]) == ("game_mismatch", "", "", None)
    assert r["alerts"] == [f"player not resolved: the screenshot's game (BKN vs BOS) is not his next scheduled "
                           f"game {says}: likely a screenshot from another game day"]


def test_no_header_ambiguous_and_not_found_phone_rows(db):
    r = _phone_rows("J. Tatum", None, NEXT)
    assert (r["match"], r["resolved_player"]) == ("no_game_header", "")
    assert r["alerts"] == ["player not resolved: the game header's two teams couldn't be read"]
    assert _phone_rows("J. Johnson", ["NY", "ATL"], NEXT)["alerts"] == \
        ["player not resolved: several roster players match"]
    assert _phone_rows("X. Nobody", ["BKN", "BOS"], NEXT)["alerts"] == \
        ["player not resolved: no roster match on BKN or BOS"]


def test_end_to_end_list_screenshot_to_confirmation_rows(db, reader):
    reader.update(items=ss.PHONE_ROWS, labels=["Points"] * 2)
    _, rows = si.extract_rows(ss.render_phone("list"))
    ctx = {"Jayson Tatum": {**NEXT}, "Michael Porter Jr.": {**NEXT, "next_opponent_full": "Boston Celtics",
                                                             "is_home": False},
           "Paul George": {**NEXT}}
    out = si.confirmation_rows(rows, context_fn=lambda n: ctx.get(n), record=False)
    assert [(r["match"], r["resolved_player"], r["stat"], r["line"], r["opponent_team"]) for r in out] == [
        ("ok", "Jayson Tatum", "Points", 24.5, "Brooklyn Nets"),
        ("ok", "Michael Porter Jr.", "Points", 19.5, "Boston Celtics"),
        ("ok", "Paul George", "Points", 19.5, "Brooklyn Nets"),
        ("not_found", "", "Points", 14.5, "")]                 # D. White: not on this test roster
