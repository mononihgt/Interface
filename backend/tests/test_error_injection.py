from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app.agents.error_evaluator import ErrorEvaluator
from backend.app.agents.error_protocol import (
    build_evaluator_messages,
    parse_error_evaluation,
)
from backend.app.agents.error_injection import (
    generation_fallback_prevents_error_presentation,
)
from backend.app.agents.graph_base import (
    ConversationMessage,
    GraphInput,
    build_graph_state,
)
from backend.app.agents.qa import build_qa_graph
from backend.app.agents.structured import ErrorMutation
from backend.app.scenarios.registry import ScenarioRegistry
from backend.app.services.providers import ProviderAttempt, ProviderResponse


def test_production_has_no_competing_local_error_generation_pipeline():
    production_sources = {
        path.name: path.read_text(encoding="utf-8")
        for path in (
            Path(__file__).parents[1] / "app" / "agents" / "error_injection.py",
            Path(__file__).parents[1] / "app" / "agents" / "graph_base.py",
            Path(__file__).parents[1] / "app" / "services" / "sessions.py",
        )
    }
    combined_source = "\n".join(production_sources.values())

    assert "MutationEngine" not in combined_source
    assert "apply_error_injection" not in combined_source
    assert "_text_mutation_messages" not in combined_source
    assert "_normalize_structured_text_mutation" not in combined_source
    assert "这个问题其实不难，你照着做就行。" not in combined_source
    assert "这种事你应该自己想清楚" not in combined_source


def test_prompt_native_error_evidence_does_not_rewrite_ai_candidate():
    scenario = ScenarioRegistry.load_default().require(
        condition="tool",
        subcondition="qa",
        topic_key="physics",
    )
    candidate = "AI 直接生成的轻微事实错误候选。"
    rule = scenario.mutation_policy.rules["factual_minor"]
    state = build_graph_state(
        session_row={
            "session_uuid": "session-prompt-native-error",
            "participant_id": 5,
            "condition": scenario.condition,
            "subcondition": scenario.subcondition,
            "topic_key": scenario.topic_key,
            "scenario_id": scenario.scenario_id,
            "error_type_id": "factual_minor",
            "planned_error_turn": 1,
        },
        turn_index=1,
        graph_input=GraphInput(user_text="为什么？", input_mode="text_test_only"),
        recent_history=[],
        scenario=scenario,
        graph_version=scenario.graph,
    )
    state.error_agent_result = ErrorMutation(
        error_type_id="factual_minor",
        severity="minor",
        presentation=rule.presentation,
        target_kind=rule.target_kind,
        target_path="assistant_text",
        original_value="generation_requested",
        mutated_value="candidate_generated",
        applied=True,
        centrality=rule.centrality,
        operation="prompt_native_generation",
        magnitude=rule.centrality,
        agent_generated=True,
    )
    graph = build_qa_graph(
        provider_runner=lambda _state: ProviderResponse(
            text=candidate,
            provider="test-provider",
            model="test-model",
            route="chat",
            attempts=[],
            used_local_fallback=False,
        ),
        evaluator_runner=lambda *_args: {
            "status": "success",
            "presented": True,
            "reason": "evaluator_presented",
        },
    )

    result = graph.run(state)

    assert result.state.assistant_text == candidate
    assert result.state.error_presented is True
    assert result.state.error_mutation is not None
    assert result.state.error_mutation.operation == "prompt_native_generation"
    assert result.state.error_mutation.agent_generated is True


def test_local_generation_fallback_never_counts_as_error_presentation():
    response = ProviderResponse(
        text="本地回退。",
        provider="local-router",
        model="fixed-text-fallback-v1",
        route="local_fallback",
        attempts=[
            ProviderAttempt(
                route="local_fallback",
                provider="local-router",
                model="fixed-text-fallback-v1",
                status="local_fallback",
            )
        ],
        used_local_fallback=True,
    )

    assert generation_fallback_prevents_error_presentation(provider_response=response)


