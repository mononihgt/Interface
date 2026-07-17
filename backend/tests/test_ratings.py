from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.tests.audio_fixtures import VALID_WEBM_AUDIO
from backend.app.services.providers import (
    ProviderMessage,
    ProviderResponse,
    ProviderRouter,
)
from backend.app.settings import Settings


TEST_DATE = "2026-07-02"


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
            text=f"ratings transcript {self._counter}",
            latency_ms=35,
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
    db_path = tmp_path / "ratings.db"
    return Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{db_path}",
        app_secret_key="test-secret-key",
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
            text=f"Ratings AI candidate for {request_id}.",
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


def submit_formal_voice_turn(client: TestClient, *, session_id: str):
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


def start_session_and_turn(client: TestClient) -> tuple[str, int]:
    login_response = client.post(
        "/api/auth/login",
        json={
            "name": "Ratings Participant",
            "phone": "19900000002",
            "participant_type": "short",
        },
    )
    assert login_response.status_code == 200

    pretest_response = client.post(
        "/api/pretest/final",
        json=build_pretest_payload(),
    )
    assert pretest_response.status_code == 200

    session_response = client.post(
        "/api/sessions/start",
        json={
            "is_test": False,
            "client_info": {
                "device_type": "desktop",
                "viewport_width": 1440,
                "is_secure_context": True,
                "browser_name": "Edge",
                "browser_version": "126",
                "microphone_available": True,
                "microphone_permission": "granted",
            },
        },
    )
    assert session_response.status_code == 200
    session_id = session_response.json()["session_id"]

    turn_response = submit_formal_voice_turn(client, session_id=session_id)
    assert turn_response.status_code == 200
    return session_id, turn_response.json()["turn_id"]


def test_rating_range_is_validated(client: TestClient):
    with client:
        _, turn_id = start_session_and_turn(client)

        bad_stance = client.post(
            f"/api/turns/{turn_id}/rating",
            json={
                "stance_score": 0,
                "trust_score": 4,
                "client_elapsed_ms": 500,
            },
        )
        bad_trust = client.post(
            f"/api/turns/{turn_id}/rating",
            json={
                "stance_score": 4,
                "trust_score": 8,
                "client_elapsed_ms": 500,
            },
        )

    assert bad_stance.status_code == 422
    assert bad_trust.status_code == 422


def test_rating_contract_uses_stance_and_trust():
    from backend.app.models.api import RatingSubmitRequest

    request = RatingSubmitRequest.model_validate(
        {"stance_score": 3, "trust_score": 5, "client_elapsed_ms": 1200}
    )

    assert request.stance_score == 3
    assert request.trust_score == 5


