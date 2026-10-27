"""
screenshot_import.py
Screenshot of pick'em player cards -> rows to confirm in the app -> the normal /predict.

The LLM only transcribes text from the image (player, team, stat, line, multipliers).
It never predicts: predictions come from /predict after the user confirms each row.

Extractor (SCREENSHOT_EXTRACTOR in .env):
  ollama  (default; development on the PC) local Ollama vision model SCREENSHOT_OLLAMA_MODEL
          (default gemma4:12b) at OLLAMA_URL. The screenshot's card grid is detected (detect_cards), each card
          is cropped and upscaled and read on its own, twice (READ_SCALES), asking only
          for the raw text: name, stat label, line. Stat labels are mapped in code
          (normalize_stat). Multipliers are NOT read on this path, so the prices of
          Underdog's 5-point rung cards (lines on x4.5 / x9.5 with non-1.0x multipliers,
          which OddsAPI doesn't carry) are not captured yet: line_checks records only that
          a line is on a rung (is_rung). Two reads that disagree flag the row.
          No cards detected: the whole image is read instead, also twice.
  ollama_cloud  (the server) the same card reads through Ollama Cloud's API (OLLAMA_CLOUD_URL,
          default https://ollama.com; key OLLAMA_API_KEY; model SCREENSHOT_OLLAMA_CLOUD_MODEL,
          default gemma4:31b), SCREENSHOT_OLLAMA_CLOUD_PARALLEL reads at once (1: the Free
          plan's concurrency). When Ollama Cloud fails, above all on a usage-limit / quota
          error, the image is read again with gemini and the switch is logged; after a quota
          error gemini is used directly for QUOTA_COOLDOWN_MINUTES.
  gemini  the same card reads through the Gemini API (generateContent, key GEMINI_API_KEY, model
          SCREENSHOT_GEMINI_MODEL, default gemma-4-31b-it), image sent inline (no file upload),
          thinking off (SCREENSHOT_GEMINI_THINKING); only the reply's answer part is parsed,
          never a part marked "thought".
  Hosted services (ollama_cloud, gemini) fail fast: every request has a timeout, HTTP 500 / 503
  is retried once, and a service with no answer within HOSTED_FIRST_READ_SECONDS, or that times
  out, is unreachable, refuses the key or fails the retry, fails the whole image at once
  (ServiceDown), so both failing reaches the app as one message within about a minute.
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

EXTRACTORS = ("claude", "ollama", "ollama_cloud", "gemini")
DEFAULT_EXTRACTOR = "ollama"
CLAUDE_MODEL = os.getenv("SCREENSHOT_CLAUDE_MODEL", "claude-opus-5-5")
OLLAMA_MODEL = os.getenv("SCREENSHOT_OLLAMA_MODEL", "gemma4:12b")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_PARALLEL = int(os.getenv("SCREENSHOT_OLLAMA_PARALLEL", "6"))   # concurrent card reads (6: fastest measured, RTX 5070 Ti)
OLLAMA_CLOUD_URL = os.getenv("OLLAMA_CLOUD_URL", "https://ollama.com")
OLLAMA_CLOUD_MODEL = os.getenv("SCREENSHOT_OLLAMA_CLOUD_MODEL", "gemma4:31b")
OLLAMA_CLOUD_PARALLEL = int(os.getenv("SCREENSHOT_OLLAMA_CLOUD_PARALLEL", "1"))   # Free plan: 1 concurrent request
GEMINI_URL = os.getenv("GEMINI_URL", "https://generativelanguage.googleapis.com")
GEMINI_MODEL = os.getenv("SCREENSHOT_GEMINI_MODEL", "gemma-4-31b-it")
GEMINI_PARALLEL = int(os.getenv("SCREENSHOT_GEMINI_PARALLEL", "2"))
# Gemma 4 on the Gemini API only switches thinking on or off: thinkingLevel "minimal" = off,
# "high" = on (ai.google.dev/gemma/docs/core/gemma_on_gemini_api). Empty: send no thinkingConfig.
GEMINI_THINKING = os.getenv("SCREENSHOT_GEMINI_THINKING", "minimal").strip()
HOSTED_TIMEOUT_SECONDS = float(os.getenv("SCREENSHOT_HOSTED_TIMEOUT_SECONDS", "20"))      # one hosted request
HOSTED_FIRST_READ_SECONDS = float(os.getenv("SCREENSHOT_HOSTED_FIRST_READ_SECONDS", "25"))  # until its first answer
HOSTED_RETRY_STATUSES = (500, 503)      # retried once; anything else fails at once
HOSTED_RETRY_PAUSE = 1.0
FALLBACK = {"ollama_cloud": "gemini"}   # extractor -> the one used when it fails
QUOTA_COOLDOWN_MINUTES = 60             # after a usage-limit error, skip that service this long
READ_SCALES = (2, 3)           # each card is read twice, upscaled 2x and 3x
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_SIDE = 2000          # longer side, px; larger screenshots are scaled down first
SUPPORTED_STATS = ("Points", "Rebounds", "Assists", "3PM")
CARD_MIN_HEIGHT = 0.1          # a card is at least this fraction of the image width tall
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


class QuotaExceeded(ExtractorUnavailable):
    """A hosted service refused because a usage limit, quota or rate limit was reached."""


class ServiceDown(ExtractorUnavailable):
    """A hosted service can't read anything right now (timed out, unreachable, refused the key,
    HTTP 5xx after the retry): the whole image fails at once instead of card by card."""


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
    '{"player_name": ..., "stat_label": ..., "line": ..., "buttons": [...]}: player_name exactly '
    "as written; stat_label, the stat words exactly as written (for example 'Points' or "
    "'3-Pointers Made'); line, the number shown with that stat, exactly as written (some cards "
    "show small up/down arrows beside it, some don't); buttons, the labels of the pick buttons "
    "shown, from 'Higher' and 'Lower' (a card may show both or only one). Ignore multipliers, "
    "teams and game times. Use null for anything not visible. Do not guess."
)
SHEET_PROMPT = (
    "This is a screenshot of player cards from a sports pick'em app. For every card copy its "
    'raw text. Return JSON {"cards": [{"player_name": ..., "stat_label": ..., "line": ..., '
    '"buttons": [...]}]}: player_name and stat_label exactly as written, line the number shown '
    "with that stat (with or without arrows beside it), buttons the pick-button labels shown "
    "('Higher', 'Lower', or just one of them). Ignore multipliers, teams and game times. Do not guess."
)
UNREADABLE = "couldn't read this card"


def detect_cards(img) -> list[tuple[int, int, int, int]]:
    """
    Boxes (x0, y0, x1, y1) of the cards in a pick'em screenshot, in reading order, or []
    when no grid of cards is found.
      cards     a card is a panel set off from the page by its fill or its outline: pixels
                that differ from the border colour, holes filled (so an outlined card whose
                fill matches the page, as in Underdog's dark theme, becomes one solid region
                and its buttons merge into it), thin separators opened away; kept when mostly
                filled, at least CARD_MIN_HEIGHT x the image width tall (a lone Higher button
                is not a card), not a bar, and of the grid's common size
      grid      the cards found give the card size, columns and row pitch; the grid is then
                tiled over the whole image, so cards whose outline is cut by the image edge or
                merges with a neighbour are still cropped; empty grid cells are skipped
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
    solid = ndimage.binary_opening(ndimage.binary_fill_holes(mask), structure=np.ones((9, 9)))
    labels, _ = ndimage.label(solid)
    boxes = []
    for i, sl in enumerate(ndimage.find_objects(labels), start=1):
        if sl is None:
            continue
        y0, y1, x0, x1 = sl[0].start, sl[0].stop, sl[1].start, sl[1].stop
        bw, bh = x1 - x0, y1 - y0
        if bw < 0.12 * w or bh < CARD_MIN_HEIGHT * w or (bw > 0.98 * w and bh > 0.9 * h):
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
    tiles = _tile_grid(boxes, mask, w, h)
    inv = 1 / scale
    return [(int(x0 * inv), int(y0 * inv), int(x1 * inv), int(y1 * inv)) for x0, y0, x1, y1 in tiles]


