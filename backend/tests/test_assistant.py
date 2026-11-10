"""
The read-only Ask assistant (assistant.py, POST /assistant). Temp DB; the LLM is mocked
(no network). The paper / challenger tables carry sentinel outcome values that must never
reach a tool's output or any SQL the tools run.
"""
import hashlib
import json
import math
import os
import re
import sqlite3
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import assistant  # noqa: E402
import database  # noqa: E402
import predictor  # noqa: E402

DATE = "2026-11-09"
SENTINEL = 987654.25            # actual_value / actual_points in every paper / challenger row
SENTINEL_TEXT = "SENTINEL_OUTCOME"

PRED_COLS = ("timestamp", "player_name", "stat_type", "stat_line", "opponent_team", "predicted_outcome",
             "confidence", "explanation", "actual_result", "game_date", "league", "is_limited_data", "model",
             "is_current")
OUTCOME_SQL = re.compile(r"\b(actual_value|actual_points|result|resolved_at|status)\b", re.I)


def _pred(player, stat, pick, actual, date, *, model="challenger", line=3.5, conf=0.6):
    return (f"{date}T15:00:00", player, stat, line, "Boston Celtics", pick, conf, "stored explanation", actual,
            date, "NBA", 0, model, 1)


def _features(**over):
    f = {c: 1.0 for c in predictor.FEATURE_COLS}
    f.update(opp_game_count=3, line_hits_last10=5, games_last10=10, **over)
    return f


