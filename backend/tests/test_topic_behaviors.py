from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.app.agents.chat import build_chat_graph
from backend.app.agents.graph_base import GraphInput, build_graph_state
from backend.app.agents.qa import build_qa_graph
from backend.app.agents.structured import StructuredAgentResult
from backend.app.db import get_connection
from backend.app.models.domain import TOPIC_KEYS_BY_CELL
from backend.app.scenarios.registry import ScenarioRegistry
from backend.app.services.providers import ProviderMessage, ProviderResponse, ProviderRouter
from backend.app.settings import Settings


TEST_DATE = "2026-07-12"
ADMIN_PASSWORD = "admin-pass-123"
ADMIN_SALT = "topic-behavior-salt"
PROVIDER_TOPIC_CELLS = [
    (condition, subcondition, topic_key)
    for (condition, subcondition), topic_keys in TOPIC_KEYS_BY_CELL.items()
    for topic_key in topic_keys
    if topic_key != "weather"
]


def _password_hash(password: str) -> str:
    return hashlib.sha256(f"{ADMIN_SALT}{password}".encode("utf-8")).hexdigest()


class FakeAsrClient:
    def transcribe(self, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            status="success",
            provider="tencent",
            text="测试语音",
            latency_ms=10,
        )


@pytest.fixture
def sqlite_settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'topic-behaviors.db'}",
        app_secret_key="test-secret-key",
        admin_user="admin",
        admin_password_salt=ADMIN_SALT,
        admin_password_hash=_password_hash(ADMIN_PASSWORD),
    )


