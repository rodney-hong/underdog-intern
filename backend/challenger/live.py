"""
live.py  (challenger, Stage 3: live use)

The challenger passed its pre-registered one-shot test (experiments/preregistration.md,
entry of commit a760c8c; results commit da21ea4) and is used live as-is: the frozen file
is loaded only if its SHA-256 and its content fingerprint (model.fingerprint) match the
entry, and it is never refitted.

Live features come from the same build_features() as training and the Stage 2 test,
through inference.features_for_context: the player's played regular-season rows that
tipped before the game's tip (schedule time, ET), the player's current team and the opponent
from the caller. No season guard applies here; guard.py governs the research scripts.

  predict(conn, person_id, team, opponent, is_home, tip, stat, lines)
      -> {"status": "ok", mu, alpha, p_over (per line), expected_minutes, rate, ...}
         or {"status": "insufficient_history", ...} with fewer than MIN_PRIOR earlier
         games (the population the test scored).
"""

import os

import numpy as np
import pandas as pd

from . import model as cm
from .features import STAT_COLS
from .inference import MODEL_DIR, features_for_context

FROZEN = {"path": os.path.join(MODEL_DIR, "challenger_stage2_frozen.pkl"),
          "sha256": "0e4fa88fee927f70d8b2a6679310f36b5f12b099d1861266cab11ae53723d604",
          "fingerprint": "d18451fedc40b51e0c7d29b50ba07515ec6b935bd4597d63b2ffee913b81ee7f"}
MIN_PRIOR = 5
STATS = tuple(STAT_COLS)

_loaded = {}


class FrozenModelError(RuntimeError):
    pass


def load_frozen(path: str = FROZEN["path"], sha256: str = FROZEN["sha256"],
                fingerprint: str = FROZEN["fingerprint"]):
    """The frozen challenger, or FrozenModelError if the file's hash or content differs."""
    key = (path, sha256, fingerprint)
    if key not in _loaded:
        if not os.path.exists(path):
            raise FrozenModelError(f"frozen challenger file missing: {path}")
        got = cm.sha256(path)
        if got != sha256:
            raise FrozenModelError(f"{path}: SHA-256 {got} does not match the pre-registered {sha256}")
        m = cm.load(path, sha256)
        fp = cm.fingerprint(m)
        if fp != fingerprint:
            raise FrozenModelError(f"{path}: fingerprint {fp} does not match the pre-registered {fingerprint}")
        _loaded[key] = m
    return _loaded[key]


_team_ids = {}


def team_ids(conn) -> dict:
    """Team nickname (player_stats.playerteamName) -> its most recent team id. Cached per
    database file and newest box-score date."""
    key = (conn.execute("PRAGMA database_list").fetchone()[2],
           conn.execute("SELECT MAX(gameDateTimeEst) FROM player_stats").fetchone()[0])
    if key not in _team_ids:
        # SQLite: the bare column comes from the row holding MAX(gameDateTimeEst)
        _team_ids[key] = {name: int(tid) for name, tid, _ in conn.execute(
            "SELECT playerteamName, playerteamId, MAX(gameDateTimeEst) FROM player_stats "
            "WHERE playerteamId IS NOT NULL GROUP BY playerteamName")}
    return _team_ids[key]


def nickname(team: str) -> str:
    """'Portland Trail Blazers' -> 'Trail Blazers'; 'Boston Celtics' -> 'Celtics'."""
    if not team:
        return ""
    return "Trail Blazers" if "Blazers" in team else team.split()[-1]


def features(conn, person_id: int, team: str, opponent: str, is_home: bool, tip: str):
    """(feature row, data_as_of) for the upcoming game; team names are full or nicknames."""
    ids = team_ids(conn)
    t, o = ids.get(nickname(team)), ids.get(nickname(opponent))
    if t is None or o is None:
        raise ValueError(f"unknown team: {team if t is None else opponent}")
    F, ctx = features_for_context(conn, int(person_id),
                                  {"tip": tip, "home": int(bool(is_home)), "team": t, "opp": o, "game_id": 0})
    return F, ctx["data_as_of"]


def predict_from_features(F: pd.DataFrame, stat: str, lines, m=None) -> dict:
    m = m or load_frozen()
    n_prior = int(F.iloc[0]["n_prior"])
    if n_prior < MIN_PRIOR:
        return {"status": "insufficient_history", "n_prior": n_prior}
    pred = m.predict(F, stat)
    mu, alpha = float(pred["mu"][0]), float(m.alpha[stat])
    feats = F.iloc[0][m.minutes_feats + m.rate_feats[stat]]
    return {"status": "ok", "n_prior": n_prior, "mu": mu, "alpha": alpha,
            "expected_minutes": float(pred["minutes"][0]), "rate": float(pred["rate"][0]),
            "p_over": {float(L): float(cm.p_over(mu, alpha, L)) for L in lines},
            "features": {k: (None if pd.isna(v) else float(v)) for k, v in feats.items()},
            "model_sha256": FROZEN["sha256"], "model_fingerprint": FROZEN["fingerprint"]}


def predict(conn, person_id: int, team: str, opponent: str, is_home: bool, tip: str,
            stat: str, lines) -> dict:
    if stat not in STATS:
        raise ValueError(f"stat must be one of {list(STATS)}")
    m = load_frozen()
    F, as_of = features(conn, person_id, team, opponent, is_home, tip)
    return {**predict_from_features(F, stat, lines, m), "data_as_of": as_of}


def explanation(player_name: str, stat: str, line: float, r: dict, opponent: str) -> str:
    """Plain-language reasons from the challenger's own drivers: expected minutes x per-minute
    rate = projected mean, compared with the line."""
    f = r["features"]
    first = player_name.split()[0]
    col = STAT_COLS[stat]
    p = r["p_over"][float(line)]
    side = "OVER" if p > 0.5 else "UNDER"
    parts = [f"Projected {stat}: {r['mu']:.1f} against the {line:g} line, "
             f"so P(over) = {p:.0%} and the pick is {side}."]
    mins = f"{first} is expected to play {r['expected_minutes']:.1f} minutes"
    recent = [(f.get("min_h3"), "last few games"), (f.get("min_h30"), "longer run")]
    recent = [f"{v:.1f} ({lab})" for v, lab in recent if v is not None]
    parts.append(mins + (f"; recent averages: {' and '.join(recent)}." if recent else "."))
    per36, recent36 = r["rate"] * 36, f.get(f"rate_{col}_h10")
    rate_txt = f"{first} projects at {per36:.1f} {stat} per 36 minutes"
    if recent36 is not None:
        rate_txt += f" (recent: {recent36 * 36:.1f})"
    parts.append(rate_txt + f": {r['expected_minutes']:.1f} min x {r['rate']:.3f} per minute = {r['mu']:.1f}.")
    ctx = []
    opp = f.get(f"opp_rel_{col}")
    if opp is not None and abs(opp - 1) >= 0.05:
        ctx.append(f"over their last 5 games, {opponent} allowed {abs(opp - 1):.0%} "
                   f"{'more' if opp > 1 else 'fewer'} {stat} than the league average")
    gsa = f.get("games_since_absence")
    if gsa is not None and gsa < 3:
        ctx.append(f"{first} is within 3 games of returning from an absence" if gsa
                   else f"{first} is returning from an absence")
    if f.get("b2b"):
        ctx.append("it is the second night of a back-to-back")
    if ctx:
        parts.append("Also: " + "; ".join(ctx) + ".")
    return " ".join(parts)
