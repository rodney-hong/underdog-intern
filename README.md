# SportsBettr

A full-stack web app that predicts NBA player prop outcomes (OVER / UNDER) using real game data from the `nba_api` Python package and ESPN's public injury feed. No paid APIs or LLMs are used.

## Stack

| Layer | Tech |
|---|---|
| Frontend | React 18 + Vite + Tailwind CSS |
| Backend | FastAPI + Python 3.11+ |
| Data | `nba_api`, ESPN public JSON endpoints |
| Prediction | Heuristic feature-weighted scoring (no training data required) |
| Storage | SQLite (`predictions.db`) |

---

## Project Structure

```
underdog-intern/
├── backend/
│   ├── main.py          # FastAPI app + all endpoints
│   ├── predictor.py     # Heuristic scoring engine
│   ├── data_fetcher.py  # nba_api + ESPN wrappers
│   ├── database.py      # SQLite logging
│   ├── predictions.db   # Auto-created on first run
│   └── requirements.txt
└── frontend/
    ├── src/
    │   ├── App.jsx      # Full single-page UI
    │   ├── main.jsx
    │   └── index.css
    ├── index.html
    ├── package.json
    ├── vite.config.js
    ├── tailwind.config.js
    └── postcss.config.js
```

---

## Setup & Running

### Prerequisites

- Python 3.11 or higher
- Node.js 18 or higher
- `pip` and `npm`

---

### 1. Backend

```bash
cd backend

# Create and activate a virtual environment (recommended)
python -m venv .venv
# macOS / Linux:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Start the server (runs on http://localhost:8000)
uvicorn main:app --reload --port 8000
```

The SQLite database (`predictions.db`) is created automatically on first startup.

> **Note:** `nba_api` makes live calls to the NBA stats website. The first request after startup may take a few seconds. The API rate-limits itself with small delays between calls to avoid being throttled.

---

### 2. Frontend

```bash
cd frontend

# Install dependencies
npm install

# Start the dev server (runs on http://localhost:5173)
npm run dev
```

Open [http://localhost:5173](http://localhost:5173) in your browser.

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/players/search?q={query}` | Returns up to 10 matching active player names |
| `GET` | `/players/{player_name}/team` | Returns the player's current team |
| `GET` | `/teams` | Returns all 30 NBA team names |
| `POST` | `/predict` | Returns `{ outcome, confidence, explanation }` |
| `GET` | `/health` | Health check |

### POST /predict — Request body

```json
{
  "player_name": "LeBron James",
  "opponent_team": "Golden State Warriors",
  "stat_line": 24.5,
  "stat_type": "Points"
}
```

Valid `stat_type` values: `Points`, `Assists`, `Rebounds`, `PRA`, `3PM`, `PR`, `PA`, `RA`

### POST /predict — Response

```json
{
  "outcome": "OVER",
  "confidence": 72.3,
  "explanation": "LeBron James is averaging 27.4 Points over the last 5 games..."
}
```

---

## How Predictions Work

The predictor computes six features from the player's last 20 game logs:

1. **5-game average vs. line** — normalised deviation, weight 2.5×
2. **10-game average vs. line** — normalised deviation, weight 1.5×
3. **Hit rate** — % of last 10 games the stat exceeded the line, weight 3.0×
4. **Trend direction** — 5-game avg minus 10-game avg (momentum), weight 1.0×
5. **Matchup history** — historical average vs. the specific opponent, weight up to 1.0×
6. **Context modifiers** — home advantage (+0.1), back-to-back (−0.3), injury flag (−0.4)

The raw score is passed through a scaled sigmoid to produce a confidence between **50–95%**. Every prediction is logged to `predictions.db` with an `actual_result` column (nullable) for future grading.

---

## Logging

All predictions are stored in `backend/predictions.db`:

```
id | timestamp | player_name | stat_type | stat_line | opponent_team
   | predicted_outcome | confidence | explanation | actual_result
```

`actual_result` is `NULL` by default and can be filled in later to grade predictions.
