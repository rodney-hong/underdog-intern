"""
predictor.py
Over/under prediction engine for NBA player props.

Uses an XGBoost model (model.pkl) for the four individual stat types
(Points, Rebounds, Assists, 3PM).  Falls back to the heuristic scorer for
combined types (PRA, PR, PA, RA) or when model.pkl is absent.
"""

import os
import sqlite3
from datetime import datetime

import joblib
import numpy as np
import pandas as pd

from data_fetcher import (
    get_game_logs, get_injury_context, full_to_abbrev,
    get_team_def_rating_last5, get_player_id,
    get_wnba_game_logs, get_wnba_injury_status,
)
from database import DB_PATH

# ---------------------------------------------------------------------------
# Model paths + feature list  (must match ml_trainer.FEATURE_COLS exactly)
# ---------------------------------------------------------------------------

_MODEL_DIR = os.path.dirname(__file__)

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

# Combined stat types are not in the training set — route them to the heuristic
_ML_STAT_TYPES = {"Points", "Assists", "Rebounds", "3PM"}

_MODELS: dict = {}
_MODELS_LOADED = False

# WNBA
_WNBA_ML_STAT_TYPES = {"Points", "Assists", "Rebounds", "3PM"}
_WNBA_MODELS: dict = {}
_WNBA_MODELS_LOADED = False

# 2025 NBA playoffs started April 19 2025
_PLAYOFF_START = datetime(2025, 4, 19)


def _load_models() -> dict:
    global _MODELS, _MODELS_LOADED
    if _MODELS_LOADED:
        return _MODELS
    for stat_type in _ML_STAT_TYPES:
        mp = os.path.join(_MODEL_DIR, f"model_{stat_type}.pkl")
        if os.path.exists(mp):
            _MODELS[stat_type] = joblib.load(mp)
    if _MODELS:
        print(f"[predictor] Loaded per-stat models: {sorted(_MODELS)}")
    else:
        print("[predictor] No per-stat models found — using heuristic scorer.")
    _MODELS_LOADED = True
    return _MODELS


def _load_wnba_models() -> dict:
    global _WNBA_MODELS, _WNBA_MODELS_LOADED
    if _WNBA_MODELS_LOADED:
        return _WNBA_MODELS
    for stat_type in _WNBA_ML_STAT_TYPES:
        mp = os.path.join(_MODEL_DIR, f"model_wnba_{stat_type}.pkl")
        if os.path.exists(mp):
            _WNBA_MODELS[stat_type] = joblib.load(mp)
    if _WNBA_MODELS:
        print(f"[predictor] Loaded WNBA models: {sorted(_WNBA_MODELS)}")
    else:
        print("[predictor] No WNBA models found — using heuristic scorer for WNBA.")
    _WNBA_MODELS_LOADED = True
    return _WNBA_MODELS


# ---------------------------------------------------------------------------
# Home/away split helper  (direct DB query — game logs don't carry home flag)
# ---------------------------------------------------------------------------

_HA_COL_MAP = {
    "Points": "points", "Assists": "assists",
    "Rebounds": "reboundsTotal", "3PM": "threePointersMade",
}

_WNBA_HA_COL_MAP = {
    "Points": "points", "Assists": "assists",
    "Rebounds": "rebounds", "3PM": "three_pm",
}


def _get_home_away_split(player_name: str, stat_type: str, is_home: bool) -> float:
    """
    Query player_stats for the player's career home-avg minus away-avg on `stat_type`.
    Returns (home_avg - away_avg) * +1 when playing at home, -1 on the road.
    Falls back to 0.0 if the player or stat mapping is not found.
    """
    col = _HA_COL_MAP.get(stat_type)
    if not col:
        return 0.0
    player_id = get_player_id(player_name)
    if player_id is None:
        return 0.0
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        f"SELECT home, AVG({col}) FROM player_stats "
        f"WHERE personId = ? AND {col} IS NOT NULL GROUP BY home",
        (player_id,),
    ).fetchall()
    conn.close()
    avgs = {int(r[0]): float(r[1]) for r in rows if r[0] in (0, 1)}
    home_avg = avgs.get(1, 0.0)
    away_avg = avgs.get(0, 0.0)
    return (home_avg - away_avg) * (1 if is_home else -1)


