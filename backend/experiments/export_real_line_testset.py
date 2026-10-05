"""
export_real_line_testset.py

Exports the 20% temporal test split for Points / Rebounds / Assists to
experiments/testset_{stat}.parquet, using ml_trainer's own real-line data path
so the rows and the split are identical to what train_model() trained on.

Nothing in ml_trainer.py / predictor.py / main.py / *.pkl is modified or written.

Each parquet row carries:
  - all 14 ml_trainer.FEATURE_COLS features
  - real_line          the real book line joined from historical_player_props
  - actual             the player's actual stat value in that game
  - hit                the label, (actual > real_line)
  - player_name, game_date
  - xgb_prob           model_{stat}.pkl's predict_proba for that row
  - row_id             position in the date-sorted full stat frame (stable cache key)
  - eval_slice         "dev" for the first 300 test rows by date, "eval" for the rest

3PM is skipped on purpose: Underdog does not carry that market.

Usage:
    .venv/Scripts/python.exe experiments/export_real_line_testset.py
"""

from __future__ import annotations

import os
import sys

import joblib
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, BACKEND)

import ml_trainer  # noqa: E402
from trainer_bridge import build_labelled_data_with_identity, test_split  # noqa: E402

# Underdog carries Points / Rebounds / Assists but not 3PM.
STATS = ["Points", "Rebounds", "Assists"]

DEV_ROWS = 300  # first N test rows by date, reserved for prompt development


def testset_path(stat: str) -> str:
    return os.path.join(HERE, f"testset_{stat}.parquet")


def main() -> None:
    if not ml_trainer.USE_REAL_LINES:
        raise SystemExit(
            "ml_trainer.USE_REAL_LINES is False. This experiment compares against "
            "the real-line models; re-enable the flag before exporting."
        )

    print(f"ml_trainer.USE_REAL_LINES = {ml_trainer.USE_REAL_LINES}")
    print(f"FEATURE_COLS ({len(ml_trainer.FEATURE_COLS)}): {ml_trainer.FEATURE_COLS}")
    print("\nRebuilding training data via ml_trainer.build_training_data()…")

    data = build_labelled_data_with_identity(STATS)

    keep = (
        list(ml_trainer.FEATURE_COLS)
        + ["real_line", "actual", "hit", "player_name", "game_date"]
    )

    print()
    summary = []
    for stat in STATS:
        stat_data = data[data["stat_type"] == stat]
        ordered, split = test_split(stat_data)
        ordered = ordered.reset_index(drop=False).rename(columns={"index": "row_id"})

        test = ordered.iloc[split:].copy()

        model = joblib.load(ml_trainer.model_path(stat))
        X = test[ml_trainer.FEATURE_COLS].fillna(0).astype(float)
        test["xgb_prob"] = model.predict_proba(X)[:, 1]

        test["eval_slice"] = "eval"
        test.iloc[: min(DEV_ROWS, len(test)), test.columns.get_loc("eval_slice")] = "dev"

        out = test[["row_id"] + keep + ["xgb_prob", "eval_slice"]].reset_index(drop=True)
        out.to_parquet(testset_path(stat), index=False)

        n_dev = int((out["eval_slice"] == "dev").sum())
        summary.append(
            {
                "stat": stat,
                "total_rows": len(ordered),
                "train_rows": split,
                "test_rows": len(out),
                "dev": n_dev,
                "eval": len(out) - n_dev,
                "hit_rate": float(out["hit"].mean()),
                "date_from": str(out["game_date"].min())[:10],
                "date_to": str(out["game_date"].max())[:10],
            }
        )
        print(f"  wrote {testset_path(stat)}")

    print("\nRow counts")
    print(pd.DataFrame(summary).to_string(index=False))


if __name__ == "__main__":
    main()
