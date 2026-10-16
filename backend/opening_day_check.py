"""
opening_day_check.py
One-off manual check for opening night (2026-10-20). Run by hand; nothing schedules it.

    python opening_day_check.py                 # first game on today's slate
    python opening_day_check.py --event-id ID   # a specific event
    python opening_day_check.py --no-probe      # never spend the us_dfs probe credit

Checks:
  1. Does Underdog appear with bookmakers=underdog,fanduel,draftkings?
  2. Per-call credit cost with three named books (x-requests-last, and the
     x-requests-remaining delta as a cross-check).
  3. If Underdog is missing: one probe call with regions=us_dfs (Points only,
     ~1 credit) to see whether Underdog shows up there and what that costs, then
     projects the monthly total for each way of getting Underdog.

Cost: 1 credit for check 1-2, +1 for the probe only when Underdog is missing.
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
    conn = sqlite3.connect(L.DB_PATH)
    r = conn.execute(
        "SELECT books_returned, requests_last, remaining_before, remaining_after "
        "FROM odds_pull_log WHERE id = ?", (row_id,)).fetchone()
    conn.close()
    return r


def _last_log_id() -> int:
    conn = sqlite3.connect(L.DB_PATH)
    r = conn.execute("SELECT MAX(id) FROM odds_pull_log").fetchone()[0]
    conn.close()
    return r


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

    # ---- Check 1 + 2: three named books, one call
    rows, _ = L.pull_event(event, "check", remaining)
    books, last, before, after = _books_in_log(_last_log_id())
    books_set = set((books or "").split(",")) - {""}
    delta = (before - after) if before is not None and after is not None else None
    print(f"\n[1] books returned: {sorted(books_set) or 'none'}")
    print(f"    Underdog present: {'YES' if 'underdog' in books_set else 'NO'}")
    print(f"[2] credit cost, bookmakers=underdog,fanduel,draftkings: "
          f"x-requests-last={last}, remaining delta={delta}")
    if last is not None and last != 1:
        print(f"    !! expected 1 credit per call; got {last}. Projection below uses {last}.")
    print(f"    projected (2 pulls/game x {last or 1} credit): {_projection(last or 1)}")

    if "underdog" in books_set:
        print("\nPASS: Underdog is in the three-book call. No change needed.")
        return

    # ---- Check 3: Underdog missing -> what would us_dfs cost?
    print("\n[3] Underdog missing from the three-book call.")
    if args.no_probe:
        print("    --no-probe: skipping the live us_dfs call. Documented cost is "
              "1 market x 1 region = 1 credit per call.")
        dfs_cost, ud_in_dfs = 1, None
    else:
        _, rem = L.fetch_events(phase="check")
        resp = httpx.get(
            L.EVENT_ODDS_URL.format(event_id=event["id"]),
            params={"apiKey": L.ODDS_API_KEY, "regions": "us_dfs",
                    "markets": L.MARKETS, "oddsFormat": "american"},
            timeout=15,
        )
        dfs_books = sorted(b["key"] for b in resp.json().get("bookmakers", [])) \
            if resp.status_code == 200 else []
        dfs_cost, _ = L.record_call(resp, endpoint="event_odds", phase="check",
                                    remaining_before=rem, game_id=event["id"],
                                    books=",".join(dfs_books), note="regions=us_dfs probe")
        ud_in_dfs = "underdog" in dfs_books
        print(f"    regions=us_dfs probe: HTTP {resp.status_code}, books={dfs_books}, "
              f"cost={dfs_cost} credit(s)")
        print(f"    Underdog present under us_dfs: {'YES' if ud_in_dfs else 'NO'}")

    if ud_in_dfs is False:
        print("\n    Underdog is absent under us_dfs too, so it has most likely not posted "
              "Points for this game yet. Re-run closer to tip before changing anything.")

    three = last or 1
    print("\n    Options to get Underdog, projected monthly (2 pulls/game):")
    print(f"    a) keep three-book call, no Underdog  ({three}/pull):  {_projection(three)}")
    print(f"    b) three-book call + separate us_dfs call ({three}+{dfs_cost}/pull): "
          f"{_projection(three + dfs_cost)}")
    print(f"    c) regions=us,us_dfs in one call (~2 regions x 1 market = 2/pull): "
          f"{_projection(2)}")
    print("\nNo code was changed. Decide which option to adopt before the next scheduled pull.")


if __name__ == "__main__":
    main()
