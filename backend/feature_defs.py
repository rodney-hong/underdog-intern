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


# ---------------------------------------------------------------------------
# v2 inference helpers (preregistration entry "inference parity, data freshness and
# pull timing", 2026-10-23). ml_trainer.build_training_data computes these four
# features inline; ml_trainer is frozen, so these helpers rerun the SAME pandas
# transforms on one player's (or team's) history plus a placeholder row for the
# upcoming game, which reproduces training's floats bit for bit (rolling and
# expanding means are online sums, so the rows and their order must match).
# Inputs: the rows training keeps (counted game types, numMinutes > 0, valid date),
# for one player in gameDateTimeEst order, or for one team in (personId,
# gameDateTimeEst) order. `cutoff` is the target game's gameDateTimeEst (ET).
# experiments/feature_parity.py checks each against the training frame.
# ---------------------------------------------------------------------------

def next_game_window_features(history: pd.DataFrame, cutoff: pd.Timestamp) -> dict:
    """games_played_last15 / fatigue_score as training: rolling("15D"/"7D", closed="left")."""
    h = history.loc[history["gameDateTimeEst"] < cutoff, ["gameDateTimeEst", "numMinutes"]]
    h = pd.concat([h, pd.DataFrame({"gameDateTimeEst": [cutoff], "numMinutes": [np.nan]})],
                  ignore_index=True)
    h["_one"] = 1.0
    idx = h.set_index("gameDateTimeEst")
    gp15 = idx["_one"].rolling("15D", closed="left").sum()
    fat = idx["numMinutes"].rolling("7D", closed="left").sum()
    return {"games_played_last15": float(gp15.iloc[-1]), "fatigue_score": float(fat.iloc[-1])}


def next_game_home_away_split(history: pd.DataFrame, col: str, is_home: bool) -> float:
    """
    home_away_split as training: per side, the lagged expanding mean, carried forward
    to every later row (so the side opposite the target game lags one more game),
    missing -> 0, times +1 at home / -1 away.
    """
    h = history[["gameDateTimeEst", "home", col]].copy()
    h = pd.concat([h, pd.DataFrame({"gameDateTimeEst": [pd.NaT], "home": [1 if is_home else 0],
                                    col: [np.nan]})], ignore_index=True)
    h["home"] = h["home"].fillna(0).astype(int)
    h[col] = h[col].astype("float64")
    h["personId"] = 0
    home_mask, away_mask = h["home"] == 1, h["home"] == 0
    for side, mask in (("_home_avg", home_mask), ("_away_avg", away_mask)):
        h[side] = np.nan
        h.loc[mask, side] = (h.loc[mask].groupby("personId")[col]
                             .transform(lambda x: x.shift(1).expanding().mean()))
        h[side] = h.groupby("personId")[side].transform(lambda x: x.ffill().fillna(0.0))
    direction = h["home"].replace(0, -1)
    return float(((h["_home_avg"] - h["_away_avg"]) * direction).iloc[-1])


def opp_def_rating_before(team_rows: pd.DataFrame, cutoff: pd.Timestamp) -> float:
    """
    opp_def_rating_last5 as training: the team's per-game mean defensiveRating,
    shift(1).rolling(5, min_periods=1).mean(), at the target game. NaN when the team
    has no earlier game (the frozen fit filled those with the all-dates mean).
    """
    g = (team_rows.loc[team_rows["gameDateTimeEst"] < cutoff]
         .groupby("gameDateTimeEst")["defensiveRating"].mean()
         .reset_index().sort_values("gameDateTimeEst"))
    g = pd.concat([g, pd.DataFrame({"gameDateTimeEst": [cutoff], "defensiveRating": [np.nan]})],
                  ignore_index=True)
    r = g["defensiveRating"].shift(1).rolling(5, min_periods=1).mean()
    return float(r.iloc[-1]) if pd.notna(r.iloc[-1]) else np.nan
