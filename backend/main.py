"""
main.py
FastAPI application for NBA Player Prop Predictor.
Run with: uvicorn main:app --reload --port 8000
"""

import concurrent.futures
import datetime as dt
import os
import sys
import threading
import time

# Never let a print crash startup: when output is redirected (service, log file) Windows
# falls back to cp1252, and a character like "→" in an ETL message raised
# UnicodeEncodeError mid-ETL. Unencodable characters now print as "?".
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, field_validator
from starlette.concurrency import run_in_threadpool

import auth
import data_fetcher
import predictor
import database
import line_logger
import news_ingest
import runmode

# Every route needs the admin session (auth.require_session) except auth.EXEMPT_PATHS.
# /docs, /redoc and /openapi.json exist only with API_DOCS_ENABLED=1 (never on the server).
_DOCS = os.getenv("API_DOCS_ENABLED", "0") == "1"
app = FastAPI(title="SportsBettr API", version="1.0.0",
              dependencies=[Depends(auth.require_session)],
              docs_url="/docs" if _DOCS else None, redoc_url="/redoc" if _DOCS else None,
              openapi_url="/openapi.json" if _DOCS else None)

# Behind Caddy the Host header is the public name; anything else is refused (400).
ALLOWED_HOSTS = [h.strip() for h in os.getenv(
    "ALLOWED_HOSTS", "bettr.rodneyhong.com,localhost,127.0.0.1").split(",") if h.strip()]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

# The Vite dev server (another origin) calls us only in dev; in production the built app
# is served from the same origin by Caddy, so no CORS at all.
ALLOWED_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]
if runmode.mode() == "dev":
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

def _startup_step(name: str, fn, *args, **kwargs):
    """
    Run one startup step; on failure log the full traceback and carry on.
    Data loading or training problems must never stop the line logger or news
    poller, which run in their own scheduler threads. Returns (ok, result).
    """
    import traceback
    try:
        return True, fn(*args, **kwargs)
    except Exception:
        print(f"[startup] {name} FAILED — continuing startup; the server stays up.")
        traceback.print_exc()
        return False, None


def _check_challenger() -> str:
    """Load the frozen challenger now (hash + fingerprint), so a missing or changed file is
    loud at startup instead of a quiet 'challenger predictions FAILED' at the first pre-tip."""
    from challenger import live
    try:
        live.load_frozen()
    except live.FrozenModelError as exc:
        print("!" * 78)
        print(f"[startup] CHALLENGER FILE FAILED ITS CHECK: {exc}")
        print("[startup] /predict returns 503 for Points/Rebounds/Assists/3PM and pre-tip pulls "
              "log NO challenger rows until the pre-registered file is back.")
        print("!" * 78)
        raise
    print(f"[startup] challenger file OK (sha256 {live.FROZEN['sha256'][:12]}..., fingerprint matches)")
    return "ok"


