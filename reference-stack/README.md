# Broken Reference Stack

This folder scaffolds a small "broken project" that feeds structured logs into Elasticsearch so the main dashboard can inspect them through `/api/logs`.

## What You Get

- `orders-service`: a deliberately flaky FastAPI service
- `filebeat`: ships JSON log lines from the service into Elasticsearch
- `elasticsearch`: local log store for the reference project
- `dashboard`: this repo's existing war-room UI, configured to read those Elastic logs

## Failure Modes

The service starts in `db_connection_leak` mode and emits:

- repeated error logs
- trace IDs
- incident IDs
- fix hints

You can switch modes or "fix" the service through admin endpoints:

- `POST /admin/mode/db_connection_leak`
- `POST /admin/mode/payment_timeout`
- `POST /admin/mode/noisy_cache`
- `POST /admin/mode/healthy`

## Run

From the repo root:

```bash
docker compose -f reference-stack/docker-compose.yml up --build
```

Then open:

- Dashboard: `http://localhost:7860`
- Broken service: `http://localhost:8081`
- Elasticsearch: `http://localhost:9200`

## Suggested Dashboard Queries

Use these inside the dashboard logs tab:

- query: `*`
- service: `orders-service`
- query: `incident_id:INC-5001`
- query: `trace_id:*`
- query: `message:*timeout* OR message:*connection*`

## Fix Workflow

1. Start in a broken mode and generate traffic:
   `curl http://localhost:8081/checkout`
2. Inspect logs in the dashboard for `orders-service`.
3. Trigger a fix:
   `curl -X POST http://localhost:8081/admin/mode/healthy`
4. Generate traffic again and confirm error logs stop.

## Notes

- The dashboard is configured to use index pattern `broken-ref-logs-*`.
- The service field is stored as `service`, which matches the dashboard default.
- This is meant to be a reference harness. Once we like the shape, we can expand it into multiple services and real remediation flows.
