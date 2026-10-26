"""
offsite_backup.py
Off-server backups to an S3-compatible bucket (DigitalOcean Spaces; cloud migration plan v2,
step 0.7). Settings in backend/.env:

  SPACES_KEY, SPACES_SECRET   access key limited to the bucket
  SPACES_BUCKET               e.g. sportsbettr-backups (private)
  SPACES_ENDPOINT             e.g. https://nyc3.digitaloceanspaces.com
  SPACES_REGION               e.g. nyc3

Every database copy is taken with SQLite's backup API (safe while the backend writes),
checked with PRAGMA integrity_check, gzipped, then uploaded; local temp files are deleted.

  nightly   (prod scheduler, BACKUP_HOUR_ET:BACKUP_MINUTE_ET = 04:30 ET) predictions.db ->
            predictions/daily/predictions_YYYY-MM-DD.db.gz, keeping the newest KEEP_DAILY (7);
            on Sundays also predictions/weekly/predictions_YYYY-Www.db.gz, keeping KEEP_WEEKLY (4).
            The result is written to backups/offsite_last.json for daily_check.py.

CLI (any mode; manual):
  python offsite_backup.py nightly              # run the nightly backup now
  python offsite_backup.py upload-models        # once: the 12 model_*.pkl + challenger/models/*,
                                                # with a SHA-256 manifest; never overwrites a
                                                # different file under the same name
  python offsite_backup.py upload-historical    # from the PC after a backfill: historical.db,
                                                # keeping the newest KEEP_HISTORICAL (3)
  python offsite_backup.py list [prefix]
  python offsite_backup.py restore KEY DEST     # download, gunzip, integrity_check; DEST must not exist
"""

import datetime as dt
import glob
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

HERE = os.path.dirname(os.path.abspath(__file__))
EASTERN = ZoneInfo("America/New_York")
BACKUP_HOUR_ET, BACKUP_MINUTE_ET = 4, 30     # after the last game, before the 06:00 scheduler
KEEP_DAILY, KEEP_WEEKLY, KEEP_HISTORICAL = 7, 4, 3
SETTINGS = ("SPACES_KEY", "SPACES_SECRET", "SPACES_BUCKET", "SPACES_ENDPOINT")
STATUS_FILE = "offsite_last.json"


def missing_settings() -> list[str]:
    return [k for k in SETTINGS if not os.getenv(k, "").strip()]


def configured() -> bool:
    return not missing_settings()


def bucket() -> str:
    return os.environ["SPACES_BUCKET"].strip()


def client():
    """boto3 S3 client for the bucket's endpoint (tests pass their own fake instead)."""
    import boto3
    return boto3.client("s3", endpoint_url=os.environ["SPACES_ENDPOINT"].strip(),
                        region_name=os.getenv("SPACES_REGION", "nyc3").strip(),
                        aws_access_key_id=os.environ["SPACES_KEY"].strip(),
                        aws_secret_access_key=os.environ["SPACES_SECRET"].strip())


# --- local copies ---------------------------------------------------------------------

def sqlite_copy(src: str, dst: str) -> None:
    """Backup-API copy of src to dst, then PRAGMA integrity_check; raises unless 'ok'."""
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    d = sqlite3.connect(dst, timeout=30)
    try:
        s.backup(d)
        check = d.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        d.close()
        s.close()
    if check != "ok":
        raise RuntimeError(f"backup copy of {src} failed integrity_check: {check}")


def gzip_file(src: str, dst: str) -> None:
    with open(src, "rb") as f, gzip.open(dst, "wb", compresslevel=6) as g:
        shutil.copyfileobj(f, g, 1024 * 1024)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _staging_dir(near: str) -> str:
    """Temp folder on the database's own disk (backups/), not /tmp (may be small or tmpfs)."""
    base = os.path.join(os.path.dirname(os.path.abspath(near)), "backups")
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(prefix="offsite_", dir=base)


def _upload_db(db_path: str, keys: list[str], s3=None) -> dict:
    """Copy, check, gzip and upload db_path under every key in `keys`. Returns sizes and hash."""
    s3 = s3 or client()
    tmp = _staging_dir(db_path)
    try:
        copy = os.path.join(tmp, "copy.db")
        gz = copy + ".gz"
        sqlite_copy(db_path, copy)
        gzip_file(copy, gz)
        sha = sha256_file(gz)
        for key in keys:
            s3.upload_file(gz, bucket(), key, ExtraArgs={"Metadata": {"sha256": sha}, "ACL": "private"})
        return {"keys": keys, "db_bytes": os.path.getsize(copy), "gz_bytes": os.path.getsize(gz), "sha256": sha}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- bucket listing and retention -----------------------------------------------------

