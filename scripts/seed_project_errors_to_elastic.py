#!/usr/bin/env python3
"""Seed repo-specific incident logs into Elasticsearch for dashboard demos."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

import requests
from dotenv import load_dotenv


load_dotenv()

ROOT_DIR = Path(__file__).resolve().parent.parent
LOCAL_DEMO_LOG_FILE = Path(
    os.getenv("LOCAL_DEMO_LOG_FILE", str(ROOT_DIR / ".run" / "local-demo-logs.jsonl"))
)


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, "")
    if not value:
        return default
    return value.strip().lower() not in {"0", "false", "no"}


def elastic_settings() -> Dict[str, Any]:
    return {
        "url": os.getenv("ELASTICSEARCH_URL", "").strip().rstrip("/"),
        "api_key": os.getenv("ELASTICSEARCH_API_KEY", "").strip(),
        "username": os.getenv("ELASTICSEARCH_USERNAME", "").strip(),
        "password": os.getenv("ELASTICSEARCH_PASSWORD", "").strip(),
        "index": os.getenv("ELASTICSEARCH_LOG_INDEX", "meta-pytorch-demo-*").strip() or "meta-pytorch-demo-*",
        "verify_tls": env_bool("ELASTICSEARCH_VERIFY_TLS", True),
    }


def auth_headers(settings: Dict[str, Any]) -> Dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-ndjson",
    }
    if settings["api_key"]:
        headers["Authorization"] = f"ApiKey {settings['api_key']}"
    elif settings["username"] and settings["password"]:
        token = base64.b64encode(
            f"{settings['username']}:{settings['password']}".encode("utf-8")
        ).decode("utf-8")
        headers["Authorization"] = f"Basic {token}"
    return headers


def docs_for_scenario(name: str) -> List[Dict[str, Any]]:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    service = "meta-pytorch-demo"
    base = {
        "event.dataset": "meta-pytorch.demo",
        "service.name": service,
        "repo": "Meta PyTorch",
        "environment": "local-demo",
    }

    scenarios: Dict[str, List[Dict[str, Any]]] = {
        "startup_failures": [
            {
                **base,
                "@timestamp": (now - timedelta(minutes=4)).isoformat(),
                "log.level": "error",
                "trace.id": "trace-runlocal-001",
                "incident_id": "INC-DEMO-1001",
                "message": "run-local.sh failed because elasticsearch-9.3.2 directory was missing.",
                "component": "run-local.sh",
                "symptom": "startup_blocked",
                "diagnosis_hint": "Install or extract the local Elasticsearch distribution, or switch to a reachable remote Elasticsearch URL.",
            },
            {
                **base,
                "@timestamp": (now - timedelta(minutes=3, seconds=30)).isoformat(),
                "log.level": "error",
                "trace.id": "trace-runlocal-001",
                "incident_id": "INC-DEMO-1001",
                "message": "App startup blocked because Elasticsearch prerequisite check failed before uvicorn launch.",
                "component": "bootstrap",
                "symptom": "startup_blocked",
                "diagnosis_hint": "The local stack bootstrap is coupled to Elasticsearch availability.",
            },
            {
                **base,
                "@timestamp": (now - timedelta(minutes=3)).isoformat(),
                "log.level": "warning",
                "trace.id": "trace-runlocal-001",
                "incident_id": "INC-DEMO-1001",
                "message": "Dashboard stayed in local-fallback mode because ELASTICSEARCH_URL was not configured.",
                "component": "observability",
                "symptom": "fallback_mode",
                "diagnosis_hint": "Point the app at an Elasticsearch endpoint to inspect real external logs.",
            },
        ],
        "port_conflict": [
            {
                **base,
                "@timestamp": (now - timedelta(minutes=2, seconds=40)).isoformat(),
                "log.level": "error",
                "trace.id": "trace-port-7860",
                "incident_id": "INC-DEMO-1002",
                "message": "uvicorn failed to bind to 127.0.0.1:7860 because the address was already in use.",
                "component": "uvicorn",
                "symptom": "port_conflict",
                "port": 7860,
                "diagnosis_hint": "Find and stop the stale Python listener or choose a different local app port.",
            },
            {
                **base,
                "@timestamp": (now - timedelta(minutes=2, seconds=10)).isoformat(),
                "log.level": "info",
                "trace.id": "trace-port-7860",
                "incident_id": "INC-DEMO-1002",
                "message": "A stray Python process was still listening on port 7860 after the earlier session.",
                "component": "stop-local.sh",
                "symptom": "stale_process",
                "port": 7860,
                "diagnosis_hint": "Use lsof or the PID file to shut down the stale process cleanly.",
            },
        ],
        "docker_gap": [
            {
                **base,
                "@timestamp": (now - timedelta(minutes=1, seconds=40)).isoformat(),
                "log.level": "error",
                "trace.id": "trace-docker-001",
                "incident_id": "INC-DEMO-1003",
                "message": "reference-stack workflow could not start because docker was not installed on the host.",
                "component": "reference-stack",
                "symptom": "missing_dependency",
                "dependency": "docker",
                "diagnosis_hint": "Install Docker Desktop or run against a remote Elasticsearch cluster instead of the compose stack.",
            },
            {
                **base,
                "@timestamp": (now - timedelta(minutes=1, seconds=5)).isoformat(),
                "log.level": "warning",
                "trace.id": "trace-docker-001",
                "incident_id": "INC-DEMO-1003",
                "message": "Side-by-side harness remained unavailable because the reference stack depends on docker compose.",
                "component": "demo-workflow",
                "symptom": "missing_dependency",
                "dependency": "docker-compose",
                "diagnosis_hint": "Use the manual seeding workflow until Docker is available.",
            },
        ],
        "healthy_recovery": [
            {
                **base,
                "@timestamp": (now - timedelta(seconds=40)).isoformat(),
                "log.level": "info",
                "trace.id": "trace-recovery-001",
                "incident_id": "INC-DEMO-1099",
                "message": "Manual replay mode is active. External Elasticsearch is reachable and ready for dashboard inspection.",
                "component": "demo-workflow",
                "symptom": "healthy",
                "diagnosis_hint": "Use service and incident filters in the dashboard to pivot through the replay data.",
            },
            {
                **base,
                "@timestamp": now.isoformat(),
                "log.level": "info",
                "trace.id": "trace-recovery-001",
                "incident_id": "INC-DEMO-1099",
                "message": "Project error replay completed successfully.",
                "component": "seed_project_errors_to_elastic.py",
                "symptom": "healthy",
                "diagnosis_hint": "Search incident_id:INC-DEMO-* to diagnose the seeded failures.",
            },
        ],
    }
    if name == "all":
        merged: List[Dict[str, Any]] = []
        for scenario_name in (
            "startup_failures",
            "port_conflict",
            "docker_gap",
            "healthy_recovery",
        ):
            merged.extend(scenarios[scenario_name])
        return merged
    if name not in scenarios:
        raise KeyError(name)
    return scenarios[name]


def bulk_lines(index_name: str, docs: Iterable[Dict[str, Any]]) -> str:
    chunks: List[str] = []
    for doc in docs:
        chunks.append(json.dumps({"index": {"_index": index_name}}))
        chunks.append(json.dumps(doc))
    return "\n".join(chunks) + "\n"


def concrete_index_name(index_pattern: str) -> str:
    today = datetime.now(timezone.utc).strftime("%Y.%m.%d")
    if "*" in index_pattern:
        return index_pattern.replace("*", today)
    return index_pattern


def write_local_demo_logs(docs: Iterable[Dict[str, Any]]) -> int:
    doc_list = list(docs)
    LOCAL_DEMO_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCAL_DEMO_LOG_FILE.open("w", encoding="utf-8") as handle:
        for doc in doc_list:
            handle.write(json.dumps(doc) + "\n")
    return len(doc_list)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed repo-specific demo errors into Elasticsearch."
    )
    parser.add_argument(
        "--scenario",
        default="all",
        choices=["all", "startup_failures", "port_conflict", "docker_gap", "healthy_recovery"],
        help="Which scenario set to seed.",
    )
    parser.add_argument(
        "--index",
        default="",
        help="Override the target index. Defaults to ELASTICSEARCH_LOG_INDEX or meta-pytorch-demo-*.",
    )
    args = parser.parse_args()

    settings = elastic_settings()
    docs = docs_for_scenario(args.scenario)

    if not settings["url"]:
        count = write_local_demo_logs(docs)
        print(f"Wrote {count} demo documents to {LOCAL_DEMO_LOG_FILE}")
        print("Backend mode: local-fallback")
        print("Open the dashboard and query: incident_id:INC-DEMO-*")
        print("Optional: set ELASTICSEARCH_URL later to seed a real cluster instead.")
        return 0

    target_index = concrete_index_name(args.index or settings["index"])
    payload = bulk_lines(target_index, docs)

    response = requests.post(
        f"{settings['url']}/_bulk",
        headers=auth_headers(settings),
        data=payload.encode("utf-8"),
        timeout=15,
        verify=settings["verify_tls"],
    )
    response.raise_for_status()

    body = response.json()
    if body.get("errors"):
        print(json.dumps(body, indent=2), file=sys.stderr)
        return 2

    incident_ids = sorted({doc["incident_id"] for doc in docs})
    print(f"Seeded {len(docs)} documents into {target_index}")
    print(f"Service filter: meta-pytorch-demo")
    print(f"Incident IDs: {', '.join(incident_ids)}")
    print("Suggested dashboard query: incident_id:INC-DEMO-* OR trace.id:*")
    return 0


if __name__ == "__main__":
    sys.exit(main())
