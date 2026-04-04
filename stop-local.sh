#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$ROOT_DIR/.run"

stop_process() {
  local label="$1"
  local port="$2"
  local process_name="$3"

  # Try to find PID using lsof first
  local pid
  pid="$(lsof -i :$port -sTCP:LISTEN -Fp 2>/dev/null | sed 's/^p//')"
  if [ -z "$pid" ]; then
    # Fallback to ps if lsof fails
    pid="$(ps -ef | grep "$process_name" | grep -v grep | awk '{print $2}' | head -1)"
  fi

  if [ -n "$pid" ] && kill -0 "$pid" >/dev/null 2>&1; then
    echo "Stopping $label (pid $pid)..."
    kill "$pid"
  else
    echo "$label process not found or not running."
  fi
}

stop_process "app" "7860" "app.py"
stop_process "elasticsearch" "9200" "elasticsearch"

echo "Stop request sent."
