"""
DevOps War Room - Deterministic Graders
Score agent performance based on efficiency, correctness, and damage minimization
"""

from typing import Dict, List, Any
from dataclasses import dataclass


@dataclass
class GradeResult:
    """Result of grading an episode"""
    score: float  # strictly (0.0, 1.0) — never exactly 0 or 1
    correctness: float
    efficiency: float
    damage: float
    components: Dict[str, float]
    details: Dict[str, Any]


class BaseGrader:
    """Base grader with common scoring logic"""

    EPSILON = 1e-3

    @classmethod
    def normalize_score(cls, value: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
        """Normalize value to strict open interval (0, 1)"""
        normalized = (value - min_val) / (max_val - min_val)
        return cls.clamp_open_interval(normalized)

    @staticmethod
    def compute_final_score(
        correctness: float,
        efficiency: float,
        damage: float,
        correctness_weight: float = 0.5,
        efficiency_weight: float = 0.3,
        damage_weight: float = 0.2,
    ) -> float:
        """Weighted combination of scoring components"""
        return (
            correctness * correctness_weight +
            efficiency * efficiency_weight +
            damage * damage_weight
        )

    @classmethod
    def clamp_open_interval(cls, value: float) -> float:
        """Clamp values into the strict open interval (0, 1)."""
        return min(1.0 - cls.EPSILON, max(cls.EPSILON, value))


class EasyTaskGrader(BaseGrader):
    """Grader for easy difficulty tasks"""

    def grade(
        self,
        resolved_correctly: bool,
        steps_taken: int,
        damage_score: float,
        actions_log: List[Dict[str, Any]],
        **kwargs
    ) -> GradeResult:
        """
        Grade easy task performance
        
        Easy tasks focus on:
        - Correctness (did agent identify right service?)
        - Efficiency (how many steps to resolve?)
        - Damage (how much did system degrade?)
        """
        max_steps_expected = 15
        
        # Correctness: binary but with partial credit for progress
        if resolved_correctly:
            correctness = 1.0
        else:
            # Partial credit if agent queried relevant services
            relevant_queries = sum(
                1 for action in actions_log
                if action.get("action_type") == "query_logs"
            )
            correctness = min(0.7, relevant_queries * 0.15)
        
        # Efficiency: penalize excessive steps
        efficiency = max(0.0, 1.0 - (steps_taken / max_steps_expected))
        
        # Damage: minimize system degradation
        damage = max(0.0, 1.0 - damage_score)
        
        # Combine scores
        final_score = self.compute_final_score(
            correctness, efficiency, damage,
            correctness_weight=0.5,
            efficiency_weight=0.3,
            damage_weight=0.2,
        )
        
        return GradeResult(
            score=self.clamp_open_interval(final_score),
            correctness=self.clamp_open_interval(correctness),
            efficiency=self.clamp_open_interval(efficiency),
            damage=self.clamp_open_interval(damage),
            components={
                "correctness": self.clamp_open_interval(correctness),
                "efficiency": self.clamp_open_interval(efficiency),
                "damage": self.clamp_open_interval(damage),
            },
            details={
                "task_difficulty": "easy",
                "resolved_correctly": resolved_correctly,
                "steps_taken": steps_taken,
                "max_steps_expected": max_steps_expected,
                "damage_score": damage_score,
            }
        )


class MediumTaskGrader(BaseGrader):
    """Grader for medium difficulty tasks"""

    def grade(
        self,
        resolved_correctly: bool,
        steps_taken: int,
        damage_score: float,
        actions_log: List[Dict[str, Any]],
        incorrect_diagnoses: int = 0,
        **kwargs
    ) -> GradeResult:
        """
        Grade medium task performance
        
        Medium tasks focus on:
        - Correctness (navigating misleading signals)
        - Efficiency (systematic exploration vs brute force)
        - Damage (avoiding cascading failures)
        """
        max_steps_expected = 25
        
        # Correctness: penalize incorrect diagnoses
        if resolved_correctly:
            correctness = 1.0 - (incorrect_diagnoses * 0.15)
        else:
            # Partial credit for relevant actions
            relevant_queries = sum(
                1 for action in actions_log
                if action.get("action_type") in ["query_logs", "query_metrics"]
            )
            correct_services = sum(
                1 for action in actions_log
                if action.get("service") in ["api_gateway", "cache"]  # For this scenario
            )
            correctness = max(0.0, min(0.6, (relevant_queries * 0.1 + correct_services * 0.2)))
        
        correctness = max(0.0, min(1.0, correctness))
        
        # Efficiency: penalize inefficient exploration
        efficiency = max(0.0, 1.0 - (steps_taken / max_steps_expected))
        
        # Damage: heavily penalize cascade failures
        damage = max(0.0, 1.0 - damage_score * 1.5)
        
        final_score = self.compute_final_score(
            correctness, efficiency, damage,
            correctness_weight=0.5,
            efficiency_weight=0.25,
            damage_weight=0.25,
        )
        
        return GradeResult(
            score=self.clamp_open_interval(final_score),
            correctness=self.clamp_open_interval(correctness),
            efficiency=self.clamp_open_interval(efficiency),
            damage=self.clamp_open_interval(damage),
            components={
                "correctness": self.clamp_open_interval(correctness),
                "efficiency": self.clamp_open_interval(efficiency),
                "damage": self.clamp_open_interval(damage),
            },
            details={
                "task_difficulty": "medium",
                "resolved_correctly": resolved_correctly,
                "steps_taken": steps_taken,
                "max_steps_expected": max_steps_expected,
                "damage_score": damage_score,
                "incorrect_diagnoses": incorrect_diagnoses,
            }
        )


class HardTaskGrader(BaseGrader):
    """Grader for hard difficulty tasks"""

    def grade(
        self,
        resolved_correctly: bool,
        root_causes_identified: List[str],
        expected_root_causes: List[str],
        steps_taken: int,
        damage_score: float,
        actions_log: List[Dict[str, Any]],
        resolution_sequence_optimal: bool = False,
        **kwargs
    ) -> GradeResult:
        """
        Grade hard task performance
        
        Hard tasks focus on:
        - Correctness (identify multiple root causes)
        - Efficiency (few steps, minimal backtracking)
        - Damage (avoid cascading failures)
        - Sequence (correct order of fixes)
        """
        max_steps_expected = 30
        
        # Correctness: identify all root causes
        correct_count = sum(
            1 for rc in root_causes_identified
            if rc in expected_root_causes
        )
        total_expected = len(expected_root_causes)
        correctness = correct_count / max(1, total_expected) if total_expected > 0 else 0.5
        
        # Bonus for sequence optimality
        if resolution_sequence_optimal:
            correctness = min(1.0, correctness * 1.1)
        
        correctness = max(0.0, min(1.0, correctness))
        
        # Efficiency: heavily penalize excess steps
        efficiency = max(0.0, 1.0 - (steps_taken / max_steps_expected) * 1.2)
        
        # Damage: critical for hard tasks
        damage = max(0.0, 1.0 - damage_score * 2.0)
        
        final_score = self.compute_final_score(
            correctness, efficiency, damage,
            correctness_weight=0.45,
            efficiency_weight=0.25,
            damage_weight=0.3,
        )
        
        return GradeResult(
            score=self.clamp_open_interval(final_score),
            correctness=self.clamp_open_interval(correctness),
            efficiency=self.clamp_open_interval(efficiency),
            damage=self.clamp_open_interval(damage),
            components={
                "correctness": self.clamp_open_interval(correctness),
                "efficiency": self.clamp_open_interval(efficiency),
                "damage": self.clamp_open_interval(damage),
            },
            details={
                "task_difficulty": "hard",
                "resolved_correctly": resolved_correctly,
                "root_causes_identified": root_causes_identified,
                "expected_root_causes": expected_root_causes,
                "steps_taken": steps_taken,
                "max_steps_expected": max_steps_expected,
                "damage_score": damage_score,
                "sequence_optimal": resolution_sequence_optimal,
            }
        )


# Grader instantiation
EASY_GRADER = EasyTaskGrader()
MEDIUM_GRADER = MediumTaskGrader()
HARD_GRADER = HardTaskGrader()


def get_grader_for_difficulty(difficulty: str):
    """Get grader for task difficulty"""
    if difficulty == "easy":
        return EASY_GRADER
    elif difficulty == "medium":
        return MEDIUM_GRADER
    elif difficulty == "hard":
        return HARD_GRADER
    else:
        return EASY_GRADER