def _tile_grid(boxes, mask, w, h) -> list[tuple[int, int, int, int]]:
    """The grid the detected cards sit on, tiled over the image (see detect_cards)."""
    import numpy as np
    cw = int(np.median([b[2] - b[0] for b in boxes]))
    ch = int(np.median([b[3] - b[1] for b in boxes]))

    def axis(starts, size, limit):
        starts = sorted(starts)
        merged = [starts[0]]
        for v in starts[1:]:
            if v - merged[-1] > size // 2:
                merged.append(v)
        gaps = [b - a for a, b in zip(merged, merged[1:])]
        pitch = min(gaps) if gaps else size + max(4, size // 18)
        v = merged[0]
        while v - pitch > -0.4 * size:                 # cells before the first card found
            v -= pitch
        out = []
        while v + 0.6 * size < limit:                  # and after the last
            out.append(v)
            v += pitch
        return out

    tiles = []
    for y in axis([b[1] for b in boxes], ch, h):
        for x in axis([b[0] for b in boxes], cw, w):
            t = (max(0, x), max(0, y), min(w, x + cw), min(h, y + ch))
            cell = mask[t[1]:t[3], t[0]:t[2]]
            if cell.size and cell.mean() > 0.03:       # an empty grid cell has nothing on it
                tiles.append(t)
    return tiles


# --- Underdog's iPhone app: a game page's Players tab -----------------------------------

PHONE_MIN_ASPECT = 1.8         # height / width at least this (a desktop crop can be too: see detect_phone)
# Where the text sits in a whole tile / list row, as fractions of its height (iPhone
# screenshots, 2026-10-27). A cut one is read only while its name, line and stat are whole.
TILE_TOP_CUT_NAME = 0.035      # cut more off a tile's top and its name banner is clipped: unreadable
TILE_TOP_CUT_LINE = 0.5        # ... and its line: skipped
TILE_BOTTOM_KEEP = 0.76        # less of a tile left above the image's bottom and its stat is clipped: skipped
ROW_TOP_CUT_LINE = 0.1         # a list row's line is on its buttons' upper half: cut more, skipped
ROW_BOTTOM_KEEP = 0.6
PARTLY_CUT = "partly cut off: check"
HEADER_PROMPT = (
    "This is the top of a game page in a sports pick'em app. Copy the two team abbreviations "
    "of the game header, the one on the left and the one on the right, exactly as written "
    '(for example \'BKN\' and \'BOS\'). Return JSON {"left_team": ..., "right_team": ...}. '
    "Use null for anything not visible. Do not guess."
)
TAB_PROMPT = ('This image is one tab button from an app. Copy its label exactly as written. Return JSON '
              '{"label": ...}. Use null if it is not readable. Do not guess.')
ROW_PROMPT = (
    "This image is one row of a player list from a sports pick'em app: a player's name, his "
    "average, and one or two pick buttons, each with an arrow, a line and a multiplier. Copy its "
    'raw text. Return JSON {"player_name": ..., "line": ..., "arrows": [...]}: player_name '
    "exactly as written; line, the number on the pick buttons exactly as written (not the "
    "average, not a multiplier); arrows, the direction of each button's arrow, 'up' or 'down'. "
    "Ignore multipliers. Use null for anything not visible. Do not guess."
)


def detect_phone(img) -> dict | None:
    """
    Underdog's iPhone game page (Players tab), or None for anything else (the desktop path).
      phone     tall (PHONE_MIN_ASPECT) with a row of at least three tab pills in its top
                half; a desktop crop, however tall, has no such row
      header    the box above the pills (the two teams), None when there is next to nothing
      tab       the pill much brighter than the others (the selected one), or None
      layout    'tiles' (two-column tiles, Popular), 'list' (rows of one or two line buttons
                on the right, a stat tab) or None when neither is found
      items     reading order; each {"box", "action"}: 'read'; 'flag' (cut by the image edge
                but name, line and stat whole: read and flagged); 'unreadable' (a tile's name
                clipped); 'skip' (its line or stat clipped). See the fractions above.
    Boxes are in the image's pixels.
    """
    import numpy as np
    from scipy import ndimage
    if img.height < PHONE_MIN_ASPECT * img.width:
        return None
    scale = min(1.0, 1200 / max(img.size))
    small = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale)))) if scale < 1 else img
    a = np.asarray(small.convert("RGB")).astype(np.int16)
    h, w = a.shape[:2]
    border = np.concatenate([a[:3].reshape(-1, 3), a[-3:].reshape(-1, 3),
                             a[:, :3].reshape(-1, 3), a[:, -3:].reshape(-1, 3)])
    mask = np.abs(a - np.median(border, axis=0)).max(axis=2) > 6
    solid = ndimage.binary_opening(ndimage.binary_fill_holes(mask), structure=np.ones((9, 9)))
    labels, _ = ndimage.label(solid)
    lum = a.mean(axis=2)
    comps = []                     # solid panels: (x0, y0, x1, y1, mean brightness)
    for i, sl in enumerate(ndimage.find_objects(labels), start=1):
        if sl is None:
            continue
        own = labels[sl] == i
        if own.mean() >= 0.8:
            comps.append((sl[1].start, sl[0].start, sl[1].stop, sl[0].stop, float(lum[sl][own].mean())))
    pills = [c for c in comps if 0.06 * w <= c[3] - c[1] <= 0.13 * w and 0.08 * w <= c[2] - c[0] <= 0.4 * w
             and c[3] < h / 2]
    tabs = None
    for p in sorted(pills, key=lambda c: c[1]):
        row = [q for q in pills if abs(q[1] - p[1]) <= 0.015 * w]
        if len(row) >= 3:
            tabs = row
            break
    if tabs is None:
        return None
    inv = 1 / scale

    def full(b):
        return tuple(int(v * inv) for v in b[:4])

    bright = sorted(tabs, key=lambda c: c[4], reverse=True)
    top, content = min(c[1] for c in tabs), max(c[3] for c in tabs)
    below = [c for c in comps if c[1] >= content and c[3] - c[1] >= 0.03 * w]
    tiles = [c for c in below if 0.35 * w <= c[2] - c[0] <= 0.5 * w]
    buttons = sorted((c for c in below if 0.12 * w <= c[2] - c[0] <= 0.35 * w and c[3] - c[1] <= 0.16 * w
                      and c[0] >= 0.4 * w), key=lambda c: c[1])
    rows = []                      # buttons side by side: one list row, the full width
    for b in buttons:
        if rows and abs(b[1] - rows[-1][1]) <= 0.03 * w:
            rows[-1] = (0, min(rows[-1][1], b[1]), w, max(rows[-1][3], b[3]))
        else:
            rows.append((0, b[1], w, b[3]))
    layout = "tiles" if tiles and len(tiles) >= len(rows) else "list" if rows else None
    boxes = rows
    if layout == "tiles":          # reading order: tiles whose tops are level form a row
        lines = []
        for c in sorted(tiles, key=lambda c: c[1]):
            if lines and c[1] - lines[-1][0][1] <= 0.05 * w:
                lines[-1].append(c)
            else:
                lines.append([c])
        boxes = [c for line in lines for c in sorted(line, key=lambda c: c[0])]
    whole = max((b[3] - b[1] for b in boxes), default=0)
    items = []
    for b in boxes:
        seen = (b[3] - b[1]) / whole
        at_bottom = b[3] >= h - 0.01 * w
        if whole - (b[3] - b[1]) <= 2:                 # px at this scale: a whole one
            action = "read"
        elif layout == "tiles" and at_bottom:
            action = "flag" if seen >= TILE_BOTTOM_KEEP else "skip"
        elif layout == "tiles":
            action = ("flag" if 1 - seen <= TILE_TOP_CUT_NAME else
                      "unreadable" if 1 - seen <= TILE_TOP_CUT_LINE else "skip")
        elif at_bottom:
            action = "flag" if seen >= ROW_BOTTOM_KEEP else "skip"
        else:
            action = "flag" if 1 - seen <= ROW_TOP_CUT_LINE else "skip"
        items.append({"box": full(b), "action": action})
    return {"header": full((0, 0, w, top)) if top >= 0.3 * w else None,
            "tab": full(bright[0]) if bright[0][4] - bright[1][4] >= 60 else None,
            "layout": layout, "items": items}


