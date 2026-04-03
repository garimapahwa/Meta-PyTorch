#!/usr/bin/env python3
"""
FastAPI server for a distributed incident war-room OpenEnv.
Deployable to Hugging Face Spaces.
"""

import os
import json
import time
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel
import requests

from environment import make_env, DevOpsWarRoomEnv
from models import Action, ActionType, ServiceName, MetricType, Observation, Reward
from tasks import TASK_DEFINITIONS


app = FastAPI(
    title="Distributed Incident War Room OpenEnv",
    description="SRE debugging simulation environment for live distributed production incidents",
    version="1.0.0",
)

# Global environment instance (for HF Space stateful deployment)
current_env: Optional[DevOpsWarRoomEnv] = None
current_task_id: str = "easy_0"


def _get_source_env() -> Optional[DevOpsWarRoomEnv]:
    source_env = current_env
    if source_env is None:
        try:
            source_env = make_env(task_id=current_task_id, seed=0)
            source_env.reset()
        except Exception:
            source_env = None
    return source_env


def _site_base_url(site: str) -> str:
    site = (site or "datadoghq.com").strip()
    return f"https://api.{site}" if not site.startswith("api.") else f"https://{site}"


def _serialize_log_entry(log: Any) -> Dict[str, Any]:
    if hasattr(log, "model_dump"):
        payload = log.model_dump()
    elif isinstance(log, dict):
        payload = dict(log)
    else:
        payload = {
            "timestamp": getattr(log, "timestamp", None),
            "service": getattr(getattr(log, "service", None), "value", getattr(log, "service", None)),
            "level": getattr(log, "level", None),
            "message": getattr(log, "message", None),
            "trace_id": getattr(log, "trace_id", None),
            "is_relevant": getattr(log, "is_relevant", None),
        }

    service_value = payload.get("service")
    if hasattr(service_value, "value"):
        payload["service"] = service_value.value
    return payload


