#!/usr/bin/env python3
"""
Baseline inference script for a distributed incident war-room environment.
Demonstrates an agent actively operating the system through tools.
Uses OpenAI client for LLM calls (required).
Follows strict logging format.
"""

import os
import json
import sys
from datetime import datetime, timezone
from typing import Optional
from openai import OpenAI, APIError

from environment import make_env
from models import Action, ActionType, ServiceName, MetricType


# Required submission environment variables.
API_BASE_URL = os.environ["API_BASE_URL"]
API_KEY = os.environ["API_KEY"]
MODEL_NAME = os.getenv("MODEL_NAME", "gpt-4o-mini")
HF_TOKEN = os.getenv("HF_TOKEN")

# Optional if you use from_docker_image().
LOCAL_IMAGE_NAME = os.getenv("LOCAL_IMAGE_NAME")
client: Optional[OpenAI] = None
DEFAULT_TASK_IDS = ["easy_0", "medium_0", "hard_0"]


def safe_submission_score(value: float) -> float:
    """Keep emitted scores comfortably inside the validator's open interval."""
    return min(0.999, max(0.001, float(value)))


def emit_event(marker: str, payload: dict) -> None:
    """Emit the exact structured stdout format expected by the evaluator."""
    print(marker, flush=True)
    print(json.dumps(payload), flush=True)


def get_openai_client() -> OpenAI:
    """Create the OpenAI client using the evaluator-injected proxy credentials."""
    global client
    if client is not None:
        return client
    client = OpenAI(
        api_key=API_KEY,
        base_url=API_BASE_URL,
    )
    return client


def get_agent_action(observation: dict, step: int, task_difficulty: str) -> Action:
    """
    Use OpenAI LLM to decide next action.
    Demonstrates required OpenAI client integration.
    """
    try:
        #Construct prompt from observation
        status_str = json.dumps(observation.get("metrics_summary", {}), indent=2)
        alerts_str = "\n".join([
            f"- {a['service']}: {a['message']} (severity: {a['severity']})"
            for a in observation.get("alerts", [])
        ])
        
        prompt = f"""You are an SRE in a distributed incident war room dealing with simultaneous production outages.
    You are not chatting with a user; you are actively operating the system through tools.
        
Current Status (Step {step}):
Alerts:
{alerts_str}

Metrics:
{status_str}

Available Actions:
- query_logs(service): Check logs from auth|payments|db|cache|api_gateway
- query_metrics(service, metric): Check metric latency|error_rate|cpu|memory|connection_count|queue_depth
- restart_service(service): Restart a service
- trace_request(trace_id): Follow a distributed request
- prioritize_incident(incident_id): Mark an incident as priority
- resolve_incident(root_cause): Provide diagnosis
- rollback_deployment(service, version): Rollback to previous version
- scale_service(service, replicas): Scale service capacity

Choose the most useful next action. Think step by step.
"""
        
        openai_client = get_openai_client()
        assert openai_client is not None, "OpenAI client could not be initialized"
        response = openai_client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": "You are an expert SRE. Choose one action."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=100,
            timeout=5.0,
        )
        response_text = (response.choices[0].message.content or "").lower()

        # Parse response into action
        if "query_logs" in response_text:
            service = ServiceName.DB if "db" in response_text else ServiceName.API_GATEWAY
            return Action(
                action_type=ActionType.QUERY_LOGS,
                service=service,
                metric=None, trace_id=None, incident_id=None, root_cause=None, replicas=None, version=None,
            )
        elif "query_metrics" in response_text:
            service = ServiceName.PAYMENTS if "payment" in response_text else ServiceName.API_GATEWAY
            metric = MetricType.ERROR_RATE if "error" in response_text else MetricType.LATENCY
            return Action(
                action_type=ActionType.QUERY_METRICS,
                service=service,
                metric=metric,
                trace_id=None, incident_id=None, root_cause=None, replicas=None, version=None,
            )
        elif "restart" in response_text:
            service = ServiceName.API_GATEWAY
            return Action(
                action_type=ActionType.RESTART_SERVICE,
                service=service,
                metric=None, trace_id=None, incident_id=None, root_cause=None, replicas=None, version=None,
            )
        elif "resolve" in response_text or "diagnosis" in response_text:
            root_causes = ["db_connection_leak", "bad_payment_deployment", "cache_backend_failure"]
            return Action(
                action_type=ActionType.RESOLVE_INCIDENT,
                root_cause=root_causes[0],
                service=None, metric=None, trace_id=None, incident_id=None, replicas=None, version=None,
            )
        elif "rollback" in response_text:
            service = ServiceName.PAYMENTS
            return Action(
                action_type=ActionType.ROLLBACK_DEPLOYMENT,
                service=service,
                version="v1.2.0",
                metric=None, trace_id=None, incident_id=None, root_cause=None, replicas=None,
            )
        elif "trace" in response_text:
            return Action(
                action_type=ActionType.TRACE_REQUEST,
                trace_id="trace_1234",
                service=None, metric=None, incident_id=None, root_cause=None, replicas=None, version=None,
            )
        elif "prioritize" in response_text:
            return Action(
                action_type=ActionType.PRIORITIZE_INCIDENT,
                incident_id="db_connection_leak",
                service=None, metric=None, trace_id=None, root_cause=None, replicas=None, version=None,
            )
        elif "scale" in response_text:
            return Action(
                action_type=ActionType.SCALE_SERVICE,
                service=ServiceName.API_GATEWAY,
                replicas=5,
                metric=None, trace_id=None, incident_id=None, root_cause=None, version=None,
            )
        else:
            # Default to query logs
            return Action(
                action_type=ActionType.QUERY_LOGS,
                service=ServiceName.API_GATEWAY,
                metric=None, trace_id=None, incident_id=None, root_cause=None, replicas=None, version=None,
            )

    except Exception as e:
        print(f"Error in LLM call: {e}", file=sys.stderr, flush=True)
        return Action(
            action_type=ActionType.QUERY_LOGS,
            service=ServiceName.API_GATEWAY,
            metric=None, trace_id=None, incident_id=None, root_cause=None, replicas=None, version=None,
        )


