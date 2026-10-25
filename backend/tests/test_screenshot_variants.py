"""
Underdog card variants (synthetic, tests/synthetic_screenshot.render_underdog): dark outlined
cards whose fill matches the page, with or without line arrows, with both pick buttons or only
Higher / only Lower. The cards (not a lone wide button) are detected, including a first row cut
by the image edge; each card is read with the sides it offers; one unreadable card becomes an
unresolved flagged row instead of failing the image; a GPU-memory failure is reported as one
short message. Ollama is stubbed by mapping each crop's pixels to its card.
"""
import hashlib
import io
import os
import sqlite3
import sys

import httpx
import pytest
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import database                                  # noqa: E402
import screenshot_import as si                   # noqa: E402
import synthetic_screenshot as ss                # noqa: E402

LABEL = {"Points": "Points", "Rebounds": "Rebounds", "Assists": "Assists", "3PM": "3-Pointers Made"}
LOWER_ONLY = [dict(c, sides="lower", higher=None, lower=1.9) for c in ss.VARIANT_CARDS]
MIXED = [ss.VARIANT_CARDS[0], ss.VARIANT_CARDS[2], ss.VARIANT_CARDS[3], LOWER_ONLY[1]]   # both / no arrows / H / L


def _img(data):
    return Image.open(io.BytesIO(data)).convert("RGB")


def _card_size(boxes):
    return {(x1 - x0, y1 - y0) for x0, y0, x1, y1 in boxes}


