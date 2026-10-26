#!/usr/bin/env bash
# deploy.sh: update the server to the latest pushed commit (cloud migration plan v2, step 0.8).
# Run on the server from the repo root as the sportsbettr user:  ./deploy.sh
#
# Refuses (backend/deploy_guard.py) within 45 min of a pre-tip pull, at 10:00-10:05 ET, or
# while a sync/ETL holds the ETL lock; checked before pulling and again right before the
# restart. Otherwise: git pull (fast-forward only), install from the lock file if it
# changed, rebuild the frontend if frontend/ changed, restart the service, check /health.
# .deployed holds the commit the service last restarted on, so a deploy refused at the
# second check restarts on the next run even though there is nothing new to pull.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE="${SPORTSBETTR_SERVICE:-sportsbettr}"
PY="$REPO/backend/.venv/bin/python"
DEPLOYED="$REPO/.deployed"
cd "$REPO"

guard() { (cd "$REPO/backend" && "$PY" deploy_guard.py); }

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "[deploy] REFUSED: tracked files changed on the server; it runs committed code only"; exit 1
fi
guard || exit 1

before="$(git rev-parse HEAD)"
running="$(cat "$DEPLOYED" 2>/dev/null || echo "$before")"
git pull --ff-only
after="$(git rev-parse HEAD)"
if [ "$running" = "$after" ]; then
    echo "[deploy] already running $after; nothing to do"; exit 0
fi
changed="$(git diff --name-only "$running" "$after")"
echo "[deploy] $running -> $after"

if grep -qx "backend/requirements-lock.txt" <<<"$changed"; then
    echo "[deploy] lock file changed: installing exact versions"
    if command -v uv >/dev/null 2>&1; then
        uv pip install --python "$PY" --no-deps -r backend/requirements-lock.txt
    else
        "$PY" -m pip install --no-deps -r backend/requirements-lock.txt
    fi
fi

if grep -q "^frontend/" <<<"$changed"; then
    echo "[deploy] frontend changed: rebuilding"
    cd "$REPO/frontend"
    if grep -qx "frontend/package-lock.json" <<<"$changed"; then
        npm ci
    fi
    VITE_API_BASE=/api npm run build
    cd "$REPO"
fi

if ! guard; then
    echo "[deploy] code updated to $after but the service was NOT restarted; run ./deploy.sh later"
    exit 1
fi
sudo systemctl restart "$SERVICE"
echo "$after" > "$DEPLOYED"
for _ in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
        echo "[deploy] $SERVICE restarted at $after; /health OK"; exit 0
    fi
    sleep 2
done
echo "[deploy] WARNING: /health did not answer within 60 s; check: journalctl -u $SERVICE -n 100"; exit 1
