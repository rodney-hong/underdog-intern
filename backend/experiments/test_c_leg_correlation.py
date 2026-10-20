"""
test_c_leg_correlation.py  --  Test C of the market-structure series.

Same-game leg correlation on REAL FanDuel historical lines. Asks whether the
empirical joint hit rate of a 2-leg same-game pair exceeds (a) the 2-leg 3x
break-even of 33.33% and (b) the product of its own marginals.

Protocol is fixed in experiments/preregistration.md. In particular:
  * legs from two DIFFERENT players only (Underdog disallows same-player pairs)
  * DNP legs (0/NULL minutes) dropped -- a real book voids them
  * dev = 2023-24 + 2024-25 picks candidates; eval = 2025-26 is computed once
  * all CIs are cluster bootstrap over gameId; effective n is GAMES, not pairs

Run order:
    python experiments/test_c_leg_correlation.py --slice dev     # selects + freezes
    python experiments/test_c_leg_correlation.py --slice eval    # scores frozen list

Every number produced is FanDuel lines -- a hypothesis for Underdog, never a
recommendation. Touches no production code and writes nothing to the database.
"""

import argparse
import json
import os
import pickle
import sqlite3
import sys
from collections import defaultdict
from itertools import combinations

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import DB_PATH                                             # noqa: E402
from real_lines import build_real_line_lookup, _norm_name, _norm_team    # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FROZEN = os.path.join(HERE, "test_c_frozen_candidates.json")
RESULTS = os.path.join(HERE, "test_c_results.json")
CACHE = os.path.join(
    os.environ.get("TEMP", HERE), "test_c_props_cache.pkl"
)

# ---- pre-registered constants (do not change without a new prereg entry) ----
BREAK_EVEN = 1.0 / 3.0          # 2-leg 3x payout
SEED = 20261005
N_BOOT = 10_000
MIN_DEV_GAMES = 200
TOP_K = 12
CI = 95.0                       # primary
N_CANDIDATES_FOR_BONFERRONI = TOP_K
DEV_SEASONS = ("2023-24", "2024-25")
EVAL_SEASONS = ("2025-26",)
EXCLUDED_GAMETYPES = {"Preseason", "Pre Season", "All-Star Game"}
STAT_COL = {
    "Points": "points",
    "Rebounds": "reboundsTotal",
    "Assists": "assists",
    "3PM": "threePointersMade",
}
NOT_ON_UNDERDOG = {"3PM"}


def season_of(date10: str) -> str:
    """Box-score date -> season label. Matches the observed ranges in the props table."""
    if date10 < "2024-08-01":
        return "2023-24"
    if date10 < "2025-08-01":
        return "2024-25"
    return "2025-26"


# ---------------------------------------------------------------- data build

def build_props(use_cache=True):
    """
    One row per graded prop leg source:
        (game_id, date, season, player, team, stat, line, actual, hit_over)
    Applies the pre-registered DNP and gameType exclusions.
    """
    if use_cache and os.path.exists(CACHE):
        with open(CACHE, "rb") as f:
            props, diag = pickle.load(f)
        print("loaded cached props from " + CACHE)
        return props, diag

    conn = sqlite3.connect(DB_PATH, timeout=30)
    lookup, _ = build_real_line_lookup(conn)

    # box index: (norm_name, date10) -> row fields needed for grading
    box = {}
    for (first, last, dt, gid, gtype, mins, team, opp,
         pts, reb, ast, tpm) in conn.execute(
        "SELECT firstName, lastName, substr(gameDateTimeEst,1,10), gameId, gameType, "
        "       numMinutes, playerteamName, opponentteamName, "
        "       points, reboundsTotal, assists, threePointersMade "
        "FROM player_stats"
    ):
        box[(_norm_name(str(first) + " " + str(last)), dt)] = {
            "game_id": gid, "gameType": gtype, "minutes": mins,
            "team": _norm_team(team), "opp": _norm_team(opp),
            "Points": pts, "Rebounds": reb, "Assists": ast, "3PM": tpm,
        }
    conn.close()

    diag = defaultdict(int)
    props = []
    for (nn, dt, stat), line in lookup.items():
        b = box.get((nn, dt))
        if b is None:
            diag["no_box_row"] += 1
            continue
        if b["gameType"] in EXCLUDED_GAMETYPES:
            diag["excluded_gametype"] += 1
            continue
        mins = b["minutes"]
        if mins is None or float(mins) == 0.0:
            diag["dnp_dropped"] += 1
            continue
        actual = b[stat]
        if actual is None:
            diag["null_actual"] += 1
            continue
        if float(actual) == float(line):
            diag["push_dropped"] += 1
            continue
        props.append({
            "game_id": b["game_id"],
            "date": dt,
            "season": season_of(dt),
            "player": nn,
            "team": b["team"],
            "stat": stat,
            "line": float(line),
            "actual": float(actual),
            "hit_over": float(actual) > float(line),
        })
        diag["kept"] += 1

    with open(CACHE, "wb") as f:
        pickle.dump((props, dict(diag)), f)
    return props, dict(diag)


