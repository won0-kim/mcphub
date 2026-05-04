#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f hub.pid ]; then
  echo "No hub.pid found. Nothing to stop."
  exit 0
fi

PID="$(cat hub.pid)"
if ! kill -0 "$PID" 2>/dev/null; then
  echo "PID $PID is not running. Removing stale hub.pid."
  rm -f hub.pid
  exit 0
fi

echo "Stopping hub PID $PID..."
kill "$PID" 2>/dev/null || true
# Give it a moment, then SIGKILL if still alive
for _ in 1 2 3 4 5; do
  if ! kill -0 "$PID" 2>/dev/null; then break; fi
  sleep 0.2
done
if kill -0 "$PID" 2>/dev/null; then
  kill -9 "$PID" 2>/dev/null || true
fi
rm -f hub.pid
