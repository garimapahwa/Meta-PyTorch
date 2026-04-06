#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$ROOT_DIR/.run"
APP_PID_FILE="$RUN_DIR/app.pid"
ES_PID_FILE="$RUN_DIR/elasticsearch.pid"
SHUTDOWN_TIMEOUT="${SHUTDOWN_TIMEOUT:-10}"

wait_for_exit() {
  local pid="$1"
  local timeout="$2"

  for _ in $(seq 1 "$timeout"); do
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done

  return 1
}

stop_process() {
  local label="$1"
  local port="$2"
  local pid_file="$3"
  local process_pattern="$4"

  local pid

  # Prefer PID files created by run-local.sh.
  if [ -f "$pid_file" ]; then
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if [ -n "$pid" ] && ! kill -0 "$pid" >/dev/null 2>&1; then
      pid=""
    fi
  fi

  # Fallback to the listener PID on the target port.
  if [ -z "$pid" ]; then
    pid="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | head -1 || true)"
  fi

  # Final fallback: command pattern match.
  if [ -z "$pid" ]; then
    pid="$(pgrep -f "$process_pattern" | head -1 || true)"
  fi

  if [ -n "$pid" ] && kill -0 "$pid" >/dev/null 2>&1; then
    echo "Stopping $label (pid $pid)..."
    kill "$pid"
    if wait_for_exit "$pid" "$SHUTDOWN_TIMEOUT"; then
      echo "$label stopped."
    else
      echo "$label did not exit after ${SHUTDOWN_TIMEOUT}s; sending SIGKILL..."
      kill -9 "$pid" >/dev/null 2>&1 || true
    fi
    rm -f "$pid_file"
  else
    echo "$label process not found or not running."
    rm -f "$pid_file"
  fi
}

stop_process "app" "7860" "$APP_PID_FILE" "uvicorn app:app|python -m uvicorn app:app|app.py"
stop_process "elasticsearch" "9200" "$ES_PID_FILE" "elasticsearch"

if [ -d "$RUN_DIR" ] && [ -z "$(ls -A "$RUN_DIR" 2>/dev/null)" ]; then
  rmdir "$RUN_DIR" 2>/dev/null || true
fi

echo "Stop request sent."