@app.on_event("startup")
def startup_event():
    import importlib
    import proc_lock

    mode = runmode.mode()          # an unknown SPORTSBETTR_MODE stops startup here
    print(runmode.banner(), flush=True)
    if not auth.configured():
        print("!" * 78)
        print("[startup] LOGIN NOT SET UP: every route but /health refuses until "
              "`python auth.py set-password` has been run and the backend restarted.")
        print("!" * 78)

    # Single instance, before anything else: uvicorn runs this hook BEFORE binding the
    # port, so a second `uvicorn main:app` would otherwise start a second scheduler
    # (duplicate pulls, overlapping ETLs) until its bind fails. Held for the process's
    # life; the OS drops it when the process exits, even on a crash.
    if not proc_lock.INSTANCE_LOCK.acquire(timeout=0, who="startup"):
        msg = (f"another backend instance holds {proc_lock.lock_path('instance')}: this one "
               f"starts no scheduler, ETL or poller and exits")
        print("!" * 78, flush=True)
        print(f"[startup] REFUSED: {msg}", flush=True)
        print("!" * 78, flush=True)
        raise RuntimeError(msg)

    # WAL first, before any scheduler thread opens a connection: readers no longer
    # block on writers (ETL, scheduler, news poller, API). Persistent once set, so in
    # dev and dry this is a no-op on a database copied from the collecting machine.
    _startup_step("database.enable_wal", database.enable_wal)
    challenger_ok = _startup_step("challenger.check", _check_challenger)[0]

    if mode != "prod":
        # dev / dry: no network, no writes, nothing automatic. Tables are created if missing
        # (a no-op on a copied database); is_current / limited-data flags are not recomputed.
        _startup_step("database.init_db", database.init_db, maintain_flags=False)
        if mode == "dry":
            _startup_step("dryrun.start", importlib.import_module("dryrun").start)
        else:
            print("[startup] dev: no sync, catch-up pulls, schedules, loggers, pollers or backups. "
                  "backfill.py and the other scripts still run by hand.")
        return

    # Current rosters (ESPN, free) before anything schedules a paper prediction:
    # player teams come from them, falling back to box scores.
    _startup_step("rosters.refresh", importlib.import_module("rosters").refresh_rosters)
    # App name matching: ESPN id -> personId map and the name-collision table, from the
    # rosters just refreshed (player_match; app display only, not the models).
    _startup_step("player_match.rebuild", importlib.import_module("player_match").rebuild_now)

    # Collection first, each guarded separately so one can't block the other.
    # Paper-mode line logging (Points, UD/FD/DK); it never touches predictions.
    _startup_step("line_logger.start_scheduler", line_logger.start_scheduler)
    # Raw injury news (ESPN every 20 min, official report every 15); no LLM, feeds nothing.
    _startup_step("news_ingest.start_scheduler", news_ingest.start_scheduler)

    ml_ok, ml_trainer = _startup_step("import ml_trainer", importlib.import_module, "ml_trainer")

    _startup_step("database.init_db", database.init_db)

    # NBA data (backup -> Kaggle check -> ETL under the guards -> resolve app and paper
    # predictions) syncs in the background as soon as startup completes (_startup_complete).
    # The 2026-27 models are frozen either way: startup never retrains them.
    if not ml_ok:
        print("[startup] ml_trainer import failed: skipping the frozen-model check and WNBA training.")
        _startup_complete(challenger_ok)
        return
    missing = [st for st in ml_trainer.FROZEN_MODEL_TYPES
               if not os.path.exists(ml_trainer.model_path(st))]
    if missing:
        print(f"[startup] WARNING: frozen NBA model files missing for {missing}; those stat "
              f"types fall back to the heuristic scorer. Not retraining (models are frozen).")
    else:
        print("NBA models frozen for 2026-27; not retraining.")

    if not runmode.wnba_enabled():
        print("[startup] WNBA off (WNBA_ENABLED=0): no WNBA data load, no WNBA training.")
        _startup_complete(challenger_ok)
        return
    wnba_ok, wnba_etl_ran = _startup_step("database.run_wnba_etl", database.run_wnba_etl)
    if wnba_ok and (wnba_etl_ran or not os.path.exists(ml_trainer.WNBA_MODEL_PATH)):
        print("Training WNBA XGBoost models…")
        if _startup_step("ml_trainer.train_wnba_models", ml_trainer.train_wnba_models)[0]:
            print("WNBA models ready.")
    elif not wnba_ok:
        print("Skipping WNBA training: WNBA ETL failed; existing models kept.")
    else:
        print("WNBA models up to date, skipping training.")
    _startup_complete(challenger_ok)


def _startup_complete(challenger_ok: bool = True):
    """Startup is done (prod): record it in startup_log, then sync the data now, in the
    background (line_logger.run_sync)."""
    _startup_step("database.log_startup", database.log_startup, mode=runmode.mode(),
                  wnba_enabled=runmode.wnba_enabled(), challenger_ok=challenger_ok)
    _startup_step("line_logger.start_sync_in_background", line_logger.start_sync_in_background)
    print("[startup] complete; data sync running in the background ([sync] lines)")


