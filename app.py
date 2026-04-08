#!/usr/bin/env python3
"""
FastAPI server for a distributed incident war-room OpenEnv.
Deployable to Hugging Face Spaces.
"""

import os
import json
import time
import base64
import fnmatch
from datetime import datetime
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel
import requests
from dotenv import load_dotenv

from environment import make_env, DevOpsWarRoomEnv
from models import Action, ActionType, ServiceName, MetricType, Observation, Reward
from scripts.seed_project_errors_to_elastic import docs_for_scenario
from tasks import TASK_DEFINITIONS

load_dotenv()


app = FastAPI(
    title="Distributed Incident War Room OpenEnv",
    description="SRE debugging simulation environment for live distributed production incidents",
    version="1.0.0",
)

# Global environment instance (for HF Space stateful deployment)
current_env: Optional[DevOpsWarRoomEnv] = None
current_task_id: str = "easy_0"
LOCAL_DEMO_LOG_FILE = os.getenv(
    "LOCAL_DEMO_LOG_FILE",
    os.path.join(os.path.dirname(__file__), ".run", "local-demo-logs.jsonl"),
)


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


def _get_datadog_settings() -> Dict[str, Any]:
    indexes = [
        item.strip()
        for item in os.getenv("DD_LOG_INDEXES", "").split(",")
        if item.strip()
    ]
    return {
        "api_key": os.getenv("DD_API_KEY", "").strip(),
        "app_key": os.getenv("DD_APP_KEY", "").strip(),
        "site": os.getenv("DD_SITE", "datadoghq.com").strip(),
        "indexes": indexes,
    }


def _datadog_enabled(settings: Optional[Dict[str, Any]] = None) -> bool:
    settings = settings or _get_datadog_settings()
    return bool(settings.get("api_key") and settings.get("app_key"))


def _get_elasticsearch_settings() -> Dict[str, Any]:
    return {
        "url": os.getenv("ELASTICSEARCH_URL", "").strip().rstrip("/"),
        "api_key": os.getenv("ELASTICSEARCH_API_KEY", "").strip(),
        "username": os.getenv("ELASTICSEARCH_USERNAME", "").strip(),
        "password": os.getenv("ELASTICSEARCH_PASSWORD", "").strip(),
        "index": os.getenv("ELASTICSEARCH_LOG_INDEX", "logs-*").strip() or "logs-*",
        "timestamp_field": os.getenv("ELASTICSEARCH_TIMESTAMP_FIELD", "@timestamp").strip() or "@timestamp",
        "service_field": os.getenv("ELASTICSEARCH_SERVICE_FIELD", "service").strip() or "service",
        "message_field": os.getenv("ELASTICSEARCH_MESSAGE_FIELD", "message").strip() or "message",
        "level_field": os.getenv("ELASTICSEARCH_LEVEL_FIELD", "log.level").strip() or "log.level",
        "verify_tls": os.getenv("ELASTICSEARCH_VERIFY_TLS", "true").strip().lower() not in {"0", "false", "no"},
    }


def _elasticsearch_enabled(settings: Optional[Dict[str, Any]] = None) -> bool:
    settings = settings or _get_elasticsearch_settings()
    return bool(settings.get("url"))


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _elastic_auth_headers(settings: Dict[str, Any]) -> Dict[str, str]:
    headers: Dict[str, str] = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if settings.get("api_key"):
        headers["Authorization"] = f"ApiKey {settings['api_key']}"
    elif settings.get("username") and settings.get("password"):
        token = base64.b64encode(f"{settings['username']}:{settings['password']}".encode("utf-8")).decode("utf-8")
        headers["Authorization"] = f"Basic {token}"
    return headers


def _build_log_search_payload(search_query: str, limit: int, minutes: int, indexes: Optional[List[str]] = None) -> Dict[str, Any]:
    payload = {
        "filter": {
            "query": search_query,
            "from": f"now-{max(1, minutes)}m",
            "to": "now",
        },
        "sort": "timestamp",
        "page": {"limit": max(1, min(limit, 100))},
    }
    if indexes:
        payload["filter"]["indexes"] = indexes
    return payload


def _build_datadog_search_query(query: str, service: Optional[str], default: str) -> str:
    normalized_query = (query or "").strip()
    ignore_queries = {"*", "*:*", "service:*", "service.name:*"}
    filters: List[str] = []
    if normalized_query and normalized_query not in ignore_queries:
        filters.append(f"({normalized_query})")
    if service:
        filters.append(f"service:{service}")
    return " AND ".join(filters) if filters else default


