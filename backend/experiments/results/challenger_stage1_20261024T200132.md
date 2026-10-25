# Challenger Stage 1: development results on 2024-25

Generated 2026-10-25T01:01:32+00:00. Regular season (incl. 2023 Cup games), played rows (minutes > 0). No game on/after 2025-07-01 loaded (max date loaded 2025-04-13 15:30:00; 702,676 rows, seasons 1996-97..2024-25, used for history only before 2012-13). No backfilled lines.

- Tuning: fit 2012-13..2022-23, validate 2023-24.
- Dev model: fit 2012-13..2023-24, evaluate 2024-25 (25,668 rows per stat, 1,224 games; rows need >= 5 earlier games).
- NB dispersion (all three models): MLE on out-of-fold training predictions, folds = contiguous season blocks 2012-13..2014-15, 2015-16..2017-18, 2018-19..2020-21, 2021-22..2023-24 (baseline (a) has no fitted parameters, so its in-sample fit is out of fold).
- Rows: tune_train 275,594, val_2023_24 25,605, dev_train 301,587, eval_2024_25 25,668, frozen_train 327,661.

## Summary: NB log-likelihood per game and MAE, 2024-25

| stat | challenger LL | (b) pooled HGB LL | (a) last-10 LL | challenger − (b) [95% CI] | challenger MAE | (b) MAE | (a) MAE |
|---|---|---|---|---|---|---|---|
| Points | -3.1233 | -3.1295 | -3.1962 | +0.0061 [+0.0025, +0.0096] | 4.5904 | 4.6519 | 4.7406 |
| Rebounds | -2.1920 | -2.1994 | -2.2472 | +0.0074 [+0.0045, +0.0103] | 1.9092 | 1.9260 | 1.9719 |
| Assists | -1.7668 | -1.7730 | -1.8335 | +0.0062 [+0.0036, +0.0089] | 1.3334 | 1.3511 | 1.3795 |
| 3PM | -1.3142 | -1.3212 | -1.3845 | +0.0070 [+0.0047, +0.0091] | 0.8941 | 0.9045 | 0.9110 |

Pooled over the four stats (each player-game-stat one row): challenger − (b) +0.0067 [+0.0048, +0.0085]; challenger − (a) +0.0663 [+0.0599, +0.0733] nats per row (game-clustered bootstrap, 1,224 games, 2,000 resamples).

## Calibration of P(stat > L), 2024-25

Lines per row: the player's median of his last 20 earlier games (min 5), median ± 2, and the nearest x4.5/x9.5 rungs below (≤) and above (>) the median. Lines below 0 are dropped. Over = stat > L (a push on an integer line counts as not over). ECE over 10 equal-width bins.