def list_keys(prefix: str, s3=None) -> list[str]:
    s3 = s3 or client()
    keys, token = [], None
    while True:
        kw = {"Bucket": bucket(), "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        keys += [o["Key"] for o in page.get("Contents", [])]
        if not page.get("IsTruncated"):
            return sorted(keys)
        token = page["NextContinuationToken"]


def prune(prefix: str, keep: int, s3=None) -> list[str]:
    """Delete all but the newest `keep` keys under prefix (names sort by date)."""
    s3 = s3 or client()
    keys = list_keys(prefix, s3)
    old = keys[:-keep] if keep > 0 else keys
    for k in old:
        s3.delete_object(Bucket=bucket(), Key=k)
    return old


# --- jobs -----------------------------------------------------------------------------

def nightly(db_path: str | None = None, today: dt.date | None = None, s3=None) -> dict:
    """predictions.db -> daily (and on Sundays weekly) keys, then retention. Raises on failure."""
    from database import DB_PATH
    db_path = db_path or DB_PATH
    s3 = s3 or client()
    today = today or dt.datetime.now(EASTERN).date()
    keys = [f"predictions/daily/predictions_{today.isoformat()}.db.gz"]
    if today.weekday() == 6:
        y, w, _ = today.isocalendar()
        keys.append(f"predictions/weekly/predictions_{y}-W{w:02d}.db.gz")
    out = _upload_db(db_path, keys, s3)
    out["pruned"] = prune("predictions/daily/", KEEP_DAILY, s3) + prune("predictions/weekly/", KEEP_WEEKLY, s3)
    return out


def run_nightly() -> dict:
    """Scheduler entry point: never raises; logs and writes backups/offsite_last.json."""
    from database import DB_PATH
    started = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    try:
        res = {"ok": True, **nightly()}
        print(f"[backup] off-server backup OK: {res['keys']} ({res['gz_bytes'] / 1e6:.0f} MB gz); "
              f"pruned {len(res['pruned'])}")
    except Exception as exc:
        res = {"ok": False, "error": repr(exc)}
        print("!" * 78)
        print(f"[backup] off-server backup FAILED: {exc!r}")
        print("!" * 78)
    res["at"] = started
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "backups", STATUS_FILE)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=1)
    except OSError as exc:
        print(f"[backup] could not write {STATUS_FILE}: {exc!r}")
    return res


def model_files() -> list[str]:
    return sorted(glob.glob(os.path.join(HERE, "model_*.pkl"))
                  + glob.glob(os.path.join(HERE, "challenger", "models", "*.pkl")))


def upload_models(s3=None) -> dict:
    """Each model file under models/<relative path>, plus models/MANIFEST.sha256. A key that
    already holds the same SHA-256 is skipped; a different file under that name stops the run."""
    s3 = s3 or client()
    files = model_files()
    if not files:
        raise FileNotFoundError("no model files found")
    manifest, uploaded, skipped = [], [], []
    for path in files:
        rel = os.path.relpath(path, HERE).replace(os.sep, "/")
        key, sha = f"models/{rel}", sha256_file(path)
        manifest.append(f"{sha}  {rel}")
        try:
            have = s3.head_object(Bucket=bucket(), Key=key).get("Metadata", {}).get("sha256")
        except Exception:
            have = None
        if have == sha:
            skipped.append(rel)
            continue
        if have is not None:
            raise RuntimeError(f"{key} already holds a different file (sha256 {have}); not overwriting")
        s3.upload_file(path, bucket(), key, ExtraArgs={"Metadata": {"sha256": sha}, "ACL": "private"})
        uploaded.append(rel)
    tmp = _staging_dir(os.path.join(HERE, "predictions.db"))
    try:
        mpath = os.path.join(tmp, "MANIFEST.sha256")
        with open(mpath, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(manifest) + "\n")
        s3.upload_file(mpath, bucket(), "models/MANIFEST.sha256", ExtraArgs={"ACL": "private"})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"uploaded": uploaded, "skipped": skipped, "manifest": manifest}


def upload_historical(db_path: str | None = None, s3=None, now: dt.datetime | None = None) -> dict:
    db_path = db_path or os.path.join(HERE, "historical.db")
    if not os.path.exists(db_path):
        raise FileNotFoundError(db_path)
    s3 = s3 or client()
    stamp = (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    out = _upload_db(db_path, [f"historical/historical_{stamp}.db.gz"], s3)
    out["pruned"] = prune("historical/", KEEP_HISTORICAL, s3)
    return out


def restore(key: str, dest: str, s3=None) -> dict:
    """Download key, gunzip to dest (which must not exist), PRAGMA integrity_check."""
    if os.path.exists(dest):
        raise FileExistsError(f"{dest} exists; restore never overwrites a database")
    s3 = s3 or client()
    tmp = _staging_dir(dest)
    try:
        gz = os.path.join(tmp, "download.gz")
        s3.download_file(bucket(), key, gz)
        part = dest + ".partial"
        with gzip.open(gz, "rb") as g, open(part, "wb") as f:
            shutil.copyfileobj(g, f, 1024 * 1024)
        conn = sqlite3.connect(f"file:{part}?mode=ro", uri=True)
        try:
            check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()
        if check != "ok":
            os.remove(part)
            raise RuntimeError(f"{key}: restored copy failed integrity_check: {check}")
        os.replace(part, dest)
        return {"key": key, "dest": dest, "integrity_check": check, "bytes": os.path.getsize(dest)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _main(argv: list[str]) -> None:
    if missing_settings():
        sys.exit(f"set {', '.join(missing_settings())} in backend/.env first")
    cmd = argv[0] if argv else ""
    if cmd == "nightly":
        print(run_nightly())
    elif cmd == "upload-models":
        print(json.dumps(upload_models(), indent=1))
    elif cmd == "upload-historical":
        print(upload_historical())
    elif cmd == "list":
        print("\n".join(list_keys(argv[1] if len(argv) > 1 else "")))
    elif cmd == "restore" and len(argv) == 3:
        print(restore(argv[1], argv[2]))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    _main(sys.argv[1:])
