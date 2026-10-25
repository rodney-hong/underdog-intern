"""
stage1.py  (challenger, Stage 1: development only)

DATA RULE (guard.py): no 2025-26 or 2026-27 game is loaded, fitted on or evaluated on.
No backfilled lines (historical.db) are read. The frozen models, ml_trainer.py and
feature_defs.py are not touched.

  rows       played (numMinutes > 0) regular-season games through 2024-25; features from
             earlier games only (features.py). Training rows need >= 1 earlier game,
             validation/evaluation rows >= MIN_PRIOR.
  tuning     fit on TRAIN_FIRST..2022-23, validate on 2023-24: HGB grid, then one pass of
             feature-group changes (drop/add), each accepted only if it beats the current
             choice by more than its tolerance. Every candidate's score is recorded.
  dev model  chosen design refitted on TRAIN_FIRST..2023-24; NB alpha by MLE on out-of-fold
             predictions over contiguous season blocks; evaluated on 2024-25.
  baselines  (a) last-10 average; (b) experiments/per_player_models.py's pooled HGB
             (same features and hyperparameters), refitted on the same seasons. Both
             get their NB alpha the same way as the challenger (MLE; (b) out of fold).
  frozen     the same design refitted on TRAIN_FIRST..2024-25 (challenger and baseline b),
             saved with SHA-256 for the Stage 2 pre-registration. Not evaluated.

Writes backend/experiments/results/challenger_stage1_<ts>.md and .json; model files go to
backend/challenger/models/ (not committed).

    python -m challenger.stage1          (from backend/)
"""

import datetime as dt
import json
import os
import sqlite3
import sys
import time

import numpy as np
import pandas as pd

from . import model as cm
from .features import STAT_COLS, batch_features, load_all
from .guard import DATA_END, FORBIDDEN_SEASONS, assert_dev_only
from .inference import DB, MODEL_DIR, DEV_MODEL, _connect, features_before_tip

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "experiments"))
import per_player_models as ppm                      # noqa: E402  baseline (b)

OUT_DIR = os.path.join(HERE, "..", "experiments", "results")
FROZEN_MODEL = os.path.join(MODEL_DIR, "challenger_stage2_frozen.pkl")
FROZEN_BASELINE = os.path.join(MODEL_DIR, "baseline_b_stage2_frozen.pkl")

TRAIN_FIRST = "2012-13"
TUNE_LAST, VAL_SEASON = "2022-23", "2023-24"
DEV_LAST, EVAL_SEASON = "2023-24", "2024-25"
MIN_PRIOR = 5
OOF_BLOCKS = 4                     # contiguous season blocks for out-of-fold dispersion
N_PARITY = 200
N_BOOT, BOOT_SEED = 2000, 20261024
MIN_TOL = 0.001                    # minutes: relative validation-MSE gain to accept a change
LL_TOL = 0.0005                    # rate: NB log-lik gain per row to accept a change

GRID = [{"learning_rate": 0.05, "max_iter": it, "max_leaf_nodes": lv, "min_samples_leaf": ms}
        for it in (200, 500) for lv in (15, 31, 63) for ms in (100, 500)]

MIN_BASE = ["min_h3", "min_h10", "min_h30", "rest_days", "b2b", "home", "games_since_absence",
            "missed_prev"]
MIN_DROP = {"rest/b2b": ["rest_days", "b2b"], "home": ["home"],
            "absence": ["games_since_absence", "missed_prev"], "half-life 3": ["min_h3"]}
MIN_ADD = {"n_prior": ["n_prior"], "experience": ["experience"]}


def rate_base(stat: str) -> list:
    c = STAT_COLS[stat]
    f = [f"rate_{c}_h{h}" for h in (3, 10, 30)] + ["usage_h3", "usage_h10", "usage_h30",
                                                    f"opp_rel_{c}", "home"]
    if stat == "3PM":
        f += [f"rate_threePointersAttempted_h{h}" for h in (3, 10, 30)]
    return f


