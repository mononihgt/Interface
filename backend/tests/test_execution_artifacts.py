from __future__ import annotations

from collections.abc import Sequence
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.app.agents.execution import build_execution_graph
from backend.app.agents.graph_base import GraphInput, build_graph_state
from backend.app.scenarios.registry import ScenarioRegistry
from backend.app.services.providers import (
    ProviderAttempt,
    ProviderMessage,
    ProviderResponse,
)
from backend.app.settings import Settings


TEST_DATE = "2026-07-02"
ADMIN_PASSWORD = "admin-pass-123"
ADMIN_SALT = "execution-artifacts-salt"


def _password_hash(password: str) -> str:
    return hashlib.sha256(f"{ADMIN_SALT}{password}".encode("utf-8")).hexdigest()


class FakeAsrClient:
    def transcribe(
        self,
        *,
        audio_bytes: bytes,
        filename: str,
        content_type: str | None,
        request_id: str,
    ) -> SimpleNamespace:
        del audio_bytes, filename, content_type, request_id
        return SimpleNamespace(
            status="success",
            provider="tencent",
            text="recognized transcript",
            latency_ms=40,
        )


@pytest.fixture
def sqlite_settings(tmp_path: Path) -> Settings:
    db_path = tmp_path / "execution-artifacts.db"
    return Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{db_path}",
        app_secret_key="test-secret-key",
        admin_user="admin",
        admin_password_salt=ADMIN_SALT,
        admin_password_hash=_password_hash(ADMIN_PASSWORD),
        yizhan_api_key="test-yizhan-key",
    )


