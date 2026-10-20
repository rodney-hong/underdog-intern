"""
underdog_probe.py  —  one-off diagnostic, run by hand

Question: why does line_snapshots have no Underdog rows? OddsAPI's bookmaker docs
say Underdog (region us_dfs) puts selections with non-1.0x multipliers in
*_alternate markets, and nothing in this repo requests player_points_alternate.

Does exactly this:
  1. GET the free NBA events list.
  2. Pick one game whose commence_time falls on 2026-10-20 (ET); the earliest,
     unless --event-id is given.
  3. Make ONE event-odds call:
       bookmakers=underdog,fanduel,draftkings
       markets=player_points,player_points_alternate
       oddsFormat=american
     Expected cost: 2 credits (2 markets x 1 region-equivalent).
  4. Save the raw response body to experiments/probe_<UTC timestamp>.json.
  5. Print, for Underdog only: outcomes per market, 5 raw outcome objects per
     market verbatim, and the distinct price values.
  6. Print x-requests-last and x-requests-remaining.

Writes nothing to predictions.db and does not go through line_logger, so these
credits do NOT appear in odds_pull_log. No retries: at most one paid call.

Run (from backend/):
    .venv\\Scripts\\python.exe experiments\\underdog_probe.py
    .venv\\Scripts\\python.exe experiments\\underdog_probe.py --event-id <id>
"""

import argparse
import datetime as dt
import json
import os
import sys
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
load_dotenv(os.path.join(BACKEND, ".env"))

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
EVENTS_URL = "https://api.the-odds-api.com/v4/sports/basketball_nba/events"
EVENT_ODDS_URL = "https://api.the-odds-api.com/v4/sports/basketball_nba/events/{event_id}/odds"

TARGET_DATE = dt.date(2026, 10, 20)  # ET
BOOKMAKERS = "underdog,fanduel,draftkings"
MARKETS = "player_points,player_points_alternate"
SAMPLE_PER_MARKET = 5

EASTERN = ZoneInfo("America/New_York")


def _commence(event: dict) -> dt.datetime:
    return dt.datetime.fromisoformat(event["commence_time"].replace("Z", "+00:00"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--event-id", help="probe this event instead of the earliest 2026-10-20 game")
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
    events = resp.json()

    # 2. Pick one 2026-10-20 (ET) game
    on_date = sorted((e for e in events if _commence(e).astimezone(EASTERN).date() == TARGET_DATE),
                     key=_commence)
    print(f"{len(on_date)} game(s) on {TARGET_DATE} ET:")
    for e in on_date:
        print(f"  {e['id']}  {e['away_team']} @ {e['home_team']}  {e['commence_time']}")
    if args.event_id:
        event = next((e for e in on_date if e["id"] == args.event_id), None)
        if event is None:
            print(f"--event-id {args.event_id} is not a {TARGET_DATE} game in the feed. Nothing spent.")
            return 1
    elif on_date:
        event = on_date[0]
    else:
        print("No game on the target date. Nothing spent.")
        return 1
    print(f"\nprobing: {event['away_team']} @ {event['home_team']}  id={event['id']}")

    # 3. The one paid call
    resp = httpx.get(
        EVENT_ODDS_URL.format(event_id=event["id"]),
        params={
            "apiKey": ODDS_API_KEY,
            "bookmakers": BOOKMAKERS,
            "markets": MARKETS,
            "oddsFormat": "american",
        },
        timeout=20,
    )

    # 4. Save the raw body, whatever the status
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = os.path.join(HERE, f"probe_{stamp}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(resp.text)

    print(f"\nevent-odds: HTTP {resp.status_code}")
    print(f"raw response saved: {out_path}")

    if resp.status_code == 200:
        data = resp.json()
        books = [b.get("key") for b in data.get("bookmakers", [])]
        print(f"bookmakers returned: {books or '(none)'}")
        for b in data.get("bookmakers", []):
            print(f"  {b.get('key')}: markets {[m.get('key') for m in b.get('markets', [])]}")

        # 5. Underdog only
        ud = next((b for b in data.get("bookmakers", []) if b.get("key") == "underdog"), None)
        print("\n=== underdog ===")
        if ud is None:
            print("underdog: NOT present in this response.")
        else:
            print(f"bookmaker last_update: {ud.get('last_update')}")
            markets = ud.get("markets", [])
            if not markets:
                print("(no markets)")
            for m in markets:
                outcomes = m.get("outcomes", [])
                prices = sorted({o.get("price") for o in outcomes},
                                key=lambda p: (p is None, p if isinstance(p, (int, float)) else 0))
                print(f"\n--- {m.get('key')}  (last_update {m.get('last_update')}) ---")
                print(f"outcomes: {len(outcomes)}")
                print(f"first {SAMPLE_PER_MARKET} outcome objects, verbatim:")
                for o in outcomes[:SAMPLE_PER_MARKET]:
                    print("  " + json.dumps(o, ensure_ascii=False))
                print(f"distinct price values ({len(prices)}): {prices}")
    else:
        print(resp.text[:1000])

    # 6. Credit headers for the paid call
    print(f"\nx-requests-last={resp.headers.get('x-requests-last')}  "
          f"x-requests-remaining={resp.headers.get('x-requests-remaining')}  "
          f"x-requests-used={resp.headers.get('x-requests-used')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
