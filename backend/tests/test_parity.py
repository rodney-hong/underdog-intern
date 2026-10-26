"""
Parity tooling (migration plan v2, Phase 3): experiments/parity_check.py and
experiments/parity_compare.py on tiny synthetic databases, offline. Covers the outcome-column
exclusion (an outcome column never reaches a hash or an export, and reading one is denied),
the canonical encoding, the network guard, the replay classification and the compare
script's pass/fail logic. The two replay tests read the real frozen model files (read-only)
and are skipped where those files are absent.
"""
import copy
import json
import math
import os
import socket
import sqlite3
import sys

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
sys.path.insert(0, os.path.join(BACKEND, "experiments"))

import parity_check as pc        # noqa: E402
import parity_compare as pcmp    # noqa: E402

DENIED = "prohibited|not authorized"     # the authorizer's error (wording varies by Python version)

_SCHEMA = """
    CREATE TABLE paper_predictions (id INTEGER PRIMARY KEY, created_at TEXT, captured_at TEXT, game_id TEXT,
        game_date TEXT, player_name TEXT, stat_type TEXT, ud_line REAL, status TEXT, model_file TEXT,
        model_sha256 TEXT, feature_def TEXT, p_over REAL, nan_features TEXT, features_json TEXT,
        p_over_v1 REAL, features_v1_json TEXT, actual_points REAL, actual_value REAL, result TEXT,
        resolved_at TEXT);
    CREATE TABLE challenger_predictions (id INTEGER PRIMARY KEY, created_at TEXT, captured_at TEXT,
        game_id TEXT, game_date TEXT, player_name TEXT, stat_type TEXT, line REAL, market_key TEXT,
        status TEXT, n_prior INTEGER, mu REAL, alpha REAL, p_over REAL, expected_minutes REAL,
        rate_per_minute REAL, model_sha256 TEXT, model_fingerprint TEXT, features_json TEXT,
        actual_value REAL, result TEXT, resolved_at TEXT);
    CREATE TABLE predictions (id INTEGER PRIMARY KEY, player_name TEXT, predicted_outcome TEXT,
        confidence REAL, actual_result TEXT);
    CREATE TABLE line_snapshots (game_id TEXT, pull_phase TEXT, captured_at TEXT, point REAL,
        outcome_index INTEGER);
    CREATE TABLE odds_pull_log (id INTEGER PRIMARY KEY, started_at TEXT, game_date TEXT, game_id TEXT,
        endpoint TEXT, pull_phase TEXT, http_status INTEGER, rows_inserted INTEGER);
    CREATE TABLE app_log (id INTEGER PRIMARY KEY, hit_flag INTEGER, was_correct INTEGER, won INTEGER,
        graded_by TEXT, note TEXT);
    CREATE TABLE player_stats (personId INTEGER, points INTEGER, win INTEGER);
    CREATE TABLE news_events (id INTEGER PRIMARY KEY, status TEXT, trigger_result TEXT);
    CREATE TABLE other_odds (outcome_index INTEGER, win INTEGER);
"""


def _db(path):
    c = sqlite3.connect(path)
    c.executescript(_SCHEMA)
    c.execute("INSERT INTO paper_predictions (created_at, captured_at, game_id, game_date, player_name, "
              "stat_type, ud_line, status, p_over, features_json, actual_points, actual_value, result, resolved_at) "
              "VALUES ('t0', 'c1', 'g1', '2026-10-25', 'A Player', 'Points', 20.5, 'ok', 0.61, '{}', 25, 25, "
              "'OVER', 'r1')")
    c.execute("INSERT INTO challenger_predictions (created_at, captured_at, game_id, game_date, player_name, "
              "stat_type, line, market_key, status, mu, actual_value, result, resolved_at) VALUES "
              "('t0', 'c1', 'g1', '2026-10-25', 'A Player', 'Points', 20.5, 'player_points', 'ok', 22.1, 25, "
              "'OVER', 'r1')")
    c.execute("INSERT INTO predictions (player_name, predicted_outcome, confidence, actual_result) "
              "VALUES ('A Player', 'OVER', 0.6, 'OVER')")
    c.execute("INSERT INTO line_snapshots VALUES ('g1', 'pretip', 'c1', 20.5, 0)")
    c.execute("INSERT INTO app_log (hit_flag, was_correct, won, graded_by, note) VALUES (1, 1, 1, 'x', 'n')")
    c.execute("INSERT INTO player_stats VALUES (1, 25, 1)")
    c.execute("INSERT INTO news_events (status, trigger_result) VALUES ('Out', 'not sent: EVENT_TRIGGERS_ENABLED is off')")
    c.execute("INSERT INTO other_odds VALUES (0, 1)")
    c.commit()
    c.close()