@pytest.fixture
def conn(sqlite_settings: Settings) -> sqlite3.Connection:
    from backend.app.db import get_connection, run_migrations

    connection = get_connection(sqlite_settings)
    run_migrations(connection)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def client(sqlite_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from backend.app import main as app_main
    from backend.app import services
    from backend.app.services import participant_days

    monkeypatch.setattr(services.participants, "current_shanghai_date", lambda: TEST_DATE)
    monkeypatch.setattr(participant_days, "current_shanghai_date", lambda: TEST_DATE)
    monkeypatch.setattr(
        services.questionnaires,
        "_timestamp_now",
        lambda: "2026-07-02T09:30:00+00:00",
    )
    monkeypatch.setattr(
        app_main,
        "get_asr_client",
        lambda _settings=sqlite_settings: FakeAsrClient(),
    )
    return TestClient(app_main.create_app(settings=sqlite_settings))


def _provider_response(text: str) -> ProviderResponse:
    return ProviderResponse(
        text=text,
        provider="yi-zhan",
        model="gpt-5.1",
        route="execution",
        attempts=[
            ProviderAttempt(
                route="execution",
                provider="yi-zhan",
                model="gpt-5.1",
                status="success",
                latency_ms=22,
            )
        ],
        used_local_fallback=False,
    )


def _structured_result(schema, payload: dict[str, object]):
    from backend.app.agents.structured import StructuredAgentResult

    return StructuredAgentResult(
        value=schema.model_validate(payload),
        response=_provider_response(json.dumps(payload, ensure_ascii=False)),
        validation_error=None,
    )


def _schedule_result(rows: list[dict[str, str]]):
    from backend.app.agents.structured import ScheduleArtifact

    return _structured_result(
        ScheduleArtifact,
        {
            "assistant_text": "我已经根据你提供的安排整理成日程表。",
            "actionType": "schedule_table",
            "actionMode": "create",
            "status": "completed",
            "requestedSource": "用户提供的安排",
            "columns": ["日期", "时间", "地点", "任务", "备注"],
            "rows": rows,
        },
    )


def _copy_result():
    from backend.app.agents.structured import CopyVersionsArtifact

    return _structured_result(
        CopyVersionsArtifact,
        _copy_payload(),
    )


def _configure_test_session(
    sqlite_settings: Settings,
    *,
    session_id: str,
    condition: str,
    subcondition: str,
    topic_key: str,
    error_type_id: str = "logic_minor",
    planned_error_turn: int = 5,
) -> None:
    from backend.app.db import get_connection

    scenario = ScenarioRegistry.load_default().require(
        condition=condition,
        subcondition=subcondition,
        topic_key=topic_key,
    )
    conn = get_connection(sqlite_settings)
    try:
        conn.execute(
            """
            UPDATE experiment_sessions
            SET condition = ?, subcondition = ?, topic_key = ?, scenario_id = ?,
                agent_graph_version = ?, error_type_id = ?, planned_error_turn = ?
            WHERE session_uuid = ?
            """,
            (
                condition,
                subcondition,
                topic_key,
                scenario.scenario_id,
                scenario.graph,
                error_type_id,
                planned_error_turn,
                session_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_test_turn(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    turn_index: int,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO conversation_turns (
            session_id, turn_index, user_text, user_input_mode, asr_status,
            assistant_text, response_latency_ms, llm_provider, llm_model,
            llm_route, llm_attempts_json, error_planned, error_presented,
            error_presentation, agent_state_json
        )
        SELECT id, ?, 'participant sentinel', 'text_test_only', 'not_used',
               'assistant sentinel', 1, 'test-provider', 'test-model', 'test-route',
               '[]', 0, 0, 'none', '{}'
        FROM experiment_sessions
        WHERE session_uuid = ?
        """,
        (turn_index, session_id),
    )
    return int(cursor.lastrowid)


def _insert_test_artifact(
    conn: sqlite3.Connection,
    *,
    turn_id: int,
    status: str,
    payload: dict[str, object],
    visible: bool,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO task_artifacts (
            turn_id, artifact_type, status, payload_json, visible_to_participant
        ) VALUES (?, 'table', ?, ?, ?)
        """,
        (turn_id, status, json.dumps(payload, ensure_ascii=False), int(visible)),
    )
    return int(cursor.lastrowid)


def _table_payload(label: str) -> dict[str, object]:
    return {
        "actionType": "schedule_table",
        "actionMode": "create",
        "status": "completed",
        "requestedSource": label,
        "columns": ["日期", "时间", "地点", "任务", "备注"],
        "rows": [
            {
                "date": "7月13日",
                "time": "09:00",
                "location": "会议室A",
                "task": label,
                "note": "带材料",
            }
        ],
    }


def _copy_payload(
    *,
    action_mode: str = "create",
    status: str = "completed",
    candidate_count: int = 2,
) -> dict[str, object]:
    candidates = [
        {
            "id": "v1",
            "label": "直接礼貌版",
            "text": "不好意思，今晚可能会晚到 10 分钟，麻烦大家先开始。",
        },
        {
            "id": "v2",
            "label": "更柔和版",
            "text": "抱歉和大家说一声，我今晚可能会晚到 10 分钟，辛苦大家先开始。",
        },
        {
            "id": "v3",
            "label": "简洁版",
            "text": "今晚我会晚到 10 分钟，大家先开始，不用等我。",
        },
        {
            "id": "v4",
            "label": "正式版",
            "text": "很抱歉，我今晚预计晚到 10 分钟，请大家按原计划先开始。",
        },
    ][:candidate_count]
    payload: dict[str, object] = {
        "assistant_text": "我整理了两个版本，你可以直接选一个再微调。",
        "actionType": "copy_editor",
        "actionMode": action_mode,
        "status": status,
        "requestedSource": "今晚可能要晚到十分钟，麻烦你们先开始。",
        "label": "迟到通知",
        "candidates": candidates,
        "recommendedIndex": None,
        "selected_version": None,
        "revision_notes": [],
    }
    if status == "completed":
        payload.update(
            {
                "recommendedIndex": 0,
                "selected_version": {
                    "version_id": "v1",
                    "reason": "信息完整、语气礼貌。",
                },
                "revision_notes": ["保留迟到时长。", "调整为更礼貌的表达。"],
            }
        )
    return payload


def _formal_client_info() -> dict[str, object]:
    return {
        "device_type": "desktop",
        "viewport_width": 1280,
        "is_secure_context": True,
        "browser_name": "Chrome",
        "browser_version": "126",
        "microphone_available": True,
        "microphone_permission": "granted",
    }


def _admin_login(client: TestClient) -> TestClient:
    response = client.post(
        "/api/admin/login",
        json={"username": "admin", "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200
    return client


def _start_test_session(client: TestClient) -> dict[str, object]:
    start_response = _admin_login(client).post(
        "/api/test/sessions/start",
        json={
            "is_test": True,
            "client_info": _formal_client_info(),
        },
    )
    assert start_response.status_code == 200
    return start_response.json()


def test_tool_execution_table_artifact_has_required_columns():
    registry = ScenarioRegistry.load_default()
    graph = build_execution_graph(
        provider_runner=lambda _state: _schedule_result(
            [
                {
                    "date": "7月3日",
                    "time": "09:00",
                    "location": "会议室A",
                    "task": "项目会",
                    "note": "无",
                },
                {
                    "date": "7月3日",
                    "time": "14:30",
                    "location": "线上",
                    "task": "提交周报",
                    "note": "带附件",
                },
            ]
        )
    )
    state = build_graph_state(
        session_row={
            "session_uuid": "session-execution-tool-1",
            "participant_id": 31,
            "condition": "tool",
            "subcondition": "execution",
            "topic_key": "taskExecution",
            "scenario_id": "tool_execution_taskExecution_v2",
            "error_type_id": "logic_minor",
            "planned_error_turn": 4,
            "is_test": 1,
        },
        turn_index=1,
        graph_input=GraphInput(
            user_text=(
                "请整理成表格：7月3日 09:00 在会议室A 开项目会；"
                "7月3日 14:30 在线上 提交周报，备注带附件。"
            ),
            input_mode="text_test_only",
        ),
        recent_history=[],
        scenario=registry.require(condition="tool", subcondition="execution", topic_key="taskExecution"),
        graph_version="execution_graph_v2",
    )

    result = graph.run(state)

    assert result.client_response["artifact_type"] == "table"
    payload = result.client_response["artifact_payload"]
    assert payload["columns"] == ["日期", "时间", "地点", "任务", "备注"]
    assert {key: payload["rows"][0][key] for key in payload["columns"]} == {
        "日期": "7月3日",
        "时间": "09:00",
        "地点": "会议室A",
        "任务": "项目会",
        "备注": "无",
    }
    assert payload["rows"][0]["time"] == "09:00"
    assert payload["rows"][1]["备注"] == "带附件"


def test_schedule_schema_rejects_completed_artifact_without_rows():
    from backend.app.agents.structured import ScheduleArtifact, parse_structured_output

    result = parse_structured_output(
        '{"status":"completed","rows":[]}',
        ScheduleArtifact,
    )

    assert result.validation_error == "rows_required_for_completed_artifact"
    assert result.value is None


@pytest.mark.parametrize(
    ("action_mode", "candidate_count"),
    [("create", 2), ("revise", 3)],
)
def test_copy_schema_accepts_completed_create_and_revise_artifacts(
    action_mode: str,
    candidate_count: int,
):
    from backend.app.agents.structured import CopyVersionsArtifact, parse_structured_output

    result = parse_structured_output(
        json.dumps(
            _copy_payload(action_mode=action_mode, candidate_count=candidate_count),
            ensure_ascii=False,
        ),
        CopyVersionsArtifact,
    )

    assert result.validation_error is None
    assert result.value is not None
    assert result.value.action_mode == action_mode
    assert len(result.value.candidates) == candidate_count
    assert result.value.selected_version is not None
    assert result.value.selected_version.version_id == result.value.candidates[0].id


@pytest.mark.parametrize("candidate_count", [1, 4])
def test_copy_schema_requires_two_to_three_completed_candidates(candidate_count: int):
    from backend.app.agents.structured import CopyVersionsArtifact, parse_structured_output

    result = parse_structured_output(
        json.dumps(_copy_payload(candidate_count=candidate_count), ensure_ascii=False),
        CopyVersionsArtifact,
    )

    assert result.value is None
    assert result.validation_error == "invalid_copy_candidate_count"


def test_copy_schema_rejects_completed_clarification():
    from backend.app.agents.structured import CopyVersionsArtifact, parse_structured_output

    result = parse_structured_output(
        json.dumps(_copy_payload(action_mode="clarify"), ensure_ascii=False),
        CopyVersionsArtifact,
    )

    assert result.value is None
    assert result.validation_error == "clarification_cannot_be_completed"


def test_copy_clarification_produces_no_artifact():
    from backend.app.agents.structured import CopyVersionsArtifact

    registry = ScenarioRegistry.load_default()
    graph = build_execution_graph(
        provider_runner=lambda _state: _structured_result(
            CopyVersionsArtifact,
            _copy_payload(
                action_mode="clarify",
                status="pending",
                candidate_count=0,
            ),
        )
    )
    state = build_graph_state(
        session_row={
            "session_uuid": "session-execution-copy-clarify-1",
            "participant_id": 40,
            "condition": "human",
            "subcondition": "execution",
            "topic_key": "collaborativeExecution",
            "scenario_id": "human_execution_collaborativeExecution_v2",
            "error_type_id": "logic_minor",
            "planned_error_turn": 4,
            "is_test": 1,
        },
        turn_index=1,
        graph_input=GraphInput(
            user_text="请帮我润色一下。",
            input_mode="text_test_only",
        ),
        recent_history=[],
        scenario=registry.require(condition="human", subcondition="execution", topic_key="collaborativeExecution"),
        graph_version="execution_graph_v2",
    )

    result = graph.run(state)

    assert result.client_response["assistant_text"] == _copy_payload(
        action_mode="clarify",
        status="pending",
        candidate_count=0,
    )["assistant_text"]
    assert result.client_response["artifact_type"] is None
    assert result.client_response["artifact_payload"] is None
    assert result.state.artifact_validation_error is None


def test_execution_validation_failure_does_not_invent_schedule_rows():
    from backend.app.agents.structured import StructuredAgentResult

    registry = ScenarioRegistry.load_default()
    graph = build_execution_graph(
        provider_runner=lambda _state: StructuredAgentResult(
            value=None,
            response=_provider_response("not valid structured output"),
            validation_error="invalid_json_object",
        )
    )
    state = build_graph_state(
        session_row={
            "session_uuid": "session-execution-invalid-1",
            "participant_id": 39,
            "condition": "tool",
            "subcondition": "execution",
            "topic_key": "taskExecution",
            "scenario_id": "tool_execution_taskExecution_v2",
            "error_type_id": "logic_minor",
            "planned_error_turn": 4,
            "is_test": 1,
        },
        turn_index=1,
        graph_input=GraphInput(
            user_text="请帮我整理，但我没有提供具体安排。",
            input_mode="text_test_only",
        ),
        recent_history=[],
        scenario=registry.require(condition="tool", subcondition="execution", topic_key="taskExecution"),
        graph_version="execution_graph_v2",
    )

    result = graph.run(state)

    assert result.client_response["artifact_type"] is None
    assert result.client_response["artifact_payload"] is None
    assert result.state.artifact_validation_error == "invalid_json_object"


def test_execution_schema_failure_records_sanitized_failed_artifact_and_risk_flag(
    client: TestClient,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.agents.structured import StructuredAgentResult
    from backend.app.db import get_connection
    from backend.app.services.sessions import ProviderRouter

    with client:
        session_payload = _start_test_session(client)

    session_id = str(session_payload["session_id"])
    scenario = ScenarioRegistry.load_default().require(condition="tool", subcondition="execution", topic_key="taskExecution")
    conn = get_connection(sqlite_settings)
    try:
        conn.execute(
            """
            UPDATE experiment_sessions
            SET condition = ?, subcondition = ?, topic_key = ?, scenario_id = ?, agent_graph_version = ?,
                error_type_id = ?, planned_error_turn = ?
            WHERE session_uuid = ?
            """,
            (
                scenario.condition,
                scenario.subcondition,
                scenario.topic_key,
                scenario.scenario_id,
                scenario.graph,
                "logic_minor",
                4,
                session_id,
            ),
        )
    finally:
        conn.close()

    async def _invalid_generation(self: ProviderRouter, **_: object):
        del self
        return StructuredAgentResult(
            value=None,
            response=_provider_response("invalid structured output"),
            validation_error="invalid_json_object",
        )

    monkeypatch.setattr(
        ProviderRouter,
        "generate_structured_agent",
        _invalid_generation,
    )

    with client:
        response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "我还没有准备好具体安排。",
            },
        )

    assert response.status_code == 200
    assert response.json()["artifact_type"] is None
    assert response.json()["artifact_payload"] is None

    conn = get_connection(sqlite_settings)
    try:
        artifact_row = conn.execute(
            """
            SELECT a.status, a.payload_json, a.visible_to_participant
            FROM task_artifacts a
            JOIN conversation_turns t ON t.id = a.turn_id
            JOIN experiment_sessions s ON s.id = t.session_id
            WHERE s.session_uuid = ?
            ORDER BY a.id DESC
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        flag_row = conn.execute(
            """
            SELECT f.flag, f.detail_json
            FROM session_risk_flags f
            JOIN experiment_sessions s ON s.id = f.session_id
            WHERE s.session_uuid = ? AND f.flag = 'artifact_schema_error'
            """,
            (session_id,),
        ).fetchone()
    finally:
        conn.close()

    assert artifact_row["status"] == "failed"
    assert artifact_row["visible_to_participant"] == 0
    assert json.loads(artifact_row["payload_json"]) == {
        "code": "artifact_schema_invalid"
    }
    assert flag_row["flag"] == "artifact_schema_error"
    assert json.loads(flag_row["detail_json"]) == {
        "turn_index": 1,
        "validation_error": "invalid_json_object",
    }


def test_execution_normalizes_provider_result_for_visible_workspace(
    client: TestClient,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.agents.structured import StructuredAgentResult
    from backend.app.db import get_connection
    from backend.app.services.sessions import ProviderRouter

    with client:
        session_id = str(_start_test_session(client)["session_id"])
    _configure_test_session(
        sqlite_settings,
        session_id=session_id,
        condition="tool",
        subcondition="execution",
        topic_key="taskExecution",
    )

    raw_payload = {
        "assistant_text": "我已经整理好了。",
        "status": "completed",
        "columns": ["时间", "事项", "位置"],
        "rows": [
            {
                "日期": "明天",
                "时间": "下午3点",
                "地点": "会议室",
                "任务": "召开项目复盘会",
                "备注": "持续1小时",
            }
        ],
    }

    async def _invalid_columns_generation(
        self: ProviderRouter,
        *,
        request_id: str,
        messages: Sequence[ProviderMessage],
        is_test: bool,
        schema,
        payload_normalizer,
    ):
        del self, request_id
        assert is_test is True
        assert callable(payload_normalizer)
        assert "右侧执行工作区" in messages[0].content
        assert messages[-1].content.startswith("请帮我建立日程")
        return StructuredAgentResult(
            value=None,
            response=_provider_response(json.dumps(raw_payload, ensure_ascii=False)),
            validation_error="invalid_schedule_columns",
            parse_attempts=2,
        )

    monkeypatch.setattr(
        ProviderRouter,
        "generate_structured_agent",
        _invalid_columns_generation,
    )

    with client:
        turn_response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "请帮我建立日程：明天下午3点在会议室召开项目复盘会，持续1小时。",
            },
        )
        session_response = client.get(f"/api/sessions/{session_id}")

    assert turn_response.status_code == 200
    assert session_response.status_code == 200
    turn_payload = turn_response.json()
    assert turn_payload["artifact_type"] == "table"
    assert turn_payload["artifact_payload"]["columns"] == ["日期", "时间", "地点", "任务", "备注"]
    assert turn_payload["artifact_payload"]["rows"][0]["time"] == "15:00"
    assert session_response.json()["artifact_status"] == "completed"
    assert session_response.json()["artifact_payload"]["rows"][0]["task"] == "召开项目复盘会"

    conn = get_connection(sqlite_settings)
    try:
        artifact_row = conn.execute(
            """
            SELECT a.status, a.visible_to_participant
            FROM task_artifacts a
            JOIN conversation_turns t ON t.id = a.turn_id
            JOIN experiment_sessions s ON s.id = t.session_id
            WHERE s.session_uuid = ?
            ORDER BY a.id DESC
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
    finally:
        conn.close()

    assert artifact_row["status"] == "completed"
    assert artifact_row["visible_to_participant"] == 1


def test_execution_uses_input_derived_workspace_when_provider_json_is_invalid(
    client: TestClient,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.agents.structured import StructuredAgentResult
    from backend.app.services.sessions import ProviderRouter

    with client:
        session_id = str(_start_test_session(client)["session_id"])
    _configure_test_session(
        sqlite_settings,
        session_id=session_id,
        condition="tool",
        subcondition="execution",
        topic_key="taskExecution",
    )

    async def _invalid_generation(self: ProviderRouter, **_: object):
        del self
        return StructuredAgentResult(
            value=None,
            response=_provider_response("not json"),
            validation_error="invalid_json_object",
            parse_attempts=2,
        )

    monkeypatch.setattr(ProviderRouter, "generate_structured_agent", _invalid_generation)

    with client:
        response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "请帮我建立日程：明天下午3点在会议室召开项目复盘会，持续1小时。",
            },
        )

    assert response.status_code == 200
    assert response.json()["artifact_type"] == "table"
    assert response.json()["artifact_payload"]["rows"][0]["任务"] == "召开项目复盘会"


def test_human_execution_correction_removes_prior_wrong_fact_from_workspace(
    client: TestClient,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.agents.structured import CopyVersionsArtifact, StructuredAgentResult
    from backend.app.services.sessions import ProviderRouter

    with client:
        session_id = str(_start_test_session(client)["session_id"])
    _configure_test_session(
        sqlite_settings,
        session_id=session_id,
        condition="human",
        subcondition="execution",
        topic_key="collaborativeExecution",
    )

    wrong_payload = {
        "assistant_text": "我整理了几个版本。",
        "actionType": "copy_editor",
        "actionMode": "create",
        "status": "completed",
        "requestedSource": "今天在长江边散步",
        "label": "朋友圈文案",
        "candidates": [
            {"id": "v1", "label": "自然版", "text": "今天在长江边散步，风很舒服。"},
            {"id": "v2", "label": "简洁版", "text": "长江边走一走，心情也放松了。"},
        ],
        "recommendedIndex": 0,
        "selected_version": {"version_id": "v1", "reason": "表达自然。"},
        "revision_notes": ["保留长江这一地点事实。"],
    }
    call_count = 0

    async def _generation(self: ProviderRouter, **_: object):
        nonlocal call_count
        del self
        call_count += 1
        if call_count == 1:
            return _structured_result(CopyVersionsArtifact, wrong_payload)
        return StructuredAgentResult(
            value=None,
            response=_provider_response("not json"),
            validation_error="invalid_json_object",
            parse_attempts=2,
        )

    monkeypatch.setattr(ProviderRouter, "generate_structured_agent", _generation)

    with client:
        first_turn = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "请写一条今天在河边散步的朋友圈文案。",
            },
        )
        assert first_turn.status_code == 200
        rating = client.post(
            f"/api/turns/{first_turn.json()['turn_id']}/rating",
            json={
                "stance_score": 3,
                "trust_score": 3,
                "client_elapsed_ms": 1000,
            },
        )
        assert rating.status_code == 200
        correction = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "并非长江。",
            },
        )
        restored = client.get(f"/api/sessions/{session_id}")

    assert correction.status_code == 200
    assert restored.status_code == 200
    assert correction.json()["artifact_type"] == "copy_versions"
    assert "长江" not in json.dumps(
        correction.json()["artifact_payload"],
        ensure_ascii=False,
    )
    assert "长江" not in json.dumps(
        restored.json()["artifact_payload"],
        ensure_ascii=False,
    )


@pytest.mark.parametrize(
    ("structured_status", "database_status", "safe_payload", "public_status"),
    [
        (
            "pending",
            "draft",
            {"code": "structured_result_pending"},
            "awaiting_input",
        ),
        (
            "failed",
            "failed",
            {"code": "structured_result_failed"},
            "failed",
        ),
    ],
)
def test_execution_noncompleted_structured_outcomes_are_hidden_and_sanitized(
    client: TestClient,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    structured_status: str,
    database_status: str,
    safe_payload: dict[str, object],
    public_status: str,
):
    from backend.app.agents.structured import ScheduleArtifact
    from backend.app.db import get_connection
    from backend.app.services.sessions import ProviderRouter

    with client:
        session_id = str(_start_test_session(client)["session_id"])
    _configure_test_session(
        sqlite_settings,
        session_id=session_id,
        condition="tool",
        subcondition="execution",
        topic_key="taskExecution",
    )

    sentinel = "participant input and model free text must not persist"

    async def _structured_outcome(self: ProviderRouter, **_: object):
        del self
        return _structured_result(
            ScheduleArtifact,
            {
                "assistant_text": sentinel,
                "actionType": "schedule_table",
                "actionMode": "clarify" if structured_status == "pending" else "create",
                "status": structured_status,
                "requestedSource": sentinel,
                "columns": ["日期", "时间", "地点", "任务", "备注"],
                "rows": [],
            },
        )

    monkeypatch.setattr(
        ProviderRouter,
        "generate_structured_agent",
        _structured_outcome,
    )

    with client:
        turn_response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": sentinel,
            },
        )
        restore_response = client.get(f"/api/sessions/{session_id}")

    assert turn_response.status_code == 200
    assert restore_response.status_code == 200
    assert restore_response.json()["artifact_status"] == public_status

    conn = get_connection(sqlite_settings)
    try:
        artifact_row = conn.execute(
            """
            SELECT a.status, a.payload_json, a.visible_to_participant
            FROM task_artifacts a
            JOIN conversation_turns t ON t.id = a.turn_id
            JOIN experiment_sessions s ON s.id = t.session_id
            WHERE s.session_uuid = ?
            ORDER BY a.id DESC
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
    finally:
        conn.close()

    assert artifact_row["status"] == database_status
    assert artifact_row["visible_to_participant"] == 0
    assert json.loads(artifact_row["payload_json"]) == safe_payload
    assert sentinel not in artifact_row["payload_json"]


def test_tool_execution_table_artifact_preserves_legitimate_leading_kai_tasks():
    registry = ScenarioRegistry.load_default()
    graph = build_execution_graph(
        provider_runner=lambda _state: _schedule_result(
            [
                {"date": "7月5日", "time": "09:00", "location": "访谈室", "task": "开展访谈", "note": ""},
                {"date": "7月5日", "time": "11:00", "location": "财务室", "task": "开发票", "note": ""},
                {"date": "7月5日", "time": "15:00", "location": "设备间", "task": "开机检查", "note": ""},
            ]
        )
    )
    state = build_graph_state(
        session_row={
            "session_uuid": "session-execution-tool-keep-kai-1",
            "participant_id": 35,
            "condition": "tool",
            "subcondition": "execution",
            "topic_key": "taskExecution",
            "scenario_id": "tool_execution_taskExecution_v2",
            "error_type_id": "logic_minor",
            "planned_error_turn": 4,
            "is_test": 1,
        },
        turn_index=1,
        graph_input=GraphInput(
            user_text=(
                "请整理成表格：7月5日 09:00 在访谈室 开展访谈；"
                "7月5日 11:00 到财务室 开发票；"
                "7月5日 15:00 在设备间 开机检查。"
            ),
            input_mode="text_test_only",
        ),
        recent_history=[],
        scenario=registry.require(condition="tool", subcondition="execution", topic_key="taskExecution"),
        graph_version="execution_graph_v2",
    )

    result = graph.run(state)

    payload = result.client_response["artifact_payload"]
    assert [row["任务"] for row in payload["rows"]] == ["开展访谈", "开发票", "开机检查"]


def test_tool_execution_artifact_uses_validated_agent_rows():
    registry = ScenarioRegistry.load_default()
    graph = build_execution_graph(
        provider_runner=lambda _state: _schedule_result(
            [
                {"date": "7月4日", "time": "10:15", "location": "实验室A", "task": "准备材料", "note": "带电脑"},
                {"date": "7月4日", "time": "16:40", "location": "线上", "task": "提交总结", "note": ""},
            ]
        )
    )
    state = build_graph_state(
        session_row={
            "session_uuid": "session-execution-tool-provider-1",
            "participant_id": 32,
            "condition": "tool",
            "subcondition": "execution",
            "topic_key": "taskExecution",
            "scenario_id": "tool_execution_taskExecution_v2",
            "error_type_id": "logic_minor",
            "planned_error_turn": 4,
            "is_test": 1,
        },
        turn_index=1,
        graph_input=GraphInput(
            user_text="请帮我整理一下安排。",
            input_mode="text_test_only",
        ),
        recent_history=[],
        scenario=registry.require(condition="tool", subcondition="execution", topic_key="taskExecution"),
        graph_version="execution_graph_v2",
    )

    result = graph.run(state)

    payload = result.client_response["artifact_payload"]
    assert payload["rows"][0]["日期"] == "7月4日"
    assert payload["rows"][0]["地点"] == "实验室A"
    assert payload["rows"][1]["任务"] == "提交总结"


def test_tool_execution_artifact_does_not_locally_merge_participant_text():
    registry = ScenarioRegistry.load_default()
    graph = build_execution_graph(
        provider_runner=lambda _state: _schedule_result(
            [
                {"date": "7月4日", "time": "10:15", "location": "实验室A", "task": "准备材料", "note": "带电脑"},
                {"date": "7月4日", "time": "16:40", "location": "线上", "task": "提交总结", "note": "发群里"},
            ]
        )
    )
    state = build_graph_state(
        session_row={
            "session_uuid": "session-execution-tool-provider-merge-1",
            "participant_id": 33,
            "condition": "tool",
            "subcondition": "execution",
            "topic_key": "taskExecution",
            "scenario_id": "tool_execution_taskExecution_v2",
            "error_type_id": "logic_minor",
            "planned_error_turn": 4,
            "is_test": 1,
        },
        turn_index=1,
        graph_input=GraphInput(
            user_text="请整理一下：7月4日 10:15 准备材料。",
            input_mode="text_test_only",
        ),
        recent_history=[],
        scenario=registry.require(condition="tool", subcondition="execution", topic_key="taskExecution"),
        graph_version="execution_graph_v2",
    )

    result = graph.run(state)

    payload = result.client_response["artifact_payload"]
    assert {key: payload["rows"][0][key] for key in payload["columns"]} == {
        "日期": "7月4日",
        "时间": "10:15",
        "地点": "实验室A",
        "任务": "准备材料",
        "备注": "带电脑",
    }
    assert payload["rows"][0]["location"] == "实验室A"
    assert any(row["任务"] == "提交总结" and row["备注"] == "发群里" for row in payload["rows"])


def test_tool_execution_artifact_keeps_validated_task_when_user_text_is_partial():
    registry = ScenarioRegistry.load_default()
    graph = build_execution_graph(
        provider_runner=lambda _state: _schedule_result(
            [
                {"date": "7月4日", "time": "10:15", "location": "实验室A", "task": "准备材料", "note": "带电脑"},
            ]
        )
    )
    state = build_graph_state(
        session_row={
            "session_uuid": "session-execution-tool-provider-partial-case-1",
            "participant_id": 34,
            "condition": "tool",
            "subcondition": "execution",
            "topic_key": "taskExecution",
            "scenario_id": "tool_execution_taskExecution_v2",
            "error_type_id": "logic_minor",
            "planned_error_turn": 4,
            "is_test": 1,
        },
        turn_index=1,
        graph_input=GraphInput(
            user_text="请整理一下：7月4日 10:15 准备。",
            input_mode="text_test_only",
        ),
        recent_history=[],
        scenario=registry.require(condition="tool", subcondition="execution", topic_key="taskExecution"),
        graph_version="execution_graph_v2",
    )

    result = graph.run(state)

    payload = result.client_response["artifact_payload"]
    assert {key: payload["rows"][0][key] for key in payload["columns"]} == {
        "日期": "7月4日",
        "时间": "10:15",
        "地点": "实验室A",
        "任务": "准备材料",
        "备注": "带电脑",
    }
    assert payload["rows"][0]["task"] == "准备材料"


def test_human_execution_copy_versions_artifact_has_selected_version(
    client: TestClient,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.db import get_connection
    from backend.app.services.sessions import ProviderRouter

    with client:
        session_payload = _start_test_session(client)

    session_id = str(session_payload["session_id"])
    scenario = ScenarioRegistry.load_default().require(condition="human", subcondition="execution", topic_key="collaborativeExecution")

    conn = get_connection(sqlite_settings)
    try:
        conn.execute(
            """
            UPDATE experiment_sessions
            SET condition = ?, subcondition = ?, topic_key = ?, scenario_id = ?, agent_graph_version = ?,
                error_type_id = ?, planned_error_turn = ?
            WHERE session_uuid = ?
            """,
            (
                scenario.condition,
                scenario.subcondition,
                scenario.topic_key,
                scenario.scenario_id,
                scenario.graph,
                "social_minor",
                4,
                session_id,
            ),
        )
    finally:
        conn.close()

    async def _successful_generation(
        self: ProviderRouter,
        *,
        request_id: str,
        messages: Sequence[ProviderMessage],
        is_test: bool,
        schema,
        payload_normalizer,
    ):
        from backend.app.agents.structured import CopyVersionsArtifact

        del self, request_id
        assert is_test is True
        assert callable(payload_normalizer)
        assert schema is CopyVersionsArtifact
        assert scenario.provider_system_prompt in messages[0].content
        assert "[指令：正常操作]" in messages[0].content
        assert messages[-1] == ProviderMessage(
            role="user",
            content="帮我把消息改得更礼貌一些：今晚可能要晚到十分钟，麻烦你们先开始。",
        )
        return _copy_result()

    monkeypatch.setattr(ProviderRouter, "generate_structured_agent", _successful_generation)

    with client:
        response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "帮我把消息改得更礼貌一些：今晚可能要晚到十分钟，麻烦你们先开始。",
            },
        )

    assert response.status_code == 200

    conn = get_connection(sqlite_settings)
    try:
        artifact_row = conn.execute(
            """
            SELECT a.artifact_type, a.status, a.payload_json, a.visible_to_participant
            FROM task_artifacts a
            JOIN conversation_turns t ON t.id = a.turn_id
            JOIN experiment_sessions s ON s.id = t.session_id
            WHERE s.session_uuid = ?
            ORDER BY a.id DESC
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
    finally:
        conn.close()

    assert artifact_row["artifact_type"] == "copy_versions"
    assert artifact_row["status"] == "completed"
    assert artifact_row["visible_to_participant"] == 1

    payload = json.loads(artifact_row["payload_json"])
    assert payload["versions"]
    assert payload["selected_version"]
    assert payload["revision_notes"]
    assert payload["selected_version"]["version_id"] == "v1"
    assert "10 分钟" in payload["versions"][0]["text"]
    assert payload["versions"][1]["label"] == "更柔和版"


def test_execution_system_failure_persists_failed_hidden_artifact(
    client: TestClient,
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection

    with client:
        session_payload = _start_test_session(client)

    session_id = str(session_payload["session_id"])
    scenario = ScenarioRegistry.load_default().require(condition="human", subcondition="execution", topic_key="collaborativeExecution")

    conn = get_connection(sqlite_settings)
    try:
        conn.execute(
            """
            UPDATE experiment_sessions
            SET condition = ?, subcondition = ?, topic_key = ?, scenario_id = ?, agent_graph_version = ?,
                error_type_id = ?, planned_error_turn = ?
            WHERE session_uuid = ?
            """,
            (
                scenario.condition,
                scenario.subcondition,
                scenario.topic_key,
                scenario.scenario_id,
                scenario.graph,
                "system_failure",
                1,
                session_id,
            ),
        )
    finally:
        conn.close()

    with client:
        response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "帮我改一下通知文案：今晚可能晚到十分钟。",
            },
        )

    assert response.status_code == 200

    conn = get_connection(sqlite_settings)
    try:
        artifact_rows = conn.execute(
            """
            SELECT a.artifact_type, a.status, a.payload_json, a.visible_to_participant
            FROM task_artifacts a
            JOIN conversation_turns t ON t.id = a.turn_id
            JOIN experiment_sessions s ON s.id = t.session_id
            WHERE s.session_uuid = ?
            ORDER BY a.id ASC
            """,
            (session_id,),
        ).fetchall()
    finally:
        conn.close()

    assert len(artifact_rows) == 1
    artifact_row = artifact_rows[0]
    assert artifact_row["artifact_type"] == "copy_versions"
    assert artifact_row["status"] == "failed"
    assert artifact_row["visible_to_participant"] == 0
    assert json.loads(artifact_row["payload_json"]) == {"code": "system_failure"}


