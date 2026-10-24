# Pre-registration — market-structure tests

Every section below is written **before** the corresponding numbers are computed.
Sections are append-only. If a decision changes after results are seen, it is
recorded as a new dated entry, never by editing an earlier one.

All line data is **FanDuel** (98.3% of `historical_player_props`; DraftKings covers
61 disjoint games). Production prices on **Underdog**. Therefore every result in this
series is labelled **"FanDuel lines, hypothesis for Underdog"** and is never a
betting recommendation.

---

## Step 0 — Schema audit (completed 2026-10-05, descriptive only)

No hypothesis, no threshold: counts only. Outcome recorded in `schema_audit.py`.

Findings that bind the rest of this series:

- `historical_player_props` has **no price/odds column** and stores **only the Over
  outcome**. Markets are one-sided.
- **0** (game, player, stat) markets carry both FanDuel and DraftKings.
- Exactly 1 snapshot per (game, player, stat, book) — no line-movement history.

Consequences, decided before any further computation:

| Test | Status | Reason |
| --- | --- | --- |
| A — devig calibration | **Not run** | No prices; markets one-sided. Nothing to devig. |
| B — cross-book disagreement | **Not run** | 0 overlapping markets. n would be 0. |
| C — leg correlation | **Run** | Real lines + box scores both present. |
| D — market-baseline residual | **Not run** | Baseline requires Test A's devigged probability. |

Tests A, B and D are not "failed" — they are **untestable on this data**. They are
deferred to the forward-logging plan (`logging_plan.md`) and must not be reported
as evidence either way.

---

## Test C — Same-game leg correlation

**Written 2026-10-05, before computing any dev or eval number.**

### Hypothesis

Two prop legs in the same NBA game are **positively correlated** — driven by shared
game state (pace, blowout, foul rate, minutes distribution) that no per-player model
sees. If the correlation is strong enough, some *pair types* clear the 2-leg break-even
even though single legs are at AUC ~0.50.

Null hypothesis: legs are independent, so joint hit rate = product of marginals, and
no pair type clears break-even except by base rate alone.

### Definitions

**Leg** = (player, stat, side), side ∈ {over, under}, graded against the resolved
FanDuel line from the validated join in `real_lines.py`.

- over hits iff `actual > line`; under hits iff `actual < line`.
- No pushes exist: 0 of 127,257 stored lines is an integer, so `actual == line`
  is impossible. Any such row that appears anyway is dropped and counted.

**Pair** = an unordered pair of legs from the **same game** belonging to **two
different players**. Same-player pairs are **excluded** — Underdog does not allow them.

**Pair type** = `(relation, legA, legB)` where

- `relation` ∈ {`teammate`, `opposing`} — same `playerteamName` or not;
- `legA`, `legB` are the two `"{stat}:{side}"` strings sorted lexicographically, so
  each unordered pair maps to exactly one type.

This makes side part of the type, so **same-direction (over/over, under/under) and
mixed (over/under) pairs are separate candidates**, as are teammate and opposing.

### Inclusion / exclusion (fixed now, applies identically to dev and eval)

1. Stats: `Points`, `Rebounds`, `Assists`, `3PM` — the four with resolved real lines.
   3PM is retained for correlation structure even though Underdog does not carry it;
   any 3PM candidate is flagged **not actionable on Underdog**.
2. Only props resolving to **exactly one** box-score game under `real_lines.py`.
3. **DNP exclusion: drop any leg whose player recorded 0 or NULL minutes.** A real
   book voids the leg instead of grading it, so grading a DNP as an under hit would
   manufacture correlation — teammates sit together in blowouts and tank games, which
   is exactly the signal under/under pairs would pick up. This is the single largest
   threat to this test's validity and is excluded, not modelled.
4. Exclude `gameType` in {`Preseason`, `Pre Season`, `All-Star Game`}. Retain
   Regular Season, Playoffs, Play-in, Emirates Cup.

### Split (temporal, no overlap)

- **dev** = seasons 2023-24 and 2024-25 (box-score date < 2025-08-01)
- **eval** = season 2025-26 (box-score date >= 2025-08-01)

Candidate selection touches **dev only**. Eval is computed once, after the candidate
list is frozen in writing in Section "Frozen candidates" below.

### Metrics (per pair type)

- `joint` = fraction of pairs where **both** legs hit — the quantity that must clear
  break-even.
- `pA`, `pB` = marginal hit rate of each slot, computed **on the same pair population**
  (not over all props), so `joint` and `pA*pB` are comparable.
- `lift` = `joint - pA*pB`. This is the correlation term, reported separately from the
  base rates so a high `joint` driven purely by high marginals is visible as `lift ~ 0`.
- `n_pairs` = number of pairs; `n_games` = number of distinct `gameId` contributing.
  **`n_games` is the effective sample size and is the n reported.**

### Uncertainty