@app.on_event("shutdown")
def shutdown_event():
    import proc_lock
    import dryrun
    line_logger.stop_scheduler()
    news_ingest.stop_scheduler()
    dryrun.stop()
    if proc_lock.INSTANCE_LOCK.held:
        proc_lock.INSTANCE_LOCK.release()


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
    league: str = "NBA"
    person_id: int | None = None      # a player chosen by personId (screenshot table); else resolved by name

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
# Login (auth.py). /auth/login and /health are the only routes without a session.
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    password: str


@app.post("/auth/login")
def login(req: LoginRequest, request: Request, response: Response):
    """Admin password -> 30-day session cookie. 5 failed tries per 15 min per IP, then 429."""
    ip = auth.client_ip(request)
    if not auth.configured():
        raise HTTPException(status_code=503, detail="Login is not set up: run `python auth.py set-password`")
    if auth.locked_out(ip):
        raise HTTPException(status_code=429, detail="Too many failed logins; try again in 15 minutes")
    if not auth.verify_password(req.password):
        auth.record_failure(ip)
        print(f"[auth] failed login from {ip}")
        raise HTTPException(status_code=401, detail="Wrong password")
    auth.set_session_cookie(response, auth.new_token())
    return {"ok": True}


@app.post("/auth/logout")
def logout(response: Response):
    auth.clear_session_cookie(response)
    return {"ok": True}


@app.get("/auth/me")
def whoami():
    """200 with a valid session (the app checks this on load), else 401 like every route."""
    return {"ok": True}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/players/search")
def search_players(q: str = Query(default="", min_length=1)):
    """Up to 10 players matching the query: [{"name", "person_id", "team"}]."""
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


def _app_tip(player_team: str, opponent_team: str, game_date: str | None) -> str:
    """Feature cutoff for an app prediction (ET): the scheduled tip of the game between the
    two teams on game_date (else the start of game_date); with no game_date, their next
    scheduled game (else now)."""
    from zoneinfo import ZoneInfo
    now = dt.datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
    start = game_date or now.strftime("%Y-%m-%d %H:%M:%S")
    tip = data_fetcher.scheduled_tip(player_team, opponent_team, start)
    if tip is not None and (game_date is None or tip.strftime("%Y-%m-%d") == game_date):
        return tip.strftime("%Y-%m-%d %H:%M:%S")
    return f"{game_date} 00:00:00" if game_date else now.strftime("%Y-%m-%d %H:%M:%S")


def _challenger_prediction(req: "PredictRequest", person_id: int) -> dict:
    """The challenger's pick and P(over) at the entered line (challenger/live.py; frozen file,
    hash and fingerprint checked). 422 'Not enough NBA games' with fewer than 5 earlier games."""
    from challenger import live
    ctx = data_fetcher.get_player_context(req.player_name)
    if ctx is None:
        raise HTTPException(status_code=404, detail=f"Player '{req.player_name}' has no current team")
    team = ctx["player_team_full"]
    conn = database.get_connection()
    try:
        r = live.predict(conn, person_id, team, req.opponent_team, req.is_home,
                         _app_tip(team, req.opponent_team, req.game_date), req.stat_type, [req.stat_line])
    except live.FrozenModelError as exc:
        print(f"[predict] challenger refused: {exc}")
        raise HTTPException(status_code=503, detail="Challenger model file failed its integrity check")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    finally:
        conn.close()
    if r["status"] == "insufficient_history":
        raise HTTPException(status_code=422, detail="Not enough NBA games")
    p = r["p_over"][float(req.stat_line)]
    return {"outcome": "OVER" if p > 0.5 else "UNDER", "confidence": round(max(p, 1 - p), 4),
            "explanation": live.explanation(req.player_name, req.stat_type, req.stat_line, r, req.opponent_team),
            "p_over": p, "projected_mean": r["mu"]}


