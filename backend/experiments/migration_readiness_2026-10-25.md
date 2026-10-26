# Migration readiness audit, 2026-10-25

Read-only audit of the "SportsBettr Cloud Migration Plan" (2026-10-21) against the
code at HEAD `431af4b` and the "SportsBettr Handoff — October 25, 2026".
Nothing was changed. Measurements ran on backup-API temp copies in the scratchpad with
sockets blocked. No OddsAPI, Kaggle, ESPN, NBA, Anthropic or Ollama calls were made, and
no outcome columns were selected (the copies used for row-size measurements leave them out).

## 1. Verdict

**Blocked on decisions, then ready after code changes.** The backend is still portable:
the locks already use `fcntl` off Windows, every live schedule runs in ET, and no code
depends on Windows paths or on UTF-16 logs. But the plan predates four things that now
decide the migration:

1. **Screenshot import needs a local GPU.** On the server it cannot work as built. Behind
   Caddy, `_require_local` either refuses every remote browser or, if proxy headers are
   misconfigured, lets anyone through.
2. **The challenger roughly triples peak memory.** A pre-tip run that includes a player
   with fewer than 11 prior games peaks at about 1.4 GB working set (2.5 GB committed), so
   the planned 2 GB server is no longer safe.
3. **The handoff's "Kaggle cache is pruned" is wrong.** The cache holds 28 GB across 21
   versions, and no pruning code exists.
4. **There is still no run mode.** Every start runs catch-up pulls (which spend credits),
   an ETL and all resolvers, even with `LINE_LOGGER_ENABLED=0`. Until `SPORTSBETTR_MODE`
   exists, a second machine cannot be started safely for any phase.

The handoff also lists "Running the server 24/7" under *Deliberately not doing*, so whether
to migrate at all is the first decision.

## Part 1: git and pre-registration state

- **Working tree:** clean except 7 untracked files in `backend/experiments/results/`.
  - Must not be committed (results `.json`): `challenger_ablation_20261025T135617.json`,
    `challenger_stage1_20261024T200132.json`, `challenger_stage2_20261024T201305.json`,
    `per_player_models_20261023T140819.json`, `retro_challenger_20261024T215108.json`.
    `.gitignore` does not cover them (they show as `??`). Adding
    `backend/experiments/results/*.json` to it would stop accidents.
  - Real work: `per_player_models_20261023T140819.md` and
    `retro_challenger_20261024T215108.md`, the exploratory results the handoff cites. They
    are candidates to commit, and they contain no paper or challenger outcomes.
- **Unconfirmed housekeeping from handoff item 11: all landed.**
  - "Sync timing" pre-registration entry and README: `addb30f` (preregistration.md line 730,
    "Entry: sync timing (2026-10-24)").
  - Frontend test dependencies: `19f159f` (package.json and package-lock.json).
- **Handoff "Bugs fixed" table:** all 26 hashes checked are ancestors of HEAD (2be98a4,
  ed43929, b249931, 9f501ef, 1b001f9, 5038296, 086b158, e29cf56, 8c071d2, 8950c6c, a9e23ec,
  e051bec, f521a25, d77c71d, 7805a1b, 0fc980d, b265dcd, 431af4b, plus 5b1cd60, da21ea4,
  a760c8c, a34f54e, bdda977, 9dbd33d, addb30f, 19f159f).
- **Stray files to leave behind, not copy:** `underdog-intern/predictions.db` (0 bytes, repo
  root); `backend/predictions_v520_backup_2026-10-19.db`; orphan `-shm`/`-wal` files beside
  the backups in `backend/backups/`.
- **Git-ignore trap:** `.gitignore:42` ignores `*.lock`. A `requirements.lock` (plan 0.6)
  would be silently ignored and never reach the server through `git clone`. Name it
  `requirements-lock.txt`, or add an explicit `!backend/requirements.lock`.

## 2. Plan steps, Phases 0–6