def _agreed(reads, keys) -> tuple | None:
    """The values of `keys` when every read has them all (non-empty) and they agree, else None."""
    out = []
    for k in keys:
        vals = [re.sub(r"\s+", " ", str(r.get(k) or "")).strip() if isinstance(r, dict) else "" for r in reads]
        if not vals or not vals[0] or any(v.lower() != vals[0].lower() for v in vals):
            return None
        out.append(vals[0])
    return tuple(out)


def _arrow_buttons(arrows) -> list[str]:
    """A list row's arrows ('up' / 'down' or the glyphs) as the button words sides_offered reads."""
    out = []
    for x in (arrows if isinstance(arrows, list) else [arrows]):
        s = str(x or "").lower()
        out += ["Higher"] if "up" in s or "↑" in s else ["Lower"] if "down" in s or "↓" in s else []
    return out


def _extract_phone(img, phone: dict, read_json, parallel: int, budget: _Budget | None = None) -> list[dict]:
    """
    One raw row per tile / list row that is not skipped (detect_phone), in reading order:
    each read twice as on desktop (tiles with CARD_PROMPT, list rows with ROW_PROMPT and the
    selected tab's label as the stat; no tab read, or two reads of it that differ: no stat,
    never a guess). The header is read twice (1x, 1.5x); its two team abbreviations, when both
    reads agree, go on every row as game_teams (else None: confirmation_rows then leaves every
    row unresolved). Flagged items get PARTLY_CUT; clipped names, unreadable_row().
    """
    if not phone["items"]:
        return []
    jobs = []
    if phone["header"]:
        header = img.crop(phone["header"])
        jobs += [(_upscale(header, f) if f > 1 else header, HEADER_PROMPT) for f in (1, 1.5)]
    n_header = len(jobs)
    if phone["layout"] == "list" and phone["tab"]:
        tab = img.crop(phone["tab"])
        jobs += [(_upscale(tab, f), TAB_PROMPT) for f in READ_SCALES]
    n_pre = len(jobs)
    prompt = ROW_PROMPT if phone["layout"] == "list" else CARD_PROMPT
    for it in phone["items"]:
        if it["action"] in ("read", "flag"):
            x0, y0, x1, y1 = it["box"]
            crop = img.crop((max(0, x0 - 4), max(0, y0 - 4), min(img.width, x1 + 4), min(img.height, y1 + 4)))
            jobs += [(_upscale(crop, f), prompt) for f in READ_SCALES]
    results = _read_all(jobs, read_json, parallel, budget)
    teams = _agreed(results[:n_header], ("left_team", "right_team")) if n_header else None
    label = _agreed(results[n_header:n_pre], ("label",)) if n_pre > n_header else None
    n, k, rows = len(READ_SCALES), n_pre, []
    for it in phone["items"]:
        if it["action"] == "skip":
            continue
        if it["action"] == "unreadable":
            rows.append(unreadable_row())
            continue
        reads = [r if isinstance(r, dict) else {} for r in results[k:k + n]]
        k += n
        if phone["layout"] == "list":
            reads = [{**r, "stat_label": label[0] if label else None, "buttons": _arrow_buttons(r.get("arrows"))}
                     for r in reads]
        if not any(_readable(r) for r in reads):
            rows.append(unreadable_row())
            continue
        row = _two_reads(reads[0], reads[1] if len(reads) > 1 else {})
        if it["action"] == "flag":
            row["alerts"].append(PARTLY_CUT)
        row["game_teams"] = [t.upper() for t in teams] if teams else None
        rows.append(row)
    return rows


