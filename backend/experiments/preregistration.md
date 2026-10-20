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
