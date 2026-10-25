"""
Challenger Stage 1: the feature builder gives identical values for a training row and
for live inference of the same game (synthetic DB with trades, long absences, same-time
tips and 0-minute rows); the data guard refuses 2026-27 (Stage 2 guard); NB helpers behave. A last test
runs challenger_predict on the real predictions.db if the dev model file exists.
"""
import datetime as dt
import os
import sqlite3
import sys

import numpy as np
import pandas as pd
import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

from challenger import features as cf          # noqa: E402
from challenger import guard, inference          # noqa: E402
from challenger import model as cm               # noqa: E402

TEAMS = [1610612738, 1610612752, 1610612748, 1610612741, 1610612747, 1610612744]


def _db(path, seed=3):
    """Two synthetic seasons (2023-24, 2024-25) plus one 2026-27 game that must never be read."""
    rng = np.random.default_rng(seed)
    roster = {tm: [10_000 + i * 10 + k for k in range(6)] for i, tm in enumerate(TEAMS)}
    rows, sched = [], []
    gid = 22300001
    for season_start in (dt.datetime(2023, 10, 24), dt.datetime(2024, 10, 22)):
        day = season_start
        for g in range(60):
            day += dt.timedelta(days=int(rng.choice([1, 1, 2])))
            order = rng.permutation(len(TEAMS))
            for pair in range(3):                        # 3 games per date, two at the same tip
                h, a = TEAMS[order[2 * pair]], TEAMS[order[2 * pair + 1]]
                tip = day.replace(hour=19 if pair < 2 else 21, minute=30)
                if season_start.year == 2024 and g == 30 and pair == 0:    # a trade
                    p = roster[h].pop()
                    roster[a].append(p)
                sched.append((gid, tip, h, a))
                for home, tm, opp in ((1, h, a), (0, a, h)):
                    for p in roster[tm]:
                        absent = (p % 7 == 0) and 20 <= g < 28          # a long absence
                        if absent or rng.random() < 0.05:
                            continue
                        mins = 0.0 if rng.random() < 0.05 else float(rng.uniform(5, 40))
                        rows.append(_row(p, gid, tip, "Regular Season", home, tm, opp, mins, rng))
                gid += 1
        gid = 22400001
    rows.append(_row(roster[TEAMS[0]][0], 22600001, dt.datetime(2026, 10, 22, 19, 30), "Regular Season",
                     1, TEAMS[0], TEAMS[1], 30.0, rng))
    rows.append(_row(roster[TEAMS[0]][1], 22300999, dt.datetime(2024, 1, 5, 19, 0), "Preseason",
                     1, TEAMS[0], TEAMS[1], 30.0, rng))
    df = pd.DataFrame(rows)
    df["playerteamId"] = df["playerteamId"].astype("Int64")
    df["opponentteamId"] = df["opponentteamId"].astype("Int64")
    nulls = df["gameId"].between(22300010, 22300060)          # Kaggle's NULL team ids (2021-22)
    df.loc[nulls, ["playerteamId", "opponentteamId"]] = pd.NA
    conn = sqlite3.connect(path)
    df.to_sql("player_stats", conn, index=False)
    pd.DataFrame([{"gameId": g, "gameDateTimeEst": t.strftime("%Y-%m-%d %H:%M:%S"), "homeTeamId": h,
                   "awayTeamId": a} for g, t, h, a in sched]).to_sql("schedule", conn, index=False)
    conn.close()


def _row(p, gid, tip, gtype, home, tm, opp, mins, rng):
    return {"personId": p, "gameId": gid, "gameDateTimeEst": tip.strftime("%Y-%m-%d %H:%M:%S"),
            "gameType": gtype, "home": home, "playerteamId": tm, "opponentteamId": opp,
            "playerteamCity": f"C{tm}", "playerteamName": f"N{tm}", "opponentteamCity": f"C{opp}",
            "opponentteamName": f"N{opp}",
            "numMinutes": mins, "points": float(rng.integers(0, 35)), "reboundsTotal": float(rng.integers(0, 14)),
            "assists": float(rng.integers(0, 10)), "threePointersMade": float(rng.integers(0, 6)),
            "threePointersAttempted": float(rng.integers(0, 12)), "usagePercentage": float(rng.uniform(0.1, 0.35))}


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "synthetic.db")
    _db(path)
    return path


def test_loader_never_reads_reserved_seasons(db):
    conn = sqlite3.connect(db)
    df = cf.load_all(conn)
    conn.close()
    assert df["t"].max() < pd.Timestamp(guard.DATA_END)
    assert not set(df["season"]) & guard.FORBIDDEN_SEASONS
    assert (df["gameId"] != 22300999).all()                     # preseason excluded
    with pytest.raises(AssertionError):
        guard.assert_dev_only(pd.Series(pd.to_datetime(["2026-10-22 19:30:00"])))