def run_episode(task_id: str = "easy_0", max_steps: int = 20) -> dict:
    """
    Run single episode of environment.
    Follows EXACT logging format required.
    """
    env = make_env(task_id=task_id, seed=0)
    
    obs = env.reset()
    episode_data = {
        "task_id": task_id,
        "steps": [],
        "final_grade": None,
        "total_reward": 0.0,
    }

    emit_event("[START]", {
        "task": task_id,
        "difficulty": env.task_config.get("difficulty"),
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    })

    step_count = 0
    done = False

    while not done and step_count < max_steps:
        step_count += 1

        # Get action from agent (LLM)
        action = get_agent_action(
            {"metrics_summary": obs.metrics_summary, "alerts": [a.model_dump() for a in obs.alerts]},
            step_count,
            env.task_config.get("difficulty", "easy"),
        )

        # Execute action
        obs, reward, done, info = env.step(action)

        # Log step (REQUIRED FORMAT)
        step_log = {
            "step": step_count,
            "action": action.action_type.value,
            "reward": safe_submission_score(round(float(reward.value), 4)),
            "done": done,
            "damage_score": safe_submission_score(round(float(env.damage_score), 4)),
            "info": info,
        }
        emit_event("[STEP]", step_log)

        episode_data["steps"].append(step_log)
        episode_data["total_reward"] += reward.value

    # Compute final grade
    grade = env.get_grade()
    episode_data["final_grade"] = {
        "score": safe_submission_score(grade["score"]),
        "correctness": safe_submission_score(grade["correctness"]),
        "efficiency": safe_submission_score(grade["efficiency"]),
        "damage": safe_submission_score(grade["damage"]),
    }

    emit_event("[END]", {
        "status": "completed",
        "steps_taken": step_count,
        "score": safe_submission_score(grade["score"]),
        "final_score": safe_submission_score(grade["score"]),
        "resolved_incidents": env.resolved_incidents,
    })

    return episode_data


def main():
    """Run one or more evaluation episodes with strict structured stdout only."""
    task_ids_env = os.getenv("TASK_IDS")
    task_id = os.getenv("TASK_ID")
    max_steps = int(os.getenv("MAX_STEPS", "30"))

    if task_ids_env:
        task_ids = [task.strip() for task in task_ids_env.split(",") if task.strip()]
    elif task_id:
        task_ids = [task_id]
    else:
        task_ids = DEFAULT_TASK_IDS

    results = []
    for current_task_id in task_ids:
        results.append(run_episode(task_id=current_task_id, max_steps=max_steps))
    return results


if __name__ == "__main__":
    try:
        main()
        sys.exit(0)
    except Exception as exc:
        emit_event("[END]", {
            "status": "failed",
            "score": safe_submission_score(0.001),
            "final_score": safe_submission_score(0.001),
            "error": str(exc),
        })
        sys.exit(1)