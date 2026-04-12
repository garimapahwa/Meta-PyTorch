"""
DevOps War Room Environment
OpenEnv-compliant environment for SRE debugging simulation
"""

import random
import json
from typing import Dict, List, Tuple, Optional, Any
from datetime import datetime
import numpy as np

from models import (
    Action, ActionType, Observation, Reward, State, ServiceName,
    ServiceStatus, Alert, LogEntry, MetricType, TaskDefinition
)
from tasks import TaskGenerator, TASK_DEFINITIONS
from graders import get_grader_for_difficulty, safe_task_score, safe_unit_score


class DevOpsWarRoomEnv:
    """
    Main OpenEnv environment for DevOps war room simulation.
    
    Implements:
    - step(action) -> (observation, reward, done, info)
    - reset() -> observation
    - state() -> current state (partially observable)
    """

    def __init__(self, task_config: Optional[Dict] = None, seed: int = 0):
        self.seed_val = seed
        random.seed(seed)
        np.random.seed(seed)

        self.task_config = task_config or TaskGenerator.generate_easy_task(seed)
        self._reset_state()

    def _reset_state(self):
        """Initialize environment state"""
        self.current_step = 0
        self.max_steps = self.task_config.get("max_steps", 20)
        self.damage_score = safe_unit_score(0.0)
        self.resolved_incidents = []
        self.incorrect_resolutions = []
        self.prioritized_incidents = []
        self.actions_log = []

        # Initialize services
        self.services_status = {
            ServiceName.AUTH: ServiceStatus(
                name=ServiceName.AUTH,
                is_healthy=True,
                latency_ms=50.0,
                error_rate=0.0,
                cpu_percent=20.0,
                memory_percent=30.0,
                uptime_seconds=86400,
            ),
            ServiceName.PAYMENTS: ServiceStatus(
                name=ServiceName.PAYMENTS,
                is_healthy=True,
                latency_ms=100.0,
                error_rate=0.0,
                cpu_percent=25.0,
                memory_percent=35.0,
                uptime_seconds=86400,
            ),
            ServiceName.DB: ServiceStatus(
                name=ServiceName.DB,
                is_healthy=True,
                latency_ms=20.0,
                error_rate=0.0,
                cpu_percent=30.0,
                memory_percent=50.0,
                uptime_seconds=86400,
            ),
            ServiceName.CACHE: ServiceStatus(
                name=ServiceName.CACHE,
                is_healthy=True,
                latency_ms=5.0,
                error_rate=0.0,
                cpu_percent=15.0,
                memory_percent=40.0,
                uptime_seconds=86400,
            ),
            ServiceName.API_GATEWAY: ServiceStatus(
                name=ServiceName.API_GATEWAY,
                is_healthy=True,
                latency_ms=150.0,
                error_rate=0.0,
                cpu_percent=40.0,
                memory_percent=45.0,
                uptime_seconds=86400,
            ),
        }

        # All logs (hidden)
        self.all_logs = []
        
        # Alerts
        self.alerts = []
        
        # Generate incident based on task
        self._initialize_incident()

    def _initialize_incident(self):
        """Initialize incident scenario from task configuration"""
        scenario = self.task_config.get("scenario", {})
        root_cause = scenario.get("root_cause", "unknown")
        
        # Store root causes
        self.root_causes = {
            root_cause: scenario.get("name", "Unknown incident")
        }
        
        # Update services to reflect incident
        if "affected_service" in scenario:
            service = scenario["affected_service"]
            if service in self.services_status:
                status = self.services_status[service]
                status.is_healthy = False
                status.error_rate = 0.45
                status.latency_ms = 5000.0
                
                # Create alert
                alert = Alert(
                    alert_id=f"alert_{root_cause}",
                    service=service,
                    severity="critical",
                    message=scenario.get("name", "Critical incident"),
                    timestamp=self.current_step,
                    is_resolved=False,
                )
                self.alerts.append(alert)
        
        # Add affected services from cascade
        for service in scenario.get("cascade_to", []):
            if service in self.services_status:
                status = self.services_status[service]
                status.error_rate = 0.2
                status.latency_ms = 1000.0

        # Generate initial logs
        self._generate_logs()

    def _generate_logs(self):
        """Generate both relevant and misleading logs"""
        scenario = self.task_config.get("scenario", {})
        
        # Relevant logs
        for service, logs in scenario.get("logs", {}).items():
            if isinstance(service, str):
                try:
                    service = ServiceName[service.upper()]
                except KeyError:
                    continue
            for log_msg in logs:
                level = "ERROR" if "ERROR" in log_msg else "WARN"
                log = LogEntry(
                    timestamp=self.current_step,
                    service=service,
                    level=level,
                    message=log_msg,
                    trace_id=f"trace_{random.randint(1000, 9999)}",
                    is_relevant=True,
                )
                self.all_logs.append(log)

        # Misleading logs (noisy signals)
        for service_name, log_msg in scenario.get("misleading_logs", []):
            try:
                if isinstance(service_name, str):
                    service = ServiceName[service_name.upper()]
                else:
                    service = service_name
            except (KeyError, ValueError):
                continue
                
            log = LogEntry(
                timestamp=self.current_step,
                service=service,
                level="WARN" if "WARN" in log_msg else "INFO",
                message=log_msg,
                trace_id=f"trace_{random.randint(1000, 9999)}",
                is_relevant=False,
            )
            self.all_logs.append(log)

    def reset(self) -> Observation:
        """
        Reset environment and return initial observation.
        OpenEnv required method.
        """
        self._reset_state()
        return self._get_observation()

    def _get_observation(self) -> Observation:
        """Get partially observable state"""
        # Return recent logs only (partial observability)
        visible_logs = [
            log for log in self.all_logs[-20:]
            if random.random() < 0.8  # 80% chance to see a log
        ]
        
        return Observation(
            alerts=self.alerts,
            services_status=list(self.services_status.values()),
            recent_logs=visible_logs,
            active_incidents=list(self.root_causes.keys()),
            metrics_summary=self._compute_metrics_summary(),
            current_step=self.current_step,
            damage_score=safe_unit_score(self.damage_score),
            available_actions=[a.value for a in ActionType],
        )

    def _compute_metrics_summary(self) -> Dict[str, Dict[str, float]]:
        """Compute aggregate metrics"""
        summary = {}
        for service_name, status in self.services_status.items():
            summary[service_name.value] = {
                "latency_ms": status.latency_ms,
                "error_rate": status.error_rate,
                "cpu_percent": status.cpu_percent,
                "memory_percent": status.memory_percent,
            }
        return summary

    def step(self, action: Action) -> Tuple[Observation, Reward, bool, Dict]:
        """
        Execute action and return (observation, reward, done, info).
        OpenEnv required method.
        """
        self.current_step += 1
        reward_value = 0.0
        reward_info = {"components": {}}

        action_dict = {
            "step": self.current_step,
            "action_type": action.action_type.value,
            "service": action.service.value if action.service else None,
            "metric": action.metric.value if action.metric else None,
            "trace_id": action.trace_id,
            "incident_id": action.incident_id,
            "root_cause": action.root_cause,
            "replicas": action.replicas,
            "version": action.version,
        }
        self.actions_log.append(action_dict)

        # Process action
        if action.action_type == ActionType.QUERY_LOGS:
            reward_value, info = self._handle_query_logs(action.service)
            reward_info.update(info)

        elif action.action_type == ActionType.QUERY_METRICS:
            reward_value, info = self._handle_query_metrics(action.service, action.metric)
            reward_info.update(info)

        elif action.action_type == ActionType.RESTART_SERVICE:
            reward_value, info = self._handle_restart_service(action.service)
            reward_info.update(info)

        elif action.action_type == ActionType.ROLLBACK_DEPLOYMENT:
            reward_value, info = self._handle_rollback(action.service)
            reward_info.update(info)

        elif action.action_type == ActionType.RESOLVE_INCIDENT:
            reward_value, info = self._handle_resolve_incident(action.root_cause)
            reward_info.update(info)

        elif action.action_type == ActionType.SCALE_SERVICE:
            reward_value, info = self._handle_scale_service(action.service, action.replicas)
            reward_info.update(info)

        elif action.action_type == ActionType.TRACE_REQUEST:
            reward_value, info = self._handle_trace_request(action.trace_id)
            reward_info.update(info)

        elif action.action_type == ActionType.PRIORITIZE_INCIDENT:
            reward_value, info = self._handle_prioritize_incident(action.incident_id)
            reward_info.update(info)

        else:
            # Unknown action
            reward_value = -0.1
            reward_info["reason"] = "unknown_action"

        # Update system state (degradation if unresolved)
        self._update_system_state()

        # Check termination
        done = self._check_done()

        observation = self._get_observation()
        reward = Reward(
            value=reward_value,
            components=reward_info.get("components", {}),
            info={"step": self.current_step, **reward_info}
        )

        return observation, reward, done, reward_info

    def _handle_query_logs(self, service: Optional[ServiceName]) -> Tuple[float, Dict]:
        """Query logs from service"""
        if service is None:
            return -0.05, {
                "action": "query_logs",
                "service": None,
                "result": "missing_service",
                "components": {"validation": -0.05},
                "note": "Choose a service before querying logs.",
            }

        reward = 0.1  # Small reward for exploration
        info = {
            "action": "query_logs",
            "service": service.value,
            "components": {"exploration": 0.1}
        }

        # Check if querying relevant service
        if "db_connection" in self.root_causes and service == ServiceName.DB:
            reward = 0.2
            info["components"]["relevant_query"] = 0.1
            info["found_relevant_logs"] = True
        
        return reward, info

    def _handle_query_metrics(self, service: Optional[ServiceName], metric: Optional[MetricType]) -> Tuple[float, Dict]:
        """Query metrics from service"""
        if service is None:
            return -0.05, {
                "action": "query_metrics",
                "service": None,
                "metric": metric.value if metric else None,
                "result": "missing_service",
                "components": {"validation": -0.05},
                "note": "Choose a service before querying metrics.",
            }

        reward = 0.1
        info = {
            "action": "query_metrics",
            "service": service.value,
            "metric": metric.value if metric else None,
            "components": {"exploration": 0.1}
        }
        
        # Reward for querying abnormal metrics
        status = self.services_status.get(service)
        if status and status.error_rate > 0.1:
            reward = 0.15
            info["components"]["found_anomaly"] = 0.05

        return reward, info

    def _handle_restart_service(self, service: Optional[ServiceName]) -> Tuple[float, Dict]:
        """Attempt to restart service"""
        if service is None:
            return -0.1, {
                "action": "restart_service",
                "service": None,
                "result": "missing_service",
                "components": {"validation": -0.1},
                "note": "Choose a service before restarting it.",
            }

        reward = 0.0
        info = {
            "action": "restart_service",
            "service": service.value,
            "components": {}
        }

        status = self.services_status.get(service)
        if status:
            if status.is_healthy:
                # Restarting healthy service = bad
                reward = -0.2
                status.latency_ms *= 1.5  # Temporary increase
                self.damage_score = safe_unit_score(self.damage_score + 0.1)
                info["reason"] = "service_was_healthy"
                info["components"]["damage"] = -0.2
            else:
                # Restarting unhealthy service
                # May temporarily fix symptoms but not root cause
                status.is_healthy = True
                status.error_rate = 0.0
                reward = 0.25
                info["components"]["symptom_fix"] = 0.25
                info["note"] = "Fixes symptoms, not root cause"

        return reward, info

    def _handle_rollback(self, service: Optional[ServiceName]) -> Tuple[float, Dict]:
        """Rollback service to previous version"""
        if service is None:
            return -0.1, {
                "action": "rollback_deployment",
                "service": None,
                "result": "missing_service",
                "components": {"validation": -0.1},
                "note": "Choose a service before attempting a rollback.",
            }

        reward = 0.0
        info = {
            "action": "rollback_deployment",
            "service": service.value,
            "components": {}
        }

        # Check if rollback fixes root cause
        root_cause_key = f"{service.value}_bad_deployment"
        if root_cause_key in self.root_causes:
            reward = 0.5
            status = self.services_status.get(service)
            if status:
                status.is_healthy = True
                status.error_rate = 0.0
            info["components"]["root_cause_resolved"] = 0.5
            info["note"] = "Root cause fixed via rollback"
        else:
            reward = -0.15  # Unnecessary rollback
            self.damage_score = safe_unit_score(self.damage_score + 0.05)
            info["note"] = "Rollback not needed"

        return reward, info

    def _handle_resolve_incident(self, root_cause: Optional[str]) -> Tuple[float, Dict]:
        """Resolve incident by identifying root cause"""
        reward = 0.0
        info = {
            "action": "resolve_incident",
            "provided_root_cause": root_cause,
            "components": {}
        }

        if root_cause and root_cause in self.root_causes:
            # Correct root cause identified
            reward = 0.6
            self.resolved_incidents.append(root_cause)
            info["components"]["correct_diagnosis"] = 0.6
            info["result"] = "correct"
            
            # Mark alerts as resolved
            for alert in self.alerts:
                if root_cause in alert.alert_id or root_cause in alert.message.lower():
                    alert.is_resolved = True
        else:
            # Incorrect diagnosis
            reward = -0.3
            if root_cause:
                self.incorrect_resolutions.append(root_cause)
            self.damage_score = safe_unit_score(self.damage_score + 0.05)
            info["components"]["wrong_diagnosis"] = -0.3
            info["result"] = "incorrect"

        return reward, info

    def _handle_scale_service(self, service: Optional[ServiceName], replicas: Optional[int]) -> Tuple[float, Dict]:
        """Scale service to increase capacity"""
        if service is None:
            return -0.1, {
                "action": "scale_service",
                "service": None,
                "replicas": replicas,
                "result": "missing_service",
                "components": {"validation": -0.1},
                "note": "Choose a service before scaling it.",
            }

        reward = 0.0
        info = {
            "action": "scale_service",
            "service": service.value,
            "replicas": replicas,
            "components": {}
        }

        # Scaling API gateway for traffic surge is helpful
        if service == ServiceName.API_GATEWAY and replicas and replicas > 3:
            status = self.services_status.get(service)
            if status:
                status.cpu_percent = max(20, status.cpu_percent - 20)
                status.latency_ms = max(50, status.latency_ms - 200)
            reward = 0.2
            info["components"]["helpful_scaling"] = 0.2
        else:
            reward = 0.05
            info["components"]["neutral_action"] = 0.05

        return reward, info

    def _handle_trace_request(self, trace_id: Optional[str]) -> Tuple[float, Dict]:
        """Trace a distributed request through available logs."""
        info = {
            "action": "trace_request",
            "trace_id": trace_id,
            "components": {"exploration": 0.05},
        }

        if not trace_id:
            info["result"] = "missing_trace_id"
            return -0.05, info

        for log in reversed(self.all_logs):
            if log.trace_id == trace_id:
                info["result"] = "trace_found"
                info["service"] = log.service.value
                info["message"] = log.message
                info["is_relevant"] = log.is_relevant
                info["components"]["trace_found"] = 0.15
                return 0.2, info

        info["result"] = "trace_not_found"
        return 0.05, info

    def _handle_prioritize_incident(self, incident_id: Optional[str]) -> Tuple[float, Dict]:
        """Prioritize an incident for investigation."""
        info = {
            "action": "prioritize_incident",
            "incident_id": incident_id,
            "components": {"coordination": 0.05},
        }

        if not incident_id:
            info["result"] = "missing_incident_id"
            return -0.05, info

        if incident_id in self.root_causes:
            if incident_id not in self.prioritized_incidents:
                self.prioritized_incidents.append(incident_id)
            info["result"] = "prioritized"
            info["components"]["relevance"] = 0.05
            return 0.05, info

        info["result"] = "unknown_incident"
        return -0.05, info

    def _update_system_state(self):
        """Update system state over time (degradation if unresolved)"""
        # Degradation from unresolved incidents
        unresolved_count = len(self.root_causes) - len(self.resolved_incidents)
        if unresolved_count > 0:
            degradation_rate = 0.02 * unresolved_count
            self.damage_score = safe_unit_score(self.damage_score + degradation_rate)
            
            # Services degrade
            for service_name, status in self.services_status.items():
                if not status.is_healthy:
                    status.error_rate = min(0.95, status.error_rate + 0.05)
                    status.latency_ms *= 1.05

    def _check_done(self) -> bool:
        """Check if episode is done"""
        # Done if all incidents resolved or max steps reached
        if len(self.resolved_incidents) == len(self.root_causes):
            return True
        if self.current_step >= self.max_steps:
            return True
        return False

    def state(self) -> State:
        """
        Return complete state (for analysis/debugging).
        OpenEnv required method.
        """
        return State(
            # Observable
            alerts=self.alerts,
            services_status=list(self.services_status.values()),
            recent_logs=[log for log in self.all_logs[-20:]],
            active_incidents=list(self.root_causes.keys()),
            metrics_summary=self._compute_metrics_summary(),
            current_step=self.current_step,
            damage_score=safe_unit_score(self.damage_score),
            
            # Hidden
            root_causes=self.root_causes,
            dependency_graph={},
            incident_history=[],
            all_logs=self.all_logs,
            true_metrics=self._compute_metrics_summary(),
            
            # Task state
            task_config=TaskDefinition(
                task_id=self.task_config.get("task_id", "unknown"),
                difficulty=self.task_config.get("difficulty", "easy"),
                name=self.task_config.get("name", "Unknown"),
                description=self.task_config.get("description", ""),
                max_steps=self.max_steps,
                num_incidents=self.task_config.get("num_incidents", 1),
                has_cascading_failures=self.task_config.get("has_cascading_failures", False),
                num_misleading_logs=self.task_config.get("num_misleading_logs", 0),
                grader=self.task_config.get("grader"),
            ),
            resolved_incidents=self.resolved_incidents,
            incorrect_resolutions=self.incorrect_resolutions,
            prioritized_incidents=self.prioritized_incidents,
        )

    def get_grade(self) -> Dict[str, float]:
        """Get final grade for completed episode"""
        state = self.state()
        difficulty = self.task_config.get("difficulty", "easy")
        grader = get_grader_for_difficulty(difficulty)

        # Prepare grading inputs based on difficulty
        if difficulty == "easy":
            result = grader.grade(
                resolved_correctly=len(self.resolved_incidents) > 0,
                steps_taken=self.current_step,
                damage_score=self.damage_score,
                actions_log=self.actions_log,
            )
        elif difficulty == "medium":
            result = grader.grade(
                resolved_correctly=len(self.resolved_incidents) == len(self.root_causes),
                steps_taken=self.current_step,
                damage_score=self.damage_score,
                actions_log=self.actions_log,
                incorrect_diagnoses=len(self.incorrect_resolutions),
            )
        else:  # hard
            result = grader.grade(
                resolved_correctly=len(self.resolved_incidents) == len(self.root_causes),
                root_causes_identified=self.resolved_incidents,
                expected_root_causes=list(self.root_causes.keys()),
                steps_taken=self.current_step,
                damage_score=self.damage_score,
                actions_log=self.actions_log,
            )

        details = dict(result.details)
        if "damage_score" in details:
            details["damage_score"] = safe_unit_score(details["damage_score"])

        return {
            "score": safe_task_score(result.score),
            "correctness": safe_task_score(result.correctness),
            "efficiency": safe_task_score(result.efficiency),
            "damage": safe_task_score(result.damage),
            "details": details,
        }


# Convenience function
def make_env(task_id: str = "easy_0", seed: int = 0) -> DevOpsWarRoomEnv:
    """Create environment for given task ID"""
    task_config = TASK_DEFINITIONS.get(task_id)
    if not task_config:
        task_config = TaskGenerator.generate_easy_task(seed)
    return DevOpsWarRoomEnv(task_config=task_config, seed=seed)
