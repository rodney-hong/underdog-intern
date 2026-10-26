"""
challenger_memory_parity.py
Proof for the challenger memory fix (cloud migration plan v2, step 0.9): the new
inference.features_for_context (team rows of the player's own teams only) gives exactly
the output of the code before the fix (every team's rows; for a player with fewer than 11
earlier games, every regular-season row since 1946).

Run on a backup-API copy, never on the live file:
  python experiments/challenger_memory_parity.py --db <copy.db> [--sample 40] [--out results/x.json]

  1. every stored challenger_predictions row: rebuild its context (person_id, teams, home,
     tip, stat, line) and compare old vs new features and predictions: mu, alpha, p_over,
     expected_minutes, rate, n_prior / status. Exact (==), not approximately equal.
  2. a sample of players with fewer than 11 played regular-season games before the tip.
  3. SHA-256 of the frozen challenger file before and after (it is only read).
Reads no outcome column (actual_value, result, resolved_at). Also reports, separately and
not as a pass criterion, how the recomputed values compare with the stored ones (box
scores before tip can be revised by later Kaggle versions).
Peak memory of the old and new calls is reported per call when psutil is unavailable
(tracemalloc's Python-heap peak; see experiments/challenger_memory_measure.py for RSS).
"""

import argparse
import datetime as dt
import json
import os
import random
import sqlite3
import sys
import time

import numpy as np
import pandas as pd

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

from challenger import inference, live                              # noqa: E402
from challenger import model as cm                                   # noqa: E402
from challenger.features import (LEAGUE_TEAM_GAMES, OPP_GAMES, allowed_table, build_features,   # noqa: E402
                                 load_team_map, prep, team_games_of)
from challenger.guard import REGULAR_SQL, season_of_one              # noqa: E402

COLUMNS = ("id, person_id, player_team, opponent_team, is_home, tip, stat_type, line, status, n_prior, "
           "mu, alpha, p_over, expected_minutes, rate_per_minute, features_json")   # no outcome columns


def features_for_context_before_fix(conn, person_id: int, ctx: dict):
    """inference.features_for_context exactly as it was at 431af4b (before step 0.9)."""
    _rows = inference._rows
    tip, game_id = ctx["tip"], ctx.get("game_id", 0)
    hist = _rows(conn, "personId = ? AND gameDateTimeEst < ?", (int(person_id), tip))
    times = sorted(hist["gameDateTimeEst"])
    team_from = times[-11] if len(times) >= 11 else "0000"
    team_rows = _rows(conn, "gameDateTimeEst >= ? AND gameDateTimeEst < ?", (team_from, tip))
    lg = conn.execute(f"SELECT gameDateTimeEst FROM (SELECT DISTINCT gameId, gameDateTimeEst FROM player_stats "
                      f"WHERE {REGULAR_SQL} AND gameDateTimeEst < ?) ORDER BY gameDateTimeEst DESC "
                      "LIMIT 1 OFFSET ?", (tip, LEAGUE_TEAM_GAMES)).fetchone()
    names = [k for k, v in load_team_map(conn).items() if v == ctx["opp"]]
    by_name = "".join(" OR (opponentteamId IS NULL AND opponentteamCity = ? AND opponentteamName = ?)"
                      for _ in names)
    op = conn.execute(f"SELECT gameDateTimeEst FROM (SELECT DISTINCT gameId, gameDateTimeEst FROM player_stats "
                      f"WHERE {REGULAR_SQL} AND (opponentteamId = ?{by_name}) AND gameDateTimeEst < ?) "
                      "ORDER BY gameDateTimeEst DESC LIMIT 1 OFFSET ?",
                      (ctx["opp"], *[x for k in names for x in k], tip, OPP_GAMES)).fetchone()
    allowed_from = min(lg[0] if lg else "0000", op[0] if op else "0000")
    league = _rows(conn, "gameDateTimeEst >= ? AND gameDateTimeEst < ?", (allowed_from, tip))

    team_map = load_team_map(conn)
    hist, league, team_rows = prep(hist, team_map), prep(league, team_map), prep(team_rows, team_map)
    t = pd.Timestamp(tip)
    target = pd.DataFrame({"personId": [int(person_id)], "gameId": [int(game_id)], "gameDateTimeEst": [tip],
                           "home": [ctx["home"]], "playerteamId": [ctx["team"]],
                           "opponentteamId": [ctx["opp"]], "t": [t], "season": [season_of_one(t)]})
    for c in ("personId", "gameId", "home", "playerteamId", "opponentteamId"):
        target[c] = target[c].astype("int64")
    F = build_features(hist, target, team_games_of(team_rows), allowed_table(league))
    ctx = {**ctx, "data_as_of": str(hist["t"].max()) if len(hist) else None}
    return F, ctx