def test_inference_features_equal_training_frame(db):
    conn = sqlite3.connect(db)
    df = cf.load_all(conn)
    F = cf.batch_features(df)
    cols = [c for c in F.columns if c not in ("personId", "gameId", "t", "season")]
    assert (F["games_since_absence"] == 0).any() and (F["missed_prev"] >= cf.ABSENCE_GAMES).any()
    checked = 0
    for i in range(0, len(F), 7):
        Fi, _ = inference.features_before_tip(conn, int(F.at[i, "personId"]), int(F.at[i, "gameId"]))
        for c in cols:
            a, b = F.at[i, c], Fi.at[0, c]
            assert (pd.isna(a) and pd.isna(b)) or a == b, (i, c, a, b)
        checked += 1
    conn.close()
    assert checked > 500


def test_inference_refuses_reserved_season(db):
    conn = sqlite3.connect(db)
    with pytest.raises(ValueError):
        inference.features_before_tip(conn, 10_000, 22600001)
    conn.close()
    with pytest.raises(ValueError):
        guard.assert_dev_tip("2026-10-22 19:30:00")
    guard.assert_dev_tip("2026-04-12 15:30:00")


def test_features_use_only_earlier_games(db):
    """Changing a game's stats changes no feature of that game (or of same-time games)."""
    conn = sqlite3.connect(db)
    df = cf.load_all(conn)
    conn.close()
    F = cf.batch_features(df)
    g = df["gameId"].iloc[len(df) // 2]
    tip = df.loc[df["gameId"] == g, "t"].iloc[0]
    df2 = df.copy()
    m = df2["t"] >= tip
    for c in ["numMinutes", *cf.RATE_COLS, "usagePercentage"]:
        df2.loc[m, c] = df2.loc[m, c] * 0 + 3.0
    F2 = cf.batch_features(df2)
    same = (df["t"] <= tip).values
    pd.testing.assert_frame_equal(F[same].reset_index(drop=True), F2[same].reset_index(drop=True))


def test_nb_helpers():
    mu, alpha = np.array([2.0, 20.0]), 0.1
    y = np.arange(0, 400)
    for m in mu:
        assert abs(np.exp(cm.nb_logpmf(y, np.full(len(y), m), alpha)).sum() - 1) < 1e-9
        assert abs(cm.p_over(m, alpha, 9.5) - np.exp(cm.nb_logpmf(y[y > 9.5], m, alpha)).sum()) < 1e-9
    assert cm.p_over(20.0, alpha, 19.5) == cm.p_over(20.0, alpha, 19)     # P(Y > 19.5) = P(Y > 19)
    rng = np.random.default_rng(0)
    m = rng.uniform(1, 20, 20000)
    r = 1 / 0.15
    ys = rng.negative_binomial(r, r / (r + m))
    assert abs(cm.nb_alpha_mle(ys, m) - 0.15) < 0.02


def test_rungs():
    from challenger.stage1 import rungs
    below, above = rungs(np.array([3.0, 4.5, 17.0, 19.5, 0.0]))
    assert np.isnan(below[0]) and above[0] == 4.5
    assert below[1] == 4.5 and above[1] == 9.5
    assert below[2] == 14.5 and above[2] == 19.5
    assert below[3] == 19.5 and above[3] == 24.5
    assert above[4] == 4.5


@pytest.mark.skipif(not (os.path.exists(inference.DEV_MODEL) and os.path.exists(inference.DB)),
                    reason="needs predictions.db and the Stage 1 dev model file")
def test_challenger_predict_real_db():
    # the top scorer on 2025-01-15 (a 2024-25 regular-season game)
    conn = sqlite3.connect(f"file:{inference.DB}?mode=ro", uri=True)
    r = conn.execute("SELECT personId, gameId FROM player_stats WHERE gameType = 'Regular Season' "
                     "AND numMinutes > 0 AND gameDateTimeEst >= '2025-01-15' AND gameDateTimeEst < '2025-01-16' "
                     "ORDER BY points DESC LIMIT 1").fetchone()
    conn.close()
    out = inference.challenger_predict(r[0], "Points", r[1], 24.5)
    assert 0 < out["p_over"] < 1 and out["mu"] > 0
    assert out["data_as_of"] < out["tip"]
    with pytest.raises(ValueError):
        inference.challenger_predict(r[0], "Points", 22600101, 24.5)       # a 2026-27 game
