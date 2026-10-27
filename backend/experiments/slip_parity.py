"""
slip_parity.py
/slip before and after the single-flight change (one slip at a time, one worker, refused
during a pre-tip pull). build_slip_before is the old endpoint body verbatim (10 worker
threads), minus the background secondary-stat logging, which the response never included.

Run ONLY on a backup-API copy of predictions.db; reads no outcome column, makes no network
call, writes nothing (the secondary logging is not started):
  python experiments/slip_parity.py --db <copy.db> [--date YYYY-MM-DD] [--size 2] [--measure]
Without --measure: builds every slip size 1-5 with the old and new code on the copy and
prints whether they are identical. With --measure: one new /slip in a fresh process and its
peak memory (peak working set on Windows, VmHWM on Linux), nothing else.
Lines come from the copy's line_snapshots for --date (default: the latest date with
Underdog lines), so the result does not depend on today's date.
"""

import argparse
import concurrent.futures
import json
import os
import sqlite3
import sys
import time

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def repoint(path: str) -> None:
    """Every module on the slip path reads the copy at `path`."""
    import data_fetcher
    import database
    import line_logger
    import predictor
    import rosters
    database.DB_PATH = data_fetcher.DB_PATH = line_logger.DB_PATH = path
    predictor.DB_PATH = rosters.DB_PATH = path


def latest_lines_date(path: str) -> str:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT MAX(game_date) FROM line_snapshots WHERE bookmaker = 'underdog'").fetchone()[0]
    finally:
        conn.close()


def fix_lines(date: str) -> dict:
    """data_fetcher.get_todays_lines returns the copy's Underdog lines for `date`."""
    import data_fetcher
    import line_logger
    lines = line_logger.todays_underdog_lines(date)
    data_fetcher.get_todays_lines = lambda: lines
    return lines


def build_slip_before(size: int = 2) -> dict:
    """The /slip body before the change (main.py at 8cdce5e), secondary logging left out."""
    import data_fetcher
    import predictor

    SLIP_STATS = ["Points", "Rebounds", "Assists", "3PM"]
    STAT_TO_MARKET = {
        "Points":   "player_points",
        "Rebounds": "player_rebounds",
        "Assists":  "player_assists",
        "3PM":      "player_threes",
    }
    MIN_CONFIDENCE = 0.65

    lines = data_fetcher.get_todays_lines()
    if not lines:
        return {"picks": [], "parlay_confidence": 0.0}

    def _predict_player(player_name: str) -> list[dict]:
        player_data = lines.get(player_name, {})
        home_team = player_data.get("home_team", "")
        away_team = player_data.get("away_team", "")
        game_id   = player_data.get("game_id", "")

        ctx = data_fetcher.get_player_context(player_name)
        if ctx is None or not ctx.get("next_opponent_full"):
            return []

        opponent_team   = ctx["next_opponent_full"]
        is_home         = ctx["is_home"]
        is_back_to_back = ctx["is_back_to_back"]

        results = []
        for stat_type in SLIP_STATS:
            market_data = player_data.get(STAT_TO_MARKET[stat_type])
            if not market_data:
                continue
            line_value = market_data["value"]
            source     = market_data["source"]
            try:
                pred = predictor.predict(
                    player_name=player_name,
                    opponent_team=opponent_team,
                    stat_line=line_value,
                    stat_type=stat_type,
                    is_home=is_home,
                    is_back_to_back=is_back_to_back,
                )
            except Exception:
                continue
            if pred["confidence"] < MIN_CONFIDENCE:
                continue
            results.append({
                "player":     player_name,
                "stat_type":  stat_type,
                "line":       line_value,
                "outcome":    pred["outcome"],
                "confidence": pred["confidence"],
                "source":     source,
                "game":       f"{home_team} vs {away_team}",
                "_game_id":   game_id,
            })
        return results

    all_candidates: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(_predict_player, name): name for name in lines}
        for fut in concurrent.futures.as_completed(futures):
            try:
                all_candidates.extend(fut.result())
            except Exception:
                pass

    all_candidates.sort(key=lambda x: x["confidence"], reverse=True)

    picks: list[dict] = []
    already_picked: set = set()
    used_games:     set = set()
    used_stat_types: set = set()
    used_players:   set = set()

    def _fill(min_conf: float, block_same_game: bool, block_same_stat: bool) -> None:
        for i, c in enumerate(all_candidates):
            if len(picks) >= size:
                break
            if i in already_picked:
                continue
            if c["confidence"] < min_conf:
                continue
            if block_same_game and c["_game_id"] in used_games:
                continue
            if block_same_stat and c["stat_type"] in used_stat_types:
                continue
            if not block_same_game and c["player"] in used_players:
                continue
            picks.append(c)
            already_picked.add(i)
            used_games.add(c["_game_id"])
            used_stat_types.add(c["stat_type"])
            used_players.add(c["player"])

    _fill(0.65, block_same_game=True,  block_same_stat=True)   # pass 1
    if len(picks) < size:
        _fill(0.60, block_same_game=True,  block_same_stat=False)  # pass 2
    if len(picks) < size:
        _fill(0.55, block_same_game=False, block_same_stat=False)  # pass 3

    for pick in picks:
        pick.pop("_game_id", None)

    parlay_confidence = 1.0
    for pick in picks:
        parlay_confidence *= pick["confidence"]
    parlay_confidence = round(parlay_confidence, 4) if picks else 0.0

    return {"picks": picks, "parlay_confidence": parlay_confidence}


def compare(sizes=(1, 2, 3, 4, 5)) -> dict:
    """Old and new slips for each size, built back to back on the repointed copy."""
    import main
    out = {}
    for size in sizes:
        old = build_slip_before(size)
        new, _ = main._build_slip(size)
        out[size] = {"identical": old == new, "picks": len(new["picks"]), "old": old, "new": new}
    return out


def main_cli():
    from challenger_memory_measure import _block_network, mem_mb
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--date")
    ap.add_argument("--size", type=int, default=2)
    ap.add_argument("--measure", action="store_true")
    a = ap.parse_args()
    _block_network()
    path = os.path.abspath(a.db)
    date = a.date or latest_lines_date(path)
    base = mem_mb()
    import main
    repoint(path)
    lines = fix_lines(date)
    imported = mem_mb()
    if a.measure:
        t0 = time.perf_counter()
        slip, _ = main._build_slip(a.size)
        print(json.dumps({"date": date, "players_with_lines": len(lines), "size": a.size,
                          "picks": len(slip["picks"]), "seconds": round(time.perf_counter() - t0, 1),
                          "mb": {"baseline": round(base[0]), "after_import": round(imported[0]),
                                 "peak_after_slip": round(mem_mb()[0]), "now": round(mem_mb()[1])}}, indent=1))
        return
    res = compare()
    print(json.dumps({"date": date, "players_with_lines": len(lines),
                      "by_size": {s: {"identical": r["identical"], "picks": r["picks"]} for s, r in res.items()},
                      "all_identical": all(r["identical"] for r in res.values())}, indent=1))
    for s, r in res.items():
        if not r["identical"]:
            print(f"size {s} differs:\n old {r['old']}\n new {r['new']}")


if __name__ == "__main__":
    main_cli()