| stat | line | n | over rate | challenger Brier / ECE | (b) Brier / ECE | (a) Brier / ECE |
|---|---|---|---|---|---|---|
| Points | median | 25,668 | 0.480 | 0.2444 / 0.0524 | 0.2465 / 0.0426 | 0.2493 / 0.0535 |
| Points | median-2 | 24,073 | 0.616 | 0.2327 / 0.0684 | 0.2336 / 0.0627 | 0.2371 / 0.0696 |
| Points | median+2 | 25,668 | 0.352 | 0.2186 / 0.0265 | 0.2201 / 0.0126 | 0.2285 / 0.0473 |
| Points | rung below | 19,593 | 0.620 | 0.2223 / 0.0543 | 0.2225 / 0.0423 | 0.2290 / 0.0582 |
| Points | rung above | 25,668 | 0.335 | 0.2076 / 0.0262 | 0.2087 / 0.0114 | 0.2168 / 0.0378 |
| Rebounds | median | 25,668 | 0.438 | 0.2339 / 0.0141 | 0.2358 / 0.0179 | 0.2445 / 0.0472 |
| Rebounds | median-2 | 20,908 | 0.752 | 0.1743 / 0.0172 | 0.1755 / 0.0184 | 0.1803 / 0.0335 |
| Rebounds | median+2 | 25,668 | 0.188 | 0.1456 / 0.0118 | 0.1469 / 0.0113 | 0.1519 / 0.0422 |
| Rebounds | rung below | 8,383 | 0.635 | 0.2083 / 0.0286 | 0.2098 / 0.0265 | 0.2148 / 0.0243 |
| Rebounds | rung above | 25,668 | 0.198 | 0.1410 / 0.0095 | 0.1423 / 0.0095 | 0.1464 / 0.0265 |
| Assists | median | 25,668 | 0.403 | 0.2257 / 0.0037 | 0.2274 / 0.0187 | 0.2376 / 0.0503 |
| Assists | median-2 | 12,843 | 0.768 | 0.1673 / 0.0084 | 0.1680 / 0.0139 | 0.1728 / 0.0286 |
| Assists | median+2 | 25,668 | 0.115 | 0.0964 / 0.0087 | 0.0974 / 0.0131 | 0.0997 / 0.0295 |
| Assists | rung below | 4,019 | 0.618 | 0.2112 / 0.0251 | 0.2135 / 0.0162 | 0.2204 / 0.0155 |
| Assists | rung above | 25,668 | 0.102 | 0.0771 / 0.0078 | 0.0777 / 0.0095 | 0.0796 / 0.0155 |
| 3PM | median | 25,668 | 0.333 | 0.1978 / 0.0047 | 0.1995 / 0.0155 | 0.2066 / 0.0390 |
| 3PM | median-2 | 6,944 | 0.812 | 0.1465 / 0.0218 | 0.1464 / 0.0121 | 0.1480 / 0.0238 |
| 3PM | median+2 | 25,668 | 0.064 | 0.0574 / 0.0058 | 0.0578 / 0.0092 | 0.0596 / 0.0242 |
| 3PM | rung below | 60 | 0.250 | 0.1952 / 0.0980 | 0.2008 / 0.1032 | 0.2208 / 0.1706 |
| 3PM | rung above | 25,668 | 0.043 | 0.0376 / 0.0031 | 0.0378 / 0.0058 | 0.0391 / 0.0125 |

### Reliability, median line (mean predicted P / observed over rate / n)

**Points**

| bin | challenger | (b) pooled HGB | (a) last-10 |
|---|---|---|---|
| 0.0-0.1 | 0.076 / 0.083 / 36 | 0.072 / 0.065 / 46 | 0.062 / 0.214 / 103 |
| 0.1-0.2 | 0.165 / 0.264 / 330 | 0.161 / 0.186 / 274 | 0.162 / 0.362 / 533 |
| 0.2-0.3 | 0.263 / 0.285 / 1,573 | 0.262 / 0.293 / 1,134 | 0.260 / 0.373 / 1,912 |
| 0.3-0.4 | 0.359 / 0.391 / 6,649 | 0.361 / 0.391 / 5,280 | 0.358 / 0.420 / 6,542 |
| 0.4-0.5 | 0.446 / 0.493 / 8,961 | 0.448 / 0.475 / 9,574 | 0.446 / 0.483 / 9,133 |
| 0.5-0.6 | 0.542 / 0.570 / 4,099 | 0.543 / 0.548 / 5,004 | 0.542 / 0.540 / 4,353 |
| 0.6-0.7 | 0.644 / 0.569 / 1,783 | 0.643 / 0.576 / 2,056 | 0.644 / 0.583 / 1,894 |
| 0.7-0.8 | 0.750 / 0.600 / 1,171 | 0.750 / 0.601 / 1,063 | 0.746 / 0.618 / 788 |
| 0.8-0.9 | 0.844 / 0.636 / 851 | 0.846 / 0.589 / 1,015 | 0.838 / 0.663 / 353 |
| 0.9-1.0 | 0.930 / 0.730 / 215 | 0.928 / 0.676 / 222 | 0.928 / 0.754 / 57 |

**Rebounds**