def _fetch_datadog_logs(query: str = "*", service: Optional[str] = None, limit: int = 25, minutes: int = 15) -> Dict[str, Any]:
    api_key = os.getenv("DD_API_KEY", "").strip()
    app_key = os.getenv("DD_APP_KEY", "").strip()
    site = os.getenv("DD_SITE", "datadoghq.com").strip()

    filters: List[str] = []
    if query:
        filters.append(f"({query})")
    if service:
        filters.append(f"service:{service}")
    search_query = " AND ".join(filters) if filters else "*"

    if not api_key or not app_key:
        fallback_logs: List[Dict[str, Any]] = []
        source_env = _get_source_env()

        if source_env is not None:
            matching_logs = [
                _serialize_log_entry(log)
                for log in source_env.all_logs
                if not service or str(getattr(log.service, "value", log.service)) == service
            ]
            fallback_logs = matching_logs[-limit:]

        return {
            "source": "local-fallback",
            "query": search_query,
            "logs": fallback_logs,
            "note": "DD_API_KEY or DD_APP_KEY not configured. Showing environment logs instead.",
        }

    payload = {
        "filter": {
            "query": search_query,
            "from": f"now-{max(1, minutes)}m",
            "to": "now",
        },
        "page": {"limit": max(1, min(limit, 100))},
    }

    response = requests.post(
        f"{_site_base_url(site)}/api/v2/logs/events/search",
        headers={
            "DD-API-KEY": api_key,
            "DD-APPLICATION-KEY": app_key,
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=12,
    )

    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Datadog logs API error: {response.text}")

    body = response.json()
    raw_logs = body.get("data") or body.get("logs") or []
    parsed_logs: List[Dict[str, Any]] = []

    for item in raw_logs:
        attributes = item.get("attributes", {}) if isinstance(item, dict) else {}
        parsed_logs.append(
            {
                "timestamp": item.get("id") or attributes.get("timestamp") or attributes.get("ingested_at"),
                "service": attributes.get("service") or attributes.get("service_name") or attributes.get("host"),
                "level": attributes.get("status") or attributes.get("level") or attributes.get("alert_type") or "info",
                "message": attributes.get("message") or attributes.get("title") or attributes.get("text") or json.dumps(attributes)[:240],
                "trace_id": attributes.get("trace_id") or attributes.get("dd.trace_id"),
                "is_relevant": True,
                "raw": item,
            }
        )

    return {
        "source": "datadog",
        "query": search_query,
        "logs": parsed_logs,
    }


def _fetch_datadog_metrics(metric_query: str = "avg:system.cpu.user{*}", service: Optional[str] = None, points: int = 24, minutes: int = 15) -> Dict[str, Any]:
    api_key = os.getenv("DD_API_KEY", "").strip()
    app_key = os.getenv("DD_APP_KEY", "").strip()
    site = os.getenv("DD_SITE", "datadoghq.com").strip()

    if not api_key or not app_key:
        source_env = _get_source_env()
        fallback_series: List[Dict[str, Any]] = []
        if source_env is not None:
            summary = source_env._compute_metrics_summary()
            for svc, values in summary.items():
                if service and svc != service:
                    continue
                fallback_series.append(
                    {
                        "service": svc,
                        "latency_ms": values.get("latency_ms", 0.0),
                        "error_rate": values.get("error_rate", 0.0),
                        "cpu_percent": values.get("cpu_percent", 0.0),
                        "memory_percent": values.get("memory_percent", 0.0),
                    }
                )

        return {
            "source": "local-fallback",
            "query": metric_query,
            "series": fallback_series,
            "note": "DD_API_KEY or DD_APP_KEY not configured. Showing simulator metrics instead.",
        }

    effective_query = metric_query or "avg:system.cpu.user{*}"
    if service and "{*}" in effective_query:
        effective_query = effective_query.replace("{*}", f"{{service:{service}}}")

    to_ts = int(time.time())
    from_ts = to_ts - max(60, minutes * 60)
    response = requests.get(
        f"{_site_base_url(site)}/api/v1/query",
        headers={
            "DD-API-KEY": api_key,
            "DD-APPLICATION-KEY": app_key,
        },
        params={"from": from_ts, "to": to_ts, "query": effective_query},
        timeout=12,
    )

    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Datadog metrics API error: {response.text}")

    body = response.json()
    series_items = body.get("series", [])
    parsed_series: List[Dict[str, Any]] = []
    for item in series_items:
        pointlist = item.get("pointlist", [])
        last_value = None
        for point in reversed(pointlist):
            if isinstance(point, list) and len(point) > 1 and point[1] is not None:
                last_value = point[1]
                break
        parsed_series.append(
            {
                "metric": item.get("metric"),
                "scope": item.get("scope"),
                "point_count": len(pointlist),
                "last_value": last_value,
                "display_name": item.get("display_name"),
            }
        )

    return {
        "source": "datadog",
        "query": effective_query,
        "series": parsed_series,
    }


def _fetch_datadog_apm(query: str = "service:*", service: Optional[str] = None, limit: int = 20, minutes: int = 15) -> Dict[str, Any]:
    api_key = os.getenv("DD_API_KEY", "").strip()
    app_key = os.getenv("DD_APP_KEY", "").strip()
    site = os.getenv("DD_SITE", "datadoghq.com").strip()

    filters: List[str] = []
    if query:
        filters.append(f"({query})")
    if service:
        filters.append(f"service:{service}")
    search_query = " AND ".join(filters) if filters else "service:*"

    if not api_key or not app_key:
        source_env = _get_source_env()
        fallback_traces: List[Dict[str, Any]] = []
        if source_env is not None:
            for action in source_env.actions_log[-max(1, min(limit, 100)):]:
                action_service = action.get("service") or "system"
                if service and action_service != service:
                    continue
                fallback_traces.append(
                    {
                        "timestamp": action.get("step"),
                        "service": action_service,
                        "operation": action.get("action_type"),
                        "resource": action.get("root_cause") or "incident-op",
                        "duration_ms": 40 + (action.get("step", 0) % 5) * 10,
                    }
                )

            if not fallback_traces:
                for log in source_env.all_logs[-max(1, min(limit, 100)):]:
                    log_service = str(getattr(log.service, "value", log.service))
                    if service and log_service != service:
                        continue
                    fallback_traces.append(
                        {
                            "timestamp": getattr(log, "timestamp", None),
                            "service": log_service,
                            "operation": "log_event",
                            "resource": getattr(log, "message", "event")[:80],
                            "duration_ms": 25,
                            "trace_id": getattr(log, "trace_id", None),
                        }
                    )

            if not fallback_traces and service:
                for log in source_env.all_logs[-max(1, min(limit, 100)):]:
                    fallback_traces.append(
                        {
                            "timestamp": getattr(log, "timestamp", None),
                            "service": str(getattr(log.service, "value", log.service)),
                            "operation": "log_event",
                            "resource": getattr(log, "message", "event")[:80],
                            "duration_ms": 25,
                            "trace_id": getattr(log, "trace_id", None),
                        }
                    )

        return {
            "source": "local-fallback",
            "query": search_query,
            "traces": fallback_traces,
            "note": "DD_API_KEY or DD_APP_KEY not configured. Showing simulator operations as pseudo traces.",
        }

    payload = {
        "filter": {
            "query": search_query,
            "from": f"now-{max(1, minutes)}m",
            "to": "now",
        },
        "sort": "timestamp",
        "page": {
            "limit": max(1, min(limit, 100)),
        },
    }
    response = requests.post(
        f"{_site_base_url(site)}/api/v2/apm/events/search",
        headers={
            "DD-API-KEY": api_key,
            "DD-APPLICATION-KEY": app_key,
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=12,
    )

    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Datadog APM API error: {response.text}")

    body = response.json()
    rows = body.get("data", [])
    traces: List[Dict[str, Any]] = []
    for row in rows:
        attrs = row.get("attributes", {}) if isinstance(row, dict) else {}
        traces.append(
            {
                "timestamp": attrs.get("timestamp") or attrs.get("start_timestamp"),
                "service": attrs.get("service") or attrs.get("service_name"),
                "operation": attrs.get("operation_name") or attrs.get("name"),
                "resource": attrs.get("resource_name") or attrs.get("resource"),
                "duration_ms": attrs.get("duration") or attrs.get("duration_ms"),
                "trace_id": attrs.get("trace_id") or attrs.get("trace.id"),
            }
        )

    return {
        "source": "datadog",
        "query": search_query,
        "traces": traces,
    }


def _dashboard_html() -> str:
        return """<!DOCTYPE html>
<html lang=\"en\">
<head>
    <meta charset=\"UTF-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />
    <title>Distributed Incident War Room</title>
    <style>
        :root {
            --bg: #07111f;
            --panel: rgba(255, 255, 255, 0.08);
            --panel-strong: rgba(10, 16, 28, 0.78);
            --border: rgba(255, 255, 255, 0.16);
            --text: #f2f6ff;
            --muted: rgba(242, 246, 255, 0.72);
            --accent: #72e4ff;
            --accent-2: #8b7dff;
            --danger: #ff7d8a;
            --good: #6effb1;
            --shadow: 0 30px 80px rgba(0, 0, 0, 0.35);
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            min-height: 100vh;
            font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            color: var(--text);
            background:
                radial-gradient(circle at top left, rgba(114, 228, 255, 0.18), transparent 28%),
                radial-gradient(circle at 80% 20%, rgba(139, 125, 255, 0.22), transparent 22%),
                linear-gradient(135deg, #050b15 0%, #081627 42%, #050b15 100%);
            overflow-x: hidden;
        }
        body::before {
            content: "";
            position: fixed;
            inset: 0;
            background-image: linear-gradient(rgba(255,255,255,0.03) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,0.03) 1px, transparent 1px);
            background-size: 48px 48px;
            mask-image: linear-gradient(180deg, rgba(0,0,0,0.85), transparent 100%);
            pointer-events: none;
        }
        .wrap {
            position: relative;
            z-index: 1;
            width: min(1440px, calc(100vw - 32px));
            margin: 0 auto;
            padding: 28px 0 40px;
        }
        .hero {
            display: grid;
            gap: 16px;
            grid-template-columns: 1.5fr 0.9fr;
            align-items: start;
            margin-bottom: 20px;
        }
        .title {
            padding: 28px;
            border: 1px solid var(--border);
            background: linear-gradient(180deg, rgba(255,255,255,0.14), rgba(255,255,255,0.05));
            backdrop-filter: blur(24px);
            border-radius: 28px;
            box-shadow: var(--shadow);
        }
        .eyebrow {
            text-transform: uppercase;
            letter-spacing: 0.18em;
            font-size: 12px;
            color: var(--accent);
            margin-bottom: 10px;
        }
        h1 {
            margin: 0;
            font-size: clamp(34px, 6vw, 68px);
            line-height: 0.95;
            max-width: 11ch;
        }
        .subtitle {
            max-width: 70ch;
            margin: 16px 0 0;
            color: var(--muted);
            font-size: 16px;
            line-height: 1.6;
        }
        .stats {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 16px;
        }
        .card {
            border: 1px solid var(--border);
            background: var(--panel);
            backdrop-filter: blur(24px);
            border-radius: 24px;
            box-shadow: var(--shadow);
            padding: 18px;
        }
        .metric-label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.12em; }
        .metric-value { font-size: 28px; font-weight: 700; margin-top: 10px; }
        .grid {
            display: grid;
            grid-template-columns: 1.15fr 0.85fr;
            gap: 16px;
            align-items: start;
        }
        .panel {
            border: 1px solid var(--border);
            background: linear-gradient(180deg, rgba(255,255,255,0.08), rgba(255,255,255,0.04));
            backdrop-filter: blur(26px);
            border-radius: 28px;
            box-shadow: var(--shadow);
            overflow: hidden;
        }
        .panel-head {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 18px 20px;
            border-bottom: 1px solid rgba(255,255,255,0.08);
            background: rgba(255,255,255,0.04);
        }
        .panel-head h2 { margin: 0; font-size: 16px; letter-spacing: 0.04em; text-transform: uppercase; }
        .panel-body { padding: 18px 20px 22px; }
        .controls {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 12px;
        }
        label { display: block; font-size: 12px; color: var(--muted); margin-bottom: 6px; }
        input, select, textarea, button {
            width: 100%;
            border: 1px solid rgba(255,255,255,0.14);
            background: rgba(5, 10, 18, 0.64);
            color: var(--text);
            border-radius: 14px;
            padding: 12px 14px;
            font: inherit;
            outline: none;
        }
        textarea { min-height: 110px; resize: vertical; }
        button {
            cursor: pointer;
            background: linear-gradient(135deg, rgba(114,228,255,0.92), rgba(139,125,255,0.92));
            color: #03101b;
            font-weight: 700;
            border: 0;
            transition: transform 0.18s ease, filter 0.18s ease;
        }
        button:hover { transform: translateY(-1px); filter: brightness(1.03); }
        .btn-row { display: flex; gap: 10px; flex-wrap: wrap; }
        .btn-row button { width: auto; min-width: 120px; }
        .chip {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 8px 12px;
            border-radius: 999px;
            background: rgba(255,255,255,0.08);
            border: 1px solid rgba(255,255,255,0.12);
            color: var(--muted);
            font-size: 13px;
        }
        .logs {
            max-height: 560px;
            overflow: auto;
            display: grid;
            gap: 12px;
        }
        .log {
            padding: 14px 16px;
            border-radius: 18px;
            background: rgba(5, 10, 18, 0.54);
            border: 1px solid rgba(255,255,255,0.1);
        }
        .log-top { display: flex; justify-content: space-between; gap: 12px; font-size: 12px; color: var(--muted); }
        .log-msg { margin-top: 8px; line-height: 1.5; }
        .level-ERROR, .level-critical { border-left: 3px solid var(--danger); }
        .level-WARN, .level-warning { border-left: 3px solid #ffcf6e; }
        .level-INFO, .level-info { border-left: 3px solid var(--accent); }
        pre {
            margin: 0;
            white-space: pre-wrap;
            word-break: break-word;
            background: rgba(5, 10, 18, 0.54);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 18px;
            padding: 16px;
            color: #d8e5ff;
            overflow: auto;
            max-height: 360px;
        }
        .row { display: grid; gap: 12px; }
        .muted { color: var(--muted); }
        .status-good { color: var(--good); }
        .status-bad { color: var(--danger); }
        .footer-note { margin-top: 16px; color: var(--muted); font-size: 13px; }
        @media (max-width: 1080px) {
            .hero, .grid { grid-template-columns: 1fr; }
            .stats, .controls { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <div class="wrap">
        <div class="hero">
            <section class="title">
                <div class="eyebrow">Distributed Incident War Room</div>
                <h1>Glass UI for live production debugging.</h1>
                <p class="subtitle">Operate the simulated microservices system directly from the dashboard, inspect live state, and pull logs either from Datadog or the local incident environment when credentials are not configured.</p>
            </section>
            <section class="stats" id="stats">
                <div class="card"><div class="metric-label">Environment</div><div class="metric-value" id="env-status">Loading</div></div>
                <div class="card"><div class="metric-label">Datadog</div><div class="metric-value" id="dd-status">Loading</div></div>
                <div class="card"><div class="metric-label">Current Step</div><div class="metric-value" id="step-count">0</div></div>
                <div class="card"><div class="metric-label">Damage Score</div><div class="metric-value" id="damage-score">0.00</div></div>
            </section>
        </div>

        <div class="grid">
            <section class="panel">
                <div class="panel-head">
                    <h2>Controls</h2>
                    <div class="chip" id="task-chip">Task: easy_0</div>
                </div>
                <div class="panel-body row">
                    <div class="controls">
                        <div>
                            <label for="taskId">Task</label>
                            <select id="taskId"></select>
                        </div>
                        <div>
                            <label for="seed">Seed</label>
                            <input id="seed" type="number" value="0" />
                        </div>
                        <div>
                            <label for="actionType">Action</label>
                            <select id="actionType"></select>
                        </div>
                        <div>
                            <label for="service">Service</label>
                            <select id="service">
                                <option value="">None</option>
                                <option value="auth">auth</option>
                                <option value="payments">payments</option>
                                <option value="db">db</option>
                                <option value="cache">cache</option>
                                <option value="api_gateway">api_gateway</option>
                            </select>
                        </div>
                        <div>
                            <label for="metric">Metric</label>
                            <select id="metric">
                                <option value="">None</option>
                                <option value="latency">latency</option>
                                <option value="error_rate">error_rate</option>
                                <option value="cpu">cpu</option>
                                <option value="memory">memory</option>
                                <option value="connection_count">connection_count</option>
                                <option value="queue_depth">queue_depth</option>
                            </select>
                        </div>
                        <div>
                            <label for="traceId">Trace ID</label>
                            <input id="traceId" placeholder="trace_1234" />
                        </div>
                        <div>
                            <label for="incidentId">Incident ID</label>
                            <input id="incidentId" placeholder="db_connection_leak" />
                        </div>
                        <div>
                            <label for="rootCause">Root Cause</label>
                            <input id="rootCause" placeholder="bad_payment_deployment" />
                        </div>
                        <div>
                            <label for="replicas">Replicas</label>
                            <input id="replicas" type="number" min="1" max="10" placeholder="5" />
                        </div>
                        <div>
                            <label for="version">Version</label>
                            <input id="version" placeholder="v1.2.0" />
                        </div>
                    </div>
                    <div class="btn-row">
                        <button id="resetBtn">Reset Environment</button>
                        <button id="stepBtn">Run Step</button>
                        <button id="refreshBtn">Refresh State</button>
                    </div>
                    <div class="footer-note" id="action-note">Choose an action, then step the environment or query logs.</div>
                </div>
            </section>

            <section class="panel">
                <div class="panel-head">
                    <h2>Log Stream</h2>
                    <div class="chip" id="log-source">Source: loading</div>
                </div>
                <div class="panel-body row">
                    <div class="controls">
                        <div>
                            <label for="logQuery">Datadog Query</label>
                            <input id="logQuery" value="*" />
                        </div>
                        <div>
                            <label for="logService">Service Filter</label>
                            <select id="logService">
                                <option value="">Any</option>
                                <option value="auth">auth</option>
                                <option value="payments">payments</option>
                                <option value="db">db</option>
                                <option value="cache">cache</option>
                                <option value="api_gateway">api_gateway</option>
                            </select>
                        </div>
                        <div>
                            <label for="logLimit">Limit</label>
                            <input id="logLimit" type="number" min="1" max="100" value="25" />
                        </div>
                        <div>
                            <label for="logMinutes">Window (minutes)</label>
                            <input id="logMinutes" type="number" min="1" max="120" value="15" />
                        </div>
                    </div>
                    <div class="btn-row">
                        <button id="loadLogsBtn">Load Logs</button>
                    </div>
                    <div class="logs" id="logs"></div>
                </div>
            </section>

            <section class="panel">
                <div class="panel-head">
                    <h2>Metrics</h2>
                    <div class="chip" id="metrics-source">Source: loading</div>
                </div>
                <div class="panel-body row">
                    <div class="controls">
                        <div>
                            <label for="metricQuery">Metric Query</label>
                            <input id="metricQuery" value="avg:system.cpu.user{*}" />
                        </div>
                        <div>
                            <label for="metricService">Service Filter</label>
                            <select id="metricService">
                                <option value="">Any</option>
                                <option value="auth">auth</option>
                                <option value="payments">payments</option>
                                <option value="db">db</option>
                                <option value="cache">cache</option>
                                <option value="api_gateway">api_gateway</option>
                            </select>
                        </div>
                        <div>
                            <label for="metricPoints">Points</label>
                            <input id="metricPoints" type="number" min="1" max="200" value="24" />
                        </div>
                        <div>
                            <label for="metricMinutes">Window (minutes)</label>
                            <input id="metricMinutes" type="number" min="1" max="120" value="15" />
                        </div>
                    </div>
                    <div class="btn-row">
                        <button id="loadMetricsBtn">Load Metrics</button>
                    </div>
                    <div class="logs" id="metricsList"></div>
                </div>
            </section>

            <section class="panel">
                <div class="panel-head">
                    <h2>APM Traces</h2>
                    <div class="chip" id="apm-source">Source: loading</div>
                </div>
                <div class="panel-body row">
                    <div class="controls">
                        <div>
                            <label for="apmQuery">APM Query</label>
                            <input id="apmQuery" value="service:*" />
                        </div>
                        <div>
                            <label for="apmService">Service Filter</label>
                            <select id="apmService">
                                <option value="">Any</option>
                                <option value="auth">auth</option>
                                <option value="payments">payments</option>
                                <option value="db">db</option>
                                <option value="cache">cache</option>
                                <option value="api_gateway">api_gateway</option>
                            </select>
                        </div>
                        <div>
                            <label for="apmLimit">Limit</label>
                            <input id="apmLimit" type="number" min="1" max="100" value="20" />
                        </div>
                        <div>
                            <label for="apmMinutes">Window (minutes)</label>
                            <input id="apmMinutes" type="number" min="1" max="120" value="15" />
                        </div>
                    </div>
                    <div class="btn-row">
                        <button id="loadApmBtn">Load Traces</button>
                    </div>
                    <div class="logs" id="tracesList"></div>
                </div>
            </section>

            <section class="panel">
                <div class="panel-head">
                    <h2>Current State</h2>
                    <div class="chip" id="dd-note">Datadog: inactive</div>
                </div>
                <div class="panel-body">
                    <pre id="stateJson">Loading state...</pre>
                </div>
            </section>
        </div>
    </div>

    <script>
        const ACTIONS = [
            "query_logs",
            "query_metrics",
            "trace_request",
            "restart_service",
            "rollback_deployment",
            "scale_service",
            "prioritize_incident",
            "resolve_incident",
        ];

        const el = (id) => document.getElementById(id);

        function setText(id, value) {
            el(id).textContent = value;
        }

        function updateDatadogStatus(source) {
            if (source === "datadog") {
                setText("dd-status", "Connected");
            } else {
                setText("dd-status", "Fallback");
            }
        }

        function renderLogs(logs) {
            const container = el("logs");
            container.innerHTML = "";
            if (!logs || logs.length === 0) {
                container.innerHTML = '<div class="chip">No logs returned.</div>';
                return;
            }
            logs.forEach((log) => {
                const node = document.createElement("div");
                node.className = `log level-${String(log.level || "info").toUpperCase()}`;
                node.innerHTML = `
                    <div class="log-top">
                        <span>${log.timestamp ?? "-"}</span>
                        <span>${log.service ?? "unknown"}</span>
                        <span>${log.level ?? "info"}</span>
                    </div>
                    <div class="log-msg">${escapeHtml(String(log.message ?? ""))}</div>
                    ${log.trace_id ? `<div class="log-top" style="margin-top:8px;"><span>trace_id</span><span>${escapeHtml(String(log.trace_id))}</span></div>` : ""}
                `;
                container.appendChild(node);
            });
        }

        function renderMetrics(series) {
            const container = el("metricsList");
            container.innerHTML = "";
            if (!series || series.length === 0) {
                container.innerHTML = '<div class="chip">No metrics returned.</div>';
                return;
            }
            series.forEach((item) => {
                const node = document.createElement("div");
                node.className = "log level-INFO";
                const title = item.metric || item.service || "metric";
                const summary = item.last_value != null
                    ? `last_value: ${Number(item.last_value).toFixed(4)} | points: ${item.point_count ?? 0}`
                    : `latency: ${Number(item.latency_ms ?? 0).toFixed(1)} | error_rate: ${Number(item.error_rate ?? 0).toFixed(3)} | cpu: ${Number(item.cpu_percent ?? 0).toFixed(1)} | mem: ${Number(item.memory_percent ?? 0).toFixed(1)}`;
                node.innerHTML = `
                    <div class="log-top">
                        <span>${escapeHtml(String(title))}</span>
                        <span>${escapeHtml(String(item.scope || item.service || ""))}</span>
                    </div>
                    <div class="log-msg">${escapeHtml(summary)}</div>
                `;
                container.appendChild(node);
            });
        }

        function renderTraces(traces) {
            const container = el("tracesList");
            container.innerHTML = "";
            if (!traces || traces.length === 0) {
                container.innerHTML = '<div class="chip">No traces returned.</div>';
                return;
            }
            traces.forEach((trace) => {
                const node = document.createElement("div");
                node.className = "log level-WARN";
                node.innerHTML = `
                    <div class="log-top">
                        <span>${escapeHtml(String(trace.timestamp ?? "-"))}</span>
                        <span>${escapeHtml(String(trace.service ?? "unknown"))}</span>
                    </div>
                    <div class="log-msg">${escapeHtml(String(trace.operation || "operation"))} -> ${escapeHtml(String(trace.resource || "resource"))}</div>
                    <div class="log-top" style="margin-top:8px;">
                        <span>duration</span>
                        <span>${escapeHtml(String(trace.duration_ms ?? "n/a"))} ms</span>
                    </div>
                `;
                container.appendChild(node);
            });
        }

        function escapeHtml(text) {
            return text
                .replaceAll("&", "&amp;")
                .replaceAll("<", "&lt;")
                .replaceAll(">", "&gt;")
                .replaceAll('"', "&quot;")
                .replaceAll("'", "&#039;");
        }

        async function refreshTasks() {
            const res = await fetch("/tasks");
            const data = await res.json();
            const taskSelect = el("taskId");
            taskSelect.innerHTML = "";
            data.tasks.forEach((task) => {
                const option = document.createElement("option");
                option.value = task;
                option.textContent = task;
                taskSelect.appendChild(option);
            });
            taskSelect.value = "easy_0";
        }

        function populateActions() {
            const select = el("actionType");
            select.innerHTML = "";
            ACTIONS.forEach((action) => {
                const option = document.createElement("option");
                option.value = action;
                option.textContent = action;
                select.appendChild(option);
            });
        }

        async function loadState() {
            try {
                const res = await fetch("/state");
                const data = await res.json();
                el("stateJson").textContent = JSON.stringify(data, null, 2);
                setText("step-count", String(data.current_step ?? 0));
                setText("damage-score", Number(data.damage_score ?? 0).toFixed(2));
                setText("env-status", data.done ? "Resolved" : "Active");
                setText("task-chip", `Task: ${el("taskId").value || "easy_0"}`);
            } catch (error) {
                el("stateJson").textContent = "Environment is idle. Select a task and click Reset Environment to initialize the incident.";
                setText("env-status", "Idle");
                setText("step-count", "0");
                setText("damage-score", "0.00");
            }
        }

        async function loadLogs() {
            const query = el("logQuery").value || "*";
            const service = el("logService").value || "";
            const limit = Number(el("logLimit").value || 25);
            const minutes = Number(el("logMinutes").value || 15);
            const params = new URLSearchParams({ query, limit: String(limit), minutes: String(minutes) });
            if (service) params.set("service", service);

            const res = await fetch(`/api/logs?${params.toString()}`);
            const data = await res.json();
            setText("log-source", `Source: ${data.source || "unknown"}`);
            setText("dd-note", data.note || `Query: ${data.query || query}`);
            updateDatadogStatus(data.source || "local-fallback");
            renderLogs(data.logs || []);
        }

        async function loadMetrics() {
            const query = el("metricQuery").value || "avg:system.cpu.user{*}";
            const service = el("metricService").value || "";
            const points = Number(el("metricPoints").value || 24);
            const minutes = Number(el("metricMinutes").value || 15);
            const params = new URLSearchParams({ query, points: String(points), minutes: String(minutes) });
            if (service) params.set("service", service);

            const res = await fetch(`/api/metrics?${params.toString()}`);
            const data = await res.json();
            setText("metrics-source", `Source: ${data.source || "unknown"}`);
            updateDatadogStatus(data.source || "local-fallback");
            renderMetrics(data.series || []);
        }

        async function loadApm() {
            const query = el("apmQuery").value || "service:*";
            const service = el("apmService").value || "";
            const limit = Number(el("apmLimit").value || 20);
            const minutes = Number(el("apmMinutes").value || 15);
            const params = new URLSearchParams({ query, limit: String(limit), minutes: String(minutes) });
            if (service) params.set("service", service);

            const res = await fetch(`/api/apm?${params.toString()}`);
            const data = await res.json();
            setText("apm-source", `Source: ${data.source || "unknown"}`);
            updateDatadogStatus(data.source || "local-fallback");
            renderTraces(data.traces || []);
        }

        async function resetEnvironment() {
            const payload = {
                task_id: el("taskId").value,
                seed: Number(el("seed").value || 0),
            };
            const res = await fetch("/reset", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            if (!res.ok) {
                const err = await res.text();
                alert(err);
                return;
            }
            await loadState();
            await loadLogs();
            await loadMetrics();
            await loadApm();
        }

        async function runStep() {
            const payload = {
                action_type: el("actionType").value,
                service: el("service").value || null,
                metric: el("metric").value || null,
                trace_id: el("traceId").value || null,
                replicas: el("replicas").value ? Number(el("replicas").value) : null,
                version: el("version").value || null,
                incident_id: el("incidentId").value || null,
                root_cause: el("rootCause").value || null,
            };
            const res = await fetch("/step", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            if (!res.ok) {
                const err = await res.text();
                alert(err);
                return;
            }
            const data = await res.json();
            el("action-note").textContent = `Last action: ${payload.action_type} | reward: ${Number(data.reward?.value ?? 0).toFixed(2)} | done: ${data.done}`;
            await loadState();
        }

        el("resetBtn").addEventListener("click", resetEnvironment);
        el("stepBtn").addEventListener("click", runStep);
        el("refreshBtn").addEventListener("click", async () => { await loadState(); await loadLogs(); await loadMetrics(); await loadApm(); });
        el("loadLogsBtn").addEventListener("click", loadLogs);
        el("loadMetricsBtn").addEventListener("click", loadMetrics);
        el("loadApmBtn").addEventListener("click", loadApm);

        (async function init() {
            populateActions();
            await refreshTasks();
            await loadState();
            await loadLogs();
            await loadMetrics();
            await loadApm();
            setInterval(loadState, 8000);
        })();
    </script>
</body>
</html>"""


class ResetRequest(BaseModel):
    """Reset endpoint request"""
    task_id: Optional[str] = "easy_0"
    seed: Optional[int] = 0


class StepRequest(BaseModel):
    """Step endpoint request"""
    action_type: str
    service: Optional[str] = None
    metric: Optional[str] = None
    trace_id: Optional[str] = None
    replicas: Optional[int] = None
    version: Optional[str] = None
    incident_id: Optional[str] = None
    root_cause: Optional[str] = None


class StateResponse(BaseModel):
    """State endpoint response"""
    observation: Dict[str, Any]
    done: bool
    max_steps: int
    current_step: int
    damage_score: float


@app.get("/", response_class=HTMLResponse)
async def root():
    """Glass dashboard for the war room."""
    return HTMLResponse(content=_dashboard_html())


@app.get("/api/info")
async def api_info():
    """Service metadata endpoint."""
    return {
        "service": "Distributed Incident War Room OpenEnv",
        "status": "online",
        "version": "1.0.0",
    }


@app.get("/ping")
async def ping():
    """Health check endpoint (required for validation)"""
    return JSONResponse(
        status_code=200,
        content={"status": "ok", "message": "Service is healthy"}
    )


@app.post("/reset")
async def reset(request: ResetRequest = None):
    """
    Reset environment and return initial observation.
    OpenEnv required endpoint.
    """
    global current_env, current_task_id
    
    try:
        task_id = request.task_id if request else "easy_0"
        seed = request.seed if request else 0
        current_task_id = task_id

        # Validate task exists
        if task_id not in TASK_DEFINITIONS:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown task: {task_id}. Available: {list(TASK_DEFINITIONS.keys())}"
            )

        # Create environment
        current_env = make_env(task_id=task_id, seed=seed)
        obs = current_env.reset()

        return {
            "status": "reset",
            "observation": obs.dict(),
            "task_id": task_id,
            "max_steps": current_env.max_steps,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/step")
async def step(request: StepRequest):
    """
    Execute action in environment.
    OpenEnv required endpoint.
    """
    global current_env
    
    if current_env is None:
        raise HTTPException(status_code=400, detail="Environment not initialized. Call /reset first.")

    try:
        # Parse action
        action_type_str = request.action_type.upper()
        action_type = ActionType[action_type_str]

        service = None
        if request.service:
            service = ServiceName[request.service.upper()]

        metric = None
        if request.metric:
            metric = MetricType[request.metric.upper()]

        action = Action(
            action_type=action_type,
            service=service,
            metric=metric,
            trace_id=request.trace_id,
            replicas=request.replicas,
            version=request.version,
            incident_id=request.incident_id,
            root_cause=request.root_cause,
        )

        # Execute step
        obs, reward, done, info = current_env.step(action)

        return {
            "observation": obs.dict(),
            "reward": reward.dict(),
            "done": done,
            "info": info,
            "step": current_env.current_step,
        }

    except KeyError as e:
        raise HTTPException(status_code=400, detail=f"Invalid parameter: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/state")
async def state() -> StateResponse:
    """
    Get current state (partially observable).
    OpenEnv required endpoint.
    """
    global current_env
    
    if current_env is None:
        raise HTTPException(status_code=400, detail="Environment not initialized. Call /reset first.")

    try:
        obs = current_env._get_observation()
        
        return {
            "observation": obs.dict(),
            "done": current_env._check_done(),
            "max_steps": current_env.max_steps,
            "current_step": current_env.current_step,
            "damage_score": float(current_env.damage_score),
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/grade")
async def get_grade():
    """Get grade for completed episode"""
    global current_env
    
    if current_env is None:
        raise HTTPException(status_code=400, detail="Environment not initialized.")

    try:
        grade = current_env.get_grade()
        return {
            "score": float(grade["score"]),
            "correctness": float(grade["correctness"]),
            "efficiency": float(grade["efficiency"]),
            "damage": float(grade["damage"]),
            "details": grade["details"],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/tasks")
async def list_tasks():
    """List available tasks"""
    return {
        "tasks": list(TASK_DEFINITIONS.keys()),
        "difficulties": ["easy", "medium", "hard"],
    }


@app.get("/tasks/{task_id}")
async def get_task(task_id: str):
    """Get task details"""
    if task_id not in TASK_DEFINITIONS:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")
    
    task = TASK_DEFINITIONS[task_id]
    return {
        "task_id": task["task_id"],
        "difficulty": task["difficulty"],
        "name": task["name"],
        "description": task["description"],
        "max_steps": task["max_steps"],
        "num_incidents": task["num_incidents"],
    }


@app.get("/health")
async def health():
    """Health check"""
    return {
        "status": "healthy",
        "environment_ready": current_env is not None,
    }


@app.get("/api/logs")
async def api_logs(query: str = "*", service: Optional[str] = None, limit: int = 25, minutes: int = 15):
    """Fetch logs from Datadog or fall back to the local simulated environment."""
    try:
        return _fetch_datadog_logs(query=query, service=service, limit=limit, minutes=minutes)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/metrics")
async def api_metrics(query: str = "avg:system.cpu.user{*}", service: Optional[str] = None, points: int = 24, minutes: int = 15):
    """Fetch metrics from Datadog or fall back to simulator service metrics."""
    try:
        return _fetch_datadog_metrics(metric_query=query, service=service, points=points, minutes=minutes)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/apm")
async def api_apm(query: str = "service:*", service: Optional[str] = None, limit: int = 20, minutes: int = 15):
    """Fetch Datadog APM traces or fall back to simulator operation traces."""
    try:
        return _fetch_datadog_apm(query=query, service=service, limit=limit, minutes=minutes)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 7860))
    uvicorn.run(app, host="0.0.0.0", port=port)
