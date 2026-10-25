"""
features.py  (challenger, Stage 1)

One feature builder for training and for live inference. build_features() takes
  player_rows  played regular-season rows (one or many players), the history;
  targets      None (training: every row of player_rows is a target), or placeholder
               rows for games not in player_rows (inference: the upcoming game);
  team_games   distinct (playerteamId, gameId, t) of team games, for absences;
  allowed      allowed_table(): per (game, defending team) totals of each stat.
Every feature of a target uses only rows that tip strictly before it (closed-left):
per-player transforms shift by one row, and team/league lookups search for t < target.

Floats match bit for bit between the two paths: per-player EWMAs run the same pandas
groupby-ewm over the same prefix of the player's career, and opponent/league windows
are integer sums divided once. tests/test_challenger.py checks it on a synthetic DB;
stage1.py checks it on real 2023-24/2024-25 rows.

Features (h = EWMA half-life in games, 3 / 10 / 30):
  min_h{h}              EWMA of minutes
  rate_{col}_h{h}       EWMA(stat) / EWMA(minutes), per-minute production
  usage_h{h}            EWMA of usagePercentage
  rest_days, b2b        calendar days since the previous game (capped), back-to-back
  missed_prev           team games missed since his previous game (capped)
  games_since_absence   games played since the last return from >= ABSENCE_GAMES
                        missed team games, the return game counting 0 (capped)
  opp_rel_{col}         opponent's mean allowed stat over its last OPP_GAMES games
                        (played rows) / league mean over the last LEAGUE_TEAM_GAMES
                        team-games
  home, n_prior, experience
  avg10_{col}, median20_{col}   for baseline (a) and the evaluation lines only
"""

import numpy as np
import pandas as pd

from .guard import REGULAR_SQL, DATA_END, assert_dev_only, season_of

STAT_COLS = {"Points": "points", "Rebounds": "reboundsTotal", "Assists": "assists",
             "3PM": "threePointersMade"}
RATE_COLS = list(STAT_COLS.values()) + ["threePointersAttempted"]
LOAD_COLS = ["personId", "gameId", "gameDateTimeEst", "home", "playerteamId", "opponentteamId",
             "playerteamCity", "playerteamName", "opponentteamCity", "opponentteamName",
             "numMinutes", *RATE_COLS, "usagePercentage"]

HALFLIVES = (3, 10, 30)
REST_CAP = 10
MISSED_CAP = 20
ABSENCE_GAMES = 5
ABSENCE_CAP = 10
OPP_GAMES = 5
LEAGUE_TEAM_GAMES = 300


# --- loading ------------------------------------------------------------------------

_team_maps = {}


def load_team_map(conn) -> dict:
    """(city, name) -> team id, from rows before DATA_END that carry ids. Kaggle has NULL
    team ids for all of 2021-22 and part of 2000-01; prep() fills them from this map.
    Cached per database file."""
    key = conn.execute("PRAGMA database_list").fetchone()[2]
    if key and key in _team_maps:
        return _team_maps[key]
    rows = conn.execute("SELECT playerteamCity, playerteamName, playerteamId FROM player_stats "
                        "WHERE playerteamId IS NOT NULL AND gameDateTimeEst < ? "
                        "GROUP BY 1, 2, 3", (DATA_END,)).fetchall()
    ids = {}
    for city, name, tid in rows:
        ids.setdefault((city, name), set()).add(int(tid))
    out = {k: next(iter(v)) for k, v in ids.items() if len(v) == 1}
    if key:
        _team_maps[key] = out
    return out


def fill_team_ids(df: pd.DataFrame, team_map: dict, side: str = "player") -> pd.Series:
    col = f"{side}teamId"
    miss = df[col].isna()
    if not miss.any():
        return df[col]
    keys = list(zip(df.loc[miss, f"{side}teamCity"], df.loc[miss, f"{side}teamName"]))
    out = df[col].copy()
    out[miss] = [team_map.get(k, np.nan) for k in keys]
    return out


