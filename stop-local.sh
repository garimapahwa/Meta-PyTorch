#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$ROOT_DIR/.run"
APP_PID_FILE="$RUN_DIR/app.pid"
ES_PID_FILE="$RUN_DIR/elasticsearch.pid"

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
    rm -f "$pid_file"
  else
    echo "$label process not found or not running."
    rm -f "$pid_file"
  fi
}

stop_process "app" "7860" "$APP_PID_FILE" "uvicorn app:app|python -m uvicorn app:app|app.py"
stop_process "elasticsearch" "9200" "$ES_PID_FILE" "elasticsearch"

echo "Stop request sent."