def _p_over(stat, feats):
    X = pd.DataFrame([feats])[predictor.FEATURE_COLS].fillna(0).astype(float)
    return float(predictor._load_models()[stat].predict_proba(X)[0][1])


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "assistant.db")
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(database, "backfill_limited_data", lambda conn: {})
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE predictions (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, "
                 "player_name TEXT NOT NULL, stat_type TEXT NOT NULL, stat_line REAL NOT NULL, opponent_team TEXT NOT NULL, "
                 "predicted_outcome TEXT NOT NULL, confidence REAL NOT NULL, explanation TEXT NOT NULL, actual_result TEXT)")
    conn.commit()
    conn.close()
    database.init_db()

    conn = sqlite3.connect(path)
    rows = [
        # resolved history: Wendell Carter Jr. Assists challenger 3-1, frozen 1-0; Kim 1-1
        _pred("Wendell Carter Jr.", "Assists", "OVER", "OVER", "2026-11-01"),
        _pred("Wendell Carter Jr.", "Assists", "OVER", "OVER", "2026-11-02"),
        _pred("Wendell Carter Jr.", "Assists", "UNDER", "UNDER", "2026-11-03"),
        _pred("Wendell Carter Jr.", "Assists", "OVER", "UNDER", "2026-11-04"),
        _pred("Wendell Carter Jr.", "Assists", "OVER", "OVER", "2026-11-04", model="frozen"),
        _pred("Kim Lee", "Assists", "OVER", "OVER", "2026-11-01"), _pred("Kim Lee", "Assists", "OVER", "UNDER", "2026-11-02"),
        # Steady Eddie Rebounds: 18-2 over 20 picks, enough for the record to count in ranking
        *[_pred("Steady Eddie", "Rebounds", "OVER", "OVER" if i < 18 else "UNDER", f"2026-10-{i + 1:02d}")
          for i in range(20)],
        # pending on DATE
        _pred("Wendell Carter Jr.", "Assists", "OVER", None, DATE, conf=0.63),
        _pred("Kim Lee", "3PM", "UNDER", None, DATE, line=1.5, conf=0.58),
        _pred("Kim Lee", "Assists", "UNDER", None, DATE, model="challenger_retro"),   # never listed
        _pred("Nikola Jokić", "Rebounds", "OVER", None, DATE, line=11.5, conf=0.57),
        _pred("Steady Eddie", "Rebounds", "OVER", None, DATE, line=7.5, conf=0.55),
        _pred("Zed Big", "Points", "UNDER", None, DATE, line=2.5, conf=0.99),     # implausible: flagged
        # pending on an earlier date (the reported bug: not found when only "today" was searched)
        _pred("Ja Morant", "3PM", "UNDER", None, "2026-10-29", line=1.5, conf=0.61),
    ]
    conn.executemany(f"INSERT INTO predictions ({', '.join(PRED_COLS)}) VALUES ({', '.join('?' * len(PRED_COLS))})", rows)
    ids = dict(conn.execute("SELECT player_name || '|' || stat_type, id FROM predictions WHERE actual_result IS NULL "
                            "AND model != 'challenger_retro'").fetchall())

    outcome_cols = "status TEXT, actual_points REAL, actual_value REAL, result TEXT, resolved_at TEXT"
    conn.execute(f"CREATE TABLE paper_predictions (id INTEGER PRIMARY KEY, created_at TEXT, captured_at TEXT, "
                 f"game_date TEXT, player_name TEXT, stat_type TEXT, ud_line REAL, feature_def TEXT, features_json TEXT, "
                 f"p_over REAL, model_file TEXT, model_sha_ok INTEGER, data_as_of TEXT, {outcome_cols})")
    conn.execute(f"CREATE TABLE paper_predictions_pre_stat_key AS SELECT * FROM paper_predictions")
    conn.execute(f"CREATE TABLE challenger_predictions (id INTEGER PRIMARY KEY, captured_at TEXT, game_date TEXT, "
                 f"player_name TEXT, stat_type TEXT, line REAL, market_key TEXT, n_prior INTEGER, mu REAL, alpha REAL, "
                 f"p_over REAL, expected_minutes REAL, rate_per_minute REAL, data_as_of TEXT, features_json TEXT, "
                 f"{outcome_cols})")
    ast_f, tpm_f = _features(line_diff=0.8, avg_pts_last5=4.2), _features(line_diff=-0.4)
    paper = [("2026-11-09T22:00:00+00:00", "Wendell Carter Jr.", "Assists", 3.5, ast_f),
             ("2026-11-09T18:00:00+00:00", "Wendell Carter Jr.", "Assists", 3.5, _features()),   # older: not used
             ("2026-11-09T22:00:00+00:00", "Kim Lee", "3PM", 1.5, tpm_f)]
    for cap, player, stat, line, f in paper:
        conn.execute("INSERT INTO paper_predictions (created_at, captured_at, game_date, player_name, stat_type, ud_line, "
                     "feature_def, features_json, p_over, model_file, model_sha_ok, data_as_of, status, actual_points, "
                     "actual_value, result, resolved_at) VALUES (?, ?, ?, ?, ?, ?, 'v2', ?, ?, 'm.pkl', 1, '2026-11-08', "
                     "?, ?, ?, ?, ?)",
                     (cap, cap, DATE, player, stat, line, json.dumps(f), _p_over(stat, f), SENTINEL_TEXT, SENTINEL,
                      SENTINEL, SENTINEL_TEXT, SENTINEL_TEXT))
    conn.execute("INSERT INTO challenger_predictions (captured_at, game_date, player_name, stat_type, line, market_key, "
                 "n_prior, mu, alpha, p_over, expected_minutes, rate_per_minute, data_as_of, features_json, status, "
                 "actual_points, actual_value, result, resolved_at) VALUES ('2026-11-09T22:00:00+00:00', ?, "
                 "'Wendell Carter Jr', 'Assists', 3.5, 'player_assists', 300, 3.0, 0.1, 0.42, 30.0, 0.1, '2026-11-08', "
                 "'{}', ?, ?, ?, ?, ?)", (DATE, SENTINEL_TEXT, SENTINEL, SENTINEL, SENTINEL_TEXT, SENTINEL_TEXT))

    conn.execute("CREATE TABLE rosters (athlete_id TEXT, player_name TEXT, name_key TEXT, team_abbrev TEXT, "
                 "team_city TEXT, team_name TEXT)")
    conn.executemany("INSERT INTO rosters VALUES (?, ?, ?, ?, ?, ?)", [
        ("1", "Wendell Carter Jr.", "wendell carter", "ORL", "Orlando", "Magic"),
        ("2", "Paolo Banchero", "paolo banchero", "ORL", "Orlando", "Magic"),
        ("3", "Jaylen Brown", "jaylen brown", "BOS", "Boston", "Celtics")])
    conn.execute("CREATE TABLE news_events (id INTEGER PRIMARY KEY, method TEXT, kind TEXT, player TEXT, team TEXT, "
                 "status TEXT, previous_status TEXT, source_time TEXT, game_date TEXT, matchup TEXT)")
    conn.executemany("INSERT INTO news_events (method, kind, player, team, status, previous_status, source_time, "
                     "game_date, matchup) VALUES ('deterministic', 'row', ?, ?, ?, ?, ?, ?, 'BOS@ORL')", [
        ("Wendell Carter Jr.", "Orlando Magic", "questionable", None, "2026-11-09T17:00:00+00:00", DATE),
        ("Wendell Carter Jr.", "Orlando Magic", "available", "questionable", "2026-11-09T21:00:00+00:00", DATE),
        ("Paolo Banchero", "Orlando Magic", "out", None, "2026-11-09T17:00:00+00:00", DATE),
        ("Paolo Banchero", "Orlando Magic", "out", None, "2026-11-01T17:00:00+00:00", "2026-11-01"),   # old game
        ("Jaylen Brown", "Boston Celtics", "probable", None, "2026-11-09T17:00:00+00:00", DATE)])
    conn.execute("CREATE TABLE news_items (id INTEGER PRIMARY KEY, source TEXT, raw_text TEXT, published_at TEXT, "
                 "fetched_at TEXT)")
    conn.execute("INSERT INTO news_items (source, raw_text, published_at, fetched_at) VALUES ('espn_injuries', "
                 "'Orlando Magic | Paolo Banchero | Out | Banchero will miss Sunday''s game. | Groin.', ?, ?)",
                 (pd.Timestamp.utcnow().isoformat(), pd.Timestamp.utcnow().isoformat()))
    conn.commit()
    conn.close()
    return {"path": path, "ids": ids, "ast_f": ast_f, "tpm_f": tpm_f}


