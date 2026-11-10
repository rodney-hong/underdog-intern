"""
assistant.py: the read-only "Ask" assistant behind POST /assistant.

An LLM (Ollama Cloud, ASSISTANT_MODEL) answers questions about the app's picks, but it never
writes SQL and never computes a number: it can only call the whitelisted tools below, which
return JSON, and phrase what they return. It is not a predictor.

Every tool opens its own read-only connection (file:...?mode=ro) and closes it, so nothing here
can write to predictions.db. Outcome columns of paper_predictions, challenger_predictions and
their *_pre_* tables (actual_value, actual_points, result, resolved_at, status) are never
selected: those tables are read only through _select() with the column whitelists
PAPER_COLS / CHALLENGER_COLS. Track records come only from the app's own picks (predictions),
through database.player_track_record, the function behind History's tooltip.

Tools:
  list_pending(date, stat, player)  pending app picks (all dates unless one is given) + challenger
                                  row + track record, ranked by confidence (track record only a
                                  tie-breaker within TIE_CONFIDENCE, both n >= RANK_MIN_N);
                                  confidence >= HIGH_CONFIDENCE flagged and ranked last
  track_record(player, stat, model)  = GET /players/{player}/track-record

Players match as in player_match (exact rosters.name_key: case, accents, suffixes ignored);
stats accept aliases (STAT_ALIASES: threes, 3-pointers, pts, reb, ast, ...).
  explain(prediction_id)          frozen model's per-feature log-odds contributions on the
                                  logged features (XGBoost pred_contribs; logistic models:
                                  coefficient x standardized value) + challenger
                                  decomposition (minutes x rate -> mean, NB2 dispersion -> P(over))
  injury_context(player, game)    latest official-report and ESPN items for the team
"""

import json
import math
import os
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

import database
from rosters import name_key

ASSISTANT_MODEL = "gemma4:31b"
OLLAMA_CLOUD_URL = os.getenv("OLLAMA_CLOUD_URL", "https://ollama.com")
MAX_TOOL_ROUNDS = 6
LLM_TIMEOUT_SECONDS = 120
ESPN_LOOKBACK_DAYS = 21

# The only columns ever read from the paper / challenger tables (no outcome columns).
PAPER_COLS = ("id", "captured_at", "player_name", "stat_type", "ud_line", "feature_def", "features_json",
              "p_over", "model_file", "model_sha_ok", "data_as_of")
CHALLENGER_COLS = ("id", "captured_at", "player_name", "stat_type", "line", "market_key", "n_prior", "mu",
                   "alpha", "p_over", "expected_minutes", "rate_per_minute", "data_as_of")
_WHITELIST = {"paper_predictions": PAPER_COLS, "challenger_predictions": CHALLENGER_COLS}

# Never selected from those tables, and never a key in any tool's output.
OUTCOME_COLUMNS = ("actual_value", "actual_points", "result", "resolved_at", "status", "actual_result")


class AssistantUnavailable(RuntimeError):
    """The LLM can't be reached or isn't configured."""


