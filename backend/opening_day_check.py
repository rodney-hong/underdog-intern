"""
opening_day_check.py
One-off manual check for opening night (2026-10-20). Run by hand; nothing schedules it.

    python opening_day_check.py                 # first game on today's slate
    python opening_day_check.py --event-id ID   # a specific event
    python opening_day_check.py --no-probe      # never spend the us_dfs probe credit

Checks:
  1. Does Underdog return Points lines in player_points_alternate (the only market
     it uses for them, per the 2026-10-20 probe) with
     line_logger.BOOKMAKERS (10 named books) and line_logger.MARKETS?
  2. Per-call credit cost with the named books (x-requests-last, and the
     x-requests-remaining delta as a cross-check).
  3. If Underdog is missing: one probe call with regions=us_dfs (same markets,
     ~2 credits) to see whether Underdog shows up there and what that costs, then
     projects the monthly total for each way of getting Underdog.

Cost: CREDITS_PER_PULL (2) for check 1-2, +2 for the probe only when Underdog is missing.
Every call goes through line_logger, so it is in odds_pull_log and counts
toward the monthly cap. Rows from check 1 are stored in line_snapshots
(pull_phase='check').
"""

import argparse
import sqlite3

import httpx

import line_logger as L

# 2025-26 games per month (regular season + play-in + playoffs + cup final),
# from the schedule table. Used only for the projection printout.
LAST_SEASON_GAMES = {"Oct": 80, "Nov": 219, "Dec": 198, "Jan": 233, "Feb": 166,
                     "Mar": 239, "Apr": 144, "May": 38}


def _books_in_log(row_id: int) -> tuple[str, int, int, int]:
    conn = sqlite3.connect(L.DB_PATH, timeout=30)
    r = conn.execute(
        "SELECT books_returned, requests_last, remaining_before, remaining_after "
        "FROM odds_pull_log WHERE id = ?", (row_id,)).fetchone()
    conn.close()
    return r


def _last_log_id() -> int:
    conn = sqlite3.connect(L.DB_PATH, timeout=30)
    r = conn.execute("SELECT MAX(id) FROM odds_pull_log").fetchone()[0]
    conn.close()
    return r


def _snapshot_counts(game_id: str) -> list[tuple[str, str, int]]:
    """(bookmaker, market_key, rows) for this game's latest 'check' pull in line_snapshots."""
    conn = sqlite3.connect(L.DB_PATH, timeout=30)
    rows = conn.execute(
        "SELECT bookmaker, market_key, COUNT(*) FROM line_snapshots "
        "WHERE game_id = ? AND pull_phase = 'check' AND captured_at = "
        "(SELECT MAX(captured_at) FROM line_snapshots WHERE game_id = ? AND pull_phase = 'check') "
        "GROUP BY bookmaker, market_key ORDER BY bookmaker, market_key",
        (game_id, game_id)).fetchall()
    conn.close()
    return rows