@pytest.fixture
def syn(tmp_path):
    path = str(tmp_path / "syn.db")
    _db(path)
    conn = pc.connect_ro(path, set())
    classes = pc.outcome_columns(conn)
    conn.close()
    pc.DENIED.clear()
    return path, classes


def _set(path, sql, *params):
    c = sqlite3.connect(path)
    c.execute(sql, params)
    c.commit()
    c.close()


# --- outcome-column exclusion ----------------------------------------------------------------

def test_outcome_columns_found_by_name_and_listed_for_review(syn):
    _, classes = syn
    ex = {t: set(d["excluded"]) for t, d in classes.items()}
    review = {t: set(d["review"]) for t, d in classes.items()}
    assert ex["paper_predictions"] == {"actual_points", "actual_value", "result", "resolved_at"}
    assert ex["challenger_predictions"] == {"actual_value", "result", "resolved_at"}
    assert ex["predictions"] == {"actual_result"}
    assert ex["app_log"] == {"hit_flag", "was_correct", "won", "graded_by"}
    assert review["app_log"] == {"was_correct"}                              # word match, not a name rule
    # the reviewed inputs (ALLOWED_INPUTS) are hashed; the allow-list is per table and column
    for t in ("line_snapshots", "player_stats", "news_events", "odds_pull_log"):
        assert ex[t] == set(), t
    assert ex["other_odds"] == {"outcome_index", "win"} and review["other_odds"] == {"win"}
    assert {("line_snapshots", "outcome_index"), ("player_stats", "win"), ("predictions", "predicted_outcome"),
            ("news_events", "trigger_result")} == set(pc.ALLOWED_INPUTS)
    for kept in ("p_over", "features_json", "mu", "line", "confidence", "note"):
        assert pc.classify_column("t", kept) is None
    assert pc.classify_column("predictions", "actual_result")[0] == "outcome"   # explicit names win


def test_allowed_inputs_are_hashed(syn):
    path, classes = syn
    blocked = pc.blocked_reads(classes)
    before = pc.run_integrity(path, classes, blocked)["tables"]
    assert "predicted_outcome" in before["predictions"]["hashed_columns"]
    for t, c, sql in (("line_snapshots", "outcome_index", "UPDATE line_snapshots SET outcome_index = 1"),
                      ("player_stats", "win", "UPDATE player_stats SET win = 0"),
                      ("predictions", "predicted_outcome", "UPDATE predictions SET predicted_outcome = 'UNDER'"),
                      ("news_events", "trigger_result", "UPDATE news_events SET trigger_result = 'pulled: 3 rows'")):
        assert c in before[t]["hashed_columns"]
        _set(path, sql)
        after = pc.run_integrity(path, classes, blocked)["tables"]
        assert after[t]["sha256"] != before[t]["sha256"], t
    assert pc.DENIED == []