Pairs within a game are heavily dependent (median 30 props/game → hundreds of pairs
from one game's shared state). All CIs are **cluster bootstrap over `gameId`**:

- resample `n_games` game ids **with replacement**, 10,000 replicates, fixed seed 20261005;
- recompute `joint` and `lift` from all pairs in the resampled games;
- 95% CI = percentile [2.5, 97.5].

Pair-level (naive) CIs are **not** reported as evidence; a naive CI would be far too
narrow and would manufacture a pass.

### Candidate selection rule (dev)

Among pair types with **dev `n_games` >= 200**, rank by dev `joint` descending and take
the **top 12**. That list is then frozen below and is the only thing evaluated on eval.
Rationale for 12: keeps the multiplicity correction tractable while covering both
relations and both direction patterns.

If fewer than 12 types meet `n_games >= 200`, all qualifying types are taken and the
actual count is recorded.

### Pass threshold

Break-even for a 2-leg 3x payout is `1/3 = 0.3333`.

- **PASS** (primary, as specified): eval cluster-bootstrap **95% CI lower bound on
  `joint` > 0.3333**.
- **Multiplicity-corrected (secondary, stricter):** with 12 candidates, a 95% per-test
  CI has inflated family-wise error. Also report the Bonferroni-corrected interval —
  per-candidate two-sided level `0.05/12`, i.e. a **99.583% CI** — and mark whether the
  lower bound still clears 0.3333. A candidate passing the 95% rule but failing the
  corrected one is reported as **marginal, not a pass**.
- **Correlation claim** (separate from the payout claim): `lift` 95% CI excluding 0.
  A pair type can clear break-even with `lift ~ 0` (pure base rate) — that is a
  base-rate finding, not a correlation finding, and is labelled as such.

A pass is a **hypothesis about Underdog**, not a recommendation: FanDuel lines,
no price data, no vig modelling, and no Underdog line comparison exists.

### Declared in advance

- Expected most likely outcome: under/under teammate pairs show the largest `lift`,
  because shared slow-pace / low-total game state pushes both unders together. Whether
  `joint` clears 0.3333 depends on the marginal under rate, which is ~50-55% on real
  lines; `0.55^2 = 0.3025`, so a pass **requires** real positive correlation and cannot
  come from base rates alone at that marginal.
- If no candidate passes, that is the result and will be reported as such. No
  re-selection, no threshold change, no additional pair-type families.

---

## Frozen candidates — Test C

*(This block is appended after dev is computed and before eval is touched.)*

**Status at time of writing: NOT YET COMPUTED.**

### Entry 1 — dev computed 2026-10-05, before any eval number was produced

Dev slice: 2023-24 + 2024-25, 58,587 props over 2,197 games. All 72 pair types met
`n_games >= 200`, so the sample-size gate excluded nothing.

**The pre-registered selection rule yields 0 candidates.** No pair type reaches the
0.3333 gate on dev. Ranked results:

- best joint overall: `teammate 3PM:under x Assists:under` = **0.2864** (2,084 games)
- best non-3PM (Underdog-actionable): `teammate Assists:under x Points:under` = **0.2755**
- worst: `opposing 3PM:over x 3PM:over` = 0.2163
- largest `lift` anywhere in the 72: **+0.0143**; smallest: -0.0143
- implied marginals are ~0.52 per leg (`pA*pB` ~0.27 at the top of the table)

**Test C therefore terminates as NO PASS at the selection stage.** This is a stronger
negative than an out-of-sample failure: no pair type clears break-even even *in-sample*,
on two full seasons, with selection free to overfit 72 types against ~2,200 games.

Arithmetic that makes the gap concrete, computed from the dev table: to reach
`joint = 0.3333` given the maximum observed `lift` of +0.0143, the marginals would have
to satisfy `p^2 >= 0.319`, i.e. **p >= 0.565 per leg**. Single-leg AUC is ~0.50, so leg
selection cannot supply that. Correlation is not the binding constraint — the marginal
hit rate is, and it is ~0.52.

### Entry 2 — descriptive eval carry-forward, declared before eval is computed

Because 0 candidates qualified, there is nothing for the pass test to score. To avoid
reporting a dev-only result, the **top 12 by dev joint are carried to eval with the
0.3333 gate removed**, and are evaluated with the same cluster-bootstrap machinery.

This carry-forward is **descriptive, not a hypothesis test**, and is binding:

- These 12 **cannot pass.** They failed the pre-registered gate on dev. No eval number
  can convert them into a pass, and none will be reported as one.
- Its only purpose is to confirm out-of-sample that the dev picture is stable rather
  than an artifact of the dev seasons, and to put CIs on how far short the gap is.
- Still reported: eval `joint`, 95% and Bonferroni cluster-bootstrap CIs, `lift` and
  its CI, `n_games` as effective n.
- Labelled **FanDuel lines, hypothesis for Underdog**, never a recommendation.

No further pair-type families, thresholds or slices will be introduced for Test C.

### Entry 3 — eval computed 2026-10-05, once, on the frozen 12

Eval slice: 2025-26, 37,725 props over 1,151 games. Results in `test_c_results.json`.

- best eval joint: `teammate 3PM:under x 3PM:under` = **0.2942**,
  95% cluster-bootstrap CI **[0.2818, 0.3068]**, 1,116 games
- **0 of 12** pair types have a 95% CI *upper* bound that even reaches 0.3333
- shortfall at the best point estimate: **3.9 percentage points**
- dev→eval ranks are stable; no pair type moved more than ~0.012 in joint

Correlation, separated from base rates: **4 of 12** have a significantly non-zero `lift`.
Signs are consistent across dev and eval for the two largest:
`teammate 3PM:under x Assists:under` (+0.0143 dev, +0.0117 eval, CI [+0.0087, +0.0149])
and `teammate Assists:under x Points:under` (+0.0133 dev, +0.0095 eval, CI [+0.0071, +0.0120]).
`opposing 3PM:under x 3PM:under` is reliably *negative* (-0.0044, CI [-0.0081, -0.0007]).

So same-game leg correlation **exists and is real but is ~1 percentage point**, an order
of magnitude too small to matter. The binding constraint is the marginal, not the
correlation: single-leg UNDER hit rates on 2025-26 real lines are Points 0.5171
[0.5088, 0.5253], Rebounds 0.5226 [0.5129, 0.5324], Assists 0.5190 [0.5074, 0.5306],
3PM 0.5443 [0.5344, 0.5541] — every CI far below the 0.5774 single-leg break-even.

**TEST C: NO PASS.** Recorded as final. FanDuel lines, hypothesis for Underdog.

---

## Frozen 2026-27 NBA model (frozen 2026-10-18, before 2026-10-20 opening night)

**No model changes until the December 2026 check.** `ml_trainer.train_model()` refuses
to run without `force=True`, and startup never retrains NBA models: new Kaggle versions
still load box scores and resolve predictions, but the model files below do not change.
WNBA models are outside this freeze.

### What is frozen

| | |
| --- | --- |
| Code commit | `1313d9125f3811322864765476c556fc097bf49d` |
| Training data | Kaggle `eoinamoore/historical-nba-data-and-player-box-scores` **v519**; `player_stats` 840,793 rows; real lines from `historical_player_props` (FanDuel/DraftKings, resolved by `real_lines.py`) |
| Environment | Python 3.14.4, xgboost 3.2.0, scikit-learn 1.8.0, pandas 2.3.3, numpy 2.4.4 |
| Game types | `game_types.py`: kept Regular Season, NBA Emirates Cup (2023 group/QF/SF), Playoffs, Play-in; excluded Preseason (48,082 rows), All-Star (794), Emirates NBA Cup final (18). `is_playoff` = Playoffs only. |
| Fit | 100% of rows per stat; missing opponent def rating filled with the all-dates mean (105.56) |

| model | type | training rows | lines | SHA-256 |
| --- | --- | --- | --- | --- |
| `model_Points.pkl` | LR | 29,836 | real | `50a26f530ef7116b7431d982da1c1f4523294a043034268d61ecc7fd5070a8aa` |
| `model_Rebounds.pkl` | LR | 26,970 | real | `71d16a7239c7f6f7147b8484d67ccf9daa68a08fd8d737cc264394cceba05ee6` |
| `model_Assists.pkl` | XGBoost | 17,557 | real | `19b0ac96bedae5b09fa6421e907cf7d3264cd1bb30ff73a2abd57353ae7cbb74` |
| `model_3PM.pkl` | LR | 21,465 | real | `b5e94bb660619f9d316f8a270dda66b7781abc6c9d6efa2456d9cf250208bcd8` |
| `model_PRA.pkl` | XGBoost | 752,661 | simulated | `59f3900af84889d2f375c5f5c2bb3444167eaabf22cf1ddcfcea156cf1b4ced1` |
| `model_PR.pkl` | XGBoost | 752,661 | simulated | `10c195e349ea014156d630622a31d092b1fbe137bd905c8b1b52f7e13afb8208` |
| `model_PA.pkl` | XGBoost | 752,661 | simulated | `c92b2b98a229ff61aef0b30ab493167feadcd0544bdfe6d0337f4d4a573b8520` |
| `model_RA.pkl` | XGBoost | 752,661 | simulated | `0c554470399176e587a66cc16a0327040e94c21c215e21ed2172ed767d6a1ad5` |

LR = `StandardScaler` + `LogisticRegression(C=1.0, max_iter=2000)`. XGBoost =
`n_estimators=200, max_depth=4, learning_rate=0.05, random_state=42`. Neither was tuned.

**Reproducible:** a second refit at this commit on the v519 database produced
byte-identical files for all 8 models. To check: `sha256sum backend/model_*.pkl`. To
regenerate (only for verification): at commit `1313d91`, with `player_stats` at v519,
`PYTHONIOENCODING=utf-8 python -c "import ml_trainer as M; M.train_model(force=True)"`.
A later ETL replaces `player_stats`, after which a refit will no longer match.

### How the model type was chosen (rule fixed before running)

`experiments/model_selection.py`; results in `experiments/results_model_selection.json`.
Temporal 70/15/15 on calendar-day boundaries; per stat, higher **validation** AUC wins
(exact tie → LR); the winner alone was scored **once** on test. Monthly walk-forward
(2024-10 .. 2026-05) is reported alongside and did not override the rule.

| stat | val AUC XGB / LR | winner | test AUC [95% CI] | walk-forward pooled XGB / LR |
| --- | --- | --- | --- | --- |
| Points | 0.514 / 0.527 | LR | 0.522 [0.505, 0.539] | 0.505 / 0.511 |
| Rebounds | 0.527 / 0.531 | LR | 0.533 [0.515, 0.549] | 0.532 / 0.534 |
| Assists | 0.531 / 0.526 | XGBoost | 0.508 [0.487, 0.531] | 0.514 / 0.531 |
| 3PM | 0.535 / 0.568 | LR | 0.556 [0.536, 0.577] | 0.544 / 0.558 |
| PRA/PR/PA/RA | XGB ahead by ~0.004-0.006 | XGBoost | 0.732-0.747 | XGB ahead in 15-16 of 16 months |

Assists: the rule picked XGBoost on validation; test and walk-forward favour LR. The
rule was kept as declared (no deviation). Combo numbers are on simulated lines and say
nothing about real-line performance. On all four real-line stats, XGBoost's log loss is
worse than predicting the base rate; LR's is at or slightly better.

### Train/inference parity

- `hit_rate_last10` and `vs_opponent_avg` are defined once in `feature_defs.py` and used
  by both training and `predictor.py`. Training frame bit-identical before/after the
  refactor; `experiments/feature_parity.py` matches 20/20 historical rows exactly
  (also `avg_pts_last10`).
- Game types and `is_playoff` are applied identically at inference.
- **Not verified identical** (inference still reads the last 40 counted games):
  `games_played_last15`, `fatigue_score`, `minutes_trend`, `avg_minutes_last5`,
  `home_away_split`, `opp_def_rating_last5`. Definitions are close but were not shown
  to match exactly; check at the December review.

### December check

Score frozen-model predictions logged from 2026-10-20 against results, on real Underdog
lines. Anything learned before then is recorded, not acted on.

---

## Amendment: December and season-end criteria

Recorded on the commit date of this entry (drafted 2026-10-19), before any 2026-27 result is seen: paper_predictions has 0 rows and line_snapshots has no Underdog rows. Earlier sections are unchanged. Repo facts cited here are in backend/experiments/research_review_context.md, committed with this entry.

### 1. Payout and break-even (correction of fact)

Earlier sections assumed a 3x two-pick payout (single leg 0.5774, two legs 0.3333). Underdog's help article "Pick'em Standard & Flex Entry Payouts" (Classic Pick'em section, read 2026-10-19) lists a 2-pick standard entry at 3.5x for 1.0x selections. It also states that per-pick multipliers and correlated projections change the payout.

- On 2026-10-20, before any result is joined, record the 2-pick standard multiplier M shown in the app for this account, with a screenshot saved in backend/experiments/.
- Single-leg break-even BE = sqrt(1/M). At M = 3.5, BE = 0.5345.
- Also on 2026-10-20, record whether line_snapshots.price is present for bookmaker 'underdog' and whether it varies across picks.
  - If it varies, write down the mapping from price to payout multiplier using prices only, before the first check, and grade each pick against its own break-even.
  - If it is absent or constant, grade every pick at BE and record that per-pick multipliers were unobserved.
- If no Underdog Points lines have been logged by 2026-11-01, this amendment is replaced by a new dated entry written before any result is joined.
- Test C, exploratory addendum only: re-score the 12 frozen candidates' eval rows against 1/M with the existing Bonferroni 99.583% intervals. Underdog changes payouts for correlated picks, so the true break-even for same-game pairs stays unknown. The Test C conclusion is not reopened.

### 2. Confirmatory hypotheses

Population: paper_predictions rows with app_path = 'model', Underdog line present, and result OVER or UNDER (PUSH and DNP dropped).

Regular season is decided by matching each row to `schedule` on ET game date plus home and away teams, and keeping games whose gameId type digit is 2 (game_types.py:40-46). Rows with any other digit are excluded, including the Cup final (digit 6). Unmatched rows are excluded, and their count is reported.

- H1 (model)
  - Side = Over if P(over) > 0.5, else Under. Confidence = max(P, 1 - P).
  - Slice = top 20% of population rows by confidence. The cutoff is computed from P alone.
- H2 (market)
  - Population rows where FanDuel or DraftKings posts the same line number as Underdog, with both sides priced, in the same pre-tip pull (paper_predictions.py:162-164).
  - Fair P(side) = Shin-devigged probability, averaged over the books available. Side = the side with higher fair P.
  - Slice = top 20% by fair P.
  - If per-pick multipliers are unobserved (section 1), a positive H2 is reported as unconfirmed, because Underdog may pay less on exactly the picks H2 selects.
- Test: hit rate in the slice > BE.
  - Family-wise alpha = 0.05 one-sided, Bonferroni: 0.025 one-sided per hypothesis.
  - An edge is declared only if the game-clustered bootstrap one-sided 97.5% lower bound exceeds BE.

### 3. Timing and power

- Interim (first check on or after 2026-12-15): futility only. H1 or H2 is stopped if its two-sided 95% upper bound is below BE. No edge can be declared at the interim.
- Final: after the last regular-season game. Only the final can declare an edge.
- Power (80%, one-sided 0.025):
  - Detecting +2.5 points over BE needs about 3,116 picks per slice; +4 points needs about 1,214.
  - Expected slice size is about 2,500 rows before the app_path filter (10.3 FanDuel Points lines per game historically, times about 1,230 regular-season games, times 20%; Underdog's count may differ). At that size, the smallest detectable difference is about +2.8 points, and about +3.1 at 2,000 rows, before game clustering.
  - Only edges of roughly +3 points or more are realistically detectable this season. A smaller true edge will most likely end as inconclusive, which is reported as inconclusive, not as "no edge".

### 4. Exploratory (reported, never decisive)

- Slice hit rate minus always-Under on the same rows.
- Closing line: share of H1 picks, recomputed at the 10:00 Underdog line, where the pre-tip line moved toward the pick (rows with movement only).
  - Only line_diff is recomputed. It is the only feature that reads the line (ml_trainer.py:257, predictor.py:236).
  - The other 13 features are line-independent: inference predictor.py:221-239 and 241-286; training ml_trainer.py:153-206, 230-238, 256, 259 and 261-291.
- Test A: calibration (Brier, reliability bins) of Shin-devigged FanDuel/DraftKings P(over) at the 10:00 and pre-tip snapshots.
- News timing: after a play/no-play change for a teammate, how often and how fast the Underdog line moved before pre-tip.
- Early-season subgroup: not tested. Historical FanDuel/DraftKings Points rows (already seen) gave an October over rate of 0.4846 against 0.4856 overall.

### 5. Feature ablation (Points only; one Holm family; evaluated once, at the final)

Each variant is a logistic regression with the same configuration as model_Points.pkl. It is fit on the same training population (Kaggle version 519, the 29,836 Points rows) and refit on 100% of that data, as the frozen models were.

Variants:

1. 14 features + over_rate_vs_line_last20
   - Share of prior counted games, up to the last 20, whose stat exceeds the line of the game being predicted (that one line is applied to every earlier game).
   - Shifted so the current game is excluded; min_periods = 10.
2. 14 features with line_diff replaced by (median_last10 - line)
   - median_last10 = shift(1).rolling(10, min_periods=10).median().
3. 14 features + rest_days
   - Days between this and the previous counted game (ET dates), capped at 4. Capped at 4 when there is no previous game.

Fitting rules:

- Variant files are fit and their SHA-256 hashes recorded in a new dated entry before any scoring.
- Any fitting choice not specified here is recorded in that entry as a deviation.

Scoring:

- On the section 2 population, compare against the frozen model's logged P(over).
- New features are computed from box scores dated before each game.
- Metric: paired log-loss difference (variant minus frozen) on identical rows.
- One-sided test that the variant is lower, using game-clustered bootstrap p-values. Holm across the three variants at family-wise alpha = 0.05.

teammate_out is excluded. Historically it can only be built from box scores, which reflect late scratches the pre-game report did not.

### 6. Other definitions

- Parity: for each of the six unverified features, the value logged in paper_predictions.features_json at prediction time is compared with the training-code value recomputed from box scores.
  - This runs only after the ETL has loaded the 2026-27 box scores for those games.
  - Pass if |diff| < 1e-9 on 20 resolved rows drawn with seed 20261018.
- hit_rate_last10 counts games above the player's own career-to-date mean, not the prop line. It is never reported as a line hit rate.

---

## Entry: Underdog market, logging change, H2 clarification (drafted 2026-10-19)

Recorded on the commit date of this entry, before any 2026-10-20 result was seen: paper_predictions had 0 rows and no 2026-10-20 game had started. Earlier sections are unchanged.

### Probe finding

One OddsAPI event-odds call on 2026-10-20T01:43Z (game 26b036ff107f3c658258eaf4a6f26228, Boston Celtics @ Detroit Pistons), bookmakers=underdog,fanduel,draftkings, markets=player_points,player_points_alternate. Raw response: backend/experiments/probe_20261020T014326Z.json.

- Underdog's Points lines were returned only in player_points_alternate, never in player_points: 17 players, exactly one line each, 15 two-sided and 2 Over-only.
- FanDuel and DraftKings appeared in both markets. Their alternate markets are ladders:
  - FanDuel: 2-29 points per player, with repeated player/side/point outcomes at different prices. For 13 of its 14 main lines, the alternate market repeats the main point with the same Over and Under prices.
  - DraftKings: 5-10 points per player, Over only.
- The logger requested player_points only, so it would have stored no Underdog rows.

### Logging change

- line_logger requests markets=player_points,player_points_alternate with 10 named bookmakers: underdog, fanduel, draftkings, prizepicks, novig, prophetx, kalshi, betmgm, espnbet, betrivers.
  - Up to 10 named books count as one region-equivalent, so the expected cost is 2 credits per pull.
  - A pull whose x-requests-last is not 2 prints a warning.
- Every outcome from every book returned is stored under its own market key.
- The line_snapshots key now includes point and the outcome's position in its market, because under the old key alternate rungs collapsed to one row per player and side. The 18 earlier rows were migrated; the old table is kept as line_snapshots_pre_alt_key.
- paper_predictions reads only Underdog, FanDuel and DraftKings:
  - The Underdog line and prices come from player_points_alternate.
  - fd_* and dk_* remain the FanDuel/DraftKings player_points main line.
  - If an Underdog player ever has more than one line in a pull, the row is recorded with no line and no prediction.
- The seven additional books (prizepicks, novig, prophetx, kalshi, betmgm, espnbet, betrivers) are logged for exploratory use only. H2 is unchanged: it uses FanDuel and DraftKings only.
- The NBA slip path no longer calls OddsAPI. It serves today's logged Underdog lines, then today's cached lines, then manual entry.

### 10:00 pull dropped

- The 10:00 ET open pull is turned off. One pull per game remains, at T-30 with one retry on failure; event-triggered pulls stay off.
- Projected peak month: 239 games x 2 credits = 478 against the 500 cap.
- Section 4 analyses that needed the 10:00 snapshot are dropped:
  - "Closing line" (H1 picks recomputed at the 10:00 Underdog line).
  - Test A's 10:00 snapshot: Test A is computed at the pre-tip snapshot only.
  - "News timing": with no snapshot before pre-tip and event pulls off, Underdog line movement before pre-tip is not observed.

### Price-to-payout mapping still open

- OddsAPI returns American-odds prices for Underdog (29 distinct values in the probe). Over plus Under implied probabilities sum to 1.184-1.219 on two-sided lines, versus about 1.06-1.07 for FanDuel and DraftKings main lines.
- These prices are not the in-app multipliers. In-app screenshots from the same evening as the probe (screenshots kept locally, not committed (they show account details)):
  - Jayson Tatum Higher/Lower 24.5: in-app 1.57x/2.25x; OddsAPI -259/+101 (decimal 1.39/2.01).
  - Cade Cunningham Higher/Lower 24.5: in-app 1.90x/1.81x; OddsAPI -122/-174 (decimal 1.82/1.57).
- The same screenshots show 2-pick Standard entries paying 2.7x (LeBron James Higher 14.5 + Jaylen Brown Higher 19.5) and 2.78x (Jayson Tatum Higher 24.5 + Shai Gilgeous-Alexander Higher 29.5).
- OddsAPI's documentation notes that DFS odds "are indicative only" because they vary with user selections.
- The price-to-payout mapping that section 1 requires before the first check is therefore not established. Until it is written down in a dated entry, section 1's "absent or constant" branch does not apply, and no pick is graded against a per-pick break-even.

### H2 clarification

These rules define H2's fair probability (section 2). They were fixed on 2026-10-19, before any 2026-27 result existed. Everything else in H2 is unchanged.

a) Per book (fanduel, draftkings only): if the book's player_points line equals ud_point, use that market; otherwise use its player_points_alternate outcomes at ud_point.
b) If a side has more than one price at that point in the chosen market, convert each to implied probability and average them.
c) The book qualifies only if both sides are priced at that point (DraftKings' Over-only alternate rungs do not qualify).
d) Devig each qualifying book with Shin's method; fair P(over) = the average across qualifying books.
e) If no book qualifies, return None. Such rows are excluded from H2 and counted.