def _projection(credits_per_pull: int) -> str:
    months = "  ".join(f"{m} {g * L.SCHEDULED_PULLS_PER_GAME * credits_per_pull}"
                       for m, g in LAST_SEASON_GAMES.items())
    peak = max(LAST_SEASON_GAMES.values()) * L.SCHEDULED_PULLS_PER_GAME * credits_per_pull
    verdict = "fits" if peak <= L.MONTHLY_CREDIT_CAP else "OVER"
    return f"{months}   | peak {peak} -> {verdict} {L.MONTHLY_CREDIT_CAP}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--event-id")
    ap.add_argument("--no-probe", action="store_true")
    args = ap.parse_args()

    L.init_tables()
    all_events, remaining = L.fetch_events(phase="check")
    if not all_events:
        print("No events in the OddsAPI feed (or the call failed). Nothing to check.")
        return
    if args.event_id:
        event = next((e for e in all_events if e["id"] == args.event_id), None)
        if event is None:
            print(f"event {args.event_id} not in feed")
            return
    else:
        today = L.todays(all_events)
        if not today:
            print("No games on today's ET slate. Pass --event-id to check a future game.")
            return
        event = today[0]

    print(f"Event: {event['away_team']} @ {event['home_team']}  {event['commence_time']}  "
          f"id={event['id']}")
    print(f"x-requests-remaining before: {remaining}\n")

    # ---- Check 1 + 2: named books (line_logger.BOOKMAKERS), one call
    rows, _ = L.pull_event(event, "check", remaining)
    books, last, before, after = _books_in_log(_last_log_id())
    books_set = set((books or "").split(",")) - {""}
    delta = (before - after) if before is not None and after is not None else None
    counts = _snapshot_counts(event["id"])
    ud_alt = sum(n for b, m, n in counts if b == "underdog" and m == L.UNDERDOG_POINTS_MARKET)
    print(f"\n[1] books returned: {sorted(books_set) or 'none'}   markets={L.MARKETS}")
    for b, m, n in counts:
        print(f"    {b:<11} {m:<25} {n} rows")
    print(f"    Underdog {L.UNDERDOG_POINTS_MARKET} rows: {ud_alt} "
          f"({'YES' if ud_alt else 'NO'})")
    print(f"[2] credit cost, {len(L.BOOKMAKERS.split(','))} named books: "
          f"x-requests-last={last}, remaining delta={delta}")
    if last is not None and last != L.CREDITS_PER_PULL:
        print(f"    !! expected {L.CREDITS_PER_PULL} credits per call; got {last}. "
              f"Projection below uses {last}.")
    per_pull = last or L.CREDITS_PER_PULL
    print(f"    projected ({L.SCHEDULED_PULLS_PER_GAME} pull(s)/game x {per_pull} credits): "
          f"{_projection(per_pull)}")

    if ud_alt:
        print(f"\nPASS: Underdog Points lines are in {L.UNDERDOG_POINTS_MARKET} in the "
              "named-book call. No change needed.")
        return

    # ---- Check 3: Underdog missing -> what would us_dfs cost?
    print("\n[3] Underdog missing from the named-book call.")
    if args.no_probe:
        print(f"    --no-probe: skipping the live us_dfs call. Documented cost is "
              f"{L.CREDITS_PER_PULL} markets x 1 region = {L.CREDITS_PER_PULL} credits per call.")
        dfs_cost, ud_in_dfs = L.CREDITS_PER_PULL, None
    else:
        _, rem = L.fetch_events(phase="check")
        resp = httpx.get(
            L.EVENT_ODDS_URL.format(event_id=event["id"]),
            params={"apiKey": L.ODDS_API_KEY, "regions": "us_dfs",
                    "markets": L.MARKETS, "oddsFormat": "american"},
            timeout=15,
        )
        dfs_data = resp.json() if resp.status_code == 200 else {}
        dfs_books = sorted(b["key"] for b in dfs_data.get("bookmakers", []))
        dfs_cost, _ = L.record_call(resp, endpoint="event_odds", phase="check",
                                    remaining_before=rem, game_id=event["id"],
                                    books=",".join(dfs_books), note="regions=us_dfs probe",
                                    expected_cost=L.CREDITS_PER_PULL)
        ud_book = next((b for b in dfs_data.get("bookmakers", []) if b["key"] == "underdog"), None)
        ud_markets = [m["key"] for m in ud_book.get("markets", [])] if ud_book else []
        print(f"    underdog markets under us_dfs: {ud_markets or 'none'}")
        ud_in_dfs = L.UNDERDOG_POINTS_MARKET in ud_markets
        print(f"    regions=us_dfs probe: HTTP {resp.status_code}, books={dfs_books}, "
              f"cost={dfs_cost} credit(s)")
        print(f"    Underdog present under us_dfs: {'YES' if ud_in_dfs else 'NO'}")

    if ud_in_dfs is False:
        print("\n    Underdog is absent under us_dfs too, so it has most likely not posted "
              "Points for this game yet. Re-run closer to tip before changing anything.")

    three = last or L.CREDITS_PER_PULL
    print(f"\n    Options to get Underdog, projected monthly "
          f"({L.SCHEDULED_PULLS_PER_GAME} pull(s)/game):")
    print(f"    a) keep named-book call, no Underdog  ({three}/pull):  {_projection(three)}")
    print(f"    b) named-book call + separate us_dfs call ({three}+{dfs_cost}/pull): "
          f"{_projection(three + dfs_cost)}")
    print(f"    c) regions=us,us_dfs in one call (~2 regions x {L.CREDITS_PER_PULL} markets = "
          f"{2 * L.CREDITS_PER_PULL}/pull): {_projection(2 * L.CREDITS_PER_PULL)}")
    _print_fallback(three, dfs_cost)
    print("\nNo code was changed. Decide which option to adopt before the next scheduled pull.")


def _print_fallback(three: int, dfs: int) -> None:
    """Which schedule still fits the cap if Underdog needs its own call."""
    peak_games = max(LAST_SEASON_GAMES.values())
    if L.OPEN_PULL_ENABLED:
        configs = [  # (label, credits per game), most Underdog coverage first
            ("both calls at open + pretip", 2 * (three + dfs)),
            ("named-book call at open + pretip, Underdog call at pretip only", 2 * three + dfs),
            ("both calls at pretip only (no open pull; loses line movement)", three + dfs),
        ]
    else:
        configs = [("both calls at pretip (open pull is off)", three + dfs)]
    print("\n    FALLBACK if Underdog requires its own call "
          f"(peak month = {peak_games} games, cap {L.MONTHLY_CREDIT_CAP}):")
    chosen = None
    for label, per_game in configs:
        peak = peak_games * per_game
        reserve = int(-(-peak * (1 + L.RESERVE_BUFFER) // 1))
        fits = peak <= L.MONTHLY_CREDIT_CAP
        print(f"      {'fits' if fits else 'OVER'}  {label}: {per_game}/game -> peak {peak}; "
              f"start-of-month reserve {reserve} -> "
              f"{'room for news pulls' if reserve < L.MONTHLY_CREDIT_CAP else 'no room for news/slip pulls early in the month'}")
        if fits and chosen is None:
            chosen = label
    if chosen:
        print(f"    -> You would need: {chosen}.")
    else:
        print(f"    -> Nothing with Underdog fits; keep the named-book call without Underdog "
              f"({L.SCHEDULED_PULLS_PER_GAME * three}/game, "
              f"peak {peak_games * L.SCHEDULED_PULLS_PER_GAME * three}).")


if __name__ == "__main__":
    main()
