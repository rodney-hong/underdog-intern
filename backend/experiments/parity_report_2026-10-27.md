# Phase 3 parity report: PC → DigitalOcean server

Date: 2026-10-27 (checks run 2026-10-26 and 2026-10-27)
Plan: "SportsBettr Cloud Migration Plan v2", Phase 3, steps 1–8
Tooling: `backend/experiments/parity_check.py` and `parity_compare.py` (TOOL_VERSION 2)
This report contains no outcome values. Outcome columns were never selected, hashed or exported.

## Result

Phase 3 passes. Data, models and inputs are identical on both machines. The model outputs differ only by last-digit floating-point rounding, which was accepted (see Decision). No pick changes side.

## Machines

| | PC | Server |
| --- | --- | --- |
| Host | Rodney | sportsbettr (DigitalOcean NYC1, 2 vCPU / 2 GB / 60 GB, 2 GB swap) |
| OS | Windows 11 10.0.26200, AMD64 | Ubuntu 26.04.1, Linux 7.0.0-38, x86_64, glibc 2.43 |
| Python | 3.14.4 (MSC v.1944) | 3.14.4 (Clang 22.1.3, via uv) |
| Packages | 92 lock pins PASS (+ colorama, pip: Windows-only / tooling) | 92 lock pins PASS, no extras |
| Commit | 1093ca6 (steps 1–5) | 1093ca6 (steps 1–5); 988b735 (step 7 retest); 8cdce5e (step 6) |
| Mode | prod (collecting) | dev (nothing automatic) |

## Deviations from the plan

1. The trial snapshot was taken mid-day on 2026-10-26 at 14:16:05 CT, not after the night's last pre-tip pull. It was not taken within 15 minutes of a PC pull.
2. End-to-end used game date 2026-10-25, a complete day inside the snapshot, instead of the snapshot night.
3. The step 7 screenshot check used a disposable copy of the snapshot as the server's `backend/predictions.db`, so name resolution and prediction had real data. Step 6 ran on the same disposable copy, after a backup to `~/trial_before_etl.db`.

## Step 1: Trial snapshot

| Item | Value |
| --- | --- |
| Method | SQLite backup API from the live PC database (source opened read-only), then `journal_mode=DELETE` on the copy |
| Taken | 2026-10-26 14:16:05 CT |
| SHA-256 | `5d748c67f2eb44e438f02465b7ba0ed36075efdf2df333338a19143574806d4a` (PC and server identical) |
| Size | 524,214,272 bytes |
| `odds_pull_log` max rowid | 207 |
| `PRAGMA integrity_check` | ok |
| Transfer | scp with the server's service stopped; server copy kept read-only (444) in `~/parity/` |

## Step 2: Integrity

- `PRAGMA integrity_check` ok on both machines. Schema SHA-256, journal mode, encoding, page size and user_version are the same.
- 27 tables, 1,326,839 rows hashed. Row counts and per-table SHA-256 are identical for every table.
- Excluded outcome columns (same on both): `challenger_predictions` (actual_value, result, resolved_at), `paper_predictions` (actual_points, actual_value, result, resolved_at), `paper_predictions_pre_stat_key` (actual_points, result, resolved_at), `predictions` (actual_result).
- 0 network attempts and 0 denied outcome-column reads on both machines.

## Models

- 8 NBA frozen models, the frozen challenger and baseline (b): SHA-256 matches on both machines and matches `preregistration.md`. Challenger fingerprints match.
- Not listed in the preregistration, as expected: 4 WNBA models and `challenger_stage1_dev.pkl`. Their hashes match between machines.
- Hashes pinned in code match the preregistration on both machines.

## Steps 3–4: Replay against stored predictions

| Series | Checked | PC exact | Server exact | Server last-digit | Server max diff | Mismatch |
| --- | --- | --- | --- | --- | --- | --- |
| paper `p_over` | 1,930 | 1,930 | 1,858 | 72 | 2 ulp (1.11e-16) | 0 |
| paper `p_over_v1` | 675 | 675 | 645 | 30 | 2 ulp (1.11e-16) | 0 |
| challenger `mu` | 298 | 298 | 296 | 2 | 2 ulp (8.88e-16) | 0 |
| challenger `alpha` | 298 | 298 | 298 | 0 | n/a | 0 |
| challenger `p_over` | 298 | 298 | 175 | 123 | 10 ulp (5.55e-16) | 0 |
| challenger `expected_minutes` | 298 | 298 | 298 | 0 | n/a | 0 |
| challenger `rate_per_minute` | 298 | 298 | 295 | 3 | 1 ulp (2.78e-17) | 0 |

