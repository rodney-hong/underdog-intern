"""
feature_parity.py
Checks that predictor's inference path computes hit_rate_last10 and
vs_opponent_avg exactly as ml_trainer does in training.

For N sampled training rows (seeded, across all 8 NBA stat types): take the
player's history strictly before that game through the INFERENCE path
(data_fetcher.get_player_history + feature_defs.next_game_features, with the
game's opponent id) and compare with the value in ml_trainer.build_training_data's
frame. Exact equality (NaN == NaN), no tolerance. Exit code 1 on any mismatch.

    python experiments/feature_parity.py [--n 20] [--seed 11]
"""

import os
import sys
import argparse

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import data_fetcher  # noqa: E402
import feature_defs  # noqa: E402
import ml_trainer as M  # noqa: E402
from trainer_bridge import _capture_build_training_data  # noqa: E402


def same(a, b) -> bool:
    return (pd.isna(a) and pd.isna(b)) or a == b


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--seed", type=int, default=11)
    a = ap.parse_args()

    data, per_stat, src = _capture_build_training_data()
    rng = np.random.default_rng(a.seed)
    stats = M._ALL_MODEL_STATS
    rows = []
    for i in range(a.n):
        stat = stats[i % len(stats)]
        canon = data[data["stat_type"] == stat].reset_index(drop=True)
        ident = src.loc[per_stat[stat]["valid"], ["personId", "player_name", "gameDateTimeEst",
                                                  "opponentteamId"]].reset_index(drop=True)
        assert (ident["gameDateTimeEst"].values == canon["game_date"].values).all()
        while True:                                   # skip names that don't resolve uniquely
            k = int(rng.integers(0, len(canon)))
            if data_fetcher.get_player_id(ident.at[k, "player_name"]) == ident.at[k, "personId"]:
                break
        hist = data_fetcher.get_player_history(ident.at[k, "player_name"], stat)
        hist = hist[hist["gameDateTimeEst"] < ident.at[k, "gameDateTimeEst"]]
        inf = feature_defs.next_game_features(hist, "_stat", ident.at[k, "opponentteamId"])
        vs_inf = inf["vs_opponent_avg"] if pd.notna(inf["vs_opponent_avg"]) else inf["avg_last10"]
        rows.append({
            "stat": stat, "player": ident.at[k, "player_name"],
            "game": str(ident.at[k, "gameDateTimeEst"])[:10],
            "prior_games": len(hist),
            "hit_train": canon.at[k, "hit_rate_last10"], "hit_infer": inf["hit_rate_last10"],
            "vs_train": canon.at[k, "vs_opponent_avg"], "vs_infer": vs_inf,
            "avg10_train": canon.at[k, "avg_pts_last10"], "avg10_infer": inf["avg_last10"],
        })
    df = pd.DataFrame(rows)
    df["match"] = [same(r.hit_train, r.hit_infer) and same(r.vs_train, r.vs_infer)
                   and same(r.avg10_train, r.avg10_infer) for r in df.itertuples()]
    with pd.option_context("display.width", 200, "display.max_columns", 20, "display.precision", 17):
        print(df.to_string(index=False))
    print(f"\n{int(df['match'].sum())}/{len(df)} rows match exactly")
    return 0 if df["match"].all() else 1


if __name__ == "__main__":
    sys.exit(main())