def _get_wnba_home_away_split(player_name: str, stat_type: str, is_home: bool) -> float:
    col = _WNBA_HA_COL_MAP.get(stat_type)
    if not col:
        return 0.0
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        f"SELECT home, AVG({col}) FROM wnba_player_stats "
        f"WHERE player_name = ? AND {col} IS NOT NULL GROUP BY home",
        (player_name,),
    ).fetchall()
    conn.close()
    avgs = {int(r[0]): float(r[1]) for r in rows if r[0] in (0, 1)}
    home_avg = avgs.get(1, 0.0)
    away_avg = avgs.get(0, 0.0)
    return (home_avg - away_avg) * (1 if is_home else -1)


# ---------------------------------------------------------------------------
# Stat column mapping
# ---------------------------------------------------------------------------

STAT_COLUMNS = {
    "Points":             ["PTS"],
    "Assists":            ["AST"],
    "Rebounds":           ["REB"],
    "PRA":                ["PTS", "REB", "AST"],
    "3PM":                ["FG3M"],
    "PR":                 ["PTS", "REB"],
    "PA":                 ["PTS", "AST"],
    "RA":                 ["REB", "AST"],
    "Blocks":             ["BLK"],
    "Steals":             ["STL"],
    "Blocks+Steals":      ["BLK", "STL"],
    "Turnovers":          ["TOV"],
    "Offensive Rebounds": ["OREB"],
    "Defensive Rebounds": ["DREB"],
    "Double Double":      ["DD"],
    "3PA":                ["FG3A"],
}


def _compute_stat(df: pd.DataFrame, stat_type: str) -> pd.Series:
    cols = STAT_COLUMNS.get(stat_type, [])
    valid_cols = [c for c in cols if c in df.columns]
    if not valid_cols:
        return pd.Series([0.0] * len(df))
    return df[valid_cols].sum(axis=1).astype(float)


# ---------------------------------------------------------------------------
# ML feature computation  (training-consistent, season-avg proxy for hit_rate)
# ---------------------------------------------------------------------------