Skipped rows are the same on both machines: paper 3 "unmatched player" and 26 "no counted games" (of 1,959); challenger 16 "insufficient_history" (of 314).

### Decision (2026-10-26, Rodney)

Accepted. The Linux recomputation differs from the PC-stored values by at most 10 ulp (max abs 8.88e-16), attributed to Windows vs glibc floating-point libraries. No stored probability lies within 1e-12 of 0.5 (paper `p_over`, paper `p_over_v1`, challenger `p_over`: 0 rows each), so no pick can change side.

Rule fixed before end-to-end and kept for later checks: non-float fields and `features_json` must match exactly; probabilities and model outputs must match within 16 ulp; no side flips.

## Step 5: End-to-end, 2026-10-25

- `log_for_pull` replayed into temporary copies on each machine for the 7 successful pre-tip pulls: 179, 183, 185, 187, 189, 191, 194. Pulls and captures are the same, and row counts per pull are the same.
- Exports (without `id` and `created_at`): `paper_predictions` 313 rows and `challenger_predictions` 314 rows on both machines.
- Non-float differences, including every field inside `features_json`: **0**.
- Float differences: 142, only in `challenger.p_over` (119), `paper.p_over` (11), `paper.p_over_v1` (7), `challenger.rate_per_minute` (3) and `challenger.mu` (2). Max 10 ulp; distribution 1 ulp: 71, 2: 37, 3: 17, 4: 6, 5: 3, 6: 4, 7: 2, 8: 1, 10: 1. Side flips: 0. **Pass** under the rule above.
- Clock pinning: the PC pinned one extra module, `dateutil.tz.win`, which is Windows-only. Harmless.
- Informational (not a criterion): re-running 2026-10-25 today reproduced 59 of 314 stored challenger rows exactly on the PC and 44 on the server. This is a replay-vs-history effect, not a machine difference. To investigate separately: which inputs changed after the fact.
- Server: peak RSS 330 MB, 6:16 wall time on one core, no swap.

## Step 6: Kaggle ETL on Linux (2026-10-27, commit 8cdce5e)

- On the disposable copy, with the service stopped, after a backup-API copy to `~/trial_before_etl.db`: `database.run_etl()` loaded Kaggle **v527 → v528** with one anonymous download and no OddsAPI calls (code reviewed).
- `run_etl` returned True; `kaggle_version` = 528; `PRAGMA integrity_check` ok.
- **Peak RSS 186,708 kB (~187 MB)**, 57 s wall time, no swap. Budget: under 1 GB.
- Kaggle cache afterwards: only `versions/528`, 435 MB in total.
- Pruning: the server's cache was empty before this run, so the server-side check is trivially one version. Pruning was verified live on the PC the same morning (v528 loaded, v527 removed, 0.5 GB freed), and the offline test with fake versions covers it.

## Step 7: Screenshot reading on the server

- Ollama Cloud (`gemma4:31b`), server in dev, through an SSH tunnel: 2 real screenshots, 13 cards. Names, stats, lines, opponent and home/away correct on 13 of 13; both reads agreed on every card; usage confirmed on the Ollama Cloud dashboard. Predictions about 2 s each.
- Forced fallback (invalid Ollama Cloud key, HTTP 401 → Gemini): the switch was logged as designed, twice.
- Gemini (`gemma-4-31b-it`): on 2026-10-26 Google returned HTTP 500/503 ("high demand"), and successful live calls took 33–59 s. Before fix 988b735 the import hung for minutes. After 988b735 (20 s per request, one retry on 500/503, thought parts ignored, `thinkingLevel: minimal`), with both services failing the app shows a clear message after about 22 s.
- Conclusion: Ollama Cloud is the working reader. The Gemini fallback is best-effort with fail-fast limits. Screenshot import does not affect collection, models or predictions.

## Resource summary (server)

| Workload | Peak RSS |
| --- | --- |
| Service idle (dev) | ~340 MB |
| Integrity + models + replay | completed (not timed) |
| End-to-end, 7 pulls | 330 MB |
| Kaggle ETL | 187 MB |

## Follow-ups (not blocking Phase 3)

- Explain the informational replay-vs-stored differences for the challenger on 2026-10-25.
- Gemini fallback: re-test when Google's latency for `gemma-4-31b-it` improves; Ollama Pro remains the plan's option if Free runs out often.
- Before the dry run: the `/slip` fix (one worker, single-flight lock, refuse while pre-tips run, load models once), committed on the PC and deployed.
- The final database copy happens at cutover (Phase 5 step 3). The server's current `predictions.db` is a disposable trial copy.