def test_an_outcome_column_never_reaches_a_table_hash(syn):
    path, classes = syn
    blocked = pc.blocked_reads(classes)
    before = pc.run_integrity(path, classes, blocked)
    assert before["ok"]
    for t in ("paper_predictions", "challenger_predictions"):
        assert not {"actual_value", "result", "resolved_at"} & set(before["tables"][t]["hashed_columns"])
    _set(path, "UPDATE paper_predictions SET actual_points = 3, actual_value = 3, result = 'UNDER', resolved_at = 'r2'")
    _set(path, "UPDATE challenger_predictions SET actual_value = NULL, result = 'DNP', resolved_at = NULL")
    _set(path, "UPDATE predictions SET actual_result = 'UNDER'")
    after = pc.run_integrity(path, classes, blocked)
    assert {t: v["sha256"] for t, v in after["tables"].items()} == {t: v["sha256"] for t, v in before["tables"].items()}
    _set(path, "UPDATE paper_predictions SET p_over = 0.62")
    changed = pc.run_integrity(path, classes, blocked)
    assert changed["tables"]["paper_predictions"]["sha256"] != before["tables"]["paper_predictions"]["sha256"]
    assert pc.DENIED == []


def test_reading_an_outcome_column_is_denied(syn):
    path, classes = syn
    conn = pc.connect_ro(path, pc.blocked_reads(classes))
    for sql in ("SELECT actual_value FROM paper_predictions", "SELECT * FROM challenger_predictions",
                "SELECT COUNT(*) FROM predictions WHERE actual_result IS NOT NULL",
                "SELECT outcome_index FROM other_odds"):
        with pytest.raises(sqlite3.DatabaseError, match=DENIED):
            conn.execute(sql)
    assert conn.execute("SELECT p_over FROM paper_predictions").fetchone() == (0.61,)
    assert conn.execute("SELECT outcome_index FROM line_snapshots").fetchone() == (0,)
    conn.close()
    # the same set on connections the code under test opens (DbGuard)
    with pc.DbGuard([], pc.blocked_reads(classes), ro_ok=True):
        c = sqlite3.connect(path)
        assert c.execute("SELECT win FROM player_stats").fetchone() == (1,)
        with pytest.raises(sqlite3.DatabaseError, match=DENIED):
            c.execute("SELECT result FROM paper_predictions")
        c.close()


def test_an_outcome_column_never_reaches_an_export(syn):
    path, classes = syn
    conn = pc.connect_ro(path, pc.blocked_reads(classes))
    keys = [("g1", "c1")]
    before = {t: pc.export_rows(conn, t, classes, keys) for t in pc.E2E_ORDER}
    conn.close()
    for t, e in before.items():
        assert e["n"] == 1
        assert not set(e["columns"]) & ({"id", "created_at"} | set(classes[t]["excluded"]))
        assert "features_json" in e["columns"] or t == "challenger_predictions"
    _set(path, "UPDATE paper_predictions SET actual_value = 0, result = 'UNDER', resolved_at = 'later'")
    _set(path, "UPDATE challenger_predictions SET actual_value = 0, result = 'UNDER'")
    conn = pc.connect_ro(path, pc.blocked_reads(classes))
    after = {t: pc.export_rows(conn, t, classes, keys) for t in pc.E2E_ORDER}
    conn.close()
    assert after == before
    assert "OVER" not in json.dumps(before) and "UNDER" not in json.dumps(after)


def test_db_guard_refuses_a_writable_connection_to_a_protected_file(syn):
    path, classes = syn
    with pc.DbGuard([path], set(), ro_ok=True) as g:
        with pytest.raises(PermissionError):
            sqlite3.connect(path)
        sqlite3.connect(pc.ro_uri(path), uri=True).close()
    with pc.DbGuard([path], set(), ro_ok=False) as g2:
        with pytest.raises(PermissionError):
            sqlite3.connect(pc.ro_uri(path), uri=True)
    assert len(g.refused) == 1 and len(g2.refused) == 1


# --- canonical encoding ----------------------------------------------------------------------