def _compute_ml_features(
    player_name: str,
    opponent_team: str,
    stat_type: str,
    stat_line: float,
    is_home: bool,
    is_back_to_back: bool,
) -> dict | None:
    """
    Compute the 19 features used during XGBoost training.
    Returns None if the player has fewer than 10 logged games.
    """
    df = get_game_logs(player_name, last_n=40)
    if df.empty or len(df) < 10:
        return None

    stat_values = _compute_stat(df, stat_type)

    avg5  = float(stat_values.head(5).mean())
    avg10 = float(stat_values.head(10).mean())

    # Season-avg proxy: mean of all available games in the log
    season_avg = float(stat_values.mean())
    hit_rate   = float((stat_values.head(10) > season_avg).mean())
    trend      = avg5 - avg10

    # "Trail Blazers" is two words so split()[-1] gives "Blazers" — handle explicitly.
    opp_nickname = "Trail Blazers" if "Blazers" in opponent_team else opponent_team.split()[-1]
    opp_games = (
        df[df["opponentteamName"].str.strip() == opp_nickname]
        if "opponentteamName" in df.columns
        else pd.DataFrame()
    )
    vs_opp = (
        float(_compute_stat(opp_games, stat_type).mean())
        if not opp_games.empty
        else avg10
    )

    # line_diff: how far avg10 sits above/below the actual prop line
    line_diff = avg10 - stat_line

    # consistency: std of the last 10 games in the log
    consistency = float(stat_values.head(10).std()) if len(stat_values) >= 2 else 0.0

    # games_played_last15: games within 15 days of the most recent game in the log
    if "GAME_DATE" in df.columns and len(df) > 0 and pd.notna(df["GAME_DATE"].iloc[0]):
        most_recent = df["GAME_DATE"].iloc[0]
        cutoff = most_recent - pd.Timedelta(days=15)
        games_last15 = int((df["GAME_DATE"] > cutoff).sum())
    else:
        games_last15 = 5

    # opp_def_rating_last5: opponent team's recent defensive quality
    opp_def = get_team_def_rating_last5(opponent_team)

    # is_playoff: are we currently in the 2025 post-season?
    is_playoff = int(datetime.now() > _PLAYOFF_START)

    # minutes_trend / avg_minutes_last5 from the player's game log (MIN column)
    if "MIN" in df.columns and df["MIN"].notna().any():
        min_vals = df["MIN"].dropna()
        avg_min5  = float(min_vals.head(5).mean())  if len(min_vals) >= 5  else float(min_vals.mean())
        avg_min10 = float(min_vals.head(10).mean()) if len(min_vals) >= 10 else float(min_vals.mean())
    else:
        avg_min5 = avg_min10 = 30.0
    minutes_trend     = avg_min5 - avg_min10
    avg_minutes_last5 = avg_min5

    # fatigue_score: total minutes in the 7 days before the most recent game.
    # Log is ordered DESC, so iloc[0] is the most recent game; we look at all
    # earlier rows (iloc[1:]) that fall within the 7-day window.
    if (
        "MIN" in df.columns
        and "GAME_DATE" in df.columns
        and len(df) > 1
        and pd.notna(df["GAME_DATE"].iloc[0])
    ):
        most_recent = df["GAME_DATE"].iloc[0]
        cutoff = most_recent - pd.Timedelta(days=7)
        prior = df.iloc[1:]
        fatigue_score = float(
            prior.loc[prior["GAME_DATE"] >= cutoff, "MIN"].sum()
        )
    else:
        fatigue_score = 0.0

    # home_away_split: direct DB query for career home/away avg split,
    # returned as a directional signal (+ve favours the current venue).
    home_away_split = _get_home_away_split(player_name, stat_type, is_home)

    return {
        "avg_pts_last5":        avg5,
        "avg_pts_last10":       avg10,
        "hit_rate_last10":      hit_rate,
        "trend":                trend,
        "vs_opponent_avg":      vs_opp,
        "line_diff":            line_diff,
        "consistency":          consistency,
        "games_played_last15":  float(games_last15),
        "opp_def_rating_last5": opp_def,
        "is_playoff":           is_playoff,
        "minutes_trend":        minutes_trend,
        "avg_minutes_last5":    avg_minutes_last5,
        "fatigue_score":        fatigue_score,
        "home_away_split":      home_away_split,
        "opp_game_count":       len(opp_games),
    }


# ---------------------------------------------------------------------------
# Heuristic feature computation  (retained for explanation text + fallback)
# ---------------------------------------------------------------------------

def compute_features(
    player_name: str,
    opponent_team: str,
    stat_line: float,
    stat_type: str,
    is_home: bool = False,
    is_back_to_back: bool = False,
) -> dict:
    df = get_game_logs(player_name, last_n=40)

    if df.empty:
        return _empty_features(stat_line)

    stat_values = _compute_stat(df, stat_type)

    last5  = stat_values.head(5)
    last10 = stat_values.head(10)

    avg5  = float(last5.mean())  if len(last5)  > 0 else 0.0
    avg10 = float(last10.mean()) if len(last10) > 0 else 0.0

    # Hit rate: % of all games in the window where stat exceeded the prop line
    hit_rate = float((stat_values > stat_line).mean()) if len(stat_values) > 0 else 0.5

    trend = avg5 - avg10

    opp_abbrev = full_to_abbrev(opponent_team)
    opp_games = (
        df[df["MATCHUP"].str.contains(opp_abbrev, na=False)]
        if "MATCHUP" in df.columns
        else pd.DataFrame()
    )
    if not opp_games.empty:
        opp_stat   = _compute_stat(opp_games, stat_type)
        avg_vs_opp = float(opp_stat.mean())
        opp_sample = len(opp_games)
    else:
        avg_vs_opp = avg10
        opp_sample = 0

    injury_note = get_injury_context(player_name)

    return {
        "avg5":          avg5,
        "avg10":         avg10,
        "hit_rate":      hit_rate,
        "trend":         trend,
        "is_home":       is_home,
        "avg_vs_opp":    avg_vs_opp,
        "opp_sample":    opp_sample,
        "opp_game_count": opp_sample,
        "b2b":           is_back_to_back,
        "injury_note":   injury_note,
        "stat_line":     stat_line,
        "games_played":  len(df),
    }