| Step | Status | Reason |
|---|---|---|
| Purpose / out of scope | Needs edit | "The challenger model" is listed as out of scope, but it now drives the app and logs a pre-registered table every pre-tip. Screenshot import (Ollama) and historical.db/backfill.py also need a decision. The handoff lists 24/7 running as declined. |
| Current-state table | Needs edit | predictions.db is now 497 MB (163 MB of it free pages); Kaggle 28 GB / 21 versions; peak RAM 1.4 GB, not 0.45; more packages; `.env` holds 8 keys (adds `ODDS_API_HISTORICAL_KEY`, `TYPESAFE_API_KEY`, `SCREENSHOT_*`); 5 frontend files hard-code the API. |
| Architecture: 2 vCPU / 2 GB / 60 GB | Needs edit | RAM: see Part 4. Disk still fits if Kaggle is pruned. |
| Ground rules 1–3, 5–9 | Still valid | — |
| Ground rule 4 (no peeking) | Still valid; 3.2 conflicts with it | 3.2's "SHA-256 of SELECT *" reads outcome columns. Hash with outcome columns left out instead. |
| Ground rule 7 | Needs edit | Add `.lock`/results `.json` handling, and `backend/challenger/models/`. |
| 0.1 Run modes | Not done; needs edit | No `SPORTSBETTR_MODE`. The existing flags do not gate startup sync, catch-up pulls, rosters, ETL or resolvers. Full component list below. |
| 0.2 Missed-pull safety net | Partly done | Single-instance lock (`9f501ef`), startup catch-up pulls (`8c071d2`) and a startup re-schedule of pre-tip jobs exist. Missing: the 15-min self-check, `startup_log`, `daily_check.py`. |
| 0.3 Admin login | Not done; needs edit | No auth. `_require_local` must be replaced, not kept. Unauthenticated routes spend OddsAPI credits (`/wnba/lines`), delete rows (`DELETE /predictions/duplicates`) and write rows (`/slip` background thread). |
| 0.4 Configurable addresses | Not done; needs edit | 5 files now, plus the backend origin check in `_require_local`. |
| 0.5 Kaggle pruning | Not done | Handoff claim incorrect. `KAGGLEHUB_CACHE` confirmed as the setting (kagglehub 1.0.1 `config.py:23`). |
| 0.6 Lock file | Not done; needs edit | `*.lock` is git-ignored; uvloop on Linux; xgboost's Linux wheel and the CUDA library it pulls in; check every package has a Linux wheel for Python 3.14.4. |
| 0.7 Off-server backups | Not done; needs edit | Add historical.db, `challenger/models/`, the 12 `model_*.pkl` and `.env` (encrypted). |
| 0.8 Deploy guard | Not done; needs edit | Combine 45 min before a pre-tip, 15 min before any pull, 10:00–10:05 ET, and no running sync, ETL or pre-tip job. |
| 0.9 Phone layout | Partly | Predict form OK. Screenshot table scrolls sideways. History table is cramped at 390 px with no horizontal scroll. Screenshot reading has no GPU on the server. |
| 1.1–1.3, 1.5–1.8 | Still valid | — |
| 1.4 Create server | Needs edit | 4 GB RAM (or a code fix), plus a swap file. |
| 2.1, 2.2, 2.6 | Still valid | — |
| 2.3 Exact Python/packages | Needs edit | Lock file name; check wheels first; decide uvloop/xgboost. |
| 2.4 Secrets | Needs edit | `ODDS_MONTHLY_CREDIT_CAP=18000` confirmed. Add `OLLAMA_URL` or `ANTHROPIC_API_KEY` per decision D1; decide whether `ODDS_API_HISTORICAL_KEY`/`TYPESAFE_API_KEY` belong on the server at all. |
| 2.5 Model files | Needs edit | Also the 3 files in `backend/challenger/models/` (SHA-256 + fingerprint). All 4 WNBA pkls are required: if `model_wnba_Points.pkl` is missing, startup retrains and writes new WNBA pkls. |
| 2.7 systemd service | Needs edit | Raise RestartSec; set thread counts to 1; set `KAGGLEHUB_CACHE`; memory limit; proxy-header settings. |
| 3.1 Trial snapshot | Still valid | ~0.5 GB copy; backup API took 2.6 s here. |
| 3.2 Integrity | Needs edit | Leave outcome columns out of table hashes. |
| 3.3 Replay | Needs edit | Add challenger replay, v1 shadow replay and `/predict` parity (Part 5). |
| 3.4 End-to-end | Needs edit | Run `challenger_log.log_for_pull` as well as paper `log_for_pull`. |
| 3.5 ETL on Linux | Still valid | Measured here: 7.1 s, 131 MB peak (stubbed version check, cached v526). |
| 3.6 Write it down | Still valid | — |
| 4.1, 4.3–4.5 | Still valid | — |
| 4.2 Caddy | Needs edit | Request body ≥ 10 MB for `/screenshot/extract`; proxy read timeout ≥ 300 s (Ollama/Claude extraction); keep uvicorn's default trusted-proxy setting (127.0.0.1 only). |
| 4.6 Real devices | Needs edit | Test screenshot import on the path chosen in D1. |
| 5.1 Dry run | Needs edit | Use the job inventory in Part 6, not "06:00/07:00/10:00/T−30" alone. |
| 5.2 Pick the night | Still valid | — |
| 5.3 Stop the PC | Needs edit | No Task Scheduler task exists (declined). The PC `.env` must hold `SPORTSBETTR_MODE=dev`, which first has to exist. |
| 5.4 Final copy | Needs edit | Add `challenger_predictions`, `line_checks`, `player_aliases` counts; decide where historical.db lives. |
| 5.5 Go live | Needs edit | Startup log lines changed: no "Kaggle ETL SKIPPED"; look for `[sync]` 1/5–5/5 and "sync on startup + 7:00 ET" in the scheduler line. |
| 5.6 First morning | Needs edit | The 07:00 ET sync usually finds no new Kaggle version (Kaggle publishes 08:45–11:45 ET). The ETL comes from an hourly `kaggle_version_retry`, so check after 12:00 ET. |
| 5.7 First evening | Needs edit | Add challenger rows per game, and the `insufficient_history` share. |
| 5.8 Record it | Still valid | The pre-registration entry must also cover the challenger log's move. |
| Phase 6 routine | Needs edit | Screenshot import routine depends on D1; `daily_check.py` must cover challenger rows. |
| Rollback | Needs edit | Copy back `challenger_predictions` and `line_checks` too (whole DB via backup API; just say so). |
| Cost | Needs edit | 4 GB server; Claude API spend under D1 option 1. Verify prices before buying. |
| Open questions | Partly answered | Cap = 18,000 (`.env`); backfill now writes a separate `historical.db`, so the "merge" problem is gone; WNBA path still spends credits outside the cap. |

### 0.1: components a run mode must gate