def test_canonical_encoding_keeps_types_and_bits_apart():
    enc = pc.encode_value
    assert enc(None) == b"N" and enc("") == b"T0:" and enc(None) != enc("")
    assert len({enc(1), enc(1.0), enc("1"), enc(b"1")}) == 4
    assert enc(1.0) == b"F0x1.0000000000000p+0;"
    assert enc(0.1) == b"F" + (0.1).hex().encode() + b";"
    assert enc(0.0) != enc(-0.0)
    assert enc(0.1 + 0.2) != enc(0.3)                    # one ulp apart, still different
    assert enc("é") == b"T2:\xc3\xa9"                    # UTF-8
    assert enc(pc._Text("ab".encode())) == enc("ab") != enc(b"ab")
    assert enc(2 ** 63 - 1) == b"I9223372036854775807;"
    assert pc.encode_row(("ab", "c")) != pc.encode_row(("a", "bc"))
    assert pc.encode_row((None, "")) != pc.encode_row(("", None))
    with pytest.raises(TypeError):
        enc(True)


def test_table_digest_is_a_fixed_value(tmp_path):
    """Same bytes on every platform: this constant must also hold on the Linux server."""
    path = str(tmp_path / "enc.db")
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE t (i INTEGER, r REAL, s TEXT, b BLOB, n)")
    c.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?)",
                  [(1, 0.1, "é", b"\x00\xff", None), (-7, -0.0, "", b"", 3.5), (2 ** 40, 1e300, "ab", b"ab", "x")])
    c.commit()
    c.close()
    conn = pc.connect_ro(path, set(), text_bytes=True)
    n, digest = pc.table_digest(conn, "t", ["i", "r", "s", "b", "n"])
    conn.close()
    assert n == 3
    # derived by hand from the spec (header, then I1;F<0.1 hex>;T2:<é UTF-8>B2:<00 ff>N\n ...);
    # SQLite stores -0.0 in a REAL column as 0.0, so row 2 hashes 0x0.0p+0
    assert digest == "9c721b0b698f7436708423684f006df6fd217705571bf0a955ff19a7f52cfaa3"


# --- network guard ---------------------------------------------------------------------------

def test_network_guard_blocks_and_reports_every_attempt():
    import httpx
    before = socket.getaddrinfo
    with pc.NetworkGuard() as g:
        with pytest.raises(pc.NetworkBlocked):
            socket.create_connection(("api.the-odds-api.com", 443))
        with pytest.raises(pc.NetworkBlocked):
            socket.getaddrinfo("www.kaggle.com", 443)
        s = socket.socket()
        with pytest.raises(pc.NetworkBlocked):
            s.connect(("127.0.0.1", 11434))              # local Ollama: loopback is blocked too
        s.close()
        with pytest.raises(Exception):
            httpx.get("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries", timeout=2)
    assert socket.getaddrinfo is before
    calls = [a["call"] for a in g.attempts]
    assert calls[:3] == ["create_connection", "getaddrinfo", "connect"] and len(calls) >= 4
    assert "11434" in g.attempts[2]["target"]


# --- replay classification -------------------------------------------------------------------

def test_classify_exact_last_digit_mismatch():
    x = 0.6123456789
    assert pc.classify(x, x) == "exact"
    assert pc.classify(x, math.nextafter(x, 1)) == "last_digit"
    assert pc.ulps(x, math.nextafter(math.nextafter(x, 1), 1)) == 2
    assert pc.classify(x, x + 1e-9) == "mismatch"
    assert pc.classify(0.0, -0.0) == "last_digit"        # bitwise different, numerically equal
    assert pc.classify(None, None) == "exact" and pc.classify(x, None) == "mismatch"
    t = pc.FieldTally()
    t.add(1, x, x, {})
    t.add(2, x, math.nextafter(x, 0), {})
    t.add(3, x, 0.7, {})
    o = t.out()
    assert (o["exact"], o["last_digit"], o["mismatch"], o["last_digit_max_ulps"]) == (1, 1, 1, 1)
    assert [r["id"] for r in o["mismatch_rows"]] == [3] and o["last_digit_rows"][0]["recomputed"] == math.nextafter(x, 0).hex()


