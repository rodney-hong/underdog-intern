"""
schema_audit.py  --  Step 0 of the market-structure experiment series.

Read-only audit of historical_player_props: what columns exist, whether
over/under PRICES exist (needed for Tests A and D), which bookmakers are
present, and how often FanDuel and DraftKings both cover the same
player/game/stat (needed for Test B).

Also sizes the same-game prop pairs available to Test C, using the validated
matchup join in backend/real_lines.py.

Run:  python experiments/schema_audit.py      (from backend/)
Touches no production code and writes nothing to the database.
"""

import os
import sqlite3
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import DB_PATH                                             # noqa: E402
from real_lines import build_real_line_lookup, _norm_name, _norm_team    # noqa: E402

PROPS = "historical_player_props"

PRICE_HINTS = ("price", "odds", "over", "under", "juice", "vig", "payout", "side")


def hdr(t):
    print()
    print("=" * 74)
    print(t)
    print("=" * 74)


def audit_columns(c):
    hdr("1. COLUMNS IN historical_player_props")
    cols = [(r[1], r[2]) for r in c.execute("PRAGMA table_info(" + PROPS + ")")]
    for name, typ in cols:
        print("  {:<14}{}".format(name, typ))
    names = {n.lower() for n, _ in cols}
    price_like = sorted(n for n in names if any(k in n for k in PRICE_HINTS))
    print()
    print("  price/odds/side-like columns: " + (str(price_like) if price_like else "NONE"))
    return names


def audit_books(c):
    hdr("2. BOOKMAKER COVERAGE")
    tot = c.execute("SELECT COUNT(*) FROM " + PROPS).fetchone()[0]
    print("{:<14}{:>10}{:>9}{:>8}{:>8}".format("bookmaker", "rows", "share", "games", "dates"))
    for b, n, g, d in c.execute(
        "SELECT bookmaker, COUNT(*), COUNT(DISTINCT game_id), COUNT(DISTINCT game_date) "
        "FROM " + PROPS + " GROUP BY 1 ORDER BY 2 DESC"
    ):
        print("{:<14}{:>10,}{:>8.1f}%{:>8,}{:>8,}".format(b, n, 100.0 * n / tot, g, d))
    print("{:<14}{:>10,}".format("TOTAL", tot))


def audit_overlap(c):
    hdr("3. FD / DK OVERLAP  (the input Test B needs)")
    print("Unit = (game_id, player_name, stat_type)")
    print("{:<10}{:>8}{:>10}{:>10}".format("stat", "BOTH", "FD only", "DK only"))
    tb = tf = td = 0
    rows = c.execute(
        "SELECT stat_type, "
        "  SUM(has_fd=1 AND has_dk=1), "
        "  SUM(has_fd=1 AND has_dk=0), "
        "  SUM(has_fd=0 AND has_dk=1) "
        "FROM (SELECT game_id, player_name, stat_type, "
        "             MAX(bookmaker='fanduel')    AS has_fd, "
        "             MAX(bookmaker='draftkings') AS has_dk "
        "      FROM " + PROPS + " GROUP BY 1,2,3) "
        "GROUP BY 1 ORDER BY 2 DESC, 1"
    ).fetchall()
    for s, b, f, d in rows:
        tb += b
        tf += f
        td += d
        print("{:<10}{:>8,}{:>10,}{:>10,}".format(s, b, f, d))
    print("{:<10}{:>8,}{:>10,}{:>10,}".format("TOTAL", tb, tf, td))

    print()
    print("Unit = game_id (does any single game carry both books?)")
    bg, fg, dg = c.execute(
        "SELECT SUM(has_fd=1 AND has_dk=1), SUM(has_fd=1 AND has_dk=0), "
        "       SUM(has_fd=0 AND has_dk=1) "
        "FROM (SELECT game_id, MAX(bookmaker='fanduel') AS has_fd, "
        "             MAX(bookmaker='draftkings') AS has_dk "
        "      FROM " + PROPS + " GROUP BY 1)"
    ).fetchone()
    print("  both books: {:,}    FD only: {:,}    DK only: {:,}".format(bg, fg, dg))
    return tb


def audit_dupes(c):
    hdr("4. SNAPSHOTS PER MARKET  (is there line-movement history?)")
    print("{:<36}{:>10}".format("rows per (game,player,stat,book)", "keys"))
    for n, k in c.execute(
        "SELECT n, COUNT(*) FROM (SELECT COUNT(*) AS n FROM " + PROPS +
        " GROUP BY game_id, player_name, stat_type, bookmaker) GROUP BY n ORDER BY n"
    ):
        print("{:<36}{:>10,}".format(n, k))
    batches = c.execute("SELECT COUNT(DISTINCT inserted_at) FROM " + PROPS).fetchone()[0]
    print()
    print("{:<36}{:>10,}".format("distinct inserted_at timestamps", batches))


