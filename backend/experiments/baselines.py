"""
baselines.py

Free baselines — no API calls. Answers: do simple models beat XGBoost on the
same rows, and does any confident slice clear the break-even hit rate?

For Points / Rebounds / Assists, on the FULL 20% temporal test split (every test
row, not just dev or eval):

  AUC with 95% bootstrap CIs for
    - XGBoost              model_{stat}.pkl, as shipped
    - line_diff alone      the raw feature used directly as a score
    - LR(line_diff)        logistic regression fitted on the TRAINING split,
                           line_diff as the only predictor
    - LR(all 14, scaled)   logistic regression fitted on the TRAINING split,
                           StandardScaler + all 14 FEATURE_COLS

  Accuracy at a 0.5 threshold for each model, next to an ALWAYS-UNDER baseline
  (always predict the stat stays at or below the line).

  A top-slice check: rank rows by the model's confidence in its own pick
  (|p - 0.5|, picking OVER when p > 0.5 else UNDER) and report hit rate with a
  95% bootstrap CI for the top 5% / 10% / 20%, against the 57.7% target.

Both logistic regressions are fitted on the same training split train_model()
used, so nothing here sees test data during fitting.

Side effect: writes the two LR test-set probabilities into
testset_{stat}.parquet as `lr_linediff_prob` and `lr_full_prob`, so
jev_report.py can show them alongside Jev and XGBoost. Row ids and the 14
features are untouched, so the Jev cache stays valid.

Usage:
    python experiments/baselines.py            # reuse LR probs in the parquets if present
    python experiments/baselines.py --refit    # force the full rebuild + refit
"""

from __future__ import annotations

import argparse
import os
import sys

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
TOP_SLICES = (0.05, 0.10, 0.20)
BREAK_EVEN = 0.577  # the hit rate a slice has to clear to be worth betting


def bootstrap_rate_ci(correct, n_boot=N_BOOT, seed=BOOT_SEED):
    """Percentile 95% CI for a hit rate over row resamples with replacement."""
    c = np.asarray(correct).astype(float)
    if len(c) == 0:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, len(c), (n_boot, len(c)))
    rates = c[draws].mean(axis=1)
    return float(np.percentile(rates, 2.5)), float(np.percentile(rates, 97.5))


def picks(y, p):
    """The model's own pick at a 0.5 threshold, its confidence, and whether it won."""
    p = np.asarray(p, dtype=float)
    pick_over = p > 0.5
    correct = np.where(pick_over, y == 1, y == 0).astype(int)
    confidence = np.abs(p - 0.5)
    return pick_over, confidence, correct


def fit_lr(train: pd.DataFrame, test: pd.DataFrame, cols: list[str], scale: bool):
    X_tr = train[cols].fillna(0).astype(float)
    X_te = test[cols].fillna(0).astype(float)
    y_tr = train["hit"].astype(int)

    lr = LogisticRegression(max_iter=2000)
    model = make_pipeline(StandardScaler(), lr) if scale else lr
    model.fit(X_tr, y_tr)
    return model.predict_proba(X_te)[:, 1]


