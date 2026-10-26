"""
Challenger memory fix (migration plan v2, step 0.9): features_for_context now loads team rows
for the player's own teams only, instead of every team's (every row since the start of the
data for a player with fewer than 11 earlier games). On the synthetic two-season DB (NULL
team ids for part of 2023-24, a mid-season trade, long absences) the feature frame is bit
for bit the old one for every player at tips across both seasons, from his first game on,
and the short-history load is a fraction of the old one. The same check on the real data:
experiments/challenger_memory_parity.py.
"""
import os
import sqlite3
import sys

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BACKEND, "experiments"))

from challenger import features as cf                 # noqa: E402
from challenger import inference                      # noqa: E402
from challenger_memory_parity import features_for_context_before_fix, frames_identical   # noqa: E402
from test_challenger import TEAMS, _db                # noqa: E402


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("mem") / "syn.db")
    _db(path)
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    cf._team_maps.clear()
    yield c
    c.close()


def _contexts(conn):
    """(person, ctx) at the tip of games across both seasons: his 1st-15th games (short
    history, inside the NULL-team-id stretch), mid-season around the trade, and late 2024-25."""
    rows = conn.execute("SELECT personId, gameId, gameDateTimeEst, home, playerteamId, opponentteamId, "
                        "playerteamCity, playerteamName, opponentteamCity, opponentteamName FROM player_stats "
                        "WHERE gameType = 'Regular Season' AND gameDateTimeEst < '2026-07-01' "
                        "ORDER BY personId, gameDateTimeEst").fetchall()
    tm = cf.load_team_map(conn)
    by_player = {}
    for r in rows:
        by_player.setdefault(r[0], []).append(r)
    out = []
    for pid, games in by_player.items():
        for k in sorted({0, 1, 3, 5, 8, 10, 11, 12, 15, len(games) // 2, len(games) - 1}):
            if k >= len(games):
                continue
            _, gid, tip, home, team, opp, pc, pn, oc, on = games[k]
            team = team if team is not None else tm.get((pc, pn))
            opp = opp if opp is not None else tm.get((oc, on))
            out.append((pid, {"tip": tip, "home": int(home), "team": int(team), "opp": int(opp), "game_id": int(gid)}))
    return out


def test_features_are_identical_to_before_the_fix(conn):
    ctxs = _contexts(conn)
    assert len(ctxs) > 300
    short = 0
    for pid, ctx in ctxs:
        old, c_old = features_for_context_before_fix(conn, pid, ctx)
        new, c_new = inference.features_for_context(conn, pid, ctx)
        assert frames_identical(old, new), (pid, ctx)
        assert c_old["data_as_of"] == c_new["data_as_of"]
        short += int(old.iloc[0]["n_prior"]) < 11
    assert short > 100                                   # the short-history path really ran


def test_an_upcoming_game_after_a_trade_matches_too(conn):
    """Live use: game_id 0, a tip after the last game, the player's new team."""
    tm = cf.load_team_map(conn)
    for pid, team in conn.execute("SELECT personId, playerteamId FROM player_stats WHERE gameDateTimeEst < '2026-07-01' "
                                  "GROUP BY personId HAVING gameDateTimeEst = MAX(gameDateTimeEst)").fetchall():
        opp = next(t for t in TEAMS if t != team)
        ctx = {"tip": "2026-10-27 19:30:00", "home": 1, "team": int(team), "opp": opp, "game_id": 0}
        assert frames_identical(features_for_context_before_fix(conn, pid, ctx)[0],
                                inference.features_for_context(conn, pid, ctx)[0]), pid
    assert tm


def test_short_history_loads_a_fraction_of_the_rows(conn, monkeypatch):
    """A player with fewer than 11 earlier games (team_from was "0000"): the old code loaded every
    played row before tip, the new one only his team's."""
    loaded = []
    real = inference._rows

    def counting(c, where, params):
        df = real(c, where, params)
        loaded.append((where, len(df)))
        return df

    monkeypatch.setattr(inference, "_rows", counting)
    pid = conn.execute("SELECT MIN(personId) FROM player_stats").fetchone()[0]
    games = conn.execute("SELECT gameDateTimeEst, playerteamId, opponentteamId, playerteamCity, playerteamName, "
                         "opponentteamCity, opponentteamName FROM player_stats WHERE personId = ? "
                         "AND gameType = 'Regular Season' AND numMinutes > 0 ORDER BY gameDateTimeEst", (pid,)).fetchall()
    tip, team, opp, pc, pn, oc, on = games[3]
    tm = cf.load_team_map(conn)                          # inside the NULL-id stretch: ids from names
    ctx = {"tip": tip, "home": 1, "team": int(team if team is not None else tm[(pc, pn)]),
           "opp": int(opp if opp is not None else tm[(oc, on)]), "game_id": 0}
    every = conn.execute(f"SELECT COUNT(*) FROM player_stats WHERE {cf.REGULAR_SQL} AND gameDateTimeEst < ?",
                         (tip,)).fetchone()[0]
    features_for_context_before_fix(conn, pid, ctx)
    old_team = loaded[1][1]                              # hist, team rows, league
    loaded.clear()
    F, _ = inference.features_for_context(conn, pid, ctx)
    new_team = next(n for w, n in loaded if "playerteamId IN" in w)
    assert 0 < int(F.iloc[0]["n_prior"]) < 11
    assert old_team == every and 0 < new_team < old_team / 2
