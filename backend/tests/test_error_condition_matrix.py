from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app.agents.error_evaluator import ErrorEvaluator
from backend.app.agents.error_protocol import (
    GENERAL_SYSTEM_PROMPT,
    SPECIFIC_INSTRUCTIONS,
    build_generation_messages,
)
from backend.app.agents.graph_base import GraphInput, build_graph_state
from backend.app.agents.structured import (
    CopyVersionsArtifact,
    ScheduleArtifact,
    schema_for_artifact,
)
from backend.app.models.domain import ERROR_TYPE_IDS
from backend.app.scenarios.registry import Scenario, ScenarioRegistry
from backend.app.services.providers import ProviderMessage, ProviderResponse
from backend.app.services.records import SYSTEM_FAILURE_TEXT


NON_SYSTEM_ERROR_TYPES = tuple(
    error_type_id
    for error_type_id in ERROR_TYPE_IDS
    if error_type_id != "system_failure"
)
ACTIVE_SCENARIOS = tuple(ScenarioRegistry.load_default().list_active())


def _scenario_id(scenario: Scenario) -> str:
    return scenario.scenario_id


@pytest.mark.parametrize("scenario", ACTIVE_SCENARIOS, ids=_scenario_id)
@pytest.mark.parametrize("error_type_id", NON_SYSTEM_ERROR_TYPES)
def test_every_active_non_system_error_has_an_ai_generation_contract(
    scenario: Scenario,
    error_type_id: str,
) -> None:
    base_messages = [
        ProviderMessage(role="system", content=scenario.provider_system_prompt),
        ProviderMessage(role="user", content="请处理这个请求。"),
    ]

    messages = build_generation_messages(
        base_messages=base_messages,
        behavior_id=error_type_id,
        feedback_reason="未发现指定错误。",
    )

    rule = scenario.mutation_policy.rules[error_type_id]
    assert GENERAL_SYSTEM_PROMPT.strip() in messages[0].content
    assert scenario.provider_system_prompt in messages[0].content
    assert SPECIFIC_INSTRUCTIONS[error_type_id].strip() in messages[0].content
    assert messages[-1] == base_messages[-1]
    assert "target_kind=" not in messages[0].content
    assert "【评估反馈】" in messages[0].content
    assert rule.centrality in {"peripheral", "core"}
    assert rule.presentation in scenario.error_policy.allowed_presentations


def test_active_generation_routes_decision_as_text_and_execution_as_structured() -> None:
    source = (
        Path(__file__).parents[1] / "app" / "services" / "sessions.py"
    ).read_text(encoding="utf-8")

    assert 'if str(session_row["subcondition"]) == "execution":' in source
    assert 'in {"decision", "execution"}' not in source


@pytest.mark.parametrize(
    ("condition", "expected_schema"),
    (("tool", ScheduleArtifact), ("human", CopyVersionsArtifact)),
)
def test_execution_generation_retains_typed_artifact_schema(
    condition: str,
    expected_schema: type,
) -> None:
    assert schema_for_artifact(
        condition=condition,
        subcondition="execution",
    ) is expected_schema


@pytest.mark.parametrize(
    "scenario",
    tuple(
        scenario
        for scenario in ACTIVE_SCENARIOS
        if scenario.subcondition == "decision" or scenario.topic_key == "weather"
    ),
    ids=_scenario_id,
)
@pytest.mark.parametrize("error_type_id", NON_SYSTEM_ERROR_TYPES)
def test_decision_and_weather_errors_preserve_authoritative_ui(
    scenario: Scenario,
    error_type_id: str,
) -> None:
    assert scenario.mutation_policy.rules[error_type_id].presentation == "assistant_text"
    if scenario.subcondition == "decision":
        assert scenario.artifact_type is None
        assert scenario.artifact.participant_visible is False


@pytest.mark.parametrize("scenario", ACTIVE_SCENARIOS, ids=_scenario_id)
def test_system_failure_remains_deterministic_for_every_active_scenario(
    scenario: Scenario,
) -> None:
    rule = scenario.mutation_policy.rules["system_failure"]

    assert SYSTEM_FAILURE_TEXT == "系统出现错误，请稍后再试。"
    assert rule.presentation == "system_failure"
    assert rule.centrality == "none"


@pytest.mark.parametrize("error_type_id", NON_SYSTEM_ERROR_TYPES)
def test_ai_evaluator_classifies_generated_candidate_without_exposing_raw_reason(
    error_type_id: str,
) -> None:
    scenario = ScenarioRegistry.load_default().require(
        condition="human",
        subcondition="chat",
        topic_key="funStory",
    )
    state = build_graph_state(
        session_row={
            "session_uuid": f"matrix-evaluator-{error_type_id}",
            "participant_id": 7,
            "condition": scenario.condition,
            "subcondition": scenario.subcondition,
            "topic_key": scenario.topic_key,
            "scenario_id": scenario.scenario_id,
            "error_type_id": error_type_id,
            "planned_error_turn": 1,
        },
        turn_index=1,
        graph_input=GraphInput(
            user_text="请继续。",
            input_mode="text_test_only",
        ),
        recent_history=[],
        scenario=scenario,
        graph_version=scenario.graph,
    )
    evaluator = ErrorEvaluator(
        runner=lambda _messages: ProviderResponse(
            text=json.dumps(
                {
                    "pass": True,
                    "reason": "RAW_PROVIDER_REASON_MUST_NOT_PERSIST",
                }
            ),
            provider="fake-evaluator",
            model="fake-model",
            route="evaluator",
            attempts=[],
        )
    )

    result = evaluator.evaluate(
        state=state,
        assistant_text="AI 生成的错误候选。",
        artifact_type=None,
        artifact_payload=None,
    )

    assert result["presented"] is True
    assert result["reason"] == "evaluator_presented"
    assert result["feedback_reason"] == "RAW_PROVIDER_REASON_MUST_NOT_PERSIST"
