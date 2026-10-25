"""
challenger_ablation.py  (exploratory; development data only)

Feature-group ablation of the challenger on the Stage 1 setup: fit on 2012-13..2023-24,
evaluate on 2024-25 (rows with >= 5 earlier games), NB alpha by MLE on out-of-fold
predictions over 4 contiguous season blocks, exactly as challenger/stage1.py.

DATA RULE: no 2025-26 or 2026-27 game is loaded, fitted on or evaluated on. guard.py now
allows 2025-26 (Stage 2), so this script loads with its own bound DEV_END = 2025-07-01 and
asserts on what was loaded and on every split. The frozen challenger files and everything
live are untouched: the Stage 1 dev model file is only read for its design (feature lists,
hyperparameters), with its SHA-256 checked.

Variants (each changes one thing; the other model stays the full design):
  minutes model   drop EWMA minutes half-life 3 / 10 / 30; rest + back-to-back; home;
                  absence (missed_prev, games_since_absence); prior games + seasons;
                  or replace it with the plain last-10 minutes average
  rate model      drop EWMA per-minute stats (all half-lives); usage; 3PA per minute (3PM);
                  opponent defense; home; prior games (Points only, its rate model uses it);
                  or replace it with the plain last-10 per-minute average
                  (last-10 stat total / last-10 minutes)
Per stat: NB log-likelihood per row and MAE, variant minus full, with game-clustered
bootstrap CIs (2,000 resamples), ranked by the log-likelihood drop.

Writes experiments/results/challenger_ablation_<ts>.md and .json.

    python experiments/challenger_ablation.py          (from backend/)
"""

import datetime as dt
import json
import os
import sys
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from challenger import model as cm                                        # noqa: E402
from challenger.features import STAT_COLS, batch_features, load_all      # noqa: E402
from challenger.guard import assert_dev_only                              # noqa: E402
from challenger.inference import DB, DEV_MODEL, _connect                  # noqa: E402
from challenger.stage1 import (MIN_PRIOR, OOF_BLOCKS, TRAIN_FIRST, boot_diff,  # noqa: E402
                               season_blocks, seasons_between)

DEV_END = "2025-07-01"
FORBIDDEN = {"2025-26", "2026-27"}
TRAIN_LAST, EVAL_SEASON = "2023-24", "2024-25"
DEV_SHA = "a6ce0b652537371c69ef87747c2066e429f28484e72a923b1be469cdb47c8ab2"
OUT_DIR = os.path.join(HERE, "results")
LAST10 = "last-10 average"


def check(df: pd.DataFrame) -> None:
    assert_dev_only(df["t"], df["season"])
    assert df["t"].max() < pd.Timestamp(DEV_END), "a game on/after 2025-07-01 was loaded"
    assert not set(df["season"]) & FORBIDDEN, "a 2025-26/2026-27 game was loaded"


def minutes_variants(base: list) -> dict:
    groups = {"EWMA minutes h3": ["min_h3"], "EWMA minutes h10": ["min_h10"], "EWMA minutes h30": ["min_h30"],
              "rest / back-to-back": ["rest_days", "b2b"], "home (minutes)": ["home"],
              "absence / games since return": ["missed_prev", "games_since_absence"],
              "prior games / seasons": ["n_prior", "experience"]}
    out = {"full": base}
    for name, cols in groups.items():
        assert set(cols) <= set(base), (name, cols)
        out[f"minutes: drop {name}"] = [c for c in base if c not in cols]
    out[f"minutes: {LAST10}"] = LAST10
    return out


def rate_variants(stat: str, base: list) -> dict:
    c = STAT_COLS[stat]
    groups = {"EWMA per-minute stats": [f"rate_{c}_h{h}" for h in (3, 10, 30)],
              "usage": ["usage_h3", "usage_h10", "usage_h30"],
              "opponent defense": [f"opp_rel_{c}"], "home (rate)": ["home"]}
    if stat == "3PM":
        groups["3PA per minute"] = [f"rate_threePointersAttempted_h{h}" for h in (3, 10, 30)]
    if "n_prior" in base:
        groups["prior games (rate)"] = ["n_prior"]
    out = {"full": base}
    for name, cols in groups.items():
        assert set(cols) <= set(base), (stat, name, cols)
        out[f"rate: drop {name}"] = [x for x in base if x not in cols]
    out[f"rate: {LAST10}"] = LAST10
    return out