def _empty_features(stat_line: float) -> dict:
    return {
        "avg5": 0.0, "avg10": 0.0, "hit_rate": 0.5, "trend": 0.0,
        "is_home": False, "avg_vs_opp": 0.0, "opp_sample": 0,
        "b2b": False, "injury_note": "", "stat_line": stat_line, "games_played": 0,
    }


# ---------------------------------------------------------------------------
# Heuristic scorer
# ---------------------------------------------------------------------------

def score_features(features: dict) -> float:
    """Returns a raw score in (-inf, +inf).  Positive → OVER, negative → UNDER."""
    score = 0.0
    stat_line = features["stat_line"]

    avg5 = features["avg5"]
    if stat_line > 0:
        score += (avg5  - stat_line) / stat_line * 2.5

    avg10 = features["avg10"]
    if stat_line > 0:
        score += (avg10 - stat_line) / stat_line * 1.5

    score += (features["hit_rate"] - 0.5) * 3.0

    if stat_line > 0:
        score += features["trend"] / stat_line * 1.0

    if features["opp_sample"] >= 1 and stat_line > 0:
        rel_opp = (features["avg_vs_opp"] - stat_line) / stat_line
        weight  = min(features["opp_sample"] / 3.0, 1.0)
        score  += rel_opp * weight

    if features["is_home"]:
        score += 0.1
    if features["b2b"]:
        score -= 0.3
    if features["injury_note"]:
        score -= 0.4

    return score


def score_to_confidence(score: float) -> float:
    """Map raw score → confidence in [0.50, 0.95] via a scaled sigmoid."""
    sigmoid    = 1 / (1 + np.exp(-score * 1.2))
    confidence = 0.50 + sigmoid * 0.45
    return round(float(np.clip(confidence, 0.50, 0.95)), 4)


# ---------------------------------------------------------------------------
# Explanation generator  (template-based, no LLM)
# ---------------------------------------------------------------------------

def build_explanation(
    player_name: str,
    stat_type: str,
    stat_line: float,
    opponent_team: str,
    features: dict,
    outcome: str,
) -> str:
    parts      = []
    first_name = player_name.split()[0]

    parts.append(
        f"{player_name} is averaging {features['avg5']:.1f} {stat_type} over the last 5 games "
        f"and {features['avg10']:.1f} over the last 10 games."
    )

    hit_pct = int(features["hit_rate"] * 10)
    parts.append(
        f"{first_name} has exceeded the {stat_line} line in {hit_pct} of the last 10 games."
    )

    if features.get("opp_game_count", 0) > 0:
        parts.append(
            f"{first_name} averages {features['avg_vs_opp']:.1f} {stat_type} "
            f"against {opponent_team} historically."
        )
    else:
        parts.append(
            f"No recent matchup data was found against {opponent_team}; "
            f"the overall season average is used as a proxy."
        )

    context_parts = []
    if features["b2b"]:
        context_parts.append("tonight is a back-to-back game, which may affect fatigue")
    venue = "at home" if features["is_home"] else "on the road"
    context_parts.append(f"{first_name} will be playing {venue}")
    parts.append(f"Contextually, {' and '.join(context_parts)}.")

    if features["injury_note"]:
        parts.append(f"Injury note — {features['injury_note']}.")
    else:
        trend_agrees = (outcome == "OVER" and features["trend"] > 0) or (
            outcome == "UNDER" and features["trend"] < 0
        )
        if trend_agrees:
            trend_word = "upward" if features["trend"] > 0 else "downward"
            parts.append(
                f"{first_name}'s recent trend is {trend_word} "
                f"(5-game avg {'above' if features['trend'] > 0 else 'below'} 10-game avg), "
                f"supporting the {outcome} projection."
            )

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def predict(
    player_name: str,
    opponent_team: str,
    stat_line: float,
    stat_type: str,
    is_home: bool = False,
    is_back_to_back: bool = False,
) -> dict:
    # Heuristic features — always computed for the explanation text
    features = compute_features(
        player_name, opponent_team, stat_line, stat_type,
        is_home=is_home, is_back_to_back=is_back_to_back,
    )

    if features["games_played"] == 0:
        return {
            "outcome": "OVER",
            "confidence": 0.52,
            "explanation": (
                f"Insufficient game log data found for {player_name} this season. "
                "The prediction is based on limited information and should be treated with caution."
            ),
        }

    models  = _load_models()
    use_ml  = stat_type in _ML_STAT_TYPES and stat_type in models

    if use_ml:
        ml_feats = _compute_ml_features(
            player_name, opponent_team, stat_type, stat_line, is_home, is_back_to_back
        )
        if ml_feats is not None:
            model  = models[stat_type]
            X      = pd.DataFrame([ml_feats])[FEATURE_COLS]
            proba  = model.predict_proba(X)[0]
            p_over = float(proba[1])
            outcome    = "OVER" if p_over > 0.5 else "UNDER"
            confidence = round(float(max(proba)), 4)
        else:
            use_ml = False  # not enough games — fall through to heuristic

    if not use_ml:
        raw_score  = score_features(features)
        outcome    = "OVER" if raw_score >= 0 else "UNDER"
        confidence = score_to_confidence(abs(raw_score))

    explanation = build_explanation(
        player_name, stat_type, stat_line, opponent_team, features, outcome
    )

    return {
        "outcome":     outcome,
        "confidence":  confidence,
        "explanation": explanation,
    }


