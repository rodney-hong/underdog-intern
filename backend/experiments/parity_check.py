"""
parity_check.py
Phase 3 parity checks for the cloud migration ("SportsBettr Cloud Migration Plan v2", Phase 3;
migration_readiness_2026-10-25.md, Part 5). Run it at the same commit on the PC and on the
server against the same snapshot, then compare the two JSON files with parity_compare.py.

  python experiments/parity_check.py --db <backup-API copy> [--mode integrity,models,replay-paper,replay-challenger]
  python experiments/parity_check.py --db <backup-API copy> --mode end-to-end --date YYYY-MM-DD [--pulls ID,...]
      [--out FILE.json]   default: experiments/results/parity_<hostname>_<UTC timestamp>.json

Modes (default: the four read-only ones; end-to-end runs on its own):
  integrity          PRAGMA integrity_check, journal_mode, encoding, schema hash; per table the row
                     count and a SHA-256 over every non-outcome column ORDER BY rowid (encoding below).
  models             SHA-256 of backend/model_*.pkl and every file in challenger/models/, against
                     preregistration.md: OK / NOT LISTED / MISMATCH; content fingerprints of the files
                     it lists one for; the hashes the code pins (paper_predictions.STATS, live.FROZEN).
  replay-paper       every 'ok' paper_predictions row re-scored with its stat's frozen model
                     (paper_predictions._load_model) from its stored features, NaN -> 0 exactly as
                     paper_predictions._predict_one's score(): p_over from features_json and, where
                     present, p_over_v1 from features_v1_json.
  replay-challenger  every 'ok' challenger_predictions row re-scored with the frozen challenger
                     (live.load_frozen, live.predict_from_features) from features_json + n_prior:
                     mu, alpha, p_over at the row's line, expected_minutes, rate_per_minute.
                     insufficient_history rows are skipped and counted.
  end-to-end         on a backup-API temp copy: delete the paper and challenger rows of the given
                     pre-tip pulls (odds_pull_log ids; default: every successful pre-tip pull on
                     --date), run paper_predictions.log_for_pull(game_id) then
                     challenger_log.log_for_pull(game_id) as line_logger._run_pretip_pull does, and
                     export the new rows without id and created_at.

Replay results are exact matches (bitwise equal), last-digit differences (unequal, within
REL_TOL relative; max difference reported) or mismatches (row ids listed). Nothing is rounded.

Rules the script enforces on itself:
  - Outcome columns are never selected, including inside hashes. They are found by name from
    the schema (outcome_columns) and printed. A SQLite authorizer on every connection denies
    reading them, so a query that names one fails instead of reading it, on the script's own
    connections and on those the code under test opens. A column whose role is unclear is
    excluded and listed for review; ALLOWED_INPUTS lists the reviewed inputs a name rule would
    otherwise catch, which are hashed and exported like any other column.
  - Installed packages are checked against backend/requirements-lock.txt (packages_vs_lock).
  - --db is opened read-only (file:...?mode=ro). Anything that writes uses a backup-API copy in
    a temp directory, deleted afterwards. While the code under test runs, a connection to the
    live predictions.db / historical.db or to --db that is not read-only raises.
  - No network: socket connect / getaddrinfo / create_connection raise NetworkBlocked (loopback
    included, so local Ollama too), and every attempt is reported.
  - Model files are only read and hashed, never written.
"""

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import math
import os
import pathlib
import platform
import re
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import types

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, BACKEND)

RESULTS_DIR = os.path.join(HERE, "results")
PREREG = os.path.join(HERE, "preregistration.md")
LIVE_DBS = tuple(os.path.join(BACKEND, n) for n in ("predictions.db", "historical.db"))
TOOL_VERSION = 2          # 2: ALLOWED_INPUTS hashed; packages checked against the lock file
LOCK_FILE = os.path.join(BACKEND, "requirements-lock.txt")
# Installed only on Windows (pulled in by other packages' win32 markers): reported, never required.
WINDOWS_ONLY = {"colorama": "Windows-only: click, tqdm and pytest require it on win32 (removed from the lock)",
                "pywin32": "Windows-only", "pywin32-ctypes": "Windows-only", "pywinpty": "Windows-only",
                "windows-curses": "Windows-only", "comtypes": "Windows-only", "wmi": "Windows-only"}
ENCODING_VERSION = b"sportsbettr-parity-v1"
REL_TOL = 1e-12
READ_ONLY_MODES = ("integrity", "models", "replay-paper", "replay-challenger")
MODES = READ_ONLY_MODES + ("end-to-end",)

# --- outcome columns ---------------------------------------------------------------------

EXPLICIT_OUTCOMES = ("actual_result", "actual_value", "actual_points", "result", "resolved_at")
OUTCOME_PATTERNS = ("actual_*", "result*", "resolved*", "hit*", "won", "correct", "outcome*", "grade*")
# A name containing one of these words anywhere has an unclear role: excluded, listed for review.
REVIEW_WORDS = {"actual", "result", "results", "resolve", "resolved", "outcome", "outcomes", "grade", "graded",
                "grading", "correct", "won", "win", "wins", "hit", "hits", "dnp", "settled", "payout", "profit", "pnl"}
# Columns a name rule or word catches that are inputs, not outcomes: hashed and exported like
# any other column (reviewed 2026-10-26). Anything not listed here stays excluded.
ALLOWED_INPUTS = {
    # OddsAPI's position of the outcome within its market's list, written when the lines are
    # pulled (before the game) and part of the snapshot's UNIQUE key; paper_predictions.log_for_pull
    # orders by it. "Outcome" is OddsAPI's word for a priced side, not a game result.
    ("line_snapshots", "outcome_index"): "OddsAPI's position of a priced side in its market; written at pull time",
    # Kaggle box score of a past game: whether the player's team won it. A model input
    # (data_fetcher.get_game_logs reads it), not the outcome of any logged prediction.
    ("player_stats", "win"): "box-score field of a past game (Kaggle); a feature-pipeline input",
    # The app's own pick (OVER / UNDER), written with the prediction before the game. Its
    # outcome is actual_result, which stays excluded.
    ("predictions", "predicted_outcome"): "the app's pick, written before the game; the outcome is actual_result",
    # Status of a news-triggered OddsAPI pull (news_events._live_trigger / _set_result):
    # 'pulled: N rows', 'skipped: <reason>', 'not sent: EVENT_TRIGGERS_ENABLED is off' or
    # 'no trigger: <reason>'. A fetch/trigger status, not a game or prediction outcome.
    ("news_events", "trigger_result"): "status of a news-triggered pull (pulled / skipped / not sent / no trigger)",
}


