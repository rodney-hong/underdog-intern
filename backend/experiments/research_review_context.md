# Research review context: SportsBettr repo facts

Compiled 2026-10-18 at commit `b045c41`. Read-only investigation. No OddsAPI, ESPN or
NBA calls were made. `backend/predictions.db` was opened with
`sqlite3.connect("file:...predictions.db?mode=ro", uri=True)`. No model, trainer or
preregistration file was touched. Line numbers refer to files at that commit. Paths are
relative to `backend/`.

---

## 1. Underdog pricing

### How price is parsed and stored

**`line_logger.py` (forward logger, Points only)**

- Request: `bookmakers=underdog,fanduel,draftkings`, `markets=player_points`,
  `oddsFormat=american` (`line_logger.py:43-44`, `:391-400`).
- Parse: every bookmaker, market and outcome in the response. Rows store
  `o.get("point")` and `o.get("price")` unchanged (`line_logger.py:417-431`).
  Underdog gets no special handling. Price is read the same way for every book.
- Store: `line_snapshots.price REAL -- american odds as returned`
  (`line_logger.py:84`). The table holds both sides (`side` 'Over'/'Under', `:82`).
  The primary key includes `captured_at`, so rows are never overwritten (`:88`,
  `INSERT OR IGNORE` at `:435-441`).
- `paper_predictions.py:116-120` copies the Over/Under `price` from
  `line_snapshots` into `paper_predictions.ud_over_price` / `ud_under_price`
  (schema at `paper_predictions.py:72-73`).

**`data_fetcher.py` (slip path, `get_todays_lines`)**

- Request: `regions=us_dfs` with 7 markets and `oddsFormat=american`
  (`data_fetcher.py:799-808`, markets at `:642`).
- Parse: only `outcome.get("point")` and `outcome.get("description")`. **`price` is
  never read** (`data_fetcher.py:830-847`). Only the first outcome per player is
  kept (`seen` set, `:829-835`), so the Over and Under sides are not distinguished.
- Store: `underdog_lines(player_name, market_key, stat_line, game_date, game_id,
  home_team, away_team, league, captured_at)`. There is **no price column**
  (`data_fetcher.py:665-671`). Writes use `INSERT OR REPLACE` (`:677-684`).
- WNBA path: same pattern. It reads `point` only (`data_fetcher.py:1259-1288`).

### Query results

```sql
SELECT COUNT(*), COUNT(price), COUNT(DISTINCT price)
FROM line_snapshots WHERE bookmaker = 'underdog';
-- (0, 0, 0)

SELECT price, COUNT(*) FROM line_snapshots WHERE bookmaker = 'underdog' GROUP BY price;
-- no rows

SELECT * FROM line_snapshots WHERE bookmaker = 'underdog' ORDER BY captured_at LIMIT 5;
-- no rows (no sample rows exist)

SELECT bookmaker, COUNT(*), COUNT(DISTINCT price), MIN(price), MAX(price)
FROM line_snapshots GROUP BY bookmaker;
-- ('draftkings', 14, 10, -124.0, -103.0)
-- ('fanduel',     4,  3, -125.0, -106.0)

SELECT COUNT(*), COUNT(ud_over_price) FROM paper_predictions;          -- (0, 0)
SELECT COUNT(*) FROM sqlite_master WHERE name = 'underdog_lines';      -- 0 (table never created)
SELECT bookmaker, COUNT(*) FROM historical_player_props GROUP BY 1;
-- ('draftkings', 2224), ('fanduel', 125033)   -- no underdog; this table has no price column
```

`line_snapshots` holds 18 rows, all from one pull: `odds_pull_log.id = 2`, game
`26b036ff107f3c658258eaf4a6f26228` (Boston Celtics @ Detroit Pistons, 2026-10-20),
captured `2026-10-16T03:38:25Z`, `pull_phase = 'manual'`. That pull logged
`books_returned = 'draftkings,fanduel'`. Underdog was not in the response.

### Cached raw OddsAPI responses on disk

**None found.**

- `odds_cache` has one row (`cached_at 2026-10-19T03:41:05Z`) and its data is `{}`.
  `wnba_odds_cache` is empty.
- Both caches store the parsed `{player: {market: {value, source}}}` shape, not raw
  responses (`data_fetcher.py:853-860`).
- `experiments/jev_cache.sqlite` holds only `jev_answers` (LLM eval cache).
  `/predictions.db` at the repo root has no tables.