GPU_MEMORY_MESSAGE = "Not enough GPU memory: close other apps"


def _out_of_memory(resp) -> bool:
    """Ollama's model-load failures when VRAM / memory runs out."""
    try:
        text = resp.text.lower()
    except Exception:
        return False
    return any(k in text for k in ("out of memory", "requires more system memory", "cudamalloc",
                                   "unable to allocate", "insufficient memory", "not enough memory"))


_QUOTA_WORDS = ("usage limit", "quota", "limit reached", "limit exceeded", "rate limit", "too many requests",
                "resource_exhausted", "resource exhausted", "out of credits", "insufficient credits",
                "subscription", "upgrade")


def _is_quota(resp) -> bool:
    """A usage-limit / quota / rate-limit refusal: 429, 402, or a 4xx that says so."""
    if resp.status_code in (402, 429):
        return True
    try:
        text = resp.text.lower()
    except Exception:
        return False
    return 400 <= resp.status_code < 500 and any(w in text for w in _QUOTA_WORDS)


def _png_b64(img) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _json_text(text: str) -> dict:
    """The JSON object in a model's reply: plain, fenced, or with words around it."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())    # tolerate a fenced reply
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        i, j = text.find("{"), text.rfind("}")
        if i < 0 or j <= i:
            raise
        return json.loads(text[i:j + 1])


class _Budget:
    """One hosted service's time limits while it reads one image: each request at most
    HOSTED_TIMEOUT_SECONDS; until the service's first answer, every request and the retry
    together at most HOSTED_FIRST_READ_SECONDS. A down service fails the image inside that
    window; a working one then reads the remaining cards at its own pace. `stopped` is set
    when the image has failed, so reads still queued or in flight send nothing more."""

    def __init__(self, label: str):
        self.label = label
        self.until = time.monotonic() + HOSTED_FIRST_READ_SECONDS
        self.answered = False
        self.stopped = False

    def timeout(self) -> float:
        if self.stopped:
            raise ServiceDown(f"{self.label}: stopped, another read already failed")
        if self.answered:
            return HOSTED_TIMEOUT_SECONDS
        left = self.until - time.monotonic()
        if left < 1:
            raise ServiceDown(f"{self.label} gave no answer within {HOSTED_FIRST_READ_SECONDS:.0f} s")
        return min(HOSTED_TIMEOUT_SECONDS, left)


def _error_detail(resp) -> str:
    """The service's own error message, short: Gemini {"error": {"message"}}, Ollama {"error"}."""
    try:
        err = resp.json().get("error")
        msg = err.get("message") if isinstance(err, dict) else err
    except Exception:
        msg = None
    return f": {str(msg)[:160]}" if msg else ""


