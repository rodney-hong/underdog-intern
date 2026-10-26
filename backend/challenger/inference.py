"""
inference.py  (challenger, Stage 1)

challenger_predict(person_id, stat, game_id, line): P(stat > line) for one player-game,
from data that tipped before the game only. Reads predictions.db read-only.

The game's context (tip time, team, opponent, home) is looked up only among games
before guard.DATA_END: from the player's box-score row for that game, metadata columns
only, or, when there is none (a future game or a DNP), from the schedule table and the
player's most recent team. The stats of the target game are never read. Then the same
build_features() as training runs on:
  - the player's whole career before tip;
  - team games from his 11th-to-last game on (absence features);
  - allowed totals far enough back to hold the opponent's last OPP_GAMES games and the
    league's last LEAGUE_TEAM_GAMES team-games.
Stage 1 refuses any game in 2025-26 or later (guard.assert_dev_tip).
"""

import os
import sqlite3

import numpy as np
import pandas as pd

from . import model as cmodel
from .features import (LOAD_COLS, OPP_GAMES, LEAGUE_TEAM_GAMES, STAT_COLS, allowed_table,
                       build_features, load_team_map, prep, team_games_of)
from .guard import DATA_END, REGULAR_SQL, assert_dev_tip, season_of_one

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "..", "predictions.db")
MODEL_DIR = os.path.join(HERE, "models")
DEV_MODEL = os.path.join(MODEL_DIR, "challenger_stage1_dev.pkl")      # trained through 2023-24

_cache = {}


def _connect(db_path: str):
    return sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True)


def game_context(conn, game_id: int, person_id: int) -> dict:
    r = conn.execute("SELECT gameDateTimeEst, home, playerteamId, opponentteamId, gameType, "
                     "playerteamCity, playerteamName, opponentteamCity, opponentteamName "
                     "FROM player_stats WHERE gameId = ? AND personId = ? AND gameDateTimeEst < ?",
                     (int(game_id), int(person_id), DATA_END)).fetchone()
    if r is not None:
        tip, home, team, opp, gtype, pc, pn, oc, on = r
        tm = load_team_map(conn)
        team = team if team is not None else tm.get((pc, pn))
        opp = opp if opp is not None else tm.get((oc, on))
        if gtype not in ("Regular Season", "NBA Emirates Cup"):
            raise ValueError(f"game {game_id} is {gtype}, not a regular-season game")
        return {"tip": tip, "home": int(home), "team": int(team), "opp": int(opp)}
    s = conn.execute("SELECT gameDateTimeEst, homeTeamId, awayTeamId FROM schedule WHERE gameId = ? "
                     "AND gameDateTimeEst < ?", (int(game_id), DATA_END)).fetchone()
    if s is None:
        raise ValueError(f"game {game_id} not found for player {person_id} before {DATA_END} "
                         "(Stage 1 reads no game on/after it)")
    tip, home_id, away_id = s
    assert_dev_tip(tip)
    t = conn.execute(f"SELECT playerteamId, playerteamCity, playerteamName FROM player_stats WHERE personId = ? AND {REGULAR_SQL} "
                     "AND gameDateTimeEst < ? ORDER BY gameDateTimeEst DESC LIMIT 1",
                     (int(person_id), tip)).fetchone()
    team = None if t is None else (t[0] if t[0] is not None else load_team_map(conn).get((t[1], t[2])))
    if team is None or int(team) not in (int(home_id), int(away_id)):
        raise ValueError(f"player {person_id}'s latest team is not in game {game_id}")
    team = int(team)
    return {"tip": tip, "home": int(team == int(home_id)), "team": team,
            "opp": int(away_id) if team == int(home_id) else int(home_id)}


def _rows(conn, where: str, params: tuple) -> pd.DataFrame:
    return pd.read_sql_query(f"SELECT {', '.join(LOAD_COLS)} FROM player_stats WHERE {REGULAR_SQL} "
                             f"AND {where}", conn, params=params)


def _team_rows(conn, hist: pd.DataFrame, team: int, team_from: str, tip: str) -> pd.DataFrame:
    """
    Rows for the absence features (team_games_of -> _absence_block): played rows in
    [team_from, tip) of the teams the player's own rows can carry, i.e. his history's teams
    after prep() fills NULL ids from load_team_map, plus ctx["team"]. _absence_block only
    looks up teams present in his rows and skips every other team, so loading other teams
    changes nothing; it only cost memory (team_from = "0000" for a player with fewer than
    11 earlier games meant every regular-season row since 1946, ~1 GB). The SQL filter
    keeps exactly the rows prep() would map to those teams: an id among them, or a NULL id
    whose (city, name) load_team_map maps to one of them (migration plan v2, step 0.9;
    tests/test_challenger_memory.py and experiments/challenger_memory_parity.py check
    identical output).
    """
    team_map = load_team_map(conn)
    teams = {int(team)} | set(prep(hist, team_map)["playerteamId"].astype("int64").tolist())
    names = [k for k, v in team_map.items() if v in teams]
    by_name = "".join(" OR (playerteamId IS NULL AND playerteamCity = ? AND playerteamName = ?)"
                      for _ in names)
    ids = ", ".join("?" * len(teams))
    return _rows(conn, f"gameDateTimeEst >= ? AND gameDateTimeEst < ? AND (playerteamId IN ({ids}){by_name})",
                 (team_from, tip, *sorted(teams), *[x for k in names for x in k]))


