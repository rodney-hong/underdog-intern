"""
screenshot_import.py
Screenshot of pick'em player cards -> rows to confirm in the app -> the normal /predict.

The LLM only transcribes text from the image (player, team, stat, line, multipliers).
It never predicts: predictions come from /predict after the user confirms each row.

Extractor (SCREENSHOT_EXTRACTOR in .env):
  ollama  (default) local Ollama vision model SCREENSHOT_OLLAMA_MODEL (default gemma4:12b)
          at OLLAMA_URL. The screenshot's card grid is detected (detect_cards), each card
          is cropped and upscaled and read on its own, twice (READ_SCALES), asking only
          for the raw text: name, stat label, line. Stat labels are mapped in code
          (normalize_stat). Multipliers are NOT read on this path, so the prices of
          Underdog's 5-point rung cards (lines on x4.5 / x9.5 with non-1.0x multipliers,
          which OddsAPI doesn't carry) are not captured yet: line_checks records only that
          a line is on a rung (is_rung). Two reads that disagree flag the row.
          No cards detected: the whole image is read instead, also twice.
  claude  Claude API, key ANTHROPIC_API_KEY, model SCREENSHOT_CLAUDE_MODEL; whole image,
          one call, multipliers included.
The image is held in memory only: nothing is written to disk.
"""

import base64
import io
import json
import os
import re
import sqlite3
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

EXTRACTORS = ("claude", "ollama")
DEFAULT_EXTRACTOR = "ollama"
CLAUDE_MODEL = os.getenv("SCREENSHOT_CLAUDE_MODEL", "claude-opus-5-5")
OLLAMA_MODEL = os.getenv("SCREENSHOT_OLLAMA_MODEL", "gemma4:12b")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_PARALLEL = int(os.getenv("SCREENSHOT_OLLAMA_PARALLEL", "6"))   # concurrent card reads (6: fastest measured, RTX 5070 Ti)
READ_SCALES = (2, 3)           # each card is read twice, upscaled 2x and 3x
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_SIDE = 2000          # longer side, px; larger screenshots are scaled down first
SUPPORTED_STATS = ("Points", "Rebounds", "Assists", "3PM")
RANGE_GAMES = 82               # "historical range": min..max over the last 82 played regular-season games
RANGE_MIN_GAMES = 10           # fewer games in the window: never flag the line
RANGE_HIGH, RANGE_LOW = 1.5, 0.5   # flag only lines above 1.5x the max or below half the min

PROMPT = (
    "This is a screenshot of player prop cards from a pick'em app. Transcribe every card. "
    "For each card give: player_name exactly as written; team, the player's own team "
    "abbreviation if shown (e.g. MIA in 'MIA vs CHA'), else null; stat as written on the card "
    "(e.g. 'Points', 'Rebounds', 'Assists', '3-Pointers Made'); line, the number next to the "
    "stat; higher_multiplier and lower_multiplier, the number before 'x' on the Higher and "
    "Lower buttons, or null when no number is shown. Copy only what is visible; never infer "
    "or fill in a value. Return JSON only."
)

_NULLABLE_NUMBER = {"anyOf": [{"type": "number"}, {"type": "null"}]}
_NULLABLE_STRING = {"anyOf": [{"type": "string"}, {"type": "null"}]}
SCHEMA = {
    "type": "object",
    "properties": {"rows": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "player_name": {"type": "string"},
            "team": _NULLABLE_STRING,
            "stat": {"type": "string"},
            "line": _NULLABLE_NUMBER,
            "higher_multiplier": _NULLABLE_NUMBER,
            "lower_multiplier": _NULLABLE_NUMBER,
        },
        "required": ["player_name", "team", "stat", "line", "higher_multiplier", "lower_multiplier"],
        "additionalProperties": False,
    }}},
    "required": ["rows"],
    "additionalProperties": False,
}


class ExtractorUnavailable(Exception):
    """The configured extractor can't run (no API key, Ollama down, unknown setting)."""


class BadImage(ValueError):
    """The upload is not a readable PNG / JPEG / WEBP / GIF image, or is too large."""