Implementation: `h2_fair_prob(rows, ud_point)` in backend/experiments/h2_rules.py, not wired into anything. Choices the rules leave open, fixed here:

- "The book's player_points line equals ud_point" means the book has at least one player_points outcome at ud_point. Points compare with tolerance 1e-9.
- Shin's z is solved by bisection so that the two probabilities sum to 1.
- If a qualifying book's implied probabilities sum to 1 or less, Shin's z is 0 and the probabilities are normalized proportionally. This case is outside rules a)-e) and is flagged when it occurs.

On the probe (line availability only, no results), 13 of the 17 Underdog lines qualified and 4 returned None.

### Known issue: Ron Holland

In an offline run on the probe, the OddsAPI name "Ron Holland" matched no player in player_stats, so paper_predictions records "unmatched player" and no prediction. This is a known prediction-side issue; the fix will be decided later.

No 2026-10-20 result had been seen when this entry was written.

---

## Entry: Underdog lines in player_points too (drafted 2026-10-19)

- The opening-day check pull (2026-10-20T03:58Z, game 26b036ff107f3c658258eaf4a6f26228) returned Underdog Points lines in both markets:
  - 30 rows (16 players) in player_points_alternate;
  - 2 rows (Cade Cunningham, 24.5, Over/Under -137) in player_points.
  No player appeared in both markets.