def rate_drop(stat: str) -> dict:
    c = STAT_COLS[stat]
    d = {"usage": ["usage_h3", "usage_h10", "usage_h30"], "opp defense": [f"opp_rel_{c}"],
         "home": ["home"], "half-life 3": [f"rate_{c}_h3", "usage_h3"]}
    if stat == "3PM":
        d["3PA per minute"] = [f"rate_threePointersAttempted_h{h}" for h in (3, 10, 30)]
    return d


RATE_ADD = {"minutes EWMA (h10)": ["min_h10"], "n_prior": ["n_prior"]}


# --- helpers ----------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = {}

    def run(self, name, fn, *a, **k):
        w, c = time.perf_counter(), time.process_time()
        out = fn(*a, **k)
        self.t[name] = {"wall_s": round(time.perf_counter() - w, 1), "cpu_s": round(time.process_time() - c, 1)}
        print(f"  [{name}] wall {self.t[name]['wall_s']}s, cpu {self.t[name]['cpu_s']}s", flush=True)
        return out


def seasons_between(s, first, last):
    return (s >= first) & (s <= last)


def season_blocks(seasons, k):
    u = sorted(set(seasons))
    return [list(b) for b in np.array_split(u, k)]


def mean_ll(y, mu, alpha):
    return float(np.mean(cm.nb_logpmf(y, mu, alpha)))


def rungs(m):
    """Nearest x4.5/x9.5 rungs below (<=) and above (>) m."""
    k = np.floor((m - 4.5) / 5.0)
    below = np.where(m >= 4.5, 4.5 + 5 * k, np.nan)
    above = 4.5 + 5 * np.where(m >= 4.5, k + 1, 0)
    return below, above


def calib(p, hit, bins=10):
    p, hit = np.asarray(p, float), np.asarray(hit, float)
    if len(p) == 0:
        return {"n": 0}
    pc = np.clip(p, 1e-6, 1 - 1e-6)
    idx = np.minimum((p * bins).astype(int), bins - 1)
    rel, ece = [], 0.0
    for b in range(bins):
        m = idx == b
        if m.any():
            rel.append({"bin": f"{b / bins:.1f}-{(b + 1) / bins:.1f}", "mean_p": round(float(p[m].mean()), 4),
                        "observed": round(float(hit[m].mean()), 4), "n": int(m.sum())})
            ece += m.mean() * abs(p[m].mean() - hit[m].mean())
    return {"n": len(p), "base_rate": round(float(hit.mean()), 4), "mean_p": round(float(p.mean()), 4),
            "brier": round(float(np.mean((p - hit) ** 2)), 4),
            "logloss": round(float(-np.mean(hit * np.log(pc) + (1 - hit) * np.log(1 - pc))), 4),
            "ece": round(float(ece), 4), "reliability": rel}


def boot_diff(game_ids, diff, n=N_BOOT, seed=BOOT_SEED):
    """Game-clustered bootstrap: mean per-row difference, percentile 95% CI."""
    g = pd.DataFrame({"g": game_ids, "d": diff}).groupby("g")["d"].agg(["sum", "count"])
    s, c = g["sum"].values, g["count"].values
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(g), size=(n, len(g)))
    means = s[idx].sum(1) / c[idx].sum(1)
    return {"mean": round(float(s.sum() / c.sum()), 5), "ci95": [round(float(np.percentile(means, 2.5)), 5),
                                                                 round(float(np.percentile(means, 97.5)), 5)],
            "games": int(len(g)), "rows": int(c.sum())}


# --- tuning -----------------------------------------------------------------------------

