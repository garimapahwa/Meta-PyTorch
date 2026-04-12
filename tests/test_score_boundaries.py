from fastapi.testclient import TestClient

import app as app_module
from environment import make_env
from models import Action, ActionType, ServiceName


def assert_score_between_zero_and_one(value: float) -> None:
    assert 0.0 < value < 1.0
    assert value == round(value, 1)


def test_environment_grade_payload_uses_open_intervals():
    env = make_env(task_id="easy_0", seed=0)
    env.reset()

    assert 0.0 < env.damage_score < 1.0

    grade = env.get_grade()

    assert_score_between_zero_and_one(grade["score"])
    assert_score_between_zero_and_one(grade["correctness"])
    assert_score_between_zero_and_one(grade["efficiency"])
    assert_score_between_zero_and_one(grade["damage"])
    assert_score_between_zero_and_one(grade["details"]["damage_score"])


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

    assert 0.0 < env.damage_score < 1.0

    grade = env.get_grade()

    assert_score_between_zero_and_one(grade["score"])
    assert_score_between_zero_and_one(grade["correctness"])
    assert_score_between_zero_and_one(grade["efficiency"])
    assert_score_between_zero_and_one(grade["damage"])
    assert_score_between_zero_and_one(grade["details"]["damage_score"])


def test_api_payloads_sanitize_damage_score_boundaries():
    client = TestClient(app_module.app)

    initial_grade_response = client.get("/grade")
    initial_grade_response.raise_for_status()
    initial_grade = initial_grade_response.json()
    assert_score_between_zero_and_one(initial_grade["score"])
    assert_score_between_zero_and_one(initial_grade["correctness"])
    assert_score_between_zero_and_one(initial_grade["efficiency"])
    assert_score_between_zero_and_one(initial_grade["damage"])
    assert_score_between_zero_and_one(initial_grade["details"]["damage_score"])

    reset_response = client.post("/reset", json={"task_id": "hard_0", "seed": 0})
    reset_response.raise_for_status()
    reset_payload = reset_response.json()
    assert_score_between_zero_and_one(reset_payload["observation"]["damage_score"])

    for _ in range(30):
        step_response = client.post(
            "/step",
            json={"action_type": "restart_service", "service": "cache"},
        )
        step_response.raise_for_status()

    state_response = client.get("/state")
    state_response.raise_for_status()
    state_payload = state_response.json()

    assert_score_between_zero_and_one(state_payload["damage_score"])
    assert_score_between_zero_and_one(state_payload["observation"]["damage_score"])

    grade_response = client.get("/grade")
    grade_response.raise_for_status()
    grade_payload = grade_response.json()

    assert_score_between_zero_and_one(grade_payload["score"])
    assert_score_between_zero_and_one(grade_payload["correctness"])
    assert_score_between_zero_and_one(grade_payload["efficiency"])
    assert_score_between_zero_and_one(grade_payload["damage"])
    assert_score_between_zero_and_one(grade_payload["details"]["damage_score"])


def test_dashboard_score_widgets_never_render_dash_placeholders():
    client = TestClient(app_module.app)

    html = client.get("/").text

    assert 'id="damage-score">0.1<' in html
    assert 'id="grade-score">0.1<' in html
    assert 'id="grade-score-mini">0.1<' in html
    assert 'id="grade-correctness-value">0.1<' in html
    assert 'id="grade-efficiency-value">0.1<' in html
    assert 'id="grade-damage-value">0.1<' in html