# ------------------------------------------------- per-game pair aggregation

def aggregate(props, seasons):
    """
    Enumerate same-game, different-player leg pairs over all four side
    combinations and aggregate per (game_id, pair_type).

    Returns {pair_type: {game_id: [n_pairs, both_hit, legA_hit, legB_hit]}}
    pair_type = (relation, legA, legB) with legA <= legB lexicographically.
    """
    by_game = defaultdict(list)
    for p in props:
        if p["season"] in seasons:
            by_game[p["game_id"]].append(p)

    agg = defaultdict(lambda: defaultdict(lambda: [0, 0, 0, 0]))
    for gid, plist in by_game.items():
        for a, b in combinations(plist, 2):
            if a["player"] == b["player"]:
                continue                      # pre-registered exclusion
            relation = "teammate" if a["team"] == b["team"] else "opposing"
            for side_a in ("over", "under"):
                hit_a = a["hit_over"] if side_a == "over" else not a["hit_over"]
                leg_a = a["stat"] + ":" + side_a
                for side_b in ("over", "under"):
                    hit_b = b["hit_over"] if side_b == "over" else not b["hit_over"]
                    leg_b = b["stat"] + ":" + side_b
                    if leg_a <= leg_b:
                        k1, k2, h1, h2 = leg_a, leg_b, hit_a, hit_b
                    else:
                        k1, k2, h1, h2 = leg_b, leg_a, hit_b, hit_a
                    cell = agg[(relation, k1, k2)][gid]
                    cell[0] += 1
                    cell[1] += 1 if (h1 and h2) else 0
                    cell[2] += 1 if h1 else 0
                    cell[3] += 1 if h2 else 0
    return agg


def to_arrays(game_map):
    """{game_id: [n, both, a, b]} -> four aligned numpy arrays over games."""
    vals = np.array(list(game_map.values()), dtype=np.int64)
    return vals[:, 0], vals[:, 1], vals[:, 2], vals[:, 3]


def point_stats(n, both, a, b):
    tot = n.sum()
    joint = both.sum() / tot
    pa = a.sum() / tot
    pb = b.sum() / tot
    return {
        "n_pairs": int(tot),
        "n_games": int(len(n)),
        "joint": float(joint),
        "pA": float(pa),
        "pB": float(pb),
        "indep": float(pa * pb),
        "lift": float(joint - pa * pb),
    }


def cluster_bootstrap(n, both, a, b, levels=(CI,), seed=SEED, reps=N_BOOT):
    """
    Resample GAME ids with replacement; recompute joint and lift from all pairs
    in the resampled games. Returns percentile CIs at each requested level.
    """
    rng = np.random.default_rng(seed)
    g = len(n)
    idx = rng.integers(0, g, size=(reps, g))
    tot = n[idx].sum(axis=1).astype(float)
    joint = both[idx].sum(axis=1) / tot
    pa = a[idx].sum(axis=1) / tot
    pb = b[idx].sum(axis=1) / tot
    lift = joint - pa * pb

    out = {}
    for lv in levels:
        lo_q = (100.0 - lv) / 2.0
        hi_q = 100.0 - lo_q
        out[lv] = {
            "joint_lo": float(np.percentile(joint, lo_q)),
            "joint_hi": float(np.percentile(joint, hi_q)),
            "lift_lo": float(np.percentile(lift, lo_q)),
            "lift_hi": float(np.percentile(lift, hi_q)),
        }
    return out