def _connect():
    conn = sqlite3.connect(f"file:{database.DB_PATH}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _select(conn, table: str, cols: tuple, where: str, params: tuple) -> list[dict]:
    """SELECT whitelisted columns from a paper / challenger table."""
    allowed = _WHITELIST[table]
    if not set(cols) <= set(allowed):
        raise ValueError(f"{table}: columns {sorted(set(cols) - set(allowed))} are not whitelisted")
    sql = f"SELECT {', '.join(cols)} FROM {table} WHERE {where} ORDER BY captured_at DESC"
    return [dict(r) for r in conn.execute(sql, params)]


def _latest_at_line(conn, table: str, cols: tuple, line_col: str, player: str, stat: str, date: str,
                    line: float) -> dict | None:
    """The latest logged row for this player (name_key match), stat, game date and line."""
    rows = _select(conn, table, cols, f"stat_type = ? AND game_date = ? AND ABS({line_col} - ?) < 0.001",
                   (stat, date, line))
    key = name_key(player)
    return next((r for r in rows if name_key(r["player_name"]) == key), None)


# Stat words a question may use -> the app's stat_type. Keys are normalized by _stat_key.
STAT_ALIASES = {
    "points": "Points", "point": "Points", "pts": "Points", "pt": "Points",
    "rebounds": "Rebounds", "rebound": "Rebounds", "reb": "Rebounds", "rebs": "Rebounds", "boards": "Rebounds",
    "assists": "Assists", "assist": "Assists", "ast": "Assists", "asts": "Assists", "dimes": "Assists",
    "3pm": "3PM", "threes": "3PM", "three": "3PM", "3s": "3PM", "3pt": "3PM", "3pts": "3PM", "3ptm": "3PM",
    "3pointers": "3PM", "3pointer": "3PM", "threepointers": "3PM", "threepointer": "3PM",
    "3pointersmade": "3PM", "threepointersmade": "3PM", "threesmade": "3PM",
    "pra": "PRA", "ptsrebast": "PRA", "pointsreboundsassists": "PRA",
    "pr": "PR", "ptsreb": "PR", "pointsrebounds": "PR",
    "pa": "PA", "ptsast": "PA", "pointsassists": "PA",
    "ra": "RA", "rebast": "RA", "reboundsassists": "RA",
}
STATS = ("Points", "Rebounds", "Assists", "3PM", "PRA", "PR", "PA", "RA")


def _stat_key(s: str) -> str:
    """'3-Pointers' -> '3pointers', 'Pts + Reb' -> 'ptsreb', 'points and rebounds' -> 'pointsrebounds'."""
    return "".join(w for w in "".join(c if c.isalnum() else " " for c in s.lower()).split() if w != "and")


def canonical_stat(stat: str | None) -> str | None:
    """'threes' / '3-pointers' / 'pts' / 'Points' -> the app's stat_type; ValueError if unknown."""
    if stat is None or not str(stat).strip():
        return None
    got = STAT_ALIASES.get(_stat_key(str(stat)))
    if got is None:
        raise ValueError(f"unknown stat {stat!r}; use one of {list(STATS)}")
    return got


def _match_player(conn, player: str, candidates) -> list[str] | dict:
    """The candidate names that are this player: exact rosters.name_key (case, accents, punctuation
    and Jr./II ignored), as player_match does. With no exact match, player_match.match_name's
    collision check decides the message: {"error": ...} for an ambiguous or unknown name."""
    import player_match
    candidates = sorted(set(candidates))
    key = name_key(player)
    exact = [c for c in candidates if name_key(c) == key]
    if exact:
        return exact            # one player, possibly spelled more than one way in the picks
    m = player_match.match_name(conn, player, candidates)
    return {"error": m.get("flag") or f"no picks found for {player!r} (names match exactly, "
                                      f"ignoring case, accents and suffixes; use the full name)"}


def _resolve_player(conn, player: str) -> str | dict:
    """The name the app's picks use for this player, or {"error"}."""
    names = [n for (n,) in conn.execute("SELECT DISTINCT player_name FROM predictions")]
    m = _match_player(conn, player, names)
    return m if isinstance(m, dict) else m[0]


RANK_MIN_N = 20     # a track record affects ranking only with at least this many resolved picks


def record_text(tr: dict) -> str:
    """'2–0 (2 picks), adjusted 63%, small sample' (en dash; never '2-0-2')."""
    if not tr["n"]:
        return "no resolved picks yet"
    text = f"{tr['wins']}–{tr['losses']} ({tr['n']} pick{'s' if tr['n'] != 1 else ''})"
    if tr["adjusted_rate"] is not None:
        text += f", adjusted {round(tr['adjusted_rate'] * 100)}%"
    return text + (", small sample" if not tr["enough"] else "")


def _record(conn, player: str, stat: str, league: str, model: str) -> dict:
    tr = database.player_track_record(player, stat, league, None, model, conn=conn)
    return {**tr, "small_sample": not tr["enough"], "record_text": record_text(tr)}


TIE_CONFIDENCE = 0.02      # picks this close in confidence (2 percentage points) tie-break on track record
HIGH_CONFIDENCE = 0.97     # at or above: flagged, and never ranked as the top pick
HIGH_CONFIDENCE_FLAG = "unusually high — check that the line was entered correctly"


def high_confidence_flag(confidence: float) -> str | None:
    return HIGH_CONFIDENCE_FLAG if confidence >= HIGH_CONFIDENCE else None


def _record_counts(p: dict) -> bool:
    tr = p["track_record"]
    return tr["n"] >= RANK_MIN_N and tr["adjusted_rate"] is not None


def _wins_tie(a: dict, b: dict) -> bool:
    """a goes ahead of b (b ranked above it on confidence): within TIE_CONFIDENCE, both records
    with n >= RANK_MIN_N, and a's adjusted record is better."""
    return (b["confidence"] - a["confidence"] <= TIE_CONFIDENCE + 1e-9 and _record_counts(a) and _record_counts(b)
            and a["track_record"]["adjusted_rate"] > b["track_record"]["adjusted_rate"])


def rank_picks(picks: list[dict]) -> list[dict]:
    """Confidence order. A track record is only a tie-breaker: a pick moves ahead of the picks just
    above it while _wins_tie holds, so it never passes one more than TIE_CONFIDENCE more confident.
    Picks at or above HIGH_CONFIDENCE come after all others (flagged, never the top pick)."""
    ranked = []
    for p in sorted(picks, key=lambda p: -p["confidence"]):
        j = len(ranked)
        while j > 0 and _wins_tie(p, ranked[j - 1]):
            j -= 1
        p["tie_break_on_record"] = j < len(ranked)
        ranked.insert(j, p)
    ranked = [p for p in ranked if not p["high_confidence_flag"]] + [p for p in ranked if p["high_confidence_flag"]]
    for i, p in enumerate(ranked, 1):
        p["rank"] = i
    return ranked


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def track_record(player: str, stat: str, model: str = "challenger", league: str = "NBA") -> dict:
    """Exactly History's tooltip (GET /players/{player}/track-record), plus small_sample and
    record_text. Stat aliases and player name matching as list_pending."""
    stat = canonical_stat(stat)
    conn = _connect()
    try:
        name = _resolve_player(conn, player)
        if isinstance(name, dict):
            return name
        return _record(conn, name, stat, league, model)
    finally:
        conn.close()


MAX_PICKS = 50      # picks returned after ranking; count says how many matched


def list_pending(date: str | None = None, stat: str | None = None, player: str | None = None) -> dict:
    """Pending (unresolved, current) live app picks: all of them, or for one ET game date, stat
    (aliases accepted) or player (name matching as player_match). Each with the matching
    challenger row (same player, stat, date and line; non-outcome columns) and the track record
    of the model that made the pick. Ranked by rank_picks: confidence, track record only as a
    tie-breaker; confidence >= HIGH_CONFIDENCE flagged and ranked last."""
    stat = canonical_stat(stat)
    conn = _connect()
    try:
        sql = ("SELECT id, timestamp, player_name, stat_type, stat_line, opponent_team, predicted_outcome, "
               "confidence, COALESCE(model, 'frozen') AS model, COALESCE(league, 'NBA') AS league, "
               "COALESCE(game_date, substr(timestamp, 1, 10)) AS game_date, "
               "COALESCE(is_limited_data, 0) AS is_limited_data FROM predictions "
               "WHERE actual_result IS NULL AND COALESCE(is_current, 1) = 1 "
               "AND COALESCE(model, 'frozen') IN ('challenger', 'frozen')")
        params = []
        if date:
            sql += " AND COALESCE(game_date, substr(timestamp, 1, 10)) = ?"
            params.append(date)
        if stat:
            sql += " AND stat_type = ?"
            params.append(stat)
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY id", params).fetchall()]
        if player:
            names = _match_player(conn, player, [r["player_name"] for r in rows])
            if isinstance(names, dict):
                return {"date": date or "all pending", "stat": stat, "player": player, "count": 0, "picks": [],
                        "note": f"no pending picks for {player!r}" + (f" ({stat})" if stat else "")
                                + (f" on {date}" if date else "") + f": {names['error']}"}
            rows = [r for r in rows if r["player_name"] in names]
        picks = []
        for p in rows:
            p["prediction_id"] = p.pop("id")
            ch = _latest_at_line(conn, "challenger_predictions", CHALLENGER_COLS, "line",
                                 p["player_name"], p["stat_type"], p["game_date"], p["stat_line"])
            p["challenger"] = ({k: ch[k] for k in ("mu", "p_over", "expected_minutes", "rate_per_minute",
                                                   "alpha", "captured_at")} if ch else None)
            p["track_record"] = _record(conn, p["player_name"], p["stat_type"], p["league"], p["model"])
            p["high_confidence_flag"] = high_confidence_flag(p["confidence"])
            picks.append(p)
    finally:
        conn.close()
    picks = rank_picks(picks)
    # flagged picks rank last but are always returned, so their warning is never cut off
    flagged = [p for p in picks if p["high_confidence_flag"]]
    shown = [p for p in picks if not p["high_confidence_flag"]][:MAX_PICKS] + flagged
    return {"date": date or "all pending", "stat": stat, "player": player, "count": len(picks),
            "shown": len(shown), "picks": shown,
            "high_confidence_flagged": len(flagged),
            "sorted_by": f"rank: model confidence; within {TIE_CONFIDENCE * 100:g} points of confidence, the better "
                         f"adjusted track record goes first when both have n >= {RANK_MIN_N}; picks with "
                         f"confidence >= {HIGH_CONFIDENCE:.0%} are flagged and listed last"}


def _frozen_contributions(conn, pick: dict, date: str) -> dict:
    import pandas as pd
    import xgboost as xgb
    import predictor

    stat, line = pick["stat_type"], pick["stat_line"]
    if pick["league"] != "NBA" or stat not in predictor._ML_STAT_TYPES:
        return {"available": False, "reason": f"no frozen NBA model is logged for {stat}"}
    row = _latest_at_line(conn, "paper_predictions", PAPER_COLS, "ud_line", pick["player_name"], stat, date, line)
    if row is None or not row["features_json"]:
        return {"available": False,
                "reason": f"no logged frozen-model features for this player, {stat} at {line:g} on {date}"}
    model = predictor._load_models().get(stat)
    if model is None:
        return {"available": False, "reason": f"model_{stat}.pkl is not loaded"}
    feats = json.loads(row["features_json"])
    X = pd.DataFrame([feats])[predictor.FEATURE_COLS].fillna(0).astype(float)   # paper_predictions' convention
    if hasattr(model, "get_booster"):          # XGBClassifier: native SHAP-style contributions
        method = "xgboost pred_contribs"
        *per_feature, bias = (float(c) for c in model.get_booster().predict(xgb.DMatrix(X), pred_contribs=True)[0])
    elif hasattr(model, "steps") and [type(s).__name__ for _, s in model.steps] == ["StandardScaler",
                                                                                    "LogisticRegression"]:
        method = "logistic regression: coefficient x standardized value"   # exact for a linear model
        scaler, lr = model.steps[0][1], model.steps[-1][1]
        per_feature = [float(v) for v in lr.coef_[0] * scaler.transform(X)[0]]
        bias = float(lr.intercept_[0])
    else:
        return {"available": False, "reason": f"no contribution method for a {type(model).__name__} model"}
    margin = sum(per_feature) + bias
    items = [{"feature": f, "value": float(X.at[0, f]), "log_odds": c}
             for f, c in zip(predictor.FEATURE_COLS, per_feature)]
    items.sort(key=lambda d: -abs(d["log_odds"]))
    return {"available": True,
            "source": "features the frozen model scored in the pre-tip log (paper_predictions), not the pick itself",
            "method": method,
            "captured_at": row["captured_at"], "line": row["ud_line"], "feature_def": row["feature_def"],
            "data_as_of": row["data_as_of"], "model_sha_ok": row["model_sha_ok"],
            "p_over_logged": row["p_over"], "p_over_recomputed": 1 / (1 + math.exp(-margin)),
            "base_log_odds": bias, "contributions": items,
            "units": "log-odds; positive pushes toward OVER, negative toward UNDER; base + all = logit(P(over))"}


def _challenger_decomposition(conn, pick: dict, date: str) -> dict:
    from challenger import model as cm
    from challenger.live import STATS

    stat, line = pick["stat_type"], pick["stat_line"]
    if pick["league"] != "NBA" or stat not in STATS:
        return {"available": False, "reason": f"the challenger does not cover {stat}"}
    row = _latest_at_line(conn, "challenger_predictions", CHALLENGER_COLS, "line", pick["player_name"], stat,
                          date, line)
    if row is None or row["mu"] is None:
        return {"available": False, "reason": f"no logged challenger row for this player, {stat} at {line:g} on {date}"}
    mu, alpha = row["mu"], row["alpha"]
    var = mu + alpha * mu * mu
    return {"available": True, "captured_at": row["captured_at"], "line": row["line"],
            "data_as_of": row["data_as_of"], "n_prior_games": row["n_prior"],
            "expected_minutes": row["expected_minutes"], "rate_per_minute": row["rate_per_minute"],
            "minutes_x_rate": row["expected_minutes"] * row["rate_per_minute"],
            "mu": mu, "alpha": alpha, "variance": var, "sd": math.sqrt(var),
            "over_means": f"{stat} >= {math.floor(line) + 1}",
            "p_over_logged": row["p_over"], "p_over_recomputed": float(cm.p_over(mu, alpha, line)),
            "how": "mean = expected minutes x rate per minute; negative binomial with Var = mu + alpha*mu^2; "
                   "P(over) = P(stat > line)"}


def explain(prediction_id: int) -> dict:
    conn = _connect()
    try:
        r = conn.execute("SELECT id, timestamp, player_name, stat_type, stat_line, opponent_team, predicted_outcome, "
                         "confidence, explanation, COALESCE(model, 'frozen') AS model, "
                         "COALESCE(league, 'NBA') AS league, game_date FROM predictions WHERE id = ?",
                         (int(prediction_id),)).fetchone()
        if r is None:
            return {"error": f"prediction {prediction_id} not found"}
        pick = dict(r)
        date = pick["game_date"] or pick["timestamp"][:10]
        return {"prediction_id": pick.pop("id"), "pick": pick,
                "confidence_note": "confidence = the pick model's probability of the side it picked",
                "high_confidence_flag": high_confidence_flag(pick["confidence"]),
                "frozen_model": _frozen_contributions(conn, pick, date),
                "challenger": _challenger_decomposition(conn, pick, date)}
    finally:
        conn.close()


def _team_for_player(conn, player: str) -> str | None:
    key = name_key(player)
    row = conn.execute("SELECT team_city, team_name FROM rosters WHERE name_key = ? LIMIT 1", (key,)).fetchone()
    return f"{row['team_city']} {row['team_name']}" if row else None


def _resolve_teams(conn, game: str) -> list[str]:
    """Full team names for a matchup ('MEM@POR', as the official report writes it) or a team name."""
    if "@" in game:
        return [t for (t,) in conn.execute("SELECT DISTINCT team FROM news_events WHERE matchup = ?",
                                           (game.strip().upper(),))]
    g = game.strip().lower()
    teams = {f"{c} {n}" for c, n, a in conn.execute("SELECT DISTINCT team_city, team_name, team_abbrev FROM rosters")
             if g in f"{c} {n}".lower() or n.lower() in g or (a or "").lower() == g}
    return sorted(teams)


def injury_context(player: str | None = None, game: str | None = None) -> dict:
    """Latest official injury report rows and ESPN items for a player's team (the player
    flagged) or a game (a matchup like 'MEM@POR', or a team name)."""
    if not player and not game:
        return {"error": "give a player or a game"}
    conn = _connect()
    try:
        teams = []
        if player:
            team = _team_for_player(conn, player)
            if team is None:
                return {"error": f"{player} is not on a current roster"}
            teams = [team]
        else:
            teams = _resolve_teams(conn, game)
            if not teams:
                return {"error": f"no team found for {game!r}"}
        key = name_key(player) if player else None
        since = (datetime.now(ZoneInfo("UTC")) - timedelta(days=ESPN_LOOKBACK_DAYS)).isoformat()
        out = []
        for team in teams:
            last = conn.execute("SELECT MAX(game_date) FROM news_events WHERE team = ? AND kind = 'row'",
                                (team,)).fetchone()[0]
            official = {}
            for r in conn.execute("SELECT player, status AS injury_status, previous_status AS previous_injury_status, "
                                  "source_time, game_date, matchup "
                                  "FROM news_events WHERE team = ? AND kind = 'row' AND game_date = ? "
                                  "ORDER BY source_time", (team, last)):
                official[r["player"]] = {**dict(r), "is_asked_player": key is not None and name_key(r["player"]) == key}
            espn = {}
            for r in conn.execute("SELECT raw_text, published_at FROM news_items WHERE source = 'espn_injuries' "
                                  "AND raw_text LIKE ? AND COALESCE(published_at, fetched_at) >= ? "
                                  "ORDER BY COALESCE(published_at, fetched_at)", (f"{team} | %", since)):
                parts = [p.strip() for p in r["raw_text"].split(" | ")]
                if len(parts) < 3:
                    continue
                espn[parts[1]] = {"player": parts[1], "injury_status": parts[2], "note": " | ".join(parts[3:])[:400],
                                  "published_at": r["published_at"],
                                  "is_asked_player": key is not None and name_key(parts[1]) == key}
            out.append({"team": team, "official_report_game_date": last,
                        "official_report": list(official.values()), "espn": list(espn.values())})
        return {"player": player, "game": game, "teams": out,
                "note": "official_report: latest status per player for the team's latest report game date; "
                        f"espn: latest item per player in the last {ESPN_LOOKBACK_DAYS} days"}
    finally:
        conn.close()


TOOLS = {"list_pending": list_pending, "track_record": track_record, "explain": explain,
         "injury_context": injury_context}

TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "list_pending",
        "description": "The user's pending app picks, in rank order (rank 1 first). With no date it searches ALL pending "
                       "picks, whatever the date. Filter by player and/or stat to find one pick. Each pick has "
                       "player, stat, line, side, model, confidence, game_date, the challenger model's mean/"
                       "P(over)/expected minutes/rate when logged, the track record (record_text) and a "
                       "prediction_id for explain().",
        "parameters": {"type": "object", "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD (ET) game date; omit to search all pending picks"},
            "stat": {"type": "string", "description": "e.g. Points, Rebounds, Assists, 3PM, PRA, PR, PA, RA "
                                                      "(threes, pts, reb, ast also work); omit for all"},
            "player": {"type": "string", "description": "player's full name, any case or accents; omit for all"}}}}},
    {"type": "function", "function": {
        "name": "track_record",
        "description": "The app's record on one player and stat (same numbers as History's tooltip): wins, losses, "
                       "n, raw rate, 95% interval, adjusted rate (shrunk toward the all-players baseline), "
                       "baseline, small_sample flag and record_text.",
        "parameters": {"type": "object", "required": ["player", "stat"], "properties": {
            "player": {"type": "string"}, "stat": {"type": "string"},
            "model": {"type": "string", "enum": ["challenger", "frozen", "challenger_retro", "all"],
                      "description": "default challenger (History's default)"}}}}},
    {"type": "function", "function": {
        "name": "explain",
        "description": "Why a pick has its confidence: the frozen model's per-feature contributions (log-odds) and "
                       "the challenger's decomposition (expected minutes x rate -> mean, dispersion -> P(over)). "
                       "For a named player, first call list_pending with player and stat (no date) to get the "
                       "prediction_id.",
        "parameters": {"type": "object", "required": ["prediction_id"], "properties": {
            "prediction_id": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "injury_context",
        "description": "Latest official injury report and ESPN injury items for a player's team (the player is "
                       "flagged) or for a game (matchup like 'MEM@POR', or a team name).",
        "parameters": {"type": "object", "properties": {
            "player": {"type": "string"}, "game": {"type": "string"}}}}},
]

SYSTEM_PROMPT = """You are the read-only research assistant of SportsBettr, a research log of NBA player-prop \
predictions. Nothing is bet. Answer the user's question using only the tools.

Rules:
- Every number you state must come from a tool result. Never estimate, compute new statistics or guess.
- When the user names no date, search all pending picks (call list_pending without a date).
- When ranking or comparing picks, keep list_pending's order (the rank field: model confidence; the track \
record only breaks near-ties). Give confidence as the model's probability for its side, and the track \
record as context by copying track_record.record_text exactly, e.g. "2–0 (2 picks), adjusted 63%, small \
sample". Never write records any other way (never "2-0-2").
- When a pick has high_confidence_flag, write next to it: "unusually high — check that the line was entered \
correctly". Never present a flagged pick as the top pick.
- No model has a measured edge. Never call a pick a lock, a value or a good bet, and never give betting advice.
- If a tool says something is unavailable, say so instead of filling the gap.
- Keep answers short and readable on a phone: a few bullet points or a short list, percentages rounded to whole numbers."""


# ---------------------------------------------------------------------------
# LLM loop
# ---------------------------------------------------------------------------

def run_tool(name: str, args) -> dict:
    """Run one whitelisted tool; a bad call returns {"error"} for the model to read."""
    fn = TOOLS.get(name)
    if fn is None:
        return {"error": f"unknown tool {name!r}; tools: {sorted(TOOLS)}"}
    if isinstance(args, str):
        try:
            args = json.loads(args or "{}")
        except json.JSONDecodeError:
            return {"error": "arguments are not valid JSON"}
    try:
        return fn(**(args or {}))
    except (TypeError, ValueError, KeyError) as exc:
        return {"error": f"{name}: {exc}"}


def _chat(messages: list[dict]) -> dict:
    key = os.getenv("OLLAMA_API_KEY", "").strip()
    if not key:
        raise AssistantUnavailable("OLLAMA_API_KEY is not set in backend/.env")
    body = {"model": ASSISTANT_MODEL, "messages": messages, "tools": TOOL_SPECS, "stream": False,
            "think": False, "options": {"temperature": 0}}
    try:
        resp = httpx.post(f"{OLLAMA_CLOUD_URL}/api/chat", json=body, headers={"Authorization": f"Bearer {key}"},
                          timeout=httpx.Timeout(LLM_TIMEOUT_SECONDS, connect=10))
    except httpx.HTTPError as exc:
        raise AssistantUnavailable(f"Ollama Cloud could not be reached ({type(exc).__name__})") from exc
    if resp.status_code >= 400:
        raise AssistantUnavailable(f"Ollama Cloud returned HTTP {resp.status_code}")
    return resp.json()["message"]


def ask(history: list[dict], prediction_id: int | None = None) -> dict:
    """history: [{role: user|assistant, content}], ending with the user's question. With
    prediction_id (History's Explain button), explain() runs for that exact pick first and its
    result is in the conversation before the LLM answers: no name matching involved.
    Returns {answer, tools_used: [{name, arguments}]}."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history]
    used = []
    if prediction_id is not None:
        args = {"prediction_id": int(prediction_id)}
        messages.append({"role": "assistant", "content": "",
                         "tool_calls": [{"function": {"name": "explain", "arguments": args}}]})
        messages.append({"role": "tool", "tool_name": "explain",
                         "content": json.dumps(run_tool("explain", args), default=str)})
        used.append({"name": "explain", "arguments": args})
    for _ in range(MAX_TOOL_ROUNDS):
        msg = _chat(messages)
        calls = msg.get("tool_calls") or []
        if not calls:
            return {"answer": (msg.get("content") or "").strip(), "tools_used": used}
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for call in calls:
            fn = call.get("function", {})
            name, args = fn.get("name"), fn.get("arguments") or {}
            used.append({"name": name, "arguments": args})
            messages.append({"role": "tool", "tool_name": name,
                             "content": json.dumps(run_tool(name, args), default=str)})
    return {"answer": "I couldn't finish that within the tool-call limit. Try a narrower question.",
            "tools_used": used}
