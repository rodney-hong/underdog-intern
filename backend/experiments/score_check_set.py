"""
score_check_set.py
Run Phase 3 extraction over the hand-labeled check set and score it, using the
rules fixed in check_set_GUIDE.md before labeling.

    python experiments/score_check_set.py --run      # extract (cached, resumable)
    python experiments/score_check_set.py --score    # report

Model outputs are cached in results_check_set_extraction.json (gitignored via
results*.json), keyed by check_id and model, so scoring never re-calls the model.
"""

import os
import sys
import csv
import json
import time
import difflib
import argparse
import unicodedata
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import news_extract as X  # noqa: E402

CHECK = os.path.join(HERE, "check_set.csv")
CACHE = os.path.join(HERE, "results_check_set_extraction.json")
FIELDS = ["player", "team", "status", "minutes_outlook", "is_change", "source_time"]


def load_rows() -> list[dict]:
    with open(CHECK, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_cache() -> dict:
    if os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def run() -> None:
    rows, cache = load_rows(), load_cache()
    key_model = X.OLLAMA_MODEL
    done = cache.setdefault(key_model, {})
    for r in rows:
        if r["check_id"] in done:
            continue
        t = time.time()
        out, problems = X.extract(r)
        done[r["check_id"]] = {"output": out, "problems": problems,
                               "seconds": round(time.time() - t, 1)}
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=1)
        print(f"#{r['check_id']:>2} {done[r['check_id']]['seconds']:5.1f}s {out} {problems or ''}",
              flush=True)
    print(f"done: {len(done)}/{len(rows)} for {key_model}")


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def norm(s) -> str:
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join(s.split()).casefold()


def truth(r: dict) -> dict:
    return {
        "player": r["label_player"], "team": r["label_team"], "status": r["label_status"],
        "minutes_outlook": r["label_minutes_outlook"],
        "is_change": r["label_is_change"].strip().lower() == "true",
        "source_time": r["label_source_time"].strip() or r["published_at"],
    }


def score() -> None:
    rows = load_rows()
    cache = load_cache().get(X.OLLAMA_MODEL, {})
    skipped = [r["check_id"] for r in rows if "skip" in r["label_notes"].lower()]
    rows = [r for r in rows if r["check_id"] not in skipped]
    missing = [r["check_id"] for r in rows if r["check_id"] not in cache]
    if missing:
        print(f"not yet extracted: {missing}. Run --run first.")
        return
    roster = sorted({r["label_player"] for r in rows})

    results = []
    for r in rows:
        t, p = truth(r), cache[r["check_id"]]["output"]
        ok = {}
        for f in FIELDS:
            if f == "is_change":
                ok[f] = (p.get(f) is not None) and bool(p.get(f)) == t[f]
            else:
                ok[f] = norm(p.get(f, "")) == norm(t[f])
        # player through the existing matcher (difflib, cutoff 0.85, as in get_player_line)
        m = difflib.get_close_matches(p.get("player", ""), roster, n=1, cutoff=0.85)
        ok["player_fuzzy"] = bool(m) and m[0] == t["player"]
        results.append({"r": r, "t": t, "p": p, "ok": ok,
                        "problems": cache[r["check_id"]]["problems"]})

    n = len(results)
    cols = FIELDS[:1] + ["player_fuzzy"] + FIELDS[1:]
    print(f"model: {X.OLLAMA_MODEL}   items: {n}   skipped: {len(skipped)}   "
          f"schema problems: {sum(bool(x['problems']) for x in results)}")
    secs = [cache[x['r']['check_id']]['seconds'] for x in results]
    print(f"latency: median {sorted(secs)[len(secs)//2]}s, max {max(secs)}s\n")

    print("ACCURACY PER FIELD")
    for c in cols:
        k = sum(x["ok"][c] for x in results)
        print(f"  {c:16s} {k:3d}/{n}  {k/n:6.1%}")
    allk = sum(all(x["ok"][f] for f in FIELDS) for x in results)
    print(f"  {'all 6 fields':16s} {allk:3d}/{n}  {allk/n:6.1%}")

    tp = sum(x["p"].get("is_change") is True and x["t"]["is_change"] for x in results)
    fp = sum(x["p"].get("is_change") is True and not x["t"]["is_change"] for x in results)
    fn = sum(x["p"].get("is_change") is not True and x["t"]["is_change"] for x in results)
    tn = n - tp - fp - fn
    prec = tp / (tp + fp) if tp + fp else float("nan")
    rec = tp / (tp + fn) if tp + fn else float("nan")
    print(f"\nIS_CHANGE (true = Phase 4 trigger)\n  TP {tp}  FP {fp}  FN {fn}  TN {tn}"
          f"   precision {prec:.1%}   recall {rec:.1%}")

    for group in ("stratum", "source"):
        print(f"\nPER {group.upper()}  (" + "  ".join(c[:8] for c in cols) + ")")
        g = defaultdict(list)
        for x in results:
            g[x["r"][group]].append(x)
        for name, xs in sorted(g.items()):
            cells = "  ".join(f"{sum(x['ok'][c] for x in xs)}/{len(xs)}".rjust(8) for c in cols)
            print(f"  {name:22s} {cells}")

    print("\nCONFUSION — status (rows = label, cols = predicted)")
    sv = X.STATUS_VALUES
    print("  " + " " * 13 + "".join(s[:6].rjust(8) for s in sv))
    for a in sv:
        print(f"  {a:13s}" + "".join(
            str(sum(x['t']['status'] == a and x['p'].get('status') == b for x in results)).rjust(8)
            for b in sv))

    print("\nERRORS")
    for x in results:
        bad = [f for f in FIELDS if not x["ok"][f]]
        if not bad:
            continue
        r = x["r"]
        print(f"  #{r['check_id']} [{r['stratum']}/{r['source']}] " + "; ".join(
            f"{f}: label={x['t'][f]!r} got={x['p'].get(f)!r}" for f in bad))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args()
    if a.run:
        run()
    if a.score or not a.run:
        score()