def features_before_tip(conn, person_id: int, game_id: int) -> tuple[pd.DataFrame, dict]:
    """The target game's feature row, built from rows that tipped before it (guarded)."""
    ctx = game_context(conn, game_id, person_id)
    assert_dev_tip(ctx["tip"])
    return features_for_context(conn, person_id, {**ctx, "game_id": int(game_id)})


def features_for_context(conn, person_id: int, ctx: dict) -> tuple[pd.DataFrame, dict]:
    """
    The feature row for player person_id in a game described by ctx: tip ('YYYY-MM-DD
    HH:MM:SS', ET), home (0/1), team and opp (team ids), game_id. Reads only rows that
    tipped before ctx["tip"]. Applies no season guard itself: features_before_tip (research)
    guards; challenger/live.py (live inference, from Stage 3) passes the context of the
    upcoming game.
    """
    tip, game_id = ctx["tip"], ctx.get("game_id", 0)
    hist = _rows(conn, "personId = ? AND gameDateTimeEst < ?", (int(person_id), tip))
    times = sorted(hist["gameDateTimeEst"])
    team_from = times[-11] if len(times) >= 11 else "0000"
    team_rows = _team_rows(conn, hist, ctx["team"], team_from, tip)
    lg = conn.execute(f"SELECT gameDateTimeEst FROM (SELECT DISTINCT gameId, gameDateTimeEst FROM player_stats "
                      f"WHERE {REGULAR_SQL} AND gameDateTimeEst < ?) ORDER BY gameDateTimeEst DESC "
                      "LIMIT 1 OFFSET ?", (tip, LEAGUE_TEAM_GAMES)).fetchone()
    names = [k for k, v in load_team_map(conn).items() if v == ctx["opp"]]
    by_name = "".join(" OR (opponentteamId IS NULL AND opponentteamCity = ? AND opponentteamName = ?)"
                      for _ in names)
    op = conn.execute(f"SELECT gameDateTimeEst FROM (SELECT DISTINCT gameId, gameDateTimeEst FROM player_stats "
                      f"WHERE {REGULAR_SQL} AND (opponentteamId = ?{by_name}) AND gameDateTimeEst < ?) "
                      "ORDER BY gameDateTimeEst DESC LIMIT 1 OFFSET ?",
                      (ctx["opp"], *[x for k in names for x in k], tip, OPP_GAMES)).fetchone()
    allowed_from = min(lg[0] if lg else "0000", op[0] if op else "0000")
    league = _rows(conn, "gameDateTimeEst >= ? AND gameDateTimeEst < ?", (allowed_from, tip))

    team_map = load_team_map(conn)
    hist, league, team_rows = prep(hist, team_map), prep(league, team_map), prep(team_rows, team_map)
    t = pd.Timestamp(tip)
    target = pd.DataFrame({"personId": [int(person_id)], "gameId": [int(game_id)], "gameDateTimeEst": [tip],
                           "home": [ctx["home"]], "playerteamId": [ctx["team"]],
                           "opponentteamId": [ctx["opp"]], "t": [t], "season": [season_of_one(t)]})
    for c in ("personId", "gameId", "home", "playerteamId", "opponentteamId"):
        target[c] = target[c].astype("int64")
    F = build_features(hist, target, team_games_of(team_rows), allowed_table(league))
    ctx = {**ctx, "data_as_of": str(hist["t"].max()) if len(hist) else None}
    return F, ctx


def challenger_predict(person_id: int, stat: str, game_id: int, line: float, *,
                       db_path: str = DB, model_path: str = DEV_MODEL,
                       expected_sha: str | None = None) -> dict:
    """
    P(stat > line) and the NB distribution behind it, for player person_id in game_id,
    using only games that tipped before it. stat: Points, Rebounds, Assists or 3PM.
    model_path defaults to the Stage 1 development model (trained through 2023-24);
    expected_sha, if given, must match the model file's SHA-256.
    """
    if stat not in STAT_COLS:
        raise ValueError(f"stat must be one of {list(STAT_COLS)}")
    key = (model_path, expected_sha)
    if key not in _cache:
        _cache[key] = (cmodel.load(model_path, expected_sha), cmodel.sha256(model_path))
    m, sha = _cache[key]
    conn = _connect(db_path)
    try:
        F, ctx = features_before_tip(conn, person_id, game_id)
    finally:
        conn.close()
    pred = m.predict(F, stat)
    mu, alpha = float(pred["mu"][0]), float(m.alpha[stat])
    return {"person_id": int(person_id), "game_id": int(game_id), "stat": stat, "line": float(line),
            "p_over": float(cmodel.p_over(mu, alpha, line)), "mu": mu, "alpha": alpha,
            "expected_minutes": float(pred["minutes"][0]), "rate_per_minute": float(pred["rate"][0]),
            "tip": ctx["tip"], "data_as_of": ctx["data_as_of"], "model_sha256": sha,
            "features": {k: (None if pd.isna(v) else float(v)) for k, v in
                         F.iloc[0][m.minutes_feats + m.rate_feats[stat]].items()}}