- Paper predictions (and the slip fallback) now read Underdog Points lines from both player_points and player_points_alternate. A new column, ud_market, records which market each line came from.
- The existing rule is unchanged and now applies across both markets: if a player has more than one distinct Underdog point in a pull, the row is recorded with no line and no prediction.
- If the same point appears in both markets with different prices for a side, that side's Underdog price is recorded as NULL.

Recorded before any 2026-10-20 result was seen: paper_predictions had 0 rows.

---

## Entry: four-stat logging and the 10:00 pull restored (drafted 2026-10-20)

Four-stat logging began with the 10:00 ET open pulls, caught up at 11:25 ET on 2026-10-20, before this entry was committed and before any 2026-27 result existed. paper_predictions had 0 rows when four-stat logging began. Earlier sections are unchanged.

### Scope change

- The OddsAPI plan is now 20,000 credits per month. ODDS_MONTHLY_CREDIT_CAP = 18000.
- line_logger requests 8 markets in its one call per event, with the same 10 named bookmakers: player_points, player_rebounds, player_assists, player_threes and the _alternate version of each.
  - Keys confirmed by one event-markets call (experiments/market_keys_probe.py, 2026-10-20T15:26Z, game 26b036ff107f3c658258eaf4a6f26228, Boston Celtics @ Detroit Pistons; raw response experiments/probe_markets_20261020T152625Z.json):
    - underdog listed all 8 keys except player_assists: its Assists lines are only in player_assists_alternate.
    - fanduel and draftkings listed all 8 keys.
  - OddsAPI bills only the markets returned, so a pull costs at most 8 credits. A pull whose x-requests-last is above 8 prints a warning.
