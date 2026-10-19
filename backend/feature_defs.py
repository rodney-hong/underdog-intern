"""
feature_defs.py
Shared definitions of hit_rate_last10 and vs_opponent_avg, used by BOTH
ml_trainer.build_training_data (vectorized over every player-game) and
predictor (the next, not-yet-played game). One implementation, so training and
inference cannot drift.

Definitions (counted games only, see game_types.py; chronological per player):
  hit_rate_last10  For each of the player's previous 10 games, did the stat beat
                   his career-to-date mean as of that game (mean of all games
                   before it)? hit_rate_last10 = share of those 10 that did.
                   NaN with fewer than 10 previous games.
  vs_opponent_avg  Mean of the stat over ALL of the player's previous games against
                   this opponent (matched on opponentteamId, so renamed franchises
                   stay one team). Falls back to avg_last10 if there are none.

Inference reuses the same pandas code: the player's history plus one placeholder
row for the upcoming game (stat NaN, its opponent id) goes through the same
transforms, and the placeholder row's values are the features. Every lagged
transform uses shift(1), so the placeholder's own stat is never read.
"""

import numpy as np
import pandas as pd


def lagged_avg(df: pd.DataFrame, col: str, n: int, key: str = "personId") -> pd.Series:
    """Mean of the previous n games (min_periods=n), per player."""
    return df.groupby(key)[col].transform(lambda x: x.shift(1).rolling(n, min_periods=n).mean())


def lagged_expanding_mean(df: pd.DataFrame, col: str, key: str = "personId") -> pd.Series:
    """Career-to-date mean before each game, per player."""
    return df.groupby(key)[col].transform(lambda x: x.shift(1).expanding(min_periods=1).mean())


def hit_rate_last10(df: pd.DataFrame, col: str, exp_mean_lagged: pd.Series,
                    key: str = "personId") -> pd.Series:
    beat = (df[col] > exp_mean_lagged).astype(float)
    return beat.groupby(df[key]).transform(lambda x: x.shift(1).rolling(10, min_periods=10).mean())


def vs_opponent_avg(df: pd.DataFrame, col: str, avg10: pd.Series,
                    key: str = "personId", opp_key: str = "opponentteamId") -> pd.Series:
    vs = df.groupby([key, opp_key])[col].transform(
        lambda x: x.shift(1).expanding(min_periods=1).mean())
    return vs.fillna(avg10)


def next_game_features(history: pd.DataFrame, col: str, opponent_id) -> dict:
    """
    hit_rate_last10 and vs_opponent_avg for the player's NEXT game against
    opponent_id. history: the player's counted games, one player, any order, with
    columns gameDateTimeEst, opponentteamId and `col`.
    """
    h = history.sort_values("gameDateTimeEst").reset_index(drop=True)
    h = h[["gameDateTimeEst", "opponentteamId", col]].copy()
    h["personId"] = 0
    nxt = pd.DataFrame({"gameDateTimeEst": [pd.NaT], "opponentteamId": [opponent_id],
                        col: [np.nan], "personId": [0]})
    h = pd.concat([h, nxt], ignore_index=True)
    h[col] = h[col].astype("float64")
    avg10 = lagged_avg(h, col, 10)
    exp_mean = lagged_expanding_mean(h, col)
    hr = hit_rate_last10(h, col, exp_mean)
    vs = vs_opponent_avg(h, col, avg10)
    return {"hit_rate_last10": float(hr.iloc[-1]) if pd.notna(hr.iloc[-1]) else np.nan,
            "vs_opponent_avg": float(vs.iloc[-1]) if pd.notna(vs.iloc[-1]) else np.nan,
            "avg_last10": float(avg10.iloc[-1]) if pd.notna(avg10.iloc[-1]) else np.nan}
