"""
build_check_set.py
Select 75 real news items for the Phase 3 hand-labeled extraction check set and
write experiments/check_set.csv with EMPTY label columns. Labels are filled in
by hand; this script never writes labels.

Pools (real rows only, nothing synthetic):
  live    news_items in predictions.db (ESPN injuries, collected by news_ingest)
  replay  official NBA injury report rows for 2026-04-10, ingested by
          news_ingest's own poll code into a scratch DB (pass --replay-db).
          Preseason has no official reports, so this is the only official data.

Strata are assigned by fixed regex rules: a selection tag, not a label, and
it can be wrong. Each item lands in at most one stratum (first match in
STRATA order). Selection is seeded.

    python experiments/build_check_set.py --replay-db <path/to/news_replay15.db>
"""

import os
import re
import csv
import json
import random
import sqlite3
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
DB   = os.path.join(HERE, "..", "predictions.db")
OUT  = os.path.join(HERE, "check_set.csv")
SEED = 20261016

# target counts; minutes_restriction is capped by what real data contains
TARGETS = {
    "clear_out":            13,
    "questionable_probable": 13,
    "minutes_restriction":   5,
    "return_from_injury":   14,
    "name_variant":         15,
    "no_real_change":       15,
}

LABEL_COLS = ["label_player", "label_team", "label_status", "label_minutes_outlook",
              "label_is_change", "label_source_time", "label_notes"]

_SUFFIX = re.compile(r"\b(Jr\.|Sr\.|II|III|IV)\b")


def _official(row_text: str) -> dict:
    game, team, player, status, reason = (row_text.split(" | ") + [""] * 5)[:5]
    return {"team": team, "player": player, "status": status, "reason": reason}


def stratum(item: dict) -> str | None:
    t, src = item["raw_text"], item["source"]
    if src == "nba_injury_report":
        o = _official(t)
        prev = item["previous_text"] or ""
        prev_status = _official(prev)["status"] if prev.startswith(("0", "1")) else ""
        if not o["player"]:
            return None                                   # NOT YET SUBMITTED placeholder
        if _SUFFIX.search(o["player"]) or re.match(r"^[A-Z][a-z]*[A-Z]|^[A-Z]{2}\b", o["player"].split(", ")[-1]):
            return "name_variant"                         # "Porter Jr., Michael", "Dennis, RayJ", "Hall, PJ"
        if o["status"] == "Available" and prev_status in ("Out", "Doubtful", "Questionable", "Probable"):
            return "return_from_injury"
        if o["status"] in ("Questionable", "Probable", "Doubtful"):
            return "questionable_probable"
        if o["status"] == "Out" and prev_status and prev_status != "Out":
            return "clear_out"
        if o["status"] == "Out" and not prev:
            return "no_real_change"                       # first listing of a standing Out
        return None
    # ESPN — names first, since name variants are the scarcest stratum
    name = t.split(" | ")[1]
    if "-" in name or "." in name or _SUFFIX.search(name) or re.search(r"\b[A-Z]{2,3}\b", name):
        return "name_variant"                             # "Caldwell-Pope", "CJ McCollum", "Jr."
    if re.search(r"ramp(ing|s)? up|workload|minutes? (restriction|limit)|limited minutes|reconditioning", t, re.I):
        return "minutes_restriction"
    if re.search(r"\b(questionable|probable|doubtful|game-time|uncertain)\b", t, re.I):
        return "questionable_probable"
    if re.search(r"expected to play|cleared|will return|returning|available for|set to play|will play", t, re.I):
        return "return_from_injury"
    if re.search(r"\bremains\b|\bcontinues\b|\bstill\b|no timetable|season-ending|remainder of the season|entirety of", t, re.I):
        return "no_real_change"
    if re.search(r"ruled out|won't play|will not play|will miss|won't be available|inactive", t, re.I):
        return "clear_out"
    return None


