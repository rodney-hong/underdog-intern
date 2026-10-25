# Challenger feature-group ablation (exploratory, development data)

Generated 2026-10-25T18:56:16+00:00. Fit 2012-13..2023-24 (301,587 rows), evaluate 2024-25 (25,668 rows per stat, 1,224 games, >= 5 earlier games). Nothing on/after 2025-07-01 loaded (max date 2025-04-13 15:30:00; seasons 1996-97..2024-25). Design (features, hyperparameters) from challenger_stage1_dev.pkl (a6ce0b652537371c69ef87747c2066e429f28484e72a923b1be469cdb47c8ab2); NB alpha by MLE on out-of-fold predictions over 4 contiguous season blocks, as Stage 1. One change per variant; the other model stays the full design.

**LL drop** = full − variant NB log-likelihood per row (positive: the group helps). **MAE increase** = variant − full (positive: worse without it). 95% CIs from a game-clustered bootstrap (2,000 resamples). Ranked by LL drop.

## Points (full model: LL -3.12332, MAE 4.5904, alpha 0.3152)

| rank | variant | LL drop [95% CI] | MAE increase [95% CI] | alpha |
|---|---|---|---|---|
| 1 | rate: last-10 average | +0.04160 [+0.02886, +0.05772] | +0.0978 [+0.0831, +0.1122] | 0.3416 |
| 2 | minutes: last-10 average | +0.03092 [+0.02518, +0.03653] | +0.0543 [+0.0431, +0.0656] | 0.3392 |
| 3 | rate: drop EWMA per-minute stats | +0.01107 [+0.00910, +0.01301] | +0.0377 [+0.0258, +0.0490] | 0.3301 |
| 4 | minutes: drop EWMA minutes h3 | +0.01079 [+0.00849, +0.01303] | +0.0368 [+0.0284, +0.0449] | 0.3244 |
| 5 | minutes: drop rest / back-to-back | +0.00091 [+0.00029, +0.00156] | +0.0038 [+0.0015, +0.0065] | 0.3158 |
| 6 | rate: drop opponent defense | +0.00064 [+0.00007, +0.00119] | +0.0063 [+0.0030, +0.0097] | 0.3157 |
| 7 | rate: drop usage | +0.00056 [-0.00034, +0.00143] | +0.0153 [+0.0113, +0.0194] | 0.3163 |
| 8 | minutes: drop prior games / seasons | +0.00042 [-0.00021, +0.00107] | +0.0003 [-0.0018, +0.0023] | 0.3156 |
| 9 | rate: drop prior games (rate) | +0.00038 [-0.00022, +0.00100] | -0.0007 [-0.0032, +0.0017] | 0.3165 |
| 10 | minutes: drop EWMA minutes h10 | +0.00029 [-0.00027, +0.00086] | -0.0004 [-0.0024, +0.0016] | 0.3159 |
| 11 | minutes: drop EWMA minutes h30 | +0.00020 [-0.00037, +0.00075] | +0.0016 [-0.0006, +0.0037] | 0.3158 |
| 12 | rate: drop home (rate) | +0.00017 [-0.00007, +0.00040] | +0.0008 [-0.0007, +0.0022] | 0.3154 |
| 13 | minutes: drop home (minutes) | -0.00007 [-0.00047, +0.00035] | -0.0002 [-0.0010, +0.0006] | 0.3153 |
| 14 | minutes: drop absence / games since return | -0.00095 [-0.00190, -0.00008] | -0.0000 [-0.0034, +0.0034] | 0.3161 |

## Rebounds (full model: LL -2.19200, MAE 1.9092, alpha 0.1100)

| rank | variant | LL drop [95% CI] | MAE increase [95% CI] | alpha |
|---|---|---|---|---|
| 1 | rate: drop EWMA per-minute stats | +0.17310 [+0.16595, +0.18018] | +0.4680 [+0.4494, +0.4867] | 0.3239 |
| 2 | rate: last-10 average | +0.02959 [+0.02550, +0.03362] | +0.0404 [+0.0341, +0.0465] | 0.1284 |
| 3 | minutes: last-10 average | +0.02711 [+0.02285, +0.03134] | +0.0266 [+0.0208, +0.0320] | 0.1242 |
| 4 | minutes: drop EWMA minutes h3 | +0.01160 [+0.00942, +0.01393] | +0.0182 [+0.0143, +0.0222] | 0.1162 |
| 5 | minutes: drop rest / back-to-back | +0.00114 [+0.00053, +0.00177] | +0.0013 [+0.0001, +0.0026] | 0.1103 |
| 6 | minutes: drop EWMA minutes h30 | +0.00076 [+0.00026, +0.00128] | +0.0025 [+0.0015, +0.0035] | 0.1106 |
| 7 | rate: drop opponent defense | +0.00067 [+0.00015, +0.00118] | +0.0014 [-0.0000, +0.0029] | 0.1105 |
| 8 | minutes: drop EWMA minutes h10 | +0.00056 [-0.00003, +0.00115] | +0.0010 [+0.0001, +0.0020] | 0.1103 |
| 9 | minutes: drop prior games / seasons | +0.00015 [-0.00045, +0.00075] | +0.0007 [-0.0003, +0.0017] | 0.1103 |
| 10 | rate: drop usage | +0.00004 [-0.00021, +0.00028] | -0.0004 [-0.0010, +0.0002] | 0.1100 |
| 11 | minutes: drop home (minutes) | -0.00001 [-0.00032, +0.00032] | +0.0001 [-0.0003, +0.0005] | 0.1100 |
| 12 | rate: drop home (rate) | -0.00009 [-0.00033, +0.00016] | +0.0001 [-0.0006, +0.0008] | 0.1102 |
| 13 | minutes: drop absence / games since return | -0.00064 [-0.00153, +0.00027] | +0.0003 [-0.0012, +0.0018] | 0.1107 |