- No `.json` or `.jsonl` file outside `.venv` contains an outcome object. No code
  path writes a raw response to disk.
- `check_underdog_markets.py` only prints market keys and outcome counts
  (`check_underdog_markets.py:92-107`). It saves nothing.

**I cannot show an Underdog outcome object verbatim, because none has ever been
stored.**

### Is Underdog's per-pick payout multiplier captured?

**No.**

- No Underdog price value exists anywhere in the database (queries above).
- The slip path discards `price` entirely (`data_fetcher.py:830-847`).
- Whether OddsAPI's `price` field for `underdog` would encode a per-pick multiplier is
  **unknown**. No Underdog outcome has ever been received and stored, and nothing in
  the repo documents that field's meaning for Underdog.
- `grep -i "multiplier\|payout"` over `*.py` and `*.md` finds no code that models a
  per-pick Underdog multiplier. The only payout references are the fixed "2-leg 3x"
  assumption (§2).

---

## 2. Break-even thresholds

### Grep hits

Pattern: `57.7`, `0.577`, `33.3`, `0.333`, `1/3`, `1.0 / 3`, `1 / 3`, over
`backend/`, `frontend/src`, `README.md`, file types py/md/json/jsx/csv/txt, excluding
`.venv` and `node_modules`.

| file:line | text |
| --- | --- |
| `experiments/baselines.py:23` | `95% bootstrap CI for the top 5% / 10% / 20%, against the 57.7% target.` |
| `experiments/baselines.py:62` | `BREAK_EVEN = 0.577  # the hit rate a slice has to clear to be worth betting` |
| `experiments/preregistration.md:132` | ``Break-even for a 2-leg 3x payout is `1/3 = 0.3333`.`` |
| `experiments/preregistration.md:135` | `` `joint` > 0.3333**.`` |
| `experiments/preregistration.md:139` | `lower bound still clears 0.3333. ...` |
| `experiments/preregistration.md:152` | `` `joint` clears 0.3333 depends on the marginal under rate ...`` |
| `experiments/preregistration.md:172` | `0.3333 gate on dev. Ranked results:` |
| `experiments/preregistration.md:185` | `` `joint = 0.3333` given the maximum observed `lift` of +0.0143 ...`` |
| `experiments/preregistration.md:194` | `0.3333 gate removed**, ...` |
| `experiments/preregistration.md:214` | `**0 of 12** pair types have a 95% CI *upper* bound that even reaches 0.3333` |
| `experiments/preregistration.md:228` | `3PM 0.5443 [0.5344, 0.5541] — every CI far below the 0.5774 single-leg break-even.` |
| `experiments/preregistration.md:283` | `3PM ... 0.556 [0.536, 0.577] ...` (an AUC CI bound, not a threshold) |
| `experiments/test_c_frozen_candidates.json:4` | `"rule": "dev n_games >= 200, dev joint > 0.333333, top 12 by dev joint"` |
| `experiments/test_c_leg_correlation.py:6` | `break-even of 33.33% and (b) the product of its own marginals.` |
| `experiments/test_c_leg_correlation.py:46` | `BREAK_EVEN = 1.0 / 3.0          # 2-leg 3x payout` |
| `experiments/test_c_results.json:2` | `"break_even": 0.3333333333333333,` |

Incidental hits, not thresholds: `predictions_backup.csv` lines 31, 139, 238, 411, 498,
522, 543, 573, 579, 685. These are confidence values such as `0.5771`, `0.5776` and
`0.5777`, or explanation text.

- No hit in `frontend/src`, `README.md`, `logging_plan.md`, `predictor.py`,
  `ml_trainer.py`, `main.py` or `paper_predictions.py`.
- The repo never derives 0.577 / 0.5774 in writing.
- Arithmetic only, not stated in the repo: √(1/3) = 0.57735.

### December thresholds in `preregistration.md`

**`preregistration.md` defines no December threshold.** The full December section
(`preregistration.md:303-306`):

> ### December check
>
> Score frozen-model predictions logged from 2026-10-20 against results, on real Underdog
> lines. Anything learned before then is recorded, not acted on.

Related December text (`preregistration.md:236-239`):

> **No model changes until the December 2026 check.** `ml_trainer.train_model()` refuses
> to run without `force=True`, and startup never retrains NBA models: new Kaggle versions
> still load box scores and resolve predictions, but the model files below do not change.
> WNBA models are outside this freeze.

### Amendment procedure (`preregistration.md:3-5`)

