"""
retro_challenger.py  (exploratory; not pre-registered)

Re-scores this season's frozen app picks with the challenger, after the fact, using only
data from before each game's tip. For every current, resolved (OVER/UNDER), non-limited
app prediction with model 'frozen', game_date >= SEASON_START, NBA Points / Rebounds /
Assists / 3PM:

  player     database.resolve_person_id (the resolver /predict and the screenshot table use)
  game       the player's box-score row on game_date (counted game types) against the stored
             opponent: its tip time, team and home flag (all known before tip). The stats in
             that row are used only for grading.
  features   challenger.live.features -> inference.features_for_context: rows that tipped
             before the game, the same path as live inference
  model      the frozen challenger file, SHA-256 and fingerprint checked (live.load_frozen);
             never refitted
  row        a separate app prediction: model 'challenger_retro', retro_of = the frozen row's
             id, the same line, graded OVER/UNDER from the same box score (a push is skipped)

Retro rows are excluded from the default History stats and from player track records.
Idempotent: a frozen row that already has a retro row is not re-scored (its stored retro
row is reported). Runs database.init_db() first (the migrations a backend restart runs) and
backs up predictions.db to backups/ before writing. Never reads paper_predictions.

Writes experiments/results/retro_challenger_<ts>.md and .json.

    python experiments/retro_challenger.py          (from backend/)
"""

import datetime as dt
import json
import os
import sqlite3
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(HERE, "..")
sys.path.insert(0, BACKEND)

import database                                    # noqa: E402
import game_types                                  # noqa: E402
from challenger import live                        # noqa: E402

SEASON_START = "2026-10-20"
STAT_COLS = {"Points": "points", "Rebounds": "reboundsTotal", "Assists": "assists", "3PM": "threePointersMade"}
OUT_DIR = os.path.join(HERE, "results")
N_BOOT, SEED = 2000, 20261025
EPS = 1e-6

ELIGIBLE_SQL = f"""
    SELECT id, player_name, stat_type, stat_line, opponent_team, predicted_outcome, confidence,
           actual_result, game_date
    FROM predictions
    WHERE COALESCE(is_current, 1) = 1 AND actual_result IN ('OVER', 'UNDER')
      AND COALESCE(is_limited_data, 0) = 0 AND COALESCE(model, 'frozen') = 'frozen'
      AND game_date >= ? AND stat_type IN ({', '.join(f"'{s}'" for s in STAT_COLS)})
      AND COALESCE(league, 'NBA') = 'NBA'
    ORDER BY game_date, player_name, stat_type
"""


def backup(path: str) -> str:
    os.makedirs(os.path.join(BACKEND, "backups"), exist_ok=True)
    dst = os.path.join(BACKEND, "backups", f"predictions_pre_retro_{dt.datetime.now():%Y%m%dT%H%M%S}.db")
    src = sqlite3.connect(path)
    out = sqlite3.connect(dst)
    src.backup(out)
    out.close()
    src.close()
    return dst


def box_row(conn, person_id: int, game_date: str, opponent: str):
    nick = live.nickname(opponent)
    return conn.execute(
        f"SELECT gameId, gameDateTimeEst, playerteamName, opponentteamName, home, numMinutes, "
        f"{', '.join(STAT_COLS.values())} FROM player_stats WHERE personId = ? "
        f"AND substr(gameDateTimeEst, 1, 10) = ? AND opponentteamName = ? "
        f"AND gameType IN ({game_types.COUNTED_SQL})", (person_id, game_date, nick)).fetchone()


