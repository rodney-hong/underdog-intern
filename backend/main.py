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

VALID_STAT_TYPES = {"Points", "Assists", "Rebounds", "PRA", "3PM", "PR", "PA", "RA"}


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
                  predicted_outcome, confidence, explanation, actual_result
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
                  predicted_outcome, confidence, explanation, actual_result
           FROM predictions WHERE id = ?""",
        (pred_id,),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail=f"Prediction {pred_id} not found")
    return dict(row)


@app.get("/health")
def health():
    return {"status": "ok"}