@pytest.mark.parametrize("cards", [ss.VARIANT_CARDS, LOWER_ONLY, MIXED], ids=["higher-only", "lower-only", "mixed"])
def test_cards_not_buttons_are_detected(cards):
    boxes = si.detect_cards(_img(ss.render_underdog(cards)))
    assert len(boxes) == 4
    assert all(w > 400 and h > 140 for w, h in _card_size(boxes))      # card-sized, not a 196 x 48 button
    assert boxes == sorted(boxes, key=lambda b: (b[1] // 50, b[0]))    # reading order


def test_no_arrow_card_and_a_cut_first_row_and_an_empty_cell():
    assert len(si.detect_cards(_img(ss.render_underdog([ss.VARIANT_CARDS[2]] * 4)))) == 4      # no arrows
    cut = si.detect_cards(_img(ss.render_underdog(crop_top=30)))
    assert len(cut) == 4 and cut[0][1] == 0                             # top row cut by the edge: still read
    assert len(si.detect_cards(_img(ss.render_underdog(ss.VARIANT_CARDS[:3])))) == 3          # empty 4th cell skipped


# --- stubbed Ollama ---------------------------------------------------------------------

def _digest(img):
    return hashlib.sha1(img.tobytes() + repr(img.size).encode()).hexdigest()


def _buttons(c):
    return ["Higher", "Lower"] if c["sides"] == "both" else [c["sides"].capitalize()]


@pytest.fixture
def fake(monkeypatch):
    """Reads each crop as its card; `fail[i]` makes card i's reads raise or come back empty."""
    state = {"fail": {}, "cards": MIXED}

    def setup(cards):
        state["cards"] = cards
        state["data"] = ss.render_underdog(cards)
        img = _img(state["data"])
        state["crops"] = {}
        for i, (x0, y0, x1, y1) in enumerate(si.detect_cards(img)):
            card = img.crop((max(0, x0 - 4), max(0, y0 - 4), min(img.width, x1 + 4), min(img.height, y1 + 4)))
            for f in si.READ_SCALES:
                state["crops"][_digest(si._upscale(card, f))] = i
    state["setup"] = setup

    def read(im, prompt):
        i = state["crops"][_digest(im)]
        how = state["fail"].get(i)
        if how == "raise":
            raise ValueError("model returned unreadable JSON")
        if how == "empty":
            return {"player_name": None, "stat_label": None, "line": None, "buttons": []}
        if how == "gpu":
            raise si.ExtractorUnavailable(si.GPU_MEMORY_MESSAGE)
        c = state["cards"][i]
        return {"player_name": c["player_name"], "stat_label": LABEL[c["stat"]], "line": f"{c['line']:g}",
                "buttons": _buttons(c)}

    monkeypatch.setenv("SCREENSHOT_EXTRACTOR", "ollama")
    monkeypatch.setattr(si, "_ollama_json", read)
    setup(MIXED)
    return state


def test_each_variant_is_read_with_the_sides_it_offers(fake):
    _, rows = si.extract_rows(fake["data"])
    assert [(r["player_name"], r["stat"], r["line"], r["sides_offered"]) for r in rows] == [
        ("Daniss Jenkins", "3PM", 0.5, "both"), ("Ausar Thompson", "3PM", 0.5, "both"),
        ("Adem Bona", "3PM", 0.5, "higher"), ("Paul Reed", "3PM", 0.5, "lower")]
    assert all(r["alerts"] == [] for r in rows)


@pytest.mark.parametrize("how", ["raise", "empty"])
def test_one_unreadable_card_does_not_drop_the_others(fake, how):
    fake["fail"][1] = how                                     # Ausar Thompson's card
    _, rows = si.extract_rows(fake["data"])
    assert len(rows) == 4
    assert [r["player_name"] for r in rows] == ["Daniss Jenkins", "", "Adem Bona", "Paul Reed"]
    bad = rows[1]
    assert bad["unreadable"] and bad["line"] is None and bad["alerts"] == ["couldn't read this card"]


def test_unreadable_card_is_an_unresolved_row_and_is_not_recorded(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "t.db"))
    fake["fail"][1] = "raise"
    _, rows = si.extract_rows(fake["data"])
    ctx = {"next_opponent_full": "Philadelphia 76ers", "is_home": False, "is_back_to_back": False,
           "next_game_date": "2026-10-25"}
    out = si.confirmation_rows(rows, context_fn=lambda n: ctx)
    bad = out[1]
    assert bad["match"] == "unreadable" and bad["resolved_player"] == "" and bad["alerts"] == ["couldn't read this card"]
    conn = sqlite3.connect(database.DB_PATH)
    got = conn.execute("SELECT screenshot_line, sides_offered FROM line_checks ORDER BY id").fetchall()
    conn.close()
    assert got == [(0.5, "both"), (0.5, "higher"), (0.5, "lower")]      # 3 read cards, none for the unreadable one


def test_all_cards_failing_on_gpu_memory_is_one_message(fake):
    fake["fail"].update({i: "gpu" for i in range(4)})
    with pytest.raises(si.ExtractorUnavailable, match="Not enough GPU memory: close other apps"):
        si.extract_rows(fake["data"])


def test_ollama_out_of_memory_maps_to_the_short_message(monkeypatch):
    def post(url, timeout, json):
        req = httpx.Request("POST", url)
        return httpx.Response(500, request=req, text='{"error":"model requires more system memory (9.1 GiB) than '
                                                     'is available (4.2 GiB)"}')
    monkeypatch.setattr(si.httpx, "post", post)
    monkeypatch.setattr(si.time, "sleep", lambda s: None)
    with pytest.raises(si.ExtractorUnavailable) as e:
        si._ollama_json(Image.new("RGB", (10, 10)), "x")
    assert str(e.value) == si.GPU_MEMORY_MESSAGE


def test_extract_endpoint_returns_the_message(fake, monkeypatch):
    from fastapi.testclient import TestClient
    import main
    monkeypatch.setattr(main, "_require_local", lambda request: None)
    fake["fail"].update({i: "gpu" for i in range(4)})
    r = TestClient(main.app).post("/screenshot/extract", files={"file": ("s.png", fake["data"], "image/png")})
    assert r.status_code == 503 and r.json()["detail"] == "Not enough GPU memory: close other apps"


def test_sides_offered_parsing():
    assert si.sides_offered(["Higher", "Lower"]) == "both"
    assert si.sides_offered(["Higher"]) == "higher"
    assert si.sides_offered("Lower") == "lower"
    assert si.sides_offered([]) is None and si.sides_offered(None) is None
    assert si.normalize_row({"player_name": "A", "stat": "Points", "line": 9.5, "higher_multiplier": 1.2,
                             "lower_multiplier": None})["sides_offered"] == "higher"      # Claude path