- Projection at the maximum cost, 2 pulls per game x 8 credits = 16 credits per game:
  - peak month January 2027: 236 games, 3,776 credits;
  - the 1,200 regular-season games in `schedule`: 19,200 credits over the season.
- paper_predictions logs one row per Underdog line per stat (Points, Rebounds, Assists, 3PM), each with that stat's frozen model from "Frozen 2026-27 NBA model". Each row records the model file's SHA-256 and whether it matches the hash recorded there.
- Each stat uses the same line rules as Points:
  - the Underdog line comes from the stat's main and _alternate markets;
  - more than one distinct Underdog point in a pull means the row gets no line and no prediction;
  - a side priced differently in the two markets gets a NULL price;
  - fd_* and dk_* use the stat's main market.
- Rows are resolved against the stat's own box-score column (points, reboundsTotal, assists, threePointersMade) in a new column, actual_value. actual_points is still filled for Points rows.
- The paper_predictions unique key was (game_id, player_name, captured_at). Under that key, every stat after the first for a player in a pull would have been silently dropped. The key is now (game_id, player_name, stat_type, captured_at).
  - The table was copied to a new table in one transaction, and row counts were checked before commit. It held 0 rows at the time.
  - The old table is kept as paper_predictions_pre_stat_key.
- The slip fallback serves logged Underdog lines for all four stats. This does not affect any test.

