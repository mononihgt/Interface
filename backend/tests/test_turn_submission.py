from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import asyncio
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from backend.tests.audio_fixtures import VALID_WEBM_AUDIO
from backend.app.db import get_connection
from backend.app.security import sign_session_payload
from backend.app.repositories.health import HealthRepository
from backend.app.services.providers import ProviderAttempt, ProviderResponse
from backend.app.settings import Settings


TEST_DATE = "2026-07-02"
ADMIN_PASSWORD = "admin-pass-123"
ADMIN_SALT = "task7-turn-salt"


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
            latency_ms=50,
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
    db_path = tmp_path / "turn-submission.db"
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

    monkeypatch.setattr(
        app_main,
        "get_asr_client",
        lambda _settings=sqlite_settings: FakeAsrClient(),
    )

    return TestClient(app_main.create_app(settings=sqlite_settings))


def login_and_prepare_formal_session(
    client: TestClient,
    *,
    name: str = "Formal Turn Participant",
    phone: str = "19900000003",
) -> str:
    login_response = client.post(
        "/api/auth/login",
        json={
            "name": name,
            "phone": phone,
            "participant_type": "short",
        },
    )
    assert login_response.status_code == 200

    pretest_response = client.post(
        "/api/pretest/final",
        json=build_pretest_payload(),
    )
    assert pretest_response.status_code == 200

    start_response = client.post(
        "/api/sessions/start",
        json={
            "is_test": False,
            "client_info": {
                "device_type": "desktop",
                "viewport_width": 1440,
                "is_secure_context": True,
                "browser_name": "Chrome",
                "browser_version": "126",
                "microphone_available": True,
                "microphone_permission": "granted",
            },
        },
    )
    assert start_response.status_code == 200
    return start_response.json()["session_id"]


def _admin_login(client: TestClient) -> TestClient:
    response = client.post(
        "/api/admin/login",
        json={"username": "admin", "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200
    return client


def start_admin_test_text_session(
    client: TestClient,
    *,
    planned_error_turn: int = 2,
    condition: str = "human",
    subcondition: str = "qa",
    topic_key: str = "advice",
) -> str:
    response = _admin_login(client).post(
        "/api/test/sessions/start",
        json={
            "is_test": True,
            "condition": condition,
            "subcondition": subcondition,
            "topic_key": topic_key,
            "error_type_id": "factual_minor",
            "planned_error_turn": planned_error_turn,
            "client_info": {
                "device_type": "desktop",
                "viewport_width": 800,
                "is_secure_context": False,
                "browser_name": "Firefox",
                "browser_version": "127",
                "microphone_available": False,
                "microphone_permission": "unavailable",
            },
        },
    )
    assert response.status_code == 200
    return response.json()["session_id"]


def build_weather_snapshot():
    from backend.app.services.weather import (
        WeatherCurrent,
        WeatherDaily,
        WeatherLocation,
        WeatherSnapshot,
    )

    return WeatherSnapshot(
        query="杭州",
        fetched_at=datetime(2026, 7, 12, 11, 2, tzinfo=timezone.utc),
        location=WeatherLocation(
            name="杭州",
            admin1="浙江",
            admin2="杭州市",
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
                weather_code=[81, 80, 3, 2, 1, 61, 63][offset],
                temperature_max_c=[30.3, 31, 32, 33, 34, 31, 29][offset],
                temperature_min_c=[25.4, 25, 25.5, 26, 26.5, 24, 23][offset],
                precipitation_probability_percent=[100, 70, 20, 10, 10, 80, 90][
                    offset
                ],
                wind_speed_max_mps=[9.84, 8, 6, 5, 4, 7, 8.5][offset],
            )
            for offset in range(7)
        ],
    )


def _get_internal_test_participant_id(settings: Settings) -> int:
    conn = get_connection(settings)
    try:
        row = conn.execute(
            "SELECT id FROM participants WHERE phone_hash = ?",
            ("test-channel",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return int(row["id"])


def _set_internal_test_participant_cookie(client: TestClient, settings: Settings) -> None:
    client.cookies.set(
        "aitrust_v2_sid",
        sign_session_payload(
            {
                "participant_id": _get_internal_test_participant_id(settings),
                "phone_hash": "test-channel",
            },
            settings.app_secret_key,
        ),
    )


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
    turn_response = client.post(
        "/api/turns",
        json={
            "session_id": session_id,
            "input_mode": "voice",
            "asr_result_id": asr_payload["asr_result_id"],
        },
    )
    return turn_response


def test_formal_asr_response_exposes_only_participant_safe_result_reference(
    client: TestClient,
) -> None:
    with client:
        session_id = login_and_prepare_formal_session(client)
        response = post_formal_asr(client, session_id=session_id)

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {
        "asr_result_id",
        "asr_status",
        "asr_text",
        "retry_count",
        "max_retry_per_turn",
    }
    assert isinstance(payload["asr_result_id"], str)
    assert len(payload["asr_result_id"]) >= 32
    assert payload["asr_status"] == "success"


def test_formal_turn_rejects_missing_or_tampered_asr_result_reference(
    client: TestClient,
) -> None:
    with client:
        session_id = login_and_prepare_formal_session(client)
        missing = client.post(
            "/api/turns",
            json={"session_id": session_id, "input_mode": "voice"},
        )
        tampered = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "voice",
                "asr_result_id": "tampered-result-reference-000000000000",
            },
        )

    assert missing.status_code == 400
    assert tampered.status_code == 400


def test_formal_turn_rejects_foreign_session_asr_result_reference(
    client: TestClient,
) -> None:
    with client:
        first_session_id = login_and_prepare_formal_session(
            client,
            name="First ASR Owner",
            phone="13800138111",
        )
        first_asr = post_formal_asr(client, session_id=first_session_id).json()
        second_session_id = login_and_prepare_formal_session(
            client,
            name="Second ASR Owner",
            phone="13800138112",
        )
        response = client.post(
            "/api/turns",
            json={
                "session_id": second_session_id,
                "input_mode": "voice",
                "asr_result_id": first_asr["asr_result_id"],
            },
        )

    assert response.status_code == 400


def test_formal_turn_rejects_asr_result_replayed_for_later_turn(
    client: TestClient,
) -> None:
    with client:
        session_id = login_and_prepare_formal_session(client)
        asr_payload = post_formal_asr(client, session_id=session_id).json()
        first_turn = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "voice",
                "asr_result_id": asr_payload["asr_result_id"],
            },
        )
        assert first_turn.status_code == 200
        rating = client.post(
            f"/api/turns/{first_turn.json()['turn_id']}/rating",
            json={"stance_score": 3, "trust_score": 5},
        )
        assert rating.status_code == 200
        replay = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "turn_index": 2,
                "input_mode": "voice",
                "asr_result_id": asr_payload["asr_result_id"],
            },
        )

    assert replay.status_code == 400


def test_formal_text_turn_rejected_by_backend(client: TestClient):
    with client:
        session_id = login_and_prepare_formal_session(client)

        response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "typed fallback",
            },
        )

    assert response.status_code == 400
    assert response.json() == {
        "detail": "Formal sessions require voice input."
    }


def test_formal_session_rejects_text_input(client: TestClient):
    with client:
        session_id = login_and_prepare_formal_session(client)

        response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "测试文本",
                "asr_status": "not_used",
            },
        )

    assert response.status_code == 400
    assert "Formal sessions require voice input" in response.json()["detail"]


def test_test_channel_text_turn_allowed_and_marked_test(
    client: TestClient,
    sqlite_settings: Settings,
):
    with client:
        session_id = start_admin_test_text_session(client)

        turn_response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "typed test prompt",
            },
        )
        restore_response = client.get(f"/api/sessions/{session_id}")

    assert turn_response.status_code == 200
    turn_payload = turn_response.json()
    assert turn_payload["turn_index"] == 1
    assert turn_payload["user_input_mode"] == "text_test_only"
    assert turn_payload["session_is_test"] is True
    assert restore_response.status_code == 200
    assert restore_response.json()["is_test"] is True
    assert restore_response.json()["turns"][0]["user_input_mode"] == "text_test_only"

    from backend.app.db import get_connection

    conn = get_connection(sqlite_settings)
    try:
        row = conn.execute(
            """
            SELECT s.is_test, t.user_input_mode
            FROM experiment_sessions s
            JOIN conversation_turns t ON t.session_id = s.id
            WHERE s.session_uuid = ?
            """,
            (session_id,),
        ).fetchone()
    finally:
        conn.close()

    assert row["is_test"] == 1
    assert row["user_input_mode"] == "text_test_only"


def test_admin_test_channel_text_turn_allowed_and_marked_test(
    client: TestClient,
    sqlite_settings: Settings,
):
    with client:
        session_id = start_admin_test_text_session(client)

        turn_response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "typed admin test prompt",
                "asr_status": "not_used",
            },
        )
        restore_response = client.get(f"/api/sessions/{session_id}")

    assert turn_response.status_code == 200
    assert restore_response.status_code == 200

    from backend.app.db import get_connection

    conn = get_connection(sqlite_settings)
    try:
        row = conn.execute(
            """
            SELECT s.is_test, t.user_input_mode, p.phone_hash
            FROM experiment_sessions s
            JOIN conversation_turns t ON t.session_id = s.id
            JOIN participants p ON p.id = s.participant_id
            WHERE s.session_uuid = ?
            """,
            (session_id,),
        ).fetchone()
    finally:
        conn.close()

    assert row["is_test"] == 1
    assert row["user_input_mode"] == "text_test_only"
    assert row["phone_hash"] == "test-channel"


