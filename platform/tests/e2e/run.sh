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
kill_tree() {
  # Streamlit is a parent + child; killing only the parent leaves the child
  # holding the port, so the next run silently drives the previous instance.
  local pid="$1"
  case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*)
      taskkill //PID "$pid" //T //F > /dev/null 2>&1 || true ;;
    *)
      kill -- "-$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true ;;
  esac
}
cleanup() {
  if [ -n "$started" ]; then
    echo "Stopping the instance (pid $started and its children)…"
    kill_tree "$started"
  fi
}
trap cleanup EXIT

if [ -z "${ANITA_E2E_URL:-}" ]; then
  # Refuse to drive a server this script did not start. A leftover instance —
  # a child that survived a previous kill, or a dev server — may be running
  # stale or module-reloaded code, and every failure it produces reads as a
  # product regression. (This happened: two suite runs silently tested one
  # long-lived instance and "a greeting is greeted" failed both times.)
  if curl -sf "http://127.0.0.1:$PORT/_stcore/health" > /dev/null 2>&1; then
    echo "Port $PORT already answers — refusing to test an instance this script did not start."
    echo "Stop it (Windows: taskkill /PID <pid> /T /F) or set ANITA_E2E_URL to drive it deliberately."
    exit 2
  fi

  # The instance embeds the whole corpus at start-up (~25 s). Playwright's own
  # readiness check is the composer appearing, so this only has to start it.
  # The redirection goes on the *subshell*, not the command inside it: applied
  # inside, the subshell itself keeps the caller's stdout, the spawned instance
  # holds the pipe open, and anything piping this script (CI logs) waits for the
  # instance to die — which is exactly what a hung `run.sh` looked like.
  echo "Starting the instance on :$PORT …"
  (cd "$PLATFORM" && .venv/Scripts/python.exe -m streamlit run streamlit_app.py \
      --server.port "$PORT" --server.headless true --browser.gatherUsageStats false) \
      > /tmp/anita-e2e-instance.log 2>&1 &
  started=$!

  echo "Waiting for the composer…"
  for _ in $(seq 1 60); do
    if curl -sf "http://127.0.0.1:$PORT/_stcore/health" > /dev/null 2>&1; then break; fi
    sleep 2
  done

  # "Up" is not enough: the thing answering might not be ours.
  if grep -q "is not available" /tmp/anita-e2e-instance.log 2>/dev/null; then
    echo "The instance failed to bind :$PORT — see /tmp/anita-e2e-instance.log"
    exit 2
  fi
  if ! kill -0 "$started" 2>/dev/null; then
    echo "The instance exited during start-up — see /tmp/anita-e2e-instance.log"
    exit 2
  fi
  export ANITA_E2E_URL="http://127.0.0.1:$PORT"
fi

cd "$HERE"
npx playwright test "$@"