### H1 and H2 are unchanged and Points-only

- H1 and H2 stay as written in "Amendment: December and season-end criteria" and the H2 clarification. They keep the same family (alpha 0.05 one-sided, Bonferroni 0.025 each), timing, power and grading.
- Their population is paper_predictions rows with stat_type = 'Points'. This only states what was already true, since no row of any other stat existed before this entry. Rows for other stats never enter H1 or H2.
- Section 5 (feature ablation) stays Points-only.

### 10:00 pull restored

- From 2026-10-20, a 10:00 ET open pull runs for every game on the slate, in addition to the T-30 pre-tip pull. Pre-tip pulls are scheduled independently of the open pull. Event-triggered pulls stay off.
- After a restart, the open pull is caught up only before 13:00 ET. Each snapshot's actual time is in captured_at. On 2026-10-20 the open snapshots were taken at 11:25 ET.
- The three section 4 analyses dropped in the 2026-10-19 entry return, as exploratory only:
  - Closing line: share of H1 picks, recomputed at the 10:00 Underdog line, where the pre-tip line moved toward the pick.
  - Test A at the 10:00 snapshot, alongside the pre-tip snapshot.
  - News timing: Underdog line movement between the 10:00 and pre-tip snapshots after a teammate's play/no-play change. Movement before 10:00 or between pulls is not observed.
- Games with no open snapshot (a missed or refused pull) are excluded from these three analyses, and their count is reported.

### Other stats are exploratory

- Rebounds, Assists and 3PM rows are reported but never decide anything. No edge is declared from them, and they are not in H1/H2's family. The only exception is H3 below.
- They are reported with the same statistics as section 4 (slice hit rate, always-Under on the same rows), labelled exploratory.

### H3 — 3PM model

- H3 mirrors H1 on 3PM:
  - Population: as section 2, with stat_type = '3PM'. That means app_path = 'model', Underdog line present, result OVER or UNDER, and regular-season games only, matched the same way.
  - Side = Over if P(over) > 0.5, else Under. Confidence = max(P, 1 - P).
  - Slice = top 20% of population rows by confidence. The cutoff is computed from P alone.
- H3 is its own family, separate from H1/H2: one-sided alpha 0.05, with no correction against H1/H2.
- An edge is declared only if the game-clustered bootstrap one-sided 95% lower bound clears break-even.
- H3 is graded exactly as H1 is graded under the payout-mapping entry.
- Timing is the same as H1. The interim (first check on or after 2026-12-15) is futility only (two-sided 95% upper bound below break-even), and only the final can declare an edge.
- Why 3PM: it is the only stat whose frozen model was clearly above chance on held-out data before this season (validation AUC 0.568, test AUC 0.556 [0.536, 0.577]; "Frozen 2026-27 NBA model"). The choice was made before any 2026-27 3PM result existed.
- Sample size is unknown until Underdog's 3PM line count is seen. An inconclusive result is reported as inconclusive, not as "no edge".
- If Underdog posts no 3PM lines (Test C recorded that Underdog did not carry 3PM), H3 is withdrawn by a dated entry before any 3PM result is joined.

Recorded before any 2026-10-20 result was joined. paper_predictions had 0 rows when four-stat logging began.

---

## Entry: current rosters and 0-minute games in paper-prediction inputs (drafted 2026-10-20)

Recorded on 2026-10-20 at about 12:45 ET, before any 2026-27 result existed: the first game (Boston Celtics @ Detroit Pistons) tips at 15:00 ET, player_stats holds no box score after 2026-10-16, and paper_predictions had 0 rows. Earlier sections are unchanged.

### What changes

1. Player team from current rosters.
   - A new table, rosters, holds ESPN's public team rosters for all 30 teams (site.api.espn.com .../nba/teams/{id}/roster; no OddsAPI credits). It is refreshed at backend startup and in the 07:00 ET maintenance job.
   - A player's current team is his roster team, matched by name (case, accents, punctuation and Jr./II-style suffixes ignored). It falls back to the team of his latest box score when he is on no roster, the name matches more than one team, or the roster rows are older than 48 hours.
   - paper_predictions records which source was used in a new column, team_source ('roster' / 'box_score').
   - The opponent and home/away still come from the OddsAPI event, by matching the player's team to its home or away team.
2. get_game_logs drops 0-minute games (numMinutes > 0), as ml_trainer's training rows do.
   - This changes, for players with a 0-minute counted game in their last 40, the features read from that log: avg_pts_last5, avg_pts_last10, trend, line_diff, consistency, games_played_last15, minutes_trend, avg_minutes_last5 and fatigue_score, and counted_games / app_path.
   - Since 2025-10-01 three players have such a game (Kelly Olynyk, Tony Bradley, Nick Richards).

### When

- Changes 1 and 2 apply to paper predictions with game_date 2026-10-21 or later.
- Rows with game_date 2026-10-20 stay as logged by the code running that day: player team from the latest box score, 0-minute games included.

### Effect on H1, H2 and H3

