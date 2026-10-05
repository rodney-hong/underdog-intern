"""
baselines.py

Free baselines — no API calls. Answers: do simple models beat XGBoost on the
same rows?

For Points / Rebounds / Assists, on the FULL 20% temporal test split (every test
row, not just dev or eval), reports AUC with 95% bootstrap CIs for:

  - XGBoost              model_{stat}.pkl, as shipped
  - line_diff alone      the raw feature used directly as a score
  - LR(line_diff)        logistic regression fitted on the TRAINING split,
                         line_diff as the only predictor
  - LR(all 14, scaled)   logistic regression fitted on the TRAINING split,
                         StandardScaler + all 14 FEATURE_COLS

Both logistic regressions are fitted on the same training split train_model()
used, so nothing here sees test data during fitting.

Side effect: writes the two LR test-set probabilities back into
testset_{stat}.parquet as `lr_linediff_prob` and `lr_full_prob`, so jev_report.py
can show them alongside Jev and XGBoost. Row ids and the 14 features are
untouched, so the Jev cache stays valid.

Usage:
    python experiments/baselines.py
"""

from __future__ import annotations

import os
import sys

import joblib
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, BACKEND)
sys.path.insert(0, HERE)

import ml_trainer  # noqa: E402
from jev_report import N_BOOT, BOOT_SEED, bootstrap_auc_ci  # noqa: E402
from trainer_bridge import build_labelled_data_with_identity, test_split  # noqa: E402

STATS = ["Points", "Rebounds", "Assists"]


def fit_lr(train: pd.DataFrame, test: pd.DataFrame, cols: list[str], scale: bool):
    X_tr = train[cols].fillna(0).astype(float)
    X_te = test[cols].fillna(0).astype(float)
    y_tr = train["hit"].astype(int)

    lr = LogisticRegression(max_iter=2000)
    model = make_pipeline(StandardScaler(), lr) if scale else lr
    model.fit(X_tr, y_tr)
    return model.predict_proba(X_te)[:, 1]


def main() -> None:
    print("Rebuilding via ml_trainer.build_training_data()…\n")
    data = build_labelled_data_with_identity(STATS)

    rows = []
    for stat in STATS:
        ordered, split = test_split(data[data["stat_type"] == stat])
        ordered = ordered.reset_index(drop=False).rename(columns={"index": "row_id"})
        train = ordered.iloc[:split]
        test = ordered.iloc[split:].copy()

        y = test["hit"].to_numpy().astype(int)

        model = joblib.load(ml_trainer.model_path(stat))
        xgb_p = model.predict_proba(
            test[ml_trainer.FEATURE_COLS].fillna(0).astype(float)
        )[:, 1]

        lr_ld = fit_lr(train, test, ["line_diff"], scale=False)
        lr_full = fit_lr(train, test, list(ml_trainer.FEATURE_COLS), scale=True)

        scores = {
            "XGBoost": xgb_p,
            "line_diff alone": test["line_diff"].to_numpy(),
            "LR(line_diff)": lr_ld,
            "LR(all 14, scaled)": lr_full,
        }

        print(f"{'=' * 72}\n{stat}   train={split:,}  test={len(test):,}  "
              f"test hit rate={y.mean():.4f}\n{'=' * 72}")
        for name, p in scores.items():
            a = roc_auc_score(y, p)
            lo, hi = bootstrap_auc_ci(y, np.asarray(p))
            print(f"  {name:<20} AUC {a:.4f}  [{lo:.4f}, {hi:.4f}]")
            rows.append(
                {"stat": stat, "model": name, "auc": a, "ci_lo": lo, "ci_hi": hi,
                 "n_test": len(test)}
            )
        print()

        # Persist LR probabilities for jev_report.py.
        path = os.path.join(HERE, f"testset_{stat}.parquet")
        ts = pd.read_parquet(path)
        lr_map_ld = dict(zip(test["row_id"], lr_ld))
        lr_map_full = dict(zip(test["row_id"], lr_full))
        ts["lr_linediff_prob"] = ts["row_id"].map(lr_map_ld)
        ts["lr_full_prob"] = ts["row_id"].map(lr_map_full)
        if ts[["lr_linediff_prob", "lr_full_prob"]].isna().any().any():
            raise RuntimeError(f"{stat}: LR probabilities did not cover every test row.")
        ts.to_parquet(path, index=False)
        print(f"  wrote lr_linediff_prob / lr_full_prob -> {os.path.basename(path)}\n")

    print(f"{'=' * 72}\nSummary (bootstrap: {N_BOOT} resamples, seed {BOOT_SEED})\n{'=' * 72}")
    summary = pd.DataFrame(rows)
    print(summary.to_string(index=False, float_format=lambda v: f"{v:.4f}"))


if __name__ == "__main__":
    main()
