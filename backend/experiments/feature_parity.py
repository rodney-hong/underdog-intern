"""
feature_parity.py
Checks that predictor's inference path computes hit_rate_last10 and
vs_opponent_avg exactly as ml_trainer does in training, and (gate for v2, see the
2026-10-23 preregistration entry) the four v2 features games_played_last15,
fatigue_score, home_away_split and opp_def_rating_last5, through the v2 inference
path (data_fetcher.get_player_history_v2 / get_team_rows_v2 + the feature_defs v2
helpers, cutoff = the game's gameDateTimeEst). Raw values are compared, before the
model-input fills (105.56 / 0), against the unfilled training frame.

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
                                                  "opponentteamId", "home"]].reset_index(drop=True)
        assert (ident["gameDateTimeEst"].values == canon["game_date"].values).all()
        while True:                                   # skip names that don't resolve uniquely, and
            k = int(rng.integers(0, len(canon)))      # rows with no opponent id (inference always
            if pd.isna(ident.at[k, "opponentteamId"]):  # has one, from the schedule)
                continue
            if data_fetcher.get_player_id(ident.at[k, "player_name"]) == ident.at[k, "personId"]:
                break
        hist = data_fetcher.get_player_history(ident.at[k, "player_name"], stat)
        hist = hist[hist["gameDateTimeEst"] < ident.at[k, "gameDateTimeEst"]]
        inf = feature_defs.next_game_features(hist, "_stat", ident.at[k, "opponentteamId"])
        vs_inf = inf["vs_opponent_avg"] if pd.notna(inf["vs_opponent_avg"]) else inf["avg_last10"]

        # v2 features through the v2 inference path, at the game's own tip time.
        cutoff = pd.Timestamp(ident.at[k, "gameDateTimeEst"])
        h2 = data_fetcher.get_player_history_v2(ident.at[k, "personId"])
        h2 = h2[h2["gameDateTimeEst"] < cutoff].reset_index(drop=True)
        h2["_stat"] = data_fetcher._HISTORY_STAT_EXPR[stat](h2)
        win = feature_defs.next_game_window_features(h2, cutoff)
        has = feature_defs.next_game_home_away_split(h2, "_stat", bool(ident.at[k, "home"]))
        team_rows = data_fetcher.get_team_rows_v2(ident.at[k, "opponentteamId"], cutoff)
        odr = feature_defs.opp_def_rating_before(team_rows, cutoff)
        rows.append({
            "stat": stat, "player": ident.at[k, "player_name"],
            "game": str(ident.at[k, "gameDateTimeEst"])[:10],
            "prior_games": len(hist),
            "hit_train": canon.at[k, "hit_rate_last10"], "hit_infer": inf["hit_rate_last10"],
            "vs_train": canon.at[k, "vs_opponent_avg"], "vs_infer": vs_inf,
            "avg10_train": canon.at[k, "avg_pts_last10"], "avg10_infer": inf["avg_last10"],
            "gp15_train": canon.at[k, "games_played_last15"], "gp15_infer": win["games_played_last15"],
            "fat_train": canon.at[k, "fatigue_score"], "fat_infer": win["fatigue_score"],
            "has_train": canon.at[k, "home_away_split"], "has_infer": has,
            "odr_train": canon.at[k, "opp_def_rating_last5"], "odr_infer": odr,
        })
    df = pd.DataFrame(rows)
    checks = {"hit_rate_last10": ("hit_train", "hit_infer"), "vs_opponent_avg": ("vs_train", "vs_infer"),
              "avg_pts_last10": ("avg10_train", "avg10_infer"),
              "games_played_last15": ("gp15_train", "gp15_infer"), "fatigue_score": ("fat_train", "fat_infer"),
              "home_away_split": ("has_train", "has_infer"),
              "opp_def_rating_last5": ("odr_train", "odr_infer")}
    for feat, (t, i) in checks.items():
        df[f"ok_{feat}"] = [same(a, b) for a, b in zip(df[t], df[i])]
    df["match"] = df[[f"ok_{f}" for f in checks]].all(axis=1)
    with pd.option_context("display.width", 250, "display.max_columns", 30, "display.precision", 17):
        print(df.drop(columns=[c for c in df.columns if c.startswith("ok_")]).to_string(index=False))
    print("\nper feature (exact equality, NaN == NaN):")
    for feat in checks:
        n_ok = int(df[f"ok_{feat}"].sum())
        print(f"  {feat:<22} {n_ok}/{len(df)} {'PASS' if n_ok == len(df) else 'FAIL'}")
    print(f"\n{int(df['match'].sum())}/{len(df)} rows match exactly on all features")
    return 0 if df["match"].all() else 1


if __name__ == "__main__":
    sys.exit(main())
