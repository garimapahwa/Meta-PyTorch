from fastapi.testclient import TestClient

import app as app_module
from environment import make_env
from models import Action, ActionType, ServiceName


def assert_open_task_score(value: float) -> None:
    assert 0.1 < value < 0.9


def assert_open_unit_score(value: float) -> None:
    assert 0.0 < value < 1.0


def test_environment_grade_payload_uses_open_intervals():
    env = make_env(task_id="easy_0", seed=0)
    env.reset()

    grade = env.get_grade()

    assert_open_task_score(grade["score"])
    assert_open_task_score(grade["correctness"])
    assert_open_task_score(grade["efficiency"])
    assert_open_task_score(grade["damage"])
    assert_open_unit_score(grade["details"]["damage_score"])


def test_environment_grade_details_never_emit_exact_one_after_collapse():
    env = make_env(task_id="hard_0", seed=0)
    env.reset()

    while not env._check_done():
        env.step(
            Action(
                action_type=ActionType.RESTART_SERVICE,
                service=ServiceName.CACHE,
            )
        )

    assert env.damage_score == 1.0

    grade = env.get_grade()

    assert_open_task_score(grade["score"])
    assert_open_task_score(grade["correctness"])
    assert_open_task_score(grade["efficiency"])
    assert_open_task_score(grade["damage"])
    assert_open_unit_score(grade["details"]["damage_score"])


def test_api_payloads_sanitize_damage_score_boundaries():
    client = TestClient(app_module.app)

    reset_response = client.post("/reset", json={"task_id": "hard_0", "seed": 0})
    reset_response.raise_for_status()
    reset_payload = reset_response.json()
    assert_open_unit_score(reset_payload["observation"]["damage_score"])

    for _ in range(30):
        step_response = client.post(
            "/step",
            json={"action_type": "restart_service", "service": "cache"},
        )
        step_response.raise_for_status()

    state_response = client.get("/state")
    state_response.raise_for_status()
    state_payload = state_response.json()

    assert_open_unit_score(state_payload["damage_score"])
    assert_open_unit_score(state_payload["observation"]["damage_score"])

    grade_response = client.get("/grade")
    grade_response.raise_for_status()
    grade_payload = grade_response.json()

    assert_open_task_score(grade_payload["score"])
    assert_open_task_score(grade_payload["correctness"])
    assert_open_task_score(grade_payload["efficiency"])
    assert_open_task_score(grade_payload["damage"])
    assert_open_unit_score(grade_payload["details"]["damage_score"])