# ---------------------------------------------------------------------------
# Image handling (memory only)
# ---------------------------------------------------------------------------

def prepare_image(data: bytes) -> tuple[bytes, str]:
    """Validate with Pillow, scale the longer side to MAX_IMAGE_SIDE, re-encode as PNG.
    Returns (png bytes, 'image/png'). Never touches disk."""
    from PIL import Image, UnidentifiedImageError
    if not data:
        raise BadImage("empty upload")
    if len(data) > MAX_IMAGE_BYTES:
        raise BadImage(f"image is over {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
    try:
        img = Image.open(io.BytesIO(data))
        if img.format not in ("PNG", "JPEG", "WEBP", "GIF"):
            raise BadImage(f"unsupported image type {img.format}")
        img.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise BadImage("not a readable image") from exc
    img = img.convert("RGB")
    if max(img.size) > MAX_IMAGE_SIDE:
        img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue(), "image/png"


# ---------------------------------------------------------------------------
# Extractors: image -> raw rows (dicts shaped like SCHEMA's items)
# ---------------------------------------------------------------------------

def _extract_claude(png: bytes, media_type: str) -> list[dict]:
    import anthropic
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise ExtractorUnavailable("ANTHROPIC_API_KEY is not set in backend/.env "
                                   "(or set SCREENSHOT_EXTRACTOR=ollama)")
    client = anthropic.Anthropic()
    try:
        response = client.beta.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=16000,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            # Server-side fallback on a refusal (routes by refusal category).
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                             "data": base64.standard_b64encode(png).decode("utf-8")}},
                {"type": "text", "text": PROMPT},
            ]}],
        )
    except anthropic.AuthenticationError as exc:
        raise ExtractorUnavailable("ANTHROPIC_API_KEY was rejected") from exc
    except anthropic.APIConnectionError as exc:
        raise ExtractorUnavailable("could not reach the Claude API") from exc
    except anthropic.APIStatusError as exc:
        raise ExtractorUnavailable(f"Claude API error {exc.status_code}: {exc.message}") from exc
    if response.stop_reason == "refusal":
        raise ExtractorUnavailable("the Claude API declined to read this image")
    text = next((b.text for b in response.content if b.type == "text"), "")
    return json.loads(text)["rows"]


# --- Ollama: card grid -> one card per call, read twice ---------------------------------

CARD_PROMPT = (
    "This image is one player card from a sports pick'em app. Copy its raw text. Return JSON "
    '{"player_name": ..., "stat_label": ..., "line": ...}: player_name exactly as written; '
    "stat_label, the stat words exactly as written (for example 'Points' or '3-Pointers Made'); "
    "line, the number shown with that stat, exactly as written. Ignore buttons, multipliers, "
    "teams and game times. Use null for anything not visible. Do not guess."
)
SHEET_PROMPT = (
    "This is a screenshot of player cards from a sports pick'em app. For every card copy its "
    'raw text. Return JSON {"cards": [{"player_name": ..., "stat_label": ..., "line": ...}]}: '
    "player_name and stat_label exactly as written, line the number shown with that stat. "
    "Ignore buttons, multipliers, teams and game times. Do not guess."
)


