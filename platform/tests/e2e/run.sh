#!/usr/bin/env bash
# Run the Playwright scenario tests against a freshly started instance.
#
# Assumes Node is available and `npm install` has been run in this directory.
# Set ANITA_E2E_URL to test an instance that is already running instead.
#
#   ./run.sh              desktop and mobile
#   ./run.sh --headed     watch it happen
#   ./run.sh --project=desktop
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLATFORM="$(cd "$HERE/../.." && pwd)"
PORT="${ANITA_E2E_PORT:-8085}"

if [ ! -d "$HERE/node_modules" ]; then
  echo "Installing Playwright (one time)…"
  (cd "$HERE" && npm install && npx playwright install chromium)
fi

started=""
cleanup() {
  if [ -n "$started" ]; then
    echo "Stopping the instance (pid $started)…"
    kill "$started" 2>/dev/null || true
  fi
}
trap cleanup EXIT

if [ -z "${ANITA_E2E_URL:-}" ]; then
  # The instance embeds the whole corpus at start-up (~25 s). Playwright's own
  # readiness check is the composer appearing, so this only has to start it.
  echo "Starting the instance on :$PORT …"
  (cd "$PLATFORM" && .venv/Scripts/python.exe -m streamlit run streamlit_app.py \
      --server.port "$PORT" --server.headless true --browser.gatherUsageStats false \
      > /tmp/anita-e2e-instance.log 2>&1) &
  started=$!

  echo "Waiting for the composer…"
  for _ in $(seq 1 60); do
    if curl -sf "http://127.0.0.1:$PORT/_stcore/health" > /dev/null 2>&1; then break; fi
    sleep 2
  done
  export ANITA_E2E_URL="http://127.0.0.1:$PORT"
fi

cd "$HERE"
npx playwright test "$@"
