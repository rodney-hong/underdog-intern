# Per-player vs pooled models (exploratory)

Train 2012-13..2022-23, regular season; evaluate 2023-24, 2024-25; no game on/after 2025-07-01 loaded (max date loaded 2025-04-13 15:30:00). Rows: >= 5 prior games. Per-player models need >= 100 training games.

## Points (k = 1000, 763 per-player models)

### 2023-24: 24,225 rows, per-player coverage 73.2%

| method | MAE | loglik Poisson | loglik NB | P(>median20) Brier | log loss | ECE |
|---|---|---|---|---|---|---|
| a_last10 | 4.7128 | -3.8226 | -3.219 | 0.2503 | 0.6985 | 0.0677 |
| b_pooled | 4.6211 | -3.7083 | -3.1668 | 0.2458 | 0.6887 | 0.0512 |
| c_per_player_with_pooled_fallback | 4.7777 | -3.8091 | -3.2077 | 0.256 | 0.717 | 0.0847 |
| d_partial_pooling | 4.6331 | -3.7121 | -3.1677 | 0.2456 | 0.6882 | 0.0533 |

On rows covered by per-player models (same rows for every method):

| method | MAE | loglik NB | Brier |
|---|---|---|---|
| a_last10 | 4.8749 | -3.2368 | 0.2492 |
| b_pooled | 4.7798 | -3.1999 | 0.2434 |
| c_per_player_covered | 4.9937 | -3.2557 | 0.2573 |
| d_partial_pooling | 4.7929 | -3.2005 | 0.2431 |

By experience (MAE / loglik NB):

| bucket | rows | coverage | a_last10 | b_pooled | c_per_player_with_pooled_fallback | d_partial_pooling |
|---|---|---|---|---|---|---|
| rookie | 2,281 | 0.0% | 4.3054 / -3.224 | 4.2919 / -3.1078 | 4.2919 / -3.1078 | 4.2919 / -3.1078 |
| 1-3 | 8,050 | 49.4% | 4.5296 / -3.1823 | 4.4022 / -3.1192 | 4.5921 / -3.176 | 4.4153 / -3.1206 |
| 4+ | 13,894 | 99.0% | 4.8858 / -3.2395 | 4.802 / -3.2041 | 4.9649 / -3.2424 | 4.8153 / -3.2047 |

### 2024-25: 25,648 rows, per-player coverage 59.7%

| method | MAE | loglik Poisson | loglik NB | P(>median20) Brier | log loss | ECE |
|---|---|---|---|---|---|---|
| a_last10 | 4.7416 | -3.9066 | -3.2706 | 0.2518 | 0.7052 | 0.0671 |
| b_pooled | 4.6601 | -3.7467 | -3.1798 | 0.2496 | 0.7009 | 0.0529 |
| c_per_player_with_pooled_fallback | 4.8234 | -3.8527 | -3.2216 | 0.2594 | 0.7309 | 0.0892 |
| d_partial_pooling | 4.6675 | -3.7483 | -3.1799 | 0.2493 | 0.7002 | 0.0531 |

On rows covered by per-player models (same rows for every method):

| method | MAE | loglik NB | Brier |
|---|---|---|---|
| a_last10 | 5.0741 | -3.2911 | 0.2505 |
| b_pooled | 4.9669 | -3.2392 | 0.2449 |
| c_per_player_covered | 5.2405 | -3.3093 | 0.2614 |
| d_partial_pooling | 4.9789 | -3.2391 | 0.2444 |

By experience (MAE / loglik NB):

| bucket | rows | coverage | a_last10 | b_pooled | c_per_player_with_pooled_fallback | d_partial_pooling |
|---|---|---|---|---|---|---|
| rookie | 2,739 | 0.0% | 3.9895 / -3.3169 | 3.973 / -3.0351 | 3.973 / -3.0351 | 3.973 / -3.0351 |
| 1-3 | 8,765 | 16.4% | 4.5041 / -3.2343 | 4.4474 / -3.144 | 4.5677 / -3.1799 | 4.4497 / -3.144 |
| 4+ | 14,144 | 98.1% | 5.0344 / -3.2841 | 4.9249 / -3.23 | 5.1466 / -3.2836 | 4.937 / -3.2302 |