def _words(name: str) -> list[str]:
    out = []
    for part in name.split("_"):
        out += [w.lower() for w in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", part)]
    return out


def classify_column(table: str, col: str) -> tuple[str, str] | None:
    """(kind, reason) if col is excluded, else None. kind: 'outcome' (explicit name or name
    rule) or 'review' (unclear role, excluded until confirmed). ALLOWED_INPUTS -> None."""
    low = col.lower()
    if low in EXPLICIT_OUTCOMES:
        return "outcome", "explicit outcome name"
    if (table.lower(), low) in ALLOWED_INPUTS:
        return None
    for pat in OUTCOME_PATTERNS:
        if fnmatch.fnmatchcase(low, pat):
            return "outcome", f"name rule {pat}"
    hits = sorted(set(_words(col)) & REVIEW_WORDS)
    if hits:
        return "review", f"name contains {', '.join(repr(h) for h in hits)}"
    return None


def outcome_columns(conn) -> dict:
    """Per table: its columns, the excluded ones with reasons, and the 'review' subset."""
    out = {}
    for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({_qi(t)})")]
        excluded, review = {}, {}
        for c in cols:
            k = classify_column(t, c)
            if k:
                excluded[c] = k[1]
                if k[0] == "review":
                    review[c] = k[1]
        out[t] = {"columns": cols, "excluded": excluded, "review": review}
    return out


def blocked_reads(classes: dict) -> set:
    """(table, column) pairs the authorizer denies, on the script's own connections and on
    those the code under test opens: every excluded column."""
    return {(t.lower(), c.lower()) for t, d in classes.items() for c in d["excluded"]}


DENIED: list = []          # reads the authorizer refused (reported; any one fails the run)


def authorizer(blocked: set):
    def auth(action, arg1, arg2, dbname, source):
        if action == sqlite3.SQLITE_READ and arg1 and arg2 and (arg1.lower(), arg2.lower()) in blocked:
            DENIED.append(f"{arg1}.{arg2}")
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    return auth


def print_exclusions(classes: dict) -> None:
    print("Excluded columns (never selected, never hashed, never exported):")
    for t, d in classes.items():
        if d["excluded"]:
            print(f"  {t}: " + ", ".join(f"{c} ({r})" for c, r in d["excluded"].items()))
    review = [(t, c, r) for t, d in classes.items() for c, r in d["review"].items()]
    print("For your review (excluded because the role is unclear):")
    for t, c, r in review or [("-", "-", "none")]:
        print(f"  {t}.{c}: {r}")
    present = {(t.lower(), c.lower()) for t, d in classes.items() for c in d["columns"]}
    print("Included although a name rule matches (ALLOWED_INPUTS: inputs, not outcomes):")
    for (t, c), r in ALLOWED_INPUTS.items():
        print(f"  {t}.{c}: {r}{'' if (t, c) in present else ' (not in this database)'}")


# --- canonical encoding --------------------------------------------------------------------

class _Text(bytes):
    """A TEXT value as its UTF-8 bytes (sqlite3 text_factory), told apart from a BLOB."""


def encode_value(v) -> bytes:
    """Type-tagged, length-prefixed, identical on every platform: NULL, INTEGER (decimal),
    REAL (float.hex, so every bit counts and -0.0 != 0.0), TEXT (UTF-8), BLOB (raw)."""
    if v is None:
        return b"N"
    if isinstance(v, _Text):
        return b"T%d:" % len(v) + v
    if isinstance(v, str):
        b = v.encode("utf-8")
        return b"T%d:" % len(b) + b
    if isinstance(v, (bytes, bytearray, memoryview)):
        b = bytes(v)
        return b"B%d:" % len(b) + b
    if isinstance(v, bool):
        raise TypeError("bool is not a SQLite value")
    if isinstance(v, int):
        return b"I%d;" % v
    if isinstance(v, float):
        return b"F" + v.hex().encode("ascii") + b";"
    raise TypeError(f"no canonical encoding for {type(v).__name__}")


def encode_row(row) -> bytes:
    return b"".join(encode_value(v) for v in row) + b"\n"


def _header(table: str, cols: list[str]) -> bytes:
    return b"\x00".join([ENCODING_VERSION, table.encode("utf-8"), "\x1f".join(cols).encode("utf-8")]) + b"\x00"


def _qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def table_digest(conn, table: str, cols: list[str], order_by: str = "rowid") -> tuple[int, str]:
    """(rows, SHA-256) over cols of every row, ORDER BY order_by. With no column, rows still count."""
    h = hashlib.sha256(_header(table, cols))
    sel = ", ".join(_qi(c) for c in cols) if cols else "1"
    n = 0
    cur = conn.execute(f"SELECT {sel} FROM {_qi(table)} ORDER BY {order_by}")
    while True:
        chunk = cur.fetchmany(5000)
        if not chunk:
            break
        for row in chunk:
            h.update(encode_row(row if cols else ()))
        n += len(chunk)
    return n, h.hexdigest()


def jsonable(v):
    """A value for the JSON exports: floats as {"float": float.hex} so nothing is rounded."""
    if isinstance(v, float):
        return {"float": v.hex()}
    if isinstance(v, (bytes, bytearray)):
        return {"blob": bytes(v).hex()}
    return v


def fhex(x):
    return None if x is None else float(x).hex()


# --- guards ----------------------------------------------------------------------------------

class NetworkBlocked(OSError):
    pass


class NetworkGuard:
    """Every outbound connection or name lookup raises NetworkBlocked and is recorded."""

    def __init__(self):
        self.attempts = []
        self._saved = None

    def _block(self, call, target):
        frames = [f"{os.path.relpath(f.filename, BACKEND)}:{f.lineno} {f.name}"
                  for f in traceback.extract_stack()[:-2]
                  if f.filename.startswith(BACKEND) and not f.filename.endswith("parity_check.py")]
        self.attempts.append({"call": call, "target": repr(target)[:200], "from": frames[-4:]})
        raise NetworkBlocked(f"network blocked by parity_check: {call} {target!r}")

    def __enter__(self):
        s = socket
        self._saved = (s.socket.connect, s.socket.connect_ex, s.socket.sendto, s.getaddrinfo,
                       s.create_connection, s.gethostbyname, s.gethostbyname_ex)
        guard = self
        s.socket.connect = lambda sock, address, *a, **k: guard._block("connect", address)
        s.socket.connect_ex = lambda sock, address, *a, **k: guard._block("connect_ex", address)
        s.socket.sendto = lambda sock, data, *a, **k: guard._block("sendto", a[-1] if a else None)
        s.getaddrinfo = lambda host, *a, **k: guard._block("getaddrinfo", host)
        s.create_connection = lambda address, *a, **k: guard._block("create_connection", address)
        s.gethostbyname = lambda host: guard._block("gethostbyname", host)
        s.gethostbyname_ex = lambda host: guard._block("gethostbyname_ex", host)
        return self

    def __exit__(self, *exc):
        s = socket
        (s.socket.connect, s.socket.connect_ex, s.socket.sendto, s.getaddrinfo,
         s.create_connection, s.gethostbyname, s.gethostbyname_ex) = self._saved
        return False


_REAL_CONNECT = sqlite3.connect


def _target_file(database) -> str:
    """The file a sqlite3.connect target names: a path, 'file:PATH?...' or 'file:///...'."""
    target = os.fspath(database)
    if target.startswith("file:"):
        rest = target[5:].split("?", 1)[0]
        if rest.startswith("//"):                       # pathlib.Path.as_uri()
            from urllib.parse import urlparse
            from urllib.request import url2pathname
            rest = url2pathname(urlparse("file:" + rest).path)
        target = rest
    return os.path.normcase(os.path.realpath(target))


class DbGuard:
    """While active, sqlite3.connect refuses the protected files unless read-only (and
    refuses them entirely with ro_ok=False), and gives every connection the authorizer."""

    def __init__(self, protected, blocked: set, ro_ok: bool):
        self.protected = {_target_file(p) for p in protected}
        self.blocked, self.ro_ok = blocked, ro_ok
        self.refused = []

    def __enter__(self):
        guard = self
        prev = self._prev = sqlite3.connect

        def connect(database, *a, **k):
            target = os.fspath(database)
            if _target_file(target) in guard.protected:
                ro = k.get("uri") and target.startswith("file:") and "mode=ro" in target.split("?", 1)[-1]
                if not (guard.ro_ok and ro):
                    guard.refused.append(target)
                    raise PermissionError(f"parity_check: refused to open {target} "
                                          f"({'not read-only' if guard.ro_ok else 'protected during end-to-end'})")
            conn = prev(database, *a, **k)
            conn.set_authorizer(authorizer(guard.blocked))
            return conn

        sqlite3.connect = connect
        return self

    def __exit__(self, *exc):
        sqlite3.connect = self._prev
        return False


def ro_uri(path: str) -> str:
    return pathlib.Path(os.path.abspath(path)).as_uri() + "?mode=ro"


def connect_ro(path: str, blocked: set, text_bytes: bool = False) -> sqlite3.Connection:
    conn = _REAL_CONNECT(ro_uri(path), uri=True, timeout=30)
    conn.set_authorizer(authorizer(blocked))
    if text_bytes:
        conn.text_factory = _Text
    return conn


def backup_copy(src_path: str, dst_path: str) -> None:
    """SQLite backup API from a read-only source connection."""
    src = _REAL_CONNECT(ro_uri(src_path), uri=True, timeout=30)
    dst = _REAL_CONNECT(dst_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


# --- float comparison ------------------------------------------------------------------------

def _bits(x: float) -> int:
    return struct.unpack("<q", struct.pack("<d", x))[0]


def ulps(a: float, b: float) -> int:
    def key(x):
        i = _bits(x)
        return i if i >= 0 else -(i & 0x7FFFFFFFFFFFFFFF)
    return abs(key(a) - key(b))


def classify(stored, new) -> str:
    """'exact' (bitwise equal), 'last_digit' (unequal, within REL_TOL relative) or 'mismatch'."""
    if stored is None or new is None:
        return "exact" if stored is None and new is None else "mismatch"
    a, b = float(stored), float(new)
    if _bits(a) == _bits(b):
        return "exact"
    if math.isfinite(a) and math.isfinite(b) and abs(a - b) <= REL_TOL * max(abs(a), abs(b)):
        return "last_digit"
    return "mismatch"


class FieldTally:
    def __init__(self):
        self.checked = self.exact = 0
        self.last_digit, self.mismatches = [], []
        self.max_abs = self.max_rel = 0.0
        self.max_ulps = 0

    def add(self, row_id, stored, new, ident: dict, note: str | None = None):
        self.checked += 1
        k = "mismatch" if note else classify(stored, new)
        if k == "exact":
            self.exact += 1
            return
        rec = {"id": row_id, **ident, "stored": fhex(stored), "recomputed": fhex(new)}
        if note:
            rec["note"] = note
        if k == "last_digit":
            a, b = float(stored), float(new)
            self.max_abs = max(self.max_abs, abs(a - b))
            self.max_rel = max(self.max_rel, abs(a - b) / max(abs(a), abs(b)))
            self.max_ulps = max(self.max_ulps, ulps(a, b))
            self.last_digit.append(rec)
        else:
            if stored is not None and new is not None:
                rec["abs_diff"] = abs(float(stored) - float(new))
            self.mismatches.append(rec)

    def out(self) -> dict:
        return {"checked": self.checked, "exact": self.exact,
                "last_digit": len(self.last_digit), "last_digit_max_abs": self.max_abs,
                "last_digit_max_rel": self.max_rel, "last_digit_max_ulps": self.max_ulps,
                "last_digit_rows": self.last_digit,
                "mismatch": len(self.mismatches), "mismatch_rows": self.mismatches}


# --- sections --------------------------------------------------------------------------------

def run_integrity(db: str, classes: dict, blocked: set) -> dict:
    conn = connect_ro(db, blocked, text_bytes=True)
    try:
        out = {"integrity_check": [bytes(r[0]).decode() if isinstance(r[0], bytes) else r[0]
                                   for r in conn.execute("PRAGMA integrity_check")],
               "journal_mode": _pragma(conn, "journal_mode"), "encoding": _pragma(conn, "encoding"),
               "page_size": _pragma(conn, "page_size"), "user_version": _pragma(conn, "user_version")}
        h = hashlib.sha256(_header("sqlite_master", ["type", "name", "tbl_name", "sql"]))
        for row in conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"):
            h.update(encode_row(row))
        out["schema_sha256"] = h.hexdigest()
        tables = {}
        for t, d in classes.items():
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (t,)).fetchone()[0]
            sql = bytes(sql).decode() if isinstance(sql, bytes) else (sql or "")
            order = "rowid"
            if "WITHOUT ROWID" in sql.upper():
                pk = [r[1] for r in sorted(conn.execute(f"PRAGMA table_info({_qi(t)})"), key=lambda r: r[5]) if r[5]]
                order = ", ".join(_qi(c) for c in pk)
            cols = [c for c in d["columns"] if c not in d["excluded"]]
            t0 = time.perf_counter()
            n, digest = table_digest(conn, t, cols, order)
            tables[t] = {"rows": n, "sha256": digest, "hashed_columns": cols,
                         "excluded_columns": sorted(d["excluded"]), "order_by": order,
                         "seconds": round(time.perf_counter() - t0, 2)}
            print(f"  {t}: {n:,} rows, {len(cols)} columns hashed, {len(d['excluded'])} excluded")
        out["tables"] = tables
        out["ok"] = out["integrity_check"] == ["ok"]
        return out
    finally:
        conn.close()


def _pragma(conn, name):
    v = conn.execute(f"PRAGMA {name}").fetchone()[0]
    return bytes(v).decode() if isinstance(v, bytes) else v


_HEX64 = re.compile(r"\b[0-9a-f]{64}\b")
_PKL = re.compile(r"[\w.-]+\.pkl\b")


def prereg_hashes(path: str = PREREG) -> dict:
    """{"files": {basename: [{"sha256", "line"}]}, "fingerprints": {basename: [...]}} from the
    pre-registration: each 64-hex value belongs to the .pkl named on its line, else to the last
    .pkl named above it (reset at each heading); lines saying 'fingerprint' are fingerprints."""
    out = {"files": {}, "fingerprints": {}}
    last = None
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            if line.startswith("#"):
                last = None
            pkls = [os.path.basename(p) for p in _PKL.findall(line)]
            if pkls:
                last = pkls[-1]
            for h in _HEX64.findall(line):
                if last is None:
                    continue
                kind = "fingerprints" if "fingerprint" in line.lower() else "files"
                out[kind].setdefault(last, []).append({"sha256": h, "line": i})
    return out


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run_models() -> dict:
    listed = prereg_hashes()
    files = sorted(os.path.join(BACKEND, n) for n in os.listdir(BACKEND)
                   if n.startswith("model_") and n.endswith(".pkl"))
    cdir = os.path.join(BACKEND, "challenger", "models")
    for root, _, names in os.walk(cdir):
        files += sorted(os.path.join(root, n) for n in names)
    out = {"files": {}, "prereg_listed": listed}
    for p in files:
        rel = os.path.relpath(p, BACKEND).replace(os.sep, "/")
        sha = _sha256_file(p)
        want = {e["sha256"] for e in listed["files"].get(os.path.basename(p), [])}
        status = "NOT LISTED" if not want else ("OK" if sha in want else "MISMATCH")
        rec = {"sha256": sha, "bytes": os.path.getsize(p), "status": status}
        fps = {e["sha256"] for e in listed["fingerprints"].get(os.path.basename(p), [])}
        if fps:
            from challenger import model as cm
            fp = cm.fingerprint(cm.load(p))                 # unpickle and hash content; read only
            rec["fingerprint"] = fp
            rec["fingerprint_status"] = "OK" if fp in fps else "MISMATCH"
        out["files"][rel] = rec
        print(f"  {rel}: {status}{', fingerprint ' + rec['fingerprint_status'] if fps else ''}")
    import paper_predictions
    from challenger import live
    pins = {s["model_file"]: s["sha256"] for s in paper_predictions.STATS.values()}
    pins[os.path.basename(live.FROZEN["path"])] = live.FROZEN["sha256"]
    out["code_pins"] = {f: {"sha256": h, "matches_prereg": h in {e["sha256"] for e in listed["files"].get(f, [])}}
                        for f, h in pins.items()}
    out["code_fingerprint_pin_matches_prereg"] = live.FROZEN["fingerprint"] in {
        e["sha256"] for e in listed["fingerprints"].get(os.path.basename(live.FROZEN["path"]), [])}
    out["ok"] = (all(r["status"] != "MISMATCH" and r.get("fingerprint_status") != "MISMATCH"
                     for r in out["files"].values())
                 and all(v["matches_prereg"] for v in out["code_pins"].values())
                 and out["code_fingerprint_pin_matches_prereg"])
    return out


PAPER_COLS = ("id", "game_date", "player_name", "stat_type", "ud_line", "status", "model_file", "model_sha256",
              "feature_def", "p_over", "nan_features", "features_json", "p_over_v1", "features_v1_json")


def run_replay_paper(db: str, blocked: set) -> dict:
    import numpy as np
    import pandas as pd
    import paper_predictions
    import predictor
    from ml_trainer import FEATURE_COLS

    conn = connect_ro(db, blocked)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"SELECT {', '.join(PAPER_COLS)} FROM paper_predictions ORDER BY id").fetchall()
    conn.close()

    def score(model, features_json):
        f = json.loads(features_json)
        missing = [c for c in FEATURE_COLS if c not in f]
        if missing:
            raise KeyError(f"features missing: {missing}")
        # json null is the NaN paper_predictions.as_json wrote; back to NaN, then score() as live
        X = pd.DataFrame([{k: (np.nan if v is None else v) for k, v in f.items()}])[FEATURE_COLS]
        nan_cols = [c for c in FEATURE_COLS if pd.isna(X.at[0, c])]
        X = X.fillna(0).astype(float)
        return float(model.predict_proba(X)[0][1]), nan_cols

    tallies = {"p_over": FieldTally(), "p_over_v1": FieldTally()}
    recomputed, skipped, combos, other_sha = {}, {}, {}, []
    nan_agree = nan_disagree = 0
    models = {}
    for r in rows:
        if r["status"] != "ok" or r["p_over"] is None:
            k = "ok without p_over" if r["status"] == "ok" else r["status"].split(":")[0]
            skipped[k] = skipped.get(k, 0) + 1
            continue
        stat = r["stat_type"]
        if stat not in models:
            models[stat] = paper_predictions._load_model(stat)
        model, sha = models[stat]
        if r["model_sha256"] != sha or r["model_file"] != paper_predictions.STATS[stat]["model_file"]:
            other_sha.append(r["id"])
        v2 = predictor.v2_in_effect(r["game_date"])
        key = (f"game_date {'>=' if v2 else '<'} {predictor.V2_EFFECTIVE_DATE}, feature_def={r['feature_def']}, "
               f"features_v1_json {'present' if r['features_v1_json'] else 'NULL'}, "
               f"p_over_v1 {'present' if r['p_over_v1'] is not None else 'NULL'}")
        combos[key] = combos.get(key, 0) + 1
        ident = {"game_date": r["game_date"], "player_name": r["player_name"], "stat_type": stat,
                 "ud_line": r["ud_line"]}
        rec = {}
        for field, fcol in (("p_over", "features_json"), ("p_over_v1", "features_v1_json")):
            if field == "p_over_v1" and r["p_over_v1"] is None and r["features_v1_json"] is None:
                continue
            if r[fcol] is None:
                tallies[field].add(r["id"], r[field], None, ident, note=f"{fcol} is NULL")
                continue
            try:
                p, nan_cols = score(model, r[fcol])
            except Exception as exc:
                tallies[field].add(r["id"], r[field], None, ident, note=f"rescoring failed: {exc!r}"[:300])
                continue
            tallies[field].add(r["id"], r[field], p, ident)
            rec[field] = fhex(p)
            if field == "p_over":
                if (",".join(nan_cols) or None) == r["nan_features"]:
                    nan_agree += 1
                else:
                    nan_disagree += 1
        recomputed[str(r["id"])] = rec
    out = {
        "columns_used": (
            "p_over is checked against features_json and p_over_v1 against features_v1_json. "
            "paper_predictions writes the features that produced p_over to features_json: v2 for "
            f"game_date >= {predictor.V2_EFFECTIVE_DATE} (feature_def 'v2'), v1 before that (feature_def "
            "'v1'/NULL, shadow columns NULL). From that date the v1 features and their probability are "
            "kept in the shadow columns features_v1_json / p_over_v1. Each feature set is rebuilt in "
            "ml_trainer.FEATURE_COLS order, JSON null -> NaN -> 0 (fillna(0).astype(float)), and scored "
            "with predict_proba of the stat's frozen model loaded by paper_predictions._load_model."),
        "rows_total": len(rows), "rows_rescored": len(recomputed), "skipped_by_status": skipped,
        "rows_by_feature_def": combos,
        "rows_logged_with_other_model_file_or_sha": other_sha,
        "nan_features_column_agrees": nan_agree, "nan_features_column_disagrees": nan_disagree,
        "fields": {k: v.out() for k, v in tallies.items()},
        "recomputed": recomputed,
    }
    out["ok"] = all(v.out()["mismatch"] == 0 and v.out()["last_digit"] == 0 for v in tallies.values())
    return out