def detect_cards(img) -> list[tuple[int, int, int, int]]:
    """
    Boxes (x0, y0, x1, y1) of the cards in a pick'em screenshot, in reading order, or []
    when no grid of cards is found. A card is a solid filled panel standing out from the
    page background: threshold against the border colour, open away thin separators,
    label connected regions, then keep the ones that are mostly filled, card-sized, and
    of the grid's common size (drops headers, nav bars and stray text).
    """
    import numpy as np
    from scipy import ndimage
    scale = min(1.0, 1200 / max(img.size))
    small = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale)))) if scale < 1 else img
    a = np.asarray(small.convert("RGB")).astype(np.int16)
    h, w = a.shape[:2]
    border = np.concatenate([a[:3].reshape(-1, 3), a[-3:].reshape(-1, 3),
                             a[:, :3].reshape(-1, 3), a[:, -3:].reshape(-1, 3)])
    bg = np.median(border, axis=0)
    mask = np.abs(a - bg).max(axis=2) > 6
    mask = ndimage.binary_opening(mask, structure=np.ones((5, 5)))
    labels, _ = ndimage.label(mask)
    boxes = []
    for i, sl in enumerate(ndimage.find_objects(labels), start=1):
        if sl is None:
            continue
        y0, y1, x0, x1 = sl[0].start, sl[0].stop, sl[1].start, sl[1].stop
        bw, bh = x1 - x0, y1 - y0
        if bw < 0.12 * w or bh < 0.05 * h or (bw > 0.98 * w and bh > 0.9 * h):
            continue
        if bw > 6 * bh or bh > 4 * bw:         # bars and strips, not cards
            continue
        if (labels[y0:y1, x0:x1] == i).mean() < 0.7:
            continue
        boxes.append((x0, y0, x1, y1))
    if not boxes:
        return []
    areas = sorted((b[2] - b[0]) * (b[3] - b[1]) for b in boxes)
    med = areas[len(areas) // 2]
    boxes = [b for b in boxes if 0.5 * med <= (b[2] - b[0]) * (b[3] - b[1]) <= 2 * med]
    row_h = min(b[3] - b[1] for b in boxes)
    boxes.sort(key=lambda b: (round(b[1] / (row_h / 2)), b[0]))
    inv = 1 / scale
    return [(int(x0 * inv), int(y0 * inv), int(x1 * inv), int(y1 * inv)) for x0, y0, x1, y1 in boxes]


def _ollama_json(img, prompt: str) -> dict:
    """One Ollama call on a PIL image: temperature 0, thinking off, JSON mode."""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    body = {"model": OLLAMA_MODEL, "stream": False, "format": "json", "think": False,
            "options": {"temperature": 0},
            "messages": [{"role": "user", "content": prompt,
                          "images": [base64.b64encode(buf.getvalue()).decode()]}]}
    for attempt in (1, 2):   # one retry on a 5xx: a model load can fail once (CUDA init, VRAM)
        try:
            resp = httpx.post(f"{OLLAMA_URL}/api/chat", timeout=300, json=body)
            resp.raise_for_status()
            break
        except httpx.HTTPStatusError as exc:
            if attempt == 2 or exc.response.status_code < 500:
                raise ExtractorUnavailable(f"Ollama ({OLLAMA_URL}, {OLLAMA_MODEL}) failed: {exc}") from exc
            time.sleep(2)
        except httpx.HTTPError as exc:
            raise ExtractorUnavailable(f"Ollama ({OLLAMA_URL}, {OLLAMA_MODEL}) failed: {exc}") from exc
    text = resp.json()["message"]["content"].strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)    # tolerate a fenced reply
    return json.loads(text)


def _read_key(r: dict) -> tuple:
    """What two reads must agree on: name (case/space-insensitive), mapped stat, line."""
    return (re.sub(r"\s+", " ", str(r.get("player_name") or "")).strip().lower(),
            normalize_stat(r.get("stat_label")) or str(r.get("stat_label") or "").strip().lower(),
            _number(r.get("line")))


def _two_reads(first: dict, second: dict) -> dict:
    """One raw row from two reads of the same card: the first read's text (the second's
    if the first has no name or line), with alerts where they disagree on name, stat or line."""
    a, b = _read_key(first), _read_key(second)
    alerts = [f"two reads disagree on {what}: {x!r} vs {y!r}"
              for what, x, y in zip(("name", "stat", "line"), a, b) if x != y]
    base = first if first.get("player_name") and _number(first.get("line")) is not None else second
    return {"player_name": base.get("player_name"), "team": None, "stat": base.get("stat_label"),
            "line": base.get("line"), "higher_multiplier": None, "lower_multiplier": None,
            "reads_agree": not alerts, "alerts": alerts}


def _upscale(img, factor: float):
    from PIL import Image
    return img.resize((int(img.width * factor), int(img.height * factor)), Image.LANCZOS)


