# Jev vs. real-line XGBoost

Standalone experiment. Nothing here imports-and-mutates or writes to
`ml_trainer.py`, `predictor.py`, `data_fetcher.py`, `main.py`, or any `model_*.pkl`.

## Setup

```
.venv/Scripts/python.exe -m pip install typesafe-sdk pyarrow
```

Add to `backend/.env`:

```
TYPESAFE_API_KEY=...
```

## Files

| file | what it does |
| --- | --- |
| `trainer_bridge.py` | Calls `ml_trainer.build_training_data()` unchanged and recovers `player_name` / `actual` / `real_line`, which that function drops. Cross-checks every recovered column against the canonical frame and raises if anything disagrees. |
| `export_real_line_testset.py` | Writes `testset_{stat}.parquet` for Points / Rebounds / Assists — the same 20% temporal test split `train_model()` uses. 3PM skipped (Underdog does not carry it). |
| `jev_eval.py` | Sends one row at a time to Jev as structured JSON state with one Noul question. Caches to `jev_cache.sqlite`. |
| `jev_report.py` | AUC / bootstrap CIs / Brier / log loss / correlation / reliability, plus the pre-registered verdict. |

## Run order

```
python experiments/export_real_line_testset.py

# prompt check only — never look at eval before the prompt is frozen
python experiments/jev_eval.py --stat Points --variant anonymized --slice dev --limit 50
python experiments/jev_report.py --stat Points --slice dev

# after the prompt is frozen
python experiments/jev_eval.py --stat Points   --variant anonymized --slice eval
python experiments/jev_eval.py --stat Rebounds --variant anonymized --slice eval
python experiments/jev_eval.py --stat Assists  --variant anonymized --slice eval
python experiments/jev_eval.py --stat Points   --variant named      --slice eval
python experiments/jev_report.py --slice eval --json-out experiments/results_eval.json
```

`--dry-run` prints the exact state and question without calling the API.

## Model pinning

`jev-1.13.0` is pinned in `jev_eval.py` (`DEFAULT_MODEL`), not `jev-latest` —
`jev-latest` and `jev-preview` are aliases that move when TypeSafe ships a
release. The version resolved by the server is stored per row as
`resolved_model` and printed in the report.

## Caching

`jev_cache.sqlite` is committed so paid answers survive a machine change.
It costs about 1.4 KB per row, so a fully populated run (all three stats, both
variants, ~28k rows) would land near 40 MB and keep a full copy in git history
on every update. If it passes ~50 MB, move it out of git (or to LFS) rather than
committing further versions.

Primary key `(stat, row_id, variant, model_version)`. A
re-run never re-pays for a cached row, and a crashed run resumes where it
stopped. Each row also stores the exact `state_json`, `question_json`, and a
`fingerprint` hash of both, so a changed prompt is detectable after the fact.

## Verdict rule (fixed before any eval data was seen)

Jev beats XGBoost **only** if Jev's anonymized AUC lower CI bound exceeds
XGBoost's upper CI bound. If XGBoost's lower bound exceeds Jev's upper bound,
XGBoost is better. Otherwise: no significant difference.

If the named variant beats anonymized by more than 0.03 AUC, the report warns
that the gap may reflect memorized outcomes rather than skill.

## Two things that differ from the original brief

- **14 features, not 15.** `ml_trainer.FEATURE_COLS` has 14 entries. All 14 are
  exported and all 14 go into the state.
- **The XGBoost models are not calibrated.** `train_model()` fits a bare
  `XGBClassifier`; there is no `CalibratedClassifierCV` in the pipeline. The
  `xgb_prob` column is raw `predict_proba` from `model_{stat}.pkl`.
