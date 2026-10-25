"""
stage2.py  (challenger, Stage 2: the pre-registered one-shot test on 2025-26)

Pre-registration: experiments/preregistration.md, entry "challenger Stage 2, one-shot test
on 2025-26" (commit a760c8c). Run ONCE. Nothing is fitted except baseline (a)'s NB alpha
(last-10 average, MLE on 2012-13..2024-25 training rows), as the entry specifies.

  models   the frozen files, loaded with their SHA-256 checked, and their content
           fingerprints checked against the entry; never refitted
  rows     2025-26 regular season, played, >= 5 earlier games; four stats per player-game
  primary  mean per-row NB log-lik, challenger - baseline (b), pooled over the four stats;
           game-clustered bootstrap (2,000, seed 20261024), pass iff the 95% CI lower bound > 0
  also     per-stat / per-experience differences, challenger - (a), MAE, calibration at five
           lines with reliability tables, a challenger_predict spot check on 50 scored rows,
           Kaggle version, row/game counts, rows dropped for missing team ids

Writes experiments/results/challenger_stage2_<ts>.json (first) and .md.

    python -m challenger.stage2          (from backend/)
"""

import datetime as dt
import json
import os
import sys
import time

import numpy as np
import pandas as pd

from . import model as cm
from .features import STAT_COLS, batch_features, load_all, load_team_map
from .guard import DATA_END, FORBIDDEN_SEASONS, REGULAR_SQL, assert_dev_only
from .inference import DB, MODEL_DIR, _connect, challenger_predict
from .stage1 import MIN_PRIOR, TRAIN_FIRST, boot_diff, evaluate, seasons_between

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "experiments"))
import per_player_models as ppm                      # noqa: E402

OUT_DIR = os.path.join(HERE, "..", "experiments", "results")
TEST_SEASON, TRAIN_LAST = "2025-26", "2024-25"
CHALLENGER = {"path": os.path.join(MODEL_DIR, "challenger_stage2_frozen.pkl"),
              "sha256": "0e4fa88fee927f70d8b2a6679310f36b5f12b099d1861266cab11ae53723d604",
              "fingerprint": "d18451fedc40b51e0c7d29b50ba07515ec6b935bd4597d63b2ffee913b81ee7f"}
BASELINE_B = {"path": os.path.join(MODEL_DIR, "baseline_b_stage2_frozen.pkl"),
              "sha256": "59f96ad61a1d900184229995770ff7102d617c86bd5e7a423ea2e0dd3bcd0370",
              "fingerprint": "e2b7233d06e8b8a513ae8c3e2a46f2c9817810fd0ba61ac749540dcb406f7bd1"}
N_SPOT, SPOT_SEED = 50, 20261024
MODELS = ["challenger", "b_pooled_hgb", "a_last10"]
LINE_TYPES = ["median", "median-2", "median+2", "rung below", "rung above"]


def merged(df):
    F = batch_features(df)
    assert (F["gameId"].values == df["gameId"].values).all() and (F["personId"].values == df["personId"].values).all()
    Fx = F.drop(columns=["personId", "gameId", "t", "season"])
    return pd.concat([df.drop(columns=[c for c in Fx.columns if c in df.columns]), Fx], axis=1)


def score(D, bfeat, ch, bb, test_season, train_last):
    """All metrics on test_season's scored rows. Only (a)'s alpha is fitted (training rows)."""
    tr = D[seasons_between(D["season"], TRAIN_FIRST, train_last) & (D["n_prior"] >= 1)]
    ev = D[(D["season"] == test_season) & (D["n_prior"] >= MIN_PRIOR)]
    assert set(tr["season"]) <= {s for s in set(D["season"]) if s <= train_last}
    alpha_a = {s: cm.nb_alpha_mle(tr[c].values, tr[f"avg10_{c}"].values) for s, c in STAT_COLS.items()}
    emin = ch.predict_minutes(ev)
    results, pooled_rows, mus = {}, [], {}
    for s, col in STAT_COLS.items():
        preds = {"challenger": ch.predict(ev, s, emin)["mu"],
                 "b_pooled_hgb": np.clip(bb["models"][s].predict(bfeat[s].loc[ev.index, ppm.FEATS]), cm.MU_FLOOR, None),
                 "a_last10": np.clip(ev[f"avg10_{col}"].values, cm.MU_FLOOR, None)}
        alphas = {"challenger": ch.alpha[s], "b_pooled_hgb": bb["alpha"][s], "a_last10": alpha_a[s]}
        y = ev[col].values
        llc, llb, lla = (cm.nb_logpmf(y, preds[k], alphas[k]) for k in MODELS)
        r = {"all": evaluate(ev, ev, preds, alphas, s), "by_experience": {},
             "boot_ll_challenger_minus_b": boot_diff(ev["gameId"].values, llc - llb),
             "boot_ll_challenger_minus_a": boot_diff(ev["gameId"].values, llc - lla)}
        exp = ev["experience"].values
        for name, m in (("rookie", exp == 0), ("1-3", (exp >= 1) & (exp <= 3)), ("4+", exp >= 4)):
            r["by_experience"][name] = evaluate(ev[m], ev[m], {k: v[m] for k, v in preds.items()}, alphas, s)
            r["by_experience"][name]["boot_ll_challenger_minus_b"] = boot_diff(ev["gameId"].values[m], (llc - llb)[m])
        results[s] = r
        mus[s] = preds["challenger"]
        pooled_rows.append(pd.DataFrame({"g": ev["gameId"].values, "d": llc - llb, "da": llc - lla}))
    pr = pd.concat(pooled_rows)
    pooled = {"challenger_minus_b": boot_diff(pr["g"].values, pr["d"].values),
              "challenger_minus_a": boot_diff(pr["g"].values, pr["da"].values)}
    return ev, results, pooled, alpha_a, mus