def prep(df: pd.DataFrame, team_map: dict) -> pd.DataFrame:
    df = df.copy()
    df["t"] = pd.to_datetime(df["gameDateTimeEst"], errors="coerce")
    df = df.dropna(subset=["t"])
    df["season"] = season_of(df["t"])
    df["playerteamId"] = fill_team_ids(df, team_map, "player")
    df["opponentteamId"] = fill_team_ids(df, team_map, "opponent")
    df = df.dropna(subset=["playerteamId", "opponentteamId"])
    for c in ("personId", "gameId", "playerteamId", "opponentteamId"):
        df[c] = df[c].astype("int64")
    df["home"] = df["home"].fillna(0).astype("int64")
    for c in ["numMinutes", *RATE_COLS, "usagePercentage"]:
        df[c] = df[c].astype("float64")
    return df.sort_values(["personId", "t", "gameId"], kind="mergesort").reset_index(drop=True)


def load_all(conn, end: str = DATA_END) -> pd.DataFrame:
    """Every played regular-season row before `end` (never later than DATA_END)."""
    end = min(end, DATA_END)
    df = pd.read_sql_query(f"SELECT {', '.join(LOAD_COLS)} FROM player_stats WHERE {REGULAR_SQL} "
                           "AND gameDateTimeEst < ?", conn, params=(end,))
    df = prep(df, load_team_map(conn))
    assert_dev_only(df["t"], df["season"])
    return df


def team_games_of(rows: pd.DataFrame) -> pd.DataFrame:
    return rows[["playerteamId", "gameId", "t"]].drop_duplicates(["playerteamId", "gameId"])


def allowed_table(rows: pd.DataFrame) -> pd.DataFrame:
    """Per (game, defending team): the stat totals of the players who faced it (played rows)."""
    cols = list(STAT_COLS.values())
    a = (rows.groupby(["gameId", "opponentteamId"], sort=False)
         .agg(t=("t", "min"), **{c: (c, "sum") for c in cols}).reset_index()
         .rename(columns={"opponentteamId": "def_team"}))
    for c in cols:
        a[c] = a[c].round().astype("int64")        # integer counts: sums are exact
    return a.sort_values(["t", "gameId", "def_team"], kind="mergesort").reset_index(drop=True)


# --- feature blocks -----------------------------------------------------------------

def _player_block(d: pd.DataFrame) -> pd.DataFrame:
    pid = d["personId"]
    g = d.groupby(pid, sort=False)
    out = pd.DataFrame(index=d.index)
    out["n_prior"] = g.cumcount().astype("float64")
    cols = ["numMinutes", *RATE_COLS, "usagePercentage"]
    prev = g[cols].shift(1)
    for h in HALFLIVES:
        e = prev.groupby(pid, sort=False).ewm(halflife=h).mean().droplevel(0).reindex(d.index)
        out[f"min_h{h}"] = e["numMinutes"]
        for c in RATE_COLS:
            out[f"rate_{c}_h{h}"] = e[c] / e["numMinutes"]
        out[f"usage_h{h}"] = e["usagePercentage"]
    stats = list(STAT_COLS.values())
    roll = prev[stats].groupby(pid, sort=False)
    a10 = roll.rolling(10, min_periods=1).mean().droplevel(0).reindex(d.index)
    m20 = roll.rolling(20, min_periods=5).median().droplevel(0).reindex(d.index)
    for c in stats:
        out[f"avg10_{c}"] = a10[c]
        out[f"median20_{c}"] = m20[c]
    day = d["t"].dt.normalize()
    rest = (day - day.groupby(pid, sort=False).shift(1)).dt.days
    out["rest_days"] = rest.clip(upper=REST_CAP).astype("float64")
    out["b2b"] = (rest == 1).astype("float64")
    out["home"] = d["home"].astype("float64")
    start = d["season"].str[:4].astype(int)
    out["experience"] = (start - start.groupby(pid, sort=False).transform("min")).astype("float64")
    return out


