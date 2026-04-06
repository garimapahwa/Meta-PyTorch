#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_URL="${APP_URL:-http://127.0.0.1:7860}"

cat <<EOF
Side-by-side demo plan

Pane 1: start the dashboard app
  cd "$ROOT_DIR"
  .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 7860

Pane 2: watch the app read replayed logs
  watch -n 2 "curl -sS '$APP_URL/api/logs?query=incident_id:INC-DEMO-*&service=meta-pytorch-demo&limit=10&minutes=60'"

Pane 3: seed project-specific failures into Elasticsearch or local fallback
  cd "$ROOT_DIR"
  .venv/bin/python scripts/seed_project_errors_to_elastic.py --scenario all

What to inspect in the app
  1. Open $APP_URL
  2. Go to the Logs tab
  3. Set service to meta-pytorch-demo
  4. Query incident_id:INC-DEMO-*
  5. Pivot on trace IDs or incident IDs to diagnose each failure chain

Scenarios available
  - startup_failures
  - port_conflict
  - docker_gap
  - healthy_recovery

Tip
  If ELASTICSEARCH_URL is not configured, the seed script writes local replay logs to .run/local-demo-logs.jsonl and the dashboard still surfaces them through /api/logs.
  If you prefer two panes only, keep the app open in the browser and run the watch command plus the seed command in the terminal.
EOF