def oof_and_eval(tr, ev, fit, predict):
    """(out-of-fold predictions on tr over season blocks, predictions on ev from a fit on all tr)."""
    oof = np.empty(len(tr))
    for block in season_blocks(tr["season"], OOF_BLOCKS):
        te = tr["season"].isin(block).values
        oof[te] = predict(fit(tr[~te]), tr[te])
    return oof, predict(fit(tr), ev)


def minutes_preds(tr, ev, feats, params):
    if feats == LAST10:
        return np.maximum(tr["min10"].values, cm.MIN_FLOOR), np.maximum(ev["min10"].values, cm.MIN_FLOOR)
    return oof_and_eval(tr, ev, lambda d: cm.fit_minutes(d[feats], d["numMinutes"].values, params),
                        lambda m, d: np.maximum(m.predict(d[feats]), cm.MIN_FLOOR))


def rate_preds(tr, ev, stat, feats, params):
    col = STAT_COLS[stat]
    if feats == LAST10:
        r = lambda d: (d[f"avg10_{col}"] / d["min10"]).fillna(0).values
        return r(tr), r(ev)
    return oof_and_eval(tr, ev, lambda d: cm.fit_rate(d[feats], d[col].values, d["numMinutes"].values, params),
                        lambda m, d: m.predict(d[feats]))


def main() -> int:
    t0 = time.perf_counter()
    design = cm.load(DEV_MODEL, DEV_SHA)                  # read for its design only
    conn = _connect(DB)
    df = load_all(conn, end=DEV_END)
    conn.close()
    check(df)
    F = batch_features(df)
    assert (F["gameId"].values == df["gameId"].values).all()
    Fx = F.drop(columns=["personId", "gameId", "t", "season"])
    D = pd.concat([df.drop(columns=[c for c in Fx.columns if c in df.columns]), Fx], axis=1)
    # last-10 minutes average, closed-left, as avg10_<stat> (features.py)
    D["min10"] = (D.groupby("personId", sort=False)["numMinutes"].shift(1)
                  .groupby(D["personId"], sort=False).rolling(10, min_periods=1).mean()
                  .droplevel(0).reindex(D.index))
    tr = D[seasons_between(D["season"], TRAIN_FIRST, TRAIN_LAST) & (D["n_prior"] >= 1)].reset_index(drop=True)
    ev = D[(D["season"] == EVAL_SEASON) & (D["n_prior"] >= MIN_PRIOR)].reset_index(drop=True)
    for part in (tr, ev):
        check(part)
    print(f"train {len(tr):,} rows ({tr['season'].min()}..{tr['season'].max()}), eval {len(ev):,} rows "
          f"({ev['gameId'].nunique():,} games of {EVAL_SEASON})", flush=True)

    mv = minutes_variants(design.minutes_feats)
    mins = {}
    for name, feats in mv.items():
        mins[name] = minutes_preds(tr, ev, feats, design.minutes_params)
        print(f"  {name}: {time.perf_counter() - t0:.0f}s", flush=True)

    results = {}
    for stat, col in STAT_COLS.items():
        rv = rate_variants(stat, design.rate_feats[stat])
        rates = {name: rate_preds(tr, ev, stat, feats, design.rate_params[stat]) for name, feats in rv.items()}
        y_tr, y_ev = tr[col].values, ev[col].values
        combos = {"full": (mins["full"], rates["full"])}
        combos.update({n: (mins[n], rates["full"]) for n in mv if n != "full"})
        combos.update({n: (mins["full"], rates[n]) for n in rv if n != "full"})
        scored = {}
        for name, ((m_oof, m_ev), (r_oof, r_ev)) in combos.items():
            alpha = cm.nb_alpha_mle(y_tr, np.clip(m_oof * r_oof, cm.MU_FLOOR, None))
            mu = np.clip(m_ev * r_ev, cm.MU_FLOOR, None)
            scored[name] = {"ll_rows": cm.nb_logpmf(y_ev, mu, alpha), "abs_err": np.abs(y_ev - mu), "alpha": alpha}
        full = scored["full"]
        rows = []
        for name, s in scored.items():
            if name == "full":
                continue
            ll = boot_diff(ev["gameId"].values, full["ll_rows"] - s["ll_rows"])     # > 0: group helps
            mae = boot_diff(ev["gameId"].values, s["abs_err"] - full["abs_err"])    # > 0: worse without it
            rows.append({"variant": name, "ll_drop": ll, "mae_increase": mae, "alpha": round(s["alpha"], 4)})
        rows.sort(key=lambda r: -r["ll_drop"]["mean"])
        results[stat] = {"full": {"ll": round(float(full["ll_rows"].mean()), 5),
                                  "mae": round(float(full["abs_err"].mean()), 4),
                                  "alpha": round(full["alpha"], 4)},
                         "variants": rows}
        print(f"  {stat} done: {time.perf_counter() - t0:.0f}s", flush=True)

    out = {"generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "data_end_exclusive": DEV_END, "max_date_loaded": str(df["t"].max()),
           "seasons_loaded": [df["season"].min(), df["season"].max()],
           "train": [TRAIN_FIRST, TRAIN_LAST], "eval": EVAL_SEASON, "rows": {"train": len(tr), "eval": len(ev)},
           "eval_games": int(ev["gameId"].nunique()), "design_from": f"{os.path.basename(DEV_MODEL)} ({DEV_SHA})",
           "results": results, "seconds": round(time.perf_counter() - t0, 1), "cpu_logical_cores": os.cpu_count()}
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"challenger_ablation_{dt.datetime.now():%Y%m%dT%H%M%S}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, default=str)
    write_md(out, path.replace(".json", ".md"))
    print(f"wrote {path} (+ .md) in {out['seconds']} s")
    return 0