CH_COLS = ("id", "game_date", "player_name", "stat_type", "line", "market_key", "status", "n_prior", "mu",
           "alpha", "p_over", "expected_minutes", "rate_per_minute", "model_sha256", "model_fingerprint",
           "features_json")
CH_FIELDS = (("mu", "mu"), ("alpha", "alpha"), ("p_over", None), ("expected_minutes", "expected_minutes"),
             ("rate_per_minute", "rate"))


def run_replay_challenger(db: str, blocked: set) -> dict:
    import numpy as np
    import pandas as pd
    from challenger import live

    m = live.load_frozen()                             # SHA-256 + fingerprint checked, or raises
    conn = connect_ro(db, blocked)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"SELECT {', '.join(CH_COLS)} FROM challenger_predictions ORDER BY id").fetchall()
    conn.close()
    tallies = {f: FieldTally() for f, _ in CH_FIELDS}
    recomputed, skipped, other_model, n_prior_disagree, feature_set_differs = {}, {}, [], [], []
    for r in rows:
        if r["status"] != "ok":
            k = r["status"].split(":")[0]
            skipped[k] = skipped.get(k, 0) + 1
            continue
        if r["model_sha256"] != live.FROZEN["sha256"] or r["model_fingerprint"] != live.FROZEN["fingerprint"]:
            other_model.append(r["id"])
        ident = {"game_date": r["game_date"], "player_name": r["player_name"], "stat_type": r["stat_type"],
                 "market_key": r["market_key"], "line": r["line"]}
        try:
            feats = json.loads(r["features_json"])
            want = set(m.minutes_feats + m.rate_feats[r["stat_type"]])
            if set(feats) != want:
                feature_set_differs.append(r["id"])
            vals = {k: (np.nan if v is None else float(v)) for k, v in feats.items()}
            if "n_prior" in vals and vals["n_prior"] != r["n_prior"]:
                n_prior_disagree.append(r["id"])
            vals.setdefault("n_prior", r["n_prior"])
            res = live.predict_from_features(pd.DataFrame([vals]), r["stat_type"], [r["line"]], m)
            if res["status"] != "ok":
                raise ValueError(f"status {res['status']}")
        except Exception as exc:
            for f, _ in CH_FIELDS:
                tallies[f].add(r["id"], r[f], None, ident, note=f"rescoring failed: {exc!r}"[:300])
            continue
        new = {f: (res["p_over"][float(r["line"])] if k is None else res[k]) for f, k in CH_FIELDS}
        for f, _ in CH_FIELDS:
            tallies[f].add(r["id"], r[f], new[f], ident)
        recomputed[str(r["id"])] = {f: fhex(v) for f, v in new.items()}
    out = {
        "columns_used": ("features_json (the minutes and rate features the frozen challenger read, JSON null "
                         "-> NaN, which HistGradientBoosting handles natively; no fill) plus the n_prior column "
                         "if the features don't carry it, scored with live.predict_from_features(F, stat_type, "
                         "[line], live.load_frozen()). Compared: mu, alpha, p_over at the row's line, "
                         "expected_minutes, rate_per_minute."),
        "rows_total": len(rows), "rows_rescored": len(recomputed), "skipped_by_status": skipped,
        "insufficient_history_skipped": skipped.get("insufficient_history", 0),
        "rows_logged_with_other_model": other_model, "rows_n_prior_disagrees": n_prior_disagree,
        "rows_feature_set_differs": feature_set_differs,
        "fields": {k: v.out() for k, v in tallies.items()},
        "recomputed": recomputed,
    }
    out["ok"] = all(v.out()["mismatch"] == 0 and v.out()["last_digit"] == 0 for v in tallies.values())
    return out


