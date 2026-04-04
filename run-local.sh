#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$ROOT_DIR/.run"
ES_DIR="$ROOT_DIR/elasticsearch-9.3.2"
VENV_PYTHON="$ROOT_DIR/.venv/bin/python"
APP_HOST="127.0.0.1"
APP_PORT="7860"
ES_URL="http://127.0.0.1:9200"
APP_URL="http://${APP_HOST}:${APP_PORT}"

ES_PID_FILE="$RUN_DIR/elasticsearch.pid"
APP_PID_FILE="$RUN_DIR/app.pid"
APP_LOG_FILE="$RUN_DIR/app.log"
ES_LOG_FILE="$RUN_DIR/elasticsearch-stdout.log"

mkdir -p "$RUN_DIR"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

is_listening() {
  local port="$1"
  lsof -iTCP:"$port" -sTCP:LISTEN -n -P >/dev/null 2>&1
}

wait_for_http() {
  local url="$1"
  local retries="${2:-30}"
  local sleep_seconds="${3:-1}"

  for _ in $(seq 1 "$retries"); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$sleep_seconds"
  done

  return 1
}

require_cmd curl
require_cmd lsof

if [ ! -x "$VENV_PYTHON" ]; then
  echo "Virtualenv not found at $VENV_PYTHON" >&2
  echo "Create it first with: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt" >&2
  exit 1
fi

if [ ! -d "$ES_DIR" ]; then
  echo "Elasticsearch directory not found at $ES_DIR" >&2
  echo "Extract the Elasticsearch tarball first or ask me to install it again." >&2
  exit 1
fi

if [ ! -f "$ROOT_DIR/.env" ]; then
  echo ".env not found in $ROOT_DIR" >&2
  echo "Create it first. The app expects local settings there." >&2
  exit 1
fi

if ! is_listening 9200; then
  echo "Starting Elasticsearch on 127.0.0.1:9200..."
  nohup env -u CLASSPATH -u JAVA_HOME \
    ES_JAVA_OPTS='-Xms512m -Xmx512m' \
    "$ES_DIR/bin/elasticsearch" \
    -E discovery.type=single-node \
    -E xpack.security.enabled=false \
    -E http.host=127.0.0.1 \
    -E transport.host=127.0.0.1 \
    -E path.data="$ROOT_DIR/elasticsearch-data" \
    -E path.logs="$ROOT_DIR/elasticsearch-logs" \
    >"$ES_LOG_FILE" 2>&1 &
  echo $! > "$ES_PID_FILE"

  if ! wait_for_http "$ES_URL" 60 1; then
    echo "Elasticsearch did not become ready in time." >&2
    echo "Check $ES_LOG_FILE and $ROOT_DIR/elasticsearch-logs/elasticsearch.log" >&2
    exit 1
  fi
else
  echo "Elasticsearch is already listening on 127.0.0.1:9200."
fi

if ! is_listening "$APP_PORT"; then
  echo "Starting app on ${APP_URL}..."
  nohup env PYTHONPYCACHEPREFIX=/tmp/pycache \
    "$VENV_PYTHON" -m uvicorn app:app --host "$APP_HOST" --port "$APP_PORT" \
    >"$APP_LOG_FILE" 2>&1 &
  echo $! > "$APP_PID_FILE"

  if ! wait_for_http "$APP_URL/ping" 30 1; then
    echo "App did not become ready in time." >&2
    echo "Check $APP_LOG_FILE" >&2
    exit 1
  fi
else
  echo "App is already listening on ${APP_URL}."
fi

echo
echo "Local stack is ready."
echo "App:    ${APP_URL}"
echo "Elastic:${ES_URL}"
echo "Status: ${APP_URL}/api/observability/status"
echo "Logs:   ${APP_URL}/api/logs?query=*&limit=5&minutes=15"