def _all_tool_outputs(ids):
    return [assistant.list_pending(), assistant.list_pending(stat="Assists"), assistant.list_pending(date=DATE),
            assistant.list_pending(player="ja morant", stat="threes"), assistant.list_pending(player="Nobody Here"),
            assistant.track_record("Wendell Carter Jr.", "Assists"),
            assistant.track_record("wendell carter", "Assists", model="all"),
            assistant.explain(ids["Wendell Carter Jr.|Assists"]), assistant.explain(ids["Kim Lee|3PM"]),
            assistant.injury_context(player="Wendell Carter Jr."), assistant.injury_context(game="BOS@ORL"),
            assistant.injury_context(game="Celtics")]


def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v)


def test_no_tool_output_or_query_touches_paper_or_challenger_outcomes(db, monkeypatch):
    statements = []
    real_connect = assistant._connect

    def traced():
        conn = real_connect()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(assistant, "_connect", traced)
    outputs = _all_tool_outputs(db["ids"])

    for out in outputs:
        bad = set(_keys(out)) & set(assistant.OUTCOME_COLUMNS)
        assert not bad, f"outcome keys in tool output: {bad}"
        dumped = json.dumps(out, default=str)
        assert str(SENTINEL) not in dumped and SENTINEL_TEXT not in dumped
    touching = [s for s in statements if re.search(r"paper_predictions|challenger_predictions|_pre_", s)]
    assert touching, "the tools should read the paper / challenger tables"
    for s in touching:
        assert not OUTCOME_SQL.search(s), f"outcome column in SQL: {s}"
        assert "*" not in s, f"SELECT * on a paper / challenger table: {s}"
    assert not any("_pre_" in s for s in statements)


def test_select_refuses_columns_outside_the_whitelist(db):
    conn = assistant._connect()
    try:
        for col in ("actual_value", "result", "resolved_at", "status"):
            with pytest.raises(ValueError):
                assistant._select(conn, "challenger_predictions", ("mu", col), "1", ())
            with pytest.raises(ValueError):
                assistant._select(conn, "paper_predictions", ("p_over", col), "1", ())
    finally:
        conn.close()