def fit_and_write(stat: str, data: pd.DataFrame) -> None:
    """Fit both LRs on the training split and persist test-set probabilities."""
    ordered, split = test_split(data[data["stat_type"] == stat])
    ordered = ordered.reset_index(drop=False).rename(columns={"index": "row_id"})
    train, test = ordered.iloc[:split], ordered.iloc[split:].copy()

    lr_ld = fit_lr(train, test, ["line_diff"], scale=False)
    lr_full = fit_lr(train, test, list(ml_trainer.FEATURE_COLS), scale=True)

    path = os.path.join(HERE, f"testset_{stat}.parquet")
    ts = pd.read_parquet(path)
    ts["lr_linediff_prob"] = ts["row_id"].map(dict(zip(test["row_id"], lr_ld)))
    ts["lr_full_prob"] = ts["row_id"].map(dict(zip(test["row_id"], lr_full)))
    if ts[["lr_linediff_prob", "lr_full_prob"]].isna().any().any():
        raise RuntimeError(f"{stat}: LR probabilities did not cover every test row.")
    ts.to_parquet(path, index=False)
    print(f"  [{stat}] train={split:,} test={len(test):,} -> wrote LR probs")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--refit", action="store_true",
                    help="force the ml_trainer rebuild and refit the LR baselines")
    args = ap.parse_args()

    paths = {s: os.path.join(HERE, f"testset_{s}.parquet") for s in STATS}
    need = args.refit or any(
        not os.path.exists(p) or "lr_full_prob" not in pd.read_parquet(p).columns
        for p in paths.values()
    )
    if need:
        print("Rebuilding via ml_trainer.build_training_data()...\n")
        data = build_labelled_data_with_identity(STATS)
        for stat in STATS:
            fit_and_write(stat, data)
        print()
    else:
        print("Reusing LR probabilities already in testset_*.parquet "
              "(pass --refit to rebuild).\n")

    auc_rows, acc_rows, slice_rows = [], [], []

    for stat in STATS:
        ts = pd.read_parquet(paths[stat])
        y = ts["hit"].to_numpy().astype(int)
        models = {
            "XGBoost": ts["xgb_prob"].to_numpy(),
            "line_diff alone": ts["line_diff"].to_numpy(),
            "LR(line_diff)": ts["lr_linediff_prob"].to_numpy(),
            "LR(all 14, scaled)": ts["lr_full_prob"].to_numpy(),
        }

        bar = "=" * 76
        print(f"{bar}\n{stat}   test={len(ts):,}  test hit rate={y.mean():.4f}\n{bar}")

        print("  AUC (95% bootstrap CI)")
        for name, p in models.items():
            a = roc_auc_score(y, p)
            lo, hi = bootstrap_auc_ci(y, np.asarray(p))
            print(f"    {name:<20} {a:.4f}  [{lo:.4f}, {hi:.4f}]")
            auc_rows.append({"stat": stat, "model": name, "auc": a,
                             "ci_lo": lo, "ci_hi": hi, "n_test": len(ts)})

        # ---- accuracy at a 0.5 threshold, vs always-UNDER ----
        print("\n  Accuracy @ 0.5 threshold (95% bootstrap CI)")
        under_correct = (y == 0).astype(int)
        lo, hi = bootstrap_rate_ci(under_correct)
        print(f"    {'ALWAYS UNDER':<20} {under_correct.mean():.4f}  [{lo:.4f}, {hi:.4f}]")
        acc_rows.append({"stat": stat, "model": "ALWAYS UNDER",
                         "accuracy": float(under_correct.mean()), "ci_lo": lo, "ci_hi": hi})
        for name, p in models.items():
            # line_diff is not a probability, so a 0.5 threshold is meaningless for it
            if name == "line_diff alone":
                continue
            _, _, correct = picks(y, p)
            lo, hi = bootstrap_rate_ci(correct)
            print(f"    {name:<20} {correct.mean():.4f}  [{lo:.4f}, {hi:.4f}]")
            acc_rows.append({"stat": stat, "model": name,
                             "accuracy": float(correct.mean()), "ci_lo": lo, "ci_hi": hi})

        # ---- top slices by the model's confidence in its own pick ----
        print(f"\n  Top slices by |p - 0.5|  (target {BREAK_EVEN:.1%})")
        for name in ("XGBoost", "LR(all 14, scaled)"):
            p = models[name]
            pick_over, conf, correct = picks(y, p)
            order = np.argsort(-conf, kind="stable")
            for frac in TOP_SLICES:
                k = max(1, int(round(len(ts) * frac)))
                sel = order[:k]
                rate = float(correct[sel].mean())
                lo, hi = bootstrap_rate_ci(correct[sel])
                if lo > BREAK_EVEN:
                    flag = "  <-- CLEARS"
                elif rate > BREAK_EVEN:
                    flag = "  (point est. above, CI does not clear)"
                else:
                    flag = ""
                print(f"    {name:<20} top {frac:>4.0%}  n={k:>5}  "
                      f"hit={rate:.4f}  [{lo:.4f}, {hi:.4f}]  "
                      f"over-picks={int(pick_over[sel].sum())}/{k}{flag}")
                slice_rows.append({"stat": stat, "model": name, "slice": f"top {frac:.0%}",
                                   "n": k, "hit_rate": rate, "ci_lo": lo, "ci_hi": hi,
                                   "clears": bool(lo > BREAK_EVEN)})
        print()

    bar = "=" * 76
    print(f"{bar}\nSummary (bootstrap: {N_BOOT} resamples, seed {BOOT_SEED})\n{bar}")
    fmt = lambda v: f"{v:.4f}"  # noqa: E731
    print("\n-- AUC --")
    print(pd.DataFrame(auc_rows).to_string(index=False, float_format=fmt))
    print("\n-- Accuracy @ 0.5 threshold --")
    print(pd.DataFrame(acc_rows).to_string(index=False, float_format=fmt))
    print("\n-- Top slices --")
    sl = pd.DataFrame(slice_rows)
    print(sl.to_string(index=False, float_format=fmt))
    print(f"\nSlices whose 95% CI lower bound clears {BREAK_EVEN:.1%}: "
          f"{int(sl['clears'].sum())} of {len(sl)}")


if __name__ == "__main__":
    main()