# -------------------------------------------------------------------- slices

def run_dev(props):
    print()
    print("=" * 96)
    print("TEST C -- DEV SLICE (2023-24 + 2024-25): CANDIDATE SELECTION")
    print("FanDuel lines, hypothesis for Underdog -- not a recommendation")
    print("=" * 96)

    agg = aggregate(props, DEV_SEASONS)
    rows = []
    for ptype, gmap in agg.items():
        n, both, a, b = to_arrays(gmap)
        st = point_stats(n, both, a, b)
        st["pair_type"] = ptype
        rows.append(st)

    eligible = [r for r in rows if r["n_games"] >= MIN_DEV_GAMES]
    eligible.sort(key=lambda r: r["joint"], reverse=True)
    print()
    print("pair types evaluated: {}   eligible (dev n_games >= {}): {}".format(
        len(rows), MIN_DEV_GAMES, len(eligible)))

    print()
    print("--- all eligible pair types, ranked by dev joint hit rate ---")
    print("{:<10}{:<18}{:<18}{:>8}{:>9}{:>8}{:>8}{:>8}{:>8}".format(
        "relation", "legA", "legB", "games", "pairs", "joint", "pA*pB", "lift", "clears"))
    for r in eligible:
        rel, la, lb = r["pair_type"]
        print("{:<10}{:<18}{:<18}{:>8,}{:>9,}{:>8.4f}{:>8.4f}{:>+8.4f}{:>8}".format(
            rel, la, lb, r["n_games"], r["n_pairs"], r["joint"], r["indep"], r["lift"],
            "yes" if r["joint"] > BREAK_EVEN else "no"))

    qualified = [r for r in eligible if r["joint"] > BREAK_EVEN][:TOP_K]
    print()
    print("selected top {} by dev joint among those with dev joint > {:.4f}: {} candidates".format(
        TOP_K, BREAK_EVEN, len(qualified)))

    descriptive_only = len(qualified) == 0
    if descriptive_only:
        # Pre-registration Entry 2: 0 candidates qualified, so the pass test
        # terminates as NO PASS. The top-K by dev joint are carried to eval with the
        # break-even gate removed, as a DESCRIPTIVE out-of-sample check only. These
        # cannot pass -- they already failed the pre-registered gate on dev.
        chosen = eligible[:TOP_K]
        print()
        print("!! 0 candidates cleared the pre-registered dev gate of {:.4f}.".format(BREAK_EVEN))
        print("!! TEST C TERMINATES AS 'NO PASS' AT THE SELECTION STAGE.")
        print("!! Carrying top {} by dev joint to eval as DESCRIPTIVE ONLY "
              "(prereg Entry 2). These cannot pass.".format(TOP_K))
        best = eligible[0]
        need_p = ((BREAK_EVEN - best["lift"]) ** 0.5)
        print("!! best dev joint {:.4f} vs break-even {:.4f}; at the best observed lift "
              "{:+.4f},".format(best["joint"], BREAK_EVEN, best["lift"]))
        print("!! each leg would need a marginal hit rate of {:.4f} to reach break-even."
              .format(need_p))
    else:
        chosen = qualified

    payload = {
        "frozen_at": "dev run",
        "descriptive_only": descriptive_only,
        "rule": "dev n_games >= {}, dev joint > {:.6f}, top {} by dev joint".format(
            MIN_DEV_GAMES, BREAK_EVEN, TOP_K),
        "dev_seasons": list(DEV_SEASONS),
        "eval_seasons": list(EVAL_SEASONS),
        "candidates": [
            {"pair_type": list(r["pair_type"]), "dev": {
                k: r[k] for k in ("n_games", "n_pairs", "joint", "pA", "pB", "indep", "lift")}}
            for r in chosen
        ],
        "all_dev_eligible": [
            {"pair_type": list(r["pair_type"]), **{
                k: r[k] for k in ("n_games", "n_pairs", "joint", "pA", "pB", "indep", "lift")}}
            for r in eligible
        ],
    }
    with open(FROZEN, "w") as f:
        json.dump(payload, f, indent=2)
    print("froze candidate list -> " + FROZEN)
    print()
    print("NEXT: append this list to preregistration.md, THEN run --slice eval.")
    return payload