def test_track_record_matches_the_tooltip_endpoint(db):
    from fastapi.testclient import TestClient
    import main
    client = TestClient(main.app)
    for player in ("Wendell Carter Jr.", "Kim Lee"):
        for model in database.TRACK_RECORD_MODELS:
            tooltip = client.get(f"/players/{player}/track-record", params={"stat": "Assists", "model": model}).json()
            mine = assistant.track_record(player, "Assists", model=model)
            assert mine.pop("small_sample") == (not tooltip["enough"])
            assert mine.pop("record_text") == assistant.record_text(tooltip)
            assert mine == tooltip
    # default = History's default filter (Challenger); Carter: 3-1 of a 4-2 challenger baseline
    tr = assistant.track_record("Wendell Carter Jr.", "Assists")
    assert (tr["model"], tr["wins"], tr["losses"], tr["overall_n"], tr["small_sample"]) == ("challenger", 3, 1, 6, True)
    # list_pending's record is the tooltip's for the model that made the pick
    pick = next(p for p in assistant.list_pending()["picks"] if p["stat_type"] == "Assists")
    tooltip = client.get("/players/Wendell Carter Jr./track-record",
                         params={"stat": "Assists", "model": pick["model"]}).json()
    assert {k: v for k, v in pick["track_record"].items() if k not in ("small_sample", "record_text")} == tooltip


def _players(out):
    return [p["player_name"] for p in out["picks"]]


def test_list_pending_without_a_date_searches_every_pending_pick(db):
    out = assistant.list_pending()
    assert out["date"] == "all pending" and out["count"] == 6          # the retro pick is never listed
    assert "Ja Morant" in _players(out)                                # dated 2026-10-29, not "today"
    on_date = assistant.list_pending(date=DATE)
    assert on_date["count"] == 5 and "Ja Morant" not in _players(on_date)
    carter = next(p for p in on_date["picks"] if p["player_name"] == "Wendell Carter Jr.")
    assert carter["challenger"]["mu"] == 3.0 and carter["challenger"]["expected_minutes"] == 30.0
    assert carter["confidence"] == 0.63 and carter["predicted_outcome"] == "OVER"
    kim = next(p for p in on_date["picks"] if p["player_name"] == "Kim Lee")
    assert kim["challenger"] is None                                   # nothing logged at that line


@pytest.mark.parametrize("asked", ["ja morant", "JA MORANT", "Ja Morant", "ja morant jr"])
def test_player_lookup_ignores_case_and_suffixes(db, asked):
    out = assistant.list_pending(player=asked, stat="3PM")
    assert _players(out) == ["Ja Morant"] and out["picks"][0]["stat_line"] == 1.5
    assert out["picks"][0]["predicted_outcome"] == "UNDER" and out["picks"][0]["game_date"] == "2026-10-29"


@pytest.mark.parametrize("asked", ["Nikola Jokic", "nikola jokić", "NIKOLA JOKIC"])
def test_player_lookup_ignores_accents(db, asked):
    assert _players(assistant.list_pending(player=asked)) == ["Nikola Jokić"]
    assert assistant.track_record(asked, "reb")["player_name"] == "Nikola Jokić"


def test_player_lookup_never_guesses(db):
    out = assistant.list_pending(player="Morant")                      # partial name: no fuzzy match
    assert out["count"] == 0 and "no pending picks" in out["note"]
    assert "error" in assistant.track_record("Nobody Here", "Points")


@pytest.mark.parametrize("alias,stat", [
    ("3PM", "3PM"), ("3pm", "3PM"), ("threes", "3PM"), ("3-pointers", "3PM"), ("3 pointers", "3PM"),
    ("three pointers made", "3PM"), ("3s", "3PM"), ("3PT", "3PM"),
    ("points", "Points"), ("pts", "Points"), ("Points", "Points"),
    ("rebounds", "Rebounds"), ("reb", "Rebounds"), ("boards", "Rebounds"),
    ("assists", "Assists"), ("ast", "Assists"), ("dimes", "Assists"),
    ("PRA", "PRA"), ("pts+reb+ast", "PRA"), ("points rebounds assists", "PRA"),
    ("pts + reb", "PR"), ("points and assists", "PA"), ("reb+ast", "RA")])
def test_stat_aliases(alias, stat):
    assert assistant.canonical_stat(alias) == stat


def test_stat_aliases_in_tools_and_unknown_stats(db):
    assert _players(assistant.list_pending(stat="threes")) == _players(assistant.list_pending(stat="3PM"))
    assert set(_players(assistant.list_pending(stat="threes"))) == {"Kim Lee", "Ja Morant"}
    assert assistant.track_record("Wendell Carter Jr.", "ast")["n"] == 4
    with pytest.raises(ValueError):
        assistant.canonical_stat("steals")
    assert "error" in assistant.run_tool("list_pending", {"stat": "steals"})