def run(conn, m) -> tuple[list[dict], dict]:
    """(records for every compared pick, counts of skipped picks by reason)."""
    eligible = conn.execute(ELIGIBLE_SQL, (SEASON_START,)).fetchall()
    existing = {r["retro_of"]: r for r in conn.execute(
        "SELECT * FROM predictions WHERE model = 'challenger_retro' AND retro_of IS NOT NULL")}
    skipped, records, pending = {}, [], []
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    feats = {}                                         # (person, game) -> feature row
    for r in eligible:
        def skip(why):
            skipped[why] = skipped.get(why, 0) + 1
        pid = database.resolve_person_id(conn, r["player_name"], r["game_date"], r["opponent_team"])
        if pid is None:
            skip("player not resolved to one personId")
            continue
        box = box_row(conn, pid, r["game_date"], r["opponent_team"])
        if box is None:
            skip("no box score against the stored opponent")
            continue
        actual = box[STAT_COLS[r["stat_type"]]]
        line = float(r["stat_line"])
        if actual is None or float(actual) == line:
            skip("push or missing stat")
            continue
        graded = "OVER" if float(actual) > line else "UNDER"
        p_frozen = r["confidence"] if r["predicted_outcome"] == "OVER" else 1 - r["confidence"]
        rec = {"frozen_id": r["id"], "player": r["player_name"], "person_id": pid, "stat": r["stat_type"],
               "line": line, "game_date": r["game_date"], "game_id": int(box["gameId"]), "actual": float(actual),
               "result": graded, "frozen_result_stored": r["actual_result"],
               "frozen_pick": r["predicted_outcome"], "p_frozen": float(p_frozen)}
        if r["id"] in existing:
            e = existing[r["id"]]
            p_ch = e["confidence"] if e["predicted_outcome"] == "OVER" else 1 - e["confidence"]
            records.append({**rec, "challenger_pick": e["predicted_outcome"], "p_challenger": float(p_ch)})
            continue
        key = (pid, int(box["gameId"]))
        if key not in feats:
            try:
                feats[key] = live.features(conn, pid, box["playerteamName"], box["opponentteamName"],
                                           bool(box["home"]), box["gameDateTimeEst"])
            except Exception as exc:
                feats[key] = exc
        if isinstance(feats[key], Exception):
            skip(f"feature error: {type(feats[key]).__name__}")
            continue
        F, _ = feats[key]
        pred = live.predict_from_features(F, r["stat_type"], [line], m)
        if pred["status"] != "ok":
            skip(pred["status"])
            continue
        p = pred["p_over"][line]
        pick = "OVER" if p > 0.5 else "UNDER"
        records.append({**rec, "challenger_pick": pick, "p_challenger": p})
        pending.append((now, r["player_name"], r["stat_type"], line, r["opponent_team"], pick,
                        round(max(p, 1 - p), 4),
                        live.explanation(r["player_name"], r["stat_type"], line, pred, r["opponent_team"]),
                        graded, r["game_date"], "NBA", 0, "challenger_retro", 1, r["id"]))
    conn.executemany(
        "INSERT INTO predictions (timestamp, player_name, stat_type, stat_line, opponent_team, predicted_outcome, "
        "confidence, explanation, actual_result, game_date, league, is_limited_data, model, is_current, retro_of) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", pending)
    database.refresh_current(conn)
    conn.commit()
    skipped["already scored (reported from the stored row)"] = sum(r["id"] in existing for r in eligible)
    skipped["eligible"] = len(eligible)
    skipped["inserted"] = len(pending)
    return records, skipped


# --- metrics ------------------------------------------------------------------------------

def _boot(games, values, fn, n=N_BOOT, seed=SEED):
    """Game-clustered bootstrap percentile 95% CI of fn over resampled games."""
    ug = np.unique(games)
    idx = {g: np.where(games == g)[0] for g in ug}
    rng = np.random.default_rng(seed)
    stats = []
    for _ in range(n):
        pick = rng.choice(ug, len(ug), replace=True)
        rows = np.concatenate([idx[g] for g in pick])
        stats.append(fn(*(v[rows] for v in values)))
    return [round(float(np.percentile(stats, 2.5)), 4), round(float(np.percentile(stats, 97.5)), 4)]