## Assists (full model: LL -1.76679, MAE 1.3334, alpha 0.0961)

| rank | variant | LL drop [95% CI] | MAE increase [95% CI] | alpha |
|---|---|---|---|---|
| 1 | rate: drop EWMA per-minute stats | +0.13846 [+0.13159, +0.14474] | +0.2072 [+0.1940, +0.2194] | 0.3996 |
| 2 | rate: last-10 average | +0.05248 [+0.04525, +0.06017] | +0.0336 [+0.0289, +0.0386] | 0.1222 |
| 3 | minutes: last-10 average | +0.01531 [+0.01241, +0.01825] | +0.0117 [+0.0088, +0.0147] | 0.1061 |
| 4 | minutes: drop EWMA minutes h3 | +0.00731 [+0.00578, +0.00891] | +0.0085 [+0.0065, +0.0107] | 0.1022 |
| 5 | rate: drop opponent defense | +0.00126 [+0.00061, +0.00187] | +0.0026 [+0.0012, +0.0040] | 0.0972 |
| 6 | minutes: drop rest / back-to-back | +0.00096 [+0.00051, +0.00143] | +0.0012 [+0.0005, +0.0019] | 0.0965 |
| 7 | rate: drop usage | +0.00072 [+0.00022, +0.00120] | +0.0020 [+0.0011, +0.0029] | 0.0970 |
| 8 | minutes: drop EWMA minutes h30 | +0.00033 [-0.00008, +0.00074] | +0.0006 [-0.0001, +0.0011] | 0.0963 |
| 9 | minutes: drop EWMA minutes h10 | +0.00032 [-0.00008, +0.00074] | +0.0003 [-0.0003, +0.0008] | 0.0963 |
| 10 | minutes: drop prior games / seasons | +0.00030 [-0.00012, +0.00069] | +0.0003 [-0.0003, +0.0008] | 0.0963 |
| 11 | rate: drop home (rate) | +0.00002 [-0.00030, +0.00032] | +0.0003 [-0.0006, +0.0012] | 0.0965 |
| 12 | minutes: drop home (minutes) | -0.00006 [-0.00026, +0.00014] | +0.0000 [-0.0002, +0.0002] | 0.0961 |
| 13 | minutes: drop absence / games since return | -0.00013 [-0.00079, +0.00050] | +0.0007 [-0.0002, +0.0016] | 0.0964 |

## 3PM (full model: LL -1.31422, MAE 0.8941, alpha 0.1349)

| rank | variant | LL drop [95% CI] | MAE increase [95% CI] | alpha |
|---|---|---|---|---|
| 1 | rate: last-10 average | +0.06317 [+0.05473, +0.07208] | +0.0148 [+0.0112, +0.0184] | 0.1946 |
| 2 | minutes: last-10 average | +0.00835 [+0.00636, +0.01028] | +0.0028 [+0.0013, +0.0044] | 0.1458 |
| 3 | rate: drop 3PA per minute | +0.00551 [+0.00386, +0.00725] | -0.0009 [-0.0020, +0.0002] | 0.1398 |
| 4 | minutes: drop EWMA minutes h3 | +0.00300 [+0.00189, +0.00403] | +0.0029 [+0.0017, +0.0040] | 0.1401 |
| 5 | rate: drop EWMA per-minute stats | +0.00233 [+0.00146, +0.00322] | +0.0044 [+0.0034, +0.0055] | 0.1399 |
| 6 | minutes: drop rest / back-to-back | +0.00032 [+0.00002, +0.00065] | +0.0003 [-0.0000, +0.0007] | 0.1353 |
| 7 | rate: drop home (rate) | +0.00013 [-0.00004, +0.00030] | +0.0001 [-0.0002, +0.0003] | 0.1350 |
| 8 | minutes: drop EWMA minutes h30 | +0.00009 [-0.00017, +0.00035] | +0.0003 [+0.0000, +0.0006] | 0.1352 |
| 9 | minutes: drop absence / games since return | +0.00001 [-0.00038, +0.00040] | +0.0001 [-0.0003, +0.0006] | 0.1352 |
| 10 | minutes: drop prior games / seasons | +0.00001 [-0.00026, +0.00028] | -0.0002 [-0.0004, +0.0001] | 0.1351 |
| 11 | rate: drop opponent defense | +0.00001 [-0.00040, +0.00039] | +0.0002 [-0.0004, +0.0008] | 0.1362 |
| 12 | rate: drop usage | -0.00002 [-0.00030, +0.00028] | -0.0005 [-0.0008, -0.0002] | 0.1350 |
| 13 | minutes: drop EWMA minutes h10 | -0.00005 [-0.00032, +0.00020] | -0.0002 [-0.0005, +0.0001] | 0.1351 |
| 14 | minutes: drop home (minutes) | -0.00008 [-0.00022, +0.00006] | -0.0000 [-0.0001, +0.0001] | 0.1349 |

Run time 204.6 s on CPU (16 logical cores).