def test_list_pending_ranks_by_confidence_and_flags_high_confidence_last(db):
    out = assistant.list_pending(date=DATE)
    # Steady Eddie's 18-2 record (n=20) does not lift a pick 2+ points less confident than the one above
    assert _players(out) == ["Wendell Carter Jr.", "Kim Lee", "Nikola Jokić", "Steady Eddie", "Zed Big"]
    assert [p["rank"] for p in out["picks"]] == [1, 2, 3, 4, 5]
    assert not any(p["tie_break_on_record"] for p in out["picks"])
    zed = out["picks"][-1]                       # 99%: the most confident, never the top pick
    assert zed["confidence"] == 0.99 and zed["high_confidence_flag"] == assistant.HIGH_CONFIDENCE_FLAG
    assert assistant.HIGH_CONFIDENCE_FLAG == "unusually high — check that the line was entered correctly"
    assert all(p["high_confidence_flag"] is None for p in out["picks"][:-1])
    assert out["high_confidence_flagged"] == 1
    assert assistant.explain(db["ids"]["Zed Big|Points"])["high_confidence_flag"] == assistant.HIGH_CONFIDENCE_FLAG
    assert assistant.explain(db["ids"]["Kim Lee|3PM"])["high_confidence_flag"] is None


def test_flagged_picks_are_returned_even_past_the_pick_limit(db, monkeypatch):
    monkeypatch.setattr(assistant, "MAX_PICKS", 2)
    out = assistant.list_pending(date=DATE)
    assert _players(out) == ["Wendell Carter Jr.", "Kim Lee", "Zed Big"]
    assert (out["count"], out["shown"], out["high_confidence_flagged"]) == (5, 3, 1)


def _p(name, conf, n=0, adj=None):
    return {"player_name": name, "confidence": conf, "high_confidence_flag": assistant.high_confidence_flag(conf),
            "track_record": {"n": n, "adjusted_rate": adj}}


def _order(*picks):
    return [p["player_name"] for p in assistant.rank_picks(list(picks))]


def test_record_breaks_ties_within_two_points_when_both_have_enough_picks():
    a, b = _p("a", 0.62, n=25, adj=0.55), _p("b", 0.60, n=30, adj=0.70)     # exactly 2 points apart
    assert _order(a, b) == ["b", "a"]
    ranked = assistant.rank_picks([_p("a", 0.62, n=25, adj=0.55), _p("b", 0.60, n=30, adj=0.70)])
    assert [p["tie_break_on_record"] for p in ranked] == [True, False]


def test_no_tie_break_beyond_two_points_or_with_a_small_sample():
    assert _order(_p("a", 0.625, n=25, adj=0.5), _p("b", 0.60, n=30, adj=0.9)) == ["a", "b"]     # 2.5 points
    assert _order(_p("a", 0.61, n=25, adj=0.5), _p("b", 0.60, n=19, adj=0.9)) == ["a", "b"]      # b: n < 20
    assert _order(_p("a", 0.61, n=19, adj=0.5), _p("b", 0.60, n=30, adj=0.9)) == ["a", "b"]      # a: n < 20
    assert _order(_p("a", 0.61, n=25, adj=0.6), _p("b", 0.60, n=30, adj=0.6)) == ["a", "b"]      # equal records


def test_tie_break_never_passes_a_pick_more_than_two_points_ahead():
    # c (0.58) beats b (0.595) on record, but not a (0.61): 3 points
    assert _order(_p("a", 0.61, n=20, adj=0.5), _p("b", 0.595, n=20, adj=0.5), _p("c", 0.58, n=20, adj=0.9)) \
        == ["a", "c", "b"]


def test_tie_break_threshold_is_one_constant(monkeypatch):
    a, b = _p("a", 0.61, n=20, adj=0.5), _p("b", 0.60, n=20, adj=0.9)
    assert _order(a, b) == ["b", "a"]
    monkeypatch.setattr(assistant, "RANK_MIN_N", 21)
    assert _order(_p("a", 0.61, n=20, adj=0.5), _p("b", 0.60, n=20, adj=0.9)) == ["a", "b"]