| bin | challenger | (b) pooled HGB | (a) last-10 |
|---|---|---|---|
| 0.0-0.1 | 0.075 / 0.107 / 75 | 0.071 / 0.100 / 40 | 0.073 / 0.319 / 204 |
| 0.1-0.2 | 0.162 / 0.212 / 631 | 0.166 / 0.186 / 366 | 0.169 / 0.316 / 981 |
| 0.2-0.3 | 0.262 / 0.276 / 2,558 | 0.262 / 0.275 / 2,410 | 0.262 / 0.352 / 3,345 |
| 0.3-0.4 | 0.355 / 0.358 / 6,669 | 0.355 / 0.364 / 6,469 | 0.356 / 0.396 / 6,729 |
| 0.4-0.5 | 0.448 / 0.449 / 8,033 | 0.448 / 0.440 / 8,553 | 0.449 / 0.446 / 7,346 |
| 0.5-0.6 | 0.543 / 0.534 / 4,600 | 0.544 / 0.527 / 4,827 | 0.543 / 0.499 / 4,520 |
| 0.6-0.7 | 0.645 / 0.584 / 1,887 | 0.641 / 0.581 / 2,013 | 0.638 / 0.563 / 1,789 |
| 0.7-0.8 | 0.743 / 0.640 / 811 | 0.741 / 0.649 / 767 | 0.740 / 0.622 / 580 |
| 0.8-0.9 | 0.840 / 0.767 / 330 | 0.841 / 0.768 / 198 | 0.840 / 0.703 / 155 |
| 0.9-1.0 | 0.933 / 0.892 / 74 | 0.923 / 0.880 / 25 | 0.924 / 0.842 / 19 |

**Assists**

| bin | challenger | (b) pooled HGB | (a) last-10 |
|---|---|---|---|
| 0.0-0.1 | 0.078 / 0.080 / 75 | 0.077 / 0.206 / 63 | 0.064 / 0.252 / 755 |
| 0.1-0.2 | 0.164 / 0.175 / 1,016 | 0.165 / 0.184 / 832 | 0.168 / 0.279 / 1,829 |
| 0.2-0.3 | 0.258 / 0.258 / 4,302 | 0.260 / 0.254 / 3,452 | 0.261 / 0.333 / 4,669 |
| 0.3-0.4 | 0.353 / 0.354 / 7,374 | 0.353 / 0.330 / 7,004 | 0.357 / 0.382 / 6,854 |
| 0.4-0.5 | 0.447 / 0.441 / 7,384 | 0.448 / 0.435 / 7,840 | 0.451 / 0.426 / 6,033 |
| 0.5-0.6 | 0.543 / 0.541 / 3,758 | 0.543 / 0.516 / 4,476 | 0.542 / 0.501 / 3,793 |
| 0.6-0.7 | 0.640 / 0.626 / 1,276 | 0.638 / 0.621 / 1,579 | 0.638 / 0.563 / 1,379 |
| 0.7-0.8 | 0.740 / 0.717 / 385 | 0.735 / 0.685 / 372 | 0.741 / 0.623 / 302 |
| 0.8-0.9 | 0.830 / 0.761 / 92 | 0.833 / 0.750 / 48 | 0.821 / 0.717 / 53 |
| 0.9-1.0 | 0.938 / 1.000 / 6 | 0.911 / 0.500 / 2 | 0.920 / 1.000 / 1 |

**3PM**

| bin | challenger | (b) pooled HGB | (a) last-10 |
|---|---|---|---|
| 0.0-0.1 | 0.023 / 0.018 / 2,486 | 0.030 / 0.021 / 2,346 | 0.028 / 0.093 / 4,028 |
| 0.1-0.2 | 0.161 / 0.172 / 2,067 | 0.161 / 0.162 / 1,799 | 0.170 / 0.256 / 2,416 |
| 0.2-0.3 | 0.255 / 0.246 / 5,087 | 0.257 / 0.252 / 4,702 | 0.261 / 0.315 / 4,967 |
| 0.3-0.4 | 0.350 / 0.348 / 7,001 | 0.350 / 0.333 / 7,004 | 0.353 / 0.350 / 6,114 |
| 0.4-0.5 | 0.446 / 0.446 / 5,410 | 0.446 / 0.422 / 5,418 | 0.451 / 0.439 / 4,823 |
| 0.5-0.6 | 0.542 / 0.536 / 2,643 | 0.543 / 0.523 / 3,413 | 0.548 / 0.497 / 2,415 |
| 0.6-0.7 | 0.639 / 0.625 / 769 | 0.637 / 0.619 / 846 | 0.643 / 0.584 / 733 |
| 0.7-0.8 | 0.738 / 0.702 / 188 | 0.729 / 0.640 / 136 | 0.737 / 0.620 / 163 |
| 0.8-0.9 | 0.839 / 0.824 / 17 | 0.831 / 1.000 / 4 | 0.820 / 0.667 / 9 |