def _post_hosted(label: str, url: str, body: dict, headers: dict, budget: _Budget):
    """One hosted request with a timeout; one retry on HTTP 500 / 503, then ServiceDown.
    A usage limit raises QuotaExceeded; a refused key, a timeout or no connection, ServiceDown."""
    for attempt in (1, 2):
        timeout = budget.timeout()
        try:
            resp = httpx.post(url, json=body, headers=headers,
                              timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)))
        except httpx.TimeoutException as exc:
            raise ServiceDown(f"{label} did not answer within {timeout:.0f} s") from exc
        except httpx.HTTPError as exc:
            raise ServiceDown(f"{label} could not be reached ({type(exc).__name__})") from exc
        code = resp.status_code
        if code < 400:
            budget.answered = True
            return resp
        if _is_quota(resp):
            raise QuotaExceeded(f"{label} usage limit reached (HTTP {code})")
        if code in (401, 403) or "api key not valid" in resp.text.lower():
            raise ServiceDown(f"{label} refused the API key (HTTP {code})")
        if code in HOSTED_RETRY_STATUSES and attempt == 1:
            time.sleep(HOSTED_RETRY_PAUSE)
            continue
        raise ServiceDown(f"{label} returned HTTP {code}{' twice' if attempt == 2 else ''}{_error_detail(resp)}")


def _ollama_chat(img, prompt: str, url: str, model: str, label: str, headers: dict | None = None,
                 budget: _Budget | None = None) -> dict:
    """One Ollama /api/chat call on a PIL image: temperature 0, thinking off, JSON mode.
    With a budget (Ollama Cloud) it goes through _post_hosted; without (local Ollama) a model
    load can take minutes, so the timeout is long and any 5xx is retried once."""
    body = {"model": model, "stream": False, "format": "json", "think": False,
            "options": {"temperature": 0},
            "messages": [{"role": "user", "content": prompt, "images": [_png_b64(img)]}]}
    if budget is not None:
        resp = _post_hosted(label, f"{url}/api/chat", body, headers or {}, budget)
        return _json_text(resp.json()["message"]["content"])
    kw = {"headers": headers} if headers else {}
    for attempt in (1, 2):   # one retry on a 5xx: a model load can fail once (CUDA init, VRAM)
        try:
            resp = httpx.post(f"{url}/api/chat", timeout=300, json=body, **kw)
            resp.raise_for_status()
            break
        except httpx.HTTPStatusError as exc:
            if _is_quota(exc.response):
                raise QuotaExceeded(f"{label} usage limit reached (HTTP {exc.response.status_code})") from exc
            if exc.response.status_code in (401, 403):
                raise ExtractorUnavailable(f"{label} refused the API key (HTTP {exc.response.status_code})") from exc
            if attempt == 2 or exc.response.status_code < 500:
                if _out_of_memory(exc.response):
                    raise ExtractorUnavailable(GPU_MEMORY_MESSAGE) from exc
                raise ExtractorUnavailable(f"{label} ({url}, {model}) failed: {exc}") from exc
            time.sleep(2)
        except httpx.HTTPError as exc:
            raise ExtractorUnavailable(f"{label} ({url}, {model}) failed: {exc}") from exc
    return _json_text(resp.json()["message"]["content"])


def _ollama_json(img, prompt: str) -> dict:
    """Local Ollama (development on the PC)."""
    return _ollama_chat(img, prompt, OLLAMA_URL, OLLAMA_MODEL, "Ollama")


def _ollama_cloud_json(img, prompt: str, budget: _Budget) -> dict:
    """Ollama Cloud: the same /api/chat on OLLAMA_CLOUD_URL with a Bearer OLLAMA_API_KEY."""
    key = os.getenv("OLLAMA_API_KEY", "").strip()
    if not key:
        raise ExtractorUnavailable("OLLAMA_API_KEY is not set in backend/.env")
    return _ollama_chat(img, prompt, OLLAMA_CLOUD_URL, OLLAMA_CLOUD_MODEL, "Ollama Cloud",
                        headers={"Authorization": f"Bearer {key}"}, budget=budget)


def gemini_answer_text(data: dict) -> str:
    """The answer text of a generateContent reply: the first candidate's parts that are not
    thoughts ("thought": true), joined. Gemma 4 may put a thought part first, even an empty
    one with thinking off; it is never parsed."""
    cands = data.get("candidates") or []
    if not cands:
        reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
        raise ExtractorUnavailable(f"Gemini returned no answer ({reason})")
    parts = (cands[0].get("content") or {}).get("parts") or []
    answer = [p["text"] for p in parts if not p.get("thought") and isinstance(p.get("text"), str)]
    if not "".join(answer).strip():
        raise ExtractorUnavailable(f"Gemini's reply had no answer part (finishReason "
                                   f"{cands[0].get('finishReason')}, {len(parts)} thought part(s))")
    return "".join(answer)


def _gemini_json(img, prompt: str, budget: _Budget) -> dict:
    """Gemini API generateContent: image inline (base64, never uploaded as a file), temperature 0,
    thinking per GEMINI_THINKING. No JSON mode (Gemma models on the Gemini API may not offer it):
    the prompt asks for JSON and _json_text reads the answer part."""
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        raise ExtractorUnavailable("GEMINI_API_KEY is not set in backend/.env")
    url = f"{GEMINI_URL}/v1beta/models/{GEMINI_MODEL}:generateContent"
    config = {"temperature": 0}
    if GEMINI_THINKING:
        config["thinkingConfig"] = {"thinkingLevel": GEMINI_THINKING}
    body = {"contents": [{"role": "user", "parts": [
                {"inline_data": {"mime_type": "image/png", "data": _png_b64(img)}}, {"text": prompt}]}],
            "generationConfig": config}
    resp = _post_hosted("Gemini", url, body, {"x-goog-api-key": key}, budget)
    return _json_text(gemini_answer_text(resp.json()))


def _read_key(r: dict) -> tuple:
    """What two reads must agree on: name (case/space-insensitive), mapped stat, line."""
    return (re.sub(r"\s+", " ", str(r.get("player_name") or "")).strip().lower(),
            normalize_stat(r.get("stat_label")) or str(r.get("stat_label") or "").strip().lower(),
            _number(r.get("line")))


def sides_offered(buttons) -> str | None:
    """'both' / 'higher' / 'lower' from the pick-button labels a read saw, None if unknown."""
    if isinstance(buttons, str):
        buttons = [buttons]
    seen = {str(x).strip().lower() for x in (buttons or [])}
    hi = any("higher" in x for x in seen)
    lo = any("lower" in x for x in seen)
    return "both" if hi and lo else "higher" if hi else "lower" if lo else None


