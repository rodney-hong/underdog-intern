"""
jev_report.py

Scores Jev against the real-line XGBoost models on exactly the rows Jev has
answers for in experiments/jev_cache.sqlite.

Per stat type and variant:
  - AUC for Jev, XGBoost, line_diff alone, and the two logistic-regression
    baselines from baselines.py (LR on line_diff only; LR on all 14, standardized)
  - 95% bootstrap CIs (1,000 resamples) for each AUC
  - Brier score and log loss for Jev and the UNCALIBRATED XGBoost model
  - Pearson + Spearman correlation of Jev's probability with XGBoost's and with line_diff
  - a 10-bin reliability table for Jev

Verdict rule, fixed in advance:
  Jev beats XGBoost only if Jev's ANONYMIZED AUC lower CI bound exceeds
  XGBoost's upper CI bound. If XGBoost's lower bound exceeds Jev's upper bound,
  XGBoost is better. Otherwise there is no significant difference.

  If the named variant beats anonymized by more than 0.03 AUC, a warning is
  printed: the gap may reflect memorized outcomes rather than skill.

Usage:
    python experiments/jev_report.py
    python experiments/jev_report.py --stat Points --slice dev
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

import numpy as np
import pandas as pd
from scipy.stats import pearsonr, spearmanr
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from jev_eval import CACHE_PATH, DEFAULT_MODEL, VARIANTS  # noqa: E402

STATS = ["Points", "Rebounds", "Assists"]
N_BOOT = 1000
BOOT_SEED = 20260920
NAMED_GAP_WARN = 0.03


def auc(y, p) -> float:
    if len(np.unique(y)) < 2:
        return float("nan")
    return float(roc_auc_score(y, p))


def bootstrap_auc_ci(y, p, n_boot=N_BOOT, seed=BOOT_SEED) -> tuple[float, float]:
    """Percentile 95% CI for AUC over `n_boot` row resamples with replacement."""
    y = np.asarray(y)
    p = np.asarray(p)
    rng = np.random.default_rng(seed)
    n = len(y)
    vals = []
    for _ in range(n_boot):
        idx = rng.integers(0, n, n)
        ys = y[idx]
        if len(np.unique(ys)) < 2:
            continue  # degenerate resample, no AUC defined
        vals.append(roc_auc_score(ys, p[idx]))
    if not vals:
        return float("nan"), float("nan")
    return float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))


def reliability_table(y, p, n_bins=10) -> pd.DataFrame:
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1], right=False), 0, n_bins - 1)
    rows = []
    for b in range(n_bins):
        m = idx == b
        rows.append(
            {
                "bin": f"[{edges[b]:.1f}, {edges[b + 1]:.1f})",
                "n": int(m.sum()),
                "mean_predicted": float(np.mean(p[m])) if m.any() else float("nan"),
                "actual_hit_rate": float(np.mean(y[m])) if m.any() else float("nan"),
            }
        )
    out = pd.DataFrame(rows)
    out["gap"] = out["actual_hit_rate"] - out["mean_predicted"]
    return out


def load_answers(conn, stat, variant, model, eval_slice=None) -> pd.DataFrame:
    q = (
        "SELECT row_id, noul, resolved_model, eval_slice, fingerprint "
        "FROM jev_answers WHERE stat=? AND variant=? AND model_version=?"
    )
    params = [stat, variant, model]
    if eval_slice:
        q += " AND eval_slice=?"
        params.append(eval_slice)
    return pd.read_sql_query(q, conn, params=params)


def analyse(merged: pd.DataFrame) -> dict:
    y = merged["hit"].to_numpy()
    jev = merged["noul"].to_numpy()
    xgb = merged["xgb_prob"].to_numpy()
    base = merged["line_diff"].to_numpy()

    # Logistic-regression baselines from baselines.py, both fitted on the
    # training split only. Absent if baselines.py has not been run.
    scores = {"jev": jev, "xgboost": xgb, "line_diff": base}
    for key, col in (("lr_linediff", "lr_linediff_prob"), ("lr_full", "lr_full_prob")):
        if col in merged.columns and merged[col].notna().all():
            scores[key] = merged[col].to_numpy()

    eps = 1e-15
    res = {
        "n": int(len(merged)),
        "base_rate": float(y.mean()),
        "auc": {k: auc(y, v) for k, v in scores.items()},
        "auc_ci": {k: bootstrap_auc_ci(y, v) for k, v in scores.items()},
        "brier": {
            "jev": float(brier_score_loss(y, jev)),
            "xgboost": float(brier_score_loss(y, xgb)),
        },
        "log_loss": {
            "jev": float(log_loss(y, np.clip(jev, eps, 1 - eps), labels=[0, 1])),
            "xgboost": float(log_loss(y, np.clip(xgb, eps, 1 - eps), labels=[0, 1])),
        },
        "corr": {},
        "reliability": reliability_table(y, jev),
    }
    for name, other in (("xgboost", xgb), ("line_diff", base)):
        if np.std(jev) > 0 and np.std(other) > 0:
            res["corr"][name] = {
                "pearson": float(pearsonr(jev, other)[0]),
                "spearman": float(spearmanr(jev, other)[0]),
            }
        else:
            res["corr"][name] = {"pearson": float("nan"), "spearman": float("nan")}
    return res


def fmt_ci(v, ci) -> str:
    return f"{v:.4f}  [{ci[0]:.4f}, {ci[1]:.4f}]"


def verdict(jev_auc, jev_ci, xgb_auc, xgb_ci) -> str:
    if jev_ci[0] > xgb_ci[1]:
        return "JEV BETTER — Jev's anonymized AUC lower bound exceeds XGBoost's upper bound."
    if xgb_ci[0] > jev_ci[1]:
        return "XGBoost better — XGBoost's AUC lower bound exceeds Jev's upper bound."
    return "no significant difference — the AUC confidence intervals overlap."


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stat", action="append", choices=STATS, help="repeatable; default all")
    ap.add_argument("--slice", dest="eval_slice", default=None, choices=["dev", "eval"])
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--json-out", default=None, help="also write the numbers to this JSON file")
    args = ap.parse_args()

    if not os.path.exists(CACHE_PATH):
        raise SystemExit(f"{CACHE_PATH} not found — run jev_eval.py first.")

    stats = args.stat or STATS
    conn = sqlite3.connect(CACHE_PATH)
    dump: dict = {"pinned_model": args.model, "slice": args.eval_slice or "all", "stats": {}}

    print("=" * 78)
    print(f"Jev vs. real-line XGBoost   pinned model: {args.model}")
    print(f"slice: {args.eval_slice or 'all'}   bootstrap: {N_BOOT} resamples, seed {BOOT_SEED}")
    print("=" * 78)

    for stat in stats:
        path = os.path.join(HERE, f"testset_{stat}.parquet")
        if not os.path.exists(path):
            print(f"\n[{stat}] no test set exported — skipping.")
            continue
        test = pd.read_parquet(path)

        per_variant: dict = {}
        for variant in VARIANTS:
            ans = load_answers(conn, stat, variant, args.model, args.eval_slice)
            if ans.empty:
                continue
            merged = test.merge(ans, on="row_id", how="inner", suffixes=("", "_ans"))
            if merged.empty:
                continue
            per_variant[variant] = analyse(merged)
            per_variant[variant]["resolved_models"] = sorted(
                ans["resolved_model"].dropna().unique().tolist()
            )
            per_variant[variant]["fingerprints"] = sorted(
                ans["fingerprint"].dropna().unique().tolist()
            )[:3]
            per_variant[variant]["slices"] = sorted(ans["eval_slice"].dropna().unique().tolist())

        if not per_variant:
            print(f"\n[{stat}] no cached Jev answers for model {args.model} — skipping.")
            continue

        print(f"\n{'=' * 78}\n{stat}\n{'=' * 78}")
        for variant, r in per_variant.items():
            print(
                f"\n  [{variant}]  n={r['n']:,}  base hit rate={r['base_rate']:.4f}  "
                f"slices={r['slices']}  resolved={r['resolved_models']}"
            )
            print("    AUC (95% bootstrap CI)")
            labels = {
                "jev": "Jev",
                "xgboost": "XGBoost",
                "line_diff": "line_diff alone",
                "lr_linediff": "LR(line_diff)",
                "lr_full": "LR(all 14, scaled)",
            }
            for key, label in labels.items():
                if key in r["auc"]:
                    print(f"      {label:<19}{fmt_ci(r['auc'][key], r['auc_ci'][key])}")
            print("    Brier / log loss  — Jev vs UNCALIBRATED XGBoost")
            print("      (model_{stat}.pkl is a bare XGBClassifier; no CalibratedClassifierCV")
            print("       is fitted anywhere in ml_trainer.py, so this is NOT a fair")
            print("       calibration comparison — read it as a raw-probability comparison.)")
            print(f"      Jev        {r['brier']['jev']:.4f} / {r['log_loss']['jev']:.4f}")
            print(f"      XGBoost    {r['brier']['xgboost']:.4f} / {r['log_loss']['xgboost']:.4f}")
            print(
                f"    corr(Jev, XGBoost)    pearson={r['corr']['xgboost']['pearson']:+.4f}  "
                f"spearman={r['corr']['xgboost']['spearman']:+.4f}"
            )
            print(
                f"    corr(Jev, line_diff)  pearson={r['corr']['line_diff']['pearson']:+.4f}  "
                f"spearman={r['corr']['line_diff']['spearman']:+.4f}"
            )
            print("    Jev reliability (10 bins)")
            tbl = r["reliability"]
            print(tbl.to_string(index=False, float_format=lambda v: f"{v:.4f}"))

        anon = per_variant.get("anonymized")
        if anon is None:
            print("\n  VERDICT: not evaluable — no anonymized run for this stat.")
        else:
            print(
                f"\n  VERDICT ({stat}): "
                + verdict(
                    anon["auc"]["jev"], anon["auc_ci"]["jev"],
                    anon["auc"]["xgboost"], anon["auc_ci"]["xgboost"],
                )
            )

        named = per_variant.get("named")
        if anon is not None and named is not None:
            gap = named["auc"]["jev"] - anon["auc"]["jev"]
            print(f"  named - anonymized AUC gap: {gap:+.4f}")
            if gap > NAMED_GAP_WARN:
                print(
                    f"  WARNING: the named variant beats anonymized by {gap:.4f} AUC "
                    f"(> {NAMED_GAP_WARN}). This gap may reflect memorized outcomes "
                    f"for these players and dates rather than genuine forecasting skill."
                )

        dump["stats"][stat] = {
            v: {k: (val.to_dict("records") if isinstance(val, pd.DataFrame) else val)
                for k, val in r.items()}
            for v, r in per_variant.items()
        }

    conn.close()

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(dump, fh, indent=2, default=str)
        print(f"\nwrote {args.json_out}")


if __name__ == "__main__":
    main()
