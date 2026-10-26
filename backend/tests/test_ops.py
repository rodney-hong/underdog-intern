"""
Server operations (migration plan v2): Kaggle cache pruning (0.5, fake version folders under
KAGGLEHUB_CACHE), off-server backups against an in-memory fake bucket (0.7: retention,
model upload with manifest, restore passing PRAGMA integrity_check), and the deploy guard
(0.8). Temp files only; nothing reaches Kaggle or a real bucket.
"""
import datetime as dt
import gzip
import os
import sqlite3
import subprocess
import sys

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

import database          # noqa: E402
import deploy_guard      # noqa: E402
import offsite_backup    # noqa: E402

ET = deploy_guard.EASTERN


# --- 0.5 Kaggle cache pruning ----------------------------------------------------------

def _fake_cache(root, versions, per_file=(), bundle=()):
    base = os.path.join(root, "datasets", "eoinamoore", "historical-nba-data-and-player-box-scores")
    for v in versions:
        d = os.path.join(base, "versions", str(v))
        os.makedirs(d)
        with open(os.path.join(d, "PlayerStatisticsExtended.csv"), "wb") as f:
            f.write(b"x" * 1000)
    for v in per_file:
        d = os.path.join(base, ".complete", str(v))
        os.makedirs(d)
        open(os.path.join(d, "Players.csv.complete"), "w").close()
    for v in bundle:
        open(os.path.join(base, f"{v}.complete"), "w").close()
    return base


def test_cache_location_follows_kagglehub_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("KAGGLEHUB_CACHE", str(tmp_path / "kc"))
    assert database.kaggle_dataset_cache_dir() == os.path.join(
        str(tmp_path / "kc"), "datasets", "eoinamoore", "historical-nba-data-and-player-box-scores")


def test_prune_keeps_only_the_loaded_version_and_newer(tmp_path, monkeypatch):
    monkeypatch.setenv("KAGGLEHUB_CACHE", str(tmp_path))
    base = _fake_cache(str(tmp_path), [383, 520, 525, 526, 527], per_file=[525, 526, 527], bundle=[383, 520])
    out = database.prune_kaggle_cache(526)
    assert out["removed"] == [383, 520, 525] and out["errors"] == [] and out["freed_bytes"] == 3000
    assert sorted(os.listdir(os.path.join(base, "versions"))) == ["526", "527"]   # 527: a newer download
    assert sorted(os.listdir(os.path.join(base, ".complete"))) == ["526", "527"]
    assert not any(f.endswith(".complete") for f in os.listdir(base) if f != ".complete")
    assert database.prune_kaggle_cache(526)["removed"] == []                      # idempotent


def test_prune_without_a_cache_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.setenv("KAGGLEHUB_CACHE", str(tmp_path / "none"))
    assert database.prune_kaggle_cache(526) == {"removed": [], "freed_bytes": 0, "errors": []}


def test_a_successful_etl_prunes_older_versions(tmp_path, monkeypatch):
    """run_etl (Kaggle stubbed) prunes with the version it just swapped in; a skipped ETL doesn't."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from test_proc_locks import _version_files
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "e.db"))
    database.init_db()
    files = _version_files(tmp_path, 10, "v523")
    monkeypatch.setattr(database, "backup_predictions_to_csv", lambda: None)
    monkeypatch.setattr(database, "resolve_pending_predictions", lambda: 0)
    monkeypatch.setattr(database, "get_latest_kaggle_version", lambda: 523)
    monkeypatch.setattr(database, "download_kaggle_files", lambda v: files)
    calls = []
    monkeypatch.setattr(database, "prune_kaggle_cache", lambda v: calls.append(v))
    assert database.run_etl() is True and calls == [523]
    assert database.run_etl() is False and calls == [523]       # up to date: no load, no prune


# --- 0.7 off-server backups -------------------------------------------------------------

class FakeS3:
    """The subset of boto3's S3 client that offsite_backup uses, in memory."""
    def __init__(self):
        self.objects = {}

    def upload_file(self, Filename, Bucket, Key, ExtraArgs=None):
        with open(Filename, "rb") as f:
            self.objects[Key] = (f.read(), (ExtraArgs or {}).get("Metadata", {}))

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start:start + 2]                         # tiny pages: exercise pagination
        out = {"Contents": [{"Key": k} for k in page], "IsTruncated": start + 2 < len(keys)}
        if out["IsTruncated"]:
            out["NextContinuationToken"] = str(start + 2)
        return out

    def delete_object(self, Bucket, Key):
        del self.objects[Key]

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Metadata": self.objects[Key][1]}

    def download_file(self, Bucket, Key, Filename):
        with open(Filename, "wb") as f:
            f.write(self.objects[Key][0])