def test_high_confidence_picks_are_flagged_and_never_first():
    assert assistant.high_confidence_flag(0.97) == assistant.HIGH_CONFIDENCE_FLAG
    assert assistant.high_confidence_flag(0.9699) is None
    assert _order(_p("sure", 1.0), _p("high", 0.97, n=50, adj=0.9), _p("ok", 0.70), _p("low", 0.55)) \
        == ["ok", "low", "sure", "high"]


def test_record_text_format(db):
    carter = assistant.track_record("Wendell Carter Jr.", "Assists")
    assert carter["record_text"] == f"3–1 (4 picks), adjusted {round(carter['adjusted_rate'] * 100)}%, small sample"
    assert assistant.track_record("Steady Eddie", "Rebounds")["record_text"] == "18–2 (20 picks), adjusted 90%"
    tr = {"wins": 2, "losses": 0, "n": 2, "adjusted_rate": 0.6327, "enough": False}
    assert assistant.record_text(tr) == "2–0 (2 picks), adjusted 63%, small sample"
    assert assistant.record_text({**tr, "wins": 1, "n": 1}) == "1–0 (1 pick), adjusted 63%, small sample"
    assert assistant.record_text({"wins": 0, "losses": 0, "n": 0, "adjusted_rate": None, "enough": False}) \
        == "no resolved picks yet"
    for p in assistant.list_pending()["picks"]:
        assert not re.search(r"\d-\d", p["track_record"]["record_text"])       # en dash, never "2-0-2"


@pytest.mark.parametrize("key,stat,feats", [("Wendell Carter Jr.|Assists", "Assists", "ast_f"),
                                            ("Kim Lee|3PM", "3PM", "tpm_f")])
def test_explain_contributions_add_up_to_the_logged_p_over(db, key, stat, feats):
    out = assistant.explain(db["ids"][key])
    fm = out["frozen_model"]
    assert fm["available"] and fm["captured_at"].startswith("2026-11-09T22")    # the latest logged row
    margin = fm["base_log_odds"] + sum(c["log_odds"] for c in fm["contributions"])
    assert 1 / (1 + math.exp(-margin)) == pytest.approx(_p_over(stat, db[feats]), abs=1e-6)
    assert fm["p_over_recomputed"] == pytest.approx(fm["p_over_logged"], abs=1e-6)
    assert {c["feature"] for c in fm["contributions"]} == set(predictor.FEATURE_COLS)
    assert out["pick"]["explanation"] == "stored explanation"


def test_explain_challenger_decomposition(db):
    ch = assistant.explain(db["ids"]["Wendell Carter Jr.|Assists"])["challenger"]
    assert ch["available"] and ch["minutes_x_rate"] == pytest.approx(3.0)
    assert ch["variance"] == pytest.approx(3.0 + 0.1 * 9)
    from challenger import model as cm
    assert ch["p_over_recomputed"] == pytest.approx(float(cm.p_over(3.0, 0.1, 3.5)))
    assert ch["over_means"] == "Assists >= 4"
    assert assistant.explain(db["ids"]["Kim Lee|3PM"])["challenger"]["available"] is False
    assert "error" in assistant.explain(999999)


def test_injury_context_player_and_teammates(db):
    out = assistant.injury_context(player="Wendell Carter Jr.")
    (team,) = out["teams"]
    assert team["team"] == "Orlando Magic" and team["official_report_game_date"] == DATE
    by = {r["player"]: r for r in team["official_report"]}
    assert by["Wendell Carter Jr."]["injury_status"] == "available" and by["Wendell Carter Jr."]["is_asked_player"]
    assert by["Paolo Banchero"]["injury_status"] == "out" and not by["Paolo Banchero"]["is_asked_player"]
    assert team["espn"][0]["player"] == "Paolo Banchero" and team["espn"][0]["injury_status"] == "Out"
    game = assistant.injury_context(game="BOS@ORL")
    assert sorted(t["team"] for t in game["teams"]) == ["Boston Celtics", "Orlando Magic"]
    assert "error" in assistant.injury_context()


def test_tools_never_write(db):
    before = hashlib.sha256(open(db["path"], "rb").read()).hexdigest()
    _all_tool_outputs(db["ids"])
    assert hashlib.sha256(open(db["path"], "rb").read()).hexdigest() == before
    conn = assistant._connect()
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM predictions")
    finally:
        conn.close()