def test_admin_test_channel_asr_voice_turn_records_test_scope_without_formal_side_effects(
    client: TestClient,
    sqlite_settings: Settings,
):
    with client:
        conn = get_connection(sqlite_settings)
        try:
            formal_counts_before = tuple(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM {table_name} scoped
                    JOIN participants p ON p.id = scoped.participant_id
                    WHERE p.phone_hash != 'test-channel'
                    """
                ).fetchone()[0]
                for table_name in ("participant_attempts", "participant_days")
            )
        finally:
            conn.close()

        session_id = start_admin_test_text_session(client)
        asr_response = client.post(
            "/api/asr",
            data={
                "session_id": session_id,
                "turn_index": "1",
                "operation_id": "admin-test-asr-0001",
            },
            files={"audio": ("turn.webm", VALID_WEBM_AUDIO, "audio/webm")},
        )
        assert asr_response.status_code == 200
        asr_result_id = asr_response.json()["asr_result_id"]

        turn_response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "turn_index": 1,
                "operation_id": "admin-test-voice-turn-0001",
                "input_mode": "voice",
                "asr_result_id": asr_result_id,
            },
        )

    assert isinstance(asr_result_id, str)
    assert len(asr_result_id) >= 32
    assert turn_response.status_code == 200
    assert turn_response.json()["user_input_mode"] == "voice"
    assert turn_response.json()["session_is_test"] is True

    conn = get_connection(sqlite_settings)
    try:
        asr_row = conn.execute(
            """
            SELECT s.id AS session_id, s.is_test, a.turn_index, a.asr_status
            FROM asr_attempts a
            JOIN experiment_sessions s ON s.id = a.session_id
            WHERE s.session_uuid = ? AND a.result_ref = ?
            """,
            (session_id, asr_result_id),
        ).fetchone()
        turn_row = conn.execute(
            """
            SELECT s.id AS session_id, s.is_test, t.turn_index, t.user_input_mode
            FROM conversation_turns t
            JOIN experiment_sessions s ON s.id = t.session_id
            WHERE s.session_uuid = ?
            """,
            (session_id,),
        ).fetchone()
        provider_log_rows = conn.execute(
            """
            SELECT l.route, l.session_id, l.turn_index, l.is_test
            FROM api_call_logs l
            JOIN experiment_sessions s ON s.id = l.session_id
            WHERE s.session_uuid = ? AND l.route IN ('asr', 'chat')
            ORDER BY l.id
            """,
            (session_id,),
        ).fetchall()
        formal_counts_after = tuple(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM {table_name} scoped
                JOIN participants p ON p.id = scoped.participant_id
                WHERE p.phone_hash != 'test-channel'
                """
            ).fetchone()[0]
            for table_name in ("participant_attempts", "participant_days")
        )
    finally:
        conn.close()

    assert dict(asr_row) == {
        "session_id": turn_row["session_id"],
        "is_test": 1,
        "turn_index": 1,
        "asr_status": "success",
    }
    assert dict(turn_row) == {
        "session_id": asr_row["session_id"],
        "is_test": 1,
        "turn_index": 1,
        "user_input_mode": "voice",
    }
    assert {row["route"] for row in provider_log_rows} == {"asr", "chat"}
    assert all(
        row["session_id"] == asr_row["session_id"]
        and row["turn_index"] == 1
        and row["is_test"] == 1
        for row in provider_log_rows
    )
    assert formal_counts_after == formal_counts_before


def test_turn_submission_builds_provider_messages_from_persisted_history(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.agents.error_protocol import build_generation_messages
    from backend.app.scenarios.registry import ScenarioRegistry
    from backend.app.services.providers import ProviderMessage
    from backend.app.services.sessions import ProviderRouter

    observed_messages: list[list[ProviderMessage]] = []

    async def _capture_generation(
        self: ProviderRouter,
        *,
        request_id: str,
        messages: list[ProviderMessage],
        is_test: bool,
    ) -> ProviderResponse:
        observed_messages.append(list(messages))
        reply_index = len(observed_messages)
        return ProviderResponse(
            text=f"reply {reply_index}",
            provider="yi-zhan",
            model="gpt-5.1",
            route="chat",
            attempts=[],
        )

    monkeypatch.setattr(ProviderRouter, "generate_chat", _capture_generation)

    with client:
        session_id = start_admin_test_text_session(
            client,
            planned_error_turn=5,
        )
        first_turn = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "turn one",
            },
        )
        assert first_turn.status_code == 200
        rating = client.post(
            f"/api/turns/{first_turn.json()['turn_id']}/rating",
            json={
                "stance_score": 3,
                "trust_score": 5,
                "client_elapsed_ms": 1200,
            },
        )
        assert rating.status_code == 200
        second_turn = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "turn two",
            },
        )

    assert second_turn.status_code == 200
    scenario = ScenarioRegistry.load_default().require(
        condition="human",
        subcondition="qa",
        topic_key="advice",
    )
    assert observed_messages == [
        build_generation_messages(
            base_messages=[
                ProviderMessage(role="system", content=scenario.provider_system_prompt),
                ProviderMessage(role="user", content="turn one"),
            ],
            behavior_id="normal",
        ),
        build_generation_messages(
            base_messages=[
                ProviderMessage(role="system", content=scenario.provider_system_prompt),
                ProviderMessage(role="user", content="turn one"),
                ProviderMessage(role="assistant", content="reply 1"),
                ProviderMessage(role="user", content="turn two"),
            ],
            behavior_id="normal",
        ),
    ]


def test_test_session_followup_routes_require_admin_auth(
    client: TestClient,
):
    with client:
        session_id = start_admin_test_text_session(client)
        client.cookies.delete("aitrust_v2_admin_sid")

        read_response = client.get(f"/api/sessions/{session_id}")
        turn_response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "typed test prompt",
            },
        )
        asr_response = client.post(
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

    assert read_response.status_code == 401
    assert read_response.json() == {"detail": "Admin login required."}
    assert turn_response.status_code == 401
    assert turn_response.json() == {"detail": "Admin login required."}
    assert asr_response.status_code == 401
    assert asr_response.json() == {"detail": "Admin login required."}


def test_admin_authenticated_test_rating_and_complete_work(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.services.sessions import ProviderRouter

    strict_request_ids: list[str] = []

    async def generate_chat(
        self,
        *,
        request_id,
        messages,
        is_test,
        allow_local_fallback=True,
    ):
        del self
        assert is_test is True
        if any("[指令：激活错误 ->" in message.content for message in messages):
            assert allow_local_fallback is False
            strict_request_ids.append(request_id)
        return ProviderResponse(
            text=f"test reply for {request_id}",
            provider="fake-provider",
            model="fake-model",
            route="chat",
            attempts=[],
        )

    monkeypatch.setattr(ProviderRouter, "generate_chat", generate_chat)
    with client:
        session_id = start_admin_test_text_session(client)

        turn_ids: list[int] = []
        for turn_number in range(1, 6):
            turn_response = client.post(
                "/api/turns",
                json={
                    "session_id": session_id,
                    "input_mode": "text_test_only",
                    "user_text": f"typed test prompt {turn_number}",
                },
            )
            assert turn_response.status_code == 200
            turn_ids.append(turn_response.json()["turn_id"])

            rating_response = client.post(
                f"/api/turns/{turn_ids[-1]}/rating",
                json={
                    "stance_score": 3,
                    "trust_score": 5,
                    "client_elapsed_ms": 1200,
                },
            )
            assert rating_response.status_code == 200

        complete_response = client.post(f"/api/sessions/{session_id}/complete")

    assert complete_response.status_code == 200
    assert complete_response.json()["status"] == "completed"
    assert strict_request_ids == [f"{session_id}-turn-2-semantic-1"]


def test_test_rating_and_complete_reject_without_admin_auth(
    client: TestClient,
):
    with client:
        session_id = start_admin_test_text_session(client)
        turn_response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "typed test prompt",
            },
        )
        assert turn_response.status_code == 200
        turn_id = turn_response.json()["turn_id"]
        client.cookies.delete("aitrust_v2_admin_sid")

        rating_response = client.post(
            f"/api/turns/{turn_id}/rating",
            json={
                "stance_score": 3,
                "trust_score": 5,
                "client_elapsed_ms": 1200,
            },
        )
        complete_response = client.post(f"/api/sessions/{session_id}/complete")

    assert rating_response.status_code == 401
    assert rating_response.json() == {"detail": "Admin login required."}
    assert complete_response.status_code == 401
    assert complete_response.json() == {"detail": "Admin login required."}


def test_internal_test_participant_cannot_submit_formal_pretest_or_start_formal_session(
    client: TestClient,
    sqlite_settings: Settings,
):
    with client:
        start_admin_test_text_session(client)
        _set_internal_test_participant_cookie(client, sqlite_settings)

        draft_response = client.post(
            "/api/pretest/draft",
            json=build_pretest_payload(),
        )
        pretest_response = client.post(
            "/api/pretest/final",
            json=build_pretest_payload(),
        )
        start_response = client.post(
            "/api/sessions/start",
            json={
                "is_test": False,
                "client_info": {
                    "device_type": "desktop",
                    "viewport_width": 1440,
                    "is_secure_context": True,
                    "browser_name": "Chrome",
                    "browser_version": "126",
                    "microphone_available": True,
                    "microphone_permission": "granted",
                },
            },
        )

    assert draft_response.status_code == 401
    assert draft_response.json() == {"detail": "Invalid session."}
    assert pretest_response.status_code == 401
    assert pretest_response.json() == {"detail": "Invalid session."}
    assert start_response.status_code == 401
    assert start_response.json() == {"detail": "Invalid session."}


def test_unrated_turn_blocks_next_turn(client: TestClient):
    with client:
        session_id = login_and_prepare_formal_session(client)

        first_turn = submit_formal_voice_turn(client, session_id=session_id)
        blocked_second_turn = post_formal_asr(client, session_id=session_id)

    assert first_turn.status_code == 200
    assert blocked_second_turn.status_code == 409
    assert blocked_second_turn.json() == {
        "detail": "Turn 1 must be rated before the next turn."
    }