@pytest.fixture
def bucket(monkeypatch, tmp_path):
    for k, v in {"SPACES_KEY": "k", "SPACES_SECRET": "s", "SPACES_BUCKET": "sportsbettr-backups",
                 "SPACES_ENDPOINT": "https://nyc3.digitaloceanspaces.com"}.items():
        monkeypatch.setenv(k, v)
    db = str(tmp_path / "predictions.db")
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(1000)])
    conn.commit()
    conn.close()
    return FakeS3(), db


def test_nightly_backup_uploads_and_keeps_7_daily_and_4_weekly(bucket):
    s3, db = bucket
    day = dt.date(2026, 11, 1)                                # a Sunday
    for k in range(40):
        offsite_backup.nightly(db, today=day + dt.timedelta(days=k), s3=s3)
    daily = sorted(k for k in s3.objects if k.startswith("predictions/daily/"))
    weekly = sorted(k for k in s3.objects if k.startswith("predictions/weekly/"))
    assert len(daily) == 7 and daily[-1] == "predictions/daily/predictions_2026-12-10.db.gz"
    assert len(weekly) == 4 and weekly[-1] == "predictions/weekly/predictions_2026-W49.db.gz"
    assert not [f for f in os.listdir(os.path.dirname(db) + "/backups") if f.startswith("offsite_")]  # temp gone


def test_restore_passes_integrity_check_and_matches(bucket, tmp_path):
    s3, db = bucket
    out = offsite_backup.nightly(db, today=dt.date(2026, 11, 3), s3=s3)
    data, meta = s3.objects[out["keys"][0]]
    assert meta["sha256"] == out["sha256"] and gzip.decompress(data)[:16] == b"SQLite format 3\x00"
    dest = str(tmp_path / "restored.db")
    r = offsite_backup.restore(out["keys"][0], dest, s3=s3)
    assert r["integrity_check"] == "ok"
    conn = sqlite3.connect(dest)
    assert conn.execute("SELECT COUNT(*), SUM(x) FROM t").fetchone() == (1000, sum(range(1000)))
    conn.close()
    with pytest.raises(FileExistsError):
        offsite_backup.restore(out["keys"][0], dest, s3=s3)    # never overwrites


def test_restore_refuses_a_corrupt_backup(bucket, tmp_path):
    s3, _ = bucket
    s3.objects["predictions/daily/bad.db.gz"] = (gzip.compress(b"SQLite format 3\x00" + b"\x00" * 5000), {})
    dest = str(tmp_path / "bad.db")
    with pytest.raises(Exception):
        offsite_backup.restore("predictions/daily/bad.db.gz", dest, s3=s3)
    assert not os.path.exists(dest)


def test_upload_models_once_with_manifest_and_never_overwrites(bucket, tmp_path, monkeypatch):
    s3, _ = bucket
    (tmp_path / "challenger" / "models").mkdir(parents=True)
    for name, body in (("model_Points.pkl", b"p"), ("challenger/models/c.pkl", b"c")):
        (tmp_path / name).write_bytes(body)
    monkeypatch.setattr(offsite_backup, "HERE", str(tmp_path))
    out = offsite_backup.upload_models(s3=s3)
    assert out["uploaded"] == ["challenger/models/c.pkl", "model_Points.pkl"]
    assert "models/MANIFEST.sha256" in s3.objects
    assert offsite_backup.upload_models(s3=s3)["skipped"] == out["uploaded"]       # same files: skipped
    (tmp_path / "model_Points.pkl").write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="different file"):
        offsite_backup.upload_models(s3=s3)