def summarize(recs: list[dict], boot: bool = True) -> dict:
    if not recs:
        return {"n": 0}
    over = np.array([r["result"] == "OVER" for r in recs], dtype=float)
    pc = np.clip(np.array([r["p_challenger"] for r in recs]), EPS, 1 - EPS)
    pf = np.clip(np.array([r["p_frozen"] for r in recs]), EPS, 1 - EPS)
    cc = np.array([r["challenger_pick"] == r["result"] for r in recs], dtype=float)
    fc = np.array([r["frozen_pick"] == r["result"] for r in recs], dtype=float)
    games = np.array([r["game_id"] for r in recs])
    agree = np.array([r["challenger_pick"] == r["frozen_pick"] for r in recs])
    brier = lambda p, y: float(np.mean((p - y) ** 2))
    logloss = lambda p, y: float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))
    out = {"n": len(recs), "games": int(len(np.unique(games))),
           "accuracy": {"challenger": round(float(cc.mean()), 4), "frozen": round(float(fc.mean()), 4),
                        "challenger_minus_frozen": round(float(cc.mean() - fc.mean()), 4)},
           "brier": {"challenger": round(brier(pc, over), 4), "frozen": round(brier(pf, over), 4)},
           "logloss": {"challenger": round(logloss(pc, over), 4), "frozen": round(logloss(pf, over), 4)},
           "agree": {"n": int(agree.sum()), "rate": round(float(agree.mean()), 4),
                     "accuracy_when_agree": round(float(cc[agree].mean()), 4) if agree.any() else None},
           "disagree": {"n": int((~agree).sum()), "challenger_right": int(cc[~agree].sum()),
                        "frozen_right": int(fc[~agree].sum())},
           "over_rate": round(float(over.mean()), 4)}
    if boot:
        out["accuracy_ci95"] = {"challenger": _boot(games, [cc], np.mean), "frozen": _boot(games, [fc], np.mean),
                                "challenger_minus_frozen": _boot(games, [cc, fc], lambda a, b: a.mean() - b.mean())}
        out["brier_diff_ci95"] = _boot(games, [pc, pf, over],
                                       lambda a, b, y: np.mean((a - y) ** 2) - np.mean((b - y) ** 2))
    return out


def calibration(recs, key, bins=10):
    p = np.array([r[key] for r in recs])
    y = np.array([r["result"] == "OVER" for r in recs], dtype=float)
    idx = np.minimum((p * bins).astype(int), bins - 1)
    return [{"bin": f"{b / bins:.1f}-{(b + 1) / bins:.1f}", "mean_p": round(float(p[idx == b].mean()), 3),
             "observed_over": round(float(y[idx == b].mean()), 3), "n": int((idx == b).sum())}
            for b in range(bins) if (idx == b).any()]