def audit_scope(c):
    hdr("5. SCOPE")
    print("{:<16}{:<10}{:>9}{:>8}{:>13}{:>13}".format(
        "sport", "season", "rows", "games", "first", "last"))
    for sp, se, n, g, lo, hi in c.execute(
        "SELECT sport, season, COUNT(*), COUNT(DISTINCT game_id), "
        "       MIN(game_date), MAX(game_date) "
        "FROM " + PROPS + " GROUP BY 1,2 ORDER BY 2"
    ):
        print("{:<16}{:<10}{:>9,}{:>8,}{:>13}{:>13}".format(sp, se, n, g, lo, hi))
    print()
    print("{:<12}{:>9}".format("stat_type", "rows"))
    for s, n in c.execute(
        "SELECT stat_type, COUNT(*) FROM " + PROPS + " GROUP BY 1 ORDER BY 2 DESC"
    ):
        print("{:<12}{:>9,}".format(s, n))


def audit_pairs(c):
    """Size Test C: resolved props that share a real game."""
    hdr("6. SAME-GAME PAIR INVENTORY  (the input Test C needs)")
    lookup, _ = build_real_line_lookup(c)
    print("resolved (player, et_date, stat) keys from real_lines.py: {:,}".format(len(lookup)))

    box = {}
    for first, last, d, pt, op in c.execute(
        "SELECT firstName, lastName, substr(gameDateTimeEst,1,10), "
        "playerteamName, opponentteamName FROM player_stats"
    ):
        box[(_norm_name(str(first) + " " + str(last)), d)] = (_norm_team(pt), _norm_team(op))

    games = defaultdict(list)   # (et_date, frozenset(team_nicks)) -> [(player, team, stat)]
    for (nn, d, st) in lookup:
        e = box.get((nn, d))
        if e is None:
            continue
        games[(d, frozenset(e))].append((nn, e[0], st))

    print("distinct games with >=1 resolved prop: {:,}".format(len(games)))
    counts = sorted(len(v) for v in games.values())
    print()
    print("props per game:  median {}   max {}   games with >=2 props {:,}".format(
        counts[len(counts) // 2], counts[-1], sum(1 for n in counts if n >= 2)))

    teammate = opposing = 0
    by_type = Counter()
    for v in games.values():
        for i in range(len(v)):
            for j in range(i + 1, len(v)):
                a, b = v[i], v[j]
                if a[0] == b[0]:
                    continue                      # same player, two stats -- not a leg pair
                same_team = a[1] == b[1]
                if same_team:
                    teammate += 1
                else:
                    opposing += 1
                sa, sb = sorted((a[2], b[2]))
                by_type[("teammate" if same_team else "opposing", sa, sb)] += 1

    print()
    print("distinct two-player pairs in the same game: {:,}".format(teammate + opposing))
    print("  teammate pairs : {:,}".format(teammate))
    print("  opposing pairs : {:,}".format(opposing))
    print()
    print("top pair types (relation, statA, statB):")
    for (rel, sa, sb), n in by_type.most_common(12):
        print("  {:<10}{:<10}{:<10}{:>9,}".format(rel, sa, sb, n))


def main():
    c = sqlite3.connect(DB_PATH, timeout=30)
    print("database: " + DB_PATH)
    names = audit_columns(c)
    audit_books(c)
    both = audit_overlap(c)
    audit_dupes(c)
    audit_scope(c)
    audit_pairs(c)

    hdr("VERDICT ON TEST FEASIBILITY")
    has_price = any(("price" in n) or ("odds" in n) for n in names)
    print("  Test A (devig calibration) : " +
          ("POSSIBLE" if has_price else "IMPOSSIBLE - no price/odds column stored"))
    print("  Test B (cross-book)        : " +
          ("POSSIBLE" if both else "IMPOSSIBLE - 0 FD/DK overlapping markets"))
    print("  Test C (leg correlation)   : POSSIBLE - real lines + box scores both present")
    print("  Test D (market baseline)   : " +
          ("POSSIBLE" if has_price else "IMPOSSIBLE - needs a devigged prob, which needs prices"))
    c.close()


if __name__ == "__main__":
    main()