@app.post("/predict")
def predict_prop(req: PredictRequest):
    """
    Predict OVER or UNDER for a player prop line.
    Returns: { outcome, confidence, explanation, model, ... }
    Logs every prediction to predictions.db with the model that made it. NBA Points,
    Rebounds, Assists and 3PM use the challenger (from Stage 3, pre-registered entry
    a760c8c); other stats and WNBA keep the frozen/heuristic path. NBA: a player with no
    counted regular-season game, or (challenger stats) fewer than 5 earlier games, gets no
    prediction (422 "Not enough NBA games") and no row.
    """
    from challenger.live import STATS as CHALLENGER_STATS
    if req.league == "WNBA":
        _wnba_on()
    counts = None
    if req.league != "WNBA":
        conn = database.get_connection()
        try:
            counts = database.player_data_counts(conn, req.player_name, req.game_date, req.opponent_team,
                                                 person_id=req.person_id)
        finally:
            conn.close()
        if counts["rs_total"] == 0:
            raise HTTPException(status_code=422, detail="Not enough NBA games")
    if req.league != "WNBA" and req.stat_type in CHALLENGER_STATS:
        if counts["person_id"] is None:     # several players share the name and none can be picked
            raise HTTPException(status_code=422, detail="Several players match this name; pick one")
        result, model = _challenger_prediction(req, counts["person_id"]), "challenger"
    else:
        _predict_fn = predictor.predict_wnba if req.league == "WNBA" else predictor.predict
        result = _predict_fn(
            player_name=req.player_name,
            opponent_team=req.opponent_team,
            stat_line=req.stat_line,
            stat_type=req.stat_type,
            is_home=req.is_home,
            is_back_to_back=req.is_back_to_back,
        )
        model = "frozen"

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        opp_inj_fut = ex.submit(data_fetcher.get_opponent_injuries, req.opponent_team)
        try:
            opponent_injuries = opp_inj_fut.result(timeout=5)
        except Exception:
            opponent_injuries = []

    prediction_id = database.log_prediction(
        player_name=req.player_name,
        stat_type=req.stat_type,
        stat_line=req.stat_line,
        opponent_team=req.opponent_team,
        predicted_outcome=result["outcome"],
        confidence=result["confidence"],
        explanation=result["explanation"],
        game_date=req.game_date,
        league=req.league,
        model=model,
    )

    return {
        "outcome": result["outcome"],
        "confidence": round(result["confidence"] * 100, 1),
        "explanation": result["explanation"],
        "model": model,
        "p_over": result.get("p_over"),
        "projected_mean": result.get("projected_mean"),
        "opponent_injuries": opponent_injuries,
        "prediction_id": prediction_id,   # lets the UI ask for the track record before this pick
    }


@app.get("/players/{player_name}/track-record")
def get_player_track_record(
    player_name: str,
    stat: str = Query(default="Points"),
    league: str = Query(default="NBA"),
    before: str | None = Query(default=None, description="prediction id or ISO timestamp"),
    model: str = Query(default="all", description="challenger | frozen | challenger_retro | all"),
):
    """
    The app's own record on this player and stat, from the predictions table only
    (never paper_predictions), over the rows History's stats cards count (current,
    not limited-data, resolved) for `model` (all = challenger + frozen). Display-only:
    it does not feed the model. With `before`, only picks whose game was on an earlier
    ET date than that prediction.
    """
    if model not in database.TRACK_RECORD_MODELS:
        raise HTTPException(status_code=422, detail=f"model must be one of {sorted(database.TRACK_RECORD_MODELS)}")
    try:
        return database.player_track_record(player_name, stat, league, before, model)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'"))
    except ValueError:
        raise HTTPException(status_code=422, detail="before must be a prediction id or an ISO timestamp")


