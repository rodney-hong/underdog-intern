"""
challenger_memory_measure.py
Peak memory of one backend process through the worst moments of a server day (migration plan
v2, step 0.9), with the challenger code before or after the memory fix:

  1. backend modules imported (startup)
  2. the 12 frozen model files + the frozen challenger loaded
  3. a pre-tip run: paper log_for_pull, then challenger log_for_pull, while another thread
     makes /predict challenger calls for short-history players (they run outside the pre-tip lock)
  4. a Kaggle ETL in the same process (as the sync runs it), loading the cached Kaggle files of
     the stored version, again with concurrent /predict calls

Run ONLY on a backup-API copy (it writes paper/challenger rows and reloads the ETL tables in it):
  python experiments/challenger_memory_measure.py --db <copy.db> --code new|old [--game ID]
Sockets are blocked; Kaggle's version check, the predictions CSV backup, the resolvers and
the cache pruning are stubbed (the real cache is never touched). Reads no outcome column.
Peak = peak working set on Windows, VmHWM on Linux; "now" = current RSS.
"""

import argparse
import glob
import json
import os
import socket
import sqlite3
import sys
import threading
import time

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _block_network():
    def blocked(self, address, *a, **k):
        raise OSError(f"network blocked: {address}")
    socket.socket.connect = blocked
    socket.getaddrinfo = lambda *a, **k: (_ for _ in ()).throw(OSError("dns blocked"))


def mem_mb() -> tuple[float, float]:
    """(peak, current) resident memory of this process, MB."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        k32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
        k32.GetCurrentProcess.restype = wintypes.HANDLE          # 64-bit pseudo-handle, not a C int
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
        c = PMC()
        c.cb = ctypes.sizeof(PMC)
        if not psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
            raise OSError("GetProcessMemoryInfo failed")
        return c.PeakWorkingSetSize / 2**20, c.WorkingSetSize / 2**20
    vals = {}
    with open("/proc/self/status") as f:
        for line in f:
            k, _, v = line.partition(":")
            if k in ("VmHWM", "VmRSS"):
                vals[k] = int(v.split()[0]) / 1024
    return vals["VmHWM"], vals["VmRSS"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--code", choices=("new", "old"), required=True)
    ap.add_argument("--game", help="OddsAPI game id with a pretip pull (default: most short-history rows)")
    ap.add_argument("--predicts", type=int, default=6, help="concurrent short-history /predict calls per phase")
    a = ap.parse_args()
    _block_network()
    steps = {"baseline": mem_mb()}

    import database
    database.DB_PATH = os.path.abspath(a.db)
    import line_logger
    line_logger.DB_PATH = database.DB_PATH
    import challenger_log
    import paper_predictions
    challenger_log.DB_PATH = paper_predictions.DB_PATH = database.DB_PATH
    import main  # noqa: F401  (every backend module, as at startup)
    from challenger import inference, live
    if a.code == "old":
        from challenger_memory_parity import features_for_context_before_fix
        inference.features_for_context = features_for_context_before_fix
        live.features_for_context = features_for_context_before_fix
    steps["1 modules imported"] = mem_mb()

    import joblib
    models = [joblib.load(p) for p in sorted(glob.glob(os.path.join(BACKEND, "model_*.pkl")))]
    m = live.load_frozen()
    steps["2 12 models + challenger loaded"] = mem_mb()

    conn = sqlite3.connect(f"file:{database.DB_PATH}?mode=ro", uri=True)
    game = a.game or conn.execute(
        "SELECT game_id FROM challenger_predictions WHERE n_prior < 11 GROUP BY game_id "
        "ORDER BY COUNT(DISTINCT person_id) DESC LIMIT 1").fetchone()[0]
    from challenger_memory_parity import short_history_sample
    tip = "2026-10-27 19:30:00"
    shorts = short_history_sample(conn, a.predicts, tip, nba_opponents=True)   # 1-10 earlier games
    n_short = conn.execute("SELECT COUNT(DISTINCT person_id) FROM challenger_predictions WHERE game_id = ? "
                           "AND n_prior < 11", (game,)).fetchone()[0]
    conn.close()

    def predicts():
        """/predict's challenger path for each short-history player (team ids given directly)."""
        c = database.get_connection()
        try:
            for pid, team, opp in shorts:
                F, _ = live.features_for_context(c, pid, {"tip": tip, "home": 1, "team": team, "opp": opp,
                                                          "game_id": 0})
                live.predict_from_features(F, "Points", [10.5], m)
        finally:
            c.close()

    t0 = time.perf_counter()
    paper = paper_predictions.log_for_pull(game)
    steps["3a paper log_for_pull"] = mem_mb()
    th = threading.Thread(target=predicts)
    th.start()
    ch = challenger_log.log_for_pull(game)
    th.join()
    steps["3b challenger log_for_pull + concurrent /predict"] = mem_mb()
    pretip_s = time.perf_counter() - t0

    stored = line_logger._stored_kaggle_version()
    cached = os.path.join(database.kaggle_dataset_cache_dir(), "versions", str(stored))
    files = {"stats": os.path.join(cached, "PlayerStatisticsExtended.csv"), "players": os.path.join(cached, "Players.csv"),
             "schedule": glob.glob(os.path.join(cached, "LeagueSchedule*.csv"))[-1]}
    database.get_latest_kaggle_version = lambda: stored + 1
    database.download_kaggle_files = lambda v: files
    database.prune_kaggle_cache = lambda v: {"removed": []}            # never touch the real cache
    database.backup_predictions_to_csv = lambda: None                  # would write backend/predictions_backup.csv
    database.resolve_pending_predictions = lambda: 0
    t1 = time.perf_counter()
    th = threading.Thread(target=predicts)
    th.start()
    database.run_etl()
    th.join()
    steps["4 ETL + concurrent /predict"] = mem_mb()
    etl_s = time.perf_counter() - t1

    out = {"code": a.code, "platform": sys.platform, "game": game, "short_history_players_in_game": n_short,
           "concurrent_predicts": len(shorts), "paper": paper, "challenger": {k: v for k, v in ch.items() if k != "rows"},
           "pretip_seconds": round(pretip_s, 1), "etl_seconds": round(etl_s, 1), "models_loaded": len(models),
           "frozen_ok": m is not None,
           "mb": {k: {"peak": round(p), "now": round(n)} for k, (p, n) in steps.items()}}
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
