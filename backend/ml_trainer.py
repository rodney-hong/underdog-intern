"""
ml_trainer.py
Builds training data from player_stats and trains one XGBoost classifier per
stat type (Points, Rebounds, Assists, 3PM) with 19 features.
"""

import os
import sqlite3

import joblib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, roc_auc_score
from xgboost import XGBClassifier

from database import DB_PATH

MODEL_DIR = os.path.dirname(__file__)
MODEL_PATH = os.path.join(MODEL_DIR, "model_Points.pkl")  # sentinel for main.py existence check
WNBA_MODEL_PATH = os.path.join(MODEL_DIR, "model_wnba_Points.pkl")  # sentinel for WNBA startup check

FEATURE_COLS = [
    "avg_pts_last5",
    "avg_pts_last10",
    "hit_rate_last10",
    "trend",
    "vs_opponent_avg",
    "line_diff",
    "consistency",
    "games_played_last15",
    "opp_def_rating_last5",
    "is_playoff",
    "minutes_trend",
    "avg_minutes_last5",
    "fatigue_score",
    "home_away_split",
]

_TRAIN_STAT_COLS = {
    "Points":   "points",
    "Rebounds": "reboundsTotal",
    "Assists":  "assists",
    "3PM":      "threePointersMade",
}

_WNBA_STAT_COLS = {
    "Points":   "points",
    "Rebounds": "rebounds",
    "Assists":  "assists",
    "3PM":      "three_pm",
}


def model_path(stat_type: str) -> str:
    return os.path.join(MODEL_DIR, f"model_{stat_type}.pkl")


def wnba_model_path(stat_type: str) -> str:
    return os.path.join(MODEL_DIR, f"model_wnba_{stat_type}.pkl")