> Every section below is written **before** the corresponding numbers are computed.
> Sections are append-only. If a decision changes after results are seen, it is
> recorded as a new dated entry, never by editing an earlier one.

Test C applies this procedure in its "Entry 1/2/3" blocks (`preregistration.md:166-230`).
No amendment procedure specific to the December check exists.

---

## 3. Features

### Shared setup

Training is `ml_trainer.build_training_data`. It uses `player_stats` rows with
`numMinutes > 0` (`ml_trainer.py:119-128`) and counted game types only (`:137-145`). Rows
are sorted by `personId, gameDateTimeEst` (`:147`), and all rolling windows are per
player with `shift(1)`, so the current game is excluded.

Inference is `predictor._compute_ml_features`. It works from `get_game_logs(player,
last_n=40)` (`predictor.py:215`). The log is ordered most-recent-first (comment at
`predictor.py:267`), so `.head(n)` means the last n games.

The `_pts_` names are historical. The same formulas run on whichever stat column is
being modelled (`ml_trainer.py:225-231`, `predictor.py:219`).

| feature | training | inference |
| --- | --- | --- |
| `avg_pts_last5` | `g.transform(lambda x: x.shift(1).rolling(5, min_periods=5).mean())` (`ml_trainer.py:230`) | `stat_values.head(5).mean()` (`predictor.py:221`) |
| `avg_pts_last10` | `feature_defs.lagged_avg(df, col, 10)` = `x.shift(1).rolling(10, min_periods=10).mean()` (`feature_defs.py:27-29`, `ml_trainer.py:231`) | `stat_values.head(10).mean()` (`predictor.py:222`) |
| `hit_rate_last10` | `feature_defs.hit_rate_last10` (below) (`ml_trainer.py:235-236`) | the same code via `feature_defs.next_game_features` on `get_player_history` (`predictor.py:228-231`, `feature_defs.py:50-69`) |
| `trend` | `avg5 - avg10` (`ml_trainer.py:238`) | `avg5 - avg10` (`predictor.py:224`) |
| `consistency` | `g.transform(lambda x: x.shift(1).rolling(10, min_periods=10).std())`, pandas default ddof=1 (`ml_trainer.py:256`) | `stat_values.head(10).std()` if ≥ 2 games, else `0.0` (`predictor.py:239`) |
| `vs_opponent_avg` | `feature_defs.vs_opponent_avg(df, col, avg10)` (below) (`ml_trainer.py:259`) | the same via `next_game_features`, falling back to `avg10` if NaN (`predictor.py:230-232`) |
| `line_diff` | `avg10 - line` (`ml_trainer.py:257`) | `avg10 - stat_line` (`predictor.py:236`) |

**`hit_rate_last10`** (`feature_defs.py:37-40`):

```python
beat = (df[col] > exp_mean_lagged).astype(float)   # exp_mean_lagged = x.shift(1).expanding(min_periods=1).mean()
beat.groupby(df[key]).transform(lambda x: x.shift(1).rolling(10, min_periods=10).mean())
```

This is the share of the player's previous 10 games in which the stat beat **his own
career-to-date mean as of that game** (`feature_defs.py:9-12`). **It is not measured
against the prop line.**

**`vs_opponent_avg`** (`feature_defs.py:43-47`):

```python
vs = df.groupby([key, opp_key])[col].transform(lambda x: x.shift(1).expanding(min_periods=1).mean())
return vs.fillna(avg10)        # opp_key = "opponentteamId"
```

**The `line` in `line_diff` during training** depends on the stat:

- Points, Rebounds, Assists and 3PM use the real book line from
  `real_lines.build_real_line_lookup`. That value is the mean of all resolved book lines
  for (name, ET date, stat) (`real_lines.py:182-192`), looked up at
  `ml_trainer.py:248-252`.
- Combo stats use a simulated line, `exp_mean_lagged + U(-1.5, 1.5)`
  (`ml_trainer.py:242-243`).
- The label is `(df[col] > line)` (`ml_trainer.py:293`).

WNBA inference repeats these formulas at `predictor.py:564`, `:578` and `:579`.

### Medians or quantiles

**None in any feature.**

`grep -rn -i "median\|quantile\|percentile" --include=*.py` (excluding `.venv`) hits
only in `experiments/`:

- bootstrap percentile CIs: `baselines.py:66-73`, `jev_report.py:59-73`,
  `model_selection.py:77`, `test_c_leg_correlation.py:210-229`
- descriptive prints: `schema_audit.py:152`, `score_check_set.py:112`

