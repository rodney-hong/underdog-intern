# Forward-logging plan

Plan only — no code changed. Nothing here has been implemented.

Motivation: Step 0 showed Tests A (devig calibration), B (cross-book disagreement) and
D (market-baseline residual) are untestable on stored data. They need prices, both
sides, and more than one book per event. None of that is being captured today, and
none of it can be backfilled affordably (see §3).

---

## 1. Why `underdog_lines` has never been created

The table is created lazily by `CREATE TABLE IF NOT EXISTS` inside
`_log_underdog_lines` ([data_fetcher.py:598](../data_fetcher.py#L598)). If that function
never runs, the table never exists. It never runs, for four stacked reasons:

1. **It is called from inside the per-event loop.** The only call sites are
   [data_fetcher.py:749](../data_fetcher.py#L749) (NBA) and
   [data_fetcher.py:1189](../data_fetcher.py#L1189) (WNBA), both in the body of
   `for event in today_events`. With an empty slate the loop body never executes.
   Today is 2026-10-05: the 2025-26 season ended 2026-05-26 and 2026-27 has not
   started, so `today_events` is empty and always has been since the switch.
2. **Nothing schedules a pull.** The only caller of `get_todays_lines()` is the slip
   endpoint ([main.py:302](../main.py#L302)). `@app.on_event("startup")` is the sole
   lifecycle hook and there is no APScheduler/cron. Logging therefore requires a human
   to hit that endpoint, while the server is up, on a day with games.
3. **Three early returns bypass logging** even on a game day: no API key
   ([data_fetcher.py:641](../data_fetcher.py#L641)), in-memory cache hit within the
   45-minute TTL, and SQLite `odds_cache` hit. On a cache hit the function returns
   before the loop, so nothing is logged.
4. **Failures are silent.** `_log_underdog_lines` ends in `except Exception: pass`
   ([data_fetcher.py:619](../data_fetcher.py#L619)). If the write ever did fail, there
   would be no log line and no raised error.

Corroborating evidence that no fetch has completed since the Underdog switch: the single
`odds_cache` row is stamped `2026-05-30T17:49` and its market entries carry
`"source": "fanduel"` / `"draftkings"`. The switch to `_PREFERRED_BOOKS = ["underdog"]`
with `regions=us_dfs` landed in commit `4441132` on 2026-09-16. That cached row predates
it, and the fetch path replaces the row on every successful pull — so no successful pull
has happened since. The API key itself is present and 32 chars, so credentials are not
the blocker.

### Verifying it on one live pull

Requires a night with NBA events in OddsAPI. Preseason games appear in the feed, so an
October preseason night is enough to test the plumbing.

1. Clear both cache layers, or the fetch will be skipped:
   `DELETE FROM odds_cache;` and restart the process (the in-memory cache dies with it).
2. Note the credit counter before: any OddsAPI call returns `x-requests-remaining`;
   `historical_odds_etl.py` already reads this header
   ([historical_odds_etl.py:220](../historical_odds_etl.py#L220)).
3. Start the backend and hit the slip endpoint once.
4. Check:

```sql
SELECT name FROM sqlite_master WHERE name = 'underdog_lines';          -- exists at all?
SELECT COUNT(*), COUNT(DISTINCT player_name), COUNT(DISTINCT market_key),
       MIN(captured_at), MAX(captured_at) FROM underdog_lines;
SELECT market_key, COUNT(*) FROM underdog_lines GROUP BY 1 ORDER BY 2 DESC;
SELECT cached_at FROM odds_cache;                                      -- should be now
```

5. Note `x-requests-remaining` after. The delta is the true per-slate cost, which
   settles §3 empirically instead of by formula.

**Pass:** table exists, row count ≈ players × markets for the slate, `captured_at` is
now, `odds_cache.cached_at` advanced. **Fail with an empty table but a fresh
`odds_cache` row** would mean the logging call itself is throwing and being swallowed at
reason 4 — add a log line there before investigating further.

### What still will not work once it does run

Even a working `underdog_lines` does not unblock the deferred tests:

- **No price column** — `stat_line` only. Tests A and D stay impossible.
- **`PRIMARY KEY (player_name, market_key, game_date, league)` with
  `INSERT OR REPLACE`** — one surviving row per player/market/day, last write wins.
  Line movement is overwritten, which is the same flaw that left
  `historical_player_props` with exactly one snapshot per market.
- **Only `_PREFERRED_BOOKS` is parsed** — one book, so Test B stays impossible.

Unrelated bug noticed while tracing this, flagged not fixed: `main.py`'s
`STAT_TO_MARKET` requests `player_threes` ([main.py:298](../main.py#L298)) but
`_PROP_MARKETS` deliberately omits it ([data_fetcher.py:574](../data_fetcher.py#L574)),
so 3PM lines can never be found in the cache.

---

## 2. Logging two-sided FD/DK prices for the same events

Five changes, all in the parse-and-store layer. **The over, the under and both prices
already arrive in the same API response** — the ETL discarded them at
[historical_odds_etl.py:253](../historical_odds_etl.py#L253) and omitted price from the
INSERT at [historical_odds_etl.py:276](../historical_odds_etl.py#L276). Recovering them
costs no additional credits.

1. **Keep both sides.** Drop the `if outcome.get("name") != "Over": continue` filter;
   store `side` as `'Over'`/`'Under'`.
2. **Store `price`.** It is already in each outcome, and `oddsFormat=american` is
   already requested. One extra column.
3. **Iterate every bookmaker returned**, not a fallback chain. Replace the
   `for book_key in _PREFERRED_BOOKS: next(...)` pattern
   ([data_fetcher.py:720](../data_fetcher.py#L720)) and the ETL's
   `for bookmaker in (...): ... break` ([historical_odds_etl.py:206](../historical_odds_etl.py#L206))
   with a loop over `data["bookmakers"]`. The fallback chain is the direct cause of the
   zero FD∩DK overlap.
4. **New table that cannot overwrite itself.** Key on
   `(game_id, player_name, market_key, bookmaker, side, captured_at)`. Keeping
   `captured_at` in the key is what preserves line movement. Do not use
   `INSERT OR REPLACE` on a key that omits it.
5. **Request all three books in one call** using
   `bookmakers=underdog,fanduel,draftkings` instead of `regions=us_dfs`. Underdog sits
   in the `us_dfs` region and FD/DK in `us`; passing two regions multiplies cost by two,
   whereas the documented rule prices up to 10 named bookmakers as one region
   equivalent. Confirm with the probe in §1 before relying on it.

With 1-4 in place, a single nightly pull yields: devigged two-sided probabilities per
book (Test A), the same market at two books for disagreement (Test B), a fixed market
baseline for the residual model (Test D), and the project's first line-movement series.

**Backfill is not an option.** OddsAPI historical endpoints cost 10× a live call. Re-running
the existing 3,489 games at 3 markets would be 3,489 × 3 × 10 ≈ **104,700 credits**
against a 500/month budget. Tests A, B and D are forward-only; the earliest they can be
answered is after a slate count large enough to matter, which at ~7 games/night is one
to two months of logging.

---

## 3. Credit cost per night against 500/month

Documented OddsAPI pricing: the events list is free; an event-odds call costs
`1 × markets × regions`; historical endpoints cost 10×. **Verify with the before/after
`x-requests-remaining` probe in §1** rather than trusting this from memory — the whole
budget decision turns on it.

Real slate sizes from the 2025-26 props table: 200 slate dates, 1,186 games, mean 5.9
games/night (median 6, p75 8, max 14). The binding month is January 2026: 31 dates,
**233 games**, 7.5/night. All "peak month" figures below use 233 games.

| option | markets | books | pulls/game | credits/night (7.5 games) | peak month | fits 500? |
| --- | --- | --- | --- | --- | --- | --- |
| current Underdog config as written | 7 | underdog | 1 | 53 | **1,631** | no — 3.3× over |
| same + FD/DK via `bookmakers` param | 7 | 3 | 1 | 53 | **1,631** | no — 3.3× over |
| Points/Rebounds/Assists | 3 | 3 | 1 | 23 | **699** | no — 1.4× over |
| **Points only** | 1 | 3 | 1 | 7.5 | **233** | yes, 53% headroom |
| **Points only, two pulls** (open + pre-tip) | 1 | 3 | 2 | 15 | **466** | yes, tight |
| P/R/A, capped at 4 games/night | 3 | 3 | 1 | 12 | ~372 | yes |

The 7-market Underdog config is already 3.3× over budget if it ever runs nightly — worth
knowing independently of these tests, since it will start burning credits the moment the
season opens and a user hits the slip endpoint.

### Recommendation

**Points only, all games, two pulls per game day: ~466 credits in the peak month.**

- Points is the deepest market in the historical table (35,798 of 127,257 rows), so it
  is where a calibration result would be most reliable.
- Two pulls are worth more than two extra markets: they give the first line-movement
  data the project has ever had, and closing-line movement is the standard proxy for
  where the sharp money went — which is closer to what Tests A and B are actually
  asking than a wider market list would be.
- January is the only month that strains it. Drop to one pull if the counter runs hot,
  which costs 233 and leaves the series intact.

Cheaper still, if the budget must hold with margin: Points only, one pull, 233/month.
Note the ceiling this budget implies — 500 credits over ~233 games is **2.1 credits per
game**. Any design beyond roughly two markets at one pull per game is out of reach at
this tier, so the lever is narrowing markets, not adding them.

An off-budget alternative is pulling Underdog's own endpoints directly rather than
through OddsAPI, which would be free and would give the actual production line source
instead of a FanDuel proxy. It carries terms-of-service and stability risk and is not
recommended without checking those first.