def test_prereg_hashes_attribute_each_hash_to_its_file(tmp_path):
    p = tmp_path / "prereg.md"
    a, b, c, d = ("a" * 64, "b" * 64, "c" * 64, "d" * 64)
    p.write_text(f"## Frozen\n| `model_Points.pkl` | LR | `{a}` |\n"
                 f"- Model file: backend/challenger/models/challenger_stage2_frozen.pkl.\n"
                 f"  - File SHA-256: {b}.\n  - Content fingerprint (model.fingerprint): {c}.\n"
                 f"## Later\nstray hash {d}\n", encoding="utf-8")
    got = pc.prereg_hashes(str(p))
    assert [e["sha256"] for e in got["files"]["model_Points.pkl"]] == [a]
    assert [e["sha256"] for e in got["files"]["challenger_stage2_frozen.pkl"]] == [b]
    assert [e["sha256"] for e in got["fingerprints"]["challenger_stage2_frozen.pkl"]] == [c]
    assert d not in json.dumps(got)                      # no file named since the heading


def _paper_row(path, rid, p_over, feats, feats_v1=None, p_v1=None):
    _set(path, "INSERT INTO paper_predictions (id, captured_at, game_id, game_date, player_name, stat_type, "
               "ud_line, status, model_file, model_sha256, feature_def, p_over, features_json, p_over_v1, "
               "features_v1_json, result) VALUES (?, 'c9', 'g9', '2026-10-25', ?, 'Points', 20.5, 'ok', "
               "'model_Points.pkl', ?, 'v2', ?, ?, ?, ?, 'OVER')",
         rid, f"P{rid}", pc_sha(), p_over, json.dumps(feats), p_v1, json.dumps(feats_v1) if feats_v1 else None)


def pc_sha():
    import paper_predictions
    return paper_predictions.STATS["Points"]["sha256"]


@pytest.mark.skipif(not os.path.exists(os.path.join(BACKEND, "model_Points.pkl")), reason="frozen model absent")
def test_replay_paper_classifies_rows(syn):
    import numpy as np
    import pandas as pd
    import paper_predictions
    from ml_trainer import FEATURE_COLS
    path, classes = syn
    model, _ = paper_predictions._load_model("Points")
    feats = {c: float(i) + 0.25 for i, c in enumerate(FEATURE_COLS)}
    feats[FEATURE_COLS[3]] = None                        # a NaN feature, filled with 0 as live
    X = pd.DataFrame([{k: (np.nan if v is None else v) for k, v in feats.items()}])[FEATURE_COLS].fillna(0).astype(float)
    p = float(model.predict_proba(X)[0][1])
    _set(path, "DELETE FROM paper_predictions")
    _paper_row(path, 1, p, feats, feats, p)
    _paper_row(path, 2, math.nextafter(p, 1), feats)
    _paper_row(path, 3, p + 1e-6, feats)
    out = pc.run_replay_paper(path, pc.blocked_reads(classes))
    f = out["fields"]["p_over"]
    assert (f["checked"], f["exact"], f["last_digit"], f["mismatch"]) == (3, 1, 1, 1)
    assert [r["id"] for r in f["mismatch_rows"]] == [3]
    assert out["fields"]["p_over_v1"]["exact"] == 1 and out["recomputed"]["1"]["p_over"] == p.hex()
    assert not out["ok"] and pc.DENIED == []


@pytest.mark.skipif(not os.path.exists(os.path.join(BACKEND, "challenger", "models", "challenger_stage2_frozen.pkl")),
                    reason="frozen challenger absent")
