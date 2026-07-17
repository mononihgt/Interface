from __future__ import annotations

import json

from langgraph.graph.state import CompiledStateGraph

from backend.app.agents.graph_base import (
    ExperimentGraphState,
    GraphInput,
    build_graph_state,
)
from backend.app.agents.chat import build_chat_graph
from backend.app.agents.execution import build_execution_graph
from backend.app.agents.planning import build_planning_graph
from backend.app.agents.qa import build_qa_graph
from backend.app.agents.structured import StructuredAgentResult
from backend.app.scenarios.registry import ScenarioRegistry
from backend.app.services.providers import ProviderAttempt, ProviderResponse
from backend.app.services.records import SYSTEM_FAILURE_TEXT


def test_graph_state_never_accepts_frontend_assignment_override():
    registry = ScenarioRegistry.load_default()
    session_row = {
        "session_uuid": "session-db-owned",
        "participant_id": 7,
        "condition": "tool",
        "subcondition": "qa",
        "topic_key": "weather",
        "scenario_id": "tool_qa_weather_v2",
        "error_type_id": "logic_minor",
        "planned_error_turn": 3,
        "is_test": 1,
    }

    state = build_graph_state(
        session_row=session_row,
        turn_index=2,
        graph_input=GraphInput(
            user_text="How windy is Hangzhou today?",
            input_mode="text_test_only",
            frontend_assignment_override={
                "condition": "human",
                "subcondition": "chat",
                "topic_key": "funStory",
                "scenario_id": "human_chat_funStory_v2",
            },
        ),
        recent_history=[],
        scenario=registry.require(condition="tool", subcondition="qa", topic_key="weather"),
        graph_version="qa_graph_v2",
    )

    assert isinstance(state, ExperimentGraphState)
    assert state.condition == "tool"
    assert state.subcondition == "qa"
    assert state.topic_key == "weather"
    assert state.scenario_id == "tool_qa_weather_v2"


def test_qa_graph_returns_text_or_info_card():
    registry = ScenarioRegistry.load_default()
    graph = build_qa_graph(
        provider_runner=lambda state: ProviderResponse(
            text="杭州今天有阵风，外出带件薄外套更稳妥。",
            provider="yi-zhan",
            model="gpt-5.1",
            route="chat",
            attempts=[
                ProviderAttempt(
                    route="chat",
                    provider="yi-zhan",
                    model="gpt-5.1",
                    status="success",
                    latency_ms=120,
                )
            ],
            used_local_fallback=False,
        )
    )

    state = build_graph_state(
        session_row={
            "session_uuid": "session-qa-1",
            "participant_id": 9,
            "condition": "tool",
            "subcondition": "qa",
            "topic_key": "weather",
            "scenario_id": "tool_qa_weather_v2",
            "error_type_id": "factual_minor",
            "planned_error_turn": 4,
            "is_test": 1,
        },
        turn_index=1,
        graph_input=GraphInput(
            user_text="杭州今天需要带外套吗？",
            input_mode="text_test_only",
        ),
        recent_history=[],
        scenario=registry.require(condition="tool", subcondition="qa", topic_key="weather"),
        graph_version="qa_graph_v2",
    )

    result = graph.run(state)

    assert isinstance(graph.compiled_graph, CompiledStateGraph)
    assert result.client_response["assistant_text"] == "杭州今天有阵风，外出带件薄外套更稳妥。"
    assert result.client_response["artifact_type"] in {None, "weather_card"}
    assert result.client_response["error_presentation"] in {"none", "assistant_text"}
    assert result.turn_record["assistant_text"] == result.client_response["assistant_text"]
    assert result.turn_record["agent_state_json"]


def test_planned_system_failure_graph_records_stable_provider_evidence():
    registry = ScenarioRegistry.load_default()
    graph = build_chat_graph(
        provider_runner=lambda _state: (_ for _ in ()).throw(
            AssertionError("provider should not be called")
        )
    )
    state = build_graph_state(
        session_row={
            "session_uuid": "session-planned-system-failure",
            "participant_id": 9,
            "condition": "human",
            "subcondition": "chat",
            "topic_key": "funStory",
            "scenario_id": "human_chat_funStory_v2",
            "error_type_id": "system_failure",
            "planned_error_turn": 1,
            "is_test": 0,
        },
        turn_index=1,
        graph_input=GraphInput(user_text="今天过得有点乱。", input_mode="voice"),
        recent_history=[],
        scenario=registry.require(condition="human", subcondition="chat", topic_key="funStory"),
        graph_version="chat_graph_v2",
    )

    result = graph.run(state)

    assert result.state.assistant_text == SYSTEM_FAILURE_TEXT
    assert result.state.llm_provider == "local-system"
    assert result.state.llm_model == "planned-system-failure-v1"
    assert result.state.llm_route == "system_failure"
    assert result.state.llm_attempts == []


