"""
model.py  (challenger, Stage 1)

Pooled minutes x rate model per stat, combined into a negative binomial:
  minutes   E[minutes | played] from a HistGradientBoosting regressor (squared error);
            one model shared by the four stats.
  rate      per-minute production: HistGradientBoosting with Poisson loss on
            stat / minutes, weighted by the actual minutes (= Poisson with exposure).
  mean      mu = E[minutes] x rate;  Y ~ NB2(mu, alpha): Var = mu + alpha mu^2, with alpha
            per stat fitted by maximum likelihood on out-of-fold predictions.
P(stat > L) = NB survival function at floor(L), for any line L.
"""

import hashlib
import pickle

import numpy as np
import pandas as pd
from scipy import optimize
from scipy.special import gammaln
from scipy import stats as sps
from sklearn.ensemble import HistGradientBoostingRegressor

SEED = 7
MIN_FLOOR = 1.0          # E[minutes] floor (played rows only)
MU_FLOOR = 1e-4


def hgb(params: dict, loss: str) -> HistGradientBoostingRegressor:
    return HistGradientBoostingRegressor(
        loss=loss, learning_rate=params["learning_rate"], max_iter=params["max_iter"],
        max_leaf_nodes=params["max_leaf_nodes"], min_samples_leaf=params["min_samples_leaf"],
        l2_regularization=params.get("l2_regularization", 0.0), early_stopping=False,
        random_state=SEED)


def fit_minutes(X: pd.DataFrame, minutes: np.ndarray, params: dict):
    return hgb(params, "squared_error").fit(X, minutes)


def fit_rate(X: pd.DataFrame, y: np.ndarray, minutes: np.ndarray, params: dict):
    return hgb(params, "poisson").fit(X, y / minutes, sample_weight=minutes)


# --- negative binomial ----------------------------------------------------------------

def nb_logpmf(y, mu, alpha) -> np.ndarray:
    y = np.asarray(y, dtype=float)
    mu = np.clip(np.asarray(mu, dtype=float), MU_FLOOR, None)
    r = 1.0 / alpha
    return (gammaln(y + r) - gammaln(r) - gammaln(y + 1)
            + r * np.log(r / (r + mu)) + y * np.log(mu / (r + mu)))


def nb_alpha_mle(y, mu) -> float:
    """alpha maximizing the NB2 log-likelihood with mu fixed (bounded search on log alpha)."""
    res = optimize.minimize_scalar(lambda la: -np.sum(nb_logpmf(y, mu, np.exp(la))),
                                   bounds=(np.log(1e-5), np.log(5.0)), method="bounded",
                                   options={"xatol": 1e-6})
    return float(np.exp(res.x))


def p_over(mu, alpha, line) -> np.ndarray:
    """P(Y > line) under NB2(mu, alpha)."""
    mu = np.clip(np.asarray(mu, dtype=float), MU_FLOOR, None)
    r = 1.0 / alpha
    return sps.nbinom.sf(np.floor(line), r, r / (r + mu))


# --- the fitted challenger ------------------------------------------------------------

class Challenger:
    """Everything needed to predict: feature lists, fitted models, alphas, provenance."""

    def __init__(self, minutes_feats, minutes_params, rate_feats, rate_params, meta):
        self.minutes_feats = list(minutes_feats)
        self.minutes_params = dict(minutes_params)
        self.rate_feats = {s: list(v) for s, v in rate_feats.items()}
        self.rate_params = {s: dict(v) for s, v in rate_params.items()}
        self.meta = dict(meta)
        self.minutes_model = None
        self.rate_models = {}
        self.alpha = {}

    def predict_minutes(self, F: pd.DataFrame) -> np.ndarray:
        return np.maximum(self.minutes_model.predict(F[self.minutes_feats]), MIN_FLOOR)

    def predict(self, F: pd.DataFrame, stat: str, emin: np.ndarray | None = None) -> dict:
        emin = self.predict_minutes(F) if emin is None else emin
        rate = self.rate_models[stat].predict(F[self.rate_feats[stat]])
        return {"minutes": emin, "rate": rate, "mu": np.clip(emin * rate, MU_FLOOR, None)}


def save(obj, path: str) -> str:
    with open(path, "wb") as f:
        pickle.dump(obj, f, protocol=4)
    return sha256(path)


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _hgb_bytes(m) -> bytes:
    parts = [np.asarray(m._baseline_prediction).tobytes()]
    parts += [np.asarray(t).tobytes() for t in m._bin_mapper.bin_thresholds_]
    parts += [p.nodes.tobytes() for row in m._predictors for p in row]
    return b"".join(parts)


def fingerprint(obj) -> str:
    """SHA-256 of the fitted content (trees, bin thresholds, baselines, alphas, design).
    The pickle's bytes differ between identical refits (RNG and loss objects), so the file
    SHA-256 pins the artifact and this fingerprint checks that a refit reproduces it."""
    h = hashlib.sha256()
    if isinstance(obj, Challenger):
        h.update(repr((obj.minutes_feats, sorted(obj.minutes_params.items()),
                       sorted((k, v) for k, v in obj.rate_feats.items()),
                       sorted((k, sorted(v.items())) for k, v in obj.rate_params.items()),
                       sorted(obj.alpha.items()))).encode())
        h.update(_hgb_bytes(obj.minutes_model))
        for s in sorted(obj.rate_models):
            h.update(_hgb_bytes(obj.rate_models[s]))
    else:                                            # baseline (b): {"features", "models", "alpha"}
        h.update(repr((obj["features"], sorted(obj["alpha"].items()))).encode())
        for s in sorted(obj["models"]):
            h.update(_hgb_bytes(obj["models"][s]))
    return h.hexdigest()


def load(path: str, expected_sha: str | None = None):
    if expected_sha is not None and sha256(path) != expected_sha:
        raise ValueError(f"model file {path} does not match SHA-256 {expected_sha}")
    with open(path, "rb") as f:
        return pickle.load(f)
