# CLAUDE.md

SportsBettr is a research system for NBA player props on Underdog. A FastAPI backend (Python, SQLite) logs lines from 10 books before each game, two models make predictions, results are graded from box scores, and pre-registered tests run at season's end. The React frontend is in `frontend/`. Nothing gets bet.

**Precedence:** the SportsBettr rules below override the general guidelines further down. If a task seems to require breaking one, stop and ask. Do not look for a workaround.

## SportsBettr hard rules

### Frozen models
- Never modify, retrain, re-save, rename or delete `backend/model_*.pkl` or anything in `backend/challenger/models/`. They are SHA-256 checked, and the live challenger refuses to run without the exact file.
- Never change the frozen 2026-27 models, the frozen challenger, or hypotheses H1–H3 before the final analysis.

### Pre-registration
- `backend/experiments/preregistration.md` is append-only. Never edit, reword, reorder or delete existing text.
- A new entry is dated and written before any result it affects. Never write or backdate an entry after looking at the results it covers.
- Never compute, print or summarise paper results (`paper_predictions` outcomes, overall or per player) before the final analysis. This includes "quick checks" and test fixtures.
- Never tune, select or compare models on 2025-26 data or on backfilled lines unless a dated entry covers it. Never evaluate the frozen models on backfilled seasons.
- Test A is computed once, after the full November backfill.

### Data and the running backend
- Before any manual write to `predictions.db` or `historical.db`, make a dated backup in `backend/backups/` and say where it is.
- Never run `run_morning_maintenance()` by hand. Sync does it.
- Only one machine may collect (SPORTSBETTR_MODE=prod). The other runs only dev (or dry during the dry run). Never start a second server on the same machine. 
- Ask before restarting the backend. Never restart within 45 minutes of a scheduled pull or between 10:00 and 10:05 ET.
- Ask before running anything that spends OddsAPI credits. Respect `ODDS_MONTHLY_CREDIT_CAP` and the backfill credit floor.

### Scope
- No real-money betting features. Never describe the models as having an edge; none has been measured.
- LLMs only transcribe screenshots and, later, may extract injury text. Never use an LLM as a predictor.
- Never scrape Underdog or drive its site with a browser agent (Terms of Use §6(ix)). Screenshots never go in the repo.
- Never publish raw Underdog line data (Terms of Use §11). Aggregated findings only.
- No package upgrades mid-season. Ask before adding any dependency.

### Git (public repo)
- Read `git status` before every commit. Stage explicit paths only; never `git add .` or `git add -A`.
- Never commit anything ending in `.db`, `-wal`, `-shm`, `.png` or `.env`. Commit `.json` only for deliberate config files such as `package.json`.
- `backend/.env` is never committed. `backend/.env.example` holds variable names only, never values.

### Environment
- Windows and PowerShell. Run backend commands from `backend\` with the venv active, or call `.\.venv\Scripts\python.exe` directly.
- The server is Ubuntu (/home/sportsbettr/sportsbettr, systemd service sportsbettr, logs via journalctl -u sportsbettr). Code changes are made on the PC and reach the server only through git push and ./deploy.sh.
- On the PC, backend logs in backend/logs/ are UTF-16, so plain-text search can miss them.
- Game-day times in the handoff are Central unless marked ET.
- Tests: `npm test` in `frontend/`; `feature_parity.py` for inference parity of the frozen models.
- Run tests only through the pytest command line, never with pytest.main() inside a longer-running script.
- Opt-in slow test: the /slip before/after parity check (`slip_parity` marker, ~6 min) is skipped by default; run it from `backend\` with `$env:RUN_SLIP_PARITY = "1"; .\.venv\Scripts\python.exe -m pytest tests/test_slip.py -m slip_parity`.

---

## General guidelines

Behavioral guidelines to reduce common LLM coding mistakes. Adapted from forrestchang/andrej-karpathy-skills (MIT).

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

### 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

### 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

### 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

A test must never read paper results or evaluate a frozen model on held-out data (see the pre-registration rules).

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