## Rebounds (k = 1000, 763 per-player models)

### 2023-24: 24,225 rows, per-player coverage 73.2%

| method | MAE | loglik Poisson | loglik NB | P(>median20) Brier | log loss | ECE |
|---|---|---|---|---|---|---|
| a_last10 | 1.9361 | -2.2741 | -2.2226 | 0.2424 | 0.6821 | 0.0473 |
| b_pooled | 1.8961 | -2.2194 | -2.1799 | 0.2347 | 0.6618 | 0.0191 |
| c_per_player_with_pooled_fallback | 1.9349 | -2.2453 | -2.1994 | 0.2402 | 0.6755 | 0.0428 |
| d_partial_pooling | 1.899 | -2.2197 | -2.1801 | 0.2347 | 0.6618 | 0.0211 |

On rows covered by per-player models (same rows for every method):

| method | MAE | loglik NB | Brier |
|---|---|---|---|
| a_last10 | 1.9595 | -2.2413 | 0.2423 |
| b_pooled | 1.9199 | -2.207 | 0.2353 |
| c_per_player_covered | 1.9728 | -2.2336 | 0.2428 |
| d_partial_pooling | 1.9226 | -2.2071 | 0.2353 |

By experience (MAE / loglik NB):

| bucket | rows | coverage | a_last10 | b_pooled | c_per_player_with_pooled_fallback | d_partial_pooling |
|---|---|---|---|---|---|---|
| rookie | 2,281 | 0.0% | 1.8485 / -2.1984 | 1.8259 / -2.1189 | 1.8259 / -2.1189 | 1.8259 / -2.1189 |
| 1-3 | 8,050 | 49.4% | 1.9108 / -2.1925 | 1.8636 / -2.1477 | 1.9161 / -2.1797 | 1.8661 / -2.1478 |
| 4+ | 13,894 | 99.0% | 1.9651 / -2.244 | 1.9265 / -2.2086 | 1.9637 / -2.224 | 1.9301 / -2.2089 |

### 2024-25: 25,648 rows, per-player coverage 59.7%

| method | MAE | loglik Poisson | loglik NB | P(>median20) Brier | log loss | ECE |
|---|---|---|---|---|---|---|
| a_last10 | 1.972 | -2.3117 | -2.2525 | 0.245 | 0.6897 | 0.0515 |
| b_pooled | 1.9294 | -2.2461 | -2.2012 | 0.2362 | 0.6649 | 0.024 |
| c_per_player_with_pooled_fallback | 1.9681 | -2.2728 | -2.2206 | 0.2411 | 0.678 | 0.0449 |
| d_partial_pooling | 1.9319 | -2.2461 | -2.2011 | 0.2362 | 0.6649 | 0.0244 |

On rows covered by per-player models (same rows for every method):

| method | MAE | loglik NB | Brier |
|---|---|---|---|
| a_last10 | 2.0225 | -2.2811 | 0.2431 |
| b_pooled | 1.9747 | -2.242 | 0.2345 |
| c_per_player_covered | 2.0396 | -2.2745 | 0.2427 |
| d_partial_pooling | 1.9789 | -2.2418 | 0.2346 |

By experience (MAE / loglik NB):

| bucket | rows | coverage | a_last10 | b_pooled | c_per_player_with_pooled_fallback | d_partial_pooling |
|---|---|---|---|---|---|---|
| rookie | 2,739 | 0.0% | 1.8691 / -2.2311 | 1.8619 / -2.1343 | 1.8619 / -2.1343 | 1.8619 / -2.1343 |
| 1-3 | 8,765 | 16.4% | 1.9327 / -2.2161 | 1.8838 / -2.1598 | 1.8913 / -2.1688 | 1.8838 / -2.1598 |
| 4+ | 14,144 | 98.1% | 2.0162 / -2.2793 | 1.9708 / -2.2398 | 2.0363 / -2.2694 | 1.9752 / -2.2397 |