# --- end to end ------------------------------------------------------------------------------

class PinnedClock:
    """datetime.now()/utcnow()/today() and date.today() return `pinned` (an aware UTC
    datetime) in the backend modules it is installed in; None = the real clock."""

    def __init__(self):
        self.pinned = None
        clock = self
        real = dt

        class PinnedDateTime(real.datetime):
            @classmethod
            def now(cls, tz=None):
                if clock.pinned is None:
                    return real.datetime.now(tz)
                return clock.pinned.astimezone(tz) if tz is not None else clock.pinned.astimezone().replace(tzinfo=None)

            @classmethod
            def utcnow(cls):
                return cls.now(real.timezone.utc).replace(tzinfo=None)

            @classmethod
            def today(cls):
                return cls.now()

        class PinnedDate(real.date):
            @classmethod
            def today(cls):
                return PinnedDateTime.now().date()

        self.datetime, self.date = PinnedDateTime, PinnedDate
        self.module = types.ModuleType("datetime")
        self.module.__dict__.update({k: getattr(real, k) for k in dir(real) if not k.startswith("__")})
        self.module.datetime, self.module.date = PinnedDateTime, PinnedDate


def _backend_modules():
    for name, mod in list(sys.modules.items()):
        f = getattr(mod, "__file__", None) or ""
        if f and os.path.abspath(f).startswith(BACKEND) and not f.endswith("parity_check.py"):
            yield name, mod