def _readable(r) -> bool:
    return (isinstance(r, dict) and bool(str(r.get("player_name") or "").strip())
            and _number(r.get("line")) is not None)


def _two_reads(first: dict, second: dict) -> dict:
    """One raw row from two reads of the same card: the first read's text (the second's
    if the first has no name or line), with alerts where they disagree on name, stat or line."""
    a, b = _read_key(first), _read_key(second)
    alerts = [f"two reads disagree on {what}: {x!r} vs {y!r}"
              for what, x, y in zip(("name", "stat", "line"), a, b) if x != y]
    base = first if _readable(first) else second
    other = second if base is first else first
    return {"player_name": base.get("player_name"), "team": None, "stat": base.get("stat_label"),
            "line": base.get("line"), "higher_multiplier": None, "lower_multiplier": None,
            "sides_offered": sides_offered(base.get("buttons")) or sides_offered(other.get("buttons")),
            "reads_agree": not alerts, "alerts": alerts}


def unreadable_row() -> dict:
    """A card that was found but couldn't be read: kept as an unresolved row with a flag."""
    return {"player_name": "", "team": None, "stat": None, "line": None, "higher_multiplier": None,
            "lower_multiplier": None, "sides_offered": None, "reads_agree": None,
            "alerts": [UNREADABLE], "unreadable": True}


def _upscale(img, factor: float):
    from PIL import Image
    return img.resize((int(img.width * factor), int(img.height * factor)), Image.LANCZOS)


def _extract_ollama(png: bytes, media_type: str) -> list[dict]:
    return _extract_cards(png, lambda im, prompt: _ollama_json(im, prompt), OLLAMA_PARALLEL)


def _extract_ollama_cloud(png: bytes, media_type: str) -> list[dict]:
    b = _Budget("Ollama Cloud")
    return _extract_cards(png, lambda im, prompt: _ollama_cloud_json(im, prompt, b), OLLAMA_CLOUD_PARALLEL, b)


def _extract_gemini(png: bytes, media_type: str) -> list[dict]:
    b = _Budget("Gemini")
    return _extract_cards(png, lambda im, prompt: _gemini_json(im, prompt, b), GEMINI_PARALLEL, b)


_FAILS_THE_IMAGE = (QuotaExceeded, ServiceDown)


