#!/usr/bin/env bash
# Run the browser-mic voice scenarios (US-003 / US-008) against a freshly
# started FastAPI instance.
#
# The main run.sh drives the Streamlit surface; the voice client lives on the
# FastAPI-served buyer page (chat.html), so it gets its own instance and its own
# run. Same rules as run.sh: this script refuses to drive an instance it did not
# start, and it kills the whole process tree on the way out.
#
#   ./run_voice.sh
#   ./run_voice.sh --headed
#   ./run_voice.sh --project=desktop
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLATFORM="$(cd "$HERE/../.." && pwd)"
PORT="${ANITA_VOICE_E2E_PORT:-8100}"

if [ ! -d "$HERE/node_modules" ]; then
  echo "Installing Playwright (one time)…"
  (cd "$HERE" && npm install && npx playwright install chromium)
fi

started=""
kill_tree() {
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

if [ -z "${ANITA_VOICE_E2E_URL:-}" ]; then
  if curl -sf "http://127.0.0.1:$PORT/health" > /dev/null 2>&1; then
    echo "Port $PORT already answers — refusing to test an instance this script did not start."
    exit 2
  fi

  echo "Starting the API instance on :$PORT …"
  (cd "$PLATFORM" && .venv/Scripts/python.exe scripts/serve.py --port "$PORT") \
      > /tmp/anita-voice-e2e-instance.log 2>&1 &
  started=$!

  echo "Waiting for /health…"
  for _ in $(seq 1 60); do
    if curl -sf "http://127.0.0.1:$PORT/health" > /dev/null 2>&1; then break; fi
    sleep 2
  done

  if ! kill -0 "$started" 2>/dev/null; then
    echo "The instance exited during start-up — see /tmp/anita-voice-e2e-instance.log"
    exit 2
  fi
  export ANITA_VOICE_E2E_URL="http://127.0.0.1:$PORT"
fi

cd "$HERE"
npx playwright test voice.spec.js "$@"