@app.get("/predictions/history")
def get_prediction_history():
    """Return all logged predictions ordered by timestamp descending."""
    conn = database.get_connection()
    rows = conn.execute(
        """SELECT id, timestamp, player_name, stat_type, stat_line, opponent_team,
                  predicted_outcome, confidence, explanation, actual_result, game_date, league,
                  is_limited_data, COALESCE(model, 'frozen') AS model,
                  COALESCE(is_current, 1) AS is_current
           FROM predictions
           ORDER BY timestamp DESC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.delete("/predictions/duplicates")
def delete_duplicate_predictions():
    """
    Remove exact duplicate predictions, keeping the newest row (timestamp, then id) for each
    combination of player_name, stat_type, stat_line, opponent_team, game_date and model
    (a challenger row never removes a frozen one). is_current is recomputed after.
    """
    conn = database.get_connection()
    cursor = conn.execute(
        """
        DELETE FROM predictions
        WHERE id NOT IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY player_name, stat_type, stat_line, opponent_team,
                                        COALESCE(game_date, ''), COALESCE(model, 'frozen')
                           ORDER BY timestamp DESC, id DESC
                       ) AS rn
                FROM predictions
            )
            WHERE rn = 1
        )
        """
    )
    deleted = cursor.rowcount
    database.refresh_current(conn)
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
                  predicted_outcome, confidence, explanation, actual_result, game_date, league,
                  is_limited_data, COALESCE(model, 'frozen') AS model,
                  COALESCE(is_current, 1) AS is_current
           FROM predictions WHERE id = ?""",
        (pred_id,),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail=f"Prediction {pred_id} not found")
    return dict(row)


# /slip on the 2 vCPU / 2 GB server: one slip at a time, built on one worker, and none while
# a pre-tip pull is waiting or running (collection comes first). A refused request returns at
# once; nothing queues.
_SLIP_LOCK = threading.Lock()
_SECONDARY_LOCK = threading.Lock()   # the background secondary-stat logging, one run at a time
SLIP_BUSY_DETAIL = "A slip is already being built; wait for it to finish and try again."
SLIP_PRETIP_DETAIL = "A pre-tip pull is in progress; try again in a few minutes."


@app.get("/slip")
def build_slip(size: int = Query(default=2, ge=1, le=5)):
    """
    Build a suggested parlay slip from tonight's games: 503 while a pre-tip pull is waiting
    or running, 429 while another slip is being built, else the slip from _build_slip.
    """
    if line_logger._running_pretips:
        raise HTTPException(status_code=503, detail=SLIP_PRETIP_DETAIL, headers={"Retry-After": "300"})
    if not _SLIP_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=429, detail=SLIP_BUSY_DETAIL, headers={"Retry-After": "30"})
    try:
        result, lines = _build_slip(size)
    finally:
        _SLIP_LOCK.release()
    if lines:
        _start_secondary_logging(lines)
    return result