def test_submit_rating_persists_stance_score(client: TestClient):
    with client:
        _, turn_id = start_session_and_turn(client)

        response = client.post(
            f"/api/turns/{turn_id}/rating",
            json={
                "stance_score": 2,
                "trust_score": 6,
                "client_elapsed_ms": 900,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["stance_score"] == 2
    assert payload["trust_score"] == 6


def test_rating_submission_persists_and_restores_turn_state(
    client: TestClient,
    sqlite_settings: Settings,
):
    with client:
        session_id, turn_id = start_session_and_turn(client)

        rating_response = client.post(
            f"/api/turns/{turn_id}/rating",
            json={
                "stance_score": 5,
                "trust_score": 6,
                "client_elapsed_ms": 900,
            },
        )
        restore_response = client.get(f"/api/sessions/{session_id}")

    assert rating_response.status_code == 200
    rating_payload = rating_response.json()
    assert rating_payload["turn_id"] == turn_id
    assert rating_payload["stance_score"] == 5
    assert rating_payload["trust_score"] == 6
    assert rating_payload["client_elapsed_ms"] == 900

    assert restore_response.status_code == 200
    restored_turn = restore_response.json()["turns"][0]
    assert restored_turn["rating"] == rating_payload
    assert restore_response.json()["expected_turn_index"] == 2

    from backend.app.db import get_connection

    conn = get_connection(sqlite_settings)
    try:
        rating_row = conn.execute(
            """
            SELECT stance_score, trust_score, client_elapsed_ms, submitted_at
            FROM turn_ratings
            WHERE turn_id = ?
            """,
            (turn_id,),
        ).fetchone()
    finally:
        conn.close()

    assert rating_row["stance_score"] == 5
    assert rating_row["trust_score"] == 6
    assert rating_row["client_elapsed_ms"] == 900
    assert rating_row["submitted_at"] is not None


def test_fifth_rating_returns_completed_session_while_earlier_ratings_keep_contract(
    client: TestClient,
):
    with client:
        session_id, first_turn_id = start_session_and_turn(client)
        rating_payloads = []

        for turn_index in range(1, 6):
            if turn_index == 1:
                turn_id = first_turn_id
            else:
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
            rating_payloads.append(rating_response.json())

    assert [payload["turn_id"] for payload in rating_payloads[:4]]
    assert all("status" not in payload for payload in rating_payloads[:4])

    completed_session = rating_payloads[4]
    assert completed_session["session_id"] == session_id
    assert completed_session["status"] == "completed"
    assert completed_session["completed_at"] is not None
    assert completed_session["expected_turn_index"] is None
    assert len(completed_session["turns"]) == 5
    assert all(turn["rating"] is not None for turn in completed_session["turns"])


def test_formal_session_completion_updates_clean_data_audit(
    client: TestClient,
    sqlite_settings: Settings,
):
    with client:
        session_id, first_turn_id = start_session_and_turn(client)
        for turn_index in range(1, 6):
            if turn_index == 1:
                turn_id = first_turn_id
            else:
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

    from backend.app.db import get_connection

    conn = get_connection(sqlite_settings)
    try:
        audit_row = conn.execute(
            """
            SELECT
                cda.status,
                cda.reasons_json,
                cda.computed_at,
                cda.attempt_id,
                p.current_attempt_id
            FROM clean_data_audits cda
            JOIN participants p ON p.id = cda.participant_id
            WHERE p.name = ?
            """,
            ("Ratings Participant",),
        ).fetchone()
    finally:
        conn.close()

    assert audit_row is not None
    assert audit_row["attempt_id"] == audit_row["current_attempt_id"]
    assert audit_row["computed_at"] is not None


def test_session_complete_requires_five_rated_turns(client: TestClient):
    with client:
        session_id, turn_id = start_session_and_turn(client)

        unrated_complete = client.post(f"/api/sessions/{session_id}/complete")
        rating_response = client.post(
            f"/api/turns/{turn_id}/rating",
            json={
                "stance_score": 4,
                "trust_score": 4,
                "client_elapsed_ms": 700,
            },
        )
        still_incomplete = client.post(f"/api/sessions/{session_id}/complete")

    assert unrated_complete.status_code == 409
    assert unrated_complete.json() == {
        "detail": "All submitted turns must be rated before completion."
    }
    assert rating_response.status_code == 200
    assert still_incomplete.status_code == 409
    assert still_incomplete.json() == {
        "detail": "Session requires exactly 5 rated turns before completion."
    }


def test_session_complete_missing_rating_persists_risk_flag(
    client: TestClient,
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection

    with client:
        session_id, _turn_id = start_session_and_turn(client)

        response = client.post(f"/api/sessions/{session_id}/complete")

    assert response.status_code == 409
    assert response.json() == {
        "detail": "All submitted turns must be rated before completion."
    }

    conn = get_connection(sqlite_settings)
    try:
        risk_flag_row = conn.execute(
            """
            SELECT f.flag, f.detail_json
            FROM session_risk_flags f
            JOIN experiment_sessions s ON s.id = f.session_id
            WHERE s.session_uuid = ?
              AND f.flag = 'missing_rating'
            """,
            (session_id,),
        ).fetchone()
    finally:
        conn.close()

    assert risk_flag_row is not None
    assert "missing_turn_indexes" in risk_flag_row["detail_json"]