def _absence_block(d: pd.DataFrame, team_games: pd.DataFrame) -> pd.DataFrame:
    pid = d["personId"]
    prev_t = d.groupby(pid, sort=False)["t"].shift(1)
    tv, pv = d["t"].values, prev_t.values
    team = d["playerteamId"].values
    missed = np.full(len(d), np.nan)
    tg = team_games.sort_values("t", kind="mergesort")
    for tm, grp in tg.groupby("playerteamId", sort=False):
        m = team == tm
        if not m.any():
            continue
        arr = grp["t"].values
        missed[m] = (np.searchsorted(arr, tv[m], "left") - np.searchsorted(arr, pv[m], "right"))
    missed[pd.isna(pv)] = np.nan                   # first career game: no previous game
    missed[~pd.isna(pv) & np.isnan(missed)] = 0     # team with no logged games before
    out = pd.DataFrame(index=d.index)
    out["missed_prev"] = np.clip(missed, 0, MISSED_CAP)
    pos = d.groupby(pid, sort=False).cumcount().values.astype("float64")
    last = pd.Series(np.where(missed >= ABSENCE_GAMES, pos, np.nan), index=d.index)
    last = last.groupby(pid, sort=False).ffill().values
    out["games_since_absence"] = np.clip(np.where(np.isnan(last), ABSENCE_CAP, pos - last), 0, ABSENCE_CAP)
    return out


def _window_mean(cs: np.ndarray, hi: np.ndarray, n: int) -> np.ndarray:
    lo = np.maximum(hi - n, 0)
    cnt = hi - lo
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(cnt > 0, (cs[hi] - cs[lo]) / np.maximum(cnt, 1), np.nan)


def _opp_block(d: pd.DataFrame, allowed: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(index=d.index)
    tv, opp = d["t"].values, d["opponentteamId"].values
    hi_lg = np.searchsorted(allowed["t"].values, tv, "left")
    by_team = {tm: grp for tm, grp in allowed.groupby("def_team", sort=False)}
    for c in STAT_COLS.values():
        cs = np.concatenate([[0], np.cumsum(allowed[c].values, dtype="int64")])
        lg = _window_mean(cs, hi_lg, LEAGUE_TEAM_GAMES)
        om = np.full(len(d), np.nan)
        for tm in np.unique(opp):
            m = opp == tm
            grp = by_team.get(tm)
            if grp is None:
                continue
            tcs = np.concatenate([[0], np.cumsum(grp[c].values, dtype="int64")])
            om[m] = _window_mean(tcs, np.searchsorted(grp["t"].values, tv[m], "left"), OPP_GAMES)
        with np.errstate(invalid="ignore", divide="ignore"):
            out[f"opp_rel_{c}"] = om / lg
    return out


def build_features(player_rows: pd.DataFrame, targets: pd.DataFrame | None,
                   team_games: pd.DataFrame, allowed: pd.DataFrame) -> pd.DataFrame:
    """Feature frame for the targets (see module docstring), with personId, gameId, t."""
    if targets is None:
        d = player_rows.copy()
        d["_target"] = True
    else:
        ph = targets.copy()
        for c in ["numMinutes", *RATE_COLS, "usagePercentage"]:
            ph[c] = np.nan
        h = player_rows.copy()
        h["_target"] = False
        ph["_target"] = True
        d = pd.concat([h, ph], ignore_index=True)
        d = d.sort_values(["personId", "t", "gameId"], kind="mergesort").reset_index(drop=True)
    f = pd.concat([d[["personId", "gameId", "t", "season"]], _player_block(d),
                   _absence_block(d, team_games), _opp_block(d, allowed)], axis=1)
    return f[d["_target"].values].reset_index(drop=True)


def batch_features(rows: pd.DataFrame) -> pd.DataFrame:
    """Training path: every loaded row is a target (rows must come from load_all/prep)."""
    return build_features(rows, None, team_games_of(rows), allowed_table(rows))
