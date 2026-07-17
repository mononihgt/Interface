from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.tests.audio_fixtures import VALID_WEBM_AUDIO
from backend.app.services.providers import ProviderAttempt, ProviderMessage, ProviderResponse
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
            text=f"long transcript {self._counter}",
            latency_ms=40,
        )


@pytest.fixture
def sqlite_settings(tmp_path: Path) -> Settings:
    db_path = tmp_path / "e2e-long.db"
    return Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{db_path}",
        app_secret_key="test-secret-key",
    )


@pytest.fixture
def client(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    from backend.app import main as app_main
    from backend.app import services
    from backend.app.services.providers import ProviderRouter

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
        messages: list[ProviderMessage],
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
        return ProviderResponse(
            text=f"Long-session AI candidate for {request_id}.",
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
                    latency_ms=100,
                )
            ],
            used_local_fallback=False,
        )

    monkeypatch.setattr(app_main, "get_asr_client", lambda _settings=sqlite_settings: FakeAsrClient())
    monkeypatch.setattr(ProviderRouter, "generate_chat", fake_generate_chat)

    test_client = TestClient(app_main.create_app(settings=sqlite_settings))
    test_client.date_controller = date_controller  # type: ignore[attr-defined]
    return test_client


def _formal_client_info() -> dict[str, object]:
    return {
        "device_type": "desktop",
        "viewport_width": 1440,
        "is_secure_context": True,
        "browser_name": "Edge",
        "browser_version": "126",
        "microphone_available": True,
        "microphone_permission": "granted",
    }


def _force_next_assignment_long(sqlite_settings: Settings) -> None:
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
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(participant_type, condition, subcondition, error_type_id)
            DO UPDATE SET enabled = excluded.enabled
            """,
            [
                ("short", condition, subcondition, error_type_id, 0)
                for condition in CONDITIONS
                for subcondition in SUBCONDITIONS
                for error_type_id in ERROR_TYPE_IDS
            ],
        )
    finally:
        conn.close()


def _submit_pretest_final(client: TestClient, *, calendar_date: str) -> None:
    scales = {f"q{index}": 3 for index in range(1, 27)}
    scales.update({f"q{index}": 50 for index in range(27, 48)})
    scales.update({f"confidence_q{index}": 75 for index in range(27, 47)})
    scales["q21"] = 4
    scales["q48"] = "B"
    scales["q49"] = "C"
    slider_touch_state = {f"q{index}": True for index in range(27, 48)}
    slider_touch_state.update(
        {f"confidence_q{index}": True for index in range(27, 47)}
    )
    response = client.post(
        "/api/pretest/final",
        json={
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
            "client_timestamp": f"{calendar_date}T17:30:00+08:00",
        },
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


def _submit_rating(client: TestClient, *, turn_id: int) -> None:
    response = client.post(
        f"/api/turns/{turn_id}/rating",
        json={
            "stance_score": 4,
            "trust_score": 5,
            "client_elapsed_ms": 1000,
        },
    )
    assert response.status_code == 200


def _complete_day_formal_session(client: TestClient, *, session_id: str) -> dict[str, object]:
    for turn_index in range(1, 6):
        turn_payload = _submit_formal_voice_turn(client, session_id=session_id)
        assert turn_payload["turn_index"] == turn_index
        _submit_rating(client, turn_id=turn_payload["turn_id"])

    response = client.post(f"/api/sessions/{session_id}/complete")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    return payload


def test_long_participant_three_days_keep_assignment(
    client: TestClient,
    sqlite_settings: Settings,
) -> None:
    raw_phone = "19900000006"
    calendar_dates = ["2026-07-02", "2026-07-03", "2026-07-04"]
    _force_next_assignment_long(sqlite_settings)

    session_ids: list[str] = []

    with client:
        for day_index, calendar_date in enumerate(calendar_dates, start=1):
            client.date_controller.current_date = calendar_date  # type: ignore[attr-defined]

            login_response = client.post(
                "/api/auth/login",
                json={
                    "name": "Long Participant",
                    "phone": raw_phone,
                },
            )
            assert login_response.status_code == 200
            participant_payload = login_response.json()
            assert participant_payload["participant_type"] == "long"
            assert participant_payload["target_days"] == 3
            assert participant_payload["current_day"]["day_index"] == day_index
            assert participant_payload["current_day"]["calendar_date"] == calendar_date

            assert {
                "condition",
                "subcondition",
                "topic_key",
                "error_type_id",
            }.isdisjoint(participant_payload)

            if day_index == 1:
                _submit_pretest_final(client, calendar_date=calendar_date)

            start_response = client.post(
                "/api/sessions/start",
                json={"is_test": False, "client_info": _formal_client_info()},
            )
            assert start_response.status_code == 200
            session_id = start_response.json()["session_id"]
            session_ids.append(session_id)

            complete_payload = _complete_day_formal_session(client, session_id=session_id)
            assert complete_payload["day_index"] == day_index
            assert len(complete_payload["turns"]) == 5
            assert all(turn["rating"] is not None for turn in complete_payload["turns"])

    assert len(set(session_ids)) == 3

    from backend.app.db import get_connection

    conn = get_connection(sqlite_settings)
    try:
        participant_row = conn.execute(
            """
            SELECT
                p.id,
                pa.condition,
                pa.subcondition,
                pa.topic_key,
                pa.error_type_id
            FROM participants p
            JOIN participant_attempts pa ON pa.id = p.current_attempt_id
            WHERE p.phone = ?
            """,
            (raw_phone,),
        ).fetchone()
        assert participant_row is not None
        day_rows = conn.execute(
            """
            SELECT day_index, calendar_date, status, completed_at
            FROM participant_days
            WHERE participant_id = ?
            ORDER BY day_index
            """,
            (participant_row["id"],),
        ).fetchall()
        session_rows = conn.execute(
            """
            SELECT
                session_uuid,
                status,
                participant_day_id,
                condition,
                subcondition,
                topic_key,
                error_type_id,
                completed_at
            FROM experiment_sessions
            WHERE participant_id = ?
            ORDER BY id
            """,
            (participant_row["id"],),
        ).fetchall()
    finally:
        conn.close()

    participant_assignment = (
        participant_row["condition"],
        participant_row["subcondition"],
        participant_row["topic_key"],
        participant_row["error_type_id"],
    )
    session_assignments = {
        (
            row["condition"],
            row["subcondition"],
            row["topic_key"],
            row["error_type_id"],
        )
        for row in session_rows
    }
    assert session_assignments == {participant_assignment}
    assert [(row["day_index"], row["calendar_date"], row["status"]) for row in day_rows] == [
        (1, "2026-07-02", "completed"),
        (2, "2026-07-03", "completed"),
        (3, "2026-07-04", "completed"),
    ]
    assert all(row["completed_at"] is not None for row in day_rows)
    assert [row["session_uuid"] for row in session_rows] == session_ids
    assert all(row["status"] == "completed" for row in session_rows)
    assert all(row["completed_at"] is not None for row in session_rows)