def _ci(b, digits=4):
    return f"{b['mean']:+.{digits}f} [{b['ci95'][0]:+.{digits}f}, {b['ci95'][1]:+.{digits}f}]"


def write_md(r: dict, path: str) -> None:
    L = ["# Challenger feature-group ablation (exploratory, development data)", "",
         f"Generated {r['generated_utc']}. Fit {r['train'][0]}..{r['train'][1]} ({r['rows']['train']:,} rows), evaluate "
         f"{r['eval']} ({r['rows']['eval']:,} rows per stat, {r['eval_games']:,} games, >= 5 earlier games). Nothing on/after "
         f"{r['data_end_exclusive']} loaded (max date {r['max_date_loaded']}; seasons {r['seasons_loaded'][0]}.."
         f"{r['seasons_loaded'][1]}). Design (features, hyperparameters) from {r['design_from']}; NB alpha by MLE on "
         "out-of-fold predictions over 4 contiguous season blocks, as Stage 1. One change per variant; the other model "
         "stays the full design.", "",
         "**LL drop** = full − variant NB log-likelihood per row (positive: the group helps). **MAE increase** = variant − "
         "full (positive: worse without it). 95% CIs from a game-clustered bootstrap (2,000 resamples). Ranked by LL drop.", ""]
    for stat, x in r["results"].items():
        f = x["full"]
        L += [f"## {stat} (full model: LL {f['ll']:.5f}, MAE {f['mae']:.4f}, alpha {f['alpha']:.4f})", "",
              "| rank | variant | LL drop [95% CI] | MAE increase [95% CI] | alpha |", "|---|---|---|---|---|"]
        for i, v in enumerate(x["variants"], 1):
            L.append(f"| {i} | {v['variant']} | {_ci(v['ll_drop'], 5)} | {_ci(v['mae_increase'])} | {v['alpha']:.4f} |")
        L.append("")
    L.append(f"Run time {r['seconds']} s on CPU ({r['cpu_logical_cores']} logical cores).")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")


if __name__ == "__main__":
    sys.exit(main())