def _parse_minutes(s) -> float:
    """Parse minutes from 'MM:SS' string or numeric value."""
    if s is None:
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip()
    if ":" in s:
        parts = s.split(":")
        try:
            return int(parts[0]) + int(parts[1]) / 60.0
        except (ValueError, IndexError):
            return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def build_training_data() -> pd.DataFrame:
    """
    Query all game rows from player_stats and return a labelled feature DataFrame.
    Each row is one (player, game, stat_type).  All features use only data from
    games before that game date — no lookahead.  Rows with fewer than 10 prior
    games for the player are dropped.
    """
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        """
        SELECT personId, firstName, lastName, gameDateTimeEst, home, opponentteamId, gameType,
               playerteamId, estimatedPace, usagePercentage, defensiveRating,
               numMinutes, points, reboundsTotal, assists, threePointersMade
        FROM player_stats
        WHERE numMinutes IS NOT NULL AND numMinutes > 0
        """,
        conn,
    )
    conn.close()

    if df.empty:
        return pd.DataFrame()

    df["gameDateTimeEst"] = pd.to_datetime(df["gameDateTimeEst"], errors="coerce")
    df = df.dropna(subset=["gameDateTimeEst"])
    df = df.sort_values(["personId", "gameDateTimeEst"]).reset_index(drop=True)
    df["home"] = df["home"].fillna(0).astype(int)
    df["is_playoff"] = (df["gameType"] != "Regular Season").astype(int)
    df["player_name"] = df["firstName"].str.strip() + " " + df["lastName"].str.strip()
    df["game_date_str"] = df["gameDateTimeEst"].dt.strftime("%Y-%m-%d")

    # games_played_last15: games within the 15 days before each game
    _tmp = df[["personId", "gameDateTimeEst"]].copy()
    _tmp["_one"] = 1.0
    _tmp_idx = _tmp.sort_values(["personId", "gameDateTimeEst"]).set_index("gameDateTimeEst")
    _gp15 = _tmp_idx.groupby("personId")["_one"].transform(
        lambda x: x.rolling("15D", closed="left").sum()
    )
    df["games_played_last15"] = _gp15.values

    # opp_def_rating_last5: each team's rolling 5-game avg defensive rating,
    # joined on opponentteamId so we get the *opponent's* recent defensive quality.
    team_game_def = (
        df.groupby(["playerteamId", "gameDateTimeEst"])["defensiveRating"]
        .mean()
        .reset_index()
        .sort_values(["playerteamId", "gameDateTimeEst"])
    )
    team_game_def["def_rtg_roll5"] = team_game_def.groupby("playerteamId")[
        "defensiveRating"
    ].transform(lambda x: x.shift(1).rolling(5, min_periods=1).mean())
    team_game_def = team_game_def.rename(
        columns={"playerteamId": "opponentteamId", "def_rtg_roll5": "opp_def_rating_last5"}
    )
    df = df.merge(
        team_game_def[["opponentteamId", "gameDateTimeEst", "opp_def_rating_last5"]],
        on=["opponentteamId", "gameDateTimeEst"],
        how="left",
    )
    global_def_mean = float(df["defensiveRating"].mean())
    df["opp_def_rating_last5"] = df["opp_def_rating_last5"].fillna(global_def_mean)

    # avg_minutes_last5 / minutes_trend — lagged rolling means of numMinutes
    _min5 = df.groupby("personId")["numMinutes"].transform(
        lambda x: x.shift(1).rolling(5, min_periods=5).mean()
    )
    _min10 = df.groupby("personId")["numMinutes"].transform(
        lambda x: x.shift(1).rolling(10, min_periods=10).mean()
    )
    df["avg_minutes_last5"] = _min5
    df["minutes_trend"] = _min5 - _min10

    # fatigue_score: sum of numMinutes in the 7 days before each game (no lookahead).
    # Uses closed='left' so the current game is excluded from its own window —
    # same pattern as games_played_last15 above.
    _fat_tmp = df[["personId", "gameDateTimeEst", "numMinutes"]].copy()
    _fat_tmp = _fat_tmp.sort_values(["personId", "gameDateTimeEst"]).set_index("gameDateTimeEst")
    _fat = _fat_tmp.groupby("personId")["numMinutes"].transform(
        lambda x: x.rolling("7D", closed="left").sum()
    )
    df["fatigue_score"] = _fat.values

    all_frames: list[pd.DataFrame] = []
    np.random.seed(42)

    for stat_label, col in _TRAIN_STAT_COLS.items():
        g = df.groupby("personId")[col]

        avg5  = g.transform(lambda x: x.shift(1).rolling(5,  min_periods=5).mean())
        avg10 = g.transform(lambda x: x.shift(1).rolling(10, min_periods=10).mean())

        # Expanding season-average proxy (lagged) used for hit_rate computation
        exp_mean_lagged = g.transform(lambda x: x.shift(1).expanding(min_periods=1).mean())

        beat_avg = (df[col] > exp_mean_lagged).astype(float)
        df["_beat"] = beat_avg
        hit_rate = df.groupby("personId")["_beat"].transform(
            lambda x: x.shift(1).rolling(10, min_periods=10).mean()
        )

        trend = avg5 - avg10

        noise = np.random.uniform(-1.5, 1.5, size=len(df))
        simulated_line = exp_mean_lagged + noise

        consistency = g.transform(lambda x: x.shift(1).rolling(10, min_periods=10).std())
        line_diff = avg10 - simulated_line

        df["_stat"] = df[col]
        vs_opp = df.groupby(["personId", "opponentteamId"])["_stat"].transform(
            lambda x: x.shift(1).expanding(min_periods=1).mean()
        )
        vs_opp = vs_opp.fillna(avg10)

        # home_away_split: (home expanding avg) minus (away expanding avg),
        # multiplied by +1 at home / -1 away for a directional signal.
        # Expanding means are computed separately on home/away subsets (shifted
        # to avoid lookahead), then ffill'd within each player so that every row
        # carries the last known value for both sides before taking the difference.
        home_mask = df["home"] == 1
        away_mask = df["home"] == 0

        df["_home_avg"] = np.nan
        df.loc[home_mask, "_home_avg"] = (
            df.loc[home_mask]
            .groupby("personId")[col]
            .transform(lambda x: x.shift(1).expanding().mean())
        )
        df["_home_avg"] = df.groupby("personId")["_home_avg"].transform(
            lambda x: x.ffill().fillna(0.0)
        )

        df["_away_avg"] = np.nan
        df.loc[away_mask, "_away_avg"] = (
            df.loc[away_mask]
            .groupby("personId")[col]
            .transform(lambda x: x.shift(1).expanding().mean())
        )
        df["_away_avg"] = df.groupby("personId")["_away_avg"].transform(
            lambda x: x.ffill().fillna(0.0)
        )

        direction = df["home"].replace(0, -1)
        home_away_split = (df["_home_avg"] - df["_away_avg"]) * direction
        df.drop(columns=["_home_avg", "_away_avg"], inplace=True)

        target = (df[col] > simulated_line).astype(int)
        valid = avg10.notna()

        stat_df = pd.DataFrame(
            {
                "avg_pts_last5":        avg5[valid].values,
                "avg_pts_last10":       avg10[valid].values,
                "hit_rate_last10":      hit_rate[valid].values,
                "trend":                trend[valid].values,
                "vs_opponent_avg":      vs_opp[valid].values,
                "line_diff":            line_diff[valid].values,
                "consistency":          consistency[valid].values,
                "games_played_last15":  df["games_played_last15"][valid].values,
                "opp_def_rating_last5": df["opp_def_rating_last5"][valid].values,
                "is_playoff":           df["is_playoff"][valid].values,
                "minutes_trend":        df["minutes_trend"][valid].values,
                "avg_minutes_last5":    df["avg_minutes_last5"][valid].values,
                "fatigue_score":        df["fatigue_score"][valid].values,
                "home_away_split":      home_away_split[valid].values,
                "game_date":            df["gameDateTimeEst"][valid].values,
                "stat_type":            stat_label,
                "hit":                  target[valid].values,
            }
        )
        all_frames.append(stat_df)

    df.drop(columns=["_beat", "_stat"], inplace=True, errors="ignore")
    return pd.concat(all_frames, ignore_index=True)