- Rows previously logged as "team mismatch" (the box-score team is neither event team, e.g. a player traded since his last box score) are now predicted. This changes H1's population: it gains those rows.
  - In a dry run on the 2026-10-20 10:00 pulls, one player changed: Anfernee Simons (box score Chicago Bulls, roster Philadelphia 76ers), 2 rows that were "team mismatch" are now predicted. Every other row's P(over) was identical with rosters on or off.
- A player whose stale box-score team is the other team in the same game would previously have been assigned the wrong opponent and home/away (vs_opponent_avg, opp_def_rating_last5, home_away_split). That is corrected from 2026-10-21. None occurred on 2026-10-20.
- Everything else in H1, H2 and H3 is unchanged.

### Recorded for completeness (no effect on the 14 features)

- The next-game lookup compared UTC time against ET schedule times, so from 4 hours before tip it returned the team's following game. Fixed. For paper predictions this only changes the is_back_to_back column, which is not a model input; rows with game_date 2026-10-20 describe the following game in that column.
- features_json gains two keys that are not model inputs: line_hits_last10 and games_last10 (used for the app's explanation text).
- A player whose latest box score has no team id, and who is on no roster, is recorded as "no team context" instead of an error.
- Not changed: home_away_split and opp_def_rating_last5 at inference still read 0-minute rows, which training excludes. Both are among the six features listed as not verified identical in "Frozen 2026-27 NBA model".

## Entry: Test A on backfilled historical lines (2026-10-22)

Recorded before any backfilled line is downloaded: historical_snapshots does not exist and
no historical price has been seen. Earlier sections are unchanged.

### Scope

- Only Test A is run on backfilled data.
- Tests B (cross-book disagreement) and D (market-baseline residual) are named in Step 0
  and logging_plan.md but have no hypothesis, metric, threshold or n anywhere in the repo.
  They remain undefined. Each gets its own dated entry before any backfilled line is
  joined to a result for it.
- The frozen 2026-27 models are not evaluated on backfilled games. Seasons 2023-24,
  2024-25 and 2025-26 are in their training data. No backfilled row enters H1, H2, H3,
  the section 5 feature ablation, or the December check. No backfilled row is used to
  fit or tune any model. Evaluating any model on backfilled lines (including the planned
  challenger's one-shot historical test) requires its own dated entry before any of its
  results are computed.
- Test A stays exploratory, as in section 4 of "Amendment: December and season-end
  criteria": reported, never decisive. No edge is declared from it. Within Test A, the
  Points analysis is primary (reported first and in full); Rebounds, Assists and 3PM are
  secondary.

### Data

- Source: OddsAPI historical event odds (backend/backfill.py), one snapshot per game at
  T-30 minutes before the event's commence_time (the closest snapshot at or before that
  time), stored in historical_snapshots with phase = 'backfill'. The same 10 named
  bookmakers as the live logger, includeMultipliers=true.
- Each row records its snapshot time. Rows whose snapshot is more than 60 minutes before
  T-30 are excluded and counted.
- Seasons: 2025-26, 2024-25, 2023-24, newest first, as far as the credit budget reaches.
  The games actually pulled are listed by game id in a dated entry before any result is
  joined. If the budget stops partway through a season, that season is reported as partial.
- Game types: as game_types.py (Regular Season, NBA Emirates Cup 2023 group/QF/SF,
  Playoffs, Play-in). Preseason, All-Star and the Cup final are excluded.
- Excluded: the two stage 3 probe games, chosen before any historical line was seen:
  Kaggle gameId 22500580 (Boston Celtics @ Miami Heat, 2026-01-15 19:30 ET) and
  gameId 22300571 (San Antonio Spurs @ Boston Celtics, 2024-01-17 19:30 ET).
- Results: the box score's own stat column (points, reboundsTotal, assists,
  threePointersMade), matched as real_lines.py matches props to games.
- Unmatched prop rows are counted and reported by reason. The matching code may be fixed
  before any result is joined; the commit used is recorded in the dated entry that lists
  the games pulled.

### Definition

Section 4 defines Test A as calibration (Brier, reliability bins) of Shin-devigged
FanDuel/DraftKings P(over) at the pre-tip snapshot. Fixed here:

- Primary: Points, main market (player_points). Secondary, exploratory: Rebounds,
  Assists and 3PM, main markets (player_rebounds, player_assists, player_threes).
  Alternate ladders are not used.
- Line: both sides priced at the same point in the book's main market. A book posting
  more than one main point for a player in the snapshot is excluded for that player
  and counted.
- Devig: Shin's method per book, with z solved by bisection, as in the H2 clarification.
  If the implied probabilities sum to 1 or less, z = 0 and they are normalized
  proportionally; such rows are flagged and counted.
- Unit: one row per (game, player, stat, book). FanDuel and DraftKings are scored
  separately; they are never averaged or pooled.
- Outcome: Over hits iff actual > line. DNP (0 or NULL minutes) and pushes are dropped
  and counted. Players with no box-score row for the game are dropped and counted
  separately.
- Metrics, per stat and per book:
  - Brier score of fair P(over) against the outcome;
  - reliability table over 10 equal-width bins of fair P(over) on [0, 1] (the last bin
    includes 1.0), with each bin's mean fair P, observed over rate, row count and
    game count;
  - reference: the Brier score of the constant base rate on the same rows.
- Uncertainty: game-clustered bootstrap over game ids, 2,000 resamples, seed 20261022,
  percentile 95% intervals for the Brier score and each bin's observed over rate.
- No pass threshold. The result describes how well the devigged market is calibrated
  and is labelled "FanDuel/DraftKings historical lines".

### Underdog in backfilled data

- Whether OddsAPI's historical feed carries Underdog lines in each season is unknown
  until the stage 3 pull. Underdog rows from the backfill are stored but enter no test
  in this entry.

No backfilled line had been downloaded when this entry was written.

## Entry: backfill probe exclusion and Test A timing (2026-10-22)

- Probe game 22400562 (Boston Celtics @ Toronto Raptors, 2025-01-15) was pulled on 2026-10-22 (player_points only) to check whether OddsAPI carries Underdog in 2024-25. It is excluded from Test A, like the two Stage 3 probes.
- Test A is computed once, after the full backfill planned for the November 2026 billing cycle is complete and its game list is recorded in a dated entry. No Test A result is computed on partial backfills, including the 183 games pulled in October 2026 (backend/experiments/backfill_games_2026-10.md).