class Patched:
    """setattr with undo."""

    def __init__(self):
        self.saved = []

    def set(self, obj, attr, value):
        self.saved.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, value)

    def undo(self):
        for obj, attr, value in reversed(self.saved):
            setattr(obj, attr, value)
        self.saved = []


def resolve_pulls(conn, date: str, pull_ids: list[int] | None) -> tuple[list[dict], list[str]]:
    """odds_pull_log pre-tip pulls -> their line_snapshots capture: the game's latest pretip
    captured_at at or before the log row's started_at (written after the response)."""
    q = ("SELECT id, started_at, game_date, game_id, endpoint, pull_phase, http_status, rows_inserted "
         "FROM odds_pull_log WHERE ")
    if pull_ids:
        rows = conn.execute(q + f"id IN ({', '.join('?' * len(pull_ids))}) ORDER BY id", pull_ids).fetchall()
    else:
        rows = conn.execute(q + "game_date = ? AND endpoint = 'event_odds' AND pull_phase = 'pretip' "
                                "AND http_status = 200 AND rows_inserted > 0 ORDER BY id", (date,)).fetchall()
    errors = [f"pull {i}: not in odds_pull_log" for i in sorted(set(pull_ids or []) - {r[0] for r in rows})]
    pulls = []
    for pid, started, gdate, game_id, endpoint, phase, status, n in rows:
        if endpoint != "event_odds" or phase != "pretip" or status != 200 or not n:
            errors.append(f"pull {pid}: {endpoint}/{phase}, HTTP {status}, {n} rows: not a successful pre-tip pull")
            continue
        if gdate != date:
            errors.append(f"pull {pid}: game_date {gdate}, not --date {date}")
            continue
        cap = conn.execute("SELECT MAX(captured_at) FROM line_snapshots WHERE game_id = ? AND pull_phase = 'pretip' "
                           "AND captured_at <= ?", (game_id, started)).fetchone()[0]
        latest = conn.execute("SELECT MAX(captured_at) FROM line_snapshots WHERE game_id = ? "
                              "AND pull_phase = 'pretip'", (game_id,)).fetchone()[0]
        if cap is None:
            errors.append(f"pull {pid}: no pretip line_snapshots for {game_id} at or before {started}")
            continue
        pulls.append({"pull_id": pid, "game_id": game_id, "game_date": gdate, "started_at": started,
                      "captured_at": cap, "latest_pretip_capture": cap == latest})
    return pulls, errors