def test_execution_turn_and_session_api_expose_artifact_and_debug_metadata(
    client: TestClient,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.db import get_connection
    from backend.app.services.sessions import ProviderRouter

    with client:
        session_payload = _start_test_session(client)

    session_id = str(session_payload["session_id"])
    scenario = ScenarioRegistry.load_default().require(condition="human", subcondition="execution", topic_key="collaborativeExecution")

    conn = get_connection(sqlite_settings)
    try:
        conn.execute(
            """
            UPDATE experiment_sessions
            SET condition = ?, subcondition = ?, topic_key = ?, scenario_id = ?, agent_graph_version = ?,
                error_type_id = ?, planned_error_turn = ?
            WHERE session_uuid = ?
            """,
            (
                scenario.condition,
                scenario.subcondition,
                scenario.topic_key,
                scenario.scenario_id,
                scenario.graph,
                "social_minor",
                1,
                session_id,
            ),
        )
    finally:
        conn.close()

    async def _successful_generation(
        self: ProviderRouter,
        *,
        request_id: str,
        messages: Sequence[ProviderMessage],
        is_test: bool,
        schema,
        allow_local_fallback: bool = True,
        payload_normalizer=None,
    ):
        from backend.app.agents.structured import CopyVersionsArtifact

        del self
        assert request_id == f"{session_id}-turn-1-semantic-1"
        assert is_test is True
        assert schema is CopyVersionsArtifact
        assert allow_local_fallback is False
        assert callable(payload_normalizer)
        assert scenario.provider_system_prompt in messages[0].content
        assert any(
            "[指令：激活错误 -> social_minor]" in message.content
            and "降低共情水平" in message.content
            for message in messages
        )
        assert messages[-1] == ProviderMessage(
            role="user",
            content="帮我把消息改得更礼貌一些：今晚可能要晚到十分钟，麻烦你们先开始。",
        )
        return _copy_result()

    monkeypatch.setattr(ProviderRouter, "generate_structured_agent", _successful_generation)

    with client:
        turn_response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "帮我把消息改得更礼貌一些：今晚可能要晚到十分钟，麻烦你们先开始。",
            },
        )
        session_response = client.get(f"/api/sessions/{session_id}")

    assert turn_response.status_code == 200
    assert session_response.status_code == 200

    turn_payload = turn_response.json()
    session_view = session_response.json()
    restored_turn = session_view["turns"][0]

    for payload in (turn_payload, restored_turn):
        assert payload["artifact_type"] == "copy_versions"
        assert payload["artifact_payload"]["versions"]
        assert payload["provider_attempts"][0]["provider"] == "yi-zhan"
        assert payload["graph_trace"]["subcondition"] == "execution"
        assert payload["graph_trace"]["artifact_type"] == "copy_versions"
        assert payload["evaluator_result"]["status"] == "success"
        assert payload["graph_trace"]["evaluator_result"]["status"] == "success"

    assert session_view["artifact_type"] == "copy_versions"
    assert session_view["artifact_payload"]["versions"]
    assert session_view["presentation_mode"] == "execution"
    assert session_view["artifact_kind"] == "copy_editor"
    assert session_view["artifact_status"] == "completed"
    assert session_view["provider_attempts"][0]["provider"] == "yi-zhan"
    assert session_view["graph_trace"]["subcondition"] == "execution"
    assert session_view["evaluator_result"]["status"] == "success"
    assert session_view["graph_trace"]["evaluator_result"]["status"] == "success"