def test_system_failure_turn_skips_provider_and_persists_fixed_text(
    client: TestClient,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.db import get_connection
    from backend.app.services.records import SYSTEM_FAILURE_TEXT
    from backend.app.services.sessions import ProviderRouter

    with client:
        session_id = start_admin_test_text_session(client)

    conn = get_connection(sqlite_settings)
    try:
        conn.execute(
            """
            UPDATE experiment_sessions
            SET error_type_id = ?, planned_error_turn = ?
            WHERE session_uuid = ?
            """,
            ("system_failure", 1, session_id),
        )
    finally:
        conn.close()

    async def _unexpected_provider_call(*args: object, **kwargs: object) -> ProviderResponse:
        raise AssertionError("provider should not be called for planned system_failure")

    monkeypatch.setattr(ProviderRouter, "generate_chat", _unexpected_provider_call)

    with client:
        response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "typed test prompt",
            },
        )

    assert response.status_code == 200
    assert response.json()["assistant_text"] == SYSTEM_FAILURE_TEXT

    conn = get_connection(sqlite_settings)
    try:
        turn_row = conn.execute(
            """
            SELECT assistant_text, llm_provider, llm_model, llm_route, llm_attempts_json,
                   error_semantic_attempt_count, error_failure_reason,
                   s.manipulation_status
            FROM conversation_turns t
            JOIN experiment_sessions s ON s.id = t.session_id
            WHERE s.session_uuid = ?
            """,
            (session_id,),
        ).fetchone()
        api_log_count = conn.execute(
            """
            SELECT COUNT(*) FROM api_call_logs
            WHERE request_id = ?
            """,
            (f"{session_id}-turn-1",),
        ).fetchone()[0]
        risk_flag_count = conn.execute(
            """
            SELECT COUNT(*) FROM session_risk_flags f
            JOIN experiment_sessions s ON s.id = f.session_id
            WHERE s.session_uuid = ?
            """,
            (session_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert dict(turn_row) == {
        "assistant_text": SYSTEM_FAILURE_TEXT,
        "llm_provider": "local-system",
        "llm_model": "planned-system-failure-v1",
        "llm_route": "system_failure",
        "llm_attempts_json": "[]",
        "error_semantic_attempt_count": 0,
        "error_failure_reason": None,
        "manipulation_status": "presented",
    }
    assert api_log_count == 0
    assert risk_flag_count == 0


def test_planned_system_failure_response_has_stable_provider_evidence() -> None:
    from backend.app.services.sessions import _planned_system_failure_response

    response = _planned_system_failure_response()

    assert response.provider == "local-system"
    assert response.model == "planned-system-failure-v1"
    assert response.route == "system_failure"
    assert response.attempts == []


def test_test_mode_evaluator_evidence_uses_configured_deepseek() -> None:
    from backend.app.services.sessions import _evaluate_injected_error

    settings = Settings(app_env="test", deepseek_model="configured-deepseek")
    state = SimpleNamespace(
        planned_error_turn=2,
        turn_index=2,
        error_type_id="factual_minor",
        llm_provider="deepseek",
        llm_model="configured-deepseek",
        llm_route="chat",
        provider_status="success",
        error_presentation="semantic",
    )

    result = _evaluate_injected_error(
        settings=settings,
        health_service=None,
        session_uuid="test-session",
        turn_index=2,
        state=state,
        assistant_text="candidate",
        artifact_type=None,
        artifact_payload=None,
    )

    assert result is not None
    assert result["provider"] == "deepseek"
    assert result["model"] == "configured-deepseek"
    assert result["route"] == "evaluator"
    assert result["attempts"] == [
        {
            "route": "evaluator",
            "provider": "deepseek",
            "model": "configured-deepseek",
            "used_local_fallback": False,
        }
    ]


def test_weather_turn_uses_server_snapshot_without_ai_or_source_leakage(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.services.providers import ProviderRouter
    from backend.app.services.weather import WeatherService

    snapshot = build_weather_snapshot()
    observed_queries: list[str] = []

    async def _lookup(self: WeatherService, query: str):
        observed_queries.append(query)
        return snapshot

    async def _unexpected_ai(*args: object, **kwargs: object) -> ProviderResponse:
        raise AssertionError("weather facts must not be generated by an AI provider")

    monkeypatch.setattr(WeatherService, "lookup", _lookup)
    monkeypatch.setattr(ProviderRouter, "generate_chat", _unexpected_ai)

    with client:
        session_id = start_admin_test_text_session(
            client,
            planned_error_turn=5,
            condition="tool",
            subcondition="qa",
            topic_key="weather",
        )
        response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "杭州明天会下雨吗？",
            },
        )

    assert response.status_code == 200
    assert observed_queries == ["杭州"]
    payload = response.json()
    expected_text = "杭州·浙江明天：阵雨，25~31°C，降水概率70%，最大风速8m/s。"
    assert payload["assistant_text"] == expected_text
    assert payload["artifact_type"] == "weather_card"
    assert payload["artifact_payload"]["summary"] == expected_text
    serialized_card = json.dumps(payload["artifact_payload"], ensure_ascii=False)
    for hidden in ("openmeteo", "30.29365", "120.16142", "fetched_at", "query"):
        assert hidden not in serialized_card
    weather_tool = payload["graph_trace"]["weather_tool"]
    assert weather_tool["status"] == "success"
    assert weather_tool["source"]["provider"] == "openmeteo"
    assert weather_tool["source"]["location"]["latitude"] == 30.29365


@pytest.mark.parametrize(
    ("user_text", "failure_code", "expected_text", "expected_status"),
    [
        pytest.param(
            "明天呢？",
            None,
            "请告诉我具体城市/地区，以便查询天气。",
            "clarification",
            id="missing-location",
        ),
        pytest.param(
            "杭州明天呢？",
            "timeout",
            "天气服务暂时不可用，请稍后再试。",
            "failed",
            id="service-timeout",
        ),
        pytest.param(
            "火星城明天呢？",
            "location_not_found",
            "未能定位该地点，请提供更具体的城市/地区（如城市+省份/国家）。",
            "failed",
            id="location-not-found",
        ),
    ],
)
def test_weather_missing_location_and_failures_never_generate_fake_weather(
    client: TestClient,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    user_text: str,
    failure_code: str | None,
    expected_text: str,
    expected_status: str,
) -> None:
    from backend.app.services.providers import ProviderRouter
    from backend.app.services.weather import (
        WeatherService,
        WeatherServiceError,
        extract_weather_location,
    )

    lookup_calls: list[str] = []

    async def _lookup(self: WeatherService, query: str):
        lookup_calls.append(query)
        if failure_code is None:
            raise AssertionError("missing location must not call Open-Meteo")
        raise WeatherServiceError(failure_code)

    async def _unexpected_ai(*args: object, **kwargs: object) -> ProviderResponse:
        raise AssertionError("failed weather lookup must not fall back to model memory")

    monkeypatch.setattr(WeatherService, "lookup", _lookup)
    monkeypatch.setattr(ProviderRouter, "generate_chat", _unexpected_ai)

    with client:
        session_id = start_admin_test_text_session(
            client,
            planned_error_turn=5,
            condition="tool",
            subcondition="qa",
            topic_key="weather",
        )
        response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": user_text,
            },
        )

    assert response.status_code == 200
    assert response.json()["assistant_text"] == expected_text
    assert response.json()["artifact_type"] is None
    assert response.json()["artifact_payload"] is None
    weather_tool = response.json()["graph_trace"]["weather_tool"]
    assert weather_tool["status"] == expected_status
    assert weather_tool.get("error_code") == (failure_code or "location_required")
    expected_queries = []
    if failure_code is not None:
        expected_query = extract_weather_location(user_text)
        assert expected_query is not None
        expected_queries.append(expected_query)
    assert lookup_calls == expected_queries

    conn = get_connection(sqlite_settings)
    try:
        row = conn.execute(
            """
            SELECT t.llm_provider, t.llm_model, t.llm_route, t.agent_state_json
            FROM conversation_turns t
            JOIN experiment_sessions s ON s.id = t.session_id
            WHERE s.session_uuid = ?
            """,
            (session_id,),
        ).fetchone()
        api_log_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM api_call_logs l
            JOIN experiment_sessions s ON s.id = l.session_id
            WHERE s.session_uuid = ? AND l.route IN ('chat', 'evaluator')
            """,
            (session_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    expected_provider = "local-system" if failure_code is None else "openmeteo"
    expected_model = (
        "weather-location-clarification-v1"
        if failure_code is None
        else "weather-service-v1"
    )
    assert row["llm_provider"] == expected_provider
    assert row["llm_model"] == expected_model
    assert row["llm_route"] == "weather"
    assert json.loads(row["agent_state_json"])["weather_tool"] == weather_tool
    assert api_log_count == 0


def test_planned_weather_error_without_valid_source_never_calls_ai(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.services.providers import ProviderRouter
    from backend.app.services.weather import WeatherService

    async def _unexpected_lookup(*args: object, **kwargs: object):
        raise AssertionError("missing location must not call Open-Meteo")

    async def _unexpected_ai(*args: object, **kwargs: object) -> ProviderResponse:
        raise AssertionError("invalid weather context must not call an AI provider")

    monkeypatch.setattr(WeatherService, "lookup", _unexpected_lookup)
    monkeypatch.setattr(ProviderRouter, "generate_chat", _unexpected_ai)

    with client:
        session_id = start_admin_test_text_session(
            client,
            planned_error_turn=1,
            condition="tool",
            subcondition="qa",
            topic_key="weather",
        )
        response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "明天呢？",
                "operation_id": "planned-weather-without-source",
                "turn_index": 1,
            },
        )

    assert response.status_code == 200
    assert response.json()["assistant_text"] == "请告诉我具体城市/地区，以便查询天气。"
    assert response.json()["error_presented"] is False


def test_weather_followup_reuses_only_latest_successful_source_query(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.services.providers import ProviderRouter
    from backend.app.services.weather import WeatherService

    snapshot = build_weather_snapshot()
    observed_queries: list[str] = []

    async def _lookup(self: WeatherService, query: str):
        observed_queries.append(query)
        return snapshot

    async def _unexpected_ai(*args: object, **kwargs: object) -> ProviderResponse:
        raise AssertionError("weather turns must not use model-memory facts")

    monkeypatch.setattr(WeatherService, "lookup", _lookup)
    monkeypatch.setattr(ProviderRouter, "generate_chat", _unexpected_ai)

    with client:
        session_id = start_admin_test_text_session(
            client,
            planned_error_turn=5,
            condition="tool",
            subcondition="qa",
            topic_key="weather",
        )
        first = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "杭州今天怎么样？",
            },
        )
        assert first.status_code == 200
        rating = client.post(
            f"/api/turns/{first.json()['turn_id']}/rating",
            json={"stance_score": 3, "trust_score": 5},
        )
        assert rating.status_code == 200
        second = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "明天呢？",
            },
        )

    assert second.status_code == 200
    assert observed_queries == ["杭州", "杭州"]
    assert second.json()["assistant_text"].startswith("杭州·浙江明天：")


def test_physics_turn_never_calls_weather_and_never_builds_weather_artifact(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.services.providers import ProviderRouter
    from backend.app.services.weather import WeatherService

    async def _unexpected_weather(self: WeatherService, query: str):
        raise AssertionError("physics must not call Open-Meteo")

    async def _physics_response(*args: object, **kwargs: object) -> ProviderResponse:
        return ProviderResponse(
            text="万有引力是任意两个有质量物体之间的相互吸引力。",
            provider="test-provider",
            model="test-model",
            route="chat",
            attempts=[],
        )

    monkeypatch.setattr(WeatherService, "lookup", _unexpected_weather)
    monkeypatch.setattr(ProviderRouter, "generate_chat", _physics_response)

    with client:
        session_id = start_admin_test_text_session(
            client,
            planned_error_turn=5,
            condition="tool",
            subcondition="qa",
            topic_key="physics",
        )
        response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "万有引力是什么？",
            },
        )

    assert response.status_code == 200
    assert response.json()["artifact_type"] is None
    assert response.json()["artifact_payload"] is None


def test_legacy_persisted_factual_lookup_uses_weather_service(
    client: TestClient,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.services.providers import ProviderRouter
    from backend.app.services.weather import WeatherService

    observed_queries: list[str] = []

    async def _lookup(self: WeatherService, query: str):
        observed_queries.append(query)
        return build_weather_snapshot()

    async def _unexpected_ai(*args: object, **kwargs: object) -> ProviderResponse:
        raise AssertionError("legacy weather sessions must not use model-memory facts")

    monkeypatch.setattr(WeatherService, "lookup", _lookup)
    monkeypatch.setattr(ProviderRouter, "generate_chat", _unexpected_ai)

    with client:
        session_id = start_admin_test_text_session(
            client,
            planned_error_turn=5,
            condition="tool",
            subcondition="qa",
            topic_key="weather",
        )
        conn = get_connection(sqlite_settings)
        try:
            conn.execute(
                """
                UPDATE experiment_sessions
                SET topic_key = 'factual_lookup'
                WHERE session_uuid = ?
                """,
                (session_id,),
            )
        finally:
            conn.close()

        response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "杭州天气怎么样？",
            },
        )

    assert response.status_code == 200
    assert observed_queries == ["杭州"]
    assert response.json()["artifact_type"] == "weather_card"
    assert response.json()["graph_trace"]["topic_key"] == "factual_lookup"
    assert response.json()["graph_trace"]["canonical_topic_key"] == "weather"


def test_formal_weather_response_hides_source_and_provider_but_persists_audit(
    client: TestClient,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app import main as app_main
    from backend.app.services.providers import ProviderRouter
    from backend.app.services.weather import WeatherService

    class WeatherAsrClient:
        def transcribe(self, **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                status="success",
                provider="tencent",
                text="杭州明天会下雨吗？",
                latency_ms=5,
            )

    async def _lookup(self: WeatherService, query: str):
        assert query == "杭州"
        return build_weather_snapshot()

    async def _unexpected_ai(*args: object, **kwargs: object) -> ProviderResponse:
        raise AssertionError("formal weather must not use model-memory facts")

    monkeypatch.setattr(app_main, "get_asr_client", lambda _settings: WeatherAsrClient())
    monkeypatch.setattr(WeatherService, "lookup", _lookup)
    monkeypatch.setattr(ProviderRouter, "generate_chat", _unexpected_ai)

    with client:
        session_id = login_and_prepare_formal_session(
            client,
            name="Weather Privacy Participant",
            phone="13800138221",
        )
        conn = get_connection(sqlite_settings)
        try:
            conn.execute(
                """
                UPDATE experiment_sessions
                SET condition = 'tool',
                    subcondition = 'qa',
                    topic_key = 'weather',
                    scenario_id = 'tool_qa_weather_v2',
                    agent_graph_version = 'qa_graph_v2'
                WHERE session_uuid = ?
                """,
                (session_id,),
            )
        finally:
            conn.close()
        asr_response = post_formal_asr(client, session_id=session_id)
        assert asr_response.status_code == 200
        response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "voice",
                "asr_result_id": asr_response.json()["asr_result_id"],
            },
        )

    assert response.status_code == 200
    payload = response.json()
    serialized_response = json.dumps(payload, ensure_ascii=False)
    assert payload["artifact_type"] == "weather_card"
    for hidden in (
        "openmeteo",
        "30.29365",
        "120.16142",
        "provider_attempts",
        "weather_tool",
    ):
        assert hidden not in serialized_response

    conn = get_connection(sqlite_settings)
    try:
        row = conn.execute(
            """
            SELECT t.llm_provider, t.llm_model, t.llm_route, t.agent_state_json
            FROM conversation_turns t
            JOIN experiment_sessions s ON s.id = t.session_id
            WHERE s.session_uuid = ?
            """,
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["llm_provider"] == "openmeteo"
    assert row["llm_model"] == "weather-snapshot-v1"
    assert row["llm_route"] == "weather"
    internal_state = json.loads(row["agent_state_json"])
    assert internal_state["weather_tool"]["source"]["provider"] == "openmeteo"
    assert internal_state["weather_tool"]["source"]["location"]["latitude"] == 30.29365


def test_local_fallback_turn_persists_final_provider_evidence(
    client: TestClient,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.db import get_connection
    from backend.app.services.sessions import ProviderRouter

    with client:
        session_id = start_admin_test_text_session(client)

    async def _local_fallback_response(
        self: ProviderRouter,
        *,
        request_id: str,
        messages: object,
        is_test: bool,
    ) -> ProviderResponse:
        return ProviderResponse(
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

    monkeypatch.setattr(ProviderRouter, "generate_chat", _local_fallback_response)

    with client:
        response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "typed test prompt",
            },
        )

    assert response.status_code == 200
    assert {"llm_provider", "llm_model", "llm_route"}.isdisjoint(response.json())

    conn = get_connection(sqlite_settings)
    try:
        turn = conn.execute(
            """
            SELECT llm_provider, llm_model, llm_route, llm_attempts_json
            FROM conversation_turns t
            JOIN experiment_sessions s ON s.id = t.session_id
            WHERE s.session_uuid = ?
            """,
            (session_id,),
        ).fetchone()
    finally:
        conn.close()

    assert turn["llm_provider"] == "local-router"
    assert turn["llm_model"] == "fixed-text-fallback-v1"
    assert turn["llm_route"] == "chat"
    assert json.loads(turn["llm_attempts_json"]) == [
        {
            "route": "chat",
            "provider": "local-router",
            "model": "fixed-text-fallback-v1",
            "status": "local_fallback",
            "http_status": None,
            "cooldown_applied": False,
        }
    ]


def test_api_turn_transport_failure_uses_controlled_fallback_without_leaking_details(
    client: TestClient,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from backend.app.services.providers import ProviderRoute, ProviderRouter

    raw_failure = (
        "PRIVATE_TRANSPORT_HOST prompt=PRIVATE_PROMPT "
        "Authorization=Bearer PRIVATE_TOKEN key=PRIVATE_KEY"
    )
    private_sentinels = (
        "PRIVATE_TRANSPORT_HOST",
        "PRIVATE_PROMPT",
        "PRIVATE_TOKEN",
        "PRIVATE_KEY",
        "private-transport.example",
    )
    adapter_calls = {"count": 0}

    class FailingAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FailingAsyncClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, *args: object, **kwargs: object) -> object:
            adapter_calls["count"] += 1
            request = httpx.Request("POST", "https://private-transport.example/v1")
            raise httpx.ConnectError(raw_failure, request=request)

    monkeypatch.setattr(httpx, "AsyncClient", FailingAsyncClient)
    monkeypatch.setattr(
        ProviderRouter,
        "formal_chat_routes",
        lambda self: [
            ProviderRoute(
                route="chat",
                provider="test-provider",
                model="test-model",
                base_url="https://configured-test-provider.invalid/v1",
                api_key="configured-for-test",
                timeout_seconds=1.0,
                extra_body={},
            )
        ],
    )

    with client:
        session_id = login_and_prepare_formal_session(client)
        conn = get_connection(sqlite_settings)
        try:
            conn.execute(
                """
                UPDATE experiment_sessions
                SET condition = 'human',
                    subcondition = 'qa',
                    topic_key = 'advice',
                    scenario_id = 'human_qa_advice_v2',
                    agent_graph_version = 'qa_graph_v2',
                    planned_error_turn = 5
                WHERE session_uuid = ?
                """,
                (session_id,),
            )
        finally:
            conn.close()
        response = submit_formal_voice_turn(client, session_id=session_id)

    assert adapter_calls["count"] == 1
    assert response.status_code == 200
    payload = response.json()
    assert payload["assistant_text"] == "抱歉，我遇到了一些技术问题。请稍后再试。"
    assert "provider_attempts" not in payload
    assert "error_code" not in payload
    serialized_response = response.text
    for sentinel in private_sentinels:
        assert sentinel not in serialized_response

    conn = get_connection(sqlite_settings)
    try:
        rows = conn.execute(
            """
            SELECT status, error_code, error_message_summary
            FROM api_call_logs
            WHERE request_id = ?
            ORDER BY id
            """,
            (f"{session_id}-turn-1-semantic-1",),
        ).fetchall()
    finally:
        conn.close()
    serialized_logs = " | ".join(str(dict(row)) for row in rows)
    assert [dict(row) for row in rows] == [
        {
            "status": "http_error",
            "error_code": "transport_error",
            "error_message_summary": "http_error:transport_error",
        },
        {
            "status": "local_fallback",
            "error_code": None,
            "error_message_summary": "local_fallback",
        },
    ]
    for sentinel in private_sentinels:
        assert sentinel not in serialized_logs


def test_api_turn_submission_rolls_back_turn_and_flags_on_failure(
    client: TestClient,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.db import get_connection
    from backend.app.services.sessions import ProviderRouter

    with client:
        session_id = start_admin_test_text_session(client)

    async def _local_fallback_response(
        self: ProviderRouter,
        *,
        request_id: str,
        messages: object,
        is_test: bool,
    ) -> ProviderResponse:
        return ProviderResponse(
            text="抱歉，我遇到了一些技术问题。请稍后再试。",
            provider="local-router",
            model=None,
            route="chat",
            attempts=[
                ProviderAttempt(
                    route="chat",
                    provider="local-router",
                    model=None,
                    status="local_fallback",
                    error_message_summary="provider route exhausted",
                )
            ],
            used_local_fallback=True,
        )

    call_count = {"value": 0}
    original_insert_flag = HealthRepository.insert_session_risk_flag

    def _failing_insert_flag(
        self: HealthRepository,
        *,
        session_id: int,
        flag: str,
        detail_json: str = None,
    ) -> int:
        call_count["value"] += 1
        if call_count["value"] == 2:
            raise RuntimeError("fail second risk flag insert")
        return original_insert_flag(
            self,
            session_id=session_id,
            flag=flag,
            detail_json=detail_json,
        )

    monkeypatch.setattr(ProviderRouter, "generate_chat", _local_fallback_response)
    monkeypatch.setattr(
        HealthRepository,
        "insert_session_risk_flag",
        _failing_insert_flag,
    )

    with client, pytest.raises(RuntimeError, match="fail second risk flag insert"):
        client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "typed test prompt",
            },
        )

    conn = get_connection(sqlite_settings)
    try:
        turn_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM conversation_turns t
            JOIN experiment_sessions s ON s.id = t.session_id
            WHERE s.session_uuid = ?
            """,
            (session_id,),
        ).fetchone()[0]
        risk_flag_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM session_risk_flags f
            JOIN experiment_sessions s ON s.id = f.session_id
            WHERE s.session_uuid = ?
            """,
            (session_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert turn_count == 0
    assert risk_flag_count == 0


def test_api_turn_submission_rollback_keeps_provider_cooldown(
    client: TestClient,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.db import get_connection
    from backend.app.services.api_health import LoggedProviderAttempt
    from backend.app.services.sessions import ProviderRouter

    with client:
        session_id = start_admin_test_text_session(client)

    async def _provider_failure_then_fallback(
        self: ProviderRouter,
        *,
        request_id: str,
        messages: object,
        is_test: bool,
    ) -> ProviderResponse:
        attempt = ProviderAttempt(
            route="chat",
            provider="yi-zhan",
            model="gpt-5.1",
            status="http_error",
            http_status=503,
            error_code="model_not_found",
            error_message_summary=(
                'Provider rejected input: "participant secret prompt" '
                "bearer token=abc123"
            ),
            cooldown_applied=True,
        )
        self._health_service.log_attempt(
            request_id=request_id,
            attempt=LoggedProviderAttempt(
                route=attempt.route,
                provider=attempt.provider,
                model=attempt.model,
                status=attempt.status,
                http_status=attempt.http_status,
                error_code=attempt.error_code,
                error_message_summary=attempt.error_message_summary,
                cooldown_applied=attempt.cooldown_applied,
            ),
        )
        self._health_service.apply_cooldown(
            route=attempt.route,
            provider=attempt.provider,
            model=attempt.model or "",
            seconds=1800,
        )
        return ProviderResponse(
            text="抱歉，我遇到了一些技术问题。请稍后再试。",
            provider="local-router",
            model=None,
            route="chat",
            attempts=[
                attempt,
                ProviderAttempt(
                    route="chat",
                    provider="local-router",
                    model=None,
                    status="local_fallback",
                    error_message_summary="provider route exhausted",
                ),
            ],
            used_local_fallback=True,
        )

    call_count = {"value": 0}
    original_insert_flag = HealthRepository.insert_session_risk_flag

    def _failing_insert_flag(
        self: HealthRepository,
        *,
        session_id: int,
        flag: str,
        detail_json: str = None,
    ) -> int:
        call_count["value"] += 1
        if call_count["value"] == 2:
            raise RuntimeError("fail second risk flag insert")
        return original_insert_flag(
            self,
            session_id=session_id,
            flag=flag,
            detail_json=detail_json,
        )

    monkeypatch.setattr(ProviderRouter, "generate_chat", _provider_failure_then_fallback)
    monkeypatch.setattr(
        HealthRepository,
        "insert_session_risk_flag",
        _failing_insert_flag,
    )

    with client, pytest.raises(RuntimeError, match="fail second risk flag insert"):
        client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "typed test prompt",
            },
        )

    conn = get_connection(sqlite_settings)
    try:
        turn_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM conversation_turns t
            JOIN experiment_sessions s ON s.id = t.session_id
            WHERE s.session_uuid = ?
            """,
            (session_id,),
        ).fetchone()[0]
        risk_flag_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM session_risk_flags f
            JOIN experiment_sessions s ON s.id = f.session_id
            WHERE s.session_uuid = ?
            """,
            (session_id,),
        ).fetchone()[0]
        cooldown_row = conn.execute(
            """
            SELECT route, provider, model
            FROM provider_cooldowns
            WHERE route = ? AND provider = ? AND model = ?
            """,
            ("chat", "yi-zhan", "gpt-5.1"),
        ).fetchone()
    finally:
        conn.close()

    assert turn_count == 0
    assert risk_flag_count == 0
    assert dict(cooldown_row) == {
        "route": "chat",
        "provider": "yi-zhan",
        "model": "gpt-5.1",
    }


def test_api_turn_submission_releases_write_lock_during_provider_routing(
    client: TestClient,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.db import get_connection, transaction
    from backend.app.services.sessions import ProviderRouter

    with client:
        session_id = start_admin_test_text_session(client)

    provider_started = Event()
    release_provider = Event()

    async def _block_then_succeed(
        self: ProviderRouter,
        *,
        request_id: str,
        messages: object,
        is_test: bool,
    ) -> ProviderResponse:
        provider_started.set()
        assert release_provider.wait(timeout=5)

        return ProviderResponse(
            text="unlocked path response",
            provider="yi-zhan",
            model="gpt-5.1",
            route="chat",
            attempts=[
                ProviderAttempt(
                    route="chat",
                    provider="yi-zhan",
                    model="gpt-5.1",
                    status="success",
                    latency_ms=5,
                )
            ],
            used_local_fallback=False,
        )

    monkeypatch.setattr(ProviderRouter, "generate_chat", _block_then_succeed)

    with client:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                client.post,
                "/api/turns",
                json={
                    "session_id": session_id,
                    "input_mode": "text_test_only",
                    "user_text": "typed test prompt",
                    "operation_id": "turn-lock-probe-0001",
                },
            )
            assert provider_started.wait(timeout=5)
            probe_conn = get_connection(sqlite_settings)
            try:
                probe_conn.execute("PRAGMA busy_timeout = 0")
                with transaction(probe_conn):
                    probe_conn.execute(
                        "INSERT OR REPLACE INTO admin_global_controls (key, value) VALUES (?, ?)",
                        ("provider_lock_probe", "committed"),
                    )
            finally:
                probe_conn.close()
                release_provider.set()
            response = future.result(timeout=5)

    assert response.status_code == 200
    assert response.json()["assistant_text"] == "unlocked path response"


def test_api_turn_submission_releases_write_lock_during_evaluator_call(
    client: TestClient,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.db import get_connection, transaction
    from backend.app.services import sessions as session_service
    from backend.app.services.sessions import ProviderRouter

    with client:
        session_id = start_admin_test_text_session(client, planned_error_turn=1)

    conn = get_connection(sqlite_settings)
    try:
        conn.execute(
            "UPDATE experiment_sessions SET error_type_id = 'social_minor' WHERE session_uuid = ?",
            (session_id,),
        )
    finally:
        conn.close()

    async def _provider_response(*args: object, **kwargs: object) -> ProviderResponse:
        return ProviderResponse(
            text="A visible answer for evaluator testing.",
            provider="yi-zhan",
            model="gpt-5.1",
            route="chat",
            attempts=[],
        )

    evaluator_started = Event()
    release_evaluator = Event()

    def _blocked_evaluator(**kwargs: object) -> dict[str, object]:
        evaluator_started.set()
        assert release_evaluator.wait(timeout=5)
        return {
            "status": "success",
            "presented": True,
            "provider": "yi-zhan",
            "model": "gemini-3.5-flash",
            "route": "evaluator",
            "reason": "visible",
        }

    monkeypatch.setattr(ProviderRouter, "generate_chat", _provider_response)
    monkeypatch.setattr(session_service, "_evaluate_injected_error", _blocked_evaluator)

    with client:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                client.post,
                "/api/turns",
                json={
                    "session_id": session_id,
                    "input_mode": "text_test_only",
                    "user_text": "trigger evaluator",
                    "operation_id": "evaluator-lock-probe-0001",
                },
            )
            assert evaluator_started.wait(timeout=5)
            probe_conn = get_connection(sqlite_settings)
            try:
                probe_conn.execute("PRAGMA busy_timeout = 0")
                with transaction(probe_conn):
                    probe_conn.execute(
                        "INSERT OR REPLACE INTO admin_global_controls (key, value) VALUES (?, ?)",
                        ("evaluator_lock_probe", "committed"),
                    )
            finally:
                probe_conn.close()
                release_evaluator.set()
            response = future.result(timeout=5)

    assert response.status_code == 200


def test_duplicate_succeeded_turn_operation_replays_without_provider_call(
    client: TestClient,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.services.sessions import ProviderRouter

    calls = {"count": 0}

    async def _provider_response(*args: object, **kwargs: object) -> ProviderResponse:
        calls["count"] += 1
        return ProviderResponse(
            text="idempotent response",
            provider="yi-zhan",
            model="gpt-5.1",
            route="chat",
            attempts=[],
        )

    monkeypatch.setattr(ProviderRouter, "generate_chat", _provider_response)
    payload = {
        "input_mode": "text_test_only",
        "user_text": "same request",
        "operation_id": "turn-idempotency-0001",
        "turn_index": 1,
    }

    with client:
        session_id = start_admin_test_text_session(client)
        payload["session_id"] = session_id
        first = client.post("/api/turns", json=payload)
        second = client.post("/api/turns", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert calls["count"] == 1
    from backend.app.db import get_connection

    conn = get_connection(sqlite_settings)
    try:
        operation_row = conn.execute(
            """
            SELECT result_entity_id, result_json
            FROM external_operations
            WHERE operation_id = ?
            """,
            ("turn-idempotency-0001",),
        ).fetchone()
    finally:
        conn.close()
    assert operation_row["result_entity_id"] == first.json()["turn_id"]
    assert operation_row["result_json"] is None


def test_same_operation_id_is_isolated_by_turn(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.services.sessions import ProviderRouter

    calls = {"count": 0}

    async def _provider_response(*args: object, **kwargs: object) -> ProviderResponse:
        messages = kwargs.get("messages")
        is_candidate = bool(
            messages
            and getattr(messages[-1], "content", None) == "same request"
        )
        if is_candidate:
            calls["count"] += 1
            response_text = f"response {calls['count']}"
        else:
            mutation_request = json.loads(getattr(messages[-1], "content", "{}"))
            response_text = json.dumps(
                {
                    "errorTypeId": mutation_request["error_type_id"],
                    "severity": mutation_request["severity"],
                    "presentation": mutation_request["presentation"],
                    "targetKind": mutation_request["target_kind"],
                    "targetPath": "assistant_text",
                    "originalValue": mutation_request["candidate"],
                        "mutatedValue": (
                            f"{mutation_request['candidate']} 补充事实：会议提前了5分钟。"
                        ),
                    "applied": True,
                    "failureReason": None,
                    "centrality": mutation_request["centrality"],
                    "operation": "structured_text_rewrite",
                    "magnitude": "peripheral",
                    "agentGenerated": True,
                },
                ensure_ascii=False,
            )
        return ProviderResponse(
            text=response_text,
            provider="yi-zhan",
            model="gpt-5.1",
            route="chat",
            attempts=[],
        )

    monkeypatch.setattr(ProviderRouter, "generate_chat", _provider_response)
    with client:
        session_id = start_admin_test_text_session(client)
        first = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "same request",
                "operation_id": "turn-scoped-key-0001",
                "turn_index": 1,
            },
        )
        assert first.status_code == 200
        rating = client.post(
            f"/api/turns/{first.json()['turn_id']}/rating",
            json={"stance_score": 3, "trust_score": 5},
        )
        assert rating.status_code == 200
        second = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "same request",
                "operation_id": "turn-scoped-key-0001",
                "turn_index": 2,
            },
        )

    assert second.status_code == 200
    assert second.json()["turn_index"] == 2
    assert second.json()["turn_id"] != first.json()["turn_id"]
    assert calls["count"] == 2


def _semantic_test_provider_response(messages, candidate_no: int) -> str:
    raw_content = getattr(messages[-1], "content", "")
    try:
        mutation_request = json.loads(raw_content)
    except json.JSONDecodeError:
        return f"clean-candidate-{candidate_no}"
    if not isinstance(mutation_request, dict) or "candidate" not in mutation_request:
        return f"clean-candidate-{candidate_no}"
    return json.dumps(
        {
            "errorTypeId": mutation_request["error_type_id"],
            "severity": mutation_request["severity"],
            "presentation": mutation_request["presentation"],
            "targetKind": mutation_request["target_kind"],
            "targetPath": "assistant_text",
            "originalValue": mutation_request["candidate"],
            "mutatedValue": f"{mutation_request['candidate']}::mutated",
            "applied": True,
            "failureReason": None,
            "centrality": mutation_request["centrality"],
            "operation": "MODEL_OPERATION_SENTINEL",
            "magnitude": "MODEL_MAGNITUDE_SENTINEL",
            "agentGenerated": True,
        },
        ensure_ascii=False,
    )


def test_production_semantic_loop_succeeds_on_second_attempt(
    client: TestClient,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.services import sessions as session_service
    from backend.app.services.sessions import ProviderRouter

    calls = {"candidate": 0, "evaluator": 0}

    request_ids: list[str] = []
    observed_system_prompts: list[str] = []

    async def generate_chat(
        self,
        *,
        request_id,
        messages,
        is_test,
        allow_local_fallback=True,
    ):
        del self
        assert is_test is True
        assert allow_local_fallback is False
        system_prompt = messages[0].content
        assert "[指令：激活错误 -> factual_minor]" in system_prompt
        observed_system_prompts.append(system_prompt)
        request_ids.append(request_id)
        calls["candidate"] += 1
        return ProviderResponse(
            text=f"ai-error-candidate-{calls['candidate']}",
            provider="fake-provider",
            model="fake-model",
            route="chat",
            attempts=[],
        )

    def evaluate(**kwargs):
        del kwargs
        calls["evaluator"] += 1
        return {
            "status": "success",
            "presented": calls["evaluator"] >= 2,
            "provider": "fake-evaluator",
            "model": "fake-model",
            "route": "evaluator",
            "parse_attempts": 1,
            "reason": (
                "visible" if calls["evaluator"] >= 2 else "evaluator_not_presented"
            ),
            "feedback_reason": (
                None
                if calls["evaluator"] >= 2
                else "候选没有包含明确的轻微事实错误。"
            ),
        }

    monkeypatch.setattr(ProviderRouter, "generate_chat", generate_chat)
    monkeypatch.setattr(session_service, "_evaluate_injected_error", evaluate)
    with client:
        session_id = start_admin_test_text_session(client, planned_error_turn=1)
        response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "semantic retry",
                "operation_id": "semantic-second-success",
                "turn_index": 1,
            },
        )

    assert response.status_code == 200
    assert response.json()["error_presented"] is True
    assert calls == {"candidate": 2, "evaluator": 2}
    assert request_ids == [
        f"{session_id}-turn-1-semantic-1",
        f"{session_id}-turn-1-semantic-2",
    ]
    assert "【评估反馈】" not in observed_system_prompts[0]
    assert "候选没有包含明确的轻微事实错误。" in observed_system_prompts[1]
    conn = get_connection(sqlite_settings)
    try:
        row = conn.execute(
            """
            SELECT t.error_semantic_attempt_count, t.error_attempts_json,
                   t.error_mutation_json,
                   s.manipulation_status
            FROM conversation_turns t
            JOIN experiment_sessions s ON s.id = t.session_id
            WHERE s.session_uuid = ?
            """,
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["error_semantic_attempt_count"] == 2
    assert len(json.loads(row["error_attempts_json"])) == 2
    assert row["manipulation_status"] == "presented"
    mutation = json.loads(row["error_mutation_json"])
    assert mutation["originalValue"] is None
    assert mutation["mutatedValue"] == "ai-error-candidate-2"
    assert mutation["operation"] == "prompt_native_generation"


def test_semantic_loop_retries_candidate_that_discloses_planned_error(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.services import sessions as session_service
    from backend.app.services.sessions import ProviderRouter

    disclosed = (
        "明天是7月15号周一。（这里我故意说成周一，其实明天是周二，"
        "这是一个很轻微的小错误。）"
    )
    safe_error = "明天是7月15号周一，整个氛围其实挺适合慢一点的。"
    candidates = [disclosed, safe_error]
    evaluator_calls = 0

    async def generate_chat(self: ProviderRouter, **_: object) -> ProviderResponse:
        del self
        return ProviderResponse(
            text=candidates.pop(0),
            provider="fake-provider",
            model="fake-model",
            route="chat",
            attempts=[],
        )

    def evaluate(**_: object) -> dict[str, object]:
        nonlocal evaluator_calls
        evaluator_calls += 1
        return {
            "status": "success",
            "presented": True,
            "provider": "fake-evaluator",
            "model": "fake-model",
            "route": "evaluator",
            "parse_attempts": 1,
            "reason": "evaluator_presented",
        }

    monkeypatch.setattr(ProviderRouter, "generate_chat", generate_chat)
    monkeypatch.setattr(session_service, "_evaluate_injected_error", evaluate)

    with client:
        session_id = start_admin_test_text_session(client, planned_error_turn=1)
        response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "最近压力很大，明天适合做什么？",
                "operation_id": "disclosure-retry",
                "turn_index": 1,
            },
        )

    assert response.status_code == 200
    assert response.json()["assistant_text"] == safe_error
    assert "故意" not in response.text
    assert evaluator_calls == 1
    assert candidates == []


def test_five_disclosed_candidates_return_retryable_error_without_turn(
    client: TestClient,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.services import sessions as session_service
    from backend.app.services.sessions import ProviderRouter

    calls = 0

    async def generate_chat(self: ProviderRouter, **_: object) -> ProviderResponse:
        nonlocal calls
        del self
        calls += 1
        return ProviderResponse(
            text=f"这里是我故意制造的第{calls}个事实错误。",
            provider="fake-provider",
            model="fake-model",
            route="chat",
            attempts=[],
        )

    monkeypatch.setattr(ProviderRouter, "generate_chat", generate_chat)
    monkeypatch.setattr(
        session_service,
        "_evaluate_injected_error",
        lambda **_: pytest.fail("disclosed candidate must not reach evaluator"),
    )

    with client:
        session_id = start_admin_test_text_session(client, planned_error_turn=1)
        response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "请给我建议。",
                "operation_id": "disclosure-exhausted",
                "turn_index": 1,
            },
        )

    assert response.status_code == 503
    assert calls == 5
    assert "故意" not in response.text
    conn = get_connection(sqlite_settings)
    try:
        turn_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM conversation_turns t
            JOIN experiment_sessions s ON s.id = t.session_id
            WHERE s.session_uuid = ?
            """,
            (session_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert turn_count == 0


def test_evaluator_receives_all_current_session_history_only(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.services import sessions as session_service
    from backend.app.services.sessions import ProviderRouter

    generated = {"count": 0}
    observed: dict[str, object] = {}

    async def generate_chat(
        self,
        *,
        request_id,
        messages,
        is_test,
        allow_local_fallback=True,
    ):
        del self, request_id, messages, is_test, allow_local_fallback
        generated["count"] += 1
        return ProviderResponse(
            text=f"assistant reply {generated['count']}",
            provider="fake-provider",
            model="fake-model",
            route="chat",
            attempts=[],
        )

    def evaluate(**kwargs):
        observed.update(kwargs)
        return {
            "status": "success",
            "presented": True,
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "route": "evaluator",
            "parse_attempts": 1,
            "reason": "evaluator_presented",
            "feedback_reason": "候选包含目标错误。",
        }

    monkeypatch.setattr(ProviderRouter, "generate_chat", generate_chat)
    monkeypatch.setattr(session_service, "_evaluate_injected_error", evaluate)

    with client:
        session_id = start_admin_test_text_session(client, planned_error_turn=2)
        first = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "第一轮用户输入",
                "turn_index": 1,
            },
        )
        assert first.status_code == 200
        rating = client.post(
            f"/api/turns/{first.json()['turn_id']}/rating",
            json={"stance_score": 3, "trust_score": 5},
        )
        assert rating.status_code == 200
        second = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "第二轮当前输入",
                "turn_index": 2,
            },
        )

    assert second.status_code == 200
    history = observed["session_history"]
    assert [(item.role, item.text) for item in history] == [
        ("user", "第一轮用户输入"),
        ("assistant", "assistant reply 1"),
    ]
    assert observed["current_user_text"] == "第二轮当前输入"


