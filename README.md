# SportsBettr

A full-stack web app that predicts NBA player prop outcomes (OVER / UNDER) with per-stat machine-learning models, trained on historical box scores and real sportsbook lines. It also logs live sportsbook lines and injury news, so the frozen 2026-27 models can be evaluated forward in "paper mode": predictions are recorded and graded, never bet.

## Stack

| Layer | Tech |
|---|---|
| Frontend | React 18 + Vite + Tailwind CSS |
| Backend | FastAPI (Python) |
| Models | scikit-learn logistic regression and XGBoost, one model per stat type |
| Box scores | Kaggle dataset [`eoinamoore/historical-nba-data-and-player-box-scores`](https://www.kaggle.com/datasets/eoinamoore/historical-nba-data-and-player-box-scores) via `kagglehub` |
| Sportsbook lines | [OddsAPI](https://the-odds-api.com/) (historical props for training, live lines for logging) |
| News / injuries | ESPN public endpoints, official NBA injury-report PDFs (`pdfplumber`) |
| Scheduling | APScheduler, in-process |
| Storage | SQLite (`backend/predictions.db`, WAL mode) |

---

## Project Structure

```
underdog-intern/
├── backend/
│   ├── main.py                 # FastAPI app, endpoints, startup sequence
│   ├── predictor.py            # Loads model_<stat>.pkl and scores the next game; heuristic fallback
│   ├── ml_trainer.py           # Training data build + model fitting (NBA frozen, WNBA)
│   ├── feature_defs.py         # Feature definitions shared by training and inference
│   ├── game_types.py           # Which game types count (allowlist), shared by training and inference
│   ├── real_lines.py           # Joins real book lines to box scores
│   ├── database.py             # SQLite schema + Kaggle ETL
│   ├── data_fetcher.py         # Reads player data from SQLite; ESPN injury feed
│   ├── historical_odds_etl.py  # One-time pull of historical player props from OddsAPI
│   ├── line_logger.py          # Scheduled live line logging (paper mode)
│   ├── paper_predictions.py    # Records frozen-model predictions against live Underdog lines
│   ├── news_ingest.py          # ESPN injuries + official NBA injury-report polling
│   ├── rosters.py              # Current rosters from ESPN
│   ├── experiments/            # Model selection, pre-registration, market-structure tests
│   └── tests/
└── frontend/
    └── src/
        ├── App.jsx             # Main single-page UI
        ├── History.jsx         # Prediction history / track record
        └── trackRecord.js
```

---

## Data

### Box scores (Kaggle)

`database.run_etl()` downloads `PlayerStatisticsExtended.csv`, `Players.csv` and the latest `LeagueSchedule<YY>_<YY>.csv` from the Kaggle dataset above and loads them into SQLite (`player_stats`, `players`, `schedule`). The frozen 2026-27 models were trained on dataset **v519** (`player_stats`: 840,793 rows).

Only competitive games count, per the allowlist in `game_types.py`: Regular Season, NBA Emirates Cup (2023 group stage / QF / SF), Playoffs and Play-in Tournament. Preseason, All-Star and the Emirates NBA Cup final are excluded, both from training rows and from every rolling feature. `is_playoff` is 1 for Playoffs only.

### Real sportsbook lines

`historical_odds_etl.py` (run manually, needs `ODDS_API_HISTORICAL_KEY`) pulls historical NBA player props from OddsAPI into `historical_player_props`. `real_lines.py` matches each prop to a single box-score game by normalized player name, a ±1 day window, and the team matchup. A date-only join would be ambiguous because the props carry UTC dates and the box scores ET dates.

---

## Models

`ml_trainer.py` trains **one binary classifier per stat type**: Points, Rebounds, Assists, 3PM, PRA, PR, PA, RA. The label is `hit = actual stat > line`.

- **Points, Rebounds, Assists, 3PM** are labeled against **real FanDuel/DraftKings lines** (`USE_REAL_LINES = True`). Rows with no matched real line are dropped, never mixed with simulated labels.
- **PRA, PR, PA, RA** have no real lines, so they use a **simulated line** (the player's career-to-date mean before the game, plus uniform noise of ±1.5).

### Features (14, computed only from games before the one being predicted)

`avg_pts_last5`, `avg_pts_last10`, `hit_rate_last10`, `trend`, `vs_opponent_avg`, `line_diff`, `consistency`, `games_played_last15`, `opp_def_rating_last5`, `is_playoff`, `minutes_trend`, `avg_minutes_last5`, `fatigue_score`, `home_away_split`

Each feature uses only data from before that game: rolling windows are shifted or `closed="left"`. Players with fewer than 10 prior counted games are dropped from training. A missing opponent defensive rating is filled with a mean computed only over training dates. `hit_rate_last10` and `vs_opponent_avg` are defined once in `feature_defs.py` and shared by training and `predictor.py`.

### Frozen 2026-27 models

The NBA models were frozen on 2026-10-18, before opening night. `train_model()` refuses to run without `force=True`, and startup never retrains them. Each model is fit on 100% of its rows:

| Model | Type | Training rows | Lines |
|---|---|---|---|
| `model_Points.pkl` | Logistic regression | 29,836 | real |
| `model_Rebounds.pkl` | Logistic regression | 26,970 | real |
| `model_Assists.pkl` | XGBoost | 17,557 | real |
| `model_3PM.pkl` | Logistic regression | 21,465 | real |
| `model_PRA.pkl` / `PR` / `PA` / `RA` | XGBoost | 752,661 each | simulated |

- Logistic regression: `StandardScaler` + `LogisticRegression(C=1.0, max_iter=2000)`
- XGBoost: `n_estimators=200, max_depth=4, learning_rate=0.05, random_state=42`
- Neither was tuned. SHA-256 hashes for every file are recorded in `experiments/preregistration.md`, and a second refit at the frozen commit produced byte-identical files for all 8 models.

WNBA: `train_wnba_models()` fits one XGBoost model per stat (Points, Rebounds, Assists, 3PM) on `wnba_player_stats`, with an 80/20 time-ordered split. The WNBA models are outside the freeze.

---

## Validation

Model type was chosen per stat by `experiments/model_selection.py`, with the rule fixed before running:

- **70/15/15 time-ordered split** on calendar-day boundaries, so no game night straddles train and evaluation. The model with the higher **validation** AUC wins (an exact tie goes to logistic regression), and the winner alone is scored **once** on test.
- **Bootstrap 95% CIs** on AUC (1,000 resamples), plus log loss and Brier score.
- **Monthly walk-forward** (2024-10 to 2026-05): train on everything before each month, predict the month. It's reported alongside the split and did not override the rule.

Results (`experiments/results_model_selection.json`):

| Stat | Val AUC XGB / LR | Winner | Test AUC [95% CI] | Walk-forward pooled XGB / LR |
|---|---|---|---|---|
| Points | 0.514 / 0.527 | LR | 0.522 [0.505, 0.539] | 0.505 / 0.511 |
| Rebounds | 0.527 / 0.531 | LR | 0.533 [0.515, 0.549] | 0.532 / 0.534 |
| Assists | 0.531 / 0.526 | XGBoost | 0.508 [0.487, 0.531] | 0.514 / 0.531 |
| 3PM | 0.535 / 0.568 | LR | 0.556 [0.536, 0.577] | 0.544 / 0.558 |
| PRA / PR / PA / RA | XGB ahead by ~0.004-0.006 | XGBoost | 0.732-0.747 | XGB ahead in 15-16 of 16 months |

**Read this table carefully.** Against **real sportsbook lines** (the first four rows), the models are only slightly better than chance. The combo-stat AUCs are measured against **simulated** lines and say nothing about performance against real lines.

`experiments/preregistration.md` also records a pre-registered test of same-game leg correlation (Test C). On 2025-26 FanDuel lines, 0 of 12 carried-forward pair types reached the 33.3% two-pick break-even; the best was 0.294 [0.282, 0.307].

---

## How Predictions Are Served

`POST /predict` computes the 14 features for the player's next game from the local database:

- **10 or more counted games:** the frozen model for that stat returns `predict_proba`, and confidence is the larger class probability.
- **1-9 games**, or a stat type without a model file: the heuristic scorer, a weighted feature score mapped through a scaled sigmoid to a confidence of 50-95%.
- **0 games:** a fixed OVER answer at 0.52 confidence.

Every prediction is logged to the `predictions` table.

---

## Line Logging (paper mode)

`line_logger.py` records live lines for forward evaluation. It **never feeds predictions, models or slips.**

- **Markets:** Points, Rebounds, Assists and 3PM, each with its main and `_alternate` market (8 markets). Underdog's lines arrive mostly in the alternate markets.
- **Books:** `underdog, fanduel, draftkings, prizepicks, novig, prophetx, kalshi, betmgm, espnbet, betrivers`, requested together in one OddsAPI call per event.
- **Schedule** (APScheduler, US Eastern time):
  - 06:00: a free events call schedules today's pre-tip pulls
  - 07:00: daily data sync (the same routine as at startup; see below)
  - 10:00: open pull
  - 30 minutes before tip-off: pre-tip pull
- **Storage:** every outcome, both sides, with price, under its real market key, in `line_snapshots`. Rows are keyed so they never overwrite or merge. `odds_pull_log` tracks credit usage against a monthly cap (`ODDS_MONTHLY_CREDIT_CAP`, default 500).

After each pre-tip pull, `paper_predictions.py` records one row per Underdog line per stat with that stat's frozen-model P(over), tagged with the model file's SHA-256 and whether it matches the pre-registered hash.

`news_ingest.py` polls ESPN injuries every 20 minutes and the official NBA injury-report PDF at minutes 2, 17, 32 and 47 past each hour, into `news_items`. Nothing there feeds predictions.

---

## Setup & Running

### Prerequisites

- **Python 3.14.4** exactly (the frozen models were built on it, with xgboost 3.2.0, scikit-learn 1.8.0, pandas 2.3.3, numpy 2.4.4; `backend/requirements-lock.txt` pins every package)
- Node.js and npm
- A Kaggle account configured for `kagglehub` (box-score ETL)
- An OddsAPI key for live lines; a separate historical-plan key only to rebuild `historical_player_props`

### Backend

```bash
cd backend
python -m venv .venv
# macOS / Linux:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate

pip install --no-deps -r requirements-lock.txt     # exact versions; never upgrade
python auth.py set-password                         # once: the admin login (hash + session key into .env)
uvicorn main:app --reload --port 8000
```

`SPORTSBETTR_MODE` in `.env` decides what the backend does on its own; only one machine may run in `prod` (it collects). `dev` (the default) runs nothing automatic: no sync, pulls, loggers, pollers or backups; the API still serves the app and scripts such as `backfill.py` run by hand. `dry` runs nothing either but logs every job `prod` would run, with its ET time (`[dry]` lines). Every API route needs the admin login except `POST /auth/login` and `/health`; `/docs` exists only with `API_DOCS_ENABLED=1`. `python daily_check.py` reports missed pulls and missing paper/challenger rows for a day; `deploy.sh` (server) refuses to restart within 45 minutes of a pre-tip pull or at 10:00-10:05 ET.

In `prod`, startup enables WAL, refreshes rosters, starts the line-logger and news schedulers, initializes the database, and checks that the frozen model files are present. As soon as startup completes, a data sync runs in the background (`line_logger.run_sync`, `[sync]` log lines): back up `predictions.db` (if today's backup doesn't exist), refresh rosters, check Kaggle for the newest Ready version, run the ETL if it is newer, then resolve app and paper predictions. If Kaggle has nothing newer, the version check repeats hourly while the server is up. The same sync runs daily at 07:00 ET and from the app's **Sync now** button (`POST /sync`, `prod` only and without catch-up pulls, so a request never spends credits; `GET /sync` returns the last result). A self-check every 15 minutes re-adds any missing pre-tip job, and with the `SPACES_*` settings a nightly off-server backup runs at 04:30 ET (`offsite_backup.py`).

ETL guards: the ETL never runs at the same time as a pre-tip job (it takes the pre-tip lock, waiting for a running job, and pre-tip jobs that come due queue behind it), and never starts within 10 minutes of a scheduled pull (open or pre-tip, from the scheduler and the `schedule` table); it is moved to the end of that window instead. That is enough because the load goes into staging tables and is swapped in with one short transaction, so readers only ever see the old tables or the new ones.

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173 and log in with the admin password. The app calls `/api/...` (`VITE_API_BASE`, default `/api`) in dev and in production: the Vite dev server (and `vite preview`) forwards `/api` to the backend at http://localhost:8000 (`API_PROXY_TARGET` overrides it), as Caddy does on the server, so the browser stays on one origin and the backend needs no CORS. Build with `VITE_API_BASE=/api npm run build`; in Git Bash on Windows add `MSYS_NO_PATHCONV=1`, or it rewrites `/api` into a Windows path (the app then falls back to `/api` anyway).

### Environment variables (`backend/.env`)

| Variable | Purpose |
|---|---|
| `ODDS_API_KEY` | Live lines (line logger, `/lines`, `/slip`) |
| `ODDS_API_HISTORICAL_KEY` | `historical_odds_etl.py` only |
| `ODDS_MONTHLY_CREDIT_CAP` | OddsAPI credit cap per month (default 500) |
| `LINE_LOGGER_ENABLED`, `NEWS_INGEST_ENABLED`, `PAPER_PREDICTIONS_ENABLED` | Set to anything other than `1` to disable (all default to `1`) |
| `LINE_LOG_SCHEDULE_HOUR_ET`, `LINE_LOG_OPEN_HOUR_ET`, `LINE_LOG_PRETIP_MINUTES`, `LINE_LOG_OPEN_PULL_ENABLED` | Line-logger schedule (defaults 6, 10, 30, 1) |
| `NEWS_POLL_MINUTES`, `NBA_REPORT_CRON_MINUTES` | News polling (defaults 20 and `2,17,32,47`) |
| `BALLDONTLIE_WNBA_KEY` | Optional, WNBA player lookup |
| `SPORTSBETTR_MODE` | `prod` / `dry` / `dev` (default `dev`); only the collecting machine runs `prod` |
| `WNBA_ENABLED` | `1` turns on the WNBA routes, startup data load and training (default `0`) |
| `ADMIN_PASSWORD_HASH`, `SESSION_SECRET` | Admin login; set with `python auth.py set-password` |
| `ALLOWED_HOSTS`, `API_DOCS_ENABLED` | Accepted Host headers (default `bettr.rodneyhong.com,localhost,127.0.0.1`); `1` serves `/docs` |
| `KAGGLEHUB_CACHE` | Kaggle download cache (kagglehub default `~/.cache/kagglehub`); older versions are pruned after each ETL |
| `SPACES_KEY`, `SPACES_SECRET`, `SPACES_BUCKET`, `SPACES_ENDPOINT`, `SPACES_REGION` | Off-server backup bucket (DigitalOcean Spaces) |
| `SCREENSHOT_EXTRACTOR` | `ollama` (local, default), `ollama_cloud` (falls back to `gemini`), `gemini` or `claude` |
| `OLLAMA_API_KEY`, `SCREENSHOT_OLLAMA_CLOUD_MODEL`, `SCREENSHOT_OLLAMA_CLOUD_PARALLEL` | Ollama Cloud (defaults `gemma4:31b`, 1) |
| `GEMINI_API_KEY`, `SCREENSHOT_GEMINI_MODEL`, `SCREENSHOT_GEMINI_PARALLEL` | Gemini API fallback (defaults `gemma-4-31b-it`, 2) |

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/players/search?q=` | Up to 10 matching player names |
| `GET` | `/players/{player_name}/team` | Team context for the player |
| `GET` | `/players/{player_name}/context` | Injury status and recent news |
| `GET` | `/teams` | All NBA team names |
| `POST` | `/predict` | OVER / UNDER with confidence and explanation |
| `GET` | `/players/{player_name}/track-record` | The app's own record on this player and stat |
| `GET` | `/predictions/history` | All logged predictions, newest first |
| `DELETE` | `/predictions/duplicates` | Remove duplicate predictions |
| `GET` | `/slip` | Suggested parlay slip from tonight's games |
| `GET` | `/lines/{player_name}/{stat_type}` | Today's sportsbook line for a player and stat |
| `GET` | `/wnba/...` | WNBA equivalents: players/search, teams, players/{name}/team, players/{name}/context, lines/{name}/{stat} |
| `GET` | `/health` | Health check |

Valid `stat_type` values: `Points`, `Rebounds`, `Assists`, `3PM`, `PRA`, `PR`, `PA`, `RA` (model-backed), plus `Blocks`, `Steals`, `Blocks+Steals`, `Turnovers`, `Offensive Rebounds`, `Defensive Rebounds`, `Double Double`, `3PA` (heuristic scorer only).

---

## Disclaimer

This is a research project. Against real sportsbook lines, the models are close to chance (see Validation), and the logging runs in paper mode only. Nothing here is betting advice.