def tune_minutes(tr, va, log):
    scores = {}
    for i, p in enumerate(GRID):
        m = cm.fit_minutes(tr[MIN_BASE], tr["numMinutes"].values, p)
        scores[i] = float(np.mean((np.maximum(m.predict(va[MIN_BASE]), cm.MIN_FLOOR) - va["numMinutes"]) ** 2))
    best = min(scores, key=scores.get)
    log["minutes_grid"] = [{"params": GRID[i], "val_mse": round(s, 4)} for i, s in scores.items()]
    params, feats, base = GRID[best], list(MIN_BASE), scores[best]
    changes = []
    cands = [("drop", k, v) for k, v in MIN_DROP.items()] + [("add", k, v) for k, v in MIN_ADD.items()]
    for kind, name, cols in cands:
        f = [c for c in MIN_BASE if c not in cols] if kind == "drop" else MIN_BASE + cols
        m = cm.fit_minutes(tr[f], tr["numMinutes"].values, params)
        s = float(np.mean((np.maximum(m.predict(va[f]), cm.MIN_FLOOR) - va["numMinutes"]) ** 2))
        ok = s < base * (1 - MIN_TOL)
        changes.append({"change": f"{kind} {name}", "val_mse": round(s, 4), "vs_base": round(base, 4),
                        "accepted": bool(ok)})
        if ok:
            feats = [c for c in feats if c not in cols] if kind == "drop" else feats + cols
    log["minutes_feature_changes"] = changes
    log["minutes_choice"] = {"params": params, "features": feats}
    return params, feats


def tune_rate(stat, tr, va, emin_va, log):
    col = STAT_COLS[stat]
    y_tr, y_va = tr[col].values, va[col].values

    def score(f, p):
        r = cm.fit_rate(tr[f], y_tr, tr["numMinutes"].values, p)
        mu = np.clip(emin_va * r.predict(va[f]), cm.MU_FLOOR, None)
        a = cm.nb_alpha_mle(y_va, mu)            # profile alpha on validation (tuning only)
        return mean_ll(y_va, mu, a), a

    base_f = rate_base(stat)
    grid = {}
    for i, p in enumerate(GRID):
        grid[i] = score(base_f, p)
    best = max(grid, key=lambda i: grid[i][0])
    params, feats, base = GRID[best], list(base_f), grid[best][0]
    changes = []
    cands = [("drop", k, v) for k, v in rate_drop(stat).items()] + [("add", k, v) for k, v in RATE_ADD.items()]
    for kind, name, cols in cands:
        f = [c for c in base_f if c not in cols] if kind == "drop" else base_f + cols
        s, _ = score(f, params)
        ok = s > base + LL_TOL
        changes.append({"change": f"{kind} {name}", "val_ll": round(s, 5), "vs_base": round(base, 5),
                        "accepted": bool(ok)})
        if ok:
            feats = [c for c in feats if c not in cols] if kind == "drop" else feats + cols
    final, _ = score(feats, params) if feats != base_f else (base, None)
    log[stat] = {"grid": [{"params": GRID[i], "val_ll": round(v[0], 5), "alpha": round(v[1], 4)}
                          for i, v in grid.items()],
                 "feature_changes": changes, "choice": {"params": params, "features": feats},
                 "val_ll_final": round(final, 5)}
    return params, feats


# --- fitting with out-of-fold dispersion -------------------------------------------------

def fit_challenger(tr, design, meta):
    """Fit on tr; alpha per stat by MLE on out-of-fold predictions over season blocks."""
    ch = cm.Challenger(design["minutes_feats"], design["minutes_params"], design["rate_feats"],
                       design["rate_params"], meta)
    oof_mu = {s: np.empty(len(tr)) for s in STAT_COLS}
    for block in season_blocks(tr["season"], OOF_BLOCKS):
        te = tr["season"].isin(block).values
        a, b = tr[~te], tr[te]
        mm = cm.fit_minutes(a[ch.minutes_feats], a["numMinutes"].values, ch.minutes_params)
        emin = np.maximum(mm.predict(b[ch.minutes_feats]), cm.MIN_FLOOR)
        for s, col in STAT_COLS.items():
            r = cm.fit_rate(a[ch.rate_feats[s]], a[col].values, a["numMinutes"].values, ch.rate_params[s])
            oof_mu[s][te] = emin * r.predict(b[ch.rate_feats[s]])
    ch.minutes_model = cm.fit_minutes(tr[ch.minutes_feats], tr["numMinutes"].values, ch.minutes_params)
    for s, col in STAT_COLS.items():
        ch.rate_models[s] = cm.fit_rate(tr[ch.rate_feats[s]], tr[col].values, tr["numMinutes"].values,
                                        ch.rate_params[s])
        ch.alpha[s] = cm.nb_alpha_mle(tr[col].values, oof_mu[s])
    return ch