def test_replay_challenger_matches_and_skips_insufficient_history(syn):
    import pandas as pd
    from challenger import live
    path, classes = syn
    m = live.load_frozen()
    names = list(dict.fromkeys(m.minutes_feats + m.rate_feats["Points"]))
    feats = {k: 0.5 + 0.01 * i for i, k in enumerate(names)}
    feats["n_prior"] = 30.0                              # a model feature too; >= live.MIN_PRIOR
    feats[next(k for k in names if k != "n_prior")] = None
    vals = {k: (float("nan") if v is None else v) for k, v in feats.items()}
    r = live.predict_from_features(pd.DataFrame([vals]), "Points", [20.5], m)
    _set(path, "DELETE FROM challenger_predictions")
    ins = ("INSERT INTO challenger_predictions (id, captured_at, game_id, game_date, player_name, stat_type, line, "
           "market_key, status, n_prior, mu, alpha, p_over, expected_minutes, rate_per_minute, model_sha256, "
           "model_fingerprint, features_json) VALUES (?, 'c9', 'g9', '2026-10-25', 'P', 'Points', 20.5, 'm', ?, "
           "30, ?, ?, ?, ?, ?, ?, ?, ?)")
    _set(path, ins, 1, "ok", r["mu"], r["alpha"], r["p_over"][20.5], r["expected_minutes"], r["rate"],
         live.FROZEN["sha256"], live.FROZEN["fingerprint"], json.dumps(feats))
    _set(path, ins, 2, "insufficient_history", None, None, None, None, None, None, None, None)
    out = pc.run_replay_challenger(path, pc.blocked_reads(classes))
    assert out["insufficient_history_skipped"] == 1 and out["rows_rescored"] == 1
    assert all(v["exact"] == 1 and v["mismatch"] == 0 for v in out["fields"].values()), out["fields"]
    assert out["ok"]


# --- end-to-end helpers ----------------------------------------------------------------------

def test_resolve_pulls_maps_a_pull_to_its_capture(syn):
    path, _ = syn
    _set(path, "INSERT INTO line_snapshots VALUES ('g2', 'pretip', '2026-10-25T23:00:00.1+00:00', 1, 0)")
    _set(path, "INSERT INTO line_snapshots VALUES ('g2', 'pretip', '2026-10-25T23:05:00.1+00:00', 1, 0)")
    for pid, started, status, n in ((7, "2026-10-25T23:00:02+00:00", 200, 10), (8, "2026-10-25T23:05:02+00:00", 200, 10),
                                    (9, "2026-10-25T23:09:00+00:00", 429, 0)):
        _set(path, "INSERT INTO odds_pull_log VALUES (?, ?, '2026-10-25', 'g2', 'event_odds', 'pretip', ?, ?)",
             pid, started, status, n)
    conn = pc.connect_ro(path, set())
    pulls, errors = pc.resolve_pulls(conn, "2026-10-25", [7, 8, 9, 99])
    assert [(p["pull_id"], p["captured_at"], p["latest_pretip_capture"]) for p in pulls] == [
        (7, "2026-10-25T23:00:00.1+00:00", False), (8, "2026-10-25T23:05:00.1+00:00", True)]
    assert len(errors) == 2 and any("99" in e for e in errors)
    assert [p["pull_id"] for p in pc.resolve_pulls(conn, "2026-10-25", None)[0]] == [7, 8]
    conn.close()


def test_pinned_clock_answers_with_the_pull_time():
    import datetime as dt
    import types
    clock = pc.PinnedClock()
    mod = types.SimpleNamespace(dt=clock.module)
    clock.pinned = dt.datetime(2026, 10, 25, 23, 0, 1, tzinfo=dt.timezone.utc)
    et = mod.dt.datetime.now(dt.timezone(dt.timedelta(hours=-4)))
    assert (et.hour, et.minute, et.date().isoformat()) == (19, 0, "2026-10-25")
    assert mod.dt.datetime.now(dt.timezone.utc).isoformat() == "2026-10-25T23:00:01+00:00"
    clock.pinned = None
    assert abs(mod.dt.datetime.now(dt.timezone.utc) - dt.datetime.now(dt.timezone.utc)).total_seconds() < 5


