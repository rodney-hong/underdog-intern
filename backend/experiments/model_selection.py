"""
model_selection.py
Step 4 of the 2026-27 model freeze: XGBoost vs logistic regression, using
ml_trainer's own data build, day-boundary cutoffs and training-only def-rating
fill. Never writes model files.

Selection rule (fixed before running):
  * Both models use fixed settings, no tuning:
      XGBoost  — exactly ml_trainer's (n_estimators=200, max_depth=4, lr=0.05, seed 42)
      LR       — StandardScaler + LogisticRegression(C=1.0, max_iter=2000), same 14 features
  * Temporal 70/15/15 on calendar-day boundaries. Per stat type, the model with the
    higher VALIDATION AUC wins (exact tie -> LR, the simpler model). The winner alone
    is then scored ONCE on test.
  * Monthly walk-forward, 2024-10 .. 2026-05: for each month, train on every row
    dated before the month, predict the month. Months with < 200 rows skipped.
  * Also reported: the 80/20 day-boundary test metrics ml_trainer.train_model now
    prints (XGBoost), for step 4.2.
Metrics: AUC (bootstrap 95% CI, 1000 resamples, seed 7), log loss, Brier.
Combo stats (PRA/PR/PA/RA) use SIMULATED lines; their numbers say nothing about
real-line performance.

    python experiments/model_selection.py [--stats Points,Rebounds] [--no-walk]
"""

import os
import sys
import json
import time
import argparse

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score, log_loss, brier_score_loss
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from xgboost import XGBClassifier

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import ml_trainer as M  # noqa: E402

OUT = os.path.join(HERE, "results_model_selection.json")
WALK_START, WALK_END = "2024-10", "2026-05"
N_BOOT, BOOT_SEED = 1000, 7


def make_xgb():
    return XGBClassifier(eval_metric="logloss", n_estimators=200, max_depth=4,
                         learning_rate=0.05, random_state=42)


def make_lr():
    return make_pipeline(StandardScaler(), LogisticRegression(C=1.0, max_iter=2000))


MODELS = {"xgb": make_xgb, "lr": make_lr}


def xy(d: pd.DataFrame, fill: float):
    X = M.fill_def_rating(d[M.FEATURE_COLS], fill).fillna(0).astype(float)
    return X, d["hit"].astype(int)


def metrics(y, p, boot: bool = True) -> dict:
    out = {"n": int(len(y)), "auc": float(roc_auc_score(y, p)),
           "logloss": float(log_loss(y, p, labels=[0, 1])), "brier": float(brier_score_loss(y, p)),
           "base_rate": float(np.mean(y))}
    if boot:
        rng = np.random.default_rng(BOOT_SEED)
        y, p = np.asarray(y), np.asarray(p)
        aucs = []
        for _ in range(N_BOOT):
            i = rng.integers(0, len(y), len(y))
            if y[i].min() != y[i].max():
                aucs.append(roc_auc_score(y[i], p[i]))
        out["auc_ci"] = [float(np.percentile(aucs, 2.5)), float(np.percentile(aucs, 97.5))]
    return out


def fit_predict(name, Xtr, ytr, Xte):
    m = MODELS[name]()
    m.fit(Xtr, ytr)
    return m.predict_proba(Xte)[:, 1]


