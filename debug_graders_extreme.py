#!/usr/bin/env python3
"""
Check all graders with extreme scenarios to find any 0.0 or 1.0 outputs.
"""

from graders import EasyTaskGrader, MediumTaskGrader, HardTaskGrader

def check_all_graders():
    """Test all graders with extreme inputs."""
    
    print("=" * 80)
    print("ALL GRADERS EXTREME TEST")
    print("=" * 80)
    
    easy = EasyTaskGrader()
    medium = MediumTaskGrader()
    hard = HardTaskGrader()
    
    # Scenario 1: Perfect performance
    print("\n[SCENARIO 1] Perfect performance")
    easy_result = easy.grade(
        resolved_correctly=True,
        steps_taken=1,
        damage_score=0.0,
        actions_log=[{"action_type": "resolve_incident"}]
    )
    print(f"  Easy - score: {easy_result.score}")
    for key, val in easy_result.components.items():
        print(f"    {key}: {val}, == 0.0? {val == 0.0}, == 1.0? {val == 1.0}")
    
    # Scenario 2: Failed performance
    print("\n[SCENARIO 2] Failed performance (max damage, no resolution)")
    easy_result = easy.grade(
        resolved_correctly=False,
        steps_taken=15,
        damage_score=1.0,
        actions_log=[]
    )
    print(f"  Easy - score: {easy_result.score}")
    for key, val in easy_result.components.items():
        print(f"    {key}: {val}, == 0.0? {val == 0.0}, == 1.0? {val == 1.0}")
    
    # Scenario 3: Medium perfect
    print("\n[SCENARIO 3] Medium perfect")
    medium_result = medium.grade(
        resolved_correctly=True,
        steps_taken=1,
        damage_score=0.0,
        actions_log=[],
        incorrect_diagnoses=0
    )
    print(f"  Medium - score: {medium_result.score}")
    for key, val in medium_result.components.items():
        print(f"    {key}: {val}, == 0.0? {val == 0.0}, == 1.0? {val == 1.0}")
    
    # Scenario 4: Medium failed
    print("\n[SCENARIO 4] Medium failed")
    medium_result = medium.grade(
        resolved_correctly=False,
        steps_taken=25,
        damage_score=1.0,
        actions_log=[],
        incorrect_diagnoses=5
    )
    print(f"  Medium - score: {medium_result.score}")
    for key, val in medium_result.components.items():
        print(f"    {key}: {val}, == 0.0? {val == 0.0}, == 1.0? {val == 1.0}")
    
    # Scenario 5: Hard perfect
    print("\n[SCENARIO 5] Hard perfect")
    hard_result = hard.grade(
        resolved_correctly=True,
        root_causes_identified=["cause1", "cause2", "cause3"],
        expected_root_causes=["cause1", "cause2", "cause3"],
        steps_taken=1,
        damage_score=0.0,
        actions_log=[],
        resolution_sequence_optimal=True
    )
    print(f"  Hard - score: {hard_result.score}")
    for key, val in hard_result.components.items():
        print(f"    {key}: {val}, == 0.0? {val == 0.0}, == 1.0? {val == 1.0}")
    
    # Scenario 6: Hard failed
    print("\n[SCENARIO 6] Hard failed")
    hard_result = hard.grade(
        resolved_correctly=False,
        root_causes_identified=[],
        expected_root_causes=["cause1", "cause2"],
        steps_taken=30,
        damage_score=1.0,
        actions_log=[],
        resolution_sequence_optimal=False
    )
    print(f"  Hard - score: {hard_result.score}")
    for key, val in hard_result.components.items():
        print(f"    {key}: {val}, == 0.0? {val == 0.0}, == 1.0? {val == 1.0}")
    
    print("\n" + "=" * 80)

if __name__ == "__main__":
    check_all_graders()