def _extract_ollama(png: bytes, media_type: str) -> list[dict]:
    """Cards detected: each card cropped and read at each of READ_SCALES (up to
    OLLAMA_PARALLEL calls at once), one raw row per card. No cards: the whole image,
    read twice (1x and 1.5x)."""
    from concurrent.futures import ThreadPoolExecutor
    from PIL import Image
    img = Image.open(io.BytesIO(png)).convert("RGB")
    boxes = detect_cards(img)
    if not boxes:
        first, second = [_ollama_json(_upscale(img, f) if f > 1 else img, SHEET_PROMPT).get("cards") or []
                         for f in (1, 1.5)]
        rows = [_two_reads(x, second[i] if i < len(second) else {}) for i, x in enumerate(first)]
        if len(second) != len(first):
            for r in rows:
                r["alerts"].append(f"two reads found {len(first)} and {len(second)} cards")
                r["reads_agree"] = False
        return rows
    jobs = []
    for x0, y0, x1, y1 in boxes:
        card = img.crop((max(0, x0 - 4), max(0, y0 - 4), min(img.width, x1 + 4), min(img.height, y1 + 4)))
        jobs += [_upscale(card, f) for f in READ_SCALES]
    with ThreadPoolExecutor(max_workers=max(1, OLLAMA_PARALLEL)) as ex:
        results = list(ex.map(lambda im: _ollama_json(im, CARD_PROMPT), jobs))
    n = len(READ_SCALES)
    return [_two_reads(results[i], results[i + 1]) for i in range(0, len(results), n)]


_EXTRACT = {"claude": _extract_claude, "ollama": _extract_ollama}


def extractor_name() -> str:
    name = os.getenv("SCREENSHOT_EXTRACTOR", DEFAULT_EXTRACTOR).strip().lower()
    if name not in EXTRACTORS:
        raise ExtractorUnavailable(f"SCREENSHOT_EXTRACTOR={name!r}; use one of {EXTRACTORS}")
    return name


def extract_rows(image: bytes) -> tuple[str, list[dict]]:
    """(extractor used, normalized rows) for an uploaded image."""
    name = extractor_name()
    png, media_type = prepare_image(image)
    raw = _EXTRACT[name](png, media_type)
    return name, [r for r in (normalize_row(x) for x in raw if isinstance(x, dict)) if r]


# ---------------------------------------------------------------------------
# Normalizing what the model wrote
# ---------------------------------------------------------------------------

_STAT_ALIASES = {
    "points": "Points", "pts": "Points",
    "rebounds": "Rebounds", "reb": "Rebounds", "rebs": "Rebounds", "total rebounds": "Rebounds",
    "assists": "Assists", "ast": "Assists", "asts": "Assists",
    "3pm": "3PM", "3-pointers made": "3PM", "3 pointers made": "3PM", "3-pt made": "3PM",
    "3pt made": "3PM", "threes": "3PM", "three pointers made": "3PM", "3-pointers": "3PM",
}


def normalize_stat(s) -> str | None:
    key = re.sub(r"\s+", " ", str(s or "").strip().lower())
    return _STAT_ALIASES.get(key)