def test_upload_historical_keeps_3(bucket):
    s3, db = bucket
    for k in range(5):
        offsite_backup.upload_historical(db, s3=s3, now=dt.datetime(2026, 11, 10 + k, tzinfo=dt.timezone.utc))
    assert sorted(s3.objects) == [f"historical/historical_202611{d}T000000Z.db.gz" for d in (12, 13, 14)]


def test_run_nightly_never_raises_and_records_the_result(bucket, monkeypatch, tmp_path):
    _, db = bucket
    monkeypatch.setattr(database, "DB_PATH", db)
    monkeypatch.setattr(offsite_backup, "nightly", lambda: (_ for _ in ()).throw(RuntimeError("bucket down")))
    res = offsite_backup.run_nightly()
    assert res["ok"] is False and "bucket down" in res["error"]
    import json
    assert json.load(open(os.path.join(os.path.dirname(db), "backups", "offsite_last.json")))["ok"] is False


def test_backup_settings(monkeypatch):
    for k in offsite_backup.SETTINGS:
        monkeypatch.delenv(k, raising=False)
    assert not offsite_backup.configured() and offsite_backup.missing_settings() == list(offsite_backup.SETTINGS)


# --- 0.8 deploy guard ----------------------------------------------------------------------

@pytest.fixture
def sched_db(tmp_path):
    path = str(tmp_path / "s.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schedule (gameId INTEGER, gameDateTimeEst TEXT)")
    conn.executemany("INSERT INTO schedule VALUES (?, ?)", [(1, "2026-11-05 19:00:00"), (2, "2026-11-05 22:30:00"),
                                                            (3, "2026-11-06 00:10:00")])
    conn.commit()
    conn.close()
    return path


def _at(day, hh, mm):
    return dt.datetime.combine(dt.date.fromisoformat(day), dt.time(hh, mm), ET)


@pytest.mark.parametrize("when, ok", [
    (("2026-11-05", 9, 59), True),
    (("2026-11-05", 10, 0), False),          # open pull
    (("2026-11-05", 10, 4), False),
    (("2026-11-05", 10, 5), True),
    (("2026-11-05", 17, 44), True),          # pre-tip 18:30: guard 17:45-19:15
    (("2026-11-05", 17, 45), False),
    (("2026-11-05", 18, 30), False),
    (("2026-11-05", 19, 15), False),
    (("2026-11-05", 19, 16), True),
    (("2026-11-05", 21, 20), False),         # pre-tip 22:00
    (("2026-11-05", 23, 0), False),          # 00:10 tip next day: pre-tip 23:40
    (("2026-11-06", 0, 26), True),
])
def test_deploy_guard_windows(sched_db, when, ok):
    got, why = deploy_guard.check(_at(*when), sched_db, etl_lock_free=lambda: True)
    assert got is ok, why


def test_deploy_guard_refuses_while_the_etl_lock_is_held(sched_db):
    ok, why = deploy_guard.check(_at("2026-11-05", 12, 0), sched_db, etl_lock_free=lambda: False)
    assert not ok and "ETL lock" in why


def test_deploy_guard_cli_exit_codes(sched_db, tmp_path):
    """The command deploy.sh runs: exit 1 and a reason when refused (time pinned in the child)."""
    code = ("import sys, datetime as dt; sys.path.insert(0, sys.argv[1]); import deploy_guard as g; "
            "ok, why = g.check(dt.datetime(2026, 11, 5, 15, 0, tzinfo=dt.timezone.utc), sys.argv[2], lambda: True); "
            "print(why); sys.exit(0 if ok else 1)")
    r = subprocess.run([sys.executable, "-c", code, BACKEND, sched_db], capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and "open-pull window" in r.stdout      # 15:00 UTC = 10:00 ET


def test_deploy_sh_parses_and_runs_the_guard_twice():
    sh = os.path.join(os.path.dirname(BACKEND), "deploy.sh")
    text = open(sh, encoding="utf-8").read()
    assert text.count("guard ||") + text.count("if ! guard") == 2
    assert "--no-deps -r backend/requirements-lock.txt" in text and "VITE_API_BASE=/api" in text
    bash = "bash"
    try:
        r = subprocess.run([bash, "-n"], input=text.encode(), capture_output=True, timeout=30)   # bytes: LF kept
    except FileNotFoundError:
        pytest.skip("no bash on this machine")
    assert r.returncode == 0, r.stderr.decode()
