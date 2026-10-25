"""
per_player_models.py  (exploratory; not part of the pre-registered tests)

Per-player vs pooled models for the stat VALUE (Points, Rebounds, Assists, 3PM),
predicted before each game from prior games only.

DATA RULE: no 2025-26 or 2026-27 game is loaded, fitted on or evaluated on (2025-26 is
reserved for the challenger's one-shot test). The SQL only reads games before
DATA_END; asserted again after loading and before evaluation.

  rows        regular-season games the player played (numMinutes > 0); features use the
              player's / opponent's earlier regular-season games only (shift by one game)
  train       seasons TRAIN_FIRST .. 2022-23
  evaluate    2023-24 and 2024-25, separately; rows with >= MIN_PRIOR earlier games
  methods     (a) last-10 average
              (b) pooled: one HistGradientBoosting (Poisson loss) model for all players
              (c) per player: a Poisson GLM per player with >= N_PER_PLAYER training
                  games (coverage reported; scored on covered rows, and overall with
                  the pooled model as fallback)
              (d) partial pooling: pooled prediction x an empirical-Bayes shrunk
                  per-player multiplier, (sum y + k) / (sum mu + k) over out-of-fold
                  training residuals; k tuned on 2022-23 using earlier seasons only
  metrics     MAE; mean log-likelihood under Poisson and negative binomial (per-stat
              dispersion fitted on training OOF residuals); P(stat > player's median of
              his last 20 prior games) as a line-free proxy: Brier, log loss, ECE and
              reliability bins. By experience: rookie, 1-3 prior seasons, 4+.

Reads predictions.db read-only. Writes experiments/results/per_player_models_<ts>.json
and .md. CPU only.

    python experiments/per_player_models.py
"""

import datetime as dt
import json
import os
import sqlite3
import sys
import time

import numpy as np
import pandas as pd
from scipy import stats as sps
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import PoissonRegressor

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "..", "predictions.db")
OUT_DIR = os.path.join(HERE, "results")

DATA_END = "2025-07-01"                         # nothing on/after this date is read
FORBIDDEN_SEASONS = {"2025-26", "2026-27"}
TRAIN_FIRST, TRAIN_LAST = "2012-13", "2022-23"
EVAL_SEASONS = ("2023-24", "2024-25")
TUNE_SEASON = "2022-23"                         # for k (d); fitted on seasons before it
MIN_PRIOR = 5
N_PER_PLAYER = 100
STATS = {"Points": "points", "Rebounds": "reboundsTotal", "Assists": "assists", "3PM": "threePointersMade"}
FEATS = ["avg5", "avg10", "season_avg", "career_avg", "min10", "n_prior", "home", "rest",
         "b2b", "opp_allowed10"]
GLM_FEATS = ["avg5", "avg10", "season_avg", "min10", "home", "rest", "b2b", "opp_allowed10"]
SEED = 7


def season_of(ts: pd.Series) -> pd.Series:
    start = np.where(ts.dt.month >= 7, ts.dt.year, ts.dt.year - 1)
    return pd.Series([f"{s}-{(s + 1) % 100:02d}" for s in start], index=ts.index)


def load() -> pd.DataFrame:
    conn = sqlite3.connect(f"file:{os.path.abspath(DB)}?mode=ro", uri=True)
    df = pd.read_sql_query(
        "SELECT personId, gameId, gameDateTimeEst, home, playerteamId, opponentteamId, numMinutes, "
        "points, reboundsTotal, assists, threePointersMade FROM player_stats "
        "WHERE gameType = 'Regular Season' AND numMinutes > 0 AND personId IS NOT NULL "
        "AND gameDateTimeEst < ?", conn, params=(DATA_END,))
    conn.close()
    df["t"] = pd.to_datetime(df["gameDateTimeEst"], errors="coerce")
    df = df.dropna(subset=["t"])
    df["season"] = season_of(df["t"])
    # DATA RULE, asserted on what was actually loaded
    assert df["t"].max() < pd.Timestamp(DATA_END), "a game on/after DATA_END was loaded"
    assert not set(df["season"]) & FORBIDDEN_SEASONS, "a 2025-26/2026-27 game was loaded"
    return df.sort_values(["personId", "t"]).reset_index(drop=True)