def _parse_timestamp(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return 0.0
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(normalized).timestamp()
        except ValueError:
            try:
                return time.mktime(time.strptime(normalized[:19], "%Y-%m-%dT%H:%M:%S"))
            except ValueError:
                return 0.0
    return 0.0


def _field_candidates(payload: Dict[str, Any], field: str) -> List[Any]:
    mapping = {
        "service": ["service", "service.name"],
        "service.name": ["service.name", "service"],
        "message": ["message", "event.original"],
        "log.level": ["log.level", "level"],
        "level": ["level", "log.level"],
        "trace.id": ["trace.id", "trace_id"],
        "trace_id": ["trace_id", "trace.id"],
        "@timestamp": ["@timestamp", "timestamp"],
        "timestamp": ["timestamp", "@timestamp"],
    }
    values: List[Any] = []
    for key in mapping.get(field, [field]):
        if key in payload:
            values.append(payload[key])
    return values


def _matches_local_query_term(payload: Dict[str, Any], term: str) -> bool:
    normalized = term.strip().strip("()")
    if not normalized or normalized in {"*", "*:*", "service:*", "service.name:*"}:
        return True

    if ":" in normalized:
        field, pattern = normalized.split(":", 1)
        field = field.strip()
        pattern = pattern.strip().strip('"').strip("'")
        values = _field_candidates(payload, field)
        if pattern == "*":
            return any(value not in (None, "") for value in values)
        lowered_pattern = pattern.lower()
        for value in values:
            if value in (None, ""):
                continue
            candidate = str(value)
            candidate_lower = candidate.lower()
            if "*" in lowered_pattern or "?" in lowered_pattern:
                if fnmatch.fnmatchcase(candidate_lower, lowered_pattern):
                    return True
            elif lowered_pattern == candidate_lower or lowered_pattern in candidate_lower:
                return True
        return False

    haystack = json.dumps(payload, default=str).lower()
    return normalized.lower() in haystack


def _matches_local_query(payload: Dict[str, Any], query: str) -> bool:
    normalized = (query or "*").strip()
    if not normalized or normalized in {"*", "*:*", "service:*", "service.name:*"}:
        return True
    if " OR " in normalized:
        return any(_matches_local_query(payload, part) for part in normalized.split(" OR "))
    if " AND " in normalized:
        return all(_matches_local_query(payload, part) for part in normalized.split(" AND "))
    return _matches_local_query_term(payload, normalized)


def _load_local_demo_logs(query: str, service: Optional[str], limit: int, minutes: int) -> List[Dict[str, Any]]:
    if not os.path.exists(LOCAL_DEMO_LOG_FILE):
        return []

    min_timestamp = time.time() - max(1, minutes) * 60
    logs: List[Dict[str, Any]] = []

    try:
        with open(LOCAL_DEMO_LOG_FILE, "r", encoding="utf-8") as handle:
            for line in handle:
                raw_line = line.strip()
                if not raw_line:
                    continue
                try:
                    payload = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                if service:
                    service_value = str(
                        _field_candidates(payload, "service")[0]
                    ) if _field_candidates(payload, "service") else ""
                    if service_value != service:
                        continue
                if _parse_timestamp(payload.get("@timestamp")) < min_timestamp:
                    continue
                if not _matches_local_query(payload, query):
                    continue

                logs.append(
                    {
                        "timestamp": payload.get("@timestamp") or payload.get("timestamp"),
                        "service": payload.get("service") or payload.get("service.name"),
                        "level": payload.get("log.level") or payload.get("level") or "info",
                        "message": payload.get("message") or json.dumps(payload)[:240],
                        "trace_id": payload.get("trace.id") or payload.get("trace_id"),
                        "is_relevant": True,
                        "raw": payload,
                    }
                )
    except OSError:
        return []

    logs.sort(key=lambda item: _parse_timestamp(item.get("timestamp")), reverse=True)
    return logs[: max(1, min(limit, 100))]


def _has_local_demo_logs() -> bool:
    if not os.path.exists(LOCAL_DEMO_LOG_FILE):
        return False
    try:
        return os.path.getsize(LOCAL_DEMO_LOG_FILE) > 0
    except OSError:
        return False


def _pick_first(payload: Dict[str, Any], paths: List[str], default: Any = None) -> Any:
    for path in paths:
        if isinstance(payload, dict) and path in payload and payload[path] not in (None, ""):
            return payload[path]
        cursor: Any = payload
        found = True
        for key in path.split("."):
            if isinstance(cursor, dict) and key in cursor:
                cursor = cursor[key]
            else:
                found = False
                break
        if found and cursor not in (None, ""):
            return cursor
    return default


def _build_elasticsearch_log_payload(
    query: str,
    service: Optional[str],
    limit: int,
    minutes: int,
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    timestamp_field = settings["timestamp_field"]
    service_field = settings["service_field"]
    filters: List[Dict[str, Any]] = [
        {
            "range": {
                timestamp_field: {
                    "gte": f"now-{max(1, minutes)}m",
                    "lte": "now",
                }
            }
        }
    ]
    if service:
        filters.append(
            {
                "bool": {
                    "should": [
                        {"term": {service_field: service}},
                        {"term": {f"{service_field}.keyword": service}},
                        {"term": {"service.name": service}},
                        {"term": {"service.name.keyword": service}},
                    ],
                    "minimum_should_match": 1,
                }
            }
        )

    must: List[Dict[str, Any]] = []
    normalized_query = (query or "").strip()
    if normalized_query and normalized_query not in {"*", "*:*", "service:*", "service.name:*"}:
        must.append(
            {
                "query_string": {
                    "query": normalized_query,
                    "default_operator": "AND",
                }
            }
        )

    return {
        "size": max(1, min(limit, 100)),
        "sort": [{timestamp_field: {"order": "desc", "unmapped_type": "date"}}],
        "query": {
            "bool": {
                "must": must,
                "filter": filters,
            }
        },
    }


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
    settings = _get_datadog_settings()
    api_key = settings["api_key"]
    app_key = settings["app_key"]
    site = settings["site"]

    filters: List[str] = []
    search_query = _build_datadog_search_query(query, service, "*")

    if not api_key or not app_key:
        fallback_logs = _load_local_demo_logs(query=query, service=service, limit=limit, minutes=minutes)
        source_env = _get_source_env()

        if source_env is not None:
            matching_logs = [
                _serialize_log_entry(log)
                for log in source_env.all_logs
                if not service or str(getattr(log.service, "value", log.service)) == service
            ]
            fallback_logs.extend(matching_logs)

        fallback_logs.sort(key=lambda item: _parse_timestamp(item.get("timestamp")), reverse=True)
        fallback_logs = fallback_logs[: max(1, min(limit, 100))]

        return {
            "source": "local-fallback",
            "query": search_query,
            "logs": fallback_logs,
            "note": (
                "DD_API_KEY and DD_APP_KEY are not configured. "
                "Showing locally replayed demo logs and simulator logs instead."
                if fallback_logs
                else "DD_API_KEY or DD_APP_KEY not configured. Showing environment logs instead."
            ),
        }

    payload = _build_log_search_payload(
        search_query=search_query,
        limit=limit,
        minutes=minutes,
        indexes=settings.get("indexes"),
    )

    response = requests.post(
        f"{_site_base_url(site)}/api/v2/logs/events/search",
        headers={
            "DD-API-KEY": api_key,
            "DD-APPLICATION-KEY": app_key,
            "Accept": "application/json",
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


def _fetch_elasticsearch_logs(query: str = "*", service: Optional[str] = None, limit: int = 25, minutes: int = 15) -> Dict[str, Any]:
    settings = _get_elasticsearch_settings()
    if not _elasticsearch_enabled(settings):
        raise HTTPException(status_code=500, detail="Elasticsearch is not configured.")

    payload = _build_elasticsearch_log_payload(
        query=query,
        service=service,
        limit=limit,
        minutes=minutes,
        settings=settings,
    )
    response = requests.post(
        f"{settings['url']}/{settings['index']}/_search",
        headers=_elastic_auth_headers(settings),
        json=payload,
        timeout=12,
        verify=settings["verify_tls"],
    )

    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Elasticsearch logs API error: {response.text}")

    body = response.json()
    hits = (((body or {}).get("hits") or {}).get("hits")) or []
    logs: List[Dict[str, Any]] = []
    message_field = settings["message_field"]
    level_field = settings["level_field"]
    service_field = settings["service_field"]
    timestamp_field = settings["timestamp_field"]

    for hit in hits:
        source = hit.get("_source", {}) if isinstance(hit, dict) else {}
        logs.append(
            {
                "timestamp": _pick_first(source, [timestamp_field], hit.get("sort", [None])[0] if isinstance(hit, dict) else None),
                "service": _pick_first(source, [service_field, f"{service_field}.name", "service.name", "kubernetes.container.name", "host.name"], hit.get("_index") if isinstance(hit, dict) else "unknown"),
                "level": _pick_first(source, [level_field, "level", "severity", "log.level"], "info"),
                "message": _pick_first(source, [message_field, "event.original", "message"], json.dumps(source)[:240] if source else ""),
                "trace_id": _pick_first(source, ["trace.id", "trace_id", "dd.trace_id"], None),
                "is_relevant": True,
                "raw": hit,
            }
        )

    return {
        "source": "elasticsearch",
        "query": query or "*",
        "logs": logs,
        "note": f"Showing Elasticsearch logs from index pattern `{settings['index']}`.",
    }


def _concrete_index_name(index_pattern: str) -> str:
    today = datetime.utcnow().strftime("%Y.%m.%d")
    if "*" in index_pattern:
        return index_pattern.replace("*", today)
    return index_pattern


def _bulk_lines(index_name: str, docs: List[Dict[str, Any]]) -> str:
    parts: List[str] = []
    for doc in docs:
        parts.append(json.dumps({"index": {"_index": index_name}}))
        parts.append(json.dumps(doc))
    return "\n".join(parts) + "\n"


def _seed_demo_logs_into_elasticsearch(scenario: str = "all") -> Dict[str, Any]:
    settings = _get_elasticsearch_settings()
    if not _elasticsearch_enabled(settings):
        raise HTTPException(status_code=400, detail="Elasticsearch is not configured for this dashboard.")

    docs = docs_for_scenario(scenario)
    target_index = _concrete_index_name(settings["index"])
    response = requests.post(
        f"{settings['url']}/_bulk",
        headers={
            **_elastic_auth_headers(settings),
            "Content-Type": "application/x-ndjson",
        },
        data=_bulk_lines(target_index, docs).encode("utf-8"),
        timeout=15,
        verify=settings["verify_tls"],
    )

    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Elasticsearch bulk seed failed: {response.text}")

    body = response.json()
    if body.get("errors"):
        raise HTTPException(status_code=502, detail="Elasticsearch reported indexing errors while seeding demo logs.")

    incident_ids = sorted({doc["incident_id"] for doc in docs})
    return {
        "status": "seeded",
        "source": "elasticsearch",
        "scenario": scenario,
        "index": target_index,
        "count": len(docs),
        "service": "meta-pytorch-demo",
        "query": "*",
        "incident_ids": incident_ids,
        "note": "Fresh demo incidents were written into Elasticsearch and are ready in the dashboard log stream.",
    }


def _fetch_elasticsearch_traces(query: str = "service:*", service: Optional[str] = None, limit: int = 20, minutes: int = 15) -> Dict[str, Any]:
    settings = _get_elasticsearch_settings()
    if not _elasticsearch_enabled(settings):
        raise HTTPException(status_code=500, detail="Elasticsearch is not configured.")

    payload = _build_elasticsearch_log_payload(
        query=query,
        service=service,
        limit=limit,
        minutes=minutes,
        settings=settings,
    )
    response = requests.post(
        f"{settings['url']}/{settings['index']}/_search",
        headers=_elastic_auth_headers(settings),
        json=payload,
        timeout=12,
        verify=settings["verify_tls"],
    )

    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Elasticsearch trace search error: {response.text}")

    body = response.json()
    hits = (((body or {}).get("hits") or {}).get("hits")) or []
    traces: List[Dict[str, Any]] = []

    for hit in hits:
        source = hit.get("_source", {}) if isinstance(hit, dict) else {}
        duration_nanos = _pick_first(
            source,
            [
                "event.duration",
                "transaction.duration.us",
                "span.duration.us",
            ],
            None,
        )
        if duration_nanos is not None:
            try:
                duration_value = float(duration_nanos)
                if duration_value > 100000:
                    duration_ms: Optional[float] = round(duration_value / 1000000.0, 2)
                else:
                    duration_ms = round(duration_value / 1000.0, 2)
            except (TypeError, ValueError):
                duration_ms = None
        else:
            duration_ms = None

        trace_id = _pick_first(source, ["trace.id", "trace_id", "transaction.id", "span.id"], None)
        message = _pick_first(
            source,
            [settings["message_field"], "event.original", "message"],
            json.dumps(source)[:160] if source else "",
        )
        traces.append(
            {
                "timestamp": _pick_first(source, [settings["timestamp_field"]], hit.get("sort", [None])[0] if isinstance(hit, dict) else None),
                "service": _pick_first(source, [settings["service_field"], "service.name", "service", "host.name"], "unknown"),
                "operation": _pick_first(source, ["event.action", "event.dataset", "transaction.name", "span.name"], "log_event"),
                "resource": message[:120],
                "duration_ms": duration_ms,
                "trace_id": trace_id,
                "derived": True,
            }
        )

    return {
        "source": "elasticsearch",
        "query": query or "*",
        "traces": traces,
        "note": "Showing Elastic log-derived trace events. Configure Datadog APM later if you want full distributed spans.",
    }


def _fetch_datadog_status() -> Dict[str, Any]:
    settings = _get_datadog_settings()
    site = settings["site"]
    configured = _datadog_enabled(settings)

    base_status = {
        "site": site,
        "configured": configured,
        "api_key_hint": _mask_secret(settings["api_key"]),
        "app_key_hint": _mask_secret(settings["app_key"]),
        "log_indexes": settings.get("indexes", []),
    }

    if not configured:
        return {
            **base_status,
            "connected": False,
            "source": "local-fallback",
            "message": "Set DD_API_KEY and DD_APP_KEY to stream Datadog logs into the dashboard.",
        }

    payload = _build_log_search_payload(search_query="*", limit=1, minutes=15, indexes=settings.get("indexes"))

    try:
        response = requests.post(
            f"{_site_base_url(site)}/api/v2/logs/events/search",
            headers={
                "DD-API-KEY": settings["api_key"],
                "DD-APPLICATION-KEY": settings["app_key"],
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10,
        )
    except requests.RequestException as exc:
        return {
            **base_status,
            "connected": False,
            "source": "datadog",
            "message": f"Datadog request failed: {exc}",
        }

    if response.status_code >= 400:
        try:
            details = response.json()
        except ValueError:
            details = {"errors": [response.text]}

        return {
            **base_status,
            "connected": False,
            "source": "datadog",
            "message": "Datadog credentials were found, but the log search request failed.",
            "details": details,
            "status_code": response.status_code,
        }

    body = response.json()
    event_count = len(body.get("data") or [])

    return {
        **base_status,
        "connected": True,
        "source": "datadog",
        "message": "Datadog log search is active for this dashboard.",
        "sample_count": event_count,
    }


def _fetch_elasticsearch_status() -> Dict[str, Any]:
    settings = _get_elasticsearch_settings()
    configured = _elasticsearch_enabled(settings)

    base_status = {
        "backend": "elasticsearch",
        "configured": configured,
        "url": settings["url"],
        "index": settings["index"],
        "timestamp_field": settings["timestamp_field"],
        "service_field": settings["service_field"],
        "message_field": settings["message_field"],
        "auth_mode": "api_key" if settings["api_key"] else ("basic" if settings["username"] and settings["password"] else "none"),
        "api_key_hint": _mask_secret(settings["api_key"]),
        "username_hint": settings["username"],
    }

    if not configured:
        return {
            **base_status,
            "connected": False,
            "source": "local-fallback",
            "message": "Set ELASTICSEARCH_URL to use Elasticsearch for dashboard logs.",
        }

    payload = _build_elasticsearch_log_payload(
        query="*",
        service=None,
        limit=1,
        minutes=15,
        settings=settings,
    )

    try:
        response = requests.post(
            f"{settings['url']}/{settings['index']}/_search",
            headers=_elastic_auth_headers(settings),
            json=payload,
            timeout=10,
            verify=settings["verify_tls"],
        )
    except requests.RequestException as exc:
        return {
            **base_status,
            "connected": False,
            "source": "elasticsearch",
            "message": f"Elasticsearch request failed: {exc}",
        }

    if response.status_code >= 400:
        return {
            **base_status,
            "connected": False,
            "source": "elasticsearch",
            "status_code": response.status_code,
            "message": "Elasticsearch credentials or index settings were found, but log search failed.",
            "details": response.text,
        }

    body = response.json()
    sample_count = len((((body or {}).get("hits") or {}).get("hits")) or [])
    return {
        **base_status,
        "connected": True,
        "source": "elasticsearch",
        "message": "Elasticsearch log search is active for this dashboard.",
        "sample_count": sample_count,
    }


def _fetch_observability_status() -> Dict[str, Any]:
    elastic_settings = _get_elasticsearch_settings()
    if _elasticsearch_enabled(elastic_settings):
        return _fetch_elasticsearch_status()

    datadog_status = _fetch_datadog_status()
    if datadog_status.get("configured"):
        return {
            **datadog_status,
            "backend": "datadog",
        }

    return {
        "backend": "local-fallback",
        "configured": False,
        "connected": False,
        "source": "local-fallback",
        "message": (
            "No external log provider configured. The dashboard will use locally replayed demo logs and simulator logs."
            if _has_local_demo_logs()
            else "No external log provider configured. The dashboard will use simulator logs."
        ),
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

    search_query = _build_datadog_search_query(query, service, "service:*")

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
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Distributed Incident War Room</title>
    <style>
        @import url("https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@500;700&family=Manrope:wght@400;500;600;700;800&family=Sora:wght@500;600;700;800&display=swap");

        :root {
            --bg: #061016;
            --bg-soft: #0d1a21;
            --panel: rgba(7, 14, 20, 0.82);
            --panel-strong: rgba(5, 10, 16, 0.94);
            --panel-tint: rgba(18, 32, 40, 0.84);
            --line: rgba(133, 167, 182, 0.16);
            --line-strong: rgba(133, 167, 182, 0.3);
            --text: #f1f6f7;
            --muted: #93a8b1;
            --muted-strong: #c2d0d7;
            --teal: #62f1d6;
            --amber: #ffbf66;
            --red: #ff6c72;
            --green: #71f0a1;
            --cyan: #6cbfff;
            --violet: #7d8cff;
            --shadow: 0 30px 80px rgba(0, 0, 0, 0.42);
            --radius-xl: 28px;
            --radius-lg: 22px;
            --radius-md: 16px;
        }

        * {
            box-sizing: border-box;
        }

        html {
            scroll-behavior: smooth;
        }

        body {
            margin: 0;
            min-height: 100vh;
            font-family: "Manrope", "Segoe UI", sans-serif;
            color: var(--text);
            background:
                radial-gradient(circle at top left, rgba(98, 241, 214, 0.2), transparent 30%),
                radial-gradient(circle at 84% 12%, rgba(255, 191, 102, 0.15), transparent 24%),
                radial-gradient(circle at 70% 88%, rgba(108, 191, 255, 0.12), transparent 28%),
                radial-gradient(circle at 50% 50%, rgba(125, 140, 255, 0.06), transparent 42%),
                linear-gradient(160deg, #040b10 0%, #09131a 46%, #050c12 100%);
            overflow-x: hidden;
        }

        body::before,
        body::after {
            content: "";
            position: fixed;
            inset: 0;
            pointer-events: none;
        }

        body::before {
            background:
                linear-gradient(rgba(255, 255, 255, 0.025) 1px, transparent 1px),
                linear-gradient(90deg, rgba(255, 255, 255, 0.025) 1px, transparent 1px);
            background-size: 56px 56px;
            mask-image: linear-gradient(180deg, rgba(0, 0, 0, 0.8), transparent 100%);
            opacity: 0.75;
        }

        body::after {
            background:
                radial-gradient(circle at center, transparent 54%, rgba(0, 0, 0, 0.28) 100%),
                linear-gradient(180deg, rgba(4, 8, 12, 0), rgba(4, 8, 12, 0.36));
        }

        .shell {
            position: relative;
            z-index: 1;
            width: min(1520px, calc(100vw - 28px));
            margin: 0 auto;
            padding: 24px 0 40px;
        }

        .hero {
            display: grid;
            gap: 18px;
            grid-template-columns: minmax(0, 1.45fr) minmax(340px, 0.85fr);
            margin-bottom: 18px;
        }

        .surface {
            position: relative;
            overflow: hidden;
            border: 1px solid var(--line);
            border-radius: var(--radius-xl);
            background: linear-gradient(180deg, rgba(13, 24, 31, 0.92), rgba(7, 14, 20, 0.88));
            box-shadow: var(--shadow);
            backdrop-filter: blur(20px);
            animation: rise 420ms ease both;
        }

        .surface::before {
            content: "";
            position: absolute;
            inset: 0;
            background: linear-gradient(135deg, rgba(98, 241, 214, 0.08), transparent 42%, rgba(255, 191, 102, 0.06));
            pointer-events: none;
        }

        .surface::after {
            content: "";
            position: absolute;
            inset: -40% auto -40% -30%;
            width: 44%;
            background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.06), transparent);
            transform: rotate(16deg);
            animation: sheen 14s linear infinite;
            pointer-events: none;
        }

        .hero-card {
            padding: 32px;
            min-height: 240px;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
        }

        .eyebrow {
            display: inline-flex;
            align-items: center;
            gap: 10px;
            color: var(--teal);
            text-transform: uppercase;
            letter-spacing: 0.22em;
            font-size: 11px;
            font-weight: 700;
            font-family: "JetBrains Mono", monospace;
        }

        .eyebrow::before {
            content: "";
            width: 26px;
            height: 1px;
            background: currentColor;
        }

        h1,
        h2,
        h3 {
            margin: 0;
            font-family: "Sora", "Manrope", sans-serif;
            letter-spacing: -0.03em;
        }

        h1 {
            margin-top: 18px;
            max-width: 10ch;
            font-size: clamp(38px, 6vw, 72px);
            line-height: 0.92;
            background: linear-gradient(135deg, #f5fbfc, #9beeff 44%, #ffd496 100%);
            -webkit-background-clip: text;
            color: transparent;
        }

        .subtitle {
            max-width: 60ch;
            margin: 18px 0 0;
            color: var(--muted-strong);
            font-size: 15px;
            line-height: 1.75;
        }

        .hero-footer {
            margin-top: 24px;
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
        }

        .chip,
        .badge {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            min-height: 36px;
            padding: 8px 12px;
            border-radius: 999px;
            border: 1px solid rgba(143, 174, 188, 0.18);
            background: rgba(255, 255, 255, 0.04);
            color: var(--muted);
            font-size: 12px;
            font-weight: 700;
            backdrop-filter: blur(10px);
        }

        .badge[data-tone="good"],
        .source-card[data-tone="good"] strong,
        .metric-card[data-tone="good"] .metric-value {
            color: var(--green);
        }

        .badge[data-tone="warn"],
        .source-card[data-tone="warn"] strong,
        .metric-card[data-tone="warn"] .metric-value {
            color: var(--amber);
        }

        .badge[data-tone="bad"],
        .source-card[data-tone="bad"] strong,
        .metric-card[data-tone="bad"] .metric-value {
            color: var(--red);
        }

        .hero-summary {
            padding: 24px;
            display: grid;
            gap: 14px;
            align-content: start;
        }

        .summary-head {
            display: flex;
            justify-content: space-between;
            gap: 12px;
            align-items: start;
        }

        .summary-head p,
        .panel-subtitle,
        .section-copy,
        .helper,
        .mini-note {
            margin: 0;
            color: var(--muted);
        }

        .summary-grid,
        .metric-strip,
        .controls-grid,
        .filters-grid,
        .services-grid,
        .insight-grid {
            display: grid;
            gap: 12px;
        }

        .summary-grid {
            grid-template-columns: repeat(2, minmax(0, 1fr));
        }

        .summary-item {
            padding: 14px;
            border-radius: var(--radius-md);
            background: rgba(255, 255, 255, 0.035);
            border: 1px solid rgba(143, 174, 188, 0.12);
        }

        .summary-label,
        .metric-label,
        .eyelabel,
        label {
            display: block;
            color: var(--muted);
            text-transform: uppercase;
            letter-spacing: 0.14em;
            font-size: 11px;
            font-weight: 600;
            font-family: "JetBrains Mono", monospace;
        }

        .summary-value {
            margin-top: 8px;
            font-size: 18px;
            font-weight: 700;
            color: var(--text);
        }

        .metric-strip {
            grid-template-columns: repeat(5, minmax(0, 1fr));
            margin-bottom: 18px;
        }

        .banner {
            display: none;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
            padding: 16px 20px;
            margin-bottom: 18px;
            border-left: 3px solid rgba(255, 255, 255, 0.12);
        }

        .banner.visible {
            display: flex;
        }

        .banner-copy {
            display: grid;
            gap: 6px;
        }

        .banner[data-kind="info"] {
            border-color: rgba(121, 240, 220, 0.24);
            background: linear-gradient(135deg, rgba(121, 240, 220, 0.1), rgba(255, 255, 255, 0.03));
        }

        .banner[data-kind="warn"] {
            border-color: rgba(255, 198, 107, 0.24);
            background: linear-gradient(135deg, rgba(255, 198, 107, 0.12), rgba(255, 255, 255, 0.03));
        }

        .banner[data-kind="error"] {
            border-color: rgba(255, 127, 115, 0.28);
            background: linear-gradient(135deg, rgba(255, 127, 115, 0.12), rgba(255, 255, 255, 0.03));
        }

        .metric-card {
            position: relative;
            padding: 18px 18px 16px;
        }

        .metric-card::before,
        .source-card::before {
            content: "";
            position: absolute;
            inset: 0 0 auto 0;
            height: 2px;
            background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.36), transparent);
            opacity: 0.5;
        }

        .metric-card[data-tone="good"] {
            box-shadow: inset 0 0 0 1px rgba(113, 240, 161, 0.08), 0 16px 36px rgba(2, 10, 10, 0.16);
        }

        .metric-card[data-tone="warn"] {
            box-shadow: inset 0 0 0 1px rgba(255, 191, 102, 0.08), 0 16px 36px rgba(18, 10, 2, 0.16);
        }

        .metric-card[data-tone="bad"] {
            box-shadow: inset 0 0 0 1px rgba(255, 108, 114, 0.08), 0 16px 36px rgba(18, 4, 4, 0.2);
        }

        .metric-value {
            margin-top: 12px;
            font-size: clamp(24px, 3vw, 34px);
            font-weight: 800;
            letter-spacing: -0.03em;
        }

        .metric-foot {
            margin-top: 8px;
            font-size: 12px;
            color: var(--muted);
        }

        .workspace {
            display: grid;
            gap: 18px;
            grid-template-columns: 320px minmax(0, 1fr) 360px;
            align-items: start;
        }

        .rail,
        .stack {
            display: grid;
            gap: 18px;
        }

        .panel {
            padding: 22px;
        }

        .panel-head {
            display: flex;
            justify-content: space-between;
            gap: 12px;
            align-items: start;
            margin-bottom: 18px;
        }

        .panel-title {
            display: grid;
            gap: 6px;
        }

        .panel-title h2 {
            font-size: 24px;
        }

        .panel-subtitle {
            font-size: 13px;
            line-height: 1.6;
        }

        .controls-grid {
            grid-template-columns: repeat(2, minmax(0, 1fr));
        }

        .filters-grid {
            grid-template-columns: repeat(4, minmax(0, 1fr));
        }

        .filters-grid.filters-grid-logs {
            grid-template-columns: minmax(130px, 0.9fr) minmax(220px, 2.2fr) minmax(130px, 1fr) minmax(100px, 0.8fr) minmax(100px, 0.8fr);
            align-items: end;
            gap: 10px;
        }

        .filters-grid.filters-grid-logs .field {
            min-width: 0;
        }

        .filters-grid.filters-grid-logs input,
        .filters-grid.filters-grid-logs select {
            min-width: 0%;
            max-width: 100%;
        }

        .filters-grid.filters-grid-logs select {
            padding-right: 10px;
        }

        .source-strip {
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 12px;
            margin-bottom: 18px;
        }

        .guide-panel {
            display: grid;
            gap: 14px;
            grid-template-columns: minmax(0, 1.2fr) auto;
            align-items: start;
            padding: 18px;
            margin-bottom: 18px;
            border-radius: 22px;
            border: 1px solid rgba(143, 174, 188, 0.14);
            background:
                linear-gradient(135deg, rgba(98, 241, 214, 0.08), transparent 40%),
                linear-gradient(180deg, rgba(255, 255, 255, 0.04), rgba(255, 255, 255, 0.02));
        }

        .guide-copy {
            display: grid;
            gap: 8px;
        }

        .guide-copy strong {
            font-size: 18px;
        }

        .guide-actions {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            justify-content: flex-end;
        }

        .guide-btn {
            width: auto;
            min-width: 180px;
            white-space: nowrap;
        }

        .guide-steps {
            display: grid;
            gap: 10px;
            grid-column: 1 / -1;
        }

        .guide-step {
            display: flex;
            gap: 12px;
            align-items: start;
            padding: 12px 14px;
            border-radius: 16px;
            border: 1px solid rgba(143, 174, 188, 0.12);
            background: rgba(255, 255, 255, 0.03);
        }

        .guide-step-index {
            display: inline-grid;
            place-items: center;
            width: 24px;
            height: 24px;
            border-radius: 999px;
            background: rgba(98, 241, 214, 0.16);
            color: var(--teal);
            font-size: 12px;
            font-weight: 800;
            font-family: "JetBrains Mono", monospace;
            flex-shrink: 0;
        }

        .history-row {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin-top: 14px;
        }

        .history-chip {
            width: auto;
            min-height: 36px;
            padding: 8px 12px;
            border-radius: 999px;
            background: rgba(255, 255, 255, 0.05);
            color: var(--muted-strong);
            border: 1px solid rgba(143, 174, 188, 0.14);
            font-size: 12px;
            font-weight: 700;
            box-shadow: none;
        }

        .history-chip:hover,
        .log-pill.log-action:hover {
            box-shadow: none;
        }

        .source-card {
            position: relative;
            padding: 14px;
            border-radius: 18px;
            border: 1px solid rgba(143, 174, 188, 0.12);
            background: rgba(255, 255, 255, 0.035);
        }

        .source-card strong {
            display: block;
            margin-top: 8px;
            font-size: 15px;
        }

        .source-card small {
            display: block;
            margin-top: 6px;
            color: var(--muted);
            line-height: 1.5;
        }

        .field {
            display: grid;
            gap: 8px;
            min-width: 0;
            transition: opacity 180ms ease, transform 180ms ease;
        }

        .field.field-full {
            grid-column: 1 / -1;
        }

        .field.field-muted {
            opacity: 0.4;
            transform: scale(0.99);
        }

        input,
        select,
        button {
            width: 100%;
            border-radius: 14px;
            font: inherit;
            outline: none;
        }

        input,
        select {
            border: 1px solid rgba(143, 174, 188, 0.16);
            background: rgba(4, 10, 14, 0.78);
            color: var(--text);
            padding: 12px 14px;
            min-height: 48px;
            transition: border-color 180ms ease, background 180ms ease, transform 180ms ease, box-shadow 180ms ease;
        }

        input:focus,
        select:focus {
            border-color: rgba(98, 241, 214, 0.72);
            background: rgba(8, 16, 22, 0.92);
            box-shadow: 0 0 0 4px rgba(98, 241, 214, 0.08);
            transform: translateY(-1px);
        }

        input:disabled,
        select:disabled {
            cursor: not-allowed;
            opacity: 0.65;
        }

        button {
            border: 0;
            cursor: pointer;
            min-height: 48px;
            padding: 12px 16px;
            font-weight: 700;
            letter-spacing: 0.01em;
            transition: transform 180ms ease, box-shadow 180ms ease, filter 180ms ease;
            font-family: "Manrope", sans-serif;
        }

        button:hover {
            transform: translateY(-1px);
            box-shadow: 0 14px 28px rgba(0, 0, 0, 0.22);
        }

        .btn-row {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 10px;
        }

        .mission-actions {
            display: grid;
            gap: 10px;
            margin-top: 14px;
        }

        .mission-actions .btn-primary {
            min-height: 56px;
            font-size: 18px;
            box-shadow: 0 16px 34px rgba(73, 219, 211, 0.16);
        }

        .utility-actions {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 10px;
        }

        .mission-hint {
            padding: 12px 14px;
            border-radius: 14px;
            border: 1px solid rgba(143, 174, 188, 0.12);
            background: rgba(255, 255, 255, 0.03);
        }

        .btn-primary {
            background: linear-gradient(135deg, #62f1d6, #9fe1ff);
            color: #08252b;
        }

        .btn-secondary {
            background: linear-gradient(135deg, rgba(255, 191, 102, 0.96), rgba(255, 126, 109, 0.92));
            color: #2c1304;
        }

        .btn-ghost {
            background: rgba(255, 255, 255, 0.06);
            color: var(--text);
            border: 1px solid rgba(143, 174, 188, 0.18);
        }

        .helper {
            font-size: 13px;
            line-height: 1.6;
        }

        .mission-box,
        .note-box,
        .stream-shell,
        .state-box,
        .action-assist,
        .score-shell {
            padding: 16px;
            border-radius: var(--radius-md);
            border: 1px solid rgba(143, 174, 188, 0.12);
            background: rgba(255, 255, 255, 0.035);
        }

        .mission-box strong,
        .note-box strong,
        .state-box strong {
            display: block;
            margin-bottom: 8px;
            font-size: 15px;
        }

        .note-box {
            background:
                linear-gradient(135deg, rgba(121, 240, 220, 0.08), transparent 60%),
                rgba(255, 255, 255, 0.035);
        }

        .action-assist,
        .score-shell {
            display: grid;
            gap: 10px;
        }

        .assist-actions {
            display: grid;
            gap: 10px;
            grid-template-columns: repeat(2, minmax(0, 1fr));
        }

        .action-risk {
            padding: 10px 12px;
            border-radius: 12px;
            background: rgba(255, 191, 102, 0.08);
            border: 1px solid rgba(255, 191, 102, 0.14);
        }

        .tabs {
            display: inline-flex;
            gap: 8px;
            padding: 6px;
            border-radius: 999px;
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid rgba(143, 174, 188, 0.14);
        }

        .tab {
            min-height: 40px;
            padding: 10px 16px;
            border-radius: 999px;
            background: transparent;
            color: var(--muted);
            box-shadow: none;
        }

        .tab.active {
            background: linear-gradient(135deg, rgba(98, 241, 214, 0.18), rgba(108, 191, 255, 0.18));
            color: var(--text);
            box-shadow: 0 0 0 1px rgba(98, 241, 214, 0.12) inset;
        }

        .tab-pane {
            display: none;
            animation: fade 220ms ease;
        }

        .tab-pane.active {
            display: block;
        }

        .stream-shell {
            padding: 18px;
        }

        .stream {
            display: grid;
            gap: 12px;
            max-height: 560px;
            overflow: auto;
        }

        .stream-card {
            position: relative;
            padding: 15px 16px;
            border-radius: 18px;
            background: rgba(4, 8, 12, 0.56);
            border: 1px solid rgba(143, 174, 188, 0.12);
            box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.02);
            transition: transform 180ms ease, border-color 180ms ease, background 180ms ease;
        }

        .stream-card:hover {
            transform: translateY(-2px);
            border-color: rgba(143, 174, 188, 0.24);
            background: rgba(8, 14, 20, 0.78);
        }

        .stream-card[data-tone="good"] {
            border-color: rgba(113, 240, 161, 0.18);
        }

        .stream-card[data-tone="warn"] {
            border-color: rgba(255, 191, 102, 0.18);
        }

        .stream-card[data-tone="bad"] {
            border-color: rgba(255, 108, 114, 0.18);
        }

        .stream-card.level-ERROR,
        .stream-card.level-critical {
            border-left: 3px solid var(--red);
        }

        .stream-card.level-WARN,
        .stream-card.level-warning {
            border-left: 3px solid var(--amber);
        }

        .stream-card.level-INFO,
        .stream-card.level-info {
            border-left: 3px solid var(--cyan);
        }

        .stream-top {
            display: flex;
            justify-content: space-between;
            gap: 12px;
            color: var(--muted);
            font-size: 12px;
        }

        .stream-body {
            margin-top: 8px;
            line-height: 1.6;
        }

        .stream-tags {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin-top: 12px;
        }

        .log-pill {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 5px 9px;
            border-radius: 999px;
            background: rgba(255, 255, 255, 0.05);
            color: var(--muted-strong);
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            font-family: "JetBrains Mono", monospace;
        }

        .log-pill.log-action {
            border: 1px solid rgba(143, 174, 188, 0.18);
            cursor: pointer;
        }

        .empty-state {
            display: grid;
            place-items: center;
            min-height: 220px;
            border-radius: 18px;
            border: 1px dashed rgba(143, 174, 188, 0.18);
            color: var(--muted);
            text-align: center;
            padding: 24px;
        }

        .empty-state strong {
            display: block;
            margin-bottom: 8px;
            color: var(--text);
        }

        .list {
            display: grid;
            gap: 10px;
        }

        .pill-list {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
        }

        .incident-pill,
        .alert-item,
        .service-card {
            border-radius: 16px;
            border: 1px solid rgba(143, 174, 188, 0.12);
            background: rgba(255, 255, 255, 0.035);
        }

        .incident-pill {
            padding: 10px 12px;
            color: var(--text);
            font-size: 13px;
        }

        .incident-pill[data-tone="good"] {
            border-color: rgba(113, 240, 161, 0.18);
            background: rgba(113, 240, 161, 0.08);
        }

        .incident-pill[data-tone="warn"] {
            border-color: rgba(255, 191, 102, 0.18);
            background: rgba(255, 191, 102, 0.08);
        }

        .incident-pill[data-tone="bad"] {
            border-color: rgba(255, 108, 114, 0.18);
            background: rgba(255, 108, 114, 0.08);
        }

        .alert-item {
            padding: 12px 14px;
        }

        .alert-item[data-severity="good"] {
            border-left: 3px solid var(--green);
        }

        .alert-item[data-severity="warn"] {
            border-left: 3px solid var(--amber);
        }

        .alert-item[data-severity="bad"] {
            border-left: 3px solid var(--red);
        }

        .alert-head {
            display: flex;
            justify-content: space-between;
            gap: 10px;
            margin-bottom: 6px;
            font-size: 12px;
            color: var(--muted);
        }

        .services-grid {
            grid-template-columns: 1fr;
        }

        .service-card {
            padding: 14px;
        }

        .service-card[data-health="degraded"] {
            background:
                linear-gradient(135deg, rgba(255, 108, 114, 0.08), transparent 56%),
                rgba(255, 255, 255, 0.035);
        }

        .service-card[data-health="healthy"] {
            background:
                linear-gradient(135deg, rgba(113, 240, 161, 0.08), transparent 56%),
                rgba(255, 255, 255, 0.035);
        }

        .service-top {
            display: flex;
            justify-content: space-between;
            gap: 12px;
            align-items: center;
            flex-wrap: wrap;
            margin-bottom: 10px;
        }

        .service-name {
            font-size: 17px;
            font-weight: 700;
        }

        .service-status {
            padding: 5px 9px;
            border-radius: 999px;
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.08em;
        }

        .service-status.healthy {
            background: rgba(135, 242, 162, 0.12);
            color: var(--green);
        }

        .service-status.degraded {
            background: rgba(255, 127, 115, 0.12);
            color: var(--red);
        }

        .service-summary {
            margin-bottom: 12px;
            color: var(--muted);
            font-size: 13px;
            line-height: 1.6;
        }

        .mini-grid {
            display: grid;
            gap: 8px 18px;
            grid-template-columns: repeat(2, minmax(0, 1fr));
        }

        .mini-row {
            display: flex;
            justify-content: space-between;
            gap: 12px;
            font-size: 12px;
            color: var(--muted);
        }

        .progress {
            position: relative;
            height: 10px;
            margin-top: 12px;
            border-radius: 999px;
            background: rgba(255, 255, 255, 0.06);
            overflow: hidden;
        }

        .progress > span {
            position: absolute;
            inset: 0 auto 0 0;
            width: 0%;
            background: linear-gradient(90deg, #79f0dc, #ffc66b, #ff7f73);
            border-radius: inherit;
            transition: width 240ms ease;
        }

        .service-meter {
            position: relative;
            height: 8px;
            border-radius: 999px;
            background: rgba(255, 255, 255, 0.08);
            overflow: hidden;
            margin-top: 12px;
        }

        .service-meter > span {
            position: absolute;
            inset: 0 auto 0 0;
            width: 0%;
            border-radius: inherit;
            background: linear-gradient(90deg, #62f1d6, #ffbf66, #ff6c72);
        }

        .service-metric-stack {
            display: grid;
            gap: 8px;
            margin-top: 12px;
        }

        .metric-track-row {
            display: grid;
            grid-template-columns: 60px 1fr auto;
            gap: 10px;
            align-items: center;
            font-size: 12px;
            color: var(--muted);
        }

        .metric-track {
            position: relative;
            height: 7px;
            border-radius: 999px;
            background: rgba(255, 255, 255, 0.08);
            overflow: hidden;
        }

        .metric-track > span {
            position: absolute;
            inset: 0 auto 0 0;
            width: 0%;
            border-radius: inherit;
            background: linear-gradient(90deg, rgba(98, 241, 214, 0.92), rgba(255, 191, 102, 0.95), rgba(255, 108, 114, 0.95));
        }

        .feedback-grid {
            display: grid;
            gap: 14px;
            grid-template-columns: minmax(0, 1.15fr) minmax(280px, 0.85fr);
        }

        .mini-metric-grid {
            display: grid;
            gap: 12px;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            margin-top: 14px;
        }

        .mini-metric-card {
            padding: 12px;
            border-radius: 16px;
            border: 1px solid rgba(143, 174, 188, 0.12);
            background: rgba(255, 255, 255, 0.03);
        }

        .mini-metric-card strong {
            display: block;
            margin-top: 8px;
            font-size: 18px;
        }

        .component-list {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin-top: 14px;
        }

        .component-pill {
            padding: 7px 10px;
            border-radius: 999px;
            font-size: 12px;
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(143, 174, 188, 0.14);
            color: var(--muted-strong);
        }

        .score-breakdown {
            display: grid;
            gap: 12px;
            margin-top: 14px;
        }

        .score-row {
            display: grid;
            gap: 10px;
            grid-template-columns: 86px 1fr 48px;
            align-items: center;
            font-size: 13px;
        }

        .score-track {
            position: relative;
            height: 10px;
            border-radius: 999px;
            background: rgba(255, 255, 255, 0.08);
            overflow: hidden;
        }

        .score-track > span {
            position: absolute;
            inset: 0 auto 0 0;
            width: 0%;
            border-radius: inherit;
            background: linear-gradient(90deg, #62f1d6, #9fe1ff);
            transition: width 220ms ease;
        }

        .timeline-list {
            display: grid;
            gap: 12px;
        }

        .timeline-item {
            display: grid;
            gap: 10px;
            grid-template-columns: 16px minmax(0, 1fr);
            align-items: start;
        }

        .timeline-dot {
            width: 12px;
            height: 12px;
            margin-top: 6px;
            border-radius: 999px;
            background: rgba(98, 241, 214, 0.85);
            box-shadow: 0 0 0 6px rgba(98, 241, 214, 0.08);
        }

        .timeline-dot[data-tone="warn"] {
            background: rgba(255, 191, 102, 0.92);
            box-shadow: 0 0 0 6px rgba(255, 191, 102, 0.08);
        }

        .timeline-dot[data-tone="bad"] {
            background: rgba(255, 108, 114, 0.92);
            box-shadow: 0 0 0 6px rgba(255, 108, 114, 0.08);
        }

        .timeline-card {
            padding: 14px;
            border-radius: 16px;
            border: 1px solid rgba(143, 174, 188, 0.12);
            background: rgba(255, 255, 255, 0.03);
        }

        .timeline-head {
            display: flex;
            justify-content: space-between;
            gap: 10px;
            margin-bottom: 6px;
            color: var(--muted);
            font-size: 12px;
        }

        .timeline-title {
            font-size: 14px;
            font-weight: 700;
            color: var(--text);
        }

        .timeline-copy {
            margin-top: 6px;
            color: var(--muted);
            line-height: 1.6;
            font-size: 13px;
        }

        pre {
            margin: 0;
            white-space: pre-wrap;
            word-break: break-word;
            border-radius: 18px;
            border: 1px solid rgba(143, 174, 188, 0.12);
            background: rgba(4, 8, 12, 0.68);
            color: #d9edf0;
            padding: 16px;
            min-height: 300px;
            max-height: 420px;
            overflow: auto;
            font-size: 12px;
            line-height: 1.6;
            font-family: "JetBrains Mono", monospace;
        }

        [data-state="good"] {
            color: var(--green);
            font-weight: 800;
        }

        [data-state="warn"] {
            color: var(--amber);
            font-weight: 800;
        }

        [data-state="bad"] {
            color: var(--red);
            font-weight: 800;
        }

        @keyframes rise {
            from {
                opacity: 0;
                transform: translateY(14px);
            }
            to {
                opacity: 1;
                transform: translateY(0);
            }
        }

        @keyframes fade {
            from {
                opacity: 0;
            }
            to {
                opacity: 1;
            }
        }

        @keyframes sheen {
            from {
                transform: translateX(-12%) rotate(16deg);
            }
            to {
                transform: translateX(240%) rotate(16deg);
            }
        }

        @media (max-width: 1280px) {
            .workspace {
                grid-template-columns: 280px minmax(0, 1fr);
            }

            .right-rail {
                grid-column: 1 / -1;
                grid-template-columns: repeat(2, minmax(0, 1fr));
            }
        }

        @media (max-width: 1080px) {
            .hero,
            .workspace,
            .right-rail {
                grid-template-columns: 1fr;
            }

            .metric-strip {
                grid-template-columns: repeat(2, minmax(0, 1fr));
            }

            .source-strip,
            .btn-row,
            .filters-grid,
            .controls-grid,
            .services-grid,
            .summary-grid,
            .feedback-grid,
            .mini-metric-grid,
            .assist-actions {
                grid-template-columns: 1fr;
            }

            .utility-actions,
            .filters-grid.filters-grid-logs {
                grid-template-columns: 1fr;
            }

            .filters-grid.filters-grid-logs .field-search {
                grid-column: auto;
            }
        }

        @media (max-width: 640px) {
            .shell {
                width: min(100vw - 16px, 100%);
                padding-top: 16px;
            }

            .hero-card,
            .hero-summary,
            .panel,
            .metric-card {
                padding: 18px;
            }

            .metric-strip {
                grid-template-columns: 1fr;
            }

            h1 {
                font-size: 42px;
            }

            .banner {
                align-items: start;
                flex-direction: column;
            }
        }
    </style>
</head>
<body>
    <div class="shell">
        <section class="hero">
            <div class="surface hero-card">
                <div>
                    <div class="eyebrow">Incident Command Surface</div>
                    <h1>Distributed incidents, without the dashboard chaos.</h1>
                    <p class="subtitle">This surface is tuned for incident response instead of generic admin work. Stronger hierarchy, severity-led color, and live observability cues make it easier to spot what needs action first while you investigate the system.</p>
                </div>
                <div class="hero-footer">
                    <div class="chip" id="task-chip">Scenario · Starter Incident</div>
                    <div class="chip">Elastic-ready observability</div>
                    <div class="chip" id="dd-note">Provider handshake in progress</div>
                </div>
            </div>

            <div class="surface hero-summary">
                <div class="summary-head">
                    <div>
                        <h2>Current Mission</h2>
                        <p>Selected scenario metadata and grading context.</p>
                    </div>
                    <div class="badge" id="task-difficulty" data-tone="warn">Easy</div>
                </div>
                <div class="mission-box">
                    <strong id="task-name">Loading task…</strong>
                    <p class="section-copy" id="task-description">Pick a task to see the incident brief and recommended response window.</p>
                </div>
                <div class="summary-grid">
                    <div class="summary-item">
                        <span class="summary-label">Max Steps</span>
                        <div class="summary-value" id="task-max-steps">15</div>
                    </div>
                    <div class="summary-item">
                        <span class="summary-label">Incidents</span>
                        <div class="summary-value" id="task-incidents">1</div>
                    </div>
                </div>
                <p class="mini-note">Reset the environment after switching tasks to load the selected scenario into the simulator.</p>
            </div>
        </section>

        <section class="metric-strip">
            <div class="surface metric-card" id="metric-env-card" data-tone="warn">
                <span class="metric-label">Environment</span>
                <div class="metric-value" id="env-status">Idle</div>
                <div class="metric-foot">Current simulator status</div>
            </div>
            <div class="surface metric-card" id="metric-observability-card" data-tone="warn">
                <span class="metric-label">Observability</span>
                <div class="metric-value" id="dd-status">Fallback</div>
                <div class="metric-foot">Observability data source</div>
            </div>
            <div class="surface metric-card" id="metric-step-card" data-tone="warn">
                <span class="metric-label">Step</span>
                <div class="metric-value" id="step-count">0</div>
                <div class="metric-foot" id="step-context">Awaiting reset</div>
            </div>
            <div class="surface metric-card" id="metric-damage-card" data-tone="good">
                <span class="metric-label">Damage</span>
                <div class="metric-value" id="damage-score">0.00</div>
                <div class="metric-foot">Lower is better</div>
            </div>
            <div class="surface metric-card" id="metric-grade-card" data-tone="warn">
                <span class="metric-label">Grade</span>
                <div class="metric-value" id="grade-score">-</div>
                <div class="metric-foot" id="grade-detail">No graded run yet</div>
            </div>
        </section>

        <section class="surface banner visible" id="statusBanner" data-kind="info">
            <div class="banner-copy">
                <strong id="statusBannerTitle">Workspace ready</strong>
                <p class="helper" id="statusBannerText">Logs prefer Elasticsearch, metrics prefer Datadog, and traces will fall back to Elastic-derived events when APM is not configured.</p>
            </div>
            <div class="badge" id="statusBannerMeta" data-tone="good">Live</div>
        </section>

        <section class="workspace">
            <div class="rail left-rail">
                <section class="surface panel">
                    <div class="panel-head">
                        <div class="panel-title">
                            <h2>Mission Control</h2>
                            <p class="panel-subtitle">Select the scenario, seed the run, and initialize the environment.</p>
                        </div>
                    </div>
                    <div class="controls-grid">
                        <div class="field">
                            <label for="taskId">Scenario</label>
                            <select id="taskId"></select>
                        </div>
                        <div class="field">
                            <label for="seed">Seed</label>
                            <input id="seed" type="number" value="0" />
                        </div>
                    </div>
                    <div class="mission-actions">
                        <button class="btn-primary" id="resetBtn">Start Incident</button>
                        <div class="utility-actions">
                            <button class="btn-secondary" id="stepBtn">Execute Action</button>
                            <button class="btn-ghost" id="refreshBtn">Refresh View</button>
                        </div>
                    </div>
                    <p class="helper mission-hint" id="mission-hint" style="margin-top: 12px;">Choose a scenario and click Start Incident to load it before executing actions.</p>
                </section>

                <section class="surface panel">
                    <div class="panel-head">
                        <div class="panel-title">
                            <h2>Action Composer</h2>
                            <p class="panel-subtitle">Only the parameters relevant to the chosen action stay emphasized.</p>
                        </div>
                    </div>
                    <div class="field field-full">
                        <label for="actionType">Response Action</label>
                        <select id="actionType"></select>
                    </div>
                    <div class="controls-grid" style="margin-top: 14px;">
                        <div class="field" id="field-service">
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
                        <div class="field" id="field-metric">
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
                        <div class="field" id="field-traceId">
                            <label for="traceId">Trace ID</label>
                            <input id="traceId" placeholder="trace_1234" />
                        </div>
                        <div class="field" id="field-incidentId">
                            <label for="incidentId">Incident ID</label>
                            <input id="incidentId" placeholder="db_connection_leak" />
                        </div>
                        <div class="field" id="field-rootCause">
                            <label for="rootCause">Root Cause</label>
                            <input id="rootCause" placeholder="bad_payment_deployment" />
                        </div>
                        <div class="field" id="field-replicas">
                            <label for="replicas">Replicas</label>
                            <input id="replicas" type="number" min="1" max="10" placeholder="5" />
                        </div>
                        <div class="field field-full" id="field-version">
                            <label for="version">Rollback Version</label>
                            <input id="version" placeholder="v1.2.0" />
                        </div>
                    </div>
                    <div class="note-box" style="margin-top: 14px;">
                        <strong id="action-heading">Action guidance</strong>
                        <p class="helper" id="action-hint">Choose an action to see which parameters matter most.</p>
                    </div>
                    <div class="action-assist" style="margin-top: 14px;">
                        <span class="eyelabel">Suggested Action</span>
                        <strong id="suggested-action-title">Start an incident</strong>
                        <p class="helper" id="suggested-action-copy">Once the scenario is active, the assistant will suggest the safest next move and prefill the composer for you.</p>
                        <p class="helper action-risk" id="action-risk">Selected action risk notes will appear here before you execute anything.</p>
                        <div class="assist-actions">
                            <button class="btn-ghost" id="applySuggestedActionBtn" type="button">Use Suggested Action</button>
                            <button class="btn-ghost" id="focusSuggestedServiceBtn" type="button">Focus Suggested Service</button>
                        </div>
                    </div>
                </section>
            </div>

            <div class="stack center-stack">
                <section class="surface panel">
                    <div class="panel-head">
                        <div class="panel-title">
                            <h2>Observability Workspace</h2>
                            <p class="panel-subtitle">Logs, metrics, and traces now share one workspace so you can stay oriented while investigating.</p>
                        </div>
                        <div class="tabs" aria-label="Observability tabs">
                            <button class="tab active" data-tab="logs">Logs</button>
                            <button class="tab" data-tab="metrics">Metrics</button>
                            <button class="tab" data-tab="traces">Traces</button>
                        </div>
                    </div>

                    <div class="source-strip">
                        <div class="source-card" id="logs-provider-card" data-tone="warn">
                            <span class="eyelabel">Logs Backend</span>
                            <strong id="logs-provider-source">Loading</strong>
                            <small id="logs-provider-detail">Waiting for the first log query.</small>
                        </div>
                        <div class="source-card" id="metrics-provider-card" data-tone="warn">
                            <span class="eyelabel">Metrics Backend</span>
                            <strong id="metrics-provider-source">Loading</strong>
                            <small id="metrics-provider-detail">Waiting for the first metric query.</small>
                        </div>
                        <div class="source-card" id="traces-provider-card" data-tone="warn">
                            <span class="eyelabel">Trace Backend</span>
                            <strong id="traces-provider-source">Loading</strong>
                            <small id="traces-provider-detail">Waiting for the first trace query.</small>
                        </div>
                        <div class="source-card" id="refresh-card" data-tone="good">
                            <span class="eyelabel">Last Refresh</span>
                            <strong id="refresh-chip">Not yet</strong>
                            <small id="refresh-detail">The observability workspace updates after each fetch.</small>
                        </div>
                    </div>

                    <div class="guide-panel">
                        <div class="guide-copy">
                            <span class="eyelabel">Recommended Next Move</span>
                            <strong id="guide-title">Start with broad logs</strong>
                            <p class="helper" id="guide-summary">Use the guide to understand which view to open first, which service to focus on, and what to verify before taking action.</p>
                        </div>
                        <div class="guide-actions">
                            <button class="btn-ghost guide-btn" id="guidePrimaryBtn" type="button">Apply Suggested Filters</button>
                            <button class="btn-ghost guide-btn" id="guideSecondaryBtn" type="button">Jump To Next View</button>
                        </div>
                        <div class="guide-steps" id="guideSteps"></div>
                    </div>

                    <div class="tab-pane active" id="pane-logs">
                        <div class="filters-grid filters-grid-logs">
                            <div class="field">
                                <label for="logPreset">Quick Filter</label>
                                <select id="logPreset">
                                    <option value="all">Everything</option>
                                    <option value="errors">Errors And Warnings</option>
                                    <option value="db">Database Issues</option>
                                    <option value="cache">Cache Issues</option>
                                    <option value="payments">Payments Issues</option>
                                    <option value="gateway">Gateway Issues</option>
                                </select>
                            </div>
                            <div class="field field-search">
                                <label for="logQuery">Query</label>
                                <input id="logQuery" value="*" placeholder="Search error text, keyword, or keep * for everything" />
                            </div>
                            <div class="field">
                                <label for="logService">Service</label>
                                <select id="logService">
                                    <option value="">Any</option>
                                    <option value="meta-pytorch-demo">meta-pytorch-demo</option>
                                    <option value="auth">auth</option>
                                    <option value="payments">payments</option>
                                    <option value="db">db</option>
                                    <option value="cache">cache</option>
                                    <option value="api_gateway">api_gateway</option>
                                </select>
                            </div>
                            <div class="field">
                                <label for="logLimit">Limit</label>
                                <input id="logLimit" type="number" min="1" max="100" value="25" />
                            </div>
                            <div class="field">
                                <label for="logMinutes">Window (minutes)</label>
                                <input id="logMinutes" type="number" min="1" max="1440" value="240" />
                            </div>
                        </div>
                        <div class="history-row" id="logHistory"></div>
                        <div class="btn-row" style="margin-top: 14px; grid-template-columns: repeat(3, minmax(0, 1fr));">
                            <button class="btn-primary" id="loadLogsBtn">Load Logs</button>
                            <button class="btn-ghost" id="seedElasticDemoBtn" type="button">Seed Demo Logs</button>
                            <button class="btn-ghost" id="loadElasticDemoBtn" type="button">Load Elastic Demo Logs</button>
                        </div>
                        <div class="note-box" style="margin-top: 14px;">
                            <strong>Log Query Context</strong>
                            <p class="helper" id="logs-note">Elastic-backed logs will appear here when the log provider is connected.</p>
                        </div>
                        <div class="stream-shell" style="margin-top: 14px;">
                            <div class="panel-head" style="margin-bottom: 14px;">
                                <div class="panel-title">
                                    <h3 style="font-size: 18px;">Log Stream</h3>
                                    <p class="panel-subtitle">Recent log events across the selected scope.</p>
                                </div>
                                <div class="badge" id="log-source" data-tone="warn">Source · loading</div>
                            </div>
                            <div class="stream" id="logs"></div>
                        </div>
                    </div>

                    <div class="tab-pane" id="pane-metrics">
                        <div class="filters-grid">
                            <div class="field">
                                <label for="metricQuery">Signal View</label>
                                <select id="metricQuery">
                                    <option value="service_overview">Service Health Snapshot</option>
                                    <option value="cpu_usage">CPU Usage</option>
                                    <option value="memory_usage">Memory Usage</option>
                                    <option value="network_traffic">Network Traffic</option>
                                    <option value="system_load">System Load</option>
                                </select>
                            </div>
                            <div class="field">
                                <label for="metricService">Service</label>
                                <select id="metricService">
                                    <option value="">Any</option>
                                    <option value="auth">auth</option>
                                    <option value="payments">payments</option>
                                    <option value="db">db</option>
                                    <option value="cache">cache</option>
                                    <option value="api_gateway">api_gateway</option>
                                </select>
                            </div>
                            <div class="field">
                                <label for="metricPoints">Points</label>
                                <input id="metricPoints" type="number" min="1" max="200" value="24" />
                            </div>
                            <div class="field">
                                <label for="metricMinutes">Window (minutes)</label>
                                <input id="metricMinutes" type="number" min="1" max="120" value="15" />
                            </div>
                        </div>
                        <div class="history-row" id="metricHistory"></div>
                        <div class="btn-row" style="margin-top: 14px; grid-template-columns: 1fr;">
                            <button class="btn-primary" id="loadMetricsBtn">Load Signal View</button>
                        </div>
                        <div class="note-box" style="margin-top: 14px;">
                            <strong>Signal View Context</strong>
                            <p class="helper" id="metrics-note">Choose a signal view like CPU or Memory instead of writing a backend metric query manually.</p>
                        </div>
                        <div class="stream-shell" style="margin-top: 14px;">
                            <div class="panel-head" style="margin-bottom: 14px;">
                                <div class="panel-title">
                                    <h3 style="font-size: 18px;">Metrics Snapshot</h3>
                                    <p class="panel-subtitle">Latest metric series or simulator fallback summaries.</p>
                                </div>
                                <div class="badge" id="metrics-source" data-tone="warn">Source · loading</div>
                            </div>
                            <div class="stream" id="metricsList"></div>
                        </div>
                    </div>

                    <div class="tab-pane" id="pane-traces">
                        <div class="filters-grid">
                            <div class="field">
                                <label for="apmMode">Trace Focus</label>
                                <select id="apmMode">
                                    <option value="suggested">Suggested Path</option>
                                    <option value="all">All Services</option>
                                    <option value="api_gateway">API Gateway Requests</option>
                                    <option value="payments">Payments Flow</option>
                                    <option value="db">Database Activity</option>
                                    <option value="auth">Authentication Flow</option>
                                    <option value="cache">Cache Path</option>
                                    <option value="service">Custom Service</option>
                                </select>
                            </div>
                            <div class="field">
                                <label for="apmService">Custom Service</label>
                                <select id="apmService">
                                    <option value="">Any</option>
                                    <option value="auth">auth</option>
                                    <option value="payments">payments</option>
                                    <option value="db">db</option>
                                    <option value="cache">cache</option>
                                    <option value="api_gateway">api_gateway</option>
                                </select>
                            </div>
                            <div class="field">
                                <label for="apmLimit">Results</label>
                                <input id="apmLimit" type="number" min="1" max="100" value="20" />
                            </div>
                            <div class="field">
                                <label for="apmMinutes">Window (minutes)</label>
                                <input id="apmMinutes" type="number" min="1" max="1440" value="240" />
                            </div>
                        </div>
                        <div class="history-row" id="traceHistory"></div>
                        <div class="btn-row" style="margin-top: 14px; grid-template-columns: 1fr;">
                            <button class="btn-primary" id="loadApmBtn">Load Trace View</button>
                        </div>
                        <div class="note-box" style="margin-top: 14px;">
                            <strong>Trace Investigation Guidance</strong>
                            <p class="helper" id="traces-note">Pick a trace focus from the dropdown to explore the request path without writing a query.</p>
                        </div>
                        <div class="stream-shell" style="margin-top: 14px;">
                            <div class="panel-head" style="margin-bottom: 14px;">
                                <div class="panel-title">
                                    <h3 style="font-size: 18px;">Trace Timeline</h3>
                                    <p class="panel-subtitle">Recent Datadog spans or Elastic-derived trace events from your current log backend.</p>
                                </div>
                                <div class="badge" id="apm-source" data-tone="warn">Source · loading</div>
                            </div>
                            <div class="stream" id="tracesList"></div>
                        </div>
                    </div>
                </section>

                <section class="surface panel">
                    <div class="panel-head">
                        <div class="panel-title">
                            <h2>Action Review</h2>
                            <p class="panel-subtitle">The latest action outcome, scoring direction, and next investigation cue all stay visible here.</p>
                        </div>
                    </div>
                    <div class="feedback-grid">
                        <div class="note-box">
                            <span class="eyelabel">Last Action Outcome</span>
                            <strong id="action-note">Choose an action, then step the environment to see the reward and outcome.</strong>
                            <p class="helper" id="action-meta">The payload sent to the environment will update here after each step.</p>
                            <div class="mini-metric-grid">
                                <div class="mini-metric-card">
                                    <span class="summary-label">Reward</span>
                                    <strong id="action-reward">-</strong>
                                </div>
                                <div class="mini-metric-card">
                                    <span class="summary-label">Action Status</span>
                                    <strong id="action-status">Waiting</strong>
                                </div>
                            </div>
                            <div class="component-list" id="action-components"></div>
                        </div>
                        <div class="state-box">
                            <strong>Damage Trajectory</strong>
                            <p class="helper" id="damage-text">System damage is idle until an environment is running.</p>
                            <div class="progress"><span id="damage-bar"></span></div>
                        </div>
                    </div>
                    <div class="score-shell" style="margin-top: 14px;">
                        <strong>Run Score Breakdown</strong>
                        <p class="helper" id="grade-summary">Start an incident to see correctness, efficiency, and damage control evolve across the run.</p>
                        <div class="score-breakdown">
                            <div class="score-row">
                                <span>Overall</span>
                                <div class="score-track"><span id="grade-score-bar"></span></div>
                                <strong id="grade-score-mini">-</strong>
                            </div>
                            <div class="score-row">
                                <span>Correctness</span>
                                <div class="score-track"><span id="grade-correctness-bar"></span></div>
                                <strong id="grade-correctness-value">-</strong>
                            </div>
                            <div class="score-row">
                                <span>Efficiency</span>
                                <div class="score-track"><span id="grade-efficiency-bar"></span></div>
                                <strong id="grade-efficiency-value">-</strong>
                            </div>
                            <div class="score-row">
                                <span>Damage</span>
                                <div class="score-track"><span id="grade-damage-bar"></span></div>
                                <strong id="grade-damage-value">-</strong>
                            </div>
                        </div>
                    </div>
                </section>
            </div>

            <div class="rail right-rail">
                <section class="surface panel">
                    <div class="panel-head">
                        <div class="panel-title">
                            <h2>Incident Snapshot</h2>
                            <p class="panel-subtitle">Active incidents, visible alerts, and current step context.</p>
                        </div>
                    </div>
                    <div class="state-box">
                        <strong>Active Incidents</strong>
                        <div class="pill-list" id="activeIncidents"></div>
                    </div>
                    <div class="state-box" style="margin-top: 14px;">
                        <strong>Alerts</strong>
                        <div class="list" id="alertsList"></div>
                    </div>
                </section>

                <section class="surface panel">
                    <div class="panel-head">
                        <div class="panel-title">
                            <h2>Investigation Timeline</h2>
                            <p class="panel-subtitle">A step-by-step narrative of what has been attempted so far in this incident.</p>
                        </div>
                    </div>
                    <div class="timeline-list" id="timelineList"></div>
                </section>

                <section class="surface panel">
                    <div class="panel-head">
                        <div class="panel-title">
                            <h2>Service Health</h2>
                            <p class="panel-subtitle">A compact service-by-service summary derived from the latest observation.</p>
                        </div>
                    </div>
                    <div class="services-grid" id="servicesGrid"></div>
                </section>

                <section class="surface panel">
                    <div class="panel-head">
                        <div class="panel-title">
                            <h2>Raw State</h2>
                            <p class="panel-subtitle">The full API payload is still available for low-level inspection.</p>
                        </div>
                    </div>
                    <pre id="stateJson">Loading state...</pre>
                </section>
            </div>
        </section>
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

        const ACTION_CONFIG = {
            query_logs: {
                label: "Inspect Logs",
                title: "Inspect service logs",
                hint: "Use this first when a service looks suspicious or you need human-readable clues from the simulated incident.",
                fields: ["service"],
            },
            query_metrics: {
                label: "Inspect Metrics",
                title: "Inspect service metrics",
                hint: "Best for checking whether latency, errors, CPU, or memory are out of family for a specific service.",
                fields: ["service", "metric"],
            },
            trace_request: {
                label: "Follow a Trace",
                title: "Follow a trace",
                hint: "Useful once you have a trace ID and want to see which service actually handled the failing request path.",
                fields: ["traceId"],
            },
            restart_service: {
                label: "Restart Service",
                title: "Restart one service",
                hint: "A fast symptom fix. It can help an unhealthy service, but restarting a healthy service is penalized.",
                fields: ["service"],
            },
            rollback_deployment: {
                label: "Rollback Deployment",
                title: "Rollback a deployment",
                hint: "Use when a bad rollout is likely. Specify the service and the target version to revert to.",
                fields: ["service", "version"],
            },
            scale_service: {
                label: "Scale Service",
                title: "Add replicas",
                hint: "Most helpful for traffic saturation scenarios, especially around the API gateway.",
                fields: ["service", "replicas"],
            },
            prioritize_incident: {
                label: "Prioritize Incident",
                title: "Prioritize an incident",
                hint: "Marks a known incident ID as the primary investigation target.",
                fields: ["incidentId"],
            },
            resolve_incident: {
                label: "Resolve Incident",
                title: "Submit a diagnosis",
                hint: "Use once you are confident in the root cause. Incorrect diagnoses cost reward and increase damage.",
                fields: ["incidentId", "rootCause"],
            },
        };

        const FIELD_IDS = ["service", "metric", "traceId", "incidentId", "rootCause", "replicas", "version"];
        let latestObservation = null;
        let latestTaskDetails = null;
        let latestSession = null;
        let latestGrade = null;
        let lastStepResult = null;
        let suggestedActionState = null;
        let environmentReady = false;
        const recentWorkspaceHistory = {
            logs: [],
            metrics: [],
            traces: [],
        };
        const el = (id) => document.getElementById(id);

        function setText(id, value) {
            const node = el(id);
            if (node) node.textContent = value;
        }

        function setHtml(id, value) {
            const node = el(id);
            if (node) node.innerHTML = value;
        }

        function setTone(id, tone = "warn") {
            const node = el(id);
            if (node) node.dataset.tone = tone;
        }

        function toneForSource(source) {
            if (source === "datadog" || source === "elasticsearch") return "good";
            if (source === "local-fallback") return "warn";
            return "bad";
        }

        function toneForDifficulty(difficulty) {
            const value = String(difficulty || "").toLowerCase();
            if (value === "easy") return "good";
            if (value === "medium") return "warn";
            if (value === "hard") return "bad";
            return "warn";
        }

        function toneForDamage(value) {
            const score = Number(value || 0);
            if (score < 0.2) return "good";
            if (score < 0.5) return "warn";
            return "bad";
        }

        function toneForGrade(value) {
            const score = Number(value);
            if (!Number.isFinite(score)) return "warn";
            if (score >= 0.75) return "good";
            if (score >= 0.4) return "warn";
            return "bad";
        }

        function toneForSeverity(value) {
            const text = String(value || "").toLowerCase();
            if (["error", "critical", "degraded", "high"].some((token) => text.includes(token))) return "bad";
            if (["warn", "warning", "medium", "active"].some((token) => text.includes(token))) return "warn";
            return "good";
        }

        function inferIncidentTone(value) {
            const text = String(value || "").toLowerCase();
            if (["db", "payment", "leak", "outage", "critical", "deploy"].some((token) => text.includes(token))) return "bad";
            if (["latency", "queue", "gateway", "cache"].some((token) => text.includes(token))) return "warn";
            return "good";
        }

        function titleCase(text) {
            return String(text || "")
                .replace(/[_-]+/g, " ")
                .replace(/\b\w/g, (match) => match.toUpperCase());
        }

        function fallbackTaskLabel(taskId) {
            const value = String(taskId || "").trim();
            if (!value) return "Unknown Scenario";
            const match = value.match(/^([a-zA-Z]+)[_-]?(\d+)$/);
            if (match) {
                const difficulty = titleCase(match[1]);
                const number = Number(match[2]) + 1;
                return `${difficulty} Scenario ${number}`;
            }
            return titleCase(value);
        }

        function taskChipLabel(task) {
            if (!task) return "Scenario · Unknown";
            const difficulty = titleCase(task.difficulty || task.task_id || "scenario");
            const name = task.name || fallbackTaskLabel(task.task_id);
            return `${difficulty} · ${name}`;
        }

        function actionLabel(actionType) {
            return ACTION_CONFIG[actionType]?.label || titleCase(actionType);
        }

        function normalizeServiceToken(value) {
            return String(value || "").toLowerCase().replaceAll(" ", "").replaceAll("_", "");
        }

        function prettyServiceName(service) {
            if (!service) return "the affected service";
            if (service === "api_gateway") return "API Gateway";
            return titleCase(service);
        }

        function apmModeLabel(mode) {
            if (mode === "suggested") return "Suggested Path";
            if (mode === "all") return "All Services";
            if (mode === "api_gateway") return "API Gateway Requests";
            if (mode === "payments") return "Payments Flow";
            if (mode === "db") return "Database Activity";
            if (mode === "auth") return "Authentication Flow";
            if (mode === "cache") return "Cache Path";
            if (mode === "service") return "Custom Service";
            return "Trace View";
        }

        function metricViewDefinition(kind) {
            const value = kind || "service_overview";
            if (value === "cpu_usage") {
                return {
                    query: "avg:system.cpu.user{*}",
                    label: "CPU Usage",
                    note: "Compare CPU usage to spot overloaded services before scaling or restarting anything.",
                };
            }
            if (value === "memory_usage") {
                return {
                    query: "avg:system.mem.used{*}",
                    label: "Memory Usage",
                    note: "Use memory usage to see whether a service is saturating or leaking over time.",
                };
            }
            if (value === "network_traffic") {
                return {
                    query: "avg:system.net.bytes_rcvd{*}",
                    label: "Network Traffic",
                    note: "Use network traffic when you want to understand request volume and traffic concentration.",
                };
            }
            if (value === "system_load") {
                return {
                    query: "avg:system.load.1{*}",
                    label: "System Load",
                    note: "System load is useful when pressure is broad and you want a high-level stress signal.",
                };
            }
            return {
                query: "avg:system.cpu.user{*}",
                label: "Service Health Snapshot",
                note: "Service Health Snapshot is the easiest first check when you want a quick read of overall service behavior.",
            };
        }

        function clampPercent(value) {
            return Math.max(0, Math.min(100, Number(value || 0)));
        }

        function trackPercent(value) {
            return `${clampPercent(value)}%`;
        }

        function normalizedLatency(value) {
            return clampPercent(Number(value || 0) / 50);
        }

        function normalizedError(value) {
            return clampPercent(Number(value || 0) * 100);
        }

        function serviceReasons(service) {
            const reasons = [];
            if (!service) return ["No service signals are available yet."];
            if (!service.is_healthy) reasons.push("Health check is failing.");
            if (Number(service.error_rate || 0) >= 0.15) reasons.push("Error rate is elevated.");
            if (Number(service.latency_ms || 0) >= 800) reasons.push("Latency is well above normal.");
            if (Number(service.cpu_percent || 0) >= 75) reasons.push("CPU pressure is high.");
            if (Number(service.memory_percent || 0) >= 80) reasons.push("Memory usage is high.");
            if (reasons.length === 0) reasons.push("Metrics look stable right now.");
            return reasons;
        }

        function serviceRiskScore(service) {
            if (!service) return 0;
            return clampPercent(Math.max(
                normalizedError(service.error_rate),
                normalizedLatency(service.latency_ms),
                Number(service.cpu_percent || 0),
                Number(service.memory_percent || 0),
            ));
        }

        function trackStyle(percent) {
            return `width:${trackPercent(percent)}`;
        }

        function gradeSummaryText(grade) {
            if (!grade) {
                return "Start an incident to see correctness, efficiency, and damage control evolve across the run.";
            }
            if (grade.score >= 0.75) {
                return "The run is on a strong path. Keep damage low and avoid unnecessary actions.";
            }
            if (grade.score >= 0.4) {
                return "The run is recoverable. Focus on accurate diagnosis and avoid noisy extra actions.";
            }
            return "The run is drifting. A precise next action matters more than taking more actions.";
        }

        function compareObservations(previous, next) {
            const before = Object.fromEntries((previous?.services_status || []).map((item) => [item.name, item]));
            const after = Object.fromEntries((next?.services_status || []).map((item) => [item.name, item]));
            const changes = [];

            Object.keys(after).forEach((serviceName) => {
                const prev = before[serviceName];
                const curr = after[serviceName];
                if (!prev || !curr) return;
                if (prev.is_healthy !== curr.is_healthy) {
                    changes.push(`${prettyServiceName(serviceName)} is now ${curr.is_healthy ? "healthy" : "degraded"}.`);
                }
                const latencyDelta = Number(curr.latency_ms || 0) - Number(prev.latency_ms || 0);
                if (Math.abs(latencyDelta) >= 150) {
                    changes.push(`${prettyServiceName(serviceName)} latency ${latencyDelta < 0 ? "improved" : "rose"} by ${formatNumber(Math.abs(latencyDelta), 0)} ms.`);
                }
                const errorDelta = Number(curr.error_rate || 0) - Number(prev.error_rate || 0);
                if (Math.abs(errorDelta) >= 0.05) {
                    changes.push(`${prettyServiceName(serviceName)} error rate ${errorDelta < 0 ? "improved" : "rose"} by ${formatNumber(Math.abs(errorDelta), 2)}.`);
                }
            });

            return changes.slice(0, 3);
        }

        function rememberWorkspaceHistory(kind, item) {
            const store = recentWorkspaceHistory[kind];
            if (!store) return;
            const key = JSON.stringify(item);
            const existingIndex = store.findIndex((entry) => JSON.stringify(entry) === key);
            if (existingIndex >= 0) store.splice(existingIndex, 1);
            store.unshift(item);
            recentWorkspaceHistory[kind] = store.slice(0, 5);
            renderWorkspaceHistory(kind);
        }

        function renderWorkspaceHistory(kind) {
            const containerMap = {
                logs: "logHistory",
                metrics: "metricHistory",
                traces: "traceHistory",
            };
            const containerId = containerMap[kind];
            const container = el(containerId);
            if (!container) return;
            const items = recentWorkspaceHistory[kind] || [];
            if (items.length === 0) {
                container.innerHTML = "";
                return;
            }
            container.innerHTML = items.map((item, index) => `
                <button class="history-chip" type="button" data-history-kind="${kind}" data-history-index="${index}">
                    ${escapeHtml(item.label)}
                </button>
            `).join("");
        }

        function restoreWorkspaceHistory(kind, index) {
            const entry = (recentWorkspaceHistory[kind] || [])[Number(index)];
            if (!entry) return;
            if (kind === "logs") {
                el("logQuery").value = entry.query || "*";
                el("logService").value = entry.service || "";
                el("logMinutes").value = String(entry.minutes || 240);
                el("logLimit").value = String(entry.limit || 25);
                loadLogs();
                return;
            }
            if (kind === "metrics") {
                el("metricQuery").value = entry.metricQuery || "service_overview";
                el("metricService").value = entry.service || "";
                el("metricMinutes").value = String(entry.minutes || 15);
                el("metricPoints").value = String(entry.points || 24);
                loadMetrics();
                return;
            }
            el("apmMode").value = entry.mode || "suggested";
            el("apmService").value = entry.service || "";
            el("apmMinutes").value = String(entry.minutes || 240);
            el("apmLimit").value = String(entry.limit || 20);
            syncApmMode();
            loadApm();
        }

        function renderActionComponents(components) {
            const container = el("action-components");
            if (!container) return;
            const entries = Object.entries(components || {});
            if (entries.length === 0) {
                container.innerHTML = '<span class="component-pill">No action components yet</span>';
                return;
            }
            container.innerHTML = entries.map(([key, value]) => `
                <span class="component-pill">${escapeHtml(titleCase(key))}: ${escapeHtml(formatNumber(value, 2))}</span>
            `).join("");
        }

        function renderGradeBreakdown(grade) {
            latestGrade = grade || null;
            const values = grade ? {
                score: clampPercent(Number(grade.score || 0) * 100),
                correctness: clampPercent(Number(grade.correctness || 0) * 100),
                efficiency: clampPercent(Number(grade.efficiency || 0) * 100),
                damage: clampPercent(Number(grade.damage || 0) * 100),
            } : {
                score: 0,
                correctness: 0,
                efficiency: 0,
                damage: 0,
            };

            el("grade-score-bar").style.width = trackPercent(values.score);
            el("grade-correctness-bar").style.width = trackPercent(values.correctness);
            el("grade-efficiency-bar").style.width = trackPercent(values.efficiency);
            el("grade-damage-bar").style.width = trackPercent(values.damage);
            setText("grade-score-mini", grade ? formatNumber(grade.score, 2) : "-");
            setText("grade-correctness-value", grade ? formatNumber(grade.correctness, 2) : "-");
            setText("grade-efficiency-value", grade ? formatNumber(grade.efficiency, 2) : "-");
            setText("grade-damage-value", grade ? formatNumber(grade.damage, 2) : "-");
            setText("grade-summary", gradeSummaryText(grade));
        }

        function renderTimeline(session) {
            const container = el("timelineList");
            if (!container) return;
            if (!session?.environment_ready) {
                container.innerHTML = `<div class="empty-state"><div><strong>No timeline yet</strong>Select a scenario and start the incident to begin building the investigation timeline.</div></div>`;
                return;
            }

            const taskName = session.task_name || fallbackTaskLabel(session.task_id);
            const events = [
                {
                    tone: "good",
                    step: 0,
                    title: `Incident started: ${taskName}`,
                    copy: `The environment is live with a ${String(session.difficulty || "unknown")} scenario and ${session.max_steps || 0} total steps.`,
                    tag: "Start",
                }
            ];

            (session.actions_log || []).forEach((action) => {
                const tone = action.action_type === "resolve_incident" ? "good" : action.action_type === "rollback_deployment" ? "warn" : "info";
                const target = action.service || action.incident_id || action.root_cause || "system";
                events.push({
                    tone: tone === "info" ? "warn" : tone,
                    step: action.step,
                    title: `${actionLabel(action.action_type)} on ${prettyServiceName(target)}`,
                    copy: `Action payload: ${Object.entries(action).filter(([key, value]) => !["step", "action_type"].includes(key) && value != null).map(([key, value]) => `${titleCase(key)} ${value}`).join(" · ") || "No extra parameters"}`,
                    tag: `Step ${action.step}`,
                });
            });

            (session.resolved_incidents || []).forEach((incident) => {
                events.push({
                    tone: "good",
                    step: session.current_step,
                    title: `${titleCase(incident)} marked resolved`,
                    copy: "The environment has recorded this incident as resolved.",
                    tag: "Resolved",
                });
            });

            (session.incorrect_resolutions || []).forEach((incident) => {
                events.push({
                    tone: "bad",
                    step: session.current_step,
                    title: `${titleCase(incident)} was submitted incorrectly`,
                    copy: "An incorrect diagnosis increases damage and usually hurts the final score.",
                    tag: "Miss",
                });
            });

            const orderedEvents = events.sort((left, right) => Number(left.step || 0) - Number(right.step || 0));
            container.innerHTML = orderedEvents.map((event) => `
                <div class="timeline-item">
                    <span class="timeline-dot" data-tone="${event.tone === "good" ? "good" : event.tone === "bad" ? "bad" : "warn"}"></span>
                    <div class="timeline-card">
                        <div class="timeline-head">
                            <span>${escapeHtml(event.tag)}</span>
                            <span>${escapeHtml(`Step ${event.step}`)}</span>
                        </div>
                        <div class="timeline-title">${escapeHtml(event.title)}</div>
                        <div class="timeline-copy">${escapeHtml(event.copy)}</div>
                    </div>
                </div>
            `).join("");
        }

        function buildSuggestedAction() {
            const observation = latestObservation || {};
            const incidents = observation.active_incidents || [];
            const alerts = observation.alerts || [];
            const focusService = focusServiceFromObservation(observation);
            const focusServiceName = prettyServiceName(focusService);
            const prioritized = latestSession?.prioritized_incidents || [];

            if (!environmentReady) {
                return {
                    title: "Start the selected incident",
                    copy: "Load the scenario first so the workspace, action composer, and timeline all have live state to work from.",
                    actionType: "__start__",
                    focusService: "",
                    fields: {},
                };
            }

            if (incidents[0] && !prioritized.includes(incidents[0])) {
                return {
                    title: `Prioritize ${titleCase(incidents[0])}`,
                    copy: "Mark the current incident as your investigation target first so the rest of the workflow has a clear anchor.",
                    actionType: "prioritize_incident",
                    focusService,
                    fields: { incidentId: incidents[0] },
                };
            }

            if (focusService && alerts.length > 0) {
                return {
                    title: `Inspect ${focusServiceName} logs`,
                    copy: `${focusServiceName} is the most suspicious service right now. Start with logs before taking a disruptive action.`,
                    actionType: "query_logs",
                    focusService,
                    fields: { service: focusService },
                };
            }

            if (focusService) {
                const targetService = (observation.services_status || []).find((service) => service.name === focusService);
                const metric = Number(targetService?.cpu_percent || 0) > 70 ? "cpu" : "error_rate";
                return {
                    title: `Validate ${focusServiceName} pressure`,
                    copy: "Check one clear metric signal before you restart, scale, or diagnose the incident.",
                    actionType: "query_metrics",
                    focusService,
                    fields: { service: focusService, metric },
                };
            }

            return {
                title: "Inspect broad logs",
                copy: "When nothing stands out yet, start broad and narrow only after you see repeated failures or warnings.",
                actionType: "query_logs",
                focusService: "",
                fields: { service: "" },
            };
        }

        function riskCopyForSelectedAction() {
            const action = el("actionType")?.value || "";
            const serviceName = el("service")?.value || "";
            const targetService = (latestObservation?.services_status || []).find((service) => service.name === serviceName);

            if (!environmentReady) {
                return "No action can run until the selected incident is started.";
            }
            if (action === "restart_service" && targetService?.is_healthy) {
                return `${prettyServiceName(serviceName)} currently looks healthy. Restarting it is likely to add damage instead of helping.`;
            }
            if (action === "rollback_deployment" && !serviceName) {
                return "Rollbacks are powerful but disruptive. Only use one when you have a strong deployment clue.";
            }
            if (action === "resolve_incident") {
                return "Resolve Incident should be your last move, not your first one. Make sure the evidence is consistent.";
            }
            if (action === "scale_service") {
                return "Scaling helps saturation problems, but it rarely fixes a bad deployment or broken dependency.";
            }
            return "This action looks safe if the selected fields match the service or incident you have actually investigated.";
        }

        function updateActionAssistant() {
            suggestedActionState = buildSuggestedAction();
            setText("suggested-action-title", suggestedActionState.title);
            setText("suggested-action-copy", suggestedActionState.copy);
            setText("action-risk", riskCopyForSelectedAction());
        }

        function applySuggestedAction() {
            if (!suggestedActionState) {
                updateActionAssistant();
            }
            if (!suggestedActionState) return;
            if (suggestedActionState.actionType === "__start__") {
                resetEnvironment();
                return;
            }

            el("actionType").value = suggestedActionState.actionType;
            syncActionFields();
            Object.entries(suggestedActionState.fields || {}).forEach(([key, value]) => {
                if (el(key)) {
                    el(key).value = value ?? "";
                }
            });
            setBanner("info", "Suggested action prepared", "The action composer was prefilled with the current recommended next step.", "Assistant");
        }

        function focusSuggestedService() {
            const service = suggestedActionState?.focusService || focusServiceFromObservation(latestObservation || {});
            if (!service) {
                setBanner("warn", "No focus service yet", "There is not enough signal yet to focus a single service. Start with a broad log view instead.", "Assistant");
                return;
            }
            el("logService").value = service;
            el("metricService").value = service;
            el("apmMode").value = "service";
            el("apmService").value = service;
            syncApmMode();
            setBanner("info", "Focus service applied", `${prettyServiceName(service)} is now preselected across logs, metrics, and traces.`, "Assistant");
        }

        function detectServiceFromText(text) {
            const source = normalizeServiceToken(text);
            const services = ["auth", "payments", "db", "cache", "api_gateway"];
            return services.find((service) => {
                const token = normalizeServiceToken(service);
                return source.includes(token) || (service === "api_gateway" && source.includes("gateway"));
            }) || null;
        }

        function focusServiceFromObservation(observation) {
            const alerts = observation?.alerts || [];
            const services = observation?.services_status || [];
            const incidents = observation?.active_incidents || [];

            const alertService = alerts.find((alert) => alert?.service)?.service;
            if (alertService) return alertService;

            const degradedService = services.find((service) => !service?.is_healthy)?.name;
            if (degradedService) return degradedService;

            const incidentService = incidents.map(detectServiceFromText).find(Boolean);
            return incidentService || "";
        }

        function syncApmMode() {
            const mode = el("apmMode")?.value || "suggested";
            const serviceInput = el("apmService");
            const focusService = focusServiceFromObservation(latestObservation || {});

            if (serviceInput) {
                serviceInput.disabled = mode !== "service";
                if (mode === "suggested" && focusService) {
                    serviceInput.value = focusService;
                }
            }

            if (mode === "suggested") {
                setText("traces-note", focusService
                    ? `Suggested Path will focus trace exploration on ${prettyServiceName(focusService)} based on the latest alerts and service health.`
                    : "Suggested Path will start with a broad trace search because no obvious focus service is visible yet.");
            } else if (mode === "all") {
                setText("traces-note", "All Services is useful when you want a broad request-path view before narrowing the investigation.");
            } else {
                const label = apmModeLabel(mode);
                if (mode === "service") {
                    setText("traces-note", "Custom Service is best when you already know which system area you want to inspect more closely.");
                } else {
                    setText("traces-note", `${label} narrows the trace view to a common investigation path without needing a raw query.`);
                }
            }
        }

        function buildApmRequest() {
            const mode = el("apmMode")?.value || "suggested";
            const focusService = focusServiceFromObservation(latestObservation || {});
            const selectedService = el("apmService").value || "";
            const limit = Number(el("apmLimit").value || 20);
            const minutes = Number(el("apmMinutes").value || 240);

            if (mode === "all") {
                return {
                    mode,
                    query: "service:*",
                    service: "",
                    limit,
                    minutes,
                    description: "Tracing across all services to find the failing request path.",
                };
            }

            if (mode === "service") {
                const service = selectedService || focusService || "";
                return {
                    mode,
                    query: service ? `service:${service}` : "service:*",
                    service,
                    limit,
                    minutes,
                    description: service
                        ? `Tracing ${prettyServiceName(service)} to understand its request path.`
                        : "Tracing broadly because no service focus is selected yet.",
                };
            }

            if (["api_gateway", "payments", "db", "auth", "cache"].includes(mode)) {
                return {
                    mode,
                    query: `service:${mode}`,
                    service: mode,
                    limit,
                    minutes,
                    description: `Tracing ${prettyServiceName(mode)} so you can inspect a common investigation path quickly.`,
                };
            }

            const service = focusService || "";
            return {
                mode,
                query: service ? `service:${service}` : "service:*",
                service,
                limit,
                minutes,
                description: service
                    ? `Using the suggested focus service ${prettyServiceName(service)} from the latest observation.`
                    : "Using a broad trace search because no obvious focus service is visible yet.",
            };
        }

        function renderGuideSteps(steps) {
            const items = (steps || []).map((step, index) => `
                <div class="guide-step">
                    <span class="guide-step-index">${index + 1}</span>
                    <div>${escapeHtml(step)}</div>
                </div>
            `).join("");
            setHtml("guideSteps", items || `<div class="guide-step"><span class="guide-step-index">1</span><div>Reset a scenario to begin the guided investigation flow.</div></div>`);
        }

        function setGuide(primaryLabel, secondaryLabel) {
            setText("guidePrimaryBtn", primaryLabel);
            setText("guideSecondaryBtn", secondaryLabel);
        }

        function setEnvironmentReady(ready) {
            environmentReady = Boolean(ready);
            const stepBtn = el("stepBtn");
            const resetBtn = el("resetBtn");
            if (stepBtn) {
                stepBtn.disabled = !environmentReady;
                stepBtn.textContent = "Execute Action";
            }
            if (resetBtn) {
                resetBtn.textContent = environmentReady ? "Restart Incident" : "Start Incident";
            }
            setText(
                "mission-hint",
                environmentReady
                    ? "Incident is active. Investigate the signals, then execute actions from the composer."
                    : "Choose a scenario and click Start Incident before executing actions."
            );
        }

        function friendlyErrorMessage(message) {
            const text = String(message || "").trim();
            if (!text) return "Something went wrong while loading this view.";
            if (text.includes("Environment not initialized")) {
                return "Reset the selected scenario before running a response action.";
            }
            if (text.includes("Environment not initialized.")) {
                return "Reset the selected scenario before viewing run-specific data.";
            }
            return text;
        }

        function updateWorkspaceGuide() {
            const active = activeTabName();
            const observation = latestObservation || {};
            const focusService = focusServiceFromObservation(observation);
            const focusServiceName = prettyServiceName(focusService);
            const activeIncidents = observation.active_incidents || [];
            const hasIncidents = activeIncidents.length > 0;
            const incidentName = hasIncidents ? activeIncidents[0] : "the current incident";

            if (active === "metrics") {
                setText("guide-title", focusService ? `Validate pressure on ${focusServiceName}` : "Validate pressure across services");
                setText("guide-summary", focusService
                    ? `Use metrics to confirm whether ${focusServiceName} is actually overloaded before you restart, scale, or diagnose anything.`
                    : "Use metrics to identify which service is showing pressure before you narrow the investigation.");
                renderGuideSteps([
                    focusService ? `Filter metrics to ${focusServiceName} and compare CPU, memory, latency, and error rate.` : "Start with all services, then narrow to the first unhealthy or noisy service.",
                    "Look for one signal that is clearly out of family rather than reacting to every spike.",
                    "If you find a suspicious service, jump to traces or logs to verify the request path and error context."
                ]);
                setGuide(focusService ? `Focus ${focusServiceName}` : "Load All Services", "Jump To Traces");
                return;
            }

            if (active === "traces") {
                setText("guide-title", focusService ? `Trace the failing path through ${focusServiceName}` : "Trace the failing request path");
                setText("guide-summary", focusService
                    ? `Use traces to understand whether ${focusServiceName} is the source of the problem or only downstream from it.`
                    : "Use traces when you need to see which service in the request path is actually creating the failure.");
                renderGuideSteps([
                    focusService ? `Filter traces to ${focusServiceName} and look for repeated operations or resources.` : "Start with a broad service query and look for repeated operations or resources.",
                    "Use the trace timeline to separate the symptom service from the actual source of failure.",
                    hasIncidents ? `Return to the action area only after you can explain why ${incidentName} is happening.` : "Return to the action area only after you can explain the failing path clearly."
                ]);
                setGuide(focusService ? `Trace ${focusServiceName}` : "Load Broad Traces", "Prepare Response Action");
                return;
            }

            setText("guide-title", focusService ? `Start with ${focusServiceName} logs` : "Start with broad logs");
            setText("guide-summary", focusService
                ? `Logs are usually the fastest way to understand why ${focusServiceName} looks suspicious right now.`
                : "Logs are the best first stop when you need fast, human-readable clues before narrowing the scope.");
            renderGuideSteps([
                focusService ? `Filter the stream to ${focusServiceName} and read repeated warnings or errors first.` : "Keep the query broad for the first pass and scan for repeated warnings or errors.",
                "Note the exact service, error wording, or trace id that shows up more than once.",
                "Once you have a strong clue, jump to metrics to validate pressure or traces to verify the request path."
            ]);
            setGuide(focusService ? `Filter To ${focusServiceName}` : "Use Broad Logs", "Jump To Metrics");
        }

        async function runGuidePrimary() {
            const active = activeTabName();
            const focusService = focusServiceFromObservation(latestObservation || {});

            if (active === "metrics") {
                el("metricService").value = focusService || "";
                el("metricMinutes").value = "30";
                el("metricPoints").value = "24";
                el("metricQuery").value = "cpu_usage";
                await loadMetrics();
                return;
            }

            if (active === "traces") {
                el("apmMode").value = focusService ? "service" : "suggested";
                el("apmService").value = focusService || "";
                el("apmMinutes").value = "240";
                el("apmLimit").value = "20";
                syncApmMode();
                await loadApm();
                return;
            }

            el("logService").value = focusService || "";
            el("logQuery").value = "*";
            el("logMinutes").value = "240";
            el("logLimit").value = "25";
            await loadLogs();
        }

        async function runGuideSecondary() {
            const active = activeTabName();
            const observation = latestObservation || {};

            if (active === "metrics") {
                activateTab("traces");
                updateWorkspaceGuide();
                await loadApm();
                return;
            }

            if (active === "traces") {
                const incidents = observation.active_incidents || [];
                el("actionType").value = incidents.length > 0 ? "prioritize_incident" : "query_logs";
                if (incidents.length > 0) {
                    el("incidentId").value = incidents[0];
                }
                syncActionFields();
                setBanner("info", "Response action prepared", "The action composer was updated with a safer next step based on the current investigation context.", "Guide");
                return;
            }

            activateTab("metrics");
            updateWorkspaceGuide();
            await loadMetrics();
        }

        function setBanner(kind, title, detail, meta = "Live") {
            const banner = el("statusBanner");
            if (!banner) return;
            banner.classList.add("visible");
            banner.dataset.kind = kind || "info";
            setText("statusBannerTitle", title);
            setText("statusBannerText", detail);
            setText("statusBannerMeta", meta);
            setTone("statusBannerMeta", kind === "error" ? "bad" : kind === "warn" ? "warn" : "good");
        }

        function formatNumber(value, digits = 2) {
            const num = Number(value ?? 0);
            return Number.isFinite(num) ? num.toFixed(digits) : "0.00";
        }

        async function fetchJson(url, options) {
            const res = await fetch(url, options);
            if (!res.ok) {
                const text = await res.text();
                let message = text || `Request failed: ${res.status}`;
                try {
                    const payload = JSON.parse(text);
                    message = payload.detail || payload.message || message;
                } catch (error) {
                    // Keep raw text when the response is not JSON.
                }
                throw new Error(friendlyErrorMessage(message));
            }
            return res.json();
        }

        function escapeHtml(text) {
            return String(text ?? "")
                .replaceAll("&", "&amp;")
                .replaceAll("<", "&lt;")
                .replaceAll(">", "&gt;")
                .replaceAll('"', "&quot;")
                .replaceAll("'", "&#039;");
        }

        function renderEmpty(containerId, message) {
            const parts = String(message || "").split("||");
            const title = parts[0] || "Nothing to show yet";
            const copy = parts[1] || "";
            setHtml(containerId, `<div class="empty-state"><div><strong>${escapeHtml(title)}</strong>${escapeHtml(copy)}</div></div>`);
        }

        function prettySourceLabel(source) {
            if (source === "datadog") return "Datadog";
            if (source === "elasticsearch") return "Elasticsearch";
            if (source === "local-fallback") return "Simulator fallback";
            return source || "Unknown";
        }

        function applyLogPreset() {
            const preset = el("logPreset")?.value || "all";
            if (preset === "errors") {
                el("logQuery").value = "error OR warn OR failed OR timeout";
                el("logService").value = "";
                return;
            }
            if (preset === "db") {
                el("logQuery").value = "database OR connection OR pool OR query";
                el("logService").value = "db";
                return;
            }
            if (preset === "cache") {
                el("logQuery").value = "cache OR redis OR miss OR eviction";
                el("logService").value = "cache";
                return;
            }
            if (preset === "payments") {
                el("logQuery").value = "payment OR checkout OR transaction OR decline";
                el("logService").value = "payments";
                return;
            }
            if (preset === "gateway") {
                el("logQuery").value = "gateway OR timeout OR upstream OR route";
                el("logService").value = "api_gateway";
                return;
            }
            el("logQuery").value = "*";
            el("logService").value = "";
        }

        function updateObservabilityStatus(source) {
            let value = "Fallback";
            let state = "warn";

            if (source === "datadog") {
                value = "Datadog";
                state = "good";
            } else if (source === "elasticsearch") {
                value = "Elastic";
                state = "good";
            } else if (source === "local-fallback") {
                value = "Fallback";
                state = "warn";
            } else {
                value = "Unknown";
                state = "bad";
            }

            setText("dd-status", value);
            el("dd-status").dataset.state = state;
            setTone("metric-observability-card", toneForSource(source));
        }

        function updateProviderCard(kind, source, detail) {
            setText(`${kind}-provider-source`, prettySourceLabel(source));
            setText(`${kind}-provider-detail`, detail || "No provider detail available.");
            setTone(`${kind}-provider-card`, toneForSource(source));
        }

        function updateRefreshStamp(reason = "Updated") {
            const now = new Date();
            setText("refresh-chip", now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }));
            setText("refresh-detail", reason);
            setTone("refresh-card", "good");
        }

        async function loadObservabilityStatus() {
            try {
                const status = await fetchJson("/api/observability/status");
                const isConnected = Boolean(status.connected);
                const isConfigured = Boolean(status.configured);
                const source = status.source || status.backend || "local-fallback";

                if (isConnected || isConfigured) {
                    updateObservabilityStatus(source);
                } else {
                    updateObservabilityStatus("local-fallback");
                }

                const indexes = Array.isArray(status.log_indexes) && status.log_indexes.length > 0
                    ? ` | indexes: ${status.log_indexes.join(", ")}`
                    : "";
                const elasticIndex = status.index ? ` | index: ${status.index}` : "";
                const location = status.site || status.url || "local";
                setText("dd-note", `${status.message || "Observability status unavailable."} (${location})${indexes}${elasticIndex}`);
                setBanner(
                    isConnected ? "info" : "warn",
                    isConnected ? "External observability is connected" : "Observability is partially configured",
                    status.message || "The dashboard is reporting provider status.",
                    prettySourceLabel(source)
                );
            } catch (error) {
                setText("dd-status", "Unknown");
                el("dd-status").dataset.state = "bad";
                setText("dd-note", "Unable to determine observability status.");
                setBanner("error", "Observability status failed", String(error.message || error), "Error");
            }
        }

        function updateDamageBar(value) {
            const pct = Math.max(0, Math.min(100, Number(value || 0) * 100));
            el("damage-bar").style.width = `${pct}%`;
        }

        function activateTab(tabName) {
            document.querySelectorAll(".tab").forEach((button) => {
                button.classList.toggle("active", button.dataset.tab === tabName);
            });
            document.querySelectorAll(".tab-pane").forEach((pane) => {
                pane.classList.toggle("active", pane.id === `pane-${tabName}`);
            });
        }

        function populateActions() {
            const select = el("actionType");
            select.innerHTML = "";
            ACTIONS.forEach((action) => {
                const option = document.createElement("option");
                option.value = action;
                option.textContent = actionLabel(action);
                select.appendChild(option);
            });
        }

        function syncActionFields() {
            const action = el("actionType").value;
            const config = ACTION_CONFIG[action] || { title: "Action guidance", hint: "Choose an action.", fields: [] };

            setText("action-heading", config.title);
            setText("action-hint", config.hint);

            FIELD_IDS.forEach((fieldId) => {
                const wrapper = el(`field-${fieldId}`);
                const input = el(fieldId);
                const isActive = config.fields.includes(fieldId);
                if (wrapper) wrapper.classList.toggle("field-muted", !isActive);
                if (input) input.disabled = !isActive;
            });

            updateWorkspaceGuide();
            updateActionAssistant();
        }

        function getEnabledValue(id, numeric = false) {
            const node = el(id);
            if (!node || node.disabled || node.value === "") return null;
            return numeric ? Number(node.value) : node.value;
        }

        function missingActionFields() {
            const action = el("actionType").value;
            const config = ACTION_CONFIG[action] || { fields: [] };
            return config.fields.filter((fieldId) => {
                const node = el(fieldId);
                if (!node || node.disabled) return false;
                return node.value === "" || node.value == null;
            });
        }

        function friendlyFieldLabel(fieldId) {
            const labels = {
                service: "service",
                metric: "metric",
                traceId: "trace ID",
                incidentId: "incident ID",
                rootCause: "root cause",
                replicas: "replica count",
                version: "rollback version",
            };
            return labels[fieldId] || titleCase(fieldId);
        }

        function renderLogs(logs) {
            const container = el("logs");
            container.innerHTML = "";
            if (!logs || logs.length === 0) {
                renderEmpty("logs", "No logs found||Try a broader window, switch the quick filter, or clear the service filter.");
                return;
            }

            logs.forEach((log) => {
                const level = String(log.level || "INFO").toUpperCase();
                const tone = toneForSeverity(level);
                const node = document.createElement("div");
                node.className = `stream-card level-${level}`;
                node.dataset.tone = tone;
                node.innerHTML = `
                    <div class="stream-top">
                        <span>${escapeHtml(log.timestamp ?? "-")}</span>
                        <span>${escapeHtml(log.service ?? "unknown")}</span>
                        <span>${escapeHtml(level)}</span>
                    </div>
                    <div class="stream-body">${escapeHtml(log.message ?? "")}</div>
                    <div class="stream-tags">
                        <span class="log-pill">${escapeHtml(level)}</span>
                        <span class="log-pill">${escapeHtml(log.service ?? "service")}</span>
                        ${log.trace_id ? `<span class="log-pill">trace ${escapeHtml(log.trace_id)}</span>` : ""}
                        ${log.service ? `<button class="log-pill log-action" type="button" data-pivot-service="${escapeHtml(log.service)}">Focus service</button>` : ""}
                        ${log.trace_id ? `<button class="log-pill log-action" type="button" data-pivot-trace="${escapeHtml(log.trace_id)}">Trace request</button>` : ""}
                    </div>
                `;
                container.appendChild(node);
            });
        }

        function renderMetrics(series) {
            const container = el("metricsList");
            container.innerHTML = "";
            if (!series || series.length === 0) {
                renderEmpty("metricsList", "No metrics found||Try a broader time window or switch the signal view.");
                return;
            }

            series.forEach((item) => {
                const node = document.createElement("div");
                node.className = "stream-card level-INFO";
                node.dataset.tone = "good";
                const title = item.display_name || item.metric || prettyServiceName(item.service) || "metric";
                const summary = item.last_value != null
                    ? `last value ${formatNumber(item.last_value, 4)} across ${item.point_count ?? 0} points`
                    : `latency ${formatNumber(item.latency_ms, 1)} ms · error ${formatNumber(item.error_rate, 3)} · cpu ${formatNumber(item.cpu_percent, 1)}% · mem ${formatNumber(item.memory_percent, 1)}%`;
                node.innerHTML = `
                    <div class="stream-top">
                        <span>${escapeHtml(title)}</span>
                        <span>${escapeHtml(item.scope || item.service || "")}</span>
                    </div>
                    <div class="stream-body">${escapeHtml(summary)}</div>
                    ${item.service ? `<div class="stream-tags"><button class="log-pill log-action" type="button" data-metric-service="${escapeHtml(item.service)}">Investigate ${escapeHtml(item.service)}</button></div>` : ""}
                `;
                container.appendChild(node);
            });
        }

        function renderTraces(traces) {
            const container = el("tracesList");
            container.innerHTML = "";
            if (!traces || traces.length === 0) {
                renderEmpty("tracesList", "No traces found||Try All Services or widen the trace window.");
                return;
            }

            traces.forEach((trace) => {
                const node = document.createElement("div");
                node.className = "stream-card level-WARN";
                node.dataset.tone = trace.duration_ms != null ? "warn" : "good";
                const durationValue = trace.duration_ms != null ? `${escapeHtml(trace.duration_ms)} ms` : "derived";
                node.innerHTML = `
                    <div class="stream-top">
                        <span>${escapeHtml(trace.timestamp ?? "-")}</span>
                        <span>${escapeHtml(trace.service ?? "unknown")}</span>
                    </div>
                    <div class="stream-body">${escapeHtml(trace.operation || "operation")} → ${escapeHtml(trace.resource || "resource")}</div>
                    <div class="stream-top" style="margin-top: 10px;">
                        <span>duration</span>
                        <span>${durationValue}</span>
                    </div>
                    <div class="stream-tags">
                        ${trace.service ? `<button class="log-pill log-action" type="button" data-trace-service="${escapeHtml(trace.service)}">Focus ${escapeHtml(trace.service)}</button>` : ""}
                        ${trace.trace_id ? `<button class="log-pill log-action" type="button" data-pivot-trace="${escapeHtml(trace.trace_id)}">Use trace ID</button>` : ""}
                    </div>
                `;
                container.appendChild(node);
            });
        }

        function renderIncidents(incidents) {
            const container = el("activeIncidents");
            container.innerHTML = "";
            if (!incidents || incidents.length === 0) {
                container.innerHTML = '<div class="helper">No active incidents visible.</div>';
                return;
            }

            incidents.forEach((incident) => {
                const node = document.createElement("div");
                node.className = "incident-pill";
                node.dataset.tone = inferIncidentTone(incident);
                node.textContent = incident;
                container.appendChild(node);
            });
        }

        function renderAlerts(alerts) {
            const container = el("alertsList");
            container.innerHTML = "";
            if (!alerts || alerts.length === 0) {
                container.innerHTML = '<div class="helper">No active alerts in the current observation.</div>';
                return;
            }

            alerts.forEach((alert) => {
                const node = document.createElement("div");
                node.className = "alert-item";
                node.dataset.severity = toneForSeverity(alert.severity || alert.message || "");
                node.innerHTML = `
                    <div class="alert-head">
                        <span>${escapeHtml(alert.service || "service")}</span>
                        <span>${escapeHtml(alert.severity || "info")}</span>
                    </div>
                    <div>${escapeHtml(alert.message || "")}</div>
                `;
                container.appendChild(node);
            });
        }

        function renderServices(services) {
            const container = el("servicesGrid");
            container.innerHTML = "";
            if (!services || services.length === 0) {
                container.innerHTML = '<div class="empty-state"><div><strong>No service health yet</strong>Start the incident to see which services look stable, pressured, or degraded.</div></div>';
                return;
            }

            services.forEach((service) => {
                const node = document.createElement("div");
                const healthy = Boolean(service.is_healthy);
                const pressure = serviceRiskScore(service);
                const reason = serviceReasons(service)[0];
                node.className = "service-card";
                node.dataset.health = healthy ? "healthy" : "degraded";
                node.innerHTML = `
                    <div class="service-top">
                        <div class="service-name">${escapeHtml(service.name)}</div>
                        <div class="service-status ${healthy ? "healthy" : "degraded"}">${healthy ? "healthy" : "degraded"}</div>
                    </div>
                    <div class="service-summary">${escapeHtml(reason)} Risk score ${formatNumber(pressure, 0)}.</div>
                    <div class="mini-grid">
                        <div class="mini-row"><span>Latency</span><span>${formatNumber(service.latency_ms, 1)} ms</span></div>
                        <div class="mini-row"><span>Error rate</span><span>${formatNumber(service.error_rate, 2)}</span></div>
                        <div class="mini-row"><span>CPU</span><span>${formatNumber(service.cpu_percent, 1)}%</span></div>
                        <div class="mini-row"><span>Memory</span><span>${formatNumber(service.memory_percent, 1)}%</span></div>
                    </div>
                    <div class="service-metric-stack">
                        <div class="metric-track-row"><span>Latency</span><div class="metric-track"><span style="${trackStyle(normalizedLatency(service.latency_ms))}"></span></div><span>${formatNumber(service.latency_ms, 0)}</span></div>
                        <div class="metric-track-row"><span>Error</span><div class="metric-track"><span style="${trackStyle(normalizedError(service.error_rate))}"></span></div><span>${formatNumber(Number(service.error_rate || 0) * 100, 0)}%</span></div>
                        <div class="metric-track-row"><span>CPU</span><div class="metric-track"><span style="${trackStyle(service.cpu_percent)}"></span></div><span>${formatNumber(service.cpu_percent, 0)}%</span></div>
                        <div class="metric-track-row"><span>Memory</span><div class="metric-track"><span style="${trackStyle(service.memory_percent)}"></span></div><span>${formatNumber(service.memory_percent, 0)}%</span></div>
                    </div>
                    <div class="service-meter"><span style="width:${Math.max(8, Math.min(100, pressure))}%"></span></div>
                `;
                container.appendChild(node);
            });
        }

        async function refreshTasks() {
            const data = await fetchJson("/tasks");
            const taskSelect = el("taskId");
            taskSelect.innerHTML = "";
            const taskDetails = await Promise.all(
                (data.tasks || []).map(async (taskId) => {
                    try {
                        return await fetchJson(`/tasks/${taskId}`);
                    } catch (error) {
                        return { task_id: taskId, difficulty: taskId.split("_")[0], name: fallbackTaskLabel(taskId) };
                    }
                })
            );

            taskDetails.forEach((task) => {
                const option = document.createElement("option");
                option.value = task.task_id;
                option.textContent = taskChipLabel(task);
                taskSelect.appendChild(option);
            });
            taskSelect.value = "easy_0";
        }

        async function loadTaskDetails(taskId) {
            try {
                const task = await fetchJson(`/tasks/${taskId}`);
                latestTaskDetails = task;
                setText("task-name", task.name || fallbackTaskLabel(taskId));
                setText("task-description", task.description || "No task description available.");
                setText("task-max-steps", String(task.max_steps ?? "-"));
                setText("task-incidents", String(task.num_incidents ?? "-"));
                setText("task-difficulty", String(task.difficulty || "unknown").toUpperCase());
                setText("task-chip", taskChipLabel(task));
                setTone("task-difficulty", toneForDifficulty(task.difficulty));
            } catch (error) {
                latestTaskDetails = null;
                setText("task-name", fallbackTaskLabel(taskId));
                setText("task-description", "Task metadata is unavailable.");
                setText("task-max-steps", "-");
                setText("task-incidents", "-");
                setText("task-difficulty", "UNKNOWN");
                setText("task-chip", `Scenario · ${fallbackTaskLabel(taskId)}`);
                setTone("task-difficulty", "bad");
            }
        }

        async function loadSession() {
            try {
                const session = await fetchJson("/api/session");
                latestSession = session;
                renderTimeline(session);
                updateActionAssistant();
            } catch (error) {
                latestSession = null;
                renderTimeline(null);
            }
        }

        async function loadGrade() {
            try {
                const grade = await fetchJson("/grade");
                setText("grade-score", formatNumber(grade.score, 3));
                setText("grade-detail", `correctness ${formatNumber(grade.correctness, 2)} · efficiency ${formatNumber(grade.efficiency, 2)} · damage ${formatNumber(grade.damage, 2)}`);
                setTone("metric-grade-card", toneForGrade(grade.score));
                renderGradeBreakdown(grade);
            } catch (error) {
                setText("grade-score", "-");
                setText("grade-detail", "Grade available after a run starts");
                setTone("metric-grade-card", "warn");
                renderGradeBreakdown(null);
            }
        }

        async function loadState() {
            try {
                const data = await fetchJson("/state");
                const observation = data.observation || {};
                latestObservation = observation;

                el("stateJson").textContent = JSON.stringify(data, null, 2);
                setText("step-count", String(data.current_step ?? 0));
                setText("step-context", `${data.current_step ?? 0} of ${data.max_steps ?? "-"}`);
                setText("damage-score", formatNumber(data.damage_score, 2));
                setText("env-status", data.done ? "Resolved" : "Active");
                el("env-status").dataset.state = data.done ? "good" : "warn";
                setTone("metric-env-card", data.done ? "good" : "warn");
                setTone("metric-step-card", data.done ? "good" : "warn");
                setTone("metric-damage-card", toneForDamage(data.damage_score));

                renderIncidents(observation.active_incidents || []);
                renderAlerts(observation.alerts || []);
                renderServices(observation.services_status || []);

                updateDamageBar(data.damage_score || 0);
                setText("damage-text", `Current damage score is ${formatNumber(data.damage_score, 2)}. Unresolved incidents continue to increase this over time.`);
                setEnvironmentReady((data.max_steps ?? 0) > 0);
                syncApmMode();
                updateWorkspaceGuide();
                updateActionAssistant();
                await loadSession();
                await loadGrade();
            } catch (error) {
                latestObservation = null;
                latestSession = null;
                el("stateJson").textContent = "Environment is idle. Select a scenario and click Start Incident to initialize the simulator.";
                setText("env-status", "Idle");
                el("env-status").dataset.state = "warn";
                setText("step-count", "0");
                setText("step-context", "Awaiting reset");
                setText("damage-score", "0.00");
                setText("damage-text", "System damage is idle until an environment is running.");
                setTone("metric-env-card", "warn");
                setTone("metric-step-card", "warn");
                setTone("metric-damage-card", "good");
                renderIncidents([]);
                renderAlerts([]);
                renderServices([]);
                updateDamageBar(0);
                setText("grade-score", "-");
                setText("grade-detail", "Grade available after a run starts");
                setTone("metric-grade-card", "warn");
                setEnvironmentReady(false);
                syncApmMode();
                updateWorkspaceGuide();
                updateActionAssistant();
                renderTimeline(null);
                renderGradeBreakdown(null);
            }
        }

        async function loadLogs() {
            try {
                const query = el("logQuery").value || "*";
                const service = el("logService").value || "";
                const limit = Number(el("logLimit").value || 25);
                const minutes = Number(el("logMinutes").value || 15);
                const params = new URLSearchParams({ query, limit: String(limit), minutes: String(minutes) });
                if (service) params.set("service", service);

                const data = await fetchJson(`/api/logs?${params.toString()}`);
                setText("log-source", `Source · ${data.source || "unknown"}`);
                setTone("log-source", toneForSource(data.source || "unknown"));
                setText("dd-note", data.note || `Query: ${data.query || query}`);
                setText("logs-note", data.note || `Querying ${prettySourceLabel(data.source || "unknown")} with \`${data.query || query}\`.`);
                updateProviderCard("logs", data.source || "unknown", data.note || "Recent log events loaded.");
                updateObservabilityStatus(data.source || "local-fallback");
                updateRefreshStamp("Logs refreshed from the selected provider.");
                rememberWorkspaceHistory("logs", {
                    label: `${service ? prettyServiceName(service) : "All services"} · ${query}`,
                    query,
                    service,
                    minutes,
                    limit,
                });
                renderLogs(data.logs || []);
            } catch (error) {
                renderEmpty("logs", "Log query failed||Try a broader query or check whether the provider is connected.");
                setBanner("error", "Log query failed", String(error.message || error), "Logs");
            }
        }

        async function seedElasticDemoLogs() {
            try {
                const data = await fetchJson("/api/demo/seed-logs", {
                    method: "POST",
                });
                setBanner("info", "Demo logs seeded", data.note || `Seeded ${data.count || 0} demo logs into Elasticsearch.`, "Elasticsearch");
                el("logService").value = data.service || "meta-pytorch-demo";
                el("logQuery").value = data.query || "*";
                el("logMinutes").value = "240";
                el("logLimit").value = "25";
                activateTab("logs");
                await loadObservabilityStatus();
                await loadLogs();
            } catch (error) {
                setBanner("error", "Demo log seed failed", String(error.message || error), "Elasticsearch");
            }
        }

        async function loadElasticDemoLogs() {
            el("logPreset").value = "all";
            el("logService").value = "meta-pytorch-demo";
            el("logQuery").value = "*";
            el("logMinutes").value = "240";
            el("logLimit").value = "25";
            activateTab("logs");
            setBanner("info", "Elastic demo filters applied", "The log workspace is now focused on the Elasticsearch demo incident stream.", "Logs");
            await loadObservabilityStatus();
            await loadLogs();
        }

        async function loadMetrics() {
            try {
                const metricView = metricViewDefinition(el("metricQuery").value);
                const query = metricView.query;
                const service = el("metricService").value || "";
                const points = Number(el("metricPoints").value || 24);
                const minutes = Number(el("metricMinutes").value || 15);
                const params = new URLSearchParams({ query, points: String(points), minutes: String(minutes) });
                if (service) params.set("service", service);

                const data = await fetchJson(`/api/metrics?${params.toString()}`);
                setText("metrics-source", `Source · ${data.source || "unknown"}`);
                setTone("metrics-source", toneForSource(data.source || "unknown"));
                setText("metrics-note", metricView.note || data.note || `${metricView.label} returned the latest available series.`);
                updateProviderCard("metrics", data.source || "unknown", data.note || "Metric provider responded successfully.");
                updateObservabilityStatus(data.source || "local-fallback");
                updateRefreshStamp(`${metricView.label} refreshed from the selected provider.`);
                rememberWorkspaceHistory("metrics", {
                    label: `${metricView.label}${service ? ` · ${prettyServiceName(service)}` : ""}`,
                    metricQuery: el("metricQuery").value,
                    service,
                    minutes,
                    points,
                });
                renderMetrics(data.series || []);
            } catch (error) {
                renderEmpty("metricsList", "Metric query failed||Try a different signal view or widen the time window.");
                setBanner("error", "Metric query failed", String(error.message || error), "Metrics");
            }
        }

        async function loadApm() {
            try {
                const request = buildApmRequest();
                const query = request.query;
                const service = request.service || "";
                const limit = request.limit;
                const minutes = request.minutes;
                const params = new URLSearchParams({ query, limit: String(limit), minutes: String(minutes) });
                if (service) params.set("service", service);

                const data = await fetchJson(`/api/apm?${params.toString()}`);
                setText("apm-source", `Source · ${data.source || "unknown"}`);
                setTone("apm-source", toneForSource(data.source || "unknown"));
                setText("traces-note", request.description || data.note || "Trace data loaded.");
                updateProviderCard("traces", data.source || "unknown", data.note || "Trace provider responded successfully.");
                updateObservabilityStatus(data.source || "local-fallback");
                updateRefreshStamp(`${apmModeLabel(request.mode)} refreshed from the selected provider.`);
                rememberWorkspaceHistory("traces", {
                    label: `${apmModeLabel(request.mode)}${service ? ` · ${prettyServiceName(service)}` : ""}`,
                    mode: request.mode,
                    service,
                    minutes,
                    limit,
                });
                renderTraces(data.traces || []);
            } catch (error) {
                renderEmpty("tracesList", "Trace query failed||Switch to All Services or widen the trace window.");
                setBanner("error", "Trace query failed", String(error.message || error), "Traces");
            }
        }

        async function refreshAll() {
            await loadState();
            await loadObservabilityStatus();
            await loadLogs();
            await loadMetrics();
            await loadApm();
        }

        function activeTabName() {
            return document.querySelector(".tab.active")?.dataset.tab || "logs";
        }

        async function refreshActiveWorkspace() {
            const active = activeTabName();
            if (active === "metrics") {
                await loadMetrics();
                return;
            }
            if (active === "traces") {
                await loadApm();
                return;
            }
            await loadLogs();
        }

        async function resetEnvironment() {
            const payload = {
                task_id: el("taskId").value,
                seed: Number(el("seed").value || 0),
            };

            try {
                await fetchJson("/reset", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload),
                });
                const scenarioName = el("taskId").selectedOptions?.[0]?.textContent || fallbackTaskLabel(payload.task_id);
                lastStepResult = null;
                setText("action-note", `Incident started for ${scenarioName} with seed ${payload.seed}.`);
                setText("action-meta", "Fresh observation loaded. Use the action composer or the observability workspace to investigate.");
                setText("action-reward", "0.00");
                setText("action-status", "Started");
                renderActionComponents({ setup: 0.0 });
                setBanner("info", "Incident started", `${scenarioName} is ready. Reload the observability tabs whenever you want a fresh read from the active providers.`, "Simulation");
                await refreshAll();
            } catch (error) {
                setBanner("error", "Incident start failed", String(error.message || error), "Simulation");
            }
        }

        async function runStep() {
            if (!environmentReady) {
                setBanner("warn", "Scenario not started", "Start the selected incident first, then execute an action. Choosing a different scenario does not start it automatically.", "Simulation");
                setText("action-note", "Start the selected incident before executing an action.");
                setText("action-meta", "Use Mission Control on the left, then click Start Incident to load the selected incident.");
                setText("action-status", "Blocked");
                return;
            }

            const missingFields = missingActionFields();
            if (missingFields.length > 0) {
                const missingList = missingFields.map(friendlyFieldLabel).join(", ");
                setBanner("warn", "Action needs more input", `Fill in ${missingList} before executing this action.`, "Action");
                setText("action-note", `This action is missing: ${missingList}.`);
                setText("action-meta", "The composer only highlights the fields that matter for the selected action.");
                setText("action-status", "Needs input");
                return;
            }

            const payload = {
                action_type: el("actionType").value,
                service: getEnabledValue("service"),
                metric: getEnabledValue("metric"),
                trace_id: getEnabledValue("traceId"),
                replicas: getEnabledValue("replicas", true),
                version: getEnabledValue("version"),
                incident_id: getEnabledValue("incidentId"),
                root_cause: getEnabledValue("rootCause"),
            };

            try {
                const previousObservation = latestObservation ? JSON.parse(JSON.stringify(latestObservation)) : null;
                const data = await fetchJson("/step", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload),
                });

                const reward = formatNumber(data.reward?.value ?? 0, 2);
                const outcome = data.info?.result || data.info?.note || "state updated";
                const actionName = actionLabel(payload.action_type);
                const changes = compareObservations(previousObservation, data.observation);
                lastStepResult = data;
                setText("action-note", `${actionName} completed with reward ${reward}.`);
                setText("action-meta", `${outcome}. ${changes[0] || "No major service-state shift was detected yet."} Step ${data.step ?? "-"} · done ${data.done ? "yes" : "no"}.`);
                setText("action-reward", reward);
                setText("action-status", titleCase(outcome));
                renderActionComponents(data.reward?.components || data.info?.components || {});
                setBanner("info", "Action executed", `${actionName} completed with reward ${reward}. ${outcome}.`, "Simulation");
                await loadState();
            } catch (error) {
                setText("action-status", "Failed");
                setBanner("error", "Action failed", String(error.message || error), "Simulation");
            }
        }

        document.querySelectorAll(".tab").forEach((button) => {
            button.addEventListener("click", async () => {
                activateTab(button.dataset.tab);
                await refreshActiveWorkspace();
            });
        });

        el("resetBtn").addEventListener("click", resetEnvironment);
        el("stepBtn").addEventListener("click", runStep);
        el("refreshBtn").addEventListener("click", refreshAll);
        el("loadLogsBtn").addEventListener("click", loadLogs);
        el("seedElasticDemoBtn").addEventListener("click", seedElasticDemoLogs);
        el("loadElasticDemoBtn").addEventListener("click", loadElasticDemoLogs);
        el("loadMetricsBtn").addEventListener("click", loadMetrics);
        el("loadApmBtn").addEventListener("click", loadApm);
        el("guidePrimaryBtn").addEventListener("click", runGuidePrimary);
        el("guideSecondaryBtn").addEventListener("click", runGuideSecondary);
        el("applySuggestedActionBtn").addEventListener("click", applySuggestedAction);
        el("focusSuggestedServiceBtn").addEventListener("click", focusSuggestedService);
        el("logPreset").addEventListener("change", applyLogPreset);
        el("taskId").addEventListener("change", async (event) => {
            await loadTaskDetails(event.target.value);
            latestObservation = null;
            latestSession = null;
            renderIncidents([]);
            renderAlerts([]);
            renderServices([]);
            renderTimeline(null);
            renderActionComponents({});
            setText("action-reward", "-");
            setText("action-status", "Pending start");
            setText("action-note", "The newly selected scenario is not running yet.");
            setText("action-meta", "Click Start Incident to initialize the selected scenario before taking any action.");
            setEnvironmentReady(false);
            updateActionAssistant();
            setBanner("warn", "Scenario selection changed", "You selected a new scenario. Click Start Incident to load it before executing actions.", "Scenario");
        });
        el("actionType").addEventListener("change", syncActionFields);
        el("service").addEventListener("change", updateActionAssistant);
        el("apmMode").addEventListener("change", syncApmMode);
        document.addEventListener("click", async (event) => {
            const historyChip = event.target.closest("[data-history-kind]");
            if (historyChip) {
                restoreWorkspaceHistory(historyChip.dataset.historyKind, historyChip.dataset.historyIndex);
                return;
            }

            const servicePivot = event.target.closest("[data-pivot-service]");
            if (servicePivot) {
                const service = servicePivot.dataset.pivotService;
                el("logService").value = service || "";
                el("metricService").value = service || "";
                el("service").value = service || "";
                el("actionType").value = "query_logs";
                syncActionFields();
                activateTab("logs");
                await loadLogs();
                return;
            }

            const tracePivot = event.target.closest("[data-pivot-trace]");
            if (tracePivot) {
                const traceId = tracePivot.dataset.pivotTrace;
                el("traceId").value = traceId || "";
                el("actionType").value = "trace_request";
                syncActionFields();
                activateTab("traces");
                await loadApm();
                return;
            }

            const metricServicePivot = event.target.closest("[data-metric-service]");
            if (metricServicePivot) {
                const service = metricServicePivot.dataset.metricService;
                el("metricService").value = service || "";
                el("service").value = service || "";
                el("actionType").value = "query_metrics";
                syncActionFields();
                await loadMetrics();
                return;
            }

            const traceServicePivot = event.target.closest("[data-trace-service]");
            if (traceServicePivot) {
                const service = traceServicePivot.dataset.traceService;
                el("apmMode").value = "service";
                el("apmService").value = service || "";
                el("service").value = service || "";
                syncApmMode();
                await loadApm();
            }
        });

        (async function init() {
            populateActions();
            await refreshTasks();
            await loadTaskDetails("easy_0");
            syncActionFields();
            updateWorkspaceGuide();
            updateActionAssistant();
            applyLogPreset();
            renderEmpty("logs", "Load logs||Inspect the selected provider stream and then pivot from suspicious events.");
            renderEmpty("metricsList", "Load metrics||Compare live signals across services before taking a disruptive action.");
            renderEmpty("tracesList", "Load traces||Inspect Datadog spans or Elastic-derived trace events across the request path.");
            renderActionComponents({});
            renderTimeline(null);
            renderGradeBreakdown(null);
            updateProviderCard("logs", "local-fallback", "Waiting for the first log query.");
            updateProviderCard("metrics", "local-fallback", "Waiting for the first metric query.");
            updateProviderCard("traces", "local-fallback", "Waiting for the first trace query.");
            await refreshAll();
            setInterval(loadState, 8000);
            setInterval(refreshActiveWorkspace, 30000);
            setInterval(loadObservabilityStatus, 45000);
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


class SessionResponse(BaseModel):
    """Session snapshot for timeline and grading UX"""
    environment_ready: bool
    task_id: Optional[str] = None
    task_name: Optional[str] = None
    difficulty: Optional[str] = None
    current_step: int = 0
    max_steps: int = 0
    done: bool = False
    damage_score: float = 0.0
    actions_log: List[Dict[str, Any]] = []
    resolved_incidents: List[str] = []
    incorrect_resolutions: List[str] = []
    prioritized_incidents: List[str] = []
    active_incidents: List[str] = []


def _build_session_snapshot() -> Dict[str, Any]:
    """Build a browser-friendly session payload for timeline and assistant UX."""
    global current_env, current_task_id

    if current_env is None:
        task = TASK_DEFINITIONS.get(current_task_id) or {}
        return {
            "environment_ready": False,
            "task_id": current_task_id,
            "task_name": task.get("name"),
            "difficulty": task.get("difficulty"),
            "current_step": 0,
            "max_steps": 0,
            "done": False,
            "damage_score": 0.0,
            "actions_log": [],
            "resolved_incidents": [],
            "incorrect_resolutions": [],
            "prioritized_incidents": [],
            "active_incidents": [],
        }

    task = current_env.task_config or {}
    return {
        "environment_ready": True,
        "task_id": task.get("task_id", current_task_id),
        "task_name": task.get("name"),
        "difficulty": task.get("difficulty"),
        "current_step": current_env.current_step,
        "max_steps": current_env.max_steps,
        "done": current_env._check_done(),
        "damage_score": float(current_env.damage_score),
        "actions_log": list(current_env.actions_log),
        "resolved_incidents": list(current_env.resolved_incidents),
        "incorrect_resolutions": list(current_env.incorrect_resolutions),
        "prioritized_incidents": list(current_env.prioritized_incidents),
        "active_incidents": list(current_env.root_causes.keys()),
    }


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
        return {
            "observation": {
                "alerts": [],
                "services_status": [],
                "recent_logs": [],
                "active_incidents": [],
                "metrics_summary": {},
                "current_step": 0,
                "damage_score": 0.0,
                "available_actions": [a.value for a in ActionType],
            },
            "done": False,
            "max_steps": 0,
            "current_step": 0,
            "damage_score": 0.0,
        }

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
        "grader": {
            "type": "deterministic",
            "score_range": {
                "min_exclusive": 0.001,
                "max_exclusive": 0.999,
            },
        },
    }


@app.get("/health")
async def health():
    """Health check"""
    return {
        "status": "healthy",
        "environment_ready": current_env is not None,
    }


@app.get("/readyz")
async def readyz():
    """Readiness-style endpoint for simple hosting checks."""
    session = _build_session_snapshot()
    return {
        "status": "ready",
        "environment_ready": session["environment_ready"],
        "observability_backend": _fetch_observability_status().get("source", "local-fallback"),
    }


@app.get("/api/session")
async def api_session() -> SessionResponse:
    """Session snapshot for timeline, assistant suggestions, and grading context."""
    return _build_session_snapshot()


@app.get("/api/observability/status")
async def api_observability_status():
    """Report which external log backend is configured and whether it is reachable."""
    try:
        return _fetch_observability_status()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to fetch observability status.")


@app.get("/api/datadog/status")
async def api_datadog_status():
    """Backward-compatible alias for observability status."""
    return await api_observability_status()


@app.get("/api/logs")
async def api_logs(query: str = "*", service: Optional[str] = None, limit: int = 25, minutes: int = 15):
    """Fetch logs from Elasticsearch, Datadog, or fall back to the local simulated environment."""
    try:
        elastic_settings = _get_elasticsearch_settings()
        if _elasticsearch_enabled(elastic_settings):
            return _fetch_elasticsearch_logs(query=query, service=service, limit=limit, minutes=minutes)
        return _fetch_datadog_logs(query=query, service=service, limit=limit, minutes=minutes)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to fetch logs from observability backend.")


@app.post("/api/demo/seed-logs")
async def api_seed_demo_logs(scenario: str = "all"):
    """Seed demo observability incidents into the active Elasticsearch backend."""
    try:
        return _seed_demo_logs_into_elasticsearch(scenario=scenario)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to seed demo logs into Elasticsearch.")


@app.get("/api/metrics")
async def api_metrics(query: str = "avg:system.cpu.user{*}", service: Optional[str] = None, points: int = 24, minutes: int = 15):
    """Fetch metrics from Datadog or fall back to simulator service metrics."""
    try:
        return _fetch_datadog_metrics(metric_query=query, service=service, points=points, minutes=minutes)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to fetch metrics from observability backend.")


@app.get("/api/apm")
async def api_apm(query: str = "service:*", service: Optional[str] = None, limit: int = 20, minutes: int = 15):
    """Fetch Datadog APM traces, Elastic-derived trace events, or simulator fallback traces."""
    try:
        if _datadog_enabled():
            return _fetch_datadog_apm(query=query, service=service, limit=limit, minutes=minutes)
        elastic_settings = _get_elasticsearch_settings()
        if _elasticsearch_enabled(elastic_settings):
            return _fetch_elasticsearch_traces(query=query, service=service, limit=limit, minutes=minutes)
        return _fetch_datadog_apm(query=query, service=service, limit=limit, minutes=minutes)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to fetch APM data from observability backend.")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 7860))
    uvicorn.run(app, host="0.0.0.0", port=port)
