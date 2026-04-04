# Deployment Guide

This project is easiest to host publicly as a Docker-based Hugging Face Space.

## Recommended Free Host

Hugging Face Spaces is the cleanest fit for this repo because:
- the app already ships with a Dockerfile
- the UI is self-contained in FastAPI
- Spaces currently offers a free `CPU Basic` tier for lightweight demos

## Option 1: Hugging Face Spaces

1. Push this repository to GitHub.
2. Create a new Space on Hugging Face.
3. Choose:
   - SDK: `Docker`
   - Visibility: your choice
4. Connect the Space to your GitHub repo or push the repo directly.
5. In the Space settings, add variables or secrets for anything sensitive.

Recommended secrets and variables:

```text
PORT=7860
ELASTICSEARCH_URL=https://your-public-elastic-endpoint
ELASTICSEARCH_API_KEY=your_api_key
ELASTICSEARCH_LOG_INDEX=logs-*
ELASTICSEARCH_TIMESTAMP_FIELD=@timestamp
ELASTICSEARCH_SERVICE_FIELD=service.name
ELASTICSEARCH_MESSAGE_FIELD=message
ELASTICSEARCH_LEVEL_FIELD=log.level
ELASTICSEARCH_VERIFY_TLS=true
DD_API_KEY=
DD_APP_KEY=
DD_SITE=datadoghq.com
```

Notes:
- A local Elasticsearch node on your laptop will not be reachable from Hugging Face.
- For a public deployment, point the app at a reachable hosted Elasticsearch cluster.
- If you do not provide Elastic or Datadog credentials, the dashboard still works using simulator fallback data.

## Option 2: Render / Railway / Fly.io

This app can also run on any platform that:
- supports Docker or Python web services
- exposes a public `PORT` environment variable
- allows outbound HTTP calls to your Elastic or Datadog backend

The included Dockerfile now respects the runtime `PORT` variable, which makes these platforms easier to use.

## Production Checklist

Before calling this production-ready, work through:

1. Replace local Elastic with a reachable managed or self-hosted cluster.
2. Add real auth for Elasticsearch or Datadog instead of unauthenticated localhost.
3. Set up persistent log ingestion so `/api/logs` and `/api/apm` are not seeded manually.
4. Add monitoring and alerting for the FastAPI service itself.
5. Add request logging and error tracking.
6. Lock environment variables in your host's secret store.
7. Review rate limits and timeout behavior for upstream observability APIs.
8. Add automated tests for:
   - `/api/logs`
   - `/api/apm`
   - `/api/observability/status`
   - dashboard rendering assumptions

## Local-to-Public Gap

The current local setup uses:
- local Elasticsearch on `127.0.0.1:9200`
- app on `127.0.0.1:7860`

That is perfect for development, but a hosted deployment needs:
- a public or host-network-reachable observability backend
- secrets configured in the hosting platform
- a deployment pipeline that rebuilds the Docker image after changes
