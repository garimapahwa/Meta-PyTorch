#!/usr/bin/env python3
"""
Test JSON serialization and API response formatting to detect value corruption.
"""

import json
from graders import BaseGrader

def test_json_serialization():
    """Check if JSON serialization preserves the clamped values."""
    
    print("=" * 80)
    print("JSON SERIALIZATION TEST")
    print("=" * 80)
    
    # Test values
    test_values = {
        "min_score": BaseGrader.MIN_SCORE,
        "max_score": BaseGrader.MAX_SCORE,
        "mid_score": 0.5,
        "raw_min": 0.1,
        "raw_max": 0.9,
        "raw_zero": 0.0,
        "raw_one": 1.0,
    }
    
    # API response simulation
    api_response = {
        "score": BaseGrader.compute_final_score(0.5, 0.5, 0.5),
        "correctness": BaseGrader.safe_score(0.5),
        "efficiency": BaseGrader.safe_score(0.5),
        "damage": BaseGrader.safe_score(0.5),
        "details": {"test": "data"}
    }
    
    print("\nDirect Python values:")
    for key, val in api_response.items():
        if key != "details":
            is_zero = val == 0.0
            is_one = val == 1.0
            print(f"  {key}: {val}, is 0.0? {is_zero}, is 1.0? {is_one}")
    
    # Serialize to JSON
    json_str = json.dumps(api_response)
    print(f"\nJSON string:\n{json_str}")
    
    # Deserialize from JSON
    parsed = json.loads(json_str)
    print("\nAfter JSON round-trip:")
    for key, val in parsed.items():
        if key != "details":
            is_zero = val == 0.0
            is_one = val == 1.0
            print(f"  {key}: {val}, is 0.0? {is_zero}, is 1.0? {is_one}")
    
    # Test rounding behavior
    print("\n" + "=" * 80)
    print("ROUNDING TEST")
    print("=" * 80)
    
    round_values = [
        BaseGrader.MIN_SCORE,
        BaseGrader.MAX_SCORE,
        0.1,
        0.9,
        0.0,
        1.0,
    ]
    
    for val in round_values:
        rounded_2 = round(val, 2)
        rounded_3 = round(val, 3)
        rounded_4 = round(val, 4)
        print(f"\n  {val}:")
        print(f"    round(x, 2) = {rounded_2}, is 0.0? {rounded_2 == 0.0}, is 1.0? {rounded_2 == 1.0}")
        print(f"    round(x, 3) = {rounded_3}, is 0.0? {rounded_3 == 0.0}, is 1.0? {rounded_3 == 1.0}")
        print(f"    round(x, 4) = {rounded_4}, is 0.0? {rounded_4 == 0.0}, is 1.0? {rounded_4 == 1.0}")
    
    # Test exact boundary check
    print("\n" + "=" * 80)
    print("BOUNDARY CHECK")
    print("=" * 80)
    
    boundaries = [0.0, 0.1, BaseGrader.MIN_SCORE, 0.9, BaseGrader.MAX_SCORE, 1.0]
    for val in boundaries:
        in_range_exclusive = 0.1 < val < 0.9
        in_range_inclusive = 0.1 <= val <= 0.9
        print(f"  {val}:")
        print(f"    0.1 < {val} < 0.9? {in_range_exclusive}")
        print(f"    0.1 <= {val} <= 0.9? {in_range_inclusive}")

if __name__ == "__main__":
    test_json_serialization()