def write_md(res: dict, path: str) -> None:
    a = res["all"]
    ci = a["accuracy_ci95"]
    L = ["# Retro comparison: challenger vs frozen on this season's app picks (exploratory)", "",
         f"Generated {res['generated_utc']}. Not pre-registered. Current, resolved, non-limited frozen app picks, "
         f"game_date >= {SEASON_START}, NBA Points/Rebounds/Assists/3PM; challenger re-scored from data before each "
         f"tip with the frozen file (SHA-256 and fingerprint checked). Skips: {res['skipped']}.", "",
         f"Picks compared: **{a['n']:,}** over {a['games']} games. Over rate {a['over_rate']:.3f}. "
         f"Graded results disagreeing with the stored frozen result: {res['grade_mismatches']}.", "",
         "| | challenger | frozen | challenger − frozen |", "|---|---|---|---|",
         f"| accuracy [95% CI, game-clustered] | {a['accuracy']['challenger']:.4f} [{ci['challenger'][0]:.4f}, "
         f"{ci['challenger'][1]:.4f}] | {a['accuracy']['frozen']:.4f} [{ci['frozen'][0]:.4f}, {ci['frozen'][1]:.4f}] | "
         f"{a['accuracy']['challenger_minus_frozen']:+.4f} [{ci['challenger_minus_frozen'][0]:+.4f}, "
         f"{ci['challenger_minus_frozen'][1]:+.4f}] |",
         f"| Brier | {a['brier']['challenger']:.4f} | {a['brier']['frozen']:.4f} | "
         f"{a['brier']['challenger'] - a['brier']['frozen']:+.4f} [{a['brier_diff_ci95'][0]:+.4f}, "
         f"{a['brier_diff_ci95'][1]:+.4f}] |",
         f"| log loss | {a['logloss']['challenger']:.4f} | {a['logloss']['frozen']:.4f} | "
         f"{a['logloss']['challenger'] - a['logloss']['frozen']:+.4f} |", "",
         f"Agree on {a['agree']['n']:,} of {a['n']:,} picks ({a['agree']['rate']:.1%}); accuracy when they agree "
         f"{a['agree']['accuracy_when_agree']:.4f}. Disagree on {a['disagree']['n']:,}: challenger right "
         f"{a['disagree']['challenger_right']:,}, frozen right {a['disagree']['frozen_right']:,}.", "",
         "## By stat", "",
         "| stat | n | acc challenger [CI] | acc frozen [CI] | Brier ch / fr | log loss ch / fr | agree | "
         "disagree: ch right / fr right |", "|---|---|---|---|---|---|---|---|"]
    for s, x in res["by_stat"].items():
        c = x["accuracy_ci95"]
        L.append(f"| {s} | {x['n']:,} | {x['accuracy']['challenger']:.3f} [{c['challenger'][0]:.3f}, {c['challenger'][1]:.3f}] | "
                 f"{x['accuracy']['frozen']:.3f} [{c['frozen'][0]:.3f}, {c['frozen'][1]:.3f}] | "
                 f"{x['brier']['challenger']:.4f} / {x['brier']['frozen']:.4f} | "
                 f"{x['logloss']['challenger']:.4f} / {x['logloss']['frozen']:.4f} | {x['agree']['rate']:.1%} | "
                 f"{x['disagree']['challenger_right']} / {x['disagree']['frozen_right']} |")
    L += ["", "## Calibration of P(over) (mean predicted / observed over rate / n)", "",
          "| bin | challenger | frozen |", "|---|---|---|"]
    cal = {k: {b["bin"]: b for b in v} for k, v in res["calibration"].items()}
    for bn in sorted(set(cal["challenger"]) | set(cal["frozen"])):
        cells = [f"{cal[k][bn]['mean_p']:.3f} / {cal[k][bn]['observed_over']:.3f} / {cal[k][bn]['n']}"
                 if bn in cal[k] else "—" for k in ("challenger", "frozen")]
        L.append(f"| {bn} | {cells[0]} | {cells[1]} |")
    L += ["", "Frozen P(over) is the stored confidence of its pick (1 − confidence for an UNDER pick).",
          f"Model file SHA-256 `{live.FROZEN['sha256']}`, fingerprint `{live.FROZEN['fingerprint']}`. "
          f"Run time {res['seconds']} s."]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


def main() -> int:
    t0 = time.perf_counter()
    database.init_db()
    bk = backup(database.DB_PATH)
    print(f"backed up predictions.db -> {bk}", flush=True)
    m = live.load_frozen()
    conn = database.get_connection()
    try:
        recs, skipped = run(conn, m)
    finally:
        conn.close()
    res = {"generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "backup": bk, "skipped": skipped, "all": summarize(recs),
           "grade_mismatches": sum(r["result"] != r["frozen_result_stored"] for r in recs),
           "by_stat": {s: summarize([r for r in recs if r["stat"] == s]) for s in STAT_COLS},
           "calibration": {"challenger": calibration(recs, "p_challenger"), "frozen": calibration(recs, "p_frozen")},
           "seconds": round(time.perf_counter() - t0, 1)}
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    path = os.path.join(OUT_DIR, f"retro_challenger_{stamp}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({**res, "records": recs}, f, indent=1, default=str)
    write_md(res, path.replace(".json", ".md"))
    print(f"wrote {path} (+ .md); {res['all']['n']} picks compared; skipped {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
