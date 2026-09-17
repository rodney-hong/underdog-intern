"""
check_underdog_markets.py  —  THROWAWAY DIAGNOSTIC (safe to delete)

Makes one us_dfs player-props call for a single upcoming event and reports:
  * which bookmaker keys OddsAPI returned
  * for "underdog" specifically, every market key present and its outcome count

Purpose: confirm which of our 16 stat types Underdog actually carries BEFORE we
trust the auto-fill. If Underdog doesn't carry a market (e.g. player_steals or
player_blocks_steals), OddsAPI may 422 the WHOLE request rather than silently
omitting it — so this script prints the raw status/body on failure and then
re-probes each market one at a time to name the offender.

Run:  python check_underdog_markets.py               # defaults to WNBA
      python check_underdog_markets.py --league nba   # NBA (may have no games)
"""

import os
import argparse
import datetime as dt

import httpx
from dotenv import load_dotenv

load_dotenv()

_ODDS_API_KEY    = os.getenv("ODDS_API_KEY", "")
_SPORT_KEYS      = {"nba": "basketball_nba", "wnba": "basketball_wnba"}
_EVENTS_URL_TMPL = "https://api.the-odds-api.com/v4/sports/{sport}/events"
_PROPS_URL_TMPL  = "https://api.the-odds-api.com/v4/sports/{sport}/events/{event_id}/odds"

# Every candidate player-prop market covering our 16 stat types (the same OddsAPI
# market keys apply to both basketball_nba and basketball_wnba). We request them
# all at once so we can see full Underdog coverage in a single call.
_CANDIDATE_MARKETS = [
    "player_points",
    "player_rebounds",
    "player_assists",
    "player_threes",
    "player_blocks",
    "player_steals",
    "player_blocks_steals",
    "player_turnovers",
    "player_points_rebounds_assists",
    "player_points_rebounds",
    "player_points_assists",
    "player_rebounds_assists",
    "player_double_double",
    "player_triple_double",
    "player_field_goals",
    "player_frees_made",
]


def _pick_upcoming_event(sport: str) -> dict | None:
    resp = httpx.get(
        _EVENTS_URL_TMPL.format(sport=sport),
        params={"apiKey": _ODDS_API_KEY}, timeout=10,
    )
    resp.raise_for_status()
    events = resp.json()
    if not events:  # off-season / no scheduled games — return None, don't index
        return None
    now = dt.datetime.now(dt.timezone.utc)
    upcoming = []
    for ev in events:
        try:
            commence = dt.datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        except Exception:
            continue
        if commence >= now:
            upcoming.append((commence, ev))
    if upcoming:
        upcoming.sort(key=lambda x: x[0])
        return upcoming[0][1]
    return events[0]  # fall back to whatever exists


def _props_request(sport: str, event_id: str, markets: str) -> httpx.Response:
    return httpx.get(
        _PROPS_URL_TMPL.format(sport=sport, event_id=event_id),
        params={
            "apiKey":     _ODDS_API_KEY,
            "regions":    "us_dfs",
            "markets":    markets,
            "oddsFormat": "american",
        },
        timeout=15,
    )


def _report_underdog(data: dict) -> None:
    bookmakers = data.get("bookmakers", [])
    keys = [b.get("key") for b in bookmakers]
    print(f"\nBookmaker keys returned: {keys or '(none)'}")

    ud = next((b for b in bookmakers if b.get("key") == "underdog"), None)
    if not ud:
        print("underdog: NOT present in this response.")
        return

    print("\nunderdog markets present:")
    markets = ud.get("markets", [])
    if not markets:
        print("  (no markets)")
    for m in markets:
        print(f"  {m.get('key'):<34} outcomes={len(m.get('outcomes', []))}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--league", choices=sorted(_SPORT_KEYS), default="wnba",
        help="which league to probe (default: wnba)",
    )
    args = parser.parse_args()
    league = args.league
    sport = _SPORT_KEYS[league]

    if not _ODDS_API_KEY:
        print("ODDS_API_KEY is not set — populate .env before running.")
        return

    event = _pick_upcoming_event(sport)
    if not event:
        print(f"No {league.upper()} events returned by OddsAPI right now "
              f"(off-season or no games scheduled). Nothing to probe.")
        return

    print(f"[{league.upper()}] Event: {event.get('away_team')} @ {event.get('home_team')}  "
          f"({event.get('commence_time')})  id={event['id']}")

    # Combined request across all candidate markets.
    resp = _props_request(sport, event["id"], ",".join(_CANDIDATE_MARKETS))
    print(f"\nCombined request status: {resp.status_code}")

    if resp.status_code == 200:
        _report_underdog(resp.json())
        return

    # Non-200 (likely 422) — show the raw body, then find the offending market(s).
    print("Response body:")
    print(resp.text)

    print("\nProbing each market individually to identify the offender(s)...")
    for mk in _CANDIDATE_MARKETS:
        try:
            r = _props_request(sport, event["id"], mk)
        except Exception as exc:
            print(f"  {mk:<34} ERROR {exc}")
            continue
        if r.status_code == 200:
            ud = next((b for b in r.json().get("bookmakers", [])
                       if b.get("key") == "underdog"), None)
            n = len(ud.get("markets", [{}])[0].get("outcomes", [])) if ud and ud.get("markets") else 0
            carried = "underdog carries it" if ud else "OK but no underdog"
            print(f"  {mk:<34} 200  ({carried}, outcomes={n})")
        else:
            print(f"  {mk:<34} {r.status_code}  <-- rejected")


if __name__ == "__main__":
    main()