def spot_check(ev, mus, ch, conn_path, model):
    rng = np.random.default_rng(SPOT_SEED)
    pick = rng.choice(len(ev), N_SPOT, replace=False)
    stats = list(STAT_COLS)
    out = []
    for k, i in enumerate(pick):
        s = stats[k % 4]
        row = ev.iloc[i]
        line = row[f"median20_{STAT_COLS[s]}"]
        line = 0.5 if pd.isna(line) else float(line) + 0.5
        want = float(cm.p_over(mus[s][i], ch.alpha[s], line))
        got = challenger_predict(int(row["personId"]), s, int(row["gameId"]), line, db_path=conn_path,
                                 model_path=model["path"], expected_sha=model["sha256"])["p_over"]
        out.append({"personId": int(row["personId"]), "gameId": int(row["gameId"]), "stat": s, "line": line,
                    "batch": want, "inference": got, "equal": got == want})
    return {"rows": len(out), "equal": sum(o["equal"] for o in out),
            "max_abs_diff": max(abs(o["batch"] - o["inference"]) for o in out),
            "mismatches": [o for o in out if not o["equal"]]}


def main() -> int:
    timings = {}
    w0, c0 = time.perf_counter(), time.process_time()

    def lap(name, w, c):
        timings[name] = {"wall_s": round(time.perf_counter() - w, 1), "cpu_s": round(time.process_time() - c, 1)}
        print(f"  [{name}] {timings[name]}", flush=True)
        return time.perf_counter(), time.process_time()

    # frozen files: hash checked on load, fingerprint checked against the entry
    ch = cm.load(CHALLENGER["path"], CHALLENGER["sha256"])
    bb = cm.load(BASELINE_B["path"], BASELINE_B["sha256"])
    assert cm.fingerprint(ch) == CHALLENGER["fingerprint"], "challenger fingerprint differs from the entry"
    assert cm.fingerprint(bb) == BASELINE_B["fingerprint"], "baseline (b) fingerprint differs from the entry"
    assert ch.meta["train_seasons"] == [TRAIN_FIRST, TRAIN_LAST] and bb["meta"]["train_seasons"] == [TRAIN_FIRST, TRAIN_LAST]

    conn = _connect(DB)
    kv = conn.execute("SELECT * FROM kaggle_version").fetchall()
    kv_cols = [d[0] for d in conn.execute("SELECT * FROM kaggle_version LIMIT 0").description]
    tm = load_team_map(conn)
    nulls = conn.execute(f"SELECT playerteamCity, playerteamName, opponentteamCity, opponentteamName FROM player_stats "
                         f"WHERE {REGULAR_SQL} AND gameDateTimeEst >= '2025-07-01' AND gameDateTimeEst < ? "
                         "AND (playerteamId IS NULL OR opponentteamId IS NULL)", (DATA_END,)).fetchall()
    unfilled = sum(1 for pc, pn, oc, on in nulls if (pc, pn) not in tm or (oc, on) not in tm)
    df = load_all(conn)
    conn.close()
    assert_dev_only(df["t"], df["season"])
    assert not set(df["season"]) & FORBIDDEN_SEASONS and df["season"].max() == TEST_SEASON
    w, c = lap("load", w0, c0)
    D = merged(df)
    bfeat = {s: ppm.features(df, col) for s, col in STAT_COLS.items()}
    w, c = lap("features", w, c)
    ev, results, pooled, alpha_a, mus = score(D, bfeat, ch, bb, TEST_SEASON, TRAIN_LAST)
    w, c = lap("score", w, c)
    p = pooled["challenger_minus_b"]
    verdict = "PASS" if p["ci95"][0] > 0 else "FAIL"
    out = {"meta": {"generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    "preregistration_commit": "a760c8c", "data_end_exclusive": DATA_END,
                    "kaggle_version": [dict(zip(kv_cols, r)) for r in kv],
                    "rows_loaded": len(df), "max_date_loaded": str(df["t"].max()),
                    "test_rows_per_stat": len(ev), "test_games": int(ev["gameId"].nunique()),
                    "test_players": int(ev["personId"].nunique()),
                    "test_season_played_rows_all": int((D["season"] == TEST_SEASON).sum()),
                    "null_team_id_rows_2025_26": len(nulls), "null_team_id_rows_unfilled_dropped": unfilled,
                    "models": {"challenger": CHALLENGER, "baseline_b": BASELINE_B},
                    "alphas": {"challenger": ch.alpha, "baseline_b": bb["alpha"], "baseline_a": alpha_a}},
           "primary": {"metric": "mean NB log-lik per row, challenger - baseline (b), pooled over 4 stats",
                       **p, "verdict": verdict},
           "pooled": pooled, "results": results}
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    path = os.path.join(OUT_DIR, f"challenger_stage2_{stamp}.json")
    with open(path, "w", encoding="utf-8") as f:            # results saved before anything else runs
        json.dump(out, f, indent=1, default=str)
    print(f"PRIMARY: {verdict}  {p['mean']:+.5f} [{p['ci95'][0]:+.5f}, {p['ci95'][1]:+.5f}]", flush=True)
    out["spot_check"] = spot_check(ev, mus, ch, DB, CHALLENGER)
    w, c = lap("spot check", w, c)
    out["meta"]["timings"] = timings
    out["meta"]["cpu_logical_cores"] = os.cpu_count()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, default=str)
    write_md(out, path.replace(".json", ".md"))
    print(f"wrote {path} (+ .md)")
    return 0


