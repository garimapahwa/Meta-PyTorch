"""
DevOps War Room - Task Definitions
Three variants (easy, medium, hard) with deterministic incident generation
"""

import random
from typing import Dict, List, Tuple
from models import (
    ServiceName, Alert, LogEntry, TaskDefinition
)


class IncidentScenario:
    """Pre-defined incident scenarios with root causes and manifestations"""

    # Easy scenarios: single clear incident
    EASY_SCENARIOS = [
        {
            "name": "Database Connection Pool Exhausted",
            "root_cause": "db_connection_leak",
            "affected_service": ServiceName.DB,
            "cascade_to": [ServiceName.PAYMENTS, ServiceName.API_GATEWAY],
            "alerts": [
                "Database latency spike (>5000ms)",
                "Failed connections from payments service",
            ],
            "logs": {
                ServiceName.DB: [
                    "ERROR: Connection pool exhausted (1000/1000 active)",
                    "ERROR: New connection requests timing out",
                ],
                ServiceName.PAYMENTS: [
                    "ERROR: Failed to connect to database",
                    "ERROR: Checkout transaction failed",
                ]
            },
            "misleading_logs": [
                ("cache", "WARN: Cache hit rate dropped to 60%"),  # Not the issue
                ("auth", "INFO: User login spike (+200%)"),  # Normal seasonal traffic
            ]
        },
        {
            "name": "Payment Service Deployment Failed",
            "root_cause": "bad_payment_deployment",
            "affected_service": ServiceName.PAYMENTS,
            "cascade_to": [ServiceName.API_GATEWAY],
            "alerts": [
                "Payment service error rate 45%",
                "Checkout endpoint returning 500s",
            ],
            "logs": {
                ServiceName.PAYMENTS: [
                    "ERROR: NullPointerException in PaymentProcessor.process()",
                    "ERROR: Service startup failed: Missing configuration value",
                ],
            },
            "misleading_logs": [
                ("db", "INFO: Query response time 200ms (normal)"),
                ("cache", "INFO: Eviction policy triggered"),
            ]
        },
    ]

    # Medium scenarios: multiple services, misleading signals
    MEDIUM_SCENARIOS = [
        {
            "name": "Cache Failure + API Gateway Overload",
            "root_cause": "cache_backend_failure",
            "affected_services": [ServiceName.CACHE, ServiceName.API_GATEWAY],
            "cascade_to": [ServiceName.AUTH, ServiceName.PAYMENTS],
            "alerts": [
                "Cache service down",
                "API Gateway latency spike (>2000ms)",
                "Auth service latency increased",
            ],
            "logs": {
                ServiceName.CACHE: [
                    "ERROR: Redis connection lost",
                    "ERROR: Unable to reconnect to cache backend",
                ],
                ServiceName.API_GATEWAY: [
                    "WARN: Cache misses increasing (70%)",
                    "WARN: Queuing requests due to high latency",
                    "INFO: Rate limiting triggered",
                ],
                ServiceName.AUTH: [
                    "WARN: Session lookups timing out",
                    "WARN: Database hit rate increased",
                ]
            },
            "misleading_logs": [
                ("payments", "INFO: Transaction volume 15% above baseline"),
                ("db", "WARN: CPU at 75% (normal for this query volume)"),
            ]
        },
        {
            "name": "Traffic Spike + Inadequate Scaling",
            "root_cause": "insufficient_api_gateway_replicas",
            "affected_services": [ServiceName.API_GATEWAY],
            "cascade_to": [ServiceName.PAYMENTS, ServiceName.DB],
            "alerts": [
                "API Gateway CPU at 90%+",
                "API Gateway request queue depth > 10k",
                "Downstream services experiencing timeouts",
            ],
            "logs": {
                ServiceName.API_GATEWAY: [
                    "WARN: Request queue depth: 12000",
                    "ERROR: Requests timing out due to queue saturation",
                    "INFO: Incoming request rate: 5000 req/s",
                ],
                ServiceName.PAYMENTS: [
                    "ERROR: Timeout calling API Gateway",
                ]
            },
            "misleading_logs": [
                ("auth", "INFO: Auth service responding normally (<100ms)"),
                ("cache", "INFO: Cache operational, hit rate 85%"),
            ]
        },
    ]

    # Hard scenarios: cascading failures with tradeoffs
    HARD_SCENARIOS = [
        {
            "name": "Cascading Auth + Payment Failures",
            "root_cause": "auth_token_validation_bug",
            "primary_affected": ServiceName.AUTH,
            "cascade_sequence": [
                {
                    "step_range": (0, 3),
                    "service": ServiceName.AUTH,
                    "error": "Auth token validation returning 50% false negatives",
                    "impact": "Users cannot authenticate"
                },
                {
                    "step_range": (4, 7),
                    "service": ServiceName.PAYMENTS,
                    "error": "Payment failures due to invalid auth",
                    "impact": "Cascades from auth to payments"
                },
                {
                    "step_range": (8, 12),
                    "service": ServiceName.API_GATEWAY,
                    "error": "Queue saturates from retries",
                    "impact": "System-wide degradation"
                }
            ],
            "quick_fix_trap": {
                "description": "Restarting auth might help, but doesn't fix root issue",
                "penalty_delay_steps": 5,
            },
            "correct_fix": "Rollback auth to previous stable version",
            "misleading_logs": [
                ("db", "WARN: Connection pool at 85%"),
                ("cache", "INFO: Recent deployment completed"),
            ]
        },
        {
            "name": "Multi-Service Outage with Tradeoffs",
            "root_cause": "database_replication_lag",
            "affected": [ServiceName.DB, ServiceName.PAYMENTS, ServiceName.AUTH],
            "tradeoff_scenario": {
                "option_a": {
                    "action": "Restart DB primary (fixes consistency)",
                    "benefit": "Replication catches up",
                    "cost": "5 minute downtime",
                    "time_penalty": 50,  # Reduces reward
                },
                "option_b": {
                    "action": "Wait for replication to catch up",
                    "benefit": "Zero downtime",
                    "cost": "Services degraded for 10 steps",
                    "flexibility": True,  # Can switch to A later
                }
            },
            "misleading_logs": [
                ("api_gateway", "INFO: CPU usage declining"),
                ("cache", "WARN: Eviction rate increased"),
            ]
        }
    ]