def test_run_tool_reports_bad_calls_to_the_model(db):
    assert "error" in assistant.run_tool("drop_table", {})
    assert "error" in assistant.run_tool("explain", {"nope": 1})
    assert "error" in assistant.run_tool("explain", "{not json")
    assert assistant.run_tool("track_record", '{"player": "Kim Lee", "stat": "Assists"}')["n"] == 2


def test_ask_runs_tools_and_returns_the_llm_answer(db, monkeypatch):
    seen = []
    replies = iter([
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "list_pending", "arguments": {"stat": "Assists"}}}]},
        {"role": "assistant", "content": "Wendell Carter Jr. Assists OVER 3.5: 3-1 (n=4, small sample)."}])

    def fake_chat(messages):
        seen.append([dict(m) for m in messages])
        return next(replies)

    monkeypatch.setattr(assistant, "_chat", fake_chat)
    from fastapi.testclient import TestClient
    import main
    r = TestClient(main.app).post("/assistant", json={"messages": [{"role": "user", "content": "best picks today?"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["answer"].startswith("Wendell Carter Jr.")
    assert body["tools_used"] == [{"name": "list_pending", "arguments": {"stat": "Assists"}}]
    assert seen[0][0]["role"] == "system" and seen[0][-1]["content"] == "best picks today?"
    tool_msg = seen[1][-1]
    assert tool_msg["role"] == "tool" and json.loads(tool_msg["content"])["count"] == 1


def test_explain_button_passes_the_exact_prediction_id(db, monkeypatch):
    seen = []

    def fake_chat(messages):
        seen.append([dict(m) for m in messages])
        return {"role": "assistant", "content": "Explained."}

    monkeypatch.setattr(assistant, "_chat", fake_chat)
    from fastapi.testclient import TestClient
    import main
    pid = db["ids"]["Ja Morant|3PM"]
    # the question text names nobody: the pick is chosen by id, not by matching a name
    r = TestClient(main.app).post("/assistant", json={"messages": [{"role": "user", "content": "Explain this pick"}],
                                                      "prediction_id": pid})
    assert r.status_code == 200 and r.json()["answer"] == "Explained."
    assert r.json()["tools_used"] == [{"name": "explain", "arguments": {"prediction_id": pid}}]
    (msgs,) = seen                                         # explain ran before the one LLM call
    call, result = msgs[-2], msgs[-1]
    assert call["tool_calls"][0]["function"] == {"name": "explain", "arguments": {"prediction_id": pid}}
    explained = json.loads(result["content"])
    assert result["role"] == "tool" and explained["prediction_id"] == pid
    assert explained["pick"]["player_name"] == "Ja Morant" and explained["pick"]["stat_type"] == "3PM"
    assert TestClient(main.app).post("/assistant", json={"messages": [{"role": "user", "content": "x"}],
                                                         "prediction_id": 0}).status_code == 422


def test_ask_stops_after_the_tool_round_limit(db, monkeypatch):
    call = {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "list_pending", "arguments": {}}}]}
    monkeypatch.setattr(assistant, "_chat", lambda messages: call)
    out = assistant.ask([{"role": "user", "content": "loop"}])
    assert len(out["tools_used"]) == assistant.MAX_TOOL_ROUNDS and "limit" in out["answer"]


def test_assistant_route_validation_and_unavailable_llm(db, monkeypatch):
    from fastapi.testclient import TestClient
    import main
    client = TestClient(main.app)
    assert client.post("/assistant", json={"messages": []}).status_code == 422
    assert client.post("/assistant", json={"messages": [{"role": "assistant", "content": "hi"}]}).status_code == 422
    assert client.post("/assistant", json={"messages": [{"role": "system", "content": "hi"}]}).status_code == 422
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)       # _chat refuses before any network call
    r = client.post("/assistant", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 503 and "OLLAMA_API_KEY" in r.json()["detail"]


@pytest.mark.real_auth
def test_assistant_requires_login():
    from fastapi.testclient import TestClient
    import main
    r = TestClient(main.app).post("/assistant", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code in (401, 503)       # 503: login not set up on this machine
