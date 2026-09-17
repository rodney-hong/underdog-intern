"""
real_lines.py
Matchup-based join between historical_player_props (real book lines, stored with a
UTC game_date) and player_stats (real box scores, ET-dated).

Because historical_player_props keeps only a UTC *date* (commence_time[:10]) and no
tip-off time, a blanket "-1 day" shift is ambiguous for back-to-backs. Instead we
resolve each prop row to a single box-score game using:
    normalized player name
  + a +/-1 day window around game_date
  + the team matchup (player's team + opponent == prop home/away pair, either orientation)

Team names differ between tables (player_stats splits city/nickname, e.g. "LA"/"Clippers";
historical stores full "Los Angeles Clippers"), so teams are compared on nickname only.

Public API:
  build_real_line_lookup(conn=None) -> (lookup, report)
      lookup: {(norm_name, et_date_str, stat_type): real_line}
      report: dict of resolution counts (see validate/main)
Run directly to print the validation report:  python real_lines.py
"""

import sqlite3
import unicodedata
from datetime import datetime, timedelta

from database import DB_PATH

# Stat types for which real book lines exist in historical_player_props.
REAL_LINE_STATS = ("Points", "Rebounds", "Assists", "3PM")


def _norm_name(n) -> str:
    if n is None:
        return ""
    s = unicodedata.normalize("NFKD", str(n)).encode("ascii", "ignore").decode()
    return " ".join(s.lower().split())


def _norm_team(n) -> str:
    if n is None:
        return ""
    s = unicodedata.normalize("NFKD", str(n)).encode("ascii", "ignore").decode()
    return " ".join(s.lower().split())


def _load_indexes(conn: sqlite3.Connection):
    """
    box:      (norm_name, date10) -> (team_nick, opp_nick)
    teamdate: set of (team_nick, date10)     -- for back-to-back detection
    nicks:    player_stats nicknames, longest first (for suffix matching)
    """
    box: dict = {}
    teamdate: set = set()
    nick_set: set = set()
    for first, last, d, pt_name, op_name in conn.execute(
        "SELECT firstName, lastName, substr(gameDateTimeEst,1,10), "
        "playerteamName, opponentteamName FROM player_stats"
    ):
        tn = _norm_team(pt_name)
        on = _norm_team(op_name)
        box[(_norm_name(f"{first} {last}"), d)] = (tn, on)
        teamdate.add((tn, d))
        if tn:
            nick_set.add(tn)
    nicks = sorted(nick_set, key=len, reverse=True)  # longest first: "hornets" before "nets"
    return box, teamdate, nicks


def _hist_nick(full_name: str, nicks: list) -> str | None:
    """Map a historical full team name ('Los Angeles Clippers') to a player_stats nickname."""
    nf = _norm_team(full_name)
    for nk in nicks:  # longest-first prevents 'hornets' -> 'nets' style collisions
        if nf.endswith(nk):
            return nk
    return None


def _resolve_dates(norm_name, d_utc, prop_pair, box):
    """
    Return the list of box-score dates within +/-1 day of the UTC game_date whose
    player+matchup agree with the prop. prop_pair is a frozenset of the two nicknames.
    """
    hits = []
    for delta in (-1, 0, 1):
        c = (d_utc + timedelta(days=delta)).isoformat()
        entry = box.get((norm_name, c))
        if entry is None:
            continue
        if frozenset(entry) == prop_pair:
            hits.append(c)
    return hits