def fit_baseline_b(tr, bfeat):
    """per_player_models' pooled HGB per stat; alpha by MLE on out-of-fold predictions."""
    out = {"features": list(ppm.FEATS), "models": {}, "alpha": {}}
    for s, col in STAT_COLS.items():
        X = bfeat[s].loc[tr.index, ppm.FEATS]
        oof = np.empty(len(tr))
        for block in season_blocks(tr["season"], OOF_BLOCKS):
            te = tr["season"].isin(block).values
            oof[te] = ppm.fit_pooled(X[~te], tr.loc[~te, col]).predict(X[te])
        out["models"][s] = ppm.fit_pooled(X, tr[col])
        out["alpha"][s] = cm.nb_alpha_mle(tr[col].values, oof)
    return out


# --- evaluation -------------------------------------------------------------------------

LINE_TYPES = ["median", "median-2", "median+2", "rung below", "rung above"]


def evaluate(ev, F_ev, preds, alphas, stat):
    col = STAT_COLS[stat]
    y = ev[col].values
    med = F_ev[f"median20_{col}"].values
    rb, ra = rungs(med)
    lines = {"median": med, "median-2": med - 2, "median+2": med + 2, "rung below": rb, "rung above": ra}
    res = {}
    for name, mu in preds.items():
        r = {"rows": len(y), "ll": round(mean_ll(y, mu, alphas[name]), 5),
             "mae": round(float(np.mean(np.abs(y - mu))), 4), "alpha": round(alphas[name], 4), "lines": {}}
        for lt, L in lines.items():
            ok = ~np.isnan(L) & (L >= 0)
            r["lines"][lt] = calib(cm.p_over(mu[ok], alphas[name], L[ok]), y[ok] > L[ok])
        res[name] = r
    return res