# --- packages vs the lock file ---------------------------------------------------------------

def test_packages_vs_lock(tmp_path):
    lock = tmp_path / "requirements-lock.txt"
    lock.write_text("# comment\nscikit-learn==1.8.0\nTyping_Extensions==4.15.0  # inline\n"
                    "zope.interface==8.0\nwinonly==1.0 ; sys_platform == 'nonexistent'\n\n", encoding="utf-8")
    installed = {pc.norm_name(k): v for k, v in
                 {"scikit_learn": "1.8.0", "typing-extensions": "4.15.0", "Zope-Interface": "8.0",
                  "colorama": "0.4.6", "pip": "25.0"}.items()}
    r = pc.packages_vs_lock(installed, str(lock))
    assert r["pass"] and r["pins"] == 3 and r["not_required_by_marker"] == {"winonly": "1.0"}
    assert {x["name"]: x["note"] for x in r["extra"]} == {"colorama": pc.WINDOWS_ONLY["colorama"], "pip": None}
    r = pc.packages_vs_lock({**installed, "scikit-learn": "1.8.1"}, str(lock))
    assert not r["pass"] and r["wrong_version"] == [{"name": "scikit-learn", "lock": "1.8.0", "installed": "1.8.1"}]
    r = pc.packages_vs_lock({k: v for k, v in installed.items() if k != "zope-interface"}, str(lock))
    assert not r["pass"] and r["missing"] == [{"name": "zope-interface", "lock": "8.0"}]
    lock.write_text("numpy>=2\n", encoding="utf-8")
    assert pc.packages_vs_lock(installed, str(lock))["unparsed_lines"] == ["line 1: numpy>=2"]


# --- compare ---------------------------------------------------------------------------------

def _result(path, classes):
    blocked = pc.blocked_reads(classes)
    tally = pc.FieldTally()
    tally.add(1, 0.5, 0.5, {})
    replay = {"fields": {"p_over": tally.out()}, "skipped_by_status": {"x": 1}, "recomputed": {"1": {"p_over": (0.5).hex()}}}
    lock = {"lock_file": "requirements-lock.txt", "pins": 1, "missing": [], "wrong_version": [],
            "unparsed_lines": [], "extra": [], "pass": True}
    return {"meta": {"tool_version": 2, "encoding": "v1", "git_commit": "abc", "git_dirty": False,
                     "python_version": "3.14.4", "packages": {"numpy": "2.4.4"}, "packages_vs_lock": lock,
                     "host": "h", "platform": "p"},
            "exclusions": {t: {"excluded": d["excluded"], "review": d["review"]} for t, d in classes.items()},
            "network": {"attempts": []}, "denied_reads": [],
            "integrity": pc.run_integrity(path, classes, blocked),
            "models": {"files": {"model_Points.pkl": {"sha256": "a" * 64, "status": "OK"}},
                       "code_pins": {"model_Points.pkl": {"matches_prereg": True}},
                       "code_fingerprint_pin_matches_prereg": True},
            "replay_paper": replay}


def _fails(a, b):
    return [n for s, n, _ in pcmp.compare(a, b).checks if s == "FAIL"]