## By experience (prior NBA seasons), 2024-25

| stat | bucket | rows | LL challenger / (b) / (a) | challenger − (b) [95% CI] | MAE challenger / (b) / (a) | Brier @median challenger / (b) / (a) |
|---|---|---|---|---|---|---|
| Points | rookie | 2,741 | -2.9104 / -2.9132 / -3.1035 | +0.0028 [-0.0096, +0.0150] | 3.9097 / 3.9639 / 3.9915 | 0.2491 / 0.2542 / 0.2493 |
| Points | 1-3 | 8,768 | -3.0486 / -3.0583 / -3.1202 | +0.0097 [+0.0032, +0.0161] | 4.3723 / 4.4428 / 4.5022 | 0.2459 / 0.2488 / 0.2496 |
| Points | 4+ | 14,159 | -3.2108 / -3.2154 / -3.2613 | +0.0046 [+0.0012, +0.0079] | 4.8572 / 4.9146 / 5.0332 | 0.2426 / 0.2437 / 0.2491 |
| Rebounds | rookie | 2,741 | -2.1168 / -2.1268 / -2.2081 | +0.0100 [-0.0020, +0.0217] | 1.8377 / 1.8606 / 1.8697 | 0.2408 / 0.2457 / 0.2524 |
| Rebounds | 1-3 | 8,768 | -2.1475 / -2.1551 / -2.2081 | +0.0075 [+0.0015, +0.0134] | 1.8650 / 1.8788 / 1.9330 | 0.2355 / 0.2361 / 0.2469 |
| Rebounds | 4+ | 14,159 | -2.2341 / -2.2409 / -2.2790 | +0.0068 [+0.0038, +0.0098] | 1.9505 / 1.9680 / 2.0158 | 0.2315 / 0.2336 / 0.2414 |
| Assists | rookie | 2,741 | -1.5699 / -1.5797 / -1.7116 | +0.0099 [-0.0009, +0.0205] | 1.1070 / 1.1292 / 1.1289 | 0.2219 / 0.2240 / 0.2373 |
| Assists | 1-3 | 8,768 | -1.6667 / -1.6677 / -1.7377 | +0.0010 [-0.0040, +0.0061] | 1.2245 / 1.2347 / 1.2632 | 0.2269 / 0.2268 / 0.2386 |
| Assists | 4+ | 14,159 | -1.8669 / -1.8756 / -1.9164 | +0.0087 [+0.0058, +0.0116] | 1.4447 / 1.4662 / 1.5000 | 0.2258 / 0.2285 / 0.2371 |
| 3PM | rookie | 2,741 | -1.1267 / -1.1364 / -1.2604 | +0.0098 [+0.0015, +0.0180] | 0.7592 / 0.7560 / 0.7428 | 0.1981 / 0.1990 / 0.2047 |
| 3PM | 1-3 | 8,768 | -1.2515 / -1.2601 / -1.3303 | +0.0085 [+0.0046, +0.0125] | 0.8355 / 0.8429 / 0.8498 | 0.1997 / 0.2019 / 0.2102 |
| 3PM | 4+ | 14,159 | -1.3893 / -1.3949 / -1.4420 | +0.0056 [+0.0027, +0.0082] | 0.9565 / 0.9715 / 0.9815 | 0.1965 / 0.1981 / 0.2048 |

## Chosen design

