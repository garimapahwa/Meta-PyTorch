#!/usr/bin/env python3
"""
Simulate the complete scoring flow to trace where 0.0 or 1.0 values leak through.
"""

from graders import EasyTaskGrader, MediumTaskGrader, HardTaskGrader, BaseGrader
from environment import make_env

def simulate_grade_flow():
    """Simulate a complete episode and trace all score values."""
    
    print("=" * 80)
    print("SCORING FLOW ANALYSIS")
    print("=" * 80)
    
    # Test case 1: Easy task, perfect resolution
    print("\n[TEST 1] Easy task - Perfect resolution (0 damage, few steps)")
    env = make_env(task_id="easy_0", seed=0)
    env.reset()
    
    # Simulate perfect episode: quickly resolve with no damage
    env.current_step = 3
    env.damage_score = 0.0
    env.resolved_incidents = ["test_incident"]
    env.actions_log = [
        {"action_type": "query_logs"},
        {"action_type": "query_logs"},
        {"action_type": "resolve_incident"},
    ]
    
    grade = env.get_grade()
    print(f"  Raw grade score: {grade['score']}")
    print(f"  Raw correctness: {grade['correctness']}")
    print(f"  Raw efficiency: {grade['efficiency']}")
    print(f"  Raw damage: {grade['damage']}")
    print(f"  Is score exactly 0.0? {grade['score'] == 0.0}")
    print(f"  Is score exactly 1.0? {grade['score'] == 1.0}")
    print(f"  Is correctness exactly 0.0? {grade['correctness'] == 0.0}")
    print(f"  Is correctness exactly 1.0? {grade['correctness'] == 1.0}")
    print(f"  Is efficiency exactly 0.0? {grade['efficiency'] == 0.0}")
    print(f"  Is efficiency exactly 1.0? {grade['efficiency'] == 1.0}")
    print(f"  Is damage exactly 0.0? {grade['damage'] == 0.0}")
    print(f"  Is damage exactly 1.0? {grade['damage'] == 1.0}")
    
    # Test case 2: Easy task, failed episode
    print("\n[TEST 2] Easy task - Failed resolution (high damage, many steps)")
    env2 = make_env(task_id="easy_0", seed=0)
    env2.reset()
    
    env2.current_step = 15
    env2.damage_score = 0.95
    env2.resolved_incidents = []
    env2.actions_log = []
    
    grade2 = env2.get_grade()
    print(f"  Raw grade score: {grade2['score']}")
    print(f"  Raw correctness: {grade2['correctness']}")
    print(f"  Raw efficiency: {grade2['efficiency']}")
    print(f"  Raw damage: {grade2['damage']}")
    print(f"  Is score exactly 0.0? {grade2['score'] == 0.0}")
    print(f"  Is score exactly 1.0? {grade2['score'] == 1.0}")
    print(f"  Is correctness exactly 0.0? {grade2['correctness'] == 0.0}")
    print(f"  Is correctness exactly 1.0? {grade2['correctness'] == 1.0}")
    print(f"  Is efficiency exactly 0.0? {grade2['efficiency'] == 0.0}")
    print(f"  Is efficiency exactly 1.0? {grade2['efficiency'] == 1.0}")
    print(f"  Is damage exactly 0.0? {grade2['damage'] == 0.0}")
    print(f"  Is damage exactly 1.0? {grade2['damage'] == 1.0}")

    # Test case 3: Check component clamping directly
    print("\n[TEST 3] Direct component clamping")
    print(f"  safe_score(0.0) = {BaseGrader.safe_score(0.0)}")
    print(f"  safe_score(1.0) = {BaseGrader.safe_score(1.0)}")
    print(f"  safe_score(0.5) = {BaseGrader.safe_score(0.5)}")
    print(f"  MIN_SCORE = {BaseGrader.MIN_SCORE}")
    print(f"  MAX_SCORE = {BaseGrader.MAX_SCORE}")

    # Test case 4: Check compute_final_score directly
    print("\n[TEST 4] Direct final score computation")
    min_val = BaseGrader.MIN_SCORE
    max_val = BaseGrader.MAX_SCORE
    mid_val = (min_val + max_val) / 2
    
    print(f"  compute_final_score({min_val}, {min_val}, {min_val}) = {BaseGrader.compute_final_score(min_val, min_val, min_val)}")
    print(f"  compute_final_score({max_val}, {max_val}, {max_val}) = {BaseGrader.compute_final_score(max_val, max_val, max_val)}")
    print(f"  compute_final_score({mid_val}, {mid_val}, {mid_val}) = {BaseGrader.compute_final_score(mid_val, mid_val, mid_val)}")
    print(f"  compute_final_score(0.0, 0.0, 0.0) = {BaseGrader.compute_final_score(0.0, 0.0, 0.0)}")
    print(f"  compute_final_score(1.0, 1.0, 1.0) = {BaseGrader.compute_final_score(1.0, 1.0, 1.0)}")

    # Test case 5: Simulation with API wrapper
    print("\n[TEST 5] API endpoint wrapping")
    def safe_openenv_score(value: float) -> float:
        min_score = 0.1 + 1e-4
        max_score = 0.9 - 1e-4
        return min(max_score, max(min_score, float(value)))
    
    test_values = [0.0, 0.1, 0.5, 0.9, 1.0, BaseGrader.MIN_SCORE, BaseGrader.MAX_SCORE]
    for val in test_values:
        wrapped = safe_openenv_score(val)
        print(f"  safe_openenv_score({val}) = {wrapped}, is 0.0? {wrapped == 0.0}, is 1.0? {wrapped == 1.0}")

    print("\n" + "=" * 80)

if __name__ == "__main__":
    simulate_grade_flow()