# ---------------------------------------------------------------------------
# WNBA prediction
# ---------------------------------------------------------------------------

def _compute_wnba_ml_features(
    player_name: str,
    opponent_team: str,
    stat_type: str,
    stat_line: float,
    is_home: bool,
    is_back_to_back: bool,
) -> dict | None:
    df = get_wnba_game_logs(player_name, last_n=40)
    if df.empty or len(df) < 10:
        return None

    stat_values = _compute_stat(df, stat_type)

    avg5  = float(stat_values.head(5).mean())
    avg10 = float(stat_values.head(10).mean())
    season_avg = float(stat_values.mean())
    hit_rate   = float((stat_values.head(10) > season_avg).mean())
    trend      = avg5 - avg10

    # Opponent matching via opponent_team column (already a full team name)
    opp_games = (
        df[df["opponent_team"].str.lower() == opponent_team.lower()]
        if "opponent_team" in df.columns
        else pd.DataFrame()
    )
    vs_opp = (
        float(_compute_stat(opp_games, stat_type).mean())
        if not opp_games.empty
        else avg10
    )

    line_diff   = avg10 - stat_line
    consistency = float(stat_values.head(10).std()) if len(stat_values) >= 2 else 0.0

    if "GAME_DATE" in df.columns and len(df) > 0 and pd.notna(df["GAME_DATE"].iloc[0]):
        most_recent = df["GAME_DATE"].iloc[0]
        cutoff = most_recent - pd.Timedelta(days=15)
        games_last15 = int((df["GAME_DATE"] > cutoff).sum())
    else:
        games_last15 = 5

    # opp_def_rating_last5 not available for WNBA — use neutral
    opp_def = 0.0

    # WNBA playoffs start in September
    is_playoff = int(datetime.now().month >= 9)

    if "MIN" in df.columns and df["MIN"].notna().any():
        min_vals  = df["MIN"].dropna()
        avg_min5  = float(min_vals.head(5).mean())  if len(min_vals) >= 5  else float(min_vals.mean())
        avg_min10 = float(min_vals.head(10).mean()) if len(min_vals) >= 10 else float(min_vals.mean())
    else:
        avg_min5 = avg_min10 = 25.0
    minutes_trend     = avg_min5 - avg_min10
    avg_minutes_last5 = avg_min5

    if (
        "MIN" in df.columns
        and "GAME_DATE" in df.columns
        and len(df) > 1
        and pd.notna(df["GAME_DATE"].iloc[0])
    ):
        most_recent = df["GAME_DATE"].iloc[0]
        cutoff = most_recent - pd.Timedelta(days=7)
        prior = df.iloc[1:]
        fatigue_score = float(
            prior.loc[prior["GAME_DATE"] >= cutoff, "MIN"].sum()
        )
    else:
        fatigue_score = 0.0

    home_away_split = _get_wnba_home_away_split(player_name, stat_type, is_home)

    return {
        "avg_pts_last5":        avg5,
        "avg_pts_last10":       avg10,
        "hit_rate_last10":      hit_rate,
        "trend":                trend,
        "vs_opponent_avg":      vs_opp,
        "line_diff":            line_diff,
        "consistency":          consistency,
        "games_played_last15":  float(games_last15),
        "opp_def_rating_last5": opp_def,
        "is_playoff":           is_playoff,
        "minutes_trend":        minutes_trend,
        "avg_minutes_last5":    avg_minutes_last5,
        "fatigue_score":        fatigue_score,
        "home_away_split":      home_away_split,
        "opp_game_count":       len(opp_games),
    }