def _build_slip(size: int) -> tuple[dict, dict]:
    """
    (slip, lines): predictions for every player in today's lines, one player after another,
    diversity rules applied, up to `size` picks with combined confidence.
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
        return {"picks": [], "parlay_confidence": 0.0}, lines

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
    for name in lines:
        try:
            all_candidates.extend(_predict_player(name))
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
    return {"picks": picks, "parlay_confidence": parlay_confidence}, lines


def _start_secondary_logging(lines: dict) -> None:
    """Log predictions for the secondary stats in today's lines on a background thread; skipped
    while an earlier run is still going, and paused while a pre-tip pull waits or runs."""
    _SECONDARY_STAT_TO_MARKET = {
        "Steals":             "player_steals",
        "Blocks":             "player_blocks",
        "Turnovers":          "player_turnovers",
        "Offensive Rebounds": "player_offensive_rebounds",
        "Defensive Rebounds": "player_defensive_rebounds",
        "3PA":                "player_three_point_attempts",
        "Blocks+Steals":      "player_blocks_steals",
        "Double Double":      "player_double_double",
    }

    def _log_secondary_stats() -> None:
        count = 0
        for player_name, player_data in lines.items():
            while line_logger._running_pretips:
                time.sleep(5)
            ctx = data_fetcher.get_player_context(player_name)
            if ctx is None or not ctx.get("next_opponent_full"):
                continue
            opponent_team   = ctx["next_opponent_full"]
            is_home         = ctx["is_home"]
            is_back_to_back = ctx["is_back_to_back"]
            game_date       = ctx["next_game_date"]
            for stat_type, market_key in _SECONDARY_STAT_TO_MARKET.items():
                market_data = player_data.get(market_key)
                if not market_data:
                    continue
                line_value = market_data["value"]
                try:
                    pred = predictor.predict(
                        player_name=player_name,
                        opponent_team=opponent_team,
                        stat_line=line_value,
                        stat_type=stat_type,
                        is_home=is_home,
                        is_back_to_back=is_back_to_back,
                    )
                    database.log_prediction(
                        player_name=player_name,
                        stat_type=stat_type,
                        stat_line=line_value,
                        opponent_team=opponent_team,
                        predicted_outcome=pred["outcome"],
                        confidence=pred["confidence"],
                        explanation=pred.get("explanation", ""),
                        game_date=game_date,
                        league="NBA",
                    )
                    count += 1
                except Exception:
                    pass
        print(f"[background] logged {count} secondary stat predictions")

    def _run() -> None:
        try:
            _log_secondary_stats()
        finally:
            _SECONDARY_LOCK.release()

    if not _SECONDARY_LOCK.acquire(blocking=False):
        print("[background] secondary stat logging still running; not started again")
        return
    threading.Thread(target=_run, daemon=True).start()


@app.get("/lines/{player_name}/{stat_type}")
def get_player_line(player_name: str, stat_type: str):
    """
    Return the sportsbook line for a player's stat type for today's game.
    { "line": 27.5, "source": "underdog", "reason": null }, or with no line
    { "line": null, "source": null, "reason": "no lines logged yet for this game (...)" }
    ("source": "ambiguous" for a name collision, reason null).
    """
    line, source = data_fetcher.get_player_line(player_name, stat_type)
    reason = None
    if line is None and source != "ambiguous":
        try:
            reason = data_fetcher.missing_line_reason(player_name, stat_type)
        except Exception as exc:   # an explanation must never break the line lookup
            reason = f"line not available ({exc!r}); enter manually"
    return {"line": line, "source": source, "reason": reason}


# ---------------------------------------------------------------------------
# WNBA endpoints: 404 unless WNBA_ENABLED=1 (/wnba/lines spends OddsAPI credits outside
# the monthly cap, so it must never be reachable on the server by default)
# ---------------------------------------------------------------------------

def _wnba_on() -> None:
    if not runmode.wnba_enabled():
        raise HTTPException(status_code=404, detail="WNBA is turned off (WNBA_ENABLED=0)")


@app.get("/wnba/players/search", dependencies=[Depends(_wnba_on)])
def search_wnba_players(q: str = Query(default="", min_length=1)):
    if not q.strip():
        return []
    return data_fetcher.search_wnba_players(q)


@app.get("/wnba/teams", dependencies=[Depends(_wnba_on)])
def get_wnba_teams():
    return data_fetcher.get_wnba_team_names()


@app.get("/wnba/players/{player_name}/team", dependencies=[Depends(_wnba_on)])
def get_wnba_player_team(player_name: str):
    ctx = data_fetcher.get_wnba_player_context(player_name)
    if ctx is None:
        raise HTTPException(
            status_code=404,
            detail=f"WNBA player '{player_name}' not found or has no current team",
        )
    return {
        "player_name": player_name,
        "player_team": {
            "full":   ctx["player_team_full"],
            "abbrev": ctx["player_team_abbrev"],
        },
        "next_opponent": (
            {
                "full":   ctx["next_opponent_full"],
                "abbrev": ctx["next_opponent_abbrev"],
            }
            if ctx["next_opponent_full"]
            else None
        ),
        "is_home":        ctx["is_home"],
        "is_back_to_back": ctx["is_back_to_back"],
        "next_game_date":  ctx.get("next_game_date"),
    }


@app.get("/wnba/players/{player_name}/context", dependencies=[Depends(_wnba_on)])
def get_wnba_player_context_endpoint(player_name: str):
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        injury_fut = ex.submit(data_fetcher.get_wnba_injury_status, player_name)
        try:
            injury_status = injury_fut.result(timeout=5)
        except Exception:
            injury_status = None
    return {"injury_status": injury_status, "news": ""}


@app.get("/wnba/lines/{player_name}/{stat_type}", dependencies=[Depends(_wnba_on)])
def get_wnba_player_line(player_name: str, stat_type: str):
    line, source = data_fetcher.get_wnba_player_line(player_name, stat_type)
    return {"line": line, "source": source}


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Data sync (line_logger.run_sync). Behind the login like every route; prod only (in dev
# and dry the machine doesn't collect, ground rule 1). A request never spends OddsAPI
# credits: Sync now skips the catch-up pulls (startup and the 07:00 sync still run them).
# ---------------------------------------------------------------------------

@app.post("/sync")
def sync_now():
    """
    Sync now: the same guarded routine as the startup and 07:00 syncs (backup ->
    rosters -> Kaggle check -> ETL if newer -> resolve), without catch-up pulls. Runs to
    completion (an ETL takes minutes); returns {"status": "busy"} at once if another sync
    or ETL is running. 409 unless SPORTSBETTR_MODE=prod.
    """
    if not runmode.is_prod():
        raise HTTPException(status_code=409, detail=f"Sync runs only on the collecting machine "
                                                    f"(SPORTSBETTR_MODE=prod; this one is {runmode.mode()})")
    return line_logger.run_sync(line_logger._scheduler, trigger="sync now", lock_wait_minutes=0,
                                catchup=False)


@app.post("/screenshot/extract")
async def screenshot_extract(file: UploadFile = File(...)):
    """
    Screenshot of player cards -> rows for the app's confirmation table. The extractor
    (screenshot_import.SCREENSHOT_EXTRACTOR) only transcribes the cards; nothing is
    predicted or logged here: the app calls /predict for each row the user confirms.
    The upload is read into memory and closed; the image is never written to disk.
    """
    import json
    import screenshot_import as si
    try:
        data = await file.read(si.MAX_IMAGE_BYTES + 1)
    finally:
        await file.close()
    try:
        name, rows = await run_in_threadpool(si.extract_rows, data)
    except si.BadImage as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except si.ExtractorUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=502, detail=f"extractor returned unreadable output ({exc!r})")
    finally:
        del data
    return {"extractor": name, "rows": await run_in_threadpool(si.confirmation_rows, rows)}


class AliasRequest(BaseModel):
    screenshot_name: str          # the card's name, as extracted
    person_id: int                # the player picked in the row's search


@app.post("/screenshot/alias")
def learn_alias(req: AliasRequest):
    """An unmatched screenshot row resolved by picking a player: remember card name -> personId
    (player_aliases, source 'user') unless the name collides or is a roster name."""
    import player_aliases
    conn = database.get_connection()
    try:
        player_aliases.init_table(conn)
        return player_aliases.learn(conn, req.screenshot_name, req.person_id)
    finally:
        conn.close()


@app.delete("/screenshot/alias")
def delete_alias(name: str = Query(..., min_length=1), person_id: int | None = Query(default=None)):
    """Remove a wrong alias (all of the name's aliases, or only the one for person_id)."""
    import player_aliases
    conn = database.get_connection()
    try:
        player_aliases.init_table(conn)
        return {"deleted": player_aliases.delete(conn, name, person_id)}
    finally:
        conn.close()


@app.get("/sync")
def last_sync():
    """The latest finished sync's result in this backend process (null before the first)."""
    return {"last": line_logger.last_sync()}