def parity_check(ev_rows, F_all, cols, conn, n=N_PARITY, seed=11):
    """Inference path (features_before_tip, SQL before tip) vs the training frame."""
    rng = np.random.default_rng(seed)
    pool = ev_rows.index.values
    special = ev_rows.index[(F_all.loc[ev_rows.index, "games_since_absence"] == 0)
                            | (F_all.loc[ev_rows.index, "experience"] == 0)].values
    pick = np.unique(np.concatenate([rng.choice(pool, n - n // 4, replace=False),
                                     rng.choice(special, n // 4, replace=False)]))
    bad = []
    for i in pick:
        row = F_all.loc[i]
        Fi, _ = features_before_tip(conn, int(row["personId"]), int(row["gameId"]))
        for c in cols:
            a, b = row[c], Fi.iloc[0][c]
            if not ((pd.isna(a) and pd.isna(b)) or a == b):
                bad.append({"personId": int(row["personId"]), "gameId": int(row["gameId"]), "feature": c,
                            "train": float(a), "inference": float(b)})
    return {"rows_checked": int(len(pick)), "features_per_row": len(cols), "mismatches": len(bad),
            "examples": bad[:10]}


def main() -> int:
    clock = Clock()
    os.makedirs(MODEL_DIR, exist_ok=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    conn = _connect(DB)
    df = clock.run("load", load_all, conn)
    assert_dev_only(df["t"], df["season"])
    print(f"loaded {len(df):,} rows, {df['season'].min()}..{df['season'].max()}, max {df['t'].max()}", flush=True)
    F = clock.run("features (challenger)", batch_features, df)
    assert (F["gameId"].values == df["gameId"].values).all() and (F["personId"].values == df["personId"].values).all()
    bfeat = clock.run("features (baseline b)", lambda: {s: ppm.features(df, c) for s, c in STAT_COLS.items()})
    Fx = F.drop(columns=["personId", "gameId", "t", "season"])
    D = pd.concat([df.drop(columns=[c for c in Fx.columns if c in df.columns]), Fx], axis=1)

    has1, has5 = D["n_prior"] >= 1, D["n_prior"] >= MIN_PRIOR
    tune_tr = D[seasons_between(D["season"], TRAIN_FIRST, TUNE_LAST) & has1]
    val = D[(D["season"] == VAL_SEASON) & has5]
    dev_tr = D[seasons_between(D["season"], TRAIN_FIRST, DEV_LAST) & has1]
    ev = D[(D["season"] == EVAL_SEASON) & has5]
    frz_tr = D[seasons_between(D["season"], TRAIN_FIRST, EVAL_SEASON) & has1]
    for part in (tune_tr, val, dev_tr, ev, frz_tr):
        assert_dev_only(part["t"], part["season"])
        assert not set(part["season"]) & FORBIDDEN_SEASONS

    parity = clock.run("parity check", parity_check, D[D["season"].isin([VAL_SEASON, EVAL_SEASON]) & has5], D,
                       [c for c in F.columns if c not in ("personId", "gameId", "t", "season")], conn)
    print(f"parity: {parity['mismatches']} mismatches over {parity['rows_checked']} rows", flush=True)

    # tuning on 2023-24
    log = {}
    mp, mf = clock.run("tune minutes", tune_minutes, tune_tr, val, log)
    mmod = cm.fit_minutes(tune_tr[mf], tune_tr["numMinutes"].values, mp)
    emin_val = np.maximum(mmod.predict(val[mf]), cm.MIN_FLOOR)
    rate_params, rate_feats = {}, {}
    for s in STAT_COLS:
        rate_params[s], rate_feats[s] = clock.run(f"tune rate {s}", tune_rate, s, tune_tr, val, emin_val, log)
    design = {"minutes_feats": mf, "minutes_params": mp, "rate_feats": rate_feats, "rate_params": rate_params}

    # dev model and baselines, evaluated on 2024-25
    meta_dev = {"stage": 1, "train_seasons": [TRAIN_FIRST, DEV_LAST], "data_end_exclusive": DATA_END}
    ch = clock.run("fit challenger dev (incl. OOF)", fit_challenger, dev_tr, design, meta_dev)
    dev_sha = cm.save(ch, DEV_MODEL)
    bb = clock.run("fit baseline b dev (incl. OOF)", fit_baseline_b, dev_tr, bfeat)
    alpha_a = {s: cm.nb_alpha_mle(dev_tr[c].values, dev_tr[f"avg10_{c}"].values) for s, c in STAT_COLS.items()}

    w, c0 = time.perf_counter(), time.process_time()
    emin_ev = ch.predict_minutes(ev)
    results, pooled_rows = {}, []
    for s, col in STAT_COLS.items():
        assert not set(ev["season"]) & FORBIDDEN_SEASONS
        mu_c = ch.predict(ev, s, emin_ev)["mu"]
        mu_b = np.clip(bb["models"][s].predict(bfeat[s].loc[ev.index, ppm.FEATS]), cm.MU_FLOOR, None)
        mu_a = np.clip(ev[f"avg10_{col}"].values, cm.MU_FLOOR, None)
        preds = {"challenger": mu_c, "b_pooled_hgb": mu_b, "a_last10": mu_a}
        alphas = {"challenger": ch.alpha[s], "b_pooled_hgb": bb["alpha"][s], "a_last10": alpha_a[s]}
        r = {"all": evaluate(ev, ev, preds, alphas, s), "by_experience": {}}
        y = ev[col].values
        llc, llb, lla = (cm.nb_logpmf(y, preds[k], alphas[k]) for k in ("challenger", "b_pooled_hgb", "a_last10"))
        r["boot_ll_challenger_minus_b"] = boot_diff(ev["gameId"].values, llc - llb)
        r["boot_ll_challenger_minus_a"] = boot_diff(ev["gameId"].values, llc - lla)
        exp = ev["experience"].values
        for name, m in (("rookie", exp == 0), ("1-3", (exp >= 1) & (exp <= 3)), ("4+", exp >= 4)):
            r["by_experience"][name] = evaluate(ev[m], ev[m], {k: v[m] for k, v in preds.items()}, alphas, s)
            r["by_experience"][name]["boot_ll_challenger_minus_b"] = boot_diff(ev["gameId"].values[m], (llc - llb)[m])
        results[s] = r
        pooled_rows.append(pd.DataFrame({"g": ev["gameId"].values, "d": llc - llb, "da": llc - lla}))
    pr = pd.concat(pooled_rows)
    pooled = {"challenger_minus_b": boot_diff(pr["g"].values, pr["d"].values),
              "challenger_minus_a": boot_diff(pr["g"].values, pr["da"].values)}
    clock.t["evaluate 2024-25"] = {"wall_s": round(time.perf_counter() - w, 1), "cpu_s": round(time.process_time() - c0, 1)}

    # frozen Stage 2 candidates (trained through 2024-25; not evaluated here)
    meta_frz = {"stage": 2, "train_seasons": [TRAIN_FIRST, EVAL_SEASON], "data_end_exclusive": DATA_END}
    chf = clock.run("fit challenger frozen (incl. OOF)", fit_challenger, frz_tr, design, meta_frz)
    frz_sha = cm.save(chf, FROZEN_MODEL)
    bbf = clock.run("fit baseline b frozen (incl. OOF)", fit_baseline_b, frz_tr, bfeat)
    bbf["meta"] = meta_frz
    frzb_sha = cm.save(bbf, FROZEN_BASELINE)

    # smoke test of challenger_predict on one 2024-25 row against the batch prediction
    from .inference import challenger_predict
    i0 = ev.index[len(ev) // 2]
    got = challenger_predict(int(ev.at[i0, "personId"]), "Points", int(ev.at[i0, "gameId"]), 19.5,
                             model_path=DEV_MODEL)
    want = float(cm.p_over(ch.predict(ev.loc[[i0]], "Points")["mu"], ch.alpha["Points"], 19.5)[0])
    smoke = {"personId": int(ev.at[i0, "personId"]), "gameId": int(ev.at[i0, "gameId"]), "line": 19.5,
             "p_over_inference": got["p_over"], "p_over_batch": want, "equal": got["p_over"] == want}
    conn.close()

    out = {"meta": {"generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    "data_end_exclusive": DATA_END, "rows_loaded": len(df), "max_date_loaded": str(df["t"].max()),
                    "seasons_loaded": [df["season"].min(), df["season"].max()],
                    "train_first": TRAIN_FIRST, "tune": [TRAIN_FIRST, TUNE_LAST, VAL_SEASON],
                    "dev": [TRAIN_FIRST, DEV_LAST, EVAL_SEASON], "min_prior": MIN_PRIOR,
                    "oof_blocks": [[str(x) for x in b] for b in season_blocks(dev_tr["season"], OOF_BLOCKS)],
                    "rows": {"tune_train": len(tune_tr), "val_2023_24": len(val), "dev_train": len(dev_tr),
                             "eval_2024_25": len(ev), "frozen_train": len(frz_tr)},
                    "eval_games": int(ev["gameId"].nunique()), "cpu_logical_cores": os.cpu_count(),
                    "timings": clock.t},
           "parity": parity, "smoke": smoke, "tuning": log, "design": design,
           "alphas": {"challenger_dev": ch.alpha, "baseline_b_dev": bb["alpha"], "baseline_a": alpha_a,
                      "challenger_frozen": chf.alpha, "baseline_b_frozen": bbf["alpha"]},
           "baseline_b": {"features": ppm.FEATS, "hgb": "per_player_models.fit_pooled (poisson, 300 iter, "
                                                        "lr 0.05, 31 leaves, min leaf 200, l2 1.0)"},
           "models": {"dev": {"path": os.path.relpath(DEV_MODEL, os.path.join(HERE, "..")), "sha256": dev_sha,
                              "fingerprint": cm.fingerprint(ch)},
                      "frozen_challenger": {"path": os.path.relpath(FROZEN_MODEL, os.path.join(HERE, "..")),
                                            "sha256": frz_sha, "fingerprint": cm.fingerprint(chf)},
                      "frozen_baseline_b": {"path": os.path.relpath(FROZEN_BASELINE, os.path.join(HERE, "..")),
                                            "sha256": frzb_sha, "fingerprint": cm.fingerprint(bbf)}},
           "results_2024_25": results, "pooled_2024_25": pooled}
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    path = os.path.join(OUT_DIR, f"challenger_stage1_{stamp}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, default=str)
    from .report import write_md
    write_md(out, path.replace(".json", ".md"))
    print(f"wrote {path} (+ .md)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