def test_planned_execution_error_keeps_invalid_ai_candidate_unchanged():
    registry = ScenarioRegistry.load_default()
    scenario = registry.require(
        condition="tool",
        subcondition="execution",
        topic_key="taskExecution",
    )
    candidate = '{"assistant_text":"第五个 AI 候选","rows":"schema invalid"}'
    response = ProviderResponse(
        text=candidate,
        provider="test-provider",
        model="test-model",
        route="chat",
        attempts=[],
        used_local_fallback=False,
    )
    graph = build_execution_graph(
        provider_runner=lambda _state: StructuredAgentResult(
            value=None,
            response=response,
            validation_error="schema_validation_error",
            parse_attempts=2,
        )
    )
    state = build_graph_state(
        session_row={
            "session_uuid": "session-invalid-planned-execution",
            "participant_id": 9,
            "condition": scenario.condition,
            "subcondition": scenario.subcondition,
            "topic_key": scenario.topic_key,
            "scenario_id": scenario.scenario_id,
            "error_type_id": "logic_major",
            "planned_error_turn": 1,
            "is_test": 1,
        },
        turn_index=1,
        graph_input=GraphInput(
            user_text="请整理日程。",
            input_mode="text_test_only",
        ),
        recent_history=[],
        scenario=scenario,
        graph_version=scenario.graph,
    )

    result = graph.run(state)

    assert result.state.assistant_text == candidate
    assert result.state.artifact_validation_status == "invalid"
    assert result.state.error_presented is False
    assert result.state.error_presentation == "none"


def test_local_fallback_graph_records_stable_provider_evidence():
    registry = ScenarioRegistry.load_default()
    graph = build_chat_graph(
        provider_runner=lambda _state: ProviderResponse(
            text="抱歉，我遇到了一些技术问题。请稍后再试。",
            provider="local-router",
            model="fixed-text-fallback-v1",
            route="chat",
            attempts=[
                ProviderAttempt(
                    route="chat",
                    provider="local-router",
                    model="fixed-text-fallback-v1",
                    status="local_fallback",
                )
            ],
            used_local_fallback=True,
        )
    )
    state = build_graph_state(
        session_row={
            "session_uuid": "session-local-fallback",
            "participant_id": 10,
            "condition": "human",
            "subcondition": "chat",
            "topic_key": "funStory",
            "scenario_id": "human_chat_funStory_v2",
            "error_type_id": "social_minor",
            "planned_error_turn": 3,
            "is_test": 0,
        },
        turn_index=1,
        graph_input=GraphInput(user_text="今天过得有点乱。", input_mode="voice"),
        recent_history=[],
        scenario=registry.require(condition="human", subcondition="chat", topic_key="funStory"),
        graph_version="chat_graph_v2",
    )

    result = graph.run(state)

    assert result.state.llm_provider == "local-router"
    assert result.state.llm_model == "fixed-text-fallback-v1"
    assert result.state.llm_route == "chat"
    assert result.state.llm_attempts == [
        {
            "route": "chat",
            "provider": "local-router",
            "model": "fixed-text-fallback-v1",
            "status": "local_fallback",
            "latency_ms": None,
            "http_status": None,
            "cooldown_applied": False,
        }
    ]


def test_qa_graph_builds_weather_card_for_original_tool_topic_key():
    registry = ScenarioRegistry.load_default()
    participant_card = {
        "summary": "杭州·浙江明天：阵雨，25~31°C，降水概率70%，最大风速8m/s。",
        "location": {
            "name": "杭州",
            "admin1": "浙江",
            "country": "中国",
            "timezone": "Asia/Shanghai",
        },
        "current": {"temperature_c": 28.2, "weather_code": 3},
        "daily": [{"date": "2026-07-13", "weather_code": 80}],
    }
    weather_tool = {
        "status": "success",
        "source": {
            "provider": "openmeteo",
            "query": "杭州",
            "fetched_at": "2026-07-12T11:02:00Z",
            "location": {"latitude": 30.29365, "longitude": 120.16142},
        },
        "participant_card": participant_card,
    }
    graph = build_qa_graph(
        provider_runner=lambda state: ProviderResponse(
            text=participant_card["summary"],
            provider="openmeteo",
            model="weather-snapshot-v1",
            route="weather",
            attempts=[
                ProviderAttempt(
                    route="weather",
                    provider="openmeteo",
                    model="weather-snapshot-v1",
                    status="success",
                    latency_ms=120,
                )
            ],
            used_local_fallback=False,
        )
    )

    state = build_graph_state(
        session_row={
            "session_uuid": "session-qa-weather",
            "participant_id": 19,
            "condition": "tool",
            "subcondition": "qa",
            "topic_key": "weather",
            "scenario_id": "tool_qa_weather_v2",
            "error_type_id": "factual_minor",
            "planned_error_turn": 4,
            "is_test": 0,
        },
        turn_index=1,
        graph_input=GraphInput(
            user_text="杭州今天需要带外套吗？",
            input_mode="voice",
        ),
        recent_history=[],
        scenario=registry.require(condition="tool", subcondition="qa", topic_key="weather"),
        graph_version="qa_graph_v2",
        weather_tool=weather_tool,
    )

    result = graph.run(state)

    assert result.client_response["artifact_type"] == "weather_card"
    assert result.client_response["artifact_payload"] == participant_card
    assert json.loads(result.turn_record["agent_state_json"])["weather_tool"] == weather_tool