There are no hits in `feature_defs.py`, `predictor.py` or `ml_trainer.py`.

### Game spread, game total, team implied total

**None.**

- The 14 `FEATURE_COLS` (`ml_trainer.py:39-54` = `predictor.py:36-51`) contain no
  spread, total or implied-total feature.
- `line_logger` requests `markets = "player_points"` only (`line_logger.py:43`).
  `data_fetcher` requests player-prop markets only (`data_fetcher.py:642`).
- `grep -i "spreads\|h2h"` finds nothing in any `.py` or `.md` file. The only `totals`
  hit is an unrelated local dict at `analyze_historical_props_match.py:89`.
- A column-name scan via `PRAGMA table_info` for
  `spread|total|implied|pace|score|ou|handicap` found no game-line column in
  `line_snapshots` (14 cols), `paper_predictions` (36 cols),
  `historical_player_props` (12 cols), `predictions` or `schedule`. The only matches
  were stat columns in `player_stats` (`reboundsTotal`, ...) and `estimatedPace`.
- `paper_predictions.features_json` stores the 14 features only.
- Closest team-context items, neither of which is a market line:
  - `player_stats.estimatedPace` is SELECTed in training (`ml_trainer.py:122`) but is
    **not** in `FEATURE_COLS` and is not used afterwards.
  - `opp_def_rating_last5` is the opponent's rolling 5-game box-score defensive rating
    (`ml_trainer.py:162-180`).

---

## 4. Exploratory stats: historical real-line Points rows

**Method.** This is a read-only reconstruction of `ml_trainer.build_training_data`'s
Points population, without importing `ml_trainer`:

- `real_lines.build_real_line_lookup(conn)` with a `mode=ro` connection
- `player_stats` where `numMinutes > 0`, `gameType IN game_types.COUNTED_GAME_TYPES`
- sorted by `personId, gameDateTimeEst`
- `avg10 = feature_defs.lagged_avg(df, "points", 10)`
- `line = lookup[(norm_name, ET date, "Points")]`

```python
SELECT personId, firstName, lastName, gameDateTimeEst, gameType, numMinutes, points
FROM player_stats WHERE numMinutes IS NOT NULL AND numMinutes > 0
```

No modeling was done.

**Population A** is the exact training population (line matched and `avg10`
defined). It reproduces the `29,836` training rows recorded for `model_Points.pkl` at
`preregistration.md:253`.

Lookup resolution was Points resolved 30,034 / unresolved 5,764 rows, giving 30,011
unique Points keys.

| statistic | A: training population | B: all resolved, minutes > 0 (no 10-game filter) |
| --- | --- | --- |
| n | **29,836** | 29,995 |
| date range | 2023-10-24 → 2026-05-25 | same |
| over rate, `points > line` | **0.4856** | 0.4852 |
| pushes, `points == line` | 0 | 0 |
| mean / median of `(actual_points − line)` | **+0.151 / −0.5** | +0.148 / −0.5 |
| mean / median of `(avg_pts_last10 − line)` (= `line_diff`) | **+0.125 / +0.2** | n/a |
| skewness of actual points (Fisher; scipy `bias=True` and pandas agree to 4 dp) | **0.669** | 0.672 |
| mean / median of actual points | 15.80 / 15.0 | 15.77 / 15.0 |

**Over rate by calendar month (population A).** Month is from `gameDateTimeEst` across
all three seasons.

| month | n | over rate |
| --- | --- | --- |
| Oct | 1,432 | 0.4846 |
| Nov | 4,538 | 0.4967 |
| Dec | 4,067 | 0.4728 |
| Jan | 5,940 | 0.4810 |
| Feb | 4,154 | 0.4846 |
| Mar | 6,106 | 0.4864 |
| Apr | 2,796 | 0.5011 |
| *May (outside Oct–Apr)* | 683 | 0.4788 |
| *Jun (outside Oct–Apr)* | 120 | 0.4083 |

By season (start year): 2023-24 n = 8,543, over 0.4766; 2024-25 n = 9,399, over 0.4963;
2025-26 n = 11,894, over 0.4835.

Notes:

- The lookup line is a mean over all resolved lines for a key (`real_lines.py:192`).
  9 of the 29,836 lookup values are integers, but none equals the actual score, so
  pushes = 0.
- Lines are FanDuel/DraftKings, not Underdog (`historical_player_props` bookmaker counts
  in §1).

---

## 5. December plan

**Scheduled for December in `preregistration.md`:**