## Entry: Kaggle data integrity, NULL minutes (2026-10-23)
- Kaggle v523 blanked numMinutes for all 27 rows of game 22600088 (Bulls at Raptors, 2026-10-21); v522 had them. The feature path drops games with NULL minutes, so these players' rolling features would silently omit a game they played.
- One-off: before the first affected pre-tip pull on 2026-10-23, numMinutes for those 27 rows is restored from the v522 backup (backups/predictions_2026-10-23.db). No other column is changed. No paper prediction for these players had been logged since v523 was loaded.
- Going forward (ETL): when a new Kaggle version has NULL numMinutes where the previously loaded version had a value for the same (gameId, personId), the previous value is kept and a warning is logged.
- Grading (paper and app): a row is DNP only if minutes are 0/NULL and all counting stats (points, rebounds, assists, 3PM, blocks, steals, turnovers) are 0/NULL. NULL minutes with any counting stat counts as played. No paper row has been graded under the old rule with NULL minutes; all 53 rows for 22600088 were graded under v522.
- Frozen models, hypotheses and populations are unchanged.

## Entry: inference parity, data freshness and pull timing (2026-10-23)

Recorded on 2026-10-23 at about 13:45 ET. paper_predictions held 664 rows with status 'ok' (game dates 2026-10-20 to 2026-10-22). Nothing in this entry was computed from a result: every number below comes from features_json and p_over, never actual_value or result. Earlier sections are unchanged.

### 1. Inference features made identical to training
- At inference, four of the 14 features were computed differently from ml_trainer.build_training_data:
  - games_played_last15: counted games within 15 days of the player's most recent logged game, that game included. Training counts games in the 15 days before the target game (`rolling("15D", closed="left")`).
  - fatigue_score: minutes within 7 days of the most recent logged game, that game excluded. Training sums minutes in the 7 days before the target game (`rolling("7D", closed="left")`).
  - home_away_split: career home and away averages that include 0-minute rows, which training excludes.
  - opp_def_rating_last5: the opponent's last 5 games, 0-minute rows included. Training uses the mean over rows with numMinutes > 0 of the opponent's 5 games before the target game; a missing value is 105.56.
- From game_date **2026-10-24**, all four are computed at inference exactly as in training, over counted games with numMinutes > 0 that start before the game's commence_time.
  - If the change is not deployed before the first 2026-10-24 pre-tip pull (17:30 ET), the effective date is the first game_date whose pre-tip pulls all ran on the new code. A dated addendum records it before any of those rows' results are joined.
- v2 is deployed only after experiments/feature_parity.py shows each of the four features at inference equal to the training frame on historical rows. Any feature that fails stays on its v1 definition; the addendum names it.
- Each row also keeps the old-definition features and P(over) in shadow columns (features_v1_json, p_over_v1). feature_def records 'v1' or 'v2'.
- The frozen model files, ml_trainer.py and the training definitions do not change.
- No result for any game_date had been viewed when this entry was written.
- **Analysis:**
  - Primary analyses (H1, H2, H3 and section 4) use every row's p_over as logged: v1 before the effective date, v2 from it. Population rules are unchanged.
  - Sensitivity: the same analyses restricted to game_date on or after the effective date. Reported next to the primary, with no separate decision rule.
- **Measured on the 664 'ok' rows (all app_path = 'model'):**
  - Number of rows whose feature differs: games_played_last15 664, fatigue_score 662, home_away_split 550, opp_def_rating_last5 478.
  - Re-scored with all four corrected, P(over) changes on every row:
    - Points: 14 of 193 change side; largest change in P(over) 0.011.
    - 3PM: 8 of 174; 0.031.
    - Rebounds: 32 of 180; 0.026.
    - Assists: 25 of 117; 0.170.
  - H1 top-20% confidence slice (Points): 5 of 39 rows leave and 5 join.
  - H3 top-20% confidence slice (3PM): 7 of 35 leave and 7 join.
  - These slices are computed over app_path = 'model' rows. The result and regular-season filters were not applied, because applying them would mean reading results.

### 2. Data freshness
- New columns on paper_predictions:
  - data_as_of: the newest counted-game date in player_stats when the row was computed;
  - data_stale = 1 when data_as_of is before the previous slate date (the latest schedule date before game_date).
- Existing rows have data_as_of NULL. From the Kaggle load times (kagglehub cache: v521 at 2026-10-21 15:57 ET, v522 at 2026-10-22 14:20 ET), every existing row was computed after the previous slate's box scores were loaded. The 2026-10-20 rows had no previous regular-season slate.
- **Analysis:** primary analyses include data_stale rows as logged. A sensitivity analysis excludes them, and their count is reported.
- **Prevention:**
  - The Kaggle retry no longer stops for the day when the 90/60-minute guards close. It skips that check and tries again an hour later, and stops only when the new version is loaded, the ETL fails, or the day ends.
  - A 07:00 maintenance missed while the backend was down or asleep is caught up on startup.

### 3. Pre-tip timing
- From 2026-10-23, pre-tip jobs run one at a time in the backend. When several games share a tip time, later games' pulls land after T−30: about 30 s per earlier game, about 2 minutes for the fifth game in a group of five.
- Each row's actual time is in captured_at. A job that reaches a game after its commence_time makes no pull. Rows are not otherwise treated differently.

### 4. Games with no pre-tip pull on 2026-10-20
- Philadelphia 76ers @ New York Knicks and Oklahoma City Thunder @ San Antonio Spurs have no pre-tip pull and no paper rows, because the backend was not running at their pre-tip times. Both have 10:00 open snapshots.
- They are absent from H1, H2, H3, section 4, and the open-vs-pre-tip analyses, which need both snapshots.

## Entry: sync timing (2026-10-24)
- From commit e29cf56, the ETL may run at any time except within 10 minutes of a scheduled pull or while a pre-tip job runs (pre-tip lock); the 90-minute pre-tip and 60-minute pull guards in the data-freshness entry no longer apply. Tables are swapped atomically, so a pre-tip job sees either the previous or the new box scores, never a partial load.
- Consequence: paper rows within one slate may be built on different Kaggle versions. Each row's data_as_of records which. Analysis rules are unchanged.
- Sync also runs at backend startup and on demand (Sync now).