def train_model() -> dict:
    """
    Train one XGBoost classifier per stat type on the full player_stats history.
    Uses an 80/20 temporal split per stat type.
    Saves model_{stat_type}.pkl and feature_importance_{stat_type}.png for each.
    Returns a dict mapping stat_type → trained model.
    """
    print("Building training data from player_stats…")
    data = build_training_data()
    if data.empty:
        raise RuntimeError("No training data — make sure player_stats is populated via ETL.")

    print(f"  {len(data):,} labelled rows across {data['stat_type'].nunique()} stat types")

    models: dict = {}

    for stat_type in _TRAIN_STAT_COLS:
        stat_data = (
            data[data["stat_type"] == stat_type]
            .sort_values("game_date")
            .reset_index(drop=True)
        )
        if stat_data.empty:
            print(f"  [{stat_type}] no rows — skipping")
            continue

        X = stat_data[FEATURE_COLS].fillna(0).astype(float)
        y = stat_data["hit"]

        split = int(len(stat_data) * 0.8)
        X_train, X_test = X.iloc[:split], X.iloc[split:]
        y_train, y_test = y.iloc[:split], y.iloc[split:]

        model = XGBClassifier(
            eval_metric="logloss",
            n_estimators=200,
            max_depth=4,
            learning_rate=0.05,
            random_state=42,
        )
        model.fit(X_train, y_train)

        y_pred = model.predict(X_test)
        y_prob = model.predict_proba(X_test)[:, 1]
        print(
            f"  [{stat_type}] accuracy={accuracy_score(y_test, y_pred):.4f}"
            f"  AUC={roc_auc_score(y_test, y_prob):.4f}"
        )

        mp = model_path(stat_type)
        joblib.dump(model, mp)
        print(f"  Model saved → {mp}")

        plot_p = os.path.join(MODEL_DIR, f"feature_importance_{stat_type}.png")
        importances = pd.Series(model.feature_importances_, index=FEATURE_COLS)
        fig, ax = plt.subplots(figsize=(8, 5))
        importances.sort_values().plot.barh(ax=ax)
        ax.set_title(f"XGBoost Feature Importances — {stat_type}")
        ax.set_xlabel("Importance")
        plt.tight_layout()
        plt.savefig(plot_p)
        plt.close(fig)

        models[stat_type] = model

    print("Training complete.")
    return models