# --- report -----------------------------------------------------------------------------

def _ci(b):
    return f"{b['mean']:+.4f} [{b['ci95'][0]:+.4f}, {b['ci95'][1]:+.4f}]"


def _bs(c):
    return "—" if not c.get("n") else f"{c['brier']:.4f} / {c['ece']:.4f}"


def write_md(r: dict, path: str) -> None:
    m, p = r["meta"], r["primary"]
    L = ["# Challenger Stage 2: one-shot test on 2025-26", "",
         f"Pre-registered in experiments/preregistration.md (commit {m['preregistration_commit']}). Computed once, "
         f"{m['generated_utc']}. Frozen files loaded with SHA-256 checked; content fingerprints match the entry; "
         "nothing refitted (only baseline (a)'s NB alpha is fitted, on 2012-13..2024-25, as the entry specifies).", "",
         f"- Kaggle version: {m['kaggle_version']}",
         f"- Loaded {m['rows_loaded']:,} played regular-season rows, max date {m['max_date_loaded']} "
         f"(guard: nothing on/after {m['data_end_exclusive']}; 2026-27 excluded).",
         f"- 2025-26: {m['test_season_played_rows_all']:,} played rows; scored (>= 5 earlier games) "
         f"{m['test_rows_per_stat']:,} rows per stat, {m['test_games']:,} games, {m['test_players']:,} players.",
         f"- 2025-26 rows with a NULL team id: {m['null_team_id_rows_2025_26']:,}; not fillable from (city, name), "
         f"dropped: {m['null_team_id_rows_unfilled_dropped']:,}.", "",
         "## Primary", "",
         f"Mean NB log-likelihood per row, challenger − baseline (b), pooled over Points, Rebounds, Assists, 3PM: "
         f"**{_ci(p)}** nats (game-clustered bootstrap, {p['games']:,} games, {p['rows']:,} rows, 2,000 resamples, "
         f"seed 20261024).", "",
         f"Pass rule: lower bound of the 95% CI > 0. **Verdict: {p['verdict']}.**", "",
         "Passing shows better stat forecasts than baseline (b). It is not evidence of an edge against the market; "
         "that requires a separate line-based test with its own entry.", "",
         "## Secondary (not decisive)", "",
         "### NB log-likelihood per row and MAE", "",
         "| stat | challenger LL | (b) LL | (a) LL | challenger − (b) [95% CI] | challenger − (a) [95% CI] | "
         "MAE challenger / (b) / (a) |", "|---|---|---|---|---|---|---|"]
    for s, x in r["results"].items():
        a = x["all"]
        L.append(f"| {s} | {a['challenger']['ll']:.4f} | {a['b_pooled_hgb']['ll']:.4f} | {a['a_last10']['ll']:.4f} | "
                 f"{_ci(x['boot_ll_challenger_minus_b'])} | {_ci(x['boot_ll_challenger_minus_a'])} | "
                 + " / ".join(f"{a[k]['mae']:.4f}" for k in MODELS) + " |")
    L += ["", f"Pooled challenger − (a): {_ci(r['pooled']['challenger_minus_a'])}.", "",
          "### By experience (prior NBA seasons)", "",
          "| stat | bucket | rows | LL challenger / (b) / (a) | challenger − (b) [95% CI] | MAE challenger / (b) / (a) |",
          "|---|---|---|---|---|---|"]
    for s, x in r["results"].items():
        for bk, e in x["by_experience"].items():
            L.append(f"| {s} | {bk} | {e['challenger']['rows']:,} | " + " / ".join(f"{e[k]['ll']:.4f}" for k in MODELS)
                     + f" | {_ci(e['boot_ll_challenger_minus_b'])} | " + " / ".join(f"{e[k]['mae']:.4f}" for k in MODELS) + " |")
    L += ["", "### Calibration of P(stat > L)", "",
          "Lines: median of the player's last 20 earlier games (min 5), median ± 2, nearest x4.5/x9.5 rungs below (≤) "
          "and above (>) the median; lines below 0 dropped; over = stat > L. ECE over 10 equal-width bins.", "",
          "| stat | line | n | over rate | challenger Brier / ECE | (b) Brier / ECE | (a) Brier / ECE |",
          "|---|---|---|---|---|---|---|"]
    for s, x in r["results"].items():
        for lt in LINE_TYPES:
            c = {k: x["all"][k]["lines"][lt] for k in MODELS}
            n = c["challenger"].get("n", 0)
            L.append(f"| {s} | {lt} | {n:,} | " + (f"{c['challenger']['base_rate']:.3f}" if n else "—") + " | "
                     + " | ".join(_bs(c[k]) for k in MODELS) + " |")
    L += ["", "Known from development: Points P(over) was over-confident above 0.7 (observed 0.60–0.73 on 2024-25); "
              "its 2025-26 reliability is the first table below.", ""]
    for lt in LINE_TYPES:
        L += [f"### Reliability, {lt} line (mean predicted P / observed over rate / n)", ""]
        for s, x in r["results"].items():
            L += [f"**{s}**", "", "| bin | challenger | (b) pooled HGB | (a) last-10 |", "|---|---|---|---|"]
            bins = sorted({b["bin"] for k in MODELS for b in x["all"][k]["lines"][lt].get("reliability", [])})
            for bn in bins:
                cells = []
                for k in MODELS:
                    e = [b for b in x["all"][k]["lines"][lt].get("reliability", []) if b["bin"] == bn]
                    cells.append(f"{e[0]['mean_p']:.3f} / {e[0]['observed']:.3f} / {e[0]['n']:,}" if e else "—")
                L.append(f"| {bn} | " + " | ".join(cells) + " |")
            L.append("")
    al = m["alphas"]
    L += ["### Other", "",
          "- NB alpha challenger / (b) / (a): " + ", ".join(
              f"{s} {al['challenger'][s]:.4f} / {al['baseline_b'][s]:.4f} / {al['baseline_a'][s]:.4f}" for s in al["challenger"]) + "."]
    sc = r.get("spot_check")
    if sc:
        L.append(f"- challenger_predict spot check (SQL before tip, frozen file with hash check) vs the batch scores: "
                 f"{sc['equal']} of {sc['rows']} identical, max |diff| {sc['max_abs_diff']:.2e}.")
    for k, v in m["models"].items():
        L.append(f"- {k}: backend/challenger/models/{os.path.basename(v['path'])}, SHA-256 `{v['sha256']}`, "
                 f"fingerprint `{v['fingerprint']}`.")
    if m.get("timings"):
        L += ["", "| phase | wall s | CPU s |", "|---|---|---|"]
        L += [f"| {k} | {v['wall_s']} | {v['cpu_s']} |" for k, v in m["timings"].items()]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


if __name__ == "__main__":
    sys.exit(main())
