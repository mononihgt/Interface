from __future__ import annotations

from collections.abc import Sequence
import hashlib
import json
from datetime import date
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.tests.audio_fixtures import VALID_WEBM_AUDIO
from backend.app.security import read_signed_session, sign_session_payload
from backend.app.services.providers import (
    ProviderMessage,
    ProviderResponse,
    ProviderRouter,
)
from backend.app.settings import Settings


TEST_DATE = "2026-07-02"
ADMIN_PASSWORD = "admin-pass-123"
ADMIN_SALT = "session-state-salt"


def _password_hash(password: str) -> str:
    return hashlib.sha256(f"{ADMIN_SALT}{password}".encode("utf-8")).hexdigest()


class FakeAsrClient:
    def __init__(self) -> None:
        self._counter = 0

    def transcribe(
        self,
        *,
        audio_bytes: bytes,
        filename: str,
        content_type: str | None,
        request_id: str,
    ) -> SimpleNamespace:
        self._counter += 1
        return SimpleNamespace(
            status="success",
            provider="tencent",
            text=f"recognized transcript {self._counter}",
            latency_ms=40,
        )


def build_pretest_payload(*, trust_score: int = 4) -> dict[str, object]:
    scales = {f"q{index}": 3 for index in range(1, 27)}
    scales.update({f"q{index}": 50 for index in range(27, 48)})
    scales.update({f"confidence_q{index}": 75 for index in range(27, 47)})
    scales["q21"] = trust_score
    scales["q48"] = "B"
    scales["q49"] = "C"

    slider_touch_state = {f"q{index}": True for index in range(27, 48)}
    slider_touch_state.update(
        {f"confidence_q{index}": True for index in range(27, 47)}
    )

    return {
        "demographics": {
            "birthDate": "2000-01-01",
            "gender": "男",
            "idNumber": "ID1234567",
        },
        "scales": scales,
        "slider_touch_state": slider_touch_state,
        "page_progress": {
            "section": "save",
            "current_step": "save",
            "completed_steps": ["intro", "demographics", "scales"],
        },
        "client_timestamp": "2026-07-02T09:30:00+08:00",
    }


@pytest.fixture
def sqlite_settings(tmp_path: Path) -> Settings:
    db_path = tmp_path / "session-state.db"
    return Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{db_path}",
        app_secret_key="test-secret-key",
        admin_user="admin",
        admin_password_salt=ADMIN_SALT,
        admin_password_hash=_password_hash(ADMIN_PASSWORD),
    )