class TaskGenerator:
    """Generate task-specific scenarios deterministically"""

    @staticmethod
    def grader_metadata(difficulty: str) -> Dict:
        return {
            "type": "deterministic",
            "difficulty": difficulty,
            "score_range": {
                "min_exclusive": 0.001,
                "max_exclusive": 0.999,
            },
        }

    @staticmethod
    def generate_easy_task(seed: int = 0) -> Dict:
        """Easy task: Single incident, clear logs"""
        random.seed(seed)
        scenario = IncidentScenario.EASY_SCENARIOS[seed % len(IncidentScenario.EASY_SCENARIOS)]
        
        return {
            "task_id": f"easy_{seed}",
            "difficulty": "easy",
            "name": scenario["name"],
            "description": f"Identify root cause: {scenario['name']}",
            "max_steps": 15,
            "num_incidents": 1,
            "has_cascading_failures": False,
            "num_misleading_logs": len(scenario.get("misleading_logs", [])),
            "grader": TaskGenerator.grader_metadata("easy"),
            "scenario": scenario,
        }

    @staticmethod
    def generate_medium_task(seed: int = 0) -> Dict:
        """Medium task: Multiple services, misleading signals"""
        random.seed(seed)
        scenario = IncidentScenario.MEDIUM_SCENARIOS[seed % len(IncidentScenario.MEDIUM_SCENARIOS)]
        
        return {
            "task_id": f"medium_{seed}",
            "difficulty": "medium",
            "name": scenario["name"],
            "description": f"Resolve multiple incidents: {scenario['name']}",
            "max_steps": 25,
            "num_incidents": 2,
            "has_cascading_failures": True,
            "num_misleading_logs": len(scenario.get("misleading_logs", [])),
            "grader": TaskGenerator.grader_metadata("medium"),
            "scenario": scenario,
        }

    @staticmethod
    def generate_hard_task(seed: int = 0) -> Dict:
        """Hard task: Cascading failures, tradeoffs"""
        random.seed(seed)
        scenario = IncidentScenario.HARD_SCENARIOS[seed % len(IncidentScenario.HARD_SCENARIOS)]
        
        return {
            "task_id": f"hard_{seed}",
            "difficulty": "hard",
            "name": scenario["name"],
            "description": f"Resolve complex incident: {scenario['name']}",
            "max_steps": 30,
            "num_incidents": 3,
            "has_cascading_failures": True,
            "num_misleading_logs": 4,
            "grader": TaskGenerator.grader_metadata("hard"),
            "scenario": scenario,
        }


TASK_DEFINITIONS = {
    "easy_0": TaskGenerator.generate_easy_task(seed=0),
    "easy_1": TaskGenerator.generate_easy_task(seed=1),
    "medium_0": TaskGenerator.generate_medium_task(seed=0),
    "medium_1": TaskGenerator.generate_medium_task(seed=1),
    "hard_0": TaskGenerator.generate_hard_task(seed=0),
    "hard_1": TaskGenerator.generate_hard_task(seed=1),
}