def experience(df: pd.DataFrame) -> pd.Series:
    """Prior NBA seasons (regular season, any year in the loaded data) before this game's season."""
    first = df.groupby("personId")["season"].transform("min").str[:4].astype(int)
    return df["season"].str[:4].astype(int) - first


def features(df: pd.DataFrame, col: str) -> pd.DataFrame:
    g = df.groupby("personId")
    out = pd.DataFrame(index=df.index)
    prev = g[col].shift(1)
    out["avg5"] = prev.groupby(df["personId"]).transform(lambda x: x.rolling(5, min_periods=1).mean())
    out["avg10"] = prev.groupby(df["personId"]).transform(lambda x: x.rolling(10, min_periods=1).mean())
    out["career_avg"] = prev.groupby(df["personId"]).transform(lambda x: x.expanding().mean())
    out["season_avg"] = (df.groupby(["personId", "season"])[col]
                         .transform(lambda x: x.shift(1).expanding().mean()))
    out["season_avg"] = out["season_avg"].fillna(out["avg10"])
    out["min10"] = (g["numMinutes"].shift(1).groupby(df["personId"])
                    .transform(lambda x: x.rolling(10, min_periods=1).mean()))
    out["n_prior"] = g.cumcount()
    out["home"] = df["home"].fillna(0).astype(int)
    rest = (df["t"] - g["t"].shift(1)).dt.days
    out["rest"] = rest.clip(upper=10).fillna(10)
    out["b2b"] = (rest == 1).astype(int)
    # opponent's allowed stat: team totals per game, lagged rolling mean of 10 by opponent
    team_game = (df.groupby(["gameId", "playerteamId", "opponentteamId"], as_index=False)
                 .agg(t=("t", "min"), total=(col, "sum")))
    allowed = team_game.rename(columns={"opponentteamId": "def_team"}).sort_values(["def_team", "t"])
    allowed["opp_allowed10"] = (allowed.groupby("def_team")["total"]
                                .transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean()))
    m = df[["gameId", "opponentteamId"]].merge(
        allowed[["gameId", "def_team", "opp_allowed10"]],
        left_on=["gameId", "opponentteamId"], right_on=["gameId", "def_team"], how="left")
    out["opp_allowed10"] = m["opp_allowed10"].values
    out["opp_allowed10"] = out["opp_allowed10"].fillna(out["opp_allowed10"].median())
    # threshold for the line-free proxy: median of the player's last 20 prior games
    out["median20"] = prev.groupby(df["personId"]).transform(lambda x: x.rolling(20, min_periods=5).median())
    return out


# --- distributions ------------------------------------------------------------------

def nb_alpha(y: np.ndarray, mu: np.ndarray) -> float:
    """NB2 dispersion: Var = mu + alpha mu^2, method of moments (floor 1e-6)."""
    return max(float(np.sum((y - mu) ** 2 - mu) / np.sum(mu ** 2)), 1e-6)


def nb_params(mu, alpha):
    n = 1.0 / alpha
    return n, n / (n + mu)


def loglik(y, mu, alpha):
    mu = np.clip(mu, 1e-6, None)
    n, p = nb_params(mu, alpha)
    return float(np.mean(sps.poisson.logpmf(y, mu))), float(np.mean(sps.nbinom.logpmf(y, n, p)))


def p_over(mu, alpha, thresh):
    """P(Y > thresh) under NB(mu, alpha); thresh may be x.5."""
    n, p = nb_params(np.clip(mu, 1e-6, None), alpha)
    return sps.nbinom.sf(np.floor(thresh), n, p)