@pytest.fixture
def client(sqlite_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from backend.app import services
    from backend.app import main as app_main
    from backend.app.db import get_connection, run_migrations
    from backend.app.models.domain import CONDITIONS, ERROR_TYPE_IDS, SUBCONDITIONS

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        conn.executemany(
            """
            INSERT INTO admin_assignment_units (
                participant_type,
                condition,
                subcondition,
                error_type_id,
                enabled
            ) VALUES ('long', ?, ?, ?, 0)
            ON CONFLICT(participant_type, condition, subcondition, error_type_id)
            DO UPDATE SET enabled = excluded.enabled
            """,
            [
                (condition, subcondition, error_type_id)
                for condition in CONDITIONS
                for subcondition in SUBCONDITIONS
                for error_type_id in ERROR_TYPE_IDS
            ],
        )
    finally:
        conn.close()

    monkeypatch.setattr(
        services.participants,
        "current_shanghai_date",
        lambda: TEST_DATE,
    )
    monkeypatch.setattr(
        services.questionnaires,
        "_timestamp_now",
        lambda: "2026-07-02T09:30:00+00:00",
    )

    try:
        from backend.app.services import participant_days
    except ImportError:
        participant_days = None

    if participant_days is not None:
        monkeypatch.setattr(
            participant_days,
            "current_shanghai_date",
            lambda: TEST_DATE,
        )

    async def fake_generate_chat(
        self: ProviderRouter,
        *,
        request_id: str,
        messages: Sequence[ProviderMessage],
        is_test: bool,
        allow_local_fallback: bool = True,
    ) -> ProviderResponse:
        del self, is_test
        error_generation = any(
            "[指令：激活错误 ->" in message.content for message in messages
        )
        assert allow_local_fallback is not error_generation
        return ProviderResponse(
            text=f"Session-state AI candidate for {request_id}.",
            provider="fake-provider",
            model="fake-model",
            route="chat",
            attempts=[],
            used_local_fallback=False,
        )

    monkeypatch.setattr(
        app_main,
        "get_asr_client",
        lambda _settings=sqlite_settings: FakeAsrClient(),
    )
    monkeypatch.setattr(ProviderRouter, "generate_chat", fake_generate_chat)

    return TestClient(app_main.create_app(settings=sqlite_settings))


def login_short_participant(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/api/auth/login",
        json={
            "name": "Session Participant",
            "phone": "19900000201",
            "participant_type": "short",
        },
    )
    assert response.status_code == 200
    return response.json()


def submit_pretest_final(client: TestClient) -> None:
    response = client.post(
        "/api/pretest/final",
        json=build_pretest_payload(),
    )
    assert response.status_code == 200


def formal_client_info(**overrides: object) -> dict[str, object]:
    payload = {
        "device_type": "desktop",
        "viewport_width": 1440,
        "is_secure_context": True,
        "browser_name": "Chrome",
        "browser_version": "126",
        "microphone_available": True,
        "microphone_permission": "granted",
    }
    payload.update(overrides)
    return payload


def login_short_participant_with_phone(client: TestClient, *, phone: str) -> dict[str, object]:
    response = client.post(
        "/api/auth/login",
        json={
            "name": "Session Participant",
            "phone": phone,
            "participant_type": "short",
        },
    )
    assert response.status_code == 200
    return response.json()


def admin_login(client: TestClient) -> TestClient:
    response = client.post(
        "/api/admin/login",
        json={"username": "admin", "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200
    return client


def start_test_session(client: TestClient, *, phone: str) -> tuple[dict[str, object], str]:
    participant = login_short_participant_with_phone(client, phone=phone)
    start_response = admin_login(client).post(
        "/api/test/sessions/start",
        json={
            "is_test": True,
            "client_info": formal_client_info(
                viewport_width=800,
                is_secure_context=False,
                browser_name="Firefox",
                browser_version="127",
                microphone_available=False,
                microphone_permission="unavailable",
            ),
        },
    )
    assert start_response.status_code == 200
    return participant, start_response.json()["session_id"]


def post_formal_asr(client: TestClient, *, session_id: str):
    return client.post(
        "/api/asr",
        data={"session_id": session_id},
        files={
            "audio": (
                "turn.webm",
                VALID_WEBM_AUDIO,
                "audio/webm",
            )
        },
    )


def submit_formal_voice_turn(client: TestClient, *, session_id: str):
    asr_response = post_formal_asr(client, session_id=session_id)
    assert asr_response.status_code == 200
    asr_payload = asr_response.json()
    return client.post(
        "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "voice",
                "asr_result_id": asr_payload["asr_result_id"],
            },
    )


def set_new_current_attempt(
    sqlite_settings: Settings,
    *,
    participant_id: int,
) -> int:
    from backend.app.db import get_connection
    from backend.app.repositories.attempts import create_attempt, set_current_attempt
    from backend.app.repositories.participants import create_participant_days

    conn = get_connection(sqlite_settings)
    try:
        next_attempt_id = create_attempt(
            conn,
            participant_id=participant_id,
            participant_type="short",
            condition="human",
            subcondition="qa",
            topic_key="advice",
            error_type_id="factual_minor",
            target_days=1,
        )
        set_current_attempt(
            conn,
            participant_id=participant_id,
            attempt_id=next_attempt_id,
        )
        create_participant_days(
            conn,
            participant_id=participant_id,
            target_days=1,
            start_date=date.fromisoformat(TEST_DATE),
            attempt_id=next_attempt_id,
        )
        conn.commit()
    finally:
        conn.close()

    return next_attempt_id


def assert_no_formal_hidden_fields(payload: dict[str, object]) -> None:
    hidden_fields = {
        "condition",
        "subcondition",
        "topic_key",
        "error_type_id",
        "planned_error_turn",
        "scenario_id",
        "task_kind",
        "client_info",
        "graph_trace",
        "provider_attempts",
        "evaluator_result",
        "manipulation_status",
        "semantic_evidence",
        "error_mutation_json",
        "error_attempts_json",
        "error_failure_reason",
    }
    assert hidden_fields.isdisjoint(payload)
    assert payload["presentation_mode"] in {"conversation", "execution"}
    assert payload["artifact_kind"] in {None, "schedule_table", "copy_editor"}
    assert payload["artifact_status"] in {
        "none",
        "awaiting_input",
        "completed",
        "failed",
    }
    assert_no_nested_formal_hidden_artifact_fields(payload)


def assert_no_formal_hidden_turn_fields(payload: dict[str, object]) -> None:
    hidden_fields = {
        "session_id",
        "error_planned",
        "error_presented",
        "error_presentation",
        "session_is_test",
        "graph_trace",
        "provider_attempts",
        "evaluator_result",
        "semantic_evidence",
        "error_mutation_json",
        "error_attempts_json",
        "error_failure_reason",
    }
    assert hidden_fields.isdisjoint(payload)
    assert_no_nested_formal_hidden_artifact_fields(payload)


def assert_no_nested_formal_hidden_artifact_fields(payload: object) -> None:
    hidden_artifact_keys = {
        "errorInjected",
        "error_injected",
        "errorTypeId",
        "error_type_id",
        "originalValue",
        "mutatedValue",
        "targetKind",
        "targetPath",
        "failureReason",
        "centrality",
        "operation",
        "magnitude",
        "semantic_evidence",
        "error_semantic_attempt_count",
        "mutatedField",
        "original",
        "mutated",
        "plannedErrorTurn",
        "planned_error_turn",
        "scenarioId",
        "scenario_id",
        "subcondition",
        "condition",
        "topicKey",
        "topic_key",
        "provider",
        "provider_name",
        "provider_model",
        "evaluator",
        "prompt",
        "system_prompt",
        "validation_error",
        "validation_reason",
    }
    if isinstance(payload, dict):
        assert hidden_artifact_keys.isdisjoint(payload)
        for value in payload.values():
            assert_no_nested_formal_hidden_artifact_fields(value)
    elif isinstance(payload, list):
        for value in payload:
            assert_no_nested_formal_hidden_artifact_fields(value)


def test_formal_session_requires_desktop_client_info(
    client: TestClient,
    sqlite_settings: Settings,
):
    with client:
        participant = login_short_participant(client)
        submit_pretest_final(client)

        cases = [
            ("mobile_device", formal_client_info(device_type="mobile")),
            ("tablet_device", formal_client_info(device_type="tablet")),
            ("small_viewport", formal_client_info(viewport_width=900)),
            ("insecure_context", formal_client_info(is_secure_context=False)),
            ("unsupported_browser", formal_client_info(browser_name="Safari")),
            (
                "missing_microphone",
                formal_client_info(
                    microphone_available=False,
                    microphone_permission="unavailable",
                ),
            ),
        ]

        for expected_code, client_info in cases:
            response = client.post(
                "/api/sessions/start",
                json={"is_test": False, "client_info": client_info},
            )
            assert response.status_code == 400
            assert response.json() == {"detail": expected_code}

    from backend.app.db import get_connection

    conn = get_connection(sqlite_settings)
    try:
        session_count = conn.execute(
            "SELECT COUNT(*) FROM experiment_sessions WHERE participant_id = ?",
            (participant["participant_id"],),
        ).fetchone()[0]
    finally:
        conn.close()

    assert session_count == 0


@pytest.mark.parametrize(
    ("subcondition", "topic_key"),
    [
        ("decision", "valueDecision"),
        ("execution", "taskExecution"),
    ],
)
def test_formal_session_and_turn_responses_redact_assignment_and_debug_metadata(
    client: TestClient,
    sqlite_settings: Settings,
    subcondition: str,
    topic_key: str,
):
    from backend.app.db import get_connection

    with client:
        participant = login_short_participant_with_phone(
            client,
            phone={
                "decision": "19900000202",
                "execution": "19900000203",
            }[subcondition],
        )
        conn = get_connection(sqlite_settings)
        try:
            current_attempt = conn.execute(
                """
                SELECT id
                FROM participant_attempts
                WHERE participant_id = ?
                ORDER BY attempt_no DESC
                LIMIT 1
                """,
                (participant["participant_id"],),
            ).fetchone()
            assert current_attempt is not None
            conn.execute(
                """
                UPDATE participant_attempts
                SET
                    condition = 'tool',
                    subcondition = ?,
                    topic_key = ?,
                    error_type_id = 'logic_minor',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (subcondition, topic_key, int(current_attempt["id"])),
            )
            conn.commit()
        finally:
            conn.close()

        submit_pretest_final(client)

        start_response = client.post(
            "/api/sessions/start",
            json={"is_test": False, "client_info": formal_client_info()},
        )
        assert start_response.status_code == 200
        start_payload = start_response.json()
        session_id = start_payload["session_id"]

        turn_response = submit_formal_voice_turn(client, session_id=session_id)
        assert turn_response.status_code == 200

        restore_response = client.get(f"/api/sessions/{session_id}")

    assert_no_formal_hidden_fields(start_payload)
    assert start_payload["topic_title"]
    assert start_payload["topic_description"]
    assert start_payload["presentation_mode"] == (
        "execution" if subcondition == "execution" else "conversation"
    )
    assert start_payload["artifact_kind"] == (
        "schedule_table" if subcondition == "execution" else None
    )
    assert start_payload["artifact_status"] == "none"
    assert set(start_payload) == {
        "session_id",
        "day_index",
        "status",
        "topic_title",
        "topic_description",
        "started_at",
        "completed_at",
        "is_test",
        "expected_turn_index",
        "presentation_mode",
        "artifact_kind",
        "artifact_status",
        "artifact_type",
        "artifact_payload",
        "turns",
    }

    turn_payload = turn_response.json()
    assert_no_formal_hidden_turn_fields(turn_payload)

    assert restore_response.status_code == 200
    restored_payload = restore_response.json()
    assert_no_formal_hidden_fields(restored_payload)
    assert restored_payload["turns"]
    assert_no_formal_hidden_turn_fields(restored_payload["turns"][0])


def test_formal_session_restore_rejects_cookie_for_non_current_attempt(
    client: TestClient,
    sqlite_settings: Settings,
):
    phone = "19900000204"
    with client:
        participant = login_short_participant_with_phone(
            client,
            phone=phone,
        )
        submit_pretest_final(client)

        start_response = client.post(
            "/api/sessions/start",
            json={"is_test": False, "client_info": formal_client_info()},
        )
        assert start_response.status_code == 200
        session_id = start_response.json()["session_id"]
        current_cookie = client.cookies.get(sqlite_settings.session_cookie_name)
        assert current_cookie is not None
        signed_payload = read_signed_session(
            current_cookie.strip('"'),
            sqlite_settings.app_secret_key,
        )
        assert signed_payload is not None
        phone_hash = str(signed_payload["phone_hash"])

    next_attempt_id = set_new_current_attempt(
        sqlite_settings,
        participant_id=int(participant["participant_id"]),
    )

    client.cookies.set(
        sqlite_settings.session_cookie_name,
        sign_session_payload(
            {
                "participant_id": int(participant["participant_id"]),
                "attempt_id": next_attempt_id,
                "phone_hash": phone_hash,
            },
            sqlite_settings.app_secret_key,
        ),
    )

    with client:
        restore_response = client.get(f"/api/sessions/{session_id}")

    assert next_attempt_id != participant["attempt_id"]
    assert restore_response.status_code == 401
    assert restore_response.json() == {"detail": "Invalid session."}


def test_formal_session_restore_rejects_old_attempt_cookie(
    client: TestClient,
    sqlite_settings: Settings,
):
    with client:
        participant = login_short_participant(client)
        submit_pretest_final(client)
        start_response = client.post(
            "/api/sessions/start",
            json={"is_test": False, "client_info": formal_client_info()},
        )
        assert start_response.status_code == 200
        session_id = start_response.json()["session_id"]
        current_cookie = client.cookies.get(sqlite_settings.session_cookie_name)
        assert current_cookie is not None
        signed_payload = read_signed_session(
            current_cookie.strip('"'),
            sqlite_settings.app_secret_key,
        )
        assert signed_payload is not None
        phone_hash = str(signed_payload["phone_hash"])

    bad_cookie = sign_session_payload(
        {
            "participant_id": participant["participant_id"],
            "attempt_id": participant["attempt_id"] + 999,
            "phone_hash": phone_hash,
        },
        sqlite_settings.app_secret_key,
    )
    client.cookies.delete(sqlite_settings.session_cookie_name)
    client.cookies.set(sqlite_settings.session_cookie_name, bad_cookie)

    with client:
        response = client.get(f"/api/sessions/{session_id}")

    assert response.status_code == 401


def test_session_route_access_rejects_current_cookie_for_historical_attempt_session(
    client: TestClient,
    sqlite_settings: Settings,
):
    from fastapi import HTTPException

    from backend.app.db import get_connection
    from backend.app.main import require_session_route_access

    with client:
        participant = login_short_participant_with_phone(
            client,
            phone="19900000205",
        )
        submit_pretest_final(client)

        start_response = client.post(
            "/api/sessions/start",
            json={"is_test": False, "client_info": formal_client_info()},
        )
        assert start_response.status_code == 200
        session_id = start_response.json()["session_id"]
        current_cookie = client.cookies.get(sqlite_settings.session_cookie_name)
        assert current_cookie is not None
        signed_payload = read_signed_session(
            current_cookie.strip('"'),
            sqlite_settings.app_secret_key,
        )
        assert signed_payload is not None
        phone_hash = str(signed_payload["phone_hash"])

    next_attempt_id = set_new_current_attempt(
        sqlite_settings,
        participant_id=int(participant["participant_id"]),
    )
    replacement_cookie = sign_session_payload(
        {
            "participant_id": int(participant["participant_id"]),
            "attempt_id": next_attempt_id,
            "phone_hash": phone_hash,
        },
        sqlite_settings.app_secret_key,
    )

    conn = get_connection(sqlite_settings)
    try:
        with pytest.raises(HTTPException, match="Invalid session."):
            require_session_route_access(
                conn,
                session_uuid=session_id,
                participant_session_token=replacement_cookie,
                admin_session_token=None,
                settings=sqlite_settings,
            )
    finally:
        conn.close()


def test_formal_public_artifact_payloads_redact_nested_error_metadata() -> None:
    from backend.app.main import public_session_view, public_turn_view
    from backend.app.models.api import ClientInfo, SessionView, TurnView

    artifact_payload = {
        "actionType": "schedule_table",
        "actionMode": "create",
        "status": "completed",
        "requestedSource": "用户安排",
        "columns": ["日期", "时间", "地点", "任务", "备注"],
        "rows": [
            {
                "date": "7月13日",
                "time": "09:00",
                "location": "会议室A",
                "task": "准备材料",
                "note": "带电脑",
                "errorInjected": {
                    "errorTypeId": "logic_minor",
                    "original": "09:00",
                    "mutated": "09:30",
                },
                "provider": "PRIVATE_PROVIDER_SENTINEL",
                "prompt": "PRIVATE_PROMPT_SENTINEL",
            }
        ],
        "planned_error_turn": 3,
        "evaluator": "PRIVATE_EVALUATOR_SENTINEL",
        "validation_reason": "PRIVATE_VALIDATION_SENTINEL",
        "providerModel": "PRIVATE_MODEL_SENTINEL",
    }
    turn = TurnView(
        turn_id=1,
        session_id="session-redacted",
        turn_index=1,
        user_text="请整理任务。",
        user_input_mode="voice",
        assistant_text="已整理。",
        error_planned=True,
        error_presented=True,
        error_presentation="simulated_ui",
        session_is_test=False,
        artifact_type="table",
        artifact_payload=artifact_payload,
        rating=None,
    )
    session = SessionView(
        session_id="session-redacted",
        day_index=1,
        status="started",
        condition="tool",
        subcondition="execution",
        topic_key="taskExecution",
        error_type_id="logic_minor",
        planned_error_turn=3,
        started_at="2026-07-02T10:00:00+08:00",
        is_test=False,
        client_info=ClientInfo(
            device_type="desktop",
            viewport_width=1440,
            is_secure_context=True,
            browser_name="Chrome",
            browser_version="126",
            microphone_available=True,
            microphone_permission="granted",
        ),
        presentation_mode="execution",
        artifact_kind="schedule_table",
        artifact_status="completed",
        turns=[turn],
    )

    public_turn = public_turn_view(turn).model_dump(mode="json")
    public_session = public_session_view(session).model_dump(mode="json")

    assert_no_nested_formal_hidden_artifact_fields(public_turn)
    assert_no_nested_formal_hidden_artifact_fields(public_session)
    serialized = json.dumps(
        {"turn": public_turn, "session": public_session},
        ensure_ascii=False,
    )
    assert "PRIVATE_" not in serialized
    assert public_turn["artifact_payload"]["rows"][0]["task"] == "准备材料"
    assert public_session["presentation_mode"] == "execution"
    assert public_session["artifact_kind"] == "schedule_table"
    assert public_session["artifact_status"] == "completed"


def test_formal_decision_attributes_remove_case_variant_internal_keys() -> None:
    from backend.app.main import public_session_view, public_turn_view
    from backend.app.models.api import ClientInfo, SessionView, TurnView

    sensitive_attributes = {
        "Provider": "PRIVATE_PROVIDER_SENTINEL",
        "Authorization": "PRIVATE_AUTH_SENTINEL",
        "targetKind": "PRIVATE_TARGET_SENTINEL",
        "TARGET_PATH": "PRIVATE_PATH_SENTINEL",
        "Centrality": "PRIVATE_CENTRALITY_SENTINEL",
        "operation": "PRIVATE_OPERATION_SENTINEL",
        "Magnitude": "PRIVATE_MAGNITUDE_SENTINEL",
        "semantic_evidence": "PRIVATE_SEMANTIC_SENTINEL",
    }
    artifact_payload = {
        "options": [
            {
                "id": "a",
                "label": "方案A",
                "attributes": {"成本": 10, **sensitive_attributes},
            },
            {"id": "b", "label": "方案B", "attributes": {"成本": 20}},
        ],
        "constraints": [{"name": "预算", "kind": "max", "value": 20}],
        "weights": [{"criterion": "成本", "weight": 1.0}],
        "recommendation": {"option_id": "a", "summary": "方案A成本更低。", "score": 1.0},
        "reasons": ["方案A满足预算。"],
    }
    turn = TurnView(
        turn_id=2,
        session_id="session-decision-redacted",
        turn_index=1,
        user_text="帮我选择。",
        user_input_mode="voice",
        assistant_text="建议方案A。",
        error_planned=False,
        error_presented=False,
        error_presentation="none",
        session_is_test=False,
        artifact_type="decision_matrix",
        artifact_payload=artifact_payload,
        rating=None,
    )
    session = SessionView(
        session_id="session-decision-redacted",
        day_index=1,
        status="started",
        condition="tool",
        subcondition="decision",
        topic_key="valueDecision",
        error_type_id="logic_minor",
        planned_error_turn=3,
        started_at="2026-07-02T10:00:00+08:00",
        is_test=False,
        client_info=ClientInfo(
            device_type="desktop",
            viewport_width=1440,
            is_secure_context=True,
            browser_name="Chrome",
            browser_version="126",
            microphone_available=True,
            microphone_permission="granted",
        ),
        presentation_mode="conversation",
        artifact_kind=None,
        artifact_status="none",
        artifact_type="decision_matrix",
        artifact_payload=artifact_payload,
        turns=[turn],
    )

    public_turn = public_turn_view(turn).model_dump(mode="json")
    public_session = public_session_view(session).model_dump(mode="json")
    serialized = json.dumps(
        {"turn": public_turn, "session": public_session},
        ensure_ascii=False,
    )

    assert "PRIVATE_" not in serialized
    assert public_turn["artifact_payload"]["options"][0]["attributes"] == {
        "成本": 10
    }
    assert public_session["artifact_payload"]["options"][0]["attributes"] == {
        "成本": 10
    }


@pytest.mark.parametrize("permission", ["denied", "prompt"])
def test_formal_session_rejects_ungranted_microphone_permission(
    client: TestClient,
    sqlite_settings: Settings,
    permission: str,
):
    with client:
        participant = login_short_participant_with_phone(
            client,
            phone={"denied": "19900000206", "prompt": "19900000207"}[permission],
        )
        submit_pretest_final(client)

        response = client.post(
            "/api/sessions/start",
            json={
                "is_test": False,
                "client_info": formal_client_info(microphone_permission=permission),
            },
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "microphone_permission_denied"}

    from backend.app.db import get_connection

    conn = get_connection(sqlite_settings)
    try:
        session_count = conn.execute(
            "SELECT COUNT(*) FROM experiment_sessions WHERE participant_id = ?",
            (participant["participant_id"],),
        ).fetchone()[0]
    finally:
        conn.close()

    assert session_count == 0


def test_formal_session_start_requires_day_one_pretest_final(client: TestClient):
    with client:
        login_short_participant(client)

        response = client.post(
            "/api/sessions/start",
            json={
                "is_test": False,
                "client_info": formal_client_info(),
            },
        )

    assert response.status_code == 409
    assert response.json() == {"detail": "Day 1 pretest final submission is required."}


def test_session_start_persists_planned_error_turn_and_restores_state(
    client: TestClient,
    sqlite_settings: Settings,
):
    with client:
        participant = login_short_participant(client)
        submit_pretest_final(client)

        start_response = client.post(
            "/api/sessions/start",
            json={
                "is_test": False,
                "client_info": formal_client_info(),
            },
        )
        assert start_response.status_code == 200

        session_payload = start_response.json()
        restore_response = client.get(
            f"/api/sessions/{session_payload['session_id']}"
        )

    assert restore_response.status_code == 200
    restored_payload = restore_response.json()
    assert restored_payload == session_payload
    assert restored_payload["status"] == "started"
    assert restored_payload["is_test"] is False
    assert restored_payload["day_index"] == 1
    assert_no_formal_hidden_fields(restored_payload)
    assert restored_payload["expected_turn_index"] == 1
    assert restored_payload["turns"] == []

    from backend.app.db import get_connection

    conn = get_connection(sqlite_settings)
    try:
        session_row = conn.execute(
            """
            SELECT participant_day_id, planned_error_turn, status, is_test, client_info_json
            FROM experiment_sessions
            WHERE participant_id = ?
            """,
            (participant["participant_id"],),
        ).fetchone()
        participant_day = conn.execute(
            """
            SELECT status
            FROM participant_days
            WHERE id = ?
            """,
            (session_row["participant_day_id"],),
        ).fetchone()
    finally:
        conn.close()

    assert session_row is not None
    assert session_row["planned_error_turn"] in {2, 3, 4}
    assert session_row["status"] == "started"
    assert session_row["is_test"] == 0
    assert "desktop" in session_row["client_info_json"]
    assert participant_day["status"] == "in_experiment"


def test_formal_session_resume_revalidates_environment_without_abandoning_attempt(
    client: TestClient,
    sqlite_settings: Settings,
):
    with client:
        participant = login_short_participant(client)
        submit_pretest_final(client)
        start_response = client.post(
            "/api/sessions/start",
            json={"is_test": False, "client_info": formal_client_info()},
        )
        rejected_resume = client.post(
            "/api/sessions/start",
            json={
                "is_test": False,
                "client_info": {
                    **formal_client_info(),
                    "microphone_permission": "denied",
                },
            },
        )
        restored_response = client.get(
            f"/api/sessions/{start_response.json()['session_id']}"
        )
        resumed_response = client.post(
            "/api/sessions/start",
            json={"is_test": False, "client_info": formal_client_info()},
        )

    assert start_response.status_code == 200
    assert rejected_resume.status_code == 400
    assert rejected_resume.json() == {"detail": "microphone_permission_denied"}
    assert restored_response.status_code == 200
    assert resumed_response.status_code == 200
    assert resumed_response.json() == restored_response.json() == start_response.json()

    from backend.app.db import get_connection

    conn = get_connection(sqlite_settings)
    try:
        attempt_row = conn.execute(
            """
            SELECT status, valid_for_export, blocked_reason
            FROM participant_attempts
            WHERE id = ?
            """,
            (participant["attempt_id"],),
        ).fetchone()
        session_count = conn.execute(
            "SELECT COUNT(*) FROM experiment_sessions WHERE attempt_id = ?",
            (participant["attempt_id"],),
        ).fetchone()[0]
    finally:
        conn.close()

    assert attempt_row["status"] == "active"
    assert attempt_row["valid_for_export"] == 1
    assert attempt_row["blocked_reason"] is None
    assert session_count == 1


def test_test_session_start_persists_requested_scenario_overrides(
    client: TestClient,
    sqlite_settings: Settings,
):
    requested = {
        "condition": "tool",
        "subcondition": "decision",
        "topic_key": "valueDecision",
        "error_type_id": "logic_minor",
        "planned_error_turn": 5,
    }

    with client:
        login_short_participant_with_phone(
            client,
            phone="19900000208",
        )
        start_response = admin_login(client).post(
            "/api/test/sessions/start",
            json={
                "is_test": True,
                "client_info": formal_client_info(
                    viewport_width=800,
                    is_secure_context=False,
                    browser_name="Firefox",
                    browser_version="127",
                    microphone_available=False,
                    microphone_permission="unavailable",
                ),
                **requested,
            },
        )
        assert start_response.status_code == 200
        session_payload = start_response.json()

        restore_response = client.get(f"/api/sessions/{session_payload['session_id']}")

    assert restore_response.status_code == 200
    restored_payload = restore_response.json()
    assert restored_payload["is_test"] is True
    for field_name, expected_value in requested.items():
        assert session_payload[field_name] == expected_value
        assert restored_payload[field_name] == expected_value

    from backend.app.db import get_connection

    conn = get_connection(sqlite_settings)
    try:
        session_row = conn.execute(
            """
            SELECT condition, subcondition, topic_key, error_type_id, planned_error_turn, is_test
            FROM experiment_sessions
            WHERE session_uuid = ?
            """,
            (session_payload["session_id"],),
        ).fetchone()
    finally:
        conn.close()

    assert session_row is not None
    assert session_row["is_test"] == 1
    assert session_row["condition"] == requested["condition"]
    assert session_row["subcondition"] == requested["subcondition"]
    assert session_row["topic_key"] == requested["topic_key"]
    assert session_row["error_type_id"] == requested["error_type_id"]
    assert session_row["planned_error_turn"] == requested["planned_error_turn"]


def test_test_session_start_rejects_topic_outside_selected_cell(
    client: TestClient,
):
    with client:
        login_short_participant_with_phone(
            client,
            phone="19900000209",
        )
        response = admin_login(client).post(
            "/api/test/sessions/start",
            json={
                "is_test": True,
                "client_info": formal_client_info(
                    viewport_width=800,
                    is_secure_context=False,
                    browser_name="Firefox",
                    browser_version="127",
                    microphone_available=False,
                    microphone_permission="unavailable",
                ),
                "condition": "tool",
                "subcondition": "execution",
                "topic_key": "advice",
                "error_type_id": "logic_minor",
                "planned_error_turn": 3,
            },
        )

    assert response.status_code == 400
    assert "topic_key" in response.json()["detail"]


def test_test_session_can_start_multiple_scenarios_for_same_participant_day(
    client: TestClient,
):
    first_requested = {
        "condition": "human",
        "subcondition": "qa",
        "topic_key": "advice",
        "error_type_id": "factual_minor",
        "planned_error_turn": 2,
    }
    second_requested = {
        "condition": "tool",
        "subcondition": "execution",
        "topic_key": "taskExecution",
        "error_type_id": "logic_major",
        "planned_error_turn": 4,
    }

    with client:
        login_short_participant_with_phone(
            client,
            phone="19900000210",
        )
        admin_client = admin_login(client)
        first_response = admin_client.post(
            "/api/test/sessions/start",
            json={
                "is_test": True,
                "client_info": formal_client_info(
                    viewport_width=800,
                    is_secure_context=False,
                    browser_name="Firefox",
                    browser_version="127",
                    microphone_available=False,
                    microphone_permission="unavailable",
                ),
                **first_requested,
            },
        )
        second_response = admin_client.post(
            "/api/test/sessions/start",
            json={
                "is_test": True,
                "client_info": formal_client_info(
                    viewport_width=800,
                    is_secure_context=False,
                    browser_name="Firefox",
                    browser_version="127",
                    microphone_available=False,
                    microphone_permission="unavailable",
                ),
                **second_requested,
            },
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    first_payload = first_response.json()
    second_payload = second_response.json()
    assert first_payload["session_id"] != second_payload["session_id"]
    assert first_payload["subcondition"] == first_requested["subcondition"]
    assert second_payload["subcondition"] == second_requested["subcondition"]
    assert first_payload["is_test"] is True
    assert second_payload["is_test"] is True


def test_formal_session_start_uses_current_attempt_assignment_and_ignores_overrides(
    client: TestClient,
    sqlite_settings: Settings,
):
    current_attempt_assignment = {
        "condition": "tool",
        "subcondition": "execution",
        "topic_key": "taskExecution",
        "error_type_id": "logic_major",
    }
    legacy_participant_assignment = {
        "condition": "human",
        "subcondition": "qa",
        "topic_key": "advice",
        "error_type_id": "factual_minor",
    }
    requested = {
        "condition": "human",
        "subcondition": "planning",
        "topic_key": "debug_should_not_apply",
        "error_type_id": "social_major",
        "planned_error_turn": 5,
    }

    with client:
        participant = login_short_participant_with_phone(
            client,
            phone="19900000211",
        )
        submit_pretest_final(client)

    from backend.app.db import get_connection

    conn = get_connection(sqlite_settings)
    try:
        conn.execute(
            """
            UPDATE participant_attempts
            SET
                condition = ?,
                subcondition = ?,
                topic_key = ?,
                error_type_id = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                current_attempt_assignment["condition"],
                current_attempt_assignment["subcondition"],
                current_attempt_assignment["topic_key"],
                current_attempt_assignment["error_type_id"],
                participant["attempt_id"],
            ),
        )
        conn.execute(
            """
            UPDATE participants
            SET
                condition = ?,
                subcondition = ?,
                topic_key = ?,
                error_type_id = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                legacy_participant_assignment["condition"],
                legacy_participant_assignment["subcondition"],
                legacy_participant_assignment["topic_key"],
                legacy_participant_assignment["error_type_id"],
                participant["participant_id"],
            ),
        )
        conn.commit()
    finally:
        conn.close()

    with client:
        start_response = client.post(
            "/api/sessions/start",
            json={
                "is_test": False,
                "client_info": formal_client_info(),
                **requested,
            },
        )

    assert start_response.status_code == 200
    session_payload = start_response.json()
    assert session_payload["is_test"] is False
    assert_no_formal_hidden_fields(session_payload)

    conn = get_connection(sqlite_settings)
    try:
        session_row = conn.execute(
            """
            SELECT attempt_id, condition, subcondition, topic_key, error_type_id, planned_error_turn
            FROM experiment_sessions
            WHERE participant_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (participant["participant_id"],),
        ).fetchone()
    finally:
        conn.close()

    assert session_row["attempt_id"] == participant["attempt_id"]
    assert session_row["condition"] == current_attempt_assignment["condition"]
    assert session_row["subcondition"] == current_attempt_assignment["subcondition"]
    assert session_row["topic_key"] == current_attempt_assignment["topic_key"]
    assert session_row["error_type_id"] == current_attempt_assignment["error_type_id"]
    assert session_row["planned_error_turn"] in {2, 3, 4}
    assert session_row["planned_error_turn"] != requested["planned_error_turn"]


def test_test_session_start_and_completion_do_not_mutate_formal_participant_day_status(
    client: TestClient,
    sqlite_settings: Settings,
):
    with client:
        participant, session_id = start_test_session(
            client,
            phone="19900000212",
        )

        final_rating_payload = None
        for turn_number in range(1, 6):
            turn_response = client.post(
                "/api/turns",
                json={
                    "session_id": session_id,
                    "input_mode": "text_test_only",
                    "user_text": f"test transcript {turn_number}",
                },
            )
            assert turn_response.status_code == 200
            turn_id = turn_response.json()["turn_id"]

            rating_response = client.post(
                f"/api/turns/{turn_id}/rating",
                json={
                    "stance_score": 3,
                    "trust_score": 5,
                    "client_elapsed_ms": 1200,
                },
            )
            assert rating_response.status_code == 200
            if turn_number == 5:
                final_rating_payload = rating_response.json()

        complete_response = client.post(f"/api/sessions/{session_id}/complete")

    assert complete_response.status_code == 200
    assert complete_response.json()["status"] == "completed"
    assert final_rating_payload == complete_response.json()

    from backend.app.db import get_connection

    conn = get_connection(sqlite_settings)
    try:
        day_row = conn.execute(
            """
            SELECT status, started_at, completed_at
            FROM participant_days
            WHERE participant_id = ?
            ORDER BY day_index
            LIMIT 1
            """,
            (participant["participant_id"],),
        ).fetchone()
    finally:
        conn.close()

    assert day_row["status"] == "not_started"
    assert day_row["started_at"] is None
    assert day_row["completed_at"] is None


def test_fifth_rating_completes_session_and_lost_response_retry_is_idempotent(
    client: TestClient,
    sqlite_settings: Settings,
):
    with client:
        login_short_participant(client)
        submit_pretest_final(client)

        start_response = client.post(
            "/api/sessions/start",
            json={
                "is_test": False,
                "client_info": formal_client_info(),
            },
        )
        assert start_response.status_code == 200
        session_id = start_response.json()["session_id"]

        fifth_rating_payload = None
        for turn_number in range(1, 6):
            turn_response = submit_formal_voice_turn(client, session_id=session_id)
            assert turn_response.status_code == 200
            turn_payload = turn_response.json()
            assert turn_payload["turn_index"] == turn_number

            rating_response = client.post(
                f"/api/turns/{turn_payload['turn_id']}/rating",
                json={
                    "stance_score": 3,
                    "trust_score": 5,
                    "client_elapsed_ms": 1200,
                },
            )
            assert rating_response.status_code == 200
            if turn_number == 5:
                fifth_rating_payload = rating_response.json()

        sixth_turn_response = post_formal_asr(client, session_id=session_id)
        complete_response = client.post(f"/api/sessions/{session_id}/complete")
        restored_response = client.get(f"/api/sessions/{session_id}")

    assert sixth_turn_response.status_code == 409
    assert sixth_turn_response.json() == {
        "detail": "Session is not active: completed."
    }
    assert complete_response.status_code == 200
    assert fifth_rating_payload == complete_response.json()
    assert complete_response.json()["status"] == "completed"
    assert complete_response.json()["expected_turn_index"] is None
    assert restored_response.status_code == 200
    assert restored_response.json()["status"] == "completed"
    assert len(restored_response.json()["turns"]) == 5
    assert all(turn["rating"] is not None for turn in restored_response.json()["turns"])

    from backend.app.db import get_connection

    conn = get_connection(sqlite_settings)
    try:
        session_row = conn.execute(
            """
            SELECT status, completed_at
            FROM experiment_sessions
            WHERE session_uuid = ?
            """,
            (session_id,),
        ).fetchone()
        day_row = conn.execute(
            """
            SELECT status, completed_at
            FROM participant_days
            WHERE participant_id = (
                SELECT participant_id
                FROM experiment_sessions
                WHERE session_uuid = ?
            )
            ORDER BY day_index
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
    finally:
        conn.close()

    assert session_row["status"] == "completed"
    assert session_row["completed_at"] is not None
    assert day_row["status"] == "completed"
    assert day_row["completed_at"] is not None


def test_short_formal_completion_marks_current_attempt_completed(
    client: TestClient,
    sqlite_settings: Settings,
):
    with client:
        participant = login_short_participant_with_phone(
            client,
            phone="19900000213",
        )
        submit_pretest_final(client)

        start_response = client.post(
            "/api/sessions/start",
            json={
                "is_test": False,
                "client_info": formal_client_info(),
            },
        )
        assert start_response.status_code == 200
        session_id = start_response.json()["session_id"]

        fifth_rating_payload = None
        for turn_number in range(1, 6):
            turn_response = submit_formal_voice_turn(client, session_id=session_id)
            assert turn_response.status_code == 200
            turn_id = turn_response.json()["turn_id"]
            rating_response = client.post(
                f"/api/turns/{turn_id}/rating",
                json={
                    "stance_score": 3,
                    "trust_score": 5,
                    "client_elapsed_ms": 1200,
                },
            )
            assert rating_response.status_code == 200
            if turn_number == 5:
                fifth_rating_payload = rating_response.json()

        me_response = client.get("/api/me")

    assert fifth_rating_payload is not None
    assert fifth_rating_payload["status"] == "completed"
    assert_no_formal_hidden_fields(fifth_rating_payload)
    assert me_response.status_code == 200
    assert me_response.json()["current_status"] == "completed"
    assert me_response.json()["participation_state"] == "completed"

    from backend.app.db import get_connection

    conn = get_connection(sqlite_settings)
    try:
        attempt_row = conn.execute(
            """
            SELECT status, valid_for_export
            FROM participant_attempts
            WHERE id = ?
            """,
            (participant["attempt_id"],),
        ).fetchone()
    finally:
        conn.close()

    assert attempt_row["status"] == "completed"
    assert attempt_row["valid_for_export"] == 1


def test_formal_completion_rejects_cookie_for_non_current_attempt(
    client: TestClient,
    sqlite_settings: Settings,
):
    phone = "19900000214"
    with client:
        participant = login_short_participant_with_phone(
            client,
            phone=phone,
        )
        submit_pretest_final(client)

        start_response = client.post(
            "/api/sessions/start",
            json={
                "is_test": False,
                "client_info": formal_client_info(),
            },
        )
        assert start_response.status_code == 200
        session_id = start_response.json()["session_id"]

        for _turn_number in range(1, 6):
            turn_response = submit_formal_voice_turn(client, session_id=session_id)
            assert turn_response.status_code == 200
            turn_id = turn_response.json()["turn_id"]
            rating_response = client.post(
                f"/api/turns/{turn_id}/rating",
                json={
                    "stance_score": 3,
                    "trust_score": 5,
                    "client_elapsed_ms": 1200,
                },
            )
            assert rating_response.status_code == 200
        current_cookie = client.cookies.get(sqlite_settings.session_cookie_name)
        assert current_cookie is not None
        signed_payload = read_signed_session(
            current_cookie.strip('"'),
            sqlite_settings.app_secret_key,
        )
        assert signed_payload is not None
        phone_hash = str(signed_payload["phone_hash"])

    second_attempt_id = set_new_current_attempt(
        sqlite_settings,
        participant_id=int(participant["participant_id"]),
    )

    client.cookies.set(
        sqlite_settings.session_cookie_name,
        sign_session_payload(
            {
                "participant_id": int(participant["participant_id"]),
                "attempt_id": second_attempt_id,
                "phone_hash": phone_hash,
            },
            sqlite_settings.app_secret_key,
        ),
    )

    with client:
        complete_response = client.post(f"/api/sessions/{session_id}/complete")

    assert complete_response.status_code == 401
    assert complete_response.json() == {"detail": "Invalid session."}

    from backend.app.db import get_connection

    conn = get_connection(sqlite_settings)
    try:
        attempt_rows = conn.execute(
            """
            SELECT id, status
            FROM participant_attempts
            WHERE id IN (?, ?)
            ORDER BY id
            """,
            (participant["attempt_id"], second_attempt_id),
        ).fetchall()
        session_row = conn.execute(
            """
            SELECT attempt_id, status
            FROM experiment_sessions
            WHERE session_uuid = ?
            """,
            (session_id,),
        ).fetchone()
    finally:
        conn.close()

    statuses_by_attempt_id = {
        int(row["id"]): str(row["status"])
        for row in attempt_rows
    }
    assert session_row["attempt_id"] == participant["attempt_id"]
    assert session_row["status"] == "completed"
    assert statuses_by_attempt_id[participant["attempt_id"]] == "completed"
    assert statuses_by_attempt_id[second_attempt_id] == "active"


def test_restore_hides_expected_turn_index_for_interrupted_session(
    client: TestClient,
    sqlite_settings: Settings,
):
    with client:
        participant = login_short_participant_with_phone(
            client,
            phone="19900000215",
        )
        submit_pretest_final(client)

        start_response = client.post(
            "/api/sessions/start",
            json={
                "is_test": False,
                "client_info": formal_client_info(),
            },
        )
        assert start_response.status_code == 200
        session_id = start_response.json()["session_id"]

    from backend.app.db import get_connection

    conn = get_connection(sqlite_settings)
    try:
        conn.execute(
            """
            UPDATE experiment_sessions
            SET status = ?, completed_at = ?
            WHERE participant_id = ? AND session_uuid = ?
            """,
            ("interrupted", "2026-07-02T10:30:00+00:00", participant["participant_id"], session_id),
        )
        conn.commit()
    finally:
        conn.close()

    with client:
        restore_response = client.get(f"/api/sessions/{session_id}")

    assert restore_response.status_code == 200
    payload = restore_response.json()
    assert payload["status"] == "interrupted"
    assert payload["expected_turn_index"] is None