def run_stat(stat: str, d: pd.DataFrame, daily_def, walk: bool) -> dict:
    d = d.sort_values("game_date").reset_index(drop=True)
    day = pd.to_datetime(d["game_date"]).dt.normalize()
    res = {"rows": int(len(d)), "date_range": [str(day.min().date()), str(day.max().date())]}

    # --- step 4.2: 80/20 day-boundary split, XGBoost (what train_model reports)
    (c80,) = M.date_cutoffs(d, (0.8,))
    tr = (day < c80).values
    X, y = xy(d, M.def_rating_mean(daily_def, before=c80))
    p = fit_predict("xgb", X[tr], y[tr], X[~tr])
    res["split_80_20"] = {"cutoff": str(c80.date()), "train": int(tr.sum()), "test": int((~tr).sum()),
                          "xgb_test": metrics(y[~tr], p)}

    # --- step 4.3a: 70/15/15
    c70, c85 = M.date_cutoffs(d, (0.70, 0.85))
    tr, va, te = (day < c70).values, ((day >= c70) & (day < c85)).values, (day >= c85).values
    X, y = xy(d, M.def_rating_mean(daily_def, before=c70))
    val = {name: metrics(y[va], fit_predict(name, X[tr], y[tr], X[va])) for name in MODELS}
    winner = "xgb" if val["xgb"]["auc"] > val["lr"]["auc"] else "lr"
    # winner refit on train+validation, scored once on test (fill from days < test start)
    X2, y2 = xy(d, M.def_rating_mean(daily_def, before=c85))
    trva = tr | va
    test = metrics(y2[te], fit_predict(winner, X2[trva], y2[trva], X2[te]))
    res["split_70_15_15"] = {"cutoffs": [str(c70.date()), str(c85.date())],
                             "rows": [int(tr.sum()), int(va.sum()), int(te.sum())],
                             "validation": val, "winner": winner, "winner_test": test}

    # --- step 4.3b: monthly walk-forward
    if walk:
        months = pd.period_range(WALK_START, WALK_END, freq="M")
        per_month, pooled = [], {k: ([], []) for k in MODELS}
        for mth in months:
            start = mth.start_time.normalize()
            end = (mth + 1).start_time.normalize()
            trm, tem = (day < start).values, ((day >= start) & (day < end)).values
            if tem.sum() < 200 or trm.sum() < 1000:
                continue
            Xm, ym = xy(d, M.def_rating_mean(daily_def, before=start))
            row = {"month": str(mth), "train": int(trm.sum()), "test": int(tem.sum())}
            for name in MODELS:
                pm = fit_predict(name, Xm[trm], ym[trm], Xm[tem])
                row[name] = metrics(ym[tem], pm, boot=False)["auc"]
                pooled[name][0].extend(ym[tem]); pooled[name][1].extend(pm)
            per_month.append(row)
        res["walk_forward"] = {
            "months": per_month,
            "pooled": {k: metrics(np.array(v[0]), np.array(v[1])) for k, v in pooled.items()},
            "xgb_wins_months": int(sum(r["xgb"] > r["lr"] for r in per_month)),
        }
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", default=",".join(M._ALL_MODEL_STATS))
    ap.add_argument("--no-walk", action="store_true")
    a = ap.parse_args()
    t0 = time.time()
    data = M.build_training_data()
    daily_def = data.attrs.get("daily_def")
    results = {"row_counts_after": {s: int((data["stat_type"] == s).sum()) for s in M._ALL_MODEL_STATS}}
    print("\nrows per stat after game-type filter:", results["row_counts_after"])
    for stat in a.stats.split(","):
        t = time.time()
        results[stat] = run_stat(stat, data[data["stat_type"] == stat], daily_def, not a.no_walk)
        r = results[stat]
        print(f"\n[{stat}] {time.time() - t:.0f}s  80/20 xgb test AUC {r['split_80_20']['xgb_test']['auc']:.4f}"
              f"  | 70/15/15 val xgb {r['split_70_15_15']['validation']['xgb']['auc']:.4f}"
              f" lr {r['split_70_15_15']['validation']['lr']['auc']:.4f} -> {r['split_70_15_15']['winner']}"
              f" test {r['split_70_15_15']['winner_test']['auc']:.4f}", flush=True)
        if "walk_forward" in r:
            w = r["walk_forward"]["pooled"]
            print(f"   walk-forward pooled AUC xgb {w['xgb']['auc']:.4f} lr {w['lr']['auc']:.4f};"
                  f" xgb wins {r['walk_forward']['xgb_wins_months']}/{len(r['walk_forward']['months'])} months",
                  flush=True)
        with open(OUT, "w") as f:
            json.dump(results, f, indent=1)
    print(f"\ndone in {time.time() - t0:.0f}s -> {OUT}")


if __name__ == "__main__":
    main()