def build_real_line_lookup(conn: sqlite3.Connection | None = None):
    """
    Resolve every historical_player_props row (Points/Rebounds/Assists/3PM) to a single
    box-score game via the matchup + /-1 day window. Returns:
      lookup  : {(norm_name, et_date_str, stat_type): mean real_line}
      report  : resolution counts, incl. validation of the old blanket -1 shift.
    Only rows that resolve to exactly one game contribute to `lookup`.
    """
    close = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        close = True

    box, teamdate, nicks = _load_indexes(conn)

    # accumulate lines per resolved key (multiple books/snapshots -> mean)
    acc: dict = {}

    # report counters
    from collections import defaultdict
    rep = {
        "prev_ambiguous_total": 0,   # matched under -1 shift AND team played both D-1 & D
        "prev_ambiguous_resolved": 0,
        "prev_ambiguous_still_multi": 0,
        "prev_ambiguous_unmatched": 0,
        "prev_clean_total": 0,       # matched under -1 shift, team played only D-1
        "prev_clean_consistent": 0,  # matchup resolves to exactly D-1
        "prev_clean_contradiction": 0,  # matchup resolves to a single date != D-1
        "prev_clean_unresolved": 0,  # matchup couldn't confirm (0 or >1)
        "resolved_per_stat": defaultdict(int),
        "unresolved_per_stat": defaultdict(int),
    }

    for player_name, game_date, stat_type, line, home_team, away_team in conn.execute(
        "SELECT player_name, game_date, stat_type, line, home_team, away_team "
        "FROM historical_player_props"
    ):
        if stat_type not in REAL_LINE_STATS or line is None:
            continue
        try:
            d_utc = datetime.strptime(game_date, "%Y-%m-%d").date()
        except Exception:
            continue

        nn = _norm_name(player_name)
        ph = _hist_nick(home_team, nicks)
        pa = _hist_nick(away_team, nicks)
        if ph is None or pa is None:
            rep["unresolved_per_stat"][stat_type] += 1
            continue
        prop_pair = frozenset((ph, pa))

        d_prev = (d_utc - timedelta(days=1)).isoformat()

        # --- previous blanket -1 shift classification (for validation reporting) ---
        prev_entry = box.get((nn, d_prev))
        if prev_entry is not None:
            prev_team = prev_entry[0]
            played_orig = (prev_team, game_date) in teamdate
            is_prev_ambiguous = played_orig
        else:
            is_prev_ambiguous = None  # not matched under old shift at all

        # --- matchup resolution ---
        hits = _resolve_dates(nn, d_utc, prop_pair, box)

        # validation bookkeeping
        if prev_entry is not None:
            if is_prev_ambiguous:
                rep["prev_ambiguous_total"] += 1
                if len(hits) == 1:
                    rep["prev_ambiguous_resolved"] += 1
                elif len(hits) > 1:
                    rep["prev_ambiguous_still_multi"] += 1
                else:
                    rep["prev_ambiguous_unmatched"] += 1
            else:
                rep["prev_clean_total"] += 1
                if len(hits) == 1:
                    if hits[0] == d_prev:
                        rep["prev_clean_consistent"] += 1
                    else:
                        rep["prev_clean_contradiction"] += 1
                else:
                    rep["prev_clean_unresolved"] += 1

        # --- assign to lookup only if resolved to exactly one game ---
        if len(hits) == 1:
            key = (nn, hits[0], stat_type)
            acc.setdefault(key, []).append(float(line))
            rep["resolved_per_stat"][stat_type] += 1
        else:
            rep["unresolved_per_stat"][stat_type] += 1

    if close:
        conn.close()

    lookup = {k: sum(v) / len(v) for k, v in acc.items()}
    rep["resolved_per_stat"] = dict(rep["resolved_per_stat"])
    rep["unresolved_per_stat"] = dict(rep["unresolved_per_stat"])
    return lookup, rep


def main() -> None:
    lookup, rep = build_real_line_lookup()

    print("=== matchup join validation ===")
    a_tot = rep["prev_ambiguous_total"]
    print(f"\nPreviously-ambiguous rows (blanket -1 shift, team played BOTH D-1 and D): {a_tot:,}")
    print(f"  now resolve to exactly one game : {rep['prev_ambiguous_resolved']:,}")
    print(f"  still ambiguous (matchup on >1)  : {rep['prev_ambiguous_still_multi']:,}")
    print(f"  unmatched (no matchup in window) : {rep['prev_ambiguous_unmatched']:,}")

    c_tot = rep["prev_clean_total"]
    print(f"\nPreviously-\"clean\" rows (blanket -1 shift, team played only D-1): {c_tot:,}")
    print(f"  matchup CONSISTENT with -1 shift : {rep['prev_clean_consistent']:,}")
    print(f"  matchup CONTRADICTS -1 shift     : {rep['prev_clean_contradiction']:,}  <-- silently-wrong count")
    print(f"  matchup could not confirm (0/>1) : {rep['prev_clean_unresolved']:,}")

    print("\n=== final resolved real-line rows per stat type ===")
    tot_r = tot_u = 0
    print(f"{'stat':<10}{'resolved':>10}{'unresolved':>12}")
    for st in REAL_LINE_STATS:
        r = rep["resolved_per_stat"].get(st, 0)
        u = rep["unresolved_per_stat"].get(st, 0)
        tot_r += r; tot_u += u
        print(f"{st:<10}{r:>10,}{u:>12,}")
    print(f"{'TOTAL':<10}{tot_r:>10,}{tot_u:>12,}")
    print(f"\nunique (player,date,stat) keys in lookup: {len(lookup):,}")


if __name__ == "__main__":
    main()