E2E_ORDER = {"paper_predictions": ("game_id", "player_name", "stat_type", "captured_at"),
             "challenger_predictions": ("game_id", "player_name", "stat_type", "captured_at", "market_key", "line")}
E2E_DROPPED = ("id", "created_at")


def export_rows(conn, table: str, classes: dict, keys: list[tuple]) -> dict:
    """Rows of `table` for the (game_id, captured_at) keys: every column except E2E_DROPPED and
    the excluded ones, sorted by game, player, stat, captured_at (then market and line)."""
    d = classes[table]
    cols = [c for c in d["columns"] if c not in d["excluded"] and c not in E2E_DROPPED]
    where = " OR ".join("(game_id = ? AND captured_at = ?)" for _ in keys) or "0"
    rows = conn.execute(f"SELECT {', '.join(_qi(c) for c in cols)} FROM {_qi(table)} WHERE {where} "
                        f"ORDER BY {', '.join(E2E_ORDER[table])}", [x for k in keys for x in k]).fetchall()
    h = hashlib.sha256(_header(table, cols))
    for r in rows:
        h.update(encode_row(r))
    return {"columns": cols, "rows": [[jsonable(v) for v in r] for r in rows], "n": len(rows), "sha256": h.hexdigest()}


def _diff_exports(a: dict, b: dict, table: str) -> dict:
    """Rows of a vs b keyed by E2E_ORDER: identical / differing columns / only in one."""
    def keyed(x):
        idx = [x["columns"].index(c) for c in E2E_ORDER[table]]
        return {json.dumps([r[i] for i in idx]): r for r in x["rows"]}
    ka, kb = keyed(a), keyed(b)
    differ = []
    for k in sorted(set(ka) & set(kb)):
        cols = [c for c, va, vb in zip(a["columns"], ka[k], kb[k]) if va != vb]
        if cols:
            differ.append({"key": json.loads(k), "columns": cols})
    return {"identical": sum(1 for k in set(ka) & set(kb) if ka[k] == kb[k]), "differ": differ,
            "only_first": [json.loads(k) for k in sorted(set(ka) - set(kb))],
            "only_second": [json.loads(k) for k in sorted(set(kb) - set(ka))]}


