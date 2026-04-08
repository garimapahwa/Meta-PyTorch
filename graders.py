from typing import Dict, List, Any
from dataclasses import dataclass


@dataclass
class GradeResult:
    score: float
    correctness: float
    efficiency: float
    damage: float
    components: Dict[str, float]
    details: Dict[str, Any]


class BaseGrader:
    EPSILON = 0.1

    @classmethod
    def safe_score(cls, value: float) -> float:
        if value is None:
            return cls.EPSILON
        return max(cls.EPSILON, min(1.0 - cls.EPSILON, float(value)))

    @classmethod
    def normalize_score(cls, value: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
        if max_val <= min_val:
            return cls.EPSILON
        normalized = (value - min_val) / (max_val - min_val)
        return cls.safe_score(normalized)

    @staticmethod
    def compute_final_score(
        correctness: float,
        efficiency: float,
        damage: float,
        correctness_weight: float = 0.5,
        efficiency_weight: float = 0.3,
        damage_weight: float = 0.2,
    ) -> float:
        return (
            correctness * correctness_weight +
            efficiency * efficiency_weight +
            damage * damage_weight
        )

    @classmethod
    def clamp_open_interval(cls, value: float) -> float:
        return cls.safe_score(value)


class EasyTaskGrader(BaseGrader):
    def grade(
        self,
        resolved_correctly: bool,
        steps_taken: int,
        damage_score: float,
        actions_log: List[Dict[str, Any]],
        **kwargs
    ) -> GradeResult:
        max_steps_expected = 15

        if resolved_correctly:
            correctness_raw = 0.9
        else:
            relevant_queries = sum(
                1 for action in actions_log
                if action.get("action_type") == "query_logs"
            )
            correctness_raw = min(0.7, relevant_queries * 0.15)

        correctness = self.safe_score(correctness_raw)

        efficiency_raw = 1.0 - (steps_taken / max_steps_expected)
        efficiency = self.safe_score(efficiency_raw)

        damage_raw = 1.0 - damage_score
        damage = self.safe_score(damage_raw)

        final_score_raw = self.compute_final_score(correctness, efficiency, damage)
        final_score = self.safe_score(final_score_raw)

        return GradeResult(
            score=final_score,
            correctness=correctness,
            efficiency=efficiency,
            damage=damage,
            components={
                "correctness": correctness,
                "efficiency": efficiency,
                "damage": damage,
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
    def grade(
        self,
        resolved_correctly: bool,
        steps_taken: int,
        damage_score: float,
        actions_log: List[Dict[str, Any]],
        incorrect_diagnoses: int = 0,
        **kwargs
    ) -> GradeResult:
        max_steps_expected = 25

        if resolved_correctly:
            correctness_raw = 0.9 - (incorrect_diagnoses * 0.15)
        else:
            relevant_queries = sum(
                1 for action in actions_log
                if action.get("action_type") in ["query_logs", "query_metrics"]
            )
            correct_services = sum(
                1 for action in actions_log
                if action.get("service") in ["api_gateway", "cache"]
            )
            correctness_raw = min(0.6, (relevant_queries * 0.1 + correct_services * 0.2))

        correctness = self.safe_score(correctness_raw)

        efficiency_raw = 1.0 - (steps_taken / max_steps_expected)
        efficiency = self.safe_score(efficiency_raw)

        damage_raw = 1.0 - (damage_score * 1.5)
        damage = self.safe_score(damage_raw)

        final_score_raw = self.compute_final_score(
            correctness, efficiency, damage,
            correctness_weight=0.5,
            efficiency_weight=0.25,
            damage_weight=0.25,
        )
        final_score = self.safe_score(final_score_raw)

        return GradeResult(
            score=final_score,
            correctness=correctness,
            efficiency=efficiency,
            damage=damage,
            components={
                "correctness": correctness,
                "efficiency": efficiency,
                "damage": damage,
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
        max_steps_expected = 30

        total_expected = max(1, len(expected_root_causes))
        correct_count = sum(
            1 for rc in root_causes_identified
            if rc in expected_root_causes
        )

        correctness_raw = correct_count / total_expected
        if resolution_sequence_optimal:
            correctness_raw *= 0.99

        correctness = self.safe_score(correctness_raw)

        efficiency_raw = 1.0 - (steps_taken / max_steps_expected)
        efficiency = self.safe_score(efficiency_raw)

        damage_raw = 1.0 - (damage_score * 1.5)
        damage = self.safe_score(damage_raw)

        final_score_raw = self.compute_final_score(
            correctness, efficiency, damage,
            correctness_weight=0.45,
            efficiency_weight=0.30,
            damage_weight=0.25,
        )
        final_score = self.safe_score(final_score_raw)

        return GradeResult(
            score=final_score,
            correctness=correctness,
            efficiency=efficiency,
            damage=damage,
            components={
                "correctness": correctness,
                "efficiency": efficiency,
                "damage": damage,
            },
            details={
                "task_difficulty": "hard",
                "resolved_correctly": resolved_correctly,
                "root_causes_identified": root_causes_identified,
                "expected_root_causes": expected_root_causes,
                "steps_taken": steps_taken,
                "max_steps_expected": max_steps_expected,
                "damage_score": damage_score,
                "resolution_sequence_optimal": resolution_sequence_optimal,
            }
        )


EASY_GRADER = EasyTaskGrader()
MEDIUM_GRADER = MediumTaskGrader()
HARD_GRADER = HardTaskGrader()


def get_grader_for_difficulty(difficulty: str):
    """Return the grader instance for a task difficulty."""
    if difficulty == "easy":
        return EASY_GRADER
    if difficulty == "medium":
        return MEDIUM_GRADER
    if difficulty == "hard":
        return HARD_GRADER
    return EASY_GRADER