def _extract_cards(png: bytes, read_json, parallel: int, budget: _Budget | None = None) -> list[dict]:
    """Cards detected: each card cropped and read at each of READ_SCALES (up to `parallel`
    calls at once) with read_json(image, prompt), one raw row per card. No cards: the whole
    image, read twice (1x and 1.5x). A usage limit or a down service on any read fails the
    whole image at once (QuotaExceeded / ServiceDown): queued reads are cancelled, the budget
    stops reads in flight from sending more, and a fallback service reads all of it again.
    Any other error stays with its card. Underdog's iPhone game page goes to _extract_phone."""
    from PIL import Image
    img = Image.open(io.BytesIO(png)).convert("RGB")
    phone = detect_phone(img)
    if phone is not None:
        return _extract_phone(img, phone, read_json, parallel, budget)
    boxes = detect_cards(img)
    if not boxes:
        first, second = [read_json(_upscale(img, f) if f > 1 else img, SHEET_PROMPT).get("cards") or []
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
        jobs += [(_upscale(card, f), CARD_PROMPT) for f in READ_SCALES]
    results = _read_all(jobs, read_json, parallel, budget)
    n, rows = len(READ_SCALES), []
    for i in range(0, len(results), n):
        reads = [r if isinstance(r, dict) else {} for r in results[i:i + n]]
        if not any(_readable(r) for r in reads):
            rows.append(unreadable_row())
            continue
        rows.append(_two_reads(reads[0], reads[1] if len(reads) > 1 else {}))
    return rows


def _read_all(jobs, read_json, parallel: int, budget: _Budget | None = None) -> list:
    """read_json(image, prompt) for each (image, prompt) job, up to `parallel` at once: the
    answers in job order, the exception in place of a read that failed. A usage limit or a
    down service fails the whole image (see _extract_cards); nothing read at all says why."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def read(job):   # one card read never fails the image: its error is kept for that card
        try:
            return read_json(*job)
        except Exception as exc:
            return exc

    ex = ThreadPoolExecutor(max_workers=max(1, parallel))
    futures = [ex.submit(read, job) for job in jobs]
    try:
        for f in as_completed(futures):
            if isinstance(f.result(), _FAILS_THE_IMAGE):
                if budget is not None:
                    budget.stopped = True
                raise f.result()
    finally:
        ex.shutdown(wait=False, cancel_futures=True)     # never wait for a read in flight
    results = [f.result() for f in futures]
    errors = [r for r in results if isinstance(r, Exception)]
    if errors and len(errors) == len(results):          # nothing read at all: say why
        first = next((e for e in errors if isinstance(e, ExtractorUnavailable)), errors[0])
        if isinstance(first, ExtractorUnavailable):
            raise first
        raise ExtractorUnavailable(f"could not read any card ({first!r})"[:300])
    return results


_EXTRACT = {"claude": _extract_claude, "ollama": _extract_ollama, "ollama_cloud": _extract_ollama_cloud,
            "gemini": _extract_gemini}
_LABEL = {"claude": "Claude", "ollama": "Ollama", "ollama_cloud": "Ollama Cloud", "gemini": "Gemini"}
_quota_until: dict[str, float] = {}     # extractor -> time.monotonic() until which it is skipped


def extractor_name() -> str:
    name = os.getenv("SCREENSHOT_EXTRACTOR", DEFAULT_EXTRACTOR).strip().lower()
    if name not in EXTRACTORS:
        raise ExtractorUnavailable(f"SCREENSHOT_EXTRACTOR={name!r}; use one of {EXTRACTORS}")
    return name


def _read_with_fallback(name: str, png: bytes, media_type: str) -> tuple[str, list[dict]]:
    """Run extractor `name`; if it fails and has a FALLBACK, run that and log the switch.
    After a QuotaExceeded the primary is skipped for QUOTA_COOLDOWN_MINUTES. Both failing
    raises one ExtractorUnavailable that says so (shown in the app)."""
    fallback = FALLBACK.get(name)
    if fallback is None:
        return name, _EXTRACT[name](png, media_type)
    first_error = None
    if time.monotonic() < _quota_until.get(name, 0.0):
        first_error = f"usage limit reached earlier; retried after {QUOTA_COOLDOWN_MINUTES} min"
        print(f"[screenshot] {_LABEL[name]} usage limit reached recently: reading with {_LABEL[fallback]}")
    else:
        try:
            return name, _EXTRACT[name](png, media_type)
        except ExtractorUnavailable as exc:
            first_error = str(exc)
            if isinstance(exc, QuotaExceeded):
                _quota_until[name] = time.monotonic() + QUOTA_COOLDOWN_MINUTES * 60
            print(f"[screenshot] {_LABEL[name]} failed ({exc}); switching to {_LABEL[fallback]}")
    try:
        return fallback, _EXTRACT[fallback](png, media_type)
    except ExtractorUnavailable as exc:
        raise ExtractorUnavailable(
            f"Screenshot reading is unavailable right now: {_LABEL[name]} ({first_error}) and "
            f"{_LABEL[fallback]} ({exc}) both failed. Enter the lines by hand, or try again later.") from exc


def extract_rows(image: bytes) -> tuple[str, list[dict]]:
    """(extractor used, normalized rows) for an uploaded image."""
    name = extractor_name()
    png, media_type = prepare_image(image)
    used, raw = _read_with_fallback(name, png, media_type)
    return used, [r for r in (normalize_row(x) for x in raw if isinstance(x, dict)) if r]


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
    """One extracted card in a fixed shape; None when it has no name or no line, except a
    card that was found but couldn't be read (unreadable_row), which is kept with its flag."""
    if r.get("unreadable"):
        return {"player_name": "", "team": None, "stat_text": "", "stat": None, "line": None,
                "higher_multiplier": None, "lower_multiplier": None, "sides_offered": None,
                "reads_agree": None, "alerts": list(r.get("alerts") or [UNREADABLE]), "unreadable": True}
    name = re.sub(r"\s+", " ", str(r.get("player_name") or "")).strip()
    line = _number(r.get("line"))
    if not name or line is None:
        return None
    team = str(r.get("team") or "").strip().upper() or None
    row = {"player_name": name, "team": team, "stat_text": str(r.get("stat") or "").strip(),
           "stat": normalize_stat(r.get("stat")), "line": line,
           "higher_multiplier": _number(r.get("higher_multiplier")),
           "lower_multiplier": _number(r.get("lower_multiplier")),
           "sides_offered": r.get("sides_offered") or _sides_from_multipliers(r),
           "reads_agree": r.get("reads_agree"), "alerts": list(r.get("alerts") or [])}
    if "game_teams" in r:          # a phone row: resolved against its header's two teams only
        row["game_teams"] = r["game_teams"]
    return row


def _sides_from_multipliers(r: dict) -> str | None:
    """Claude path: a Higher / Lower multiplier read means that button is shown."""
    hi = _number(r.get("higher_multiplier")) is not None
    lo = _number(r.get("lower_multiplier")) is not None
    return "both" if hi and lo else "higher" if hi else "lower" if lo else None


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
    "player_name", "team_abbrev", "team_name"}. None left: a player_aliases alias whose player is
    on the card's team (adds "person_id", "via": "alias"), else not_found, or team_mismatch (the
    name is on a roster, but not on the card's team; "roster_teams" lists where). Several ->
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
    if not hits:                     # no exact roster match on the card's team: an alias may (app path only)
        import player_aliases
        via = player_aliases.resolve(conn, name, team)
        if via:
            return via
    if not hits and named:
        return {"status": "team_mismatch", "roster_teams": sorted(f"{h['team_city']} {h['team_name']}" for h in named)}
    return {"status": "ambiguous" if hits else "not_found"}


def game_roster_teams(conn, abbrevs) -> list[dict] | None:
    """The two roster teams a phone header names (ESPN or NBA abbreviation, nickname), or None
    unless there are two, each names exactly one team, and they differ."""
    if not abbrevs or len(abbrevs) != 2:
        return None
    for cols in ("team_abbrev, team_city, team_name, nba_team_id", "team_abbrev, team_city, team_name"):
        try:
            teams = [dict(zip(("team_abbrev", "team_city", "team_name", "nba_team_id"), r))
                     for r in conn.execute(f"SELECT DISTINCT {cols} FROM rosters")]
            break
        except sqlite3.OperationalError:
            teams = []
    out = []
    for a in abbrevs:
        hits = [t for t in teams if str(a or "").strip().upper() in _team_names(t)]
        if len(hits) != 1:
            return None
        out.append(hits[0])
    return out if out[0]["team_name"] != out[1]["team_name"] else None


