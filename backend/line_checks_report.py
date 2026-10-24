"""
line_checks_report.py
Read-only report on line_checks (screenshot import): how often the screenshot line
matches the latest Underdog line logged in line_snapshots, by stat and by minutes
between that pull and the screenshot. Only rows with a logged line to compare count.

    python line_checks_report.py            # predictions.db next to this file
    python line_checks_report.py --db PATH
"""

import argparse
import datetime as dt
import sqlite3

from database import DB_PATH

AGE_BUCKETS = ((0, 30), (30, 60), (60, 120), (120, 240), (240, None))   # minutes since the pull


def _minutes(checked_at: str, captured_at: str) -> float:
    return (dt.datetime.fromisoformat(checked_at) - dt.datetime.fromisoformat(captured_at)).total_seconds() / 60


def report(db_path: str = DB_PATH) -> dict:
    """{"total", "compared", "overall", "by_stat", "by_age", "reads_disagree"}; each rate entry
    is (matches, compared)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT checked_at, stat_type, matches, logged_captured_at, reads_agree "
                            "FROM line_checks").fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    compared = [r for r in rows if r[2] is not None]
    by_stat: dict = {}
    for _, stat, m, _, _ in compared:
        hit, n = by_stat.get(stat or "unmapped", (0, 0))
        by_stat[stat or "unmapped"] = (hit + m, n + 1)
    label = {b: (f"{b[0]}+ min" if b[1] is None else f"{b[0]}-{b[1]} min") for b in AGE_BUCKETS}
    by_age = {label[b]: (0, 0) for b in AGE_BUCKETS}
    for checked, _, m, captured, _ in compared:
        age = max(0.0, _minutes(checked, captured))
        b = next(b for b in AGE_BUCKETS if age >= b[0] and (b[1] is None or age < b[1]))
        hit, n = by_age[label[b]]
        by_age[label[b]] = (hit + m, n + 1)
    return {"total": len(rows), "compared": len(compared),
            "overall": (sum(r[2] for r in compared), len(compared)),
            "by_stat": dict(sorted(by_stat.items())), "by_age": by_age,
            "reads_disagree": sum(1 for r in rows if r[4] == 0)}


def _rate(hit_n) -> str:
    hit, n = hit_n
    return f"{hit}/{n} ({hit / n:.0%})" if n else "-"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=DB_PATH)
    r = report(ap.parse_args().db)
    print(f"line_checks: {r['total']} cards, {r['compared']} with a logged Underdog line to compare, "
          f"{r['reads_disagree']} where the two reads disagreed")
    print(f"match rate overall: {_rate(r['overall'])}")
    print("by stat:")
    for stat, v in r["by_stat"].items():
        print(f"  {stat:<10} {_rate(v)}")
    print("by minutes since the logged pull:")
    for label, v in r["by_age"].items():
        print(f"  {label:<12} {_rate(v)}")


if __name__ == "__main__":
    main()