| Component | Trigger | Writes DB | OddsAPI credits | Other network |
|---|---|---|---|---|
| Single-instance lock | startup | lock file | no | no |
| `enable_wal` | startup | pragma | no | no |
| `rosters.refresh_rosters` | startup; sync (not at startup) | rosters | no | ESPN |
| `player_match.rebuild_now` | startup; after roster refresh | espn_person_map, player_name_collisions | no | no |
| `database.init_db` (refresh_current + limited-data backfill) | startup | predictions (is_current, is_limited_data) | no | no |
| WNBA ETL | startup | wnba_player_stats | no | ESPN (only if data < 30 days stale; now skipped) |
| WNBA training | startup, if `model_wnba_Points.pkl` missing | **writes model_wnba_*.pkl** | no | no |
| Startup sync (`start_sync_in_background`) | startup | see sync | yes (catch-up) | yes |
| Catch-up pulls | first step of every sync | line_snapshots, odds_pull_log | **8/game** | OddsAPI |
| Backup | sync | backups/*.db | no | no |
| Kaggle version check + ETL | sync; hourly retry | player_stats, players, schedule, kaggle_version; predictions_backup.csv | no | Kaggle |
| Resolvers (app, paper, challenger) | sync; after ETL | predictions, paper_predictions, challenger_predictions | no | no |
| Hourly Kaggle retry | after every sync, while up | as ETL | no | Kaggle |
| Pre-tip scheduler (06:00 ET + once at startup) | cron / startup | odds_pull_log | 0 (events) | OddsAPI |
| Open pull (10:00 ET; catch-up at startup 10:00–12:59 ET) | cron / startup | line_snapshots, odds_pull_log | 8/game | OddsAPI |
| Pre-tip pull + paper + challenger logging | T−30, retry +5 | line_snapshots, odds_pull_log, paper_predictions, challenger_predictions | 8/game | OddsAPI |
| ESPN injury poller (20 min) + startup catch-up | interval | news_items, news_poll_log | no | ESPN |
| NBA injury report (:02/:17/:32/:47) + startup catch-up | cron | nba_report_index, news_events, news_items | only if `EVENT_TRIGGERS_ENABLED=1` (unset = off) | NBA PDF |
| `/sync` endpoint | API | as sync | yes | yes |
| `/wnba/lines/...` | API | wnba_odds_cache | **yes, outside the cap** | OddsAPI |
| backfill.py | manual CLI | historical.db only | yes (account floor) | OddsAPI |

Existing toggles (`LINE_LOGGER_ENABLED`, `NEWS_INGEST_ENABLED`, `PAPER_PREDICTIONS_ENABLED`,
`CHALLENGER_PREDICTIONS_ENABLED`, `LINE_LOG_OPEN_PULL_ENABLED`) are not a dev mode.
`LINE_LOGGER_ENABLED=0` stops the scheduler, but `_startup_complete` still runs `run_sync`,
which runs catch-up pulls (`run_catchup_pulls` checks no enable flag), the ETL and every
resolver.

### 0.2: locks on Linux

- `proc_lock.py`: `msvcrt.locking` on Windows, `fcntl.flock(LOCK_EX|LOCK_NB)` elsewhere,
  on `predictions.db.etl.lock` and `predictions.db.instance.lock` beside `DB_PATH`. These
  are OS locks, so the kernel drops them when the process dies, even on a crash or kill.
  A leftover lock *file* is never a stale lock. This works on a local ext4 SSD (flock is
  unreliable on NFS; the plan already rules out network disks).
- The instance lock is taken in the startup hook before the port binds. A second instance
  raises `RuntimeError`, so uvicorn exits. Under `Restart=always, RestartSec=10`, that
  becomes a 10 s refuse-and-exit loop while the other holder lives. It is harmless (it
  refuses before doing anything) but noisy.
- A crash *after* the lock (bad import, OOM) is worse: every restart refreshes ESPN
  rosters, checks Kaggle and runs a sync. Catch-up pulls are idempotent (skip games
  already pulled), so the loop doesn't spend credits again, but it does hammer ESPN and
  Kaggle. Use `RestartSec=60` plus `StartLimitIntervalSec`/`StartLimitBurst`.
- `_PRETIP_LOCK` (pre-tip jobs and the ETL) is a `threading.Lock`, process-local. That is
  correct because only one instance runs.
- Startup catch-up covers part of the 15-minute self-check: on every start,
  `line_pretip_scheduler_catchup` re-schedules today's pre-tip jobs, and
  `run_catchup_pulls` covers unpulled games. On an always-on server nothing restarts,
  though, so a job lost mid-day (scheduler thread exception, failed events call after the
  last retry) is still silent. The self-check is still needed.
- `startup_log` and `daily_check.py` do not exist.

### 0.3: routes

| Route | Spends credits | Writes DB | Upload | Ollama/Anthropic | External network |
|---|---|---|---|---|---|
| GET /players/search | | | | | |
| GET /players/{name}/team | | | | | |
| GET /teams | | | | | |
| GET /players/{name}/context | | | | | ESPN |
| POST /predict | | predictions | | | ESPN (opponent injuries) |
| GET /players/{name}/track-record | | | | | |
| GET /predictions/history | | | | | |
| DELETE /predictions/duplicates | | **deletes** | | | |
| PATCH /predictions/{id}/result | | predictions | | | |
| GET /slip | | predictions (background thread) | | | |
| GET /lines/{name}/{stat} | | | | | |
| GET /wnba/players/search, /wnba/teams, /wnba/players/{name}/team | | | | | |
| GET /wnba/players/{name}/context | | | | | ESPN |
| GET /wnba/lines/{name}/{stat} | **yes, uncapped** | wnba_odds_cache | | | OddsAPI |
| GET /health | | | | | |
| POST /sync, GET /sync | POST: yes (catch-up) | POST: all sync tables | | | Kaggle, ESPN |
| POST /screenshot/extract | | line_checks | **≤10 MB image** | **yes** | Ollama or Anthropic |
| POST, DELETE /screenshot/alias | | player_aliases | | | |
| /docs, /redoc, /openapi.json | | | | | |

`_require_local` guards `/sync`, `/screenshot/*` and the aliases using `request.client.host`
plus an `Origin` allow-list of `localhost:5173`. Behind Caddy there are two outcomes:

- With uvicorn 0.29 defaults (`proxy_headers=True`, trusted proxy 127.0.0.1), `client.host`
  becomes the real remote IP and the browser's `Origin` is `https://bettr.rodneyhong.com`.
  Every remote use returns 403, so screenshot import breaks.
- If proxy headers are turned off, `client.host` is 127.0.0.1 for every request, and any
  internet client that omits `Origin` (curl) passes.

Either way, the app-wide session dependency in 0.3 must replace it.

### 0.4: hard-coded addresses

- Frontend, `const API = 'http://localhost:8000'` in: `src/App.jsx:8`, `src/History.jsx:5`,
  `src/trackRecord.js:4`, `src/ScreenshotImport.jsx:5`, `src/components/PlayerSearch.jsx:6`.
  None send credentials.
- Backend: CORS `ALLOWED_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]`
  (`main.py:34`), reused by `_require_local`. `LOCAL_HOSTS` in `main.py`.
  `OLLAMA_URL` defaults to `http://localhost:11434` (`screenshot_import.py:40`; settable in `.env`).
- No `TrustedHostMiddleware`.

### 0.5: Kaggle cache

`~/.cache/kagglehub/.../versions/` holds **21 versions, 28 GB** (≈435 MB each, latest 526).
No pruning code exists anywhere in the backend, so the handoff's "pruned to the newest
version" is incorrect. kagglehub 1.0.1 reads the cache location from `KAGGLEHUB_CACHE`
(`kagglehub/config.py:23`, default `~/.cache/kagglehub`). At about one new version a day,
an unpruned cache would add roughly 75 GB by the end of the playoffs.

### 0.6: packages

- `backend/.venv`: **Python 3.14.4**. Plan's seven pins match exactly: numpy 2.4.4,
  scikit-learn 1.8.0, xgboost 3.2.0, pandas 2.3.3, joblib 1.5.3, scipy 1.17.1, kagglehub 1.0.1.
  Also in use at runtime: Pillow 12.2.0 (screenshot), anthropic 2.3.0 (Claude extractor),
  httpx 0.27.0 (Ollama client is plain httpx, no `ollama` package), APScheduler 3.11.0,
  pdfplumber 0.11.4 (NBA report), fastapi 0.111.0, starlette 0.37.2, uvicorn 0.29.0,
  python-multipart 0.0.26 (uploads), pyarrow 25.0.1, tzdata 2026.1. No OpenCV: card
  detection uses numpy and `scipy.ndimage`.
- Installed but unused at runtime: `nba_api` (docstring only), pytest, ruff, matplotlib
  (training plots).
- Windows-only: none strictly (no pywin32). `colorama` and `truststore` are cross-platform.
- Linux differences to decide:
  - `uvicorn[standard]` installs `uvloop` on Linux, which is absent from the Windows
    freeze. A lock built from the PC simply lacks it, which is fine.
  - xgboost's manylinux x86_64 wheel depends on `nvidia-nccl-cu12` (a few hundred MB); the
    `xgboost-cpu` wheel of the same version avoids it.
  - I could not check wheel availability for every pin on Linux/Python 3.14 offline. Before
    Phase 1, run `pip download -r <lock> --only-binary=:all: --platform manylinux2014_x86_64
    --python-version 3.14 -d tmp` (a PyPI download, no app data).
- `requirements.txt` exists but is loosely pinned (`pandas>=2.2.0`, unpinned
  xgboost/sklearn/joblib/kagglehub). No lock file exists.

### 0.7: backups today

- `line_logger.backup_database` (backup API + `quick_check`) writes
  `backend/backups/predictions_YYYY-MM-DD.db`, keeping `BACKUP_KEEP=3`, once per sync day
  (retaken if a new Kaggle version loads). It covers **predictions.db only**.
- `historical.db` (199 MB, 14,618 credits of pulls) has **no backup at all**.
- `backend/challenger/models/` has a manual copy in `backups/challenger_models_2026-10-24/`
  on the same disk (handoff "Now" item still open). The 12 `model_*.pkl` files have none
  outside the pre-registration hashes.
- Manual pre-fix backups (`manual_pre_*`, `predictions_pre_retro_*`): 1.5 GB. They don't
  need to go to the server.

### 0.8: deploy guard vs restart behavior

A restart now triggers the startup sync: catch-up pulls (8 credits per unpulled game,
skipped within 15 min of that game's pre-tip), the ETL if Kaggle is newer, and all
resolvers. It also runs the open-pull catch-up between 10:00 and 12:59 ET. A `systemctl
restart` during a pre-tip job kills it mid-transaction: credits are spent and that game's
rows roll back. The pre-tip retry only covers failed HTTP calls, not killed processes.

`deploy.sh` should refuse if any of these holds:

- a pre-tip pull is due within 45 min;
- any scheduled pull is within 15 min, or it is 10:00–10:05 ET;
- the ETL lock is held (sync or ETL running);
- a pre-tip job is running.

### 0.9: phone width (~390 px)

- Predict form: `max-w-lg w-full`, fine.
- Screenshot confirmation table: `minWidth: 640px` inside `overflow-x-auto`. It scrolls
  sideways and is usable.
- Mobile can upload: there is a file chooser (`input type=file accept=image/*`) as well
  as clipboard paste. Phone screenshots are a different layout from the 150%-zoom desktop
  board the card detector was tuned on (untested).
- History: an 11-column `table-layout: fixed` table with 3–14% column widths and
  `overflow-y-auto` only. Columns are about 12–55 px wide, unreadable without a mobile layout.
- There is no separate "board" page in `src/` (App + History only).

## Part 3: features not in the plan

**a) Challenger.**
- Files: `backend/challenger/models/challenger_stage2_frozen.pkl` (live),
  `baseline_b_stage2_frozen.pkl`, `challenger_stage1_dev.pkl` (research); 6.8 MB total.
- `challenger/live.py:29` pins SHA-256 `0e4fa88f…d604` and fingerprint `d18451fe…e7f`.
  The file is loaded lazily and cached on first use.
- Missing or mismatched file: `FrozenModelError`. `/predict` returns 503 for NBA
  Points/Rebounds/Assists/3PM. In the pre-tip job, `challenger_log` raises before writing,
  and line_logger catches it and prints `challenger predictions FAILED`. Paper rows still log.
- **No crash and no restart loop, but the failure is silent until someone reads the log.**
  Add a startup check that logs loudly, and a `daily_check.py` line for "pre-tip pull
  without challenger rows".
- Runtime dependencies: numpy, pandas, scipy (`optimize`, `special`, `stats`), scikit-learn
  `HistGradientBoostingRegressor`, pickle protocol 4.

**b) Screenshot import.**
- `SCREENSHOT_EXTRACTOR` = `ollama` (default) | `claude`; `_EXTRACT` dispatch at
  `screenshot_import.py:390`.
- Ollama: `OLLAMA_URL` (default `http://localhost:11434`), `SCREENSHOT_OLLAMA_MODEL`,
  `SCREENSHOT_OLLAMA_PARALLEL=6`, 300 s timeout per call.
- Claude: `ANTHROPIC_API_KEY`, `SCREENSHOT_CLAUDE_MODEL`. It reads the whole image; card
  cropping is Ollama-only.
- Limits: `MAX_IMAGE_BYTES` 10 MB, longest side scaled to 2,000 px.
- Images: the upload is read into memory, never written to disk; crops and re-encodes go
  to `BytesIO`. Extracted rows are written to `line_checks` (text only).

Options for the server:

1. **Server uses the Claude API extractor.**
   - Work: set `SCREENSHOT_EXTRACTOR=claude` and the key on the server. Optionally port
     card cropping to the Claude path; it is whole-image today and unbenchmarked on this
     board.
   - Cost: per-image token billing (set a spend limit; check current pricing).
   - Security and terms: Underdog screenshots go to a third party. That is not
     "publishing" under ToS §11, but note it. The API key lives on an internet-facing
     server, which raises the stakes of 0.3.
   - Works from the phone with the PC off.
2. **Browser on the PC calls local Ollama and sends rows to the server.**
   - Work: move card detection (numpy/scipy) and the two-read logic into the browser (JS
     or WASM), or into a small local helper. Allow the site's origin in `OLLAMA_ORIGINS`.
     Chrome's private-network-access rules for https→localhost may need a preflight-aware
     helper. Add an endpoint that accepts confirmed rows (they go through `/predict`
     already, but `line_checks` recording would move).
   - The most code; works only on the PC; any page the browser trusts with that origin can
     drive Ollama.
3. **Server calls Ollama on the PC through a reverse SSH tunnel.**
   - Work: `ssh -R 127.0.0.1:11434:localhost:11434 sportsbettr@server` from the PC
     (autossh to keep it up). Set `OLLAMA_URL=http://127.0.0.1:11434` on the server. No
     code change.
   - Works from any device while the PC and tunnel are up; that is when screenshots are
     taken anyway (desktop board at 150%).
   - Security: Ollama has no auth, but the tunnel binds only to the server's loopback,
     so only server processes (and anyone with server shell) can use the GPU. Images
     cross the SSH tunnel encrypted.
   - Cost: none. When the PC is off, extraction returns 503 "Extractor unavailable" and the
     app falls back to manual entry.
4. **GPU server.** Needs about 8 GB VRAM. Cost: an always-on GPU instance is roughly an
   order of magnitude above the planned server (check prices); serverless GPU per call is
   cheaper but adds a vendor and a cold-start of tens of seconds. Same third-party data
   flow as option 1.

**c) historical.db and backfill.py.**
- Size today: 199 MB (517,900 rows; 183 games + 3 probes), about **1.07 MB per game** and
  **13.6 KB per credit**.
- Projection: the full ~317,000-credit backfill comes to about 4.3 GB. Steals and blocks
  add roughly 25% more credits, so about 5.5 GB. 2023-24 has 5 books and no Underdog, so
  real per-game size there is lower: expect **3.5–5.5 GB**.
- Credit floor: backfill reads `x-requests-remaining` from its own responses (first value
  from a free live events call). That number is account-wide, so the floor stays correct
  when live pulls run on another machine. Floor = live games until next reset × 16 + 1,500,
  computed from the local predictions.db `schedule` table.
- The live cap counts only the live `odds_pull_log` (UTC month), so backfill credits never
  count against it (by design). Live pulls also refuse when the header's remaining credits
  fall below 8.
- Split machines break nothing in the accounting. One weakness: the 45-minute live guard
  and the floor read tip times from the **local** `schedule`. On the PC after cutover
  (dev mode, no ETL) that table goes stale. Restore a fresh server backup before each
  backfill run.
- historical.db can move independently: backfill only *reads* predictions.db (`schedule`,
  read-only) and writes nothing else.

**d) line_checks, player_aliases, challenger_predictions.** Nothing machine-specific: no
paths, hostnames or local times. Timestamps are UTC ISO. `challenger_predictions` records
`model_sha256`/`model_fingerprint`, which must match on the server (same file).
`player_aliases` uses personIds from Kaggle, which are identical everywhere.

**e) Time zones.**
- Explicit ET: all schedulers (`BackgroundScheduler(timezone=EASTERN)`), pulls, cutoffs,
  backups, sync, `_app_tip`, news.
- Naive local-time uses:
  - `data_fetcher.py:128` `date.today()`: player-search recency ranking.
  - `database.py:1426` `date.today()`: WNBA startup ETL gap.
  - `predictor.py:706` `datetime.now().month`: WNBA `is_playoff`.
  - `wnba_backfill.py:29` `date.today()`.
  - `challenger/stage1.py:409` and `challenger/stage2.py:161`: result-file timestamps
    (research only).
  - `database.py:189,205` `datetime.utcnow()`: UTC, so unaffected (deprecated since
    Python 3.12 but works).
- An **America/Chicago server clock reproduces the PC exactly**. None of these affect
  paper or challenger rows.
- History's UTC display: `predictions.timestamp` is `utcnow().isoformat()` with no offset,
  and the frontend shows it as-is. The migration doesn't change it. A separate fix would
  append `Z` and render local time.

**f) Logging and Windows assumptions.**
- No code reads logs or assumes UTF-16; `Tee-Object` appears only in the handoff's run
  command.
- `main.py` reconfigures stdout/stderr with `errors="replace"` (a cp1252 workaround),
  harmless on Linux.
- No `os.startfile`, PowerShell, `ollama.exe` or `nvidia-smi` calls in code. Windows paths
  appear only in docstrings of experiment scripts (`market_keys_probe.py`,
  `underdog_probe.py`, `export_real_line_testset.py`).
- `proc_lock.py` has both branches.
- Under systemd, logs go to journald.

## Part 4: measured numbers

Measured on this PC (desktop CPU) on backup-API temp copies with networking blocked.
Peak = Windows peak working set (≈ Linux RSS); "commit" = committed virtual memory.

**RAM**

| Step | Peak working set | Commit |
|---|---|---|
| Python baseline | 83 MB | 0.5 GB |
| Backend modules imported | 197 MB | 1.1 GB |
| + 12 frozen pkls + frozen challenger loaded | 343 MB | 1.45 GB |
| + paper `log_for_pull` (46 lines) | 357 MB | 1.46 GB |
| + challenger `log_for_pull` (47 lines, 4 short-history players) | **1,370 MB** | **2.5 GB** |
| One challenger prediction, established player | +14 MB (187 MB total) | — |
| One challenger prediction, player with <11 prior games | **+1,020 MB (1,192 MB total)** | 2.1 GB |
| ETL (separate process, cached v526, version check stubbed) | 131 MB | 0.6 GB |

- **Cause of the spike:** in `challenger/inference.py:features_for_context`, a player with
  fewer than 11 prior games gets `team_from = "0000"`, so `team_rows` loads every
  regular-season `player_stats` row ever into pandas. This happens in the pre-tip log and
  in `/predict` (manual form, screenshot rows) for any rookie or short-history player.
- `/predict` runs outside the pre-tip lock, so a rookie lookup during a pre-tip run stacks
  two spikes: about 2.4 GB in the process, plus about 0.3 GB for OS and Caddy.
- On Linux, glibc may keep the freed pandas memory, so RSS can stay high after a spike.

**Run time per pre-tip pull** (this desktop):
- Paper: 2.2 s.
- Challenger: 14.5 s, at about 2.7 s per established player and 5.3 s per short-history
  player, per player rather than per line.
- On 2 shared vCPUs, expect roughly 2–3× longer (estimate, not measured): about 35–50 s
  per game. Seven games sharing a tip would finish their captures 4–6 min after the first.

**Disk**

| Item | Now | Basis | End of regular season (2027-04-11) | End of playoffs |
|---|---|---|---|---|
| predictions.db | 497 MB (163 MB free pages from ETL swaps) | line_snapshots 271,107 rows at **278 B/row** (vs 412 assumed); 6,305 rows/game; 1,157 games left | **≈2.7 GB** | **≈2.9 GB** |
| — line_snapshots | 75 MB | above | +2.0 GB | +0.16 GB |
| — paper_predictions | 1,733 rows, 1,302 B/row | ~45 rows/game | +0.07 GB | +0.01 GB |
| — challenger_predictions | 88 rows, 1,257 B/row | ~47 rows/game | +0.07 GB | +0.01 GB |
| — player_stats | 841,625 rows, 198 B/row (167 MB) | +~32k rows/season; ETL swap leaves a same-size free area | ≈+0.01 GB | — |
| Rotating backups (3 + 1 being written) | 0.96 GB (2 files) | 4 × DB size | ≈10.8 GB | ≈11.6 GB |
| Manual backups | 1.5 GB | not copied | — | — |
| historical.db | 199 MB | 13.6 KB/credit | 3.5–5.5 GB after the November backfill | same |
| Kaggle cache | 28 GB (21 versions) | 435 MB/version | 0.9 GB pruned (one kept + one downloading); unpruned ≈+75 GB | same |
| Logs | 0.5 MB (2 days) | journald on server | negligible | negligible |
| Challenger + frozen models | ≈10 MB | fixed | same | same |

The plan's 4.1–5.7 GB database projection and 16–23 GB backup peak are too high. Measured
per-row size is lower, and the four-day jump from 234 MB to 497 MB is mostly ETL free pages
and the existing `historical_player_props` table, not line growth.

**Does 2 vCPU / 2 GB / 60 GB fit?**
- **Disk: yes.** OS ≈5, venv ≈2, frontend ≈0.4, DB 2.9, backups 11.6, Kaggle 0.9, backup
  staging ≈1 comes to about 24 GB, or about 35 GB with historical.db and one copy of it.
  That leaves about 40% margin, **only with Kaggle pruning**.
- **CPU: yes**, with slower stacked pre-tip captures.
- **RAM: no.** The challenger's short-history path needs about 1.4 GB in one process, and
  about 2.4 GB if `/predict` overlaps. Use 4 GB plus a 2 GB swap file, or fix the window in
  code (D2).

## Part 5: parity plan additions

The plan's Phase 3 covers frozen paper rows only. Add:

1. **Challenger replay.** For each `challenger_predictions` row with `status='ok'`, rebuild
   the feature frame from `features_json`, call `live.predict_from_features(F, stat,
   [line], model)`, and compare `mu`, `alpha`, `p_over`, `expected_minutes` and
   `rate_per_minute` exactly. Check the file's SHA-256 and fingerprint on both machines.
   Outcome columns (`actual_value`, `result`, `resolved_at`) are not read.
2. **v1 shadow replay.** For paper rows with `features_v1_json`, score with the frozen pkl
   and compare `p_over_v1`, as in the main replay.
3. **End-to-end:** run both `paper_predictions.log_for_pull` and
   `challenger_log.log_for_pull` into temp copies on both machines (same snapshot, same ET
   date, before any roster refresh or ETL), and diff the exports. On this PC, deleting one
   pull's rows and re-running reproduced **46/46 paper `p_over`** and **46/46 challenger
   (`mu`, `alpha`, `p_over`)** exactly, so same-machine determinism holds.
4. **App `/predict` path.** App rows don't store features. Call `challenger.live.predict`
   with a fixed tip on a fixed list of players (include short-history players), and the
   frozen `predictor.predict` for a combo stat, on both machines; compare outputs. Tip is
   an argument, so this doesn't depend on the date.
5. **Nondeterminism to expect.**
   - Logistic models are dot products through numpy's OpenBLAS, whose kernel choice
     depends on the CPU (AVX2 vs AVX-512). Last-ulp differences between the desktop and a
     server Xeon are possible.
   - XGBoost and `HistGradientBoostingRegressor` predictions sum trees in a fixed order,
     so they are deterministic regardless of thread count.
   - scipy's NB CDF is deterministic given its inputs.
   - Set `OMP_NUM_THREADS=1`, `OPENBLAS_NUM_THREADS=1` and `MKL_NUM_THREADS=1` in the
     service; this also caps memory. Record any last-digit difference explicitly, as the
     plan says.

## Part 6: schedule inventory

All cron times are ET (`BackgroundScheduler(timezone=America/New_York)`); "now" means on
each process start.

| Job (id) | Trigger | Credits | Tables written | Lock |
|---|---|---|---|---|
| Startup hook | each start | 0 | rosters, espn_person_map, player_name_collisions, predictions (flags), wnba_* | instance lock (process life) |
| Startup sync (`sync-startup` thread) | each start, after the hook | 8 × unpulled games (catch-up) | line_snapshots, odds_pull_log, backups/, ETL tables, predictions, paper_predictions, challenger_predictions | ETL lock (cross-process); `_PRETIP_LOCK` per catch-up pull and for the ETL load |
| `morning_maintenance` | cron 07:00, misfire 12 h | as sync (catch-up usually 0) | as sync (+ roster refresh) | ETL lock |
| `kaggle_version_retry` | date, hourly after each sync while nothing new; pushed clear of pull windows | 0 | ETL tables, predictions_backup.csv, resolvers | ETL lock (try once), `_PRETIP_LOCK` for the load |
| `line_pretip_scheduler` | cron 06:00, misfire 3 h; failure retry every 20 min | 0 (events) | odds_pull_log | none |
| `line_pretip_scheduler_catchup` | now | 0 | odds_pull_log | none |
| `line_open_pull` | cron 10:00, misfire 3 h | 8/game | line_snapshots, odds_pull_log | none |
| `line_open_catchup` | now, only 10:00–12:59 | 8/game not yet pulled | same | none |
| `pretip:{event}` | date, tip − 30 min (misfire 25 min) | 8 | line_snapshots, odds_pull_log, paper_predictions, challenger_predictions | `_PRETIP_LOCK` |
| `pretip-retry:{event}` | +5 min after a failed call, if ≥1 min before tip | 8 | same | `_PRETIP_LOCK` |
| `news_espn` | every 20 min | 0 | news_items, news_poll_log | none |
| `news_nba_catchup` | now | 0 | nba_report_index, news_events, news_items | none |
| `news_nba_report` | cron :02, :17, :32, :47 | 0 (event pulls off unless `EVENT_TRIGGERS_ENABLED=1`) | same | none |

The dry run (5.1) should compare all rows except the news jobs against `odds_pull_log` and
the `[sync]`/`[kaggle-retry]` log lines. The first-morning check (5.6) becomes:
- the 06:00 schedule line;
- the 07:00 sync (backup, rosters, "newest Ready vN, loaded vN");
- the 10:00 open pulls at 8 credits;
- one or more hourly retries until Kaggle publishes, with the ETL usually landing between
  09:00 and 12:00 ET.

## 3. New steps the plan needs, in order

1. **Decide D0–D6** (below) and rewrite the plan's scope and out-of-scope lines.
2. **Housekeeping on the PC:**
   - ignore `backend/experiments/results/*.json`;
   - choose a lock-file name not matched by `*.lock`;
   - copy `backups/challenger_models_2026-10-24/` off-disk (still open);
   - leave the stray root `predictions.db` and old manual backups behind.
3. **Phase 0 additions:**
   - 0.1 covering every component in the 0.1 table, with `dev` also skipping startup
     sync, catch-up pulls and WNBA training;
   - 0.3 replacing `_require_local`, and gating or removing `/wnba/lines` and
     `DELETE /predictions/duplicates`;
   - 0.5 pruning via `KAGGLEHUB_CACHE`;
   - startup integrity check for the challenger file;
   - turn off the WNBA startup ETL/training and lines on the server (D5);
   - the screenshot path chosen in D1;
   - either the memory fix (D2, with a parity test proving byte-identical challenger
     output) or a 4 GB server;
   - `daily_check.py` covering challenger rows;
   - the expanded deploy guard.
4. **Phase 2 additions:** run `tests/test_proc_locks.py` and the full backend test suite
   on Linux before any data arrives; set thread variables and `KAGGLEHUB_CACHE` in the unit.
5. **Phase 3 additions:** the five parity items in Part 5; outcome-free table hashes.
6. **Phase 4 additions:** Caddy body size and timeouts; screenshot import tested from two
   devices on the chosen path.
7. **Phase 5 additions:** dry run against the Part 6 inventory; cutover entry covering the
   paper and challenger logs; decide where historical.db lives at cutover.

## 4. Decisions only you can make

- **D0. Migrate at all?** The Oct 25 handoff lists "Running the server 24/7 or via Task
  Scheduler" under *Deliberately not doing*, and the cloud plan as "not done".
  - *Options:* keep the PC routine (catch-up pulls now cover late starts, but pre-tip
    pulls while the PC is off are still lost); migrate as planned; or migrate collection
    only and keep the app/screenshot UI on the PC.
  - *Recommendation:* migrate only if missed pre-tip pulls keep happening. The PC routine
    plus catch-up pulls has worked since Oct 21, and the remaining loss is pre-tip pulls
    while the PC is off.
- **D1. Screenshot import after cutover:** (1) Claude API, (2) browser-to-local-Ollama,
  (3) reverse SSH tunnel to the PC's Ollama, (4) GPU server.
  - *Recommendation:* (3). No code change and no new vendor; screenshots come from the
    desktop board, so the PC is on when it's needed. Add (1) later, with a spend limit,
    only if phone-only use matters.
- **D2. Memory:** 4 GB server plus swap, or fix the `team_from = "0000"` full-history load.
  - *Recommendation:* 4 GB plus swap now. Change challenger inference code only with a
    pre-registered note and a parity test showing identical `mu`, `alpha` and `p_over` on
    every stored row, since this is the frozen challenger's live path.
- **D3. Where historical.db and backfill.py live.**
  - *Recommendation:* keep them on the PC in dev mode. The floor uses the account-wide
    remaining count. Restore a fresh server backup before each run so the live guard sees
    current tip times. Back up historical.db off-disk in any case.
- **D4. Cutover date** relative to Nov 1 (credit reset), Nov 20 (backfill on the $119 plan)
  and Dec 15 (interim).
  - *Recommendation:* not within 48 h of Nov 20 if the backfill runs on the same machine
    as the live logger. Leave at least a week of clean server collection before Dec 15.
- **D5. WNBA path on the server.**
  - *Recommendation:* off. Gate `/wnba/lines` (it spends uncapped credits), the startup
    WNBA ETL and the training fallback. WNBA data has been stale since 2024-10-13.
- **D6. Untracked results.** Commit the two `.md` results files, or keep them local.
  Never the `.json` files.

## 5. Risks to H1–H3 and the challenger log during migration

1. **Two machines collecting.** There is no run mode yet. Any start of the PC backend after
   cutover runs catch-up pulls, the ETL and both loggers into a diverging database, and
   the instance lock protects only one machine. This is the largest risk; 0.1 must land
   first.
2. **Lost or partial pre-tip rows on restart.**
   - A deploy, or an OOM kill on a 2 GB box, during a pre-tip job loses that game's rows
     after the credits are spent. The retry only covers failed HTTP calls.
   - After a restart, re-scheduling skips games already pulled. If the crash came between
     the pull and the paper/challenger inserts, those rows may never be logged. This was
     not tested; `daily_check.py` should flag it.
3. **Cross-platform float differences.** Possible last-digit changes in `p_over` (Part 5).
   They must be measured and recorded in the cutover entry, never silently accepted.
4. **Challenger file missing on the server.** Paper rows would continue with no challenger
   rows and only a log line. Add a startup check and a daily check.
5. **Capture timing.** Slower CPUs and the serialized pre-tip lock push stacked-tip captures
   later than on the PC. The earliest-successful-pre-tip-row rule and the payout-mapping
   entry should note the machine change date.
6. **Peeking during parity.** Plan step 3.2's `SELECT *` hash reads outcome columns; hash
   with them left out.
7. **Disk fill.** An unpruned Kaggle cache fills 60 GB within weeks. Failed ETLs then leave
   `data_stale` rows in both logs.
8. **Unauthenticated exposure.** If the site went live before 0.3, anyone could delete app
   predictions, write results, trigger a sync or spend WNBA credits. Paper and challenger
   tables have no write routes, but `/sync` writes their resolutions.
