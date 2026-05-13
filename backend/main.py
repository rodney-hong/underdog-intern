"""
main.py
FastAPI application for NBA Player Prop Predictor.
Run with: uvicorn main:app --reload --port 8000
"""

import concurrent.futures
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator

import data_fetcher
import predictor
import database

app = FastAPI(title="SportsBettr API", version="1.0.0")

# Allow the Vite dev server to call us
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialise DB on startup
@app.on_event("startup")
def startup_event():
    import os
    import ml_trainer

    database.init_db()

    etl_ran = database.run_etl()

    if etl_ran or not os.path.exists(ml_trainer.MODEL_PATH):
        print("Training XGBoost model…")
        ml_trainer.train_model()
        print("Model ready.")
    else:
        print("Model up to date, skipping training.")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

VALID_STAT_TYPES = {
    "Points", "Assists", "Rebounds", "PRA", "3PM", "PR", "PA", "RA",
    "Blocks", "Steals", "Blocks+Steals", "Turnovers",
    "Offensive Rebounds", "Defensive Rebounds", "Double Double", "3PA",
}


class ActualResultRequest(BaseModel):
    actual_result: str

    @field_validator("actual_result")
    @classmethod
    def validate_actual_result(cls, v: str) -> str:
        if v not in ("OVER", "UNDER"):
            raise ValueError("actual_result must be OVER or UNDER")
        return v