def build_wnba_training_data() -> pd.DataFrame:
    """
    Query wnba_player_stats and return a labelled feature DataFrame.
    Uses the same 14 features as the NBA model.  opp_def_rating_last5 is
    set to 0.0 (neutral) because per-game defensive ratings are not available
    for WNBA.  Rows with fewer than 10 prior games per player are dropped.
    """
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        """
        SELECT player_name, game_date, home, player_team, opponent_team,
               minutes, points, rebounds, assists, three_pm
        FROM wnba_player_stats
        WHERE minutes IS NOT NULL AND minutes NOT IN ('0', '0:00', '')
        """,
        conn,
    )
    conn.close()

    if df.empty:
        return pd.DataFrame()

    df["game_date"] = pd.to_datetime(df["game_date"], errors="coerce")
    df = df.dropna(subset=["game_date"])
    df["numMinutes"] = df["minutes"].apply(_parse_minutes)
    df = df[df["numMinutes"] > 0].reset_index(drop=True)  # drop DNP rows
    df = df.sort_values(["player_name", "game_date"]).reset_index(drop=True)
    df["home"] = df["home"].fillna(0).astype(int)
    # WNBA playoffs start in September
    df["is_playoff"] = (df["game_date"].dt.month >= 9).astype(int)
    df["opp_def_rating_last5"] = 0.0

    # games_played_last15
    _tmp = df[["player_name", "game_date"]].copy()
    _tmp["_one"] = 1.0
    _tmp_idx = _tmp.sort_values(["player_name", "game_date"]).set_index("game_date")
    _gp15 = _tmp_idx.groupby("player_name")["_one"].transform(
        lambda x: x.rolling("15D", closed="left").sum()
    )
    df["games_played_last15"] = _gp15.values

    # avg_minutes_last5 / minutes_trend
    _min5 = df.groupby("player_name")["numMinutes"].transform(
        lambda x: x.shift(1).rolling(5, min_periods=5).mean()
    )
    _min10 = df.groupby("player_name")["numMinutes"].transform(
        lambda x: x.shift(1).rolling(10, min_periods=10).mean()
    )
    df["avg_minutes_last5"] = _min5
    df["minutes_trend"] = _min5 - _min10

    # fatigue_score: sum of minutes in the 7 days before each game
    _fat_tmp = df[["player_name", "game_date", "numMinutes"]].copy()
    _fat_tmp = _fat_tmp.sort_values(["player_name", "game_date"]).set_index("game_date")
    _fat = _fat_tmp.groupby("player_name")["numMinutes"].transform(
        lambda x: x.rolling("7D", closed="left").sum()
    )
    df["fatigue_score"] = _fat.values

    all_frames: list[pd.DataFrame] = []
    np.random.seed(42)

    for stat_label, col in _WNBA_STAT_COLS.items():
        g = df.groupby("player_name")[col]

        avg5  = g.transform(lambda x: x.shift(1).rolling(5,  min_periods=5).mean())
        avg10 = g.transform(lambda x: x.shift(1).rolling(10, min_periods=10).mean())

        exp_mean_lagged = g.transform(lambda x: x.shift(1).expanding(min_periods=1).mean())

        beat_avg = (df[col] > exp_mean_lagged).astype(float)
        df["_beat"] = beat_avg
        hit_rate = df.groupby("player_name")["_beat"].transform(
            lambda x: x.shift(1).rolling(10, min_periods=10).mean()
        )

        trend = avg5 - avg10

        noise = np.random.uniform(-1.5, 1.5, size=len(df))
        simulated_line = exp_mean_lagged + noise

        consistency = g.transform(lambda x: x.shift(1).rolling(10, min_periods=10).std())
        line_diff = avg10 - simulated_line

        df["_stat"] = df[col]
        vs_opp = df.groupby(["player_name", "opponent_team"])["_stat"].transform(
            lambda x: x.shift(1).expanding(min_periods=1).mean()
        )
        vs_opp = vs_opp.fillna(avg10)

        home_mask = df["home"] == 1
        away_mask = df["home"] == 0

        df["_home_avg"] = np.nan
        df.loc[home_mask, "_home_avg"] = (
            df.loc[home_mask]
            .groupby("player_name")[col]
            .transform(lambda x: x.shift(1).expanding().mean())
        )
        df["_home_avg"] = df.groupby("player_name")["_home_avg"].transform(
            lambda x: x.ffill().fillna(0.0)
        )

        df["_away_avg"] = np.nan
        df.loc[away_mask, "_away_avg"] = (
            df.loc[away_mask]
            .groupby("player_name")[col]
            .transform(lambda x: x.shift(1).expanding().mean())
        )
        df["_away_avg"] = df.groupby("player_name")["_away_avg"].transform(
            lambda x: x.ffill().fillna(0.0)
        )

        direction = df["home"].replace(0, -1)
        home_away_split = (df["_home_avg"] - df["_away_avg"]) * direction
        df.drop(columns=["_home_avg", "_away_avg"], inplace=True)

        target = (df[col] > simulated_line).astype(int)
        valid = avg10.notna()

        stat_df = pd.DataFrame(
            {
                "avg_pts_last5":        avg5[valid].values,
                "avg_pts_last10":       avg10[valid].values,
                "hit_rate_last10":      hit_rate[valid].values,
                "trend":                trend[valid].values,
                "vs_opponent_avg":      vs_opp[valid].values,
                "line_diff":            line_diff[valid].values,
                "consistency":          consistency[valid].values,
                "games_played_last15":  df["games_played_last15"][valid].values,
                "opp_def_rating_last5": df["opp_def_rating_last5"][valid].values,
                "is_playoff":           df["is_playoff"][valid].values,
                "minutes_trend":        df["minutes_trend"][valid].values,
                "avg_minutes_last5":    df["avg_minutes_last5"][valid].values,
                "fatigue_score":        df["fatigue_score"][valid].values,
                "home_away_split":      home_away_split[valid].values,
                "game_date":            df["game_date"][valid].values,
                "stat_type":            stat_label,
                "hit":                  target[valid].values,
            }
        )
        all_frames.append(stat_df)

    df.drop(columns=["_beat", "_stat"], inplace=True, errors="ignore")
    return pd.concat(all_frames, ignore_index=True)


