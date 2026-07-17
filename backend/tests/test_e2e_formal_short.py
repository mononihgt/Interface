from __future__ import annotations

from collections.abc import Sequence
import csv
import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.tests.audio_fixtures import VALID_WEBM_AUDIO
from backend.app.services.export import create_v2_export
from backend.app.services.providers import (
    ProviderAttempt,
    ProviderMessage,
    ProviderResponse,
    ProviderRouter,
)
from backend.app.settings import Settings


class DateController:
    def __init__(self, initial_date: str) -> None:
        self.current_date = initial_date


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
            text=f"formal short transcript {self._counter}",
            latency_ms=45,
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
    db_path = tmp_path / "e2e-formal-short.db"
    return Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{db_path}",
        app_secret_key="SESSION_SIGNING_TEST_VALUE",
        yizhan_api_key="YIZHAN_SENTINEL_VALUE",
        aabao_api_key="AABAO_SENTINEL_VALUE",
        packyapi_api_key="PACKY_SENTINEL_VALUE",
        tencent_secret_key="TENCENT_SENTINEL_VALUE",
    )


@pytest.fixture
def client(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    from backend.app import main as app_main
    from backend.app import services

    date_controller = DateController("2026-07-02")

    monkeypatch.setattr(
        services.participants,
        "current_shanghai_date",
        lambda: date_controller.current_date,
    )
    monkeypatch.setattr(
        services.questionnaires,
        "_timestamp_now",
        lambda: f"{date_controller.current_date}T09:30:00+00:00",
    )

    try:
        from backend.app.services import participant_days
    except ImportError:
        participant_days = None

    if participant_days is not None:
        monkeypatch.setattr(
            participant_days,
            "current_shanghai_date",
            lambda: date_controller.current_date,
        )

    async def fake_generate_chat(
        self: ProviderRouter,
        *,
        request_id: str,
        messages: Sequence[ProviderMessage],
        is_test: bool,
        allow_local_fallback: bool = True,
    ) -> ProviderResponse:
        del self
        assert is_test is False
        error_generation = any(
            "[指令：激活错误 ->" in message.content
            for message in messages
        )
        assert allow_local_fallback is not error_generation
        assert messages[0].role == "system"
        assert messages[0].content
        if error_generation:
            return ProviderResponse(
                text=f"AI-generated experimental candidate for {request_id}.",
                provider="yi-zhan",
                model="gpt-5.1",
                route="chat",
                attempts=[],
                used_local_fallback=False,
            )
        assert [message.role for message in messages[1:]] == [
            "user" if index % 2 == 0 else "assistant"
            for index in range(len(messages) - 1)
        ]
        user_text = messages[-1].content
        return ProviderResponse(
            text=f"Fake provider reply for {request_id}: {user_text}",
            provider="yi-zhan",
            model="gpt-5.1",
            route="chat",
            attempts=[
                ProviderAttempt(
                    route="chat",
                    provider="yi-zhan",
                    model="gpt-5.1",
                    status="success",
                    http_status=200,
                    latency_ms=120,
                    error_code=None,
                    error_message_summary=None,
                    cooldown_applied=False,
                )
            ],
            used_local_fallback=False,
        )

    monkeypatch.setattr(app_main, "get_asr_client", lambda _settings=sqlite_settings: FakeAsrClient())
    monkeypatch.setattr(ProviderRouter, "generate_chat", fake_generate_chat)

    return TestClient(app_main.create_app(settings=sqlite_settings))


def _formal_client_info() -> dict[str, object]:
    return {
        "device_type": "desktop",
        "viewport_width": 1440,
        "is_secure_context": True,
        "browser_name": "Chrome",
        "browser_version": "126",
        "microphone_available": True,
        "microphone_permission": "granted",
    }


def _force_next_assignment_short_qa(sqlite_settings: Settings) -> None:
    from backend.app.db import get_connection, run_migrations
    from backend.app.models.domain import (
        CONDITIONS,
        ERROR_TYPE_IDS,
        PARTICIPANT_TYPES,
        SUBCONDITIONS,
    )

    target_unit = ("short", "human", "qa", "factual_minor")
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
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(participant_type, condition, subcondition, error_type_id)
            DO UPDATE SET enabled = excluded.enabled
            """,
            [
                (
                    participant_type,
                    condition,
                    subcondition,
                    error_type_id,
                    int(
                        (participant_type, condition, subcondition, error_type_id)
                        == target_unit
                    ),
                )
                for participant_type in PARTICIPANT_TYPES
                for condition in CONDITIONS
                for subcondition in SUBCONDITIONS
                for error_type_id in ERROR_TYPE_IDS
            ],
        )
    finally:
        conn.close()


def _submit_pretest_final(client: TestClient) -> None:
    response = client.post(
        "/api/pretest/final",
        json=build_pretest_payload(),
    )
    assert response.status_code == 200


def _submit_formal_voice_turn(client: TestClient, *, session_id: str) -> dict[str, object]:
    asr_response = client.post(
        "/api/asr",
        data={"session_id": session_id},
        files={"audio": ("turn.webm", VALID_WEBM_AUDIO, "audio/webm")},
    )
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
    assert turn_response.status_code == 200
    return turn_response.json()


def _submit_rating(client: TestClient, *, turn_id: int, turn_index: int) -> dict[str, object]:
    response = client.post(
        f"/api/turns/{turn_id}/rating",
        json={
            "stance_score": 3 + (turn_index % 3),
            "trust_score": 4 + (turn_index % 3),
            "client_elapsed_ms": 800 + turn_index,
        },
    )
    assert response.status_code == 200
    return response.json()


def _read_csv_rows(archive_path: Path, member_name: str) -> list[dict[str, str]]:
    with zipfile.ZipFile(archive_path) as archive:
        with archive.open(member_name) as handle:
            reader = csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8", newline=""))
            return list(reader)


def _read_archive_text(archive_path: Path) -> str:
    with zipfile.ZipFile(archive_path) as archive:
        return "\n".join(
            archive.read(member_name).decode("utf-8", errors="ignore")
            for member_name in archive.namelist()
        )


def test_short_formal_session_runs_five_turns_with_ratings(
    client: TestClient,
    sqlite_settings: Settings,
    tmp_path: Path,
) -> None:
    raw_phone = "13800138000"
    _force_next_assignment_short_qa(sqlite_settings)

    with client:
        login_response = client.post(
            "/api/auth/login",
            json={
                "name": "Formal Short Participant",
                "phone": raw_phone,
                "participant_type": "short",
            },
        )
        assert login_response.status_code == 200
        participant_payload = login_response.json()
        assert participant_payload["masked_phone"] == "138****8000"
        assert "phone" not in participant_payload

        _submit_pretest_final(client)

        start_response = client.post(
            "/api/sessions/start",
            json={"is_test": False, "client_info": _formal_client_info()},
        )
        assert start_response.status_code == 200
        session_payload = start_response.json()
        session_id = session_payload["session_id"]
        assert session_payload["expected_turn_index"] == 1
        assert session_payload["is_test"] is False

        completed_session = None
        for turn_index in range(1, 6):
            turn_payload = _submit_formal_voice_turn(client, session_id=session_id)
            assert turn_payload["turn_index"] == turn_index
            assert "session_is_test" not in turn_payload
            assert turn_payload["user_input_mode"] == "voice"

            rating_payload = _submit_rating(
                client,
                turn_id=turn_payload["turn_id"],
                turn_index=turn_index,
            )
            if turn_index < 5:
                assert rating_payload["turn_id"] == turn_payload["turn_id"]
                assert "status" not in rating_payload
            else:
                completed_session = rating_payload

            if turn_index == 3:
                restore_response = client.get(f"/api/sessions/{session_id}")
                assert restore_response.status_code == 200
                restored = restore_response.json()
                assert restored["status"] == "started"
                assert restored["expected_turn_index"] == 4
                assert len(restored["turns"]) == 3
                assert all(turn["rating"] is not None for turn in restored["turns"])

        complete_response = client.post(f"/api/sessions/{session_id}/complete")
        restore_complete = client.get(f"/api/sessions/{session_id}")

    assert completed_session is not None
    assert completed_session["status"] == "completed"
    assert complete_response.status_code == 200
    complete_payload = complete_response.json()
    assert complete_payload == completed_session
    assert complete_payload["status"] == "completed"
    assert complete_payload["expected_turn_index"] is None
    assert len(complete_payload["turns"]) == 5
    assert all(turn["rating"] is not None for turn in complete_payload["turns"])

    assert restore_complete.status_code == 200
    assert restore_complete.json()["status"] == "completed"

    from backend.app.db import get_connection

    conn = get_connection(sqlite_settings)
    try:
        export_path = tmp_path / "formal-short-export.zip"
        export_result = create_v2_export(
            conn,
            sqlite_settings,
            export_path,
            include_test=False,
        )
    finally:
        conn.close()

    assert export_result.output_path == export_path
    participants_rows = _read_csv_rows(export_path, "participants.csv")
    session_rows = _read_csv_rows(export_path, "sessions.csv")
    turn_rows = _read_csv_rows(export_path, "turns.csv")
    rating_rows = _read_csv_rows(export_path, "ratings.csv")
    archive_text = _read_archive_text(export_path)

    assert len(participants_rows) == 1
    assert participants_rows[0]["participant_id"].startswith("participant-")
    assert participants_rows[0]["attempt_id"].startswith("attempt-")
    assert {"name", "phone", "masked_phone", "phone_hash"}.isdisjoint(
        participants_rows[0]
    )
    assert len(session_rows) == 1
    assert session_rows[0]["session_uuid"] == session_id
    assert session_rows[0]["is_test"] == "0"
    assert len(turn_rows) == 5
    assert len(rating_rows) == 5
    assert raw_phone not in archive_text
    assert sqlite_settings.app_secret_key not in archive_text
    assert sqlite_settings.yizhan_api_key not in archive_text
    assert sqlite_settings.aabao_api_key not in archive_text
    assert sqlite_settings.packyapi_api_key not in archive_text
    assert sqlite_settings.tencent_secret_key not in archive_text
