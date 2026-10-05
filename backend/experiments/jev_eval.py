"""
jev_eval.py

Scores exported test-set rows with TypeSafe's Jev model via the official
typesafe-sdk Python client, one Noul question per row, and caches every
response in experiments/jev_cache.sqlite.

Docs followed:
  https://docs.typesafe.ai/introduction/quickstart
  https://docs.typesafe.ai/sdk/python
  https://docs.typesafe.ai/primitives/noul
  https://docs.typesafe.ai/concepts/state

Variants:
  anonymized (primary)  14 features + stat type + line. No name, no date, no team.
  named      (secondary) the same, plus player name and game date.

The Noul question wording is byte-identical across variants; only the state differs.

Usage:
  python experiments/jev_eval.py --stat Points --variant anonymized --slice dev --limit 50
  python experiments/jev_eval.py --stat Points --variant anonymized --slice dev --limit 1 --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pandas as pd
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, BACKEND)

load_dotenv(os.path.join(BACKEND, ".env"))

from typesafe_sdk import (  # noqa: E402
    Noul,
    RetryPolicy,
    TypeSafeClient,
    TypeSafeAPIConnectionError,
    TypeSafeAPITimeoutError,
    TypeSafeInternalServerError,
    TypeSafeRateLimitError,
)

# Pinned on purpose — jev-latest/jev-preview are moving aliases
# (https://docs.typesafe.ai/models). The resolved version is recorded per row.
DEFAULT_MODEL = "jev-1.13.0"

CACHE_PATH = os.path.join(HERE, "jev_cache.sqlite")
QUESTION_NAME = "over"
VARIANTS = ("anonymized", "named")

# ml_trainer's terse column names -> descriptive names Jev can read as a
# judgment task. Order is the order they appear in the state.
FEATURE_LABELS = {
    "avg_pts_last5": "average_last_5_games",
    "avg_pts_last10": "average_last_10_games",
    "trend": "recent_form_trend_last5_average_minus_last10_average",
    "consistency": "standard_deviation_over_last_10_games",
    "hit_rate_last10": "share_of_last_10_games_above_his_season_average",
    "vs_opponent_avg": "average_in_prior_games_against_this_opponent",
    "line_diff": "average_last_10_games_minus_betting_line",
    "avg_minutes_last5": "average_minutes_played_last_5_games",
    "minutes_trend": "minutes_trend_last5_average_minus_last10_average",
    "games_played_last15": "games_played_in_the_last_15_days",
    "fatigue_score": "total_minutes_played_in_the_last_7_days",
    "opp_def_rating_last5": "opponent_defensive_rating_last_5_games_lower_is_better",
    "home_away_split": "home_versus_away_scoring_edge_signed_for_this_game",
    "is_playoff": "is_playoff_game",
}

# Frozen question wording. Identical for both variants and every stat type.
QUESTION_TEMPLATE = (
    "This is a single basketball player prop bet. Using the player's performance "
    "profile in the state, judge the over: the player's actual {stat} total in this "
    "game will be strictly greater than the betting line of {line}."
)
CRITERIA_TRUE = (
    "The player finishes the game with a {stat} total strictly greater than {line}. "
    "The over hits."
)
CRITERIA_FALSE = (
    "The player finishes the game with a {stat} total of exactly {line} or less. "
    "The over misses."
)


# --------------------------------------------------------------------------- #
# payload construction
# --------------------------------------------------------------------------- #
def _num(v):
    """JSON-safe number: NaN/inf -> None, floats rounded for a stable payload."""
    if v is None:
        return None
    f = float(v)
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, 3)


def build_state(row: pd.Series, stat: str, variant: str) -> dict:
    features = {}
    for col, label in FEATURE_LABELS.items():
        v = row[col]
        features[label] = bool(v) if col == "is_playoff" else _num(v)

    state: dict = {
        "prop_bet": {
            "stat_category": stat,
            "betting_line": _num(row["real_line"]),
        },
        "player_profile": features,
    }
    if variant == "named":
        state["player_identity"] = {
            "player_name": str(row["player_name"]),
            "game_date": str(pd.Timestamp(row["game_date"]).date()),
        }
    return state


def build_question(row: pd.Series, stat: str) -> Noul:
    line = _num(row["real_line"])
    return Noul(
        instructions=QUESTION_TEMPLATE.format(stat=stat, line=line),
        criteria={
            "true": CRITERIA_TRUE.format(stat=stat, line=line),
            "false": CRITERIA_FALSE.format(stat=stat, line=line),
        },
    )


def fingerprint(state: dict, question: Noul) -> str:
    """Hash of the exact request content, to detect a changed prompt."""
    blob = json.dumps(
        {"state": state, "question": question.model_dump(mode="json")},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# cache
# --------------------------------------------------------------------------- #
def open_cache() -> sqlite3.Connection:
    conn = sqlite3.connect(CACHE_PATH, check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jev_answers (
            stat            TEXT    NOT NULL,
            row_id          INTEGER NOT NULL,
            variant         TEXT    NOT NULL,
            model_version   TEXT    NOT NULL,
            noul            REAL    NOT NULL,
            resolved_model  TEXT,
            eval_slice      TEXT,
            fingerprint     TEXT,
            state_json      TEXT,
            question_json   TEXT,
            input_tokens    INTEGER,
            output_tokens   INTEGER,
            created_at      TEXT,
            PRIMARY KEY (stat, row_id, variant, model_version)
        )
        """
    )
    conn.commit()
    return conn


