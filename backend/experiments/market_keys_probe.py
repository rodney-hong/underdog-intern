"""
market_keys_probe.py  —  one-off diagnostic, run by hand

Question: which market keys do underdog, fanduel and draftkings list for an NBA
game? Confirms the keys line_logger.STAT_MARKETS requests (expected:
player_points, player_rebounds, player_assists, player_threes and their
_alternate versions).

Does exactly this:
  1. GET the free NBA events list (0 credits).
  2. Pick the earliest game that has not started, unless --event-id is given.
  3. Make ONE event-markets call (1 credit):
       GET /v4/sports/basketball_nba/events/{id}/markets
       bookmakers=line_logger.BOOKMAKERS (the same 10 named books)
  4. Save the raw response body to experiments/probe_markets_<UTC timestamp>.json.
  5. Print every market key per book for underdog, fanduel, draftkings, then the
     expected keys present/missing per book, then the other books' keys.
  6. Print x-requests-last and x-requests-remaining.

Writes nothing to predictions.db and does not go through line_logger, so the
credit does NOT appear in odds_pull_log. No retries: at most one paid call.
The endpoint lists markets a book has recently offered for the event, so run it
on a game with props posted (game day, a few hours before tip).

Run (from backend/):
    .venv\\Scripts\\python.exe experiments\\market_keys_probe.py
    .venv\\Scripts\\python.exe experiments\\market_keys_probe.py --event-id <id>
"""

import argparse
import datetime as dt
import json
import os
import sys

import httpx
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
load_dotenv(os.path.join(BACKEND, ".env"))

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
EVENTS_URL = "https://api.the-odds-api.com/v4/sports/basketball_nba/events"
EVENT_MARKETS_URL = "https://api.the-odds-api.com/v4/sports/basketball_nba/events/{event_id}/markets"

# Same 10 books as line_logger.BOOKMAKERS (copied, so this script imports nothing
# that touches the database).
BOOKMAKERS = ("underdog,fanduel,draftkings,prizepicks,novig,prophetx,kalshi,"
              "betmgm,espnbet,betrivers")
FOCUS_BOOKS = ("underdog", "fanduel", "draftkings")
EXPECTED = [k for base in ("player_points", "player_rebounds", "player_assists", "player_threes")
            for k in (base, base + "_alternate")]


def _commence(event: dict) -> dt.datetime:
    return dt.datetime.fromisoformat(event["commence_time"].replace("Z", "+00:00"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--event-id", help="probe this event instead of the earliest unstarted game")
    args = ap.parse_args()

    if not ODDS_API_KEY:
        print("ODDS_API_KEY is not set in backend/.env. Nothing called.")
        return 1

    # 1. Free events list
    resp = httpx.get(EVENTS_URL, params={"apiKey": ODDS_API_KEY}, timeout=15)
    print(f"events: HTTP {resp.status_code}, "
          f"x-requests-last={resp.headers.get('x-requests-last')}, "
          f"x-requests-remaining={resp.headers.get('x-requests-remaining')}")
    if resp.status_code != 200:
        print(resp.text[:500])
        return 1
    now = dt.datetime.now(dt.timezone.utc)
    upcoming = sorted((e for e in resp.json() if _commence(e) > now), key=_commence)

    # 2. Pick one game
    if args.event_id:
        event = next((e for e in upcoming if e["id"] == args.event_id), None)
        if event is None:
            print(f"--event-id {args.event_id} is not an unstarted game in the feed. Nothing spent.")
            return 1
    elif upcoming:
        event = upcoming[0]
    else:
        print("No unstarted NBA game in the feed. Nothing spent.")
        return 1
    print(f"probing: {event['away_team']} @ {event['home_team']}  "
          f"{event['commence_time']}  id={event['id']}")

    # 3. The one paid call
    resp = httpx.get(EVENT_MARKETS_URL.format(event_id=event["id"]),
                     params={"apiKey": ODDS_API_KEY, "bookmakers": BOOKMAKERS},
                     timeout=20)

    # 4. Save the raw body, whatever the status
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = os.path.join(HERE, f"probe_markets_{stamp}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(resp.text)
    print(f"\nevent-markets: HTTP {resp.status_code}")
    print(f"raw response saved: {out_path}")

    if resp.status_code == 200:
        books = {b.get("key"): sorted(m.get("key") for m in b.get("markets", []))
                 for b in resp.json().get("bookmakers", [])}
        # 5. Market keys per book
        for book in FOCUS_BOOKS:
            print(f"\n=== {book} ===")
            if book not in books:
                print("NOT present in this response.")
                continue
            print(f"{len(books[book])} market keys:")
            for k in books[book]:
                print(f"  {k}")
        print("\n=== expected keys ===")
        print(f"{'key':<28}" + "".join(f"{b:>12}" for b in FOCUS_BOOKS))
        for k in EXPECTED:
            print(f"{k:<28}" + "".join(f"{('yes' if k in books.get(b, []) else '-'):>12}"
                                       for b in FOCUS_BOOKS))
        others = {b: ks for b, ks in books.items() if b not in FOCUS_BOOKS}
        if others:
            print("\n=== other books (player_* keys only) ===")
            for b, ks in sorted(others.items()):
                print(f"  {b}: {[k for k in ks if k.startswith('player_')]}")
    else:
        print(resp.text[:1000])

    # 6. Credit headers for the paid call
    print(f"\nx-requests-last={resp.headers.get('x-requests-last')}  "
          f"x-requests-remaining={resp.headers.get('x-requests-remaining')}  "
          f"x-requests-used={resp.headers.get('x-requests-used')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
