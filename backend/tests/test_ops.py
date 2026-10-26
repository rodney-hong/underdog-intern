"""
Server operations (migration plan v2): Kaggle cache pruning (0.5, fake version folders under
KAGGLEHUB_CACHE), off-server backups against an in-memory fake bucket (0.7: retention,
model upload with manifest, restore passing PRAGMA integrity_check), and the deploy guard
(0.8). Temp files only; nothing reaches Kaggle or a real bucket.
"""
import os
import sqlite3
import sys

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

import database          # noqa: E402



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