@pytest.fixture
def client(sqlite_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from backend.app import main as app_main
    from backend.app import services
    from backend.app.services import participant_days

    monkeypatch.setattr(services.participants, "current_shanghai_date", lambda: TEST_DATE)
    monkeypatch.setattr(participant_days, "current_shanghai_date", lambda: TEST_DATE)
    monkeypatch.setattr(app_main, "get_asr_client", lambda _settings: FakeAsrClient())
    return TestClient(app_main.create_app(settings=sqlite_settings))


def _admin_login(client: TestClient) -> None:
    response = client.post(
        "/api/admin/login",
        json={"username": "admin", "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200


def _start_topic_session(
    client: TestClient,
    *,
    condition: str,
    subcondition: str,
    topic_key: str,
) -> str:
    response = client.post(
        "/api/test/sessions/start",
        json={
            "is_test": True,
            "condition": condition,
            "subcondition": subcondition,
            "topic_key": topic_key,
            "error_type_id": "social_minor",
            "planned_error_turn": 5,
            "client_info": {
                "device_type": "desktop",
                "viewport_width": 1280,
                "is_secure_context": True,
                "browser_name": "Chrome",
                "browser_version": "126",
                "microphone_available": True,
                "microphone_permission": "granted",
            },
        },
    )
    assert response.status_code == 200
    return str(response.json()["session_id"])


def _structured_payload(schema: type, *, assistant_text: str) -> object:
    if schema.__name__ == "DecisionMatrixArtifact":
        payload = {
            "assistant_text": assistant_text,
            "options": [
                {"id": "a", "label": "方案A", "attributes": {"成本": 10}},
                {"id": "b", "label": "方案B", "attributes": {"成本": 20}},
            ],
            "constraints": [{"name": "成本", "kind": "max", "value": 20}],
            "weights": [{"criterion": "成本", "weight": 1.0}],
            "recommendation": {"option_id": "a", "summary": "方案A更合适。"},
            "reasons": ["满足约束。"],
        }
    elif schema.__name__ == "PreferenceCardsArtifact":
        payload = {
            "assistant_text": assistant_text,
            "mood": "想放松",
            "preferences": ["安静"],
            "options": [
                {"id": "a", "title": "散步", "signals": ["安静"]},
                {"id": "b", "title": "看电影", "signals": ["室内"]},
            ],
            "ai_preference": {"option_id": "a", "summary": "更符合当前心情。"},
            "friend_like_reason": "散步更容易放松。",
        }
    elif schema.__name__ == "ScheduleArtifact":
        payload = {
            "assistant_text": assistant_text,
            "status": "completed",
            "requestedSource": "明天9点开会",
            "rows": [
                {
                    "date": "明天",
                    "time": "09:00",
                    "location": "会议室",
                    "task": "开会",
                    "note": "带材料",
                }
            ],
        }
    elif schema.__name__ == "CopyVersionsArtifact":
        payload = {
            "assistant_text": assistant_text,
            "status": "completed",
            "requestedSource": "今天散步后轻松了很多。",
            "label": "朋友圈文案",
            "candidates": [
                {"id": "v1", "label": "自然版", "text": "散步后，整个人轻松了。"},
                {"id": "v2", "label": "简洁版", "text": "散步是今天的回血方式。"},
            ],
            "recommendedIndex": 0,
            "selected_version": {"version_id": "v1", "reason": "语气自然。"},
            "revision_notes": ["保留轻松感。"],
        }
    else:
        raise AssertionError(f"Unexpected structured schema: {schema.__name__}")
    return schema.model_validate(payload)


def _weather_snapshot(query: str):
    from backend.app.services.weather import (
        WeatherCurrent,
        WeatherDaily,
        WeatherLocation,
        WeatherSnapshot,
    )

    return WeatherSnapshot(
        query=query,
        fetched_at=datetime(2026, 7, 12, 11, 2, tzinfo=timezone.utc),
        location=WeatherLocation(
            name=query,
            admin1=None,
            admin2=None,
            country="中国",
            country_code="CN",
            latitude=30.29365,
            longitude=120.16142,
            timezone="Asia/Shanghai",
        ),
        current=WeatherCurrent(
            time="2026-07-12T19:00",
            temperature_c=28.2,
            relative_humidity_percent=84,
            apparent_temperature_c=32.2,
            wind_speed_mps=5.35,
            weather_code=3,
        ),
        daily=[
            WeatherDaily(
                date=(date(2026, 7, 12) + timedelta(days=offset)).isoformat(),
                weather_code=80,
                temperature_max_c=31,
                temperature_min_c=25,
                precipitation_probability_percent=70,
                wind_speed_max_mps=8,
            )
            for offset in range(7)
        ],
    )


@pytest.mark.parametrize(
    ("condition", "subcondition", "topic_key"), PROVIDER_TOPIC_CELLS
)
def test_each_provider_topic_runs_five_turns_with_prompt_and_growing_history(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    condition: str,
    subcondition: str,
    topic_key: str,
):
    scenario = ScenarioRegistry.load_default().require(
        condition=condition,
        subcondition=subcondition,
        topic_key=topic_key,
    )
    captured_messages: list[list[ProviderMessage]] = []
    captured_request_ids: list[str] = []
    captured_allow_local_fallback: list[bool] = []

    async def fake_chat(
        self: ProviderRouter,
        *,
        request_id: str,
        messages: list[ProviderMessage],
        is_test: bool,
        allow_local_fallback: bool = True,
    ) -> ProviderResponse:
        del self
        assert is_test is True
        error_planned = any(
            "[指令：激活错误 -> social_minor]" in message.content
            for message in messages
        )
        assert allow_local_fallback is (not error_planned)
        captured_messages.append(list(messages))
        captured_request_ids.append(request_id)
        captured_allow_local_fallback.append(allow_local_fallback)
        return ProviderResponse(
            text=f"assistant {len(captured_messages)}",
            provider="fake",
            model="fake-model",
            route="chat",
            attempts=[],
        )

    async def fake_structured(
        self: ProviderRouter,
        *,
        request_id: str,
        messages: list[ProviderMessage],
        is_test: bool,
        schema: type,
        max_parse_attempts: int = 2,
        allow_local_fallback: bool = True,
        payload_normalizer=None,
    ) -> StructuredAgentResult:
        del self, max_parse_attempts
        assert is_test is True
        assert callable(payload_normalizer)
        error_planned = any(
            "[指令：激活错误 -> social_minor]" in message.content
            for message in messages
        )
        assert allow_local_fallback is (not error_planned)
        captured_messages.append(list(messages))
        captured_request_ids.append(request_id)
        captured_allow_local_fallback.append(allow_local_fallback)
        response = ProviderResponse(
            text="structured",
            provider="fake",
            model="fake-model",
            route="chat",
            attempts=[],
        )
        return StructuredAgentResult(
            value=_structured_payload(
                schema,
                assistant_text=f"assistant {len(captured_messages)}",
            ),
            response=response,
            validation_error=None,
        )

    monkeypatch.setattr(ProviderRouter, "generate_chat", fake_chat)
    monkeypatch.setattr(ProviderRouter, "generate_structured_agent", fake_structured)

    with client:
        _admin_login(client)
        session_id = _start_topic_session(
            client,
            condition=condition,
            subcondition=subcondition,
            topic_key=topic_key,
        )
        for turn_index in range(1, 6):
            turn_response = client.post(
                "/api/turns",
                json={
                    "session_id": session_id,
                    "input_mode": "text_test_only",
                    "user_text": f"user {turn_index}",
                    "operation_id": f"{topic_key}-turn-{turn_index}",
                    "turn_index": turn_index,
                },
            )
            assert turn_response.status_code == 200
            rating_response = client.post(
                f"/api/turns/{turn_response.json()['turn_id']}/rating",
                json={"stance_score": 3, "trust_score": 5},
            )
            assert rating_response.status_code == 200

    assert len(captured_messages) == 5
    assert captured_request_ids == [
        f"{session_id}-turn-{turn_index}-semantic-1"
        for turn_index in range(1, 6)
    ]
    assert captured_allow_local_fallback == [True, True, True, True, False]
    for turn_index, messages in enumerate(captured_messages, start=1):
        system_message = messages[0]
        assert system_message.role == "system"
        assert scenario.system_prompt in system_message.content
        assert scenario.clarification.response_goal in system_message.content
        for capability_limit in scenario.response_policy.capability_limits:
            assert capability_limit in system_message.content
        if turn_index == 5:
            assert "[指令：激活错误 -> social_minor]" in system_message.content
            assert "降低共情水平" in system_message.content
        else:
            assert "[指令：正常操作]" in system_message.content
        conversation_messages = messages[1:]
        assert [message.role for message in conversation_messages] == [
            role
            for _ in range(1, turn_index)
            for role in ("user", "assistant")
        ] + ["user"]
        assert [message.content for message in conversation_messages] == [
            content
            for previous_turn in range(1, turn_index)
            for content in (f"user {previous_turn}", f"assistant {previous_turn}")
        ] + [f"user {turn_index}"]


def test_weather_topic_runs_five_turns_with_reuse_artifacts_and_restore(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.services.weather import WeatherService

    user_inputs = [
        "杭州天气怎么样？",
        "明天会下雨吗？",
        "后天适合出门吗？",
        "要带伞吗？",
        "巴黎湿度怎么样？",
    ]
    expected_queries = ["杭州", "杭州", "杭州", "杭州", "巴黎"]
    observed_queries: list[str] = []
    ai_request_ids: list[str] = []
    planned_error_text = "巴黎湿度信息已列出，请自行查看。"

    async def fake_lookup(self: WeatherService, query: str):
        observed_queries.append(query)
        return _weather_snapshot(query)

    async def fake_ai(
        self: ProviderRouter,
        *,
        request_id: str,
        messages: list[ProviderMessage],
        is_test: bool,
        allow_local_fallback: bool = True,
    ) -> ProviderResponse:
        del self
        assert is_test is True
        assert allow_local_fallback is False
        assert any(
            "[指令：激活错误 -> social_minor]" in message.content
            and "权威上下文" in message.content
            and "巴黎" in message.content
            and "28.2" in message.content
            for message in messages
        )
        ai_request_ids.append(request_id)
        return ProviderResponse(
            text=planned_error_text,
            provider="fake",
            model="fake-model",
            route="chat",
            attempts=[],
        )

    monkeypatch.setattr(WeatherService, "lookup", fake_lookup)
    monkeypatch.setattr(ProviderRouter, "generate_chat", fake_ai)

    with client:
        _admin_login(client)
        session_id = _start_topic_session(
            client,
            condition="tool",
            subcondition="qa",
            topic_key="weather",
        )
        for turn_index, (user_text, expected_query) in enumerate(
            zip(user_inputs, expected_queries, strict=True),
            start=1,
        ):
            turn_response = client.post(
                "/api/turns",
                json={
                    "session_id": session_id,
                    "input_mode": "text_test_only",
                    "user_text": user_text,
                    "operation_id": f"weather-turn-{turn_index}",
                    "turn_index": turn_index,
                },
            )
            assert turn_response.status_code == 200
            turn_payload = turn_response.json()
            assert turn_payload["artifact_type"] == "weather_card"
            assert turn_payload["graph_trace"]["weather_tool"]["status"] == "success"
            assert (
                turn_payload["graph_trace"]["weather_tool"]["source"]["query"]
                == expected_query
            )
            assert len(ai_request_ids) == (1 if turn_index == 5 else 0)
            if turn_index == 5:
                assert turn_payload["assistant_text"] == planned_error_text
            else:
                assert turn_payload["assistant_text"] != planned_error_text

            rating_response = client.post(
                f"/api/turns/{turn_payload['turn_id']}/rating",
                json={"stance_score": 3, "trust_score": 5},
            )
            assert rating_response.status_code == 200

            restored = client.get(f"/api/sessions/{session_id}")
            assert restored.status_code == 200
            restored_turns = restored.json()["turns"]
            assert len(restored_turns) == turn_index
            assert restored_turns[-1]["artifact_type"] == "weather_card"
            assert (
                restored_turns[-1]["graph_trace"]["weather_tool"]["source"]["query"]
                == expected_query
            )

    assert observed_queries == expected_queries
    assert observed_queries.count("巴黎") == 1
    assert ai_request_ids == [f"{session_id}-turn-5-semantic-1"]


def test_legacy_persisted_execution_session_restores_and_submits_next_turn(
    client: TestClient,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    captured_messages: list[list[ProviderMessage]] = []

    async def fake_structured(
        self: ProviderRouter,
        *,
        request_id: str,
        messages: list[ProviderMessage],
        is_test: bool,
        schema: type,
        max_parse_attempts: int = 2,
        allow_local_fallback: bool = True,
        payload_normalizer=None,
    ) -> StructuredAgentResult:
        del self, request_id, is_test, max_parse_attempts
        assert allow_local_fallback is True
        assert callable(payload_normalizer)
        captured_messages.append(list(messages))
        response = ProviderResponse(
            text="structured",
            provider="fake",
            model="fake-model",
            route="chat",
            attempts=[],
        )
        return StructuredAgentResult(
            value=_structured_payload(schema, assistant_text="已整理。"),
            response=response,
            validation_error=None,
        )

    monkeypatch.setattr(ProviderRouter, "generate_structured_agent", fake_structured)

    with client:
        _admin_login(client)
        session_id = _start_topic_session(
            client,
            condition="tool",
            subcondition="execution",
            topic_key="taskExecution",
        )
        conn = get_connection(sqlite_settings)
        try:
            conn.execute(
                """
                UPDATE experiment_sessions
                SET topic_key = ?, scenario_id = ?, agent_graph_version = ?
                WHERE session_uuid = ?
                """,
                (
                    "task_table",
                    "tool_execution_task_table_v1",
                    "execution_graph_v1",
                    session_id,
                ),
            )
        finally:
            conn.close()

        restored = client.get(f"/api/sessions/{session_id}")
        turn_response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "明天9点在会议室开会。",
                "operation_id": "legacy-task-table-turn-1",
                "turn_index": 1,
            },
        )

    assert restored.status_code == 200
    assert restored.json()["topic_key"] == "task_table"
    assert turn_response.status_code == 200
    assert turn_response.json()["artifact_type"] == "table"
    expected_scenario = ScenarioRegistry.load_default().require(
        condition="tool",
        subcondition="execution",
        topic_key="taskExecution",
    )
    assert expected_scenario.provider_system_prompt in captured_messages[0][0].content
    assert "[指令：正常操作]" in captured_messages[0][0].content

    conn = get_connection(sqlite_settings)
    try:
        row = conn.execute(
            "SELECT topic_key, scenario_id, agent_graph_version FROM experiment_sessions WHERE session_uuid = ?",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    assert dict(row) == {
        "topic_key": "task_table",
        "scenario_id": "tool_execution_task_table_v1",
        "agent_graph_version": "execution_graph_v1",
    }


def test_new_test_session_rejects_legacy_topic_alias(client: TestClient):
    with client:
        _admin_login(client)
        response = client.post(
            "/api/test/sessions/start",
            json={
                "is_test": True,
                "condition": "tool",
                "subcondition": "execution",
                "topic_key": "task_table",
                "error_type_id": "social_minor",
                "planned_error_turn": 5,
                "client_info": {
                    "device_type": "desktop",
                    "viewport_width": 1280,
                    "is_secure_context": True,
                    "browser_name": "Chrome",
                    "browser_version": "126",
                    "microphone_available": True,
                    "microphone_permission": "granted",
                },
            },
        )

    assert response.status_code == 400
    assert "task_table" in response.json()["detail"]


@pytest.mark.parametrize("topic_key", ["funStory", "news", "tech"])
def test_chat_topics_never_generate_task_artifacts(topic_key: str):
    condition = "human" if topic_key == "funStory" else "tool"
    scenario = ScenarioRegistry.load_default().require(
        condition=condition,
        subcondition="chat",
        topic_key=topic_key,
    )
    state = build_graph_state(
        session_row={
            "session_uuid": f"session-{topic_key}",
            "participant_id": 1,
            "condition": condition,
            "subcondition": "chat",
            "topic_key": topic_key,
            "scenario_id": scenario.scenario_id,
            "error_type_id": "social_minor",
            "planned_error_turn": 3,
        },
        turn_index=1,
        graph_input=GraphInput(user_text="我们聊聊。", input_mode="text_test_only"),
        recent_history=[],
        scenario=scenario,
        graph_version=scenario.graph,
    )
    graph = build_chat_graph(
        provider_runner=lambda _state: ProviderResponse(
            text="可以，我们从你最关心的话题开始。",
            provider="fake",
            model="fake-model",
            route="chat",
            attempts=[],
        )
    )

    result = graph.run(state)

    assert result.client_response["artifact_type"] is None
    assert result.client_response["artifact_payload"] is None
    assert scenario.artifact.artifact_type is None


def test_physics_qa_does_not_generate_weather_artifact():
    scenario = ScenarioRegistry.load_default().require(
        condition="tool",
        subcondition="qa",
        topic_key="physics",
    )
    state = build_graph_state(
        session_row={
            "session_uuid": "session-physics",
            "participant_id": 1,
            "condition": "tool",
            "subcondition": "qa",
            "topic_key": "physics",
            "scenario_id": scenario.scenario_id,
            "error_type_id": "factual_minor",
            "planned_error_turn": 3,
        },
        turn_index=1,
        graph_input=GraphInput(
            user_text="万有引力定律是什么？",
            input_mode="text_test_only",
        ),
        recent_history=[],
        scenario=scenario,
        graph_version=scenario.graph,
    )
    graph = build_qa_graph(
        provider_runner=lambda _state: ProviderResponse(
            text="万有引力描述有质量物体之间相互吸引的作用。",
            provider="fake",
            model="fake-model",
            route="chat",
            attempts=[],
        )
    )

    result = graph.run(state)

    assert result.client_response["artifact_type"] is None
    assert result.client_response["artifact_payload"] is None
