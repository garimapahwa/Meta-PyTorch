from decimal import Decimal, ROUND_HALF_UP
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
    SCORE_EPSILON = 1e-4
    GRADE_MIN_EXCLUSIVE = 0.1
    GRADE_MAX_EXCLUSIVE = 0.9
    MIN_SCORE = GRADE_MIN_EXCLUSIVE + SCORE_EPSILON
    MAX_SCORE = GRADE_MAX_EXCLUSIVE - SCORE_EPSILON

    @classmethod
    def clamp_open_interval(
        cls,
        value: float,
        min_exclusive: float = 0.0,
        max_exclusive: float = 1.0,
    ) -> float:
        if max_exclusive <= min_exclusive:
            midpoint = (min_exclusive + max_exclusive) / 2.0
            return float(midpoint)

        safe_min = min_exclusive + cls.SCORE_EPSILON
        safe_max = max_exclusive - cls.SCORE_EPSILON
        if value is None:
            return safe_min
        return max(safe_min, min(safe_max, float(value)))

    @classmethod
    def safe_score(cls, value: float) -> float:
        return cls.clamp_open_interval(
            value,
            min_exclusive=cls.GRADE_MIN_EXCLUSIVE,
            max_exclusive=cls.GRADE_MAX_EXCLUSIVE,
        )

    @classmethod
    def safe_unit_interval(cls, value: float) -> float:
        return cls.clamp_open_interval(value, min_exclusive=0.0, max_exclusive=1.0)

    @staticmethod
    def quantize_for_output(
        value: float,
        digits: int = 1,
        min_value: float = 0.1,
        max_value: float = 0.9,
    ) -> float:
        if value is None:
            return min_value

        quantizer = "0." + ("0" * max(0, digits - 1)) + "1"
        rounded = float(
            Decimal(str(float(value))).quantize(Decimal(quantizer), rounding=ROUND_HALF_UP)
        )
        return max(min_value, min(max_value, rounded))

    @classmethod
    def normalize_score(cls, value: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
        if max_val <= min_val:
            return cls.MIN_SCORE
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
        """Weighted combination of scoring components"""
        score = (
            correctness * correctness_weight +
            efficiency * efficiency_weight +
            damage * damage_weight
        )
        return BaseGrader.safe_score(score)


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

        final_score = self.compute_final_score(correctness, efficiency, damage)

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
                "damage_score": safe_display_score(damage_score),
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

        final_score = self.compute_final_score(
            correctness, efficiency, damage,
            correctness_weight=0.5,
            efficiency_weight=0.25,
            damage_weight=0.25,
        )

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
                "damage_score": safe_display_score(damage_score),
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

        final_score = self.compute_final_score(
            correctness, efficiency, damage,
            correctness_weight=0.45,
            efficiency_weight=0.30,
            damage_weight=0.25,
        )

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
                "damage_score": safe_display_score(damage_score),
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


def safe_task_score(value: float) -> float:
    """Clamp emitted grading outputs into a one-decimal score inside (0, 1)."""
    return BaseGrader.quantize_for_output(BaseGrader.safe_score(value))


def safe_display_score(value: float) -> float:
    """Clamp emitted score-like values into a one-decimal score inside (0, 1)."""
    return BaseGrader.quantize_for_output(BaseGrader.safe_unit_interval(value))


def safe_unit_score(value: float) -> float:
    """Clamp internal score-like metrics into the open unit interval."""
    return BaseGrader.safe_unit_interval(value)
