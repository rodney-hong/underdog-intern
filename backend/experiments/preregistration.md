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