def run_end_to_end(db: str, date: str, pull_ids: list[int] | None, classes: dict, net: NetworkGuard) -> dict:
    own_blocked = blocked_reads(classes)
    tmp = tempfile.mkdtemp(prefix="parity_e2e_")
    copy = os.path.join(tmp, "predictions.db")
    patch = Patched()
    out = {"date": date, "temp_dir": tmp}
    try:
        backup_copy(db, copy)
        conn = connect_ro(copy, own_blocked)
        pulls, errors = resolve_pulls(conn, date, pull_ids)
        out["pulls"], out["errors"] = pulls, errors
        keys = [(p["game_id"], p["captured_at"]) for p in pulls]
        original = {t: export_rows(conn, t, classes, keys) for t in E2E_ORDER}
        conn.close()
        if errors or not pulls:
            out["errors"] = errors or [f"no successful pre-tip pull on {date}"]
            out["ok"] = False
            return out

        import importlib
        # every module on the pre-tip logging path, imported now so all their DB_PATH and clock
        # references are repointed below (a module first imported later would keep the live
        # path, and DbGuard would refuse its connection)
        for name in ("database", "line_logger", "rosters", "player_match", "data_fetcher", "game_types",
                     "feature_defs", "predictor", "ml_trainer", "paper_predictions", "challenger_log",
                     "challenger.live", "challenger.inference", "challenger.features"):
            importlib.import_module(name)
        import challenger_log
        import paper_predictions
        repointed = []
        for name, mod in _backend_modules():
            if isinstance(getattr(mod, "DB_PATH", None), str) and _target_file(mod.DB_PATH) == _target_file(LIVE_DBS[0]):
                patch.set(mod, "DB_PATH", copy)
                repointed.append(name)
        clock = PinnedClock()
        pinned_in = []
        for name, mod in _backend_modules():
            for attr, real, fake in (("dt", dt, clock.module), ("datetime", dt, clock.module),
                                     ("datetime", dt.datetime, clock.datetime), ("date", dt.date, clock.date)):
                if getattr(mod, attr, None) is real:
                    patch.set(mod, attr, fake)
                    pinned_in.append(f"{name}.{attr}")
        out["db_path_repointed_in"], out["clock_pinned_in"] = sorted(repointed), sorted(pinned_in)

        # the delete: the script's own read-write connection to the temp copy
        w = _REAL_CONNECT(copy, timeout=30)
        w.set_authorizer(authorizer(own_blocked))
        deleted = {t: 0 for t in E2E_ORDER}
        for gid, cap in keys:
            for t in E2E_ORDER:
                deleted[t] += w.execute(f"DELETE FROM {t} WHERE game_id = ? AND captured_at = ?", (gid, cap)).rowcount
        w.commit()
        w.close()
        out["deleted"] = deleted

        protected = [*LIVE_DBS, db]
        results = []
        with DbGuard(protected, blocked_reads(classes), ro_ok=False) as guard:
            for p in sorted(pulls, key=lambda p: (p["captured_at"], p["game_id"])):   # the order the jobs ran
                clock.pinned = dt.datetime.fromisoformat(p["captured_at"]).astimezone(dt.timezone.utc)
                n_net = len(net.attempts)
                # line_logger._log_paper_predictions / _log_challenger_predictions: log_for_pull(game_id),
                # paper first. captured_at is passed only if this pull is not the game's latest capture.
                cap = None if p["latest_pretip_capture"] else p["captured_at"]
                rec = {"pull_id": p["pull_id"], "game_id": p["game_id"], "pinned_now_utc": clock.pinned.isoformat()}
                for label, fn in (("paper", paper_predictions.log_for_pull), ("challenger", challenger_log.log_for_pull)):
                    try:
                        rec[label] = fn(p["game_id"], cap)
                    except Exception as exc:
                        rec[label] = {"error": repr(exc)[:500]}
                        errors.append(f"pull {p['pull_id']} {label}: {exc!r}"[:500])
                rec["network_attempts"] = len(net.attempts) - n_net
                results.append(rec)
                print(f"  pull {p['pull_id']} ({p['game_id']}): paper {rec['paper'].get('logged')}, "
                      f"challenger {rec['challenger'].get('logged')} rows")
            clock.pinned = None
            out["refused_connections"] = guard.refused
        out["log_for_pull"] = results

        conn = connect_ro(copy, own_blocked)
        rerun = {t: export_rows(conn, t, classes, keys) for t in E2E_ORDER}
        conn.close()
        out["export"] = rerun
        out["rerun_vs_stored (not a criterion)"] = {t: _diff_exports(original[t], rerun[t], t) for t in E2E_ORDER}
        out["columns"] = {
            "dropped": list(E2E_DROPPED),
            "excluded_outcomes": {t: sorted(classes[t]["excluded"]) for t in E2E_ORDER},
            "clock": ("datetime.now/utcnow/today and date.today are pinned to each pull's captured_at in the "
                      "modules listed in clock_pinned_in, so the next-game lookup and back-to-back "
                      "(data_fetcher.get_player_context), roster freshness (rosters.current_team), "
                      "is_playoff (data_fetcher.is_playoff_matchup) and created_at see the time of the original "
                      "pull; created_at is dropped anyway."),
            "kept_snapshot_dependent": ("paper data_as_of / data_stale and challenger data_as_of depend on the "
                                        "snapshot's player_stats and schedule, not on the clock: kept. They "
                                        "(and the features) can differ from the stored originals when a later "
                                        "ETL or roster refresh is in the snapshot, which is why the criterion "
                                        "is PC vs server on the same snapshot, not rerun vs stored."),
        }
        if date < challenger_log.EFFECTIVE_DATE:
            out["note"] = f"challenger_log skips games before {challenger_log.EFFECTIVE_DATE}: no challenger rows"
        out["ok"] = not errors and not out["refused_connections"]
        return out
    finally:
        patch.undo()
        shutil.rmtree(tmp, ignore_errors=True)
        out["temp_dir_removed"] = not os.path.exists(tmp)


# --- main ------------------------------------------------------------------------------------

def norm_name(name: str) -> str:
    """PEP 503: '-', '_' and '.' are equal, case is ignored."""
    return re.sub(r"[-_.]+", "-", name).lower()


def installed_packages() -> dict:
    from importlib import metadata
    out = {}
    for d in metadata.distributions():
        name = d.metadata["Name"]
        if name:
            out[norm_name(name)] = d.version
    return dict(sorted(out.items()))


_LOCK_LINE = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*==\s*([^\s;]+)\s*(?:;(.*))?")


