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
from typing import Optional
from openai import OpenAI, APIError

from environment import make_env
from models import Action, ActionType, ServiceName, MetricType


# Initialize OpenAI client with environment variables
API_BASE_URL = os.getenv("API_BASE_URL", "https://api.openai.com/v1")
MODEL_NAME = os.getenv("MODEL_NAME", "gpt-3.5-turbo")
HF_TOKEN = os.getenv("HF_TOKEN", "")  # Used for HF Space deployment

client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY", "sk-test"),
    base_url=API_BASE_URL,
)


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
        
        try:
            response = client.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {"role": "system", "content": "You are an expert SRE. Choose one action."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=100,
                timeout=5.0,
            )
            response_text = response.choices[0].message.content.lower()
        except (APIError, Exception):
            # Fallback if LLM call fails
            response_text = "query_logs api_gateway"

        # Parse response into action
        if "query_logs" in response_text:
            service = ServiceName.DB if "db" in response_text else ServiceName.API_GATEWAY
            return Action(
                action_type=ActionType.QUERY_LOGS,
                service=service,
            )
        elif "query_metrics" in response_text:
            service = ServiceName.PAYMENTS if "payment" in response_text else ServiceName.API_GATEWAY
            metric = MetricType.ERROR_RATE if "error" in response_text else MetricType.LATENCY
            return Action(
                action_type=ActionType.QUERY_METRICS,
                service=service,
                metric=metric,
            )
        elif "restart" in response_text:
            service = ServiceName.API_GATEWAY
            return Action(
                action_type=ActionType.RESTART_SERVICE,
                service=service,
            )
        elif "resolve" in response_text or "diagnosis" in response_text:
            root_causes = ["db_connection_leak", "bad_payment_deployment", "cache_backend_failure"]
            return Action(
                action_type=ActionType.RESOLVE_INCIDENT,
                root_cause=root_causes[0],
            )
        elif "rollback" in response_text:
            service = ServiceName.PAYMENTS
            return Action(
                action_type=ActionType.ROLLBACK_DEPLOYMENT,
                service=service,
                version="v1.2.0",
            )
        elif "trace" in response_text:
            return Action(
                action_type=ActionType.TRACE_REQUEST,
                trace_id="trace_1234",
            )
        elif "prioritize" in response_text:
            return Action(
                action_type=ActionType.PRIORITIZE_INCIDENT,
                incident_id="db_connection_leak",
            )
        elif "scale" in response_text:
            return Action(
                action_type=ActionType.SCALE_SERVICE,
                service=ServiceName.API_GATEWAY,
                replicas=5,
            )
        else:
            # Default to query logs
            return Action(
                action_type=ActionType.QUERY_LOGS,
                service=ServiceName.API_GATEWAY,
            )

    except Exception as e:
        print(f"Error in LLM call: {e}")
        return Action(
            action_type=ActionType.QUERY_LOGS,
            service=ServiceName.API_GATEWAY,
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

    print("[START]")
    print(json.dumps({
        "task": task_id,
        "difficulty": env.task_config.get("difficulty"),
        "timestamp": "2024-01-01T00:00:00Z",
    }))

    step_count = 0
    done = False

    while not done and step_count < max_steps:
        step_count += 1

        # Get action from agent (LLM)
        action = get_agent_action(
            {"metrics_summary": obs.metrics_summary, "alerts": [a.dict() for a in obs.alerts]},
            step_count,
            env.task_config.get("difficulty", "easy"),
        )

        # Execute action
        obs, reward, done, info = env.step(action)

        # Log step (REQUIRED FORMAT)
        print("[STEP]")
        step_log = {
            "step": step_count,
            "action": action.action_type.value,
            "reward": round(float(reward.value), 4),
            "done": done,
            "damage_score": round(float(env.damage_score), 4),
            "info": info,
        }
        print(json.dumps(step_log))

        episode_data["steps"].append(step_log)
        episode_data["total_reward"] += reward.value

    # Compute final grade
    grade = env.get_grade()
    episode_data["final_grade"] = {
        "score": round(float(grade["score"]), 4),
        "correctness": round(float(grade["correctness"]), 4),
        "efficiency": round(float(grade["efficiency"]), 4),
        "damage": round(float(grade["damage"]), 4),
    }

    print("[END]")
    print(json.dumps({
        "status": "completed",
        "steps_taken": step_count,
        "final_score": round(float(grade["score"]), 4),
        "resolved_incidents": env.resolved_incidents,
    }))

    return episode_data


def main():
    """Run baseline inference on all tasks"""
    print("DevOps War Room - Baseline Inference")
    print("====================================\n")

    tasks = ["easy_0", "easy_1", "medium_0", "medium_1", "hard_0", "hard_1"]
    results = {}

    for task_id in tasks:
        print(f"\n--- Running task: {task_id} ---")
        try:
            result = run_episode(task_id=task_id, max_steps=30)
            results[task_id] = {
                "status": "success",
                "final_score": result["final_grade"]["score"],
            }
            print(f"✓ Task {task_id} completed with score: {result['final_grade']['score']:.4f}")
        except Exception as e:
            print(f"✗ Task {task_id} failed: {e}")
            results[task_id] = {
                "status": "failed",
                "error": str(e),
            }

    # Summary
    print("\n\n=== SUMMARY ===")
    successful = sum(1 for r in results.values() if r["status"] == "success")
    print(f"Completed: {successful}/{len(tasks)} tasks")
    
    for task_id, result in results.items():
        if result["status"] == "success":
            score = result["final_score"]
            print(f"{task_id}: {score:.4f}")
        else:
            print(f"{task_id}: FAILED")

    return results


if __name__ == "__main__":
    results = main()
    sys.exit(0 if all(r["status"] == "success" for r in results.values()) else 1)