def compute_wnba_features(
    player_name: str,
    opponent_team: str,
    stat_line: float,
    stat_type: str,
    is_home: bool = False,
    is_back_to_back: bool = False,
) -> dict:
    df = get_wnba_game_logs(player_name, last_n=40)

    if df.empty:
        return _empty_features(stat_line)

    stat_values = _compute_stat(df, stat_type)

    last5  = stat_values.head(5)
    last10 = stat_values.head(10)

    avg5  = float(last5.mean())  if len(last5)  > 0 else 0.0
    avg10 = float(last10.mean()) if len(last10) > 0 else 0.0
    hit_rate = float((stat_values > stat_line).mean()) if len(stat_values) > 0 else 0.5
    trend = avg5 - avg10

    opp_games = (
        df[df["opponent_team"].str.lower() == opponent_team.lower()]
        if "opponent_team" in df.columns
        else pd.DataFrame()
    )
    if not opp_games.empty:
        opp_stat   = _compute_stat(opp_games, stat_type)
        avg_vs_opp = float(opp_stat.mean())
        opp_sample = len(opp_games)
    else:
        avg_vs_opp = avg10
        opp_sample = 0

    injury_info = get_wnba_injury_status(player_name)
    injury_note = injury_info.get("status", "") if injury_info else ""

    return {
        "avg5":           avg5,
        "avg10":          avg10,
        "hit_rate":       hit_rate,
        "trend":          trend,
        "is_home":        is_home,
        "avg_vs_opp":     avg_vs_opp,
        "opp_sample":     opp_sample,
        "opp_game_count": opp_sample,
        "b2b":            is_back_to_back,
        "injury_note":    injury_note,
        "stat_line":      stat_line,
        "games_played":   len(df),
    }


def predict_wnba(
    player_name: str,
    opponent_team: str,
    stat_line: float,
    stat_type: str,
    is_home: bool = False,
    is_back_to_back: bool = False,
) -> dict:
    features = compute_wnba_features(
        player_name, opponent_team, stat_line, stat_type,
        is_home=is_home, is_back_to_back=is_back_to_back,
    )

    if features["games_played"] == 0:
        return {
            "outcome": "OVER",
            "confidence": 0.52,
            "explanation": (
                f"Insufficient game log data found for {player_name} in WNBA. "
                "The prediction is based on limited information and should be treated with caution."
            ),
        }

    models  = _load_wnba_models()
    use_ml  = stat_type in _WNBA_ML_STAT_TYPES and stat_type in models

    if use_ml:
        ml_feats = _compute_wnba_ml_features(
            player_name, opponent_team, stat_type, stat_line, is_home, is_back_to_back
        )
        if ml_feats is not None:
            model  = models[stat_type]
            X      = pd.DataFrame([ml_feats])[FEATURE_COLS]
            proba  = model.predict_proba(X)[0]
            p_over = float(proba[1])
            outcome    = "OVER" if p_over > 0.5 else "UNDER"
            confidence = round(float(max(proba)), 4)
        else:
            use_ml = False

    if not use_ml:
        raw_score  = score_features(features)
        outcome    = "OVER" if raw_score >= 0 else "UNDER"
        confidence = score_to_confidence(abs(raw_score))

    explanation = build_explanation(
        player_name, stat_type, stat_line, opponent_team, features, outcome
    )

    return {
        "outcome":     outcome,
        "confidence":  confidence,
        "explanation": explanation,
    }