def test_nonplanned_turn_persists_zero_semantic_audit(
    client: TestClient,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.services.sessions import ProviderRouter

    async def generate_chat(self, *, request_id, messages, is_test):
        del self, request_id, messages, is_test
        return ProviderResponse(
            text="ordinary nonplanned response",
            provider="fake-provider",
            model="fake-model",
            route="chat",
            attempts=[],
        )

    monkeypatch.setattr(ProviderRouter, "generate_chat", generate_chat)
    with client:
        session_id = start_admin_test_text_session(client, planned_error_turn=2)
        response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "ordinary turn",
                "operation_id": "nonplanned-zero-semantic-audit",
                "turn_index": 1,
            },
        )

    assert response.status_code == 200
    conn = get_connection(sqlite_settings)
    try:
        row = conn.execute(
            """
            SELECT error_semantic_attempt_count, error_attempts_json,
                   error_failure_reason
            FROM conversation_turns t
            JOIN experiment_sessions s ON s.id = t.session_id
            WHERE s.session_uuid = ?
            """,
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["error_semantic_attempt_count"] == 0
    assert json.loads(row["error_attempts_json"]) == []
    assert row["error_failure_reason"] is None


def test_production_semantic_loop_five_failures_return_fifth_ai_candidate(
    client: TestClient,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.services import sessions as session_service
    from backend.app.services.sessions import ProviderRouter

    calls = {"candidate": 0}

    async def generate_chat(
        self,
        *,
        request_id,
        messages,
        is_test,
        allow_local_fallback=True,
    ):
        del self, request_id, is_test
        assert allow_local_fallback is False
        assert any(
            "[指令：激活错误 -> factual_minor]" in message.content
            for message in messages
        )
        calls["candidate"] += 1
        return ProviderResponse(
            text=f"ai-error-candidate-{calls['candidate']}",
            provider="fake-provider",
            model="fake-model",
            route="chat",
            attempts=[],
        )

    monkeypatch.setattr(ProviderRouter, "generate_chat", generate_chat)
    monkeypatch.setattr(
        session_service,
        "_evaluate_injected_error",
        lambda **_kwargs: {
            "status": "success",
            "presented": False,
            "provider": "fake-evaluator",
            "model": "fake-model",
            "route": "evaluator",
            "parse_attempts": 1,
            "reason": "MALICIOUS_EVALUATOR_REASON_SENTINEL",
            "feedback_reason": "MALICIOUS_EVALUATOR_REASON_SENTINEL",
        },
    )
    with client:
        session_id = start_admin_test_text_session(client, planned_error_turn=1)
        response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "semantic failure",
                "operation_id": "semantic-five-failures",
                "turn_index": 1,
            },
        )

    assert response.status_code == 200
    assert response.json()["error_presented"] is False
    assert response.json()["assistant_text"] == "ai-error-candidate-5"
    assert "这个问题其实不难" not in response.json()["assistant_text"]
    assert "你应该自己想清楚" not in response.json()["assistant_text"]
    assert "MALICIOUS_EVALUATOR_REASON_SENTINEL" not in response.text
    assert calls["candidate"] == 5
    conn = get_connection(sqlite_settings)
    try:
        rows = conn.execute(
            """
            SELECT t.*, s.manipulation_status
            FROM conversation_turns t
            JOIN experiment_sessions s ON s.id = t.session_id
            WHERE s.session_uuid = ?
            """,
            (session_id,),
        ).fetchall()
        risk = conn.execute(
            """
            SELECT flag, detail_json FROM session_risk_flags f
            JOIN experiment_sessions s ON s.id = f.session_id
            WHERE s.session_uuid = ? AND flag = 'error_not_presented'
            """,
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["error_semantic_attempt_count"] == 5
    assert rows[0]["error_failure_reason"] == "evaluator_not_presented"
    assert rows[0]["error_presented"] == 0
    assert rows[0]["error_presentation"] == "none"
    assert rows[0]["manipulation_status"] == "failed"
    assert risk["flag"] == "error_not_presented"
    risk_detail = json.loads(risk["detail_json"])
    assert risk_detail["semantic_failure_code"] == "evaluator_not_presented"
    assert risk_detail["semantic_attempt_count"] == 5
    state_json = rows[0]["agent_state_json"]
    persisted_state = json.loads(state_json)
    assert persisted_state["error_presented"] is False
    assert persisted_state["error_presentation"] == "none"
    assert "ai-error-candidate-1" not in state_json
    assert "previous_failure_code" not in state_json
    persisted_evidence = " | ".join(
        str(rows[0][column])
        for column in (
            "error_mutation_json",
            "error_attempts_json",
            "error_evaluator_result_json",
            "agent_state_json",
        )
    )
    assert "MALICIOUS_EVALUATOR_REASON_SENTINEL" not in persisted_evidence
    assert "MODEL_OPERATION_SENTINEL" not in persisted_evidence
    assert "MODEL_MAGNITUDE_SENTINEL" not in persisted_evidence
    mutation_evidence = json.loads(rows[0]["error_mutation_json"])
    assert "originalValue" not in mutation_evidence
    assert "mutatedValue" not in mutation_evidence
    assert "ai-error-candidate-5" not in rows[0]["error_mutation_json"]
    assert mutation_evidence["operation"] == "prompt_native_generation"
    assert mutation_evidence["agentGenerated"] is True

    from backend.app.services.export import _select_turns

    raw_export_projection = _select_turns(
        conn=get_connection(sqlite_settings),
        session_ids=[int(rows[0]["session_id"])],
    )
    serialized_export = json.dumps(raw_export_projection, ensure_ascii=False)
    assert "MODEL_OPERATION_SENTINEL" not in serialized_export
    assert "MODEL_MAGNITUDE_SENTINEL" not in serialized_export


def test_weather_semantic_retries_fetch_authoritative_source_once(
    client: TestClient,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.services import sessions as session_service
    from backend.app.services.sessions import ProviderRouter
    from backend.app.services.weather import WeatherService

    calls = {"weather": 0, "evaluator": 0}
    request_ids: list[str] = []

    async def lookup(self, query: str):
        del self, query
        calls["weather"] += 1
        return build_weather_snapshot()

    def evaluate(**kwargs):
        del kwargs
        calls["evaluator"] += 1
        return {
            "status": "success",
            "presented": calls["evaluator"] >= 5,
            "provider": "fake-evaluator",
            "model": "fake-model",
            "route": "evaluator",
            "parse_attempts": 1,
            "reason": (
                "visible" if calls["evaluator"] >= 5 else "evaluator_not_presented"
            ),
        }

    async def generate_chat(
        self,
        *,
        request_id,
        messages,
        is_test,
        allow_local_fallback=True,
    ):
        del self
        assert is_test is True
        assert allow_local_fallback is False
        assert any(
            "[指令：激活错误 -> factual_minor]" in message.content
            and "权威上下文" in message.content
            and "杭州" in message.content
            and "28.2" in message.content
            for message in messages
        )
        request_ids.append(request_id)
        return ProviderResponse(
            text="杭州当前气温为 18.2°C。",
            provider="fake-provider",
            model="fake-model",
            route="chat",
            attempts=[],
        )

    monkeypatch.setattr(WeatherService, "lookup", lookup)
    monkeypatch.setattr(ProviderRouter, "generate_chat", generate_chat)
    monkeypatch.setattr(session_service, "_evaluate_injected_error", evaluate)
    with client:
        session_id = start_admin_test_text_session(
            client,
            planned_error_turn=1,
            condition="tool",
            subcondition="qa",
            topic_key="weather",
        )
        response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "杭州天气怎么样？",
                "operation_id": "weather-semantic-retries",
                "turn_index": 1,
            },
        )

    assert response.status_code == 200
    assert response.json()["error_presented"] is True
    assert calls == {"weather": 1, "evaluator": 5}
    assert request_ids == [
        f"{session_id}-turn-1-semantic-{attempt_no}"
        for attempt_no in range(1, 6)
    ]
    conn = get_connection(sqlite_settings)
    try:
        row = conn.execute(
            """
            SELECT t.error_semantic_attempt_count, t.agent_state_json
            FROM conversation_turns t
            JOIN experiment_sessions s ON s.id = t.session_id
            WHERE s.session_uuid = ?
            """,
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["error_semantic_attempt_count"] == 5
    weather_state = json.loads(row["agent_state_json"])["weather_tool"]
    assert weather_state["source"]["query"] == "杭州"
    assert weather_state["source"]["current"]["temperature_c"] == 28.2


def test_semantic_total_timeout_persists_no_turn_or_artifact(
    client: TestClient,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.agents.error_loop import SemanticLoopTimeout
    from backend.app.services.sessions import ProviderRouter

    cancelled = Event()
    late_effects = {"count": 0}

    async def slow_provider(
        self,
        *,
        request_id,
        messages,
        is_test,
        allow_local_fallback=True,
    ):
        del self
        assert request_id.endswith("-turn-1-semantic-1")
        assert is_test is True
        assert allow_local_fallback is False
        assert any(
            "[指令：激活错误 -> factual_minor]" in message.content
            for message in messages
        )
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        late_effects["count"] += 1
        raise AssertionError("prompt-native generation should have been cancelled")

    sqlite_settings.error_semantic_timeout_seconds = 0.001
    monkeypatch.setattr(ProviderRouter, "generate_chat", slow_provider)
    with client:
        session_id = start_admin_test_text_session(client, planned_error_turn=1)
        started_at = monotonic()
        with pytest.raises(SemanticLoopTimeout, match="semantic_loop_timeout"):
            client.post(
                "/api/turns",
                json={
                    "session_id": session_id,
                    "input_mode": "text_test_only",
                    "user_text": "timeout request",
                    "operation_id": "semantic-total-timeout",
                    "turn_index": 1,
                },
            )
        elapsed = monotonic() - started_at

    assert elapsed < 0.5
    assert cancelled.is_set()
    sleep(0.05)
    assert late_effects["count"] == 0

    conn = get_connection(sqlite_settings)
    try:
        counts = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM conversation_turns t WHERE t.session_id = s.id) AS turns,
                (SELECT COUNT(*) FROM task_artifacts a
                 JOIN conversation_turns t ON t.id = a.turn_id
                 WHERE t.session_id = s.id) AS artifacts,
                (SELECT COUNT(*) FROM api_call_logs l
                 WHERE l.session_id = s.id) AS health_writes
            FROM experiment_sessions s
            WHERE s.session_uuid = ?
            """,
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    assert counts["turns"] == 0
    assert counts["artifacts"] == 0
    assert counts["health_writes"] == 0


def test_structured_parse_retry_stays_within_one_semantic_attempt(
    client: TestClient,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.agents.structured import ScheduleArtifact, StructuredAgentResult
    from backend.app.services.sessions import ProviderRouter

    async def structured_provider(
        self,
        *,
        request_id,
        messages,
        is_test,
        schema,
        max_parse_attempts=2,
        allow_local_fallback=True,
        payload_normalizer=None,
    ):
        del self, max_parse_attempts
        assert request_id.endswith("-turn-1-semantic-1")
        assert is_test is True
        assert schema is ScheduleArtifact
        assert allow_local_fallback is False
        assert callable(payload_normalizer)
        assert any(
            "[指令：激活错误 -> factual_minor]" in message.content
            and "target_kind=" not in message.content
            for message in messages
        )
        response = ProviderResponse(
            text="{}",
            provider="fake-provider",
            model="fake-model",
            route="execution",
            attempts=[],
        )
        return StructuredAgentResult(
            value=ScheduleArtifact.model_validate(
                {
                    "assistant_text": "我已整理为日程表。",
                    "actionType": "schedule_table",
                    "actionMode": "create",
                    "status": "completed",
                    "requestedSource": "明天9点到办公室交材料。",
                    "columns": ["日期", "时间", "地点", "任务", "备注"],
                    "rows": [
                        {
                            "date": "明天",
                            "time": "09:10",
                            "location": "办公室",
                            "task": "交材料",
                            "note": "",
                        }
                    ],
                }
            ),
            response=response,
            validation_error=None,
            parse_attempts=2,
        )

    monkeypatch.setattr(
        ProviderRouter,
        "generate_structured_agent",
        structured_provider,
    )
    with client:
        session_id = start_admin_test_text_session(
            client,
            planned_error_turn=1,
            condition="tool",
            subcondition="execution",
            topic_key="taskExecution",
        )
        response = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "明天9点到办公室交材料。",
                "operation_id": "structured-parse-retry",
                "turn_index": 1,
            },
        )

    assert response.status_code == 200
    conn = get_connection(sqlite_settings)
    try:
        row = conn.execute(
            """
            SELECT error_semantic_attempt_count, error_attempts_json
            FROM conversation_turns t
            JOIN experiment_sessions s ON s.id = t.session_id
            WHERE s.session_uuid = ?
            """,
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    attempts = json.loads(row["error_attempts_json"])
    assert row["error_semantic_attempt_count"] == 1
    assert attempts[0]["structured_parse_attempts"] == 2


def test_duplicate_pending_turn_operation_returns_stable_pending_state(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.services.sessions import ProviderRouter

    provider_started = Event()
    release_provider = Event()

    async def _blocked_provider(*args: object, **kwargs: object) -> ProviderResponse:
        provider_started.set()
        assert release_provider.wait(timeout=5)
        return ProviderResponse(
            text="eventual response",
            provider="yi-zhan",
            model="gpt-5.1",
            route="chat",
            attempts=[],
        )

    monkeypatch.setattr(ProviderRouter, "generate_chat", _blocked_provider)

    with client:
        session_id = start_admin_test_text_session(client)
        payload = {
            "session_id": session_id,
            "input_mode": "text_test_only",
            "user_text": "pending request",
            "operation_id": "turn-pending-0001",
        }
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(client.post, "/api/turns", json=payload)
            assert provider_started.wait(timeout=5)
            duplicate = client.post("/api/turns", json=payload)
            release_provider.set()
            completed = future.result(timeout=5)

    assert duplicate.status_code == 409
    assert duplicate.json() == {
        "detail": {
            "code": "external_operation_pending",
            "status": "pending",
            "operation_id": "turn-pending-0001",
            "retryable": True,
            "retry_after_ms": 250,
        }
    }
    assert completed.status_code == 200


def test_provider_exhaustion_releases_turn_operation_for_same_id_retry(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.services.providers import ProviderAttempt, ProviderRoutesExhausted
    from backend.app.services.sessions import ProviderRouter

    calls = {"count": 0}

    async def _provider(
        self,
        *,
        request_id,
        messages,
        is_test,
        allow_local_fallback=True,
    ) -> ProviderResponse:
        del self, request_id, messages, is_test
        assert allow_local_fallback is False
        calls["count"] += 1
        if calls["count"] == 1:
            raise ProviderRoutesExhausted(
                [
                    ProviderAttempt(
                        route="chat",
                        provider="fake-provider",
                        model="fake-model",
                        status="http_error",
                        error_code="transport_error",
                    )
                ]
            )
        return ProviderResponse(
            text="retry generated candidate",
            provider="fake-provider",
            model="fake-model",
            route="chat",
            attempts=[],
        )

    monkeypatch.setattr(ProviderRouter, "generate_chat", _provider)
    with client:
        session_id = start_admin_test_text_session(client, planned_error_turn=1)
        payload = {
            "session_id": session_id,
            "input_mode": "text_test_only",
            "user_text": "retry strict generation",
            "operation_id": "strict-provider-retry",
            "turn_index": 1,
        }
        first = client.post("/api/turns", json=payload)
        second = client.post("/api/turns", json=payload)

    assert first.status_code == 503
    assert second.status_code == 200
    assert second.json()["assistant_text"] == "retry generated candidate"
    assert calls["count"] == 2


def test_turn_operation_id_reuse_with_different_content_is_rejected(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.services.sessions import ProviderRouter

    calls = {"count": 0}

    async def _provider_response(*args: object, **kwargs: object) -> ProviderResponse:
        calls["count"] += 1
        return ProviderResponse(
            text="first response",
            provider="yi-zhan",
            model="gpt-5.1",
            route="chat",
            attempts=[],
        )

    monkeypatch.setattr(ProviderRouter, "generate_chat", _provider_response)
    with client:
        session_id = start_admin_test_text_session(client)
        first = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "first content",
                "operation_id": "turn-key-reuse-0001",
            },
        )
        second = client.post(
            "/api/turns",
            json={
                "session_id": session_id,
                "input_mode": "text_test_only",
                "user_text": "different content",
                "operation_id": "turn-key-reuse-0001",
            },
        )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "idempotency_key_reused"
    assert calls["count"] == 1


def test_failed_turn_operation_allows_intentional_retry_with_new_id(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.services.sessions import ProviderRouter

    calls = {"count": 0}

    async def _provider_response(*args: object, **kwargs: object) -> ProviderResponse:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("simulated transport crash")
        return ProviderResponse(
            text="retry succeeded",
            provider="yi-zhan",
            model="gpt-5.1",
            route="chat",
            attempts=[],
        )

    monkeypatch.setattr(ProviderRouter, "generate_chat", _provider_response)
    with client:
        session_id = start_admin_test_text_session(client)
        base = {
            "session_id": session_id,
            "input_mode": "text_test_only",
            "user_text": "retry this request",
        }
        with pytest.raises(RuntimeError, match="simulated transport crash"):
            client.post(
                "/api/turns",
                json={**base, "operation_id": "turn-failed-0001"},
            )
        retry = client.post(
            "/api/turns",
            json={**base, "operation_id": "turn-retry-0002"},
        )

    assert retry.status_code == 200
    assert retry.json()["assistant_text"] == "retry succeeded"
    assert calls["count"] == 2


def test_turn_finalization_rejects_session_changed_during_provider_call(
    client: TestClient,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.db import get_connection
    from backend.app.services.sessions import ProviderRouter

    provider_started = Event()
    release_provider = Event()

    async def _blocked_provider(*args: object, **kwargs: object) -> ProviderResponse:
        provider_started.set()
        assert release_provider.wait(timeout=5)
        return ProviderResponse(
            text="stale result",
            provider="yi-zhan",
            model="gpt-5.1",
            route="chat",
            attempts=[],
        )

    monkeypatch.setattr(ProviderRouter, "generate_chat", _blocked_provider)
    with client:
        session_id = start_admin_test_text_session(client)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                client.post,
                "/api/turns",
                json={
                    "session_id": session_id,
                    "input_mode": "text_test_only",
                    "user_text": "become stale",
                    "operation_id": "turn-stale-0001",
                },
            )
            assert provider_started.wait(timeout=5)
            conn = get_connection(sqlite_settings)
            try:
                conn.execute(
                    "UPDATE experiment_sessions SET status = 'interrupted' WHERE session_uuid = ?",
                    (session_id,),
                )
            finally:
                conn.close()
                release_provider.set()
            response = future.result(timeout=5)

    assert response.status_code == 409
    conn = get_connection(sqlite_settings)
    try:
        turn_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM conversation_turns t
            JOIN experiment_sessions s ON s.id = t.session_id
            WHERE s.session_uuid = ?
            """,
            (session_id,),
        ).fetchone()[0]
        operation_status = conn.execute(
            "SELECT status FROM external_operations WHERE operation_id = ?",
            ("turn-stale-0001",),
        ).fetchone()[0]
    finally:
        conn.close()
    assert turn_count == 0
    assert operation_status == "failed"