def frames_identical(a: pd.DataFrame, b: pd.DataFrame) -> bool:
    """Same columns, dtypes and values bit for bit (NaN equals NaN)."""
    if list(a.columns) != list(b.columns) or a.shape != b.shape or list(a.dtypes) != list(b.dtypes):
        return False
    for c in a.columns:
        x, y = a[c].to_numpy(), b[c].to_numpy()
        if x.dtype.kind == "f":
            if not np.array_equal(x.view(f"i{x.itemsize}"), y.view(f"i{y.itemsize}")):   # bitwise, NaN == NaN
                return False
        elif not (x == y).all():
            return False
    return True


def _pred(F, stat, line, m):
    r = live.predict_from_features(F, stat, [line], m)
    if r["status"] != "ok":
        return {"status": r["status"], "n_prior": r["n_prior"]}
    return {"status": "ok", "n_prior": r["n_prior"], "mu": r["mu"], "alpha": r["alpha"],
            "p_over": r["p_over"][float(line)], "expected_minutes": r["expected_minutes"], "rate": r["rate"]}


def compare(conn, person_id, ctx, stats_lines, m, timings=None):
    """Old vs new for one context; returns (features identical, [(stat, line, old, new)])."""
    t0 = time.perf_counter()
    F_old, c_old = features_for_context_before_fix(conn, person_id, ctx)
    t1 = time.perf_counter()
    F_new, c_new = inference.features_for_context(conn, person_id, ctx)
    t2 = time.perf_counter()
    if timings is not None:
        timings.append((t1 - t0, t2 - t1))
    same = frames_identical(F_old, F_new) and c_old["data_as_of"] == c_new["data_as_of"]
    return same, [(s, L, _pred(F_old, s, L, m), _pred(F_new, s, L, m)) for s, L in stats_lines]


def stored_rows(conn) -> list[dict]:
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f"SELECT {COLUMNS} FROM challenger_predictions "
                                              "WHERE person_id IS NOT NULL ORDER BY id")]
    finally:
        conn.row_factory = None