## Assists (k = 1000, 763 per-player models)

### 2023-24: 24,225 rows, per-player coverage 73.2%

| method | MAE | loglik Poisson | loglik NB | P(>median20) Brier | log loss | ECE |
|---|---|---|---|---|---|---|
| a_last10 | 1.3934 | -1.8588 | -1.8355 | 0.2383 | 0.6834 | 0.0507 |
| b_pooled | 1.3696 | -1.7951 | -1.7788 | 0.2295 | 0.6504 | 0.0256 |
| c_per_player_with_pooled_fallback | 1.3907 | -1.8201 | -1.799 | 0.2342 | 0.663 | 0.0405 |
| d_partial_pooling | 1.3718 | -1.7956 | -1.7791 | 0.2296 | 0.6506 | 0.0264 |

On rows covered by per-player models (same rows for every method):

| method | MAE | loglik NB | Brier |
|---|---|---|---|
| a_last10 | 1.4637 | -1.8844 | 0.239 |
| b_pooled | 1.437 | -1.8431 | 0.2305 |
| c_per_player_covered | 1.4659 | -1.8707 | 0.237 |
| d_partial_pooling | 1.4398 | -1.8434 | 0.2306 |

By experience (MAE / loglik NB):

| bucket | rows | coverage | a_last10 | b_pooled | c_per_player_with_pooled_fallback | d_partial_pooling |
|---|---|---|---|---|---|---|
| rookie | 2,281 | 0.0% | 1.1828 / -1.7199 | 1.1912 / -1.6154 | 1.1912 / -1.6154 | 1.1912 / -1.6154 |
| 1-3 | 8,050 | 49.4% | 1.3385 / -1.7804 | 1.3105 / -1.7175 | 1.3477 / -1.7497 | 1.312 / -1.7178 |
| 4+ | 13,894 | 99.0% | 1.4597 / -1.8865 | 1.4331 / -1.8411 | 1.4484 / -1.8577 | 1.4361 / -1.8416 |

### 2024-25: 25,648 rows, per-player coverage 59.7%

| method | MAE | loglik Poisson | loglik NB | P(>median20) Brier | log loss | ECE |
|---|---|---|---|---|---|---|
| a_last10 | 1.3791 | -1.8688 | -1.846 | 0.2379 | 0.6913 | 0.0522 |
| b_pooled | 1.3532 | -1.7892 | -1.774 | 0.2278 | 0.6473 | 0.0247 |
| c_per_player_with_pooled_fallback | 1.3814 | -1.8209 | -1.7996 | 0.2349 | 0.6646 | 0.0453 |
| d_partial_pooling | 1.3555 | -1.7899 | -1.7744 | 0.228 | 0.6475 | 0.0264 |

On rows covered by per-player models (same rows for every method):

| method | MAE | loglik NB | Brier |
|---|---|---|---|
| a_last10 | 1.5165 | -1.9322 | 0.2378 |
| b_pooled | 1.483 | -1.8892 | 0.2288 |
| c_per_player_covered | 1.5302 | -1.9321 | 0.2407 |
| d_partial_pooling | 1.4867 | -1.8899 | 0.229 |

By experience (MAE / loglik NB):

| bucket | rows | coverage | a_last10 | b_pooled | c_per_player_with_pooled_fallback | d_partial_pooling |
|---|---|---|---|---|---|---|
| rookie | 2,739 | 0.0% | 1.1292 / -1.7713 | 1.1304 / -1.5846 | 1.1304 / -1.5846 | 1.1304 / -1.5846 |
| 1-3 | 8,765 | 16.4% | 1.263 / -1.752 | 1.2388 / -1.6692 | 1.2608 / -1.686 | 1.2396 / -1.6692 |
| 4+ | 14,144 | 98.1% | 1.4995 / -1.9187 | 1.4673 / -1.8756 | 1.5047 / -1.9117 | 1.471 / -1.8763 |