def train_wnba_models() -> dict:
    """
    Train one XGBoost classifier per WNBA stat type on the full wnba_player_stats
    history.  Uses an 80/20 temporal split per stat type.
    Saves model_wnba_{stat_type}.pkl and feature_importance_wnba_{stat_type}.png.
    Returns a dict mapping stat_type → trained model.
    """
    print("Building WNBA training data from wnba_player_stats…")
    data = build_wnba_training_data()
    if data.empty:
        print("  No WNBA training data yet — skipping WNBA model training.")
        return {}

    print(f"  {len(data):,} labelled rows across {data['stat_type'].nunique()} stat types")

    models: dict = {}

    for stat_type in _WNBA_STAT_COLS:
        stat_data = (
            data[data["stat_type"] == stat_type]
            .sort_values("game_date")
            .reset_index(drop=True)
        )
        if stat_data.empty:
            print(f"  [WNBA {stat_type}] no rows — skipping")
            continue

        X = stat_data[FEATURE_COLS].fillna(0).astype(float)
        y = stat_data["hit"]

        split = int(len(stat_data) * 0.8)
        X_train, X_test = X.iloc[:split], X.iloc[split:]
        y_train, y_test = y.iloc[:split], y.iloc[split:]

        model = XGBClassifier(
            eval_metric="logloss",
            n_estimators=200,
            max_depth=4,
            learning_rate=0.05,
            random_state=42,
        )
        model.fit(X_train, y_train)

        y_pred = model.predict(X_test)
        y_prob = model.predict_proba(X_test)[:, 1]
        print(
            f"  [WNBA {stat_type}] accuracy={accuracy_score(y_test, y_pred):.4f}"
            f"  AUC={roc_auc_score(y_test, y_prob):.4f}"
        )

        mp = wnba_model_path(stat_type)
        joblib.dump(model, mp)
        print(f"  Model saved → {mp}")

        plot_p = os.path.join(MODEL_DIR, f"feature_importance_wnba_{stat_type}.png")
        importances = pd.Series(model.feature_importances_, index=FEATURE_COLS)
        fig, ax = plt.subplots(figsize=(8, 5))
        importances.sort_values().plot.barh(ax=ax)
        ax.set_title(f"XGBoost Feature Importances — WNBA {stat_type}")
        ax.set_xlabel("Importance")
        plt.tight_layout()
        plt.savefig(plot_p)
        plt.close(fig)

        models[stat_type] = model

    print("WNBA training complete.")
    return models