def load_live() -> list[dict]:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, source, url, raw_text, published_at FROM news_items "
                        "ORDER BY id").fetchall()
    conn.close()
    items, last_by_player = [], {}
    for r in rows:
        key = (r["source"], r["raw_text"].split(" | ")[1] if " | " in r["raw_text"] else "")
        items.append({"item_ref": f"live:news_items:{r['id']}", "source": r["source"],
                      "url": r["url"], "published_at": r["published_at"],
                      "raw_text": r["raw_text"], "previous_text": last_by_player.get(key, "")})
        last_by_player[key] = r["raw_text"]
    return items


def load_replay(path: str) -> list[dict]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    reports = conn.execute("SELECT url, published_at, rows_json FROM nba_report_index "
                           "ORDER BY published_at").fetchall()
    # previous row text per (game, team, player) as of each report URL
    prev_lookup: dict[tuple, str] = {}
    last_text: dict[tuple, str] = {}
    for rep in reports:
        rows = json.loads(rep["rows_json"])
        for r in rows:
            k = (r["game_date"], r["matchup"], r["team"], r["player"])
            prev_lookup[(rep["url"], *k)] = last_text.get(k, "")
        for r in rows:
            k = (r["game_date"], r["matchup"], r["team"], r["player"])
            game = f"{r['game_date']} {r['game_time']} {r['matchup']}".strip()
            last_text[k] = " | ".join([game, r["team"], r["player"], r["status"], r["reason"]])
    items = []
    for r in conn.execute("SELECT id, source, url, raw_text, published_at FROM news_items "
                          "WHERE source = 'nba_injury_report' ORDER BY id"):
        game, team, player = r["raw_text"].split(" | ")[:3]
        gd, *_rest = game.split(" ")
        matchup = game.split(" ")[-1]
        prev = prev_lookup.get((r["url"], gd, matchup, team, player), "")
        items.append({"item_ref": f"replay:2026-04-10:{r['id']}", "source": r["source"],
                      "url": r["url"], "published_at": r["published_at"],
                      "raw_text": r["raw_text"], "previous_text": prev})
    conn.close()
    return items


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay-db", required=True)
    args = ap.parse_args()

    pool = load_live() + load_replay(args.replay_db)
    by_stratum: dict[str, list[dict]] = {s: [] for s in TARGETS}
    for it in pool:
        s = stratum(it)
        if s:
            by_stratum[s].append(it)

    rng = random.Random(SEED)
    chosen = []
    shortfall = 0
    for s, n in TARGETS.items():
        cands = by_stratum[s]
        # balance sources inside a stratum where both exist
        live = [c for c in cands if c["item_ref"].startswith("live")]
        rep  = [c for c in cands if c["item_ref"].startswith("replay")]
        rng.shuffle(live); rng.shuffle(rep)
        pick = []
        while len(pick) < n and (live or rep):
            for src in (live, rep):
                if src and len(pick) < n:
                    pick.append(src.pop())
        shortfall += n - len(pick)
        for p in pick:
            p["stratum"] = s
        chosen += pick
        print(f"{s:24s} available {len(cands):4d}  chosen {len(pick):3d} "
              f"(live {sum(p['item_ref'].startswith('live') for p in pick)}, "
              f"replay {sum(p['item_ref'].startswith('replay') for p in pick)})")
    if shortfall:
        print(f"WARNING: {shortfall} short of target")

    rng.shuffle(chosen)   # don't present strata in blocks
    cols = ["check_id", "item_ref", "stratum", "source", "published_at", "url",
            "previous_text", "raw_text"] + LABEL_COLS
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for i, it in enumerate(chosen, 1):
            w.writerow({"check_id": i, **{c: it.get(c, "") for c in cols[1:8]},
                        **{c: "" for c in LABEL_COLS}})
    print(f"wrote {len(chosen)} items -> {OUT}")


if __name__ == "__main__":
    main()