## 3PM (k = 1000, 763 per-player models)

### 2023-24: 24,225 rows, per-player coverage 73.2%

| method | MAE | loglik Poisson | loglik NB | P(>median20) Brier | log loss | ECE |
|---|---|---|---|---|---|---|
| a_last10 | 0.888 | -1.384 | -1.3714 | 0.2063 | 0.6399 | 0.0495 |
| b_pooled | 0.885 | -1.2969 | -1.2893 | 0.1994 | 0.5761 | 0.0255 |
| c_per_player_with_pooled_fallback | 0.8902 | -1.3182 | -1.308 | 0.204 | 0.5892 | 0.0298 |
| d_partial_pooling | 0.8862 | -1.297 | -1.2893 | 0.1994 | 0.576 | 0.0273 |

On rows covered by per-player models (same rows for every method):

| method | MAE | loglik NB | Brier |
|---|---|---|---|
| a_last10 | 0.9343 | -1.4102 | 0.2073 |
| b_pooled | 0.9281 | -1.3433 | 0.2007 |
| c_per_player_covered | 0.9352 | -1.3689 | 0.2069 |
| d_partial_pooling | 0.9297 | -1.3434 | 0.2006 |

By experience (MAE / loglik NB):

| bucket | rows | coverage | a_last10 | b_pooled | c_per_player_with_pooled_fallback | d_partial_pooling |
|---|---|---|---|---|---|---|
| rookie | 2,281 | 0.0% | 0.7731 / -1.3189 | 0.7776 / -1.1745 | 0.7776 / -1.1745 | 0.7776 / -1.1745 |
| 1-3 | 8,050 | 49.4% | 0.8412 / -1.3335 | 0.8375 / -1.2378 | 0.8432 / -1.262 | 0.8381 / -1.2378 |
| 4+ | 13,894 | 99.0% | 0.934 / -1.402 | 0.9301 / -1.338 | 0.9359 / -1.3566 | 0.932 / -1.3381 |

### 2024-25: 25,648 rows, per-player coverage 59.7%

| method | MAE | loglik Poisson | loglik NB | P(>median20) Brier | log loss | ECE |
|---|---|---|---|---|---|---|
| a_last10 | 0.911 | -1.4233 | -1.4099 | 0.2069 | 0.6425 | 0.0432 |
| b_pooled | 0.907 | -1.3303 | -1.3221 | 0.1999 | 0.5772 | 0.0182 |
| c_per_player_with_pooled_fallback | 0.9175 | -1.37 | -1.3575 | 0.2062 | 0.6017 | 0.0285 |
| d_partial_pooling | 0.9082 | -1.3305 | -1.3222 | 0.2 | 0.5772 | 0.0199 |

On rows covered by per-player models (same rows for every method):

| method | MAE | loglik NB | Brier |
|---|---|---|---|
| a_last10 | 0.9886 | -1.4678 | 0.2064 |
| b_pooled | 0.9816 | -1.4082 | 0.2 |
| c_per_player_covered | 0.9993 | -1.4676 | 0.2105 |
| d_partial_pooling | 0.9836 | -1.4085 | 0.2001 |

By experience (MAE / loglik NB):

| bucket | rows | coverage | a_last10 | b_pooled | c_per_player_with_pooled_fallback | d_partial_pooling |
|---|---|---|---|---|---|---|
| rookie | 2,739 | 0.0% | 0.7428 / -1.3281 | 0.7563 / -1.1385 | 0.7563 / -1.1385 | 0.7563 / -1.1385 |
| 1-3 | 8,765 | 16.4% | 0.8499 / -1.3583 | 0.8451 / -1.2605 | 0.8561 / -1.2962 | 0.8452 / -1.2603 |
| 4+ | 14,144 | 98.1% | 0.9814 / -1.4576 | 0.9745 / -1.3958 | 0.9868 / -1.4379 | 0.9766 / -1.396 |

Run time: {'load_s': 2.6, 'total_s': 129.5} on CPU (16 logical cores).
