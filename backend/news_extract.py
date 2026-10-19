"""
news_extract.py
Phase 3 extraction: news_items -> structured fields. NOT ENABLED: nothing
schedules or calls this until the hand-labeled check set
(experiments/check_set.csv) has been scored and approved.

Paper mode only; nothing here feeds predictions.

The model call is behind one function, call_model(). Swap the backend by
replacing that function (or setting EXTRACT_BACKEND); everything else —
prompt, schema, validation, storage — stays the same.
"""

import os
import json

import httpx
from dotenv import load_dotenv

load_dotenv()

OLLAMA_URL   = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("EXTRACT_MODEL", "gemma4:e2b")

# ---------------------------------------------------------------------------
# Fixed output schema
# ---------------------------------------------------------------------------

STATUS_VALUES  = ["out", "doubtful", "questionable", "probable", "available", "unknown"]
MINUTES_VALUES = ["restricted", "normal", "unknown"]

SCHEMA = {
    "type": "object",
    "properties": {
        "player":          {"type": "string"},   # "First Last" as normally written
        "team":            {"type": "string"},   # full team name, e.g. "Utah Jazz"
        "status":          {"type": "string", "enum": STATUS_VALUES},
        "minutes_outlook": {"type": "string", "enum": MINUTES_VALUES},
        "is_change":       {"type": "boolean"},
        "source_time":     {"type": "string"},   # ISO-8601 UTC; the item's published time
    },
    "required": ["player", "team", "status", "minutes_outlook", "is_change", "source_time"],
}

PROMPT = """You extract NBA availability news into JSON. Use only the text given.

Fields:
- player: the player the item is about, written "First Last" (e.g. "Michael Porter Jr.", not "Porter Jr., Michael").
- team: the player's full team name.
- status: availability for the player's next game: one of {statuses}.
  Use "unknown" if the item does not say.
- minutes_outlook: "restricted" if the item says minutes, workload or playing time will be limited;
  "normal" if it says he will play without limits; otherwise "unknown".
- is_change: true only if this item reports a change in availability compared with PREVIOUS
  (e.g. questionable -> out, out -> available). Restating an unchanged status is false.
  If PREVIOUS is "none", judge from the item text alone (e.g. "will return" or "ruled out" are changes;
  "remains out" is not).
- source_time: copy PUBLISHED exactly.

SOURCE: {source}
PUBLISHED: {published_at}
PREVIOUS: {previous}
ITEM: {raw_text}

Return only the JSON object."""


def build_prompt(item: dict) -> str:
    return PROMPT.format(statuses=", ".join(STATUS_VALUES), source=item["source"],
                         published_at=item.get("published_at") or "unknown",
                         previous=item.get("previous_text") or "none",
                         raw_text=item["raw_text"])


# ---------------------------------------------------------------------------
# The one swappable model call
# ---------------------------------------------------------------------------

def call_model(prompt: str, schema: dict) -> dict:
    """
    Send prompt, return the parsed JSON object. Currently: local Ollama with a
    JSON-schema `format`, temperature 0. Replace this function to change backend.
    """
    resp = httpx.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": OLLAMA_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "format": schema,
            "stream": False,
            "options": {"temperature": 0},
        },
        timeout=120,
    )
    resp.raise_for_status()
    return json.loads(resp.json()["message"]["content"])


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate(out: dict) -> tuple[dict, list[str]]:
    """Coerce to the fixed schema. Returns (clean, problems). Never raises."""
    problems = []
    clean = {}
    for k in SCHEMA["required"]:
        if k not in out:
            problems.append(f"missing {k}")
    clean["player"] = str(out.get("player", "")).strip()
    clean["team"] = str(out.get("team", "")).strip()
    st = str(out.get("status", "")).strip().lower()
    if st not in STATUS_VALUES:
        problems.append(f"bad status {st!r}")
        st = "unknown"
    clean["status"] = st
    mo = str(out.get("minutes_outlook", "")).strip().lower()
    if mo not in MINUTES_VALUES:
        problems.append(f"bad minutes_outlook {mo!r}")
        mo = "unknown"
    clean["minutes_outlook"] = mo
    ic = out.get("is_change")
    if isinstance(ic, str):
        ic = ic.strip().lower() in ("true", "yes", "1")
    clean["is_change"] = bool(ic)
    clean["source_time"] = str(out.get("source_time", "")).strip()
    return clean, problems


def extract(item: dict) -> tuple[dict, list[str]]:
    """item: {source, raw_text, published_at, previous_text}."""
    try:
        raw = call_model(build_prompt(item), SCHEMA)
    except Exception as exc:
        return {}, [f"model call failed: {exc!r}"]
    return validate(raw)
