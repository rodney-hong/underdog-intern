"""
screenshot_import.py
Screenshot of pick'em player cards -> rows to confirm in the app -> the normal /predict.

The LLM only transcribes text from the image (player, team, stat, line, multipliers).
It never predicts: predictions come from /predict after the user confirms each row.

Extractor (SCREENSHOT_EXTRACTOR in .env):
  claude  (default) Claude API, key ANTHROPIC_API_KEY, model SCREENSHOT_CLAUDE_MODEL.
  ollama  local Ollama vision model SCREENSHOT_OLLAMA_MODEL (default gemma4:e2b) at
          OLLAMA_URL. Measured 2026-10-24 on a synthetic 4-card screenshot: names and
          lines right, but '3-Pointers Made' read as Points in 3/3 runs, and its
          schema-constrained mode returned wrong lines; so it is not the default.
The image is held in memory only: nothing is written to disk.
"""

import base64
import io
import json
import os
import re

import httpx
from dotenv import load_dotenv

load_dotenv()

EXTRACTORS = ("claude", "ollama")
CLAUDE_MODEL = os.getenv("SCREENSHOT_CLAUDE_MODEL", "claude-opus-5-5")
OLLAMA_MODEL = os.getenv("SCREENSHOT_OLLAMA_MODEL", "gemma4:e2b")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_SIDE = 2000          # longer side, px; larger screenshots are scaled down first
SUPPORTED_STATS = ("Points", "Rebounds", "Assists", "3PM")
RANGE_GAMES = 82               # "historical range": min..max over his last 82 counted games

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


def _extract_ollama(png: bytes, media_type: str) -> list[dict]:
    # JSON mode, not a JSON schema: the schema-constrained output gave wrong numbers.
    try:
        resp = httpx.post(f"{OLLAMA_URL}/api/chat", timeout=300, json={
            "model": OLLAMA_MODEL, "stream": False, "format": "json",
            "options": {"temperature": 0},
            "messages": [{"role": "user", "images": [base64.b64encode(png).decode()],
                          "content": PROMPT + ' Shape: {"rows": [{"player_name", "team", "stat", '
                                              '"line", "higher_multiplier", "lower_multiplier"}]}'}],
        })
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise ExtractorUnavailable(f"Ollama ({OLLAMA_URL}, {OLLAMA_MODEL}) failed: {exc}") from exc
    data = json.loads(resp.json()["message"]["content"])
    return data["rows"] if isinstance(data, dict) else data


_EXTRACT = {"claude": _extract_claude, "ollama": _extract_ollama}


def extractor_name() -> str:
    name = os.getenv("SCREENSHOT_EXTRACTOR", "claude").strip().lower()
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
            "lower_multiplier": _number(r.get("lower_multiplier"))}


# ---------------------------------------------------------------------------
# Resolving rows for the confirmation table (no prediction here)
# ---------------------------------------------------------------------------

def resolve_player(conn, name: str, team: str | None) -> dict:
    """
    Exact name_key against the ESPN rosters, then the card's team (abbreviation or name)
    if it shows one. Exactly one roster player left -> {"status": "ok", "player_name",
    "team_abbrev", "team_name"}; none -> not_found; several -> ambiguous (left blank).
    """
    import rosters
    key = rosters.name_key(name)
    try:
        hits = [dict(r) for r in conn.execute(
            "SELECT player_name, team_abbrev, team_city, team_name FROM rosters WHERE name_key = ?",
            (key,)).fetchall()]
    except Exception:
        hits = []
    if team:
        t = team.upper()
        hits = [h for h in hits if t in {str(h["team_abbrev"] or "").upper(),
                                         str(h["team_name"] or "").upper(),
                                         f"{h['team_city']} {h['team_name']}".upper()}]
    if len(hits) == 1:
        h = hits[0]
        return {"status": "ok", "player_name": h["player_name"], "team_abbrev": h["team_abbrev"],
                "team_name": f"{h['team_city']} {h['team_name']}"}
    return {"status": "ambiguous" if hits else "not_found"}


def historical_range(conn, player_name: str, stat: str) -> dict | None:
    """min / max of the stat over his last RANGE_GAMES counted games, or None if none."""
    import database
    import game_types
    cols = database._RESOLVE_STAT_COLS.get(stat)
    if not cols or stat not in SUPPORTED_STATS:
        return None
    exact, loose = database._cached_person_index(conn)
    import rosters
    ids = exact.get(database._ascii_normalize(player_name)) or loose.get(rosters.name_key(player_name))
    if not ids or len(ids) != 1:
        return None
    rows = conn.execute(
        f"SELECT {cols[0]} FROM player_stats WHERE personId = ? AND gameType IN ({game_types.COUNTED_SQL}) "
        f"AND {cols[0]} IS NOT NULL ORDER BY gameDateTimeEst DESC LIMIT ?",
        (next(iter(ids)), RANGE_GAMES)).fetchall()
    if not rows:
        return None
    vals = [float(r[0]) for r in rows]
    return {"min": min(vals), "max": max(vals), "games": len(vals)}


def confirmation_rows(rows: list[dict], context_fn=None) -> list[dict]:
    """
    Each extracted row plus what the confirmation table needs: the resolved player
    (blank unless exactly one exact name_key + team match), his next game context for
    /predict (opponent, home, back-to-back, game date), the historical range and flags.
    """
    import database
    if context_fn is None:
        import data_fetcher
        context_fn = data_fetcher.get_player_context
    conn = database.get_connection()
    try:
        out = []
        for r in rows:
            who = resolve_player(conn, r["player_name"], r["team"])
            row = {**r, "resolved_player": who.get("player_name", ""), "match": who["status"],
                   "opponent_team": "", "is_home": False, "is_back_to_back": False,
                   "game_date": None, "range": None, "flags": []}
            if r["stat"] is None:
                row["flags"].append(f"stat '{r['stat_text']}' is not one of {', '.join(SUPPORTED_STATS)}")
            if who["status"] == "ok":
                ctx = context_fn(who["player_name"]) or {}
                row.update(opponent_team=ctx.get("next_opponent_full") or "",
                           is_home=bool(ctx.get("is_home")), is_back_to_back=bool(ctx.get("is_back_to_back")),
                           game_date=ctx.get("next_game_date"))
                if r["stat"]:
                    rng = historical_range(conn, who["player_name"], r["stat"])
                    row["range"] = rng
                    if rng is None:
                        row["flags"].append("no game history for this stat")
                    elif not rng["min"] <= r["line"] <= rng["max"]:
                        row["flags"].append(f"line {r['line']:g} is outside his range "
                                            f"{rng['min']:g}–{rng['max']:g} (last {rng['games']} games)")
            out.append(row)
        return out
    finally:
        conn.close()