def _evaluator_result(*, presented: bool, reason: str) -> dict[str, object]:
    scenario = ScenarioRegistry.load_default().require(
        condition="tool",
        subcondition="qa",
        topic_key="physics",
    )
    state = build_graph_state(
        session_row={
            "session_uuid": "session-evaluator-reason",
            "participant_id": 5,
            "condition": scenario.condition,
            "subcondition": scenario.subcondition,
            "topic_key": scenario.topic_key,
            "scenario_id": scenario.scenario_id,
            "error_type_id": "logic_minor",
            "planned_error_turn": 1,
        },
        turn_index=1,
        graph_input=GraphInput(user_text="请解释。", input_mode="text_test_only"),
        recent_history=[],
        scenario=scenario,
        graph_version=scenario.graph,
    )
    evaluator = ErrorEvaluator(
        runner=lambda _messages: ProviderResponse(
            text=json.dumps({"pass": presented, "reason": reason}),
            provider="fake-evaluator",
            model="fake-model",
            route="evaluator",
            attempts=[],
        )
    )
    return evaluator.evaluate(
        state=state,
        assistant_text="候选",
        artifact_type=None,
        artifact_payload=None,
    )


def test_external_evaluator_failure_reason_is_normalized_to_fixed_code():
    result = _evaluator_result(
        presented=False,
        reason="MALICIOUS_EVALUATOR_REASON_SENTINEL",
    )

    assert result["reason"] == "evaluator_not_presented"
    assert result["feedback_reason"] == "MALICIOUS_EVALUATOR_REASON_SENTINEL"


def test_external_evaluator_success_reason_is_normalized_to_fixed_code():
    result = _evaluator_result(
        presented=True,
        reason="MALICIOUS_SUCCESS_REASON_SENTINEL",
    )

    assert result["reason"] == "evaluator_presented"
    assert result["feedback_reason"] == "MALICIOUS_SUCCESS_REASON_SENTINEL"


def test_evaluator_prompt_contains_all_current_session_history_and_candidate() -> None:
    messages = build_evaluator_messages(
        error_type_id="factual_minor",
        session_history=[
            ConversationMessage(role="user", text="第一轮用户事实"),
            ConversationMessage(role="assistant", text="第一轮助手回复"),
            ConversationMessage(role="user", text="第二轮用户事实"),
            ConversationMessage(role="assistant", text="第二轮助手回复"),
        ],
        current_user_text="当前用户输入",
        assistant_text="当前候选回复",
        weather_context="权威天气上下文",
    )

    assert [message.role for message in messages] == ["system", "user"]
    assert "只输出JSON" in messages[0].content
    prompt = messages[1].content
    assert "事实错误-轻微" in prompt
    assert "第一轮用户事实" in prompt
    assert "第一轮助手回复" in prompt
    assert "第二轮用户事实" in prompt
    assert "第二轮助手回复" in prompt
    assert prompt.index("第一轮用户事实") < prompt.index("第二轮用户事实")
    assert "当前用户输入" in prompt
    assert "当前候选回复" in prompt
    assert "权威天气上下文" in prompt


def test_evaluator_prompt_targets_participant_visible_execution_artifact() -> None:
    messages = build_evaluator_messages(
        error_type_id="factual_major",
        session_history=[],
        current_user_text="明天下午3点在会议室开项目会",
        assistant_text="我已经整理到右侧。",
        error_presentation="simulated_ui",
        artifact_type="table",
        artifact_payload={
            "columns": ["日期", "时间", "地点", "任务", "备注"],
            "rows": [
                {
                    "date": "明天",
                    "time": "15:00",
                    "location": "错误地点哨兵",
                    "task": "项目会",
                    "note": "",
                }
            ],
        },
    )

    prompt = messages[1].content
    assert "simulated_ui" in prompt
    assert "右侧执行结果" in prompt
    assert "错误地点哨兵" in prompt
    assert "主要根据右侧执行结果判断" in prompt