class PredictRequest(BaseModel):
    player_name: str
    opponent_team: str
    stat_line: float
    stat_type: str
    is_home: bool = False
    is_back_to_back: bool = False
    game_date: str | None = None

    @field_validator("stat_type")
    @classmethod
    def validate_stat_type(cls, v: str) -> str:
        if v not in VALID_STAT_TYPES:
            raise ValueError(f"stat_type must be one of {sorted(VALID_STAT_TYPES)}")
        return v

    @field_validator("stat_line")
    @classmethod
    def validate_stat_line(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("stat_line must be a positive number")
        return v


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/players/search")
def search_players(q: str = Query(default="", min_length=1)):
    """Return up to 10 player names matching the query string."""
    if not q.strip():
        return []
    results = data_fetcher.search_players(q)
    return results


@app.get("/players/{player_name}/team")
def get_player_team(player_name: str):
    """
    Return team context for the selected player:
      player_team         – { full, abbrev }
      next_opponent       – { full, abbrev } | null
      is_home             – bool
      is_back_to_back     – bool
    """
    ctx = data_fetcher.get_player_context(player_name)
    if ctx is None:
        raise HTTPException(
            status_code=404,
            detail=f"Player '{player_name}' not found or has no current team",
        )
    return {
        "player_name": player_name,
        "player_team": {
            "full": ctx["player_team_full"],
            "abbrev": ctx["player_team_abbrev"],
        },
        "next_opponent": (
            {
                "full": ctx["next_opponent_full"],
                "abbrev": ctx["next_opponent_abbrev"],
            }
            if ctx["next_opponent_full"]
            else None
        ),
        "is_home": ctx["is_home"],
        "is_back_to_back": ctx["is_back_to_back"],
        "next_game_date": ctx.get("next_game_date"),
    }


@app.get("/teams")
def get_teams():
    """Return a sorted list of all NBA team names."""
    return data_fetcher.get_team_names()


@app.get("/players/{player_name}/context")
def get_player_context_endpoint(player_name: str):
    """
    Return injury status and recent news for a player.
    Each external call is bounded to 5 seconds so a slow injury report
    doesn't block the UI.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        injury_fut = ex.submit(data_fetcher.get_player_injury_status, player_name)
        news_fut   = ex.submit(data_fetcher.get_player_news, player_name)
        try:
            injury_status = injury_fut.result(timeout=5)
        except Exception:
            injury_status = None
        try:
            news = news_fut.result(timeout=5)
        except Exception:
            news = ""
    return {"injury_status": injury_status, "news": news}


@app.post("/predict")
def predict_prop(req: PredictRequest):
    """
    Predict OVER or UNDER for a player prop line.
    Returns: { outcome, confidence, explanation }
    Logs every prediction to predictions.db.
    """
    result = predictor.predict(
        player_name=req.player_name,
        opponent_team=req.opponent_team,
        stat_line=req.stat_line,
        stat_type=req.stat_type,
        is_home=req.is_home,
        is_back_to_back=req.is_back_to_back,
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        opp_inj_fut = ex.submit(data_fetcher.get_opponent_injuries, req.opponent_team)
        try:
            opponent_injuries = opp_inj_fut.result(timeout=5)
        except Exception:
            opponent_injuries = []

    database.log_prediction(
        player_name=req.player_name,
        stat_type=req.stat_type,
        stat_line=req.stat_line,
        opponent_team=req.opponent_team,
        predicted_outcome=result["outcome"],
        confidence=result["confidence"],
        explanation=result["explanation"],
        game_date=req.game_date,
    )

    return {
        "outcome": result["outcome"],
        "confidence": round(result["confidence"] * 100, 1),
        "explanation": result["explanation"],
        "opponent_injuries": opponent_injuries,
    }


@app.get("/predictions/history")
def get_prediction_history():
    """Return all logged predictions ordered by timestamp descending."""
    conn = database.get_connection()
    rows = conn.execute(
        """SELECT id, timestamp, player_name, stat_type, stat_line, opponent_team,
                  predicted_outcome, confidence, explanation, actual_result, game_date
           FROM predictions
           ORDER BY timestamp DESC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.delete("/predictions/duplicates")
def delete_duplicate_predictions():
    """
    Remove duplicate predictions, keeping the earliest row (lowest id) for each
    unique combination of player_name, stat_type, stat_line, opponent_team, and
    predicted_outcome.
    """
    conn = database.get_connection()
    cursor = conn.execute(
        """
        DELETE FROM predictions
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM predictions
            GROUP BY player_name, stat_type, stat_line, opponent_team, predicted_outcome
        )
        """
    )
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return {"deleted": deleted}


@app.patch("/predictions/{pred_id}/result")
def update_prediction_result(pred_id: int, req: ActualResultRequest):
    """Manually set the actual result for a prediction."""
    conn = database.get_connection()
    conn.execute(
        "UPDATE predictions SET actual_result = ? WHERE id = ?",
        (req.actual_result, pred_id),
    )
    conn.commit()
    row = conn.execute(
        """SELECT id, timestamp, player_name, stat_type, stat_line, opponent_team,
                  predicted_outcome, confidence, explanation, actual_result, game_date
           FROM predictions WHERE id = ?""",
        (pred_id,),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail=f"Prediction {pred_id} not found")
    return dict(row)


@app.get("/slip")
def build_slip(size: int = Query(default=2, ge=1, le=5)):
    """
    Build a suggested parlay slip from tonight's games.
    Runs predictions for every player in the OddsAPI lines cache in parallel,
    applies diversity rules, and returns up to 3 picks with combined confidence.
    """
    SLIP_STATS = ["Points", "Rebounds", "Assists", "3PM"]
    STAT_TO_MARKET = {
        "Points":   "player_points",
        "Rebounds": "player_rebounds",
        "Assists":  "player_assists",
        "3PM":      "player_threes",
    }
    MIN_CONFIDENCE = 0.65

    lines = data_fetcher.get_todays_lines()
    if not lines:
        return {"picks": [], "parlay_confidence": 0.0}

    def _predict_player(player_name: str) -> list[dict]:
        player_data = lines.get(player_name, {})
        home_team = player_data.get("home_team", "")
        away_team = player_data.get("away_team", "")
        game_id   = player_data.get("game_id", "")

        ctx = data_fetcher.get_player_context(player_name)
        if ctx is None or not ctx.get("next_opponent_full"):
            return []

        opponent_team   = ctx["next_opponent_full"]
        is_home         = ctx["is_home"]
        is_back_to_back = ctx["is_back_to_back"]

        results = []
        for stat_type in SLIP_STATS:
            market_data = player_data.get(STAT_TO_MARKET[stat_type])
            if not market_data:
                continue
            line_value = market_data["value"]
            source     = market_data["source"]
            try:
                pred = predictor.predict(
                    player_name=player_name,
                    opponent_team=opponent_team,
                    stat_line=line_value,
                    stat_type=stat_type,
                    is_home=is_home,
                    is_back_to_back=is_back_to_back,
                )
            except Exception:
                continue
            if pred["confidence"] < MIN_CONFIDENCE:
                continue
            results.append({
                "player":     player_name,
                "stat_type":  stat_type,
                "line":       line_value,
                "outcome":    pred["outcome"],
                "confidence": pred["confidence"],
                "source":     source,
                "game":       f"{home_team} vs {away_team}",
                "_game_id":   game_id,
            })
        return results

    all_candidates: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(_predict_player, name): name for name in lines}
        for fut in concurrent.futures.as_completed(futures):
            try:
                all_candidates.extend(fut.result())
            except Exception:
                pass

    all_candidates.sort(key=lambda x: x["confidence"], reverse=True)

    picks: list[dict] = []
    already_picked: set = set()
    used_games:     set = set()
    used_stat_types: set = set()
    used_players:   set = set()

    def _fill(min_conf: float, block_same_game: bool, block_same_stat: bool) -> None:
        for i, c in enumerate(all_candidates):
            if len(picks) >= size:
                break
            if i in already_picked:
                continue
            if c["confidence"] < min_conf:
                continue
            if block_same_game and c["_game_id"] in used_games:
                continue
            if block_same_stat and c["stat_type"] in used_stat_types:
                continue
            if not block_same_game and c["player"] in used_players:
                continue
            picks.append(c)
            already_picked.add(i)
            used_games.add(c["_game_id"])
            used_stat_types.add(c["stat_type"])
            used_players.add(c["player"])

    _fill(0.65, block_same_game=True,  block_same_stat=True)   # pass 1
    if len(picks) < size:
        _fill(0.60, block_same_game=True,  block_same_stat=False)  # pass 2
    if len(picks) < size:
        _fill(0.55, block_same_game=False, block_same_stat=False)  # pass 3

    for pick in picks:
        pick.pop("_game_id", None)

    parlay_confidence = 1.0
    for pick in picks:
        parlay_confidence *= pick["confidence"]
    parlay_confidence = round(parlay_confidence, 4) if picks else 0.0

    return {"picks": picks, "parlay_confidence": parlay_confidence}


@app.get("/lines/{player_name}/{stat_type}")
def get_player_line(player_name: str, stat_type: str):
    """
    Return the sportsbook line for a player's stat type for today's game.
    { "line": 27.5, "source": "fanduel" } or { "line": null, "source": null }
    """
    line, source = data_fetcher.get_player_line(player_name, stat_type)
    return {"line": line, "source": source}


@app.get("/health")
def health():
    return {"status": "ok"}