def short_history_sample(conn, n: int, tip: str, seed: int = 7, nba_opponents: bool = False) -> list[tuple[int, int, int]]:
    """(person_id, team, opp) for up to n players with 1-10 played regular-season games
    before tip, team = his latest team id, opp = another team, chosen with a fixed seed.
    By default opp is any team id in the box scores, including exhibition opponents with
    fewer than OPP_GAMES games (whose league window then starts at '0000'); with
    nba_opponents, only the teams of the current schedule."""
    rows = conn.execute(f"SELECT personId, COUNT(*) FROM player_stats WHERE {REGULAR_SQL} AND gameDateTimeEst < ? "
                        "GROUP BY personId HAVING COUNT(*) BETWEEN 1 AND 10", (tip,)).fetchall()
    rng = random.Random(seed)
    rng.shuffle(rows)
    teams = (sorted({r[0] for r in conn.execute("SELECT homeTeamId FROM schedule")}) if nba_opponents
             else sorted(set(load_team_map(conn).values())))
    out = []
    for pid, _ in rows:
        r = conn.execute(f"SELECT playerteamId FROM player_stats WHERE personId = ? AND {REGULAR_SQL} "
                         "AND playerteamId IS NOT NULL AND gameDateTimeEst < ? ORDER BY gameDateTimeEst DESC LIMIT 1",
                         (pid, tip)).fetchone()
        if r is None:
            continue
        team = int(r[0])
        opp = rng.choice([t for t in teams if t != team])
        out.append((int(pid), team, opp))
        if len(out) >= n:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="a backup-API copy of predictions.db")
    ap.add_argument("--sample", type=int, default=40)
    ap.add_argument("--tip", default="2026-10-27 19:30:00", help="tip for the short-history sample (ET)")
    ap.add_argument("--out")
    a = ap.parse_args()

    sha_before = cm.sha256(live.FROZEN["path"])
    m = live.load_frozen()
    conn = sqlite3.connect(f"file:{os.path.abspath(a.db)}?mode=ro", uri=True)
    ids = live.team_ids(conn)
    res = {"db": os.path.abspath(a.db), "run_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "model_sha256_before": sha_before}

    # 1. every stored row, grouped by context (one feature build per player per pull)
    rows = stored_rows(conn)
    groups: dict = {}
    for r in rows:
        t, o = ids.get(live.nickname(r["player_team"])), ids.get(live.nickname(r["opponent_team"]))
        key = (r["person_id"], t, o, int(r["is_home"]), r["tip"])
        groups.setdefault(key, []).append(r)
    n_rows = n_pred_same = n_feat_ctx_same = 0
    stored_vs_new = {"mu_equal": 0, "p_over_equal": 0, "compared": 0}
    mismatches, timings = [], []
    for (pid, team, opp, home, tip), grp in groups.items():
        ctx = {"tip": tip, "home": home, "team": team, "opp": opp, "game_id": 0}
        same, preds = compare(conn, pid, ctx, [(r["stat_type"], r["line"]) for r in grp], m, timings)
        n_feat_ctx_same += same
        for r, (s, L, old, new) in zip(grp, preds):
            n_rows += 1
            if old == new:
                n_pred_same += 1
            else:
                mismatches.append({"id": r["id"], "old": old, "new": new})
            if r["status"] == "ok" and new["status"] == "ok":
                stored_vs_new["compared"] += 1
                stored_vs_new["mu_equal"] += r["mu"] == new["mu"]
                stored_vs_new["p_over_equal"] += r["p_over"] == new["p_over"]
        print(f"  person {pid} @ {tip}: features {'identical' if same else 'DIFFER'}, {len(grp)} rows", flush=True)
    res["stored_rows"] = {"rows": n_rows, "contexts": len(groups), "contexts_features_identical": n_feat_ctx_same,
                          "rows_prediction_identical": n_pred_same, "mismatches": mismatches,
                          "seconds_old_new_total": [round(sum(x[0] for x in timings), 1),
                                                    round(sum(x[1] for x in timings), 1)],
                          "recomputed_vs_stored (not a criterion)": stored_vs_new}

    # 2. short-history sample
    sample = short_history_sample(conn, a.sample, a.tip)
    s_same = s_pred = 0
    s_mis, s_tim = [], []
    stats_lines = [("Points", 9.5), ("Rebounds", 3.5), ("Assists", 1.5), ("3PM", 0.5)]
    for pid, team, opp in sample:
        ctx = {"tip": a.tip, "home": pid % 2, "team": team, "opp": opp, "game_id": 0}
        same, preds = compare(conn, pid, ctx, stats_lines, m, s_tim)
        s_same += same
        for s, L, old, new in preds:
            s_pred += old == new
            if old != new:
                s_mis.append({"person_id": pid, "stat": s, "old": old, "new": new})
        print(f"  short-history {pid}: features {'identical' if same else 'DIFFER'}", flush=True)
    res["short_history_sample"] = {"players": len(sample), "features_identical": s_same,
                                   "predictions": len(sample) * len(stats_lines), "predictions_identical": s_pred,
                                   "mismatches": s_mis,
                                   "seconds_old_new_total": [round(sum(x[0] for x in s_tim), 1),
                                                             round(sum(x[1] for x in s_tim), 1)]}
    conn.close()
    res["model_sha256_after"] = cm.sha256(live.FROZEN["path"])
    res["model_sha256_unchanged"] = res["model_sha256_before"] == res["model_sha256_after"] == live.FROZEN["sha256"]
    res["pass"] = (not mismatches and not s_mis and n_feat_ctx_same == len(groups) and s_same == len(sample)
                   and res["model_sha256_unchanged"])
    print(json.dumps({k: v for k, v in res.items() if k not in ("stored_rows", "short_history_sample")}, indent=1))
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "mismatches"}
                      for k, v in res.items() if k in ("stored_rows", "short_history_sample")}, indent=1))
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=1, default=str)
    sys.exit(0 if res["pass"] else 1)


if __name__ == "__main__":
    main()