def test_planning_graph_uses_compiled_langgraph_and_builds_plan_card():
    registry = ScenarioRegistry.load_default()
    graph = build_planning_graph(
        provider_runner=lambda state: ProviderResponse(
            text="先确定目标，再拆成三个今天可以执行的小步骤。",
            provider="yi-zhan",
            model="gpt-5.1",
            route="planning",
            attempts=[
                ProviderAttempt(
                    route="planning",
                    provider="yi-zhan",
                    model="gpt-5.1",
                    status="success",
                    latency_ms=95,
                )
            ],
            used_local_fallback=False,
        )
    )
    state = build_graph_state(
        session_row={
            "session_uuid": "session-plan-1",
            "participant_id": 11,
            "condition": "human",
            "subcondition": "planning",
            "topic_key": "goalPlan",
            "scenario_id": "human_planning_goalPlan_v2",
            "error_type_id": "logic_minor",
            "planned_error_turn": 4,
            "is_test": 1,
        },
        turn_index=2,
        graph_input=GraphInput(
            user_text="帮我把这周的复习安排成三步。",
            input_mode="text_test_only",
        ),
        recent_history=[],
        scenario=registry.require(condition="human", subcondition="planning", topic_key="goalPlan"),
        graph_version="planning_graph_v2",
    )

    result = graph.run(state)

    assert isinstance(graph.compiled_graph, CompiledStateGraph)
    assert result.client_response["artifact_type"] == "plan_card"
    assert result.client_response["artifact_payload"] == {
        "title": "goalPlan",
        "summary": "先确定目标，再拆成三个今天可以执行的小步骤。",
    }
    assert result.turn_record["assistant_text"] == "先确定目标，再拆成三个今天可以执行的小步骤。"
    assert result.turn_record["llm_route"] == "planning"
    assert json.loads(result.turn_record["llm_attempts_json"]) == [
        {
            "route": "planning",
            "provider": "yi-zhan",
            "model": "gpt-5.1",
            "status": "success",
            "http_status": None,
            "cooldown_applied": False,
        }
    ]


def test_chat_graph_uses_compiled_langgraph_and_builds_turn_draft(monkeypatch):
    registry = ScenarioRegistry.load_default()
    graph = build_chat_graph(
        provider_runner=lambda state: ProviderResponse(
            text="今天先说说你最想解决的那件事。",
            provider="yi-zhan",
            model="gpt-5.1",
            route="chat",
            attempts=[
                ProviderAttempt(
                    route="chat",
                    provider="yi-zhan",
                    model="gpt-5.1",
                    status="success",
                    latency_ms=88,
                )
            ],
            used_local_fallback=False,
        )
    )
    state = build_graph_state(
        session_row={
            "session_uuid": "session-chat-1",
            "participant_id": 12,
            "condition": "human",
            "subcondition": "chat",
            "topic_key": "funStory",
            "scenario_id": "human_chat_funStory_v2",
            "error_type_id": "none",
            "planned_error_turn": 5,
            "is_test": 1,
        },
        turn_index=3,
        graph_input=GraphInput(
            user_text="我今天状态有点乱。",
            input_mode="text_test_only",
        ),
        recent_history=[],
        scenario=registry.require(condition="human", subcondition="chat", topic_key="funStory"),
        graph_version="chat_graph_v2",
    )

    called = False
    original_invoke = graph.compiled_graph.invoke if hasattr(graph, "compiled_graph") else None

    if original_invoke is not None:
        def tracking_invoke(payload):
            nonlocal called
            called = True
            return original_invoke(payload)

        monkeypatch.setattr(graph.compiled_graph, "invoke", tracking_invoke)

    result = graph.run(state)

    assert isinstance(graph.compiled_graph, CompiledStateGraph)
    assert called is True
    assert result.client_response["artifact_type"] is None
    assert result.turn_record["assistant_text"] == "今天先说说你最想解决的那件事。"
    assert result.turn_record["error_planned"] is False
    assert result.turn_record["agent_state_json"]