def _number(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(?:\.\d+)?", str(v))
    return float(m.group()) if m else None


def normalize_row(r: dict) -> dict | None:
    """One extracted card in a fixed shape; None when it has no name or no line."""
    name = re.sub(r"\s+", " ", str(r.get("player_name") or "")).strip()
    line = _number(r.get("line"))
    if not name or line is None:
        return None
    team = str(r.get("team") or "").strip().upper() or None
    return {"player_name": name, "team": team, "stat_text": str(r.get("stat") or "").strip(),
            "stat": normalize_stat(r.get("stat")), "line": line,
            "higher_multiplier": _number(r.get("higher_multiplier")),
            "lower_multiplier": _number(r.get("lower_multiplier")),
            "reads_agree": r.get("reads_agree"), "alerts": list(r.get("alerts") or [])}


# ---------------------------------------------------------------------------
# Resolving rows for the confirmation table (no prediction here)
# ---------------------------------------------------------------------------

# NBA-style abbreviations (what pick'em cards print) by nba_team_id, for the six teams whose
# ESPN roster abbreviation differs (GS, NO, NY, SA, UTAH, WSH), plus common alternates.
NBA_ABBREVS = {1610612744: {"GSW"}, 1610612740: {"NOP"}, 1610612752: {"NYK"}, 1610612759: {"SAS"},
               1610612762: {"UTA"}, 1610612764: {"WAS"}, 1610612751: {"BRK"}, 1610612756: {"PHO"},
               1610612766: {"CHO"}}


def _team_names(h: dict) -> set:
    """Every way a card can name this roster player's team."""
    return ({str(h["team_abbrev"] or "").upper(), str(h["team_name"] or "").upper(),
             f"{h['team_city']} {h['team_name']}".upper()} | NBA_ABBREVS.get(h.get("nba_team_id"), set())) - {""}


def resolve_player(conn, name: str, team: str | None) -> dict:
    """
    Exact name_key against the ESPN rosters, then the card's team (ESPN or NBA abbreviation,
    nickname or full name) if it shows one. Exactly one roster player left -> {"status": "ok",
    "player_name", "team_abbrev", "team_name"}; none -> not_found, or team_mismatch (the name
    is on a roster, but not on the card's team; "roster_teams" lists where); several ->
    ambiguous (left blank).
    """
    import rosters
    key = rosters.name_key(name)
    hits = []
    for cols in ("player_name, team_abbrev, team_city, team_name, nba_team_id",
                 "player_name, team_abbrev, team_city, team_name"):          # older tables: no nba_team_id
        try:
            hits = [dict(r) for r in conn.execute(f"SELECT {cols} FROM rosters WHERE name_key = ?", (key,))]
            break
        except Exception:
            continue
    named = hits
    if team:
        t = team.upper()
        hits = [h for h in hits if t in _team_names(h)]
    if len(hits) == 1:
        h = hits[0]
        return {"status": "ok", "player_name": h["player_name"], "team_abbrev": h["team_abbrev"],
                "team_name": f"{h['team_city']} {h['team_name']}"}
    if not hits and named:
        return {"status": "team_mismatch", "roster_teams": sorted(f"{h['team_city']} {h['team_name']}" for h in named)}
    return {"status": "ambiguous" if hits else "not_found"}


def historical_range(conn, person_id: int | None, stat: str) -> dict | None:
    """
    min / max of the stat over the player's last RANGE_GAMES played regular-season games
    ("Regular Season" and the 2023 "NBA Emirates Cup" label; playoffs, play-in and DNP rows
    excluded), across seasons, or None if he has none. person_id comes from
    database.resolve_person_id, the same resolver /predict uses.
    """
    import database
    cols = database._RESOLVE_STAT_COLS.get(stat)
    if person_id is None or not cols or stat not in SUPPORTED_STATS:
        return None
    rs = ", ".join(f"'{t}'" for t in database.REGULAR_SEASON_GAME_TYPES)
    played = ("(COALESCE(numMinutes, 0) > 0 OR "
              + " OR ".join(f"COALESCE({c}, 0) > 0" for c in database.DNP_COUNTING_COLS) + ")")
    rows = None
    for cond in (f"AND {played}", ""):            # tables without numMinutes / counting columns: no DNP filter
        try:
            rows = conn.execute(
                f"SELECT {cols[0]} FROM player_stats WHERE personId = ? AND gameType IN ({rs}) "
                f"AND {cols[0]} IS NOT NULL {cond} ORDER BY gameDateTimeEst DESC LIMIT ?",
                (int(person_id), RANGE_GAMES)).fetchall()
            break
        except sqlite3.OperationalError:
            continue
    if not rows:
        return None
    vals = [float(r[0]) for r in rows]
    return {"min": min(vals), "max": max(vals), "games": len(vals)}


def implausible_line(line: float, rng: dict | None) -> bool:
    """Only with RANGE_MIN_GAMES+ games in the window, and only far outside it: above
    RANGE_HIGH x the max or below RANGE_LOW x the min."""
    return (rng is not None and rng["games"] >= RANGE_MIN_GAMES
            and (line > RANGE_HIGH * rng["max"] or line < RANGE_LOW * rng["min"]))


def logged_underdog_line(conn, player_name: str, team_name: str, stat: str,
                         game_date: str) -> dict | None:
    """
    The latest Underdog line logged in line_snapshots on game_date (ET) for this player
    (exact name_key, in a game of his team) and stat, from either of the stat's markets:
    {"line", "captured_at", "pull_phase", "market_key", "over_price", "under_price"}
    (the main market's row when both markets carry it). None when nothing is logged, or
    when the latest pull holds more than one Underdog point for him (as
    line_logger.todays_underdog_lines). This is OddsAPI's Underdog line, the standard
    pick at the book line; Underdog's 5-point rung cards are not in that feed.
    """
    import line_logger
    import rosters
    markets = line_logger.STAT_MARKETS.get(stat)
    if not markets:
        return None
    nick = "Trail Blazers" if "Blazers" in team_name else team_name.split()[-1]
    try:
        rows = conn.execute(
            f"SELECT player_name, point, captured_at, pull_phase, market_key, side, price FROM line_snapshots "
            f"WHERE game_date = ? AND bookmaker = 'underdog' AND market_key IN ({', '.join('?' * len(markets))}) "
            f"AND (home_team LIKE ? OR away_team LIKE ?)",
            (game_date, *markets, f"%{nick}", f"%{nick}")).fetchall()
    except Exception:
        return None
    key = rosters.name_key(player_name)
    rows = [r for r in rows if rosters.name_key(r["player_name"]) == key and r["point"] is not None]
    if not rows:
        return None
    latest = max(r["captured_at"] for r in rows)
    points = {float(r["point"]) for r in rows if r["captured_at"] == latest}
    if len(points) != 1:
        return None
    pull = [r for r in rows if r["captured_at"] == latest]
    market = markets[0] if any(r["market_key"] == markets[0] for r in pull) else pull[0]["market_key"]
    price = {r["side"]: r["price"] for r in pull if r["market_key"] == market}
    return {"line": points.pop(), "captured_at": latest, "pull_phase": pull[0]["pull_phase"],
            "market_key": market, "over_price": price.get("Over"), "under_price": price.get("Under")}


def is_rung(line: float) -> bool:
    """On one of Underdog's 5-point rungs: x4.5 or x9.5 (4.5, 9.5, 14.5, 19.5, 24.5, ...)."""
    return abs((line - 4.5) / 5 - round((line - 4.5) / 5)) < 1e-9


_LINE_CHECKS_DDL = """
    CREATE TABLE IF NOT EXISTS line_checks (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        checked_at         TEXT NOT NULL,   -- UTC ISO, when the screenshot was read
        person_id          INTEGER,         -- NULL: card not resolved to one player
        stat_type          TEXT,            -- Points / Rebounds / Assists / 3PM, NULL if unmapped
        screenshot_line    REAL NOT NULL,
        logged_line        REAL,            -- latest Underdog line in today's line_snapshots
        logged_captured_at TEXT,            -- its capture time (UTC ISO)
        logged_pull_phase  TEXT,            -- open / pretip / catchup / event / ...
        matches            INTEGER,         -- 1 / 0; NULL when nothing was logged to compare
        reads_agree        INTEGER,         -- 1 / 0 for the two reads; NULL (one read: Claude)
        is_rung            INTEGER,         -- 1: screenshot line on a 5-point rung (x4.5 / x9.5)
        logged_market_key  TEXT,            -- market of the logged line (main or _alternate)
        logged_over_price  REAL,            -- its Over / Under prices (American)
        logged_under_price REAL,
        line_diff          REAL             -- screenshot_line - logged_line
    )"""
_LINE_CHECKS_ADDED = (("is_rung", "INTEGER"), ("logged_market_key", "TEXT"), ("logged_over_price", "REAL"),
                      ("logged_under_price", "REAL"), ("line_diff", "REAL"))


def record_line_checks(conn, rows: list[dict], checked_at: str) -> int:
    """One line_checks row per extracted card (numbers only, no image). Silent: the
    comparison with the logged line is recorded here, never flagged in the table."""
    conn.execute(_LINE_CHECKS_DDL)
    have = {c[1] for c in conn.execute("PRAGMA table_info(line_checks)")}
    for col, kind in _LINE_CHECKS_ADDED:
        if col not in have:
            conn.execute(f"ALTER TABLE line_checks ADD COLUMN {col} {kind}")
    data = []
    for r in rows:
        logged = r.get("logged_line") or {}
        diff = None if not logged else round(r["line"] - logged["line"], 4)
        match = None if diff is None else int(abs(diff) < 1e-9)
        agree = r.get("reads_agree")
        data.append((checked_at, r.get("person_id"), r.get("stat"), r["line"],
                     logged.get("line"), logged.get("captured_at"), logged.get("pull_phase"), match,
                     None if agree is None else int(agree), int(is_rung(r["line"])),
                     logged.get("market_key"), logged.get("over_price"), logged.get("under_price"), diff))
    conn.executemany("INSERT INTO line_checks (checked_at, person_id, stat_type, screenshot_line, "
                     "logged_line, logged_captured_at, logged_pull_phase, matches, reads_agree, is_rung, "
                     "logged_market_key, logged_over_price, logged_under_price, line_diff) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", data)
    conn.commit()
    return len(data)


def confirmation_rows(rows: list[dict], context_fn=None, record: bool = True) -> list[dict]:
    """
    Each extracted row plus what the confirmation table needs: the resolved player
    (blank unless exactly one exact name_key + team match), his next game context for
    /predict (opponent, home, back-to-back, game date), the historical range and flags,
    and today's logged Underdog line for him and that stat. The screenshot line is the one
    predicted. Alerts (the row's flag icon) only for uncertain rows: the two reads disagree,
    or the player can't be resolved. A difference from the logged line is not an alert
    (a 5-point rung card differs by design); it is recorded silently in line_checks.
    With `record`, every card is written to line_checks.
    """
    import datetime as dt
    from zoneinfo import ZoneInfo
    import database
    if context_fn is None:
        import data_fetcher
        context_fn = data_fetcher.get_player_context
    checked_at = dt.datetime.now(dt.timezone.utc)
    today = checked_at.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    conn = database.get_connection()
    try:
        out = []
        for r in rows:
            who = resolve_player(conn, r["player_name"], r["team"])
            row = {**r, "resolved_player": who.get("player_name", ""), "match": who["status"],
                   "opponent_team": "", "is_home": False, "is_back_to_back": False,
                   "game_date": None, "range": None, "flags": [], "alerts": list(r.get("alerts") or []),
                   "logged_line": None, "person_id": None}
            if r["stat"] is None:
                row["flags"].append(f"stat '{r['stat_text']}' is not one of {', '.join(SUPPORTED_STATS)}")
            if who["status"] == "ok":
                ctx = context_fn(who["player_name"]) or {}
                row.update(opponent_team=ctx.get("next_opponent_full") or "",
                           is_home=bool(ctx.get("is_home")), is_back_to_back=bool(ctx.get("is_back_to_back")),
                           game_date=ctx.get("next_game_date"))
                # the same personId /predict resolves from this row's name, game date and opponent
                row["person_id"] = database.resolve_person_id(conn, who["player_name"], row["game_date"],
                                                              row["opponent_team"])
                if r["stat"]:
                    rng = historical_range(conn, row["person_id"], r["stat"])
                    row["range"] = rng
                    if rng is None:
                        row["flags"].append("no game history for this stat")
                    elif implausible_line(r["line"], rng):
                        row["flags"].append(f"line {r['line']:g} is far outside the range "
                                            f"{rng['min']:g}–{rng['max']:g} (last {rng['games']} games)")
                if r["stat"]:
                    row["logged_line"] = logged_underdog_line(conn, who["player_name"], who["team_name"],
                                                              r["stat"], today)
            else:
                why = {"ambiguous": "several roster players match",
                       "team_mismatch": f"the card's team {r['team']} is not his roster team "
                                        f"({', '.join(who.get('roster_teams', []))})"}
                row["alerts"].append("player not resolved: " + why.get(who["status"], "no exact roster match"))
            out.append(row)
        if record and out:
            record_line_checks(conn, out, checked_at.isoformat())
        return out
    finally:
        conn.close()