def cached_keys(conn, stat, variant, model) -> dict:
    rows = conn.execute(
        "SELECT row_id, fingerprint FROM jev_answers "
        "WHERE stat=? AND variant=? AND model_version=?",
        (stat, variant, model),
    ).fetchall()
    return dict(rows)


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #
def ask_with_backoff(client, state, question, model, max_attempts=6):
    """
    One system_one call. The SDK's RetryPolicy already handles 429/5xx with
    backoff; this outer loop is the belt-and-braces layer for the case where
    the policy is exhausted, and it honours the server's retry_after_ms.
    """
    delay = 2.0
    for attempt in range(1, max_attempts + 1):
        try:
            return client.system_one(
                state=state,
                questions={QUESTION_NAME: question},
                model=model,
            )
        except TypeSafeRateLimitError as exc:
            if attempt == max_attempts:
                raise
            wait = (exc.retry_after_ms / 1000.0) if exc.retry_after_ms else delay
            wait += random.uniform(0, 0.5)
            print(f"    rate limited; sleeping {wait:.1f}s (attempt {attempt})")
            time.sleep(wait)
            delay = min(delay * 2, 60.0)
        except (
            TypeSafeInternalServerError,
            TypeSafeAPIConnectionError,
            TypeSafeAPITimeoutError,
        ) as exc:
            if attempt == max_attempts:
                raise
            wait = delay + random.uniform(0, 0.5)
            print(f"    {type(exc).__name__}; sleeping {wait:.1f}s (attempt {attempt})")
            time.sleep(wait)
            delay = min(delay * 2, 60.0)
    raise RuntimeError("unreachable")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stat", required=True, choices=["Points", "Rebounds", "Assists"])
    ap.add_argument("--variant", default="anonymized", choices=VARIANTS)
    ap.add_argument("--slice", dest="eval_slice", default="dev", choices=["dev", "eval"])
    ap.add_argument("--limit", type=int, default=None,
                    help="restrict to the FIRST N rows of the slice (cached or not); "
                         "cached rows in that window are free and not re-scored")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"pinned model (default {DEFAULT_MODEL})")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true", help="print payloads, make no API calls")
    ap.add_argument("--show", type=int, default=1, help="pretty-print the first N payloads")
    args = ap.parse_args()

    path = os.path.join(HERE, f"testset_{args.stat}.parquet")
    if not os.path.exists(path):
        raise SystemExit(f"{path} not found — run export_real_line_testset.py first.")

    df = pd.read_parquet(path)
    df = df[df["eval_slice"] == args.eval_slice].reset_index(drop=True)

    conn = open_cache()
    have = cached_keys(conn, args.stat, args.variant, args.model)

    # --limit means "the first N rows of the slice, cached or not". Cached rows
    # inside that window are free and are simply not re-scored, so re-running the
    # same command never scores anything new and never re-pays.
    scope = df if args.limit is None else df.iloc[: args.limit]

    stale = [r for r in scope["row_id"] if r in have and have[r] is None]
    todo = [i for i, r in enumerate(scope["row_id"]) if r not in have]
    n_cached = len(scope) - len(todo)

    print(f"stat={args.stat}  variant={args.variant}  slice={args.eval_slice}  model={args.model}")
    print(f"  {len(df):,} rows in slice", end="")
    if args.limit is not None:
        print(f" | --limit {args.limit} -> first {len(scope):,} rows", end="")
    print(f" | {n_cached:,} already cached | {len(todo):,} to score")
    if stale:
        print(f"  note: {len(stale)} cached rows have no fingerprint recorded")

    # Show the exact payloads before anything is sent.
    for i in todo[: max(0, args.show)]:
        row = df.iloc[i]
        state = build_state(row, args.stat, args.variant)
        question = build_question(row, args.stat)
        print(f"\n--- request for row_id={int(row['row_id'])} ---")
        print("state =")
        print(json.dumps(state, indent=2))
        print("questions =")
        print(json.dumps({QUESTION_NAME: question.model_dump(mode="json")}, indent=2))
        print(f"fingerprint = {fingerprint(state, question)}")
        print("--- end request ---\n")

    if args.dry_run:
        print("dry run — no API calls made.")
        return
    if not todo:
        print("nothing to do.")
        return

    if not os.getenv("TYPESAFE_API_KEY"):
        raise SystemExit(
            "TYPESAFE_API_KEY is not set. Add it to backend/.env as:\n"
            "    TYPESAFE_API_KEY=ts_..."
        )

    retry = RetryPolicy(
        max_retries=4,
        backoff_initial=1.0,
        backoff_max=30.0,
        respect_retry_after=True,
        timeout=120.0,
    )

    write_lock = threading.Lock()
    counters = {"ok": 0, "err": 0, "in_tok": 0, "out_tok": 0}
    resolved_models: set[str] = set()
    fingerprints: set[str] = set()
    t0 = time.time()

    with TypeSafeClient(retry=retry, timeout=60.0) as client:

        def score(i: int):
            row = df.iloc[i]
            state = build_state(row, args.stat, args.variant)
            question = build_question(row, args.stat)
            fp = fingerprint(state, question)
            resp = ask_with_backoff(client, state, question, args.model)
            return row, state, question, fp, resp

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for result in pool.map(score, todo):
                row, state, question, fp, resp = result
                noul = resp.nouls[QUESTION_NAME].noul
                usage = resp.usage
                with write_lock:
                    conn.execute(
                        "INSERT OR REPLACE INTO jev_answers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            args.stat,
                            int(row["row_id"]),
                            args.variant,
                            args.model,
                            float(noul),
                            resp.model,
                            args.eval_slice,
                            fp,
                            json.dumps(state, separators=(",", ":")),
                            json.dumps(question.model_dump(mode="json"), separators=(",", ":")),
                            getattr(usage, "input_tokens", None),
                            getattr(usage, "output_tokens", None),
                            datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        ),
                    )
                    counters["ok"] += 1
                    counters["in_tok"] += getattr(usage, "input_tokens", 0) or 0
                    counters["out_tok"] += getattr(usage, "output_tokens", 0) or 0
                    resolved_models.add(resp.model)
                    fingerprints.add(fp)
                    if counters["ok"] % 25 == 0:
                        conn.commit()
                        print(f"  {counters['ok']}/{len(todo)} scored")
        conn.commit()

    dt = time.time() - t0
    print(f"\nscored {counters['ok']:,} rows in {dt:.1f}s ({counters['ok']/max(dt,1e-9):.1f}/s)")
    print(f"  resolved model version(s): {sorted(resolved_models)}")
    print(f"  tokens: {counters['in_tok']:,} in / {counters['out_tok']:,} out")
    print(f"  cache: {CACHE_PATH}")
    conn.close()


if __name__ == "__main__":
    main()