def calib(pred, hit, bins=10):
    pred, hit = np.asarray(pred), np.asarray(hit, dtype=float)
    brier = float(np.mean((pred - hit) ** 2))
    pc = np.clip(pred, 1e-6, 1 - 1e-6)
    ll = float(-np.mean(hit * np.log(pc) + (1 - hit) * np.log(1 - pc)))
    edges = np.quantile(pred, np.linspace(0, 1, bins + 1))
    idx = np.clip(np.searchsorted(edges, pred, side="right") - 1, 0, bins - 1)
    rel, ece = [], 0.0
    for b in range(bins):
        m = idx == b
        if m.any():
            rel.append([round(float(pred[m].mean()), 3), round(float(hit[m].mean()), 3), int(m.sum())])
            ece += m.mean() * abs(pred[m].mean() - hit[m].mean())
    return {"brier": round(brier, 4), "logloss": round(ll, 4), "ece": round(float(ece), 4),
            "base_rate": round(float(hit.mean()), 4), "reliability": rel}


# --- models -----------------------------------------------------------------------

def fit_pooled(X, y):
    m = HistGradientBoostingRegressor(loss="poisson", max_iter=300, learning_rate=0.05,
                                      max_leaf_nodes=31, min_samples_leaf=200,
                                      l2_regularization=1.0, random_state=SEED)
    return m.fit(X, y)


def oof_pooled(d: pd.DataFrame, col: str) -> np.ndarray:
    """Out-of-fold pooled predictions, folds = seasons (no row predicts itself)."""
    mu = np.empty(len(d))
    for s in d["season"].unique():
        tr, te = d["season"] != s, d["season"] == s
        mu[te.values] = fit_pooled(d.loc[tr, FEATS], d.loc[tr, col]).predict(d.loc[te, FEATS])
    return mu


def eb_adjust(players, y, mu, k):
    s = pd.DataFrame({"p": players, "y": y, "mu": mu}).groupby("p")[["y", "mu"]].sum()
    return ((s["y"] + k) / (s["mu"] + k)).to_dict()


def tune_k(d: pd.DataFrame, col: str) -> tuple[float, dict]:
    """k for (d): fit/adjust on seasons < TUNE_SEASON, score Poisson log-lik on TUNE_SEASON."""
    early, tune = d[d["season"] < TUNE_SEASON], d[d["season"] == TUNE_SEASON]
    mu_early = oof_pooled(early, col)
    mu_tune = fit_pooled(early[FEATS], early[col]).predict(tune[FEATS])
    scores = {}
    for k in (1, 3, 10, 30, 100, 300, 1000):
        adj = eb_adjust(early["personId"].values, early[col].values, mu_early, k)
        m = mu_tune * tune["personId"].map(adj).fillna(1.0).values
        scores[k] = float(np.mean(sps.poisson.logpmf(tune[col].values, np.clip(m, 1e-6, None))))
    return max(scores, key=scores.get), scores


def fit_per_player(d: pd.DataFrame, col: str) -> dict:
    models = {}
    for pid, grp in d.groupby("personId"):
        if len(grp) < N_PER_PLAYER:
            continue
        X = grp[GLM_FEATS].values
        mean, sd = X.mean(0), X.std(0) + 1e-9
        glm = PoissonRegressor(alpha=1.0, max_iter=300).fit((X - mean) / sd, grp[col].values)
        models[pid] = (glm, mean, sd)
    return models


def predict_per_player(models, d):
    out = np.full(len(d), np.nan)
    for pid, idx in d.groupby("personId").indices.items():
        if pid in models:
            glm, mean, sd = models[pid]
            out[idx] = glm.predict((d.iloc[idx][GLM_FEATS].values - mean) / sd)
    return out


# --- run --------------------------------------------------------------------------

def evaluate(ev: pd.DataFrame, col: str, preds: dict, alphas: dict) -> dict:
    y = ev[col].values
    out = {}
    for name, mu in preds.items():
        ok = ~np.isnan(mu)
        thr = ev["median20"].values
        okp = ok & ~np.isnan(thr)
        if not okp.any():           # e.g. per-player models on rookies: no model, no rows
            out[name] = {"rows": int(ok.sum())}
            continue
        r = {"rows": int(ok.sum()),
             "mae": round(float(np.mean(np.abs(y[ok] - mu[ok]))), 4)}
        r["loglik_poisson"], r["loglik_nb"] = (round(v, 4) for v in loglik(y[ok], mu[ok], alphas[name]))
        r["p_over_median20"] = calib(p_over(mu[okp], alphas[name], thr[okp]), (y[okp] > thr[okp]))
        out[name] = r
    return out


