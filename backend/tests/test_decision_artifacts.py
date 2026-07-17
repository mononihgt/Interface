from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.app.agents.decision import build_decision_graph
from backend.app.agents.graph_base import GraphInput, build_graph_state
from backend.app.scenarios.registry import ScenarioRegistry
from backend.app.services.providers import ProviderResponse
from backend.app.services.records import SYSTEM_FAILURE_TEXT
from backend.app.settings import Settings


TEST_DATE = "2026-07-02"
ADMIN_PASSWORD = "admin-pass-123"
ADMIN_SALT = "decision-text-salt"


def _password_hash(password: str) -> str:
    return hashlib.sha256(f"{ADMIN_SALT}{password}".encode("utf-8")).hexdigest()


class FakeAsrClient:
    def transcribe(self, **_: object) -> SimpleNamespace:
        return SimpleNamespace(
            status="success",
            provider="tencent",
            text="recognized transcript",
            latency_ms=40,
        )


@pytest.fixture
def sqlite_settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'decision-text.db'}",
        app_secret_key="test-secret-key",
        admin_user="admin",
        admin_password_salt=ADMIN_SALT,
        admin_password_hash=_password_hash(ADMIN_PASSWORD),
        yizhan_api_key="test-yizhan-key",
    )


@pytest.fixture
def client(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    from backend.app import main as app_main
    from backend.app import services
    from backend.app.services import participant_days

    monkeypatch.setattr(services.participants, "current_shanghai_date", lambda: TEST_DATE)
    monkeypatch.setattr(participant_days, "current_shanghai_date", lambda: TEST_DATE)
    monkeypatch.setattr(
        app_main,
        "get_asr_client",
        lambda _settings=sqlite_settings: FakeAsrClient(),
    )
    return TestClient(app_main.create_app(settings=sqlite_settings))


def _provider_response(text: str) -> ProviderResponse:
    return ProviderResponse(
        text=text,
        provider="fake-provider",
        model="fake-model",
        route="chat",
        attempts=[],
    )


def _graph_state(*, condition: str, topic_key: str):
    scenario = ScenarioRegistry.load_default().require(
        condition=condition,
        subcondition="decision",
        topic_key=topic_key,
    )
    return build_graph_state(
        session_row={
            "session_uuid": f"decision-{condition}",
            "participant_id": 20,
            "condition": condition,
            "subcondition": "decision",
            "topic_key": topic_key,
            "scenario_id": scenario.scenario_id,
            "error_type_id": "logic_minor",
            "planned_error_turn": 4,
            "is_test": 1,
        },
        turn_index=1,
        graph_input=GraphInput(
            user_text="请帮我比较两个选择。",
            input_mode="text_test_only",
        ),
        recent_history=[],
        scenario=scenario,
        graph_version=scenario.graph,
    )


@pytest.mark.parametrize(
    ("condition", "topic_key"),
    [
        ("tool", "valueDecision"),
        ("human", "preferenceDecision"),
    ],
)
def test_decision_graph_returns_provider_text_without_artifact(
    condition: str,
    topic_key: str,
) -> None:
    text = "我建议先比较约束、风险和你最在意的体验，再做选择。"
    graph = build_decision_graph(
        provider_runner=lambda _state: _provider_response(text)
    )

    result = graph.run(_graph_state(condition=condition, topic_key=topic_key))

    assert result.client_response == {
        "assistant_text": text,
        "artifact_type": None,
        "artifact_payload": None,
        "error_presentation": "none",
    }
    assert result.state.artifact_validation_status == "not_requested"


def _start_decision_test_session(
    client: TestClient,
    settings: Settings,
    *,
    error_type_id: str,
) -> str:
    from backend.app.db import get_connection

    login = client.post(
        "/api/admin/login",
        json={"username": "admin", "password": ADMIN_PASSWORD},
    )
    assert login.status_code == 200
    started = client.post(
        "/api/test/sessions/start",
        json={
            "is_test": True,
            "condition": "tool",
            "subcondition": "decision",
            "topic_key": "valueDecision",
            "error_type_id": error_type_id,
            "planned_error_turn": 1,
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
    assert started.status_code == 200
    session_id = str(started.json()["session_id"])
    conn = get_connection(settings)
    try:
        row = conn.execute(
            "SELECT subcondition, topic_key FROM experiment_sessions WHERE session_uuid = ?",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    assert tuple(row) == ("decision", "valueDecision")
    return session_id


def test_decision_api_uses_ai_text_error_without_artifact(
    client: TestClient,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.services.sessions import ProviderRouter

    generated_text = "方案A预算更低，但我故意给出一个前后矛盾的主要建议。"

    async def generate_chat(
        self,
        *,
        request_id,
        messages,
        is_test,
        allow_local_fallback=False,
    ) -> ProviderResponse:
        del self, request_id
        assert is_test is True
        assert allow_local_fallback is False
        assert any("[指令：激活错误 -> logic_major]" in item.content for item in messages)
        return _provider_response(generated_text)

    monkeypatch.setattr(ProviderRouter, "generate_chat", generate_chat)

    with client:
        session_id = _start_decision_test_session(
            client,
            sqlite_settings,
            error_type_id="logic_major",
        )
        response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "请比较两个租房方案。",
            },
        )
        restored = client.get(f"/api/sessions/{session_id}")

    assert response.status_code == 200
    assert response.json()["assistant_text"] == generated_text
    assert response.json()["artifact_type"] is None
    assert response.json()["artifact_payload"] is None
    assert restored.status_code == 200
    assert restored.json()["artifact_type"] is None
    assert restored.json()["artifact_payload"] is None


def test_decision_system_failure_is_fixed_text_without_artifact(
    client: TestClient,
    sqlite_settings: Settings,
) -> None:
    with client:
        session_id = _start_decision_test_session(
            client,
            sqlite_settings,
            error_type_id="system_failure",
        )
        response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "请比较两个选择。",
            },
        )

    assert response.status_code == 200
    assert response.json()["assistant_text"] == SYSTEM_FAILURE_TEXT
    assert response.json()["artifact_type"] is None
    assert response.json()["artifact_payload"] is None


def test_historical_decision_artifact_schema_remains_readable() -> None:
    from backend.app.agents.structured import DecisionMatrixArtifact

    artifact = DecisionMatrixArtifact.model_validate(
        {
            "assistant_text": "历史记录",
            "options": [
                {"id": "a", "label": "A", "attributes": {"预算": 10}},
            ],
            "constraints": [
                {"name": "预算", "kind": "max", "value": 20},
            ],
            "weights": [{"criterion": "预算", "weight": 1.0}],
            "recommendation": {
                "option_id": "a",
                "summary": "A",
            },
            "reasons": ["历史兼容"],
        }
    )

    assert artifact.recommendation is not None
    assert artifact.recommendation.option_id == "a"