def run_eval(props):
    if not os.path.exists(FROZEN):
        sys.exit("refusing to run eval: no frozen candidate list at " + FROZEN +
                 "\nrun --slice dev first and record the candidates in preregistration.md")
    with open(FROZEN) as f:
        frozen = json.load(f)
    cands = [tuple(c["pair_type"]) for c in frozen["candidates"]]
    dev_by_type = {tuple(c["pair_type"]): c["dev"] for c in frozen["candidates"]}
    descriptive_only = frozen.get("descriptive_only", False)

    bonf_level = 100.0 * (1.0 - 0.05 / max(1, N_CANDIDATES_FOR_BONFERRONI))

    print()
    print("=" * 110)
    print("TEST C -- EVAL SLICE (2025-26): FROZEN CANDIDATES ONLY")
    print("FanDuel lines, hypothesis for Underdog -- not a recommendation")
    print("=" * 110)
    print("candidates frozen on dev: {}".format(len(cands)))
    if descriptive_only:
        print()
        print("*** DESCRIPTIVE ONLY (prereg Entry 2). 0 pair types cleared the dev gate, so")
        print("*** Test C already terminated as NO PASS at selection. Nothing below can pass;")
        print("*** these numbers only show whether the dev picture holds out of sample.")
        print()
    print("break-even (2-leg 3x): {:.4f}".format(BREAK_EVEN))
    print("CIs: cluster bootstrap over gameId, {:,} reps, seed {}".format(N_BOOT, SEED))
    print("primary {:.0f}% CI; Bonferroni-corrected {:.3f}% CI for {} candidates".format(
        CI, bonf_level, N_CANDIDATES_FOR_BONFERRONI))

    agg = aggregate(props, EVAL_SEASONS)
    out = []
    for ptype in cands:
        gmap = agg.get(ptype)
        if not gmap:
            out.append({"pair_type": ptype, "missing": True})
            continue
        n, both, a, b = to_arrays(gmap)
        st = point_stats(n, both, a, b)
        boot = cluster_bootstrap(n, both, a, b, levels=(CI, bonf_level))
        st["pair_type"] = ptype
        st["ci95"] = boot[CI]
        st["ci_bonf"] = boot[bonf_level]
        st["dev_joint"] = dev_by_type[ptype]["joint"]
        st["pass_95"] = bool(boot[CI]["joint_lo"] > BREAK_EVEN)
        st["pass_bonf"] = bool(boot[bonf_level]["joint_lo"] > BREAK_EVEN)
        st["lift_sig_95"] = bool(boot[CI]["lift_lo"] > 0 or boot[CI]["lift_hi"] < 0)
        st["underdog_actionable"] = not any(
            ptype[i].split(":")[0] in NOT_ON_UNDERDOG for i in (1, 2))
        out.append(st)

    print()
    print("--- EVAL RESULTS (effective n = GAMES) ---")
    print("{:<10}{:<17}{:<17}{:>7}{:>8}{:>8}{:>8}{:>17}{:>8}{:>8}".format(
        "relation", "legA", "legB", "games", "pairs", "devJ", "evalJ",
        "95% CI joint", "lift", "verdict"))
    for r in out:
        rel, la, lb = r["pair_type"]
        if r.get("missing"):
            print("{:<10}{:<17}{:<17}{:>7}".format(rel, la, lb, "ABSENT"))
            continue
        ci = "[{:.4f},{:.4f}]".format(r["ci95"]["joint_lo"], r["ci95"]["joint_hi"])
        if descriptive_only:
            verdict = "no-pass"          # cannot pass: failed the dev gate
        elif r["pass_bonf"]:
            verdict = "PASS"
        elif r["pass_95"]:
            verdict = "marginal"
        else:
            verdict = "fail"
        print("{:<10}{:<17}{:<17}{:>7,}{:>8,}{:>8.4f}{:>8.4f}{:>17}{:>+8.4f}{:>8}".format(
            rel, la, lb, r["n_games"], r["n_pairs"], r["dev_joint"], r["joint"],
            ci, r["lift"], verdict))

    print()
    print("--- correlation term, separated from base rates ---")
    print("{:<10}{:<17}{:<17}{:>8}{:>8}{:>8}{:>19}{:>10}".format(
        "relation", "legA", "legB", "joint", "pA*pB", "lift", "95% CI lift", "corr sig"))
    for r in out:
        if r.get("missing"):
            continue
        rel, la, lb = r["pair_type"]
        ci = "[{:+.4f},{:+.4f}]".format(r["ci95"]["lift_lo"], r["ci95"]["lift_hi"])
        print("{:<10}{:<17}{:<17}{:>8.4f}{:>8.4f}{:>+8.4f}{:>19}{:>10}".format(
            rel, la, lb, r["joint"], r["indep"], r["lift"], ci,
            "yes" if r["lift_sig_95"] else "no"))

    scored = [r for r in out if not r.get("missing")]
    print()
    if descriptive_only:
        best = max(scored, key=lambda r: r["joint"])
        print("TEST C VERDICT: NO PASS (terminated at dev selection; prereg Entry 2).")
        print("best eval joint among the carried 12: {:.4f} 95% CI [{:.4f},{:.4f}]  "
              "vs break-even {:.4f}".format(
                  best["joint"], best["ci95"]["joint_lo"], best["ci95"]["joint_hi"], BREAK_EVEN))
        print("shortfall at the point estimate: {:.4f} ({:.1f} percentage points)".format(
            BREAK_EVEN - best["joint"], 100 * (BREAK_EVEN - best["joint"])))
        print("pair types whose 95% CI upper bound even reaches break-even: {} of {}".format(
            sum(1 for r in scored if r["ci95"]["joint_hi"] > BREAK_EVEN), len(scored)))
        print("pair types with a significantly non-zero lift: {} of {}".format(
            sum(1 for r in scored if r["lift_sig_95"]), len(scored)))
    else:
        print("PASS (Bonferroni-corrected CI clears break-even): {}".format(
            sum(1 for r in scored if r["pass_bonf"])))
        print("marginal (95% only): {}".format(
            sum(1 for r in scored if r["pass_95"] and not r["pass_bonf"])))
        print("fail: {}".format(sum(1 for r in scored if not r["pass_95"])))

    with open(RESULTS, "w") as f:
        json.dump({"break_even": BREAK_EVEN, "seed": SEED, "n_boot": N_BOOT,
                   "descriptive_only": descriptive_only,
                   "ci_primary": CI, "ci_bonferroni": bonf_level,
                   "line_source": "fanduel_historical",
                   "label": "FanDuel lines, hypothesis for Underdog",
                   "results": [{**r, "pair_type": list(r["pair_type"])} for r in out]},
                  f, indent=2)
    print("wrote " + RESULTS)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slice", choices=("dev", "eval"), required=True)
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    props, diag = build_props(use_cache=not args.no_cache)
    print()
    print("--- prop inventory after pre-registered filters ---")
    for k in ("kept", "dnp_dropped", "excluded_gametype", "push_dropped",
              "null_actual", "no_box_row"):
        if k in diag:
            print("  {:<20}{:>8,}".format(k, diag[k]))
    per_season = defaultdict(int)
    for p in props:
        per_season[p["season"]] += 1
    games = defaultdict(set)
    for p in props:
        games[p["season"]].add(p["game_id"])
    print()
    print("  {:<10}{:>10}{:>9}".format("season", "props", "games"))
    for s in sorted(per_season):
        print("  {:<10}{:>10,}{:>9,}".format(s, per_season[s], len(games[s])))

    if args.slice == "dev":
        run_dev(props)
    else:
        run_eval(props)


if __name__ == "__main__":
    main()