- Minutes model (shared by the four stats): HGB, squared error, {'learning_rate': 0.05, 'max_iter': 500, 'max_leaf_nodes': 15, 'min_samples_leaf': 500}; features ['min_h3', 'min_h10', 'min_h30', 'rest_days', 'b2b', 'home', 'games_since_absence', 'missed_prev', 'n_prior', 'experience'].
- Rate model Points: HGB, Poisson loss on stat/minutes weighted by minutes, {'learning_rate': 0.05, 'max_iter': 200, 'max_leaf_nodes': 15, 'min_samples_leaf': 500}; features ['rate_points_h3', 'rate_points_h10', 'rate_points_h30', 'usage_h3', 'usage_h10', 'usage_h30', 'opp_rel_points', 'home', 'n_prior'].
- Rate model Rebounds: HGB, Poisson loss on stat/minutes weighted by minutes, {'learning_rate': 0.05, 'max_iter': 200, 'max_leaf_nodes': 15, 'min_samples_leaf': 100}; features ['rate_reboundsTotal_h3', 'rate_reboundsTotal_h10', 'rate_reboundsTotal_h30', 'usage_h3', 'usage_h10', 'usage_h30', 'opp_rel_reboundsTotal', 'home'].
- Rate model Assists: HGB, Poisson loss on stat/minutes weighted by minutes, {'learning_rate': 0.05, 'max_iter': 200, 'max_leaf_nodes': 15, 'min_samples_leaf': 100}; features ['rate_assists_h3', 'rate_assists_h10', 'rate_assists_h30', 'usage_h3', 'usage_h10', 'usage_h30', 'opp_rel_assists', 'home'].
- Rate model 3PM: HGB, Poisson loss on stat/minutes weighted by minutes, {'learning_rate': 0.05, 'max_iter': 200, 'max_leaf_nodes': 15, 'min_samples_leaf': 500}; features ['rate_threePointersMade_h3', 'rate_threePointersMade_h10', 'rate_threePointersMade_h30', 'usage_h3', 'usage_h10', 'usage_h30', 'opp_rel_threePointersMade', 'home', 'rate_threePointersAttempted_h3', 'rate_threePointersAttempted_h10', 'rate_threePointersAttempted_h30'].
- NB alpha (dev / frozen): Points 0.3152 / 0.3172, Rebounds 0.1100 / 0.1107, Assists 0.0961 / 0.0963, 3PM 0.1349 / 0.1342.
- Baseline (b) alpha (dev / frozen): Points 0.3226 / 0.3247, Rebounds 0.1160 / 0.1168, Assists 0.1026 / 0.1030, 3PM 0.1499 / 0.1483; (a) Points 0.3636, Rebounds 0.1406, Assists 0.1327, 3PM 0.2042.

## Every tuning choice (fit 2012-13..2022-23, validate 2023-24)

Minutes grid (validation MSE, minutes², lower is better):

| max_iter | leaves | min leaf | val MSE |
|---|---|---|---|
| 200 | 15 | 100 | 39.9535 |
| 200 | 15 | 500 | 39.9528 |
| 200 | 31 | 100 | 39.893 |
| 200 | 31 | 500 | 39.8885 |
| 200 | 63 | 100 | 39.8787 |
| 200 | 63 | 500 | 39.902 |
| 500 | 15 | 100 | 39.8756 |
| 500 | 15 | 500 | 39.8632 |
| 500 | 31 | 100 | 39.9104 |
| 500 | 31 | 500 | 39.8981 |
| 500 | 63 | 100 | 39.9829 |
| 500 | 63 | 500 | 39.9756 |

Minutes feature-group changes (each vs the tuned base; accepted if MSE improves by > 0.1%):

| change | val MSE | base | accepted |
|---|---|---|---|
| drop rest/b2b | 40.1721 | 39.8632 | False |
| drop home | 39.8597 | 39.8632 | False |
| drop absence | 40.0262 | 39.8632 | False |
| drop half-life 3 | 43.1137 | 39.8632 | False |
| add n_prior | 39.8172 | 39.8632 | True |
| add experience | 39.8123 | 39.8632 | True |

Rate Points: grid (validation NB log-lik per row, alpha profiled on validation; tuning only):

| max_iter | leaves | min leaf | val LL | alpha |
|---|---|---|---|---|
| 200 | 15 | 100 | -3.11061 | 0.3111 |
| 200 | 15 | 500 | -3.11053 | 0.3109 |
| 200 | 31 | 100 | -3.11094 | 0.3114 |
| 200 | 31 | 500 | -3.11066 | 0.3111 |
| 200 | 63 | 100 | -3.11112 | 0.3116 |
| 200 | 63 | 500 | -3.11124 | 0.3117 |
| 500 | 15 | 100 | -3.11085 | 0.3113 |
| 500 | 15 | 500 | -3.1107 | 0.3111 |
| 500 | 31 | 100 | -3.11139 | 0.3119 |
| 500 | 31 | 500 | -3.11125 | 0.3117 |
| 500 | 63 | 100 | -3.11198 | 0.3126 |
| 500 | 63 | 500 | -3.11241 | 0.3129 |