1. **December check: score the frozen model** (`preregistration.md:303-306`). "Score
   frozen-model predictions logged from 2026-10-20 against results, on real Underdog
   lines."
   - Not specified: the metric (AUC, hit rate, log loss, ...), the threshold, which of
     the 8 frozen models or stats are scored, or minimum n.
   - The paper log covers Points only (`paper_predictions.py:3-9`, `STAT_TYPE = "Points"`
     at `:36`) and has 0 rows today.
2. **Train/inference parity check** (`preregistration.md:298-301`). Six features are
   "Not verified identical ... check at the December review": `games_played_last15`,
   `fatigue_score`, `minutes_trend`, `avg_minutes_last5`, `home_away_split`,
   `opp_def_rating_last5`. No pass criterion is given.
3. **Model freeze lifts at the December check** (`preregistration.md:236`). Echoed at
   `ml_trainer.py:359` and `paper_predictions.py:20-21`. This is a constraint, not a
   test.

**Scheduled for December in `logging_plan.md`:** nothing. The file contains no December
or date-specific test schedule.

- It defers Tests A (devig calibration), B (cross-book disagreement) and D
  (market-baseline residual) to forward logging (`logging_plan.md:5-8`, `:119-121`).
- It says only that "the earliest they can be answered is after a slate count large
  enough to matter, which at ~7 games/night is one to two months of logging"
  (`logging_plan.md:125-127`).
- No hypotheses, thresholds or n are specified for A, B or D.
- `logging_plan.md:3` still reads "Plan only — no code changed. Nothing here has been
  implemented." `line_logger.py` now implements the logging (`line_logger.py:1-24`).
- The data_fetcher line numbers cited in `logging_plan.md` §1-2 (e.g. `:598`, `:749`,
  `:720`) no longer match current `data_fetcher.py`; the same code is now at
  `:658-688`, `:849` and `:820`.

**Primary test or multiple-comparison correction for December:** **none specified**, in
either file.

- The only multiplicity correction in the repo belongs to Test C, which is already final
  (`preregistration.md:136-140`): Bonferroni, 99.583% CI for 12 candidates.
- The only primary/secondary designation also belongs to Test C
  (`preregistration.md:134-141`).

---

## 6. Credit cost

**One per-event call with `markets=player_points` and
`bookmakers=underdog,fanduel,draftkings` cost 1 credit.** This was observed once.

```sql
SELECT * FROM odds_pull_log WHERE endpoint = 'event_odds';
-- (2, '2026-10-16T03:38:26.208796+00:00', '2026-10-20', '26b036ff107f3c658258eaf4a6f26228',
--  'event_odds', 'manual', 200, remaining_before=500, remaining_after=499,
--  requests_last=1, credits_used=1, rows_inserted=18, books_returned='draftkings,fanduel', ...)
```

- `x-requests-last = 1`, and `x-requests-remaining` went from 500 to 499.
- Underdog was **not** in that response, so the cost of a call that does return
  Underdog is **unobserved**.
- Every other logged call is the free events endpoint (`credits_used = 0`, ids 1, 3-13).

**Code and docs:**

- `line_logger.py:377`: `# Expected cost is 1 (1 market x 1 region-equivalent)`.
- `line_logger.py:178`: fallback of 1 credit per odds call when headers are missing.
- `logging_plan.md:113-117`: "passing two regions multiplies cost by two, whereas the
  documented rule prices up to 10 named bookmakers as one region equivalent. Confirm
  with the probe in §1 before relying on it."
- `logging_plan.md:133-136`: "the events list is free; an event-odds call costs
  `1 × markets × regions`; historical endpoints cost 10×. **Verify** ... rather than
  trusting this from memory."
- `opening_day_check.py:11-17`, `:97`, `:108`: expects 1 credit per three-book call and
  documents the `us_dfs` probe as "1 market x 1 region = 1 credit per call".
- `data_fetcher.py:768`: the slip path budgets `len(today_events) * 7` (markets × 1
  region, upper bound).

**Cost of adding `spreads,totals` to the same call: not documented in the repo.**

- No `.py` or `.md` file mentions `spreads`, `h2h` or game `totals` markets (grep in §3).
- The only general rule written down is `1 × markets × regions`
  (`logging_plan.md:133-134`). Arithmetic on that rule alone, not verified in the repo
  for game-line markets on the per-event endpoint: 3 markets × 1 region = 3 credits.
- Actual cost: **unknown** until observed via `x-requests-last`.
