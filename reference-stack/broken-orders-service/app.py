import json
import logging
import os
import random
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Dict

from fastapi import FastAPI, HTTPException


SERVICE_NAME = os.getenv("SERVICE_NAME", "orders-service")
PORT = int(os.getenv("PORT", "8081"))
LOG_FILE = os.getenv("LOG_FILE", "/tmp/orders-service.log")
FAILURE_MODE = os.getenv("FAILURE_MODE", "db_connection_leak")

FAILURE_DETAILS: Dict[str, Dict[str, str]] = {
    "healthy": {
        "incident_id": "INC-0000",
        "message": "Checkout pipeline is stable.",
        "hint": "No action required.",
        "level": "info",
    },
    "db_connection_leak": {
        "incident_id": "INC-5001",
        "message": "Database pool exhausted while reserving inventory.",
        "hint": "Reduce leaked sessions and recycle pooled connections.",
        "level": "error",
    },
    "payment_timeout": {
        "incident_id": "INC-5002",
        "message": "Payment gateway timed out during charge authorization.",
        "hint": "Retry safely or route traffic away from the degraded dependency.",
        "level": "error",
    },
    "noisy_cache": {
        "incident_id": "INC-5003",
        "message": "Cache miss storm increased checkout latency but requests still complete.",
        "hint": "Distinguish noisy warnings from the primary failure before restarting services.",
        "level": "warning",
    },
}


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "@timestamp": datetime.now(timezone.utc).isoformat(),
            "service": SERVICE_NAME,
            "log.level": record.levelname.lower(),
            "message": record.getMessage(),
        }
        extra = getattr(record, "payload", None)
        if isinstance(extra, dict):
            payload.update(extra)
        return json.dumps(payload)


logger = logging.getLogger("orders-service")
logger.setLevel(logging.INFO)
logger.handlers.clear()

startup_dir = os.path.dirname(LOG_FILE)
if startup_dir:
    os.makedirs(startup_dir, exist_ok=True)

file_handler = logging.FileHandler(LOG_FILE)
file_handler.setFormatter(JsonLineFormatter())
logger.addHandler(file_handler)

state = {
    "mode": FAILURE_MODE,
    "requests": 0,
}

app = FastAPI(title="Broken Orders Service")


def current_detail() -> Dict[str, str]:
    return FAILURE_DETAILS.get(state["mode"], FAILURE_DETAILS["healthy"])


def emit(level: str, message: str, **payload: str) -> None:
    record_payload = {"service": SERVICE_NAME, **payload}
    log_method = getattr(logger, level.lower(), logger.info)
    log_method(message, extra={"payload": record_payload})


def background_noise() -> None:
    while True:
        detail = current_detail()
        trace_id = str(uuid.uuid4())
        if state["mode"] == "healthy":
            emit(
                "info",
                "Background health check passed.",
                trace_id=trace_id,
                incident_id=detail["incident_id"],
                fix_hint=detail["hint"],
                component="scheduler",
            )
        else:
            emit(
                detail["level"],
                detail["message"],
                trace_id=trace_id,
                incident_id=detail["incident_id"],
                fix_hint=detail["hint"],
                component="checkout-worker",
                retryable=str(state["mode"] != "db_connection_leak").lower(),
            )
        time.sleep(4)


@app.get("/health")
def health():
    return {"status": "ok", "mode": state["mode"], "service": SERVICE_NAME}


@app.get("/checkout")
def checkout():
    state["requests"] += 1
    detail = current_detail()
    trace_id = str(uuid.uuid4())
    request_id = f"req-{state['requests']:04d}"

    emit(
        "info",
        "Received checkout request.",
        trace_id=trace_id,
        request_id=request_id,
        incident_id=detail["incident_id"],
        route="/checkout",
    )

    if state["mode"] == "healthy":
        emit(
            "info",
            "Checkout completed successfully.",
            trace_id=trace_id,
            request_id=request_id,
            incident_id=detail["incident_id"],
            latency_ms=str(random.randint(40, 120)),
        )
        return {"status": "ok", "trace_id": trace_id, "mode": state["mode"]}

    status_code = 503 if state["mode"] != "noisy_cache" else 200
    emit(
        detail["level"],
        detail["message"],
        trace_id=trace_id,
        request_id=request_id,
        incident_id=detail["incident_id"],
        fix_hint=detail["hint"],
        latency_ms=str(random.randint(250, 1400)),
    )

    if status_code >= 400:
        raise HTTPException(
            status_code=status_code,
            detail={
                "status": "error",
                "trace_id": trace_id,
                "mode": state["mode"],
                "incident_id": detail["incident_id"],
            },
        )

    return {
        "status": "degraded",
        "trace_id": trace_id,
        "mode": state["mode"],
        "incident_id": detail["incident_id"],
    }


@app.post("/admin/mode/{mode}")
def set_mode(mode: str):
    if mode not in FAILURE_DETAILS:
        raise HTTPException(status_code=400, detail={"error": f"Unknown mode `{mode}`."})

    previous = state["mode"]
    state["mode"] = mode
    detail = current_detail()
    emit(
        "info",
        "Service failure mode changed.",
        previous_mode=previous,
        new_mode=mode,
        incident_id=detail["incident_id"],
        fix_hint=detail["hint"],
    )
    return {"status": "ok", "previous_mode": previous, "new_mode": mode}


if __name__ == "__main__":
    emit(
        "info",
        "Orders service booted.",
        incident_id=current_detail()["incident_id"],
        fix_hint=current_detail()["hint"],
    )

    worker = threading.Thread(target=background_noise, daemon=True)
    worker.start()

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