Rate Points: feature-group changes (accepted if LL improves by > 0.0005 per row):

| change | val LL | base | accepted |
|---|---|---|---|
| drop usage | -3.11243 | -3.11053 | False |
| drop opp defense | -3.11115 | -3.11053 | False |
| drop home | -3.11054 | -3.11053 | False |
| drop half-life 3 | -3.11085 | -3.11053 | False |
| add minutes EWMA (h10) | -3.11013 | -3.11053 | False |
| add n_prior | -3.10977 | -3.11053 | True |

Final validation LL: -3.10977

Rate Rebounds: grid (validation NB log-lik per row, alpha profiled on validation; tuning only):

| max_iter | leaves | min leaf | val LL | alpha |
|---|---|---|---|---|
| 200 | 15 | 100 | -2.16825 | 0.1043 |
| 200 | 15 | 500 | -2.16834 | 0.1043 |
| 200 | 31 | 100 | -2.16839 | 0.1044 |
| 200 | 31 | 500 | -2.1685 | 0.1045 |
| 200 | 63 | 100 | -2.16892 | 0.1048 |
| 200 | 63 | 500 | -2.16917 | 0.1049 |
| 500 | 15 | 100 | -2.16858 | 0.1045 |
| 500 | 15 | 500 | -2.16863 | 0.1045 |
| 500 | 31 | 100 | -2.16917 | 0.1049 |
| 500 | 31 | 500 | -2.16917 | 0.1049 |
| 500 | 63 | 100 | -2.17027 | 0.1058 |
| 500 | 63 | 500 | -2.17036 | 0.1058 |

Rate Rebounds: feature-group changes (accepted if LL improves by > 0.0005 per row):

| change | val LL | base | accepted |
|---|---|---|---|
| drop usage | -2.16834 | -2.16825 | False |
| drop opp defense | -2.16854 | -2.16825 | False |
| drop home | -2.16832 | -2.16825 | False |
| drop half-life 3 | -2.16857 | -2.16825 | False |
| add minutes EWMA (h10) | -2.16814 | -2.16825 | False |
| add n_prior | -2.16782 | -2.16825 | False |

Final validation LL: -2.16825

Rate Assists: grid (validation NB log-lik per row, alpha profiled on validation; tuning only):

| max_iter | leaves | min leaf | val LL | alpha |
|---|---|---|---|---|
| 200 | 15 | 100 | -1.76566 | 0.0939 |
| 200 | 15 | 500 | -1.76567 | 0.094 |
| 200 | 31 | 100 | -1.76575 | 0.094 |
| 200 | 31 | 500 | -1.76585 | 0.0941 |
| 200 | 63 | 100 | -1.76626 | 0.0946 |
| 200 | 63 | 500 | -1.76643 | 0.0948 |
| 500 | 15 | 100 | -1.76589 | 0.0941 |
| 500 | 15 | 500 | -1.76592 | 0.0942 |
| 500 | 31 | 100 | -1.76646 | 0.0948 |
| 500 | 31 | 500 | -1.76662 | 0.0948 |
| 500 | 63 | 100 | -1.76754 | 0.0959 |
| 500 | 63 | 500 | -1.76807 | 0.0963 |

Rate Assists: feature-group changes (accepted if LL improves by > 0.0005 per row):

| change | val LL | base | accepted |
|---|---|---|---|
| drop usage | -1.76634 | -1.76566 | False |
| drop opp defense | -1.76592 | -1.76566 | False |
| drop home | -1.76621 | -1.76566 | False |
| drop half-life 3 | -1.76606 | -1.76566 | False |
| add minutes EWMA (h10) | -1.76519 | -1.76566 | False |
| add n_prior | -1.76523 | -1.76566 | False |

Final validation LL: -1.76566

Rate 3PM: grid (validation NB log-lik per row, alpha profiled on validation; tuning only):