def test_error_evaluator_forwards_execution_artifact_to_prompt() -> None:
    scenario = ScenarioRegistry.load_default().require(
        condition="tool",
        subcondition="execution",
        topic_key="taskExecution",
    )
    state = build_graph_state(
        session_row={
            "session_uuid": "session-artifact-evaluator",
            "participant_id": 9,
            "condition": scenario.condition,
            "subcondition": scenario.subcondition,
            "topic_key": scenario.topic_key,
            "scenario_id": scenario.scenario_id,
            "error_type_id": "factual_major",
            "planned_error_turn": 1,
        },
        turn_index=1,
        graph_input=GraphInput(user_text="请整理。", input_mode="text_test_only"),
        recent_history=[],
        scenario=scenario,
        graph_version=scenario.graph,
    )
    state.error_presentation = "simulated_ui"
    captured: list[list] = []

    def run(messages):
        captured.append(list(messages))
        return ProviderResponse(
            text='{"pass": true, "reason": "右侧存在目标错误"}',
            provider="deepseek",
            model="deepseek-v4-pro",
            route="evaluator",
            attempts=[],
        )

    ErrorEvaluator(runner=run).evaluate(
        state=state,
        assistant_text="已整理。",
        artifact_type="table",
        artifact_payload={"rows": [{"task": "ARTIFACT_FORWARD_SENTINEL"}]},
        current_user_text="请整理。",
    )

    assert "ARTIFACT_FORWARD_SENTINEL" in captured[0][1].content


@pytest.mark.parametrize(
    ("raw_text", "expected_pass", "expected_reason"),
    [
        ('{"pass": true, "reason": "包含轻微事实错误"}', True, "包含轻微事实错误"),
        (
            '```json\n{"pass": false, "reason": "没有指定错误"}\n```',
            False,
            "没有指定错误",
        ),
        (
            '判断如下：{"pass": true, "reason": "存在事实错误"}。',
            True,
            "存在事实错误",
        ),
        ("该回复存在轻微事实错误。", True, "该回复存在轻微事实错误。"),
        ("该回复没有事实错误。", False, "该回复没有事实错误。"),
    ],
)
def test_legacy_evaluator_parser_accepts_json_and_keyword_compatibility(
    raw_text: str,
    expected_pass: bool,
    expected_reason: str,
) -> None:
    parsed = parse_error_evaluation(
        raw_text,
        expected_error_type="factual_minor",
    )

    assert parsed is not None
    assert parsed.passed is expected_pass
    assert parsed.feedback_reason == expected_reason


def test_evaluator_parser_negative_reason_overrides_incorrect_true_flag() -> None:
    parsed = parse_error_evaluation(
        '{"pass": true, "reason": "回复没有事实错误"}',
        expected_error_type="factual_minor",
    )

    assert parsed is not None
    assert parsed.passed is False


def test_evaluator_parser_rejects_unclassifiable_output() -> None:
    assert (
        parse_error_evaluation(
            "无法判断。",
            expected_error_type="factual_minor",
        )
        is None
    )


def test_error_evaluator_uses_at_most_two_format_attempts() -> None:
    scenario = ScenarioRegistry.load_default().require(
        condition="tool",
        subcondition="qa",
        topic_key="physics",
    )
    state = build_graph_state(
        session_row={
            "session_uuid": "two-format-attempts",
            "participant_id": 5,
            "condition": scenario.condition,
            "subcondition": scenario.subcondition,
            "topic_key": scenario.topic_key,
            "scenario_id": scenario.scenario_id,
            "error_type_id": "factual_minor",
            "planned_error_turn": 1,
        },
        turn_index=1,
        graph_input=GraphInput(user_text="请解释。", input_mode="text_test_only"),
        recent_history=[],
        scenario=scenario,
        graph_version=scenario.graph,
    )
    responses = iter(
        [
            "无法解析",
            '{"pass": true, "reason": "包含轻微事实错误"}',
        ]
    )
    calls = {"count": 0}

    def run(_messages):
        calls["count"] += 1
        return ProviderResponse(
            text=next(responses),
            provider="deepseek",
            model="deepseek-v4-pro",
            route="evaluator",
            attempts=[],
        )

    result = ErrorEvaluator(runner=run).evaluate(
        state=state,
        assistant_text="候选",
        artifact_type=None,
        artifact_payload=None,
        current_user_text="请解释。",
    )

    assert calls["count"] == 2
    assert result["presented"] is True
    assert result["parse_attempts"] == 2
