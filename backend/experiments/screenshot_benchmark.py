"""
screenshot_benchmark.py
Accuracy and seconds per screenshot for the screenshot import (screenshot_import.py),
on a local folder of screenshots. Read-only: images are opened in memory, never copied,
and nothing is written to the database (confirmation rows are built with record=False).
Keep the folder outside the repo; never commit it.

    python experiments/screenshot_benchmark.py "C:/path/to/screenshots"
    python experiments/screenshot_benchmark.py DIR --labels DIR/labels.csv

labels.csv (optional, the truth for accuracy): file,player_name,stat,line
  one row per card; stat is Points / Rebounds / Assists / 3PM.
Without labels it reports what it can check: the two reads agreeing, players resolved,
and the screenshot line matching today's logged Underdog line (a proxy, not accuracy).
"""

import argparse
import csv
import io
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rosters                      # noqa: E402
import screenshot_import as si      # noqa: E402

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def _labels(path: str | None) -> dict:
    if not path:
        return {}
    out: dict = {}
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out.setdefault(r["file"], []).append(
                (rosters.name_key(r["player_name"]), r["stat"].strip(), float(r["line"])))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("folder")
    ap.add_argument("--labels")
    ap.add_argument("--parallel", type=int, default=si.OLLAMA_PARALLEL)
    args = ap.parse_args()
    si.OLLAMA_PARALLEL = args.parallel
    labels = _labels(args.labels)
    files = sorted(f for f in os.listdir(args.folder) if f.lower().endswith(IMAGE_EXT))
    if not files:
        sys.exit(f"no images in {args.folder}")
    from PIL import Image
    name = si.extractor_name()
    print(f"extractor {name}" + (f" ({si.OLLAMA_MODEL}, parallel {si.OLLAMA_PARALLEL})" if name == "ollama" else ""))
    tot = {"secs": 0.0, "cards": 0, "agree": 0, "resolved": 0, "compared": 0, "logged_match": 0,
           "labeled": 0, "correct": 0, "fallback": 0}
    for fname in files:
        with open(os.path.join(args.folder, fname), "rb") as f:
            data = f.read()
        boxes = si.detect_cards(Image.open(io.BytesIO(data)).convert("RGB"))
        t0 = time.monotonic()
        _, rows = si.extract_rows(data)
        secs = time.monotonic() - t0
        conf = si.confirmation_rows(rows, record=False)
        agree = sum(1 for r in rows if r.get("reads_agree") is not False)
        resolved = sum(1 for r in conf if r["resolved_player"])
        compared = [r for r in conf if r["logged_line"]]
        matched = sum(1 for r in compared if abs(r["logged_line"]["line"] - r["line"]) < 1e-9)
        line = (f"{fname}: {secs:.1f}s, {len(boxes) or 'no'} cards detected"
                f"{' (whole image read)' if not boxes else ''}, {len(rows)} rows, "
                f"reads agree {agree}/{len(rows)}, resolved {resolved}, "
                f"logged line matches {matched}/{len(compared)}")
        if fname in labels:
            truth = labels[fname]
            got = [(rosters.name_key(r["player_name"]), r["stat"], r["line"]) for r in rows]
            correct = sum(1 for t in truth if t in got)
            line += f", correct {correct}/{len(truth)} labeled cards"
            tot["labeled"] += len(truth)
            tot["correct"] += correct
        print(line)
        tot["secs"] += secs
        tot["cards"] += len(rows)
        tot["agree"] += agree
        tot["resolved"] += resolved
        tot["compared"] += len(compared)
        tot["logged_match"] += matched
        tot["fallback"] += not boxes
    n = len(files)
    print(f"\n{n} screenshots, {tot['secs'] / n:.1f}s per screenshot "
          f"({tot['secs'] / max(1, tot['cards']):.1f}s per card), {tot['cards']} cards, "
          f"whole-image fallback on {tot['fallback']}")
    print(f"two reads agree: {tot['agree']}/{tot['cards']}; players resolved: {tot['resolved']}/{tot['cards']}")
    print(f"screenshot line = today's logged Underdog line: {tot['logged_match']}/{tot['compared']} compared")
    if tot["labeled"]:
        print(f"accuracy vs labels (name, stat and line all right): {tot['correct']}/{tot['labeled']} "
              f"({tot['correct'] / tot['labeled']:.0%})")


if __name__ == "__main__":
    main()