| max_iter | leaves | min leaf | val LL | alpha |
|---|---|---|---|---|
| 200 | 15 | 100 | -1.27503 | 0.111 |
| 200 | 15 | 500 | -1.27501 | 0.1109 |
| 200 | 31 | 100 | -1.27527 | 0.1114 |
| 200 | 31 | 500 | -1.27527 | 0.1113 |
| 200 | 63 | 100 | -1.27553 | 0.1118 |
| 200 | 63 | 500 | -1.27581 | 0.112 |
| 500 | 15 | 100 | -1.27542 | 0.1119 |
| 500 | 15 | 500 | -1.27519 | 0.1112 |
| 500 | 31 | 100 | -1.27594 | 0.1125 |
| 500 | 31 | 500 | -1.276 | 0.1127 |
| 500 | 63 | 100 | -1.27714 | 0.1145 |
| 500 | 63 | 500 | -1.27712 | 0.114 |

Rate 3PM: feature-group changes (accepted if LL improves by > 0.0005 per row):

| change | val LL | base | accepted |
|---|---|---|---|
| drop usage | -1.27514 | -1.27501 | False |
| drop opp defense | -1.27584 | -1.27501 | False |
| drop home | -1.27507 | -1.27501 | False |
| drop half-life 3 | -1.27512 | -1.27501 | False |
| drop 3PA per minute | -1.27855 | -1.27501 | False |
| add minutes EWMA (h10) | -1.2746 | -1.27501 | False |
| add n_prior | -1.27471 | -1.27501 | False |

Final validation LL: -1.27501

## Train/inference parity

challenger.inference.features_before_tip (SQL on rows before tip, one placeholder row) vs the training frame on 200 rows of 2023-24/2024-25 (a quarter drawn from rookies and first games back from an absence), 40 features each: **0 mismatches** (exact equality, NaN = NaN).
challenger_predict smoke test: person 1630167, game 22400320, Points > 19.5: inference 0.075838, batch 0.075838, equal = True.

## Model files (not committed)

- dev: backend/challenger/models/challenger_stage1_dev.pkl  file SHA-256 `a6ce0b652537371c69ef87747c2066e429f28484e72a923b1be469cdb47c8ab2`, content fingerprint `371dcf61f25659d50385fdd4128785842d5c899db3bda2e9ad5aaf6ac153ac25`
- frozen_challenger: backend/challenger/models/challenger_stage2_frozen.pkl  file SHA-256 `0e4fa88fee927f70d8b2a6679310f36b5f12b099d1861266cab11ae53723d604`, content fingerprint `d18451fedc40b51e0c7d29b50ba07515ec6b935bd4597d63b2ffee913b81ee7f`
- frozen_baseline_b: backend/challenger/models/baseline_b_stage2_frozen.pkl  file SHA-256 `59f96ad61a1d900184229995770ff7102d617c86bd5e7a423ea2e0dd3bcd0370`, content fingerprint `e2b7233d06e8b8a513ae8c3e2a46f2c9817810fd0ba61ac749540dcb406f7bd1`

The file SHA-256 pins the saved artifact. A refit with the same code and data gives identical trees, bin thresholds and alphas (checked for the frozen challenger) but different pickle bytes, so model.fingerprint() hashes the fitted content for refit checks.

## CPU time

| phase | wall s | CPU s (all threads) |
|---|---|---|
| load | 4.0 | 4.0 |
| features (challenger) | 3.2 | 3.1 |
| features (baseline b) | 11.3 | 11.2 |
| parity check | 224.1 | 221.9 |
| tune minutes | 31.8 | 233.1 |
| tune rate Points | 29.7 | 226.0 |
| tune rate Rebounds | 25.2 | 200.8 |
| tune rate Assists | 25.6 | 204.2 |
| tune rate 3PM | 30.6 | 243.7 |
| fit challenger dev (incl. OOF) | 27.1 | 204.9 |
| fit baseline b dev (incl. OOF) | 23.4 | 173.8 |
| evaluate 2024-25 | 3.7 | 10.8 |
| fit challenger frozen (incl. OOF) | 28.8 | 216.0 |
| fit baseline b frozen (incl. OOF) | 25.6 | 188.4 |

16 logical cores.