def resolve_on_game(conn, name: str, game_teams) -> dict:
    """
    A phone row's name against the rosters of its header's two teams only, never league-wide.
    'J. Tatum' (one letter first): that initial + the rest of the name_key ('M. Porter Jr.' ->
    'm porter' matches 'michael porter'); a full name: its exact name_key. An alias counts the
    same way when its player is on one of the two teams now (player_aliases rules: collision
    names and roster names ignored). Exactly one player -> resolve_player's ok shape plus
    "game_opponent" (the other team, 'City Nickname'); several -> ambiguous; none -> not_found;
    no header, or not two roster teams -> no_game_header.
    """
    import player_aliases
    import rosters
    teams = game_roster_teams(conn, game_teams)
    if not teams:
        return {"status": "no_game_header"}
    first, *rest = rosters.name_key(name).split() or [""]

    def fits(key: str) -> bool:
        k = key.split()
        if len(first) == 1:
            return len(k) >= 2 and k[0].startswith(first) and k[1:] == rest
        return k == [first, *rest]

    by_name = {t["team_name"]: t for t in teams}
    hits = {}                      # (name_key, team nickname) -> ok result
    for r in conn.execute("SELECT player_name, name_key, team_abbrev, team_city, team_name FROM rosters "
                          "WHERE team_name IN (?, ?)", tuple(by_name)):
        if fits(r[1]):
            hits[(r[1], r[4])] = {"status": "ok", "player_name": r[0], "team_abbrev": r[2],
                                  "team_name": f"{r[3]} {r[4]}"}
    try:
        aliases = conn.execute("SELECT DISTINCT alias_key, person_id FROM player_aliases").fetchall()
    except sqlite3.OperationalError:
        aliases = []
    for key, pid in aliases:
        if not fits(key) or player_aliases._collides(conn, key) or player_aliases._is_roster_name(conn, key):
            continue
        for team in sorted(player_aliases.current_teams(conn, pid) & set(by_name)):
            shown = player_aliases._display(conn, pid, team)
            hits.setdefault((rosters.name_key(shown["player_name"]), team),
                            {"status": "ok", **shown, "person_id": int(pid), "via": "alias"})
    if len(hits) != 1:
        return {"status": "ambiguous" if hits else "not_found"}
    (_, team), who = next(iter(hits.items()))
    other = next(t for t in teams if t["team_name"] != team)
    return {**who, "game_opponent": f"{other['team_city']} {other['team_name']}"}


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
                      ("logged_under_price", "REAL"), ("line_diff", "REAL"),
                      ("sides_offered", "TEXT"))      # both / higher / lower: the card's pick buttons


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
        if r.get("line") is None:          # a card that couldn't be read has no line to record
            continue
        logged = r.get("logged_line") or {}
        diff = None if not logged else round(r["line"] - logged["line"], 4)
        match = None if diff is None else int(abs(diff) < 1e-9)
        agree = r.get("reads_agree")
        data.append((checked_at, r.get("person_id"), r.get("stat"), r["line"],
                     logged.get("line"), logged.get("captured_at"), logged.get("pull_phase"), match,
                     None if agree is None else int(agree), int(is_rung(r["line"])),
                     logged.get("market_key"), logged.get("over_price"), logged.get("under_price"), diff,
                     r.get("sides_offered")))
    conn.executemany("INSERT INTO line_checks (checked_at, person_id, stat_type, screenshot_line, "
                     "logged_line, logged_captured_at, logged_pull_phase, matches, reads_agree, is_rung, "
                     "logged_market_key, logged_over_price, logged_under_price, line_diff, sides_offered) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", data)
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
        import player_aliases
        player_aliases.init_table(conn)
        out = []
        for r in rows:
            if r.get("unreadable"):        # found but not read: an unresolved row with its flag
                out.append({**r, "resolved_player": "", "match": "unreadable", "opponent_team": "",
                            "is_home": False, "is_back_to_back": False, "game_date": None, "range": None,
                            "flags": [], "alerts": list(r.get("alerts") or [UNREADABLE]),
                            "logged_line": None, "person_id": None})
                continue
            phone = "game_teams" in r
            who = (resolve_on_game(conn, r["player_name"], r["game_teams"]) if phone
                   else resolve_player(conn, r["player_name"], r["team"]))
            ctx = (context_fn(who["player_name"]) or {}) if who["status"] == "ok" else {}
            if phone and who["status"] == "ok" and ctx.get("next_opponent_full") != who["game_opponent"]:
                # the header's game is not his next scheduled one (e.g. another day's screenshot)
                who = {"status": "game_mismatch", "next": ctx.get("next_opponent_full"),
                       "date": ctx.get("next_game_date")}
            row = {**r, "resolved_player": who.get("player_name", ""), "match": who["status"],
                   "opponent_team": "", "is_home": False, "is_back_to_back": False,
                   "game_date": None, "range": None, "flags": [], "alerts": list(r.get("alerts") or []),
                   "logged_line": None, "person_id": None}
            if r["stat"] is None:
                row["flags"].append(f"stat '{r['stat_text']}' is not one of {', '.join(SUPPORTED_STATS)}")
            if who["status"] == "ok":
                row.update(opponent_team=ctx.get("next_opponent_full") or "",
                           is_home=bool(ctx.get("is_home")), is_back_to_back=bool(ctx.get("is_back_to_back")),
                           game_date=ctx.get("next_game_date"))
                # the same personId /predict resolves from this row's name, game date and opponent
                row["person_id"] = who.get("person_id") or database.resolve_person_id(
                    conn, who["player_name"], row["game_date"], row["opponent_team"])
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
                                        f"({', '.join(who.get('roster_teams', []))})",
                       "no_game_header": "the game header's two teams couldn't be read",
                       "game_mismatch": f"the screenshot's game ({' vs '.join(r.get('game_teams') or [])}) is not "
                                        f"his next scheduled game ({who.get('next') or 'none found'}"
                                        f"{', ' + who['date'] if who.get('date') else ''}): likely a screenshot "
                                        f"from another game day"}
                if phone and who["status"] == "not_found":
                    why["not_found"] = f"no roster match on {' or '.join(r['game_teams'])}"
                row["alerts"].append("player not resolved: " + why.get(who["status"], "no exact roster match"))
            out.append(row)
        if record and out:
            record_line_checks(conn, out, checked_at.isoformat())
        return out
    finally:
        conn.close()