def test_compare_passes_identical_and_fails_each_difference(syn, tmp_path):
    path, classes = syn
    a = _result(path, classes)
    assert _fails(a, copy.deepcopy(a)) == []

    def changed(fn):
        b = copy.deepcopy(a)
        fn(b)
        return _fails(a, b)

    assert changed(lambda b: b["integrity"]["tables"]["paper_predictions"].update(sha256="0" * 64)) == \
        ["integrity: table paper_predictions (1 rows, 17 columns)"]
    assert changed(lambda b: b["integrity"]["tables"]["predictions"].update(rows=2))
    # installed lists are never compared with each other; platform fields are INFO
    assert changed(lambda b: b["meta"]["packages"].update(numpy="2.4.5", uvloop="0.21.0")) == []
    assert changed(lambda b: b["meta"].update(platform="Linux-x86_64", host="srv")) == []
    assert changed(lambda b: b["meta"]["packages_vs_lock"]["extra"].append(
        {"name": "colorama", "version": "0.4.6", "note": "Windows-only"})) == []
    assert changed(lambda b: b["meta"]["packages_vs_lock"].update(
        {"pass": False, "wrong_version": [{"name": "numpy", "lock": "2.4.4", "installed": "2.4.5"}]})) == \
        ["packages: every requirements-lock.txt line installed at its version (server: 1 pins)"]
    assert changed(lambda b: b["meta"].update(python_version="3.14.3")) == ["meta: same Python version"]
    assert changed(lambda b: b["meta"].update(git_commit="def")) == ["meta: same git commit"]
    assert changed(lambda b: b["network"]["attempts"].append({"call": "connect"}))
    assert changed(lambda b: b["denied_reads"].append("paper_predictions.result"))
    assert changed(lambda b: b["models"]["files"]["model_Points.pkl"].update(status="MISMATCH"))
    assert changed(lambda b: b["models"]["files"]["model_Points.pkl"].update(sha256="b" * 64))
    assert changed(lambda b: b.pop("replay_paper")) == ["replay_paper: present in both files"]
    assert changed(lambda b: b["replay_paper"]["recomputed"]["1"].update(p_over=(0.25).hex())) == \
        ["replay_paper: recomputed values bitwise identical across machines (1 rows)"]

    def mismatch(b):
        t = pc.FieldTally()
        t.add(1, 0.5, 0.6, {})
        b["replay_paper"]["fields"]["p_over"] = t.out()
    assert changed(mismatch)

    def last_digit(b):
        t = pc.FieldTally()
        t.add(1, 0.5, math.nextafter(0.5, 1), {})
        b["replay_paper"]["fields"]["p_over"] = t.out()
    assert changed(last_digit) == ["replay_paper: p_over has no last-digit difference (server)"]

    # exit codes through main()
    fa, fb = tmp_path / "a.json", tmp_path / "b.json"
    fa.write_text(json.dumps(a), encoding="utf-8")
    fb.write_text(json.dumps(a), encoding="utf-8")
    assert pcmp.main([str(fa), str(fb)]) == 0
    b = copy.deepcopy(a)
    b["integrity"]["integrity_check"] = ["*** in database main ***"]
    fb.write_text(json.dumps(b), encoding="utf-8")
    assert pcmp.main([str(fa), str(fb)]) == 1


def test_compare_end_to_end_export_diff(syn):
    path, classes = syn
    conn = pc.connect_ro(path, pc.blocked_reads(classes))
    exp = {t: pc.export_rows(conn, t, classes, [("g1", "c1")]) for t in pc.E2E_ORDER}
    conn.close()
    base = _result(path, classes)
    a = {**base, "end_to_end": {"date": "2026-10-25", "errors": [], "refused_connections": [],
                                "temp_dir_removed": True,
                                "pulls": [{"pull_id": 1, "game_id": "g1", "captured_at": "c1"}], "export": exp}}
    assert _fails(a, copy.deepcopy(a)) == []
    b = copy.deepcopy(a)
    i = b["end_to_end"]["export"]["paper_predictions"]["columns"].index("p_over")
    b["end_to_end"]["export"]["paper_predictions"]["rows"][0][i] = {"float": (0.62).hex()}
    b["end_to_end"]["export"]["paper_predictions"]["sha256"] = "0" * 64
    r = pcmp.compare(a, b)
    fail = [(n, d) for s, n, d in r.checks if s == "FAIL"]
    assert [n for n, _ in fail] == ["end-to-end: paper_predictions export identical"]
    assert any("p_over" in d and "0.62" in d for d in fail[0][1])
