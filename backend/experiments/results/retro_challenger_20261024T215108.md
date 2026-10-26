# Retro comparison: challenger vs frozen on this season's app picks (exploratory)

Generated 2026-10-25T02:51:08+00:00. Not pre-registered. Current, resolved, non-limited frozen app picks, game_date >= 2026-10-20, NBA Points/Rebounds/Assists/3PM; challenger re-scored from data before each tip with the frozen file (SHA-256 and fingerprint checked). Skips: {'already scored (reported from the stored row)': 0, 'eligible': 1433, 'inserted': 1433}.

Picks compared: **1,433** over 28 games. Over rate 0.423. Graded results disagreeing with the stored frozen result: 0.

| | challenger | frozen | challenger − frozen |
|---|---|---|---|
| accuracy [95% CI, game-clustered] | 0.5918 [0.5640, 0.6191] | 0.5659 [0.5419, 0.5901] | +0.0258 [+0.0072, +0.0465] |
| Brier | 0.2401 | 0.2454 | -0.0053 [-0.0127, +0.0022] |
| log loss | 0.6740 | 0.6839 | -0.0099 |

Agree on 1,084 of 1,433 picks (75.6%); accuracy when they agree 0.6042. Disagree on 349: challenger right 193, frozen right 156.

## By stat

| stat | n | acc challenger [CI] | acc frozen [CI] | Brier ch / fr | log loss ch / fr | agree | disagree: ch right / fr right |
|---|---|---|---|---|---|---|---|
| Points | 391 | 0.622 [0.568, 0.674] | 0.555 [0.501, 0.607] | 0.2286 / 0.2469 | 0.6499 / 0.6868 | 75.4% | 61 / 35 |
| Rebounds | 380 | 0.566 [0.515, 0.617] | 0.576 [0.535, 0.620] | 0.2518 / 0.2445 | 0.7003 / 0.6821 | 76.3% | 43 / 47 |
| Assists | 294 | 0.612 [0.560, 0.663] | 0.592 [0.533, 0.642] | 0.2319 / 0.2453 | 0.6543 / 0.6840 | 69.4% | 48 / 42 |
| 3PM | 368 | 0.571 [0.530, 0.614] | 0.546 [0.500, 0.595] | 0.2470 / 0.2448 | 0.6884 / 0.6826 | 80.2% | 41 / 32 |

## Calibration of P(over) (mean predicted / observed over rate / n)

| bin | challenger | frozen |
|---|---|---|
| 0.0-0.1 | 0.064 / 0.097 / 31 | — |
| 0.1-0.2 | 0.156 / 0.290 / 93 | — |
| 0.2-0.3 | 0.255 / 0.348 / 164 | 0.255 / 0.000 / 1 |
| 0.3-0.4 | 0.354 / 0.379 / 322 | 0.375 / 0.313 / 67 |
| 0.4-0.5 | 0.451 / 0.429 / 354 | 0.466 / 0.392 / 832 |
| 0.5-0.6 | 0.546 / 0.490 / 259 | 0.525 / 0.486 / 508 |
| 0.6-0.7 | 0.648 / 0.542 / 155 | 0.621 / 0.500 / 24 |
| 0.7-0.8 | 0.733 / 0.608 / 51 | 0.781 / 0.000 / 1 |
| 0.8-0.9 | 0.863 / 0.667 / 3 | — |
| 0.9-1.0 | 0.912 / 1.000 / 1 | — |

Frozen P(over) is the stored confidence of its pick (1 − confidence for an UNDER pick).
Model file SHA-256 `0e4fa88fee927f70d8b2a6679310f36b5f12b099d1861266cab11ae53723d604`, fingerprint `d18451fedc40b51e0c7d29b50ba07515ec6b935bd4597d63b2ffee913b81ee7f`. Run time 289.5 s.