def packages_vs_lock(installed: dict, lock_path: str = LOCK_FILE) -> dict:
    """Every lock line ('name==version', optional marker) installed at exactly that version.
    A line whose marker does not apply here is not required; any other line format FAILs.
    Installed packages not in the lock are listed as extra (INFO), with a note if Windows-only."""
    pins, skipped_marker, bad = {}, {}, []
    with open(lock_path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            s = line.split("#", 1)[0].strip()
            if not s:
                continue
            m = _LOCK_LINE.fullmatch(s)
            if not m:
                bad.append(f"line {i}: {s}")
                continue
            name, version, marker = norm_name(m.group(1)), m.group(3), (m.group(4) or "").strip()
            if marker:
                try:
                    from packaging.markers import Marker
                    applies = Marker(marker).evaluate()
                except Exception as exc:
                    bad.append(f"line {i}: marker not evaluated ({exc!r})")
                    continue
                if not applies:
                    skipped_marker[name] = version
                    continue
            pins[name] = version
    missing = [{"name": n, "lock": v} for n, v in pins.items() if n not in installed]
    wrong = [{"name": n, "lock": v, "installed": installed[n]} for n, v in pins.items()
             if n in installed and installed[n] != v]
    extra = [{"name": n, "version": v, "note": WINDOWS_ONLY.get(n)} for n, v in installed.items()
             if n not in pins and n not in skipped_marker]
    return {"lock_file": os.path.relpath(lock_path, BACKEND).replace(os.sep, "/"), "lock_sha256": _sha256_file(lock_path),
            "pins": len(pins), "not_required_by_marker": skipped_marker, "unparsed_lines": bad,
            "missing": missing, "wrong_version": wrong, "extra": extra,
            "pass": not (missing or wrong or bad)}


def _meta(args, modes) -> dict:
    pk = installed_packages()

    def git(*a):
        try:
            return subprocess.run(["git", *a], cwd=BACKEND, capture_output=True, text=True, timeout=30).stdout.strip()
        except Exception as exc:
            return f"unavailable: {exc!r}"

    out = {"tool": "parity_check", "tool_version": TOOL_VERSION, "encoding": ENCODING_VERSION.decode(),
           "rel_tol": REL_TOL, "host": socket.gethostname(), "platform": platform.platform(),
           "machine": platform.machine(), "python_version": platform.python_version(), "python": sys.version,
           "packages": pk, "packages_vs_lock": packages_vs_lock(pk),
           "git_commit": git("rev-parse", "HEAD"),
           "git_dirty": bool(git("status", "--porcelain", "--untracked-files=no")),
           "db": os.path.abspath(args.db), "modes": list(modes), "argv": sys.argv[1:],
           "local_utc_offset": time.strftime("%z"), "tz_name": time.tzname,
           "thread_env": {k: os.environ.get(k) for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")}}
    try:
        from threadpoolctl import threadpool_info
        out["threadpools"] = [{k: i.get(k) for k in ("internal_api", "version", "architecture", "num_threads")}
                              for i in threadpool_info()]
    except Exception as exc:
        out["threadpools"] = f"unavailable: {exc!r}"
    return out


def default_out() -> str:
    host = re.sub(r"[^A-Za-z0-9.-]", "_", socket.gethostname())
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return os.path.join(RESULTS_DIR, f"parity_{host}_{stamp}.json")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", required=True, help="a backup-API copy of predictions.db (opened read-only)")
    ap.add_argument("--out", help="output JSON (default: experiments/results/parity_<host>_<UTC>.json)")
    ap.add_argument("--mode", default=",".join(READ_ONLY_MODES), help=f"comma-separated: {', '.join(MODES)}")
    ap.add_argument("--date", help="end-to-end: ET game date YYYY-MM-DD")
    ap.add_argument("--pulls", help="end-to-end: odds_pull_log ids, comma-separated (default: every "
                                    "successful pre-tip pull on --date)")
    args = ap.parse_args(argv)
    modes = [m.strip() for m in args.mode.split(",") if m.strip()]
    bad = [m for m in modes if m not in MODES]
    if bad:
        ap.error(f"unknown mode(s) {bad}; choose from {list(MODES)}")
    if "end-to-end" in modes:
        if modes != ["end-to-end"]:
            ap.error("run end-to-end on its own (it repoints modules and pins the clock)")
        if not args.date or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date):
            ap.error("end-to-end needs --date YYYY-MM-DD")
    if not os.path.isfile(args.db):
        ap.error(f"--db {args.db}: no such file")
    pull_ids = [int(x) for x in args.pulls.split(",")] if args.pulls else None
    if _target_file(args.db) in {_target_file(p) for p in LIVE_DBS}:
        print("WARNING: --db is the live database; it is opened read-only, but use a backup-API copy.")
    out_path = args.out or default_out()

    started = dt.datetime.now(dt.timezone.utc)
    with NetworkGuard() as net:
        res = {"meta": _meta(args, modes)}
        conn = connect_ro(args.db, set())
        classes = outcome_columns(conn)
        conn.close()
        print_exclusions(classes)
        res["exclusions"] = {t: {"excluded": d["excluded"], "review": d["review"]} for t, d in classes.items()}
        own_blocked = blocked_reads(classes)
        runners = {
            "integrity": lambda: run_integrity(args.db, classes, own_blocked),
            "models": run_models,
            "replay-paper": lambda: run_replay_paper(args.db, own_blocked),
            "replay-challenger": lambda: run_replay_challenger(args.db, own_blocked),
            "end-to-end": lambda: run_end_to_end(args.db, args.date, pull_ids, classes, net),
        }
        for mode in modes:
            print(f"\n== {mode}")
            t0 = time.perf_counter()
            # every connection the code under test opens gets the authorizer; --db read-only only
            with DbGuard([*LIVE_DBS, args.db], blocked_reads(classes), ro_ok=True) \
                    if mode != "end-to-end" else _null():
                try:
                    sec = runners[mode]()
                except Exception as exc:
                    sec = {"ok": False, "error": repr(exc), "traceback": traceback.format_exc()[-3000:]}
                    print(f"  FAILED: {exc!r}")
            sec["seconds"] = round(time.perf_counter() - t0, 1)
            res[mode.replace("-", "_")] = sec
        res["network"] = {"attempts": net.attempts}
    res["denied_reads"] = DENIED
    res["meta"]["started_utc"] = started.isoformat(timespec="seconds")
    res["meta"]["finished_utc"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    ok = (all(res[m.replace("-", "_")].get("ok") for m in modes) and not net.attempts and not DENIED
          and res["meta"]["packages_vs_lock"]["pass"])
    res["ok"] = ok

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1, default=str)
    print_summary(res, modes)
    print(f"\nwrote {out_path}")
    return 0 if ok else 1


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def print_summary(res: dict, modes) -> None:
    print("\n== summary")
    m = res["meta"]
    print(f"  host {m['host']}, {m['platform']}, Python {m['python_version']}, commit {m['git_commit'][:12]}"
          f"{' (dirty)' if m['git_dirty'] else ''}")
    p = m["packages_vs_lock"]
    print(f"  packages vs {p['lock_file']}: {'PASS' if p['pass'] else 'FAIL'}: {p['pins']} pins, "
          f"missing {[x['name'] for x in p['missing']]}, wrong version {p['wrong_version']}, "
          f"unparsed {p['unparsed_lines']}; extra (INFO): "
          + (", ".join(f"{x['name']}=={x['version']}{' [' + x['note'] + ']' if x['note'] else ''}" for x in p["extra"])
             or "none"))
    for mode in modes:
        s = res[mode.replace("-", "_")]
        tag = "ok" if s.get("ok") else "NOT OK"
        if "error" in s:
            print(f"  {mode}: {tag}: {s['error']}")
        elif mode == "integrity":
            print(f"  integrity: {tag}: integrity_check {s['integrity_check']}, journal_mode {s['journal_mode']}, "
                  f"{len(s['tables'])} tables, {sum(t['rows'] for t in s['tables'].values()):,} rows hashed")
        elif mode == "models":
            counts = {}
            for r in s["files"].values():
                counts[r["status"]] = counts.get(r["status"], 0) + 1
            print(f"  models: {tag}: {counts}; code pins match the pre-registration: "
                  f"{all(v['matches_prereg'] for v in s['code_pins'].values()) and s['code_fingerprint_pin_matches_prereg']}")
        elif mode in ("replay-paper", "replay-challenger"):
            print(f"  {mode}: {tag}: {s['rows_rescored']} of {s['rows_total']} rows re-scored; skipped {s['skipped_by_status']}")
            for f, v in s["fields"].items():
                ld = (f" (max abs {v['last_digit_max_abs']:.3g}, {v['last_digit_max_ulps']} ulp)"
                      if v["last_digit"] else "")
                print(f"    {f}: {v['checked']} checked, {v['exact']} exact, {v['last_digit']} last-digit{ld}, "
                      f"{v['mismatch']} mismatch")
        elif mode == "end-to-end":
            ex = s.get("export", {})
            print(f"  end-to-end: {tag}: {len(s.get('pulls', []))} pulls; "
                  + ", ".join(f"{t} {e['n']} rows" for t, e in ex.items()) + f"; errors {s.get('errors')}")
    print(f"  network attempts: {len(res['network']['attempts'])}; denied outcome-column reads: {len(res['denied_reads'])}")


if __name__ == "__main__":
    sys.exit(main())