def test_execution_session_starts_with_stable_empty_workspace(
    client: TestClient,
    sqlite_settings: Settings,
):
    with client:
        session_id = str(_start_test_session(client)["session_id"])
        _configure_test_session(
            sqlite_settings,
            session_id=session_id,
            condition="tool",
            subcondition="execution",
            topic_key="taskExecution",
        )
        response = client.get(f"/api/sessions/{session_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["presentation_mode"] == "execution"
    assert payload["artifact_kind"] == "schedule_table"
    assert payload["artifact_status"] == "none"
    assert payload["artifact_type"] is None
    assert payload["artifact_payload"] is None


@pytest.mark.parametrize(
    ("subcondition", "topic_key"),
    [
        ("qa", "physics"),
        ("planning", "travelPlan"),
        ("chat", "news"),
        ("decision", "valueDecision"),
    ],
)
def test_non_execution_sessions_use_conversation_presentation_contract(
    client: TestClient,
    sqlite_settings: Settings,
    subcondition: str,
    topic_key: str,
):
    with client:
        session_id = str(_start_test_session(client)["session_id"])
        _configure_test_session(
            sqlite_settings,
            session_id=session_id,
            condition="tool",
            subcondition=subcondition,
            topic_key=topic_key,
        )
        response = client.get(f"/api/sessions/{session_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["presentation_mode"] == "conversation"
    assert payload["artifact_kind"] is None
    assert payload["artifact_status"] == "none"


def test_execution_session_keeps_latest_success_while_latest_outcome_awaits_input(
    client: TestClient,
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection

    with client:
        session_id = str(_start_test_session(client)["session_id"])
        _configure_test_session(
            sqlite_settings,
            session_id=session_id,
            condition="tool",
            subcondition="execution",
            topic_key="taskExecution",
        )

        conn = get_connection(sqlite_settings)
        try:
            first_turn_id = _insert_test_turn(conn, session_id=session_id, turn_index=1)
            _insert_test_artifact(
                conn,
                turn_id=first_turn_id,
                status="completed",
                payload=_table_payload("first"),
                visible=True,
            )
            second_turn_id = _insert_test_turn(conn, session_id=session_id, turn_index=2)
            _insert_test_artifact(
                conn,
                turn_id=second_turn_id,
                status="draft",
                payload={},
                visible=False,
            )
            conn.commit()
        finally:
            conn.close()

        response = client.get(f"/api/sessions/{session_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["artifact_status"] == "awaiting_input"
    assert payload["artifact_type"] == "table"
    assert payload["artifact_payload"] == _table_payload("first")


def test_execution_session_revision_replaces_latest_successful_payload(
    client: TestClient,
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection

    with client:
        session_id = str(_start_test_session(client)["session_id"])
        _configure_test_session(
            sqlite_settings,
            session_id=session_id,
            condition="tool",
            subcondition="execution",
            topic_key="taskExecution",
        )

        conn = get_connection(sqlite_settings)
        try:
            first_turn_id = _insert_test_turn(conn, session_id=session_id, turn_index=1)
            _insert_test_artifact(
                conn,
                turn_id=first_turn_id,
                status="completed",
                payload=_table_payload("first"),
                visible=True,
            )
            second_turn_id = _insert_test_turn(conn, session_id=session_id, turn_index=2)
            _insert_test_artifact(
                conn,
                turn_id=second_turn_id,
                status="completed",
                payload=_table_payload("revised"),
                visible=True,
            )
            conn.commit()
        finally:
            conn.close()

        response = client.get(f"/api/sessions/{session_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["artifact_status"] == "completed"
    assert payload["artifact_payload"] == _table_payload("revised")


def test_execution_session_keeps_latest_success_when_latest_outcome_failed(
    client: TestClient,
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection

    with client:
        session_id = str(_start_test_session(client)["session_id"])
        _configure_test_session(
            sqlite_settings,
            session_id=session_id,
            condition="tool",
            subcondition="execution",
            topic_key="taskExecution",
        )

        conn = get_connection(sqlite_settings)
        try:
            first_turn_id = _insert_test_turn(conn, session_id=session_id, turn_index=1)
            _insert_test_artifact(
                conn,
                turn_id=first_turn_id,
                status="completed",
                payload=_table_payload("first"),
                visible=True,
            )
            second_turn_id = _insert_test_turn(conn, session_id=session_id, turn_index=2)
            _insert_test_artifact(
                conn,
                turn_id=second_turn_id,
                status="failed",
                payload={"code": "artifact_schema_invalid"},
                visible=False,
            )
            conn.commit()
        finally:
            conn.close()

        response = client.get(f"/api/sessions/{session_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["artifact_status"] == "failed"
    assert payload["artifact_type"] == "table"
    assert payload["artifact_payload"] == _table_payload("first")


def test_artifact_repository_separates_latest_outcome_from_visible_completed_payload(
    client: TestClient,
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection
    from backend.app.repositories.artifacts import (
        get_latest_artifact_status_for_session,
        get_latest_visible_completed_artifact_for_session,
        get_visible_artifact_for_turn,
    )

    with client:
        session_id = str(_start_test_session(client)["session_id"])

    conn = get_connection(sqlite_settings)
    try:
        first_turn_id = _insert_test_turn(conn, session_id=session_id, turn_index=1)
        _insert_test_artifact(
            conn,
            turn_id=first_turn_id,
            status="completed",
            payload=_table_payload("first"),
            visible=True,
        )
        second_turn_id = _insert_test_turn(conn, session_id=session_id, turn_index=2)
        _insert_test_artifact(
            conn,
            turn_id=second_turn_id,
            status="draft",
            payload={"must": "remain hidden"},
            visible=True,
        )
        conn.commit()

        session_row = conn.execute(
            "SELECT id FROM experiment_sessions WHERE session_uuid = ?",
            (session_id,),
        ).fetchone()
        latest_status = get_latest_artifact_status_for_session(
            conn,
            session_id=int(session_row["id"]),
        )
        latest_completed = get_latest_visible_completed_artifact_for_session(
            conn,
            session_id=int(session_row["id"]),
        )
        hidden_turn_artifact = get_visible_artifact_for_turn(
            conn,
            turn_id=second_turn_id,
        )
    finally:
        conn.close()

    assert latest_status["status"] == "draft"
    assert latest_completed["status"] == "completed"
    assert json.loads(latest_completed["payload_json"]) == _table_payload("first")
    assert hidden_turn_artifact is None


@pytest.mark.parametrize("corruption", ["wrong_kind", "invalid_schema", "invalid_json"])
def test_execution_restore_rejects_corrupt_latest_artifact_and_keeps_prior_success(
    client: TestClient,
    sqlite_settings: Settings,
    corruption: str,
):
    from backend.app.db import get_connection

    with client:
        session_id = str(_start_test_session(client)["session_id"])
        _configure_test_session(
            sqlite_settings,
            session_id=session_id,
            condition="tool",
            subcondition="execution",
            topic_key="taskExecution",
        )

    conn = get_connection(sqlite_settings)
    try:
        first_turn_id = _insert_test_turn(conn, session_id=session_id, turn_index=1)
        _insert_test_artifact(
            conn,
            turn_id=first_turn_id,
            status="completed",
            payload=_table_payload("first"),
            visible=True,
        )
        second_turn_id = _insert_test_turn(conn, session_id=session_id, turn_index=2)
        if corruption == "wrong_kind":
            conn.execute(
                """
                INSERT INTO task_artifacts (
                    turn_id, artifact_type, status, payload_json, visible_to_participant
                ) VALUES (?, 'copy_versions', 'completed', ?, 1)
                """,
                (second_turn_id, json.dumps(_copy_payload(), ensure_ascii=False)),
            )
        elif corruption == "invalid_schema":
            _insert_test_artifact(
                conn,
                turn_id=second_turn_id,
                status="completed",
                payload={"invalid": True},
                visible=True,
            )
        else:
            conn.execute(
                """
                INSERT INTO task_artifacts (
                    turn_id, artifact_type, status, payload_json, visible_to_participant
                ) VALUES (?, 'table', 'completed', '{', 1)
                """,
                (second_turn_id,),
            )
        conn.commit()
    finally:
        conn.close()

    with client:
        response = client.get(f"/api/sessions/{session_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["artifact_status"] == "failed"
    assert payload["artifact_type"] == "table"
    assert payload["artifact_payload"] == _table_payload("first")