def main() -> int:
    t_all = time.perf_counter()
    timings = {}
    t = time.perf_counter()
    df = load()
    df["exp"] = experience(df)
    df["exp_bucket"] = pd.cut(df["exp"], [-1, 0, 3, 100], labels=["rookie", "1-3", "4+"]).astype(str)
    timings["load_s"] = round(time.perf_counter() - t, 1)
    print(f"loaded {len(df):,} rows, seasons {df['season'].min()}..{df['season'].max()} "
          f"(max date {df['t'].max()}) in {timings['load_s']}s", flush=True)

    results = {"meta": {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "data_end_exclusive": DATA_END, "train": [TRAIN_FIRST, TRAIN_LAST], "eval": EVAL_SEASONS,
        "min_prior_games": MIN_PRIOR, "n_per_player": N_PER_PLAYER, "features_pooled": FEATS,
        "features_glm": GLM_FEATS, "rows_loaded": len(df), "max_date_loaded": str(df["t"].max()),
        "seasons_loaded": [df["season"].min(), df["season"].max()]}, "stats": {}}

    for stat, col in STATS.items():
        t = time.perf_counter()
        d = pd.concat([df, features(df, col)], axis=1)
        train = d[(d["season"] >= TRAIN_FIRST) & (d["season"] <= TRAIN_LAST) & (d["n_prior"] >= 1)]
        k, k_scores = tune_k(train, col)
        mu_oof = oof_pooled(train, col)
        pooled = fit_pooled(train[FEATS], train[col])
        adj = eb_adjust(train["personId"].values, train[col].values, mu_oof, k)
        per_player = fit_per_player(train, col)
        # NB dispersion: the baseline's from its own training residuals; (b), (c), (d) share
        # the pooled model's out-of-fold one (an in-sample per-player fit would understate
        # it and unfairly penalize (c)'s NB log-likelihood out of sample).
        alpha_pool = nb_alpha(train[col].values, mu_oof)
        alpha_base = nb_alpha(train[col].values, train["avg10"].values)
        alpha_pp = alpha_pool
        fit_s = round(time.perf_counter() - t, 1)

        res = {"k_partial_pooling": k, "k_tuning_loglik_2022_23": {str(a): round(b, 4) for a, b in k_scores.items()},
               "nb_alpha": {"baseline_last10": round(alpha_base, 4), "pooled_oof (b, c, d)": round(alpha_pool, 4)},
               "per_player_models": len(per_player), "fit_seconds": fit_s, "seasons": {}}
        for season in EVAL_SEASONS:
            ev = d[(d["season"] == season) & (d["n_prior"] >= MIN_PRIOR)].reset_index(drop=True)
            assert set(ev["season"]) <= set(EVAL_SEASONS) and not set(ev["season"]) & FORBIDDEN_SEASONS
            mu_b = pooled.predict(ev[FEATS])
            mu_c = predict_per_player(per_player, ev)
            preds = {"a_last10": ev["avg10"].values,
                     "b_pooled": mu_b,
                     "c_per_player_covered": mu_c,
                     "c_per_player_with_pooled_fallback": np.where(np.isnan(mu_c), mu_b, mu_c),
                     "d_partial_pooling": mu_b * ev["personId"].map(adj).fillna(1.0).values}
            alphas = {"a_last10": alpha_base, "b_pooled": alpha_pool, "c_per_player_covered": alpha_pp,
                      "c_per_player_with_pooled_fallback": alpha_pp, "d_partial_pooling": alpha_pool}
            covered = ~np.isnan(mu_c)
            sres = {"rows": len(ev), "all": evaluate(ev, col, preds, alphas),
                    "per_player_coverage": round(float(covered.mean()), 4),
                    # same rows for every method: those (c) covers
                    "on_c_covered_rows": evaluate(ev[covered].reset_index(drop=True), col,
                                                  {n: v[covered] for n, v in preds.items()}, alphas),
                    "by_experience": {}}
            for b in ("rookie", "1-3", "4+"):
                m = (ev["exp_bucket"] == b).values
                if m.sum() < 50:
                    continue
                sres["by_experience"][b] = {
                    "rows": int(m.sum()), "per_player_coverage": round(float(covered[m].mean()), 4),
                    "metrics": evaluate(ev[m].reset_index(drop=True), col,
                                        {n: v[m] for n, v in preds.items()}, alphas)}
            res["seasons"][season] = sres
        results["stats"][stat] = res
        print(f"{stat}: k={k}, per-player models={len(per_player)}, {time.perf_counter() - t:.1f}s", flush=True)

    timings["total_s"] = round(time.perf_counter() - t_all, 1)
    results["meta"]["timings"] = timings
    results["meta"]["cpu"] = os.cpu_count()
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    path = os.path.join(OUT_DIR, f"per_player_models_{stamp}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=1, default=str)
    write_md(results, path.replace(".json", ".md"))
    print(f"wrote {path} (+ .md); total {timings['total_s']}s on CPU ({os.cpu_count()} logical cores)")
    return 0


def write_md(r: dict, path: str) -> None:
    names = ["a_last10", "b_pooled", "c_per_player_with_pooled_fallback", "d_partial_pooling"]
    lines = ["# Per-player vs pooled models (exploratory)", "",
             f"Train {r['meta']['train'][0]}..{r['meta']['train'][1]}, regular season; evaluate "
             f"{', '.join(r['meta']['eval'])}; no game on/after {r['meta']['data_end_exclusive']} loaded "
             f"(max date loaded {r['meta']['max_date_loaded']}). Rows: >= {r['meta']['min_prior_games']} "
             f"prior games. Per-player models need >= {r['meta']['n_per_player']} training games.", ""]
    for stat, s in r["stats"].items():
        lines += [f"## {stat} (k = {s['k_partial_pooling']}, {s['per_player_models']} per-player models)", ""]
        for season, ss in s["seasons"].items():
            lines += [f"### {season}: {ss['rows']:,} rows, per-player coverage {ss['per_player_coverage']:.1%}", "",
                      "| method | MAE | loglik Poisson | loglik NB | P(>median20) Brier | log loss | ECE |",
                      "|---|---|---|---|---|---|---|"]
            for n in names:
                m = ss["all"][n]
                pm = m["p_over_median20"]
                lines.append(f"| {n} | {m['mae']} | {m['loglik_poisson']} | {m['loglik_nb']} | "
                             f"{pm['brier']} | {pm['logloss']} | {pm['ece']} |")
            lines += ["", "On rows covered by per-player models (same rows for every method):", "",
                      "| method | MAE | loglik NB | Brier |", "|---|---|---|---|"]
            for n in ["a_last10", "b_pooled", "c_per_player_covered", "d_partial_pooling"]:
                m = ss["on_c_covered_rows"][n]
                lines.append(f"| {n} | {m['mae']} | {m['loglik_nb']} | {m['p_over_median20']['brier']} |")
            lines += ["", "By experience (MAE / loglik NB):", "",
                      "| bucket | rows | coverage | " + " | ".join(names) + " |",
                      "|---|---|---|" + "---|" * len(names)]
            for b, bb in ss["by_experience"].items():
                cells = [f"{bb['metrics'][n]['mae']} / {bb['metrics'][n]['loglik_nb']}" for n in names]
                lines.append(f"| {b} | {bb['rows']:,} | {bb['per_player_coverage']:.1%} | " + " | ".join(cells) + " |")
            lines.append("")
    lines.append(f"Run time: {r['meta']['timings']} on CPU ({r['meta']['cpu']} logical cores).")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
